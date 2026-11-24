"""Fuentes de patchers de GitHub para la Fase 1 de Synthesis (ADR 0019 §2.1) — corte PR-3e.

Un ``GithubPatcherSettings`` se compila desde su repositorio. Este módulo baja **un commit fijado**
(SHA completo, nunca una rama ni un tag) a la caché y devuelve su árbol, que después compila
:func:`synthesis_compilacion.compilar_patcher_local` (PR-3d, con su propia compuerta de NuGet).
Traducir el ``Tag``/``Branch`` del perfil a un SHA es trabajo del orquestador (PR-3f).

Contrato:

- **Solo ``https://github.com/<dueño>/<repo>``**: sin usuario, clave, puerto, query ni fragmento.
- **Compuerta HITL de egress** antes de cualquier proceso: muestra repositorio, commit y host.
- **Git aislado del usuario**: ``GIT_CONFIG_NOSYSTEM`` y un ``GIT_CONFIG_GLOBAL`` vacío (un
  ``url.insteadOf`` del ``~/.gitconfig`` podría redirigir github.com a otro host), sin prompts ni
  credenciales guardadas, solo protocolo https, sin symlinks ni submódulos.
- **Verificación**: ``HEAD`` tiene que ser exactamente el commit pedido.
- **Caché por (repo, commit)**: se guarda el árbol SIN ``.git`` con su hash; un acierto no abre
  red y una copia alterada falla cerrado. Publicación atómica con ``os.replace``, sin borrar.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import re
import shutil
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sky_claw.app.security.hitl import Decision, new_hitl_request_id
from sky_claw.app.security.links import exigir_arbol_copiable_sin_reparse, rmtree_link_aware
from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.synthesis_runner import con_perfil_privado, construir_entorno_dotnet
from sky_claw.local.tools.synthesis_shim import ShimConfigError, hash_arbol, hilo_hasta_terminar

if TYPE_CHECKING:
    from sky_claw.app.security.hitl import HITLGuard

__all__ = [
    "CONFIG_GIT_SEGURA",
    "FuentesGit",
    "FuentesGitError",
    "argumentos_git_checkout",
    "argumentos_git_fetch",
    "argumentos_git_init",
    "argumentos_git_ls_remote",
    "argumentos_git_rev_parse",
    "RefRemoto",
    "TIPOS_DE_REF",
    "obtener_fuentes_git",
    "ref_remoto",
    "resolver_refs_remotos",
]

logger = logging.getLogger(__name__)

_ESQUEMA_CACHE = 1
_NOMBRE_MARCA = "marca.json"
_HOST_PERMITIDO = "github.com"
_SHA_COMPLETO = re.compile(r"[0-9a-f]{40}")
_SEGMENTO = re.compile(r"[A-Za-z0-9._-]+")
_COLA_DE_SALIDA = 2000

#: Cada ``-c`` cierra una vía de escape conocida (anclado por igualdad literal en los tests).
CONFIG_GIT_SEGURA = [
    "-c",
    "core.symlinks=false",
    "-c",
    "core.longpaths=true",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "submodule.recurse=false",
    "-c",
    "credential.helper=",
    "-c",
    "core.fsmonitor=false",
]


class FuentesGitError(Exception):
    """No se pudo dejar el árbol del commit pedido en la caché (fail-closed)."""


@dataclass(frozen=True)
class FuentesGit:
    """Árbol del commit, listo para :func:`compilar_patcher_local` (``raiz_fuentes``)."""

    raiz: pathlib.Path
    arbol_sha256: str
    cache_hit: bool


def argumentos_git_init(repo: pathlib.Path) -> list[str]:
    return ["init", "--quiet", str(repo)]


def argumentos_git_fetch(repo: pathlib.Path, url: str, sha: str) -> list[str]:
    return [
        *CONFIG_GIT_SEGURA,
        "-C",
        str(repo),
        "fetch",
        "--depth",
        "1",
        "--no-tags",
        "--no-recurse-submodules",
        "--quiet",
        url,
        sha,
    ]


def argumentos_git_checkout(repo: pathlib.Path) -> list[str]:
    return [*CONFIG_GIT_SEGURA, "-C", str(repo), "checkout", "--quiet", "--detach", "FETCH_HEAD"]


def argumentos_git_rev_parse(repo: pathlib.Path) -> list[str]:
    return ["-C", str(repo), "rev-parse", "HEAD"]


def argumentos_git_ls_remote(url: str, refs: list[str]) -> list[str]:
    """``git ls-remote`` de refs exactos (sin patrones): solo lee, no baja objetos."""
    return [*CONFIG_GIT_SEGURA, "ls-remote", "--exit-code", url, *refs]


#: Nombre de tag o rama admitido: sin espacios, ``..``, ``@{``, ``\\``, ni prefijo ``-`` (no puede
#: leerse como flag). Más estricto que ``git check-ref-format``; un nombre raro falla cerrado.
_NOMBRE_DE_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")
TIPOS_DE_REF = ("tag", "branch")


@dataclass(frozen=True, order=True)
class RefRemoto:
    """Un tag o una rama de un repositorio de GitHub (``repositorio`` = identidad ``dueño/repo``)."""

    repositorio: str
    tipo: str
    nombre: str


def ref_remoto(url: str, tipo: str, nombre: str) -> RefRemoto:
    """Valida y normaliza; ``FuentesGitError`` si la URL, el tipo o el nombre no son admisibles."""
    _url_git, identidad = _normalizar_url(url)
    if tipo not in TIPOS_DE_REF:
        raise FuentesGitError(f"Tipo de ref no admitido: {tipo!r}")
    if not _NOMBRE_DE_REF.fullmatch(nombre) or ".." in nombre or "//" in nombre or nombre.endswith(("/", ".", ".lock")):
        raise FuentesGitError(f"Nombre de {tipo} inválido: {nombre!r}")
    return RefRemoto(repositorio=identidad, tipo=tipo, nombre=nombre)


def _sha_de_ls_remote(salida: bytes, ref: RefRemoto) -> str:
    """El SHA del ref en la salida de ls-remote; para un tag anotado, el commit (``^{}``)."""
    completo = f"refs/tags/{ref.nombre}" if ref.tipo == "tag" else f"refs/heads/{ref.nombre}"
    encontrados: dict[str, str] = {}
    for linea in salida.decode("utf-8", "replace").splitlines():
        partes = linea.strip().split("\t")
        if len(partes) == 2 and _SHA_COMPLETO.fullmatch(partes[0]):
            encontrados[partes[1]] = partes[0]
    sha = encontrados.get(completo + "^{}") or encontrados.get(completo)
    if sha is None:
        raise FuentesGitError(f"{ref.repositorio} no tiene el {ref.tipo} {ref.nombre!r}")
    return sha


async def resolver_refs_remotos(
    refs: list[RefRemoto],
    *,
    trabajo_raiz: pathlib.Path,
    git: pathlib.Path,
    hitl: HITLGuard | None,
    timeout_s: float,
    max_output_bytes: int,
) -> dict[RefRemoto, str]:
    """Resuelve cada tag/rama a su commit con ``git ls-remote`` (PR-3k), tras UNA compuerta HITL.

    El pedido enumera repositorios, refs y host. Corre con el mismo aislamiento que el fetch.
    """
    pendientes = sorted(set(refs))
    if not pendientes:
        return {}
    if not git.is_absolute() or not git.is_file():
        raise FuentesGitError(f"El ejecutable de git debe ser absoluto y existir: {git}")
    if not trabajo_raiz.is_absolute():
        raise FuentesGitError(f"La carpeta de trabajo debe ser una ruta absoluta: {trabajo_raiz}")
    if hitl is None:
        raise FuentesGitError("Resolver tags/ramas necesita egress (github.com) y no hay compuerta HITL: se aborta.")
    detalle = "\n".join(f"{r.repositorio}: {r.tipo} {r.nombre}" for r in pendientes)
    decision = await hitl.request_approval(
        request_id=new_hitl_request_id("synthesis-git-ls-remote"),
        reason=f"Consultar en GitHub el commit de {len(pendientes)} tag(s)/rama(s) de patchers?",
        detail=f"{detalle}\nHosts: {_HOST_PERMITIDO}\nComando: git ls-remote (solo lectura)",
        category="download",
    )
    if decision is not Decision.APPROVED:
        raise FuentesGitError(f"El operador no aprobó la consulta a GitHub (decisión={decision.value}).")

    trabajo = trabajo_raiz / f".trabajo-ls-remote-{uuid.uuid4().hex}"
    try:
        env = await asyncio.to_thread(_preparar_entorno_git, trabajo)
        resueltos: dict[RefRemoto, str] = {}
        for ref in pendientes:
            completo = f"refs/tags/{ref.nombre}" if ref.tipo == "tag" else f"refs/heads/{ref.nombre}"
            consulta = [completo, completo + "^{}"] if ref.tipo == "tag" else [completo]
            salida = await _correr_git(
                git,
                argumentos_git_ls_remote(f"https://{_HOST_PERMITIDO}/{ref.repositorio}", consulta),
                env=env,
                cwd=trabajo,
                timeout_s=timeout_s,
                max_output_bytes=max_output_bytes,
            )
            resueltos[ref] = _sha_de_ls_remote(salida, ref)
    finally:
        await asyncio.to_thread(_limpiar_trabajo, trabajo)
    return resueltos


def _normalizar_url(url: str) -> tuple[str, str]:
    """Devuelve (url canónica para git, identidad estable ``dueño/repo`` en minúsculas)."""
    try:
        partes = urlsplit(url)
        puerto = partes.port
    except ValueError as exc:
        raise FuentesGitError(f"URL de repositorio inválida: {url!r}") from exc
    if (
        partes.scheme != "https"
        or partes.hostname != _HOST_PERMITIDO
        or partes.username is not None
        or partes.password is not None
        or puerto is not None
        or partes.query
        or partes.fragment
    ):
        raise FuentesGitError(f"Solo se admiten repositorios https://{_HOST_PERMITIDO}/<dueño>/<repo>: {url!r}")
    segmentos = [s for s in partes.path.split("/") if s]
    if len(segmentos) != 2 or partes.path.endswith("//"):
        raise FuentesGitError(f"El repositorio debe ser https://{_HOST_PERMITIDO}/<dueño>/<repo>: {url!r}")
    duenio, repo = segmentos[0], segmentos[1].removesuffix(".git")
    if any(not _SEGMENTO.fullmatch(s) or s in {".", ".."} for s in (duenio, repo)):
        raise FuentesGitError(f"Dueño o repositorio inválidos en {url!r}")
    return f"https://{_HOST_PERMITIDO}/{duenio}/{repo}", f"{duenio}/{repo}".casefold()


def _verificar_entrada(final: pathlib.Path) -> FuentesGit:
    try:
        marca = json.loads((final / _NOMBRE_MARCA).read_text(encoding="utf-8"))
        if marca.get("esquema") != _ESQUEMA_CACHE:
            raise ValueError("marca fuera de esquema")
        esperado = str(marca["arbol_sha256"])
        actual = hash_arbol(final / "arbol")
    except (OSError, ValueError, KeyError, TypeError, ShimConfigError) as exc:
        raise FuentesGitError(f"La entrada de caché {final} es inválida: {exc}") from exc
    if actual != esperado:
        raise FuentesGitError(f"El árbol en caché {final} no coincide con el que se bajó (alterado).")
    return FuentesGit(raiz=final / "arbol", arbol_sha256=actual, cache_hit=True)


async def _correr_git(
    git: pathlib.Path,
    args: list[str],
    *,
    env: dict[str, str],
    cwd: pathlib.Path,
    timeout_s: float,
    max_output_bytes: int,
) -> bytes:
    try:
        stdout, stderr, rc = await run_capture(
            [str(git), *args],
            timeout=timeout_s,
            cwd=str(cwd),
            env=env,
            require_job=True,
            max_output_bytes=max_output_bytes,
        )
    except OSError as exc:
        raise FuentesGitError(f"git no pudo correr: {exc}") from exc
    if rc != 0:
        salida = (stderr or stdout).decode("utf-8", "replace")[-_COLA_DE_SALIDA:]
        raise FuentesGitError(f"git salió con {rc}: {salida}")
    return stdout


class _YaPublicadoError(Exception):
    """Otro proceso publicó la misma entrada mientras esta corrida trabajaba."""


def _limpiar_trabajo(trabajo: pathlib.Path) -> None:
    """Borra SOLO la carpeta de trabajo de este intento (nunca la entrada publicada)."""
    if not os.path.lexists(trabajo):
        return
    try:
        # limpiar_readonly: los packs de .git son de solo lectura en Windows.
        rmtree_link_aware(trabajo, limpiar_readonly=True)
    except OSError as exc:
        logger.warning("No se pudo limpiar la carpeta de trabajo %s: %s", trabajo, exc)


def _preparar_entorno_git(trabajo: pathlib.Path) -> dict[str, str]:
    """Crea *trabajo* y devuelve el entorno aislado de git (lo comparten fetch y ls-remote).

    Un HOME privado y vacío (sin ``.netrc``/``_netrc`` del usuario que autentique), sin config de
    sistema ni global (un ``url.insteadOf`` podría redirigir github.com), sin prompts.
    """
    hogar = trabajo / "home"
    config_global = trabajo / "gitconfig-vacio"
    try:
        trabajo.mkdir(parents=True)
        (trabajo / "tmp").mkdir()
        hogar.mkdir()
        config_global.write_text("", encoding="utf-8")
    except OSError as exc:
        raise FuentesGitError(f"No se pudo preparar la carpeta de trabajo de git: {exc}") from exc
    env = con_perfil_privado(construir_entorno_dotnet(dotnet_root=None, temp_dir=trabajo / "tmp"), hogar)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(config_global),
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    return env


async def _bajar_y_publicar(
    trabajo: pathlib.Path,
    final: pathlib.Path,
    *,
    git: pathlib.Path,
    url_git: str,
    sha: str,
    timeout_s: float,
    max_output_bytes: int,
) -> str:
    repo = trabajo / "repo"
    publicar = trabajo / "publicar"
    env = _preparar_entorno_git(trabajo)
    for argumentos in (
        argumentos_git_init(repo),
        argumentos_git_fetch(repo, url_git, sha),
        argumentos_git_checkout(repo),
    ):
        await _correr_git(git, argumentos, env=env, cwd=trabajo, timeout_s=timeout_s, max_output_bytes=max_output_bytes)
    salida_head = await _correr_git(
        git,
        argumentos_git_rev_parse(repo),
        env=env,
        cwd=trabajo,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    head = salida_head.decode("utf-8", "replace").strip()
    if head != sha:
        raise FuentesGitError(f"HEAD quedó en {head!r} y no en el commit pedido {sha}: se aborta.")

    # Disco pesado (copia + hash del árbol + publicación) fuera del event loop del servicio (PR-3h).
    return await hilo_hasta_terminar(_guardar_y_publicar, repo, publicar, final, url_git, sha)


def _guardar_y_publicar(repo: pathlib.Path, publicar: pathlib.Path, final: pathlib.Path, url_git: str, sha: str) -> str:
    try:
        exigir_arbol_copiable_sin_reparse(repo)
        # Se publica SOLO el árbol (sin .git) y la marca; el checkout se descarta con el trabajo.
        shutil.copytree(repo, publicar / "arbol", symlinks=True, ignore=shutil.ignore_patterns(".git"))
        arbol_sha256 = hash_arbol(publicar / "arbol")
        marca = {"esquema": _ESQUEMA_CACHE, "repositorio": url_git, "commit": sha, "arbol_sha256": arbol_sha256}
        (publicar / _NOMBRE_MARCA).write_text(json.dumps(marca, indent=2), encoding="utf-8")
    except (OSError, ShimConfigError) as exc:
        raise FuentesGitError(f"No se pudo guardar el árbol del commit en la caché: {exc}") from exc
    try:
        os.replace(publicar, final)
    except OSError as exc:
        if os.path.lexists(final):
            raise _YaPublicadoError from exc
        raise FuentesGitError(f"No se pudo publicar las fuentes en la caché ({final}): {exc}") from exc
    return arbol_sha256


async def obtener_fuentes_git(
    url: str,
    sha: str,
    *,
    cache_raiz: pathlib.Path,
    git: pathlib.Path,
    hitl: HITLGuard | None,
    timeout_s: float,
    max_output_bytes: int,
) -> FuentesGit:
    """Deja en la caché el árbol del commit *sha* de *url* y lo devuelve verificado."""
    url_git, identidad = _normalizar_url(url)
    if not _SHA_COMPLETO.fullmatch(sha):
        raise FuentesGitError(f"Se requiere el commit completo (40 hex en minúsculas), no {sha!r}.")
    if not git.is_absolute() or not git.is_file():
        raise FuentesGitError(f"El ejecutable de git debe ser absoluto y existir: {git}")
    if not cache_raiz.is_absolute():
        # git corre con cwd=<trabajo>: una ruta relativa apuntaría a otra carpeta.
        raise FuentesGitError(f"La caché debe ser una ruta absoluta: {cache_raiz}")

    clave = hashlib.sha256(f"{_ESQUEMA_CACHE}\0{identidad}\0{sha}".encode()).hexdigest()[:32]
    final = cache_raiz / clave
    if os.path.lexists(final):
        return await asyncio.to_thread(_verificar_entrada, final)

    if hitl is None:
        raise FuentesGitError("Bajar el patcher necesita egress (github.com) y no hay compuerta HITL: se aborta.")
    decision = await hitl.request_approval(
        request_id=new_hitl_request_id("synthesis-git-fetch"),
        reason=f"Bajar el patcher {identidad} desde GitHub?",
        detail=f"Repositorio: {url_git}\nCommit: {sha}\nHosts: {_HOST_PERMITIDO}\nComando: git fetch --depth 1",
        category="download",
    )
    if decision is not Decision.APPROVED:
        raise FuentesGitError(f"El operador no aprobó el egress a GitHub (decisión={decision.value}).")

    trabajo = cache_raiz / f".trabajo-{uuid.uuid4().hex}"
    try:
        arbol_sha256 = await _bajar_y_publicar(
            trabajo,
            final,
            git=git,
            url_git=url_git,
            sha=sha,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
        )
    except _YaPublicadoError:
        logger.info("Otro proceso publicó %s primero; se revalida y se usa esa entrada", final)
        return await asyncio.to_thread(_verificar_entrada, final)
    finally:
        await asyncio.to_thread(_limpiar_trabajo, trabajo)
    logger.info("Patcher %s@%s guardado en la caché %s", identidad, sha[:12], final)
    return FuentesGit(raiz=final / "arbol", arbol_sha256=arbol_sha256, cache_hit=False)

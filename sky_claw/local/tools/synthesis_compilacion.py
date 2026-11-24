"""Build de patchers locales de Synthesis en la Fase 1 (ADR 0019 §2.1/§2.4) — corte PR-3d.

La Fase 1 corre en el host, FUERA de USVFS, y deja cada patcher compilado en una caché propia para
que la Fase 2 lo lance a través del shim (PR-3b) sin compilar ni abrir red. Este módulo cubre los
patchers que ya están en disco (``SolutionPatcherSettings``: una carpeta de solución y el proyecto
dentro de ella). Los patchers Git (clonar a un commit fijado) son el corte siguiente.

Contrato:

- **Nunca compila en la carpeta del usuario.** Copia el árbol de la solución (con los proyectos
  hermanos que un ``ProjectReference`` necesita) a la caché y compila ahí.
- **Caché por contenido.** La clave sale del hash del árbol de fuentes y del proyecto. Un acierto
  revalida la huella de la salida y que el apphost no tenga CET, y NO abre red ni pide aprobación.
  Una salida alterada falla cerrado: no se recompila encima de algo que hay que investigar.
- **Compuerta HITL de egress.** ``dotnet restore`` baja paquetes de ``api.nuget.org``: sin
  aprobación explícita del operador no se lanza ningún proceso.
- **Mismo entorno que el CLI.** :func:`construir_entorno_dotnet` (una sola lista blanca, con
  ``PROGRAMFILES``, que NuGet necesita) más ``NUGET_PACKAGES`` y ``TEMP`` dentro de la caché.
- **Sin CET.** Argumentos de PR-3a (``--no-incremental``, ``-p:CETCompat=false``) y verificación
  fail-closed del apphost producido.
- **Publicación atómica.** Se compila en una carpeta de trabajo propia; se publica SOLO la salida y
  la marca con ``os.replace``, y la carpeta de trabajo se borra al salir (``rmtree_link_aware``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sky_claw.app.security.hitl import Decision, new_hitl_request_id
from sky_claw.app.security.links import exigir_arbol_copiable_sin_reparse, rmtree_link_aware
from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.synthesis_prebuild import (
    FUENTE_NUGET_APROBABLE,
    CetCompatPersistenteError,
    argumentos_dotnet_assembly_name,
    argumentos_dotnet_build_patcher,
    argumentos_dotnet_restore_patcher,
    contenido_nuget_config_aprobado,
    validar_propiedades_msbuild,
    verificar_apphost_sin_cet,
)
from sky_claw.local.tools.synthesis_runner import con_perfil_privado, construir_entorno_dotnet
from sky_claw.local.tools.synthesis_shim import ShimConfigError, hash_arbol, hilo_hasta_terminar, validar_dotnet_x64

if TYPE_CHECKING:
    from sky_claw.app.security.hitl import HITLGuard

__all__ = ["CompilacionPatcherError", "PatcherCompilado", "compilar_patcher_local"]

logger = logging.getLogger(__name__)

_ESQUEMA_CACHE = 1
_NOMBRE_MARCA = "marca.json"
# El host del pedido HITL sale de la MISMA constante que fija el restore: lo aprobado = lo efectivo.
_HOST_DE_RESTORE = urlsplit(FUENTE_NUGET_APROBABLE).hostname
_CARACTERES_PROHIBIDOS_EN_NOMBRE = frozenset('<>:"/\\|?*')
_COLA_DE_SALIDA = 2000  # caracteres de stderr/stdout que se citan en un error


class CompilacionPatcherError(Exception):
    """No se pudo dejar el patcher compilado y verificado en la caché (fail-closed)."""


@dataclass(frozen=True)
class PatcherCompilado:
    """Patcher listo para la Fase 2: ``exe`` va al shim (PR-3b)."""

    exe: pathlib.Path
    salida_sha256: str
    cache_hit: bool


def _validar_proyecto(raiz: pathlib.Path, proyecto_rel: str) -> PurePosixPath:
    rel = PurePosixPath(proyecto_rel.replace("\\", "/"))
    if rel.is_absolute() or ":" in proyecto_rel or ".." in rel.parts or not rel.parts:
        raise CompilacionPatcherError(f"El proyecto debe ser una ruta relativa dentro de la solución: {proyecto_rel!r}")
    if rel.suffix.casefold() != ".csproj":
        raise CompilacionPatcherError(f"El proyecto debe ser un .csproj: {proyecto_rel!r}")
    if not (raiz / rel).is_file():
        raise CompilacionPatcherError(f"El proyecto {proyecto_rel!r} no existe en {raiz}")
    return rel


def _identidad_toolchain(dotnet: pathlib.Path) -> str:
    """Qué toolchain compila: ruta del ``dotnet`` y las versiones de SDK y runtime instaladas.

    Se lee del disco y no con ``dotnet --version``: un acierto de caché no lanza procesos. Basta
    para invalidar la caché si se usa otro ``dotnet`` o se instala/actualiza un SDK (sin
    ``global.json``, el SDK más nuevo es el que compila).
    """
    raiz = dotnet.parent
    versiones: list[str] = []
    for sub in ("sdk", "shared/Microsoft.NETCore.App"):
        carpeta = raiz / sub
        nombres = sorted(p.name for p in carpeta.iterdir() if p.is_dir()) if carpeta.is_dir() else []
        versiones.append(f"{sub}={','.join(nombres)}")
    return f"{os.path.normcase(str(dotnet))}|{'|'.join(versiones)}"


def _clave_de_cache(
    arbol_fuentes: str, proyecto: PurePosixPath, toolchain: str, propiedades: tuple[tuple[str, str], ...] = ()
) -> str:
    material = f"{_ESQUEMA_CACHE}\0{arbol_fuentes}\0{proyecto.as_posix().casefold()}\0{toolchain}"
    if propiedades:
        # Otra propiedad, otro binario: no puede compartir entrada (sin propiedades la clave no cambia).
        material += "\0" + "\0".join(f"{clave}={valor}" for clave, valor in propiedades)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _verificar_salida(final: pathlib.Path) -> PatcherCompilado:
    """Revalida una entrada publicada de la caché; cualquier diferencia falla cerrado."""
    try:
        marca = json.loads((final / _NOMBRE_MARCA).read_text(encoding="utf-8"))
        exe = final / "salida" / str(marca["exe"])
        esperado = str(marca["salida_sha256"])
        if marca.get("esquema") != _ESQUEMA_CACHE or exe.parent != final / "salida":
            raise ValueError("marca fuera de esquema")
        actual = hash_arbol(final / "salida")
        verificar_apphost_sin_cet(exe)
    except (OSError, ValueError, KeyError, TypeError, ShimConfigError, CetCompatPersistenteError) as exc:
        raise CompilacionPatcherError(f"La entrada de caché {final} es inválida: {exc}") from exc
    if actual != esperado:
        raise CompilacionPatcherError(
            f"La salida en caché {final} no coincide con la que se compiló (alterada); revisala antes de borrarla."
        )
    return PatcherCompilado(exe=exe, salida_sha256=actual, cache_hit=True)


async def _correr_dotnet(
    dotnet: pathlib.Path,
    args: list[str],
    *,
    env: dict[str, str],
    cwd: pathlib.Path,
    timeout_s: float,
    max_output_bytes: int,
) -> bytes:
    try:
        stdout, stderr, rc = await run_capture(
            [str(dotnet), *args],
            timeout=timeout_s,
            cwd=str(cwd),
            env=env,
            require_job=True,
            max_output_bytes=max_output_bytes,
        )
    except OSError as exc:
        # Job Object no asignable, salida que excede el límite, timeout (TimeoutError) o ejecutable
        # ausente: todos son OSError. La cancelación (CancelledError) no lo es y se propaga.
        raise CompilacionPatcherError(f"dotnet {args[0]} no pudo correr: {exc}") from exc
    if rc != 0:
        salida = (stderr or stdout).decode("utf-8", "replace")[-_COLA_DE_SALIDA:]
        raise CompilacionPatcherError(f"dotnet {args[0]} salió con {rc}: {salida}")
    return stdout


def _validar_assembly_name(salida_msbuild: bytes) -> str:
    """``AssemblyName`` evaluado por MSBuild: un nombre de archivo simple, nunca una ruta."""
    nombre = salida_msbuild.decode("utf-8", "replace").strip()
    if not nombre or nombre in {".", ".."} or any(c in _CARACTERES_PROHIBIDOS_EN_NOMBRE or ord(c) < 32 for c in nombre):
        raise CompilacionPatcherError(f"AssemblyName inválido evaluado por MSBuild: {nombre!r}")
    return nombre


class _YaPublicadoError(Exception):
    """Otro build publicó la misma entrada mientras esta corrida trabajaba."""


def _limpiar_trabajo(trabajo: pathlib.Path) -> None:
    """Borra SOLO la carpeta de trabajo de este intento (nunca la entrada publicada)."""
    if not os.path.lexists(trabajo):
        return
    try:
        rmtree_link_aware(trabajo, limpiar_readonly=True)
    except OSError as exc:
        logger.warning("No se pudo limpiar la carpeta de trabajo %s: %s", trabajo, exc)


async def _compilar_y_publicar(
    trabajo: pathlib.Path,
    final: pathlib.Path,
    *,
    raiz_fuentes: pathlib.Path,
    arbol_fuentes: str,
    proyecto: PurePosixPath,
    cache_raiz: pathlib.Path,
    dotnet: pathlib.Path,
    timeout_s: float,
    max_output_bytes: int,
    propiedades: tuple[tuple[str, str], ...] = (),
) -> tuple[str, str]:
    fuentes = trabajo / "fuentes"
    hogar = trabajo / "home"
    publicar = trabajo / "publicar"
    salida = publicar / "salida"
    temporal = trabajo / "tmp"
    configfile = trabajo / "nuget.config"

    def _copiar() -> bool:
        exigir_arbol_copiable_sin_reparse(raiz_fuentes)
        temporal.mkdir(parents=True)
        hogar.mkdir()
        shutil.copytree(raiz_fuentes, fuentes, symlinks=True)
        configfile.write_text(contenido_nuget_config_aprobado(), encoding="utf-8")
        return hash_arbol(fuentes) == arbol_fuentes

    try:
        # Disco pesado (copia + hash del árbol) fuera del event loop del servicio (PR-3h).
        copia_fiel = await hilo_hasta_terminar(_copiar)
    except (OSError, ShimConfigError) as exc:
        raise CompilacionPatcherError(f"No se pudo copiar la solución a la caché: {exc}") from exc
    if not copia_fiel:
        # Lo que se compila tiene que ser lo que se hasheó para la clave: si la solución cambió
        # mientras el operador aprobaba, la caché quedaría envenenada con otra versión.
        raise CompilacionPatcherError(
            f"Las fuentes de {raiz_fuentes} cambiaron mientras se preparaba el build; se aborta sin publicar."
        )

    # Perfil privado: el build no ve (ni borra) el perfil real del usuario; ver con_perfil_privado.
    env = con_perfil_privado(construir_entorno_dotnet(dotnet_root=dotnet.parent, temp_dir=temporal), hogar)
    env["NUGET_PACKAGES"] = str(cache_raiz / "nuget")
    proyecto_copia = fuentes / proyecto
    for argumentos in (
        argumentos_dotnet_restore_patcher(proyecto_copia, configfile=configfile),
        argumentos_dotnet_build_patcher(proyecto_copia, salida=salida, propiedades=dict(propiedades)),
    ):
        await _correr_dotnet(
            dotnet,
            argumentos,
            env=env,
            cwd=proyecto_copia.parent,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
        )

    salida_msbuild = await _correr_dotnet(
        dotnet,
        argumentos_dotnet_assembly_name(proyecto_copia),
        env=env,
        cwd=proyecto_copia.parent,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    exe = salida / f"{_validar_assembly_name(salida_msbuild)}.exe"
    return await hilo_hasta_terminar(_sellar_y_publicar, exe, publicar, final, proyecto)


def _sellar_y_publicar(
    exe: pathlib.Path, publicar: pathlib.Path, final: pathlib.Path, proyecto: PurePosixPath
) -> tuple[str, str]:
    """Verifica CET, hashea la salida, escribe la marca y publica (síncrono: corre en un hilo)."""
    try:
        verificar_apphost_sin_cet(exe)
        salida_sha256 = hash_arbol(exe.parent)
    except (CetCompatPersistenteError, ShimConfigError) as exc:
        raise CompilacionPatcherError(str(exc)) from exc
    marca = {
        "esquema": _ESQUEMA_CACHE,
        "exe": exe.name,
        "salida_sha256": salida_sha256,
        "proyecto": proyecto.as_posix(),
    }
    (publicar / _NOMBRE_MARCA).write_text(json.dumps(marca, indent=2), encoding="utf-8")
    try:
        # Se publica SOLO la salida y la marca; la copia de fuentes (con obj/) se descarta.
        os.replace(publicar, final)
    except OSError as exc:
        if os.path.lexists(final):
            raise _YaPublicadoError from exc
        raise CompilacionPatcherError(f"No se pudo publicar el build en la caché ({final}): {exc}") from exc
    return exe.name, salida_sha256


async def compilar_patcher_local(
    raiz_fuentes: pathlib.Path,
    proyecto_rel: str,
    *,
    cache_raiz: pathlib.Path,
    dotnet: pathlib.Path,
    hitl: HITLGuard | None,
    timeout_s: float,
    max_output_bytes: int,
    propiedades: Mapping[str, str] | None = None,
) -> PatcherCompilado:
    """Deja el patcher de *proyecto_rel* (dentro de *raiz_fuentes*) compilado y verificado.

    Los procesos corren con ``run_capture`` (async) y la compuerta HITL en el loop del llamador; el
    disco pesado (hash y copia del árbol, verificación de la caché, publicación y limpieza) corre en
    hilos con ``asyncio.to_thread`` para no trabar el loop del servicio (PR-3h).
    """
    try:
        dotnet = validar_dotnet_x64(dotnet)
    except ShimConfigError as exc:
        raise CompilacionPatcherError(str(exc)) from exc
    if not raiz_fuentes.is_absolute() or not raiz_fuentes.is_dir():
        raise CompilacionPatcherError(f"La carpeta de la solución debe ser absoluta y existir: {raiz_fuentes}")
    if not cache_raiz.is_absolute():
        # dotnet corre con cwd=<proyecto copiado>: una ruta relativa apuntaría a otra carpeta.
        raise CompilacionPatcherError(f"La caché debe ser una ruta absoluta: {cache_raiz}")
    proyecto = _validar_proyecto(raiz_fuentes, proyecto_rel)
    try:
        propiedades_validas = validar_propiedades_msbuild(propiedades)
    except ValueError as exc:
        raise CompilacionPatcherError(str(exc)) from exc
    try:
        arbol_fuentes = await asyncio.to_thread(hash_arbol, raiz_fuentes)
    except ShimConfigError as exc:
        raise CompilacionPatcherError(str(exc)) from exc

    try:
        toolchain = await asyncio.to_thread(_identidad_toolchain, dotnet)
    except OSError as exc:
        raise CompilacionPatcherError(f"No se pudo leer el toolchain de {dotnet}: {exc}") from exc
    final = cache_raiz / _clave_de_cache(arbol_fuentes, proyecto, toolchain, propiedades_validas)
    if os.path.lexists(final):
        return await asyncio.to_thread(_verificar_salida, final)

    # Sin acierto de caché hay que abrir red: primero la compuerta, después cualquier proceso.
    detalle = (
        f"Solución: {raiz_fuentes}\nProyecto: {proyecto.as_posix()}\n"
        f"Hosts: {_HOST_DE_RESTORE}\nComando: dotnet restore (paquetes NuGet del patcher)"
    )
    if hitl is None:
        raise CompilacionPatcherError(
            "El build del patcher necesita egress (NuGet) y no hay compuerta HITL: se aborta."
        )
    decision = await hitl.request_approval(
        request_id=new_hitl_request_id("synthesis-restore"),
        reason=f"Descargar los paquetes NuGet del patcher {proyecto.stem}?",
        detail=detalle,
        category="download",
    )
    if decision is not Decision.APPROVED:
        raise CompilacionPatcherError(f"El operador no aprobó el egress del build (decisión={decision.value}).")

    trabajo = cache_raiz / f".trabajo-{uuid.uuid4().hex}"
    try:
        exe_nombre, salida_sha256 = await _compilar_y_publicar(
            trabajo,
            final,
            raiz_fuentes=raiz_fuentes,
            arbol_fuentes=arbol_fuentes,
            proyecto=proyecto,
            cache_raiz=cache_raiz,
            dotnet=dotnet,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
            propiedades=propiedades_validas,
        )
    except _YaPublicadoError:
        # Otro build del mismo patcher publicó primero: su entrada se revalida y se usa.
        logger.info("Otro build publicó %s primero; se revalida y se usa esa entrada", final)
        return await asyncio.to_thread(_verificar_salida, final)
    finally:
        await asyncio.to_thread(_limpiar_trabajo, trabajo)
    logger.info("Patcher %s compilado en la caché %s", proyecto.as_posix(), final)
    return PatcherCompilado(exe=final / "salida" / exe_nombre, salida_sha256=salida_sha256, cache_hit=False)

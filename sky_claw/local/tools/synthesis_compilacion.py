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
- **Publicación atómica y sin borrar.** Se compila en una carpeta de trabajo y se publica con
  ``os.replace``. Una corrida fallida deja su carpeta de trabajo para diagnóstico.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sky_claw.app.security.hitl import Decision, new_hitl_request_id
from sky_claw.app.security.links import exigir_arbol_copiable_sin_reparse
from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.synthesis_prebuild import (
    FUENTE_NUGET_APROBABLE,
    CetCompatPersistenteError,
    argumentos_dotnet_assembly_name,
    argumentos_dotnet_build_patcher,
    argumentos_dotnet_restore_patcher,
    contenido_nuget_config_aprobado,
    verificar_apphost_sin_cet,
)
from sky_claw.local.tools.synthesis_runner import construir_entorno_dotnet
from sky_claw.local.tools.synthesis_shim import ShimConfigError, hash_arbol, validar_dotnet_x64

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


def _clave_de_cache(arbol_fuentes: str, proyecto: PurePosixPath) -> str:
    material = f"{_ESQUEMA_CACHE}\0{arbol_fuentes}\0{proyecto.as_posix().casefold()}"
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
    stdout, stderr, rc = await run_capture(
        [str(dotnet), *args],
        timeout=timeout_s,
        cwd=str(cwd),
        env=env,
        require_job=True,
        max_output_bytes=max_output_bytes,
    )
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


async def compilar_patcher_local(
    raiz_fuentes: pathlib.Path,
    proyecto_rel: str,
    *,
    cache_raiz: pathlib.Path,
    dotnet: pathlib.Path,
    hitl: HITLGuard | None,
    timeout_s: float,
    max_output_bytes: int,
) -> PatcherCompilado:
    """Deja el patcher de *proyecto_rel* (dentro de *raiz_fuentes*) compilado y verificado.

    Los procesos corren con ``run_capture`` (async). El hash y la copia del árbol son síncronos y
    corren en el hilo del llamador: un consumidor que no pueda bloquear el loop lo llama desde un
    worker.
    """
    try:
        dotnet = validar_dotnet_x64(dotnet)
    except ShimConfigError as exc:
        raise CompilacionPatcherError(str(exc)) from exc
    if not raiz_fuentes.is_absolute() or not raiz_fuentes.is_dir():
        raise CompilacionPatcherError(f"La carpeta de la solución debe ser absoluta y existir: {raiz_fuentes}")
    proyecto = _validar_proyecto(raiz_fuentes, proyecto_rel)
    try:
        arbol_fuentes = hash_arbol(raiz_fuentes)
    except ShimConfigError as exc:
        raise CompilacionPatcherError(str(exc)) from exc

    final = cache_raiz / _clave_de_cache(arbol_fuentes, proyecto)
    if os.path.lexists(final):
        return _verificar_salida(final)

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
    fuentes = trabajo / "fuentes"
    salida = trabajo / "salida"
    temporal = trabajo / "tmp"
    try:
        exigir_arbol_copiable_sin_reparse(raiz_fuentes)
        temporal.mkdir(parents=True)
        shutil.copytree(raiz_fuentes, fuentes, symlinks=True)
        copia_fiel = hash_arbol(fuentes) == arbol_fuentes
        configfile = trabajo / "nuget.config"
        configfile.write_text(contenido_nuget_config_aprobado(), encoding="utf-8")
    except (OSError, ShimConfigError) as exc:
        raise CompilacionPatcherError(f"No se pudo copiar la solución a la caché: {exc}") from exc
    if not copia_fiel:
        # Lo que se compila tiene que ser lo que se hasheó para la clave: si la solución cambió
        # mientras el operador aprobaba, la caché quedaría envenenada con otra versión.
        raise CompilacionPatcherError(
            f"Las fuentes de {raiz_fuentes} cambiaron mientras se preparaba el build; se aborta sin publicar."
        )

    env = construir_entorno_dotnet(dotnet_root=dotnet.parent, temp_dir=temporal)
    env["NUGET_PACKAGES"] = str(cache_raiz / "nuget")
    proyecto_copia = fuentes / proyecto
    for argumentos in (
        argumentos_dotnet_restore_patcher(proyecto_copia, configfile=configfile),
        argumentos_dotnet_build_patcher(proyecto_copia, salida=salida),
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
    try:
        verificar_apphost_sin_cet(exe)
        salida_sha256 = hash_arbol(salida)
    except (CetCompatPersistenteError, ShimConfigError) as exc:
        raise CompilacionPatcherError(str(exc)) from exc
    marca = {
        "esquema": _ESQUEMA_CACHE,
        "exe": exe.name,
        "salida_sha256": salida_sha256,
        "proyecto": proyecto.as_posix(),
    }
    (trabajo / _NOMBRE_MARCA).write_text(json.dumps(marca, indent=2), encoding="utf-8")
    try:
        os.replace(trabajo, final)
    except OSError as exc:
        raise CompilacionPatcherError(f"No se pudo publicar el build en la caché ({final}): {exc}") from exc
    logger.info("Patcher %s compilado en la caché %s", proyecto.as_posix(), final)
    return PatcherCompilado(exe=final / "salida" / exe.name, salida_sha256=salida_sha256, cache_hit=False)

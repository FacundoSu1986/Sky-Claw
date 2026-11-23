"""Orquestador de la Fase 1 de Synthesis (ADR 0019 §2.1) — corte PR-3f.

Une los cortes anteriores de PR-3 para dejar, por cada patcher seleccionado del perfil V2, un shim
listo para la Fase 2 bajo USVFS:

- **CLI** (``CliPatcherSettings``): su ``PathToExecutable`` tal cual (absoluto), sin carpetas de datos
  (``CliPatcherRun`` no las pasa).
- **Solution** (``SolutionPatcherSettings``): se compila con PR-3d desde la carpeta de la solución.
- **Git** (``GithubPatcherSettings``): solo ``PatcherVersioning=Commit`` con ``TargetCommit`` completo;
  se baja con PR-3e y se compila con PR-3d. ``Tag``/``Branch`` fallan cerrado: resolverlos a un commit
  consulta la red y es trabajo de los presets.

Para Solution y Git, las carpetas de datos siguen a upstream (``ConstructSolutionPatcherRunArgs``,
``DefaultDataPathProvider`` y ``PatcherInternalDataPathProvider`` de 0.36.6): ``<dir del .csproj>/Data``
como ``-f`` y ``<dir del .csproj>/InternalData`` como ``-i``, cada una solo si existe.

El shim propio se compila una vez con PR-3d desde las fuentes del paquete; cada patcher recibe su copia
(PR-3b) con la decisión CET de PR-3a. El resultado es el mapa ``ID canónico → shim`` que consume el
traductor de PR-3c (``prepare_synthesis_cli_settings(cli_executables=...)``).

La lectura del perfil es la MISMA que usa el traductor (:meth:`PatcherPipeline.seleccionar_patchers_v2`).
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from sky_claw.local.tools.patcher_pipeline import PatcherPipeline, PatcherPipelineError, PatcherV2Seleccionado
from sky_claw.local.tools.synthesis_compilacion import CompilacionPatcherError, compilar_patcher_local
from sky_claw.local.tools.synthesis_fuentes_git import FuentesGitError, obtener_fuentes_git
from sky_claw.local.tools.synthesis_shim import (
    NOMBRE_EJECUTABLE_SHIM,
    ShimConfigError,
    construir_config_shim,
    directorio_fuentes_shim,
    materializar_shim,
)

if TYPE_CHECKING:
    from sky_claw.app.security.hitl import HITLGuard

__all__ = ["PREPARADORES", "Fase1Error", "ResultadoFase1", "preparar_fase1"]

logger = logging.getLogger(__name__)

_PROYECTO_SHIM = pathlib.Path(NOMBRE_EJECUTABLE_SHIM).with_suffix(".csproj").name
_SHA_COMPLETO = re.compile(r"[0-9a-f]{40}")
#: ``PatcherVersioningEnum.Commit`` de upstream: ``StringEnumConverter`` lo escribe como texto y
#: Newtonsoft acepta también el valor numérico (Tag=0, Branch=1, Commit=2).
_VERSIONADO_COMMIT = ("commit", 2)


class Fase1Error(Exception):
    """La Fase 1 no pudo dejar listo algún patcher; el mensaje nombra cuál (fail-closed)."""


@dataclass(frozen=True)
class ResultadoFase1:
    """``ejecutables``: ID canónico → shim, para ``prepare_synthesis_cli_settings(cli_executables=...)``."""

    ejecutables: dict[str, pathlib.Path]


@dataclass(frozen=True)
class _Contexto:
    cache_raiz: pathlib.Path
    dotnet: pathlib.Path
    git: pathlib.Path
    hitl: HITLGuard | None
    timeout_s: float
    max_output_bytes: int


@dataclass(frozen=True)
class _PatcherListo:
    exe: pathlib.Path
    internal_data: pathlib.Path | None
    default_data: pathlib.Path | None


def _texto(datos: dict[str, Any], clave: str) -> str:
    valor = datos.get(clave)
    if not isinstance(valor, str) or not valor.strip():
        raise Fase1Error(f"falta {clave}")
    return valor.strip()


def _carpetas_de_datos(proyecto: pathlib.Path) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    """(InternalData, Data) junto al .csproj, cada una solo si existe (como upstream)."""
    carpeta = proyecto.parent
    internal = carpeta / "InternalData"
    default = carpeta / "Data"
    return (internal if internal.is_dir() else None, default if default.is_dir() else None)


async def _compilar(raiz: pathlib.Path, proyecto_rel: str, ctx: _Contexto) -> _PatcherListo:
    compilado = await compilar_patcher_local(
        raiz,
        proyecto_rel,
        cache_raiz=ctx.cache_raiz / "builds",
        dotnet=ctx.dotnet,
        hitl=ctx.hitl,
        timeout_s=ctx.timeout_s,
        max_output_bytes=ctx.max_output_bytes,
    )
    internal, default = _carpetas_de_datos(raiz / PurePosixPath(proyecto_rel.replace("\\", "/")))
    return _PatcherListo(exe=compilado.exe, internal_data=internal, default_data=default)


async def _preparar_cli(datos: dict[str, Any], ctx: _Contexto) -> _PatcherListo:
    exe = pathlib.Path(_texto(datos, "PathToExecutable"))
    if not exe.is_absolute():
        raise Fase1Error(f"PathToExecutable debe ser absoluto: {exe}")
    return _PatcherListo(exe=exe, internal_data=None, default_data=None)


async def _preparar_solution(datos: dict[str, Any], ctx: _Contexto) -> _PatcherListo:
    solucion = pathlib.Path(_texto(datos, "SolutionPath"))
    if not solucion.is_absolute() or not solucion.is_file():
        raise Fase1Error(f"SolutionPath debe ser un archivo absoluto existente: {solucion}")
    return await _compilar(solucion.parent, _texto(datos, "ProjectSubpath"), ctx)


async def _preparar_git(datos: dict[str, Any], ctx: _Contexto) -> _PatcherListo:
    versionado = datos.get("PatcherVersioning", "Branch")  # default upstream: Branch
    es_commit = (isinstance(versionado, str) and versionado.casefold() == _VERSIONADO_COMMIT[0]) or (
        type(versionado) is int and versionado == _VERSIONADO_COMMIT[1]
    )
    commit = datos.get("TargetCommit")
    if not es_commit or not isinstance(commit, str) or not _SHA_COMPLETO.fullmatch(commit):
        raise Fase1Error(
            "solo se admite un patcher Git fijado por commit (PatcherVersioning=Commit y TargetCommit de 40 hex); "
            f"versionado={versionado!r}"
        )
    fuentes = await obtener_fuentes_git(
        _texto(datos, "RemoteRepoPath"),
        commit,
        cache_raiz=ctx.cache_raiz / "git",
        git=ctx.git,
        hitl=ctx.hitl,
        timeout_s=ctx.timeout_s,
        max_output_bytes=ctx.max_output_bytes,
    )
    return await _compilar(fuentes.raiz, _texto(datos, "SelectedProjectSubpath"), ctx)


#: Tipo (de ``TIPO_POR_PATCHER_TYPE``) → preparación. Igualdad anclada en los tests: un tipo admitido
#: sin preparación rompe el ancla.
PREPARADORES: dict[str, Callable[[dict[str, Any], _Contexto], Awaitable[_PatcherListo]]] = {
    "cli": _preparar_cli,
    "solution": _preparar_solution,
    "git": _preparar_git,
}


def _nombre_seguro(canonical_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", canonical_id)[:60] or "patcher"


async def preparar_fase1(
    settings_path: pathlib.Path,
    profile_identifier: str,
    patcher_ids: list[str] | None,
    *,
    cache_raiz: pathlib.Path,
    directorio_shims: pathlib.Path,
    dotnet: pathlib.Path,
    git: pathlib.Path,
    hitl: HITLGuard | None,
    timeout_s: float,
    max_output_bytes: int,
) -> ResultadoFase1:
    """Deja un shim por patcher seleccionado en *directorio_shims* (nuevo, de esta corrida)."""
    if not directorio_shims.is_absolute() or os.path.lexists(directorio_shims):
        raise Fase1Error(f"El directorio de shims debe ser absoluto y nuevo (no existe): {directorio_shims}")
    try:
        seleccion = PatcherPipeline.seleccionar_patchers_v2(settings_path, profile_identifier, patcher_ids)
    except PatcherPipelineError as exc:
        raise Fase1Error(f"Perfil de Synthesis inválido: {exc}") from exc
    ctx = _Contexto(
        cache_raiz=cache_raiz,
        dotnet=dotnet,
        git=git,
        hitl=hitl,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )

    # Primero los patchers: un rechazo corta antes de compilar el shim o materializar nada.
    listos: list[tuple[PatcherV2Seleccionado, _PatcherListo]] = []
    for patcher in seleccion:
        try:
            listos.append((patcher, await PREPARADORES[patcher.tipo](patcher.datos, ctx)))
        except (Fase1Error, CompilacionPatcherError, FuentesGitError) as exc:
            raise Fase1Error(f"Patcher {patcher.canonical_id!r} ({patcher.tipo}): {exc}") from exc

    configs: list[tuple[PatcherV2Seleccionado, dict[str, object]]] = []
    for patcher, listo in listos:
        try:
            configs.append(
                (
                    patcher,
                    construir_config_shim(
                        listo.exe,
                        dotnet=ctx.dotnet,
                        internal_data_folder=listo.internal_data,
                        default_data_folder_path=listo.default_data,
                    ),
                )
            )
        except ShimConfigError as exc:
            raise Fase1Error(f"Patcher {patcher.canonical_id!r}: {exc}") from exc

    try:
        shim = await compilar_patcher_local(
            directorio_fuentes_shim(),
            _PROYECTO_SHIM,
            cache_raiz=cache_raiz / "builds",
            dotnet=dotnet,
            hitl=hitl,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
        )
    except CompilacionPatcherError as exc:
        raise Fase1Error(f"No se pudo compilar el shim de Sky-Claw: {exc}") from exc

    ejecutables: dict[str, pathlib.Path] = {}
    for indice, (patcher, config) in enumerate(configs):
        destino = directorio_shims / f"{indice:02d}-{_nombre_seguro(patcher.canonical_id)}"
        try:
            ejecutables[patcher.canonical_id] = materializar_shim(shim.exe.parent, destino, config)
        except ShimConfigError as exc:
            raise Fase1Error(f"Patcher {patcher.canonical_id!r}: {exc}") from exc
    logger.info("Fase 1 de Synthesis lista: %d patcher(s) con shim en %s", len(ejecutables), directorio_shims)
    return ResultadoFase1(ejecutables=ejecutables)

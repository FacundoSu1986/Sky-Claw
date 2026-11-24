"""Orquestador de la Fase 1 de Synthesis (ADR 0019 §2.1) — corte PR-3f.

Une los cortes anteriores de PR-3 para dejar, por cada patcher seleccionado del perfil V2, un shim
listo para la Fase 2 bajo USVFS:

- **CLI** (``CliPatcherSettings``): su ``PathToExecutable`` tal cual (absoluto), sin carpetas de datos
  (``CliPatcherRun`` no las pasa).
- **Solution** (``SolutionPatcherSettings``): se compila con PR-3d desde la carpeta de la solución.
- **Git** (``GithubPatcherSettings``): el versionado se interpreta como upstream
  (``GitPatcherVersioning.Factory`` 0.36.6, ver :func:`versionado_efectivo`): ``Commit``, ``Tag`` sin
  ``LatestTag`` y ``Branch`` sin ``AutoUpdateToBranchTip`` (el default) corren su ``TargetCommit``; un
  ``Tag`` con ``LatestTag`` o una ``Branch`` con ``AutoUpdateToBranchTip`` corren el commit que fija el
  ``SynthesisPreset`` (PR-3k, :mod:`synthesis_presets`), nunca uno consultado en el momento. Se baja con
  PR-3e y se compila con PR-3d.

Para Solution y Git, las carpetas de datos siguen a upstream (``ConstructSolutionPatcherRunArgs``,
``DefaultDataPathProvider`` y ``PatcherInternalDataPathProvider`` de 0.36.6): ``<dir del .csproj>/Data``
como ``-f`` y ``<dir del .csproj>/InternalData`` como ``-i``, cada una solo si existe.

El shim propio se compila una vez con PR-3d desde las fuentes del paquete; cada patcher recibe su copia
(PR-3b) con la decisión CET de PR-3a. El resultado es el mapa ``ID canónico → shim`` que consume el
traductor de PR-3c (``prepare_synthesis_cli_settings(cli_executables=...)``).

La lectura del perfil es la MISMA que usa el traductor (:meth:`PatcherPipeline.seleccionar_patchers_v2`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

from sky_claw.app.security.links import rmtree_link_aware
from sky_claw.local.tools.patcher_pipeline import PatcherPipeline, PatcherPipelineError, PatcherV2Seleccionado
from sky_claw.local.tools.synthesis_compilacion import CompilacionPatcherError, compilar_patcher_local
from sky_claw.local.tools.synthesis_fuentes_git import (
    FuentesGitError,
    RefRemoto,
    obtener_fuentes_git,
    ref_remoto,
)
from sky_claw.local.tools.synthesis_shim import (
    NOMBRE_EJECUTABLE_SHIM,
    ShimConfigError,
    construir_config_shim,
    directorio_fuentes_shim,
    materializar_shim,
)

if TYPE_CHECKING:
    from sky_claw.app.security.hitl import HITLGuard

__all__ = [
    "PREPARADORES",
    "Fase1Error",
    "PreparadorFase1",
    "PreparadorFase1Protocol",
    "ResultadoFase1",
    "preparar_fase1",
    "ref_dinamico",
    "versionado_efectivo",
]

logger = logging.getLogger(__name__)

_PROYECTO_SHIM = pathlib.Path(NOMBRE_EJECUTABLE_SHIM).with_suffix(".csproj").name
_SHA_COMPLETO = re.compile(r"[0-9a-f]{40}")
#: ``PatcherVersioningEnum`` de upstream: ``StringEnumConverter`` lo escribe como texto y Newtonsoft
#: acepta también el valor numérico.
_VERSIONADOS = {"tag": 0, "branch": 1, "commit": 2}
#: Timeout por subproceso (git/restore/build) de la Fase 1: ``synthesis_build_timeout_seconds`` del ADR §2.
TIMEOUT_FASE1_S = 600.0
#: Tope de salida capturada por subproceso de la Fase 1 (un build verborrágico no llena la memoria).
MAX_SALIDA_FASE1_BYTES = 50_000_000


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
    pins: Mapping[RefRemoto, str]


def _booleano(datos: dict[str, Any], clave: str, *, default: bool) -> bool:
    valor = datos.get(clave, default)
    if type(valor) is not bool:
        raise Fase1Error(f"{clave} debe ser true/false, no {valor!r}")
    return valor


def versionado_efectivo(datos: dict[str, Any]) -> tuple[str, str]:
    """``(tipo, objetivo)`` con el que upstream EJECUTA un ``GithubPatcherSettings``.

    Copia de ``GitPatcherVersioning.Factory`` (0.36.6): ``Tag`` sin ``LatestTag`` y ``Branch`` sin
    ``AutoUpdateToBranchTip`` se degradan a ``Commit`` (``TargetCommit``); el tipo final elige el
    objetivo (``TargetTag``/``TargetBranch``/``TargetCommit``). Defaults de upstream:
    ``PatcherVersioning=Branch``, ``LatestTag=true``, ``AutoUpdateToBranchTip=false``.
    """
    crudo = datos.get("PatcherVersioning", "Branch")
    tipo = next(
        (
            nombre
            for nombre, numero in _VERSIONADOS.items()
            if (isinstance(crudo, str) and crudo.casefold() == nombre) or (type(crudo) is int and crudo == numero)
        ),
        None,
    )
    if tipo is None:
        raise Fase1Error(f"PatcherVersioning desconocido: {crudo!r}")
    if (
        tipo == "tag"
        and not _booleano(datos, "LatestTag", default=True)
        or tipo == "branch"
        and not _booleano(datos, "AutoUpdateToBranchTip", default=False)
    ):
        tipo = "commit"
    campo = {"tag": "TargetTag", "branch": "TargetBranch", "commit": "TargetCommit"}[tipo]
    objetivo = datos.get(campo, "")
    if not isinstance(objetivo, str):
        raise Fase1Error(f"{campo} debe ser texto, no {objetivo!r}")
    return tipo, objetivo.strip()


def ref_dinamico(datos: dict[str, Any]) -> RefRemoto | None:
    """El tag/rama que hay que fijar en el preset, o ``None`` si el patcher ya corre un commit."""
    tipo, objetivo = versionado_efectivo(datos)
    if tipo == "commit":
        return None
    if not objetivo:
        raise Fase1Error(f"el patcher sigue un {tipo} pero no nombra cuál (Target{tipo.title()} vacío)")
    try:
        return ref_remoto(_texto(datos, "RemoteRepoPath"), tipo, objetivo)
    except FuentesGitError as exc:
        raise Fase1Error(str(exc)) from exc


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
    internal, default = await asyncio.to_thread(
        _carpetas_de_datos, raiz / PurePosixPath(proyecto_rel.replace("\\", "/"))
    )
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
    ref = ref_dinamico(datos)
    if ref is None:
        commit = versionado_efectivo(datos)[1]
        if not _SHA_COMPLETO.fullmatch(commit):
            raise Fase1Error(f"el patcher corre su TargetCommit y no es un commit completo de 40 hex: {commit!r}")
    else:
        fijado = ctx.pins.get(ref)
        if fijado is None:
            raise Fase1Error(
                f"sigue el {ref.tipo} {ref.nombre!r} de {ref.repositorio} y no hay un commit fijado: "
                "fijá el SynthesisPreset del perfil (la Fase 1 no consulta la red por su cuenta)"
            )
        if not _SHA_COMPLETO.fullmatch(fijado):
            raise Fase1Error(f"el pin de {ref.tipo} {ref.nombre!r} no es un commit completo: {fijado!r}")
        commit = fijado
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
    pins: Mapping[RefRemoto, str] | None = None,
) -> ResultadoFase1:
    """Deja un shim por patcher seleccionado en *directorio_shims* (nuevo, de esta corrida).

    ``pins`` (PR-3k): commit fijado por el ``SynthesisPreset`` para cada tag/rama dinámico.
    """
    if not directorio_shims.is_absolute() or os.path.lexists(directorio_shims):
        raise Fase1Error(f"El directorio de shims debe ser absoluto y nuevo (no existe): {directorio_shims}")
    try:
        seleccion = await asyncio.to_thread(
            PatcherPipeline.seleccionar_patchers_v2, settings_path, profile_identifier, patcher_ids
        )
    except PatcherPipelineError as exc:
        raise Fase1Error(f"Perfil de Synthesis inválido: {exc}") from exc
    ctx = _Contexto(
        cache_raiz=cache_raiz,
        dotnet=dotnet,
        git=git,
        hitl=hitl,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
        pins=dict(pins or {}),
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
                    # Hashea el árbol del patcher: fuera del loop del servicio.
                    await asyncio.to_thread(
                        construir_config_shim,
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
            ejecutables[patcher.canonical_id] = await asyncio.to_thread(
                materializar_shim, shim.exe.parent, destino, config
            )
        except ShimConfigError as exc:
            raise Fase1Error(f"Patcher {patcher.canonical_id!r}: {exc}") from exc
    logger.info("Fase 1 de Synthesis lista: %d patcher(s) con shim en %s", len(ejecutables), directorio_shims)
    return ResultadoFase1(ejecutables=ejecutables)


#: Prefijo de la carpeta de shims de cada corrida; :meth:`PreparadorFase1.liberar` solo borra estas.
PREFIJO_CORRIDA_DE_SHIMS = "skyclaw-synthesis-shims-"


def _borrar_corrida(corrida: pathlib.Path) -> None:
    """Borra una carpeta de corrida con la primitiva link-aware; un fallo se registra, no se propaga."""
    try:
        rmtree_link_aware(corrida, limpiar_readonly=True)
    except OSError as exc:
        logger.warning("No se pudo borrar la carpeta de shims %s: %s", corrida, exc)


class PreparadorFase1Protocol(Protocol):
    """Lo que el servicio necesita de la Fase 1: el mapa ``ID canónico → shim`` de UNA corrida."""

    async def preparar(
        self, settings_path: pathlib.Path, profile_identifier: str, patcher_ids: list[str]
    ) -> Mapping[str, pathlib.Path]: ...

    async def liberar(self, ejecutables: Mapping[str, pathlib.Path]) -> None:
        """Borra la carpeta de shims de una corrida ya terminada (el servicio decide cuándo, PR-3j)."""
        ...


@dataclass(frozen=True)
class PreparadorFase1:
    """Cableado de :func:`preparar_fase1` para ``SynthesisPipelineService`` (PR-3h).

    Cada corrida estrena una carpeta de shims bajo ``raiz_shims``. Por defecto es el temporal del
    sistema: el worker USVFS confina los shims a sus raíces autorizadas (PR-3g) y la caché de
    ``runtime_state_dir`` no es una de ellas. La caché (``cache_raiz``) sí persiste entre corridas, así
    que una segunda corrida sin cambios no abre red ni pide HITL.
    """

    dotnet: pathlib.Path
    git: pathlib.Path
    cache_raiz: pathlib.Path
    hitl: HITLGuard
    raiz_shims: pathlib.Path | None = None
    timeout_s: float = TIMEOUT_FASE1_S
    max_output_bytes: int = MAX_SALIDA_FASE1_BYTES
    #: PR-3k: commits fijados por el ``SynthesisPreset`` del perfil (``SynthesisPreset.pins``).
    pins: Mapping[RefRemoto, str] | None = None

    async def preparar(
        self, settings_path: pathlib.Path, profile_identifier: str, patcher_ids: list[str]
    ) -> dict[str, pathlib.Path]:
        raiz = self.raiz_shims if self.raiz_shims is not None else pathlib.Path(tempfile.gettempdir())
        try:
            corrida = pathlib.Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=PREFIJO_CORRIDA_DE_SHIMS, dir=raiz))
        except OSError as exc:
            raise Fase1Error(f"No se pudo crear la carpeta de shims de la corrida bajo {raiz}: {exc}") from exc
        try:
            resultado = await preparar_fase1(
                settings_path,
                profile_identifier,
                patcher_ids,
                cache_raiz=self.cache_raiz,
                directorio_shims=corrida / "shims",
                dotnet=self.dotnet,
                git=self.git,
                hitl=self.hitl,
                timeout_s=self.timeout_s,
                max_output_bytes=self.max_output_bytes,
                pins=self.pins,
            )
        except Exception:
            # Nada se despachó: la carpeta (vacía o a medias) no la usa nadie. Una cancelación NO entra
            # acá: un hilo de materialización podría seguir escribiendo en ella.
            await asyncio.to_thread(_borrar_corrida, corrida)
            raise
        if not resultado.ejecutables:
            # Sin shims no hay ruta que lleve a esta carpeta: liberar() no la encontraría nunca.
            await asyncio.to_thread(_borrar_corrida, corrida)
            raise Fase1Error("La Fase 1 no dejó ningún shim para los patchers seleccionados")
        return dict(resultado.ejecutables)

    async def liberar(self, ejecutables: Mapping[str, pathlib.Path]) -> None:
        """Borra la carpeta de la corrida de *ejecutables*; solo una ``skyclaw-synthesis-shims-*`` propia.

        Cada shim vive en ``<corrida>/shims/<NN-id>/``. Una ruta que no tenga esa forma, o cuya corrida
        no cuelgue directamente de ``raiz_shims``, no se toca (se registra y se sigue).
        """
        raiz = (self.raiz_shims if self.raiz_shims is not None else pathlib.Path(tempfile.gettempdir())).resolve()
        corridas: set[pathlib.Path] = set()
        for exe in ejecutables.values():
            corrida = pathlib.Path(exe).parents[2] if len(pathlib.Path(exe).parents) > 2 else None
            if (
                corrida is None
                or pathlib.Path(exe).parent.parent.name != "shims"
                or not corrida.name.startswith(PREFIJO_CORRIDA_DE_SHIMS)
                or corrida.resolve().parent != raiz
            ):
                logger.warning("Shim fuera de una corrida propia; no se borra: %s", exe)
                continue
            corridas.add(corrida)
        for corrida in sorted(corridas):
            await asyncio.to_thread(_borrar_corrida, corrida)

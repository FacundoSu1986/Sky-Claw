"""``SynthesisPreset``: los commits fijados de un perfil de Synthesis (ADR 0019 §2.6 «Modo dual», PR-3k).

Upstream ejecuta un ``GithubPatcherSettings`` dinámico (``Tag`` con ``LatestTag`` o ``Branch`` con
``AutoUpdateToBranchTip``) contra lo que el tag o la rama apunten EN ESE MOMENTO. La Fase 1 no consulta
la red por su cuenta: un preset congela ese "momento" en un commit por ref, con una sola compuerta
HITL, y desde ahí cada corrida es reproducible y sin red (las fuentes y el build salen de caché).

- :func:`fijar_preset`: lee el perfil con la MISMA selección que la Fase 1, junta los tags/ramas
  dinámicos (:func:`synthesis_fase1.ref_dinamico`) y los resuelve con ``git ls-remote``
  (:func:`synthesis_fuentes_git.resolver_refs_remotos`).
- :func:`guardar_preset` / :func:`cargar_preset`: JSON de esquema cerrado, escritura atómica.
- :attr:`SynthesisPreset.pins` es lo que recibe :class:`synthesis_fase1.PreparadorFase1`.

Volver a fijar el preset es la forma explícita de "actualizar" un patcher dinámico; nada lo hace solo.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sky_claw.local.tools.patcher_pipeline import PatcherPipeline, PatcherPipelineError
from sky_claw.local.tools.synthesis_fase1 import Fase1Error, ref_dinamico
from sky_claw.local.tools.synthesis_fuentes_git import (
    FuentesGitError,
    RefRemoto,
    ref_remoto,
    resolver_refs_remotos,
)

if TYPE_CHECKING:
    from sky_claw.app.security.hitl import HITLGuard

__all__ = [
    "CLAVES_DE_PIN",
    "CLAVES_DE_PRESET",
    "PresetError",
    "SynthesisPreset",
    "cargar_preset",
    "fijar_preset",
    "guardar_preset",
    "refs_dinamicos",
]

ESQUEMA_PRESET = 1
#: Esquema cerrado (igualdad anclada en los tests): una clave de más o de menos falla cerrado.
CLAVES_DE_PRESET = frozenset({"esquema", "perfil", "pins"})
CLAVES_DE_PIN = frozenset({"repositorio", "tipo", "nombre", "commit"})
_SHA_COMPLETO = re.compile(r"[0-9a-f]{40}")


class PresetError(Exception):
    """El preset no se pudo fijar, leer o escribir (fail-closed)."""


@dataclass(frozen=True)
class SynthesisPreset:
    """Commits fijados para los tags/ramas dinámicos de un perfil (``perfil`` = nickname o ID)."""

    perfil: str
    pins: dict[RefRemoto, str]


def refs_dinamicos(settings_path: pathlib.Path, perfil: str, patcher_ids: list[str] | None) -> list[RefRemoto]:
    """Los tags/ramas que la Fase 1 va a necesitar fijados para esta selección (sin red)."""
    try:
        seleccion = PatcherPipeline.seleccionar_patchers_v2(settings_path, perfil, patcher_ids)
    except PatcherPipelineError as exc:
        raise PresetError(f"Perfil de Synthesis inválido: {exc}") from exc
    refs: set[RefRemoto] = set()
    for patcher in seleccion:
        if patcher.tipo != "git":
            continue
        try:
            ref = ref_dinamico(patcher.datos)
        except Fase1Error as exc:
            raise PresetError(f"Patcher {patcher.canonical_id!r}: {exc}") from exc
        if ref is not None:
            refs.add(ref)
    return sorted(refs)


async def fijar_preset(
    settings_path: pathlib.Path,
    perfil: str,
    patcher_ids: list[str] | None,
    *,
    git: pathlib.Path,
    hitl: HITLGuard | None,
    trabajo_raiz: pathlib.Path,
    timeout_s: float,
    max_output_bytes: int,
) -> SynthesisPreset:
    """Resuelve a commits los tags/ramas dinámicos del perfil (una compuerta HITL para todos)."""
    refs = await asyncio.to_thread(refs_dinamicos, settings_path, perfil, patcher_ids)
    try:
        pins = await resolver_refs_remotos(
            refs, trabajo_raiz=trabajo_raiz, git=git, hitl=hitl, timeout_s=timeout_s, max_output_bytes=max_output_bytes
        )
    except FuentesGitError as exc:
        raise PresetError(f"No se pudo fijar el preset de {perfil!r}: {exc}") from exc
    return SynthesisPreset(perfil=perfil, pins=pins)


def _a_json(preset: SynthesisPreset) -> dict[str, Any]:
    return {
        "esquema": ESQUEMA_PRESET,
        "perfil": preset.perfil,
        "pins": [
            {"repositorio": r.repositorio, "tipo": r.tipo, "nombre": r.nombre, "commit": sha}
            for r, sha in sorted(preset.pins.items())
        ],
    }


def guardar_preset(destino: pathlib.Path, preset: SynthesisPreset) -> None:
    """Escribe el preset de forma atómica (temporal en el mismo directorio + ``os.replace``)."""
    datos = json.dumps(_a_json(preset), indent=2, ensure_ascii=False)
    destino.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=f".{destino.name}.", suffix=".tmp", dir=destino.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            archivo.write(datos)
        os.replace(temporal, destino)
    except BaseException:
        pathlib.Path(temporal).unlink(missing_ok=True)
        raise


def cargar_preset(origen: pathlib.Path) -> SynthesisPreset:
    """Lee un preset validando su esquema cerrado; cualquier desvío es :class:`PresetError`."""
    try:
        datos = json.loads(origen.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PresetError(f"No se pudo leer el preset {origen}: {exc}") from exc
    if not isinstance(datos, dict) or set(datos) != CLAVES_DE_PRESET or datos["esquema"] != ESQUEMA_PRESET:
        raise PresetError(f"Preset con esquema inválido: {origen}")
    perfil, crudos = datos["perfil"], datos["pins"]
    if not isinstance(perfil, str) or not perfil.strip() or not isinstance(crudos, list):
        raise PresetError(f"Preset con perfil o pins inválidos: {origen}")
    pins: dict[RefRemoto, str] = {}
    for crudo in crudos:
        if (
            not isinstance(crudo, dict)
            or set(crudo) != CLAVES_DE_PIN
            or not all(isinstance(v, str) for v in crudo.values())
        ):
            raise PresetError(f"Pin con esquema inválido en {origen}: {crudo!r}")
        try:
            ref = ref_remoto(f"https://github.com/{crudo['repositorio']}", crudo["tipo"], crudo["nombre"])
        except FuentesGitError as exc:
            raise PresetError(f"Pin inválido en {origen}: {exc}") from exc
        if ref.repositorio != crudo["repositorio"] or not _SHA_COMPLETO.fullmatch(crudo["commit"]):
            raise PresetError(f"Pin inválido en {origen}: {crudo!r}")
        if ref in pins:
            raise PresetError(f"Pin duplicado en {origen}: {ref}")
        pins[ref] = crudo["commit"]
    return SynthesisPreset(perfil=perfil, pins=pins)

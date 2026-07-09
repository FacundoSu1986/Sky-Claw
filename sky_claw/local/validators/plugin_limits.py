"""Sensor de límites de plugins full/light para el preflight (T-30·2, Oleada 7).

Skyrim SE/AE tiene DOS pools de plugins independientes:

* **full** (``.esp``/``.esm`` no ligeros): máximo **254** (los índices 0x00–0xFD;
  0xFE queda reservado para el pool ligero).
* **light** (FE: ``.esl`` + ``.esp``/``.esm`` con el flag light): máximo **4096**.

Exceder cualquiera de los dos impide arrancar. Este sensor los cuenta con los
**flags reales** del header TES4 (vía :func:`read_plugin_header`), no por
extensión: un ``.esp`` con flag ESL (ESPFE) consume slot *light*, no *full* —
exactamente el caso que la heurística por extensión de
``conflict_analyzer.validate_load_order_limit`` cuenta mal. Corre en el
preflight, antes de cualquier herramienta.

:func:`limits_preflight_check` compone el resultado en un :class:`PreflightCheck`
para el semáforo; el cableado al ``PreflightService`` es un parámetro inyectable
(mismo patrón que el sensor de masters).
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sky_claw.local.validators.plugin_header import PluginHeaderError, read_plugin_header
from sky_claw.local.validators.preflight import PreflightCheck, PreflightStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Límites de cada pool (Skyrim SE/AE). 254 = 0x00–0xFD; 0xFE = pool ligero.
FULL_PLUGIN_LIMIT = 254
LIGHT_PLUGIN_LIMIT = 4096

#: Umbral de advertencia "acercándose" al límite full (margen de 4 slots).
_FULL_NEAR_THRESHOLD = FULL_PLUGIN_LIMIT - 4

#: Extensiones de plugin que el juego carga.
_PLUGIN_SUFFIXES: frozenset[str] = frozenset({".esp", ".esm", ".esl"})

Severity = Literal["critical", "warning"]
LimitKind = Literal["full_exceeded", "light_exceeded", "full_near", "unreadable"]


@dataclass(frozen=True, slots=True)
class LimitIssue:
    """Un problema de límites de pool, explicable al usuario."""

    kind: LimitKind
    severity: Severity
    detail: str


@dataclass(frozen=True, slots=True)
class LoadOrderLimits:
    """Conteo de ambos pools + issues detectados.

    Attributes:
        full_count: Plugins en el pool full (no ligeros).
        light_count: Plugins en el pool light (FE).
        unreadable: Cuántos headers no se pudieron leer (contados por extensión).
        issues: Problemas detectados (vacío si todo dentro de límites).
    """

    full_count: int
    light_count: int
    unreadable: int
    issues: tuple[LimitIssue, ...]


class PluginLimitsChecker:
    """Cuenta los pools full/light del load order habilitado con flags reales.

    Args:
        plugin_dirs: Directorios donde viven los plugins (``Data`` y/o carpetas
            de mods de MO2). No se recorre recursivo.
    """

    def __init__(self, *, plugin_dirs: Sequence[pathlib.Path]) -> None:
        self._plugin_dirs = tuple(plugin_dirs)

    def check(self, enabled_plugins: Sequence[str]) -> LoadOrderLimits:
        """Clasifica cada plugin habilitado y agrega los conteos + issues."""
        available = self._index_available()
        full = 0
        light = 0
        unreadable = 0
        seen: set[str] = set()

        for plugin_name in enabled_plugins:
            key = plugin_name.casefold()
            # Un plugin no puede ocupar dos slots: un load order con nombres
            # repetidos (stale/malformado) no debe inflar el conteo (review
            # Copilot PR #250).
            if key in seen:
                continue
            seen.add(key)
            path = available.get(key)
            if path is None:
                # Ausente en disco: no consume slot (el sensor de masters ya lo
                # reporta como plugin_not_found).
                continue
            is_light = self._is_light(plugin_name, path)
            if is_light is None:
                unreadable += 1
                is_light = plugin_name.lower().endswith(".esl")  # fallback por extensión
            if is_light:
                light += 1
            else:
                full += 1

        issues = self._build_issues(full, light, unreadable)
        if issues:
            logger.warning(
                "Plugin limits: full=%d/%d light=%d/%d unreadable=%d",
                full,
                FULL_PLUGIN_LIMIT,
                light,
                LIGHT_PLUGIN_LIMIT,
                unreadable,
            )
        return LoadOrderLimits(full_count=full, light_count=light, unreadable=unreadable, issues=issues)

    @staticmethod
    def _is_light(plugin_name: str, path: pathlib.Path) -> bool | None:
        """True/False si se pudo determinar; None si el header es ilegible.

        Un plugin es ligero si su extensión es ``.esl`` **o** su header trae el
        flag light (ESPFE). El header se lee SIEMPRE — también para ``.esl`` —
        para no saltear la validación: un ``.esl`` corrupto debe reportarse
        ilegible como cualquier otra extensión (review Codex PR #250). La
        extensión ``.esl`` es ligera aun sin el flag, de ahí el ``or``.
        """
        ext_light = plugin_name.lower().endswith(".esl")
        try:
            header = read_plugin_header(path)
        except PluginHeaderError as exc:
            logger.debug("Header ilegible en %s: %s", plugin_name, exc)
            return None
        return ext_light or header.is_light

    def _build_issues(self, full: int, light: int, unreadable: int) -> tuple[LimitIssue, ...]:
        issues: list[LimitIssue] = []
        if full > FULL_PLUGIN_LIMIT:
            issues.append(
                LimitIssue(
                    kind="full_exceeded",
                    severity="critical",
                    detail=(
                        f"Pool full excedido: {full}/{FULL_PLUGIN_LIMIT}. El juego no arranca. "
                        "Convertí mods chicos (<2048 records nuevos) a ESL en xEdit o desactivá plugins."
                    ),
                )
            )
        elif full >= _FULL_NEAR_THRESHOLD:
            issues.append(
                LimitIssue(
                    kind="full_near",
                    severity="warning",
                    detail=f"Pool full cerca del límite: {full}/{FULL_PLUGIN_LIMIT}. Considerá ESL-ificar mods chicos.",
                )
            )
        if light > LIGHT_PLUGIN_LIMIT:
            issues.append(
                LimitIssue(
                    kind="light_exceeded",
                    severity="critical",
                    detail=f"Pool light excedido: {light}/{LIGHT_PLUGIN_LIMIT}. Desactivá plugins ligeros.",
                )
            )
        if unreadable:
            issues.append(
                LimitIssue(
                    kind="unreadable",
                    severity="warning",
                    detail=(
                        f"{unreadable} header(s) ilegible(s): el conteo se estimó por extensión y "
                        "puede ser aproximado (un .esp con flag ESL contaría distinto)."
                    ),
                )
            )
        return tuple(issues)

    def _index_available(self) -> dict[str, pathlib.Path]:
        """Nombre casefold → ruta del plugin (primer directorio gana)."""
        available: dict[str, pathlib.Path] = {}
        for directory in self._plugin_dirs:
            try:
                if not directory.is_dir():
                    continue
                entries = sorted(directory.iterdir())
            except OSError as exc:
                logger.debug("No se pudo inspeccionar %s: %s", directory, exc)
                continue
            for entry in entries:
                try:
                    if entry.is_file() and entry.suffix.lower() in _PLUGIN_SUFFIXES:
                        available.setdefault(entry.name.casefold(), entry)
                except OSError as exc:
                    logger.debug("No se pudo inspeccionar %s: %s", entry, exc)
        return available


def limits_preflight_check(limits: LoadOrderLimits) -> PreflightCheck:
    """Compone los conteos/issues en un :class:`PreflightCheck` para el semáforo.

    Rojo si algún pool excede su límite; amarillo si hay warnings (cerca del
    límite / headers ilegibles); verde reporta los conteos.
    """
    summary = f"{limits.full_count}/{FULL_PLUGIN_LIMIT} full, {limits.light_count}/{LIGHT_PLUGIN_LIMIT} light."
    if not limits.issues:
        return PreflightCheck(name="plugin_limits", status=PreflightStatus.GREEN, summary=summary)
    status = (
        PreflightStatus.RED if any(issue.severity == "critical" for issue in limits.issues) else PreflightStatus.YELLOW
    )
    return PreflightCheck(
        name="plugin_limits",
        status=status,
        summary=summary,
        details=tuple(f"[{i.kind}] {i.detail}" for i in limits.issues),
    )

"""Builders compartidos de sensores de preflight (T-16d).

Extrae la costura de sensores que ``loot_service``, ``xedit_service`` y
``synthesis_service`` duplicaban en sus ``_ensure_preflight``:

* ``build_vfs_sensor`` — construye el :class:`VfsHealthChecker` sobre las rutas
  CRUDAS con el guard de "al menos una raíz", coaccionando no-``Path`` a ``None``.
* ``build_modlist_sensors`` — arma los closures de masters/límites con gate de
  honestidad y re-resolución por llamada (freshness, review Codex #252).
* ``build_overwrite_sensor`` — arma el closure del sensor de overwrite sucio.

Los sensores que difieren por ritual NO se extraen: los **permisos de
escritura** se prueban sobre rutas distintas según lo que cada Ritual reescribe
(LOOT → dirs del load order; xEdit → ``Data`` + masters oficiales; Synthesis →
el output), así que cada servicio arma su propio closure.

Anti-ciclo: este módulo alcanza ``tools._process`` a través de los checkers →
``validators.preflight`` → ``loot.version``. Debe importarse **de forma
perezosa** desde ``sky_claw/local/tools/`` (dentro de métodos), igual que el
resto de los imports de preflight en los servicios; por eso los checkers se
importan perezosamente dentro de cada builder.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from sky_claw.local.mo2.plugin_sources import PluginSources
    from sky_claw.local.validators.missing_masters import MasterIssue
    from sky_claw.local.validators.overwrite_health import OverwriteScan
    from sky_claw.local.validators.plugin_limits import LoadOrderLimits
    from sky_claw.local.validators.preflight import (
        LimitsCheck,
        MastersCheck,
        OverwriteCheck,
    )
    from sky_claw.local.validators.vfs_health import VfsHealthChecker


def build_vfs_sensor(
    *,
    raw_game: pathlib.Path | None,
    raw_mo2: pathlib.Path | None,
    scan_mods_dir: bool,
) -> VfsHealthChecker | None:
    """Construye el ``VfsHealthChecker`` sobre rutas CRUDAS.

    Las rutas resueltas ya siguieron los symlinks/junctions que este sensor
    debe inspeccionar, así que el caller pasa las crudas. Coacciona a ``None``
    cualquier valor que no sea ``pathlib.Path`` (defiende de ``path_resolver``
    mockeados que devuelven no-``Path``). Sin ninguna raíz utilizable → ``None``.
    """
    game = raw_game if isinstance(raw_game, pathlib.Path) else None
    mo2 = raw_mo2 if isinstance(raw_mo2, pathlib.Path) else None
    if game is None and mo2 is None:
        return None
    from sky_claw.local.validators.vfs_health import VfsHealthChecker

    return VfsHealthChecker(game_path=game, mo2_root=mo2, scan_mods_dir=scan_mods_dir)


def build_modlist_sensors(
    sources_resolver: Callable[[], PluginSources],
) -> tuple[MastersCheck | None, LimitsCheck | None]:
    """Closures de los sensores de masters/límites (T-30w, extraído en T-16d).

    Gate de honestidad al construir: solo cablea si HOY hay fuentes utilizables;
    si no, ``(None, None)`` → el semáforo reporta "no configurado" en vez de
    mentir verde (lección #250). Los closures re-resuelven ``sources_resolver()``
    en cada llamada (freshness, review Codex #252): si el usuario instala/activa
    plugins entre corridas, la siguiente los ve.
    """
    from sky_claw.local.validators.missing_masters import MissingMastersChecker
    from sky_claw.local.validators.plugin_limits import PluginLimitsChecker

    initial = sources_resolver()
    if not initial.plugin_dirs or not initial.enabled_plugins:
        return None, None

    def _masters() -> list[MasterIssue]:
        sources = sources_resolver()
        return MissingMastersChecker(plugin_dirs=sources.plugin_dirs).check(sources.enabled_plugins)

    def _limits() -> LoadOrderLimits:
        sources = sources_resolver()
        return PluginLimitsChecker(plugin_dirs=sources.plugin_dirs).check(sources.enabled_plugins)

    return _masters, _limits


def build_mo2_profile_sources_resolver(
    *,
    game: pathlib.Path,
    mo2: pathlib.Path,
    profile: str | None,
) -> Callable[[], PluginSources] | None:
    """Resolver de fuentes de plugins desde el **perfil MO2 activo** (T-16c·2/3).

    Lee el load order de ``profiles/<perfil>/plugins.txt`` (activos con ``*``) o,
    en su defecto, ``loadorder.txt`` — NO el ``%LOCALAPPDATA%`` global que
    reescribe LOOT fuera del VFS. Los rituales que procesan TODO el modlist
    (Synthesis, DynDOLOD) deben validar el modlist REAL que corre MO2, no un load
    order global/stale que ``LoadOrderFileResolver`` prioriza en su unión (review
    Codex #306). Valida el nombre del perfil contra path traversal
    (``assert_safe_component``). Devuelve ``None`` si el perfil no es resoluble o
    no hay archivo de load order → el caller reporta "no configurado", no miente
    verde (lección #250). El feed de ``build_modlist_sensors``.
    """
    if not isinstance(profile, str):
        return None
    from sky_claw.antigravity.security.path_validator import PathViolationError, assert_safe_component
    from sky_claw.local.mo2.plugin_sources import resolve_plugin_sources

    try:
        assert_safe_component(profile, field="profile")  # guard de path-traversal (patrón T-05)
    except PathViolationError:
        return None
    profile_dir = mo2 / "profiles" / profile
    load_order_file = next(
        (profile_dir / name for name in ("plugins.txt", "loadorder.txt") if (profile_dir / name).is_file()),
        None,
    )
    if load_order_file is None:
        return None
    game_data_dir = game / "Data"
    mo2_mods_dir = mo2 / "mods"
    mo2_overwrite_dir = mo2 / "overwrite"

    def _resolve() -> PluginSources:
        return resolve_plugin_sources(
            game_data_dir=game_data_dir,
            mo2_mods_dir=mo2_mods_dir,
            mo2_overwrite_dir=mo2_overwrite_dir,
            load_order_file=load_order_file,
        )

    return _resolve


def build_overwrite_sensor(overwrite_dir: pathlib.Path | None) -> OverwriteCheck | None:
    """Closure del sensor de overwrite sucio (T-30·3, extraído en T-16d).

    ``overwrite_dir`` es ``<mo2>/overwrite`` (fuera del árbol del perfil). Sin un
    dir ``pathlib.Path`` → ``None`` → "no configurado". El closure re-escanea en
    cada run (freshness, patrón #252): el ``PreflightService`` se cachea, así que
    la salida de una herramienta corrida entre preflight y preflight debe verse.
    """
    if not isinstance(overwrite_dir, pathlib.Path):
        return None
    from sky_claw.local.validators.overwrite_health import OverwriteHealthChecker

    resolved = overwrite_dir

    def _overwrite() -> OverwriteScan:
        return OverwriteHealthChecker(overwrite_dir=resolved).check()

    return _overwrite

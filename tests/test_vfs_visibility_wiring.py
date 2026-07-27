"""Cableado del sensor de visibilidad de mods en los servicios (U-01).

**Test-ancla de una CLASE de defecto, no de un caso.** El defecto #1 del repo es
el fix que aterriza en un camino y deja intacto al gemelo (13 de 21 follow-ups
auditados). U-01 es exactamente esa forma: el falso verde del run standalone no
vive en un servicio, vive en TODOS los que construyen un preflight. Por eso acá
se **enumera la familia** (patrón ``RITUAL_TOOL_MAP`` de
``tests/test_ritual_dispatch.py``) y un guard de completitud falla si aparece un
servicio nuevo que nadie clasificó — en vez de seis tests escritos a mano que
callan sobre el séptimo.

Ver ``sky_claw/local/validators/vfs_visibility`` para el porqué del sensor.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from sky_claw.local.validators.preflight import PreflightStatus

_TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent / "sky_claw" / "local" / "tools"

#: Servicios cuyo preflight DEBE cablear el sensor de visibilidad (U-01).
SERVICIOS_CON_VISIBILIDAD: frozenset[str] = frozenset(
    {
        "loot_service",
        "dyndolod_service",
        "synthesis_service",
        "pandora_service",
        "wrye_bash_service",
    }
)

#: Excluidos **con motivo explícito** (la regla de "Hermanos" del template pide
#: nombrarlos, no darlos por descartados de memoria).
SERVICIOS_SIN_VISIBILIDAD: dict[str, str] = {
    "xedit_service": (
        "Su preflight gatea SOLO quick_auto_clean, que limpia los DLC oficiales "
        "(Update/Dawnguard/HearthFires/Dragonborn) que viven en el Data del juego "
        "con o sin VFS. Un ROJO por visibilidad ahí bloquearía una limpieza que "
        "funciona perfecto en standalone: falso positivo, justo lo que el sensor "
        "está diseñado para NO hacer. execute_patch no usa preflight."
    ),
}


def _modulos_con_preflight_vfs() -> set[str]:
    """Servicios que construyen un sensor de VFS — la familia a clasificar."""
    return {
        ruta.stem for ruta in _TOOLS_DIR.glob("*_service.py") if "build_vfs_sensor(" in ruta.read_text(encoding="utf-8")
    }


def test_la_enumeracion_cubre_toda_la_familia() -> None:
    """Guard de completitud: un servicio nuevo con preflight VFS obliga a
    decidir si lleva el sensor, en vez de heredar el falso verde en silencio."""
    clasificados = SERVICIOS_CON_VISIBILIDAD | set(SERVICIOS_SIN_VISIBILIDAD)

    assert _modulos_con_preflight_vfs() == clasificados


def test_ningun_servicio_hardcodea_scan_mods_dir_false() -> None:
    """``scan_mods_dir`` se deriva de si la raíz MO2 está validada (patrón de
    ``loot_service``), nunca se fija en False: hardcodearlo dejaba el scan de
    symlinks de ``mods/`` ciego incluso con una instancia MO2 legítima."""
    culpables = [
        ruta.name
        for ruta in _TOOLS_DIR.glob("*_service.py")
        if re.search(r"scan_mods_dir\s*=\s*False", ruta.read_text(encoding="utf-8"))
    ]

    assert culpables == []


# ---------------------------------------------------------------------------
# Comportamiento: el escenario real de U-01, servicio por servicio
# ---------------------------------------------------------------------------


def _instancia(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Juego con Data vainilla + MO2 cuyo perfil activa un mod NO materializado.

    Es el escenario exacto de U-01: MO2 en USVFS estándar, Sky-Claw corriendo
    standalone, así que ``MiMod.esp`` existe bajo ``mods/`` pero el tool leería
    un ``Data`` sin él.
    """
    data = tmp_path / "Skyrim" / "Data"
    data.mkdir(parents=True)
    (data / "Skyrim.esm").write_bytes(b"TES4")

    mo2 = tmp_path / "MO2"
    (mo2 / "mods" / "MiMod").mkdir(parents=True)
    (mo2 / "mods" / "MiMod" / "MiMod.esp").write_bytes(b"TES4")
    (mo2 / "overwrite").mkdir(parents=True)
    perfil = mo2 / "profiles" / "Default"
    perfil.mkdir(parents=True)
    (perfil / "plugins.txt").write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n*MiMod.esp\r\n")
    return tmp_path / "Skyrim", mo2


def _resolver(*, skyrim: pathlib.Path, mo2: pathlib.Path) -> MagicMock:
    """Resolver con rutas crudas y validadas (mismo shape que test_preflight_wiring)."""
    resolver = MagicMock()
    resolver.get_skyrim_path_raw = MagicMock(return_value=skyrim)
    resolver.get_skyrim_path = MagicMock(return_value=skyrim)
    resolver.get_mo2_path_raw = MagicMock(return_value=mo2)
    resolver.get_mo2_path = MagicMock(return_value=mo2)
    resolver.detect_mo2_path = MagicMock(return_value=mo2)
    resolver.get_active_profile = MagicMock(return_value="Default")
    resolver.get_loot_exe = MagicMock(return_value=None)
    resolver.get_pandora_exe = MagicMock(return_value=None)
    return resolver


def _construir(nombre: str, *, resolver: MagicMock, mo2: pathlib.Path) -> Any:
    """Instancia el servicio *nombre* con colaboradores mockeados."""
    comunes: dict[str, Any] = {
        "lock_manager": MagicMock(),
        "snapshot_manager": MagicMock(),
        "path_resolver": resolver,
    }
    if nombre == "loot_service":
        from sky_claw.local.mo2.load_order import LoadOrderPaths
        from sky_claw.local.tools.loot_service import LootSortingService

        # LOOT toma los habilitados de su resolver de load order (prefiere
        # plugins.txt sobre loadorder.txt); apuntarlo al del perfil real.
        load_order = MagicMock()
        load_order.resolve.return_value = LoadOrderPaths(
            files=(mo2 / "profiles" / "Default" / "plugins.txt",), sources=("mo2_profile",)
        )
        return LootSortingService(**comunes, loot_runner=MagicMock(), load_order_resolver=load_order)
    if nombre == "dyndolod_service":
        from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

        return DynDOLODPipelineService(**comunes, journal=MagicMock(), event_bus=MagicMock())
    if nombre == "synthesis_service":
        from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

        return SynthesisPipelineService(**comunes, journal=MagicMock(), event_bus=MagicMock())
    if nombre == "pandora_service":
        from sky_claw.local.tools.pandora_service import PandoraPipelineService

        return PandoraPipelineService(**comunes)
    if nombre == "wrye_bash_service":
        from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

        return WryeBashPipelineService(**comunes)
    raise AssertionError(f"servicio no contemplado en el factory: {nombre}")


@pytest.mark.parametrize("servicio", sorted(SERVICIOS_CON_VISIBILIDAD))
async def test_el_modlist_invisible_pone_el_preflight_rojo(servicio: str, tmp_path: pathlib.Path) -> None:
    """El falso verde de U-01, cerrado en CADA servicio de la familia: el perfil
    activa un mod que el Data del juego no tiene → el Ritual no corre."""
    skyrim, mo2 = _instancia(tmp_path)
    svc = _construir(servicio, resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2)

    preflight = svc._ensure_preflight()
    assert preflight is not None, f"{servicio} no construyó preflight con game+MO2 resolubles"
    reporte = await preflight.run()

    visibilidad = next((c for c in reporte.checks if c.name == "vfs_visibility"), None)
    assert visibilidad is not None, f"{servicio} no cableó el sensor de visibilidad"
    assert visibilidad.status is PreflightStatus.RED
    assert any("MiMod.esp" in d for d in visibilidad.details)
    assert reporte.blocks_mutations is True


@pytest.mark.parametrize("servicio", sorted(SERVICIOS_CON_VISIBILIDAD))
async def test_el_modlist_visible_no_bloquea(servicio: str, tmp_path: pathlib.Path) -> None:
    """Contracara imprescindible: con los mods materializados el gate no
    estorba. Sin este test, 'siempre rojo' pasaría el test de arriba."""
    skyrim, mo2 = _instancia(tmp_path)
    (skyrim / "Data" / "MiMod.esp").write_bytes(b"TES4")  # materializado
    svc = _construir(servicio, resolver=_resolver(skyrim=skyrim, mo2=mo2), mo2=mo2)

    reporte = await svc._ensure_preflight().run()

    visibilidad = next(c for c in reporte.checks if c.name == "vfs_visibility")
    assert visibilidad.status is PreflightStatus.GREEN
    # Un verde "no configurado" pasaría este test sin que el sensor exista:
    # exigir que el verde venga de una MEDICIÓN real (los servicios con
    # omit_unconfigured=False emiten ese checkpoint igual).
    assert "no configurado" not in visibilidad.summary.lower()

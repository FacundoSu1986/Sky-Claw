"""Guard de etapas previas de la etapa 9 (SOP §1 / §5 regla 2, #661).

La propiedad: *ningún camino nuevo de LODs arranca sin probar los artefactos de
las etapas 6 (Bashed Patch), 7 (Synthesis) y 8 (cache de grass) en el perfil que
la etapa 9 va a leer*, y "no lo pude leer" nunca es "está".

Los casos se enumeran por etapa y por fuente del artefacto (overwrite / mod
habilitado / mod deshabilitado), con el perfil MO2 montado en ``tmp_path``.
"""

from __future__ import annotations

import pathlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.validators.etapas_previas_lods import (
    PLUGIN_DE_SYNTHESIS,
    PLUGIN_DE_WRYE_BASH,
    verificar_etapas_previas_de_lods,
)


def _perfil(
    raiz: pathlib.Path,
    *,
    plugins: list[str],
    modlist: list[str] | None = None,
    grass_en_overwrite: bool = False,
    grass_en_mods: tuple[str, ...] = (),
) -> dict[str, pathlib.Path]:
    perfil = raiz / "profiles" / "Default"
    perfil.mkdir(parents=True)
    (perfil / "plugins.txt").write_text("\n".join(plugins) + "\n", encoding="utf-8")
    (perfil / "modlist.txt").write_text("\n".join(modlist or []) + "\n", encoding="utf-8")
    overwrite = raiz / "overwrite"
    overwrite.mkdir()
    if grass_en_overwrite:
        (overwrite / "Grass").mkdir()
        (overwrite / "Grass" / "Tamriel.cgid").write_bytes(b"cgid")
    mods = raiz / "mods"
    mods.mkdir()
    for mod in grass_en_mods:
        (mods / mod / "Grass").mkdir(parents=True)
        (mods / mod / "Grass" / "Tamriel.cgid").write_bytes(b"cgid")
    return {
        "plugins_txt": perfil / "plugins.txt",
        "modlist_txt": perfil / "modlist.txt",
        "mods_dir": mods,
        "overwrite_dir": overwrite,
    }


_COMPLETO = [f"*{PLUGIN_DE_WRYE_BASH}", f"*{PLUGIN_DE_SYNTHESIS}"]


def test_las_tres_etapas_presentes_son_completas(tmp_path):
    rutas = _perfil(tmp_path, plugins=_COMPLETO, grass_en_overwrite=True)
    evidencia = verificar_etapas_previas_de_lods(**rutas)
    assert evidencia.completas()
    assert evidencia.faltantes == ()


@pytest.mark.parametrize(
    ("plugins", "etapa"),
    [
        ([f"*{PLUGIN_DE_SYNTHESIS}"], "etapa 6"),
        ([f"{PLUGIN_DE_WRYE_BASH}", f"*{PLUGIN_DE_SYNTHESIS}"], "etapa 6"),  # presente pero deshabilitado
        ([f"*{PLUGIN_DE_WRYE_BASH}"], "etapa 7"),
        ([f"*{PLUGIN_DE_WRYE_BASH}", f"{PLUGIN_DE_SYNTHESIS}"], "etapa 7"),
    ],
)
def test_cada_plugin_faltante_o_deshabilitado_se_nombra(tmp_path, plugins, etapa):
    rutas = _perfil(tmp_path, plugins=plugins, grass_en_overwrite=True)
    evidencia = verificar_etapas_previas_de_lods(**rutas)
    assert not evidencia.completas()
    assert len(evidencia.faltantes) == 1
    assert evidencia.faltantes[0].startswith(etapa)


def test_los_nombres_de_plugin_se_comparan_sin_mayusculas(tmp_path):
    rutas = _perfil(tmp_path, plugins=["*bashed patch, 0.ESP", "*SYNTHESIS.esp"], grass_en_overwrite=True)
    assert verificar_etapas_previas_de_lods(**rutas).completas()


def test_el_grass_de_un_mod_habilitado_cuenta(tmp_path):
    rutas = _perfil(tmp_path, plugins=_COMPLETO, modlist=["+Grass Cache"], grass_en_mods=("Grass Cache",))
    assert verificar_etapas_previas_de_lods(**rutas).completas()


def test_el_grass_de_un_mod_deshabilitado_no_cuenta(tmp_path):
    rutas = _perfil(tmp_path, plugins=_COMPLETO, modlist=["-Grass Cache"], grass_en_mods=("Grass Cache",))
    evidencia = verificar_etapas_previas_de_lods(**rutas)
    assert len(evidencia.faltantes) == 1
    assert evidencia.faltantes[0].startswith("etapa 8")


def test_sin_cache_de_grass_falta_la_etapa_8(tmp_path):
    rutas = _perfil(tmp_path, plugins=_COMPLETO)
    evidencia = verificar_etapas_previas_de_lods(**rutas)
    assert [f.split(" ")[1] for f in evidencia.faltantes] == ["8"]


def test_un_modlist_malformado_no_inventa_grass(tmp_path):
    """Una línea inválida en ``modlist.txt`` hace fallar cerrado a ``read_enabled_mods``:
    la etapa 8 sólo puede probarse por el overwrite."""
    rutas = _perfil(tmp_path, plugins=_COMPLETO, modlist=["Grass Cache"], grass_en_mods=("Grass Cache",))
    evidencia = verificar_etapas_previas_de_lods(**rutas)
    assert [f.split(" ")[1] for f in evidencia.faltantes] == ["8"]


def test_sin_perfil_no_hay_forma_de_probar_las_etapas_6_y_7(tmp_path):
    rutas = _perfil(tmp_path, plugins=_COMPLETO, grass_en_overwrite=True)
    evidencia = verificar_etapas_previas_de_lods(**{**rutas, "plugins_txt": None})
    assert len(evidencia.faltantes) == 1
    assert "plugins.txt" in evidencia.faltantes[0]


def test_un_plugins_txt_ilegible_es_faltante_no_vacio(tmp_path):
    rutas = _perfil(tmp_path, plugins=_COMPLETO, grass_en_overwrite=True)
    rutas["plugins_txt"].write_bytes(b"*Bashed Patch, 0.esp\n\xff\xfe*Synthesis.esp\n")
    evidencia = verificar_etapas_previas_de_lods(**rutas)
    assert len(evidencia.faltantes) == 1
    assert "plugins.txt" in evidencia.faltantes[0]


def test_nada_resoluble_son_todas_faltantes():
    evidencia = verificar_etapas_previas_de_lods(plugins_txt=None, modlist_txt=None, mods_dir=None, overwrite_dir=None)
    assert len(evidencia.faltantes) == 2  # load order ilegible + etapa 8


# =============================================================================
# El servicio lee el MISMO perfil que alimenta el -p:
# =============================================================================


def _servicio(resolver: MagicMock):
    from sky_claw.app.core.event_bus import CoreEventBus
    from sky_claw.app.db.locks import DistributedLockManager, LockInfo
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

    locks = AsyncMock(spec=DistributedLockManager)
    info = LockInfo(
        resource_id="dyndolod-pipeline",
        agent_id="dyndolod-pipeline-service",
        acquired_at=time.time(),
        expires_at=time.time() + 3600.0,
    )
    locks.acquire_lock = AsyncMock(return_value=info)
    return DynDOLODPipelineService(
        lock_manager=locks,
        snapshot_manager=AsyncMock(spec=FileSnapshotManager),
        journal=AsyncMock(),
        path_resolver=resolver,
        event_bus=AsyncMock(spec=CoreEventBus),
        mo2_profile="Default",
    )


def _resolver(raiz: pathlib.Path) -> MagicMock:
    resolver = MagicMock()
    resolver.get_mo2_instance_data_root_estricto = MagicMock(return_value=raiz)
    resolver.get_active_profile = MagicMock(return_value="Default")
    resolver.get_mo2_mods_path = MagicMock(return_value=raiz / "mods")
    return resolver


def test_el_servicio_verifica_el_perfil_activo_de_la_instancia(tmp_path):
    _perfil(tmp_path, plugins=_COMPLETO, grass_en_overwrite=True)
    assert _servicio(_resolver(tmp_path)).verificar_etapas_previas().completas()


def test_el_servicio_ve_la_etapa_que_falta_en_el_perfil_activo(tmp_path):
    _perfil(tmp_path, plugins=[f"*{PLUGIN_DE_WRYE_BASH}"], grass_en_overwrite=True)
    evidencia = _servicio(_resolver(tmp_path)).verificar_etapas_previas()
    assert [f.split(" ")[1] for f in evidencia.faltantes] == ["7"]


@pytest.mark.parametrize("metodo", ["get_mo2_instance_data_root_estricto", "get_mo2_mods_path"])
def test_una_metadata_de_instancia_rota_no_lanza_y_falla_cerrado(tmp_path, metodo):
    _perfil(tmp_path, plugins=_COMPLETO, modlist=["+Grass Cache"], grass_en_mods=("Grass Cache",))
    resolver = _resolver(tmp_path)
    setattr(resolver, metodo, MagicMock(side_effect=RuntimeError("ModOrganizer.ini corrupto")))
    evidencia = _servicio(resolver).verificar_etapas_previas()
    assert not evidencia.completas()


def test_sin_instancia_mo2_el_servicio_no_afirma_etapas(tmp_path):
    resolver = _resolver(tmp_path)
    resolver.get_mo2_instance_data_root_estricto = MagicMock(return_value=None)
    resolver.get_mo2_mods_path = MagicMock(side_effect=RuntimeError("sin instancia"))
    evidencia = _servicio(resolver).verificar_etapas_previas()
    assert len(evidencia.faltantes) == 2

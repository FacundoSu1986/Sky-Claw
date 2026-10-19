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


def _grass_con(raiz: pathlib.Path, crear) -> dict[str, pathlib.Path]:
    rutas = _perfil(raiz, plugins=_COMPLETO)
    grass = rutas["overwrite_dir"] / "Grass"
    grass.mkdir()
    crear(grass)
    return rutas


def _symlink_o_skip(enlace: pathlib.Path, destino: pathlib.Path) -> None:
    try:
        enlace.symlink_to(destino)
    except (OSError, NotImplementedError):
        pytest.skip("el entorno no permite crear symlinks")


@pytest.mark.parametrize(
    "crear",
    [
        pytest.param(lambda g: (g / "Tamriel.cgid").mkdir(), id="directorio"),
        pytest.param(lambda g: (g / "Tamriel.cgid").write_bytes(b""), id="vacio"),
        pytest.param(lambda g: _symlink_o_skip(g / "Tamriel.cgid", g / "no-existe.bin"), id="enlace_colgado"),
        pytest.param(
            lambda g: ((g / "real.bin").write_bytes(b"cgid"), _symlink_o_skip(g / "Tamriel.cgid", g / "real.bin")),
            id="enlace_a_archivo",
        ),
    ],
)
def test_verificar_etapas_previas_de_lods_con_cgid_no_regular_falta_la_etapa_8(tmp_path, crear):
    """Un nombre ``*.cgid`` no es un cache: un directorio, un placeholder vacío o un
    enlace no prueban que No Grass In Objects haya generado algo (review Codex, PR #740)."""
    evidencia = verificar_etapas_previas_de_lods(**_grass_con(tmp_path, crear))
    assert [f.split(" ")[1] for f in evidencia.faltantes] == ["8"]


def test_verificar_etapas_previas_de_lods_con_extension_en_mayusculas_cuenta_el_cache(tmp_path):
    rutas = _grass_con(tmp_path, lambda g: (g / "Tamriel.CGID").write_bytes(b"cgid"))
    assert verificar_etapas_previas_de_lods(**rutas).completas()


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


# =============================================================================
# Toda superficie que puede pedir la etapa 9 DESATENDIDA pasa el guard
# (review Codex P1, PR #740)
# =============================================================================
#
# La propiedad: *una corrida desatendida nunca arranca sin probar las etapas 6–8
# o sin la aprobación explícita del operador, por cualquier superficie*. El camino
# remoto (``GestorDeTrabajosDeLods``) tiene el override HITL; el dispatcher
# (``GenerateLodsStrategy``) no tiene canal remoto, así que corta. La corrida
# ASISTIDA desde la GUI sigue sin guard: decisión del owner (SOP §5.2).

import ast  # noqa: E402

from sky_claw.app.orchestrator.tool_strategies.generate_lods import GenerateLodsStrategy  # noqa: E402
from sky_claw.local.validators.etapas_previas_lods import EvidenciaDeEtapasPrevias  # noqa: E402

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _servicio_con_evidencia(evidencia: EvidenciaDeEtapasPrevias | Exception) -> MagicMock:
    servicio = MagicMock()
    servicio.execute = AsyncMock(return_value={"success": True, "message": ""})
    if isinstance(evidencia, Exception):
        servicio.verificar_etapas_previas = MagicMock(side_effect=evidencia)
    else:
        servicio.verificar_etapas_previas = MagicMock(return_value=evidencia)
    return servicio


@pytest.mark.parametrize(
    "evidencia",
    [EvidenciaDeEtapasPrevias(faltantes=("etapa 7 (Synthesis): falta",)), RuntimeError("metadata rota")],
    ids=["falta_una_etapa", "no_se_pudo_verificar"],
)
async def test_el_dispatcher_no_lanza_una_corrida_desatendida_sin_etapas_previas(evidencia):
    servicio = _servicio_con_evidencia(evidencia)
    resultado = await GenerateLodsStrategy(servicio).execute({"preset": "Medium", "interaction_mode": "desatendido"})
    servicio.execute.assert_not_awaited()
    assert resultado["success"] is False
    assert resultado["message"]
    assert resultado["etapas_faltantes"]
    assert resultado["interaction_mode"] == "desatendido"
    assert resultado["assisted"] is False


async def test_el_dispatcher_lanza_la_corrida_desatendida_con_las_etapas_completas():
    servicio = _servicio_con_evidencia(EvidenciaDeEtapasPrevias(faltantes=()))
    await GenerateLodsStrategy(servicio).execute({"preset": "Medium", "interaction_mode": "desatendido"})
    servicio.execute.assert_awaited_once_with(preset="Medium", interaction_mode="desatendido")


@pytest.mark.parametrize("payload", [{"preset": "Medium"}, {"preset": "Medium", "interaction_mode": "asistido"}])
async def test_la_corrida_asistida_del_dispatcher_sigue_sin_guard(payload):
    """La exclusión documentada (SOP §5.2): el owner decide si la GUI asistida lo suma."""
    servicio = _servicio_con_evidencia(EvidenciaDeEtapasPrevias(faltantes=("etapa 6: falta",)))
    await GenerateLodsStrategy(servicio).execute(payload)
    servicio.verificar_etapas_previas.assert_not_called()
    servicio.execute.assert_awaited_once()


async def test_un_modo_invalido_lo_rechaza_el_servicio_no_el_guard():
    servicio = _servicio_con_evidencia(EvidenciaDeEtapasPrevias(faltantes=()))
    await GenerateLodsStrategy(servicio).execute({"interaction_mode": "unattended"})
    servicio.verificar_etapas_previas.assert_not_called()
    servicio.execute.assert_awaited_once_with(interaction_mode="unattended")


def _superficies_que_reenvian_el_modo() -> set[str]:
    """Módulos de producción que llevan ``interaction_mode`` hasta un ``.execute(...)``.

    Detección por AST, no por lista: el literal ``"interaction_mode"`` en el módulo y
    una llamada ``.execute(`` que lo pasa por nombre o por ``**``. El servicio mismo
    sólo lo escribe en su resultado y no ejecuta a nadie con él.
    """
    encontradas: set[str] = set()
    for archivo in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        if not any(isinstance(n, ast.Constant) and n.value == "interaction_mode" for n in ast.walk(arbol)):
            continue
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == "execute"
                and any(kw.arg in {"interaction_mode", None} for kw in nodo.keywords)
            ):
                encontradas.add(archivo.relative_to(RAIZ).as_posix())
                break
    return encontradas


def test_toda_superficie_que_reenvia_el_modo_pasa_por_el_guard_de_etapas_previas():
    superficies = _superficies_que_reenvian_el_modo()
    # Congelado por igualdad: una superficie nueva rompe el ancla hasta que pase el guard.
    assert superficies == {
        "sky_claw/app/agent/tools/lod_tools.py",
        "sky_claw/app/orchestrator/tool_strategies/generate_lods.py",
    }
    for relativa in superficies:
        arbol = ast.parse((RAIZ / relativa).read_text(encoding="utf-8"))
        nombres = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)}
        assert "etapas_previas_faltantes" in nombres, f"{relativa} reenvía el modo sin el guard de etapas previas"

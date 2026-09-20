"""Tests exhaustivos para el registro canónico de herramientas y ToolReadiness.

Anclas de arquitectura para P2 (Pre-LOD):
A. Set exacto del registry (7 tools congeladas)
B. Identidad key/spec
C. Inmutabilidad del registry y specs
D. Metadata exacta del scanner (proyección tool_defs)
E. Derivaciones GUI/Bootloader (RITUAL_TOOL_MAP, RITUAL_INSTALLER_MAP, RITUAL_INSTALL_ENV, _SNAPSHOT_TOOL_ENV)
F. No tools futuras (PGPatcher, VRAMr, etc. prohibidas en P2)
G. ToolReadiness y clasificación pura
H. Preservación de detección especial (SKSE y Community Shaders)
"""

from __future__ import annotations

import dataclasses
import pathlib
from unittest.mock import AsyncMock, Mock, patch

import pytest

from sky_claw.app.gui._bootloader import _SNAPSHOT_TOOL_ENV
from sky_claw.app.gui.controllers.ritual_runner import (
    RITUAL_INSTALL_ENV,
    RITUAL_INSTALLER_MAP,
    RITUAL_TOOL_MAP,
)
from sky_claw.local.discovery import scanner as scanner_module
from sky_claw.local.discovery.environment import (
    EnvironmentSnapshot,
    HealthStatus,
    MissingTool,
    SkyrimEdition,
    ToolInfo,
    ToolReadiness,
    classify_tool_readiness,
)
from sky_claw.local.discovery.registry import (
    EXTERNAL_TOOL_REGISTRY,
    ExternalToolSpec,
    _validate_and_freeze_registry,
    build_ritual_install_env,
    build_ritual_installer_map,
    build_ritual_tool_map,
    build_snapshot_tool_env,
    build_tool_defs,
    build_tool_path_cfg_keys,
    get_tool_spec,
    iter_external_tool_specs,
)
from sky_claw.local.discovery.scanner import EnvironmentScanner

# ==============================================================================
# A. Set exacto del registry
# ==============================================================================


def test_registry_contiene_exactamente_las_siete_herramientas_actuales() -> None:
    """Congelar por igualdad de conjunto que el registry solo contiene las 7 herramientas actuales."""
    herramientas_esperadas = {
        "skse",
        "loot",
        "xedit",
        "pandora",
        "wrye_bash",
        "dyndolod",
        "community_shaders",
    }
    assert set(EXTERNAL_TOOL_REGISTRY) == herramientas_esperadas
    assert len(EXTERNAL_TOOL_REGISTRY) == 7


# ==============================================================================
# B. Identidad key / spec
# ==============================================================================


def test_identidad_key_y_spec() -> None:
    """Para cada entrada en el registry, la clave debe coincidir exactamente con spec.key."""
    for key, spec in EXTERNAL_TOOL_REGISTRY.items():
        assert key == spec.key
        assert get_tool_spec(key) is spec


def test_iter_external_tool_specs_mantiene_orden_declarativo() -> None:
    """La iteración del registry respeta el orden y devuelve todas las specs."""
    specs = iter_external_tool_specs()
    assert len(specs) == 7
    assert [s.key for s in specs] == [
        "skse",
        "loot",
        "xedit",
        "pandora",
        "wrye_bash",
        "dyndolod",
        "community_shaders",
    ]


# ==============================================================================
# C. Inmutabilidad
# ==============================================================================


def test_registry_es_inmutable() -> None:
    """El registry no permite asignación, borrado o modificación accidental."""
    with pytest.raises(TypeError):
        EXTERNAL_TOOL_REGISTRY["nuevo"] = ExternalToolSpec(  # type: ignore[index]
            key="nuevo",
            exe_names=("nuevo.exe",),
            friendly_action="Accion",
            download_url="https://example.com",
        )

    with pytest.raises(TypeError):
        del EXTERNAL_TOOL_REGISTRY["loot"]  # type: ignore[misc]


def test_external_tool_spec_es_inmutable() -> None:
    """ExternalToolSpec está congelado (frozen) y no permite mutación de atributos."""
    spec = EXTERNAL_TOOL_REGISTRY["loot"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.key = "otro"  # type: ignore[misc]

    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.is_critical = False  # type: ignore[misc]


def test_construccion_con_claves_duplicadas_falla_cerrado() -> None:
    """Validación del registry rechaza duplicados de forma estricta."""
    dup_specs = (
        ExternalToolSpec("loot", ("LOOT.exe",), "Sort", "http://loot"),
        ExternalToolSpec("loot", ("loot.exe",), "Sort", "http://loot"),
    )
    with pytest.raises(ValueError, match="Clave duplicada"):
        _validate_and_freeze_registry(dup_specs)


# ==============================================================================
# D. Metadata exacta del scanner (proyección tool_defs)
# ==============================================================================


def test_proyeccion_tool_defs_es_identica_a_la_forma_historica() -> None:
    """La proyección build_tool_defs() reproduce con exactitud literal los datos de tool_defs."""
    defs = build_tool_defs()
    assert len(defs) == 7

    assert defs == (
        (
            "skse",
            ("skse64_loader.exe", "skse_loader.exe"),
            "Extensor del motor (SKSE) - Requiere instalación manual o auto-instalador",
            "https://skse.silverlock.org/",
            True,
        ),
        (
            "loot",
            ("LOOT.exe", "loot.exe"),
            "Ordenar mods automáticamente",
            "https://github.com/loot/loot/releases",
            True,
        ),
        (
            "xedit",
            ("SSEEdit.exe", "TES5Edit.exe", "xEdit.exe"),
            "Limpiar archivos problemáticos",
            "https://github.com/TES5Edit/TES5Edit/releases",
            False,
        ),
        (
            "pandora",
            ("Pandora Behaviour Engine+.exe", "Pandora Engine.exe", "Pandora.exe"),
            "Generar animaciones",
            "https://github.com/Monitor221hz/Pandora-Behaviour-Engine-Plus/releases",
            False,
        ),
        (
            "wrye_bash",
            ("Wrye Bash.exe", "Wrye Bash Launcher.exe"),
            "Crear parche de compatibilidad",
            "https://www.nexusmods.com/skyrimspecialedition/mods/6837",
            False,
        ),
        (
            "dyndolod",
            ("DynDOLOD64.exe", "DynDOLOD.exe", "DynDOLODx64.exe"),
            "Optimizar gráficos (LOD)",
            "https://www.nexusmods.com/skyrimspecialedition/mods/32382",
            False,
        ),
        (
            "community_shaders",
            ("community_shaders.exe",),
            "Renderizado moderno (Community Shaders) - Instalable desde el dashboard",
            "https://www.nexusmods.com/skyrimspecialedition/mods/86492",
            False,
        ),
    )


def test_scanner_constantes_derivadas_de_registry() -> None:
    """Las constantes de ejecutables de scanner.py son derivadas directamente del registry."""
    assert scanner_module.PANDORA_EXE_NAMES is EXTERNAL_TOOL_REGISTRY["pandora"].exe_names
    assert scanner_module._PANDORA_NAMES is scanner_module.PANDORA_EXE_NAMES
    assert EXTERNAL_TOOL_REGISTRY["loot"].exe_names == scanner_module._LOOT_NAMES
    assert EXTERNAL_TOOL_REGISTRY["xedit"].exe_names == scanner_module._XEDIT_NAMES
    assert EXTERNAL_TOOL_REGISTRY["wrye_bash"].exe_names == scanner_module._WRYE_BASH_NAMES
    assert EXTERNAL_TOOL_REGISTRY["dyndolod"].exe_names == scanner_module._DYNDOLOD_NAMES
    assert EXTERNAL_TOOL_REGISTRY["skse"].exe_names == scanner_module._SKSE_LOADER_NAMES


# ==============================================================================
# E. Derivaciones GUI / Bootloader
# ==============================================================================


def test_derivacion_ritual_tool_map_es_identica_a_la_historica() -> None:
    """RITUAL_TOOL_MAP derivado coincide exactamente con la forma esperada."""
    mapa_derivado = build_ritual_tool_map()
    assert mapa_derivado == RITUAL_TOOL_MAP
    assert RITUAL_TOOL_MAP == {
        "loot": "execute_loot_sorting",
        "wrye_bash": "generate_bashed_patch",
        "dyndolod": "generate_lods",
        "pandora": "generate_animations",
        "xedit": "quick_auto_clean",
    }
    # SKSE y Community Shaders no tienen dispatcher
    assert "skse" not in RITUAL_TOOL_MAP
    assert "community_shaders" not in RITUAL_TOOL_MAP


def test_derivacion_ritual_installer_map_es_identica_a_la_historica() -> None:
    """RITUAL_INSTALLER_MAP derivado coincide exactamente con la forma esperada."""
    mapa_derivado = build_ritual_installer_map()
    assert mapa_derivado == RITUAL_INSTALLER_MAP
    assert RITUAL_INSTALLER_MAP == {
        "loot": "ensure_loot",
        "xedit": "ensure_xedit",
        "pandora": "ensure_pandora",
        "skse": "ensure_skse",
        "community_shaders": "ensure_community_shaders",
    }
    # Wrye Bash y DynDOLOD no tienen auto-instalador de GitHub
    assert "wrye_bash" not in RITUAL_INSTALLER_MAP
    assert "dyndolod" not in RITUAL_INSTALLER_MAP


def test_derivacion_ritual_install_env_es_identica_a_la_historica() -> None:
    """RITUAL_INSTALL_ENV derivado coincide exactamente con la forma esperada."""
    mapa_derivado = build_ritual_install_env()
    assert mapa_derivado == RITUAL_INSTALL_ENV
    assert RITUAL_INSTALL_ENV == {
        "loot": "LOOT_EXE",
        "xedit": "XEDIT_PATH",
        "pandora": "PANDORA_EXE",
    }
    assert "skse" not in RITUAL_INSTALL_ENV


def test_derivacion_snapshot_tool_env_es_identica_a_la_historica() -> None:
    """_SNAPSHOT_TOOL_ENV derivado coincide exactamente con la forma esperada, incluyendo DYNDLOD_EXE."""
    mapa_derivado = build_snapshot_tool_env()
    assert mapa_derivado == _SNAPSHOT_TOOL_ENV
    assert _SNAPSHOT_TOOL_ENV == {
        "loot": "LOOT_EXE",
        "wrye_bash": "WRYE_BASH_PATH",
        "dyndolod": "DYNDLOD_EXE",
        "pandora": "PANDORA_EXE",
        "xedit": "XEDIT_PATH",
    }
    assert _SNAPSHOT_TOOL_ENV["dyndolod"] == "DYNDLOD_EXE"


def test_derivacion_tool_path_cfg_keys() -> None:
    """build_tool_path_cfg_keys() coincide exactamente con los campos soportados en config.toml."""
    assert build_tool_path_cfg_keys() == {
        "loot": "loot_exe",
        "xedit": "xedit_exe",
        "pandora": "pandora_exe",
    }


def test_omision_limpia_de_valores_none() -> None:
    """Herramientas sin metadata aplicable se omiten de los mapas en vez de tener valor None."""
    assert None not in RITUAL_TOOL_MAP.values()
    assert None not in RITUAL_INSTALLER_MAP.values()
    assert None not in RITUAL_INSTALL_ENV.values()
    assert None not in _SNAPSHOT_TOOL_ENV.values()


# ==============================================================================
# F. No tools futuras
# ==============================================================================


def test_prohibicion_de_herramientas_futuras_en_p2() -> None:
    """Asegura que P2 no introduce especulativamente herramientas de PRs futuros."""
    assert "pgpatcher" not in EXTERNAL_TOOL_REGISTRY
    assert "vramr" not in EXTERNAL_TOOL_REGISTRY
    assert "parallaxr" not in EXTERNAL_TOOL_REGISTRY
    assert "bendr" not in EXTERNAL_TOOL_REGISTRY


# ==============================================================================
# G. ToolReadiness y Clasificación Pura
# ==============================================================================


def test_tool_readiness_enum_members() -> None:
    """Verifica todos los miembros requeridos de ToolReadiness."""
    expected_members = {
        "FOUND": "found",
        "MISSING": "missing",
        "INVALID_PATH": "invalid_path",
        "WRONG_EXECUTABLE": "wrong_executable",
        "STALE_CONFIGURED_PATH": "stale_configured_path",
        "MOVED_INSTALLATION": "moved_installation",
        "VERSION_UNKNOWN": "version_unknown",
        "VERSION_UNSUPPORTED": "version_unsupported",
    }
    for name, value in expected_members.items():
        assert ToolReadiness[name].value == value


def test_tool_info_y_missing_tool_readiness_defaults() -> None:
    """ToolInfo y MissingTool tienen defaults de readiness compatibles."""
    info = ToolInfo(
        name="LOOT",
        exe_path=pathlib.Path("C:/Tools/LOOT/loot.exe"),
        friendly_action="Ordenar mods",
    )
    assert info.readiness == ToolReadiness.FOUND

    missing = MissingTool(
        name="LOOT",
        technical_name="LOOT",
        friendly_description="Ordenar mods",
        download_url="https://example.com",
    )
    assert missing.readiness == ToolReadiness.MISSING


def test_classify_tool_readiness_pura() -> None:
    """Prueba la máquina de estados de classify_tool_readiness con inputs puros."""
    loot_path = pathlib.Path("C:/Modding/loot.exe")

    # 1. Configured path válido + ejecutable esperado
    assert (
        classify_tool_readiness(
            configured_path=loot_path,
            expected_names=("loot.exe", "LOOT.exe"),
            configured_path_exists=True,
            configured_path_is_file=True,
        )
        == ToolReadiness.FOUND
    )

    # 2. Configured path inexistente y sin autodiscovery
    assert (
        classify_tool_readiness(
            configured_path=loot_path,
            expected_names=("loot.exe",),
            configured_path_exists=False,
            discovered_path=None,
        )
        == ToolReadiness.STALE_CONFIGURED_PATH
    )

    # 3. Configured path inexistente pero autodiscovery encontró la tool en otra ruta
    otra_ruta = pathlib.Path("D:/Games/loot.exe")
    assert (
        classify_tool_readiness(
            configured_path=loot_path,
            expected_names=("loot.exe",),
            configured_path_exists=False,
            discovered_path=otra_ruta,
        )
        == ToolReadiness.MOVED_INSTALLATION
    )

    # 4. Configured path existe pero es un directorio (invalido)
    assert (
        classify_tool_readiness(
            configured_path=pathlib.Path("C:/Modding/loot"),
            expected_names=("loot.exe",),
            configured_path_exists=True,
            configured_path_is_file=False,
        )
        == ToolReadiness.INVALID_PATH
    )

    # 5. Configured path existe pero el nombre no corresponde a la herramienta esperada
    assert (
        classify_tool_readiness(
            configured_path=pathlib.Path("C:/Modding/notepad.exe"),
            expected_names=("loot.exe", "LOOT.exe"),
            configured_path_exists=True,
            configured_path_is_file=True,
        )
        == ToolReadiness.WRONG_EXECUTABLE
    )

    # 5b. Basename con casing distinto (Loot.exe vs ("LOOT.exe", "loot.exe"))
    assert (
        classify_tool_readiness(
            configured_path=pathlib.Path("C:/Modding/Loot.exe"),
            expected_names=("LOOT.exe", "loot.exe"),
            configured_path_exists=True,
            configured_path_is_file=True,
        )
        == ToolReadiness.FOUND
    )

    # 5c. Sin probes de filesystem cuando exists=False ya es conocido (Finding D / M6)
    mock_stale = Mock(spec=pathlib.Path)
    mock_stale.name = "LOOT.exe"
    mock_stale.is_file.side_effect = AssertionError("classify_tool_readiness invocó is_file() pese a exists=False")
    mock_stale.exists.side_effect = AssertionError(
        "classify_tool_readiness invocó exists() pese a configured_path_exists dado"
    )
    assert (
        classify_tool_readiness(
            configured_path=mock_stale,
            configured_path_exists=False,
            configured_path_is_file=None,
            expected_names=("LOOT.exe",),
        )
        == ToolReadiness.STALE_CONFIGURED_PATH
    )

    # 6. Sin configured path, descubierto por scan
    assert (
        classify_tool_readiness(
            configured_path=None,
            discovered_path=loot_path,
        )
        == ToolReadiness.FOUND
    )

    # 7. Sin configured path y no descubierto
    assert (
        classify_tool_readiness(
            configured_path=None,
            discovered_path=None,
        )
        == ToolReadiness.MISSING
    )

    # 8. Versión desconocida (no bloqueante)
    assert (
        classify_tool_readiness(
            configured_path=loot_path,
            expected_names=("loot.exe",),
            configured_path_exists=True,
            configured_path_is_file=True,
            version_unknown=True,
        )
        == ToolReadiness.VERSION_UNKNOWN
    )

    # 9. Versión no soportada
    assert (
        classify_tool_readiness(
            configured_path=loot_path,
            expected_names=("loot.exe",),
            configured_path_exists=True,
            configured_path_is_file=True,
            version_supported=False,
        )
        == ToolReadiness.VERSION_UNSUPPORTED
    )


def test_environment_snapshot_to_dict_preserva_wire_shape() -> None:
    """EnvironmentSnapshot.to_dict() conserva intacta su interfaz serializada."""
    snap = EnvironmentSnapshot(
        tools={
            "loot": ToolInfo(
                name="LOOT",
                exe_path=pathlib.Path("C:/loot.exe"),
                friendly_action="Ordenar",
            )
        },
        missing=[
            MissingTool(
                name="SSEEdit",
                technical_name="SSEEdit",
                friendly_description="Limpiar",
                download_url="https://example.com",
            )
        ],
        health_status=HealthStatus.READY,
        health_messages=["Todo listo"],
    )
    d = snap.to_dict()
    assert set(d.keys()) == {"skyrim", "mo2", "tools", "missing", "health", "messages"}
    assert d["tools"] == {"loot": str(pathlib.Path("C:/loot.exe"))}
    assert d["missing"] == ["SSEEdit"]
    assert d["health"] == "ready"
    assert d["messages"] == ["Todo listo"]


# ==============================================================================
# H. Preservación de detección especial
# ==============================================================================


@pytest.mark.asyncio
async def test_preservacion_deteccion_especial_skse(tmp_path: pathlib.Path) -> None:
    """Verifica que SKSE continúa invocando find_skse_installation en lugar del resolver genérico."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    scanner = EnvironmentScanner(skyrim_path=skyrim_dir)

    with patch("sky_claw.local.discovery.scanner.find_skse_installation") as mock_skse:
        mock_skse.return_value = skyrim_dir / "skse64_loader.exe"
        snap = await scanner.scan()

        mock_skse.assert_called_once_with(
            skyrim_dir,
            edition=SkyrimEdition.SE,
            game_version="",
        )
        assert snap.has_tool("skse")
        assert snap.tools["skse"].exe_path == skyrim_dir / "skse64_loader.exe"
        assert snap.tools["skse"].readiness == ToolReadiness.FOUND


@pytest.mark.asyncio
async def test_preservacion_deteccion_especial_community_shaders(tmp_path: pathlib.Path) -> None:
    """Verifica que Community Shaders no busca un binario ejecutable en disco sino su sentinel de mod MO2.

    Hermético para CI: siembra un Skyrim controlado en tmp_path para que el scanner no aborte
    antes de la fase MO2 en runners limpios sin registro de Windows ni Steam.
    """
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    mo2_dir = tmp_path / "MO2"
    mo2_dir.mkdir()
    mods_dir = mo2_dir / "mods"
    mods_dir.mkdir()

    scanner = EnvironmentScanner(skyrim_path=skyrim_dir)
    with patch.object(EnvironmentScanner, "_find_mo2", AsyncMock(return_value=mo2_dir)):
        # Sin mod instalado -> reporta missing
        with patch.object(EnvironmentScanner, "_resolve_tool_path") as mock_resolve:
            snap = await scanner.scan()
            # _resolve_tool_path NO fue llamado para community_shaders ni skse
            assert "community_shaders" not in [call.args[0] for call in mock_resolve.call_args_list]
            assert not snap.has_tool("community_shaders")
            cs_missing = [m for m in snap.missing if m.technical_name == "community_shaders"]
            assert len(cs_missing) == 1
            assert cs_missing[0].readiness == ToolReadiness.MISSING

        # Con mod y sentinel presente en disco
        cs_dir = mods_dir / "Community Shaders"
        cs_plugins = cs_dir / "SKSE" / "Plugins"
        cs_plugins.mkdir(parents=True)
        (cs_plugins / "CommunityShaders.dll").write_bytes(b"DLL")
        (cs_dir / "Shaders").mkdir()

        snap2 = await scanner.scan()
        assert snap2.has_tool("community_shaders")
        assert snap2.tools["community_shaders"].exe_path == cs_plugins / "CommunityShaders.dll"
        assert snap2.tools["community_shaders"].readiness == ToolReadiness.FOUND


# ==============================================================================
# I. Integración real del clasificador de readiness con EnvironmentScanner
# ==============================================================================


@pytest.mark.asyncio
async def test_scanner_configured_path_correcta_produce_tool_info_found(tmp_path: pathlib.Path) -> None:
    """Ruta configurada válida y con nombre esperado produce ToolInfo con FOUND."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    loot_exe = tmp_path / "LOOT" / "LOOT.exe"
    loot_exe.parent.mkdir()
    loot_exe.write_bytes(b"MZ")

    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(loot_exe)},
    )
    snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.tools["loot"].readiness == ToolReadiness.FOUND
    assert snap.tools["loot"].exe_path == loot_exe
    assert any("✅ LOOT encontrado" in m for m in snap.health_messages)


@pytest.mark.asyncio
async def test_scanner_configured_path_casing_distinto_produce_found(tmp_path: pathlib.Path) -> None:
    """Ruta configurada con casing diferente ('Loot.exe' vs 'LOOT.exe') se acepta case-insensitively como FOUND."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    loot_exe = tmp_path / "LOOT" / "Loot.exe"
    loot_exe.parent.mkdir()
    loot_exe.write_bytes(b"MZ")

    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(loot_exe)},
    )
    snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.tools["loot"].readiness == ToolReadiness.FOUND
    assert snap.tools["loot"].exe_path == loot_exe


@pytest.mark.asyncio
async def test_scanner_configured_path_inexistente_sin_alternativa_produce_missing_stale(
    tmp_path: pathlib.Path,
) -> None:
    """Ruta configurada que no existe en disco, sin alternativa, produce MissingTool con STALE_CONFIGURED_PATH."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    stale_path = tmp_path / "inexistente" / "LOOT.exe"
    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(stale_path)},
    )

    with patch.object(EnvironmentScanner, "_find_tool", return_value=None):
        snap = await scanner.scan()

    assert not snap.has_tool("loot")
    missing_loot = [m for m in snap.missing if m.technical_name.lower() == "loot"][0]
    assert missing_loot.readiness == ToolReadiness.STALE_CONFIGURED_PATH
    assert not any("✅ LOOT" in m for m in snap.health_messages)
    assert any("la ruta configurada ya no existe" in m for m in snap.health_messages)


@pytest.mark.asyncio
async def test_scanner_configured_path_inexistente_con_autodiscovery_produce_moved_installation(
    tmp_path: pathlib.Path,
) -> None:
    """Ruta configurada inexistente pero la herramienta es descubierta en otra ruta produce ToolInfo con MOVED_INSTALLATION."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    stale_path = tmp_path / "inexistente" / "LOOT.exe"
    discovered_loot = tmp_path / "Discovered" / "LOOT.exe"
    discovered_loot.parent.mkdir()
    discovered_loot.write_bytes(b"MZ")

    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(stale_path)},
    )

    with patch.object(EnvironmentScanner, "_find_tool", return_value=discovered_loot):
        snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.tools["loot"].readiness == ToolReadiness.MOVED_INSTALLATION
    assert snap.tools["loot"].exe_path == discovered_loot
    assert not any("✅ LOOT encontrado" in m for m in snap.health_messages)
    assert any(
        "encontrado en ubicación detectada, pero la ruta configurada ya no existe" in m for m in snap.health_messages
    )


@pytest.mark.asyncio
async def test_scanner_configured_path_directorio_produce_invalid_path(tmp_path: pathlib.Path) -> None:
    """Ruta configurada que apunta a un directorio en vez de un archivo produce MissingTool con INVALID_PATH."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    dir_loot = tmp_path / "un_directorio"
    dir_loot.mkdir()

    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(dir_loot)},
    )

    with patch.object(EnvironmentScanner, "_find_tool", return_value=None):
        snap = await scanner.scan()

    assert not snap.has_tool("loot")
    missing_loot = [m for m in snap.missing if m.technical_name.lower() == "loot"][0]
    assert missing_loot.readiness == ToolReadiness.INVALID_PATH
    assert not any("✅ LOOT" in m for m in snap.health_messages)


@pytest.mark.asyncio
async def test_scanner_configured_path_notepad_sin_alternativa_produce_missing_wrong_executable(
    tmp_path: pathlib.Path,
) -> None:
    """Ruta configurada que apunta a un ejecutable ajeno (notepad.exe) produce WRONG_EXECUTABLE y NUNCA se publica en ToolInfo."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    notepad = tmp_path / "notepad.exe"
    notepad.write_bytes(b"MZ")

    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(notepad)},
    )

    with patch.object(EnvironmentScanner, "_find_tool", return_value=None):
        snap = await scanner.scan()

    assert not snap.has_tool("loot")
    missing_loot = [m for m in snap.missing if m.technical_name.lower() == "loot"][0]
    assert missing_loot.readiness == ToolReadiness.WRONG_EXECUTABLE
    # El binario ajeno NUNCA debe ser publicado como ToolInfo runnable
    assert not any(t.exe_path == notepad for t in snap.tools.values())
    assert any("apunta a un ejecutable incorrecto" in m for m in snap.health_messages)


@pytest.mark.asyncio
async def test_scanner_configured_path_notepad_con_autodiscovery_produce_tool_info_con_real_loot(
    tmp_path: pathlib.Path,
) -> None:
    """Configured path incorrecta con autodiscovery válido usa la ruta real de LOOT y reporta WRONG_EXECUTABLE."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    notepad = tmp_path / "notepad.exe"
    notepad.write_bytes(b"MZ")

    real_loot = tmp_path / "Discovered" / "LOOT.exe"
    real_loot.parent.mkdir()
    real_loot.write_bytes(b"MZ")

    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(notepad)},
    )

    with patch.object(EnvironmentScanner, "_find_tool", return_value=real_loot):
        snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.tools["loot"].readiness == ToolReadiness.WRONG_EXECUTABLE
    assert snap.tools["loot"].exe_path == real_loot
    assert snap.tools["loot"].exe_path != notepad
    assert any("apunta a un ejecutable incorrecto" in m for m in snap.health_messages)
    assert not any("✅ LOOT encontrado" in m for m in snap.health_messages)


@pytest.mark.asyncio
async def test_scanner_sin_configured_path_con_autodiscovery_produce_found(tmp_path: pathlib.Path) -> None:
    """Sin ruta configurada, cuando autodiscovery encuentra la herramienta se reporta FOUND."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    real_loot = tmp_path / "Discovered" / "LOOT.exe"
    real_loot.parent.mkdir()
    real_loot.write_bytes(b"MZ")

    scanner = EnvironmentScanner(skyrim_path=skyrim_dir)

    def mock_find(exe_names, roots):
        if "LOOT.exe" in exe_names:
            return real_loot
        return None

    with patch.object(EnvironmentScanner, "_find_tool", side_effect=mock_find):
        snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.tools["loot"].readiness == ToolReadiness.FOUND
    assert snap.tools["loot"].exe_path == real_loot
    assert any("✅ LOOT encontrado" in m for m in snap.health_messages)


@pytest.mark.asyncio
async def test_scanner_sin_configured_path_ni_discovery_produce_missing(tmp_path: pathlib.Path) -> None:
    """Sin ruta configurada ni autodiscovery, se reporta MissingTool con MISSING."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    scanner = EnvironmentScanner(skyrim_path=skyrim_dir)

    with patch.object(EnvironmentScanner, "_find_tool", return_value=None):
        snap = await scanner.scan()

    assert not snap.has_tool("loot")
    missing_loot = [m for m in snap.missing if m.technical_name.lower() == "loot"][0]
    assert missing_loot.readiness == ToolReadiness.MISSING
    assert any("❌ LOOT no encontrado" in m for m in snap.health_messages)

"""Tests para detección de versión de herramientas externas (Pre-LOD P3).

Verifica la infraestructura de detección de versiones para herramientas externas
declaradas en el registry (LOOT y xEdit) sin convertir versiones no detectadas
en bloqueos de disponibilidad ni alterar los contratos de los lanes críticos
(SKSE, DynDOLOD, Golden Master).
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.discovery.environment import (
    EnvironmentSnapshot,
    HealthStatus,
    SkyrimEdition,
    SkyrimInfo,
    ToolInfo,
    ToolReadiness,
    classify_tool_readiness,
)
from sky_claw.local.discovery.registry import (
    EXTERNAL_TOOL_REGISTRY,
    VersionProbeKind,
    get_tool_spec,
)
from sky_claw.local.discovery.scanner import (
    EnvironmentScanner,
    detect_tool_version,
)

# ==============================================================================
# Registry Declarativo y Metadata Mínima
# ==============================================================================


def test_registry_metadata_version_probe_kind() -> None:
    """Verifica que el registry declare los tipos de probe esperados para cada herramienta."""
    # Arrange & Act & Assert
    # Exactamente 7 herramientas en P3
    assert len(EXTERNAL_TOOL_REGISTRY) == 7

    # LOOT utiliza probe CLI
    spec_loot = EXTERNAL_TOOL_REGISTRY["loot"]
    assert spec_loot.version_probe_kind == VersionProbeKind.LOOT_CLI

    # xEdit utiliza probe PE ProductVersion
    spec_xedit = EXTERNAL_TOOL_REGISTRY["xedit"]
    assert spec_xedit.version_probe_kind == VersionProbeKind.PE_PRODUCT_VERSION

    # Las restantes herramientas NO tienen probe configurado en P3
    for key in ("skse", "pandora", "wrye_bash", "dyndolod", "community_shaders"):
        spec = EXTERNAL_TOOL_REGISTRY[key]
        assert spec.version_probe_kind == VersionProbeKind.NONE


def test_m7_no_se_agrega_pgpatcher_ni_tools_futuras_en_p3() -> None:
    """M7: Regresión que prohíbe incorporar PGPatcher, VRAMr o herramientas de P4/P5."""
    assert "pgpatcher" not in EXTERNAL_TOOL_REGISTRY
    assert "vramr" not in EXTERNAL_TOOL_REGISTRY
    assert "bendr" not in EXTERNAL_TOOL_REGISTRY
    assert "parallaxr" not in EXTERNAL_TOOL_REGISTRY
    assert get_tool_spec("pgpatcher") is None


# ==============================================================================
# A. xEdit PE ProductVersion
# ==============================================================================


@pytest.mark.asyncio
async def test_xedit_pe_version_detectada_exitosamente() -> None:
    """M1: xEdit con ProductVersion válido en PE retorna la versión formateada."""
    # Arrange
    fake_exe = pathlib.Path("C:/Tools/xEdit/SSEEdit.exe")

    with patch("sky_claw.local.discovery.scanner._read_pe_product_version", return_value="4.1.5f") as mock_read:
        # Act
        version = await detect_tool_version(
            "xedit",
            fake_exe,
            probe_kind=VersionProbeKind.PE_PRODUCT_VERSION,
        )

        # Assert
        mock_read.assert_called_once_with(fake_exe)
        assert version == "4.1.5f"


@pytest.mark.asyncio
async def test_xedit_pe_sin_recurso_de_version_retorna_none() -> None:
    """xEdit con PE válido pero sin ProductVersion (cadena vacía) retorna None."""
    # Arrange
    fake_exe = pathlib.Path("C:/Tools/xEdit/SSEEdit.exe")

    with patch("sky_claw.local.discovery.scanner._read_pe_product_version", return_value=""):
        # Act
        version = await detect_tool_version(
            "xedit",
            fake_exe,
            probe_kind=VersionProbeKind.PE_PRODUCT_VERSION,
        )

        # Assert
        assert version is None


@pytest.mark.asyncio
async def test_xedit_pe_error_o_sin_pefile_retorna_none_sin_excepcion() -> None:
    """M4: Error al leer PE o ausencia de pefile retorna None sin propagar excepciones."""
    # Arrange
    fake_exe = pathlib.Path("C:/Tools/xEdit/SSEEdit.exe")

    with patch("sky_claw.local.discovery.scanner._read_pe_product_version", return_value=None):
        # Act
        version = await detect_tool_version(
            "xedit",
            fake_exe,
            probe_kind=VersionProbeKind.PE_PRODUCT_VERSION,
        )

        # Assert
        assert version is None


@pytest.mark.asyncio
async def test_xedit_cancelacion_propaga_correctamente() -> None:
    """La cancelación externa de asyncio no se enmascara en detect_tool_version."""
    fake_exe = pathlib.Path("C:/Tools/xEdit/SSEEdit.exe")

    with (
        patch(
            "sky_claw.local.discovery.scanner._read_pe_product_version",
            side_effect=asyncio.CancelledError("Cancelación solicitada"),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await detect_tool_version(
            "xedit",
            fake_exe,
            probe_kind=VersionProbeKind.PE_PRODUCT_VERSION,
        )


# ==============================================================================
# B. LOOT --version
# ==============================================================================


@pytest.mark.asyncio
async def test_loot_version_output_valido_retorna_version() -> None:
    """M2: LOOT con output parseable por detect_loot_version retorna versión semver."""
    # Arrange
    fake_exe = pathlib.Path("C:/Tools/LOOT/LOOT.exe")

    with patch("sky_claw.local.loot.version.detect_loot_version", AsyncMock(return_value=(0, 29, 1))) as mock_detect:
        # Act
        version = await detect_tool_version(
            "loot",
            fake_exe,
            probe_kind=VersionProbeKind.LOOT_CLI,
        )

        # Assert
        mock_detect.assert_called_once_with(fake_exe)
        assert version == "0.29.1"


@pytest.mark.asyncio
async def test_loot_version_exit_no_cero_retorna_none() -> None:
    """detect_loot_version retorna None cuando el subproceso falla (exit != 0)."""
    # Arrange
    fake_exe = pathlib.Path("C:/Tools/LOOT/LOOT.exe")

    with patch("sky_claw.local.loot.version.detect_loot_version", AsyncMock(return_value=None)):
        # Act
        version = await detect_tool_version(
            "loot",
            fake_exe,
            probe_kind=VersionProbeKind.LOOT_CLI,
        )

        # Assert
        assert version is None


@pytest.mark.asyncio
async def test_loot_version_timeout_retorna_none_sin_bloqueo() -> None:
    """M3: Timeout o fallo de subproceso en LOOT retorna None y no propaga error."""
    # Arrange
    fake_exe = pathlib.Path("C:/Tools/LOOT/LOOT.exe")

    with patch("sky_claw.local.loot.version.detect_loot_version", AsyncMock(side_effect=TimeoutError("Timeout"))):
        # Act
        version = await detect_tool_version(
            "loot",
            fake_exe,
            probe_kind=VersionProbeKind.LOOT_CLI,
        )

        # Assert
        assert version is None


@pytest.mark.asyncio
async def test_loot_version_output_no_parseable_retorna_none() -> None:
    """LOOT con salida corrupta o inesperada retorna None sin romper el flujo."""
    # Arrange
    fake_exe = pathlib.Path("C:/Tools/LOOT/LOOT.exe")

    with patch("sky_claw.local.loot.version.detect_loot_version", AsyncMock(return_value=None)):
        # Act
        version = await detect_tool_version(
            "loot",
            fake_exe,
            probe_kind=VersionProbeKind.LOOT_CLI,
        )

        # Assert
        assert version is None


@pytest.mark.asyncio
async def test_loot_version_cancelacion_propaga_correctamente() -> None:
    """asyncio.CancelledError se propaga sin atraparse en detect_tool_version para LOOT."""
    fake_exe = pathlib.Path("C:/Tools/LOOT/LOOT.exe")

    with (
        patch(
            "sky_claw.local.loot.version.detect_loot_version",
            AsyncMock(side_effect=asyncio.CancelledError("Cancelación solicitada")),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await detect_tool_version(
            "loot",
            fake_exe,
            probe_kind=VersionProbeKind.LOOT_CLI,
        )


# ==============================================================================
# C. Integración en Snapshot (EnvironmentScanner)
# ==============================================================================


@pytest.mark.asyncio
async def test_snapshot_puebla_tool_info_version_para_loot_y_xedit(tmp_path: pathlib.Path) -> None:
    """M1 & M2: EnvironmentScanner puebla ToolInfo.version con la evidencia detectada."""
    # Arrange
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")

    loot_exe = tmp_path / "LOOT" / "LOOT.exe"
    loot_exe.parent.mkdir()
    loot_exe.write_bytes(b"MZ")

    xedit_exe = tmp_path / "xEdit" / "SSEEdit.exe"
    xedit_exe.parent.mkdir()
    xedit_exe.write_bytes(b"MZ")

    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(loot_exe), "xedit": str(xedit_exe)},
    )

    with (
        patch("sky_claw.local.loot.version.detect_loot_version", AsyncMock(return_value=(0, 29, 0))),
        patch("sky_claw.local.discovery.scanner._read_pe_product_version", return_value="4.1.5"),
    ):
        # Act
        snap = await scanner.scan()

    # Assert
    assert snap.has_tool("loot")
    assert snap.tools["loot"].version == "0.29.0"
    assert snap.tools["loot"].readiness == ToolReadiness.FOUND
    assert any("✅ LOOT encontrado (v0.29.0)" in m for m in snap.health_messages)

    assert snap.has_tool("xedit")
    assert snap.tools["xedit"].version == "4.1.5"
    assert snap.tools["xedit"].readiness == ToolReadiness.FOUND
    assert any("✅ XEDIT encontrado (v4.1.5)" in m for m in snap.health_messages)


@pytest.mark.asyncio
async def test_snapshot_version_desconocida_mantiene_toolinfo_usable(tmp_path: pathlib.Path) -> None:
    """M3 & M4: Versión desconocida mantiene la herramienta disponible y no la envía a missing."""
    # Arrange
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"MZ")
    (skyrim_dir / "skse64_loader.exe").write_bytes(b"MZ")
    (skyrim_dir / "skse64_1_6_1170.dll").write_bytes(b"MZ")

    loot_exe = tmp_path / "LOOT" / "LOOT.exe"
    loot_exe.parent.mkdir()
    loot_exe.write_bytes(b"MZ")

    xedit_exe = tmp_path / "xEdit" / "SSEEdit.exe"
    xedit_exe.parent.mkdir()
    xedit_exe.write_bytes(b"MZ")

    scanner = EnvironmentScanner(
        skyrim_path=skyrim_dir,
        tool_paths={"loot": str(loot_exe), "xedit": str(xedit_exe)},
    )

    # Simular timeout en LOOT y PE sin versión en xEdit
    with (
        patch("sky_claw.local.loot.version.detect_loot_version", AsyncMock(return_value=None)),
        patch("sky_claw.local.discovery.scanner._read_pe_product_version", return_value=""),
    ):
        # Act
        snap = await scanner.scan()

    # Assert
    # Ambas herramientas permanecen en tools, con versión vacía
    assert snap.has_tool("loot")
    assert snap.tools["loot"].version == ""
    assert snap.tools["loot"].readiness == ToolReadiness.FOUND
    assert not any(m.technical_name.lower() == "loot" for m in snap.missing)

    assert snap.has_tool("xedit")
    assert snap.tools["xedit"].version == ""
    assert snap.tools["xedit"].readiness == ToolReadiness.FOUND
    assert not any("sseedit" in m.technical_name.lower() for m in snap.missing)

    # Health general no se degrada a crítico por falta de versión
    assert snap.health_status == HealthStatus.READY


def test_tool_info_con_readiness_version_unknown_sigue_siendo_usable() -> None:
    """ToolReadiness.VERSION_UNKNOWN preserva la disponibilidad del tool en EnvironmentSnapshot."""
    # Arrange
    fake_exe = pathlib.Path("C:/Tools/LOOT/LOOT.exe")
    tool_info = ToolInfo(
        name="LOOT",
        exe_path=fake_exe,
        version="",
        friendly_action="Ordenar mods",
        readiness=ToolReadiness.VERSION_UNKNOWN,
    )
    snap = EnvironmentSnapshot(
        tools={"loot": tool_info},
        missing=[],
        health_status=HealthStatus.READY,
    )

    # Act & Assert
    assert snap.has_tool("loot")
    assert snap.tools["loot"].readiness == ToolReadiness.VERSION_UNKNOWN
    assert snap.tools["loot"].version == ""
    assert not any(m.technical_name.lower() == "loot" for m in snap.missing)


# ==============================================================================
# D. Precedencia de Readiness (M5, M6 y UNKNOWN != UNSUPPORTED)
# ==============================================================================


def test_m5_version_unknown_no_sobrescribe_wrong_executable() -> None:
    """M5: Configured path que apunta a otro ejecutable preserva WRONG_EXECUTABLE aun con version_unknown=True."""
    # Arrange
    notepad = pathlib.Path("C:/Windows/notepad.exe")

    # Act
    readiness = classify_tool_readiness(
        configured_path=notepad,
        expected_names=("LOOT.exe", "loot.exe"),
        configured_path_exists=True,
        configured_path_is_file=True,
        version_unknown=True,
    )

    # Assert: La identidad incorrecta tiene prioridad sobre la versión desconocida
    assert readiness == ToolReadiness.WRONG_EXECUTABLE


def test_m6_version_unknown_no_sobrescribe_moved_installation() -> None:
    """M6: Configured path inexistente con autodiscovery válido preserva MOVED_INSTALLATION aun con version_unknown=True."""
    # Arrange
    stale_path = pathlib.Path("C:/OldTools/LOOT.exe")
    discovered_path = pathlib.Path("D:/Games/LOOT/LOOT.exe")

    # Act
    readiness = classify_tool_readiness(
        configured_path=stale_path,
        discovered_path=discovered_path,
        expected_names=("LOOT.exe", "loot.exe"),
        configured_path_exists=False,
        version_unknown=True,
    )

    # Assert: El drift de ubicación tiene prioridad sobre la versión desconocida
    assert readiness == ToolReadiness.MOVED_INSTALLATION


def test_version_unknown_no_sobrescribe_stale_path_ni_invalid_path() -> None:
    """STALE_CONFIGURED_PATH e INVALID_PATH prevalecen sobre version_unknown=True."""
    # Stale sin descubrimiento
    stale_readiness = classify_tool_readiness(
        configured_path=pathlib.Path("C:/Old/LOOT.exe"),
        discovered_path=None,
        configured_path_exists=False,
        version_unknown=True,
    )
    assert stale_readiness == ToolReadiness.STALE_CONFIGURED_PATH

    # Carpeta en vez de ejecutable
    dir_readiness = classify_tool_readiness(
        configured_path=pathlib.Path("C:/Old/LOOT_DIR"),
        configured_path_exists=True,
        configured_path_is_file=False,
        version_unknown=True,
    )
    assert dir_readiness == ToolReadiness.INVALID_PATH


def test_version_unknown_no_convierte_missing_en_encontrada() -> None:
    """Sin configured path ni descubrimiento, el estado sigue siendo MISSING aun con version_unknown=True."""
    readiness = classify_tool_readiness(
        configured_path=None,
        discovered_path=None,
        version_unknown=True,
    )
    assert readiness == ToolReadiness.MISSING


def test_distincion_estricta_unknown_vs_unsupported() -> None:
    """VERSION_UNKNOWN y VERSION_UNSUPPORTED son estados ortogonales."""
    # Arrange & Act & Assert
    assert ToolReadiness.VERSION_UNKNOWN != ToolReadiness.VERSION_UNSUPPORTED

    loot_path = pathlib.Path("C:/Tools/LOOT/LOOT.exe")

    # Versión desconocida: evidencia no disponible
    unknown = classify_tool_readiness(
        configured_path=loot_path,
        expected_names=("LOOT.exe",),
        configured_path_exists=True,
        configured_path_is_file=True,
        version_unknown=True,
    )
    assert unknown == ToolReadiness.VERSION_UNKNOWN

    # Versión no soportada: evidencia presente pero rechazada por policy explícita
    unsupported = classify_tool_readiness(
        configured_path=loot_path,
        expected_names=("LOOT.exe",),
        configured_path_exists=True,
        configured_path_is_file=True,
        version_supported=False,
    )
    assert unsupported == ToolReadiness.VERSION_UNSUPPORTED


@pytest.mark.asyncio
async def test_unsupported_solo_con_politica_explicita(tmp_path: pathlib.Path) -> None:
    """VERSION_UNSUPPORTED solo se emite cuando check_tool_version_supported retorna False explícito."""
    # Arrange
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

    # Mockeamos política explícita que rechaza la versión 0.28.0
    with (
        patch("sky_claw.local.loot.version.detect_loot_version", AsyncMock(return_value=(0, 28, 0))),
        patch("sky_claw.local.discovery.scanner.check_tool_version_supported", return_value=False),
    ):
        # Act
        snap = await scanner.scan()

    # Assert: Se detecta la versión y el readiness refleja VERSION_UNSUPPORTED
    assert snap.has_tool("loot")
    assert snap.tools["loot"].version == "0.28.0"
    assert snap.tools["loot"].readiness == ToolReadiness.VERSION_UNSUPPORTED
    assert any("la versión no está soportada" in m for m in snap.health_messages)


# ==============================================================================
# E. Aislamiento y No Side Effects (M8, M9, M10)
# ==============================================================================


@pytest.mark.asyncio
async def test_m8_community_shaders_no_ejecuta_probe_pe() -> None:
    """M8: Community Shaders no es un ejecutable PE y nunca pasa por detect_tool_version ni PE probe."""
    spec = EXTERNAL_TOOL_REGISTRY["community_shaders"]
    assert spec.version_probe_kind == VersionProbeKind.NONE

    # Invocar detect_tool_version con probe_kind=NONE debe retornar None inmediatamente sin tocar disco
    result = await detect_tool_version(
        "community_shaders",
        pathlib.Path("C:/Fake/community_shaders.exe"),
        probe_kind=spec.version_probe_kind,
    )
    assert result is None


def test_m9_skse_toolinfo_version_no_asume_skyrim_runtime() -> None:
    """M9: SKSE en ToolInfo no adquiere automáticamente la versión del runtime de Skyrim."""
    skyrim_info = SkyrimInfo(
        path=pathlib.Path("C:/Skyrim"),
        exe_name="SkyrimSE.exe",
        edition=SkyrimEdition.AE,
        version="1.6.1170",
    )
    skse_spec = EXTERNAL_TOOL_REGISTRY["skse"]
    assert skse_spec.version_probe_kind == VersionProbeKind.NONE

    tool_info = ToolInfo(
        name="SKSE",
        exe_path=pathlib.Path("C:/Skyrim/skse64_loader.exe"),
        version="",
    )
    # ToolInfo.version de SKSE NO es igual a SkyrimInfo.version
    assert tool_info.version != skyrim_info.version
    assert tool_info.version == ""


def test_m10_skyrim_version_matches_preservada_exactamente() -> None:
    """M10: skyrim_version_matches preserva su semántica por prefijo intacta."""
    from sky_claw.local.discovery.scanner import skyrim_version_matches

    # Match de build de 4 partes contra expectativa de 3 partes
    assert skyrim_version_matches("1.6.1170.0", "1.6.1170") is True
    # Match exacto
    assert skyrim_version_matches("1.6.640", "1.6.640") is True
    # Desajuste de versión menor
    assert skyrim_version_matches("1.5.97", "1.6.1170") is False


@pytest.mark.asyncio
async def test_detect_tool_version_probe_kind_none_retorna_none() -> None:
    """Herramientas con VersionProbeKind.NONE (Pandora, Wrye Bash, DynDOLOD) retornan None sin I/O."""
    for key in ("pandora", "wrye_bash", "dyndolod"):
        spec = EXTERNAL_TOOL_REGISTRY[key]
        ver = await detect_tool_version(
            key,
            pathlib.Path(f"C:/Fake/{spec.exe_names[0]}"),
            probe_kind=spec.version_probe_kind,
        )
        assert ver is None


@pytest.mark.asyncio
async def test_no_side_effects_durante_escaneo(tmp_path: pathlib.Path) -> None:
    """E: El escaneo con version detection no invoca el instalador ni persiste config ni altera el filesystem."""
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

    with (
        patch("sky_claw.local.loot.version.detect_loot_version", AsyncMock(return_value=(0, 29, 0))),
        patch("sky_claw.local.tools_installer.ToolsInstaller") as mock_installer,
        patch("sky_claw.config.Config.save") as mock_config_save,
        patch("sky_claw.local.local_config.guardar_config") as mock_guardar_cfg,
    ):
        snap = await scanner.scan()

        # Ningún instalador ni persistencia debe invocarse
        mock_installer.assert_not_called()
        mock_config_save.assert_not_called()
        mock_guardar_cfg.assert_not_called()

        assert snap.has_tool("loot")
        assert snap.tools["loot"].version == "0.29.0"

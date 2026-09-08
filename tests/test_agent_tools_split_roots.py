"""Tests para las tools del agente con topología de raíces separadas (Issue #557).

Verifica que:
1. install_mod_from_archive escribe en mo2.mods_dir y no en install_root/mods.
2. setup_tools (NGIO y Community Shaders) instala en mods_dir y no en mo2_root/mods.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.agent.tools.external_tools import setup_tools
from sky_claw.app.agent.tools.system_tools import install_mod_from_archive
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.mo2.vfs import MO2Controller


@pytest.fixture()
def split_topology(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    install = tmp_path / "Install"
    data = tmp_path / "Instance"
    mods = tmp_path / "CustomMods"

    install.mkdir(parents=True)
    data.mkdir(parents=True)
    mods.mkdir(parents=True)

    (install / "ModOrganizer.exe").write_bytes(b"fake-exe")
    (data / "profiles" / "Test").mkdir(parents=True)
    (data / "profiles" / "Test" / "modlist.txt").write_text("", encoding="utf-8-sig")

    # Trampa: carpeta mods bajo Install
    install_mods = install / "mods"
    install_mods.mkdir(parents=True)

    return {
        "install": install,
        "data": data,
        "mods": mods,
        "install_mods": install_mods,
    }


@pytest.mark.asyncio
async def test_install_mod_from_archive_usa_mods_dir_y_no_install_mods(split_topology: dict[str, pathlib.Path]) -> None:
    """install_mod_from_archive pasa mo2.mods_dir a fomod_installer."""
    validator = PathValidator(roots=[split_topology["install"], split_topology["data"], split_topology["mods"]])
    mo2 = MO2Controller(
        install_root=split_topology["install"],
        data_root=split_topology["data"],
        mods_dir=split_topology["mods"],
        path_validator=validator,
    )

    fomod_installer = MagicMock()
    fake_install_result = MagicMock()
    fake_install_result.installed = True
    fake_install_result.mod_name = "MiFomodMod"
    fake_install_result.errors = []
    fake_install_result.files_copied = []
    fake_install_result.pending_decisions = []
    fomod_installer.install = AsyncMock(return_value=fake_install_result)

    hitl = MagicMock()
    from sky_claw.app.security.hitl import Decision

    hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)

    archive = split_topology["install"] / "dummy.zip"
    archive.write_bytes(b"zip")

    res_raw = await install_mod_from_archive(
        mo2=mo2,
        fomod_installer=fomod_installer,
        hitl=hitl,
        archive_path=str(archive),
        selections={},
        profile="Test",
    )

    res = json.loads(res_raw)
    assert res["success"] is True
    fomod_installer.install.assert_called_once()
    kwargs = fomod_installer.install.call_args.kwargs
    # Debe ser el MODS_DIR legítimo, NO install/mods
    assert kwargs["mo2_mods_dir"] == split_topology["mods"]
    assert kwargs["mo2_mods_dir"] != split_topology["install_mods"]


@pytest.mark.asyncio
async def test_setup_tools_ngio_y_cs_usan_mods_dir_explicito(split_topology: dict[str, pathlib.Path]) -> None:
    """setup_tools pasa mods_dir a ensure_ngio y ensure_community_shaders."""
    tools_installer = MagicMock()
    fake_mod = MagicMock()
    fake_mod.mod_name = "NGIOMod"
    fake_mod.mod_dir = split_topology["mods"] / "NGIOMod"
    fake_mod.already_existed = False
    fake_mod.version = "1.0.0"
    tools_installer.ensure_ngio = AsyncMock(return_value=[fake_mod])

    fake_cs_mod = MagicMock()
    fake_cs_mod.mod_name = "CSMod"
    fake_cs_mod.mod_dir = split_topology["mods"] / "CSMod"
    fake_cs_mod.already_existed = False
    fake_cs_mod.version = "1.0.0"
    tools_installer.ensure_community_shaders = AsyncMock(return_value=[fake_cs_mod])

    skyrim_dir = split_topology["install"] / "Skyrim"
    skyrim_dir.mkdir(parents=True)
    (skyrim_dir / "SkyrimSE.exe").write_bytes(b"fake-skyrim-exe")

    local_cfg = MagicMock()
    local_cfg.mo2_root = str(split_topology["install"])
    local_cfg.skyrim_path = str(skyrim_dir)

    gateway = MagicMock()
    session = MagicMock()

    with (
        patch("sky_claw.local.discovery.scanner.detect_skyrim_edition") as mock_detect_ed,
        patch("sky_claw.local.discovery.scanner.read_skyrim_version") as mock_read_ver,
    ):
        mock_detect_ed.return_value = MagicMock(value="SE")
        mock_read_ver.return_value = "1.5.97"

        res_raw = await setup_tools(
            tools_installer=tools_installer,
            install_dir=split_topology["install"],
            local_cfg=local_cfg,
            config_path=split_topology["install"] / "cfg.toml",
            downloader=MagicMock(),
            tools=["ngio", "community_shaders"],
            gateway=gateway,
            session=session,
            mods_dir=split_topology["mods"],
        )

        res = json.loads(res_raw)
        assert res["ngio"]["success"] is True
        assert res["community_shaders"]["success"] is True

        tools_installer.ensure_ngio.assert_called_once()
        ngio_target = tools_installer.ensure_ngio.call_args[0][0]
        assert ngio_target == split_topology["mods"]
        assert ngio_target != split_topology["install_mods"]

        tools_installer.ensure_community_shaders.assert_called_once()
        cs_target = tools_installer.ensure_community_shaders.call_args[0][0]
        assert cs_target == split_topology["mods"]
        assert cs_target != split_topology["install_mods"]


@pytest.mark.asyncio
async def test_asynctoolregistry_setup_tools_wires_mo2_mods_dir(split_topology: dict[str, pathlib.Path]) -> None:
    """AsyncToolRegistry delega en setup_tools pasando self._mo2.mods_dir."""
    validator = PathValidator(roots=[split_topology["install"], split_topology["data"], split_topology["mods"]])
    mo2 = MO2Controller(
        install_root=split_topology["install"],
        data_root=split_topology["data"],
        mods_dir=split_topology["mods"],
        path_validator=validator,
    )
    tools_installer = MagicMock()
    gateway = MagicMock()

    registry = AsyncToolRegistry(
        registry=None,
        mo2=mo2,
        sync_engine=None,
        tools_installer=tools_installer,
        install_dir=split_topology["install"],
        gateway=gateway,
    )

    with patch("sky_claw.app.agent.tools.setup_tools", new_callable=AsyncMock) as mock_setup:
        mock_setup.return_value = "{}"
        await registry.execute("setup_tools", {"tools": ["loot"]})
        mock_setup.assert_called_once()
        kwargs = mock_setup.call_args.kwargs
        assert kwargs["mods_dir"] == split_topology["mods"]
        assert kwargs["mods_dir"] != split_topology["install_mods"]

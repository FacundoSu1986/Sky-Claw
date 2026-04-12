"""Tests for sky_claw.mo2.vfs – async modlist.txt parser."""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.mo2.vfs import MO2Controller
from sky_claw.security.path_validator import PathValidator, PathViolation


@pytest.fixture()
def mo2_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal MO2 directory structure with a modlist.txt."""
    profile_dir = tmp_path / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    modlist = profile_dir / "modlist.txt"
    modlist.write_text(
        "+SKSE-30150-v2-2-6\n"
        "-DisabledMod-9999\n"
        "*Separator\n"
        "# comment line\n"
        "\n"
        "+SkyUI-3863-v5-2\n"
        "+AnotherMod-12345\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def controller(mo2_root: pathlib.Path) -> MO2Controller:
    validator = PathValidator(roots=[mo2_root])
    return MO2Controller(mo2_root, path_validator=validator)


class TestReadModlist:
    @pytest.mark.asyncio
    async def test_yields_enabled_and_disabled(self, controller: MO2Controller) -> None:
        entries = [(name, enabled) async for name, enabled in controller.read_modlist()]
        assert ("SKSE-30150-v2-2-6", True) in entries
        assert ("DisabledMod-9999", False) in entries

    @pytest.mark.asyncio
    async def test_skips_separators_and_comments(
        self, controller: MO2Controller
    ) -> None:
        names = [name async for name, _ in controller.read_modlist()]
        assert "Separator" not in names
        assert "# comment line" not in names

    @pytest.mark.asyncio
    async def test_correct_count(self, controller: MO2Controller) -> None:
        entries = [e async for e in controller.read_modlist()]
        assert len(entries) == 4

    @pytest.mark.asyncio
    async def test_empty_modlist(self, tmp_path: pathlib.Path) -> None:
        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text("", encoding="utf-8")
        validator = PathValidator(roots=[tmp_path])
        ctrl = MO2Controller(tmp_path, path_validator=validator)
        entries = [e async for e in ctrl.read_modlist()]
        assert entries == []

    @pytest.mark.asyncio
    async def test_path_outside_sandbox_rejected(self, tmp_path: pathlib.Path) -> None:
        other = tmp_path / "other"
        other.mkdir()
        profile_dir = other / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text("+SomeMod\n", encoding="utf-8")
        validator = PathValidator(roots=[tmp_path / "sandbox"])
        ctrl = MO2Controller(other, path_validator=validator)
        with pytest.raises(PathViolation):
            async for _ in ctrl.read_modlist():
                pass

    @pytest.mark.asyncio
    async def test_skips_lines_with_bad_prefix(self, tmp_path: pathlib.Path) -> None:
        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text(
            "!BadPrefix\n+GoodMod-100\n", encoding="utf-8"
        )
        validator = PathValidator(roots=[tmp_path])
        ctrl = MO2Controller(tmp_path, path_validator=validator)
        entries = [e async for e in ctrl.read_modlist()]
        assert len(entries) == 1
        assert entries[0][0] == "GoodMod-100"


class TestRemoveMod:
    @pytest.mark.asyncio
    async def test_remove_existing_enabled(
        self, controller: MO2Controller, mo2_root: pathlib.Path
    ) -> None:
        await controller.remove_mod_from_modlist("SKSE-30150-v2-2-6")
        entries = [name async for name, _ in controller.read_modlist()]
        assert "SKSE-30150-v2-2-6" not in entries
        assert "SkyUI-3863-v5-2" in entries

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, controller: MO2Controller) -> None:
        # Should not raise
        await controller.remove_mod_from_modlist("ImaginaryMod")


class TestToggleMod:
    @pytest.mark.asyncio
    async def test_disable_mod(self, controller: MO2Controller) -> None:
        await controller.toggle_mod_in_modlist("SKSE-30150-v2-2-6", enable=False)
        entries = dict(
            [(name, status) async for name, status in controller.read_modlist()]
        )
        assert entries.get("SKSE-30150-v2-2-6") is False

    @pytest.mark.asyncio
    async def test_enable_mod(self, controller: MO2Controller) -> None:
        await controller.toggle_mod_in_modlist("DisabledMod-9999", enable=True)
        entries = dict(
            [(name, status) async for name, status in controller.read_modlist()]
        )
        assert entries.get("DisabledMod-9999") is True


class TestDeleteModFiles:
    @pytest.mark.asyncio
    async def test_delete_existing_dir(
        self, controller: MO2Controller, mo2_root: pathlib.Path
    ) -> None:
        mod_dir = mo2_root / "mods" / "SomeMod"
        mod_dir.mkdir(parents=True)
        (mod_dir / "plugin.esp").write_text("dummy")

        await controller.delete_mod_files("SomeMod")
        assert not mod_dir.exists()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_dir(self, controller: MO2Controller) -> None:
        # Should not raise
        await controller.delete_mod_files("GhostMod")


class TestGameControl:
    @pytest.mark.asyncio
    async def test_launch_game(
        self, controller: MO2Controller, mo2_root: pathlib.Path, monkeypatch
    ) -> None:
        mo2_exe = mo2_root / "ModOrganizer.exe"
        mo2_exe.write_text("dummy")

        from unittest.mock import AsyncMock

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_create = AsyncMock(return_value=mock_proc)

        import asyncio

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create)

        result = await controller.launch_game("Default")
        assert result["status"] == "launched"
        assert result["pid"] == 12345
        mock_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_game(self, controller: MO2Controller, monkeypatch) -> None:
        import psutil
        from unittest.mock import MagicMock

        mock_proc_1 = MagicMock()
        mock_proc_1.info = {"name": "SkyrimSE.exe"}
        mock_proc_1.kill = MagicMock()

        mock_proc_2 = MagicMock()
        mock_proc_2.info = {"name": "chrome.exe"}

        monkeypatch.setattr(
            psutil, "process_iter", lambda x: [mock_proc_1, mock_proc_2]
        )

        result = await controller.close_game()
        assert result["status"] == "closed"
        assert "SkyrimSE.exe" in result["killed_processes"]
        mock_proc_1.kill.assert_called_once()

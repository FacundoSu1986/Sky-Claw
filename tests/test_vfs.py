"""Tests for sky_claw.local.mo2.vfs -- async modlist.txt parser."""

from __future__ import annotations

import asyncio
import pathlib
from typing import NamedTuple

import pytest

from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError
from sky_claw.local.mo2.vfs import MO2Controller


class BomFixture(NamedTuple):
    controller: MO2Controller
    modlist: pathlib.Path


@pytest.fixture()
def mo2_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal MO2 directory structure with a modlist.txt."""
    profile_dir = tmp_path / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    modlist = profile_dir / "modlist.txt"
    modlist.write_text(
        "+SKSE-30150-v2-2-6\n-DisabledMod-9999\n*Separator\n# comment line\n\n+SkyUI-3863-v5-2\n+AnotherMod-12345\n",
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
    async def test_skips_separators_and_comments(self, controller: MO2Controller) -> None:
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
        with pytest.raises(PathViolationError):
            async for _ in ctrl.read_modlist():
                pass

    @pytest.mark.asyncio
    async def test_skips_lines_with_bad_prefix(self, tmp_path: pathlib.Path) -> None:
        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text("!BadPrefix\n+GoodMod-100\n", encoding="utf-8")
        validator = PathValidator(roots=[tmp_path])
        ctrl = MO2Controller(tmp_path, path_validator=validator)
        entries = [e async for e in ctrl.read_modlist()]
        assert len(entries) == 1
        assert entries[0][0] == "GoodMod-100"


class TestRemoveMod:
    @pytest.mark.asyncio
    async def test_remove_existing_enabled(self, controller: MO2Controller, mo2_root: pathlib.Path) -> None:
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
        entries = dict([(name, status) async for name, status in controller.read_modlist()])
        assert entries.get("SKSE-30150-v2-2-6") is False

    @pytest.mark.asyncio
    async def test_enable_mod(self, controller: MO2Controller) -> None:
        await controller.toggle_mod_in_modlist("DisabledMod-9999", enable=True)
        entries = dict([(name, status) async for name, status in controller.read_modlist()])
        assert entries.get("DisabledMod-9999") is True


class TestDeleteModFiles:
    @pytest.mark.asyncio
    async def test_delete_existing_dir(self, controller: MO2Controller, mo2_root: pathlib.Path) -> None:
        mod_dir = mo2_root / "mods" / "SomeMod"
        mod_dir.mkdir(parents=True)
        (mod_dir / "plugin.esp").write_text("dummy")

        await controller.delete_mod_files("SomeMod")
        assert not mod_dir.exists()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_dir(self, controller: MO2Controller) -> None:
        # Should not raise
        await controller.delete_mod_files("GhostMod")


class TestBomPreservation:
    """C-01 -- modlist.txt rewrites must retain the UTF-8 BOM required by MO2."""

    @pytest.fixture()
    def bom_controller(self, tmp_path: pathlib.Path) -> BomFixture:
        """MO2 root whose modlist.txt starts with a real UTF-8 BOM."""
        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        modlist = profile_dir / "modlist.txt"
        # Write with BOM explicitly so the fixture models a real MO2 file.
        modlist.write_bytes(b"\xef\xbb\xbf+RealMod-1\n-DisabledMod-2\n")
        validator = PathValidator(roots=[tmp_path])
        controller = MO2Controller(tmp_path, path_validator=validator)
        return BomFixture(controller=controller, modlist=modlist)

    @pytest.mark.asyncio
    async def test_remove_mod_preserves_bom(self, bom_controller: BomFixture) -> None:
        await bom_controller.controller.remove_mod_from_modlist("RealMod-1")
        raw = bom_controller.modlist.read_bytes()
        assert raw[:3] == b"\xef\xbb\xbf", "UTF-8 BOM must be present after remove_mod_from_modlist rewrite"

    @pytest.mark.asyncio
    async def test_toggle_mod_preserves_bom(self, bom_controller: BomFixture) -> None:
        await bom_controller.controller.toggle_mod_in_modlist("DisabledMod-2", enable=True)
        raw = bom_controller.modlist.read_bytes()
        assert raw[:3] == b"\xef\xbb\xbf", "UTF-8 BOM must be present after toggle_mod_in_modlist rewrite"

    @pytest.mark.asyncio
    async def test_add_mod_preserves_bom(self, bom_controller: BomFixture) -> None:
        """add_mod must rewrite atomically (BOM-preserving), like remove/toggle (obs #192)."""
        await bom_controller.controller.add_mod_to_modlist("AddedMod-3")
        raw = bom_controller.modlist.read_bytes()
        assert raw[:3] == b"\xef\xbb\xbf", "UTF-8 BOM must be present after add_mod_to_modlist rewrite"
        text = raw.decode("utf-8-sig")
        # Existing entries preserved and the new one appended.
        assert "+RealMod-1" in text
        assert "-DisabledMod-2" in text
        assert "+AddedMod-3" in text

    @pytest.mark.asyncio
    async def test_add_mod_creates_file_with_bom(self, tmp_path: pathlib.Path) -> None:
        """A freshly created modlist must carry the UTF-8 BOM (append mode would omit it)."""
        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        validator = PathValidator(roots=[tmp_path])
        controller = MO2Controller(tmp_path, path_validator=validator)

        await controller.add_mod_to_modlist("NewMod-1")

        raw = (profile_dir / "modlist.txt").read_bytes()
        assert raw[:3] == b"\xef\xbb\xbf", "add_mod must write the UTF-8 BOM like its siblings"
        assert b"+NewMod-1" in raw


class TestGameControl:
    @pytest.mark.asyncio
    async def test_launch_game(self, controller: MO2Controller, mo2_root: pathlib.Path, monkeypatch) -> None:
        mo2_exe = mo2_root / "ModOrganizer.exe"
        mo2_exe.write_text("dummy")

        from unittest.mock import AsyncMock

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_create = AsyncMock(return_value=mock_proc)

        async def _fake_verify(pid: int) -> None:
            return

        monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create)
        monkeypatch.setattr("sky_claw.local.mo2.vfs._verify_pid_alive", _fake_verify)

        result = await controller.launch_game("Default")
        assert result["status"] == "launched"
        assert result["pid"] == 12345
        mock_create.assert_awaited_once()
        # §1.1: el PID quedó trackeado para que close_game pueda terminarlo.
        assert controller._launched_pids == {12345}

    @pytest.mark.asyncio
    async def test_launch_game_trackea_pid_antes_de_verificar(
        self, controller: MO2Controller, mo2_root: pathlib.Path, monkeypatch
    ) -> None:
        """§1.1: el PID se registra ANTES de _verify_pid_alive, así una muerte/
        cancelación durante la verificación no deja un MO2 fuera de close_game."""
        (mo2_root / "ModOrganizer.exe").write_text("dummy")
        from unittest.mock import AsyncMock

        mock_proc = AsyncMock()
        mock_proc.pid = 777
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

        visto: dict[str, bool] = {}

        async def _verify_espia(pid: int) -> None:
            # En el momento de verificar, el PID ya debe estar trackeado.
            visto["trackeado"] = pid in controller._launched_pids

        monkeypatch.setattr("sky_claw.local.mo2.vfs._verify_pid_alive", _verify_espia)

        await controller.launch_game("Default")

        assert visto["trackeado"] is True

    @pytest.mark.asyncio
    async def test_launch_game_timeout_no_deja_pid_trackeado(
        self, controller: MO2Controller, mo2_root: pathlib.Path, monkeypatch
    ) -> None:
        """Si el spawn no aparece (TimeoutError), el PID se descarta (no queda
        trackeado) y el proceso defunto se mata."""
        (mo2_root / "ModOrganizer.exe").write_text("dummy")
        from unittest.mock import AsyncMock, MagicMock

        mock_proc = AsyncMock()
        mock_proc.pid = 888
        mock_proc.kill = MagicMock()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_proc))

        async def _verify_cuelga(pid: int) -> None:
            raise TimeoutError

        monkeypatch.setattr("sky_claw.local.mo2.vfs._verify_pid_alive", _verify_cuelga)

        from sky_claw.local.mo2.vfs import GameLaunchTimeoutError

        with pytest.raises(GameLaunchTimeoutError):
            await controller.launch_game("Default")

        assert controller._launched_pids == set()
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_game_kills_only_launched_tree(self, controller: MO2Controller, monkeypatch) -> None:
        """M-8: close_game mata sólo el árbol del PID lanzado, no procesos homónimos ajenos."""
        from unittest.mock import MagicMock

        # Árbol del juego lanzado: MO2 (root) → SKSE (hijo).
        child = MagicMock()
        child.pid = 555
        child.name = MagicMock(return_value="skse64_loader.exe")
        child.kill = MagicMock()

        root = MagicMock()
        root.pid = 12345
        root.name = MagicMock(return_value="ModOrganizer.exe")
        root.kill = MagicMock()
        root.children = MagicMock(return_value=[child])

        # Un SkyrimSE.exe AJENO (otra instancia del usuario) NO debe tocarse.
        def _fake_process(pid):
            assert pid == 12345, "close_game debe consultar sólo el PID lanzado"
            return root

        monkeypatch.setattr("psutil.Process", _fake_process)

        controller._launched_pids = {12345}  # simular un launch previo
        result = await controller.close_game()

        assert result["status"] == "closed"
        root.kill.assert_called_once()
        child.kill.assert_called_once()
        # killed_processes contiene el árbol lanzado, identificado por PID.
        assert any("12345" in k for k in result["killed_processes"])
        assert any("555" in k for k in result["killed_processes"])
        # tras cerrar, los PIDs se limpian.
        assert controller._launched_pids == set()

    @pytest.mark.asyncio
    async def test_close_game_mata_todos_los_pids_lanzados(self, controller: MO2Controller, monkeypatch) -> None:
        """§1.2: si se relanzó MO2 sin cerrar, close_game mata TODOS los árboles
        trackeados, no solo el último — un MO2 viejo vivo no queda huérfano."""
        from unittest.mock import MagicMock

        procesos = {}
        for pid in (100, 200):
            p = MagicMock()
            p.pid = pid
            p.name = MagicMock(return_value="ModOrganizer.exe")
            p.kill = MagicMock()
            p.children = MagicMock(return_value=[])
            procesos[pid] = p

        monkeypatch.setattr("psutil.Process", lambda pid: procesos[pid])

        controller._launched_pids = {100, 200}
        result = await controller.close_game()

        assert result["status"] == "closed"
        procesos[100].kill.assert_called_once()
        procesos[200].kill.assert_called_once()
        assert any("100" in k for k in result["killed_processes"])
        assert any("200" in k for k in result["killed_processes"])
        assert controller._launched_pids == set()

    @pytest.mark.asyncio
    async def test_close_game_noop_without_launch(self, controller: MO2Controller, monkeypatch) -> None:
        """M-8: sin un juego lanzado por esta instancia, close_game es no-op (no mata por nombre)."""
        from unittest.mock import MagicMock

        called = MagicMock()
        monkeypatch.setattr("psutil.Process", called)

        result = await controller.close_game()
        assert result == {"status": "closed", "killed_processes": []}
        called.assert_not_called()


# ---------------------------------------------------------------------------
# TestHostileComponentInputs
# ---------------------------------------------------------------------------


class TestHostileComponentInputs:
    """assert_safe_component must fire before any filesystem I/O in MO2Controller."""

    @pytest.fixture()
    def ctrl(self, tmp_path: pathlib.Path) -> MO2Controller:
        validator = PathValidator(roots=[tmp_path])
        return MO2Controller(tmp_path, path_validator=validator)

    # --- add_mod_to_modlist ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_name",
        [
            "evil\nfake_entry",  # newline injection
            "../escape",  # traversal
            "mods/subdir",  # forward slash
            "mods\\subdir",  # backslash
        ],
    )
    async def test_add_mod_hostile_mod_name_raises(self, ctrl: MO2Controller, bad_name: str) -> None:
        with pytest.raises(PathViolationError):
            await ctrl.add_mod_to_modlist(bad_name)

    @pytest.mark.asyncio
    async def test_add_mod_hostile_profile_raises(self, ctrl: MO2Controller) -> None:
        with pytest.raises(PathViolationError):
            await ctrl.add_mod_to_modlist("LegitMod", profile="../escape")

    # --- delete_mod_files ---

    @pytest.mark.asyncio
    async def test_delete_mod_separator_raises(self, ctrl: MO2Controller) -> None:
        with pytest.raises(PathViolationError):
            await ctrl.delete_mod_files("../../secret")

    # --- remove_mod_from_modlist ---

    @pytest.mark.asyncio
    async def test_remove_mod_newline_injection_raises(self, ctrl: MO2Controller) -> None:
        with pytest.raises(PathViolationError):
            await ctrl.remove_mod_from_modlist("legit\nevil")

    # --- toggle_mod_in_modlist ---

    @pytest.mark.asyncio
    async def test_toggle_mod_separator_raises(self, ctrl: MO2Controller) -> None:
        with pytest.raises(PathViolationError):
            await ctrl.toggle_mod_in_modlist("mods/evil")

    # --- read_modlist ---

    @pytest.mark.asyncio
    async def test_read_modlist_hostile_profile_raises(self, ctrl: MO2Controller) -> None:
        with pytest.raises(PathViolationError):
            # read_modlist is an async generator — must iterate to trigger
            async for _ in ctrl.read_modlist(profile="../evil"):
                pass

    # --- smoke test: legitimate inputs still work ---

    @pytest.mark.asyncio
    async def test_legitimate_inputs_still_work(self, ctrl: MO2Controller, tmp_path: pathlib.Path) -> None:
        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text("+ExistingMod\n", encoding="utf-8")

        await ctrl.add_mod_to_modlist("NewMod", profile="Default")

        content = (profile_dir / "modlist.txt").read_text(encoding="utf-8")
        assert "+NewMod" in content

"""Tests for sky_claw.loot (cli, parser, masterlist)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.agent.tools.system_tools import run_loot_sort
from sky_claw.antigravity.db.locks import DistributedLockManager
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.cli import (
    LOOTConfig,
    LOOTNotFoundError,
    LOOTRunner,
    LOOTTimeoutError,
)
from sky_claw.local.loot.masterlist import MasterlistDownloader
from sky_claw.local.loot.parser import LOOTOutputParser, LOOTResult
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID

if TYPE_CHECKING:
    import pathlib

# ------------------------------------------------------------------
# LOOTOutputParser
# ------------------------------------------------------------------


class TestLOOTOutputParser:
    def test_parse_sorted_plugins(self) -> None:
        stdout = "Sorting plugins...\n  1. Skyrim.esm\n  2. Update.esm\n  3. Dawnguard.esm\n  4. Requiem.esp\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert result.sorted_plugins == [
            "Skyrim.esm",
            "Update.esm",
            "Dawnguard.esm",
            "Requiem.esp",
        ]
        assert result.success is True

    def test_parse_warnings(self) -> None:
        stdout = "Warning: Requiem.esp has unresolved masters\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert len(result.warnings) == 1
        assert "Requiem.esp" in result.warnings[0]

    def test_parse_errors_in_stderr(self) -> None:
        result = LOOTOutputParser.parse(stdout="", stderr="Error: Game path not found\n", return_code=1)
        assert len(result.errors) == 1
        assert result.success is False

    def test_parse_empty_output(self) -> None:
        result = LOOTOutputParser.parse(stdout="", stderr="", return_code=0)
        assert result.sorted_plugins == []
        assert result.warnings == []
        assert result.errors == []
        # Golden Master: success requires plugins > 0
        assert result.success is False

    def test_parse_mixed_output(self) -> None:
        stdout = (
            "  1. Skyrim.esm\n"
            "Warning: Missing master for SomePlugin.esp\n"
            "  2. SomePlugin.esp\n"
            "Error: Critical conflict detected\n"
        )
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=1)
        assert result.sorted_plugins == ["Skyrim.esm", "SomePlugin.esp"]
        assert len(result.warnings) == 1
        assert len(result.errors) == 1
        assert result.success is False

    def test_parse_esl_and_esm(self) -> None:
        stdout = "  1. ccBGSSSE001-Fish.esl\n  2. Unofficial Skyrim Special Edition Patch.esp\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert len(result.sorted_plugins) == 2

    def test_parse_ansi_escapes(self) -> None:
        """Golden Master: ANSI escape sequences are stripped before parsing."""
        stdout = "\x1b[32m  1. Skyrim.esm\x1b[0m\n\x1b[33m  2. Requiem.esp\x1b[0m\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert result.sorted_plugins == ["Skyrim.esm", "Requiem.esp"]
        assert result.success is True

    def test_parse_native_crash(self) -> None:
        """Golden Master: native crash signature injects CRITICAL error."""
        stdout = "  1. Skyrim.esm\nFATAL ERROR: access violation at 0xDEADBEEF\n"
        result = LOOTOutputParser.parse(stdout=stdout, stderr="", return_code=1)
        assert result.success is False
        assert len(result.errors) >= 1
        assert "CRITICAL" in result.errors[0]
        assert "crashed natively" in result.errors[0]


# ------------------------------------------------------------------
# LOOTRunner
# ------------------------------------------------------------------


class TestLOOTRunner:
    def _make_config(self, tmp_path: pathlib.Path) -> LOOTConfig:
        loot_exe = tmp_path / "loot.exe"
        loot_exe.touch()
        game_path = tmp_path / "Skyrim"
        game_path.mkdir()
        return LOOTConfig(loot_exe=loot_exe, game_path=game_path, timeout=5)

    @pytest.mark.asyncio
    async def test_loot_not_found_raises(self, tmp_path: pathlib.Path) -> None:
        config = LOOTConfig(
            loot_exe=tmp_path / "nonexistent.exe",
            game_path=tmp_path,
        )
        runner = LOOTRunner(config)
        with pytest.raises(LOOTNotFoundError, match="not found"):
            await runner.sort()

    @pytest.mark.asyncio
    async def test_sort_success(self, tmp_path: pathlib.Path) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(
                b"  1. Skyrim.esm\n  2. Update.esm\n",
                b"",
            )
        )
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            result = await runner.sort()

        assert result.success is True
        assert result.sorted_plugins == ["Skyrim.esm", "Update.esm"]
        mock_proc.kill.assert_not_called()
        mock_proc.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sort_timeout_mata_reapea_y_traduce_error(self, tmp_path: pathlib.Path) -> None:
        base = self._make_config(tmp_path)
        config = LOOTConfig(
            loot_exe=base.loot_exe,
            game_path=base.game_path,
            timeout=0,
        )
        runner = LOOTRunner(config)

        async def communicate_bloqueado() -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=communicate_bloqueado)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=-9)

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            pytest.raises(LOOTTimeoutError, match="timed out after 0s"),
        ):
            await runner.sort()

        mock_proc.kill.assert_called_once_with()
        mock_proc.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_loot_timeout_kills_only_this_process(self, tmp_path: pathlib.Path) -> None:
        """Timeout mata sólo el proceso representado por proc; nunca usa taskkill host-wide."""
        base = self._make_config(tmp_path)
        config = LOOTConfig(
            loot_exe=base.loot_exe,
            game_path=base.game_path,
            timeout=0,
        )
        runner = LOOTRunner(config)

        async def communicate_bloqueado() -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=communicate_bloqueado)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=-9)
        mock_proc.pid = 4242

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            pytest.raises(LOOTTimeoutError, match="timed out after 0s"),
        ):
            await runner.sort()

        mock_proc.kill.assert_called_once_with()
        mock_proc.wait.assert_awaited_once_with()
        import sky_claw.local.loot.cli as _cli

        assert not hasattr(_cli, "subprocess")

    @pytest.mark.asyncio
    async def test_sort_cancelado_mata_reapea_y_repropaga(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)
        communicate_iniciado = asyncio.Event()

        async def communicate_bloqueado() -> tuple[bytes, bytes]:
            communicate_iniciado.set()
            await asyncio.Event().wait()
            return b"", b""

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=communicate_bloqueado)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=-9)

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            task = asyncio.create_task(runner.sort())
            await asyncio.wait_for(communicate_iniciado.wait(), timeout=1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        mock_proc.kill.assert_called_once_with()
        mock_proc.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_sort_error_de_pipe_tolera_proceso_terminado_y_reapea(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=OSError("pipe rota"))
        mock_proc.returncode = None
        mock_proc.kill = MagicMock(side_effect=ProcessLookupError)
        mock_proc.wait = AsyncMock(return_value=-9)

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            pytest.raises(OSError, match="pipe rota"),
        ):
            await runner.sort()

        mock_proc.kill.assert_called_once_with()
        mock_proc.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_sort_errores_de_cleanup_no_ocultan_error_primario(
        self,
        tmp_path: pathlib.Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)
        error_primario = OSError("pipe primaria")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=error_primario)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock(side_effect=PermissionError("kill secundario"))
        mock_proc.wait = AsyncMock(side_effect=OSError("wait secundario"))

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            caplog.at_level(logging.WARNING, logger="sky_claw.local.loot.cli"),
            pytest.raises(OSError) as capturada,
        ):
            await runner.sort()

        assert capturada.value is error_primario
        mock_proc.kill.assert_called_once_with()
        mock_proc.wait.assert_awaited_once_with()
        assert "kill secundario" in caplog.text
        assert "wait secundario" in caplog.text

    @pytest.mark.asyncio
    async def test_sort_reap_vencido_advierte_proceso_no_reapeado(
        self,
        tmp_path: pathlib.Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Si el reap no termina dentro del deadline, se advierte que LOOT
        puede seguir vivo — sin ocultar el error primario.

        Antes, ``_reap_process`` retornaba en silencio al vencer el deadline,
        así que el path de cleanup "parecía exitoso" justo en el escenario que
        fue agregado para diagnosticar (proceso posiblemente sin reapear).
        """
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)
        error_primario = OSError("pipe rota")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=error_primario)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        # El reap vence: proc.wait() nunca termina dentro del deadline.
        mock_proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.pid = 4242

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
            caplog.at_level(logging.WARNING, logger="sky_claw.local.loot.cli"),
            pytest.raises(OSError) as capturada,
        ):
            await runner.sort()

        # La excepción primaria se preserva intacta.
        assert capturada.value is error_primario
        mock_proc.kill.assert_called_once_with()
        # Y queda constancia de que el proceso pudo no ser reapeado.
        assert "no terminó" in caplog.text
        assert "sin reapear" in caplog.text
        assert "4242" in caplog.text

    @pytest.mark.asyncio
    async def test_sort_doble_cancelacion_espera_reap_y_preserva_primaria(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)
        communicate_iniciado = asyncio.Event()
        wait_iniciado = asyncio.Event()
        liberar_wait = asyncio.Event()
        wait_finalizado = asyncio.Event()

        async def communicate_bloqueado() -> tuple[bytes, bytes]:
            communicate_iniciado.set()
            await asyncio.Event().wait()
            return b"", b""

        async def wait_bloqueado() -> int:
            wait_iniciado.set()
            await liberar_wait.wait()
            wait_finalizado.set()
            return -9

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=communicate_bloqueado)
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(side_effect=wait_bloqueado)

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            task = asyncio.create_task(runner.sort())
            await asyncio.wait_for(communicate_iniciado.wait(), timeout=1.0)
            task.cancel("cancelación primaria")
            await asyncio.wait_for(wait_iniciado.wait(), timeout=1.0)
            mock_proc.kill.assert_called_once_with()

            task.cancel("cancelación secundaria")
            await asyncio.sleep(0)
            termino_antes_del_reap = task.done()
            liberar_wait.set()

            with pytest.raises(asyncio.CancelledError) as capturada:
                await task

        assert termino_antes_del_reap is False
        assert wait_finalizado.is_set()
        assert capturada.value.args == ("cancelación primaria",)
        mock_proc.wait.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_sort_with_errors(self, tmp_path: pathlib.Path) -> None:
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"Error: Game path invalid\n"))
        mock_proc.returncode = 1
        mock_proc.kill = MagicMock()

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            result = await runner.sort()

        assert result.success is False
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_sort_appends_update_masterlist_flag(self, tmp_path: pathlib.Path) -> None:
        """update_masterlist=True appends --update-masterlist to the LOOT args."""
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)
        captured: dict[str, list[str]] = {}

        async def fake_exec(*args: str, **_kwargs: object) -> AsyncMock:
            captured["args"] = list(args)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"  1. Skyrim.esm\n", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            await runner.sort(update_masterlist=True)

        assert "--update-masterlist" in captured["args"]

    @pytest.mark.asyncio
    async def test_sort_omits_update_masterlist_by_default(self, tmp_path: pathlib.Path) -> None:
        """By default (e.g. dry-run preview) the masterlist flag is NOT passed."""
        config = self._make_config(tmp_path)
        runner = LOOTRunner(config)
        captured: dict[str, list[str]] = {}

        async def fake_exec(*args: str, **_kwargs: object) -> AsyncMock:
            captured["args"] = list(args)
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"  1. Skyrim.esm\n", b""))
            proc.returncode = 0
            proc.kill = MagicMock()
            return proc

        with (
            patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(config.game_path)),
        ):
            await runner.sort()

        assert "--update-masterlist" not in captured["args"]


# ------------------------------------------------------------------
# MasterlistDownloader
# ------------------------------------------------------------------


class TestMasterlistDownloader:
    @pytest.mark.asyncio
    async def test_uses_cache_when_valid(self, tmp_path: pathlib.Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cached = cache_dir / "masterlist.yaml"
        cached.write_text("cached content")

        mock_gw = MagicMock()
        downloader = MasterlistDownloader(gateway=mock_gw, cache_dir=cache_dir, ttl=3600)
        session = MagicMock()
        path = await downloader.get(session)

        assert path == cached
        mock_gw.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_downloads_when_cache_expired(self, tmp_path: pathlib.Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cached = cache_dir / "masterlist.yaml"
        cached.write_text("old content")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(cached, (old_time, old_time))

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"new content")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_gw = MagicMock()
        mock_gw.request = AsyncMock(return_value=mock_resp)

        downloader = MasterlistDownloader(gateway=mock_gw, cache_dir=cache_dir, ttl=3600)
        session = MagicMock()
        path = await downloader.get(session)

        assert path == cached
        assert cached.read_text() == "new content"
        mock_gw.request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_downloads_when_no_cache(self, tmp_path: pathlib.Path) -> None:
        cache_dir = tmp_path / "cache"

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"masterlist yaml")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_gw = MagicMock()
        mock_gw.request = AsyncMock(return_value=mock_resp)

        downloader = MasterlistDownloader(gateway=mock_gw, cache_dir=cache_dir, ttl=3600)
        session = MagicMock()
        path = await downloader.get(session)

        assert path.exists()
        assert path.read_text() == "masterlist yaml"

    @pytest.mark.asyncio
    async def test_raises_on_download_failure(self, tmp_path: pathlib.Path) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.text = AsyncMock(return_value="Not Found")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_gw = MagicMock()
        mock_gw.request = AsyncMock(return_value=mock_resp)

        downloader = MasterlistDownloader(gateway=mock_gw, cache_dir=tmp_path, ttl=3600)
        session = MagicMock()
        with pytest.raises(RuntimeError, match="404"):
            await downloader.get(session)


# ------------------------------------------------------------------
# Tool integration (run_loot_sort uses LOOTRunner)
# ------------------------------------------------------------------


class TestLootSortTool:
    @pytest.mark.asyncio
    async def test_loot_sort_no_runner_configured(self, tmp_path: pathlib.Path) -> None:
        """When no LOOTRunner is provided, tool returns error JSON."""
        from sky_claw.antigravity.agent.tools import AsyncToolRegistry
        from sky_claw.antigravity.db.async_registry import AsyncModRegistry
        from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine
        from sky_claw.antigravity.security.path_validator import PathValidator
        from sky_claw.local.mo2.vfs import MO2Controller

        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text("+TestMod-100\n")
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, validator)

        registry = AsyncModRegistry(db_path=tmp_path / "test.db")
        await registry.open()
        try:
            sync = SyncEngine(mo2, MagicMock(), registry)
            tool_reg = AsyncToolRegistry(
                registry=registry,
                mo2=mo2,
                sync_engine=sync,
                loot_runner=None,
            )
            import json

            result = json.loads(await tool_reg.execute("run_loot_sort", {"profile": "Default"}))
            assert "error" in result
            assert "not configured" in result["error"] or "not found" in result["error"]
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_loot_sort_with_runner(self, tmp_path: pathlib.Path) -> None:
        """When LOOTRunner is provided, tool delegates to it."""
        from sky_claw.antigravity.agent.tools import AsyncToolRegistry
        from sky_claw.antigravity.db.async_registry import AsyncModRegistry
        from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine
        from sky_claw.antigravity.security.path_validator import PathValidator
        from sky_claw.local.mo2.vfs import MO2Controller

        profile_dir = tmp_path / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_text("+TestMod-100\n")
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, validator)

        registry = AsyncModRegistry(db_path=tmp_path / "test.db")
        await registry.open()
        try:
            sync = SyncEngine(mo2, MagicMock(), registry)

            mock_runner = MagicMock()
            mock_runner.sort = AsyncMock(
                return_value=LOOTResult(
                    return_code=0,
                    sorted_plugins=["Skyrim.esm", "Requiem.esp"],
                    warnings=["Some warning"],
                    errors=[],
                )
            )

            tool_reg = AsyncToolRegistry(
                registry=registry,
                mo2=mo2,
                sync_engine=sync,
                loot_runner=mock_runner,
            )
            import json

            result = json.loads(await tool_reg.execute("run_loot_sort", {"profile": "Default"}))
            assert result["success"] is True
            assert result["sorted_plugins"] == ["Skyrim.esm", "Requiem.esp"]
            assert result["warnings"] == ["Some warning"]
            mock_runner.sort.assert_awaited_once()
        finally:
            await registry.close()


# ------------------------------------------------------------------
# run_loot_sort distributed-lock coverage (audit #190 — live agent path)
# ------------------------------------------------------------------


class TestRunLootSortLock:
    """The agent tool serializes on the shared load-order lock when wired.

    Closes the gap where the live Telegram / /api/chat LOOT path bypassed the
    cross-process SnapshotTransactionLock that the GUI orchestrator uses.
    """

    async def _managers(self, tmp_path: pathlib.Path) -> tuple[DistributedLockManager, FileSnapshotManager]:
        lm = DistributedLockManager(
            tmp_path / "locks.db",
            default_ttl=5.0,
            max_retries=2,
            backoff_base=0.05,
            backoff_max=0.2,
        )
        await lm.initialize()
        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        sm = FileSnapshotManager(snapshot_dir=snap_dir)
        await sm.initialize()
        return lm, sm

    @pytest.mark.asyncio
    async def test_acquires_and_releases_load_order_lock(self, tmp_path: pathlib.Path) -> None:
        lm, sm = await self._managers(tmp_path)
        try:
            runner = MagicMock()
            runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"]))
            result = json.loads(
                await run_loot_sort(MagicMock(), runner, None, "Default", lock_manager=lm, snapshot_manager=sm)
            )
            assert result["success"] is True
            assert result["sorted_plugins"] == ["Skyrim.esm"]
            runner.sort.assert_awaited_once()
            # Lock released after the sort completes.
            assert await lm.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None
        finally:
            await lm.close()

    @pytest.mark.asyncio
    async def test_serializes_when_load_order_lock_held(self, tmp_path: pathlib.Path) -> None:
        """A LOOT sort held elsewhere (e.g. the orchestrator/preview) blocks this one."""
        lm, sm = await self._managers(tmp_path)
        try:
            await lm.acquire_lock(LOAD_ORDER_RESOURCE_ID, "orchestrator", ttl=30.0)
            runner = MagicMock()
            runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=["x.esp"]))
            result = json.loads(
                await run_loot_sort(MagicMock(), runner, None, "Default", lock_manager=lm, snapshot_manager=sm)
            )
            assert result["success"] is False
            assert "error" in result
            runner.sort.assert_not_awaited()  # serialized — never ran under contention
        finally:
            await lm.close()

    @pytest.mark.asyncio
    async def test_legacy_path_without_lock_manager(self, tmp_path: pathlib.Path) -> None:
        """Back-compat: with no lock manager wired, the tool sorts directly (no lock)."""
        runner = MagicMock()
        runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"]))
        result = json.loads(await run_loot_sort(MagicMock(), runner, None, "Default"))
        assert result["success"] is True
        runner.sort.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_locked_path_returns_json_on_unexpected_error(self, tmp_path: pathlib.Path) -> None:
        """An unexpected subprocess error (e.g. OSError) is returned as JSON, not raised.

        Preserves the AsyncToolRegistry.execute() contract on the locked live path
        (matches the legacy direct path's catch-all behavior).
        """
        lm, sm = await self._managers(tmp_path)
        try:
            runner = MagicMock()
            runner.sort = AsyncMock(side_effect=OSError("loot.exe is not executable"))
            result = json.loads(
                await run_loot_sort(MagicMock(), runner, None, "Default", lock_manager=lm, snapshot_manager=sm)
            )
            assert "error" in result
            # Lock must still be released after the failure.
            assert await lm.get_lock_info(LOAD_ORDER_RESOURCE_ID) is None
        finally:
            await lm.close()

    @pytest.mark.asyncio
    async def test_emits_action_manifest_when_journal_wired(self, tmp_path: pathlib.Path) -> None:
        """Con journal cableado, el path del agente persiste la "caja negra de
        vuelo" del sort (T-26 end-to-end).

        Cierra el ítem del path del agente que quedó abierto en #243: la emisión
        del ActionManifest estaba gateada por ``self._journal is not None`` en el
        servicio, pero el path del agente (Telegram / /api/chat) nunca le pasaba
        journal, así que ahí la caja negra era un no-op (review Codex #243 P1).
        """
        from sky_claw.antigravity.db.journal import OperationJournal
        from sky_claw.antigravity.orchestrator.preview.action_manifest import ActionManifest
        from sky_claw.local.tools.loot_service import LootSortingService

        lm, sm = await self._managers(tmp_path)
        journal = OperationJournal(tmp_path / "journal.db")
        await journal.open()
        try:
            runner = MagicMock()
            runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"]))
            result = json.loads(
                await run_loot_sort(
                    MagicMock(),
                    runner,
                    None,
                    "Default",
                    lock_manager=lm,
                    snapshot_manager=sm,
                    journal=journal,
                )
            )
            assert result["success"] is True
            # El manifiesto quedó persistido en el journal del path del agente.
            # Se selecciona explícito: el FlightReport (T-28) comparte la TX y
            # también trae ritual_id — se discrimina por su clave ``kind``.
            op = await journal.get_last_operation(agent_id=LootSortingService.AGENT_ID)
            assert op is not None, "el path del agente debe persistir la caja negra"
            (ultima,) = await journal.list_recent_transactions(limit=1)
            entries = await journal.get_operations_by_transaction(ultima.transaction_id)
            manifiestos = [
                e.metadata
                for e in entries
                if e.metadata and e.metadata.get("ritual_id") and e.metadata.get("kind") != "flight_report"
            ]
            assert len(manifiestos) == 1, "el path del agente debe persistir el ActionManifest"
            manifest = ActionManifest.model_validate(manifiestos[0])
            assert manifest.tool == "LOOT"
            # Y el informe post-vuelo (T-28) también quedó en la misma TX.
            assert any(e.metadata and e.metadata.get("kind") == "flight_report" for e in entries)
        finally:
            await journal.close()
            await lm.close()

    @pytest.mark.asyncio
    async def test_journal_opcional_preserva_compat(self, tmp_path: pathlib.Path) -> None:
        """Sin journal, el path del agente ordena igual que antes (back-compat)."""
        lm, sm = await self._managers(tmp_path)
        try:
            runner = MagicMock()
            runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, sorted_plugins=["Skyrim.esm"]))
            result = json.loads(
                await run_loot_sort(MagicMock(), runner, None, "Default", lock_manager=lm, snapshot_manager=sm)
            )
            assert result["success"] is True
            runner.sort.assert_awaited_once()
        finally:
            await lm.close()

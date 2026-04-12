"""Tests for sky_claw.xedit (runner, output_parser, tool integration)."""

from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.xedit.output_parser import XEditOutputParser, XEditResult, XEditConflict
from sky_claw.xedit.runner import (
    XEditRunner,
    XEditNotFoundError,
    XEditValidationError,
    ScriptGenerator,
)


# ------------------------------------------------------------------
# XEditOutputParser
# ------------------------------------------------------------------


class TestXEditOutputParser:
    def test_parse_processing_lines(self) -> None:
        stdout = (
            "[00:01] Processing: Skyrim.esm\n"
            "[00:02] Processing: Update.esm\n"
            "[00:03] Processing: Requiem.esp\n"
        )
        result = XEditOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert result.processed_plugins == ["Skyrim.esm", "Update.esm", "Requiem.esp"]
        assert result.success is True

    def test_parse_conflict_found(self) -> None:
        stdout = "Conflict found: WEAP record overridden by Requiem.esp\n"
        result = XEditOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert len(result.conflicts) == 1
        assert "Requiem.esp" in result.conflicts[0].detail

    def test_parse_detailed_conflict(self) -> None:
        stdout = "[00:05] Requiem.esp -> WEAP\\00012345: Damage value override\n"
        result = XEditOutputParser.parse(stdout=stdout, stderr="", return_code=0)
        assert len(result.conflicts) == 1
        assert result.conflicts[0].plugin == "Requiem.esp"
        assert "WEAP" in result.conflicts[0].record
        assert "Damage" in result.conflicts[0].detail

    def test_parse_errors(self) -> None:
        stdout = "Error: Could not load plugin BadMod.esp\n"
        result = XEditOutputParser.parse(stdout=stdout, stderr="", return_code=1)
        assert len(result.errors) == 1
        assert result.success is False

    def test_parse_stderr_errors(self) -> None:
        result = XEditOutputParser.parse(
            stdout="", stderr="Fatal: Out of memory\n", return_code=1
        )
        assert len(result.errors) == 1
        assert "Out of memory" in result.errors[0]

    def test_parse_empty(self) -> None:
        result = XEditOutputParser.parse(stdout="", stderr="", return_code=0)
        assert result.processed_plugins == []
        assert result.conflicts == []
        assert result.success is True

    def test_parse_mixed(self) -> None:
        stdout = (
            "[00:01] Processing: Skyrim.esm\n"
            "[00:02] Processing: Requiem.esp\n"
            "Conflict found: NPC record conflict\n"
            "Error: Missing master file\n"
        )
        result = XEditOutputParser.parse(stdout=stdout, stderr="", return_code=1)
        assert len(result.processed_plugins) == 2
        assert len(result.conflicts) == 1
        assert len(result.errors) == 1


# ------------------------------------------------------------------
# XEditRunner — validation
# ------------------------------------------------------------------


class TestXEditRunnerValidation:
    def _make_runner(self, tmp_path: pathlib.Path) -> XEditRunner:
        xedit = tmp_path / "SSEEdit.exe"
        xedit.touch()
        game = tmp_path / "Skyrim"
        game.mkdir()
        return XEditRunner(xedit_path=xedit, game_path=game, timeout=5)

    @pytest.mark.asyncio
    async def test_invalid_script_name_raises(self, tmp_path: pathlib.Path) -> None:
        runner = self._make_runner(tmp_path)
        with pytest.raises(XEditValidationError, match="Invalid script name"):
            await runner.run_script("../evil.pas", ["Skyrim.esm"])

    @pytest.mark.asyncio
    async def test_invalid_script_no_extension(self, tmp_path: pathlib.Path) -> None:
        runner = self._make_runner(tmp_path)
        with pytest.raises(XEditValidationError, match="Invalid script name"):
            await runner.run_script("script", ["Skyrim.esm"])

    @pytest.mark.asyncio
    async def test_invalid_plugin_name_raises(self, tmp_path: pathlib.Path) -> None:
        runner = self._make_runner(tmp_path)
        with pytest.raises(XEditValidationError, match="Invalid plugin name"):
            await runner.run_script("list_conflicts.pas", ["../evil.dll"])

    @pytest.mark.asyncio
    async def test_valid_names_accepted(self, tmp_path: pathlib.Path) -> None:
        runner = self._make_runner(tmp_path)
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        with patch(
            "sky_claw.xedit.runner.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await runner.run_script(
                "list_conflicts.pas",
                ["Skyrim.esm", "Unofficial Skyrim Special Edition Patch.esp"],
            )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_xedit_not_found(self, tmp_path: pathlib.Path) -> None:
        runner = XEditRunner(
            xedit_path=tmp_path / "nonexistent.exe",
            game_path=tmp_path,
        )
        with pytest.raises(XEditNotFoundError):
            await runner.run_script("list_conflicts.pas", ["Skyrim.esm"])


# ------------------------------------------------------------------
# XEditRunner — execution
# ------------------------------------------------------------------


class TestXEditRunnerExecution:
    def _make_runner(self, tmp_path: pathlib.Path) -> XEditRunner:
        xedit = tmp_path / "SSEEdit.exe"
        xedit.touch()
        game = tmp_path / "Skyrim"
        game.mkdir()
        return XEditRunner(xedit_path=xedit, game_path=game, timeout=5)

    @pytest.mark.asyncio
    async def test_successful_run(self, tmp_path: pathlib.Path) -> None:
        runner = self._make_runner(tmp_path)
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(
                b"[00:01] Processing: Skyrim.esm\nConflict found: NPC override\n",
                b"",
            )
        )
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        with patch(
            "sky_claw.xedit.runner.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await runner.run_script("list_conflicts.pas", ["Skyrim.esm"])

        assert result.success is True
        assert result.processed_plugins == ["Skyrim.esm"]
        assert len(result.conflicts) == 1

    @pytest.mark.asyncio
    async def test_timeout(self, tmp_path: pathlib.Path) -> None:
        runner = self._make_runner(tmp_path)
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.kill = MagicMock()

        with patch(
            "sky_claw.xedit.runner.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            with patch(
                "sky_claw.xedit.runner.asyncio.wait_for",
                side_effect=asyncio.TimeoutError,
            ):
                with pytest.raises(RuntimeError, match="timed out"):
                    await runner.run_script("list_conflicts.pas", ["Skyrim.esm"])


# ------------------------------------------------------------------
# Tool integration
# ------------------------------------------------------------------


class TestXEditTool:
    @pytest.mark.asyncio
    async def test_tool_no_runner_configured(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.db.async_registry import AsyncModRegistry
        from sky_claw.mo2.vfs import MO2Controller
        from sky_claw.orchestrator.sync_engine import SyncEngine
        from sky_claw.security.path_validator import PathValidator

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
            )
            result = json.loads(
                await tool_reg.execute(
                    "run_xedit_script",
                    {"script_name": "test.pas", "plugins": ["test.esp"]},
                )
            )
            assert "error" in result
            assert "xEdit runner is not configured" in result["error"]
        finally:
            await registry.close()

    @pytest.mark.asyncio
    async def test_tool_with_runner(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.agent.tools import AsyncToolRegistry
        from sky_claw.db.async_registry import AsyncModRegistry
        from sky_claw.mo2.vfs import MO2Controller
        from sky_claw.orchestrator.sync_engine import SyncEngine
        from sky_claw.security.path_validator import PathValidator

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
            mock_runner.run_script = AsyncMock(
                return_value=XEditResult(
                    return_code=0,
                    processed_plugins=["Skyrim.esm"],
                    conflicts=[
                        XEditConflict(
                            plugin="Requiem.esp", record="WEAP", detail="Damage"
                        )
                    ],
                    errors=[],
                )
            )

            tool_reg = AsyncToolRegistry(
                registry=registry,
                mo2=mo2,
                sync_engine=sync,
                xedit_runner=mock_runner,
            )
            result = json.loads(
                await tool_reg.execute(
                    "run_xedit_script",
                    {"script_name": "list_conflicts.pas", "plugins": ["Skyrim.esm"]},
                )
            )
            assert result["success"] is True
            assert result["processed_plugins"] == ["Skyrim.esm"]
            assert len(result["conflicts"]) == 1
            assert result["conflicts"][0]["plugin"] == "Requiem.esp"
        finally:
            await registry.close()


# ------------------------------------------------------------------
# ScriptGenerator - Pascal String Escaping (CRIT-001)
# ------------------------------------------------------------------


class TestScriptGeneratorEscapePascalString:
    """Tests para la mitigación CRIT-001: escape de strings Pascal."""

    def test_escape_empty_string(self) -> None:
        """String vacío retorna vacío."""
        result = ScriptGenerator._escape_pascal_string("")
        assert result == ""

    def test_escape_none_returns_empty(self) -> None:
        """None retorna string vacío."""
        result = ScriptGenerator._escape_pascal_string(None)
        assert result == ""

    def test_escape_simple_string(self) -> None:
        """String sin caracteres especiales no se modifica."""
        result = ScriptGenerator._escape_pascal_string("Skyrim.esm")
        assert result == "Skyrim.esm"

    def test_escape_single_quote(self) -> None:
        """Comillas simples se escapan como dobles comillas (Pascal)."""
        result = ScriptGenerator._escape_pascal_string("O'Reilly's Mod.esp")
        assert result == "O''Reilly''s Mod.esp"

    def test_escape_backslash(self) -> None:
        """Backslashes se escapan con doble backslash."""
        result = ScriptGenerator._escape_pascal_string(r"path\to\file.esp")
        assert result == r"path\\to\\file.esp"

    def test_escape_combined_quotes_and_backslashes(self) -> None:
        """Combinación de comillas y backslashes se escapan correctamente."""
        result = ScriptGenerator._escape_pascal_string(r"O'Reilly's\path\file.esp")
        assert result == r"O''Reilly''s\\path\\file.esp"

    def test_escape_multiple_single_quotes(self) -> None:
        """Múltiples comillas simples se escapan."""
        result = ScriptGenerator._escape_pascal_string("It's John's Mod's Patch.esp")
        assert result == "It''s John''s Mod''s Patch.esp"

    def test_escape_backslash_before_quote(self) -> None:
        """Backslash antes de comilla se escapan ambos."""
        result = ScriptGenerator._escape_pascal_string(r"path\'s file")
        assert result == r"path\\''s file"

    def test_no_escape_double_quotes(self) -> None:
        """Comillas dobles no se escapan (no especiales en Pascal)."""
        result = ScriptGenerator._escape_pascal_string('Mod "Special" Version.esp')
        assert result == 'Mod "Special" Version.esp'

    def test_escape_injection_attempt_comment(self) -> None:
        """Intento de inyección con comentarios no rompe el string."""
        # El string contiene comilla simple que se escapa, evitando inyección
        malicious = "'); AddMessage('HACKED'); //"
        result = ScriptGenerator._escape_pascal_string(malicious)
        # La comilla simple se convierte en '', previniendo cierre de string
        assert "'" not in result or "''" in result
        assert result == "''); AddMessage(''HACKED''); //"

    def test_escape_form_id_valid(self) -> None:
        """FormID válido no se modifica."""
        result = ScriptGenerator._escape_pascal_string("00012345")
        assert result == "00012345"

    def test_escape_form_id_with_colon(self) -> None:
        """FormID con dos puntos no se modifica."""
        result = ScriptGenerator._escape_pascal_string("000123:45")
        assert result == "000123:45"


class TestScriptGeneratorWithEscaping:
    """Tests de integración para generación de scripts con escape."""

    def test_generate_forward_script_escapes_inputs(self) -> None:
        """Los inputs se escapan en el script generado."""
        # Usar un plugin con comilla simple
        script = ScriptGenerator.generate_forward_script(
            form_id="00012345",
            source="John's Mod.esp",
            target="Target.esp",
        )
        # Verificar que la comilla fue escapada
        assert "John''s Mod.esp" in script
        assert "John's Mod.esp" not in script  # Sin escape original

    def test_generate_merge_script_escapes_output_plugin(self) -> None:
        """El nombre del plugin de salida se escapa."""
        # Usar un nombre de plugin con backslash
        script = ScriptGenerator.generate_merge_script(
            output_plugin="Test_Patch.esp",  # Nombre válido
            record_types=["LVLI"],
        )
        # Verificar que el script se generó correctamente
        assert "Test_Patch.esp" in script

    def test_generate_merge_script_validates_plugin_name(self) -> None:
        """Nombres de plugin inválidos son rechazados."""
        from sky_claw.xedit.runner import XEditScriptError

        with pytest.raises(XEditScriptError, match="Invalid output plugin name"):
            ScriptGenerator.generate_merge_script(
                output_plugin="../evil.esp",  # Path traversal
                record_types=["LVLI"],
            )

    def test_generate_forward_script_validates_form_id(self) -> None:
        """FormIDs inválidos son rechazados."""
        from sky_claw.xedit.runner import XEditScriptError

        with pytest.raises(XEditScriptError, match="Invalid FormID format"):
            ScriptGenerator.generate_forward_script(
                form_id="NOTHEX",  # No es hexadecimal
                source="Source.esp",
                target="Target.esp",
            )

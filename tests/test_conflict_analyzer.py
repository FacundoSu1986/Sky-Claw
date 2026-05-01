"""Tests for ESP record-level conflict analysis."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.xedit.conflict_analyzer import (
    ConflictAnalyzer,
    ConflictReport,
    PluginConflictPair,
    RecordConflict,
    parse_conflict_lines,
    parse_summary_line,
)
from sky_claw.local.xedit.output_parser import XEditOutputParser, XEditResult

if TYPE_CHECKING:
    import pathlib

# ---------------------------------------------------------------------------
# Sample xEdit output
# ---------------------------------------------------------------------------

SAMPLE_OUTPUT = """\
[00:01] Processing: Skyrim.esm
[00:01] Processing: Requiem.esp
[00:01] Processing: USSEP.esp
CONFLICT|00014BB3|MQ201Alduin|NPC_|Requiem.esp|Skyrim.esm
CONFLICT|00039F26|WhiterunDragonsreach|CELL|USSEP.esp|Skyrim.esm,Requiem.esp
CONFLICT|0001396B|IronSword|WEAP|Requiem.esp|Skyrim.esm
CONFLICT|000A1234|SomeTexture|TXST|USSEP.esp|Skyrim.esm
SUMMARY|total_conflicts=4|critical=1|minor=1
"""

EMPTY_OUTPUT = """\
[00:01] Processing: Skyrim.esm
SUMMARY|total_conflicts=0|critical=0|minor=0
"""

MALFORMED_OUTPUT = """\
CONFLICT|bad line missing fields
CONFLICT|00014BB3|Good|NPC_|Winner.esp|Loser.esp
some random log line
CONFLICT|toofewfields|only
"""


# ---------------------------------------------------------------------------
# parse_conflict_lines
# ---------------------------------------------------------------------------


class TestParseConflictLines:
    def test_parses_valid_conflicts(self) -> None:
        conflicts = parse_conflict_lines(SAMPLE_OUTPUT)
        assert len(conflicts) == 4

        npc = conflicts[0]
        assert npc.form_id == "00014BB3"
        assert npc.editor_id == "MQ201Alduin"
        assert npc.record_type == "NPC_"
        assert npc.winner == "Requiem.esp"
        assert npc.losers == ["Skyrim.esm"]

    def test_parses_multiple_losers(self) -> None:
        conflicts = parse_conflict_lines(SAMPLE_OUTPUT)
        cell = conflicts[1]
        assert cell.losers == ["Skyrim.esm", "Requiem.esp"]

    def test_empty_output_returns_empty(self) -> None:
        assert parse_conflict_lines(EMPTY_OUTPUT) == []

    def test_malformed_lines_skipped(self) -> None:
        conflicts = parse_conflict_lines(MALFORMED_OUTPUT)
        assert len(conflicts) == 1
        assert conflicts[0].form_id == "00014BB3"

    def test_completely_empty_string(self) -> None:
        assert parse_conflict_lines("") == []


class TestParseSummaryLine:
    def test_parses_summary(self) -> None:
        result = parse_summary_line(SAMPLE_OUTPUT)
        assert result == {"total_conflicts": 4, "critical": 1, "minor": 1}

    def test_no_summary_returns_empty(self) -> None:
        assert parse_summary_line("no summary here") == {}


# ---------------------------------------------------------------------------
# XEditOutputParser.parse_conflict_report
# ---------------------------------------------------------------------------


class TestOutputParserConflictReport:
    def test_delegates_to_conflict_analyzer(self) -> None:
        result = XEditOutputParser.parse_conflict_report(SAMPLE_OUTPUT)
        assert len(result) == 4
        assert result[0]["form_id"] == "00014BB3"
        assert result[0]["winner"] == "Requiem.esp"


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------


class TestSeverityClassification:
    def test_npc_is_critical(self) -> None:
        analyzer = ConflictAnalyzer()
        assert analyzer._classify("NPC_") == "critical"

    def test_qust_is_critical(self) -> None:
        analyzer = ConflictAnalyzer()
        assert analyzer._classify("QUST") == "critical"

    def test_cell_is_warning(self) -> None:
        analyzer = ConflictAnalyzer()
        assert analyzer._classify("CELL") == "warning"

    def test_weap_is_warning(self) -> None:
        analyzer = ConflictAnalyzer()
        assert analyzer._classify("WEAP") == "warning"

    def test_txst_is_info(self) -> None:
        analyzer = ConflictAnalyzer()
        assert analyzer._classify("TXST") == "info"

    def test_unknown_type_is_info(self) -> None:
        analyzer = ConflictAnalyzer()
        assert analyzer._classify("ZZZZ") == "info"

    def test_custom_critical_types(self) -> None:
        analyzer = ConflictAnalyzer(critical_types=frozenset({"WEAP"}))
        assert analyzer._classify("WEAP") == "critical"
        # NPC_ is no longer critical with custom set.
        assert analyzer._classify("NPC_") != "critical"


# ---------------------------------------------------------------------------
# ConflictAnalyzer.analyze
# ---------------------------------------------------------------------------


class TestAnalyze:
    @pytest.mark.asyncio
    async def test_full_analysis(self) -> None:
        """End-to-end: mock xEdit runner → parsed + classified report."""
        mock_runner = MagicMock()
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(
                return_code=0,
                raw_stdout=SAMPLE_OUTPUT,
                raw_stderr="",
            )
        )

        analyzer = ConflictAnalyzer()
        report = await analyzer.analyze(
            ["Skyrim.esm", "Requiem.esp", "USSEP.esp"],
            mock_runner,
        )

        assert report.total_conflicts == 4
        assert report.critical_conflicts == 1
        assert len(report.plugin_pairs) > 0
        assert "conflict" in report.summary.lower()

        mock_runner.run_script.assert_awaited_once_with(
            "list_all_conflicts.pas",
            ["Skyrim.esm", "Requiem.esp", "USSEP.esp"],
        )

    @pytest.mark.asyncio
    async def test_no_conflicts_report(self) -> None:
        mock_runner = MagicMock()
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(
                return_code=0,
                raw_stdout=EMPTY_OUTPUT,
                raw_stderr="",
            )
        )

        analyzer = ConflictAnalyzer()
        report = await analyzer.analyze(["Skyrim.esm"], mock_runner)

        assert report.total_conflicts == 0
        assert report.critical_conflicts == 0
        assert report.plugin_pairs == []
        assert "no record-level conflicts" in report.summary.lower()


# ---------------------------------------------------------------------------
# Plugin pair grouping
# ---------------------------------------------------------------------------


class TestPluginPairGrouping:
    @pytest.mark.asyncio
    async def test_groups_by_pair(self) -> None:
        mock_runner = MagicMock()
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(
                return_code=0,
                raw_stdout=SAMPLE_OUTPUT,
                raw_stderr="",
            )
        )

        analyzer = ConflictAnalyzer()
        report = await analyzer.analyze(
            ["Skyrim.esm", "Requiem.esp", "USSEP.esp"],
            mock_runner,
        )

        pair_keys = {(p.plugin_a, p.plugin_b) for p in report.plugin_pairs}
        # Requiem.esp vs Skyrim.esm should be a pair.
        assert ("Requiem.esp", "Skyrim.esm") in pair_keys or (
            "Skyrim.esm",
            "Requiem.esp",
        ) in pair_keys

    @pytest.mark.asyncio
    async def test_sorted_by_conflict_count(self) -> None:
        mock_runner = MagicMock()
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(
                return_code=0,
                raw_stdout=SAMPLE_OUTPUT,
                raw_stderr="",
            )
        )

        analyzer = ConflictAnalyzer()
        report = await analyzer.analyze(
            ["Skyrim.esm", "Requiem.esp", "USSEP.esp"],
            mock_runner,
        )

        counts = [len(p.conflicts) for p in report.plugin_pairs]
        assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# suggest_resolution
# ---------------------------------------------------------------------------


class TestSuggestResolution:
    def test_npc_conflict_suggests_patch(self) -> None:
        analyzer = ConflictAnalyzer()
        report = ConflictReport(
            total_conflicts=1,
            critical_conflicts=1,
            plugin_pairs=[
                PluginConflictPair(
                    plugin_a="A.esp",
                    plugin_b="B.esp",
                    conflicts=[
                        RecordConflict(
                            form_id="001",
                            editor_id="SomeNPC",
                            record_type="NPC_",
                            winner="A.esp",
                            losers=["B.esp"],
                            severity="critical",
                        ),
                    ],
                ),
            ],
        )

        suggestions = analyzer.suggest_resolution(report)
        assert any("NPC" in s for s in suggestions)
        assert any("patch" in s.lower() for s in suggestions)

    def test_cell_conflict_suggests_reorder(self) -> None:
        analyzer = ConflictAnalyzer()
        report = ConflictReport(
            total_conflicts=1,
            critical_conflicts=0,
            plugin_pairs=[
                PluginConflictPair(
                    plugin_a="A.esp",
                    plugin_b="B.esp",
                    conflicts=[
                        RecordConflict(
                            form_id="002",
                            editor_id="SomeCell",
                            record_type="CELL",
                            winner="A.esp",
                            losers=["B.esp"],
                            severity="warning",
                        ),
                    ],
                ),
            ],
        )

        suggestions = analyzer.suggest_resolution(report)
        assert any("reorder" in s.lower() or "load order" in s.lower() for s in suggestions)

    def test_leveled_list_suggests_bashed_patch(self) -> None:
        analyzer = ConflictAnalyzer()
        report = ConflictReport(
            total_conflicts=1,
            critical_conflicts=0,
            plugin_pairs=[
                PluginConflictPair(
                    plugin_a="A.esp",
                    plugin_b="B.esp",
                    conflicts=[
                        RecordConflict(
                            form_id="003",
                            editor_id="LItemSword",
                            record_type="LVLI",
                            winner="A.esp",
                            losers=["B.esp"],
                            severity="warning",
                        ),
                    ],
                ),
            ],
        )

        suggestions = analyzer.suggest_resolution(report)
        assert any("bashed" in s.lower() or "smashed" in s.lower() for s in suggestions)

    def test_no_conflicts_clean_message(self) -> None:
        analyzer = ConflictAnalyzer()
        report = ConflictReport(total_conflicts=0, critical_conflicts=0)
        suggestions = analyzer.suggest_resolution(report)
        assert any("clean" in s.lower() or "no conflict" in s.lower() for s in suggestions)

    def test_heavy_pair_suggests_dedicated_patch(self) -> None:
        analyzer = ConflictAnalyzer()
        conflicts = [
            RecordConflict(
                form_id=f"00{i:04X}",
                editor_id=f"Record{i}",
                record_type="WEAP",
                winner="BigMod.esp",
                losers=["OtherMod.esp"],
                severity="warning",
            )
            for i in range(15)
        ]
        report = ConflictReport(
            total_conflicts=15,
            critical_conflicts=0,
            plugin_pairs=[
                PluginConflictPair(
                    plugin_a="BigMod.esp",
                    plugin_b="OtherMod.esp",
                    conflicts=conflicts,
                ),
            ],
        )
        suggestions = analyzer.suggest_resolution(report)
        assert any("BigMod.esp" in s and "OtherMod.esp" in s for s in suggestions)


# ---------------------------------------------------------------------------
# ConflictReport.to_dict
# ---------------------------------------------------------------------------


class TestConflictReportToDict:
    def test_serializes_correctly(self) -> None:
        report = ConflictReport(
            total_conflicts=1,
            critical_conflicts=1,
            plugin_pairs=[
                PluginConflictPair(
                    plugin_a="A.esp",
                    plugin_b="B.esp",
                    conflicts=[
                        RecordConflict(
                            form_id="001",
                            editor_id="TestNPC",
                            record_type="NPC_",
                            winner="A.esp",
                            losers=["B.esp"],
                            severity="critical",
                        ),
                    ],
                ),
            ],
            summary="Test summary",
        )
        d = report.to_dict()
        assert d["total_conflicts"] == 1
        assert d["critical_conflicts"] == 1
        assert len(d["plugin_pairs"]) == 1
        assert d["plugin_pairs"][0]["critical_count"] == 1
        # Verify it's JSON-serializable.
        json.dumps(d)


# ---------------------------------------------------------------------------
# analyze_esp_conflicts tool (via AsyncToolRegistry)
# ---------------------------------------------------------------------------


class TestAnalyzeEspConflictsTool:
    @pytest.mark.asyncio
    async def test_xedit_not_configured_returns_error(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.antigravity.agent.tools import AsyncToolRegistry
        from sky_claw.antigravity.db.async_registry import AsyncModRegistry
        from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine
        from sky_claw.antigravity.scraper.masterlist import MasterlistClient
        from sky_claw.antigravity.security.network_gateway import EgressPolicy, NetworkGateway
        from sky_claw.antigravity.security.path_validator import PathValidator
        from sky_claw.local.mo2.vfs import MO2Controller

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("+Skyrim.esm\n+Requiem.esp\n", encoding="utf-8")

        db = AsyncModRegistry(db_path=tmp_path / "test.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        registry = AsyncToolRegistry(
            registry=db,
            mo2=mo2,
            sync_engine=sync_engine,
            # xedit_runner=None — not configured
        )

        result_str = await registry.execute("analyze_esp_conflicts", {"profile": "Default"})
        result = json.loads(result_str)
        assert "error" in result
        assert "setup_tools" in result["error"].lower() or "xedit" in result["error"].lower()

        await db.close()

    @pytest.mark.asyncio
    async def test_with_specific_plugins(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.antigravity.agent.tools import AsyncToolRegistry
        from sky_claw.antigravity.db.async_registry import AsyncModRegistry
        from sky_claw.antigravity.orchestrator.sync_engine import SyncEngine
        from sky_claw.antigravity.scraper.masterlist import MasterlistClient
        from sky_claw.antigravity.security.network_gateway import EgressPolicy, NetworkGateway
        from sky_claw.antigravity.security.path_validator import PathValidator
        from sky_claw.local.mo2.vfs import MO2Controller
        from sky_claw.local.xedit.runner import XEditRunner

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

        db = AsyncModRegistry(db_path=tmp_path / "test.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        mock_runner = MagicMock(spec=XEditRunner)
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(
                return_code=0,
                raw_stdout=SAMPLE_OUTPUT,
                raw_stderr="",
            )
        )

        registry = AsyncToolRegistry(
            registry=db,
            mo2=mo2,
            sync_engine=sync_engine,
            xedit_runner=mock_runner,
        )

        result_str = await registry.execute(
            "analyze_esp_conflicts",
            {"profile": "Default", "plugins": ["Skyrim.esm", "Requiem.esp"]},
        )
        result = json.loads(result_str)
        assert result["total_conflicts"] == 4
        assert result["critical_conflicts"] == 1
        assert "suggestions" in result
        assert len(result["suggestions"]) > 0

        await db.close()


# ---------------------------------------------------------------------------
# validate_load_order_limit — full vs light pool distinction
# ---------------------------------------------------------------------------


class TestValidateLoadOrderLimit:
    """Tests for validate_load_order_limit covering both full and light pools."""

    @pytest.fixture
    def analyzer(self):
        """ConflictAnalyzer instance for testing."""
        return ConflictAnalyzer()

    def test_full_plugins_within_limit(self, analyzer):
        plugins = [f"mod{i:03d}.esp" for i in range(254)]
        analyzer.validate_load_order_limit(plugins)  # Must not raise

    def test_full_plugins_exceed_limit(self, analyzer):
        plugins = [f"mod{i:03d}.esp" for i in range(255)]
        with pytest.raises(RuntimeError, match="Full plugin limit exceeded"):
            analyzer.validate_load_order_limit(plugins)

    def test_light_plugins_within_limit(self, analyzer):
        plugins = [f"mod{i:04d}.esl" for i in range(4096)]
        analyzer.validate_load_order_limit(plugins)  # Must not raise

    def test_light_plugins_exceed_limit(self, analyzer):
        plugins = [f"mod{i:04d}.esl" for i in range(4097)]
        with pytest.raises(RuntimeError, match="Light plugin limit exceeded"):
            analyzer.validate_load_order_limit(plugins)

    def test_mixed_plugins_within_both_limits(self, analyzer):
        """250 full + 100 light = legal, should not raise."""
        full = [f"mod{i:03d}.esp" for i in range(250)]
        light = [f"mod{i:03d}.esl" for i in range(100)]
        analyzer.validate_load_order_limit(full + light)  # Must not raise

    def test_full_exceeds_but_light_within(self, analyzer):
        """Only full pool violation raises."""
        full = [f"mod{i:03d}.esp" for i in range(255)]
        light = [f"mod{i:03d}.esl" for i in range(50)]
        with pytest.raises(RuntimeError, match="Full plugin limit exceeded"):
            analyzer.validate_load_order_limit(full + light)

    def test_esl_not_counted_in_full_pool(self, analyzer):
        """254 .esp + any .esl should not exceed full limit."""
        full = [f"mod{i:03d}.esp" for i in range(254)]
        light = [f"mod{i:03d}.esl" for i in range(100)]
        analyzer.validate_load_order_limit(full + light)  # Must not raise

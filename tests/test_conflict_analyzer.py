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

    @pytest.mark.asyncio
    async def test_falla_de_xedit_lanza_en_vez_de_reporte_vacio(self) -> None:
        """Exit != 0 no debe parecer "sin conflictos": ocultaría disputas reales (Codex #226)."""
        mock_runner = MagicMock()
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(
                return_code=1,
                raw_stdout="",
                raw_stderr="Fatal: could not load master",
            )
        )

        analyzer = ConflictAnalyzer()
        with pytest.raises(RuntimeError, match="xEdit falló"):
            await analyzer.analyze(["Skyrim.esm"], mock_runner)

    @pytest.mark.asyncio
    async def test_errores_parseados_tambien_lanzan(self) -> None:
        # success es False también si hay errors, aunque el exit code sea 0.
        mock_runner = MagicMock()
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(
                return_code=0,
                errors=["Error: record malformado"],
                raw_stdout="",
                raw_stderr="",
            )
        )

        analyzer = ConflictAnalyzer()
        with pytest.raises(RuntimeError, match="record malformado"):
            await analyzer.analyze(["Skyrim.esm"], mock_runner)


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


# ---------------------------------------------------------------------------
# T-19a: flag_states por override (líneas FLAG|FormID|Plugin|FlagName|0/1)
# ---------------------------------------------------------------------------

SPEL_FLAG_OUTPUT = """\
[00:01] Processing: Skyrim.esm
CONFLICT|000AB123|HealSpell|SPEL|Overhaul.esp|Skyrim.esm,MagicFix.esp
FLAG|000AB123|Skyrim.esm|Manual Cost Calc|1
FLAG|000AB123|MagicFix.esp|Manual Cost Calc|1
FLAG|000AB123|Overhaul.esp|Manual Cost Calc|0
SUMMARY|total_conflicts=1|critical=1|minor=0
"""


class TestFlagStatesPorOverride:
    """T-19a: el export por override del flag Manual Cost Calc llega parseado
    al RecordConflict — el dato que la regla de T-19b y el panel de T-29
    necesitan (un override define coste manual y el ganador no lo preserva)."""

    def test_flag_states_se_adjuntan_por_form_id(self) -> None:
        from sky_claw.local.xedit.conflict_analyzer import parse_conflict_lines

        conflicts = parse_conflict_lines(SPEL_FLAG_OUTPUT)

        assert len(conflicts) == 1
        estados = {(f.plugin, f.flag, f.value) for f in conflicts[0].flag_states}
        assert ("Skyrim.esm", "Manual Cost Calc", True) in estados
        assert ("MagicFix.esp", "Manual Cost Calc", True) in estados
        # El escenario de la regla T-19b: el ganador NO preserva el flag.
        assert ("Overhaul.esp", "Manual Cost Calc", False) in estados

    def test_sin_lineas_flag_queda_tupla_vacia(self) -> None:
        """Compat: la salida actual (sin FLAG) produce flag_states == ()."""
        from sky_claw.local.xedit.conflict_analyzer import parse_conflict_lines

        conflicts = parse_conflict_lines(SAMPLE_OUTPUT)

        assert len(conflicts) == 4  # nada del parseo existente cambia
        assert all(c.flag_states == () for c in conflicts)

    def test_orden_de_lineas_es_indistinto(self) -> None:
        """Las FLAG pueden llegar antes que su CONFLICT (parser robusto)."""
        from sky_claw.local.xedit.conflict_analyzer import parse_conflict_lines

        reordenado = (
            "FLAG|000AB123|Overhaul.esp|Manual Cost Calc|0\nCONFLICT|000AB123|HealSpell|SPEL|Overhaul.esp|Skyrim.esm\n"
        )
        conflicts = parse_conflict_lines(reordenado)

        assert len(conflicts) == 1
        assert conflicts[0].flag_states[0].plugin == "Overhaul.esp"
        assert conflicts[0].flag_states[0].value is False

    def test_flags_malformadas_se_saltean(self) -> None:
        """FormID inválido, value fuera de {0,1}, campos de menos O de más →
        skip con warning, sin tumbar el parseo. Los campos de más son
        corrupción (el script controla el formato): parsearlos correría
        plugin/flag/value en silencio (review Copilot #259)."""
        from sky_claw.local.xedit.conflict_analyzer import parse_conflict_lines

        salida = (
            "CONFLICT|000AB123|HealSpell|SPEL|Overhaul.esp|Skyrim.esm\n"
            "FLAG|ZZZNOHEX|Overhaul.esp|Manual Cost Calc|1\n"
            "FLAG|000AB123|Overhaul.esp|Manual Cost Calc|2\n"
            "FLAG|000AB123|solo3campos\n"
            "FLAG|000AB123|Extra.esp|Manual Cost Calc|1|basura\n"
            "FLAG|000AB123|Skyrim.esm|Manual Cost Calc|1\n"
        )
        conflicts = parse_conflict_lines(salida)

        assert len(conflicts[0].flag_states) == 1
        assert conflicts[0].flag_states[0].plugin == "Skyrim.esm"

    def test_flag_de_form_id_sin_conflict_se_ignora(self) -> None:
        from sky_claw.local.xedit.conflict_analyzer import parse_conflict_lines

        salida = (
            "FLAG|0FFFFFFF|Huerfano.esp|Manual Cost Calc|1\nCONFLICT|000AB123|HealSpell|SPEL|Overhaul.esp|Skyrim.esm\n"
        )
        conflicts = parse_conflict_lines(salida)

        assert len(conflicts) == 1
        assert conflicts[0].flag_states == ()

    def test_to_dict_incluye_flag_states_y_es_serializable(self) -> None:
        from sky_claw.local.xedit.conflict_analyzer import parse_conflict_lines

        rc = parse_conflict_lines(SPEL_FLAG_OUTPUT)[0]
        report = ConflictReport(
            total_conflicts=1,
            critical_conflicts=1,
            plugin_pairs=[PluginConflictPair(plugin_a="Overhaul.esp", plugin_b="Skyrim.esm", conflicts=[rc])],
        )

        data = report.to_dict()

        serializado = data["plugin_pairs"][0]["conflicts"][0]["flag_states"]
        assert {"plugin": "Overhaul.esp", "flag": "Manual Cost Calc", "value": False} in serializado
        json.dumps(data)  # sigue siendo JSON-serializable

    def test_output_parser_expone_flag_states(self) -> None:
        """El camino XEditOutputParser.parse_conflict_report también los lleva."""
        dicts = XEditOutputParser.parse_conflict_report(SPEL_FLAG_OUTPUT)

        assert len(dicts) == 1
        assert {"plugin": "Overhaul.esp", "flag": "Manual Cost Calc", "value": False} in dicts[0]["flag_states"]


# ---------------------------------------------------------------------------
# T-19b: alertas de flags en el flujo analyze → to_dict → suggest_resolution
# ---------------------------------------------------------------------------


class TestFlagAlertsEnAnalyze:
    """T-19b end-to-end: el motor de reglas corre dentro de analyze() y la
    alerta explicada llega al reporte, al JSON y a las sugerencias."""

    @staticmethod
    def _runner() -> MagicMock:
        mock_runner = MagicMock()
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(return_code=0, raw_stdout=SPEL_FLAG_OUTPUT, raw_stderr="")
        )
        return mock_runner

    @pytest.mark.asyncio
    async def test_analyze_adjunta_la_alerta_al_conflicto(self) -> None:
        analyzer = ConflictAnalyzer()

        report = await analyzer.analyze(["Skyrim.esm", "MagicFix.esp", "Overhaul.esp"], self._runner())

        conflicto = report.plugin_pairs[0].conflicts[0]
        assert len(conflicto.flag_alerts) == 1
        alerta = conflicto.flag_alerts[0]
        assert alerta.flag == "Manual Cost Calc"
        assert alerta.winner == "Overhaul.esp"
        assert "coste astronómico" in alerta.explanation

    @pytest.mark.asyncio
    async def test_to_dict_incluye_las_alertas(self) -> None:
        analyzer = ConflictAnalyzer()
        report = await analyzer.analyze(["Skyrim.esm"], self._runner())

        data = report.to_dict()

        alertas = data["plugin_pairs"][0]["conflicts"][0]["flag_alerts"]
        assert len(alertas) == 1
        assert alertas[0]["flag"] == "Manual Cost Calc"
        assert alertas[0]["severity"] == "critical"
        assert "coste astronómico" in alertas[0]["explanation"]
        json.dumps(data)  # sigue siendo JSON-serializable

    @pytest.mark.asyncio
    async def test_suggest_resolution_lleva_el_texto_explicativo(self) -> None:
        """La alerta explicada llega al operador como la sugerencia MÁS
        prioritaria (primera de la lista)."""
        analyzer = ConflictAnalyzer()
        report = await analyzer.analyze(["Skyrim.esm"], self._runner())

        sugerencias = analyzer.suggest_resolution(report)

        assert "coste astronómico" in sugerencias[0]
        assert "Manual Cost Calc" in sugerencias[0]

    @pytest.mark.asyncio
    async def test_salida_sin_flags_no_genera_alertas(self) -> None:
        """Compat: la salida actual (sin líneas FLAG) → flag_alerts vacío."""
        mock_runner = MagicMock()
        mock_runner.run_script = AsyncMock(
            return_value=XEditResult(return_code=0, raw_stdout=SAMPLE_OUTPUT, raw_stderr="")
        )
        analyzer = ConflictAnalyzer()

        report = await analyzer.analyze(["Skyrim.esm"], mock_runner)

        for pair in report.plugin_pairs:
            for c in pair.conflicts:
                assert c.flag_alerts == ()


# ---------------------------------------------------------------------------
# T-18: límites full/light con flags reales del header (adiós heurística .esl)
# ---------------------------------------------------------------------------


def _plugin_binario(path, *, flags: int = 0) -> None:
    """Plugin sintético mínimo con esos flags de record TES4 (0x200 = light)."""
    import struct

    hedr = struct.pack("<fiI", 1.7, 0, 0x800)
    subrecords = b"HEDR" + struct.pack("<H", len(hedr)) + hedr
    path.write_bytes(b"TES4" + struct.pack("<IIIIHH", len(subrecords), flags, 0, 0, 44, 0) + subrecords)


class TestValidateLoadOrderLimitConFlagsReales:
    """T-18: con plugin_dirs los pools se cuentan con los flags reales del
    header (vía PluginLimitsChecker), no por extensión. El caso que la
    heurística contaba mal: un ESPFE (.esp con flag ESL) consume slot light."""

    def test_espfe_cuenta_como_light_con_flags_reales(self, tmp_path) -> None:
        """El test rojo del backlog: 255 ESPFE — la heurística por extensión
        los contaría como 255 full (> 254 → lanza); con flags reales son
        light y NO se lanza."""
        plugins_dir = tmp_path / "Data"
        plugins_dir.mkdir()
        nombres = [f"espfe_{i:03}.esp" for i in range(255)]
        for nombre in nombres:
            _plugin_binario(plugins_dir / nombre, flags=0x200)  # ESL-flag real
        analyzer = ConflictAnalyzer()

        with pytest.raises(RuntimeError):
            analyzer.validate_load_order_limit(list(nombres))  # heurística: 255 "full"

        analyzer.validate_load_order_limit(list(nombres), plugin_dirs=[plugins_dir])  # no lanza

    def test_limite_full_real_excedido_lanza(self, tmp_path) -> None:
        plugins_dir = tmp_path / "Data"
        plugins_dir.mkdir()
        nombres = [f"full_{i:03}.esp" for i in range(255)]
        for nombre in nombres:
            _plugin_binario(plugins_dir / nombre, flags=0)  # full de verdad
        analyzer = ConflictAnalyzer()

        with pytest.raises(RuntimeError, match="255/254"):
            analyzer.validate_load_order_limit(list(nombres), plugin_dirs=[plugins_dir])

    def test_esl_corrupto_se_reporta_sin_explotar(self, tmp_path, caplog) -> None:
        """Un .esl con header ilegible no tumba la validación: cae al conteo
        por extensión y queda el warning de 'unreadable' (aceptación T-18)."""
        plugins_dir = tmp_path / "Data"
        plugins_dir.mkdir()
        (plugins_dir / "Roto.esl").write_bytes(b"NO-TES4-BASURA")
        analyzer = ConflictAnalyzer()

        with caplog.at_level("WARNING"):
            analyzer.validate_load_order_limit(["Roto.esl"], plugin_dirs=[plugins_dir])

        assert any("unreadable" in r.message or "ilegible" in r.message for r in caplog.records)

    def test_sin_plugin_dirs_degrada_honesto(self, caplog) -> None:
        """Sin dirs no hay flags reales: heurística de siempre + warning
        explícito de conteo aproximado (no mentir precisión)."""
        analyzer = ConflictAnalyzer()

        with caplog.at_level("WARNING"):
            analyzer.validate_load_order_limit(["A.esp", "B.esl"])

        assert any("aproximado" in r.message for r in caplog.records)

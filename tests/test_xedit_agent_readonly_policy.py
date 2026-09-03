"""Regresiones de la capacidad xEdit read-only expuesta al agente LLM."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import ValidationError

from sky_claw.app.agent.tools.schemas import XEditAnalysisParams
from sky_claw.app.agent.tools.xedit_policy import (
    XEDIT_AGENT_ALLOWED_SCRIPT_NAMES,
    XEDIT_AGENT_CANONICAL_SCRIPTS,
    XEDIT_AGENT_SCRIPT_ALIASES,
)
from sky_claw.app.agent.tools.xedit_readonly_tool import run_xedit_script
from sky_claw.local.xedit.output_parser import XEditResult

_ESPERADOS_CANONICOS = frozenset(
    {
        "dump_record_detail.pas",
        "list_all_conflicts.pas",
        "list_grass_worldspaces.pas",
        "list_zero_bound_grass.pas",
    }
)
_ESPERADOS_ALIASES = {"list_conflicts.pas": "list_all_conflicts.pas"}


def _stdout_valido(script: str) -> str:
    if script == "list_all_conflicts.pas":
        return (
            "CONFLICT|00012345|IronSword|WEAP|Winner.esp|Loser.esp\n"
            "SUMMARY|total_conflicts=1|critical=0|minor=1\n"
        )
    if script == "dump_record_detail.pas":
        return (
            "DUMP_BEGIN|00012345|IronSword|WEAP\n"
            "VERSION|00012345|Winner.esp|1\n"
            "ELEMENT|00012345|Winner.esp|DATA\\Damage|12\n"
            "DUMP_END|00012345\n"
        )
    if script == "list_grass_worldspaces.pas":
        return (
            "WSGRASS|0000003C|Tamriel|Skyrim.esm\n"
            "SUMMARY|grass_worldspaces=1|land_scanned=1|ltex_grass=1\n"
        )
    if script == "list_zero_bound_grass.pas":
        return (
            "ZEROBOUND|00012345|Grass01|Winner.esp|Origin.esm|zeros\n"
            "SUMMARY|total_gras=1|zero_bounds=1\n"
        )
    raise AssertionError(f"Falta fixture para {script}")


class _RunnerConStagingCanonico:
    """Doble mínimo con las dos capacidades que exige producción."""

    def __init__(self) -> None:
        self.stageados: list[list[str]] = []
        self.ejecuciones: list[tuple[str, list[str]]] = []

    async def ensure_scripts_staged(self, script_names: list[str]) -> list[Any]:
        self.stageados.append(list(script_names))
        return []

    async def run_script(
        self,
        script_name: str,
        plugins: list[str],
        *,
        timeout: int | None = None,
    ) -> XEditResult:
        del timeout
        self.ejecuciones.append((script_name, list(plugins)))
        return XEditResult(
            return_code=0,
            processed_plugins=list(plugins),
            errors=[],
            raw_stdout=_stdout_valido(script_name),
        )


def test_la_policy_congela_allowlist_y_aliases_completos() -> None:
    # Preparar / Actuar / Verificar
    assert XEDIT_AGENT_CANONICAL_SCRIPTS == _ESPERADOS_CANONICOS
    assert XEDIT_AGENT_SCRIPT_ALIASES == _ESPERADOS_ALIASES
    assert XEDIT_AGENT_ALLOWED_SCRIPT_NAMES == _ESPERADOS_CANONICOS | frozenset(_ESPERADOS_ALIASES)


def test_el_schema_anuncia_exactamente_los_scripts_permitidos() -> None:
    # Preparar / Actuar
    schema = XEditAnalysisParams.model_json_schema()

    # Verificar
    assert set(schema["properties"]["script_name"]["enum"]) == XEDIT_AGENT_ALLOWED_SCRIPT_NAMES


def test_el_schema_rechaza_un_pas_desconocido() -> None:
    # Preparar / Actuar / Verificar
    with pytest.raises(ValidationError):
        XEditAnalysisParams.model_validate(
            {"script_name": "evil_local.pas", "plugins": ["Skyrim.esm"]},
            strict=True,
        )


@pytest.mark.parametrize("script_name", sorted(XEDIT_AGENT_CANONICAL_SCRIPTS))
async def test_cada_script_canonico_stagea_y_ejecuta_su_parser(script_name: str) -> None:
    # Preparar
    runner = _RunnerConStagingCanonico()
    plugins = ["Skyrim.esm"]

    # Actuar
    result = json.loads(await run_xedit_script(runner, script_name, plugins))

    # Verificar
    assert result["success"] is True
    assert result["message"] == ""
    assert result["script"] == script_name
    assert runner.stageados == [[script_name]]
    assert runner.ejecuciones == [(script_name, plugins)]


def test_los_protocolos_devuelven_datos_estructurados_reales() -> None:
    async def _ejecutar(script: str) -> dict[str, Any]:
        return json.loads(await run_xedit_script(_RunnerConStagingCanonico(), script, ["Skyrim.esm"]))

    async def _verificar() -> None:
        conflictos = await _ejecutar("list_all_conflicts.pas")
        dump = await _ejecutar("dump_record_detail.pas")
        worldspaces = await _ejecutar("list_grass_worldspaces.pas")
        bounds = await _ejecutar("list_zero_bound_grass.pas")

        assert conflictos["conflicts"][0]["form_id"] == "00012345"
        assert conflictos["summary"]["total_conflicts"] == 1
        assert dump["dumps"][0]["form_id"] == "00012345"
        assert worldspaces["worldspaces"][0]["editor_id"] == "Tamriel"
        assert bounds["findings"][0]["reason"] == "zeros"

    asyncio.run(_verificar())


async def test_el_alias_legacy_canonicaliza_antes_del_staging() -> None:
    # Preparar
    runner = _RunnerConStagingCanonico()

    # Actuar
    result = json.loads(await run_xedit_script(runner, "list_conflicts.pas", ["Skyrim.esm"]))

    # Verificar
    assert result["success"] is True
    assert result["script"] == "list_all_conflicts.pas"
    assert runner.stageados == [["list_all_conflicts.pas"]]
    assert runner.ejecuciones == [("list_all_conflicts.pas", ["Skyrim.esm"])]


@pytest.mark.parametrize("script_name", ("apply_leveled_list_merge.pas", "evil_local.pas"))
async def test_un_script_fuera_de_policy_falla_antes_del_runner(script_name: str) -> None:
    # Preparar
    runner = _RunnerConStagingCanonico()

    # Actuar
    result = json.loads(await run_xedit_script(runner, script_name, ["Skyrim.esm"]))

    # Verificar
    assert result["success"] is False
    assert result["message"] == result["error"]
    assert "no está permitido" in result["message"]
    assert runner.stageados == []
    assert runner.ejecuciones == []


async def test_un_runner_sin_staging_falla_cerrado_antes_de_ejecutar() -> None:
    class _RunnerSinStaging:
        def __init__(self) -> None:
            self.ejecutado = False

        async def run_script(self, script_name: str, plugins: list[str]) -> XEditResult:
            del script_name, plugins
            self.ejecutado = True
            return XEditResult(return_code=0)

    # Preparar
    runner = _RunnerSinStaging()

    # Actuar
    result = json.loads(await run_xedit_script(runner, "list_all_conflicts.pas", ["Skyrim.esm"]))

    # Verificar
    assert result["success"] is False
    assert "staging canónico" in result["message"]
    assert runner.ejecutado is False


async def test_sin_runner_conserva_el_error_legacy_y_contrato_canonico() -> None:
    # Preparar / Actuar
    result = json.loads(await run_xedit_script(None, "list_all_conflicts.pas", ["Skyrim.esm"]))

    # Verificar
    assert result["success"] is False
    assert result["message"] == result["error"]
    assert "xEdit runner is not configured" in result["error"]


async def test_cancelled_error_se_propaga_sin_convertirse_en_json() -> None:
    class _RunnerCancelado(_RunnerConStagingCanonico):
        async def run_script(
            self,
            script_name: str,
            plugins: list[str],
            *,
            timeout: int | None = None,
        ) -> XEditResult:
            del timeout
            self.ejecuciones.append((script_name, list(plugins)))
            raise asyncio.CancelledError

    # Preparar
    runner = _RunnerCancelado()

    # Actuar / Verificar
    with pytest.raises(asyncio.CancelledError):
        await run_xedit_script(runner, "list_all_conflicts.pas", ["Skyrim.esm"])

    assert runner.stageados == [["list_all_conflicts.pas"]]
    assert runner.ejecuciones == [("list_all_conflicts.pas", ["Skyrim.esm"])]

"""Tool LLM para diagnósticos xEdit read-only con provenance canónica."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

from sky_claw.app.security.sanitize import sanitize_for_prompt

from .xedit_policy import (
    XEDIT_AGENT_ALLOWED_SCRIPT_NAMES,
    XEDIT_AGENT_CANONICAL_SCRIPTS,
    canonicalizar_script_xedit_del_agente,
)


def _error_json(detalle: str) -> str:
    """Serializa un fallo con contrato canónico y clave legacy ``error``."""
    mensaje = sanitize_for_prompt(detalle, max_length=256)
    return json.dumps({"success": False, "message": mensaje, "error": mensaje})


async def _ejecutar_script_canonico(xedit_runner: Any, script: str, plugins: list[str]) -> Any:
    """Stagea bytes canónicos, ejecuta y falla cerrado ante un resultado inválido."""
    stager = getattr(xedit_runner, "ensure_scripts_staged", None)
    if not callable(stager):
        raise RuntimeError("El runner de xEdit no puede garantizar staging canónico; run_xedit_script queda bloqueado.")

    await stager([script])
    result = await xedit_runner.run_script(script, plugins)
    if not result.success:
        detalle = "; ".join(result.errors) or result.raw_stderr.strip() or f"exit code {result.return_code}"
        raise RuntimeError(f"El análisis de xEdit falló ({detalle}).")
    return result


def _conflictos_legacy(result: Any) -> list[dict[str, str]]:
    """Adapta resultados preparseados de runners legacy sin stdout de protocolo."""
    return [{"plugin": c.plugin, "record": c.record, "detail": c.detail} for c in result.conflicts]


async def _ejecutar_conflictos(xedit_runner: Any, plugins: list[str]) -> dict[str, Any]:
    """Ejecuta ``list_all_conflicts.pas`` y valida su protocolo estructurado."""
    from sky_claw.local.xedit.conflict_analyzer import parse_summary_line
    from sky_claw.local.xedit.output_parser import XEditOutputParser

    script = "list_all_conflicts.pas"
    result = await _ejecutar_script_canonico(xedit_runner, script, plugins)
    raw_stdout = result.raw_stdout
    summary = parse_summary_line(raw_stdout)
    conflictos = XEditOutputParser.parse_conflict_report(raw_stdout)

    if raw_stdout.strip():
        if "total_conflicts" not in summary:
            raise RuntimeError(f"Salida de {script} truncada: falta SUMMARY|total_conflicts.")
        if summary["total_conflicts"] != len(conflictos):
            raise RuntimeError(
                f"Salida de {script} inconsistente: SUMMARY declara "
                f"total_conflicts={summary['total_conflicts']} pero se parsearon "
                f"{len(conflictos)} conflictos."
            )
    else:
        # Compatibilidad con adapters heredados que ya entregan ``XEditResult``
        # preparseado y no conservan stdout. El runner productivo sí conserva
        # raw_stdout y por tanto siempre pasa por el protocolo estricto de arriba.
        conflictos = _conflictos_legacy(result)

    return {
        "success": True,
        "message": "",
        "script": script,
        "return_code": result.return_code,
        "processed_plugins": result.processed_plugins,
        "conflicts": conflictos,
        "summary": summary,
    }


async def _ejecutar_dump(xedit_runner: Any, plugins: list[str]) -> dict[str, Any]:
    """Ejecuta ``dump_record_detail.pas`` y devuelve sólo bloques completos."""
    from sky_claw.local.xedit.record_dump_parser import parse_dump_output

    script = "dump_record_detail.pas"
    result = await _ejecutar_script_canonico(xedit_runner, script, plugins)
    dumps = parse_dump_output(result.raw_stdout)
    return {
        "success": True,
        "message": "",
        "script": script,
        "return_code": result.return_code,
        "processed_plugins": result.processed_plugins,
        "dumps": [dataclasses.asdict(dump) for dump in dumps],
    }


async def _ejecutar_worldspaces(xedit_runner: Any, plugins: list[str]) -> dict[str, Any]:
    """Delega al analyzer canónico con checks de SUMMARY y ``land_scanned``."""
    from sky_claw.local.xedit.grass_analyzer import GrassAnalyzer

    report = await GrassAnalyzer().list_grass_worldspaces(plugins, xedit_runner)
    return {
        "success": True,
        "message": "",
        "script": "list_grass_worldspaces.pas",
        **report.to_dict(),
    }


async def _ejecutar_zero_bounds(xedit_runner: Any, plugins: list[str]) -> dict[str, Any]:
    """Delega al analyzer canónico con validación de SUMMARY y conteo."""
    from sky_claw.local.xedit.grass_analyzer import GrassAnalyzer

    report = await GrassAnalyzer().detect_zero_bound_grass(plugins, xedit_runner)
    return {
        "success": True,
        "message": "",
        "script": "list_zero_bound_grass.pas",
        **report.to_dict(),
    }


async def run_xedit_script(xedit_runner: Any, script_name: str, plugins: list[str]) -> str:
    """Ejecuta únicamente diagnósticos xEdit read-only declarados por Sky-Claw.

    El schema Pydantic y esta frontera comparten la misma política de nombres.
    Antes de ejecutar se exige staging canónico; un runner que no pueda probar
    provenance de los bytes falla cerrado. Cada script se enruta al parser o
    analyzer que entiende su protocolo real, evitando éxitos con payload vacío.
    """
    if xedit_runner is None:
        return _error_json("xEdit runner is not configured")

    if script_name not in XEDIT_AGENT_ALLOWED_SCRIPT_NAMES:
        permitidos = ", ".join(sorted(XEDIT_AGENT_ALLOWED_SCRIPT_NAMES))
        return _error_json(
            f"El script xEdit {script_name!r} no está permitido para el tool read-only. "
            f"Scripts permitidos: {permitidos}"
        )

    script = canonicalizar_script_xedit_del_agente(script_name)
    if script not in XEDIT_AGENT_CANONICAL_SCRIPTS:
        return _error_json(f"Política xEdit inconsistente para {script_name!r}; ejecución bloqueada.")

    stager = getattr(xedit_runner, "ensure_scripts_staged", None)
    if not callable(stager):
        return _error_json("El runner de xEdit no puede garantizar staging canónico; run_xedit_script queda bloqueado.")

    try:
        if script == "list_all_conflicts.pas":
            resultado = await _ejecutar_conflictos(xedit_runner, plugins)
        elif script == "dump_record_detail.pas":
            resultado = await _ejecutar_dump(xedit_runner, plugins)
        elif script == "list_grass_worldspaces.pas":
            resultado = await _ejecutar_worldspaces(xedit_runner, plugins)
        elif script == "list_zero_bound_grass.pas":
            resultado = await _ejecutar_zero_bounds(xedit_runner, plugins)
        else:  # pragma: no cover - protegido por el ancla exhaustiva de policy
            return _error_json(f"No existe un parser registrado para {script!r}.")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _error_json(str(exc))

    return json.dumps(resultado)


__all__ = ["run_xedit_script"]

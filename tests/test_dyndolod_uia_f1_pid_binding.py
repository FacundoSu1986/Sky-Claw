"""F1: a conclusive helper MATCH must echo solicitud.pid."""

from __future__ import annotations

import json
import pathlib
import sys
import textwrap

from sky_claw.local.tools.dyndolod_uia_ejecutor import EjecutorGatePorHelper
from sky_claw.local.tools.dyndolod_uia_gate import PoliticaDeReintento
from sky_claw.local.tools.dyndolod_uia_preflight import (
    CriteriosDeControl,
    EstadoPreflight,
    RazonPreflight,
    SolicitudPreflightUIA,
)


def _solicitud() -> SolicitudPreflightUIA:
    return SolicitudPreflightUIA(
        tool="TexGen",
        ejecutable_esperado=r"C:\Modding\DynDOLOD\TexGenx64.exe",
        salida_administrada_esperada=r"E:\Sky-Claw T5 Rig\TexGen Output",
        criterios_del_control=CriteriosDeControl(tipo_de_control="Edit", class_name="TEdit"),
        pid=1234,
    )


def _script(payload: dict[str, object]) -> str:
    return textwrap.dedent(
        f"""
        import pathlib, sys
        pathlib.Path(sys.argv[1], "resultado.json").write_text({json.dumps(payload)!r}, encoding="utf-8")
        """
    )


_MATCH = {
    "estado": "MATCH",
    "razon": "OUTPUT_COINCIDE",
    "tool": "TexGen",
    "detalle": "ok",
    "valor_esperado": r"E:\Sky-Claw T5 Rig\TexGen Output",
    "pid": 1234,
    "ventana": "v",
    "control_observado": {
        "pid": 1234,
        "automation_id": "",
        "nombre": "",
        "tipo_de_control": "Edit",
        "class_name": "TEdit",
    },
    "valor_observado": r"E:\Sky-Claw T5 Rig\TexGen Output",
    "valor_observado_canonico": r"e:\sky-claw t5 rig\texgen output",
    "valor_esperado_canonico": r"e:\sky-claw t5 rig\texgen output",
    "evidencia": [],
}


async def test_match_mismo_tool_mismo_output_sin_pid_falla_cerrado(tmp_path: pathlib.Path) -> None:
    payload = dict(_MATCH)
    payload["pid"] = None
    script = tmp_path / "helper_match_sin_pid.py"
    script.write_text(_script(payload), encoding="utf-8")
    resultado = await EjecutorGatePorHelper(comando=(sys.executable, str(script))).ejecutar(
        _solicitud(), politica=PoliticaDeReintento.INICIO, timeout_segundos=5.0, intervalo_segundos=0.05
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert "no corresponde a la solicitud" in resultado.detalle


async def test_match_con_pid_eco_sigue_siendo_match(tmp_path: pathlib.Path) -> None:
    script = tmp_path / "helper_match_ok.py"
    script.write_text(_script(_MATCH), encoding="utf-8")
    resultado = await EjecutorGatePorHelper(comando=(sys.executable, str(script))).ejecutar(
        _solicitud(), politica=PoliticaDeReintento.INICIO, timeout_segundos=5.0, intervalo_segundos=0.05
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.pid == 1234


async def test_unknown_temprano_sin_pid_conserva_la_razon(tmp_path: pathlib.Path) -> None:
    script = tmp_path / "helper_unknown.py"
    script.write_text(
        _script(
            {
                "estado": "UNKNOWN",
                "razon": "TOOL_DESCONOCIDA",
                "tool": "TexGen",
                "detalle": "sin observar pid",
                "valor_esperado": r"E:\Sky-Claw T5 Rig\TexGen Output",
                "pid": None,
                "ventana": None,
                "valor_observado": None,
                "valor_observado_canonico": None,
                "valor_esperado_canonico": None,
                "evidencia": [],
            }
        ),
        encoding="utf-8",
    )
    resultado = await EjecutorGatePorHelper(comando=(sys.executable, str(script))).ejecutar(
        _solicitud(), politica=PoliticaDeReintento.INICIO, timeout_segundos=5.0, intervalo_segundos=0.05
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.TOOL_DESCONOCIDA

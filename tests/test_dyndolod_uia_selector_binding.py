"""Selector binding (#590): el control observado tiene que ser el solicitado.

Finding ``PRRT_kwDOR1JjU86jgggG``. La frontera IPC del helper validaba tool,
pid y ``valor_esperado``, pero no ligaba la respuesta a
``solicitud.criterios_del_control``: un helper regresionado podía resolver con
un selector rancio OTRO control que contuviera la ruta administrada y su
``MATCH`` —internamente coherente— autorizaba el Start del FINAL gate sin
probar que el control leído era el campo Output pedido.

El fix es estructural y fail-closed en dos capas, y esta suite las ancla:

* la respuesta del helper transporta el descriptor SERIALIZABLE del control
  realmente seleccionado (``EvidenciaControlObservado``), sin handles COM;
* el padre re-valida ese descriptor contra ``criterios_del_control`` con la
  MISMA semántica de ``CriteriosDeControl.coincide`` antes de aceptar un
  veredicto concluyente. Un UNKNOWN temprano puede no traer control: su
  contrato es no concluir.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import textwrap

import pytest

from sky_claw.local.tools.dyndolod_uia_ejecutor import EjecutorGatePorHelper
from sky_claw.local.tools.dyndolod_uia_gate import (
    CAMPOS_DEL_CONTROL_OBSERVADO,
    ContratoDeHelperError,
    PoliticaDeReintento,
    evidencia_de_resultado_coherente,
    resultado_a_json,
    resultado_desde_json,
)
from sky_claw.local.tools.dyndolod_uia_preflight import (
    CriteriosDeControl,
    EstadoPreflight,
    EvidenciaControlObservado,
    RazonPreflight,
    ResultadoPreflightUIA,
    SolicitudPreflightUIA,
    canonicalizar_ruta_windows,
)

SALIDA = r"E:\Sky-Claw T5 Rig\TexGen Output"


def _solicitud(**criterios: object) -> SolicitudPreflightUIA:
    """Solicitud con el selector productivo; los criterios extra son del caso."""
    base: dict[str, object] = {"tipo_de_control": "Edit", "class_name": "TEdit"}
    base.update(criterios)
    return SolicitudPreflightUIA(
        tool="TexGen",
        ejecutable_esperado=r"C:\Modding\DynDOLOD\TexGenx64.exe",
        salida_administrada_esperada=SALIDA,
        criterios_del_control=CriteriosDeControl(**base),  # type: ignore[arg-type]
        pid=1234,
    )


def _control(pid: int = 1234, **campos: object) -> dict[str, object]:
    """Descriptor serializable del control observado, como viaja por el canal."""
    datos: dict[str, object] = {
        "pid": pid,
        "automation_id": "",
        "nombre": "",
        "tipo_de_control": "Edit",
        "class_name": "TEdit",
    }
    datos.update(campos)
    return datos


def _match_payload(control: object = None, *, incluir_control: bool = True, **overrides: object) -> dict[str, object]:
    """Respuesta del helper con MATCH internamente coherente (P2) ya satisfecho."""
    payload: dict[str, object] = {
        "estado": "MATCH",
        "razon": "OUTPUT_COINCIDE",
        "tool": "TexGen",
        "detalle": "ok",
        "valor_esperado": SALIDA,
        "pid": 1234,
        "ventana": "TexGen 3.00",
        "valor_observado": SALIDA,
        "valor_observado_canonico": canonicalizar_ruta_windows(SALIDA),
        "valor_esperado_canonico": canonicalizar_ruta_windows(SALIDA),
        "evidencia": [],
    }
    if incluir_control:
        payload["control_observado"] = control
    payload.update(overrides)
    return payload


async def _ejecutar(
    tmp_path: pathlib.Path,
    payload: object,
    solicitud: SolicitudPreflightUIA | None = None,
) -> ResultadoPreflightUIA:
    """Corre el ejecutor productivo contra un helper falso con el payload dado."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "helper_selector.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import pathlib, sys
            pathlib.Path(sys.argv[1], "resultado.json").write_text({json.dumps(payload)!r}, encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    return await EjecutorGatePorHelper(comando=(sys.executable, str(script))).ejecutar(
        solicitud if solicitud is not None else _solicitud(),
        politica=PoliticaDeReintento.INICIO,
        timeout_segundos=5.0,
        intervalo_segundos=0.05,
    )


# ---------------------------------------------------------------------------
# A/B — el hueco: selector incorrecto o control ausente
# ---------------------------------------------------------------------------


async def test_match_con_control_de_otra_clase_se_rechaza(tmp_path: pathlib.Path) -> None:
    """Caso A: se pidió Edit+TEdit y el control observado es Edit+TMemo.

    El valor observado coincide con el esperado y tool/pid/valor_esperado son
    correctos, así que la coherencia de valor (P2) no lo atrapa: lo que lo
    rechaza es el binding contra el selector solicitado.
    """
    resultado = await _ejecutar(tmp_path, _match_payload(_control(class_name="TMemo")))

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert "no corresponde a la solicitud" in resultado.detalle


async def test_match_sin_control_observado_se_rechaza(tmp_path: pathlib.Path) -> None:
    """Caso B: un veredicto concluyente sin identidad de control no es contrato."""
    resultado = await _ejecutar(tmp_path, _match_payload(incluir_control=False))

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert "ilegible" in resultado.detalle


async def test_match_con_control_inyectado_por_otra_clave_es_el_mismo_rechazo(tmp_path: pathlib.Path) -> None:
    """El control no puede "colarse" por una clave que el deserializador ignore."""
    payload = _match_payload(incluir_control=False)
    payload["control"] = _control()
    resultado = await _ejecutar(tmp_path, payload)

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE


# ---------------------------------------------------------------------------
# C/D — los MATCH legítimos siguen vivos
# ---------------------------------------------------------------------------


async def test_match_legitimo_con_el_control_solicitado_sigue_siendo_match(tmp_path: pathlib.Path) -> None:
    """Caso C: el descriptor observado satisface exactamente el selector pedido."""
    resultado = await _ejecutar(tmp_path, _match_payload(_control()))

    assert resultado.estado is EstadoPreflight.MATCH
    assert resultado.razon is RazonPreflight.OUTPUT_COINCIDE


async def test_criterio_parcial_de_clase_se_satisface_con_un_control_exacto(tmp_path: pathlib.Path) -> None:
    """Caso D: si el criterio es sólo ``class_name``, el resto no se exige."""
    resultado = await _ejecutar(
        tmp_path,
        _match_payload(_control(automation_id="inestable", nombre="Output")),
        _solicitud(tipo_de_control=None),
    )

    assert resultado.estado is EstadoPreflight.MATCH


# ---------------------------------------------------------------------------
# E/F — cada criterio presente en la solicitud se compara exacto
# ---------------------------------------------------------------------------


async def test_automation_id_solicitado_se_compara_exacto(tmp_path: pathlib.Path) -> None:
    """Caso E: con ``automation_id`` en los criterios, otro id no satisface."""
    solicitud = _solicitud(automation_id="edOutput")
    aceptado = await _ejecutar(tmp_path / "aceptado", _match_payload(_control(automation_id="edOutput")), solicitud)
    assert aceptado.estado is EstadoPreflight.MATCH

    rechazado = await _ejecutar(tmp_path / "rechazado", _match_payload(_control(automation_id="otro")), solicitud)
    assert rechazado.estado is EstadoPreflight.UNKNOWN
    assert rechazado.razon is RazonPreflight.UIA_NO_DISPONIBLE


async def test_nombre_solicitado_se_compara_exacto(tmp_path: pathlib.Path) -> None:
    """Caso F: con ``nombre`` en los criterios, otro nombre no satisface."""
    solicitud = _solicitud(nombre="Output")
    aceptado = await _ejecutar(tmp_path / "aceptado", _match_payload(_control(nombre="Output")), solicitud)
    assert aceptado.estado is EstadoPreflight.MATCH

    rechazado = await _ejecutar(tmp_path / "rechazado", _match_payload(_control(nombre="Log")), solicitud)
    assert rechazado.estado is EstadoPreflight.UNKNOWN


# ---------------------------------------------------------------------------
# G — pid del control
# ---------------------------------------------------------------------------


async def test_control_con_pid_de_otro_proceso_se_rechaza(tmp_path: pathlib.Path) -> None:
    """Caso G: resultado.pid correcto pero el control vino de otro proceso."""
    resultado = await _ejecutar(tmp_path, _match_payload(_control(pid=9999)))

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert "no corresponde a la solicitud" in resultado.detalle


async def test_control_con_pid_ilegible_no_sobrevive_la_frontera(tmp_path: pathlib.Path) -> None:
    """Un pid no positivo no identifica un proceso: contrato roto, no veredicto."""
    resultado = await _ejecutar(tmp_path, _match_payload(_control(pid=0)))

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert "ilegible" in resultado.detalle


# ---------------------------------------------------------------------------
# Fase 9 — un UNKNOWN temprano no está obligado a traer control
# ---------------------------------------------------------------------------


async def test_unknown_temprano_sin_control_sigue_siendo_unknown(tmp_path: pathlib.Path) -> None:
    """El corte de validación de solicitud ocurre ANTES de resolver control."""
    payload = _match_payload(incluir_control=False)
    payload.update(
        {
            "estado": "UNKNOWN",
            "razon": "TOOL_DESCONOCIDA",
            "detalle": "tool fuera del contrato",
            "pid": None,
            "valor_observado": None,
            "valor_observado_canonico": None,
            "valor_esperado_canonico": None,
        }
    )
    resultado = await _ejecutar(tmp_path, payload)

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.TOOL_DESCONOCIDA


# ---------------------------------------------------------------------------
# Contrato serializable: round-trip y malformados fail-closed
# ---------------------------------------------------------------------------


def _resultado_con_control(estado: EstadoPreflight, razon: RazonPreflight) -> ResultadoPreflightUIA:
    observado = SALIDA if estado is EstadoPreflight.MATCH else r"E:\Sky-Claw T5 Rig\Stale"
    return ResultadoPreflightUIA(
        estado=estado,
        razon=razon,
        tool="TexGen",
        detalle="prueba de selector",
        valor_esperado=SALIDA,
        pid=1234,
        valor_observado=observado,
        valor_observado_canonico=canonicalizar_ruta_windows(observado),
        valor_esperado_canonico=canonicalizar_ruta_windows(SALIDA),
        control_observado=EvidenciaControlObservado(
            pid=1234,
            automation_id="",
            nombre="",
            tipo_de_control="Edit",
            class_name="TEdit",
        ),
    )


@pytest.mark.parametrize(
    ("estado", "razon"),
    [
        (EstadoPreflight.MATCH, RazonPreflight.OUTPUT_COINCIDE),
        (EstadoPreflight.MISMATCH, RazonPreflight.OUTPUT_DIFIERE),
    ],
)
def test_el_control_observado_va_y_vuelve_sin_perder_campos(estado, razon) -> None:
    original = _resultado_con_control(estado, razon)
    vuelta = resultado_desde_json(resultado_a_json(original))
    assert vuelta == original
    assert vuelta.control_observado == original.control_observado


@pytest.mark.parametrize(
    "control_roto",
    [
        pytest.param({}, id="objeto-vacio"),
        pytest.param(_control(clave_sin_uso="x"), id="campo-de-mas"),
        pytest.param(
            {clave: valor for clave, valor in _control().items() if clave != "class_name"}, id="falta-class_name"
        ),
        pytest.param({**_control(), "pid": "1234"}, id="pid-string"),
        pytest.param({**_control(), "pid": True}, id="pid-bool"),
        pytest.param({**_control(), "nombre": 7}, id="nombre-no-string"),
        pytest.param("no es un objeto", id="string"),
        pytest.param(["Edit", "TEdit"], id="lista"),
    ],
)
def test_un_control_con_estructura_invalida_es_contrato_roto(control_roto: object) -> None:
    with pytest.raises(ContratoDeHelperError):
        resultado_desde_json(json.dumps(_match_payload(control_roto)))


def test_un_match_sin_control_observado_es_contrato_roto() -> None:
    """El caso B a nivel deserializador: la coherencia del veredicto lo exige."""
    with pytest.raises(ContratoDeHelperError, match="evidencia"):
        resultado_desde_json(json.dumps(_match_payload(incluir_control=False)))


def test_la_coherencia_del_resultado_exige_control_en_veredictos_concluyentes() -> None:
    """``evidencia_de_resultado_coherente`` es la autoridad de coherencia interna."""
    sin_control = ResultadoPreflightUIA(
        estado=EstadoPreflight.MATCH,
        razon=RazonPreflight.OUTPUT_COINCIDE,
        tool="TexGen",
        detalle="valor coherente pero sin control",
        valor_esperado=SALIDA,
        valor_observado=SALIDA,
        valor_observado_canonico=canonicalizar_ruta_windows(SALIDA),
        valor_esperado_canonico=canonicalizar_ruta_windows(SALIDA),
    )
    assert not evidencia_de_resultado_coherente(sin_control)
    assert evidencia_de_resultado_coherente(
        _resultado_con_control(EstadoPreflight.MATCH, RazonPreflight.OUTPUT_COINCIDE)
    )


def test_los_campos_del_control_observado_estan_congelados() -> None:
    """El schema del descriptor es el de la dataclass: sin listas paralelas.

    Ancla de enumeración, no de muestra: agregar un campo a la dataclass sin
    decidir si viaja por el canal rompe acá, y el serializer/deserializer no
    pueden divergir en silencio.
    """
    assert CAMPOS_DEL_CONTROL_OBSERVADO == ("pid", "automation_id", "nombre", "tipo_de_control", "class_name")
    assert tuple(campo.name for campo in dataclasses.fields(EvidenciaControlObservado)) == CAMPOS_DEL_CONTROL_OBSERVADO


def test_un_unknown_puede_llevar_control_y_se_preserva() -> None:
    """La obligación fuerte es de MATCH/MISMATCH; un UNKNOWN puede traerlo igual."""
    original = ResultadoPreflightUIA(
        estado=EstadoPreflight.UNKNOWN,
        razon=RazonPreflight.CONTROL_AMBIGUO,
        tool="TexGen",
        detalle="ambiguo",
        valor_esperado=SALIDA,
        control_observado=EvidenciaControlObservado(
            pid=1234,
            automation_id="",
            nombre="",
            tipo_de_control="Edit",
            class_name="TEdit",
        ),
    )
    vuelta = resultado_desde_json(resultado_a_json(original))
    assert vuelta == original
    assert vuelta.control_observado == original.control_observado

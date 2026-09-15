"""T5-v2.1 — el ejecutor del gate: helper descartable y contrato serializable.

El defecto que esta suite ancla: ``wait_for(to_thread(COM))`` NO mata el hilo
subyacente, así que la "gracia externa" no evitaba un hilo huérfano cuando una
llamada COM individual nunca retorna. La solución productiva es un PROCESO
descartable, y el test central (``test_hard_hang_deadline_externo_mata_y_reapea_el_helper``)
exige las tres propiedades del encargo: el deadline externo devuelve al caller,
el helper YA NO EXISTE y no queda ningún proceso del mecanismo.

Los helpers de esta suite son procesos reales bloqueados indefinidamente —no
sleeps cooperativos que vuelven— porque un fake cooperativo no puede refutar la
garantía que se está probando.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import textwrap
import threading
import time

import psutil
import pytest

import sky_claw.local.tools.dyndolod_uia_ejecutor as ejecutor_mod
from sky_claw.local.tools.dyndolod_uia_ejecutor import EjecutorGateEnProceso, EjecutorGatePorHelper
from sky_claw.local.tools.dyndolod_uia_gate import (
    ContratoDeHelperError,
    PedidoDeGate,
    PoliticaDeReintento,
    ResultadoPreflightUIA,
    pedido_a_json,
    pedido_desde_json,
    resultado_a_json,
    resultado_desde_json,
)
from sky_claw.local.tools.dyndolod_uia_preflight import (
    CriteriosDeControl,
    EstadoPreflight,
    RazonPreflight,
    SolicitudPreflightUIA,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
MODULO_HELPER = "sky_claw.local.tools.dyndolod_uia_helper"


def _resultado_de_ejemplo() -> ResultadoPreflightUIA:
    return ResultadoPreflightUIA(
        estado=EstadoPreflight.MISMATCH,
        razon=RazonPreflight.OUTPUT_DIFIERE,
        tool="TexGen",
        detalle="la GUI muestra otra ruta",
        valor_esperado=r"E:\Sky-Claw T5 Rig\TexGen Output",
        pid=4242,
        ventana="TexGen 3.00",
        valor_observado=r"E:\Sky-Claw T5 Rig\Stale TexGen",
        valor_observado_canonico=r"e:\sky-claw t5 rig\stale texgen",
        valor_esperado_canonico=r"e:\sky-claw t5 rig\texgen output",
        evidencia=("pid=4242 identidad probada por ruta completa",),
    )


def _solicitud_de_ejemplo(tool: str = "TexGen") -> SolicitudPreflightUIA:
    return SolicitudPreflightUIA(
        tool=tool,
        ejecutable_esperado=r"C:\Modding\DynDOLOD\TexGenx64.exe",
        salida_administrada_esperada=r"E:\Sky-Claw T5 Rig\TexGen Output",
        criterios_del_control=CriteriosDeControl(tipo_de_control="Edit", class_name="TEdit"),
        pid=4242,
    )


# ---------------------------------------------------------------------------
# Contrato serializable
# ---------------------------------------------------------------------------


def test_el_contrato_serializable_va_y_vuelve_sin_perder_campos() -> None:
    original = _resultado_de_ejemplo()
    vuelta = resultado_desde_json(resultado_a_json(original))
    assert vuelta == original


def test_el_pedido_serializable_va_y_vuelve_sin_perder_campos() -> None:
    pedido = PedidoDeGate(
        solicitud=_solicitud_de_ejemplo(),
        politica=PoliticaDeReintento.FINAL,
        timeout_segundos=12.5,
        intervalo_segundos=0.25,
    )
    assert pedido_desde_json(pedido_a_json(pedido)) == pedido


@pytest.mark.parametrize(
    "texto",
    [
        "no es json",
        "{}",
        '{"estado": "MATCH"}',
        json.dumps({"estado": "MATCH", "razon": "NO_EXISTE", "tool": "TexGen", "detalle": "x", "valor_esperado": "y"}),
    ],
)
def test_el_contrato_rechaza_respuestas_incompletas_o_desconocidas(texto: str) -> None:
    """Fail-closed por contrato: una respuesta rara no puede volverse veredicto."""
    with pytest.raises(ContratoDeHelperError):
        resultado_desde_json(texto)


# ---------------------------------------------------------------------------
# Helper real: el worker corre de punta a punta
# ---------------------------------------------------------------------------


async def test_el_helper_real_responde_el_fail_closed_del_backend(tmp_path: pathlib.Path) -> None:
    """El worker arranca, lee el pedido, corre el gate y escribe un JSON parseable.

    Con una tool fuera de ``TOOLS_OBSERVABLES`` el pipeline corta en la
    validación (TOOL_DESCONOCIDA) antes de tocar COM, así que el test vale en
    cualquier plataforma sin mentir sobre lo que ejercita: el canal padre↔helper
    y el ciclo de vida del worker. El backend COM real tiene su rig.
    """
    pedido = PedidoDeGate(
        solicitud=_solicitud_de_ejemplo(tool="LOOT"),
        politica=PoliticaDeReintento.INICIO,
        timeout_segundos=0.5,
        intervalo_segundos=0.01,
    )
    (tmp_path / "pedido.json").write_text(pedido_a_json(pedido), encoding="utf-8")

    proceso = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        MODULO_HELPER,
        str(tmp_path),
        cwd=str(RAIZ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await asyncio.wait_for(proceso.communicate(), timeout=60)
    assert proceso.returncode == 0, stderr.decode(errors="replace")
    resultado = resultado_desde_json((tmp_path / "resultado.json").read_text(encoding="utf-8"))
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.TOOL_DESCONOCIDA


# ---------------------------------------------------------------------------
# Ejecutor con helper falso: caminos de fallo
# ---------------------------------------------------------------------------


def _escribir_script(tmp_path: pathlib.Path, nombre: str, cuerpo: str) -> pathlib.Path:
    script = tmp_path / nombre
    script.write_text(textwrap.dedent(cuerpo), encoding="utf-8")
    return script


async def test_el_helper_falso_que_devuelve_match_se_parsea(tmp_path: pathlib.Path) -> None:
    """Camino feliz del canal: el padre lee el ``resultado.json`` del helper."""
    script = _escribir_script(
        tmp_path,
        "helper_ok.py",
        """
        import json, pathlib, sys
        base = pathlib.Path(sys.argv[1])
        resultado = {
            "estado": "MATCH", "razon": "OUTPUT_COINCIDE", "tool": "TexGen",
            "detalle": "ok", "valor_esperado": "E:/out", "pid": 1, "ventana": "v",
            "valor_observado": "E:/out", "valor_observado_canonico": "e:/out",
            "valor_esperado_canonico": "e:/out", "evidencia": [],
        }
        (base / "resultado.json").write_text(json.dumps(resultado), encoding="utf-8")
        """,
    )
    ejecutor = EjecutorGatePorHelper(comando=(sys.executable, str(script)))

    resultado = await ejecutor.ejecutar(
        _solicitud_de_ejemplo(), politica=PoliticaDeReintento.INICIO, timeout_segundos=5.0, intervalo_segundos=0.05
    )

    assert resultado.estado is EstadoPreflight.MATCH


async def test_el_helper_que_sale_con_codigo_no_cero_falla_cerrado(tmp_path: pathlib.Path) -> None:
    script = _escribir_script(
        tmp_path,
        "helper_muere.py",
        """
        import sys
        sys.stderr.write("boom")
        raise SystemExit(3)
        """,
    )
    ejecutor = EjecutorGatePorHelper(comando=(sys.executable, str(script)))

    resultado = await ejecutor.ejecutar(
        _solicitud_de_ejemplo(), politica=PoliticaDeReintento.INICIO, timeout_segundos=5.0, intervalo_segundos=0.05
    )

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert "código 3" in resultado.detalle


async def test_el_helper_que_responde_basura_falla_cerrado(tmp_path: pathlib.Path) -> None:
    script = _escribir_script(
        tmp_path,
        "helper_basura.py",
        """
        import pathlib, sys
        base = pathlib.Path(sys.argv[1])
        (base / "resultado.json").write_text("eso no es un resultado", encoding="utf-8")
        """,
    )
    ejecutor = EjecutorGatePorHelper(comando=(sys.executable, str(script)))

    resultado = await ejecutor.ejecutar(
        _solicitud_de_ejemplo(), politica=PoliticaDeReintento.INICIO, timeout_segundos=5.0, intervalo_segundos=0.05
    )

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert "ilegible" in resultado.detalle


async def test_el_helper_inexistente_falla_cerrado(tmp_path: pathlib.Path) -> None:
    ejecutor = EjecutorGatePorHelper(comando=(str(tmp_path / "no_existe.py"),))

    resultado = await ejecutor.ejecutar(
        _solicitud_de_ejemplo(), politica=PoliticaDeReintento.INICIO, timeout_segundos=1.0, intervalo_segundos=0.05
    )

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE


# ---------------------------------------------------------------------------
# Hard-hang: el helper BLOQUEADO INDEFINIDAMENTE
# ---------------------------------------------------------------------------

#: argv: [pid_path, directorio-del-canal]. El padre agrega el directorio al
#: final; el script lo ignora y usa su propio argv[1] como pid file.
_SCRIPT_BLOQUEADO = """
import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(3600)
"""


async def _esperar_pid(path: pathlib.Path, limite: float = 15.0) -> int:
    inicio = time.monotonic()
    while time.monotonic() - inicio < limite:
        if path.exists():
            texto = path.read_text(encoding="utf-8").strip()
            if texto:
                return int(texto)
        await asyncio.sleep(0.02)
    raise AssertionError("el helper bloqueado nunca escribió su pid")


def _procesos_con_el_script(script: pathlib.Path) -> list[int]:
    encontrados: list[int] = []
    for proceso in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proceso.info["cmdline"] or []
        except psutil.Error:  # pragma: no cover -- proceso muerto entre iter y lectura
            continue
        if any(str(script) in argumento for argumento in cmdline):
            encontrados.append(int(proceso.info["pid"]))
    return encontrados


async def test_hard_hang_deadline_externo_mata_y_reapea_el_helper(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El test central del defecto original: el helper no retorna NUNCA.

    Propiedades exigidas, en orden: el deadline externo devuelve al caller; el
    resultado es el fail-closed honesto (UIA_NO_DISPONIBLE); el helper YA NO
    EXISTE (kill + reap); no queda ningún proceso del mecanismo; y el
    directorio temporal del canal se borró.
    """
    pid_path = tmp_path / "pid.txt"
    script = _escribir_script(tmp_path, "helper_colgado.py", _SCRIPT_BLOQUEADO)
    ejecutor = EjecutorGatePorHelper(comando=(sys.executable, str(script), str(pid_path)), gracia_externa_segundos=0.4)

    # Se registra el directorio del canal para exigir su limpieza SIN parchear
    # la conducta: sólo se envuelve `mkdtemp`.
    creados: list[pathlib.Path] = []
    mkdtemp_real = ejecutor_mod.tempfile.mkdtemp

    def _mkdtemp(*args: object, **kwargs: object) -> str:
        creado = mkdtemp_real(*args, **kwargs)
        creados.append(pathlib.Path(creado))
        return creado

    monkeypatch.setattr(ejecutor_mod.tempfile, "mkdtemp", _mkdtemp)

    resultado = await ejecutor.ejecutar(
        _solicitud_de_ejemplo(), politica=PoliticaDeReintento.INICIO, timeout_segundos=0.2, intervalo_segundos=0.1
    )

    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert "deadline externo" in resultado.detalle

    pid = await _esperar_pid(pid_path)
    assert not psutil.pid_exists(pid), "el helper sobrevivió al deadline: el kill/reap no está garantizado"
    assert _procesos_con_el_script(script) == [], "quedó un helper del mecanismo corriendo"
    assert creados and all(not directorio.exists() for directorio in creados), (
        "el directorio del canal no se limpió en la salida"
    )


async def test_hard_hang_la_cancelacion_tambien_mata_el_helper(tmp_path: pathlib.Path) -> None:
    """Cancelar el await del ejecutor no puede dejar al helper colgado vivo."""
    pid_path = tmp_path / "pid_cancel.txt"
    script = _escribir_script(tmp_path, "helper_colgado_cancel.py", _SCRIPT_BLOQUEADO)
    ejecutor = EjecutorGatePorHelper(comando=(sys.executable, str(script), str(pid_path)), gracia_externa_segundos=30.0)

    tarea = asyncio.ensure_future(
        ejecutor.ejecutar(
            _solicitud_de_ejemplo(), politica=PoliticaDeReintento.INICIO, timeout_segundos=30.0, intervalo_segundos=0.1
        )
    )
    pid = await _esperar_pid(pid_path)
    assert psutil.pid_exists(pid)

    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    assert not psutil.pid_exists(pid), "la cancelación no mató al helper"
    assert _procesos_con_el_script(script) == []


async def test_el_ejecutor_en_proceso_no_mata_el_hilo_y_lo_deja_explicito() -> None:
    """El ejecutor de tests mantiene el CONTRATO aunque no pueda matar el hilo.

    Un hilo de Python bloqueado en una llamada C no se puede interrumpir: lo que
    se exige acá es que el resultado sea el fail-closed del puerto (nunca una
    excepción) y que la limitación quede dicha — la garantía dura la da el helper
    productivo. El hilo se libera al final para no dejar basura en la suite.
    """
    liberar = threading.Event()
    llamado = threading.Event()

    def _fabrica_colgada():
        llamado.set()
        liberar.wait(30)
        return None

    class _LocalizadorVacio:
        def procesos(self):
            return ()

    ejecutor = EjecutorGateEnProceso(
        fabrica_observador=_fabrica_colgada,
        localizador=_LocalizadorVacio(),
        gracia_externa_segundos=0.1,
    )
    resultado = await ejecutor.ejecutar(
        _solicitud_de_ejemplo(), politica=PoliticaDeReintento.INICIO, timeout_segundos=0.1, intervalo_segundos=0.05
    )

    assert llamado.is_set(), "el gate nunca llegó a ejecutarse"
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    liberar.set()

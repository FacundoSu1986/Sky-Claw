"""T5-v2 — gate productivo del preflight UIA sobre el seam de spawn.

**Qué prueba este archivo y qué NO.** Prueba el gate de extremo a extremo en el
único seam por donde cruzan los dos lanzadores (``_execute_process``): dado un
proceso recién lanzado y un observador UIA (falso, determinista), ¿el runner
continúa sólo con ``MATCH``, y ante ``MISMATCH``/``UNKNOWN`` termina el proceso
con el mismo ownership de siempre —sin huérfanos, con el Job cerrado, y con un
resultado de corrida fallida (no un crash)? El módulo de decisión T5A tiene su
propia suite (``test_dyndolod_uia_preflight.py``); acá se prueba el CABLEADO.

**Los fakes son de observación, nunca de mutación.** Los observadores falsos
implementan exactamente los tres métodos de ``ObservadorUIA``; el proceso falso
no toca Windows real. La cancelación DURANTE el gate se mide con el seam real
(``_execute_process``) y un gate colgado inyectado: lo que se verifica es la
propiedad del seam — ``CancelledError`` conserva identidad y el proceso muere —
no el interior del gate, que es puro y ya está cubierto.

**Congelamientos de esta suite.** Las razones transitorias (``RAZONES_
TRANSITORIAS_DE_INICIO``), los defaults del gate en ``DynDOLODConfig`` (== las
constantes del módulo del gate), el mapa structural "ambos lanzadores → un
seam → un gate", y la regla §21 (el gate jamás lee el header ``Using Output
Path:`` como prueba de destino: el rig T5A midió que ese header ecoea el argv
aunque las escrituras vayan al preset).
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.tools import dyndolod_runner as ddl
from sky_claw.local.tools.dyndolod_uia_gate import (
    GATE_UIA_INTERVALO_SEGUNDOS,
    GATE_UIA_TIMEOUT_SEGUNDOS,
    RAZONES_TRANSITORIAS_DE_INICIO,
    ejecutar_gate_sincrono,
)
from sky_claw.local.tools.dyndolod_uia_preflight import (
    ControlObservado,
    EstadoPreflight,
    ObservacionUIAError,
    ProcesoObservado,
    RazonPreflight,
    ResultadoPreflightUIA,
    SolicitudPreflightUIA,
    UIANoDisponibleError,
    VentanaObservada,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
MODULO_GATE = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_uia_gate.py"
MODULO_RUNNER = RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_runner.py"

#: La salida administrada, escrita en semántica Windows a propósito: es el valor
#: que ``dyndolod_output_target`` produciría en un rig real, y el gate tiene que
#: poder compararla en CUALQUIER plataforma de CI (la canonicalización del
#: preflight es puramente textual).
SALIDA_ADMINISTRADA = pathlib.PureWindowsPath(r"C:\Games\Skyrim Special Edition\Sky-Claw\DynDOLOD")
SALIDA_ADMINISTRADA_TXT = str(SALIDA_ADMINISTRADA)

OTRA_SALIDA = r"C:\Modding\DynDOLOD Out Test\_rig_test\output"

PID = 4242


# ---------------------------------------------------------------------------
# Dobles de prueba
# ---------------------------------------------------------------------------


class _EOFStream:
    async def read(self, _n: int) -> bytes:
        return b""


class _RelojFalso:
    """Reloj monotónico determinista: avanza sólo cuando el gate duerme."""

    def __init__(self) -> None:
        self._t = 0.0
        self.suenos: list[float] = []

    def __call__(self) -> float:
        return self._t

    def dormir(self, delta: float) -> None:
        self.suenos.append(delta)
        self._t += delta


class _LocalizadorFalso:
    def __init__(self, procesos: Sequence[ProcesoObservado]) -> None:
        self._procesos = tuple(procesos)

    def procesos(self) -> Sequence[ProcesoObservado]:
        return self._procesos


class _Ronda:
    """Un estado completo del árbol UIA para UNA ronda del gate."""

    def __init__(
        self,
        ventanas: Sequence[VentanaObservada] = (),
        controles: dict[object, Sequence[ControlObservado]] | None = None,
        valores: dict[object, str] | None = None,
    ) -> None:
        self.ventanas = tuple(ventanas)
        self.controles = controles or {}
        self.valores = valores or {}


class _ObservadorPorRonda:
    """Observador falso que entrega una :class:`_Ronda` por ronda del gate.

    Avanza el índice al INICIO de cada observación (primera llamada a
    ``ventanas_de_proceso``); todas las lecturas de esa ronda ven la misma foto.
    Se queda en la última ronda si el gate sigue preguntando.
    """

    def __init__(self, rondas: Sequence[_Ronda]) -> None:
        assert rondas, "un observador sin rondas no modela nada"
        self._rondas = list(rondas)
        self._indice = 0
        self.observaciones = 0

    def _actual(self) -> _Ronda:
        return self._rondas[min(self._indice, len(self._rondas) - 1)]

    def ventanas_de_proceso(self, pid: int) -> Sequence[VentanaObservada]:
        self.observaciones += 1
        self._indice = max(self._indice, self.observaciones - 1)
        return tuple(v for v in self._actual().ventanas if v.pid == pid)

    def controles_de_ventana(self, ventana: VentanaObservada) -> Sequence[ControlObservado]:
        return tuple(self._actual().controles.get(ventana.handle, ()))

    def leer_valor(self, control: ControlObservado) -> str | None:
        # La clave es exactamente la tupla que arma el preflight en
        # `observar_output`: (automation_id, nombre, tipo_de_control, pid).
        # Cambiarla es un ``VALOR_NO_LEIBLE`` espurio.
        valores = self._actual().valores
        compuesta = (control.automation_id, control.nombre, control.tipo_de_control, control.pid)
        return valores.get(compuesta)


class _ProcesoFalso:
    """El mínimo que ``_execute_process`` necesita de un asyncio Process."""

    def __init__(self, pid: int = PID) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = _EOFStream()
        self.stderr = _EOFStream()
        self.esperas = 0
        self.muertes = 0

    async def wait(self) -> int:
        self.esperas += 1
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.muertes += 1
        self.returncode = -9


# ---------------------------------------------------------------------------
# Constructores compartidos
# ---------------------------------------------------------------------------


def _ventana(pid: int = PID, handle: object = "w1") -> VentanaObservada:
    return VentanaObservada(pid=pid, titulo="TexGen 3.0 Alpha-209 x64 - SSE", class_name="TfrmMain", handle=handle)


def _control_output(
    pid: int = PID,
    *,
    automation_id: str = "6293158",
    clase: str = "TEdit",
) -> ControlObservado:
    """Un ``Edit``/``TEdit`` (selector medido de TexGen/DynDOLOD).

    ``pid`` es posicional a propósito: si el primer argumento fuera ``automation_id``,
    un call site como ``_control_output("a")`` —para distinguir dos controles
    homónimos del selector— convierte el pid en un string y ``_pid_es_legible`` lo
    recibe como ``"a" > 0``: ``TypeError`` → ``ERROR_UIA`` espurio.
    """
    return ControlObservado(pid=pid, automation_id=automation_id, nombre="", tipo_de_control="Edit", class_name=clase)


def _ronda_output(valor: str, pid: int = PID) -> _Ronda:
    """La foto del wizard con exactamente un Edit/TEdit mostrando ``valor``.

    La clave del dict es la MISMA tupla que el observador consulta
    (``(automation_id, nombre, tipo_de_control, pid)``); cambiarla es un MATCH
    espurio.
    """
    control = _control_output(pid)
    return _Ronda(
        ventanas=[_ventana(pid)],
        controles={"w1": [control]},
        valores={(control.automation_id, control.nombre, control.tipo_de_control, control.pid): valor},
    )


def _ronda_dos_candidatos(pid: int = PID) -> _Ronda:
    """Dos controles que satisfacen el mismo selector: ``CONTROL_AMBIGUO``."""
    return _Ronda(
        ventanas=[_ventana(pid)],
        controles={"w1": [_control_output(pid, automation_id="a"), _control_output(pid, automation_id="b")]},
    )


def _solicitud(pid: int | None = PID) -> SolicitudPreflightUIA:
    return SolicitudPreflightUIA(
        tool="TexGen",
        ejecutable_esperado=r"C:\Modding\DynDOLOD\TexGenx64.exe",
        salida_administrada_esperada=SALIDA_ADMINISTRADA_TXT,
        criterios_del_control=_selector(),
        pid=pid,
    )


def _selector():
    from sky_claw.local.tools.dyndolod_uia_preflight import selector_de_output

    return selector_de_output("TexGen")


def _config(
    tmp_path: pathlib.Path,
    *,
    timeout: float | None = None,
    intervalo: float | None = None,
) -> ddl.DynDOLODConfig:
    """Runner config; ``timeout``/``intervalo`` quedan en ``None`` para que el
    constructor de :class:`DynDOLODConfig` aplique los defaults del gate
    (no se pisan las constantes congeladas en :data:`GATE_UIA_*`)."""
    juego = tmp_path / "juego"
    juego.mkdir(exist_ok=True)
    exe = tmp_path / "DynDOLODx64.exe"
    exe.write_bytes(b"pe")
    mo2 = tmp_path / "mo2"
    mo2.mkdir(exist_ok=True)
    mods = tmp_path / "mods"
    mods.mkdir(exist_ok=True)
    return ddl.DynDOLODConfig(
        game_path=juego,
        mo2_path=mo2,
        mo2_mods_path=mods,
        dyndolod_exe=exe,
        output_root=SALIDA_ADMINISTRADA,
        uia_gate_timeout_seconds=timeout if timeout is not None else GATE_UIA_TIMEOUT_SEGUNDOS,
        uia_gate_poll_interval_seconds=intervalo if intervalo is not None else GATE_UIA_INTERVALO_SEGUNDOS,
    )


def _config_con_gate_corto(tmp_path: pathlib.Path) -> ddl.DynDOLODConfig:
    """Una config con deadline e intervalo cortos, para los tests del seam."""
    return _config(tmp_path, timeout=5.0, intervalo=1.0)


def _proceso_falso_del_rig(ejecutable: str) -> ProcesoObservado:
    """La fila de psutil que la identidad del gate espera encontrar."""
    return ProcesoObservado(
        pid=PID, nombre_ejecutable=pathlib.PureWindowsPath(ejecutable).name, ruta_ejecutable=ejecutable
    )


# ---------------------------------------------------------------------------
# Congelamientos del módulo del gate
# ---------------------------------------------------------------------------


def test_las_razones_transitorias_estan_congeladas():
    """Mover una razón de caja es una decisión: la igualdad lo exige en voz alta.

    Cada una tiene evidencia en el docstring del módulo (rig T5A: el modal de
    DynDOLOD esconde el TEdit del wizard; el proceso/ventana tardan en existir;
    un Edit recién creado puede no tener texto). Todo lo demás es terminal.
    """
    assert (
        frozenset(
            {
                RazonPreflight.PROCESO_NO_ENCONTRADO,
                RazonPreflight.PID_NO_COINCIDE,
                RazonPreflight.VENTANA_NO_ENCONTRADA,
                RazonPreflight.CONTROL_NO_ENCONTRADO,
                RazonPreflight.VALOR_NO_LEIBLE,
            }
        )
        == RAZONES_TRANSITORIAS_DE_INICIO
    )


def test_los_defaults_del_gate_en_config_son_las_constantes_del_gate(tmp_path):
    """Los defaults de config vienen del módulo del gate, no son otro literal."""
    config = _config(tmp_path)  # sin ``timeout``/``intervalo``: defaults del gate
    assert config.uia_gate_timeout_seconds == GATE_UIA_TIMEOUT_SEGUNDOS
    assert config.uia_gate_poll_interval_seconds == GATE_UIA_INTERVALO_SEGUNDOS


def test_el_presupuesto_invalido_es_un_error_de_programacion():
    """Un gate sin presupuesto de tiempo no decide: es un bug del caller."""
    for timeout, intervalo in ((0.0, 1.0), (5.0, 0.0), (-1.0, 1.0)):
        with pytest.raises(ValueError):
            ejecutar_gate_sincrono(
                _solicitud(),
                fabrica_observador=lambda: _ObservadorPorRonda([_ronda_output(SALIDA_ADMINISTRADA_TXT)]),
                localizador=_LocalizadorFalso([]),
                timeout_segundos=timeout,
                intervalo_segundos=intervalo,
            )


# ---------------------------------------------------------------------------
# El lazo del gate (unidad, con reloj falso — nada duerme de verdad)
# ---------------------------------------------------------------------------


def test_match_y_mismatch_salen_inmediatos_sin_reintento():
    """M-V2-1 / M-V2-2: un veredicto concluyente no se vuelve a consultar.

    Reintentar un MISMATCH sólo dilataría el bloqueo; reintentarlo "hasta que
    cambie" sería auto-remediación encubierta. Una sola observación, una salida.
    """
    reloj = _RelojFalso()
    for valor, estado in (
        (SALIDA_ADMINISTRADA_TXT, EstadoPreflight.MATCH),
        (OTRA_SALIDA, EstadoPreflight.MISMATCH),
    ):
        observador = _ObservadorPorRonda([_ronda_output(valor)])
        # ``fabrica_observador`` default-binding: el lambda captura por valor,
        # no por referencia — sin esto, la última iteración del loop afecta a
        # todas las anteriores (``B023``).
        resultado = ejecutar_gate_sincrono(
            _solicitud(),
            fabrica_observador=(lambda o=observador: o),
            localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
            timeout_segundos=300.0,
            intervalo_segundos=1.0,
            reloj=reloj,
            dormir=reloj.dormir,
        )
        assert resultado.estado is estado
        assert observador.observaciones == 1, "un veredicto concluyente se re-consultó"
        assert reloj.suenos == [], "un veredicto concluyente no debe esperar"


def test_un_unknown_terminal_no_se_reintenta():
    """M-V2-2 (variante estructural): la ambigüedad no mejora con el reloj."""
    reloj = _RelojFalso()
    observador = _ObservadorPorRonda([_ronda_dos_candidatos(pid=PID)])
    resultado = ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=lambda: observador,
        localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        timeout_segundos=300.0,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.CONTROL_AMBIGUO
    assert observador.observaciones == 1
    assert reloj.suenos == []


def test_un_error_del_sensor_no_se_reintenta():
    """ERROR_UIA es del sensor, no del timing de la GUI: terminal."""
    reloj = _RelojFalso()

    class _SensorRoto:
        def ventanas_de_proceso(self, pid):
            raise ObservacionUIAError("COM roto")

        def controles_de_ventana(self, ventana):
            return ()

        def leer_valor(self, control):
            return None

    resultado = ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=lambda: _SensorRoto(),  # type: ignore[arg-type,return-value]
        localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        timeout_segundos=300.0,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.ERROR_UIA
    assert reloj.suenos == []


def test_un_transitorio_se_reintenta_hasta_match_dentro_del_deadline():
    """G10 — arranque transitorio: sin ventana, luego sin control, luego MATCH.

    Es el orden de arranque real: el proceso nace (psutil puede no verlo aún),
    la ventana aparece, y con el modal de DynDOLOD el TEdit del wizard aparece
    sólo después de que el humano resuelva. El gate alcanza MATCH dentro del
    deadline sin reinventar el veredicto.
    """
    reloj = _RelojFalso()
    observador = _ObservadorPorRonda(
        [
            _Ronda(),  # ni ventana
            _Ronda(ventanas=[_ventana()]),  # ventana sin el control (modal)
            _ronda_output(SALIDA_ADMINISTRADA_TXT),  # wizard con el Output
        ]
    )
    resultado = ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=lambda: observador,
        localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        timeout_segundos=300.0,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
    )
    assert resultado.estado is EstadoPreflight.MATCH
    assert observador.observaciones == 3
    assert reloj.suenos == [1.0, 1.0], "duerme sólo entre rondas"


def test_el_deadline_agotado_devuelve_el_ultimo_unknown_y_es_acotado():
    """G11 — la ventana nunca aparece: fail closed con la ÚLTIMA razón.

    El número de rondas está acotado por el deadline (no hay retry infinito) y
    el resultado es el último UNKNOWN transitorio: la evidencia del corte es
    "se esperó y seguía sin aparecer", no un genérico.
    """
    reloj = _RelojFalso()
    observador = _ObservadorPorRonda([_Ronda()])  # nunca hay ventana
    resultado = ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=lambda: observador,
        localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        timeout_segundos=5.0,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.VENTANA_NO_ENCONTRADA
    # 6 rondas: la 1 sin dormir + una por cada segundo dentro del deadline.
    assert observador.observaciones == 6
    assert reloj.suenos == [1.0] * 5
    assert reloj() >= 5.0, "el deadline se respetó"


def test_el_sueno_nunca_se_pasa_del_deadline():
    """La última siesta se recorta contra el deadline: `min(intervalo, restante)`.

    Sin ese recorte, un "acotado" dejaba de serlo por hasta un intervalo entero.
    """
    reloj = _RelojFalso()
    ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=lambda: _ObservadorPorRonda([_Ronda()]),
        localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        timeout_segundos=2.5,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
    )
    assert reloj.suenos[-1] == 0.5, f"la última siesta excedió el deadline: {reloj.suenos}"


def test_un_proceso_muerto_corta_sin_esperar_el_deadline():
    """El binario murió a mitad del arranque: otra ronda de UIA no lo resucita."""
    reloj = _RelojFalso()
    observador = _ObservadorPorRonda([_Ronda()])
    resultado = ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=lambda: observador,
        localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        timeout_segundos=300.0,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
        proceso_vivo=lambda: False,  # el proceso ya no está
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert observador.observaciones == 1
    assert reloj.suenos == []


def test_la_fabrica_sin_backend_da_unknown_uia_no_disponible():
    """Sin plataforma/binding no hay veredicto: UNKNOWN inmediato, sin reintentos."""
    reloj = _RelojFalso()

    def _fabrica_rota():
        raise UIANoDisponibleError("no hay backend")

    resultado = ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=_fabrica_rota,
        localizador=_LocalizadorFalso([]),
        timeout_segundos=300.0,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
    )
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.UIA_NO_DISPONIBLE
    assert reloj.suenos == []


def test_al_progreso_reporta_solo_cambios_de_razon():
    """Una GUI sana no produce un log por segundo: sólo cuando CAMBIA la razón."""
    reloj = _RelojFalso()
    vistos: list[str] = []
    ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=lambda: _ObservadorPorRonda([_Ronda()]),
        localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        timeout_segundos=4.0,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
        al_progreso=lambda r: vistos.append(r.razon.value),
    )
    assert vistos == [RazonPreflight.VENTANA_NO_ENCONTRADA.value]


# ---------------------------------------------------------------------------
# El seam en el runner: G2..G9, G12, G13 — extremo a extremo con spawn falso
# ---------------------------------------------------------------------------


def _preparar_corrida(
    tmp_path: pathlib.Path,
    *,
    rondas: Sequence[_Ronda],
    ejecutable: str,
    timeout: float = 5.0,
):
    """Runner real (gate real) + spawn falso + espías de ownership del proceso."""
    config = _config(tmp_path, timeout=timeout)
    reloj = _RelojFalso()
    observador = _ObservadorPorRonda(rondas)
    runner = ddl.DynDOLODRunner(
        config,
        uia_localizador=_LocalizadorFalso([_proceso_falso_del_rig(ejecutable)]),
        uia_observador_factory=lambda: observador,
        uia_reloj=reloj,
        uia_dormir=reloj.dormir,
    )
    proc = _ProcesoFalso()
    return runner, proc, observador


def _espías_de_ownership(proc: _ProcesoFalso | None = None) -> dict[str, MagicMock | AsyncMock]:
    """Los tres patches de ownership que los tests del seam repiten.

    ``kill_and_reap`` se mocke con un ``side_effect`` que mata el proceso
    pasado si el test quiere medir la muerte efectiva: el mock por defecto
    (sin side_effect) NO llama a ``proc.kill()``, así que ``proc.muertes`` queda
    en cero y el test del seam que verifica la muerte del proceso (G12) falla
    sin que el seam sea el problema.

    Implementación: side_effect **sync** (no una coroutine), y que captura el
    proceso vía closure plana, porque un side_effect async dejaba coroutines
    internas (``_execute_mock_call``) que pytest reportaba en el teardown como
    "never awaited" — con ``filterwarnings = ["error::RuntimeWarning"]`` en
    pyproject, eso cruzaba la frontera entre tests y reventaba tests AST
    posteriores. Un callable sync que mata el proceso sortea la fuga.

    Devuelve un dict con los mocks ya construidos, para que los tests puedan
    asertarlos DESPUÉS del ``with``: star-unpacking dentro de un ``with`` no
    compila en 3.11, y guardar los mocks en un atributo de ``_PilaDeOwnership``
    permite asserts tanto dentro como fuera.
    """
    if proc is None:
        return {
            "assign_kill_on_close_job": MagicMock(return_value=4242),
            "kill_and_reap": AsyncMock(),
            "close_job": MagicMock(),
        }

    # Closure plana sobre el proc: sin ``async def`` anidada que retenga
    # un frame y le quede una coroutine sin consumir.
    def _mata(proceso: _ProcesoFalso) -> None:
        proceso.kill()

    return {
        "assign_kill_on_close_job": MagicMock(return_value=4242),
        "kill_and_reap": AsyncMock(side_effect=_mata),
        "close_job": MagicMock(),
    }


class _PilaDeOwnership:
    """Context manager que entra los tres espías de ownership (3.11-safe).

    Guarda los mocks en ``self.mocks`` para que los asserts del test los alcancen
    DESPUÉS de salir del ``with`` (los mocks de :mod:`unittest.mock` se restauran
    al cerrar el bloque y ya no tienen ``assert_*``).
    """

    def __init__(self, proc: _ProcesoFalso | None = None) -> None:
        self.mocks = _espías_de_ownership(proc=proc)
        self._cms = [patch.object(ddl, nombre, mock) for nombre, mock in self.mocks.items()]

    def __enter__(self):
        for cm in self._cms:
            cm.__enter__()
        return self

    def __exit__(self, *exc_info):
        for cm in reversed(self._cms):
            cm.__exit__(*exc_info)
        return False


def _spawn_de(proc):
    """Side_effect que retorna ``proc`` y se "await-ea" sincrónicamente.

    Un ``AsyncMock(return_value=proc)`` acumula coroutines internas
    (``_execute_mock_call``) que pytest reporta en el teardown como "never
    awaited" y, con ``filterwarnings = ["error::RuntimeWarning"]`` en
    pyproject, revientan tests AST que vienen después. Una función normal
    (sync) que retorna el proceso, usada como ``side_effect`` de un
    ``AsyncMock``, sortea esa fuga: la coroutine interna del mock existe sólo
    mientras dura el await del caller.
    """
    return proc


@pytest.mark.parametrize(
    ("tool", "ejecutable", "valor"),
    [
        ("TexGen", r"C:\Modding\DynDOLOD\TexGenx64.exe", OTRA_SALIDA),
        ("DynDOLOD", r"C:\Modding\DynDOLOD\DynDOLODx64.exe", OTRA_SALIDA),
    ],
)
async def test_g2_g5_mismatch_bloquea_y_mata_al_proceso(tmp_path, tool, ejecutable, valor):
    """G2/G5 — MISMATCH: no continúa, el proceso muere, el Job se cierra."""
    runner, proc, _observador = _preparar_corrida(
        tmp_path, rondas=[_ronda_output(valor, pid=PID)], ejecutable=ejecutable
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership() as pila,
        pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], tool)

    resultado = excinfo.value.resultado
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert resultado.razon is RazonPreflight.OUTPUT_DIFIERE
    assert resultado.pid == PID
    assert proc.esperas == 0, "la corrida no continuó a la espera normal"
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
    pila.mocks["close_job"].assert_called_once_with(4242)


@pytest.mark.parametrize(
    ("tool", "ejecutable"),
    [
        ("TexGen", r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        ("DynDOLOD", r"C:\Modding\DynDOLOD\DynDOLODx64.exe"),
    ],
)
async def test_g3_g6_unknown_bloquea_y_mata_al_proceso(tmp_path, tool, ejecutable):
    """G3/G6 — UNKNOWN terminal (dos candidatos): mismo fail-closed."""
    runner, proc, _observador = _preparar_corrida(
        tmp_path, rondas=[_ronda_dos_candidatos(pid=PID)], ejecutable=ejecutable
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership() as pila,
        pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], tool)

    assert excinfo.value.resultado.estado is EstadoPreflight.UNKNOWN
    assert excinfo.value.resultado.razon is RazonPreflight.CONTROL_AMBIGUO
    assert proc.esperas == 0
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
    pila.mocks["close_job"].assert_called_once_with(4242)


@pytest.mark.parametrize(
    ("tool", "ejecutable"),
    [
        ("TexGen", r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        ("DynDOLOD", r"C:\Modding\DynDOLOD\DynDOLODx64.exe"),
    ],
)
async def test_g4_g7_match_permite_continuar_al_camino_normal(tmp_path, tool, ejecutable):
    """G4/G7 — MATCH: la ejecución sigue a la espera normal (sin kills)."""
    runner, proc, _observador = _preparar_corrida(
        tmp_path, rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT, pid=PID)], ejecutable=ejecutable
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership() as pila,
    ):
        stdout, stderr, return_code, _duracion = await runner._execute_process(pathlib.Path(ejecutable), [], tool)

    assert return_code == 0
    assert proc.esperas == 1, "con MATCH se llega a la espera normal del proceso"
    assert proc.muertes == 0
    pila.mocks["kill_and_reap"].assert_not_awaited()
    # La salida normal TAMBIÉN cierra el job (contrato U-07 preexistente).
    pila.mocks["close_job"].assert_called_once_with(4242)


@pytest.mark.parametrize(
    ("tool", "ejecutable"),
    [
        ("TexGen", r"C:\Modding\DynDOLOD\TexGenx64.exe"),
        ("DynDOLOD", r"C:\Modding\DynDOLOD\DynDOLODx64.exe"),
    ],
)
async def test_g8_g9_cero_o_dos_candidatos_bloquean_al_agotar_el_deadline(tmp_path, tool, ejecutable):
    """G8/G9 — cero candidatos (CONTROL_NO_ENCONTRADO transitorio) y dos
    candidatos (CONTROL_AMBIGUO terminal): UNKNOWN y bloqueo, sin elegir
    nunca "el primero" (M-V2-7). El caso transitorio espera su deadline acotado
    (reloj falso: nada duerme de verdad) y sale con la última razón."""
    # Cero candidatos: transitorio → deadline (5 s de reloj falso) → UNKNOWN.
    runner, proc, observador = _preparar_corrida(
        tmp_path, rondas=[_Ronda(ventanas=[_ventana()])], ejecutable=ejecutable
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership() as pila,
        pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(ejecutable), [], tool)
    assert excinfo.value.resultado.razon is RazonPreflight.CONTROL_NO_ENCONTRADO
    assert observador.observaciones == 6, "reintentó acotado por el deadline y falló cerrado"
    assert proc.esperas == 0
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)

    # Dos candidatos: terminal inmediato.
    runner2, proc2, _obs2 = _preparar_corrida(tmp_path, rondas=[_ronda_dos_candidatos(pid=PID)], ejecutable=ejecutable)
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc2))),
        _PilaDeOwnership() as pila2,
        pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo2,
    ):
        await runner2._execute_process(pathlib.Path(ejecutable), [], tool)
    assert excinfo2.value.resultado.razon is RazonPreflight.CONTROL_AMBIGUO
    pila2.mocks["kill_and_reap"].assert_awaited_once_with(proc2)


async def test_g12_cancelacion_durante_el_gate_no_deja_huerfanos(tmp_path):
    """G12 — cancelar durante el gate: kill/reap + job cerrado + identidad.

    El gate inyectado se cuelga a propósito: lo que se mide es el seam — que la
    cancelación del await del gate ejecute el cleanup y re-raise CancelledError
    sin convertirla en un fallo genérico de ejecución.
    """
    runner, proc, _observador = _preparar_corrida(
        tmp_path, rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT)], ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"
    )
    gate_empezo = asyncio.Event()
    liberar_gate = asyncio.Event()

    async def _gate_colgado(**_kwargs):
        gate_empezo.set()
        await liberar_gate.wait()

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        patch.object(runner, "_gate_uia_output", AsyncMock(side_effect=_gate_colgado)),
        _PilaDeOwnership(proc=proc) as pila,
    ):
        task = asyncio.create_task(
            runner._execute_process(pathlib.Path(r"C:\Modding\DynDOLOD\TexGenx64.exe"), [], "TexGen")
        )
        try:
            await asyncio.wait_for(gate_empezo.wait(), timeout=5.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            liberar_gate.set()
        # Drenar cualquier coroutine interna de AsyncMock que haya quedado
        # pendiente por la cancelación, antes de que el teardown de pytest
        # la reporte como "never awaited" y reviente un test AST posterior.
        await asyncio.sleep(0)

    assert proc.esperas == 0
    assert proc.muertes == 1, "el proceso se mató"
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
    pila.mocks["close_job"].assert_called_once_with(4242)


async def test_g13_el_esperado_del_gate_es_el_mismo_que_el_o_administrado(tmp_path):
    """G13/M-V2-10 — UNA fuente: la solicitud del gate y el `-o:` salen del
    MISMO ``config.output_root``. Mutar cualquiera de las dos puntas rompe."""
    runner, proc, _observador = _preparar_corrida(
        tmp_path, rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT)], ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"
    )
    capturadas: dict[str, object] = {}

    def _espia(solicitud, **_kwargs):
        capturadas["solicitud"] = solicitud
        capturadas["argv"] = None
        return ResultadoPreflightUIA(
            estado=EstadoPreflight.MATCH,
            razon=RazonPreflight.OUTPUT_COINCIDE,
            tool=solicitud.tool,
            detalle="espía",
            valor_esperado=solicitud.salida_administrada_esperada,
            pid=solicitud.pid,
        )

    args_capturados: list[list[str]] = []

    async def _spawn_falso(_exe, *args, **_kwargs):
        args_capturados.append(list(args))
        return proc

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=_spawn_falso)),
        patch.object(ddl, "ejecutar_gate_sincrono", _espia),
        patch.object(ddl, "close_job", MagicMock()),
        patch.object(ddl, "assign_kill_on_close_job", MagicMock(return_value=4242)),
    ):
        args = runner._build_xedit_args(None)
        await runner._execute_process(pathlib.Path(r"C:\Modding\DynDOLOD\TexGenx64.exe"), args, "TexGen")

    solicitud = capturadas["solicitud"]
    assert isinstance(solicitud, SolicitudPreflightUIA)
    assert solicitud.salida_administrada_esperada == str(runner._config.output_root)
    # Y el argv lleva EXACTAMENTE el -o: que esa misma fuente produce:
    esperado_o = runner._switch_de_ruta("o", runner._config.output_root, directorio=True)
    assert esperado_o in args_capturados[0]
    assert args_capturados[0].index(esperado_o) == args.index(esperado_o)


# ---------------------------------------------------------------------------
# Contrato del fallo hacia capas superiores (§20) y bordes del seam
# ---------------------------------------------------------------------------


def test_el_mensaje_distingue_mismatch_de_inconcluso():
    """No es "tool crashed": el fallo nombra preflight UIA + estado + razón."""
    mismatch = ResultadoPreflightUIA(
        estado=EstadoPreflight.MISMATCH,
        razon=RazonPreflight.OUTPUT_DIFIERE,
        tool="TexGen",
        detalle="la GUI muestra otra ruta",
        valor_esperado=SALIDA_ADMINISTRADA_TXT,
        valor_observado=OTRA_SALIDA,
        pid=PID,
    )
    inconcluso = ResultadoPreflightUIA(
        estado=EstadoPreflight.UNKNOWN,
        razon=RazonPreflight.VENTANA_NO_ENCONTRADA,
        tool="TexGen",
        detalle="sin ventana",
        valor_esperado=SALIDA_ADMINISTRADA_TXT,
        pid=PID,
    )
    mensaje_mismatch = str(ddl.DynDOLODPreflightUIAError(mismatch))
    mensaje_inconcluso = str(ddl.DynDOLODPreflightUIAError(inconcluso))

    for mensaje in (mensaje_mismatch, mensaje_inconcluso):
        assert "preflight UIA" in mensaje
        assert "TexGen" in mensaje
        assert str(PID) in mensaje
    assert "MISMATCH" in mensaje_mismatch and "OUTPUT_DIFIERE" in mensaje_mismatch
    assert "UNKNOWN" in mensaje_inconcluso
    # ``repr`` de la cadena observada: el mensaje lo embebe con ``!r``, así que
    # el valor original aparece con backslashes cuádruples (uno por nivel:
    # raw-string + repr). Buscamos la versión canónica para evitar
    # falsos negativos del escapado.
    assert repr(OTRA_SALIDA) in mensaje_mismatch, "el observado viaja en la evidencia"
    # La excepción es de la familia que run_full_pipeline traduce a corrida
    # fallida, no un crash sin manejar:
    assert isinstance(ddl.DynDOLODPreflightUIAError(mismatch), ddl.DynDOLODExecutionError)


@pytest.mark.parametrize("tool", ["TexGen", "DynDOLOD"])
async def test_g2_g5_el_fallo_del_gate_llega_como_corrida_fallida_al_pipeline(tmp_path, tool):
    """§20 — ``run_full_pipeline`` traduce el bloqueo a ``success=False`` con el
    mensaje del gate, para AMBOS hermanos (no "tool crashed").

    El veredicto del gate ya está cubierto de extremo a extremo por
    ``test_g2_g5``; acá se prueba SÓLO la traducción del service layer: el gate
    (parcheado) lanza :class:`DynDOLODPreflightUIAError`, la etapa la relanza y
    ``run_full_pipeline`` la atrapa como ``DynDOLODExecutionError`` y la reporta
    como corrida fallida con el mensaje del gate.
    """
    runner, _proc, _obs = _preparar_corrida(
        tmp_path, rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT)], ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe"
    )
    bloqueo = ddl.DynDOLODPreflightUIAError(
        ResultadoPreflightUIA(
            estado=EstadoPreflight.MISMATCH,
            razon=RazonPreflight.OUTPUT_DIFIERE,
            tool=tool,
            detalle="la GUI muestra otra ruta",
            valor_esperado=SALIDA_ADMINISTRADA_TXT,
            pid=PID,
        )
    )

    def _lanzar_bloqueo(*_a, **_k):
        """Side_effect sync que ``raise`` la excepción sin coroutine intermedia.

        Pasar la excepción directamente como ``side_effect`` deja un wrapper
        coroutine interno en el AsyncMock que se reporta como "never
        awaited" en el teardown de pytest cuando el await se interrumpe
        por el raise. Un callable sync que lanza la excepción sortea eso.
        """
        raise bloqueo

    with (
        patch.object(runner, "_execute_process", AsyncMock(side_effect=_lanzar_bloqueo)),
        # ``_firmas_de_salida`` y ``_firma_del_log`` se ejecutan vía
        # ``asyncio.to_thread`` (sync); parchearlas con ``MagicMock`` evita
        # que cada ``AsyncMock`` acumule su coroutine interna y reviente
        # un test AST posterior con un unraisable warning.
        patch.object(runner, "_firmas_de_salida", MagicMock(return_value={})),
        patch.object(runner, "_firma_del_log", MagicMock(return_value=None)),
    ):
        if tool == "TexGen":
            resultado = await runner.run_full_pipeline(run_texgen=True, preset="Medium")
        else:
            # DynDOLOD: TexGen se inyecta exitoso para que el pipeline
            # llegue hasta DynDOLOD y falle ahí por el gate.
            with patch.object(
                runner,
                "run_texgen",
                AsyncMock(
                    return_value=ddl.ToolExecutionResult(
                        success=True, tool_name="TexGen", return_code=0, stdout="", stderr=""
                    )
                ),
            ):
                resultado = await runner.run_full_pipeline(run_texgen=True, preset="Medium")
    # Drenar el event loop: ``_execute_process`` se llama como ``await
    # mock(...)``, lo que crea una coroutine interna del AsyncMock que en
    # 3.11 queda como "never awaited" al destruirse el patch si el await
    # se interrumpió por una excepción. Sin el sleep, el teardown de
    # pytest reventaría un test AST posterior.
    await asyncio.sleep(0)

    assert resultado.success is False
    errores = " | ".join(resultado.errors)
    assert "preflight UIA" in errores, errores
    assert tool in errores, errores
    if tool == "TexGen":
        assert resultado.texgen_result is not None
        assert resultado.texgen_result.success is False
    else:
        assert resultado.dyndolod_result is not None
        assert resultado.dyndolod_result.success is False


async def test_una_tool_sin_selector_medido_bloquea_por_construccion(tmp_path):
    """Una tercera tool sin decisión de selector no hereda un MATCH por omisión."""
    runner, proc, _observador = _preparar_corrida(
        tmp_path, rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT)], ejecutable=r"C:\Modding\Otro\Otrox64.exe"
    )
    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership() as pila,
        pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo,
    ):
        await runner._execute_process(pathlib.Path(r"C:\Modding\Otro\Otrox64.exe"), [], "OtraCosa")
    assert excinfo.value.resultado.razon is RazonPreflight.TOOL_DESCONOCIDA
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)


# ---------------------------------------------------------------------------
# Anclas estructurales: el gate es propiedad del MECANISMO, no de un launcher
# ---------------------------------------------------------------------------


def _funcion_del_runner(nombre: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """La ``def``/``async def`` de nombre ``nombre`` en el runner.

    `ast.AsyncFunctionDef` **es** un `ast.FunctionDef` (hereda), pero el isinstance
    contra la subclase más específica del árbol del runner puede romperse si un
    día cambia la forma: aceptar las dos es la opción que hace que el ancla
    sobreviva al refactor.
    """
    arbol = ast.parse(MODULO_RUNNER.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"{MODULO_RUNNER.name} ya no define {nombre!r}: revisá estas anclas")


def test_los_dos_lanzadores_pasan_por_el_unico_seam_gateado():
    """G-mecanismo — TexGen y DynDOLOD son HERMANOS y el gate los cubre a ambos.

    Congelado por AST: ``run_texgen`` y ``run_dyndolod`` llaman a
    ``_execute_process``, y ``_execute_process`` —el ÚNICO ``create_subprocess_exec``
    del módulo— llama al gate ANTES de crear las tasks de monitoreo. Aplicar el
    gate sólo a uno (M-V2-3/M-V2-4) o moverlo después de la espera rompe acá.
    """
    for lanzador in ("run_texgen", "run_dyndolod"):
        llamadas = {
            hijo.func.attr
            for hijo in ast.walk(_funcion_del_runner(lanzador))
            if isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute)
        }
        assert "_execute_process" in llamadas, f"{lanzador} no pasa por el seam gateado"

    seam = _funcion_del_runner("_execute_process")
    llamadas_seam = {
        hijo.func.attr for hijo in ast.walk(seam) if isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute)
    }
    assert "_gate_uia_output" in llamadas_seam, "_execute_process dejó de llamar al gate"

    arbol_modulo = ast.parse(MODULO_RUNNER.read_text(encoding="utf-8"))
    spawns = [
        nodo
        for nodo in ast.walk(arbol_modulo)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "create_subprocess_exec"
    ]
    assert len(spawns) == 1, f"apareció un segundo spawn de proceso: {_ast_ubicaciones(spawns)}"


def _ast_ubicaciones(nodos: list[ast.Call]) -> list[str]:
    return [f"línea {n.lineno}" for n in nodos]


def test_el_gate_corre_antes_de_la_espera_y_de_los_drains():
    """G-posición — el gate está DESPUÉS del spawn y ANTES de considerar lista
    la GUI: su llamada precede a la creación de las tasks de drenaje (y por
    tanto al ``wait_for`` de la espera normal). Después del proceso sería tarde;
    antes del spawn no habría ventana que observar."""
    seam = _funcion_del_runner("_execute_process")
    linea_del_gate = next(
        nodo.lineno
        for nodo in ast.walk(seam)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "_gate_uia_output"
    )
    primera_task = min(
        (
            nodo.lineno
            for nodo in ast.walk(seam)
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "create_task"
        ),
        default=None,
    )
    assert primera_task is not None, "_execute_process ya no crea tasks de monitoreo"
    assert linea_del_gate < primera_task, "el gate quedó DESPUÉS de las tasks: la GUI se daría por lista sin veredicto"


def test_el_cleanup_del_gate_reusa_kill_and_reap_y_close_job():
    """§16/§17 — el gate NO crea otro sistema de ownership: reusa los helpers
    existentes (``kill_and_reap`` + ``close_job``) y conserva la identidad de
    ``CancelledError``. Congelado por AST sobre el bloque ``except`` del seam.

    El gate se invoca DENTRO del ``try``; el ``except`` que lo acompaña es el que
    cubre ``DynDOLODPreflightUIAError`` y ``asyncio.CancelledError`` a la vez y
    cuyo cuerpo llama a ``kill_and_reap`` + ``close_job`` antes del ``raise``.

    El runner tiene OTROS ``except`` que también llaman a ``kill_and_reap`` +
    ``close_job`` (los de timeout / ``Exception``), pero son ``ast.Name``
    simples, no ``ast.Tuple``. El handler del gate es el único con tipo tupla
    de más de un elemento: esa es la firma inequívoca del coverage compartido
    que pidió la review.
    """
    seam = _funcion_del_runner("_execute_process")
    handlers = [hijo for hijo in ast.walk(seam) if isinstance(hijo, ast.ExceptHandler)]
    handlers_del_gate = [
        h
        for h in handlers
        if isinstance(h.type, ast.Tuple)
        and len(h.type.elts) >= 2
        and any("DynDOLODPreflightUIAError" in ast.dump(elt) for elt in h.type.elts)
        and any(
            isinstance(n, ast.Call)
            and isinstance(n.func, (ast.Name, ast.Attribute))
            and (getattr(n.func, "id", None) == "kill_and_reap" or getattr(n.func, "attr", None) == "kill_and_reap")
            for n in ast.walk(h)
        )
        and any(
            isinstance(n, ast.Call)
            and isinstance(n.func, (ast.Name, ast.Attribute))
            and (getattr(n.func, "id", None) == "close_job" or getattr(n.func, "attr", None) == "close_job")
            for n in ast.walk(h)
        )
        and any(isinstance(n, ast.Raise) and n.exc is None for n in ast.walk(h))
    ]
    assert len(handlers_del_gate) == 1, (
        "el seam no tiene un único except con tipo tupla que cubra "
        "DynDOLODPreflightUIAError + CancelledError y limpie con kill_and_reap + "
        "close_job antes del raise"
    )
    handler = handlers_del_gate[0]
    # Cubre ambas excepciones del gate en UNA sola cláusula (tuple), para que el
    # cleanup sea idéntico en bloqueo y en cancelación.
    tipos_dump = {ast.dump(elt) for elt in handler.type.elts}
    assert any("DynDOLODPreflightUIAError" in t for t in tipos_dump)
    assert any("CancelledError" in t for t in tipos_dump)


def test_el_gate_no_lee_el_header_del_log_como_prueba_de_destino():
    """§21/M-V2-11 — ``Using Output Path:`` NO es attestation del destino físico.

    El rig T5A midió la divergencia: el header ecoea el ``-o:`` mientras las
    escrituras van al preset. El gate no puede leer jamás ese header ni el log
    del binario: su única evidencia es la observación UIA contra la salida
    administrada.
    """
    fuente_gate = MODULO_GATE.read_text(encoding="utf-8")
    assert "Using Output Path" not in fuente_gate
    assert "_ruta_del_log" not in fuente_gate

    metodo = _funcion_del_runner("_gate_uia_output")
    literales = {n.value for n in ast.walk(metodo) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not any("Using Output Path" in literal for literal in literales)
    atributos = {n.attr for n in ast.walk(metodo) if isinstance(n, ast.Attribute)}
    assert "_ruta_del_log" not in atributos and "_leer_log" not in atributos


# ---------------------------------------------------------------------------
# F1/F3/F4 — deadline externo al COM, ownership universal post-spawn y ciclo
# de vida del apartamento COM (reviews adversariales de #528)
# ---------------------------------------------------------------------------


async def test_f3_una_excepcion_no_prevista_del_gate_limpia_proceso_y_job(tmp_path):
    """F3 — un fallo no normalizado del gate NO deja el proceso sin ownership.

    La tupla ``(DynDOLODPreflightUIAError, CancelledError)`` no cubre un bug del
    adapter ni un error del executor: un ``RuntimeError`` de la fábrica (dentro
    del hilo del gate, vía ``to_thread``) tiene que limpiar igual: kill+reap del
    proceso, cierre del Job, y la excepción ORIGINAL propagada con su identidad
    (no degradada a ``DynDOLODExecutionError`` genérico).
    """
    runner, proc, _observador = _preparar_corrida(
        tmp_path,
        rondas=[_ronda_output(SALIDA_ADMINISTRADA_TXT)],
        ejecutable=r"C:\Modding\DynDOLOD\TexGenx64.exe",
    )

    def _fabrica_rota():
        raise RuntimeError("boom")

    runner._uia_observador_factory = _fabrica_rota

    with (
        patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
        _PilaDeOwnership(proc=proc) as pila,
        pytest.raises(RuntimeError, match="boom"),
    ):
        await runner._execute_process(pathlib.Path(r"C:\Modding\DynDOLOD\TexGenx64.exe"), [], "TexGen")

    assert proc.muertes == 1, "el proceso se mató pese a la excepción no prevista"
    pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
    pila.mocks["close_job"].assert_called_once_with(4242)


async def test_f1_un_com_bloqueado_dispara_el_deadline_externo_y_limpia(tmp_path, monkeypatch):
    """F1 — una observación bloqueada más allá del deadline NO cuelga el pipeline.

    El deadline interno del gate sólo se consulta ENTRE observaciones: una
    llamada que no vuelve jamás dejaría el ``to_thread`` corriendo y el pipeline
    esperando. El deadline EXTERNO (``asyncio.wait_for`` sobre el ``to_thread``)
    devuelve el control de forma acotada, termina el proceso, cierra el Job y
    reporta UNKNOWN/ERROR_UIA — fail-closed. El test no espera segundos reales:
    el deadline interno se configura corto y la gracia externa se monkeypatchea.

    Lo que el test NO afirma (limitación documentada): cancelar el ``await`` no
    mata al worker del pool ni a la llamada bloqueada dentro de él; el worker se
    libera cuando esa llamada retorna. Por eso el test libera al worker antes de
    terminar.
    """
    import threading
    import time

    listo = threading.Event()

    class _ObservadorBloqueado:
        """La primera observación queda bloqueada hasta que el test la libera."""

        def ventanas_de_proceso(self, pid: int):
            listo.wait(timeout=10)
            raise ObservacionUIAError("liberado por el test")

        def controles_de_ventana(self, ventana):
            return ()

        def leer_valor(self, control):
            return None

    monkeypatch.setattr(ddl, "_GRACIA_EXTERNA_DEL_GATE_SEGUNDOS", 0.1)

    config = _config(tmp_path, timeout=0.2, intervalo=0.05)
    runner = ddl.DynDOLODRunner(
        config,
        uia_localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        uia_observador_factory=_ObservadorBloqueado,
    )
    proc = _ProcesoFalso()
    try:
        with (
            patch.object(ddl.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: _spawn_de(proc))),
            _PilaDeOwnership(proc=proc) as pila,
        ):
            inicio = time.monotonic()
            with pytest.raises(ddl.DynDOLODPreflightUIAError) as excinfo:
                await runner._execute_process(pathlib.Path(r"C:\Modding\DynDOLOD\TexGenx64.exe"), [], "TexGen")
            duracion = time.monotonic() - inicio

        assert excinfo.value.resultado.estado is EstadoPreflight.UNKNOWN
        assert duracion < 5.0, "el pipeline recuperó el control de forma acotada"
        pila.mocks["kill_and_reap"].assert_awaited_once_with(proc)
        pila.mocks["close_job"].assert_called_once_with(4242)
    finally:
        # Liberar al worker del pool: sin esto, el hilo queda bloqueado y los
        # workers de ``to_thread`` se joinean al cierre del intérprete.
        listo.set()
        await asyncio.sleep(0.3)


class _ObservadorQueSeLibera:
    """Observador falso con ciclo de vida explícito (protocolo de liberación)."""

    def __init__(self, rondas: Sequence[_Ronda]) -> None:
        self._interno = _ObservadorPorRonda(rondas)
        self.liberaciones = 0

    def ventanas_de_proceso(self, pid: int):
        return self._interno.ventanas_de_proceso(pid)

    def controles_de_ventana(self, ventana):
        return self._interno.controles_de_ventana(ventana)

    def leer_valor(self, control):
        return self._interno.leer_valor(control)

    def liberar(self) -> None:
        self.liberaciones += 1


def _correr_gate_con_observador(observador) -> ResultadoPreflightUIA:
    reloj = _RelojFalso()
    return ejecutar_gate_sincrono(
        _solicitud(),
        fabrica_observador=lambda: observador,
        localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
        timeout_segundos=5.0,
        intervalo_segundos=1.0,
        reloj=reloj,
        dormir=reloj.dormir,
        proceso_vivo=lambda: True,
    )


def test_f4_el_gate_libera_al_observador_en_match():
    observador = _ObservadorQueSeLibera([_ronda_output(SALIDA_ADMINISTRADA_TXT)])
    resultado = _correr_gate_con_observador(observador)
    assert resultado.estado is EstadoPreflight.MATCH
    assert observador.liberaciones == 1, "CoInitialize sin CoUninitialize: el apartamento queda abierto"


def test_f4_el_gate_libera_al_observador_en_mismatch():
    observador = _ObservadorQueSeLibera([_ronda_output(OTRA_SALIDA)])
    resultado = _correr_gate_con_observador(observador)
    assert resultado.estado is EstadoPreflight.MISMATCH
    assert observador.liberaciones == 1


def test_f4_el_gate_libera_al_observador_en_unknown_terminal():
    observador = _ObservadorQueSeLibera([_ronda_dos_candidatos()])
    resultado = _correr_gate_con_observador(observador)
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert observador.liberaciones == 1


def test_f4_el_gate_libera_al_observador_cuando_la_observacion_falla():
    class _ObservadorQueFalla(_ObservadorQueSeLibera):
        def ventanas_de_proceso(self, pid: int):
            raise ObservacionUIAError("sensor roto")

        def __init__(self) -> None:
            self.liberaciones = 0

    observador = _ObservadorQueFalla()
    resultado = _correr_gate_con_observador(observador)
    assert resultado.estado is EstadoPreflight.UNKNOWN
    assert resultado.razon is RazonPreflight.ERROR_UIA
    assert observador.liberaciones == 1, "incluso con la observación rota, el apartamento se cierra"


def test_f4_el_gate_libera_al_observador_si_algo_inesperado_explota():
    """Una excepción NO-ObservacionUIAError dentro del gate: igual se libera."""

    class _ObservadorConBug(_ObservadorQueSeLibera):
        def controles_de_ventana(self, ventana):
            raise RuntimeError("bug del adapter")

    observador = _ObservadorConBug([_ronda_output(SALIDA_ADMINISTRADA_TXT)])
    # ``RuntimeError`` no está en ``_ERRORES_DE_ADAPTADOR``: propaga con
    # identidad (no se disfraza de fallo del sensor), pero la liberación
    # del observador ocurre igual en el ``finally`` del gate.
    with pytest.raises(RuntimeError, match="bug del adapter"):
        _correr_gate_con_observador(observador)
    assert observador.liberaciones == 1


def test_f4_un_observador_sin_protocolo_de_liberacion_no_rompe_el_gate():
    """Backwards-compat: un observador sin ``liberar`` sigue funcionando igual."""
    observador = _ObservadorPorRonda([_ronda_output(SALIDA_ADMINISTRADA_TXT)])
    resultado = _correr_gate_con_observador(observador)
    assert resultado.estado is EstadoPreflight.MATCH


def test_f3_una_fabrica_con_bug_no_se_traga_dentro_del_gate():
    """F3 a nivel gate: un RuntimeError de la fábrica propaga con identidad.

    El gate traduce ``ObservacionUIAError`` a UNKNOWN/UIA_NO_DISPONIBLE; un bug
    del adapter (``RuntimeError``, ``ValueError``…) NO se disfraza de fallo del
    rig: sale con su tipo para que el seam lo limpie con ownership.
    """

    def _fabrica_rota():
        raise RuntimeError("boom")

    reloj = _RelojFalso()
    with pytest.raises(RuntimeError, match="boom"):
        ejecutar_gate_sincrono(
            _solicitud(),
            fabrica_observador=_fabrica_rota,
            localizador=_LocalizadorFalso([_proceso_falso_del_rig(r"C:\Modding\DynDOLOD\TexGenx64.exe")]),
            timeout_segundos=5.0,
            intervalo_segundos=1.0,
            reloj=reloj,
            dormir=reloj.dormir,
            proceso_vivo=lambda: True,
        )


def test_observar_output_sigue_siendo_la_unicas_decision():
    """El gate delega el veredicto en el módulo puro: no re-implementa la
    comparación (que es donde un MISMATCH se convertiría en MATCH)."""
    fuente_gate = MODULO_GATE.read_text(encoding="utf-8")
    arbol = ast.parse(fuente_gate)
    usos = {nodo.func.id for nodo in ast.walk(arbol) if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)}
    assert "observar_output" in usos, "el gate dejó de delegar el veredicto en el preflight"

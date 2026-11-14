"""#575 §3: ciclo de vida, aislamiento y presión del EventBus de la GUI.

Decisiones que estos tests anclan (``sky_claw/app/gui/gui_event_adapter.py``):

- **Sigue siendo un singleton deliberado.** Los controladores ya reciben el bus por
  inyección; el singleton es solo el default del composition root, y hacerlo DI exigiría
  editar ``_bootloader.py`` y ``sky_claw_gui.py``. Para tests hay un aislamiento soportado
  (``EventBus.aislado()``) en vez de tocar ``EventBus._instance`` a mano.
- **No hay ``maxsize`` ni descarte.** Medido: con el loop bloqueado, la cola propia del bus
  queda en 0 (el hilo procesador reenvía al instante con ``call_soon_threadsafe``) y el
  backlog se acumula en la cola interna del *loop*; acotar ``_event_queue`` no acotaría la
  memoria. Y hoy ningún productor publica telemetría de alta frecuencia (ver el ancla de
  abajo). Nada se descarta: lo único que se hace es que lo no entregado sea **ruidoso**.
- **Lo no despachado no es silencioso**: ``stop()`` avisa cuántos eventos quedaron sin que el hilo
  procesador los tomara y ``publish()`` tras ``stop()`` avisa (el evento se retiene hasta un
  ``start()``). Lo ya reenviado al loop no se cuenta: ver el test del loop ocupado.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest

from sky_claw.app.gui.gui_event_adapter import EventBus, EventType, SkyClawEvent, event_bus

_LOGGER = "SkyClaw.EventBus"


def _evento(tipo: EventType = EventType.LLM_RESPONSE, **datos: object) -> SkyClawEvent:
    return SkyClawEvent(type=tipo, data=dict(datos))


async def _esperar(condicion, *, timeout: float = 10.0) -> None:
    limite = time.monotonic() + timeout
    while not condicion():
        if time.monotonic() > limite:
            raise AssertionError("la condición no se cumplió a tiempo")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Aislamiento
# ---------------------------------------------------------------------------


def test_aislado_entrega_un_bus_nuevo_y_restaura_el_singleton() -> None:
    original = EventBus()
    suscriptores_antes = {k: list(v) for k, v in original._subscribers.items()}

    with EventBus.aislado() as bus:
        assert bus is not original
        assert bus._subscribers == {}
        bus.subscribe(EventType.MOD_SELECTED, lambda e: None)
        assert EventType.MOD_SELECTED in bus._subscribers

    # el singleton real quedó intacto y vuelve a ser el que devuelve EventBus()
    assert EventBus() is original
    assert event_bus is original
    assert {k: list(v) for k, v in original._subscribers.items()} == suscriptores_antes


def test_aislado_restaura_el_singleton_aunque_el_cuerpo_falle() -> None:
    original = EventBus()

    with pytest.raises(RuntimeError, match="boom"), EventBus.aislado():
        raise RuntimeError("boom")

    assert EventBus() is original


# ---------------------------------------------------------------------------
# Ciclo de vida: nada se pierde en silencio
# ---------------------------------------------------------------------------


async def test_los_eventos_publicados_antes_del_start_se_entregan_en_orden() -> None:
    with EventBus.aislado() as bus:
        recibidos: list[int] = []
        bus.subscribe(EventType.LLM_RESPONSE, lambda e: recibidos.append(e.data["n"]))
        for n in range(5):
            bus.publish(_evento(n=n))
        assert bus.pending == 5

        bus.start()
        await _esperar(lambda: len(recibidos) == 5)

        assert recibidos == [0, 1, 2, 3, 4]
        assert bus.pending == 0


def test_stop_con_eventos_pendientes_avisa_cuantos_quedaron(caplog: pytest.LogCaptureFixture) -> None:
    with EventBus.aislado() as bus:
        for n in range(3):
            bus.publish(_evento(n=n))

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            bus.stop()

        avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(avisos) == 1
        assert "3" in avisos[0].getMessage()
        # no se descarta: sigue disponible para un start() posterior
        assert bus.pending == 3


def test_stop_sin_pendientes_no_avisa(caplog: pytest.LogCaptureFixture) -> None:
    with EventBus.aislado() as bus, caplog.at_level(logging.WARNING, logger=_LOGGER):
        bus.stop()

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_publicar_tras_stop_avisa_retiene_y_un_start_posterior_lo_entrega(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with EventBus.aislado() as bus:
        recibidos: list[int] = []
        bus.subscribe(EventType.LLM_RESPONSE, lambda e: recibidos.append(e.data["n"]))
        bus.start()
        bus.stop()

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            bus.publish(_evento(n=7))

        avisos = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(avisos) == 1
        assert "detenido" in avisos[0]
        assert bus.pending == 1

        bus.start()
        await _esperar(lambda: recibidos == [7])


async def test_publicar_antes_del_primer_start_no_avisa(caplog: pytest.LogCaptureFixture) -> None:
    """Publicar durante el arranque (antes de ``app.on_startup``) es normal, no un aviso."""
    with EventBus.aislado() as bus:
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            bus.publish(_evento(n=1))
        # se lee ANTES de salir: al salir, aislado() llama a stop(), que sí avisa del pendiente
        avisos = [r for r in caplog.records if r.levelno >= logging.WARNING]

    assert avisos == []


# ---------------------------------------------------------------------------
# Presión: los eventos críticos no se pierden ni se duplican
# ---------------------------------------------------------------------------


async def test_bajo_presion_y_con_el_loop_ocupado_no_se_pierde_ni_se_duplica_ningun_evento() -> None:
    productores, por_productor = 4, 500
    with EventBus.aislado() as bus:
        recibidos: list[tuple[int, int]] = []
        bus.subscribe(EventType.LLM_RESPONSE, lambda e: recibidos.append((e.data["p"], e.data["n"])))
        bus.start()

        # Loop ocupado: mientras dura, el backlog se acumula en el loop y no se descarta nada.
        asyncio.get_running_loop().call_soon(time.sleep, 0.3)

        def producir(p: int) -> None:
            for n in range(por_productor):
                bus.publish(_evento(p=p, n=n))

        hilos = [threading.Thread(target=producir, args=(p,)) for p in range(productores)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        await _esperar(lambda: len(recibidos) == productores * por_productor)

        assert sorted(recibidos) == [(p, n) for p in range(productores) for n in range(por_productor)]
        for p in range(productores):
            assert [n for (q, n) in recibidos if q == p] == list(range(por_productor)), "orden por productor"
        assert bus.pending == 0


async def test_con_el_loop_ocupado_stop_no_cuenta_lo_ya_reenviado_al_loop_pero_nada_se_pierde(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fija el LÍMITE de ``pending``/``stop()`` (hallazgo de la auditoría sobre #932).

    Este test corre EN el loop, así que mientras ejecuta código síncrono el loop está ocupado:
    el hilo procesador reenvía los eventos con ``call_soon_threadsafe`` y quedan esperando en el
    loop. ``pending`` (cola propia del bus) llega a 0 y ``stop()`` no avisa de ellos. No se
    pierde nada: al devolverle el control al loop se entregan todos. Hay que leerlo como
    «el bus no da acuse de entrega», no como una garantía de que ``stop()`` cuenta todo lo no
    entregado.
    """
    with EventBus.aislado() as bus:
        recibidos: list[int] = []
        bus.subscribe(EventType.LLM_RESPONSE, lambda e: recibidos.append(e.data["n"]))
        bus.start()
        for n in range(50):
            bus.publish(_evento(n=n))

        limite = time.monotonic() + 5.0
        while bus.pending and time.monotonic() < limite:
            time.sleep(0.01)  # a propósito SÍNCRONO: el loop debe seguir ocupado
        assert bus.pending == 0
        assert recibidos == [], "el loop no pudo ejecutar nada mientras el test es síncrono"

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            bus.stop()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

        await _esperar(lambda: len(recibidos) == 50)
        assert recibidos == list(range(50))


# ---------------------------------------------------------------------------
# Ancla: qué tipos de evento publica producción hoy (la política de overflow espera a esto)
# ---------------------------------------------------------------------------

_RAIZ = Path(__file__).resolve().parent.parent

#: Tipos de ``EventType`` que algún sitio de producción publica (``SkyClawEvent(type=...)``).
#: Congelado por igualdad literal. **Ninguno es telemetría de alta frecuencia**: todos nacen de
#: una acción del usuario o de un cambio de estado. ``DOWNLOAD_PROGRESS`` (el único tipo con
#: forma de telemetría) no tiene productor. Si este test se rompe porque apareció uno, ANTES de
#: mergear hay que decidir la política de overflow que #575 §3 dejó explícitamente pendiente
#: (el ``maxsize`` de ``_event_queue`` no sirve: ver el docstring del módulo), y los eventos de
#: control/HITL deben quedar fuera de cualquier política de descarte.
_PUBLICADOS_EN_PRODUCCION = frozenset(
    {
        "AGENT_STATUS_CHANGE",
        "CONFLICT_DETECTED",
        "EVENT_BROADCAST",
        "LLM_RESPONSE",
        "MOD_ADDED",
        "MOD_SELECTED",
        "NAVIGATION_REQUESTED",
    }
)


def _tipos_publicados(fuente: str) -> set[str]:
    """Nombres ``EventType.X`` usados al construir un ``SkyClawEvent`` (``type=`` o 1.er arg).

    **Límite**: solo reconoce el tipo escrito como literal ``EventType.X``. No ve un tipo dinámico
    (``type=tipo``), un alias ni una factoría que construya el evento. Es un test de regresión
    útil, no una garantía formal de que nunca aparezca telemetría.
    """
    tipos: set[str] = set()
    for nodo in ast.walk(ast.parse(fuente)):
        if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "SkyClawEvent"):
            continue
        candidatos = [kw.value for kw in nodo.keywords if kw.arg == "type"] + nodo.args[:1]
        for valor in candidatos:
            if isinstance(valor, ast.Attribute) and isinstance(valor.value, ast.Name) and valor.value.id == "EventType":
                tipos.add(valor.attr)
    return tipos


@pytest.mark.parametrize(
    ("fuente", "esperados"),
    [
        ("SkyClawEvent(type=EventType.LLM_RESPONSE, data={})", {"LLM_RESPONSE"}),
        ("SkyClawEvent(EventType.MOD_ADDED, {})", {"MOD_ADDED"}),
        ("SkyClawEvent(type=tipo_dinamico, data={})", set()),
        ("otra_cosa(type=EventType.DOWNLOAD_PROGRESS)", set()),
    ],
)
def test_detector_de_tipos_publicados_sintetico(fuente: str, esperados: set[str]) -> None:
    assert _tipos_publicados(fuente) == esperados


def test_los_tipos_publicados_en_produccion_estan_congelados() -> None:
    encontrados: set[str] = set()
    for ruta in sorted((_RAIZ / "sky_claw").rglob("*.py")):
        if ruta.name == "gui_event_adapter.py":
            continue
        encontrados |= _tipos_publicados(ruta.read_text(encoding="utf-8"))

    assert encontrados == _PUBLICADOS_EN_PRODUCCION


def test_ningun_tipo_con_forma_de_telemetria_tiene_productor() -> None:
    assert "DOWNLOAD_PROGRESS" not in _PUBLICADOS_EN_PRODUCCION
    assert {t.name for t in EventType} - _PUBLICADOS_EN_PRODUCCION == {
        "MOD_REMOVED",
        "MOD_UPDATED",
        "CONFLICT_RESOLVED",
        "DOWNLOAD_PROGRESS",
        "CONFIG_CHANGED",
    }

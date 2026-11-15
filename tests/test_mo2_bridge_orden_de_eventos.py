"""B-2 (H-14): el bridge entrega sus eventos EN ORDEN aunque el envío falle y haya que reconectar.

Antes, un evento cuyo envío fallaba se re-encolaba AL FINAL de la cola: con ``[launch_ack, worker_exit]`` y un fallo en el
primero, tras reconectar salía ``worker_exit`` antes que ``launch_ack``. El broker, que arma su máquina de estados con ese
orden, podía ver la salida de un worker que todavía no había confirmado. Ahora el evento que falló vuelve al FRENTE.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
import queue
import threading
import time

import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import BridgeEventOutbox, flush_outbox

RAIZ = pathlib.Path(__file__).resolve().parents[1]
BUNDLE = RAIZ / "sky_claw" / "local" / "mo2" / "plugin_bundle" / "skyclaw_bridge"

LAUNCH_ACK = {"type": "launch_ack", "job_id": "job-1"}
WORKER_EXIT = {"type": "event", "event": "worker_exit", "job_id": "job-1"}


class _Enlace:
    """Doble del socket: entrega lo que se le manda salvo que se le programe un fallo (que NO entrega)."""

    def __init__(self, fallos: dict[int, Exception] | None = None) -> None:
        self.entregados: list[dict[str, object]] = []
        self.intentos = 0
        self._fallos = fallos or {}

    def __call__(self, evento: dict[str, object]) -> None:
        self.intentos += 1
        fallo = self._fallos.get(self.intentos)
        if fallo is not None:
            raise fallo
        self.entregados.append(evento)


def _pendientes(outbox: BridgeEventOutbox) -> list[dict[str, object]]:
    """Lo que hay en la cola, en orden, sin consumirlo."""
    with outbox.mutex:
        return list(outbox.queue)


# ---------------------------------------------------------------------------------------------------- el outbox


def test_requeue_front_devuelve_el_evento_al_frente_sin_alterar_la_cuenta_de_pendientes() -> None:
    outbox = BridgeEventOutbox()
    outbox.put(LAUNCH_ACK)
    outbox.put(WORKER_EXIT)
    sacado = outbox.get_nowait()  # el consumidor lo sacó y todavía no confirmó

    outbox.requeue_front(sacado)

    assert _pendientes(outbox) == [LAUNCH_ACK, WORKER_EXIT]
    assert outbox.unfinished_tasks == 2
    assert outbox.wait_until_drained(timeout=0.01) is False


def test_requeue_front_despierta_a_quien_espera_un_evento() -> None:
    outbox = BridgeEventOutbox()
    outbox.put(LAUNCH_ACK)
    sacado = outbox.get_nowait()
    recibido: list[dict[str, object]] = []
    esperando = threading.Event()

    def consumidor() -> None:
        esperando.set()
        with contextlib.suppress(queue.Empty):
            recibido.append(outbox.get(timeout=5))

    hilo = threading.Thread(target=consumidor, daemon=True)  # daemon: si el ``notify`` faltara, no cuelga la sesión
    hilo.start()
    esperando.wait(timeout=2)
    time.sleep(0.1)  # que el consumidor ya esté bloqueado en ``get``
    outbox.requeue_front(sacado)
    # Sin ``notify`` el ``get`` solo se enteraría al vencer su plazo (5 s): el plazo de 1 s distingue ambos casos.
    hilo.join(timeout=1)

    assert not hilo.is_alive()
    assert recibido == [LAUNCH_ACK]


# --------------------------------------------------------------------------------------------------- el drenaje


def test_flush_entrega_en_orden_y_deja_el_outbox_drenado() -> None:
    outbox = BridgeEventOutbox()
    outbox.put(LAUNCH_ACK)
    outbox.put(WORKER_EXIT)
    enlace = _Enlace()

    flush_outbox(outbox, enlace)

    assert enlace.entregados == [LAUNCH_ACK, WORKER_EXIT]
    assert outbox.wait_until_drained(timeout=0.01) is True


def test_flush_sobre_una_cola_vacia_no_hace_nada() -> None:
    outbox = BridgeEventOutbox()
    enlace = _Enlace()

    flush_outbox(outbox, enlace)

    assert enlace.intentos == 0
    assert outbox.wait_until_drained(timeout=0.01) is True


def test_tras_un_fallo_al_enviar_el_primero_la_reconexion_conserva_el_orden() -> None:
    """EL caso de H-14: antes salía ``worker_exit`` y después ``launch_ack``."""
    outbox = BridgeEventOutbox()
    outbox.put(LAUNCH_ACK)
    outbox.put(WORKER_EXIT)
    primera_conexion = _Enlace({1: ConnectionResetError("se cayó el socket")})

    with pytest.raises(ConnectionResetError):
        flush_outbox(outbox, primera_conexion)

    assert primera_conexion.entregados == []
    assert _pendientes(outbox) == [LAUNCH_ACK, WORKER_EXIT]  # ninguno se perdió y el orden es el original

    segunda_conexion = _Enlace()
    flush_outbox(outbox, segunda_conexion)

    assert segunda_conexion.entregados == [LAUNCH_ACK, WORKER_EXIT]
    assert outbox.wait_until_drained(timeout=0.01) is True


def test_un_evento_que_llega_durante_la_caida_va_despues_de_los_pendientes() -> None:
    outbox = BridgeEventOutbox()
    outbox.put(LAUNCH_ACK)
    outbox.put(WORKER_EXIT)
    caida = _Enlace({2: OSError("socket cerrado a mitad")})  # el 1.º sale, el 2.º falla

    with pytest.raises(OSError, match="socket cerrado"):
        flush_outbox(outbox, caida)
    posterior = {"type": "event", "event": "bridge_health"}
    outbox.put(posterior)  # lo produce otro hilo mientras el bridge reconecta

    reconexion = _Enlace()
    flush_outbox(outbox, reconexion)

    assert caida.entregados == [LAUNCH_ACK]
    assert reconexion.entregados == [WORKER_EXIT, posterior]


def test_dos_fallos_seguidos_sobre_el_mismo_evento_no_lo_pierden_ni_lo_reordenan() -> None:
    outbox = BridgeEventOutbox()
    outbox.put(LAUNCH_ACK)
    outbox.put(WORKER_EXIT)

    for _ in range(2):
        with pytest.raises(OSError, match="intermitente"):
            flush_outbox(outbox, _Enlace({1: OSError("intermitente")}))

    final = _Enlace()
    flush_outbox(outbox, final)

    assert final.entregados == [LAUNCH_ACK, WORKER_EXIT]


def test_un_error_que_no_es_de_red_tampoco_pierde_el_evento() -> None:
    """Antes solo se re-encolaba ante ``OSError``/``VfsFrameError``: cualquier otro error perdía el evento y la cuenta."""
    outbox = BridgeEventOutbox()
    outbox.put(WORKER_EXIT)

    with pytest.raises(RuntimeError, match="inesperado"):
        flush_outbox(outbox, _Enlace({1: RuntimeError("inesperado")}))

    assert _pendientes(outbox) == [WORKER_EXIT]
    assert (
        outbox.wait_until_drained(timeout=0.01) is False
    )  # sigue pendiente: el apagado lo espera, no lo da por enviado


def test_un_productor_concurrente_y_un_enlace_intermitente_entregan_todo_en_el_orden_de_produccion() -> None:
    """Propiedad: lo entregado, con cualquier patrón de fallos, es EXACTAMENTE lo producido y en su orden."""
    total = 300
    outbox = BridgeEventOutbox()
    entregados: list[int] = []
    intentos = 0

    def enlace_intermitente(evento: dict[str, object]) -> None:
        nonlocal intentos
        intentos += 1
        if intentos % 3 == 0:  # cada 3.er envío falla y NO entrega
            raise ConnectionResetError("intermitente")
        entregados.append(int(str(evento["n"])))

    def productor() -> None:
        for n in range(total):
            outbox.put({"n": n})

    hilo = threading.Thread(target=productor)
    hilo.start()
    while len(entregados) < total:
        try:
            flush_outbox(outbox, enlace_intermitente)
        except ConnectionResetError:
            continue  # el bridge reconecta y vuelve a drenar
    hilo.join(timeout=5)

    assert entregados == list(range(total))
    assert outbox.wait_until_drained(timeout=0.01) is True


# ------------------------------------------------------------------------------------------------------- anclas


def _arbol(ruta: pathlib.Path) -> ast.Module:
    return ast.parse(ruta.read_text(encoding="utf-8"))


def _llamadas(nodo: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(nodo) if isinstance(n, ast.Call)]


def _funcion(arbol: ast.Module, nombre: str) -> ast.FunctionDef:
    encontradas = [n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == nombre]
    assert len(encontradas) == 1, nombre
    return encontradas[0]


def _atributos_de_outgoing(arbol: ast.Module) -> set[str]:
    """Todo ``self._outgoing.<algo>`` del módulo: la superficie COMPLETA con la que el plugin toca el outbox."""
    return {
        nodo.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Attribute) and nodo.value.attr == "_outgoing"
    }


def test_el_plugin_solo_produce_o_espera_el_outbox_y_nunca_lo_consume_por_su_cuenta() -> None:
    """El ÚNICO consumidor es ``flush_outbox``. Cualquier ``get``/``get_nowait``/re-``put`` nuevo en el plugin rompe esto."""
    assert _atributos_de_outgoing(_arbol(BUNDLE / "plugin.py")) == {"put", "wait_until_drained"}


def test_solo_flush_outbox_saca_eventos_del_outbox_y_solo_el_mete_al_frente() -> None:
    runtime = _arbol(BUNDLE / "runtime.py")
    flush = _funcion(runtime, "flush_outbox")
    dentro = {id(n) for n in ast.walk(flush)}

    def sitios(metodos: set[str]) -> list[ast.Call]:
        return [
            c
            for c in _llamadas(runtime)
            if isinstance(c.func, ast.Attribute)
            and c.func.attr in metodos
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "outbox"
        ]

    consumos = sitios({"get", "get_nowait"})
    reencolados = sitios({"requeue_front"})
    assert consumos and reencolados, "el ancla no encontró nada que anclar"
    assert all(id(c) in dentro for c in consumos + reencolados)
    # y el re-encolado al FINAL (``put`` de lo que se sacó) no existe en el drenaje
    assert not [c for c in sitios({"put", "put_nowait"}) if id(c) in dentro]


def test_el_bucle_de_conexion_del_plugin_drena_por_flush_outbox() -> None:
    plugin = _arbol(BUNDLE / "plugin.py")
    bucle = _funcion(plugin, "_connected_loop")

    llamadas = [c for c in _llamadas(bucle) if isinstance(c.func, ast.Name) and c.func.id == "flush_outbox"]

    assert len(llamadas) == 1

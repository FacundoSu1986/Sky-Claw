"""Teardown del plugin MO2: worker_exit encolado debe salir antes de cortar el socket.

El módulo ``plugin.py`` importa ``mobase``/``PyQt6`` (solo existen dentro de MO2),
así que estos tests los stubean en ``sys.modules`` antes de importarlo.
"""

from __future__ import annotations

import importlib
import pathlib
import queue
import socket
import sys
import threading
import time
import types
from typing import Any

import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.protocol import (
    recv_authenticated_message,
)

_PLUGIN_MODULE = "sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.plugin"


@pytest.fixture()
def plugin_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    mobase = types.ModuleType("mobase")
    mobase.IPlugin = type("IPlugin", (), {})  # type: ignore[attr-defined]
    mobase.VersionInfo = lambda *args: args  # type: ignore[attr-defined]
    mobase.PluginSetting = type("PluginSetting", (), {})  # type: ignore[attr-defined]

    qtcore = types.ModuleType("PyQt6.QtCore")

    class _QTimer:
        def __init__(self) -> None:
            self.timeout = types.SimpleNamespace(connect=lambda _fn: None)

        def start(self, _interval: int) -> None: ...

        def stop(self) -> None: ...

    qtcore.QTimer = _QTimer  # type: ignore[attr-defined]
    qtcore.QCoreApplication = types.SimpleNamespace(instance=lambda: None)  # type: ignore[attr-defined]
    qtcore.qInfo = lambda _msg: None  # type: ignore[attr-defined]
    qtcore.qWarning = lambda _msg: None  # type: ignore[attr-defined]
    pyqt6 = types.ModuleType("PyQt6")
    pyqt6.QtCore = qtcore  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "mobase", mobase)
    monkeypatch.setitem(sys.modules, "PyQt6", pyqt6)
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", qtcore)
    sys.modules.pop(_PLUGIN_MODULE, None)
    module = importlib.import_module(_PLUGIN_MODULE)
    yield module
    sys.modules.pop(_PLUGIN_MODULE, None)


def test_shutdown_espera_monitores_antes_de_cortar_el_cliente(plugin_module: Any) -> None:
    """MO2 descarga el plugin con un worker vivo: terminar el Job Object, join
    de los monitores (worker_exit queda encolado) y recién entonces parar el
    cliente. Sin ese orden el broker del daemon espera el fence para siempre."""
    llamadas: list[str] = []
    plugin = plugin_module.SkyClawBridgePlugin.__new__(plugin_module.SkyClawBridgePlugin)
    plugin._timer = types.SimpleNamespace(stop=lambda: llamadas.append("timer.stop"))
    plugin._controller = types.SimpleNamespace(
        stop=lambda: llamadas.append("controller.stop"),
        wait_for_monitors=lambda *, timeout: llamadas.append("wait_for_monitors"),
    )
    plugin._client = types.SimpleNamespace(stop=lambda: llamadas.append("client.stop"))

    plugin._shutdown()

    assert llamadas == [
        "timer.stop",
        "controller.stop",
        "wait_for_monitors",
        "client.stop",
    ]


def test_stop_del_cliente_flushea_outgoing_antes_de_senalizar_cierre(
    plugin_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    llamadas: list[tuple[str, bool]] = []
    client = plugin_module._BridgeClient(
        descriptor_path=tmp_path / "descriptor-inexistente.json",
        instance_id="mo2-test",
        commands=queue.Queue(),
        outgoing=queue.Queue(),
    )

    def _flush_falso(
        pendientes: queue.Queue[dict[str, object]],
        *,
        timeout: float,
        still_running: Any,
    ) -> bool:
        llamadas.append(("flush", client._stop.is_set()))
        return True

    monkeypatch.setattr(plugin_module, "flush_pending_events", _flush_falso)
    client.start()
    client.stop()

    # El flush corre ANTES de señalizar el cierre: si _stop ya estuviera seteado
    # el hilo del socket podría salir del loop sin drenar el evento terminal.
    assert llamadas == [("flush", False)]


def test_connected_loop_marca_task_done_tras_enviar(
    plugin_module: Any,
    tmp_path: pathlib.Path,
) -> None:
    """El flush del stop se apoya en la contabilidad task_done del hilo del
    socket: cada evento sacado de la cola debe marcarse enviado (o re-encolarse
    sin cerrar la cuenta) para que el drenaje sepa cuándo terminó de verdad."""
    secreto = b"s" * 32
    lado_cliente, lado_broker = socket.socketpair()
    outgoing: queue.Queue[dict[str, object]] = queue.Queue()
    client = plugin_module._BridgeClient(
        descriptor_path=tmp_path / "descriptor.json",
        instance_id="mo2-test",
        commands=queue.Queue(),
        outgoing=outgoing,
    )
    outgoing.put(
        {
            "protocol_version": 1,
            "type": "event",
            "event": "worker_exit",
            "job_id": "job-1",
        }
    )

    hilo = threading.Thread(
        target=client._connected_loop,
        args=(lado_cliente, secreto, tmp_path),
        daemon=True,
    )
    hilo.start()
    try:
        mensaje = recv_authenticated_message(lado_broker, secreto)
        assert mensaje["event"] == "worker_exit"
        deadline = time.monotonic() + 2
        while outgoing.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert outgoing.unfinished_tasks == 0
    finally:
        client._stop.set()
        hilo.join(timeout=2)
        lado_cliente.close()
        lado_broker.close()

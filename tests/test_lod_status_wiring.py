"""Wiring de ``/lod_status``: ¿lo cablea el proceso REAL, en TODOS los caminos que arman el webhook?

El comando, el read-model y el dispatcher tienen sus propios tests. Lo que ninguno puede decir es si el
proceso real le entrega al webhook el seguimiento vivo: un comando perfecto cuyo webhook recibe ``None``
responde «no disponible» en silencio, y un camino que arma el webhook por su cuenta lo deja sin comando
(el defecto dominante de este repo: un camino correcto y su gemelo no; ver «La regla que más se viola» en
``AGENTS.md``). Hay TRES sitios de producción que construyen ``TelegramWebhook``: el arranque normal
(``app_context``), el hot-reload del token desde la UI (``frontend_bridge``) y el modo ``telegram`` sin GUI
(``telegram_mode``).

Propiedades que este archivo fija:

* El censo de sitios que construyen el webhook está congelado por igualdad literal, y TODOS pasan el MISMO
  proveedor: el seguimiento único del proceso (``AppContext.seguimiento_etapa9``). Ninguno pasa ``None``: el
  modo sin supervisor se resuelve con el estado «esta instancia no ejecuta la etapa 9» del propio
  seguimiento, que dice la verdad en vez de un genérico «no disponible».
* El bootloader de la GUI conecta ESE seguimiento al bus del supervisor, antes de ``supervisor.start()``.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sky_claw.app.core.event_bus import CoreEventBus, Event
from sky_claw.app.core.event_payloads import DynDOLODPipelineStartedPayload
from sky_claw.app.gui import _bootloader
from sky_claw.app.orchestrator.dyndolod_seguimiento import SeguimientoDeEtapa9

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Sitios de PRODUCCIÓN que construyen ``TelegramWebhook``. Igualdad literal: un sitio nuevo rompe esto hasta que
#: se decida cómo recibe ``/lod_status``.
SITIOS_QUE_CONSTRUYEN_EL_WEBHOOK = frozenset(
    {
        "sky_claw/app/modes/telegram_mode.py",
        "sky_claw/app/comms/frontend_bridge.py",
        "sky_claw/app_context.py",
    }
)

#: El único proveedor admitido, visto desde el objeto que cada sitio tiene a mano (``self`` en el contexto, ``self.ctx``
#: en el bridge, ``ctx`` en el modo telegram).
PROVEEDORES_ADMITIDOS = frozenset(
    {
        "self.seguimiento_etapa9.resumen_html",
        "self.ctx.seguimiento_etapa9.resumen_html",
        "ctx.seguimiento_etapa9.resumen_html",
    }
)


def _llamadas_al_webhook() -> dict[str, list[ast.Call]]:
    encontradas: dict[str, list[ast.Call]] = {}
    for archivo in sorted(PAQUETE.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = nodo.func.attr if isinstance(nodo.func, ast.Attribute) else getattr(nodo.func, "id", "")
            if nombre == "TelegramWebhook":
                encontradas.setdefault(archivo.relative_to(RAIZ).as_posix(), []).append(nodo)
    return encontradas


def test_los_sitios_que_construyen_el_webhook_estan_congelados() -> None:
    assert set(_llamadas_al_webhook()) == SITIOS_QUE_CONSTRUYEN_EL_WEBHOOK, (
        "apareció (o desapareció) un sitio de producción que construye TelegramWebhook: sin `lod_status=` ese camino "
        "queda sin /lod_status. Decidí cómo lo recibe y declaralo acá"
    )


def test_todo_sitio_le_pasa_al_webhook_el_seguimiento_unico_del_proceso() -> None:
    incorrectos: list[str] = []
    for archivo, llamadas in _llamadas_al_webhook().items():
        for llamada in llamadas:
            valores = [kw.value for kw in llamada.keywords if kw.arg == "lod_status"]
            if len(valores) != 1 or ast.unparse(valores[0]) not in PROVEEDORES_ADMITIDOS:
                incorrectos.append(
                    f"{archivo}:{llamada.lineno} -> {[ast.unparse(v) for v in valores] or 'sin lod_status'}"
                )

    assert incorrectos == [], f"construyen el webhook sin el seguimiento del proceso: {incorrectos}"


class TestLosTresSitiosEntregaElSeguimiento:
    async def test_el_modo_telegram_sin_gui_se_lo_entrega(self) -> None:
        from sky_claw.app.modes import telegram_mode

        ctx = MagicMock()
        ctx.router = MagicMock()
        ctx.session = MagicMock()
        ctx.network.gateway = MagicMock()
        ctx.sender = MagicMock()
        ctx.sender._token = "123:ABC"
        ctx._args.operator_chat_id = 987654
        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()
        fake_polling.stop = AsyncMock()

        with (
            patch.object(telegram_mode, "TelegramWebhook") as mock_webhook,
            patch.object(telegram_mode, "TelegramPolling", return_value=fake_polling),
        ):
            task = asyncio.create_task(telegram_mode._run_telegram(ctx, "127.0.0.1", 0))
            await asyncio.sleep(0.05)
            task.cancel()
            await task

        assert mock_webhook.call_args.kwargs["lod_status"] == ctx.seguimiento_etapa9.resumen_html

    async def test_el_hot_reload_del_token_se_lo_entrega_al_webhook_nuevo(self) -> None:
        from sky_claw.app.comms.frontend_bridge import FrontendBridge

        bridge = FrontendBridge.__new__(FrontendBridge)
        ctx = MagicMock()
        ctx.polling = None
        bridge.ctx = ctx  # type: ignore[attr-defined]
        fake_polling = MagicMock()
        fake_polling.start = AsyncMock()

        with (
            patch("sky_claw.app.comms.telegram.TelegramWebhook") as mock_webhook,
            patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling),
            patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
        ):
            ok = await bridge._reload_telegram(token="123:ABC", chat_id="55501")

        assert ok is True
        assert mock_webhook.call_args.kwargs["lod_status"] == ctx.seguimiento_etapa9.resumen_html


class TestElBootloaderConectaElSeguimiento:
    async def test_instalar_el_seguimiento_lo_conecta_al_bus_y_sigue_una_corrida_real(self) -> None:
        seguimiento = SeguimientoDeEtapa9()
        ctx = SimpleNamespace(seguimiento_etapa9=seguimiento)
        bus = CoreEventBus()
        await bus.start()
        try:
            assert "no ejecuta la etapa 9" in seguimiento.resumen_html()

            _bootloader._install_dyndolod_status_tracking(ctx, SimpleNamespace(event_bus=bus))  # type: ignore[arg-type]
            payload = DynDOLODPipelineStartedPayload(preset="Medium", run_texgen=True).to_log_dict()
            await bus.publish(Event(topic="pipeline.dyndolod.started", payload=payload))

            async def _girar() -> None:
                while "en curso" not in seguimiento.resumen_html():
                    await asyncio.sleep(0)

            await asyncio.wait_for(_girar(), timeout=5)
        finally:
            await bus.stop()

        assert "<code>Medium</code>" in seguimiento.resumen_html()

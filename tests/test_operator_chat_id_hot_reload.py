"""El hot-reload de Telegram mantiene sincronizados el ``sender`` y el chat del operador.

``AppContext.operator_chat_id`` se publica junto a ``sender`` (ver
``test_app_context_publicacion_simetrica.py``) para que los consumidores que se instalan fuera de
``start_full`` —el notificador de la etapa 9— los lean EN CADA envío. Hay un segundo camino que
reasigna ``ctx.sender``: ``FrontendBridge._reload_telegram``, el hot-reload del token desde la UI. Si
reasigna el sender y el webhook pero no el chat, el bot pasa a autorizar al chat NUEVO y los avisos
de la etapa 9 siguen yendo al VIEJO: un camino correcto y su gemelo no (el defecto dominante de este
repo, ver «La regla que más se viola» en ``AGENTS.md``).

Dos propiedades:

* el hot-reload deja ``ctx.operator_chat_id`` igual al chat con el que autoriza al webhook (uno solo:
  HITL, comandos y avisos hablan con el MISMO operador);
* el ancla enumera por AST toda función de producción que asigna ``<algo>.sender`` y exige que asigne
  también ``<mismo objeto>.operator_chat_id``: un sitio nuevo rompe el ancla hasta que se decida.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Funciones de PRODUCCIÓN que asignan ``<objeto>.sender``, como ``(archivo, función)``. Igualdad literal:
#: una función nueva rompe esto hasta que se decida cómo mantiene sincronizado el chat del operador.
ASIGNAN_EL_SENDER = frozenset(
    {
        ("sky_claw/app_context.py", "__init__"),
        ("sky_claw/app_context.py", "_sanitize_full_references"),
        ("sky_claw/app_context.py", "_start_full_inner"),
        ("sky_claw/app/comms/frontend_bridge.py", "_reload_telegram"),
    }
)


def _puente(*, chat_viejo: int | None) -> tuple[object, MagicMock]:
    from sky_claw.app.comms.frontend_bridge import FrontendBridge

    bridge = FrontendBridge.__new__(FrontendBridge)
    ctx = MagicMock()
    ctx.polling = None
    ctx.operator_chat_id = chat_viejo
    bridge.ctx = ctx  # type: ignore[attr-defined]
    return bridge, ctx


async def _recargar(bridge: object, *, chat_id: str) -> bool:
    fake_polling = MagicMock()
    fake_polling.start = AsyncMock()
    with (
        patch("sky_claw.app.comms.telegram.TelegramWebhook"),
        patch("sky_claw.app.comms.telegram_polling.TelegramPolling", return_value=fake_polling),
        patch("sky_claw.app.comms.telegram_sender.TelegramSender"),
    ):
        return await bridge._reload_telegram(token="123:ABC", chat_id=chat_id)  # type: ignore[attr-defined,no-any-return]


class TestReloadSincronizaElChatDelOperador:
    async def test_el_reload_publica_el_chat_nuevo_para_los_avisos(self) -> None:
        bridge, ctx = _puente(chat_viejo=111)

        ok = await _recargar(bridge, chat_id="55501")

        assert ok is True
        assert ctx.operator_chat_id == 55501, "los avisos de la etapa 9 tienen que ir al chat que el bot autoriza"

    async def test_el_reload_sin_chat_deja_a_los_avisos_sin_destino(self) -> None:
        """Sin operador no hay canal: el mismo criterio que ``authorized_user_id=None`` (fail-closed)."""
        bridge, ctx = _puente(chat_viejo=111)

        ok = await _recargar(bridge, chat_id="")

        assert ok is True
        assert ctx.operator_chat_id is None

    async def test_un_chat_invalido_no_publica_un_chat_a_medias(self) -> None:
        bridge, ctx = _puente(chat_viejo=111)

        ok = await _recargar(bridge, chat_id="no-es-un-numero")

        assert ok is False
        assert ctx.operator_chat_id == 111, "si el reload falla al leer el chat, el chat publicado no cambia"


def _funciones_que_asignan_el_sender() -> dict[tuple[str, str], tuple[set[str], set[str]]]:
    """``{(archivo, función): (objetos que reciben .sender, objetos que reciben .operator_chat_id)}``."""
    resultado: dict[tuple[str, str], tuple[set[str], set[str]]] = {}
    for archivo in sorted(PAQUETE.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        clave = archivo.relative_to(RAIZ).as_posix()
        for funcion in ast.walk(arbol):
            if not isinstance(funcion, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            con_sender: set[str] = set()
            con_chat: set[str] = set()
            for nodo in ast.walk(funcion):
                if isinstance(nodo, ast.Assign):
                    objetivos = nodo.targets
                elif isinstance(nodo, (ast.AnnAssign, ast.AugAssign)):
                    objetivos = [nodo.target]
                else:
                    continue
                for objetivo in objetivos:
                    if not isinstance(objetivo, ast.Attribute):
                        continue
                    base = ast.unparse(objetivo.value)
                    if objetivo.attr == "sender":
                        con_sender.add(base)
                    elif objetivo.attr == "operator_chat_id":
                        con_chat.add(base)
            if con_sender:
                resultado[(clave, funcion.name)] = (con_sender, con_chat)
    return resultado


def test_las_funciones_que_asignan_el_sender_estan_congeladas() -> None:
    assert set(_funciones_que_asignan_el_sender()) == ASIGNAN_EL_SENDER, (
        "apareció (o desapareció) una función de producción que asigna `.sender`: decidí cómo mantiene "
        "sincronizado `.operator_chat_id` (los avisos de la etapa 9 leen los dos del AppContext)"
    )


def test_toda_funcion_que_asigna_el_sender_asigna_tambien_el_chat_del_operador() -> None:
    sin_chat = {
        clave: sorted(con_sender - con_chat)
        for clave, (con_sender, con_chat) in _funciones_que_asignan_el_sender().items()
        if con_sender - con_chat
    }

    assert sin_chat == {}, f"asignan `.sender` sin `.operator_chat_id` sobre el mismo objeto: {sin_chat}"

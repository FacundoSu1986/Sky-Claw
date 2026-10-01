"""Comandos de salida del REPL de la CLI.

El banner del REPL promete que escribir un comando de salida cierra la sesión,
pero el loop sólo salía con EOF (Ctrl-D) o ``KeyboardInterrupt``: el texto
``exit`` viajaba al LLM como si fuera un pedido de modding más.

La familia de comandos se congela por igualdad literal (mismo criterio que
``RITUAL_TOOL_MAP`` en ``tests/test_ritual_dispatch.py``) y el banner se deriva
de la MISMA constante, así que agregar un comando sin cablearlo —o prometer uno
que no existe— rompe el ancla.

Los casi-aciertos (``"exit please"``, ``"salir del juego"``) NO cierran: son
frases para el agente, no comandos.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.modes import cli_mode


def _ctx_con_router() -> MagicMock:
    ctx = MagicMock()
    ctx.router = MagicMock()
    ctx.session = MagicMock()
    ctx.router.chat = AsyncMock(return_value="respuesta")
    return ctx


async def _correr_repl(ctx: MagicMock, entradas: list[str]) -> MagicMock:
    """Corre ``_run_cli`` con las entradas dadas; EOF después de la última."""
    pendientes = iter(entradas)

    def _input(_prompt: str) -> str:
        try:
            return next(pendientes)
        except StopIteration:
            raise EOFError from None

    # ``asyncio.to_thread`` se reemplaza por una llamada directa: el REPL lo usa
    # tanto para ``input`` como para ``flush_logging``.
    async def _to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    with (
        patch.object(cli_mode.asyncio, "to_thread", side_effect=_to_thread),
        patch.object(cli_mode, "input", create=True, side_effect=_input),
        patch.object(cli_mode, "flush_logging", return_value=True),
    ):
        await cli_mode._run_cli(ctx)
    return ctx


def test_la_familia_de_comandos_de_salida_esta_congelada() -> None:
    """Igualdad literal: un comando nuevo exige decidir si sale o va al LLM."""
    assert {"exit", "quit", "salir"} == cli_mode._COMANDOS_DE_SALIDA


@pytest.mark.parametrize("comando", ["exit", "quit", "salir", "EXIT", "  Quit  ", "/exit", "/salir"])
async def test_los_comandos_de_salida_cortan_el_repl_sin_llamar_al_llm(comando: str) -> None:
    ctx = await _correr_repl(_ctx_con_router(), [comando, "no debe llegar al LLM"])
    ctx.router.chat.assert_not_awaited()


@pytest.mark.parametrize("texto", ["exit please", "salir del juego", "quit now", "exit!"])
async def test_las_frases_que_empiezan_como_comando_siguen_yendo_al_llm(texto: str) -> None:
    ctx = await _correr_repl(_ctx_con_router(), [texto])
    ctx.router.chat.assert_awaited_once()
    assert ctx.router.chat.await_args.args[0] == texto.strip()


async def test_el_banner_anuncia_todos_los_comandos_de_salida() -> None:
    """El banner y la familia no pueden divergir: promete lo que el loop cumple."""
    ctx = _ctx_con_router()

    def _input(_prompt: str) -> str:
        raise EOFError

    async def _to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    with (
        patch.object(cli_mode.asyncio, "to_thread", side_effect=_to_thread),
        patch.object(cli_mode, "input", create=True, side_effect=_input),
        patch.object(cli_mode, "flush_logging", return_value=True),
        patch.object(cli_mode.logger, "info") as mock_info,
    ):
        await cli_mode._run_cli(ctx)

    banner = None
    for call in mock_info.call_args_list:
        if call.args and "interactive mode" in str(call.args[0]):
            banner = str(call.args[0]) % tuple(call.args[1:])
            break

    assert banner is not None, "El REPL no anunció el modo interactivo"
    for comando in cli_mode._COMANDOS_DE_SALIDA:
        assert comando in banner, f"El banner no menciona el comando '{comando}': {banner!r}"

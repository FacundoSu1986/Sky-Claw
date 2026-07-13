"""Race de la conexión de historial del LLMRouter (R-2, arbitraje de auditorías).

R-2 [HIGH] del OODA analysis: ``_save_message`` y ``_load_context`` releen
``self._conn`` a través de ``await``s sin serializar contra ``close()``. En un
shutdown mientras un chat está en vuelo, ``close()`` nula ``self._conn`` entre el
``execute()`` y el ``commit()`` de ``_save_message`` → ``None.commit()`` →
``AttributeError`` (o una query sobre una conexión que se está cerrando).

El fix serializa el acceso a la conexión con un ``asyncio.Lock`` (``_conn_lock``)
compartido por ``open``/``close``/``_save_message``/``_load_context``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from sky_claw.antigravity.agent.router import LLMRouter


def _bare_router() -> LLMRouter:
    """LLMRouter mínimo para ejercitar solo la capa de persistencia de historial.

    Evita el ``__init__`` pesado (providers, gateway, tool registry): setea a mano
    únicamente los atributos que tocan ``open``/``close``/``_save_message``/
    ``_load_context``.
    """
    r = LLMRouter.__new__(LLMRouter)
    r._conn = None  # type: ignore[attr-defined]
    r._owns_conn = True  # type: ignore[attr-defined]
    r._lifecycle = None  # type: ignore[attr-defined]
    r._max_context = 10  # type: ignore[attr-defined]
    r._conn_lock = asyncio.Lock()  # type: ignore[attr-defined]
    return r


class TestRaceConexionHistorial:
    """R-2: shutdown concurrente con la persistencia de historial."""

    async def test_save_message_concurrente_con_close_no_crashea(self) -> None:
        """Un close() mientras _save_message está en su execute() no debe
        producir AttributeError al releer self._conn en el commit()."""
        r = _bare_router()

        dentro_de_execute = asyncio.Event()
        liberar = asyncio.Event()

        async def _execute_lento(*_a: object, **_k: object) -> None:
            dentro_de_execute.set()
            await liberar.wait()  # el save queda suspendido dentro de execute()

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(side_effect=_execute_lento)
        mock_conn.commit = AsyncMock()
        mock_conn.close = AsyncMock()
        r._conn = mock_conn  # type: ignore[attr-defined]

        save_task = asyncio.create_task(r._save_message("chat", "user", "hola"))
        await asyncio.wait_for(dentro_de_execute.wait(), timeout=1.0)

        # close() concurrente: sin el lock nula _conn de inmediato; con el lock
        # queda bloqueado esperando a que _save_message suelte la conexión.
        close_task = asyncio.create_task(r.close())
        for _ in range(20):
            await asyncio.sleep(0)
            if r._conn is None:
                break

        liberar.set()

        # Sin el fix: el commit() reencuentra self._conn == None → AttributeError.
        await asyncio.wait_for(save_task, timeout=1.0)
        await asyncio.wait_for(close_task, timeout=1.0)

    async def test_close_espera_a_load_en_vuelo(self) -> None:
        """close() no debe cerrar la conexión mientras _load_context la usa:
        el lock serializa (la conexión sigue viva durante la lectura)."""
        r = _bare_router()

        dentro_de_query = asyncio.Event()
        liberar = asyncio.Event()

        class _Cursor:
            async def __aenter__(self) -> _Cursor:
                dentro_de_query.set()
                await liberar.wait()
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

            async def fetchall(self) -> list[object]:
                return []

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(return_value=_Cursor())
        mock_conn.close = AsyncMock()
        r._conn = mock_conn  # type: ignore[attr-defined]

        load_task = asyncio.create_task(r._load_context("chat"))
        await asyncio.wait_for(dentro_de_query.wait(), timeout=1.0)

        close_task = asyncio.create_task(r.close())
        for _ in range(20):
            await asyncio.sleep(0)
            if r._conn is None:
                break

        # Mientras la lectura está en vuelo, close() no debió cerrar la conexión.
        assert r._conn is mock_conn, "close() cerró la conexión durante una lectura activa"
        mock_conn.close.assert_not_called()

        liberar.set()
        await asyncio.wait_for(load_task, timeout=1.0)
        await asyncio.wait_for(close_task, timeout=1.0)
        assert r._conn is None  # tras soltar el lock, close() sí cierra


class TestFlujoNormalHistorial:
    """Regresión: el fix del lock no rompe el flujo secuencial normal."""

    async def test_flujo_open_save_load_close(self, tmp_path) -> None:
        """open → save → load → close sobre una conexión sqlite real."""
        r = _bare_router()
        r._db_path = str(tmp_path / "chat_history.db")  # type: ignore[attr-defined]

        await r.open()
        await r._save_message("chat-1", "user", "hola")
        await r._save_message("chat-1", "assistant", "qué tal")
        contexto = await r._load_context("chat-1")
        await r.close()

        contenidos = [m["content"] for m in contexto]
        assert contenidos == ["hola", "qué tal"]  # orden cronológico preservado
        assert r._conn is None

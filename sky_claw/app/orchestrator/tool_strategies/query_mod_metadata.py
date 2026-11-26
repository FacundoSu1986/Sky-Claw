"""Strategy for the `query_mod_metadata` tool.

Replaces supervisor.py:244-249 (case "query_mod_metadata"). Owns its
ScrapingQuery validation; on invalid payload, pydantic.ValidationError
propagates (current behavior preserved).

H05/C-min: `ScrapingQuery.url` runs a synchronous SSRF DNS check in a Pydantic
field validator. The validation is performed by ONE daemon thread at a time;
a second request is rejected immediately with the dispatcher's canonical error
dict. No queue, no default executor, and no cancellation of the underlying
blocking work is claimed.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import threading
from typing import TYPE_CHECKING, Any

from sky_claw.app.core.schemas import ScrapingQuery

if TYPE_CHECKING:
    from sky_claw.app.scraper.scraper_agent import ScraperAgent

# Ámbito compartido por la aplicación: el límite vive en el módulo, no en cada
# instancia de la estrategia. Un worker cancelado sigue ocupando la capacidad.
_CANDADO_CAPACIDAD = threading.Lock()
_CAPACIDAD_OCUPADA = False
_Thread = threading.Thread
_NOMBRE_HILO = "h05-scraping-query"
_RAZON_CAPACIDAD = "CapacityExhausted"


def _admitir_capacidad() -> bool:
    """Admite una sola validación H05, sin esperar ni encolar."""
    global _CAPACIDAD_OCUPADA
    with _CANDADO_CAPACIDAD:
        if _CAPACIDAD_OCUPADA:
            return False
        _CAPACIDAD_OCUPADA = True
        return True


def _liberar_capacidad() -> None:
    """Libera la capacidad; lo llama el worker, nunca el ``await`` cancelado."""
    global _CAPACIDAD_OCUPADA
    with _CANDADO_CAPACIDAD:
        _CAPACIDAD_OCUPADA = False


def _rechazo_por_capacidad() -> dict[str, Any]:
    """Dict de error canónico: mismo contrato que el dispatcher y sus middlewares."""
    return {
        "status": "error",
        "reason": _RAZON_CAPACIDAD,
        "details": "H05 ya tiene una validación en vuelo; reintente cuando finalice.",
    }


def _construir_modelo(payload_dict: dict[str, Any]) -> ScrapingQuery:
    """Único sitio productivo de construcción; corre dentro del worker daemon."""
    return ScrapingQuery(**payload_dict)


async def _validar_fuera_del_loop(payload_dict: dict[str, Any]) -> ScrapingQuery:
    """Conexiona un hilo daemon con un ``Future`` del loop, sin executor."""
    try:
        loop = asyncio.get_running_loop()
        futuro: asyncio.Future[ScrapingQuery] = loop.create_future()
        contexto = contextvars.copy_context()
    except BaseException:
        _liberar_capacidad()
        raise

    def entregar(resultado: ScrapingQuery | None, error: BaseException | None) -> None:
        # El await pudo cancelarse: no resolvemos dos veces un future cancelado.
        if futuro.done():
            return
        if error is None:
            futuro.set_result(resultado)  # type: ignore[arg-type]
        else:
            futuro.set_exception(error)

    def trabajo() -> None:
        resultado: ScrapingQuery | None = None
        error: BaseException | None = None
        try:
            resultado = contexto.run(_construir_modelo, payload_dict)
        except BaseException as exc:  # noqa: BLE001 — se re-propaga al caller vía el future
            error = exc
        finally:
            # La capacidad se libera aquí, no en el coroutine: cancelar el await
            # NO termina la syscall bloqueante que sigue ocupando el worker.
            _liberar_capacidad()
        with contextlib.suppress(RuntimeError):
            # Si el loop ya cerró, el worker daemon muere sin excepción secundaria.
            loop.call_soon_threadsafe(entregar, resultado, error)

    try:
        hilo = _Thread(target=trabajo, name=_NOMBRE_HILO, daemon=True)
        hilo.start()
    except BaseException:
        _liberar_capacidad()
        raise
    return await futuro


class QueryModMetadataStrategy:
    name = "query_mod_metadata"

    def __init__(self, scraper: ScraperAgent) -> None:
        self.scraper = scraper

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        if not _admitir_capacidad():
            return _rechazo_por_capacidad()
        query = await _validar_fuera_del_loop(payload_dict)
        result = await self.scraper.query_nexus(query)
        return result.model_dump()

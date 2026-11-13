"""``LLMRouter.chat`` oculta el razonamiento del modelo en TODAS sus salidas (#918).

``chat()`` es el cuello de botella de las superficies que consumen al agente
(Telegram, GUI, web, CLI, WS): filtrar ahí cubre a las ocho por construcción.
Filtrar en una sola superficie era justamente arreglar a un hermano y dejar al otro.

Dos rutas devuelven el texto final del modelo: la nativa (``stop_reason != "tool_use"``)
y la de tool-calling XML. Un test de comportamiento por ruta, y un ancla por AST que
enumera los ``return`` de ``chat()`` para que una tercera ruta no devuelva texto crudo.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from sky_claw.app.agent import router as router_mod
from sky_claw.app.agent.providers import AnthropicProvider
from sky_claw.app.agent.router import LLMRouter
from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.db.async_registry import AsyncModRegistry
from sky_claw.app.orchestrator.sync_engine import SyncEngine
from sky_claw.app.scraper.masterlist import MasterlistClient
from sky_claw.app.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.mo2.vfs import MO2Controller

_RESPUESTA_CRUDA = "<thought>el usuario saluda, respondo corto</thought>\nHola, ¿en qué te ayudo?"
_RESPUESTA_VISIBLE = "Hola, ¿en qué te ayudo?"


def _mo2(tmp_path: pathlib.Path) -> MO2Controller:
    perfil = tmp_path / "profiles" / "Default"
    perfil.mkdir(parents=True)
    (perfil / "modlist.txt").write_text("+TestMod-100\n", encoding="utf-8")
    return MO2Controller(tmp_path, path_validator=PathValidator(roots=[tmp_path]))


@pytest.fixture()
async def registro(tmp_path: pathlib.Path) -> Any:
    reg = AsyncModRegistry(db_path=tmp_path / "razonamiento.db")
    await reg.open()
    yield reg
    await reg.close()


@pytest.fixture()
def herramientas(registro: AsyncModRegistry, tmp_path: pathlib.Path) -> AsyncToolRegistry:
    mo2 = _mo2(tmp_path)
    gw = NetworkGateway(EgressPolicy(block_private_ips=False))
    engine = SyncEngine(mo2=mo2, masterlist=MasterlistClient(gateway=gw, api_key="fake"), registry=registro)
    return AsyncToolRegistry(registry=registro, mo2=mo2, sync_engine=engine)


async def _router(herramientas: AsyncToolRegistry, tmp_path: pathlib.Path, *, xml: bool) -> LLMRouter:
    r = LLMRouter(
        provider=AnthropicProvider("test-key"),
        tool_registry=herramientas,
        db_path=str(tmp_path / ("xml.db" if xml else "nativo.db")),
        system_prompt="Sos un asistente de Skyrim.",
        gateway=MagicMock(spec=NetworkGateway),
        xml_tool_calling=xml,
    )
    await r.open()
    r._semantic_router.route = MagicMock(  # type: ignore[method-assign]
        return_value={
            "intent": "CHAT_GENERAL",
            "confidence": 0.7,
            "target_agent": None,
            "tool_name": None,
            "parameters": {},
            "original_text": "",
        }
    )

    async def _chat_falso(**_kwargs: Any) -> dict[str, Any]:
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": _RESPUESTA_CRUDA}]}

    r._provider.chat = _chat_falso  # type: ignore[method-assign]
    return r


class TestElRouterOcultaElRazonamiento:
    async def test_la_ruta_nativa_no_devuelve_el_bloque_thought(
        self, herramientas: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        router = await _router(herramientas, tmp_path, xml=False)
        try:
            respuesta = await router.chat("hola", MagicMock(spec=aiohttp.ClientSession), chat_id="razon-nativo")
        finally:
            await router.close()

        assert respuesta == _RESPUESTA_VISIBLE

    async def test_la_ruta_xml_no_devuelve_el_bloque_thought(
        self, herramientas: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        router = await _router(herramientas, tmp_path, xml=True)
        try:
            respuesta = await router.chat("hola", MagicMock(spec=aiohttp.ClientSession), chat_id="razon-xml")
        finally:
            await router.close()

        assert respuesta == _RESPUESTA_VISIBLE

    async def test_el_guardrail_de_salida_sigue_viendo_el_texto_crudo(
        self, herramientas: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        """Ocultar el razonamiento al usuario no puede ocultárselo a la puerta de seguridad."""
        router = await _router(herramientas, tmp_path, xml=False)
        guardrail = MagicMock()
        guardrail.before_model_callback = AsyncMock(side_effect=lambda mensaje: mensaje)
        guardrail.after_model_callback = AsyncMock()
        router._guardrail = guardrail
        try:
            await router.chat("hola", MagicMock(spec=aiohttp.ClientSession), chat_id="razon-guardrail")
        finally:
            await router.close()

        guardrail.after_model_callback.assert_awaited_once_with(_RESPUESTA_CRUDA)


class TestAnclaDeRetornosDeChat:
    """Enumera los ``return`` de ``LLMRouter.chat``: ninguno devuelve texto del modelo sin filtrar."""

    @staticmethod
    def _retornos_de_chat() -> list[ast.Return]:
        arbol = ast.parse(pathlib.Path(router_mod.__file__).read_text(encoding="utf-8"))
        clase = next(n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == "LLMRouter")
        chat = next(n for n in clase.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat")
        return [n for n in ast.walk(chat) if isinstance(n, ast.Return) and n.value is not None]

    def test_ningun_return_de_chat_devuelve_el_texto_del_modelo_crudo(self) -> None:
        crudos = [
            n.lineno
            for n in self._retornos_de_chat()
            if isinstance(n.value, ast.Name) and n.value.id in {"final_text", "full_text"}
        ]
        assert crudos == [], f"chat() devuelve texto del modelo sin ocultar_razonamiento en las líneas {crudos}"

    def test_las_rutas_que_devuelven_texto_del_modelo_pasan_por_el_filtro(self) -> None:
        filtrados = [
            n
            for n in self._retornos_de_chat()
            if isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "ocultar_razonamiento"
        ]
        # Congelado por igualdad: ruta nativa + ruta XML. Una tercera ruta que devuelva
        # texto del modelo rompe este test hasta que se la cablee (y se actualice el 2).
        assert len(filtrados) == 2

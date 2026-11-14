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
import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.app.agent import router as router_mod
from sky_claw.app.agent.providers import AnthropicProvider
from sky_claw.app.agent.reasoning_filter import MENSAJE_SIN_RESPUESTA
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


async def _router(
    herramientas: AsyncToolRegistry,
    tmp_path: pathlib.Path,
    *,
    xml: bool,
    respuestas: tuple[str, ...] = (_RESPUESTA_CRUDA,),
) -> LLMRouter:
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

    pendientes = list(respuestas)

    async def _chat_falso(**_kwargs: Any) -> dict[str, Any]:
        # Cada llamada consume una respuesta; la última se repite si el router pide más turnos.
        texto = pendientes.pop(0) if len(pendientes) > 1 else pendientes[0]
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": texto}]}

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


_LLAMADA_OCULTA = (
    '<thought>Voy a considerar esta operación:\n<tool_call>{"name": "search_mod", "arguments": {"query": "SKSE"}}'
    "</tool_call>\n</thought>"
)
_LLAMADA_REAL = '<tool_call>{"name": "search_mod", "arguments": {"query": "Frostfall"}}</tool_call>'


class TestLasLlamadasAHerramientasOcultasNoSeEjecutan:
    """El parser XML solo ve el texto visible: lo que el modelo «pensó» no es una orden (fail-closed)."""

    async def _correr(
        self, herramientas: AsyncToolRegistry, tmp_path: pathlib.Path, respuestas: tuple[str, ...]
    ) -> tuple[str, AsyncMock]:
        router = await _router(herramientas, tmp_path, xml=True, respuestas=respuestas)
        try:
            with patch.object(router._tools, "execute", new_callable=AsyncMock) as ejecutar:
                ejecutar.return_value = json.dumps({"success": True, "message": ""})
                respuesta = await router.chat(
                    "hola", MagicMock(spec=aiohttp.ClientSession), chat_id="razon-herramienta"
                )
        finally:
            await router.close()
        return respuesta, ejecutar

    async def test_una_llamada_escrita_solo_dentro_del_razonamiento_no_se_ejecuta(
        self, herramientas: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        respuesta, ejecutar = await self._correr(herramientas, tmp_path, (_LLAMADA_OCULTA + "\nRespuesta",))

        ejecutar.assert_not_awaited()
        assert respuesta == "Respuesta"

    async def test_si_toda_la_respuesta_era_razonamiento_con_una_llamada_no_se_ejecuta_y_hay_aviso_neutro(
        self, herramientas: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        respuesta, ejecutar = await self._correr(herramientas, tmp_path, (_LLAMADA_OCULTA,))

        ejecutar.assert_not_awaited()
        assert respuesta == MENSAJE_SIN_RESPUESTA

    async def test_una_llamada_visible_se_ejecuta_y_la_oculta_de_al_lado_no(
        self, herramientas: AsyncToolRegistry, tmp_path: pathlib.Path
    ) -> None:
        """No se sobre-bloquea: la herramienta que el modelo pidió a la vista sigue corriendo, una sola vez."""
        respuesta, ejecutar = await self._correr(
            herramientas, tmp_path, (_LLAMADA_OCULTA + "\n" + _LLAMADA_REAL, "Listo")
        )

        ejecutar.assert_awaited_once()
        assert "Frostfall" in json.dumps(ejecutar.await_args.args) + json.dumps(ejecutar.await_args.kwargs, default=str)
        assert "SKSE" not in json.dumps(ejecutar.await_args.args) + json.dumps(ejecutar.await_args.kwargs, default=str)
        assert respuesta == "Listo"


class TestAnclaDeRetornosDeChat:
    """Enumera TODO ``return`` de ``LLMRouter.chat``: o es un literal de aviso, o es una expresión congelada.

    Una expresión nueva (una variable, un f-string, una llamada) rompe el test hasta que alguien decide si
    puede llevar texto del modelo y, si puede, la pasa por ``ocultar_razonamiento``.
    """

    #: Únicas expresiones no literales que ``chat()`` devuelve. Las dos primeras son el texto del modelo, ya
    #: filtrado (ruta nativa y ruta XML); ``loop_response`` es la respuesta fija del corte de bucle de herramientas.
    EXPRESIONES_CONGELADAS = frozenset(
        {
            "ocultar_razonamiento(final_text)",
            "ocultar_razonamiento(full_text)",
            "loop_response",
        }
    )

    @staticmethod
    def _retornos_de_chat() -> list[ast.Return]:
        arbol = ast.parse(pathlib.Path(router_mod.__file__).read_text(encoding="utf-8"))
        clase = next(n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == "LLMRouter")
        chat = next(n for n in clase.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat")
        return [n for n in ast.walk(chat) if isinstance(n, ast.Return) and n.value is not None]

    def test_todo_return_de_chat_es_un_literal_o_una_expresion_congelada(self) -> None:
        no_permitidos = [
            f"línea {n.lineno}: {ast.unparse(n.value)}"
            for n in self._retornos_de_chat()
            if not (isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
            and ast.unparse(n.value) not in self.EXPRESIONES_CONGELADAS
        ]
        assert no_permitidos == []

    def test_las_dos_rutas_del_texto_del_modelo_pasan_por_el_filtro(self) -> None:
        filtrados = sorted(
            ast.unparse(n.value)
            for n in self._retornos_de_chat()
            if ast.unparse(n.value).startswith("ocultar_razonamiento(")
        )
        assert filtrados == ["ocultar_razonamiento(final_text)", "ocultar_razonamiento(full_text)"]

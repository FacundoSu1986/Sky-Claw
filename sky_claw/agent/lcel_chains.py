# -*- coding: utf-8 -*-
"""
lcel_chains.py - Cadenas LangChain Expression Language para Sky-Claw.
Implementa composición declarativa de prompts y manejo de herramientas
utilizando el patrón LCEL (LangChain Expression Language).
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

try:
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.runnables import RunnableLambda

    LANGCHAIN_AVAILABLE = True
except ImportError:
    LANGCHAIN_AVAILABLE = False
    # Crear stubs para cuando LangChain no está disponible
    ChatPromptTemplate = None
    StrOutputParser = None
    RunnableLambda = object


logger = logging.getLogger(__name__)


class ToolExecutor(RunnableLambda if LANGCHAIN_AVAILABLE else object):
    """Ejecutor de herramientas usando LCEL para composición dinámica."""

    def __init__(self, tool_name: str, tool_description: str):
        """Inicializa el ejecutor de herramientas.

        Args:
            tool_name: Nombre de la herramienta
            tool_description: Descripción de la herramienta
        """
        self.tool_name = tool_name
        self.tool_description = tool_description

    def __call__(self, tool_input: Dict[str, Any]) -> str:
        """Ejecuta la herramienta con el input proporcionado.

        Args:
            tool_input: Diccionario con parámetros para la herramienta

        Returns:
            Resultado de la ejecución como string
        """
        logger.info(
            "Ejecutando herramienta: %s con input: %s", self.tool_name, tool_input
        )

        # Simular ejecución - en producción esto llamaría a la herramienta real
        result = f"[{self.tool_name}] Result: {tool_input}"

        return result


class PromptComposer:
    """Componedor de prompts LCEL para Sky-Claw.

    Nota: Si LangChain no está instalado, los métodos retornan mensajes
    en formato de diccionario compatible con la API de LLM.
    """

    def __init__(
        self,
        system_prompt: str = "Eres un asistente de modding de Skyrim SE/AE.",
        tool_registry: Optional[Any] = None,
    ):
        """Inicializa el compositor de prompts.

        Args:
            system_prompt: Prompt del sistema del asistente
            tool_registry: Registro de herramientas disponibles
        """
        self.system_prompt = system_prompt
        self._tool_registry = tool_registry

    def compose_tool_prompt(
        self, tool_name: str, tool_input: Dict[str, Any], tool_description: str
    ) -> Any:
        """Compone un prompt para una herramienta específica.

        Args:
            tool_name: Nombre de la herramienta
            tool_input: Input para la herramienta
            tool_description: Descripción de la herramienta

        Returns:
            Prompt compuesto en formato LCEL o diccionario si LangChain no está disponible
        """
        if not LANGCHAIN_AVAILABLE:
            # Retornar formato de diccionario compatible con LLM API
            return [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": f"Usa la herramienta {tool_name} para: {tool_description}",
                },
                {"role": "user", "content": f"Input: {tool_input}"},
                {
                    "role": "user",
                    "content": "Ejecuta la herramienta y retorna el resultado.",
                },
            ]

        # Template del prompt
        template = ChatPromptTemplate.from_messages(
            [
                ("system", self.system_prompt),
                ("user", "Usa la herramienta {tool_name} para: {tool_description}"),
                ("user", "Input: {tool_input}"),
                ("user", "Ejecuta la herramienta y retorna el resultado."),
            ]
        )

        # Formatear el input para el template
        formatted_input = {key: str(value) for key, value in tool_input.items()}

        return template.format_messages(**formatted_input)

    def compose_multi_tool_prompt(
        self, tools: List[Dict[str, Any]], task_description: str
    ) -> Any:
        """Compone un prompt para múltiples herramientas.

        Args:
            tools: Lista de herramientas a ejecutar
            task_description: Descripción de la tarea

        Returns:
            Prompt compuesto para múltiples herramientas
        """
        tool_descriptions = "\n".join(
            [f"- {tool['name']}: {tool['description']}" for tool in tools]
        )

        if not LANGCHAIN_AVAILABLE:
            return [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Tarea: {task_description}"},
                {"role": "user", "content": "Herramientas disponibles:"},
                {"role": "user", "content": tool_descriptions},
                {
                    "role": "user",
                    "content": "Ejecuta las herramientas en el orden apropiado.",
                },
            ]

        # Template del prompt
        template = ChatPromptTemplate.from_messages(
            [
                ("system", self.system_prompt),
                ("user", "Tarea: {task_description}"),
                ("user", "Herramientas disponibles:"),
                ("user", tool_descriptions),
                ("user", "Ejecuta las herramientas en el orden apropiado."),
            ]
        )

        formatted_tools = [f"{tool['name']}: {tool['input']}" for tool in tools]

        return template.format_messages(
            tools=formatted_tools, task_description=task_description
        )

    def compose_rag_prompt(self, query: str, context: str, sources: List[str]) -> Any:
        """Compone un prompt para RAG (Retrieval-Augmented Generation).

        Args:
            query: Consulta del usuario
            context: Contexto relevante
            sources: Fuentes de información

        Returns:
            Prompt RAG compuesto
        """
        if not LANGCHAIN_AVAILABLE:
            return [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Consulta: {query}"},
                {"role": "user", "content": "Contexto:"},
                {"role": "user", "content": context},
                {"role": "user", "content": "Fuentes:"},
                {"role": "user", "content": str(sources)},
                {
                    "role": "user",
                    "content": "Usa el contexto y las fuentes para responder la consulta.",
                },
            ]

        template = ChatPromptTemplate.from_messages(
            [
                ("system", self.system_prompt),
                ("user", "Consulta: {query}"),
                ("user", "Contexto:"),
                ("user", context),
                ("user", "Fuentes:"),
                ("user", sources),
                ("role", "user"),
                (
                    "content",
                    "Usa el contexto y las fuentes para responder la consulta.",
                ),
            ]
        )

        return template.format_messages(query=query, context=context, sources=sources)


class ChainBuilder:
    """Constructor de cadenas LCEL para Sky-Claw."""

    def __init__(self, tool_executor: ToolExecutor):
        """Inicializa el constructor de cadenas.

        Args:
            tool_executor: Ejecutor de herramientas
        """
        self._tool_executor = tool_executor

    def create_tool_chain(
        self, tool_name: str, tool_description: str, next_step: Optional[str] = None
    ) -> Any:
        """Crea una cadena LCEL para ejecutar una herramienta.

        Args:
            tool_name: Nombre de la herramienta
            tool_description: Descripción de la herramienta
            next_step: Siguiente paso en la cadena (opcional)

        Returns:
            RunnableLambda que ejecuta la herramienta o stub si LangChain no está disponible
        """
        if not LANGCHAIN_AVAILABLE:
            return lambda x: self._tool_executor(tool_input=x)

        tool = self._tool_executor(tool_name, tool_description)

        # Definir la cadena LCEL
        chain = {
            "role": "user",
            "content": f"Ejecuta la herramienta {tool_name}.",
        } | tool

        if next_step:
            chain = (chain, {"role": "user", "content": f"Luego, {next_step}."})

        return chain

    def create_sequential_chain(
        self, steps: List[Dict[str, Any]], task_description: str
    ) -> Any:
        """Crea una cadena secuencial de pasos.

        Args:
            steps: Lista de pasos a ejecutar
            task_description: Descripción de la tarea

        Returns:
            RunnableLambda que ejecuta los pasos en secuencia o stub si LangChain no está disponible
        """
        if not LANGCHAIN_AVAILABLE:

            def execute_steps(x: Any) -> list[Any]:
                results = []
                for step in steps:
                    tool = self._tool_executor(
                        step.get("tool", "Herramienta"),
                        step.get("description", "Descripci��n de paso"),
                    )
                    results.append(tool(x))
                return results

            return execute_steps

        chain_steps: list[Any] = []
        chain: Any = None

        for i, step in enumerate(steps):
            step_tool = self._tool_executor(
                step.get("tool", "Herramienta"),
                step.get("description", f"Descripción de paso {i + 1}"),
            )

            # Capturar step_tool en el closure de forma segura
            captured_tool = step_tool
            step_lambda = RunnableLambda(
                func=lambda x, t=captured_tool: t(x), name=f"step_{i + 1}"
            )

            if not chain_steps:
                chain_steps = [step_lambda]
                chain = step_lambda
            else:
                chain_steps.append(step_lambda)
                chain = (chain, step_lambda)

        return chain

    def create_conditional_chain(
        self, condition: str, true_chain: Any, false_chain: Any
    ) -> Any:
        """Crea una cadena condicional LCEL.

        Args:
            condition: Descripción de la condición
            true_chain: Cadena a ejecutar si la condición es verdadera
            false_chain: Cadena a ejecutar si la condición es falsa

        Returns:
            RunnableLambda que ejecuta la cadena apropiada o stub si LangChain no está disponible
        """

        def route(x: Any) -> Any:
            return true_chain(x) if condition else false_chain(x)

        return route

    def create_with_retry(
        self, chain: Any, max_retries: int = 3, retry_delay: float = 1.0
    ) -> Any:
        """Crea una cadena con lógica de reintentos.

        Args:
            chain: Cadena a ejecutar
            max_retries: Máximo número de reintentos
            retry_delay: Tiempo de espera entre reintentos

        Returns:
            RunnableLambda con lógica de reintentos o stub si LangChain no está disponible
        """

        async def execute_with_retry(x: Any) -> Any:
            """Ejecuta con reintentos usando backoff."""
            last_error: Exception | None = None
            for attempt in range(max_retries):
                try:
                    result = chain(x)
                    if asyncio.iscoroutine(result):
                        return await result
                    return result
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Intento %d/%d falló: %s", attempt + 1, max_retries, exc
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
            raise last_error  # type: ignore[misc]

        if not LANGCHAIN_AVAILABLE:
            return execute_with_retry

        return RunnableLambda(execute_with_retry)


# ---------------------------------------------------------------------------
# Instancias globales para uso común
# ---------------------------------------------------------------------------
_prompt_composer = PromptComposer(
    system_prompt="Eres un asistente de modding de Skyrim SE/AE."
)
_default_tool_executor = ToolExecutor(
    tool_name="default", tool_description="Herramienta por defecto para Sky-Claw"
)
chain_builder = ChainBuilder(tool_executor=_default_tool_executor)

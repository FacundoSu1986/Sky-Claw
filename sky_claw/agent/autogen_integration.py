"""
autogen_integration.py - Integración de Microsoft AutoGen para Sky-Claw.
Implementa orquestación multi-agente con conversaciones entre agentes,
usando los módulos existentes (ToolExecutor, PromptComposer, RouteClassification).

Nota: Si AutoGen no está instalado, el módulo funciona en modo degradado
usando implementaciones stub.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Optional

try:
    import autogen  # noqa: F401
    from autogen import AssistantAgent, GroupChat, GroupChatManager, UserProxyAgent

    AUTOGEN_AVAILABLE = True
except ImportError:
    AUTOGEN_AVAILABLE = False
    # Crear stubs para cuando AutoGen no está disponible
    AssistantAgent = object
    UserProxyAgent = object
    GroupChat = object
    GroupChatManager = object


logger = logging.getLogger("SkyClaw.AutoGenIntegration")


class AutoGenConfig:
    """Configuración para agentes AutoGen."""

    def __init__(
        self,
        model: str = "gpt-4",
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        timeout: int = 60,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    def to_llm_config(self) -> dict[str, Any]:
        """Convierte la configuración a formato AutoGen LLM config."""
        config = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
        }
        if self.api_key:
            config["api_key"] = self.api_key
        if self.base_url:
            config["base_url"] = self.base_url
        return config


class SkyClawConversableAgent(ABC):
    """Clase base abstracta para agentes conversacionales de Sky-Claw.

    Esta clase proporciona una interfaz común para agentes que pueden participar
    en conversaciones multi-agente, independientemente de si AutoGen está disponible.
    """

    def __init__(
        self,
        name: str,
        system_message: str,
        tool_executor: Any | None = None,
        **kwargs,
    ):
        self.name = name
        self.system_message = system_message
        self._tool_executor = tool_executor
        self._message_history: list[dict[str, Any]] = []
        self._kwargs = kwargs

    @abstractmethod
    async def send_message(
        self, message: str, recipient: Optional["SkyClawConversableAgent"] = None
    ) -> str:
        """Envía un mensaje a otro agente o al grupo."""
        pass

    @abstractmethod
    async def receive_message(
        self, message: str, sender: "SkyClawConversableAgent"
    ) -> str:
        """Recibe un mensaje de otro agente."""
        pass

    def get_history(self) -> list[dict[str, Any]]:
        """Retorna el historial de mensajes del agente."""
        return self._message_history.copy()


class AutoGenWrapper(SkyClawConversableAgent):
    """Wrapper para agentes AutoGen que integra con la arquitectura Sky-Claw.

    Este wrapper permite usar agentes AutoGen (AssistantAgent, UserProxyAgent)
    con los componentes existentes de Sky-Claw como ToolExecutor y RouteClassification.
    """

    def __init__(
        self,
        name: str,
        system_message: str,
        agent_type: str = "assistant",
        tool_executor: Any | None = None,
        config: AutoGenConfig | None = None,
        human_input_mode: str = "NEVER",
        max_consecutive_auto_reply: int = 10,
        **kwargs,
    ):
        super().__init__(name, system_message, tool_executor, **kwargs)

        self.agent_type = agent_type
        self.human_input_mode = human_input_mode
        self.max_consecutive_auto_reply = max_consecutive_auto_reply
        self._config = config or AutoGenConfig()
        self._autogen_agent = None

        if AUTOGEN_AVAILABLE:
            self._initialize_autogen_agent()
        else:
            logger.warning(f"AutoGen no disponible. Usando stub para agente {name}")

    def _initialize_autogen_agent(self):
        """Inicializa el agente AutoGen correspondiente."""
        llm_config = {
            "config_list": [self._config.to_llm_config()],
            "timeout": self._config.timeout,
        }

        if self.agent_type == "assistant":
            self._autogen_agent = AssistantAgent(
                name=self.name,
                system_message=self.system_message,
                llm_config=llm_config,
                **self._kwargs,
            )
        elif self.agent_type == "user_proxy":
            self._autogen_agent = UserProxyAgent(
                name=self.name,
                system_message=self.system_message,
                human_input_mode=self.human_input_mode,
                max_consecutive_auto_reply=self.max_consecutive_auto_reply,
                **self._kwargs,
            )

        logger.info(f"Agente AutoGen inicializado: {self.name} ({self.agent_type})")

    async def send_message(
        self, message: str, recipient: Optional["AutoGenWrapper"] = None
    ) -> str:
        """Envía un mensaje a otro agente o al grupo.

        Args:
            message: Contenido del mensaje
            recipient: Agente destinatario (opcional para broadcast)

        Returns:
            Respuesta del destinatario o confirmación de envío
        """
        self._message_history.append(
            {
                "role": "sender",
                "content": message,
                "recipient": recipient.name if recipient else "broadcast",
            }
        )

        if AUTOGEN_AVAILABLE and self._autogen_agent:
            if recipient and recipient._autogen_agent:
                # Conversación directa
                self._autogen_agent.initiate_chat(
                    recipient._autogen_agent, message=message
                )
                # Obtener última respuesta
                last_message = recipient._autogen_agent.last_message()
                return last_message.get("content", "")
            else:
                logger.warning("No hay destinatario válido para AutoGen")
                return "Message logged (no recipient)"
        else:
            # Modo stub: simular respuesta
            logger.debug(f"[STUB] Mensaje enviado: {message[:50]}...")
            if recipient:
                return await recipient.receive_message(message, self)
            return "Message logged (stub mode)"

    async def receive_message(self, message: str, sender: "AutoGenWrapper") -> str:
        """Recibe un mensaje de otro agente.

        Args:
            message: Contenido del mensaje
            sender: Agente remitente

        Returns:
            Respuesta al mensaje
        """
        self._message_history.append(
            {"role": "receiver", "content": message, "sender": sender.name}
        )

        if AUTOGEN_AVAILABLE and self._autogen_agent:
            # Procesar con AutoGen
            response = self._autogen_agent.generate_reply(
                messages=[{"role": "user", "content": message}]
            )
            return response
        else:
            # Modo stub: respuesta simulada
            return f"[{self.name}] Recibido: {message[:30]}..."


class MultiAgentOrchestrator:
    """Orquestador de conversaciones multi-agente.

    Coordina conversaciones entre múltiples agentes usando GroupChat
    de AutoGen cuando está disponible, o una implementación stub cuando no.
    """

    def __init__(
        self,
        agents: list[AutoGenWrapper],
        max_round: int = 10,
        route_classification_callback: Callable | None = None,
    ):
        self.agents = agents
        self.max_round = max_round
        self._route_callback = route_classification_callback
        self._group_chat = None
        self._manager = None

        if AUTOGEN_AVAILABLE:
            self._initialize_group_chat()

    def _initialize_group_chat(self):
        """Inicializa el GroupChat de AutoGen."""
        autogen_agents = [
            agent._autogen_agent
            for agent in self.agents
            if agent._autogen_agent is not None
        ]

        if autogen_agents:
            self._group_chat = GroupChat(
                agents=autogen_agents, messages=[], max_round=self.max_round
            )
            self._manager = GroupChatManager(groupchat=self._group_chat)
            logger.info(f"GroupChat inicializado con {len(autogen_agents)} agentes")

    async def run_conversation(
        self, initial_message: str, starter_agent: AutoGenWrapper | None = None
    ) -> dict[str, Any]:
        """Ejecuta una conversación multi-agente.

        Args:
            initial_message: Mensaje inicial para iniciar la conversación
            starter_agent: Agente que inicia la conversación (opcional)

        Returns:
            Diccionario con resultados de la conversación
        """
        results = {
            "initial_message": initial_message,
            "messages": [],
            "participants": [agent.name for agent in self.agents],
            "rounds": 0,
            "status": "pending",
        }

        if AUTOGEN_AVAILABLE and self._group_chat and self._manager:
            # Usar AutoGen GroupChat
            starter = starter_agent or self.agents[0]
            if starter._autogen_agent:
                starter._autogen_agent.initiate_chat(
                    self._manager, message=initial_message
                )

                # Recopilar mensajes del GroupChat
                results["messages"] = self._group_chat.messages
                results["rounds"] = len(self._group_chat.messages)
                results["status"] = "completed"

                logger.info(f"Conversación completada: {results['rounds']} rondas")
        else:
            # Modo stub: simular conversación
            results["status"] = "stub_mode"
            current_message = initial_message

            for i, agent in enumerate(self.agents):
                response = await agent.receive_message(
                    current_message, self.agents[(i - 1) % len(self.agents)]
                )
                results["messages"].append({"agent": agent.name, "content": response})
                current_message = response
                results["rounds"] = i + 1

            logger.warning(
                "Conversación ejecutada en modo stub (AutoGen no disponible)"
            )

        return results

    def add_agent(self, agent: AutoGenWrapper) -> None:
        """Agrega un nuevo agente al orquestador."""
        self.agents.append(agent)
        if AUTOGEN_AVAILABLE and agent._autogen_agent:
            self._group_chat.agents.append(agent._autogen_agent)
            logger.info(f"Agente {agent.name} agregado al GroupChat")

    def remove_agent(self, agent_name: str) -> bool:
        """Remueve un agente del orquestador por nombre."""
        for i, agent in enumerate(self.agents):
            if agent.name == agent_name:
                self.agents.pop(i)
                if AUTOGEN_AVAILABLE and self._group_chat:
                    self._group_chat.agents = [
                        a for a in self._group_chat.agents if a.name != agent_name
                    ]
                logger.info(f"Agente {agent_name} removido del GroupChat")
                return True
        return False


def create_sky_claw_agents(
    tool_executor: Any, config: AutoGenConfig | None = None
) -> dict[str, AutoGenWrapper]:
    """Factory function para crear agentes Sky-Claw preconfigurados.

    Crea un conjunto de agentes conversacionales especializados para
    las diferentes tareas de Sky-Claw:
    - SupervisorAgent: Coordina y despacha tareas
    - ScraperAgent: Maneja scraping de Nexus Mods
    - SecurityAgent: Realiza auditorías de seguridad
    - DatabaseAgent: Gestiona operaciones de base de datos

    Args:
        tool_executor: Ejecutor de herramientas para los agentes
        config: Configuración AutoGen (opcional)

    Returns:
        Diccionario con agentes preconfigurados por nombre
    """
    agents = {}

    # Supervisor Agent
    agents["supervisor"] = AutoGenWrapper(
        name="SupervisorAgent",
        system_message="""Eres el agente supervisor de Sky-Claw. Tu rol es:
1. Coordinar las tareas entre los agentes especializados
2. Despachar solicitudes al agente apropiado
3. Sintetizar resultados de múltiples agentes
4. Mantener el estado global del sistema

Debes usar RouteClassification para determinar qué agente debe manejar cada solicitud.""",
        agent_type="assistant",
        tool_executor=tool_executor,
        config=config,
    )

    # Scraper Agent
    agents["scraper"] = AutoGenWrapper(
        name="ScraperAgent",
        system_message="""Eres el agente de scraping de Sky-Claw. Tu rol es:
1. Consultar Nexus Mods para obtener metadata de mods
2. Manejar el modo stealth cuando sea necesario
3. Respetar el Circuit Breaker para evitar baneos
4. Extraer información estructurada de las respuestas

Usa las herramientas de scraping disponibles para completar las tareas.""",
        agent_type="assistant",
        tool_executor=tool_executor,
        config=config,
    )

    # Security Agent
    agents["security"] = AutoGenWrapper(
        name="SecurityAgent",
        system_message="""Eres el agente de seguridad de Sky-Claw. Tu rol es:
1. Auditar archivos y código en busca de vulnerabilidades
2. Detectar patrones maliciosos usando análisis estático
3. Validar inputs contra schemas Pydantic
4. Reportar hallazgos con severidad y recomendaciones

Aplica el framework metacognitivo de 5 fases para análisis profundo.""",
        agent_type="assistant",
        tool_executor=tool_executor,
        config=config,
    )

    # Database Agent
    agents["database"] = AutoGenWrapper(
        name="DatabaseAgent",
        system_message="""Eres el agente de base de datos de Sky-Claw. Tu rol es:
1. Gestionar consultas a SQLite con modo WAL
2. Mantener la integridad de los datos de mods
3. Registrar actividad y logs del sistema
4. Optimizar consultas para rendimiento

Usa las herramientas de base de datos disponibles para las operaciones CRUD.""",
        agent_type="assistant",
        tool_executor=tool_executor,
        config=config,
    )

    logger.info(f"Creados {len(agents)} agentes Sky-Claw preconfigurados")
    return agents


# Instancia global del orquestador (lazy initialization)
_orchestrator_instance: MultiAgentOrchestrator | None = None


def get_orchestrator(
    tool_executor: Any, config: AutoGenConfig | None = None, force_new: bool = False
) -> MultiAgentOrchestrator:
    """Obtiene la instancia global del orquestador multi-agente.

    Args:
        tool_executor: Ejecutor de herramientas
        config: Configuración AutoGen
        force_new: Si True, crea una nueva instancia

    Returns:
        Instancia del orquestador multi-agente
    """
    global _orchestrator_instance

    if _orchestrator_instance is None or force_new:
        agents = create_sky_claw_agents(tool_executor, config)
        _orchestrator_instance = MultiAgentOrchestrator(agents=agents)

    return _orchestrator_instance

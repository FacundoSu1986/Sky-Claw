import re
import logging
from typing import TypedDict, Literal, Dict, Any

# Standard 2026 Intent Classification
IntentType = Literal["COMANDO_SISTEMA", "CONSULTA_MODDING", "EJECUCION_HERRAMIENTA"]

logger = logging.getLogger("SkyClaw.SemanticRouter")

class RoutedCommand(TypedDict):
    intent: IntentType
    action: str
    original_text: str
    metadata: Dict[str, Any]

class SemanticRouter:
    """
    COGNITIVE ROUTING LAYER (STANDARD 2026)
    
    Orchestrates incoming traffic from ws_daemon using deterministic regex 
    patterns for latency-free edge classification.
    """
    
    # Branch A: COMANDO_SISTEMA
    SYSTEM_PATTERNS = [
        r"(?i)\b(status|estado|stop|detener|exit|salir|ping|uptime)\b",
        r"(?i)\b(help|ayuda|info|version)\b"
    ]
    
    # Branch C: EJECUCION_HERRAMIENTA
    TOOL_PATTERNS = [
        r"(?i)\b(xedit|cleaning|limpiar|lootsort|ordenar|loot|pandora|bodyslide|nemesis|fomod)\b",
        r"(?i)\b(install|instalar|remove|borrar|update|actualizar|download|descargar)\b",
        r"(?i)\b(sync|sincronizar|check|verificar)\b"
    ]

    def route(self, data: Dict[str, Any]) -> RoutedCommand:
        """
        Zero-Shot Regex Router. Validates intent before LLM tokenization.
        """
        text: str = data.get("payload", {}).get("text", "").strip()
        metadata: Dict[str, Any] = data.get("metadata", {})
        
        # 1. High-priority: System commands (Deterministic branch)
        for pattern in self.SYSTEM_PATTERNS:
            if re.search(pattern, text):
                return {
                    "intent": "COMANDO_SISTEMA",
                    "action": "system_core_op",
                    "original_text": text,
                    "metadata": metadata
                }
        
        # 2. Tool Execution (Direct tools branch)
        for pattern in self.TOOL_PATTERNS:
            if re.search(pattern, text):
                return {
                    "intent": "EJECUCION_HERRAMIENTA",
                    "action": "managed_tool_invocation",
                    "original_text": text,
                    "metadata": metadata
                }
        
        # 3. Default: Modding Knowledge Base / RAG (Cognitive branch)
        return {
            "intent": "CONSULTA_MODDING",
            "action": "rag_cognitive_query",
            "original_text": text,
            "metadata": metadata
        }

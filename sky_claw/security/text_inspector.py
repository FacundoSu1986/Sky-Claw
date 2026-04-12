"""
Sky-Claw TextInspector v5.5 (Abril 2026)
Analizador de inyección de prompts indirecta (Indirect Prompt Injection).
Detecta patrones maliciosos en archivos de texto, MD y configuración.
"""

import re
import logging
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

# Patrones de Inyección de Prompts comunes (OWASP LLM01)
INJECTION_PATTERNS = [
    (
        r"(?i)ignore\s+(all\s+)?(previous|prior)\s+instructions",
        "Bypass de instrucciones previas",
        "CRITICAL",
    ),
    (r"(?i)system\s*prompt:", "Intento de sobrescritura de prompt de sistema", "HIGH"),
    (
        r"(?i)\[(INST|INST\b|/INST)\]",
        "Patrón de instrucción de modelo (Llama/Mistral style)",
        "HIGH",
    ),
    (r"(?i)!system", "Directiva de sistema inyectada", "CRITICAL"),
    (
        r"(?i)forgotten\s*your\s*previous\s*commands",
        "Técnica de amnesia forzada de prompt",
        "MEDIUM",
    ),
    (
        r"(?i)as\s*a\s*developer\s*you\s*must",
        "Ingeniería social de rol (Jailbreak style)",
        "MEDIUM",
    ),
    (
        r"(?i)print\s+(the\s+)?(entire|full)?\s*(prompt|initial\s*instruction)",
        "Intento de extracción de prompt",
        "HIGH",
    ),
]

# Patrones de Homoglifos y caracteres invisibles sospechosos (Steganography)
SUSPICIOUS_UNICODE = [
    (
        r"[\u200B-\u200D\u2060\uFEFF]",
        "Caracteres invisibles detectados (Posible esteganografía)",
        "MEDIUM",
    ),
    (
        r"[\u0430\u0435\u043E\u0440\u0441\u0443\u0445]",
        "Homoglifos cirílicos detectados en texto latino",
        "LOW",
    ),
]


class TextInspector:
    def __init__(self, max_bytes: int = 10240):  # 10 KB límite por rendimiento
        self.max_bytes = max_bytes

    def inspect(self, content: str, filename: str = "doc.md") -> List[Dict[str, Any]]:
        """Busca patrones de inyección y anomalías en el texto."""
        findings = []

        # Limitar contenido para rendimiento
        content_fragment = content[: self.max_bytes]

        # 1. Buscar inyección de prompts
        for pattern, desc, severity in INJECTION_PATTERNS:
            matches = re.finditer(pattern, content_fragment)
            for m in matches:
                line_idx = content_fragment.count("\n", 0, m.start()) + 1
                findings.append(
                    {
                        "message": f"Posible Indirect Prompt Injection: {desc}",
                        "line": line_idx,
                        "severity": severity,
                        "confidence": 0.85,
                        "file": filename,
                    }
                )

        # 2. Buscar anomalías Unicode
        for pattern, desc, severity in SUSPICIOUS_UNICODE:
            matches = re.finditer(pattern, content_fragment)
            for m in matches:
                line_idx = content_fragment.count("\n", 0, m.start()) + 1
                findings.append(
                    {
                        "message": f"Anomalía de texto: {desc}",
                        "line": line_idx,
                        "severity": severity,
                        "confidence": 0.7,
                        "file": filename,
                    }
                )

        return findings


def scan_text(content: str, filename: str = "README.md") -> List[Dict[str, Any]]:
    """Punto de entrada principal para inspección de texto."""
    inspector = TextInspector()
    return inspector.inspect(content, filename)

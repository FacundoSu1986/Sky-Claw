"""
Sky-Claw TextInspector v5.5 (Abril 2026)
Analizador de inyección de prompts indirecta (Indirect Prompt Injection).
Detecta patrones maliciosos en archivos de texto, MD y configuración.
"""

import logging
import re
from typing import Any

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
    def __init__(self, max_chars: int = 10240):  # Límite por rendimiento: 10_240 caracteres
        self.max_chars = max_chars

    def inspect(self, content: str, filename: str = "doc.md") -> list[dict[str, Any]]:
        """Busca patrones de inyección y anomalías en el texto.

        SEC-09: Uses sliding-window analysis (start + end of content) to detect
        payloads placed beyond the initial ``max_chars`` boundary.
        """
        findings: list[dict[str, Any]] = []

        # SEC-09: Analizar inicio Y final del contenido (ventanas deslizantes)
        if len(content) <= self.max_chars:
            fragments = [content]
        else:
            half = self.max_chars // 2
            fragments = [content[:half], content[-half:]]

        for content_fragment in fragments:
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

        # Deduplicate findings by (message, severity, file) to avoid double-reporting
        seen: set[tuple[str, str, str]] = set()
        unique_findings: list[dict[str, Any]] = []
        for f in findings:
            key = (f["message"], f["severity"], f["file"])
            if key not in seen:
                seen.add(key)
                unique_findings.append(f)

        return unique_findings


def scan_text(content: str, filename: str = "README.md") -> list[dict[str, Any]]:
    """Punto de entrada principal para inspección de texto."""
    inspector = TextInspector()
    return inspector.inspect(content, filename)

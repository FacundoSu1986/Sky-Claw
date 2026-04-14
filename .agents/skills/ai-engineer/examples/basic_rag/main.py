#!/usr/bin/env python3
"""
Ejemplo básico de sistema RAG para Sky-Claw.

Este ejemplo demuestra:
- Indexación de documentos
- Búsqueda semántica
- Generación de respuestas con contexto

Uso:
    python examples/basic_rag/main.py
"""

import logging
from pathlib import Path

from sky_claw.services.rag_pipeline import RAGPipeline, HybridRetriever

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    """Ejecutar ejemplo RAG básico."""
    logger.info("Iniciando ejemplo RAG básico")

    # Nota: Este es un template - adaptar a implementación real
    # pipeline = RAGPipeline(...)
    # result = pipeline.generate("¿Cómo instalar mods en Skyrim?")
    # logger.info(f"Respuesta: {result['response']}")

    logger.info("Ejemplo completado - adaptar a tu implementación")


if __name__ == "__main__":
    main()

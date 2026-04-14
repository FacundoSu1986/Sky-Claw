#!/usr/bin/env python3
"""
Ejemplo de sistema multimodal para Sky-Claw.

Este ejemplo demuestra:
- Procesamiento de imágenes
- Análisis de documentos
- Respuestas combinadas (texto + imagen)

Uso:
    python examples/multimodal/main.py --image screenshot.png
"""

import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    """Ejecutar ejemplo multimodal."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, help="Imagen a procesar")
    parser.add_argument("--document", type=Path, help="Documento a analizar")
    args = parser.parse_args()

    logger.info("Iniciando ejemplo multimodal")

    # Nota: Este es un template - adaptar a implementación real
    # if args.image:
    #     result = vision_model.analyze(args.image)
    #     logger.info(f"Análisis: {result}")

    logger.info("Ejemplo completado - adaptar a tu implementación")


if __name__ == "__main__":
    main()

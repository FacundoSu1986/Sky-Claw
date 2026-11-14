"""Ids de trabajos en segundo plano del agente LLM.

Un contador por proceso (``itertools.count``) repite ``admision-1`` en cada arranque, y dos sesiones
distintas terminan con un mismo id para trabajos distintos (#920). El id lleva un sello de tiempo y un
sufijo aleatorio corto: legible en los registros y sin colisiones entre arranques.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def nuevo_id_de_trabajo(prefijo: str) -> str:
    """``<prefijo>-AAAAMMDD-HHMMSS-<6 hex>``, p. ej. ``admision-20261112-162244-a1b2c3``."""
    sello = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{prefijo}-{sello}-{secrets.token_hex(3)}"

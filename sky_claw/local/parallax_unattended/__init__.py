"""Parallax desatendido: decidir si una corrida SIRVE, no sólo si terminó.

Esta capa **consume artefactos** —el informe del generador y los archivos de salida— y no
importa nada de ``sky_claw.local.native_parallax``: el generador puede cambiar por dentro sin
redefinir en silencio qué significa "listo" (ver ``AGENTS.md``).

El orden de uso en una corrida desatendida es siempre el mismo:

1. ``dictar(informe)`` — ¿el informe se sostiene y nada quedó en revisión?
2. ``codigo_de_salida(decision)`` — 0 sólo con ``PASS``.
3. ``auditar(informe, raiz)`` — ¿cada artefacto declarado está en disco y tiene relieve?
"""

from __future__ import annotations

from .auditor import (
    SD_MINIMA,
    CabeceraDDS,
    CodigoDeHallazgo,
    Hallazgo,
    InformeDeAuditoria,
    auditar,
    codigos,
)
from .veredicto import (
    CODIGOS_DE_SALIDA,
    ESTADOS_CONOCIDOS,
    MOTIVOS_DE_FALLA,
    MOTIVOS_DE_INCONSISTENCIA,
    DecisionDelLote,
    MotivoDelLote,
    VeredictoDelLote,
    codigo_de_salida,
    dictar,
)

__all__ = [
    "CODIGOS_DE_SALIDA",
    "ESTADOS_CONOCIDOS",
    "MOTIVOS_DE_FALLA",
    "MOTIVOS_DE_INCONSISTENCIA",
    "SD_MINIMA",
    "CabeceraDDS",
    "CodigoDeHallazgo",
    "DecisionDelLote",
    "Hallazgo",
    "InformeDeAuditoria",
    "MotivoDelLote",
    "VeredictoDelLote",
    "auditar",
    "codigo_de_salida",
    "codigos",
    "dictar",
]

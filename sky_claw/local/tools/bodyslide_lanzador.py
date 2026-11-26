"""Contrato del lanzador de BodySlide (PR 2c-2b): quién spawnea y qué prueba sobre la terminalidad.

El runner (``bodyslide_runner``) es dueño del journal, la siembra, la restauración y el
veredicto. El spawn es inyectable para que el backend brokered (USVFS, ``brokered_bodyslide``)
use la MISMA orquestación que el standalone: si cada backend tuviera su copia del journal,
la primera corrección aterrizaría en un camino y no en el otro.

Propiedad: *el lanzador reporta; el runner decide*. Todo lanzador cumple:

- devolver :class:`SalidaDelLanzamiento`, lanzar ``TimeoutError`` o cualquier otro error
  significa que **el proceso terminó**: el runner registra ``TERMINAL`` y restaura;
- lanzar :class:`TerminalidadDesconocidaError` significa que **el proceso pudo seguir
  vivo**: el runner deja el journal en ``LANZADO`` y NO restaura
  (``HOLD_TERMINALITY_UNKNOWN``). Lo libera el operador con evidencia
  (``--mode bodyslide-recovery``);
- la cancelación se propaga: HOLD sin restaurar, la regla de 2c-1.

La tabla se congela en ``tests/test_bodyslide_lanzador.py``.

Este módulo no lanza procesos: solo define el contrato.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Protocol


class TerminalidadDesconocidaError(Exception):
    """El lanzador no puede probar que el proceso terminó: el runner no restaura la config."""


@dataclasses.dataclass(frozen=True, slots=True)
class PedidoDeLanzamiento:
    """Lo que el runner pide lanzar.

    ``argv`` es el que el runner construyó con ``construir_argv``. Los campos sueltos son
    los del contrato headless: el backend brokered NO manda el argv al worker; el worker lo
    reconstruye con esos campos y la misma función, así un argv armado nunca cruza el IPC.
    """

    argv: tuple[str, ...]
    exe: pathlib.Path
    cwd: pathlib.Path
    timeout: float
    group: str
    target: str
    preset: str | None
    preset_verified: bool
    build_morphs: bool
    quiet_verified: bool


@dataclasses.dataclass(frozen=True, slots=True)
class SalidaDelLanzamiento:
    """Lo que dejó un proceso que terminó."""

    stdout: bytes
    stderr: bytes
    return_code: int


class LanzadorDeBodySlide(Protocol):
    """Spawn de BodySlide. Ver la tabla de terminalidad en el docstring del módulo."""

    async def lanzar(self, pedido: PedidoDeLanzamiento) -> SalidaDelLanzamiento: ...


__all__ = [
    "LanzadorDeBodySlide",
    "PedidoDeLanzamiento",
    "SalidaDelLanzamiento",
    "TerminalidadDesconocidaError",
]

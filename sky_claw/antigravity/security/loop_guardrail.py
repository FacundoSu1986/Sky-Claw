"""Cognitive circuit breaker that detects agentic action loops.

Mientras que ``sky_claw.antigravity.scraper.masterlist._CircuitBreaker`` protege la capa de
red (baneos de IP), esta clase protege la capa cognitiva: detecta cuando el
LLM intenta ejecutar la misma herramienta con los mismos argumentos repetidas
veces seguidas (el típico síntoma de un agente atascado) y transfiere el
control a un humano vía HITL.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from typing import Any

from sky_claw.antigravity.core.models import CircuitBreakerTrippedError

logger = logging.getLogger("SkyClaw.AgenticLoopGuardrail")


class AgenticLoopGuardrail:
    """Sliding-window detector for repeated (tool_name, tool_args) actions."""

    __slots__ = ("_max_repeats", "_history")

    def __init__(self, max_repeats: int = 3, window_size: int = 5) -> None:
        if max_repeats < 2:
            raise ValueError("max_repeats must be >= 2")
        if window_size < max_repeats:
            raise ValueError("window_size must be >= max_repeats")
        self._max_repeats = max_repeats
        self._history: deque[str] = deque(maxlen=window_size)

    def register_and_check(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        """Register the action and raise if a loop is detected.

        Args:
            tool_name: Nombre de la herramienta que está por invocarse.
            tool_args: Kwargs con los que se invocará la herramienta.

        Raises:
            CircuitBreakerTrippedError: Si la misma (tool_name, tool_args) aparece
                ``max_repeats`` veces **consecutivas** (últimos N elementos idénticos).
                Detección de bucles infinitos, no incidental repeats.
                Tras el disparo, el historial queda limpio para que una
                eventual aprobación HITL pueda reintentar sin re-tripear.
        """
        args_str = json.dumps(tool_args, sort_keys=True, default=str)
        action_hash = hashlib.sha256(f"{tool_name}|{args_str}".encode()).hexdigest()

        self._history.append(action_hash)

        # FIX 3: Check if the last max_repeats elements are all identical (consecutive detection)
        if len(self._history) >= self._max_repeats:
            last_n = list(self._history)[-self._max_repeats :]
            if all(h == last_n[0] for h in last_n):
                logger.critical(
                    "Loop Detectado: el agente intentó ejecutar %s %d veces seguidas consecutivas. Activando cortacircuitos cognitivo.",
                    tool_name,
                    self._max_repeats,
                )
                self._history.clear()
                raise CircuitBreakerTrippedError(
                    f"Has entrado en un bucle intentando usar '{tool_name}'. "
                    "DETENTE. Solicita asistencia humana (HITL) inmediatamente.",
                    tool_name=tool_name,
                    occurrences=self._max_repeats,
                )

    def reset(self) -> None:
        """Limpia el historial. Úsalo cuando un humano aprueba retomar tras HITL."""
        self._history.clear()

    def snapshot(self) -> tuple[str, ...]:
        """Devuelve una copia inmutable del historial actual (útil para tests)."""
        return tuple(self._history)

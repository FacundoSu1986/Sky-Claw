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

    def __init__(self, max_repeats: int = 3, window_size: int = 6) -> None:
        if max_repeats < 2:
            raise ValueError("max_repeats must be >= 2")
        if window_size < max_repeats:
            raise ValueError("window_size must be >= max_repeats")
        self._max_repeats = max_repeats
        self._history: deque[str] = deque(maxlen=window_size)

    def register_and_check(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        """Register the action and raise if a loop is detected.

        Detecta dos patrones de bucle sobre la ventana deslizante de acciones:

        1. **Repeticiones idénticas** (período 1): la misma (tool_name, tool_args)
           ``max_repeats`` veces seguidas — A,A,A.
        2. **Ciclos oscilantes** (período ≥ 2): un bloque de longitud ``p`` que se
           repite dos veces seguidas — A,B,A,B o A,B,C,A,B,C. El síntoma típico de
           un agente que alterna entre herramientas sin progresar.

        Args:
            tool_name: Nombre de la herramienta que está por invocarse.
            tool_args: Kwargs con los que se invocará la herramienta.

        Raises:
            CircuitBreakerTrippedError: Al detectar cualquiera de los dos patrones.
                Tras el disparo, el historial queda limpio para que una eventual
                aprobación HITL pueda reintentar sin re-tripear.
        """
        args_str = json.dumps(tool_args, sort_keys=True, default=str)
        action_hash = hashlib.sha256(f"{tool_name}|{args_str}".encode()).hexdigest()

        self._history.append(action_hash)
        hist = list(self._history)
        n = len(hist)

        # 1. Repeticiones idénticas (período 1): últimos max_repeats hashes iguales.
        if n >= self._max_repeats and all(h == hist[-1] for h in hist[-self._max_repeats :]):
            self._trip(tool_name, "repeticiones idénticas consecutivas", self._max_repeats)

        # 2. Ciclos oscilantes (período p ≥ 2): los últimos 2p hashes son un bloque
        #    de longitud p repetido dos veces. El guard ``len(set(...)) > 1`` evita
        #    solapar con la detección idéntica (A,A,A,A la corta la rama de arriba).
        #    Se evalúa del período más chico al más grande para reportar el ciclo
        #    más específico.
        for p in range(2, n // 2 + 1):
            if hist[-2 * p : -p] == hist[-p:] and len(set(hist[-p:])) > 1:
                self._trip(tool_name, f"ciclo oscilante de período {p}", 2 * p)

    def _trip(self, tool_name: str, reason: str, occurrences: int) -> None:
        """Registra el bucle, limpia el historial y lanza el cortacircuitos."""
        logger.critical(
            "Loop Detectado (%s): el agente osciló %d acciones alrededor de %s. Activando cortacircuitos cognitivo.",
            reason,
            occurrences,
            tool_name,
        )
        self._history.clear()
        raise CircuitBreakerTrippedError(
            f"Has entrado en un bucle ({reason}) intentando usar '{tool_name}'. "
            "DETENTE. Solicita asistencia humana (HITL) inmediatamente.",
            tool_name=tool_name,
            occurrences=occurrences,
        )

    def reset(self) -> None:
        """Limpia el historial. Úsalo cuando un humano aprueba retomar tras HITL."""
        self._history.clear()

    def snapshot(self) -> tuple[str, ...]:
        """Devuelve una copia inmutable del historial actual (útil para tests)."""
        return tuple(self._history)

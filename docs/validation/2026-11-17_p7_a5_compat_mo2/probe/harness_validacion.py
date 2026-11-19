"""Validación del harness de P7-A5-COMPAT.

Un resultado parcial NO es un experimento exitoso: si el proceso murió, no dejó
su JSON o devolvió algo distinto de lo esperado, la medición falla. Este módulo
existe separado de las sondas para que los tests del repo lo importen sin PyQt6
ni sky_claw.

Lo que NO hace: convertir en fallo los bloqueos esperados. Un
``ERROR_SHARING_VIOLATION`` o un ``AccessError`` de QSettings son RESULTADOS
VÁLIDOS cuando el escenario dice que debe ocurrir; se validan como valor
esperado, no como error de proceso.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any


class HarnessError(RuntimeError):
    """La sonda no puede afirmar que midió lo que dice medir."""


def leer_json(path: str | pathlib.Path, etiqueta: str) -> dict[str, Any]:
    """Lee el JSON de un escenario o falla si no existe, no parsea o no es objeto."""
    ruta = pathlib.Path(path)
    if not ruta.exists():
        raise HarnessError(f"{etiqueta}: no se produjo el JSON esperado ({ruta.name})")
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"{etiqueta}: JSON ilegible ({ruta.name}): {exc}") from exc
    if not isinstance(datos, dict):
        raise HarnessError(f"{etiqueta}: el JSON no es un objeto ({ruta.name})")
    return datos


def exigir_codigo(proc: Any, etiqueta: str, detalle: str = "") -> None:
    """Falla cerrado si el proceso no terminó con 0."""
    codigo = getattr(proc, "returncode", None)
    if codigo != 0:
        extra = f": {detalle}" if detalle else ""
        raise HarnessError(f"{etiqueta}: el proceso terminó con rc={codigo}{extra}")


def exigir_resultado(nombre: str, condicion: bool, detalle: object = "") -> None:
    """Falla si el escenario no dio el valor que su contrato declara."""
    if not condicion:
        raise HarnessError(f"escenario {nombre}: resultado inesperado {detalle!r}")


def exigir_bloqueo(salida: str, etiqueta: str) -> str:
    """Exige ``BLOQUEADO:<código>`` y devuelve el código.

    Distingue un bloqueo medido de una salida vacía, truncada o de un error del
    propio agresor (que antes se colaba como si fuera un resultado).
    """
    texto = (salida or "").strip()
    if not texto.startswith("BLOQUEADO:"):
        raise HarnessError(f"{etiqueta}: se esperaba un bloqueo medido y llegó {texto!r}")
    codigo = texto.split(":", 1)[1].strip()
    if not codigo.isdigit():
        raise HarnessError(f"{etiqueta}: bloqueo sin código Win32 ({texto!r})")
    return codigo


def exigir_marca(salida: str, etiqueta: str, esperada: str) -> None:
    """Exige una marca de éxito exacta (p. ej. ``EXITO`` o ``ESCRIBIO``)."""
    if (salida or "").strip() != esperada:
        raise HarnessError(f"{etiqueta}: se esperaba {esperada!r} y llegó {(salida or '').strip()!r}")

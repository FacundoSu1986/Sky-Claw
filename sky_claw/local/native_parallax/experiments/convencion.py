"""Lectura de la convención del canal G de una normal MIRANDO SOLO sus píxeles (NP-X0a, EXP-005 mecánico).

Una normal DirectX (G hacia abajo) se integra con ``escala_y = +1``; una OpenGL, con ``escala_y = -1``. Si se la
integra con la convención equivocada, el campo de gradientes deja de ser un gradiente y el residuo de integrabilidad
(``metrics.residuo_de_integrabilidad``) lo delata. Esta lectura mide el residuo bajo las DOS hipótesis.

**Es un indicio, no una detección.** Para una superficie integrable, el residuo con la convención equivocada es
``2m / (a + 2m + b)``, con ``m = media|h_xy|`` y ``a``, ``b`` las medias de ``|h_xx|`` y ``|h_yy|``: depende de cuánta
derivada MIXTA tiene la superficie. Si no tiene (planos, un solo eje, separables) las dos hipótesis dan lo mismo y
no hay nada que leer; con ruido o con cuantización de dos canales, el margen cae al nivel de esos casos
(``tests/test_native_parallax_x0_convencion.py`` mide dónde). Por eso este módulo no corrige nada y ``elegida`` puede
ser ``None``: la convención la fija la fuente del archivo, y una lectura ambigua es motivo de revisión.

Dos cosas que NINGUNA métrica puede resolver y que el mismo archivo de tests demuestra: la inversión global (X e Y a
la vez) niega la altura exactamente, y una escala uniforme equivocada no cambia la altura normalizada.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.local.native_parallax.integrate import Bordes, EntradaInvalidaError
from sky_claw.local.native_parallax.metrics import residuo_de_integrabilidad

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = ["LecturaDeConvencion", "leer_convencion"]


@dataclass(frozen=True, slots=True)
class LecturaDeConvencion:
    """Residuo de integrabilidad de un archivo leído como DirectX (``escala_y = +1``) y como OpenGL (``-1``)."""

    residuo_directx: float
    residuo_opengl: float

    def __post_init__(self) -> None:
        for nombre in ("residuo_directx", "residuo_opengl"):
            valor = getattr(self, nombre)
            if not math.isfinite(valor) or valor < 0.0:
                raise EntradaInvalidaError(f"{nombre} debe ser un real finito >= 0, no {valor!r}")

    @property
    def margen_relativo(self) -> float:
        """``|r_directx - r_opengl| / (r_directx + r_opengl)`` en ``[0, 1]``: 0 = empate, 1 = una lectura perfecta."""
        total = self.residuo_directx + self.residuo_opengl
        return 0.0 if total == 0.0 else abs(self.residuo_directx - self.residuo_opengl) / total

    @property
    def elegida(self) -> str | None:
        """``"directx"`` u ``"opengl"`` según el menor residuo; ``None`` si empatan (no hay nada que leer)."""
        if self.residuo_directx < self.residuo_opengl:
            return "directx"
        if self.residuo_opengl < self.residuo_directx:
            return "opengl"
        return None


def leer_convencion(normal: NDArray[np.float64], *, bordes: Bordes) -> LecturaDeConvencion:
    """Lee ``normal`` bajo las dos hipótesis de la convención del canal G (la validación es la del residuo)."""
    return LecturaDeConvencion(
        residuo_directx=residuo_de_integrabilidad(normal, bordes=bordes, escala_y=1.0),
        residuo_opengl=residuo_de_integrabilidad(normal, bordes=bordes, escala_y=-1.0),
    )

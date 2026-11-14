"""Identidad del bundle del bridge que trae ESTA versión de Sky-Claw (H-5).

Una sola fuente para las dos puntas del contrato: el instalador la registra al copiar y el broker la exige al conectar.
Repartirla garantizaba que cada punta calculara «el bundle del producto» a su manera.
"""

from __future__ import annotations

import functools
import pathlib

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.protocol import bundle_digest

RUTA_DEL_BUNDLE = pathlib.Path(__file__).parent / "plugin_bundle" / "skyclaw_bridge"


@functools.cache
def digest_del_bundle_del_producto() -> str:
    """SHA-256 del bundle empaquetado con este proceso (se calcula una vez: el bundle no cambia en vida del proceso)."""

    return bundle_digest(RUTA_DEL_BUNDLE)

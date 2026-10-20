"""Punto único de importación perezosa de ``numpy`` para los módulos de producto.

``numpy`` pertenece al extra opcional ``native-parallax`` y no al runtime base (ADR 0014, decisión 3), así que
ningún módulo lo importa al cargarse. Los que solo lo necesitan a ÉL (el corpus sintético, el integrador) lo
piden por acá; ``decode`` necesita además Pillow y verifica las dos dependencias por su cuenta. Quien importa
``numpy`` dentro de una función por fuera de estos dos puntos rompe el ancla de
``tests/test_native_parallax_producto_invariant.py``.
"""

from __future__ import annotations

from types import ModuleType

from sky_claw.local.native_parallax.decode import DependenciaNoDisponibleError

__all__ = ["importar_numpy"]

_MENSAJE_EXTRA = (
    "El generador nativo de parallax necesita numpy, que pertenece al extra opcional: instalá "
    "'sky-claw[native-parallax]' (o pip install --require-hashes -r requirements-native-parallax.lock)."
)


def importar_numpy() -> ModuleType:
    """Devuelve ``numpy`` o lanza ``DependenciaNoDisponibleError`` con la receta de instalación."""
    try:
        import numpy
    except ImportError as exc:
        raise DependenciaNoDisponibleError(_MENSAJE_EXTRA) from exc
    return numpy

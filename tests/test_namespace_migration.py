"""Contratos del corte limpio hacia el namespace ``sky_claw.app``."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sky_claw


def test_namespace_app_reemplaza_fisicamente_al_namespace_anterior() -> None:
    """El paquete nuevo existe y la ruta retirada ya no es importable ni física."""
    package_root = Path(sky_claw.__file__).parent

    assert importlib.util.find_spec("sky_claw.app") is not None
    assert importlib.util.find_spec("sky_claw.antigravity") is None
    assert not (package_root / "antigravity").exists()

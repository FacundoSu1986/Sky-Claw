"""H-01: la GUI NO debe servir el árbol source del paquete gui/ sobre HTTP.

Regresión: ``app.add_static_files("/static", _CSS_PATH.parent)`` exponía todo el
directorio del paquete (``sky_claw_gui.py``, ``_bootloader.py``,
``controllers/…``) sin filtro de extensión y nunca se referenciaba desde el
cliente (el CSS se inyecta inline vía ``_load_css`` y los fonts van por
``/assets``). Este test falla si alguien reintroduce esa ruta.
"""

from __future__ import annotations

import pathlib


def test_setup_app_no_sirve_el_paquete_gui_por_static() -> None:
    src = pathlib.Path("sky_claw/antigravity/gui/sky_claw_gui.py").read_text(encoding="utf-8")
    # No debe registrarse una ruta /static apuntando al directorio del paquete gui/.
    assert 'add_static_files("/static"' not in src, (
        "H-01: no reintroducir add_static_files('/static', ...) — expone el árbol source de gui/."
    )


def test_assets_route_sigue_presente() -> None:
    """/assets (fonts offline) debe seguir sirviéndose."""
    src = pathlib.Path("sky_claw/antigravity/gui/sky_claw_gui.py").read_text(encoding="utf-8")
    assert 'add_static_files("/assets"' in src

"""Tests del cableado del header del Forge (fase GUI estática → funcional).

Cubre los *seams* puros de:
- A1 buscador: filtro de mods reutilizable por el input del header y la lista.
- A3 identidad: iniciales derivadas + HTML de identidad data-driven (sin literales
  hardcodeados "Dovahkiin"/"Maestro de la Forja" en la vista).
- AppState: campos de estado que respaldan búsqueda e identidad.
"""

from __future__ import annotations

from pathlib import Path

from sky_claw.antigravity.gui.models.app_state import AppState
from sky_claw.antigravity.gui.views.forge_dashboard import _identity_html, _initials
from sky_claw.antigravity.gui.views.mod_list import _filter_mods


# ── A3: iniciales derivadas del nombre ──────────────────────────────────────────
def test_initials_una_palabra_toma_dos_letras() -> None:
    assert _initials("Dovahkiin") == "DO"


def test_initials_dos_palabras_toma_iniciales() -> None:
    assert _initials("Jon Snow") == "JS"


def test_initials_ignora_espacios_extra_y_normaliza_mayusculas() -> None:
    assert _initials("  ada   lovelace  ") == "AL"


def test_initials_vacio_cae_a_placeholder() -> None:
    assert _initials("") == "?"
    assert _initials("   ") == "?"


# ── A3: HTML de identidad data-driven ───────────────────────────────────────────
def test_identity_html_refleja_nombre_rol_e_iniciales() -> None:
    html = _identity_html("Ada Lovelace", "Forjadora")
    assert "Ada Lovelace" in html
    assert "Forjadora" in html
    # Iniciales calculadas, no el "DS" hardcodeado anterior.
    assert ">AL<" in html


def test_identity_html_escapa_contenido() -> None:
    html = _identity_html("<script>x</script>", "Rol & Cía")
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


# ── A1: filtro de mods reutilizable ─────────────────────────────────────────────
_MODS = [
    {"name": "Immersive Armors", "enabled": True},
    {"name": "Lux Via", "enabled": True},
    {"name": "Ordinator", "enabled": False},
]


def test_filter_mods_termino_vacio_devuelve_todo() -> None:
    assert _filter_mods(_MODS, "") == _MODS
    assert _filter_mods(_MODS, "   ") == _MODS


def test_filter_mods_es_case_insensitive_y_parcial() -> None:
    out = _filter_mods(_MODS, "or")
    names = {m["name"] for m in out}
    assert names == {"Ordinator", "Immersive Armors"}


def test_filter_mods_sin_coincidencias_devuelve_vacio() -> None:
    assert _filter_mods(_MODS, "zzz") == []


# ── AppState: campos de respaldo ────────────────────────────────────────────────
def test_appstate_tiene_search_query_por_defecto_vacio() -> None:
    state = AppState(config_path=Path("config.json"))
    assert state.search_query == ""


def test_appstate_identidad_por_defecto() -> None:
    state = AppState(config_path=Path("config.json"))
    assert state.user_display_name
    assert state.user_role

"""Anclas de contrato del tema «Forja del Dovahkiin» (quick wins de la auditoría).

Cada aserción protege UN fix de diseño que ya ocurrió una vez y rompía en
silencio (ningún gate lo veía): textos ilegibles, scrollbar nativa gris, emojis
que se renderizan con la pila de color del SO, un h1 que pedía un peso que la
fuente declaraba no tener. La convención del repo para este tipo de regla es el
test ancla textual (ver tests/test_pyinstaller.py): si un refactor futuro pisa
cualquiera de estas cuerdas, el test se rompe en CI y no en la cara del usuario.
"""

from __future__ import annotations

from pathlib import Path

_GUI_DIR = Path(__file__).resolve().parent.parent / "sky_claw" / "app" / "gui"
_STYLES = (_GUI_DIR / "styles.css").read_text(encoding="utf-8")
_FONTS = (_GUI_DIR / "assets" / "fonts" / "fonts.css").read_text(encoding="utf-8")
_FORGE = (_GUI_DIR / "views" / "forge_dashboard.py").read_text(encoding="utf-8")


def test_sc_scroll_tiene_scrollbar_tallada() -> None:
    """El shell Forge usa ``sc-scroll`` en sus tres áreas con scroll; sin regla CSS
    caían al scrollbar nativo (gris claro) y rompían la estética de piedra."""
    assert ".sc-scroll::-webkit-scrollbar-thumb" in _STYLES


def test_cinzel_declara_rango_hasta_900() -> None:
    """El h1 del hero pide ``font-weight:900``; la variable font de Cinzel lo
    cubre (400–900), pero declarar 400–700 lo clampaba a 700."""
    assert "font-weight: 400 900;" in _FONTS


def test_tema_respeta_focus_visible_y_reduced_motion() -> None:
    """A11y mínima del tema: foco de teclado visible (los botones del Forge son
    <button> inline sin foco) y apagado de aurora/brasas/shimmer con
    prefers-reduced-motion."""
    assert ":focus-visible" in _STYLES
    assert "prefers-reduced-motion" in _STYLES


def test_shell_forge_sin_emojis_de_ui() -> None:
    """Los emojis (🛡️ HITL, ⚔ disputas, 🔒/🔓 Modo local) se reemplazaron por los
    SVG de ``icons.py`` (stroke actualColor): el emoji toma la pila de color del
    SO y rompe la ilusión diegética. Este ancla bloquea su reingreso al shell;
    no aplica a Telegram/logs, donde el emoji es nativo del canal."""
    for emoji in ("🛡", "⚔", "🔒", "🔓"):
        assert emoji not in _FORGE, f"emoji {emoji!r} reingresó al shell Forge"
    # Y los reemplazos existen de verdad en el registro de iconos.
    icons = (_GUI_DIR / "icons.py").read_text(encoding="utf-8")
    assert '_ICON_SHIELD_CHECK = """<svg' in icons
    assert '_ICON_SWORDS = """<svg' in icons
    assert '_ICON_LOCK = """<svg' in icons
    assert '_ICON_UNLOCK = """<svg' in icons

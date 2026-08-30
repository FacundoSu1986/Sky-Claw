"""Anclas de contrato del tema «Forja del Dovahkiin» (quick wins de la auditoría).

Cada aserción protege UN fix de diseño que ya ocurrió una vez y rompía en
silencio (ningún gate lo veía): textos ilegibles, scrollbar nativa gris, emojis
que se renderizan con la pila de color del SO, un h1 que pedía un peso que la
fuente declaraba no tener, y una atenuación de estado que se llevaba puesto el
contraste del texto. La convención del repo para este tipo de regla es el test
ancla que ENUMERA la familia completa (ver AGENTS.md y test_pyinstaller.py):
si un refactor futuro pisa cualquiera de estas cuerdas, el test se rompe en CI
y no en la cara del usuario.
"""

from __future__ import annotations

import ast
from pathlib import Path

_GUI_DIR = Path(__file__).resolve().parent.parent / "sky_claw" / "app" / "gui"
_STYLES = (_GUI_DIR / "styles.css").read_text(encoding="utf-8")
_FONTS = (_GUI_DIR / "assets" / "fonts" / "fonts.css").read_text(encoding="utf-8")
_FORGE = (_GUI_DIR / "views" / "forge_dashboard.py").read_text(encoding="utf-8")


def _funciones_que_usan_sc_scroll() -> set[str]:
    """Devuelve los nombres de las funciones del shell que aplican ``sc-scroll``.

    Búsqueda por AST sobre ``.classes("sc-scroll")``: si alguien quita la clase
    de un contenedor, el conjunto cambia y el ancla lo detecta — verificar solo
    que la regla CSS EXISTE no protege sus consumidores (revisión Codex #522).
    """
    usos: set[str] = set()

    class _Buscador(ast.NodeVisitor):
        def __init__(self) -> None:
            self._pila: list[str] = []

        def _con_pila(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self._pila.append(node.name)
            self.generic_visit(node)
            self._pila.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._con_pila(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._con_pila(node)

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "classes"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and "sc-scroll" in node.args[0].value
            ):
                usos.add(self._pila[-1] if self._pila else "<módulo>")
            self.generic_visit(node)

    _Buscador().visit(ast.parse(_FORGE))
    return usos


def test_sc_scroll_tiene_scrollbar_tallada_y_fallback_estandar() -> None:
    """Scrollbar del shell: receta webkit tallada + propiedades estándar
    (``scrollbar-width``/``scrollbar-color``) para Firefox — el shell puede
    abrirse en un navegador externo además del webview Chromium de NiceGUI."""
    assert ".sc-scroll::-webkit-scrollbar-thumb" in _STYLES
    inicio = _STYLES.index(".sc-scroll {")
    bloque_base = _STYLES[inicio : _STYLES.index("}", inicio)]
    assert "scrollbar-width: thin" in bloque_base
    assert "scrollbar-color" in bloque_base


def test_sc_scroll_enumera_todos_sus_consumidores() -> None:
    """Los 3 contenedores scrolleables del shell (contenido principal, Orden de
    Carga, chat) deben seguir usando la clase: quien quite una, vuelve a la
    scrollbar nativa gris. Enumeración exhaustiva, no muestreo."""
    assert _funciones_que_usan_sc_scroll() == {"render_forge_dashboard", "_orden_carga", "_asistente"}


def test_cinzel_ambas_caras_declaran_hasta_900() -> None:
    """El h1 del hero pide ``font-weight:900``; la variable font de Cinzel lo
    cubre (400–900), pero declarar 400–700 lo clampaba a 700. Hay exactamente
    DOS @font-face de Cinzel (latin / latin-ext) y ambas deben cubrir hasta 900
    — verificar solo una ocurrencia dejaría la otra cara fuera de control."""
    caras = [bloque for bloque in _FONTS.split("@font-face") if "font-family: 'Cinzel'" in bloque]
    assert len(caras) == 2
    assert all("font-weight: 400 900;" in cara for cara in caras)


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


def test_rituales_atenuan_decoracion_nunca_el_texto() -> None:
    """Las tarjetas de los estados no-listos (``missing``/``unknown``) solo
    atenúan lo decorativo — medallón rúnico y borde vía ``deco_opacity`` /
    ``card_border``. Un ``opacity`` global en la tarjeta tambaleaba también el
    texto: #8a8270 con opacity .62 componía ~2.5:1 contra el fondo, por debajo
    de WCAG AA (revisión Codex #522).

    Cobertura de los cuatro estados: available / present_unverified / missing /
    unknown.
    """
    from sky_claw.app.gui.views.forge_dashboard import _RITUAL_STATE_STYLE

    assert set(_RITUAL_STATE_STYLE) == {"available", "present_unverified", "missing", "unknown"}
    for estado, estilo in _RITUAL_STATE_STYLE.items():
        assert "opacity" not in estilo, f"{estado}: opacity global en la tarjeta (ataca al texto)"
        assert float(estilo["deco_opacity"]) <= 1.0
        assert estilo["card_border"].startswith("rgba(200,168,106")

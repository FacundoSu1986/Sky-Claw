"""Pure-helper tests for the Forge dashboard's real-data rendering.

Phase 1 replaces hardcoded vitals ("CPU 3%", "GPU 18%", "Memoria 41%") and
hardcoded ritual states ("No instalado") with values derived from live
telemetry and a real :class:`EnvironmentScanner` snapshot. These helpers are
the pure seam between that data and the inline-styled HTML.
"""

from __future__ import annotations

from pathlib import Path

from sky_claw.app.gui.controllers.ritual_runner import (
    RITUAL_INSTALLER_MAP,
    RITUAL_TOOL_MAP,
)
from sky_claw.app.gui.state import get_store, reset_store_for_tests
from sky_claw.app.gui.views.forge_dashboard import (
    _RITUALS,
    STORE_KEY_CPU,
    STORE_KEY_GPU,
    STORE_KEY_RAM,
    _fmt_pct,
    _hud_html,
    _ritual_action,
    _ritual_status,
    _vital_bar_width,
    _vitals_html,
)
from sky_claw.local.discovery import scanner as scanner_mod
from sky_claw.local.discovery.environment import (
    EnvironmentSnapshot,
    ToolInfo,
)


def _scanner_tool_keys() -> set[str]:
    """Claves de tool que el scanner publica, leídas del propio `tool_defs`.

    `_scan_inner` construye `tool_defs` como literal local, así que se extrae del
    AST en vez de hardcodear la lista: escribirla a mano es lo que dejó este test
    sin cubrir `skse` durante toda una release.
    """
    import ast
    import inspect
    import textwrap

    fuente = textwrap.dedent(inspect.getsource(scanner_mod.EnvironmentScanner._scan_inner))
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "tool_defs" for t in nodo.targets):
            return {
                elt.elts[0].value
                for elt in nodo.value.elts  # type: ignore[attr-defined]
                if isinstance(elt, ast.Tuple) and isinstance(elt.elts[0], ast.Constant)
            }
    raise AssertionError("No encontré `tool_defs` en EnvironmentScanner._scan_inner")


# ── Metric formatting ──────────────────────────────────────────────────────────
def test_fmt_pct_none_is_nd() -> None:
    assert _fmt_pct(None) == "N/D"


def test_fmt_pct_rounds_to_int_percent() -> None:
    assert _fmt_pct(41.4) == "41%"
    assert _fmt_pct(2.6) == "3%"
    assert _fmt_pct(0) == "0%"


def test_vital_bar_width_clamped_and_zero_for_unknown() -> None:
    assert _vital_bar_width(None) == 0
    assert _vital_bar_width(150) == 100
    assert _vital_bar_width(-5) == 0
    assert _vital_bar_width(41.6) == 42


# ── Ritual availability from the environment snapshot ───────────────────────────
def _snapshot_with(tool_key: str) -> EnvironmentSnapshot:
    snap = EnvironmentSnapshot()
    snap.tools[tool_key] = ToolInfo(name=tool_key.upper(), exe_path=Path("/x") / f"{tool_key}.exe")
    return snap


def test_ritual_status_unknown_when_no_snapshot() -> None:
    assert _ritual_status(None, "loot") == "unknown"


def test_ritual_status_available_when_tool_detected() -> None:
    assert _ritual_status(_snapshot_with("loot"), "loot") == "available"


def test_ritual_status_missing_when_tool_absent() -> None:
    assert _ritual_status(EnvironmentSnapshot(), "loot") == "missing"


def test_every_ritual_maps_to_a_scanner_tool_key() -> None:
    # Las claves salen del scanner por introspección, no de una lista escrita a mano:
    # la lista literal que había acá se quedó sin `skse` cuando el scanner lo sumó a
    # `tool_defs`, y el test siguió pasando en verde sin cubrirlo.
    for ritual in _RITUALS:
        assert ritual["tool"] in _scanner_tool_keys()


def test_todo_lo_instalable_tiene_tarjeta_en_la_grilla() -> None:
    """Un instalador cableado sin tarjeta es código muerto: nadie puede apretarlo.

    `_ritual_card` solo ofrece "Instalar" para `r["tool"] in RITUAL_INSTALLER_MAP`,
    pero la grilla itera `_RITUALS`. Agregar una clave al mapa sin su tarjeta deja
    el instalador inalcanzable desde producción y solo los tests lo ejercitan —que
    es exactamente lo que pasó con `skse`.
    """
    con_tarjeta = {r["tool"] for r in _RITUALS}
    huerfanos = set(RITUAL_INSTALLER_MAP) - con_tarjeta
    assert not huerfanos, f"instaladores sin tarjeta en la GUI: {sorted(huerfanos)}"


def test_ninguna_tarjeta_promete_un_boton_que_no_existe() -> None:
    """La dirección inversa: una tarjeta sin dispatcher NI instalador no hace nada."""
    for ritual in _RITUALS:
        tool = ritual["tool"]
        assert tool in RITUAL_TOOL_MAP or tool in RITUAL_INSTALLER_MAP, (
            f"la tarjeta «{tool}» no tiene ni ejecución ni instalación"
        )


# ── Acción del botón de cada tarjeta ────────────────────────────────────────────
def test_tool_ejecutable_disponible_ofrece_correr() -> None:
    assert _ritual_action("loot", "available") == "run"


def test_tool_instalable_faltante_ofrece_instalar() -> None:
    assert _ritual_action("skse", "missing") == "install"
    assert _ritual_action("loot", "missing") == "install"


def test_skse_instalado_no_ofrece_ejecutar_sino_que_informa() -> None:
    """SKSE es un runtime que carga el juego, no una tool que Sky-Claw ejecute.

    Con el estado genérico, una tarjeta `available` cablea el botón a
    `on_ritual_run`, que resuelve el dispatcher por `RITUAL_TOOL_MAP` — donde
    `skse` no está ni puede estar. El botón prometía "Ejecutar" y no tenía a
    dónde ir.

    El desenlace es `installed` y NO `none`: son dos situaciones distintas que
    `_ritual_card` tiene que poder separar (ver el test de abajo).
    """
    assert _ritual_action("skse", "available") == "installed"


def test_instalado_y_sin_accion_no_son_el_mismo_desenlace() -> None:
    """Señalado por la revisión adversarial de Qodo sobre este mismo PR.

    Colapsar ambos en `none` hacía que el botón "Instalado" cayera en la rama del
    aviso interino de `_ritual_card` y notificara "disponible en la próxima
    iteración" sobre un componente que YA está instalado. El label decía la verdad
    y el handler la contradecía.
    """
    instalado = _ritual_action("skse", "available")
    sin_accion = _ritual_action("wrye_bash", "missing")

    assert instalado != sin_accion, "la tarjeta no puede tratar «ya instalado» como «no hay nada que hacer»"
    assert instalado == "installed"
    assert sin_accion == "none"


def test_tool_sin_instalador_y_faltante_no_ofrece_instalar() -> None:
    """Wrye Bash / DynDOLOD no tienen auto-instalador: mantienen el aviso interino."""
    assert _ritual_action("wrye_bash", "missing") == "none"
    assert _ritual_action("dyndolod", "missing") == "none"


def test_estado_desconocido_no_ofrece_nada() -> None:
    """Antes de que aterrice el primer scan la UI no puede afirmar nada."""
    for tool in ("loot", "skse", "wrye_bash"):
        assert _ritual_action(tool, "unknown") == "none"


# ── Live vitals/HUD HTML builders (pure seam for the @ui.refreshable timer) ──────
def test_vitals_html_reflects_store_values() -> None:
    # The Vitalidad bars are redrawn ~every LIVE_REFRESH_SECONDS from these store
    # keys; the builder must echo the live percentages so the bars actually pulse.
    reset_store_for_tests()
    store = get_store()
    store.set(STORE_KEY_CPU, 41)
    store.set(STORE_KEY_GPU, 18)
    store.set(STORE_KEY_RAM, 72)
    html = _vitals_html()
    assert "41%" in html
    assert "18%" in html
    assert "72%" in html


def test_vitals_html_paints_nd_for_unknown_metrics() -> None:
    # GPU stays None on a box with no NVIDIA GPU/pynvml; the bars must read "N/D"
    # instead of a fabricated number.
    reset_store_for_tests()
    store = get_store()
    store.set(STORE_KEY_CPU, None)
    store.set(STORE_KEY_GPU, None)
    store.set(STORE_KEY_RAM, None)
    html = _vitals_html()
    # Three vitals rows (Procesador / Gráficos / Memoria) → three "N/D".
    assert html.count("N/D") == 3


def test_hud_html_reflects_store_values() -> None:
    reset_store_for_tests()
    store = get_store()
    store.set(STORE_KEY_GPU, 55)
    store.set(STORE_KEY_CPU, 12)
    html = _hud_html()
    assert "GPU" in html
    assert "55%" in html
    assert "CPU" in html
    assert "12%" in html


def test_hud_html_paints_nd_for_unknown_metrics() -> None:
    reset_store_for_tests()
    store = get_store()
    store.set(STORE_KEY_GPU, None)
    store.set(STORE_KEY_CPU, None)
    html = _hud_html()
    # GPU + CPU both unknown → two "N/D".
    assert html.count("N/D") == 2


def test_ritual_card_distingue_todos_los_desenlaces_del_helper() -> None:
    """Cada valor que `_ritual_action` puede devolver tiene que tener rama propia.

    El defecto que motiva este ancla (revisión adversarial de #429) no fue un valor
    mal calculado sino un desenlace que el consumidor no distinguía: `installed`
    caía en el `else` del aviso interino y le decía "disponible en la próxima
    iteración" a un componente ya instalado. Agregar un desenlace al helper sin
    cablearlo en `_ritual_card` tiene que romper acá, no en la cara del usuario.

    `none` es la única excepción legítima: es justamente lo que atiende el `else`.
    """
    import ast
    import inspect
    import textwrap

    from sky_claw.app.gui.views import forge_dashboard as fd

    def _constantes_de_retorno(fn: object) -> set[str]:
        arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))  # type: ignore[arg-type]
        valores: set[str] = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Return) and nodo.value is not None:
                valores |= {
                    c.value for c in ast.walk(nodo.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)
                }
        return valores

    def _cableados_en_los_handlers(fn: object) -> set[str]:
        """Desenlaces con rama propia **en la cadena que cablea el click**.

        Mirar cualquier `action == "..."` del cuerpo no alcanza: `_ritual_card` también
        compara contra `action` para elegir el label, así que un handler borrado seguía
        "manejado" a ojos del test. Verificado por mutación: con la rama del handler
        removida, esta versión falla y la otra pasaba.
        """
        arbol = ast.parse(textwrap.dedent(inspect.getsource(fn)))  # type: ignore[arg-type]
        valores: set[str] = set()
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.If):
                continue
            cablea_click = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "on"
                for rama in nodo.body
                for c in ast.walk(rama)
            )
            if not cablea_click:
                continue
            for comparacion in ast.walk(nodo.test):
                if (
                    isinstance(comparacion, ast.Compare)
                    and isinstance(comparacion.left, ast.Name)
                    and comparacion.left.id == "action"
                ):
                    valores |= {
                        c.value
                        for c in comparacion.comparators
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)
                    }
        return valores

    desenlaces = _constantes_de_retorno(fd._ritual_action)
    manejados = _cableados_en_los_handlers(fd._ritual_card)

    assert desenlaces, "no se pudo leer los desenlaces de _ritual_action"
    sin_rama = desenlaces - manejados - {"none"}
    assert not sin_rama, f"desenlaces sin rama propia en _ritual_card: {sorted(sin_rama)}"

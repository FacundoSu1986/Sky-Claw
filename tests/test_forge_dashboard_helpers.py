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


def test_skse_instalado_no_ofrece_ejecutar() -> None:
    """SKSE es un runtime que carga el juego, no una tool que Sky-Claw ejecute.

    Con el estado genérico, una tarjeta `available` cablea el botón a
    `on_ritual_run`, que resuelve el dispatcher por `RITUAL_TOOL_MAP` — donde
    `skse` no está ni puede estar. El botón prometía "Ejecutar" y no tenía a
    dónde ir.
    """
    assert _ritual_action("skse", "available") == "none"


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

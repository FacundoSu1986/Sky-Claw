"""Shared GUI utilities and constants."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

CSS_PATH = Path(__file__).parent / "styles.css"
ASSETS_PATH = Path(__file__).parent / "assets"
MAX_CHAT_MESSAGES = 500


def _load_css() -> None:
    """Wire the Nordic theme (webfonts + stylesheet + UI-sound shim) once per client.

    Idempotent on purpose: ``main_page`` is a ``@ui.refreshable`` and the wizard
    also calls this, so without a guard every refresh would inject a *duplicate*
    ``<style>``/``<link>`` and the head would grow without bound. We tag the live
    client and bail on later calls within the same page load.
    """
    try:
        client = ui.context.client
    except Exception:  # noqa: BLE001 — no client context (e.g. unit tests)
        client = None
    if client is not None:
        if getattr(client, "_skyclaw_theme_loaded", False):
            return
        client._skyclaw_theme_loaded = True

    # Recolour Quasar's brand palette to Nordic gold. NiceGUI's default
    # --q-primary is a blue (#5898d4) that wins the cascade on every
    # default-coloured control (buttons, spinners, focus rings), so fighting it
    # per-component with class CSS is whack-a-mole; recolouring the variable
    # flips them all at once. Dark gold keeps Quasar's white button text at
    # WCAG-AA contrast.
    ui.colors(primary="#8b6d23", secondary="#5d4037", accent="#ff9d00", dark="#0b0e14")

    # Bundled medieval webfonts (Cinzel body / MedievalSharp display / Noto Sans
    # Runic). Served offline from gui/assets/fonts via add_static_files("/assets"),
    # so the frozen exe renders the theme with no network call at boot.
    ui.add_head_html('<link rel="stylesheet" href="/assets/fonts/fonts.css">')

    # Define playSkyrimSound up front: stat/feature cards and CTA buttons call it
    # on hover/click, and without it every interaction threw ReferenceError in the
    # console. Silent no-op by default (no sfx asset bundled yet) but kept as a
    # single seam so a real cue can be wired later without touching the views.
    ui.add_body_html("<script>window.playSkyrimSound = window.playSkyrimSound || function (_type) {};</script>")

    if CSS_PATH.exists():
        ui.add_css(CSS_PATH.read_text(encoding="utf-8"))

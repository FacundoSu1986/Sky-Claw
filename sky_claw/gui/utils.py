"""Shared GUI utilities and constants."""

from __future__ import annotations
from pathlib import Path
from nicegui import ui

CSS_PATH = Path(__file__).parent / "styles.css"
ASSETS_PATH = Path(__file__).parent / "assets"
MAX_CHAT_MESSAGES = 500


def _load_css() -> None:
    """Load external CSS once per page."""
    if CSS_PATH.exists():
        ui.add_css(CSS_PATH.read_text(encoding="utf-8"))

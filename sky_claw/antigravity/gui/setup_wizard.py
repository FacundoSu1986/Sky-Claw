"""Setup wizard modal and legacy setup page."""

from __future__ import annotations

import json
import logging
import secrets
from typing import TYPE_CHECKING, Any

import keyring
from nicegui import ui

from .gui_helpers import _load_css
from .icons import _ICON_ROCKET, _ICON_SETTINGS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# SETUP WIZARD MODAL — Overlay sobre el Dashboard (Nordic / Parchment)
# =============================================================================


class SetupWizardModal:
    """Wizard de credenciales renderizado como modal overlay sobre el dashboard.

    - Auto-abre si las credenciales están vacías.
    - NO se cierra al hacer clic en el backdrop.
    - Autoguarda borradores no sensibles en localStorage.
    - Solo se cierra al guardar exitosamente.
    """

    VALID_PROVIDERS = {"anthropic", "deepseek", "ollama"}

    def __init__(self, config_path: Path, on_complete: Callable) -> None:
        self._config_path = config_path
        self._on_complete = on_complete
        self._overlay_el: ui.element | None = None
        self._step = 1
        self._step_label: ui.label | None = None
        self._step1_container: ui.element | None = None
        self._step2_container: ui.element | None = None
        # Input references
        self._api_key_input: ui.input | None = None
        self._telegram_id_input: ui.input | None = None
        self._frequency_input: ui.input | None = None
        self._provider_toggle = None
        self._nexus_input: ui.input | None = None
        self._telegram_token_input: ui.input | None = None
        # Draft fields (non-sensitive) for localStorage
        self._draft_fields: dict[str, ui.input] = {}

    def build(self) -> None:
        """Renderiza el overlay fijo sobre el dashboard."""
        # Overlay backdrop — NO cierra al hacer clic
        self._overlay_el = ui.element("div").classes("sky-wizard-overlay")

        with self._overlay_el:
            # Modal container — stopPropagation para evitar propagación de clics
            modal = ui.element("div").classes("sky-wizard-modal")
            modal.on("click", lambda e: None, [])  # absorb clicks

            with modal:
                # Header
                with ui.row().classes("items-center justify-between w-full mb-4"):
                    with ui.row().classes("items-center gap-3"):
                        ui.html(f"""
                            <div style="width:36px;height:36px;border-radius:8px;display:flex;
                                        align-items:center;justify-content:center;
                                        background:linear-gradient(135deg, #C8A84E, #8B7332);">
                                {_ICON_SETTINGS}
                            </div>
                        """)
                        ui.label("ASISTENTE DE CONFIGURACIÓN").classes("sky-wizard-title")
                    self._step_label = ui.label("Paso 1 de 2").classes("sky-wizard-step")

                # Progress bar
                with (
                    ui.element("div")
                    .classes("w-full mb-4")
                    .style("height:4px; background:rgba(255,255,255,0.08); border-radius:2px; overflow:hidden;")
                ):
                    self._progress_bar = ui.element("div").style(
                        "width:50%; height:100%; background:var(--sky-gold); "
                        "border-radius:2px; transition:width 0.3s ease;"
                    )

                # Description
                ui.label(
                    "Parece que sus credenciales están vacías. Complete los siguientes campos "
                    "para inicializar el sistema Sky-Claw. Los datos se guardan localmente como borradores."
                ).classes("sky-wizard-description mb-5")

                # ── Step 1 ──
                self._step1_container = ui.column().classes("w-full gap-4")
                with self._step1_container:
                    # API Key
                    with ui.column().classes("w-full gap-1"):
                        ui.label("CLAVE API DE OPERACIONES").classes("sky-wizard-label")
                        self._api_key_input = (
                            ui.input(
                                placeholder="sk-... o clave del proveedor",
                            )
                            .classes("w-full")
                            .props(
                                'type=password dark standout="bg-transparent" '
                                'input-class="sky-wizard-input" color=amber maxlength=512'
                            )
                        )
                        ui.label("Usa tu API Key de producción").classes("sky-wizard-hint")

                    # Telegram ID
                    with ui.column().classes("w-full gap-1"):
                        ui.label("ID DE TELEGRAM").classes("sky-wizard-label")
                        self._telegram_id_input = (
                            ui.input(
                                placeholder="@usuario_id",
                            )
                            .classes("w-full")
                            .props(
                                'dark standout="bg-transparent" input-class="sky-wizard-input" color=amber maxlength=32'
                            )
                        )
                        ui.label("ID único de tu cuenta de Telegram").classes("sky-wizard-hint")
                        self._draft_fields["telegram_chatid"] = self._telegram_id_input

                    # Frecuencia
                    with ui.column().classes("w-full gap-1"):
                        ui.label("FRECUENCIA (MS)").classes("sky-wizard-label")
                        self._frequency_input = (
                            ui.input(
                                placeholder="5000",
                                value="5000",
                            )
                            .classes("w-full")
                            .props(
                                'dark standout="bg-transparent" input-class="sky-wizard-input" color=amber maxlength=10'
                            )
                        )
                        ui.label("Frecuencia de monitoreos en milisegundos").classes("sky-wizard-hint")
                        self._draft_fields["frequency_ms"] = self._frequency_input

                # ── Step 2 (hidden initially) ──
                self._step2_container = ui.column().classes("w-full gap-4")
                self._step2_container.style("display: none;")
                with self._step2_container:
                    # Provider
                    with ui.column().classes("w-full gap-1"):
                        ui.label("PROVEEDOR IA").classes("sky-wizard-label")
                        self._provider_toggle = (
                            ui.toggle(
                                ["anthropic", "deepseek", "ollama"],
                                value="deepseek",
                            )
                            .classes("w-full")
                            .props("color=amber")
                        )

                    # Nexus Key
                    with ui.column().classes("w-full gap-1"):
                        ui.label("NEXUS MODS API KEY").classes("sky-wizard-label")
                        self._nexus_input = (
                            ui.input(
                                placeholder="Opcional — para descargas automáticas",
                            )
                            .classes("w-full")
                            .props(
                                'type=password dark standout="bg-transparent" '
                                'input-class="sky-wizard-input" color=amber maxlength=512'
                            )
                        )

                    # Telegram Token
                    with ui.column().classes("w-full gap-1"):
                        ui.label("TELEGRAM BOT TOKEN").classes("sky-wizard-label")
                        self._telegram_token_input = (
                            ui.input(
                                placeholder="Opcional — para notificaciones HITL",
                            )
                            .classes("w-full")
                            .props(
                                'type=password dark standout="bg-transparent" '
                                'input-class="sky-wizard-input" color=amber maxlength=512'
                            )
                        )

                # ── CTA Button ──
                with ui.row().classes("w-full justify-end gap-3 mt-4"):
                    self._back_btn = (
                        ui.button(
                            "Atrás",
                            on_click=self._go_step1,
                        )
                        .classes("px-5 py-3 rounded-xl font-semibold")
                        .props("ripple flat no-caps")
                        .style("color: var(--sky-parchment-text); display: none;")
                    )

                    self._next_btn = (
                        ui.button(
                            "Siguiente",
                            on_click=self._go_step2,
                        )
                        .classes("sky-wizard-cta px-6 py-3 rounded-xl text-lg")
                        .props("ripple no-caps")
                    )

                    self._submit_btn = (
                        ui.button(
                            on_click=self._on_submit,
                        )
                        .classes("sky-wizard-cta px-6 py-3 rounded-xl text-lg")
                        .props("ripple no-caps")
                        .style("display: none;")
                    )
                    with self._submit_btn:
                        ui.html(f'<span style="margin-right:8px;">{_ICON_ROCKET}</span>')
                        ui.label("Inicializar Sistema")

        # Attach localStorage autosave handlers
        for field_name, input_el in self._draft_fields.items():
            input_el.on(
                "update:model-value",
                lambda e, fn=field_name: self._save_draft(fn, e.args),
            )

        # Restore drafts from localStorage
        ui.timer(0.3, self._restore_drafts, once=True)

        # Prevent backdrop from propagating clicks
        ui.run_javascript("""
            document.querySelector('.sky-wizard-overlay')?.addEventListener('click', function(e) {
                e.stopPropagation();
            });
        """)

    def _go_step2(self) -> None:
        self._step = 2
        self._step_label.set_text("Paso 2 de 2")
        self._progress_bar.style(
            "width:100%; height:100%; background:var(--sky-gold); border-radius:2px; transition:width 0.3s ease;"
        )
        self._step1_container.style("display: none;")
        self._step2_container.style("display: flex;")
        self._next_btn.style("display: none;")
        self._back_btn.style("display: block; color: var(--sky-parchment-text);")
        self._submit_btn.style("display: flex;")

    def _go_step1(self) -> None:
        self._step = 1
        self._step_label.set_text("Paso 1 de 2")
        self._progress_bar.style(
            "width:50%; height:100%; background:var(--sky-gold); border-radius:2px; transition:width 0.3s ease;"
        )
        self._step1_container.style("display: flex;")
        self._step2_container.style("display: none;")
        self._next_btn.style("display: block;")
        self._back_btn.style("display: none;")
        self._submit_btn.style("display: none;")

    async def _on_submit(self) -> None:
        provider = self._provider_toggle.value if self._provider_toggle else "deepseek"
        api_key = self._api_key_input.value.strip() if self._api_key_input else ""
        nexus_key = self._nexus_input.value.strip() if self._nexus_input else ""
        telegram_token = self._telegram_token_input.value.strip() if self._telegram_token_input else ""
        telegram_chatid = self._telegram_id_input.value.strip() if self._telegram_id_input else ""

        await self._validate_and_save(
            provider=provider,
            api_key=api_key,
            nexus_key=nexus_key,
            telegram_token=telegram_token,
            telegram_chatid=telegram_chatid,
        )

    async def _validate_and_save(
        self,
        provider: str,
        api_key: str,
        nexus_key: str,
        telegram_token: str,
        telegram_chatid: str,
    ) -> None:
        if provider not in self.VALID_PROVIDERS:
            ui.notify("Proveedor no válido", type="negative")
            return

        if provider in ("anthropic", "deepseek") and not api_key:
            ui.notify("API Key requerida para este proveedor", type="negative")
            return

        if len(api_key) > 512:
            ui.notify("Máximo 512 caracteres en API Key", type="negative")
            return

        if telegram_token and ":" not in telegram_token:
            ui.notify("Token Telegram inválido — debe contener ':'", type="negative")
            return

        if telegram_chatid and not telegram_chatid.replace("@", "").replace("-", "").isdigit():
            ui.notify("Chat ID debe ser numérico", type="negative")
            return

        try:
            key_map = {
                f"{provider}_api_key": api_key,
                "nexus_api_key": nexus_key,
                "telegram_bot_token": telegram_token,
            }
            if api_key:
                key_map["llm_api_key"] = api_key

            for k, v in key_map.items():
                if v:
                    keyring.set_password("sky_claw", k, v)

            existing_ws = keyring.get_password("sky_claw", "ws_auth_token")
            if not existing_ws:
                keyring.set_password("sky_claw", "ws_auth_token", secrets.token_hex(32))

            from sky_claw.config import Config

            cfg = Config(self._config_path)
            cfg._data["llm_provider"] = provider
            cfg._data["first_run"] = False
            if telegram_chatid:
                cfg._data["telegram_chat_id"] = telegram_chatid.replace("@", "")
            cfg.save()

            # Clear localStorage drafts
            await self._clear_drafts()

            logger.info("Setup completado — provider=%s", provider)

            # Remove overlay from DOM
            if self._overlay_el:
                self._overlay_el.delete()

            await self._on_complete()

        except Exception as e:
            logger.exception("Error guardando configuración:")
            ui.notify(f"Error guardando configuración: {e}", type="negative")

    # ── localStorage Draft Autosave ──────────────────────────────────

    def _save_draft(self, field_name: str, value: Any) -> None:
        val = str(value) if value else ""
        ui.run_javascript(f'localStorage.setItem("skyclaw_draft_{field_name}", {json.dumps(val)})')

    async def _restore_drafts(self) -> None:
        for field_name, input_el in self._draft_fields.items():
            try:
                val = await ui.run_javascript(f'localStorage.getItem("skyclaw_draft_{field_name}")')
                if val:
                    input_el.value = val
            except Exception:
                pass

    async def _clear_drafts(self) -> None:
        await ui.run_javascript("""
            Object.keys(localStorage)
                .filter(k => k.startsWith("skyclaw_draft_"))
                .forEach(k => localStorage.removeItem(k))
        """)


# =============================================================================
# SETUP PAGE — Legacy wrapper (mantiene compatibilidad con __main__.py)
# =============================================================================


class SetupPage:
    """Legacy: redirige internamente al wizard modal. Mantenido para compat."""

    VALID_PROVIDERS = {"anthropic", "deepseek", "ollama"}

    def __init__(self, config_path: Path, on_complete: Callable) -> None:
        self._config_path = config_path
        self._on_complete = on_complete

    def build(self) -> None:
        _load_css()
        with (
            ui.element("div")
            .classes("w-full min-h-screen flex items-center justify-center relative")
            .style("background: var(--sky-bg-primary); font-family: var(--sky-font-family);")
        ):
            ui.html('<div class="sky-glow-overlay" style="opacity: 0.1;"></div>')
            wizard = SetupWizardModal(self._config_path, self._on_complete)
            wizard.build()

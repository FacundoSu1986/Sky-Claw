"""Setup wizard modal and legacy setup page."""

from __future__ import annotations

import itertools
import json
import logging
import secrets
import tomllib
from typing import TYPE_CHECKING, Any

import keyring
from nicegui import ui

from .gui_helpers import _FRAGMENTO, _load_css
from .icons import _ICON_ROCKET, _icon_dragon_eye
from .state import get_store

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Proveedores LLM aceptados (fuente única para wizard y panel de Ajustes).
VALID_PROVIDERS = {"anthropic", "deepseek", "openai", "ollama"}

#: Frases de la forja (D2): lore rotatorio al pie del wizard de primer arranque,
#: inspirado en las pantallas de carga de Skyrim. TEXTOS ORIGINALES — ninguno es
#: cita del juego (licencia Bethesda). Congeladas por igualdad literal en
#: tests/test_gui_theme_contracts.py: quien cambie la lista la cambia con conciencia.
_WIZARD_LORE: tuple[str, ...] = (
    "No todos los descansos son derrotas: a veces el dragón duerme para que la forja aguante.",
    "Un orden de carga bien atado vale más que diez mods brillantes mal pertrechados.",
    "LOOT ordena, xEdit confiesa, DynDOLOD revela: cada herramienta a su ritual.",
    "Que cada cambio tenga prueba y cada prueba tenga nombre — eso separa la forja del fuego.",
    "El viento de la garganta no borra las runas, si alguien las grabó de verdad.",
)


def _lore_markup(frase: str) -> str:
    """Markup de la cita de lore (función de módulo: el ancla la invoca sin
    levantar la sala NiceGUI)."""
    return (
        '<div id="sky-wizard-lore" style="text-align:center; margin-top:18px;'
        " font-family:'EB Garamond',serif; font-style:italic; font-size:13px;"
        f' color:#b6ab90; transition:opacity .45s ease;">{frase}</div>'
    )


def validate_credentials(
    provider: str,
    api_key: str,
    telegram_token: str = "",
    telegram_chatid: str = "",
    *,
    require_api_key: bool = True,
) -> str | None:
    """Valida las credenciales; devuelve el mensaje de error o ``None`` si ok.

    Seam puro compartido entre el wizard first-run (``require_api_key=True``:
    los proveedores cloud exigen clave) y el panel de Ajustes
    (``require_api_key=False``: clave vacía significa "no cambiar").
    """
    if provider not in VALID_PROVIDERS:
        return "Proveedor no válido"
    if require_api_key and provider in ("anthropic", "deepseek", "openai") and not api_key:
        return "API Key requerida para este proveedor"
    if len(api_key) > 512:
        return "Máximo 512 caracteres en API Key"
    if telegram_token and ":" not in telegram_token:
        return "Token Telegram inválido — debe contener ':'"
    if telegram_chatid and not telegram_chatid.replace("@", "").replace("-", "").isdigit():
        return "Chat ID debe ser numérico"
    return None


def _search_key_ausente() -> bool:
    """Confirma que ``search_api_key`` no está en el backend, sin volcar secretos."""
    try:
        return keyring.get_password("sky_claw", "search_api_key") is None
    except keyring.errors.NoKeyringError:
        logger.warning("Sin backend de keyring: no se puede confirmar la desactivación de search_api_key.")
    except (keyring.errors.KeyringError, OSError) as exc:
        logger.warning(
            "No se pudo leer search_api_key del keyring (%s); no se confirma la desactivación.",
            type(exc).__name__,
        )
    except Exception as exc:  # noqa: BLE001 — backend nativo: se clasifica por clase, sin mensaje
        logger.error(
            "Error inesperado del keyring al leer search_api_key (%s); no se confirma la desactivación.",
            type(exc).__name__,
        )
    return False


def _desactivar_search_key() -> bool:
    """Borra ``search_api_key`` y exige verificar la ausencia.

    ``PasswordDeleteError`` no se trata como prueba de ausencia: algunos
    backends lo usan para errores reales de borrado. Sólo una lectura posterior
    que devuelva ``None`` habilita el éxito visible del wizard.
    """
    try:
        keyring.delete_password("sky_claw", "search_api_key")
    except keyring.errors.PasswordDeleteError:
        # Puede ser ausencia legítima o un fallo del backend: lo decide la lectura.
        pass
    except keyring.errors.NoKeyringError:
        logger.warning("Sin backend de keyring: no se pudo desactivar search_api_key.")
        return False
    except (keyring.errors.KeyringError, OSError) as exc:
        logger.warning(
            "No se pudo borrar search_api_key del keyring (%s); no se confirma la desactivación.",
            type(exc).__name__,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — se registra la clase, nunca el mensaje
        logger.error(
            "Error inesperado del keyring al borrar search_api_key (%s); no se confirma la desactivación.",
            type(exc).__name__,
        )
        return False
    return _search_key_ausente()


def _config_sin_search_key(config_path: Path) -> bool:
    """Confirma que el TOML no contiene un plaintext de ``search_api_key``.

    La lectura de keyring no ve una escritura ajena al TOML. Antes de publicar
    ``first_run=False`` hay que comprobar ese archivo: ``Config.save()`` podría
    migrar el plaintext al backend justo en el guardado que cierra el wizard.
    """
    try:
        datos = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        logger.warning("No se pudo verificar el TOML antes de cerrar el wizard (%s).", type(exc).__name__)
        return False
    return not datos.get("search_api_key")


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
        self._search_input: ui.input | None = None
        self._telegram_token_input: ui.input | None = None
        # Draft fields (non-sensitive) for localStorage
        self._draft_fields: dict[str, ui.input] = {}
        # Lore rotatorio (D2) — ciclo por la frase al pie del wizard.
        self._lore_iter = itertools.cycle(_WIZARD_LORE)
        self._lore_el: ui.html | None = None
        self._lore_timer: ui.timer | None = None

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
                        # El emblema del ojo de dragón (mismo recurso que el
                        # sidebar del shell) en el medallón de la marca — antes
                        # era un engranaje genérico que no decía nada.
                        ui.html(f"""
                            <div style="width:46px;height:46px;border-radius:50%;display:flex;
                                        align-items:center;justify-content:center;flex-shrink:0;
                                        background:radial-gradient(circle at 50% 38%, #2a1d12, #0c0805);
                                        border:1.5px solid #c8a86a;
                                        box-shadow:0 0 18px rgba(200,168,106,.45), inset 0 0 12px rgba(0,0,0,.7);">
                                {_icon_dragon_eye(iris_id="scIris-wizard")}
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
                                ["anthropic", "deepseek", "openai", "ollama"],
                                value="deepseek",
                            )
                            .classes("w-full sc-toggle")
                            # Mismo seam que la Cámara de Ajustes (receta .sc-toggle):
                            # colores sin clase en Quasar para que su .bg-primary
                            # !important (capa quasar_importants) no pise el oro.
                            .props("unelevated toggle-color=sc-tema toggle-text-color=sc-tema")
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

                    # Brave Search Key (enables natural-language mod search)
                    with ui.column().classes("w-full gap-1"):
                        ui.label("BRAVE SEARCH API KEY").classes("sky-wizard-label")
                        self._search_input = (
                            ui.input(
                                placeholder="Opcional — para buscar mods por descripción",
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
                        # sc-tema también acá: con el primary por defecto el
                        # .text-primary !important de Quasar pisaba el oro inline.
                        .props("ripple flat no-caps color=sc-tema text-color=sc-tema")
                        # gold, not parchment-ink: the v4.0 wizard modal is dark,
                        # so #2c2016 would render the Back control near-invisible.
                        .style("color: var(--sky-gold); display: none;")
                    )

                    self._next_btn = (
                        ui.button(
                            "Siguiente",
                            on_click=self._go_step2,
                        )
                        .classes("sky-wizard-cta px-6 py-3 rounded-xl text-lg")
                        # color/text-color sin clase en Quasar: con el primary por
                        # defecto, .bg-primary/.text-white (!important en la capa
                        # quasar_importants de NiceGUI 3) pisaban el oro fundido de
                        # .sky-wizard-cta y el CTA salía ocre plano.
                        .props("ripple no-caps color=sc-tema text-color=sc-tema")
                    )

                    self._submit_btn = (
                        ui.button(
                            on_click=self._on_submit,
                        )
                        .classes("sky-wizard-cta px-6 py-3 rounded-xl text-lg")
                        .props("ripple no-caps color=sc-tema text-color=sc-tema")
                        .style("display: none;")
                    )
                    with self._submit_btn:
                        # _FRAGMENTO (display:contents): el wrapper <div> de ui.html
                        # dejaba el cohete fuera de la alineación flex del contenido
                        # del botón. Misma constante compartida que el shell (no un
                        # literal suelto: el ancla lo prohíbe en todo el paquete GUI).
                        ui.html(f'<span style="display:inline-flex; margin-right:8px;">{_ICON_ROCKET}</span>').style(
                            _FRAGMENTO
                        )
                        ui.label("Inicializar Sistema")

                # Lore rotatorio (D2): cita al pie del modal, estilo pantalla de
                # carga de primer inicio. Sin CSS keyframes: solo cambio de texto,
                # compatible con la política de movimiento reducido.
                self._lore_el = ui.html(_lore_markup(next(self._lore_iter)))

        # Temporizador de UI: rota la frase cada ~6s mientras el wizard vive.
        # immediate=False: con el valor por defecto (True) la primera cita se saltaría
        # al instante en vez de durar su intervalo (revisión de Codex en #584).
        self._lore_timer = ui.timer(6.0, self._rotate_lore, immediate=False)

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

    def _rotate_lore(self) -> None:
        """Rota la cita de lore; cuando el modal ya cerró, apaga el timer.

        Contrato explícito contra NiceGUI 3.12: cuando el overlay se borra,
        ``element.content = ...`` sobre un elemento marcado es un NO-OP
        silencioso (``update()`` retorna temprano), NO lanza
        ``RuntimeError`` — un `try/except` era código defensivo muerto.
        Por eso el vida-muerte del timer se decide por ``is_deleted``.
        Patrón: fuente de verdad única = estado del elemento.
        """
        if self._lore_el is None or self._lore_el.is_deleted:
            if self._lore_timer is not None:
                self._lore_timer.deactivate()
                self._lore_timer = None
            return
        self._lore_el.content = _lore_markup(next(self._lore_iter))

    def _go_step2(self) -> None:
        self._step = 2
        self._step_label.set_text("Paso 2 de 2")
        self._progress_bar.style(
            "width:100%; height:100%; background:var(--sky-gold); border-radius:2px; transition:width 0.3s ease;"
        )
        self._step1_container.style("display: none;")
        self._step2_container.style("display: flex;")
        self._next_btn.style("display: none;")
        self._back_btn.style("display: block; color: var(--sky-gold);")
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
        search_key = self._search_input.value.strip() if self._search_input else ""
        telegram_token = self._telegram_token_input.value.strip() if self._telegram_token_input else ""
        telegram_chatid = self._telegram_id_input.value.strip() if self._telegram_id_input else ""

        await self._validate_and_save(
            provider=provider,
            api_key=api_key,
            nexus_key=nexus_key,
            search_key=search_key,
            telegram_token=telegram_token,
            telegram_chatid=telegram_chatid,
        )

    async def _validate_and_save(
        self,
        provider: str,
        api_key: str,
        nexus_key: str,
        search_key: str,
        telegram_token: str,
        telegram_chatid: str,
    ) -> None:
        error = validate_credentials(provider, api_key, telegram_token, telegram_chatid, require_api_key=True)
        if error is not None:
            ui.notify(error, type="negative")
            return

        # ── Fase A: validar y persistir ──────────────────────────────────
        # Todo lo que pueda escribir secretos queda dentro de este bloque. Si
        # falla, el overlay todavía no se tocó: recién acá es verdad que el
        # wizard sigue abierto. Las credenciales ya escritas no se revierten:
        # se informa que la aplicación puede haber quedado parcial.
        try:
            # Un campo vacío en búsqueda es una intención explícita de desactivar.
            # Se resuelve ANTES de escribir otros secretos para no mutar de más
            # cuando el backend no permite confirmar el borrado.
            if not search_key and not _desactivar_search_key():
                ui.notify(
                    "No se pudo desactivar la clave de búsqueda; el wizard sigue abierto para reintentar.",
                    type="negative",
                )
                return

            from sky_claw.config import Config

            cfg = Config(self._config_path)

            if not search_key:
                # Config puede rehidratar un secreto histórico del TOML. La
                # intención explícita de borrado se registra en el objeto para
                # que Config.save() no lo reinserte, y se confirma de nuevo
                # ANTES de escribir credenciales opcionales.
                cfg._data["search_api_key"] = ""
                if not _desactivar_search_key():
                    ui.notify(
                        "No se pudo desactivar la clave de búsqueda; el wizard sigue abierto para reintentar.",
                        type="negative",
                    )
                    return

            key_map = {
                f"{provider}_api_key": api_key,
                "nexus_api_key": nexus_key,
                "search_api_key": search_key,
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

            cfg._data["llm_provider"] = provider
            if telegram_chatid:
                cfg._data["telegram_chat_id"] = telegram_chatid.replace("@", "")

            if not search_key:
                mensaje_fallo = (
                    "No se pudo confirmar la desactivación de la clave de búsqueda; "
                    "la configuración puede haberse aplicado parcialmente. "
                    "El wizard sigue abierto para reintentar."
                )
                # Primer guardado: registra el borrado sin publicar first_run.
                cfg.save()
                # ``Config.save`` puede reportar ``PasswordDeleteError`` como
                # pendiente incluso cuando el secreto ya no existe. La verdad
                # la da la lectura: sólo ausencia verificada habilita el éxito.
                if not _search_key_ausente():
                    ui.notify(mensaje_fallo, type="negative")
                    return

                # Reafirmar la mutación crea una generación nueva: el segundo
                # save trata el borrado como explícito y no migra un secreto
                # que otra superficie haya vuelto a dejar en el TOML.
                cfg._data["search_api_key"] = ""
                cfg.save()
                if not _search_key_ausente():
                    ui.notify(mensaje_fallo, type="negative")
                    return

                # El guardado que cierra el wizard no puede migrar un plaintext
                # que otra superficie haya dejado en el TOML. Se comprueba el
                # archivo, se limpia con un guardado explícito si apareció y
                # recién entonces se persiste first_run=False.
                if not _config_sin_search_key(self._config_path):
                    cfg._data["search_api_key"] = ""
                    cfg.save()
                    ui.notify(mensaje_fallo, type="negative")
                    return

                cfg._data["first_run"] = False
                cfg.save()
            else:
                cfg._data["first_run"] = False
                cfg.save()

        except Exception as e:
            # El mensaje de una excepción del backend puede arrastrar secretos.
            # Se registra sólo la clase y se muestra un texto fijo.
            logger.error("No se pudo completar la configuración (%s).", type(e).__name__)
            ui.notify(
                "No se pudo completar la configuración; puede haberse aplicado parcialmente. El wizard sigue abierto.",
                type="negative",
            )
            return

        # ── Fase B: limpieza y finalización ──────────────────────────────
        # La persistencia ya terminó. Ningún fallo de aquí hacia abajo puede
        # describirse como "no se pudo guardar" ni revertir credenciales ya
        # escritas. Cada paso se intenta una sola vez y se informa por separado.
        fallos_finalizacion: list[str] = []

        try:
            await self._clear_drafts()
        except Exception as e:
            logger.warning("No se pudieron limpiar los borradores (%s).", type(e).__name__)
            fallos_finalizacion.append("borradores")

        try:
            get_store().set("first_run", False)
        except Exception as e:
            logger.warning("No se pudo publicar first_run en el estado (%s).", type(e).__name__)
            fallos_finalizacion.append("estado")

        # Eliminar overlay del DOM — pero el temporizador de lore fue creado como
        # elemento de la página (fuera de overlay_el), así que NO muere con
        # él y hay que desactivarlo explícitamente acá. La guarda por
        # is_deleted de _rotate_lore sigue como segunda línea (si algún
        # manejador deja el modal borrado sin pasar por acá).
        if self._overlay_el:
            try:
                self._overlay_el.delete()
            except Exception as e:
                logger.warning("No se pudo cerrar el overlay (%s).", type(e).__name__)
                fallos_finalizacion.append("overlay")

        if self._lore_timer is not None:
            try:
                self._lore_timer.deactivate()
            except Exception as e:
                logger.warning("No se pudo desactivar el temporizador de lore (%s).", type(e).__name__)
                fallos_finalizacion.append("temporizador")
            finally:
                self._lore_timer = None

        try:
            await self._on_complete()
        except Exception as e:
            logger.error("Falló la finalización del wizard (%s).", type(e).__name__)
            fallos_finalizacion.append("finalización")

        if fallos_finalizacion:
            ui.notify(
                "Configuración guardada, pero la finalización quedó incompleta: "
                + ", ".join(fallos_finalizacion)
                + ".",
                type="warning",
            )
        else:
            logger.info("Setup completado — provider=%s", provider)

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

from __future__ import annotations

import abc
import asyncio
import logging
import queue
from typing import Dict, Any, List, Optional

from nicegui import ui, app

logger = logging.getLogger(__name__)

# =============================================================================
# STRATEGY PATTERN: MANEJO DE MENSAJES DE COLA (THREAD-SAFE)
# =============================================================================

class MessageHandlerStrategy(abc.ABC):
    @abc.abstractmethod
    def handle(self, gui: "SkyClawGUI", data: Any) -> None:
        pass

class ResponseHandler(MessageHandlerStrategy):
    def handle(self, gui: "SkyClawGUI", data: Any) -> None:
        gui.custom_log_push(f"Agente: {data}", "text-[#c5a059]")

class ModlistHandler(MessageHandlerStrategy):
    def handle(self, gui: "SkyClawGUI", data: Any) -> None:
        gui.update_mod_list(data)

class SuccessHandler(MessageHandlerStrategy):
    def handle(self, gui: "SkyClawGUI", data: Any) -> None:
        gui.custom_log_push(f"[ÉXITO] {data}", "text-[#eab308] font-bold")
        gui.enable_action_buttons()

class ErrorHandler(MessageHandlerStrategy):
    def handle(self, gui: "SkyClawGUI", data: Any) -> None:
        gui.custom_log_push(f"[ERROR] {data}", "text-[#ef4444] font-bold")
        gui.enable_action_buttons()


# =============================================================================
# BUILDER PATTERN: ARQUITECTURA FRONTEND "ELDER SCROLLS" PURE CSS
# =============================================================================

class GUIBuilder:
    """
    Construye una UI indestructible basada en CSS/Tailwind (Elder Scrolls Theme).
    Cero dependencias de assets locales (.jpg/.svg crudos) para garantizar
    compatibilidad 100% atómica con PyInstaller.
    """
    
    def __init__(self, gui: "SkyClawGUI") -> None:
        self.gui = gui

    def build(self) -> None:
        self._build_base_styles()
        
        # Contenedor Principal (Reemplaza el body normal)
        with ui.column().classes('w-full min-h-screen p-0 m-0 gap-0 bg-transparent fade-in'):
            self._build_header()
            
            # Layout Dividido (Panel VFS y Terminal Arcana)
            with ui.row().classes('w-full max-w-[1600px] mx-auto flex-grow gap-6 p-6 h-[calc(100vh-80px)] flex-nowrap'):
                self._build_mod_panel()
                self._build_console_panel()

    def _build_base_styles(self) -> None:
        """Inyección segura de variables y fuentes al <head> del DOM.

        Uses system font stacks as fallback to guarantee offline and
        PyInstaller compatibility — no external CDN dependency.
        """
        ui.add_head_html('''
        <style>
            
            @keyframes fadeIn {
                0% { opacity: 0; transform: translateY(10px); }
                100% { opacity: 1; transform: translateY(0); }
            }
            .fade-in {
                animation: fadeIn 0.8s ease-out forwards;
            }
            
            body { 
                background: radial-gradient(circle at top, #1a1612 0%, #050505 100%) !important;
                background-color: #050505 !important;
                color: #d1d5db; 
                margin: 0; padding: 0;
                overflow: hidden;
                font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
            }
            /* SVG Dragon / Scroll motif subtle background overlay */
            body::before {
                content: "";
                position: absolute;
                top: 0; left: 0; right: 0; bottom: 0;
                background-image: url('data:image/svg+xml;utf8,<svg width="400" height="400" viewBox="0 0 400 400" xmlns="http://www.w3.org/2000/svg"><path d="M 200 40 C 250 80, 280 150, 200 200 C 120 250, 150 320, 200 360 C 250 320, 280 250, 200 200 C 120 150, 150 80, 200 40 Z" fill="none" stroke="rgba(197, 160, 89, 0.03)" stroke-width="2"/></svg>');
                background-size: 800px;
                background-position: center;
                opacity: 0.6;
                z-index: -1;
                pointer-events: none;
            }
            .es-title { font-family: 'Cinzel', 'Palatino Linotype', 'Book Antiqua', Palatino, serif; text-shadow: 0 2px 4px rgba(0,0,0,0.8); }
            .es-panel { 
                background: rgba(20, 20, 20, 0.7) !important;
                border: 1px solid rgba(184, 134, 11, 0.3);
                box-shadow: 0 0 20px rgba(184, 134, 11, 0.05); /* very faint golden glow */
                border-radius: 8px;
                backdrop-filter: blur(12px) !important;
                -webkit-backdrop-filter: blur(12px) !important;
            }
            .es-header-border { 
                border-bottom: 1px solid rgba(184, 134, 11, 0.3); 
                background: rgba(15, 15, 15, 0.7) !important;
                backdrop-filter: blur(12px) !important;
                -webkit-backdrop-filter: blur(12px) !important;
            }
            .es-btn {
                background: rgba(15, 15, 15, 0.6) !important;
                border: 1px solid rgba(184, 134, 11, 0.4);
                color: #c5a059 !important;
                font-family: 'Cinzel', 'Palatino Linotype', 'Book Antiqua', Palatino, serif;
                text-transform: uppercase;
                letter-spacing: 1px;
                border-radius: 4px;
                transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                cursor: pointer;
            }
            .es-btn:hover:not(:disabled) {
                background: rgba(25, 20, 15, 0.8) !important;
                box-shadow: 0 0 12px rgba(184, 134, 11, 0.4), inset 0 0 8px rgba(184, 134, 11, 0.2);
                color: #ffd700 !important;
                border-color: rgba(255, 215, 0, 0.8);
            }
            .es-btn:disabled { 
                opacity: 0.4; 
                cursor: not-allowed; 
                border-color: rgba(74, 60, 34, 0.5); 
                color: #6a5c42 !important; 
            }
            .console-text { font-family: 'JetBrains Mono', 'Cascadia Code', 'Consolas', 'Courier New', monospace; line-height: 1.6; }
            
            /* Custom Scrollbar (WebKit) */
            ::-webkit-scrollbar { width: 6px; }
            ::-webkit-scrollbar-track { background: rgba(5,5,5,0.5); border-left: 1px solid rgba(30,25,20,0.5); }
            ::-webkit-scrollbar-thumb { background: rgba(139, 107, 51, 0.5); border-radius: 3px; }
            ::-webkit-scrollbar-thumb:hover { background: rgba(197, 160, 89, 0.8); }
        </style>
        ''')

    def _build_header(self) -> None:
        with ui.row().classes('w-full justify-center items-center py-4 bg-transparent es-header-border shadow-2xl h-[80px]'):
            ui.label('SKY-CLAW').classes('es-title text-4xl font-bold text-[#c5a059] tracking-[0.2em]')
            ui.label('AGENTE AUTÓNOMO').classes('es-title text-xl text-[#8b6b33] mt-2 ml-4 tracking-widest opacity-80')

    def _build_mod_panel(self) -> None:
        with ui.column().classes('w-1/3 h-full es-panel p-5 relative flex-nowrap'):
            ui.label('ORDEN DE CARGA (VFS)').classes('es-title text-lg font-bold text-[#c5a059] mb-2 w-full text-center border-b border-[#4a3c22] pb-2 shrink-0')
            
            # Contenedor scrolleable aislado para no romper el flex
            with ui.scroll_area().classes('w-full flex-grow pr-2') as mod_scroll:
                self.gui.mod_list = ui.column().classes('w-full gap-1')
            
            with ui.row().classes('w-full mt-4 pt-4 border-t border-[#4a3c22] gap-3 shrink-0'):
                self.gui.btn_update = ui.button('Actualizar', on_click=self.gui.update_all).classes('es-btn flex-grow h-12')
                self.gui.btn_scan = ui.button('Escanear', on_click=self.gui.scan_all).classes('es-btn flex-grow h-12')

    def _build_console_panel(self) -> None:
        with ui.column().classes('w-2/3 h-full es-panel p-5 relative flex-nowrap'):
            ui.label('TERMINAL ARCANA').classes('es-title text-lg font-bold text-[#c5a059] mb-2 w-full text-center border-b border-[#4a3c22] pb-2 shrink-0')
            
            # Scroll Area de alto rendimiento para logs
            self.gui.chat_display = ui.scroll_area().classes('w-full flex-grow bg-[#050505] border border-[#2a2a2a] p-4 mb-4 rounded')
            with self.gui.chat_display:
                self.gui.chat_content = ui.column().classes('w-full gap-1')
            
            with ui.row().classes('w-full items-stretch gap-3 h-14 shrink-0'):
                self.gui.input = ui.input(placeholder='Ingresá tu comando aquí...').classes(
                    'flex-grow text-[#d1d5db] console-text text-base'
                ).props('dark standard color="amber"').on('keydown.enter', self.gui.fire_ejecutar)
                
                ui.button('EJECUTAR', on_click=self.gui.fire_ejecutar).classes('es-btn h-full px-8 text-lg')


# =============================================================================
# CONTROLADOR PRINCIPAL
# =============================================================================

MAX_CONSOLE_MESSAGES = 500


class SkyClawGUI:
    """
    Gestor de la GUI. Enruta eventos entre NiceGUI y el Backend.
    Resuelve los problemas de colisión del Event Loop de Uvicorn.
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._running: bool = True
        self._message_labels: list = []  # explicit tracking for O(1) eviction
        self._is_thinking: bool = False
        self._thinking_label: Optional[ui.label] = None
        self._bg_tasks: set = set()
        self.btn_update: Optional[ui.button] = None
        self.btn_scan: Optional[ui.button] = None
        self.mod_list: Optional[ui.column] = None
        self.chat_display: Optional[ui.scroll_area] = None
        self.chat_content: Optional[ui.column] = None
        self.input: Optional[ui.input] = None

        self.handlers: Dict[str, MessageHandlerStrategy] = {
            "response": ResponseHandler(),
            "modlist": ModlistHandler(),
            "success": SuccessHandler(),
            "error": ErrorHandler(),
        }
    
    async def _load_initial_mods(self) -> None:
        try:
            mods_dicts = await self.ctx.registry.search_mods("")
            mods = [m["name"] for m in mods_dicts]
            self.update_mod_list(mods)
            self.custom_log_push("Sky-Claw inicializado. Conexión con Nexus/MO2 establecida.", "text-[#8b6b33]")
        except Exception as e:
            logger.error(f"Fallo cargando mods iniciales: {e}")
            self.custom_log_push(f"[SISTEMA] Error accediendo a la DB: {str(e)}", "text-[#ef4444] font-bold")

    def _shutdown(self) -> None:
        logger.info("Cerrando interfaz gráfica...")
        self._running = False

    def custom_log_push(self, text: str, style_class: str = "text-[#d1d5db]") -> None:
        """Inyecta texto tipado en la consola con buffer circular."""
        self._hide_thinking()
        # Evict oldest message labels when buffer is full
        while len(self._message_labels) >= MAX_CONSOLE_MESSAGES:
            oldest = self._message_labels.pop(0)
            try:
                self.chat_content.remove(oldest)
            except (ValueError, KeyError):
                pass
        with self.chat_content:
            lbl = ui.label(text).classes(f'console-text text-sm break-words w-full mb-1 {style_class}')
        self._message_labels.append(lbl)
        self.chat_display.scroll_to(percent=1.0)

    def _show_thinking(self) -> None:
        """Show a visual indicator while the LLM is processing."""
        if self._is_thinking:
            return
        self._is_thinking = True
        with self.chat_content:
            self._thinking_label = ui.label(
                '⏳ Procesando...'
            ).classes('console-text text-sm text-[#c5a059] animate-pulse mb-1')
        self.chat_display.scroll_to(percent=1.0)

    def _hide_thinking(self) -> None:
        """Remove the thinking indicator."""
        if self._thinking_label is not None:
            try:
                self.chat_content.remove(self._thinking_label)
            except (ValueError, KeyError):
                pass
            self._thinking_label = None
        self._is_thinking = False

    def send_message(self) -> None:
        text = self.input.value.strip()
        if not text:
            return

        self.custom_log_push(f"> {text}", "text-white font-bold")
        self.input.value = ""
        self._show_thinking()
        self.ctx.logic_queue.put(("chat", text))

    def fire_ejecutar(self) -> None:
        text = self.input.value.strip()
        if text:
            self.send_message()
        else:
            if hasattr(self, 'on_ejecutar'):
                task = asyncio.create_task(self.on_ejecutar({"type": "EJECUTAR", "payload": {}}))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            self.custom_log_push("> [Señal de Ejecución Forzada enviada al Demonio]", "text-[#c5a059] font-bold")

    def _poll_queue(self) -> None:
        """Sondea la cola del backend sin bloquear el hilo principal de Uvicorn."""
        if not self._running:
            return
        try:
            while True:
                msg_type, data = self.ctx.gui_queue.get_nowait()
                handler = self.handlers.get(msg_type)
                if handler:
                    handler.handle(self, data)
                else:
                    logger.warning(f"Mensaje desconocido en cola UI: '{msg_type}'")
        except queue.Empty:
            pass
        except Exception:
            logger.exception("Error procesando cola GUI:")

    def enable_action_buttons(self) -> None:
        if self.btn_update: self.btn_update.enable()
        if self.btn_scan: self.btn_scan.enable()

    def disable_action_buttons(self) -> None:
        if self.btn_update: self.btn_update.disable()
        if self.btn_scan: self.btn_scan.disable()

    def update_mod_list(self, mods: List[str]) -> None:
        if not self.mod_list:
            return
            
        self.mod_list.clear()
        with self.mod_list:
            for i, mod in enumerate(mods, 1):
                with ui.row().classes('w-full items-center py-2 border-b border-[#2a2a2a] hover:bg-[#1f1a14] transition-colors px-2 flex-nowrap'):
                    ui.label().classes('w-[3px] h-[14px] bg-[#8b6b33] shrink-0')
                    ui.label(f"{i:03d}").classes('console-text text-xs text-[#666] w-8 shrink-0')
                    ui.label(mod).classes('console-text text-sm text-[#d1d5db] flex-grow truncate')

    def update_all(self) -> None:
        self.disable_action_buttons()
        self.custom_log_push("Iniciando ciclo de actualización de mods...", "text-[#8b6b33]")
        self.ctx.logic_queue.put(("chat", "/update_mods"))

    def scan_all(self) -> None:
        self.disable_action_buttons()
        self.custom_log_push("Iniciando escaneo de VFS...", "text-[#8b6b33]")
        self.ctx.logic_queue.put(("chat", "/scan"))

    def build_ui(self) -> None:
        """
        Punto de entrada para la generación de la interfaz.
        """
        GUIBuilder(self).build()
        ui.timer(0.1, self._poll_queue)
        ui.timer(0.5, self._load_initial_mods, once=True)
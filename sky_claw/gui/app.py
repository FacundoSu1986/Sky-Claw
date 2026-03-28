import logging
import queue
import asyncio
import threading
from nicegui import ui, app

logger = logging.getLogger(__name__)

class SkyClawGUI:
    """Modern UI for Sky-Claw using NiceGUI."""
    
    def __init__(self, ctx):
        self.ctx = ctx
        self._running = True
        self.btn_update = None
        self.btn_scan = None
        self._setup_ui()
    
    def _setup_ui(self):
        ui.query('body').classes('m-0 p-0 text-gray-300 bg-black')
        ui.query('body').style("background-image: url('/assets/alduin_menace_bg.jpg'); background-size: cover; background-position: center; background-attachment: fixed;")
        
        ui.add_head_html('''
        <style>
            .dragon-glow {
                transition: all 0.3s ease;
            }
            .dragon-glow:hover:not(:disabled) {
                box-shadow: 0 0 15px #FFD700, inset 0 0 10px #B8860B;
            }
            .dragon-glow:disabled {
                opacity: 0.5;
                cursor: not-allowed;
            }
            
            /* Hide scrollbar for a cleaner cinematic look */
            ::-webkit-scrollbar { width: 8px; }
            ::-webkit-scrollbar-track { background: transparent; }
            ::-webkit-scrollbar-thumb { background: #404040; border-radius: 4px; }
            ::-webkit-scrollbar-thumb:hover { background: #B8860B; }
        </style>
        ''')
        
        # Main Layout Container - with backdrop blur and semi-transparent overlay
        # This keeps the dragon recognizable while maintaining high UI contrast
        with ui.column().classes('w-full max-w-6xl mx-auto h-[95vh] mt-[2.5vh] p-4 z-10 relative flex flex-col bg-black/60 backdrop-blur-sm rounded-xl shadow-2xl border border-[#404040]/50'):
            # Header
            with ui.row().classes('w-full justify-center py-4 items-center gap-4 border-b border-[#404040]/50'):
                ui.html('''
                <svg width="50" height="50" viewBox="0 0 100 100" fill="#B8860B">
                    <!-- Stylized Skyrim Dragon Diamond Emblem -->
                    <path d="M50,0 L90,40 L50,100 L10,40 Z" />
                </svg>
                ''')
                ui.label('SKY-CLAW').classes('text-4xl font-bold text-[#B8860B] tracking-widest uppercase')

            with ui.row().classes('w-full flex-grow gap-6 h-full p-2 mt-4'):
                # Left Panel: Mods
                with ui.card().classes('w-1/3 h-full bg-[#1A1A1A]/80 border-2 border-[#404040] shadow-lg flex flex-col p-4 relative'):
                    ui.label('Active Mods').classes('text-xl font-bold text-[#B8860B] mb-2 uppercase border-b border-[#404040] pb-2 w-full')
                    
                    self.mod_list = ui.column().classes('w-full flex-grow overflow-y-auto pr-2 gap-0')
                    
                    with ui.row().classes('w-full mt-auto pt-4 justify-center gap-2'):
                        self.btn_update = ui.button('Update Mods', on_click=self._update_all).classes(
                            'bg-[#1A1A1A] text-[#FFD700] border border-[#B8860B] dragon-glow font-bold flex-grow'
                        )
                        self.btn_scan = ui.button('Scan', on_click=self._scan_all).classes(
                            'bg-[#1A1A1A] text-[#FFD700] border border-[#B8860B] dragon-glow font-bold flex-grow'
                        )
                
                # Right Panel: Agent Console
                with ui.card().classes('w-2/3 h-full bg-[#1A1A1A]/80 border-2 border-[#404040] shadow-lg flex flex-col p-4 relative'):
                    ui.label('Agent Console').classes('text-xl font-bold text-[#B8860B] mb-2 uppercase border-b border-[#404040] pb-2 w-full')
                    
                    # Custom Log Area
                    self.chat_display = ui.column().classes(
                        'w-full flex-grow bg-black/80 border border-[#404040] p-3 overflow-y-auto mb-4 custom-log-container gap-1'
                    )
                    
                    with ui.row().classes('w-full items-center gap-2 mt-auto h-12'):
                        self.input = ui.input(placeholder='Enter command...').classes(
                            'flex-grow text-white h-full'
                        ).props('dark outlined').on('keydown.enter', self._send_message)
                        ui.button('Send', on_click=self._send_message).classes(
                            'bg-[#1A1A1A] text-[#FFD700] border border-[#B8860B] dragon-glow font-bold h-full px-6'
                        )

        # Start background polling wrapper that doesn't block the UI thread
        self.poll_timer = ui.timer(0.1, self._poll_queue)
        
        # Lectura inicial de base de datos
        app.on_startup(self._load_initial_mods)
        
        # Graceful Shutdown
        app.on_shutdown(self._shutdown)

    async def _load_initial_mods(self):
        """Carga la lista inicial de mods de la DB.
        Debe estar envuelta en un try/except para no colapsar el loop de uvicorn si ocurre un error (AttributeError, etc).
        """
        try:
            # Usar await search_mods("") ya que AsyncModRegistry es asincrónico y get_all no existe.
            mods_dicts = await self.ctx.registry.search_mods("")
            mods = [m["name"] for m in mods_dicts]
            self._update_mod_list(mods)
        except Exception as e:
            logger.error(f"Error cargando mods iniciales en GUI: {e}")
            self._custom_log_push(f"[SYSTEM ERROR] Fallo al leer DB inicial: {str(e)}", "text-red-500 font-bold")

    def _shutdown(self):
        logger.info("Initiating UI graceful shutdown...")
        self._running = False

    def _custom_log_push(self, text, style_class="text-gray-300"):
        with self.chat_display:
            ui.label(text).classes(f'font-mono text-sm {style_class} break-words w-full')
            # auto scroll down via JS
            ui.run_javascript("document.querySelectorAll('.custom-log-container').forEach(el => el.scrollTop = el.scrollHeight);")

    def _send_message(self):
        text = self.input.value.strip()
        if not text:
            return
        
        self._custom_log_push(f"> {text}", "text-white font-bold")
        self.input.value = ""
        self.ctx.logic_queue.put(("chat", text))

    def _poll_queue(self):
        if not self._running:
            return
        try:
            while True:
                msg_type, data = self.ctx.gui_queue.get_nowait()
                if msg_type == "response":
                    self._custom_log_push(f"Agent: {data}", "text-gray-300")
                elif msg_type == "modlist":
                    self._update_mod_list(data)
                elif msg_type == "success":
                    self._custom_log_push(f"[SUCCESS] {data}", "text-[#FFD700] font-bold")
                    self._enable_action_buttons()
                elif msg_type == "error":
                    self._custom_log_push(f"[ERROR] {data}", "text-red-500 font-bold")
                    self._enable_action_buttons()
        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"Error reading gui queue: {e}")
            self._custom_log_push(f"[SYSTEM ERROR] Exception in gui queue: {str(e)}", "text-red-500 font-bold")
            self._enable_action_buttons()

    def _enable_action_buttons(self):
        if self.btn_update:
            self.btn_update.enable()
        if self.btn_scan:
            self.btn_scan.enable()

    def _disable_action_buttons(self):
        if self.btn_update:
            self.btn_update.disable()
        if self.btn_scan:
            self.btn_scan.disable()

    def _update_mod_list(self, mods):
        self.mod_list.clear()
        with self.mod_list:
            for i, mod in enumerate(mods, 1):
                with ui.row().classes('w-full items-center justify-between py-1 border-b border-[#404040]/30'):
                    ui.label(f"{i}.").classes('text-xs text-[#404040] w-6')
                    ui.label(mod).classes('text-sm text-gray-300 flex-grow truncate')

    def _update_all(self):
        self._disable_action_buttons()
        self.ctx.logic_queue.put(("chat", "/update_mods"))

    def _scan_all(self):
        self._disable_action_buttons()
        self.ctx.logic_queue.put(("chat", "/scan"))

    def run(self):
        # We explicitly turn off auto-reload. Given we run in to_thread, 
        # we can just block here without an extra threading.Thread.
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            ui.run(title="Sky-Claw Agent", dark=True, show=False, reload=False)
        except Exception as e:
            logger.error(f"NiceGUI crash collission handled: {e}")

import os
import time
import asyncio
import logging
from sky_claw.core.database import DatabaseAgent
from sky_claw.scraper.scraper_agent import ScraperAgent
from sky_claw.core.windows_interop import ModdingToolsAgent
from sky_claw.comms.interface import InterfaceAgent
from sky_claw.core.models import LootExecutionParams, ModMetadataQuery, HitlApprovalRequest
import psutil

logger = logging.getLogger("SkyClaw.Watcher")
security_logger = logging.getLogger("SkyClaw.Security")

class SupervisorAgent:
    def __init__(self, profile_name: str = "Default"):
        self.db = DatabaseAgent()
        self.scraper = ScraperAgent(self.db)
        self.tools = ModdingToolsAgent()
        self.interface = InterfaceAgent()
        self.profile_name = profile_name
        # Ruta estándar de MO2 montada en WSL2
        self.modlist_path = f"/mnt/c/Modding/MO2/profiles/{self.profile_name}/modlist.txt"

    async def start(self):
        await self.db.init_db()
        # Vincular con la señal de ejecución de la interfaz
        self.interface.register_command_callback(self.handle_execution_signal)
        
        logger.info("SupervisorAgent inicializado: IPC y Watcher listos. Lanzando TaskGroup de fondo...")
        
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.interface.connect())
                tg.create_task(self._proactive_watcher())
                tg.create_task(self._telemetry_worker())
        except Exception as e:
            logger.error(f"TaskGroup del Supervisor cancelado por error fatal: {e}")

    async def _proactive_watcher(self):
        """
        Polling asincrónico del modlist.txt. 
        Evita inotify porque falla a través del protocolo 9P (WSL2 -> Windows).
        """
        mem_key = f"modlist_mtime_{self.profile_name}"
        
        while True:
            try:
                if os.path.exists(self.modlist_path):
                    current_mtime = os.stat(self.modlist_path).st_mtime
                    last_mtime_str = await self.db.get_memory(mem_key)
                    last_mtime = float(last_mtime_str) if last_mtime_str else 0.0

                    if current_mtime > last_mtime:
                        logger.info("Modificación detectada en MO2 desde fuera del agente. Iniciando análisis proactivo.")
                        await self.db.set_memory(mem_key, str(current_mtime), time.time())
                        await self._trigger_proactive_analysis()
            except Exception as e:
                logger.error(f"RCA: Fallo en el watcher asincrónico: {str(e)}")
            
            # Polling ligero cada 10 segundos
            await asyncio.sleep(10.0)

    async def _trigger_proactive_analysis(self):
        """Lógica inyectada al flujo ReAct sin prompt del usuario."""
        logger.info("Analizando topología del Load Order por cambio detectado...")
        # Aquí se inyectaría la llamada real a la herramienta de parsing local.
        pass

    async def handle_execution_signal(self, payload: dict):
        """Reacciona a la señal de ignición forzada desde la GUI."""
        logger.info("Ignición forzada desde GUI detectada. Despertando demonio proactivo.")
        await self._trigger_proactive_analysis()

    async def dispatch_tool(self, tool_name: str, payload_dict: dict) -> dict:
        """
        Enrutador estricto. El LLM devuelve 'tool_name' y 'payload_dict'.
        Se valida con Pydantic inmediatamente.
        """
        match tool_name:
            case "query_mod_metadata":
                params = ModMetadataQuery(**payload_dict)
                return await self.scraper.query_nexus(params)
                
            case "execute_loot_sorting":
                params = LootExecutionParams(**payload_dict)
                # Requiere HITL si implica cambios destructivos o sobreescritura de metadatos sensibles
                hitl_req = HitlApprovalRequest(
                    action_type="destructive_xedit", # Reusado conceptualmente
                    reason="Se va a reordenar el Load Order, lo que podría afectar partidas guardadas.",
                    context_data={"profile": params.profile_name}
                )
                decision = await self.interface.request_hitl(hitl_req)
                
                if decision == "approved":
                    return await self.tools.run_loot(params)
                else:
                    return {"status": "aborted", "reason": "Usuario denegó la operación."}
                    
            case _:
                logger.error(f"RCA: LLM alucinó la herramienta '{tool_name}'.")
                return {"status": "error", "reason": "ToolNotFound"}

    async def _telemetry_worker(self):
        """Monitorea el uso de recursos del propio proceso y lo envía al Gateway."""
        proc = psutil.Process()
        while True:
            try:
                cpu_usage = proc.cpu_percent(interval=None) # Porcentaje de un solo núcleo o total
                ram_mb = proc.memory_info().rss / (1024 * 1024) # MB
                
                telemetry = {
                    "cpu": round(cpu_usage, 1),
                    "ram": round(ram_mb, 1)
                }
                
                await self.interface.send_telemetry(telemetry)
            except Exception as e:
                logger.error(f"Error en worker de telemetría: {e}")
            
            await asyncio.sleep(3.0)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    
    supervisor = SupervisorAgent()
    # asyncio.run(supervisor.start()) # En producción

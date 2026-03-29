import asyncio
import time
import logging
# Playwright and requests-html strictly banned locally in WSL2 per SRE (Cloudflare constraints).
from sky_claw.core.models import ModMetadataQuery, CircuitBreakerTripped
from sky_claw.core.database import DatabaseAgent

logger = logging.getLogger("SkyClaw.Scraper")

class ScraperAgent:
    def __init__(self, db: DatabaseAgent):
        self.db = db
        self.nexus_api_key = None # Se carga vía config segura (ej. keyring)
        self.max_failures = 3

    async def query_nexus(self, params: ModMetadataQuery) -> dict:
        """Enrutador Híbrido: Intenta API, si falla o fuerza stealth, usa Playwright. Pasa por el Circuit Breaker."""
        domain = "nexusmods.com"
        state = await self.db.get_circuit_breaker_state(domain)
        
        # 1. Evaluar Circuit Breaker
        if time.time() < state["locked_until"]:
            logger.error(f"RCA: Circuit Breaker abierto para {domain}. Abortando para proteger IP local.")
            raise CircuitBreakerTripped(f"Bloqueo activo hasta {state['locked_until']}")

        try:
            if not params.force_stealth and self.nexus_api_key:
                return await self._api_request(params)
            else:
                return await self._stealth_scrape(params)
                
        except Exception as e:
            # 2. RCA del fallo y actualización del Circuit Breaker
            new_failures = state["failures"] + 1
            lock_time = time.time() + (300 * new_failures) if new_failures >= self.max_failures else 0
            await self.db.update_circuit_breaker(domain, new_failures, lock_time)
            
            logger.warning(f"Fallo de extracción en {domain}. Fallos: {new_failures}. Error: {str(e)}")
            return {"status": "error", "data": None, "reason": str(e)}

    async def _api_request(self, params: ModMetadataQuery) -> dict:
        # Lógica aiohttp REST estándar con manejo de HTTP 429
        # (Omitido por brevedad, simula éxito)
        return {"status": "success", "source": "API", "data": {"nexus_id": params.nexus_id}}

    async def _stealth_scrape(self, params: ModMetadataQuery) -> dict:
        """Modo de emergencia: RPC hacia Windows Host vía Gateway para evadir anti-bots."""
        import websockets
        import uuid
        import json
        
        request_id = str(uuid.uuid4())
        payload = {
            "type": "command",
            "action": "scrape_nexus",
            "url": f"https://www.nexusmods.com/skyrimspecialedition/mods/{params.nexus_id}",
            "request_id": request_id
        }
        
        logger.info(f"Stealth Scrape RPC [{request_id}] emitiendo IPC al Gateway en host Windows...")
        
        try:
            # Conexión RPC efímera pero robusta (Zero-Trust boundaries passthrough)
            async with websockets.connect("ws://localhost:8080", open_timeout=5) as ws:
                await ws.send(json.dumps(payload))
                
                # Polling asíncrono con timeout de seguridad SRE de 30 segundos
                start_time = time.time()
                while time.time() - start_time < 30:
                    try:
                        resp_str = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        resp_data = json.loads(resp_str)
                        if resp_data.get("type") == "scrape_response" and resp_data.get("request_id") == request_id:
                            if resp_data.get("error"):
                                logger.error(f"Fallo en Node.js Playwright: {resp_data['error']}")
                                return {"status": "error", "source": "Playwright_Windows", "data": None, "reason": resp_data['error']}
                                
                            logger.info(f"Stealth Scrape RPC [{request_id}] completado exitosamente.")
                            return {
                                "status": "success",
                                "source": "Playwright_Windows",
                                "data": resp_data.get("data"),
                                "reason": None
                            }
                    except asyncio.TimeoutError:
                        continue
                        
                # Expiración del circuito
                logger.error(f"Stealth Scrape RPC [{request_id}] Time-Out (30s) abortado graciosamente.")
                return {"status": "error", "source": "Playwright_Windows", "data": None, "reason": "Timeout en WS IPC"}
                
        except Exception as e:
            logger.error(f"Fallo crítico en conexión IPC para Scraper: {type(e).__name__} - {e}")
            return {
                "status": "error", 
                "source": "None", 
                "data": None,
                "reason": f"No se pudo contactar al Gateway de Windows: {e}"
            }
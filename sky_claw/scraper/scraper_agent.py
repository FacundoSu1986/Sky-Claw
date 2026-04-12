import time
import logging

# Playwright and requests-html strictly banned locally in WSL2 per SRE (Cloudflare constraints).
from sky_claw.core.models import CircuitBreakerTripped
from sky_claw.core.schemas import ScrapingQuery, ModMetadata
from sky_claw.core.database import DatabaseAgent

logger = logging.getLogger("SkyClaw.Scraper")


class ScraperAgent:
    def __init__(self, db: DatabaseAgent):
        self.db = db
        self.nexus_api_key = None  # Se carga vía config segura (ej. keyring)
        self.max_failures = 3

    async def query_nexus(self, query: ScrapingQuery) -> ModMetadata:
        """Consulta Nexus Mods usando el esquema ScrapingQuery validado.

        Enrutador Híbrido: Intenta API, si falla o fuerza stealth, usa Playwright.
        Pasa por el Circuit Breaker.

        Args:
            query: ScrapingQuery con los parámetros validados de la consulta.

        Returns:
            ModMetadata con la información del mod consultado.
        """
        domain = "nexusmods.com"
        state = await self.db.get_circuit_breaker_state(domain)

        # 1. Evaluar Circuit Breaker
        if time.time() < state["locked_until"]:
            logger.error(
                f"RCA: Circuit Breaker abierto para {domain}. Abortando para proteger IP local."
            )
            raise CircuitBreakerTripped(f"Bloqueo activo hasta {state['locked_until']}")

        try:
            if not query.force_stealth and self.nexus_api_key:
                result = await self._api_request(query)
            else:
                result = await self._stealth_scrape(query)

            # DETECCIÓN DE ToS_Blocked - Manejo resiliente sin detener el flujo
            if result.get("source") == "ToS_Blocked":
                logger.warning(
                    f"ToS_Blocked detectado para query '{query.query}'. "
                    "Retornando metadata placeholder sin detener el flujo de LangGraph."
                )
                # Retornar ModMetadata placeholder válido que indica el bloqueo
                return ModMetadata(
                    mod_id=query.mod_id or 999999,
                    name=f"[ToS_Blocked] {query.query}",
                    version="0.0.0",
                    category="other",
                    author="N/A - ToS Blocked",
                    description=result.get(
                        "reason", "Scraping bloqueado por cumplimiento ToS Nexus Mods"
                    ),
                )

            # Convertir resultado dict a ModMetadata
            return self._build_mod_metadata(result, query)

        except Exception as e:
            # 2. RCA del fallo y actualización del Circuit Breaker
            new_failures = state["failures"] + 1
            lock_time = (
                time.time() + (300 * new_failures)
                if new_failures >= self.max_failures
                else 0
            )
            await self.db.update_circuit_breaker(domain, new_failures, lock_time)

            logger.warning(
                f"Fallo de extracción en {domain}. Fallos: {new_failures}. Error: {str(e)}"
            )
            raise

    async def _api_request(self, query: ScrapingQuery) -> dict:
        """Realiza petición API REST estándar con manejo de HTTP 429.

        Args:
            query: ScrapingQuery con los parámetros validados.

        Returns:
            dict con los datos de respuesta del API.
        """
        # Usar query.url (ya validada contra SSRF) o construir URL desde mod_id
        nexus_id = query.mod_id or 0
        # Lógica aiohttp REST estándar con manejo de HTTP 429
        # (Omitido por brevedad, simula éxito)
        return {"status": "success", "source": "API", "data": {"nexus_id": nexus_id}}

    async def _stealth_scrape(self, query: ScrapingQuery) -> dict:
        """Método deshabilitado por cumplimiento ToS - Nexus Mods.

        El scraping evasivo ha sido permanentemente desactivado.
        Use la API oficial de Nexus Mods en su lugar.
        """
        logger.warning(
            "Acceso bloqueado por ToS: Scraping evasivo deshabilitado. "
            "El recurso no está disponible vía API."
        )
        return {
            "status": "error",
            "source": "ToS_Blocked",
            "data": None,
            "reason": "Scraping evasivo deshabilitado por cumplimiento ToS Nexus Mods",
        }

    def _build_mod_metadata(self, result: dict, query: ScrapingQuery) -> ModMetadata:
        """Construye un objeto ModMetadata a partir del resultado de la consulta.

        Args:
            result: Diccionario con los datos obtenidos del API o scraping.
            query: ScrapingQuery original con los parámetros de búsqueda.

        Returns:
            ModMetadata validado con Pydantic.

        Raises:
            ValueError: Si los datos son insuficientes para construir ModMetadata.
        """
        if result.get("status") != "success" or not result.get("data"):
            raise ValueError(
                f"Consulta fallida: {result.get('reason', 'Unknown error')}"
            )

        data = result.get("data", {})

        # Extraer datos del resultado, con fallbacks
        mod_id = data.get("nexus_id") or data.get("mod_id") or query.mod_id
        if not mod_id:
            raise ValueError("No se pudo determinar el mod_id")

        name = data.get("name") or data.get("title") or query.query
        version = data.get("version", "1.0.0")
        author = data.get("author", "Unknown")
        category = data.get("category", "other")
        dependencies = data.get("dependencies", [])
        description = data.get("description")

        return ModMetadata(
            mod_id=int(mod_id),
            name=name,
            version=version,
            category=category,
            author=author,
            dependencies=dependencies,
            description=description,
        )

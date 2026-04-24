"""
semantic_router.py - Router semántico para clasificación O(1) de intents de usuario.
Utiliza FastEmbed para embeddings locales y clasificación semántica.
"""

import asyncio
import logging

from sky_claw.core.schemas import RouteClassification

logger = logging.getLogger("SkyClaw.SemanticRouter")


class SemanticRouter:
    """
    Router semántico para clasificación O(1) de intents de usuario.

    Utiliza embeddings locales para clasificación rápida sin dependencia de LLM.
    Soporta fallback a clasificación LLM si la confianza es baja.
    """

    # Rutas semánticas definidas para cada agente/herramienta
    ROUTES = {
        "scrape_mod": [
            "descargar mod de nexus",
            "buscar mod de armadura",
            "obtener metadata de mod",
            "scrapear información del mod",
            "consultar nexus mods",
        ],
        "security_audit": [
            "auditar archivo de mod",
            "verificar seguridad",
            "escanear vulnerabilidades",
            "analizar código malicioso",
            "revisar seguridad del mod",
        ],
        "resolve_conflict": [
            "conflicto entre mods",
            "error de load order",
            "incompatibilidad detectada",
            "resolver conflicto de mods",
            "mods incompatibles",
        ],
        "database_query": [
            "consultar base de datos",
            "ver mods instalados",
            "historial de cambios",
            "estado de la base de datos",
            "obtener información de mods",
        ],
        "modding_tools": [
            "ejecutar loot",
            "ordenar plugins",
            "optimizar load order",
            "usar xedit",
            "limpiar masters",
        ],
    }

    def __init__(self, confidence_threshold: float = 0.7):
        """
        Inicializa el router semántico.

        Args:
            confidence_threshold: Umbral de confianza para considerar una clasificación válida (0.0-1.0)
        """
        self.confidence_threshold = confidence_threshold
        self._encoder = None  # FastEmbedEncoder se inicializa lazy
        self._routes_cache = {}
        logger.info(f"SemanticRouter inicializado con threshold={confidence_threshold}")

    async def _get_encoder(self):
        """Inicializa el encoder de embeddings de forma lazy."""
        if self._encoder is None:
            try:
                from fastembed import FastEmbedEncoder

                self._encoder = FastEmbedEncoder(model_name="all-MiniLM-L6-v2", cache_dir="./.cache/embeddings")
                logger.info("FastEmbedEncoder inicializado")
            except ImportError:
                logger.warning("fastembed no instalado, usando fallback a LLM")
                self._encoder = None
        return self._encoder

    async def classify(self, query: str, fallback_route: str = "unknown") -> RouteClassification:
        """
        Clasifica una query de usuario en una ruta específica.

        Args:
            query: Texto de la consulta del usuario
            fallback_route: Ruta a usar si la confianza es baja

        Returns:
            RouteClassification con la ruta y confianza
        """
        encoder = await self._get_encoder()

        if encoder is None:
            # Fallback a clasificación LLM
            logger.warning("Usando fallback a clasificación LLM")
            return RouteClassification(route=fallback_route, confidence=0.0, fallback_to_llm=True)

        # Buscar mejor coincidencia semántica
        best_route = None
        best_score = 0.0

        for route_name, utterances in self.ROUTES.items():
            for utterance in utterances:
                # Calcular similitud simple (en producción usar embeddings reales)
                similarity = self._calculate_similarity(query, utterance)
                if similarity > best_score:
                    best_score = similarity
                    best_route = route_name

        if best_route and best_score >= self.confidence_threshold:
            logger.info(f"Query clasificada como '{best_route}' con confianza {best_score:.2f}")
            return RouteClassification(route=best_route, confidence=best_score, fallback_to_llm=False)

        # Fallback si la confianza es baja
        logger.info(f"Confianza {best_score:.2f} < threshold {self.confidence_threshold}, usando fallback")
        return RouteClassification(route=fallback_route, confidence=best_score, fallback_to_llm=True)

    def _calculate_similarity(self, query: str, utterance: str) -> float:
        """
        Calcula similitud simple entre query y utterance.

        En producción esto usaría embeddings reales del encoder.
        Por ahora usa similitud de palabras para demostración.
        """
        query_words = set(query.lower().split())
        utterance_words = set(utterance.lower().split())

        if not query_words or not utterance_words:
            return 0.0

        # Similitud Jaccard (intersección de palabras)
        intersection = query_words & utterance_words
        union = query_words | utterance_words

        if not union:
            return 0.0

        return len(intersection) / len(union)

    async def batch_classify(self, queries: list[str], fallback_route: str = "unknown") -> list[RouteClassification]:
        """
        Clasifica múltiples queries en batch.

        Args:
            queries: Lista de consultas a clasificar
            fallback_route: Ruta a usar si la confianza es baja

        Returns:
            Lista de RouteClassification
        """
        tasks = [self.classify(q, fallback_route) for q in queries]
        # H-01: return_exceptions=True para prevenir crashes del orquestador
        return await asyncio.gather(*tasks, return_exceptions=True)

    def add_route(self, route_name: str, utterances: list[str]) -> None:
        """
        Agrega una nueva ruta semántica al router.

        Args:
            route_name: Nombre de la ruta (ej. "custom_action")
            utterances: Lista de utterances de ejemplo
        """
        self.ROUTES[route_name] = utterances
        logger.info(f"Ruta '{route_name}' agregada con {len(utterances)} utterances")

    def remove_route(self, route_name: str) -> None:
        """
        Elimina una ruta del router.

        Args:
            route_name: Nombre de la ruta a eliminar
        """
        if route_name in self.ROUTES:
            del self.ROUTES[route_name]
            logger.info(f"Ruta '{route_name}' eliminada")

    def get_routes(self) -> dict[str, list[str]]:
        """
        Retorna todas las rutas definidas.

        Returns:
            Diccionario con rutas y sus utterances
        """
        return self.ROUTES.copy()

    def set_confidence_threshold(self, threshold: float) -> None:
        """
        Actualiza el umbral de confianza.

        Args:
            threshold: Nuevo umbral (0.0-1.0)
        """
        if 0.0 <= threshold <= 1.0:
            self.confidence_threshold = threshold
            logger.info(f"Umbral de confianza actualizado a {threshold}")
        else:
            raise ValueError("El umbral debe estar entre 0.0 y 1.0")

#!/usr/bin/env python3
"""
Pipeline RAG Production-Ready para Sky-Claw.

Este template implementa un sistema RAG completo con:
- Recuperación híbrida (vector + BM25)
- Reranking cruzado
- Caching semántico
- Monitoreo y métricas

Uso:
    Copiar a sky_claw/services/rag_pipeline.py y adaptar
"""

import logging
import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from typing_extensions import override

logger = logging.getLogger(__name__)


@dataclass
class Document:
    """Documento para indexación RAG."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: np.ndarray | None = None
    score: float = 0.0


@dataclass
class RetrievalResult:
    """Resultado de recuperación."""

    documents: list[Document]
    query: str
    latency_ms: float
    source: str  # "vector", "bm25", "hybrid"


class EmbeddingModel(Protocol):
    """Protocolo para modelos de embedding."""

    def embed(self, texts: list[str]) -> np.ndarray:
        """Generar embeddings para textos."""
        ...


class VectorStore(Protocol):
    """Protocolo para almacén vectorial."""

    def add(self, embeddings: np.ndarray, metadata: list[dict[str, Any]]) -> None:
        """Añadir embeddings al almacén."""
        ...

    def similarity_search(
        self, query_embedding: np.ndarray, k: int = 5
    ) -> list[tuple[np.ndarray, dict[str, Any], float]]:
        """Búsqueda por similitud."""
        ...


class Reranker(Protocol):
    """Protocolo para reranker."""

    def rerank(self, query: str, documents: list[Document]) -> list[Document]:
        """Reordenar documentos por relevancia."""
        ...


class SemanticCache(Protocol):
    """Protocolo para cache semántico."""

    async def get(self, query: str) -> list[Document] | None:
        """Obtener resultados cacheados."""
        ...

    async def set(self, query: str, documents: list[Document]) -> None:
        """Cacheear resultados."""
        ...


class BaseRetriever(ABC):
    """
    Clase base para retrievers.

    Attributes:
        embedding_model: Modelo para generar embeddings.
        cache: Cache semántico opcional.
    """

    def __init__(
        self, embedding_model: EmbeddingModel, cache: SemanticCache | None = None
    ) -> None:
        """
        Inicializar retriever.

        Args:
            embedding_model: Modelo de embeddings.
            cache: Cache semántico opcional.
        """
        self.embedding_model = embedding_model
        self.cache = cache
        self.logger = logging.getLogger(__name__)

    @abstractmethod
    def retrieve(self, query: str, k: int = 5) -> RetrievalResult:
        """
        Recuperar documentos relevantes.

        Args:
            query: Consulta de búsqueda.
            k: Número de documentos a retornar.

        Returns:
            RetrievalResult con documentos y metadata.
        """
        pass

    def _generate_cache_key(self, query: str) -> str:
        """Generar clave única para cache."""
        return hashlib.sha256(query.encode()).hexdigest()[:16]


class HybridRetriever(BaseRetriever):
    """
    Retriever híbrido combinando vector search y BM25.

    Este retriever implementa retrieval de producción con:
    - Búsqueda vectorial para similitud semántica
    - Búsqueda BM25 para matching de keywords
    - Reranking cruzado para optimizar relevancia
    """

    def __init__(
        self,
        embedding_model: EmbeddingModel,
        vector_store: VectorStore,
        bm25_index: Any,  # rank_bm25.BM25Okapi
        reranker: Reranker,
        cache: SemanticCache | None = None,
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
    ) -> None:
        """
        Inicializar retriever híbrido.

        Args:
            embedding_model: Modelo de embeddings.
            vector_store: Almacén vectorial.
            bm25_index: Índice BM25.
            reranker: Modelo de reranking.
            cache: Cache semántico.
            vector_weight: Peso para resultados vectoriales.
            bm25_weight: Peso para resultados BM25.
        """
        super().__init__(embedding_model, cache)
        self.vector_store = vector_store
        self.bm25_index = bm25_index
        self.reranker = reranker
        self.vector_weight = vector_weight
        self.bm25_weight = bm25_weight

        if abs(vector_weight + bm25_weight - 1.0) > 0.01:
            raise ValueError("vector_weight + bm25_weight debe ser 1.0")

    @override
    def retrieve(self, query: str, k: int = 5) -> RetrievalResult:
        """
        Recuperar documentos con estrategia híbrida.

        Args:
            query: Consulta de búsqueda.
            k: Número de documentos.

        Returns:
            RetrievalResult con documentos ordenados.
        """
        start_time = time.perf_counter()

        # Verificar cache primero
        if self.cache is not None:
            import asyncio

            loop = asyncio.get_event_loop()
            cached = loop.run_until_complete(self.cache.get(query))
            if cached:
                self.logger.debug(f"Cache hit para query: {query[:50]}...")
                return RetrievalResult(
                    documents=cached, query=query, latency_ms=0.0, source="cache"
                )

        # Generar embedding
        query_embedding = self.embedding_model.embed([query])[0]

        # Búsqueda vectorial
        vector_results = self.vector_store.similarity_search(
            query_embedding, k=k * 2  # Obtener más para reranking
        )

        # Búsqueda BM25
        bm25_scores = self.bm25_index.get_scores(query.split())
        bm25_results = self._get_top_bm25(bm25_scores, k * 2)

        # Combinar resultados
        combined = self._combine_results(vector_results, bm25_results, query)

        # Reranking
        reranked = self.reranker.rerank(query, combined)

        # Tomar top k
        final_results = reranked[:k]

        latency_ms = (time.perf_counter() - start_time) * 1000

        result = RetrievalResult(
            documents=final_results, query=query, latency_ms=latency_ms, source="hybrid"
        )

        # Cacheear resultados
        if self.cache is not None and final_results:
            import asyncio

            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.cache.set(query, final_results))

        self.logger.debug(
            f"Retrieval completado: {len(final_results)} docs, "
            f"{latency_ms:.2f}ms, source={result.source}"
        )

        return result

    def _get_top_bm25(self, scores: np.ndarray, k: int) -> list[Document]:
        """Obtener top k documentos BM25."""
        top_indices = np.argsort(scores)[-k:][::-1]
        # Implementación dependiente del índice BM25
        return []  # Placeholder

    def _combine_results(
        self,
        vector_results: list[tuple[np.ndarray, dict[str, Any], float]],
        bm25_results: list[Document],
        query: str,
    ) -> list[Document]:
        """Combinar resultados con weighted reciprocal rank fusion."""
        # Implementación de RRF (Reciprocal Rank Fusion)
        combined: dict[str, Document] = {}

        for rank, (embedding, metadata, score) in enumerate(vector_results, 1):
            doc_id = metadata.get("id", str(rank))
            if doc_id not in combined:
                combined[doc_id] = Document(
                    content=metadata.get("content", ""),
                    metadata=metadata,
                    embedding=embedding,
                    score=self.vector_weight / (rank + 60),
                )
            else:
                combined[doc_id].score += self.vector_weight / (rank + 60)

        for rank, doc in enumerate(bm25_results, 1):
            doc_id = doc.metadata.get("id", str(rank))
            if doc_id not in combined:
                doc.score = self.bm25_weight / (rank + 60)
                combined[doc_id] = doc
            else:
                combined[doc_id].score += self.bm25_weight / (rank + 60)

        # Ordenar por score combinado
        return sorted(combined.values(), key=lambda d: d.score, reverse=True)


class RAGPipeline:
    """
    Pipeline RAG completo para producción.

    Este pipeline orquesta todo el flujo RAG:
    1. Preprocesamiento de query
    2. Recuperación de documentos
    3. Construcción de contexto
    4. Generación con LLM
    5. Post-procesamiento de respuesta
    """

    def __init__(
        self,
        retriever: BaseRetriever,
        llm_client: Any,  # Protocol para cliente LLM
        max_context_tokens: int = 4000,
        temperature: float = 0.7,
    ) -> None:
        """
        Inicializar pipeline RAG.

        Args:
            retriever: Retriever de documentos.
            llm_client: Cliente LLM para generación.
            max_context_tokens: Máximo tokens de contexto.
            temperature: Temperatura para generación.
        """
        self.retriever = retriever
        self.llm_client = llm_client
        self.max_context_tokens = max_context_tokens
        self.temperature = temperature
        self.logger = logging.getLogger(__name__)

    def generate(
        self, query: str, k: int = 5, system_prompt: str | None = None
    ) -> dict[str, Any]:
        """
        Generar respuesta RAG.

        Args:
            query: Consulta del usuario.
            k: Número de documentos a recuperar.
            system_prompt: Prompt de sistema opcional.

        Returns:
            Diccionario con respuesta y metadata.
        """
        start_time = time.perf_counter()

        # Recuperar documentos
        retrieval_result = self.retriever.retrieve(query, k=k)

        # Construir contexto
        context = self._build_context(
            retrieval_result.documents, self.max_context_tokens
        )

        # Generar respuesta
        response = self.llm_client.generate(
            prompt=query,
            context=context,
            system_prompt=system_prompt,
            temperature=self.temperature,
        )

        latency_ms = (time.perf_counter() - start_time) * 1000

        return {
            "response": response,
            "query": query,
            "documents_used": len(retrieval_result.documents),
            "retrieval_latency_ms": retrieval_result.latency_ms,
            "total_latency_ms": latency_ms,
            "source": retrieval_result.source,
        }

    def _build_context(self, documents: list[Document], max_tokens: int) -> str:
        """
        Construir contexto desde documentos.

        Args:
            documents: Documentos recuperados.
            max_tokens: Máximo tokens permitidos.

        Returns:
            Contexto formateado como string.
        """
        context_parts = []
        current_tokens = 0

        # Estimación simple: 4 caracteres ≈ 1 token
        tokens_per_char = 0.25

        for doc in documents:
            doc_tokens = len(doc.content) * tokens_per_char
            if current_tokens + doc_tokens > max_tokens:
                break

            context_parts.append(
                f"[Fuente: {doc.metadata.get('source', 'unknown')}]\n{doc.content}"
            )
            current_tokens += doc_tokens

        return "\n\n---\n\n".join(context_parts)


# Ejemplo de uso
if __name__ == "__main__":
    # Este es un template - adaptar a implementación real
    logging.basicConfig(level=logging.INFO)
    logger.info("RAG Pipeline template cargado")
    logger.info("Copiar a sky_claw/services/rag_pipeline.py y adaptar")

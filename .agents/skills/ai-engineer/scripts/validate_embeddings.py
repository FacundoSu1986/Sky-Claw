#!/usr/bin/env python3
"""
Validación de calidad de embeddings para sistemas RAG.

Este script analiza la distribución de embeddings, detecta outliers
y valida la consistencia semántica de los vectores almacenados.

Uso:
    python scripts/validate_embeddings.py --config config.toml

Autor: Sky-Claw AI Engineer Skill
Versión: 2026.03
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import spatial
import tomli

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/embedding_validation.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


@dataclass
class EmbeddingStats:
    """Estadísticas de calidad de embeddings."""

    dimension: int
    count: int
    mean_magnitude: float
    std_magnitude: float
    outlier_count: int
    duplicate_count: int
    avg_similarity: float


class EmbeddingValidator:
    """
    Validador de calidad de embeddings.

    Attributes:
        threshold_outlier: Umbral para detectar outliers (desviaciones estándar).
        threshold_duplicate: Umbral de similitud para considerar duplicados.
    """

    def __init__(
        self, threshold_outlier: float = 3.0, threshold_duplicate: float = 0.99
    ) -> None:
        """
        Inicializar el validador.

        Args:
            threshold_outlier: Número de desviaciones estándar para outliers.
            threshold_duplicate: Similitud coseno para considerar duplicados.
        """
        self.threshold_outlier = threshold_outlier
        self.threshold_duplicate = threshold_duplicate
        self.logger = logging.getLogger(__name__)

    def validate_embeddings(
        self, embeddings: np.ndarray, metadata: list[dict[str, Any]] | None = None
    ) -> EmbeddingStats:
        """
        Validar calidad de un conjunto de embeddings.

        Args:
            embeddings: Array numpy de shape (n_samples, n_dimensions).
            metadata: Metadata opcional asociada a cada embedding.

        Returns:
            EmbeddingStats con estadísticas de calidad.

        Raises:
            ValueError: Si los embeddings están vacíos o mal formados.
        """
        if embeddings.size == 0:
            raise ValueError("No se proporcionaron embeddings para validar")

        if embeddings.ndim != 2:
            raise ValueError(f"Embeddings deben ser 2D, got {embeddings.ndim}D")

        n_samples, n_dimensions = embeddings.shape
        self.logger.debug(
            f"Validando {n_samples} embeddings de {n_dimensions} dimensiones"
        )

        # Calcular magnitudes
        magnitudes = np.linalg.norm(embeddings, axis=1)
        mean_mag = float(np.mean(magnitudes))
        std_mag = float(np.std(magnitudes))

        # Detectar outliers
        outlier_mask = np.abs(magnitudes - mean_mag) > (
            self.threshold_outlier * std_mag
        )
        outlier_count = int(np.sum(outlier_mask))

        if outlier_count > 0:
            self.logger.warning(f"Detectados {outlier_count} outliers en embeddings")

        # Detectar duplicados (similitud coseno > threshold)
        duplicate_count = self._count_duplicates(embeddings)

        # Calcular similitud promedio
        avg_similarity = self._calculate_avg_similarity(embeddings, sample_size=1000)

        return EmbeddingStats(
            dimension=n_dimensions,
            count=n_samples,
            mean_magnitude=mean_mag,
            std_magnitude=std_mag,
            outlier_count=outlier_count,
            duplicate_count=duplicate_count,
            avg_similarity=avg_similarity,
        )

    def _count_duplicates(self, embeddings: np.ndarray, sample_size: int = 500) -> int:
        """
        Contar embeddings duplicados o casi duplicados.

        Args:
            embeddings: Array de embeddings.
            sample_size: Tamaño de muestra para cálculo (performance).

        Returns:
            Número de pares duplicados detectados.
        """
        n_samples = embeddings.shape[0]
        if n_samples > sample_size:
            indices = np.random.choice(n_samples, sample_size, replace=False)
            sample = embeddings[indices]
        else:
            sample = embeddings

        duplicate_count = 0
        for i in range(len(sample)):
            for j in range(i + 1, len(sample)):
                similarity = 1 - spatial.distance.cosine(sample[i], sample[j])
                if similarity > self.threshold_duplicate:
                    duplicate_count += 1

        # Escalar al tamaño total
        if n_samples > sample_size:
            scale_factor = (n_samples * (n_samples - 1)) / (
                sample_size * (sample_size - 1)
            )
            duplicate_count = int(duplicate_count * scale_factor)

        return duplicate_count

    def _calculate_avg_similarity(
        self, embeddings: np.ndarray, sample_size: int = 1000
    ) -> float:
        """
        Calcular similitud coseno promedio entre embeddings.

        Args:
            embeddings: Array de embeddings.
            sample_size: Tamaño de muestra para cálculo.

        Returns:
            Similitud promedio.
        """
        n_samples = embeddings.shape[0]
        if n_samples <= 1:
            return 0.0

        if n_samples > sample_size:
            indices = np.random.choice(n_samples, sample_size, replace=False)
            sample = embeddings[indices]
        else:
            sample = embeddings

        similarities = []
        for i in range(min(100, len(sample))):
            for j in range(i + 1, min(100, len(sample))):
                sim = 1 - spatial.distance.cosine(sample[i], sample[j])
                similarities.append(sim)

        return float(np.mean(similarities)) if similarities else 0.0

    def generate_report(self, stats: EmbeddingStats) -> str:
        """
        Generar reporte legible de validación.

        Args:
            stats: Estadísticas de embeddings.

        Returns:
            Reporte formateado como string.
        """
        report = [
            "=" * 60,
            "REPORTE DE VALIDACIÓN DE EMBEDDINGS",
            "=" * 60,
            f"Dimensiones: {stats.dimension}",
            f"Total embeddings: {stats.count}",
            f"Magnitud media: {stats.mean_magnitude:.4f} (±{stats.std_magnitude:.4f})",
            f"Outliers detectados: {stats.outlier_count} ({stats.outlier_count/max(stats.count,1)*100:.2f}%)",
            f"Duplicados estimados: {stats.duplicate_count}",
            f"Similitud promedio: {stats.avg_similarity:.4f}",
            "=" * 60,
        ]

        # Evaluación de calidad
        quality_score = self._calculate_quality_score(stats)
        report.append(f"Puntuación de calidad: {quality_score}/100")

        if quality_score >= 80:
            report.append("Estado: ✅ PASSED")
        elif quality_score >= 60:
            report.append("Estado: ⚠️ WARNING - Revisar outliers/duplicados")
        else:
            report.append("Estado: ❌ FAILED - Requiere atención inmediata")

        return "\n".join(report)

    def _calculate_quality_score(self, stats: EmbeddingStats) -> int:
        """
        Calcular puntuación de calidad (0-100).

        Args:
            stats: Estadísticas de embeddings.

        Returns:
            Puntuación de calidad.
        """
        score = 100

        # Penalizar outliers
        outlier_ratio = stats.outlier_count / max(stats.count, 1)
        score -= min(30, int(outlier_ratio * 100))

        # Penalizar duplicados
        duplicate_ratio = stats.duplicate_count / max(
            stats.count * (stats.count - 1) / 2, 1
        )
        score -= min(30, int(duplicate_ratio * 100))

        # Penalizar similitud muy alta (poca diversidad)
        if stats.avg_similarity > 0.8:
            score -= min(20, int((stats.avg_similarity - 0.8) * 100))

        return max(0, score)


def load_embeddings_from_file(file_path: Path) -> np.ndarray:
    """
    Cargar embeddings desde archivo numpy o texto.

    Args:
        file_path: Ruta al archivo de embeddings.

    Returns:
        Array numpy de embeddings.

    Raises:
        FileNotFoundError: Si el archivo no existe.
        ValueError: Si el formato no es soportado.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Archivo no encontrado: {file_path}")

    if file_path.suffix == ".npy":
        return np.load(file_path)
    elif file_path.suffix in [".txt", ".csv"]:
        return np.loadtxt(file_path, delimiter=",")
    else:
        raise ValueError(f"Formato no soportado: {file_path.suffix}")


def main() -> int:
    """
    Punto de entrada principal.

    Returns:
        Código de salida (0=éxito, 1=error).
    """
    parser = argparse.ArgumentParser(
        description="Validar calidad de embeddings para sistemas RAG"
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Ruta al archivo de embeddings (.npy, .txt, .csv)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Ruta al archivo de configuración",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/validation_report.txt"),
        help="Ruta para guardar el reporte",
    )
    parser.add_argument(
        "--threshold-outlier",
        type=float,
        default=3.0,
        help="Umbral de outlier (desviaciones estándar)",
    )
    parser.add_argument(
        "--threshold-duplicate",
        type=float,
        default=0.99,
        help="Umbral de similitud para duplicados",
    )

    args = parser.parse_args()

    try:
        # Cargar configuración si existe
        config: dict[str, Any] = {}
        if args.config.exists():
            with open(args.config, "rb") as f:
                config = tomli.load(f)
            logger.info(f"Configuración cargada desde {args.config}")

        # Cargar embeddings
        logger.info(f"Cargando embeddings desde {args.input}")
        embeddings = load_embeddings_from_file(args.input)
        logger.info(f"Loaded {embeddings.shape[0]} embeddings")

        # Validar
        validator = EmbeddingValidator(
            threshold_outlier=args.threshold_outlier,
            threshold_duplicate=args.threshold_duplicate,
        )
        stats = validator.validate_embeddings(embeddings)

        # Generar reporte
        report = validator.generate_report(stats)
        print(report)

        # Guardar reporte
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"Reporte guardado en {args.output}")

        # Retornar código según calidad
        quality_score = validator._calculate_quality_score(stats)
        return 0 if quality_score >= 60 else 1

    except FileNotFoundError as e:
        logger.error(f"Archivo no encontrado: {e}")
        return 1
    except ValueError as e:
        logger.error(f"Error de validación: {e}")
        return 1
    except Exception as e:
        logger.exception(f"Error inesperado: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

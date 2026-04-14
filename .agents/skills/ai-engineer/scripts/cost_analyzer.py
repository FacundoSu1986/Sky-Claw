#!/usr/bin/env python3
"""
Analizador de costos para APIs de LLM.

Este script analiza logs de llamadas a modelos y calcula
costos estimados por endpoint, modelo y usuario.

Uso:
    python scripts/cost_analyzer.py --logs logs/llm_calls.jsonl

Autor: Sky-Claw AI Engineer Skill
Versión: 2026.03
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import tomli

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/cost_analysis.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# Precios por modelo (USD por 1K tokens) - Actualizable desde config
MODEL_PRICES: dict[str, dict[str, float]] = {
    "openai": {
        "gpt-4o": {"input": 0.005, "output": 0.015},
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
        "o1-preview": {"input": 0.015, "output": 0.06},
        "o1-mini": {"input": 0.003, "output": 0.012},
        "text-embedding-3-large": {"input": 0.00013, "output": 0.0},
    },
    "anthropic": {
        "claude-4-5-sonnet": {"input": 0.003, "output": 0.015},
        "claude-4-5-haiku": {"input": 0.0008, "output": 0.004},
        "claude-4-1-opus": {"input": 0.015, "output": 0.075},
    },
    "deepseek": {
        "deepseek-chat": {"input": 0.00027, "output": 0.0011},
        "deepseek-coder": {"input": 0.00027, "output": 0.0011},
    },
    "ollama": {
        "llama-3.1-8b": {"input": 0.0, "output": 0.0},  # Local = gratis
        "llama-3.1-70b": {"input": 0.0, "output": 0.0},
        "mixtral-8x7b": {"input": 0.0, "output": 0.0},
    },
}


@dataclass
class CallRecord:
    """Registro de una llamada a LLM."""

    timestamp: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    endpoint: str
    user_id: str | None = None
    latency_ms: int | None = None
    success: bool = True


@dataclass
class CostSummary:
    """Resumen de costos."""

    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_calls: int
    cost_by_model: dict[str, float]
    cost_by_endpoint: dict[str, float]
    cost_by_day: dict[str, float]
    average_latency_ms: float | None
    success_rate: float


class CostAnalyzer:
    """
    Analizador de costos para llamadas a LLM.

    Attributes:
        model_prices: Diccionario de precios por modelo.
    """

    def __init__(self, model_prices: dict[str, dict[str, float]] | None = None) -> None:
        """
        Inicializar el analizador.

        Args:
            model_prices: Precios personalizados (sobrescribe defaults).
        """
        self.model_prices = model_prices or MODEL_PRICES
        self.logger = logging.getLogger(__name__)

    def calculate_cost(
        self, provider: str, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """
        Calcular costo de una llamada.

        Args:
            provider: Proveedor (openai, anthropic, etc.).
            model: Nombre del modelo.
            input_tokens: Tokens de entrada.
            output_tokens: Tokens de salida.

        Returns:
            Costo en USD.
        """
        prices = self.model_prices.get(provider, {}).get(model)

        if prices is None:
            self.logger.warning(f"Modelo no encontrado: {provider}/{model}")
            return 0.0

        input_cost = (input_tokens / 1000) * prices["input"]
        output_cost = (output_tokens / 1000) * prices["output"]

        return round(input_cost + output_cost, 6)

    def parse_log_line(self, line: str) -> CallRecord | None:
        """
        Parsear una línea de log JSON.

        Args:
            line: Línea de log en formato JSON.

        Returns:
            CallRecord o None si hay error.
        """
        try:
            data = json.loads(line.strip())
            return CallRecord(
                timestamp=data.get("timestamp", ""),
                provider=data.get("provider", "unknown"),
                model=data.get("model", "unknown"),
                input_tokens=data.get("input_tokens", 0),
                output_tokens=data.get("output_tokens", 0),
                endpoint=data.get("endpoint", "/"),
                user_id=data.get("user_id"),
                latency_ms=data.get("latency_ms"),
                success=data.get("success", True),
            )
        except json.JSONDecodeError as e:
            self.logger.debug(f"Error parseando línea: {e}")
            return None

    def analyze_logs(self, log_file: Path) -> CostSummary:
        """
        Analizar archivo de logs y generar resumen.

        Args:
            log_file: Ruta al archivo de logs (JSONL).

        Returns:
            CostSummary con estadísticas completas.

        Raises:
            FileNotFoundError: Si el archivo no existe.
        """
        if not log_file.exists():
            raise FileNotFoundError(f"Archivo de logs no encontrado: {log_file}")

        calls: list[CallRecord] = []
        cost_by_model: dict[str, float] = {}
        cost_by_endpoint: dict[str, float] = {}
        cost_by_day: dict[str, float] = {}
        latencies: list[int] = []
        success_count = 0

        self.logger.info(f"Analizando logs desde {log_file}")

        with open(log_file, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue

                record = self.parse_log_line(line)
                if record is None:
                    continue

                calls.append(record)

                # Calcular costo
                cost = self.calculate_cost(
                    record.provider,
                    record.model,
                    record.input_tokens,
                    record.output_tokens,
                )

                # Acumular por modelo
                model_key = f"{record.provider}/{record.model}"
                cost_by_model[model_key] = cost_by_model.get(model_key, 0.0) + cost

                # Acumular por endpoint
                cost_by_endpoint[record.endpoint] = (
                    cost_by_endpoint.get(record.endpoint, 0.0) + cost
                )

                # Acumular por día
                day = record.timestamp[:10] if record.timestamp else "unknown"
                cost_by_day[day] = cost_by_day.get(day, 0.0) + cost

                # Latencias
                if record.latency_ms is not None:
                    latencies.append(record.latency_ms)

                # Éxitos
                if record.success:
                    success_count += 1

        # Calcular totales
        total_cost = sum(cost_by_model.values())
        total_input = sum(c.input_tokens for c in calls)
        total_output = sum(c.output_tokens for c in calls)
        avg_latency = sum(latencies) / len(latencies) if latencies else None
        success_rate = success_count / len(calls) if calls else 0.0

        self.logger.info(
            f"Analizadas {len(calls)} llamadas, costo total: ${total_cost:.4f}"
        )

        return CostSummary(
            total_cost_usd=round(total_cost, 4),
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_calls=len(calls),
            cost_by_model={k: round(v, 4) for k, v in cost_by_model.items()},
            cost_by_endpoint={k: round(v, 4) for k, v in cost_by_endpoint.items()},
            cost_by_day={k: round(v, 4) for k, v in cost_by_day.items()},
            average_latency_ms=round(avg_latency, 2) if avg_latency else None,
            success_rate=round(success_rate, 4),
        )

    def generate_report(self, summary: CostSummary) -> str:
        """
        Generar reporte legible de costos.

        Args:
            summary: Resumen de costos.

        Returns:
            Reporte formateado como string.
        """
        report = [
            "=" * 70,
            "REPORTE DE ANÁLISIS DE COSTOS - LLM API",
            "=" * 70,
            f"Fecha de generación: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "📊 RESUMEN GENERAL",
            "-" * 70,
            f"Total de llamadas: {summary.total_calls:,}",
            f"Costo total: ${summary.total_cost_usd:.4f} USD",
            f"Tokens de entrada: {summary.total_input_tokens:,}",
            f"Tokens de salida: {summary.total_output_tokens:,}",
            f"Tasa de éxito: {summary.success_rate * 100:.2f}%",
            (
                f"Latencia promedio: {summary.average_latency_ms:.2f} ms"
                if summary.average_latency_ms
                else "Latencia promedio: N/A"
            ),
            "",
            "💰 COSTO POR MODELO",
            "-" * 70,
        ]

        # Ordenar por costo descendente
        sorted_models = sorted(
            summary.cost_by_model.items(), key=lambda x: x[1], reverse=True
        )
        for model, cost in sorted_models[:10]:  # Top 10
            percentage = (
                (cost / summary.total_cost_usd * 100)
                if summary.total_cost_usd > 0
                else 0
            )
            report.append(f"  {model:40s} ${cost:>8.4f} ({percentage:>5.1f}%)")

        if len(sorted_models) > 10:
            report.append(f"  ... y {len(sorted_models) - 10} modelos más")

        report.extend(
            [
                "",
                "🔗 COSTO POR ENDPOINT",
                "-" * 70,
            ]
        )

        sorted_endpoints = sorted(
            summary.cost_by_endpoint.items(), key=lambda x: x[1], reverse=True
        )
        for endpoint, cost in sorted_endpoints[:10]:
            percentage = (
                (cost / summary.total_cost_usd * 100)
                if summary.total_cost_usd > 0
                else 0
            )
            report.append(f"  {endpoint:40s} ${cost:>8.4f} ({percentage:>5.1f}%)")

        report.extend(
            [
                "",
                "📈 COSTO POR DÍA (Últimos 7 días)",
                "-" * 70,
            ]
        )

        sorted_days = sorted(summary.cost_by_day.items(), reverse=True)[:7]
        for day, cost in sorted_days:
            report.append(f"  {day:20s} ${cost:>8.4f}")

        report.extend(
            [
                "",
                "⚠️ RECOMENDACIONES",
                "-" * 70,
            ]
        )

        # Generar recomendaciones
        recommendations = self._generate_recommendations(summary)
        for i, rec in enumerate(recommendations, 1):
            report.append(f"  {i}. {rec}")

        report.extend(
            [
                "",
                "=" * 70,
            ]
        )

        return "\n".join(report)

    def _generate_recommendations(self, summary: CostSummary) -> list[str]:
        """
        Generar recomendaciones basadas en el análisis.

        Args:
            summary: Resumen de costos.

        Returns:
            Lista de recomendaciones.
        """
        recommendations = []

        # Verificar tasa de éxito
        if summary.success_rate < 0.95:
            recommendations.append(
                f"Tasa de éxito baja ({summary.success_rate * 100:.1f}%). "
                "Revisar errores y implementar retry logic."
            )

        # Verificar modelos caros
        expensive_models = [
            m
            for m, c in summary.cost_by_model.items()
            if c > summary.total_cost_usd * 0.3
        ]
        if expensive_models:
            recommendations.append(
                f"Modelo(s) {', '.join(expensive_models)} representa(n) >30% del costo. "
                "Considerar modelos más económicos para casos simples."
            )

        # Verificar latencia
        if summary.average_latency_ms and summary.average_latency_ms > 2000:
            recommendations.append(
                f"Latencia promedio alta ({summary.average_latency_ms:.0f}ms). "
                "Considerar caching semántico o modelos más rápidos."
            )

        # Verificar ratio output/input
        if summary.total_input_tokens > 0:
            ratio = summary.total_output_tokens / summary.total_input_tokens
            if ratio > 2:
                recommendations.append(
                    f"Ratio output/input alto ({ratio:.2f}). "
                    "Revisar prompts para reducir verbosity del modelo."
                )

        if not recommendations:
            recommendations.append(
                "Sin recomendaciones críticas. El sistema está optimizado."
            )

        return recommendations


def main() -> int:
    """
    Punto de entrada principal.

    Returns:
        Código de salida (0=éxito, 1=error).
    """
    parser = argparse.ArgumentParser(
        description="Analizar costos de llamadas a APIs de LLM"
    )
    parser.add_argument(
        "--logs", type=Path, required=True, help="Ruta al archivo de logs (JSONL)"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Ruta al archivo de configuración con precios personalizados",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/cost_report.txt"),
        help="Ruta para guardar el reporte",
    )

    args = parser.parse_args()

    try:
        # Cargar precios personalizados si existen
        model_prices = MODEL_PRICES
        if args.config.exists():
            with open(args.config, "rb") as f:
                config = tomli.load(f)
                if "llm_prices" in config:
                    model_prices = config["llm_prices"]
                    logger.info("Precios personalizados cargados desde config")

        # Analizar
        analyzer = CostAnalyzer(model_prices=model_prices)
        summary = analyzer.analyze_logs(args.logs)

        # Generar reporte
        report = analyzer.generate_report(summary)
        print(report)

        # Guardar reporte
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"Reporte guardado en {args.output}")

        # Guardar resumen JSON para integración
        json_output = args.output.with_suffix(".json")
        with open(json_output, "w", encoding="utf-8") as f:
            json.dump(asdict(summary), f, indent=2)
        logger.info(f"Resumen JSON guardado en {json_output}")

        return 0

    except FileNotFoundError as e:
        logger.error(f"Archivo no encontrado: {e}")
        return 1
    except Exception as e:
        logger.exception(f"Error inesperado: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

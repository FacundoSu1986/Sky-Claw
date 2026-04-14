#!/usr/bin/env python3
"""
Auditoría de seguridad para sistemas de IA.

Este script verifica configuraciones de seguridad, detecta
vulnerabilidades comunes y valida compliance con políticas.

Uso:
    python scripts/security_audit.py --config config.toml

Autor: Sky-Claw AI Engineer Skill
Versión: 2026.03
"""

import argparse
import logging
import re
import sys
from dataclasses import dataclass
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
        logging.FileHandler("logs/security_audit.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


@dataclass
class SecurityFinding:
    """Hallazgo de seguridad."""

    severity: str  # CRITICAL, HIGH, MEDIUM, LOW, INFO
    category: str
    description: str
    recommendation: str
    location: str | None = None


@dataclass
class AuditReport:
    """Reporte de auditoría."""

    timestamp: str
    total_checks: int
    passed: int
    failed: int
    warnings: int
    findings: list[SecurityFinding]
    compliance_score: float


class SecurityAuditor:
    """
    Auditor de seguridad para sistemas de IA.

    Attributes:
        config: Configuración del sistema a auditar.
    """

    # Patrones de datos sensibles
    SENSITIVE_PATTERNS = {
        "api_key": re.compile(
            r"(?i)(api[_-]?key|apikey)\s*[=:]\s*['\"][a-zA-Z0-9]{20,}['\"]"
        ),
        "password": re.compile(
            r"(?i)(password|passwd|pwd)\s*[=:]\s*['\"][^'\"]{8,}['\"]"
        ),
        "token": re.compile(
            r"(?i)(token|auth_token|access_token)\s*[=:]\s*['\"][a-zA-Z0-9]{20,}['\"]"
        ),
        "secret": re.compile(
            r"(?i)(secret|private_key)\s*[=:]\s*['\"][a-zA-Z0-9]{20,}['\"]"
        ),
        "aws_key": re.compile(r"(?i)AKIA[0-9A-Z]{16}"),
        "github_token": re.compile(r"(?i)ghp_[a-zA-Z0-9]{36}"),
    }

    # Patrones de prompt injection
    INJECTION_PATTERNS = [
        re.compile(
            r"(?i)ignore\s+(previous|all)\s+(instructions|rules)", re.IGNORECASE
        ),
        re.compile(r"(?i)bypass\s+(security|filters|restrictions)", re.IGNORECASE),
        re.compile(r"(?i)(system|developer)\s+message\s*:", re.IGNORECASE),
        re.compile(
            r"(?i)you\s+are\s+now\s+(in|a)\s+(developer|admin|unrestricted)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?i)print\s+(the|your)\s+(system|initial)\s+prompt", re.IGNORECASE
        ),
    ]

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """
        Inicializar el auditor.

        Args:
            config: Configuración del sistema a auditar.
        """
        self.config = config or {}
        self.logger = logging.getLogger(__name__)
        self.findings: list[SecurityFinding] = []

    def audit_config_file(self, config_path: Path) -> AuditReport:
        """
        Auditar archivo de configuración.

        Args:
            config_path: Ruta al archivo de configuración.

        Returns:
            AuditReport con hallazgos.
        """
        self.logger.info(f"Auditando configuración: {config_path}")

        if not config_path.exists():
            self.findings.append(
                SecurityFinding(
                    severity="CRITICAL",
                    category="Configuration",
                    description="Archivo de configuración no encontrado",
                    recommendation="Crear archivo de configuración con valores seguros",
                    location=str(config_path),
                )
            )
            return self._generate_report(0)

        try:
            with open(config_path, "rb") as f:
                config = tomli.load(f)
            self.config = config
        except Exception as e:
            self.logger.error(f"Error leyendo configuración: {e}")
            self.findings.append(
                SecurityFinding(
                    severity="CRITICAL",
                    category="Configuration",
                    description=f"Error leyendo configuración: {e}",
                    recommendation="Verificar formato TOML válido",
                    location=str(config_path),
                )
            )
            return self._generate_report(0)

        # Ejecutar checks
        checks = [
            self._check_api_keys_exposed,
            self._check_https_enforcement,
            self._check_rate_limiting,
            self._check_model_restrictions,
            self._check_hitl_enabled,
            self._check_network_gateway,
            self._check_logging_security,
            self._check_encryption,
        ]

        passed = 0
        failed = 0
        warnings = 0

        for check in checks:
            try:
                result = check()
                if result == "PASS":
                    passed += 1
                elif result == "FAIL":
                    failed += 1
                else:
                    warnings += 1
            except Exception as e:
                self.logger.exception(f"Error en check {check.__name__}: {e}")
                failed += 1

        return self._generate_report(passed + failed + warnings)

    def _check_api_keys_exposed(self) -> str:
        """Verificar que no haya API keys hardcodeadas."""
        self.logger.debug("Check: API keys expuestas")

        config_str = str(self.config)
        for pattern_name, pattern in self.SENSITIVE_PATTERNS.items():
            matches = pattern.findall(config_str)
            if matches:
                self.findings.append(
                    SecurityFinding(
                        severity="CRITICAL",
                        category="Secrets Management",
                        description=f"{pattern_name} detectado en configuración",
                        recommendation="Usar variables de entorno o secret manager",
                        location="config.toml",
                    )
                )
                return "FAIL"

        return "PASS"

    def _check_https_enforcement(self) -> str:
        """Verificar que las URLs usen HTTPS."""
        self.logger.debug("Check: HTTPS enforcement")

        urls = self._extract_urls(self.config)
        http_urls = [u for u in urls if u.startswith("http://")]

        if http_urls:
            self.findings.append(
                SecurityFinding(
                    severity="HIGH",
                    category="Transport Security",
                    description=f"URLs HTTP detectadas (deben ser HTTPS): {http_urls[:3]}",
                    recommendation="Cambiar todas las URLs a HTTPS",
                )
            )
            return "FAIL"

        return "PASS"

    def _check_rate_limiting(self) -> str:
        """Verificar configuración de rate limiting."""
        self.logger.debug("Check: Rate limiting")

        llm_config = self.config.get("llm", {})

        if not llm_config.get("rate_limit"):
            self.findings.append(
                SecurityFinding(
                    severity="MEDIUM",
                    category="Rate Limiting",
                    description="Rate limiting no configurado para LLM",
                    recommendation="Configurar rate_limit para prevenir abuso y controlar costos",
                )
            )
            return "WARNING"

        if llm_config.get("rate_limit", {}).get("requests_per_minute", 0) > 100:
            self.findings.append(
                SecurityFinding(
                    severity="LOW",
                    category="Rate Limiting",
                    description="Rate limit muy alto (>100 req/min)",
                    recommendation="Considerar reducir para proteger contra abuso",
                )
            )
            return "WARNING"

        return "PASS"

    def _check_model_restrictions(self) -> str:
        """Verificar restricciones de modelos permitidos."""
        self.logger.debug("Check: Restricciones de modelos")

        allowed_models = self.config.get("llm", {}).get("allowed_models", [])

        if not allowed_models:
            self.findings.append(
                SecurityFinding(
                    severity="MEDIUM",
                    category="Model Governance",
                    description="No hay lista de modelos permitidos",
                    recommendation="Definir allowed_models para controlar qué modelos pueden usarse",
                )
            )
            return "WARNING"

        # Verificar modelos potencialmente problemáticos
        unrestricted_models = [
            "gpt-4",
            "claude-3-opus",
        ]  # Modelos caros/sin restricciones
        for model in allowed_models:
            if any(um in model.lower() for um in unrestricted_models):
                self.findings.append(
                    SecurityFinding(
                        severity="INFO",
                        category="Model Governance",
                        description=f"Modelo de alto costo permitido: {model}",
                        recommendation="Considerar requerir aprobación para modelos premium",
                    )
                )

        return "PASS"

    def _check_hitl_enabled(self) -> str:
        """Verificar que HITL esté habilitado para operaciones críticas."""
        self.logger.debug("Check: HITL enabled")

        hitl_config = self.config.get("hitl", {})

        if not hitl_config.get("enabled", False):
            self.findings.append(
                SecurityFinding(
                    severity="MEDIUM",
                    category="Human-in-the-Loop",
                    description="HITL no está habilitado",
                    recommendation="Habilitar HITL para descargas externas y operaciones críticas",
                )
            )
            return "WARNING"

        required_approvals = hitl_config.get("require_approval_for", [])
        critical_ops = ["external_download", "model_change", "config_update"]
        missing = [op for op in critical_ops if op not in required_approvals]

        if missing:
            self.findings.append(
                SecurityFinding(
                    severity="LOW",
                    category="Human-in-the-Loop",
                    description=f"Operaciones críticas sin aprobación HITL: {missing}",
                    recommendation=f"Añadir {missing} a require_approval_for",
                )
            )
            return "WARNING"

        return "PASS"

    def _check_network_gateway(self) -> str:
        """Verificar configuración de NetworkGateway."""
        self.logger.debug("Check: Network Gateway")

        gateway_config = self.config.get("network_gateway", {})

        if not gateway_config.get("enabled", False):
            self.findings.append(
                SecurityFinding(
                    severity="MEDIUM",
                    category="Network Security",
                    description="NetworkGateway no está habilitado",
                    recommendation="Habilitar para restringir dominios autorizados",
                )
            )
            return "WARNING"

        allowed_domains = gateway_config.get("allowed_domains", [])
        if not allowed_domains:
            self.findings.append(
                SecurityFinding(
                    severity="HIGH",
                    category="Network Security",
                    description="NetworkGateway habilitado pero sin dominios permitidos",
                    recommendation="Definir allowed_domains explícitamente",
                )
            )
            return "FAIL"

        # Verificar dominios demasiado permisivos
        wildcard_domains = [d for d in allowed_domains if d.startswith("*.")]
        if len(wildcard_domains) > 3:
            self.findings.append(
                SecurityFinding(
                    severity="LOW",
                    category="Network Security",
                    description=f"Muchos dominios wildcard: {wildcard_domains}",
                    recommendation="Reducir dominios wildcard a lo esencial",
                )
            )
            return "WARNING"

        return "PASS"

    def _check_logging_security(self) -> str:
        """Verificar configuración segura de logging."""
        self.logger.debug("Check: Logging security")

        logging_config = self.config.get("logging", {})

        # Verificar que no se loggeen datos sensibles
        if logging_config.get("log_request_bodies", False):
            self.findings.append(
                SecurityFinding(
                    severity="MEDIUM",
                    category="Logging Security",
                    description="Loggeo de cuerpos de request habilitado",
                    recommendation="Desactivar o implementar redacción de datos sensibles",
                )
            )
            return "WARNING"

        # Verificar rotación de logs
        if not logging_config.get("rotation", {}).get("enabled", False):
            self.findings.append(
                SecurityFinding(
                    severity="LOW",
                    category="Logging Security",
                    description="Rotación de logs no configurada",
                    recommendation="Habilitar rotación para evitar llenado de disco",
                )
            )
            return "WARNING"

        return "PASS"

    def _check_encryption(self) -> str:
        """Verificar configuración de encriptación."""
        self.logger.debug("Check: Encryption")

        db_config = self.config.get("database", {})

        if db_config.get("type", "").lower() == "sqlite":
            # SQLite no tiene encriptación nativa
            if not db_config.get("encryption", {}).get("enabled", False):
                self.findings.append(
                    SecurityFinding(
                        severity="INFO",
                        category="Data Encryption",
                        description="SQLite sin encriptación habilitada",
                        recommendation="Considerar SQLCipher para datos sensibles",
                    )
                )
                return "WARNING"

        return "PASS"

    def _extract_urls(
        self, config: dict[str, Any], urls: list[str] | None = None
    ) -> list[str]:
        """Extraer todas las URLs de la configuración."""
        if urls is None:
            urls = []

        for key, value in config.items():
            if isinstance(value, str):
                if value.startswith("http://") or value.startswith("https://"):
                    urls.append(value)
            elif isinstance(value, dict):
                self._extract_urls(value, urls)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self._extract_urls(item, urls)

        return urls

    def _generate_report(self, total_checks: int) -> AuditReport:
        """
        Generar reporte de auditoría.

        Args:
            total_checks: Número total de checks ejecutados.

        Returns:
            AuditReport completo.
        """
        passed = sum(1 for f in self.findings if f.severity == "PASS")
        failed = sum(
            1 for f in self.findings if f.severity in ["CRITICAL", "HIGH", "FAIL"]
        )
        warnings = sum(
            1 for f in self.findings if f.severity in ["MEDIUM", "LOW", "WARNING"]
        )

        # Calcular score de compliance
        severity_weights = {
            "CRITICAL": 25,
            "HIGH": 15,
            "MEDIUM": 8,
            "LOW": 3,
            "INFO": 0,
        }
        penalty = sum(severity_weights.get(f.severity, 0) for f in self.findings)
        compliance_score = max(0, 100 - penalty)

        return AuditReport(
            timestamp=datetime.now().isoformat(),
            total_checks=total_checks,
            passed=passed,
            failed=failed,
            warnings=warnings,
            findings=self.findings,
            compliance_score=compliance_score,
        )

    def generate_report_text(self, report: AuditReport) -> str:
        """
        Generar reporte en formato texto legible.

        Args:
            report: Reporte de auditoría.

        Returns:
            Reporte formateado como string.
        """
        lines = [
            "=" * 70,
            "REPORTE DE AUDITORÍA DE SEGURIDAD - SISTEMA DE IA",
            "=" * 70,
            f"Fecha: {report.timestamp}",
            f"Puntuación de Compliance: {report.compliance_score}/100",
            "",
            "📊 RESUMEN",
            "-" * 70,
            f"Total checks: {report.total_checks}",
            f"PASSED: {report.passed}",
            f"FAILED: {report.failed}",
            f"WARNING: {report.warnings}",
            "",
        ]

        # Estado general
        if report.compliance_score >= 80:
            lines.append("Estado: ✅ PASSED - Buen nivel de seguridad")
        elif report.compliance_score >= 60:
            lines.append("Estado: ⚠️ WARNING - Mejoras recomendadas")
        else:
            lines.append("Estado: ❌ FAILED - Requiere atención inmediata")

        lines.extend(["", "🔍 HALLAZGOS", "-" * 70])

        # Agrupar por severidad
        severity_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
        for severity in severity_order:
            findings = [f for f in report.findings if f.severity == severity]
            if findings:
                emoji = {
                    "CRITICAL": "🔴",
                    "HIGH": "🟠",
                    "MEDIUM": "🟡",
                    "LOW": "🔵",
                    "INFO": "ℹ️",
                }.get(severity, "")
                lines.append(f"\n{emoji} {severity} ({len(findings)})")

                for finding in findings:
                    lines.append(f"  • {finding.description}")
                    lines.append(f"    Categoría: {finding.category}")
                    lines.append(f"    Recomendación: {finding.recommendation}")
                    if finding.location:
                        lines.append(f"    Ubicación: {finding.location}")

        lines.extend(["", "=" * 70])

        return "\n".join(lines)


def main() -> int:
    """
    Punto de entrada principal.

    Returns:
        Código de salida (0=éxito, 1=error crítico).
    """
    parser = argparse.ArgumentParser(
        description="Auditoría de seguridad para sistemas de IA"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("~/.sky_claw/config.toml").expanduser(),
        help="Ruta al archivo de configuración",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/security_audit_report.txt"),
        help="Ruta para guardar el reporte",
    )
    parser.add_argument(
        "--fail-on-critical",
        action="store_true",
        help="Salir con código 1 si hay hallazgos CRITICAL",
    )

    args = parser.parse_args()

    try:
        # Ejecutar auditoría
        auditor = SecurityAuditor()
        report = auditor.audit_config_file(args.config)

        # Generar reporte
        report_text = auditor.generate_report_text(report)
        print(report_text)

        # Guardar reporte
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report_text)
        logger.info(f"Reporte guardado en {args.output}")

        # Guardar JSON para integración
        json_output = args.output.with_suffix(".json")
        with open(json_output, "w", encoding="utf-8") as f:
            import json
            from dataclasses import asdict

            json.dump(asdict(report), f, indent=2, default=str)
        logger.info(f"Resumen JSON guardado en {json_output}")

        # Determinar código de salida
        has_critical = any(f.severity == "CRITICAL" for f in report.findings)
        if args.fail_on_critical and has_critical:
            logger.error("Hallazgos CRITICAL detectados")
            return 1

        return 0 if report.compliance_score >= 60 else 1

    except Exception as e:
        logger.exception(f"Error en auditoría: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

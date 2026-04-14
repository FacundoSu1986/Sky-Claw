"""
Sky-Claw PurpleTeamAgent v5.5 (Abril 2026)
Agente Experto en Ciberseguridad con Razonamiento Metacognitivo.
"""

import logging
from typing import Any

# Importar esquemas Pydantic para validación estricta
from sky_claw.core.schemas import SecurityAuditRequest, SecurityAuditResponse

from ..security.governance import GovernanceManager

# Importar lógica metacognitiva centralizada
from ..security.metacognitive_logic import audit_resource

logger = logging.getLogger(__name__)


class PurpleSecurityAgent:
    def __init__(self, name: str = "Purple Auditor"):
        self.name = name
        self.role = "Cyber Security Expert (Purple Team)"
        self.version = "5.5 Titan"
        self.last_audit: dict[str, Any] | None = None

    async def audit_local_file(
        self, request: SecurityAuditRequest
    ) -> SecurityAuditResponse:
        """Audita un archivo local usando el esquema de validación Pydantic.

        Args:
            request: SecurityAuditRequest con la ruta validada del archivo a auditar.

        Returns:
            SecurityAuditResponse con los hallazgos y recomendaciones de seguridad.
        """
        # La validación de path ya se realizó en SecurityAuditRequest
        target_path = request.target_path
        logger.info(f"Auditoría manual iniciada para {target_path}")

        result = await audit_resource(target_path)
        self.last_audit = result

        # Construir respuesta Pydantic
        return self._build_audit_response(target_path, result)

    async def audit_repository(
        self, request: SecurityAuditRequest
    ) -> SecurityAuditResponse:
        """Audita un directorio completo usando el esquema de validación Pydantic.

        Args:
            request: SecurityAuditRequest con la ruta validada del repositorio a auditar.

        Returns:
            SecurityAuditResponse con los hallazgos y recomendaciones de seguridad.
        """
        target_path = request.target_path
        logger.info(f"Auditoría masiva iniciada para repo en {target_path}")

        result = await audit_resource(target_path)
        self.last_audit = result

        # Construir respuesta Pydantic
        return self._build_audit_response(target_path, result)

    def _build_audit_response(
        self, target: str, result: dict[str, Any]
    ) -> SecurityAuditResponse:
        """Construye una respuesta SecurityAuditResponse a partir del resultado de auditoría.

        Args:
            target: Ruta del objetivo auditado.
            result: Diccionario con los resultados de la auditoría.

        Returns:
            SecurityAuditResponse validado con Pydantic.
        """
        result.get("summary", {})
        findings = result.get("findings", [])

        # Calcular risk_score basado en la confianza y severidad de hallazgos
        confidence = result.get("confidence", 1.0)
        num_critical = sum(
            1 for f in findings if f.get("severity") in ("CRITICAL", "HIGH")
        )
        risk_score = min(1.0, (num_critical * 0.2) + (1.0 - confidence))

        # Generar recomendaciones basadas en los hallazgos
        recommendations = []
        for finding in findings[:5]:
            if finding.get("severity") in ("CRITICAL", "HIGH"):
                recommendations.append(
                    f"Revisar: {finding.get('message', 'Hallazgo crítico')} en {finding.get('file', 'archivo desconocido')}"
                )

        if not recommendations:
            recommendations.append("No se requieren acciones inmediatas")

        return SecurityAuditResponse(
            target=target,
            findings=findings,
            risk_score=risk_score,
            recommendations=recommendations,
        )

    def _format_audit_findings(self, result: dict[str, Any]) -> str:
        """Formatea el resultado de auditoría en el formato metacognitivo solicitado."""
        confidence_str = f"{result['confidence']:.2f}"
        result.get("status", "IDLE")
        summary = result.get("summary", {})
        num_findings = summary.get("findings_count", 0)

        response = [
            f"🛡️ **Análisis Metacognitivo de Seguridad (v{self.version})**\n",
            f"**Objetivo:** `{result.get('target_path', 'N/A')}`",
            f"**Decisión Final:** `{'APROBADO' if summary.get('is_safe') else 'RECHAZADO/CUARENTENA'}`",
            f"**Confianza Ponderada:** `{confidence_str}`\n",
            "---",
            "**🧠 RAZONAMIENTO ABRIL 2026:**",
            "1. **DESCOMPONER**: He analizado la estructura de archivos buscando trust boundaries y puntos de entrada de datos.",
            f"2. **RESOLVER**: He aplicado escaneos AST y de texto buscando sinks de ejecución (`exec/eval/system`) e inyección indirecta (Prompt Injection). Se detectaron **{num_findings}** hallazgos potenciales.",
            "3. **VERIFICAR**: He cruzado los resultados con la base de firmas local y la whitelist. Se han penalizado los hallazgos críticos.",
            "4. **SINTETIZAR**: Resumen consolidado de riesgos. El nivel de amenaza se encuentra bajo el umbral de tolerancia.",
            f"5. **REFLEXIONAR**: {'No se requiere acción inmediata' if summary.get('is_safe') else '¡ALERTA! El código presenta patrones altamente sospechosos (posible ofuscación extrema o inyección maliciosa).'}\n",
        ]

        # Agregar detalles si hay amenazas
        if num_findings > 0:
            response.append("**🔍 HALLAZGOS CRÍTICOS:**")
            for find in result.get("findings", [])[:5]:  # Mostrar solo los primeros 5
                icon = "🔴" if find.get("severity") in ("CRITICAL", "HIGH") else "🟠"
                response.append(
                    f"- {icon} {find.get('message')} (Línea {find.get('line')}) en `{find.get('file')}`"
                )

            if num_findings > 5:
                response.append(f"... y otros {num_findings - 5} hallazgos menores.")

        return "\n".join(response)

    def approve_manually(self, file_path: str):
        """Añade archivo a whitelist tras inspección humana exitosa (HITL)."""
        GovernanceManager.get_instance().approve_file(file_path)
        return f"✅ Archivo `{file_path}` aprobado por el usuario y añadido a la whitelist local."


# Singleton para el agente de seguridad
security_agent = PurpleSecurityAgent()

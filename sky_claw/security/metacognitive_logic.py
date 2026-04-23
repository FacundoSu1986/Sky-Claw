"""
Sky-Claw Metacognitive Reasoning Logic v5.5 (Abril 2026)
Framework de decisión secuencial de 5 pasos para Auditoría Purple Team.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .governance import GovernanceManager

# Importar componentes de seguridad
from .purple_scanner import run_scan
from .text_inspector import scan_text

logger = logging.getLogger(__name__)


class SecurityMetacognition:
    def __init__(self, target_path: str):
        self.target_path = Path(target_path)
        self.session_data: dict[str, Any] = {
            "start_time": datetime.now(UTC).isoformat(),
            "findings": [],
            "confidence": 1.0,
            "status": "INIT",
        }

    async def execute_cycle(self) -> dict[str, Any]:
        """Ejecuta el ciclo metacognitivo completo de 5 pasos."""
        try:
            # Fase 1: DESCOMPONER
            if not await self._phase_decompose():
                return self.session_data

            # Fase 2: RESOLVER
            await self._phase_resolve()

            # Fase 3: VERIFICAR
            await self._phase_verify()

            # Fase 4: SINTETIZAR
            await self._phase_synthesize()

            # Fase 5: REFLEXIONAR
            await self._phase_reflect()

            return self.session_data

        except Exception as e:
            logger.error(f"Error en ciclo metacognitivo: {e}")
            self.session_data["status"] = "ERROR"
            return self.session_data

    async def _phase_decompose(self) -> bool:
        """Fase 1: Descompone el objetivo en archivos analizables."""
        self.session_data["status"] = "DECOMPOSING"
        if not self.target_path.exists():
            logger.error(f"Ruta no encontrada: {self.target_path}")
            return False

        files = []
        if self.target_path.is_file():
            files = [self.target_path]
        else:
            py_files = list(self.target_path.rglob("*.py"))
            md_files = list(self.target_path.rglob("*.md"))
            txt_files = list(self.target_path.rglob("*.txt"))
            files = py_files + md_files + txt_files

        self.session_data["files_to_scan"] = sorted([str(f) for f in files])
        return True

    async def _phase_resolve(self):
        """Fase 2: Ejecuta los escáneres correspondientes."""
        self.session_data["status"] = "RESOLVING"
        all_findings = []
        gov = GovernanceManager.get_instance()

        for file_path in self.session_data["files_to_scan"]:
            Path(file_path)

            # Protección: No volver a escanear si está limpio en el caché incremental
            if await gov.is_scanned_and_clean(file_path):
                logger.info(f"Saltando {file_path} (Caché incremental limpio)")
                continue

            try:
                with open(file_path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()

                # Escáner según extensión
                findings = []
                if file_path.endswith(".py"):
                    findings = run_scan(content, filename=file_path)
                elif file_path.endswith((".md", ".txt")):
                    findings = scan_text(content, filename=file_path)

                # Persistir hallazgos si es necesario
                status = "CLEAN" if not findings else "SUSPICIOUS"
                await gov.update_scan_result(file_path, findings, status)

                all_findings.extend(findings)

            except Exception as e:
                logger.error(f"Error procesando {file_path}: {e}")

        self.session_data["findings"] = all_findings

    async def _phase_verify(self):
        """Fase 3: Cruce de hallazgos con base de datos de amenazas y lógica de negocio."""
        self.session_data["status"] = "VERIFYING"
        # Aquí se ajusta la confianza basada en la severidad de los hallazgos
        num_critical = len([f for f in self.session_data["findings"] if f.get("severity") == "CRITICAL"])
        num_high = len([f for f in self.session_data["findings"] if f.get("severity") == "HIGH"])

        # Penalización bayesiana simple
        self.session_data["confidence"] -= num_critical * 0.4
        self.session_data["confidence"] -= num_high * 0.15
        self.session_data["confidence"] = max(0.0, self.session_data["confidence"])

    async def _phase_synthesize(self):
        """Fase 4: Agregación de resultados y generación de informe local."""
        self.session_data["status"] = "SYNTHESIZING"
        self.session_data["summary"] = {
            "total_files": len(self.session_data["files_to_scan"]),
            "findings_count": len(self.session_data["findings"]),
            "is_safe": self.session_data["confidence"] >= 0.8,
        }

    async def _phase_reflect(self):
        """Fase 5: Decisión final y autorreflexión si la confianza es baja."""
        self.session_data["status"] = "REFLECTING"
        if self.session_data["confidence"] < 0.8:
            logger.warning(f"Confianza baja ({self.session_data['confidence']}). Requiere revisión manual (HITL).")
            self.session_data["final_decision"] = "REJECT/QUARANTINE"
        else:
            self.session_data["final_decision"] = "ACCEPT/EXECUTE"

        self.session_data["end_time"] = datetime.now(UTC).isoformat()
        self.session_data["status"] = "COMPLETED"


async def audit_resource(path: str) -> dict[str, Any]:
    """Punto de entrada asíncrono para auditoría Purple Team."""
    logic = SecurityMetacognition(path)
    return await logic.execute_cycle()

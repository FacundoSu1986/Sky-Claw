import ast
import asyncio
import hashlib
import logging
from typing import Final, FrozenSet, List

from sky_claw.core.errors import AppNexusError

logger = logging.getLogger(__name__)


class SecurityAuditError(AppNexusError):
    """
    Excepción específica para fallos de auditoría de seguridad.

    Forma parte de la jerarquía Zero-Trust del sistema Sky-Claw.
    Se lanza cuando se detectan violaciones de políticas AST o
    errores críticos durante el análisis estático.
    """

    pass


class ASTGuardian:
    """
    Servicio de seguridad para auditoría estática de código Python mediante AST.

    Implementa controles de seguridad para agentes autónomos, incluyendo
    detección de funciones peligrosas, límites de complejidad y trazabilidad
    forense mediante hashing de payloads.

    Attributes:
        forbidden_functions: Conjunto inmutable de funciones prohibidas.
        max_nodes: Límite máximo de nodos AST para prevenir DoS.
    """

    def __init__(
        self, max_nodes: int = 2000, forbidden_functions: FrozenSet[str] | None = None
    ) -> None:
        """
        Inicializa el guardián con políticas de seguridad configurables.

        Args:
            max_nodes: Límite de nodos AST antes de considerar DoS. Default: 2000.
            forbidden_functions: Conjunto de funciones prohibidas. Si es None,
                usa valores por defecto seguros.
        """
        # Inmutabilidad garantizada con frozenset
        self.forbidden_functions: Final[FrozenSet[str]] = (
            frozenset(forbidden_functions)
            if forbidden_functions
            else frozenset(
                [
                    "eval",
                    "exec",
                    "compile",
                    "__import__",
                    "globals",
                    "locals",
                    "getattr",
                    "setattr",
                    "delattr",
                    "hasattr",
                    "__builtins__",
                ]
            )
        )
        self.max_nodes: Final[int] = max_nodes
        logger.debug(f"ASTGuardian initialized with max_nodes={max_nodes}")

    async def execute_audit(self, context: str, payload: str) -> bool:
        """
        Audita asincrónicamente un payload de texto en busca de violaciones.

        Args:
            context: Identificador del contexto de ejecución (ej. 'llm_generation').
            payload: Código fuente Python a analizar.

        Returns:
            bool: True si el payload es seguro, False si se detectan violaciones.
        """
        logger.debug(f"Audit requested for context: {context}")

        if not payload or not isinstance(payload, str):
            logger.warning("Empty or invalid payload received.")
            return True

        try:
            # Delegación a thread pool para no bloquear el event loop
            return await asyncio.to_thread(self._sync_audit, payload)
        except SecurityAuditError as sae:
            logger.error(f"[SECURITY BLOCK] Zero-Trust policy enforced: {sae}")
            return False
        except Exception as e:
            logger.error(
                f"[ERROR] Guardian failed to process AST: {e.__class__.__name__}"
            )
            return False  # Fall back to block (Zero-Trust)

    def _sync_audit(self, payload: str) -> bool:
        """
        Ejecuta el análisis estático del AST de forma síncrona.

        Args:
            payload: Código fuente a parsear.

        Returns:
            bool: Estado de seguridad del código.

        Raises:
            SecurityAuditError: Si se exceden los límites de recursos.
        """
        try:
            tree = ast.parse(payload)
            violations: List[str] = []
            node_count: int = 0

            for node in ast.walk(tree):
                node_count += 1
                if node_count > self.max_nodes:
                    raise SecurityAuditError(
                        f"AST node limit exceeded (DoS Protection). Limit: {self.max_nodes}"
                    )

                # Chequeo de llamadas prohibidas
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        if node.func.id in self.forbidden_functions:
                            violations.append(
                                f"Forbidden func '{node.func.id}' at line {node.lineno}"
                            )

                # Bloqueo de accesos dunder (ej. __class__, __mro__)
                elif isinstance(node, ast.Attribute):
                    if node.attr.startswith("__") and node.attr.endswith("__"):
                        violations.append(
                            f"Forbidden dunder attribute '{node.attr}' at line {node.lineno}"
                        )

                # Bloqueo de importaciones dinámicas
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    violations.append(
                        f"Forbidden import detected at line {node.lineno}"
                    )

            if violations:
                # Trazabilidad forense con SHA-256
                payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
                logger.warning(
                    f"Vulnerabilities detected. Hash: {payload_hash}. Findings: {violations}"
                )
                return False

            return True

        except SyntaxError:
            # Código no parseable = no ejecutable = seguro en este contexto
            logger.debug("Payload failed syntax parse (non-executable)")
            return True
        except SecurityAuditError:
            raise
        except Exception as e:
            raise SecurityAuditError(
                f"Sync audit encountered unexpected core error: {e}"
            ) from e


if __name__ == "__main__":
    # Test block - Usando logging en lugar de print
    logging.basicConfig(
        level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    guardian = ASTGuardian()
    unsafe_code = "import os; exec('print(1)')"
    is_safe = asyncio.run(guardian.execute_audit("cli_test", unsafe_code))
    logger.info(f"Safe: {is_safe}")

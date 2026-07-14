"""Flujo promote/discard del sandbox con aprobación HITL post-run (T-27b·2, ADR 0005).

``run_ritual_in_sandbox`` (T-27b·1) devuelve un clon **vivo** que el caller
debe ``promote()`` o ``discard()`` tras aprobación HITL — y hasta este módulo
ese bucle de decisión no existía en ningún caller de producción.
:class:`SandboxPromotionFlow` lo cierra: corre el ritual contra el clon,
presenta el diff al operador vía :class:`HITLGuard` (categoría
``sandbox_promotion``, bloqueante con timeout fail-secure) y promueve o
descarta según la decisión.

La promoción es **síncrona** (dentro del mismo dispatch, como el gate
pre-ejecución de las tools destructivas): el drift-gate de ``promote()`` hace
frágil cualquier ventana larga de aprobación, así que un estado asíncrono
"pendiente de promoción" compraría fragilidad, no valor (ver ADR 0005).

Fail-closed en todas las ramas:

* sin ``HITLGuard`` → se deniega **sin correr el ritual** (precedente
  ``HitlGateMiddleware``);
* ritual con ``success`` falsy → se descarta sin prompt (escrituras parciales
  no se promueven jamás), con el diff como evidencia en el result;
* diff vacío → se descarta sin prompt (aprobar cero cambios es ruido);
* denegado/timeout → se descarta, con mensaje explícito y el diff en el result;
* solo ``Decision.APPROVED`` promueve.

Divergencia deliberada con el contrato de ``sandbox_run`` (que deja el clon
vivo ante un fallo del tool, para forense manual): acá el flujo es dueño del
ciclo completo, captura el diff en el result y descarta — un clon huérfano por
run fallido sería un leak de disco sin GUI que lo inspeccione. Excepción: si
``promote()`` falló Y su rollback también (:class:`SandboxRollbackError`), el
clon NO se descarta porque su árbol contiene el backup de restauración manual.

Vive en la capa orchestrator (no en ``local/mo2``) porque necesita importar
``security.hitl`` sin acoplar el núcleo del sandbox a la política de
aprobación. Ritual-agnóstico: el callable se inyecta, igual que en
``run_ritual_in_sandbox``, para servir a futuros runners redirigibles.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.security.hitl import Decision
from sky_claw.local.mo2.profile_sandbox import SandboxDriftError, SandboxRollbackError
from sky_claw.local.mo2.sandbox_run import run_ritual_in_sandbox

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sky_claw.antigravity.security.hitl import HITLGuard
    from sky_claw.local.mo2.profile_sandbox import ProfileSandbox, SandboxClone, SandboxDiff

logger = logging.getLogger(__name__)

#: Categoría HITL propia de la promoción post-run. NUNCA auto-aprobada por
#: «Modo local» (ver ``make_gui_hitl_notify``) ni por el fallback headless de
#: ``app_context`` — revisar el diff ES el propósito del sandbox.
SANDBOX_PROMOTION_CATEGORY = "sandbox_promotion"

#: Límites del detail del prompt (mismos que los helpers de HitlGateMiddleware:
#: el operador decide sobre un resumen legible, no sobre un dump infinito).
_MAX_DETAIL_LENGTH = 800
_MAX_LISTED_PATHS = 10

_KIND_PREFIX = {"added": "+", "modified": "~", "removed": "-"}


def format_diff_detail(diff: SandboxDiff) -> str:
    """Resumen operador-legible del diff: conteo por tipo + primeros paths.

    ``+`` added, ``~`` modified, ``-`` removed; truncado a ~800 caracteres
    para que el prompt (modal GUI / mensaje Telegram) siga siendo legible.
    """
    counts: dict[str, int] = {}
    for change in diff.changes:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    resumen = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))
    lineas = [f"{len(diff.changes)} cambio(s) — {resumen}"]
    for change in diff.changes[:_MAX_LISTED_PATHS]:
        lineas.append(f"{_KIND_PREFIX[change.kind]} {change.area}/{change.relative_path}")
    restantes = len(diff.changes) - _MAX_LISTED_PATHS
    if restantes > 0:
        lineas.append(f"… y {restantes} más")
    detalle = "\n".join(lineas)
    if len(detalle) > _MAX_DETAIL_LENGTH:
        detalle = detalle[: _MAX_DETAIL_LENGTH - 3] + "..."
    return detalle


def _sandbox_annotation(
    diff: SandboxDiff,
    *,
    promoted: bool,
    decision: str,
    files_written: int = 0,
    files_deleted: int = 0,
) -> dict[str, Any]:
    """Bloque ``result["sandbox"]`` uniforme para todas las ramas del flujo."""
    return {
        "promoted": promoted,
        "decision": decision,
        "changes": [{"area": c.area, "relative_path": c.relative_path, "kind": c.kind} for c in diff.changes],
        "files_written": files_written,
        "files_deleted": files_deleted,
    }


def _rewrite_clone_paths(data: Any, clone: SandboxClone) -> Any:
    """Reescribe recursivamente las rutas del clon por las del perfil real en el dict de resultado."""
    if isinstance(data, dict):
        return {k: _rewrite_clone_paths(v, clone) for k, v in data.items()}
    elif isinstance(data, list):
        return [_rewrite_clone_paths(item, clone) for item in data]
    elif isinstance(data, str):
        res = data.replace(str(clone.profile_copy), str(clone.profile_source))
        res = res.replace(str(clone.overwrite_copy), str(clone.overwrite_source))
        return res
    return data


class SandboxPromotionFlow:
    """Corre un ritual en sandbox y resuelve promote/discard vía HITL.

    Args:
        sandbox: El :class:`ProfileSandbox` ya configurado (mo2_root/perfil).
        hitl_guard: Backbone de aprobación del proyecto. ``None`` = fail-closed:
            el ritual se deniega sin ejecutarse (nadie podría aprobar el diff).
    """

    def __init__(self, *, sandbox: ProfileSandbox, hitl_guard: HITLGuard | None) -> None:
        self._sandbox = sandbox
        self._hitl_guard = hitl_guard

    async def run(
        self,
        *,
        ritual_name: str,
        ritual: Callable[[SandboxClone], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Ejecuta el ciclo completo: clonar → ritual → diff → HITL → promote/discard.

        Returns:
            El dict del ritual anotado con ``result["sandbox"]`` (promoted,
            decision, changes, files_written/deleted), preservando el contrato
            canónico ``success``/``message``. Las ramas que NO aplican cambios
            al árbol real (denegado, drift, rollback fallido, sin guard)
            devuelven ``success=False`` con un ``reason`` programático.

        Raises:
            Lo que lance el ritual o el propio sandbox (el clon nunca queda
            huérfano: se descarta antes de propagar, salvo tras un
            :class:`SandboxRollbackError`, donde preserva el backup manual).
        """
        if self._hitl_guard is None:
            logger.critical(
                "SandboxPromotionFlow: sin HITLGuard — se DENIEGA el ritual '%s' "
                "sin ejecutarlo (fail-closed): nadie podría aprobar la promoción.",
                ritual_name,
            )
            return {
                "success": False,
                "message": (
                    f"Sin canal de aprobación HITL configurado; el ritual '{ritual_name}' "
                    "se deniega sin ejecutarse (política fail-closed del sandbox)."
                ),
                "reason": "SandboxPromotionUnavailable",
                "sandbox": {
                    "promoted": False,
                    "decision": "unavailable",
                    "changes": [],
                    "files_written": 0,
                    "files_deleted": 0,
                },
            }

        sandboxed = await run_ritual_in_sandbox(sandbox=self._sandbox, ritual=ritual)
        clone, diff, result = sandboxed.clone, sandboxed.diff, dict(sandboxed.result)

        # Escrituras parciales no se promueven jamás; el diff viaja como
        # evidencia para el operador (filosofía caja negra) y el clon se
        # descarta — sin GUI de forense, un clon vivo sería solo un leak.
        if not result.get("success"):
            with contextlib.suppress(Exception):
                await self._sandbox.discard(clone)
            result["sandbox"] = _sandbox_annotation(diff, promoted=False, decision="ritual_failed")
            return result

        if diff.is_empty:
            with contextlib.suppress(Exception):
                await self._sandbox.discard(clone)
            result["sandbox"] = _sandbox_annotation(diff, promoted=False, decision="empty_diff")
            return result

        decision = await self._request_decision(ritual_name, diff, clone)

        if decision is not Decision.APPROVED:
            with contextlib.suppress(Exception):
                await self._sandbox.discard(clone)
            logger.warning(
                "SandboxPromotionFlow: promoción de '%s' NO aprobada (decision=%s) — %d cambio(s) descartado(s).",
                ritual_name,
                decision.value,
                len(diff.changes),
            )
            result["success"] = False
            result["message"] = (
                f"El ritual '{ritual_name}' corrió en sandbox pero el operador no aprobó la "
                f"promoción (decisión: {decision.value}); {len(diff.changes)} cambio(s) descartado(s)."
            )
            result["reason"] = "SandboxPromotionDenied"
            result["sandbox"] = _sandbox_annotation(diff, promoted=False, decision="denied")
            return result

        return await self._promote(ritual_name, clone, diff, result)

    async def _request_decision(self, ritual_name: str, diff: SandboxDiff, clone: SandboxClone) -> Decision:
        """Bloquea en el HITL con el diff como detail; ante cancelación o fallo
        inesperado del guard, descarta el clon antes de propagar (mismo
        contrato de limpieza que ``run_ritual_in_sandbox``)."""
        assert self._hitl_guard is not None  # ya validado en run()
        request_id = f"sandbox-{ritual_name}-{uuid.uuid4().hex[:12]}"
        logger.info(
            "SandboxPromotionFlow: '%s' terminó en sandbox con %d cambio(s); "
            "esperando decisión del operador (request_id=%s).",
            ritual_name,
            len(diff.changes),
            request_id,
        )
        try:
            return await self._hitl_guard.request_approval(
                request_id=request_id,
                reason=(
                    f"El ritual '{ritual_name}' terminó en sandbox: aprobá para promover "
                    f"{len(diff.changes)} cambio(s) al perfil real, o denegá para descartarlos."
                ),
                detail=format_diff_detail(diff),
                category=SANDBOX_PROMOTION_CATEGORY,
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._sandbox.discard(clone)
            raise
        except Exception:
            try:
                await self._sandbox.discard(clone)
            except Exception:
                logger.warning("No se pudo descartar el clon %s tras el fallo del HITL", clone.root, exc_info=True)
            raise

    async def _promote(
        self,
        ritual_name: str,
        clone: SandboxClone,
        diff: SandboxDiff,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Promueve el clon aprobado; drift y rollback fallido son fail-closed."""
        try:
            promocion = await self._sandbox.promote(clone)
        except SandboxDriftError as exc:
            # El árbol real cambió en la ventana de aprobación: promover
            # pisaría cambios vivos. El propio promote ya cortó; descartar.
            with contextlib.suppress(Exception):
                await self._sandbox.discard(clone)
            result["success"] = False
            result["message"] = str(exc)
            result["reason"] = "SandboxDriftDetected"
            result["sandbox"] = _sandbox_annotation(diff, promoted=False, decision="drift")
            return result
        except SandboxRollbackError as exc:
            # NO descartar: el árbol del clon contiene el backup para la
            # restauración manual (la ruta viaja en el mensaje de la excepción).
            logger.critical(
                "SandboxPromotionFlow: promote de '%s' falló Y el rollback también; "
                "el clon %s se preserva con el backup manual.",
                ritual_name,
                clone.root,
            )
            result["success"] = False
            result["message"] = str(exc)
            result["reason"] = "SandboxRollbackFailed"
            result["sandbox"] = _sandbox_annotation(diff, promoted=False, decision="rollback_failed")
            return result
        except Exception:
            # Descarte general para errores imprevistos (e.g. symlinks en Windows)
            with contextlib.suppress(Exception):
                await self._sandbox.discard(clone)
            raise

        try:
            await self._sandbox.discard(clone)
        except Exception:
            logger.warning("Limpieza post-promoción fallida: no se pudo descartar %s", clone.root, exc_info=True)

        logger.info(
            "SandboxPromotionFlow: '%s' promovido al perfil real (%d escrito(s), %d borrado(s)).",
            ritual_name,
            promocion.files_written,
            promocion.files_deleted,
        )

        # Reescribir las rutas en el payload final
        result = _rewrite_clone_paths(result, clone)

        result["sandbox"] = _sandbox_annotation(
            diff,
            promoted=True,
            decision="approved",
            files_written=promocion.files_written,
            files_deleted=promocion.files_deleted,
        )
        return result


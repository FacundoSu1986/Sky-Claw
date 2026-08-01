"""Orquestador mínimo de rituales en sandbox (T-27b·1, ADR 0002).

El ``ProfileSandbox`` (T-27, #245) provee clone/diff/promote, pero hasta este
módulo **nadie poseía el ciclo de vida**: quién clona, quién corre el ritual
contra la copia, quién calcula el diff. :func:`run_ritual_in_sandbox` es ese
dueño mínimo, y con él la garantía de T-27 se vuelve real para el primer
runner redirigible (Synthesis, vía el ``output_path`` inyectable de
``SynthesisPipelineService`` → ``SandboxClone.overwrite_copy``).

Contrato de propiedad del clon:

* ritual OK (aunque devuelva ``success=False``) → el clon **queda vivo** y el
  caller es su dueño: ``promote()`` tras aprobación HITL o ``discard()``. Un
  fallo a nivel tool no descarta: el diff de las escrituras parciales es
  evidencia para el operador (filosofía caja negra).
* ritual lanza excepción → el clon se **descarta** acá mismo (no dejar
  ``.skyclaw_sandbox`` colgados) y la excepción propaga.

El ritual es un callable inyectado (``local/mo2/`` no importa
``local/tools/``: sin acople ni ciclo de imports).

El dueño de producción de este ciclo es
``sky_claw.app.orchestrator.sandbox_promotion.SandboxPromotionFlow``
(T-27b·2, ADR 0005): corre el ritual acá, presenta el diff al operador vía
HITL y resuelve promote/discard según la decisión.

Follow-up: Pandora sí soporta ``--output`` absoluto y Sky-Claw lo fija a
``<game>/Pandora_Output``. Aún no acepta un override hacia el clon del sandbox
ni está cableado al broker/perfil USVFS con diff/promoción; por eso T-27 sigue
parcial. Esta integración está verificada por código y tests, no en un rig real
de Pandora/MO2. DynDOLOD/bashed, ídem cuando toque cablearlos.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sky_claw.local.mo2.profile_sandbox import ProfileSandbox, SandboxClone, SandboxDiff

logger = logging.getLogger(__name__)

#: Referencias fuertes a las tasks de clone()/cleanup en vuelo tras una
#: cancelación: el loop solo guarda referencias DÉBILES a las tasks (ver docs
#: de ``asyncio.shield``/``asyncio.ensure_future``), así que sin esto una task
#: que sigue corriendo en background tras un `raise` (p. ej. una segunda
#: cancelación que nos hizo dejar de esperarla) podría ser recolectada por GC
#: antes de terminar, perdiendo el discard() pendiente (review CodeRabbit
#: PR #378; mismo hallazgo que Codex PR #320 en ``SandboxPromotionFlow``, que
#: usa ``self._cleanup_tasks`` porque ahí SÍ hay un objeto de vida larga donde
#: anclarla — acá, función suelta sin instancia, el ancla es este set módulo).
_background_tasks: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    """Ancla ``task`` contra GC hasta que termine (ver ``_background_tasks``)."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


@dataclass(frozen=True, slots=True)
class SandboxedRunResult:
    """Resultado de un ritual corrido contra el sandbox.

    Attributes:
        clone: El clon vivo — el caller debe llamar ``promote()`` o
            ``discard()`` sobre él (ver contrato del módulo).
        diff: Qué hizo el ritual sobre la copia (perfil + overwrite).
        result: El dict del ritual, intacto (contrato ``success``/``message``).
    """

    clone: SandboxClone
    diff: SandboxDiff
    result: dict[str, Any]


async def run_ritual_in_sandbox(
    *,
    sandbox: ProfileSandbox,
    ritual: Callable[[SandboxClone], Awaitable[dict[str, Any]]],
) -> SandboxedRunResult:
    """Clona, corre el ritual contra la copia y devuelve el diff explicable.

    Args:
        sandbox: El :class:`ProfileSandbox` ya configurado (mo2_root/perfil).
        ritual: Callable que ejecuta el Ritual mutante contra el clon —
            típicamente construye el servicio del tool apuntando su salida a
            ``clone.overwrite_copy`` (p. ej. ``SynthesisPipelineService``
            con ``output_path=clone.overwrite_copy``).

    Returns:
        :class:`SandboxedRunResult` con el clon vivo, el diff y el resultado.

    Raises:
        Lo que lance el ritual (el clon se descarta antes de propagar) o el
        propio sandbox (``ProfileNotFoundError``, ``SandboxSymlinkError``…).
    """
    # U-08: clone() corre _materialize vía asyncio.to_thread (profile_sandbox.py)
    # — cancelar la corrutina que lo espera no interrumpe ese hilo. Sin shield,
    # el hilo seguía escribiendo el clon en background sin que nadie lo limpie
    # (el `try` de acá abajo, que sí maneja CancelledError, nunca se alcanza a
    # tiempo). Se espera el desenlace REAL antes de decidir qué limpiar — mismo
    # patrón F2 que SandboxPromotionFlow._promote usa para promote().
    clone_task = _track(asyncio.ensure_future(sandbox.clone()))
    try:
        clone = await asyncio.shield(clone_task)
    except asyncio.CancelledError:
        cleanup = _track(asyncio.ensure_future(_finalizar_clone_cancelado(sandbox, clone_task)))
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup)
        raise
    try:
        result = await ritual(clone)
        # El diff va dentro del mismo cleanup: si lanza (p. ej.
        # SandboxSymlinkError por un artefacto inseguro que dejó el ritual),
        # el caller nunca recibe el handle del clon — hay que descartarlo acá
        # o quedaría un .skyclaw_sandbox huérfano (review Codex #258).
        diff = await sandbox.diff(clone)
    except asyncio.CancelledError:
        # Limpieza best-effort SIN tragar ni demorar la cancelación
        # (precedente: loot_service). KeyboardInterrupt/SystemExit propagan
        # sin limpieza: en un shutdown no se hace I/O de filesystem.
        with contextlib.suppress(Exception):
            await sandbox.discard(clone)
        raise
    except Exception:
        # No dejar clones colgados: el run no produjo un resultado utilizable.
        # Si el propio discard falla, propaga el error ORIGINAL (el del
        # ritual/diff), no el de la limpieza.
        try:
            await sandbox.discard(clone)
        except Exception:
            logger.warning("No se pudo descartar el clon %s tras el fallo", clone.root, exc_info=True)
        raise
    logger.info(
        "Ritual en sandbox completado (success=%s): %d cambio(s) en el clon %s",
        result.get("success"),
        len(diff.changes),
        clone.root,
    )
    return SandboxedRunResult(clone=clone, diff=diff, result=result)


async def _finalizar_clone_cancelado(sandbox: ProfileSandbox, clone_task: asyncio.Task[SandboxClone]) -> None:
    """Observa el desenlace real de un ``clone()`` cancelado y descarta si materializó.

    Espeja ``SandboxPromotionFlow._finalizar_promote_cancelado`` (F2): el hilo
    de ``_materialize`` sigue escribiendo tras la cancelación de la corrutina
    que lo esperaba, así que hay que esperar su desenlace REAL (shieldeado)
    antes de tocar el disco. Si terminó en excepción, ``_materialize`` ya se
    autolimpió (o nunca llegó a crear nada) — nada que hacer acá. Si terminó
    con éxito, el clon quedó materializado pero nadie lo pidió: se descarta.
    """
    try:
        clone = await asyncio.shield(clone_task)
    except (asyncio.CancelledError, Exception):
        return
    with contextlib.suppress(Exception):
        await sandbox.discard(clone)

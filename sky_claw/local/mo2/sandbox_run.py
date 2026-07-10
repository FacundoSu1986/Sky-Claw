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

Follow-up T-27b·2: Pandora no es redirigible hoy (sin palanca de output — el
subproceso escribe vía el VFS de MO2 con ``cwd``); su aislamiento requiere
diseño de redirección aparte. DynDOLOD/bashed, ídem cuando toque cablearlos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sky_claw.local.mo2.profile_sandbox import ProfileSandbox, SandboxClone, SandboxDiff

logger = logging.getLogger(__name__)


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
    clone = await sandbox.clone()
    try:
        result = await ritual(clone)
    except BaseException:
        # No dejar clones colgados: el run no produjo un resultado utilizable.
        await sandbox.discard(clone)
        raise
    diff = await sandbox.diff(clone)
    logger.info(
        "Ritual en sandbox completado (success=%s): %d cambio(s) en el clon %s",
        result.get("success"),
        len(diff.changes),
        clone.root,
    )
    return SandboxedRunResult(clone=clone, diff=diff, result=result)

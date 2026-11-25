"""Servicio compartido de BodySlide (PR 2a).

Propiedad del mecanismo: *toda superficie que lanza BodySlide pasa por el MISMO
servicio*, y por lo tanto por el mismo lock, la misma validación del grupo, el
mismo move-aside y el mismo puente fallo→rollback. Hoy la única superficie viva
es la tool del agente (``run_bodyslide_batch``); la GUI de PR 4 tiene que
construir este servicio y no una copia de la orquestación — el defecto dominante
del repo es arreglar un hermano y dejar al otro (AGENTS.md). Ancla:
``tests/test_bodyslide_servicio.py`` (censo AST de superficies).

PR 2a mueve el código que vivía inline en ``run_bodyslide_batch`` SIN cambiar el
comportamiento: mismo orden, mismos mensajes, mismo dict de salida. Lo nuevo es
el estado tipado (:class:`EstadoBodySlide`) en :class:`ResultadoDelServicio`;
el dict que ve el agente no lo lleva (estados tipados al agente = PR 3).

Diseño: ``docs/design/specs/2026-11-23-bodyslide-pr2-concepts-brief.md`` §13.3.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import pathlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from sky_claw.app.security.sanitize import sanitize_for_prompt
from sky_claw.local.tools.output_targets import BODYSLIDE_MESHES_RESOURCE_ID

#: Barrera productiva de la ejecución brokered de BodySlide (brief §13.2 C5).
#: En PR 2a no hay backend brokered que habilitar; la barrera existe para que el
#: backend de 2c nazca detrás de ella. Sólo el PR de activación —con 2a–2d
#: mergeados, PR 3 (catálogo/preset/manifiesto certificados), el rig real con
#: preregistro y la firma del propietario— puede ponerla en ``True``. Ancla:
#: ``test_nadie_reasigna_la_barrera_productiva``.
_CONTENCION_BODYSLIDE_VERIFICADA = False


class EstadoBodySlide(enum.StrEnum):
    """Vocabulario CERRADO de estados de una corrida de BodySlide.

    Congelado por igualdad literal en ``tests/test_bodyslide_servicio.py``.
    Sólo :attr:`SUCCESS` es éxito en 2a (en 2d el éxito pasa a ser ``PUBLISHED``).
    """

    # Contrato y runner de PR 1 (``bodyslide_headless`` / ``bodyslide_runner``).
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
    CONFIG_PRECONDITION_FAILED = "CONFIG_PRECONDITION_FAILED"
    ARGS_INVALID = "ARGS_INVALID"
    STAGING_UNDETERMINED = "STAGING_UNDETERMINED"
    STAGING_UNVERIFIABLE = "STAGING_UNVERIFIABLE"
    # Precondiciones y desenlaces de la superficie (PR 2a).
    RUNNER_NOT_CONFIGURED = "RUNNER_NOT_CONFIGURED"
    PROTECTION_MISSING = "PROTECTION_MISSING"
    LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    RESULT_WITHOUT_STATE = "RESULT_WITHOUT_STATE"
    # Reservados para 2b–2d (brief §6.4). Ningún camino los emite en 2a.
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    CONFIG_IO_HUNG = "CONFIG_IO_HUNG"
    WRITE_OUTSIDE_STAGING = "WRITE_OUTSIDE_STAGING"
    STAGING_TAMPERED = "STAGING_TAMPERED"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    HOLD_TERMINALITY_UNKNOWN = "HOLD_TERMINALITY_UNKNOWN"
    HOLD_LEASE_LOST = "HOLD_LEASE_LOST"
    RESTORE_FAILED = "RESTORE_FAILED"
    OUTPUT_MOD_NOT_READY = "OUTPUT_MOD_NOT_READY"
    OUTPUT_MOD_NOT_OWNED = "OUTPUT_MOD_NOT_OWNED"
    CONFLICT_BETWEEN_GROUPS = "CONFLICT_BETWEEN_GROUPS"
    PUBLISH_ROLLED_BACK = "PUBLISH_ROLLED_BACK"
    PUBLISH_ROLLBACK_PENDING = "PUBLISH_ROLLBACK_PENDING"
    PUBLISHED = "PUBLISHED"


#: Estados de 2c-2/2d: existen en el vocabulario para que el contrato no cambie de
#: forma en cada PR, pero un :class:`ResultadoDelServicio` no puede llevarlos todavía
#: y un runner que los reporte no se acepta. PR 2c-1 liberó los cuatro que el runner
#: emite con el journal: PRECONDITION_FAILED, CONFIG_IO_HUNG, CONCURRENT_MODIFICATION y
#: RESTORE_FAILED. PR 2c-2b liberó HOLD_TERMINALITY_UNKNOWN: lo emite el runner cuando el
#: lanzador no puede probar que el proceso terminó.
ESTADOS_RESERVADOS: frozenset[EstadoBodySlide] = frozenset(
    {
        EstadoBodySlide.WRITE_OUTSIDE_STAGING,
        EstadoBodySlide.STAGING_TAMPERED,
        EstadoBodySlide.TIMEOUT,
        EstadoBodySlide.CANCELLED,
        EstadoBodySlide.HOLD_LEASE_LOST,
        EstadoBodySlide.OUTPUT_MOD_NOT_READY,
        EstadoBodySlide.OUTPUT_MOD_NOT_OWNED,
        EstadoBodySlide.CONFLICT_BETWEEN_GROUPS,
        EstadoBodySlide.PUBLISH_ROLLED_BACK,
        EstadoBodySlide.PUBLISH_ROLLBACK_PENDING,
        EstadoBodySlide.PUBLISHED,
    }
)


@dataclass(frozen=True, slots=True)
class ResultadoDelServicio:
    """Desenlace tipado de :meth:`BodySlidePipelineService.ejecutar`.

    ``payload`` es EXACTAMENTE el dict que la tool del agente serializaba antes de
    PR 2a (contrato ``success``/``message`` de AGENTS.md, salvo la rama legacy
    ``{"error": ...}`` de runner ausente / protección faltante, que se conserva).
    Invariante: ``success`` es verdadero si y sólo si ``estado`` es ``SUCCESS``;
    cualquier otra combinación falla cerrada al construir.
    """

    estado: EstadoBodySlide
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.estado in ESTADOS_RESERVADOS:
            raise ValueError(f"estado reservado para 2b–2d: {self.estado}")
        exito_del_payload = self.payload.get("success") is True
        if exito_del_payload != (self.estado is EstadoBodySlide.SUCCESS):
            raise ValueError(f"resultado incoherente: estado={self.estado} success={self.payload.get('success')!r}")

    @property
    def success(self) -> bool:
        return self.estado is EstadoBodySlide.SUCCESS

    @property
    def message(self) -> str:
        mensaje = self.payload.get("message", self.payload.get("error", ""))
        return mensaje if isinstance(mensaje, str) else ""


class _ConfigDelRunner(Protocol):
    game_path: pathlib.Path | None


class RunnerDeBodySlide(Protocol):
    """Lo que el servicio usa de ``BodySlideRunner`` (inyectado, no importado:
    el censo de importadores del runner está congelado en
    ``test_animation_tool_consolidation``)."""

    config: _ConfigDelRunner

    async def run_batch(
        self,
        group: str,
        output_path: str,
        *,
        preset: str | None = ...,
        build_morphs: bool = ...,
    ) -> Any: ...


class _BodySlideRunFallidoError(Exception):
    """Interno (U-04): un resultado con ``success=False`` se convierte en excepción
    DENTRO del lock para disparar el rollback real de la salida.

    Mismo patrón que ``pandora_service._RunFallidoError``: tanto
    ``SnapshotTransactionLock`` como ``DirectoryRollback`` sólo restauran en la
    rama de excepción de su ``__aexit__``; un resultado ``success=False`` sale
    "limpio" del ``async with`` si no se puentea, y el backup se descartaría con
    el parcial fallido pisando la salida previa.

    Carga el resultado completo para que el ``except`` arme el MISMO dict de
    salida que el camino no excepcional.
    """

    def __init__(self, result: Any) -> None:
        self.result = result
        super().__init__(getattr(result, "stderr", "") or getattr(result, "stdout", "") or "BodySlide run failed")


def _entradas_del_staging(raiz: pathlib.Path) -> list[str]:
    """Nombres de la raíz del staging de BodySlide; raíz ausente ⇒ ninguno (cierre #1003)."""
    try:
        return [entrada.name for entrada in os.scandir(raiz)]
    except FileNotFoundError:
        return []


def _dict_de_resultado(result: Any) -> dict[str, Any]:
    """Dict de salida compartido por el camino exitoso y el de ``_BodySlideRunFallidoError``.

    Contrato ``success``/``message`` (AGENTS.md, mismo patrón que
    ``pandora_service._dict_de_resultado``): ``message`` canónico vacío en
    éxito, detalle de fallo si no (CodeRabbit, PR #430), saneado con
    ``sanitize_for_prompt`` como ``stdout``/``stderr``.
    """
    # ``message`` del runner primero: para un binario ``/SUBSYSTEM:Windows``
    # ``stdout``/``stderr`` son SIEMPRE vacíos, así que derivar el detalle sólo de
    # ellos producía ``success=False`` con ``message=""``. Se exige ``str`` para
    # que un doble de test que no fije el atributo no filtre un repr de mock al LLM.
    mensaje_del_runner = getattr(result, "message", "")
    if not isinstance(mensaje_del_runner, str):
        mensaje_del_runner = ""
    detalle_fallo = mensaje_del_runner or result.stderr or result.stdout or ""
    return {
        "success": result.success,
        "message": "" if result.success else sanitize_for_prompt(detalle_fallo),
        "return_code": result.return_code,
        "stdout": sanitize_for_prompt(result.stdout) if result.stdout else "",
        "stderr": sanitize_for_prompt(result.stderr) if result.stderr else "",
        "duration_seconds": result.duration_seconds,
    }


def _estado_del_resultado(result: Any) -> EstadoBodySlide:
    """Estado tipado de un resultado del runner.

    ``success is True`` ⇒ ``SUCCESS`` (el dict del agente dice éxito y el tipo no
    puede contradecirlo). En fallo se toma el ``estado`` del runner sólo si
    pertenece al vocabulario, no es ``SUCCESS`` y no es reservado; cualquier otra
    cosa es ``RESULT_WITHOUT_STATE`` (fail-closed, nunca éxito).
    """
    if getattr(result, "success", False) is True:
        return EstadoBodySlide.SUCCESS
    crudo = getattr(result, "estado", None)
    if isinstance(crudo, str):
        try:
            estado = EstadoBodySlide(crudo)
        except ValueError:
            return EstadoBodySlide.RESULT_WITHOUT_STATE
        if estado is not EstadoBodySlide.SUCCESS and estado not in ESTADOS_RESERVADOS:
            return estado
    return EstadoBodySlide.RESULT_WITHOUT_STATE


def _fallo(estado: EstadoBodySlide, detalle: str) -> ResultadoDelServicio:
    return ResultadoDelServicio(estado=estado, payload={"success": False, "message": detalle, "error": detalle})


class BodySlidePipelineService:
    """Orquestación transaccional de una corrida de BodySlide.

    Args:
        runner: ``BodySlideRunner`` (o ``None`` si no está configurado).
        lock_manager: gestor de locks distribuidos; sin él, falla cerrada.
        snapshot_manager: gestor de snapshots; sin él, falla cerrada.
    """

    def __init__(
        self,
        *,
        runner: RunnerDeBodySlide | None,
        lock_manager: Any | None,
        snapshot_manager: Any | None,
    ) -> None:
        self._runner = runner
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager

    async def ejecutar(self, *, group: str, preset: str | None, build_morphs: bool) -> ResultadoDelServicio:
        """Corre BodySlide para ``group`` bajo el lock ``bodyslide-meshes``.

        U-04: el destino físico es ``output_targets.bodyslide_output_target``, un
        subárbol propio POR GRUPO que Sky-Claw administra (la misma propiedad que
        habilita el move-aside de Pandora/DynDOLOD), nunca un path que mande el
        LLM. Sin lock manager o snapshot manager, la ejecución falla cerrada.
        """
        runner = self._runner
        if runner is None:
            return ResultadoDelServicio(
                estado=EstadoBodySlide.RUNNER_NOT_CONFIGURED,
                payload={
                    "error": (
                        "BodySlideRunner is not configured. Set bodyslide_exe in config or install it via setup_tools."
                    )
                },
            )

        missing_managers = [
            name
            for name, manager in (
                ("lock_manager", self._lock_manager),
                ("snapshot_manager", self._snapshot_manager),
            )
            if manager is None
        ]
        if missing_managers:
            return ResultadoDelServicio(
                estado=EstadoBodySlide.PROTECTION_MISSING,
                payload={"error": f"BodySlide requiere protección; faltan: {', '.join(missing_managers)}"},
            )
        lock_manager: Any = self._lock_manager
        snapshot_manager: Any = self._snapshot_manager

        # Imports diferidos, como en la tool original: los tests (y los dobles)
        # parchean ``sky_claw.app.db.locks.SnapshotTransactionLock`` en su módulo.
        from sky_claw.app.db.locks import LockAcquisitionError, SnapshotTransactionLock
        from sky_claw.local.tools._dir_rollback import DirectoryRollback
        from sky_claw.local.tools.bodyslide_headless import (
            HeadlessContractError,
            exigir_identidad_de_grupo_unica,
            validar_nombre_de_grupo,
        )
        from sky_claw.local.tools.output_targets import bodyslide_output_target

        game_path = getattr(getattr(runner, "config", None), "game_path", None)
        target = bodyslide_output_target(game=game_path, group=group)
        if target is None:
            return _fallo(
                EstadoBodySlide.STAGING_UNDETERMINED,
                "No se pudo resolver el destino administrado de BodySlide (game_path no configurado).",
            )

        try:
            async with contextlib.AsyncExitStack() as stack:
                tx = await stack.enter_async_context(
                    SnapshotTransactionLock(
                        lock_manager=lock_manager,
                        snapshot_manager=snapshot_manager,
                        resource_id=BODYSLIDE_MESHES_RESOURCE_ID,
                        agent_id="bodyslide-tool",
                        target_files=[],
                        metadata={"source": "bodyslide_batch", "group": group},
                    )
                )
                # U-04: move-aside DENTRO del lock (mismo orden que pandora_service) —
                # el AsyncExitStack cierra en orden LIFO, así que el restore corre
                # mientras todavía se tiene la exclusividad del lock. El veto de
                # lease (`not tx.lease_lost`) hace que el move-aside y el lock que
                # lo envuelve apliquen el MISMO criterio (review Codex #399).
                # Cierre #1003: identidad del grupo y reglas Win32 ANTES del
                # move-aside — en NTFS el rename de ``.../cbbe`` (o ``CBBE.``)
                # movería la carpeta ``CBBE``.
                try:
                    validar_nombre_de_grupo(group)
                    nombres = await asyncio.to_thread(_entradas_del_staging, target.parent)
                    exigir_identidad_de_grupo_unica(group, nombres)
                except (HeadlessContractError, OSError) as exc:
                    return _fallo(EstadoBodySlide.ARGS_INVALID, sanitize_for_prompt(f"BodySlide no se lanzó: {exc}"))
                await stack.enter_async_context(DirectoryRollback(target, should_rollback=lambda: not tx.lease_lost))
                result = await runner.run_batch(
                    group,
                    str(target),
                    preset=preset,
                    build_morphs=build_morphs,
                )
                if not result.success:
                    # U-04: el puente fallo→excepción. Sin esto, un fallo sale limpio
                    # del stack, DirectoryRollback DESCARTA el backup y el árbol
                    # parcial queda en disco.
                    raise _BodySlideRunFallidoError(result)
        except LockAcquisitionError as exc:
            return _fallo(
                EstadoBodySlide.LOCK_UNAVAILABLE,
                sanitize_for_prompt(f"Could not acquire '{BODYSLIDE_MESHES_RESOURCE_ID}' lock: {exc}"),
            )
        except _BodySlideRunFallidoError as exc:
            return ResultadoDelServicio(
                estado=_estado_del_resultado(exc.result), payload=_dict_de_resultado(exc.result)
            )
        except Exception as exc:  # noqa: BLE001 — boundary de la tool: el contrato devuelve dict, nunca propaga
            # Mismo patrón que ``run_pandora``: una excepción inesperada puede
            # envolver salida cruda del runner.
            return _fallo(EstadoBodySlide.EXECUTION_ERROR, sanitize_for_prompt(str(exc)))

        return ResultadoDelServicio(estado=_estado_del_resultado(result), payload=_dict_de_resultado(result))

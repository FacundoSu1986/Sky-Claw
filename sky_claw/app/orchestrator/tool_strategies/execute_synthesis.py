"""Strategy for the `execute_synthesis_pipeline` tool.

T-27b·2 (ADR 0005): el pipeline ya no corre contra el overwrite real — la
strategy construye el ritual apuntado a ``clone.overwrite_copy`` y delega el
ciclo clonar → correr → diff → HITL → promote/discard en
:class:`~sky_claw.app.orchestrator.sandbox_promotion.SandboxPromotionFlow`.
Ambos colaboradores llegan como providers lazy (mismo patrón que
``PreviewChainStrategy``): cablear el dispatcher nunca exige MO2 presente, y
un provider que falla lo convierte ErrorWrappingMiddleware en error dict.

Sin gate HITL pre-ejecución (double-gating, precedente PR #173): la aprobación
post-run sobre el diff real es estrictamente más fuerte que aprobar a ciegas.

PR-2b3 (ADR 0019 §3.2): con ``synthesis_backend`` distinto de ``standalone`` la salida va a la
raíz administrada ``<mods>/Sky-Claw Synthesis Output``, protegida por el move-aside del
servicio (PR-2b2). Ahí el clon del overwrite no protege nada, así que la strategy corre el
servicio SIN clon y contra el journal real, y adjunta el diff de activación en solo lectura
(``synthesis_activacion``). La decisión sale de ``dominio_de_visibilidad_del_backend``, la misma
función que usa el servicio. La ejecución brokered sigue bloqueada por el HOLD
(``_CONTENCION_SYNTHESIS_VERIFICADA``): el servicio falla cerrado sin fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from sky_claw.app.core.path_resolver import RaicesDeInstanciaMO2
    from sky_claw.app.orchestrator.sandbox_promotion import SandboxPromotionFlow
    from sky_claw.local.mo2.profile_sandbox import SandboxClone
    from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

logger = logging.getLogger(__name__)


class _JournalQueRecuerdaLaTX:
    """Proxy del journal real que recuerda la última TX abierta (PR-2b3, ruta sin clon).

    El servicio no devuelve su ``tx_id`` en el resultado; sin clon tampoco hay ``StagingJournal``
    que lo exponga. El resto de las llamadas se delegan tal cual al journal real.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.ultima_tx: int | None = None

    async def begin_transaction(self, *args: Any, **kwargs: Any) -> int:
        tx_id: int = await self._real.begin_transaction(*args, **kwargs)
        self.ultima_tx = tx_id
        return tx_id

    def __getattr__(self, nombre: str) -> Any:
        return getattr(self._real, nombre)


class ExecuteSynthesisPipelineStrategy:
    name = "execute_synthesis_pipeline"

    def __init__(
        self,
        *,
        flow_provider: Callable[[], SandboxPromotionFlow],
        service_factory: Callable[[pathlib.Path | None, Any], SynthesisPipelineService],
        real_journal_provider: Callable[[], Any],
        synthesis_backend: str = "standalone",
        raices_mo2_provider: Callable[[], RaicesDeInstanciaMO2] | None = None,
        profile_getter: Callable[[], str] | None = None,
    ) -> None:
        from sky_claw.local.mo2.brokered_synthesis import dominio_de_visibilidad_del_backend

        self._flow_provider = flow_provider
        self._service_factory = service_factory
        self._real_journal_provider = real_journal_provider
        self._synthesis_backend = synthesis_backend
        self._raices_mo2_provider = raices_mo2_provider
        self._profile_getter = profile_getter
        #: PR-2b3: ``True`` ⇔ el servicio promueve la raíz administrada (misma función que el servicio).
        self.corre_sin_clon = dominio_de_visibilidad_del_backend(synthesis_backend) == "virtual_usvfs"
        # Referencias fuertes a las resoluciones de journal post-cancelación
        # (el loop solo referencia tasks débilmente; patrón de
        # SandboxPromotionFlow._cleanup_tasks — review Codex #320).
        self._resoluciones_pendientes: set[asyncio.Task[None]] = set()

    async def drain_pendientes(self) -> None:
        """Espera las resoluciones de journal en vuelo (review Codex #322, P2).

        Lo invoca el shutdown del supervisor (vía ``dispatcher.drain()``) ANTES
        de cerrar el journal real: sin esto, una segunda cancelación deja la
        resolución corriendo en background y el cierre del journal puede
        ganarle la carrera, dejando la TX diferida sin estado final.
        """
        while self._resoluciones_pendientes:
            await asyncio.gather(*list(self._resoluciones_pendientes), return_exceptions=True)

    async def _resolver_staged_tras_cancelacion(
        self,
        flow: SandboxPromotionFlow,
        staging_journal: Any,
        tx_id: int,
    ) -> None:
        """Resuelve la TX diferida de un run cancelado según el desenlace real.

        ``flow.desenlace_promocion()`` espera los cleanups en vuelo del promote
        shieldeado y responde si los cambios llegaron al árbol real. El
        FlightReport se omite en esta ruta (telemetría best-effort desde un
        contexto cancelado); el estado del journal es lo crítico. Nunca lanza:
        si la resolución falla, queda el CRITICAL con el tx_id para
        reconciliación manual.
        """
        try:
            desenlace = await flow.desenlace_promocion()
            if desenlace == "aplicada":
                await staging_journal.commit_staged()
            else:
                # "no_aplicada" y "rollback_fallido" marcan la TX rolled_back —
                # mismo criterio que la rama no cancelada (review #310) — pero
                # el segundo NO es un descarte limpio y se alerta abajo.
                await staging_journal.rollback_staged()
            if desenlace == "rollback_fallido":
                # Review Codex #322 (P2): promote falló Y su rollback también.
                # El overwrite real puede estar INCONSISTENTE y el clon se
                # preserva con el backup manual — registrar solo un rollback
                # limpio ocultaría que hace falta recuperación manual.
                logger.critical(
                    "Synthesis cancelado con promote en rollback FALLIDO: TX diferida %s "
                    "marcada rolled_back, pero el overwrite real puede requerir "
                    "restauración manual desde el backup preservado en el clon.",
                    tx_id,
                )
            else:
                logger.warning(
                    "Synthesis cancelado: TX diferida %s resuelta como %s (promote %s aplicó "
                    "cambios al árbol real); FlightReport omitido en esta ruta.",
                    tx_id,
                    "committed" if desenlace == "aplicada" else "rolled_back",
                    "SÍ" if desenlace == "aplicada" else "NO",
                )
        except Exception:
            logger.critical(
                "Synthesis cancelado y NO se pudo resolver la TX diferida %s — "
                "requiere reconciliación manual en el journal real.",
                tx_id,
                exc_info=True,
            )

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        # Filter to only valid parameters — the LLM may inject extra keys
        # (e.g. "tool_name") that would cause TypeError on the service.
        valid_keys = {"patcher_ids", "create_snapshot"}
        filtered = {k: v for k, v in payload_dict.items() if k in valid_keys}
        unexpected = payload_dict.keys() - valid_keys
        if unexpected:
            logger.warning("Dropping unexpected payload keys in %s: %s", self.name, unexpected)

        if self.corre_sin_clon:
            return await self._ejecutar_sin_clon(filtered)

        flow = self._flow_provider()
        real_journal = self._real_journal_provider()

        from sky_claw.app.db.journal import StagingJournal

        staging_journal = StagingJournal(real_journal)

        captured: dict[str, SandboxClone] = {}

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            # Servicio fresco por run, con la salida redirigida a la copia del
            # overwrite (T-27b: el servicio deshabilita su propio snapshot en
            # modo sandbox — el clon ES el rollback).
            captured["clone"] = clone
            service = self._service_factory(clone.overwrite_copy, staging_journal)
            return await service.execute_pipeline(**filtered)

        try:
            result = await flow.run(ritual_name="synthesis", ritual=ritual)
        except asyncio.CancelledError:
            # Review Codex #320 (P1): si la cancelación llegó durante un promote
            # aprobado que terminó COMPLETANDO, los archivos reales quedan
            # aplicados pero este método ya no llega al commit_staged de abajo:
            # la TX diferida quedaba sin estado final en el journal real. Se
            # resuelve acá — commit si el promote aplicó cambios, rollback si
            # no — bajo shield para sobrevivir una segunda cancelación, y la
            # señal propaga igual (contrato asyncio intacto).
            tx_id = staging_journal.staged_transaction_id
            if tx_id is not None:
                resolucion = asyncio.ensure_future(self._resolver_staged_tras_cancelacion(flow, staging_journal, tx_id))
                self._resoluciones_pendientes.add(resolucion)
                resolucion.add_done_callback(self._resoluciones_pendientes.discard)
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(resolucion)
            raise

        # Capturar la TX diferida ANTES de resolver el staged journal
        # (commit_staged/rollback_staged la resetean a None).
        tx_id = staging_journal.staged_transaction_id
        sandbox_info = result.get("sandbox", {}) if isinstance(result, dict) else {}
        reason = result.get("reason") if isinstance(result, dict) else None
        promoted = bool(sandbox_info.get("promoted"))
        if promoted:
            await staging_journal.commit_staged()
        else:
            await staging_journal.rollback_staged()

        if reason == "SandboxRollbackFailed":
            # promote() falló Y su rollback también (SandboxRollbackError): el
            # overwrite real puede quedar INCONSISTENTE y el clon se preserva como
            # backup de restauración manual (el flow NO lo descarta). Emitir un
            # informe de descarte limpio ("rolled_back", rutas del clon) mentiría
            # "nada llegó al real" y ocultaría que hace falta recuperación manual.
            # Se omite el FlightReport; el `reason` del result es la alerta al
            # operador (review Codex #310).
            logger.critical(
                "Synthesis: promote falló con rollback fallido (TX %s); se omite el "
                "FlightReport para no registrar un descarte limpio engañoso.",
                tx_id,
            )
        else:
            # T-28 (ADR 0002): cerrar la caja negra recién ACÁ. Synthesis corre en
            # sandbox con commit diferido, así que el informe compuesto dentro de
            # execute_pipeline reflejaría un estado pre-promoción stale (por eso
            # #309 dejó T-28 como follow-up de esta capa). Ahora la TX real ya
            # tiene su estado final (committed/rolled_back). Las rutas del clon →
            # reales SOLO al promover (mismo criterio que rewrite_clone_paths en
            # _promote, que reescribe únicamente la rama promovida; un descarte no
            # tocó el real).
            await self._emit_flight_report(real_journal, tx_id=tx_id, clone=captured.get("clone") if promoted else None)

        return result

    async def _ejecutar_sin_clon(self, filtered: dict[str, Any]) -> dict[str, Any]:
        """Ruta de la raíz administrada (PR-2b3): servicio sin ``output_path`` y journal real.

        El servicio resuelve su TX (commit, ``rolled_back`` o pendiente) y el move-aside; acá solo se
        cierra la caja negra y, en éxito, se adjunta el diff de activación en solo lectura.
        """
        real_journal = self._real_journal_provider()
        journal = _JournalQueRecuerdaLaTX(real_journal)
        service = self._service_factory(None, journal)
        result = await service.execute_pipeline(**filtered)
        await self._emit_flight_report(real_journal, tx_id=journal.ultima_tx, clone=None)
        if isinstance(result, dict) and result.get("success"):
            result["activacion"] = await self._diff_de_activacion(result, tx_id=journal.ultima_tx)
        return result

    async def _diff_de_activacion(self, result: dict[str, Any], *, tx_id: int | None) -> dict[str, Any]:
        """Diff de ``modlist.txt``/``plugins.txt`` del perfil, sin mutarlos (ADR 0019 §2.3).

        Es informativo: un perfil ilegible o inseguro se reporta como ``disponible=False`` y nunca
        convierte en fallo una corrida que ya confirmó su salida.
        """
        from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
        from sky_claw.local.tools.synthesis_activacion import calcular_diff_de_activacion

        try:
            if self._raices_mo2_provider is None:
                raise ValueError("la strategy no tiene cableado el proveedor de raíces de MO2")
            if self._profile_getter is None:
                raise ValueError("la strategy no tiene cableado el proveedor del perfil")
            perfil = self._profile_getter()
            assert_safe_component(perfil, field="profile")
            directorio = self._raices_mo2_provider().data_root / "profiles" / perfil
            salidas = [str(s) for s in result.get("output_esps") or ()]
            diff = await asyncio.to_thread(calcular_diff_de_activacion, perfil=directorio, salidas=salidas)
        except (OSError, UnicodeDecodeError, ValueError, PathViolationError, RuntimeError) as exc:
            logger.warning(
                "Diff de activación de Synthesis no disponible: %s",
                exc,
                extra={"pipeline_stage": 7, "tx_id": tx_id},
            )
            return {"disponible": False, "motivo": str(exc), "aplicado": False}
        return {"disponible": True, **diff.to_dict()}

    async def _emit_flight_report(self, journal: Any, *, tx_id: int | None, clone: SandboxClone | None) -> None:
        """Compone y persiste el FlightReport de la TX ya resuelta (T-28, best-effort).

        Sin TX (el ritual no llegó a abrirla: fail-closed sin guard, o abortó
        antes de ``begin_transaction``) no hay caja negra que cerrar. Con ``clone``
        se traducen las rutas del clon a las reales del overwrite. Un fallo se
        loguea y NO rompe el resultado del ritual (disciplina best-effort de
        LOOT/xEdit).
        """
        if tx_id is None:
            return
        try:
            from sky_claw.app.orchestrator.preview.flight_report import (
                compose_flight_report_from_journal,
            )

            report = await compose_flight_report_from_journal(journal, transaction_id=tx_id)
            if clone is not None:
                from sky_claw.app.orchestrator.preview.manifest import FlightReport
                from sky_claw.app.orchestrator.sandbox_promotion import rewrite_clone_paths

                report = FlightReport.model_validate(rewrite_clone_paths(report.model_dump(mode="json"), clone))
            # agent_id del servicio de Synthesis (mismo que emite el manifiesto).
            await journal.persist_flight_report(report, agent_id="synthesis-service", transaction_id=tx_id)
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error("Fallo al persistir el FlightReport de la TX %s de Synthesis", tx_id, exc_info=True)

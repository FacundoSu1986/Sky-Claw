"""Tests del flujo promote/discard con aprobación HITL post-run (T-27b·2).

``run_ritual_in_sandbox`` (T-27b·1) devuelve un clon vivo que el caller debe
``promote()`` o ``discard()`` tras aprobación HITL — pero ese bucle de decisión
no existía en ningún caller de producción. ``SandboxPromotionFlow`` es ese
dueño: corre el ritual en sandbox, muestra el diff al operador vía
``HITLGuard`` (categoría ``sandbox_promotion``) y promueve o descarta según la
decisión. Fail-closed en todas las ramas: sin guard se deniega sin correr el
ritual; denegado/timeout descarta; solo APPROVED promueve.

Estos tests usan el ``ProfileSandbox`` REAL contra ``tmp_path`` (mismo estilo
que ``test_sandbox_run.py``): verifican de punta a punta que el overwrite real
solo cambia tras una aprobación explícita.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest

from sky_claw.antigravity.orchestrator.sandbox_promotion import (
    SandboxPromotionFlow,
    format_diff_detail,
)
from sky_claw.antigravity.security.hitl import Decision
from sky_claw.local.mo2.profile_sandbox import (
    FileChange,
    ProfileSandbox,
    SandboxClone,
    SandboxDiff,
    SandboxRollbackError,
)


def _mo2(tmp_path: pathlib.Path) -> pathlib.Path:
    """Instancia MO2 mínima: perfil Default + overwrite vacío."""
    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    profile.mkdir(parents=True)
    (profile / "plugins.txt").write_bytes(b"\xef\xbb\xbf*Skyrim.esm\r\n")
    (mo2 / "overwrite").mkdir()
    return mo2


def _sandbox(tmp_path: pathlib.Path) -> ProfileSandbox:
    return ProfileSandbox(mo2_root=_mo2(tmp_path), sandbox_root=tmp_path / "sandbox")


def _sin_clones_colgados(tmp_path: pathlib.Path) -> bool:
    """True si el sandbox_root no dejó clones huérfanos."""
    root = tmp_path / "sandbox"
    return not root.exists() or list(root.iterdir()) == []


class _GuardFake:
    """HITLGuard mínimo: registra las solicitudes y devuelve una decisión fija.

    ``on_request`` permite simular actividad del operador DURANTE la ventana de
    aprobación (p. ej. drift en el árbol real).
    """

    def __init__(self, decision: Decision, on_request: Any = None) -> None:
        self.decision = decision
        self.requests: list[dict[str, Any]] = []
        self._on_request = on_request

    async def request_approval(
        self,
        request_id: str | None = None,
        reason: str = "",
        url: str | None = None,
        detail: str = "",
        category: str = "scope",
    ) -> Decision:
        self.requests.append({"request_id": request_id, "reason": reason, "detail": detail, "category": category})
        if self._on_request is not None:
            self._on_request()
        return self.decision


async def _ritual_escribe_esp(clone: SandboxClone) -> dict[str, Any]:
    (clone.overwrite_copy / "Synthesis.esp").write_bytes(b"TES4")
    return {"success": True, "message": ""}


class TestSandboxPromotionFlow:
    async def test_aprobado_promueve_al_real_y_descarta_el_clon(self, tmp_path: pathlib.Path) -> None:
        """El flujo completo de T-27: ejecutar → diff → aprobar → promover."""
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=guard)

        resultado = await flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp)

        assert resultado["success"] is True
        assert resultado["sandbox"]["promoted"] is True
        assert resultado["sandbox"]["decision"] == "approved"
        assert resultado["sandbox"]["files_written"] == 1
        assert (tmp_path / "MO2" / "overwrite" / "Synthesis.esp").read_bytes() == b"TES4"
        assert _sin_clones_colgados(tmp_path)

    async def test_la_solicitud_hitl_lleva_categoria_y_diff(self, tmp_path: pathlib.Path) -> None:
        """El operador decide sobre el diff real, con la categoría propia del
        sandbox (nunca auto-aprobable por «Modo local»)."""
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=guard)

        await flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp)

        assert len(guard.requests) == 1
        solicitud = guard.requests[0]
        assert solicitud["category"] == "sandbox_promotion"
        assert "Synthesis.esp" in solicitud["detail"]
        assert "synthesis" in solicitud["reason"]

    async def test_denegado_descarta_y_el_real_queda_intacto(self, tmp_path: pathlib.Path) -> None:
        guard = _GuardFake(Decision.DENIED)
        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=guard)

        resultado = await flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp)

        assert resultado["success"] is False
        assert resultado["reason"] == "SandboxPromotionDenied"
        assert "descartado" in resultado["message"]
        assert resultado["sandbox"]["promoted"] is False
        assert resultado["sandbox"]["decision"] == "denied"
        # La evidencia de qué HABRÍA cambiado viaja en el result.
        assert any(c["relative_path"] == "Synthesis.esp" for c in resultado["sandbox"]["changes"])
        assert list((tmp_path / "MO2" / "overwrite").iterdir()) == []
        assert _sin_clones_colgados(tmp_path)

    async def test_timeout_equivale_a_denegado(self, tmp_path: pathlib.Path) -> None:
        """Fail-secure: sin respuesta del operador, los cambios se descartan."""
        guard = _GuardFake(Decision.TIMEOUT)
        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=guard)

        resultado = await flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp)

        assert resultado["success"] is False
        assert resultado["reason"] == "SandboxPromotionDenied"
        assert list((tmp_path / "MO2" / "overwrite").iterdir()) == []
        assert _sin_clones_colgados(tmp_path)

    async def test_sin_guard_deniega_sin_correr_el_ritual(self, tmp_path: pathlib.Path) -> None:
        """Fail-closed (precedente HitlGateMiddleware): sin canal de aprobación
        no se ejecuta nada — ni siquiera contra el clon."""
        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=None)
        corridas: list[str] = []

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            corridas.append("corrió")
            return {"success": True, "message": ""}

        resultado = await flow.run(ritual_name="synthesis", ritual=ritual)

        assert corridas == []
        assert resultado["success"] is False
        assert resultado["reason"] == "SandboxPromotionUnavailable"
        assert resultado["sandbox"]["decision"] == "unavailable"

    async def test_diff_vacio_descarta_sin_molestar_al_operador(self, tmp_path: pathlib.Path) -> None:
        """Aprobar cero cambios es ruido: sin prompt, el result lo declara."""
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=guard)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            return {"success": True, "message": ""}

        resultado = await flow.run(ritual_name="synthesis", ritual=ritual)

        assert guard.requests == []
        assert resultado["success"] is True
        assert resultado["sandbox"]["promoted"] is False
        assert resultado["sandbox"]["decision"] == "empty_diff"
        assert _sin_clones_colgados(tmp_path)

    async def test_ritual_fallido_descarta_con_el_diff_como_evidencia(self, tmp_path: pathlib.Path) -> None:
        """Escrituras parciales no se promueven jamás; el diff viaja en el
        result para el operador (filosofía caja negra)."""
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=guard)

        async def ritual(clone: SandboxClone) -> dict[str, Any]:
            (clone.overwrite_copy / "parcial.log").write_text("x", encoding="utf-8")
            return {"success": False, "message": "patcher falló"}

        resultado = await flow.run(ritual_name="synthesis", ritual=ritual)

        assert guard.requests == []
        assert resultado["success"] is False
        assert resultado["message"] == "patcher falló"  # el fallo del tool no se reinterpreta
        assert resultado["sandbox"]["decision"] == "ritual_failed"
        assert any(c["relative_path"] == "parcial.log" for c in resultado["sandbox"]["changes"])
        assert list((tmp_path / "MO2" / "overwrite").iterdir()) == []
        assert _sin_clones_colgados(tmp_path)

    async def test_drift_durante_la_aprobacion_descarta_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """Si el árbol real cambió en la ventana de aprobación, promover
        pisaría cambios vivos: el promote corta (drift gate) y el flow lo
        reporta como error accionable."""
        mo2 = _mo2(tmp_path)
        sandbox = ProfileSandbox(mo2_root=mo2, sandbox_root=tmp_path / "sandbox")

        def _operador_toca_el_real() -> None:
            (mo2 / "overwrite" / "ajuste_manual.ini").write_text("x", encoding="utf-8")

        guard = _GuardFake(Decision.APPROVED, on_request=_operador_toca_el_real)
        flow = SandboxPromotionFlow(sandbox=sandbox, hitl_guard=guard)

        resultado = await flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp)

        assert resultado["success"] is False
        assert resultado["reason"] == "SandboxDriftDetected"
        assert resultado["sandbox"]["promoted"] is False
        assert resultado["sandbox"]["decision"] == "drift"
        # El cambio vivo del operador sobrevive; la salida del ritual no se aplicó.
        assert (mo2 / "overwrite" / "ajuste_manual.ini").exists()
        assert not (mo2 / "overwrite" / "Synthesis.esp").exists()
        assert _sin_clones_colgados(tmp_path)

    async def test_rollback_fallido_preserva_el_clon_con_el_backup(self, tmp_path: pathlib.Path) -> None:
        """Si promote falló Y el rollback también, el clon NO se descarta: su
        árbol contiene el backup para restauración manual."""

        class _SandboxRollbackRoto:
            """Sandbox fake: solo el promote está roto (rollback incluido)."""

            def __init__(self, real: ProfileSandbox) -> None:
                self._real = real
                self.descartes = 0

            async def clone(self) -> SandboxClone:
                return await self._real.clone()

            async def diff(self, clone: SandboxClone) -> SandboxDiff:
                return await self._real.diff(clone)

            async def promote(self, clone: SandboxClone) -> Any:
                raise SandboxRollbackError(f"Backup para restauración manual en: {clone.root / 'rollback-x'}")

            async def discard(self, clone: SandboxClone) -> None:
                self.descartes += 1
                await self._real.discard(clone)

        sandbox = _SandboxRollbackRoto(_sandbox(tmp_path))
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=sandbox, hitl_guard=guard)  # type: ignore[arg-type]

        resultado = await flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp)

        assert resultado["success"] is False
        assert resultado["reason"] == "SandboxRollbackFailed"
        assert "rollback-x" in resultado["message"]  # la ruta del backup es accionable
        assert resultado["sandbox"]["decision"] == "rollback_failed"
        assert sandbox.descartes == 0  # el clon (y su backup) quedan en disco
        assert not _sin_clones_colgados(tmp_path)

    async def test_cancelacion_durante_la_aprobacion_descarta_y_propaga(self, tmp_path: pathlib.Path) -> None:
        """Mismo contrato que sandbox_run: la cancelación no se traga ni deja
        clones colgados."""

        class _GuardCancelado:
            async def request_approval(self, **kwargs: Any) -> Decision:
                raise asyncio.CancelledError()

        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=_GuardCancelado())  # type: ignore[arg-type]

        with pytest.raises(asyncio.CancelledError):
            await flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp)

        assert _sin_clones_colgados(tmp_path)


class TestCancelacionDurantePromote:
    """F2 (auditoría 2026-07-18): ``promote()`` muta el árbol REAL dentro de
    ``asyncio.to_thread`` — cancelar la task no interrumpe ese hilo. El flow
    debe observar el desenlace real del promote antes de limpiar: descartar el
    clon con el promote aún corriendo rompería su rollback interno (los
    backups viven en el árbol del clon)."""

    class _SandboxPromoteLento:
        """Sandbox fake: promote bloqueante y observable, resto delega al real.

        ``promote_iniciado``/``liberar_promote`` simulan la ventana en la que
        el "hilo" de promote sigue vivo tras la cancelación del caller.
        """

        def __init__(self, real: ProfileSandbox, resultado_promote: Any) -> None:
            self._real = real
            self._resultado_promote = resultado_promote
            self.descartes = 0
            self.promote_iniciado = asyncio.Event()
            self.liberar_promote = asyncio.Event()
            self.promote_completado = False

        async def clone(self) -> SandboxClone:
            return await self._real.clone()

        async def diff(self, clone: SandboxClone) -> SandboxDiff:
            return await self._real.diff(clone)

        async def promote(self, clone: SandboxClone) -> Any:
            self.promote_iniciado.set()
            await self.liberar_promote.wait()
            self.promote_completado = True
            if isinstance(self._resultado_promote, Exception):
                raise self._resultado_promote
            return self._resultado_promote

        async def discard(self, clone: SandboxClone) -> None:
            self.descartes += 1
            await self._real.discard(clone)

    async def _correr_y_cancelar_en_promote(
        self, sandbox: _SandboxPromoteLento
    ) -> tuple[asyncio.Task[dict[str, Any]], SandboxPromotionFlow]:
        """Lanza el flow, espera a que el promote arranque y cancela la task."""
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=sandbox, hitl_guard=guard)  # type: ignore[arg-type]
        task = asyncio.create_task(flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp))
        await asyncio.wait_for(sandbox.promote_iniciado.wait(), timeout=2.0)
        task.cancel()
        return task, flow

    async def _esperar(self, condicion: Any, timeout: float = 2.0) -> None:
        """Drena el loop hasta que ``condicion()`` sea verdadera (cleanup shieldeado)."""
        async with asyncio.timeout(timeout):
            while not condicion():
                await asyncio.sleep(0)

    async def test_cancelacion_espera_el_desenlace_del_promote_y_descarta(self, tmp_path: pathlib.Path) -> None:
        """La cancelación propaga, pero recién después de observar que el
        promote terminó; el clon se descarta y no queda huérfano."""
        from sky_claw.local.mo2.profile_sandbox import PromoteResult

        sandbox = self._SandboxPromoteLento(
            _sandbox(tmp_path), resultado_promote=PromoteResult(files_written=1, files_deleted=0)
        )
        task, flow = await self._correr_y_cancelar_en_promote(sandbox)
        # El promote "sigue corriendo en el hilo": liberar su desenlace.
        sandbox.liberar_promote.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert sandbox.promote_completado is True  # el desenlace fue observado
        await self._esperar(lambda: sandbox.descartes == 1)
        assert _sin_clones_colgados(tmp_path)

    async def test_cancelacion_no_descarta_mientras_el_promote_sigue_vivo(self, tmp_path: pathlib.Path) -> None:
        """El discard NO puede correr con el promote todavía en vuelo: borraría
        los backups del rollback interno que viven en el árbol del clon."""
        from sky_claw.local.mo2.profile_sandbox import PromoteResult

        sandbox = self._SandboxPromoteLento(
            _sandbox(tmp_path), resultado_promote=PromoteResult(files_written=1, files_deleted=0)
        )
        task, flow = await self._correr_y_cancelar_en_promote(sandbox)
        # Darle varios ciclos al loop SIN liberar el promote.
        for _ in range(20):
            await asyncio.sleep(0)

        assert sandbox.descartes == 0  # el clon sigue intacto bajo el promote vivo
        assert not task.done()  # la cancelación espera el desenlace real

        sandbox.liberar_promote.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await self._esperar(lambda: sandbox.descartes == 1)

    async def test_cancelacion_con_rollback_fallido_preserva_el_clon(self, tmp_path: pathlib.Path) -> None:
        """Mismo contrato que la rama no cancelada: tras SandboxRollbackError el
        clon NO se descarta — su árbol contiene el backup manual."""
        sandbox = self._SandboxPromoteLento(
            _sandbox(tmp_path),
            resultado_promote=SandboxRollbackError("Backup para restauración manual en: rollback-x"),
        )
        task, flow = await self._correr_y_cancelar_en_promote(sandbox)
        sandbox.liberar_promote.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        await self._esperar(lambda: sandbox.promote_completado)
        for _ in range(20):  # chance de que un discard indebido corra
            await asyncio.sleep(0)
        assert sandbox.descartes == 0
        assert not _sin_clones_colgados(tmp_path)  # el backup queda en disco

    async def test_cancelar_el_cleanup_no_descarta_con_el_promote_vivo(self, tmp_path: pathlib.Path) -> None:
        """Review Codex PR #320: si el teardown cancela a la task de cleanup
        directamente, eso NO prueba que el promote paró — descartar el clon
        borraría los backups que su hilo todavía necesita."""
        from sky_claw.local.mo2.profile_sandbox import PromoteResult

        sandbox = self._SandboxPromoteLento(
            _sandbox(tmp_path), resultado_promote=PromoteResult(files_written=1, files_deleted=0)
        )
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=sandbox, hitl_guard=guard)  # type: ignore[arg-type]
        task = asyncio.create_task(flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp))
        await asyncio.wait_for(sandbox.promote_iniciado.wait(), timeout=2.0)

        task.cancel()  # 1ª cancelación → arranca el cleanup
        await self._esperar(lambda: len(flow._cleanup_tasks) == 1)
        cleanup = next(iter(flow._cleanup_tasks))
        cleanup.cancel()  # teardown cancela el cleanup con el promote aún vivo

        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(20):
            await asyncio.sleep(0)
        assert sandbox.descartes == 0  # el clon (y sus backups) quedan intactos

        # El "hilo" del promote termina después; nadie debe tocar el clon igual.
        sandbox.liberar_promote.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert sandbox.descartes == 0

    async def test_desenlace_promocion_aplicada_si_el_promote_completo_pese_al_cancel(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Review Codex #320 (P1 línea 287): el caller cancelado necesita saber
        si el promote terminó aplicando cambios al árbol real, para resolver su
        journal diferido (commit_staged) en vez de dejarlo sin estado final."""
        from sky_claw.local.mo2.profile_sandbox import PromoteResult

        sandbox = self._SandboxPromoteLento(
            _sandbox(tmp_path), resultado_promote=PromoteResult(files_written=1, files_deleted=0)
        )
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=sandbox, hitl_guard=guard)  # type: ignore[arg-type]
        task = asyncio.create_task(flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp))
        await asyncio.wait_for(sandbox.promote_iniciado.wait(), timeout=2.0)

        task.cancel()
        sandbox.liberar_promote.set()  # el "hilo" del promote completa igual
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await asyncio.wait_for(flow.desenlace_promocion(), timeout=2.0) == "aplicada"

    async def test_desenlace_promocion_no_aplicada_si_el_promote_nunca_corrio(self, tmp_path: pathlib.Path) -> None:
        """Cancelación antes del promote (en la ventana HITL): ningún cambio
        llegó al árbol real — el journal diferido debe revertirse."""

        class _GuardCancelado:
            async def request_approval(self, **kwargs: Any) -> Decision:
                raise asyncio.CancelledError()

        flow = SandboxPromotionFlow(sandbox=_sandbox(tmp_path), hitl_guard=_GuardCancelado())  # type: ignore[arg-type]

        with pytest.raises(asyncio.CancelledError):
            await flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp)

        assert await asyncio.wait_for(flow.desenlace_promocion(), timeout=2.0) == "no_aplicada"

    async def test_desenlace_promocion_distingue_el_rollback_fallido(self, tmp_path: pathlib.Path) -> None:
        """Review Codex #322 (P2): promote fallido CON rollback fallido no es un
        descarte limpio — el árbol real puede estar inconsistente y el caller
        debe alertar recuperación manual, no registrar "no_aplicada"."""
        sandbox = self._SandboxPromoteLento(
            _sandbox(tmp_path),
            resultado_promote=SandboxRollbackError("Backup para restauración manual en: rollback-x"),
        )
        task, flow = await self._correr_y_cancelar_en_promote(sandbox)
        sandbox.liberar_promote.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert await asyncio.wait_for(flow.desenlace_promocion(), timeout=2.0) == "rollback_fallido"

    async def test_desenlace_promocion_espera_al_promote_si_el_cleanup_fue_cancelado(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Review Codex #322 (P2): un cleanup cancelado por el teardown sale de
        _cleanup_tasks sin observar el promote — desenlace_promocion debe
        esperar a la task del promote igual, no responder "no_aplicada" con los
        cambios todavía aterrizando en el árbol real."""
        from sky_claw.local.mo2.profile_sandbox import PromoteResult

        sandbox = self._SandboxPromoteLento(
            _sandbox(tmp_path), resultado_promote=PromoteResult(files_written=1, files_deleted=0)
        )
        guard = _GuardFake(Decision.APPROVED)
        flow = SandboxPromotionFlow(sandbox=sandbox, hitl_guard=guard)  # type: ignore[arg-type]
        task = asyncio.create_task(flow.run(ritual_name="synthesis", ritual=_ritual_escribe_esp))
        await asyncio.wait_for(sandbox.promote_iniciado.wait(), timeout=2.0)

        task.cancel()  # 1ª cancelación → arranca el cleanup
        await self._esperar(lambda: len(flow._cleanup_tasks) == 1)
        next(iter(flow._cleanup_tasks)).cancel()  # teardown cancela el cleanup
        with pytest.raises(asyncio.CancelledError):
            await task

        desenlace_task = asyncio.create_task(flow.desenlace_promocion())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not desenlace_task.done()  # espera al promote, no responde en falso

        sandbox.liberar_promote.set()  # el "hilo" del promote completa después
        assert await asyncio.wait_for(desenlace_task, timeout=2.0) == "aplicada"


class TestFormatDiffDetail:
    def test_resume_conteo_y_lista_los_primeros_paths(self) -> None:
        diff = SandboxDiff(
            changes=(
                FileChange(area="overwrite", relative_path="Synthesis.esp", kind="added"),
                FileChange(area="profile", relative_path="plugins.txt", kind="modified"),
                FileChange(area="overwrite", relative_path="viejo.log", kind="removed"),
            )
        )

        detalle = format_diff_detail(diff)

        assert "3 cambio(s)" in detalle
        assert "+ overwrite/Synthesis.esp" in detalle
        assert "~ profile/plugins.txt" in detalle
        assert "- overwrite/viejo.log" in detalle

    def test_trunca_diffs_largos_sin_pasarse_del_limite(self) -> None:
        diff = SandboxDiff(
            changes=tuple(
                FileChange(area="overwrite", relative_path=f"meshes/{i:04d}_{'x' * 60}.nif", kind="added")
                for i in range(200)
            )
        )

        detalle = format_diff_detail(diff)

        assert len(detalle) <= 800
        assert "200 cambio(s)" in detalle
        assert "+ overwrite/meshes/0000_" in detalle  # los primeros sí se listan

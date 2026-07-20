"""Tests for ARC-01 and ARC-03: AppContext teardown resilience and zombie prevention.

ARC-01: database.close() failure during teardown must not prevent exit-stack
reconstruction on the next start_full() call.

ARC-03: After a failed start_full(), all mutable references must be nulled so
that is_configured returns False and callers do not use closed/zombie objects.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import threading
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app_context import AppContext


@pytest.fixture
def mock_args(tmp_path: pathlib.Path):
    """Minimal argparse-like namespace for AppContext."""
    args = argparse.Namespace(
        db_path=str(tmp_path / "test.db"),
        mo2_root=tmp_path / "MO2",
        staging_dir=tmp_path / "staging",
        provider="ollama",
        operator_chat_id=None,
        loot_exe=None,
        install_dir=None,
        mode="cli",
    )
    return args


class TestAppContextResilience:
    """ARC-01 + ARC-03: Teardown atomicity and zombie reference nulling."""

    @pytest.mark.asyncio
    async def test_teardown_survives_database_close_failure(self, mock_args, caplog):
        """ARC-01: el error primario sobrevive y el cierre fallido queda retenido."""
        ctx = AppContext(mock_args)
        TestAppContextLifecycleCoordinator._aislar_fase_minima(ctx)
        db_error = RuntimeError("DB close failure")
        ctx.database.close = AsyncMock(side_effect=[db_error, None])

        async def fallar_tarde() -> None:
            ctx._push_startup_cleanup(ctx.database.close)
            raise RuntimeError("forced router failure")

        ctx._start_full_inner = fallar_tarde
        with (
            caplog.at_level("ERROR", logger="sky_claw"),
            pytest.raises(RuntimeError, match="forced router failure"),
        ):
            await ctx.start_full()

        assert "cleanup retenido para retry" in caplog.text
        ctx.database.close.assert_awaited_once()
        await ctx.stop()
        assert ctx.database.close.await_count == 2

    @pytest.mark.asyncio
    async def test_references_nulled_after_failed_start_full(self, mock_args):
        """ARC-03: After start_full() fails, mutable refs must be None."""
        ctx = AppContext(mock_args)
        TestAppContextLifecycleCoordinator._aislar_fase_minima(ctx)

        async def fallar_full() -> None:
            ctx.router = MagicMock()
            ctx.polling = MagicMock()
            ctx.hitl = MagicMock()
            ctx.sender = MagicMock()
            ctx.sync_engine = MagicMock()
            ctx.tools_installer = MagicMock()
            raise RuntimeError("forced init failure")

        ctx._start_full_inner = fallar_full

        with pytest.raises(RuntimeError, match="forced init failure"):
            await ctx.start_full()

        # ARC-03: After rollback, references must be nulled
        assert ctx.router is None
        assert ctx.polling is None
        assert ctx.hitl is None
        assert ctx.sender is None
        assert ctx.sync_engine is None
        assert ctx.tools_installer is None


class TestAppContextLifecycleCoordinator:
    """Regresiones del coordinador cancelable y del cleanup reintentable."""

    @staticmethod
    def _aislar_fase_minima(ctx: AppContext) -> None:
        ctx._resolve_config_path = MagicMock()
        ctx._migrate_legacy_json = MagicMock()
        ctx.lifecycle.initialize = AsyncMock()
        ctx.network.initialize = AsyncMock()

    @pytest.mark.asyncio
    async def test_stop_reintenta_callback_fallido_con_misma_identidad(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        error = RuntimeError("network close failure")
        ctx.network.close = AsyncMock(side_effect=[error, None])
        ctx.lifecycle.close = AsyncMock()

        await ctx.start_minimal()

        with pytest.raises(RuntimeError) as raised:
            await ctx.stop()
        assert raised.value is error

        await ctx.stop()

        assert ctx.network.close.await_count == 2

    @pytest.mark.asyncio
    async def test_stop_conserva_mismo_lifo_si_fallan_dos_callbacks(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        eventos: list[str] = []
        intentos = {"network": 0, "lifecycle": 0}

        async def cerrar_network() -> None:
            eventos.append("network")
            intentos["network"] += 1
            if intentos["network"] == 1:
                raise RuntimeError("network close failure")

        async def cerrar_lifecycle() -> None:
            eventos.append("lifecycle")
            intentos["lifecycle"] += 1
            if intentos["lifecycle"] == 1:
                raise RuntimeError("lifecycle close failure")

        ctx.network.close = cerrar_network
        ctx.lifecycle.close = cerrar_lifecycle
        await ctx.start_minimal()

        with pytest.raises(RuntimeError):
            await ctx.stop()
        await ctx.stop()

        assert eventos == ["network", "lifecycle", "network", "lifecycle"]

    @pytest.mark.asyncio
    async def test_preflight_fallido_no_reintenta_en_mismo_start(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        ctx.lifecycle.close = AsyncMock()
        segundo_cierre = RuntimeError("retained cleanup still failing")
        ctx.network.close = AsyncMock(
            side_effect=[None, RuntimeError("rollback cleanup failure"), segundo_cierre, None]
        )

        await ctx.start_minimal()

        async def fallar_full() -> None:
            raise RuntimeError("full startup failure")

        ctx._start_full_inner = fallar_full
        with pytest.raises(RuntimeError, match="full startup failure"):
            await ctx.start_full()

        inicializaciones_antes = ctx.lifecycle.initialize.await_count
        with pytest.raises(RuntimeError) as raised:
            await ctx.start_minimal()
        assert raised.value is segundo_cierre
        assert ctx.lifecycle.initialize.await_count == inicializaciones_antes

        await ctx.stop()
        assert ctx.network.close.await_count == 4
        await ctx.stop()
        assert ctx.network.close.await_count == 4

    @pytest.mark.asyncio
    async def test_preflight_fallido_sanea_referencias_sin_retry_inmediato(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        cleanup_error = RuntimeError("retained cleanup failure")
        cleanup = AsyncMock(side_effect=[cleanup_error, None])
        ctx._push_startup_cleanup(cleanup)
        ctx.router = MagicMock()
        ctx.polling = MagicMock()
        ctx.hitl = MagicMock()
        ctx.sender = MagicMock()
        ctx.sync_engine = MagicMock()
        ctx.tools_installer = MagicMock()

        with pytest.raises(RuntimeError) as raised:
            await ctx.start_minimal()

        assert raised.value is cleanup_error
        cleanup.assert_awaited_once()
        assert ctx.router is None
        assert ctx.polling is None
        assert ctx.hitl is None
        assert ctx.sender is None
        assert ctx.sync_engine is None
        assert ctx.tools_installer is None

        await ctx.stop()
        assert cleanup.await_count == 2

    @pytest.mark.asyncio
    async def test_fallo_posterior_a_adquisicion_hace_un_solo_rollback(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        ctx.lifecycle.close = AsyncMock()
        ctx.network.close = AsyncMock()

        async def fallar_full() -> None:
            raise RuntimeError("full startup failure")

        ctx._start_full_inner = fallar_full

        with pytest.raises(RuntimeError, match="full startup failure"):
            await ctx.start_full()

        ctx.network.close.assert_awaited_once()
        ctx.lifecycle.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_cancela_start_full_antes_de_tomar_lifecycle_lock(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        ctx.lifecycle.close = AsyncMock()
        ctx.network.close = AsyncMock()
        entro = asyncio.Event()
        rollback = asyncio.Event()

        async def cleanup() -> None:
            rollback.set()

        async def bloquear_full() -> None:
            ctx._exit_stack.push_async_callback(cleanup)
            entro.set()
            await asyncio.wait_for(asyncio.Event().wait(), timeout=10.0)

        ctx._start_full_inner = bloquear_full
        startup = asyncio.create_task(ctx.start_full())
        try:
            await asyncio.wait_for(entro.wait(), timeout=1.0)
            await asyncio.wait_for(ctx.stop(), timeout=1.0)
            await asyncio.wait_for(rollback.wait(), timeout=1.0)
            assert startup.cancelled()
            assert ctx.is_configured is False
        finally:
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_start_en_cola_anterior_a_stop_no_ejecuta_fases(self, mock_args):
        ctx = AppContext(mock_args)
        primera_fase = asyncio.Event()
        segunda_fase = asyncio.Event()
        llamadas = 0

        async def bloquear_minimal() -> None:
            nonlocal llamadas
            llamadas += 1
            if llamadas == 1:
                primera_fase.set()
            else:
                segunda_fase.set()
            await asyncio.wait_for(asyncio.Event().wait(), timeout=10.0)

        async def full_noop() -> None:
            return None

        ctx.start_minimal = bloquear_minimal
        ctx.start_full = full_noop
        primero = asyncio.create_task(ctx.start())
        segundo: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(primera_fase.wait(), timeout=1.0)
            segundo = asyncio.create_task(ctx.start())
            await asyncio.wait_for(ctx.stop(), timeout=1.0)
            terminadas, _ = await asyncio.wait({primero, segundo}, timeout=0.1)

            assert terminadas == {primero, segundo}
            assert llamadas == 1
            assert not segunda_fase.is_set()
            assert primero.cancelled()
            assert segundo.cancelled()
        finally:
            for tarea in (primero, segundo):
                if tarea is not None and not tarea.done():
                    tarea.cancel()
            await asyncio.gather(
                *(tarea for tarea in (primero, segundo) if tarea is not None),
                return_exceptions=True,
            )

    @pytest.mark.asyncio
    async def test_stop_agota_presupuesto_si_startup_ignora_cancelacion(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        ctx.lifecycle.close = AsyncMock()
        ctx.network.close = AsyncMock()
        ctx._startup_shutdown_timeout_s = 0.01
        entro = asyncio.Event()
        ignoro_cancelacion = asyncio.Event()
        liberar = asyncio.Event()

        async def full_obstinado() -> None:
            entro.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=10.0)
            except asyncio.CancelledError:
                ignoro_cancelacion.set()
                await asyncio.wait_for(liberar.wait(), timeout=1.0)
            ctx.router = MagicMock()

        ctx._start_full_inner = full_obstinado
        startup = asyncio.create_task(ctx.start_full())
        try:
            await asyncio.wait_for(entro.wait(), timeout=1.0)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ctx.stop(), timeout=1.0)
            await asyncio.wait_for(ignoro_cancelacion.wait(), timeout=1.0)
            assert ctx.is_configured is False

            liberar.set()
            await asyncio.gather(startup, return_exceptions=True)
            assert startup.cancelled()
            assert ctx.router is None
            await asyncio.wait_for(ctx.stop(), timeout=1.0)
        finally:
            liberar.set()
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_segunda_cancelacion_no_interrumpe_rollback_en_curso(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        ctx.lifecycle.close = AsyncMock()
        ctx.network.close = AsyncMock()
        entro = asyncio.Event()
        cleanup_inicio = asyncio.Event()
        cleanup_fin = asyncio.Event()
        liberar_cleanup = asyncio.Event()

        async def cleanup_lento() -> None:
            cleanup_inicio.set()
            try:
                await asyncio.wait_for(liberar_cleanup.wait(), timeout=1.0)
            finally:
                cleanup_fin.set()

        async def bloquear_full() -> None:
            ctx._push_startup_cleanup(cleanup_lento)
            entro.set()
            await asyncio.wait_for(asyncio.Event().wait(), timeout=10.0)

        ctx._start_full_inner = bloquear_full
        startup = asyncio.create_task(ctx.start_full())
        stop_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entro.wait(), timeout=1.0)
            stop_task = asyncio.create_task(ctx.stop())
            await asyncio.wait_for(cleanup_inicio.wait(), timeout=1.0)

            startup.cancel()
            await asyncio.sleep(0)
            assert cleanup_fin.is_set() is False

            liberar_cleanup.set()
            await asyncio.wait_for(stop_task, timeout=1.0)
            await asyncio.wait_for(cleanup_fin.wait(), timeout=1.0)
            assert startup.cancelled()
        finally:
            liberar_cleanup.set()
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            if stop_task is not None and not stop_task.done():
                await asyncio.wait_for(stop_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_stop_no_cuelga_si_background_task_ignora_cancelacion(self, mock_args):
        ctx = AppContext(mock_args)
        ctx._startup_shutdown_timeout_s = 0.01
        entro = asyncio.Event()
        ignoro_cancelacion = asyncio.Event()
        liberar = asyncio.Event()

        async def tarea_obstinada() -> None:
            entro.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=10.0)
            except asyncio.CancelledError:
                ignoro_cancelacion.set()
                await asyncio.wait_for(liberar.wait(), timeout=1.0)

        background = ctx._track_task(tarea_obstinada(), name="background-obstinado")
        stop_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entro.wait(), timeout=1.0)
            stop_task = asyncio.create_task(ctx.stop())
            terminadas, _ = await asyncio.wait({stop_task}, timeout=0.1)
            assert stop_task in terminadas
            with pytest.raises(TimeoutError):
                stop_task.result()
            await asyncio.wait_for(ignoro_cancelacion.wait(), timeout=1.0)
        finally:
            liberar.set()
            if stop_task is not None and not stop_task.done():
                await asyncio.wait_for(stop_task, timeout=1.0)
            await asyncio.gather(background, return_exceptions=True)

        await asyncio.wait_for(ctx.stop(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_stop_acota_cleanup_obstinado_sin_ejecutarlo_dos_veces(self, mock_args):
        ctx = AppContext(mock_args)
        ctx._startup_shutdown_timeout_s = 0.01
        entro = asyncio.Event()
        liberar = asyncio.Event()
        ejecuciones = 0

        async def cleanup_obstinado() -> None:
            nonlocal ejecuciones
            ejecuciones += 1
            entro.set()
            await liberar.wait()

        ctx._push_startup_cleanup(cleanup_obstinado)
        loop = asyncio.get_running_loop()
        liberacion_de_emergencia = loop.call_later(0.1, liberar.set)

        with pytest.raises(TimeoutError):
            await ctx.stop()
        liberacion_de_emergencia.cancel()
        assert ejecuciones == 1

        segundo_stop = asyncio.create_task(ctx.stop())
        try:
            await asyncio.sleep(0)
            assert ejecuciones == 1
            liberar.set()
            await asyncio.wait_for(segundo_stop, timeout=0.2)
        finally:
            liberar.set()
            if not segundo_stop.done():
                await asyncio.wait_for(segundo_stop, timeout=0.2)

        assert ejecuciones == 1

    @pytest.mark.asyncio
    async def test_cancelacion_repetida_de_stop_preserva_la_primaria(self, mock_args):
        ctx = AppContext(mock_args)
        ctx._startup_shutdown_timeout_s = 0.2
        entro = asyncio.Event()
        liberar = asyncio.Event()

        async def cleanup_lento() -> None:
            entro.set()
            await liberar.wait()

        ctx._push_startup_cleanup(cleanup_lento)
        stop_task = asyncio.create_task(ctx.stop())
        try:
            await asyncio.wait_for(entro.wait(), timeout=0.2)
            stop_task.cancel("cancelacion-primaria")
            await asyncio.sleep(0)
            stop_task.cancel("cancelacion-secundaria")
            liberar.set()

            with pytest.raises(asyncio.CancelledError) as raised:
                await stop_task
            assert str(raised.value) == "cancelacion-primaria"
        finally:
            liberar.set()
            await asyncio.gather(stop_task, return_exceptions=True)

        await ctx.stop()

    @pytest.mark.asyncio
    async def test_cleanup_falla_despues_del_timeout_y_conserva_retry(self, mock_args):
        ctx = AppContext(mock_args)
        ctx._startup_shutdown_timeout_s = 0.01
        entro = asyncio.Event()
        liberar = asyncio.Event()
        fallo_emitido = asyncio.Event()
        error = RuntimeError("cleanup failure after timeout")
        ejecuciones = 0

        async def cleanup_tardio() -> None:
            nonlocal ejecuciones
            ejecuciones += 1
            if ejecuciones == 1:
                entro.set()
                await liberar.wait()
                fallo_emitido.set()
                raise error

        ctx._push_startup_cleanup(cleanup_tardio)
        loop = asyncio.get_running_loop()
        liberacion_de_emergencia = loop.call_later(0.1, liberar.set)

        with pytest.raises(TimeoutError):
            await ctx.stop()
        liberacion_de_emergencia.cancel()
        assert ejecuciones == 1

        liberar.set()
        await asyncio.wait_for(fallo_emitido.wait(), timeout=0.2)
        with pytest.raises(RuntimeError) as raised:
            await ctx.stop()
        assert raised.value is error
        assert ejecuciones == 1

        await ctx.stop()
        assert ejecuciones == 2

    @pytest.mark.asyncio
    async def test_startup_preserva_error_primario_si_cleanup_agota_presupuesto(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        ctx.lifecycle.close = AsyncMock()
        ctx.network.close = AsyncMock()
        ctx._startup_shutdown_timeout_s = 0.01
        entro = asyncio.Event()
        liberar = asyncio.Event()
        error_primario = RuntimeError("startup primary failure")
        ejecuciones = 0

        async def cleanup_obstinado() -> None:
            nonlocal ejecuciones
            ejecuciones += 1
            entro.set()
            await liberar.wait()

        async def full_fallido() -> None:
            ctx.router = MagicMock()
            ctx.hitl = MagicMock()
            ctx._push_startup_cleanup(cleanup_obstinado)
            raise error_primario

        ctx._start_full_inner = full_fallido
        loop = asyncio.get_running_loop()
        liberacion_de_emergencia = loop.call_later(0.5, liberar.set)
        startup = asyncio.create_task(ctx.start_full())
        done, _ = await asyncio.wait({startup}, timeout=0.2)
        if startup not in done:
            liberar.set()
            await asyncio.gather(startup, return_exceptions=True)
        assert startup in done
        with pytest.raises(RuntimeError) as raised:
            startup.result()
        assert raised.value is error_primario
        liberacion_de_emergencia.cancel()
        assert ejecuciones == 1
        assert ctx.router is None
        assert ctx.hitl is None

        inicializaciones_antes = ctx.lifecycle.initialize.await_count
        with pytest.raises(TimeoutError):
            await ctx.start_minimal()
        assert ctx.lifecycle.initialize.await_count == inicializaciones_antes
        assert ejecuciones == 1

        liberar.set()
        await ctx.start_minimal()
        assert ejecuciones == 1
        await ctx.stop()

    @pytest.mark.asyncio
    async def test_start_minimal_limpia_referencias_full_previas(self, mock_args):
        ctx = AppContext(mock_args)
        self._aislar_fase_minima(ctx)
        ctx.lifecycle.close = AsyncMock()
        ctx.network.close = AsyncMock()
        ctx.sandbox_validator = MagicMock()
        ctx.install_dir = pathlib.Path("tools-previos")
        ctx.sender = MagicMock()
        ctx.hitl = MagicMock()
        ctx.sync_engine = MagicMock()
        ctx.tools_installer = MagicMock()
        ctx.router = MagicMock()
        ctx.polling = MagicMock()
        ctx._full_start_committed = True

        await ctx.start_minimal()

        assert ctx.sandbox_validator is None
        assert ctx.install_dir is None
        assert ctx.sender is None
        assert ctx.hitl is None
        assert ctx.sync_engine is None
        assert ctx.tools_installer is None
        assert ctx.router is None
        assert ctx.polling is None
        assert ctx.is_configured is False
        await ctx.stop()

    @pytest.mark.asyncio
    async def test_fallo_parcial_minimal_cierra_objeto_antes_de_repropagar(self, mock_args):
        ctx = AppContext(mock_args)
        ctx._resolve_config_path = MagicMock()
        ctx._migrate_legacy_json = MagicMock()
        recurso_abierto = False
        cierres = 0

        async def inicializar_lifecycle() -> None:
            nonlocal recurso_abierto
            recurso_abierto = True
            raise RuntimeError("partial lifecycle failure")

        async def cerrar_lifecycle() -> None:
            nonlocal recurso_abierto, cierres
            cierres += 1
            recurso_abierto = False

        ctx.lifecycle.initialize = inicializar_lifecycle
        ctx.lifecycle.close = cerrar_lifecycle
        ctx.network.initialize = AsyncMock()

        with pytest.raises(RuntimeError, match="partial lifecycle failure"):
            await ctx.start_minimal()

        assert recurso_abierto is False
        assert cierres == 1

    @pytest.mark.asyncio
    async def test_fallo_parcial_network_cierra_misma_instancia(self, mock_args):
        ctx = AppContext(mock_args)
        ctx._resolve_config_path = MagicMock()
        ctx._migrate_legacy_json = MagicMock()
        ctx.lifecycle.initialize = AsyncMock()
        ctx.lifecycle.close = AsyncMock()
        recurso_abierto = False
        cierres: list[object] = []

        async def inicializar_network(_key: str, _staging: object) -> None:
            nonlocal recurso_abierto
            recurso_abierto = True
            raise RuntimeError("partial network failure")

        async def cerrar_network() -> None:
            nonlocal recurso_abierto
            cierres.append(ctx.network)
            recurso_abierto = False

        ctx.network.initialize = inicializar_network
        ctx.network.close = cerrar_network

        with pytest.raises(RuntimeError, match="partial network failure"):
            await ctx.start_minimal()

        assert recurso_abierto is False
        assert cierres == [ctx.network]


class TestAppContextPartialFullAcquisition:
    """Cada acquire async registra ownership antes de su primer await."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stage",
        ["database", "lock", "journal", "router", "polling"],
    )
    async def test_fallo_parcial_cierra_misma_instancia(
        self,
        mock_args,
        tmp_path: pathlib.Path,
        stage: str,
    ) -> None:
        ctx = AppContext(mock_args)
        ctx.config_path = tmp_path / "config.toml"
        ctx._resolve_config_path = MagicMock()
        ctx._migrate_legacy_json = MagicMock()
        ctx.lifecycle.initialize = AsyncMock()
        ctx.lifecycle.close = AsyncMock()
        ctx.network.initialize = AsyncMock()
        ctx.network.close = AsyncMock()
        ctx.network.gateway = MagicMock()
        ctx.network.session = MagicMock()

        instances: dict[str, object] = {"database": ctx.database}
        cierres: list[tuple[str, object]] = []

        registry = MagicMock()
        registry.is_empty = AsyncMock(return_value=False)
        ctx.database.registry = registry

        async def inicializar_database() -> None:
            if stage == "database":
                raise RuntimeError("partial database failure")

        async def cerrar_database() -> None:
            cierres.append(("database", ctx.database))

        ctx.database.initialize = inicializar_database
        ctx.database.close = cerrar_database

        class FakeLock:
            def __init__(self, **_kwargs: object) -> None:
                instances["lock"] = self

            async def initialize(self) -> None:
                if stage == "lock":
                    raise RuntimeError("partial lock failure")

            async def close(self) -> None:
                cierres.append(("lock", self))

        class FakeSnapshot:
            def __init__(self, **_kwargs: object) -> None:
                return None

            async def initialize(self) -> None:
                return None

        class FakeJournal:
            def __init__(self, **_kwargs: object) -> None:
                instances["journal"] = self

            async def open(self) -> None:
                if stage == "journal":
                    raise RuntimeError("partial journal failure")

            async def close(self) -> None:
                cierres.append(("journal", self))

        class FakeRouter:
            def __init__(self, **_kwargs: object) -> None:
                instances["router"] = self

            async def open(self) -> None:
                if stage == "router":
                    assert ctx.router is None
                    assert ctx.hitl is None
                    raise RuntimeError("partial router failure")

            async def close(self) -> None:
                cierres.append(("router", self))

        class FakePolling:
            def __init__(self, **_kwargs: object) -> None:
                instances["polling"] = self

            async def start(self) -> None:
                if stage == "polling":
                    raise RuntimeError("partial polling failure")

            async def stop(self) -> None:
                cierres.append(("polling", self))

        local_cfg = MagicMock(
            mo2_root=str(tmp_path),
            skyrim_path=str(tmp_path),
            llm_provider="ollama",
            ollama_api_key="",
            ollama_model="",
            llm_api_key="",
            nexus_api_key="",
            telegram_bot_token="token",
            telegram_chat_id="",
            loot_exe="",
            xedit_exe="",
            pandora_exe="",
            bodyslide_exe="",
            install_dir="",
            allowed_tools=None,
        )
        metrics_auth = MagicMock()
        metrics_auth.generate = MagicMock()
        metrics_auth.start_rotation = AsyncMock()
        metrics_auth.stop_rotation = AsyncMock()
        metrics_runner = MagicMock()

        patches = [
            patch("sky_claw.app_context.Config", return_value=local_cfg),
            patch("sky_claw.app_context.MO2Controller"),
            patch("sky_claw.app_context.MasterlistClient"),
            patch("sky_claw.app_context.TelegramSender"),
            patch("sky_claw.app_context.HITLGuard"),
            patch("sky_claw.app_context.configure_tracing"),
            patch("sky_claw.app_context.shutdown_tracing"),
            patch("sky_claw.app_context.AuthTokenManager", return_value=metrics_auth),
            patch(
                "sky_claw.app_context.start_metrics_server",
                new=AsyncMock(return_value=metrics_runner),
            ),
            patch("sky_claw.app_context.stop_metrics_server", new=AsyncMock()),
            patch("sky_claw.app_context.SyncEngine"),
            patch("sky_claw.app_context.ToolsInstaller"),
            patch("sky_claw.app_context.scan_common_paths", return_value=None),
            patch("sky_claw.app_context.DistributedLockManager", FakeLock),
            patch("sky_claw.app_context.FileSnapshotManager", FakeSnapshot),
            patch("sky_claw.app_context.OperationJournal", FakeJournal),
            patch("sky_claw.app_context.AsyncToolRegistry"),
            patch("sky_claw.app_context.LLMRouter", FakeRouter),
            patch("sky_claw.app_context.TelegramPolling", FakePolling),
            patch("sky_claw.app_context._LOCK_STAGING_DIR", tmp_path / "locks"),
        ]
        with ExitStack() as stack:
            for active_patch in patches:
                stack.enter_context(active_patch)
            with pytest.raises(RuntimeError, match=f"partial {stage} failure"):
                await ctx.start_full()

        target = instances[stage]
        assert [obj for name, obj in cierres if name == stage] == [target]
        if stage == "router":
            assert ctx.router is None
        if stage == "polling":
            assert ctx.polling is None

    @pytest.mark.asyncio
    async def test_scan_common_paths_no_bloquea_heartbeat(
        self,
        mock_args,
        tmp_path: pathlib.Path,
    ) -> None:
        ctx = AppContext(mock_args)
        ctx.config_path = tmp_path / "config.toml"
        ctx._resolve_config_path = MagicMock()
        ctx._migrate_legacy_json = MagicMock()
        ctx.lifecycle.initialize = AsyncMock()
        ctx.lifecycle.close = AsyncMock()
        ctx.network.initialize = AsyncMock()
        ctx.network.close = AsyncMock()
        ctx.network.gateway = MagicMock()
        ctx.network.session = MagicMock()
        registry = MagicMock()
        registry.is_empty = AsyncMock(return_value=False)
        ctx.database.registry = registry
        ctx.database.initialize = AsyncMock()
        ctx.database.close = AsyncMock()

        local_cfg = MagicMock(
            mo2_root=str(tmp_path),
            skyrim_path=str(tmp_path),
            llm_provider="ollama",
            ollama_api_key="",
            ollama_model="",
            llm_api_key="",
            nexus_api_key="",
            telegram_bot_token="",
            telegram_chat_id="",
            loot_exe="",
            xedit_exe="",
            pandora_exe="",
            bodyslide_exe="",
            install_dir="",
            allowed_tools=None,
        )
        metrics_auth = MagicMock()
        metrics_auth.generate = MagicMock()
        metrics_auth.start_rotation = AsyncMock()
        metrics_auth.stop_rotation = AsyncMock()
        metrics_runner = MagicMock()
        loop = asyncio.get_running_loop()
        heartbeat = asyncio.Event()
        release_scan = threading.Event()
        scan_calls = 0

        def scan_lento(_paths: object, _name: str) -> None:
            nonlocal scan_calls
            scan_calls += 1
            if scan_calls == 1:
                loop.call_soon_threadsafe(heartbeat.set)
                release_scan.wait(timeout=0.2)
            return None

        def fallar_despues_del_scan(**_kwargs: object) -> None:
            raise RuntimeError("stop after scan")

        patches = [
            patch("sky_claw.app_context.Config", return_value=local_cfg),
            patch("sky_claw.app_context.MO2Controller"),
            patch("sky_claw.app_context.MasterlistClient"),
            patch("sky_claw.app_context.TelegramSender"),
            patch("sky_claw.app_context.HITLGuard"),
            patch("sky_claw.app_context.configure_tracing"),
            patch("sky_claw.app_context.shutdown_tracing"),
            patch("sky_claw.app_context.AuthTokenManager", return_value=metrics_auth),
            patch(
                "sky_claw.app_context.start_metrics_server",
                new=AsyncMock(return_value=metrics_runner),
            ),
            patch("sky_claw.app_context.stop_metrics_server", new=AsyncMock()),
            patch("sky_claw.app_context.SyncEngine"),
            patch("sky_claw.app_context.ToolsInstaller"),
            patch("sky_claw.app_context.scan_common_paths", side_effect=scan_lento),
            patch(
                "sky_claw.app_context.DistributedLockManager",
                side_effect=fallar_despues_del_scan,
            ),
            patch("sky_claw.app_context._LOCK_STAGING_DIR", tmp_path / "locks"),
        ]

        with ExitStack() as stack:
            for active_patch in patches:
                stack.enter_context(active_patch)
            startup = asyncio.create_task(ctx.start_full())
            started_at = loop.time()
            try:
                await asyncio.wait_for(heartbeat.wait(), timeout=1.0)
                assert loop.time() - started_at < 0.1
            finally:
                release_scan.set()
                result = await asyncio.gather(startup, return_exceptions=True)

        assert isinstance(result[0], RuntimeError)
        assert str(result[0]) == "stop after scan"

    @pytest.mark.asyncio
    async def test_cold_boot_con_key_cae_a_local_only_si_enriquecimiento_falla_total(
        self,
        mock_args,
        tmp_path: pathlib.Path,
    ) -> None:
        """Con API key pero Nexus caído en el primer arranque, el sync enriquecido
        falla para todos los mods (processed=0, failed>0) y el cold boot reintenta
        en modo local-only para no dejar el registry vacío."""
        from sky_claw.antigravity.orchestrator.sync_engine import SyncResult

        ctx = AppContext(mock_args)
        ctx.config_path = tmp_path / "config.toml"
        ctx._resolve_config_path = MagicMock()
        ctx._migrate_legacy_json = MagicMock()
        ctx.lifecycle.initialize = AsyncMock()
        ctx.lifecycle.close = AsyncMock()
        ctx.network.initialize = AsyncMock()
        ctx.network.close = AsyncMock()
        ctx.network.gateway = MagicMock()
        ctx.network.session = MagicMock()
        registry = MagicMock()
        registry.is_empty = AsyncMock(return_value=True)
        ctx.database.registry = registry
        ctx.database.initialize = AsyncMock()
        ctx.database.close = AsyncMock()

        local_cfg = MagicMock(
            mo2_root=str(tmp_path),
            skyrim_path=str(tmp_path),
            llm_provider="ollama",
            ollama_api_key="",
            ollama_model="",
            llm_api_key="",
            nexus_api_key="fake",
            telegram_bot_token="",
            telegram_chat_id="",
            loot_exe="",
            xedit_exe="",
            pandora_exe="",
            bodyslide_exe="",
            install_dir="",
            allowed_tools=None,
        )
        metrics_auth = MagicMock()
        metrics_auth.generate = MagicMock()
        metrics_auth.start_rotation = AsyncMock()
        metrics_auth.stop_rotation = AsyncMock()
        metrics_runner = MagicMock()

        mock_sync = MagicMock()
        # 1er run enriquecido: 0 procesados, 2 fallidos → gatilla el fallback
        # local-only; 2do run local: 2 procesados.
        mock_sync.return_value.run = AsyncMock(
            side_effect=[
                SyncResult(processed=0, failed=2),
                SyncResult(processed=2),
            ]
        )

        patches = [
            patch("sky_claw.app_context.Config", return_value=local_cfg),
            patch("sky_claw.app_context.MO2Controller"),
            patch("sky_claw.app_context.MasterlistClient"),
            patch("sky_claw.app_context.TelegramSender"),
            patch("sky_claw.app_context.HITLGuard"),
            patch("sky_claw.app_context.configure_tracing"),
            patch("sky_claw.app_context.shutdown_tracing"),
            patch("sky_claw.app_context.AuthTokenManager", return_value=metrics_auth),
            patch(
                "sky_claw.app_context.start_metrics_server",
                new=AsyncMock(return_value=metrics_runner),
            ),
            patch("sky_claw.app_context.stop_metrics_server", new=AsyncMock()),
            patch("sky_claw.app_context.SyncEngine", mock_sync),
            # Cortamos el arranque justo después del cold boot (ToolsInstaller es
            # la siguiente llamada) para no montar el resto del stack.
            patch(
                "sky_claw.app_context.ToolsInstaller",
                side_effect=RuntimeError("stop after cold boot"),
            ),
            patch("sky_claw.app_context._LOCK_STAGING_DIR", tmp_path / "locks"),
        ]

        with ExitStack() as stack:
            for active_patch in patches:
                stack.enter_context(active_patch)
            result = await asyncio.gather(ctx.start_full(), return_exceptions=True)

        assert isinstance(result[0], RuntimeError)
        assert str(result[0]) == "stop after cold boot"
        run = mock_sync.return_value.run
        assert run.await_count == 2
        assert run.await_args_list[0].kwargs["enrich_remote"] is True
        assert run.await_args_list[1].kwargs["enrich_remote"] is False


class TestAppContextSecretMigrationLogging:
    def test_secret_migration_failure_does_not_log_secret_material(
        self,
        mock_args,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret_value = "sk-" + "A" * 32
        legacy_path = tmp_path / "sky_claw_config.json"
        legacy_path.write_text("{}", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        class LegacyConfig:
            first_run = True

            def get_api_key(self) -> str:
                raise RuntimeError(f"could not decode {secret_value}")

            def get_nexus_api_key(self) -> None:
                return None

            def get_telegram_bot_token(self) -> None:
                return None

        toml_cfg = MagicMock()
        toml_cfg._data = {}

        ctx = AppContext(mock_args)
        ctx.config_path = tmp_path / "config.toml"

        with (
            patch("sky_claw.app_context._load_legacy_json", return_value=LegacyConfig()),
            patch("sky_claw.app_context.Config", return_value=toml_cfg),
            caplog.at_level(logging.WARNING, logger="sky_claw"),
        ):
            ctx._migrate_legacy_json()

        assert "Failed to migrate a legacy credential" in caplog.text
        assert secret_value not in caplog.text
        assert "could not decode" not in caplog.text
        assert "llm_api_key" not in caplog.text

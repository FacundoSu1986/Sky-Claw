"""Tests for :class:`VRAMrPipelineService`.

Cubren:
* Validación Zero-Trust vía ``PathValidator``.
* Happy path + publicación de eventos + commit de journal.
* Ramas de fallo (exit!=0, timeout, OSError, lock contention).
* Streaming en tiempo real (INFO para stdout, WARNING para stderr).
* Cleanup selectivo de ``output_dir`` preservando archivos preexistentes.
* Ramas de plataforma (``CREATE_NO_WINDOW`` sólo en Windows).
* Drain-task que lanza excepción sin tumbar la pipeline.
* Event bus real + sincronización con ``asyncio.Event`` (sin ``asyncio.sleep``).

Meta: 100% de cobertura de ramas en :mod:`sky_claw.local.tools.vramr_service`.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event
from sky_claw.antigravity.db.locks import DistributedLockManager, LockLeaseLostError
from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.tools import vramr_service as vramr_mod
from sky_claw.local.tools.vramr_service import (
    VRAMrExecutionError,
    VRAMrPipelineService,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_lock_db(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "vramr_locks.db"


@pytest.fixture
async def lock_manager(tmp_lock_db: pathlib.Path):
    mgr = DistributedLockManager(
        tmp_lock_db,
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    d = tmp_path / "snapshots"
    d.mkdir()
    return FileSnapshotManager(snapshot_dir=d)


@pytest.fixture
def mock_journal() -> AsyncMock:
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=1)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    return journal


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    bus = AsyncMock(spec=CoreEventBus)
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def path_validator(tmp_path: pathlib.Path) -> PathValidator:
    return PathValidator(roots=[tmp_path])


@pytest.fixture
def vramr_exe(tmp_path: pathlib.Path) -> pathlib.Path:
    exe = tmp_path / "VRAMr.exe"
    exe.touch()
    return exe


@pytest.fixture
def output_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "textures_out"
    d.mkdir()
    return d


@pytest.fixture
def outside_path(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    return tmp_path_factory.mktemp("outside") / "evil.exe"


def make_fake_proc(
    *,
    exit_code: int = 0,
    stdout_lines: Iterable[bytes] = (b"out-1\n", b"out-2\n"),
    stderr_lines: Iterable[bytes] = (),
    stdout_side_effect: list | None = None,
    stderr_side_effect: list | None = None,
    returncode_override: int | None = ...,  # type: ignore[assignment]
) -> MagicMock:
    proc = MagicMock()
    if returncode_override is ...:
        proc.returncode = exit_code
    else:
        proc.returncode = returncode_override

    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(
        side_effect=stdout_side_effect if stdout_side_effect is not None else [*stdout_lines, b""],
    )
    proc.stderr = MagicMock()
    proc.stderr.readline = AsyncMock(
        side_effect=stderr_side_effect if stderr_side_effect is not None else [*stderr_lines, b""],
    )
    proc.wait = AsyncMock(return_value=exit_code)
    proc.kill = MagicMock()
    return proc


def make_service(
    *,
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    path_validator: PathValidator,
    event_bus: AsyncMock | CoreEventBus,
    default_timeout: float = 60.0,
    snapshot_manager: FileSnapshotManager | None = None,
) -> VRAMrPipelineService:
    # target_files=[] → SnapshotTransactionLock nunca toca el snapshot_manager,
    # pero es un arg requerido. Se construye uno real en un tmpdir efímero para
    # no acoplar los 23 call sites a una fixture nueva.
    if snapshot_manager is None:
        import tempfile

        snapshot_manager = FileSnapshotManager(snapshot_dir=pathlib.Path(tempfile.mkdtemp()))
    return VRAMrPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=mock_journal,
        path_validator=path_validator,
        event_bus=event_bus,
        default_timeout=default_timeout,
    )


# =============================================================================
# 1–2. Path-validation early returns
# =============================================================================


async def test_path_validation_rejects_exe_outside_sandbox(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    output_dir: pathlib.Path,
    outside_path: pathlib.Path,
) -> None:
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    result = await svc.execute_pipeline(
        vramr_exe=outside_path,
        args=[],
        output_dir=output_dir,
    )
    assert result["success"] is False
    assert "Path violation" in result["error"]
    mock_event_bus.publish.assert_not_called()
    mock_journal.begin_transaction.assert_not_called()
    mock_journal.commit_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_not_called()


async def test_path_validation_rejects_output_dir_outside_sandbox(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside_dir = tmp_path_factory.mktemp("other-root")
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    result = await svc.execute_pipeline(
        vramr_exe=vramr_exe,
        args=[],
        output_dir=outside_dir,
    )
    assert result["success"] is False
    assert "Path violation" in result["error"]
    mock_event_bus.publish.assert_not_called()


# =============================================================================
# 3. Happy path
# =============================================================================


async def test_happy_path_success_publishes_events_and_commits(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    fake = make_fake_proc(
        exit_code=0,
        stdout_lines=(b"comp 1\n", b"comp 2\n", b"done\n"),
    )
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=["--preset", "high"],
            output_dir=output_dir,
        )
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout_line_count"] == 3
    assert result["rolled_back"] is False
    assert result["error"] is None

    topics = [c.args[0].topic for c in mock_event_bus.publish.call_args_list]
    assert topics == ["vramr.pipeline.started", "vramr.pipeline.completed"]

    completed = mock_event_bus.publish.call_args_list[1].args[0]
    assert completed.source == "vramr-service"
    assert completed.payload["rolled_back"] is False
    assert completed.payload["success"] is True

    mock_journal.begin_transaction.assert_awaited_once_with(
        description="vramr_pipeline",
        agent_id="vramr-service",
    )
    mock_journal.commit_transaction.assert_awaited_once_with(1)
    mock_journal.mark_transaction_rolled_back.assert_not_called()


# =============================================================================
# 4. Non-zero exit → rollback + cleanup
# =============================================================================


async def test_nonzero_exit_raises_execution_error_and_rolls_back(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    # Simular un artefacto nuevo creado durante la ejecución (para cleanup).
    artifact = output_dir / "partial.dds"
    artifact.write_bytes(b"partial")

    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )

    # existed_before lo toma ANTES de ejecutar → así que si creamos el archivo
    # DESPUÉS del snapshot_existing pero ANTES del subprocess fake, cleanup lo borra.
    # Hack: reseteamos el artefacto dentro del lado-del-subprocess.
    async def _fake_create(*a, **kw):
        # El snapshot ya se tomó; este archivo aparece "durante" el run.
        (output_dir / "new_during_run.dds").write_bytes(b"junk")
        return make_fake_proc(exit_code=2, stderr_lines=(b"bad input\n",))

    # Limpiamos el artefacto pre-existente porque _snapshot_existing ya lo vio —
    # queremos verificar que se PRESERVA.
    # (Lo dejamos: el cleanup no debe borrarlo.)

    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(side_effect=_fake_create)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "VRAMr exit 2" in result["error"]
    assert "bad input" in result["error"]
    mock_journal.commit_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)

    # Cleanup selectivo: preexistente preservado, nuevo borrado.
    assert artifact.exists()
    assert not (output_dir / "new_during_run.dds").exists()

    completed = mock_event_bus.publish.call_args_list[1].args[0]
    assert completed.payload["rolled_back"] is True
    assert completed.payload["success"] is False
    assert completed.payload["exit_code"] == 2


# =============================================================================
# 5. Timeout
# =============================================================================


async def test_timeout_kills_process_and_cleans_up(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
        default_timeout=0.5,
    )
    fake = make_fake_proc(exit_code=0)

    # El primer wait_for (proc.wait) levanta TimeoutError. El segundo (kill-grace)
    # devuelve None limpiamente.
    wait_for_mock = AsyncMock(side_effect=[TimeoutError(), None])

    async def _fake_create(*a, **kw):
        (output_dir / "pending.dds").write_bytes(b"x")
        return fake

    with (
        patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(side_effect=_fake_create)),
        patch.object(vramr_mod.asyncio, "wait_for", wait_for_mock),
    ):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
            timeout=0.25,
        )

    fake.kill.assert_called_once()
    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "timed out" in result["error"]
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)
    assert not (output_dir / "pending.dds").exists()


async def test_timeout_mata_el_arbol_de_procesos_no_solo_el_hijo(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    """En timeout se invoca kill_and_reap (taskkill /F /T del árbol) y no un
    proc.kill() pelado, que dejaría huérfanos los nietos (workers de compresión
    de texturas). Regresión de U-05: el test de timeout previo pasa
    incidentalmente porque kill_and_reap TAMBIÉN llama proc.kill(), así que no
    ancla este comportamiento; acá espiamos el helper directamente."""
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
        default_timeout=0.5,
    )
    fake = make_fake_proc(exit_code=0)

    async def _fake_create(*a, **kw):
        return fake

    kill_and_reap_spy = AsyncMock()
    with (
        patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(side_effect=_fake_create)),
        patch.object(vramr_mod.asyncio, "wait_for", AsyncMock(side_effect=TimeoutError())),
        patch.object(vramr_mod, "kill_and_reap", kill_and_reap_spy),
    ):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
            timeout=0.25,
        )

    kill_and_reap_spy.assert_awaited_once_with(fake)
    assert result["success"] is False
    assert "timed out" in result["error"]


# =============================================================================
# 6. Lock contention
# =============================================================================


async def test_lock_contention_returns_error_without_rollback(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    # Pre-adquirir el mismo resource_id desde otro agente.
    # Nota: desde 4.7, el código usa str(validated_output) como resource_id, no .name
    await lock_manager.acquire_lock(str(output_dir), "other-agent")

    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    # create_subprocess_exec NO debe ser llamado — el lock falla antes.
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock()) as cse:
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
        cse.assert_not_called()

    assert result["success"] is False
    assert result["rolled_back"] is False
    assert "Lock contention" in result["error"]
    mock_journal.begin_transaction.assert_not_called()
    mock_journal.mark_transaction_rolled_back.assert_not_called()

    # El evento completed igual se publica.
    completed = mock_event_bus.publish.call_args_list[1].args[0]
    assert completed.payload["rolled_back"] is False
    assert completed.payload["success"] is False


class _LockLeaseLost:
    """Lock de frontera: en un clean-exit (el body no lanzó), ``__aexit__`` se
    comporta como una lease perdida — review Codex #316: el heartbeat pudo
    perder el lease DURANTE un run largo aunque VRAMr haya terminado con
    exit 0 (el body ya cometió el journal y salió sin excepción)."""

    rollback_completed = False

    async def __aenter__(self) -> _LockLeaseLost:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: object, exc_tb: object) -> bool:
        if exc_type is None:
            raise LockLeaseLostError("lease perdida (simulado)")
        return False


async def test_lease_perdida_devuelve_dict_no_propaga(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    """Regla T11: execute_pipeline SIEMPRE devuelve dict, incluso si el lock
    pierde el lease en un clean-exit (después de que VRAMr terminó bien)."""
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
        snapshot_manager=snapshot_manager,
    )

    with (
        patch.object(vramr_mod, "SnapshotTransactionLock", return_value=_LockLeaseLost()),
        patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=make_fake_proc(exit_code=0))),
    ):
        result = await svc.execute_pipeline(vramr_exe=vramr_exe, args=[], output_dir=output_dir)

    assert result["success"] is False
    assert "lease" in result["error"].lower()
    assert result["rolled_back"] is False


# =============================================================================
# 6b. Lease de larga duración: el lock usa SnapshotTransactionLock (§2.1)
# =============================================================================


async def test_lease_sobrevive_al_ttl_gracias_al_heartbeat(
    tmp_lock_db: pathlib.Path,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    """El bug §2.1: con ``acquire_lock`` crudo (TTL 10 min) un run largo perdía
    el lease en silencio. Con SnapshotTransactionLock el heartbeat lo renueva.

    Se fuerza un TTL chico (1.0 s) y un run que dura ~2× eso; al terminar, el lock
    sigue siendo del servicio (no expiró) y se libera limpio.

    El TTL no se baja más (p.ej. 0.3 s) a propósito: el heartbeat renueva cada
    ``TTL/renew_divisor`` (divisor 3.0), así que con TTL 0.3 s la tolerancia a que
    el scheduler starve la task de renovación es solo ~0.3 s → flaky bajo carga en
    CI. Con TTL 1.0 s la ventana de supervivencia es ~1.0 s, holgada frente al
    jitter de scheduling; el run de 2.0 s sigue forzando varias renovaciones."""
    mgr = DistributedLockManager(tmp_lock_db, default_ttl=1.0, max_retries=2, backoff_base=0.05, backoff_max=0.2)
    await mgr.initialize()
    try:
        svc = make_service(
            lock_manager=mgr,
            snapshot_manager=snapshot_manager,
            mock_journal=mock_journal,
            path_validator=path_validator,
            event_bus=mock_event_bus,
        )

        async def _proc_lento(*_a: object, **_k: object) -> MagicMock:
            # El "run" dura ~2× el TTL: sin heartbeat el lease habría expirado.
            proc = make_fake_proc(exit_code=0)
            original = proc.wait

            async def _wait_lento() -> int:
                await asyncio.sleep(2.0)
                return await original()

            proc.wait = _wait_lento  # type: ignore[assignment]
            return proc

        with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(side_effect=_proc_lento)):
            result = await svc.execute_pipeline(vramr_exe=vramr_exe, args=[], output_dir=output_dir)

        assert result["success"] is True  # el lease no expiró a mitad de run
        # Lock liberado limpio al salir del context manager.
        assert await mgr.get_lock_info(str(output_dir)) is None
    finally:
        await mgr.close()


async def test_lock_tomado_con_target_files_vacio(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    """VRAMr no snapshotea entrada: el lock se toma con ``target_files=[]``
    (rollback propio vía _cleanup_output_dir). Ancla el contrato del swap."""
    captured: dict[str, object] = {}
    real_lock = vramr_mod.SnapshotTransactionLock

    def _spy(**kwargs: object) -> object:
        captured.update(kwargs)
        return real_lock(**kwargs)  # type: ignore[arg-type]

    svc = make_service(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    with (
        patch.object(vramr_mod, "SnapshotTransactionLock", side_effect=_spy),
        patch.object(
            vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(side_effect=lambda *a, **k: make_fake_proc())
        ),
    ):
        await svc.execute_pipeline(vramr_exe=vramr_exe, args=[], output_dir=output_dir)

    assert captured["target_files"] == []
    assert captured["resource_id"] == str(output_dir)


# =============================================================================
# 7. Unexpected exception (Regla T11)
# =============================================================================


async def test_unexpected_exception_inside_lock_triggers_rollback(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    with patch.object(
        vramr_mod.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=OSError("Disk full")),
    ):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "Unexpected error" in result["error"]
    assert "Disk full" in result["error"]
    mock_journal.mark_transaction_rolled_back.assert_awaited_once_with(1)


# =============================================================================
# 8. Streaming — stdout → INFO, stderr → WARNING
# =============================================================================


async def test_stdout_logged_as_info_stderr_as_warning(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="sky_claw.local.tools.vramr_service")
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    fake = make_fake_proc(
        exit_code=0,
        stdout_lines=(b"progress 50%\n",),
        stderr_lines=(b"warning: unusual input\n",),
    )
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )

    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("progress 50%" in m for m in info_msgs)
    assert any("warning: unusual input" in m for m in warn_msgs)


# =============================================================================
# 9. Args forwarded verbatim
# =============================================================================


async def test_args_are_forwarded_to_subprocess(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    cse_mock = AsyncMock(return_value=make_fake_proc(exit_code=0))
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", cse_mock):
        await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=["--preset", "ultra", "--format", "bc7"],
            output_dir=output_dir,
        )
    # El primer argumento posicional es el exe, luego los args.
    called_positional = cse_mock.call_args.args
    assert called_positional[1:] == ("--preset", "ultra", "--format", "bc7")


# =============================================================================
# 10–11. Windows CREATE_NO_WINDOW branch
# =============================================================================


async def test_windows_creationflags_applied_on_win32(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vramr_mod.sys, "platform", "win32")
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    cse_mock = AsyncMock(return_value=make_fake_proc(exit_code=0))
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", cse_mock):
        await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    assert cse_mock.call_args.kwargs.get("creationflags") == 0x08000000


async def test_no_creationflags_on_linux(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vramr_mod.sys, "platform", "linux")
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    cse_mock = AsyncMock(return_value=make_fake_proc(exit_code=0))
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", cse_mock):
        await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    assert "creationflags" not in cse_mock.call_args.kwargs


# =============================================================================
# 12. Drain-task exception doesn't crash pipeline
# =============================================================================


async def test_drain_task_exception_does_not_crash_pipeline(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="sky_claw.local.tools.vramr_service")
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    # stdout.readline lanza OSError tras la primera línea; stderr hace EOF limpio.
    fake = make_fake_proc(
        exit_code=0,
        stdout_side_effect=[b"first\n", OSError("pipe broken")],
        stderr_side_effect=[b""],
    )
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert any("stream drain" in r.message for r in caplog.records)


# =============================================================================
# 13. Real CoreEventBus — asyncio.Event synchronization
# =============================================================================


async def test_real_event_bus_receives_events_in_order(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    bus = CoreEventBus()
    await bus.start()
    try:
        received: list[Event] = []
        completed_flag = asyncio.Event()

        async def subscriber(event: Event) -> None:
            received.append(event)
            if event.topic == "vramr.pipeline.completed":
                completed_flag.set()

        bus.subscribe("vramr.pipeline.*", subscriber)

        svc = make_service(
            lock_manager=lock_manager,
            mock_journal=mock_journal,
            path_validator=path_validator,
            event_bus=bus,
        )
        fake = make_fake_proc(exit_code=0)
        with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
            await svc.execute_pipeline(
                vramr_exe=vramr_exe,
                args=[],
                output_dir=output_dir,
            )

        await asyncio.wait_for(completed_flag.wait(), timeout=5.0)
    finally:
        await bus.stop()

    topics = [e.topic for e in received]
    assert topics == ["vramr.pipeline.started", "vramr.pipeline.completed"]
    assert received[1].payload["success"] is True


# =============================================================================
# 14. Cleanup preserves preexisting files
# =============================================================================


async def test_cleanup_preserves_preexisting_files(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    preexisting_file = output_dir / "keep-me.dds"
    preexisting_file.write_bytes(b"sacred")
    preexisting_subdir = output_dir / "keep-dir"
    preexisting_subdir.mkdir()
    (preexisting_subdir / "inner.txt").write_text("x")

    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )

    async def _fake_create(*a, **kw):
        # Crea artefactos nuevos (archivo y directorio)
        (output_dir / "new_file.dds").write_bytes(b"garbage")
        new_dir = output_dir / "new_dir"
        new_dir.mkdir()
        (new_dir / "inside.txt").write_text("also garbage")
        return make_fake_proc(exit_code=99, stderr_lines=(b"boom\n",))

    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(side_effect=_fake_create)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )

    assert result["success"] is False
    # Preexistentes intactos:
    assert preexisting_file.exists()
    assert preexisting_subdir.exists()
    assert (preexisting_subdir / "inner.txt").exists()
    # Nuevos borrados:
    assert not (output_dir / "new_file.dds").exists()
    assert not (output_dir / "new_dir").exists()


# =============================================================================
# 15. stdout_tail truncation
# =============================================================================


async def test_result_dict_truncates_stdout_to_tail_20(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    many_lines = tuple(f"line-{i}\n".encode() for i in range(30))
    fake = make_fake_proc(exit_code=0, stdout_lines=many_lines)
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    assert result["stdout_line_count"] == 30
    assert len(result["stdout_tail"]) == 20
    assert result["stdout_tail"][0] == "line-10"
    assert result["stdout_tail"][-1] == "line-29"


# =============================================================================
# Extra: returncode=None fallback
# =============================================================================


async def test_returncode_none_falls_back_to_minus_one(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    fake = make_fake_proc(exit_code=0, returncode_override=None)
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    # exit_code=-1 → success=False via VRAMrExecutionError
    assert result["success"] is False
    assert result["exit_code"] == -1
    assert result["rolled_back"] is True


# =============================================================================
# Extra: last line without trailing newline
# =============================================================================


async def test_stream_handles_line_without_trailing_newline(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    fake = make_fake_proc(
        exit_code=0,
        stdout_lines=(b"with-newline\n", b"no-newline"),
    )
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    assert result["stdout_tail"] == ["with-newline", "no-newline"]


# =============================================================================
# Extra: release_lock failure is swallowed
# =============================================================================


async def test_release_lock_failure_is_swallowed(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Tras el swap a SnapshotTransactionLock, el fallo de release lo aísla y
    # loguea el _safe_release del propio lock (locks.py), no el VRAMr — el
    # comportamiento visible (swallowed, pipeline exitosa) se conserva.
    caplog.set_level(logging.WARNING, logger="sky_claw.antigravity.db.locks")
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    original_release = lock_manager.release_lock

    async def failing_release(*a, **kw):
        # Liberamos realmente antes de fallar, para no dejar locks huérfanos.
        await original_release(*a, **kw)
        raise RuntimeError("simulated release failure")

    with (
        patch.object(lock_manager, "release_lock", side_effect=failing_release),
        patch.object(
            vramr_mod.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=make_fake_proc(exit_code=0)),
        ),
    ):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    assert result["success"] is True  # el fallo de release no tumba el pipeline
    assert any("Failed to release lock" in r.message for r in caplog.records)


# =============================================================================
# Extra: journal.mark_rolled_back failure is logged critically, not raised
# =============================================================================


async def test_journal_mark_rolled_back_failure_is_logged(
    lock_manager: DistributedLockManager,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    output_dir: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.CRITICAL, logger="sky_claw.local.tools.vramr_service")
    journal = AsyncMock()
    journal.begin_transaction = AsyncMock(return_value=42)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock(side_effect=RuntimeError("journal died"))

    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    fake = make_fake_proc(exit_code=1, stderr_lines=(b"nope\n",))
    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=fake)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=output_dir,
        )
    assert result["success"] is False
    assert result["rolled_back"] is True
    journal.mark_transaction_rolled_back.assert_awaited_once_with(42)
    assert any("journal TX 42" in r.message for r in caplog.records)


# =============================================================================
# Extra: _snapshot_existing + _cleanup when output_dir doesn't exist
# =============================================================================


async def test_cleanup_when_output_dir_does_not_exist(
    lock_manager: DistributedLockManager,
    mock_journal: AsyncMock,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
    vramr_exe: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    # output_dir existe al momento de la validación, pero lo borramos
    # antes del subprocess para ejercitar la rama "no existe" de
    # _snapshot_existing y _cleanup_output_dir.
    out = tmp_path / "will_disappear"
    out.mkdir()

    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=mock_journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )

    async def _fake_create(*a, **kw):
        out.rmdir()  # desaparece durante la ejecución
        return make_fake_proc(exit_code=3, stderr_lines=(b"gone\n",))

    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(side_effect=_fake_create)):
        result = await svc.execute_pipeline(
            vramr_exe=vramr_exe,
            args=[],
            output_dir=out,
        )
    assert result["success"] is False
    assert result["rolled_back"] is True


async def test_snapshot_existing_returns_empty_for_missing_dir(
    tmp_path: pathlib.Path,
) -> None:
    missing = tmp_path / "does-not-exist"
    assert VRAMrPipelineService._snapshot_existing(missing) == set()

    # También para un archivo (no-directorio)
    a_file = tmp_path / "a.txt"
    a_file.write_text("x")
    assert VRAMrPipelineService._snapshot_existing(a_file) == set()


async def test_cleanup_when_entry_unlink_fails_logs_warning(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="sky_claw.local.tools.vramr_service")
    d = tmp_path / "out"
    d.mkdir()
    bad = d / "bad"
    bad.write_text("x")

    def _boom(*a, **kw):
        raise OSError("cannot unlink")

    with patch.object(pathlib.Path, "unlink", _boom):
        VRAMrPipelineService._cleanup_output_dir(d, existed_before=set())

    assert any("No pude limpiar" in r.message for r in caplog.records)


# =============================================================================
# Static helpers — pure unit
# =============================================================================


def test_error_dict_static_helper_shape() -> None:
    d = VRAMrPipelineService._error_dict("boom")
    assert d == {
        "success": False,
        "exit_code": -1,
        "stdout_line_count": 0,
        "stderr_line_count": 0,
        "stdout_tail": [],
        "stderr_tail": [],
        "error": "boom",
        "rolled_back": False,
        "duration_seconds": 0.0,
    }


def test_result_to_dict_static_helper_shape() -> None:
    d = VRAMrPipelineService._result_to_dict(
        exit_code=0,
        stdout_lines=["a", "b"],
        stderr_lines=[],
        error=None,
        rolled_back=False,
        duration=1.2345,
    )
    assert d["success"] is True
    assert d["exit_code"] == 0
    assert d["stdout_line_count"] == 2
    assert d["stdout_tail"] == ["a", "b"]
    assert d["stderr_tail"] == []
    assert d["duration_seconds"] == 1.234


def test_vramr_execution_error_has_fields() -> None:
    e = VRAMrExecutionError(7, "bad stuff")
    assert e.exit_code == 7
    assert e.stderr_tail == "bad stuff"
    assert "7" in str(e)
    assert "bad stuff" in str(e)


async def test_read_stream_handles_none_stream() -> None:
    """Defensa: si el subprocess no expone stream (improbable), no crashea."""
    bucket: list[str] = []
    await VRAMrPipelineService._read_stream(None, bucket, logging.INFO)
    assert bucket == []


async def test_safe_mark_rolled_back_no_op_when_tx_id_is_none(
    lock_manager: DistributedLockManager,
    mock_event_bus: AsyncMock,
    path_validator: PathValidator,
) -> None:
    """Cubre la rama ``tx_id is None`` del helper (return temprano)."""
    journal = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    svc = make_service(
        lock_manager=lock_manager,
        mock_journal=journal,
        path_validator=path_validator,
        event_bus=mock_event_bus,
    )
    await svc._safe_mark_rolled_back(None)
    journal.mark_transaction_rolled_back.assert_not_called()


# =============================================================================
# S-4: drain con cota en el path de éxito
# =============================================================================


class TestRunVramrDrainGrace:
    """S-4: ``_run_vramr`` no debe colgarse si un drain nunca ve EOF.

    Si VRAMr deja un nieto que hereda el pipe y sobrevive al proceso, el
    write-end nunca cierra y ``_read_stream`` no recibe EOF. Sin cota, el
    ``gather`` del path de éxito colgaría ``_run_vramr`` indefinidamente.
    """

    async def test_drain_colgado_en_exito_retorna_dentro_del_grace(self) -> None:
        async def _never_eof(*_a: object, **_k: object) -> bytes:
            await asyncio.Event().wait()  # se bloquea para siempre
            return b""  # pragma: no cover

        proc = make_fake_proc(exit_code=0, stdout_lines=(b"out-1\n",))
        proc.stderr.readline = _never_eof  # este drain nunca termina

        # `_run_vramr` no toca lock_manager/journal/path_validator/event_bus.
        svc = make_service(
            lock_manager=MagicMock(),
            mock_journal=AsyncMock(),
            path_validator=MagicMock(),
            event_bus=MagicMock(),
        )

        with (
            patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
            patch.object(vramr_mod, "_DRAIN_GRACE_SECONDS", 0.1),
        ):
            exit_code, stdout_lines, stderr_lines = await asyncio.wait_for(
                svc._run_vramr(pathlib.Path("VRAMr.exe"), [], timeout=60.0),
                timeout=5.0,
            )

        assert exit_code == 0
        assert stdout_lines == ["out-1"]  # stdout drenó normalmente
        assert stderr_lines == []  # drain cancelado tras el grace → parcial


# =============================================================================
# Cancelación externa — limpieza de proceso y drains
# =============================================================================


class _LineDrainUntilReleased:
    """Stream línea-a-línea que expone la cancelación del drain."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()

    async def readline(self) -> bytes:
        try:
            await self.release.wait()
            return b""
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.finished.set()


async def test_cancelacion_externa_mata_vramr_y_cancela_drains() -> None:
    """Un shutdown no deja VRAMr ni sus readers vivos en segundo plano."""
    wait_started = asyncio.Event()
    process_reaped = asyncio.Event()
    stdout = _LineDrainUntilReleased()
    stderr = _LineDrainUntilReleased()
    proc = MagicMock()
    proc.returncode = None
    proc.stdout = stdout
    proc.stderr = stderr

    async def _wait() -> int:
        wait_started.set()
        await process_reaped.wait()
        proc.returncode = -9
        return -9

    def _kill() -> None:
        process_reaped.set()

    proc.wait = _wait
    proc.kill = MagicMock(side_effect=_kill)
    svc = make_service(
        lock_manager=MagicMock(),
        mock_journal=AsyncMock(),
        path_validator=MagicMock(),
        event_bus=MagicMock(),
    )

    with patch.object(vramr_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
        task = asyncio.create_task(svc._run_vramr(pathlib.Path("VRAMr.exe"), [], timeout=60.0))
        try:
            await wait_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            proc.kill.assert_called_once()
            assert stdout.cancelled.is_set()
            assert stderr.cancelled.is_set()
        finally:
            stdout.release.set()
            stderr.release.set()
            await stdout.finished.wait()
            await stderr.finished.wait()

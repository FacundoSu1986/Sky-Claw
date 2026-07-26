from __future__ import annotations

import asyncio
import errno
import json
import logging
import queue
import sys
import threading
from collections.abc import Iterator

import pytest

from sky_claw._logging_runtime import (
    FailSafeRotatingFileHandler,
    LoggerStream,
    LoggingHealth,
    NonBlockingQueueHandler,
)
from sky_claw.logging_config import (
    default_log_dir,
    install_missing_std_stream_adapters,
    setup_logging,
    shutdown_logging,
)


@pytest.fixture(autouse=True)
def _aislar_config_sensible(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setattr(
        "sky_claw.logging_config._resolve_telegram_chat_id",
        lambda: "",
    )
    yield
    shutdown_logging(timeout_s=0.5)


def _record(level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test.runtime",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def _raise_nested(secret: str) -> None:
    raise RuntimeError(f"fallo interno {secret}")


def test_default_log_dir_vive_en_perfil_del_usuario() -> None:
    assert default_log_dir().parts[-2:] == (".sky_claw", "logs")


def test_setup_escribe_json_y_separa_crashes(tmp_path) -> None:
    secret = "ghp_" + "A" * 36
    runtime = setup_logging(log_dir=tmp_path, process_role="test")
    assert runtime.records.maxsize == 8192
    file_handlers = [handler for handler in runtime.handlers if isinstance(handler, FailSafeRotatingFileHandler)]
    assert file_handlers
    assert all(handler.maxBytes == 10 * 1024 * 1024 for handler in file_handlers)
    assert all(handler.backupCount == 5 for handler in file_handlers)
    app_logger = logging.getLogger("test.runtime")

    app_logger.info("evento normal", extra={"event": "normal"})
    try:
        _raise_nested(secret)
    except RuntimeError:
        app_logger.exception(
            "fallo del proceso",
            extra={
                "event": "external_process_failed",
                "tool": "xedit",
                "exit_code": 17,
            },
        )

    assert shutdown_logging() is True
    assert runtime.health_snapshot().listener_alive is False

    main_records = [json.loads(line) for line in (tmp_path / "sky_claw.log").read_text(encoding="utf-8").splitlines()]
    crash_records = [json.loads(line) for line in (tmp_path / "crash.log").read_text(encoding="utf-8").splitlines()]

    assert any(record["message"] == "evento normal" for record in main_records)
    crash = next(record for record in crash_records if record["message"] == "fallo del proceso")
    assert crash["event"] == "external_process_failed"
    assert crash["timestamp"].endswith("Z")
    assert crash["process_role"] == "test"
    assert crash["tool"] == "xedit"
    assert crash["exit_code"] == 17
    assert "_raise_nested" in crash["exc_info"]
    assert secret not in crash["exc_info"]
    assert "[REDACTED]" in crash["exc_info"]


def test_crash_log_incluye_watcher_y_seguridad(tmp_path) -> None:
    setup_logging(log_dir=tmp_path, process_role="app", console_stream=None)
    logging.getLogger("SkyClaw.Watcher.Scan").error(
        "watcher roto",
        extra={"event": "watcher_failed"},
    )
    logging.getLogger("SkyClaw.Security.Policy").critical(
        "security rota",
        extra={"event": "security_failed"},
    )
    assert shutdown_logging() is True

    crash_records = [json.loads(line) for line in (tmp_path / "crash.log").read_text(encoding="utf-8").splitlines()]
    assert {record["event"] for record in crash_records} >= {
        "watcher_failed",
        "security_failed",
    }


def test_worker_vfs_escribe_solo_su_archivo_por_job(tmp_path) -> None:
    setup_logging(
        log_dir=tmp_path,
        log_file="vfs-worker-job-1.log",
        process_role="vfs_worker",
        console_stream=None,
    )
    logging.getLogger("sky_claw.local.mo2.vfs_worker").error("worker roto")
    assert shutdown_logging() is True

    assert (tmp_path / "vfs-worker-job-1.log").exists()
    assert not (tmp_path / "crash.log").exists()


def test_rotacion_ocurre_en_listener_dedicado(tmp_path) -> None:
    runtime = setup_logging(
        log_dir=tmp_path,
        process_role="test",
        console_stream=None,
    )
    main_sink = next(
        handler
        for handler in runtime.handlers
        if isinstance(handler, FailSafeRotatingFileHandler) and handler.baseFilename.endswith("sky_claw.log")
    )
    main_sink.maxBytes = 256
    main_sink.backupCount = 2

    for index in range(8):
        logging.getLogger("test.rotation").info(
            "registro de rotacion %s %s",
            index,
            "x" * 128,
        )
    assert shutdown_logging() is True

    assert (tmp_path / "sky_claw.log").exists()
    assert (tmp_path / "sky_claw.log.1").exists()


async def test_fallo_de_rotacion_degrada_sin_propagar(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = setup_logging(
        log_dir=tmp_path,
        process_role="test",
        console_stream=None,
    )
    main_sink = next(
        handler
        for handler in runtime.handlers
        if isinstance(handler, FailSafeRotatingFileHandler) and handler.baseFilename.endswith("sky_claw.log")
    )
    main_sink.maxBytes = 1
    attempted = threading.Event()

    def _fail_rollover(_self) -> None:
        attempted.set()
        error = PermissionError("rotacion denegada")
        error.winerror = 32
        raise error

    monkeypatch.setattr(
        logging.handlers.RotatingFileHandler,
        "doRollover",
        _fail_rollover,
    )
    try:
        logging.getLogger("test.rotation").info("registro conservado uno")
        assert await asyncio.to_thread(attempted.wait, 1)
        logging.getLogger("test.rotation").info("registro conservado dos")
        for _ in range(20):
            if runtime.health_snapshot().file_errors:
                break
            await asyncio.sleep(0.01)

        assert runtime.health_snapshot().degraded is True
        assert runtime.health_snapshot().file_errors >= 1
    finally:
        assert shutdown_logging() is True

    contents = (tmp_path / "sky_claw.log").read_text(encoding="utf-8")
    assert "registro conservado uno" in contents
    assert "registro conservado dos" in contents


async def test_sink_de_archivo_no_bloquea_el_event_loop(tmp_path, monkeypatch) -> None:
    from sky_claw import _logging_runtime

    entered = threading.Event()
    release = threading.Event()
    original_emit = _logging_runtime.FailSafeRotatingFileHandler.emit

    def _blocking_emit(self, record):  # noqa: ANN001, ANN202
        entered.set()
        release.wait(timeout=2)
        return original_emit(self, record)

    monkeypatch.setattr(
        _logging_runtime.FailSafeRotatingFileHandler,
        "emit",
        _blocking_emit,
    )
    setup_logging(log_dir=tmp_path, process_role="test")
    try:
        logging.getLogger("test.runtime").error("sink lento")
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    finally:
        release.set()
        assert shutdown_logging() is True


async def test_shutdown_acotado_puede_reintentarse_tras_sink_bloqueado(
    tmp_path,
    monkeypatch,
) -> None:
    from sky_claw import _logging_runtime

    entered = threading.Event()
    release = threading.Event()
    original_emit = _logging_runtime.FailSafeRotatingFileHandler.emit

    def _blocking_emit(self, record):  # noqa: ANN001, ANN202
        entered.set()
        release.wait(timeout=2)
        return original_emit(self, record)

    monkeypatch.setattr(
        _logging_runtime.FailSafeRotatingFileHandler,
        "emit",
        _blocking_emit,
    )
    setup_logging(log_dir=tmp_path, process_role="test", console_stream=None)
    logging.getLogger("test.runtime").error("sink bloqueado al cerrar")
    assert await asyncio.to_thread(entered.wait, 1)

    assert shutdown_logging(timeout_s=0.01) is False
    release.set()
    assert await asyncio.to_thread(_wait_for_shutdown)


def _wait_for_shutdown() -> bool:
    return any(shutdown_logging(timeout_s=0.05) for _ in range(100))


def test_setup_degrada_sin_crashear_si_directorio_no_es_escribible(tmp_path) -> None:
    blocker = tmp_path / "archivo"
    blocker.write_text("no es un directorio", encoding="utf-8")

    runtime = setup_logging(
        log_dir=blocker / "logs",
        process_role="test",
        console_stream=None,
    )
    logging.getLogger("test.runtime").critical("continua sin disco")

    snapshot = runtime.health_snapshot()
    assert snapshot.degraded is True
    assert snapshot.file_errors >= 1
    assert shutdown_logging() is True


def test_queue_saturada_preserva_error_en_buffer_de_emergencia() -> None:
    records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
    health = LoggingHealth(emergency_capacity=2)
    handler = NonBlockingQueueHandler(records, health)

    handler.emit(_record(logging.INFO, "ocupa cola"))
    handler.emit(_record(logging.ERROR, "crash preservado"))

    snapshot = health.snapshot(listener_alive=False)
    assert snapshot.dropped_records == 1
    assert snapshot.emergency_records == 1

    assert records.get_nowait().getMessage() == "ocupa cola"
    handler.emit(_record(logging.INFO, "dispara recuperacion"))
    assert records.get_nowait().getMessage() == "crash preservado"


def test_shutdown_es_idempotente_y_no_deja_listener(tmp_path) -> None:
    runtime = setup_logging(log_dir=tmp_path, process_role="test")
    assert runtime.health_snapshot().listener_alive is True

    assert shutdown_logging(timeout_s=1.0) is True
    assert shutdown_logging(timeout_s=1.0) is True
    assert runtime.health_snapshot().listener_alive is False
    assert any(isinstance(handler, logging.NullHandler) for handler in logging.getLogger().handlers)


def test_setup_repetido_reutiliza_unico_listener(tmp_path) -> None:
    first = setup_logging(log_dir=tmp_path, process_role="test")
    second = setup_logging(log_dir=tmp_path, process_role="test")

    assert second is first
    assert first.health_snapshot().listener_alive is True
    assert shutdown_logging() is True


def test_fallo_al_arrancar_listener_degrada_sin_propagar(
    tmp_path,
    monkeypatch,
) -> None:
    from sky_claw import _logging_runtime

    def _fail_start(_self) -> None:
        raise RuntimeError("sin threads disponibles")

    monkeypatch.setattr(_logging_runtime._SafeQueueListener, "start", _fail_start)
    runtime = setup_logging(
        log_dir=tmp_path,
        process_role="test",
        console_stream=None,
    )

    assert runtime.health_snapshot().degraded is True
    assert runtime.health_snapshot().listener_alive is False
    assert shutdown_logging() is True


async def test_enospc_del_sink_degrada_sin_afectar_heartbeat(
    tmp_path,
    monkeypatch,
) -> None:
    from sky_claw._logging_runtime import FailSafeRotatingFileHandler

    runtime = setup_logging(
        log_dir=tmp_path,
        process_role="test",
        console_stream=None,
    )
    sink = next(handler for handler in runtime.handlers if isinstance(handler, FailSafeRotatingFileHandler))
    attempted = threading.Event()

    def _fail_open():
        attempted.set()
        raise OSError(errno.ENOSPC, "disco lleno")

    monkeypatch.setattr(sink, "_open", _fail_open)
    try:
        logging.getLogger("test.runtime").error("evento durante ENOSPC")

        heartbeat = 0
        for _ in range(20):
            heartbeat += 1
            if runtime.health_snapshot().file_errors:
                break
            await asyncio.sleep(0.01)

        assert attempted.is_set()
        assert heartbeat > 0
        assert runtime.health_snapshot().degraded is True
        assert runtime.health_snapshot().file_errors >= 1
    finally:
        assert shutdown_logging() is True


def test_fallo_de_filtro_productor_no_escapa_al_caller() -> None:
    records: queue.Queue[logging.LogRecord | None] = queue.Queue(maxsize=1)
    health = LoggingHealth()
    handler = NonBlockingQueueHandler(records, health)

    class _BrokenFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            del record
            raise OSError("filtro roto")

    handler.addFilter(_BrokenFilter())
    assert handler.handle(_record(logging.ERROR, "no propagar")) is False
    assert health.snapshot(listener_alive=False).degraded is True


def test_mensaje_mapping_redacta_secretos_anidados_y_conserva_estructura(
    tmp_path,
) -> None:
    access_token = "opaque-access-token"
    client_secret = "opaque-client-secret"
    setup_logging(log_dir=tmp_path, process_role="test", console_stream=None)

    logging.getLogger("test.runtime").error(
        {
            "access_token": access_token,
            "nested": {"client_secret": client_secret},
            "status": "failed",
        }
    )
    assert shutdown_logging() is True

    raw_log = (tmp_path / "crash.log").read_text(encoding="utf-8")
    record = json.loads(raw_log)
    assert access_token not in raw_log
    assert client_secret not in raw_log
    assert record["access_token"] == "[REDACTED]"
    assert record["nested"]["client_secret"] == "[REDACTED]"
    assert record["status"] == "failed"


def test_reconfigurar_tras_adaptar_stdout_no_crea_recursion(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("sys.stdout", None)
    setup_logging(log_dir=tmp_path / "first", process_role="test")
    install_missing_std_stream_adapters()
    assert isinstance(sys.stdout, LoggerStream)
    assert shutdown_logging() is True

    runtime = setup_logging(log_dir=tmp_path / "second", process_role="test")
    assert not any(
        isinstance(handler, logging.StreamHandler) and isinstance(getattr(handler, "stream", None), LoggerStream)
        for handler in runtime.handlers
    )
    logging.getLogger("test.runtime").error("registro-unico")
    assert shutdown_logging() is True

    records = [
        json.loads(line) for line in (tmp_path / "second" / "sky_claw.log").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(record["message"] == "registro-unico" for record in records) == 1

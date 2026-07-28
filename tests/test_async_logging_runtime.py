from __future__ import annotations

import asyncio
import errno
import io
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
from sky_claw.config import Config
from sky_claw.logging_config import (
    default_log_dir,
    flush_logging,
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
    logging.getLogger("test.runtime").error("registro posterior al timeout")
    release.set()
    assert await asyncio.to_thread(_wait_for_shutdown)

    contents = (tmp_path / "sky_claw.log").read_text(encoding="utf-8")
    assert "registro posterior al timeout" in contents


def _wait_for_shutdown() -> bool:
    return any(shutdown_logging(timeout_s=0.05) for _ in range(100))


def test_shutdown_tolera_cola_llena_al_encolar_sentinel(
    tmp_path,
    monkeypatch,
) -> None:
    from sky_claw import _logging_runtime

    original_enqueue_sentinel = _logging_runtime._SafeQueueListener.enqueue_sentinel
    attempts = 0

    def _raise_full_once(self):  # noqa: ANN001, ANN202
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise queue.Full
        return original_enqueue_sentinel(self)

    monkeypatch.setattr(
        _logging_runtime._SafeQueueListener,
        "enqueue_sentinel",
        _raise_full_once,
    )
    setup_logging(log_dir=tmp_path, process_role="test", console_stream=None)

    assert shutdown_logging(timeout_s=1.0) is False
    logging.getLogger("test.runtime").error("registro tras carrera de sentinel")
    assert shutdown_logging(timeout_s=1.0) is True

    contents = (tmp_path / "sky_claw.log").read_text(encoding="utf-8")
    assert "registro tras carrera de sentinel" in contents


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


def test_stderr_windowed_no_convierte_inicio_normal_en_crash(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("sys.stderr", None)
    setup_logging(log_dir=tmp_path, process_role="test", console_stream=None)
    install_missing_std_stream_adapters()

    assert isinstance(sys.stderr, LoggerStream)
    sys.stderr.write("INFO: Uvicorn running\n")
    assert shutdown_logging() is True

    main_records = [json.loads(line) for line in (tmp_path / "sky_claw.log").read_text(encoding="utf-8").splitlines()]
    stdio = next(record for record in main_records if record.get("message") == "INFO: Uvicorn running")
    assert stdio["level"] == "INFO"
    assert not (tmp_path / "crash.log").exists() or "INFO: Uvicorn running" not in (tmp_path / "crash.log").read_text(
        encoding="utf-8"
    )


def test_flush_logging_confirma_salida_de_consola_antes_de_continuar(
    tmp_path,
) -> None:
    stream = io.StringIO()
    setup_logging(log_dir=tmp_path, process_role="test", console_stream=stream)

    logging.getLogger("test.runtime").info("respuesta ordenada")

    assert flush_logging(timeout_s=1.0) is True
    assert "respuesta ordenada" in stream.getvalue()


def test_consola_con_encoding_limitado_no_pierde_el_registro(tmp_path) -> None:
    """Una consola Windows con codepage limitado (cp1252, no UTF-8) no puede
    codificar buena parte de los mensajes en español del repo (tildes, `→`,
    emojis). Sin reconfigurar el stream, ``TextIOWrapper.write`` levanta
    ``UnicodeEncodeError`` ANTES de escribir nada (el encode falla completo,
    no parcial), `StreamHandler.emit` lo deriva a `handleError`, y la stdlib
    imprime "--- Logging error ---" en vez del mensaje real: el registro se
    pierde de la consola sin dejar rastro legible (reproducido en un run real
    de `python -m sky_claw` bajo una terminal cp1252)."""
    crudo = io.BytesIO()
    consola = io.TextIOWrapper(crudo, encoding="cp1252", write_through=True)
    setup_logging(log_dir=tmp_path, process_role="test", console_stream=consola)

    logging.getLogger("test.runtime").info("paso-A %s paso-B", "→")

    assert shutdown_logging() is True
    salida = crudo.getvalue().decode("cp1252", errors="replace")
    assert "paso-A" in salida
    assert "paso-B" in salida
    # Ancla el contrato exacto (backslashreplace conserva el código del
    # carácter escapado, p. ej. "→"), no solo "algo sobrevivió":
    # errors="replace" (que lo pierde, mostrando "?") pasaría las dos
    # aserciones de arriba igual.
    assert "\\u2192" in salida


@pytest.mark.parametrize(
    "excepcion",
    [
        TypeError("reconfigure() got an unexpected keyword argument 'errors'"),
        ValueError("boom"),
        OSError("boom"),
        AttributeError("stream no completamente inicializado"),
        RuntimeError("reconfigure sobre un stream activo"),
    ],
)
def test_stream_con_reconfigure_incompatible_no_rompe_setup_logging(tmp_path, excepcion) -> None:
    """``callable(reconfigure)`` confirma que el método existe, pero no qué
    excepción puede levantar: un stream inyectado por un caller de terceros
    (tests, wrappers de GUI, mocks) puede tener un ``reconfigure`` con otra
    firma o un estado interno roto. Enumerar tipos uno por uno (``ValueError``/
    ``OSError`` primero, luego ``TypeError``, luego ``AttributeError``/
    ``RuntimeError``...) es una lista que nunca termina de cerrarse; el punto
    real es que NINGUNA excepción de este ``reconfigure()`` best-effort debe
    abortar ``setup_logging()`` entero — justo lo que este bloque existe para
    evitar. Parametrizado para que agregar un tipo nuevo no requiera un test
    nuevo."""

    class _StreamConReconfigureRoto(io.StringIO):
        def reconfigure(self, **_kwargs: object) -> None:
            raise excepcion

    consola = _StreamConReconfigureRoto()

    runtime = setup_logging(log_dir=tmp_path, process_role="test", console_stream=consola)

    logging.getLogger("test.runtime").info("sigue funcionando pese al reconfigure roto")
    assert shutdown_logging() is True
    assert runtime.health_snapshot().degraded is False
    assert "sigue funcionando pese al reconfigure roto" in consola.getvalue()


def test_stream_con_reconfigure_desconocido_loggea_y_propaga(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    """Un fallo ajeno a las incompatibilidades conocidas no puede ocultarse."""

    class _FalloDeAplicacionError(Exception):
        pass

    class _StreamConFalloInesperado(io.StringIO):
        def reconfigure(self, **_kwargs: object) -> None:
            raise _FalloDeAplicacionError("estado interno corrupto")

    with (
        caplog.at_level(logging.ERROR, logger="sky_claw"),
        pytest.raises(
            _FalloDeAplicacionError,
            match="estado interno corrupto",
        ),
    ):
        setup_logging(
            log_dir=tmp_path,
            process_role="test",
            console_stream=_StreamConFalloInesperado(),
        )

    assert "reconfigure" in caplog.text


def test_error_de_formateo_no_ciega_el_sink_de_archivo(tmp_path) -> None:
    """Un record malformado solo se pierde a sí mismo, no al siguiente.

    ``prepare()`` ya no formatea en el productor, así que el ``TypeError`` de
    interpolación explota en el listener y la stdlib lo deriva a ``handleError``
    (``BaseRotatingHandler.emit`` captura ``Exception``, y ``shouldRollover``
    formatea porque ``maxBytes > 0``). La ventana de reintento existe para un
    disco caído: armarla ante un bug de formateo ciega el sink un segundo entero.
    """
    runtime = setup_logging(log_dir=tmp_path, process_role="test", console_stream=None)

    logging.getLogger("test.runtime").error("%s %s", "un-solo-argumento")
    logging.getLogger("test.runtime").error("registro sano tras el malformado")

    assert shutdown_logging() is True

    principal = (tmp_path / "sky_claw.log").read_text(encoding="utf-8")
    crash = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "registro sano tras el malformado" in principal
    assert "registro sano tras el malformado" in crash

    # Un bug de formateo no es un problema de disco: no debe contarse como
    # file_error (mentiría sobre la causa) — pero el record SÍ se perdió, así
    # que debe seguir siendo visible como supresión, no desaparecer sin rastro.
    snapshot = runtime.health_snapshot()
    assert snapshot.file_errors == 0
    assert snapshot.suppressed_records >= 1
    assert snapshot.degraded is True


def test_oserror_de_escritura_ciega_el_sink_y_contabiliza_lo_suprimido(
    tmp_path,
    monkeypatch,
) -> None:
    """El disco caído sí arma la ventana, y lo suprimido deja de ser invisible."""
    runtime = setup_logging(
        log_dir=tmp_path,
        process_role="test",
        console_stream=None,
    )
    sink = next(
        handler
        for handler in runtime.handlers
        if isinstance(handler, FailSafeRotatingFileHandler) and handler.baseFilename.endswith("sky_claw.log")
    )
    # El record de inicialización ya se escribió: cerrar deja ``stream=None`` de
    # forma determinista, así el próximo emit vuelve a pasar por ``_open``.
    assert flush_logging(timeout_s=1.0) is True
    sink.close()

    def _fail_open():
        raise OSError(errno.ENOSPC, "disco lleno")

    monkeypatch.setattr(sink, "_open", _fail_open)

    logging.getLogger("test.runtime").error("primer intento con disco lleno")
    logging.getLogger("test.runtime").error("segundo intento dentro de la ventana")
    assert flush_logging(timeout_s=1.0) is True

    snapshot = runtime.health_snapshot()
    assert snapshot.file_errors == 1
    assert snapshot.suppressed_records >= 1
    assert snapshot.degraded is True

    assert shutdown_logging() is True


def test_worker_vfs_no_resuelve_el_chat_id_de_telegram(
    tmp_path,
    monkeypatch,
) -> None:
    """Resolver el chat id construye ``Config()`` y lee el keyring.

    El worker es un subproceso efímero por job que nunca habla con Telegram:
    pagar ocho lecturas del Credential Manager por cada job —y hacer transitar
    los secretos por su memoria— no compra ninguna redacción.
    """
    resoluciones: list[str] = []

    def _espia() -> str:
        resoluciones.append("resuelto")
        return ""

    monkeypatch.setattr("sky_claw.logging_config._resolve_telegram_chat_id", _espia)
    setup_logging(
        log_dir=tmp_path,
        log_file="vfs-worker-job-1.log",
        process_role="vfs_worker",
        console_stream=None,
    )

    assert resoluciones == []
    assert shutdown_logging() is True


def test_proceso_principal_si_resuelve_el_chat_id_de_telegram(
    tmp_path,
    monkeypatch,
) -> None:
    """Hermano del test anterior: el recorte no puede tragarse al proceso que sí redacta."""
    resoluciones: list[str] = []

    def _espia() -> str:
        resoluciones.append("resuelto")
        return ""

    monkeypatch.setattr("sky_claw.logging_config._resolve_telegram_chat_id", _espia)
    setup_logging(log_dir=tmp_path, process_role="app", console_stream=None)

    assert resoluciones == ["resuelto"]
    assert shutdown_logging() is True


def test_guardar_config_actualiza_chat_id_redactado_sin_reiniciar(
    tmp_path,
) -> None:
    chat_id = "123456789012"
    setup_logging(log_dir=tmp_path / "logs", process_role="test", console_stream=None)
    config = Config(tmp_path / "config.toml")
    config._data["telegram_chat_id"] = chat_id
    config.save()

    logging.getLogger("test.runtime").error("destino telegram=%s", chat_id)
    assert shutdown_logging() is True

    raw_log = (tmp_path / "logs" / "crash.log").read_text(encoding="utf-8")
    assert chat_id not in raw_log
    assert "[REDACTED]" in raw_log

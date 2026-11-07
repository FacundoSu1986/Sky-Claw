"""El executable congelado puede actuar como worker/nieto sin iniciar la GUI."""

from __future__ import annotations

import json
import logging
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sky_claw import __main__ as main_module
from sky_claw.local.mo2.vfs_job_identity import JobIdentityError, PruebaDeJob

NOMBRE_DE_JOB = "Local\\SkyClawVfs-" + "0123456789abcdef" * 2
#: Lo que devolvería ``verificar_identidad_del_job`` para un worker ya verificado (en Linux la real falla cerrado).
PRUEBA = PruebaDeJob(nombre=NOMBRE_DE_JOB, pid=1, procesos_totales=2, procesos_activos=2)


def test_main_deriva_vfs_worker_antes_del_parser_normal() -> None:
    with (
        patch("sky_claw.local.mo2.vfs_worker.worker_main", return_value=0) as worker,
        pytest.raises(SystemExit) as exit_info,
    ):
        main_module.main(["--vfs-worker", "--manifest", "job.json"])

    assert exit_info.value.code == 0
    worker.assert_called_once_with(["--manifest", "job.json"])


def test_main_deriva_probe_child_al_mismo_entrypoint() -> None:
    with (
        patch("sky_claw.local.mo2.vfs_worker.worker_main", return_value=3) as worker,
        pytest.raises(SystemExit) as exit_info,
    ):
        main_module.main(["--vfs-probe-child", "canary.txt", "abc"])

    assert exit_info.value.code == 3
    worker.assert_called_once_with(["--probe-child", "canary.txt", "abc"])


def test_worker_configura_log_por_job_y_cierra_listener(tmp_path) -> None:
    from sky_claw.local.mo2 import vfs_worker

    def _run(coro) -> None:  # noqa: ANN001
        coro.close()

    with (
        patch.object(vfs_worker, "default_log_dir", return_value=tmp_path),
        patch.object(vfs_worker, "verificar_identidad_del_job", return_value=PRUEBA),
        patch.object(vfs_worker, "setup_logging") as setup,
        patch.object(vfs_worker, "shutdown_logging", return_value=True) as shutdown,
        patch.object(vfs_worker.asyncio, "run", side_effect=_run),
    ):
        result = vfs_worker.worker_main(
            [
                "--manifest",
                "job.json",
                "--descriptor",
                "descriptor.json",
                "--job-object",
                NOMBRE_DE_JOB,
                "--job-id",
                "job-123",
            ]
        )

    assert result == 2
    setup.assert_called_once_with(
        log_dir=tmp_path / "workers",
        log_file="vfs-worker-job-123.log",
        process_role="vfs_worker",
        console_stream=None,
    )
    shutdown.assert_called_once_with()


def test_probe_hijo_no_inicializa_logging(tmp_path) -> None:
    from sky_claw.local.mo2 import vfs_worker

    canary = tmp_path / "canary.txt"
    canary.write_bytes(b"canary")
    expected = vfs_worker.hashlib.sha256(b"canary").hexdigest()

    with (
        patch.object(vfs_worker, "setup_logging") as setup,
        patch.object(vfs_worker, "shutdown_logging") as shutdown,
        patch.object(vfs_worker.sys.stdout, "write"),
        patch.object(vfs_worker.sys.stdout, "flush"),
    ):
        result = vfs_worker.worker_main(["--probe-child", str(canary), expected])

    assert result == 0
    setup.assert_not_called()
    shutdown.assert_not_called()


def test_nombre_de_log_worker_bloquea_path_traversal() -> None:
    from sky_claw.local.mo2.vfs_worker import _worker_log_name

    assert _worker_log_name("../../otro\\job") == "vfs-worker-otro-job.log"


def test_worker_registra_resultado_terminal_fallido(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sky_claw.local.mo2 import vfs_worker

    failed = SimpleNamespace(
        success=False,
        job_id="job-err",
        exit_code=9,
        stderr="xEdit termino abruptamente",
    )

    def _run(coro):  # noqa: ANN001, ANN202
        coro.close()
        return failed

    with (
        patch.object(vfs_worker, "default_log_dir", return_value=tmp_path),
        patch.object(vfs_worker, "verificar_identidad_del_job", return_value=PRUEBA),
        patch.object(vfs_worker, "setup_logging"),
        patch.object(vfs_worker, "shutdown_logging", return_value=True),
        patch.object(vfs_worker.asyncio, "run", side_effect=_run),
        caplog.at_level(logging.ERROR, logger="sky_claw.local.mo2.vfs_worker"),
    ):
        result = vfs_worker.worker_main(
            [
                "--manifest",
                "job.json",
                "--descriptor",
                "descriptor.json",
                "--job-object",
                NOMBRE_DE_JOB,
                "--job-id",
                "job-err",
            ]
        )

    assert result == 1
    record = next(item for item in caplog.records if getattr(item, "event", "") == "external_process_failed")
    assert record.operation == "vfs_worker"
    assert record.job_id == "job-err"
    assert record.exit_code == 9
    assert record.stderr == "xEdit termino abruptamente"


def test_worker_registra_excepcion_inesperada_en_log_del_job(
    tmp_path: pathlib.Path,
) -> None:
    from sky_claw.local.mo2 import vfs_worker
    from sky_claw.logging_config import shutdown_logging

    shutdown_logging()

    def _raise_assertion(coro) -> None:  # noqa: ANN001
        coro.close()
        raise AssertionError("invariante worker rota")

    with (
        patch.object(vfs_worker, "default_log_dir", return_value=tmp_path),
        patch.object(vfs_worker, "verificar_identidad_del_job", return_value=PRUEBA),
        patch.object(vfs_worker.asyncio, "run", side_effect=_raise_assertion),
        pytest.raises(AssertionError, match="invariante worker rota"),
    ):
        vfs_worker.worker_main(
            [
                "--manifest",
                "job.json",
                "--descriptor",
                "descriptor.json",
                "--job-object",
                NOMBRE_DE_JOB,
                "--job-id",
                "job-assert",
            ]
        )

    records = [
        json.loads(line)
        for line in (tmp_path / "workers" / "vfs-worker-job-assert.log").read_text(encoding="utf-8").splitlines()
    ]
    failure = next(record for record in records if record.get("event") == "vfs_worker_unhandled_exception")
    assert failure["level"] == "CRITICAL"
    assert failure["operation"] == "vfs_worker"
    assert failure["job_id"] == "job-assert"
    assert "AssertionError: invariante worker rota" in failure["exc_info"]


def _argv_del_worker(job_id: str = "job-id") -> list[str]:
    return [
        "--manifest",
        "job.json",
        "--descriptor",
        "descriptor.json",
        "--job-object",
        NOMBRE_DE_JOB,
        "--job-id",
        job_id,
    ]


def test_worker_sin_job_object_no_arranca_ni_abre_logs(tmp_path) -> None:
    from sky_claw.local.mo2 import vfs_worker

    sin_job_object = [a for a in _argv_del_worker() if a not in ("--job-object", NOMBRE_DE_JOB)]
    with (
        patch.object(vfs_worker, "default_log_dir", return_value=tmp_path),
        patch.object(vfs_worker, "setup_logging") as setup,
        patch.object(vfs_worker.asyncio, "run") as run,
    ):
        resultado = vfs_worker.worker_main(sin_job_object)

    assert resultado == 64
    setup.assert_not_called()
    run.assert_not_called()


def test_worker_que_no_prueba_su_identidad_no_conecta_y_termina_a_sus_descendientes(tmp_path) -> None:
    from sky_claw.local.mo2 import vfs_worker

    with (
        patch.object(vfs_worker, "default_log_dir", return_value=tmp_path),
        patch.object(vfs_worker, "setup_logging"),
        patch.object(vfs_worker, "shutdown_logging", return_value=True) as shutdown,
        patch.object(vfs_worker, "verificar_identidad_del_job", side_effect=JobIdentityError("fuera del Job")),
        patch.object(vfs_worker, "terminar_descendientes_vivos", return_value=(41, 42)) as limpiar,
        patch.object(vfs_worker.asyncio, "run") as run,
    ):
        resultado = vfs_worker.worker_main(_argv_del_worker())

    assert resultado == 72
    run.assert_not_called()  # ni un socket al broker: no hay hello sin prueba
    limpiar.assert_called_once_with()
    shutdown.assert_called_once_with()


def test_si_la_limpieza_tambien_falla_el_worker_igual_sale_con_el_codigo_de_identidad(tmp_path, caplog) -> None:
    from sky_claw.local.mo2 import vfs_worker

    with (
        patch.object(vfs_worker, "default_log_dir", return_value=tmp_path),
        patch.object(vfs_worker, "setup_logging"),
        patch.object(vfs_worker, "shutdown_logging", return_value=True),
        patch.object(vfs_worker, "verificar_identidad_del_job", side_effect=JobIdentityError("fuera del Job")),
        patch.object(vfs_worker, "terminar_descendientes_vivos", side_effect=JobIdentityError("no murió el 13")),
        patch.object(vfs_worker.asyncio, "run") as run,
        caplog.at_level(logging.CRITICAL, logger="sky_claw.local.mo2.vfs_worker"),
    ):
        resultado = vfs_worker.worker_main(_argv_del_worker())

    assert resultado == 72
    run.assert_not_called()
    assert any(getattr(r, "event", "") == "vfs_worker_cleanup_failed" for r in caplog.records)


def test_la_sesion_recibe_la_prueba_que_produjo_el_verificador(tmp_path) -> None:
    from sky_claw.local.mo2 import vfs_worker

    capturado: dict[str, object] = {}

    def _run(coro) -> None:  # noqa: ANN001
        coro.close()

    def _sesion(**kwargs: object) -> object:
        capturado.update(kwargs)
        return SimpleNamespace(close=lambda: None)  # lo que `_run` cierra en lugar de esperar la corrutina

    with (
        patch.object(vfs_worker, "default_log_dir", return_value=tmp_path),
        patch.object(vfs_worker, "setup_logging"),
        patch.object(vfs_worker, "shutdown_logging", return_value=True),
        patch.object(vfs_worker, "verificar_identidad_del_job", return_value=PRUEBA) as verificar,
        # MagicMock explícito: `patch` crearía un AsyncMock (la función es `async`) y el side_effect solo correría al esperar.
        patch.object(vfs_worker, "run_worker_session", new_callable=MagicMock, side_effect=_sesion),
        patch.object(vfs_worker.asyncio, "run", side_effect=_run),
    ):
        vfs_worker.worker_main(_argv_del_worker())

    verificar.assert_called_once_with(NOMBRE_DE_JOB)
    assert capturado["job_identity"] is PRUEBA

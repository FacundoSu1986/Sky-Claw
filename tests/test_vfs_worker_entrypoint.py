"""El executable congelado puede actuar como worker/nieto sin iniciar la GUI."""

from __future__ import annotations

import logging
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sky_claw import __main__ as main_module


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

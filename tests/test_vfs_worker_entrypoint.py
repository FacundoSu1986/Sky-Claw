"""El executable congelado puede actuar como worker/nieto sin iniciar la GUI."""

from __future__ import annotations

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

"""Seam de testing del OperatorVerifierBridge — EXCLUSIVA de la suite.

Vive en ``tests/`` y no en ``sky_claw/`` a propósito:

- El ancla `tests/test_contrato_argumentos_cli.py` congela el inventario de
  módulos productivos que lanzan subprocesos. Un ``subprocess.Popen`` de
  testing dentro de `sky_claw/local/runtime_vault/` inflaba ese inventario con
  un "lanzador" que no lanza ninguna herramienta de terceros.
- El módulo productivo declara en su docstring que no usa ``subprocess`` en
  producción y que la seam está "confinada exclusivamente a tests". Con la
  clase viviendo en el paquete productivo eso era una afirmación, no un hecho
  verificable; acá lo garantiza el propio layout del repo y el gate AST
  (`test_ast_gate_test_launcher_confinado_a_tests`).

Lanza un subproceso Python real desechable para que el bridge interactúe con un
proceso Win32 genuino (ciclo de vida, WaitForSingleObject, TerminateProcess).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any, Final

from sky_claw.local.runtime_vault.operator_token import OperatorPrimaryToken
from sky_claw.local.runtime_vault.operator_verifier_bridge import (
    ChildProcessEvidence,
    OperatorVerifierLaunchError,
    _ensure_windows,
    _is_invalid_handle,
    _read_process_creation_time,
    _read_process_image_path,
)

# ``_kernel32`` sólo existe bajo Windows en el módulo productivo (el binding
# ctypes vive dentro de ``if sys.platform == "win32"``). Importarlo a nivel de
# módulo rompía la COLECCIÓN de pytest en Linux/macOS, no sólo la ejecución.
if sys.platform == "win32":
    from sky_claw.local.runtime_vault.operator_verifier_bridge import _kernel32

__all__ = ["_TestOnlyVerifierLauncher"]


class _TestOnlyVerifierLauncher:
    """Seam de testing confinada a pruebas Win32 (no para producción).

    Lanza un subproceso Python real desechable para que el helper interactúe
    con un proceso Win32 genuino, permitiendo verificar ciclo de vida,
    WaitForSingleObject y TerminateProcess sin comprometer el proceso de test.
    """

    #: Ruta ficticia de la imagen del verificador usada por la seam. No existe en
    #: disco: el hijo real lo lanza esta clase con el intérprete de la suite.
    FAKE_VERIFIER_IMAGE: Final[pathlib.Path] = pathlib.Path("C:\\Sky-Claw\\test_verifier.exe")

    def resolve_executable(self) -> pathlib.Path:
        """Contrato de ``OperatorVerifierLauncher``: imagen ficticia sólo para tests."""
        return self.FAKE_VERIFIER_IMAGE

    def __init__(
        self,
        *,
        tamper_child_pid: int | None = None,
        tamper_creation_time_delta: int = 0,
        tamper_image_path: str | None = None,
        tamper_response_nonce: str | None = None,
        tamper_hang_after_response: bool = False,
        worker_code_override: str | None = None,
    ) -> None:
        self.tamper_child_pid = tamper_child_pid
        self.tamper_creation_time_delta = tamper_creation_time_delta
        self.tamper_image_path = tamper_image_path
        self.tamper_response_nonce = tamper_response_nonce
        self.tamper_hang_after_response = tamper_hang_after_response
        self.worker_code_override = worker_code_override
        self._spawned_procs: list[Any] = []

    def reap_proc(self) -> None:
        """Drena y reapea los subprocesos de prueba para evitar ResourceWarning en Python."""
        for p in list(self._spawned_procs):
            try:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            try:
                if p.stdin:
                    p.stdin.close()
                if p.stdout:
                    p.stdout.close()
                if p.stderr:
                    p.stderr.close()
            except Exception:  # noqa: BLE001
                pass

    def launch_child(
        self,
        token: OperatorPrimaryToken,
        executable_path: pathlib.Path,
        cmdline: str,
    ) -> ChildProcessEvidence:
        _ensure_windows()
        parts = cmdline.split()
        pipe_name = ""
        for i, part in enumerate(parts):
            if part == "--pipe-name" and i + 1 < len(parts):
                pipe_name = parts[i + 1]
                break
        if not pipe_name:
            raise OperatorVerifierLaunchError("No se encontró --pipe-name en el cmdline de prueba")

        if self.worker_code_override is not None:
            script = self.worker_code_override.replace("{PIPE_NAME}", pipe_name)
        else:
            script = (
                "import os, sys\n"
                "from unittest.mock import patch\n"
                "from sky_claw.local.runtime_vault.operator_verifier_bridge import run_verifier_child_worker\n"
                "test_ver = os.environ.get('_SKYCLAW_TEST_RUNTIME_VERSION', '1.6.1170.0')\n"
                "with patch('sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version', return_value=test_ver):\n"
                f"    run_verifier_child_worker({pipe_name!r}, tamper_nonce={self.tamper_response_nonce!r}, tamper_hang_after_response={self.tamper_hang_after_response!r})\n"
            )

        sys_exe = getattr(sys, "_base_executable", sys.executable)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        proc = subprocess.Popen(
            [sys_exe, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self._spawned_procs.append(proc)

        pid = proc.pid
        # SYNCHRONIZE (0x00100000) | PROCESS_TERMINATE (0x0001) | PROCESS_QUERY_INFORMATION (0x0400)
        desired_access = 0x00100000 | 0x0001 | 0x0400
        h_process = _kernel32.OpenProcess(desired_access, False, pid)
        if _is_invalid_handle(h_process):
            proc.kill()
            raise OperatorVerifierLaunchError("OpenProcess sobre subproceso de prueba falló")

        real_creation = _read_process_creation_time(int(h_process)) or 100_000_000
        reported_creation = real_creation + self.tamper_creation_time_delta

        real_image = _read_process_image_path(int(h_process)) or str(executable_path)
        reported_image = self.tamper_image_path if self.tamper_image_path is not None else real_image
        reported_pid = self.tamper_child_pid if self.tamper_child_pid is not None else pid

        return ChildProcessEvidence(
            process_handle=int(h_process),
            pid=reported_pid,
            creation_time=reported_creation,
            image_path=reported_image,
        )

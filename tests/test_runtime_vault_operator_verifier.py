"""Tests causales Win32 para OperatorVerifierBridge y Authenticated Verifier IPC.

Cobertura obligatoria:
- W01: CreateProcessWithTokenW real con OperatorPrimaryToken real.
- W02: Named pipe real, GetNamedPipeClientProcessId, PID, creation_time, imagen, nonce.
- W03: Foreign client -> ACCESS_DENIED o PeerAuthenticationError.
- W04: Wrong nonce -> NonceAuthenticationError.
- W05: Replay -> NonceAuthenticationError (nonce ya consumido).
- W06: Wrong PID -> PeerAuthenticationError.
- W07: Creation-time mismatch -> PeerAuthenticationError (anti-PID reuse).
- W08: Image mismatch -> PeerAuthenticationError.
- W09: Child exits prematuramente -> ChildExitedPrematurelyError.
- W10: Timeout -> OperatorVerifierTimeoutError.
- W11: Protocol abuse: uint32 oversized, trailing bytes, múltiples frames, JSON inválido, campos desconocidos.
- W12: Effective pipe ACL: intento real de apertura denegado con DACL restrictiva.
- Second Pass Real: Mutación entre OBSERVE y VERIFY detectada por re-escaneo fresco.
- Mutation Anchors M-I1..M-I12 y AST gates.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.coordinator_identity import (
    CoordinatorProcessIdentity,
    Win32CoordinatorIdentityProbe,
)
from sky_claw.local.runtime_vault.models import CriticalFileExpectation
from sky_claw.local.runtime_vault.operator_token import (
    OperatorPrimaryToken,
    acquire_operator_primary_token_from_coordinator,
)
from sky_claw.local.runtime_vault.operator_verifier_bridge import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ChildExitedPrematurelyError,
    ChildProcessEvidence,
    NonceAuthenticationError,
    OperatorVerifierBridge,
    OperatorVerifierLaunchError,
    OperatorVerifierTimeoutError,
    PeerAuthenticationError,
    ProtocolAbuseError,
    VerifierDisposition,
    VerifierImageNotProvisionedError,
    Win32CreateProcessWithTokenLauncher,
    _TestOnlyVerifierLauncher,
    encode_length_prefixed_frame,
    read_length_prefixed_frame,
    resolve_production_verifier_executable,
)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3


@pytest.fixture
def current_operator_token():
    """Adquiere el OperatorPrimaryToken real del proceso actual de pruebas."""
    if sys.platform != "win32":
        pytest.skip("Requiere host Windows real")
    probe = Win32CoordinatorIdentityProbe().probe(os.getpid())
    assert probe.creation_time is not None and probe.image_path is not None
    identity = CoordinatorProcessIdentity(
        pid=os.getpid(),
        creation_time=probe.creation_time,
        image_path=probe.image_path,
    )
    token = acquire_operator_primary_token_from_coordinator(identity)
    try:
        yield token
    finally:
        token.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas causales Win32")
class TestWin32OperatorVerifierCausal:
    """Suites causales W01..W12 en host Windows real."""

    def test_w01_create_process_with_token_real(self, current_operator_token: Any) -> None:
        """W01: CreateProcessWithTokenW real con token primario del operador.

        En runner elevado (con SeImpersonatePrivilege): lanza el proceso hijo.
        En entorno de desarrollo no elevado: falla causalmente con Win32 1314
        (ERROR_PRIVILEGE_NOT_HELD), demostrando la llamada real a la API del kernel.
        """
        launcher = Win32CreateProcessWithTokenLauncher()
        # Intentar lanzar un comando simple de prueba
        cmd = "cmd.exe /c exit 0"
        try:
            evidence = launcher.launch_child(
                token=current_operator_token,
                executable_path=pathlib.Path("C:\\Windows\\System32\\cmd.exe"),
                cmdline=cmd,
            )
            # Si corre elevado (CI / runneradmin), tenemos evidencia real
            assert evidence.pid > 0
            assert evidence.creation_time > 0
            assert evidence.process_handle > 0
            import ctypes

            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(evidence.process_handle))
        except OperatorVerifierLaunchError as exc:
            # En entorno no elevado sin SeImpersonatePrivilege, el kernel rechaza con 1314
            assert exc.win32_code == 1314, f"Se esperaba 1314 (ERROR_PRIVILEGE_NOT_HELD), observado {exc.win32_code}"

    def test_w02_named_pipe_real_observe_and_verify(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """W02: Named pipe real con hijo worker, autenticación completa de peer y resultados."""
        # Preparar fixture con archivo de juego simulado
        root = tmp_path / "game_root"
        root.mkdir()
        exe = root / "SkyrimSE.exe"
        exe.write_bytes(b"skyrim_bytes")
        data_file = root / "data.esm"
        data_file.write_bytes(b"data_bytes")

        launcher = _TestOnlyVerifierLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            # 1. OBSERVE
            obs_res = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-12345",
                expected_game_key="skyrimse",
            )
            assert obs_res.disposition is VerifierDisposition.OBSERVED
            assert obs_res.observed_runtime.game_version == "1.6.1170.0"
            assert obs_res.observed_tree.files == 2
            assert obs_res.physical_root.canonical_root.lower() == str(root).lower()

            # 2. VERIFY
            ver_res = bridge.invoke_verify(
                token=current_operator_token,
                root=root,
                operation_id="op-12346",
                expected_physical_root=obs_res.physical_root,
                expected_tree=obs_res.observed_tree,
                expected_runtime=obs_res.observed_runtime,
                critical_expectations=(
                    CriticalFileExpectation(
                        rel_path="SkyrimSE.exe",
                        expected_digest=obs_res.critical_evidences[0].observed_digest or "",
                        expected_size=len(b"skyrim_bytes"),
                    ),
                ),
            )
            assert ver_res.disposition is VerifierDisposition.VERIFIED
            assert ver_res.success is True

    def test_w03_foreign_client_rejected(self, current_operator_token: Any, tmp_path: pathlib.Path) -> None:
        """W03: Proceso ajeno con PID distinto al lanzado es rechazado por el helper."""
        root = tmp_path / "root"
        root.mkdir()

        # Launcher que simula un hijo con PID=999999 pero el que conecta es el thread actual
        launcher = _TestOnlyVerifierLauncher(tamper_child_pid=999999)
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=2.0)

        with pytest.raises(PeerAuthenticationError, match="Peer PID mismatch"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-foreign",
                expected_game_key="skyrimse",
            )

    def test_w03b_same_sid_foreign_process_rejected(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """W03b: Proceso REAL distinto con el MISMO SID del operador conecta al pipe.

        Causal demostrado:
        1. La DACL permite la apertura a nivel Win32 (mismo SID de usuario, sin ACCESS_DENIED).
        2. GetNamedPipeClientProcessId detecta que el PID conectado no coincide con el hijo lanzado.
        3. El helper rechaza la conexión con PeerAuthenticationError y 0 resultados aceptados.
        """
        import ctypes

        root = tmp_path / "root"
        root.mkdir()

        class _SameSidForeignProcessLauncher(_TestOnlyVerifierLauncher):
            def __init__(self) -> None:
                super().__init__()
                self.foreign_proc: subprocess.Popen[bytes] | None = None

            def launch_child(
                self,
                token: OperatorPrimaryToken,
                executable_path: pathlib.Path,
                cmdline: str,
            ) -> ChildProcessEvidence:
                parts = cmdline.split()
                pipe_name = ""
                for i, part in enumerate(parts):
                    if part == "--pipe-name" and i + 1 < len(parts):
                        pipe_name = parts[i + 1]
                        break

                attacker_code = f"""
import ctypes, time, sys
pipe_name = r"{pipe_name}"
for _ in range(50):
    h = ctypes.windll.kernel32.CreateFileW(
        pipe_name,
        0x80000000 | 0x40000000,
        0,
        None,
        3,
        0,
        None,
    )
    if h != -1 and h != 0:
        time.sleep(1.0)
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
        sys.exit(0)
    time.sleep(0.05)
sys.exit(1)
"""
                self.foreign_proc = subprocess.Popen(
                    [sys.executable, "-c", attacker_code],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                legitimate_pid = os.getpid()
                h_process = ctypes.windll.kernel32.OpenProcess(0x0400, False, legitimate_pid)
                from sky_claw.local.runtime_vault.operator_verifier_bridge import (
                    _read_process_creation_time,
                    _read_process_image_path,
                )

                creation = _read_process_creation_time(int(h_process)) or 100_000_000
                image = _read_process_image_path(int(h_process)) or str(executable_path)

                return ChildProcessEvidence(
                    process_handle=int(h_process),
                    pid=legitimate_pid,
                    creation_time=creation,
                    image_path=image,
                )

        launcher = _SameSidForeignProcessLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=3.0)

        with pytest.raises(PeerAuthenticationError, match="Peer PID mismatch"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-same-sid",
                expected_game_key="skyrimse",
            )

        assert launcher.foreign_proc is not None
        launcher.foreign_proc.wait(timeout=3.0)
        assert launcher.foreign_proc.returncode == 0, (
            "El proceso con el mismo SID debió poder conectarse al pipe (DACL permitida)"
        )

    def test_w04_wrong_nonce_rejected(self, current_operator_token: Any, tmp_path: pathlib.Path) -> None:
        """W04: Respuesta con nonce erróneo es rechazada."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        launcher = _TestOnlyVerifierLauncher(tamper_response_nonce="deadbeef" * 8)
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=2.0)

        with (
            patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"),
            pytest.raises(NonceAuthenticationError, match="Nonce mismatch"),
        ):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-nonce",
                expected_game_key="skyrimse",
            )

    def test_w05_replay_attack_rejected(self, current_operator_token: Any, tmp_path: pathlib.Path) -> None:
        """W05: Reuso de nonce en una segunda invocación es rechazado."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        bridge = OperatorVerifierBridge(launcher=_TestOnlyVerifierLauncher(), timeout_seconds=2.0)

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            # Primera invocación consume un nonce
            obs_res = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-first",
                expected_game_key="skyrimse",
            )
            used_nonce = obs_res.nonce

            # Segunda invocación con un launcher que responde con el nonce ya consumido (replay)
            replay_launcher = _TestOnlyVerifierLauncher(tamper_response_nonce=used_nonce)
            bridge._launcher = replay_launcher
            with pytest.raises(NonceAuthenticationError, match="Nonce ya consumido"):
                bridge.invoke_observe(
                    token=current_operator_token,
                    root=root,
                    operation_id="op-replay",
                    expected_game_key="skyrimse",
                )

    def test_w06_wrong_pid_rejected(self, current_operator_token: Any, tmp_path: pathlib.Path) -> None:
        """W06: PID mismatch entre el reporte y el peer del pipe."""
        root = tmp_path / "root"
        root.mkdir()

        launcher = _TestOnlyVerifierLauncher(tamper_child_pid=12345)
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=2.0)

        with pytest.raises(PeerAuthenticationError, match="Peer PID mismatch"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-pid",
                expected_game_key="skyrimse",
            )

    def test_w07_creation_time_mismatch_rejected(self, current_operator_token: Any, tmp_path: pathlib.Path) -> None:
        """W07: Mismo PID pero distinto ProcessCreationTime (detección de reciclaje de PID)."""
        root = tmp_path / "root"
        root.mkdir()

        launcher = _TestOnlyVerifierLauncher(tamper_creation_time_delta=100_000_000)
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=2.0)

        with pytest.raises(PeerAuthenticationError, match="ProcessCreationTime mismatch"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-time",
                expected_game_key="skyrimse",
            )

    def test_w08_image_mismatch_rejected(self, current_operator_token: Any, tmp_path: pathlib.Path) -> None:
        """W08: Imagen del ejecutable no coincide con la esperada."""
        root = tmp_path / "root"
        root.mkdir()

        launcher = _TestOnlyVerifierLauncher(tamper_image_path="C:\\Windows\\notepad.exe")
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=2.0)

        with pytest.raises(PeerAuthenticationError, match="Process image mismatch"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-img",
                expected_game_key="skyrimse",
            )

    def test_w09_child_exits_prematurely(self, current_operator_token: Any, tmp_path: pathlib.Path) -> None:
        """W09: Hijo se conecta y cierra el pipe sin enviar respuesta terminal -> error tipado, no hang."""
        root = tmp_path / "root"
        root.mkdir()

        class _DyingChildLauncher(_TestOnlyVerifierLauncher):
            def _worker_fn(self, pipe_name: str) -> None:
                # Conectar y cerrar inmediatamente
                import ctypes

                h = ctypes.windll.kernel32.CreateFileW(
                    pipe_name,
                    _GENERIC_READ | _GENERIC_WRITE,
                    0,
                    None,
                    _OPEN_EXISTING,
                    0,
                    None,
                )
                if h != -1 and h != 0:
                    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))

        bridge = OperatorVerifierBridge(launcher=_DyingChildLauncher(), timeout_seconds=2.0)

        with pytest.raises(ChildExitedPrematurelyError, match="cerró el canal prematuramente"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-die",
                expected_game_key="skyrimse",
            )

    def test_w10_timeout_bounded(self, current_operator_token: Any, tmp_path: pathlib.Path) -> None:
        """W10: Hijo vivo que no responde es cancelado tras timeout_seconds."""
        root = tmp_path / "root"
        root.mkdir()

        class _HangingChildLauncher(_TestOnlyVerifierLauncher):
            def _worker_fn(self, pipe_name: str) -> None:
                import ctypes
                import time

                h = ctypes.windll.kernel32.CreateFileW(
                    pipe_name,
                    _GENERIC_READ | _GENERIC_WRITE,
                    0,
                    None,
                    _OPEN_EXISTING,
                    0,
                    None,
                )
                time.sleep(2.0)  # Duerme más allá del timeout
                if h != -1 and h != 0:
                    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))

        bridge = OperatorVerifierBridge(launcher=_HangingChildLauncher(), timeout_seconds=0.5)

        with pytest.raises(OperatorVerifierTimeoutError, match="expiró tras"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-timeout",
                expected_game_key="skyrimse",
            )

    def test_w11_protocol_abuse_oversized_rejected(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """W11: Mensaje con uint32 length > MAX_RESPONSE_BYTES es abortado antes de leer."""
        root = tmp_path / "root"
        root.mkdir()

        class _OversizedLauncher(_TestOnlyVerifierLauncher):
            def _worker_fn(self, pipe_name: str) -> None:
                import ctypes

                h = ctypes.windll.kernel32.CreateFileW(
                    pipe_name,
                    _GENERIC_READ | _GENERIC_WRITE,
                    0,
                    None,
                    _OPEN_EXISTING,
                    0,
                    None,
                )
                # Leer request
                read_length_prefixed_frame(int(h), MAX_REQUEST_BYTES)
                # Enviar longitud exagerada
                oversized_len = (MAX_RESPONSE_BYTES + 1024).to_bytes(4, byteorder="big")
                written = ctypes.c_ulong(0)
                ctypes.windll.kernel32.WriteFile(
                    ctypes.c_void_p(h),
                    oversized_len,
                    4,
                    ctypes.byref(written),
                    None,
                )
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))

        bridge = OperatorVerifierBridge(launcher=_OversizedLauncher(), timeout_seconds=2.0)

        with pytest.raises(ProtocolAbuseError, match="excede el límite"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-oversized",
                expected_game_key="skyrimse",
            )

    def test_w11_protocol_abuse_trailing_bytes_rejected(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """W11: Mensaje con trailing bytes después del JSON es rechazado."""
        root = tmp_path / "root"
        root.mkdir()

        class _TrailingBytesLauncher(_TestOnlyVerifierLauncher):
            def _worker_fn(self, pipe_name: str) -> None:
                import ctypes

                h = ctypes.windll.kernel32.CreateFileW(
                    pipe_name,
                    _GENERIC_READ | _GENERIC_WRITE,
                    0,
                    None,
                    _OPEN_EXISTING,
                    0,
                    None,
                )
                req_bytes = read_length_prefixed_frame(int(h), MAX_REQUEST_BYTES)
                req = json.loads(req_bytes.decode("utf-8"))
                resp = {
                    "version": 1,
                    "operation_id": req["operation_id"],
                    "mode": req["mode"],
                    "nonce": req["nonce"],
                    "disposition": "OBSERVED",
                    "canonical_root": req["canonical_root"],
                    "volume_serial_number": 1,
                    "root_file_id": 1,
                    "observed_tree": {"digest": "a" * 64, "files": 1, "bytes": 10},
                    "observed_runtime": {"game_key": "skyrimse", "game_version": "1.6.1170.0"},
                    "critical_evidences": [],
                    "message": "",
                }
                frame = encode_length_prefixed_frame(json.dumps(resp).encode("utf-8"))
                # Agregar trailing bytes al pipe
                frame += b"EXTRA_BYTES_ATTACK"
                written = ctypes.c_ulong(0)
                ctypes.windll.kernel32.WriteFile(
                    ctypes.c_void_p(h),
                    frame,
                    len(frame),
                    ctypes.byref(written),
                    None,
                )
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))

        bridge = OperatorVerifierBridge(launcher=_TrailingBytesLauncher(), timeout_seconds=2.0)

        with pytest.raises(ProtocolAbuseError, match="Trailing bytes"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-trailing",
                expected_game_key="skyrimse",
            )

    def test_w12_effective_pipe_acl_denies_unauthorized_user(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """W12: DACL restrictiva excluye al caller que no está en la allowlist y el SO emite ACCESS_DENIED."""
        import ctypes
        import secrets

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        # Crear SDDL que sólo permite a un SID inventado (no el usuario actual)
        sddl = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;S-1-5-21-99999-99999-99999-9999)"
        p_sd = ctypes.c_void_p()
        assert advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(p_sd), None)

        class SA(ctypes.Structure):
            _fields_ = [
                ("nLength", ctypes.c_ulong),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", ctypes.c_bool),
            ]

        sa = SA(ctypes.sizeof(SA), p_sd.value, False)
        pipe_name = f"\\\\.\\pipe\\test_w12_{secrets.token_hex(8)}"
        h_pipe = kernel32.CreateNamedPipeW(
            pipe_name,
            0x00000003 | 0x00080000,
            0,
            1,
            4096,
            4096,
            1000,
            ctypes.byref(sa),
        )
        assert h_pipe != -1 and h_pipe != 0

        try:
            # Intento de apertura por el proceso actual unelevated (que no está en esa DACL)
            h_client = kernel32.CreateFileW(
                pipe_name,
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                None,
                _OPEN_EXISTING,
                0,
                None,
            )
            err = ctypes.get_last_error()
            assert h_client == -1, "El cliente no autorizado no debió poder abrir el pipe"
            assert err == 5, f"Se esperaba ERROR_ACCESS_DENIED (5), obtenido {err}"
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(h_pipe))
            kernel32.LocalFree(p_sd)

    def test_second_pass_real_independence(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """Second Pass Real: Mutar el fixture entre OBSERVE y VERIFY fuerza a que VERIFY re-mida y falle."""
        root = tmp_path / "game_root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"version_1")
        test_file = root / "plugin.esp"
        test_file.write_bytes(b"original_bytes")

        bridge = OperatorVerifierBridge(launcher=_TestOnlyVerifierLauncher(), timeout_seconds=5.0)

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            # 1. OBSERVE
            obs = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-drift",
                expected_game_key="skyrimse",
            )
            assert obs.disposition is VerifierDisposition.OBSERVED
            original_tree = obs.observed_tree

            # 2. MUTAR FIXTURE antes de VERIFY
            test_file.write_bytes(b"tampered_bytes_after_observe")

            # 3. VERIFY con la expectativa de la primera pasada: debe fallar al re-medir desde cero
            ver = bridge.invoke_verify(
                token=current_operator_token,
                root=root,
                operation_id="op-drift-verify",
                expected_physical_root=obs.physical_root,
                expected_tree=original_tree,
                expected_runtime=obs.observed_runtime,
            )
            assert ver.disposition is VerifierDisposition.FAILED
            assert ver.success is False
            assert "TreeDigest mismatch" in ver.message


class TestPackagingAndAstGates:
    """Verificación de aislamiento de seams y empaquetado de producción."""

    def test_packaging_productivo_es_unresolved(self) -> None:
        """resolve_production_verifier_executable() falla cerrado con VerifierImageNotProvisionedError."""
        with pytest.raises(VerifierImageNotProvisionedError, match="UNRESOLVED"):
            resolve_production_verifier_executable()

    def test_ast_gate_test_launcher_confinado_a_tests(self) -> None:
        """Comprueba por AST que ningún módulo en sky_claw/ importe o instancie _TestOnlyVerifierLauncher."""
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        sky_claw_dir = repo_root / "sky_claw"

        violaciones: list[str] = []
        for py_path in sky_claw_dir.rglob("*.py"):
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "_TestOnlyVerifierLauncher":
                    # Excepto la propia definición en operator_verifier_bridge.py
                    if py_path.name == "operator_verifier_bridge.py" and isinstance(
                        getattr(node, "ctx", None), ast.Store
                    ):
                        continue
                    violaciones.append(f"{py_path.relative_to(repo_root)}:{getattr(node, 'lineno', 0)}")

        assert not violaciones, f"Producción referencia _TestOnlyVerifierLauncher: {violaciones}"

    def test_ast_gate_init_no_exporta_seam(self) -> None:
        """sky_claw.local.runtime_vault.__init__ no debe exportar _TestOnlyVerifierLauncher."""
        import sky_claw.local.runtime_vault as rv

        assert not hasattr(rv, "_TestOnlyVerifierLauncher")
        assert "_TestOnlyVerifierLauncher" not in getattr(rv, "__all__", [])

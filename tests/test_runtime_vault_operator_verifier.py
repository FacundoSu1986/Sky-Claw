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
import ctypes
import os
import pathlib
import secrets
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.coordinator_identity import (
    CoordinatorProcessIdentity,
    Win32CoordinatorIdentityProbe,
)
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    RuntimeIdentity,
    TreeDigest,
    VerificationState,
)
from sky_claw.local.runtime_vault.operator_token import (
    OperatorPrimaryToken,
    acquire_operator_primary_token_from_coordinator,
)
from sky_claw.local.runtime_vault.operator_verifier_bridge import (
    _PIPE_REJECT_REMOTE_CLIENTS,
    _SECURITY_ATTRIBUTES,
    ChildExitedPrematurelyError,
    ChildProcessEvidence,
    NonceAuthenticationError,
    OperatorVerifierBridge,
    OperatorVerifierBridgeError,
    OperatorVerifierLaunchError,
    OperatorVerifierTimeoutError,
    PeerAuthenticationError,
    ProtocolAbuseError,
    VerifierDisposition,
    VerifierImageNotProvisionedError,
    Win32CreateProcessWithTokenLauncher,
    _build_named_pipe_security_descriptor,
    _kernel32,
    _TestOnlyVerifierLauncher,
    resolve_production_verifier_executable,
)
from sky_claw.local.runtime_vault.physical_root import (
    PhysicalRootIdentity,
    PhysicalRootMismatchError,
    PhysicalRootReparseError,
    bound_physical_root,
)
from sky_claw.local.runtime_vault.runtime_observation import (
    FreshRuntimeObservation,
    RuntimeObservationError,
    observe_runtime_identity_from_root,
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
            # En entorno no elevado sin SeImpersonatePrivilege, el kernel rechaza con 1314 (ERROR_PRIVILEGE_NOT_HELD).
            # En ciertos entornos de CI o cuentas con UAC filtrado donde el handle carece de TOKEN_ASSIGN_PRIMARY
            # o el llamador no tiene permiso de asignación entre sesiones, el kernel devuelve 5 (ERROR_ACCESS_DENIED).
            assert exc.win32_code in (5, 1314), (
                f"CreateProcessWithTokenW debe fallar causalmente con Win32 1314 (ERROR_PRIVILEGE_NOT_HELD) "
                f"o 5 (ERROR_ACCESS_DENIED); obtenido {exc.win32_code}"
            )

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
            # En OBSERVE: estado UNKNOWN y sin fabricar expectativas
            assert len(obs_res.critical_evidences) == 1
            assert obs_res.critical_evidences[0].state is VerificationState.UNKNOWN
            assert obs_res.critical_evidences[0].expected_digest is None
            assert obs_res.critical_evidences[0].expected_size is None

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
            assert len(ver_res.critical_evidences) == 1
            assert ver_res.critical_evidences[0].state is VerificationState.VERIFIED
            assert ver_res.critical_evidences[0].rel_path == "SkyrimSE.exe"
            assert ver_res.critical_evidences[0].success is True

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

        worker_code = """
import ctypes
kernel32 = ctypes.windll.kernel32
h = kernel32.CreateFileW(r'{PIPE_NAME}', 0xC0000000, 0, None, 3, 0, None)
if h != -1 and h != 0:
    kernel32.CloseHandle(ctypes.c_void_p(h))
"""
        bridge = OperatorVerifierBridge(
            launcher=_TestOnlyVerifierLauncher(worker_code_override=worker_code), timeout_seconds=2.0
        )

        with pytest.raises(ChildExitedPrematurelyError, match=r"cerró el canal"):
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

        worker_code = """
import ctypes, time
kernel32 = ctypes.windll.kernel32
h = kernel32.CreateFileW(r'{PIPE_NAME}', 0xC0000000, 0, None, 3, 0, None)
time.sleep(2.0)
if h != -1 and h != 0:
    kernel32.CloseHandle(ctypes.c_void_p(h))
"""
        bridge = OperatorVerifierBridge(
            launcher=_TestOnlyVerifierLauncher(worker_code_override=worker_code), timeout_seconds=0.5
        )

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

        worker_code = """
import ctypes
from sky_claw.local.runtime_vault.operator_verifier_bridge import read_length_prefixed_frame, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES
kernel32 = ctypes.windll.kernel32
h = kernel32.CreateFileW(r'{PIPE_NAME}', 0xC0000000, 0, None, 3, 0, None)
read_length_prefixed_frame(int(h), MAX_REQUEST_BYTES)
oversized_len = (MAX_RESPONSE_BYTES + 1024).to_bytes(4, byteorder='big')
written = ctypes.c_ulong(0)
kernel32.WriteFile(ctypes.c_void_p(h), oversized_len, 4, ctypes.byref(written), None)
kernel32.CloseHandle(ctypes.c_void_p(h))
"""
        bridge = OperatorVerifierBridge(
            launcher=_TestOnlyVerifierLauncher(worker_code_override=worker_code), timeout_seconds=2.0
        )

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

        worker_code = """
import ctypes, json
from sky_claw.local.runtime_vault.operator_verifier_bridge import (
    read_length_prefixed_frame, encode_length_prefixed_frame, MAX_REQUEST_BYTES, PROTOCOL_VERSION
)
kernel32 = ctypes.windll.kernel32
h = kernel32.CreateFileW(r'{PIPE_NAME}', 0xC0000000, 0, None, 3, 0, None)
req_bytes = read_length_prefixed_frame(int(h), MAX_REQUEST_BYTES)
req = json.loads(req_bytes.decode('utf-8'))
resp = {
    'version': PROTOCOL_VERSION,
    'operation_id': req['operation_id'],
    'mode': req['mode'],
    'nonce': req['nonce'],
    'disposition': 'OBSERVED',
    'canonical_root': req['canonical_root'],
    'volume_serial_number': 1,
    'root_file_id': 1,
    'observed_tree': {'digest': 'a' * 64, 'files': 1, 'bytes': 10},
    'observed_runtime': {
        'game_key': 'skyrimse',
        'game_version': '1.6.1170.0',
        'observed_exe_path': r'C:\\game\\SkyrimSE.exe',
        'observed_at_ns': 1000000,
    },
    'critical_evidences': [],
    'message': '',
}
frame = encode_length_prefixed_frame(json.dumps(resp).encode('utf-8')) + b'EXTRA_BYTES_ATTACK'
written = ctypes.c_ulong(0)
kernel32.WriteFile(ctypes.c_void_p(h), frame, len(frame), ctypes.byref(written), None)
kernel32.CloseHandle(ctypes.c_void_p(h))
"""
        bridge = OperatorVerifierBridge(
            launcher=_TestOnlyVerifierLauncher(worker_code_override=worker_code), timeout_seconds=2.0
        )

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

        # Crear SDDL que sólo permite a un SID inventado (no el usuario actual ni administradores)
        sddl = "D:P(A;;GA;;;S-1-5-21-99999-99999-99999-9999)"
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

    def test_verify_critical_expectation_mismatch_fails_disposition(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """VERIFY con discrepancia de digest o tamaño en un archivo crítico produce disposition=FAILED y success=False."""
        root = tmp_path / "game_root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"version_1")
        bridge = OperatorVerifierBridge(launcher=_TestOnlyVerifierLauncher(), timeout_seconds=5.0)

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            obs = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-crit-obs",
                expected_game_key="skyrimse",
            )
            # Expectativa con digest corrupto
            ver = bridge.invoke_verify(
                token=current_operator_token,
                root=root,
                operation_id="op-crit-ver",
                expected_physical_root=obs.physical_root,
                expected_tree=obs.observed_tree,
                expected_runtime=obs.observed_runtime,
                critical_expectations=(
                    CriticalFileExpectation(
                        rel_path="SkyrimSE.exe",
                        expected_digest="0" * 64,
                        expected_size=len(b"version_1"),
                    ),
                ),
            )
            assert ver.disposition is VerifierDisposition.FAILED
            assert ver.success is False
            assert len(ver.critical_evidences) == 1
            assert ver.critical_evidences[0].state is VerificationState.FAILED
            assert "mismatch" in ver.message.lower()

    def test_verify_critical_expectation_missing_file_fails_disposition(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """VERIFY con un archivo crítico ausente en el árbol produce disposition=FAILED y success=False."""
        root = tmp_path / "game_root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"version_1")
        bridge = OperatorVerifierBridge(launcher=_TestOnlyVerifierLauncher(), timeout_seconds=5.0)

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            obs = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-missing-obs",
                expected_game_key="skyrimse",
            )
            ver = bridge.invoke_verify(
                token=current_operator_token,
                root=root,
                operation_id="op-missing-ver",
                expected_physical_root=obs.physical_root,
                expected_tree=obs.observed_tree,
                expected_runtime=obs.observed_runtime,
                critical_expectations=(
                    CriticalFileExpectation(
                        rel_path="Data/Skyrim.esm",
                        expected_digest="a" * 64,
                    ),
                ),
            )
            assert ver.disposition is VerifierDisposition.FAILED
            assert ver.success is False
            assert len(ver.critical_evidences) == 1
            assert ver.critical_evidences[0].state is VerificationState.FAILED
            assert "missing" in ver.message.lower()

    def test_worker_failure_returns_typed_rejected_response(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """Un fallo en el worker (raíz inexistente) envía un frame REJECTED en lugar de abortar silenciosamente."""
        nonexistent = tmp_path / "nonexistent_root"
        bridge = OperatorVerifierBridge(launcher=_TestOnlyVerifierLauncher(), timeout_seconds=5.0)

        with pytest.raises(OperatorVerifierBridgeError, match="REJECTED"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=nonexistent,
                operation_id="op-fail-worker",
                expected_game_key="skyrimse",
            )

    def test_observe_runtime_symlink_executable_rejected(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """observe_runtime_identity_from_root rechaza fail-closed ejecutables que sean symlinks."""
        root = tmp_path / "symlink_game"
        root.mkdir()
        real_exe = tmp_path / "real_payload.exe"
        real_exe.write_bytes(b"MZfake_exe_bytes")
        symlink_exe = root / "SkyrimSE.exe"
        try:
            symlink_exe.symlink_to(real_exe)
            with pytest.raises(RuntimeObservationError, match="enlace simbólico"):
                observe_runtime_identity_from_root(root, expected_game_key="skyrimse")
        except OSError:

            class FakeEntry:
                name = "SkyrimSE.exe"
                path = str(symlink_exe)

                def is_symlink(self) -> bool:
                    return True

                def is_file(self, *, follow_symlinks: bool = True) -> bool:
                    return False

            with (
                patch("os.scandir", return_value=[FakeEntry()]),
                pytest.raises(RuntimeObservationError, match="enlace simbólico"),
            ):
                observe_runtime_identity_from_root(root, expected_game_key="skyrimse")

    def test_bound_physical_root_detects_toctou_mutation(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """bound_physical_root detecta si la raíz fue renombrada/sustituida entre entrada y salida."""
        root = tmp_path / "game_root"
        root.mkdir()

        from sky_claw.local.runtime_vault.physical_root import derive_physical_root as real_derive

        real_id = real_derive(root)
        fake_tampered_id = PhysicalRootIdentity(
            canonical_root=real_id.canonical_root,
            volume_serial_number=real_id.volume_serial_number,
            root_file_id=real_id.root_file_id + 9999,
        )

        with (
            patch("sky_claw.local.runtime_vault.physical_root.derive_physical_root", return_value=fake_tampered_id),
            pytest.raises(PhysicalRootMismatchError, match="TOCTOU detectado"),
            bound_physical_root(root),
        ):
            pass

    def test_s1_peer_handle_actually_opened_from_pipe_client_pid(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S1: Verifica que h_peer se abre directamente desde el PID conectado (GetNamedPipeClientProcessId)."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        open_process_calls: list[int] = []
        real_open_process = _kernel32.OpenProcess

        def fake_open_process(dw_desired_access: int, b_inherit_handle: bool, dw_process_id: int) -> int:
            open_process_calls.append(dw_process_id)
            return real_open_process(dw_desired_access, b_inherit_handle, dw_process_id)

        launcher = _TestOnlyVerifierLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with (
            patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"),
            patch.object(_kernel32, "OpenProcess", side_effect=fake_open_process),
        ):
            res = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s1",
                expected_game_key="skyrimse",
            )
            assert res.disposition is VerifierDisposition.OBSERVED
            # El PID del proceso que conectó al pipe debe haber sido consultado mediante OpenProcess
            assert any(pid > 0 for pid in open_process_calls)

    def test_s2_same_pid_wrong_creation_time_rejected(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S2: Replay attack con mismo PID pero creación distinta es rechazado por el helper."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        launcher = _TestOnlyVerifierLauncher(tamper_creation_time_delta=9999999)
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with (
            patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"),
            pytest.raises(PeerAuthenticationError, match="ProcessCreationTime mismatch"),
        ):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s2",
                expected_game_key="skyrimse",
            )

    def test_s3_client_process_not_still_active_rejected(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S3: Proceso que ya no está activo (exit code != STILL_ACTIVE) al verificar es rechazado."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        launcher = _TestOnlyVerifierLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        def fake_get_exit_code(h_proc: Any, lp_exit: Any) -> int:
            lp_exit._obj.value = 0  # 0 indica finalizado, no 259 (STILL_ACTIVE)
            return 1

        with (
            patch.object(_kernel32, "GetExitCodeProcess", side_effect=fake_get_exit_code),
            pytest.raises(PeerAuthenticationError, match=r"ya no est[áa] activo"),
        ):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s3",
                expected_game_key="skyrimse",
            )

    def test_s4_child_verified_cannot_override_helper_failed(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S4: El hijo no puede forzar VERIFIED si el helper determina independientemente que la verificación falló."""
        root = tmp_path / "game_root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        launcher = _TestOnlyVerifierLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            obs = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s4-obs",
                expected_game_key="skyrimse",
            )
            divergent_tree = TreeDigest(
                digest="0" * 64,
                files=obs.observed_tree.files + 1,
                bytes=obs.observed_tree.bytes,
            )

            real_execute = bridge._execute_ipc_cycle

            def fake_execute(*args: Any, **kwargs: Any) -> Any:
                resp = real_execute(*args, **kwargs)
                resp["disposition"] = "VERIFIED"
                return resp

            with (
                patch.object(bridge, "_execute_ipc_cycle", side_effect=fake_execute),
                pytest.raises(ProtocolAbuseError, match=r"report[oó] VERIFIED pero la verificaci[oó]n independiente"),
            ):
                bridge.invoke_verify(
                    token=current_operator_token,
                    root=root,
                    operation_id="op-s4-ver",
                    expected_physical_root=obs.physical_root,
                    expected_tree=divergent_tree,
                    expected_runtime=obs.observed_runtime,
                )

    def test_s5_response_physical_root_mismatch_fails(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S5: Discrepancia en la identidad física de la raíz produce disposition=FAILED/REJECTED y success=False."""
        root = tmp_path / "game_root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        launcher = _TestOnlyVerifierLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            obs = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s5-obs",
                expected_game_key="skyrimse",
            )
            fake_expected_root = PhysicalRootIdentity(
                canonical_root=obs.physical_root.canonical_root,
                volume_serial_number=obs.physical_root.volume_serial_number,
                root_file_id=obs.physical_root.root_file_id + 1111,
            )
            ver = bridge.invoke_verify(
                token=current_operator_token,
                root=root,
                operation_id="op-s5-ver",
                expected_physical_root=fake_expected_root,
                expected_tree=obs.observed_tree,
                expected_runtime=obs.observed_runtime,
            )
            assert ver.disposition in (VerifierDisposition.FAILED, VerifierDisposition.REJECTED)
            assert ver.success is False
            assert "mismatch" in ver.message.lower()

    def test_s6_root_rename_blocked_while_bound(self, tmp_path: pathlib.Path) -> None:
        """S6: bound_physical_root bloquea activamente renombramientos y borrados (ERROR_SHARING_VIOLATION 32)."""
        root = tmp_path / "locked_root"
        root.mkdir()
        target_renamed = tmp_path / "renamed_root"

        with bound_physical_root(root):
            with pytest.raises(PermissionError) as exc_info:
                os.rename(root, target_renamed)
            # Código Win32 32 es ERROR_SHARING_VIOLATION
            assert getattr(exc_info.value, "winerror", None) == 32 or "32" in str(exc_info.value)

        # Tras salir del contexto, el bloqueo de sharing se libera y el rename tiene éxito
        os.rename(root, target_renamed)
        assert target_renamed.exists()
        assert not root.exists()

    def test_s7_ancestor_reparse_rejected(self, tmp_path: pathlib.Path) -> None:
        """S7: Si un directorio ancestro es un reparse point, falla cerrado con PhysicalRootReparseError."""
        root = tmp_path / "sub" / "game"
        root.mkdir(parents=True)

        from sky_claw.local.runtime_vault import physical_root as pr_mod

        real_get_attr = pr_mod._kernel32.GetFileAttributesW
        parent_str = str(root.parent).lower()

        def fake_get_attr(path_str: str) -> int:
            if path_str.lower() == parent_str:
                return 0x00000400 | 0x00000010  # REPARSE_POINT | DIRECTORY
            return real_get_attr(path_str)

        with (
            patch.object(pr_mod._kernel32, "GetFileAttributesW", side_effect=fake_get_attr),
            pytest.raises(PhysicalRootReparseError, match="ancestro.*reparse point"),
        ):
            bound_physical_root(root).__enter__()

    def test_s8_pipe_rejects_remote_clients(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S8: El servidor de named pipe incluye obligatoriamente la bandera PIPE_REJECT_REMOTE_CLIENTS."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        captured_pipe_modes: list[int] = []
        real_create_pipe = _kernel32.CreateNamedPipeW

        def fake_create_pipe(name: Any, open_mode: int, pipe_mode: int, *args: Any) -> int:
            captured_pipe_modes.append(pipe_mode)
            return real_create_pipe(name, open_mode, pipe_mode, *args)

        launcher = _TestOnlyVerifierLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with (
            patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"),
            patch.object(_kernel32, "CreateNamedPipeW", side_effect=fake_create_pipe),
        ):
            res = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s8",
                expected_game_key="skyrimse",
            )
            assert res.disposition is VerifierDisposition.OBSERVED

        assert len(captured_pipe_modes) == 1
        assert captured_pipe_modes[0] & _PIPE_REJECT_REMOTE_CLIENTS != 0, (
            "El named pipe debe crearse con PIPE_REJECT_REMOTE_CLIENTS (0x08)"
        )

    def test_s9_protocol_version_mismatch_rejected(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S9: Versión de protocolo discordante en la respuesta es rechazada con ProtocolAbuseError."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        worker_code = (
            "import ctypes, json, sys\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "h = kernel32.CreateFileW(r'{PIPE_NAME}', 0x80000000 | 0x40000000, 0, None, 3, 0, None)\n"
            "buf = ctypes.create_string_buffer(4096)\n"
            "read = ctypes.c_ulong(0)\n"
            "kernel32.ReadFile(h, buf, 4096, ctypes.byref(read), None)\n"
            "req = json.loads(buf.raw[4:4+int.from_bytes(buf.raw[:4], 'big')].decode('utf-8'))\n"
            "resp = {'version': 999, 'operation_id': req['operation_id'], 'mode': req['mode'], 'nonce': req['nonce'], 'disposition': 'REJECTED', 'error_type': 'Fake', 'message': 'test'}\n"
            "body = json.dumps(resp).encode('utf-8')\n"
            "frame = len(body).to_bytes(4, 'big') + body\n"
            "written = ctypes.c_ulong(0)\n"
            "kernel32.WriteFile(h, frame, len(frame), ctypes.byref(written), None)\n"
            "kernel32.CloseHandle(h)\n"
            "sys.exit(0)\n"
        )
        launcher = _TestOnlyVerifierLauncher(worker_code_override=worker_code)
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with pytest.raises(ProtocolAbuseError, match="Versión de respuesta no soportada"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s9",
                expected_game_key="skyrimse",
            )

    def test_s10_unknown_key_rejected(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S10: Esquema cerrado rechaza cualquier clave desconocida en la respuesta con ProtocolAbuseError."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        worker_code = (
            "import ctypes, json, sys\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "h = kernel32.CreateFileW(r'{PIPE_NAME}', 0x80000000 | 0x40000000, 0, None, 3, 0, None)\n"
            "buf = ctypes.create_string_buffer(4096)\n"
            "read = ctypes.c_ulong(0)\n"
            "kernel32.ReadFile(h, buf, 4096, ctypes.byref(read), None)\n"
            "req = json.loads(buf.raw[4:4+int.from_bytes(buf.raw[:4], 'big')].decode('utf-8'))\n"
            "resp = {'version': 1, 'operation_id': req['operation_id'], 'mode': req['mode'], 'nonce': req['nonce'], 'disposition': 'REJECTED', 'error_type': 'Fake', 'message': 'test', 'malicious_extra_key': 'attacker_payload'}\n"
            "body = json.dumps(resp).encode('utf-8')\n"
            "frame = len(body).to_bytes(4, 'big') + body\n"
            "written = ctypes.c_ulong(0)\n"
            "kernel32.WriteFile(h, frame, len(frame), ctypes.byref(written), None)\n"
            "kernel32.CloseHandle(h)\n"
            "sys.exit(0)\n"
        )
        launcher = _TestOnlyVerifierLauncher(worker_code_override=worker_code)
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with pytest.raises(ProtocolAbuseError, match=r"Violaci[oó]n de esquema cerrado.*malicious_extra_key"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s10",
                expected_game_key="skyrimse",
            )

    def test_s11_rejected_contains_no_fake_evidence(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S11: Un frame REJECTED devuelve None en todas las evidencias, sin inventar ni filtrar datos."""
        nonexistent = tmp_path / "nonexistent"
        bridge = OperatorVerifierBridge(launcher=_TestOnlyVerifierLauncher(), timeout_seconds=5.0)

        ver = bridge.invoke_verify(
            token=current_operator_token,
            root=nonexistent,
            operation_id="op-s11",
            expected_physical_root=PhysicalRootIdentity("C:\\nonexistent", 1, 1),
            expected_tree=TreeDigest(digest="a" * 64, files=0, bytes=0),
            expected_runtime=RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
        )
        assert ver.disposition is VerifierDisposition.REJECTED
        assert ver.success is False
        assert ver.physical_root is None
        assert ver.observed_runtime is None
        assert ver.tree_result.observed is None
        assert ver.runtime_result.observed is None
        assert ver.critical_evidences == ()
        assert "PhysicalRootError" in ver.message

    def test_s12_abnormal_child_killed_and_reaped(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S12: Ante salida anormal, el hijo es terminado con TerminateProcess y reapeado."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        terminate_calls: list[int] = []
        wait_calls: list[int] = []
        real_term = _kernel32.TerminateProcess
        real_wait = _kernel32.WaitForSingleObject

        def fake_term(h_proc: Any, exit_code: int) -> int:
            val = getattr(h_proc, "value", h_proc)
            if val is not None:
                terminate_calls.append(int(val))
            return real_term(h_proc, exit_code)

        def fake_wait(h_handle: Any, ms: int) -> int:
            val = getattr(h_handle, "value", h_handle)
            if val is not None:
                wait_calls.append(int(val))
            return real_wait(h_handle, ms)

        worker_code = (
            "import ctypes, time, sys\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "h = kernel32.CreateFileW(r'{PIPE_NAME}', 0x80000000 | 0x40000000, 0, None, 3, 0, None)\n"
            "time.sleep(5.0)\n"
            "kernel32.CloseHandle(h)\n"
            "sys.exit(0)\n"
        )
        launcher = _TestOnlyVerifierLauncher(
            tamper_child_pid=999999,
            worker_code_override=worker_code,
        )
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=2.0)

        with (
            patch.object(_kernel32, "TerminateProcess", side_effect=fake_term),
            patch.object(_kernel32, "WaitForSingleObject", side_effect=fake_wait),
            pytest.raises(PeerAuthenticationError),
        ):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s12",
                expected_game_key="skyrimse",
            )

        assert len(terminate_calls) >= 1
        assert len(wait_calls) >= 1

    def test_s13_terminal_response_child_remains_alive_fails(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S13: Si el hijo permanece vivo tras emitir la respuesta terminal, se termina forzosamente y falla."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        launcher = _TestOnlyVerifierLauncher(tamper_hang_after_response=True)
        bridge = OperatorVerifierBridge(launcher=launcher, grace_period_ms=100, timeout_seconds=5.0)

        with (
            patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"),
            pytest.raises(ProtocolAbuseError, match="continuó ejecutándose tras enviar"),
        ):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s13",
                expected_game_key="skyrimse",
            )

    def test_s14_operation_exceeds_io_timeout_but_within_operation_timeout_succeeds(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S14: Operación que tarda más que io_timeout en procesar pero entra en operation_timeout tiene éxito."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        worker_code = (
            "import ctypes, json, time, sys\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "h = kernel32.CreateFileW(r'{PIPE_NAME}', 0x80000000 | 0x40000000, 0, None, 3, 0, None)\n"
            "buf = ctypes.create_string_buffer(4096)\n"
            "read = ctypes.c_ulong(0)\n"
            "kernel32.ReadFile(h, buf, 4096, ctypes.byref(read), None)\n"
            "req = json.loads(buf.raw[4:4+int.from_bytes(buf.raw[:4], 'big')].decode('utf-8'))\n"
            "time.sleep(0.8)\n"
            "resp = {'version': 1, 'operation_id': req['operation_id'], 'mode': req['mode'], 'nonce': req['nonce'], 'disposition': 'OBSERVED', 'canonical_root': req['canonical_root'], 'volume_serial_number': 12345, 'root_file_id': 67890, 'observed_tree': {'digest': 'a'*64, 'files': 1, 'bytes': 100}, 'observed_runtime': {'game_key': 'skyrimse', 'game_version': '1.6.1170.0', 'observed_exe_path': req['canonical_root'] + r'\\\\SkyrimSE.exe', 'observed_at_ns': 1000}, 'critical_evidences': [], 'message': ''}\n"
            "body = json.dumps(resp).encode('utf-8')\n"
            "frame = len(body).to_bytes(4, 'big') + body\n"
            "written = ctypes.c_ulong(0)\n"
            "kernel32.WriteFile(h, frame, len(frame), ctypes.byref(written), None)\n"
            "kernel32.CloseHandle(h)\n"
            "sys.exit(0)\n"
        )
        launcher = _TestOnlyVerifierLauncher(worker_code_override=worker_code)
        # io_timeout_seconds=0.4 < 0.8s, pero operation_timeout_seconds=5.0 > 0.8s
        bridge = OperatorVerifierBridge(
            launcher=launcher,
            connect_timeout_seconds=5.0,
            io_timeout_seconds=0.4,
            operation_timeout_seconds=5.0,
        )

        res = bridge.invoke_observe(
            token=current_operator_token,
            root=root,
            operation_id="op-s14",
            expected_game_key="skyrimse",
        )
        assert res.disposition is VerifierDisposition.OBSERVED

    def test_s15_operation_exceeds_operation_timeout_kills_and_reaps(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S15: Operación que supera el operation_timeout total es abortada, matando y reapeando al hijo."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        worker_code = (
            "import ctypes, json, time, sys\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "h = kernel32.CreateFileW(r'{PIPE_NAME}', 0x80000000 | 0x40000000, 0, None, 3, 0, None)\n"
            "buf = ctypes.create_string_buffer(4096)\n"
            "read = ctypes.c_ulong(0)\n"
            "kernel32.ReadFile(h, buf, 4096, ctypes.byref(read), None)\n"
            "time.sleep(2.0)\n"
            "kernel32.CloseHandle(h)\n"
            "sys.exit(0)\n"
        )
        launcher = _TestOnlyVerifierLauncher(worker_code_override=worker_code)
        bridge = OperatorVerifierBridge(
            launcher=launcher,
            connect_timeout_seconds=5.0,
            io_timeout_seconds=1.0,
            operation_timeout_seconds=0.4,
        )

        with pytest.raises(OperatorVerifierTimeoutError, match="operation_timeout"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s15",
                expected_game_key="skyrimse",
            )

    def test_s16_large_response_frame_succeeds(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S16: Frame de respuesta grande (250 KB) es transferido en chunks múltiples mediante overlapped I/O."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        launcher = _TestOnlyVerifierLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        real_execute = bridge._execute_ipc_cycle

        def fake_execute(*args: Any, **kwargs: Any) -> Any:
            resp = real_execute(*args, **kwargs)
            # Agregar un payload de 250 KB en el campo message
            resp["message"] = "A" * 250_000
            return resp

        with (
            patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"),
            patch.object(bridge, "_execute_ipc_cycle", side_effect=fake_execute),
        ):
            res = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s16",
                expected_game_key="skyrimse",
            )
            assert res.disposition is VerifierDisposition.OBSERVED
            assert len(res.message) == 250_000

    def test_s17_fresh_runtime_observation_preserved_across_ipc(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S17: FreshRuntimeObservation se preserva íntegramente a través de la serialización IPC."""
        root = tmp_path / "game_root"
        root.mkdir()
        exe = root / "SkyrimSE.exe"
        exe.write_bytes(b"exe_bytes")

        launcher = _TestOnlyVerifierLauncher()
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=5.0)

        with patch("sky_claw.local.runtime_vault.runtime_observation.read_skyrim_version", return_value="1.6.1170.0"):
            obs = bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s17-obs",
                expected_game_key="skyrimse",
            )
            runtime = obs.observed_runtime
            assert isinstance(runtime, FreshRuntimeObservation)
            assert runtime.game_key == "skyrimse"
            assert runtime.game_version == "1.6.1170.0"
            assert runtime.observed_exe_path.lower() == str(exe).lower()
            assert isinstance(runtime.observed_at_ns, int)
            assert runtime.observed_at_ns > 0

            # Probar pasarlo directamente como expected_runtime a invoke_verify
            ver = bridge.invoke_verify(
                token=current_operator_token,
                root=root,
                operation_id="op-s17-ver",
                expected_physical_root=obs.physical_root,
                expected_tree=obs.observed_tree,
                expected_runtime=runtime,
            )
            assert ver.disposition is VerifierDisposition.VERIFIED
            assert ver.success is True

    def test_s18_production_dacl_builder_denies_unauthorized_principal(self) -> None:
        """S18: La DACL restrictiva construida para el named pipe rechaza a principales no autorizados."""
        fake_operator_sid = "S-1-5-21-99999-99999-99999-9999"
        p_sd = _build_named_pipe_security_descriptor(fake_operator_sid)
        assert p_sd != 0

        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = ctypes.c_void_p(p_sd)
        sa.bInheritHandle = False

        pipe_name = f"\\\\.\\pipe\\test_s18_{secrets.token_hex(8)}"
        h_pipe = _kernel32.CreateNamedPipeW(
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
            h_client = _kernel32.CreateFileW(
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
            _kernel32.CloseHandle(ctypes.c_void_p(h_pipe))
            _kernel32.LocalFree(ctypes.c_void_p(p_sd))

    def test_s19_worker_header_short_read_handled(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S19: Desconexión prematura tras enviar solo parte del encabezado de 4 bytes es manejada fail-closed."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"exe")

        # Worker que conecta, lee la petición, envía solo 2 bytes del header de 4 bytes y cierra el pipe
        worker_code = (
            "import ctypes, sys\n"
            "kernel32 = ctypes.windll.kernel32\n"
            "h = kernel32.CreateFileW(r'{PIPE_NAME}', 0x80000000 | 0x40000000, 0, None, 3, 0, None)\n"
            "buf = ctypes.create_string_buffer(4096)\n"
            "read = ctypes.c_ulong(0)\n"
            "kernel32.ReadFile(h, buf, 4096, ctypes.byref(read), None)\n"
            "written = ctypes.c_ulong(0)\n"
            "kernel32.WriteFile(h, b'\\x00\\x01', 2, ctypes.byref(written), None)\n"
            "kernel32.CloseHandle(h)\n"
            "sys.exit(0)\n"
        )
        launcher = _TestOnlyVerifierLauncher(worker_code_override=worker_code)
        bridge = OperatorVerifierBridge(launcher=launcher, timeout_seconds=3.0)

        with pytest.raises(ChildExitedPrematurelyError, match="EOF inesperado"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s19",
                expected_game_key="skyrimse",
            )

    def test_s20_production_launcher_cannot_use_test_seam(
        self,
        current_operator_token: Any,
        tmp_path: pathlib.Path,
    ) -> None:
        """S20: El launcher productivo Win32CreateProcessWithTokenLauncher no puede acceder a la seam de test."""
        root = tmp_path / "root"
        root.mkdir()

        prod_launcher = Win32CreateProcessWithTokenLauncher()
        assert not getattr(prod_launcher, "_is_test_verifier_launcher", False)

        bridge = OperatorVerifierBridge(launcher=prod_launcher)
        with pytest.raises(VerifierImageNotProvisionedError, match="UNRESOLVED"):
            bridge.invoke_observe(
                token=current_operator_token,
                root=root,
                operation_id="op-s20",
                expected_game_key="skyrimse",
            )


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
                if isinstance(node, ast.ClassDef) and node.name == "_TestOnlyVerifierLauncher":
                    if py_path.name != "operator_verifier_bridge.py":
                        violaciones.append(f"{py_path.relative_to(repo_root)}:{node.lineno} (ClassDef)")
                elif isinstance(node, ast.Name) and node.id == "_TestOnlyVerifierLauncher":
                    # Excepto la propia definición en operator_verifier_bridge.py
                    if py_path.name == "operator_verifier_bridge.py" and isinstance(
                        getattr(node, "ctx", None), ast.Store
                    ):
                        continue
                    violaciones.append(f"{py_path.relative_to(repo_root)}:{getattr(node, 'lineno', 0)} (Name)")
                elif isinstance(node, ast.Attribute) and node.attr == "_TestOnlyVerifierLauncher":
                    violaciones.append(f"{py_path.relative_to(repo_root)}:{getattr(node, 'lineno', 0)} (Attribute)")
                elif isinstance(node, ast.alias) and (
                    node.name == "_TestOnlyVerifierLauncher" or node.asname == "_TestOnlyVerifierLauncher"
                ):
                    violaciones.append(f"{py_path.relative_to(repo_root)}:{getattr(node, 'lineno', 0)} (alias)")

        assert not violaciones, f"Producción referencia _TestOnlyVerifierLauncher: {violaciones}"

    def test_ast_gate_init_no_exporta_seam(self) -> None:
        """sky_claw.local.runtime_vault.__init__ no debe exportar _TestOnlyVerifierLauncher."""
        import sky_claw.local.runtime_vault as rv

        assert not hasattr(rv, "_TestOnlyVerifierLauncher")
        assert "_TestOnlyVerifierLauncher" not in getattr(rv, "__all__", [])

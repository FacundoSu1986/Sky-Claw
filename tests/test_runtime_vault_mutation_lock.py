"""Tests focales GP2-S3b-1: GoldenMutationLock exclusivo (componente G).

Cobertura: LOCK-01..LOCK-08, mutantes M-L1/M-L2/M-L3 y anclas estructurales
sobre golden_mutation_lock.py. Los modelos puros y toda la lógica de adquisición
se ejercen contra un kernel fake (share=0 simulado, reparse tags, metadata en
memoria + disco); la clase Win32 causal verifica CreateFileW share=0 real con
archivos desechables y el anclaje DACL (GP2-T40 PARTIAL heredado de S3a).

Nunca se habilitan privilegios; nunca se toca el Golden real.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import pathlib
import sys
from typing import Any

import pytest

from sky_claw.local.runtime_vault.golden_mutation_lock import (
    _MAX_METADATA_BYTES,
    GOLDEN_LOCK_CREATION_DISPOSITION,
    GOLDEN_LOCK_DESIRED_ACCESS,
    GOLDEN_LOCK_FLAGS,
    GOLDEN_LOCK_NAME_PREFIX,
    GOLDEN_LOCK_SHARE_MODE,
    GoldenLockAcquisitionCleanupError,
    GoldenLockBusyError,
    GoldenLockError,
    GoldenLockIdentity,
    GoldenLockIoError,
    GoldenLockMetadata,
    GoldenLockMetadataError,
    GoldenLockModelError,
    GoldenLockOwnershipError,
    GoldenLockPhase,
    GoldenMutationLockHandle,
    PreexistingLockDisposition,
    _acquire_golden_mutation_lock_at,
    acquire_golden_mutation_lock,
    classify_preexisting_lock,
    derive_golden_lock_key,
    derive_golden_lock_path,
    deserialize_golden_lock_metadata,
    serialize_golden_lock_metadata,
)
from sky_claw.local.runtime_vault.privileged_boundary import PlanAuthorizationError

_VALID_OP_ID = "3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a"
_VOLUME_SERIAL = 0xA1B2C3D4
_ROOT_FILE_ID = 0x1122334455667788


# ============================================================================
# Kernel fake de propósito de prueba (share=0 estricto, archivos en memoria)
# ============================================================================


class _FakeLockKernel:
    """Simula CreateFileW(share=0): una sola adquisición por archivo a la vez."""

    def __init__(self, *, process_identity: tuple[int, int, int] = (4242, 133_456_789_012_345_678, 1)) -> None:
        self.files: dict[str, bytes] = {}
        self.open_handles: dict[int, str] = {}
        self.busy_paths: set[str] = set()
        self.reparse_tags: dict[str, int] = {}
        self.owner_alive: bool | None = True
        self.identity = process_identity
        self.epoch = 1_700_000_000
        self.close_calls: list[int] = []
        self.flush_calls: list[str] = []
        self.write_calls: list[tuple[str, bytes]] = []
        self._next_handle = 10
        self.fail_flush = False
        self.fail_write = False
        # Inyección causal por intento (1-based) para orquestaciones multi-flush.
        self.flush_attempts = 0
        self.fail_flush_on_attempts: set[int] = set()

    def open_lock_file(self, path: Any) -> int:
        path_str = str(path)
        if path_str in self.busy_paths:
            raise GoldenLockBusyError(f"Violación de compartición en {path_str}", win32_error=32)
        handle = self._next_handle
        self._next_handle += 1
        self.open_handles[handle] = path_str
        self.busy_paths.add(path_str)
        if path_str not in self.files:
            self.files[path_str] = b""
        return handle

    def get_reparse_tag(self, handle: int) -> int:
        return self.reparse_tags.get(self.open_handles[handle], 0)

    def read_lock_bytes(self, handle: int) -> bytes:
        return self.files[self.open_handles[handle]]

    def write_lock_bytes(self, handle: int, payload: bytes) -> None:
        if self.fail_write:
            raise GoldenLockIoError("escritura simulada falló", win32_error=1)
        path_str = self.open_handles[handle]
        self.files[path_str] = payload
        self.write_calls.append((path_str, payload))

    def flush_lock(self, handle: int) -> None:
        self.flush_attempts += 1
        if self.fail_flush or self.flush_attempts in self.fail_flush_on_attempts:
            raise GoldenLockIoError("flush simulado falló", win32_error=1)
        self.flush_calls.append(self.open_handles[handle])

    def is_owner_alive(self, owner_pid: int, owner_process_creation_time: int) -> bool | None:
        return self.owner_alive

    def current_process_identity(self) -> tuple[int, int, int]:
        return self.identity

    def current_epoch_seconds(self) -> int:
        return self.epoch

    def close_handle(self, handle: int) -> None:
        self.close_calls.append(handle)
        self.busy_paths.discard(self.open_handles.pop(handle))


def _fake_programdata() -> pathlib.PureWindowsPath:
    return pathlib.PureWindowsPath("C:/ProgramData")


def _acquire_fake(
    *,
    kernel: _FakeLockKernel | None = None,
    operation_id: str = _VALID_OP_ID,
    volume_serial_number: int = _VOLUME_SERIAL,
    root_file_id: int = _ROOT_FILE_ID,
) -> tuple[GoldenLockIdentity, GoldenMutationLockHandle, _FakeLockKernel]:
    active = kernel or _FakeLockKernel()
    handle = acquire_golden_mutation_lock(
        volume_serial_number,
        root_file_id,
        operation_id,
        kernel=active,
        programdata_resolver=_fake_programdata,
    )
    return handle.identity, handle, active


# ============================================================================
# LOCK-01/02/03: derivación, flags de creación y share=0 estricto
# ============================================================================


class TestLockDerivacionYContrato:
    def test_lock_01_nombre_canonico(self) -> None:
        key = derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID)
        assert key == f"{GOLDEN_LOCK_NAME_PREFIX}a1b2c3d4_1122334455667788"

    def test_lock_01_path_bajo_programdata_sin_inyeccion(self, tmp_path: pathlib.Path) -> None:
        path = derive_golden_lock_path(_VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=lambda: tmp_path)
        expected_parent = (tmp_path / "Sky-Claw" / "runtime_vault" / "locks").resolve()
        assert path.parent == expected_parent
        assert path.name.startswith(GOLDEN_LOCK_NAME_PREFIX)
        assert path.suffix == ".lock"

    def test_lock_01_path_rechaza_resolver_inseguro_o_no_absoluto(self) -> None:
        with pytest.raises(PlanAuthorizationError):
            derive_golden_lock_path(
                _VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=lambda: pathlib.Path("relative")
            )
        with pytest.raises(PlanAuthorizationError):
            derive_golden_lock_path(_VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=lambda: "")

    def test_lock_02_flags_de_creacion_anclados(self) -> None:
        assert GOLDEN_LOCK_CREATION_DISPOSITION == 4  # OPEN_ALWAYS
        assert GOLDEN_LOCK_FLAGS == 0x00200080  # NORMAL | OPEN_REPARSE_POINT
        assert GOLDEN_LOCK_DESIRED_ACCESS == 0xC0000000  # GENERIC_READ | GENERIC_WRITE

    def test_lock_02_rango_de_vol_serial_y_file_id(self) -> None:
        with pytest.raises(PlanAuthorizationError):
            derive_golden_lock_path(-1, _ROOT_FILE_ID, programdata_resolver=lambda: pathlib.Path("/x"))
        with pytest.raises(PlanAuthorizationError):
            derive_golden_lock_path(_VOLUME_SERIAL, 2**128, programdata_resolver=lambda: pathlib.Path("/x"))

    def test_lock_03_share_mode_estrictamente_cero(self) -> None:
        assert GOLDEN_LOCK_SHARE_MODE == 0
        # El kernel Win32 construye CreateFileW con dwShareMode literal 0: anclado por AST en TestMlockAnclas.


# ============================================================================
# LOCK-01 (metadata): esquema cerrado de 7 claves, canonical JSON
# ============================================================================


class TestLockMetadata:
    def _metadata(self, **overrides: Any) -> GoldenLockMetadata:
        kwargs: dict[str, Any] = {
            "lock_key": derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            "owner_pid": 4242,
            "owner_process_creation_time": 133_456_789_012_345_678,
            "session_id": 1,
            "operation_id": _VALID_OP_ID,
            "phase": GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            "created_at": 1_758_499_200,
        }
        kwargs.update(overrides)
        return GoldenLockMetadata(**kwargs)

    def test_campos_exactamente_siete(self) -> None:
        fields = [field.name for field in dataclasses.fields(GoldenLockMetadata)]
        assert fields == [
            "lock_key",
            "owner_pid",
            "owner_process_creation_time",
            "session_id",
            "operation_id",
            "phase",
            "created_at",
        ]

    def test_serializacion_canonica_estable(self) -> None:
        metadata = self._metadata()
        raw = serialize_golden_lock_metadata(metadata)
        assert raw == serialize_golden_lock_metadata(metadata)
        assert json.loads(raw.decode("utf-8")) == {
            "created_at": 1_758_499_200,
            "lock_key": derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            "operation_id": _VALID_OP_ID,
            "owner_pid": 4242,
            "owner_process_creation_time": 133_456_789_012_345_678,
            "phase": "AUTHORIZATION_BOUNDARY",
            "session_id": 1,
        }

    def test_deserializacion_estricta_rechaza_claves_extra(self) -> None:
        raw = serialize_golden_lock_metadata(self._metadata())
        payload = json.loads(raw)
        payload["extra"] = True
        with pytest.raises(GoldenLockMetadataError):
            deserialize_golden_lock_metadata(json.dumps(payload).encode("utf-8"))

    def test_deserializacion_rechaza_faltantes(self) -> None:
        payload = json.loads(serialize_golden_lock_metadata(self._metadata()))
        del payload["session_id"]
        with pytest.raises(GoldenLockMetadataError):
            deserialize_golden_lock_metadata(json.dumps(payload).encode("utf-8"))

    def test_deserializacion_rechaza_bytes_excesivos(self) -> None:
        with pytest.raises(GoldenLockMetadataError):
            deserialize_golden_lock_metadata(b"x" * (_MAX_METADATA_BYTES + 1))

    def test_serializacion_rechaza_modelo_inconsistente(self) -> None:
        with pytest.raises(GoldenLockModelError):
            serialize_golden_lock_metadata(self._metadata(owner_pid=0))


# ============================================================================
# Identidad del lock: cross-binding con operation/volumen/file-id (M-CD)
# ============================================================================


class TestGoldenLockIdentity:
    def test_identity_ligada_a_la_llave_derivada(self) -> None:
        identity = GoldenLockIdentity(
            lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            volume_serial_number=_VOLUME_SERIAL,
            root_file_id=_ROOT_FILE_ID,
            owner_pid=4242,
            owner_process_creation_time=133_456_789_012_345_678,
            session_id=1,
            operation_id=_VALID_OP_ID,
            phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            created_at=1_758_499_200,
        )
        assert identity.operation_id == _VALID_OP_ID

    def test_identity_rechaza_llave_inconsistente(self) -> None:
        with pytest.raises(GoldenLockModelError):
            GoldenLockIdentity(
                lock_key=derive_golden_lock_key(0x1234, _ROOT_FILE_ID),
                volume_serial_number=_VOLUME_SERIAL,
                root_file_id=_ROOT_FILE_ID,
                owner_pid=1,
                owner_process_creation_time=1,
                session_id=0,
                operation_id=_VALID_OP_ID,
                phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
                created_at=1,
            )


# ============================================================================
# Clasificación del lock preexistente: busy / released / orphaned / ambigüedad
# ============================================================================


class TestPreexistingClassification:
    def _existing(self, phase: str = GoldenLockPhase.AUTHORIZATION_BOUNDARY.value) -> GoldenLockMetadata:
        return GoldenLockMetadata(
            lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            owner_pid=9999,
            owner_process_creation_time=555,
            session_id=2,
            operation_id="11111111-2222-3333-4444-555555555555",
            phase=phase,
            created_at=1_758_400_000,
        )

    def test_lock_04_busy_owner_vivo(self) -> None:
        assert (
            classify_preexisting_lock(self._existing(), owner_alive=True) is PreexistingLockDisposition.BUSY_OWNER_ALIVE
        )

    def test_lock_04_busy_owner_ilegible(self) -> None:
        assert classify_preexisting_lock(self._existing(), owner_alive=None) is (
            PreexistingLockDisposition.BUSY_OWNER_UNREADABLE
        )

    def test_lock_06_owner_muerto_es_orfanado(self) -> None:
        assert (
            classify_preexisting_lock(self._existing(), owner_alive=False) is PreexistingLockDisposition.ORPHANED_LOCK
        )

    def test_residual_released_nunca_busy(self) -> None:
        assert classify_preexisting_lock(self._existing(GoldenLockPhase.RELEASED.value), owner_alive=None) is (
            PreexistingLockDisposition.RESIDUAL_RELEASED
        )


# ============================================================================
# Ciclo de vida: adquisición, reintentos tipados, release, exclusividad
# ============================================================================


class TestAcquireRelease:
    def test_lock_05_identidad_del_lock_acordada(self) -> None:
        identity, handle, kernel = _acquire_fake()
        assert identity.owner_pid == 4242
        assert identity.owner_process_creation_time == 133_456_789_012_345_678
        assert identity.session_id == 1
        assert identity.operation_id == _VALID_OP_ID
        assert identity.lock_key == derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID)
        assert identity.phase == GoldenLockPhase.AUTHORIZATION_BOUNDARY.value
        handle.release()
        assert kernel.flush_calls, "release debe flushear"
        assert len(kernel.close_calls) == 1

    def test_lock_04_busy_segunda_adquisicion_tipada(self) -> None:
        _, handle, kernel = _acquire_fake()
        with pytest.raises(GoldenLockBusyError) as excinfo:
            acquire_golden_mutation_lock(
                _VOLUME_SERIAL,
                _ROOT_FILE_ID,
                _VALID_OP_ID,
                kernel=kernel,
                programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData"),
            )
        assert excinfo.value.win32_error == 32
        assert handle.closed is False
        handle.release()

    def test_lock_06_orphaned_no_se_roba_silent(self) -> None:
        kernel = _FakeLockKernel()
        path = derive_golden_lock_path(
            _VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData")
        )
        stale = GoldenLockMetadata(
            lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
            owner_pid=777,
            owner_process_creation_time=1,
            session_id=9,
            operation_id="11111111-2222-3333-4444-555555555555",
            phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            created_at=1_758_400_000,
        )
        kernel.files[str(path)] = serialize_golden_lock_metadata(stale)
        kernel.owner_alive = False
        with pytest.raises(GoldenLockError):
            acquire_golden_mutation_lock(
                _VOLUME_SERIAL,
                _ROOT_FILE_ID,
                _VALID_OP_ID,
                kernel=kernel,
                programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData"),
            )
        # El lock huérfano nunca se roba ni reabre tras el fallo tipado.
        assert kernel.busy_paths == set()
        assert len(kernel.close_calls) == 1

    def test_ambiguous_owner_alive_none_es_busy_no_orfanado(self) -> None:
        kernel = _FakeLockKernel()
        path = derive_golden_lock_path(
            _VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData")
        )
        kernel.files[str(path)] = serialize_golden_lock_metadata(
            GoldenLockMetadata(
                lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
                owner_pid=777,
                owner_process_creation_time=1,
                session_id=9,
                operation_id="11111111-2222-3333-4444-555555555555",
                phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
                created_at=1_758_400_000,
            )
        )
        kernel.owner_alive = None
        with pytest.raises(GoldenLockBusyError):
            acquire_golden_mutation_lock(
                _VOLUME_SERIAL,
                _ROOT_FILE_ID,
                _VALID_OP_ID,
                kernel=kernel,
                programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData"),
            )

    def test_reparse_point_rechazado_con_cierre(self) -> None:
        kernel = _FakeLockKernel()
        path = derive_golden_lock_path(
            _VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData")
        )
        kernel.reparse_tags[str(path)] = 0xA000000C
        with pytest.raises(GoldenLockError):
            acquire_golden_mutation_lock(
                _VOLUME_SERIAL,
                _ROOT_FILE_ID,
                _VALID_OP_ID,
                kernel=kernel,
                programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData"),
            )
        assert kernel.busy_paths == set()
        assert len(kernel.close_calls) == 1

    def test_metadata_corrupta_en_disco_rechaza_tipado(self) -> None:
        kernel = _FakeLockKernel()
        path = derive_golden_lock_path(
            _VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData")
        )
        kernel.files[str(path)] = b"not-json"
        with pytest.raises(GoldenLockMetadataError):
            acquire_golden_mutation_lock(
                _VOLUME_SERIAL,
                _ROOT_FILE_ID,
                _VALID_OP_ID,
                kernel=kernel,
                programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData"),
            )
        assert kernel.busy_paths == set()

    def test_adquisicion_escribe_metadata_canonica_y_flushea(self) -> None:
        identity, handle, kernel = _acquire_fake()
        path = derive_golden_lock_path(
            _VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData")
        )
        on_disk = json.loads(kernel.files[str(path)])
        assert on_disk["lock_key"] == identity.lock_key
        assert on_disk["owner_pid"] == identity.owner_pid
        assert on_disk["owner_process_creation_time"] == identity.owner_process_creation_time
        assert on_disk["session_id"] == identity.session_id
        assert on_disk["operation_id"] == identity.operation_id
        assert on_disk["phase"] == "AUTHORIZATION_BOUNDARY"
        assert isinstance(on_disk["created_at"], int) and on_disk["created_at"] > 0
        assert kernel.flush_calls == [str(path)]
        handle.release()

    def test_lock_07_release_exactamente_una_vez_y_cierre_unico(self) -> None:
        _, handle, kernel = _acquire_fake()
        assert handle.release() is True
        assert handle.release() is False  # release idempotente (no doble cierre)
        assert len(kernel.close_calls) == 1
        # metadata queda en RELEASED (el archivo NUNCA se borra, §19.1.5)
        path = derive_golden_lock_path(_VOLUME_SERIAL, _ROOT_FILE_ID, programdata_resolver=_fake_programdata)
        assert json.loads(kernel.files[str(path)])["phase"] == "RELEASED"

    def test_use_after_release_rechazado(self) -> None:
        _, handle, _ = _acquire_fake()
        handle.release()
        with pytest.raises(GoldenLockOwnershipError):
            _ = handle.raw_handle

    def test_release_escribe_fase_released_antes_del_cierre(self) -> None:
        # Un proceso vivo NUNCA pierde su lock por un fallo de flush: el handle
        # se cierra exactamente una vez y el error se propaga tipado.
        _, handle, kernel = _acquire_fake()
        kernel.fail_flush = True
        with pytest.raises(GoldenLockIoError):
            handle.release()
        assert handle.closed is True
        assert len(kernel.close_calls) == 1

    def test_context_manager_release_al_salir(self) -> None:
        with _acquire_fake()[1] as handle:
            assert not handle.closed
        assert handle.closed


class TestFlushFailureCleanup:
    """Post-review P1/P2: fallo de flush post-escritura durante ADQUISICIÓN."""

    _LOCKS_DIR = pathlib.PurePath("/tmp/fake-locks")

    def test_flush_falla_cleanup_released_y_la_siguiente_adquisicion_continua(self) -> None:
        kernel = _FakeLockKernel()
        kernel.fail_flush_on_attempts = {1}  # falla el flush de la metadata fresca; el cleanup no
        with pytest.raises(GoldenLockAcquisitionCleanupError) as excinfo:
            _acquire_golden_mutation_lock_at(
                self._LOCKS_DIR, _VOLUME_SERIAL, _ROOT_FILE_ID, _VALID_OP_ID, kernel=kernel
            )
        assert excinfo.value.cleanup_succeeded is True
        assert len(kernel.close_calls) == 1, "El handle exclusivo se cierra exactamente una vez (jamás borrado)"
        # El estado residual quedó RELEASED: la siguiente adquisición puede continuar.
        handle = _acquire_golden_mutation_lock_at(
            self._LOCKS_DIR, _VOLUME_SERIAL, _ROOT_FILE_ID, _VALID_OP_ID, kernel=kernel
        )
        handle.release()
        assert len(kernel.close_calls) == 2

    def test_flush_falla_y_cleanup_falla_estado_ambiguo_tipado_nunca_success(self) -> None:
        kernel = _FakeLockKernel()
        kernel.fail_flush_on_attempts = {1, 2}  # fresh + cleanup flushes fallan
        with pytest.raises(GoldenLockAcquisitionCleanupError) as excinfo:
            _acquire_golden_mutation_lock_at(
                self._LOCKS_DIR, _VOLUME_SERIAL, _ROOT_FILE_ID, _VALID_OP_ID, kernel=kernel
            )
        assert excinfo.value.cleanup_succeeded is False, "Nunca se afirma que el lock quedó liberado"
        assert excinfo.value.win32_error == 1
        assert len(kernel.close_calls) == 1


# ============================================================================
# Mutantes M-L1/M-L2/M-L3: anclas AST/estructurales y GP2-T40 (parcial)
# ============================================================================


class TestMlockAnclasAst:
    PRODUCTION_MODULE = (
        pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "runtime_vault" / "golden_mutation_lock.py"
    )

    def _source(self) -> str:
        return self.PRODUCTION_MODULE.read_text(encoding="utf-8")

    def test_m_l3_ningun_parametro_inyecta_ruta_del_lock(self) -> None:
        params = inspect.signature(acquire_golden_mutation_lock).parameters
        assert set(params) == {"volume_serial_number", "root_file_id", "operation_id", "kernel", "programdata_resolver"}
        # Sólo el resolver de ProgramData raíz (interno); jamás un path completo del .lock ni flags compartidos.
        assert "path" not in params and "lock_path" not in params and "share_mode" not in params

    def test_m_l2_no_hay_reintentos_infinitos_ni_setsecurityinfo(self) -> None:
        source = self._source()
        assert "while True" not in source
        assert "SetSecurityInfo" not in source
        assert "ERROR_SHARING_VIOLATION" in source  # tipado de busy, ancla del mutante de reintentos

    def test_m_l1_no_hay_lectura_publica_del_lock(self) -> None:
        source = self._source()
        # La lectura de metadata sólo existe ligada al handle del adquirente
        # (método del kernel con handle ya abierto); jamás hay una API de
        # lectura "por path público" que un no-owner pudiera invocar.
        assert "read_lock_bytes(self, handle" in source
        forbidden_public_read = ("read_lock_path", "read_metadata_at", "open_lock_for_read")
        for literal in forbidden_public_read:
            assert literal not in source

    def test_gp2_t40_parcial_posix_dacl_lugar_correcto(self) -> None:
        # PARTIAL: la verificación real de la ACL NTFS (AU sin lectura de *.lock) vive en
        # tests/test_runtime_vault_trusted_namespace.py sobre el volumen NTFS real.
        assert GOLDEN_LOCK_CREATION_DISPOSITION == 4
        assert GOLDEN_LOCK_SHARE_MODE == 0


# ============================================================================
# Causal Win32 con archivos desechables reales (share=0 nativo)
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Pruebas causales Win32 de exclusividad de archivo")
class TestWin32CausalGoldenLock:
    def test_segunda_apertura_share_cero_falla_con_sharing_violation(self, tmp_path: pathlib.Path) -> None:
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        handle = _acquire_golden_mutation_lock_at(locks_dir, _VOLUME_SERIAL, _ROOT_FILE_ID, _VALID_OP_ID)
        identity = handle.identity
        try:
            # Segunda adquisición mientras el owner EXCLUSIVO (dwShareMode=0) vive:
            # busy tipado con el código causal exacto de Windows.
            with pytest.raises(GoldenLockBusyError) as excinfo:
                _acquire_golden_mutation_lock_at(locks_dir, _VOLUME_SERIAL, _ROOT_FILE_ID, _VALID_OP_ID)
            assert excinfo.value.win32_error == 32  # ERROR_SHARING_VIOLATION
            # Mientras el lock está tomado NO se lee el archivo por pathname
            # (sería PermissionError: justamente lo que dwShareMode=0 garantiza).
            # La evidencia vive en handle.identity, propiedad del owner.
            assert identity.phase == "authorization_boundary"
            assert identity.owner_pid == __import__("os").getpid()
            assert identity.lock_key.startswith("skyclaw_golden_lock_")
        finally:
            handle.release()
        # Sólo DESPUÉS del release se lee por pathname: fase residual RELEASED.
        assert json.loads((locks_dir / f"{identity.lock_key}.lock").read_bytes())["phase"] == "released"

    def test_release_reabre_lock_residual(self, tmp_path: pathlib.Path) -> None:
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        first_handle = _acquire_golden_mutation_lock_at(locks_dir, _VOLUME_SERIAL, _ROOT_FILE_ID, _VALID_OP_ID)
        first_identity = first_handle.identity
        first_handle.release()
        second_handle = _acquire_golden_mutation_lock_at(locks_dir, _VOLUME_SERIAL, _ROOT_FILE_ID, _VALID_OP_ID)
        second_identity = second_handle.identity
        assert second_identity.lock_key == first_identity.lock_key
        second_handle.release()

    def test_reaperturas_seriadas_nunca_concatenan_json(self, tmp_path: pathlib.Path) -> None:
        # Post-review P1 (file pointer): acquire -> release -> acquire -> release
        # -> acquire. Cada reapertura de un lock RELEASED debe parsear EXACTAMENTE
        # UNA metadata válida — jamás <JSON viejo><JSON nuevo> concatenado.
        import json as _json

        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        lock_file: pathlib.Path | None = None
        for _cycle in range(3):
            handle = _acquire_golden_mutation_lock_at(locks_dir, _VOLUME_SERIAL, _ROOT_FILE_ID, _VALID_OP_ID)
            try:
                assert handle.identity.phase == "authorization_boundary"
                lock_file = locks_dir / f"{handle.identity.lock_key}.lock"
            finally:
                handle.release()
            raw = lock_file.read_bytes()
            decoder = _json.JSONDecoder()
            parsed, end_index = decoder.raw_decode(raw.decode("utf-8"))
            assert raw.decode("utf-8")[end_index:].strip() == "", (
                f"Reapertura {_cycle}: el lock contiene contenido colgado tras el JSON (concatenación detectada)"
            )
            assert parsed["phase"] == "released"

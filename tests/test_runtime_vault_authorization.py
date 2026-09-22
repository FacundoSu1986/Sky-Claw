"""Tests focales GP2-S3b-1: contexto de autorización privilegiada (H) + PPSC (F).

Cobertura: AUTH-01..AUTH-05, PPSC-01..PPSC-04, mutantes M-U1/M-U2/M-A1, orden
canónico de establecimiento, ciclo de vida de la sesión (cierre exacto) y la
fundación §13 (header de plan autorizado NO durable en memoria).

Todo corre con seams deterministas: probe provider fake, proveedor PPSC fake,
acquirers de token/lock fakes. NINGÚN test de este archivo requiere privilegios
reales ni muta el Golden; las clases causales Win32 viven en los archivos de
frontera/token/lock.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import pathlib
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorization_context import (
    AUTHORIZATION_ESTABLISHMENT_ORDER,
    AUTHORIZED_PLAN_FOUNDATION_STATUS,
    FOUNDATION_HEADER_KEYS,
    PURE_INPUT_BINDING_VALIDATION,
    AuthorizationCleanupError,
    AuthorizationContextModelError,
    AuthorizationSessionError,
    AuthorizedPlanFoundationHeader,
    PrivilegedAuthorizationContext,
    PrivilegedBoundarySession,
    build_foundation_header_from_context,
    compute_foundation_plan_digest,
    establish_privileged_authorization,
    serialize_authorized_plan_foundation_header,
)
from sky_claw.local.runtime_vault.coordinator_identity import (
    CoordinatorIdentityBindingError,
    CoordinatorProcessIdentity,
    ProcessIdentityProbe,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockBusyError,
    GoldenLockIdentity,
    GoldenLockModelError,
    GoldenLockPhase,
    acquire_golden_mutation_lock,
    derive_golden_lock_key,
)
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.operator_token import (
    OpenCoordinatorProcessError,
    OperatorPrimaryToken,
    OtsElevationCase,
    acquire_operator_primary_token_from_coordinator,
)
from sky_claw.local.runtime_vault.ppsc import (
    PPSC_NORMATIVE_FIELDS,
    PrivilegedPlanConfirmation,
    PrivilegedPlanConfirmationModelError,
    PrivilegedPlanConfirmationReceipt,
    PrivilegedPlanConfirmationResult,
    require_ppsc_outcome,
)
from sky_claw.local.runtime_vault.privileged_boundary import (
    PlanAuthorizationError,
    PrivilegedHelperLaunchRequest,
)

_VALID_UUID = "3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a"
_ALT_UUID = "00f93602-bd14-4a95-80e1-a4f9689a3afb"
_STAGING_DIGEST = "a" * 64
_TREE_DIGEST = "b" * 64
_VOLUME_SERIAL = 0xA1B2C3D4
_ROOT_FILE_ID = 0x1122334455667788


def _launch_request(**overrides: Any) -> PrivilegedHelperLaunchRequest:
    kwargs: dict[str, Any] = {
        "operation_id": _VALID_UUID,
        "staging_digest": _STAGING_DIGEST,
        "coordinator_pid": 4242,
        "coordinator_creation_time": 133_456_789_012_345_678,
    }
    kwargs.update(overrides)
    return PrivilegedHelperLaunchRequest(**kwargs)


def _expected_coordinator() -> CoordinatorProcessIdentity:
    return CoordinatorProcessIdentity(
        pid=4242,
        creation_time=133_456_789_012_345_678,
        image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
    )


def _probe_provider(image_path: str = "C:\\Program Files\\Sky-Claw\\sky-claw.exe"):
    class _Provider:
        def probe(self, pid: int) -> ProcessIdentityProbe:
            return ProcessIdentityProbe(
                pid=pid,
                is_openable=True,
                creation_time=133_456_789_012_345_678,
                image_path=image_path,
            )

    return _Provider()


def _ppsc_payload(**overrides: Any) -> PrivilegedPlanConfirmation:
    kwargs: dict[str, Any] = {
        "operation_id": _VALID_UUID,
        "canonical_root": "C:\\Games\\Skyrim",
        "volume_serial_number": _VOLUME_SERIAL,
        "root_file_id": _ROOT_FILE_ID,
        "tree_digest": TreeDigest(digest=_TREE_DIGEST, files=3, bytes=10),
        "node_count": 7,
        "policy_version": "golden-policy-v1",
    }
    kwargs.update(overrides)
    return PrivilegedPlanConfirmation(**kwargs)


def _ppsc_provider(result: PrivilegedPlanConfirmationResult = PrivilegedPlanConfirmationResult.CONFIRMED):
    class _Provider:
        def __init__(self) -> None:
            self.calls: list[PrivilegedPlanConfirmation] = []

        def request_confirmation(self, payload: PrivilegedPlanConfirmation) -> PrivilegedPlanConfirmationResult:
            self.calls.append(payload)
            return result

    return _Provider()


# ---- Fakes de adquisición (sin kernels reales) ------------------------------


class _FakeTokenAdapter:
    def __init__(self) -> None:
        self.closed: list[int] = []

    def open_process(self, desired_access: int, pid: int) -> int:
        return 101

    def open_process_token(self, process_handle: int, desired_access: int) -> int:
        return 202

    def duplicate_token_ex_primary(self, source_handle: int, desired_access: int) -> int:
        return 303

    def get_token_type(self, token_handle: int) -> int:
        return 1

    def read_token_user_sid(self, token_handle: int) -> str:
        return "S-1-5-21-1001-1002-1003-1001"

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)


class _FakeLockKernel:
    """Kernel fake del lock: share=0 monoproceso, reparse tags, metadata en memoria."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.busy_paths: set[str] = set()
        self.open_handles: dict[int, str] = {}
        self.reparse_tags: dict[str, int] = {}
        self.owner_alive: bool | None = True
        self.close_calls: list[int] = []
        self._next_handle = 700

    def open_lock_file(self, path: pathlib.PurePath) -> int:
        path_str = str(path)
        if path_str in self.busy_paths:
            raise GoldenLockBusyError("ocupado", win32_error=32)
        handle = self._next_handle
        self._next_handle += 1
        self.open_handles[handle] = path_str
        self.busy_paths.add(path_str)
        self.files.setdefault(path_str, b"")
        return handle

    def get_reparse_tag(self, handle: int) -> int:
        return self.reparse_tags.get(self.open_handles[handle], 0)

    def read_lock_bytes(self, handle: int) -> bytes:
        return self.files[self.open_handles[handle]]

    def write_lock_bytes(self, handle: int, payload: bytes) -> None:
        self.files[self.open_handles[handle]] = payload

    def flush_lock(self, handle: int) -> None:
        pass

    def is_owner_alive(self, owner_pid: int, owner_creation_time: int) -> bool | None:
        return self.owner_alive

    def current_process_identity(self) -> tuple[int, int, int]:
        return (4242, 133_456_789_012_345_678, 1)

    def current_epoch_seconds(self) -> int:
        return 1_758_499_200

    def close_handle(self, handle: int) -> None:
        self.close_calls.append(handle)
        self.busy_paths.discard(self.open_handles.pop(handle))


def _fake_programdata() -> pathlib.PureWindowsPath:
    return pathlib.PureWindowsPath("C:/ProgramData")


def _make_token_via_fake_adapter() -> tuple[OperatorPrimaryToken, _FakeTokenAdapter]:
    adapter = _FakeTokenAdapter()
    token = acquire_operator_primary_token_from_coordinator(4242, adapter=adapter)
    return token, adapter


def _fake_lock_acquirer(kernel: _FakeLockKernel):
    def _acquire(volume_serial_number: int, root_file_id: int, operation_id: str):
        return acquire_golden_mutation_lock(
            volume_serial_number,
            root_file_id,
            operation_id,
            kernel=kernel,
            programdata_resolver=_fake_programdata,
        )

    return _acquire


# ============================================================================
# PPSC-01/02: modelo exacto de siete campos normativos
# ============================================================================


class TestPpscModel:
    def test_ppsc_01_campos_exactamente_siete_en_orden_normativo(self) -> None:
        fields = [field.name for field in dataclasses.fields(PrivilegedPlanConfirmation)]
        assert fields == list(PPSC_NORMATIVE_FIELDS)
        assert len(PPSC_NORMATIVE_FIELDS) == 7

    def test_ppsc_02_falta_un_campo_rechaza(self) -> None:
        with pytest.raises(TypeError):
            PrivilegedPlanConfirmation(  # type: ignore[call-arg]
                operation_id=_VALID_UUID,
                canonical_root="C:\\Games\\Skyrim",
                volume_serial_number=_VOLUME_SERIAL,
                root_file_id=_ROOT_FILE_ID,
                tree_digest=TreeDigest(digest=_TREE_DIGEST, files=1, bytes=1),
                node_count=7,
            )

    def test_ppsc_02_tipos_y_rangos_validados(self) -> None:
        with pytest.raises(PrivilegedPlanConfirmationModelError):
            _ppsc_payload(volume_serial_number=-1)
        with pytest.raises(PrivilegedPlanConfirmationModelError):
            _ppsc_payload(node_count=0)
        with pytest.raises(PrivilegedPlanConfirmationModelError):
            _ppsc_payload(operation_id=_VALID_UUID.upper())
        with pytest.raises(PrivilegedPlanConfirmationModelError):
            _ppsc_payload(policy_version="")
        with pytest.raises(PrivilegedPlanConfirmationModelError):
            _ppsc_payload(canonical_root="relative/path")

    def test_ppsc_payload_immutable(self) -> None:
        payload = _ppsc_payload()
        with pytest.raises(dataclasses.FrozenInstanceError):
            payload.node_count = 99  # type: ignore[misc]


# ============================================================================
# PPSC-03/04 y M-U1/M-A1: resultado estricto, sin defaults
# ============================================================================


class TestPpscOutcome:
    def test_ppsc_03_confirmed_explicito_requerido(self) -> None:
        payload = _ppsc_payload()
        provider = _ppsc_provider(PrivilegedPlanConfirmationResult.CONFIRMED)
        receipt = require_ppsc_outcome(provider, payload)
        assert receipt.result is PrivilegedPlanConfirmationResult.CONFIRMED
        assert len(provider.calls) == 1

    def test_ppsc_03_rejected_refuse_to_plan(self) -> None:
        with pytest.raises(PlanAuthorizationError):
            require_ppsc_outcome(_ppsc_provider(PrivilegedPlanConfirmationResult.REJECTED), _ppsc_payload())

    def test_ppsc_04_resultado_no_enum_rechazado(self) -> None:
        class _BadProvider:
            def request_confirmation(self, payload: PrivilegedPlanConfirmation) -> Any:
                return True  # bool jamás es PPSC

        with pytest.raises(PlanAuthorizationError):
            require_ppsc_outcome(_BadProvider(), _ppsc_payload())

    def test_ppsc_04_none_no_es_confirmacion(self) -> None:
        class _NoneProvider:
            def request_confirmation(self, payload: PrivilegedPlanConfirmation) -> Any:
                return None

        with pytest.raises(PlanAuthorizationError):
            require_ppsc_outcome(_NoneProvider(), _ppsc_payload())

    def test_m_u1_receipt_rejected_imposible_por_construccion(self) -> None:
        with pytest.raises(PlanAuthorizationError):
            PrivilegedPlanConfirmationReceipt(
                payload=_ppsc_payload(),
                result=PrivilegedPlanConfirmationResult.REJECTED,
            )

    def test_m_a1_ancla_establish_requiere_ppsc(self) -> None:
        params = inspect.signature(establish_privileged_authorization).parameters
        assert params["ppsc_provider"].default is inspect.Parameter.empty
        assert params["ppsc_payload"].default is inspect.Parameter.empty


# ============================================================================
# Contexto H: cross-binding explícito (M-CD1/M-CD2 a nivel contexto)
# ============================================================================


def _make_context_components() -> dict[str, Any]:
    token, _ = _make_token_via_fake_adapter()
    kernel = _FakeLockKernel()
    handle = acquire_golden_mutation_lock(
        _VOLUME_SERIAL,
        _ROOT_FILE_ID,
        _VALID_UUID,
        kernel=kernel,
        programdata_resolver=_fake_programdata,
    )
    return {
        "operation_id": _VALID_UUID,
        "staging_digest": _STAGING_DIGEST,
        "coordinator_identity": _expected_coordinator(),
        "operator_identity": token.evidence,
        "ppsc_confirmation": PrivilegedPlanConfirmationReceipt(
            payload=_ppsc_payload(), result=PrivilegedPlanConfirmationResult.CONFIRMED
        ),
        "lock_identity": handle.identity,
        "_token": token,
        "_handle": handle,
    }


class TestAuthorizationContextModel:
    def test_auth_04_campos_exactamente_seis(self) -> None:
        fields = [field.name for field in dataclasses.fields(PrivilegedAuthorizationContext)]
        assert fields == [
            "operation_id",
            "staging_digest",
            "coordinator_identity",
            "operator_identity",
            "ppsc_confirmation",
            "lock_identity",
        ]

    def test_contexto_feliz_con_evidence_solo(self) -> None:
        parts = _make_context_components()
        ctx = PrivilegedAuthorizationContext(
            operation_id=parts["operation_id"],
            staging_digest=parts["staging_digest"],
            coordinator_identity=parts["coordinator_identity"],
            operator_identity=parts["operator_identity"],
            ppsc_confirmation=parts["ppsc_confirmation"],
            lock_identity=parts["lock_identity"],
        )
        assert ctx.operation_id == _VALID_UUID
        # NUNCA handles crudos: ningún campo es un entero-handle opaco.
        assert not isinstance(ctx.operation_id, int)
        assert not isinstance(ctx.staging_digest, int)
        parts["_handle"].release()
        parts["_token"].close()

    def test_operation_id_discrepante_rechazado(self) -> None:
        parts = _make_context_components()
        try:
            with pytest.raises(AuthorizationContextModelError):
                PrivilegedAuthorizationContext(
                    operation_id=_ALT_UUID,
                    staging_digest=parts["staging_digest"],
                    coordinator_identity=parts["coordinator_identity"],
                    operator_identity=parts["operator_identity"],
                    ppsc_confirmation=parts["ppsc_confirmation"],
                    lock_identity=parts["lock_identity"],
                )
        finally:
            parts["_handle"].release()
            parts["_token"].close()

    def test_m_u1_ppsc_none_rechazado(self) -> None:
        parts = _make_context_components()
        try:
            with pytest.raises(AuthorizationContextModelError):
                PrivilegedAuthorizationContext(
                    operation_id=parts["operation_id"],
                    staging_digest=parts["staging_digest"],
                    coordinator_identity=parts["coordinator_identity"],
                    operator_identity=parts["operator_identity"],
                    ppsc_confirmation=None,  # type: ignore[arg-type]
                    lock_identity=parts["lock_identity"],
                )
        finally:
            parts["_handle"].release()
            parts["_token"].close()

    def test_m_u2_lock_identity_none_rechazado(self) -> None:
        parts = _make_context_components()
        try:
            with pytest.raises(AuthorizationContextModelError):
                PrivilegedAuthorizationContext(
                    operation_id=parts["operation_id"],
                    staging_digest=parts["staging_digest"],
                    coordinator_identity=parts["coordinator_identity"],
                    operator_identity=parts["operator_identity"],
                    ppsc_confirmation=parts["ppsc_confirmation"],
                    lock_identity=None,  # type: ignore[arg-type]
                )
        finally:
            parts["_handle"].release()
            parts["_token"].close()

    def test_volumen_discrepante_ppsc_vs_lock_rechazado(self) -> None:
        parts = _make_context_components()
        mismatched_receipt = PrivilegedPlanConfirmationReceipt(
            payload=_ppsc_payload(volume_serial_number=0xDEADBEEF),
            result=PrivilegedPlanConfirmationResult.CONFIRMED,
        )
        try:
            with pytest.raises(AuthorizationContextModelError):
                PrivilegedAuthorizationContext(
                    operation_id=parts["operation_id"],
                    staging_digest=parts["staging_digest"],
                    coordinator_identity=parts["coordinator_identity"],
                    operator_identity=parts["operator_identity"],
                    ppsc_confirmation=mismatched_receipt,
                    lock_identity=parts["lock_identity"],
                )
        finally:
            parts["_handle"].release()
            parts["_token"].close()

    def test_staging_digest_invalido_rechazado(self) -> None:
        parts = _make_context_components()
        try:
            with pytest.raises(AuthorizationContextModelError):
                PrivilegedAuthorizationContext(
                    operation_id=parts["operation_id"],
                    staging_digest="no-hex",
                    coordinator_identity=parts["coordinator_identity"],
                    operator_identity=parts["operator_identity"],
                    ppsc_confirmation=parts["ppsc_confirmation"],
                    lock_identity=parts["lock_identity"],
                )
        finally:
            parts["_handle"].release()
            parts["_token"].close()


# ============================================================================
# AUTH-01..03 + orden de establecimiento + ciclo de sesión
# ============================================================================


class TestEstablishmentOrchestration:
    def test_orden_canonico_declarado(self) -> None:
        assert AUTHORIZATION_ESTABLISHMENT_ORDER == (
            "coordinator_identity_binding",
            "operator_token_strategy",
            "operator_token_acquisition",
            "ppsc_confirmation",
            "golden_mutation_lock",
        )

    def test_pure_input_binding_validation_precede_sin_efectos_laterales(self) -> None:
        # Finding AUTHORIZATION_ESTABLISHMENT_ORDER resuelto: la validación
        # estructural pura (PURE_INPUT_BINDING_VALIDATION) precede a los cinco
        # stages de autoridad; ante mismatch NO abre proceso, NO adquiere token,
        # NO llama a la PPSC y NO toma el lock. Tampoco es un sexto stage/estado.
        assert PURE_INPUT_BINDING_VALIDATION.startswith("pure_input_binding_validation")
        assert "precedes AUTHORIZATION_ESTABLISHMENT_ORDER" in PURE_INPUT_BINDING_VALIDATION
        side_effects: list[str] = []

        class _SpyingProbe:
            def probe(self, pid: int) -> ProcessIdentityProbe | None:
                side_effects.append("identity_probe")
                return None

        class _SpyingPpsc:
            def request_confirmation(self, payload: PrivilegedPlanConfirmation) -> PrivilegedPlanConfirmationResult:
                side_effects.append("ppsc")
                return PrivilegedPlanConfirmationResult.REJECTED

        def spying_token_acquirer(pid: int) -> OperatorPrimaryToken:
            side_effects.append("token")
            raise PlanAuthorizationError("nunca debería invocarse")

        def spying_lock_acquirer(vol: int, fid: int, op: str) -> object:
            side_effects.append("lock")
            raise PlanAuthorizationError("nunca debería invocarse")

        for refusal_kwargs in (
            {"volume_serial_number": 9_999_999_999},  # mismatch vía argumentos
            {"ppsc_payload": _ppsc_payload(operation_id=_ALT_UUID)},  # mismatch vía payload
        ):
            with pytest.raises(PlanAuthorizationError):
                self._establish(
                    coordinator_probe_provider=_SpyingProbe(),
                    token_acquirer=spying_token_acquirer,
                    ppsc_provider=_SpyingPpsc(),
                    lock_acquirer=spying_lock_acquirer,
                    **refusal_kwargs,
                )
        assert side_effects == [], "mismatch de entrada: cero efectos privilegiados"

    def _establish(self, **overrides: Any) -> tuple[PrivilegedAuthorizationContext, PrivilegedBoundarySession]:
        kwargs: dict[str, Any] = {
            "launch_request": _launch_request(),
            "expected_coordinator": _expected_coordinator(),
            "coordinator_probe_provider": _probe_provider(),
            "elevation_case": OtsElevationCase.SAME_ACCOUNT,
            "ppsc_provider": _ppsc_provider(),
            "ppsc_payload": _ppsc_payload(),
            "volume_serial_number": _VOLUME_SERIAL,
            "root_file_id": _ROOT_FILE_ID,
        }
        kwargs.update(overrides)
        return establish_privileged_authorization(**kwargs)

    def test_auth_establecimiento_feliz_y_orden_real_de_llamadas(self) -> None:
        order: list[str] = []
        token_adapter = _FakeTokenAdapter()
        kernel = _FakeLockKernel()

        def token_acquirer(pid: int) -> OperatorPrimaryToken:
            order.append("token")
            return acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter)

        class _OrderedPpsc:
            def request_confirmation(self, payload: PrivilegedPlanConfirmation) -> PrivilegedPlanConfirmationResult:
                order.append("ppsc")
                return PrivilegedPlanConfirmationResult.CONFIRMED

        def lock_acquirer(vol: int, fid: int, op: str):
            order.append("lock")
            return acquire_golden_mutation_lock(vol, fid, op, kernel=kernel, programdata_resolver=_fake_programdata)

        ctx, session = self._establish(
            token_acquirer=token_acquirer, ppsc_provider=_OrderedPpsc(), lock_acquirer=lock_acquirer
        )
        assert order == ["token", "ppsc", "lock"]
        assert ctx.operator_identity.operator_sid.startswith("S-1-")
        assert ctx.lock_identity.operation_id == ctx.operation_id
        assert session.close() is True
        # Cierre exacto: lock y token, una sola vez cada uno.
        assert len(kernel.close_calls) == 1
        assert token_adapter.closed.count(303) == 1
        assert session.close() is False

    def test_auth_01_token_no_adquirible_detiene_todo(self) -> None:
        provider = _ppsc_provider()
        kernel = _FakeLockKernel()

        def failing_acquirer(pid: int) -> OperatorPrimaryToken:
            raise OpenCoordinatorProcessError("denegado")

        with pytest.raises(OpenCoordinatorProcessError):
            self._establish(
                token_acquirer=failing_acquirer,
                ppsc_provider=provider,
                lock_acquirer=_fake_lock_acquirer(kernel),
            )
        assert provider.calls == [], "PPSC nunca corre si el token no existe"
        assert kernel.busy_paths == set(), "Lock nunca se intenta"

    def test_ppsc_rechazado_cierra_token_antes_del_lock(self) -> None:
        token_adapter = _FakeTokenAdapter()
        kernel = _FakeLockKernel()

        with pytest.raises(PlanAuthorizationError):
            self._establish(
                token_acquirer=lambda pid: acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter),
                ppsc_provider=_ppsc_provider(PrivilegedPlanConfirmationResult.REJECTED),
                lock_acquirer=_fake_lock_acquirer(kernel),
            )
        assert kernel.busy_paths == set()
        assert token_adapter.closed.count(303) == 1, "el token adquirido debe cerrarse exactamente una vez"

    def test_auth_02_lock_ocupado_refuse_to_plan_con_token_cerrado(self) -> None:
        token_adapter = _FakeTokenAdapter()
        kernel = _FakeLockKernel()
        # Ocupo el lock antes: la adquisición posterior debe fallar con busy tipado.
        blocking_handle = acquire_golden_mutation_lock(
            _VOLUME_SERIAL,
            _ROOT_FILE_ID,
            _ALT_UUID,
            kernel=kernel,
            programdata_resolver=_fake_programdata,
        )
        with pytest.raises(GoldenLockBusyError):
            self._establish(
                token_acquirer=lambda pid: acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter),
                lock_acquirer=_fake_lock_acquirer(kernel),
            )
        assert token_adapter.closed.count(303) == 1
        blocking_handle.release()

    def test_contexto_falla_post_lock_libera_lock_y_token_exactamente_una_vez(self) -> None:
        # Post-review P1 (resource ownership): lock adquirido → el binding del
        # contexto falla (operation_id del lock no coincide) → lock.release() y
        # token.close() EXACTAMENTE una vez cada uno.
        releases: list[int] = []
        token_adapter = _FakeTokenAdapter()

        class _DuckLock:
            def __init__(self) -> None:
                self.closed = False
                self.identity = GoldenLockIdentity(
                    lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
                    volume_serial_number=_VOLUME_SERIAL,
                    root_file_id=_ROOT_FILE_ID,
                    owner_pid=4242,
                    owner_process_creation_time=133_456_789_012_345_678,
                    session_id=1,
                    operation_id=_ALT_UUID,  # mismatch deliberado: rompe el binding del contexto
                    phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
                    created_at=1_700_000_000,
                )

            def release(self) -> bool:
                releases.append(1)
                self.closed = True
                return True

        with pytest.raises(AuthorizationContextModelError):
            self._establish(
                token_acquirer=lambda pid: acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter),
                lock_acquirer=lambda vol, fid, op: _DuckLock(),
            )
        assert releases == [1], "el lock adquirido se libera exactamente una vez pese al fallo del contexto"
        assert token_adapter.closed.count(303) == 1, "el token adquirido se cierra exactamente una vez"

    def test_release_de_lock_falla_token_igual_cierra_y_causalidad_tipada(self) -> None:
        # Variante: lock.release() FALLA → el token igualmente debe cerrarse y
        # la causalidad de ambos errores se conserva tipada (AuthorizationCleanupError).
        release_exc = GoldenLockModelError("release simulado falló")
        token_adapter = _FakeTokenAdapter()

        class _FailingReleaseLock:
            def __init__(self) -> None:
                self.closed = False
                self.identity = GoldenLockIdentity(
                    lock_key=derive_golden_lock_key(_VOLUME_SERIAL, _ROOT_FILE_ID),
                    volume_serial_number=_VOLUME_SERIAL,
                    root_file_id=_ROOT_FILE_ID,
                    owner_pid=4242,
                    owner_process_creation_time=133_456_789_012_345_678,
                    session_id=1,
                    operation_id=_ALT_UUID,
                    phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
                    created_at=1_700_000_000,
                )

            def release(self) -> bool:
                raise release_exc

        with pytest.raises(AuthorizationCleanupError) as excinfo:
            self._establish(
                token_acquirer=lambda pid: acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter),
                lock_acquirer=lambda vol, fid, op: _FailingReleaseLock(),
            )
        assert token_adapter.closed.count(303) == 1, "un fallo de cleanup del lock nunca impide cerrar el token"
        assert excinfo.value.lock_cleanup_error is release_exc
        assert excinfo.value.token_cleanup_error is None
        assert isinstance(excinfo.value.__cause__, AuthorizationContextModelError), "el error original nunca se esconde"

    def test_auth_03_ppsc_payload_incoherente_con_lock_args_rechaza_antes_de_todo(self) -> None:
        token_adapter = _FakeTokenAdapter()
        kernel = _FakeLockKernel()
        with pytest.raises(PlanAuthorizationError):
            self._establish(
                token_acquirer=lambda pid: acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter),
                ppsc_payload=_ppsc_payload(volume_serial_number=0xDEADBEEF),
                lock_acquirer=_fake_lock_acquirer(kernel),
            )
        assert token_adapter.closed == []
        assert kernel.busy_paths == set()

    def test_operation_id_discrepante_request_vs_ppsc_rechaza_antes_de_todo(self) -> None:
        token_adapter = _FakeTokenAdapter()
        kernel = _FakeLockKernel()
        with pytest.raises(PlanAuthorizationError):
            self._establish(
                token_acquirer=lambda pid: acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter),
                ppsc_payload=_ppsc_payload(operation_id=_ALT_UUID),
                lock_acquirer=_fake_lock_acquirer(kernel),
            )
        assert token_adapter.closed == [] and kernel.busy_paths == set()

    def test_m_pid_binding_fallo_de_identidad_refuse_antes_de_token(self) -> None:
        provider = _ppsc_provider()
        kernel = _FakeLockKernel()
        token_adapter = _FakeTokenAdapter()
        with pytest.raises(CoordinatorIdentityBindingError):
            self._establish(
                coordinator_probe_provider=_probe_provider(image_path="C:\\Windows\\System32\\cmd.exe"),
                token_acquirer=lambda pid: acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter),
                ppsc_provider=provider,
                lock_acquirer=_fake_lock_acquirer(kernel),
            )
        assert provider.calls == []
        assert token_adapter.closed == []
        assert kernel.busy_paths == set()

    def test_cross_account_sin_proveedor_refuse_antes_de_token(self) -> None:
        token_adapter = _FakeTokenAdapter()
        kernel = _FakeLockKernel()
        with pytest.raises(PlanAuthorizationError):
            self._establish(
                elevation_case=OtsElevationCase.CROSS_ACCOUNT,
                token_acquirer=lambda pid: acquire_operator_primary_token_from_coordinator(pid, adapter=token_adapter),
                lock_acquirer=_fake_lock_acquirer(kernel),
            )
        assert token_adapter.closed == [] and kernel.busy_paths == set()


class TestSessionLifecycle:
    def test_close_doble_es_noop_y_use_after_close_rechaza(self) -> None:
        parts = _make_context_components()
        session = PrivilegedBoundarySession(operator_token=parts["_token"], lock=parts["_handle"])
        assert session.close() is True
        assert session.close() is False
        with pytest.raises(AuthorizationSessionError):
            _ = session.operator_token
        with pytest.raises(AuthorizationSessionError):
            _ = session.lock

    def test_sesion_context_manager_cierra(self) -> None:
        parts = _make_context_components()
        with PrivilegedBoundarySession(operator_token=parts["_token"], lock=parts["_handle"]) as session:
            assert not session.closed
        assert session.closed
        assert parts["_token"].closed
        assert parts["_handle"].closed


# ============================================================================
# §13: fundación del plan autorizado (NO durable; AUTH-05)
# ============================================================================


class TestAuthorizedPlanFoundation:
    def _context(self) -> PrivilegedAuthorizationContext:
        parts = _make_context_components()
        try:
            return PrivilegedAuthorizationContext(
                operation_id=parts["operation_id"],
                staging_digest=parts["staging_digest"],
                coordinator_identity=parts["coordinator_identity"],
                operator_identity=parts["operator_identity"],
                ppsc_confirmation=parts["ppsc_confirmation"],
                lock_identity=parts["lock_identity"],
            )
        finally:
            parts["_handle"].release()
            parts["_token"].close()

    def test_auth_04_header_campos_exactos_ocho(self) -> None:
        fields = [field.name for field in dataclasses.fields(AuthorizedPlanFoundationHeader)]
        assert fields == list(FOUNDATION_HEADER_KEYS)

    def test_auth_05_estado_declarado_no_durable_sin_autoridad_de_mutacion(self) -> None:
        assert AUTHORIZED_PLAN_FOUNDATION_STATUS == "NOT_DURABLE_AUTHORIZED_PLAN_NOT_MUTATION_AUTHORITY"

    def test_header_serializacion_canonica_y_digest_determinista(self) -> None:
        header = build_foundation_header_from_context(self._context())
        raw = serialize_authorized_plan_foundation_header(header)
        assert raw == serialize_authorized_plan_foundation_header(header)
        digest = compute_foundation_plan_digest(header)
        assert digest == hashlib.sha256(raw).hexdigest()
        assert digest == compute_foundation_plan_digest(header)

    def test_digest_cambia_ante_manipulacion(self) -> None:
        header = build_foundation_header_from_context(self._context())
        mutated = AuthorizedPlanFoundationHeader(
            operation_id=header.operation_id,
            canonical_root=header.canonical_root,
            volume_serial_number=header.volume_serial_number,
            root_file_id=header.root_file_id,
            tree_digest=header.tree_digest,
            node_count=8,
            policy_version=header.policy_version,
            staging_digest=header.staging_digest,
        )
        assert compute_foundation_plan_digest(mutated) != compute_foundation_plan_digest(header)

    def test_auth_05_ast_sin_escritura_durable_en_modulo(self) -> None:
        # Ancla sobre el CÓDIGO (no docstrings): la foundation §13 no contiene
        # NINGÚN writer durable invocable; las menciones en docstrings nombran
        # lo prohibido. Se verifica que el módulo no importa os.path/os.replace,
        # no abre archivos y no llama a writers del TGR.
        import ast
        import io
        import tokenize

        module_path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "sky_claw"
            / "local"
            / "runtime_vault"
            / "authorization_context.py"
        )
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        call_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    call_names.add(func.attr)
                elif isinstance(func, ast.Name):
                    call_names.add(func.id)
        for forbidden_call in ("replace", "open", "write_text", "write_bytes"):
            assert forbidden_call not in call_names, f"Writer durable invocado: {forbidden_call}"
        names = {
            tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline) if tok.type == tokenize.NAME
        }
        assert "SetSecurityInfo" not in names
        assert "_write_trusted_registry_atomically" not in names
        # El JSON del header se expone sólo como bytes en memoria.
        assert "dumps" in call_names

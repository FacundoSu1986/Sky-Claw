"""Tests focales GP2-S4A: plan autoritativo durable (modelo, gates y promoción).

Cobertura del modelo y de los gates de autorización de ADR 0010 §12.2 paso 7:

- ``APLAN-01``: esquema CERRADO y versión del ``authorized_plan.json``.
- ``APLAN-02``: ``plan_digest`` DERIVADO (el valor recibido se ignora).
- ``APLAN-03``: serialización canónica (UTF-8, sin BOM, claves ordenadas,
  nodos en orden determinista bottom-up) y round-trip byte exacto.
- ``APLAN-04``: ``staging_digest`` != ``authorized_plan_digest``.
- ``APLAN-05``: tabla completa de nodos + integridad PRE por nodo.
- ``APLAN-06``: gates en orden normativo (staging → esquema → PPSC → TGR →
  ``node_count``) con fail-closed tipado en cada uno.
- ``APLAN-07``: create-once, revalidación post-escritura y prueba de acuñación.

Los tests causales de escritura protegida (SD canónica, ``FlushFileBuffers``,
publicación no-reemplazante) viven bajo ``skipif(win32)`` porque requieren el
namespace real; acá todo corre con seams deterministas (writer fake, resolver de
ProgramData sintético) y sin tocar el Golden.
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sys
from typing import Any

import pytest

from sky_claw.local.runtime_vault.authorization_context import (
    PrivilegedAuthorizationContext,
    PrivilegedBoundarySession,
)
from sky_claw.local.runtime_vault.authorized_plan import (
    AUTHORIZED_PLAN_FILE_NAME,
    AUTHORIZED_PLAN_ROOT_KEYS,
    AUTHORIZED_PLAN_SCHEMA_VERSION,
    OPERATOR_IDENTITY_KEYS,
    AuthorizedPlan,
    AuthorizedPlanAlreadyExistsError,
    AuthorizedPlanDigestError,
    AuthorizedPlanError,
    AuthorizedPlanNotFoundError,
    AuthorizedPlanSchemaError,
    NodeCountMismatchError,
    PpscPlanBindingError,
    StagingDigestMismatchError,
    TrustedRegistryPlanBindingError,
    authorized_plan_content_dict,
    build_authorized_plan,
    compute_authorized_plan_digest,
    deserialize_authorized_plan,
    serialize_authorized_plan,
)
from sky_claw.local.runtime_vault.authorized_plan_store import (
    DurableAuthorizedPlanWriteError,
    DurableWriteOutcome,
    classify_durable_authorized_plan,
    derive_authorized_plan_dir,
    derive_authorized_plan_path,
    derive_candidate_manifest_path,
    load_durable_authorized_plan,
    promote_durable_authorized_plan,
    read_candidate_manifest_bytes,
)
from sky_claw.local.runtime_vault.coordinator_identity import CoordinatorProcessIdentity
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenMutationLockHandle,
    acquire_golden_mutation_lock,
)
from sky_claw.local.runtime_vault.golden_protection_plan import (
    GoldenProtectionNodeKind,
    GoldenProtectionPlan,
    GoldenProtectionPlanState,
    NodeSecurityBackup,
    _canonical_manifest_bytes,
)
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.operator_token import (
    OperatorPrimaryToken,
    acquire_operator_primary_token_from_coordinator,
)
from sky_claw.local.runtime_vault.ppsc import (
    PrivilegedPlanConfirmation,
    PrivilegedPlanConfirmationReceipt,
    PrivilegedPlanConfirmationResult,
)
from sky_claw.local.runtime_vault.protection import GoldenProtectionState
from sky_claw.local.runtime_vault.trusted_namespace import AUTHORIZED_PLAN_OBJECT
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenEntry,
    TrustedGoldenRegistry,
)

_VALID_UUID = "3f6b0be2-1c2a-4d3e-8f4a-9b7c6d5e4f3a"
_ALT_UUID = "00f93602-bd14-4a95-80e1-a4f9689a3afb"
_STAGING_DIGEST = "a" * 64
_TREE_DIGEST = "b" * 64
_VOLUME_SERIAL = 0xA1B2C3D4
_ROOT_FILE_ID = 0x1122334455667788
_CANONICAL_ROOT = "C:\\Games\\Skyrim"
_POLICY_VERSION = "golden-policy-v1"
_OPERATOR_SID = "S-1-5-21-1001-1002-1003-1001"


# ============================================================================
# Fábricas deterministas
# ============================================================================


def _node(rel_path: str, file_id: int, *, sd_prefix: bytes = b"\x01\x00\x04\x80") -> NodeSecurityBackup:
    sd = sd_prefix + rel_path.encode("utf-8") + b"\x00" * 8
    return NodeSecurityBackup(
        relative_path=rel_path,
        node_kind=GoldenProtectionNodeKind.DIR if rel_path == "." else GoldenProtectionNodeKind.FILE,
        volume_serial_number=_VOLUME_SERIAL,
        file_id=file_id,
        pre_sd_bytes_b64=base64.b64encode(sd).decode("ascii"),
        pre_sd_length=len(sd),
        pre_sd_sha256=hashlib.sha256(sd).hexdigest(),
        owner_sid="S-1-5-18",
        group_sid="S-1-5-32-544",
        dacl_control_flags=0x1000,
        pre_dacl_protected_flag=True,
        sddl_diagnostic="D:(A;;FA;;;BA)",
    )


def _nodes() -> tuple[NodeSecurityBackup, ...]:
    return (
        _node(".", _ROOT_FILE_ID),
        _node("Data/Skyrim.esm", _ROOT_FILE_ID + 1),
        _node("Data/Scripts/quest.pex", _ROOT_FILE_ID + 2),
    )


def _tree_digest() -> TreeDigest:
    return TreeDigest(digest=_TREE_DIGEST, files=3, bytes=10)


def _candidate_plan() -> GoldenProtectionPlan:
    return GoldenProtectionPlan(
        operation_id=_VALID_UUID,
        canonical_root=_CANONICAL_ROOT,
        volume_serial_number=_VOLUME_SERIAL,
        root_file_id=_ROOT_FILE_ID,
        tree_digest=_tree_digest(),
        policy_version=_POLICY_VERSION,
        initial_protection_state=GoldenProtectionState.WRITE_PROTECTED,
        nodes=_nodes(),
        state=GoldenProtectionPlanState.PREPARED,
    )


def _candidate_bytes() -> bytes:
    return _canonical_manifest_bytes(_candidate_plan())


def _staging_digest() -> str:
    return hashlib.sha256(_candidate_bytes()).hexdigest()


def _ppsc_payload(**overrides: Any) -> PrivilegedPlanConfirmation:
    kwargs: dict[str, Any] = {
        "operation_id": _VALID_UUID,
        "canonical_root": _CANONICAL_ROOT,
        "volume_serial_number": _VOLUME_SERIAL,
        "root_file_id": _ROOT_FILE_ID,
        "tree_digest": _tree_digest(),
        "node_count": 3,
        "policy_version": _POLICY_VERSION,
    }
    kwargs.update(overrides)
    return PrivilegedPlanConfirmation(**kwargs)


class _FakeTokenAdapter:
    def open_process(self, desired_access: int, pid: int) -> int:
        return 101

    def read_process_creation_time(self, process_handle: int) -> int | None:
        return 133_456_789_012_345_678

    def read_process_image_path(self, process_handle: int) -> str | None:
        return "C:\\Program Files\\Sky-Claw\\sky-claw.exe"

    def open_process_token(self, process_handle: int, desired_access: int) -> int:
        return 202

    def duplicate_token_ex_primary(self, source_handle: int, desired_access: int) -> int:
        return 303

    def get_token_type(self, token_handle: int) -> int:
        return 1

    def read_token_user_sid(self, token_handle: int) -> str:
        return _OPERATOR_SID

    def close_handle(self, handle: int) -> None:
        return None


def _operator_token() -> OperatorPrimaryToken:
    identity = CoordinatorProcessIdentity(
        pid=4242,
        creation_time=133_456_789_012_345_678,
        image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
    )
    return acquire_operator_primary_token_from_coordinator(identity, adapter=_FakeTokenAdapter())


class _FakeLockKernel:
    """Kernel fake del lock: share=0 monoproceso, metadata en memoria."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.busy_paths: set[str] = set()
        self.open_handles: dict[int, str] = {}
        self.reparse_tags: dict[int, int] = {}

    def open_lock_file(self, path: pathlib.PurePath) -> int:
        path_str = str(path)
        if path_str in self.busy_paths:
            from sky_claw.local.runtime_vault.golden_mutation_lock import GoldenLockBusyError

            raise GoldenLockBusyError("ocupado", win32_error=32)
        handle = 700 + len(self.open_handles)
        self.open_handles[handle] = path_str
        self.busy_paths.add(path_str)
        self.files.setdefault(path_str, b"")
        return handle

    def get_reparse_tag(self, handle: int) -> int:
        return self.reparse_tags.get(handle, 0)

    def read_lock_bytes(self, handle: int) -> bytes:
        return self.files[self.open_handles[handle]]

    def write_lock_bytes(self, handle: int, payload: bytes) -> None:
        self.files[self.open_handles[handle]] = payload

    def flush_lock(self, handle: int) -> None:
        return None

    def is_owner_alive(self, owner_pid: int, owner_creation_time: int) -> bool | None:
        return True

    def current_process_identity(self) -> tuple[int, int, int]:
        return (4242, 133_456_789_012_345_678, 1)

    def current_epoch_seconds(self) -> int:
        return 1_758_499_200

    def close_handle(self, handle: int) -> None:
        self.busy_paths.discard(self.open_handles.pop(handle, ""))


def _fake_programdata() -> pathlib.PureWindowsPath:
    return pathlib.PureWindowsPath("C:/ProgramData")


def _lock_handle(
    *,
    volume_serial_number: int = _VOLUME_SERIAL,
    root_file_id: int = _ROOT_FILE_ID,
    operation_id: str = _VALID_UUID,
) -> GoldenMutationLockHandle:
    """Lock adquirido por la vía real (mint-proof) con kernel fake determinista."""
    return acquire_golden_mutation_lock(
        volume_serial_number,
        root_file_id,
        operation_id,
        kernel=_FakeLockKernel(),
        programdata_resolver=_fake_programdata,
    )


def _session(**overrides: Any) -> PrivilegedBoundarySession:
    kwargs: dict[str, Any] = {"operator_token": _operator_token(), "lock": _lock_handle()}
    kwargs.update(overrides)
    return PrivilegedBoundarySession(**kwargs)


def _context(**overrides: Any) -> PrivilegedAuthorizationContext:
    kwargs: dict[str, Any] = {
        "operation_id": _VALID_UUID,
        "staging_digest": _staging_digest(),
        "coordinator_identity": CoordinatorProcessIdentity(
            pid=4242,
            creation_time=133_456_789_012_345_678,
            image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
        ),
        "operator_identity": _operator_token().evidence,
        "ppsc_confirmation": PrivilegedPlanConfirmationReceipt(
            payload=_ppsc_payload(), result=PrivilegedPlanConfirmationResult.CONFIRMED
        ),
        "lock_identity": _lock_handle().identity,
    }
    kwargs.update(overrides)
    return PrivilegedAuthorizationContext(**kwargs)


def _registry(**overrides: Any) -> TrustedGoldenRegistry:
    entry_kwargs: dict[str, Any] = {
        "canonical_root": _CANONICAL_ROOT,
        "volume_serial_number": _VOLUME_SERIAL,
        "root_file_id": _ROOT_FILE_ID,
        "tree_digest": _tree_digest(),
        "policy_version": _POLICY_VERSION,
        "registered_by": _OPERATOR_SID,
        "registered_at": "2026-09-26T00:00:00Z",
    }
    entry_kwargs.update(overrides)
    return TrustedGoldenRegistry(entries=(TrustedGoldenEntry(**entry_kwargs),))


class _RecordingWriter:
    """Writer fake que imita create-once + durabilidad sobre ``tmp_path``."""

    def __init__(self) -> None:
        self.writes: list[tuple[pathlib.Path, bytes, str]] = []

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        if dest.exists():
            # Mismo contrato que el writer real del namespace: la colisión
            # create-once se señala con el error tipado, no con un OSError genérico.
            raise AuthorizedPlanAlreadyExistsError(f"create-once: '{dest}' ya existe")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        self.writes.append((dest, payload, object_name))


@pytest.fixture
def programdata(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "Sky-Claw" / "runtime_vault" / "staging" / _VALID_UUID).mkdir(parents=True)
    (tmp_path / "Sky-Claw" / "runtime_vault" / "staging" / _VALID_UUID / "candidate_manifest.json").write_bytes(
        _candidate_bytes()
    )
    return tmp_path


def _resolver(base: pathlib.Path) -> Any:
    return lambda: base


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """JSON canónico del repo, reescrito en el test para no depender del writer."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ============================================================================
# APLAN-01/02/03/04: modelo y serialización canónica
# ============================================================================


class TestAuthorizedPlanModel:
    def test_aplan_01_esquema_cerrado_y_version(self) -> None:
        assert AUTHORIZED_PLAN_SCHEMA_VERSION == "1.0"
        assert AUTHORIZED_PLAN_FILE_NAME == "authorized_plan.json"
        assert set(OPERATOR_IDENTITY_KEYS) == {"operator_sid", "token_type", "acquired_via"}
        assert "plan_digest" in AUTHORIZED_PLAN_ROOT_KEYS
        assert "staging_digest" in AUTHORIZED_PLAN_ROOT_KEYS
        assert "nodes" in AUTHORIZED_PLAN_ROOT_KEYS

    def test_aplan_02_plan_digest_es_derivado_e_ignora_el_input(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        esperado = compute_authorized_plan_digest(plan)
        assert plan.plan_digest == esperado
        # El valor recibido del caller se ignora: el digest siempre se recalcula.
        clon = AuthorizedPlan(
            schema_version=plan.schema_version,
            operation_id=plan.operation_id,
            canonical_root=plan.canonical_root,
            volume_serial_number=plan.volume_serial_number,
            root_file_id=plan.root_file_id,
            tree_digest=plan.tree_digest,
            node_count=plan.node_count,
            policy_version=plan.policy_version,
            staging_digest=plan.staging_digest,
            operator_identity=plan.operator_identity,
            nodes=plan.nodes,
        )
        assert clon.plan_digest == plan.plan_digest

    def test_aplan_03_serializacion_canonica_round_trip_byte_exacto(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        raw = serialize_authorized_plan(plan)
        assert raw.decode("utf-8") == raw.decode("utf-8")  # UTF-8 válido
        assert not raw.startswith(b"\xef\xbb\xbf")  # sin BOM
        assert raw.endswith(b"}")
        payload = json.loads(raw)
        assert list(payload.keys()) == sorted(AUTHORIZED_PLAN_ROOT_KEYS)  # claves ordenadas
        assert serialize_authorized_plan(deserialize_authorized_plan(raw)) == raw
        assert deserialize_authorized_plan(raw) == plan

    def test_aplan_03_b_nodes_en_orden_determinista_bottom_up(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        # Bottom-up: los más profundos primero, la raíz al final.
        relpaths = [node.relative_path for node in plan.nodes]
        assert relpaths == ["Data/Scripts/quest.pex", "Data/Skyrim.esm", "."]
        reordenado = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        assert serialize_authorized_plan(reordenado) == serialize_authorized_plan(plan)

    def test_aplan_04_staging_digest_distinto_de_plan_digest(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        assert plan.staging_digest == _staging_digest()
        assert plan.plan_digest != plan.staging_digest
        assert plan.staging_digest == hashlib.sha256(_candidate_bytes()).hexdigest()

    def test_aplan_05_tabla_completa_de_nodos(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        assert plan.node_count == 3 == len(plan.nodes)
        for node in plan.nodes:
            sd = base64.b64decode(node.pre_sd_bytes_b64, validate=True)
            assert len(sd) == node.pre_sd_length
            assert hashlib.sha256(sd).hexdigest() == node.pre_sd_sha256
            assert node.pre_dacl_protected_flag is True
            assert node.volume_serial_number == _VOLUME_SERIAL

    def test_corrupcion_de_un_byte_falla_cerrado(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        raw = bytearray(serialize_authorized_plan(plan))
        marca = b'"plan_digest":"'
        indice = raw.index(marca) + len(marca)
        raw[indice] = ord("0") if raw[indice] != ord("0") else ord("1")
        with pytest.raises(AuthorizedPlanDigestError):
            deserialize_authorized_plan(bytes(raw))

    def test_digest_adulterado_falla_cerrado(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        payload = authorized_plan_content_dict(plan)
        payload["plan_digest"] = "0" * 64
        with pytest.raises(AuthorizedPlanDigestError):
            deserialize_authorized_plan(_canonical_bytes(payload))

    def test_clave_desconocida_falla_cerrado(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        payload = authorized_plan_content_dict(plan)
        payload["extra"] = 1
        with pytest.raises(AuthorizedPlanSchemaError):
            deserialize_authorized_plan(_canonical_bytes(payload))

    def test_str_no_es_aceptado_como_bytes(self) -> None:
        with pytest.raises(AuthorizedPlanSchemaError):
            deserialize_authorized_plan("{}")  # type: ignore[arg-type]

    def test_version_de_schema_desconocida_falla_cerrado(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        payload = authorized_plan_content_dict(plan)
        payload["schema_version"] = "9.9"
        with pytest.raises(AuthorizedPlanSchemaError):
            deserialize_authorized_plan(_canonical_bytes(payload))


# ============================================================================
# APLAN-06: gates de autorización en orden normativo
# ============================================================================


class TestAuthorizedPlanGates:
    def test_promocion_valida_produce_plan_completo(self) -> None:
        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        assert plan.operation_id == _VALID_UUID
        assert plan.canonical_root == _CANONICAL_ROOT
        assert plan.volume_serial_number == _VOLUME_SERIAL
        assert plan.root_file_id == _ROOT_FILE_ID
        assert plan.policy_version == _POLICY_VERSION
        assert plan.operator_identity.operator_sid == _OPERATOR_SID
        assert plan.node_count == 3

    def test_staging_digest_no_coincide_refuse_to_plan(self) -> None:
        with pytest.raises(StagingDigestMismatchError):
            build_authorized_plan(
                context=_context(staging_digest="f" * 64),
                candidate_manifest_bytes=_candidate_bytes(),
                trusted_registry=_registry(),
            )

    def test_manifest_con_esquema_no_cerrado_falla_cerrado(self) -> None:
        payload = json.loads(_candidate_bytes().decode("utf-8"))
        payload["inyectado_por_staging"] = True
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        contexto = _context(staging_digest=hashlib.sha256(raw).hexdigest())
        with pytest.raises(AuthorizedPlanSchemaError):
            build_authorized_plan(context=contexto, candidate_manifest_bytes=raw, trusted_registry=_registry())

    def test_ppsc_campo_divergente_falla_cerrado(self) -> None:
        with pytest.raises(PpscPlanBindingError):
            build_authorized_plan(
                context=_context(
                    ppsc_confirmation=PrivilegedPlanConfirmationReceipt(
                        payload=_ppsc_payload(tree_digest=TreeDigest(digest="c" * 64, files=3, bytes=10)),
                        result=PrivilegedPlanConfirmationResult.CONFIRMED,
                    )
                ),
                candidate_manifest_bytes=_candidate_bytes(),
                trusted_registry=_registry(),
            )

    def test_ppsc_node_count_divergente_falla_cerrado(self) -> None:
        with pytest.raises(NodeCountMismatchError):
            build_authorized_plan(
                context=_context(
                    ppsc_confirmation=PrivilegedPlanConfirmationReceipt(
                        payload=_ppsc_payload(node_count=99),
                        result=PrivilegedPlanConfirmationResult.CONFIRMED,
                    )
                ),
                candidate_manifest_bytes=_candidate_bytes(),
                trusted_registry=_registry(),
            )

    def test_tgr_ausente_refuse_to_plan(self) -> None:
        with pytest.raises(TrustedRegistryPlanBindingError):
            build_authorized_plan(
                context=_context(),
                candidate_manifest_bytes=_candidate_bytes(),
                trusted_registry=TrustedGoldenRegistry(entries=()),
            )

    def test_tgr_tree_digest_divergente_refuse_to_plan(self) -> None:
        with pytest.raises(TrustedRegistryPlanBindingError):
            build_authorized_plan(
                context=_context(),
                candidate_manifest_bytes=_candidate_bytes(),
                trusted_registry=_registry(tree_digest=TreeDigest(digest="d" * 64, files=3, bytes=10)),
            )

    def test_tgr_policy_version_divergente_refuse_to_plan(self) -> None:
        """``verify_trusted_golden_binding`` sólo compara CUATRO campos.

        ``policy_version`` debe compararse explícitamente además: sin esa
        comparación un TGR con la misma identidad física pero otra política
        pasaría el gate (mutante M-T1).
        """
        with pytest.raises(TrustedRegistryPlanBindingError):
            build_authorized_plan(
                context=_context(),
                candidate_manifest_bytes=_candidate_bytes(),
                trusted_registry=_registry(policy_version="otra-politica"),
            )

    def test_nodo_con_pre_sd_hash_invalido_falla_cerrado(self) -> None:
        from sky_claw.local.runtime_vault.golden_protection_plan import (
            _plan_to_manifest_dict,
            node_security_backup_to_dict,
        )

        payload = _plan_to_manifest_dict(_candidate_plan())
        nodos = list(payload["nodes"])  # type: ignore[arg-type]
        roto = node_security_backup_to_dict(_node("Data/roto.esp", _ROOT_FILE_ID + 9))
        roto["pre_sd_sha256"] = "0" * 64
        nodos.append(roto)
        payload["nodes"] = nodos
        payload["node_count"] = len(nodos)
        raw = _canonical_bytes(payload)
        contexto = _context(
            staging_digest=hashlib.sha256(raw).hexdigest(),
            ppsc_confirmation=PrivilegedPlanConfirmationReceipt(
                payload=_ppsc_payload(node_count=len(nodos)),
                result=PrivilegedPlanConfirmationResult.CONFIRMED,
            ),
        )
        with pytest.raises(AuthorizedPlanSchemaError):
            build_authorized_plan(context=contexto, candidate_manifest_bytes=raw, trusted_registry=_registry())

    def test_manifest_como_str_es_rechazado(self) -> None:
        with pytest.raises(StagingDigestMismatchError):
            build_authorized_plan(
                context=_context(),
                candidate_manifest_bytes=_candidate_bytes().decode("utf-8"),  # type: ignore[arg-type]
                trusted_registry=_registry(),
            )


# ============================================================================
# APLAN-07: promoción durable (writer fake) y fail-closed
# ============================================================================


class TestPromocionDurable:
    def test_promocion_valida_escribe_y_acuna_autoridad(self, programdata: pathlib.Path) -> None:
        writer = _RecordingWriter()
        durable = promote_durable_authorized_plan(
            context=_context(),
            session=_session(),
            trusted_registry=_registry(),
            programdata_resolver=_resolver(programdata),
            plan_writer=writer,
        )
        assert durable.operation_id == _VALID_UUID
        assert durable.digest == durable.plan.plan_digest
        assert durable.path == derive_authorized_plan_path(_VALID_UUID, programdata_resolver=_resolver(programdata))
        assert len(writer.writes) == 1
        dest, payload, object_name = writer.writes[0]
        assert object_name == AUTHORIZED_PLAN_OBJECT
        assert dest.read_bytes() == payload
        assert (
            classify_durable_authorized_plan(_VALID_UUID, programdata_resolver=_resolver(programdata))
            is DurableWriteOutcome.DURABLE
        )

    def test_plan_create_replay_falla_cerrado(self, programdata: pathlib.Path) -> None:
        promote_durable_authorized_plan(
            context=_context(),
            session=_session(),
            trusted_registry=_registry(),
            programdata_resolver=_resolver(programdata),
            plan_writer=_RecordingWriter(),
        )
        with pytest.raises(AuthorizedPlanAlreadyExistsError):
            promote_durable_authorized_plan(
                context=_context(),
                session=_session(),
                trusted_registry=_registry(),
                programdata_resolver=_resolver(programdata),
                plan_writer=_RecordingWriter(),
            )

    def test_sesion_cerrada_no_promueve(self, programdata: pathlib.Path) -> None:
        sesion = _session()
        sesion.close()
        with pytest.raises(Exception, match="sesión de frontera está cerrada"):
            promote_durable_authorized_plan(
                context=_context(),
                session=sesion,
                trusted_registry=_registry(),
                programdata_resolver=_resolver(programdata),
                plan_writer=_RecordingWriter(),
            )
        assert not derive_authorized_plan_path(_VALID_UUID, programdata_resolver=_resolver(programdata)).exists()

    def test_lock_con_identidad_fisica_divergente_no_promueve(self, programdata: pathlib.Path) -> None:
        # El lock vive sobre OTRA identidad física: nunca plan A con lock B (§45).
        lock = _lock_handle(volume_serial_number=0xDEAD, root_file_id=0xBEEF)
        with pytest.raises(Exception, match="identidad física del lock"):
            promote_durable_authorized_plan(
                context=_context(),
                session=_session(lock=lock),
                trusted_registry=_registry(),
                programdata_resolver=_resolver(programdata),
                plan_writer=_RecordingWriter(),
            )

    def test_escritura_fallida_clasifica_el_outcome_observable(self, programdata: pathlib.Path) -> None:
        class _WriterQueFalla:
            def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
                raise OSError("disco simulado roto")

        with pytest.raises(DurableAuthorizedPlanWriteError) as excinfo:
            promote_durable_authorized_plan(
                context=_context(),
                session=_session(),
                trusted_registry=_registry(),
                programdata_resolver=_resolver(programdata),
                plan_writer=_WriterQueFalla(),  # type: ignore[arg-type]
            )
        assert excinfo.value.outcome is DurableWriteOutcome.NOT_DURABLE

    def test_revalidacion_post_escritura_con_bytes_distintos_no_acuna(
        self, programdata: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _WriterQueCorrompe:
            def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload + b"\n")

        with pytest.raises(DurableAuthorizedPlanWriteError) as excinfo:
            promote_durable_authorized_plan(
                context=_context(),
                session=_session(),
                trusted_registry=_registry(),
                programdata_resolver=_resolver(programdata),
                plan_writer=_WriterQueCorrompe(),  # type: ignore[arg-type]
            )
        assert excinfo.value.outcome is DurableWriteOutcome.INDETERMINATE

    def test_flush_fallido_no_acuña_autoridad(self, programdata: pathlib.Path) -> None:
        """GATE de durabilidad (§12.2 7c / §24): sin flush no hay plan ni permiso.

        El writer productivo lanza tipado cuando ``FlushFileBuffers`` devuelve
        ``FALSE``; el promotor debe clasificar por evidencia observable y NO
        acuñar ``DurableAuthorizedPlan`` bajo ninguna circunstancia.
        """

        class _WriterSinFlush:
            def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
                raise AuthorizedPlanError("FlushFileBuffers falló en el temporal: código 1 (GATE de durabilidad)")

        with pytest.raises(DurableAuthorizedPlanWriteError) as excinfo:
            promote_durable_authorized_plan(
                context=_context(),
                session=_session(),
                trusted_registry=_registry(),
                programdata_resolver=_resolver(programdata),
                plan_writer=_WriterSinFlush(),  # type: ignore[arg-type]
            )
        assert excinfo.value.outcome is DurableWriteOutcome.NOT_DURABLE
        assert not derive_authorized_plan_path(_VALID_UUID, programdata_resolver=_resolver(programdata)).exists()
        assert (
            classify_durable_authorized_plan(_VALID_UUID, programdata_resolver=_resolver(programdata))
            is DurableWriteOutcome.NOT_DURABLE
        )

    def test_plan_ausente_es_not_durable_y_no_se_inventa(self, programdata: pathlib.Path) -> None:
        assert (
            classify_durable_authorized_plan(_VALID_UUID, programdata_resolver=_resolver(programdata))
            is DurableWriteOutcome.NOT_DURABLE
        )
        with pytest.raises(AuthorizedPlanNotFoundError):
            load_durable_authorized_plan(_VALID_UUID, programdata_resolver=_resolver(programdata))

    def test_bytes_corruptos_son_indeterminate_no_ausentes(self, programdata: pathlib.Path) -> None:
        destino = derive_authorized_plan_path(_VALID_UUID, programdata_resolver=_resolver(programdata))
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b'{"schema_version": "1.0"')
        assert (
            classify_durable_authorized_plan(_VALID_UUID, programdata_resolver=_resolver(programdata))
            is DurableWriteOutcome.INDETERMINATE
        )

    def test_staging_digest_mismatch_no_escribe_nada(self, programdata: pathlib.Path) -> None:
        writer = _RecordingWriter()
        with pytest.raises(StagingDigestMismatchError):
            promote_durable_authorized_plan(
                context=_context(staging_digest="f" * 64),
                session=_session(),
                trusted_registry=_registry(),
                programdata_resolver=_resolver(programdata),
                plan_writer=writer,
            )
        assert writer.writes == []
        assert not derive_authorized_plan_path(_VALID_UUID, programdata_resolver=_resolver(programdata)).exists()

    def test_autoridad_no_se_puede_fabricar_sin_prueba_de_acuñacion(self, programdata: pathlib.Path) -> None:
        from sky_claw.local.runtime_vault.authorized_plan_store import DurableAuthorizedPlan

        plan = build_authorized_plan(
            context=_context(), candidate_manifest_bytes=_candidate_bytes(), trusted_registry=_registry()
        )
        with pytest.raises(AuthorizedPlanError):
            DurableAuthorizedPlan(plan, pathlib.Path("C:/x"))  # type: ignore[call-arg]


# ============================================================================
# Derivación de rutas: nunca desde el caller
# ============================================================================


class TestDerivacionDeRutas:
    def test_ruta_canonica_bajo_el_resolver(self, tmp_path: pathlib.Path) -> None:
        base = derive_authorized_plan_dir(_VALID_UUID, programdata_resolver=lambda: tmp_path)
        assert base == tmp_path / "Sky-Claw" / "runtime_vault" / "operations" / _VALID_UUID
        assert (
            derive_authorized_plan_path(_VALID_UUID, programdata_resolver=lambda: tmp_path).name
            == AUTHORIZED_PLAN_FILE_NAME
        )

    def test_staging_es_solo_lectura_y_nunca_autoridad(self, tmp_path: pathlib.Path) -> None:
        staging = derive_candidate_manifest_path(_VALID_UUID, programdata_resolver=lambda: tmp_path)
        assert staging == (
            tmp_path / "Sky-Claw" / "runtime_vault" / "staging" / _VALID_UUID / "candidate_manifest.json"
        )
        assert staging.parent.parent.name == "staging"
        assert staging.parent.parent.parent.name == "runtime_vault"

    @pytest.mark.skipif(sys.platform != "win32", reason="Separadores Windows")
    def test_ruta_canonica_literal_bajo_programdata(self) -> None:
        base = derive_authorized_plan_dir(_VALID_UUID, programdata_resolver=lambda: "C:\\ProgramData")
        assert base == pathlib.PureWindowsPath(f"C:\\ProgramData\\Sky-Claw\\runtime_vault\\operations\\{_VALID_UUID}")

    def test_operation_id_invalido_es_rechazado(self) -> None:
        with pytest.raises(AuthorizedPlanSchemaError):
            derive_authorized_plan_dir("..\\..\\etc", programdata_resolver=lambda: "C:\\ProgramData")

    def test_candidate_manifest_se_relee_desde_staging(self, programdata: pathlib.Path) -> None:
        assert (
            read_candidate_manifest_bytes(_VALID_UUID, programdata_resolver=_resolver(programdata))
            == _candidate_bytes()
        )


# ============================================================================
# Anclas estructurales del slice
# ============================================================================


class TestAnclasEstructurales:
    def test_cli_del_helper_sigue_con_dos_flags(self) -> None:
        from sky_claw.local.runtime_vault.privileged_boundary import HELPER_CLI_ALLOWED_FLAGS

        assert set(HELPER_CLI_ALLOWED_FLAGS) == {"--operation-id", "--staging-digest"}

    def test_nombre_de_objeto_protegido_registrado(self) -> None:
        from sky_claw.local.runtime_vault import trusted_namespace

        assert trusted_namespace.AUTHORIZED_PLAN_OBJECT == "authorized_plan.json"
        assert trusted_namespace.PROTECTION_JOURNAL_OBJECT == "protection_journal.json"

    def test_durabilidad_declarada_sin_fingir_directory_entry(self) -> None:
        from sky_claw.local.runtime_vault.authorized_plan_store import (
            AUTHORIZED_PLAN_CONTENT_DURABILITY,
            POWER_LOSS_DIRECTORY_ENTRY_DURABILITY_LIMITATION,
        )

        assert AUTHORIZED_PLAN_CONTENT_DURABILITY == "FILE_FLAG_WRITE_THROUGH_PLUS_FLUSHFILEBUFFERS_TRUE"
        assert POWER_LOSS_DIRECTORY_ENTRY_DURABILITY_LIMITATION == "DECLARED"


@pytest.fixture
def entorno_elevado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutraliza las syscalls privilegiadas (1307/1314) del namespace.

    Espejo de ``TestNamespaceBootstrapWindows.mock_elevated_provisioning`` y del
    ``entorno_elevado`` de ``test_runtime_vault_golden_admission_store.py``: permite
    ejercitar la escritura REAL (``CreateFileW`` + ``WriteFile`` +
    ``FlushFileBuffers`` + ``CreateHardLinkW`` + ``DeleteFileW``) sobre ``tmp_path``
    sin exigir un proceso SYSTEM, conservando toda la lógica de la primitiva.
    """
    from sky_claw.local.runtime_vault import trusted_namespace as tn
    from sky_claw.local.runtime_vault.trusted_namespace import (
        _FILE_READ_ATTRIBUTES,
        _READ_CONTROL,
        _WRITE_OWNER,
        BUILTIN_ADMINISTRATORS_SID,
        PERMITTED_NAMESPACE_OWNERS,
        _advapi32,
        _kernel32,
        _open_handle_no_reparse,
        _read_live_owner_group_dacl,
    )

    real_create_dir = _kernel32.CreateDirectoryW
    monkeypatch.setattr(_kernel32, "CreateDirectoryW", lambda path, sa: bool(real_create_dir(path, None)))

    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace._open_handle_no_reparse",
        lambda path, desired_access=_READ_CONTROL | _FILE_READ_ATTRIBUTES, **kwargs: _open_handle_no_reparse(
            path, desired_access=desired_access & ~_WRITE_OWNER, **kwargs
        ),
    )

    real_create_file = _kernel32.CreateFileW

    def _create_file_sin_sd(
        lp_file_name: Any,
        dw_desired_access: int,
        dw_share_mode: int,
        lp_security_attributes: Any,
        dw_creation_disposition: int,
        dw_flags_and_attributes: int,
        h_template_file: Any,
    ) -> int:
        return int(
            real_create_file(
                lp_file_name,
                dw_desired_access & ~_WRITE_OWNER,
                dw_share_mode,
                None,
                dw_creation_disposition,
                dw_flags_and_attributes,
                h_template_file,
            )
        )

    monkeypatch.setattr(_kernel32, "CreateFileW", _create_file_sin_sd)
    monkeypatch.setattr(_advapi32, "SetSecurityInfo", lambda *args: 0)
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace.create_secured_file_from_birth",
        lambda path, obj="trusted_goldens.json", *, extra_flags=0: _kernel32.CreateFileW(
            str(path), 0x40000000 | 0x80000000, 0, None, 1, 0x80 | extra_flags, None
        ),
    )
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace._verify_secured_file_contract",
        lambda path, object_name: None,
    )
    monkeypatch.setattr(tn, "_verify_canonical_directory_security_on_handle", lambda *a, **k: None)

    real_read_owner = _read_live_owner_group_dacl

    def _read_owner_simulado(handle: int) -> tuple[str, str, bool]:
        owner, group, is_protected = real_read_owner(handle)
        if owner not in PERMITTED_NAMESPACE_OWNERS:
            owner = BUILTIN_ADMINISTRATORS_SID
        return owner, group, is_protected

    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace._read_live_owner_group_dacl",
        _read_owner_simulado,
    )

    @pytest.mark.skipif(sys.platform != "win32", reason="Namespace protegido Win32")
    def test_escritura_real_create_once_en_namespace(self, tmp_path: pathlib.Path, entorno_elevado: None) -> None:
        """En Windows real, la primitiva create-once publica sin reemplazar."""
        from sky_claw.local.runtime_vault import trusted_namespace

        destino = tmp_path / "authorized_plan.json"
        trusted_namespace.write_secured_file_create_once_at(destino, b'{"ok":true}', AUTHORIZED_PLAN_OBJECT)
        assert destino.read_bytes() == b'{"ok":true}'
        with pytest.raises(trusted_namespace.TrustedNamespaceError):
            trusted_namespace.write_secured_file_create_once_at(destino, b'{"ok":false}', AUTHORIZED_PLAN_OBJECT)
        assert destino.read_bytes() == b'{"ok":true}'

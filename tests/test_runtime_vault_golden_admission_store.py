"""Tests del registro protegido de operación de Golden Admission (ADR 0010 §11.4).

Dos capas, sin mezclarlas:

- serialización/esquema cerrado → corre en cualquier plataforma (es puro);
- persistencia en el namespace Win32 → sólo Windows, con el espejo de elevación
  de ``TestNamespaceBootstrapWindows.mock_elevated_provisioning``
  (``tests/test_runtime_vault_trusted_namespace.py``), porque en este entorno
  ``CreateDirectoryW``/``CreateFileW`` con SD canónico fallan con 1307.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
from typing import Any

import pytest

from sky_claw.local.runtime_vault.critical_expectations import critical_expectations_digest
from sky_claw.local.runtime_vault.golden_admission import (
    GoldenAdmissionModelError,
    GoldenAdmissionOutcome,
    GoldenAdmissionReceipt,
    GoldenAdmissionRejectionReason,
    GoldenAdmissionSource,
    GoldenAdmissionState,
    GoldenAdmissionStoreError,
    issue_golden_admission_receipt,
    validate_operation_id,
)
from sky_claw.local.runtime_vault.golden_admission_store import (
    ADMISSION_RECORD_SCHEMA_VERSION,
    GoldenAdmissionObservationRecord,
    GoldenAdmissionRecord,
    GoldenAdmissionResultRecord,
    GoldenAdmissionSourceReference,
    GoldenAdmissionTgrBinding,
    append_observation,
    append_result,
    bind_tgr_replacement,
    create_admission_record,
    derive_admission_record_dir,
    derive_admission_record_path,
    deserialize_admission_record,
    load_admission_record,
    revalidate_for_tgr_replace,
    serialize_admission_record,
)
from sky_claw.local.runtime_vault.models import CriticalFileExpectation, RuntimeIdentity, TreeDigest
from sky_claw.local.runtime_vault.physical_root import PhysicalRootIdentity
from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation
from sky_claw.local.runtime_vault.trusted_registry import TrustedGoldenEntry

_OPERATION_ID = validate_operation_id("11111111-2222-4333-8444-555555555555")
_ROOT = "C:\\Games\\Skyrim Special Edition"
_SHA = "0" * 64
_SHA_OTRO = "1" * 64


def _expectativas(digest: str = _SHA) -> tuple[CriticalFileExpectation, ...]:
    return (CriticalFileExpectation(rel_path="Meshes\\x.nif", expected_digest=digest, expected_size=128),)


def _receipt(
    *,
    operation_id: str = _OPERATION_ID,
    admission_source: GoldenAdmissionSource = GoldenAdmissionSource.OPERATOR_TOFU,
    source_provenance_digest: str | None = None,
    critical_expectations: tuple[CriticalFileExpectation, ...] | None = None,
) -> GoldenAdmissionReceipt:
    criticas = _expectativas() if critical_expectations is None else critical_expectations
    return issue_golden_admission_receipt(
        operation_id=operation_id,
        canonical_root=_ROOT,
        volume_serial_number=1,
        root_file_id=2,
        tree_digest=TreeDigest(digest=_SHA, files=3, bytes=4096),
        expected_runtime=RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
        admission_source=admission_source,
        operator_sid="S-1-5-21-1-2-3-500",
        admitted_at="2026-09-25T10:00:00.000000Z",
        critical_expectations_digest=critical_expectations_digest(criticas),
        source_provenance_digest=source_provenance_digest,
    )


def _observacion(
    stage: str = "OBSERVE",
    state: str = GoldenAdmissionState.OBSERVED,
    critical_expectations: tuple[CriticalFileExpectation, ...] | None = None,
) -> GoldenAdmissionObservationRecord:
    criticas = _expectativas() if critical_expectations is None else critical_expectations
    return GoldenAdmissionObservationRecord(
        stage=stage,
        state=state,
        physical_root=PhysicalRootIdentity(canonical_root=_ROOT, volume_serial_number=1, root_file_id=2),
        observed_tree=TreeDigest(digest=_SHA, files=3, bytes=4096),
        observed_runtime=FreshRuntimeObservation(
            game_key="skyrimse",
            game_version="1.6.1170.0",
            observed_exe_path="C:\\Games\\SkyrimSE.exe",
            observed_at_ns=1_700_000_000_000_000_000,
        ),
        critical_expectations_digest=critical_expectations_digest(criticas),
        message="",
    )


def _entry(canonical_root: str = _ROOT) -> TrustedGoldenEntry:
    return TrustedGoldenEntry(
        canonical_root=canonical_root,
        volume_serial_number=1,
        root_file_id=2,
        tree_digest=TreeDigest(digest=_SHA, files=3, bytes=4096),
        policy_version="gp2-v1",
        registered_by="S-1-5-21-1-2-3-500",
        registered_at="2026-09-25T10:00:00.000000Z",
    )


def _registro(**kwargs: Any) -> GoldenAdmissionRecord:
    criticas = kwargs.pop("critical_expectations", _expectativas())
    return GoldenAdmissionRecord(
        operation_id=kwargs.pop("operation_id", _OPERATION_ID),
        receipt=kwargs.pop("receipt", _receipt(critical_expectations=criticas)),
        critical_expectations=criticas,
        **kwargs,
    )


# ============================================================================
# Serialización canónica (plataforma independiente)
# ============================================================================


class TestSerializacionCanonica:
    """Round-trip, determinismo y digests del registro."""

    def test_roundtrip_con_expectativas_no_vacias(self) -> None:
        registro = _registro(observations=(_observacion(),))
        assert deserialize_admission_record(serialize_admission_record(registro)) == registro

    def test_roundtrip_con_lista_vacia_de_expectativas(self) -> None:
        """La lista vacía se serializa como ``()`` y su digest es recomputable (§11.4)."""
        registro = _registro(critical_expectations=())
        bytes_reg = serialize_admission_record(registro)
        assert b'"critical_expectations":[]' in bytes_reg
        assert deserialize_admission_record(bytes_reg) == registro
        assert registro.critical_expectations_digest == critical_expectations_digest(())

    def test_serializacion_determinista_sin_newline_final(self) -> None:
        registro = _registro(observations=(_observacion(),))
        primero = serialize_admission_record(registro)
        segundo = serialize_admission_record(deserialize_admission_record(primero))
        assert primero == segundo
        assert not primero.endswith(b"\n")
        assert list(json.loads(primero).keys()) == sorted(json.loads(primero).keys())

    def test_binding_con_before_ausente_roundtrip(self) -> None:
        """``before=None`` es el registro de ``ABSENT`` y debe sobrevivir el round-trip."""
        registro = _registro(tgr_binding=GoldenAdmissionTgrBinding(before=None, after=_entry()))
        leido = deserialize_admission_record(serialize_admission_record(registro))
        assert leido.tgr_binding is not None
        assert leido.tgr_binding.before is None
        assert leido.tgr_before_is_absent is True

    def test_observacion_conserva_su_propio_digest_de_expectativas(self) -> None:
        otra = _observacion(critical_expectations=())
        registro = _registro(critical_expectations=(), observations=(otra,))
        leido = deserialize_admission_record(serialize_admission_record(registro))
        assert leido.observations[0].critical_expectations_digest == critical_expectations_digest(())


# ============================================================================
# Esquema cerrado (Fail-Closed en cada frontera)
# ============================================================================


class TestEsquemaCerradoRegistro:
    """Nada entra ni sale del registro fuera del contrato de §11.4."""

    @staticmethod
    def _bruto() -> dict[str, Any]:
        return json.loads(serialize_admission_record(_registro()))

    def test_clave_desconocida_en_la_raiz_es_rechazada(self) -> None:
        bruto = self._bruto()
        bruto["wal_de_recuperacion"] = True
        with pytest.raises(GoldenAdmissionStoreError, match="Clave desconocida"):
            deserialize_admission_record(json.dumps(bruto).encode("utf-8"))

    def test_clave_faltante_en_la_raiz_es_rechazada(self) -> None:
        bruto = self._bruto()
        del bruto["observations"]
        with pytest.raises(GoldenAdmissionStoreError, match="Clave obligatoria ausente"):
            deserialize_admission_record(json.dumps(bruto).encode("utf-8"))

    def test_schema_version_desconocida_es_rechazada(self) -> None:
        bruto = self._bruto()
        bruto["schema_version"] = "9.9"
        with pytest.raises(GoldenAdmissionStoreError, match="schema_version"):
            deserialize_admission_record(json.dumps(bruto).encode("utf-8"))

    def test_version_actual_del_schema(self) -> None:
        assert ADMISSION_RECORD_SCHEMA_VERSION == "1.0"

    def test_receipt_con_claves_de_mas_es_rechazado(self) -> None:
        bruto = self._bruto()
        bruto["receipt"]["nonce_extra"] = "x"
        with pytest.raises(GoldenAdmissionStoreError, match="receipt con claves inesperadas"):
            deserialize_admission_record(json.dumps(bruto).encode("utf-8"))

    def test_observacion_con_claves_de_mas_es_rechazada(self) -> None:
        bruto = self._bruto()
        bruto["observations"] = [_observation_dict()]
        bruto["observations"][0]["autoridad"] = "OBSERVATION_IS_AUTHORITY"
        with pytest.raises(GoldenAdmissionStoreError, match="observation con claves inesperadas"):
            deserialize_admission_record(json.dumps(bruto).encode("utf-8"))

    def test_stage_fuera_del_conjunto_cerrado_es_rechazado(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="no pertenece a"):
            _observacion(stage="APPLY")

    def test_state_fuera_del_conjunto_cerrado_es_rechazado(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="conjunto cerrado"):
            _observacion(state="APPROVED")

    def test_verified_no_puede_nacer_de_una_observacion_inicial(self) -> None:
        """``VERIFIED`` sólo nace de un RV-2; la primera observación es ``OBSERVED``."""
        with pytest.raises(GoldenAdmissionStoreError, match="no es admisible en el pass"):
            _observacion(stage="OBSERVE", state=GoldenAdmissionState.VERIFIED)

    def test_state_admitido_es_verified_para_el_pass_rv2(self) -> None:
        obs = _observacion(stage="RV2", state=GoldenAdmissionState.VERIFIED)
        assert obs.state == "VERIFIED"

    def test_json_que_no_es_objeto_es_rechazado(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="diccionario"):
            deserialize_admission_record(b"[1,2,3]")

    def test_bytes_invalidos_utf8_es_rechazado(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="UTF-8"):
            deserialize_admission_record(b"\xff\xfe")


def _observation_dict() -> dict[str, Any]:
    return json.loads(serialize_admission_record(_registro(observations=(_observacion(),))))["observations"][0]


class TestResultadoYFuente:
    """Reglas de contenido sobre ``result``, ``source_reference`` y el binding."""

    def test_registered_sin_binding_es_rechazado(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="tgr_binding"):
            _registro(
                result=GoldenAdmissionResultRecord(
                    outcome=GoldenAdmissionOutcome.REGISTERED,
                    registered_at="2026-09-25T10:00:01.000000Z",
                    registered_by="S-1-5-21-1-2-3-500",
                )
            )

    def test_rejected_sin_reason_es_rechazado(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="REJECTED exige reason"):
            _registro(result=GoldenAdmissionResultRecord(outcome=GoldenAdmissionOutcome.REJECTED))

    def test_registered_no_admite_reason_de_rechazo(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="REGISTERED no puede llevar reason"):
            _registro(
                tgr_binding=GoldenAdmissionTgrBinding(before=None, after=_entry()),
                result=GoldenAdmissionResultRecord(
                    outcome=GoldenAdmissionOutcome.REGISTERED,
                    reason=GoldenAdmissionRejectionReason.RV2_MISMATCH,
                    registered_at="2026-09-25T10:00:01.000000Z",
                    registered_by="S-1-5-21-1-2-3-500",
                ),
            )

    def test_rejected_no_admite_timestamps_de_registro(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="Sólo REGISTERED"):
            GoldenAdmissionResultRecord(
                outcome=GoldenAdmissionOutcome.REJECTED,
                reason=GoldenAdmissionRejectionReason.STORE_FAILED,
                registered_at="2026-09-25T10:00:01.000000Z",
                registered_by="S-1-5-21-1-2-3-500",
            )

    def test_outcome_y_reason_se_coercen_desde_strings_crudos(self) -> None:
        registro = _registro(
            tgr_binding=GoldenAdmissionTgrBinding(before=None, after=_entry()),
            result=GoldenAdmissionResultRecord(
                outcome=GoldenAdmissionOutcome.REJECTED, reason=GoldenAdmissionRejectionReason.RV2_MISMATCH
            ),
        )
        leido = deserialize_admission_record(serialize_admission_record(registro))
        assert isinstance(leido.result.outcome, GoldenAdmissionOutcome)
        assert isinstance(leido.result.reason, GoldenAdmissionRejectionReason)

    def test_commit_outcome_unknown_es_un_resultado_valido_sin_reason(self) -> None:
        resultado = GoldenAdmissionResultRecord(outcome=GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN)
        assert resultado.reason is None
        assert resultado.registered_at is None

    def test_source_reference_exige_provenance(self) -> None:
        ref = GoldenAdmissionSourceReference(kind="evidence", digest=_SHA)
        with pytest.raises(GoldenAdmissionStoreError, match="INDEPENDENT_PROVENANCE"):
            _registro(source_reference=ref)

    def test_source_reference_tomatofu_no_puede_rellenarse(self) -> None:
        """TOFU no se convierte en provenance por rellenar un campo (§11.0)."""
        ref = GoldenAdmissionSourceReference(kind="evidence", digest=_SHA)
        with pytest.raises(GoldenAdmissionStoreError, match="INDEPENDENT_PROVENANCE"):
            _registro(receipt=_receipt(admission_source=GoldenAdmissionSource.OPERATOR_TOFU), source_reference=ref)

    def test_source_reference_con_digest_que_no_coincide_es_rechazada(self) -> None:
        criticas = _expectativas()
        receipt = _receipt(
            admission_source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
            source_provenance_digest=_SHA_OTRO,
            critical_expectations=criticas,
        )
        with pytest.raises(GoldenAdmissionStoreError, match="no coincide"):
            _registro(receipt=receipt, source_reference=GoldenAdmissionSourceReference(kind="evidence", digest=_SHA))

    def test_source_reference_valida_roundtripea_en_provenance(self) -> None:
        criticas = _expectativas()
        receipt = _receipt(
            admission_source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
            source_provenance_digest=_SHA_OTRO,
            critical_expectations=criticas,
        )
        registro = _registro(
            receipt=receipt,
            source_reference=GoldenAdmissionSourceReference(kind="evidence", digest=_SHA_OTRO),
        )
        leido = deserialize_admission_record(serialize_admission_record(registro))
        assert leido.source_reference == GoldenAdmissionSourceReference(kind="evidence", digest=_SHA_OTRO)

    def test_receipt_de_otra_operacion_es_rechazado(self) -> None:
        otro = validate_operation_id("99999999-8888-4777-8666-555555555555")
        with pytest.raises(GoldenAdmissionStoreError, match="pertenece a otra operación"):
            _registro(receipt=_receipt(operation_id=otro))


# ============================================================================
# Derivación de rutas (sin syscalls privilegiados)
# ============================================================================


class TestDerivacionDeRutas:
    def test_ruta_canonica_bajo_el_resolver(self) -> None:
        base = derive_admission_record_dir(_OPERATION_ID, programdata_resolver=lambda: "C:\\ProgramData")
        assert base == pathlib.PureWindowsPath("C:\\ProgramData\\Sky-Claw\\runtime_vault\\operations\\" + _OPERATION_ID)
        assert (
            derive_admission_record_path(_OPERATION_ID, programdata_resolver=lambda: "C:\\ProgramData").name
            == "golden_admission_record.json"
        )

    def test_operation_id_invalido_es_rechazado_antes_de_tocar_paths(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            derive_admission_record_dir("..\\..\\etc", programdata_resolver=lambda: "C:\\ProgramData")


# ============================================================================
# Persistencia en el namespace Win32
# ============================================================================


@pytest.fixture
def entorno_elevado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Espejo de ``TestNamespaceBootstrapWindows.mock_elevated_provisioning``.

    Neutraliza las syscalls privilegiadas (1307/1314) para poder ejercitar la
    escritura REAL del registro en ``tmp_path``: directorios con
    ``CreateDirectoryW`` sin SD, archivos con ``CreateFileW`` sin SD y las
    verificaciones por handle que ese entorno sintético no puede satisfacer.
    """
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

    def _create_dir_sin_sd(path: str, sa: Any) -> bool:
        return bool(real_create_dir(path, None))

    monkeypatch.setattr(_kernel32, "CreateDirectoryW", _create_dir_sin_sd)

    def _open_sin_write_owner(
        path: Any, desired_access: int = _READ_CONTROL | _FILE_READ_ATTRIBUTES, **kwargs: Any
    ) -> int:
        return _open_handle_no_reparse(path, desired_access=desired_access & ~_WRITE_OWNER, **kwargs)

    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace._open_handle_no_reparse",
        _open_sin_write_owner,
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
        lambda path, obj="trusted_goldens.json": _kernel32.CreateFileW(
            str(path), 0x40000000 | 0x80000000, 0, None, 1, 0x80, None
        ),
    )
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace.verify_golden_admission_record_by_handle",
        lambda path: None,
    )
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace.verify_secured_file_by_handle",
        lambda path: None,
    )
    monkeypatch.setattr(
        "sky_claw.local.runtime_vault.trusted_namespace._verify_canonical_directory_security_on_handle",
        lambda *args, **kwargs: None,
    )

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
class TestPersistenciaProtegida:
    """Ciclo completo de una operación sobre el registro en disco real."""

    @pytest.fixture(autouse=True)
    def _namespace(self, tmp_path: pathlib.Path, entorno_elevado: None) -> None:
        padre = tmp_path / "Sky-Claw" / "runtime_vault" / "operations"
        padre.mkdir(parents=True)
        self.resolver = lambda: tmp_path  # type: ignore[assignment]

    def test_creacion_y_carga_releen_el_mismo_registro(self) -> None:
        creado = create_admission_record(
            receipt=_receipt(),
            critical_expectations=_expectativas(),
            programdata_resolver=self.resolver,
        )
        assert load_admission_record(_OPERATION_ID, programdata_resolver=self.resolver) == creado
        assert derive_admission_record_path(_OPERATION_ID, programdata_resolver=self.resolver).is_file()

    def test_claim_one_use_rechaza_el_replay_de_la_misma_operacion(self) -> None:
        create_admission_record(
            receipt=_receipt(),
            critical_expectations=_expectativas(),
            programdata_resolver=self.resolver,
        )
        with pytest.raises(GoldenAdmissionStoreError, match="one-use"):
            create_admission_record(
                receipt=_receipt(),
                critical_expectations=_expectativas(),
                programdata_resolver=self.resolver,
            )

    def test_observacion_se_persiste_y_vuelve_igual(self) -> None:
        create_admission_record(
            receipt=_receipt(),
            critical_expectations=_expectativas(),
            programdata_resolver=self.resolver,
        )
        con_obs = append_observation(_OPERATION_ID, _observacion(), programdata_resolver=self.resolver)
        assert con_obs.observations == (_observacion(),)
        assert load_admission_record(_OPERATION_ID, programdata_resolver=self.resolver) == con_obs

    def test_binding_antes_del_replace_y_revalidacion_en_discos(self) -> None:
        create_admission_record(
            receipt=_receipt(),
            critical_expectations=_expectativas(),
            programdata_resolver=self.resolver,
        )
        registrado = bind_tgr_replacement(
            _OPERATION_ID, before=None, after=_entry(), programdata_resolver=self.resolver
        )
        assert registrado.tgr_before_is_absent
        revalidado = revalidate_for_tgr_replace(_OPERATION_ID, expected=registrado, programdata_resolver=self.resolver)
        assert revalidado == registrado

    def test_revalidacion_falla_si_el_registro_en_disco_divergio(self) -> None:
        create_admission_record(
            receipt=_receipt(),
            critical_expectations=_expectativas(),
            programdata_resolver=self.resolver,
        )
        registrado = bind_tgr_replacement(
            _OPERATION_ID, before=None, after=_entry(), programdata_resolver=self.resolver
        )
        # Otro escritor altera el registro entre el persist y el replace TGR.
        otro = GoldenAdmissionRecord(
            operation_id=registrado.operation_id,
            receipt=registrado.receipt,
            critical_expectations=registrado.critical_expectations,
            source_reference=registrado.source_reference,
            observations=(_observacion(),),
            tgr_binding=registrado.tgr_binding,
        )
        from sky_claw.local.runtime_vault.golden_admission_store import _persist

        _persist(otro, programdata_resolver=self.resolver)

        with pytest.raises(GoldenAdmissionStoreError, match="divergió"):
            revalidate_for_tgr_replace(_OPERATION_ID, expected=registrado, programdata_resolver=self.resolver)

    def test_revalidacion_falla_sin_binding_before_after(self) -> None:
        creado = create_admission_record(
            receipt=_receipt(),
            critical_expectations=_expectativas(),
            programdata_resolver=self.resolver,
        )
        with pytest.raises(GoldenAdmissionStoreError, match="before/intended after"):
            revalidate_for_tgr_replace(_OPERATION_ID, expected=creado, programdata_resolver=self.resolver)

    def test_resultado_se_anexa_una_unica_vez(self) -> None:
        create_admission_record(
            receipt=_receipt(),
            critical_expectations=_expectativas(),
            programdata_resolver=self.resolver,
        )
        bind_tgr_replacement(_OPERATION_ID, before=None, after=_entry(), programdata_resolver=self.resolver)
        con_resultado = append_result(
            _OPERATION_ID,
            GoldenAdmissionResultRecord(
                outcome=GoldenAdmissionOutcome.REGISTERED,
                registered_at="2026-09-25T10:00:01.000000Z",
                registered_by="S-1-5-21-1-2-3-500",
            ),
            programdata_resolver=self.resolver,
        )
        assert load_admission_record(_OPERATION_ID, programdata_resolver=self.resolver) == con_resultado
        with pytest.raises(GoldenAdmissionStoreError, match="ya fue anexado"):
            append_result(
                _OPERATION_ID,
                GoldenAdmissionResultRecord(
                    outcome=GoldenAdmissionOutcome.REJECTED,
                    reason=GoldenAdmissionRejectionReason.UNEXPECTED_FAILURE,
                ),
                programdata_resolver=self.resolver,
            )

    def test_no_se_permite_un_binding_duplicado(self) -> None:
        create_admission_record(
            receipt=_receipt(),
            critical_expectations=_expectativas(),
            programdata_resolver=self.resolver,
        )
        bind_tgr_replacement(_OPERATION_ID, before=None, after=_entry(), programdata_resolver=self.resolver)
        with pytest.raises(GoldenAdmissionStoreError, match="ya fue registrado"):
            bind_tgr_replacement(_OPERATION_ID, before=None, after=_entry(), programdata_resolver=self.resolver)

    def test_carga_falla_cerrado_si_el_archivo_no_existe(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="No se pudo leer"):
            load_admission_record(_OPERATION_ID, programdata_resolver=self.resolver)

    def test_creacion_falla_cerrado_si_los_ancestros_no_existen(self) -> None:
        with pytest.raises(GoldenAdmissionStoreError, match="directorios de operación|ancestro|No se pudo"):
            create_admission_record(
                receipt=_receipt(operation_id=validate_operation_id("12121212-3232-4343-8444-555555555555")),
                critical_expectations=_expectativas(),
                programdata_resolver=lambda: tmp_sin_ancestros(self.resolver()),
            )


def tmp_sin_ancestros(base: Any) -> pathlib.Path:
    """Raíz de ProgramData sintética SIN el directorio ``operations`` creado."""
    raiz = pathlib.Path(base) / "ProgramDataSinAncestros"
    (raiz / "Sky-Claw" / "runtime_vault").mkdir(parents=True, exist_ok=True)
    return raiz


# ============================================================================
# Anclas enumerantes: quién puede reemplazar bytes protegidos
# ============================================================================


def _arbol(nombre_archivo: str) -> ast.AST:
    ruta = pathlib.Path(__file__).parents[1] / "sky_claw" / "local" / "runtime_vault" / nombre_archivo
    return ast.parse(ruta.read_text(encoding="utf-8"))


#: Constructores de pathlib que devuelven una ruta con su propio
#: ``Path.replace`` (el reemplazo de ARCHIVO, no el de ``str``).
_CONSTRUCTORES_PATHLIB = frozenset({"Path", "PurePath", "PureWindowsPath", "PurePosixPath", "WindowsPath", "PosixPath"})


def _es_constructor_de_pathlib(nodo: ast.AST) -> bool:
    if not isinstance(nodo, ast.Call):
        return False
    func = nodo.func
    if isinstance(func, ast.Attribute):
        return func.attr in _CONSTRUCTORES_PATHLIB
    if isinstance(func, ast.Name):
        return func.id in _CONSTRUCTORES_PATHLIB
    return False


def _es_llamada_a_os_replace(nodo: ast.AST) -> bool:
    """Detecta UN reemplazo de archivo: ``os.replace(...)`` o ``pathlib.Path(...).replace(...)``.

    No confunde ``str.replace`` (p. ej. ``x.isoformat().replace('+00:00','Z')``),
    que sobre un nodo ``ast.Call`` receptor pasaría el chequeo ingenuo. La
    precisión importa: un detector que rechaza código correcto termina
    debilitado por el siguiente que lo toque.
    """
    if not isinstance(nodo, ast.Call):
        return False
    func = nodo.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "replace":
        return False
    # os.replace(...)
    if isinstance(func.value, ast.Name) and func.value.id == "os":
        return True
    # pathlib.Path(...).replace(...)
    return _es_constructor_de_pathlib(func.value)


class TestAnclasDeEscritura:
    """Propiedad del mecanismo: el reemplazo de bytes protegidos tiene UN dueño."""

    def test_os_replace_solo_vive_en_los_dos_modulos_autorizados(self) -> None:
        runtime_vault = pathlib.Path(__file__).parents[1] / "sky_claw" / "local" / "runtime_vault"
        encontrados: set[str] = set()
        for archivo in sorted(runtime_vault.glob("*.py")):
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
            if any(_es_llamada_a_os_replace(n) for n in ast.walk(arbol)):
                encontrados.add(archivo.name)
        assert encontrados == {"clone.py", "trusted_namespace.py"}

    @pytest.mark.parametrize(
        ("codigo", "esperado"),
        [
            ("os.replace(a, b)", True),
            ("pathlib.Path(a).replace(b)", True),
            ("Path(a).replace(b)", True),
            ("x.isoformat().replace('+00:00', 'Z')", False),
            ("texto.replace('a', 'b')", False),
            ("datetime.now().isoformat().replace('+00:00', 'Z')", False),
            ("os.path.join(a, b)", False),
        ],
    )
    def test_el_detector_de_reemplazo_de_archivo_no_confunde_str_replace(self, codigo: str, esperado: bool) -> None:
        """Autochequeo del detector: si acierta mal en un sentido, se debilita solo."""
        detectado = any(_es_llamada_a_os_replace(n) for n in ast.walk(ast.parse(codigo)))
        assert detectado is esperado, codigo

    @pytest.mark.parametrize(
        "nombre", ["trusted_registry.py", "golden_admission_store.py", "golden_admission_service.py"]
    )
    def test_ningun_escritor_llama_os_replace_directamente(self, nombre: str) -> None:
        llamadas = [n for n in ast.walk(_arbol(nombre)) if _es_llamada_a_os_replace(n)]
        assert llamadas == []

    @pytest.mark.parametrize(
        "nombre", ["trusted_registry.py", "golden_admission_store.py", "golden_admission_service.py"]
    )
    def test_ningun_escritor_importa_el_simbolo_reemplazante(self, nombre: str) -> None:
        """Sin ``from os import replace``: el reemplazo se alcanza sólo por la primitiva única."""
        importados: set[str] = set()
        for nodo in ast.walk(_arbol(nombre)):
            if isinstance(nodo, ast.ImportFrom) and nodo.module in {"os", "pathlib"}:
                importados.update(aliased.name for aliased in nodo.names)
        assert importados.isdisjoint({"replace", "rename"})

    def test_el_servicio_no_tiene_un_camino_de_escritura_propio(self) -> None:
        """El servicio alcanza el disco sólo por el store: ni una primitiva suya.

        Ancla de UN dueño sobre el tercer módulo de GP2-P3: si el servicio
        empezara a escribir por su cuenta, tendría dos caminos de persistencia
        con garantías distintas (el defecto dominante del repo).
        """
        arbol = _arbol("golden_admission_service.py")
        delegaciones = [
            n
            for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"write_secured_file_atomically_at", "create_secure_directory_exclusive"}
        ]
        assert delegaciones == []
        aperturas = [
            n
            for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"write_bytes", "write_text", "open"}
        ]
        assert aperturas == []

    @pytest.mark.parametrize("nombre", ["trusted_registry.py", "golden_admission_store.py"])
    def test_los_dos_escritores_delegan_en_write_secured_file_atomically_at(self, nombre: str) -> None:
        delegaciones = [
            n
            for n in ast.walk(_arbol(nombre))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "write_secured_file_atomically_at"
        ]
        assert len(delegaciones) == 1, f"{nombre} debe tener UNA sola delegación, no {len(delegaciones)}"

    def test_el_store_no_tiene_un_segundo_camino_de_escritura(self) -> None:
        """Toda escritura del registro pasa por ``_persist``: ni una apertura de archivo propia."""
        arbol = _arbol("golden_admission_store.py")
        aperturas = [
            n
            for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"write_bytes", "write_text", "open"}
        ]
        assert aperturas == []

    def test_el_store_no_escribe_el_tgr(self) -> None:
        """El store audita; escribir el TGR le pertenece al servicio (§11.4)."""
        llamadas = [
            n.func.id
            for n in ast.walk(_arbol("golden_admission_store.py"))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        prohibidas = {
            "_write_trusted_registry_atomically_at",
            "mutate_trusted_golden_registry",
            "write_trusted_registry_atomically",
        }
        assert prohibidas.isdisjoint(llamadas)

"""DTOs de Golden Admission: fuentes cerradas, receipt helper-issued y confirmación.

Ancla los invariantes de ADR 0010 §11.4 que NO son sólo documentales:

- el conjunto de fuentes es cerrado y exacto (§11.0);
- una observación no puede auto-admitirse ni auto-producir VERIFIED (RVO-02);
- el receipt tiene exactamente sus 13 campos normativos y su nonce es CSPRNG
  helper-issued, jamás suministrado por staging;
- ``policy_version`` sólo puede ser la policy ACTIVA del helper;
- ``source_provenance_digest`` obligatorio en provenance y ``None`` en TOFU;
- la confirmación es un DTO distinto de ``PrivilegedPlanConfirmation`` y sin
  provider por defecto (fail-closed).
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from sky_claw.local.runtime_vault.critical_expectations import critical_expectations_digest
from sky_claw.local.runtime_vault.golden_admission import (
    ACTIVE_HELPER_POLICY_VERSION,
    GOLDEN_ADMISSION_CONFIRMATION_FIELDS,
    GOLDEN_ADMISSION_RECEIPT_FIELDS,
    OPERATOR_TOFU_WARNING,
    GoldenAdmissionConfirmation,
    GoldenAdmissionConfirmationReceipt,
    GoldenAdmissionConfirmationResult,
    GoldenAdmissionConfirmationStage,
    GoldenAdmissionExpectation,
    GoldenAdmissionModelError,
    GoldenAdmissionObservation,
    GoldenAdmissionReceipt,
    GoldenAdmissionRejectedError,
    GoldenAdmissionRequestError,
    GoldenAdmissionSource,
    GoldenAdmissionSourceError,
    GoldenAdmissionState,
    issue_golden_admission_receipt,
    require_golden_admission_outcome,
)
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    RuntimeIdentity,
    TreeDigest,
)
from sky_claw.local.runtime_vault.physical_root import PhysicalRootIdentity
from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation

_OP_ID = "123e4567-e89b-42d3-a456-426614174000"
_ROOT = "C:\\Games\\Skyrim Special Edition"
_TREE = TreeDigest(digest="a" * 64, files=10, bytes=1024)
_RUNTIME = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
_FRESH = FreshRuntimeObservation(
    game_key="skyrimse",
    game_version="1.6.1170.0",
    observed_exe_path="C:\\Games\\Skyrim Special Edition\\SkyrimSE.exe",
    observed_at_ns=1_700_000_000_000_000_000,
)
_PHYSICAL = PhysicalRootIdentity(canonical_root=_ROOT, volume_serial_number=12345, root_file_id=67890)
_SID = "S-1-5-21-1-2-3-1001"
_TS = "2026-09-25T12:00:00.123456Z"
_PROVENANCE = "b" * 64


def _receipt_kwargs(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "operation_id": _OP_ID,
        "canonical_root": _ROOT,
        "volume_serial_number": 12345,
        "root_file_id": 67890,
        "tree_digest": _TREE,
        "expected_runtime": _RUNTIME,
        "policy_version": ACTIVE_HELPER_POLICY_VERSION,
        "admission_source": GoldenAdmissionSource.OPERATOR_TOFU,
        "operator_sid": _SID,
        "admitted_at": _TS,
        "critical_expectations_digest": critical_expectations_digest(()),
        "source_provenance_digest": None,
        "nonce": "c" * 64,
    }
    base.update(extra)
    return base


def _observation() -> GoldenAdmissionObservation:
    return GoldenAdmissionObservation(
        state=GoldenAdmissionState.OBSERVED,
        physical_root=_PHYSICAL,
        observed_tree=_TREE,
        observed_runtime=_FRESH,
    )


class TestFuentesCerradas:
    def test_conjunto_de_fuentes_exacto_y_cerrado(self) -> None:
        assert set(GoldenAdmissionSource) == {"INDEPENDENT_PROVENANCE", "OPERATOR_TOFU"}

    def test_source_desconocido_se_rechaza(self) -> None:
        with pytest.raises(ValueError):
            GoldenAdmissionSource("MANUAL_UPLOAD")  # type: ignore[arg-type]

    def test_validador_de_solicitud_traduce_el_source_desconocido(self) -> None:
        from sky_claw.local.runtime_vault.golden_admission import _validate_source

        with pytest.raises(GoldenAdmissionSourceError):
            _validate_source("MANUAL_UPLOAD")

    def test_receipt_con_source_fuera_del_conjunto_falla(self) -> None:
        with pytest.raises(GoldenAdmissionSourceError):
            GoldenAdmissionReceipt(**_receipt_kwargs(admission_source="OTRA_FUENTE"))  # type: ignore[arg-type]

    def test_source_error_es_un_error_de_solicitud(self) -> None:
        assert issubclass(GoldenAdmissionSourceError, GoldenAdmissionRequestError)


class TestEstadosDelWorkflow:
    def test_estados_exactos_sin_transiciones_del_fsm_gp2(self) -> None:
        assert set(GoldenAdmissionState) == {"OBSERVED", "ADMITTED", "VERIFIED", "REJECTED"}

    def test_observacion_no_puede_ser_admitida_ni_verificada(self) -> None:
        for estado in (GoldenAdmissionState.ADMITTED, GoldenAdmissionState.VERIFIED, GoldenAdmissionState.REJECTED):
            with pytest.raises(GoldenAdmissionModelError):
                GoldenAdmissionObservation(
                    state=estado,
                    physical_root=_PHYSICAL,
                    observed_tree=_TREE,
                    observed_runtime=_FRESH,
                )

    def test_observacion_no_transporta_expected_tree(self) -> None:
        nombres = {f.name for f in dataclasses.fields(GoldenAdmissionObservation)}
        assert "expected_tree" not in nombres
        assert "expected_runtime" not in nombres

    def test_expectativa_no_puede_ser_observada(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionExpectation(
                state=GoldenAdmissionState.OBSERVED,
                source=GoldenAdmissionSource.OPERATOR_TOFU,
                expected_tree=_TREE,
                expected_runtime=_RUNTIME,
            )


class TestExpectativaAdmitida:
    def test_digest_de_critical_expectations_se_deriva_de_la_lista(self) -> None:
        esperadas = (CriticalFileExpectation(rel_path="Data/a.esm", expected_digest="d" * 64, expected_size=1),)
        expectativa = GoldenAdmissionExpectation(
            state=GoldenAdmissionState.ADMITTED,
            source=GoldenAdmissionSource.OPERATOR_TOFU,
            expected_tree=_TREE,
            expected_runtime=_RUNTIME,
            critical_expectations=esperadas,
        )
        assert expectativa.critical_expectations_digest == critical_expectations_digest(esperadas)

    def test_no_existe_forma_de_suministrar_un_digest_de_lista_aparte(self) -> None:
        nombres = {f.name for f in dataclasses.fields(GoldenAdmissionExpectation)}
        assert "critical_expectations_digest" in nombres
        # El campo existe pero no es constructor: el caller no lo puede elegir.
        assert not next(
            f for f in dataclasses.fields(GoldenAdmissionExpectation) if f.name == "critical_expectations_digest"
        ).init

    def test_lista_vacia_sigue_siendo_valida_y_digest_presente(self) -> None:
        expectativa = GoldenAdmissionExpectation(
            state=GoldenAdmissionState.ADMITTED,
            source=GoldenAdmissionSource.OPERATOR_TOFU,
            expected_tree=_TREE,
            expected_runtime=_RUNTIME,
            critical_expectations=(),
        )
        assert expectativa.critical_expectations_digest == critical_expectations_digest(())

    def test_provenance_exige_source_provenance_digest(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionExpectation(
                state=GoldenAdmissionState.ADMITTED,
                source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
                expected_tree=_TREE,
                expected_runtime=_RUNTIME,
                source_provenance_digest=None,
            )

    def test_tofu_no_puede_declarar_provenance_rellenando_el_campo(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionExpectation(
                state=GoldenAdmissionState.ADMITTED,
                source=GoldenAdmissionSource.OPERATOR_TOFU,
                expected_tree=_TREE,
                expected_runtime=_RUNTIME,
                source_provenance_digest=_PROVENANCE,
            )


class TestReceipt:
    def test_receipt_tiene_exactamente_los_13_campos_normativos(self) -> None:
        nombres = tuple(f.name for f in dataclasses.fields(GoldenAdmissionReceipt))
        assert nombres == GOLDEN_ADMISSION_RECEIPT_FIELDS
        assert len(nombres) == 13

    def test_receipt_tofu_es_valido(self) -> None:
        receipt = GoldenAdmissionReceipt(**_receipt_kwargs())
        assert receipt.admission_source is GoldenAdmissionSource.OPERATOR_TOFU
        assert receipt.source_provenance_digest is None

    def test_receipt_provenance_exige_digest_de_fuente(self) -> None:
        receipt = GoldenAdmissionReceipt(
            **_receipt_kwargs(
                admission_source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
                source_provenance_digest=_PROVENANCE,
            )
        )
        assert receipt.source_provenance_digest == _PROVENANCE

    def test_receipt_provenance_sin_digest_falla(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionReceipt(
                **_receipt_kwargs(
                    admission_source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
                    source_provenance_digest=None,
                )
            )

    def test_policy_version_debe_ser_la_activa_del_helper(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionReceipt(**_receipt_kwargs(policy_version="staging-v9"))

    def test_policy_version_activa_es_gp2_v1(self) -> None:
        assert ACTIVE_HELPER_POLICY_VERSION == "gp2-v1"

    def test_operator_sid_debe_ser_sid_canonico(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionReceipt(**_receipt_kwargs(operator_sid="Administradores"))

    def test_admitted_at_debe_ser_iso8601_utc_z(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionReceipt(**_receipt_kwargs(admitted_at="2026-09-25 12:00:00"))

    def test_operation_id_debe_ser_uuid_canonico(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionReceipt(**_receipt_kwargs(operation_id=_OP_ID.upper()))

    def test_nonce_corto_se_rechaza(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            GoldenAdmissionReceipt(**_receipt_kwargs(nonce="abc123"))


class TestEmisionDelReceipt:
    def test_el_nonce_lo_genera_el_helper_y_no_el_caller(self) -> None:
        kwargs = _receipt_kwargs()
        kwargs.pop("nonce")
        kwargs["source_provenance_digest"] = None
        a = issue_golden_admission_receipt(**kwargs)  # type: ignore[arg-type]
        b = issue_golden_admission_receipt(**kwargs)  # type: ignore[arg-type]
        assert a.nonce != b.nonce
        assert len(a.nonce) == 64
        assert all(c in "0123456789abcdef" for c in a.nonce)

    def test_la_emision_no_acepta_un_nonce_del_caller(self) -> None:
        kwargs = _receipt_kwargs()
        kwargs["nonce"] = "d" * 64
        with pytest.raises(TypeError):
            issue_golden_admission_receipt(**kwargs)  # type: ignore[arg-type]

    def test_receipt_es_inmutable(self) -> None:
        receipt = GoldenAdmissionReceipt(**_receipt_kwargs())
        with pytest.raises(dataclasses.FrozenInstanceError):
            receipt.nonce = "d" * 64  # type: ignore[misc]


class TestConfirmationDTO:
    def test_es_un_dto_distinto_de_privileged_plan_confirmation(self) -> None:
        from sky_claw.local.runtime_vault.ppsc import PPSC_NORMATIVE_FIELDS

        nombres = GOLDEN_ADMISSION_CONFIRMATION_FIELDS
        assert nombres != PPSC_NORMATIVE_FIELDS
        assert not set(nombres) >= set(PPSC_NORMATIVE_FIELDS)

    def test_campos_del_payload_coinciden_con_la_lista_normativa(self) -> None:
        nombres = tuple(f.name for f in dataclasses.fields(GoldenAdmissionConfirmation))
        assert nombres == GOLDEN_ADMISSION_CONFIRMATION_FIELDS

    def _confirmacion(self, **extra: object) -> GoldenAdmissionConfirmation:
        base: dict[str, object] = {
            "operation_id": _OP_ID,
            "canonical_root": _ROOT,
            "volume_serial_number": 12345,
            "root_file_id": 67890,
            "proposed_tree": _TREE,
            "proposed_runtime": _RUNTIME,
            "policy_version": ACTIVE_HELPER_POLICY_VERSION,
            "previous_tgr_tree_digest": None,
            "admission_source": GoldenAdmissionSource.OPERATOR_TOFU,
            "critical_expectations": (),
            "stage": GoldenAdmissionConfirmationStage.TOFU_PRE_RERUN,
        }
        base.update(extra)
        return GoldenAdmissionConfirmation(**base)  # type: ignore[arg-type]

    def test_tofu_muestra_advertencia_literal(self) -> None:
        assert self._confirmacion().tofu_warning == OPERATOR_TOFU_WARNING
        assert OPERATOR_TOFU_WARNING == "OPERATOR_TOFU DOES NOT DETECT PRE-EXISTING COMPROMISE"

    def test_provenance_no_muestra_advertencia_tofu(self) -> None:
        confirmacion = self._confirmacion(
            admission_source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
            previous_tgr_tree_digest="e" * 64,
            stage=GoldenAdmissionConfirmationStage.PROVENANCE_POST_VERIFIED,
        )
        assert confirmacion.tofu_warning == ""

    def test_ausencia_de_entrada_previa_se_muestra_como_absent(self) -> None:
        assert self._confirmacion().previous_tgr_display == "ABSENT"
        assert self._confirmacion().expected_display.startswith("ABSENT -> ")

    def test_digest_de_lista_presente_incluso_cuando_es_vacia(self) -> None:
        assert self._confirmacion().critical_expectations_digest == critical_expectations_digest(())

    def test_stage_tofu_exige_fuente_tofu(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            self._confirmacion(
                admission_source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
                stage=GoldenAdmissionConfirmationStage.TOFU_PRE_RERUN,
            )

    def test_stage_provenance_exige_fuente_provenance(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            self._confirmacion(
                admission_source=GoldenAdmissionSource.OPERATOR_TOFU,
                stage=GoldenAdmissionConfirmationStage.PROVENANCE_POST_VERIFIED,
            )

    def test_stage_source_acoplados_por_construccion(self) -> None:
        with pytest.raises(GoldenAdmissionModelError):
            self._confirmacion(
                admission_source=GoldenAdmissionSource.OPERATOR_TOFU,
                stage=GoldenAdmissionConfirmationStage.PROVENANCE_POST_VERIFIED,
            )


class _ProviderConfirmado:
    def __init__(self) -> None:
        self.visto: list[GoldenAdmissionConfirmation] = []

    def request_confirmation(self, payload: GoldenAdmissionConfirmation) -> GoldenAdmissionConfirmationResult:
        self.visto.append(payload)
        return GoldenAdmissionConfirmationResult.CONFIRMED


class _ProviderRechazado:
    def request_confirmation(self, payload: GoldenAdmissionConfirmation) -> GoldenAdmissionConfirmationResult:
        return GoldenAdmissionConfirmationResult.REJECTED


class _ProviderExotico:
    def request_confirmation(self, payload: GoldenAdmissionConfirmation) -> object:
        return True


class TestConfirmacionProvider:
    def _payload(self) -> GoldenAdmissionConfirmation:
        return GoldenAdmissionConfirmation(
            operation_id=_OP_ID,
            canonical_root=_ROOT,
            volume_serial_number=12345,
            root_file_id=67890,
            proposed_tree=_TREE,
            proposed_runtime=_RUNTIME,
            policy_version=ACTIVE_HELPER_POLICY_VERSION,
            previous_tgr_tree_digest=None,
            admission_source=GoldenAdmissionSource.OPERATOR_TOFU,
            critical_expectations=(),
            stage=GoldenAdmissionConfirmationStage.TOFU_PRE_RERUN,
        )

    def test_sin_provider_es_rechazado(self) -> None:
        with pytest.raises(GoldenAdmissionRejectedError):
            require_golden_admission_outcome(None, self._payload())

    def test_provider_que_rechaza_produce_error_tipado(self) -> None:
        with pytest.raises(GoldenAdmissionRejectedError):
            require_golden_admission_outcome(_ProviderRechazado(), self._payload())

    def test_valor_exotico_del_provider_no_es_confirmacion(self) -> None:
        with pytest.raises(GoldenAdmissionRejectedError):
            require_golden_admission_outcome(_ProviderExotico(), self._payload())  # type: ignore[arg-type]

    def test_confirmado_produce_recibo(self) -> None:
        provider = _ProviderConfirmado()
        receipt = require_golden_admission_outcome(provider, self._payload())
        assert isinstance(receipt, GoldenAdmissionConfirmationReceipt)
        assert provider.visto[0] is receipt.payload

    def test_recibo_de_no_confirmado_no_puede_construirse(self) -> None:
        with pytest.raises(GoldenAdmissionRejectedError):
            GoldenAdmissionConfirmationReceipt(
                payload=self._payload(), result=GoldenAdmissionConfirmationResult.REJECTED
            )

    def test_recibo_con_resultado_no_contractual_no_puede_construirse(self) -> None:
        with pytest.raises(GoldenAdmissionRejectedError):
            GoldenAdmissionConfirmationReceipt(payload=self._payload(), result="confirmed")  # type: ignore[arg-type]


class TestObservacion:
    def test_observacion_es_inmutable(self) -> None:
        obs = _observation()
        with pytest.raises(dataclasses.FrozenInstanceError):
            obs.message = "x"  # type: ignore[misc]

    def test_observacion_liga_identidad_fisica_y_runtime_fresco(self) -> None:
        obs = _observation()
        assert obs.physical_root == _PHYSICAL
        assert obs.observed_runtime is _FRESH

    def test_operation_id_de_un_receipt_es_el_de_la_operacion(self) -> None:
        receipt = GoldenAdmissionReceipt(**_receipt_kwargs())
        assert receipt.operation_id == str(uuid.UUID(_OP_ID))

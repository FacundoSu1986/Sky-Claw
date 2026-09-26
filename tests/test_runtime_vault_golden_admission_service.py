"""Tests del servicio ``REGISTER_OR_REFRESH_TRUSTED_GOLDEN`` (ADR 0010 §11.4).

Dos capas, sin mezclarlas:

- contrato de entrada/salida y mapeo de fallas → son puros, corren en cualquier
  plataforma y no tocan disco;
- el flujo completo (observe → confirmación → receipt → RV-2 → commit) → sólo
  Windows, porque la autoridad vive en el namespace Win32 y en el writer real.

El flujo completo usa un bridge falso y un provider de confirmación falso, pero
NUNCA la lógica del servicio: se reemplazan los puertos, no el camino. El token
es un stub sólo de ``evidence`` (la adquisición real del token tiene sus propios
tests en ``test_runtime_vault_operator_verifier.py``).
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import pytest

from sky_claw.local.runtime_vault.golden_admission import (
    GoldenAdmissionConfirmationResult,
    GoldenAdmissionConfirmationStage,
    GoldenAdmissionExpectation,
    GoldenAdmissionOutcome,
    GoldenAdmissionRejectionReason,
    GoldenAdmissionRequestError,
    GoldenAdmissionSource,
    GoldenAdmissionSourceError,
    GoldenAdmissionSourceUnavailableError,
    GoldenAdmissionState,
    GoldenAdmissionStoreError,
    validate_operation_id,
)
from sky_claw.local.runtime_vault.golden_admission_service import (
    GoldenAdmissionService,
    GoldenAdmissionServiceError,
    IndependentProvenanceProvider,
    RegisterOrRefreshTrustedGoldenRequest,
    RegisterOrRefreshTrustedGoldenResult,
    _FalloDeFlujoError,
    validate_golden_admission_request,
)
from sky_claw.local.runtime_vault.golden_admission_store import (
    GoldenAdmissionRecord,
    derive_admission_record_path,
    load_admission_record,
)
from sky_claw.local.runtime_vault.models import (
    CriticalFileExpectation,
    RuntimeIdentity,
    TreeDigest,
)
from sky_claw.local.runtime_vault.operator_token import OperatorTokenEvidence
from sky_claw.local.runtime_vault.physical_root import PhysicalRootIdentity, derive_physical_root
from sky_claw.local.runtime_vault.runtime_observation import FreshRuntimeObservation
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenEntry,
    TrustedGoldenRegistry,
    TrustedRegistryError,
    load_trusted_golden_registry,
)
from sky_claw.local.runtime_vault.trusted_registry_lock import (
    TrustedRegistryLockBusyError,
    TrustedRegistryLockError,
)
from sky_claw.local.runtime_vault.verification import (
    RuntimeVerificationResult,
    TreeVerificationResult,
    VerificationState,
)
from tests.test_runtime_vault_golden_admission_store import entorno_elevado  # noqa: F401 — fixture compartida

_OPERACION = validate_operation_id("11111111-2222-4333-8444-555555555555")
_SHA = "0" * 64
_SHA_OTRO = "1" * 64
_SID = "S-1-5-21-1-2-3-500"


def _expectativas(digest: str = _SHA) -> tuple[CriticalFileExpectation, ...]:
    return (CriticalFileExpectation(rel_path="Meshes\\x.nif", expected_digest=digest, expected_size=128),)


class _TokenFalso:
    """Token mínimo con la única propiedad que el servicio lee: ``evidence``."""

    def __init__(self, sid: str = _SID) -> None:
        self.evidence = OperatorTokenEvidence(
            operator_sid=sid,
            token_type="primary",
            acquired_via="same_account_coordinator_extraction",
        )


# ============================================================================
# Capa pura: validación de la solicitud (cero efectos secundarios)
# ============================================================================


class TestValidacionDeSolicitud:
    def test_tofu_no_admite_expectativa_del_caller(self) -> None:
        base = dict(operation_id=_OPERACION, root="C:\\Juego", admission_source=GoldenAdmissionSource.OPERATOR_TOFU)
        arbol = TreeDigest(digest=_SHA, files=1, bytes=1)
        runtime = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        with pytest.raises(GoldenAdmissionRequestError, match="no admite expectativa"):
            validate_golden_admission_request(RegisterOrRefreshTrustedGoldenRequest(**base, expected_tree=arbol))
        with pytest.raises(GoldenAdmissionRequestError, match="no admite expectativa"):
            validate_golden_admission_request(RegisterOrRefreshTrustedGoldenRequest(**base, expected_runtime=runtime))

    def test_tofu_no_admite_source_reference(self) -> None:
        from sky_claw.local.runtime_vault.golden_admission_store import GoldenAdmissionSourceReference

        with pytest.raises(GoldenAdmissionRequestError, match="no admite source_reference"):
            validate_golden_admission_request(
                RegisterOrRefreshTrustedGoldenRequest(
                    operation_id=_OPERACION,
                    root="C:\\Juego",
                    admission_source=GoldenAdmissionSource.OPERATOR_TOFU,
                    source_reference=GoldenAdmissionSourceReference(kind="bundle", digest=_SHA),
                )
            )

    @pytest.mark.parametrize("faltante", ["expected_tree", "expected_runtime", "source_reference"])
    def test_provenance_exige_cobertura_completa_de_la_fuente(self, faltante: str) -> None:
        from sky_claw.local.runtime_vault.golden_admission_store import GoldenAdmissionSourceReference

        completo: dict[str, Any] = {
            "expected_tree": TreeDigest(digest=_SHA, files=1, bytes=1),
            "expected_runtime": RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
            "source_reference": GoldenAdmissionSourceReference(kind="bundle", digest=_SHA),
        }
        completo.pop(faltante)
        with pytest.raises(GoldenAdmissionRequestError, match="INDEPENDENT_PROVENANCE"):
            validate_golden_admission_request(
                RegisterOrRefreshTrustedGoldenRequest(
                    operation_id=_OPERACION,
                    root="C:\\Juego",
                    admission_source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
                    critical_expectations=_expectativas(),
                    **completo,
                )
            )

    @pytest.mark.parametrize("operation_id", ["", "no-es-uuid", 42, None, "11111111-2222-4333-8444-5555"])
    def test_operation_id_debe_ser_un_uuid_canonico(self, operation_id: Any) -> None:
        with pytest.raises(GoldenAdmissionRequestError, match="operation_id"):
            validate_golden_admission_request(
                RegisterOrRefreshTrustedGoldenRequest(
                    operation_id=operation_id,
                    root="C:\\Juego",
                    admission_source=GoldenAdmissionSource.OPERATOR_TOFU,
                )
            )

    @pytest.mark.parametrize("root", ["", "   ", "Juego\\relativo", "C:\\Juego\x00"])
    def test_root_debe_ser_absoluto_sin_nul(self, root: str) -> None:
        with pytest.raises(GoldenAdmissionRequestError, match="root"):
            validate_golden_admission_request(
                RegisterOrRefreshTrustedGoldenRequest(
                    operation_id=_OPERACION,
                    root=root,
                    admission_source=GoldenAdmissionSource.OPERATOR_TOFU,
                )
            )

    @pytest.mark.parametrize("fuente", ["", "STAGING", "PROVENANCE", None, 7])
    def test_fuente_fuera_del_conjunto_cerrado(self, fuente: Any) -> None:
        with pytest.raises(GoldenAdmissionRequestError, match="admission_source"):
            validate_golden_admission_request(
                RegisterOrRefreshTrustedGoldenRequest(
                    operation_id=_OPERACION,
                    root="C:\\Juego",
                    admission_source=fuente,
                )
            )

    def test_critical_expectations_malformadas_se_rechazan_en_la_frontera(self) -> None:
        with pytest.raises(GoldenAdmissionRequestError, match="critical_expectations"):
            validate_golden_admission_request(
                RegisterOrRefreshTrustedGoldenRequest(
                    operation_id=_OPERACION,
                    root="C:\\Juego",
                    admission_source=GoldenAdmissionSource.OPERATOR_TOFU,
                    critical_expectations=("no soy un modelo",),
                )
            )

    def test_solicitud_invalida_no_toca_disco_ni_llama_al_bridge(self, tmp_path: pathlib.Path) -> None:
        """Fail-closed total: ni record dir, ni TGR, ni un solo pass del bridge."""
        llamadas: list[str] = []

        class _BridgeQueNoDebeCorrer:
            def invoke_observe(self, *args: Any, **kwargs: Any) -> Any:
                llamadas.append("observe")
                raise AssertionError("no debe observar")

            def invoke_verify(self, *args: Any, **kwargs: Any) -> Any:
                llamadas.append("verify")
                raise AssertionError("no debe verificar")

        servicio = GoldenAdmissionService(
            bridge=_BridgeQueNoDebeCorrer(),
            confirmation=None,
            token_provider=lambda: _TokenFalso(),
            programdata_resolver=lambda: tmp_path,
        )
        resultado = servicio.register_or_refresh_trusted_golden(
            RegisterOrRefreshTrustedGoldenRequest(
                operation_id="no-es-uuid",
                root=str(tmp_path),
                admission_source=GoldenAdmissionSource.OPERATOR_TOFU,
            )
        )
        assert resultado.success is False
        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is GoldenAdmissionRejectionReason.REQUEST_INVALID
        assert resultado.record_path is None
        assert resultado.tgr_entry is None
        assert llamadas == []
        assert list(tmp_path.iterdir()) == []


# ============================================================================
# Capa pura: contrato del resultado
# ============================================================================


class TestContratoDeResultado:
    def _entry(self) -> TrustedGoldenEntry:
        return TrustedGoldenEntry(
            canonical_root="C:\\Juego",
            volume_serial_number=1,
            root_file_id=2,
            tree_digest=TreeDigest(digest=_SHA, files=3, bytes=4096),
            policy_version="gp2-v1",
            registered_by=_SID,
            registered_at="2026-09-25T10:00:00.000000Z",
        )

    def test_registered_exige_entrada_y_no_lleva_reason(self) -> None:
        with pytest.raises(GoldenAdmissionServiceError, match="entrada TGR"):
            RegisterOrRefreshTrustedGoldenResult(
                success=True,
                message="",
                outcome=GoldenAdmissionOutcome.REGISTERED,
                operation_id=_OPERACION,
            )
        with pytest.raises(GoldenAdmissionServiceError, match="no lleva reason"):
            RegisterOrRefreshTrustedGoldenResult(
                success=True,
                message="",
                outcome=GoldenAdmissionOutcome.REGISTERED,
                operation_id=_OPERACION,
                reason=GoldenAdmissionRejectionReason.RV2_MISMATCH,
                tgr_entry=self._entry(),
            )

    def test_rejected_exige_reason_tipado_y_sin_entrada(self) -> None:
        with pytest.raises(GoldenAdmissionServiceError, match="reason tipado"):
            RegisterOrRefreshTrustedGoldenResult(
                success=False,
                message="x",
                outcome=GoldenAdmissionOutcome.REJECTED,
                operation_id=_OPERACION,
            )
        with pytest.raises(GoldenAdmissionServiceError, match="Sólo REGISTERED"):
            RegisterOrRefreshTrustedGoldenResult(
                success=False,
                message="x",
                outcome=GoldenAdmissionOutcome.REJECTED,
                operation_id=_OPERACION,
                reason=GoldenAdmissionRejectionReason.RV2_MISMATCH,
                tgr_entry=self._entry(),
            )

    def test_success_es_true_exactamente_para_registered(self) -> None:
        with pytest.raises(GoldenAdmissionServiceError, match="exactamente para REGISTERED"):
            RegisterOrRefreshTrustedGoldenResult(
                success=True,
                message="x",
                outcome=GoldenAdmissionOutcome.REJECTED,
                operation_id=_OPERACION,
                reason=GoldenAdmissionRejectionReason.RV2_MISMATCH,
            )

    def test_commit_outcome_unknown_no_lleva_reason_ni_entrada(self) -> None:
        with pytest.raises(GoldenAdmissionServiceError, match="Sólo REJECTED"):
            RegisterOrRefreshTrustedGoldenResult(
                success=False,
                message="x",
                outcome=GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN,
                operation_id=_OPERACION,
                reason=GoldenAdmissionRejectionReason.STORE_FAILED,
            )
        with pytest.raises(GoldenAdmissionServiceError, match="Sólo REGISTERED"):
            RegisterOrRefreshTrustedGoldenResult(
                success=False,
                message="x",
                outcome=GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN,
                operation_id=_OPERACION,
                tgr_entry=self._entry(),
            )


# ============================================================================
# Capa pura: helpers internos
# ============================================================================


def _cadena_de_marcas(base: str, cantidad: int) -> list[str]:
    from sky_claw.local.runtime_vault.golden_admission_service import _sumar_microsegundo

    marcas = []
    actual = base
    for _ in range(cantidad):
        actual = _sumar_microsegundo(actual)
        marcas.append(actual)
    return marcas


class TestHelpersInternos:
    def test_registered_at_unico_salta_el_valor_ya_usado_un_microsegundo(self) -> None:
        from sky_claw.local.runtime_vault.golden_admission_service import _registered_at_unico

        fisica = PhysicalRootIdentity(canonical_root="C:\\Juego", volume_serial_number=1, root_file_id=2)
        anterior = TrustedGoldenEntry(
            canonical_root="C:\\Juego",
            volume_serial_number=1,
            root_file_id=2,
            tree_digest=TreeDigest(digest=_SHA_OTRO, files=1, bytes=1),
            policy_version="gp2-v1",
            registered_by="S-1-5-21-9-9-9-500",
            registered_at="2026-09-25T10:00:00.000000Z",
        )
        registro = TrustedGoldenRegistry(entries=(anterior,), schema_version="1.0")

        assert _registered_at_unico(registro, fisica, "2026-09-25T10:00:00.000000Z") == "2026-09-25T10:00:00.000001Z"
        # Otra identidad física no colisiona: no debe saltar nada.
        otra = PhysicalRootIdentity(canonical_root="C:\\Otro", volume_serial_number=9, root_file_id=9)
        assert _registered_at_unico(registro, otra, "2026-09-25T10:00:00.000000Z") == ("2026-09-25T10:00:00.000000Z")

    def test_registered_at_unico_falla_cerrado_cuando_se_agota(self) -> None:
        from sky_claw.local.runtime_vault.golden_admission_service import (
            MAX_INTENTOS_REGISTERED_AT,
            _registered_at_unico,
        )

        class _RegistroSoloEntradas:
            """Sustituto mínimo: sólo ``entries``, que es todo lo que se lee acá.

            Un ``TrustedGoldenRegistry`` válido NO puede llegar a esta rama: su
            unicidad de identidad física admite UNA marca por root, así que
            ``usados`` nunca supera 1. El límite es una cota de trabajo y se
            ejercita acá sobre el sustituto, no sobre un registro fabulado con
            entradas duplicadas (que el validador rechazaría).
            """

            def __init__(self, entradas: tuple[TrustedGoldenEntry, ...]) -> None:
                self.entries = entradas

        fisica = PhysicalRootIdentity(canonical_root="C:\\Juego", volume_serial_number=1, root_file_id=2)
        base = "2026-09-25T10:00:00.000000Z"
        # Congela el límite: si crece o se acorta, cambia la garantía §11.4.
        assert MAX_INTENTOS_REGISTERED_AT == 1000

        def entrada_con(marca: str) -> TrustedGoldenEntry:
            return TrustedGoldenEntry(
                canonical_root="C:\\Juego",
                volume_serial_number=1,
                root_file_id=2,
                tree_digest=TreeDigest(digest=_SHA_OTRO, files=1, bytes=1),
                policy_version="gp2-v1",
                registered_by=_SID,
                registered_at=marca,
            )

        # Registro real: una sola marca ocupada, la base queda libre.
        libre = TrustedGoldenRegistry(
            entries=(entrada_con("2026-09-25T09:00:00.000000Z"),),
            schema_version="1.0",
        )
        assert _registered_at_unico(libre, fisica, base) == base

        # La identidad física ocupa la base y los MAX intentos siguientes.
        ocupadas = (base, *_cadena_de_marcas(base, MAX_INTENTOS_REGISTERED_AT))
        lleno = _RegistroSoloEntradas(tuple(entrada_con(marca) for marca in ocupadas))
        with pytest.raises(_FalloDeFlujoError) as exc:
            _registered_at_unico(lleno, fisica, base)  # type: ignore[arg-type]
        assert exc.value.reason is GoldenAdmissionRejectionReason.TGR_CONCURRENT_CHANGE

    def test_misma_entrada_compara_campo_por_campo(self) -> None:
        from sky_claw.local.runtime_vault.golden_admission_service import _misma_entrada

        def entrada(**kwargs: Any) -> TrustedGoldenEntry:
            base: dict[str, Any] = {
                "canonical_root": "C:\\Juego",
                "volume_serial_number": 1,
                "root_file_id": 2,
                "tree_digest": TreeDigest(digest=_SHA, files=3, bytes=4096),
                "policy_version": "gp2-v1",
                "registered_by": _SID,
                "registered_at": "2026-09-25T10:00:00.000000Z",
            }
            base.update(kwargs)
            return TrustedGoldenEntry(**base)

        assert _misma_entrada(entrada(), entrada())
        assert _misma_entrada(entrada(), entrada(canonical_root="c:\\juego"))
        assert not _misma_entrada(entrada(), entrada(tree_digest=TreeDigest(digest=_SHA_OTRO, files=3, bytes=4096)))
        assert not _misma_entrada(entrada(), entrada(registered_at="2026-09-25T10:00:00.000001Z"))
        assert not _misma_entrada(entrada(), entrada(root_file_id=3))


# ============================================================================
# Capa pura: una falla previo al replace NUNCA es un commit desconocido
# ============================================================================


class TestMapeoDeFallasPreviasAlReplace:
    def _servicio(self) -> GoldenAdmissionService:
        return GoldenAdmissionService(
            bridge=object(),  # type: ignore[arg-type]
            confirmation=None,
            token_provider=lambda: _TokenFalso(),
        )

    @pytest.mark.parametrize(
        ("exc", "esperado"),
        [
            (TrustedRegistryLockBusyError("tomado"), GoldenAdmissionRejectionReason.TGR_CONCURRENT_CHANGE),
            (TrustedRegistryLockError("io"), GoldenAdmissionRejectionReason.STORE_FAILED),
            (GoldenAdmissionStoreError("disco"), GoldenAdmissionRejectionReason.AUDIT_RECORD_FAILED),
            (TrustedRegistryError("parse"), GoldenAdmissionRejectionReason.STORE_FAILED),
            (RuntimeError("raro"), GoldenAdmissionRejectionReason.UNEXPECTED_FAILURE),
        ],
    )
    def test_falla_antes_del_writer_es_rejected_tipado(self, exc: Exception, esperado: Any) -> None:
        fallo = self._servicio()._fallo_previo_al_replace(exc)
        assert isinstance(fallo, _FalloDeFlujoError)
        assert fallo.reason is esperado

    def test_el_mapeo_no_puede_producir_un_desenlace_desconocido(self) -> None:
        """Un fallo previo al replace es ``REJECTED``: jamás el desenlace ambiguo.

        ``COMMIT_OUTCOME_UNKNOWN`` se construye sólo desde
        ``_CommitDesconocidoError``; este camino sólo puede emitir valores del
        vocabulario de rechazo.
        """
        fallo = self._servicio()._fallo_previo_al_replace(GoldenAdmissionStoreError("disco"))
        assert isinstance(fallo.reason, GoldenAdmissionRejectionReason)


# ============================================================================
# Flujo completo (Windows: namespace Win32 + writer real)
# ============================================================================


@dataclass
class _BridgeFalso:
    """Puerto OBSERVE/VERIFY con la misma forma y los mismos DTOs que el real."""

    fisica: PhysicalRootIdentity
    arbol: TreeDigest
    arbol_en_verify: TreeDigest | None
    runtime: FreshRuntimeObservation
    verify_fallido: bool = False
    game_key_en_observe: str = "skyrimse"
    traza: list[tuple[Any, ...]] = field(default_factory=list)
    contexto: Any = None

    def invoke_observe(self, token: Any, root: Any, operation_id: str, *, expected_game_key: str = "skyrimse"):
        self.traza.append(("observe", self.contexto.estado(), self.contexto.tgr()))
        from sky_claw.local.runtime_vault.operator_verifier_bridge import (
            OperatorVerifierObservationResult,
            VerifierDisposition,
        )

        runtime = self.runtime
        if self.game_key_en_observe != "skyrimse":
            runtime = FreshRuntimeObservation(
                game_key=self.game_key_en_observe,
                game_version=self.runtime.game_version,
                observed_exe_path=self.runtime.observed_exe_path,
                observed_at_ns=self.runtime.observed_at_ns,
            )
        return OperatorVerifierObservationResult(
            disposition=VerifierDisposition.OBSERVED,
            operation_id=operation_id,
            nonce="n" * 32,
            physical_root=self.fisica,
            observed_tree=self.arbol,
            observed_runtime=runtime,
            critical_evidences=(),
            message="",
        )

    def invoke_verify(
        self,
        token: Any,
        root: Any,
        operation_id: str,
        *,
        expected_physical_root: PhysicalRootIdentity,
        expected_tree: TreeDigest,
        expected_runtime: RuntimeIdentity,
        critical_expectations: Any = (),
        expected_game_key: str = "skyrimse",
    ):
        self.traza.append(
            (
                "verify",
                self.contexto.estado(),
                self.contexto.tgr(),
                expected_tree.digest,
                expected_runtime.game_version,
                expected_physical_root.root_file_id,
            )
        )
        from sky_claw.local.runtime_vault.operator_verifier_bridge import (
            OperatorVerifierVerificationResult,
            VerifierDisposition,
        )

        arbol_medido = self.arbol_en_verify if self.arbol_en_verify is not None else expected_tree
        coincide = arbol_medido == expected_tree
        estado_arbol = VerificationState.VERIFIED if coincide else VerificationState.FAILED
        return OperatorVerifierVerificationResult(
            disposition=VerifierDisposition.VERIFIED
            if (coincide and not self.verify_fallido)
            else VerifierDisposition.FAILED,
            operation_id=operation_id,
            nonce="n" * 32,
            physical_root=self.fisica,
            tree_result=TreeVerificationResult(state=estado_arbol, expected=expected_tree, observed=arbol_medido),
            runtime_result=RuntimeVerificationResult(
                state=VerificationState.VERIFIED,
                expected=expected_runtime,
                observed=expected_runtime,
            ),
            observed_runtime=self.runtime,
            critical_evidences=(),
            message="" if coincide and not self.verify_fallido else "medida distinta del snapshot admitido",
        )


class _ConfirmacionFalsa:
    def __init__(self, resultado: GoldenAdmissionConfirmationResult, contexto: Any) -> None:
        self.resultado = resultado
        self.contexto = contexto
        self.payloads: list[Any] = []

    def request_confirmation(self, payload: Any) -> GoldenAdmissionConfirmationResult:
        self.payloads.append(payload)
        self.contexto.traza.append(("confirm", str(payload.stage), self.contexto.estado(), self.contexto.tgr()))
        return self.resultado


@dataclass
class _ProvenanceFalsa:
    """Verificador de provenance de test: la expectativa sale de acá, no del caller.

    Implementa el puerto ``IndependentProvenanceProvider`` con la forma real. Un
    test configura ``arbol`` distinto del ``expected_tree`` de la solicitud para
    probar que el caller no es autoridad. ``criticas=None`` espeja las
    ``critical_expectations`` declaradas (como haría un bundle que las cubre);
    un valor explícito distinto modela una fuente que no las cubre.
    """

    arbol: TreeDigest
    runtime: RuntimeIdentity
    digest: str
    criticas: tuple[CriticalFileExpectation, ...] | None = None
    fallo: Exception | None = None
    retorno: Any = None
    admitidas: list[Any] = field(default_factory=list)

    def admit(self, request: RegisterOrRefreshTrustedGoldenRequest) -> Any:
        self.admitidas.append(request)
        if self.fallo is not None:
            raise self.fallo
        if self.retorno is not None:
            return self.retorno
        return GoldenAdmissionExpectation(
            state=GoldenAdmissionState.ADMITTED,
            source=GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
            expected_tree=self.arbol,
            expected_runtime=self.runtime,
            critical_expectations=request.critical_expectations if self.criticas is None else self.criticas,
            source_provenance_digest=self.digest,
        )


class _ContextoDeHilo:
    """Contexto por hilo: ``traza`` propia para no mezclar carreras concurrentes."""

    def __init__(self, base: Any) -> None:
        self.base = base
        self.traza: list[tuple[Any, ...]] = []

    def estado(self) -> str:
        return self.base.estado()

    def tgr(self) -> str:
        return self.base.tgr()


@pytest.mark.skipif(sys.platform != "win32", reason="Namespace Win32 y writer real del TGR")
class TestFlujoCompletoWindows:
    @pytest.fixture(autouse=True)
    def _entorno(self, tmp_path: pathlib.Path, entorno_elevado: None) -> None:  # noqa: F811 — fixture compartida
        from sky_claw.local.runtime_vault.trusted_registry import _write_trusted_registry_atomically_at

        self.tmp_path = tmp_path
        rv = tmp_path / "Sky-Claw" / "runtime_vault"
        (rv / "operations").mkdir(parents=True)
        (rv / "locks").mkdir(parents=True)
        self.rv = rv
        self.resolver = lambda: tmp_path  # type: ignore[assignment]
        self.raiz = tmp_path / "juego"
        self.raiz.mkdir()
        self.fisica = derive_physical_root(self.raiz)
        self.arbol = TreeDigest(digest=_SHA, files=3, bytes=4096)
        self.runtime = FreshRuntimeObservation(
            game_key="skyrimse",
            game_version="1.6.1170.0",
            observed_exe_path=str(self.raiz / "SkyrimSE.exe"),
            observed_at_ns=1_700_000_000_000_000_000,
        )
        self.traza: list[tuple[Any, ...]] = []
        self.token = _TokenFalso()
        _write_trusted_registry_atomically_at(
            TrustedGoldenRegistry(entries=(), schema_version="1.0"),
            self.rv / "trusted_goldens.json",
        )
        self.traza = []

    # ------------------------------------------------------------- helpers

    def _neutralizar_el_pre_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sólo el PRE-CHECK deja de ver el registro: ``_rechazado`` no se engaña.

        Devuelve ``False`` en la primera consulta de cada hilo y delega en la
        implementación real a partir de la segunda. Así la colisión real ocurre
        en el ``CREATE_NEW`` del directorio, y una implementación que anexe el
        resultado del perdedor sobre el registro del ganador vuelve a leer el
        disco y el test lo detecta (con un ``False`` permanente el defecto
        quedaría invisible).
        """
        real = GoldenAdmissionService._existe_registro
        visto = threading.local()

        def una_vez_falso(self: GoldenAdmissionService, operacion_id: str) -> bool:
            if not getattr(visto, "pasado", False):
                visto.pasado = True
                return False
            return real(self, operacion_id)

        monkeypatch.setattr(GoldenAdmissionService, "_existe_registro", una_vez_falso)

    @property
    def ruta_tgr(self) -> pathlib.Path:
        return self.rv / "trusted_goldens.json"

    @property
    def ruta_registro(self) -> pathlib.Path:
        return self.rv / "operations" / _OPERACION

    def estado(self) -> str:
        if not self.ruta_registro.exists():
            return "sin-registro"
        registro = load_admission_record(_OPERACION, programdata_resolver=self.resolver)
        fases = ",".join(f"{obs.stage}/{obs.state}" for obs in registro.observations)
        return f"registro({fases or 'sin-observaciones'})"

    def tgr(self) -> str:
        return f"tgr({len(load_trusted_golden_registry(self.ruta_tgr).entries)})"

    def bytes_tgr(self) -> bytes:
        return self.ruta_tgr.read_bytes()

    def registro(self) -> GoldenAdmissionRecord:
        return load_admission_record(_OPERACION, programdata_resolver=self.resolver)

    def bridge(self, **kwargs: Any) -> _BridgeFalso:
        arbol_en_verify = kwargs.pop("arbol_en_verify", None)
        arbol = kwargs.pop("arbol", self.arbol)
        puente = _BridgeFalso(
            fisica=self.fisica,
            arbol=arbol,
            arbol_en_verify=arbol_en_verify,
            runtime=self.runtime,
            **kwargs,
        )
        puente.contexto = self
        puente.traza = self.traza  # la misma lista que usa el diálogo de confirmación
        return puente

    def confirmacion(
        self, resultado: GoldenAdmissionConfirmationResult = GoldenAdmissionConfirmationResult.CONFIRMED
    ) -> _ConfirmacionFalsa:
        return _ConfirmacionFalsa(resultado, self)

    def servicio(
        self,
        bridge: Any,
        confirmation: Any,
        clock: Any = None,
        provenance: Any = None,
    ) -> GoldenAdmissionService:
        return GoldenAdmissionService(
            bridge=bridge,
            confirmation=confirmation,
            token_provider=lambda: self.token,
            provenance_provider=provenance,
            programdata_resolver=self.resolver,
            clock=clock,
        )

    def provenance(self, **kwargs: Any) -> _ProvenanceFalsa:
        """Provider de provenance de test que admite el árbol/runtime que se le pida."""
        base: dict[str, Any] = {
            "arbol": self.arbol,
            "runtime": RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
            "digest": _SHA,
        }
        base.update(kwargs)
        return _ProvenanceFalsa(**base)

    def solicitud_provenance(self, **kwargs: Any) -> RegisterOrRefreshTrustedGoldenRequest:
        from sky_claw.local.runtime_vault.golden_admission_store import GoldenAdmissionSourceReference

        base: dict[str, Any] = {
            "admission_source": GoldenAdmissionSource.INDEPENDENT_PROVENANCE,
            "critical_expectations": _expectativas(),
            "expected_tree": self.arbol,
            "expected_runtime": RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0"),
            "source_reference": GoldenAdmissionSourceReference(kind="bundle", digest=_SHA),
        }
        base.update(kwargs)
        return self.solicitud(**base)

    def solicitud(self, **kwargs: Any) -> RegisterOrRefreshTrustedGoldenRequest:
        base: dict[str, Any] = {
            "operation_id": _OPERACION,
            "root": str(self.raiz),
            "admission_source": GoldenAdmissionSource.OPERATOR_TOFU,
        }
        base.update(kwargs)
        return RegisterOrRefreshTrustedGoldenRequest(**base)

    def sembrar_entrada_previa(self, registered_at: str = "2026-09-25T10:00:00.000000Z") -> TrustedGoldenEntry:
        from sky_claw.local.runtime_vault.trusted_registry import _write_trusted_registry_atomically_at

        anterior = TrustedGoldenEntry(
            canonical_root=self.fisica.canonical_root,
            volume_serial_number=self.fisica.volume_serial_number,
            root_file_id=self.fisica.root_file_id,
            tree_digest=TreeDigest(digest=_SHA_OTRO, files=1, bytes=1),
            policy_version="gp2-v1",
            registered_by="S-1-5-21-9-9-9-500",
            registered_at=registered_at,
        )
        _write_trusted_registry_atomically_at(
            TrustedGoldenRegistry(entries=(anterior,), schema_version="1.0"), self.ruta_tgr
        )
        return anterior

    # ------------------------------------------------------------- tests

    def test_el_puerto_de_provenance_tiene_una_sola_forma(self) -> None:
        """Ancla del puerto: ``admit(request)`` es la única forma de admisión.

        ``tests/`` está fuera del alcance de mypy, así que la conformidad del
        fake con el Protocol se ancla en runtime y no en el type checker.
        """
        firma = inspect.signature(IndependentProvenanceProvider.admit)
        assert list(firma.parameters) == ["self", "request"]
        provider: IndependentProvenanceProvider = self.provenance()
        admitida = provider.admit(self.solicitud_provenance())
        assert isinstance(admitida, GoldenAdmissionExpectation)
        assert admitida.source is GoldenAdmissionSource.INDEPENDENT_PROVENANCE

    def test_tofu_registro_observable_en_orden_y_un_solo_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Observe → confirmación PRE receipt → receipt/registro → RV-2 → UN commit."""
        from sky_claw.local.runtime_vault import golden_admission_service as modulo

        real_mutate = modulo.mutate_trusted_golden_registry
        conteo = {"commits": 0}

        def contando(mutador: Any, **kwargs: Any) -> Any:
            conteo["commits"] += 1
            return real_mutate(mutador, **kwargs)

        monkeypatch.setattr(modulo, "mutate_trusted_golden_registry", contando)

        bridge = self.bridge()
        confirmacion = self.confirmacion()
        resultado = self.servicio(bridge, confirmacion).register_or_refresh_trusted_golden(self.solicitud())

        assert resultado.success is True
        assert resultado.outcome is GoldenAdmissionOutcome.REGISTERED
        assert resultado.reason is None
        assert resultado.tgr_entry is not None
        assert resultado.record_path is not None
        assert conteo["commits"] == 1

        assert self.traza == [
            ("observe", "sin-registro", "tgr(0)"),
            ("confirm", str(GoldenAdmissionConfirmationStage.TOFU_PRE_RERUN), "sin-registro", "tgr(0)"),
            (
                "verify",
                "registro(OBSERVE/OBSERVED)",
                "tgr(0)",
                _SHA,
                "1.6.1170.0",
                self.fisica.root_file_id,
            ),
        ]
        # La confirmación TOFU ocurre ANTES del receipt: payload sin receipt que
        # la respalde, y con el previous del diálogo.
        assert len(confirmacion.payloads) == 1
        assert confirmacion.payloads[0].stage is GoldenAdmissionConfirmationStage.TOFU_PRE_RERUN

        registro = self.registro()
        assert [obs.stage for obs in registro.observations] == ["OBSERVE", "RV2"]
        assert [obs.state for obs in registro.observations] == [
            str(GoldenAdmissionState.OBSERVED),
            str(GoldenAdmissionState.VERIFIED),
        ]
        assert registro.tgr_binding is not None
        assert registro.tgr_before_is_absent
        assert registro.result is not None
        assert registro.result.outcome is GoldenAdmissionOutcome.REGISTERED
        assert registro.result.registered_by == self.token.evidence.operator_sid

        escrita = load_trusted_golden_registry(self.ruta_tgr).entries
        assert len(escrita) == 1
        assert escrita[0].tree_digest == self.arbol
        assert escrita[0].registered_by == self.token.evidence.operator_sid
        assert resultado.tgr_entry == escrita[0]

    def test_provenance_confirma_recien_despues_de_verified_y_antes_del_commit(self) -> None:
        bridge = self.bridge()
        confirmacion = self.confirmacion()
        verificado = self.provenance()
        resultado = self.servicio(bridge, confirmacion, provenance=verificado).register_or_refresh_trusted_golden(
            self.solicitud_provenance()
        )
        assert resultado.success is True
        assert len(verificado.admitidas) == 1

        assert self.traza == [
            ("verify", "registro(sin-observaciones)", "tgr(0)", _SHA, "1.6.1170.0", self.fisica.root_file_id),
            (
                "confirm",
                str(GoldenAdmissionConfirmationStage.PROVENANCE_POST_VERIFIED),
                "registro(RV2/VERIFIED)",
                "tgr(0)",
            ),
        ]
        registro = self.registro()
        assert [obs.stage for obs in registro.observations] == ["RV2"]
        assert registro.source_reference is not None
        assert registro.source_reference.digest == _SHA

    def test_provenance_sin_provider_cableado_es_source_unavailable_fail_closed(self) -> None:
        """Producción: sin verificador de provenance no hay autoridad ni efectos."""
        antes = self.bytes_tgr()
        bridge = self.bridge()
        confirmacion = self.confirmacion()

        resultado = self.servicio(bridge, confirmacion).register_or_refresh_trusted_golden(self.solicitud_provenance())

        assert resultado.success is False
        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is GoldenAdmissionRejectionReason.SOURCE_UNAVAILABLE
        assert resultado.record_path is None
        assert self.traza == []  # ni un pass del bridge, ni una confirmación
        assert confirmacion.payloads == []
        assert not self.ruta_registro.exists()
        assert self.bytes_tgr() == antes

    def test_la_autoridad_de_provenance_es_el_provider_no_el_caller(self) -> None:
        """El caller declara un árbol; el TGR queda con el árbol que admitió el provider."""
        otro_arbol = TreeDigest(digest=_SHA_OTRO, files=7, bytes=7)
        bridge = self.bridge(arbol=otro_arbol)
        verificado = self.provenance(arbol=otro_arbol)

        resultado = self.servicio(
            bridge, self.confirmacion(), provenance=verificado
        ).register_or_refresh_trusted_golden(self.solicitud_provenance(expected_tree=self.arbol))

        assert resultado.success is True
        assert resultado.tgr_entry is not None
        assert resultado.tgr_entry.tree_digest == otro_arbol
        assert load_trusted_golden_registry(self.ruta_tgr).entries[0].tree_digest == otro_arbol

    @pytest.mark.parametrize(
        ("fallo", "esperado"),
        [
            (
                GoldenAdmissionSourceUnavailableError("bundle ausente"),
                GoldenAdmissionRejectionReason.SOURCE_UNAVAILABLE,
            ),
            (GoldenAdmissionSourceError("bundle inválido"), GoldenAdmissionRejectionReason.SOURCE_INVALID),
            (RuntimeError("provider roto"), GoldenAdmissionRejectionReason.SOURCE_INVALID),
        ],
    )
    def test_provider_que_falla_se_rechaza_tipado_y_sin_tgr(self, fallo: Exception, esperado: Any) -> None:
        antes = self.bytes_tgr()
        resultado = self.servicio(
            self.bridge(), self.confirmacion(), provenance=self.provenance(fallo=fallo)
        ).register_or_refresh_trusted_golden(self.solicitud_provenance())

        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is esperado
        assert resultado.record_path is None
        assert not self.ruta_registro.exists()
        assert self.bytes_tgr() == antes

    @pytest.mark.parametrize(
        ("kwargs", "esperado"),
        [
            ({"retorno": object()}, GoldenAdmissionRejectionReason.SOURCE_INVALID),
            ({"criticas": _expectativas(_SHA_OTRO)}, GoldenAdmissionRejectionReason.SOURCE_INVALID),
            ({"digest": _SHA_OTRO}, GoldenAdmissionRejectionReason.SOURCE_INVALID),
        ],
    )
    def test_provider_que_no_cumple_el_contrato_no_autoriza(self, kwargs: Any, esperado: Any) -> None:
        """Un retorno fuera de contrato, o que no cubre lo declarado, es SOURCE_INVALID."""
        antes = self.bytes_tgr()
        resultado = self.servicio(
            self.bridge(), self.confirmacion(), provenance=self.provenance(**kwargs)
        ).register_or_refresh_trusted_golden(self.solicitud_provenance())

        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is esperado
        assert resultado.record_path is None
        assert not self.ruta_registro.exists()
        assert self.bytes_tgr() == antes

    def test_provider_que_no_expone_admit_es_rechazado_al_construir_el_servicio(self) -> None:
        class _SinAdmit:
            pass

        with pytest.raises(GoldenAdmissionServiceError, match="admit"):
            GoldenAdmissionService(
                bridge=self.bridge(),
                confirmation=self.confirmacion(),
                token_provider=lambda: self.token,
                provenance_provider=_SinAdmit(),  # type: ignore[arg-type]
                programdata_resolver=self.resolver,
            )

    def test_tofu_rechazado_por_el_operador_deja_el_tgr_intacto(self) -> None:
        """§11.4: sólo CONFIRMED produce recibo; sin recibo no hay registro ni write."""
        antes = self.bytes_tgr()
        resultado = self.servicio(
            self.bridge(),
            self.confirmacion(GoldenAdmissionConfirmationResult.REJECTED),
        ).register_or_refresh_trusted_golden(self.solicitud())

        assert resultado.success is False
        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is GoldenAdmissionRejectionReason.OPERATOR_REJECTED
        assert resultado.record_path is None
        assert not self.ruta_registro.exists()
        assert self.bytes_tgr() == antes

    def test_sin_provider_no_hay_confirmacion_posible(self) -> None:
        antes = self.bytes_tgr()
        resultado = self.servicio(self.bridge(), None).register_or_refresh_trusted_golden(self.solicitud())
        assert resultado.reason is GoldenAdmissionRejectionReason.OPERATOR_REJECTED
        assert not self.ruta_registro.exists()
        assert self.bytes_tgr() == antes

    def test_rv2_fallido_registra_la_evidencia_y_deja_el_tgr_intacto(self) -> None:
        antes = self.bytes_tgr()
        bridge = self.bridge(arbol_en_verify=TreeDigest(digest=_SHA_OTRO, files=9, bytes=9))
        resultado = self.servicio(bridge, self.confirmacion()).register_or_refresh_trusted_golden(self.solicitud())

        assert resultado.success is False
        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is GoldenAdmissionRejectionReason.RV2_MISMATCH
        assert resultado.record_path is not None
        assert self.bytes_tgr() == antes

        registro = self.registro()
        assert [obs.stage for obs in registro.observations] == ["OBSERVE", "RV2"]
        assert registro.observations[-1].state == str(GoldenAdmissionState.OBSERVED)
        assert registro.tgr_binding is None
        assert registro.result is not None
        assert registro.result.outcome is GoldenAdmissionOutcome.REJECTED
        assert registro.result.reason is GoldenAdmissionRejectionReason.RV2_MISMATCH

    def test_observacion_sin_runtime_del_juego_es_rechazada_antes_del_registro(self) -> None:
        antes = self.bytes_tgr()
        bridge = self.bridge()
        bridge.game_key_en_observe = "skyrim"
        resultado = self.servicio(bridge, self.confirmacion()).register_or_refresh_trusted_golden(self.solicitud())
        assert resultado.reason is GoldenAdmissionRejectionReason.RUNTIME_OBSERVATION_FAILED
        assert not self.ruta_registro.exists()
        assert self.bytes_tgr() == antes

    def test_replay_de_la_misma_operacion_es_one_use(self) -> None:
        bridge = self.bridge()
        primera = self.servicio(bridge, self.confirmacion()).register_or_refresh_trusted_golden(self.solicitud())
        assert primera.success is True
        despues_de_la_primera = self.bytes_tgr()

        segunda = self.servicio(bridge, self.confirmacion()).register_or_refresh_trusted_golden(self.solicitud())
        assert segunda.success is False
        assert segunda.outcome is GoldenAdmissionOutcome.REJECTED
        assert segunda.reason is GoldenAdmissionRejectionReason.REQUEST_INVALID
        assert "one-use" in segunda.message
        assert self.bytes_tgr() == despues_de_la_primera
        # El resultado terminal de la primera corrida sigue siendo REGISTERED.
        registro = self.registro()
        assert registro.result is not None
        assert registro.result.outcome is GoldenAdmissionOutcome.REGISTERED

    def test_claim_ajeno_no_escribe_en_el_registro_del_ganador(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Carrera perdida por el claim: el perdedor ni lee ni escribe el registro ajeno.

        Se fuerza el escenario causal (ambos requests pasan el pre-check y la
        colisión ocurre en el ``CREATE_NEW`` del directorio de operación) sin
        depender del scheduling: el pre-check se neutraliza y se espían las
        únicas rutas del store que tocan el registro.
        """
        from sky_claw.local.runtime_vault import golden_admission_service as modulo

        ganador = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(self.solicitud())
        assert ganador.success is True
        registro_ganador = self.registro()
        bytes_del_registro = derive_admission_record_path(_OPERACION, programdata_resolver=self.resolver).read_bytes()

        ajenas = {"append_result": 0, "append_observation": 0, "bind_tgr_replacement": 0}
        reales = {nombre: getattr(modulo, nombre) for nombre in ajenas}

        def _espia(nombre: str) -> Any:
            def envoltura(*args: Any, **kwargs: Any) -> Any:
                ajenas[nombre] += 1
                return reales[nombre](*args, **kwargs)

            return envoltura

        for nombre in ajenas:
            monkeypatch.setattr(modulo, nombre, _espia(nombre))
        self._neutralizar_el_pre_check(monkeypatch)

        perdedor = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )

        assert perdedor.success is False
        assert perdedor.outcome is GoldenAdmissionOutcome.REJECTED
        assert perdedor.reason is GoldenAdmissionRejectionReason.REQUEST_INVALID
        assert perdedor.record_path is None
        assert ajenas == {"append_result": 0, "append_observation": 0, "bind_tgr_replacement": 0}
        assert (
            derive_admission_record_path(_OPERACION, programdata_resolver=self.resolver).read_bytes()
            == bytes_del_registro
        )
        assert self.registro() == registro_ganador

    def test_dos_requests_concurrentes_con_el_mismo_operation_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Oracle causal: dos requests reales, mismo ``operation_id``, un solo dueño.

        Los dos hilos se sincronizan en la creación del registro para que la
        colisión ocurra de verdad: el ganador completa, el perdedor es
        ``REJECTED`` sin ``record_path`` y el registro del ganador no queda
        contaminado por el perdedor.
        """
        from sky_claw.local.runtime_vault import golden_admission_service as modulo

        barrera = threading.Barrier(2, timeout=20)
        real_crear = modulo.create_admission_record

        def crear_sincronizado(*args: Any, **kwargs: Any) -> Any:
            barrera.wait()
            return real_crear(*args, **kwargs)

        monkeypatch.setattr(modulo, "create_admission_record", crear_sincronizado)
        self._neutralizar_el_pre_check(monkeypatch)

        # El ganador anexa su resultado una sola vez. Se cuenta el INTENTO (no el
        # éxito): si el perdedor intentara escribir el registro ajeno, el append
        # podría fallar por carrera con la persistencia del ganador y el defecto
        # quedaría invisible.
        real_append_result = modulo.append_result
        intentos = {"n": 0}

        def append_contado(*args: Any, **kwargs: Any) -> Any:
            intentos["n"] += 1
            return real_append_result(*args, **kwargs)

        monkeypatch.setattr(modulo, "append_result", append_contado)

        def correr() -> RegisterOrRefreshTrustedGoldenResult:
            contexto = _ContextoDeHilo(self)
            puente = _BridgeFalso(
                fisica=self.fisica,
                arbol=self.arbol,
                arbol_en_verify=None,
                runtime=self.runtime,
                traza=contexto.traza,
            )
            puente.contexto = contexto
            return self.servicio(
                puente, _ConfirmacionFalsa(GoldenAdmissionConfirmationResult.CONFIRMED, contexto)
            ).register_or_refresh_trusted_golden(self.solicitud())

        with ThreadPoolExecutor(max_workers=2) as pool:
            futuros = [pool.submit(correr) for _ in range(2)]
            resultados = [futuro.result(timeout=120) for futuro in futuros]

        ganadores = [r for r in resultados if r.outcome is GoldenAdmissionOutcome.REGISTERED]
        perdedores = [r for r in resultados if r.outcome is GoldenAdmissionOutcome.REJECTED]
        assert len(ganadores) == 1
        assert len(perdedores) == 1
        assert perdedores[0].record_path is None
        assert perdedores[0].reason is GoldenAdmissionRejectionReason.REQUEST_INVALID
        assert intentos["n"] == 1

        registro = self.registro()
        assert registro.result is not None
        assert registro.result.outcome is GoldenAdmissionOutcome.REGISTERED
        assert [obs.stage for obs in registro.observations] == ["OBSERVE", "RV2"]
        assert ganadores[0].tgr_entry is not None
        assert load_trusted_golden_registry(self.ruta_tgr).entries == (ganadores[0].tgr_entry,)

    def test_refresh_reemplaza_la_entrada_y_salta_el_registered_at_un_microsegundo(self) -> None:
        anterior = self.sembrar_entrada_previa()
        reloj = lambda: anterior.registered_at  # noqa: E731 — reloj fijo del test
        bridge = self.bridge()
        resultado = self.servicio(bridge, self.confirmacion(), clock=reloj).register_or_refresh_trusted_golden(
            self.solicitud()
        )
        assert resultado.success is True

        registro = self.registro()
        assert registro.tgr_binding is not None
        assert registro.tgr_binding.before == anterior
        assert registro.tgr_binding.after.registered_at == "2026-09-25T10:00:00.000001Z"

        escritas = load_trusted_golden_registry(self.ruta_tgr).entries
        assert len(escritas) == 1
        assert escritas[0].tree_digest == self.arbol
        assert escritas[0].registered_at == "2026-09-25T10:00:00.000001Z"

    def test_falla_previa_al_replace_es_rejected_y_no_vincula_el_tgr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sky_claw.local.runtime_vault import golden_admission_service as modulo

        antes = self.bytes_tgr()

        def vinculo_roto(*args: Any, **kwargs: Any) -> Any:
            raise modulo.GoldenAdmissionStoreError("disco lleno")

        monkeypatch.setattr(modulo, "bind_tgr_replacement", vinculo_roto)
        resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )

        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is GoldenAdmissionRejectionReason.AUDIT_RECORD_FAILED
        assert self.bytes_tgr() == antes
        registro = self.registro()
        assert registro.tgr_binding is None
        assert registro.result is not None
        assert registro.result.outcome is GoldenAdmissionOutcome.REJECTED

    def test_revalidacion_previa_al_replace_tambien_deja_el_tgr_intacto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sky_claw.local.runtime_vault import golden_admission_service as modulo

        antes = self.bytes_tgr()

        def revalidacion_rota(*args: Any, **kwargs: Any) -> Any:
            raise modulo.GoldenAdmissionStoreError("divergió")

        monkeypatch.setattr(modulo, "revalidate_for_tgr_replace", revalidacion_rota)
        resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )
        assert resultado.reason is GoldenAdmissionRejectionReason.AUDIT_RECORD_FAILED
        assert self.bytes_tgr() == antes
        # §11.4: el binding se persiste ANTES de la revalidación, así que su
        # presencia NO implica un write del TGR — lo que prueba que no lo hubo
        # son los bytes del TGR. El desenlace terminal queda REJECTED.
        registro = self.registro()
        assert registro.tgr_binding is not None
        assert registro.result is not None
        assert registro.result.outcome is GoldenAdmissionOutcome.REJECTED
        assert registro.result.reason is GoldenAdmissionRejectionReason.AUDIT_RECORD_FAILED

    def test_commit_ambiguo_es_commit_outcome_unknown_y_nunca_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """§11.4: writer que falla DESPUÉS de que el mutador devolvió → desconocido."""
        from sky_claw.local.runtime_vault import trusted_registry_lock as lock_mod

        antes = self.bytes_tgr()

        def write_que_lanza(registry: Any, path: Any) -> None:
            # El reemplazo pudo haber corrido: el writer muere sin decidir.
            raise OSError("release del lock interrumpido")

        monkeypatch.setattr(lock_mod, "_write_trusted_registry_atomically_at", write_que_lanza)

        resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )

        assert resultado.success is False
        assert resultado.outcome is GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN
        assert resultado.reason is None
        assert "before" in resultado.message
        assert "Requiere revalidación" in resultado.message
        assert self.bytes_tgr() == antes
        registro = self.registro()
        assert registro.result is not None
        assert registro.result.outcome is GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN

    def test_lock_fallido_despues_del_replace_se_confirma_como_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el TGR confirma el after exacto, el commit ocurrió: es REGISTERED."""
        from sky_claw.local.runtime_vault import trusted_registry_lock as lock_mod

        real_write = lock_mod._write_trusted_registry_atomically_at

        def write_y_lanza(registry: Any, path: Any) -> None:
            real_write(registry, path)
            raise OSError("release del lock interrumpido")

        monkeypatch.setattr(lock_mod, "_write_trusted_registry_atomically_at", write_y_lanza)

        resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )
        assert resultado.success is True
        assert resultado.outcome is GoldenAdmissionOutcome.REGISTERED
        assert load_trusted_golden_registry(self.ruta_tgr).entries == (resultado.tgr_entry,)

    def test_tgr_ausente_es_rejected_sin_crear_el_archivo(self) -> None:
        self.ruta_tgr.unlink()
        antes_existe = self.ruta_tgr.exists()
        assert antes_existe is False

        resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )
        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is GoldenAdmissionRejectionReason.STORE_FAILED
        assert not self.ruta_tgr.exists()
        assert not self.ruta_registro.exists()

    def test_un_solo_camino_de_escritura_tgr_por_operacion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El servicio sólo escribe el TGR por el lock RMW global: UNA vez."""
        from sky_claw.local.runtime_vault import golden_admission_service as modulo

        real_mutate = modulo.mutate_trusted_golden_registry
        conteo = {"n": 0}

        def contando(mutador: Any, **kwargs: Any) -> Any:
            conteo["n"] += 1
            return real_mutate(mutador, **kwargs)

        monkeypatch.setattr(modulo, "mutate_trusted_golden_registry", contando)
        resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )
        assert resultado.success is True
        assert conteo["n"] == 1

    def test_post_commit_audit_append_failure_preserves_registered_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-P3-AUDIT-1: una vez commiteado el TGR, un fallo del audit append NO convierte en REJECTED."""
        from sky_claw.local.runtime_vault import golden_admission_service as modulo

        real_append = modulo.append_result

        def append_que_falla(operacion_id: str, resultado: Any, **kwargs: Any) -> Any:
            if resultado.outcome is GoldenAdmissionOutcome.REGISTERED:
                raise OSError("Disk I/O error simulado en audit append")
            return real_append(operacion_id, resultado, **kwargs)

        monkeypatch.setattr(modulo, "append_result", append_que_falla)

        resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )

        # Invariante central: TGR COMMITTED + audit failure = REGISTERED + audit warning
        assert resultado.success is True
        assert resultado.outcome is GoldenAdmissionOutcome.REGISTERED
        assert resultado.reason is None
        assert resultado.tgr_entry is not None
        assert "No se pudo anexar el resultado terminal al registro de auditoría" in resultado.message
        assert "cero TGR writes" not in resultado.message
        # El TGR real en disco contiene la nueva entrada commiteada
        assert load_trusted_golden_registry(self.ruta_tgr).entries == (resultado.tgr_entry,)

    def test_commit_outcome_unknown_audit_append_failure_preserves_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-P3-AUDIT-4: si el commit es ambiguo y el append de auditoría falla, sigue COMMIT_OUTCOME_UNKNOWN."""
        from sky_claw.local.runtime_vault import golden_admission_service as modulo
        from sky_claw.local.runtime_vault import trusted_registry_lock as lock_mod

        antes = self.bytes_tgr()

        def write_que_lanza(registry: Any, path: Any) -> None:
            raise OSError("release del lock interrumpido")

        monkeypatch.setattr(lock_mod, "_write_trusted_registry_atomically_at", write_que_lanza)

        def append_que_falla(operacion_id: str, resultado: Any, **kwargs: Any) -> Any:
            if resultado.outcome is GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN:
                raise OSError("Disk I/O error en registro de outcome unknown")
            return modulo.append_result(operacion_id, resultado, **kwargs)

        monkeypatch.setattr(modulo, "append_result", append_que_falla)

        resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
            self.solicitud()
        )

        assert resultado.success is False
        assert resultado.outcome is GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN
        assert resultado.reason is None
        assert "before" in resultado.message
        assert "registro terminal: No se pudo anexar" in resultado.message
        assert "cero TGR writes" not in resultado.message
        assert self.bytes_tgr() == antes

    def test_rechazado_audit_append_failure_preserves_rejected_and_never_throws(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-P3-AUDIT-5: si la operación es REJECTED y el audit append falla, el desenlace sigue REJECTED y no lanza."""
        from sky_claw.local.runtime_vault import golden_admission_service as modulo

        def append_que_falla(operacion_id: str, resultado: Any, **kwargs: Any) -> Any:
            if resultado.outcome is GoldenAdmissionOutcome.REJECTED:
                raise OSError("Disk I/O error en registro de REJECTED")
            return modulo.append_result(operacion_id, resultado, **kwargs)

        monkeypatch.setattr(modulo, "append_result", append_que_falla)

        bridge = self.bridge(arbol_en_verify=TreeDigest(digest=_SHA_OTRO, files=9, bytes=9))
        resultado = self.servicio(bridge, self.confirmacion()).register_or_refresh_trusted_golden(self.solicitud())

        assert resultado.success is False
        assert resultado.outcome is GoldenAdmissionOutcome.REJECTED
        assert resultado.reason is GoldenAdmissionRejectionReason.RV2_MISMATCH
        assert "RV-2 no verificó el snapshot admitido" in resultado.message
        assert resultado.record_path is None

    @pytest.mark.parametrize(
        ("fase_fallo", "desenlace_esperado", "success_esperado"),
        [
            ("pre_commit_audit_fail", GoldenAdmissionOutcome.REJECTED, False),
            ("post_commit_audit_fail", GoldenAdmissionOutcome.REGISTERED, True),
            ("rejected_audit_fail", GoldenAdmissionOutcome.REJECTED, False),
            ("unknown_audit_fail", GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN, False),
        ],
    )
    def test_contrato_never_throws_ante_matriz_de_fallos_de_audit_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fase_fallo: str,
        desenlace_esperado: GoldenAdmissionOutcome,
        success_esperado: bool,
    ) -> None:
        """Contrato 'never throws': matriz de fallos de colaboradores del audit-store."""
        from sky_claw.local.runtime_vault import golden_admission_service as modulo
        from sky_claw.local.runtime_vault import trusted_registry_lock as lock_mod

        if fase_fallo == "pre_commit_audit_fail":

            def create_que_falla(*args: Any, **kwargs: Any) -> Any:
                raise OSError("Fallo en create_admission_record")

            monkeypatch.setattr(modulo, "create_admission_record", create_que_falla)
            resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
                self.solicitud()
            )
        elif fase_fallo == "post_commit_audit_fail":
            real_append = modulo.append_result

            def append_post_commit(operacion_id: str, res: Any, **kwargs: Any) -> Any:
                if res.outcome is GoldenAdmissionOutcome.REGISTERED:
                    raise OSError("Fallo en append REGISTERED")
                return real_append(operacion_id, res, **kwargs)

            monkeypatch.setattr(modulo, "append_result", append_post_commit)
            resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
                self.solicitud()
            )
        elif fase_fallo == "rejected_audit_fail":

            def append_rejected(operacion_id: str, res: Any, **kwargs: Any) -> Any:
                if res.outcome is GoldenAdmissionOutcome.REJECTED:
                    raise OSError("Fallo en append REJECTED")
                return modulo.append_result(operacion_id, res, **kwargs)

            monkeypatch.setattr(modulo, "append_result", append_rejected)
            bridge = self.bridge(arbol_en_verify=TreeDigest(digest=_SHA_OTRO, files=9, bytes=9))
            resultado = self.servicio(bridge, self.confirmacion()).register_or_refresh_trusted_golden(self.solicitud())
        elif fase_fallo == "unknown_audit_fail":

            def write_ambiguo(registry: Any, path: Any) -> None:
                raise OSError("Fallo ambiguo")

            monkeypatch.setattr(lock_mod, "_write_trusted_registry_atomically_at", write_ambiguo)

            def append_unknown(operacion_id: str, res: Any, **kwargs: Any) -> Any:
                if res.outcome is GoldenAdmissionOutcome.COMMIT_OUTCOME_UNKNOWN:
                    raise OSError("Fallo en append UNKNOWN")
                return modulo.append_result(operacion_id, res, **kwargs)

            monkeypatch.setattr(modulo, "append_result", append_unknown)
            resultado = self.servicio(self.bridge(), self.confirmacion()).register_or_refresh_trusted_golden(
                self.solicitud()
            )
        else:
            pytest.fail(f"Fase desconocida: {fase_fallo}")

        assert isinstance(resultado, RegisterOrRefreshTrustedGoldenResult)
        assert resultado.outcome is desenlace_esperado
        assert resultado.success is success_esperado


# ============================================================================
# Cableado del paquete: la familia GP2-P3 se enumera, no se muestrea
# ============================================================================


class TestCableadoDelPaquete:
    """Todo módulo de la familia GP2-P3 tiene que estar declarado en
    ``runtime_vault/__init__.__all__``.

    La familia se descubre por glob en el directorio del paquete, no por una
    lista escrita a mano: agregar ``golden_admission_*.py`` rompe
    ``test_la_familia_es_la_esperada`` hasta que se lo enumere acá, y el
    cableado del nuevo módulo queda verificado por
    ``test_cada_modulo_publica_y_el_paquete_reexporta``. Un símbolo público
    renombrado o despublicado rompe el testigo.
    """

    _FAMILIA_ESPERADA = {
        "critical_expectations",
        "golden_admission",
        "golden_admission_service",
        "golden_admission_store",
    }

    #: Un símbolo testigo por módulo: acredita que el módulo efectivamente
    #: publica su API y que el paquete la reexporta (una lista vacía de
    #: ``__all__`` no puede pasar por vacuidad).
    _TESTIGO = {
        "critical_expectations": "canonical_critical_expectations_bytes",
        "golden_admission": "require_golden_admission_outcome",
        "golden_admission_service": "GoldenAdmissionService",
        "golden_admission_store": "create_admission_record",
    }

    #: ``golden_admission_service`` no declara ``__all__`` (mismo patrón que
    #: ``operator_verifier_bridge``/``physical_root``), así que su API pública
    #: se congela por igualdad literal.
    _PUBLICOS_DEL_SERVICIO = (
        "MAX_INTENTOS_REGISTERED_AT",
        "GoldenAdmissionCommitOutcomeUnknownError",
        "GoldenAdmissionService",
        "GoldenAdmissionServiceError",
        "IndependentProvenanceProvider",
        "RegisterOrRefreshTrustedGoldenRequest",
        "RegisterOrRefreshTrustedGoldenResult",
        "validate_golden_admission_request",
    )

    def _modulos_de_la_familia(self) -> set[str]:
        import sky_claw.local.runtime_vault as paquete

        directorio = pathlib.Path(paquete.__file__).parent
        return {p.stem for p in directorio.glob("golden_admission*.py")} | {
            p.stem for p in directorio.glob("critical_expectations.py")
        }

    def test_la_familia_es_la_esperada(self) -> None:
        assert self._modulos_de_la_familia() == self._FAMILIA_ESPERADA

    def test_cada_modulo_publica_y_el_paquete_reexporta(self) -> None:
        import sky_claw.local.runtime_vault as paquete

        expuestos = set(paquete.__all__)
        for modulo in sorted(self._FAMILIA_ESPERADA):
            importado = importlib.import_module(f"sky_claw.local.runtime_vault.{modulo}")
            testigo = self._TESTIGO[modulo]
            assert hasattr(importado, testigo), f"{modulo} no define {testigo}"
            assert testigo in expuestos, f"el paquete no reexporta {testigo} ({modulo})"
            declarados = set(getattr(importado, "__all__", ()))
            faltan = declarados - expuestos
            assert not faltan, f"{modulo} declara nombres sin cablear en el paquete: {sorted(faltan)}"

    def test_la_api_del_servicio_sin_all_esta_cableada(self) -> None:
        import sky_claw.local.runtime_vault as paquete

        faltan = [nombre for nombre in self._PUBLICOS_DEL_SERVICIO if nombre not in paquete.__all__]
        assert not faltan, f"el servicio publica sin cablear en el paquete: {faltan}"

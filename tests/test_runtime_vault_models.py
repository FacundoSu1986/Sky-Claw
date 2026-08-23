"""Modelos del Runtime Vault (RV-1): contrato de forma e invariantes.

Ancla la forma de la identidad: (relpath, size, sha256) por archivo más las
dimensiones del árbol; el mtime NO existe como campo; UNKNOWN != VERIFIED. Y
ancla —por AST— que el paquete no importe código candidate-only del PR #493
(``artifact_digest``, ``texgen_visibility``, ``handoffs``): si una
implementación futura lo cablea, este test rompe antes de que el candidato
salga a revisión independiente.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from sky_claw.local.runtime_vault import models as modelos
from sky_claw.local.runtime_vault.models import (
    CriticalFileEvidence,
    FileIdentity,
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    InventoryError,
    InventoryLinkError,
    RuntimeIdentity,
    RuntimeVaultError,
    RuntimeVerificationResult,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)

#: Módulos candidate-only del PR #493 que RV-1 tiene PROHIBIDO importar.
#: Se compara por subcadena del módulo para atrapar también cualquier subpath.
_MODULOS_PROHIBIDOS = frozenset({"artifact_digest", "texgen_visibility", "handoffs"})


def _imports_de_runtime_vault() -> set[str]:
    paquete = pathlib.Path(__file__).parents[1] / "sky_claw" / "local" / "runtime_vault"
    imports: set[str] = set()
    for archivo in paquete.glob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                imports.update(alias.name for alias in nodo.names)
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                imports.add(nodo.module)
    return imports


class TestFormaDeLosModelos:
    """Los DTOs del vault son frozen+slots y no conocen el mtime."""

    def test_los_dtos_son_frozen_y_slots(self) -> None:
        # Introspección, no muestra manual: un DTO nuevo en models.__all__
        # queda cubierto por construcción (ancla enumerativa del repo).
        tipos = tuple(
            objeto for nombre in modelos.__all__ if dataclasses.is_dataclass(objeto := getattr(modelos, nombre))
        )
        assert tipos, "no se descubrió ningún DTO en sky_claw.local.runtime_vault.models"
        for tipo in tipos:
            # frozen se ve en los parámetros; slots (Python 3.11) se ve en que
            # la clase define __slots__ con los nombres de sus campos.
            assert tipo.__dataclass_params__.frozen, tipo
            slots = getattr(tipo, "__slots__", None)
            assert isinstance(slots, tuple) and len(slots) > 0, tipo

    def test_file_identity_es_la_tripla_relpath_size_digest(self) -> None:
        identidad = FileIdentity(rel_path="textures/x.dds", size=10, digest="a" * 64)
        assert identidad.rel_path == "textures/x.dds"
        assert identidad.size == 10
        assert identidad.digest == "a" * 64
        # El mtime NO forma parte de la identidad: ni siquiera existe como campo.
        assert not hasattr(identidad, "mtime")

    def test_success_solo_en_verified_para_arbol(self) -> None:
        verificado = TreeVerificationResult(state=VerificationState.VERIFIED)
        desconocido = TreeVerificationResult(state=VerificationState.UNKNOWN, message="sin datos")
        fallido = TreeVerificationResult(state=VerificationState.FAILED, message="no coincide")
        assert verificado.success is True
        assert verificado.message == ""
        assert desconocido.success is False
        assert fallido.success is False

    def test_runtime_verification_result_mismo_contrato_success(self) -> None:
        verificado = RuntimeVerificationResult(state=VerificationState.VERIFIED)
        desconocido = RuntimeVerificationResult(state=VerificationState.UNKNOWN, message="sin versión")
        assert verificado.success is True
        assert desconocido.success is False

    def test_estados_son_distintos_y_estables(self) -> None:
        # UNKNOWN != VERIFIED es el invariante central del vault.
        assert VerificationState.UNKNOWN is not VerificationState.VERIFIED
        assert VerificationState.FAILED is not VerificationState.VERIFIED
        assert {e.value for e in VerificationState} == {"verified", "unknown", "failed"}

    def test_runtime_identity_recibe_version_como_dato_de_entrada(self) -> None:
        # El vault no escanea el host: la versión llega observada. Una versión
        # vacía significa "no observado" y jamás puede darse por VERIFIED sola.
        identidad = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        sin_version = RuntimeIdentity(game_key="skyrimse", game_version="")
        assert identidad.game_version == "1.6.1170.0"
        assert sin_version.game_version == ""

    def test_errores_de_dominio_con_jerarquia(self) -> None:
        assert issubclass(InventoryError, RuntimeVaultError)
        assert issubclass(InventoryLinkError, InventoryError)

    def test_success_solo_en_verified_para_golden_y_criticos(self) -> None:
        crit_verificado = CriticalFileEvidence(
            rel_path="a.txt",
            state=VerificationState.VERIFIED,
            expected_digest="a" * 64,
            observed_digest="a" * 64,
            expected_size=10,
            observed_size=10,
        )
        crit_desconocido = CriticalFileEvidence(rel_path="a.txt", state=VerificationState.UNKNOWN)
        crit_fallido = CriticalFileEvidence(rel_path="a.txt", state=VerificationState.FAILED)
        assert crit_verificado.success is True
        assert crit_desconocido.success is False
        assert crit_fallido.success is False

        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=tree_digest,
        )
        golden_verificado = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=tree_res,
            runtime_result=runtime_res,
            critical_results=(crit_verificado,),
            descriptor=desc,
        )
        golden_desconocido = GoldenMasterVerificationResult(state=VerificationState.UNKNOWN)
        golden_fallido = GoldenMasterVerificationResult(state=VerificationState.FAILED)
        assert golden_verificado.success is True
        assert golden_desconocido.success is False
        assert golden_fallido.success is False


_NON_VERIFIED_STATES = tuple(state for state in VerificationState if state is not VerificationState.VERIFIED)


class TestInvarianteResultadoGolden:
    """Valida la invariante estructural de GoldenMasterVerificationResult en __post_init__."""

    def test_b1_verified_sin_tree_result_levanta_error(self) -> None:
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=TreeDigest("a" * 64, 1, 10),
        )
        with pytest.raises(ValueError, match="tree_result en estado VERIFIED"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=None,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    def test_b2_verified_sin_runtime_result_levanta_error(self) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            tree_digest=tree_digest,
        )
        with pytest.raises(ValueError, match="runtime_result en estado VERIFIED"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=None,
                descriptor=desc,
            )

    @pytest.mark.parametrize("non_verified_state", _NON_VERIFIED_STATES)
    def test_b3_verified_con_tree_no_verified_levanta_error(
        self,
        non_verified_state: VerificationState,
    ) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=non_verified_state,
            expected=tree_digest,
            observed=tree_digest if non_verified_state is not VerificationState.UNKNOWN else None,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=tree_digest,
        )
        with pytest.raises(ValueError, match="tree_result en estado VERIFIED"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    @pytest.mark.parametrize("non_verified_state", _NON_VERIFIED_STATES)
    def test_b4_verified_con_runtime_no_verified_levanta_error(
        self,
        non_verified_state: VerificationState,
    ) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=non_verified_state,
            expected=runtime_id,
            observed=runtime_id if non_verified_state is not VerificationState.UNKNOWN else None,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=tree_digest,
        )
        with pytest.raises(ValueError, match="runtime_result en estado VERIFIED"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    @pytest.mark.parametrize("non_verified_state", _NON_VERIFIED_STATES)
    def test_b5_verified_con_critical_no_verified_levanta_error(
        self,
        non_verified_state: VerificationState,
    ) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        crit_non_verified = (CriticalFileEvidence(rel_path="a.txt", state=non_verified_state),)
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=tree_digest,
        )
        with pytest.raises(ValueError, match="toda evidencia crítica esté en estado VERIFIED"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                critical_results=crit_non_verified,
                descriptor=desc,
            )

    def test_b6_verified_sin_descriptor_levanta_error(self) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        with pytest.raises(ValueError, match="requiere descriptor no nulo"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=None,
            )

    @pytest.mark.parametrize("non_verified_state", _NON_VERIFIED_STATES)
    def test_b7_b8_non_verified_con_descriptor_levanta_error(
        self,
        non_verified_state: VerificationState,
    ) -> None:
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            tree_digest=TreeDigest("a" * 64, 1, 10),
        )
        with pytest.raises(ValueError, match="no puede tener descriptor"):
            GoldenMasterVerificationResult(
                state=non_verified_state,
                descriptor=desc,
            )

    def test_b9_verified_con_criticos_vacios_y_descriptor_es_valido(self) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=tree_digest,
        )
        res = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=tree_res,
            runtime_result=runtime_res,
            critical_results=(),
            descriptor=desc,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.success is True
        assert res.descriptor == desc

    def test_c1_verified_descriptor_tree_mismatch_levanta_error(self) -> None:
        tree_observed = TreeDigest("a" * 64, 1, 10)
        tree_mismatched = TreeDigest("b" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_observed,
            observed=tree_observed,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=tree_mismatched,
        )
        with pytest.raises(ValueError, match="coincidir con el árbol observado"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    def test_c2_verified_descriptor_runtime_mismatch_levanta_error(self) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_observed = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_mismatched = RuntimeIdentity("skyrimse", "9.9.999.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_observed,
            observed=runtime_observed,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_mismatched,
            tree_digest=tree_digest,
        )
        with pytest.raises(ValueError, match="coincidir con el runtime observado"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    def test_c3_verified_tree_result_observed_none_levanta_error(self) -> None:
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=TreeDigest("a" * 64, 1, 10),
            observed=None,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=TreeDigest("a" * 64, 1, 10),
        )
        with pytest.raises(ValueError, match="tree_result con expected == observed"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    def test_c4_verified_runtime_result_observed_none_levanta_error(self) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            observed=None,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            tree_digest=tree_digest,
        )
        with pytest.raises(ValueError, match="runtime_result con expected == observed"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    def test_c5_verified_tree_expected_not_equal_observed_levanta_error(self) -> None:
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=TreeDigest("a" * 64, 1, 10),
            observed=TreeDigest("b" * 64, 1, 10),
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=TreeDigest("b" * 64, 1, 10),
        )
        with pytest.raises(ValueError, match="tree_result con expected == observed"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    def test_c6_verified_runtime_expected_not_equal_observed_levanta_error(self) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            observed=RuntimeIdentity("skyrimse", "1.5.97.0"),
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=RuntimeIdentity("skyrimse", "1.5.97.0"),
            tree_digest=tree_digest,
        )
        with pytest.raises(ValueError, match="runtime_result con expected == observed"):
            GoldenMasterVerificationResult(
                state=VerificationState.VERIFIED,
                tree_result=tree_res,
                runtime_result=runtime_res,
                descriptor=desc,
            )

    def test_c7_critical_verified_digest_mismatch_levanta_error(self) -> None:
        with pytest.raises(ValueError, match="expected_digest coincida con observed_digest"):
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.VERIFIED,
                expected_digest="a" * 64,
                observed_digest="b" * 64,
            )

    def test_c8_critical_verified_size_mismatch_levanta_error(self) -> None:
        with pytest.raises(ValueError, match="observed_size coincida con expected_size"):
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.VERIFIED,
                expected_digest="a" * 64,
                observed_digest="a" * 64,
                expected_size=100,
                observed_size=200,
            )

    def test_c9_critical_verified_observed_digest_none_levanta_error(self) -> None:
        with pytest.raises(ValueError, match="requiere expected_digest y observed_digest no nulos"):
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.VERIFIED,
                expected_digest="a" * 64,
                observed_digest=None,
            )

    def test_c10_critical_verified_expected_digest_none_levanta_error(self) -> None:
        with pytest.raises(ValueError, match="requiere expected_digest y observed_digest no nulos"):
            CriticalFileEvidence(
                rel_path="SkyrimSE.exe",
                state=VerificationState.VERIFIED,
                expected_digest=None,
                observed_digest="a" * 64,
            )

    def test_c11_resultado_totalmente_coherente_es_valido(self) -> None:
        crit_ok = CriticalFileEvidence(
            rel_path="SkyrimSE.exe",
            state=VerificationState.VERIFIED,
            expected_digest="a" * 64,
            observed_digest="a" * 64,
            expected_size=10,
            observed_size=10,
        )
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=tree_digest,
        )
        res = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=tree_res,
            runtime_result=runtime_res,
            critical_results=(crit_ok,),
            descriptor=desc,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.success is True
        assert res.descriptor == desc

    def test_c12_resultado_producido_por_verify_golden_master_es_valido(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        from sky_claw.local.runtime_vault.golden import verify_golden_master
        from sky_claw.local.runtime_vault.inventory import inventory_tree
        from sky_claw.local.runtime_vault.models import CriticalFileExpectation
        from sky_claw.local.runtime_vault.verification import tree_digest_from_files

        root = tmp_path / "golden"
        root.mkdir()
        (root / "SkyrimSE.exe").write_bytes(b"MZ_EXE")

        files = inventory_tree(root)
        tree_digest = tree_digest_from_files(files)
        runtime_id = RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
        critical = [
            CriticalFileExpectation(
                rel_path="SkyrimSE.exe",
                expected_digest=files[0].digest,
                expected_size=files[0].size,
            )
        ]

        res = verify_golden_master(
            candidate=root,
            expected_tree=tree_digest,
            expected_runtime=runtime_id,
            observed_runtime=runtime_id,
            critical_expectations=critical,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.success is True
        assert res.descriptor is not None
        assert res.descriptor.tree_digest == tree_digest
        assert res.descriptor.runtime_identity == runtime_id


class TestInvarianteRoleDescriptor:
    """Valida la invariante del rol en GoldenMasterDescriptor.__post_init__."""

    def test_r1_role_runtime_clone_levanta_error(self) -> None:
        with pytest.raises(ValueError, match="role='reference_only'"):
            GoldenMasterDescriptor(
                location=pathlib.Path("G:/game"),
                runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
                tree_digest=TreeDigest("a" * 64, 1, 10),
                role="runtime_clone",
            )

    def test_r2_role_vacio_levanta_error(self) -> None:
        with pytest.raises(ValueError, match="role='reference_only'"):
            GoldenMasterDescriptor(
                location=pathlib.Path("G:/game"),
                runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
                tree_digest=TreeDigest("a" * 64, 1, 10),
                role="",
            )

    def test_r3_role_uppercase_levanta_error(self) -> None:
        with pytest.raises(ValueError, match="role='reference_only'"):
            GoldenMasterDescriptor(
                location=pathlib.Path("G:/game"),
                runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
                tree_digest=TreeDigest("a" * 64, 1, 10),
                role="REFERENCE_ONLY",
            )

    def test_r4_role_con_espacios_levanta_error(self) -> None:
        with pytest.raises(ValueError, match="role='reference_only'"):
            GoldenMasterDescriptor(
                location=pathlib.Path("G:/game"),
                runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
                tree_digest=TreeDigest("a" * 64, 1, 10),
                role=" reference_only ",
            )

    def test_r5_role_reference_only_explicito_es_valido(self) -> None:
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            tree_digest=TreeDigest("a" * 64, 1, 10),
            role="reference_only",
        )
        assert desc.role == "reference_only"

    def test_r6_role_default_es_reference_only(self) -> None:
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=RuntimeIdentity("skyrimse", "1.6.1170.0"),
            tree_digest=TreeDigest("a" * 64, 1, 10),
        )
        assert desc.role == "reference_only"

    def test_r7_integracion_descriptor_valido_en_resultado_verified(self) -> None:
        tree_digest = TreeDigest("a" * 64, 1, 10)
        tree_res = TreeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=tree_digest,
            observed=tree_digest,
        )
        runtime_id = RuntimeIdentity("skyrimse", "1.6.1170.0")
        runtime_res = RuntimeVerificationResult(
            state=VerificationState.VERIFIED,
            expected=runtime_id,
            observed=runtime_id,
        )
        desc = GoldenMasterDescriptor(
            location=pathlib.Path("G:/game"),
            runtime_identity=runtime_id,
            tree_digest=tree_digest,
            role="reference_only",
        )
        res = GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            tree_result=tree_res,
            runtime_result=runtime_res,
            critical_results=(),
            descriptor=desc,
        )
        assert res.state is VerificationState.VERIFIED
        assert res.descriptor is not None
        assert res.descriptor.role == "reference_only"


class TestSinCodigoCandidateOnlyDePr493:
    """Gate del preflight: RV-1 no importa primitives que solo existen en #493."""

    def test_runtime_vault_no_importa_modulos_candidate_only_de_pr493(self) -> None:
        imports = _imports_de_runtime_vault()
        hallados = [m for m in imports if any(prohibido in m for prohibido in _MODULOS_PROHIBIDOS)]
        assert hallados == []

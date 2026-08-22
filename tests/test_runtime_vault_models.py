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

from sky_claw.local.runtime_vault import models as modelos
from sky_claw.local.runtime_vault.models import (
    FileIdentity,
    InventoryError,
    InventoryLinkError,
    RuntimeIdentity,
    RuntimeVaultError,
    RuntimeVerificationResult,
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
        from sky_claw.local.runtime_vault.models import (
            CriticalFileEvidence,
            GoldenMasterVerificationResult,
        )

        crit_verificado = CriticalFileEvidence(rel_path="a.txt", state=VerificationState.VERIFIED)
        crit_desconocido = CriticalFileEvidence(rel_path="a.txt", state=VerificationState.UNKNOWN)
        crit_fallido = CriticalFileEvidence(rel_path="a.txt", state=VerificationState.FAILED)
        assert crit_verificado.success is True
        assert crit_desconocido.success is False
        assert crit_fallido.success is False

        golden_verificado = GoldenMasterVerificationResult(state=VerificationState.VERIFIED)
        golden_desconocido = GoldenMasterVerificationResult(state=VerificationState.UNKNOWN)
        golden_fallido = GoldenMasterVerificationResult(state=VerificationState.FAILED)
        assert golden_verificado.success is True
        assert golden_desconocido.success is False
        assert golden_fallido.success is False


class TestSinCodigoCandidateOnlyDePr493:
    """Gate del preflight: RV-1 no importa primitives que solo existen en #493."""

    def test_runtime_vault_no_importa_modulos_candidate_only_de_pr493(self) -> None:
        imports = _imports_de_runtime_vault()
        hallados = [m for m in imports if any(prohibido in m for prohibido in _MODULOS_PROHIBIDOS)]
        assert hallados == []

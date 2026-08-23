"""Módulo de importación y verificación de Golden Master (RV-2).

Permite tomar una carpeta existente como CANDIDATO a Golden Master,
verificar su identidad utilizando exclusivamente los contratos de RV-1 y
elevarla al rol inmutable REFERENCE_ONLY (GOLDEN_VERIFIED) solo cuando
exista evidencia independiente suficiente.

Trust model central:
    HASHING_A_FOLDER_DOES_NOT_MAKE_IT_A_GOLDEN_MASTER

El inventario y hashing del candidato se realiza en una única pasada sellada
reutilizando :func:`inventory_tree` y :func:`tree_digest_from_files`,
garantizando atomicidad y eficiencia (sin doble hashing ni TOCTOU).
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Sequence

from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    CriticalFileEvidence,
    CriticalFileExpectation,
    FileIdentity,
    GoldenMasterCandidate,
    GoldenMasterDescriptor,
    GoldenMasterVerificationResult,
    InventoryError,
    RuntimeIdentity,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)
from sky_claw.local.runtime_vault.verification import (
    tree_digest_from_files,
    verify_runtime_identity,
)


def verify_critical_files(
    files: Sequence[FileIdentity],
    expectations: Sequence[CriticalFileExpectation],
) -> tuple[CriticalFileEvidence, ...]:
    """Verifica archivos críticos contra el inventario ya capturado (single-pass).

    No re-escanea ni re-hashea el disco: reutiliza las FileIdentity selladas
    durante el inventario, garantizando atomicidad de evidencia y eliminando
    la posibilidad de TOCTOU entre tree digest y critical files.
    """
    vistos: set[str] = set()
    for exp in expectations:
        if exp.rel_path in vistos:
            raise ValueError(f"Expectativas críticas duplicadas para el path '{exp.rel_path}'")
        vistos.add(exp.rel_path)

    por_relpath: dict[str, FileIdentity] = {f.rel_path: f for f in files}
    evidencias: list[CriticalFileEvidence] = []

    for exp in expectations:
        observado = por_relpath.get(exp.rel_path)
        if observado is None:
            evidencias.append(
                CriticalFileEvidence(
                    rel_path=exp.rel_path,
                    state=VerificationState.FAILED,
                    message=f"Archivo crítico ausente en el árbol: '{exp.rel_path}'",
                    expected_digest=exp.expected_digest,
                    observed_digest=None,
                    expected_size=exp.expected_size,
                    observed_size=None,
                )
            )
            continue

        digest_ok = observado.digest.lower() == exp.expected_digest.lower()
        size_ok = exp.expected_size is None or observado.size == exp.expected_size

        if digest_ok and size_ok:
            evidencias.append(
                CriticalFileEvidence(
                    rel_path=exp.rel_path,
                    state=VerificationState.VERIFIED,
                    message="",
                    expected_digest=exp.expected_digest,
                    observed_digest=observado.digest,
                    expected_size=exp.expected_size,
                    observed_size=observado.size,
                )
            )
        else:
            discrepancias: list[str] = []
            if not digest_ok:
                discrepancias.append(f"sha256 ({observado.digest} vs {exp.expected_digest})")
            if not size_ok:
                discrepancias.append(f"tamaño ({observado.size} vs {exp.expected_size} bytes)")
            evidencias.append(
                CriticalFileEvidence(
                    rel_path=exp.rel_path,
                    state=VerificationState.FAILED,
                    message=f"Archivo crítico '{exp.rel_path}' no coincide: {', '.join(discrepancias)}",
                    expected_digest=exp.expected_digest,
                    observed_digest=observado.digest,
                    expected_size=exp.expected_size,
                    observed_size=observado.size,
                )
            )

    return tuple(evidencias)


def verify_golden_master(
    candidate: pathlib.Path | GoldenMasterCandidate,
    *,
    expected_tree: TreeDigest | None,
    expected_runtime: RuntimeIdentity | None,
    observed_runtime: RuntimeIdentity | None = None,
    critical_expectations: Sequence[CriticalFileExpectation] = (),
) -> GoldenMasterVerificationResult:
    """Verifica un candidato a Golden Master y lo eleva a GOLDEN_VERIFIED si hay evidencia independiente.

    Regla central: HASHING_A_FOLDER_DOES_NOT_MAKE_IT_A_GOLDEN_MASTER.
    Requiere evidencia independiente (expected_tree, expected_runtime).
    Sin identidad esperada o ante discrepancias, el estado devuelto es
    UNKNOWN o FAILED (nunca auto-trust ni falso verde).
    """
    if isinstance(candidate, GoldenMasterCandidate):
        root = pathlib.Path(os.path.abspath(os.fspath(candidate.location)))
        obs_runtime = candidate.observed_runtime if observed_runtime is None else observed_runtime
    else:
        root = pathlib.Path(os.path.abspath(os.fspath(candidate)))
        obs_runtime = observed_runtime

    # Validar que no haya duplicados en expectativas críticas antes de empezar
    vistos_criticos: set[str] = set()
    for exp in critical_expectations:
        if exp.rel_path in vistos_criticos:
            raise ValueError(f"Expectativas críticas duplicadas para el path '{exp.rel_path}'")
        vistos_criticos.add(exp.rel_path)

    # 1. Verificación de identidad de runtime (datos de entrada, sin escanear)
    runtime_res = verify_runtime_identity(expected_runtime, obs_runtime)

    # 2. Verificación de árbol y archivos críticos
    if expected_tree is None:
        tree_res = TreeVerificationResult(
            state=VerificationState.UNKNOWN,
            message="Sin identidad de árbol esperada: no se puede verificar el Golden Master (UNKNOWN != VERIFIED).",
            expected=None,
            observed=None,
        )
        crit_res = tuple(
            CriticalFileEvidence(
                rel_path=exp.rel_path,
                state=VerificationState.UNKNOWN,
                message="Sin árbol verificado: no se evaluaron archivos críticos.",
                expected_digest=exp.expected_digest,
                observed_digest=None,
                expected_size=exp.expected_size,
                observed_size=None,
            )
            for exp in critical_expectations
        )
    else:
        try:
            files = inventory_tree(root)
        except InventoryError as exc:
            tree_res = TreeVerificationResult(
                state=VerificationState.UNKNOWN,
                message=f"No se pudo verificar el árbol bajo '{root}': {exc}",
                expected=expected_tree,
                observed=None,
            )
            crit_res = tuple(
                CriticalFileEvidence(
                    rel_path=exp.rel_path,
                    state=VerificationState.UNKNOWN,
                    message=f"No se pudo inspeccionar el árbol: {exc}",
                    expected_digest=exp.expected_digest,
                    observed_digest=None,
                    expected_size=exp.expected_size,
                    observed_size=None,
                )
                for exp in critical_expectations
            )
        else:
            observed_tree = tree_digest_from_files(files)
            if observed_tree == expected_tree:
                tree_res = TreeVerificationResult(
                    state=VerificationState.VERIFIED,
                    message="",
                    expected=expected_tree,
                    observed=observed_tree,
                )
            else:
                diferencias: list[str] = []
                if observed_tree.digest != expected_tree.digest:
                    diferencias.append("digest")
                if observed_tree.files != expected_tree.files:
                    diferencias.append(f"archivos ({observed_tree.files} vs {expected_tree.files})")
                if observed_tree.bytes != expected_tree.bytes:
                    diferencias.append(f"bytes ({observed_tree.bytes} vs {expected_tree.bytes})")
                tree_res = TreeVerificationResult(
                    state=VerificationState.FAILED,
                    message=f"El árbol bajo '{root}' no coincide con la identidad esperada: {', '.join(diferencias)}.",
                    expected=expected_tree,
                    observed=observed_tree,
                )
            crit_res = verify_critical_files(files, critical_expectations)

    # 3. Determinar estado global e invariante Golden
    hay_failed = (
        tree_res.state is VerificationState.FAILED
        or runtime_res.state is VerificationState.FAILED
        or any(c.state is VerificationState.FAILED for c in crit_res)
    )
    if hay_failed:
        mensajes_fallo: list[str] = []
        if tree_res.state is VerificationState.FAILED:
            mensajes_fallo.append(f"Árbol: {tree_res.message}")
        if runtime_res.state is VerificationState.FAILED:
            mensajes_fallo.append(f"Runtime: {runtime_res.message}")
        crit_failed = [c for c in crit_res if c.state is VerificationState.FAILED]
        if crit_failed:
            mensajes_fallo.append(
                f"Archivos críticos fallidos ({len(crit_failed)}): {'; '.join(c.message for c in crit_failed[:3])}"
            )

        return GoldenMasterVerificationResult(
            state=VerificationState.FAILED,
            message="; ".join(mensajes_fallo),
            tree_result=tree_res,
            runtime_result=runtime_res,
            critical_results=crit_res,
            descriptor=None,
        )

    todos_verified = (
        tree_res.state is VerificationState.VERIFIED
        and runtime_res.state is VerificationState.VERIFIED
        and all(c.state is VerificationState.VERIFIED for c in crit_res)
    )
    if todos_verified:
        assert runtime_res.observed is not None
        assert tree_res.observed is not None
        descriptor = GoldenMasterDescriptor(
            location=root,
            runtime_identity=runtime_res.observed,
            tree_digest=tree_res.observed,
            role="reference_only",
        )
        return GoldenMasterVerificationResult(
            state=VerificationState.VERIFIED,
            message="",
            tree_result=tree_res,
            runtime_result=runtime_res,
            critical_results=crit_res,
            descriptor=descriptor,
        )

    mensajes_unknown: list[str] = []
    if tree_res.state is VerificationState.UNKNOWN and tree_res.message:
        mensajes_unknown.append(tree_res.message)
    if runtime_res.state is VerificationState.UNKNOWN and runtime_res.message:
        mensajes_unknown.append(runtime_res.message)
    crit_unknown = [c for c in crit_res if c.state is VerificationState.UNKNOWN and c.message]
    if crit_unknown:
        mensajes_unknown.append(f"Archivos críticos no evaluados ({len(crit_unknown)})")

    return GoldenMasterVerificationResult(
        state=VerificationState.UNKNOWN,
        message="; ".join(mensajes_unknown)
        or "Evidencia incompleta para verificar el Golden Master (UNKNOWN != VERIFIED).",
        tree_result=tree_res,
        runtime_result=runtime_res,
        critical_results=crit_res,
        descriptor=None,
    )


__all__ = ["verify_critical_files", "verify_golden_master"]

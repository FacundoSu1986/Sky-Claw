"""Verificación de árbol y de identidad de runtime (RV-1).

El digest del árbol se DERIVA del inventario (nunca se recorre dos veces) y es
independiente del root físico: participan sólo relpaths, tamaños y digests por
archivo. La comparación es de identidad completa —digest + files + bytes— y el
resultado es siempre uno de los tres estados del contrato:

- VERIFIED: lo observado ES la identidad esperada.
- FAILED: se pudo medir y NO coincide.
- UNKNOWN: no se pudo afirmar nada (sin identidad esperada, árbol ausente o no
  inspeccionable, enlace inyectado). UNKNOWN != VERIFIED: jamás un verde por
  omisión.
"""

from __future__ import annotations

import hashlib
import pathlib
from collections.abc import Sequence

from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import (
    FileIdentity,
    InventoryError,
    RuntimeIdentity,
    RuntimeVerificationResult,
    TreeDigest,
    TreeVerificationResult,
    VerificationState,
)

#: Separador entre campos del digest agregado.
_SEP = b"\x00"


def tree_digest_from_files(files: Sequence[FileIdentity]) -> TreeDigest:
    """Digest de árbol derivado de un inventario, independiente del root físico.

    Fórmula por archivo, en orden lexicográfico del relpath (el orden de
    ``files`` es irrelevante):

        sha256( relpath + NUL + size + NUL + file_digest + NUL )

    path + size + file digest forman la identidad; el mtime no participa y el
    digest no conoce el path absoluto del árbol — mover el árbol a otro root
    produce la misma identidad.
    """
    acumulador = hashlib.sha256()
    total_bytes = 0
    for f in sorted(files, key=lambda f: f.rel_path):
        acumulador.update(f.rel_path.encode("utf-8", "surrogatepass"))
        acumulador.update(_SEP)
        acumulador.update(str(f.size).encode("ascii"))
        acumulador.update(_SEP)
        acumulador.update(f.digest.encode("ascii"))
        acumulador.update(_SEP)
        total_bytes += f.size
    return TreeDigest(digest=acumulador.hexdigest(), files=len(files), bytes=total_bytes)


def verify_tree(root: pathlib.Path, expected: TreeDigest | None) -> TreeVerificationResult:
    """Verifica que el árbol bajo *root* sea exactamente la identidad *expected*.

    Sin identidad esperada no hay nada que verificar: UNKNOWN. Si el inventario
    falla cerrado (enlace, ilegible, mutación concurrente), el resultado es
    UNKNOWN con el motivo en ``message`` — nunca un falso verde.

    Returns:
        :class:`TreeVerificationResult` con ``success=True`` SOLO en VERIFIED.
    """
    if expected is None:
        return TreeVerificationResult(
            state=VerificationState.UNKNOWN,
            message="Sin identidad esperada: no hay nada que verificar (UNKNOWN != VERIFIED).",
            expected=None,
            observed=None,
        )
    try:
        files = inventory_tree(root)
    except InventoryError as exc:
        return TreeVerificationResult(
            state=VerificationState.UNKNOWN,
            message=f"No se pudo verificar el árbol bajo '{root}': {exc}",
            expected=expected,
            observed=None,
        )
    observed = tree_digest_from_files(files)
    if observed == expected:
        return TreeVerificationResult(state=VerificationState.VERIFIED, expected=expected, observed=observed)
    diferencias: list[str] = []
    if observed.digest != expected.digest:
        diferencias.append("digest")
    if observed.files != expected.files:
        diferencias.append(f"archivos ({observed.files} vs {expected.files})")
    if observed.bytes != expected.bytes:
        diferencias.append(f"bytes ({observed.bytes} vs {expected.bytes})")
    return TreeVerificationResult(
        state=VerificationState.FAILED,
        message=f"El árbol bajo '{root}' no coincide con la identidad esperada: {', '.join(diferencias)}.",
        expected=expected,
        observed=observed,
    )


def verify_runtime_identity(
    expected: RuntimeIdentity | None,
    observed: RuntimeIdentity | None,
) -> RuntimeVerificationResult:
    """Compara la identidad de runtime esperada contra la observada (datos de entrada).

    El vault no escanea el host: ambas identidades llegan observadas. Sin
    alguna de las dos, o sin game_key/versión válidos (vacíos o
    whitespace-only) en cualquiera de ellas, no hay VERIFIED posible: una
    comparación a medias es UNKNOWN, no verde.
    """
    if expected is None or observed is None:
        return RuntimeVerificationResult(
            state=VerificationState.UNKNOWN,
            message="Sin identidad esperada u observada: no hay comparación que afirmar (UNKNOWN != VERIFIED).",
            expected=expected,
            observed=observed,
        )
    # Validación de completitud ANTES de comparar: un game_key o una versión
    # vacía (o whitespace-only) no identifica nada, así que la evidencia es
    # incompleta y el resultado es UNKNOWN — nunca VERIFIED ni un mismatch
    # definitivo entre observaciones a medias.
    if not expected.game_key.strip() or not observed.game_key.strip():
        return RuntimeVerificationResult(
            state=VerificationState.UNKNOWN,
            message="Sin game_key válido (esperado u observado): no se puede afirmar VERIFIED.",
            expected=expected,
            observed=observed,
        )
    if expected.game_key != observed.game_key:
        return RuntimeVerificationResult(
            state=VerificationState.FAILED,
            message=(f"El game_key observado '{observed.game_key}' no coincide con el esperado '{expected.game_key}'."),
            expected=expected,
            observed=observed,
        )
    if not expected.game_version.strip() or not observed.game_version.strip():
        return RuntimeVerificationResult(
            state=VerificationState.UNKNOWN,
            message="Sin versión observada (esperada o del host): no se puede afirmar VERIFIED.",
            expected=expected,
            observed=observed,
        )
    if expected.game_version != observed.game_version:
        return RuntimeVerificationResult(
            state=VerificationState.FAILED,
            message=(
                f"La versión observada '{observed.game_version}' no coincide con la esperada '{expected.game_version}'."
            ),
            expected=expected,
            observed=observed,
        )
    return RuntimeVerificationResult(state=VerificationState.VERIFIED, expected=expected, observed=observed)


__all__ = ["tree_digest_from_files", "verify_runtime_identity", "verify_tree"]

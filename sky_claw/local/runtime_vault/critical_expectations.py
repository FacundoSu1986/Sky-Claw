"""Serialización canónica de `critical_expectations` y su digest (ADR 0010 §11.4).

`GoldenAdmissionReceipt.critical_expectations_digest` es ``SHA-256`` de los
bytes de UNA serialización canónica única de la lista confirmada. Reglas
normativas (§11.4, "Bindings normativos del receipt"; oráculo RVO-10):

- cada entrada tiene exactamente ``rel_path``, ``expected_digest`` y
  ``expected_size``;
- ``rel_path`` usa la normalización anti-traversal de
  :class:`CriticalFileExpectation` y los paths normalizados duplicados se
  rechazan (fail-closed);
- las entradas se ordenan por orden lexicográfico de los bytes UTF-8 de
  ``rel_path``;
- ``expected_digest`` va en minúsculas y ``expected_size`` se serializa
  siempre (entero JSON no negativo, no boolean, o ``null``);
- el JSON es un array UTF-8 sin BOM ni newline final, ``ensure_ascii=False``,
  con campos de objeto ordenados lexicográficamente, separadores compactos
  ``(',', ':')`` y sin campos extra ni espacios insignificantes;
- la lista vacía se serializa exactamente como ``[]`` y su digest es
  ``SHA-256(UTF-8("[]"))``: nunca se omite ni se sustituye por ``null``.

Ninguna función de este módulo side-effectúa: es serialización pura, usable
tanto por el helper elevado como por los tests de contrato.
"""

from __future__ import annotations

import hashlib
import json
import string
from collections.abc import Sequence

from sky_claw.local.runtime_vault.models import CriticalFileExpectation

_LOWER_HEX = frozenset(string.hexdigits.lower())

#: Serialización exacta de la lista vacía (§11.4): jamás ``null`` ni omisión.
EMPTY_CRITICAL_EXPECTATIONS_BYTES = b"[]"

__all__ = [
    "EMPTY_CRITICAL_EXPECTATIONS_BYTES",
    "CriticalExpectationsDigestError",
    "canonical_critical_expectations_bytes",
    "critical_expectations_digest",
]


class CriticalExpectationsDigestError(ValueError):
    """La lista de expectativas críticas viola el contrato canónico: fail-closed."""


def _entrada_canonica(expectation: CriticalFileExpectation, indice: int) -> dict[str, object]:
    if not isinstance(expectation, CriticalFileExpectation):
        raise CriticalExpectationsDigestError(
            f"La entrada {indice} debe ser CriticalFileExpectation; obtenido {type(expectation).__name__}"
        )

    size = expectation.expected_size
    if size is not None:
        if isinstance(size, bool) or not isinstance(size, int):
            raise CriticalExpectationsDigestError(
                f"expected_size de la entrada {indice} debe ser un entero JSON no negativo o null"
            )
        if size < 0:
            raise CriticalExpectationsDigestError(f"expected_size de la entrada {indice} no puede ser negativo")

    digest = expectation.expected_digest
    if not isinstance(digest, str) or len(digest) != 64 or any(ch not in _LOWER_HEX for ch in digest):
        raise CriticalExpectationsDigestError(
            f"expected_digest de la entrada {indice} debe ser SHA-256 hex de 64 caracteres en minúsculas"
        )

    return {
        "rel_path": expectation.rel_path,
        "expected_digest": digest,
        "expected_size": size,
    }


def canonical_critical_expectations_bytes(
    expectations: Sequence[CriticalFileExpectation],
) -> bytes:
    """Serializa la lista confirmada a su única representación canónica UTF-8.

    Args:
        expectations: secuencia de :class:`CriticalFileExpectation` (puede ser
            vacía; la vacía serializa exactamente como ``[]``).

    Returns:
        Los bytes JSON UTF-8 exactos: sin BOM, sin newline final,
        ``ensure_ascii=False``, claves de objeto ordenadas lexicográficamente
        y separadores compactos.

    Raises:
        CriticalExpectationsDigestError: si la lista no satisface el contrato
            canónico (entrada no-modelo, ``expected_size`` booleano/flotante/
            negativo, digest malformado o ``rel_path`` normalizado duplicado).
    """
    if isinstance(expectations, (str, bytes)) or not isinstance(expectations, Sequence):
        raise CriticalExpectationsDigestError(
            f"expectations debe ser una secuencia de CriticalFileExpectation; obtenido {type(expectations).__name__}"
        )

    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for indice, expectation in enumerate(expectations):
        canonica = _entrada_canonica(expectation, indice)
        rel_path = str(canonica["rel_path"])
        if rel_path in seen:
            raise CriticalExpectationsDigestError(
                f"rel_path normalizado duplicado en critical_expectations: '{rel_path}'"
            )
        seen.add(rel_path)
        entries.append(canonica)

    # Orden lexicográfico por los bytes UTF-8 de rel_path (§11.4).
    entries.sort(key=lambda e: str(e["rel_path"]).encode("utf-8"))

    texto = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # encode("utf-8") no emite BOM; json.dumps no agrega newline final.
    return texto.encode("utf-8")


def critical_expectations_digest(expectations: Sequence[CriticalFileExpectation]) -> str:
    """Devuelve ``SHA-256`` hex en minúsculas de la serialización canónica."""
    return hashlib.sha256(canonical_critical_expectations_bytes(expectations)).hexdigest()

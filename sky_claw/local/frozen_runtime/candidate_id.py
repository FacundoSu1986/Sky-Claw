"""Candidate-id: identificador de Candidate, NO de Generation (P3).

Formato: ``cand_<32 hex>`` (UUID4 hex). Un Candidate todavia NO es una
Generation, asi que **no** reutiliza el ``generation-id`` ligado a contenido
(``<display-version>__<digest12>``): ese id afirma una version y un digest de
arbol que un Candidate en BUILDING todavia no tiene, y reutilizarlo haria que un
estadio transitorio se presentara con la identidad de uno definitivo.

Propiedades del contrato:

- **path-safe**: todo candidate-id que llega a un path pasa por acá. Charset
  ``[a-z0-9]`` mas el separador ``_``, sin ``..``, sin separadores de Windows,
  sin NUL/control, sin punto/espacio final y sin nombres reservados.
- **no autoridad criptografica**: identifica una corrida, no un contenido. Dos
  Candidates del MISMO arbol tienen ids distintos (a proposito: la generacion se
  decide en P4, no aqui). La autoridad la dan PRE/Candidate/POST.
- **inyectable**: la factory se pasa por parametro para que los tests sean
  deterministas sin abrir una ventana de colision en produccion.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Final

from sky_claw.local.frozen_runtime.errors import InvalidCandidateIdError

CANDIDATE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^cand_[0-9a-f]{32}$")

CANDIDATE_ID_PREFIX: Final[str] = "cand_"

_RESERVADOS_WINDOWS: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def default_candidate_id_factory() -> str:
    """Factory productiva: UUID4 hex (collision-safe, generado internamente)."""
    return f"{CANDIDATE_ID_PREFIX}{uuid.uuid4().hex}"


def validar_candidate_id(candidate_id: str) -> str:
    """Valida un candidate-id; fail-closed ante toda forma ambigua."""
    if not isinstance(candidate_id, str) or not candidate_id:
        raise InvalidCandidateIdError("candidate_id no puede ser vacio")
    if candidate_id != candidate_id.strip():
        raise InvalidCandidateIdError(f"candidate_id con espacios en los bordes: '{candidate_id}'")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate_id):
        raise InvalidCandidateIdError("candidate_id con caracteres de control o NUL")
    if not CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        raise InvalidCandidateIdError(
            f"candidate_id con formato invalido: '{candidate_id}' "
            "(esperado 'cand_<32 hex>'; sin traversal, sin separadores y sin ruta absoluta)"
        )
    token = candidate_id.split("_", 1)[0]
    if token in _RESERVADOS_WINDOWS:
        raise InvalidCandidateIdError(f"candidate_id con nombre reservado de Windows: '{token}'")
    return candidate_id


def nuevo_candidate_id(factory: Callable[[], str] = default_candidate_id_factory) -> str:
    """Genera y valida un candidate-id nuevo (el factory es inyectable)."""
    return validar_candidate_id(factory())

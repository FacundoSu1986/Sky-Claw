"""Generation-id ligado a contenido (P2).

Formato: ``<display-version>__<tree-digest[:12]>`` (p. ej. ``1.6.1170__a1b2c3d4e5f6``).

- El digest es SHA-256 en lowercase hex; la metadata conserva el digest
  COMPLETO (el prefijo es sólo legibilidad).
- Dos árboles distintos pueden compartir display version ⇒ ids distintos por
  contenido (G02). El ``buildid`` del proveedor jamás forma parte del id.
- Charset ``[a-z0-9._-]``; sin ``..`` (puntos sólo simples entre runs
  alfanuméricos), sin separadores ``/\\:*?\"<>|``, sin ``:``, sin NUL/control,
  sin punto/espacio final, y sin nombres reservados de Windows (G03–G05).
- La validación es fail-closed: todo id que llegue a un path pasó por acá.
"""

from __future__ import annotations

import re

from sky_claw.local.frozen_runtime.errors import InvalidGenerationIdError
from sky_claw.local.runtime_vault.models import TreeDigest

GENERATION_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)*__[0-9a-f]{12}$")

_RESERVADOS_WINDOWS: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def _token_reservado(generation_id: str) -> str | None:
    token = re.split(r"[._]", generation_id, maxsplit=1)[0]
    return token if token in _RESERVADOS_WINDOWS else None


def validar_generation_id(generation_id: str) -> str:
    """Valida y normaliza un generation-id; fail-closed ante todo lo ambiguo."""
    if not isinstance(generation_id, str) or not generation_id:
        raise InvalidGenerationIdError("generation_id no puede ser vacío")
    if generation_id != generation_id.strip():
        raise InvalidGenerationIdError(f"generation_id con espacios en los bordes: '{generation_id}'")
    normalized = generation_id.casefold()
    if not GENERATION_ID_PATTERN.fullmatch(normalized):
        raise InvalidGenerationIdError(
            f"generation_id con formato inválido: '{generation_id}' "
            "(esperado '<display-version>__<digest12>', charset [a-z0-9._-], sin traversal ni separadores)"
        )
    reservado = _token_reservado(normalized)
    if reservado is not None:
        raise InvalidGenerationIdError(f"generation_id con nombre reservado de Windows: '{reservado}'")
    return normalized


def construir_generation_id(display_version: str, tree_digest: TreeDigest) -> str:
    """Construye el id legible y ligado a contenido desde versión + digest."""
    if not isinstance(display_version, str) or not display_version.strip():
        raise InvalidGenerationIdError("display_version no puede ser vacía")
    display = display_version.strip().casefold().replace(" ", "-")
    if not re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9]+)*", display):
        raise InvalidGenerationIdError(
            f"display_version con formato inválido para el id: '{display_version}' (esperado p. ej. '1.6.1170')"
        )
    return validar_generation_id(f"{display}__{tree_digest.digest[:12].casefold()}")


def generation_id_desde_version(game_version: str, tree_digest: TreeDigest) -> str:
    """Deriva el display (major.minor.patch) de ``game_version`` y construye el id."""
    partes = game_version.strip().split(".")
    if len(partes) < 3:
        raise InvalidGenerationIdError(f"game_version sin major.minor.patch para derivar el display: '{game_version}'")
    display = ".".join(partes[:3])
    return construir_generation_id(display, tree_digest)

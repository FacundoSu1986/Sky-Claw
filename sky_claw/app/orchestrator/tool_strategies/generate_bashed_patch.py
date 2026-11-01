"""Strategy for the `generate_bashed_patch` tool."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# ADR 0016 §7: `fase` elige media etapa asistida (preparar | verificar) y
# `testigo_previo` es el campo homónimo que la fase 1 devolvió, que la fase 2
# necesita para probar frescura. Sin `fase` acá, la GUI y el supervisor no podían
# llegar nunca a la verificación: el filtro la descartaba en silencio y toda
# invocación caía en la preparación.
_VALID_BASHED_PATCH_KEYS = {"profile", "validate_limit", "fase", "testigo_previo"}


class GenerateBashedPatchStrategy:
    name = "generate_bashed_patch"

    def __init__(
        self,
        wrye_bash_pipeline: Callable[..., Awaitable[dict[str, Any]]],
    ) -> None:
        self.wrye_bash_pipeline = wrye_bash_pipeline

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        # warn=False: execute() filters (and warns) again for the same payload —
        # warning here too would duplicate the log line per invocation.
        filtered = self._filter_payload(payload_dict, warn=False)
        if not filtered:
            return "payload: <empty>"
        parts = [f"{key}={value!r}" for key, value in sorted(filtered.items())]
        return "payload: " + ", ".join(parts)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        filtered = self._filter_payload(payload_dict)
        # `fase=None` (clave presente y vacía, típico del formulario de la GUI) llegaría
        # al servicio como fase inválida y devolvería `FaseInvalida`. Se descarta acá para
        # que el default del pipeline (`preparar`) aplique en vez de fallar por un None.
        # `in` + `pop` y no `get(...) is None`: la clave ausente también da None.
        for clave_opcional in ("fase", "testigo_previo"):
            if clave_opcional in filtered and filtered[clave_opcional] is None:
                del filtered[clave_opcional]
        return await self.wrye_bash_pipeline(**filtered)

    def _filter_payload(self, payload_dict: dict[str, Any], *, warn: bool = True) -> dict[str, Any]:
        filtered = {key: value for key, value in payload_dict.items() if key in _VALID_BASHED_PATCH_KEYS}
        unexpected = payload_dict.keys() - _VALID_BASHED_PATCH_KEYS
        if unexpected and warn:
            logger.warning("Dropping unexpected payload keys in %s: %s", self.name, unexpected)
        return filtered

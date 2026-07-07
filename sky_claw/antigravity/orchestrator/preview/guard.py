"""Guard del manifiesto por acción — enforcement de la "caja negra de vuelo".

Regla: un Ritual mutante NO se ejecuta si antes no declaró y persistió su
:class:`ActionManifest`. :func:`require_manifest` es la primitiva fail-secure
que lo garantiza: lee el manifiesto del journal y corta si falta.

Desacoplado del journal concreto: solo depende de ``get_action_manifest`` (se
tipa con un Protocol para que sea trivialmente mockeable en tests y no ate este
módulo a :class:`~sky_claw.antigravity.db.journal.OperationJournal`).
"""

from __future__ import annotations

from typing import Any, Protocol

from sky_claw.antigravity.orchestrator.preview.manifest import ActionManifest


class MissingManifestError(RuntimeError):
    """Se intentó ejecutar un Ritual mutante sin manifiesto previo persistido."""


class _ManifestStore(Protocol):
    """Lo mínimo que el guard necesita del journal."""

    async def get_action_manifest(self, workflow_id: str) -> dict[str, Any] | None: ...


async def require_manifest(journal: _ManifestStore, workflow_id: str) -> ActionManifest:
    """Devuelve el :class:`ActionManifest` persistido para ``workflow_id``.

    Raises:
        MissingManifestError: si no hay manifiesto persistido para ese workflow.
    """
    data = await journal.get_action_manifest(workflow_id)
    if not data:
        raise MissingManifestError(
            f"No hay manifiesto persistido para el workflow '{workflow_id}': "
            "un Ritual mutante debe declarar su ActionManifest antes de ejecutar."
        )
    return ActionManifest.model_validate(data)

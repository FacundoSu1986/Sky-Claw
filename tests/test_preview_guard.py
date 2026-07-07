"""Tests del guard ``require_manifest`` — enforcement del manifiesto por acción.

Regla de oro de la "caja negra": un Ritual mutante NO se ejecuta si antes no
declaró (y persistió) su manifiesto. ``require_manifest`` es la primitiva que
hace verdadera esa regla: lee del journal y corta (fail-secure) si falta.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sky_claw.antigravity.orchestrator.preview.guard import (
    MissingManifestError,
    require_manifest,
)
from sky_claw.antigravity.orchestrator.preview.manifest import ActionManifest


def _journal_con(manifest_dict: dict | None) -> AsyncMock:
    journal = AsyncMock()
    journal.get_action_manifest = AsyncMock(return_value=manifest_dict)
    return journal


@pytest.mark.asyncio
async def test_require_manifest_lanza_si_no_hay_manifiesto() -> None:
    journal = _journal_con(None)

    with pytest.raises(MissingManifestError):
        await require_manifest(journal, "wf-sin-manifiesto")

    journal.get_action_manifest.assert_awaited_once_with("wf-sin-manifiesto")


@pytest.mark.asyncio
async def test_require_manifest_devuelve_el_action_manifest_si_esta() -> None:
    persistido = ActionManifest(
        workflow_id="wf-ok",
        ritual="loot_sort",
        tool="LOOT",
    ).model_dump(mode="json")
    journal = _journal_con(persistido)

    manifest = await require_manifest(journal, "wf-ok")

    assert isinstance(manifest, ActionManifest)
    assert manifest.workflow_id == "wf-ok"
    assert manifest.ritual == "loot_sort"

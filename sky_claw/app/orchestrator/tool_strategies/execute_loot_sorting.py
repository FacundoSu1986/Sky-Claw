"""Strategy for the `execute_loot_sorting` tool.

The shared dispatcher HITL gate owns operator approval. This strategy owns
payload validation and the lock-protected LOOT service call.

Perfil MO2: el payload que manda el Ritual de la GUI es ``{}``
(``ritual_runner.dispatch_ritual`` → ``supervisor.dispatch_tool(tool, {})``), así que
sin ``default_profile_getter`` ``LootExecutionParams`` caía a su default de pydantic
(``"Default"``) y ``LootSortingService._ensure_loot_runner`` rebindeaba el runner a
ESE perfil — ``BrokeredLootRunner.for_profile`` devuelve ``self`` solo cuando el
perfil coincide, así que el ``profile=active_profile`` con el que ``app_context``
construye el runner quedaba descartado. Resultado: la GUI ordenaba el load order de
``Default`` mientras el agente LLM ordenaba el de la sesión, sobre el MISMO
``plugins.txt``/``loadorder.txt``; el modal HITL nombraba ``Default``; y el
snapshot/rollback (que saca su perfil del ``PathResolver``) protegía los archivos
del perfil de sesión, es decir los que LOOT no tocaba.

El mecanismo es el mismo que ya usan los dos hermanos de esta carpeta:
``ValidatePluginLimitStrategy`` (``default_profile_getter``) y
``GenerateBashedPatchStrategy`` (``profile or get_active_profile()``). Ancla:
``tests/test_perfil_sesion_ritual_gui.py``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sky_claw.app.core.models import LootExecutionParams

if TYPE_CHECKING:
    from sky_claw.local.tools.loot_service import LootSortingService


class ExecuteLootSortingStrategy:
    name = "execute_loot_sorting"

    def __init__(
        self,
        service: LootSortingService,
        default_profile_getter: Callable[[], str],
    ) -> None:
        self.service = service
        self.default_profile_getter = default_profile_getter

    def validate_for_approval(self, payload_dict: dict[str, Any]) -> None:
        self._parse_params(payload_dict)

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        params = self._parse_params(payload_dict)
        return f"profile_name={params.profile_name!r}, update_masterlist={params.update_masterlist!r}"

    async def prepare_for_approval(self, payload_dict: dict[str, Any]) -> None:
        params = self._parse_params(payload_dict)
        prepared = self.service.prepare_vfs_attestation(params)
        if inspect.isawaitable(prepared):
            await prepared

    def clear_approval_preparation(self, payload_dict: dict[str, Any]) -> None:
        params = self._parse_params(payload_dict)
        self.service.clear_vfs_attestation(params)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        params = self._parse_params(payload_dict)
        return await self.service.sort_load_order(params)

    def _parse_params(self, payload_dict: dict[str, Any]) -> LootExecutionParams:
        # Chequeo explícito en vez de `dict.get(key, default)`: no evaluar el getter
        # cuando el payload ya trae el perfil (mismo criterio que
        # ``ValidatePluginLimitStrategy``).
        if "profile_name" in payload_dict:
            return LootExecutionParams(**payload_dict)
        return LootExecutionParams(**payload_dict, profile_name=self.default_profile_getter())

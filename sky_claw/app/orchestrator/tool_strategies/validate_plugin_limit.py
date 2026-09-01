"""Estrategia de la tool ``validate_plugin_limit``.

Adaptador fino sobre ``PluginLimitGuard.run(profile)`` (PR5). El guard se
inyecta como callable de producción desde el ``SupervisorAgent.__init__`` vía
``build_orchestration_composition``; los tests que necesiten reemplazar la
implementación (vinculación tardía) lo hacen sobre el
``OrchestrationDispatcherDependencies`` reconstruyendo el dispatcher con
``dataclasses.replace``.

Recibe un **callable** para mantener la estrategia testeable sin acoplarse al
supervisor, y el getter del perfil por defecto para cuando el payload omite
``profile``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class ValidatePluginLimitStrategy:
    name = "validate_plugin_limit"

    def __init__(
        self,
        plugin_limit_guard: Callable[[str], Awaitable[dict[str, Any]]],
        default_profile_getter: Callable[[], str],
    ) -> None:
        self.plugin_limit_guard = plugin_limit_guard
        self.default_profile_getter = default_profile_getter

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        # Evitar la evaluación ansiosa de default_profile_getter() cuando la
        # clave "profile" ya existe: dict.get(key, default) evalúa el default
        # primero, así que se usa una verificación explícita.
        profile = payload_dict["profile"] if "profile" in payload_dict else self.default_profile_getter()
        return await self.plugin_limit_guard(profile)

"""Strategy for the `validate_plugin_limit` tool.

Thin adapter onto `supervisor._run_plugin_limit_guard(profile)`. That
method stays on the supervisor (Spec §9) — wrapping it is a separate
refactor.

Receives a **callable** for late-binding (so test fixtures can replace
`supervisor._run_plugin_limit_guard = AsyncMock(...)`) and the default
profile name to use when payload omits `profile`.
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
        # Avoid eager evaluation of default_profile_getter() if "profile" key exists.
        # dict.get(key, default) evaluates default first, so use explicit check instead.
        profile = payload_dict["profile"] if "profile" in payload_dict else self.default_profile_getter()
        return await self.plugin_limit_guard(profile)

"""FOMOD resolver — evaluates selections and conditions to produce file lists.

Given a parsed :class:`FomodConfig` and a dictionary of user selections,
determines which files should be installed by evaluating visibility
conditions, flag dependencies, and conditional file installs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sky_claw.fomod.models import (
    CompositeDependency,
    FileInstall,
    FomodConfig,
    GroupType,
    InstallStep,
)

logger = logging.getLogger(__name__)


@dataclass
class ResolveResult:
    """Result of resolving a FOMOD configuration.

    Attributes:
        files: Files to install, sorted by priority.
        pending_decisions: Steps/groups that need user input.
        flags: Final state of all condition flags.
    """

    files: list[FileInstall] = field(default_factory=list)
    pending_decisions: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)


class FomodResolver:
    """Resolves FOMOD selections into a concrete list of files.

    Args:
        config: Parsed FOMOD configuration.
    """

    def __init__(self, config: FomodConfig) -> None:
        self._config = config

    def resolve(
        self,
        selections: dict[str, list[str]],
    ) -> ResolveResult:
        """Resolve selections into a file install list.

        Args:
            selections: Maps step name → list of selected plugin names.
                For ``SelectAll`` groups, all plugins are auto-selected
                even if not explicitly listed.

        Returns:
            ResolveResult with files sorted by priority, pending
            decisions for unresolved steps, and final flag state.
        """
        result = ResolveResult()
        flags: dict[str, str] = {}

        # 1. Always include required files.
        result.files.extend(self._config.required_files)

        # 2. Walk install steps in order.
        for step in self._config.install_steps:
            if not self._is_step_visible(step, flags):
                continue

            step_selections = selections.get(step.name, [])

            for group in step.groups:
                selected_plugins = self._resolve_group(
                    step.name,
                    group.name,
                    group.group_type,
                    group.plugins,
                    step_selections,
                    result,
                )

                for plugin in group.plugins:
                    if plugin.name in selected_plugins:
                        result.files.extend(plugin.files)
                        for cf in plugin.condition_flags:
                            flags[cf.name] = cf.value

        result.flags = flags

        # 3. Evaluate conditional file installs.
        for pattern in self._config.conditional_installs:
            if self._evaluate_conditions(pattern.conditions, flags):
                result.files.extend(pattern.files)

        # 4. Sort by priority (higher = later = overwrites).
        result.files.sort(key=lambda f: f.priority)

        return result

    def _is_step_visible(self, step: InstallStep, flags: dict[str, str]) -> bool:
        """Check if a step's visibility conditions are met."""
        if step.visibility is None:
            return True
        return self._evaluate_conditions(step.visibility, flags)

    def _resolve_group(
        self,
        step_name: str,
        group_name: str,
        group_type: GroupType,
        plugins: list,
        step_selections: list[str],
        result: ResolveResult,
    ) -> set[str]:
        """Determine which plugins are selected in a group."""
        plugin_names = {p.name for p in plugins}
        selected = {s for s in step_selections if s in plugin_names}

        if group_type == GroupType.SELECT_ALL:
            return plugin_names

        if group_type == GroupType.SELECT_EXACTLY_ONE and len(selected) != 1:
            if not selected:
                result.pending_decisions.append(
                    f"{step_name}/{group_name}: must select exactly one"
                )
            return selected

        if group_type == GroupType.SELECT_AT_LEAST_ONE and not selected:
            result.pending_decisions.append(
                f"{step_name}/{group_name}: must select at least one"
            )

        return selected

    def _evaluate_conditions(
        self,
        conditions: CompositeDependency,
        flags: dict[str, str],
    ) -> bool:
        """Evaluate a composite dependency against current flags."""
        results: list[bool] = []

        for fd in conditions.flag_deps:
            results.append(flags.get(fd.flag, "") == fd.value)

        for _ in conditions.file_deps:
            # File deps cannot be fully evaluated without filesystem access.
            # Default to True (optimistic).
            results.append(True)

        for nested in conditions.nested:
            results.append(self._evaluate_conditions(nested, flags))

        if not results:
            return True

        if conditions.operator == "And":
            return all(results)
        return any(results)

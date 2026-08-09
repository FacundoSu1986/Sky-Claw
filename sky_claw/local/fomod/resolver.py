"""FOMOD resolver — evaluates selections and conditions to produce file lists.

Given a parsed :class:`FomodConfig` and a dictionary of user selections,
determines which files should be installed by evaluating visibility
conditions, flag dependencies, and conditional file installs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sky_claw.local.fomod.models import (
    CompositeDependency,
    FileInstall,
    FomodConfig,
    GroupType,
    InstallStep,
    Plugin,
    PluginType,
)
from sky_claw.local.fomod.plugin_state import FileStateProvider

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
        file_state: Provider of installed-file state used to evaluate
            ``fileDependency`` conditions. When None, file dependencies are
            evaluated as **false** (fail-closed): a condition on an unverifiable
            file never installs its content blindly.
    """

    def __init__(self, config: FomodConfig, file_state: FileStateProvider | None = None) -> None:
        self._config = config
        self._file_state = file_state

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
                    flags,
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
        plugins: list[Plugin],
        step_selections: list[str],
        flags: dict[str, str],
        result: ResolveResult,
    ) -> set[str]:
        """Determine which plugins are selected in a group.

        Semantics applied here (FOMOD ``typeDescriptor``):
        - plugins whose ``dependencyType`` is unsatisfied are ineligible —
          an explicit user selection on them is ignored;
        - ``Required`` plugins are forced when eligible;
        - with no user selection, ``Recommended`` plugins are preselected.
        """
        effective_types = {p.name: self._plugin_type_effective(p, flags) for p in plugins}
        eligible = [p for p in plugins if effective_types[p.name] is not None]
        plugin_names = {p.name for p in eligible}
        selected = {s for s in step_selections if s in plugin_names}

        # Required: forzado siempre que su dependencyType esté satisfecho. En
        # un grupo de selección única el obligatorio DEFINE el grupo: la
        # selección del usuario en ese grupo (incompatible por cardinalidad)
        # se descarta — instalar dos variantes mutuamente excluyentes es el
        # defecto que esta validación previene.
        required = {p.name for p in eligible if effective_types[p.name] is PluginType.REQUIRED}
        if required and group_type in (GroupType.SELECT_EXACTLY_ONE, GroupType.SELECT_AT_MOST_ONE):
            selected = set(required)
        else:
            selected |= required

        if not selected:
            # Preselección Recommended: sin selección explícita del usuario se
            # elige lo que el autor del mod recomienda.
            selected |= {p.name for p in eligible if effective_types[p.name] is PluginType.RECOMMENDED}

        if group_type == GroupType.SELECT_ALL:
            return plugin_names

        if group_type == GroupType.SELECT_EXACTLY_ONE:
            if len(selected) != 1:
                if plugins and not eligible:
                    # Todas las opciones bloqueadas por dependencyType: el
                    # mensaje genérico confundiría al LLM (no es falta de
                    # selección, es un requisito no satisfecho).
                    result.pending_decisions.append(
                        f"{step_name}/{group_name}: no eligible options (unsatisfied dependencies)"
                    )
                else:
                    if selected:
                        # Selección inválida (>1) en un grupo de selección única:
                        # fail-closed — nada de este grupo se instala.
                        logger.warning(
                            "%s/%s: selección inválida (%d opciones) — se descarta el grupo",
                            step_name,
                            group_name,
                            len(selected),
                        )
                    result.pending_decisions.append(f"{step_name}/{group_name}: must select exactly one")
                return set()
            return selected

        if group_type == GroupType.SELECT_AT_MOST_ONE:
            if len(selected) > 1:
                logger.warning(
                    "%s/%s: selección inválida (%d opciones) — se descarta el grupo",
                    step_name,
                    group_name,
                    len(selected),
                )
                result.pending_decisions.append(f"{step_name}/{group_name}: must select at most one")
                return set()
            return selected

        if group_type == GroupType.SELECT_AT_LEAST_ONE and not selected:
            if plugins and not eligible:
                result.pending_decisions.append(
                    f"{step_name}/{group_name}: no eligible options (unsatisfied dependencies)"
                )
            else:
                result.pending_decisions.append(f"{step_name}/{group_name}: must select at least one")

        return selected

    def _plugin_type_effective(self, plugin: Plugin, flags: dict[str, str]) -> PluginType | None:
        """Tipo efectivo del plugin tras evaluar su ``dependencyType``.

        None = plugin no elegible (no puede instalarse):
        - con ``patterns``: el primero que matchee define el tipo; ``NotUsable``
          o ningún match → no elegible; ``CouldBeUsable`` → Optional.
        - sin ``patterns``: dependencias directas insatisfechas → se aplica
          ``defaultType`` (o no elegible si no hay default); satisfechas → el
          ``type`` normal.
        """
        if plugin.dependency_patterns:
            for pattern in plugin.dependency_patterns:
                if self._evaluate_conditions(pattern.conditions, flags):
                    if pattern.type is PluginType.NOT_USABLE:
                        return None
                    if pattern.type is PluginType.COULD_BE_USABLE:
                        return PluginType.OPTIONAL
                    return pattern.type
            return None

        if plugin.dependency is not None and not self._evaluate_conditions(plugin.dependency, flags):
            return plugin.dependency_default_type

        return plugin.type_descriptor

    def _evaluate_conditions(
        self,
        conditions: CompositeDependency,
        flags: dict[str, str],
    ) -> bool:
        """Evaluate a composite dependency against current flags and file state."""
        results: list[bool] = []

        for fd in conditions.flag_deps:
            results.append(flags.get(fd.flag, "") == fd.value)

        for file_dep in conditions.file_deps:
            # File deps are evaluated against the installed-file state. Without
            # a provider there is no evidence the condition holds — fail-closed.
            if self._file_state is None:
                results.append(False)
            else:
                results.append(self._file_state.file_state(file_dep.file) == file_dep.state)

        for nested in conditions.nested:
            results.append(self._evaluate_conditions(nested, flags))

        if not results:
            return True

        if conditions.operator == "And":
            return all(results)
        return any(results)

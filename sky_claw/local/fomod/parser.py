"""FOMOD XML parser — converts ModuleConfig.xml to Pydantic models.

Uses ``defusedxml.ElementTree`` to guard against XML entity-expansion
(billion-laughs) and external-entity (XXE) attacks.  Malformed elements
are logged and skipped rather than crashing the entire parse.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import defusedxml.ElementTree as DefusedElementTree
from defusedxml import (
    DefusedXmlException,
    DTDForbidden,
    EntitiesForbidden,
    ExternalReferenceForbidden,
)

from sky_claw.antigravity.core.errors import FomodParserSecurityError
from sky_claw.local.fomod.models import (
    CompositeDependency,
    ConditionalPattern,
    ConditionFlag,
    FileDependency,
    FileInstall,
    FileState,
    FlagDependency,
    FomodConfig,
    Group,
    GroupType,
    InstallStep,
    Plugin,
)

if TYPE_CHECKING:
    import pathlib
    import xml.etree.ElementTree as _stdlib_ET

logger = logging.getLogger(__name__)


class FomodParseError(Exception):
    """Raised when the XML cannot be parsed at all."""


def parse_fomod(xml_path: pathlib.Path) -> FomodConfig:
    """Parse a FOMOD ModuleConfig.xml into a :class:`FomodConfig`.

    Args:
        xml_path: Path to the ModuleConfig.xml file.

    Returns:
        Parsed FOMOD configuration.

    Raises:
        FomodParseError: If the XML is fundamentally unparseable.
    """
    try:
        tree = DefusedElementTree.parse(
            xml_path,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden) as exc:
        logger.critical("XML injection attempt detected in FOMOD: %s — %s", xml_path, exc)
        raise FomodParserSecurityError(f"XML security violation in {xml_path}: {exc}") from exc
    except (DefusedElementTree.ParseError, DefusedXmlException) as exc:
        raise FomodParseError(f"Failed to parse XML: {exc}") from exc

    root = tree.getroot()

    module_name = ""
    name_el = root.find("moduleName")
    if name_el is not None and name_el.text:
        module_name = name_el.text.strip()

    required_files = _parse_file_list(root.find("requiredInstallFiles"))
    install_steps = _parse_install_steps(root.find("installSteps"))
    conditional_installs = _parse_conditional_installs(root.find("conditionalFileInstalls"))

    return FomodConfig(
        module_name=module_name,
        required_files=required_files,
        install_steps=install_steps,
        conditional_installs=conditional_installs,
    )


def parse_fomod_string(xml_string: str) -> FomodConfig:
    """Parse FOMOD XML from a string.

    Args:
        xml_string: Raw XML content.

    Returns:
        Parsed FOMOD configuration.
    """
    try:
        root = DefusedElementTree.fromstring(
            xml_string,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DTDForbidden, EntitiesForbidden, ExternalReferenceForbidden) as exc:
        logger.critical("XML injection attempt detected in FOMOD string input — %s", exc)
        raise FomodParserSecurityError(f"XML security violation in FOMOD string: {exc}") from exc
    except (DefusedElementTree.ParseError, DefusedXmlException) as exc:
        raise FomodParseError(f"Failed to parse XML: {exc}") from exc

    module_name = ""
    name_el = root.find("moduleName")
    if name_el is not None and name_el.text:
        module_name = name_el.text.strip()

    required_files = _parse_file_list(root.find("requiredInstallFiles"))
    install_steps = _parse_install_steps(root.find("installSteps"))
    conditional_installs = _parse_conditional_installs(root.find("conditionalFileInstalls"))

    return FomodConfig(
        module_name=module_name,
        required_files=required_files,
        install_steps=install_steps,
        conditional_installs=conditional_installs,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _norm_path(path: str) -> str:
    """Normalize backslashes to forward slashes."""
    return path.replace("\\", "/")


def _parse_file_list(element: _stdlib_ET.Element | None) -> list[FileInstall]:
    """Parse a list of <file> and <folder> elements."""
    if element is None:
        return []
    files: list[FileInstall] = []
    for tag in ("file", "folder"):
        for el in element.findall(tag):
            try:
                source = _norm_path(el.get("source", ""))
                dest = _norm_path(el.get("destination", ""))
                priority = int(el.get("priority", "0"))
                if source:
                    files.append(FileInstall(source=source, destination=dest, priority=priority))
            except (ValueError, TypeError):
                logger.warning("Skipping malformed file element: %s", DefusedElementTree.tostring(el))
    return files


def _parse_install_steps(element: _stdlib_ET.Element | None) -> list[InstallStep]:
    """Parse <installSteps> → list of InstallStep."""
    if element is None:
        return []
    steps: list[InstallStep] = []
    for step_el in element.findall("installStep"):
        try:
            name = step_el.get("name", "Unnamed Step")
            visibility = _parse_composite_dependency(step_el.find("visible"))
            groups = _parse_groups(step_el.find("optionalFileGroups"))
            steps.append(InstallStep(name=name, visibility=visibility, groups=groups))
        except Exception:
            logger.warning("Skipping malformed install step", exc_info=True)
    return steps


def _parse_groups(element: _stdlib_ET.Element | None) -> list[Group]:
    """Parse <optionalFileGroups> → list of Group."""
    if element is None:
        return []
    groups: list[Group] = []
    for group_el in element.findall("group"):
        try:
            name = group_el.get("name", "Unnamed Group")
            type_str = group_el.get("type", "SelectAny")
            group_type = _parse_group_type(type_str)
            plugins = _parse_plugins(group_el.find("plugins"))
            groups.append(Group(name=name, group_type=group_type, plugins=plugins))
        except Exception:
            logger.warning("Skipping malformed group", exc_info=True)
    return groups


def _parse_group_type(type_str: str) -> GroupType:
    """Convert a string to GroupType, defaulting to SelectAny."""
    try:
        return GroupType(type_str)
    except ValueError:
        logger.warning("Unknown group type %r, defaulting to SelectAny", type_str)
        return GroupType.SELECT_ANY


def _parse_plugins(element: _stdlib_ET.Element | None) -> list[Plugin]:
    """Parse <plugins> → list of Plugin."""
    if element is None:
        return []
    plugins: list[Plugin] = []
    for plugin_el in element.findall("plugin"):
        try:
            name = plugin_el.get("name", "Unnamed Plugin")
            desc_el = plugin_el.find("description")
            description = (desc_el.text or "").strip() if desc_el is not None else ""
            image_el = plugin_el.find("image")
            image = _norm_path(image_el.get("path", "")) if image_el is not None else ""

            condition_flags = _parse_condition_flags(plugin_el.find("conditionFlags"))
            files = _parse_file_list(plugin_el.find("files"))

            type_desc = "Optional"
            td_el = plugin_el.find("typeDescriptor")
            if td_el is not None:
                type_el = td_el.find("type")
                if type_el is not None:
                    type_desc = type_el.get("name", "Optional")

            plugins.append(
                Plugin(
                    name=name,
                    description=description,
                    image=image,
                    condition_flags=condition_flags,
                    files=files,
                    type_descriptor=type_desc,
                )
            )
        except Exception:
            logger.warning("Skipping malformed plugin", exc_info=True)
    return plugins


def _parse_condition_flags(element: _stdlib_ET.Element | None) -> list[ConditionFlag]:
    """Parse <conditionFlags> → list of ConditionFlag."""
    if element is None:
        return []
    flags: list[ConditionFlag] = []
    for flag_el in element.findall("flag"):
        name = flag_el.get("name", "")
        value = (flag_el.text or "").strip()
        if name:
            flags.append(ConditionFlag(name=name, value=value))
    return flags


def _parse_composite_dependency(
    element: _stdlib_ET.Element | None,
) -> CompositeDependency | None:
    """Parse a dependency element with nested conditions."""
    if element is None:
        return None

    # Check for <dependencies> child or use the element itself.
    deps_el = element.find("dependencies")
    if deps_el is None:
        deps_el = element

    operator = deps_el.get("operator", "And")
    if operator not in ("And", "Or"):
        operator = "And"

    flag_deps: list[FlagDependency] = []
    file_deps: list[FileDependency] = []
    nested: list[CompositeDependency] = []

    for child in deps_el:
        if child.tag == "flagDependency":
            flag = child.get("flag", "")
            value = child.get("value", "")
            if flag:
                flag_deps.append(FlagDependency(flag=flag, value=value))
        elif child.tag == "fileDependency":
            file_path = _norm_path(child.get("file", ""))
            state_str = child.get("state", "Active")
            try:
                state = FileState(state_str)
            except ValueError:
                state = FileState.ACTIVE
            if file_path:
                file_deps.append(FileDependency(file=file_path, state=state))
        elif child.tag == "dependencies":
            sub = _parse_composite_dependency_direct(child)
            if sub is not None:
                nested.append(sub)

    return CompositeDependency(
        operator=operator,
        flag_deps=flag_deps,
        file_deps=file_deps,
        nested=nested,
    )


def _parse_composite_dependency_direct(
    element: _stdlib_ET.Element,
) -> CompositeDependency | None:
    """Parse a <dependencies> element directly (for nesting)."""
    operator = element.get("operator", "And")
    if operator not in ("And", "Or"):
        operator = "And"

    flag_deps: list[FlagDependency] = []
    file_deps: list[FileDependency] = []
    nested: list[CompositeDependency] = []

    for child in element:
        if child.tag == "flagDependency":
            flag = child.get("flag", "")
            value = child.get("value", "")
            if flag:
                flag_deps.append(FlagDependency(flag=flag, value=value))
        elif child.tag == "fileDependency":
            file_path = _norm_path(child.get("file", ""))
            state_str = child.get("state", "Active")
            try:
                state = FileState(state_str)
            except ValueError:
                state = FileState.ACTIVE
            if file_path:
                file_deps.append(FileDependency(file=file_path, state=state))
        elif child.tag == "dependencies":
            sub = _parse_composite_dependency_direct(child)
            if sub is not None:
                nested.append(sub)

    return CompositeDependency(
        operator=operator,
        flag_deps=flag_deps,
        file_deps=file_deps,
        nested=nested,
    )


def _parse_conditional_installs(
    element: _stdlib_ET.Element | None,
) -> list[ConditionalPattern]:
    """Parse <conditionalFileInstalls> → list of ConditionalPattern."""
    if element is None:
        return []
    patterns_el = element.find("patterns")
    if patterns_el is None:
        return []

    patterns: list[ConditionalPattern] = []
    for pattern_el in patterns_el.findall("pattern"):
        try:
            deps_el = pattern_el.find("dependencies")
            conditions = _parse_composite_dependency_direct(deps_el) if deps_el is not None else CompositeDependency()
            files = _parse_file_list(pattern_el.find("files"))
            patterns.append(ConditionalPattern(conditions=conditions, files=files))
        except Exception:
            logger.warning("Skipping malformed conditional pattern", exc_info=True)
    return patterns

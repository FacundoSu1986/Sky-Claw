"""Pydantic v2 models for FOMOD installer configuration.

Models the structure of a FOMOD ``ModuleConfig.xml`` file:
install steps, groups, plugins, conditions, flags, and file installs.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

import pydantic


class GroupType(StrEnum):
    """Selection constraint for a group of plugins."""

    SELECT_EXACTLY_ONE = "SelectExactlyOne"
    SELECT_AT_MOST_ONE = "SelectAtMostOne"
    SELECT_AT_LEAST_ONE = "SelectAtLeastOne"
    SELECT_ALL = "SelectAll"
    SELECT_ANY = "SelectAny"


class FileState(StrEnum):
    """Expected state of a file dependency."""

    ACTIVE = "Active"
    INACTIVE = "Inactive"
    MISSING = "Missing"


# ------------------------------------------------------------------
# Dependency conditions
# ------------------------------------------------------------------


class FlagDependency(pydantic.BaseModel):
    """A condition on a previously set flag."""

    model_config = pydantic.ConfigDict(strict=True)

    flag: str
    value: str


class FileDependency(pydantic.BaseModel):
    """A condition on the state of a file."""

    model_config = pydantic.ConfigDict(strict=True)

    file: str
    state: FileState = FileState.ACTIVE


class CompositeDependency(pydantic.BaseModel):
    """A group of conditions combined with AND or OR."""

    model_config = pydantic.ConfigDict(strict=True)

    operator: Literal["And", "Or"] = "And"
    flag_deps: list[FlagDependency] = pydantic.Field(default_factory=list)
    file_deps: list[FileDependency] = pydantic.Field(default_factory=list)
    nested: list[CompositeDependency] = pydantic.Field(default_factory=list)


# ------------------------------------------------------------------
# File install directives
# ------------------------------------------------------------------


class FileInstall(pydantic.BaseModel):
    """A single file/folder to install."""

    model_config = pydantic.ConfigDict(strict=True)

    source: str
    destination: str = ""
    priority: int = 0


# ------------------------------------------------------------------
# Plugin / Group / Step
# ------------------------------------------------------------------


class ConditionFlag(pydantic.BaseModel):
    """A flag to set when a plugin is selected."""

    model_config = pydantic.ConfigDict(strict=True)

    name: str
    value: str = ""


class Plugin(pydantic.BaseModel):
    """A selectable option within a group."""

    model_config = pydantic.ConfigDict(strict=True)

    name: str
    description: str = ""
    image: str = ""
    condition_flags: list[ConditionFlag] = pydantic.Field(default_factory=list)
    files: list[FileInstall] = pydantic.Field(default_factory=list)
    type_descriptor: str = "Optional"


class Group(pydantic.BaseModel):
    """A named group of plugins with a selection type."""

    model_config = pydantic.ConfigDict(strict=True)

    name: str
    group_type: GroupType = GroupType.SELECT_ANY
    plugins: list[Plugin] = pydantic.Field(default_factory=list)


class InstallStep(pydantic.BaseModel):
    """A single page/step in the FOMOD installer wizard."""

    model_config = pydantic.ConfigDict(strict=True)

    name: str
    visibility: CompositeDependency | None = None
    groups: list[Group] = pydantic.Field(default_factory=list)


# ------------------------------------------------------------------
# Conditional file installs
# ------------------------------------------------------------------


class ConditionalPattern(pydantic.BaseModel):
    """A pattern that installs files when conditions are met."""

    model_config = pydantic.ConfigDict(strict=True)

    conditions: CompositeDependency = pydantic.Field(default_factory=CompositeDependency)
    files: list[FileInstall] = pydantic.Field(default_factory=list)


# ------------------------------------------------------------------
# Root config
# ------------------------------------------------------------------


class FomodConfig(pydantic.BaseModel):
    """Root model for a parsed FOMOD ModuleConfig.xml."""

    model_config = pydantic.ConfigDict(strict=True)

    module_name: str = ""
    required_files: list[FileInstall] = pydantic.Field(default_factory=list)
    install_steps: list[InstallStep] = pydantic.Field(default_factory=list)
    conditional_installs: list[ConditionalPattern] = pydantic.Field(default_factory=list)

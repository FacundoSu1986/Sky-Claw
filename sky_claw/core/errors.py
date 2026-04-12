"""Base exception hierarchy for Sky-Claw."""

from __future__ import annotations


class AppNexusError(Exception):
    """Root exception for all Sky-Claw application errors."""


class FomodParserSecurityError(AppNexusError):
    """XML security violation detected during FOMOD parsing.

    Raised when ``defusedxml`` detects a forbidden DTD declaration,
    entity definition, or external reference in a FOMOD XML file.
    The SupervisorAgent should abort the mod installation when this
    is raised.
    """

"""Compatibility shims for upstream third-party dependencies.

Centralizes polyfills required to keep transitively-pulled libraries
(currently `vbuild` via NiceGUI) running on newer Python interpreters.
Removable as a single unit once upstream catches up.
"""

from __future__ import annotations

import importlib.util
import pkgutil
import sys
from typing import Any


def _pkgutil_find_loader_polyfill(name: str) -> Any:
    """Polyfill for deprecated pkgutil.find_loader.

    Python 3.14 removed ``pkgutil.find_loader(name)`` which returned a loader
    object (or None). The modern equivalent is ``importlib.util.find_spec(name)``
    which returns a ModuleSpec (or None). This wrapper calls find_spec and
    extracts the loader from the spec to maintain backward compatibility with
    code that expects a loader object.

    Args:
        name: Module name to search for.

    Returns:
        Loader object (or None) matching the original ``pkgutil.find_loader`` API.
    """
    spec = importlib.util.find_spec(name)
    return spec.loader if spec is not None else None


def setup_python_compat() -> None:
    """Apply global polyfills for deprecated Python features used by 3rd-party libs.

    - Python 3.14 removed ``pkgutil.find_loader`` (deprecated since 3.4).
      ``vbuild`` (pulled by NiceGUI) still calls it; this installs a wrapper
      that calls ``importlib.util.find_spec`` and extracts the loader to match
      the original API contract.

    Idempotent: safe to call multiple times. No-op on Python < 3.14.
    """
    if sys.version_info >= (3, 14):
        if not hasattr(pkgutil, "find_loader"):
            pkgutil.find_loader = _pkgutil_find_loader_polyfill  # type: ignore[attr-defined]

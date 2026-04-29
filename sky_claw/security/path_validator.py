"""Path Isolation – sandboxing for all file-system I/O.

Ensures Sky-Claw can only read, write, or extract files within the
configured sandbox roots (MO2 directory and ``/tmp/sky_claw``).
Prevents path-traversal attacks via ``..`` components.
"""

from __future__ import annotations

import functools
import inspect
import pathlib
from typing import TYPE_CHECKING, ParamSpec, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

P = ParamSpec("P")
R = TypeVar("R")


class PathViolationError(Exception):
    """Raised when an I/O path escapes the sandbox."""


# ---------------------------------------------------------------------------
# Standalone helpers — usable without a full PathValidator instance
# ---------------------------------------------------------------------------


def assert_safe_component(name: str | None, *, field: str = "name") -> str:
    """Validate that *name* is a single safe path component.

    A safe component:
    - is a non-empty ``str``
    - is not the traversal sentinel ``"."`` or ``".."``
    - contains no path separators (``/`` or ``\\``)
    - contains no NUL bytes
    - contains no ASCII control characters (U+0000–U+001F)

    Unicode letters, digits, spaces, hyphens, underscores, dots embedded
    in a longer name (e.g. ``"mod_v1.2"``) are all accepted.

    Parameters
    ----------
    name:
        The component string to validate.
    field:
        Human-readable label for error messages (e.g. ``"mod_name"``).

    Returns
    -------
    str
        The original *name*, unchanged, if it passes all checks.

    Raises
    ------
    PathViolationError
        When any rule is violated.
    """
    if name is None or not isinstance(name, str):
        raise PathViolationError(f"{field}: expected a non-empty string, got {type(name).__name__}")
    if not name:
        raise PathViolationError(f"{field}: must not be empty")
    if name in (".", ".."):
        raise PathViolationError(f"{field}: traversal segment {name!r} is not a valid component")
    if "/" in name or "\\" in name:
        raise PathViolationError(f"{field}: path separators are not allowed in a component, got {name!r}")
    if "\x00" in name:
        raise PathViolationError(f"{field}: NUL byte is not allowed")
    for ch in name:
        if ord(ch) < 0x20:
            raise PathViolationError(f"{field}: control character {ch!r} (U+{ord(ch):04X}) is not allowed")
    return name


def safe_join(root: str | pathlib.Path, untrusted: str | pathlib.Path) -> pathlib.Path:
    """Join *untrusted* onto *root* and verify the result stays inside *root*.

    Unlike the ``/`` operator on :class:`pathlib.Path`, this function fully
    resolves both paths and rejects any result that escapes *root* — whether
    via ``..`` segments, absolute overrides, or symlink chains.

    Parameters
    ----------
    root:
        The trusted sandbox root directory.
    untrusted:
        An attacker-controlled relative path to append.

    Returns
    -------
    pathlib.Path
        The resolved, sandboxed path.

    Raises
    ------
    PathViolationError
        When the resolved candidate escapes *root*.
    """
    root_resolved = pathlib.Path(root).resolve()
    candidate = (root_resolved / untrusted).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise PathViolationError(
            f"Untrusted path {str(untrusted)!r} escapes root {str(root_resolved)!r} "
            f"(would resolve to {str(candidate)!r})"
        ) from exc
    return candidate


class PathValidator:
    """Validates that a path is within the allowed sandbox roots.

    Parameters
    ----------
    roots:
        Iterable of directory paths that define the sandbox.  Paths are
        resolved to absolute form at construction time.
    """

    def __init__(self, roots: Iterable[pathlib.Path]) -> None:
        resolved = [r.resolve() for r in roots]
        if not resolved:
            raise ValueError("At least one sandbox root is required")
        self._roots: tuple[pathlib.Path, ...] = tuple(resolved)

    @property
    def roots(self) -> tuple[pathlib.Path, ...]:
        return self._roots

    def validate(self, path: str | pathlib.Path, *, strict_symlink: bool = True) -> pathlib.Path:
        """Return the resolved *path* if it is inside the sandbox.

        Raises :class:`PathViolationError` otherwise.
        """
        target = pathlib.Path(path)

        # Reject obvious traversal attempts before resolving.
        if ".." in target.parts:
            raise PathViolationError(f"Path traversal component ('..') detected in: {path}")

        # Require symlinks to explicitly resolve inside the sandbox
        if strict_symlink and target.is_symlink():
            try:
                symlink_target = target.resolve(strict=True)
            except FileNotFoundError as e:
                raise PathViolationError(f"Symlink target not found: {path} -> {e}") from e

            # Ensure the symlink target itself is within the sandbox
            is_symlink_valid = False
            for root in self._roots:
                try:
                    symlink_target.relative_to(root)
                    is_symlink_valid = True
                    break
                except ValueError:
                    continue

            if not is_symlink_valid:
                raise PathViolationError(f"Symlink strictly escapes sandbox: {path} -> {symlink_target}")

        resolved = target.resolve()

        for root in self._roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue

        raise PathViolationError(f"Path '{resolved}' is outside all sandbox roots: {self._roots}")


def sandboxed_io(
    roots: Iterable[pathlib.Path],
    *,
    arg_name: str = "path",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that validates a *path* argument before executing the function.

    Parameters
    ----------
    roots:
        Sandbox roots that determine the safe environment.
    arg_name:
        Name of the keyword argument to validate.  Defaults to ``"path"``.

    Example::

        @sandboxed_io()
        def extract_archive(path: pathlib.Path) -> None:
            ...
    """
    validator = PathValidator(roots)

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        # Resolve the signature once at decoration time so instance methods
        # (where args[0] is `self`) and non-first-position path arguments
        # are bound to the correct parameter name.
        sig = inspect.signature(fn)
        if arg_name not in sig.parameters:
            raise TypeError(
                f"sandboxed_io: function '{fn.__name__}' has no parameter named '{arg_name}' — cannot validate path"
            )

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                bound = sig.bind_partial(*args, **kwargs)
            except TypeError as e:
                raise PathViolationError(
                    f"Sandboxed function '{fn.__name__}' called with invalid arguments: {e}"
                ) from e
            raw_path = bound.arguments.get(arg_name)
            if raw_path is None:
                raise PathViolationError(
                    f"Sandboxed function '{fn.__name__}' called without required path argument '{arg_name}'"
                )
            validator.validate(raw_path)  # type: ignore[arg-type]
            return fn(*args, **kwargs)

        return wrapper

    return decorator

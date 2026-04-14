"""Path Isolation – sandboxing for all file-system I/O.

Ensures Sky-Claw can only read, write, or extract files within the
configured sandbox roots (MO2 directory and ``/tmp/sky_claw``).
Prevents path-traversal attacks via ``..`` components.
"""

from __future__ import annotations

import functools
import pathlib
from typing import TYPE_CHECKING, ParamSpec, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

P = ParamSpec("P")
R = TypeVar("R")


class PathViolation(Exception):
    """Raised when an I/O path escapes the sandbox."""


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

    def validate(
        self, path: str | pathlib.Path, *, strict_symlink: bool = True
    ) -> pathlib.Path:
        """Return the resolved *path* if it is inside the sandbox.

        Raises :class:`PathViolation` otherwise.
        """
        target = pathlib.Path(path)

        # Reject obvious traversal attempts before resolving.
        if ".." in target.parts:
            raise PathViolation(f"Path traversal component ('..') detected in: {path}")

        # Require symlinks to explicitly resolve inside the sandbox
        if strict_symlink and target.is_symlink():
            try:
                symlink_target = target.resolve(strict=True)
            except FileNotFoundError as e:
                raise PathViolation(f"Symlink target not found: {path} -> {e}") from e

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
                raise PathViolation(
                    f"Symlink strictly escapes sandbox: {path} -> {symlink_target}"
                )

        resolved = target.resolve()

        for root in self._roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue

        raise PathViolation(
            f"Path '{resolved}' is outside all sandbox roots: {self._roots}"
        )


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
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # Try to pull the path from kwargs first, then positional.
            raw_path = kwargs.get(arg_name)
            if raw_path is None and args:
                raw_path = args[0]  # type: ignore[assignment]
            if raw_path is not None:
                validator.validate(raw_path)
            return fn(*args, **kwargs)

        return wrapper

    return decorator

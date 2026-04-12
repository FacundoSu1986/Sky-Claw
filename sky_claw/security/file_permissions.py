"""Cross-platform file permission enforcement.

On Windows, uses icacls to restrict file access to the current user.
On POSIX, uses os.chmod with restrictive permissions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"


def restrict_to_owner(path: Path) -> None:
    """Restrict *path* so only the current user can read/write it.

    On Windows, uses ``icacls`` to set owner-only permissions.
    On POSIX, uses ``chmod 0o600`` for files and ``0o700`` for directories.
    """
    if not path.exists():
        return

    if _IS_WINDOWS:
        _restrict_windows(path)
    else:
        mode = 0o700 if path.is_dir() else 0o600
        try:
            os.chmod(path, mode)
        except OSError as exc:
            logger.warning("chmod(%s, %o) failed: %s", path, mode, exc)


def _restrict_windows(path: Path) -> None:
    """Use icacls to set owner-only ACL on Windows."""
    try:
        username = os.environ.get("USERNAME", "")
        if not username:
            logger.warning("Cannot determine USERNAME for ACL on %s", path)
            return
        # Reset inheritance, grant only current user full control
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{username}:(F)",
                "/remove",
                "Everyone",
                "/remove",
                "Users",
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ) as exc:
        logger.warning("icacls ACL enforcement failed for %s: %s", path, exc)


async def restrict_to_owner_async(path: Path) -> None:
    """Async variant of restrict_to_owner for use inside coroutines.

    Delegates to a thread executor so the event loop is never blocked
    by the ``subprocess.run`` (icacls) or ``os.chmod`` calls.

    Args:
        path: The file or directory to restrict.
    """
    await asyncio.to_thread(restrict_to_owner, path)

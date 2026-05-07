"""Cross-platform file permission enforcement.

On Windows, uses icacls to restrict file access to the current user.
On POSIX, uses os.chmod with restrictive permissions.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


def _get_current_user_sid() -> str | None:
    """Return the current user's SID string via PowerShell, or None on failure.

    Using the SID directly with icacls ``*SID:(F)`` syntax avoids the
    username→SID lookup that fails with exit 1332 on domain-joined machines
    and service accounts.
    """
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        sid = result.stdout.strip()
        return sid if sid else None
    except Exception as exc:
        logger.warning("Could not resolve current user SID: %s", exc)
        return None


def _restrict_windows(path: Path) -> None:
    """Use icacls to set owner-only ACL on Windows.

    Strategy:
    1. Try username-based ``icacls /grant:r username:(F)`` — works on local accounts.
    2. On failure (e.g., exit 1332 on domain/service accounts), resolve the SID
       via PowerShell and retry ``icacls /grant:r *SID:(F)`` — bypasses the
       username→SID mapping that fails in those environments.
    3. If both icacls invocations fail, log CRITICAL — there is no meaningful
       fallback on Windows because os.chmod only sets the read-only attribute
       and does NOT enforce owner-only access via DACL.
    """
    # --- Attempt 1: username-based grant ---
    try:
        username = getpass.getuser()
    except Exception:
        logger.warning("Cannot determine username for ACL on %s — skipping to SID-based grant", path)
        username = None

    if username is not None:
        try:
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
            return  # success
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("icacls (username) failed for %s: %s — retrying with SID", path, exc)

    # --- Attempt 2: SID-based grant (bypasses exit-1332 SID resolution failure) ---
    sid = _get_current_user_sid()
    if sid is not None:
        try:
            subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{sid}:(F)",
                    "/remove",
                    "Everyone",
                    "/remove",
                    "Users",
                ],
                capture_output=True,
                check=True,
                timeout=10,
            )
            return  # success
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.critical(
                "SECURITY: Both icacls attempts failed for %s — file may be world-readable: %s",
                path,
                exc,
            )
            return

    # SID resolution itself failed — no icacls possible
    logger.critical(
        "SECURITY: Could not set owner-only ACL on %s — SID resolution failed and "
        "no icacls fallback is available. File may be world-readable.",
        path,
    )


async def restrict_to_owner_async(path: Path) -> None:
    """Async variant of restrict_to_owner for use inside coroutines.

    Delegates to a thread executor so the event loop is never blocked
    by the ``subprocess.run`` (icacls) or ``os.chmod`` calls.

    Args:
        path: The file or directory to restrict.
    """
    await asyncio.to_thread(restrict_to_owner, path)

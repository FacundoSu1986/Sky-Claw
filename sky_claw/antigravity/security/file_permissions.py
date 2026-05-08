"""Cross-platform file permission enforcement.

On Windows, uses icacls to restrict file access to the current user
AND post-validates the effective DACL. On POSIX, uses os.chmod with
restrictive permissions.

Both paths are fail-closed: if the requested permission state cannot
be verified after the action, the artifact is destroyed (files only —
directories are preserved to avoid wiping user data) and a
``PermissionError`` is raised.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Match `IDENTIFIER:(PERMS)` ACE tokens in `icacls <path>` output.
# IDENTIFIER may be `username`, `DOMAIN\username`, `*S-1-...`, or `S-1-...`.
# The character class excludes whitespace, colons, and parens, so the
# leading path on the first line (e.g. ``C:\path\file``) never matches —
# paths contain colons but are not followed by ``(...)``.
_ACE_TOKEN_RE = re.compile(r"([^\s:()]+):\(([^)]+)\)")


def restrict_to_owner(path: Path) -> None:
    """Restrict *path* so only the current user can read/write it.

    On Windows, uses ``icacls`` to set owner-only permissions and then
    post-validates the effective DACL. On POSIX, uses ``chmod 0o600``
    for files and ``0o700`` for directories.

    Raises:
        PermissionError: if the owner-only state cannot be enforced or
            verified. Files are unlinked before raising so a leaky
            artifact never persists; directories are preserved (callers
            are expected to handle higher-level cleanup themselves).
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
            logger.error("chmod(%s, %o) failed: %s", path, mode, exc)
            raise PermissionError(f"Owner-only chmod failed for {path}") from exc


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


def _dacl_is_owner_only(icacls_output: str, allowed_identifiers: list[str]) -> bool:
    """Parse `icacls <path>` stdout and assert every ACE belongs to an allowed identifier.

    Args:
        icacls_output: stdout from ``icacls <path>`` (no flags).
        allowed_identifiers: list of acceptable identifiers — typically
            ``[username]``, ``[username, sid]``, or ``[sid]``. Comparison is
            case-insensitive and tolerates ``DOMAIN\\`` prefixes and the
            leading ``*`` used by icacls grant syntax for SIDs.

    Returns:
        True iff at least one ACE was found and every ACE's identifier
        matches one of ``allowed_identifiers``. Returns False on empty
        output, missing identifiers, or any non-allowed ACE (e.g.
        ``Everyone``, ``BUILTIN\\Users``, ``NT AUTHORITY\\SYSTEM``,
        ``BUILTIN\\Administrators``).
    """
    if not allowed_identifiers:
        return False

    allowed_full: set[str] = set()
    allowed_bare: set[str] = set()
    for ident in allowed_identifiers:
        if not ident:
            continue
        normalized = ident.lstrip("*").lower()
        allowed_full.add(normalized)
        # Bare form (strip DOMAIN\ prefix) so `username:(F)` matches
        # when icacls resolves the SID and drops the domain.
        allowed_bare.add(normalized.split("\\")[-1])

    found_any = False
    for match in _ACE_TOKEN_RE.finditer(icacls_output):
        ident = match.group(1).lstrip("*").lower()
        bare = ident.split("\\")[-1]
        if ident not in allowed_full and bare not in allowed_bare:
            return False
        found_any = True

    return found_any


def _fail_closed(path: Path, reason: str) -> None:
    """Destroy a leaky artifact and raise PermissionError.

    Files are unlinked (``missing_ok=True``); directories are *not*
    removed recursively — wiping ``~/.sky_claw/`` would destroy the
    credential vault DB and salt backups. The PermissionError signal
    lets higher-level callers decide whether to recover, regenerate,
    or abort startup.

    Always raises; never returns.
    """
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
    except OSError as exc:
        # Best-effort destruction; the security error must still propagate.
        logger.warning("fail_closed: unlink(%s) failed: %s", path, exc)

    raise PermissionError(
        f"Owner-only ACL enforcement failed for {path}; "
        f"artifact destroyed to prevent leak. Reason: {reason}"
    )


def _verify_dacl(path: Path, allowed_identifiers: list[str]) -> None:
    """Run ``icacls <path>`` and assert the effective DACL is owner-only.

    Calls ``_fail_closed`` (which raises) if the verification call itself
    fails or the parsed DACL contains any non-allowed ACE.

    The ``LANGUAGE=en_US`` override is best-effort — icacls on Windows
    does not always honor locale env vars, but the parser only inspects
    the structured ``IDENTIFIER:(PERMS)`` tokens, which are
    locale-independent. The trailing ``Successfully processed ...``
    summary may be localized without affecting validation.
    """
    try:
        result = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            check=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "LANGUAGE": "en_US"},
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _fail_closed(path, f"icacls verification failed: {exc}")

    if not _dacl_is_owner_only(result.stdout, allowed_identifiers):
        _fail_closed(
            path,
            f"DACL contains non-owner ACEs after hardening: {result.stdout!r}",
        )


def _restrict_windows(path: Path) -> None:
    """Use icacls to set owner-only ACL on Windows, then post-validate.

    Strategy:
    1. Try username-based ``icacls /grant:r username:(F)`` — works on local accounts.
    2. On failure (e.g., exit 1332 on domain/service accounts), resolve the SID
       via PowerShell and retry ``icacls /grant:r *SID:(F)`` — bypasses the
       username→SID mapping that fails in those environments.
    3. After each successful icacls invocation, run ``icacls <path>`` and
       parse the effective DACL — if any ACE references a principal other
       than the current user (Everyone, BUILTIN\\Users, SYSTEM,
       BUILTIN\\Administrators, etc.), destroy the artifact and raise.
    4. If both icacls invocations fail OR neither verification passes,
       log CRITICAL and fail closed — there is no meaningful fallback on
       Windows because os.chmod only sets the read-only attribute and
       does NOT enforce owner-only access via DACL.
    """
    # --- Resolve username (best-effort; SID path is the safety net) ---
    try:
        username = getpass.getuser()
    except Exception:
        logger.warning("Cannot determine username for ACL on %s — skipping to SID-based grant", path)
        username = None

    # --- Attempt 1: username-based grant + verify ---
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
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("icacls (username) failed for %s: %s — retrying with SID", path, exc)
        else:
            # Hardening returned 0; verify effective DACL before declaring success.
            # _verify_dacl raises (with cleanup) on mismatch — do NOT swallow.
            _verify_dacl(path, [username])
            return

    # --- Attempt 2: SID-based grant + verify ---
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
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.critical(
                "SECURITY: Both icacls attempts failed for %s — file may be world-readable: %s",
                path,
                exc,
            )
            _fail_closed(path, f"both icacls attempts failed: {exc}")
        else:
            allowed = [username, sid] if username else [sid]
            _verify_dacl(path, allowed)
            return

    # SID resolution itself failed — no icacls possible
    logger.critical(
        "SECURITY: Could not set owner-only ACL on %s — SID resolution failed and "
        "no icacls fallback is available. File may be world-readable.",
        path,
    )
    _fail_closed(path, "SID resolution failed and no icacls fallback available")


async def restrict_to_owner_async(path: Path) -> None:
    """Async variant of restrict_to_owner for use inside coroutines.

    Delegates to a thread executor so the event loop is never blocked
    by the ``subprocess.run`` (icacls) or ``os.chmod`` calls.

    Args:
        path: The file or directory to restrict.

    Raises:
        PermissionError: propagated from the threaded ``restrict_to_owner``
            call — same fail-closed contract as the sync variant.
    """
    await asyncio.to_thread(restrict_to_owner, path)

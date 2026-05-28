"""Cross-platform file permission enforcement.

On Windows, uses icacls to restrict file access to the current user
AND post-validates the effective DACL.  On POSIX, uses os.chmod with
restrictive permissions.

**Windows** is fail-closed: if the DACL cannot be verified as owner-only
after hardening, the file artifact is destroyed (unlinked) and a
``PermissionError`` is raised.  Directories are never recursively deleted —
callers handle higher-level cleanup.

**POSIX** raises ``PermissionError`` on ``os.chmod`` failure but does *not*
unlink the file — the chmod failure itself indicates a permission problem on
the filesystem, not a leaked secret.
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

    On Windows, uses ``icacls`` to set owner-only permissions and then
    post-validates the effective DACL. On POSIX, uses ``chmod 0o600``
    for files and ``0o700`` for directories.

    T1-04 — Symlink hardening: if *path* is a symbolic link, refuse with
    ``PermissionError`` instead of following the link.  An attacker who
    can race-swap *path* to a symlink between ``exists()`` and ``chmod``
    would otherwise be able to alter the permissions of the link's target
    (potential 0600 on ``/etc/shadow`` etc.).  Callers must hand us real
    paths; this is fail-closed by design.

    Raises:
        PermissionError: if *path* is a symlink, or if the owner-only state
            cannot be enforced or verified. Files are unlinked before
            raising so a leaky artifact never persists; directories are
            preserved (callers are expected to handle higher-level cleanup
            themselves).
    """
    if not path.exists():
        return

    # T1-04: bloquear symlinks ANTES de cualquier llamada a icacls/chmod.
    # ``Path.is_symlink()`` no sigue el link (usa lstat internamente), por lo
    # que un symlink colgante también se detecta correctamente.
    if path.is_symlink():
        logger.error("Refusing to harden symlink %s — possible TOCTOU attack", path)
        raise PermissionError(
            f"Refusing to harden symlink {path} — possible TOCTOU attack"
        )

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
    """Parse ``icacls <path>`` stdout and assert every ACE belongs to an allowed identifier.

    Uses line-based parsing around the ``:(`` separator so that
    space-containing ACE principals such as ``NT AUTHORITY\\SYSTEM`` and
    ``CREATOR OWNER`` are extracted in full rather than being split at the
    embedded space.

    Args:
        icacls_output: stdout from ``icacls <path>`` (no flags).
        allowed_identifiers: list of acceptable identifiers — typically
            ``[username]``, ``[username, sid]``, or ``[sid]``. Comparison is
            case-insensitive; ``DOMAIN\\`` prefixes and the leading ``*`` used
            by icacls SID grant syntax are normalised away before comparison.

    Returns:
        ``True`` iff at least one ACE was found and *every* ACE's identifier
        matches one of ``allowed_identifiers``.  Returns ``False`` on empty
        output, an empty allowed list, or any non-allowed ACE (e.g.
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
        # when icacls resolves the SID and drops the domain prefix.
        allowed_bare.add(normalized.split("\\")[-1])

    found_any = False

    # Maps bare names → the first full qualified form seen via bare-name match.
    # When the caller supplies only a bare username (e.g. "alice" from
    # getpass.getuser()), icacls renders the ACE as "DOMAIN\alice:(F)".
    # We accept that via the bare fallback, but record the domain so that a
    # second ACE with the same bare name under a *different* domain
    # (e.g. "EVIL\alice:(F)" on a domain-joined host) is detected and rejected.
    bare_domain_map: dict[str, str] = {}

    for line in icacls_output.splitlines():
        # Skip lines that have no ACE pattern at all.
        if ":(" not in line:
            continue

        # Determine whether this is an indented ACE-only line or the path line
        # (the first line emitted by icacls, which carries the file path before
        # the first ACE).  Indented lines start with whitespace; the path line
        # does not.
        is_path_line = bool(line) and not line[0].isspace()

        # Split on ":(" to locate every ACE boundary on this line.
        # For "  NT AUTHORITY\SYSTEM:(F)(OI)":
        #   parts = ["  NT AUTHORITY\SYSTEM", "F)(OI)"]
        # For "C:\file alice:(F)":
        #   parts = ["C:\file alice", "F)"]
        # For "  alice:(F) BUILTIN\Admins:(F)":
        #   parts = ["  alice", "F) BUILTIN\Admins", "F)"]
        parts = line.split(":(")

        for i, segment in enumerate(parts[:-1]):
            # Locate the identifier preceding this ":(":
            #
            # Case 1 — after a previous ACE on the same line (i > 0, or i == 0
            #           when the segment contains a closing paren from the path):
            #   The previous ACE's permissions end with ")".  Everything after
            #   the last ")" is the next identifier (may contain spaces, e.g.
            #   "F) NT AUTHORITY\SYSTEM" → identifier = "NT AUTHORITY\SYSTEM").
            #
            # Case 2 — first segment on an indented line (no previous ACE):
            #   The full stripped segment is the identifier (handles
            #   "  NT AUTHORITY\SYSTEM" → "NT AUTHORITY\SYSTEM").
            #
            # Case 3 — first segment on the path line (is_path_line, i == 0):
            #   Format is "C:\path\file IDENTIFIER".  The identifier is the
            #   last whitespace-delimited token.  Space-containing identifiers
            #   on the path line are not expected after our /inheritance:r
            #   /grant:r hardening (only the owner ACE appears there), so the
            #   last-token heuristic is sufficient in practice.

            last_paren = segment.rfind(")")
            if last_paren != -1:
                # Case 1: identifier follows the last closing paren
                candidate = segment[last_paren + 1 :].strip()
            elif i == 0 and not is_path_line:
                # Case 2: indented line, full stripped segment is the identifier
                candidate = segment.strip()
            else:
                # Case 3: path line, take the last whitespace-delimited token
                candidate = segment.strip().split()[-1] if segment.strip() else ""

            if not candidate:
                continue

            ident = candidate.lstrip("*").lower()
            bare = ident.split("\\")[-1]

            if ident in allowed_full:
                # Exact (possibly domain-qualified) match.
                found_any = True
            elif bare in allowed_bare:
                # Bare-name fallback: icacls rendered our grant as DOMAIN\alice.
                # Guard against domain collisions on domain-joined hosts:
                # if the same bare name already appeared under a *different*
                # domain-qualified form, treat it as a non-owner ACE.
                prev = bare_domain_map.get(bare)
                if prev is not None and prev != ident:
                    return False  # CORP\alice already seen, now EVIL\alice
                bare_domain_map[bare] = ident
                found_any = True
            else:
                return False

    return found_any


def _fail_closed(path: Path, reason: str) -> None:
    """Destroy a leaky artifact and raise PermissionError.

    Files are unlinked (``missing_ok=True``); directories are *not*
    removed recursively — wiping ``~/.sky_claw/`` would destroy the
    credential vault DB and salt backups. The PermissionError signal
    lets higher-level callers decide whether to recover, regenerate,
    or abort startup.

    The raised message reflects the *actual* deletion outcome so callers
    and audit logs can distinguish a clean destroy from a best-effort
    attempt that was blocked (e.g. a locked file on Windows).

    Always raises; never returns.
    """
    destroyed = False
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
            destroyed = True
    except OSError as exc:
        # Best-effort destruction; the security error must still propagate.
        logger.warning("fail_closed: unlink(%s) failed: %s", path, exc)

    outcome = "artifact destroyed" if destroyed else "artifact destruction attempted"
    raise PermissionError(f"Owner-only ACL enforcement failed for {path}; {outcome} to prevent leak. Reason: {reason}")


def _verify_dacl(path: Path, allowed_identifiers: list[str]) -> None:
    """Run ``icacls <path>`` and assert the effective DACL is owner-only.

    Calls ``_fail_closed`` (which raises) if the verification call itself
    fails or the parsed DACL contains any non-allowed ACE.

    Decoding uses the platform default (Windows ANSI code page) so that
    non-ASCII account names — e.g. CJK or Cyrillic usernames — are read
    losslessly.  Hard-coding UTF-8 + errors=replace would corrupt such names
    and cause false-fail / spurious file deletion for valid owner-only ACLs.
    The ``LANGUAGE=en_US`` override is best-effort for the summary line;
    the structured ``IDENTIFIER:(PERMS)`` tokens are locale-independent.
    """
    try:
        result = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            check=True,
            timeout=10,
            text=True,  # system ANSI code page — handles non-ASCII account names
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
                    # Strip well-known system principals that icacls leaves in
                    # place when they hold pre-existing explicit ACEs.  On a
                    # fresh GitHub Actions runner (and many corporate builds),
                    # ~/.sky_claw retains NT AUTHORITY\SYSTEM and
                    # BUILTIN\Administrators ACEs after /inheritance:r because
                    # those were explicit — not inherited — on the directory.
                    # Removing them here produces a truly owner-only DACL.
                    # icacls /remove is a no-op (exit 0) if the principal is
                    # absent, so this is safe on all environments.
                    "/remove",
                    "NT AUTHORITY\\SYSTEM",
                    "/remove",
                    "BUILTIN\\Administrators",
                    "/remove",
                    "CREATOR OWNER",
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
                    "/remove",
                    "NT AUTHORITY\\SYSTEM",
                    "/remove",
                    "BUILTIN\\Administrators",
                    "/remove",
                    "CREATOR OWNER",
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

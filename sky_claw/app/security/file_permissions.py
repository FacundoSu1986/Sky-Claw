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
import re
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
    post-validates the effective DACL. On POSIX, opens the file with
    ``O_NOFOLLOW`` and uses ``fchmod`` on the descriptor so the operation
    is atomic against symlink swap races.

    T1-04 — Symlink hardening (review PR #141): refuse symlinks and use
    no-follow descriptor-based chmod on POSIX so the TOCTOU window described
    in the docstring is actually closed.

    **PR #141 review fixes**:

    1. ``is_symlink()`` se chequea ANTES de ``exists()`` — para un dangling
       symlink ``exists()`` retorna False y antes hubieramos vuelto silenciosamente.
       Ahora cualquier symlink (incluso colgante) lanza ``PermissionError``.
    2. POSIX path ahora usa ``os.open(O_NOFOLLOW)`` + ``os.fchmod(fd)``.
       El kernel rechaza el open si el path es un symlink en el ultimo
       componente, cerrando la ventana TOCTOU entre el check y el chmod.

    Raises:
        PermissionError: si *path* es un symlink (incluso colgante), o si
            el estado owner-only no puede ser aplicado o verificado. Files
            son unlinked antes de raise para que un artefacto leaky nunca
            persista; directorios se preservan (callers manejan cleanup).
    """
    # T1-04 (review): is_symlink ANTES de exists. lstat() no sigue el link,
    # asi que detecta dangling symlinks tambien (para esos exists()==False
    # y hubieramos retornado silenciosamente — bug del fix original).
    try:
        is_link = path.is_symlink()
    except OSError:
        is_link = False
    if is_link:
        logger.error("Refusing to harden symlink %s — possible TOCTOU attack", path)
        raise PermissionError(f"Refusing to harden symlink {path} — possible TOCTOU attack")

    if not path.exists():
        return

    if _IS_WINDOWS:
        _restrict_windows(path)
    else:
        _restrict_posix_atomic(path)


def _restrict_posix_atomic(path: Path) -> None:
    """Atomic owner-only chmod on POSIX using ``O_NOFOLLOW`` + ``fchmod``.

    Cierra el TOCTOU residual entre ``is_symlink()`` y ``chmod``: si un
    atacante race-swapea *path* a un symlink despues del check, el
    ``os.open(..., O_NOFOLLOW)`` falla con ``ELOOP`` y abortamos. Sin esto,
    ``os.chmod`` por path seguiria el symlink y cambiaria permisos del target.

    En plataformas sin ``O_NOFOLLOW`` (Windows runtime real, aunque
    `_restrict_windows` se usa alli), cae a ``os.chmod`` con la proteccion
    de pre-check ``is_symlink()`` ya hecha por el caller.
    """
    mode = 0o700 if path.is_dir() else 0o600

    # Si O_NOFOLLOW no esta disponible (Windows runtime), usar chmod no-atomic.
    # En produccion el path windows ya va por icacls; este branch solo se
    # ejecuta si un test fuerza _IS_WINDOWS=False en Windows.
    if not hasattr(os, "O_NOFOLLOW"):
        try:
            os.chmod(path, mode)
        except OSError as exc:
            logger.error("chmod(%s, %o) failed: %s", path, mode, exc)
            raise PermissionError(f"Owner-only chmod failed for {path}") from exc
        return

    # O_NOFOLLOW: rechaza si el ultimo componente es un symlink (ELOOP).
    # O_RDONLY + (O_DIRECTORY si es dir): suficiente para fchmod.
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if path.is_dir() and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        # ELOOP (40): symlink race entre is_symlink() y open. Fail closed.
        logger.error("open(%s, O_NOFOLLOW) failed: %s", path, exc)
        raise PermissionError(f"Owner-only open failed for {path} (symlink race?)") from exc

    try:
        os.fchmod(fd, mode)
    except OSError as exc:
        logger.error("fchmod(%s, %o) failed: %s", path, mode, exc)
        raise PermissionError(f"Owner-only fchmod failed for {path}") from exc
    finally:
        os.close(fd)


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


def _get_current_user_name() -> str | None:
    """Return the current user's fully-qualified ``DOMAIN\\user`` name, or None.

    Unlike the bare ``getpass.getuser()`` name, this is domain-qualified, so the
    degrade-path verifier can require an *exact* owner match and reject a
    same-bare-name account from a different domain (e.g. ``OTHERDOMAIN\\alice``).
    """
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        name = result.stdout.strip()
        return name or None
    except Exception as exc:
        logger.warning("Could not resolve current user name: %s", exc)
        return None


def _get_current_logon_sid() -> str | None:
    """Return the current process's logon-session SID (``S-1-5-5-X-Y``), or None.

    The logon SID identifies *this* sign-in session.  It lets the owner-only
    check tolerate a transient session-local ACE while rejecting ACEs for
    *other* logon sessions (stale, or planted by another sign-in), which are
    not the owner.  Best-effort via ``whoami /groups``.
    """
    try:
        result = subprocess.run(
            ["whoami", "/groups"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except Exception as exc:
        logger.warning("Could not resolve current logon-session SID: %s", exc)
        return None
    match = re.search(r"\bS-1-5-5-\d+-\d+\b", result.stdout)
    return match.group(0) if match else None


def _logon_session_key(identifier: str) -> str | None:
    """Extract the session key (``X_Y``) from a logon-session identifier.

    Accepts the SID form ``S-1-5-5-X-Y`` or the icacls name form
    ``…\\LogonSessionId_0_<n>`` (both locale-independent).  Returns ``None`` when
    *identifier* is not a logon-session principal.
    """
    ident = identifier.lstrip("*").lower()
    if ident.startswith("s-1-5-5-"):
        return ident[len("s-1-5-5-") :].replace("-", "_")
    bare = ident.split("\\")[-1]
    if bare.startswith("logonsessionid_"):
        return bare[len("logonsessionid_") :]
    return None


def _logon_session_matches(identifier: str, current_logon_sid: str | None) -> bool:
    """True iff *identifier* is the *current* process's logon-session ACE."""
    if not current_logon_sid:
        return False
    this_key = _logon_session_key(identifier)
    cur_key = _logon_session_key(current_logon_sid)
    return this_key is not None and cur_key is not None and this_key == cur_key


# Well-known SIDs are locale-independent.  The English display names
# "Everyone"/"Users"/"BUILTIN\\Administrators"/"CREATOR OWNER" are localized on
# non-English Windows ("Usuarios"/"Administradores"/…) and do NOT resolve there,
# so passing them to ``icacls /remove`` makes the whole command exit 1332
# ("No mapping between account names and SIDs") — destroying secrets via the
# fail-closed path even though the owner ``/grant`` actually succeeded.  Removing
# by SID never depends on the OS display language.
_STRIP_SIDS: tuple[str, ...] = (
    "*S-1-1-0",  # Everyone
    "*S-1-5-32-545",  # BUILTIN\Users
    "*S-1-5-18",  # NT AUTHORITY\SYSTEM
    "*S-1-5-32-544",  # BUILTIN\Administrators
    "*S-1-3-0",  # CREATOR OWNER
)


def _owner_grant_spec(path: Path, principal: str) -> str:
    """Return the ``icacls /grant:r`` spec for *principal*, inheritable on dirs.

    A bare ``principal:(F)`` grant on a DIRECTORY applies to that object only,
    so artifacts created inside it afterwards do NOT inherit the owner-only ACL
    — they inherit whatever the parent still propagates (e.g. a group's read
    ACE).  That is exactly why ``~/.sky_claw/dlq/dlq.db`` could not be created
    after ``restrict_to_owner(~/.sky_claw)`` (the salt dir) ran: the ``dlq``
    child inherited only a non-writable group ACE → ``aiosqlite.connect`` →
    ``OperationalError: unable to open database file``.

    Adding ``(OI)(CI)`` makes the owner grant propagate to children, keeping the
    whole subtree owner-only *and* writable by the owner.  On a FILE the
    inheritance flags are meaningless, so a plain ``(F)`` is used.
    """
    try:
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    return f"{principal}:(OI)(CI)(F)" if is_dir else f"{principal}:(F)"


def _build_harden_command(path: Path, principal: str) -> list[str]:
    """Build the owner-only ``icacls`` hardening command for *principal*.

    *principal* is an account name (``alice`` / ``DOMAIN\\alice``) or a SID
    grant token (``*S-1-5-…``).  Inheritance is reset and the owner is granted
    full control; the well-known principals are stripped **by SID** so the
    command is locale-independent.
    """
    cmd = ["icacls", str(path), "/inheritance:r", "/grant:r", _owner_grant_spec(path, principal)]
    for sid in _STRIP_SIDS:
        cmd += ["/remove", sid]
    return cmd


def _is_transient_session_principal(ident: str) -> bool:
    """True for a transient *logon-session* pseudo-principal.

    Windows tags artifacts created under a restricted/derived token with a
    logon-session SID — ``S-1-5-5-X-Y`` — rendered by icacls as
    ``NT AUTHORITY\\LogonSessionId_0_<n>``.  It is session-local and transient
    (it dies with the logon session) and never grants access to another user or
    to the world, so an owner-only check must not treat it as a leak and destroy
    the artifact over it.  Seen in restricted-token environments (Windows
    Sandbox, app containers, some CI agents).

    *ident* is the already-normalised (``*`` stripped, lower-cased) identifier.
    The synthetic ``LogonSessionId_`` name is locale-independent, and the
    ``S-1-5-5-`` SID prefix is locale-independent by construction.
    """
    return ident.startswith("s-1-5-5-") or "logonsessionid_" in ident


def _dacl_is_owner_only(
    icacls_output: str,
    allowed_identifiers: list[str],
    *,
    current_logon_sid: str | None = None,
) -> bool:
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

            if _is_transient_session_principal(ident):
                # A logon-session ACE is session-local, but only the *current*
                # session is safe — a different/unknown session is not the owner
                # and must be rejected, not tolerated (Codex P1).
                if _logon_session_matches(ident, current_logon_sid):
                    continue
                return False

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

    if not _dacl_is_owner_only(result.stdout, allowed_identifiers, current_logon_sid=_get_current_logon_sid()):
        _fail_closed(
            path,
            f"DACL contains non-owner ACEs after hardening: {result.stdout!r}",
        )


def _parse_aces_with_perms(icacls_output: str) -> list[tuple[str, str]]:
    """Parse icacls stdout into ``(normalized_identifier, perms)`` ACE tuples.

    ``perms`` is the raw, upper-cased flag blob immediately following the
    identifier, e.g. ``"(OI)(CI)(F)"``.  Identifier tokenisation mirrors
    :func:`_dacl_is_owner_only` (handles spaces in ``NT AUTHORITY\\…`` names and
    multiple ACEs per line); the perms are the leading ``(XX)`` groups of the
    next split segment.
    """
    aces: list[tuple[str, str]] = []
    for line in icacls_output.splitlines():
        if ":(" not in line:
            continue
        is_path_line = bool(line) and not line[0].isspace()
        parts = line.split(":(")
        for i, segment in enumerate(parts[:-1]):
            last_paren = segment.rfind(")")
            if last_paren != -1:
                candidate = segment[last_paren + 1 :].strip()
            elif i == 0 and not is_path_line:
                candidate = segment.strip()
            else:
                candidate = segment.strip().split()[-1] if segment.strip() else ""
            if not candidate:
                continue
            ident = candidate.lstrip("*").lower()
            match = re.match(r"((?:\([A-Za-z]+\))+)", "(" + parts[i + 1])
            perms = match.group(1).upper() if match else ""
            aces.append((ident, perms))
    return aces


def _dacl_owner_has_inherited_full_control(
    icacls_output: str,
    allowed_identifiers: list[str],
    *,
    require_inheritance: bool,
    current_logon_sid: str | None = None,
) -> bool:
    """Strict owner-only check for the degrade (icacls-exception) path.

    Unlike :func:`_dacl_is_owner_only`, this requires the owner ACE to *prove the
    grant actually took effect* before a non-zero icacls exit is accepted:

    * every ACE must be an **exact** allowed owner identifier (no bare-name
      fallback — ``OTHERDOMAIN\\alice`` must not satisfy ``DESKTOP\\alice``;
      Codex P1) or the current logon session;
    * the owner ACE must carry Full Control ``(F)`` and, on a directory, the
      ``(OI)(CI)`` inheritance flags (a read-only or non-inheritable grant does
      not make children writable — the DLQ failure this change fixes; Codex P1).
    """
    if not allowed_identifiers:
        return False
    allowed = {i.lstrip("*").lower() for i in allowed_identifiers if i}
    owner_ok = False
    for ident, perms in _parse_aces_with_perms(icacls_output):
        if ident in allowed:
            if "(F)" not in perms:
                return False  # owner present but not Full Control
            if require_inheritance and not ("(OI)" in perms and "(CI)" in perms):
                return False  # directory grant won't propagate to children
            owner_ok = True
        elif _is_transient_session_principal(ident) and _logon_session_matches(ident, current_logon_sid):
            continue  # current session — session-local, not a leak
        else:
            return False  # foreign principal / wrong session / bare-name collision
    return owner_ok


def _effective_dacl_grants_inherited_owner_only(
    path: Path,
    allowed_identifiers: list[str],
    *,
    require_inheritance: bool,
    current_logon_sid: str | None = None,
) -> bool:
    """Non-raising strict verifier for the degrade path.

    Runs ``icacls <path>`` and returns whether the effective DACL grants the
    current owner Full Control (with inheritance on directories) and nothing
    else — returning ``False`` on any subprocess error instead of destroying the
    artifact.  Used to decide whether a non-zero icacls *exit code* reflects a
    genuine failure or a benign one (e.g. an unresolvable ``/remove`` that ran
    AFTER the owner ``/grant`` already applied).
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
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return _dacl_owner_has_inherited_full_control(
        result.stdout,
        allowed_identifiers,
        require_inheritance=require_inheritance,
        current_logon_sid=current_logon_sid,
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
                _build_harden_command(path, username),
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
        allowed = [username, sid] if username else [sid]
        try:
            subprocess.run(
                _build_harden_command(path, f"*{sid}"),
                capture_output=True,
                check=True,
                timeout=10,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            # Degrade safely: a non-zero icacls exit is NOT proof the owner grant
            # failed.  icacls applies operations left-to-right and does not roll
            # back, so an unresolvable /remove can fail AFTER the owner /grant
            # already took effect.  Accept ONLY if the effective DACL strictly
            # proves the *current* owner holds Full Control with the right
            # inheritance — exact qualified-name/SID match, no bare-name
            # fallback, owner rights + (OI)(CI) on dirs verified (Codex P1).
            try:
                is_dir = path.is_dir()
            except OSError:
                is_dir = False
            strict_allowed = [a for a in (_get_current_user_name(), sid) if a]
            if _effective_dacl_grants_inherited_owner_only(
                path,
                strict_allowed,
                require_inheritance=is_dir,
                current_logon_sid=_get_current_logon_sid(),
            ):
                logger.warning(
                    "icacls (SID) exited non-zero for %s but the effective DACL "
                    "already grants owner-only Full Control — accepting (icacls "
                    "error: %s)",
                    path,
                    exc,
                )
                return
            attempted = "both icacls attempts" if username else "the SID-based icacls attempt"
            logger.critical(
                "SECURITY: %s failed for %s — file may be world-readable: %s",
                attempted,
                path,
                exc,
            )
            _fail_closed(path, f"{attempted} failed: {exc}")
        else:
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

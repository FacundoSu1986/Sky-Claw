"""
╔══════════════════════════════════════════════════════════════════╗
║  AuthTokenManager — Secure Token Generation for WS Handshake  ║
║  Sky-Claw v2.0 (2026)                                         ║
╚══════════════════════════════════════════════════════════════════╝

Generates a one-time token at NiceGUI startup.  The Background Daemon
reads it from a secure temp file to authenticate the WebSocket upgrade.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from sky_claw.antigravity.security.file_permissions import restrict_to_owner

logger = logging.getLogger("SkyClaw.AuthToken")

# Token length in bytes (32 bytes = 256-bit entropy)
_TOKEN_BYTES = 32
# How long a token stays valid (seconds)
_TOKEN_TTL = 3600  # 1 hour


class AuthTokenManager:
    """
    Manages a shared secret between NiceGUI server and the WS Daemon.

    Flow:
      1. NiceGUI server calls generate() → writes token to a temp file.
      2. WS Daemon reads the file via read_token_file() and injects it
         as X-Auth-Token header on the WebSocket upgrade request.
      3. NiceGUI server validates incoming headers with validate().
    """

    def __init__(self, token_dir: str | None = None):
        self._token: str | None = None
        self._token_hash: str | None = None
        self._created_at: float = 0.0

        if token_dir:
            self._token_dir = Path(token_dir)
        else:
            # Default: ~/.sky_claw/tokens/
            self._token_dir = Path.home() / ".sky_claw" / "tokens"

        self._token_dir.mkdir(parents=True, exist_ok=True)
        restrict_to_owner(self._token_dir)
        self._token_path = self._token_dir / "ws_auth_token"
        self._rotation_task: asyncio.Task | None = None
        self._rotation_callbacks: list[Callable[[], Awaitable[None]]] = []

    # ── Server Side ──────────────────────────────────────────────────

    def generate(self) -> str:
        """Generate a new token, store its hash, and write to file."""
        self._token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._token_hash = self._hash(self._token)
        self._created_at = time.time()

        # Write token with metadata (JSON) so the client can validate TTL.
        # The daemon reads plaintext via read_token_file() for WS handshake.
        payload = {
            "token": self._token,
            "created_at": self._created_at,
            "ttl": _TOKEN_TTL,
        }
        # H-08: escritura ATÓMICA. write_text in-place no es atómico (en Windows
        # sobre todo): durante la rotación el daemon podía leer un archivo
        # parcial/vacío, fallar el json.loads, caer al fallback "legacy plaintext"
        # y usar un token corrupto para el handshake WS. Se escribe a un temp en el
        # MISMO directorio (misma unidad → rename atómico) y se hace os.replace.
        tmp_path = self._token_path.with_name(f".{self._token_path.name}.tmp.{secrets.token_hex(4)}")
        try:
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            restrict_to_owner(tmp_path)  # perms cerrados ANTES de exponerlo
            tmp_path.replace(self._token_path)  # atómico (POSIX y Windows)
        finally:
            # Si el replace ya movió el temp, unlink es no-op (missing_ok).
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)

        logger.info(f"Auth token generated (TTL={_TOKEN_TTL}s)")
        return self._token

    def validate(self, token: str) -> bool:
        """Validate an incoming token against the stored hash."""
        if not self._token_hash:
            logger.warning("No token generated yet — rejecting.")
            return False

        elapsed = time.time() - self._created_at
        if elapsed > _TOKEN_TTL:
            logger.warning(f"Token expired ({elapsed:.0f}s > {_TOKEN_TTL}s).")
            return False

        incoming_hash = self._hash(token)
        is_valid = secrets.compare_digest(incoming_hash, self._token_hash)

        if not is_valid:
            logger.warning("Token validation failed — hash mismatch.")

        return is_valid

    # ── Token Rotation (April 2026 hardening) ─────────────────────

    async def start_rotation(self) -> None:
        """Rotate token proactively at half TTL to limit exposure window."""
        if self._rotation_task is not None:
            logger.warning("Token rotation already running")
            return
        self._rotation_task = asyncio.create_task(self._rotation_loop())
        logger.info("Token rotation started (interval=%ds)", _TOKEN_TTL / 2)

    def register_rotation_callback(self, cb: Callable[[], Awaitable[None]]) -> None:
        """Register an async callable invoked after each successful token rotation.

        Used to notify subscribers (e.g., WebSocket handler) that existing
        connections should be closed and re-authenticated with the new token.
        Idempotent — registering the same callable twice is a no-op.
        """
        if cb not in self._rotation_callbacks:
            self._rotation_callbacks.append(cb)

    async def _rotation_loop(self) -> None:
        while True:
            await asyncio.sleep(_TOKEN_TTL / 2)
            try:
                logger.info("Rotating auth token...")
                # generate() hace I/O de archivo y, en Windows, un subprocess icacls
                # (restrict_to_owner). Ejecutarlo directo en el event loop lo congela;
                # se delega en un hilo para no bloquear el resto de coroutines.
                await asyncio.to_thread(self.generate)
            except Exception:
                # Log and continue — a single rotation failure must not kill
                # the loop and leave tokens permanently stale.
                logger.exception("Token rotation failed — will retry at next interval")
                continue
            for cb in self._rotation_callbacks:
                try:
                    await cb()
                except Exception as exc:
                    logger.warning("Rotation callback %r failed: %s", cb, exc, exc_info=True)

    async def stop_rotation(self) -> None:
        """Cancel the token rotation task."""
        if self._rotation_task:
            self._rotation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rotation_task
            self._rotation_task = None
            logger.info("Token rotation stopped")

    def revoke(self) -> None:
        """Revoke the current token — clears memory first, then removes disk artifact.

        In-memory state is cleared before the file is removed so concurrent
        ``validate()`` calls fail immediately.

        Note: overwriting before unlink is NOT guaranteed to erase data on SSDs
        (wear-leveling/TRIM) or NTFS (journal recovery). Real protection against
        forensic recovery requires full-disk encryption (BitLocker/LUKS).
        """
        # Clear in-memory state FIRST to prevent concurrent validate() from succeeding
        self._token = None
        self._token_hash = None
        self._created_at = 0.0

        self._token_path.unlink(missing_ok=True)

        logger.info("Auth token revoked.")

    # ── Client / Daemon Side ─────────────────────────────────────────

    @classmethod
    def read_token_file(cls, token_dir: str | None = None) -> str | None:
        """Read the token from the shared file, validating TTL.

        Returns None if the file is missing, malformed, or expired.
        Backwards-compatible: also reads legacy plaintext tokens."""
        if token_dir:
            path = Path(token_dir) / "ws_auth_token"
        else:
            path = Path.home() / ".sky_claw" / "tokens" / "ws_auth_token"

        if not path.exists():
            logger.warning(f"Token file not found at {path}")
            return None

        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None

        # Try JSON format first (April 2026+), fall back to legacy plaintext
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                token = payload.get("token")
                created_at = payload.get("created_at", 0)
                if created_at and (time.time() - created_at) > _TOKEN_TTL:
                    logger.warning(
                        f"Token file at {path} is expired (age={time.time() - created_at:.0f}s > TTL={_TOKEN_TTL}s)"
                    )
                    return None
                return token if token else None
        except json.JSONDecodeError:
            pass

        # Legacy plaintext token
        logger.debug("Reading legacy plaintext token (consider regenerating)")
        return raw

    # ── Internal ─────────────────────────────────────────────────────

    @staticmethod
    def _hash(token: str) -> str:
        """SHA-256 hash of the token."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

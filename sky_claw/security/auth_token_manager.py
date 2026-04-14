"""
╔══════════════════════════════════════════════════════════════════╗
║  AuthTokenManager — Secure Token Generation for WS Handshake  ║
║  Sky-Claw v2.0 (2026)                                         ║
╚══════════════════════════════════════════════════════════════════╝

Generates a one-time token at NiceGUI startup.  The Background Daemon
reads it from a secure temp file to authenticate the WebSocket upgrade.
"""

import contextlib
import hashlib
import logging
import secrets
import time
from pathlib import Path

from sky_claw.security.file_permissions import restrict_to_owner

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

    # ── Server Side ──────────────────────────────────────────────────

    def generate(self) -> str:
        """Generate a new token, store its hash, and write to file."""
        self._token = secrets.token_urlsafe(_TOKEN_BYTES)
        self._token_hash = self._hash(self._token)
        self._created_at = time.time()

        # Write plaintext token to a file readable by the daemon.
        # The daemon needs plaintext for the WS handshake (IPC channel).
        self._token_path.write_text(self._token, encoding="utf-8")
        # Restrict permissions (cross-platform)
        restrict_to_owner(self._token_path)

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

    def revoke(self) -> None:
        """Revoke the current token atomically — clear memory first, then disk."""
        # Clear in-memory state FIRST to prevent concurrent validate() from succeeding
        self._token = None
        self._token_hash = None
        self._created_at = 0.0

        if self._token_path.exists():
            # Overwrite with zeros before unlinking to prevent forensic recovery
            with contextlib.suppress(OSError):
                self._token_path.write_bytes(b"\x00" * 64)
            self._token_path.unlink(missing_ok=True)

        logger.info("Auth token revoked.")

    # ── Client / Daemon Side ─────────────────────────────────────────

    @classmethod
    def read_token_file(cls, token_dir: str | None = None) -> str | None:
        """Read the token from the shared file (called by the Daemon)."""
        if token_dir:
            path = Path(token_dir) / "ws_auth_token"
        else:
            path = Path.home() / ".sky_claw" / "tokens" / "ws_auth_token"

        if not path.exists():
            logger.warning(f"Token file not found at {path}")
            return None

        token = path.read_text(encoding="utf-8").strip()
        return token if token else None

    # ── Internal ─────────────────────────────────────────────────────

    @staticmethod
    def _hash(token: str) -> str:
        """SHA-256 hash of the token."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

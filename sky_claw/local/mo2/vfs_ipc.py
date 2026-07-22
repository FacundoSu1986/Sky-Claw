"""Frames JSON length-prefixed autenticados con HMAC para IPC local."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Mapping
from typing import Any

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.protocol import (
    LENGTH_BYTES,
    MAC_BYTES,
    MAX_FRAME_BYTES,
    VfsAuthenticationError,
    VfsFrameError,
    decode_authenticated_body,
    encode_authenticated_frame,
    validate_secret,
)

__all__ = [
    "VfsAuthenticationError",
    "VfsFrameError",
    "read_authenticated_message",
    "write_authenticated_message",
]


async def read_authenticated_message(
    reader: asyncio.StreamReader,
    secret: bytes,
) -> dict[str, Any]:
    """Lee un frame completo y lo autentica antes de parsear JSON."""
    validate_secret(secret)
    try:
        prefix = await reader.readexactly(LENGTH_BYTES)
    except asyncio.IncompleteReadError as exc:
        raise VfsFrameError("conexión cerrada antes del prefijo del frame") from exc
    body_length = struct.unpack(">I", prefix)[0]
    if body_length < MAC_BYTES:
        raise VfsFrameError("longitud de frame menor que el HMAC")
    if body_length > MAX_FRAME_BYTES:
        raise VfsFrameError(f"frame IPC excede el máximo de {MAX_FRAME_BYTES} bytes")
    try:
        body = await reader.readexactly(body_length)
    except asyncio.IncompleteReadError as exc:
        raise VfsFrameError("conexión cerrada con un frame IPC incompleto") from exc
    return decode_authenticated_body(body, secret)


async def write_authenticated_message(
    writer: asyncio.StreamWriter,
    message: Mapping[str, object],
    secret: bytes,
) -> None:
    """Escribe un frame autenticado sin bloquear el event loop."""
    writer.write(encode_authenticated_frame(message, secret))
    await writer.drain()

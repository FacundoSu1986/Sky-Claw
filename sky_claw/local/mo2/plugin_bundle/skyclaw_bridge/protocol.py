"""Protocolo IPC stdlib compartido por Sky-Claw y el Python embebido de MO2."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import struct
from collections.abc import Mapping
from typing import Any

MAX_FRAME_BYTES = 1024 * 1024
MAC_BYTES = hashlib.sha256().digest_size
LENGTH_BYTES = 4


class VfsFrameError(RuntimeError):
    """Frame incompleto, demasiado grande o con JSON inválido."""


class VfsAuthenticationError(VfsFrameError):
    """El HMAC del frame no corresponde al secreto de sesión."""


def validate_secret(secret: bytes) -> None:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("el secreto IPC debe contener al menos 32 bytes")


def canonical_payload(message: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            dict(message),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VfsFrameError(f"mensaje IPC no serializable: {exc}") from exc


def encode_authenticated_frame(message: Mapping[str, object], secret: bytes) -> bytes:
    validate_secret(secret)
    payload = canonical_payload(message)
    body_length = MAC_BYTES + len(payload)
    if body_length > MAX_FRAME_BYTES:
        raise VfsFrameError(f"frame IPC excede el máximo de {MAX_FRAME_BYTES} bytes")
    mac = hmac.new(secret, payload, hashlib.sha256).digest()
    return struct.pack(">I", body_length) + mac + payload


def decode_authenticated_body(body: bytes, secret: bytes) -> dict[str, Any]:
    validate_secret(secret)
    if len(body) < MAC_BYTES:
        raise VfsFrameError("longitud de frame menor que el HMAC")
    supplied_mac = body[:MAC_BYTES]
    payload = body[MAC_BYTES:]
    expected_mac = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        raise VfsAuthenticationError("HMAC del frame IPC inválido")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VfsFrameError(f"payload JSON inválido: {exc}") from exc
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise VfsFrameError("el payload IPC debe ser un objeto JSON con claves string")
    return decoded


def recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise VfsFrameError("conexión cerrada con un frame IPC incompleto")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_authenticated_message(connection: socket.socket, secret: bytes) -> dict[str, Any]:
    prefix = recv_exact(connection, LENGTH_BYTES)
    body_length = struct.unpack(">I", prefix)[0]
    if body_length < MAC_BYTES:
        raise VfsFrameError("longitud de frame menor que el HMAC")
    if body_length > MAX_FRAME_BYTES:
        raise VfsFrameError(f"frame IPC excede el máximo de {MAX_FRAME_BYTES} bytes")
    return decode_authenticated_body(recv_exact(connection, body_length), secret)


def send_authenticated_message(
    connection: socket.socket,
    message: Mapping[str, object],
    secret: bytes,
) -> None:
    connection.sendall(encode_authenticated_frame(message, secret))

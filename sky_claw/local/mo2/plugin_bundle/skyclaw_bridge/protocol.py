"""Protocolo IPC stdlib compartido por Sky-Claw y el Python embebido de MO2."""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import socket
import struct
from collections.abc import Mapping
from typing import Any

MAX_FRAME_BYTES = 1024 * 1024
MAC_BYTES = hashlib.sha256().digest_size
LENGTH_BYTES = 4
#: Config por instancia que el instalador escribe DENTRO del bundle: no es código y difiere entre instancias, así que
#: queda fuera de la identidad del bundle (si no, el mismo código tendría un digest distinto por instancia).
BRIDGE_CONFIG_NAME = "bridge_config.json"
_BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})


class VfsFrameError(RuntimeError):
    """Frame incompleto, demasiado grande o con JSON inválido."""


def _archivos_del_bundle(carpeta: pathlib.Path, base: pathlib.Path) -> list[pathlib.Path]:
    archivos: list[pathlib.Path] = []
    for path in sorted(carpeta.iterdir()):
        if path.name == "__pycache__" or path.suffix in _BYTECODE_SUFFIXES:
            continue
        if carpeta == base and path.name == BRIDGE_CONFIG_NAME:
            continue
        if path.is_symlink():
            raise ValueError(f"el bundle del bridge no admite enlaces: {path.name}")
        if path.is_dir():
            archivos.extend(_archivos_del_bundle(path, base))
        else:
            archivos.append(path)
    return archivos


def bundle_digest(root: pathlib.Path) -> str:
    """SHA-256 hex de TODO el código del bundle del bridge: la identidad que el bridge declara en su ``hello``.

    Cubre los archivos por ruta relativa POSIX (ordenados) y por contenido, de modo que agregar, quitar, renombrar o
    editar cualquiera cambia el resultado. Excluye lo que no es código: ``__pycache__``, ``*.pyc``/``*.pyo`` y la
    ``bridge_config.json`` de la raíz (es por instancia). Un symlink dentro del bundle es un error: no se sigue, porque
    hashearía contenido de otro lugar.

    Los junctions de Windows NO se detectan acá: este módulo corre dentro del Python embebido de MO2 y no puede importar
    ``sky_claw``, y la detección de junctions vive en UN solo módulo (``sky_claw/app/security/links.py``, ancla en
    ``tests/test_links.py``). La verificación del bundle del PRODUCTO la hace el lado host con esa pieza
    (``bridge_bundle.py`` y el instalador).
    """

    base = pathlib.Path(root)
    resumen = hashlib.sha256()
    for path in _archivos_del_bundle(base, base):
        nombre = path.relative_to(base).as_posix().encode("utf-8")
        # Longitud antes del nombre: así la secuencia (nombre, digest) se parsea de una sola manera.
        resumen.update(len(nombre).to_bytes(4, "big"))
        resumen.update(nombre)
        resumen.update(hashlib.sha256(path.read_bytes()).digest())
    return resumen.hexdigest()


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

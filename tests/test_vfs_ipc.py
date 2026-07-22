"""Transporte IPC autenticado del bridge MO2."""

from __future__ import annotations

import asyncio
import struct

import pytest

from sky_claw.local.mo2.vfs_ipc import (
    MAX_FRAME_BYTES,
    VfsAuthenticationError,
    VfsFrameError,
    encode_authenticated_frame,
    read_authenticated_message,
    write_authenticated_message,
)


async def _reader_con(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_frame_autenticado_roundtrip() -> None:
    secret = b"s" * 32
    frame = encode_authenticated_frame(
        {"protocol_version": 1, "type": "health", "request_id": "r-1"},
        secret,
    )

    message = await read_authenticated_message(await _reader_con(frame), secret)

    assert message == {"protocol_version": 1, "request_id": "r-1", "type": "health"}


async def test_frame_rechaza_payload_manipulado() -> None:
    secret = b"s" * 32
    frame = bytearray(encode_authenticated_frame({"type": "health"}, secret))
    frame[-2] ^= 1

    with pytest.raises(VfsAuthenticationError, match="HMAC"):
        await read_authenticated_message(await _reader_con(bytes(frame)), secret)


async def test_frame_rechaza_longitud_excesiva_sin_leer_payload() -> None:
    prefix = struct.pack(">I", MAX_FRAME_BYTES + 1)

    with pytest.raises(VfsFrameError, match="excede"):
        await read_authenticated_message(await _reader_con(prefix), b"s" * 32)


async def test_stream_escribe_y_lee_un_mensaje() -> None:
    received = asyncio.Future[dict[str, object]]()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            received.set_result(await read_authenticated_message(reader, b"k" * 32))
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    del reader
    try:
        await write_authenticated_message(writer, {"type": "hello", "role": "bridge"}, b"k" * 32)
        assert await asyncio.wait_for(received, timeout=1) == {"role": "bridge", "type": "hello"}
    finally:
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()

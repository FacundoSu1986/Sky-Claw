"""Tests focales de inyección de OSError dentro de _write_metadata_crash_safe.

Cubre el contrato de error de P4-S2: fallos de I/O (write/flush/fsync/truncate)
se traducen a FrozenRuntimeLockError con __cause__ OSError, sin sustituir el helper.

No monkeypatch del helper completo: se inyecta el fallo en el file_obj real o mock
para ejercitar el try/except interno.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from sky_claw.local.frozen_runtime.errors import FrozenRuntimeLockError
from sky_claw.local.frozen_runtime.root_lock import _write_metadata_crash_safe


class _FailingFile:
    """File-like que falla en un método específico con OSError."""

    def __init__(self, fail_on: str, old_size: int = 100) -> None:
        self.fail_on = fail_on
        self._pos = 0
        self._size = old_size
        self.closed = False

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == os.SEEK_END:
            self._pos = self._size
        elif whence == os.SEEK_SET:
            self._pos = offset
        else:
            self._pos += offset
        return self._pos

    def tell(self) -> int:
        return self._pos

    def write(self, data: bytes) -> int:
        if self.fail_on == "write":
            raise OSError("simulated write failure")
        return len(data)

    def flush(self) -> None:
        if self.fail_on == "flush":
            raise OSError("simulated flush failure")

    def fileno(self) -> int:
        return 1

    def truncate(self, size: int | None = None) -> int:
        if self.fail_on == "truncate":
            raise OSError("simulated truncate failure")
        return 0


@pytest.mark.parametrize("fail_on", ["write", "flush", "fsync", "truncate"])
def test_write_metadata_crash_safe_traduce_oserror_a_frozen_runtime_lock_error(fail_on: str) -> None:
    """Un OSError en write/flush/fsync/truncate se traduce a FrozenRuntimeLockError.

    El __cause__ debe ser el OSError original. No se sustituye el helper.
    """
    payload = {
        "schema_version": 1,
        "canonical_root": "/tmp/test-root",
        "session_id": "00000000-0000-0000-0000-000000000001",
        "pid": 1234,
        "process_create_time": 1000.0,
        "acquired_at": 2000.0,
        "phase": "HELD",
    }

    # old_size grande para forzar el camino de truncate (cuando el payload nuevo es más corto)
    old_size = 200 if fail_on == "truncate" else 50
    file_obj = _FailingFile(fail_on=fail_on, old_size=old_size)

    # Para fsync: el helper llama os.fsync(file_obj.fileno())
    # Mockeamos os.fsync solo si es el punto de fallo, sin tocar el helper.
    if fail_on == "fsync":
        original_fsync = os.fsync

        def failing_fsync(fd: int) -> None:
            raise OSError("simulated fsync failure")

        os.fsync = failing_fsync  # type: ignore[assignment]
        try:
            with pytest.raises(FrozenRuntimeLockError) as exc_info:
                _write_metadata_crash_safe(file_obj, payload)
            assert isinstance(exc_info.value.__cause__, OSError)
            assert "no se pudo persistir la metadata del root lock" in str(exc_info.value)
        finally:
            os.fsync = original_fsync  # type: ignore[assignment]
        return

    with pytest.raises(FrozenRuntimeLockError) as exc_info:
        _write_metadata_crash_safe(file_obj, payload)

    assert isinstance(exc_info.value.__cause__, OSError)
    assert "no se pudo persistir la metadata del root lock" in str(exc_info.value)


def test_write_metadata_crash_safe_exito_no_lanza(tmp_path: Any) -> None:
    """Camino feliz: no OSError, no excepción."""
    payload = {
        "schema_version": 1,
        "canonical_root": str(tmp_path),
        "session_id": "00000000-0000-0000-0000-000000000002",
        "pid": 5678,
        "process_create_time": 3000.0,
        "acquired_at": 4000.0,
        "phase": "RELEASED",
        "released_at": 5000.0,
    }

    lock_file = tmp_path / "test.lock"
    lock_file.write_bytes(b" " * 20)  # tamaño previo

    with open(lock_file, "r+b") as f:
        _write_metadata_crash_safe(f, payload)

    # Debe haber escrito sin error
    assert lock_file.exists()
    content = lock_file.read_bytes()
    assert b"RELEASED" in content

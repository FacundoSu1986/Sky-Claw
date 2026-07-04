"""Tests para DirectoryRollback — rollback move-aside de un directorio regenerado."""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.tools._dir_rollback import DirectoryRollback


async def test_success_discards_backup(tmp_path: pathlib.Path) -> None:
    """En éxito: el dir queda con el contenido nuevo y el backup se descarta."""
    target = tmp_path / "Output"
    target.mkdir()
    (target / "old.txt").write_text("OLD", encoding="utf-8")

    async with DirectoryRollback(target):
        # El dir original fue movido aparte → el body regenera desde cero.
        assert not target.exists()
        target.mkdir()
        (target / "new.txt").write_text("NEW", encoding="utf-8")

    assert (target / "new.txt").read_text(encoding="utf-8") == "NEW"
    assert not (target / "old.txt").exists()
    assert not list(tmp_path.glob("Output.rollback-*"))  # sin huérfanos


async def test_failure_restores_original(tmp_path: pathlib.Path) -> None:
    """En excepción: el dir original se restaura byte-a-byte y el parcial se borra."""
    target = tmp_path / "Output"
    target.mkdir()
    (target / "old.txt").write_text("OLD", encoding="utf-8")
    (target / "sub").mkdir()
    (target / "sub" / "asset.bin").write_bytes(b"\x00\x01\x02")

    with pytest.raises(RuntimeError, match="boom"):
        async with DirectoryRollback(target):
            target.mkdir()
            (target / "partial.txt").write_text("PARTIAL", encoding="utf-8")
            raise RuntimeError("boom")

    assert (target / "old.txt").read_text(encoding="utf-8") == "OLD"
    assert (target / "sub" / "asset.bin").read_bytes() == b"\x00\x01\x02"
    assert not (target / "partial.txt").exists()
    assert not list(tmp_path.glob("Output.rollback-*"))


async def test_first_run_no_backup_success(tmp_path: pathlib.Path) -> None:
    """Primer run (dir no existe): en éxito conserva el dir nuevo."""
    target = tmp_path / "Output"

    async with DirectoryRollback(target):
        assert not target.exists()
        target.mkdir()
        (target / "new.txt").write_text("NEW", encoding="utf-8")

    assert (target / "new.txt").read_text(encoding="utf-8") == "NEW"


async def test_first_run_failure_removes_partial(tmp_path: pathlib.Path) -> None:
    """Primer run: en fallo borra el parcial y no deja backup."""
    target = tmp_path / "Output"

    with pytest.raises(RuntimeError):
        async with DirectoryRollback(target):
            target.mkdir()
            (target / "partial.txt").write_text("X", encoding="utf-8")
            raise RuntimeError("boom")

    assert not target.exists()
    assert not list(tmp_path.glob("Output.rollback-*"))


async def test_disabled_is_noop(tmp_path: pathlib.Path) -> None:
    """enabled=False: no mueve nada; el body opera sobre el dir real."""
    target = tmp_path / "Output"
    target.mkdir()
    (target / "old.txt").write_text("OLD", encoding="utf-8")

    async with DirectoryRollback(target, enabled=False):
        assert target.exists()  # NO fue movido aparte
        (target / "new.txt").write_text("NEW", encoding="utf-8")

    assert (target / "old.txt").exists()
    assert (target / "new.txt").exists()


async def test_rename_failure_fail_closed(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Si el rename de move-aside falla, __aenter__ hace fail-closed (raise)."""
    target = tmp_path / "Output"
    target.mkdir()
    (target / "old.txt").write_text("OLD", encoding="utf-8")

    def _boom_rename(self: pathlib.Path, _dst: pathlib.Path) -> None:
        raise PermissionError("directorio bloqueado")

    monkeypatch.setattr(pathlib.Path, "rename", _boom_rename)

    with pytest.raises(OSError, match="rollback"):
        async with DirectoryRollback(target):
            pass

    # El dir original sigue intacto (no se movió).
    assert (target / "old.txt").read_text(encoding="utf-8") == "OLD"

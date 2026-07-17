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


async def test_rollback_completed_true_on_successful_restore(tmp_path: pathlib.Path) -> None:
    """M-7: rollback_completed=True cuando el restore en el path de excepción tuvo éxito."""
    target = tmp_path / "Output"
    target.mkdir()
    (target / "old.txt").write_text("OLD", encoding="utf-8")

    rb = DirectoryRollback(target)
    with pytest.raises(RuntimeError, match="boom"):
        async with rb:
            raise RuntimeError("boom")

    assert rb.rollback_completed is True


async def test_rollback_completed_false_when_restore_fails(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-7: si _restore_backup lanza OSError (best-effort, se traga), rollback_completed=False.

    Antes, dyndolod/xedit hardcodeaban rolled_back=True aunque el restore fallara
    en silencio, mintiendo al usuario y envenenando el audit trail.
    """
    target = tmp_path / "Output"
    target.mkdir()
    (target / "old.txt").write_text("OLD", encoding="utf-8")

    rb = DirectoryRollback(target)

    async def _boom_restore() -> None:
        raise OSError("rmtree bloqueado por handle de Windows")

    # El body lanza → __aexit__ intenta restore, que ahora falla.
    with pytest.raises(RuntimeError, match="boom"):
        async with rb:
            monkeypatch.setattr(rb, "_restore_backup", _boom_restore)
            raise RuntimeError("boom")

    assert rb.rollback_completed is False


async def test_rollback_completed_false_on_success_path(tmp_path: pathlib.Path) -> None:
    """M-7: en el path de éxito no hay rollback, así que rollback_completed queda False."""
    target = tmp_path / "Output"
    target.mkdir()

    rb = DirectoryRollback(target)
    async with rb:
        pass

    assert rb.rollback_completed is False


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


async def test_commit_desactiva_el_restore_en_excepcion(tmp_path: pathlib.Path) -> None:
    """Tras commit() (punto de no-retorno), una excepción posterior NO restaura:
    el output nuevo se conserva (review Codex #312)."""
    target = tmp_path / "Output"
    target.mkdir()
    (target / "old.txt").write_text("OLD", encoding="utf-8")

    with pytest.raises(RuntimeError, match="post-commit"):
        async with DirectoryRollback(target) as rb:
            target.mkdir()
            (target / "new.txt").write_text("NEW", encoding="utf-8")
            await rb.commit()  # el output ya es final
            raise RuntimeError("post-commit boom")  # p. ej. cancelación durante el informe

    # El output nuevo se conserva; NO se restauró el viejo.
    assert (target / "new.txt").read_text(encoding="utf-8") == "NEW"
    assert not (target / "old.txt").exists()
    assert not list(tmp_path.glob("Output.rollback-*"))  # backup descartado


async def test_commit_es_idempotente(tmp_path: pathlib.Path) -> None:
    """commit() dos veces no rompe (idempotente)."""
    target = tmp_path / "Output"
    target.mkdir()
    (target / "old.txt").write_text("OLD", encoding="utf-8")

    async with DirectoryRollback(target) as rb:
        target.mkdir()
        await rb.commit()
        await rb.commit()  # segunda vez: no-op

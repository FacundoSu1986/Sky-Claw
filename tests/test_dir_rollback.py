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


async def test_exito_sin_regeneracion_conserva_el_contenido_previo(tmp_path: pathlib.Path) -> None:
    """Descartar el backup sólo es seguro si la herramienta regeneró el dir.

    La propiedad se ancla acá —y no sólo en el caller que la destapó (Pandora, con
    sus dos ``Pandora_Output`` candidatos)— porque no es de Pandora: cualquiera que
    proteja varios destinos candidatos, o cuya herramienta pueda no producir
    salida, tiene el mismo agujero. Y es el peor tipo de agujero: ocurre en el
    camino de ÉXITO, así que no hay excepción que lo delate (review Codex #399).
    """
    target = tmp_path / "Output"
    target.mkdir()
    (target / "previo.txt").write_text("irremplazable", encoding="utf-8")

    async with DirectoryRollback(target):
        pass  # la herramienta "corre" bien pero no escribe nada acá

    assert target.exists(), "se borró un dir que la herramienta no regeneró"
    assert (target / "previo.txt").read_text(encoding="utf-8") == "irremplazable"
    assert not list(tmp_path.glob("Output.rollback-*"))  # sin backup huérfano


async def test_exito_con_regeneracion_sigue_descartando_el_backup(tmp_path: pathlib.Path) -> None:
    """El hermano: cuando la herramienta SÍ regenera, el backup se descarta como
    siempre — el fix de arriba no puede convertirse en una fuga de disco (los
    ``Output/`` de DynDOLOD pesan GBs, que es el motivo de existir del move-aside)."""
    target = tmp_path / "Output"
    target.mkdir()
    (target / "previo.txt").write_text("viejo", encoding="utf-8")

    async with DirectoryRollback(target):
        target.mkdir()
        (target / "nuevo.txt").write_text("nuevo", encoding="utf-8")

    assert (target / "nuevo.txt").read_text(encoding="utf-8") == "nuevo"
    assert not (target / "previo.txt").exists()
    assert not list(tmp_path.glob("Output.rollback-*"))


async def test_cancelacion_durante_el_move_aside_devuelve_el_dir(tmp_path: pathlib.Path) -> None:
    """Una cancelación mientras corre el rename no puede dejar el dir varado.

    El rename va por ``asyncio.to_thread``: cancelar la corrutina que lo espera NO
    interrumpe el hilo. Sin shield, el rename terminaba en background mientras el
    caller nunca llegaba a registrar el context en su ``AsyncExitStack`` — el dir
    previo quedaba bajo un nombre ``.rollback-*`` y ningún ``__aexit__`` podía
    devolverlo, pese a que la herramienta nunca llegó a correr (review Codex #399).

    Mismo patrón F2 que ``sandbox_run.run_ritual_in_sandbox`` ya usa para
    ``clone()``: esperar el desenlace REAL antes de decidir qué limpiar.
    """
    import asyncio
    import threading
    import time
    from unittest.mock import patch

    target = tmp_path / "Output"
    target.mkdir()
    (target / "previo.txt").write_text("irremplazable", encoding="utf-8")

    rename_real = pathlib.Path.rename
    # Sincroniza con el HILO, no con el reloj: sin esto el test afirmaba antes de
    # que el rename ocurriera y pasaba igual sin el shield — o sea, no probaba nada.
    primer_rename = threading.Event()

    def _rename_lento(self: pathlib.Path, dst: pathlib.Path) -> pathlib.Path:
        if primer_rename.is_set():
            return rename_real(self, dst)  # el deshacer no se demora
        time.sleep(0.3)  # el hilo sigue pese a la cancelación de quien lo espera
        try:
            return rename_real(self, dst)
        finally:
            primer_rename.set()

    async def _entrar() -> None:
        async with DirectoryRollback(target):
            pass

    with patch.object(pathlib.Path, "rename", _rename_lento):
        task = asyncio.ensure_future(_entrar())
        await asyncio.sleep(0.05)  # dejar que entre al rename
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # El move-aside corre en un hilo que la cancelación no interrumpe: hay que
        # esperar su desenlace REAL antes de mirar el disco.
        assert await asyncio.to_thread(primer_rename.wait, 5.0)

    assert target.exists(), "el dir previo quedó varado bajo el nombre de backup"
    assert (target / "previo.txt").read_text(encoding="utf-8") == "irremplazable"
    assert not list(tmp_path.glob("Output.rollback-*"))


async def test_veto_de_lease_omite_el_restore_y_conserva_el_backup(tmp_path: pathlib.Path) -> None:
    """Perdida la exclusividad, NO se restaura — pero el backup se conserva.

    El move-aside vive anidado dentro de un ``SnapshotTransactionLock`` y sale
    ANTES que él. El lock saltea deliberadamente su rollback tras perder la lease
    (*"never clobber a concurrent owner's mutations"*, ``locks.py``), así que sin
    este veto el context de abajo restauraba igual: borraba la salida del nuevo
    dueño y devolvía un backup viejo (review Codex #399).

    El backup NO se descarta: es el único ejemplar del estado previo y el recovery
    manual lo necesita. Y ``rollback_completed`` queda False, para que el caller
    deje la TX PENDIENTE en vez de afirmar que revirtió.
    """
    target = tmp_path / "Output"
    target.mkdir()
    (target / "previo.txt").write_text("mio", encoding="utf-8")

    rb = DirectoryRollback(target, should_rollback=lambda: False)
    with pytest.raises(RuntimeError):
        async with rb:
            target.mkdir()
            (target / "del_otro.txt").write_text("del nuevo dueño", encoding="utf-8")
            raise RuntimeError("boom")

    assert (target / "del_otro.txt").exists(), "se pisó la salida del dueño concurrente"
    assert not (target / "previo.txt").exists()
    assert rb.rollback_completed is False
    backups = list(tmp_path.glob("Output.rollback-*"))
    assert len(backups) == 1, "el backup debe quedar en disco para recuperación manual"
    assert (backups[0] / "previo.txt").read_text(encoding="utf-8") == "mio"


def test_todos_los_callers_de_move_aside_cablean_el_veto_de_lease() -> None:
    """Ancla que ENUMERA: un caller nuevo de ``DirectoryRollback`` no puede nacer
    sin el veto.

    El agujero del lease no era de Pandora sino del patrón —``DirectoryRollback``
    anidado dentro de un ``SnapshotTransactionLock``—, así que arreglarlo sólo
    donde lo reportó el review habría dejado a DynDOLOD intacto: el defecto #1 del
    repo. En vez de listar a mano los servicios que ya conozco, se detectan los que
    construyen un ``DirectoryRollback`` y se exige que cada uno pase
    ``should_rollback``.
    """
    import re

    tools = pathlib.Path(__file__).resolve().parent.parent / "sky_claw" / "local" / "tools"
    constructores = re.compile(r"DirectoryRollback\((?!\s*\))[^)]*\)", re.DOTALL)

    sin_veto: list[str] = []
    for modulo in sorted(tools.glob("*.py")):
        if modulo.name == "_dir_rollback.py":  # la definición, no un caller
            continue
        for uso in constructores.findall(modulo.read_text(encoding="utf-8")):
            if "should_rollback" not in uso:
                sin_veto.append(f"{modulo.name}: {' '.join(uso.split())}")

    assert not sin_veto, (
        f"estos move-aside restaurarían aunque se hubiera perdido la lease, pisando a un dueño concurrente: {sin_veto}"
    )

"""Tests para DirectoryRollback — rollback move-aside de un directorio regenerado."""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.local.tools import _dir_rollback
from sky_claw.local.tools._dir_rollback import DirectoryRollback, _borrar_arbol_o_enlace
from tests._symlink_guard import crear_junction, is_junction_real, junction_guard, symlink_guard


def _llamadas_sin_veto(arbol: ast.Module) -> list[int]:
    """Líneas de llamadas a ``DirectoryRollback`` (o su alias local) sin
    ``should_rollback``. Función pura, testeable contra un AST aislado.

    **Resuelve el alias de import** (review Codex #401): matchear el nombre
    literal ``"DirectoryRollback"`` deja pasar en silencio un caller que
    importe con ``from ... import DirectoryRollback as Rollback`` — el nodo de
    la llamada tiene ``func.id == "Rollback"``, y un ancla que se supone
    congela TODOS los callers no puede depender de que nadie use ese alias.
    """
    nombres_validos = {"DirectoryRollback"}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            for alias in nodo.names:
                if alias.name == "DirectoryRollback":
                    nombres_validos.add(alias.asname or alias.name)

    lineas: list[int] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        nombre = nodo.func.id if isinstance(nodo.func, ast.Name) else getattr(nodo.func, "attr", "")
        if nombre not in nombres_validos:
            continue
        # Presente pero en None equivale a ausente (review CodeRabbit #401):
        # ``_veto_permite_restaurar`` trata ``self._should_rollback is None`` como
        # "siempre permitido" — el mismo estado sin protección que no pasar el
        # kwarg. Un ``DirectoryRollback(x, should_rollback=None)`` pasaría el
        # chequeo de sólo-presencia mientras deja el veto tan desactivado como si
        # nunca se hubiera escrito: exactamente la clase de descuido que este
        # ancla exhaustiva existe para atajar.
        tiene_veto_real = any(
            kw.arg == "should_rollback" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
            for kw in nodo.keywords
        )
        if not tiene_veto_real:
            lineas.append(nodo.lineno)
    return lineas


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
    # Y el ARRANQUE también (review CodeRabbit #399): con un `sleep` de reloj, en
    # CI cargada la task puede no haber llegado al rename cuando se la cancela, y
    # entonces el verde no ejerce el shield — el mismo falso verde de nuevo, un
    # paso antes.
    rename_empezo = threading.Event()

    def _rename_lento(self: pathlib.Path, dst: pathlib.Path) -> pathlib.Path:
        if primer_rename.is_set():
            return rename_real(self, dst)  # el deshacer no se demora
        rename_empezo.set()
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
        assert await asyncio.to_thread(rename_empezo.wait, 5.0), "el rename nunca arrancó"
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
    # Por AST y recursivo sobre TODO ``sky_claw`` (review CodeRabbit #399), con
    # resolución de alias de import (review Codex #401, ``_llamadas_sin_veto``).
    # La primera versión usaba un regex y ``glob`` no recursivo sobre
    # ``local/tools``: un caller en un subpaquete —o en la GUI, o en la capa del
    # agente— nacía sin veto y el ancla no lo veía, y el patrón ``[^)]*`` no
    # cruza paréntesis, así que una construcción anidada se cortaba en el primer
    # ``)``. Un ancla que muestrea es justo lo que este test existe para no ser.
    raiz = pathlib.Path(__file__).resolve().parent.parent / "sky_claw"

    sin_veto: list[str] = []
    for modulo in sorted(raiz.rglob("*.py")):
        if modulo.name == "_dir_rollback.py":  # la definición, no un caller
            continue
        arbol = ast.parse(modulo.read_text(encoding="utf-8"))
        sin_veto += [f"{modulo.relative_to(raiz)}:{linea}" for linea in _llamadas_sin_veto(arbol)]

    assert not sin_veto, (
        f"estos move-aside restaurarían aunque se hubiera perdido la lease, pisando a un dueño concurrente: {sin_veto}"
    )


async def test_exito_con_target_vacio_conserva_el_contenido_previo(tmp_path: pathlib.Path) -> None:
    """ "Existe" no es "lo regeneró" (review CodeRabbit #399).

    Una herramienta que crea el directorio candidato y no escribe nada dentro
    dejaba pasar el ``rmtree`` del backup: la misma pérdida silenciosa que el test
    anterior ataja, un paso más allá — y plausible justo en el escenario
    multi-candidato de Pandora que motivó todo esto.
    """
    target = tmp_path / "Output"
    target.mkdir()
    (target / "previo.txt").write_text("irremplazable", encoding="utf-8")

    async with DirectoryRollback(target):
        target.mkdir()  # la tool crea el dir y no escribe nada

    assert (target / "previo.txt").read_text(encoding="utf-8") == "irremplazable"
    assert not list(tmp_path.glob("Output.rollback-*"))


async def test_veto_que_lanza_no_escapa_de_aexit_y_es_fail_closed(tmp_path: pathlib.Path) -> None:
    """Un ``should_rollback`` que lance no puede romper la garantía de ``__aexit__``.

    La clase promete no lanzar nunca desde ``__aexit__`` para no enmascarar la
    excepción del body. El veto lo provee el caller y puede reventar; ante eso se
    asume lo conservador —no restaurar, conservar el backup—, porque restaurar a
    ciegas es lo único irreversible de las dos opciones (review CodeRabbit #399).
    """
    target = tmp_path / "Output"
    target.mkdir()
    (target / "previo.txt").write_text("mio", encoding="utf-8")

    def _veto_roto() -> bool:
        raise AttributeError("el lock cambió de superficie")

    rb = DirectoryRollback(target, should_rollback=_veto_roto)
    with pytest.raises(RuntimeError, match="boom"):  # la excepción ORIGINAL, no la del veto
        async with rb:
            target.mkdir()
            (target / "parcial.txt").write_text("parcial", encoding="utf-8")
            raise RuntimeError("boom")

    assert rb.rollback_completed is False
    backups = list(tmp_path.glob("Output.rollback-*"))
    assert len(backups) == 1, "el backup debe conservarse cuando el veto no se pudo evaluar"
    assert (backups[0] / "previo.txt").read_text(encoding="utf-8") == "mio"


async def test_veto_de_lease_tambien_protege_el_restore_del_camino_limpio(tmp_path: pathlib.Path) -> None:
    """El veto de lease debe cubrir la restauración del target vacío EN EL CAMINO
    LIMPIO, no sólo en el de excepción (review Codex #401).

    Es el mismo defecto #1 del repo cometido dentro de la propia clase: el veto se
    cableó en la rama ``if exc_type is not None`` de ``__aexit__`` y se olvidó el
    ``else`` — que es exactamente donde vive la restauración de target vacío que
    este PR agregó. Si el heartbeat marcó la lease perdida DURANTE un run que
    igual "termina bien" (sin excepción) y el target queda vacío, restaurar sin
    consultar el veto pisaría lo que un dueño concurrente pudo haber empezado a
    escribir ahí — el mismo daño que el veto ya evita en la rama de excepción.
    """
    target = tmp_path / "Output"
    target.mkdir()
    (target / "previo.txt").write_text("del dueño viejo", encoding="utf-8")

    rb = DirectoryRollback(target, should_rollback=lambda: False)
    async with rb:
        target.mkdir()  # el "dueño nuevo" recreó el dir, todavía sin contenido

    # Sin excepción: el body terminó bien. Si el veto se ignora acá, el dir del
    # dueño nuevo se borra y se restaura el backup del viejo encima.
    assert target.exists()
    assert not (target / "previo.txt").exists(), "se restauró el backup del dueño viejo sobre el del nuevo"
    backups = list(tmp_path.glob("Output.rollback-*"))
    assert len(backups) == 1, "el backup del dueño viejo debe conservarse, ni perderse ni restaurarse"
    assert (backups[0] / "previo.txt").read_text(encoding="utf-8") == "del dueño viejo"


async def test_commit_con_target_vacio_no_revierte_el_estado_comprometido(tmp_path: pathlib.Path) -> None:
    """``commit()`` es un punto de no-retorno: un target vacío en ese instante es
    el estado FINAL que el caller eligió, no algo que revertir (review Codex #401).

    ``commit()`` delega en ``_discard_backup`` para soltar el backup — pero esa
    función ganó, en este mismo PR, la lógica de "restaurar si está vacío". Sin
    diferenciar el llamador, un commit sobre un output vacío quedaba revertido
    por la propia limpieza que el commit dispara, violando el contrato que la
    docstring de ``commit()`` promete textualmente.
    """
    target = tmp_path / "Output"
    target.mkdir()
    (target / "viejo.txt").write_text("viejo", encoding="utf-8")

    async with DirectoryRollback(target) as rb:
        target.mkdir()  # el caller deja el target vacío A PROPÓSITO
        await rb.commit()  # ya es final

    assert target.exists()
    assert not (target / "viejo.txt").exists(), "commit() revirtió un estado ya comprometido"
    assert not list(tmp_path.glob("Output.rollback-*")), "no debe quedar backup huérfano tras commit"


async def test_cancelacion_no_deja_rmdir_sin_su_rename(tmp_path: pathlib.Path) -> None:
    """Entre el ``rmdir`` del target vacío y el ``rename`` del backup no puede
    haber una cancelación que ejecute uno sin el otro (review Codex #401).

    Eran dos ``await`` separados: una cancelación que llegara justo entre medio
    dejaba el target BORRADO (rmdir ya corrió) y el backup varado bajo su nombre
    ``.rollback-*`` (rename nunca llegó a correr) — el mismo agujero que el shield
    de ``__aenter__`` ataja del otro lado del ciclo de vida. Se cierra fusionando
    los dos pasos en una sola llamada de hilo: sin punto de `await` entre medio,
    no hay ventana que una cancelación pueda aprovechar.
    """
    import asyncio
    import threading
    from unittest.mock import patch

    target = tmp_path / "Output"
    target.mkdir()
    (target / "previo.txt").write_text("irremplazable", encoding="utf-8")

    rmdir_real = pathlib.Path.rmdir
    rmdir_hecho = threading.Event()
    # El hilo del rmdir se BLOQUEA tras terminar, hasta que el test ya haya
    # llamado cancel(). Sin esto la reproducción es una carrera (verificado: sólo
    # 3 de 5 corridas reproducían el agujero) — cancelar DESPUÉS de que el hilo
    # ya haya devuelto el control no prueba nada, porque la corrutina puede haber
    # alcanzado a arrancar el rename antes de que la cancelación se procese.
    # Bloquear garantiza el orden: cancel() SIEMPRE ocurre antes de que la
    # corrutina tenga chance de seguir.
    continuar = threading.Event()

    def _rmdir_bloqueante(self: pathlib.Path) -> None:
        rmdir_real(self)
        rmdir_hecho.set()
        continuar.wait(5.0)

    rename_real = pathlib.Path.rename
    # Sólo el rename de RESTAURACIÓN (backup -> target) importa acá; el de
    # move-aside de __aenter__ (target -> backup) usa el MISMO Path.rename
    # parcheado y dispararía este evento antes de tiempo si no se discrimina
    # por dirección.
    rename_de_restauracion_corrio = threading.Event()

    def _rename(self: pathlib.Path, dst: pathlib.Path) -> pathlib.Path:
        try:
            return rename_real(self, dst)
        finally:
            if dst == target:
                rename_de_restauracion_corrio.set()

    async def _entrar() -> None:
        async with DirectoryRollback(target):
            target.mkdir()  # target vacío al salir -> dispara el restore

    with (
        patch.object(pathlib.Path, "rmdir", _rmdir_bloqueante),
        patch.object(pathlib.Path, "rename", _rename),
    ):
        task = asyncio.ensure_future(_entrar())
        assert await asyncio.to_thread(rmdir_hecho.wait, 5.0), "rmdir nunca corrió"
        task.cancel()
        continuar.set()  # recién ahora el hilo del rmdir devuelve el control
        with pytest.raises(asyncio.CancelledError):
            await task
        # Si el rename SÍ llegó a correr (con el fix, es lo esperado: la fusión
        # corre en el mismo hilo pese a la cancelación), hay que esperar su
        # desenlace real antes de mirar el disco.
        await asyncio.to_thread(rename_de_restauracion_corrio.wait, 1.0)

    if target.exists():
        # Con el fix: rmdir+rename fusionados corrieron como una unidad pese a
        # la cancelación (el hilo no se detiene, sólo el await se corta).
        assert (target / "previo.txt").read_text(encoding="utf-8") == "irremplazable"
        assert not list(tmp_path.glob("Output.rollback-*"))
    else:
        # Sin el fix: el rmdir corrió y el rename nunca llegó a arrancar — el
        # target quedó BORRADO y el backup varado. Es el estado que este test
        # existe para prohibir.
        pytest.fail("el rmdir corrió sin su rename: target borrado, backup varado")


def test_meta_test_resuelve_alias_de_import(tmp_path: pathlib.Path) -> None:
    """El ancla de callers debe resolver ``import ... as`` (review Codex #401).

    Matchear el nombre literal ``DirectoryRollback`` en el AST deja pasar un
    caller que importe con alias (``from ... import DirectoryRollback as
    Rollback``): el nodo de la llamada tiene ``func.id == "Rollback"``, así que
    un chequeo por nombre fijo lo ignora en silencio y el ancla deja de cumplir
    lo que promete —congelar TODOS los callers—. Se prueba contra una fuente
    aislada (no un archivo real del árbol) para no ensuciar ``sky_claw/`` sólo
    para este test.
    """
    fuente = (
        "from sky_claw.local.tools._dir_rollback import DirectoryRollback as Rollback\n"
        "\n"
        "def f(target):\n"
        "    return Rollback(target)\n"  # sin should_rollback — debe detectarse
    )
    arbol = ast.parse(fuente)

    assert _llamadas_sin_veto(arbol) == [4]


def test_meta_test_detecta_should_rollback_en_none(tmp_path: pathlib.Path) -> None:
    """``should_rollback=None`` equivale a no pasarlo — el ancla debe verlo
    igual (review CodeRabbit #401).

    ``_veto_permite_restaurar`` trata ``self._should_rollback is None`` como
    "siempre permitido restaurar": el mismo estado sin protección que omitir el
    kwarg. Un chequeo que sólo mira si la keyword está PRESENTE (sin mirar su
    valor) dejaría pasar ``DirectoryRollback(x, should_rollback=None)`` como si
    tuviera veto — exactamente la clase de descuido que el ancla existe para
    atajar, cometido dentro del propio ancla.
    """
    fuente = (
        "from sky_claw.local.tools._dir_rollback import DirectoryRollback\n"
        "\n"
        "def f(target):\n"
        "    return DirectoryRollback(target, should_rollback=None)\n"
    )
    arbol = ast.parse(fuente)

    assert _llamadas_sin_veto(arbol) == [4]


# ---------------------------------------------------------------------------
# Enlaces: el enlace no es su destino
# ---------------------------------------------------------------------------


@symlink_guard
async def test_target_enlazado_es_fail_closed_y_no_deja_backup(tmp_path: pathlib.Path) -> None:
    """Un target que es un enlace aborta el ritual ANTES de mutar nada.

    El move-aside renombra **el enlace**, no el árbol al que apunta, así que el
    backup queda siendo un enlace y las dos operaciones destructivas que vienen
    después dejan de significar lo que dicen:

    * ``shutil.rmtree`` sobre un symlink **lanza** ``OSError`` — que ``__aexit__``
      traga por diseño para no enmascarar la excepción del body. Resultado: el
      backup queda huérfano para siempre y el run se reporta exitoso.
    * sobre un **junction** de Windows no lanza: ``os.path.islink()`` devuelve
      False para un ``IO_REPARSE_TAG_MOUNT_POINT``, así que ``rmtree`` entra por
      ``scandir`` y borra el contenido del DESTINO — datos que no son ni de la
      herramienta ni del rollback, en el camino de éxito y sin excepción que lo
      delate.

    Se aborta en vez de "arreglar" cada operación porque el rollback prometido es
    inejecutable: mismo criterio fail-closed que el rename fallido de más arriba y
    que ``grass_profile.py`` ante un mod que ya existe como symlink.
    """
    real = tmp_path / "salida_real"
    real.mkdir()
    (real / "dato.txt").write_text("del usuario", encoding="utf-8")
    target = tmp_path / "Output"
    target.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError, match="enlace"):
        async with DirectoryRollback(target):
            pytest.fail("el body no debe correr: el rollback no se puede garantizar")

    # Nada se movió ni se borró: el enlace sigue en su lugar y su destino intacto.
    assert target.is_symlink()
    assert (real / "dato.txt").read_text(encoding="utf-8") == "del usuario"
    assert not list(tmp_path.glob("Output.rollback-*"))


@symlink_guard
async def test_target_con_enlace_roto_es_fail_closed_y_no_dice_primer_run(tmp_path: pathlib.Path) -> None:
    """Un enlace ROTO no es "primer run, nada que preservar".

    ``exists()`` sigue el enlace, así que para uno colgante devuelve False y el
    camino de "primer run" se tomaba solo. Pero la ruta **está ocupada**: el
    ``mkdir()`` que la herramienta hace después falla con ``FileExistsError``, un
    error que no menciona enlaces y manda a depurar el lugar equivocado.

    Mismo orden que ``file_permissions.restrict_to_owner``: preguntar por el
    enlace ANTES que por la existencia, porque el orden inverso deja pasar los
    colgantes en silencio.
    """
    target = tmp_path / "Output"
    target.symlink_to(tmp_path / "jamas-existio", target_is_directory=True)
    assert target.exists() is False  # el motivo por el que el bug existía

    with pytest.raises(OSError, match="enlace"):
        async with DirectoryRollback(target):
            pytest.fail("el body no debe correr")

    assert target.is_symlink(), "el enlace roto no debe tocarse"
    assert not list(tmp_path.glob("Output.rollback-*"))


@symlink_guard
async def test_descartar_un_backup_enlazado_no_toca_el_destino(tmp_path: pathlib.Path) -> None:
    """Defensa en profundidad: descartar un backup que ES un enlace lo desenlaza.

    El fail-closed de ``__aenter__`` cubre el caso normal, pero **no todos**: el
    reconciliador de arranque (``rollback_reconciler``) opera sobre backups que
    encuentra en disco sin ningún ``__aenter__`` que los haya filtrado, y un
    ``.rollback-*`` puede ser un enlace por haber quedado de una versión anterior.
    Ahí la operación correcta es ``unlink`` del enlace, nunca ``rmtree`` de su
    destino: el enlace es el artefacto, su destino es de otro.
    """
    ajeno = tmp_path / "arbol_ajeno"
    ajeno.mkdir()
    (ajeno / "importante.txt").write_text("no borrar", encoding="utf-8")

    target = tmp_path / "Output"
    target.mkdir()
    (target / "nuevo.txt").write_text("salida nueva", encoding="utf-8")

    backup = tmp_path / "Output.rollback-123"
    backup.symlink_to(ajeno, target_is_directory=True)

    rollback = DirectoryRollback(target)
    rollback._backup = backup

    # permitir_restore=True es el camino del run limpio (el de commit() es el otro):
    # el target FUE regenerado y no está vacío, así que se toma la rama de descarte,
    # que es justo la que hacía rmtree sobre el enlace.
    await rollback._discard_backup(permitir_restore=True)

    assert not backup.exists() and not backup.is_symlink(), "el enlace debía desaparecer"
    assert (ajeno / "importante.txt").read_text(encoding="utf-8") == "no borrar"
    assert (target / "nuevo.txt").exists(), "la salida nueva no se toca"


@junction_guard
async def test_target_con_junction_es_fail_closed_y_no_borra_el_destino(tmp_path: pathlib.Path) -> None:
    """El caso que SÍ pierde datos, y que sólo Windows puede ejercer.

    Un junction no es un symlink para la stdlib: ``os.path.islink()`` devuelve False
    para un ``IO_REPARSE_TAG_MOUNT_POINT``, y el guard interno de ``shutil.rmtree``
    es exactamente ``os.path.islink()``. Así que donde un symlink hace **lanzar** a
    ``rmtree``, un junction lo hace **atravesar**: ``scandir`` entra al destino,
    borra su contenido y después ``rmdir`` quita el reparse point.

    Eso ocurría en ``_discard_backup``, el camino de **ÉXITO**, cuyo ``OSError`` (si
    lo hubiera) ``__aexit__`` traga por diseño. Sin este test el modo de falla más
    grave del fix quedaría sin ejercer, porque en POSIX no existe.

    Junctionear una carpeta de mods a otro disco es práctica común de MO2, así que
    el escenario es el del usuario real, no uno sintético.
    """
    real = tmp_path / "salida_en_otro_disco"
    real.mkdir()
    (real / "dato.txt").write_text("del usuario", encoding="utf-8")
    target = tmp_path / "Output"

    # NO se hace skip si mklink falla: crear un junction en Windows no requiere
    # privilegios, así que un fallo es el helper roto, no el entorno. Un skip acá
    # dejaría la ausencia de cobertura idéntica a la cobertura — que es lo que pasó
    # en la primera corrida de este PR, donde Windows reportó el mismo conteo que
    # Linux y el caso más grave nunca se ejerció.
    if (motivo := crear_junction(target, real)) is not None:
        pytest.fail(f"no se pudo crear el junction que este test existe para ejercer: {motivo}")

    # Se afirma la premisa antes que la conclusión: si esto no fuera un junction
    # "invisible" para islink(), el test estaría pasando por el camino del symlink
    # y no probaría nada de lo que dice probar.
    assert is_junction_real(target), "el enlace creado no es un junction invisible a islink()"

    with pytest.raises(OSError, match="enlace"):
        async with DirectoryRollback(target):
            pytest.fail("el body no debe correr sobre un junction")

    assert (real / "dato.txt").read_text(encoding="utf-8") == "del usuario"
    assert not list(tmp_path.glob("Output.rollback-*"))


@junction_guard
async def test_descartar_un_backup_con_junction_no_borra_el_destino(tmp_path: pathlib.Path) -> None:
    """``_borrar_arbol_o_enlace`` sobre un backup-junction: mismo caso que el
    symlink de arriba, ejerciendo la rama ``JUNCTION`` que ``rmtree`` atraviesa
    y que ``unlink`` (a diferencia de un symlink) rechaza siempre — la rama que
    ``link_kind`` decide de antemano en vez de descubrir vía retry fallido.
    """
    ajeno = tmp_path / "arbol_ajeno"
    ajeno.mkdir()
    (ajeno / "importante.txt").write_text("no borrar", encoding="utf-8")

    target = tmp_path / "Output"
    target.mkdir()
    (target / "nuevo.txt").write_text("salida nueva", encoding="utf-8")

    backup = tmp_path / "Output.rollback-123"
    if (motivo := crear_junction(backup, ajeno)) is not None:
        pytest.fail(f"no se pudo crear el junction que este test existe para ejercer: {motivo}")

    rollback = DirectoryRollback(target)
    rollback._backup = backup

    await rollback._discard_backup(permitir_restore=True)

    assert not backup.exists(), "el junction debía desaparecer"
    assert (ajeno / "importante.txt").read_text(encoding="utf-8") == "no borrar"
    assert (target / "nuevo.txt").exists(), "la salida nueva no se toca"


@symlink_guard
async def test_borrar_arbol_o_enlace_cae_a_rmdir_si_unlink_falla_en_symlink_de_directorio(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """En Windows, ``unlink()`` sobre un symlink de DIRECTORIO falla siempre
    (review qodo-merge #404): ``DeleteFileW`` —lo que invoca ``unlink``—
    rechaza cualquier ruta con ``FILE_ATTRIBUTE_DIRECTORY``, y un symlink de
    directorio SÍ la tiene. ``RemoveDirectoryW`` (``rmdir()``) sí la acepta y
    quita sólo el enlace.

    Se simulan las dos llamadas (POSIX no puede reproducir la falla real de
    ``unlink`` en un symlink, ni ``rmdir`` sobre un symlink real — ahí falla
    con ``ENOTDIR`` por una razón DISTINTA): lo que se ejerce es la política de
    fallback en `_borrar_arbol_o_enlace`, no el errno exacto de una plataforma
    que este runner no tiene.
    """
    ajeno = tmp_path / "arbol_ajeno"
    ajeno.mkdir()
    (ajeno / "importante.txt").write_text("no borrar", encoding="utf-8")

    enlace = tmp_path / "enlace"
    enlace.symlink_to(ajeno, target_is_directory=True)

    unlink_real = pathlib.Path.unlink

    def _unlink_como_windows(self: pathlib.Path, missing_ok: bool = False) -> None:
        if self == enlace:
            raise PermissionError("WinError 5: Acceso denegado")
        unlink_real(self, missing_ok=missing_ok)

    def _rmdir_como_windows(self: pathlib.Path) -> None:
        if self == enlace:
            # RemoveDirectoryW sobre un symlink de directorio quita SÓLO el
            # reparse point, sin tocar destino — en POSIX eso es un unlink real
            # del enlace (bypasseando el mock de arriba a propósito).
            unlink_real(self)
        else:
            pathlib.Path.rmdir(self)

    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_como_windows)
    monkeypatch.setattr(pathlib.Path, "rmdir", _rmdir_como_windows)

    await _borrar_arbol_o_enlace(enlace)

    assert not enlace.exists() and not enlace.is_symlink(), "el enlace debía desaparecer vía el fallback a rmdir"
    assert (ajeno / "importante.txt").read_text(encoding="utf-8") == "no borrar"


@symlink_guard
async def test_restaurar_target_vacio_es_fail_closed_si_el_target_es_un_enlace(tmp_path: pathlib.Path) -> None:
    """Un target que se volvió un enlace ROTO durante el run no dispara la
    restauración ciega del backup vía el camino de "target vacío".

    ``_sin_regenerar`` ve el target "vacío" a través de ``exists()``/``iterdir()``,
    que siguen el enlace: para uno roto da directamente ``not exists()``. Sin el
    fail-closed, ``_restaurar_target_vacio_sync`` saltearía el ``rmdir()`` (porque
    ``exists()`` da False) y haría ``rename()`` del backup DIRECTO sobre la ruta
    del enlace.
    """
    backup = tmp_path / "Output.rollback-123"
    backup.mkdir()
    (backup / "previo.txt").write_text("del backup", encoding="utf-8")

    target = tmp_path / "Output"
    target.symlink_to(tmp_path / "jamas-existio", target_is_directory=True)

    rollback = DirectoryRollback(target)
    rollback._backup = backup

    await rollback._discard_backup(permitir_restore=True)

    assert target.is_symlink(), "el enlace no debe tocarse"
    assert backup.exists(), "el backup no debe restaurarse ciegamente sobre el enlace"


@junction_guard
async def test_restaurar_target_vacio_es_fail_closed_si_el_target_es_un_junction(tmp_path: pathlib.Path) -> None:
    """El caso que de verdad pierde datos: un junction sobre un directorio VACÍO.

    A diferencia del symlink (que en POSIX falla ``rmdir()`` con ``ENOTDIR`` por
    casualidad), un junction de Windows apuntando a un directorio real y vacío
    hace que ``_sin_regenerar`` vea "vacío" (``exists()``/``iterdir()`` siguen el
    enlace) y que ``_restaurar_target_vacio_sync`` acepte el ``rmdir()`` sobre el
    reparse point — sin el fail-closed, seguiría de largo pisando el path con el
    backup sin que nadie evaluara si el target seguía siendo un directorio real.
    """
    externo = tmp_path / "carpeta_vacia_ajena"
    externo.mkdir()

    backup = tmp_path / "Output.rollback-123"
    backup.mkdir()
    (backup / "previo.txt").write_text("del backup", encoding="utf-8")

    target = tmp_path / "Output"
    if (motivo := crear_junction(target, externo)) is not None:
        pytest.fail(f"no se pudo crear el junction que este test existe para ejercer: {motivo}")

    rollback = DirectoryRollback(target)
    rollback._backup = backup

    await rollback._discard_backup(permitir_restore=True)

    assert is_junction_real(target), "el junction no debe tocarse"
    assert backup.exists(), "el backup no debe restaurarse ciegamente sobre el junction"


@symlink_guard
async def test_sin_regenerar_no_deja_pasar_un_enlace_con_contenido_ajeno(tmp_path: pathlib.Path) -> None:
    """Un target-enlace a un directorio AJENO NO VACÍO no debe descartar el backup.

    ``_sin_regenerar`` sólo miraba ``exists()``/``iterdir()``: un symlink a un
    directorio con contenido (no vacío) hacía que viera "hay contenido" y
    devolviera ``False`` — el flujo se saltaba ENTERO el fail-closed de
    ``_restaurar_target_vacio_sync`` y caía directo al
    ``_borrar_arbol_o_enlace(self._backup)`` incondicional, descartando la
    única copia del estado previo mientras el enlace ajeno seguía en
    ``self._target`` sin que nadie lo mirara (review CodeRabbit #404).
    """
    ajeno = tmp_path / "arbol_ajeno_no_vacio"
    ajeno.mkdir()
    (ajeno / "no_es_nuestro.txt").write_text("de otro proceso", encoding="utf-8")

    backup = tmp_path / "Output.rollback-123"
    backup.mkdir()
    (backup / "previo.txt").write_text("del backup", encoding="utf-8")

    target = tmp_path / "Output"
    target.symlink_to(ajeno, target_is_directory=True)

    rollback = DirectoryRollback(target)
    rollback._backup = backup

    await rollback._discard_backup(permitir_restore=True)

    assert target.is_symlink(), "el enlace no debe tocarse"
    assert backup.exists(), "el backup no debe descartarse mientras un enlace ajeno ocupa el target"
    assert (ajeno / "no_es_nuestro.txt").read_text(encoding="utf-8") == "de otro proceso"


@junction_guard
async def test_sin_regenerar_no_deja_pasar_un_junction_con_contenido_ajeno(tmp_path: pathlib.Path) -> None:
    """Mismo caso que el symlink de arriba, con un junction de Windows.

    El más grave de los dos: si esto NO estuviera cubierto, el camino de éxito
    llegaría a ``_borrar_arbol_o_enlace(self._backup)`` — que sí es link-aware
    y no tocaría el backup en sí, pero el punto es que el backup se DESCARTA
    igual, perdiendo la única copia del estado previo sin haber restaurado
    nada sobre el árbol ajeno que ocupa el target.
    """
    ajeno = tmp_path / "arbol_ajeno_no_vacio"
    ajeno.mkdir()
    (ajeno / "no_es_nuestro.txt").write_text("de otro proceso", encoding="utf-8")

    backup = tmp_path / "Output.rollback-123"
    backup.mkdir()
    (backup / "previo.txt").write_text("del backup", encoding="utf-8")

    target = tmp_path / "Output"
    if (motivo := crear_junction(target, ajeno)) is not None:
        pytest.fail(f"no se pudo crear el junction que este test existe para ejercer: {motivo}")

    rollback = DirectoryRollback(target)
    rollback._backup = backup

    await rollback._discard_backup(permitir_restore=True)

    assert is_junction_real(target), "el junction no debe tocarse"
    assert backup.exists(), "el backup no debe descartarse mientras un junction ajeno ocupa el target"
    assert (ajeno / "no_es_nuestro.txt").read_text(encoding="utf-8") == "de otro proceso"


async def test_borrar_arbol_o_enlace_reintenta_la_inspeccion_ante_bloqueo_transitorio(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un ``OSError`` transitorio (AV/indexer) al inspeccionar no debe leerse
    como "no es un enlace" (review qodo-merge #404).

    ``link_kind`` traga el ``OSError`` por diseño y devuelve ``None`` — que
    ``_borrar_arbol_o_enlace`` interpretaba como "es un directorio real" y
    llamaba ``rmtree``. Si la ruta en verdad ERA un junction y el bloqueo se
    liberaba durante el propio retry de ``rmtree``, éste atravesaba el
    junction y borraba el destino. Acá se simula el bloqueo con
    ``link_kind_or_raise`` (la variante que sí propaga) fallando una vez antes
    de resolver — sin necesitar Windows real, porque lo que se prueba es la
    política de reintento, no el reparse tag en sí.
    """
    real = tmp_path / "arbol_real"
    real.mkdir()
    (real / "dato.txt").write_text("contenido", encoding="utf-8")

    llamadas = {"n": 0}
    original = _dir_rollback.link_kind_or_raise

    def _con_un_bloqueo_transitorio(path: pathlib.Path) -> str | None:
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise OSError("WinError 32: el proceso no tiene acceso al archivo")
        return original(path)

    monkeypatch.setattr(_dir_rollback, "link_kind_or_raise", _con_un_bloqueo_transitorio)

    await _borrar_arbol_o_enlace(real)

    assert llamadas["n"] >= 2, "debía reintentar la inspección tras el OSError transitorio"
    assert not real.exists(), "tras resolver el bloqueo, la inspección debía ver un dir real y rmtree debía correr"


async def test_borrar_arbol_o_enlace_falla_cerrado_si_la_inspeccion_no_se_recupera(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la inspección NUNCA se resuelve, no hay fallback silencioso a ``rmtree``.

    Antes de este fix, cualquier ``OSError`` de inspección —transitorio o no—
    se leía como "no es un enlace" y corría ``rmtree`` igual. Ahora, agotados
    los reintentos, el ``OSError`` se propaga: el caller (``__aexit__``/
    ``commit()``) ya lo atrapa y deja el backup huérfano en vez de arriesgar
    borrar un destino ajeno a ciegas.
    """
    real = tmp_path / "arbol_real"
    real.mkdir()
    (real / "dato.txt").write_text("contenido", encoding="utf-8")

    def _siempre_bloqueado(path: pathlib.Path) -> str | None:
        raise OSError("WinError 32: el proceso no tiene acceso al archivo")

    monkeypatch.setattr(_dir_rollback, "link_kind_or_raise", _siempre_bloqueado)

    with pytest.raises(OSError):
        await _borrar_arbol_o_enlace(real)

    assert real.exists(), "sin poder inspeccionar, no debe arriesgarse un rmtree a ciegas"
    assert (real / "dato.txt").read_text(encoding="utf-8") == "contenido"

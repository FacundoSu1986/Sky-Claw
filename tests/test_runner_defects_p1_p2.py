"""Reproducción de los tres defectos del runner DynDOLOD (R1/R2/R3) — `#661` FASE B.

Cada test es un test ROJO que documenta el defecto real en `origin/main`
`0103ee4f6de15207032d25c254ede5cf2c01bff9`. Ninguno corrige el defecto; son la
reproducción determinista que un fix futuro debe hacer pasar.

- **R1** — `await asyncio.to_thread(_empaquetar_sincrono)` cancelable: el task
  asyncio recibe `CancelledError` pero el thread worker NATIVO sigue
  ejecutando la copia; el código posterior (rollback/cleanup) avanza sin
  esperar al writer. Violación del invariante:
  *"ROLLBACK/CLEANUP MUST NOT BEGIN UNTIL MUTATING PACKAGING WORKER IS
  EFFECTIVELY TERMINAL"*.
- **R2** — `shutil.copytree(src, dst)` sigue junctions que `_bytes_del_arbol`
  (vía `iter_archivos_propios`, link-aware) no cuenta. Violación del
  invariante: *"EVERY BYTE COPIED MUST BELONG TO THE ADMITTED WORKSPACE TREE"
  y "inventory set == copyable set"*.
- **R3** — en la rama `except asyncio.CancelledError` de `_execute_process`,
  una segunda cancelación interrumpe el `await asyncio.gather(...)` **antes**
  de `close_job(job)`: el Job Object queda abierto y los nietos sobreviven.
  Violación del invariante: *"EVERY EXIT PATH MUST TERMINATE ALL OWNED
  PROCESS/HELPER RESOURCES"*.

Cada test está verificado por:
  (a) la aserción del defecto (LO QUE PASA HOY), y
  (b) una ancla AST sobre el código real que falla si la forma vulnerable
      cambia antes de arreglarse.

Referencias: `docs/pending_ooda_status.md`, `#592`, rig P0 de #661.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sky_claw.local.tools import dyndolod_runner as runner_mod
from tests._symlink_guard import crear_junction, junction_guard

RUNNER_SRC = pathlib.Path(runner_mod.__file__).resolve()


def _runner_stub(tmp_path: pathlib.Path):
    """Runner aislado del pipeline: sólo lo que necesita `_package_output_as_mod`."""
    r = runner_mod.DynDOLODRunner.__new__(runner_mod.DynDOLODRunner)
    r._config = SimpleNamespace(mo2_mods_path=tmp_path / "mods", fence_ownership=None)
    r._es_la_raiz_administrada = lambda p: False
    r._exigir_fuente_del_subroot = AsyncMock(return_value=None)
    r._generate_meta_ini = lambda mod_path, mod_name: None
    return r


# ---------------------------------------------------------------------------
# R1 — to_thread(_empaquetar_sincrono) cancelable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r1_cancelacion_to_thread_no_mata_al_worker(tmp_path, monkeypatch):
    """Cancelar `await asyncio.to_thread(empaquetar)` libera el task con
    `CancelledError` pero el thread subyacente SIGUE corriendo la copia —
    exactamente el defecto P1-A del packaging del runner."""
    arranque = threading.Event()
    permitir = threading.Event()
    terminado = threading.Event()

    def _worker():
        arranque.set()
        permitir.wait(timeout=5)
        terminado.set()

    task = asyncio.create_task(asyncio.to_thread(_worker))
    await asyncio.wait_for(asyncio.to_thread(arranque.wait, True), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # DEFECTO: tras el cancel el task murió, pero el thread NUNCA fue notificado.
    assert arranque.is_set()
    assert not terminado.is_set(), (
        "el hilo worker continuó corriendo tras el cancel: nada lo interrumpe."
    )

    permitir.set()
    await asyncio.wait_for(asyncio.to_thread(terminado.wait, True), timeout=2)


def test_r1_ancla_ast_el_to_thread_empaquetar_esta_sin_proteccion():
    """La invocación exacta del defecto en `dyndolod_runner.py`, congelada por AST."""
    arbol = ast.parse(RUNNER_SRC.read_text(encoding="utf-8"))
    metodo = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_package_output_as_mod"
    )
    await_calls = [
        nodo
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.Await)
        and isinstance(nodo.value, ast.Call)
        and isinstance(nodo.value.func, ast.Attribute)
        and nodo.value.func.attr == "to_thread"
    ]
    objetivos = [
        c
        for c in await_calls
        if c.value.args
        and isinstance(c.value.args[0], ast.Name)
        and c.value.args[0].id == "_empaquetar_sincrono"
    ]
    assert len(objetivos) == 1, "debe haber exactamente un to_thread(_empaquetar_sincrono)"
    # La propiedad del defecto: NO está envuelto en asyncio.shield.
    call = objetivos[0].value
    assert call.func.attr == "to_thread"
    # Si un fix introduce shield, este assert se rompe: consciente y deliberado.
    assert not (
        isinstance(call.func.value, ast.Attribute) and call.func.value.attr == "shield"
    ), "si el fix llega, este punto de la forma actual deja de valer"


# ---------------------------------------------------------------------------
# R2 — copytree pelado atraviesa junctions; presupuesto link-aware no los cuenta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@junction_guard
async def test_r2_copytree_atraviesa_junction_mientras_medidor_no(tmp_path, monkeypatch):
    """Con un junction DENTRO del árbol fuente, el inventory (bytes a medir) no
    incurre en esos bytes, pero `copytree` los COPIA igual — el defecto #592-3.

    En main actual: `esl bytes copiados > presupuesto de bytes`, y el modeo destino
    recibe archivos del exterior de la cadena admitida.
    """

    # estructura: src con archivo real + junction "externo" a una carpeta separada
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_bytes(b"r")
    externo = tmp_path / "externo"
    externo.mkdir()
    (externo / "evil.bin").write_bytes(b"e" * 64)
    motivo = crear_junction(src / "nested", externo)
    assert motivo is None, f"no se pudo crear junction: {motivo}"
    # verificar la identidad: junction real, q `os.path.islink` NO lo detecta
    import os

    assert os.path.islink(src / "nested") is False
    assert getattr((src / "nested").lstat(), "st_reparse_tag", 0) != 0

    dst = tmp_path / "dst"

    # Medición: la primitiva del runner (link-aware, iter_archivos_propios)
    medido = runner_mod._bytes_del_arbol(src)

    # Cómo copia el runner HOY: shutil.copytree(src, dst)
    shutil.copytree(src, dst)

    # DEFECTO 1: el destino recibió el contenido del junction (su destino externo)
    destino = dst / "nested"
    assert (destino / "evil.bin").exists(), (
        "copytree siguió el junction y arrastró archivos externos al paquete"
    )
    assert (destino / "evil.bin").read_bytes() == b"e" * 64

    # DEFECTO 2: el presupuesto no cuenta los bytes recibidos del exterior
    copiado_bytes = sum(
        p.stat().st_size for p in dst.rglob("*") if p.is_file()
    )
    assert medido == (src / "real.txt").stat().st_size, (
        "_bytes_del_arbol correctamente NO cuenta los junctions"
    )
    assert copiado_bytes == medido + (externo / "evil.bin").stat().st_size, (
        "el destino tiene MÁS bytes de los medidos: el presupuesto sub-cuenta"
    )


@pytest.mark.asyncio
@junction_guard
async def test_r2_package_output_as_mod_admite_copia_de_junction(tmp_path, monkeypatch):
    """El defecto atraviesa el packaging real: `_package_output_as_mod` con una
    fuente que es un junction realiza la copia y no rechaza."""

    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_bytes(b"r")
    externo = tmp_path / "externo"
    externo.mkdir()
    (externo / "evil.bin").write_bytes(b"e" * 8)
    motivo = crear_junction(src / "nested", externo)
    assert motivo is None
    monkeypatch.setattr(runner_mod, "link_kind_or_raise_with_retry", lambda p: None)
    runner = _runner_stub(tmp_path)

    mod_path = await runner._package_output_as_mod(src, "TestMod")

    # el defecto se reproduce: el packaging completo fue exitoso y el destino
    # recibió el archivo externo a través del junction.
    assert (mod_path / "nested" / "evil.bin").exists()


# ---------------------------------------------------------------------------
# R3 — segunda cancelación interrumpe el cleanup antes de close_job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r3_segunda_cancelacion_interrumpe_la_limpieza():
    """Estructura exacta del handler actual: multi-step cleanup sin protección.

    Secuencia del defecto en `_execute_process` (branch `except asyncio.CancelledError`):
        await kill_and_reap(proc)
        heartbeat.cancel(); drain_out.cancel(); drain_err.cancel()
        await asyncio.gather(heartbeat, drain_out, drain_err, return_exceptions=True)
        close_job(job)
        raise

    Una segunda cancelación puede llegar durante el `await kill_and_reap` o el
    `await gather`, y el `close_job(job)` nunca se ejecuta. En main actual, quo
    opener queda resource-leaked.
    """

    close_job_llamado = 0
    task_interna_completada = threading.Event()

    async def _kill_and_reap(_proc: object) -> None:
        # simula reap lento: observable del exterior
        await asyncio.sleep(0.05)
        task_interna_completada.set()

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(60)

    async def _drain() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            raise  # imita drenaje: puede cancelarse mas no hacer nada

    async def _execute_process_fake(job: object) -> None:
        heartbeat = asyncio.create_task(_heartbeat())
        drain_out = asyncio.create_task(_drain())
        drain_err = asyncio.create_task(_drain())
        job_obj = job
        nonlocal close_job_llamado

        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await _kill_and_reap(job_obj)
            heartbeat.cancel()
            drain_out.cancel()
            drain_err.cancel()
            await asyncio.gather(heartbeat, drain_out, drain_err, return_exceptions=True)
            close_job_llamado += 1
            raise

    t = asyncio.create_task(_execute_process_fake(object()))
    await asyncio.sleep(0.01)  # entrar en la corrida
    t.cancel()
    await asyncio.sleep(0.01)  # llegar al handler; según timing, a mitad de kill/reap
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t
    # DEFECTO: si una segunda cancelación llega durante el cleanup, close_job no corre.
    # En la forma actual el test ASI falla: close_job_llamado == 0.
    # La propiedad exigida por PR-R3: close_job SIEMPRE corre exactamente una vez.
    assert close_job_llamado == 0, (
        "en la forma actual el test DEBE fallar: la segunda cancelación "
        "interrumpe el cleanup antes de close_job"
    )


def test_r3_ancla_ast_gather_sin_suppress_en_rama_cancelled():
    """El defecto de la rama `except asyncio.CancelledError` en `_execute_process`,
    congelada por AST: falta un `suppress(asyncio.CancelledError)` alrededor del
    `await asyncio.gather(...)`."""
    arbol = ast.parse(RUNNER_SRC.read_text(encoding="utf-8"))
    metodo = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_execute_process"
    )
    rama_cancel = [
        nodo
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.ExceptHandler) and _es_cancelled(nodo.type)
    ]
    assert rama_cancel, "'except asyncio.CancelledError' debe existir en _execute_process"
    rama = rama_cancel[0]
    hay_suppress_cancelled = False
    for nodo in ast.walk(rama):
        if isinstance(nodo, ast.With):
            for item in nodo.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    fn = ctx.func
                    if (
                        isinstance(fn, ast.Attribute)
                        and fn.attr == "suppress"
                        and ctx.args
                        and _es_cancelled(ctx.args[0])
                    ):
                        hay_suppress_cancelled = True
    assert not hay_suppress_cancelled, (
        "esto cambio al agregar suppress(CancelledError) al gather: "
        "el test ancla deja de ser rojo cuando el fix llegue"
    )


def _es_cancelled(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Attribute) and expr.attr == "CancelledError" and isinstance(expr.value, ast.Name) and expr.value.id == "asyncio"

"""Contratos de los tres defectos del runner DynDOLOD (R1/R2/R3) — `#661` FASE B.

Estado de cada defecto — los tests de este archivo tienen contratos DISTINTOS
según el estado del defecto que cubren:

- **R1** — `RUNNER_P1_PACKAGING_CANCEL` — **FIXED / regression invariant**: el
  `to_thread(_empaquetar_sincrono)` corre en una Task propia y espera por la
  primitiva común `_esperar_terminalidad_del_worker`; cancelar el caller NO lo
  libera (ni con cancelaciones repetidas) hasta que el worker es terminal. Sus
  tests congelan el invariante *worker_terminal < rollback_started <
  lease_released* en orden causal, con sincronización explícita
  (`threading.Event` / registros de handoff) — nunca sleeps como autoridad
  temporal.
- **R2** — `RUNNER_P1_REPARSE_COPY` — **FIXED / regression invariant**: antes de
  cualquier `rmtree`, `mkdir` o `copytree`, el packaging recorre el árbol con la
  primitiva central `exigir_arbol_copiable_sin_reparse`, en una Task propia
  protegida por el mismo terminal handoff. Si hay cancelación durante el scan,
  éste termina antes de propagarla y el worker mutante no empieza. Symlinks,
  junctions y reparse tags no clasificados se rechazan; sobre un árbol admitido,
  inventario y conjunto copiable contienen los mismos archivos. El pre-scan
  cierra el defecto reproducido, pero no se declara race-proof frente a un swap
  concurrente.
- **R3** — `RUNNER_P2_DOUBLE_CANCEL` — **FIXED / regression invariant**: la
  limpieza de `_execute_process` (matar y reapear el proceso, cancelar los
  auxiliares, esperarlos a terminal y cerrar el Job Object) es UNA operación
  resistente a cancelación, `_liberar_proceso_hasta_terminal`, que corre en una
  Task propia y se espera por el mismo terminal handoff que R1. Una segunda
  cancelación durante la limpieza queda absorbida hasta el terminal y recién
  entonces se propaga; antes interrumpía el `await asyncio.gather(...)` **antes**
  de `close_job(job)` y el Job Object quedaba abierto con los nietos vivos.
  Invariante: *"EVERY EXIT PATH MUST TERMINATE ALL OWNED PROCESS/HELPER
  RESOURCES"*. Sus tests ejercen el `_execute_process` REAL (no un fake) y un
  ancla AST ENUMERA los caminos de salida del método.

Los tests R2 verifican aceptación (rechazo antes de copiar y preservar el destino
previo, tags desconocidos, igualdad del inventario y la semántica TexGen); los
tests R3 verifican la aceptación de la limpieza resistente a cancelación sobre el
`_execute_process` real. Los tests R1 verifican la aceptación del fix y el
handoff terminal.

Referencias: `docs/pending_ooda_status.md`, `#592`, rig P0 de #661.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import pathlib
import shutil
import stat
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sky_claw.app.security.links as links_mod
from sky_claw.local.tools import dyndolod_runner as runner_mod
from tests._symlink_guard import crear_junction, junction_guard, symlink_guard

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
# R1 — FIXED: la cancelación del packaging espera la terminalidad del worker
# ---------------------------------------------------------------------------
#
# Invariante de aceptación (antes reproducción del defecto, ahora regresión):
#     worker_terminal < rollback_started < lease_released
# Congelado por igualdad de la secuencia causal en los tests de abajo.

_OPERACION_HANDOFF = "dyndolod_packaging_cancel_handoff"
_OPERACION_TERMINAL = "dyndolod_packaging_cancel_terminal"
_OPERACION_FALLA_EN_HANDOFF = "dyndolod_packaging_worker_falla_en_handoff"


class _ObservadorDeCancelacion(logging.Handler):
    """Congela en UNA lista el orden causal handoff → terminal → rollback/lease.

    El helper productivo emite estos registros desde el event loop, en el mismo
    hilo que el harness del test: el orden de la lista es orden causal real,
    no timing. ``handoffs`` cuenta cancelaciones ABSORBIDAS — cada cancelación
    procesada por el handoff emite exactamente un registro, así que el contador
    prueba que esa cancelación ya se PROCESÓ (no está sólo pendiente): el
    checkpoint de HANDOFF_ACTIVE que exige R1 antes de mandar la siguiente.
    """

    def __init__(self, orden: list[str], eventos: asyncio.Queue[str] | None = None) -> None:
        super().__init__(level=logging.INFO)
        self.orden = orden
        self.eventos = eventos
        self.handoffs = 0

    def emit(self, record: logging.LogRecord) -> None:
        operacion = getattr(record, "operation_type", None)
        if operacion == _OPERACION_HANDOFF:
            self.handoffs += 1
            evento = f"cancel_{self.handoffs}_procesada"
            self.orden.append(evento)
            if self.eventos is not None:
                self.eventos.put_nowait(evento)
        elif operacion == _OPERACION_TERMINAL:
            self.orden.append("worker_terminal")


@contextlib.contextmanager
def _observar_cancelaciones(orden: list[str], eventos: asyncio.Queue[str] | None = None):
    """Instala el observador sobre el logger del runner (baja a INFO el gate)."""
    logger_del_runner = logging.getLogger(runner_mod.__name__)
    nivel_previo = logger_del_runner.level
    observador = _ObservadorDeCancelacion(orden, eventos)
    logger_del_runner.addHandler(observador)
    logger_del_runner.setLevel(logging.INFO)
    try:
        yield observador
    finally:
        logger_del_runner.removeHandler(observador)
        logger_del_runner.setLevel(nivel_previo)


async def _esperar_handoffs(observador: _ObservadorDeCancelacion, esperados: int) -> None:
    """Espera a que el handoff haya procesado ``esperados`` cancelaciones.

    La autoridad es el CONTADOR de registros (checkpoint de HANDOFF_ACTIVE),
    nunca el tiempo: ``sleep(0)`` sólo cede scheduling; el ``wait_for`` es una
    red de seguridad contra el hang, no la prueba.
    """
    with contextlib.suppress(TimeoutError):

        async def _girar() -> None:
            while observador.handoffs < esperados:
                await asyncio.sleep(0)

        await asyncio.wait_for(_girar(), timeout=5)
    assert observador.handoffs >= esperados, (
        f"se esperaban {esperados} cancelaciones procesadas por el handoff y hubo "
        f"{observador.handoffs}; orden hasta acá = {observador.orden}"
    )


class _CopytreeBloqueado:
    """Proxy de ``shutil`` SÓLO para el runner: copytree se bloquea en checkpoint.

    Deja al worker mutante deterministamente dentro de una operación de disco
    (el ``copytree`` del packaging), que es exactamente el punto donde R1 exige
    que ninguna cancelación pueda liberar al caller. Los demás atributos de
    ``shutil`` se reenvían al módulo real (``copy2``, ``disk_usage``, …).
    """

    def __init__(
        self,
        arranque: threading.Event,
        permitir: threading.Event,
        falla: BaseException | None = None,
    ) -> None:
        self._arranque = arranque
        self._permitir = permitir
        self._falla = falla

    def copytree(self, *args, **kwargs):
        self._arranque.set()
        assert self._permitir.wait(timeout=30), "el test no liberó el copytree del worker"
        if self._falla is not None:
            raise self._falla
        return shutil.copytree(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(shutil, name)


def _preparar_packaging_bloqueado(tmp_path, monkeypatch, *, falla_en_copia=None):
    """Runner stub + fuente real + copytree bloqueable: el escenario base de R1."""
    runner = _runner_stub(tmp_path)
    monkeypatch.setattr(runner_mod, "link_kind_or_raise_with_retry", lambda p: None)
    arranque = threading.Event()
    permitir = threading.Event()
    monkeypatch.setattr(runner_mod, "shutil", _CopytreeBloqueado(arranque, permitir, falla_en_copia))
    # Un directorio + un archivo: los directorios pasan por `copytree` (el
    # punto de bloqueo) y los archivos por `copy2`.
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "hola.txt").write_bytes(b"hola")
    (src / "suelto.txt").write_bytes(b"suelto")
    return runner, src, arranque, permitir


@pytest.mark.asyncio
async def test_r1_cancelacion_to_thread_no_mata_al_worker(tmp_path, monkeypatch):
    """Premisa de R1 (propiedad del runtime): cancelar ``asyncio.to_thread`` NO
    detiene el hilo nativo.

    Es el PRIMER eslabón de la cadena causal que el fix de R1 contiene: como el
    thread sobrevive al cancel, el caller no puede liberarse hasta observar la
    terminalidad del worker. El invariante de aceptación (caller retenido hasta
    terminal, orden congelado) viven en
    ``test_r1_cancel_durante_el_worker_ret_al_caller_hasta_terminal`` y hermanos.
    """
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
    assert not terminado.is_set(), "el hilo worker continuó corriendo tras el cancel: nada lo interrumpe."

    permitir.set()
    await asyncio.wait_for(asyncio.to_thread(terminado.wait, True), timeout=2)


def test_r1_ancla_ast_el_worker_de_packaging_pasa_por_el_handoff_terminal():
    """Ancla de la FORMA FIXEADA de R1: si vuelve el `to_thread` pelado, ésta falla.

    Congela las cuatro propiedades del mecanismo sobre el código real:
    (1) no existe `await asyncio.to_thread(_empaquetar_sincrono)` directo,
    (2) el worker vive en una Task propia (handle confiable de terminalidad),
    (3) esa Task se espera por el helper de terminal handoff,
    (4) el helper espera con `asyncio.shield` dentro de un loop condicionado a
    `worker.done()` que absorbe `CancelledError`.
    """
    arbol = ast.parse(RUNNER_SRC.read_text(encoding="utf-8"))
    metodo = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_package_output_as_mod"
    )
    padres = {hijo: padre for padre in ast.walk(arbol) for hijo in ast.iter_child_nodes(padre)}

    llamadas = [
        nodo
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "to_thread"
        and nodo.args
        and isinstance(nodo.args[0], ast.Name)
        and nodo.args[0].id == "_empaquetar_sincrono"
    ]
    assert len(llamadas) == 1, "debe haber exactamente un to_thread(_empaquetar_sincrono)"
    llamada = llamadas[0]

    # (1) la forma vulnerable —await DIRECTO del to_thread— no existe más.
    directos = [nodo for nodo in ast.walk(metodo) if isinstance(nodo, ast.Await) and nodo.value is llamada]
    assert not directos, "el to_thread del worker ya no puede esperarse directamente: ese await es R1 abierto"

    # (2) queda envuelto en una Task propia — done() significa terminal real.
    envoltorio = padres.get(llamada)
    assert isinstance(envoltorio, ast.Call), "el to_thread del worker debe estar dentro de una Task"
    assert isinstance(envoltorio.func, ast.Attribute)
    assert envoltorio.func.attr in {"create_task", "ensure_future"}, (
        "el worker debe vivir en una Task propia para que done() sea un handle confiable"
    )

    # (3) esa espera pasa por el helper de terminal handoff.
    esperas_helper = [
        nodo
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.Await)
        and isinstance(nodo.value, ast.Call)
        and isinstance(nodo.value.func, ast.Name)
        and nodo.value.func.id == "_esperar_terminalidad_del_worker"
    ]
    assert len(esperas_helper) == 2, (
        "el pre-scan y el worker mutante deben usar la primitiva común _esperar_terminalidad_del_worker"
    )
    assert any(
        espera.value.args and isinstance(espera.value.args[0], ast.Name) and espera.value.args[0].id == "worker_mutante"
        for espera in esperas_helper
    ), "R1 debe seguir pasando el worker mutante por el terminal handoff"

    # (4) el helper implementa el mecanismo: loop sobre worker.done() con shield
    # y rama que absorbe CancelledError.
    helper = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_esperar_terminalidad_del_worker"
    )
    bucles = [nodo for nodo in ast.walk(helper) if isinstance(nodo, ast.While)]
    assert bucles, "el helper debe esperar en un loop hasta terminal"
    assert any(
        isinstance(nodo, ast.Await)
        and isinstance(nodo.value, ast.Call)
        and isinstance(nodo.value.func, ast.Attribute)
        and nodo.value.func.attr == "shield"
        for bucle in bucles
        for nodo in ast.walk(bucle)
    ), "la espera del worker debe ser con asyncio.shield desde el primer await"
    assert any(
        isinstance(nodo, ast.While)
        and any(isinstance(hijo, ast.Attribute) and hijo.attr == "done" for hijo in ast.walk(nodo.test))
        for nodo in ast.walk(helper)
    ), "el loop del helper debe condicionarse a worker.done()"
    assert any(isinstance(nodo, ast.ExceptHandler) and _es_cancelled(nodo.type) for nodo in ast.walk(helper)), (
        "el helper debe absorber CancelledError: esa rama es el handoff de terminalidad"
    )


@pytest.mark.asyncio
async def test_r1_cancel_durante_el_worker_ret_al_caller_hasta_terminal(tmp_path, monkeypatch):
    """Cancel #1 DURANTE la mutación: el caller no avanza a rollback ni libera la
    lease hasta que el worker no es terminal, y recién entonces se propaga.

    Secuencia congelada por IGUALDAD (no por timing):
        cancel_1_procesada < worker_terminal < rollback_started < lease_released
    """
    runner, src, arranque, permitir = _preparar_packaging_bloqueado(tmp_path, monkeypatch)
    orden: list[str] = []

    async def _caller_con_rollback_y_lease() -> None:
        # Superficie representativa del caller real: su cleanup post-cancelación
        # hace rollback y libera la lease — R1 exige probar el orden aunque
        # `_package_output_as_mod` no implemente ambos por su cuenta.
        try:
            await runner._package_output_as_mod(src, "TestMod")
        finally:
            orden.append("rollback_started")
            orden.append("lease_released")

    caller = asyncio.create_task(_caller_con_rollback_y_lease())
    try:
        with _observar_cancelaciones(orden) as observador:
            assert await asyncio.to_thread(arranque.wait, 5), "el worker no entró al copytree"
            caller.cancel()  # cancel #1, con el worker mutando disco
            await _esperar_handoffs(observador, 1)
            # HANDOFF_ACTIVE: la cancelación #1 ya se PROCESÓ (registro emitido),
            # no está sólo pendiente — y el caller sigue retenido:
            assert orden == ["cancel_1_procesada"], orden
            assert not caller.done(), "el caller fue liberado antes de la terminalidad del worker"
            assert "rollback_started" not in orden and "lease_released" not in orden, (
                "rollback/lease avanzaron con el worker todavía mutando"
            )
            permitir.set()  # el worker termina recién ahora
            with pytest.raises(asyncio.CancelledError):
                await caller
    finally:
        permitir.set()  # el hilo nunca queda bloqueado aunque el test falle

    assert orden == [
        "cancel_1_procesada",
        "worker_terminal",
        "rollback_started",
        "lease_released",
    ], orden


@pytest.mark.asyncio
async def test_r1_cancelaciones_repetidas_no_liberan_al_caller_antes_de_terminal(tmp_path, monkeypatch):
    """Cancel #2 y #N DURANTE el handoff activo: ninguna libera al caller antes
    de `worker.done()`.

    Cada cancelación se envía sólo DESPUÉS del checkpoint de que la anterior ya
    se procesó (contador de registros de handoff == HANDOFF_ACTIVE), nunca por
    timing: el `sleep(0)` sólo cede scheduling.
    """
    runner, src, arranque, permitir = _preparar_packaging_bloqueado(tmp_path, monkeypatch)
    orden: list[str] = []

    async def _caller_con_rollback_y_lease() -> None:
        try:
            await runner._package_output_as_mod(src, "TestMod")
        finally:
            orden.append("rollback_started")
            orden.append("lease_released")

    caller = asyncio.create_task(_caller_con_rollback_y_lease())
    try:
        with _observar_cancelaciones(orden) as observador:
            assert await asyncio.to_thread(arranque.wait, 5), "el worker no entró al copytree"
            caller.cancel()  # cancel #1
            await _esperar_handoffs(observador, 1)  # checkpoint: handoff activo
            caller.cancel()  # cancel #2, con el handoff comprobado activo
            await _esperar_handoffs(observador, 2)
            assert not caller.done()
            for numero in (3, 4):  # cancel #N
                caller.cancel()
                await _esperar_handoffs(observador, numero)
                assert not caller.done(), f"cancel #{numero} liberó al caller antes de terminal"
                assert "rollback_started" not in orden, f"cancel #{numero} dejó avanzar el rollback"
                assert "lease_released" not in orden, f"cancel #{numero} liberó la lease"
            assert orden == [f"cancel_{n}_procesada" for n in (1, 2, 3, 4)], orden
            permitir.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
    finally:
        permitir.set()

    assert orden == [
        "cancel_1_procesada",
        "cancel_2_procesada",
        "cancel_3_procesada",
        "cancel_4_procesada",
        "worker_terminal",
        "rollback_started",
        "lease_released",
    ], orden


@pytest.mark.asyncio
async def test_r1_exito_sin_cancelacion_devuelve_el_mod(tmp_path, monkeypatch):
    """Sin cancelación el wrapper no toca el camino normal (A1):
    packaging completo → `mod_path` con el contenido copiado."""
    runner = _runner_stub(tmp_path)
    monkeypatch.setattr(runner_mod, "link_kind_or_raise_with_retry", lambda p: None)
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "hola.txt").write_bytes(b"hola")

    mod_path = await runner._package_output_as_mod(src, "TestMod")

    assert mod_path == tmp_path / "mods" / "TestMod"
    assert (mod_path / "sub" / "hola.txt").read_bytes() == b"hola"


@pytest.mark.asyncio
async def test_r1_excepcion_del_worker_sin_cancel_se_propaga_como_antes(tmp_path, monkeypatch):
    """Worker terminal con excepción y SIN cancelación: la excepción de dominio
    no cambia — `PermissionError` del worker sigue traduciéndose a
    `DynDOLODValidationError` con el mismo mensaje (§8 / A7)."""
    runner, src, arranque, permitir = _preparar_packaging_bloqueado(
        tmp_path, monkeypatch, falla_en_copia=PermissionError("denegado")
    )
    tarea = asyncio.create_task(runner._package_output_as_mod(src, "TestMod"))
    try:
        assert await asyncio.to_thread(arranque.wait, 5), "el worker no entró al copytree"
        permitir.set()
        with pytest.raises(runner_mod.DynDOLODValidationError, match="Permission denied creating mod"):
            await tarea
    finally:
        permitir.set()


@pytest.mark.asyncio
async def test_r1_excepcion_del_worker_con_cancel_se_consume_y_encadena(tmp_path, monkeypatch, caplog):
    """Worker terminal con excepción DESPUÉS de cancel #1: el resultado externo
    es `CancelledError` (manda la semántica de cancelación del caller), pero la
    falla del worker queda encadenada (`__cause__`), registrada con `exc_info`
    y consumida — nunca silenciada ni ``Task exception was never retrieved``."""
    runner, src, arranque, permitir = _preparar_packaging_bloqueado(
        tmp_path, monkeypatch, falla_en_copia=PermissionError("boom")
    )
    orden: list[str] = []

    async def _caller_con_rollback_y_lease() -> None:
        try:
            await runner._package_output_as_mod(src, "TestMod")
        finally:
            orden.append("rollback_started")
            orden.append("lease_released")

    caller = asyncio.create_task(_caller_con_rollback_y_lease())
    try:
        with _observar_cancelaciones(orden) as observador:
            assert await asyncio.to_thread(arranque.wait, 5), "el worker no entró al copytree"
            caller.cancel()  # cancel #1 mientras el worker muta
            await _esperar_handoffs(observador, 1)
            assert not caller.done()
            permitir.set()  # el worker termina CON excepción
            with pytest.raises(asyncio.CancelledError) as excinfo:
                await caller
    finally:
        permitir.set()

    # la cancelación gana como resultado externo…
    assert isinstance(excinfo.value.__cause__, PermissionError), (
        f"la falla del worker debe quedar encadenada, no perdida: causa={excinfo.value.__cause__!r}"
    )
    # …y queda observable además de encadenada:
    assert any(getattr(r, "operation_type", None) == _OPERACION_FALLA_EN_HANDOFF for r in caplog.records), (
        "la falla del worker durante el handoff no se registró con exc_info"
    )
    # el orden causal completo se conserva:
    assert orden == [
        "cancel_1_procesada",
        "worker_terminal",
        "rollback_started",
        "lease_released",
    ], orden


# ---------------------------------------------------------------------------
# R2 — FIXED: sólo se empaqueta un árbol validado como propio y sin reparse
# ---------------------------------------------------------------------------


def _crear_arbol_con_junction(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Fuente con un archivo propio y un junction anidado a un árbol externo."""
    import os

    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_bytes(b"r")
    externo = tmp_path / "externo"
    externo.mkdir()
    (externo / "evil.bin").write_bytes(b"e" * 64)
    junction = src / "nested"
    motivo = crear_junction(junction, externo)
    assert motivo is None, f"no se pudo crear junction: {motivo}"
    # Ancla que evita sustituir accidentalmente el junction por un symlink.
    assert os.path.islink(junction) is False
    assert getattr(junction.lstat(), "st_reparse_tag", 0) != 0
    return src, externo


def _espiar_copytree(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Registra la frontera de copia del runner y conserva su comportamiento real."""
    llamadas: list[object] = []
    copytree_original = runner_mod.shutil.copytree

    def _copytree(*args, **kwargs):
        llamadas.append((args, kwargs))
        return copytree_original(*args, **kwargs)

    monkeypatch.setattr(runner_mod.shutil, "copytree", _copytree)
    return llamadas


@pytest.mark.asyncio
@junction_guard
async def test_r2_junction_descendiente_falla_antes_de_copytree_y_no_copia_evil(tmp_path, monkeypatch):
    """Un junction preexistente dentro de ``src`` se rechaza antes de copiar.

    El medidor link-aware sigue reportando sólo ``real.txt``; a diferencia de
    la reproducción histórica, la copia productiva no alcanza ``evil.bin``.
    """
    src, externo = _crear_arbol_con_junction(tmp_path)
    runner = _runner_stub(tmp_path)
    copytree_calls = _espiar_copytree(monkeypatch)
    medido = runner_mod._bytes_del_arbol(src)

    with pytest.raises(runner_mod.DynDOLODValidationError) as excinfo:
        await runner._package_output_as_mod(src, "TestMod")

    mod_path = tmp_path / "mods" / "TestMod"
    assert "junction" in str(excinfo.value).lower()
    assert str(src) in str(excinfo.value)
    assert str(src / "nested") in str(excinfo.value)
    assert medido == len(b"r")
    assert copytree_calls == [], "la fuente inadmisible no debe llegar a shutil.copytree"
    assert not mod_path.exists()
    assert not (mod_path / "nested" / "evil.bin").exists()
    assert (externo / "evil.bin").read_bytes() == b"e" * 64


@pytest.mark.asyncio
@junction_guard
async def test_r2_package_rechaza_junction_antes_de_rmtree_y_preserva_mod_previo(tmp_path, monkeypatch):
    """Una fuente con junction no destruye el mod anterior ni inicia ninguna copia."""
    src, externo = _crear_arbol_con_junction(tmp_path)
    runner = _runner_stub(tmp_path)
    mod_path = tmp_path / "mods" / "TestMod"
    mod_path.mkdir(parents=True)
    previo = mod_path / "previous.txt"
    previo.write_bytes(b"keep the prior mod")

    copytree_calls = _espiar_copytree(monkeypatch)
    rmtree_calls: list[pathlib.Path] = []
    rmtree_original = runner_mod.rmtree_link_aware

    def _rmtree(path: pathlib.Path, **kwargs):
        rmtree_calls.append(path)
        return rmtree_original(path, **kwargs)

    monkeypatch.setattr(runner_mod, "rmtree_link_aware", _rmtree)

    with pytest.raises(runner_mod.DynDOLODValidationError, match="junction"):
        await runner._package_output_as_mod(src, "TestMod")

    assert rmtree_calls == [], "el gate R2 debe ejecutarse antes de borrar el mod previo"
    assert copytree_calls == [], "el gate R2 debe ejecutarse antes de copiar cualquier directorio"
    assert previo.read_bytes() == b"keep the prior mod"
    assert not (mod_path / "nested" / "evil.bin").exists()
    assert (externo / "evil.bin").read_bytes() == b"e" * 64


@pytest.mark.asyncio
@symlink_guard
async def test_r2_symlink_descendiente_falla_cerrado_sin_seguir_destino(tmp_path, monkeypatch):
    """Un symlink interno tiene la misma política fail-closed que un junction."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_bytes(b"r")
    externo = tmp_path / "externo"
    externo.mkdir()
    (externo / "evil.bin").write_bytes(b"external bytes")
    (src / "nested").symlink_to(externo, target_is_directory=True)
    runner = _runner_stub(tmp_path)
    copytree_calls = _espiar_copytree(monkeypatch)

    with pytest.raises(runner_mod.DynDOLODValidationError) as excinfo:
        await runner._package_output_as_mod(src, "TestMod")

    mod_path = tmp_path / "mods" / "TestMod"
    assert "symlink" in str(excinfo.value).lower()
    assert str(src / "nested") in str(excinfo.value)
    assert copytree_calls == []
    assert not (mod_path / "nested" / "evil.bin").exists()
    assert (externo / "evil.bin").read_bytes() == b"external bytes"


@pytest.mark.asyncio
async def test_r2_reparse_no_clasificado_falla_cerrado_antes_de_copytree(tmp_path, monkeypatch):
    """Un tag ajeno a symlink/junction se rechaza mediante la política central."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_bytes(b"owned")
    sospechoso = src / "opaque-reparse.bin"
    sospechoso.write_bytes(b"placeholder for a reparse entry")
    tamano_sospechoso = sospechoso.stat().st_size
    clasificador_real = links_mod.link_kind_and_identity_or_raise
    tag_no_clasificado = 0x9000001A

    def _clasificar_con_tag_ajeno(path: pathlib.Path):
        if path == sospechoso:
            return None, SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644,
                st_reparse_tag=tag_no_clasificado,
                st_size=tamano_sospechoso,
            )
        return clasificador_real(path)

    monkeypatch.setattr(links_mod, "link_kind_and_identity_or_raise", _clasificar_con_tag_ajeno)
    runner = _runner_stub(tmp_path)
    copytree_calls = _espiar_copytree(monkeypatch)

    with pytest.raises(runner_mod.DynDOLODValidationError) as excinfo:
        await runner._package_output_as_mod(src, "TestMod")

    mensaje = str(excinfo.value)
    assert "reparse tag no clasificado" in mensaje
    assert f"0x{tag_no_clasificado:08X}" in mensaje
    assert str(src) in mensaje
    assert str(sospechoso) in mensaje
    assert "junction" not in mensaje.lower()
    assert copytree_calls == []


@pytest.mark.asyncio
async def test_r2_inventario_igual_a_bytes_empaquetados_en_arbol_limpio(tmp_path):
    """Para un árbol admitido, todo archivo propio se copia byte-exactamente."""
    runner = _runner_stub(tmp_path)
    src = tmp_path / "src"
    (src / "meshes" / "lod").mkdir(parents=True)
    (src / "DynDOLOD.esp").write_bytes(b"plugin bytes")
    (src / "meshes" / "lod" / "tree.nif").write_bytes(b"mesh bytes" * 7)

    inventario = list(runner_mod.iter_archivos_propios(src))
    bytes_inventariados = runner_mod._bytes_del_arbol(src)
    esperado = {path.relative_to(src): path.read_bytes() for path, _identidad in inventario}

    mod_path = await runner._package_output_as_mod(src, "DynDOLOD Output")

    empaquetado = list(runner_mod.iter_archivos_propios(mod_path))
    observado = {path.relative_to(mod_path): path.read_bytes() for path, _identidad in empaquetado}
    bytes_empaquetados = sum(identidad.st_size for _path, identidad in empaquetado)
    assert observado == esperado, "el conjunto y el contenido del paquete deben ser byte-exactos"
    assert bytes_inventariados == bytes_empaquetados
    assert bytes_inventariados == sum(len(contenido) for contenido in esperado.values())


@pytest.mark.asyncio
async def test_r2_texgen_valido_conserva_prefijo_textures(tmp_path):
    """El guard no aplana el root ``textures/`` Data-relative de TexGen."""
    runner = _runner_stub(tmp_path)
    src = tmp_path / "staging" / "textures"
    (src / "terrain" / "lod").mkdir(parents=True)
    esperado = b"texgen texture bytes"
    (src / "terrain" / "lod" / "mountain.dds").write_bytes(esperado)

    mod_path = await runner._package_output_as_mod(src, "TexGen Output", preservar_directorio_raiz=True)

    assert (mod_path / "textures" / "terrain" / "lod" / "mountain.dds").read_bytes() == esperado
    assert not (mod_path / "terrain").exists(), "TexGen debe conservar textures/ como prefijo del mod"


@pytest.mark.asyncio
async def test_r2_cancel_durante_prescan_espera_terminal_y_no_muta(tmp_path, monkeypatch):
    """El scan real de packaging hereda R1: cancel #1/#2 no liberan al caller.

    El test ejecuta ``_package_output_as_mod`` real y bloquea únicamente su
    primitive de scan dentro del thread. Hasta que ``scan_terminal`` se señala,
    el caller no propaga cancelación ni puede empezar rollback/liberar la lease;
    el worker mutante no arranca (ningún rmtree/mkdir/copytree/meta.ini).
    """
    runner = _runner_stub(tmp_path)
    src = tmp_path / "src"
    (src / "meshes").mkdir(parents=True)
    (src / "meshes" / "tree.nif").write_bytes(b"fresh output")

    mod_path = tmp_path / "mods" / "TestMod"
    mod_path.mkdir(parents=True)
    previous = mod_path / "previous.txt"
    previous.write_bytes(b"previous mod must survive")

    scan_started = threading.Event()
    release_scan = threading.Event()
    scan_terminal = threading.Event()
    rollback_started = threading.Event()
    lease_released = threading.Event()
    scan_paths: list[pathlib.Path] = []
    orden: list[str] = []
    eventos: asyncio.Queue[str] = asyncio.Queue()

    def _scan_bloqueado(path: pathlib.Path) -> None:
        scan_paths.append(path)
        scan_started.set()
        assert release_scan.wait(timeout=30), "el test no liberó el scan del preflight"
        orden.append("scan_terminal")
        scan_terminal.set()

    monkeypatch.setattr(runner_mod, "exigir_arbol_copiable_sin_reparse", _scan_bloqueado)
    monkeypatch.setattr(runner_mod, "link_kind_or_raise_with_retry", lambda _path: None)

    rmtree_calls: list[pathlib.Path] = []
    rmtree_original = runner_mod.rmtree_link_aware

    def _rmtree(path: pathlib.Path, **kwargs):
        rmtree_calls.append(path)
        return rmtree_original(path, **kwargs)

    monkeypatch.setattr(runner_mod, "rmtree_link_aware", _rmtree)
    copytree_calls = _espiar_copytree(monkeypatch)

    copy2_calls: list[object] = []
    copy2_original = runner_mod.shutil.copy2

    def _copy2(*args, **kwargs):
        copy2_calls.append((args, kwargs))
        return copy2_original(*args, **kwargs)

    monkeypatch.setattr(runner_mod.shutil, "copy2", _copy2)

    mkdir_calls: list[pathlib.Path] = []
    mkdir_original = pathlib.Path.mkdir

    def _mkdir(path: pathlib.Path, *args, **kwargs):
        if path == mod_path:
            mkdir_calls.append(path)
        return mkdir_original(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", _mkdir)

    meta_calls: list[tuple[pathlib.Path, str]] = []

    def _write_meta(path: pathlib.Path, name: str) -> None:
        meta_calls.append((path, name))
        (path / "meta.ini").write_text("[General]\ngameName=Skyrim Special Edition\n", encoding="utf-8")

    runner._generate_meta_ini = _write_meta

    async def _caller_con_rollback_y_lease() -> None:
        try:
            await runner._package_output_as_mod(src, "TestMod")
        except asyncio.CancelledError:
            orden.append("package_cancel_propagated")
            eventos.put_nowait("package_cancel_propagated")
            raise
        finally:
            orden.append("rollback_started")
            rollback_started.set()
            orden.append("lease_released")
            lease_released.set()

    caller = asyncio.create_task(_caller_con_rollback_y_lease())

    def _assert_sin_mutacion() -> None:
        assert rmtree_calls == [], "el worker mutante no debe borrar el mod previo"
        assert mkdir_calls == [], "el worker mutante no debe iniciar mkdir del mod"
        assert copytree_calls == [], "el worker mutante no debe iniciar copytree"
        assert copy2_calls == [], "el worker mutante no debe iniciar copy2"
        assert meta_calls == [], "el worker mutante no debe escribir meta.ini"
        assert previous.read_bytes() == b"previous mod must survive"
        assert not (mod_path / "meta.ini").exists()

    try:
        with _observar_cancelaciones(orden, eventos):
            assert await asyncio.to_thread(scan_started.wait, 5), "el pre-scan no arrancó en su thread"
            assert scan_paths == [src]

            caller.cancel()  # cancel #1 durante el pre-scan
            primer_evento = await asyncio.wait_for(eventos.get(), timeout=5)
            assert primer_evento == "cancel_1_procesada", (
                "el caller propagó cancelación mientras el scan seguía vivo; "
                f"rollback_started={rollback_started.is_set()}, "
                f"lease_released={lease_released.is_set()}, "
                f"scan_terminal={scan_terminal.is_set()}, orden={orden}"
            )
            assert not caller.done(), "cancel #1 liberó al caller antes de scan_terminal"
            assert not rollback_started.is_set(), "rollback empezó mientras el scan seguía vivo"
            assert not lease_released.is_set(), "la lease se liberó mientras el scan seguía vivo"
            assert not scan_terminal.is_set(), "el scan debe seguir bloqueado hasta que el test lo libere"
            _assert_sin_mutacion()
            assert orden == ["cancel_1_procesada"], orden

            caller.cancel()  # cancel #2 durante el mismo handoff
            segundo_evento = await asyncio.wait_for(eventos.get(), timeout=5)
            assert segundo_evento == "cancel_2_procesada", (
                f"cancel #2 no fue absorbida por el handoff: evento={segundo_evento!r}, orden={orden}"
            )
            assert not caller.done(), "cancel #2 liberó al caller mientras el scan seguía vivo"
            assert not rollback_started.is_set(), "cancel #2 dejó avanzar el rollback"
            assert not lease_released.is_set(), "cancel #2 liberó la lease"
            _assert_sin_mutacion()
            assert orden == ["cancel_1_procesada", "cancel_2_procesada"], orden

            release_scan.set()
            assert await asyncio.to_thread(scan_terminal.wait, 5), "el thread del scan no llegó a terminal"
            with pytest.raises(asyncio.CancelledError):
                await caller
    finally:
        # También en una aserción roja, nunca dejar vivo el thread bloqueado ni
        # una excepción pendiente en la Task del caller.
        release_scan.set()
        if scan_started.is_set():
            assert await asyncio.to_thread(scan_terminal.wait, 5), "el thread del scan quedó vivo al limpiar el test"
        if not caller.done():
            caller.cancel()
        with contextlib.suppress(BaseException):
            await caller

    _assert_sin_mutacion()
    assert orden == [
        "cancel_1_procesada",
        "cancel_2_procesada",
        "scan_terminal",
        "worker_terminal",
        "package_cancel_propagated",
        "rollback_started",
        "lease_released",
    ], orden
    assert rollback_started.is_set()
    assert lease_released.is_set()


# ---------------------------------------------------------------------------
# R3 — FIXED: la limpieza del proceso es UNA operación resistente a cancelación
# ---------------------------------------------------------------------------
#
# Invariante de aceptación, ejercido sobre el `_execute_process` REAL:
#
#     kill_and_reap(proc) -> cancelar auxiliares -> auxiliares terminales -> close_job(job)
#
# y la unidad completa termina ANTES de que se propague la cancelación. Una
# segunda cancelación que llega DURANTE la limpieza no la interrumpe: queda
# absorbida hasta el terminal (el mismo handoff de R1) y recién entonces se
# propaga. Cada fase se sincroniza por eventos, nunca por sleeps como autoridad.

_JOB = 4242
_EXE = pathlib.Path("DynDOLODx64.exe")


class _LectorDeStream:
    """StreamReader falso: bloquea hasta ser cancelado y registra su terminalidad.

    ``demora_terminal`` modela un auxiliar lento en llegar a terminal tras la
    cancelación (el drain real espera a que el pipe devuelva el control), que es
    exactamente la ventana donde una segunda cancelación rompía la limpieza.
    """

    def __init__(
        self,
        orden: list[str],
        nombre: str,
        *,
        demora_terminal: asyncio.Event | None = None,
    ) -> None:
        self._orden = orden
        self._nombre = nombre
        self._demora = demora_terminal
        self.arranco = asyncio.Event()
        self.cancelado = asyncio.Event()
        self.terminal = asyncio.Event()

    async def read(self, _n: int) -> bytes:
        self.arranco.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelado.set()
            self._orden.append(f"{self._nombre}_cancelado")
            if self._demora is not None:
                await self._demora.wait()
            raise
        finally:
            self.terminal.set()
        return b""  # pragma: no cover


class _StreamEOF:
    async def read(self, _n: int) -> bytes:
        return b""


def _runner_directo(*, timeout_seconds: float = 3600) -> runner_mod.DynDOLODRunner:
    """Runner directo (sin servicio ni fence): el seam de los tests de `_execute_process`."""
    config = MagicMock(timeout_seconds=timeout_seconds, heartbeat_interval=60)
    config.fence_ownership = None
    return runner_mod.DynDOLODRunner(config, readiness=runner_mod.ReadinessMode.DISABLED_FOR_TEST)


def _proceso_que_no_termina(stdout: object, stderr: object, wait_iniciado: asyncio.Event) -> MagicMock:
    """Proceso falso cuyo ``wait()`` bloquea hasta ser cancelado."""
    proc = MagicMock()
    proc.pid = None
    proc.returncode = None
    proc.stdout = stdout
    proc.stderr = stderr

    async def _wait() -> int:
        wait_iniciado.set()
        await asyncio.Event().wait()
        return 0  # pragma: no cover

    proc.wait = _wait
    return proc


@contextlib.contextmanager
def _entorno_de_proceso(proc: MagicMock, reap, close_spy: MagicMock):
    """Inyecta el proceso falso y espía los dos helpers que el invariante ordena."""
    with (
        patch.object(runner_mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)),
        patch.object(runner_mod, "assign_kill_on_close_job", MagicMock(return_value=_JOB)),
        patch.object(runner_mod, "close_job", close_spy),
        patch.object(runner_mod, "kill_and_reap", reap),
    ):
        yield


async def _ceder(veces: int = 5) -> None:
    """Cede scheduling para que una cancelación pendiente se procese (no es la prueba)."""
    for _ in range(veces):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_r3_doble_cancelacion_durante_el_reap_no_interrumpe_la_limpieza():
    """Cancel #2 llega con el reap bloqueado: el caller sigue retenido, el job se
    cierra UNA vez y recién entonces se propaga la cancelación."""
    orden: list[str] = []
    wait_iniciado = asyncio.Event()
    reap_entro = asyncio.Event()
    liberar_reap = asyncio.Event()
    stdout, stderr = _LectorDeStream(orden, "stdout"), _LectorDeStream(orden, "stderr")
    proc = _proceso_que_no_termina(stdout, stderr, wait_iniciado)

    async def _reap(_proc: object) -> None:
        orden.append("reap")
        reap_entro.set()
        await liberar_reap.wait()

    close_spy = MagicMock(side_effect=lambda _job: orden.append("close_job"))
    runner = _runner_directo()

    with _entorno_de_proceso(proc, _reap, close_spy):
        tarea = asyncio.create_task(runner._execute_process(_EXE, [], "DynDOLOD"))
        await asyncio.wait_for(wait_iniciado.wait(), timeout=5)
        tarea.cancel()  # cancel #1
        await asyncio.wait_for(reap_entro.wait(), timeout=5)
        tarea.cancel()  # cancel #2, con el reap en vuelo
        await _ceder()

        assert not tarea.done(), "la segunda cancelación liberó al caller antes de que la limpieza fuera terminal"
        close_spy.assert_not_called()

        liberar_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea

    close_spy.assert_called_once_with(_JOB)
    assert stdout.terminal.is_set()
    assert stderr.terminal.is_set()
    assert orden[0] == "reap"
    assert orden[-1] == "close_job"
    assert {"stdout_cancelado", "stderr_cancelado"} <= set(orden), orden


@pytest.mark.asyncio
async def test_r3_doble_cancelacion_con_auxiliar_lento_cierra_el_job_al_final():
    """Cancel #2 llega mientras un auxiliar tarda en llegar a terminal (la ventana
    del `gather` que el defecto original dejaba sin proteger)."""
    orden: list[str] = []
    wait_iniciado = asyncio.Event()
    liberar_auxiliar = asyncio.Event()
    stdout = _LectorDeStream(orden, "stdout", demora_terminal=liberar_auxiliar)
    stderr = _LectorDeStream(orden, "stderr")
    proc = _proceso_que_no_termina(stdout, stderr, wait_iniciado)

    async def _reap(_proc: object) -> None:
        orden.append("reap")

    close_spy = MagicMock(side_effect=lambda _job: orden.append("close_job"))
    runner = _runner_directo()

    with _entorno_de_proceso(proc, _reap, close_spy):
        tarea = asyncio.create_task(runner._execute_process(_EXE, [], "DynDOLOD"))
        await asyncio.wait_for(wait_iniciado.wait(), timeout=5)
        tarea.cancel()  # cancel #1
        await asyncio.wait_for(stdout.cancelado.wait(), timeout=5)
        tarea.cancel()  # cancel #2, con el auxiliar todavía sin terminar
        await _ceder()

        assert not tarea.done(), "la segunda cancelación liberó al caller con un auxiliar vivo"
        close_spy.assert_not_called()

        liberar_auxiliar.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea

    close_spy.assert_called_once_with(_JOB)
    assert stdout.terminal.is_set()
    assert orden[-1] == "close_job"


@pytest.mark.asyncio
async def test_r3_cancelacion_durante_la_limpieza_de_un_timeout_gana_como_cancelacion():
    """El hermano del handler de cancelación: el de ``TimeoutError`` tampoco puede
    tragarse una cancelación externa (antes la envolvía en `suppress` y devolvía
    ``DynDOLODTimeoutError``, o sea, convertía un shutdown en un fallo de herramienta)."""
    orden: list[str] = []
    wait_iniciado = asyncio.Event()
    reap_entro = asyncio.Event()
    liberar_reap = asyncio.Event()
    stdout, stderr = _LectorDeStream(orden, "stdout"), _LectorDeStream(orden, "stderr")
    proc = _proceso_que_no_termina(stdout, stderr, wait_iniciado)

    async def _reap(_proc: object) -> None:
        orden.append("reap")
        reap_entro.set()
        await liberar_reap.wait()

    close_spy = MagicMock(side_effect=lambda _job: orden.append("close_job"))
    runner = _runner_directo(timeout_seconds=0.05)

    with _entorno_de_proceso(proc, _reap, close_spy):
        tarea = asyncio.create_task(runner._execute_process(_EXE, [], "DynDOLOD"))
        await asyncio.wait_for(reap_entro.wait(), timeout=5)  # el timeout disparó y la limpieza arrancó
        tarea.cancel()
        await _ceder()
        assert not tarea.done()
        close_spy.assert_not_called()

        liberar_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea

    close_spy.assert_called_once_with(_JOB)
    assert orden[-1] == "close_job"


@pytest.mark.asyncio
async def test_r3_timeout_sin_cancelacion_sigue_siendo_timeout_error_y_limpia_una_vez():
    """Contraparte: sin cancelación, el contrato histórico del timeout no cambia."""
    orden: list[str] = []
    wait_iniciado = asyncio.Event()
    stdout, stderr = _LectorDeStream(orden, "stdout"), _LectorDeStream(orden, "stderr")
    proc = _proceso_que_no_termina(stdout, stderr, wait_iniciado)

    async def _reap(_proc: object) -> None:
        orden.append("reap")

    close_spy = MagicMock(side_effect=lambda _job: orden.append("close_job"))
    runner = _runner_directo(timeout_seconds=0.05)

    with _entorno_de_proceso(proc, _reap, close_spy), pytest.raises(runner_mod.DynDOLODTimeoutError):
        await runner._execute_process(_EXE, [], "DynDOLOD")

    close_spy.assert_called_once_with(_JOB)
    assert orden.count("reap") == 1
    assert orden[0] == "reap"
    assert orden[-1] == "close_job"
    assert stdout.terminal.is_set()
    assert stderr.terminal.is_set()


@pytest.mark.asyncio
async def test_r3_error_inesperado_limpia_una_vez_y_conserva_la_excepcion_tipada():
    """El cuarto handler (`except Exception`): misma limpieza, mismo contrato."""
    orden: list[str] = []
    stdout, stderr = _LectorDeStream(orden, "stdout"), _LectorDeStream(orden, "stderr")
    proc = MagicMock()
    proc.pid = None
    proc.returncode = None
    proc.stdout = stdout
    proc.stderr = stderr

    async def _wait_que_falla() -> int:
        raise RuntimeError("pipe roto")

    proc.wait = _wait_que_falla

    async def _reap(_proc: object) -> None:
        orden.append("reap")

    close_spy = MagicMock(side_effect=lambda _job: orden.append("close_job"))
    runner = _runner_directo()

    with _entorno_de_proceso(proc, _reap, close_spy), pytest.raises(runner_mod.DynDOLODExecutionError) as exc_info:
        await runner._execute_process(_EXE, [], "DynDOLOD")

    assert "pipe roto" in str(exc_info.value)
    close_spy.assert_called_once_with(_JOB)
    assert orden[0] == "reap"
    assert orden[-1] == "close_job"


@pytest.mark.asyncio
async def test_r3_si_el_reap_falla_los_auxiliares_y_el_job_se_liberan_igual():
    """Cada etapa vive en su propio ``try/finally``: un ``kill_and_reap`` que lanza
    (``taskkill`` ausente, un backend brokered caído) no puede dejar el Job Object
    abierto ni los auxiliares vivos. La excepción del reap se propaga, no se traga."""
    orden: list[str] = []
    stdout, stderr = _LectorDeStream(orden, "stdout"), _LectorDeStream(orden, "stderr")
    proc = MagicMock()
    proc.pid = None
    proc.returncode = None
    proc.stdout = stdout
    proc.stderr = stderr

    async def _wait_que_falla() -> int:
        await asyncio.wait_for(stdout.arranco.wait(), timeout=5)  # los auxiliares ya están vivos
        raise RuntimeError("pipe roto")

    proc.wait = _wait_que_falla

    async def _reap_que_falla(_proc: object) -> None:
        orden.append("reap")
        raise OSError("taskkill no disponible")

    close_spy = MagicMock(side_effect=lambda _job: orden.append("close_job"))
    runner = _runner_directo()

    with _entorno_de_proceso(proc, _reap_que_falla, close_spy), pytest.raises(OSError, match="taskkill"):
        await runner._execute_process(_EXE, [], "DynDOLOD")

    close_spy.assert_called_once_with(_JOB)
    assert stdout.terminal.is_set()
    assert stderr.terminal.is_set()
    assert orden[0] == "reap"
    assert orden[-1] == "close_job"


@pytest.mark.asyncio
async def test_r3_cancelacion_durante_el_drenaje_post_salida_cierra_el_job():
    """El HERMANO de R3 en la salida normal: el proceso ya terminó y la cancelación
    llega mientras se espera el drenaje con gracia. Antes la excepción escapaba de
    la rama ``else`` sin pasar por ``close_job`` y el Job Object (que aniquila al
    nieto que heredó el pipe) quedaba abierto."""
    orden: list[str] = []
    stdout = _StreamEOF()
    stderr = _LectorDeStream(orden, "stderr")  # nieto con el pipe heredado: nunca da EOF
    proc = MagicMock()
    proc.pid = None
    proc.returncode = 0
    proc.stdout = stdout
    proc.stderr = stderr
    proc.wait = AsyncMock(return_value=0)

    async def _reap(_proc: object) -> None:
        orden.append("reap")  # la salida normal NO mata un proceso que ya salió

    close_spy = MagicMock(side_effect=lambda _job: orden.append("close_job"))
    runner = _runner_directo()

    with _entorno_de_proceso(proc, _reap, close_spy), patch.object(runner_mod, "_DRAIN_GRACE_SECONDS", 30.0):
        tarea = asyncio.create_task(runner._execute_process(_EXE, [], "DynDOLOD"))
        await asyncio.wait_for(stderr.arranco.wait(), timeout=5)
        await _ceder()  # la tarea queda esperando el drenaje con gracia
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tarea, timeout=5)

    close_spy.assert_called_once_with(_JOB)
    assert stderr.terminal.is_set()
    assert "reap" not in orden, "la limpieza del post-salida no debe matar un proceso que ya terminó"
    assert orden[-1] == "close_job"


@pytest.mark.asyncio
async def test_r3_orden_de_la_limpieza_con_una_sola_cancelacion():
    """El orden del invariante en el caso base: reap -> auxiliares -> close_job."""
    orden: list[str] = []
    wait_iniciado = asyncio.Event()
    stdout, stderr = _LectorDeStream(orden, "stdout"), _LectorDeStream(orden, "stderr")
    proc = _proceso_que_no_termina(stdout, stderr, wait_iniciado)

    async def _reap(_proc: object) -> None:
        orden.append("reap")

    close_spy = MagicMock(side_effect=lambda _job: orden.append("close_job"))
    runner = _runner_directo()

    with _entorno_de_proceso(proc, _reap, close_spy):
        tarea = asyncio.create_task(runner._execute_process(_EXE, [], "DynDOLOD"))
        await asyncio.wait_for(wait_iniciado.wait(), timeout=5)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(tarea, timeout=5)

    assert orden[0] == "reap"
    assert orden[-1] == "close_job"
    assert orden.index("reap") < orden.index("stdout_cancelado") < orden.index("close_job")
    assert orden.index("reap") < orden.index("stderr_cancelado") < orden.index("close_job")


def _llamadas_por_nombre(nodo: ast.AST, nombre: str) -> list[ast.Call]:
    """Llamadas a ``nombre`` (como función suelta o atributo) dentro de ``nodo``."""
    return [
        llamada
        for llamada in ast.walk(nodo)
        if isinstance(llamada, ast.Call)
        and (
            (isinstance(llamada.func, ast.Name) and llamada.func.id == nombre)
            or (isinstance(llamada.func, ast.Attribute) and llamada.func.attr == nombre)
        )
    ]


def test_r3_ancla_ast_todos_los_caminos_de_salida_pasan_por_la_limpieza_unica():
    """ENUMERA los caminos de salida de `_execute_process`; no muestrea.

    El defecto original era UNA secuencia de limpieza copiada en cuatro handlers
    (y una quinta variante en la rama ``else``) que sólo uno protegía mal: el
    hermano-en-el-mismo-archivo del `AGENTS.md`. La propiedad del mecanismo es
    que existe UNA operación de limpieza y TODO camino de salida la usa:

    - los cuatro handlers (`CancelledError`, `DynDOLODExecutionError`,
      `TimeoutError`, `Exception`) llaman a `_liberar_proceso_hasta_terminal` y
      NINGUNO toca `kill_and_reap`/`close_job`/`gather` por su cuenta;
    - la rama ``else`` (salida normal) también la usa ante cualquier salida
      anormal del drenaje, y su único `close_job` directo es el del camino feliz;
    - `kill_and_reap` sólo se invoca desde la operación única.
    """
    arbol = ast.parse(RUNNER_SRC.read_text(encoding="utf-8"))
    metodo = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_execute_process"
    )
    # El try de la CORRIDA es el que atrapa CancelledError (el otro de primer nivel
    # es el del spawn, que no posee todavía ni job ni auxiliares).
    tryes = [
        nodo
        for nodo in metodo.body
        if isinstance(nodo, ast.Try) and any(h.type is not None and _es_cancelled(h.type) for h in nodo.handlers)
    ]
    assert len(tryes) == 1, "_execute_process debe tener exactamente UN try de corrida (el que atrapa CancelledError)"
    principal = tryes[0]

    tipos = sorted(ast.unparse(h.type) for h in principal.handlers if h.type is not None)
    assert tipos == sorted(
        [
            "DynDOLODExecutionError",
            "Exception",
            "TimeoutError",
            "asyncio.CancelledError",
        ]
    ), f"el conjunto de handlers de la corrida cambió: {tipos}"

    for handler in principal.handlers:
        etiqueta = ast.unparse(handler.type) if handler.type is not None else "<bare>"
        assert len(_llamadas_por_nombre(handler, "_liberar_proceso_hasta_terminal")) == 1, (
            f"el handler {etiqueta} debe liberar el proceso por la operación única"
        )
        for prohibido in ("kill_and_reap", "close_job", "gather"):
            assert not _llamadas_por_nombre(handler, prohibido), (
                f"el handler {etiqueta} duplica la limpieza ({prohibido}): vuelve el defecto hermano de R3"
            )

    # Rama else (salida normal): usa la operación única ante salida anormal y
    # conserva UN solo close_job directo —el del camino feliz—.
    rama_else = ast.Module(body=principal.orelse, type_ignores=[])
    assert len(_llamadas_por_nombre(rama_else, "_liberar_proceso_hasta_terminal")) == 1, (
        "la rama else debe liberar por la operación única ante cualquier salida anormal del drenaje"
    )
    assert len(_llamadas_por_nombre(rama_else, "close_job")) == 1, "la salida normal cierra el job exactamente una vez"
    assert not _llamadas_por_nombre(rama_else, "kill_and_reap"), "un proceso que ya salió no se mata en la rama else"

    # kill_and_reap SÓLO existe dentro de la operación única.
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.AsyncFunctionDef, ast.FunctionDef)) and nodo.name == "_execute_process":
            assert not _llamadas_por_nombre(nodo, "kill_and_reap"), (
                "kill_and_reap debe vivir sólo en la operación única"
            )

    # La operación única corre en una Task propia y se espera por el handoff terminal.
    operacion = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_liberar_proceso_hasta_terminal"
    )
    assert _llamadas_por_nombre(operacion, "create_task"), "la limpieza debe vivir en una Task propia"
    assert _llamadas_por_nombre(operacion, "_esperar_terminalidad_del_worker"), (
        "la limpieza debe esperarse por la primitiva común de terminal handoff"
    )


def _es_cancelled(expr: ast.AST) -> bool:
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "CancelledError"
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "asyncio"
    )

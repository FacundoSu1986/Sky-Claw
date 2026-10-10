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
  limpieza del proceso es UNA operación resistente a cancelaciones repetidas.
  Corre como Task propia y se espera por el mismo terminal handoff que usa R1
  (`_handoff_terminal`, núcleo común): ninguna cancelación libera al caller
  antes de que el proceso esté reapeado, los helpers TERMINALES y el Job Object
  cerrado. Antes, una segunda cancelación interrumpía el `await kill_and_reap`
  del handler y `close_job(job)` nunca corría — violación de *"EVERY EXIT PATH
  MUST TERMINATE ALL OWNED PROCESS/HELPER RESOURCES"*. Sus tests atraviesan el
  `_execute_process` REAL y congelan el orden causal con checkpoints explícitos.

  Cuatro findings de la revisión pre-merge de #693, todos con test propio:
  **F1** la intención de cancelación absorbida durante el cleanup se descartaba,
  así que las ramas de error/timeout/genérica devolvían un veredicto ordinario
  después de que el caller pidió cancelación (`_cerrar_recursos_del_proceso`
  devuelve ahora `(intención, falla)` y cada rama aplica su precedencia);
  **F2** `_etapa` esperaba el trabajo pelado, así que cancelar la Task de
  limpieza —o la de la etapa— abandonaba el reap y `close_job` corría con el
  proceso sin reapear y el cleanup "exitoso" (ahora cada etapa corre en su propia
  Task por el handoff, y una etapa cancelada se re-conduce);
  **F3** `gather(return_exceptions=True)` convertía la falla de un helper en un
  valor que nadie inspeccionaba (ahora se clasifica: `CancelledError` esperado vs
  falla real, que se registra y se encadena);
  **F4** la misma falla se registraba dos veces —en `_etapa` y otra vez en
  `_cerrar_recursos_del_proceso`— violando *log once* (ahora se registra una sola
  vez, donde se descubre);
  **F5** la espera del heartbeat en la rama de salida normal suprimía la cancelación
  del caller vía `suppress(CancelledError)` (`gather(return_exceptions=True)` absorbe
  la del heartbeat pero propaga la del caller);
  **F6** la cancelación durante el drain-grace en salida normal saltaba directamente
  a `finally: close_job` con los drains vivos y sin resistir cancelación repetida
  (`_limpiar_helpers_en_salida_normal` con `_handoff_terminal` garantiza
  *helpers_terminal < close_job* en todo camino).

Los tests R2 verifican aceptación (rechazo antes de copiar y preservar el destino
previo, tags desconocidos, igualdad del inventario y la semántica TexGen). Los
tests R1 y R3 verifican la aceptación de sus fixes y el handoff terminal
compartido.

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
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

    Los nombres de operación son parámetros porque R1 (packaging) y R3 (limpieza
    del proceso) son DOS call sites del mismo mecanismo: el observador es uno
    solo y cada familia le pasa sus etiquetas. Los defaults preservan el
    contrato de R1 sin cambios.
    """

    def __init__(
        self,
        orden: list[str],
        eventos: asyncio.Queue[str] | None = None,
        *,
        operacion_handoff: str = _OPERACION_HANDOFF,
        operacion_terminal: str = _OPERACION_TERMINAL,
        etiqueta_terminal: str = "worker_terminal",
    ) -> None:
        super().__init__(level=logging.INFO)
        self.orden = orden
        self.eventos = eventos
        self.handoffs = 0
        self._operacion_handoff = operacion_handoff
        self._operacion_terminal = operacion_terminal
        self._etiqueta_terminal = etiqueta_terminal

    def emit(self, record: logging.LogRecord) -> None:
        operacion = getattr(record, "operation_type", None)
        if operacion == self._operacion_handoff:
            self.handoffs += 1
            evento = f"cancel_{self.handoffs}_procesada"
            self.orden.append(evento)
            if self.eventos is not None:
                self.eventos.put_nowait(evento)
        elif operacion == self._operacion_terminal:
            self.orden.append(self._etiqueta_terminal)


@contextlib.contextmanager
def _observar_cancelaciones(orden: list[str], eventos: asyncio.Queue[str] | None = None, **etiquetas):
    """Instala el observador sobre el logger del runner (baja a INFO el gate)."""
    logger_del_runner = logging.getLogger(runner_mod.__name__)
    nivel_previo = logger_del_runner.level
    observador = _ObservadorDeCancelacion(orden, eventos, **etiquetas)
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

    # (4) el mecanismo vive en el núcleo común `_handoff_terminal` — loop sobre
    # task.done() con shield y rama que absorbe CancelledError. R1 y R3 son DOS
    # call sites de la misma primitiva (el ancla enumeradora está en
    # `test_r3_ancla_ast_el_terminal_handoff_es_una_sola_primitiva`); acá se
    # verifica además que el wrapper del packaging NO lo reimplemente.
    helper = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_esperar_terminalidad_del_worker"
    )
    assert any(
        isinstance(nodo, ast.Await)
        and isinstance(nodo.value, ast.Call)
        and isinstance(nodo.value.func, ast.Name)
        and nodo.value.func.id == "_handoff_terminal"
        for nodo in ast.walk(helper)
    ), "el packaging debe esperar por el núcleo común _handoff_terminal, no por una copia del loop"

    nucleo = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_handoff_terminal"
    )
    bucles = [nodo for nodo in ast.walk(nucleo) if isinstance(nodo, ast.While)]
    assert bucles, "el núcleo del handoff debe esperar en un loop hasta terminal"
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
        for nodo in ast.walk(nucleo)
    ), "el loop del handoff debe condicionarse a task.done()"
    assert any(isinstance(nodo, ast.ExceptHandler) and _es_cancelled(nodo.type) for nodo in ast.walk(nucleo)), (
        "el handoff debe absorber CancelledError: esa rama es el handoff de terminalidad"
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


class _EspiaSustitucion:
    """Espía de la frontera que SÍ puede tocar el mod previo (#592 finding 2).

    Reemplaza al viejo espía de ``rmtree_link_aware`` en los tests de R2: desde
    que la sustitución es transaccional, el destino anterior ya no se borra
    directamente — se aparta bajo ``DirectoryRollback``. La pregunta "¿el gate
    corrió antes de mutar el destino?" se mide ahora sobre esa frontera, que es
    la única que muta. Si el espía llegara a instanciarse, el test que lo usa
    estaría probando la razón equivocada.
    """

    def __init__(self) -> None:
        self.instanciados: list[pathlib.Path] = []

    def __call__(self, target: pathlib.Path, **_kwargs: object) -> _EspiaSustitucion:
        self.instanciados.append(pathlib.Path(target))
        return self

    async def __aenter__(self) -> _EspiaSustitucion:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _espiar_sustitucion(monkeypatch: pytest.MonkeyPatch) -> _EspiaSustitucion:
    espia = _EspiaSustitucion()
    monkeypatch.setattr(runner_mod, "DirectoryRollback", espia)
    return espia


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
    """Una fuente con junction no destruye el mod anterior ni inicia ninguna copia.

    El nombre conserva el histórico "antes de rmtree" (lo referencian
    `docs/validation/2026-10-02_p0_uia_alpha209/p0b/runner-defects-plan.md` y la
    tabla de `docs/pending_ooda_status.md`), pero desde #592 finding 2 el mod
    previo no se borra: se aparta bajo `DirectoryRollback`. La aserción se
    enuncia sobre esa frontera —la única que muta el destino—, no sobre la
    llamada que ya no existe.
    """
    src, externo = _crear_arbol_con_junction(tmp_path)
    runner = _runner_stub(tmp_path)
    mod_path = tmp_path / "mods" / "TestMod"
    mod_path.mkdir(parents=True)
    previo = mod_path / "previous.txt"
    previo.write_bytes(b"keep the prior mod")

    copytree_calls = _espiar_copytree(monkeypatch)
    sustitucion = _espiar_sustitucion(monkeypatch)

    with pytest.raises(runner_mod.DynDOLODValidationError, match="junction"):
        await runner._package_output_as_mod(src, "TestMod")

    assert sustitucion.instanciados == [], "el gate R2 debe ejecutarse antes de apartar el mod previo"
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

    sustitucion = _espiar_sustitucion(monkeypatch)
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
        assert sustitucion.instanciados == [], "el worker mutante no debe apartar el mod previo"
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
# R3 — la limpieza del proceso sobrevive a cancelaciones repetidas
# ---------------------------------------------------------------------------
#
# Invariante de aceptación (antes reproducción del defecto, ahora regresión):
#     proc_reaped < helpers_cancelled < helpers_terminal < job_closed
#                 < cancellation_propagated
# Se congela con checkpoints explícitos (Eventos + registros de handoff), nunca
# con sleeps como autoridad causal.

_OPERACION_LIMPIEZA_HANDOFF = "dyndolod_cleanup_cancel_handoff"
_OPERACION_LIMPIEZA_TERMINAL = "dyndolod_cleanup_terminal"

#: Sufijos de `__qualname__` de los helpers que `_execute_process` crea con
#: `asyncio.create_task`. Se enumeran para poder afirmar su TERMINALIDAD en el
#: instante en que corre `close_job` — `Task.cancel()` sólo *solicita* la
#: cancelación, y una Task cancelada está `done` con el trabajo ya terminado.
_HELPERS_DEL_PROCESO = frozenset({"_drain", "_heartbeat_watcher"})


def _helpers_no_terminales() -> list[str]:
    """Helpers del `_execute_process` que siguen VIVOS en este instante.

    `asyncio.all_tasks()` sólo devuelve tareas no terminadas, así que una lista
    vacía es la prueba de terminalidad real (no de cancelación solicitada).
    """
    vivos: list[str] = []
    for tarea in asyncio.all_tasks():
        coro = tarea.get_coro()
        nombre = getattr(coro, "__qualname__", "") or ""
        if nombre.rsplit(".", 1)[-1] in _HELPERS_DEL_PROCESO:
            vivos.append(nombre)
    return vivos


async def _ceder_hasta(condicion: Callable[[], bool], *, vueltas: int = 500) -> bool:
    """Cede scheduling hasta que la condición se cumpla.

    NO es la prueba: la autoridad son los asserts de propiedad (`caller.done()`,
    contador de `close_job`, lista causal). El `sleep(0)` sólo deja correr al
    loop; el tope de vueltas evita un test colgado si la condición nunca llega.
    """
    for _ in range(vueltas):
        if condicion():
            return True
        await asyncio.sleep(0)
    return False


#: `__qualname__` de la Task de limpieza de R3. Se la busca por introspección
#: —enumerar, no muestrear— porque el test necesita su handle REAL para
#: cancelarla directamente, que es el escenario de F2.
_TAREA_LIMPIEZA = "_limpiar_recursos_del_proceso"


async def _ceder_scheduling(vueltas: int = 50) -> None:
    """Cede el control `vueltas` veces, sin afirmar nada sobre el tiempo.

    Se usa donde la propiedad a verificar es un INVARIANTE que debe valer en todo
    momento —"el job no se cierra con el reap pendiente", "el caller no se libera
    antes de terminal"—: darle al camino defectuoso la oportunidad de violarlo es
    lo que hace que el `assert` posterior discrimine. La autoridad sigue siendo
    el `assert`, nunca cuánto se cedió.
    """
    for _ in range(vueltas):
        await asyncio.sleep(0)


def _tareas_del_proceso(sufijo: str) -> list[asyncio.Task]:
    """Tasks VIVAS cuyo coro termina en `sufijo`. Lista vacía = no queda ninguna."""
    encontradas: list[asyncio.Task] = []
    for tarea in asyncio.all_tasks():
        coro = tarea.get_coro()
        nombre = getattr(coro, "__qualname__", "") or ""
        if nombre.rsplit(".", 1)[-1] == sufijo:
            encontradas.append(tarea)
    return encontradas


class _ContadorDeRegistros(logging.Handler):
    """Recolecta los `LogRecord` del logger del runner.

    Se engancha directo al logger del módulo (y no vía `caplog`) para no depender
    de la propagación: lo que se congela acá es cuántos registros EMITE el
    runner, no cuántos llegan a la raíz.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.registros: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.registros.append(record)


@contextlib.contextmanager
def _capturar_registros():
    """Baja el gate a INFO y devuelve los registros emitidos por el runner."""
    logger_del_runner = logging.getLogger(runner_mod.__name__)
    nivel_previo = logger_del_runner.level
    contador = _ContadorDeRegistros()
    logger_del_runner.addHandler(contador)
    logger_del_runner.setLevel(logging.INFO)
    try:
        yield contador
    finally:
        logger_del_runner.removeHandler(contador)
        logger_del_runner.setLevel(nivel_previo)


class _StreamSinEOF:
    """Stream drenable que nunca da EOF y registra cuándo lo cancela el runner."""

    def __init__(self, orden: list[str], nombre: str) -> None:
        self._orden = orden
        self._nombre = nombre

    async def read(self, _n: int) -> bytes:
        self._orden.append(f"drain_{self._nombre}_entered")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self._orden.append(f"drain_{self._nombre}_cancelado")
            raise
        return b""


class _StreamFinito:
    """Stream drenable con EOF: el camino normal del `_drain`."""

    def __init__(self, trozos: list[bytes]) -> None:
        self._trozos = list(trozos)

    async def read(self, _n: int) -> bytes:
        return self._trozos.pop(0) if self._trozos else b""


class _ProcesoCancelable:
    """Contrato mínimo de `DynDOLODProcess` para la rama de cancelación.

    ``pid`` es deliberadamente NO-int: `kill_and_reap` saltea así el `taskkill`
    de Windows (que dispararía sobre un PID inexistente) y se queda con el
    contrato portable —`kill()` + reap acotado—, que es el que R3 necesita
    ejercitar de verdad. El segundo `wait()` es el del reap y se bloquea en un
    checkpoint del test.
    """

    def __init__(self, *, job: int | None, orden: list[str]) -> None:
        self._job = job
        self._orden = orden
        self._matado = False
        self.returncode: int | None = None
        self.pid: object = "proceso-fake"
        self.stdout = _StreamSinEOF(orden, "out")
        self.stderr = _StreamSinEOF(orden, "err")
        self.espera_entrada = asyncio.Event()
        self.reap_entrada = asyncio.Event()
        self.permitir_reap = asyncio.Event()

    def assign_job(self) -> int | None:
        self._orden.append("job_asignado")
        return self._job

    def kill(self) -> None:
        self._matado = True
        self._orden.append("kill")

    async def terminate(self) -> None:
        self._matado = True
        self._orden.append("terminate")

    async def wait(self) -> int:
        if not self._matado:
            self.espera_entrada.set()
            await asyncio.Event().wait()  # sólo la corta la cancelación del caller
        self._orden.append("reap_entrada")
        self.reap_entrada.set()
        await self.permitir_reap.wait()
        self.returncode = -9
        self._orden.append("reap_terminal")
        return -9

    async def captured_output(self) -> tuple[str, str] | None:
        return None


class _StreamRoto:
    """Stream cuyo `read` falla DE VERDAD: el `_drain` termina con esa excepción.

    Es el escenario de F3: la Task del helper queda TERMINAL con una falla real,
    y `gather(return_exceptions=True)` la devuelve como VALOR. `roto` es el
    checkpoint que prueba que la falla ya ocurrió antes de la limpieza.
    """

    def __init__(self, orden: list[str], nombre: str, falla: BaseException) -> None:
        self._orden = orden
        self._nombre = nombre
        self._falla = falla
        self.roto = asyncio.Event()

    async def read(self, _n: int) -> bytes:
        self._orden.append(f"drain_{self._nombre}_roto")
        self.roto.set()
        raise self._falla


class _ProcesoConDrainRoto(_ProcesoCancelable):
    """Proceso cuyo drain de stdout muere con una excepción real."""

    def __init__(self, *, job: int | None, orden: list[str], falla: BaseException) -> None:
        super().__init__(job=job, orden=orden)
        self.stdout = _StreamRoto(orden, "out", falla)
        self.drain_roto = self.stdout.roto


class _ProcesoNormal:
    """Proceso que sale solo: el camino feliz no debe cambiar."""

    def __init__(self, *, salida: bytes, error: bytes, job: int | None) -> None:
        self._job = job
        self.stdout = _StreamFinito([salida])
        self.stderr = _StreamFinito([error])
        self.returncode: int | None = 0
        self.pid: object = "proceso-fake"
        self.kill_llamado = False

    def assign_job(self) -> int | None:
        return self._job

    def kill(self) -> None:
        self.kill_llamado = True

    async def terminate(self) -> None:
        self.kill_llamado = True

    async def wait(self) -> int:
        return 0

    async def captured_output(self) -> tuple[str, str] | None:
        return None


class _EstrategiaFake:
    """`DynDOLODSpawnStrategy` mínima: entrega el proceso ya construido."""

    def __init__(self, proc: object) -> None:
        self._proc = proc

    async def spawn(self, **_kwargs) -> object:
        return self._proc

    def data_visibility_domain(self):  # pragma: no cover - no lo usa _execute_process
        return None


def _runner_para_execute_process(proc: object, *, timeout: float = 30.0) -> runner_mod.DynDOLODRunner:
    """Runner real con SOLO las dependencias externas sustituidas.

    La rama de cancelación que corre es la implementación productiva de
    `_execute_process`, no una copia del handler en el test.
    """
    runner = runner_mod.DynDOLODRunner.__new__(runner_mod.DynDOLODRunner)
    runner._config = SimpleNamespace(
        timeout_seconds=timeout,
        heartbeat_interval=60.0,
        fence_ownership=None,
        output_layout=None,
        external_work_root=None,
    )
    runner._readiness = runner_mod.ReadinessMode.DISABLED_FOR_TEST
    runner._spawn_strategy = _EstrategiaFake(proc)
    runner._exigir_root_born_empty = lambda _tool: None
    return runner


@contextlib.contextmanager
def _observar_cierre_de_job(monkeypatch: pytest.MonkeyPatch, orden: list[str], registro: dict):
    """Sustituye `close_job` y verifica la terminalidad de los helpers AL CERRAR.

    El invariante `helpers_terminal < job_closed` se comprueba en el punto de
    llamada (no por timing): si `close_job` corriera con un drain o el heartbeat
    vivos, el test falla en ese mismo instante.
    """

    def _close_job(job: int | None) -> None:
        registro["vivos_al_cerrar"].append(_helpers_no_terminales())
        registro["close_job"] += 1
        registro["job"] = job
        orden.append("job_closed")

    monkeypatch.setattr(runner_mod, "close_job", _close_job)
    yield registro


def _registro_de_cierre() -> dict:
    return {"close_job": 0, "job": None, "vivos_al_cerrar": []}


@pytest.mark.asyncio
async def test_r3_cancelacion_repetida_no_interrumpe_la_limpieza(monkeypatch):
    """R3 (aceptación) — cancel #2 durante el reap NO libera al caller.

    Secuencia congelada por checkpoints::

        cancel_1 → reap_entrada → cancel_2 → [caller SIGUE pendiente, job abierto]
                 → reap_terminal → helpers terminales → job_closed
                 → caller propaga CancelledError

    Atraviesa el `_execute_process` REAL. Pre-fix, el `assert not caller.done()`
    de más abajo falla: la segunda cancelación interrumpe el `await
    kill_and_reap` del handler y `close_job` nunca corre.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=4242, orden=orden)
    runner = _runner_para_execute_process(proc)

    with (
        _observar_cierre_de_job(monkeypatch, orden, registro),
        _observar_cancelaciones(
            orden,
            operacion_handoff=_OPERACION_LIMPIEZA_HANDOFF,
            operacion_terminal=_OPERACION_LIMPIEZA_TERMINAL,
            etiqueta_terminal="limpieza_terminal",
        ) as observador,
    ):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5), (
                "el runner nunca llegó a esperar al proceso"
            )
            caller.cancel()  # cancel #1 → entra al handler
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5), (
                "el cleanup no llegó al reap: la rama de cancelación no corrió"
            )
            assert not caller.done(), "el caller se liberó durante el reap"
            assert registro["close_job"] == 0, "close_job corrió antes del reap"

            caller.cancel()  # cancel #2, con el reap bloqueado en checkpoint
            await _ceder_hasta(lambda: observador.handoffs >= 1)
            assert not caller.done(), (
                "cancel #2 liberó al caller antes de que la limpieza fuera terminal: "
                f"orden={orden} close_job={registro['close_job']}"
            )
            assert registro["close_job"] == 0, f"close_job corrió con el proceso sin reapear: orden={orden}"

            proc.permitir_reap.set()  # el reap termina recién ahora
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    # Orden causal: el reap termina, DESPUÉS se cierran helpers y job, y sólo
    # entonces el caller propaga.
    hitos = [m for m in orden if m in {"reap_entrada", "reap_terminal", "job_closed", "limpieza_terminal"}]
    assert hitos == ["reap_entrada", "reap_terminal", "limpieza_terminal", "job_closed"], orden
    assert "cancel_1_procesada" in orden, orden
    assert orden.index("reap_terminal") < orden.index("job_closed"), orden
    # Los dos drains se cancelaron ANTES de cerrar el job.
    assert orden.index("drain_out_cancelado") < orden.index("job_closed"), orden
    assert orden.index("drain_err_cancelado") < orden.index("job_closed"), orden
    # Exactamente una vez, con el handle real, y sin helpers vivos al cerrar.
    assert registro["close_job"] == 1, registro
    assert registro["job"] == 4242, registro
    assert registro["vivos_al_cerrar"] == [[]], registro
    # Sin Tasks huérfanas: los helpers quedaron TERMINALES, no sólo cancel-requested.
    assert _helpers_no_terminales() == [], _helpers_no_terminales()


@pytest.mark.asyncio
async def test_r3_cancelaciones_repetidas_n_no_liberan_al_caller(monkeypatch):
    """Cancel #2, #3 y #4 durante la limpieza: ninguno libera al caller ni cierra
    el job antes de tiempo, y el cierre ocurre exactamente una vez.

    Cada cancelación se manda sólo DESPUÉS del checkpoint de que la anterior fue
    PROCESADA por el handoff (contador de registros), nunca por timing.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=99, orden=orden)
    runner = _runner_para_execute_process(proc)

    with (
        _observar_cierre_de_job(monkeypatch, orden, registro),
        _observar_cancelaciones(
            orden,
            operacion_handoff=_OPERACION_LIMPIEZA_HANDOFF,
            operacion_terminal=_OPERACION_LIMPIEZA_TERMINAL,
            etiqueta_terminal="limpieza_terminal",
        ) as observador,
    ):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5)
            caller.cancel()  # cancel #1
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5)
            for numero in (2, 3, 4):  # cancel #N, todos con el reap bloqueado
                caller.cancel()
                await _ceder_hasta(lambda n=numero: observador.handoffs >= n - 1)
                assert not caller.done(), f"cancel #{numero} liberó al caller antes de terminal"
                assert registro["close_job"] == 0, f"cancel #{numero} cerró el job con el proceso sin reapear"
            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    assert observador.handoffs == 3, orden  # #2, #3 y #4 absorbidos por el handoff
    assert registro["close_job"] == 1, registro
    assert registro["job"] == 99, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_cierra_el_job_una_vez_cuando_el_handle_es_none(monkeypatch):
    """`job == None` (no-Windows / brokered): el cierre sigue siendo exactamente uno.

    `close_job(None)` es no-op, pero el contrato de "en TODA salida, una vez" no
    depende del handle: si alguien mueve el cierre a una rama, este test lo ve.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=None, orden=orden)
    runner = _runner_para_execute_process(proc)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5)
            caller.cancel()
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5)
            caller.cancel()  # cancel #2
            await _ceder_hasta(lambda: registro["close_job"] > 0, vueltas=50)
            assert registro["close_job"] == 0
            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    assert registro["close_job"] == 1, registro
    assert registro["job"] is None, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_cancel_durante_la_limpieza_de_otra_rama_tampoco_saltea_el_cierre(monkeypatch):
    """Hermano del defecto: la rama de TIMEOUT comparte la MISMA limpieza.

    Antes, un cancel durante su `await kill_and_reap` saltaba `close_job` igual
    que en la rama de cancelación. La unidad compartida lo cubre en las dos: el
    job se cierra exactamente una vez.

    **F1** — y la cancelación NO se descarta: cuando el caller pidió cancelación
    mientras la limpieza corría, el resultado externo es `CancelledError` (manda
    la semántica del caller) y el veredicto tipado `DynDOLODTimeoutError` viaja
    como `__cause__`. Antes, `_cerrar_recursos_del_proceso` tiraba la intención
    absorbida y salía un `DynDOLODTimeoutError` ordinario: una cancelación
    pedida y perdida.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=7, orden=orden)
    runner = _runner_para_execute_process(proc, timeout=0.05)

    async def _readiness_que_vence(**_kwargs) -> None:
        await asyncio.sleep(10)  # el presupuesto whole-process lo corta

    runner._protocolo_de_readiness = _readiness_que_vence

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5), (
                "la rama de timeout no llegó a la limpieza"
            )
            assert not caller.done()
            caller.cancel()  # cancel durante la limpieza de la rama de timeout
            await _ceder_scheduling()
            assert not caller.done(), f"el cancel saltó la limpieza de la rama de timeout: orden={orden}"
            assert registro["close_job"] == 0
            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    assert isinstance(exc_info.value.__cause__, runner_mod.DynDOLODTimeoutError), (
        f"la cancelación debe ganar como resultado externo CONSERVANDO el veredicto tipado; "
        f"causa observada = {exc_info.value.__cause__!r}"
    )
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_una_falla_real_de_la_limpieza_no_se_traga_y_el_cierre_ocurre(monkeypatch):
    """Una falla REAL del reap no puede quedar silenciada ni abortar el resto.

    Política: la limpieza intenta TODAS sus etapas aunque una falle (abandonar
    a medias es el defecto que R3 cierra), la falla se registra con `exc_info` y
    viaja como `__cause__` del veredicto. El job se cierra igual.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=5, orden=orden)
    runner = _runner_para_execute_process(proc)
    falla_de_limpieza = OSError("el reap explotó")

    async def _kill_and_reap_que_falla(_proc: object, **_kwargs) -> None:
        orden.append("reap_falla")
        raise falla_de_limpieza

    monkeypatch.setattr(runner_mod, "kill_and_reap", _kill_and_reap_que_falla)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await asyncio.wait_for(caller, timeout=5)

    assert exc_info.value.__cause__ is falla_de_limpieza, exc_info.value.__cause__
    # La etapa siguiente corrió igual: los drains se cancelaron pese a la falla.
    assert "reap_falla" in orden, orden
    assert "drain_out_cancelado" in orden, orden
    assert "drain_err_cancelado" in orden, orden
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_camino_normal_sin_cancelacion_no_cambia(monkeypatch):
    """Happy path intacto: mismo resultado, y el job se cierra una sola vez."""
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoNormal(salida=b"hola", error=b"chau", job=1234)
    runner = _runner_para_execute_process(proc)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        salida, error, codigo, duracion = await runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen")

    assert (salida, error, codigo) == ("hola", "chau", 0)
    assert duracion >= 0.0
    assert not proc.kill_llamado, "el camino normal no debe matar el proceso"
    assert registro["close_job"] == 1, registro
    assert registro["job"] == 1234, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_salida_normal_cancelacion_durante_espera_de_heartbeat_no_se_pierde(monkeypatch):
    """R3 (salida normal) — la cancelación del caller durante la espera de heartbeat
    NO se suprime ni produce resultado nominal.

    Vulnerabilidad reproducida: `with contextlib.suppress(asyncio.CancelledError): await heartbeat`
    suprime la cancelación externa del caller. El caller devolvía éxito en vez de propagar `CancelledError`.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()

    heartbeat_iniciado = asyncio.Event()
    permitir_salida_proceso = asyncio.Event()
    heartbeat_en_cancelacion = asyncio.Event()
    permitir_finalizar_heartbeat = asyncio.Event()

    class _ProcesoNormalConHold(_ProcesoNormal):
        async def wait(self) -> int:
            await permitir_salida_proceso.wait()
            return 0

    proc = _ProcesoNormalConHold(salida=b"out", error=b"err", job=42)
    runner = _runner_para_execute_process(proc)
    real_sleep = runner_mod.asyncio.sleep

    async def _mock_sleep(delay: float) -> None:
        heartbeat_iniciado.set()
        try:
            await real_sleep(delay)
        except asyncio.CancelledError:
            heartbeat_en_cancelacion.set()
            await permitir_finalizar_heartbeat.wait()
            raise

    monkeypatch.setattr(runner_mod.asyncio, "sleep", _mock_sleep)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            # 1. Esperar checkpoint causal de que el heartbeat inició su ciclo
            await asyncio.wait_for(heartbeat_iniciado.wait(), timeout=5.0)
            # 2. Permitir que el proceso termine su salida normal
            permitir_salida_proceso.set()
            # 3. Esperar que la limpieza de salida normal cancele el heartbeat
            await asyncio.wait_for(heartbeat_en_cancelacion.wait(), timeout=5.0)
            # 4. Cancelar al caller mientras espera la terminación del heartbeat
            caller.cancel()
            # 5. Liberar al heartbeat para completar su cancelación
            permitir_finalizar_heartbeat.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=5.0)
        finally:
            permitir_salida_proceso.set()
            permitir_finalizar_heartbeat.set()

    assert caller.cancelled()
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_salida_normal_cancelacion_durante_drain_grace_espera_helpers_antes_de_close_job(monkeypatch):
    """R3 (salida normal) — cancelación durante drain-grace lleva los drains a
    terminalidad ANTES de close_job y resiste cancelación repetida.

    Invariante: helpers_terminal < close_job.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()

    drain_en_gracia = asyncio.Event()
    drain_cancel_iniciado = asyncio.Event()
    permitir_terminar_drain = asyncio.Event()
    proceso_termino = asyncio.Event()

    class _StreamConDrainLento:
        async def read(self, _n: int) -> bytes:
            await proceso_termino.wait()
            drain_en_gracia.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                orden.append("drain_cancel_iniciado")
                drain_cancel_iniciado.set()
                await permitir_terminar_drain.wait()
                orden.append("drain_cancel_terminal")
                raise
            return b""

    class _ProcesoSalidaNormalConDrain:
        def __init__(self) -> None:
            self._job = 99
            self.stdout = _StreamConDrainLento()
            self.stderr = _StreamFinito([b""])
            self.returncode = 0
            self.pid = "fake"
            self.kill_llamado = False

        def assign_job(self) -> int | None:
            return self._job

        def kill(self) -> None:
            self.kill_llamado = True

        async def terminate(self) -> None:
            self.kill_llamado = True

        async def wait(self) -> int:
            proceso_termino.set()
            return 0

        async def captured_output(self) -> tuple[str, str] | None:
            return None

    proc = _ProcesoSalidaNormalConDrain()
    runner = _runner_para_execute_process(proc)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        await asyncio.wait_for(drain_en_gracia.wait(), timeout=5.0)

        # Cancel #1 durante drain-grace:
        orden.append("cancel_1")
        caller.cancel()

        # Esperar causalmente a que el drain reciba su cancelación y empiece su cleanup:
        await asyncio.wait_for(drain_cancel_iniciado.wait(), timeout=5.0)

        # Cancel #2 repetido mientras el drain sigue limpiando:
        orden.append("cancel_2")
        caller.cancel()

        # Cancel #2 NO debe liberar al caller ni cerrar el job mientras el drain no sea terminal:
        assert not caller.done(), "cancel #2 no debe liberar al caller antes de que el drain sea terminal"
        assert registro["close_job"] == 0, "close_job no debe correr mientras el drain esté vivo"

        # Permitir que el drain complete su etapa terminal:
        permitir_terminar_drain.set()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=5.0)

    assert caller.cancelled()
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], f"close_job corrió con helpers vivos: {registro['vivos_al_cerrar']}"
    idx_drain_term = orden.index("drain_cancel_terminal")
    idx_job_closed = orden.index("job_closed")
    assert idx_drain_term < idx_job_closed, f"helpers deben ser terminales ANTES de close_job: orden={orden}"


@pytest.mark.asyncio
async def test_r3_la_clasificacion_de_error_no_cambia(monkeypatch):
    """Veredicto tipado intacto: `DynDOLODExecutionError` sigue saliendo tal cual."""
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=3, orden=orden)
    runner = _runner_para_execute_process(proc)
    veredicto = runner_mod.DynDOLODExecutionError("readiness no-MATCH", return_code=None, stderr="x")

    async def _readiness_rechaza(**_kwargs) -> None:
        raise veredicto

    runner._protocolo_de_readiness = _readiness_rechaza

    with (
        _observar_cierre_de_job(monkeypatch, orden, registro),
        pytest.raises(runner_mod.DynDOLODExecutionError) as exc_info,
    ):
        await runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen")

    assert exc_info.value is veredicto, "el veredicto tipado debe re-lanzarse sin envolver"
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_cancel_durante_la_limpieza_de_la_rama_tipada_propaga_cancelacion(monkeypatch):
    """F1 — hermano de la rama de timeout: la rama TIPADA tampoco puede tragarse el cancel.

    Escenario: el protocolo de readiness rechaza con `DynDOLODExecutionError`, la
    limpieza arranca, y REcién ahí el caller pide cancelación. La intención se
    absorbe (la limpieza debe terminar) pero NO se descarta: el resultado externo
    es `CancelledError` y el veredicto tipado queda como `__cause__`.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=17, orden=orden)
    runner = _runner_para_execute_process(proc)
    veredicto = runner_mod.DynDOLODExecutionError("readiness no-MATCH", return_code=None, stderr="x")

    async def _readiness_rechaza(**_kwargs) -> None:
        raise veredicto

    runner._protocolo_de_readiness = _readiness_rechaza

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5), "la rama tipada no llegó a la limpieza"
            caller.cancel()
            await _ceder_scheduling()
            assert not caller.done(), f"el cancel saltó la limpieza de la rama tipada: orden={orden}"
            assert registro["close_job"] == 0
            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    assert exc_info.value.__cause__ is veredicto, (
        f"el veredicto tipado debe conservarse como causa de la cancelación; "
        f"causa observada = {exc_info.value.__cause__!r}"
    )
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_cancel_durante_la_limpieza_de_la_rama_generica_propaga_cancelacion(monkeypatch):
    """F1 — la rama genérica tampoco puede devolver un veredicto tras pedirse cancelación.

    Política de clasificación preservada: el `Exception` original se envuelve en
    `DynDOLODExecutionError`; si además hubo cancelación durante la limpieza, ese
    envoltorio queda como causa del `CancelledError` externo — nunca se devuelve
    normalmente un resultado después de que el caller pidió cancelación.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=19, orden=orden)
    runner = _runner_para_execute_process(proc)

    async def _readiness_explota(**_kwargs) -> None:
        raise RuntimeError("boom inesperado")

    runner._protocolo_de_readiness = _readiness_explota

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5)
            caller.cancel()
            await _ceder_scheduling()
            assert not caller.done(), f"el cancel saltó la limpieza de la rama genérica: orden={orden}"
            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    causa = exc_info.value.__cause__
    assert isinstance(causa, runner_mod.DynDOLODExecutionError), (
        f"la rama genérica debe conservar su veredicto tipado como causa; observado = {causa!r}"
    )
    assert isinstance(causa.__cause__, RuntimeError), (
        f"y ese veredicto debe seguir encadenando el error inesperado original; observado = {causa.__cause__!r}"
    )
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_cancel_y_falla_de_limpieza_conservan_los_tres_hechos(monkeypatch):
    """F1 — los TRES hechos sobreviven en la cadena causal: cancelación, veredicto
    tipado y falla real de la limpieza.

    No alcanza con que gane la cancelación: si además la limpieza falló, esa falla
    no puede desaparecer para simplificar el test. La cadena queda
    `CancelledError → DynDOLODTimeoutError → OSError`, cada eslabón verificable.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=23, orden=orden)
    runner = _runner_para_execute_process(proc, timeout=0.05)
    falla_de_limpieza = OSError("el reap explotó")

    async def _readiness_que_vence(**_kwargs) -> None:
        await asyncio.sleep(10)

    async def _reap_que_falla_tarde(_proc: object, **_kwargs) -> None:
        orden.append("reap_entrada")
        proc.reap_entrada.set()
        await proc.permitir_reap.wait()
        raise falla_de_limpieza

    runner._protocolo_de_readiness = _readiness_que_vence
    monkeypatch.setattr(runner_mod, "kill_and_reap", _reap_que_falla_tarde)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5)
            caller.cancel()
            await _ceder_scheduling()
            assert not caller.done(), orden
            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    veredicto = exc_info.value.__cause__
    assert isinstance(veredicto, runner_mod.DynDOLODTimeoutError), (
        f"eslabón 2: el veredicto tipado debe seguir en la cadena; observado = {veredicto!r}"
    )
    assert veredicto.__cause__ is falla_de_limpieza, (
        f"eslabón 3: la falla real de la limpieza no puede desaparecer; observado = {veredicto.__cause__!r}"
    )
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_cancelar_la_task_de_limpieza_no_abandona_el_reap(monkeypatch):
    """F2 — cancelar DIRECTAMENTE la Task de limpieza no puede abandonar una etapa.

    Con `_etapa` esperando el await pelado, la cancelación de la Task de limpieza
    abortaba el `kill_and_reap`, la limpieza seguía con los helpers, `close_job`
    corría con el proceso SIN reapear y el cleanup parecía exitoso. Ahora cada
    etapa corre en su PROPIA Task y se espera por el terminal handoff: la
    cancelación de la limpieza se absorbe y el reap llega a terminal de verdad.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=11, orden=orden)
    runner = _runner_para_execute_process(proc)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5)
            caller.cancel()
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5)
            assert await _ceder_hasta(lambda: bool(_tareas_del_proceso(_TAREA_LIMPIEZA))), (
                "no se encontró la Task de limpieza viva"
            )

            limpiezas = _tareas_del_proceso(_TAREA_LIMPIEZA)
            assert len(limpiezas) == 1, [t.get_coro().__qualname__ for t in asyncio.all_tasks()]
            limpiezas[0].cancel()  # cancelación DIRECTA de la Task de limpieza

            await _ceder_scheduling()
            assert not caller.done(), f"cancelar la limpieza liberó al caller: orden={orden}"
            assert registro["close_job"] == 0, (
                f"close_job corrió con el proceso SIN reapear: la limpieza se dio por exitosa: orden={orden}"
            )
            assert "reap_terminal" not in orden, f"el reap se dio por terminal sin completarse: {orden}"

            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    assert "reap_terminal" in orden, f"el reap nunca llegó a terminal: {orden}"
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro
    assert _helpers_no_terminales() == []
    assert _tareas_del_proceso(_TAREA_LIMPIEZA) == [], "la Task de limpieza quedó viva"
    # §18: las Tasks de ETAPA también son recursos — enumerarlas, no muestrearlas.
    # `_etapa` las espera hasta terminalidad real, así que ninguna puede quedar viva.
    assert _tareas_del_proceso("kill_and_reap") == [], "quedó viva la Task de la etapa de reap"
    assert _tareas_del_proceso("_esperar_helpers") == [], "quedó viva la Task de la etapa de helpers"


@pytest.mark.asyncio
async def test_r3_cancelar_la_task_de_etapa_no_cuenta_como_terminal(monkeypatch):
    """F2 (hermano) — el `done()` de una Task de etapa CANCELADA no prueba trabajo hecho.

    Un handle terminal por cancelación no es evidencia de que la operación se
    completó. Aceptarlo dejaría el proceso sin reapear con el cleanup "exitoso";
    la etapa se re-conduce y el reap llega a terminal de verdad.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=13, orden=orden)
    runner = _runner_para_execute_process(proc)
    etapas: list[asyncio.Task] = []

    async def _reap_observable(_proc: object, **_kwargs) -> None:
        etapas.append(asyncio.current_task())
        orden.append("reap_entrada")
        proc.reap_entrada.set()
        await proc.permitir_reap.wait()
        proc.returncode = -9
        orden.append("reap_terminal")

    monkeypatch.setattr(runner_mod, "kill_and_reap", _reap_observable)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5)
            caller.cancel()
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5)
            assert await _ceder_hasta(lambda: len(etapas) >= 1)

            etapas[0].cancel()  # cancelación DIRECTA de la Task de la etapa
            assert await _ceder_hasta(lambda: len(etapas) >= 2), (
                f"la etapa cancelada se dio por completada en vez de re-conducirse: orden={orden}"
            )
            assert not caller.done(), f"una etapa cancelada liberó al caller: orden={orden}"
            assert registro["close_job"] == 0, f"close_job corrió sin reap terminal: orden={orden}"
            assert "reap_terminal" not in orden, f"se contó como completada una etapa cancelada: {orden}"

            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    assert len(etapas) == 2, f"se esperaba exactamente UNA re-conducción y hubo {len(etapas)}"
    assert "reap_terminal" in orden, orden
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro
    assert _tareas_del_proceso(_TAREA_LIMPIEZA) == []


@pytest.mark.asyncio
async def test_r3_una_limpieza_cancelada_no_se_declara_exitosa(monkeypatch):
    """F2 (hermano) — el `done()` de una limpieza CANCELADA no es una limpieza exitosa.

    Una Task cancelada queda `done` sin haber completado el trabajo: darla por
    buena es exactamente el caso patológico de R3 (proceso sin reapear, Job
    Object abierto). La unidad lo reporta como falla real —una sola vez— y el
    veredicto la encadena.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=41, orden=orden)
    runner = _runner_para_execute_process(proc)

    async def _limpieza_que_se_cancela(*_args, **_kwargs) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(runner_mod, "_limpiar_recursos_del_proceso", _limpieza_que_se_cancela)

    with (
        _observar_cierre_de_job(monkeypatch, orden, registro),
        _capturar_registros() as capturados,
    ):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await asyncio.wait_for(caller, timeout=5)

    causa = exc_info.value.__cause__
    assert isinstance(causa, RuntimeError), (
        f"una limpieza abandonada por cancelación debe reportarse como falla; observado = {causa!r}"
    )
    assert "sin completar sus etapas" in str(causa), causa
    abandonos = [r for r in capturados.registros if getattr(r, "operation_type", None) == "dyndolod_cleanup_falla"]
    assert len(abandonos) == 1, [r.getMessage() for r in abandonos]
    assert registro["close_job"] == 1, registro


@pytest.mark.asyncio
async def test_r3_una_falla_de_helper_no_queda_como_valor_del_gather(monkeypatch):
    """F3 — `return_exceptions=True` observa terminalidad, pero no puede TRAGAR la falla.

    El `gather` convierte la excepción del helper en un VALOR, así que la etapa
    "helpers" terminaba sin falla y la limpieza se reportaba exitosa. La
    terminalidad de los tres helpers se conserva, y además cada falla REAL
    (nunca el `CancelledError` que la propia limpieza provocó) se OBSERVA,
    se REGISTRA como su propio hecho y se ENCADENA al veredicto: los tres
    eslabones del contrato, no sólo el último.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    falla_del_helper = OSError("drain roto")
    proc = _ProcesoConDrainRoto(job=21, orden=orden, falla=falla_del_helper)
    runner = _runner_para_execute_process(proc)

    with (
        _observar_cierre_de_job(monkeypatch, orden, registro),
        _capturar_registros() as capturados,
    ):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            assert await asyncio.wait_for(proc.drain_roto.wait(), timeout=5), "el drain no falló"
            assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5)
            caller.cancel()
            assert await asyncio.wait_for(proc.reap_entrada.wait(), timeout=5)
            proc.permitir_reap.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await asyncio.wait_for(caller, timeout=5)
        finally:
            proc.permitir_reap.set()

    # REGISTRADA: la falla del helper es un hecho propio, con su exc_info real.
    registros_del_helper = [
        r
        for r in capturados.registros
        if getattr(r, "operation_type", None) == "dyndolod_cleanup_falla"
        and r.exc_info is not None
        and r.exc_info[1] is falla_del_helper
    ]
    assert len(registros_del_helper) == 1, (
        f"la falla del helper debe quedar registrada EXACTAMENTE una vez; hubo {len(registros_del_helper)}: "
        f"{[r.getMessage() for r in registros_del_helper]}"
    )
    # ENCADENADA: no se traga — viaja en la cadena causal del veredicto externo.
    assert exc_info.value.__cause__ is falla_del_helper, (
        f"la falla real del helper debe viajar en la cadena; observado = {exc_info.value.__cause__!r}"
    )
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro
    assert _helpers_no_terminales() == [], "quedaron helpers vivos"


@pytest.mark.asyncio
async def test_r3_una_falla_se_registra_exactamente_una_vez(monkeypatch):
    """F4 — LOG ONCE: una falla de etapa produce UN registro técnico, no dos.

    `_etapa` registraba la falla y `_cerrar_recursos_del_proceso` la volvía a
    registrar: dos registros con el MISMO `operation_type`, `pipeline_stage`,
    `tx_id` y `exc_info` para un solo hecho. La multiplicidad es el contrato: no
    alcanza con que exista al menos uno.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=31, orden=orden)
    runner = _runner_para_execute_process(proc)
    falla_de_limpieza = OSError("el reap explotó")

    async def _reap_que_falla(_proc: object, **_kwargs) -> None:
        raise falla_de_limpieza

    monkeypatch.setattr(runner_mod, "kill_and_reap", _reap_que_falla)

    with (
        _observar_cierre_de_job(monkeypatch, orden, registro),
        _capturar_registros() as capturados,
    ):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        assert await asyncio.wait_for(proc.espera_entrada.wait(), timeout=5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=5)

    del_fallo = [
        r
        for r in capturados.registros
        if getattr(r, "operation_type", None) == "dyndolod_cleanup_falla"
        and r.exc_info is not None
        and r.exc_info[1] is falla_de_limpieza
    ]
    assert len(del_fallo) == 1, (
        f"un hecho de falla debe producir EXACTAMENTE un registro de cleanup_falla y hubo {len(del_fallo)}: "
        f"{[r.getMessage() for r in del_fallo]}"
    )
    # La observabilidad estructurada no se sacrifica para eliminar la repetición:
    # los campos siguen presentes en el registro canónico (fuera de una
    # transacción `tx_id` vale None, pero el campo se emite igual).
    assert del_fallo[0].pipeline_stage, "el registro canónico debe conservar pipeline_stage"
    assert hasattr(del_fallo[0], "tx_id"), "el registro canónico debe conservar tx_id"
    assert registro["close_job"] == 1, registro


def test_r3_ancla_ast_la_rama_cancelled_delega_en_la_limpieza_protegida():
    """Ancla estructural de R3 sobre `_execute_process` (código real).

    Congela tres propiedades que impiden volver a la forma vulnerable sin romper
    un test:
      (1) la rama `except asyncio.CancelledError` NO contiene la secuencia
          multi-paso inline (`await kill_and_reap` / `await asyncio.gather`):
          delega el cleanup a `_cerrar_recursos_del_proceso` ANTES de re-lanzar;
      (2) `close_job` aparece EXACTAMENTE UNA VEZ en el método entero y vive en
          el `finally` — "en toda salida, exactamente una vez";
      (3) ninguna rama de excepción re-inlinea el cleanup ni re-llama `close_job`.
    """
    arbol = ast.parse(RUNNER_SRC.read_text(encoding="utf-8"))
    metodo = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_execute_process"
    )

    ramas_cancel = [
        nodo for nodo in ast.walk(metodo) if isinstance(nodo, ast.ExceptHandler) and _es_cancelled(nodo.type)
    ]
    assert len(ramas_cancel) == 1, "debe haber exactamente una rama `except asyncio.CancelledError`"
    rama = ramas_cancel[0]

    # (1) el cleanup multi-paso ya no vive inline en la rama de cancelación.
    awaits_inline = [
        nodo.value for nodo in ast.walk(rama) if isinstance(nodo, ast.Await) and isinstance(nodo.value, ast.Call)
    ]
    assert not [c for c in awaits_inline if isinstance(c.func, ast.Name) and c.func.id == "kill_and_reap"], (
        "el `await kill_and_reap` inline en la rama de cancelación es la forma vulnerable de R3"
    )
    assert not [c for c in awaits_inline if isinstance(c.func, ast.Attribute) and c.func.attr == "gather"], (
        "el `await asyncio.gather` inline en la rama de cancelación es la forma vulnerable de R3"
    )
    assert [c for c in awaits_inline if isinstance(c.func, ast.Name) and c.func.id == "_cerrar_recursos_del_proceso"], (
        "la rama de cancelación debe delegar el cleanup en `_cerrar_recursos_del_proceso`"
    )

    # (3) ninguna otra rama de excepción re-inlinea el cleanup.
    for otra in (nodo for nodo in ast.walk(metodo) if isinstance(nodo, ast.ExceptHandler) and nodo is not rama):
        assert not [
            nodo.value
            for nodo in ast.walk(otra)
            if isinstance(nodo, ast.Await)
            and isinstance(nodo.value, ast.Call)
            and isinstance(nodo.value.func, ast.Name)
            and nodo.value.func.id == "kill_and_reap"
        ], "las ramas de excepción comparten la MISMA unidad de limpieza: no re-inlinear `kill_and_reap`"

    # (2) close_job: exactamente una vez, en el `finally` del try del proceso.
    llamadas = [
        nodo
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "close_job"
    ]
    assert len(llamadas) == 1, (
        f"`close_job` debe aparecer exactamente una vez en `_execute_process` (hay {len(llamadas)})"
    )
    llamada = llamadas[0]
    en_finally = any(
        any(nodo is llamada for sentencia in try_nodo.finalbody for nodo in ast.walk(sentencia))
        for try_nodo in ast.walk(metodo)
        if isinstance(try_nodo, ast.Try)
    )
    assert en_finally, (
        "`close_job` debe vivir en el `finally` del try del proceso: eso es lo que garantiza "
        "que corra en TODA salida (incluida la cancelación del propio cleanup) exactamente una vez"
    )


def test_r3_ancla_ast_el_terminal_handoff_es_una_sola_primitiva():
    """Enumera, no muestrea: el mecanismo de espera resistente a cancelaciones
    repetidas vive en UNA sola función, y los DOS call sites pasan por ella.

    Una tercera familia que necesite esperar una Task privada tiene que entrar
    por `_handoff_terminal`; si alguien duplica el loop `shield` en otro lado,
    este test lo delata por igualdad del conjunto.
    """
    arbol = ast.parse(RUNNER_SRC.read_text(encoding="utf-8"))
    funciones = [nodo for nodo in ast.walk(arbol) if isinstance(nodo, (ast.AsyncFunctionDef, ast.FunctionDef))]

    def _tiene_loop_de_shield(fn: ast.AST) -> bool:
        return any(
            isinstance(nodo, ast.Await)
            and isinstance(nodo.value, ast.Call)
            and isinstance(nodo.value.func, ast.Attribute)
            and nodo.value.func.attr == "shield"
            for bucle in (n for n in ast.walk(fn) if isinstance(n, ast.While))
            for nodo in ast.walk(bucle)
        )

    con_loop = {fn.name for fn in funciones if _tiene_loop_de_shield(fn)}
    assert con_loop == {"_handoff_terminal"}, (
        f"el loop de espera con `asyncio.shield` debe existir en una sola primitiva; está en {sorted(con_loop)}"
    )

    def _delega_en_handoff(fn: ast.AST) -> bool:
        return any(
            isinstance(nodo, ast.Await)
            and isinstance(nodo.value, ast.Call)
            and isinstance(nodo.value.func, ast.Name)
            and nodo.value.func.id == "_handoff_terminal"
            for nodo in ast.walk(fn)
        )

    for nombre in (
        "_esperar_terminalidad_del_worker",
        "_cerrar_recursos_del_proceso",
        "_limpiar_recursos_del_proceso",
        "_limpiar_helpers_en_salida_normal",
    ):
        fn = next((f for f in funciones if f.name == nombre), None)
        assert fn is not None, f"falta el call site {nombre}"
        assert _delega_en_handoff(fn), f"{nombre} debe esperar por `_handoff_terminal`"

    # `_cerrar_recursos_del_proceso` crea la Task de limpieza y la entrega al
    # handoff: si alguien la `await`ea directo, el shield desaparece.
    cierra = next(f for f in funciones if f.name == "_cerrar_recursos_del_proceso")
    assert any(
        isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "create_task"
        for nodo in ast.walk(cierra)
    ), "la limpieza debe correr en una Task propia para que `done()` sea un handle confiable"

    # F2: cada ETAPA también posee su handle confiable — la Task de limpieza no
    # puede `await`ear el trabajo pelado, porque su propia cancelación lo
    # abortaría a mitad de camino y `close_job` correría sin reap terminal.
    limpia = next(f for f in funciones if f.name == "_limpiar_recursos_del_proceso")
    assert any(
        isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "create_task"
        for nodo in ast.walk(limpia)
    ), "cada etapa debe correr en su propia Task: un `await` pelado es la forma vulnerable de F2"


def _es_cancelled(expr: ast.AST) -> bool:
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "CancelledError"
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "asyncio"
    )


@pytest.mark.asyncio
async def test_r3_integracion_brokered_teardown_deadline_con_rollback_veto(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integración R3 + #695: proceso brokered con teardown deadline vencido veta rollback.

    Verifica la interacción completa entre la unidad de limpieza R3 de DynDOLODRunner
    y los contratos brokered de #695:
    1. Cancelación durante la ejecución de un proceso brokered.
    2. kill_and_reap delega en proc.terminate() -> session.cancel().
    3. El broker acota la espera con el bridge conectado (deadline).
    4. Al vencer el deadline sin worker_exit, se levanta VfsTeardownDeadlineError
       marcado con terminality_unknown.
    5. _limpiar_recursos_del_proceso registra la falla exactamente una vez (log-once)
       y limpia los helpers.
    6. _execute_process propaga CancelledError encadenando VfsTeardownDeadlineError.
    7. close_job se invoca exactamente una vez en finally.
    8. DirectoryRollback evalúa el veto vía exception_forbids_rollback y VETA la
       restauración, preservando el estado mutado y reteniendo el backup en disco.
    """
    from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODProcess
    from sky_claw.local.mo2.vfs_broker import VfsTeardownDeadlineError
    from sky_claw.local.tools._dir_rollback import DirectoryRollback
    from tests.test_vfs_teardown_deadline import _abrir_sesion, _BridgeFalso, _entorno, _kw_broker

    mo2, data, challenge, job = _entorno(tmp_path)
    broker = _kw_broker(tmp_path, deadline=0.2)
    await broker.start()
    bridge = await _BridgeFalso.conectar(broker)
    worker = None
    try:
        sesion, worker = await _abrir_sesion(broker, bridge, job, challenge, mo2, data, pid=8888)
        proc = BrokeredDynDOLODProcess(sesion)

        target = tmp_path / "target_dir"
        target.mkdir()
        (target / "orig.txt").write_text("original", encoding="utf-8")

        runner = runner_mod.DynDOLODRunner.__new__(runner_mod.DynDOLODRunner)
        runner._config = SimpleNamespace(
            timeout_seconds=30.0,
            heartbeat_interval=60.0,
            fence_ownership=None,
            output_layout=None,
            external_work_root=None,
        )
        runner._readiness = runner_mod.ReadinessMode.DISABLED_FOR_TEST

        spawn_listo = asyncio.Event()

        class _Strat:
            async def spawn(self, **kwargs):
                spawn_listo.set()
                return proc

        runner._spawn_strategy = _Strat()
        runner._exigir_root_born_empty = lambda _tool: None

        orden: list[str] = []
        registro = _registro_de_cierre()

        rollback = DirectoryRollback(target, enabled=True)
        caught_exc: BaseException | None = None

        with (
            _observar_cierre_de_job(monkeypatch, orden, registro),
            _capturar_registros() as capturados,
        ):
            try:
                async with rollback:
                    target.mkdir()
                    (target / "mutated.txt").write_text("mutated", encoding="utf-8")

                    task = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))

                    # Checkpoint causal: esperar que el spawn haya ocurrido y que
                    # el heartbeat esté activo (se crea inmediatamente antes del `try`
                    # del proceso, sin ningún `await` intermedio). Esto garantiza que
                    # la cancelación impacte dentro del bloque `try` protegido.
                    await asyncio.wait_for(spawn_listo.wait(), timeout=5.0)

                    async def _hasta_dentro_del_try() -> None:
                        while not _tareas_del_proceso("_heartbeat_watcher"):
                            await asyncio.sleep(0)

                    await asyncio.wait_for(_hasta_dentro_del_try(), timeout=5.0)
                    task.cancel()
                    await task
            except asyncio.CancelledError as exc:
                caught_exc = exc

        assert caught_exc is not None, "la cancelación del caller debe propagarse"
        assert isinstance(caught_exc.__cause__, VfsTeardownDeadlineError), (
            f"la causa debe ser VfsTeardownDeadlineError; observado = {caught_exc.__cause__!r}"
        )
        assert sesion.terminality_unknown, "la sesión brokered debe reportar terminalidad desconocida"

        # Invariante 1: close_job corrió exactamente una vez
        assert registro["close_job"] == 1, registro
        assert registro["job"] is None  # brokered assign_job devuelve None

        # Invariante 2: log-once de la falla de limpieza
        registros_falla = [
            r for r in capturados.registros if getattr(r, "operation_type", None) == "dyndolod_cleanup_falla"
        ]
        assert len(registros_falla) == 1, (
            f"la falla de teardown deadline debe registrarse exactamente UNA vez; hubo {len(registros_falla)}"
        )

        # Invariante 3: rollback vetado por terminalidad desconocida
        assert not rollback.rollback_completed, "el rollback debió ser vetado"
        assert (target / "mutated.txt").exists(), "el target mutado debe conservarse al vetarse el restore"
        assert not (target / "orig.txt").exists(), "el target previo no debe haber sido restaurado"
        assert rollback._backup is not None and (rollback._backup / "orig.txt").exists(), (
            "el backup previo debe quedar retenido en disco para recuperación manual"
        )
    finally:
        if worker is not None:
            await worker.cerrar()
        await bridge.cerrar()
        await broker.close()


@pytest.mark.asyncio
async def test_r3_generica_sin_cancelacion_encadena_falla_de_limpieza_y_veta_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding A: en la rama `except Exception as e:`, si la limpieza falla
    (por ejemplo, teardown con terminalidad desconocida), esa falla NO se descarta
    de la cadena causal cuando el caller no fue cancelado.

    Contrato de seguridad:
    1. El error inesperado original `e` queda como causa directa del veredicto tipado.
    2. La falla de limpieza se encadena al final (`veredicto -> e -> falla_limpieza`).
    3. `exception_forbids_rollback(veredicto)` reconoce la falla y veta el rollback.
    """
    from sky_claw.app.db.rollback_veto import exception_forbids_rollback
    from sky_claw.local.mo2.vfs_broker import VfsTeardownDeadlineError

    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=77, orden=orden)
    runner = _runner_para_execute_process(proc)

    falla_limpieza = VfsTeardownDeadlineError("teardown deadline vencido")

    async def _readiness_explota(**_kwargs) -> None:
        raise RuntimeError("error inesperado en protocolo")

    async def _reap_que_falla(_proc: object, **_kwargs) -> None:
        raise falla_limpieza

    runner._protocolo_de_readiness = _readiness_explota
    monkeypatch.setattr(runner_mod, "kill_and_reap", _reap_que_falla)

    with (
        _observar_cierre_de_job(monkeypatch, orden, registro),
        pytest.raises(runner_mod.DynDOLODExecutionError) as exc_info,
    ):
        await runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen")

    veredicto = exc_info.value
    assert isinstance(veredicto.__cause__, RuntimeError), (
        f"el error original debe ser la causa directa del veredicto; observado = {veredicto.__cause__!r}"
    )
    # Contrato Finding A: falla_limpieza debe estar encadenada y vetar rollback
    assert exception_forbids_rollback(veredicto), (
        "exception_forbids_rollback debe retornar True al estar VfsTeardownDeadlineError encadenada"
    )
    assert veredicto.__cause__.__cause__ is falla_limpieza
    assert registro["close_job"] == 1, registro


@pytest.mark.asyncio
async def test_r3_salida_normal_cancelacion_con_drain_fallido_encadena_falla_y_emite_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding B: en salida normal, si el caller es cancelado durante la espera de helpers
    y un drain falla inesperadamente (e.g. OSError), la excepción del drain NO se pierde
    ni se silencia.

    Contrato:
    1. La cancelación del caller gana como resultado externo (`CancelledError`).
    2. La falla no-cancelación del helper viaja encadenada en `__cause__`.
    3. Se emite el registro técnico `dyndolod_drenaje_fallido`.
    4. close_job se ejecuta en finally.
    """
    orden: list[str] = []
    registro = _registro_de_cierre()

    falla_drain = OSError("pipe roto en drain_out")
    drain_en_gracia = asyncio.Event()
    drain_cancel_iniciado = asyncio.Event()
    permitir_drain_terminar = asyncio.Event()
    proceso_termino = asyncio.Event()

    class _StreamConFalla:
        async def read(self, _n: int) -> bytes:
            await proceso_termino.wait()
            drain_en_gracia.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                drain_cancel_iniciado.set()
                await permitir_drain_terminar.wait()
                raise falla_drain from None
            return b""

    class _ProcesoSalidaNormalConFallaDrain(_ProcesoNormal):
        def __init__(self) -> None:
            super().__init__(salida=b"", error=b"", job=88)
            self.stdout = _StreamConFalla()

        async def wait(self) -> int:
            proceso_termino.set()
            return 0

    proc = _ProcesoSalidaNormalConFallaDrain()
    runner = _runner_para_execute_process(proc)

    with (
        _observar_cierre_de_job(monkeypatch, orden, registro),
        _capturar_registros() as capturados,
    ):
        caller = asyncio.create_task(runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        try:
            await asyncio.wait_for(drain_en_gracia.wait(), timeout=5.0)
            caller.cancel()
            await asyncio.wait_for(drain_cancel_iniciado.wait(), timeout=5.0)
            permitir_drain_terminar.set()
            with pytest.raises(asyncio.CancelledError) as exc_info:
                await asyncio.wait_for(caller, timeout=5.0)
        finally:
            permitir_drain_terminar.set()

    # 1. CancelledError con falla de drain encadenada
    assert exc_info.value.__cause__ is falla_drain, (
        f"la falla del drain debe viajar como causa de la cancelación; observado = {exc_info.value.__cause__!r}"
    )

    # 2. Log dyndolod_drenaje_fallido emitido (no silenciado)
    logs_drenaje = [r for r in capturados.registros if getattr(r, "operation_type", None) == "dyndolod_drenaje_fallido"]
    assert len(logs_drenaje) == 1, f"la falla de drain debe registrarse exactamente UNA vez; hubo {len(logs_drenaje)}"

    # 3. close_job ejecutado exactamente una vez
    assert registro["close_job"] == 1, registro
    assert registro["vivos_al_cerrar"] == [[]], registro


@pytest.mark.asyncio
async def test_r3_falla_en_close_job_preserva_contexto_y_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding D: si `close_job` en el `finally` lanza una excepción, Python
    encadena la excepción previa activa en `__context__`. `exception_forbids_rollback`
    inspecciona `__context__` recursivamente, garantizando que un fallo en el cierre
    del job no borre un veto de rollback previo originado en teardown.
    """
    from sky_claw.app.db.rollback_veto import exception_forbids_rollback
    from sky_claw.local.mo2.vfs_broker import VfsTeardownDeadlineError

    orden: list[str] = []
    registro = _registro_de_cierre()
    proc = _ProcesoCancelable(job=55, orden=orden)
    runner = _runner_para_execute_process(proc)

    falla_limpieza = VfsTeardownDeadlineError("teardown deadline vencido")
    falla_close_job = OSError("falla cerrando job handle")

    async def _readiness_explota(**_kwargs) -> None:
        raise RuntimeError("error inesperado en protocolo")

    async def _reap_que_falla(_proc: object, **_kwargs) -> None:
        raise falla_limpieza

    def _close_job_que_falla(_job: object) -> None:
        registro["close_job"] += 1
        raise falla_close_job

    runner._protocolo_de_readiness = _readiness_explota
    monkeypatch.setattr(runner_mod, "kill_and_reap", _reap_que_falla)
    monkeypatch.setattr(runner_mod, "close_job", _close_job_que_falla)

    with pytest.raises(OSError) as exc_info:
        await runner._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen")

    falla_final = exc_info.value
    assert falla_final is falla_close_job
    # Invariante Finding D: falla_final.__context__ contiene veredicto, y éste contiene falla_limpieza
    assert exception_forbids_rollback(falla_final), (
        "exception_forbids_rollback debe inspeccionar __context__ y vetar rollback ante falla en close_job"
    )
    assert registro["close_job"] == 1, registro


@pytest.mark.asyncio
async def test_r3_cancelacion_preserva_identidad_y_mensaje_con_y_sin_falla_de_limpieza(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caso B: la rama `except asyncio.CancelledError as exc` debe preservar la
    identidad y mensaje del `CancelledError` original del caller en ambos caminos:
    1. Cleanup exitoso: re-lanza el `exc` original intacto (con su mensaje/args).
    2. Cleanup fallido: re-lanza el `exc` original (con su mensaje/args) encadenando
       la falla de limpieza como `__cause__`, permitiendo el veto de rollback.
    """
    from sky_claw.app.db.rollback_veto import exception_forbids_rollback
    from sky_claw.local.mo2.vfs_broker import VfsTeardownDeadlineError

    orden: list[str] = []
    registro = _registro_de_cierre()

    # --- Camino 1: Cleanup fallido ---
    proc1 = _ProcesoCancelable(job=101, orden=orden)
    runner1 = _runner_para_execute_process(proc1)
    falla_limpieza = VfsTeardownDeadlineError("teardown deadline vencido")

    async def _reap_que_falla(_proc: object, **_kwargs) -> None:
        raise falla_limpieza

    monkeypatch.setattr(runner_mod, "kill_and_reap", _reap_que_falla)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        task1 = asyncio.create_task(runner1._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        assert await asyncio.wait_for(proc1.espera_entrada.wait(), timeout=5.0)
        task1.cancel("motivo de cancelacion 1")
        with pytest.raises(asyncio.CancelledError) as exc_info1:
            await asyncio.wait_for(task1, timeout=5.0)

    exc1 = exc_info1.value
    # Preservación de mensaje / args originales
    assert exc1.args == ("motivo de cancelacion 1",), f"se perdió el mensaje del cancel: args={exc1.args}"
    assert exc1.__cause__ is falla_limpieza
    assert exception_forbids_rollback(exc1)

    # --- Camino 2: Cleanup exitoso ---
    orden.clear()

    async def _reap_exitoso(_proc: object, **_kwargs) -> None:
        pass

    monkeypatch.setattr(runner_mod, "kill_and_reap", _reap_exitoso)
    proc2 = _ProcesoCancelable(job=102, orden=orden)
    runner2 = _runner_para_execute_process(proc2)

    with _observar_cierre_de_job(monkeypatch, orden, registro):
        task2 = asyncio.create_task(runner2._execute_process(pathlib.Path("DynDOLODx64.exe"), [], "TexGen"))
        assert await asyncio.wait_for(proc2.espera_entrada.wait(), timeout=5.0)
        task2.cancel("motivo de cancelacion 2")
        with pytest.raises(asyncio.CancelledError) as exc_info2:
            await asyncio.wait_for(task2, timeout=5.0)

    exc2 = exc_info2.value
    assert exc2.args == ("motivo de cancelacion 2",), f"se perdió el mensaje del cancel: args={exc2.args}"
    assert exc2.__cause__ is None


# ---------------------------------------------------------------------------
# #592 finding 2 — la sustitución del mod anterior es transaccional
# ---------------------------------------------------------------------------
#
# **Defecto (PRE-FIX, reproducido).** `_empaquetar_sincrono` ejecutaba, en este
# orden:
#
#     rmtree_link_aware(mod_path)  →  _bytes_del_arbol / _espacio_libre_en  →  copytree
#
# así que el mod anterior desaparecía ANTES de saber si el nuevo podía
# empaquetarse. Con `create_snapshot=True` lo cubría el `DirectoryRollback` del
# servicio; con `create_snapshot=False` —renuncia válida del operador— no había
# red: ENOSPC, una copia parcial o un `meta.ini` fallido dejaban al operador sin
# la última generación de LODs, varios GB y horas de cómputo.
#
# **Invariante congelado (BEFORE SUCCESSFUL PROMOTION):**
#
#     previous_mod intacto  O  recuperable bajo un protocolo de backup con dueño
#
# Tres propiedades lo sostienen, y cada test de abajo falla si se rompe UNA:
#
#   1. el presupuesto ENOSPC es de COEXISTENCIA — no se borra el mod previo
#      para liberar espacio, así que `necesarios > libres` rechaza sin tocar el
#      destino (el caso crítico de §8: 500 MiB libres, 2 GiB previos, 3 GiB
#      nuevos no se arregla borrando el previo);
#   2. la sustitución corre bajo `DirectoryRollback` — move-aside O(1) con
#      nombre exclusivo, restore ante CUALQUIER fallo, descarte sólo tras el
#      éxito completo (copia + `meta.ini`);
#   3. el nombre del residuo es el que `rollback_reconciler` ya reconoce, así
#      que una muerte dura entre el move-aside y la copia es recuperable sin
#      inventar un segundo sistema de recovery.
#
# Los tests NO llenan el disco, NO usan mods reales del usuario y NO ejecutan
# TexGen/DynDOLOD: el espacio se inyecta por mock determinista.

_PREVIO_ARCHIVOS: dict[str, bytes] = {
    "meshes/previous.nif": b"P" * 2048,
    "textures/previous.dds": b"T" * 1024,
}
_PREVIO_META_INI = b"[General]\nname=Previous\n"


def _runner_592(tmp_path: pathlib.Path, *, meta_ini_falla: bool = False):
    """Runner stub con `meta.ini` real (o fallable) y veto de rollback inyectable."""
    r = runner_mod.DynDOLODRunner.__new__(runner_mod.DynDOLODRunner)
    r._config = SimpleNamespace(mo2_mods_path=tmp_path / "mods", fence_ownership=None)
    r._es_la_raiz_administrada = lambda _p: False
    r._exigir_fuente_del_subroot = AsyncMock(return_value=None)

    def _meta(mod_path: pathlib.Path, mod_name: str) -> None:
        if meta_ini_falla:
            raise OSError("no se pudo escribir meta.ini")
        (mod_path / "meta.ini").write_text(f"[General]\nname={mod_name}\n", encoding="utf-8")

    r._generate_meta_ini = _meta
    return r


def _poblar_mod_previo(mod_path: pathlib.Path) -> dict[str, bytes]:
    """Mod anterior poblado, con contenido byte-exacto conocido."""
    for rel, contenido in _PREVIO_ARCHIVOS.items():
        destino = mod_path / rel
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(contenido)
    (mod_path / "meta.ini").write_bytes(_PREVIO_META_INI)
    return {**_PREVIO_ARCHIVOS, "meta.ini": _PREVIO_META_INI}


def _staging_nuevo(base: pathlib.Path, nombre: str = "staging") -> pathlib.Path:
    """Staging nuevo independiente del mod anterior."""
    src = base / nombre
    (src / "meshes").mkdir(parents=True)
    (src / "textures").mkdir(parents=True)
    (src / "meshes" / "new.nif").write_bytes(b"N" * 4096)
    (src / "textures" / "new.dds").write_bytes(b"D" * 512)
    return src


def _leer_arbol(raiz: pathlib.Path) -> dict[str, bytes]:
    """Contenido byte-exacto del árbol (vacío si la raíz no existe)."""
    if not raiz.exists():
        return {}
    return {p.relative_to(raiz).as_posix(): p.read_bytes() for p in sorted(raiz.rglob("*")) if p.is_file()}


def _residuos_de(mod_path: pathlib.Path) -> list[pathlib.Path]:
    """Backups move-aside del destino — el residuo que el protocolo puede dejar."""
    if not mod_path.parent.is_dir():
        return []
    return sorted(p for p in mod_path.parent.iterdir() if p.name.startswith(f"{mod_path.name}.rollback-"))


def _mock_espacio_libre(monkeypatch: pytest.MonkeyPatch, libres: int) -> None:
    monkeypatch.setattr(
        runner_mod.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=libres, used=0, free=libres),
    )


@pytest.mark.asyncio
async def test_592_p1_enospc_conserva_el_mod_anterior_byte_exacto(tmp_path, monkeypatch):
    """P1/P17 — ENOSPC sin snapshot: el mod previo y el source raw sobreviven.

    Es la reproducción directa del finding 2: el presupuesto se agota y el
    destino anterior NO puede haberse tocado, porque la medición corre antes de
    cualquier mutación del mod.
    """
    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)
    source_antes = _leer_arbol(src)
    runner = _runner_592(tmp_path)
    _mock_espacio_libre(monkeypatch, 1)

    with pytest.raises(runner_mod.DynDOLODValidationError, match="espacio"):
        await runner._package_output_as_mod(src, "DynDOLOD Output")

    assert _leer_arbol(mod_path) == previo, "el mod anterior se perdió (o cambió) al fallar por ENOSPC"
    assert _leer_arbol(src) == source_antes, "el staging raw debe quedar byte-exacto"
    assert _residuos_de(mod_path) == [], "un rechazo por capacidad no debe dejar residuo de move-aside"


@pytest.mark.asyncio
async def test_592_p14_el_presupuesto_es_de_coexistencia_y_no_libera_borrando_el_previo(tmp_path, monkeypatch):
    """P14 — espacio insuficiente para COEXISTIR: fallo seguro, previo intacto.

    Caso discriminante de §8. Con 2,5 GiB libres y un previo de 2 GiB, una
    solución que borra el previo para recuperar espacio vería 4,5 GiB y copiaría
    3 GiB "con éxito" destruyendo la última generación. La política correcta
    exige que ambos árboles convivan: 3 GiB > 2,5 GiB ⇒ rechazo sin mutación.
    """
    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)
    runner = _runner_592(tmp_path)
    monkeypatch.setattr(runner_mod, "_bytes_del_arbol", lambda _p: 3 * 1024**3)
    _mock_espacio_libre(monkeypatch, int(2.5 * 1024**3))

    with pytest.raises(runner_mod.DynDOLODValidationError, match="espacio"):
        await runner._package_output_as_mod(src, "DynDOLOD Output")

    assert _leer_arbol(mod_path) == previo
    assert _residuos_de(mod_path) == []


@pytest.mark.asyncio
async def test_592_p2_copia_parcial_conserva_el_mod_anterior(tmp_path, monkeypatch):
    """P2/P17 — fallo a mitad de `copytree`: el previo se restaura byte-exacto.

    Demuestra por qué una comprobación de espacio al principio no alcanza: el
    fallo ocurre DESPUÉS de empezar a escribir el mod nuevo.
    """
    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)
    source_antes = _leer_arbol(src)
    runner = _runner_592(tmp_path)

    def _copytree_parcial(_src: object, dst: object, **_kw: object) -> None:
        destino = pathlib.Path(str(dst))
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "parcial.bin").write_bytes(b"PARC")
        raise OSError("fallo de copia a mitad")

    monkeypatch.setattr(runner_mod.shutil, "copytree", _copytree_parcial)

    with pytest.raises(runner_mod.DynDOLODValidationError):
        await runner._package_output_as_mod(src, "DynDOLOD Output")

    assert _leer_arbol(mod_path) == previo, "el mod anterior no volvió byte-exacto tras la copia parcial"
    assert _residuos_de(mod_path) == [], "el backup debía descartarse al restaurar"
    assert _leer_arbol(src) == source_antes, "el staging raw debe quedar byte-exacto"


@pytest.mark.asyncio
async def test_592_p3_fallo_de_meta_ini_no_publica_un_mod_incompleto(tmp_path, monkeypatch):
    """P3 — `meta.ini` falla: no se publica un mod nuevo sin metadata."""
    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)
    runner = _runner_592(tmp_path, meta_ini_falla=True)

    with pytest.raises(runner_mod.DynDOLODValidationError):
        await runner._package_output_as_mod(src, "DynDOLOD Output")

    assert _leer_arbol(mod_path) == previo, "un `meta.ini` fallido no puede dejar expuesto el mod nuevo"
    assert not (mod_path / "meshes" / "new.nif").exists()
    assert _residuos_de(mod_path) == []


@pytest.mark.asyncio
async def test_592_p4_primera_instalacion_sin_mod_previo(tmp_path, monkeypatch):
    """P4 — sin mod anterior el camino sigue siendo válido, y su fallo no deja parcial.

    La corrección no puede asumir que siempre hay algo que preservar: se afirma
    el éxito limpio Y la ausencia de un "previo preservado" que nunca existió.
    """
    mod_path = tmp_path / "mods" / "DynDOLOD Output"

    src_ok = _staging_nuevo(tmp_path, "staging-ok")
    runner_ok = _runner_592(tmp_path)
    empaquetado = await runner_ok._package_output_as_mod(src_ok, "DynDOLOD Output")
    assert empaquetado == mod_path
    assert (mod_path / "meshes" / "new.nif").read_bytes() == b"N" * 4096
    assert (mod_path / "meta.ini").read_text(encoding="utf-8").startswith("[General]")
    assert _residuos_de(mod_path) == []

    # Segundo escenario: primera instalación que FALLA. No hay previo que
    # preservar, pero tampoco puede quedar un mod a medias.
    mod_path_2 = tmp_path / "mods2" / "DynDOLOD Output"
    src_falla = _staging_nuevo(tmp_path, "staging-falla")
    runner_falla = _runner_592(tmp_path)
    runner_falla._config = SimpleNamespace(mo2_mods_path=tmp_path / "mods2", fence_ownership=None)
    monkeypatch.setattr(
        runner_mod.shutil,
        "copytree",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("fallo sin previo")),
    )

    with pytest.raises(runner_mod.DynDOLODValidationError):
        await runner_falla._package_output_as_mod(src_falla, "DynDOLOD Output")

    assert not mod_path_2.exists(), "sin mod previo, un fallo no debe dejar un parcial publicado"
    assert _residuos_de(mod_path_2) == []


@pytest.mark.asyncio
async def test_592_p11_rename_del_move_aside_fallido_no_destruye_el_previo(tmp_path, monkeypatch):
    """P11 — si el move-aside no se puede hacer, fail-closed y previo intacto."""
    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)
    runner = _runner_592(tmp_path)

    rename_original = pathlib.Path.rename

    def _rename_que_falla(self: pathlib.Path, destino: object) -> None:
        if self == mod_path:
            raise OSError("rename bloqueado por otro handle")
        return rename_original(self, destino)

    monkeypatch.setattr(pathlib.Path, "rename", _rename_que_falla)

    with pytest.raises(runner_mod.DynDOLODValidationError):
        await runner._package_output_as_mod(src, "DynDOLOD Output")

    assert _leer_arbol(mod_path) == previo, "el mod anterior debe quedar donde estaba si no se pudo apartar"
    assert _residuos_de(mod_path) == []


@pytest.mark.asyncio
async def test_592_p10_veto_de_lease_no_restaura_sobre_un_nuevo_dueno(tmp_path, monkeypatch):
    """P10 — sin exclusividad no se restaura: el backup queda, no se pisa al nuevo dueño."""
    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)
    runner = _runner_592(tmp_path)

    def _copytree_parcial(_src: object, dst: object, **_kw: object) -> None:
        destino = pathlib.Path(str(dst))
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "de_otro_dueno.bin").write_bytes(b"AJENO")
        raise OSError("fallo con lease perdida")

    monkeypatch.setattr(runner_mod.shutil, "copytree", _copytree_parcial)

    with pytest.raises(runner_mod.DynDOLODValidationError):
        await runner._package_output_as_mod(
            src,
            "DynDOLOD Output",
            veto_de_rollback=lambda: False,
        )

    residuos = _residuos_de(mod_path)
    assert len(residuos) == 1, "el backup debe quedar en disco para recuperación manual"
    assert _leer_arbol(residuos[0]) == previo, "el backup es la última copia buena y debe estar íntegro"
    assert _leer_arbol(mod_path) == {"meshes/de_otro_dueno.bin": b"AJENO"}, (
        "con la lease perdida no se toca el target: restaurar encima pisaría al dueño concurrente"
    )


@pytest.mark.asyncio
async def test_592_p12_residuos_de_otros_destinos_no_se_borran(tmp_path, monkeypatch):
    """P12 — un residuo con forma de backup que no es NUESTRO no se toca.

    El descarte sólo puede alcanzar el backup que este protocolo creó: un
    `rmtree` por patrón sobre el padre borraría datos ajenos.
    """
    src = _staging_nuevo(tmp_path)
    mods = tmp_path / "mods"
    mod_path = mods / "DynDOLOD Output"
    _poblar_mod_previo(mod_path)

    ajeno = mods / "Otro Mod.rollback-123456789012345"
    ajeno.mkdir(parents=True)
    (ajeno / "ajeno.bin").write_bytes(b"NO TOCAR")
    real_ajeno = mods / "Otro Mod"
    real_ajeno.mkdir()
    (real_ajeno / "real.bin").write_bytes(b"REAL")

    runner = _runner_592(tmp_path)
    await runner._package_output_as_mod(src, "DynDOLOD Output")

    assert (ajeno / "ajeno.bin").read_bytes() == b"NO TOCAR"
    assert (real_ajeno / "real.bin").read_bytes() == b"REAL"
    assert (mod_path / "meshes" / "new.nif").read_bytes() == b"N" * 4096
    assert _residuos_de(mod_path) == []


@pytest.mark.asyncio
async def test_592_p5_texgen_sustituye_el_mod_previo_conservando_textures(tmp_path, monkeypatch):
    """P5 — TexGen: la sustitución no aplana el layout `textures/` Data-relative."""
    src = tmp_path / "staging" / "textures"
    (src / "terrain").mkdir(parents=True)
    (src / "terrain" / "new.dds").write_bytes(b"NUEVO")

    mod_path = tmp_path / "mods" / "TexGen Output"
    (mod_path / "textures" / "terrain").mkdir(parents=True)
    (mod_path / "textures" / "terrain" / "old.dds").write_bytes(b"VIEJO")
    (mod_path / "meta.ini").write_bytes(_PREVIO_META_INI)

    runner = _runner_592(tmp_path)
    await runner._package_output_as_mod(src, "TexGen Output", preservar_directorio_raiz=True)

    assert (mod_path / "textures" / "terrain" / "new.dds").read_bytes() == b"NUEVO"
    assert not (mod_path / "terrain").exists(), "TexGen debe conservar textures/ como prefijo del mod"
    assert set(_leer_arbol(mod_path)) == {"textures/terrain/new.dds", "meta.ini"}, (
        "la versión anterior debe haber sido reemplazada por completo, no fusionada"
    )
    assert _residuos_de(mod_path) == []


@pytest.mark.asyncio
async def test_592_p16_falla_el_descarte_del_backup_sin_perdida_ni_falso_exito(tmp_path, monkeypatch):
    """P16 — si el cleanup del backup falla: el mod nuevo está completo y el previo sigue.

    El descarte del backup es la ÚNICA operación que borra la generación
    anterior, y corre en el camino de ÉXITO. Si falla, no puede haber pérdida
    silenciosa: ambos árboles quedan en disco y el resultado es el mod nuevo
    (que sí está completo).
    """
    import sky_claw.local.tools._dir_rollback as dir_rollback_mod

    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)
    runner = _runner_592(tmp_path)

    rmtree_original = dir_rollback_mod.rmtree_link_aware

    def _rmtree_que_falla(ruta: pathlib.Path, **kwargs: object) -> int:
        if ".rollback-" in pathlib.Path(ruta).name:
            raise OSError("handle retenido por el indexador")
        return rmtree_original(ruta, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dir_rollback_mod, "rmtree_link_aware", _rmtree_que_falla)

    empaquetado = await runner._package_output_as_mod(src, "DynDOLOD Output")

    assert empaquetado == mod_path
    assert (mod_path / "meshes" / "new.nif").read_bytes() == b"N" * 4096
    assert (mod_path / "meta.ini").exists()
    residuos = _residuos_de(mod_path)
    assert len(residuos) == 1, "un backup que no se pudo descartar debe quedar localizable"
    assert _leer_arbol(residuos[0]) == previo, "no puede haber pérdida: el backup sigue íntegro"


@pytest.mark.asyncio
async def test_592_readonly_el_descarte_del_backup_no_deja_residuo(tmp_path, monkeypatch):
    """§9 — un mod anterior con archivos READONLY no debe bloquear la sustitución.

    La salida de una corrida previa de DynDOLOD puede quedar con
    `FILE_ATTRIBUTE_READONLY` (herramienta externa en Windows). El descarte del
    backup tiene que tolerarlo, o el residuo queda huérfano en el camino de
    éxito — que es justo donde nadie lo mira.
    """
    import stat as stat_mod

    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    _poblar_mod_previo(mod_path)
    readonly = mod_path / "meshes" / "previous.nif"
    readonly.chmod(stat_mod.S_IREAD)

    runner = _runner_592(tmp_path)
    try:
        await runner._package_output_as_mod(src, "DynDOLOD Output")
        assert (mod_path / "meshes" / "new.nif").read_bytes() == b"N" * 4096
        assert _residuos_de(mod_path) == [], "el backup readonly quedó huérfano tras un empaquetado exitoso"
    finally:
        # El archivo readonly debe haber sido descartado junto con el backup; si
        # el fix no funcionó, sigue acá y hay que devolverle el bit de escritura
        # para que pytest pueda limpiar el tmp_path.
        if readonly.exists():
            readonly.chmod(stat_mod.S_IWRITE)


@pytest.mark.asyncio
async def test_592_p9_doble_cancelacion_durante_la_copia_restaura_el_previo(tmp_path, monkeypatch):
    """P9 — cancel #1/#2 durante la copia: el caller no se libera y el previo vuelve.

    Conserva el orden de R1 (`worker_terminal < rollback_started <
    lease_released`) con la sustitución activa, y agrega la propiedad nueva: al
    final del unwind el mod anterior está de vuelta byte-exacto.
    """
    src = _staging_nuevo(tmp_path)
    mod_path = tmp_path / "mods" / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)

    runner = _runner_592(tmp_path)
    monkeypatch.setattr(runner_mod, "link_kind_or_raise_with_retry", lambda _p: None)

    copia_arranco = threading.Event()
    permitir_copia = threading.Event()
    orden: list[str] = []
    eventos: asyncio.Queue[str] = asyncio.Queue()

    def _copytree_bloqueado(*args: object, **kwargs: object):
        copia_arranco.set()
        assert permitir_copia.wait(timeout=30), "el test no liberó la copia"
        orden.append("copia_terminada")
        return shutil.copytree(*args, **kwargs)

    monkeypatch.setattr(runner_mod.shutil, "copytree", _copytree_bloqueado)

    rollback_started = threading.Event()
    lease_released = threading.Event()

    async def _caller() -> None:
        try:
            await runner._package_output_as_mod(src, "DynDOLOD Output")
        except asyncio.CancelledError:
            orden.append("cancel_propagado")
            raise
        finally:
            orden.append("rollback_started")
            rollback_started.set()
            orden.append("lease_released")
            lease_released.set()

    tarea = asyncio.create_task(_caller())
    try:
        with _observar_cancelaciones(orden, eventos):
            assert await asyncio.to_thread(copia_arranco.wait, 10), "la copia no arrancó"
            tarea.cancel()
            assert await asyncio.wait_for(eventos.get(), timeout=5) == "cancel_1_procesada"
            assert not tarea.done(), "cancel #1 liberó al caller antes de worker_terminal"
            assert not rollback_started.is_set()
            assert not lease_released.is_set()

            tarea.cancel()
            assert await asyncio.wait_for(eventos.get(), timeout=5) == "cancel_2_procesada"
            assert not tarea.done(), "cancel #2 liberó al caller antes de worker_terminal"
            assert not rollback_started.is_set()

            permitir_copia.set()
            with pytest.raises(asyncio.CancelledError):
                await tarea
    finally:
        permitir_copia.set()
        if not tarea.done():
            tarea.cancel()
        with contextlib.suppress(BaseException):
            await tarea

    assert orden.index("worker_terminal") < orden.index("cancel_propagado") < orden.index("rollback_started")
    assert orden.index("rollback_started") < orden.index("lease_released")
    assert _leer_arbol(mod_path) == previo, "tras el doble cancel el mod anterior debe volver byte-exacto"
    assert _residuos_de(mod_path) == []


@pytest.mark.asyncio
async def test_592_p15_muerte_dura_entre_el_move_aside_y_la_copia_es_recuperable(tmp_path):
    """P15 — el residuo del move-aside es el que `rollback_reconciler` ya reconoce.

    Una muerte dura (SIGKILL/corte) después del move-aside y antes de que el
    nuevo mod exista deja `<mod>.rollback-<nonce>` sin destino. El protocolo NO
    inventa un recovery paralelo: reutiliza el nombre que el reconciliador de
    arranque ya sabe identificar y restaurar.
    """
    import sky_claw.local.tools.rollback_reconciler as recon_mod

    mods = tmp_path / "mods"
    mod_path = mods / "DynDOLOD Output"
    previo = _poblar_mod_previo(mod_path)

    # La muerte dura: move-aside hecho, restore nunca ejecutado. El stack NO se
    # sale a propósito — salir ejecutaría el `__aexit__` que la muerte dura evita.
    from sky_claw.local.tools._dir_rollback import DirectoryRollback

    pila = contextlib.AsyncExitStack()
    await pila.enter_async_context(DirectoryRollback(mod_path))
    assert not mod_path.exists(), "el move-aside debe haber apartado el mod"

    residuos = _residuos_de(mod_path)
    assert len(residuos) == 1
    assert recon_mod._listar_backups_move_aside([mod_path]) == residuos, (
        "el nombre del residuo debe ser el que el reconciliador ya enumera"
    )

    acc = recon_mod._Acumulador()
    await recon_mod._reconciliar_move_aside([mod_path], acc)
    assert acc.restaurados == [mod_path]
    assert acc.preservados == []
    assert _leer_arbol(mod_path) == previo, "el reconciliador debe devolver el último estado bueno"
    assert _residuos_de(mod_path) == []


def test_592_ancla_ast_el_previo_solo_se_aparta_bajo_el_protocolo_y_tras_el_presupuesto():
    """Ancla de forma: enumera las tres propiedades del mecanismo en el código real.

    Congela, sobre `_package_output_as_mod`:
    (1) NO existe ningún borrado directo del mod (`rmtree_link_aware`) — el
        único camino que puede tocar el destino anterior es el protocolo de
        rollback, que lo aparta en O(1) y lo devuelve ante cualquier fallo;
    (2) la sustitución corre dentro de un `async with DirectoryRollback(...)`;
    (3) el presupuesto ENOSPC se mide —y se invoca— ANTES de ese `async with` y
        FUERA del worker mutante, así que un rechazo por capacidad no muta el
        destino. Se ancla por EJECUCIÓN y no por posición en el archivo: ver el
        comentario de (3a).
    """
    arbol = ast.parse(RUNNER_SRC.read_text(encoding="utf-8"))
    metodo = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "_package_output_as_mod"
    )

    borrados_directos = [
        nodo
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "rmtree_link_aware"
    ]
    assert borrados_directos == [], (
        "el packaging no puede borrar el mod anterior directamente: la única vía es el move-aside de DirectoryRollback"
    )

    async_withs = [
        nodo
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "DirectoryRollback"
            for item in nodo.items
        )
    ]
    assert len(async_withs) == 1, "la sustitución del mod anterior debe correr bajo UN DirectoryRollback"
    sustitucion = async_withs[0]
    construccion = sustitucion.items[0].context_expr
    assert isinstance(construccion, ast.Call)

    # (2b) El descarte del backup borra la generación anterior —el árbol que dejó
    # una herramienta EXTERNA de Windows, con FILE_ATTRIBUTE_READONLY posible—, y
    # corre en el camino de ÉXITO. Sin el flag, ese OSError lo traga `__aexit__`
    # por diseño y el residuo queda huérfano donde nadie lo mira. Este ancla es
    # donde vive el requisito desde que #592 finding 2 sacó a `dyndolod_runner.py`
    # de `POLITICA_DE_LIMPIAR_READONLY` (ya no invoca `rmtree_link_aware`).
    flags = {kw.arg: kw.value for kw in construccion.keywords if isinstance(kw.value, ast.Constant)}
    assert flags.get("limpiar_readonly_al_borrar") is not None
    assert flags["limpiar_readonly_al_borrar"].value is True, (
        "el descarte del backup del mod previo debe tolerar el bit read-only de la salida de una herramienta externa"
    )
    assert "should_rollback" in {kw.arg for kw in construccion.keywords}, (
        "la sustitución debe aceptar el veto de lease del servicio: sin él restauraría "
        "sobre la salida de un dueño concurrente"
    )

    def _llamadas_de_presupuesto(raiz: ast.AST) -> list[ast.Call]:
        return [
            nodo
            for nodo in ast.walk(raiz)
            if isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Name)
            and nodo.func.id in {"_espacio_libre_en", "_bytes_del_arbol"}
        ]

    presupuestos = _llamadas_de_presupuesto(metodo)
    assert presupuestos, "el packaging debe seguir midiendo el presupuesto de disco"

    # (3a) El worker mutante NO mide el presupuesto.
    #
    # Es una propiedad de EJECUCIÓN, y comparar posiciones en el ARCHIVO no
    # alcanza para congelarla: `_empaquetar_sincrono` se DEFINE antes del
    # `async with` y se INVOCA dentro, así que cualquier línea de su cuerpo queda
    # "antes" del move-aside aunque corra después. Un mutante que movía la
    # medición adentro del worker sobrevivía a la versión anterior de este ancla
    # por exactamente eso (M01 del barrido de mutación: 0 tests nuevos en rojo).
    workers = [
        nodo for nodo in ast.walk(metodo) if isinstance(nodo, ast.FunctionDef) and nodo.name == "_empaquetar_sincrono"
    ]
    assert len(workers) == 1, "el packaging debe tener UN worker mutante identificable"
    assert _llamadas_de_presupuesto(workers[0]) == [], (
        "el worker que muta el destino NO puede medir el presupuesto: para cuando corre, "
        "el mod previo ya fue apartado y un rechazo por capacidad ya mutó el destino"
    )

    # (3b) La medición se INVOCA desde el cuerpo, y antes del `async with`.
    invocaciones = [
        nodo
        for nodo in ast.walk(metodo)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "to_thread"
        and any(isinstance(arg, ast.Name) and arg.id == "_presupuesto" for arg in nodo.args)
    ]
    assert len(invocaciones) == 1, (
        "el presupuesto debe INVOCARSE una vez desde el cuerpo del packaging: medirlo "
        "dentro del worker mutante (o no invocarlo) deja el rechazo por capacidad "
        "después de la mutación del destino"
    )
    assert invocaciones[0].lineno < sustitucion.lineno, (
        "el presupuesto ENOSPC debe evaluarse ANTES de apartar el mod anterior: "
        "un rechazo por capacidad no puede haber mutado el destino"
    )

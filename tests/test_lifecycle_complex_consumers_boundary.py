"""PR-1b2a — Router y DistributedLockManager: todo el SQL corre dentro del boundary.

Propiedad demostrada (carreras con barreras de ``asyncio.Event``, sin sleeps):
con lifecycle, el SQL real de ``LLMRouter`` (``open``/``_save_message``/
``_load_context``) y de ``DistributedLockManager`` (``initialize`` + operaciones
de lease) ocurre completamente dentro de ``lifecycle.operation()`` /
``lifecycle.transaction()``:

- un ``shutdown_all()`` concurrente ESPERA a la unidad en vuelo (admisión + lock
  del path) en vez de cerrar la conexión debajo de ella;
- una llegada post-shutdown recibe ``DatabaseLifecycleShuttingDownError`` (no
  ``ValueError``/``ProgrammingError`` de conexión cerrada);
- el backoff de ``acquire_lock`` corre con el boundary YA liberado (nunca se
  sostiene el path lock durante el sleep);
- el orden de locks del Router es único: ``_conn_lock`` → boundary del path.

Discriminante de las carreras: un ``Event`` que se dispara cuando el ``close()``
de la conexión COMPLETA. Con boundary correcto, el cierre no puede ocurrir
mientras el SQL está suspendido (el shutdown queda en el drenaje) y la espera
del evento agota su timeout; bajo reversión (``get_connection`` + SQL suelto),
el cierre completa durante el vuelo y el assert queda ROJO de forma
determinística — sin depender de cuántos yields necesite el worker thread.

Jerarquía de autoridad: conducta observable > estado interno (``_admitted`` como
evidencia complementaria) > inventario AST (orden estructural, sin dataflow).
Timeout solo como detector de hang / de ausencia de cierre.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
from pathlib import Path

import pytest

import sky_claw.app.db.locks as locks_mod
from sky_claw.app.agent.router import LLMRouter
from sky_claw.app.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
    DatabaseLifecycleShuttingDownError,
)
from sky_claw.app.db.locks import DistributedLockManager

DEADLINE = 5.0

#: Plazo de la espera "el cierre NO debe ocurrir": el drenaje bloquea el
#: shutdown indefinidamente mientras la unidad SQL esté admitida, así que un
#: timeout acá es determinístico (no es sincronización por sleep).
CIERRE_DEBIDO_TIMEOUT = 2.0

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_PAQUETE = _RAIZ_REPO / "sky_claw"


async def ceder(veces: int = 60) -> None:
    """Vacía la cola de listos del loop (no es sleep de sincronización)."""
    for _ in range(veces):
        await asyncio.sleep(0)


def _mgr(*paths: Path) -> DatabaseLifecycleManager:
    return DatabaseLifecycleManager(
        db_paths=list(paths),
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )


class _CursorSuspensor:
    """Proxy del async-context-manager de ``aiosqlite.Connection.execute``.

    En aiosqlite 0.22 ``conn.execute(...)`` devuelve un
    ``_AsyncGeneratorContextManager`` (no un Cursor). El proxy:

    - ``modo="await"``: suspende en ``__await__`` (patrón ``await conn.execute``).
    - ``modo="fetchall"``: suspende en ``fetchall`` (patrón ``async with``).
    """

    def __init__(self, real: object, entro: asyncio.Event, puerta: asyncio.Event, *, modo: str) -> None:
        self._real = real
        self._cursor: object | None = None
        self._entro = entro
        self._puerta = puerta
        self._modo = modo

    def __await__(self):
        async def _esperar_y_completar():
            self._entro.set()
            await self._puerta.wait()
            return await self._real

        return _esperar_y_completar().__await__()

    async def __aenter__(self):
        self._cursor = await self._real.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object):
        return await self._real.__aexit__(*exc_info)

    async def fetchall(self):
        if self._modo == "fetchall":
            self._entro.set()
            await self._puerta.wait()
        return await self._cursor.fetchall()


def _patch_execute_con_barrera(conn, sql_marcador: str, modo: str, entro, puerta):
    """Intercepta ``conn.execute`` y devuelve un ``_CursorSuspensor`` para el SQL
    que matchea el marcador; el resto pasa directo."""
    real_execute = conn.execute

    def execute_patch(sql, *args, **kwargs):
        cur = real_execute(sql, *args, **kwargs)
        if isinstance(sql, str) and sql_marcador in sql:
            return _CursorSuspensor(cur, entro, puerta, modo=modo)
        return cur

    conn.execute = execute_patch  # type: ignore[method-assign]
    return real_execute


def _patch_executescript_con_barrera(conn, entro, puerta):
    real_executescript = conn.executescript

    async def executescript_patch(sql: str):
        entro.set()
        await puerta.wait()
        return await real_executescript(sql)

    conn.executescript = executescript_patch  # type: ignore[method-assign]
    return real_executescript


def _patch_close_con_evento(conn, evento: asyncio.Event):
    """Envuelve ``conn.close``: setea el evento cuando el cierre COMPLETA.

    Es el discriminante determinístico de las carreras: con boundary correcto el
    shutdown no llega al cierre mientras la unidad SQL esté admitida; bajo
    reversión, el cierre completa durante el vuelo y el evento se dispara.
    """
    real_close = conn.close

    async def close_patch():
        await real_close()
        evento.set()

    conn.close = close_patch  # type: ignore[method-assign]
    return real_close


async def _cierre_ocurrio_durante_el_vuelo(evento: asyncio.Event) -> bool:
    """True si el cierre de la conexión completó mientras el SQL está suspendido."""
    try:
        await asyncio.wait_for(evento.wait(), timeout=CIERRE_DEBIDO_TIMEOUT)
    except TimeoutError:
        return False
    return True


# ===========================================================================
# LLMRouter — carreras conductuales
# ===========================================================================


async def _router_abierto(tmp_path: Path, nombre: str = "history.db") -> tuple[LLMRouter, DatabaseLifecycleManager]:
    history = tmp_path / nombre
    lifecycle = _mgr(history)
    await lifecycle.init_all()
    router = LLMRouter(db_path=str(history), api_key="test-api-key-123", lifecycle=lifecycle)
    await router.open()
    return router, lifecycle


async def test_router_save_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """R-A: un INSERT de ``_save_message`` en vuelo mantiene la admisión; el
    shutdown NO puede cerrar la conexión debajo (discriminante: el cierre no
    completa) y el save termina correctamente."""
    router, lifecycle = await _router_abierto(tmp_path)
    conn = await lifecycle.get_connection(tmp_path / "history.db")

    entro, puerta = asyncio.Event(), asyncio.Event()
    cierre_completo = asyncio.Event()
    real_execute = _patch_execute_con_barrera(conn, "INSERT INTO chat_history", "await", entro, puerta)
    real_close = _patch_close_con_evento(conn, cierre_completo)

    try:
        t_save = asyncio.create_task(router._save_message("c1", "user", "hola"))
        await asyncio.wait_for(entro.wait(), DEADLINE)  # INSERT suspendido DENTRO de transaction()
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not await _cierre_ocurrio_durante_el_vuelo(cierre_completo), (
            "shutdown cerró la conexión con un save en vuelo (boundary roto)"
        )
        assert not t_sd.done(), "shutdown se completó con un save en vuelo (boundary roto)"
        assert lifecycle._admitted >= 1  # evidencia complementaria del mecanismo

        puerta.set()
        await asyncio.wait_for(t_save, DEADLINE)
        await asyncio.wait_for(t_sd, DEADLINE)
    finally:
        conn.execute = real_execute
        conn.close = real_close

    assert lifecycle._admitted == 0


async def test_router_load_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """R-B: un fetch de ``_load_context`` en vuelo mantiene la admisión; el
    shutdown NO puede cerrar la conexión debajo y la lectura termina bien."""
    router, lifecycle = await _router_abierto(tmp_path)
    await router._save_message("c1", "user", "hola")
    conn = await lifecycle.get_connection(tmp_path / "history.db")

    entro, puerta = asyncio.Event(), asyncio.Event()
    cierre_completo = asyncio.Event()
    real_execute = _patch_execute_con_barrera(conn, "SELECT role, content", "fetchall", entro, puerta)
    real_close = _patch_close_con_evento(conn, cierre_completo)
    resultado: dict[str, object] = {"filas": None, "error": None}

    async def lector() -> None:
        try:
            resultado["filas"] = len(await router._load_context("c1"))
        except Exception as e:  # noqa: BLE001 — observamos el cierre prematuro de la reversión
            resultado["error"] = f"{type(e).__name__}: {e}"

    try:
        t_lec = asyncio.create_task(lector())
        await asyncio.wait_for(entro.wait(), DEADLINE)  # fetch suspendido DENTRO de operation()
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not await _cierre_ocurrio_durante_el_vuelo(cierre_completo), (
            "shutdown cerró la conexión con una lectura en vuelo (boundary roto)"
        )
        assert not t_sd.done(), "shutdown se completó con una lectura en vuelo (boundary roto)"

        puerta.set()
        await asyncio.wait_for(t_lec, DEADLINE)
        await asyncio.wait_for(t_sd, DEADLINE)
    finally:
        conn.execute = real_execute
        conn.close = real_close

    assert resultado["error"] is None, f"la lectura falló por cierre debajo del SQL: {resultado['error']}"
    assert resultado["filas"] == 1


async def test_router_open_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """R-C: el executescript del schema de ``open()`` corre bajo el boundary;
    el shutdown NO puede cerrar la conexión debajo del DDL."""
    history = tmp_path / "history.db"
    lifecycle = _mgr(history)
    await lifecycle.init_all()  # pre-resolver la conexión para patch-ear sus métodos
    conn = await lifecycle.get_connection(history)

    entro, puerta = asyncio.Event(), asyncio.Event()
    cierre_completo = asyncio.Event()
    real_executescript = _patch_executescript_con_barrera(conn, entro, puerta)
    real_close = _patch_close_con_evento(conn, cierre_completo)

    router = LLMRouter(db_path=str(history), api_key="test-api-key-123", lifecycle=lifecycle)
    error: dict[str, object] = {"e": None}

    try:
        t_open = asyncio.create_task(router.open())
        await asyncio.wait_for(entro.wait(), DEADLINE)  # executescript suspendido DENTRO de operation()
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not await _cierre_ocurrio_durante_el_vuelo(cierre_completo), (
            "shutdown cerró la conexión con un open en vuelo (boundary roto)"
        )
        assert not t_sd.done(), "shutdown se completó con un open en vuelo (boundary roto)"

        puerta.set()
        await asyncio.wait_for(t_open, DEADLINE)
        await asyncio.wait_for(t_sd, DEADLINE)
    except BaseException as e:  # noqa: BLE001 — observamos el cierre prematuro de la reversión
        error["e"] = f"{type(e).__name__}: {e}"
        puerta.set()
        raise
    finally:
        conn.executescript = real_executescript
        conn.close = real_close

    assert error["e"] is None, f"open() falló por cierre debajo del DDL: {error['e']}"


@pytest.mark.parametrize(
    "operacion",
    [
        ("save", lambda router: router._save_message("c1", "user", "tarde")),
        ("load", lambda router: router._load_context("c1")),
    ],
    ids=["save", "load"],
)
async def test_router_late_admission_es_shutting_down(tmp_path: Path, operacion: tuple[str, object]) -> None:
    """R-D: tras empezar el shutdown, una operación DB del router recibe
    ``DatabaseLifecycleShuttingDownError`` — nunca ValueError/ProgrammingError."""
    router, lifecycle = await _router_abierto(tmp_path)
    await lifecycle.shutdown_all()

    _, fabrica = operacion
    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await fabrica(router)


async def test_router_persistencia_funciona_con_lifecycle(tmp_path: Path) -> None:
    """Funcional: open → save → load → close sobre la conexión managed."""
    router, lifecycle = await _router_abierto(tmp_path)
    try:
        await router._save_message("chat-1", "user", "hola")
        await router._save_message("chat-1", "assistant", "qué tal")
        contexto = await router._load_context("chat-1")
        assert [m["content"] for m in contexto] == ["hola", "qué tal"]

        await router.close()
        with pytest.raises(RuntimeError, match="Router database is not open"):
            await router._save_message("chat-1", "user", "post-close")
    finally:
        await lifecycle.shutdown_all()


# ===========================================================================
# Ancla estructural — orden de locks del Router: _conn_lock → boundary del path
# ===========================================================================


def _arbol_de(archivo: Path) -> ast.AST:
    return ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))


def _mapa_de_padres(arbol: ast.AST) -> dict[ast.AST, ast.AST]:
    padres: dict[ast.AST, ast.AST] = {}
    for nodo in ast.walk(arbol):
        for hijo in ast.iter_child_nodes(nodo):
            padres[hijo] = nodo
    return padres


def _qualname_envolvente(padres: dict[ast.AST, ast.AST], objetivo: ast.AST) -> str:
    nombres: list[str] = []
    cur: ast.AST | None = objetivo
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombres.append(cur.name)
        cur = padres.get(cur)
    return ".".join(reversed(nombres)) if nombres else "<module>"


_ROUTER_PY = _PAQUETE / "app" / "agent" / "router.py"

_ORDER_ESPERADO = {"open": 1, "_save_message": 1, "_load_context": 1}


def test_router_orden_de_locks_conn_lock_antes_del_boundary() -> None:
    """Ancla de orden estructural (PR-1b2a): TODO llamada a
    ``self._lifecycle.operation/transaction`` del router está léxicamente DENTRO
    de un ``async with self._conn_lock`` — el orden único es ``_conn_lock`` →
    boundary del path. Invertir el orden (boundary primero, lock después) rompe
    el test. Es INVENTARIO de orden, no prueba de seguridad conductual (eso lo
    hacen las carreras con barreras de arriba)."""
    arbol = _arbol_de(_ROUTER_PY)
    padres = _mapa_de_padres(arbol)
    dentro_de_conn_lock: dict[str, int] = {}
    fuera_de_conn_lock: list[str] = []

    for nodo in ast.walk(arbol):
        if not (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr in {"operation", "transaction"}
            and isinstance(nodo.func.value, ast.Attribute)
            and nodo.func.value.attr == "_lifecycle"
        ):
            continue
        metodo = _qualname_envolvente(padres, nodo).split(".")[-1]
        esta_dentro = False
        cur = padres.get(nodo)
        while cur is not None:
            if isinstance(cur, ast.AsyncWith):
                ctx = cur.items[0].context_expr
                if isinstance(ctx, ast.Attribute) and ctx.attr == "_conn_lock":
                    esta_dentro = True
                    break
            cur = padres.get(cur)
        if esta_dentro:
            dentro_de_conn_lock[metodo] = dentro_de_conn_lock.get(metodo, 0) + 1
        else:
            fuera_de_conn_lock.append(metodo)

    assert fuera_de_conn_lock == [], (
        f"boundary del lifecycle fuera de _conn_lock en: {fuera_de_conn_lock} (inversión de orden)"
    )
    assert dentro_de_conn_lock == _ORDER_ESPERADO


# ===========================================================================
# DistributedLockManager — carreras conductuales
# ===========================================================================


async def _dlm_inicializado(
    tmp_path: Path, nombre: str = "locks.db", **kwargs: object
) -> tuple[DistributedLockManager, DatabaseLifecycleManager]:
    db = tmp_path / nombre
    lifecycle = _mgr(db)
    dm = DistributedLockManager(str(db), lifecycle=lifecycle, **kwargs)
    await dm.initialize()
    return dm, lifecycle


async def test_dlm_initialize_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """D-A: el executescript de ``initialize()`` corre bajo el boundary; el
    shutdown NO puede cerrar la conexión debajo del DDL (discriminante: el
    cierre no completa durante el vuelo)."""
    db = tmp_path / "locks.db"
    lifecycle = _mgr(db)
    await lifecycle.init_all()  # pre-resolver la conexión para patch-ear sus métodos
    conn = await lifecycle.get_connection(db)

    entro, puerta = asyncio.Event(), asyncio.Event()
    cierre_completo = asyncio.Event()
    real_executescript = _patch_executescript_con_barrera(conn, entro, puerta)
    real_close = _patch_close_con_evento(conn, cierre_completo)

    dm = DistributedLockManager(str(db), lifecycle=lifecycle)
    error: dict[str, object] = {"e": None}

    try:
        t_init = asyncio.create_task(dm.initialize())
        await asyncio.wait_for(entro.wait(), DEADLINE)  # executescript suspendido DENTRO de operation()
        t_sd = asyncio.create_task(lifecycle.shutdown_all())
        await ceder()
        assert not await _cierre_ocurrio_durante_el_vuelo(cierre_completo), (
            "shutdown cerró la conexión con un initialize en vuelo (boundary roto)"
        )
        assert not t_sd.done(), "shutdown se completó con un initialize en vuelo (boundary roto)"

        puerta.set()
        await asyncio.wait_for(t_init, DEADLINE)
        await asyncio.wait_for(t_sd, DEADLINE)
    except BaseException as e:  # noqa: BLE001 — observamos el cierre prematuro de la reversión
        error["e"] = f"{type(e).__name__}: {e}"
        puerta.set()
        raise
    finally:
        conn.executescript = real_executescript
        conn.close = real_close

    assert error["e"] is None, f"initialize() falló por cierre debajo del DDL: {error['e']}"


@pytest.mark.parametrize(
    "operacion",
    [
        ("acquire", lambda dm: dm.acquire_lock("r-tarde", "agente-tarde")),
        ("get_lock_info", lambda dm: dm.get_lock_info("r-tarde")),
        ("cleanup", lambda dm: dm.cleanup_expired()),
    ],
    ids=["acquire", "get_lock_info", "cleanup_expired"],
)
async def test_dlm_late_admission_es_shutting_down(tmp_path: Path, operacion: tuple[str, object]) -> None:
    """D-B: tras empezar el shutdown, un intento DB del manager recibe
    ``DatabaseLifecycleShuttingDownError`` — nunca ValueError de conexión cerrada."""
    dm, lifecycle = await _dlm_inicializado(tmp_path)
    await lifecycle.shutdown_all()

    _, fabrica = operacion
    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await fabrica(dm)


async def test_dlm_renew_durante_shutdown_es_false_y_no_revive(tmp_path: Path) -> None:
    """D-C: durante el shutdown, ``renew_lock`` devuelve False (lease no
    renovable = lease perdida) y NO reabre ni revive la conexión managed."""
    dm, lifecycle = await _dlm_inicializado(tmp_path)
    await dm.acquire_lock("r-renov", "agente-a")
    await lifecycle.shutdown_all()

    assert await dm.renew_lock("r-renov", "agente-a") is False
    assert lifecycle._shutting_down is True
    assert lifecycle.managed_paths == [], "renew_lock revivió una conexión managed durante el shutdown"


async def test_dlm_acquire_retry_conserva_semantica_y_backoff_fuera_del_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-D: (1) el retry normal conserva la semántica (conflicto → reintento →
    éxito); (2) durante el backoff el boundary está LIBERADO (``_admitted == 0``
    en cada sleep, como evidencia complementaria del mecanismo)."""
    dm, lifecycle = await _dlm_inicializado(tmp_path, max_retries=5, backoff_base=0.05, backoff_max=0.1)
    conn = await lifecycle.get_connection(tmp_path / "locks.db")

    # Recurso ocupado por OTRO agente con lease vigente (expires_at lejano).
    await conn.execute(
        "INSERT INTO resource_locks (resource_id, agent_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        ("recurso-retry", "otro", 0.0, 4_000_000_000.0),
    )
    await conn.commit()

    admitidos_en_backoff: list[int] = []
    segundo_backoff = asyncio.Event()
    real_sleep = locks_mod.asyncio.sleep

    async def sleep_espia(delay: float):
        if delay > 0:
            admitidos_en_backoff.append(lifecycle._admitted)
            if len(admitidos_en_backoff) >= 2:
                segundo_backoff.set()
        await real_sleep(delay)

    async def liberador() -> None:
        await segundo_backoff.wait()
        async with lifecycle.operation(tmp_path / "locks.db") as c:
            await c.execute("DELETE FROM resource_locks WHERE resource_id = ?", ("recurso-retry",))
            await c.commit()

    monkeypatch.setattr(locks_mod.asyncio, "sleep", sleep_espia)
    t_liberador = asyncio.create_task(liberador())
    try:
        info = await asyncio.wait_for(dm.acquire_lock("recurso-retry", "agente-1"), DEADLINE)
    finally:
        await asyncio.wait_for(t_liberador, DEADLINE)
        await lifecycle.shutdown_all()

    assert info.resource_id == "recurso-retry"
    assert info.agent_id == "agente-1"
    assert len(admitidos_en_backoff) >= 2, "el retry nunca durmió el backoff"
    assert all(a == 0 for a in admitidos_en_backoff), (
        f"backoff corrió con trabajo admitido (boundary envuelve el sleep): {admitidos_en_backoff}"
    )


async def test_dlm_backoff_fuera_del_boundary_shutdown_completa_durante_el_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-D (conductual): el shutdown completa MIENTRAS acquire duerme el backoff
    (el sleep no es trabajo admitido); el intento siguiente es rechazado por la
    admisión con ``DatabaseLifecycleShuttingDownError``."""
    dm, lifecycle = await _dlm_inicializado(tmp_path, max_retries=3, backoff_base=0.1, backoff_max=0.2)
    conn = await lifecycle.get_connection(tmp_path / "locks.db")

    await conn.execute(
        "INSERT INTO resource_locks (resource_id, agent_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        ("recurso-retenido", "otro", 0.0, 4_000_000_000.0),
    )
    await conn.commit()

    entro_backoff = asyncio.Event()
    retener_backoff = asyncio.Event()
    admitidos_en_backoff: list[int] = []
    real_sleep = locks_mod.asyncio.sleep

    async def sleep_espia(delay: float):
        if delay > 0:
            admitidos_en_backoff.append(lifecycle._admitted)
            if not entro_backoff.is_set():
                entro_backoff.set()
                await retener_backoff.wait()  # retenemos el backoff: NO es trabajo admitido
        await real_sleep(delay)

    monkeypatch.setattr(locks_mod.asyncio, "sleep", sleep_espia)
    t_acq = asyncio.create_task(dm.acquire_lock("recurso-retenido", "agente-1"))
    try:
        await asyncio.wait_for(entro_backoff.wait(), DEADLINE)
        assert admitidos_en_backoff == [0], "el backoff entró con trabajo admitido (boundary envuelve el sleep)"

        # El shutdown completa DURANTE el backoff retenido: la propiedad conductual.
        await asyncio.wait_for(lifecycle.shutdown_all(), DEADLINE)

        retener_backoff.set()
        with pytest.raises(DatabaseLifecycleShuttingDownError):
            await asyncio.wait_for(t_acq, DEADLINE)
    finally:
        retener_backoff.set()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(t_acq, DEADLINE)


async def test_dlm_metodos_migrados_funcionan_con_lifecycle(tmp_path: Path) -> None:
    """Funcional: acquire → get_lock_info → renew → cleanup → release →
    force_release sobre la conexión managed, con semántica intacta."""
    dm, lifecycle = await _dlm_inicializado(tmp_path)

    info = await dm.acquire_lock("recurso-func", "agente-func")
    assert info.agent_id == "agente-func"

    leido = await dm.get_lock_info("recurso-func")
    assert leido is not None
    assert leido.agent_id == "agente-func"
    assert leido.acquired_at == info.acquired_at

    assert await dm.renew_lock("recurso-func", "agente-func") is True
    assert await dm.renew_lock("recurso-func", "otro-agente") is False  # no es dueño
    assert await dm.renew_lock("inexistente", "agente-func") is False

    assert await dm.cleanup_expired() == 0  # nada expirado

    assert await dm.force_release("recurso-func") is True
    assert await dm.release_lock("recurso-func", "agente-func") is False  # ya fue liberado
    assert await dm.get_lock_info("recurso-func") is None

    await lifecycle.shutdown_all()

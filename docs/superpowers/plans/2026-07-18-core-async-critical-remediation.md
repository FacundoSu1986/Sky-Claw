# Core Async Critical Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminar la intercalación de transacciones sobre conexiones SQLite compartidas y hacer que `CoreEventBus` drene de forma determinista publicaciones y fallos ya aceptados durante `stop()`/`start()`.

**Architecture:** `DatabaseLifecycleManager` pasa a ser el único dueño del límite transaccional de escritura por ruta, con commit/rollback protegido ante cancelación y observado hasta finalizar. `DatabaseAgent` y `DLQManager` delegan sus escrituras a ese contrato. `CoreEventBus` usa una máquina de estados serializada, un sentinel FIFO y tareas DLQ con referencia fuerte para cerrar sin publicar detrás del sentinel ni perder fallos por `CancelledError`.

**Tech Stack:** Python 3.11+, `asyncio`, `aiosqlite`, `contextlib.asynccontextmanager`, `pytest`, `pytest-asyncio`, `ruff`, `mypy`.

---

## Mapa de archivos

- Crear `tests/test_core_async_transactions.py`: regresiones deterministas del límite transaccional, cancelación durante commit/rollback y uso compartido por `DatabaseAgent`/`DLQManager`.
- Crear `tests/test_event_bus_shutdown.py`: regresiones de cola llena, publicación concurrente con `stop()`, reinicio, fallo de `start()` y persistencia DLQ al cancelar un suscriptor.
- Modificar `sky_claw/antigravity/core/db_lifecycle.py`: añadir `transaction()` y el observador de operaciones SQLite resistentes a cancelación.
- Modificar `sky_claw/antigravity/core/database.py`: reemplazar cada `execute()+commit()` de escritura por el límite transaccional del lifecycle.
- Modificar `sky_claw/antigravity/core/dlq_manager.py`: separar conexión de lectura y transacción de escritura; migrar todo DDL/DML.
- Modificar `sky_claw/antigravity/core/event_bus.py`: introducir estados, serialización de lifecycle, drenaje por sentinel y persistencia DLQ protegida.
- Modificar `docs/superpowers/specs/2026-07-18-core-async-critical-remediation-design.md` solamente si la implementación descubre una divergencia contractual; cualquier divergencia debe quedar explicada antes del commit correspondiente.

### Task 1: Límite transaccional cancel-safe en `DatabaseLifecycleManager`

**Files:**
- Create: `tests/test_core_async_transactions.py`
- Modify: `sky_claw/antigravity/core/db_lifecycle.py:18-24,483-541`
- Test: `tests/test_core_async_transactions.py`

- [ ] **Step 1: Escribir regresiones rojas de serialización y rollback**

Crear `tests/test_core_async_transactions.py` con imports, fixture y estas pruebas. La segunda transacción debe quedar fuera del contexto hasta que la primera haya terminado su rollback.

```python
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)


@pytest.fixture
async def lifecycle(tmp_path: Path):
    db_path = tmp_path / "shared.db"
    manager = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    await manager.init_all()
    conn = await manager.get_connection(db_path)
    await conn.execute("CREATE TABLE writes (value TEXT NOT NULL)")
    await conn.commit()
    try:
        yield manager, db_path
    finally:
        await manager.shutdown_all()


async def test_transaction_serializa_escritores_y_aísla_rollback(lifecycle) -> None:
    manager, db_path = lifecycle
    primera_dentro = asyncio.Event()
    liberar_primera = asyncio.Event()
    segunda_dentro = asyncio.Event()

    async def primera() -> None:
        with pytest.raises(RuntimeError, match="fallo deliberado"):
            async with manager.transaction(db_path) as conn:
                await conn.execute("INSERT INTO writes VALUES ('primera')")
                primera_dentro.set()
                await liberar_primera.wait()
                raise RuntimeError("fallo deliberado")

    async def segunda() -> None:
        await primera_dentro.wait()
        async with manager.transaction(db_path) as conn:
            segunda_dentro.set()
            await conn.execute("INSERT INTO writes VALUES ('segunda')")

    task_primera = asyncio.create_task(primera())
    task_segunda = asyncio.create_task(segunda())
    await primera_dentro.wait()
    await asyncio.sleep(0)
    assert not segunda_dentro.is_set()

    liberar_primera.set()
    await asyncio.gather(task_primera, task_segunda)

    conn = await manager.get_connection(db_path)
    rows = await (await conn.execute("SELECT value FROM writes ORDER BY rowid")).fetchall()
    assert [row[0] for row in rows] == ["segunda"]
```

- [ ] **Step 2: Añadir regresiones rojas de cancelación durante rollback y commit**

Añadir al mismo archivo. Estas pruebas fuerzan la cancelación exactamente mientras la operación SQLite está pausada; la tarea exterior no puede finalizar antes que la operación protegida.

```python
async def test_cancelación_espera_rollback_y_no_persiste_fila(lifecycle, monkeypatch) -> None:
    manager, db_path = lifecycle
    conn = await manager.get_connection(db_path)
    rollback_real = conn.rollback
    rollback_iniciado = asyncio.Event()
    liberar_rollback = asyncio.Event()
    escritura_hecha = asyncio.Event()

    async def rollback_lento() -> None:
        rollback_iniciado.set()
        await liberar_rollback.wait()
        await rollback_real()

    monkeypatch.setattr(conn, "rollback", rollback_lento)

    async def escritor() -> None:
        async with manager.transaction(db_path) as db:
            await db.execute("INSERT INTO writes VALUES ('cancelada')")
            escritura_hecha.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(escritor())
    await escritura_hecha.wait()
    task.cancel()
    await rollback_iniciado.wait()
    assert not task.done()
    liberar_rollback.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = await (await conn.execute("SELECT value FROM writes")).fetchall()
    assert rows == []


async def test_cancelación_espera_commit_y_conserva_commit_point(lifecycle, monkeypatch) -> None:
    manager, db_path = lifecycle
    conn = await manager.get_connection(db_path)
    commit_real = conn.commit
    commit_iniciado = asyncio.Event()
    liberar_commit = asyncio.Event()

    async def commit_lento() -> None:
        commit_iniciado.set()
        await liberar_commit.wait()
        await commit_real()

    monkeypatch.setattr(conn, "commit", commit_lento)

    async def escritor() -> None:
        async with manager.transaction(db_path) as db:
            await db.execute("INSERT INTO writes VALUES ('confirmada')")

    task = asyncio.create_task(escritor())
    await commit_iniciado.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    liberar_commit.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = await (await conn.execute("SELECT value FROM writes")).fetchall()
    assert [row[0] for row in rows] == ["confirmada"]
```

- [ ] **Step 3: Ejecutar las tres pruebas y verificar el rojo**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_core_async_transactions.py
```

Expected: FAIL en las tres pruebas con `AttributeError: 'DatabaseLifecycleManager' object has no attribute 'transaction'`.

- [ ] **Step 4: Implementar el contrato transaccional mínimo**

En `db_lifecycle.py`, importar `asynccontextmanager`, `AsyncGenerator` y `Awaitable`. Añadir estos métodos inmediatamente después de `get_write_lock()`:

```python
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager


    @staticmethod
    async def _await_db_operation(awaitable: Awaitable[None]) -> None:
        """Observa una operación SQLite hasta terminar aunque el caller sea cancelado."""
        task = asyncio.ensure_future(awaitable)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancellation = exc

        task.result()
        if cancellation is not None:
            raise cancellation

    @asynccontextmanager
    async def transaction(
        self,
        db_path: Path | str,
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Serializa una transacción de escritura completa sobre la conexión compartida."""
        conn = await self.get_connection(db_path)
        async with self.get_write_lock(db_path):
            try:
                yield conn
            except BaseException:
                await self._await_db_operation(conn.rollback())
                raise
            else:
                await self._await_db_operation(conn.commit())
```

No capturar `Exception`: `CancelledError`, `KeyboardInterrupt` y `SystemExit` también deben disparar rollback. No liberar el lock hasta que commit/rollback haya concluido.

- [ ] **Step 5: Ejecutar las pruebas focalizadas y verificar verde**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_core_async_transactions.py tests\test_db_lifecycle.py
```

Expected: PASS; no `PytestUnhandledThreadExceptionWarning` nuevo procedente de estas pruebas.

- [ ] **Step 6: Ejecutar lint y tipos sobre el corte**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff check sky_claw\antigravity\core\db_lifecycle.py tests\test_core_async_transactions.py
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff format --check sky_claw\antigravity\core\db_lifecycle.py tests\test_core_async_transactions.py
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m mypy sky_claw\antigravity\core\db_lifecycle.py
```

Expected: los tres comandos terminan con exit code 0.

- [ ] **Step 7: Commit del límite transaccional**

```powershell
git add tests/test_core_async_transactions.py sky_claw/antigravity/core/db_lifecycle.py
git commit -m "fix: serializar transacciones sqlite compartidas"
```

### Task 2: Migrar `DatabaseAgent` y `DLQManager` al dueño transaccional

**Files:**
- Modify: `tests/test_core_async_transactions.py`
- Modify: `sky_claw/antigravity/core/database.py:31-102,138-293`
- Modify: `sky_claw/antigravity/core/dlq_manager.py:141-177,200-257,292-421`
- Test: `tests/test_core_async_transactions.py`
- Test: `tests/test_dlq_manager.py`
- Test: `tests/test_dlq_attempts_atomic.py`
- Test: `tests/test_dlq_double_dispatch.py`

- [ ] **Step 1: Añadir una prueba roja que exige el mismo lock para ambos productores**

Añadir al archivo de pruebas imports de `asynccontextmanager`, `DatabaseAgent`, `DLQManager` y `Event`, más un lifecycle espía que cuenta entradas activas:

```python
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import aiosqlite

from sky_claw.antigravity.core.database import DatabaseAgent
from sky_claw.antigravity.core.dlq_manager import DLQManager
from sky_claw.antigravity.core.event_bus import Event


class LifecycleEspía(DatabaseLifecycleManager):
    entradas = 0
    activas = 0
    máximo_activas = 0

    @asynccontextmanager
    async def transaction(
        self,
        db_path: Path | str,
    ) -> AsyncGenerator[aiosqlite.Connection, None]:
        type(self).entradas += 1
        async with super().transaction(db_path) as conn:
            type(self).activas += 1
            type(self).máximo_activas = max(type(self).máximo_activas, type(self).activas)
            await asyncio.sleep(0)
            try:
                yield conn
            finally:
                type(self).activas -= 1


async def test_database_agent_y_dlq_delegan_escrituras_al_lifecycle(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "shared.db"
    LifecycleEspía.entradas = 0
    LifecycleEspía.activas = 0
    LifecycleEspía.máximo_activas = 0
    lifecycle = LifecycleEspía(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    await lifecycle.init_all()

    agent = DatabaseAgent(str(db_path))
    agent._lifecycle = lifecycle
    agent._conn = await lifecycle.get_connection(db_path)
    await agent._conn.executescript(
        "CREATE TABLE agent_memory (key TEXT PRIMARY KEY, value TEXT, updated_at REAL);"
    )
    await agent._conn.commit()

    dlq = DLQManager(db_path=db_path, handler_resolver={}.get, lifecycle=lifecycle)

    async def handler(event: Event) -> None:
        return None

    try:
        await asyncio.gather(
            agent.set_memory("clave", {"valor": 1}),
            dlq.enqueue(Event(topic="probe", payload={}), handler, RuntimeError("fallo")),
        )
        assert LifecycleEspía.entradas >= 3
        assert LifecycleEspía.máximo_activas == 1
    finally:
        await lifecycle.shutdown_all()
```

El mínimo de tres entradas corresponde a `set_memory`, creación de esquema DLQ y `enqueue`.

- [ ] **Step 2: Ejecutar la prueba nueva y verificar el rojo**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_core_async_transactions.py::test_database_agent_y_dlq_delegan_escrituras_al_lifecycle
```

Expected: FAIL porque `DatabaseAgent` y la rama lifecycle de `DLQManager` todavía ejecutan `commit()` directamente; `LifecycleEspía.entradas` queda por debajo de 3.

- [ ] **Step 3: Añadir el adaptador privado de transacción a `DatabaseAgent`**

Importar `asynccontextmanager` y `AsyncGenerator`. Añadir después de `_get_conn()`:

```python
    @asynccontextmanager
    async def _write_transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise RuntimeError("DatabaseAgent not initialized. Await init_db() first.")
        async with lifecycle.transaction(self.db_path) as conn:
            yield conn
```

Este método no crea un segundo lock ni hace commit: solamente conserva el error público de “no inicializado” y delega al lifecycle.

- [ ] **Step 4: Migrar cada unidad de escritura de `DatabaseAgent`**

En `init_db`, envolver la creación conjunta de las cinco tablas en un único `async with self._write_transaction() as conn:` y eliminar el commit manual. En las funciones siguientes reemplazar `_get_conn()` + DML + `commit()` por un contexto completo:

```python
async with self._write_transaction() as conn:
    await conn.execute(sql, parámetros)
```

Aplicar ese bloque, conservando exactamente el SQL y retornos existentes, a:

- `update_circuit_breaker`
- `set_memory`
- `add_mod` (el `SELECT id` permanece dentro del contexto antes de retornar)
- `add_conflict`
- `resolve_conflict`
- `log_activity`

El `except sqlite3.Error` existente de cada API permanece fuera del contexto para conservar logging y valor de retorno. No añadir reintentos ni cambiar contratos `None`/`bool`/`int`.

- [ ] **Step 5: Separar lectura y escritura en `DLQManager`**

Mantener `_connect()` para lecturas. En su rama lifecycle eliminar el rollback implícito porque una consulta fallida no debe hacer rollback a una transacción perteneciente a otro componente. Añadir el siguiente contexto inmediatamente después:

```python
    @contextlib.asynccontextmanager
    async def _write_transaction(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        if self._lifecycle is not None:
            async with self._lifecycle.transaction(self._db_path) as db:
                yield db
            return

        async with self._connect() as db:
            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            else:
                await db.commit()
```

La rama standalone conserva conexión por operación. La rama compartida delega lock, commit y rollback.

- [ ] **Step 6: Migrar todas las unidades DDL/DML de `DLQManager`**

Usar `_write_transaction()` y eliminar cada `commit()` local en estas unidades completas:

- `enqueue`: INSERT del evento.
- `_ensure_schema`: `_DDL` completo.
- `_recover_stale_rows`: UPDATE de recuperación.
- `_process_row`: claim condicional `pending -> in_progress`.
- `_process_row`: transición por handler ausente.
- `_process_row`: incremento atómico de intentos y transición a `pending`/`dead`.
- `_process_row`: DELETE tras handler exitoso.
- `_mark_dead`: UPDATE final.

Las consultas `SELECT` de `list_pending`, `list_dead`, `_fetch_due` y la lectura posterior del contador de intentos permanecen en `_connect()`. No mantener un contexto de escritura abierto durante `await handler(event)`: claim, callback y resultado son tres fases separadas.

- [ ] **Step 7: Ejecutar regresiones focalizadas y verificar verde**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_core_async_transactions.py tests\test_db_lifecycle.py tests\test_dlq_manager.py tests\test_dlq_attempts_atomic.py tests\test_dlq_double_dispatch.py tests\test_conflict_persistence.py tests\test_record_conflict_persistence.py
```

Expected: PASS; los tests de claim atómico siguen entregando cada fila una sola vez y los retornos `None`/`bool`/`int` de `DatabaseAgent` no cambian.

- [ ] **Step 8: Ejecutar lint, formato y tipos del corte**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff check sky_claw\antigravity\core\db_lifecycle.py sky_claw\antigravity\core\database.py sky_claw\antigravity\core\dlq_manager.py tests\test_core_async_transactions.py
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff format --check sky_claw\antigravity\core\db_lifecycle.py sky_claw\antigravity\core\database.py sky_claw\antigravity\core\dlq_manager.py tests\test_core_async_transactions.py
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m mypy sky_claw\antigravity\core\db_lifecycle.py sky_claw\antigravity\core\database.py sky_claw\antigravity\core\dlq_manager.py
```

Expected: los tres comandos terminan con exit code 0.

- [ ] **Step 9: Commit de consumidores SQLite**

```powershell
git add tests/test_core_async_transactions.py sky_claw/antigravity/core/database.py sky_claw/antigravity/core/dlq_manager.py
git commit -m "fix: unificar escrituras del core en transacciones"
```

### Task 3: Lifecycle determinista de `CoreEventBus`

**Files:**
- Create: `tests/test_event_bus_shutdown.py`
- Modify: `sky_claw/antigravity/core/event_bus.py:1-295`
- Test: `tests/test_event_bus_shutdown.py`
- Test: `tests/test_core_event_bus.py`
- Test: `tests/test_event_bus_backpressure.py`
- Test: `tests/test_event_bus_dlq_integration.py`

- [ ] **Step 1: Escribir pruebas rojas de drenaje FIFO y reinicio**

Crear `tests/test_event_bus_shutdown.py`:

```python
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus, Event


def _dlq_mock() -> MagicMock:
    dlq = MagicMock()
    dlq.start = AsyncMock()
    dlq.stop = AsyncMock()
    dlq.enqueue = AsyncMock()
    return dlq


async def test_stop_drena_cola_llena_y_publicador_aceptado(monkeypatch) -> None:
    bus = CoreEventBus(max_queue_size=1)
    get_real = bus._queue.get
    dispatcher_pausado = asyncio.Event()
    liberar_dispatcher = asyncio.Event()

    async def get_pausado():
        event = await get_real()
        if event is not None and not dispatcher_pausado.is_set():
            dispatcher_pausado.set()
            await liberar_dispatcher.wait()
        return event

    monkeypatch.setattr(bus._queue, "get", get_pausado)
    await bus.start()
    await bus.publish(Event(topic="probe", payload={"seq": 1}))
    await dispatcher_pausado.wait()
    await bus.publish(Event(topic="probe", payload={"seq": 2}))
    publicación_bloqueada = asyncio.create_task(
        bus.publish(Event(topic="probe", payload={"seq": 3}))
    )
    await asyncio.sleep(0)
    stop_task = asyncio.create_task(bus.stop())
    await asyncio.sleep(0)
    liberar_dispatcher.set()
    await asyncio.wait_for(asyncio.gather(publicación_bloqueada, stop_task), timeout=2)
    assert bus._queue.empty()


async def test_reinicio_entrega_eventos_sin_residuos() -> None:
    bus = CoreEventBus()
    recibidos: list[int] = []

    async def handler(event: Event) -> None:
        recibidos.append(int(event.payload["seq"]))

    bus.subscribe("probe", handler)
    await bus.start()
    await bus.publish(Event(topic="probe", payload={"seq": 1}))
    await bus._queue.join()
    await asyncio.sleep(0)
    await bus.stop()
    await bus.start()
    await bus.publish(Event(topic="probe", payload={"seq": 2}))
    await bus._queue.join()
    await asyncio.sleep(0)
    await bus.stop()
    assert recibidos == [1, 2]
    assert bus._queue.empty()
```

La prueba exige que el sentinel se encole detrás de toda publicación que ya superó la admisión y que el dispatcher consuma hasta encontrarlo.

- [ ] **Step 2: Añadir pruebas rojas de publicación concurrente y fallo de start**

```python
async def test_publish_iniciado_antes_de_stop_no_queda_bloqueado() -> None:
    bus = CoreEventBus(max_queue_size=1)
    await bus.start()
    await bus.publish(Event(topic="sin-handler", payload={"seq": 1}))
    publisher = asyncio.create_task(bus.publish(Event(topic="sin-handler", payload={"seq": 2})))
    await asyncio.sleep(0)
    await asyncio.wait_for(asyncio.gather(publisher, bus.stop()), timeout=2)
    assert bus._queue.empty()


async def test_start_fallido_restaura_estado_y_permite_reintento() -> None:
    dlq = _dlq_mock()
    dlq.start.side_effect = [RuntimeError("dlq no disponible"), None]
    bus = CoreEventBus(dlq=dlq)

    with pytest.raises(RuntimeError, match="dlq no disponible"):
        await bus.start()
    with pytest.raises(RuntimeError, match="bus is not running"):
        await bus.publish(Event(topic="probe", payload={}))

    await bus.start()
    await bus.stop()
    assert dlq.start.await_count == 2
    assert dlq.stop.await_count == 1


async def test_start_y_stop_concurrentes_terminan_detenidos() -> None:
    dlq = _dlq_mock()
    start_dlq_iniciado = asyncio.Event()
    liberar_start_dlq = asyncio.Event()

    async def start_dlq() -> None:
        start_dlq_iniciado.set()
        await liberar_start_dlq.wait()

    dlq.start.side_effect = start_dlq
    bus = CoreEventBus(dlq=dlq)
    start_task = asyncio.create_task(bus.start())
    await start_dlq_iniciado.wait()
    stop_task = asyncio.create_task(bus.stop())
    await asyncio.sleep(0)
    liberar_start_dlq.set()
    await asyncio.wait_for(asyncio.gather(start_task, stop_task), timeout=2)

    with pytest.raises(RuntimeError, match="bus is not running"):
        await bus.publish(Event(topic="probe", payload={}))
    assert bus._dispatch_task is None
    assert dlq.stop.await_count == 1
```

- [ ] **Step 3: Añadir prueba roja de cancelación de handler persistida antes de parar DLQ**

```python
async def test_stop_persiste_handler_cancelado_antes_de_detener_dlq() -> None:
    dlq = _dlq_mock()
    enqueue_iniciado = asyncio.Event()
    liberar_enqueue = asyncio.Event()
    handler_iniciado = asyncio.Event()
    orden: list[str] = []

    async def enqueue(*args: object) -> None:
        orden.append("enqueue-inicio")
        enqueue_iniciado.set()
        await liberar_enqueue.wait()
        orden.append("enqueue-fin")

    async def stop_dlq() -> None:
        orden.append("dlq-stop")

    async def handler(event: Event) -> None:
        handler_iniciado.set()
        await asyncio.Event().wait()

    dlq.enqueue.side_effect = enqueue
    dlq.stop.side_effect = stop_dlq
    bus = CoreEventBus(dlq=dlq)
    bus.subscribe("probe", handler)
    await bus.start()
    await bus.publish(Event(topic="probe", payload={}))
    await handler_iniciado.wait()

    stop_task = asyncio.create_task(bus.stop())
    await enqueue_iniciado.wait()
    assert not stop_task.done()
    liberar_enqueue.set()
    await stop_task

    assert orden == ["enqueue-inicio", "enqueue-fin", "dlq-stop"]
    assert isinstance(dlq.enqueue.call_args.args[2], asyncio.CancelledError)
```

- [ ] **Step 4: Ejecutar el archivo y verificar los rojos**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_event_bus_shutdown.py
```

Expected: la prueba de cola llena agota timeout porque el fallback cancela al dispatcher y deja al publicador aceptado bloqueado; la prueba concurrente deja el bus corriendo porque `stop()` retorna durante `dlq.start()`; la prueba de handler cancelado falla porque `_safe_execute` no captura `CancelledError`.

- [ ] **Step 5: Introducir la máquina de estados y serializar lifecycle**

Importar `Enum` y definir antes de `CoreEventBus`:

```python
class _BusState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
```

En `__init__` añadir:

```python
self._state = _BusState.STOPPED
self._state_lock = asyncio.Lock()
```

Conservar `_running` por compatibilidad interna, pero actualizarlo únicamente al entrar/salir de `RUNNING`.

Reemplazar `start()` por un bloque `async with self._state_lock:` que:

1. retorna si el estado ya es `RUNNING`;
2. fija `STARTING`;
3. ejecuta `dlq.start()`;
4. crea `_dispatch_task` con `_dispatch_loop()`;
5. fija `_running=True` y `RUNNING`;
6. ante cualquier `BaseException`, cancela/observa un dispatcher creado, detiene una DLQ iniciada, limpia referencias, fija `_running=False`/`STOPPED` y relanza.

`start()` no puede solaparse con `stop()` porque ambos mantienen `_state_lock` durante toda la transición.

- [ ] **Step 6: Cambiar el dispatcher a finalización exclusiva por sentinel**

Reemplazar el encabezado de `_dispatch_loop()` y envolver cada item en `try/finally`:

```python
    async def _dispatch_loop(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event is None:
                    return
                # conservar aquí el bucle existente de patrones, backpressure y create_task
            finally:
                self._queue.task_done()
```

No consultar `_running` dentro del loop. Así un `stop()` no desactiva al consumidor antes de que alcance el sentinel y `Queue.join()` nunca pierde un `task_done()` si ocurre una excepción inesperada.

- [ ] **Step 7: Reemplazar `stop()` por drenaje ordenado sin timeout arbitrario**

Bajo `async with self._state_lock:`:

```python
if self._state is _BusState.STOPPED:
    return
self._state = _BusState.STOPPING
self._running = False
dispatch_task = self._dispatch_task
if dispatch_task is not None:
    await self._queue.put(None)
    await dispatch_task
self._dispatch_task = None

for task in list(self._pending_tasks):
    task.cancel()
if self._pending_tasks:
    await asyncio.gather(*self._pending_tasks, return_exceptions=True)
self._pending_tasks.clear()

if self._dlq_tasks:
    await asyncio.gather(*list(self._dlq_tasks), return_exceptions=True)
self._dlq_tasks.clear()
if self._dlq is not None:
    await self._dlq.stop()
self._state = _BusState.STOPPED
```

Encapsular la restauración de estado en `finally` para que una excepción de DLQ no deje `STOPPING`. No usar `put_nowait`, no cancelar el dispatcher por cola llena y no vaciar la cola manualmente.

- [ ] **Step 8: Proteger la persistencia DLQ y tratar cancelación como fallo entregable**

Añadir un único programador con referencia fuerte:

```python
    def _create_dlq_task(
        self,
        event: Event,
        callback: Subscriber,
        exc: BaseException,
    ) -> asyncio.Task[None] | None:
        if self._dlq is None:
            self._events_lost += 1
            return None
        if len(self._dlq_tasks) >= self._MAX_DLQ_TASKS:
            self._events_lost += 1
            return None
        task = asyncio.create_task(self._enqueue_failure(event, callback, exc))
        self._dlq_tasks.add(task)
        task.add_done_callback(self._dlq_tasks.discard)
        return task

    async def _enqueue_failure(
        self,
        event: Event,
        callback: Subscriber,
        exc: BaseException,
    ) -> None:
        try:
            assert self._dlq is not None
            await self._dlq.enqueue(event, callback, exc)
        except Exception:
            self._events_lost += 1
            logger.critical("DLQ enqueue falló; evento perdido", exc_info=True)
```

Usar este programador tanto para backpressure como para `_safe_execute`. Para el callback normal:

```python
    async def _safe_execute(self, callback: Subscriber, event: Event) -> None:
        cancelled: asyncio.CancelledError | None = None
        failure: BaseException | None = None
        try:
            await callback(event)
        except asyncio.CancelledError as exc:
            cancelled = exc
            failure = exc
        except Exception as exc:
            failure = exc

        if failure is not None:
            task = self._create_dlq_task(event, callback, failure)
            if task is not None:
                await asyncio.shield(task)
        if cancelled is not None:
            raise cancelled
```

Si una segunda cancelación interrumpe el `shield`, la tarea DLQ sigue en `_dlq_tasks` y `stop()` la observa antes de `dlq.stop()`. Mantener los mensajes de log actuales con tópico/handler y conservar `BackpressureDroppedError` como excepción de backpressure.

- [ ] **Step 9: Ejecutar suite focalizada del EventBus**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_event_bus_shutdown.py tests\test_core_event_bus.py tests\test_event_bus_require_dlq.py tests\test_event_bus_dlq_integration.py tests\test_event_bus_backpressure.py
```

Expected: PASS; ninguna prueba queda bloqueada, `stop()` sigue siendo idempotente y el modo sin DLQ conserva su API.

- [ ] **Step 10: Ejecutar lint, formato y tipos del corte**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff check sky_claw\antigravity\core\event_bus.py tests\test_event_bus_shutdown.py
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff format --check sky_claw\antigravity\core\event_bus.py tests\test_event_bus_shutdown.py
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m mypy sky_claw\antigravity\core\event_bus.py
```

Expected: los tres comandos terminan con exit code 0.

- [ ] **Step 11: Commit del lifecycle del bus**

```powershell
git add tests/test_event_bus_shutdown.py sky_claw/antigravity/core/event_bus.py
git commit -m "fix: drenar event bus durante apagado y reinicio"
```

### Task 4: Prueba de estrés integrada y gates de entrega

**Files:**
- Modify: `tests/test_core_async_transactions.py`
- Modify: `tests/test_event_bus_shutdown.py`
- Verify: `sky_claw/`
- Verify: `tests/`

- [ ] **Step 1: Añadir estrés acotado SQLite con valores únicos**

Añadir una prueba que lanza 25 `set_memory` y 25 `DLQManager.enqueue` sobre el mismo lifecycle/archivo, espera todas las tareas, y verifica exactamente 25 claves más 25 filas DLQ mediante `SELECT COUNT(*)`. Cada payload y clave usa su índice para detectar pérdida o sobrescritura accidental. Reutilizar `LifecycleEspía` y el esquema de Task 2; no usar sleeps aleatorios.

```python
async def test_cincuenta_escrituras_compartidas_persisten_sin_intercalación(tmp_path) -> None:
    db_path = tmp_path / "stress.db"
    lifecycle = DatabaseLifecycleManager(
        db_paths=[db_path],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )
    await lifecycle.init_all()
    agent = DatabaseAgent(str(db_path))
    agent._lifecycle = lifecycle
    agent._conn = await lifecycle.get_connection(db_path)
    await agent._conn.execute(
        "CREATE TABLE agent_memory (key TEXT PRIMARY KEY, value TEXT, updated_at REAL)"
    )
    await agent._conn.commit()
    dlq = DLQManager(db_path=db_path, handler_resolver={}.get, lifecycle=lifecycle)

    async def handler(event: Event) -> None:
        return None

    try:
        writes = [agent.set_memory(f"clave-{i}", {"i": i}) for i in range(25)]
        writes += [
            dlq.enqueue(
                Event(topic="stress", payload={"i": i}),
                handler,
                RuntimeError(str(i)),
            )
            for i in range(25)
        ]
        await asyncio.gather(*writes)
        conn = await lifecycle.get_connection(db_path)
        memory_count = (await (await conn.execute("SELECT COUNT(*) FROM agent_memory")).fetchone())[0]
        dlq_count = (await (await conn.execute("SELECT COUNT(*) FROM dead_letter_events")).fetchone())[0]
        assert (memory_count, dlq_count) == (25, 25)
    finally:
        await lifecycle.shutdown_all()
```

- [ ] **Step 2: Añadir estrés acotado de cinco ciclos EventBus**

Añadir una prueba que por cada ciclo hace `start()`, publica diez secuencias únicas, ejecuta `stop()` concurrentemente con la última publicación y al final exige `list(range(50))`, cola vacía, `_dispatch_task is None` y ningún task en `_pending_tasks`/`_dlq_tasks`. No usar tiempo real salvo `asyncio.wait_for(..., timeout=3)` como detector de deadlock del test.

- [ ] **Step 3: Ejecutar los dos archivos de regresión 20 veces**

Run:

```powershell
1..20 | ForEach-Object {
    E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_core_async_transactions.py tests\test_event_bus_shutdown.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Expected: 20 ejecuciones con exit code 0; cero timeout, cero pérdida de filas/eventos y cero warning de task destruida pendiente.

- [ ] **Step 4: Ejecutar toda la suite focalizada del core async**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests\test_core_async_transactions.py tests\test_event_bus_shutdown.py tests\test_core_event_bus.py tests\test_event_bus_require_dlq.py tests\test_event_bus_dlq_integration.py tests\test_event_bus_backpressure.py tests\test_dlq_manager.py tests\test_dlq_attempts_atomic.py tests\test_dlq_double_dispatch.py tests\test_db_lifecycle.py tests\test_conflict_persistence.py tests\test_record_conflict_persistence.py
```

Expected: PASS. Comparar warnings con el baseline documentado de 80 tests; cualquier warning nuevo de `aiosqlite` sobre loop cerrado bloquea la entrega.

- [ ] **Step 5: Ejecutar gates canónicos completos**

Run:

```powershell
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff check sky_claw\ tests\
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m ruff format --check sky_claw\ tests\
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m mypy sky_claw\
E:\Skyclaw_Main_Sync\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

Expected: los cuatro comandos terminan con exit code 0. Si falla algo fuera del corte, registrar archivo, prueba y evidencia y verificar si también falla en `origin/main`; no modificar código ajeno para ocultarlo.

- [ ] **Step 6: Revisar diff y ausencia de scope creep**

Run:

```powershell
git status --short
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- sky_claw/antigravity/core/db_lifecycle.py sky_claw/antigravity/core/database.py sky_claw/antigravity/core/dlq_manager.py sky_claw/antigravity/core/event_bus.py tests/test_core_async_transactions.py tests/test_event_bus_shutdown.py
```

Expected: `git diff --check` sin salida; cambios limitados a los seis archivos de código/test y los documentos de diseño/plan. Confirmar explícitamente que no se incorporaron los hallazgos ZAI diferidos (timeouts de handler, leases, retry ceiling o carrera `init_all/get_connection`).

- [ ] **Step 7: Commit de estrés y evidencia de regresión**

```powershell
git add tests/test_core_async_transactions.py tests/test_event_bus_shutdown.py
git commit -m "test: cubrir estrés del core async"
```

- [ ] **Step 8: Preparar entrega sin publicar cambios externos**

Run:

```powershell
git log --oneline --decorate origin/main..HEAD
git status --short --branch
```

Expected: rama `codex/fix-core-async-critical`, worktree limpio y cinco commits locales posteriores a `origin/main`: diseño, límite SQLite, migración de consumidores, lifecycle EventBus y estrés. No hacer push ni abrir PR sin autorización explícita del usuario.

---

## Criterios de parada OODA

- Si una regresión no falla antes del cambio, refutar si el escenario ya está corregido en el SHA actual; no modificar producción para satisfacer una prueba inválida.
- Si una cancelación deja commit/rollback activo al liberar el lock, detener la implementación SQLite: el contrato central todavía no es seguro.
- Si `stop()` requiere timeout para terminar en las pruebas deterministas, detener la implementación EventBus y localizar qué tarea no tiene dueño; no convertir el timeout en comportamiento productivo.
- Si la solución altera retornos públicos (`None`, `bool`, `int`) o el claim atómico DLQ, revertir únicamente el commit del subsistema afectado y rediseñar ese corte.
- Si los gates completos revelan una falla preexistente, comprobarla contra `origin/main` en un entorno aislado y reportarla separada; no mezclarla con esta remediación.

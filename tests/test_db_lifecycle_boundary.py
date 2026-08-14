"""Contrato conductual del boundary de conexiones compartidas (PR-1a).

La propiedad que estos tests defienden:

    Toda lectura o escritura de la conexión compartida de un path está
    serializada con toda mutación del registro de ese path.

Son tests A–J del preflight de PR-1a, más las secciones P1-B/H1 (init_all
idempotente por path, Casos 1–5) y P2 (shutdown incompleto rechaza la
reapertura, K1–K3). **La autoridad sobre comportamiento es la
carrera ejecutada, no una ancla sintáctica**: acá no se afirma "está protegido"
mirando el AST, se pone una operación en vuelo y se comprueba que el shutdown no
puede declararse completo. Política vigente desde la ronda de review de
#468/#469: AST = inventario, evento/barrera = comportamiento.

**Cómo se sincroniza, y por qué no hay sleeps.** Dos instrumentos, cada uno con
su significado, y ninguno mide tiempo:

* ``ceder()`` — ``await asyncio.sleep(0)`` repetido. No espera: sólo vacía la
  cola de listos del loop. Si la tarea bajo prueba *pudiera* avanzar, avanza
  ahí. Se usa para afirmar lo que NO pasó (``assert not t.done()``).
* ``asyncio.wait_for(..., timeout=N)`` — deadline de falla, no sincronización.
  El camino sano termina en milisegundos; si algo se autobloquea, el test falla
  en N segundos en vez de colgar la suite. Se usa para afirmar que algo SÍ
  termina.

La única forma de bloquear el manager por dentro sin tocar producción es la
fixture ``conexiones``, que espía ``aiosqlite.connect``: registra cada conexión
creada —para poder probar que ninguna quedó huérfana— y permite interponer una
puerta antes de delegar en la real.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import sky_claw.app.core.db_lifecycle as db_lifecycle
from sky_claw.app.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
    DatabaseLifecycleShuttingDownError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

# Deadline para "esto tiene que terminar". El camino sano tarda milisegundos;
# sólo un autobloqueo llega hasta acá.
DEADLINE = 5.0


async def ceder(veces: int = 30) -> None:
    """Cede el control ``veces`` veces para vaciar la cola de listos del loop.

    NO es un sleep de sincronización: ``asyncio.sleep(0)`` no espera tiempo, sólo
    reencola la corrutina actual al final de la cola. Todo lo que estuviera listo
    para correr, corre. Por eso un ``assert not tarea.done()`` después de esto
    significa "no pudo avanzar", no "todavía no le tocó".
    """
    for _ in range(veces):
        await asyncio.sleep(0)


@pytest.fixture
def ruta_db(tmp_path: Path) -> Path:
    """Path de la DB principal de cada test."""
    return tmp_path / "core.db"


@pytest.fixture
def gestor(ruta_db: Path) -> DatabaseLifecycleManager:
    """Manager con un solo path y sin efectos de proceso (señales/atexit)."""
    return DatabaseLifecycleManager(
        db_paths=[ruta_db],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )


@pytest.fixture
async def conexiones(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[aiosqlite.Connection]]:
    """Espía de ``aiosqlite.connect``: registra cada conexión y garantiza su cierre.

    La lista es la evidencia de los tests de fuga: una conexión abierta que el
    manager ya no tiene registrada es exactamente "nadie va a cerrarla". El
    teardown cierra lo que quede, para que un test que prueba una fuga no deje
    hilos aiosqlite no-daemon colgando la sesión entera.
    """
    creadas: list[aiosqlite.Connection] = []
    real = db_lifecycle.aiosqlite.connect

    async def espia(*args: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await real(*args, **kwargs)
        creadas.append(conn)
        return conn

    monkeypatch.setattr(db_lifecycle.aiosqlite, "connect", espia)
    yield creadas
    for conn in creadas:
        with contextlib.suppress(Exception):
            await conn.close()


async def esta_abierta(conn: aiosqlite.Connection) -> bool:
    """¿La conexión sigue usable? Se pregunta usándola, no mirando internals."""
    try:
        await conn.execute("SELECT 1")
    except ValueError:
        return False
    return True


def _sostener(
    gestor: DatabaseLifecycleManager,
    ruta: Path,
    adentro: asyncio.Event,
    soltar: asyncio.Event,
    orden: list[str] | None = None,
    etiqueta: str = "op",
):
    """Corrutina que entra al boundary de ``ruta`` y se queda hasta ``soltar``."""

    async def _operacion() -> None:
        async with gestor.operation(ruta):
            adentro.set()
            await soltar.wait()
        if orden is not None:
            orden.append(f"{etiqueta}_salio")

    return _operacion()


# ---------------------------------------------------------------------------
# A — trabajo admitido en vuelo bloquea el shutdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entrada",
    [
        pytest.param("operation", id="operation"),
        pytest.param("get_connection", id="get_connection"),
    ],
)
async def test_a_admitido_en_vuelo_bloquea_el_shutdown(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
    entrada: str,
) -> None:
    """El shutdown ESPERA lo ya admitido; no alcanza con rechazar lo nuevo.

    La entrada se toma en la posición donde el drenaje es lo ÚNICO que la
    sostiene: admitida, con la conexión todavía abriéndose, así que el registro
    está **vacío**. Sin drenaje, el shutdown ve cero conexiones, se declara
    completo, y la entrada registra después una conexión que ya nadie va a
    cerrar. Tomarla con la conexión ya registrada no probaría el drenaje: ahí
    el lock del path que toma el cierre alcanzaría para ordenar los dos, y el
    test pasaría igual con el drenaje removido.

    Se parametriza sobre los DOS puntos de entrada públicos que abren la
    conexión perezosamente: ``operation`` (que además toma el lock del path) y
    ``get_connection`` (que no). La carrera lazy-open es la misma para ambos:
    admitir → ``_resolve_connection`` → ``aiosqlite.connect`` suspendido. Quitar
    la admisión de cualquiera de los dos debe romper SU caso, no el del hermano.
    """
    puerta, abriendo = asyncio.Event(), asyncio.Event()
    connect_espiado = db_lifecycle.aiosqlite.connect

    async def connect_lenta(*args: object, **kwargs: object) -> aiosqlite.Connection:
        abriendo.set()
        await puerta.wait()
        return await connect_espiado(*args, **kwargs)

    monkeypatch.setattr(db_lifecycle.aiosqlite, "connect", connect_lenta)

    orden: list[str] = []

    async def admitir() -> None:
        # Sin init_all previo: la conexión se abre perezosamente acá adentro.
        if entrada == "operation":
            async with gestor.operation(ruta_db):
                pass
        else:
            await gestor.get_connection(ruta_db)
        orden.append("entrada_salio")

    async def apagar() -> None:
        await gestor.shutdown_all()
        orden.append("shutdown_termino")

    t_op = asyncio.create_task(admitir())
    await abriendo.wait()
    assert gestor._connections == {}, "el registro debía estar vacío: si no, lo que ordena es el lock, no el drenaje"

    t_sd = asyncio.create_task(apagar())
    await ceder()
    assert not t_sd.done(), "el shutdown se declaró completo con trabajo admitido en vuelo"

    puerta.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)

    assert orden == ["entrada_salio", "shutdown_termino"], f"el shutdown no esperó al trabajo admitido: {orden}"
    assert conexiones, "el test no ejercitó ninguna conexión real"
    assert not await esta_abierta(conexiones[0]), "la conexión que registró la entrada quedó viva tras el shutdown"


# ---------------------------------------------------------------------------
# B — no se admite trabajo nuevo después de SHUTTING_DOWN
# ---------------------------------------------------------------------------


async def test_b_admision_rechazada_una_vez_que_el_shutdown_empezo(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Quien llega tarde es RECHAZADO, no encolado.

    Encolarlo sería peor que rechazarlo: la operación se reanudaría después del
    cierre y reabriría una DB que ya nadie va a cerrar.
    """
    await gestor.init_all()
    creadas_antes = len(conexiones)
    adentro, soltar = asyncio.Event(), asyncio.Event()

    t_op = asyncio.create_task(_sostener(gestor, ruta_db, adentro, soltar))
    await adentro.wait()
    t_sd = asyncio.create_task(gestor.shutdown_all())
    await ceder()
    assert not t_sd.done(), "el shutdown terminó antes de que se pudiera probar la puerta cerrada"

    # La puerta ya está cerrada: las TRES vías públicas de admisión tienen que
    # rechazar. Se enumeran por comportamiento, ejecutando cada API de verdad:
    # get_connection, operation y checkpoint_all. Un ancla sintáctica no
    # alcanzaría — si a una de ellas se le quita la admisión, SU caso debe
    # ponerse rojo (reversión: quitar la admisión de checkpoint_all).
    intento_get = asyncio.create_task(gestor.get_connection(ruta_db))
    await ceder()
    assert intento_get.done(), "get_connection() quedó encolado en vez de fallar: resucitaría la DB después del cierre"
    with pytest.raises(DatabaseLifecycleShuttingDownError):
        intento_get.result()

    async def entrar_por_operation() -> None:
        async with gestor.operation(ruta_db):
            pytest.fail("operation() admitió trabajo con el shutdown en curso")

    intento_op = asyncio.create_task(entrar_por_operation())
    await ceder()
    assert intento_op.done(), "operation() quedó encolada en vez de fallar"
    with pytest.raises(DatabaseLifecycleShuttingDownError):
        intento_op.result()

    intento_cp = asyncio.create_task(gestor.checkpoint_all())
    await ceder()
    assert intento_cp.done(), "checkpoint_all() quedó encolado en vez de fallar"
    with pytest.raises(DatabaseLifecycleShuttingDownError):
        intento_cp.result()

    assert len(conexiones) == creadas_antes, "un intento rechazado igual abrió una conexión"

    soltar.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)


# ---------------------------------------------------------------------------
# C — una init en vuelo bloquea el shutdown (D aísla el transition lock)
# ---------------------------------------------------------------------------


async def test_c_init_en_vuelo_bloquea_el_shutdown(
    gestor: DatabaseLifecycleManager,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una ``init_all()`` en vuelo bloquea a un ``shutdown_all()`` concurrente.

    Esa —y sólo esa— es la propiedad que C demuestra. El mecanismo que la
    produce con el código actual es el ``_transition_lock``: ``init_all()`` lo
    sostiene durante toda la reapertura, así que el shutdown no alcanza el
    drenaje hasta que la init termina y lo libera.

    Pero C NO aísla ese mecanismo por sí solo, y la matriz de reversiones lo
    muestra: quitar SOLO la admisión (``_admit``/``_release``) conservando el
    lock deja C y D verdes; quitar SOLO la serialización del transition lock
    rompe D pero NO C —la admisión, todavía presente, cubre a C de forma
    redundante—; sólo quitar AMBOS mecanismos rompe C. Por eso el test que
    discrimina específicamente la serialización por ``_transition_lock`` es
    **D** (ver su reversión dedicada), no C.
    """
    puerta, entro_init = asyncio.Event(), asyncio.Event()
    connect_espiado = db_lifecycle.aiosqlite.connect

    async def connect_lenta(*args: object, **kwargs: object) -> aiosqlite.Connection:
        entro_init.set()
        await puerta.wait()
        return await connect_espiado(*args, **kwargs)

    monkeypatch.setattr(db_lifecycle.aiosqlite, "connect", connect_lenta)

    orden: list[str] = []

    async def iniciar() -> None:
        await gestor.init_all()
        orden.append("init_termino")

    async def apagar() -> None:
        await gestor.shutdown_all()
        orden.append("shutdown_termino")

    t_init = asyncio.create_task(iniciar())
    await entro_init.wait()

    t_sd = asyncio.create_task(apagar())
    await ceder()
    assert not t_sd.done(), "el shutdown se declaró completo con una init suspendida a mitad de camino"

    puerta.set()
    await asyncio.wait_for(t_init, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)

    assert orden == ["init_termino", "shutdown_termino"], f"el shutdown no esperó a la init admitida: {orden}"
    assert conexiones, "la init no llegó a abrir ninguna conexión"
    assert not await esta_abierta(conexiones[0]), "el shutdown no cerró la conexión que registró la init"


# ---------------------------------------------------------------------------
# D — init_all no se cuela en la ventana de drenaje (BLOCKER 1a-1)
# ---------------------------------------------------------------------------


async def test_d_init_all_no_se_cuela_durante_el_shutdown(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La carrera de BLOCKER 1a-1, ejecutada.

    Sin la transición serializada: el shutdown drena, ``init_all`` se cuela y
    REEMPLAZA la conexión registrada, el shutdown cierra el reemplazo y la
    original queda abierta para siempre. La invariante que lo delata no es
    "cuántas conexiones hay" sino **que no exista una conexión viva que el
    manager ya no conozca**: ésa es, literalmente, la que nadie va a cerrar.
    """
    await gestor.init_all()
    primera = conexiones[0]
    adentro, soltar = asyncio.Event(), asyncio.Event()

    t_op = asyncio.create_task(_sostener(gestor, ruta_db, adentro, soltar))
    await adentro.wait()

    t_sd = asyncio.create_task(gestor.shutdown_all())
    await ceder()
    assert not t_sd.done(), "el shutdown no llegó a quedar bloqueado en el drenaje"

    # Puerta sobre connect: la señal determinista de que la reapertura se coló
    # no es "terminó" —abrir una conexión real tarda un viaje al hilo worker y
    # dejaría la tarea pendiente igual— sino que HAYA LLEGADO A ABRIR una.
    puerta, reabriendo = asyncio.Event(), asyncio.Event()
    connect_espiado = db_lifecycle.aiosqlite.connect

    async def connect_vigilada(*args: object, **kwargs: object) -> aiosqlite.Connection:
        reabriendo.set()
        await puerta.wait()
        return await connect_espiado(*args, **kwargs)

    monkeypatch.setattr(db_lifecycle.aiosqlite, "connect", connect_vigilada)

    # La reapertura corre la carrera contra la ventana de drenaje.
    t_reinit = asyncio.create_task(gestor.init_all())
    await ceder()
    assert not t_reinit.done(), "init_all() se coló en la ventana de drenaje del shutdown"
    assert not reabriendo.is_set(), "init_all() abrió una conexión nueva mientras el shutdown drenaba"

    puerta.set()
    soltar.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    await asyncio.wait_for(t_reinit, timeout=DEADLINE)

    assert not await esta_abierta(primera), "el shutdown no cerró la conexión que existía antes de la reapertura"

    registradas = set(gestor._connections.values())
    huerfanas = [c for c in conexiones if await esta_abierta(c) and c not in registradas]
    assert not huerfanas, (
        f"{len(huerfanas)} conexión(es) viva(s) que el manager ya no tiene registradas: nadie las cierra"
    )

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


# ---------------------------------------------------------------------------
# E — checkpoint_all respeta el boundary por path
# ---------------------------------------------------------------------------


async def test_e_checkpoint_all_respeta_el_lock_del_path(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """El checkpoint periódico emite SQL sobre la conexión compartida: participa.

    Y el checkpoint que corre DENTRO del shutdown no puede re-adquirir el lock
    que el drenaje ya garantizó, o el apagado se autobloquea. Además, ese
    checkpoint interno SÍ debe emitir su ``PRAGMA wal_checkpoint(TRUNCATE)``:
    el spy se limpia antes del shutdown para observarlo a él y no al público.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)

    # Lo observable es EL SQL EMITIDO, no que la tarea del checkpoint siga
    # pendiente: un `execute` que ya salió viaja por el hilo worker de aiosqlite
    # y deja la tarea pendiente un rato aunque el lock nunca lo haya frenado.
    # Registrar en la llamada —no al completar— es lo que distingue "no corrió"
    # de "corrió y todavía está volviendo".
    sentencias: list[str] = []
    execute_real = conn.execute

    def execute_espiado(sql: str, *args: object, **kwargs: object) -> object:
        sentencias.append(str(sql))
        return execute_real(sql, *args, **kwargs)

    conn.execute = execute_espiado  # type: ignore[method-assign]

    adentro, soltar = asyncio.Event(), asyncio.Event()
    t_op = asyncio.create_task(_sostener(gestor, ruta_db, adentro, soltar))
    await adentro.wait()

    t_cp = asyncio.create_task(gestor.checkpoint_all())
    await ceder()
    assert not any("wal_checkpoint" in s.lower() for s in sentencias), (
        f"checkpoint_all() emitió SQL sobre la conexión compartida con el lock del path tomado: {sentencias}"
    )
    assert not t_cp.done(), "el checkpoint terminó con el lock del path tomado por otra operación"

    soltar.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    resultados = await asyncio.wait_for(t_cp, timeout=DEADLINE)
    assert any("wal_checkpoint" in s.lower() for s in sentencias), "el checkpoint nunca llegó a emitir su SQL"
    assert str(ruta_db.resolve()) in resultados, f"el checkpoint no reportó el path esperado: {resultados}"

    # El checkpoint interno del shutdown NO vuelve a tomar el lock, y SÍ debe
    # ejecutar su TRUNCATE. Limpiar el spy antes distingue el checkpoint público
    # (ya observado arriba) del interno: si el shutdown cambiara a usar la
    # pública, ``_admit()`` rechazaría por SHUTTING_DOWN y el ``except`` amplio
    # se tragaría la excepción, dejando este assert sin TRUNCATE y a E verde.
    sentencias.clear()
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)
    assert any("wal_checkpoint" in s.lower() and "truncate" in s.lower() for s in sentencias), (
        f"el shutdown no ejecutó su checkpoint interno TRUNCATE: {sentencias}"
    )
    assert conexiones, "el test no ejercitó ninguna conexión real"


# ---------------------------------------------------------------------------
# F — el boundary es por path, no global
# ---------------------------------------------------------------------------


async def test_f_paths_distintos_no_se_bloquean_entre_si(
    tmp_path: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Un lock global serializaría DBs que no comparten una sola página."""
    ruta_a, ruta_b = tmp_path / "a.db", tmp_path / "b.db"
    gestor = DatabaseLifecycleManager(
        db_paths=[ruta_a, ruta_b],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )
    await gestor.init_all()

    adentro_a, soltar_a = asyncio.Event(), asyncio.Event()
    t_a = asyncio.create_task(_sostener(gestor, ruta_a, adentro_a, soltar_a))
    await adentro_a.wait()

    async def operar_sobre_b() -> None:
        # Entrar y salir del boundary, sin SQL: lo que se mide es el lock, y un
        # `execute` real haría un round-trip al hilo worker de aiosqlite que
        # `ceder()` —que sólo vacía la cola del loop— no puede esperar.
        async with gestor.operation(ruta_b):
            pass

    t_b = asyncio.create_task(operar_sobre_b())
    await ceder()
    assert t_b.done(), "una operación sobre otro path quedó bloqueada: el lock es global, no por path"
    t_b.result()

    soltar_a.set()
    await asyncio.wait_for(t_a, timeout=DEADLINE)
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


# ---------------------------------------------------------------------------
# G — get_connection no resucita un manager cerrado
# ---------------------------------------------------------------------------


async def test_g_get_connection_no_resucita_un_manager_cerrado(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Después del cierre terminal no hay resurrección implícita.

    Devolver una conexión acá reabriría una DB que el shutdown ya cerró, y esa
    reapertura no la cierra nadie: el shutdown ya pasó.
    """
    await gestor.init_all()
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)
    creadas_tras_cierre = len(conexiones)

    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await gestor.get_connection(ruta_db)

    async def entrar_por_operation() -> None:
        async with gestor.operation(ruta_db):
            pytest.fail("operation() abrió una conexión sobre un manager cerrado")

    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await entrar_por_operation()

    assert len(conexiones) == creadas_tras_cierre, "se abrió una conexión nueva sobre un manager cerrado"
    assert gestor._connections == {}, "el registro quedó con conexiones después del cierre terminal"


# ---------------------------------------------------------------------------
# H — init_all es la reapertura explícita
# ---------------------------------------------------------------------------


async def test_h_init_all_reabre_un_manager_cerrado(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """CLOSED → ``init_all()`` → RUNNING. La reapertura existe, pero es explícita."""
    await gestor.init_all()
    primera = await gestor.get_connection(ruta_db)
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)

    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await gestor.get_connection(ruta_db)

    await asyncio.wait_for(gestor.init_all(), timeout=DEADLINE)

    segunda = await gestor.get_connection(ruta_db)
    assert segunda is not primera, "la reapertura devolvió la conexión vieja"
    assert await esta_abierta(segunda), "la conexión reabierta no es usable"
    assert not await esta_abierta(primera), "la conexión previa al cierre seguía viva"

    # Y el ciclo vuelve a cerrar: la reapertura no dejó el estado a medio camino.
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)
    assert not await esta_abierta(segunda), "el segundo shutdown no cerró la conexión reabierta"


# ---------------------------------------------------------------------------
# I — el rollback de transaction no se autobloquea
# ---------------------------------------------------------------------------


async def test_i_el_rollback_fallido_no_reintenta_el_lock_del_path(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """``transaction`` → ``_rollback_after_failure`` → ``_quarantine_connection``.

    Ese camino corre **ya sosteniendo** el lock del path. Es la única excepción
    interna del contrato, y sólo es válida si ninguno de esos tres vuelve a
    pedirlo: si lo pidiera, la transacción fallida colgaría para siempre en vez
    de propagar el error.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)

    async def rollback_que_falla() -> None:
        raise sqlite3.OperationalError("rollback falló a propósito")

    monkeypatched = rollback_que_falla
    conn.rollback = monkeypatched  # type: ignore[method-assign]

    async def transaccion_que_falla() -> None:
        async with gestor.transaction(ruta_db):
            raise RuntimeError("error de la operación")

    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(transaccion_que_falla(), timeout=DEADLINE)

    # La conexión dañada se retira del registro (cuarentena), sin adelantar el
    # contrato de invalidación de PR-2.
    assert str(ruta_db.resolve()) not in gestor._connections, "la conexión en cuarentena siguió registrada"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


# ---------------------------------------------------------------------------
# J — la cancelación deja la admisión coherente
# ---------------------------------------------------------------------------


async def test_j_la_cancelacion_no_deja_admisiones_colgadas(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Se cancela en las dos posiciones que importan, no en la cómoda.

    La discriminante es la segunda: una operación **admitida pero todavía
    encolada en el lock** del path. Si el release viviera en el camino normal en
    vez del ``finally``, esa cancelación dejaría el contador arriba para
    siempre y ningún shutdown volvería a completarse.
    """
    await gestor.init_all()

    # Posición 1: cancelada SOSTENIENDO el lock.
    adentro, nunca = asyncio.Event(), asyncio.Event()
    t1 = asyncio.create_task(_sostener(gestor, ruta_db, adentro, nunca))
    await adentro.wait()
    assert gestor._admitted == 1
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1
    assert gestor._admitted == 0, "una operación cancelada sosteniendo el lock no liberó su admisión"
    assert gestor._drained.is_set(), "el drenaje quedó cerrado después de cancelar"

    # Posición 2 (discriminante): cancelada ESPERANDO el lock.
    adentro2, soltar2 = asyncio.Event(), asyncio.Event()
    t_dueño = asyncio.create_task(_sostener(gestor, ruta_db, adentro2, soltar2))
    await adentro2.wait()

    encolada, jamas = asyncio.Event(), asyncio.Event()
    t2 = asyncio.create_task(_sostener(gestor, ruta_db, encolada, jamas))
    await ceder()
    assert not t2.done(), "la segunda operación no quedó encolada en el lock"
    assert not encolada.is_set(), "la segunda operación entró al boundary con el lock tomado"
    assert gestor._admitted == 2, "una operación encolada en el lock tiene que contar como admitida"

    t2.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t2
    assert gestor._admitted == 1, "la cancelación de una operación encolada no liberó su admisión"

    soltar2.set()
    await asyncio.wait_for(t_dueño, timeout=DEADLINE)
    assert gestor._admitted == 0
    assert gestor._drained.is_set()

    # La prueba conductual: con la contabilidad rota, esto no volvería nunca.
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


# ---------------------------------------------------------------------------
# P1-B / H1 — init_all idempotente por path (singleton-open bajo el boundary)
# ---------------------------------------------------------------------------


async def test_p1b_init_all_running_no_reemplaza_conexion(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Caso 1 — RUNNING, path ya abierto: init_all conserva la conexión.

    El comportamiento viejo abría una conexión nueva y reemplazaba la
    registrada, dejando a la original viva y fuera del registro: nadie la
    cerraba. La reversión R-B1 (init_all → ``_init_single`` directo, sin
    singleton) debe romper este test.
    """
    await gestor.init_all()
    primera = await gestor.get_connection(ruta_db)
    creadas_antes = len(conexiones)

    await asyncio.wait_for(gestor.init_all(), timeout=DEADLINE)

    segunda = await gestor.get_connection(ruta_db)
    assert segunda is primera, "init_all() reemplazó la conexión registrada estando RUNNING"
    assert len(conexiones) == creadas_antes, "init_all() abrió una conexión nueva sobre un path ya inicializado"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)
    assert not await esta_abierta(primera), "el shutdown no cerró la conexión conservada"


async def test_p1b_init_all_espera_el_boundary_del_path(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Caso 2 — operation en vuelo: init_all espera el lock del path y no reemplaza.

    La reapertura participa del mismo boundary por path que ``operation()`` y el
    cierre del shutdown: con una operación sosteniendo el lock del path,
    ``init_all()`` debe esperarla y recién entonces re-chequear. La reversión
    R-B2 (init_all con ``_init_lock`` pero SIN el lock del path) rompe la
    espera: init_all terminaría con la operación todavía en vuelo.
    """
    await gestor.init_all()
    primera = await gestor.get_connection(ruta_db)
    creadas_antes = len(conexiones)

    adentro, soltar = asyncio.Event(), asyncio.Event()
    t_op = asyncio.create_task(_sostener(gestor, ruta_db, adentro, soltar))
    await adentro.wait()

    t_init = asyncio.create_task(gestor.init_all())
    await ceder()
    assert not t_init.done(), "init_all() no esperó el lock del path tomado por la operación en vuelo"

    soltar.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_init, timeout=DEADLINE)

    registrada = await gestor.get_connection(ruta_db)
    assert registrada is primera, "init_all() reemplazó la conexión tras esperar a la operación"
    assert len(conexiones) == creadas_antes, "init_all() abrió otra conexión pese al re-check"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


async def test_p1b_init_all_inicializa_solo_paths_nuevos(
    tmp_path: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Caso 5 — path nuevo mientras RUNNING: idempotente ≠ "no hacer nada".

    A ya inicializado NO cambia de identidad; B (registrado después con
    ``add_db_path``) SÍ se inicializa. Eso distingue idempotencia de un
    early-return por "ya estoy RUNNING".
    """
    ruta_a = tmp_path / "a.db"
    ruta_b = tmp_path / "b.db"
    gestor = DatabaseLifecycleManager(
        db_paths=[ruta_a],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )
    await gestor.init_all()
    conn_a = await gestor.get_connection(ruta_a)
    creadas = len(conexiones)

    gestor.add_db_path(ruta_b)
    await asyncio.wait_for(gestor.init_all(), timeout=DEADLINE)

    assert gestor._connections[str(ruta_a.resolve())] is conn_a, "init_all() reemplazó el path ya inicializado"
    assert str(ruta_b.resolve()) in gestor._connections, "init_all() no inicializó el path nuevo"
    assert len(conexiones) == creadas + 1, "se abrió más de una conexión nueva"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


async def test_h1_lazy_open_no_puede_convivir_con_init_all(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caso 3 — H1: lazy-open concurrente con init_all. Una sola apertura gana.

    El lazy-open (``get_connection``) llega primero y queda suspendido
    abriendo, con ``_init_lock`` tomado y el registro vacío. ``init_all()``
    debe esperar el mismo singleton y REUSAR la conexión que registró el
    lazy-open: ``connect`` count == 1 y la conexión entregada es exactamente
    la registrada. La reversión R-B3 (quitar el re-check dentro de
    ``_init_lock``) permite la segunda apertura y rompe este test.
    """
    puerta, abriendo = asyncio.Event(), asyncio.Event()
    connect_espiado = db_lifecycle.aiosqlite.connect
    contador = {"n": 0}

    async def connect_vigilada(*args: object, **kwargs: object) -> aiosqlite.Connection:
        contador["n"] += 1
        abriendo.set()
        await puerta.wait()
        return await connect_espiado(*args, **kwargs)

    monkeypatch.setattr(db_lifecycle.aiosqlite, "connect", connect_vigilada)

    t_get = asyncio.create_task(gestor.get_connection(ruta_db))
    await abriendo.wait()
    assert gestor._connections == {}, "el registro debía estar vacío durante el lazy-open"

    t_init = asyncio.create_task(gestor.init_all())
    await ceder()
    assert not t_init.done(), "init_all() abrió mientras el lazy-open estaba a mitad de camino"

    puerta.set()
    conn_get = await asyncio.wait_for(t_get, timeout=DEADLINE)
    await asyncio.wait_for(t_init, timeout=DEADLINE)

    assert contador["n"] == 1, f"se abrieron {contador['n']} conexiones para un solo path"
    registrada = gestor._connections[str(ruta_db.resolve())]
    assert registrada is conn_get, "la conexión entregada por el lazy-open no es la registrada"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


async def test_h1_init_all_gana_y_el_lazy_open_reutiliza(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caso 4 — H1 inverso: init_all abre primero; el lazy-open reutiliza.

    ``connect`` count == 1 y el lazy-open entrega exactamente la conexión que
    ``init_all()`` registró.
    """
    puerta, abriendo = asyncio.Event(), asyncio.Event()
    connect_espiado = db_lifecycle.aiosqlite.connect
    contador = {"n": 0}

    async def connect_vigilada(*args: object, **kwargs: object) -> aiosqlite.Connection:
        contador["n"] += 1
        abriendo.set()
        await puerta.wait()
        return await connect_espiado(*args, **kwargs)

    monkeypatch.setattr(db_lifecycle.aiosqlite, "connect", connect_vigilada)

    t_init = asyncio.create_task(gestor.init_all())
    await abriendo.wait()
    assert gestor._connections == {}, "el registro debía estar vacío mientras init_all abría"

    t_get = asyncio.create_task(gestor.get_connection(ruta_db))
    await ceder()
    assert not t_get.done(), "el lazy-open no quedó esperando a init_all"

    puerta.set()
    await asyncio.wait_for(t_init, timeout=DEADLINE)
    conn_get = await asyncio.wait_for(t_get, timeout=DEADLINE)

    assert contador["n"] == 1, f"se abrieron {contador['n']} conexiones para un solo path"
    registrada = gestor._connections[str(ruta_db.resolve())]
    assert registrada is conn_get, "el lazy-open entregó una conexión distinta de la registrada"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


# ---------------------------------------------------------------------------
# P2 — shutdown incompleto rechaza la reapertura (guard fail-closed)
# ---------------------------------------------------------------------------


async def _dejar_shutdown_incompleto(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[aiosqlite.Connection, object]:
    """Deja al manager en shutdown incompleto; retorna (conexión retenida, close real).

    El fault injection respeta la semántica real de ``aiosqlite.close()``: el
    ``finally`` de aiosqlite deja ``_connection = None`` aunque el cierre falle,
    así que la conexión retenida queda MUERTA. Es el estado exacto del P2.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)
    real_close = conn.close

    async def close_que_falla() -> None:
        await real_close()
        raise sqlite3.OperationalError("close falló a propósito")

    monkeypatch.setattr(conn, "close", close_que_falla)
    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)

    assert gestor._connections.get(str(ruta_db.resolve())) is conn, "el shutdown incompleto no retuvo la entrada"
    assert gestor._shutting_down and not gestor._closed
    return conn, real_close


async def test_k1_shutdown_incompleto_rechaza_el_reopen(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K1 — tras un shutdown incompleto, init_all() DEBE fallar explícitamente.

    Sin el guard, la idempotencia reutilizaría la entrada retenida —una
    conexión muerta— y el manager entregaría un objeto que falla toda query.
    El guard no debe abrir una conexión nueva ni tocar el estado de shutdown:
    la recuperación es reintentar ``shutdown_all()``, no reabrir.
    """
    conn_a, _ = await _dejar_shutdown_incompleto(gestor, ruta_db, monkeypatch)
    creadas_antes = len(conexiones)

    with pytest.raises(DatabaseLifecycleShuttingDownError):
        await asyncio.wait_for(gestor.init_all(), timeout=DEADLINE)

    assert len(conexiones) == creadas_antes, "el guard abrió una conexión nueva"
    assert gestor._connections.get(str(ruta_db.resolve())) is conn_a, "el guard tocó la entrada retenida"
    assert gestor._shutting_down, "el guard limpió el estado de shutdown"
    assert not gestor._closed, "el guard declaró CLOSED sin completar el cierre"


async def test_k2_retry_de_shutdown_completa_el_cierre(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K2 — liberado el fault injection, reintentar shutdown_all() SÍ completa.

    El contrato "shutdown incompleto es reintentable" no debe ser destruido por
    el guard: el segundo close es idempotente en aiosqlite (la conexión ya está
    muerta) y el shutdown termina en CLOSED con el registro vacío.
    """
    conn_a, real_close = await _dejar_shutdown_incompleto(gestor, ruta_db, monkeypatch)
    monkeypatch.setattr(conn_a, "close", real_close)

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)

    assert gestor._connections == {}, "el retry no vació el registro"
    assert gestor._closed, "el retry no llegó a CLOSED"


async def test_k3_tras_el_retry_si_puede_reabrir(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K3 — completado el retry (CLOSED), init_all() reabre una conexión VÁLIDA.

    No alcanza con que init_all no lance: la conexión reabierta debe ejecutar
    una query real.
    """
    conn_a, real_close = await _dejar_shutdown_incompleto(gestor, ruta_db, monkeypatch)
    monkeypatch.setattr(conn_a, "close", real_close)
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)
    assert gestor._closed

    await asyncio.wait_for(gestor.init_all(), timeout=DEADLINE)
    conn_b = await gestor.get_connection(ruta_db)
    assert conn_b is not conn_a, "la reapertura reutilizó la conexión muerta del shutdown incompleto"

    async with conn_b.execute("SELECT 1") as cursor:
        row = await cursor.fetchone()
        assert row[0] == 1, "la conexión reabierta no ejecuta queries"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)

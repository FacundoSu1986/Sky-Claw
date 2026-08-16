"""Contrato de invalidación compartida de conexiones (PR-2).

La propiedad que estos tests defienden, en una línea:

    Una conexión lifecycle-owned nunca queda físicamente viva, ausente del
    registro y sin nadie responsable de cerrarla; y mientras se la invalida,
    ningún camino hermano puede recibirla ni abrir un reemplazo sobre el
    archivo que está por moverse.

Por qué hacía falta un contrato y no un parche. Había DOS caminos que hacían
"cerrar + mutar el registro" con políticas opuestas: ``shutdown_all()`` retiraba
la entrada sólo si el cierre se confirmaba —y por eso era reintentable— mientras
que la cuarentena retiraba primero y cerraba después. Cuando el cierre fallaba,
el segundo camino dejaba el fd vivo, el registro vacío y a ``shutdown_all()``
declarando un cierre completo que nunca ocurrió. El fix no es "arreglar la
cuarentena": es que las dos rutas compartan el mismo mecanismo, con la misma
regla de ownership.

**Qué se mide.** Los asserts miran comportamiento observable —si un ``SELECT``
funciona, si el shutdown puede reintentar, qué archivo existe en disco, qué
conexión devuelve el próximo resolve— y usan internals sólo como complemento.
La política de #468/#469 sigue vigente: AST = inventario, evento/barrera =
comportamiento.

**Cómo se sincroniza.** Sólo ``asyncio.Event`` como barrera. ``wait_for`` es
deadline de falla, nunca sincronización: el camino sano tarda milisegundos y el
timeout sólo evita que un autobloqueo cuelgue la suite. ``ceder()`` es
``asyncio.sleep(0)`` repetido —no espera tiempo, vacía la cola de listos— y se
usa para afirmar que algo NO pudo avanzar.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import sky_claw.app.core.db_lifecycle as db_lifecycle
from sky_claw.app.core.db_lifecycle import (
    DatabaseConnectionQuarantinedError,
    DatabaseConnectionReplacedError,
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from sky_claw.app.db.async_registry import AsyncModRegistry, _DatabaseCorruptionError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import aiosqlite

DEADLINE = 5.0


async def ceder(veces: int = 30) -> None:
    """Vacía la cola de listos del loop. No espera tiempo."""
    for _ in range(veces):
        await asyncio.sleep(0)


@pytest.fixture
def ruta_db(tmp_path: Path) -> Path:
    return tmp_path / "quarantine.db"


@pytest.fixture
def gestor(ruta_db: Path) -> DatabaseLifecycleManager:
    return DatabaseLifecycleManager(
        db_paths=[ruta_db],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )


@pytest.fixture
async def conexiones(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[aiosqlite.Connection]]:
    """Espía de ``aiosqlite.connect``: registra cada conexión y cierra las que queden.

    La lista es la evidencia de fuga: una conexión abierta que el manager ya no
    tiene registrada es exactamente "nadie va a cerrarla". El teardown cierra lo
    que sobre para no dejar hilos aiosqlite no-daemon colgando la sesión.
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


async def esta_usable(conn: aiosqlite.Connection) -> bool:
    """¿La conexión sirve? Se pregunta usándola, no mirando internals."""
    try:
        async with conn.execute("SELECT 1") as cur:
            await cur.fetchone()
    except BaseException:  # noqa: BLE001 — cualquier fallo significa inservible
        return False
    return True


def _romper_rollback(conn: aiosqlite.Connection) -> None:
    async def rollback_que_falla() -> None:
        raise sqlite3.OperationalError("rollback falló a propósito")

    conn.rollback = rollback_que_falla  # type: ignore[method-assign]


async def _transaccion_que_falla(gestor: DatabaseLifecycleManager, ruta: Path) -> None:
    async with gestor.transaction(ruta):
        raise RuntimeError("error de la operación")


class _CursorCorrupto:
    """Cursor cuyo ``quick_check`` responde algo distinto de ``ok``."""

    async def __aenter__(self) -> _CursorCorrupto:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def fetchone(self) -> tuple[str]:
        return ("corrupto",)


def _fingir_corrupcion(conn: aiosqlite.Connection) -> None:
    """Hace que el PRIMER ``PRAGMA quick_check`` de ``conn`` reporte corrupción."""
    ejecutar_real = conn.execute
    disparado = {"si": False}

    def execute_espia(sql: object, *a: object, **k: object) -> object:
        if "quick_check" in str(sql) and not disparado["si"]:
            disparado["si"] = True
            return _CursorCorrupto()
        return ejecutar_real(sql, *a, **k)

    conn.execute = execute_espia  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# A — OWNERSHIP: el pop va DESPUÉS del cierre confirmado
# ---------------------------------------------------------------------------


async def test_a1_cierre_confirmado_retira_la_entrada(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Control: si el cierre SÍ termina, la instancia se retira y el path queda libre."""
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)
    _romper_rollback(conn)

    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(_transaccion_que_falla(gestor, ruta_db), timeout=DEADLINE)

    assert str(ruta_db.resolve()) not in gestor._connections, "la conexión cerrada siguió registrada"
    assert not await esta_usable(conn), "la conexión retirada seguía viva"
    # El path NO quedó en cuarentena: un cierre confirmado no bloquea nada.
    async with gestor.operation(ruta_db) as fresca:
        assert fresca is not conn, "el resolve devolvió la conexión ya cerrada"
        assert await esta_usable(fresca)

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


async def test_a2_cierre_fallido_conserva_ownership_y_permite_reintento(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """REV-OWNERSHIP / REV-CLOSE. El discriminante del P1.

    Con el ``pop`` antes del cierre —la forma revertida— la conexión desaparece
    del registro con su fd todavía vivo y ``shutdown_all()`` se declara completo:
    nadie puede volver a intentar cerrarla nunca. Acá se exige lo contrario, y se
    exige con el comportamiento del shutdown, no sólo con el dict.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)
    clave = str(ruta_db.resolve())
    close_real = conn.close
    fallar_cierre = True

    async def close_que_falla() -> None:
        # Falla ANTES de tocar el sqlite3 subyacente: la conexión queda viva.
        # Es el caso que importa —"close lanzó" no implica "quedó cerrada"—.
        if fallar_cierre:
            raise sqlite3.OperationalError("close falló a propósito")
        await close_real()

    conn.close = close_que_falla  # type: ignore[method-assign]
    _romper_rollback(conn)

    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(_transaccion_que_falla(gestor, ruta_db), timeout=DEADLINE)

    # 1. Ownership retenido: hay alguien responsable de esa conexión.
    assert gestor._connections.get(clave) is conn, "se perdió el ownership de una conexión sin cerrar"
    assert clave in gestor.managed_paths, "managed_paths dejó de reportar la conexión retenida"
    assert await esta_usable(conn), "precondición: el fault injection debía dejarla viva"

    # 2. El shutdown la VE y no puede declararse completo mientras siga abierta.
    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)
    assert not gestor._closed, "el shutdown se declaró completo con una conexión sin cerrar"
    assert gestor._connections.get(clave) is conn, "el shutdown fallido soltó el ownership"

    # 3. Liberado el fallo, el reintento SÍ cierra: la retención era reintentable.
    fallar_cierre = False
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)
    assert gestor.managed_paths == [], "el reintento no completó el cierre"
    assert gestor._closed, "el reintento no llegó a CLOSED"


# ---------------------------------------------------------------------------
# B — FAIL-CLOSED: retener sin marcar sólo cambia una fuga por un veneno
# ---------------------------------------------------------------------------


async def test_b1_path_retenido_no_entrega_la_conexion_danada(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """REV-POISON.

    Sin la marca fail-closed por path, retener el ownership hace que el próximo
    ``operation()`` reciba la entrada retenida —una conexión que el manager ya
    decidió invalidar— y toda query falle. Retener y marcar son la MISMA
    decisión, no dos.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)
    close_real = conn.close

    async def close_que_falla() -> None:
        # El cierre real corre: la conexión queda MUERTA pero no confirmada,
        # que es la variante que un simple "¿anda?" no distingue.
        await close_real()
        raise sqlite3.OperationalError("close falló a propósito")

    conn.close = close_que_falla  # type: ignore[method-assign]
    _romper_rollback(conn)

    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(_transaccion_que_falla(gestor, ruta_db), timeout=DEADLINE)

    creadas_antes = len(conexiones)

    # El manager sigue RUNNING; un hermano pide el path.
    with pytest.raises(DatabaseConnectionQuarantinedError):
        async with gestor.operation(ruta_db):
            pass

    assert len(conexiones) == creadas_antes, "el path en cuarentena abrió un reemplazo por su cuenta"
    # Y tampoco lo entrega por get_connection(), que es la otra puerta.
    with pytest.raises(DatabaseConnectionQuarantinedError):
        await gestor.get_connection(ruta_db)

    with contextlib.suppress(Exception):
        await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


async def test_b2_el_shutdown_exitoso_levanta_la_cuarentena(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Fail-closed no puede ser un callejón sin salida.

    El shutdown es el owner que conserva el derecho a reintentar el cierre;
    cuando lo consigue, el path tiene que volver a ser reabrible. Si no, un
    cierre fallido dejaría la DB inutilizable hasta reiniciar el proceso.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)
    clave = str(ruta_db.resolve())
    close_real = conn.close
    intentos = 0

    async def close_que_falla_una_vez() -> None:
        nonlocal intentos
        intentos += 1
        if intentos == 1:
            await close_real()
            raise sqlite3.OperationalError("close falló a propósito")
        await close_real()

    conn.close = close_que_falla_una_vez  # type: ignore[method-assign]
    _romper_rollback(conn)

    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(_transaccion_que_falla(gestor, ruta_db), timeout=DEADLINE)
    assert clave in gestor._quarantined_paths, "precondición: el path debía quedar fail-closed"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)
    assert clave not in gestor._quarantined_paths, "el cierre exitoso no levantó la cuarentena"

    # Y el path vuelve a servir de verdad, no sólo en el set.
    await asyncio.wait_for(gestor.init_all(), timeout=DEADLINE)
    async with gestor.operation(ruta_db) as fresca:
        assert await esta_usable(fresca), "el path recuperado no entrega una conexión usable"
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


# ---------------------------------------------------------------------------
# C — MATRIZ DE CIERRE: CLOSE-1 … CLOSE-5
# ---------------------------------------------------------------------------


async def test_c1_close_exitoso_sin_cancelacion(gestor: DatabaseLifecycleManager, ruta_db: Path) -> None:
    """CLOSE-1 — cerró; nada que restaurar."""
    conn = await gestor.get_connection(ruta_db)
    resultado = await gestor._close_connection(conn, shielded=True)
    assert resultado.closed is True
    assert resultado.error is None
    assert resultado.cancellation is None


async def test_c2_close_con_excepcion_ordinaria(gestor: DatabaseLifecycleManager, ruta_db: Path) -> None:
    """CLOSE-2 — NO cerró; el error viaja como diagnóstico."""
    conn = await gestor.get_connection(ruta_db)
    close_real = conn.close
    fallo = sqlite3.OperationalError("close falló a propósito")

    async def close_que_falla() -> None:
        raise fallo

    conn.close = close_que_falla  # type: ignore[method-assign]
    resultado = await gestor._close_connection(conn, shielded=True)
    assert resultado.closed is False, "un cierre fallido no puede reportarse como cerrado"
    assert resultado.error is fallo
    assert resultado.cancellation is None
    conn.close = close_real  # type: ignore[method-assign]
    await conn.close()


async def test_c3_cancelacion_externa_con_close_exitoso(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
) -> None:
    """CLOSE-3 — el discriminante central de la matriz.

    Hay cancelación externa Y la conexión SÍ cerró. Clasificarla como "falló
    porque hubo CancelledError" retendría para siempre una conexión ya cerrada;
    perder la cancelación, en cambio, se comería una señal del llamador. Las dos
    cosas tienen que salir: ``closed=True`` **y** la cancelación con su identidad.
    """
    conn = await gestor.get_connection(ruta_db)
    cierre_empezo = asyncio.Event()
    soltar = asyncio.Event()
    close_real = conn.close

    async def close_lento() -> None:
        cierre_empezo.set()
        await soltar.wait()
        await close_real()

    conn.close = close_lento  # type: ignore[method-assign]
    resultado: dict[str, object] = {}

    async def cerrar() -> None:
        resultado["out"] = await gestor._close_connection(conn, shielded=True)

    tarea = asyncio.create_task(cerrar())
    await cierre_empezo.wait()
    tarea.cancel("cancelacion externa deliberada")
    soltar.set()
    await asyncio.wait_for(tarea, timeout=DEADLINE)

    out = resultado["out"]
    assert out.closed is True, "CLOSE-3 se clasificó como cierre fallido"  # type: ignore[union-attr]
    assert out.error is None  # type: ignore[union-attr]
    assert isinstance(out.cancellation, asyncio.CancelledError)  # type: ignore[union-attr]
    assert out.cancellation.args == ("cancelacion externa deliberada",), (  # type: ignore[union-attr]
        "la cancelación externa perdió su identidad"
    )
    assert not await esta_usable(conn), "el cierre debía haber terminado"


async def test_c4_cancelacion_externa_con_close_fallido(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
) -> None:
    """CLOSE-4 — REV-CANCEL.

    Hay dos hechos y hay que conservar los dos: la conexión NO cerró y hay una
    cancelación externa que restaurar. La forma vieja devolvía sólo el error y
    descartaba la cancelación en silencio.
    """
    conn = await gestor.get_connection(ruta_db)
    cierre_empezo = asyncio.Event()
    soltar = asyncio.Event()
    close_real = conn.close
    fallo = sqlite3.OperationalError("close falló a propósito")

    async def close_lento_que_falla() -> None:
        cierre_empezo.set()
        await soltar.wait()
        raise fallo

    conn.close = close_lento_que_falla  # type: ignore[method-assign]
    resultado: dict[str, object] = {}

    async def cerrar() -> None:
        resultado["out"] = await gestor._close_connection(conn, shielded=True)

    tarea = asyncio.create_task(cerrar())
    await cierre_empezo.wait()
    tarea.cancel("cancelacion externa deliberada")
    soltar.set()
    await asyncio.wait_for(tarea, timeout=DEADLINE)

    out = resultado["out"]
    assert out.closed is False  # type: ignore[union-attr]
    assert out.error is fallo, "se perdió el fallo del cierre"  # type: ignore[union-attr]
    assert out.cancellation is not None, "se descartó la cancelación externa"  # type: ignore[union-attr]
    assert out.cancellation.args == ("cancelacion externa deliberada",)  # type: ignore[union-attr]

    conn.close = close_real  # type: ignore[method-assign]
    await conn.close()


async def test_c5_close_produce_su_propia_cancelacion(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
) -> None:
    """CLOSE-5 — no es CLOSE-3, aunque ambos sean ``CancelledError``.

    Acá la cancelación nació DENTRO del cierre: no hay nada externo que
    restaurar y, sobre todo, la conexión NO está cerrada. Tratarlo como CLOSE-3
    retiraría del registro una conexión que sigue viva — el P1 otra vez, por la
    puerta de la cancelación.
    """
    conn = await gestor.get_connection(ruta_db)
    close_real = conn.close
    propia = asyncio.CancelledError("cancelación propia del cierre")

    async def close_que_se_autocancela() -> None:
        raise propia

    conn.close = close_que_se_autocancela  # type: ignore[method-assign]
    resultado = await gestor._close_connection(conn, shielded=True)

    assert resultado.closed is False, "CLOSE-5 se clasificó como cerrado (sería el P1 de nuevo)"
    assert resultado.error is propia, "la cancelación propia debía viajar como diagnóstico"
    assert resultado.cancellation is None, "no hay cancelación externa que restaurar en CLOSE-5"
    assert await esta_usable(conn), "precondición: la conexión seguía viva"

    conn.close = close_real  # type: ignore[method-assign]
    await conn.close()


async def test_c6_cierre_no_confirmado_retiene_en_las_cinco_variantes(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """El puente entre la matriz y el ownership: sólo ``closed`` retira la entrada.

    Enumera los desenlaces en vez de muestrear uno: si mañana aparece una
    variante que reporta ``closed=False`` y aun así se retira, este test la
    ataja sin que nadie tenga que acordarse de escribirle un caso.
    """
    clave = str(ruta_db.resolve())
    for closed, error, cancelacion in [
        (True, None, None),
        (False, sqlite3.OperationalError("x"), None),
        (True, None, asyncio.CancelledError("ext")),
        (False, sqlite3.OperationalError("x"), asyncio.CancelledError("ext")),
        (False, asyncio.CancelledError("propia"), None),
    ]:
        conn = await gestor.get_connection(ruta_db)
        gestor._quarantined_paths.discard(clave)
        desenlace = db_lifecycle._CloseOutcome(closed, error, cancelacion)

        async def _close_falso(_conn: object, *, shielded: bool, _d=desenlace) -> object:  # noqa: ARG001
            return _d

        original = gestor._close_connection
        # Atributo de instancia: no hay binding de descriptor, así que la firma
        # es la del método sin ``self``.
        gestor._close_connection = _close_falso  # type: ignore[assignment,method-assign]
        try:
            async with gestor.get_write_lock(ruta_db):
                await gestor._invalidate_under_boundary(ruta_db, conn, shielded=True)
        finally:
            gestor._close_connection = original  # type: ignore[method-assign]

        registrada = gestor._connections.get(clave) is conn
        assert registrada is (not closed), (
            f"closed={closed}: la entrada {'debía retenerse' if not closed else 'debía retirarse'}"
        )
        assert (clave in gestor._quarantined_paths) is (not closed), (
            f"closed={closed}: la cuarentena no acompañó a la retención"
        )
        gestor._connections.pop(clave, None)
        gestor._quarantined_paths.discard(clave)
        with contextlib.suppress(Exception):
            await conn.close()


# ---------------------------------------------------------------------------
# D — IDENTIDAD: nunca invalidar el reemplazo
# ---------------------------------------------------------------------------


async def test_d1_no_toca_el_reemplazo_registrado(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """REV-EXPECTED. Guard de identidad ABA, por ``is``.

    El guard protege al REEMPLAZO, y sólo a él: quitar la comparación
    ``is expected_connection`` hace que la invalidación de A expulse a B, una
    conexión sana de otro dueño. Lo que el guard NO hace es eximir de cerrar A:
    dejarla viva y fuera del registro sería la fuga que este contrato prohíbe,
    así que las dos mitades se afirman juntas.

    El test se escribe al nivel del helper porque el lock, cuando está, ya ordena
    la carrera: lo que se ancla acá es la guarda, no el lock.
    """
    conn_a = await gestor.get_connection(ruta_db)
    clave = str(ruta_db.resolve())
    conn_b = await db_lifecycle.aiosqlite.connect(str(ruta_db))
    gestor._connections[clave] = conn_b  # B pasa a ser la vigente

    async with gestor.get_write_lock(ruta_db):
        resultado = await gestor._invalidate_under_boundary(ruta_db, conn_a, shielded=True)

    assert resultado is None, "la invalidación mutó el registro pese al reemplazo"
    assert gestor._connections.get(clave) is conn_b, "se expulsó al reemplazo"
    assert await esta_usable(conn_b), "se cerró al reemplazo"
    assert clave not in gestor._quarantined_paths, "se marcó fail-closed un path cuya conexión está sana"
    # La otra mitad: A sí se cerró. Si no, queda viva, sin registro y sin owner.
    assert not await esta_usable(conn_a), "A quedó viva fuera del registro: fuga sin owner"


async def test_d2_la_ventana_publica_rechaza_si_la_conexion_fue_reemplazada(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """El nivel público aborta la ventana: cierra lo suyo y no renombra nada.

    Es el caso que importa para la recovery: si el registro ya apunta a otra
    conexión, renombrar el archivo lo haría por debajo de una conexión viva.
    """
    conn_a = await gestor.get_connection(ruta_db)
    clave = str(ruta_db.resolve())
    conn_b = await db_lifecycle.aiosqlite.connect(str(ruta_db))
    gestor._connections[clave] = conn_b

    with pytest.raises(DatabaseConnectionReplacedError):
        async with gestor.recover_connection(ruta_db, conn_a):
            pytest.fail("no debía entrar a la ventana con la conexión ya reemplazada")

    assert gestor._connections.get(clave) is conn_b, "se expulsó al reemplazo"
    assert await esta_usable(conn_b), "se cerró al reemplazo"
    assert not await esta_usable(conn_a), "A quedó viva fuera del registro: fuga sin owner"
    assert gestor._admitted == 0, "la ventana rechazada dejó una admisión colgada"
    assert clave not in gestor._quarantined_paths, "la ventana abortada dejó el path fail-closed"


async def test_d3_usar_el_nivel_publico_bajo_el_boundary_se_autobloquea(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Por qué el mecanismo tiene DOS niveles y no uno.

    ``asyncio.Lock`` no es reentrante. El camino de fallo de ``transaction()``
    llega a la invalidación con ``L_path`` ya en la mano, así que enrutarlo por
    el nivel público —que adquiere ``L_path``— no da un error: cuelga la
    transacción fallida para siempre, y el error original nunca se propaga.

    Este test fija esa propiedad como un hecho medido, no como una nota al pie:
    si alguien "simplifica" el diseño a una sola API con lock, acá se ve por qué
    no se puede. El ``wait_for`` es el watchdog que convierte el cuelgue en una
    falla legible, nunca un mecanismo de sincronización.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)
    _romper_rollback(conn)

    async def invalidar_por_el_publico(db_path, expected, *, shielded):  # noqa: ANN001, ANN202, ARG001
        async with gestor.recover_connection(db_path, expected):
            return None

    gestor._invalidate_under_boundary = invalidar_por_el_publico  # type: ignore[assignment,method-assign]

    tarea = asyncio.create_task(_transaccion_que_falla(gestor, ruta_db))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(tarea), timeout=0.5)

    assert gestor.get_write_lock(ruta_db).locked(), "el autobloqueo debía dejar L_path tomado"
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)


# ---------------------------------------------------------------------------
# E — VENTANA DE RECOVERY: el hermano no puede colarse
# ---------------------------------------------------------------------------


async def test_e1_ningun_hermano_abre_el_path_durante_la_ventana(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """REV-REGISTRY-WINDOW.

    Es la ventana que rompía la recovery del registry: entre la invalidación y
    el rename, un ``operation()`` hermano abría una conexión "fresh" sobre el
    archivo condenado; el rename se llevaba el inodo y la recovery terminaba
    escribiendo dentro del backup corrupto. Se comprueba además que el lock NO
    se sostiene durante la ventana — sostenerlo sería la otra forma de cerrarla,
    y está prohibida porque adentro corre filesystem.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)
    creadas_antes = len(conexiones)

    async with gestor.recover_connection(ruta_db, conn) as reabrir:
        assert not gestor.get_write_lock(ruta_db).locked(), (
            "la ventana sostiene L_path: adentro corre filesystem y no debe hacerlo"
        )

        with pytest.raises(DatabaseConnectionQuarantinedError):
            async with gestor.operation(ruta_db):
                pass
        assert len(conexiones) == creadas_antes, "un hermano abrió una conexión durante la ventana"

        async with reabrir() as fresca:
            assert fresca is not conn, "la ventana devolvió la conexión invalidada"
            assert await esta_usable(fresca)

    # Al salir, el path vuelve a estar disponible y entrega LA MISMA fresca.
    async with gestor.operation(ruta_db) as despues:
        assert despues is fresca, "tras la ventana el path no entrega la conexión reconstruida"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


async def test_e2_el_shutdown_espera_a_la_recovery_admitida(
    gestor: DatabaseLifecycleManager,
    ruta_db: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """REV-ADMISSION.

    La ventana es trabajo ADMITIDO: cubre invalidación, rename y reapertura. Sin
    esa admisión, ``shutdown_all()`` drena, se declara completo y la recovery
    sigue corriendo después — abriendo una conexión que nadie va a cerrar.
    """
    await gestor.init_all()
    conn = await gestor.get_connection(ruta_db)
    en_ventana = asyncio.Event()
    soltar = asyncio.Event()
    recuperada: dict[str, object] = {}

    async def recovery() -> None:
        async with gestor.recover_connection(ruta_db, conn) as reabrir:
            en_ventana.set()
            await soltar.wait()  # acá viviría el rename/backoff
            async with reabrir() as fresca:
                recuperada["conn"] = fresca

    tarea = asyncio.create_task(recovery())
    await en_ventana.wait()

    shutdown = asyncio.create_task(gestor.shutdown_all())
    await ceder()
    assert not shutdown.done(), "el shutdown se declaró completo con una recovery admitida en vuelo"

    soltar.set()
    await asyncio.wait_for(tarea, timeout=DEADLINE)
    await asyncio.wait_for(shutdown, timeout=DEADLINE)

    # Ni resurrección ni huérfanas: la conexión que abrió la recovery quedó cerrada.
    assert gestor.managed_paths == [], "el shutdown terminó dejando conexiones registradas"
    assert not await esta_usable(recuperada["conn"]), "la conexión de la recovery sobrevivió al shutdown"
    # Y después del shutdown no se admite trabajo nuevo.
    with pytest.raises(db_lifecycle.DatabaseLifecycleShuttingDownError):
        async with gestor.operation(ruta_db):
            pass


# ---------------------------------------------------------------------------
# F — REGISTRY: la recovery completa, extremo a extremo
# ---------------------------------------------------------------------------


async def test_f1_la_recovery_del_registry_reconstruye_el_path_nuevo(
    tmp_path: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """El P1 del registry, con discriminante en filesystem Y en conexión.

    Antes: el hermano abría fresh sobre el archivo corrupto, el rename se lo
    llevaba y el registry escribía el schema en el backup — el ``.db`` nuevo ni
    siquiera existía. Ahora el hermano queda bloqueado durante la ventana, y la
    conexión con la que termina el registry corresponde al archivo NUEVO.
    """
    ruta = tmp_path / "registry.db"
    gestor = DatabaseLifecycleManager(
        db_paths=[ruta],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )
    conn_corrupta = await gestor.get_connection(ruta)
    registry = AsyncModRegistry(ruta, lifecycle=gestor)

    rename_empezo = asyncio.Event()
    soltar_rename = asyncio.Event()
    rename_real = pathlib.Path.rename
    loop = asyncio.get_running_loop()

    def rename_bloqueante(self: pathlib.Path, target: object) -> object:
        # Corre en to_thread: la barrera se cruza de vuelta al loop.
        rename_empezo.set()
        asyncio.run_coroutine_threadsafe(soltar_rename.wait(), loop).result(timeout=DEADLINE * 4)
        return rename_real(self, target)

    _fingir_corrupcion(conn_corrupta)
    pathlib.Path.rename = rename_bloqueante  # type: ignore[method-assign]
    try:
        apertura = asyncio.create_task(registry.open())
        await asyncio.wait_for(rename_empezo.wait(), timeout=DEADLINE)

        # Plena ventana: el hermano NO puede abrir sobre el archivo condenado.
        creadas_antes = len(conexiones)
        with pytest.raises(DatabaseConnectionQuarantinedError):
            async with gestor.operation(ruta):
                pass
        assert len(conexiones) == creadas_antes, "un hermano abrió sobre el archivo que estaba por renombrarse"

        soltar_rename.set()
        await asyncio.wait_for(apertura, timeout=DEADLINE * 4)
    finally:
        pathlib.Path.rename = rename_real  # type: ignore[method-assign]
        soltar_rename.set()

    # 1. Filesystem: el backup existe Y el path nuevo también.
    backups = list(tmp_path.glob("registry.corrupt.*.db"))
    assert len(backups) == 1, f"no quedó exactamente un backup corrupto: {backups}"
    assert ruta.exists(), "la recovery no dejó un .db nuevo en el path original"

    # 2. Conexión: la que quedó vigente sirve y tiene el schema, y es la que ve
    #    el path NUEVO — no el inodo renombrado.
    async with gestor.operation(ruta) as conn:
        async with conn.execute("SELECT COUNT(*) FROM mods") as cur:
            assert (await cur.fetchone())[0] == 0
        async with conn.execute("PRAGMA database_list") as cur:
            archivo = [row[2] for row in await cur.fetchall() if row[1] == "main"][0]
    assert pathlib.Path(archivo).resolve() == ruta.resolve(), (
        f"la conexión vigente apunta a {archivo}, no al path reconstruido"
    )
    assert not any(pathlib.Path(archivo).resolve() == b.resolve() for b in backups), (
        "la conexión vigente quedó atada al backup corrupto"
    )

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


async def test_f2_el_registry_no_deja_el_path_en_cuarentena_al_terminar(
    tmp_path: Path,
    conexiones: list[aiosqlite.Connection],
) -> None:
    """Una recovery exitosa devuelve el path a servicio; si no, la DB queda muerta."""
    ruta = tmp_path / "registry2.db"
    gestor = DatabaseLifecycleManager(
        db_paths=[ruta],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )
    conn_corrupta = await gestor.get_connection(ruta)
    registry = AsyncModRegistry(ruta, lifecycle=gestor)
    _fingir_corrupcion(conn_corrupta)

    await asyncio.wait_for(registry.open(), timeout=DEADLINE * 4)

    assert gestor._quarantined_paths == set(), "la recovery exitosa dejó el path fail-closed"
    await registry.upsert_mod(nexus_id=1, name="mod de prueba")
    assert (await registry.get_mod(1))["name"] == "mod de prueba"

    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


async def test_f3_corrupcion_con_cierre_fallido_no_renombra_nada(
    tmp_path: Path,
    conexiones: list[aiosqlite.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si el cierre no se confirma, el archivo NO se toca.

    Renombrar debajo de una conexión que quizá siga escribiendo es exactamente
    cómo se corrompe el backup. El fail-closed tiene que llegar antes que el
    filesystem.
    """
    ruta = tmp_path / "registry3.db"
    gestor = DatabaseLifecycleManager(
        db_paths=[ruta],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )
    conn = await gestor.get_connection(ruta)
    registry = AsyncModRegistry(ruta, lifecycle=gestor)
    fallo = OSError("close deliberadamente fallido")
    close_real = conn.close

    async def close_que_falla() -> None:
        raise fallo

    monkeypatch.setattr(conn, "close", close_que_falla)
    monkeypatch.setattr(
        pathlib.Path,
        "rename",
        lambda *_a, **_k: pytest.fail("no debía renombrar con el cierre sin confirmar"),
    )
    _fingir_corrupcion(conn)

    with pytest.raises(_DatabaseCorruptionError) as exc_info:
        await asyncio.wait_for(registry.open(), timeout=DEADLINE)

    assert exc_info.value.__cause__ is fallo, "se perdió la causa real del fallo de cierre"
    assert gestor._connections.get(str(ruta.resolve())) is conn, "se soltó el ownership sin confirmar el cierre"
    assert str(ruta.resolve()) in gestor._quarantined_paths, "el path no quedó fail-closed"
    assert registry._conn is None
    assert list(tmp_path.glob("*.corrupt.*")) == [], "renombró pese al cierre sin confirmar"

    monkeypatch.setattr(conn, "close", close_real)
    await asyncio.wait_for(gestor.shutdown_all(), timeout=DEADLINE)


# ---------------------------------------------------------------------------
# G — INVENTARIO AST: quién puede mutar el registro
# ---------------------------------------------------------------------------

RAIZ = Path(__file__).resolve().parents[1]

# Funciones autorizadas a mutar ``_connections``, con la cantidad EXACTA de
# mutaciones de cada una. Congelar el conteo y no sólo el nombre: agregar un
# segundo `pop` dentro de una función ya listada es el punto de extensión más
# probable, y con un set de nombres pasaría sin romper nada.
#
# La regla que este inventario defiende: "cerrar + mutar el registro" tiene UNA
# sola política. Un tercer camino hermano rompe el test hasta que se decida
# explícitamente si participa del mecanismo compartido o es una exención.
MUTADORES_DE_REGISTRO_ESPERADOS = {
    # Registro tras un init exitoso.
    "_init_single": 1,
    # Único camino de retiro por cierre confirmado del shutdown.
    "_shutdown_all_under_transition": 1,
    # Nivel interno del mecanismo compartido: el pop va DESPUÉS del cierre.
    "_invalidate_under_boundary": 1,
    # API legacy sin callers en producción (ver test G2).
    "evict_connection": 1,
}


def _mutaciones_de_connections_por_funcion() -> dict[str, int]:
    """Cuenta, por función, las sentencias que MUTAN ``self._connections``."""
    arbol = ast.parse((RAIZ / "sky_claw/app/core/db_lifecycle.py").read_text(encoding="utf-8"))
    conteo: dict[str, int] = {}

    def es_registro(nodo: ast.expr) -> bool:
        return (
            isinstance(nodo, ast.Attribute)
            and nodo.attr == "_connections"
            and isinstance(nodo.value, ast.Name)
            and nodo.value.id == "self"
        )

    for funcion in ast.walk(arbol):
        if not isinstance(funcion, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        mutaciones = 0
        for nodo in ast.walk(funcion):
            # self._connections[k] = v   /   del self._connections[k]
            if isinstance(nodo, ast.Assign | ast.Delete):
                mutaciones += sum(1 for t in nodo.targets if isinstance(t, ast.Subscript) and es_registro(t.value))
            # self._connections.pop(...) / .clear() / .update(...) / .setdefault(...)
            elif (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr in {"pop", "clear", "update", "setdefault", "popitem"}
                and es_registro(nodo.func.value)
            ):
                mutaciones += 1
        if mutaciones:
            conteo[funcion.name] = mutaciones
    return conteo


def test_g1_inventario_exacto_de_mutadores_del_registro() -> None:
    """Ancla que ENUMERA, no que muestrea.

    Un caso escrito a mano para el hermano que faltó no ataja al siguiente. Esto
    congela el conjunto completo por igualdad literal: cualquier callsite nuevo
    que mute ``_connections`` rompe acá hasta que se le escriba su política de
    ownership y su test de comportamiento.

    AST = inventario. La autoridad sobre correctness siguen siendo las carreras
    ejecutadas de las secciones A–F.
    """
    assert _mutaciones_de_connections_por_funcion() == MUTADORES_DE_REGISTRO_ESPERADOS


def test_g2_evict_connection_quedo_sin_callers_en_produccion() -> None:
    """``evict_connection`` es hoy API muerta; su baja necesita adjudicación.

    La recovery del registry —su único caller— pasó al mecanismo compartido.
    Se deja el símbolo y se ancla el hecho: si alguien lo vuelve a cablear, este
    test lo obliga a decidirlo a propósito en vez de resucitar por inercia el
    patrón "expulsar sin cerrar" que el P1 hizo fallar.
    """
    callers = [
        ruta.relative_to(RAIZ).as_posix()
        for ruta in (RAIZ / "sky_claw").rglob("*.py")
        if "evict_connection(" in ruta.read_text(encoding="utf-8") and ruta.name != "db_lifecycle.py"
    ]
    assert callers == [], f"evict_connection volvió a tener callers en producción: {callers}"

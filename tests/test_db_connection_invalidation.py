"""Contrato de invalidación de conexiones compartidas del lifecycle.

Por qué existe: `DatabaseLifecycleManager` entrega una conexión **singleton por
path resuelto**, así que varios wrappers comparten el mismo objeto. Hasta este
contrato el lifecycle podía *expulsar* una conexión (`evict_connection`) pero no
*invalidarla*: el que la expulsaba se enteraba, y los otros cuatro wrappers que
la tenían cacheada seguían emitiendo SQL contra una conexión cerrada. Nada en el
árbol podía contestar "¿mi conexión cacheada sigue siendo la válida de este
path?".

Las dos propiedades que se anclan acá, enunciadas como propiedades del
mecanismo y no como recordatorio de proceso:

    la invalidación de una conexión compartida sólo protege si TODOS los que la
    cachean la consultan antes de reusarla

    ceder ownership es un acto posterior al cierre, no simultáneo: si el cierre
    falla, el lifecycle conserva la conexión porque es la única referencia con
    la que shutdown puede reintentar la limpieza

Los tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): `test_ancla_wrappers_que_cachean_conexion` congela por AST el
conjunto de módulos que guardan una conexión del lifecycle en un atributo, con
su estado de adopción. Un wrapper nuevo rompe el ancla hasta que se le escriba
su receta — y la receta lo mete en los tests de comportamiento de abajo.
"""

from __future__ import annotations

import ast
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sky_claw.app.agent.router import LLMRouter
from sky_claw.app.core.database import DatabaseAgent
from sky_claw.app.core.db_lifecycle import (
    DatabaseLifecycleManager,
    DatabasePathPoisonedError,
)
from sky_claw.app.db.async_registry import AsyncModRegistry
from sky_claw.app.db.journal import JournalConnectionError, OperationJournal
from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockDatabaseUnavailableError,
    LockLeaseLostError,
    SnapshotTransactionLock,
)

RAIZ = Path(__file__).resolve().parents[1]


# ===========================================================================
# Ancla enumerativa
# ===========================================================================

# Módulos que guardan una conexión del lifecycle en un atributo de instancia y,
# por lo tanto, pueden quedarse con una referencia obsoleta. Congelado por
# igualdad: un wrapper nuevo rompe el test hasta que se decida su estado.
#
# `revalida`: consume `refresh_connection()` antes de reusar lo cacheado.
# `refetch`:  vuelve a pedir la conexión al lifecycle en cada uso, así que es
#             inmune por construcción — verificado por comportamiento en
#             `test_database_agent_repide_la_conexion_en_cada_uso`, no por
#             confianza en este comentario.
WRAPPERS_QUE_CACHEAN_CONEXION = {
    "sky_claw/app/agent/router.py": "revalida",
    "sky_claw/app/core/database.py": "refetch",
    "sky_claw/app/db/async_registry.py": "revalida",
    "sky_claw/app/db/journal.py": "revalida",
    "sky_claw/app/db/locks.py": "revalida",
}

_OBTENCION = {"get_connection", "refresh_connection"}


def _asignaciones(nodo: ast.AST) -> tuple[list[ast.expr], ast.expr] | None:
    """Normaliza ``x = v`` y ``x: T = v`` a ``(destinos, valor)``."""
    if isinstance(nodo, ast.Assign):
        return nodo.targets, nodo.value
    if isinstance(nodo, ast.AnnAssign) and nodo.value is not None:
        return [nodo.target], nodo.value
    return None


def _es_obtencion(valor: ast.expr) -> bool:
    """True si *valor* es ``await <algo>.get_connection(...)`` o ``refresh_connection(...)``."""
    return (
        isinstance(valor, ast.Await)
        and isinstance(valor.value, ast.Call)
        and isinstance(valor.value.func, ast.Attribute)
        and valor.value.func.attr in _OBTENCION
    )


def _es_atributo_de_self(destino: ast.expr) -> bool:
    return isinstance(destino, ast.Attribute) and isinstance(destino.value, ast.Name) and destino.value.id == "self"


def _cachea_conexion(arbol: ast.AST) -> bool:
    """True si el módulo guarda en un atributo de ``self`` una conexión del lifecycle.

    Detecta también el guardado **en dos pasos** —``conn = await ...get_connection();
    self._conn = conn``—, que no es hipotético: es exactamente lo que hace
    ``core/database.py:128-130``. Un detector que sólo mirara la asignación
    directa dejaría entrar un wrapper nuevo escrito con ese idioma sin romper el
    ancla, o sea justo el defecto que el ancla existe para atajar.

    Sobre-aproxima a propósito (no sigue el flujo de datos, sólo nombres dentro
    de la función): para un ancla, marcar de más obliga a tomar una decisión y
    marcar de menos deja pasar el hermano.
    """
    for funcion in ast.walk(arbol):
        if not isinstance(funcion, ast.AsyncFunctionDef | ast.FunctionDef):
            continue

        # Paso 1: nombres locales que reciben una conexión del lifecycle.
        locales: set[str] = set()
        for nodo in ast.walk(funcion):
            par = _asignaciones(nodo)
            if par is None or not _es_obtencion(par[1]):
                continue
            for destino in par[0]:
                if isinstance(destino, ast.Name):
                    locales.add(destino.id)

        # Paso 2: ¿alguno termina en un atributo de self, directo o vía local?
        for nodo in ast.walk(funcion):
            par = _asignaciones(nodo)
            if par is None:
                continue
            destinos, valor = par
            if not (_es_obtencion(valor) or (isinstance(valor, ast.Name) and valor.id in locales)):
                continue
            if any(_es_atributo_de_self(destino) for destino in destinos):
                return True
    return False


def test_ancla_wrappers_que_cachean_conexion() -> None:
    """El conjunto de wrappers que cachean conexión está congelado y clasificado."""
    encontrados = set()
    for archivo in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        if _cachea_conexion(arbol):
            encontrados.add(archivo.relative_to(RAIZ).as_posix())

    assert encontrados == set(WRAPPERS_QUE_CACHEAN_CONEXION), (
        "Cambió el conjunto de wrappers que cachean una conexión del lifecycle. "
        "Un wrapper nuevo debe declarar si revalida contra el lifecycle "
        "('revalida') o si re-pide la conexión en cada uso ('refetch'), y sumar "
        "su caso a los tests de comportamiento de este archivo."
    )


# Para cada wrapper 'revalida': el atributo donde cachea la conexión, los puntos
# por los que se revalida, y los métodos que pueden LEER el atributo sin pasar
# por ellos.
#
# La exención no es discrecional: son los métodos que ESTABLECEN o DESTRUYEN la
# conexión. Revalidar ahí no tendría sentido —``open`` es quien la consigue y
# ``close`` quien la suelta—, y son los únicos dos roles que legítimamente
# tocan el atributo crudo. Cualquier otro método que lo lea está usando la
# conexión para trabajar, y entonces debe revalidarla.
REVALIDACION_POR_WRAPPER = {
    "sky_claw/app/agent/router.py": ("_conn", {"refresh_connection"}, {"open", "close"}),
    "sky_claw/app/db/async_registry.py": (
        "_conn",
        {"refresh_connection", "_revalidar_conexion"},
        {"open", "close"},
    ),
    "sky_claw/app/db/journal.py": ("_db", {"refresh_connection", "_ensure_connected"}, {"open", "close"}),
    "sky_claw/app/db/locks.py": ("_conn", {"refresh_connection", "_ensure_conn"}, {"initialize", "close"}),
}


def _lee_atributo(funcion: ast.AST, attr: str) -> bool:
    """True si la función LEE ``self.<attr>`` (uso, no asignación)."""
    return any(
        isinstance(nodo, ast.Attribute)
        and nodo.attr == attr
        and isinstance(nodo.ctx, ast.Load)
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "self"
        for nodo in ast.walk(funcion)
    )


def _llama_a(funcion: ast.AST, nombres: set[str]) -> bool:
    return any(
        isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr in nombres
        for nodo in ast.walk(funcion)
    )


def test_ancla_todo_uso_de_la_conexion_cacheada_se_revalida() -> None:
    """Enumera los MÉTODOS, no la presencia de la llamada en el archivo.

    Verificar que ``refresh_connection(`` aparezca una vez por módulo no ancla
    nada: un método nuevo que use ``self._conn`` sin revalidar deja los tests en
    verde y reproduce exactamente el hermano sin invalidar que este contrato
    existe para prevenir. Acá se enumera cada método que lee la conexión
    cacheada y se exige que pase por la revalidación, con las excepciones
    congeladas por igualdad.
    """
    declarados = {r for r, e in WRAPPERS_QUE_CACHEAN_CONEXION.items() if e == "revalida"}
    assert declarados == set(REVALIDACION_POR_WRAPPER), "todo wrapper 'revalida' necesita su receta de revalidación acá"

    for ruta, (attr, chokepoints, exentos) in sorted(REVALIDACION_POR_WRAPPER.items()):
        arbol = ast.parse((RAIZ / ruta).read_text(encoding="utf-8"), filename=ruta)
        sin_revalidar = {
            funcion.name
            for funcion in ast.walk(arbol)
            if isinstance(funcion, ast.AsyncFunctionDef | ast.FunctionDef)
            and _lee_atributo(funcion, attr)
            and not _llama_a(funcion, chokepoints)
        }
        assert sin_revalidar == exentos, (
            f"En {ruta} cambió el conjunto de métodos que leen self.{attr} sin "
            f"revalidar: {sorted(sin_revalidar)} (esperado {sorted(exentos)}). "
            "Un método que usa la conexión cacheada tiene que pasar por "
            f"{sorted(chokepoints)}; sólo los que la establecen o la cierran "
            "quedan exentos."
        )


# ===========================================================================
# Helpers
# ===========================================================================


def _lifecycle(db: Path) -> DatabaseLifecycleManager:
    return DatabaseLifecycleManager(db_paths=[db])


async def _romper_rollback(conn: aiosqlite.Connection) -> None:
    """Hace fallar el rollback de *conn* (dispara la cuarentena real)."""

    async def _falla() -> None:
        raise sqlite3.OperationalError("rollback imposible")

    conn.rollback = _falla  # type: ignore[method-assign]


async def _provocar_cuarentena(
    lifecycle: DatabaseLifecycleManager,
    db: Path,
    conn: aiosqlite.Connection,
) -> None:
    """Cuarentena por la ruta de producción: transacción que falla y no puede deshacerse."""
    await _romper_rollback(conn)
    with pytest.raises(BaseException):  # noqa: B017,PT011 - el tipo lo decide el encadenado
        async with lifecycle.transaction(db):
            raise RuntimeError("boom")


# ===========================================================================
# T1 — invalidación entre hermanos (mismo tipo de wrapper)
# ===========================================================================


async def test_t1_un_journal_hermano_detecta_la_referencia_obsoleta(tmp_path: Path) -> None:
    """j2 nunca se cierra a mano: debe detectar solo que su conexión ya no vale."""
    db = tmp_path / "journal.db"
    lifecycle = _lifecycle(db)
    j1 = OperationJournal(str(db), lifecycle=lifecycle)
    j2 = OperationJournal(str(db), lifecycle=lifecycle)
    await j1.open()
    await j2.open()

    compartida = j1._db
    assert compartida is not None
    assert j2._db is compartida, "precondición: ambos journals comparten la conexión"

    await _provocar_cuarentena(lifecycle, db, compartida)

    # Nadie tocó j2. Su siguiente operación tiene que funcionar sobre una
    # conexión nueva, no explotar contra la cerrada.
    tx = await j2.begin_transaction("después de la cuarentena")
    assert tx > 0

    assert j2._db is not compartida, "j2 siguió con la conexión invalidada"
    assert not compartida._running, "la conexión vieja debería estar cerrada"

    await lifecycle.shutdown_all()


# ===========================================================================
# T2 — otra familia de wrapper (el mecanismo no es del Journal)
# ===========================================================================


async def test_t2_otra_familia_de_wrapper_tambien_reengancha(tmp_path: Path) -> None:
    """Un DistributedLockManager sobre el mismo path también detecta la invalidación."""
    db = tmp_path / "compartida.db"
    lifecycle = _lifecycle(db)
    journal = OperationJournal(str(db), lifecycle=lifecycle)
    locks = DistributedLockManager(str(db), lifecycle=lifecycle)
    await journal.open()
    await locks.initialize()

    compartida = journal._db
    assert compartida is not None
    assert locks._conn is compartida, "precondición: journal y locks comparten conexión"

    await _provocar_cuarentena(lifecycle, db, compartida)

    # El lock manager no se enteró de nada y no fue cerrado.
    adquirido = await locks.acquire_lock("recurso", "duenio")
    assert adquirido is not None
    assert locks._conn is not compartida

    await lifecycle.shutdown_all()


# ===========================================================================
# T3 — el cierre también falla: fail-closed y ownership conservado
# ===========================================================================


async def test_t3_cierre_fallido_deja_el_path_envenenado(tmp_path: Path) -> None:
    """close() falla ⇒ invalidada igual, ownership retenido, sin reemplazo."""
    db = tmp_path / "envenenada.db"
    lifecycle = _lifecycle(db)
    j1 = OperationJournal(str(db), lifecycle=lifecycle)
    j2 = OperationJournal(str(db), lifecycle=lifecycle)
    await j1.open()
    await j2.open()

    compartida = j1._db
    assert compartida is not None

    cierre_real = compartida.close

    async def _cierre_que_falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    compartida.close = _cierre_que_falla  # type: ignore[method-assign]

    await _provocar_cuarentena(lifecycle, db, compartida)

    clave = str(db.resolve())

    # (a) el path quedó fail-closed
    assert lifecycle.is_path_poisoned(db)
    # (b) ownership conservado: shutdown/recovery todavía la conoce
    assert clave in lifecycle.managed_paths
    assert lifecycle._connections[clave] is compartida
    # (c) NO se entrega un reemplazo
    with pytest.raises(DatabasePathPoisonedError):
        await lifecycle.get_connection(db)
    # (d) el hermano cacheado no puede seguir usándola en silencio
    assert not lifecycle.is_connection_current(db, compartida)
    # El journal lo traduce a su propia taxonomía (ver T11), pero el hermano
    # falla closed igual: lo que no puede es seguir usándola en silencio.
    with pytest.raises(JournalConnectionError):
        await j2.begin_transaction("no debería entrar")

    # (e) evict_connection no puede robarle el ownership al path envenenado:
    #     lo dejaría fail-closed para siempre y sin nadie que reintente el cierre.
    assert lifecycle.evict_connection(db, expected_connection=compartida) is False
    assert lifecycle._connections[clave] is compartida

    # (f) recovery: cuando el cierre por fin funciona, el veneno se levanta.
    compartida.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()
    assert not lifecycle.is_path_poisoned(db)
    assert clave not in lifecycle.managed_paths


async def test_t3b_el_error_de_cierre_viaja_en_la_excepcion(tmp_path: Path) -> None:
    """El diagnóstico no se tapa: el fallo de cierre queda accesible."""
    db = tmp_path / "diag.db"
    lifecycle = _lifecycle(db)
    conn = await lifecycle.get_connection(db)

    async def _cierre_que_falla() -> None:
        raise sqlite3.OperationalError("disco lleno")

    cierre_real = conn.close
    conn.close = _cierre_que_falla  # type: ignore[method-assign]

    close_error = await lifecycle.quarantine_connection(db, conn)
    assert isinstance(close_error, sqlite3.OperationalError)

    with pytest.raises(DatabasePathPoisonedError) as excinfo:
        await lifecycle.get_connection(db)
    assert excinfo.value.close_error is close_error
    assert "disco lleno" in str(excinfo.value)

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# T4 — un rollback normal no invalida nada
# ===========================================================================


async def test_t4_rollback_exitoso_no_invalida_la_conexion(tmp_path: Path) -> None:
    """El camino feliz no puede pagar el costo del camino de recuperación."""
    db = tmp_path / "ok.db"
    lifecycle = _lifecycle(db)
    j1 = OperationJournal(str(db), lifecycle=lifecycle)
    j2 = OperationJournal(str(db), lifecycle=lifecycle)
    await j1.open()
    await j2.open()
    compartida = j1._db
    assert compartida is not None

    with pytest.raises(RuntimeError):
        async with lifecycle.transaction(db):
            raise RuntimeError("falla con rollback sano")

    assert not lifecycle.is_path_poisoned(db)
    assert lifecycle.is_connection_current(db, compartida)

    await j2.begin_transaction("sigue igual")
    assert j2._db is compartida, "una conexión sana no debe reemplazarse"
    assert j1._db is compartida

    await lifecycle.shutdown_all()


# ===========================================================================
# T5 — paths independientes
# ===========================================================================


async def test_t5_envenenar_un_path_no_afecta_a_otro(tmp_path: Path) -> None:
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    lifecycle = DatabaseLifecycleManager(db_paths=[a, b])
    conn_a = await lifecycle.get_connection(a)
    conn_b = await lifecycle.get_connection(b)

    async def _cierre_que_falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    cierre_real = conn_a.close
    conn_a.close = _cierre_que_falla  # type: ignore[method-assign]
    await lifecycle.quarantine_connection(a, conn_a)

    assert lifecycle.is_path_poisoned(a)
    assert not lifecycle.is_path_poisoned(b)
    assert lifecycle.is_connection_current(b, conn_b)
    assert await lifecycle.get_connection(b) is conn_b

    conn_a.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# T6 — aliasing: dos formas del mismo path resuelto comparten estado
# ===========================================================================


async def test_t6_un_alias_del_mismo_path_comparte_la_invalidacion(tmp_path: Path) -> None:
    """Misma canonicalización que get_connection/get_write_lock."""
    directo = tmp_path / "alias.db"
    (tmp_path / "sub").mkdir()
    alias = tmp_path / "sub" / ".." / "alias.db"
    assert directo.resolve() == alias.resolve()

    lifecycle = DatabaseLifecycleManager(db_paths=[directo])
    conn = await lifecycle.get_connection(directo)
    assert await lifecycle.get_connection(alias) is conn
    assert lifecycle.get_write_lock(alias) is lifecycle.get_write_lock(directo)

    async def _cierre_que_falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    cierre_real = conn.close
    conn.close = _cierre_que_falla  # type: ignore[method-assign]
    await lifecycle.quarantine_connection(directo, conn)

    assert lifecycle.is_path_poisoned(alias), "el alias quedó sin proteger"
    assert not lifecycle.is_connection_current(alias, conn)
    with pytest.raises(DatabasePathPoisonedError):
        await lifecycle.get_connection(alias)

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# T7 — concurrencia: dos detecciones simultáneas, un solo reemplazo
# ===========================================================================


async def test_t7_dos_wrappers_no_abren_dos_reemplazos(tmp_path: Path) -> None:
    """El singleton por path sigue valiendo cuando la invalidación es concurrente."""
    db = tmp_path / "carrera.db"
    lifecycle = _lifecycle(db)
    j1 = OperationJournal(str(db), lifecycle=lifecycle)
    j2 = OperationJournal(str(db), lifecycle=lifecycle)
    await j1.open()
    await j2.open()
    compartida = j1._db
    assert compartida is not None

    await _provocar_cuarentena(lifecycle, db, compartida)

    # Contar aperturas reales a partir de acá.
    init_real = lifecycle._init_single
    aperturas = 0
    listos = asyncio.Event()

    async def _init_contado(path: Path) -> None:
        nonlocal aperturas
        aperturas += 1
        # Ceder el control dentro de la sección crítica: sin `_init_lock`, la
        # segunda coroutine entraría acá y abriría un segundo reemplazo.
        await listos.wait()
        await init_real(path)

    lifecycle._init_single = _init_contado  # type: ignore[method-assign]

    tarea1 = asyncio.create_task(j1._ensure_connected())
    tarea2 = asyncio.create_task(j2._ensure_connected())
    await asyncio.sleep(0)
    listos.set()
    conn1, conn2 = await asyncio.gather(tarea1, tarea2)

    assert aperturas == 1, f"se abrieron {aperturas} reemplazos para el mismo path"
    assert conn1 is conn2
    assert conn1 is not compartida

    lifecycle._init_single = init_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# T8 — dos cuarentenas concurrentes de la MISMA conexión
# ===========================================================================


async def test_t8_una_cuarentena_perdedora_no_mata_el_path(tmp_path: Path) -> None:
    """El veneno nunca puede quedar sin conexión registrada que lo levante.

    Si dos caminos cuarentenan la misma conexión a la vez y uno cierra bien, el
    que llega tarde recibe "already closed". Envenenar con ESE error dejaría el
    path fail-closed y con ``_connections`` vacío: ``shutdown_all`` no tendría
    qué cerrar, así que el veneno sería permanente hasta reiniciar el proceso —
    peor que el estado previo a este contrato, donde el siguiente
    ``get_connection`` simplemente abría un reemplazo.
    """
    db = tmp_path / "doble.db"
    lifecycle = _lifecycle(db)
    conn = await lifecycle.get_connection(db)

    cierre_real = conn.close
    intentos = 0

    async def _cierre(*, _real=cierre_real) -> None:
        nonlocal intentos
        intentos += 1
        if intentos == 1:
            await _real()
            return
        # El perdedor termina DESPUÉS del ganador: es el orden que deja el
        # veneno escrito al final si no se relee el estado tras el await.
        await asyncio.sleep(0.02)
        raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    conn.close = _cierre  # type: ignore[method-assign]

    await asyncio.gather(
        lifecycle.quarantine_connection(db, conn),
        lifecycle.quarantine_connection(db, conn),
        return_exceptions=True,
    )
    assert intentos == 2, "precondición: las dos cuarentenas llegaron a cerrar"

    assert not lifecycle.is_path_poisoned(db), "el path quedó envenenado por un cierre ajeno ya resuelto"
    reemplazo = await lifecycle.get_connection(db)
    assert reemplazo is not conn

    await lifecycle.shutdown_all()


async def test_t8b_el_veneno_siempre_conserva_su_conexion(tmp_path: Path) -> None:
    """Invariante: ``POISONED`` implica conexión registrada (si no, es un callejón)."""
    db = tmp_path / "inv.db"
    lifecycle = _lifecycle(db)
    conn = await lifecycle.get_connection(db)
    cierre_real = conn.close

    async def _falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    conn.close = _falla  # type: ignore[method-assign]
    await lifecycle.quarantine_connection(db, conn)

    assert lifecycle.is_path_poisoned(db)
    assert lifecycle._connections.get(str(db.resolve())) is conn, (
        "un path envenenado sin conexión registrada no lo puede recuperar nadie"
    )

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# T9 — cancelación externa con el cierre ya completado
# ===========================================================================


async def test_t9_cancelacion_tras_un_cierre_exitoso_no_envenena(tmp_path: Path) -> None:
    """Un cierre que SÍ terminó cede ownership, aunque cancelen al llamador.

    ``_await_db_operation`` completa la operación y recién después re-lanza la
    cancelación externa. Tratar esa ``CancelledError`` como un error de cierre
    cualquiera envenenaría el path pese a que el cierre salió bien, y encima
    conservaría el ownership de una conexión ya cerrada.

    La cancelación se devuelve en vez de re-lanzarse a propósito: los llamadores
    ya restauran la suya —``_init_single`` la ORIGINAL, verificado por
    ``test_init_single_cancelado_espera_close_y_preserva_cancelacion_original``—
    y re-lanzarla acá la pisaría con la del cierre.
    """
    db = tmp_path / "cancel.db"
    lifecycle = _lifecycle(db)
    conn = await lifecycle.get_connection(db)

    cierre_real = conn.close

    async def _cierre_lento(*, _real=cierre_real) -> None:
        await asyncio.sleep(0.05)
        await _real()

    conn.close = _cierre_lento  # type: ignore[method-assign]

    tarea = asyncio.create_task(lifecycle.quarantine_connection(db, conn))
    await asyncio.sleep(0.01)
    tarea.cancel()

    resultado = await asyncio.gather(tarea, return_exceptions=True)
    devuelto = resultado[0]
    assert isinstance(devuelto, asyncio.CancelledError), (
        "la cancelación no puede desaparecer: vuelve como resultado para que el llamador la restaure"
    )

    assert not conn._running, "el cierre tenía que haber terminado igual"
    assert not lifecycle.is_path_poisoned(db), "se envenenó el path por una cancelación, no por un fallo"
    assert str(db.resolve()) not in lifecycle._connections, "el cierre exitoso no cedió ownership"

    await lifecycle.shutdown_all()


# ===========================================================================
# T10 — un path envenenado no puede disfrazarse de error de cuerpo
# ===========================================================================


async def test_t10_propiedad_no_verificable_es_lease_perdida(tmp_path: Path) -> None:
    """Fail-closed no puede degenerar en pisar los archivos de otro agente.

    Hacer que `_ensure_conn` pueda lanzar `DatabasePathPoisonedError` metió un
    tipo de excepción nuevo en `assert_owned`. Sin traducirlo, `__aexit__` lo
    clasificaría como una excepción del cuerpo y restauraría los snapshots
    justo cuando la exclusividad dejó de ser verificable — y otro agente pudo
    haber reclamado la lease vencida y mutado esos archivos.
    """
    db = tmp_path / "lease.db"
    lifecycle = _lifecycle(db)
    locks = DistributedLockManager(str(db), lifecycle=lifecycle)
    await locks.initialize()
    conn = locks._conn
    assert conn is not None

    gestor_snapshots = AsyncMock()
    snapshot = SnapshotTransactionLock(
        resource_id="recurso",
        agent_id="agente",
        lock_manager=locks,
        snapshot_manager=gestor_snapshots,
        target_files=[],
    )
    cierre_real = conn.close
    causa: BaseException | None = None

    async def _falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    # El contexto termina en lease perdida, NO en la excepción del cuerpo: es
    # esa clasificación la que decide si se restauran los snapshots.
    with pytest.raises(LockLeaseLostError):
        async with snapshot:
            conn.close = _falla  # type: ignore[method-assign]
            await lifecycle.quarantine_connection(db, conn)
            assert lifecycle.is_path_poisoned(db)

            with pytest.raises(LockLeaseLostError) as excinfo:
                await snapshot.assert_owned()
            causa = excinfo.value.__cause__

            assert snapshot.lease_lost, "una propiedad no verificable debe marcar la lease como perdida"

            # Restaurar antes de salir para que el teardown pueda cerrar.
            conn.close = cierre_real  # type: ignore[method-assign]
            lifecycle._poisoned_paths.clear()

    assert isinstance(causa, LockDatabaseUnavailableError), "la causa debe venir de la taxonomía del módulo"
    assert isinstance(causa.__cause__, DatabasePathPoisonedError), (
        "se perdió el diagnóstico de por qué no se pudo verificar"
    )
    gestor_snapshots.restore_snapshot.assert_not_called()
    gestor_snapshots.restore.assert_not_called()

    await lifecycle.shutdown_all()


# ===========================================================================
# T11 — el path envenenado habla la taxonomía de cada módulo
# ===========================================================================

# Qué excepción documenta cada wrapper para "no hay conexión utilizable". Un
# path envenenado tiene que llegar al llamador COMO ESA, o el `except` que ya
# tenía escrito deja de atrapar el caso.
#
# `router.py` y `async_registry.py` levantan `RuntimeError` pelado para ese
# estado y `DatabasePathPoisonedError` ES un `RuntimeError`, así que su contrato
# se cumple sin traducir. No es una excusa: lo verifica el subtest de abajo, y
# el día que alguno de los dos se dé una excepción propia, el ancla lo obliga a
# traducir como los otros.
TAXONOMIA_DE_PATH_ENVENENADO: dict[str, type[BaseException]] = {
    "journal": JournalConnectionError,
    "locks": LockDatabaseUnavailableError,
    "async_registry": RuntimeError,
    "router": RuntimeError,
}


async def _envenenar(lifecycle: DatabaseLifecycleManager, db: Path, conn: aiosqlite.Connection) -> None:
    async def _falla() -> None:
        raise sqlite3.OperationalError("close imposible")

    conn.close = _falla  # type: ignore[method-assign]
    await lifecycle.quarantine_connection(db, conn)
    assert lifecycle.is_path_poisoned(db)


def test_t11_la_taxonomia_cubre_a_toda_la_familia() -> None:
    """Cada wrapper 'revalida' declara con qué excepción sale un path envenenado."""
    esperados = {Path(r).stem for r, e in WRAPPERS_QUE_CACHEAN_CONEXION.items() if e == "revalida"}
    assert set(TAXONOMIA_DE_PATH_ENVENENADO) == esperados, (
        "un wrapper que revalida sin declarar su taxonomía dejaría escapar "
        "DatabasePathPoisonedError crudo por su API pública"
    )
    for modulo, excepcion in TAXONOMIA_DE_PATH_ENVENENADO.items():
        # Declarar `RuntimeError` sólo vale mientras el error del lifecycle YA
        # lo sea: es la única razón por la que esos wrappers no traducen. El día
        # que deje de serlo, este assert los manda a traducir como los otros.
        if excepcion is RuntimeError:
            assert issubclass(DatabasePathPoisonedError, RuntimeError), (
                f"{modulo}: el error envenenado dejó de ser RuntimeError; ahora hay que traducirlo"
            )


async def test_t11a_el_journal_traduce_a_su_excepcion(tmp_path: Path) -> None:
    db = tmp_path / "tax_journal.db"
    lifecycle = _lifecycle(db)
    journal = OperationJournal(str(db), lifecycle=lifecycle)
    await journal.open()
    conn = journal._db
    assert conn is not None
    cierre_real = conn.close

    await _envenenar(lifecycle, db, conn)

    with pytest.raises(JournalConnectionError) as excinfo:
        await journal.begin_transaction("no debería entrar")
    assert isinstance(excinfo.value.__cause__, DatabasePathPoisonedError), "se perdió el diagnóstico original"

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


async def test_t11b_locks_traduce_a_su_excepcion(tmp_path: Path) -> None:
    db = tmp_path / "tax_locks.db"
    lifecycle = _lifecycle(db)
    locks = DistributedLockManager(str(db), lifecycle=lifecycle)
    await locks.initialize()
    conn = locks._conn
    assert conn is not None
    cierre_real = conn.close

    await _envenenar(lifecycle, db, conn)

    with pytest.raises(LockDatabaseUnavailableError) as excinfo:
        await locks.get_lock_info("recurso")
    assert isinstance(excinfo.value.__cause__, DatabasePathPoisonedError)

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


async def test_t11c_async_registry_sale_como_runtime_error(tmp_path: Path) -> None:
    """La entrada 'RuntimeError' se verifica por comportamiento, no por la tabla."""
    db = tmp_path / "tax_registry.db"
    lifecycle = _lifecycle(db)
    registry = AsyncModRegistry(str(db), lifecycle=lifecycle)
    await registry.open()
    conn = registry._conn
    assert conn is not None
    cierre_real = conn.close

    await _envenenar(lifecycle, db, conn)

    with pytest.raises(TAXONOMIA_DE_PATH_ENVENENADO["async_registry"]):
        await registry.is_empty()

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


async def test_t11d_router_sale_como_runtime_error(tmp_path: Path) -> None:
    db = tmp_path / "tax_router.db"
    lifecycle = _lifecycle(db)
    router = LLMRouter(provider=MagicMock(), db_path=str(db), lifecycle=lifecycle)
    await router.open()
    conn = router._conn
    assert conn is not None
    cierre_real = conn.close

    await _envenenar(lifecycle, db, conn)

    with pytest.raises(TAXONOMIA_DE_PATH_ENVENENADO["router"]):
        await router._save_message("chat", "user", "hola")

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


async def test_t11e_el_journal_traduce_tambien_al_reabrir(tmp_path: Path) -> None:
    """El hermano de T11a: la traducción no puede vivir sólo en la rama cacheada.

    Con ``_db is None`` (tras un ``close()``) ``_ensure_connected`` va por
    ``open()``, que resuelve por el lifecycle y sólo atrapa ``sqlite3.Error``.
    Sin traducir ahí, el MISMO estado fail-closed daba dos excepciones distintas
    según si la instancia estaba abierta — que es justo la taxonomía rota que
    T11a creía estar anclando.
    """
    db = tmp_path / "tax_reopen.db"
    lifecycle = _lifecycle(db)
    a = OperationJournal(str(db), lifecycle=lifecycle)
    b = OperationJournal(str(db), lifecycle=lifecycle)
    await a.open()
    await b.open()
    conn = a._db
    assert conn is not None
    cierre_real = conn.close

    await a.close()
    assert a._db is None, "el escenario exige que A entre por la rama open()"
    await _envenenar(lifecycle, db, conn)

    with pytest.raises(JournalConnectionError) as excinfo:
        await a.begin_transaction("no debería entrar")
    assert isinstance(excinfo.value.__cause__, DatabasePathPoisonedError)

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


async def test_t11f_renew_lock_degrada_a_false_con_la_base_caida(tmp_path: Path) -> None:
    """`renew_lock` es best-effort: la base fail-closed no puede ser la excepción.

    Degrada CUALQUIER `sqlite3.Error` a False para que un fallo de base no mate
    el heartbeat con un traceback. Resolver la conexión quedó FUERA de ese try,
    así que un path envenenado era la única forma de fallo que rompía el
    contrato `-> bool`. Una lease que no se puede renovar es una lease perdida,
    igual que en `assert_owned`.
    """
    db = tmp_path / "renew.db"
    lifecycle = _lifecycle(db)
    locks = DistributedLockManager(str(db), lifecycle=lifecycle)
    await locks.initialize()
    await locks.acquire_lock("recurso", "agente")
    conn = locks._conn
    assert conn is not None
    cierre_real = conn.close

    await _envenenar(lifecycle, db, conn)

    assert await locks.renew_lock("recurso", "agente") is False, (
        "renew_lock levantó en vez de degradar: el heartbeat muere con un traceback"
    )

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


async def test_t12_init_all_no_puede_pisar_un_path_envenenado(tmp_path: Path) -> None:
    """``init_all`` entra por ``_init_single``, sin pasar por ``get_connection``.

    Registrar un reemplazo ahí pisaría la conexión que el path envenenado
    conserva: nadie a quien reintentarle el cierre en ``shutdown_all`` (veneno
    permanente) y la vieja huérfana, quizá todavía sosteniendo el write lock.
    Es el invariante que documenta ``_completar_retirada``, alcanzado por el
    otro camino de registro.
    """
    db = tmp_path / "init_envenenada.db"
    lifecycle = _lifecycle(db)
    conn = await lifecycle.get_connection(db)
    cierre_real = conn.close

    await _envenenar(lifecycle, db, conn)
    registrada = lifecycle._connections[str(db.resolve())]

    with pytest.raises(DatabasePathPoisonedError):
        await lifecycle.init_all()

    assert lifecycle._connections[str(db.resolve())] is registrada, (
        "init_all pisó la conexión del path envenenado: shutdown_all se queda sin "
        "a quién reintentarle el cierre y la vieja queda huérfana"
    )

    conn.close = cierre_real  # type: ignore[method-assign]
    await lifecycle.shutdown_all()


# ===========================================================================
# Capacidad del detector del ancla
# ===========================================================================


@pytest.mark.parametrize(
    ("nombre", "fuente"),
    [
        ("directo", "async def f(self):\n    self._c = await self._lc.get_connection(p)\n"),
        # El de dos pasos NO es hipotético: es `core/database.py:128-130`.
        ("dos_pasos", "async def f(self):\n    c = await self._lc.get_connection(p)\n    self._c = c\n"),
        ("anotado", "async def f(self):\n    self._c: object = await self._lc.get_connection(p)\n"),
        ("multiple", "async def f(self):\n    a = self._c = await self._lc.get_connection(p)\n"),
        ("refresh", "async def f(self):\n    self._c = await self._lc.refresh_connection(p, self._c)\n"),
    ],
)
def test_el_detector_no_es_evadible_por_el_idioma_de_asignacion(nombre: str, fuente: str) -> None:
    """El ancla vale lo que valga su detector: si se esquiva, no ancla nada."""
    assert _cachea_conexion(ast.parse(fuente)), f"el idioma '{nombre}' esquiva el ancla"


def test_el_detector_no_marca_a_quien_no_cachea() -> None:
    """Una conexión que vive en una local y no se guarda no cuenta (dlq_manager/governance)."""
    fuente = "async def f(self):\n    c = await self._lc.get_connection(p)\n    await c.execute('SELECT 1')\n"
    assert not _cachea_conexion(ast.parse(fuente))


# ===========================================================================
# Verificación de la exención declarada en el ancla
# ===========================================================================


async def test_database_agent_repide_la_conexion_en_cada_uso(tmp_path: Path) -> None:
    """`core/database.py` está exento por 'refetch': acá se verifica, no se afirma.

    Escribir el racional de una exclusión no cuenta como verificarla
    (`AGENTS.md`), así que la inmunidad estructural de `DatabaseAgent` —vuelve a
    pedirle la conexión al lifecycle en cada `_get_conn()`— se demuestra.
    """
    db = tmp_path / "agent.db"
    agent = DatabaseAgent(str(db))
    await agent.init_db()
    lifecycle = agent._lifecycle
    assert lifecycle is not None

    primera = await agent._get_conn()
    await lifecycle.quarantine_connection(db, primera)

    segunda = await agent._get_conn()
    assert segunda is not primera, "DatabaseAgent reusó una conexión invalidada"
    assert lifecycle.is_connection_current(db, segunda)

    await agent.close()

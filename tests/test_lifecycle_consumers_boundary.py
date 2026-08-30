"""PR-1b1 — los consumidores simples usan el boundary durante TODO su SQL.

Propiedad demostrada (carrera ejecutada, no ancla sintáctica): cuando un
consumidor migrado tiene una operación en vuelo sobre la conexión compartida,
``shutdown_all()`` **espera** a que esa operación libere el boundary antes de
cerrar — a diferencia del patrón viejo (``get_connection()`` + SQL suelto), en
el que el shutdown cerraba la conexión debajo del SQL.

Consumidores en scope: ``DatabaseAgent`` (lecturas), ``DLQManager._connect()``
y ``GovernanceManager`` (operaciones de cache). Además, política EXPLÍCITA de
``DatabaseLifecycleShuttingDownError`` en Governance.

Sincronización con ``asyncio.Event`` (barreras) + ``ceder()`` (vaciar la cola
de listos del loop). Sin sleeps.

Ronda de revisión PR-1b1 (hallazgos de @codex / @coderabbitai): además de las
carreras conductuales, dos anclas por introspección/AST que **enumeran** en vez
de muestrear (regla "enumerar, no muestrear" de AGENTS.md): (1) el conjunto de
lecturas públicas de ``DatabaseAgent`` migradas a ``_read_operation()``, con una
prueba funcional parametrizada por lectura; y (2) el inventario congelado de los
puntos de llamada a ``lifecycle.get_connection(...)`` en producción. El AST es
INVENTARIO, no prueba de seguridad conductual: la autoridad sobre "shutdown vs
SQL en vuelo" siguen siendo las carreras con barreras de ``asyncio.Event``
(jerarquía: conducta observable > estado interno > inventario AST).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from sky_claw.app.core.database import DatabaseAgent
from sky_claw.app.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.app.core.dlq_manager import DLQManager
from sky_claw.app.security.governance import GovernanceManager

DEADLINE = 5.0


async def ceder(veces: int = 40) -> None:
    """Vacía la cola de listos del loop (no es sleep de sincronización)."""
    for _ in range(veces):
        await asyncio.sleep(0)


def _mgr(*paths: Path) -> DatabaseLifecycleManager:
    return DatabaseLifecycleManager(
        db_paths=list(paths),
        config=DatabaseLifecycleConfig(enable_signal_handlers=False, enable_auto_checkpoint=False),
    )


# ---------------------------------------------------------------------------
# DatabaseAgent — lecturas bajo _read_operation()
# ---------------------------------------------------------------------------


async def test_databaseagent_lectura_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """Una lectura de DatabaseAgent en vuelo (``_read_operation``) mantiene la
    admisión: el shutdown no puede completarse hasta que la lectura sale."""
    agent = DatabaseAgent(str(tmp_path / "agent.db"))
    await agent.init_db()
    lifecycle = agent._lifecycle
    assert lifecycle is not None

    entro, puerta = asyncio.Event(), asyncio.Event()

    async def lectura_suspendida() -> None:
        async with agent._read_operation():
            entro.set()
            await puerta.wait()

    t_op = asyncio.create_task(lectura_suspendida())
    await entro.wait()
    # PROPIEDAD PR-1b1 (discriminante): la admisión sigue ACTIVA mientras la
    # lectura usa la conexión. La reversión a get_connection() —que suelta la
    # admisión antes del yield— deja _admitted en 0 y rompe esta prueba.
    assert lifecycle._admitted >= 1, "la lectura soltó la admisión antes de terminar el SQL"
    t_sd = asyncio.create_task(lifecycle.shutdown_all())
    await ceder()
    assert not t_sd.done(), "shutdown se completó con una lectura admitida en vuelo"

    puerta.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert lifecycle._admitted == 0


async def test_databaseagent_get_memory_end_to_end_sigue_leyendo(tmp_path: Path) -> None:
    """Guard de no-regresión: la lectura pública sigue devolviendo el valor."""
    agent = DatabaseAgent(str(tmp_path / "agent.db"))
    await agent.init_db()
    try:
        await agent.set_memory("clave", "valor", 1.0)
        assert await agent.get_memory("clave") == "valor"
        assert await agent.get_memory("ausente") is None
    finally:
        await agent.close()


async def test_databaseagent_carrera_conductual_contra_shutdown(tmp_path: Path) -> None:
    """Prueba conductual integral (NO inspecciona ``_admitted``): el cierre de
    ``shutdown_all()`` debe ESPERAR a que una lectura en vuelo libere el boundary.

    Reproduce fielmente el paso de cierre de ``shutdown_all`` —el único mutador
    que corre la conexión debajo de una operación—: ``async with
    get_write_lock(path): await conn.close()`` (``db_lifecycle.py``,
    ``_shutdown_all_under_transition`` paso 2). No se llama a ``shutdown_all()``
    entero porque su checkpoint TRUNCATE previo viaja al worker-thread de
    aiosqlite: ``ceder()`` (vaciar la cola de listos del loop) no lo puede
    conducir de forma determinista, así que "el shutdown llegó a cerrar" no sería
    observable sin timing. El paso de cierre aislado SÍ es determinista: la cola
    del worker es FIFO.

    - Boundary correcto (``operation()``): la lectura sostiene el write-lock del
      path; el cierre queda bloqueado tomándolo, la lectura corre su SELECT y
      recién al salir libera → el cierre procede. El SELECT lee su valor.
    - Reversión a ``get_connection()`` (sin boundary): la lectura NO sostiene el
      lock; el cierre lo toma y encola ``conn.close()`` en el worker ANTES de que
      la lectura encole su SELECT → el SELECT corre sobre la conexión ya cerrada
      → cierre prematuro observable (``ProgrammingError``/``ValueError``). La
      prueba falla por COMPORTAMIENTO, sin tocar ``_admitted``.
    """
    agent = DatabaseAgent(str(tmp_path / "agent.db"))
    await agent.init_db()
    await agent.set_memory("clave", "valor", 1.0)
    lifecycle = agent._lifecycle
    assert lifecycle is not None

    # La MISMA instancia singleton que la lectura usará y que shutdown cerraría.
    conn = await lifecycle.get_connection(agent.db_path)

    entro, puerta = asyncio.Event(), asyncio.Event()
    cerro = asyncio.Event()
    lectura: dict[str, object] = {"valor": None, "error": None}
    t_lec: asyncio.Task[None] | None = None
    t_close: asyncio.Task[None] | None = None

    async def lector() -> None:
        async with agent._read_operation() as c:
            entro.set()
            await puerta.wait()  # suspendida DENTRO del boundary, ANTES del SQL
            try:
                async with c.execute("SELECT value FROM agent_memory WHERE key = ?", ("clave",)) as cur:
                    row = await cur.fetchone()
                lectura["valor"] = row[0] if row else None
            except Exception as e:  # noqa: BLE001 — el cierre prematuro es justo lo que observamos
                lectura["error"] = f"{type(e).__name__}: {e}"

    async def cierre_como_shutdown() -> None:
        # Reproducción literal de shutdown_all paso 2 (cierre bajo el lock del path).
        async with lifecycle.get_write_lock(str(agent.db_path)):
            await conn.close()
            cerro.set()

    try:
        t_lec = asyncio.create_task(lector())
        await entro.wait()  # lector dentro del boundary, ANTES de su SQL
        t_close = asyncio.create_task(cierre_como_shutdown())
        await ceder()  # el cierre intenta tomar el lock TODO lo que pueda

        # Con el boundary, el cierre queda bloqueado en el write-lock que la
        # lectura sostiene: NO pudo cerrar todavía. (En la reversión el cierre
        # ya encoló su close en el worker; el discriminante fuerte es el SELECT
        # de abajo, pero esto documenta el mecanismo.)
        assert not cerro.is_set(), "el cierre completó con la lectura en vuelo (boundary roto)"

        puerta.set()  # liberar la lectura: recién ahora encola su SELECT
        await asyncio.wait_for(t_lec, timeout=DEADLINE)
        await asyncio.wait_for(t_close, timeout=DEADLINE)

        # DISCRIMINANTE CONDUCTUAL (sin ``_admitted``): con el boundary, el cierre
        # esperó y el SELECT leyó su valor. La reversión cierra la conexión debajo
        # del SELECT y esto queda en ``lectura["error"]``.
        assert lectura["error"] is None, f"cierre prematuro debajo del SELECT: {lectura['error']}"
        assert lectura["valor"] == "valor", "la lectura no leyó su valor"
        assert cerro.is_set(), "el cierre nunca ocurrió (debía diferirse, no perderse)"
    finally:
        puerta.set()
        for t in (t_lec, t_close):
            if t is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(t, timeout=DEADLINE)


# ---------------------------------------------------------------------------
# DLQManager — _connect() managed
# ---------------------------------------------------------------------------


async def test_dlq_connect_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """Un caller dentro de ``DLQManager._connect()`` mantiene la admisión."""
    path = tmp_path / "dlq.db"
    lifecycle = _mgr(path)
    dlq = DLQManager(db_path=path, handler_resolver={}.get, lifecycle=lifecycle)

    entro, puerta = asyncio.Event(), asyncio.Event()

    async def uso_suspendido() -> None:
        async with dlq._connect():
            entro.set()
            await puerta.wait()

    t_op = asyncio.create_task(uso_suspendido())
    await entro.wait()
    assert lifecycle._admitted >= 1, "_connect() soltó la admisión antes de terminar el SQL"
    t_sd = asyncio.create_task(lifecycle.shutdown_all())
    await ceder()
    assert not t_sd.done(), "shutdown se completó con _connect() admitido en vuelo"

    puerta.set()
    await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert lifecycle._admitted == 0


# ---------------------------------------------------------------------------
# GovernanceManager — boundary + política de shutdown
# ---------------------------------------------------------------------------


def _gov(tmp_path: Path, lifecycle: DatabaseLifecycleManager) -> GovernanceManager:
    # El autouse _reset_governance_singleton del conftest aísla el singleton.
    gov = GovernanceManager.get_instance(base_path=str(tmp_path))
    gov.set_lifecycle(lifecycle)
    return gov


async def test_governance_operacion_en_vuelo_bloquea_el_shutdown(tmp_path: Path) -> None:
    """``is_scanned_and_clean`` en vuelo (suspendida en ``_ensure_schema``, que
    corre DENTRO de ``operation()``) mantiene la admisión."""
    lifecycle = _mgr()
    gov = _gov(tmp_path, lifecycle)
    archivo = tmp_path / "f.bin"
    archivo.write_bytes(b"contenido")

    entro, puerta = asyncio.Event(), asyncio.Event()
    ensure_real = gov._ensure_schema

    async def ensure_con_barrera(db: object) -> None:
        entro.set()
        await puerta.wait()
        await ensure_real(db)

    gov._ensure_schema = ensure_con_barrera  # type: ignore[method-assign]

    t_op = asyncio.create_task(gov.is_scanned_and_clean(str(archivo)))
    await entro.wait()
    assert lifecycle._admitted >= 1, "la operación de Governance soltó la admisión antes de terminar el SQL"
    t_sd = asyncio.create_task(lifecycle.shutdown_all())
    await ceder()
    assert not t_sd.done(), "shutdown se completó con una operación de Governance admitida en vuelo"

    puerta.set()
    resultado = await asyncio.wait_for(t_op, timeout=DEADLINE)
    await asyncio.wait_for(t_sd, timeout=DEADLINE)
    assert resultado is False  # el hash no está cacheado
    assert lifecycle._admitted == 0


async def test_governance_is_scanned_durante_shutdown_es_fail_closed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Llegada tardía: con el lifecycle ya cerrado, ``is_scanned_and_clean``
    captura ``DatabaseLifecycleShuttingDownError`` y devuelve False (fail-closed)."""
    lifecycle = _mgr()
    gov = _gov(tmp_path, lifecycle)
    await lifecycle.shutdown_all()  # _shutting_down: rechaza admisión nueva

    archivo = tmp_path / "f.bin"
    archivo.write_bytes(b"contenido")

    with caplog.at_level("WARNING"):
        resultado = await gov.is_scanned_and_clean(str(archivo))

    assert resultado is False
    assert any("shutdown del lifecycle" in r.message for r in caplog.records), (
        "no se registró la política explícita de shutdown (fail-closed)"
    )


async def test_governance_update_durante_shutdown_no_persiste(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Llegada tardía: ``update_scan_result`` no persiste ni propaga; loggea la
    política. Al reabrir con ``init_all()``, el hash no quedó registrado."""
    lifecycle = _mgr()
    gov = _gov(tmp_path, lifecycle)
    await lifecycle.shutdown_all()

    archivo = tmp_path / "f.bin"
    archivo.write_bytes(b"contenido")

    with caplog.at_level("WARNING"):
        await gov.update_scan_result(str(archivo), [{"rule": "x"}], "CLEAN")  # no debe lanzar

    assert any("shutdown del lifecycle" in r.message for r in caplog.records), (
        "no se registró la política explícita de shutdown (no persistido)"
    )

    # Reabrir explícitamente y confirmar que NO se persistió nada.
    await lifecycle.init_all()
    try:
        assert await gov.is_scanned_and_clean(str(archivo)) is False
    finally:
        await lifecycle.shutdown_all()


# ===========================================================================
# Ronda de revisión PR-1b1 — anclas de enumeración (no muestreo)
# ===========================================================================
#
# Utilidades AST compartidas por las dos anclas. Detectan y CONGELAN el conjunto
# de superficies; NO analizan flujo de datos ni infieren "este get_connection
# corre SQL después" (eso sería un analizador semántico complejo, fuera de alcance).

_RAIZ_REPO = Path(__file__).resolve().parents[1]
_PAQUETE = _RAIZ_REPO / "sky_claw"


def _arbol_de(archivo: Path) -> ast.AST:
    return ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))


def _mapa_de_padres(arbol: ast.AST) -> dict[ast.AST, ast.AST]:
    """Enlaces hijo→padre (el AST de stdlib no los expone)."""
    padres: dict[ast.AST, ast.AST] = {}
    for nodo in ast.walk(arbol):
        for hijo in ast.iter_child_nodes(nodo):
            padres[hijo] = nodo
    return padres


def _qualname_envolvente(padres: dict[ast.AST, ast.AST], objetivo: ast.AST) -> str:
    """``Clase.metodo`` (o ``funcion``) del def que contiene a ``objetivo``."""
    nombres: list[str] = []
    cur: ast.AST | None = objetivo
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombres.append(cur.name)
        cur = padres.get(cur)
    return ".".join(reversed(nombres)) if nombres else "<module>"


def _es_llamada_self(nodo: ast.AST, metodo: str) -> bool:
    """True si ``nodo`` es una llamada ``self.<metodo>(...)``."""
    return (
        isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == metodo
        and isinstance(nodo.func.value, ast.Name)
        and nodo.func.value.id == "self"
    )


# ---------------------------------------------------------------------------
# Ancla 1 — lecturas públicas de DatabaseAgent migradas a _read_operation()
# ---------------------------------------------------------------------------

_LECTURAS_MIGRADAS = frozenset({"get_circuit_breaker_state", "get_memory", "get_mods", "get_conflicts"})


def _lecturas_por_read_operation() -> set[str]:
    """Métodos de ``database.py`` cuyo cuerpo usa ``self._read_operation()``."""
    arbol = _arbol_de(_PAQUETE / "app" / "core" / "database.py")
    padres = _mapa_de_padres(arbol)
    return {
        _qualname_envolvente(padres, nodo).split(".")[-1]
        for nodo in ast.walk(arbol)
        if _es_llamada_self(nodo, "_read_operation")
    }


def test_inventario_lecturas_migradas_esta_congelado() -> None:
    """Ancla de igualdad: las lecturas que pasan por ``_read_operation()`` son
    EXACTAMENTE las esperadas. Migrar una lectura nueva sin sumarla acá (y a la
    parametrización de abajo) rompe el test — enumera, no muestrea."""
    assert _lecturas_por_read_operation() == set(_LECTURAS_MIGRADAS)


# Prueba funcional por lectura migrada (+ ramas de filtro de get_mods/get_conflicts).
# NO repite la carrera de shutdown (esa tiene autoridad propia arriba): solo verifica
# que cada lectura sigue devolviendo datos correctos a través del boundary.


async def _leer_cb_default(agent: DatabaseAgent) -> object:
    return await agent.get_circuit_breaker_state("dominio.sin.estado")


async def _leer_cb_con_estado(agent: DatabaseAgent) -> object:
    await agent.update_circuit_breaker("nexusmods.com", 3, 987.0)
    estado = await agent.get_circuit_breaker_state("nexusmods.com")
    return (estado["failures"], estado["locked_until"])


async def _leer_memory_presente(agent: DatabaseAgent) -> object:
    await agent.set_memory("clave", "valor", 1.0)
    return await agent.get_memory("clave")


async def _leer_memory_ausente(agent: DatabaseAgent) -> object:
    return await agent.get_memory("ausente")


async def _leer_mods_sin_filtro(agent: DatabaseAgent) -> object:
    await agent.add_mod("ModA")
    await agent.add_mod("ModB")
    return sorted((f["name"], f["status"]) for f in await agent.get_mods())


async def _leer_mods_filtro_activos(agent: DatabaseAgent) -> object:
    await agent.add_mod("ModA")
    return sorted((f["name"], f["status"]) for f in await agent.get_mods(status="active"))


async def _leer_mods_filtro_vacio(agent: DatabaseAgent) -> object:
    await agent.add_mod("ModA")
    return await agent.get_mods(status="inactive")


async def _leer_conflicts_sin_filtro(agent: DatabaseAgent) -> object:
    m1 = await agent.add_mod("ModA")
    m2 = await agent.add_mod("ModB")
    await agent.add_conflict(m1, m2, "record")
    return [(f["mod_id_1"], f["mod_id_2"], bool(f["resolved"])) for f in await agent.get_conflicts()]


async def _leer_conflicts_no_resueltos(agent: DatabaseAgent) -> object:
    m1 = await agent.add_mod("ModA")
    m2 = await agent.add_mod("ModB")
    await agent.add_conflict(m1, m2, "record")
    filas = await agent.get_conflicts(resolved=False)
    return [(f["mod_id_1"], f["mod_id_2"], bool(f["resolved"])) for f in filas]


async def _leer_conflicts_resueltos_vacio(agent: DatabaseAgent) -> object:
    m1 = await agent.add_mod("ModA")
    m2 = await agent.add_mod("ModB")
    await agent.add_conflict(m1, m2, "record")
    return await agent.get_conflicts(resolved=True)


class _Escenario(NamedTuple):
    metodo: str
    sufijo: str
    operacion: Callable[[DatabaseAgent], Awaitable[object]]
    esperado: object


_ESCENARIOS_LECTURA = [
    _Escenario("get_circuit_breaker_state", "default", _leer_cb_default, {"failures": 0, "locked_until": 0}),
    _Escenario("get_circuit_breaker_state", "con_estado", _leer_cb_con_estado, (3, 987.0)),
    _Escenario("get_memory", "presente", _leer_memory_presente, "valor"),
    _Escenario("get_memory", "ausente", _leer_memory_ausente, None),
    _Escenario("get_mods", "sin_filtro", _leer_mods_sin_filtro, [("ModA", "active"), ("ModB", "active")]),
    _Escenario("get_mods", "filtro_activos", _leer_mods_filtro_activos, [("ModA", "active")]),
    _Escenario("get_mods", "filtro_vacio", _leer_mods_filtro_vacio, []),
    _Escenario("get_conflicts", "sin_filtro", _leer_conflicts_sin_filtro, [(1, 2, False)]),
    _Escenario("get_conflicts", "no_resueltos", _leer_conflicts_no_resueltos, [(1, 2, False)]),
    _Escenario("get_conflicts", "resueltos_vacio", _leer_conflicts_resueltos_vacio, []),
]


@pytest.mark.parametrize("esc", _ESCENARIOS_LECTURA, ids=[f"{e.metodo}:{e.sufijo}" for e in _ESCENARIOS_LECTURA])
async def test_lecturas_publicas_migradas_funcionan(tmp_path: Path, esc: _Escenario) -> None:
    """Prueba funcional integral de CADA lectura migrada (+ ramas de filtro): sigue
    devolviendo datos correctos a través de ``_read_operation()``."""
    agent = DatabaseAgent(str(tmp_path / "agent.db"))
    await agent.init_db()
    try:
        assert await esc.operacion(agent) == esc.esperado
    finally:
        await agent.close()


def test_los_escenarios_cubren_todas_las_lecturas_migradas() -> None:
    """La parametrización enumera EXACTAMENTE las lecturas migradas (no muestrea):
    su conjunto de métodos coincide con el ancla de introspección de arriba."""
    assert {e.metodo for e in _ESCENARIOS_LECTURA} == set(_LECTURAS_MIGRADAS)


# ---------------------------------------------------------------------------
# Ancla 2 — inventario de consumidores de lifecycle.get_connection(...)
# ---------------------------------------------------------------------------


class _Receta(NamedTuple):
    categoria: str
    call_sites: int
    motivo: str


_CATEGORIAS_PERMITIDAS = frozenset({"ACCESSOR_ALLOWED", "INIT_ALLOWED", "DEFERRED_PR_1B_COMPLEX"})

# Universo REAL (verificado por AST) de los puntos de llamada a ``<x>.get_connection(...)``
# en producción, EXCLUYENDO el propio DatabaseLifecycleManager (que la define). Clave:
# (ruta relativa, qualname del def envolvente). Los puntos de paso migrados en PR-1b1
# (DatabaseAgent._read_operation, DLQManager._connect, GovernanceManager) NO aparecen
# a propósito: ya no usan get_connection (pasan por operation()); no se inventan
# entradas SAFE/MIGRATED para ellos.
#
# PR-1b2a retiró las recetas de LLMRouter.open y DistributedLockManager.initialize:
# ambos ejecutan hoy su DDL dentro de operation() y ya no llaman a get_connection.
# PR-1b2b retiró las de AsyncModRegistry.open y OperationJournal.open: su SQL
# lifecycle-backed corre dentro de operation()/transaction() y tampoco llaman ya
# a get_connection (su quick_check/schema/reapertura entran por el boundary).
_CONSUMIDORES_GET_CONNECTION: dict[tuple[str, str], _Receta] = {
    ("sky_claw/app/core/database.py", "DatabaseAgent._get_conn"): _Receta(
        "ACCESSOR_ALLOWED",
        1,
        "Accesor: devuelve la conexión vigente, no corre SQL por sí mismo. Contrato "
        "congelado en test_core_async_transactions.py; sus 4 lecturas ya pasan por "
        "operation() (PR-1b1). No auto-permite accesores futuros: cada uno se enumera.",
    ),
    ("sky_claw/app/core/database.py", "DatabaseAgent.init_db"): _Receta(
        "INIT_ALLOWED",
        1,
        "Inicialización: guarda en caché self._conn tras init_all(); el DDL del esquema "
        "corre bajo _write_transaction() (boundary transaccional), no suelto tras el get_connection.",
    ),
}

_DB_LIFECYCLE = _PAQUETE / "app" / "core" / "db_lifecycle.py"


def _inventario_real_get_connection() -> dict[tuple[str, str], int]:
    """``{(ruta_rel, qualname): nº de puntos de llamada}`` de ``.get_connection(...)``
    en producción, excluyendo ``db_lifecycle.py`` (el gestor que la define)."""
    conteo: dict[tuple[str, str], int] = {}
    for archivo in sorted(_PAQUETE.rglob("*.py")):
        if archivo == _DB_LIFECYCLE:
            continue
        arbol = _arbol_de(archivo)
        padres = _mapa_de_padres(arbol)
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == "get_connection"
            ):
                clave = (archivo.relative_to(_RAIZ_REPO).as_posix(), _qualname_envolvente(padres, nodo))
                conteo[clave] = conteo.get(clave, 0) + 1
    return conteo


def test_inventario_de_consumidores_get_connection_esta_congelado() -> None:
    """Ancla estructural (Codex P1): el conjunto de puntos de llamada a
    ``lifecycle.get_connection(...)`` en producción está TOTALMENTE inventariado.

    Igualdad exhaustiva (no ``subset <=``): un consumidor nuevo no listado —o un
    punto de llamada extra en uno existente— rompe el test y exige asignarle receta.
    Un consumidor diferido que se migre también lo rompe (hay que quitarlo del mapa):
    el inventario se mantiene fiel al código real.

    LÍMITE HONESTO: es INVENTARIO, no prueba de que cada consumidor respete el
    boundary en tiempo de ejecución. La seguridad conductual (shutdown vs SQL en
    vuelo) la prueban las carreras con barreras de ``asyncio.Event`` de este módulo;
    acá no hay análisis de flujo de datos, solo se congela el conjunto de superficies.
    """
    esperado = {clave: receta.call_sites for clave, receta in _CONSUMIDORES_GET_CONNECTION.items()}
    assert _inventario_real_get_connection() == esperado

    categorias = {receta.categoria for receta in _CONSUMIDORES_GET_CONNECTION.values()}
    assert categorias <= set(_CATEGORIAS_PERMITIDAS)


# ---------------------------------------------------------------------------
# Ancla 3 — llamadores de producción del accesor permitido _get_conn (Codex P1)
# ---------------------------------------------------------------------------
#
# El inventario de Ancla 2 permite ``DatabaseAgent._get_conn`` como accesor, pero
# eso NO congela quién lo llama. Una lectura futura con ``conn = await
# self._get_conn()`` seguida de SQL fuera del boundary evadiría a las dos anclas de
# arriba (la 2 solo ve ``.get_connection()``; la 1 solo ve usuarios de
# ``_read_operation()``). Esta ancla congela la propiedad reproducida hoy:
# ``_get_conn`` no tiene NINGÚN llamador de producción (sus 4 lecturas ya pasan por
# ``operation()``). Un llamador nuevo → test ROJO → decisión humana explícita
# (migrarlo a ``_read_operation()``/``operation()`` o justificar otra receta).

# Igualdad exhaustiva contra el conjunto vacío (no una allowlist ampliable ni un
# ``subconjunto <=``): hoy el conjunto REAL de llamadores es ∅.
_LLAMADORES_GET_CONN_ESPERADOS: frozenset[tuple[str, str]] = frozenset()


def _llamadores_de_get_conn() -> set[tuple[str, str]]:
    """``{(ruta_rel, qualname)}`` de las llamadas ``<x>._get_conn()`` en producción.

    ``_get_conn`` es privado de ``DatabaseAgent`` y no existe otro método con ese
    nombre en producción, así que detectar cualquier atributo ``._get_conn`` llamado
    (incluido ``self._get_conn()``) es exacto y exhaustivo.
    """
    llamadores: set[tuple[str, str]] = set()
    for archivo in sorted(_PAQUETE.rglob("*.py")):
        arbol = _arbol_de(archivo)
        padres = _mapa_de_padres(arbol)
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "_get_conn":
                llamadores.add((archivo.relative_to(_RAIZ_REPO).as_posix(), _qualname_envolvente(padres, nodo)))
    return llamadores


def test_get_conn_no_tiene_llamadores_de_produccion() -> None:
    """Ancla estructural (Codex P1): ``DatabaseAgent._get_conn`` se conserva por
    contrato, pero HOY ningún consumidor de producción lo usa para emitir SQL.

    Congela esa propiedad por igualdad exhaustiva (== ∅): cualquier
    ``self._get_conn()`` nuevo rompe el test y fuerza una decisión (migrarlo al
    boundary o justificar otra receta). Cierra la indirección que el inventario de
    ``get_connection()`` no ve: un llamador de ``_get_conn`` no cambia ese inventario
    ni el de ``_read_operation()``.

    LÍMITE HONESTO: es INVENTARIO, no prueba conductual; la carrera "shutdown vs SQL
    en vuelo" ya la cubren los tests con barreras de este módulo.
    """
    assert _llamadores_de_get_conn() == set(_LLAMADORES_GET_CONN_ESPERADOS)

"""El token de fencing ``acquired_at`` tiene que ser ÚNICO por adquisición, también con un reloj grueso.

**El defecto (visto en CI, Windows py3.11).** ``acquire_lock`` acuñaba el token con
``time.time()``. En Windows con Python < 3.13 ese reloj avanza a saltos de ~15,6 ms, así
que dos adquisiciones del mismo ``resource_id`` y ``agent_id`` dentro del mismo tick
recibían el MISMO token. El fencing de #742 se apoya en que el token distinga una lease
de la que la reemplazó (``_RELEASE_SQL`` / ``_RENEW_SQL`` matchean ``acquired_at``): con
tokens iguales, el ``release`` tardío de un titular viejo borraba la lease readquirida
por un hermano. ``test_lock_cancelacion_adquisicion.py::test_la_liberacion_es_idempotente_...``
lo mostró como ``assert 1792426825.2883885 != 1792426825.2883885``.

**La propiedad.** El token es estrictamente creciente por base de datos dentro del proceso
(``_acuniar_token``): lo acuña un único punto y lo usan los DOS caminos de ``acquire_lock``
(con y sin ``lifecycle``), que son hermanos en el mismo archivo.

**Cómo se prueba.** Se simula el reloj grueso fijando ``time.time`` (el mecanismo que ya usa
``test_distributed_locks.py``) en vez de depender de la resolución del runner: el defecto
deja de ser intermitente y se reproduce en Linux. El ancla por AST enumera los sitios que
acuñan token y falla si aparece uno que no pase por ``_acuniar_token``.

**Límite declarado.** La garantía es por proceso. Dos procesos distintos que acuñen en el
mismo tick para el mismo recurso y ``agent_id`` seguirían pudiendo coincidir; cerrarlo
requiere una identidad persistida (columna nueva en ``resource_locks``), que es un cambio
de esquema y queda fuera de este PR.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
from collections.abc import AsyncGenerator

import pytest

from sky_claw.app.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.app.db import locks
from sky_claw.app.db.locks import DistributedLockManager

RECURSO = "recurso-token-creciente"
AGENTE = "servicio-con-id-constante"

#: Valor real observado en el fallo de CI de Windows.
RELOJ_GRUESO = 1_792_426_825.2883885


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "locks.db"


async def _abrir_gestor(
    db_path: pathlib.Path, modo: str
) -> tuple[DistributedLockManager, DatabaseLifecycleManager | None]:
    lifecycle: DatabaseLifecycleManager | None = None
    if modo == "lifecycle":
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db_path],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
    gestor = DistributedLockManager(
        db_path, lifecycle=lifecycle, default_ttl=30.0, max_retries=2, backoff_base=0.01, backoff_max=0.02
    )
    await gestor.initialize()
    return gestor, lifecycle


@pytest.fixture(params=["legacy", "lifecycle"])
async def gestor(request: pytest.FixtureRequest, db_path: pathlib.Path) -> AsyncGenerator[DistributedLockManager, None]:
    """El gestor en los DOS modos de conexión: son los dos sitios que acuñan el token."""
    mgr, lifecycle = await _abrir_gestor(db_path, request.param)
    try:
        yield mgr
    finally:
        try:
            await mgr.close()
        finally:
            if lifecycle is not None:
                await lifecycle.shutdown_all()


def _fila_en_disco(db_path: pathlib.Path, recurso: str = RECURSO) -> tuple[str, float] | None:
    """Lee la lease con una conexión INDEPENDIENTE (lo que verá la próxima instancia)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        fila = conn.execute(
            "SELECT agent_id, acquired_at FROM resource_locks WHERE resource_id = ?", (recurso,)
        ).fetchone()
    finally:
        conn.close()
    return None if fila is None else (fila[0], fila[1])


def _congelar_reloj(monkeypatch: pytest.MonkeyPatch, valores: list[float] | float) -> None:
    """Fija ``time.time`` (reloj grueso). Una lista se consume en orden y repite el último valor."""
    secuencia = [valores] if isinstance(valores, float) else list(valores)

    def _reloj() -> float:
        return secuencia.pop(0) if len(secuencia) > 1 else secuencia[0]

    monkeypatch.setattr("sky_claw.app.db.locks.time.time", _reloj)


async def test_con_reloj_grueso_dos_adquisiciones_sucesivas_tienen_tokens_distintos(
    gestor: DistributedLockManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _congelar_reloj(monkeypatch, RELOJ_GRUESO)

    primera = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)
    assert await gestor.release_lock(RECURSO, AGENTE, acquired_at=primera.acquired_at)
    segunda = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)

    assert segunda.acquired_at > primera.acquired_at, (
        "el token de la segunda adquisición no puede igualar al de la primera aunque el reloj no haya avanzado"
    )


async def test_el_release_tardio_con_el_token_viejo_no_borra_la_lease_readquirida_con_reloj_grueso(
    gestor: DistributedLockManager, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La consecuencia real: sin tokens distintos, el titular viejo borraba la lease del hermano."""
    _congelar_reloj(monkeypatch, RELOJ_GRUESO)

    vieja = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)
    assert await gestor.release_lock(RECURSO, AGENTE, acquired_at=vieja.acquired_at)
    nueva = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)

    # El titular viejo se libera por segunda vez con SU token (p. ej. un __aexit__ repetido).
    assert not await gestor.release_lock(RECURSO, AGENTE, acquired_at=vieja.acquired_at)

    assert _fila_en_disco(db_path) == (AGENTE, nueva.acquired_at), "el release tardío borró la lease readquirida"


async def test_el_token_sigue_creciendo_aunque_el_reloj_retroceda(
    gestor: DistributedLockManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un salto hacia atrás del reloj de pared (NTP) no puede repetir ni bajar un token ya emitido."""
    _congelar_reloj(monkeypatch, [RELOJ_GRUESO, RELOJ_GRUESO - 3600.0])

    primera = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)
    assert await gestor.release_lock(RECURSO, AGENTE, acquired_at=primera.acquired_at)
    segunda = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)

    assert segunda.acquired_at > primera.acquired_at


async def test_el_token_coincide_con_el_reloj_cuando_el_reloj_avanza(
    gestor: DistributedLockManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin colisión el token es el reloj tal cual: el fix no desplaza los valores en el caso normal."""
    _congelar_reloj(monkeypatch, [RELOJ_GRUESO, RELOJ_GRUESO + 1.0])

    primera = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)
    assert await gestor.release_lock(RECURSO, AGENTE, acquired_at=primera.acquired_at)
    segunda = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)

    assert primera.acquired_at == RELOJ_GRUESO
    assert segunda.acquired_at == RELOJ_GRUESO + 1.0


async def test_dos_gestores_sobre_la_misma_base_comparten_la_monotonia(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Los servicios comparten ``agent_id`` y pueden tener cada uno su gestor sobre la MISMA base."""
    _congelar_reloj(monkeypatch, RELOJ_GRUESO)
    uno, _ = await _abrir_gestor(db_path, "legacy")
    otro, _ = await _abrir_gestor(db_path, "legacy")
    try:
        primera = await uno.acquire_lock(RECURSO, AGENTE, ttl=60.0)
        assert await uno.release_lock(RECURSO, AGENTE, acquired_at=primera.acquired_at)
        segunda = await otro.acquire_lock(RECURSO, AGENTE, ttl=60.0)

        assert segunda.acquired_at > primera.acquired_at
    finally:
        await uno.close()
        await otro.close()


async def test_bases_distintas_no_se_acoplan(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La monotonía es por base: la primera adquisición de otra base sigue siendo el reloj tal cual."""
    _congelar_reloj(monkeypatch, RELOJ_GRUESO)
    uno, _ = await _abrir_gestor(tmp_path / "uno.db", "legacy")
    otro, _ = await _abrir_gestor(tmp_path / "otro.db", "legacy")
    try:
        a = await uno.acquire_lock(RECURSO, AGENTE, ttl=60.0)
        assert await uno.release_lock(RECURSO, AGENTE, acquired_at=a.acquired_at)
        await uno.acquire_lock(RECURSO, AGENTE, ttl=60.0)  # empuja el estado de la base "uno"
        b = await otro.acquire_lock(RECURSO, AGENTE, ttl=60.0)

        assert b.acquired_at == RELOJ_GRUESO
    finally:
        await uno.close()
        await otro.close()


# ---------------------------------------------------------------------------
# Ancla: los sitios que acuñan un token se ENUMERAN, no se muestrean
# ---------------------------------------------------------------------------

_LOCKS_PY = pathlib.Path(locks.__file__).resolve()


def _acquire_lock() -> ast.AsyncFunctionDef:
    arbol = ast.parse(_LOCKS_PY.read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "acquire_lock":
            return nodo
    raise AssertionError("acquire_lock desapareció de locks.py: actualizá el ancla")


def _nombre_punteado(nodo: ast.AST) -> str:
    if isinstance(nodo, ast.Name):
        return nodo.id
    if isinstance(nodo, ast.Attribute):
        return f"{_nombre_punteado(nodo.value)}.{nodo.attr}"
    return ""


def test_todo_sitio_que_acuna_un_token_pasa_por_acuniar_token() -> None:
    """Hay exactamente DOS sitios que insertan una lease (con y sin lifecycle) y los dos acuñan igual.

    Un tercer camino que escriba ``_ACQUIRE_SQL`` con su propio ``time.time()`` reabre el defecto en
    silencio: este test lo obliga a decidir su receta.
    """
    funcion = _acquire_lock()

    inserciones = [
        nodo
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call)
        and _nombre_punteado(nodo.func).endswith("execute")
        and nodo.args
        and _nombre_punteado(nodo.args[0]) == "_ACQUIRE_SQL"
    ]
    assert len(inserciones) == 2, "cambió la familia de sitios que insertan una lease: decidí la receta del nuevo"

    for insercion in inserciones:
        params = insercion.args[1]
        assert isinstance(params, ast.Tuple) and len(params.elts) == 4
        assert _nombre_punteado(params.elts[2]) == "now", "el token de la inserción no sale de la variable acuñada"

    asignaciones = [
        nodo
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Assign) and any(_nombre_punteado(t) == "now" for t in nodo.targets)
    ]
    assert len(asignaciones) == len(inserciones), "hay una asignación de `now` sin su inserción (o al revés)"
    for asignacion in asignaciones:
        valor = asignacion.value
        assert isinstance(valor, ast.Call) and _nombre_punteado(valor.func) == "self._acuniar_token", (
            "el token se acuña con `time.time()` directo: usá `self._acuniar_token()`"
        )

    construcciones = [
        nodo for nodo in ast.walk(funcion) if isinstance(nodo, ast.Call) and _nombre_punteado(nodo.func) == "LockInfo"
    ]
    assert construcciones, "acquire_lock dejó de construir LockInfo"
    for construccion in construcciones:
        token = next(p.value for p in construccion.keywords if p.arg == "acquired_at")
        assert _nombre_punteado(token) == "now", "el LockInfo devuelto no lleva el token acuñado"

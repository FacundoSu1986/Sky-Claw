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
from sky_claw.app.db.locks import DistributedLockManager, LockAcquisitionError

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


def _vence_en_disco(db_path: pathlib.Path, recurso: str = RECURSO) -> float | None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        fila = conn.execute("SELECT expires_at FROM resource_locks WHERE resource_id = ?", (recurso,)).fetchone()
    finally:
        conn.close()
    return None if fila is None else fila[0]


def _insertar_lease_ajena(
    db_path: pathlib.Path, recurso: str, agente: str, *, acquired_at: float, expires_at: float
) -> None:
    """Simula la lease que dejó OTRO proceso: escribe la fila por una conexión independiente."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO resource_locks (resource_id, agent_id, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (recurso, agente, acquired_at, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


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


async def test_tras_un_retroceso_del_reloj_la_expiracion_sigue_al_reloj_real(
    gestor: DistributedLockManager, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El token puede quedar adelantado al reloj; la expiración NO: es el crash-recovery por TTL.

    Si ``expires_at`` se derivara del token, una lease de 60 s tomada tras un retroceso de 1 h solo
    sería reclamable ~1 h después de que su titular muriera.
    """
    _congelar_reloj(monkeypatch, [RELOJ_GRUESO, RELOJ_GRUESO - 3600.0])

    primera = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)
    assert await gestor.release_lock(RECURSO, AGENTE, acquired_at=primera.acquired_at)
    segunda = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)

    assert segunda.acquired_at > primera.acquired_at, "el token sigue creciendo"
    assert segunda.expires_at == (RELOJ_GRUESO - 3600.0) + 60.0, "la expiración sale del reloj real"
    assert _vence_en_disco(db_path) == segunda.expires_at


async def test_un_token_adelantado_al_reloj_no_pisa_una_lease_viva_de_otro_proceso(
    gestor: DistributedLockManager, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El predicado ``expires_at < ?`` se mide con el reloj real: un token adelantado no vence leases ajenas.

    Otro proceso, con su propio estado de monotonía, tomó la lease con el reloj ya retrocedido. Este
    proceso conserva el máximo de antes del retroceso; si usara ese token como "ahora" del predicado,
    daría por vencida (y pisaría) una lease que sigue viva.
    """
    _congelar_reloj(monkeypatch, [RELOJ_GRUESO, RELOJ_GRUESO - 3600.0])
    primera = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)
    assert await gestor.release_lock(RECURSO, AGENTE, acquired_at=primera.acquired_at)

    ahora = RELOJ_GRUESO - 3600.0
    _insertar_lease_ajena(db_path, RECURSO, "otro-proceso", acquired_at=ahora - 1.0, expires_at=ahora + 60.0)

    with pytest.raises(LockAcquisitionError):
        await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)

    assert _fila_en_disco(db_path) == ("otro-proceso", ahora - 1.0), "se pisó una lease viva de otro proceso"


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

    # (resource_id, agent_id, token, expires_at, ahora): el token es la IDENTIDAD de la lease; el
    # reloj real gobierna la expiración y el predicado de vencimiento (``expires_at < ?``).
    for insercion in inserciones:
        params = insercion.args[1]
        assert isinstance(params, ast.Tuple) and len(params.elts) == 5
        assert _nombre_punteado(params.elts[2]) == "token", "el token de la inserción no sale de la variable acuñada"
        assert _nombre_punteado(params.elts[3]) == "expires_at"
        assert _nombre_punteado(params.elts[4]) == "ahora", (
            "el predicado de vencimiento tiene que medirse con el reloj real, no con el token"
        )

    def _asignaciones(nombre: str) -> list[ast.Assign]:
        return [
            nodo
            for nodo in ast.walk(funcion)
            if isinstance(nodo, ast.Assign) and any(_nombre_punteado(t) == nombre for t in nodo.targets)
        ]

    tokens = _asignaciones("token")
    assert len(tokens) == len(inserciones), "hay una asignación de `token` sin su inserción (o al revés)"
    for asignacion in tokens:
        valor = asignacion.value
        assert isinstance(valor, ast.Call) and _nombre_punteado(valor.func) == "self._acuniar_token", (
            "el token se acuña con `time.time()` directo: usá `self._acuniar_token(ahora)`"
        )

    relojes = _asignaciones("ahora")
    assert len(relojes) == len(inserciones), "hay una lectura del reloj sin su inserción (o al revés)"
    for asignacion in relojes:
        valor = asignacion.value
        assert isinstance(valor, ast.Call) and _nombre_punteado(valor.func) == "time.time"

    vencimientos = _asignaciones("expires_at")
    assert len(vencimientos) == len(inserciones)
    for asignacion in vencimientos:
        valor = asignacion.value
        assert isinstance(valor, ast.BinOp) and _nombre_punteado(valor.left) == "ahora", (
            "`expires_at` se deriva del token: tras un retroceso del reloj la lease viviría de más"
        )

    construcciones = [
        nodo for nodo in ast.walk(funcion) if isinstance(nodo, ast.Call) and _nombre_punteado(nodo.func) == "LockInfo"
    ]
    assert construcciones, "acquire_lock dejó de construir LockInfo"
    for construccion in construcciones:
        token = next(p.value for p in construccion.keywords if p.arg == "acquired_at")
        assert _nombre_punteado(token) == "token", "el LockInfo devuelto no lleva el token acuñado"


def _referencias_a(raiz: ast.AST, nombre: str) -> list[ast.Name]:
    """Lecturas del identificador ``nombre`` bajo ``raiz`` (cualquier método, clase o función anidada)."""
    return [
        nodo
        for nodo in ast.walk(raiz)
        if isinstance(nodo, ast.Name) and nodo.id == nombre and isinstance(nodo.ctx, ast.Load)
    ]


def test_acquire_sql_solo_se_usa_en_los_dos_sitios_de_acquire_lock() -> None:
    """Censo de TODO el módulo: un método hermano con su propio reloj reabriría el defecto en silencio.

    El ancla de arriba mira únicamente ``acquire_lock``; un ``_adquirir_rapido`` que ejecute
    ``_ACQUIRE_SQL`` con su propio ``time.time()`` le pasaba por al lado. Esta cuenta las lecturas de
    ``_ACQUIRE_SQL`` en el archivo entero y exige que sean exactamente las dos de ``acquire_lock``.
    Límite: no ve referencias dinámicas (``globals()["_ACQUIRE_SQL"]``, ``getattr``).
    """
    arbol = ast.parse(_LOCKS_PY.read_text(encoding="utf-8"))
    acquire = next(
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.AsyncFunctionDef) and nodo.name == "acquire_lock"
    )

    en_el_modulo = len(_referencias_a(arbol, "_ACQUIRE_SQL"))
    en_acquire_lock = len(_referencias_a(acquire, "_ACQUIRE_SQL"))

    assert en_acquire_lock == 2, "cambió la familia de sitios que insertan una lease en acquire_lock"
    assert en_el_modulo == en_acquire_lock, (
        f"`_ACQUIRE_SQL` se usa {en_el_modulo - en_acquire_lock} vez/veces fuera de acquire_lock: un tercer "
        "sitio que inserte una lease tiene que acuñar con `_acuniar_token` y medir el vencimiento con el "
        "reloj real; decidí su receta y sumalo al ancla de arriba."
    )


def test_el_censo_de_referencias_ve_metodos_hermanos_clases_y_alias() -> None:
    fuente = (
        "X = 1\n"
        "async def a():\n"
        "    y = X\n"
        "    return X\n"
        "class C:\n"
        "    async def b(self):\n"
        "        return X\n"
        "def otro():\n"
        "    return X2\n"
    )
    arbol = ast.parse(fuente)

    assert len(_referencias_a(arbol, "X")) == 3  # la asignación de la línea 1 es Store y no cuenta
    assert len(_referencias_a(arbol, "X2")) == 1
    assert len(_referencias_a(arbol, "Y")) == 0

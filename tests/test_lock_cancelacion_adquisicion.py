"""#741 — una cancelación durante la adquisición no puede dejar la lease viva.

Familia: ``SnapshotTransactionLock.__aenter__`` → ``DistributedLockManager.acquire_lock``.

La ventana del defecto: la fila de ``resource_locks`` YA quedó persistida (el
commit terminó en el hilo de aiosqlite) y la tarea se cancela antes de que el
``await`` de ``acquire_lock`` retorne. El ``except BaseException`` que libera la
lease envolvía SÓLO la creación de snapshots, así que la cancelación propagaba
con la fila viva hasta su TTL y la instancia siguiente veía ``OCUPADO``.

**Cómo se fijan las ventanas.** Con EVENTOS sobre ``commit`` / ``acquire_lock``,
nunca con sleeps: la propiedad es de ORDENAMIENTO (la cancelación tiene que
entregarse con la fila ya escrita), así que la prueba tiene que decidir cuándo
corre cada cosa, no esperar a que pase. El único ``await asyncio.sleep(0)`` de
cada test es un yield de scheduling para que la cancelación se entregue ANTES
de abrir la puerta — no una espera por tiempo.

**Cómo se lee la lease.** Con una conexión INDEPENDIENTE (``_fila_en_disco``),
nunca con la del manager: en modo legacy, una transacción abierta deja la fila
visible para la MISMA conexión aunque jamás se haya commiteado, y el invariante
que importa es el de la instancia siguiente. El oráculo tiene que ser el mismo
que va a sufrir la contención espuria.

Las tres ventanas de la adquisición se enumeran explícitamente:

- ``antes_del_commit`` — la mutación está EN VUELO (nada commiteado todavía).
  Es el guard de la carrera que el fix no puede introducir: un cleanup eager
  borraría antes de que el commit escriba, y la fila sobreviviría igual.
- ``tras_el_commit`` — la fila está persistida y el ``await`` no retornó.
  Es el escenario del issue; sin el fix, la lease queda viva.
- ``tras_el_retorno`` — ``acquire_lock`` ya devolvió el ``LockInfo`` y la
  cancelación llega antes de que ``__aenter__`` lo publique. Es la
  reproducción del issue (demora inyectada *después* de persistir).

Las tres tienen que terminar sin fila. Además se verifican las dos propiedades
que separan un fix seguro de uno ingenuo (liberar por ``resource_id`` +
``agent_id``): la compensación NO puede tocar la lease de otro agente, ni la de
un hermano con el MISMO ``agent_id`` pero otro token ``acquired_at``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from sky_claw.app.core.db_lifecycle import DatabaseLifecycleConfig, DatabaseLifecycleManager
from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    LockInfo,
    LockReleaseError,
    SnapshotTransactionLock,
)
from sky_claw.app.db.snapshot_manager import FileSnapshotManager

if TYPE_CHECKING:
    import pathlib

#: Recurso y agente de la familia. El ``agent_id`` constante es deliberado: es
#: el caso que rompe una compensación por ``resource_id + agent_id`` (dos
#: instancias del mismo servicio comparten el id).
RECURSO = "dyndolod-ownership-741"
AGENTE = "dyndolod-owner-741"


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "locks.db"


@pytest.fixture(params=["legacy", "lifecycle"])
async def gestor(
    request: pytest.FixtureRequest,
    db_path: pathlib.Path,
) -> AsyncGenerator[DistributedLockManager, None]:
    """``DistributedLockManager`` en los DOS modos de conexión.

    ``legacy`` = conexión propia con commit manual (pre-M-01).
    ``lifecycle`` = conexión del ``DatabaseLifecycleManager``, cuyo ``transaction()``
    es el dueño del commit y ya completa la operación pese a la cancelación
    (``_await_db_operation``): el modo donde el COMMIT_WINS es explícito.
    """
    opciones = {"default_ttl": 30.0, "max_retries": 2, "backoff_base": 0.01, "backoff_max": 0.02}
    lifecycle: DatabaseLifecycleManager | None = None
    if request.param == "lifecycle":
        lifecycle = DatabaseLifecycleManager(
            db_paths=[db_path],
            config=DatabaseLifecycleConfig(enable_signal_handlers=False),
        )
    mgr = DistributedLockManager(db_path, lifecycle=lifecycle, **opciones)
    await mgr.initialize()
    try:
        yield mgr
    finally:
        # finally anidado: shutdown_all() tiene que correr aunque close() falle,
        # o los hilos de aiosqlite cuelgan la sesión de pytest.
        await mgr.close()
        if lifecycle is not None:
            await lifecycle.shutdown_all()


@pytest.fixture
async def snapshots(tmp_path: pathlib.Path) -> AsyncGenerator[FileSnapshotManager, None]:
    manager = FileSnapshotManager(snapshot_dir=tmp_path / "snapshots")
    await manager.initialize()
    yield manager


def _fila_en_disco(db_path: pathlib.Path) -> tuple[str, float] | None:
    """Lee ``resource_locks`` con una conexión INDEPENDIENTE.

    Es el oráculo de "la próxima instancia ve OCUPADO": una lectura sobre la
    conexión del manager vería la fila de una transacción todavía abierta, que
    ninguna otra instancia puede ver.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        fila = conn.execute(
            "SELECT agent_id, acquired_at FROM resource_locks WHERE resource_id = ?",
            (RECURSO,),
        ).fetchone()
    finally:
        conn.close()
    return None if fila is None else (fila[0], fila[1])


def _contexto(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    *,
    agent_id: str = AGENTE,
    auto_renew: bool = False,
) -> SnapshotTransactionLock:
    return SnapshotTransactionLock(
        lock_manager=gestor,
        snapshot_manager=snapshots,
        resource_id=RECURSO,
        agent_id=agent_id,
        target_files=[],
        auto_renew=auto_renew,
    )


def _instalar_ventana(
    monkeypatch: pytest.MonkeyPatch,
    ventana: str,
) -> tuple[asyncio.Event, asyncio.Event, asyncio.Event]:
    """Fija la ventana de cancelación sobre la adquisición.

    Returns
    -------
    tuple
        ``(alcanzada, liberar, commit_ejecutado)``: el evento que avisa que la
        tarea está DENTRO de la ventana, la puerta que el test abre para dejarla
        seguir, y el evento que prueba que el commit llegó a ejecutarse (para
        distinguir "liberó la lease" de "nunca la escribió").
    """
    alcanzada = asyncio.Event()
    liberar = asyncio.Event()
    commit_ejecutado = asyncio.Event()

    if ventana == "antes_del_insert":
        # Cancelación antes de adquirir (antes del INSERT).
        original_acquire = DistributedLockManager.acquire_lock

        async def acquire_con_puerta_previa(
            self: DistributedLockManager,
            resource_id: str,
            agent_id: str,
            ttl: float | None = None,
        ) -> LockInfo:
            alcanzada.set()
            await liberar.wait()
            return await original_acquire(self, resource_id, agent_id, ttl)

        monkeypatch.setattr(DistributedLockManager, "acquire_lock", acquire_con_puerta_previa)
        return alcanzada, liberar, commit_ejecutado

    if ventana == "tras_el_retorno":
        # Reproducción del issue: la demora se inyecta DESPUÉS de que la
        # adquisición persistió y devolvió el LockInfo.
        original_acquire = DistributedLockManager.acquire_lock

        async def acquire_con_puerta(
            self: DistributedLockManager,
            resource_id: str,
            agent_id: str,
            ttl: float | None = None,
        ) -> LockInfo:
            info = await original_acquire(self, resource_id, agent_id, ttl)
            alcanzada.set()
            await liberar.wait()
            return info

        monkeypatch.setattr(DistributedLockManager, "acquire_lock", acquire_con_puerta)
        return alcanzada, liberar, commit_ejecutado

    original_commit = aiosqlite.Connection.commit
    primera = {"valor": True}

    async def commit_con_puerta(conn: aiosqlite.Connection) -> None:
        if not primera["valor"]:
            await original_commit(conn)
            commit_ejecutado.set()
            return
        primera["valor"] = False
        if ventana == "antes_del_commit":
            alcanzada.set()
            await liberar.wait()
            await original_commit(conn)
        else:
            await original_commit(conn)
            alcanzada.set()
            await liberar.wait()
        commit_ejecutado.set()

    monkeypatch.setattr(aiosqlite.Connection, "commit", commit_con_puerta)
    return alcanzada, liberar, commit_ejecutado


async def _cancelar_en_la_ventana(
    tarea: asyncio.Task[object],
    alcanzada: asyncio.Event,
    liberar: asyncio.Event,
) -> None:
    """Espera la ventana, cancela, entrega la cancelación y abre la puerta."""
    await asyncio.wait_for(alcanzada.wait(), timeout=10.0)
    tarea.cancel()
    # Un solo yield: entrega la cancelación a la tarea ANTES de abrir la puerta.
    # No es una espera por tiempo — es el ordenamiento que la prueba necesita.
    await asyncio.sleep(0)
    liberar.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10.0)


# =============================================================================
# La familia: ninguna ventana de cancelación deja la lease viva
# =============================================================================


@pytest.mark.parametrize("ventana", ["antes_del_insert", "antes_del_commit", "tras_el_commit", "tras_el_retorno"])
async def test_ninguna_ventana_de_cancelacion_deja_la_lease_viva(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    ventana: str,
) -> None:
    """Cancelación en cualquier punto de la adquisición ⇒ cero lease residual.

    Las cuatro ventanas comparten el invariante y difieren en el estado de la
    adquisición: antes del INSERT, con el INSERT ejecutado y el commit en vuelo,
    con el commit persistido, y con el LockInfo ya retornado. Enumerarlas (en
    vez de muestrear una) es lo que impide que el fix cubra sólo el caso que el
    autor tenía en la cabeza.
    """
    alcanzada, liberar, _commit_ejecutado = _instalar_ventana(monkeypatch, ventana)
    contexto = _contexto(gestor, snapshots)

    tarea = asyncio.create_task(contexto.__aenter__())
    await _cancelar_en_la_ventana(tarea, alcanzada, liberar)

    assert _fila_en_disco(db_path) is None, f"la ventana '{ventana}' dejó la lease viva hasta el TTL"
    assert contexto.lock_info is None, "una adquisición cancelada no puede publicarse como propia"


async def test_la_cancelacion_entregada_al_retornar_libera_la_lease(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La cancelación gana la carrera contra el ``await`` que iba a consumir el token.

    Es la ventana más angosta de la familia: el commit terminó y la tarea de
    adquisición YA tiene su ``LockInfo``, pero la cancelación se entrega antes de
    que el ``await`` lo devuelva. Un wrapper que se queda con la cancelación y
    descarta el resultado pierde el token — y con él la única forma de liberar.

    Se simula el ordenamiento exacto (resultado listo, cancelación entregada)
    en vez de esperar a que ocurra: la ventana real dura microsegundos.
    """
    original_shield = asyncio.shield
    primera = {"valor": True}

    def shield_con_carrera(awaitable: object) -> object:
        if not primera["valor"]:
            return original_shield(awaitable)  # type: ignore[arg-type]
        primera["valor"] = False

        async def _resultado_listo_pero_cancelado() -> object:
            await original_shield(awaitable)  # type: ignore[arg-type]
            raise asyncio.CancelledError("cancelación entregada con el resultado listo")

        return _resultado_listo_pero_cancelado()

    monkeypatch.setattr(asyncio, "shield", shield_con_carrera)
    contexto = _contexto(gestor, snapshots)

    tarea = asyncio.create_task(contexto.__aenter__())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10.0)

    assert _fila_en_disco(db_path) is None, "la cancelación en el retorno perdió el token de la lease"
    assert contexto.lock_info is None


async def test_el_cleanup_no_borra_antes_de_que_la_adquisicion_escriba(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La compensación espera el commit: borrar antes deja la fila viva igual.

    Con la mutación EN VUELO, un cleanup eager (DELETE por resource+agent sin
    esperar el resultado) no encuentra la fila, el commit la escribe después y
    el lease queda vivo hasta el TTL: el mismo síntoma, con un fix de por medio.
    Por eso el commit TIENE que haberse ejecutado cuando la cancelación termina.
    """
    alcanzada, liberar, commit_ejecutado = _instalar_ventana(monkeypatch, "antes_del_commit")
    contexto = _contexto(gestor, snapshots)

    tarea = asyncio.create_task(contexto.__aenter__())
    await _cancelar_en_la_ventana(tarea, alcanzada, liberar)

    assert commit_ejecutado.is_set(), "la adquisición se abandonó antes de commitear: no hay resultado definitivo"
    assert _fila_en_disco(db_path) is None, "la fila se escribió después del cleanup y quedó viva"


# =============================================================================
# Condición de seguridad: la compensación no toca leases ajenas
# =============================================================================


async def test_la_compensacion_no_libera_la_lease_de_otro_agente(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El recurso lo tiene OTRO agente: la cancelación no puede borrar su fila."""
    ajena = await gestor.acquire_lock(RECURSO, "otro-proceso", ttl=60.0)
    alcanzada, liberar, _commit_ejecutado = _instalar_ventana(monkeypatch, "tras_el_commit")
    contexto = _contexto(gestor, snapshots)

    tarea = asyncio.create_task(contexto.__aenter__())
    await _cancelar_en_la_ventana(tarea, alcanzada, liberar)

    fila = _fila_en_disco(db_path)
    assert fila is not None, "la compensación borró la lease de un tercero"
    assert fila == ("otro-proceso", ajena.acquired_at)


async def test_la_compensacion_no_libera_una_lease_del_mismo_agente_con_otro_token(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismo ``agent_id``, otro token: es la lease de un HERMANO, no la nuestra.

    Este es el test que refuta el fix propuesto en el issue (liberar por
    ``resource_id`` + ``agent_id``): los servicios comparten un ``agent_id``
    constante, así que esa liberación borraría la lease que otra instancia del
    mismo servicio acaba de adquirir. La identidad que se puede demostrar es el
    token ``acquired_at`` que devolvió NUESTRA adquisición — el mismo que exigen
    ``release_lock`` y ``renew_lock``.
    """
    hermana = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)
    alcanzada, liberar, _commit_ejecutado = _instalar_ventana(monkeypatch, "tras_el_commit")
    contexto = _contexto(gestor, snapshots)

    tarea = asyncio.create_task(contexto.__aenter__())
    await _cancelar_en_la_ventana(tarea, alcanzada, liberar)

    fila = _fila_en_disco(db_path)
    assert fila is not None, "la compensación borró la lease de un hermano con el mismo agent_id"
    assert fila == (AGENTE, hermana.acquired_at)


@pytest.mark.parametrize("reclamante", ["otro-proceso", AGENTE])
async def test_la_compensacion_con_token_no_borra_lease_reclamada_antes_del_delete(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    reclamante: str,
) -> None:
    """Si la lease adquirida expira y es reclamada antes del DELETE compensatorio, no se borra.

    A diferencia de los dos casos anteriores (donde el tercero o hermano ya tenía
    el lock antes de empezar), acá nuestra adquisición SÍ gana el lock (token T1),
    se cancela tras el commit y, justo antes de que ``release_lock`` ejecute el
    DELETE de compensación, otro propietario (o un hermano con el mismo ``agent_id``)
    reclama la fila con un token nuevo T2 != T1. Como la compensación pasa T1 a
    ``release_lock``, el DELETE no matchea T2 y la lease reclamada sobrevive.
    """
    alcanzada, liberar, _commit_ejecutado = _instalar_ventana(monkeypatch, "tras_el_commit")
    original_release = DistributedLockManager.release_lock
    token_reclamado = {"valor": 0.0}
    token_enviado: list[float | None] = []

    async def release_con_reclamo_previo(
        self: DistributedLockManager,
        resource_id: str,
        agent_id: str,
        *,
        acquired_at: float | None = None,
    ) -> bool:
        token_enviado.append(acquired_at)
        assert acquired_at is not None
        nuevo_token = acquired_at + 100.0
        token_reclamado["valor"] = nuevo_token
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE resource_locks SET agent_id = ?, acquired_at = ?, expires_at = ? WHERE resource_id = ?",
                (reclamante, nuevo_token, nuevo_token + 60.0, resource_id),
            )
            conn.commit()
        finally:
            conn.close()
        return await original_release(self, resource_id, agent_id, acquired_at=acquired_at)

    monkeypatch.setattr(DistributedLockManager, "release_lock", release_con_reclamo_previo)
    contexto = _contexto(gestor, snapshots)

    tarea = asyncio.create_task(contexto.__aenter__())
    await _cancelar_en_la_ventana(tarea, alcanzada, liberar)

    assert len(token_enviado) == 1 and token_enviado[0] is not None
    assert _fila_en_disco(db_path) == (reclamante, token_reclamado["valor"]), (
        f"el DELETE compensatorio borró la lease reclamada por '{reclamante}' con otro token"
    )
    assert contexto.lock_info is None


# =============================================================================
# Cancelación durante el cleanup, idempotencia y heartbeat
# =============================================================================


async def test_una_segunda_cancelacion_durante_el_cleanup_no_deja_lease(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelar OTRA VEZ mientras se libera no puede abandonar el DELETE.

    El cleanup es el último acto del contexto: si la segunda cancelación lo
    abandona a mitad, la fila sobrevive y el fix no sirvió de nada. La
    liberación se reintenta (es idempotente: el DELETE matchea el token exacto).
    """
    alcanzada, liberar, _commit_ejecutado = _instalar_ventana(monkeypatch, "tras_el_commit")

    liberando = asyncio.Event()
    puerta_del_release = asyncio.Event()
    original_release = DistributedLockManager.release_lock

    async def release_con_puerta(
        self: DistributedLockManager,
        resource_id: str,
        agent_id: str,
        *,
        acquired_at: float | None = None,
    ) -> bool:
        liberando.set()
        await puerta_del_release.wait()
        return await original_release(self, resource_id, agent_id, acquired_at=acquired_at)

    monkeypatch.setattr(DistributedLockManager, "release_lock", release_con_puerta)

    contexto = _contexto(gestor, snapshots)
    tarea = asyncio.create_task(contexto.__aenter__())

    await asyncio.wait_for(alcanzada.wait(), timeout=10.0)
    tarea.cancel()
    await asyncio.sleep(0)
    liberar.set()

    # La compensación ya está en vuelo (dentro del DELETE envuelto).
    await asyncio.wait_for(liberando.wait(), timeout=10.0)
    tarea.cancel()
    await asyncio.sleep(0)
    puerta_del_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10.0)

    assert _fila_en_disco(db_path) is None, "la segunda cancelación abandonó el cleanup"
    assert contexto.lock_info is None, "la segunda cancelación dejó lock_info publicado"


async def test_fallo_de_release_durante_cleanup_cancelado_no_se_traga_la_cancelacion(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si ``release_lock`` falla con ``LockReleaseError`` tras una segunda cancelación,
    ``_safe_release`` propaga ``CancelledError`` y nunca deja ``lock_info`` publicado.
    """
    alcanzada, liberar, _commit_ejecutado = _instalar_ventana(monkeypatch, "tras_el_commit")
    liberando = asyncio.Event()
    puerta_del_release = asyncio.Event()

    async def release_que_falla(
        self: DistributedLockManager,
        resource_id: str,
        agent_id: str,
        *,
        acquired_at: float | None = None,
    ) -> bool:
        liberando.set()
        await puerta_del_release.wait()
        raise LockReleaseError("simulated release failure")

    monkeypatch.setattr(DistributedLockManager, "release_lock", release_que_falla)

    contexto = _contexto(gestor, snapshots)
    tarea = asyncio.create_task(contexto.__aenter__())

    await asyncio.wait_for(alcanzada.wait(), timeout=10.0)
    tarea.cancel()
    await asyncio.sleep(0)
    liberar.set()

    await asyncio.wait_for(liberando.wait(), timeout=10.0)
    tarea.cancel()
    await asyncio.sleep(0)
    puerta_del_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10.0)

    assert contexto.lock_info is None


async def test_la_liberacion_es_idempotente_y_no_borra_la_lease_readquirida(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
) -> None:
    """Un ``__aexit__`` repetido con el token viejo es no-op, no un borrado a ciegas.

    ``__aexit__`` puede correr dos veces (un handle que se libera y un
    ``async with`` que sale, por ejemplo). El segundo intento lleva el token de
    la adquisición ORIGINAL: tiene que encontrar la fila ya borrada y no tocar
    la que otro hermano readquirió en el medio.
    """
    contexto = _contexto(gestor, snapshots)
    await contexto.__aenter__()
    assert contexto.lock_info is not None
    token_original = contexto.lock_info.acquired_at

    await contexto.__aexit__(None, None, None)
    assert _fila_en_disco(db_path) is None

    # Otro hermano (mismo agent_id) readquiere el recurso antes del segundo exit.
    readquirida = await gestor.acquire_lock(RECURSO, AGENTE, ttl=60.0)
    assert readquirida.acquired_at != token_original

    await contexto.__aexit__(None, None, None)

    assert _fila_en_disco(db_path) == (AGENTE, readquirida.acquired_at), (
        "el segundo __aexit__ borró la lease readquirida por un hermano"
    )


async def test_la_cancelacion_no_deja_un_heartbeat_huerfano(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una adquisición cancelada (con ``auto_renew=True``) no puede dejar el heartbeat vivo."""
    alcanzada, liberar, _commit_ejecutado = _instalar_ventana(monkeypatch, "tras_el_commit")
    contexto = _contexto(gestor, snapshots, auto_renew=True)

    tarea = asyncio.create_task(contexto.__aenter__())
    await _cancelar_en_la_ventana(tarea, alcanzada, liberar)

    vivos = [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith(f"lock-heartbeat-{RECURSO}")]
    assert vivos == [], f"quedó un heartbeat vivo para una adquisición cancelada: {vivos}"
    assert _fila_en_disco(db_path) is None
    assert contexto.lock_info is None


async def test_adquisicion_completa_y_luego_cancelacion_libera_la_lease(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
) -> None:
    """Contracara: si la adquisición SÍ terminó, la cancelación del cuerpo libera.

    La lease existió de verdad (``lock_info`` publicado), así que este caso no lo
    cubre la compensación de la adquisición sino el ``__aexit__``: se verifica
    que el fix no haya movido la liberación de lugar.
    """
    contexto = _contexto(gestor, snapshots)
    dentro = asyncio.Event()

    async def cuerpo() -> None:
        async with contexto:
            dentro.set()
            await asyncio.Event().wait()  # se cancela acá

    tarea = asyncio.create_task(cuerpo())
    await asyncio.wait_for(dentro.wait(), timeout=10.0)
    assert contexto.lock_info is not None, "la adquisición completa tiene que publicar su token"
    assert _fila_en_disco(db_path) is not None

    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10.0)

    assert _fila_en_disco(db_path) is None


async def test_una_adquisicion_rechazada_no_deja_lease_ni_compensa_de_mas(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
) -> None:
    """``LockAcquisitionError`` sigue propagando igual (el fix no cambia el contrato).

    El recurso está tomado por otro proceso: la adquisición agota sus intentos y
    levanta. Ni la excepción ni el tipo pueden cambiar — ``dyndolod_workspace``
    la traduce a ``OCUPADO`` — y la lease ajena tiene que sobrevivir.
    """
    ajena = await gestor.acquire_lock(RECURSO, "otro-proceso", ttl=60.0)
    contexto = _contexto(gestor, snapshots)

    with pytest.raises(LockAcquisitionError):
        await contexto.__aenter__()

    assert _fila_en_disco(db_path) == ("otro-proceso", ajena.acquired_at)
    assert contexto.lock_info is None


async def test_la_cancelacion_durante_el_cleanup_completa_el_delete(
    gestor: DistributedLockManager,
    snapshots: FileSnapshotManager,
    db_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El ``__aexit__`` de una lease REAL no abandona el DELETE si lo cancelan.

    Hermano del caso de adquisición: acá la lease existió y el ``async with``
    sale normalmente; la cancelación llega mientras corre la liberación. Si el
    cleanup se abandona, la fila sobrevive hasta el TTL y el fix de la
    adquisición no habría servido de nada.
    """
    contexto = _contexto(gestor, snapshots)
    await contexto.__aenter__()
    assert contexto.lock_info is not None

    original_release = DistributedLockManager.release_lock
    liberando = asyncio.Event()
    puerta = asyncio.Event()

    async def release_con_puerta(
        self: DistributedLockManager,
        resource_id: str,
        agent_id: str,
        *,
        acquired_at: float | None = None,
    ) -> bool:
        liberando.set()
        await puerta.wait()
        return await original_release(self, resource_id, agent_id, acquired_at=acquired_at)

    monkeypatch.setattr(DistributedLockManager, "release_lock", release_con_puerta)

    tarea = asyncio.create_task(contexto.__aexit__(None, None, None))
    await asyncio.wait_for(liberando.wait(), timeout=10.0)
    tarea.cancel()
    await asyncio.sleep(0)
    puerta.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10.0)

    assert _fila_en_disco(db_path) is None, "la cancelación abandonó el DELETE del __aexit__"

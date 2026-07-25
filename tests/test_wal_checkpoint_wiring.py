"""P0-3 — las tres rutas del checkpoint WAL deben estar realmente cableadas.

Historia (post-mortem WinError 10048): el repo acumulaba archivos ``*.db-wal``
sin truncar porque las tres vías de checkpoint estaban cortadas a la vez:

1. **Periódica.** ``SupervisorAgent`` recibe un ``DatabaseLifecycleManager``
   (``lifecycle=ctx.lifecycle.manager`` desde el bootloader de la GUI) pero
   construía el ``MaintenanceDaemon`` SIN pasárselo. ``_checkpoint_tick``
   retorna en su primera línea cuando ``lifecycle_manager is None``: el tick
   de 5 minutos existía, corría, y no checkpointeaba nada.
2. **Red de ``atexit``.** ``register_atexit_handler()`` no tenía NINGÚN caller
   en todo el árbol — código muerto. En un exit duro (el caso que motivó el
   post-mortem) nadie truncaba el WAL.
3. **Apagado coordinado.** ``shutdown_all()`` solo corre si ``ctx.stop()``
   completa, y el deadlock del worker no-daemon lo impedía.

Este archivo fija (1) y (2). (3) quedó cerrado en #361.
"""

from __future__ import annotations

import atexit
import gc
import pathlib
import weakref
from collections.abc import Callable
from typing import Any

import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.antigravity.security.path_validator import PathValidator


@pytest.fixture
def mo2_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Layout MO2 descartable + MO2_PATH, con el cwd movido a tmp para que los
    SQLite de los componentes de rollback caigan bajo tmp y no en el repo."""
    monkeypatch.chdir(tmp_path)
    mo2 = tmp_path / "MO2"
    (mo2 / "profiles" / "Default").mkdir(parents=True)
    monkeypatch.setenv("MO2_PATH", str(mo2))
    return mo2


def test_supervisor_pasa_el_lifecycle_al_maintenance_daemon(mo2_root: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """El daemon debe recibir el lifecycle que el supervisor ya tiene inyectado.

    Sin él, ``_checkpoint_tick`` es un no-op silencioso: el checkpoint
    periódico nunca corre y el WAL crece sin cota.
    """
    sandbox = PathValidator(roots=[mo2_root, tmp_path])
    lifecycle = DatabaseLifecycleManager(db_paths=[])

    sup = SupervisorAgent(path_validator=sandbox, lifecycle=lifecycle)

    assert sup._maintenance_daemon._lifecycle_manager is lifecycle, (
        "MaintenanceDaemon construido sin lifecycle_manager: _checkpoint_tick "
        "retorna en su primera línea y el checkpoint periódico nunca corre"
    )


async def test_init_all_arma_la_red_de_atexit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``init_all`` debe armar la red de ``atexit`` del propio manager.

    La garantía viaja con el objeto que la necesita: quien inicializa el
    lifecycle no tiene que acordarse de registrar la red por separado (que es
    exactamente cómo ``register_atexit_handler`` terminó sin callers).
    """
    registrados: list[Callable[..., Any]] = []

    def _fake_register(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[..., Any]:
        registrados.append(fn)
        return fn

    monkeypatch.setattr(atexit, "register", _fake_register)

    manager = DatabaseLifecycleManager(db_paths=[])
    await manager.init_all()

    assert registrados, (
        "init_all no armó la red de atexit: sin caller, register_atexit_handler "
        "es código muerto y un exit duro deja el WAL sin truncar"
    )

    # Lo registrado debe delegar en el checkpoint sincrónico del manager.
    llamadas: list[bool] = []
    monkeypatch.setattr(manager, "_sync_shutdown", lambda: llamadas.append(True))
    registrados[0]()
    assert llamadas == [True], "el handler registrado no dispara el checkpoint del manager"


async def test_init_all_no_arma_la_red_de_atexit_si_esta_deshabilitado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cuando ``enable_auto_checkpoint=False``, ``init_all`` NO debe registrar nada en ``atexit``."""
    registrados: list[Callable[..., Any]] = []

    def _fake_register(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[..., Any]:
        registrados.append(fn)
        return fn

    monkeypatch.setattr(atexit, "register", _fake_register)

    config = DatabaseLifecycleConfig(enable_auto_checkpoint=False)
    manager = DatabaseLifecycleManager(db_paths=[], config=config)
    await manager.init_all()

    assert not registrados, "init_all registró en atexit pese a tener enable_auto_checkpoint=False"


async def test_init_all_no_extiende_la_vida_del_manager() -> None:
    """El registro de ``atexit`` NO debe mantener vivo al manager.

    Un bound method (``self._sync_shutdown``) en el registro de ``atexit``
    ancla ``self`` con una referencia fuerte, y con él ``self._connections``:
    las conexiones ``aiosqlite`` dejan de recolectarse, su ``__del__`` —que es
    quien llama a ``stop()``— nunca corre, y sus worker threads NO-daemon
    sobreviven al proceso. Ese es exactamente el modo de falla que vigila
    ``pytest_unconfigure`` (y que en producción sería una fuga de por vida).
    """
    manager = DatabaseLifecycleManager(db_paths=[])
    await manager.init_all()
    ref = weakref.ref(manager)

    del manager
    gc.collect()

    assert ref() is None, (
        "la red de atexit ancló al manager: sus conexiones aiosqlite nunca se "
        "recolectan y sus worker threads non-daemon sobreviven al proceso"
    )


def test_sync_shutdown_no_materializa_dbs_inexistentes(tmp_path: pathlib.Path) -> None:
    """``_sync_shutdown`` debe saltear paths cuyo archivo ya no existe.

    ``sqlite3.connect`` CREA el archivo si falta. Con la red de atexit ya viva,
    un path registrado cuya DB fue borrada (DB efímera, tmpdir de test) dejaría
    un ``.db`` vacío al salir del proceso, además de ruido en los logs.
    """
    fantasma = tmp_path / "no_existe.db"
    manager = DatabaseLifecycleManager(db_paths=[fantasma])

    manager._sync_shutdown()

    assert not fantasma.exists(), "_sync_shutdown materializó una DB vacía en un path inexistente"


def test_sync_shutdown_checkpointea_paths_relativos(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``_sync_shutdown`` debe funcionar con paths relativos, no solo absolutos.

    ``DatabaseAgent`` — el que usan la GUI (``sky_claw_gui.py``) y el
    ``SupervisorAgent``, ambos vía ``DatabaseAgent()`` sin argumentos — abre
    ``"sky_claw_state.db"`` por defecto: un path RELATIVO. Un fix posterior de
    revisión reemplazó ``sqlite3.connect(path_str)`` por una URI construida con
    ``Path(path_str).as_uri()`` para cerrar el TOCTOU de
    ``test_sync_shutdown_no_materializa_dbs_inexistentes``. Pero
    ``Path.as_uri()`` exige una ruta ABSOLUTA — con una relativa levanta
    ``ValueError``, que el ``except Exception`` genérico traga en silencio.
    Resultado: la red de atexit para el path por defecto de PRODUCCIÓN nunca
    checkpointea nada, sin ningún error visible salvo el log.
    """
    monkeypatch.chdir(tmp_path)
    relativo = pathlib.Path("sky_claw_state.db")
    manager = DatabaseLifecycleManager(db_paths=[relativo])

    import sqlite3

    # La conexión de setup se mantiene ABIERTA a propósito: cerrar la última
    # conexión de una DB en WAL dispara un auto-checkpoint de SQLite que ya
    # truncaría el -wal, invalidando la premisa del test (WAL con contenido
    # antes de que corra ``_sync_shutdown``).
    conn = sqlite3.connect(str(relativo))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x INT)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        wal = tmp_path / "sky_claw_state.db-wal"
        assert wal.exists() and wal.stat().st_size > 0, (
            "setup del test: el WAL debería tener contenido antes del checkpoint"
        )

        with caplog.at_level("ERROR"):
            manager._sync_shutdown()

        assert not caplog.records, f"_sync_shutdown falló en silencio para un path relativo: {caplog.text}"
        assert wal.stat().st_size == 0, "el WAL no se truncó: el checkpoint para el path relativo no corrió"
    finally:
        conn.close()

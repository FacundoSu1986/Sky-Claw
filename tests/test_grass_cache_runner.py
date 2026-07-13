"""Tests del ``GrassCacheRunner`` (PR-4 del plan grass cache, Fase C del SOP).

El crash-loop supervisor del precache de NGIO: los CTD del juego son ESPERADOS
(memory leak del Creation Engine) — crash = proceso muerto Y ``PrecacheGrass.txt``
presente → relanzar. Fin = NGIO borró el flag. Sky-Claw ES el "Restart on Crash".

Arnés (patrones del repo):
- ``MO2Controller`` entero como ``AsyncMock`` — NO se mockea
  ``create_subprocess_exec``: el runner usa el controller inyectado (D2).
- ``psutil`` reemplazado a nivel módulo del runner por un "mundo de procesos"
  con guion de vida mutable (PIDs vivos, hijos, ``process_iter``, vidas por
  chequeo) — determinista, sin fake clocks.
- Tiempos diminutos vía config (TODOS los intervalos son configurables: sin
  monkeypatch de constantes de módulo).
- ``shutil.disk_usage`` mockeado con namedtuple (primer uso en el repo).
- Toda corrida envuelta en ``asyncio.wait_for(..., 10)`` como red externa.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import pathlib
import time
from collections import namedtuple
from typing import Any
from unittest.mock import AsyncMock

import psutil as psutil_real
import pytest

import sky_claw.local.tools.grass_cache_runner as gcr_mod
from sky_claw.antigravity.security.path_validator import PathViolationError
from sky_claw.local.mo2.vfs import GameLaunchTimeoutError, MO2Controller
from sky_claw.local.tools.grass_cache_runner import (
    GrassCacheConfig,
    GrassCacheProgress,
    GrassCacheRunner,
    GrassCacheRunResult,
)
from tests.polling_utils import poll_until

_USO_DISCO = namedtuple("_USO_DISCO", "total used free")  # shape de shutil.disk_usage
_GIB = 1024**3


# ---------------------------------------------------------------------------
# Mundo de procesos falso (reemplaza el objeto psutil del módulo del runner)
# ---------------------------------------------------------------------------


class _ProcFake:
    """Un proceso del mundo: handle estilo psutil.Process."""

    def __init__(self, mundo: _MundoProcesos, pid: int, nombre: str, create_time: float) -> None:
        self._mundo = mundo
        self.pid = pid
        self._nombre = nombre
        self._create_time = create_time

    @property
    def info(self) -> dict[str, Any]:
        return {"name": self._nombre, "create_time": self._create_time}

    def name(self) -> str:
        return self._nombre

    def kill(self) -> None:
        self._mundo.vivos.discard(self.pid)

    def children(self, recursive: bool = False) -> list[_ProcFake]:
        return [h for h in self._mundo.hijos.get(self.pid, []) if h.pid in self._mundo.vivos]


class _MundoProcesos:
    """Guion de vida de procesos. Actúa como el módulo psutil dentro del runner.

    ``vidas[pid] = N``: el pid sobrevive N chequeos de ``pid_exists`` y muere en
    el N+1 (crash determinista sin coordinar tasks). ``al_chequear_pid`` es un
    hook para que el test reaccione en el momento exacto de un chequeo (p.ej.
    borrar el flag "justo antes del crash").
    """

    # Excepciones reales de psutil: el runner las captura vía el objeto módulo.
    Error = psutil_real.Error
    NoSuchProcess = psutil_real.NoSuchProcess
    AccessDenied = psutil_real.AccessDenied

    def __init__(self) -> None:
        self.vivos: set[int] = set()
        self.hijos: dict[int, list[_ProcFake]] = {}
        self.globales: list[_ProcFake] = []
        self.vidas: dict[int, int] = {}
        self.al_chequear_pid: Any = None

    def pid_exists(self, pid: int) -> bool:
        if self.al_chequear_pid is not None:
            self.al_chequear_pid(pid)
        if pid not in self.vivos:
            return False
        if pid in self.vidas:
            if self.vidas[pid] <= 0:
                self.vivos.discard(pid)
                return False
            self.vidas[pid] -= 1
        return True

    def Process(self, pid: int) -> _ProcFake:  # noqa: N802 — espejo de la API psutil
        if pid not in self.vivos:
            raise psutil_real.NoSuchProcess(pid)
        return _ProcFake(self, pid, "ModOrganizer.exe", 0.0)

    def process_iter(self, attrs: list[str] | None = None) -> Any:
        return iter([p for p in self.globales if p.pid in self.vivos])

    # -- helpers de guion --

    def alta_juego(
        self,
        mo2_pid: int,
        game_pid: int,
        *,
        vidas: int | None = None,
        nombre: str = "SkyrimSE.exe",
        create_time: float | None = None,
    ) -> _ProcFake:
        """Registra MO2 + su juego hijo, ambos vivos."""
        self.vivos |= {mo2_pid, game_pid}
        juego = _ProcFake(self, game_pid, nombre, create_time if create_time is not None else time.time())
        self.hijos[mo2_pid] = [juego]
        self.globales.append(juego)
        if vidas is not None:
            self.vidas[game_pid] = vidas
        return juego


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def entorno(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(game_path, overwrite_grass_dir) sobre tmp_path; Grass/ NO existe aún."""
    game = tmp_path / "game"
    game.mkdir()
    overwrite = tmp_path / "overwrite"
    overwrite.mkdir()
    return game, overwrite / "Grass"


@pytest.fixture
def config(entorno: tuple[pathlib.Path, pathlib.Path]) -> GrassCacheConfig:
    """Config con tiempos diminutos: los tests corren en milisegundos."""
    game, grass = entorno
    return GrassCacheConfig(
        game_path=game,
        overwrite_grass_dir=grass,
        max_runtime_s=30.0,
        max_restarts=50,
        stall_threshold=5,
        relaunch_delay_s=0.01,
        spawn_window_s=0.3,
        poll_interval_s=0.01,
        spawn_poll_interval_s=0.01,
        heartbeat_interval_s=60.0,
    )


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch) -> _MundoProcesos:
    m = _MundoProcesos()
    monkeypatch.setattr(gcr_mod, "psutil", m)
    return m


@pytest.fixture
def mo2(mundo: _MundoProcesos) -> AsyncMock:
    """MO2Controller mockeado cuyo launch_game da de alta un juego en el mundo.

    Guion default: cada launch crea (mo2_pid, game_pid) frescos con el juego
    INMORTAL; los tests ajustan ``mundo.vidas``/hooks o pisan el side_effect.
    """
    controller = AsyncMock(spec=MO2Controller)
    contador = itertools.count(100)

    async def _launch(profile: str) -> dict[str, Any]:
        mo2_pid = next(contador)
        mundo.alta_juego(mo2_pid, mo2_pid + 1000)
        return {"pid": mo2_pid, "status": "launched", "profile": profile}

    controller.launch_game.side_effect = _launch
    controller.close_game.return_value = {"status": "closed", "killed_processes": []}
    return controller


def _runner(config: GrassCacheConfig, mo2: AsyncMock, **kwargs: Any) -> GrassCacheRunner:
    return GrassCacheRunner(config, mo2, **kwargs)


async def _correr(runner: GrassCacheRunner, cancel: asyncio.Event | None = None) -> GrassCacheRunResult:
    """Red de seguridad externa: ningún test puede colgar más de 10s."""
    return await asyncio.wait_for(runner.run(cancel), timeout=10)


def _escribir_cgid(grass_dir: pathlib.Path, nombre: str, datos: bytes = b"cgid") -> None:
    grass_dir.mkdir(parents=True, exist_ok=True)
    (grass_dir / nombre).write_bytes(datos)


# ---------------------------------------------------------------------------
# Contratos: config y resultado
# ---------------------------------------------------------------------------


def test_config_valida_paths_y_valores(entorno: tuple[pathlib.Path, pathlib.Path]) -> None:
    game, grass = entorno

    with pytest.raises(ValueError, match="game_path"):
        GrassCacheConfig(game_path=game / "no_existe", overwrite_grass_dir=grass)
    with pytest.raises(ValueError, match="overwrite_grass_dir"):
        GrassCacheConfig(game_path=game, overwrite_grass_dir=game / "x" / "y" / "Grass")
    with pytest.raises(ValueError, match="max_restarts"):
        GrassCacheConfig(game_path=game, overwrite_grass_dir=grass, max_restarts=0)
    with pytest.raises(ValueError, match="stall_threshold"):
        GrassCacheConfig(game_path=game, overwrite_grass_dir=grass, stall_threshold=0)
    with pytest.raises(PathViolationError):
        GrassCacheConfig(game_path=game, overwrite_grass_dir=grass, profile="../evil")


# ---------------------------------------------------------------------------
# Flag y pre-vuelo
# ---------------------------------------------------------------------------


async def test_cancelacion_previa_no_crea_flag_ni_lanza(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    cancel = asyncio.Event()
    cancel.set()

    resultado = await _correr(_runner(config, mo2), cancel)

    assert resultado.outcome == "cancelled"
    assert resultado.cancelled is True
    assert resultado.success is False
    mo2.launch_game.assert_not_awaited()
    assert not (config.game_path / "PrecacheGrass.txt").exists()


async def test_disco_lleno_antes_de_arrancar_no_crea_flag_ni_lanza(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sky_claw.local.tools.grass_cache_runner.shutil.disk_usage",
        lambda _p: _USO_DISCO(total=100 * _GIB, used=100 * _GIB, free=0),
    )

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "disk_full"
    assert resultado.success is False
    mo2.launch_game.assert_not_awaited()
    assert not (config.game_path / "PrecacheGrass.txt").exists()


async def test_crea_el_flag_durante_el_run_y_lo_limpia_al_salir(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    # Path de timeout: el juego es inmortal y max_runtime diminuto — el flag
    # debe existir DURANTE el run y desaparecer en el finally.
    cfg = dataclasses.replace(config, max_runtime_s=0.5)
    flag = cfg.game_path / "PrecacheGrass.txt"
    tarea = asyncio.create_task(_runner(cfg, mo2).run())

    await poll_until(flag.exists, timeout=5.0, msg="el flag nunca se creó")
    resultado = await asyncio.wait_for(tarea, timeout=10)

    assert resultado.outcome == "timeout"
    assert not flag.exists(), "flag residual dejaría el juego del usuario en modo precache"


# ---------------------------------------------------------------------------
# Spawn window
# ---------------------------------------------------------------------------


async def test_juego_nunca_aparece_en_ciclo_cero_es_spawn_failed(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    # launch_game "exitoso" pero sin juego en el mundo: entorno roto → fail-fast.
    async def _launch_sin_juego(profile: str) -> dict[str, Any]:
        return {"pid": 55, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch_sin_juego
    cfg = dataclasses.replace(config, spawn_window_s=0.05)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "spawn_failed"
    assert resultado.crash_count == 0
    assert mo2.launch_game.await_count == 1, "sin retry: reintentar con entorno roto quema la ventana"
    mo2.close_game.assert_awaited()
    assert not (cfg.game_path / "PrecacheGrass.txt").exists()


async def test_game_launch_timeout_en_ciclo_cero_es_spawn_failed(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    mo2.launch_game.side_effect = GameLaunchTimeoutError(5)
    cfg = dataclasses.replace(config, spawn_window_s=0.05)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "spawn_failed"
    assert resultado.success is False


async def test_no_spawn_tras_ciclo_exitoso_cuenta_como_crash_y_reintenta(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Ciclo 0 spawnea y crashea; los siguientes no spawnean. Sin .cgid nuevos,
    # el stall detector acota la repetición (no aborta como spawn_failed).
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        if n == 0:
            mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100 + n, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    cfg = dataclasses.replace(config, spawn_window_s=0.05, stall_threshold=3)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "stalled"
    assert resultado.crash_count >= 2, "el no-spawn posterior contó como crash y hubo retry"
    assert mo2.launch_game.await_count >= 3


async def test_mo2_muerto_el_fallback_por_nombre_encuentra_al_juego(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # MO2 lanza y sale de inmediato (config común): el juego reparentado se
    # encuentra vía process_iter + filtro por create_time (D1-C). D7b: en la
    # cancelación, el kill directo del game_pid mata al juego huérfano.
    async def _launch(profile: str) -> dict[str, Any]:
        juego = _ProcFake(mundo, 2000, "SkyrimSE.exe", time.time())
        mundo.vivos.add(2000)
        mundo.globales.append(juego)
        return {"pid": 999, "status": "launched", "profile": profile}  # 999 jamás vivo

    mo2.launch_game.side_effect = _launch
    cancel = asyncio.Event()
    tarea = asyncio.create_task(_runner(config, mo2).run(cancel))

    await poll_until(lambda: mo2.launch_game.await_count >= 1, timeout=5.0)
    await asyncio.sleep(0.05)  # dejar que la vigilancia arranque
    cancel.set()
    resultado = await asyncio.wait_for(tarea, timeout=10)

    assert resultado.outcome == "cancelled"
    assert resultado.crash_count == 0, "la muerte de MO2 no es un crash del juego"
    assert 2000 not in mundo.vivos, "D7b: el juego huérfano debe morir aunque close_game no lo alcance"


async def test_process_iter_ignora_skyrim_preexistente(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Un SkyrimSE del usuario, anterior al lanzamiento, NO se adopta (filtro
    # temporal de D1-C): ventana agotada → spawn_failed, y ese proceso vive.
    preexistente = _ProcFake(mundo, 3000, "SkyrimSE.exe", time.time() - 9999)
    mundo.vivos.add(3000)
    mundo.globales.append(preexistente)

    async def _launch(profile: str) -> dict[str, Any]:
        return {"pid": 999, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    cfg = dataclasses.replace(config, spawn_window_s=0.05)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "spawn_failed"
    assert 3000 in mundo.vivos, "el Skyrim preexistente del usuario no se toca"


async def test_flag_borrado_durante_spawn_window_es_exito(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # NGIO completó durante el boot (reanudación casi terminada): éxito, no
    # spawn_failed.
    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid")
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        return {"pid": 999, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is True
    assert resultado.cgid_count == 1


# ---------------------------------------------------------------------------
# Camino feliz y fin multi-criterio
# ---------------------------------------------------------------------------


async def test_flag_ausente_con_juego_vivo_es_exito_y_cierra_el_juego(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    chequeos = itertools.count()

    def _al_chequear(pid: int) -> None:
        if next(chequeos) >= 1:  # tras el primer chequeo de vigilancia
            _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid")
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is True
    assert resultado.cgid_count == 1
    assert resultado.cache_size_mb > 0
    mo2.close_game.assert_awaited()


async def test_flag_ausente_con_cero_cgid_es_fallo_silencioso(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Postcheck fail-closed (D5): "completó" sin ningún .cgid = el fallo
    # silencioso de zero-bounds, jamás success=True.
    chequeos = itertools.count()

    def _al_chequear(pid: int) -> None:
        if next(chequeos) >= 1:
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is False
    assert resultado.cgid_count == 0
    assert "cgid" in resultado.message.lower() or "vac" in resultado.message.lower()


async def test_tres_crashes_consecutivos_terminan_en_exito(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # 3 vidas cortas del juego (cada ciclo aporta un .cgid nuevo → sin stall);
    # el 4.º lanzamiento encuentra el precache completo (flag borrado).
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        _escribir_cgid(config.overwrite_grass_dir, f"celda_{n}.cgid")
        if n == 3:
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        else:
            mundo.alta_juego(100 + n, 1100 + n, vidas=1)
        return {"pid": 100 + n, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is True
    assert resultado.crash_count == 3
    assert mo2.launch_game.await_count == 4
    assert resultado.cgid_count == 4


async def test_flag_borrado_justo_antes_del_crash_no_relanza(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # NGIO borra el flag y el juego muere en la MISMA iteración: el re-check
    # post-crash evita un relanzamiento espurio (juego en modo normal eterno).
    _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid")

    def _al_chequear(pid: int) -> None:
        if pid not in mundo.vivos or mundo.vidas.get(pid) == 0:
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is True
    assert resultado.crash_count == 1
    assert mo2.launch_game.await_count == 1, "el flag ausente post-crash no debe relanzar"


# ---------------------------------------------------------------------------
# Cortes estructurados: stall / disco / timeout / cancelación / presupuesto
# ---------------------------------------------------------------------------


async def test_stall_crashes_sin_cgid_nuevos_corta_con_stalled(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # El juego muere siempre en la misma celda sin escribir nada.
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        mundo.alta_juego(100 + n, 1100 + n, vidas=1)
        return {"pid": 100 + n, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    cfg = dataclasses.replace(config, stall_threshold=3)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "stalled"
    assert resultado.stalled is True
    assert resultado.success is False
    assert resultado.crash_count == 3, "corta en el threshold, sin quemar max_restarts"


async def test_max_restarts_agotado(config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos) -> None:
    # Snapshot SIEMPRE cambiante (esquiva el stall): el presupuesto corta.
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        _escribir_cgid(config.overwrite_grass_dir, f"celda_{n}.cgid")
        mundo.alta_juego(100 + n, 1100 + n, vidas=1)
        return {"pid": 100 + n, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    cfg = dataclasses.replace(config, max_restarts=2)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "max_restarts"
    assert resultado.crash_count == 3  # intento 0 + 2 relanzamientos
    assert resultado.success is False


async def test_timeout_global_cierra_el_juego_y_no_deja_flag(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    cfg = dataclasses.replace(config, max_runtime_s=0.05)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "timeout"
    assert resultado.success is False
    mo2.close_game.assert_awaited()
    assert not (cfg.game_path / "PrecacheGrass.txt").exists()


async def test_cancelacion_cierra_el_juego_y_conserva_el_cache_parcial(
    config: GrassCacheConfig, mo2: AsyncMock
) -> None:
    _escribir_cgid(config.overwrite_grass_dir, "parcial.cgid")
    cancel = asyncio.Event()
    tarea = asyncio.create_task(_runner(config, mo2).run(cancel))

    await poll_until(lambda: mo2.launch_game.await_count >= 1, timeout=5.0)
    cancel.set()
    resultado = await asyncio.wait_for(tarea, timeout=10)

    assert resultado.outcome == "cancelled"
    assert resultado.cancelled is True
    mo2.close_game.assert_awaited()
    assert (config.overwrite_grass_dir / "parcial.cgid").exists(), "el cache parcial se conserva"
    assert not (config.game_path / "PrecacheGrass.txt").exists()


async def test_disco_lleno_durante_el_run_corta_con_disk_full(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _escribir_cgid(config.overwrite_grass_dir, "parcial.cgid")
    lecturas = itertools.count()

    def _disk_usage(_p: Any) -> Any:
        # Primera lectura (pre-vuelo) con espacio; después, disco lleno.
        if next(lecturas) == 0:
            return _USO_DISCO(total=100 * _GIB, used=1 * _GIB, free=99 * _GIB)
        return _USO_DISCO(total=100 * _GIB, used=100 * _GIB, free=0)

    monkeypatch.setattr("sky_claw.local.tools.grass_cache_runner.shutil.disk_usage", _disk_usage)

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "disk_full"
    assert resultado.success is False
    mo2.close_game.assert_awaited()
    assert (config.overwrite_grass_dir / "parcial.cgid").exists()


# ---------------------------------------------------------------------------
# Observabilidad y robustez
# ---------------------------------------------------------------------------


async def test_heartbeat_emite_progreso_tipado(config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos) -> None:
    progresos: list[GrassCacheProgress] = []

    async def _on_progress(p: GrassCacheProgress) -> None:
        progresos.append(p)

    chequeos = itertools.count()

    def _al_chequear(pid: int) -> None:
        if next(chequeos) >= 4:
            _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid")
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    mundo.al_chequear_pid = _al_chequear
    cfg = dataclasses.replace(config, heartbeat_interval_s=0.02)

    resultado = await _correr(_runner(cfg, mo2, on_progress=_on_progress))

    assert resultado.success is True
    fases = {p.phase for p in progresos}
    assert "launching" in fases
    assert "scanning" in fases, "el heartbeat debe emitir durante la vigilancia"
    assert "finished" in fases
    ultimo = progresos[-1]
    assert ultimo.cgid_count == 1
    assert ultimo.elapsed_s >= 0


async def test_callback_de_progreso_roto_no_interrumpe_el_ritual(
    config: GrassCacheConfig,
    mo2: AsyncMock,
    mundo: _MundoProcesos,
    caplog: pytest.LogCaptureFixture,
) -> None:
    roto = AsyncMock(side_effect=RuntimeError("observador caído"))
    chequeos = itertools.count()

    def _al_chequear(pid: int) -> None:
        if next(chequeos) >= 1:
            _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid")
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2, on_progress=roto))

    assert resultado.outcome == "completed"
    assert resultado.success is True
    assert any("on_progress" in r.message for r in caplog.records)


async def test_excepcion_inesperada_cierra_el_juego_y_limpia_el_flag(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        if n == 1:
            raise RuntimeError("bug inesperado")
        _escribir_cgid(config.overwrite_grass_dir, f"celda_{n}.cgid")
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    with pytest.raises(RuntimeError, match="bug inesperado"):
        await _correr(_runner(config, mo2))

    mo2.close_game.assert_awaited()
    assert not (config.game_path / "PrecacheGrass.txt").exists(), "el finally limpia el flag ante bugs"

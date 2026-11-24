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
import logging
import pathlib
import threading
import time
from collections import namedtuple
from typing import Any
from unittest.mock import AsyncMock

import psutil as psutil_real
import pytest

import sky_claw.local.tools.grass_cache_runner as gcr_mod
import sky_claw.local.tools.grass_cache_safety as grass_cache_safety
from sky_claw.app.security.path_validator import PathViolationError
from sky_claw.local.mo2.vfs import GameLaunchTimeoutError, MO2Controller
from sky_claw.local.tools.grass_cache_runner import (
    GrassCacheConfig,
    GrassCacheProgress,
    GrassCacheRunner,
    GrassCacheRunResult,
)
from sky_claw.local.tools.grass_cache_safety import (
    EstadoAplicacion,
    GrassCacheSafetyError,
    NeutralizacionFlag,
    archivar_progreso,
    inventariar_cache,
)
from tests.polling_utils import poll_until

_USO_DISCO = namedtuple("_USO_DISCO", "total used free")  # shape de shutil.disk_usage
_GIB = 1024**3


# ---------------------------------------------------------------------------
# Mundo de procesos falso (reemplaza el objeto psutil del módulo del runner)
# ---------------------------------------------------------------------------


class _ProcFake:
    """Un proceso del mundo: handle estilo psutil.Process."""

    def __init__(
        self,
        mundo: _MundoProcesos,
        pid: int,
        nombre: str,
        create_time: float,
        exe: str | None = None,
    ) -> None:
        self._mundo = mundo
        self.pid = pid
        self._nombre = nombre
        self._create_time = create_time
        self._exe = exe

    @property
    def info(self) -> dict[str, Any]:
        return {"name": self._nombre, "create_time": self._create_time, "exe": self._exe}

    def name(self) -> str:
        return self._nombre

    def exe(self) -> str | None:
        # psutil.Process.exe() puede lanzar AccessDenied; el mundo modela el
        # exe ilegible como None (el runner lo trata como fail-closed).
        return self._exe

    def create_time(self) -> float:
        return self._create_time

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
        # Dir del juego para que alta_juego construya un exe verificable bajo él
        # (lo setea la fixture `mundo` desde `entorno`). El kill del runner ahora
        # revalida name+exe antes de matar (§2.5), así que el juego "legítimo"
        # debe residir en game_path o no se mataría.
        self.game_dir: pathlib.Path | None = None

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
        # Devolver el proc REGISTRADO (name/exe reales) si existe: el kill del
        # runner revalida identidad vía Process(pid).exe(), así que el handle
        # debe ser fiel, no un genérico. Fallback MO2 para pids sin _ProcFake
        # propio (el pid de MO2 no se registra como objeto, solo como key).
        registrado = self._buscar_registrado(pid)
        if registrado is not None:
            return registrado
        return _ProcFake(self, pid, "ModOrganizer.exe", 0.0)

    def _buscar_registrado(self, pid: int) -> _ProcFake | None:
        for p in self.globales:
            if p.pid == pid:
                return p
        for lst in self.hijos.values():
            for p in lst:
                if p.pid == pid:
                    return p
        return None

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
        # exe bajo game_dir → el juego "legítimo" pasa el revalidado de identidad
        # del kill (§2.5). Si la fixture no seteó game_dir, exe queda None.
        exe = str(self.game_dir / nombre) if self.game_dir is not None else None
        juego = _ProcFake(self, game_pid, nombre, create_time if create_time is not None else time.time(), exe=exe)
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
    (game / "SkyrimSE.exe").write_bytes(b"MZ")  # el config exige el exe presente
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
        # La cobertura USVFS no está demostrada: hasta P2, TODO run que escriba
        # el flag vacío necesita el reconocimiento explícito. Los tests que
        # verifican el HOLD lo apagan a propósito.
        ack_usvfs_no_verificado=True,
    )


@pytest.fixture
def config_rebuild(config: GrassCacheConfig) -> GrassCacheConfig:
    """Config que declara REBUILD sobre un caché preexistente, con respaldo.

    Desde P0 SAFE CACHE (F-01) el default ``new`` **se niega** a arrancar con
    ``.cgid`` preexistentes: NGIO los borraría al ver el flag sin progreso. Los
    tests que necesitan un caché de partida (baseline del detector de stall,
    "el parcial se conserva") declaran la decisión explícita en vez de heredar
    un default que destruiría datos en producción.
    """
    raiz_estado = config.overwrite_grass_dir.parent / "estado_grass"
    return dataclasses.replace(
        config,
        mode="rebuild",
        rebuild_approval="REBUILD",
        backup_root=raiz_estado,
        ack_usvfs_no_verificado=True,
    )


@pytest.fixture
def mundo(monkeypatch: pytest.MonkeyPatch, entorno: tuple[pathlib.Path, pathlib.Path]) -> _MundoProcesos:
    m = _MundoProcesos()
    m.game_dir = entorno[0]  # alta_juego construye el exe del juego bajo game_path
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


def _juego_escribe_cgid(config: GrassCacheConfig, nombre: str, *, celda: str | None = None) -> None:
    """El juego de mentira escribe un ``.cgid`` y NGIO **appendea** la celda al flag.

    ``GidFileCache`` (1.0.13) abre ``PrecacheGrass.txt`` en ``std::ios::app`` y
    anota cada celda completada: un ciclo real deja SIEMPRE progreso detrás. Los
    tests que modelaban un relanzamiento con el flag vacío describían un estado
    que NGIO no produce (y que, de producirse, hace que borre el caché).
    """
    _escribir_cgid(config.overwrite_grass_dir, nombre)
    with (config.game_path / "PrecacheGrass.txt").open("ab") as fh:
        fh.write((celda or nombre).encode() + b"\n")


# ---------------------------------------------------------------------------
# Contratos: config y resultado
# ---------------------------------------------------------------------------


def test_config_valida_paths_y_valores(entorno: tuple[pathlib.Path, pathlib.Path]) -> None:
    game, grass = entorno

    with pytest.raises(ValueError, match="game_path"):
        GrassCacheConfig(game_path=game / "no_existe", overwrite_grass_dir=grass)
    # Directorio existente pero SIN el exe del juego (p.ej. la raíz de MO2):
    # el flag iría donde NGIO jamás lo ve y el run se comería las 12 h.
    sin_exe = game.parent / "mo2_root"
    sin_exe.mkdir()
    with pytest.raises(ValueError, match="ejecutable"):
        GrassCacheConfig(game_path=sin_exe, overwrite_grass_dir=grass)
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


async def test_deadline_global_rige_tambien_en_spawn_window(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    # Fix review #287: sin el check dentro de la ventana de spawn, ciclos de
    # no-spawn repetidos excederían max_runtime por stall_threshold*spawn_window.
    async def _launch_sin_juego(profile: str) -> dict[str, Any]:
        return {"pid": 55, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch_sin_juego
    cfg = dataclasses.replace(config, max_runtime_s=0.05, spawn_window_s=30.0)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "timeout", "el deadline global gana aunque la ventana de spawn siga abierta"
    assert not (cfg.game_path / "PrecacheGrass.txt").exists()


async def test_cancelacion_dura_durante_progreso_inicial_limpia_el_flag(
    config: GrassCacheConfig, mo2: AsyncMock
) -> None:
    # Fix review #287: una CancelledError en el primer on_progress (antes del
    # loop) también debe pasar por el finally que borra el flag.
    duro = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        await _runner(config, mo2, on_progress=duro).run()

    assert not (config.game_path / "PrecacheGrass.txt").exists(), "el finally debe cubrir la fase inicial"


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
        juego = _ProcFake(mundo, 2000, "SkyrimSE.exe", time.time(), exe=str(config.game_path / "SkyrimSE.exe"))
        mundo.vivos.add(2000)
        mundo.globales.append(juego)
        return {"pid": 999, "status": "launched", "profile": profile}  # 999 jamás vivo

    mo2.launch_game.side_effect = _launch
    # Señal determinista de "la vigilancia arrancó": el primer pid_exists del
    # juego adoptado solo ocurre en la fase de vigilancia (sin sleeps fijos).
    vigilando = asyncio.Event()

    def _al_chequear(pid: int) -> None:
        if pid == 2000:
            vigilando.set()

    mundo.al_chequear_pid = _al_chequear
    cancel = asyncio.Event()
    tarea = asyncio.create_task(_runner(config, mo2).run(cancel))

    await asyncio.wait_for(vigilando.wait(), timeout=5.0)
    cancel.set()
    resultado = await asyncio.wait_for(tarea, timeout=10)

    assert resultado.outcome == "cancelled"
    assert resultado.crash_count == 0, "la muerte de MO2 no es un crash del juego"
    assert 2000 not in mundo.vivos, "D7b: el juego huérfano debe morir aunque close_game no lo alcance"


async def test_fallback_ignora_skyrim_de_otra_instalacion(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, tmp_path: pathlib.Path
) -> None:
    # Fix review #287: un SkyrimSE con nombre y create_time válidos pero cuyo
    # exe vive en OTRA instalación (otro MO2/otro juego del usuario) no se
    # adopta — matarlo en cancel/timeout rompería una sesión ajena.
    otra_instalacion = tmp_path / "otro_juego"
    otra_instalacion.mkdir()
    ajeno = _ProcFake(mundo, 4000, "SkyrimSE.exe", time.time() + 60, exe=str(otra_instalacion / "SkyrimSE.exe"))
    mundo.vivos.add(4000)
    mundo.globales.append(ajeno)

    async def _launch(profile: str) -> dict[str, Any]:
        return {"pid": 999, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    cfg = dataclasses.replace(config, spawn_window_s=0.05)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "spawn_failed"
    assert 4000 in mundo.vivos, "el Skyrim de otra instalación no se toca jamás"


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


async def test_cgid_todos_vacios_es_fallo_silencioso(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Fix review #287 (P1): el zero-bounds también se manifiesta como .cgid de
    # 0 bytes — un cache "presente pero vacío" jamás es success=True.
    chequeos = itertools.count()

    def _al_chequear(pid: int) -> None:
        if next(chequeos) >= 1:
            _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid", datos=b"")
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is False
    assert resultado.cgid_count == 1
    assert resultado.cache_size_mb == 0
    assert "vac" in resultado.message.lower()


async def test_tres_crashes_consecutivos_terminan_en_exito(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # 3 vidas cortas del juego (cada ciclo aporta un .cgid nuevo → sin stall);
    # el 4.º lanzamiento encuentra el precache completo (flag borrado).
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        if n == 3:
            _escribir_cgid(config.overwrite_grass_dir, f"celda_{n}.cgid")
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        else:
            # NGIO appendea la celda al flag: el relanzamiento tiene progreso real.
            _juego_escribe_cgid(config, f"celda_{n}.cgid")
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
    config_rebuild: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    config = config_rebuild
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


async def test_flag_borrado_en_el_ciclo_del_stall_gana_el_exito(
    config_rebuild: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    config = config_rebuild
    # Auditoría adversarial #287: con la racha de stall en el umbral, si NGIO
    # borró el flag en el MISMO ciclo (resume casi completo que muere sin
    # escribir nada), el resultado es completed — jamás stalled.
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid")  # baseline no vacío

    def _al_chequear(pid: int) -> None:
        if pid not in mundo.vivos or mundo.vidas.get(pid) == 0:
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    mundo.al_chequear_pid = _al_chequear
    cfg = dataclasses.replace(config, stall_threshold=1)  # el crash SIN .cgid nuevos ya alcanza el umbral

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "completed", "el flag ausente gana sobre el detector de stall"
    assert resultado.stalled is False


async def test_salida_exitosa_en_spawn_window_mata_al_juego_reparentado(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Auditoría adversarial #287 (D7b): si NGIO borra el flag durante la
    # ventana de spawn ANTES de que el juego fuera atribuido, la salida de
    # éxito hace una última búsqueda y mata al juego reparentado — sin ella
    # quedaría un SkyrimSE huérfano corriendo en modo normal.
    async def _launch(profile: str) -> dict[str, Any]:
        juego = _ProcFake(mundo, 5000, "SkyrimSE.exe", time.time(), exe=str(config.game_path / "SkyrimSE.exe"))
        mundo.vivos.add(5000)
        mundo.globales.append(juego)
        _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid")
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        return {"pid": 999, "status": "launched", "profile": profile}  # MO2 ya muerto

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is True
    assert 5000 not in mundo.vivos, "la última atribución debe matar al juego reparentado"


async def test_pid_reusado_por_otro_proceso_no_se_mata(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, tmp_path: pathlib.Path
) -> None:
    # §2.5: entre la atribución del juego y el kill directo (D7b), el SO puede
    # reusar el PID para OTRO proceso. _kill_game_tree_sync revalida name+exe
    # antes de matar: un PID reusado (exe fuera de game_path, o nombre ajeno)
    # NO se mata — cerrar un Steam/Discord/otro Skyrim ajeno sería el bug.
    otra_instalacion = tmp_path / "steam"
    otra_instalacion.mkdir()
    # El pid 7000 se atribuye como el juego (exe bajo game_path)...
    juego = _ProcFake(mundo, 7000, "SkyrimSE.exe", time.time(), exe=str(config.game_path / "SkyrimSE.exe"))
    mundo.vivos.add(7000)
    mundo.globales.append(juego)

    async def _launch(profile: str) -> dict[str, Any]:
        return {"pid": 999, "status": "launched", "profile": profile}  # MO2 ya muerto

    mo2.launch_game.side_effect = _launch

    vigilando = asyncio.Event()

    def _al_chequear(pid: int) -> None:
        if pid == 7000 and not vigilando.is_set():
            vigilando.set()
            # ...pero JUSTO antes del kill el SO reusa el pid 7000 para otro exe.
            mundo.globales[:] = [
                _ProcFake(mundo, 7000, "steam.exe", time.time(), exe=str(otra_instalacion / "steam.exe"))
            ]

    mundo.al_chequear_pid = _al_chequear
    cancel = asyncio.Event()
    tarea = asyncio.create_task(_runner(config, mo2).run(cancel))

    await asyncio.wait_for(vigilando.wait(), timeout=5.0)
    cancel.set()
    resultado = await asyncio.wait_for(tarea, timeout=10)

    assert resultado.outcome == "cancelled"
    assert 7000 in mundo.vivos, "§2.5: un PID reusado por otro proceso jamás se mata"


async def test_pid_reusado_por_el_mismo_juego_relanzado_no_se_mata(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Review Codex #316: name+exe solos NO alcanzan — un PID reusado por OTRO
    # SkyrimSE.exe de la MISMA instalación (el usuario relanza el juego a mano
    # entre la atribución y el kill) pasaría ese chequeo igual. Debe comparar
    # también create_time contra el capturado al atribuir.
    juego = _ProcFake(mundo, 8000, "SkyrimSE.exe", time.time(), exe=str(config.game_path / "SkyrimSE.exe"))
    mundo.vivos.add(8000)
    mundo.globales.append(juego)

    async def _launch(profile: str) -> dict[str, Any]:
        return {"pid": 999, "status": "launched", "profile": profile}  # MO2 ya muerto

    mo2.launch_game.side_effect = _launch

    vigilando = asyncio.Event()

    def _al_chequear(pid: int) -> None:
        if pid == 8000 and not vigilando.is_set():
            vigilando.set()
            # ...pero JUSTO antes del kill el usuario relanza el MISMO juego:
            # mismo pid (el SO lo reusó), mismo name+exe, OTRO create_time.
            mundo.globales[:] = [
                _ProcFake(mundo, 8000, "SkyrimSE.exe", time.time() + 3600, exe=str(config.game_path / "SkyrimSE.exe"))
            ]

    mundo.al_chequear_pid = _al_chequear
    cancel = asyncio.Event()
    tarea = asyncio.create_task(_runner(config, mo2).run(cancel))

    await asyncio.wait_for(vigilando.wait(), timeout=5.0)
    cancel.set()
    resultado = await asyncio.wait_for(tarea, timeout=10)

    assert resultado.outcome == "cancelled"
    assert 8000 in mundo.vivos, "#316: create_time distinto (mismo name+exe) tampoco se mata"


async def test_permission_error_de_launch_es_spawn_failed_y_file_not_found_propaga(
    config: GrassCacheConfig, mo2: AsyncMock
) -> None:
    # Auditoría adversarial #287: un OSError del spawn (exe sin permiso de
    # ejecución) vuelve como outcome estructurado; FileNotFoundError (MO2
    # ausente = misconfiguración) sigue propagando según el contrato.
    cfg = dataclasses.replace(config, spawn_window_s=0.05)
    mo2.launch_game.side_effect = PermissionError("ejecución denegada")

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "spawn_failed"
    assert not (cfg.game_path / "PrecacheGrass.txt").exists()

    mo2.launch_game.side_effect = FileNotFoundError("MO2 executable not found")
    with pytest.raises(FileNotFoundError):
        await _correr(_runner(cfg, mo2))


async def test_exe_ilegible_no_se_adopta_fail_closed(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Auditoría adversarial #287: exe=None (AccessDenied de psutil) ya no se
    # adopta por nombre+tiempo — matar un proceso sin verificar su instalación
    # puede cerrar la sesión de OTRO Skyrim (elevado/otro usuario).
    ilegible = _ProcFake(mundo, 6000, "SkyrimSE.exe", time.time() + 60, exe=None)
    mundo.vivos.add(6000)
    mundo.globales.append(ilegible)

    async def _launch(profile: str) -> dict[str, Any]:
        return {"pid": 999, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    cfg = dataclasses.replace(config, spawn_window_s=0.05)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "spawn_failed"
    assert 6000 in mundo.vivos, "un proceso sin exe verificable jamás se mata"


async def test_error_transitorio_de_disco_no_aborta_el_run(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Auditoría adversarial #287: un FileNotFoundError de disk_usage (TOCTOU,
    # share de red parpadeando) no debe reventar un run de 12h — se loguea y
    # el ritual continúa.
    lecturas = itertools.count()

    def _disk_usage(_p: Any) -> Any:
        if next(lecturas) == 0:
            return _USO_DISCO(total=100 * _GIB, used=1 * _GIB, free=99 * _GIB)
        raise FileNotFoundError("probe desapareció")

    monkeypatch.setattr("sky_claw.local.tools.grass_cache_runner.shutil.disk_usage", _disk_usage)
    chequeos = itertools.count()

    def _al_chequear(pid: int) -> None:
        if next(chequeos) >= 1:
            _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid")
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is True


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
        _juego_escribe_cgid(config, f"celda_{n}.cgid")
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
    config_rebuild: GrassCacheConfig, mo2: AsyncMock
) -> None:
    config = config_rebuild
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
    config_rebuild: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_rebuild
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
    # Determinista sin ratios de reloj: el flag se borra recién DESPUÉS de que
    # el callback observó un heartbeat "scanning" (en Windows py3.11
    # time.monotonic tiene granularidad ~15.6 ms y un contador de iteraciones
    # puede ganarle al reloj — flake real visto en CI del PR #287).
    progresos: list[GrassCacheProgress] = []
    visto_scanning = False

    async def _on_progress(p: GrassCacheProgress) -> None:
        nonlocal visto_scanning
        progresos.append(p)
        if p.phase == "scanning":
            visto_scanning = True

    def _al_chequear(pid: int) -> None:
        if visto_scanning:
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
        _juego_escribe_cgid(config, f"celda_{n}.cgid")
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    with pytest.raises(RuntimeError, match="bug inesperado"):
        await _correr(_runner(config, mo2))

    mo2.close_game.assert_awaited()
    assert not (config.game_path / "PrecacheGrass.txt").exists(), "el finally limpia el flag ante bugs"


# ---------------------------------------------------------------------------
# P0 SAFE CACHE (F-01) — el arranque jamás destruye el caché sin decisión
# ---------------------------------------------------------------------------
#
# NGIO-NG (1.0.13, ``GidFileCache.cpp::Init``) BORRA ``*.cgid``/``*.dgid``/
# ``*.fail`` de ``Data/Grass`` cuando ``PrecacheGrass.txt`` no tiene progreso.
# El runner escribía el flag vacío SIEMPRE, así que cualquier corrida —incluso
# una que solo quería reanudar— borraba horas de precache. Estos tests fijan el
# contrato NEW/RESUME/REBUILD del lado del runner.


async def test_cache_existente_sin_decision_no_inicia(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    # Caché previo + modo NEW (default): NGIO lo borraría al ver el flag vacío.
    # El runner debe negarse ANTES de lanzar y sin tocar un solo byte.
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(config, mo2))

    assert excinfo.value.codigo == "cache_existente_sin_decision"
    mo2.launch_game.assert_not_awaited()
    assert (config.overwrite_grass_dir / "previo.cgid").read_bytes() == b"prec"
    assert not (config.game_path / "PrecacheGrass.txt").exists()


async def test_flag_con_progreso_no_se_trunca(config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos) -> None:
    # Un flag con progreso es la ÚNICA evidencia de reanudación que NGIO tiene:
    # truncarlo es lo que dispara el borrado del caché. Sin decisión, no se toca.
    flag = config.game_path / "PrecacheGrass.txt"
    progreso = "Tamriel\n-2,4\n"
    flag.write_bytes(progreso.encode("utf-8"))

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(config, mo2))

    assert excinfo.value.codigo == "flag_con_progreso_sin_decision"
    mo2.launch_game.assert_not_awaited()
    assert flag.read_text(encoding="utf-8") == progreso, "el progreso no se trunca"


async def test_rebuild_exige_aprobacion_explicita(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    cfg = dataclasses.replace(config, mode="rebuild")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(cfg, mo2))

    assert excinfo.value.codigo == "rebuild_sin_aprobacion"
    mo2.launch_game.assert_not_awaited()
    assert (config.overwrite_grass_dir / "previo.cgid").exists()


async def test_rebuild_sin_raiz_de_respaldo_no_destruye(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    # Aprobación sin lugar donde respaldar = no hay respaldo verificable, y sin
    # respaldo no se habilita una operación destructiva.
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    # El ack de USVFS ya está dado: lo que falta es dónde dejar el respaldo.
    cfg = dataclasses.replace(config, mode="rebuild", rebuild_approval="REBUILD", ack_usvfs_no_verificado=True)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(cfg, mo2))

    assert excinfo.value.codigo == "rebuild_sin_respaldo"
    mo2.launch_game.assert_not_awaited()
    assert (config.overwrite_grass_dir / "previo.cgid").exists()


async def test_rebuild_con_aprobacion_y_respaldo_respalda_y_arranca(
    config: GrassCacheConfig,
    mo2: AsyncMock,
    mundo: _MundoProcesos,
    tmp_path: pathlib.Path,
) -> None:
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    estado = tmp_path / "estado"
    cfg = dataclasses.replace(
        config,
        mode="rebuild",
        rebuild_approval="REBUILD",
        backup_root=estado,
        ack_usvfs_no_verificado=True,
        max_runtime_s=0.3,
    )

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "timeout"
    assert resultado.backup_dir, "el resultado expone dónde quedó el respaldo"
    respaldo = pathlib.Path(resultado.backup_dir)
    assert (respaldo / "previo.cgid").read_bytes() == b"prec"


async def test_resume_con_flag_con_progreso_lo_conserva(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, tmp_path: pathlib.Path
) -> None:
    # RESUME no reescribe el flag: NGIO reanuda desde ese progreso. Al salir se
    # neutraliza (seguridad del juego del usuario) pero queda archivado.
    progreso = "Tamriel\n-2,4\n"
    flag = config.game_path / "PrecacheGrass.txt"
    flag.write_bytes(progreso.encode("utf-8"))
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    cfg = dataclasses.replace(config, mode="resume", backup_root=tmp_path / "estado", max_runtime_s=0.3)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "timeout"
    assert resultado.mode == "resume"
    assert not flag.exists(), "el flag se neutraliza al salir (el juego no debe arrancar en precache)"
    assert (config.overwrite_grass_dir / "previo.cgid").read_bytes() == b"prec"
    archivados = list((tmp_path / "estado" / "progreso").glob("*.json"))
    assert archivados, "el progreso se archiva como evidencia de reanudación"


async def test_resume_sin_evidencia_no_inicia(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    cfg = dataclasses.replace(config, mode="resume")

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(cfg, mo2))

    assert excinfo.value.codigo == "resume_sin_progreso"
    mo2.launch_game.assert_not_awaited()


async def test_resume_restaura_el_progreso_archivado(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, tmp_path: pathlib.Path
) -> None:
    # Muerte dura previa: el run dejó el progreso archivado con la MISMA huella
    # del caché. RESUME lo restaura antes de lanzar, en vez de empezar de cero.
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    estado = tmp_path / "estado"
    progreso = "Tamriel\n-2,4\n"
    inventario = inventariar_cache(
        grass_dir=config.overwrite_grass_dir, flag_path=config.game_path / "PrecacheGrass.txt"
    )
    archivar_progreso(
        estado, inventario, progreso.encode("utf-8"), grass_dir=config.overwrite_grass_dir, perfil=config.profile
    )
    flag = config.game_path / "PrecacheGrass.txt"
    visto: list[str] = []

    async def _launch(profile: str) -> dict[str, Any]:
        visto.append(flag.read_text(encoding="utf-8"))
        mundo.alta_juego(100, 1100)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    cfg = dataclasses.replace(config, mode="resume", backup_root=estado, max_runtime_s=0.3)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "timeout"
    assert visto and visto[0] == progreso, "NGIO debe ver el progreso restaurado, no un flag vacío"


async def test_resume_no_restaura_el_progreso_de_otro_perfil(
    config: GrassCacheConfig, mo2: AsyncMock, tmp_path: pathlib.Path
) -> None:
    # Hallazgo de revisión: dos perfiles comparten `overwrite/Grass`. El progreso
    # archivado por otro perfil no puede reanudar este: NGIO saltearía celdas que
    # este run todavía no generó.
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    estado = tmp_path / "estado"
    inventario = inventariar_cache(
        grass_dir=config.overwrite_grass_dir, flag_path=config.game_path / "PrecacheGrass.txt"
    )
    archivar_progreso(estado, inventario, b"Tamriel\n", grass_dir=config.overwrite_grass_dir, perfil="Perfil-Ajeno")
    cfg = dataclasses.replace(config, mode="resume", backup_root=estado, max_runtime_s=0.3)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(cfg, mo2))

    assert excinfo.value.codigo == "progreso_de_otra_instalacion"
    mo2.launch_game.assert_not_awaited()


async def test_guard_que_se_niega_no_borra_el_flag_existente(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    # El `finally` sólo neutraliza un flag que el run ADOPTÓ. Si el guard se
    # niega antes de tocarlo, borrarlo cambiaría un borrado de caché por otro.
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    progreso = b"Tamriel\n-2,4\n"
    flag = config.game_path / "PrecacheGrass.txt"
    flag.write_bytes(progreso)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(config, mo2))

    assert excinfo.value.codigo == "cache_existente_sin_decision"
    mo2.launch_game.assert_not_awaited()
    assert flag.read_bytes() == progreso


async def test_flag_residual_se_reporta_y_no_se_da_por_limpio(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CORRECCIÓN 4: un unlink que falla al salir NO puede leerse como limpieza
    # exitosa — el juego del usuario quedaría arrancando en modo precache.
    cfg = dataclasses.replace(config, max_runtime_s=0.2)
    real_unlink = pathlib.Path.unlink

    def _unlink_que_falla(self: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        if self.name == "PrecacheGrass.txt":
            raise PermissionError("el archivo está en uso")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_que_falla)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "timeout"
    assert resultado.flag_residual is True
    assert any("No se pudo eliminar" in error for error in resultado.limpieza_errores)
    assert (cfg.game_path / "PrecacheGrass.txt").exists()


async def test_rebuild_sin_ack_de_usvfs_no_arranca(
    config: GrassCacheConfig, mo2: AsyncMock, tmp_path: pathlib.Path
) -> None:
    # La cobertura USVFS no está demostrada: REBUILD queda en HOLD por defecto.
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    cfg = dataclasses.replace(
        config,
        mode="rebuild",
        rebuild_approval="REBUILD",
        backup_root=tmp_path / "estado",
        ack_usvfs_no_verificado=False,  # el HOLD es justamente por su ausencia
    )

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(cfg, mo2))

    assert excinfo.value.codigo == "usvfs_no_demostrado"
    mo2.launch_game.assert_not_awaited()
    assert (config.overwrite_grass_dir / "previo.cgid").exists()


async def test_new_sin_ack_de_usvfs_no_arranca(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    # B5: NEW también escribe el flag vacío. Sin reconocimiento de la cobertura
    # USVFS no se lanza nada ni se toca el flag.
    cfg = dataclasses.replace(config, ack_usvfs_no_verificado=False)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(cfg, mo2))

    assert excinfo.value.codigo == "usvfs_no_demostrado"
    mo2.launch_game.assert_not_awaited()
    assert not (cfg.game_path / "PrecacheGrass.txt").exists()


async def test_progreso_perdido_llega_al_resultado(
    config: GrassCacheConfig, mo2: AsyncMock, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B4: si el archivado del progreso falla al salir, el flag igual se borra
    # (seguridad del juego) pero la pérdida tiene que viajar en el resultado.
    progreso = b"Tamriel\n-2,4\n"
    flag = config.game_path / "PrecacheGrass.txt"
    flag.write_bytes(progreso)
    _escribir_cgid(config.overwrite_grass_dir, "previo.cgid", datos=b"prec")
    cfg = dataclasses.replace(config, mode="resume", backup_root=tmp_path / "estado", max_runtime_s=0.2)

    def _archivar_falla(*args: Any, **kwargs: Any) -> pathlib.Path:
        raise OSError("no queda espacio en el disco")

    monkeypatch.setattr(grass_cache_safety, "archivar_progreso", _archivar_falla)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.flag_residual is False, "el flag sí se borró"
    assert resultado.progreso_perdido is True
    assert any("archivar" in error.lower() for error in resultado.limpieza_errores)


# ---------------------------------------------------------------------------
# TERCERA REVISIÓN — B9 y el invariante de `run()`
# ---------------------------------------------------------------------------


async def test_error_posterior_a_replace_no_deja_flag_vacio_sin_limpiar(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B9: `os.replace()` escribe el flag y DESPUÉS falla el postcheck. El flag
    # en disco ya es NUESTRO: si el runner no lo adopta, queda un flag vacío
    # que haría que el próximo arranque del juego borre el caché.
    real_read_bytes = pathlib.Path.read_bytes
    lecturas = {"n": 0}

    def _read_bytes_falla_una_vez(self: pathlib.Path) -> bytes:
        if self.name == "PrecacheGrass.txt":
            lecturas["n"] += 1
            if lecturas["n"] == 1:
                raise PermissionError("lectura bloqueada (una vez)")
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _read_bytes_falla_una_vez)

    with pytest.raises(GrassCacheSafetyError) as excinfo:
        await _correr(_runner(config, mo2))

    assert excinfo.value.codigo == "flag_no_verificado"
    mo2.launch_game.assert_not_awaited()
    assert not (config.game_path / "PrecacheGrass.txt").exists(), (
        "el flag escrito antes del postcheck tiene que quedar bajo responsabilidad del runner"
    )


async def test_limpieza_incompleta_no_devuelve_exito(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # El ritual completa, pero la limpieza del flag no: el resultado no puede
    # leerse como éxito por el outcome. El runner lo declara él mismo (no
    # depende de que el servicio lo corrija).
    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid")
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        return {"pid": 999, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    monkeypatch.setattr(
        gcr_mod,
        "neutralizar_flag",
        lambda **kwargs: NeutralizacionFlag(borrado=False, archivado=False, errores=("el archivo está en uso",)),
    )

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.flag_residual is True
    assert resultado.success is False, "una limpieza incompleta no es éxito"
    assert "limpieza" in resultado.message.lower()


# ---------------------------------------------------------------------------
# CUARTA REVISIÓN — B11: la cancelación no puede abandonar la escritura
# ---------------------------------------------------------------------------


async def test_cancelacion_durante_la_escritura_del_flag_no_deja_flag_vacio(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B11: la escritura corre en un hilo. Si la tarea se cancela mientras ese
    # hilo está en vuelo, el `finally` corre ANTES de que el flag exista y la
    # limpieza se omite: queda un flag vacío sin dueño. La sección crítica tiene
    # que esperar a que el hilo termine, conocer su estado y recién entonces
    # abandonar.
    #
    # LA BARRERA VA ANTES DE ESCRIBIR. Con la barrera después (versión anterior:
    # escribir → `estado.escrito = True` → bloquear), el `finally` ya veía
    # `escrito == True` y neutralizaba igual: el test pasaba TAMBIÉN sin
    # `esperar_terminar`, así que no podía matar el mutante M4 (verificado: verde
    # con la protección eliminada). Bloqueando antes de la escritura, sin
    # protección la cancelación gana la carrera y el flag queda escrito después
    # por un hilo huérfano.
    en_espera = threading.Event()
    liberar = threading.Event()
    termino = threading.Event()

    def _aplicar_lento(
        plan: Any, flag_path: pathlib.Path, *, estado: EstadoAplicacion | None = None
    ) -> EstadoAplicacion:
        estado = estado if estado is not None else EstadoAplicacion()
        en_espera.set()
        liberar.wait(timeout=10)  # la barrera está ANTES de tocar el disco
        flag_path.write_bytes(plan.contenido)
        estado.escrito = True
        estado.verificado = True
        termino.set()
        return estado

    monkeypatch.setattr(gcr_mod, "aplicar_plan", _aplicar_lento)
    tarea = asyncio.ensure_future(_correr(_runner(config, mo2)))

    # Determinismo: se cancela EXACTAMENTE mientras el worker todavía NO escribió.
    assert await asyncio.to_thread(en_espera.wait, 5.0), "la escritura no llegó a empezar"
    assert not (config.game_path / "PrecacheGrass.txt").exists(), "la barrera tiene que ir antes de escribir"
    tarea.cancel()
    await asyncio.sleep(0)  # deja que la cancelación se entregue en el await del hilo

    # Discriminador del mutante: sin la protección de `esperar_terminar` la tarea
    # ya estaría terminada acá. Con ella, la operación protegida sigue pendiente.
    assert not tarea.done(), "la cancelación se propagó con la escritura todavía en vuelo"
    liberar.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10)

    assert termino.is_set(), "el run salió antes de que el worker terminara"
    assert not (config.game_path / "PrecacheGrass.txt").exists(), (
        "quedó un flag vacío que el próximo arranque del juego convertiría en borrado del caché"
    )
    mo2.launch_game.assert_not_awaited()


async def test_la_cancelacion_publica_la_limpieza_fallida(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Una cancelación posterior a la adopción del flag puede dejar la limpieza a
    # medias. `_run_interno` propaga CancelledError, así que sin publicar ANTES de
    # propagar, el `unlink` fallido no llega a ningún lado: el flag queda en disco
    # y nadie lo dice.
    import threading

    en_escritura = threading.Event()
    liberar = threading.Event()

    def _aplicar_lento(
        plan: Any, flag_path: pathlib.Path, *, estado: EstadoAplicacion | None = None
    ) -> EstadoAplicacion:
        estado = estado if estado is not None else EstadoAplicacion()
        flag_path.write_bytes(plan.contenido)
        estado.escrito = True
        en_escritura.set()
        liberar.wait(timeout=10)
        estado.verificado = True
        return estado

    def _neutralizar_falla(**kwargs: Any) -> NeutralizacionFlag:
        return NeutralizacionFlag(borrado=False, archivado=False, errores=("unlink bloqueado por el AV",))

    monkeypatch.setattr(gcr_mod, "aplicar_plan", _aplicar_lento)
    monkeypatch.setattr(gcr_mod, "neutralizar_flag", _neutralizar_falla)
    caplog.set_level("WARNING", logger=gcr_mod.__name__)
    tarea = asyncio.ensure_future(_correr(_runner(config, mo2)))

    assert await asyncio.to_thread(en_escritura.wait, 5.0), "la escritura no llegó a empezar"
    tarea.cancel()
    await asyncio.sleep(0)
    liberar.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10)

    avisos = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("NO se pudo eliminar" in aviso for aviso in avisos), (
        f"la limpieza fallida no se publicó antes de propagar la cancelación: {avisos}"
    )
    assert any("unlink bloqueado por el AV" in aviso for aviso in avisos)


# ---------------------------------------------------------------------------
# Revisión adversarial automatizada (CodeRabbit) sobre el drenaje y la cancelación
# ---------------------------------------------------------------------------


async def test_cancelacion_durante_la_preparacion_no_abandona_el_hilo(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # El runner vuelve a llamar a `preparar_arranque` (el servicio ya lo hizo con
    # SU copia). Si la cancelación llega durante ESTA copia y el `await` la deja
    # atrás, el servicio suelta los dos locks y hace teardown del perfil con el
    # hilo todavía escribiendo.
    import threading

    import sky_claw.local.tools.grass_cache_runner as gcr_mod

    real = gcr_mod.preparar_arranque
    en_copia = threading.Event()
    liberar = threading.Event()
    termino = threading.Event()

    def _preparar_lento(**kwargs: Any) -> Any:
        en_copia.set()
        liberar.wait(timeout=10)
        termino.set()
        return real(**kwargs)

    monkeypatch.setattr(gcr_mod, "preparar_arranque", _preparar_lento)
    tarea = asyncio.ensure_future(_correr(_runner(config, mo2)))

    assert await asyncio.to_thread(en_copia.wait, 5.0), "la preparación no llegó a empezar"
    tarea.cancel()
    await asyncio.sleep(0)
    liberar.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10)

    assert termino.is_set(), "la cancelación se propagó con la copia del respaldo en vuelo"
    mo2.launch_game.assert_not_awaited()


async def test_cancelacion_pedida_durante_la_preparacion_no_escribe_el_flag(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cancelación COOPERATIVA (evento) pedida mientras se prepara el arranque: el
    # runner terminaba preparando, escribía el flag vacío y lanzaba el juego. El
    # evento sólo se miraba después del lanzamiento.
    import sky_claw.local.tools.grass_cache_runner as gcr_mod

    real = gcr_mod.preparar_arranque
    evento = asyncio.Event()
    bucle = asyncio.get_running_loop()

    def _preparar_y_pedir_cancelacion(**kwargs: Any) -> Any:
        plan = real(**kwargs)
        bucle.call_soon_threadsafe(evento.set)
        return plan

    monkeypatch.setattr(gcr_mod, "preparar_arranque", _preparar_y_pedir_cancelacion)

    resultado = await _correr(_runner(config, mo2), evento)

    assert resultado.outcome == "cancelled"
    assert "preparación" in resultado.message
    mo2.launch_game.assert_not_awaited()
    assert not (config.game_path / "PrecacheGrass.txt").exists()


async def test_cancelacion_pedida_desde_el_progreso_no_lanza_el_juego(config: GrassCacheConfig, mo2: AsyncMock) -> None:
    # El evento también se puede pedir entre la escritura del flag y el spawn
    # (snapshot/progress). Con el flag ya adoptado, el run sale por el camino de
    # limpieza de siempre, sin lanzar el juego.
    evento = asyncio.Event()

    async def _progreso(info: Any) -> None:
        if info.phase == "launching":
            evento.set()

    resultado = await _correr(_runner(config, mo2, on_progress=_progreso), evento)

    assert resultado.outcome == "cancelled"
    mo2.launch_game.assert_not_awaited()
    assert not (config.game_path / "PrecacheGrass.txt").exists()


# ---------------------------------------------------------------------------
# F2: la neutralización no bloquea el loop y no se abandona
# ---------------------------------------------------------------------------


def _neutralizador_con_barrera(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event, threading.Event]:
    """Sustituye ``neutralizar_flag`` por un worker lento con barreras deterministas."""
    en_hilo = threading.Event()
    liberar = threading.Event()
    termino = threading.Event()

    def _neutralizar_lento(**kwargs: Any) -> NeutralizacionFlag:
        en_hilo.set()
        liberar.wait(timeout=20)
        termino.set()
        return NeutralizacionFlag(borrado=True, archivado=True, errores=())

    monkeypatch.setattr(gcr_mod, "neutralizar_flag", _neutralizar_lento)
    return en_hilo, liberar, termino


def _runner_que_cancela_en_el_progreso(
    config: GrassCacheConfig, mo2: AsyncMock, evento: asyncio.Event
) -> GrassCacheRunner:
    """Runner que pide cancelación al emitir ``launching`` (llega al ``finally``)."""

    async def _progreso(info: Any) -> None:
        if info.phase == "launching":
            evento.set()

    return _runner(config, mo2, on_progress=_progreso)


async def test_la_neutralizacion_corre_en_un_hilo_y_no_bloquea_el_loop(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F2: archivar el progreso lee y hashea TODO el caché parcial en el `finally`.
    # Síncrono, bloquea el loop —donde vive el heartbeat de los locks
    # `grass-cache`/`load-order`— y el lease puede expirar en silencio.
    en_hilo, liberar, termino = _neutralizador_con_barrera(monkeypatch)
    evento = asyncio.Event()
    tarea = asyncio.ensure_future(_correr(_runner_que_cancela_en_el_progreso(config, mo2, evento), evento))

    assert await asyncio.to_thread(en_hilo.wait, 10.0), "la neutralización no llegó a empezar"
    # El loop tiene que seguir despachando MIENTRAS el worker archiva. Si la
    # neutralización fuera síncrona, estos `sleep` no correrían.
    for _ in range(3):
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=5)
    assert not tarea.done(), "el run no puede salir con la neutralización en vuelo"
    liberar.set()

    resultado = await asyncio.wait_for(tarea, timeout=10)

    assert termino.is_set(), "se salió antes de que el worker terminara"
    assert resultado.outcome == "cancelled"


async def test_el_lease_no_expira_y_nadie_mas_entra_mientras_se_neutraliza(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Con locks REALES y TTL corto: durante la neutralización el heartbeat tiene
    # que poder renovar (si el loop está bloqueado, no renueva y el lease vence)
    # y ningún otro dueño puede adquirir hasta que el teardown termine.
    from sky_claw.app.db.locks import (
        DistributedLockManager,
        LockAcquisitionError,
        SnapshotTransactionLock,
    )
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager

    en_hilo, liberar, termino = _neutralizador_con_barrera(monkeypatch)
    manager = DistributedLockManager(tmp_path / "locks.db", default_ttl=1.5, max_retries=1)
    await manager.initialize()
    snaps = FileSnapshotManager(snapshot_dir=tmp_path / "snaps")
    await snaps.initialize()
    evento = asyncio.Event()
    try:
        async with SnapshotTransactionLock(
            lock_manager=manager,
            snapshot_manager=snaps,
            resource_id="grass-cache",
            agent_id="grass-test",
            ttl=1.5,
        ) as tx:
            assert tx.lock_info is not None
            expira_antes = tx.lock_info.expires_at
            tarea = asyncio.ensure_future(_correr(_runner_que_cancela_en_el_progreso(config, mo2, evento), evento))

            assert await asyncio.to_thread(en_hilo.wait, 10.0), "la neutralización no llegó a empezar"
            # Más que un intervalo de heartbeat (TTL/3 = 0.5 s) con el worker vivo.
            await asyncio.sleep(1.2)
            info = await manager.get_lock_info("grass-cache")
            assert info is not None, "el lease venció mientras se neutralizaba"
            assert info.expires_at > expira_antes, "el heartbeat no pudo renovar: el loop estaba bloqueado"
            with pytest.raises(LockAcquisitionError):
                await manager.acquire_lock("grass-cache", "otro-dueno", ttl=1.5)
            assert not tarea.done(), "el run salió con el worker todavía archivando"

            liberar.set()
            resultado = await asyncio.wait_for(tarea, timeout=10)
            assert termino.is_set(), "se liberó el lock con una tarea de escritura activa"
            assert resultado.outcome == "cancelled"
            assert not tx.lease_lost
        # Fuera del `async with`: la lease se liberó.
        assert await manager.get_lock_info("grass-cache") is None
    finally:
        liberar.set()
        await manager.close()


async def test_el_lease_sigue_vigente_durante_una_neutralizacion_mas_larga_que_el_ttl(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Reproducción REAL de la pérdida silenciosa de lease (F2): TTL de 1,5 s y una
    # neutralización que "hashea" durante 2,5 s. Un OBSERVADOR INDEPENDIENTE —otro
    # proceso lógico, con su propia conexión y su propio loop, como el servicio
    # hermano— mira la fila del lock en plena ventana. Si la renovación vive en el
    # loop y la neutralización lo bloquea, la fila está VENCIDA ahí mismo: el dueño
    # sigue mutando el caché sin exclusividad y nadie se entera (`renew_lock` no
    # resucita vencidos, pero el heartbeat nunca llega a intentarlo porque
    # `__aexit__` cancela la tarea antes).
    from sky_claw.app.db.locks import DistributedLockManager, SnapshotTransactionLock
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager

    ruta_bd = tmp_path / "locks.db"
    suelta = threading.Event()
    lectura: dict[str, Any] = {}

    def _neutralizar_lento(**kwargs: Any) -> NeutralizacionFlag:
        suelta.wait(timeout=20)
        return NeutralizacionFlag(borrado=True, archivado=True, errores=())

    monkeypatch.setattr(gcr_mod, "neutralizar_flag", _neutralizar_lento)

    def _observar() -> None:
        """Mira la fila desde OTRO loop/conexión, en plena ventana de bloqueo."""
        time.sleep(2.0)

        async def _leer() -> None:
            lector = DistributedLockManager(ruta_bd, default_ttl=1.5, max_retries=1)
            await lector.initialize()
            try:
                for _ in range(5):
                    try:
                        lectura["info"] = await lector.get_lock_info("grass-cache")
                        lectura["ahora"] = time.time()
                        lectura.pop("error", None)  # un reintento exitoso no deja el error viejo
                        return
                    except Exception as exc:  # noqa: BLE001 — reintento corto por lock de SQLite
                        lectura["error"] = repr(exc)
                        time.sleep(0.05)
            finally:
                await lector.close()

        asyncio.run(_leer())

    manager = DistributedLockManager(ruta_bd, default_ttl=1.5, max_retries=1)
    await manager.initialize()
    snaps = FileSnapshotManager(snapshot_dir=tmp_path / "snaps")
    await snaps.initialize()
    evento = asyncio.Event()
    observador = threading.Thread(target=_observar, daemon=True)
    liberacion: threading.Timer | None = None
    try:
        async with SnapshotTransactionLock(
            lock_manager=manager,
            snapshot_manager=snaps,
            resource_id="grass-cache",
            agent_id="grass-test",
            ttl=1.5,
        ):
            observador.start()
            # La ventana de bloqueo (2,5 s) la cierra un temporizador, no el loop:
            # en el mutante el loop está bloqueado y ningún `await` propio correría.
            liberacion = threading.Timer(2.5, suelta.set)
            liberacion.start()
            tarea = asyncio.ensure_future(_correr(_runner_que_cancela_en_el_progreso(config, mo2, evento), evento))
            resultado = await asyncio.wait_for(tarea, timeout=15)
            assert resultado.outcome == "cancelled"
            observador.join(timeout=15)
            assert not observador.is_alive(), "el observador no terminó"
            assert "error" not in lectura, f"el observador no pudo leer: {lectura['error']}"
            info = lectura["info"]
            assert info is not None, "el lock desapareció mientras se neutralizaba"
            assert info.expires_at > lectura["ahora"], (
                "el lease estaba VENCIDO en plena neutralización: se perdió la exclusividad "
                f"(expires_at={info.expires_at:.3f} < ahora={lectura['ahora']:.3f})"
            )
    finally:
        if liberacion is not None:
            liberacion.cancel()
        suelta.set()
        observador.join(timeout=5)
        await manager.close()


async def test_la_neutralizacion_cancelada_igual_registra_el_resultado(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # La asignación del resultado NO puede depender del `await` del llamador: si la
    # cancelación llega mientras el hilo neutraliza, `esperar_terminar` espera y
    # propaga CancelledError, así que un `self._neutralizacion = await …` externo
    # nunca corre y el fallo del worker (flag residual) se pierde en silencio.
    en_hilo, liberar, termino = _neutralizador_con_barrera(monkeypatch)
    fallo = NeutralizacionFlag(borrado=False, archivado=True, errores=("no se pudo borrar el flag",))

    def _neutralizar_y_fallar(**kwargs: Any) -> NeutralizacionFlag:
        en_hilo.set()
        liberar.wait(timeout=20)
        termino.set()
        return fallo

    monkeypatch.setattr(gcr_mod, "neutralizar_flag", _neutralizar_y_fallar)
    evento = asyncio.Event()
    runner = _runner_que_cancela_en_el_progreso(config, mo2, evento)
    tarea = asyncio.ensure_future(runner.run(evento))

    assert await asyncio.to_thread(en_hilo.wait, 10.0), "la neutralización no llegó a empezar"
    tarea.cancel()
    liberar.set()
    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarea, timeout=10)

    assert termino.is_set(), "el worker terminó: el flag quedó neutralizado antes de propagar"
    assert runner.neutralizacion is not None, "se perdió el resultado de la neutralización al cancelar"
    assert runner.neutralizacion is fallo
    assert any("neutraliz" in r.message.lower() for r in caplog.records), (
        "el fallo de la limpieza tiene que quedar registrado, no sólo en memoria"
    )


# ---------------------------------------------------------------------------
# Relanzamiento: el flag tiene que seguir siendo PROGRESO para NGIO
# ---------------------------------------------------------------------------


async def test_resume_no_relanza_si_el_flag_perdio_el_progreso(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Un tercero (barrido, antivirus, operador) vacía el flag entre el crash y el
    # relanzamiento. El archivo EXISTE, así que `_flag_exists()` no alcanza: NGIO
    # lo leería sin progreso y borraría el caché parcial. No se relanza.
    config = dataclasses.replace(config, mode="resume")
    _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid", datos=b"celda-tamriel")
    (config.game_path / "PrecacheGrass.txt").write_bytes(b"Tamriel\n")
    disparado = {"ya": False}

    def _al_chequear(pid: int) -> None:
        if pid in mundo.vivos and mundo.vidas.get(pid) == 0 and not disparado["ya"]:
            disparado["ya"] = True
            (config.game_path / "PrecacheGrass.txt").write_bytes(b"")

    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "flag_sin_progreso"
    assert resultado.success is False
    assert mo2.launch_game.await_count == 1, "no puede haber un segundo lanzamiento"


async def test_new_no_reinicia_destructivamente_un_cache_parcial(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # NEW escribe el flag vacío por contrato y el primer lanzamiento es válido.
    # Después de un crash, si el flag sigue sin progreso y YA hay .cgid, relanzar
    # deja que NGIO borre lo generado: se corta sin tocar el caché.
    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "celda_0.cgid")
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "flag_sin_progreso"
    assert resultado.success is False
    assert mo2.launch_game.await_count == 1
    assert (config.overwrite_grass_dir / "celda_0.cgid").exists(), "el caché parcial no se borra"


async def test_progreso_valido_permite_seguir_el_crash_loop(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # NGIO APPENDEA progreso al flag mientras genera (Init() abre en std::ios::app):
    # con progreso válido, el crash-loop sigue igual que antes.
    config = dataclasses.replace(config, mode="resume")
    _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid", datos=b"celda-tamriel")
    flag = config.game_path / "PrecacheGrass.txt"
    flag.write_bytes(b"Tamriel\n")
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        if n == 2:
            flag.unlink(missing_ok=True)  # NGIO terminó: borró el flag
        else:
            _escribir_cgid(config.overwrite_grass_dir, f"celda_{n}.cgid")
            with flag.open("ab") as fh:
                fh.write(f"celda_{n}\n".encode())
            mundo.alta_juego(100 + n, 1100 + n, vidas=1)
        return {"pid": 100 + n, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert mo2.launch_game.await_count == 3
    assert resultado.crash_count == 2


async def test_el_primer_lanzamiento_de_rebuild_con_flag_vacio_sigue_adelante(
    config_rebuild: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # REBUILD archiva el progreso y escribe el flag VACÍO a propósito. El primer
    # lanzamiento es válido y no puede confundirse con un flag que perdió progreso.
    config = config_rebuild
    _escribir_cgid(config.overwrite_grass_dir, "Tamriel.cgid", datos=b"celda-tamriel")
    cancel = asyncio.Event()

    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100, vidas=50)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    def _al_chequear(pid: int) -> None:
        if pid == 1100:  # el juego está bajo vigilancia: el lanzamiento ocurrió
            cancel.set()

    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2), cancel)

    assert resultado.outcome == "cancelled"
    assert mo2.launch_game.await_count == 1
    assert resultado.success is False


async def test_el_corte_del_relanzamiento_deja_limpieza_y_no_borra_el_cache(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # El corte es una salida ORDENADA: el flag se neutraliza (no queda residual,
    # el juego del usuario no arranca en modo precache), el caché parcial queda y
    # el resultado no es exitoso ni simula una limpieza completa.
    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "celda_0.cgid")
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    runner = _runner(config, mo2)

    resultado = await _correr(runner)

    assert resultado.outcome == "flag_sin_progreso"
    assert resultado.flag_residual is False
    assert resultado.success is False
    assert not (config.game_path / "PrecacheGrass.txt").exists(), "el flag no puede quedar residual"
    assert (config.overwrite_grass_dir / "celda_0.cgid").exists()
    assert runner.neutralizacion is not None and runner.neutralizacion.limpio

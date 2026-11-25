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
    GrassCacheOutcome,
    GrassCacheProgress,
    GrassCacheRunner,
    GrassCacheRunResult,
)
from sky_claw.local.tools.grass_cache_safety import (
    EstadoAplicacion,
    GrassCacheMode,
    GrassCacheSafetyError,
    NeutralizacionFlag,
    aplicar_plan,
    archivar_progreso,
    inventariar_cache,
    preparar_arranque,
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
        # Presupuesto de terminalidad en escala de test: el default de producción (10 s) no cabe en
        # tests con `max_runtime_s` de décimas. Los casos que necesitan esperar lo suben explícito.
        terminality_wait_s=0.2,
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
    # Ciclo 0 spawnea, crashea y deja el rastro de un ciclo NGIO real (un .cgid +
    # el progreso appendeado); los siguientes no spawnean ni escriben. Sin .cgid
    # nuevos, el stall detector acota la repetición (no aborta como spawn_failed).
    # Ronda 10: el rastro del ciclo 0 es obligatorio; con el flag vacío el runner
    # ya no relanza (outcome flag_sin_progreso) y este test quedaría sin cubrir
    # el stall detector.
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        if n == 0:
            _juego_escribe_cgid(config, "celda_0.cgid")
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
    # P1 (D1 del propietario): el caché de partida ya estaba y la corrida no escribió NADA, así que
    # el flag ausente no alcanza para declarar éxito. Antes este assert decía `success is True`: era
    # el falso éxito de F-02 congelado en un test.
    assert resultado.success is False
    assert resultado.verdict == "completed_unverified"
    assert "archivos_heredados_sin_procedencia" in resultado.verdict_razones
    assert resultado.certificado is False
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
    # El juego completa una celda y después muere siempre en la misma: el ciclo 0
    # deja el progreso que NGIO appendea (ronda 10: sin él no hay relanzamiento,
    # y el stall detector no llegaría a evaluarse), los siguientes no escriben nada.
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamada)
        if n == 0:
            _juego_escribe_cgid(config, "celda_0.cgid")
        mundo.alta_juego(100 + n, 1100 + n, vidas=1)
        return {"pid": 100 + n, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    cfg = dataclasses.replace(config, stall_threshold=3)

    resultado = await _correr(_runner(cfg, mo2))

    assert resultado.outcome == "stalled"
    assert resultado.stalled is True
    assert resultado.success is False
    # 4 = el ciclo 0 (que sí dejó caché) + 3 relanzamientos consecutivos sin .cgid nuevo:
    # el threshold corta sin quemar max_restarts.
    assert resultado.crash_count == 4, "corta en el threshold, sin quemar max_restarts"


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
    # El hecho tiene que quedar publicado ANTES de propagar; da igual cuál de los
    # dos puntos lo publique (el trabajo drenado o `run()`), pero una sola vez.
    publicados = [aviso for aviso in avisos if "NO se pudo eliminar" in aviso or "Neutralización" in aviso]
    assert len(publicados) == 1, f"la limpieza fallida no se publicó exactamente una vez: {avisos}"
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
    en_hilo = threading.Event()
    lectura: dict[str, Any] = {}

    def _neutralizar_lento(**kwargs: Any) -> NeutralizacionFlag:
        # Barrera determinista: el worker señala al entrar y permanece bloqueado
        # hasta que el observador termine de leer el lease (con timeout
        # independiente como protección contra cuelgues).
        en_hilo.set()
        if not suelta.wait(timeout=10):
            lectura["timeout_worker"] = True
            suelta.set()
        return NeutralizacionFlag(borrado=True, archivado=True, errores=())

    monkeypatch.setattr(gcr_mod, "neutralizar_flag", _neutralizar_lento)

    def _observar() -> None:
        """Mira la fila desde OTRO loop/conexión, DENTRO de la ventana de bloqueo."""
        try:
            if not en_hilo.wait(timeout=15):
                lectura["error"] = "la neutralización nunca empezó"
                return
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
            # Se registra DESPUÉS de leer el lease y ANTES de soltar al worker:
            # si el worker se soltó por timeout antes de terminar _leer(), falla.
            lectura["durante"] = not suelta.is_set()
        finally:
            suelta.set()

    manager = DistributedLockManager(ruta_bd, default_ttl=1.5, max_retries=1)
    await manager.initialize()
    snaps = FileSnapshotManager(snapshot_dir=tmp_path / "snaps")
    await snaps.initialize()
    evento = asyncio.Event()
    observador = threading.Thread(target=_observar, daemon=True)
    try:
        async with SnapshotTransactionLock(
            lock_manager=manager,
            snapshot_manager=snaps,
            resource_id="grass-cache",
            agent_id="grass-test",
            ttl=1.5,
        ):
            observador.start()
            tarea = asyncio.ensure_future(_correr(_runner_que_cancela_en_el_progreso(config, mo2, evento), evento))
            resultado = await asyncio.wait_for(tarea, timeout=15)
            assert resultado.outcome == "cancelled"
            observador.join(timeout=15)
            assert not observador.is_alive(), "el observador no terminó"
            assert "error" not in lectura, f"el observador no pudo leer: {lectura['error']}"
            assert not lectura.get("timeout_worker"), "el worker salió por timeout antes de terminar la lectura"
            info = lectura["info"]
            assert info is not None, "el lock desapareció mientras se neutralizaba"
            assert lectura.get("durante") is True, "la observación cayó fuera de la neutralización"
            assert info.expires_at > lectura["ahora"], (
                "el lease estaba VENCIDO en plena neutralización: se perdió la exclusividad "
                f"(expires_at={info.expires_at:.3f} < ahora={lectura['ahora']:.3f})"
            )
    finally:
        suelta.set()
        observador.join(timeout=5)
        await manager.close()


async def test_la_limpieza_incompleta_se_avisa_una_sola_vez(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Un unlink fallido se publicaba DOS veces: al neutralizar (dentro del trabajo
    # drenado) y otra vez desde `run()`. El operador ve un solo hecho; el resultado
    # sigue siendo el mismo (`flag_residual`, `success=False`).
    monkeypatch.setattr(
        gcr_mod,
        "neutralizar_flag",
        lambda **kwargs: NeutralizacionFlag(borrado=False, archivado=True, errores=("no se pudo borrar",)),
    )

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "celda.cgid", datos=b"celda")
        (config.game_path / "PrecacheGrass.txt").unlink()
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    with caplog.at_level(logging.WARNING, logger="sky_claw.local.tools.grass_cache_runner"):
        resultado = await _correr(_runner(config, mo2))

    avisos = [
        registro
        for registro in caplog.records
        if "Neutralización del flag incompleta" in registro.getMessage()
        or "NO se pudo eliminar" in registro.getMessage()
    ]
    assert len(avisos) == 1, f"el fallo de limpieza se avisó {len(avisos)} veces: {[r.getMessage() for r in avisos]}"
    assert resultado.flag_residual is True
    assert resultado.success is False


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


# ---------------------------------------------------------------------------
# Ronda 11 — contrato ÚNICO de autorización, inmediatamente antes del spawn
# ---------------------------------------------------------------------------

#: Progreso que NGIO leería como reanudación (formato de ``PrecacheGrass.txt``).
_PROGRESO_DEL_PLAN = b"Tamriel\n-2,4\n"
#: Progreso válido pero DISTINTO del que el plan aplicó (tercero/otra corrida).
_PROGRESO_AJENO = b"Solstheim\n"


def _tiene_progreso(contenido: bytes | None) -> bool:
    """Predicado de NGIO (``getline`` + ``trim`` de ``" \\r\\v\\n"``) sobre el fixture."""
    if contenido is None:
        return False
    return any(linea.strip(b" \r\v\n") for linea in contenido.split(b"\n"))


@pytest.fixture(params=["new", "resume", "rebuild"])
def escenario(
    request: pytest.FixtureRequest, config: GrassCacheConfig, tmp_path: pathlib.Path
) -> tuple[GrassCacheMode, GrassCacheConfig, Any]:
    """(modo, cfg, plan REAL ya aplicado) en el estado que ese modo autoriza.

    El plan se construye con la misma función que usa el runner
    (``preparar_arranque``) y se aplica con ``aplicar_plan``: la matriz mide el
    contrato de lanzamiento sobre el estado real que deja el ritual, no sobre un
    plan de juguete. Cada caso después lo muta (flag, caché, alternativo).
    """
    modo = request.param
    cfg = dataclasses.replace(
        config,
        mode=modo,
        ack_usvfs_no_verificado=True,
        rebuild_approval="REBUILD",
        backup_root=tmp_path / "estado_grass",
    )
    flag = cfg.game_path / "PrecacheGrass.txt"
    if modo == "resume":
        _escribir_cgid(cfg.overwrite_grass_dir, "Tamriel.cgid", datos=b"celda-tamriel")
        flag.write_bytes(_PROGRESO_DEL_PLAN)
    elif modo == "rebuild":
        _escribir_cgid(cfg.overwrite_grass_dir, "previo.cgid", datos=b"previo")
    plan = preparar_arranque(
        modo=modo,
        game_path=cfg.game_path,
        grass_dir=cfg.overwrite_grass_dir,
        aprobacion=cfg.rebuild_approval,
        raiz_estado=cfg.backup_root,
        ack_usvfs_no_verificado=True,
        perfil=cfg.perfil_de_progreso,
    )
    aplicar_plan(plan, flag)
    return modo, cfg, plan


def _contenido_tras_la_mutacion(estado: str, plan: Any) -> bytes | None:
    return {
        "igual_al_plan": plan.contenido,
        "vacio": b"",
        "ausente": None,
        "progreso_ajeno": _PROGRESO_AJENO,
    }[estado]


@pytest.mark.parametrize("intento", [0, 1], ids=["primer_lanzamiento", "relanzamiento"])
@pytest.mark.parametrize("primario", ["igual_al_plan", "vacio", "ausente", "progreso_ajeno"])
@pytest.mark.parametrize("alternativo", ["ausente", "presente"])
@pytest.mark.parametrize("cambio_cache", ["ninguno", "agregar", "vaciar"])
async def test_matriz_de_autorizacion_de_lanzamiento(
    escenario: tuple[GrassCacheMode, GrassCacheConfig, Any],
    mo2: AsyncMock,
    intento: int,
    primario: str,
    alternativo: str,
    cambio_cache: str,
) -> None:
    # Matriz completa del contrato: 3 modos × primer lanzamiento/relanzamiento ×
    # 4 estados del flag primario × alternativo ausente/presente × 3 cambios del
    # caché = 144 casos. Cada fila verifica el veredicto Y su código.
    modo, cfg, plan = escenario
    flag = cfg.game_path / "PrecacheGrass.txt"
    grass = cfg.overwrite_grass_dir

    contenido = _contenido_tras_la_mutacion(primario, plan)
    if contenido is None:
        flag.unlink(missing_ok=True)
    else:
        flag.write_bytes(contenido)
    if cambio_cache == "agregar":
        _escribir_cgid(grass, "extra.cgid", datos=b"extra")
    elif cambio_cache == "vaciar":
        for ruta in grass.glob("*"):
            ruta.unlink()
    if alternativo == "presente":
        (cfg.game_path / "PrecacheGrass.txt.txt").write_bytes(_PROGRESO_AJENO)

    corte = await _runner(cfg, mo2)._autorizar_lanzamiento(intento=intento, plan=plan)
    obtenido = corte.outcome if corte is not None else None

    hay_cache = grass.exists() and any(grass.iterdir())
    if alternativo == "presente":
        # Invariante 5: el alternativo bloquea TODOS los lanzamientos, incluso con
        # el primario lleno de progreso.
        esperado: GrassCacheOutcome | None = "flag_alternativo_presente"
    elif intento:
        # Invariante 4: todo relanzamiento exige progreso, sea cual sea el modo y
        # el inventario físico.
        esperado = None if _tiene_progreso(contenido) else "flag_sin_progreso"
    elif contenido != plan.contenido:
        # Invariantes 1-3: el flag tiene que seguir siendo el que el plan aplicó.
        esperado = "cache_cambio_despues_del_plan"
    elif modo == "new":
        # Invariante 1: el inventario físico tiene que seguir vacío.
        esperado = "cache_cambio_despues_del_plan" if hay_cache else None
    elif modo == "rebuild":
        # Invariante 2: el respaldo tiene que seguir cubriendo el caché actual.
        esperado = "cache_cambio_despues_del_plan" if cambio_cache != "ninguno" else None
    else:
        # Invariante 3: en RESUME el inventario físico no participa de la decisión.
        esperado = None

    assert obtenido == esperado, (
        f"modo={modo} intento={intento} primario={primario} alternativo={alternativo} "
        f"cambio_cache={cambio_cache}: se esperaba {esperado!r} y se obtuvo {obtenido!r} "
        f"({corte.motivo if corte else 'lanzamiento autorizado'})"
    )


async def test_el_relanzamiento_no_se_autoriza_con_cache_fisico_vacio(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # El caso exacto que reprodujo la revisión: `overwrite/Grass` vacío y flag sin
    # progreso. Antes se relanzaba ("no hay nada que borrar"), aunque la USVFS puede
    # superponer `.cgid` de mods que NGIO SÍ borraría.
    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100, vidas=1)  # el juego muere sin escribir nada
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "flag_sin_progreso"
    assert resultado.success is False
    assert mo2.launch_game.await_count == 1, "no se relanza sin progreso, ni con el caché físico vacío"


async def test_resume_con_cache_fisico_vacio_no_relanza_si_el_flag_pierde_el_progreso(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # El escenario exacto que pidió la revisión: `resume` arranca con progreso y el
    # caché FÍSICO vacío (el plan lo autoriza: `conservar`), y un tercero vacía el
    # flag tras el crash. El vacío físico no acredita nada —la USVFS puede superponer
    # `.cgid` de mods—, así que no hay segundo lanzamiento.
    config = dataclasses.replace(config, mode="resume")
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
    assert mo2.launch_game.await_count == 1, "el caché físico vacío no autoriza un segundo lanzamiento"


# ---------------------------------------------------------------------------
# Ronda 11 — el contrato único, de punta a punta (callback `launching` incluido)
# ---------------------------------------------------------------------------


async def test_new_no_lanza_si_el_cache_aparece_durante_el_callback_launching(
    config: GrassCacheConfig, mo2: AsyncMock
) -> None:
    # La ventana real entre la última comprobación del plan y el spawn es el
    # callback `launching`. Si ahí aparece un .cgid, NEW ya no tiene la cobertura
    # que el plan evaluó (caché vacío) y no puede lanzar.
    async def _progreso(info: Any) -> None:
        if info.phase == "launching":
            _escribir_cgid(config.overwrite_grass_dir, "intruso.cgid", datos=b"intruso")

    resultado = await _correr(_runner(config, mo2, on_progress=_progreso))

    assert resultado.outcome == "cache_cambio_despues_del_plan"
    assert resultado.success is False
    mo2.launch_game.assert_not_awaited()
    assert (config.overwrite_grass_dir / "intruso.cgid").exists(), "no se borra nada"


async def test_rebuild_no_lanza_si_el_cache_cambia_durante_el_callback_launching(
    config_rebuild: GrassCacheConfig, mo2: AsyncMock
) -> None:
    # El respaldo verificó el caché al planificar; si el contenido cambia después
    # (callback `launching`), el respaldo ya no cubre lo que NGIO borraría.
    _escribir_cgid(config_rebuild.overwrite_grass_dir, "previo.cgid", datos=b"previo")

    async def _progreso(info: Any) -> None:
        if info.phase == "launching":
            (config_rebuild.overwrite_grass_dir / "previo.cgid").write_bytes(b"contenido-nuevo")

    resultado = await _correr(_runner(config_rebuild, mo2, on_progress=_progreso))

    assert resultado.outcome == "cache_cambio_despues_del_plan"
    assert "respaldo" in resultado.message.lower()
    mo2.launch_game.assert_not_awaited()
    assert (config_rebuild.overwrite_grass_dir / "previo.cgid").read_bytes() == b"contenido-nuevo"


async def test_resume_no_spawnea_si_el_flag_se_vacia_durante_el_callback_launching(
    config: GrassCacheConfig, mo2: AsyncMock
) -> None:
    # RESUME conserva el progreso byte a byte; si un tercero lo vacía en la
    # ventana del callback, el primer spawn no puede ocurrir.
    cfg = dataclasses.replace(config, mode="resume")
    (cfg.game_path / "PrecacheGrass.txt").write_bytes(_PROGRESO_DEL_PLAN)

    async def _progreso(info: Any) -> None:
        if info.phase == "launching":
            (cfg.game_path / "PrecacheGrass.txt").write_bytes(b"")

    resultado = await _correr(_runner(cfg, mo2, on_progress=_progreso))

    assert resultado.outcome == "cache_cambio_despues_del_plan"
    mo2.launch_game.assert_not_awaited()


async def test_cancelar_durante_la_inspeccion_no_lanza_el_juego(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Invariante 6: la inspección hace E/S. Si la cancelación llega AHÍ (no antes),
    # el veredicto ya no alcanza: hay que volver a mirar antes del spawn.
    cancel = asyncio.Event()
    original = gcr_mod.inventariar_cache
    llamadas = {"n": 0}

    def _inventariar(**kwargs: Any) -> Any:
        llamadas["n"] += 1
        cancel.set()  # llega durante la inspección, no durante la preparación
        return original(**kwargs)

    monkeypatch.setattr(gcr_mod, "inventariar_cache", _inventariar)

    resultado = await _correr(_runner(config, mo2), cancel)

    assert llamadas["n"] == 1, "el disparo tiene que ser la inspección previa al lanzamiento"
    assert resultado.outcome == "cancelled"
    assert "autorización" in resultado.message
    mo2.launch_game.assert_not_awaited()


async def test_el_deadline_vencido_durante_la_inspeccion_no_lanza_el_juego(
    config: GrassCacheConfig, mo2: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Invariante 6 (segunda mitad): si el deadline se agota mientras se inspecciona,
    # el run sale por timeout y no deja un juego recién lanzado fuera de plazo.
    cfg = dataclasses.replace(config, max_runtime_s=30.0)
    runner = _runner(cfg, mo2)
    original = gcr_mod.inventariar_cache

    def _inventariar(**kwargs: Any) -> Any:
        runner._start_mono = time.monotonic() - cfg.max_runtime_s - 1.0
        return original(**kwargs)

    monkeypatch.setattr(gcr_mod, "inventariar_cache", _inventariar)

    resultado = await _correr(runner)

    assert resultado.outcome == "timeout"
    assert "autorización" in resultado.message
    mo2.launch_game.assert_not_awaited()


async def test_el_flag_alternativo_aparecido_en_launching_bloquea_el_spawn(
    config: GrassCacheConfig, mo2: AsyncMock
) -> None:
    # Invariante 5: el alternativo bloquea el lanzamiento aunque el primario esté
    # en su estado esperado, y al salir se reporta como residual (no es nuestro).
    async def _progreso(info: Any) -> None:
        if info.phase == "launching":
            (config.game_path / "PrecacheGrass.txt.txt").write_bytes(_PROGRESO_AJENO)

    resultado = await _correr(_runner(config, mo2, on_progress=_progreso))

    assert resultado.outcome == "flag_alternativo_presente"
    assert resultado.success is False
    assert resultado.flag_alternativo_residual is True
    mo2.launch_game.assert_not_awaited()
    assert (config.game_path / "PrecacheGrass.txt.txt").exists(), "un flag ajeno no se borra"
    assert not (config.game_path / "PrecacheGrass.txt").exists(), "el primario sí se neutraliza"


async def test_un_flag_alternativo_residual_invalida_la_limpieza_completa(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    # Un run que termina bien (NGIO borra el flag y deja .cgid) sigue sin ser
    # "limpieza completa" si quedó un alternativo: NGIO podría abrirlo como flag
    # de progreso en el próximo arranque manual. Se preserva y se declara.
    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "celda.cgid", datos=b"celda")
        mundo.alta_juego(100, 1100, vidas=3)
        (config.game_path / "PrecacheGrass.txt").unlink()
        (config.game_path / "PrecacheGrass.txt.txt").write_bytes(_PROGRESO_AJENO)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "completed"
    assert resultado.success is False, "una limpieza incompleta no es éxito"
    assert resultado.flag_alternativo_residual is True
    assert "ALTERNATIVO" in resultado.message
    assert resultado.cgid_count == 1
    assert (config.game_path / "PrecacheGrass.txt.txt").exists()


# ---------------------------------------------------------------------------
# P1 · veredicto por evidencia (F-02/F-12) y watchdog de progreso (F-05)
# ---------------------------------------------------------------------------


async def test_p1_rebuild_sin_regeneracion_no_declara_exito(
    config_rebuild: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """F-02: el caché ya estaba y la corrida no escribió nada; el flag ausente no certifica."""
    config = config_rebuild
    _escribir_cgid(config.overwrite_grass_dir, "Tamrielx0000y0000.cgid")

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
    assert resultado.verdict == "completed_unverified"
    assert resultado.certificado is False
    assert resultado.success is False
    assert resultado.alcance_certificado == "ninguno"
    assert "archivos_heredados_sin_procedencia" in resultado.verdict_razones
    assert resultado.evidencia["archivos_heredados"] == 1
    assert resultado.evidencia["archivos_nuevos"] == 0
    assert resultado.evidencia["completitud_de_celdas"] == "no_verificada"


async def test_p1_fail_nuevo_de_la_corrida_impide_certificar(
    config_rebuild: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """C4: un `.fail` que escribe ESTA corrida es una celda que no completó."""
    config = config_rebuild

    def _al_chequear(pid: int) -> None:
        if pid not in mundo.vivos or mundo.vidas.get(pid) == 0:
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamrielx0002y0000.cgid")
        (config.overwrite_grass_dir / "Tamrielx0003y0000.fail").write_bytes(b"1\n")
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.verdict == "completed_unverified"
    assert resultado.certificado is False
    # El trabajo ocurrió (la corrida escribió un .cgid): eso sostiene el éxito operacional...
    assert resultado.success is True
    # ...pero el fallo de celda impide certificar.
    assert "fallos_de_celda_atribuibles" in resultado.verdict_razones
    assert resultado.evidencia["fallos_de_celda_atribuibles"] == 1


async def test_p1_fail_historico_no_se_atribuye_pero_tampoco_certifica(
    config_rebuild: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """C4: un `.fail` con el MISMO contador no es de esta corrida, pero el caché tiene un hueco."""
    config = config_rebuild
    grass = config.overwrite_grass_dir
    _escribir_cgid(grass, "Tamrielx0000y0000.cgid")
    (grass / "Tamrielx0001y0000.fail").write_bytes(b"2\n")

    def _al_chequear(pid: int) -> None:
        if pid not in mundo.vivos or mundo.vidas.get(pid) == 0:
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.verdict == "completed_unverified"
    assert resultado.evidencia["fallos_de_celda_historicos"] == 1
    assert resultado.evidencia["fallos_de_celda_atribuibles"] == 0
    assert (grass / "Tamrielx0001y0000.fail").read_bytes() == b"2\n"


async def test_p1_new_certifica_con_evidencia_y_alcance_de_corrida_completa(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """Contraprueba: con evidencia completa el veredicto SÍ certifica (no es un «no» permanente)."""
    config = dataclasses.replace(config, worldspaces=("Tamriel", "DLC2SolstheimWorld"))

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamrielx0000y0000.cgid")
        _escribir_cgid(config.overwrite_grass_dir, "DLC2SolstheimWorldx0000y0000.cgid")
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.verdict == "completed_verified"
    assert resultado.certificado is True
    assert resultado.success is True
    assert resultado.alcance_certificado == "corrida_completa"
    assert resultado.verdict_razones == ()
    assert resultado.evidencia["worldspaces_solicitados"] == ["Tamriel", "DLC2SolstheimWorld"]
    assert resultado.evidencia["worldspaces_con_artefactos"] == ["DLC2SolstheimWorld", "Tamriel"]
    assert resultado.evidencia["cobertura_usvfs"] == "no_verificada"
    assert resultado.evidencia["completitud_de_celdas"] == "no_verificada"


async def test_p1_worldspace_pedido_sin_artefactos_no_certifica(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    config = dataclasses.replace(config, worldspaces=("Tamriel", "DLC2SolstheimWorld"))

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamrielx0000y0000.cgid")
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.verdict == "completed_unverified"
    assert "worldspaces_sin_artefactos" in resultado.verdict_razones


async def test_p1_limpieza_incompleta_degrada_el_veredicto_en_run(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RD-11: el veredicto se cierra DESPUÉS de neutralizar; el provisional no se publica."""
    config = dataclasses.replace(config, mode="new")

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamrielx0000y0000.cgid")
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    def _neutralizar_falla(**_kwargs: Any) -> NeutralizacionFlag:
        return NeutralizacionFlag(borrado=False, archivado=False, errores=("disco bloqueado",))

    monkeypatch.setattr(gcr_mod, "neutralizar_flag", _neutralizar_falla)

    resultado = await _correr(_runner(config, mo2))

    assert resultado.verdict == "completed_unverified"
    assert resultado.certificado is False
    assert resultado.success is False
    assert "limpieza_incompleta" in resultado.verdict_razones
    assert resultado.flag_residual is True


async def test_p1_watchdog_corta_un_pid_vivo_sin_progreso_antes_del_deadline(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """F-05: el freeze con PID vivo se detecta por progreso, no por el deadline de 12 h."""
    config = dataclasses.replace(
        config,
        max_runtime_s=5.0,
        poll_interval_s=0.01,
        progress_poll_interval_s=0.05,
        stall_progress_s=0.2,
        stall_grace_s=0.0,
    )

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "stalled"
    assert resultado.stalled is True
    assert resultado.certificado is False
    assert resultado.elapsed_s < config.max_runtime_s, "el watchdog tiene que cortar antes del deadline"
    assert resultado.evidencia["progreso_detenido_s"] is not None
    assert mo2.launch_game.await_count == 1, "un freeze no se relanza"


async def test_p1_watchdog_no_corta_si_hay_progreso_legitimo(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    config = dataclasses.replace(
        config,
        max_runtime_s=0.6,
        poll_interval_s=0.01,
        progress_poll_interval_s=0.05,
        stall_progress_s=0.2,
        stall_grace_s=0.0,
    )
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100)
        return {"pid": 100, "status": "launched", "profile": profile}

    def _avanzar(pid: int) -> None:
        if pid != 1100:
            return
        n = next(llamada)
        if n < 8:
            _escribir_cgid(config.overwrite_grass_dir, f"celda_{n}.cgid")

    mo2.launch_game.side_effect = _launch
    mundo.al_chequear_pid = _avanzar

    resultado = await _correr(_runner(config, mo2))

    # El progreso observado NO se reporta como stall: el run llega al deadline o termina.
    assert resultado.outcome != "stalled" or resultado.evidencia["progreso_observado"] is True


async def test_p1_gracia_de_arranque_lento_no_corta_antes_de_tiempo(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    config = dataclasses.replace(
        config,
        max_runtime_s=0.4,
        poll_interval_s=0.01,
        progress_poll_interval_s=0.05,
        stall_progress_s=0.1,
        stall_grace_s=60.0,  # nunca se sale de la gracia en este test
    )

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "timeout"
    assert resultado.stalled is False


async def test_p1_terminalidad_no_verificable_declara_unknown(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R14: si no se puede demostrar que el proceso terminó, no se certifica nada."""
    config = dataclasses.replace(config, terminality_wait_s=0.05)

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamrielx0000y0000.cgid")
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        mundo.alta_juego(100, 1100)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_clasificar_terminalidad", lambda self, gp: "vivo")
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_kill_game_tree_sync", lambda *a, **k: None)

    resultado = await _correr(_runner(config, mo2))

    assert resultado.verdict == "unknown"
    assert resultado.certificado is False
    assert resultado.success is False
    assert "terminalidad_no_verificada" in resultado.verdict_razones
    assert resultado.evidencia["terminalidad"] == "vivo"


async def test_p1_terminalidad_espera_y_no_degrada_un_run_bueno(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RD-10: la muerte real del proceso tarda; la espera acotada evita un `unknown` por latencia."""
    config = dataclasses.replace(config, terminality_wait_s=1.0)
    estados = iter(["vivo", "vivo", "terminado"])

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamrielx0000y0000.cgid")
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_clasificar_terminalidad", lambda self, gp: next(estados))

    resultado = await _correr(_runner(config, mo2))

    assert resultado.verdict == "completed_verified"
    assert resultado.evidencia["terminalidad"] == "terminado"


# ---------------------------------------------------------------------------
# Revisión independiente (ronda consolidada): H1, H2/H5, H3, H4, H7
# ---------------------------------------------------------------------------


class _RelojFalso:
    """Reloj controlado para el watchdog: `time.monotonic()` lo maneja el test."""

    def __init__(self, ahora: float = 0.0) -> None:
        self.ahora = ahora

    def monotonic(self) -> float:
        return self.ahora

    def time(self) -> float:
        return 1_700_000_000.0 + self.ahora


def test_h1_inventario_cuenta_los_fail_no_evaluables(config: GrassCacheConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """H1 (unitario): el inventario de egreso NO convierte un `.fail` ilegible en «no hay fallos».

    Sin esta aserción la defensa de conteo de la política enmascara la supresión en el runner: el
    veredicto sale bien por el conteo, pero la evidencia miente sobre cuántos fallos se evaluaron.
    """
    grass = config.overwrite_grass_dir
    _escribir_cgid(grass, "Tamrielx0000y0000.cgid")
    (grass / "Tamrielx0001y0000.fail").write_bytes(b"1\n")
    original = pathlib.Path.read_bytes

    def _read_bytes_falla(self: pathlib.Path) -> bytes:
        if self.suffix.lower() == ".fail":
            raise PermissionError(13, "acceso denegado", str(self))
        return original(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _read_bytes_falla)
    runner = _runner(config, AsyncMock())

    inventario, fallos, _ws, _heredados, _nuevos, no_evaluables = runner._inventario_de_egreso_sync()

    assert inventario.fail_count == 1, "el `.fail` existe y se cuenta"
    assert fallos == {}, "no se inventa contenido que no se pudo leer"
    assert no_evaluables == 1, "el fallo de lectura se declara como NO EVALUABLE, no como ausencia"


async def test_h1_fail_ilegible_no_certifica(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: un `.fail` que existe pero no se puede leer NO puede desaparecer del inventario."""
    config = dataclasses.replace(config, mode="rebuild", rebuild_approval="REBUILD", ack_usvfs_no_verificado=True)
    config = dataclasses.replace(config, backup_root=config.overwrite_grass_dir.parent / "estado")
    grass = config.overwrite_grass_dir

    def _al_chequear(pid: int) -> None:
        if pid not in mundo.vivos or mundo.vidas.get(pid) == 0:
            (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(grass, "Tamrielx0000y0000.cgid")
        (grass / "Tamrielx0001y0000.fail").write_bytes(b"2\n")
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    original = pathlib.Path.read_bytes

    def _read_bytes_falla(self: pathlib.Path) -> bytes:
        if self.suffix.lower() == ".fail":
            raise PermissionError(13, "acceso denegado", str(self))
        return original(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _read_bytes_falla)
    mo2.launch_game.side_effect = _launch
    mundo.al_chequear_pid = _al_chequear

    resultado = await _correr(_runner(config, mo2))

    assert resultado.certificado is False, "un .fail ilegible no puede certificar el caché"
    assert resultado.verdict != "completed_verified"
    assert "fallos_de_celda_no_evaluables" in resultado.verdict_razones
    assert resultado.evidencia["fallos_de_celda_no_evaluables"] >= 1


async def test_h3_progreso_durante_la_gracia_se_registra(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """H3: la gracia suspende la DECISIÓN de stall, no el registro de progreso."""
    config = dataclasses.replace(
        config,
        max_runtime_s=0.6,
        poll_interval_s=0.01,
        progress_poll_interval_s=0.02,
        stall_progress_s=0.4,
        stall_grace_s=60.0,  # toda la corrida cae dentro de la gracia
    )
    llamada = itertools.count()

    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100)
        return {"pid": 100, "status": "launched", "profile": profile}

    def _avanzar(pid: int) -> None:
        if pid != 1100:
            return
        n = next(llamada)
        if n < 6:
            _escribir_cgid(config.overwrite_grass_dir, f"celda_{n}.cgid")

    mo2.launch_game.side_effect = _launch
    mundo.al_chequear_pid = _avanzar

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "timeout", "la gracia impide cortar por stall"
    assert resultado.evidencia["progreso_observado"] is True


async def test_h2_cancelacion_con_juego_no_verificable_es_unknown(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2/H5: cancelar y pedir el kill no prueba que el proceso murió."""
    config = dataclasses.replace(config, terminality_wait_s=0.05)
    cancel = asyncio.Event()

    async def _launch(profile: str) -> dict[str, Any]:
        mundo.alta_juego(100, 1100)
        cancel.set()
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_clasificar_terminalidad", lambda self, gp: "vivo")
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_kill_game_tree_sync", lambda *a, **k: None)

    resultado = await _correr(_runner(config, mo2), cancel)

    assert resultado.outcome == "cancelled"
    assert resultado.verdict == "unknown"
    assert resultado.certificado is False
    assert "terminalidad_no_verificada" in resultado.verdict_razones


async def test_h2_disco_lleno_con_juego_no_verificable_es_unknown(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2/H5: el corte por disco tambien tiene que verificar terminalidad."""
    config = dataclasses.replace(config, terminality_wait_s=0.05)
    llamadas = itertools.count()

    monkeypatch.setattr(
        "sky_claw.local.tools.grass_cache_runner.shutil.disk_usage",
        lambda _p: _USO_DISCO(total=100 * _GIB, used=100 * _GIB, free=(100 * _GIB if next(llamadas) == 0 else 0)),
    )
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_clasificar_terminalidad", lambda self, gp: "vivo")
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_kill_game_tree_sync", lambda *a, **k: None)

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "disk_full"
    assert resultado.verdict == "unknown"
    assert "terminalidad_no_verificada" in resultado.verdict_razones


async def test_h2_sin_intento_de_spawn_no_declara_terminado(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """H2/H5: «no lancé» no es «el proceso terminó»: son estados distintos y se distinguen."""
    cancel = asyncio.Event()
    cancel.set()

    resultado = await _correr(_runner(config, mo2), cancel)

    assert resultado.outcome == "cancelled"
    assert resultado.evidencia["lanzamiento"] == "no_intentado"
    assert resultado.evidencia["terminalidad"] == "sin_proceso"
    assert resultado.evidencia["terminalidad_verificada"] is True
    mo2.launch_game.assert_not_awaited()


async def test_h2_spawn_fallido_no_es_unknown(config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos) -> None:
    """H2/H5: si el lanzamiento no se efectivizó (nunca hubo proceso), es failed, no unknown."""
    config = dataclasses.replace(config, spawn_window_s=0.05)

    async def _launch(profile: str) -> dict[str, Any]:
        # MO2 "lanza" pero el juego no aparece en la ventana de spawn.
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "spawn_failed"
    assert resultado.verdict == "failed"
    # H2/H5: la ausencia se DEMOSTRÓ (búsqueda exhaustiva sin proceso nacido en la ventana del
    # intento), así que el estado es `sin_proceso`, no «terminado por no haberlo encontrado».
    assert resultado.evidencia["terminalidad"] == "sin_proceso"
    assert resultado.evidencia["lanzamiento"] == "rechazado"


# ---------------------------------------------------------------------------
# H7: watchdog determinista (reloj controlado)
# ---------------------------------------------------------------------------


async def test_h7_no_corta_antes_del_umbral_desde_el_ultimo_avance(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    reloj = _RelojFalso()
    config = dataclasses.replace(config, stall_progress_s=600.0, stall_grace_s=0.0, progress_poll_interval_s=1.0)
    muestras = iter([(1, 0, 0, 10), (1, 0, 0, 10), (1, 0, 0, 10)])
    monkeypatch.setattr(gcr_mod, "time", reloj)
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_muestra_de_progreso_sync", lambda self: next(muestras))
    runner = _runner(config, mo2)
    pid = gcr_mod._GamePid(pid=1100, create_time=None)

    # t=0 baseline; t=300 sin avance; t=599 sin avance → NO corta.
    for ahora in (0.0, 300.0, 599.0):
        reloj.ahora = ahora
        assert await runner._vigilar_progreso(pid, asyncio.Event(), 0, t_spawn_mono=0.0) is None


async def test_h7_avance_reinicia_el_presupuesto(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    reloj = _RelojFalso()
    config = dataclasses.replace(config, stall_progress_s=600.0, stall_grace_s=0.0, progress_poll_interval_s=1.0)
    muestras = iter([(1, 0, 0, 10), (1, 0, 0, 10), (2, 0, 0, 20), (2, 0, 0, 20)])
    monkeypatch.setattr(gcr_mod, "time", reloj)
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_muestra_de_progreso_sync", lambda self: next(muestras))
    runner = _runner(config, mo2)
    pid = gcr_mod._GamePid(pid=1100, create_time=None)

    cortes = []
    for ahora in (0.0, 500.0, 700.0, 1200.0):
        reloj.ahora = ahora
        cortes.append(await runner._vigilar_progreso(pid, asyncio.Event(), 0, t_spawn_mono=0.0))

    # t=500 sin avance; t=700 HAY avance → el presupuesto arranca de nuevo; t=1200 son 500 s → no corta.
    assert all(corte is None for corte in cortes)


async def test_h7_corta_despues_del_umbral_sin_avance(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    reloj = _RelojFalso()
    config = dataclasses.replace(config, stall_progress_s=600.0, stall_grace_s=0.0, progress_poll_interval_s=1.0)
    muestras = iter([(1, 0, 0, 10)] * 3)
    monkeypatch.setattr(gcr_mod, "time", reloj)
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_muestra_de_progreso_sync", lambda self: next(muestras))
    runner = _runner(config, mo2)
    pid = gcr_mod._GamePid(pid=1100, create_time=None)

    reloj.ahora = 0.0
    assert await runner._vigilar_progreso(pid, asyncio.Event(), 0, t_spawn_mono=0.0) is None
    reloj.ahora = 601.0
    corte = await runner._vigilar_progreso(pid, asyncio.Event(), 0, t_spawn_mono=0.0)

    assert corte is not None
    assert corte.outcome == "stalled"
    assert corte.evidencia["progreso_detenido_s"] >= 600.0


async def test_h4_baseline_ilegible_aborta_antes_del_spawn(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H4: si no se puede inventariar el estado inicial, no se lanza y no se certifica.

    P0 ya falla cerrado cuando `overwrite/Grass` es ilegible de entrada (`inventariar_cache` levanta
    `cache_ilegible`). El hueco era la captura de baseline del runner, que absorbía el error y lo
    convertía en «caché vacío»: todo lo heredado pasaba a contar como nuevo.
    """

    def _baseline_falla(self: Any) -> Any:
        raise PermissionError(13, "acceso denegado", str(self._config.overwrite_grass_dir))

    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_capturar_baseline_sync", _baseline_falla)

    resultado = await _correr(_runner(config, mo2))

    assert mo2.launch_game.await_count == 0, "sin baseline verificable no se lanza el juego"
    assert resultado.certificado is False
    assert resultado.success is False
    assert resultado.verdict == "unknown"
    assert "baseline_ilegible" in resultado.verdict_razones
    # Se aborta antes del punto sin retorno: el flag no se escribió.
    assert not (config.game_path / "PrecacheGrass.txt").exists()


def test_h4_capturar_baseline_no_silencia_un_error_de_listado(
    config: GrassCacheConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H4 (unitario): un error de listado NO puede degradar a baseline vacío."""

    def _iterdir_falla(self: pathlib.Path) -> Any:
        raise PermissionError(13, "acceso denegado", str(self))

    monkeypatch.setattr(pathlib.Path, "iterdir", _iterdir_falla)
    runner = _runner(config, AsyncMock())

    with pytest.raises(PermissionError):
        runner._capturar_baseline_sync()


async def test_h2_corte_por_progreso_perdido_con_juego_no_verificable_es_unknown(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2/H5: el corte de contrato también tiene que verificar terminalidad."""
    config = dataclasses.replace(config, terminality_wait_s=0.05)

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "celda_0.cgid")
        (config.game_path / "PrecacheGrass.txt").write_bytes(b"")  # flag sin progreso
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_clasificar_terminalidad", lambda self, gp: "vivo")
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_kill_game_tree_sync", lambda *a, **k: None)
    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "flag_sin_progreso"
    assert resultado.verdict == "unknown"
    assert "terminalidad_no_verificada" in resultado.verdict_razones


async def test_h2_no_relanza_si_el_intento_anterior_no_tiene_terminalidad(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2/H5: con estado previo vivo o indeterminado NO se autoriza otro lanzamiento."""
    config = dataclasses.replace(config, terminality_wait_s=0.05, max_restarts=2, relaunch_delay_s=0.01)

    async def _launch(profile: str) -> dict[str, Any]:
        # El juego nace, escribe su celda y muere: el ciclo pide un relanzamiento.
        _juego_escribe_cgid(config, "celda_0.cgid")
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch
    # El proceso queda vivo tras el crash: el PRIMER cierre ya no puede demostrar muerte.
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_clasificar_terminalidad", lambda self, gp: "vivo")
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_kill_game_tree_sync", lambda *a, **k: None)

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome in {"terminalidad_indeterminada", "flag_sin_progreso"}
    if resultado.outcome == "terminalidad_indeterminada":
        assert mo2.launch_game.await_count == 1, "no puede haber un segundo lanzamiento"
    assert resultado.verdict == "unknown"
    assert resultado.certificado is False


async def test_h2_terminalidad_no_verificada_no_archiva_el_progreso(
    config: GrassCacheConfig,
    mo2: AsyncMock,
    mundo: _MundoProcesos,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """H2/H5: un caché que puede seguir escribiéndose NO se archiva como evidencia estable."""
    estado = tmp_path / "estado_h2"
    config = dataclasses.replace(config, terminality_wait_s=0.05, backup_root=estado)
    archivos: list[str] = []

    def _archivar_registra(*args: Any, **kwargs: Any) -> None:
        archivos.append("archivado")

    import sky_claw.local.tools.grass_cache_safety as gcs_mod

    monkeypatch.setattr(gcs_mod, "archivar_progreso", _archivar_registra)
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_clasificar_terminalidad", lambda self, gp: "vivo")
    monkeypatch.setattr(gcr_mod.GrassCacheRunner, "_kill_game_tree_sync", lambda *a, **k: None)

    async def _launch(profile: str) -> dict[str, Any]:
        _escribir_cgid(config.overwrite_grass_dir, "Tamrielx0000y0000.cgid")
        with (config.game_path / "PrecacheGrass.txt").open("ab") as fh:
            fh.write(b"cell_Tamriel_0_0\n")
        mundo.alta_juego(100, 1100, vidas=1)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.verdict == "unknown"
    assert archivos == [], "no se archiva progreso de un caché que puede estar en curso"
    assert resultado.evidencia.get("archivado_omitido") is True
    # El flag SÍ se neutraliza: dejarlo mete al juego del usuario en modo precache.
    assert not (config.game_path / "PrecacheGrass.txt").exists()


async def test_hc_cache_parcial_de_dgid_y_fail_no_es_vacio(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """H-C: un caché parcial compuesto por `.dgid`/`.fail` no puede reportarse como vacío."""
    config = dataclasses.replace(config, max_runtime_s=0.5)

    async def _launch(profile: str) -> dict[str, Any]:
        (config.overwrite_grass_dir).mkdir(parents=True, exist_ok=True)
        (config.overwrite_grass_dir / "Tamrielx0000y0000.dgid").write_bytes(b"dgid")
        (config.overwrite_grass_dir / "Tamrielx0001y0000.fail").write_bytes(b"1\n")
        mundo.alta_juego(100, 1100)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    resultado = await _correr(_runner(config, mo2))

    assert resultado.outcome == "timeout"
    assert resultado.verdict == "partial", "hay artefactos conservados: no es un caché vacío"
    assert resultado.evidencia["inventario_final"]["archivos"] == 2
    assert resultado.evidencia["inventario_final"]["dgid_count"] == 1
    assert resultado.evidencia["inventario_final"]["fail_count"] == 1


async def test_b1_excepcion_usa_el_intento_actual_y_no_un_pid_anterior(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """B1: si el intento actual no atribuyó, un PID del intento anterior NO prueba su terminalidad."""
    config = dataclasses.replace(config, max_restarts=2, relaunch_delay_s=0.01, terminality_wait_s=0.05)
    llamadas = itertools.count()
    runner = _runner(config, mo2)

    async def _launch(profile: str) -> dict[str, Any]:
        n = next(llamadas)
        if n == 0:
            _juego_escribe_cgid(config, "celda_0.cgid")
            mundo.alta_juego(100, 1100, vidas=1)
            return {"pid": 100, "status": "launched", "profile": profile}
        raise RuntimeError("el lanzamiento explotó")

    mo2.launch_game.side_effect = _launch

    with pytest.raises(RuntimeError, match="explotó"):
        await _correr(runner)

    # El intento 1 pidió spawn y no atribuyó: el PID del intento 0 (muerto) no prueba nada.
    assert runner.terminalidad == "indeterminado"
    assert runner.lanzamiento == "solicitado_sin_atribucion"


async def test_b2_candidato_ilegible_no_es_ausencia(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: AccessDenied sobre un candidato relevante deja la evidencia INCOMPLETA."""

    class _ProcIlegible:
        pid = 4242

        def name(self) -> str:
            raise psutil_real.AccessDenied(4242)

        def create_time(self) -> float:
            return time.time()

        def exe(self) -> str | None:
            return None

    class _MundoIlegible(_MundoProcesos):
        def process_iter(self, attrs: Any = None) -> Any:
            return iter([_ProcIlegible()])

    ilegible = _MundoIlegible()
    monkeypatch.setattr(gcr_mod, "psutil", ilegible)
    runner = _runner(config, mo2)
    runner._spawn_solicitado[0] = True
    runner._epoch_del_intento[0] = 0.0

    estado, encontrado = runner._buscar_proceso_del_intento(0)

    assert (estado, encontrado) == ("incompleto", None)


async def test_b5_directorio_con_nombre_de_artefacto_no_es_cache(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """B5: un directorio con nombre `.cgid` no cuenta como artefacto."""
    grass = config.overwrite_grass_dir
    (grass / "Tamrielx0000y0000.cgid").mkdir(parents=True)
    runner = _runner(config, mo2)

    conteo = runner._conteo_rapido_sync()

    assert conteo.cgid_count == 0
    assert conteo.archivos == 0


async def test_b3_archivar_false_declara_perdida_sin_copia_predecible(
    config: GrassCacheConfig, tmp_path: pathlib.Path
) -> None:
    """B3+F4: sin archivado se declara la pérdida; no se escribe una copia de diagnóstico predecible."""
    from sky_claw.local.tools.grass_cache_safety import neutralizar_flag

    flag = config.game_path / "PrecacheGrass.txt"
    flag.write_bytes(b"cell_Tamriel_0_0\n")
    estado = tmp_path / "estado_b3"

    resultado = neutralizar_flag(
        flag_path=flag,
        grass_dir=config.overwrite_grass_dir,
        raiz_estado=estado,
        perfil="Default",
        archivar=False,
    )

    assert resultado.borrado is True
    assert resultado.progreso_perdido is True, "omitir el archivado ES perder la reanudación"
    assert not flag.exists()
    assert not (estado / "diagnostico").exists(), "F4: no se escribe una copia en destino predecible"
    assert any("NO se conserva como evidencia reanudable" in e for e in resultado.errores)


def _fence_de(tmp_path: pathlib.Path, config: GrassCacheConfig) -> pathlib.Path:
    from sky_claw.local.tools.grass_cache_fence import ruta_fence

    return ruta_fence(
        raiz_estado=tmp_path / "estado_fence",
        game_path=config.game_path,
        overwrite_grass_dir=config.overwrite_grass_dir,
    )


def test_f1_marcador_ilegible_bloquea(tmp_path: pathlib.Path, config: GrassCacheConfig) -> None:
    """F1: un marcador que no se puede interpretar es bloqueo, nunca ausencia."""
    from sky_claw.local.tools.grass_cache_fence import bloqueo_por_fence

    ruta = _fence_de(tmp_path, config)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(b"{no es json")

    bloqueo = bloqueo_por_fence(ruta)

    assert bloqueo is not None
    assert "ILEGIBLE" in bloqueo


def test_f1_marcador_indeterminado_bloquea_y_el_de_proceso_muerto_se_libera(
    tmp_path: pathlib.Path, config: GrassCacheConfig
) -> None:
    """F1: sin PID no se puede afirmar que murió (bloquea); con PID muerto se libera."""
    from sky_claw.local.tools.grass_cache_fence import FenceProceso, bloqueo_por_fence, escribir_fence

    ruta = _fence_de(tmp_path, config)
    escribir_fence(
        ruta,
        FenceProceso(
            run_id="r1",
            intento=0,
            lanzamiento="solicitado_sin_atribucion",
            pid=None,
            create_time=None,
            modo="new",
            game_path=str(config.game_path),
            overwrite_grass_dir=str(config.overwrite_grass_dir),
            creado="2026-11-25T00:00:00",
        ),
    )
    assert bloqueo_por_fence(ruta) is not None, "pid=None no demuestra ausencia"

    escribir_fence(
        ruta,
        FenceProceso(
            run_id="r2",
            intento=0,
            lanzamiento="atribuido",
            pid=999_999,
            create_time=1.0,
            modo="new",
            game_path=str(config.game_path),
            overwrite_grass_dir=str(config.overwrite_grass_dir),
            creado="2026-11-25T00:00:00",
        ),
    )

    assert bloqueo_por_fence(ruta) is None
    assert not ruta.exists(), "el marcador obsoleto se borra al demostrarse terminado"


async def test_f1_el_runner_escribe_el_fence_antes_del_spawn(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, tmp_path: pathlib.Path
) -> None:
    """F1: el marcador existe ANTES de pedir el spawn (sobrevive a un crash de Sky-Claw)."""
    estado = tmp_path / "estado_fence_run"
    config = dataclasses.replace(config, backup_root=estado)
    from sky_claw.local.tools.grass_cache_fence import ruta_fence

    ruta = ruta_fence(raiz_estado=estado, game_path=config.game_path, overwrite_grass_dir=config.overwrite_grass_dir)
    visto: list[bool] = []

    async def _launch(profile: str) -> dict[str, Any]:
        visto.append(ruta.exists())
        mundo.alta_juego(100, 1100, vidas=1)
        (config.game_path / "PrecacheGrass.txt").unlink(missing_ok=True)
        return {"pid": 100, "status": "launched", "profile": profile}

    mo2.launch_game.side_effect = _launch

    await _correr(_runner(config, mo2))

    assert visto == [True], "el fence se escribe antes del spawn"


def test_p1_candidato_legible_y_otro_ilegible_es_incompleto(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 (anexo): encontrar UN candidato legible no demuestra nada si otro es ilegible."""
    import sky_claw.local.tools.grass_cache_runner as gcr_mod

    class _ProcLegible:
        pid = 5001

        def name(self) -> str:
            return "SkyrimSE.exe"

        def create_time(self) -> float:
            return time.time()

        def exe(self) -> str:
            return str(config.game_path / "SkyrimSE.exe")

    class _ProcIlegible:
        pid = 5002

        def name(self) -> str:
            raise psutil_real.AccessDenied(5002)

        def create_time(self) -> float:
            return time.time()

        def exe(self) -> str | None:
            return None

    class _MundoMixto(_MundoProcesos):
        def process_iter(self, attrs: Any = None) -> Any:
            return iter([_ProcLegible(), _ProcIlegible()])

    monkeypatch.setattr(gcr_mod, "psutil", _MundoMixto())
    runner = _runner(config, mo2)
    runner._spawn_solicitado[0] = True
    runner._epoch_del_intento[0] = 0.0

    estado, encontrado = runner._buscar_proceso_del_intento(0)

    assert (estado, encontrado) == ("incompleto", None), "la incertidumbre no se resuelve con el legible"


def test_p1_multiples_legibles_es_encontrado(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 (anexo): varios candidatos legibles ⇒ se adopta uno (sin incertidumbre)."""
    import sky_claw.local.tools.grass_cache_runner as gcr_mod

    class _Proc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def name(self) -> str:
            return "SkyrimSE.exe"

        def create_time(self) -> float:
            return time.time()

        def exe(self) -> str:
            return str(config.game_path / "SkyrimSE.exe")

    class _MundoDos(_MundoProcesos):
        def process_iter(self, attrs: Any = None) -> Any:
            return iter([_Proc(6001), _Proc(6002)])

    monkeypatch.setattr(gcr_mod, "psutil", _MundoDos())
    runner = _runner(config, mo2)
    runner._spawn_solicitado[0] = True
    runner._epoch_del_intento[0] = 0.0

    estado, encontrado = runner._buscar_proceso_del_intento(0)

    assert estado == "encontrado"
    assert encontrado is not None and encontrado.pid == 6001


def test_b5_watchdog_ve_un_cache_parcial_solo_de_dgid_y_fail(
    config: GrassCacheConfig, mo2: AsyncMock, mundo: _MundoProcesos
) -> None:
    """B5 (anexo): el watchdog usa el cierre común: `.dgid`/`.fail` cuentan como artefactos."""
    grass = config.overwrite_grass_dir
    grass.mkdir(parents=True, exist_ok=True)
    (grass / "Tamrielx0000y0000.dgid").write_bytes(b"d")
    (grass / "Tamrielx0001y0000.fail").write_bytes(b"1\n")
    runner = _runner(config, mo2)

    cgid, dgid, fail, tamano = runner._muestra_de_progreso_sync()

    assert (cgid, dgid, fail) == (0, 1, 1)
    assert (dgid + fail) > 0, "un caché parcial de .dgid/.fail no puede parecer vacío al watchdog"
    assert tamano >= 0

"""GrassCacheRunner — crash-loop supervisor del precache de grass NGIO (PR-4).

Fase C del Stage 8 del SOP (No Grass In Objects). El precache lanza SkyrimSE.exe
(vía MO2, perfil dedicado del PR-3) con el flag ``PrecacheGrass.txt`` junto al
exe; NGIO escanea celdas escribiendo ``.cgid`` en ``overwrite/Grass/`` y el
juego **crashea repetidamente por diseño** (memory leak del Creation Engine).
El plugin MO2 de NGIO que auto-relanza corre dentro del GUI (no headless):
**Sky-Claw ES el "Restart on Crash"**.

Semántica de señales (ninguna única es autoritativa — D5):
- **Fin** = NGIO borró el flag (mecanismo documentado para gestores sin plugin).
  Postcheck fail-closed: flag borrado + cero ``.cgid`` → ``success=False``
  (fallo silencioso por zero-bounds).
- **Crash** = el proceso del juego murió Y el flag sigue presente → relanzar.

Disciplinas del repo que este runner respeta:
- **Agnóstico de HITL/bus** (gate único en ``HitlGateMiddleware``, PR #173;
  capa agente lock-only #217): stall/disco/timeout cortan y devuelven un
  resultado estructurado (``outcome``) para que la capa strategy (PR-5) decida.
  El cache parcial SIEMPRE se conserva (el precache reanuda donde quedó).
- **Progreso por callback inyectado** (patrón ``router.progress_callback``):
  jamás publica al CoreEventBus; un observador roto no mata un run de 12 h.
- **Anti-huérfanos**: todo camino de salida pasa por ``close_game()`` (árbol de
  MO2, M-8) MÁS el kill directo del PID del juego (D7b — si MO2 salió tras
  lanzar, el juego reparentado queda fuera del árbol de ``close_game``).

⚠️ Relojes: ``psutil.create_time`` es epoch wall-clock (``time.time()``); los
deadlines usan ``time.monotonic()``. Jamás mezclarlos (D1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import shutil  # import de módulo completo: los tests mockean shutil.disk_usage
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import psutil  # import de módulo completo: los tests reemplazan el objeto entero

from sky_claw.antigravity.security.path_validator import assert_safe_component
from sky_claw.local.mo2.vfs import GameLaunchTimeoutError, MO2Controller

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sky_claw.antigravity.security.path_validator import PathValidator

logger = logging.getLogger(__name__)

_GIB = 1024**3
#: Flag que NGIO consume junto a SkyrimSE.exe; su borrado señala el fin.
_PRECACHE_FLAG = "PrecacheGrass.txt"
#: Margen (s) restado al wall-clock del lanzamiento para el filtro por
#: create_time (granularidad del reloj en Windows).
_CREATE_TIME_EPSILON = 1.0

GrassCacheOutcome = Literal[
    "completed",
    "stalled",
    "disk_full",
    "timeout",
    "cancelled",
    "max_restarts",
    "spawn_failed",
]
GrassCachePhase = Literal["launching", "scanning", "crashed", "relaunching", "finished"]


@dataclass(frozen=True, slots=True)
class GrassCacheConfig:
    """Configuración del crash-loop. TODOS los tiempos viven acá (los tests
    pasan intervalos diminutos — sin monkeypatch de constantes de módulo).

    Attributes:
        game_path: Directorio que contiene ``SkyrimSE.exe`` (ahí va el flag).
        overwrite_grass_dir: ``<mo2>/overwrite/Grass`` — NGIO lo crea al
            escribir el primer ``.cgid``; puede no existir aún.
        profile: Perfil MO2 dedicado a lanzar (PR-3).
        max_runtime_s: Deadline global del ritual (default 12 h).
        max_restarts: Presupuesto de relanzamientos tras el intento inicial.
        stall_threshold: Crashes consecutivos sin ``.cgid`` nuevos → stall.
        relaunch_delay_s: Espera entre la muerte y el relanzamiento.
        spawn_window_s: Cuánto esperar a que SkyrimSE.exe aparezca tras MO2.
        poll_interval_s: Poll de la vigilancia (vida del juego, flag, disco).
        spawn_poll_interval_s: Poll más fino durante la ventana de spawn.
        heartbeat_interval_s: Cadencia del callback de progreso.
        min_free_bytes: Umbral de disco libre; por debajo se corta (disk_full).
        game_exe_names: Nombres de proceso del juego a localizar.
    """

    game_path: pathlib.Path
    overwrite_grass_dir: pathlib.Path
    profile: str = "SkyClaw-GrassCache"
    max_runtime_s: float = 43200.0
    max_restarts: int = 200
    stall_threshold: int = 5
    relaunch_delay_s: float = 5.0
    spawn_window_s: float = 120.0
    poll_interval_s: float = 5.0
    spawn_poll_interval_s: float = 1.0
    heartbeat_interval_s: float = 60.0
    min_free_bytes: int = _GIB
    game_exe_names: tuple[str, ...] = ("SkyrimSE.exe",)

    def __post_init__(self) -> None:
        if not self.game_path.is_dir():
            raise ValueError(f"game_path no es un directorio existente: {self.game_path}")
        if not self.overwrite_grass_dir.parent.is_dir():
            raise ValueError(
                f"El parent de overwrite_grass_dir no existe: {self.overwrite_grass_dir.parent} "
                "(el probe de disco necesita un ancestro real)."
            )
        assert_safe_component(self.profile, field="profile")
        if self.max_restarts < 1:
            raise ValueError(f"max_restarts debe ser >= 1, no {self.max_restarts}")
        if self.stall_threshold < 1:
            raise ValueError(f"stall_threshold debe ser >= 1, no {self.stall_threshold}")
        for campo in ("max_runtime_s", "spawn_window_s", "poll_interval_s", "spawn_poll_interval_s"):
            if getattr(self, campo) <= 0:
                raise ValueError(f"{campo} debe ser > 0")
        if not self.game_exe_names:
            raise ValueError("game_exe_names no puede estar vacío")
        if not any((self.game_path / nombre).is_file() for nombre in self.game_exe_names):
            # Sin este guard, un game_path equivocado (p.ej. la raíz de MO2)
            # recibe el flag donde NGIO jamás lo ve: el juego corre en modo
            # normal hasta agotar el deadline de 12 h.
            raise ValueError(
                f"game_path no contiene el ejecutable del juego ({', '.join(self.game_exe_names)}): {self.game_path}"
            )


@dataclass(frozen=True, slots=True)
class GrassCacheProgress:
    """Foto de progreso para el callback (GUI/Telegram vía capa servicio)."""

    phase: GrassCachePhase
    crash_count: int
    cgid_count: int
    cache_size_mb: float
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class GrassCacheRunResult:
    """Resultado estructurado del ritual (el dict success/message es de PR-5).

    ``success=True`` SOLO con ``outcome="completed"`` y ``cgid_count > 0``
    (postcheck fail-closed — D5). ``cancelled``/``stalled`` son derivables de
    ``outcome`` pero explícitos (contrato del plan maestro).
    """

    success: bool
    message: str
    outcome: GrassCacheOutcome
    crash_count: int
    cgid_count: int
    cache_size_mb: float
    elapsed_s: float
    cancelled: bool = False
    stalled: bool = False


class GrassCacheRunner:
    """Supervisor del crash-loop: lanza, vigila, relanza y corta con criterio.

    Args:
        config: Presupuestos y paths del ritual.
        mo2: Controller inyectado (``launch_game``/``close_game`` — D2).
        path_validator: Sandbox opcional; valida el path del flag en el ctor
            (patrón ``XEditRunner``: fallar temprano, antes de tocar nada).
        on_progress: Callback async opcional (D3). Sus excepciones ``Exception``
            se suprimen con warning; ``CancelledError`` propaga.

    Raises:
        PathViolationError: Si el path del flag queda fuera del sandbox del
            *path_validator* (fail-early: antes de escribir nada).
    """

    def __init__(
        self,
        config: GrassCacheConfig,
        mo2: MO2Controller,
        *,
        path_validator: PathValidator | None = None,
        on_progress: Callable[[GrassCacheProgress], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._mo2 = mo2
        self._on_progress = on_progress
        flag = config.game_path / _PRECACHE_FLAG
        self._flag_path = path_validator.validate(flag, strict_symlink=False) if path_validator else flag
        self._start_mono = 0.0

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def run(self, cancel_event: asyncio.Event | None = None) -> GrassCacheRunResult:
        """Ejecuta el ritual completo; siempre devuelve un resultado estructurado.

        Raises:
            FileNotFoundError: ``ModOrganizer.exe`` ausente (misconfiguración —
                propaga desde ``launch_game``; la capa servicio la reporta).
        """
        cancel = cancel_event if cancel_event is not None else asyncio.Event()
        self._start_mono = time.monotonic()
        crash_count = 0
        stall_streak = 0
        game_ever_seen = False
        game_pid: int | None = None

        # -- Pre-vuelo: sin side effects hasta pasar estos guards --
        if cancel.is_set():
            return self._resultado("cancelled", "Cancelado antes de iniciar.", crash_count, (0, 0))
        if await self._free_disk_bytes() < self._config.min_free_bytes:
            return self._resultado(
                "disk_full",
                "Espacio en disco insuficiente para arrancar el precache.",
                crash_count,
                await self._snapshot_cache(),
            )

        try:
            # El try cubre DESDE la escritura del flag: una cancelación dura
            # durante el snapshot inicial o el primer progress (que propaga
            # CancelledError) también debe limpiar el flag en el finally.
            await asyncio.to_thread(self._flag_path.write_text, "", encoding="utf-8")
            # Baseline del stall detector: el estado del cache ANTES del primer
            # lanzamiento — un primer ciclo sin .cgid nuevos ya cuenta como racha.
            last_snapshot = await self._snapshot_cache()
            await self._emit_progress("launching", crash_count)

            for _ in range(self._config.max_restarts + 1):
                if time.monotonic() - self._start_mono >= self._config.max_runtime_s:
                    # Deadline global también entre relanzamientos: acota el
                    # overshoot a un relaunch_delay (auditoría #287).
                    await self._matar_todo(game_pid)
                    return self._resultado(
                        "timeout",
                        f"Deadline global de {self._config.max_runtime_s:.0f}s alcanzado; "
                        "el cache parcial se conserva.",
                        crash_count,
                        await self._snapshot_cache(),
                    )
                game_pid = None
                try:
                    info: dict[str, Any] = await self._mo2.launch_game(self._config.profile)
                    mo2_pid: int | None = int(info["pid"])
                except FileNotFoundError:
                    raise  # ModOrganizer.exe ausente: misconfiguración (contrato de run())
                except (GameLaunchTimeoutError, OSError):
                    # PermissionError/OSError del spawn = mismo trato que un
                    # no-spawn: outcome estructurado, no excepción cruda
                    # (auditoría #287).
                    logger.warning("El lanzamiento de MO2 falló; se trata como no-spawn.", exc_info=True)
                    mo2_pid = None
                t_spawn_mono = time.monotonic()
                min_create_epoch = time.time() - _CREATE_TIME_EPSILON  # wall-clock para psutil

                # ---- Ventana de spawn: localizar SkyrimSE.exe (D1) ----
                exito_temprano = False
                if mo2_pid is not None:
                    while time.monotonic() - t_spawn_mono < self._config.spawn_window_s:
                        if cancel.is_set():
                            await self._matar_todo(await self._ultima_atribucion(game_pid, mo2_pid, min_create_epoch))
                            return self._resultado(
                                "cancelled", "Cancelado durante el arranque.", crash_count, await self._snapshot_cache()
                            )
                        if time.monotonic() - self._start_mono >= self._config.max_runtime_s:
                            # El deadline es GLOBAL: sin este check, ciclos de
                            # no-spawn repetidos lo excederían por hasta
                            # stall_threshold * spawn_window_s.
                            await self._matar_todo(await self._ultima_atribucion(game_pid, mo2_pid, min_create_epoch))
                            return self._resultado(
                                "timeout",
                                f"Deadline global de {self._config.max_runtime_s:.0f}s alcanzado "
                                "durante la ventana de spawn; el cache parcial se conserva.",
                                crash_count,
                                await self._snapshot_cache(),
                            )
                        if not await self._flag_exists():
                            # NGIO terminó durante el boot (reanudación casi
                            # completa): éxito, no spawn-fail.
                            exito_temprano = True
                            break
                        pid = await self._find_game_pid(mo2_pid, min_create_epoch)
                        if pid is not None:
                            game_pid = pid
                            game_ever_seen = True
                            break
                        # La muerte de MO2 acá NO es fallo: el juego pudo
                        # reparentarse; el fallback por nombre lo sigue buscando.
                        if await self._sleep_or_cancel(cancel, self._config.spawn_poll_interval_s):
                            await self._matar_todo(await self._ultima_atribucion(game_pid, mo2_pid, min_create_epoch))
                            return self._resultado(
                                "cancelled", "Cancelado durante el arranque.", crash_count, await self._snapshot_cache()
                            )
                if exito_temprano:
                    # Última atribución (auditoría #287, D7b): el flag pudo
                    # borrarse ANTES de localizar al juego — sin esta búsqueda
                    # final, un juego reparentado quedaría vivo en modo normal.
                    return await self._salida_exito(
                        crash_count, await self._ultima_atribucion(game_pid, mo2_pid, min_create_epoch)
                    )

                if game_pid is None:
                    # Ventana agotada sin juego localizado.
                    await self._matar_todo(None)
                    if not game_ever_seen:
                        # Ciclo 0: entorno roto (Steam sin login, SKSE mal
                        # instalado) — reintentar quema la ventana en vano (D7).
                        return self._resultado(
                            "spawn_failed",
                            "SkyrimSE.exe no apareció en la ventana de spawn: revisar SKSE/Steam/NGIO.",
                            crash_count,
                            await self._snapshot_cache(),
                        )
                    # Fallo transitorio tras ciclos exitosos: cuenta como crash;
                    # el stall detector acota la repetición.
                    crash_count += 1
                else:
                    # ---- Vigilancia mientras el juego vive (D7) ----
                    salida = await self._vigilar(game_pid, cancel, crash_count)
                    if salida is not None:
                        return salida
                    crash_count += 1

                # ---- Post-crash ----
                snap = await self._snapshot_cache()
                if snap == last_snapshot:
                    stall_streak += 1
                else:
                    stall_streak = 0
                last_snapshot = snap
                await self._emit_progress("crashed", crash_count, snap)
                if not await self._flag_exists():
                    # NGIO borró el flag justo antes de morir: completó. Este
                    # check va ANTES que el de stall (auditoría #287): un
                    # resume casi terminado muere sin escribir nada y NO debe
                    # reportarse como stalled. Un relanzamiento acá abriría el
                    # juego en modo normal (falso "vivo eterno" hasta timeout).
                    return await self._salida_exito(crash_count, game_pid)
                if stall_streak >= self._config.stall_threshold:
                    await self._matar_todo(game_pid)
                    return self._resultado(
                        "stalled",
                        f"{stall_streak} relanzamientos consecutivos sin ningún .cgid nuevo: "
                        "el juego muere siempre en la misma celda.",
                        crash_count,
                        snap,
                    )
                # Árbol MO2 residual fuera ANTES de pisar el slot _launched_pid.
                await self._mo2.close_game()
                if await self._sleep_or_cancel(cancel, self._config.relaunch_delay_s):
                    await self._matar_todo(game_pid)
                    return self._resultado("cancelled", "Cancelado entre relanzamientos.", crash_count, snap)
                await self._emit_progress("relaunching", crash_count)

            await self._matar_todo(game_pid)
            return self._resultado(
                "max_restarts",
                f"Presupuesto de {self._config.max_restarts} relanzamientos agotado sin completar.",
                crash_count,
                await self._snapshot_cache(),
            )
        except BaseException:
            # Cancelación dura o bug: jamás dejar huérfanos. Best-effort para
            # no enmascarar la excepción original.
            with contextlib.suppress(Exception):
                await self._matar_todo(game_pid)
            raise
        finally:
            # Síncrono deliberado: un await acá, durante una CancelledError,
            # puede re-cancelarse ANTES del unlink y dejar el flag residual
            # (que pondría el juego del usuario en modo precache al abrirlo).
            self._remove_flag_sync()

    # ------------------------------------------------------------------
    # Fases internas
    # ------------------------------------------------------------------

    async def _vigilar(self, game_pid: int, cancel: asyncio.Event, crash_count: int) -> GrassCacheRunResult | None:
        """Poll de la vida del juego. ``None`` = crash (el caller relanza)."""
        last_heartbeat = time.monotonic()
        while True:
            if cancel.is_set():
                await self._matar_todo(game_pid)
                return self._resultado(
                    "cancelled",
                    "Cancelado; el cache parcial se conserva.",
                    crash_count,
                    await self._snapshot_cache(),
                )
            if not await self._flag_exists():
                return await self._salida_exito(crash_count, game_pid)
            if time.monotonic() - self._start_mono >= self._config.max_runtime_s:
                await self._matar_todo(game_pid)
                return self._resultado(
                    "timeout",
                    f"Deadline global de {self._config.max_runtime_s:.0f}s alcanzado; el cache parcial se conserva.",
                    crash_count,
                    await self._snapshot_cache(),
                )
            if await self._free_disk_bytes() < self._config.min_free_bytes:
                await self._matar_todo(game_pid)
                return self._resultado(
                    "disk_full",
                    "Espacio en disco por debajo del umbral; liberar espacio y reanudar.",
                    crash_count,
                    await self._snapshot_cache(),
                )
            if not await self._pid_alive(game_pid):
                return None  # crash: murió con el flag presente
            if time.monotonic() - last_heartbeat >= self._config.heartbeat_interval_s:
                await self._emit_progress("scanning", crash_count)
                last_heartbeat = time.monotonic()
            # La cancelación que llegue durante el sleep la atiende el check ①
            # del próximo ciclo (el sleep despierta al instante con el event).
            await self._sleep_or_cancel(cancel, self._config.poll_interval_s)

    async def _salida_exito(self, crash_count: int, game_pid: int | None) -> GrassCacheRunResult:
        """Fin multi-criterio: flag ausente + postcheck fail-closed de .cgid.

        El éxito exige count > 0 **y** bytes > 0: el fallo silencioso de
        zero-bounds documentado en el SOP también se manifiesta como archivos
        ``.cgid`` de cero bytes — un cache "presente pero vacío" no es éxito.
        """
        await self._matar_todo(game_pid)
        snap = await self._snapshot_cache()
        await self._emit_progress("finished", crash_count)
        if snap[0] > 0 and snap[1] > 0:
            return self._resultado(
                "completed",
                "",
                crash_count,
                snap,
                success=True,
            )
        return self._resultado(
            "completed",
            "El precache terminó pero el cache quedó vacío (sin .cgid o todos de 0 bytes) — "
            "revisar records GRAS con bounds nulos (fallo silencioso de NGIO).",
            crash_count,
            snap,
        )

    async def _ultima_atribucion(
        self, game_pid: int | None, mo2_pid: int | None, min_create_epoch: float
    ) -> int | None:
        """Búsqueda final del juego antes de una salida desde la spawn window.

        El flag puede borrarse (o llegar una cancelación/timeout) ANTES de que
        el juego fuera atribuido: sin este intento final, un juego reparentado
        quedaría vivo tras la salida (D7b — auditoría #287).
        """
        if game_pid is not None or mo2_pid is None:
            return game_pid
        return await self._find_game_pid(mo2_pid, min_create_epoch)

    async def _matar_todo(self, game_pid: int | None) -> None:
        """``close_game()`` (árbol MO2, M-8) + kill directo del juego (D7b).

        Si MO2 salió tras lanzar, su PID está muerto y ``close_game`` no
        alcanza al juego reparentado: el kill directo cierra ese hueco.
        """
        await self._mo2.close_game()
        if game_pid is not None and await self._pid_alive(game_pid):
            await asyncio.to_thread(self._kill_game_tree_sync, game_pid)

    # ------------------------------------------------------------------
    # Helpers (I/O y psutil sync detrás de to_thread)
    # ------------------------------------------------------------------

    async def _sleep_or_cancel(self, cancel: asyncio.Event, seconds: float) -> bool:
        """Espera *seconds* o hasta la cancelación (reacción instantánea)."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(cancel.wait(), timeout=seconds)
        return cancel.is_set()

    async def _flag_exists(self) -> bool:
        return await asyncio.to_thread(self._flag_path.exists)

    async def _snapshot_cache(self) -> tuple[int, int]:
        return await asyncio.to_thread(self._scan_cgid_sync)

    def _scan_cgid_sync(self) -> tuple[int, int]:
        """(count, total_bytes) de los ``.cgid``; (0, 0) si Grass/ no existe aún."""
        count = 0
        total = 0
        try:
            for archivo in self._config.overwrite_grass_dir.glob("*.cgid"):
                with contextlib.suppress(OSError):
                    total += archivo.stat().st_size
                    count += 1
        except (FileNotFoundError, NotADirectoryError):
            return (0, 0)
        return (count, total)

    async def _free_disk_bytes(self) -> int:
        probe = (
            self._config.overwrite_grass_dir
            if self._config.overwrite_grass_dir.is_dir()
            else self._config.overwrite_grass_dir.parent
        )
        try:
            uso = await asyncio.to_thread(shutil.disk_usage, probe)
        except OSError:
            # TOCTOU (probe borrado entre is_dir y disk_usage) o share de red
            # parpadeando: un error de MEDICIÓN no debe abortar un run de 12h
            # ni confundirse con disco lleno (auditoría #287). Se devuelve el
            # umbral exacto (no corta) y se deja rastro.
            logger.warning("No se pudo medir el disco libre en %s; el ritual continúa.", probe, exc_info=True)
            return self._config.min_free_bytes
        return int(uso.free)

    async def _find_game_pid(self, mo2_pid: int, min_create_epoch: float) -> int | None:
        return await asyncio.to_thread(self._scan_for_game_sync, mo2_pid, min_create_epoch)

    def _scan_for_game_sync(self, mo2_pid: int, min_create_epoch: float) -> int | None:
        """UNA pasada de búsqueda del juego (el caller polea).

        Rama B (D1): hijo del árbol de MO2. Rama C (fallback): búsqueda global
        por nombre acotada por ``create_time >= min_create_epoch`` **y** por el
        exe residente en ``game_path`` — no adopta un Skyrim preexistente del
        usuario, ni uno de otra instalación lanzado en la ventana, ni pierde al
        juego reparentado. La atribución perfecta post-reparent es imposible
        (el vínculo padre-hijo se perdió); si el exe no es legible
        (AccessDenied), se acepta por nombre+tiempo con warning.
        """
        nombres = {n.lower() for n in self._config.game_exe_names}
        game_dir = self._config.game_path.resolve()
        try:
            hijos = psutil.Process(mo2_pid).children(recursive=True)
        except psutil.Error:
            hijos = []
        for hijo in hijos:
            try:
                if hijo.name().lower() in nombres:
                    return int(hijo.pid)
            except psutil.Error:
                continue
        try:
            for proc in psutil.process_iter(["name", "create_time", "exe"]):
                try:
                    info = proc.info
                    nombre = str(info.get("name") or "")
                    creado = float(info.get("create_time") or 0.0)
                    exe = info.get("exe")
                except psutil.Error:
                    continue
                if nombre.lower() not in nombres or creado < min_create_epoch:
                    continue
                if not exe:
                    # exe ilegible (AccessDenied: proceso elevado/de otro
                    # usuario): fail-closed — adoptar a ciegas podría terminar
                    # matando la sesión de OTRO Skyrim (auditoría #287).
                    logger.warning(
                        "Se ignora %s (pid=%s): exe ilegible, no se puede verificar la instalación.",
                        nombre,
                        proc.pid,
                    )
                    continue
                try:
                    if pathlib.Path(str(exe)).resolve().parent != game_dir:
                        continue  # mismo nombre pero OTRA instalación: no se adopta
                except OSError:
                    continue
                return int(proc.pid)
        except psutil.Error:
            logger.warning("process_iter falló durante la búsqueda del juego.", exc_info=True)
        return None

    async def _pid_alive(self, pid: int) -> bool:
        return bool(await asyncio.to_thread(psutil.pid_exists, pid))

    def _es_proceso_del_juego(self, proc: psutil.Process) -> bool:
        """True si *proc* es un exe del juego residente en ``game_path``.

        Misma verificación que ``_scan_for_game_sync`` usa al ATRIBUIR el juego
        (nombre en ``game_exe_names`` + exe bajo ``game_path``), reutilizada al
        MATAR: entre la atribución y el kill el SO pudo reusar el PID para otro
        proceso, y matar su árbol a ciegas cerraría un Steam/Discord/otro Skyrim
        ajeno (auditoría #287 §2.5). Fail-closed: nombre que no matchea o exe
        ilegible (AccessDenied) ⇒ NO es nuestro juego, no se mata.
        """
        nombres = {n.lower() for n in self._config.game_exe_names}
        try:
            if proc.name().lower() not in nombres:
                return False
            exe = proc.exe()
        except psutil.Error:
            return False
        if not exe:
            return False
        try:
            return pathlib.Path(exe).resolve().parent == self._config.game_path.resolve()
        except OSError:
            return False

    def _kill_game_tree_sync(self, pid: int) -> None:
        """Kill directo del árbol del juego (réplica acotada de M-8 sobre el
        game_pid — D7b). Best-effort.

        Revalida la identidad del PID raíz ANTES de matar (§2.5): un PID reusado
        por el SO tras morir el juego original tendría otro nombre/exe → no se
        mata un proceso ajeno del usuario. Los hijos se matan solo si la raíz
        verificó (son descendientes del juego real)."""
        try:
            root = psutil.Process(pid)
        except psutil.Error:
            return
        if not self._es_proceso_del_juego(root):
            logger.warning(
                "No se mata el pid=%s: ya no es un exe del juego bajo %s (PID reusado por el SO).",
                pid,
                self._config.game_path,
            )
            return
        try:
            procs = list(root.children(recursive=True))
        except psutil.Error:
            procs = []
        procs.append(root)
        for proc in procs:
            with contextlib.suppress(psutil.Error):
                proc.kill()

    async def _emit_progress(
        self, phase: GrassCachePhase, crash_count: int, snap: tuple[int, int] | None = None
    ) -> None:
        """Foto de progreso al callback; un observador roto no mata el ritual.

        *snap* permite reutilizar un snapshot recién computado por el caller —
        hacia el final del precache ``Grass/`` tiene decenas de miles de
        ``.cgid`` y un glob+stat duplicado por crash no es gratis.
        """
        if self._on_progress is None:
            return
        if snap is None:
            snap = await self._snapshot_cache()
        progreso = GrassCacheProgress(
            phase=phase,
            crash_count=crash_count,
            cgid_count=snap[0],
            cache_size_mb=snap[1] / (1024 * 1024),
            elapsed_s=time.monotonic() - self._start_mono,
        )
        try:
            await self._on_progress(progreso)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — cualquier fallo del observador se aísla
            logger.warning("El callback on_progress falló; el ritual continúa.", exc_info=True)

    def _remove_flag_sync(self) -> None:
        """Borra el flag (missing_ok: en éxito NGIO ya lo borró)."""
        with contextlib.suppress(OSError):
            self._flag_path.unlink(missing_ok=True)

    def _resultado(
        self,
        outcome: GrassCacheOutcome,
        message: str,
        crash_count: int,
        snap: tuple[int, int],
        *,
        success: bool = False,
    ) -> GrassCacheRunResult:
        return GrassCacheRunResult(
            success=success,
            message=message,
            outcome=outcome,
            crash_count=crash_count,
            cgid_count=snap[0],
            cache_size_mb=snap[1] / (1024 * 1024),
            elapsed_s=time.monotonic() - self._start_mono,
            cancelled=outcome == "cancelled",
            stalled=outcome == "stalled",
        )


__all__ = [
    "GrassCacheConfig",
    "GrassCacheOutcome",
    "GrassCachePhase",
    "GrassCacheProgress",
    "GrassCacheRunResult",
    "GrassCacheRunner",
]

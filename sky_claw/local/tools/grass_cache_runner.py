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
- **El caché previo no se destruye sin decisión** (P0 SAFE CACHE, F-01): NGIO
  borra ``.cgid``/``.dgid``/``.fail`` cuando el flag no tiene progreso, así que
  escribir el flag es el punto sin retorno. Lo decide
  ``grass_cache_safety.preparar_arranque`` —``new`` exige caché vacío,
  ``resume`` nunca trunca, ``rebuild`` exige aprobación + respaldo verificado—
  y al salir el flag se archiva antes de neutralizarlo.
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
import dataclasses
import logging
import pathlib
import shutil  # import de módulo completo: los tests mockean shutil.disk_usage
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import psutil  # import de módulo completo: los tests reemplazan el objeto entero

from sky_claw.app.security.path_validator import assert_safe_component
from sky_claw.local.mo2.vfs import GameLaunchTimeoutError, MO2Controller
from sky_claw.local.tools.grass_cache_safety import (
    MODOS_GRASS_CACHE,
    PRECACHE_FLAG,
    EstadoAplicacion,
    GrassCacheMode,
    NeutralizacionFlag,
    PlanArranque,
    aplicar_plan,
    inventariar_cache,
    neutralizar_flag,
    preparar_arranque,
    ruta_de_respaldo,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sky_claw.app.security.path_validator import PathValidator

logger = logging.getLogger(__name__)

T = TypeVar("T")

_GIB = 1024**3
#: Flag que NGIO consume junto a SkyrimSE.exe; su borrado señala el fin.
#: La constante vive en ``grass_cache_safety`` (inventario, plan y runner tienen
#: que hablar del MISMO archivo); se re-exporta porque el servicio la importa
#: de acá desde PR-5.
_PRECACHE_FLAG = PRECACHE_FLAG
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
    "flag_sin_progreso",
]
GrassCachePhase = Literal["launching", "scanning", "crashed", "relaunching", "finished"]


@dataclass(frozen=True, slots=True)
class _GamePid:
    """PID del juego + su ``create_time`` en el momento de la ATRIBUCIÓN.

    Se captura una sola vez, cuando ``_scan_for_game_sync`` identifica el
    proceso (no en el kill, más tarde): revalidar contra el ``create_time``
    capturado AHÍ es lo que demuestra que, al matar, seguimos hablando del
    MISMO proceso que atribuimos — no uno que el SO reusó después para otro
    ``SkyrimSE.exe`` de la misma instalación (p. ej. el usuario relanzó el
    juego manualmente entre la atribución y el kill — review Codex #316,
    mismo patrón que ``vfs.py`` #302). ``create_time=None`` si no se pudo leer
    al atribuir (AccessDenied puntual): el kill omite el chequeo de tiempo.
    """

    pid: int
    create_time: float | None


@dataclass(frozen=True, slots=True)
class GrassCacheConfig:
    """Configuración del crash-loop. TODOS los tiempos viven acá (los tests
    pasan intervalos diminutos — sin monkeypatch de constantes de módulo).

    Attributes:
        game_path: Directorio que contiene ``SkyrimSE.exe`` (ahí va el flag).
        overwrite_grass_dir: ``<mo2>/overwrite/Grass`` — NGIO lo crea al
            escribir el primer ``.cgid``; puede no existir aún.
        profile: Perfil MO2 dedicado a LANZAR (PR-3, el clon ``SkyClaw-GrassCache``).
            NO es la identidad del progreso: el clon es constante entre perfiles.
        progress_profile: Perfil MO2 de ORIGEN — la identidad con la que se
            archiva y se restaura la evidencia de progreso. Dos perfiles de
            origen distintos comparten ``overwrite/Grass`` y el mismo clon, así
            que atar el progreso al nombre del clon dejaría que el ``resume`` de
            uno reanudara con el progreso del otro (otro conjunto de mods). El
            servicio pasa acá el perfil activo del usuario. Vacío = se usa
            ``profile`` (compatibilidad con construcciones directas).
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
        mode: Contrato de arranque (P0 SAFE CACHE) — ``new`` exige caché
            vacío y sin progreso; ``resume`` exige progreso demostrable y no
            trunca el flag; ``rebuild`` exige aprobación y respaldo verificado.
            NGIO borra ``*.cgid``/``*.dgid``/``*.fail`` cuando el flag no tiene
            progreso, así que el default es el modo que NO destruye.
        rebuild_approval: Token literal de aprobación (``REBUILD``) para el
            modo destructivo. Se exige explícito en el payload HITL.
        backup_root: Raíz durable donde viven el respaldo verificado y la
            evidencia de progreso archivada (``<state>/grass_cache``). Sin ella
            no hay ``rebuild`` con caché preexistente ni ``resume`` archivado.
        ack_usvfs_no_verificado: Reconocimiento explícito de que la cobertura
            USVFS **no** está demostrada: el respaldo cubre ``overwrite/Grass``,
            no el ``Grass/`` de los mods habilitados que NGIO ve vía la VFS.
            Sin este reconocimiento, ``rebuild`` queda en HOLD.
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
    mode: GrassCacheMode = "new"
    rebuild_approval: str | None = None
    backup_root: pathlib.Path | None = None
    ack_usvfs_no_verificado: bool = False
    progress_profile: str = ""

    @property
    def perfil_de_progreso(self) -> str:
        """Identidad del progreso archivado: perfil de ORIGEN, no el del clon.

        Único punto donde se decide: las DOS superficies que archivan/restauran
        evidencia —``preparar_arranque`` y ``neutralizar_flag``— leen de acá, así
        que no pueden divergir. El fallback a ``profile`` mantiene el
        comportamiento de una construcción directa (tests, uso embebido); en
        producción el servicio siempre pasa ``progress_profile``.
        """
        return self.progress_profile or self.profile

    def __post_init__(self) -> None:
        if not self.game_path.is_dir():
            raise ValueError(f"game_path no es un directorio existente: {self.game_path}")
        if not self.overwrite_grass_dir.parent.is_dir():
            raise ValueError(
                f"El parent de overwrite_grass_dir no existe: {self.overwrite_grass_dir.parent} "
                "(el probe de disco necesita un ancestro real)."
            )
        assert_safe_component(self.profile, field="profile")
        if self.mode not in MODOS_GRASS_CACHE:
            raise ValueError(f"mode debe ser uno de {MODOS_GRASS_CACHE}, no {self.mode!r}")
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
    #: Modo con el que arrancó el ritual (contrato P0 SAFE CACHE).
    mode: GrassCacheMode = "new"
    #: Respaldo verificado del intento (cadena vacía si no hubo rebuild con caché).
    backup_dir: str = ""
    #: True si el flag ``PrecacheGrass.txt`` NO se pudo eliminar al salir.
    flag_residual: bool = False
    #: True si el flag se borró pero su progreso NO se pudo archivar: la
    #: evidencia de reanudación de este caché se perdió.
    progreso_perdido: bool = False
    #: Errores concretos de la limpieza (archivado/borrado del flag).
    limpieza_errores: tuple[str, ...] = ()


async def esperar_terminar(trabajo: Awaitable[T], *, descripcion: str) -> T:
    """Espera un trabajo en HILO sin poder abandonarlo por cancelación.

    ``asyncio.to_thread`` no es cancelable: al cancelar la tarea, el ``await``
    levanta ``CancelledError`` y **el hilo sigue escribiendo**. Cualquier
    ``await`` normal devuelve el control con el trabajo en vuelo, y quien espera
    suelta lo que protegía —locks, la limpieza del flag— sobre un estado que
    todavía cambia. Los dos casos que lo necesitan: la escritura del flag del
    runner (B11) y la preparación del respaldo del servicio (con los dos locks
    del ritual tomados).

    **Cómo lo garantiza.** ``asyncio.shield`` sobre la tarea del hilo: al recibir
    ``CancelledError`` no se propaga mientras el trabajo siga vivo; se vuelve a
    esperar hasta que TERMINA y recién entonces se levanta la cancelación (o el
    error del trabajo, si falló). El llamador nunca ve un ``return`` con trabajo
    pendiente, así que puede decidir con el estado consolidado.

    Args:
        trabajo: awaitable que corre en otro hilo (``asyncio.to_thread(...)``).
        descripcion: qué trabajo es, para el mensaje de diagnóstico.

    Raises:
        asyncio.CancelledError: el llamador fue cancelado (después de drenar).
    """
    tarea = asyncio.ensure_future(trabajo)
    cancelado = False
    fallo: BaseException | None = None
    while True:
        try:
            await asyncio.shield(tarea)
            break
        except asyncio.CancelledError:
            # Si la tarea ya terminó, el estado está consolidado: se sale.
            # Si no, se sigue esperando al hilo antes de propagar.
            cancelado = True
            if tarea.done():
                break
        except Exception as exc:  # noqa: BLE001 — el error del hilo es opaco: se re-lanza tal cual
            fallo = exc
            break
    if cancelado:
        with contextlib.suppress(Exception):
            tarea.result()  # consume la excepción del trabajo: gana la cancelación
        raise asyncio.CancelledError
    if fallo is not None:
        raise fallo
    return tarea.result()


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
        #: Plan de arranque aplicado (None si el run no llegó al preflight).
        self._plan: PlanArranque | None = None
        #: Respaldo verificado de este run ("" si no hubo rebuild con caché).
        self._backup_dir = ""
        #: Resultado de archivar+borrar el flag (None si el run no adoptó el flag).
        self._neutralizacion: NeutralizacionFlag | None = None

    @property
    def neutralizacion(self) -> NeutralizacionFlag | None:
        """Resultado de archivar/borrar el flag, o ``None`` si no se adoptó."""
        return self._neutralizacion

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def run(self, cancel_event: asyncio.Event | None = None) -> GrassCacheRunResult:
        """Ejecuta el ritual y publica si el flag quedó NEUTRALIZADO.

        El cuerpo real es :meth:`_run_interno`; este wrapper existe porque la
        limpieza del flag ocurre en su ``finally`` (tiene que correr también
        ante ``CancelledError``/``KeyboardInterrupt``), o sea DESPUÉS de que el
        resultado está construido. Sin este paso, un ``unlink`` que falla se
        reportaba como limpieza exitosa y el juego del usuario quedaba
        arrancando en modo precache sin que nadie lo supiera.

        Raises:
            FileNotFoundError: ``ModOrganizer.exe`` ausente (misconfiguración —
                propaga desde ``launch_game``; la capa servicio la reporta).
            GrassCacheSafetyError: El arranque no cumple las precondiciones de
                su modo (F-01). Se levanta ANTES de tocar el flag o lanzar nada.
        """
        try:
            resultado = await self._run_interno(cancel_event)
        except BaseException:
            # La cancelación (o un bug) NO puede tragarse una limpieza fallida:
            # el ``finally`` de ``_run_interno`` ya archivó y borró el flag, pero
            # si esa limpieza no fue completa el resultado nunca se construye y
            # el fallo no llega a ningún lado —el flag queda en disco y nadie lo
            # dice—. Se publica ANTES de propagar y se preserva la excepción
            # original (la cancelación sigue siendo cancelación para el llamador).
            self._avisar_limpieza_incompleta()
            raise
        limpieza = self._neutralizacion
        if limpieza is not None and not limpieza.limpio:
            self._avisar_limpieza_incompleta()
            # Una limpieza incompleta no es éxito, ni aunque el outcome haya
            # sido "completed": el contrato no depende de que el servicio lo
            # corrija más arriba.
            return dataclasses.replace(
                resultado,
                success=False,
                message=(f"{resultado.message} | " if resultado.message else "")
                + "Limpieza del flag incompleta: "
                + ("; ".join(limpieza.errores) or "causa desconocida"),
                flag_residual=not limpieza.borrado,
                progreso_perdido=limpieza.progreso_perdido,
                limpieza_errores=limpieza.errores,
            )
        return resultado

    def _avisar_limpieza_incompleta(self) -> None:
        """Publica el fallo de limpieza del ``finally`` — también al propagar.

        Es el único punto donde el fallo de ``neutralizar_flag`` se hace visible
        cuando el run NO termina devolviendo un resultado (cancelación, error):
        sin esto, un ``unlink`` fallido después de adoptar el flag quedaba
        silencioso. El resultado estructurado (``success=False``,
        ``flag_residual``, ``limpieza_errores``) sigue siendo el otro camino.
        """
        limpieza = self._neutralizacion
        if limpieza is None or limpieza.limpio:
            return
        if limpieza.borrado:
            logger.warning(
                "El flag %s se borró, pero su progreso NO se archivó: la reanudación de este "
                "caché se perdió. Errores: %s",
                self._flag_path,
                "; ".join(limpieza.errores) or "desconocido",
            )
        else:
            logger.warning(
                "El flag %s NO se pudo eliminar: el juego puede arrancar en modo precache. Errores: %s",
                self._flag_path,
                "; ".join(limpieza.errores) or "desconocido",
            )

    async def _run_interno(self, cancel_event: asyncio.Event | None = None) -> GrassCacheRunResult:
        """Crash-loop supervisado (ver :meth:`run`)."""
        cancel = cancel_event if cancel_event is not None else asyncio.Event()
        self._start_mono = time.monotonic()
        crash_count = 0
        stall_streak = 0
        game_ever_seen = False
        game_pid: _GamePid | None = None
        # Un flag que NO escribimos nosotros no es nuestro para borrarlo: si el
        # guard de abajo se niega, el ``finally`` no puede destruir el progreso
        # de un precache ajeno (sería cambiar un borrado de caché por otro).
        flag_apropiado = False

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
            # P0 SAFE CACHE (F-01): NGIO borra .cgid/.dgid/.fail cuando el flag
            # no tiene progreso, así que escribir el flag es el punto sin
            # retorno. El plan lo decide grass_cache_safety (misma función que
            # usa el servicio): NEW exige caché vacío, RESUME nunca trunca,
            # REBUILD exige aprobación + respaldo VERIFICADO.
            # Drenada: si la cancelación llega mientras se COPIA el respaldo, el
            # servicio soltaría los dos locks y haría teardown del perfil con el
            # hilo todavía escribiendo. `esperar_terminar` retiene la cancelación
            # hasta que el hilo termina.
            plan = await esperar_terminar(
                asyncio.to_thread(
                    preparar_arranque,
                    modo=self._config.mode,
                    game_path=self._config.game_path,
                    grass_dir=self._config.overwrite_grass_dir,
                    aprobacion=self._config.rebuild_approval,
                    raiz_estado=self._config.backup_root,
                    ack_usvfs_no_verificado=self._config.ack_usvfs_no_verificado,
                    perfil=self._config.perfil_de_progreso,
                ),
                descripcion="preparar_arranque",
            )
            self._plan = plan
            # Cancelación COOPERATIVA pedida durante la preparación: sin este
            # corte, el runner escribía el flag vacío y lanzaba el juego igual,
            # porque el evento sólo se miraba después del lanzamiento.
            if cancel.is_set():
                return self._resultado(
                    "cancelled",
                    "Cancelado durante la preparación del arranque.",
                    crash_count,
                    (0, 0),
                )
            self._backup_dir = ruta_de_respaldo(plan)
            # El try cubre DESDE la escritura del flag: una cancelación dura
            # durante el snapshot inicial o el primer progress (que propaga
            # CancelledError) también debe limpiar el flag en el finally.
            # El estado de la aplicación se lee TAMBIÉN si la verificación
            # posterior falla: si la sustitución ocurrió, el flag en disco es
            # nuestro y limpiarlo sigue siendo responsabilidad de este run.
            estado_aplicacion = EstadoAplicacion()
            try:
                await self._aplicar_plan_en_seccion_critica(plan, estado_aplicacion)
            finally:
                # La sección crítica NO vuelve mientras el hilo de escritura
                # siga vivo, así que acá el estado en disco ya es conocido: si
                # la sustitución ocurrió (aunque la cancelación o un error
                # lleguen después), el flag es nuestro y limpiarlo es
                # responsabilidad de este run.
                if estado_aplicacion.escrito:
                    flag_apropiado = True
            # Con la escritura consolidada, el flag —escrito por el plan o
            # conservado como evidencia de reanudación— queda bajo custodia de
            # este run, que es quien lo archiva y lo neutraliza al salir.
            flag_apropiado = True
            # Baseline del stall detector: el estado del cache ANTES del primer
            # lanzamiento — un primer ciclo sin .cgid nuevos ya cuenta como racha.
            last_snapshot = await self._snapshot_cache()
            await self._emit_progress("launching", crash_count)

            # ``intento`` = lanzamientos ya hechos: el 0 es el autorizado por el plan.
            for intento in range(self._config.max_restarts + 1):
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
                if cancel.is_set():
                    # Entre relanzamientos y antes del PRIMER spawn: el evento
                    # pedido durante el snapshot o el progress no puede terminar
                    # en un juego recién lanzado que nadie va a mirar.
                    return self._resultado(
                        "cancelled",
                        "Cancelado antes del lanzamiento.",
                        crash_count,
                        await self._snapshot_cache(),
                    )
                if intento:
                    # El plan sólo autorizó el PRIMER lanzamiento. Cada
                    # relanzamiento se revalida contra el flag real: un archivo
                    # que EXISTE pero ya no tiene progreso hace que NGIO borre
                    # el caché parcial en el próximo arranque.
                    motivo = await self._motivo_para_no_relanzar()
                    if motivo is not None:
                        await self._matar_todo(game_pid)
                        return self._resultado("flag_sin_progreso", motivo, crash_count, await self._snapshot_cache())
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
            # Antes de borrar el flag se archiva el progreso: neutralizar el flag
            # no puede costar la reanudación (P0, evidencia de F-01).
            #
            # Se hace en un HILO, no en el loop: archivar lee y hashea TODO el
            # caché parcial (miles de .cgid/.dgid/.fail), y el heartbeat de los
            # locks `grass-cache` y `load-order` vive en este mismo loop. Un hash
            # largo bloqueaba la renovación y el lease podía expirar en silencio
            # mientras el run seguía mutando (F2).
            #
            # Y se hace DRENADO (`esperar_terminar`): un await normal durante una
            # CancelledError abandonaría el hilo y dejaría el flag vivo sin dueño
            # (B11). La cancelación se propaga recién cuando el worker terminó, y
            # `self._neutralizacion` queda cargado para que `run()` publique la
            # limpieza incompleta antes de propagar.
            if flag_apropiado:
                await self._neutralizar_flag_drenado()

    async def _aplicar_plan_en_seccion_critica(self, plan: PlanArranque, estado: EstadoAplicacion) -> None:
        """Escribe el flag sin poder abandonar la escritura a medias.

        ``asyncio.to_thread`` NO es cancelable: al cancelar la tarea el ``await``
        levanta ``CancelledError`` y el hilo SIGUE escribiendo. Un ``finally`` que
        consulta ``estado.escrito`` en ese instante ve ``False``, se saltea la
        limpieza, y el hilo termina después dejando un flag vacío sin dueño —que
        el próximo arranque del juego convierte en borrado del caché—. Atrapar
        ``GrassCacheSafetyError`` no cambia nada: la cancelación no pasa por ahí.

        La espera la hace :func:`esperar_terminar`: al recibir ``CancelledError``
        no se propaga mientras el hilo siga en vuelo; se espera a que TERMINE y
        recién entonces se levanta. Así el run conoce el resultado real de la
        escritura antes de salir y el ``finally`` neutraliza lo que sea nuestro.
        La neutralización usa el mismo patrón (``_neutralizar_flag_drenado``):
        corre en un hilo para no bloquear el heartbeat de los locks, y se espera
        drenada para que la cancelación no la abandone a mitad de camino.
        """
        await esperar_terminar(
            asyncio.to_thread(aplicar_plan, plan, self._flag_path, estado=estado),
            descripcion="aplicar_plan",
        )

    # ------------------------------------------------------------------
    # Fases internas
    # ------------------------------------------------------------------

    async def _vigilar(self, game_pid: _GamePid, cancel: asyncio.Event, crash_count: int) -> GrassCacheRunResult | None:
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
            if not await self._pid_alive(game_pid.pid):
                return None  # crash: murió con el flag presente
            if time.monotonic() - last_heartbeat >= self._config.heartbeat_interval_s:
                await self._emit_progress("scanning", crash_count)
                last_heartbeat = time.monotonic()
            # La cancelación que llegue durante el sleep la atiende el check ①
            # del próximo ciclo (el sleep despierta al instante con el event).
            await self._sleep_or_cancel(cancel, self._config.poll_interval_s)

    async def _salida_exito(self, crash_count: int, game_pid: _GamePid | None) -> GrassCacheRunResult:
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
        self, game_pid: _GamePid | None, mo2_pid: int | None, min_create_epoch: float
    ) -> _GamePid | None:
        """Búsqueda final del juego antes de una salida desde la spawn window.

        El flag puede borrarse (o llegar una cancelación/timeout) ANTES de que
        el juego fuera atribuido: sin este intento final, un juego reparentado
        quedaría vivo tras la salida (D7b — auditoría #287).
        """
        if game_pid is not None or mo2_pid is None:
            return game_pid
        return await self._find_game_pid(mo2_pid, min_create_epoch)

    async def _matar_todo(self, game_pid: _GamePid | None) -> None:
        """``close_game()`` (árbol MO2, M-8) + kill directo del juego (D7b).

        Si MO2 salió tras lanzar, su PID está muerto y ``close_game`` no
        alcanza al juego reparentado: el kill directo cierra ese hueco.
        """
        await self._mo2.close_game()
        if game_pid is not None and await self._pid_alive(game_pid.pid):
            await asyncio.to_thread(self._kill_game_tree_sync, game_pid.pid, game_pid.create_time)

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

    async def _motivo_para_no_relanzar(self) -> str | None:
        """¿Puede NGIO leer el flag como PROGRESO antes del próximo spawn?

        ``None`` = seguro relanzar. Un string = motivo del corte (fail-closed).

        Que el archivo exista no es lo mismo que NGIO lo lea como progreso: con
        el flag sin progreso y ``.cgid/.dgid/.fail`` alcanzables, el ``remove()``
        de ``GidFileCache.cpp::Init()`` (1.0.13) borra el caché en el próximo
        arranque. El plan autoriza un único lanzamiento —el que validó al
        aplicar el flag—; los siguientes no pueden asumir que sigue siendo
        seguro.

        La decisión se toma con el MISMO inventario y predicado del módulo de
        seguridad (`inventariar_cache` → `_ngio_tiene_progreso`): no hay un
        segundo parser. Corre en un hilo, drenado, como el resto del E/S del
        runner.

        **Alcance:** los archivos que se pueden inventariar son los del caché
        FÍSICO (`overwrite/Grass`). Los `.cgid` de mods que la USVFS superpone en
        `Data/Grass` **no** son visibles acá: su cobertura sigue NO VERIFICADA
        (§4.1/§4.2 de la auditoría) y por eso el contrato no promete nada sobre
        ellos, ni antes ni después de este chequeo.
        """
        inventario = await esperar_terminar(
            asyncio.to_thread(
                inventariar_cache,
                grass_dir=self._config.overwrite_grass_dir,
                flag_path=self._flag_path,
            ),
            descripcion="inventariar_cache",
        )
        if inventario.flag_progreso is not None:
            return None  # NGIO reanuda: el relanzamiento continúa el trabajo
        if not inventario.tiene_cache:
            return None  # nada que NGIO pueda borrar
        if self._config.mode == "resume":
            return (
                "El flag de precache quedó SIN progreso y el caché parcial tiene "
                f"{inventario.archivos_cache} archivo(s): relanzar dejaría que NGIO los borre. "
                "No se relanza y el caché no se toca; revisá qué vació el flag y, si hay que "
                "regenerar, usá mode='rebuild' con respaldo verificado."
            )
        return (
            f"El flag de precache quedó sin progreso y hay {inventario.archivos_cache} archivo(s) de "
            f"caché parcial: relanzar en modo '{self._config.mode}' dejaría que NGIO borre lo generado "
            "en esta corrida. Se corta sin borrar ni restaurar nada; el caché parcial queda en disco "
            "para decidir resume/rebuild con la evidencia a la vista."
        )

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

    async def _find_game_pid(self, mo2_pid: int, min_create_epoch: float) -> _GamePid | None:
        return await asyncio.to_thread(self._scan_for_game_sync, mo2_pid, min_create_epoch)

    def _scan_for_game_sync(self, mo2_pid: int, min_create_epoch: float) -> _GamePid | None:
        """UNA pasada de búsqueda del juego (el caller polea).

        Rama B (D1): hijo del árbol de MO2. Rama C (fallback): búsqueda global
        por nombre acotada por ``create_time >= min_create_epoch`` **y** por el
        exe residente en ``game_path`` — no adopta un Skyrim preexistente del
        usuario, ni uno de otra instalación lanzado en la ventana, ni pierde al
        juego reparentado. La atribución perfecta post-reparent es imposible
        (el vínculo padre-hijo se perdió); si el exe no es legible
        (AccessDenied), se acepta por nombre+tiempo con warning.

        El ``create_time`` capturado ACÁ (no en el kill) es lo que le permite a
        ``_kill_game_tree_sync`` revalidar identidad más tarde — review Codex
        #316: un PID reusado por otro exe de la MISMA instalación pasaría el
        chequeo de nombre+exe pero tendría un ``create_time`` distinto.
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
                    creado: float | None
                    try:
                        creado = hijo.create_time()
                    except psutil.Error:
                        creado = None
                    return _GamePid(pid=int(hijo.pid), create_time=creado)
            except psutil.Error:
                continue
        try:
            for proc in psutil.process_iter(["name", "create_time", "exe"]):
                try:
                    info = proc.info
                    nombre = str(info.get("name") or "")
                    creado_raw = info.get("create_time")
                    creado_fallback = float(creado_raw or 0.0)
                    exe = info.get("exe")
                except psutil.Error:
                    continue
                if nombre.lower() not in nombres or creado_fallback < min_create_epoch:
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
                return _GamePid(pid=int(proc.pid), create_time=creado_raw)
        except psutil.Error:
            logger.warning("process_iter falló durante la búsqueda del juego.", exc_info=True)
        return None

    async def _pid_alive(self, pid: int) -> bool:
        return bool(await asyncio.to_thread(psutil.pid_exists, pid))

    def _es_proceso_del_juego(self, proc: psutil.Process, expected_create_time: float | None) -> bool:
        """True si *proc* es el MISMO juego atribuido (nombre + exe + create_time).

        Misma verificación de nombre+exe que ``_scan_for_game_sync`` usa al
        ATRIBUIR el juego, MÁS ``create_time`` (review Codex #316): un PID
        reusado por OTRO ``SkyrimSE.exe`` de la MISMA instalación (p. ej. el
        usuario relanza el juego manualmente entre la atribución y el kill)
        pasaría el chequeo de nombre+exe solo, pero tendría un ``create_time``
        distinto al capturado en la atribución — matar su árbol a ciegas
        cerraría la sesión que el usuario acaba de abrir. Fail-closed: nombre
        que no matchea, exe ilegible (AccessDenied) o ``create_time`` que no
        coincide ⇒ NO es nuestro juego, no se mata. ``expected_create_time=None``
        (no se pudo leer al atribuir) omite ESE chequeo puntual — el proceso
        casi siempre ya murió para cuando esto importa (mismo criterio que
        ``vfs.MO2Controller._kill_process_tree``, review Codex #302).
        """
        nombres = {n.lower() for n in self._config.game_exe_names}
        try:
            if proc.name().lower() not in nombres:
                return False
            if expected_create_time is not None and proc.create_time() != expected_create_time:
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

    def _kill_game_tree_sync(self, pid: int, expected_create_time: float | None = None) -> None:
        """Kill directo del árbol del juego (réplica acotada de M-8 sobre el
        game_pid — D7b). Best-effort.

        Revalida la identidad del PID raíz ANTES de matar (§2.5 + #316): un PID
        reusado por el SO tras morir/relanzarse el juego original tendría otro
        nombre/exe/create_time → no se mata un proceso ajeno del usuario. Los
        hijos se matan solo si la raíz verificó (son descendientes del juego
        real)."""
        try:
            root = psutil.Process(pid)
        except psutil.Error:
            return
        if not self._es_proceso_del_juego(root, expected_create_time):
            logger.warning(
                "No se mata el pid=%s: ya no es el mismo exe del juego bajo %s (PID reusado por el SO).",
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

    async def _neutralizar_flag_drenado(self) -> NeutralizacionFlag:
        """Archiva el progreso y borra el flag en un hilo, sin abandonarlo.

        El borrado es obligatorio —un flag residual haría que el juego del
        usuario arranque en modo precache—, pero el contenido se archiva antes,
        atado a la huella del caché de este momento: es lo que un ``resume``
        posterior restaura. ``neutralizar_flag`` no propaga errores: devuelve el
        resultado con ``errores``/``progreso_perdido`` y ``run()`` lo publica.

        ``esperar_terminar`` es lo que hace que sacarlo del loop NO reabra B11:
        si la tarea se cancela, la espera no se propaga hasta que el hilo
        termina, así que el flag queda neutralizado (o su fallo registrado)
        ANTES de que el servicio suelte los locks y haga teardown.
        """

        async def _archivar_y_registrar() -> NeutralizacionFlag:
            resultado = await asyncio.to_thread(
                neutralizar_flag,
                flag_path=self._flag_path,
                grass_dir=self._config.overwrite_grass_dir,
                raiz_estado=self._config.backup_root,
                perfil=self._config.perfil_de_progreso,
            )
            # Se registra ACÁ, no en el llamador: si la cancelación llega
            # mientras el hilo neutraliza, `esperar_terminar` espera a que
            # termine y recién entonces propaga, así que un
            # `self._neutralizacion = await …` del llamador NUNCA correría y un
            # flag que no se pudo borrar se perdería en silencio.
            self._neutralizacion = resultado
            if resultado.errores or not resultado.limpio:
                logger.warning(
                    "Neutralización del flag incompleta (borrado=%s, progreso_perdido=%s): %s",
                    resultado.borrado,
                    resultado.progreso_perdido,
                    "; ".join(resultado.errores) or "sin detalle",
                )
            return resultado

        return await esperar_terminar(_archivar_y_registrar(), descripcion="neutralizar_flag")

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
            mode=self._config.mode,
            backup_dir=self._backup_dir,
        )


__all__ = [
    "GrassCacheConfig",
    "GrassCacheOutcome",
    "GrassCachePhase",
    "GrassCacheProgress",
    "GrassCacheRunResult",
    "GrassCacheRunner",
    "esperar_terminar",
]

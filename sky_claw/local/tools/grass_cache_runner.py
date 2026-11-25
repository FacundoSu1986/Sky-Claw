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
import re
import shutil  # import de módulo completo: los tests mockean shutil.disk_usage
import stat
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar

import psutil  # import de módulo completo: los tests reemplazan el objeto entero

from sky_claw.app.security.links import path_present
from sky_claw.app.security.path_validator import assert_safe_component
from sky_claw.local.mo2.vfs import GameLaunchTimeoutError, MO2Controller
from sky_claw.local.tools.grass_cache_fence import (
    FenceProceso,
    borrar_fence,
    escribir_fence,
    ruta_fence,
)
from sky_claw.local.tools.grass_cache_safety import (
    EXTENSIONES_CACHE,
    MODOS_GRASS_CACHE,
    PRECACHE_FLAG,
    EstadoAplicacion,
    GrassCacheMode,
    GrassCacheSafetyError,
    InventarioCache,
    NeutralizacionFlag,
    PlanArranque,
    aplicar_plan,
    exigir_cobertura_usvfs,
    exigir_flag_conservado,
    exigir_sin_flag_alternativo,
    inventariar_cache,
    neutralizar_flag,
    preparar_arranque,
    ruta_de_respaldo,
    ruta_del_flag_alternativo,
    verificar_respaldo_para_borrado,
)
from sky_claw.local.tools.grass_cache_verdict import (
    AlcanceCertificado,
    CacheObservado,
    EstadoLanzamiento,
    EvidenciaVeredicto,
    GrassCacheOutcome,
    GrassCacheVerdict,
    Terminalidad,
    VeredictoCache,
    evaluar_veredicto,
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
#: Tope de bytes que el watchdog lee del flag por muestra (lectura incremental, en hilo).
_TOPE_LECTURA_FLAG = 1 << 20
#: Tope de bytes que se leen de un `.fail` (su contenido es un contador: `<n>\n`).
_TOPE_FALLO_BYTES = 64
#: Espera entre reintentos de la verificación de terminalidad (RD-10).
_ESPERA_TERMINALIDAD_S = 0.2
#: ``<WorldspaceEditorID>x<XXXX>y<YYYY>.cgid`` — el artefacto es POR CELDA, no por worldspace:
#: contar `.cgid` y compararlos con worldspaces esperados no es una comparación válida (1.0.13,
#: ``GidFileCache.cpp``: ``"Grass\\\\%sx%04dy%04d.cgid"``).
_WS_DE_ARTEFACTO = re.compile(r"^(?P<ws>.+)x-?\d+y-?\d+$", re.IGNORECASE)
#: Texto operatorio por razón del vocabulario cerrado (``grass_cache_verdict``).
_RAZONES_LEGIBLES: Final[dict[str, str]] = {
    "cache_ilegible": "no se pudo leer el caché al salir",
    "terminalidad_no_verificada": "no se pudo demostrar que SkyrimSE.exe terminó",
    "cache_vacio": "el caché quedó vacío (posible fallo silencioso por bounds nulos)",
    "limpieza_incompleta": "la limpieza del flag de precache no fue completa",
    "archivos_heredados_sin_procedencia": (
        "el caché presente ya estaba antes de esta corrida: no hay evidencia de regeneración"
    ),
    "sin_evidencia_de_esta_corrida": "no hay evidencia de avance de esta corrida sobre ese caché",
    "fallos_de_celda_atribuibles": "hay celdas que fallaron en esta corrida (archivos .fail)",
    "fallos_de_celda_historicos": "quedan celdas sin completar de intentos anteriores (archivos .fail)",
    "worldspaces_sin_artefactos": "hay worldspaces solicitados sin ningún artefacto",
    "outcome_no_completado": "el ritual no llegó a completarse",
    "sin_certificacion": "sin evidencia suficiente para certificar",
    "cancelado": "cancelado por el operador",
}

#: Cómo terminó el ritual. La definición vive en ``grass_cache_verdict`` (la política pura que
#: decide el veredicto la necesita y no puede depender de este módulo: sería un ciclo). Se re-exporta
#: porque el servicio y los tests la importan de acá desde PR-5.
#: ``GrassCacheOutcome`` se importa arriba.
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
    #: Worldspaces que el payload pidió precachear (Fase A). Viaja al resultado como identidad de
    #: la corrida y como requisito de certificación: un worldspace pedido sin NINGÚN artefacto no
    #: permite certificar (F-12/C1). Vacío = no se pidió filtro explícito.
    worldspaces: tuple[str, ...] = ()
    #: Cada cuánto se muestrea el progreso durante la vigilancia (barato: nombres, no `stat`).
    progress_poll_interval_s: float = 30.0
    #: Sin NINGÚN cambio observable en el caché ni en el flag durante este tiempo, con el proceso
    #: vivo, el run se corta como ``stalled``. NGIO usa 60 s para su propio freeze-check: 600 s es
    #: 10× más conservador y evita competir con él.
    stall_progress_s: float = 600.0
    #: Gracia desde el spawn del ciclo antes de empezar a medir: cargar el mundo puede tardar.
    stall_grace_s: float = 1200.0
    #: Cuánto esperar, después de matar, para poder DEMOSTRAR que el proceso terminó. Sin esto, la
    #: latencia normal de muerte degradaría a ``unknown`` un run perfecto (RD-10).
    terminality_wait_s: float = 10.0

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
        if self.terminality_wait_s < 0:
            raise ValueError("terminality_wait_s no puede ser negativo")
        if self.stall_progress_s <= 0:
            raise ValueError("stall_progress_s debe ser > 0")
        if self.stall_grace_s < 0:
            raise ValueError("stall_grace_s no puede ser negativo")
        if self.progress_poll_interval_s <= 0:
            raise ValueError("progress_poll_interval_s debe ser > 0")
        if self.progress_poll_interval_s > self.stall_progress_s:
            raise ValueError(
                "progress_poll_interval_s no puede superar stall_progress_s: "
                "el watchdog no detectaría el stall antes de que venza el deadline"
            )
        for ws in self.worldspaces:
            assert_safe_component(ws, field="worldspaces")
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
    #: True si quedó el flag ALTERNATIVO (``PrecacheGrass.txt.txt``) en disco: no es
    #: nuestro y no se borra, pero la limpieza no es completa mientras exista.
    flag_alternativo_residual: bool = False
    #: Errores concretos de la limpieza (archivado/borrado del flag).
    limpieza_errores: tuple[str, ...] = ()
    #: Veredicto de la política (P1): qué se puede afirmar del caché que quedó.
    verdict: GrassCacheVerdict = "unknown"
    #: True SÓLO si ``verdict == "completed_verified"`` (D1: ``success`` no es certificación).
    certificado: bool = False
    #: Por qué NO se certificó (vocabulario cerrado de ``grass_cache_verdict``).
    verdict_razones: tuple[str, ...] = ()
    #: Alcance de la certificación (D2): corrida completa / trabajo de la corrida / ninguno.
    alcance_certificado: AlcanceCertificado = "ninguno"
    #: Evidencia observada, serializable (identidad, inventarios, `.fail`, progreso, terminalidad).
    evidencia: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _CorteDeLanzamiento:
    """Veredicto que BLOQUEA un lanzamiento, con su código del contrato.

    Lo devuelve :meth:`GrassCacheRunner._autorizar_lanzamiento`, el único punto
    que autoriza un ``launch_game()``. ``outcome`` es el código que viaja al
    resultado/journal; ``motivo`` explica el estado concreto que lo invalidó.
    """

    outcome: GrassCacheOutcome
    motivo: str


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
        #: True si el fallo de limpieza ya se logueó al neutralizar: `run()` no lo repite.
        self._limpieza_logueada = False
        # -- P1: evidencia y watchdog (ver `grass_cache_verdict`) --
        #: Evidencia de egreso; `run()` la RE-EVALÚA con el resultado real de la limpieza (RD-11).
        self._evidencia: EvidenciaVeredicto | None = None
        #: (nombre, tamaño) del caché ANTES del primer lanzamiento: procedencia de lo heredado.
        self._baseline_entradas: frozenset[tuple[str, int]] = frozenset()
        #: nombre → contenido de cada `.fail` del baseline (atribución por contenido, no por fecha).
        self._fallos_previos: dict[str, bytes] = {}
        #: El watchdog observó avanzar algún indicador durante la corrida.
        self._progreso_observado = False
        #: Segundos sin avance observable en el corte por stall (None si no aplica).
        self._progreso_detenido_s: float | None = None
        #: Tri-estado de terminalidad del proceso del juego (RD-10 / H2).
        # H2/H5: ANTES de cualquier spawn el estado correcto es "sin_proceso", no "terminado".
        self._terminalidad: Terminalidad = "sin_proceso"
        #: Último proceso ATRIBUIDO (sólo para el cierre síncrono del camino de excepción).
        self._ultimo_atribuido: _GamePid | None = None
        #: Atribución POR INTENTO: un PID de un intento anterior no prueba nada sobre el actual.
        self._atribuciones: dict[int, _GamePid] = {}
        #: ¿Se pidió el spawn en este intento? y ¿desde qué epoch wall-clock buscarlo?
        self._spawn_solicitado: dict[int, bool] = {}
        self._epoch_del_intento: dict[int, float] = {}
        self._intento_actual = 0
        #: Qué pasó con el lanzamiento del intento actual (H2/H5).
        self._lanzamiento: EstadoLanzamiento = "no_intentado"
        #: Conteo barato de TODOS los artefactos al cerrar (H-C: `.dgid`/`.fail` también son caché).
        self._conteo_artefactos: CacheObservado | None = None
        #: El conteo al cerrar no se pudo leer: la evidencia de salidas parciales es incompleta.
        self._conteo_ilegible = False
        #: `.fail` presentes pero ilegibles en el inventario de egreso (B4).
        self._fallos_ilegibles: frozenset[str] = frozenset()
        #: PID de MO2 por intento: si el lanzador sigue vivo, una búsqueda vacía no es ausencia (B2).
        self._mo2_pid_del_intento: dict[int, int] = {}
        #: Identidad de la corrida y ruta del fence durable (F1).
        self._run_id = uuid.uuid4().hex
        self._ruta_fence: pathlib.Path | None = None
        if self._config.backup_root is not None:
            self._ruta_fence = ruta_fence(
                raiz_estado=self._config.backup_root,
                game_path=self._config.game_path,
                overwrite_grass_dir=self._config.overwrite_grass_dir,
            )
        #: Worldspaces que NGIO DECLARÓ terminados (líneas `ws_` del flag): evidencia, no requisito.
        self._ws_declarados: set[str] = set()
        #: Offset de lectura incremental del flag y última muestra del watchdog.
        self._flag_offset = 0
        self._muestra_previa: tuple[int, int, int, int] | None = None
        self._ultimo_avance_mono: float | None = None
        self._proximo_probe_mono = 0.0

    @property
    def neutralizacion(self) -> NeutralizacionFlag | None:
        """Resultado de archivar/borrar el flag, o ``None`` si no se adoptó."""
        return self._neutralizacion

    @property
    def terminalidad(self) -> Terminalidad:
        """Última terminalidad observada del proceso del juego (H2/H5).

        El servicio la consulta también cuando el run propaga una excepción (cancelación dura): sin
        esto, un teardown destruiría el perfil temporal que un proceso vivo pueda estar usando.
        """
        return self._terminalidad

    @property
    def lanzamiento(self) -> EstadoLanzamiento:
        """Estado del último lanzamiento intentado (``no_intentado``/``rechazado``/…)."""
        return self._lanzamiento

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
        limpio = limpieza is None or limpieza.limpio
        # RD-11: el veredicto NO puede quedar cerrado antes de neutralizar el flag. Se RE-EVALÚA la
        # misma evidencia con el resultado real de la limpieza; el provisional nunca se publica.
        resultado = self._veredicto_final(resultado, limpio=limpio)
        # F1: el fence se borra SÓLO cuando la terminalidad quedó demostrada. Si quedó viva o
        # indeterminada, el marcador sobrevive (también a un reinicio de Sky-Claw).
        if self._ruta_fence is not None and self._terminalidad in ("terminado", "sin_proceso"):
            borrar_fence(self._ruta_fence)
        if self._neutralizacion is not None and self._terminalidad not in ("terminado", "sin_proceso"):
            # H2/H5: el archivado del progreso se omitió a propósito (caché posiblemente en curso).
            # Aplica también a los caminos sin evidencia de egreso (timeout, cancelación, cortes).
            resultado = dataclasses.replace(resultado, evidencia={**resultado.evidencia, "archivado_omitido": True})
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
                flag_alternativo_residual=limpieza.alternativo_residual,
                progreso_perdido=limpieza.progreso_perdido,
                limpieza_errores=limpieza.errores,
            )
        return resultado

    def _veredicto_final(self, resultado: GrassCacheRunResult, *, limpio: bool) -> GrassCacheRunResult:
        """Cierra el veredicto con el resultado REAL de la neutralización (RD-11).

        El veredicto provisional se calculó al salir del ritual, cuando todavía no se sabía si el
        flag quedó limpio. Se re-evalúa la MISMA evidencia con ``limpieza_limpia`` real (una sola
        política, un solo lugar que decide) y se publica recién ahora. Los caminos que no dejan
        evidencia de egreso (timeout, cancelación, cortes de contrato) conservan su veredicto.
        """
        if self._evidencia is None:
            return resultado
        evidencia = dataclasses.replace(self._evidencia, limpieza_limpia=limpio)
        self._evidencia = evidencia
        veredicto = evaluar_veredicto(evidencia)
        evidencia_dict = {**resultado.evidencia, **veredicto.a_dict()}
        if self._neutralizacion is not None and self._terminalidad not in ("terminado", "sin_proceso"):
            # H2/H5: el archivado del progreso se omitió a propósito (caché posiblemente en curso).
            evidencia_dict["archivado_omitido"] = True
        return dataclasses.replace(
            resultado,
            success=veredicto.exito_operacional,
            verdict=veredicto.verdict,
            certificado=veredicto.certificado,
            verdict_razones=veredicto.razones,
            alcance_certificado=veredicto.alcance_certificado,
            evidencia=evidencia_dict,
            message=self._mensaje_de_veredicto(veredicto),
        )

    @staticmethod
    def _mensaje_de_veredicto(veredicto: VeredictoCache) -> str:
        """Mensaje operatorio. Un resultado no certificado NUNCA recomienda rebuild (D1)."""
        if veredicto.certificado:
            return ""
        razones = "; ".join(_RAZONES_LEGIBLES.get(razon, razon) for razon in veredicto.razones)
        return (
            "El precache terminó, pero NO se pudo certificar el caché: "
            f"{razones or 'sin evidencia suficiente'}. El caché se conserva y la evidencia queda en "
            "el campo `evidencia` del resultado; no se declara una generación válida."
        )

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
        if self._limpieza_logueada:
            # El fallo ya se publicó al neutralizar (`_neutralizar_flag_drenado`):
            # repetirlo acá duplicaba el mismo warning en el camino normal.
            return
        self._limpieza_logueada = True
        if limpieza.alternativo_residual:
            logger.warning(
                "Queda un flag ALTERNATIVO residual (%s): no es de este run y no se borra, pero NGIO "
                "puede abrirlo como flag de progreso. La limpieza no es completa. Errores: %s",
                ruta_del_flag_alternativo(self._config.game_path),
                "; ".join(limpieza.errores) or "desconocido",
            )
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
            # Baseline de PROCEDENCIA (P1/H4): qué archivos ya estaban ANTES del primer spawn. Se
            # captura antes de aplicar el plan porque escribir el flag es el punto sin retorno: si el
            # inventario inicial no se puede verificar, no se toca el disco ni se lanza nada.
            try:
                self._baseline_entradas, self._fallos_previos = await esperar_terminar(
                    asyncio.to_thread(self._capturar_baseline_sync),
                    descripcion="baseline_cache",
                )
            except OSError:
                logger.warning(
                    "No se pudo inventariar el caché antes de arrancar: el run se detiene sin lanzar.",
                    exc_info=True,
                )
                evidencia = EvidenciaVeredicto(
                    outcome="cache_cambio_despues_del_plan",
                    modo=self._config.mode,
                    terminalidad="terminado",
                    limpieza_limpia=None,
                    evidencia_ilegible=True,
                    baseline_ilegible=True,
                )
                self._evidencia = evidencia
                veredicto = evaluar_veredicto(evidencia)
                return self._resultado(
                    "cache_cambio_despues_del_plan",
                    "No se pudo verificar el inventario inicial del caché de grass (baseline "
                    "ilegible): sin procedencia demostrable no se lanza el juego ni se escribe el flag.",
                    crash_count,
                    (0, 0),
                    veredicto=veredicto,
                    evidencia={**self._evidencia_a_dict(evidencia), **veredicto.a_dict()},
                )
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
                self._intento_actual = intento
                if time.monotonic() - self._start_mono >= self._config.max_runtime_s:
                    # Deadline global también entre relanzamientos: acota el
                    # overshoot a un relaunch_delay (auditoría #287).
                    await self._cerrar_y_verificar(game_pid, cancel)
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
                # Contrato único de autorización: se corre para TODOS los
                # lanzamientos —el primero incluido— inmediatamente antes del
                # spawn. Cubre los seis invariantes (flag esperado, inventario
                # sin cambios en NEW, respaldo aún válido en REBUILD, progreso
                # concordante en RESUME, progreso válido en todo relanzamiento y
                # ausencia del flag alternativo siempre).
                corte = await self._autorizar_lanzamiento(intento=intento, plan=plan)
                if corte is not None:
                    await self._cerrar_y_verificar(game_pid, cancel)
                    return self._resultado(corte.outcome, corte.motivo, crash_count, await self._snapshot_cache())
                # Invariante 6: la inspección hace E/S (hilo) y el callback
                # `launching` ya corrió. Entre el veredicto y el spawn se vuelven
                # a mirar cancelación y deadline: un juego que nadie va a mirar —
                # o un run fuera de plazo— no se lanza.
                if cancel.is_set():
                    await self._cerrar_y_verificar(game_pid, cancel)
                    return self._resultado(
                        "cancelled",
                        "Cancelado durante la autorización del lanzamiento.",
                        crash_count,
                        await self._snapshot_cache(),
                    )
                if time.monotonic() - self._start_mono >= self._config.max_runtime_s:
                    await self._cerrar_y_verificar(game_pid, cancel)
                    return self._resultado(
                        "timeout",
                        f"Deadline global de {self._config.max_runtime_s:.0f}s alcanzado durante la "
                        "autorización del lanzamiento; el cache parcial se conserva.",
                        crash_count,
                        await self._snapshot_cache(),
                    )
                game_pid = None
                # H2/H5: la atribución es POR INTENTO y ningún lanzamiento nuevo se autoriza si el
                # estado del intento anterior es vivo o indeterminado. Un `launch_game` sobre un
                # proceso potencialmente vivo duplica el crash-loop y puede escribir el caché con
                # dos juegos a la vez.
                if intento > 0 and self._terminalidad not in ("terminado", "sin_proceso"):
                    return self._resultado(
                        "terminalidad_indeterminada",
                        "No se relanza: el intento anterior no tiene terminalidad demostrada "
                        f"({self._lanzamiento}/{self._terminalidad}). El caché y el proceso quedan "
                        "como están; verificar a mano antes de reintentar.",
                        crash_count,
                        await self._snapshot_cache(),
                    )
                self._spawn_solicitado[intento] = True
                self._epoch_del_intento[intento] = time.time() - _CREATE_TIME_EPSILON
                # F1: el marcador se escribe ANTES de pedir el spawn. Si Sky-Claw muere entre acá y
                # el kill, el próximo arranque encuentra incertidumbre en vez de silencio.
                if self._ruta_fence is not None:
                    try:
                        escribir_fence(
                            self._ruta_fence,
                            FenceProceso(
                                run_id=self._run_id,
                                intento=intento,
                                lanzamiento="solicitado",
                                pid=self._pid_del_intento(intento),
                                create_time=self._create_time_del_intento(intento),
                                modo=self._config.mode,
                                game_path=str(self._config.game_path),
                                overwrite_grass_dir=str(self._config.overwrite_grass_dir),
                                creado=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            ),
                        )
                    except Exception:  # noqa: BLE001 — no poder escribir el fence no cancela el ritual
                        logger.warning("No se pudo escribir el fence del precache.", exc_info=True)
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
                if mo2_pid is not None:
                    self._mo2_pid_del_intento[self._intento_actual] = mo2_pid

                # ---- Ventana de spawn: localizar SkyrimSE.exe (D1) ----
                exito_temprano = False
                if mo2_pid is not None:
                    while time.monotonic() - t_spawn_mono < self._config.spawn_window_s:
                        if cancel.is_set():
                            await self._cerrar_y_verificar(
                                await self._ultima_atribucion(game_pid, mo2_pid, min_create_epoch), cancel
                            )
                            return self._resultado(
                                "cancelled", "Cancelado durante el arranque.", crash_count, await self._snapshot_cache()
                            )
                        if time.monotonic() - self._start_mono >= self._config.max_runtime_s:
                            # El deadline es GLOBAL: sin este check, ciclos de
                            # no-spawn repetidos lo excederían por hasta
                            # stall_threshold * spawn_window_s.
                            await self._cerrar_y_verificar(
                                await self._ultima_atribucion(game_pid, mo2_pid, min_create_epoch), cancel
                            )
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
                            await self._cerrar_y_verificar(
                                await self._ultima_atribucion(game_pid, mo2_pid, min_create_epoch), cancel
                            )
                            return self._resultado(
                                "cancelled", "Cancelado durante el arranque.", crash_count, await self._snapshot_cache()
                            )
                if exito_temprano:
                    # Última atribución (auditoría #287, D7b): el flag pudo
                    # borrarse ANTES de localizar al juego — sin esta búsqueda
                    # final, un juego reparentado quedaría vivo en modo normal.
                    return await self._salida_exito(
                        crash_count,
                        await self._ultima_atribucion(game_pid, mo2_pid, min_create_epoch),
                        cancel,
                    )

                if game_pid is None:
                    # Ventana agotada sin juego localizado.
                    await self._cerrar_y_verificar(None, cancel)
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
                    salida = await self._vigilar(game_pid, cancel, crash_count, t_spawn_mono=t_spawn_mono)
                    if salida is not None:
                        return salida
                    # H2/H5: el ciclo terminó por muerte OBSERVADA, pero antes de decidir un
                    # relanzamiento se registra la terminalidad del intento que acaba de cerrar:
                    # el guard de la próxima iteración no puede basarse en una suposición.
                    self._terminalidad = await self._verificar_terminalidad(game_pid, cancel)
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
                    return await self._salida_exito(crash_count, game_pid, cancel)
                if stall_streak >= self._config.stall_threshold:
                    await self._cerrar_y_verificar(game_pid, cancel)
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
                    await self._cerrar_y_verificar(game_pid, cancel)
                    return self._resultado("cancelled", "Cancelado entre relanzamientos.", crash_count, snap)
                await self._emit_progress("relaunching", crash_count)

            await self._cerrar_y_verificar(game_pid, cancel)
            return self._resultado(
                "max_restarts",
                f"Presupuesto de {self._config.max_restarts} relanzamientos agotado sin completar.",
                crash_count,
                await self._snapshot_cache(),
            )
        except BaseException:
            # Cancelación dura o bug: jamás dejar huérfanos. Best-effort para
            # no enmascarar la excepción original.
            #
            # H2/H5: acá NO se puede esperar. Un `await` en este camino re-lanzaría la cancelación y
            # enmascararía la excepción original, así que el cierre es SÍNCRONO: kill directo del
            # último proceso atribuido y UNA clasificación de terminalidad. Si no se puede afirmar,
            # queda `indeterminado` y se registra.
            # B1: identidad del intento ACTUAL, nunca un PID de otro intento.
            intento = self._intento_actual
            objetivo = game_pid if game_pid is not None else self._atribuciones.get(intento)
            if objetivo is not None:
                with contextlib.suppress(Exception):
                    self._kill_game_tree_sync(objetivo.pid, objetivo.create_time)
            with contextlib.suppress(Exception):
                if objetivo is not None:
                    self._terminalidad = self._clasificar_terminalidad(objetivo)
                else:
                    # B1/B2: sin atribución del intento actual NO se puede afirmar ausencia: el
                    # lanzamiento pudo estar en curso. `indeterminado` es lo único honesto.
                    self._lanzamiento = (
                        "solicitado_sin_atribucion" if self._spawn_solicitado.get(intento, False) else "no_intentado"
                    )
                    self._terminalidad = "indeterminado" if self._lanzamiento != "no_intentado" else "sin_proceso"
            if self._terminalidad != "terminado":
                logger.warning(
                    "Run abortado con terminalidad %s del PID %s: el caché pudo seguir escribiéndose.",
                    self._terminalidad,
                    objetivo.pid if objetivo is not None else None,
                )
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

    async def _vigilar(
        self, game_pid: _GamePid, cancel: asyncio.Event, crash_count: int, *, t_spawn_mono: float
    ) -> GrassCacheRunResult | None:
        """Poll de la vida del juego y de su PROGRESO. ``None`` = crash (el caller relanza).

        El watchdog de progreso (F-05) cubre el caso que el deadline global no cubre: un
        ``SkyrimSE.exe`` **vivo pero congelado**. NGIO detecta sus propios freezes a los 60 s, pero
        reacciona con ``RE::DebugMessageBox`` ANTES de abortar: si nadie cierra el diálogo, el
        proceso queda vivo con PID válido y el ritual quema hasta 12 h. Acá se mide progreso
        observable —conteo de nombres `.cgid`/`.dgid`/`.fail` y tamaño del flag de NGIO— y se corta
        de forma acotada: una sola pasada de kill, sin relanzar y conservando el caché parcial.
        """
        last_heartbeat = time.monotonic()
        self._ultimo_avance_mono = t_spawn_mono
        while True:
            if cancel.is_set():
                await self._cerrar_y_verificar(game_pid, cancel)
                return self._resultado(
                    "cancelled",
                    "Cancelado; el cache parcial se conserva.",
                    crash_count,
                    await self._snapshot_cache(),
                )
            if not await self._flag_exists():
                return await self._salida_exito(crash_count, game_pid, cancel)
            if time.monotonic() - self._start_mono >= self._config.max_runtime_s:
                await self._cerrar_y_verificar(game_pid, cancel)
                self._terminalidad = await self._verificar_terminalidad(game_pid, cancel)
                return self._resultado(
                    "timeout",
                    f"Deadline global de {self._config.max_runtime_s:.0f}s alcanzado; el cache parcial se conserva.",
                    crash_count,
                    await self._snapshot_cache(),
                )
            if await self._free_disk_bytes() < self._config.min_free_bytes:
                await self._cerrar_y_verificar(game_pid, cancel)
                return self._resultado(
                    "disk_full",
                    "Espacio en disco por debajo del umbral; liberar espacio y reanudar.",
                    crash_count,
                    await self._snapshot_cache(),
                )
            if not await self._pid_alive(game_pid.pid):
                return None  # crash: murió con el flag presente
            corte = await self._vigilar_progreso(game_pid, cancel, crash_count, t_spawn_mono=t_spawn_mono)
            if corte is not None:
                return corte
            if time.monotonic() - last_heartbeat >= self._config.heartbeat_interval_s:
                await self._emit_progress("scanning", crash_count)
                last_heartbeat = time.monotonic()
            # La cancelación que llegue durante el sleep la atiende el check ①
            # del próximo ciclo (el sleep despierta al instante con el event).
            await self._sleep_or_cancel(cancel, self._config.poll_interval_s)

    async def _vigilar_progreso(
        self, game_pid: _GamePid, cancel: asyncio.Event, crash_count: int, *, t_spawn_mono: float
    ) -> GrassCacheRunResult | None:
        """Muestrea progreso y devuelve un corte si el proceso vivo dejó de avanzar.

        ``None`` = sigue avanzando (o todavía no se puede juzgar). Las muestras son baratas —
        nombres de archivo, sin ``stat`` por archivo— y corren en un hilo: el event loop sigue libre
        para renovar los leases ``grass-cache``/``load-order`` (F2 de P0).
        """
        if self._config.stall_progress_s <= 0:
            return None
        ahora = time.monotonic()
        if ahora < self._proximo_probe_mono:
            return None
        self._proximo_probe_mono = ahora + self._config.progress_poll_interval_s
        # La gracia suspende la DECISIÓN de stall, no el registro de progreso (H3): una corrida que
        # sólo avanza durante los primeros minutos (RESUME sin `.cgid` nuevos) tiene que dejar
        # evidencia de avance.
        await self._muestrear_progreso(linea_base=self._muestra_previa is None)
        if ahora - t_spawn_mono < self._config.stall_grace_s:
            return None
        if self._ultimo_avance_mono is None:
            return None
        detenido = ahora - self._ultimo_avance_mono
        if detenido < self._config.stall_progress_s:
            return None
        self._progreso_detenido_s = round(detenido, 3)
        logger.warning(
            "Progreso detenido %.0fs con SkyrimSE.exe PID=%s vivo (última muestra: %s). "
            "Se corta el run como stalled: el caché parcial se conserva.",
            detenido,
            game_pid.pid,
            self._muestra_previa,
        )
        await self._matar_todo(game_pid)
        self._terminalidad = await self._verificar_terminalidad(game_pid, cancel)
        await self._emit_progress("finished", crash_count)
        return self._resultado(
            "stalled",
            f"Sin progreso observable durante {detenido:.0f}s con el proceso vivo: el juego se "
            "congeló (freeze o diálogo modal de NGIO). Se mató el proceso; el caché parcial se "
            "conserva y NO se relanza.",
            crash_count,
            await self._snapshot_cache(),
        )

    async def _muestrear_progreso(self, *, linea_base: bool) -> None:
        """Actualiza el latido de progreso. Un fallo de lectura NUNCA aborta el run."""
        try:
            muestra = await asyncio.to_thread(self._muestra_de_progreso_sync)
        except OSError:
            return
        if linea_base or self._muestra_previa is None:
            self._muestra_previa = muestra
            self._ultimo_avance_mono = time.monotonic()
            return
        if muestra != self._muestra_previa:
            self._muestra_previa = muestra
            self._ultimo_avance_mono = time.monotonic()
            self._progreso_observado = True

    def _muestra_de_progreso_sync(self) -> tuple[int, int, int, int]:
        """(nombres `.cgid`, nombres `.dgid`, nombres `.fail`, tamaño del flag).

        ``iterdir`` aparece acá porque el censo de ``tests/test_borrado_recursivo.py`` ya declara a
        este módulo (sólo lee; no borra ni copia árboles).
        """
        cgid = dgid = fail = 0
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            for archivo in self._config.overwrite_grass_dir.iterdir():
                sufijo = archivo.suffix.lower()
                if sufijo == ".cgid":
                    cgid += 1
                elif sufijo == ".dgid":
                    dgid += 1
                elif sufijo == ".fail":
                    fail += 1
        try:
            tamano = self._flag_path.stat().st_size
        except OSError:
            tamano = -1
        self._leer_avance_del_flag(tamano)
        return (cgid, dgid, fail, tamano)

    def _leer_avance_del_flag(self, tamano: int) -> None:
        """Lee SÓLO los bytes nuevos del flag y guarda los worldspaces que NGIO declaró.

        NGIO appendea una línea por celda y una por worldspace terminado (``ws_<EditorID>``), pero
        **sin** ``flush`` por línea: lo que se ve acá es un límite inferior del avance real. Por eso
        es evidencia y nunca requisito. La lectura incremental y el tope por muestra mantienen el
        costo acotado (decenas de miles de celdas ⇒ archivo de MB, no de GB).
        """
        if tamano <= 0 or tamano <= self._flag_offset:
            if tamano < self._flag_offset:
                self._flag_offset = 0  # el flag se reescribió: releer desde el principio
            return
        tope = min(tamano, self._flag_offset + _TOPE_LECTURA_FLAG)
        try:
            with self._flag_path.open("rb") as fh:
                fh.seek(self._flag_offset)
                crudo = fh.read(tope - self._flag_offset)
        except OSError:
            return
        self._flag_offset = tope
        for linea in crudo.splitlines():
            texto = linea.strip()
            if texto.startswith(b"ws_"):
                with contextlib.suppress(UnicodeDecodeError):
                    self._ws_declarados.add(texto[3:].decode("utf-8"))

    async def _salida_exito(
        self, crash_count: int, game_pid: _GamePid | None, cancel: asyncio.Event
    ) -> GrassCacheRunResult:
        """Fin del ritual: el flag desapareció. **Terminación NO es certificación** (F-02).

        Antes bastaba «flag ausente + algún `.cgid` + bytes > 0», y eso convertía un caché
        PREEXISTENTE en salida nueva. Ahora se reúne la evidencia (procedencia contra el baseline,
        `.fail` por contenido, worldspaces con artefactos, progreso observado y terminalidad) y la
        decide ``grass_cache_verdict.evaluar_veredicto``: una sola política.
        """
        await self._cerrar_y_verificar(game_pid, cancel)
        snap = await self._snapshot_cache()
        await self._emit_progress("finished", crash_count)
        evidencia = await esperar_terminar(
            asyncio.to_thread(self._reunir_evidencia_de_egreso),
            descripcion="evidencia_egreso",
        )
        self._evidencia = evidencia
        veredicto = evaluar_veredicto(evidencia)
        return self._resultado(
            "completed",
            "",
            crash_count,
            snap,
            success=veredicto.exito_operacional,
            veredicto=veredicto,
            evidencia={**self._evidencia_a_dict(evidencia), **veredicto.a_dict()},
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

    async def _autorizar_lanzamiento(self, *, intento: int, plan: PlanArranque) -> _CorteDeLanzamiento | None:
        """ÚNICO contrato de autorización, justo antes de CADA ``launch_game()``.

        ``None`` = lanzamiento autorizado; un corte = no se lanza (fail-closed).

        **Por qué un solo punto.** Antes había comprobaciones repartidas —el plan
        en ``preparar_arranque``, el flag en ``aplicar_plan``, el progreso en el
        viejo guard de relanzamiento—, y cada una veía una foto distinta del
        disco. Entre la última de ellas y el ``launch_game()`` había una ventana
        real: el callback ``launching`` corre exactamente ahí. Este método se
        ejecuta inmediatamente antes del spawn y contesta por los seis
        invariantes del contrato, con las primitivas del módulo de seguridad
        (sin un segundo motor de planificación):

        1. ``new`` en el primer lanzamiento: flag esperado, inventario físico
           todavía vacío y autorización USVFS vigente;
        2. ``rebuild`` en el primer lanzamiento: flag esperado y respaldo todavía
           íntegro **y** correspondiente al caché actual (si el caché cambió
           después del respaldo, no se lanza);
        3. ``resume`` en el primer lanzamiento: progreso válido y concordante con
           el plan aplicado, byte a byte;
        4. todo relanzamiento: progreso válido, sea cual sea el modo y el
           inventario físico (un caché físico vacío **no** acredita nada: la VFS
           puede superponer ``.cgid`` de mods en ``Data/Grass``);
        5. todos los lanzamientos: ausencia del flag ALTERNATIVO, incluso cuando
           el primario tiene progreso (NGIO lo abre si el primario falta);
        6. la cancelación y el deadline se vuelven a comprobar **después** de
           esta inspección (que hace E/S) y antes del spawn — ver el bucle.

        La E/S corre en un hilo drenado: si la cancelación llega mientras se
        inspecciona, ``esperar_terminar`` espera al hilo y recién entonces
        propaga, así que el veredicto nunca se pierde a mitad de camino.
        """
        return await esperar_terminar(
            asyncio.to_thread(self._inspeccionar_antes_de_lanzar, intento=intento, plan=plan),
            descripcion="autorizar_lanzamiento",
        )

    def _inspeccionar_antes_de_lanzar(self, *, intento: int, plan: PlanArranque) -> _CorteDeLanzamiento | None:
        """Inspección síncrona (en hilo) de la matriz de autorización."""
        try:
            # Invariante 5: primero el alternativo, para todos los lanzamientos.
            exigir_sin_flag_alternativo(self._config.game_path)
        except GrassCacheSafetyError as exc:
            return _CorteDeLanzamiento("flag_alternativo_presente", str(exc))
        inventario = inventariar_cache(
            grass_dir=self._config.overwrite_grass_dir,
            flag_path=self._flag_path,
        )
        if intento:
            return self._corte_del_relanzamiento(inventario)
        return self._corte_del_primer_lanzamiento(plan, inventario)

    def _corte_del_primer_lanzamiento(
        self, plan: PlanArranque, inventario: InventarioCache
    ) -> _CorteDeLanzamiento | None:
        """Invariantes 1-3: el estado tiene que seguir siendo el que el plan autorizó."""
        try:
            # El flag que el plan aplicó (vacío en new/rebuild, progreso exacto en
            # resume) sigue siendo ese: si un tercero lo tocó, el plan ya no rige.
            exigir_flag_conservado(self._flag_path, plan.contenido)
            if plan.modo == "resume":
                if inventario.flag_progreso is None:
                    return _CorteDeLanzamiento(
                        "cache_cambio_despues_del_plan",
                        "El flag de precache dejó de tener progreso entre el plan y el lanzamiento: "
                        "NGIO empezaría de cero y borraría el caché. No se lanza el juego.",
                    )
            else:
                exigir_cobertura_usvfs(self._config.ack_usvfs_no_verificado)
                if plan.modo == "new":
                    if inventario.tiene_cache:
                        return _CorteDeLanzamiento(
                            "cache_cambio_despues_del_plan",
                            f"Aparecieron {inventario.archivos_cache} archivo(s) en el caché físico después "
                            "de que NEW autorizara el arranque con el caché vacío: lanzar ahora dejaría que "
                            "NGIO borre contenido que el plan no evaluó. No se lanza y no se toca el caché.",
                        )
                elif plan.respaldo is None:
                    if inventario.tiene_cache:
                        return _CorteDeLanzamiento(
                            "cache_cambio_despues_del_plan",
                            f"REBUILD se autorizó con el caché vacío y ahora hay {inventario.archivos_cache} "
                            "archivo(s), sin respaldo: no hay copia verificada de lo que NGIO borraría. "
                            "No se lanza y no se toca el caché.",
                        )
                else:
                    # Vuelve a leer origen y destino: si el caché cambió después del
                    # respaldo (o el respaldo fue alterado), REBUILD no arranca.
                    verificar_respaldo_para_borrado(
                        grass_dir=self._config.overwrite_grass_dir,
                        respaldo=plan.respaldo,
                    )
        except GrassCacheSafetyError as exc:
            return _CorteDeLanzamiento(
                "cache_cambio_despues_del_plan",
                f"El estado cambió después de que el plan autorizara el lanzamiento [{exc.codigo}]: {exc}",
            )
        return None

    def _corte_del_relanzamiento(self, inventario: InventarioCache) -> _CorteDeLanzamiento | None:
        """Invariante 4: sin progreso válido no hay relanzamiento, en ningún modo.

        Que el archivo exista no es lo mismo que NGIO lo lea como progreso: con el
        flag sin progreso y ``.cgid/.dgid/.fail`` alcanzables, el ``remove()`` de
        ``GidFileCache.cpp::Init()`` (1.0.13) borra el caché en el próximo
        arranque. El caché físico vacío tampoco habilita nada (§17.2).
        """
        if inventario.flag_progreso is not None:
            return None  # NGIO reanuda: el relanzamiento continúa el trabajo
        if self._config.mode == "resume":
            motivo = (
                "El flag de precache quedó SIN progreso: `resume` exige progreso siempre, así que no "
                f"se relanza. El caché ({inventario.archivos_cache} archivo(s) en el caché físico) no se "
                "borra ni se restaura; revisá qué vació el flag y, si hay que regenerar, usá "
                "mode='rebuild' con respaldo verificado."
            )
        else:
            motivo = (
                f"El flag de precache quedó sin progreso en modo '{self._config.mode}': no se autoriza "
                f"otro lanzamiento automático. El caché físico tiene {inventario.archivos_cache} archivo(s) "
                "y no se borra ni se restaura. Si NGIO murió antes de registrar progreso, el ritual se "
                "detiene acá: decidí resume/rebuild con la evidencia a la vista."
            )
        return _CorteDeLanzamiento("flag_sin_progreso", motivo)

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
        pid = await asyncio.to_thread(self._scan_for_game_sync, mo2_pid, min_create_epoch)
        if pid is not None:
            self._ultimo_atribuido = pid
            self._atribuciones[self._intento_actual] = pid
        return pid

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
                # H2/H5: si la terminalidad no está demostrada, el caché puede seguir escribiéndose
                # y NO se archiva esa foto como evidencia de reanudación. El flag igual se borra.
                archivar=self._terminalidad in ("terminado", "sin_proceso"),
            )
            # Se registra ACÁ, no en el llamador: si la cancelación llega
            # mientras el hilo neutraliza, `esperar_terminar` espera a que
            # termine y recién entonces propaga, así que un
            # `self._neutralizacion = await …` del llamador NUNCA correría y un
            # flag que no se pudo borrar se perdería en silencio.
            # El flag ALTERNATIVO no es nuestro: no se borra. Pero mientras exista,
            # NGIO puede abrirlo como flag de progreso si el primario falta, así
            # que la limpieza NO es completa y el resultado tiene que declararlo.
            alternativo = ruta_del_flag_alternativo(self._config.game_path)
            try:
                residual = path_present(alternativo)
            except OSError:
                residual = True  # no se pudo inspeccionar: fail-closed
            if residual:
                resultado = dataclasses.replace(
                    resultado,
                    alternativo_residual=True,
                    errores=(
                        *resultado.errores,
                        f"Queda el flag ALTERNATIVO {alternativo} en disco: no lo escribió este run, así "
                        "que no se borra, pero NGIO lo abre como flag de progreso cuando falta el primario. "
                        "La limpieza NO es completa: resolvelo con el juego cerrado.",
                    ),
                )
            self._neutralizacion = resultado
            if resultado.errores or not resultado.limpio:
                self._limpieza_logueada = True
                logger.warning(
                    "Neutralización del flag incompleta (borrado=%s, progreso_perdido=%s): %s",
                    resultado.borrado,
                    resultado.progreso_perdido,
                    "; ".join(resultado.errores) or "sin detalle",
                )
            return resultado

        return await esperar_terminar(_archivar_y_registrar(), descripcion="neutralizar_flag")

    # ------------------------------------------------------------------
    # P1: evidencia de egreso, terminalidad y veredicto
    # ------------------------------------------------------------------

    async def _verificar_terminalidad(self, game_pid: _GamePid | None, cancel: asyncio.Event) -> Terminalidad:
        """Terminalidad del INTENTO ACTUAL, sin colapsar «no hay proceso» en «terminado» (H2/H5).

        Seis estados observables, vía ``terminalidad`` + ``lanzamiento``:

        | situación                                   | ``lanzamiento``            | ``terminalidad`` |
        |---------------------------------------------|----------------------------|------------------|
        | spawn no intentado                          | ``no_intentado``           | ``sin_proceso``  |
        | spawn rechazado / ausencia DEMOSTRADA       | ``rechazado``              | ``sin_proceso``  |
        | spawn solicitado con atribución desconocida | ``solicitado_sin_atribucion`` | ``indeterminado`` |
        | proceso atribuido y muerto                  | ``atribuido``              | ``terminado``    |
        | proceso atribuido vivo                      | ``atribuido``              | ``vivo``         |
        | no se puede clasificar                      | ``atribuido``              | ``indeterminado`` |

        **Un PID de un intento anterior NO prueba la terminalidad del actual**: la atribución es por
        intento. Si el actual no atribuyó, se busca EXHAUSTIVAMENTE un proceso nacido en su ventana;
        encontrarlo lo adopta, no encontrarlo (con la búsqueda funcionando) demuestra ausencia, y una
        búsqueda que falla deja ``indeterminado`` — nunca ``sin_proceso``.
        """
        intento = self._intento_actual
        objetivo = game_pid if game_pid is not None else self._atribuciones.get(intento)
        if objetivo is None and game_pid is not None:
            self._atribuciones[intento] = game_pid
            objetivo = game_pid
        if objetivo is None:
            if not self._spawn_solicitado.get(intento, False):
                self._lanzamiento = "no_intentado"
                return "sin_proceso"
            resultado, encontrado = await asyncio.to_thread(self._buscar_proceso_del_intento, intento)
            if resultado == "encontrado" and encontrado is not None:
                self._atribuciones[intento] = encontrado
                self._lanzamiento = "atribuido"
                return await self._esperar_muerte(encontrado, cancel)
            if resultado == "ausente":
                self._lanzamiento = "rechazado"
                return "sin_proceso"
            self._lanzamiento = "solicitado_sin_atribucion"
            return "indeterminado"
        self._lanzamiento = "atribuido"
        return await self._esperar_muerte(objetivo, cancel)

    async def _esperar_muerte(self, objetivo: _GamePid, cancel: asyncio.Event) -> Terminalidad:
        """Espera ACOTADA a que el proceso atribuido muera (RD-10). La corta la cancelación."""
        limite = time.monotonic() + self._config.terminality_wait_s
        while True:
            estado = await asyncio.to_thread(self._clasificar_terminalidad, objetivo)
            if estado != "vivo":
                return estado
            if time.monotonic() >= limite:
                return "vivo"
            if await self._sleep_or_cancel(cancel, _ESPERA_TERMINALIDAD_S):
                return "vivo"

    def _buscar_proceso_del_intento(self, intento: int) -> tuple[str, _GamePid | None]:
        """Busca un proceso del juego nacido en la ventana del intento.

        Devuelve ``("encontrado", pid)``, ``("ausente", None)`` cuando la búsqueda corrió completa y
        no hay ninguno, o ``("fallo", None)`` cuando no se pudo inspeccionar el sistema: eso es
        ``indeterminado``, jamás una ausencia.
        """
        minimo = self._epoch_del_intento.get(intento, 0.0)
        nombres = {nombre.lower() for nombre in self._config.game_exe_names}
        game_dir = self._config.game_path.resolve()
        encontrados: list[_GamePid] = []
        incompleta = False
        try:
            for proc in psutil.process_iter(["name", "create_time", "exe"]):
                try:
                    nombre = proc.name().lower()
                except psutil.AccessDenied:
                    # B2: un candidato que no se puede identificar NO es ausencia.
                    incompleta = True
                    continue
                except psutil.Error:
                    incompleta = True
                    continue
                if nombre not in nombres:
                    continue
                # Es un SkyrimSE.exe: si no se puede leer su identidad, la evidencia es incompleta.
                try:
                    creado = proc.create_time()
                    exe = proc.exe()
                except psutil.Error:
                    incompleta = True
                    continue
                if creado < minimo:
                    continue  # de un intento anterior: no cuenta para éste
                if not exe:
                    incompleta = True
                    continue
                try:
                    if pathlib.Path(exe).resolve().parent != game_dir:
                        continue
                except OSError:
                    incompleta = True
                    continue
                encontrados.append(_GamePid(pid=int(proc.pid), create_time=creado))
        except psutil.Error:
            logger.warning("No se pudo inspeccionar el sistema para verificar el lanzamiento.", exc_info=True)
            return ("incompleto", None)
        if encontrados:
            return ("encontrado", encontrados[0])
        if incompleta:
            return ("incompleto", None)
        # B2: una búsqueda vacía no demuestra ausencia si el lanzador sigue vivo (aún puede crear
        # procesos). Sin esa prueba, `incompleto` => indeterminado.
        mo2_pid = self._mo2_pid_del_intento.get(intento)
        if mo2_pid is None:
            # F3: si `launch_game` timeouteó o falló sin devolver PID, NO se sabe si el lanzador
            # llegó a crearse. Eso no demuestra ausencia.
            return ("incompleto", None)
        try:
            if psutil.pid_exists(mo2_pid):
                return ("incompleto", None)
        except psutil.Error:
            return ("incompleto", None)
        return ("ausente", None)

    def _pid_del_intento(self, intento: int) -> int | None:
        atribuido = self._atribuciones.get(intento)
        return atribuido.pid if atribuido is not None else None

    def _create_time_del_intento(self, intento: int) -> float | None:
        atribuido = self._atribuciones.get(intento)
        return atribuido.create_time if atribuido is not None else None

    async def _cerrar_y_verificar(self, game_pid: _GamePid | None, cancel: asyncio.Event) -> None:
        """ÚNICO cierre de una salida terminal: pide el kill y DEMUESTRA (o declara) terminalidad.

        Una solicitud de kill NO es prueba de terminación (H2/H5). Todo camino que termina el
        ritual —cancelación, disco, deadline, stall, spawn fallido, agotamiento de reintentos,
        cortes de contrato— pasa por acá antes de construir el resultado.
        """
        await self._matar_todo(game_pid if game_pid is not None else self._atribuciones.get(self._intento_actual))
        self._terminalidad = await self._verificar_terminalidad(game_pid, cancel)
        try:
            self._conteo_artefactos = await asyncio.to_thread(self._conteo_rapido_sync)
        except OSError:
            # B5: un inventario ilegible NO es un caché vacío.
            self._conteo_artefactos = None
            self._conteo_ilegible = True
            logger.warning("No se pudo contar el caché al cerrar: la evidencia queda incompleta.", exc_info=True)

    def _conteo_rapido_sync(self) -> CacheObservado:
        """Conteo barato de TODOS los artefactos reconocidos, para la evidencia de salidas parciales.

        ``_snapshot_cache`` cuenta sólo ``.cgid``; un caché parcial compuesto por ``.dgid``/``.fail``
        no es «vacío» (H-C). Sólo se hace `stat` de los `.cgid` (hace falta el tamaño para decidir el
        fallo de zero-bounds); el resto es conteo de nombres.
        """
        cgid = cgid_bytes = dgid = fail = 0
        try:
            candidatos = list(self._config.overwrite_grass_dir.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return CacheObservado()
        for archivo in candidatos:
            sufijo = archivo.suffix.lower()
            if sufijo not in EXTENSIONES_CACHE:
                continue
            info = archivo.stat()  # si falla, el cierre marca la evidencia como ilegible
            if not stat.S_ISREG(info.st_mode):
                continue  # B5: directorios y enlaces no son artefactos
            if sufijo == ".cgid":
                cgid += 1
                cgid_bytes += int(info.st_size)
            elif sufijo == ".dgid":
                dgid += 1
            else:
                fail += 1
        return CacheObservado(
            cgid_count=cgid,
            cgid_bytes=cgid_bytes,
            dgid_count=dgid,
            fail_count=fail,
            archivos=cgid + dgid + fail,
        )

    def _clasificar_terminalidad(self, game_pid: _GamePid) -> Terminalidad:
        """``terminado`` / ``vivo`` / ``indeterminado`` sin matar ni tocar a nadie.

        ``terminado`` cubre dos casos: el proceso ya no existe, o el PID ahora es OTRO proceso (el
        SO lo recicló). ``indeterminado`` es fail-closed: no poder leer nombre/exe/create_time no
        autoriza a afirmar que terminó.
        """
        try:
            root = psutil.Process(game_pid.pid)
        except psutil.NoSuchProcess:
            return "terminado"
        except psutil.Error:
            return "indeterminado"
        try:
            if root.name().lower() not in {nombre.lower() for nombre in self._config.game_exe_names}:
                return "terminado"
            if game_pid.create_time is not None and root.create_time() != game_pid.create_time:
                return "terminado"  # otro SkyrimSE.exe: el PID se recicló
            exe = root.exe()
        except psutil.Error:
            return "indeterminado"
        if not exe:
            return "indeterminado"
        try:
            if pathlib.Path(exe).resolve().parent != self._config.game_path.resolve():
                return "terminado"
        except OSError:
            return "indeterminado"
        return "vivo"

    def _capturar_baseline_sync(self) -> tuple[frozenset[tuple[str, int]], dict[str, bytes]]:
        """(nombre, tamaño) y contadores `.fail` del caché ANTES del primer lanzamiento.

        Es la medida de «esto ya estaba»: un archivo final que coincide con una entrada del baseline
        no lo produjo esta corrida. La atribución de `.fail` usa **contenido**, nunca la fecha.

        **Fail-closed (H4).** Cualquier error de listado, `stat` o lectura **propaga**: degradarlo a
        «caché vacío» convertiría todo lo heredado en «nuevo» y produciría un falso `completed_verified`.
        """
        entradas: set[tuple[str, int]] = set()
        fallos: dict[str, bytes] = {}
        try:
            candidatos = list(self._config.overwrite_grass_dir.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            # Caché INEXISTENTE: es un estado legítimo (primer run), no un error de lectura. Sólo
            # esta forma de ausencia se degrada a baseline vacío; cualquier otro OSError propaga.
            return frozenset(), {}
        for archivo in candidatos:
            sufijo = archivo.suffix.lower()
            if sufijo not in EXTENSIONES_CACHE:
                continue
            info = archivo.stat()
            entradas.add((archivo.name, int(info.st_size)))
            if sufijo == ".fail":
                fallos[archivo.name] = archivo.read_bytes()[:_TOPE_FALLO_BYTES]
        return frozenset(entradas), fallos

    def _inventario_de_egreso_sync(
        self,
    ) -> tuple[CacheObservado, dict[str, bytes], tuple[str, ...], int, int, int]:
        """Una sola pasada al salir: conteos, `.fail` por contenido y worldspaces con artefactos.

        Un `.fail` que existe pero no se puede leer **no desaparece** (H1): se cuenta en
        ``fallos_no_evaluables`` y la política no certifica. Un fallo de `stat` o de listado
        invalida la evidencia entera (el caller lo declara ilegible).
        """
        cgid = cgid_bytes = dgid = fail = 0
        no_evaluables = 0
        entradas: set[tuple[str, int]] = set()
        fallos: dict[str, bytes] = {}
        worldspaces: set[str] = set()
        ilegibles: set[str] = set()
        try:
            candidatos = list(self._config.overwrite_grass_dir.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            candidatos = []
        for archivo in candidatos:
            sufijo = archivo.suffix.lower()
            if sufijo not in EXTENSIONES_CACHE:
                continue
            info = archivo.stat()
            if not stat.S_ISREG(info.st_mode):
                continue  # B5: un directorio o enlace con nombre de artefacto no es caché
            entradas.add((archivo.name, int(info.st_size)))
            if sufijo == ".cgid":
                cgid += 1
                cgid_bytes += int(info.st_size)
            elif sufijo == ".dgid":
                dgid += 1
            else:
                fail += 1
                try:
                    fallos[archivo.name] = archivo.read_bytes()[:_TOPE_FALLO_BYTES]
                except OSError:
                    # No se transforma un fallo de lectura en ausencia de fallo (H1/B4).
                    no_evaluables += 1
                    ilegibles.add(archivo.name)
                    logger.warning("No se pudo leer %s: queda como fallo NO EVALUABLE.", archivo)
            worldspace = self._worldspace_de_archivo(archivo.name)
            if worldspace is not None:
                worldspaces.add(worldspace)
        inventario = CacheObservado(
            cgid_count=cgid,
            cgid_bytes=cgid_bytes,
            dgid_count=dgid,
            fail_count=fail,
            archivos=len(entradas),
        )
        heredados = len(entradas & self._baseline_entradas)
        nuevos = len(entradas - self._baseline_entradas)
        self._fallos_ilegibles = frozenset(ilegibles)
        return inventario, fallos, tuple(sorted(worldspaces)), heredados, nuevos, no_evaluables

    @staticmethod
    def _worldspace_de_archivo(nombre: str) -> str | None:
        """``Tamrielx0007y-003.cgid`` → ``Tamriel``. ``None`` si no sigue el patrón de NGIO."""
        tallo = nombre.rsplit(".", 1)[0]
        coincidencia = _WS_DE_ARTEFACTO.match(tallo)
        return coincidencia.group("ws") if coincidencia is not None else None

    def _reunir_evidencia_de_egreso(self) -> EvidenciaVeredicto:
        """Foto completa para la política. Un fallo de lectura se declara, no se disfraza."""
        try:
            inventario, fallos, worldspaces, heredados, nuevos, no_evaluables = self._inventario_de_egreso_sync()
        except OSError:
            logger.warning("No se pudo leer el caché al salir: el veredicto queda sin evidencia.", exc_info=True)
            return EvidenciaVeredicto(
                outcome="completed",
                modo=self._config.mode,
                terminalidad=self._terminalidad,
                limpieza_limpia=None,
                evidencia_ilegible=True,
            )
        return EvidenciaVeredicto(
            outcome="completed",
            modo=self._config.mode,
            terminalidad=self._terminalidad,
            limpieza_limpia=None,  # el `finally` todavía no neutralizó el flag (RD-11)
            inventario_final=inventario,
            archivos_heredados=heredados,
            archivos_nuevos=nuevos,
            fallos_finales=fallos,
            fallos_previos=dict(self._fallos_previos),
            worldspaces_solicitados=tuple(self._config.worldspaces),
            worldspaces_con_artefactos=worldspaces,
            worldspaces_declarados_por_ngio=tuple(sorted(self._ws_declarados)),
            progreso_observado=self._progreso_observado,
            fallos_no_evaluables=no_evaluables,
            fallos_ilegibles=self._fallos_ilegibles,
            lanzamiento=self._lanzamiento,
        )

    def _evidencia_a_dict(self, evidencia: EvidenciaVeredicto) -> dict[str, Any]:
        """Evidencia serializable: identidad de la corrida + lo observado (F-12/C1)."""
        atribuibles = sum(
            1
            for nombre, contenido in evidencia.fallos_finales.items()
            if evidencia.fallos_previos.get(nombre) != contenido
        )
        return {
            "mode": self._config.mode,
            "perfil_de_lanzamiento": self._config.profile,
            "perfil_de_origen": self._config.perfil_de_progreso,
            "game_path": str(self._config.game_path),
            "overwrite_grass_dir": str(self._config.overwrite_grass_dir),
            "backup_dir": self._backup_dir,
            "worldspaces_solicitados": list(evidencia.worldspaces_solicitados),
            "worldspaces_con_artefactos": list(evidencia.worldspaces_con_artefactos),
            "worldspaces_declarados_por_ngio": list(evidencia.worldspaces_declarados_por_ngio),
            "inventario_final": evidencia.inventario_final.a_dict() if evidencia.inventario_final else None,
            "archivos_heredados": evidencia.archivos_heredados,
            "archivos_nuevos": evidencia.archivos_nuevos,
            "fallos_de_celda_total": (
                evidencia.inventario_final.fail_count if evidencia.inventario_final else len(evidencia.fallos_finales)
            ),
            "fallos_de_celda_atribuibles": atribuibles,
            "fallos_de_celda_historicos": len(evidencia.fallos_finales) - atribuibles,
            # H1: `.fail` que existen y no se pudieron leer. Nunca se reportan como «ninguno».
            "fallos_de_celda_no_evaluables": max(
                evidencia.fallos_no_evaluables,
                max(
                    0,
                    (evidencia.inventario_final.fail_count if evidencia.inventario_final else 0)
                    - len(evidencia.fallos_finales),
                ),
            ),
            "baseline_ilegible": evidencia.baseline_ilegible,
            "progreso_observado": evidencia.progreso_observado,
            # H2/H5: «no lancé», «lancé y no sé qué pasó» y «lancé y murió» no se confunden.
            "lanzamiento": evidencia.lanzamiento,
            "terminalidad_verificada": evidencia.terminalidad in ("terminado", "sin_proceso"),
            "progreso_detenido_s": self._progreso_detenido_s,
            "terminalidad": evidencia.terminalidad,
            "cobertura_usvfs": "no_verificada",
            "completitud_de_celdas": "no_verificada",
        }

    def _resultado(
        self,
        outcome: GrassCacheOutcome,
        message: str,
        crash_count: int,
        snap: tuple[int, int],
        *,
        success: bool | None = None,
        veredicto: VeredictoCache | None = None,
        evidencia: dict[str, Any] | None = None,
    ) -> GrassCacheRunResult:
        """Construye el resultado. El veredicto NUNCA se inventa acá: sale de la política.

        Los caminos que no reúnen evidencia completa (timeout, cancelación, cortes de contrato)
        igual pasan por la política, con la evidencia mínima que tienen. ``success`` sale de
        ``exito_operacional`` (D1): sin evidencia de finalización legítima no hay éxito.
        """
        if veredicto is None:
            ligera = self._evidencia_ligera(outcome, snap)
            veredicto = evaluar_veredicto(ligera)
            if evidencia is None:
                # La evidencia viaja SIEMPRE (F-12), también en los cortes: progreso, terminalidad
                # e identidad son lo que explica el desenlace.
                evidencia = self._evidencia_a_dict(ligera)
        return GrassCacheRunResult(
            success=veredicto.exito_operacional if success is None else success,
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
            verdict=veredicto.verdict,
            certificado=veredicto.certificado,
            verdict_razones=veredicto.razones,
            alcance_certificado=veredicto.alcance_certificado,
            evidencia={**(evidencia or {}), **veredicto.a_dict()},
        )

    def _evidencia_ligera(self, outcome: GrassCacheOutcome, snap: tuple[int, int]) -> EvidenciaVeredicto:
        """Evidencia mínima para los caminos que no pasan por ``_salida_exito``.

        H-C: usa el conteo de TODOS los artefactos (``.cgid`` + ``.dgid`` + ``.fail``) cuando el
        cierre lo tomó. ``snap`` sólo cuenta ``.cgid``: un caché parcial compuesto por ``.dgid`` o
        ``.fail`` no es «vacío» y no puede reportarse como tal.
        """
        conteo = self._conteo_artefactos or CacheObservado(
            cgid_count=snap[0],
            cgid_bytes=snap[1],
            archivos=snap[0],
        )
        return EvidenciaVeredicto(
            outcome=outcome,
            modo=self._config.mode,
            terminalidad=self._terminalidad,
            limpieza_limpia=None,
            inventario_final=conteo,
            progreso_observado=self._progreso_observado,
            worldspaces_solicitados=tuple(self._config.worldspaces),
            evidencia_ilegible=self._conteo_ilegible,
            lanzamiento=self._lanzamiento,
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

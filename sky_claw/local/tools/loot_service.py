"""LootSortingService — LOOT load-order sorting under the distributed lock.

Audit #190 fast-follow. LOOT ``--sort`` rewrites the shared load order
(``plugins.txt`` / ``loadorder.txt``), exactly the serializable state the other
mutating runners (xEdit, Synthesis, DynDOLOD) already guard with
:class:`SnapshotTransactionLock`. The real-execution path previously called the
deprecated, lock-free ``ModdingToolsAgent.run_loot``; this service closes that
gap so a real sort serializes against:

* another concurrent LOOT sort, and
* the dry-run preview chain (which snapshots and force-reverts the same load
  order) — both share :data:`LOAD_ORDER_RESOURCE_ID`.

**Snapshot rollback (T-06):** los ``target_files`` se resuelven con
:class:`LoadOrderFileResolver` — la unión de ``plugins.txt``/``loadorder.txt``
existentes en LOCALAPPDATA (LOOT corre fuera del VFS con ``--game-path``), el
profile de MO2 y un override explícito. Un sort que lanza (timeout) o sale con
error restaura el snapshot; si no se encuentra ningún candidato (entorno no
configurado), el sort se rechaza tras la corrida: sin targets observables no
hay evidencia para atribuir ni verificar el éxito (fail-closed — review
adversarial #495; el "serialización-sola con warning" previo al T-06 reportaba
éxito sin haber podido observar nada).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.local.loot.cli import (
    LOOTConfig,
    LOOTNotFoundError,
    LOOTRunner,
    LOOTTimeoutError,
)
from sky_claw.local.mo2.load_order import LoadOrderFileResolver, LoadOrderPaths

if TYPE_CHECKING:
    from sky_claw.app.core.models import LootExecutionParams
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.db.journal import OperationJournal
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager
    from sky_claw.app.security.path_validator import PathValidator
    from sky_claw.local.loot.parser import LOOTResult
    from sky_claw.local.mo2.brokered_loot import VfsBrokerProtocol
    from sky_claw.local.mo2.vfs_attestation import VfsAttestationChallenge
    from sky_claw.local.validators.preflight import (
        PermissionsCheck,
        PreflightReport,
        PreflightService,
    )
    from sky_claw.local.validators.write_permissions import WriteAccessReport

logger = logging.getLogger(__name__)


class LootRunnerProtocol(Protocol):
    async def sort(self, *, update_masterlist: bool = False) -> LOOTResult: ...


#: Shared lock resource id for the Skyrim load order (``plugins.txt`` /
#: ``loadorder.txt``). Used by this service AND the dry-run preview chain so a
#: real sort and a preview serialize on the load order instead of racing.
LOAD_ORDER_RESOURCE_ID = "load-order"

#: Default LOOT timeout in seconds. Preserves the prior ``run_loot`` allowance
#: (120s) rather than ``LOOTRunner``'s 60s default, so a slow masterlist update
#: or a large load order completing between 60 and 120s is not falsely timed out.
_DEFAULT_LOOT_TIMEOUT_SECONDS = 120

#: Prioridad al elegir el archivo cuyo orden refleja el load order: loadorder.txt
#: tiene el orden completo; plugins.txt lista los activos con marca ``*``.
_LOAD_ORDER_FILE_PRIORITY = ("loadorder.txt", "plugins.txt")


def _is_per_profile_runner(runner: object) -> bool:
    """True si *runner* declara el factory ``for_profile`` (runner VFS-aware).

    Solo ``BrokeredLootRunner``/``VfsRequiredLootRunner`` lo implementan, así que
    su presencia es lo único que certifica que el sort va a correr DENTRO de la
    USVFS. Se exige el factory también en la CLASE: ``MagicMock`` fabrica
    atributos arbitrarios, y mirar solo la instancia confundiría un mock (o un
    runner legacy) con esta extensión.

    **Fuente única de esa detección.** La consultan los dos lugares que tienen
    que coincidir:

    * :meth:`LootSortingService._ensure_loot_runner` — qué runner construir.
    * :meth:`LootSortingService._routes_through_physical_data` — si el gate de
      visibilidad (U-01) aplica.

    Si divergieran, el preflight opinaría sobre un modo de lanzamiento distinto
    del que realmente va a correr: o un ROJO falso que bloquea un sort correcto
    bajo USVFS, o el falso verde de U-01 reabierto en standalone. Tenerlo una
    sola vez es lo que hace imposible ese desfasaje (clase de defecto #1 del
    repo — ver ``AGENTS.md``).
    """
    return callable(getattr(type(runner), "for_profile", None)) and callable(getattr(runner, "for_profile", None))


# ---------------------------------------------------------------------------
# API pública de la detección de runner por perfil
# ---------------------------------------------------------------------------
# El tercer consumidor de esta detección vive fuera del módulo: la rama SIN lock
# de `run_loot_sort` (agente LLM) también tiene que rebindear el runner al perfil
# pedido antes de ordenar. Consume el alias en vez del helper privado —mismo
# criterio que `bajo_lock_de_instalacion` / `install_lock_resource_id` en
# `tools_installer` (T-31)—: un rename interno rompe el import time del paquete,
# no la corrida real. Si esta detección se duplicara allá, sería exactamente el
# desfasaje que el docstring de arriba existe para hacer imposible.
es_runner_por_perfil = _is_per_profile_runner


def _primary_load_order_file(paths: list[pathlib.Path]) -> pathlib.Path | None:
    """Elige el archivo de load order que mejor refleja el orden de plugins.

    ``loadorder.txt`` primero (orden completo), luego ``plugins.txt``, y como
    último recurso el primer candidato. ``None`` si no hay ninguno.
    """
    for preferido in _LOAD_ORDER_FILE_PRIORITY:
        for path in paths:
            if path.name.lower() == preferido:
                return path
    return paths[0] if paths else None


def _read_plugin_order(path: pathlib.Path | None) -> list[str]:
    """Lee el orden de plugins de un plugins.txt/loadorder.txt (best-effort).

    Ignora líneas vacías y comentarios (``#``) y quita la marca de activo
    (``*``), de modo que los nombres queden comparables con
    ``LOOTResult.sorted_plugins``. Devuelve ``[]`` ante cualquier problema de
    lectura/decodificación — el informe simplemente no llevará diff de orden.
    """
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return []
    plugins: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        plugins.append(line.lstrip("*").strip())
    return plugins


@dataclass(frozen=True, slots=True)
class _EstadoDeArchivo:
    """Evidencia física de un target: presente (con metadata), ausente o ilegible.

    La distinción importa para el gate de atribución: "ausente" es un estado
    observable; "ilegible" (``stat`` falló por permisos/IO, no por ausencia) es
    NO observabilidad — indistinguible en runtime de un archivo presente cuya
    metadata no se pudo leer. Mapear "ilegible" a "ausente" (o a "cambió") es
    un falso verde: el gate nunca deduce mutación de un ``stat`` fallido
    (review adversarial #495 P1).
    """

    tipo: str  # "presente" | "ausente" | "ilegible"
    mtime_ns: int = 0
    size: int = 0


def _capturar_estado_de_un_archivo(path: pathlib.Path) -> _EstadoDeArchivo:
    """Tri-estado de un target: presente/ausente/ilegible (nunca confundidos)."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return _EstadoDeArchivo(tipo="ausente")
    except OSError:
        return _EstadoDeArchivo(tipo="ilegible")
    return _EstadoDeArchivo(tipo="presente", mtime_ns=st.st_mtime_ns, size=st.st_size)


def _capturar_estado_de_archivos(
    paths: list[pathlib.Path],
) -> dict[pathlib.Path, _EstadoDeArchivo]:
    """Evidencia pre-sort de cada target, para la atribución post-sort.

    LOOT real reescribe plugins.txt/loadorder.txt al aplicar (libloot
    ``set_load_order`` → ``save()`` incondicional, más el backup del GUI),
    incluso cuando el orden no cambia. Una corrida que salió 0 SIN tocar
    ninguno de los targets no aplicó el sort (p. ej. otra instancia de LOOT
    ya abierta se quedó con el mutex de aplicación — loot/loot
    ``src/gui/qt/main.cpp`` sale 0 enfocando la ventana existente).
    """
    return {path: _capturar_estado_de_un_archivo(path) for path in paths}


def _evaluar_evidencia(
    antes: dict[pathlib.Path, _EstadoDeArchivo],
    paths: list[pathlib.Path],
) -> tuple[bool, pathlib.Path | None]:
    """Devuelve ``(hubo_cambio, ilegible)`` comparando estado pre vs post.

    ``ilegible is not None`` → la evidencia NO es verificable (estado previo o
    final inobservable) y el caller debe fallar cerrado: nunca se deduce
    "cambió" ni "no cambió" de un ``stat`` fallido.
    """
    cambio = False
    for path in paths:
        estado_post = _capturar_estado_de_un_archivo(path)
        estado_pre = antes.get(path)
        if estado_post.tipo == "ilegible" or (estado_pre is not None and estado_pre.tipo == "ilegible"):
            return False, path
        if estado_pre is None or estado_post != estado_pre:
            cambio = True
    return cambio, None


class _LootSortFailedError(Exception):
    """Interno: un sort que no puede confirmarse como exitoso debe lanzar
    DENTRO del lock para que ``SnapshotTransactionLock.__aexit__`` restaure el
    load order; el resultado original viaja en la excepción para armar la
    respuesta al caller. ``detail`` (opcional) reemplaza la inferencia de
    ``message`` cuando la causa no está en ``errors``/stderr/stdout."""

    def __init__(self, result: LOOTResult, detail: str | None = None) -> None:
        super().__init__(detail or f"LOOT sort failed with return code {result.return_code}")
        self.result = result
        self.detail = detail


class _ActionManifestError(Exception):
    """Interno (T-26): la emisión del manifiesto de vuelo falló. Se lanza DENTRO
    del lock (antes de mutar) para que el sort NO proceda sin manifiesto — la
    caja negra no es opcional cuando el journal está cableado."""


class LootSortingService:
    """Run LOOT's load-order sort under the shared distributed lock.

    Dependencies are injected (DI). ``loot_runner`` is built lazily from
    ``path_resolver`` on first use because tool paths may be unconfigured at
    construction time (mirrors ``SynthesisPipelineService._ensure_*``); it can
    also be injected directly for tests.
    """

    RESOURCE_ID: str = LOAD_ORDER_RESOURCE_ID
    AGENT_ID: str = "loot-sorting-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        path_resolver: PathResolutionService | None = None,
        path_validator: PathValidator | None = None,
        loot_exe: pathlib.Path | None = None,
        timeout: int = _DEFAULT_LOOT_TIMEOUT_SECONDS,
        loot_runner: LootRunnerProtocol | None = None,
        load_order_resolver: LoadOrderFileResolver | None = None,
        preflight: PreflightService | None = None,
        mo2_root: pathlib.Path | None = None,
        journal: OperationJournal | None = None,
        vfs_broker: VfsBrokerProtocol | None = None,
        vfs_instance_id: str = "portable-main",
        require_vfs: bool = False,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._path_validator = path_validator
        self._loot_exe = loot_exe
        self._timeout = timeout
        self._loot_runner = loot_runner
        self._load_order_resolver = load_order_resolver
        self._preflight = preflight
        # Hint para el preflight en call sites sin path_resolver (agente):
        # la raíz de la instancia MO2 ya conocida por el caller.
        self._mo2_root = mo2_root
        # T-26 (ADR 0002): cuando el journal está cableado, el sort emite un
        # ActionManifest ANTES de mutar. Opcional para no romper callers legacy.
        self._journal = journal
        self._vfs_broker = vfs_broker
        self._vfs_instance_id = vfs_instance_id
        self._require_vfs = require_vfs
        self._brokered_runners: dict[str, LootRunnerProtocol] = {}
        # T-21: resolver de fuentes compartido con el validador post-run; lo
        # setea _ensure_preflight (None hasta entonces o con preflight inyectado).
        self._sources_resolver = None

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight con las piezas disponibles.

        Review Codex PR #239 (P1): los call sites de producción no inyectaban
        ``preflight``, así que el guard era un no-op fuera de los tests — la
        construcción perezosa protege a todos sin cambiarlos. Usa las rutas
        CRUDAS (P2): las resueltas siguen los symlinks y borran exactamente lo
        que el VfsHealthChecker necesita inspeccionar. Review PR #240: también
        cubre el call site del agente (sin path_resolver, con ``mo2_root``/
        ``loot_exe``), cae al auto-detect de MO2 cuando el env no está, espeja
        el fallback ``loot.exe`` del runner, y solo enumera ``mods/`` cuando
        la ruta tiene contraparte validada por el sandbox.
        """
        if self._preflight is not None:
            return self._preflight

        # Import perezoso: validators.preflight (y los builders compartidos que
        # llegan a él) alcanzan tools._process vía loot.version; importarlos a
        # nivel módulo desde tools/ podría ciclar.
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.preflight_sensors import (
            build_modlist_sensors,
            build_overwrite_sensor,
            build_vfs_sensor,
            build_vfs_visibility_sensor,
        )

        raw_game: pathlib.Path | None = None
        raw_mo2: pathlib.Path | None = None
        mo2_validated = False
        loot_exe = self._loot_exe

        if self._path_resolver is not None:
            raw_game = self._path_resolver.get_skyrim_path_raw()
            raw_mo2 = self._path_resolver.get_mo2_path_raw()
            if raw_mo2 is not None:
                mo2_validated = self._path_resolver.get_mo2_path() is not None
            else:
                # Sin MO2_PATH, el Supervisor puede resolver la instancia por
                # auto-detección; ese candidato ya viene resuelto (pierde
                # symlinks de ancestros) pero sí permite ver mods enlazados.
                raw_mo2 = self._path_resolver.detect_mo2_path()
                mo2_validated = raw_mo2 is not None
            loot_exe = loot_exe or self._path_resolver.get_loot_exe()

        if raw_mo2 is None and self._mo2_root is not None:
            raw_mo2 = self._mo2_root
            mo2_validated = True  # raíz provista por el caller (instancia MO2 real)

        # Builder compartido (T-16d): rutas CRUDAS, guard de "al menos una raíz".
        vfs_checker = build_vfs_sensor(raw_game=raw_game, raw_mo2=raw_mo2, scan_mods_dir=mo2_validated)

        # Espejo del fallback de _ensure_loot_runner: el preflight debe medir
        # la versión del binario que efectivamente va a correr.
        loot_exe = loot_exe or pathlib.Path("loot.exe")

        # T-30w/T-21: el resolver de fuentes de plugins se comparte entre los
        # sensores de modlist y el check de headers del validador post-run.
        sources_resolver = self._build_sources_resolver(raw_mo2, mo2_validated)
        self._sources_resolver = sources_resolver

        # T-30w (builder compartido T-16d): cablear los sensores de
        # masters/límites cuando las fuentes de plugins son resolubles. Si no lo
        # son, quedan en None → el semáforo reporta "no configurado" en vez de
        # mentir verde (regla de honestidad).
        masters_check, limits_check = build_modlist_sensors(sources_resolver)

        # T-30·3 (builder compartido T-16d): sensor de overwrite sucio. Solo
        # requiere una raíz MO2 validada (el overwrite es <mo2>/overwrite, fuera
        # del árbol del perfil), así que se cablea aparte de los de modlist.
        overwrite_dir = raw_mo2 / "overwrite" if (mo2_validated and isinstance(raw_mo2, pathlib.Path)) else None
        overwrite_check = build_overwrite_sensor(overwrite_dir)

        # T-30·4: sensor de permisos de escritura sobre las rutas que ESTE
        # Ritual (el sort de LOOT) reescribe — los dirs de los archivos de load
        # order resueltos, no rutas de otros rituales (review Codex #256).
        permissions_check = self._build_permissions_check()

        # U-01: ¿el modlist del perfil se ve en el Data que LOOT va a leer? Sin
        # este sensor, un run standalone (sin heredar la USVFS de MO2) ordena el
        # juego base y reporta verde. Reusa el resolver de fuentes ya armado.
        #
        # SOLO aplica al lanzamiento STANDALONE: el sensor mide el Data FÍSICO, y
        # con broker LOOT corre DENTRO de la USVFS (`BrokeredLootRunner`), donde
        # ve los mods virtualizados que ese Data no tiene. Medirlo ahí daría un
        # ROJO falso que bloquea un sort correcto — exactamente el falso positivo
        # que este sensor existe para evitar. El modo de lanzamiento es una
        # PRECONDICIÓN del sensor, no un detalle del caller: sin él, la medición
        # no significa nada. Con `None` el checkpoint sale "no configurado" —
        # honesto: no se midió (lección #250).
        #
        # "vfs_broker configurado" no prueba "USVFS en uso" (review CodeRabbit,
        # posterior a 134d9e0): un `loot_runner` inyectado sin `for_profile` hace
        # que `_ensure_loot_runner` devuelva ESE runner y jamás toque el broker.
        # `_routes_through_physical_data` espeja esa misma decisión para que el
        # gate no pueda desincronizarse de lo que el sort va a ejecutar de verdad.
        visibility_check = (
            build_vfs_visibility_sensor(
                game=self._path_resolver.get_skyrim_path() if self._path_resolver is not None else None,
                sources_resolver=sources_resolver,
            )
            if self._routes_through_physical_data()
            else None
        )

        self._preflight = PreflightService(
            vfs_checker=vfs_checker,
            loot_exe=loot_exe,
            masters_check=masters_check,
            limits_check=limits_check,
            overwrite_check=overwrite_check,
            permissions_check=permissions_check,
            visibility_check=visibility_check,
        )
        return self._preflight

    def _build_permissions_check(self) -> PermissionsCheck | None:
        """Construye el closure del sensor de permisos de escritura (T-30·4).

        Prueba escritura exactamente en lo que ESTE Ritual reescribe: los
        directorios de los archivos de load order resueltos —
        ``plugins.txt``/``loadorder.txt`` en LOCALAPPDATA, el perfil de MO2 y
        overrides — la misma unión que ``target_files`` snapshotea. Acotarlo a lo
        que LOOT toca evita falsos rojos por un ``Data``/``overwrite`` de solo
        lectura que no afectan al sort, e incluye LOCALAPPDATA/overrides que un
        target por-directorio de MO2 no cubría (review Codex #256). El perfil ya
        viene validado por ``LoadOrderFileResolver`` (no se re-arma la ruta acá,
        así se evita el traversal de un ``MO2_PROFILE`` con ``..``). El closure
        re-resuelve por run (freshness); sin archivos resolubles → ``None`` → el
        semáforo dice "no configurado" (no miente verde).
        """
        if not self._ensure_load_order_resolver().resolve().files:
            return None
        from sky_claw.local.validators.write_permissions import WritePermissionsChecker

        def _permissions() -> WriteAccessReport:
            files = self._ensure_load_order_resolver().resolve().files
            seen: set[pathlib.Path] = set()
            targets: list[pathlib.Path] = []
            for load_order_file in files:
                parent = load_order_file.parent
                if isinstance(parent, pathlib.Path) and parent not in seen:
                    seen.add(parent)
                    targets.append(parent)
            return WritePermissionsChecker(targets=targets).check()

        return _permissions

    def _build_sources_resolver(self, raw_mo2: pathlib.Path | None, mo2_validated: bool):
        """Closure que re-resuelve las fuentes de plugins en cada llamada.

        Compartido por los sensores de modlist (T-30w) y el check de headers
        del post-run (T-21). Re-resolver por llamada evita quedar con un
        snapshot viejo: si el usuario instala/activa plugins después del
        primer sort, las corridas siguientes ven el estado nuevo (review
        Codex #252).
        """
        from sky_claw.local.mo2.plugin_sources import resolve_plugin_sources

        game_data_dir: pathlib.Path | None = None
        if self._path_resolver is not None:
            skyrim = self._path_resolver.get_skyrim_path()
            # isinstance defiende de resolvers mockeados que devuelven no-Path.
            if isinstance(skyrim, pathlib.Path):
                game_data_dir = skyrim / "Data"

        mo2_ok = mo2_validated and isinstance(raw_mo2, pathlib.Path)
        mo2_mods_dir = raw_mo2 / "mods" if mo2_ok else None
        mo2_overwrite_dir = raw_mo2 / "overwrite" if mo2_ok else None

        # Para el set de HABILITADOS preferimos plugins.txt (activos con `*`)
        # sobre loadorder.txt (orden completo, incluye deshabilitados) — usar
        # loadorder.txt daría falsos rojos por plugins inactivos (review #252).
        load_order_files = list(self._ensure_load_order_resolver().resolve().files)
        load_order_file = next(
            (f for f in load_order_files if f.name.lower() == "plugins.txt"),
            _primary_load_order_file(load_order_files),
        )

        def _resolve():
            return resolve_plugin_sources(
                game_data_dir=game_data_dir,
                mo2_mods_dir=mo2_mods_dir,
                mo2_overwrite_dir=mo2_overwrite_dir,
                load_order_file=load_order_file,
            )

        return _resolve

    def _ensure_load_order_resolver(self) -> LoadOrderFileResolver:
        """Construye perezosamente el resolver de load order (mismo patrón que
        ``_ensure_loot_runner``): MO2 root/profile del path resolver si están
        configurados; LOCALAPPDATA lo toma el resolver de su entorno."""
        if self._load_order_resolver is not None:
            return self._load_order_resolver

        mo2_root: pathlib.Path | None = None
        profile = "Default"
        if self._path_resolver is not None:
            mo2_root = self._path_resolver.get_mo2_path()
            if mo2_root is not None:
                profile = self._path_resolver.get_active_profile()

        # Call site del agente (sin path_resolver): usar el mo2_root provisto por
        # el caller para encontrar el plugins.txt del profile (review Copilot #252).
        # isinstance defiende de un mo2_root mockeado (no-Path) en tests.
        if mo2_root is None and isinstance(self._mo2_root, pathlib.Path):
            mo2_root = self._mo2_root

        self._load_order_resolver = LoadOrderFileResolver(mo2_root=mo2_root, profile=profile)
        return self._load_order_resolver

    def _routes_through_physical_data(self) -> bool:
        """¿El runner que ``_ensure_loot_runner`` va a devolver lee el ``Data``
        físico, en vez de correr bajo la USVFS del broker?

        Espeja el mismo árbol de decisión de ``_ensure_loot_runner`` sin
        construir el runner real (esto se evalúa en el preflight, antes de
        que exista un job): un ``loot_runner`` inyectado SIN ``for_profile``
        gana siempre, ignorando ``_vfs_broker`` por completo — solo
        ``BrokeredLootRunner``/``VfsRequiredLootRunner`` implementan esa
        fábrica, así que su presencia es lo único que certifica que el
        runner es VFS-aware. Sin runner inyectado, decide el broker.
        """
        if self._loot_runner is not None:
            return not _is_per_profile_runner(self._loot_runner)
        return self._vfs_broker is None

    def _ensure_loot_runner(self, profile: str = "Default") -> LootRunnerProtocol:
        """Lazily build the LOOTRunner, resolving the LOOT exe + game path on first use.

        The LOOT executable is taken from (in order) the injected ``loot_exe``,
        the path resolver (``LOOT_EXE``), then a bare ``loot.exe`` last resort —
        so a configured/discovered install is honored instead of always assuming
        ``loot.exe`` is on the cwd/PATH.
        """
        if self._loot_runner is not None:
            # La detección vive en _is_per_profile_runner (fuente única): este
            # predicado y el de _routes_through_physical_data TIENEN que decidir
            # lo mismo, o el gate de visibilidad opinaría sobre otro modo de
            # lanzamiento que el real.
            if not _is_per_profile_runner(self._loot_runner):
                return self._loot_runner
            cached = self._brokered_runners.get(profile)
            if cached is None:
                cached = self._loot_runner.for_profile(profile)  # type: ignore[attr-defined]
                self._brokered_runners[profile] = cached
            return cached

        if self._vfs_broker is not None:
            cached = self._brokered_runners.get(profile)
            if cached is not None:
                return cached
            if self._path_resolver is None:
                raise LOOTNotFoundError("Cannot run LOOT under USVFS: no path_resolver configured.")
            game_path = self._path_resolver.get_skyrim_path()
            mo2_root = self._path_resolver.get_mo2_path()
            loot_exe = self._loot_exe or self._path_resolver.get_loot_exe()
            if game_path is None or mo2_root is None or loot_exe is None:
                raise LOOTNotFoundError("Cannot run LOOT under USVFS: MO2, Skyrim or LOOT path is not configured.")
            from sky_claw.local.mo2.brokered_loot import BrokeredLootRunner

            resolver = LoadOrderFileResolver(mo2_root=mo2_root, profile=profile)
            runner = BrokeredLootRunner(
                broker=self._vfs_broker,
                instance_id=self._vfs_instance_id,
                mo2_root=mo2_root,
                profile=profile,
                game_data_dir=game_path / "Data",
                loot_exe=loot_exe,
                timeout=self._timeout,
                mutation_targets=lambda: tuple(resolver.resolve().files),
            )
            self._brokered_runners[profile] = runner
            return runner

        if self._require_vfs:
            raise LOOTNotFoundError(
                "F8 guard: LOOT requiere un VfsExecutionBroker conectado a MO2/USVFS; "
                "se rechazó el subprocess standalone."
            )

        if self._path_resolver is None:
            raise LOOTNotFoundError("Cannot run LOOT: no loot_runner injected and no path_resolver configured.")

        game_path = self._path_resolver.get_skyrim_path()
        if game_path is None:
            raise LOOTNotFoundError("Cannot run LOOT: SKYRIM_PATH is not configured.")

        loot_exe = self._loot_exe or self._path_resolver.get_loot_exe() or pathlib.Path("loot.exe")

        self._loot_runner = LOOTRunner(
            LOOTConfig(loot_exe=loot_exe, game_path=game_path, timeout=self._timeout),
            path_validator=self._path_validator,
        )
        return self._loot_runner

    async def prepare_vfs_attestation(
        self,
        params: LootExecutionParams | None = None,
    ) -> VfsAttestationChallenge | None:
        """Captura el fingerprint antes del HITL sin mantener un worker vivo."""
        profile = str(getattr(params, "profile_name", "Default"))
        runner = self._ensure_loot_runner(profile)
        prepare = getattr(runner, "prepare_attestation", None)
        if prepare is None:
            return None
        challenge = await prepare()
        return challenge

    def clear_vfs_attestation(self, params: LootExecutionParams | None = None) -> None:
        """Descarta el preview de esta invocación, incluso si HITL deniega o cancela."""
        profile = str(getattr(params, "profile_name", "Default"))
        runner = self._brokered_runners.get(profile)
        if runner is None and self._loot_runner is not None:
            declared_factory = getattr(type(self._loot_runner), "for_profile", None)
            if not callable(declared_factory):
                runner = self._loot_runner
        if runner is None:
            return
        clear = getattr(runner, "clear_prepared_attestation", None)
        if callable(clear):
            clear()

    async def _resolve_load_order_for_runner(self, runner: LootRunnerProtocol) -> LoadOrderPaths:
        """Usa los targets del perfil VFS; runners legacy conservan el resolver actual."""
        declared_provider = getattr(type(runner), "mutation_targets", None)
        provider = getattr(runner, "mutation_targets", None)
        if callable(declared_provider) and callable(provider):
            targets = await asyncio.to_thread(provider)
            if not isinstance(targets, tuple) or any(not isinstance(path, pathlib.Path) for path in targets):
                raise TypeError("mutation_targets del runner VFS debe devolver tuple[Path, ...]")
            return LoadOrderPaths(
                files=tuple(path.resolve() for path in targets),
                sources=("vfs_profile",),
            )
        return self._ensure_load_order_resolver().resolve()

    async def sort_load_order(
        self,
        params: LootExecutionParams | None = None,
        *,
        update_masterlist: bool | None = None,
        override_preflight: bool = False,
    ) -> dict[str, Any]:
        """Sort the load order under the load-order lock.

        Always returns a serializable ``dict`` for known failure modes (lock
        contention, missing LOOT, timeout) so the caller can forward it verbatim
        instead of propagating an exception.

        ``update_masterlist`` takes precedence when given (the agent tool passes
        ``False`` to preserve its no-network behavior); otherwise it falls back
        to ``params.update_masterlist`` (LootExecutionParams default is True).

        Un preflight en ROJO (T-15: p.ej. symlinks + LOOT <0.29, el escenario
        de LOOT ciego ante el VFS) bloquea el sort salvo ``override_preflight``
        explícito (flujo HITL); el reporte viaja en la respuesta.
        """
        if update_masterlist is None:
            update_masterlist = bool(getattr(params, "update_masterlist", True))

        # Versión de LOOT para el ActionManifest (T-26): la detecta el preflight
        # sin relanzar el binario (review Codex PR #243). None si no corrió.
        loot_version: tuple[int, int, int] | None = None
        preflight_report: PreflightReport | None = None
        preflight = None if override_preflight else self._ensure_preflight()
        if preflight is not None:
            preflight_report = await preflight.run()
            loot_version = preflight.loot_version
            if preflight_report.blocks_mutations:
                detail = "Preflight en rojo: el sort de LOOT quedó bloqueado. " + "; ".join(
                    c.summary for c in preflight_report.checks if c.status.value == "red"
                )
                logger.warning("%s", detail)
                return {
                    "status": "error",
                    "success": False,
                    "message": detail,
                    "logs": detail,
                    "preflight": preflight_report.to_dict(),
                }

        try:
            profile = str(getattr(params, "profile_name", "Default"))
            runner = self._ensure_loot_runner(profile)
        except LOOTNotFoundError as exc:
            logger.error("LOOT runner unavailable: %s", exc)
            return {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}

        # T-06: snapshotear lo que LOOT realmente puede reescribir. Sin
        # candidatos (entorno no configurado) el sort se rechaza tras la
        # corrida: sin targets observables no hay evidencia para atribuir el
        # éxito (fail-closed — review adversarial #495).
        load_order = await self._resolve_load_order_for_runner(runner)
        target_files = list(load_order.files)
        rolled_back = False
        # T-21: se llena solo en el path de éxito (el validador es post-vuelo).
        post_run_payload: dict[str, Any] | None = None

        # Referencia al lock fuera del with: rolled_back se deriva del resultado
        # REAL del rollback (tx.rollback_completed) — un restore fallido en la
        # ruta de excepción solo se loguea, así que bool(target_files) mentiría
        # (review Codex PR #238).
        tx = SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.RESOURCE_ID,
            agent_id=self.AGENT_ID,
            target_files=target_files,
            metadata={
                "source": "loot_sorting",
                "update_masterlist": update_masterlist,
                "load_order_sources": list(load_order.sources),
            },
        )
        journal_tx_id: int | None = None
        # Una vez commiteada la TX, ninguna ruta posterior debe re-marcarla
        # rolled-back: una cancelación mientras se compone/persiste el informe
        # (post-commit) corrompería el audit trail de una TX ya exitosa
        # (mark_transaction_rolled_back no valida el estado) — review Codex #249.
        journal_committed = False
        try:
            async with tx:
                # T-28: el "antes" se lee DENTRO del lock, en el mismo dominio
                # de serialización que el "después" y la evidencia física — una
                # lectura pre-lock podría atribuir a este sort un cambio que
                # otro Ritual hizo antes de que adquiriéramos (review
                # adversarial #495). Best-effort: si no se puede leer, el
                # informe no lleva diff.
                before_order = _read_plugin_order(_primary_load_order_file(target_files))
                # T-26 (ADR 0002): emitir la "caja negra de vuelo" ANTES de
                # mutar. Si el journal está cableado y la emisión falla, el sort
                # NO procede (se lanza dentro del lock → __aexit__ revierte).
                if self._journal is not None:
                    journal_tx_id = await self._emit_action_manifest(tx, target_files, loot_version)
                # Evidencia física pre-sort (DENTRO del lock, atribuible a esta
                # corrida).
                estado_pre_sort = _capturar_estado_de_archivos(target_files)
                result = await runner.sort(update_masterlist=update_masterlist)
                if not result.success:
                    # Lanzar DENTRO del lock para que __aexit__ restaure el snapshot.
                    raise _LootSortFailedError(result)
                if not target_files:
                    # Sin targets observables no hay evidencia para verificar
                    # ni atribuir el sort: incertidumbre → fallo, nunca éxito
                    # ciego (review adversarial #495). El mutex de main.cpp
                    # también sale 0 sin sortear en este entorno.
                    raise _LootSortFailedError(
                        result,
                        detail=(
                            "No hay archivos de load order observables (plugins.txt/"
                            "loadorder.txt) para verificar ni atribuir el sort: sin "
                            "evidencia no se puede confirmar que LOOT aplicó el orden. "
                            "Configurá LOCALAPPDATA/MO2 (o el override) para que el "
                            "servicio pueda snapshotear y validar los targets."
                        ),
                    )
                cambiaron, ilegible = _evaluar_evidencia(estado_pre_sort, target_files)
                if ilegible is not None:
                    # Estado previo o final inobservable: no se deduce mutación
                    # (ni su ausencia) de un stat fallido — fail-closed.
                    raise _LootSortFailedError(
                        result,
                        detail=(
                            f"No se pudo inspeccionar el estado del load order "
                            f"({ilegible}) tras la corrida: sin evidencia verificable "
                            "no se puede confirmar que LOOT aplicó el orden."
                        ),
                    )
                if not cambiaron:
                    # rc=0 sin mutación observable: LOOT no aplicó. Caso real
                    # (main.cpp): con otra instancia ya abierta, el proceso sale
                    # 0 enfocando la ventana existente SIN sortear. Incertidumbre
                    # → fallo (nunca éxito ciego): el snapshot se restaura (no-op
                    # si nada cambió) y el caller recibe un mensaje accionable.
                    raise _LootSortFailedError(
                        result,
                        detail=(
                            "LOOT salió con código 0 pero ningún archivo del load order "
                            "cambió durante la corrida: el sort no aplicó (¿otra instancia "
                            "de LOOT ya abierta se quedó con el mutex?) o los archivos "
                            "resueltos no cubren los que LOOT reescribió."
                        ),
                    )
                # T-28: el "después" real se lee del archivo que LOOT reescribió,
                # DENTRO del lock (atribuible a esta corrida). `result.sorted_plugins`
                # es telemetría opcional: con LOOT real llega vacía (la GUI no
                # imprime la lista — upstream main.cpp) y usarla como "después"
                # haría que el informe omita el diff aunque el orden haya cambiado.
                # Misma fuente que `before_order` (loadorder.txt primero), así el
                # diff compara archivo contra archivo.
                after_order = _read_plugin_order(_primary_load_order_file(target_files))
                # T-21: validar DENTRO del lock — con el lock liberado, otro
                # Ritual concurrente podría mutar el load order antes de la
                # lectura y el reporte quedaría atribuido a este sort (review
                # Copilot #264). El helper no lanza (best-effort), así que no
                # puede disparar el rollback de un sort exitoso.
                post_run_payload = await self._run_post_run_validation()
            if journal_tx_id is not None and self._journal is not None:
                # El sort ya terminó y el lock se liberó; un fallo de commit del
                # journal es de estado (el manifiesto ya quedó persistido), no
                # debe romper el contrato "siempre devolver dict" (review
                # Copilot PR #243). Best-effort: se loguea con traceback.
                try:
                    await self._journal.commit_transaction(journal_tx_id)
                    journal_committed = True
                except Exception:  # noqa: BLE001 — boundary best-effort del journal
                    logger.error(
                        "Fallo al commitear la transacción del journal %d tras el sort exitoso",
                        journal_tx_id,
                        exc_info=True,
                    )
                # T-28 (ADR 0002): cerrar la caja negra con el informe
                # post-vuelo. Va DESPUÉS del commit para leer el estado real
                # de la TX; también best-effort — el sort ya fue exitoso.
                await self._emit_flight_report(
                    journal_tx_id,
                    before_order=before_order,
                    after_order=after_order,
                    post_run_validation=post_run_payload,
                )
        except LockAcquisitionError as exc:
            logger.warning("Lock contention on '%s': %s", self.RESOURCE_ID, exc)
            detail = f"Could not acquire load-order lock '{self.RESOURCE_ID}': {exc}"
            return {"status": "error", "success": False, "message": detail, "logs": detail}
        except _ActionManifestError as exc:
            await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("No se pudo emitir el ActionManifest; sort abortado: %s", exc)
            detail = f"Manifiesto de vuelo requerido no emitido: {exc}"
            return {
                "status": "error",
                "success": False,
                "message": detail,
                "logs": detail,
                "rolled_back": tx.rollback_completed,
            }
        except _LootSortFailedError as exc:
            await self._mark_journal_rolled_back(journal_tx_id)
            result = exc.result
            rolled_back = tx.rollback_completed
            if exc.detail is not None:
                # rc=0 sin mutación observable: el resultado de PROCESO es
                # "exitoso" pero el contrato lo rechaza. Degradar a fallo con el
                # detalle como error para que status/success/message/errors de la
                # respuesta no mientan (el return_code queda en 0: es la verdad
                # del proceso, el detalle explica el rechazo).
                result = replace(result, errors=[*result.errors, exc.detail])
        except (LOOTNotFoundError, LOOTTimeoutError) as exc:
            await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("LOOT sort failed: %s", exc)
            return {
                "status": "error",
                "success": False,
                "message": str(exc),
                "logs": str(exc),
                "rolled_back": tx.rollback_completed,
            }
        except asyncio.CancelledError:
            # La cancelación propaga; el snapshot ya se restauró en __aexit__.
            # Cerrar la TX del journal es best-effort (no debe tragar la cancelación).
            # Si ya se commiteó (cancelación durante el informe post-vuelo) NO se
            # revierte: la TX fue exitosa y el audit trail no debe mentir (#249).
            if not journal_committed:
                await self._mark_journal_rolled_back(journal_tx_id)
            raise
        except Exception as exc:  # noqa: BLE001 — contrato: sort_load_order SIEMPRE devuelve dict
            # runner.sort() u otra pieza puede lanzar algo fuera de las excepciones
            # LOOT-específicas (RuntimeError de subproceso, error de validador). El
            # snapshot ya se restauró en __aexit__; acá cerramos la TX del manifiesto
            # (no dejar PENDING) y devolvemos un dict serializable en vez de propagar
            # (review Codex PR #243). Si ya se commiteó, no revertir (#249).
            if not journal_committed:
                await self._mark_journal_rolled_back(journal_tx_id)
            logger.error("Error inesperado en el sort de LOOT: %s", exc, exc_info=True)
            return {
                "status": "error",
                "success": False,
                "message": f"Error inesperado durante el sort: {exc}",
                "logs": str(exc),
                "rolled_back": tx.rollback_completed,
            }

        # Contrato compartido (deuda #5): ``message`` canónico junto a los campos
        # estructurados; en éxito queda vacío (el consumidor arma su copy). En
        # fallo, incluir raw_stderr: LOOT puede salir non-zero con el error solo
        # en stderr no estructurado (errors=[] del parser) — review Codex #222.
        # Si nada de eso existe (LOOT GUI no imprime por consola — upstream
        # main.cpp), el mensaje mínimo accionable es el exit code: nunca dejar
        # success=False con message vacío cuando la causa es identificable. El
        # strip evita que un stderr/stdout solo-whitespace cuente como mensaje
        # visualmente vacío (review adversarial #495).
        stderr_text = result.raw_stderr.strip()
        stdout_text = result.raw_stdout.strip()
        message = (
            ""
            if result.success
            else (
                "; ".join(str(e) for e in result.errors)
                or stderr_text
                or stdout_text
                or f"LOOT sort failed with exit code {result.return_code}."
            )
        )
        response: dict[str, Any] = {
            "status": "success" if result.success else "error",
            "success": result.success,
            "message": message,
            "return_code": result.return_code,
            "sorted_plugins": result.sorted_plugins,
            "warnings": result.warnings,
            "errors": result.errors,
            "logs": result.raw_stdout or "",
            "rolled_back": rolled_back,
        }
        # Superficie de los warnings del preflight (T-30·3): un preflight no-verde
        # que NO bloquea (amarillo, p.ej. overwrite sucio) igual debe llegar al
        # operador. Sin esto solo se loguearía y el agente/GUI vería un success
        # limpio, perdiendo el aviso antes del próximo Ritual (review Codex #254).
        if preflight_report is not None and preflight_report.status.value != "green":
            response["preflight"] = preflight_report.to_dict()
        # T-21: los hallazgos del validador post-run llegan al caller — sin
        # esto solo quedarían en el journal y el operador vería un success
        # limpio (misma lección que el surfacing amarillo del preflight, #254).
        if post_run_payload is not None and post_run_payload.get("has_findings"):
            response["post_run"] = post_run_payload
        vfs_result = getattr(runner, "last_vfs_result", None)
        if vfs_result is not None and vfs_result.attestation is not None:
            response["vfs_attestation"] = vfs_result.attestation
        return response

    async def _emit_action_manifest(
        self,
        tx: SnapshotTransactionLock,
        target_files: list[pathlib.Path],
        loot_version: tuple[int, int, int] | None,
    ) -> int:
        """Construye y persiste el ActionManifest del sort dentro del lock (T-26).

        Se llama ANTES de ``runner.sort()`` con el journal ya cableado: los
        snapshots del lock (``tx.snapshots``) ya existen acá, así que el plan de
        rollback del manifiesto apunta a snapshots reales. Devuelve el id de la
        transacción del journal para poder commit/rollback después.

        Args:
            tx: El lock activo (sus ``snapshots`` alimentan el plan de rollback).
            target_files: Archivos que el sort tocará.
            loot_version: Versión de LOOT detectada por el preflight, o None.

        Raises:
            _ActionManifestError: Si begin_transaction/persist falla — el sort
                no debe proceder sin la caja negra emitida. La TX del journal
                recién abierta se marca rolled-back para no dejarla PENDING
                (review Codex PR #243).
        """
        from sky_claw.app.orchestrator.preview.action_manifest import build_action_manifest

        assert self._journal is not None  # cableado verificado por el caller
        journal_tx_id: int | None = None
        try:
            journal_tx_id = await self._journal.begin_transaction(
                description="loot_sort",
                agent_id=self.AGENT_ID,
            )
            manifest = build_action_manifest(
                ritual_id=f"loot-sort-{journal_tx_id}",
                tool="LOOT",
                tool_version=".".join(map(str, loot_version)) if loot_version else None,
                target_files=[str(f) for f in target_files],
                snapshots=tx.snapshots,
                summary="Ordenar orden de carga con LOOT.",
            )
            await self._journal.persist_action_manifest(
                manifest,
                agent_id=self.AGENT_ID,
                transaction_id=journal_tx_id,
            )
            return journal_tx_id
        except Exception as exc:  # noqa: BLE001 — boundary: cualquier fallo del journal
            # El journal puede lanzar JournalTransactionError, sqlite3.Error, etc.
            # Todos deben convertirse a _ActionManifestError para que el
            # enforcement devuelva un dict serializable en vez de propagar y
            # romper el contrato de sort_load_order (review Copilot PR #243).
            # No dejar la TX recién abierta en PENDING (review Codex PR #243).
            await self._mark_journal_rolled_back(journal_tx_id)
            raise _ActionManifestError(str(exc)) from exc

    async def _run_post_run_validation(self) -> dict[str, Any] | None:
        """Corre el validador post-run (T-21) — best-effort, post-vuelo.

        Reusa el MISMO ``PreflightService`` del gate previo (sus closures
        re-resuelven por run, así que acá ven el estado post-mutación) y el
        resolver de fuentes compartido para el check de headers. El guard
        ``isinstance`` deja afuera los preflight mockeados/inyectados de tests
        y callers ad-hoc: sin un servicio real no hay validación que afirmar.
        Un fallo se loguea y devuelve ``None`` — jamás rompe un sort exitoso
        (misma disciplina que el flight report, reviews #243/#249).
        """
        try:
            from sky_claw.local.validators.post_run import PostRunValidator
            from sky_claw.local.validators.preflight import PreflightService

            preflight = self._ensure_preflight()
            if not isinstance(preflight, PreflightService):
                return None
            validator = PostRunValidator(preflight=preflight, plugin_sources=self._sources_resolver)
            return (await validator.run()).to_dict()
        except Exception:  # noqa: BLE001 — post-vuelo best-effort (disciplina del flight report)
            logger.error("El validador post-run falló (best-effort)", exc_info=True)
            return None

    async def _emit_flight_report(
        self,
        journal_tx_id: int,
        *,
        before_order: list[str] | None = None,
        after_order: list[str] | None = None,
        post_run_validation: dict[str, Any] | None = None,
    ) -> None:
        """Compone y persiste el FlightReport del sort ya terminado (T-28).

        Lee la caja negra desde el journal — el manifiesto persistido en
        ``_emit_action_manifest`` y el estado REAL de la transacción (si el
        commit best-effort falló, el informe dirá ``pending``: verdad antes
        que optimismo). El manifiesto se emite ANTES del sort y es inmutable,
        así que no puede cargar el orden resultante; el diff real (orden antes
        vs orden leído del archivo post-sort, ambos dentro del lock) se calcula
        acá y se adjunta al informe (review Codex #249). ``after_order`` viene
        del load order FÍSICO, no de ``sorted_plugins`` — con LOOT real esa
        lista llega vacía (la GUI no la imprime) y el diff mentiría "sin
        cambio" (review CodeRabbit #495).
        Best-effort con la misma disciplina que el commit:
        un fallo se loguea y NO rompe el contrato "siempre devolver dict" ni
        revierte el sort exitoso.
        """
        from sky_claw.app.orchestrator.preview.flight_report import (
            compose_flight_report_from_journal,
        )
        from sky_claw.app.orchestrator.preview.manifest import LoadOrderDiff

        assert self._journal is not None  # cableado verificado por el caller
        try:
            report = await compose_flight_report_from_journal(self._journal, transaction_id=journal_tx_id)
            # Adjuntar el diff real solo si hay orden antes/después y cambió;
            # from_orders solo genera moves para plugins presentes en ambos, así
            # que un listado parcial de LOOT no puede fabricar movimientos falsos.
            if before_order and after_order:
                diff = LoadOrderDiff.from_orders(before_order, after_order)
                if diff.changed:
                    report = report.model_copy(update={"load_order_diff": diff})
            # T-21: llenar el slot que T-28 dejó esperando al validador
            # post-run — el informe deja de decir "T-21 pendiente".
            if post_run_validation is not None:
                report = report.model_copy(update={"post_run_validation": post_run_validation})
            await self._journal.persist_flight_report(
                report,
                agent_id=self.AGENT_ID,
                transaction_id=journal_tx_id,
            )
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error(
                "Fallo al persistir el informe de vuelo de la transacción %d",
                journal_tx_id,
                exc_info=True,
            )

    async def _mark_journal_rolled_back(self, journal_tx_id: int | None) -> None:
        """Marca la transacción del journal como rolled-back (best-effort).

        Se llama en los caminos de excepción del sort; si el journal falla acá
        (sqlite/IO) NO debe enmascarar el error original ni romper el contrato
        de respuesta serializable — se suprime con log (review Copilot PR #243).
        """
        if journal_tx_id is None or self._journal is None:
            return
        try:
            await self._journal.mark_transaction_rolled_back(journal_tx_id)
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error(
                "Fallo al marcar la transacción del journal %d como rolled-back",
                journal_tx_id,
                exc_info=True,
            )

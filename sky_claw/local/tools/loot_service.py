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
configurado), se degrada a serialización-sola con warning, el comportamiento
previo al T-06.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import TYPE_CHECKING, Any

from sky_claw.antigravity.db.locks import (
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
from sky_claw.local.mo2.load_order import LoadOrderFileResolver

if TYPE_CHECKING:
    from sky_claw.antigravity.core.models import LootExecutionParams
    from sky_claw.antigravity.core.path_resolver import PathResolutionService
    from sky_claw.antigravity.db.journal import OperationJournal
    from sky_claw.antigravity.db.snapshot_manager import FileSnapshotManager
    from sky_claw.antigravity.security.path_validator import PathValidator
    from sky_claw.local.loot.parser import LOOTResult
    from sky_claw.local.validators.overwrite_health import OverwriteScan
    from sky_claw.local.validators.preflight import (
        LimitsCheck,
        MastersCheck,
        OverwriteCheck,
        PermissionsCheck,
        PreflightReport,
        PreflightService,
    )
    from sky_claw.local.validators.write_permissions import WriteAccessReport

logger = logging.getLogger(__name__)

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


class _LootSortFailedError(Exception):
    """Interno: un sort con exit non-zero debe lanzar DENTRO del lock para que
    ``SnapshotTransactionLock.__aexit__`` restaure el load order; el resultado
    original viaja en la excepción para armar la respuesta al caller."""

    def __init__(self, result: LOOTResult) -> None:
        super().__init__(f"LOOT sort failed with return code {result.return_code}")
        self.result = result


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
        loot_runner: LOOTRunner | None = None,
        load_order_resolver: LoadOrderFileResolver | None = None,
        preflight: PreflightService | None = None,
        mo2_root: pathlib.Path | None = None,
        journal: OperationJournal | None = None,
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

        # Import perezoso: validators.preflight llega a tools._process vía
        # loot.version; importarlo a nivel módulo desde tools/ podría ciclar.
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.vfs_health import VfsHealthChecker

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

        vfs_checker = None
        if raw_game is not None or raw_mo2 is not None:
            vfs_checker = VfsHealthChecker(
                game_path=raw_game,
                mo2_root=raw_mo2,
                scan_mods_dir=mo2_validated,
            )

        # Espejo del fallback de _ensure_loot_runner: el preflight debe medir
        # la versión del binario que efectivamente va a correr.
        loot_exe = loot_exe or pathlib.Path("loot.exe")

        # T-30w/T-21: el resolver de fuentes de plugins se comparte entre los
        # sensores de modlist y el check de headers del validador post-run.
        sources_resolver = self._build_sources_resolver(raw_mo2, mo2_validated)
        self._sources_resolver = sources_resolver

        # T-30w: cablear los sensores de masters/límites cuando las fuentes de
        # plugins son resolubles. Si no lo son, quedan en None → el semáforo
        # reporta "no configurado" en vez de mentir verde (regla de honestidad).
        masters_check, limits_check = self._build_modlist_checks(sources_resolver)

        # T-30·3: sensor de overwrite sucio. Solo requiere una raíz MO2 validada
        # (no depende del load order), así que se cablea aparte de los de modlist.
        overwrite_check = self._build_overwrite_check(raw_mo2, mo2_validated)

        # T-30·4: sensor de permisos de escritura sobre las rutas que ESTE
        # Ritual (el sort de LOOT) reescribe — los dirs de los archivos de load
        # order resueltos, no rutas de otros rituales (review Codex #256).
        permissions_check = self._build_permissions_check()

        self._preflight = PreflightService(
            vfs_checker=vfs_checker,
            loot_exe=loot_exe,
            masters_check=masters_check,
            limits_check=limits_check,
            overwrite_check=overwrite_check,
            permissions_check=permissions_check,
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

    def _build_overwrite_check(self, raw_mo2: pathlib.Path | None, mo2_validated: bool) -> OverwriteCheck | None:
        """Construye el closure del sensor de overwrite sucio (T-30·3).

        Requiere solo una raíz MO2 validada (el overwrite es ``<mo2>/overwrite``,
        fuera del árbol del perfil). El closure re-escanea en cada run — el
        ``PreflightService`` se cachea, así que la salida de una herramienta
        corrida entre preflight y preflight debe verse (freshness, patrón #252).
        Sin MO2 resoluble devuelve ``None`` → el semáforo dice "no configurado".
        """
        if not (mo2_validated and isinstance(raw_mo2, pathlib.Path)):
            return None
        from sky_claw.local.validators.overwrite_health import OverwriteHealthChecker

        overwrite_dir = raw_mo2 / "overwrite"

        def _overwrite() -> OverwriteScan:
            return OverwriteHealthChecker(overwrite_dir=overwrite_dir).check()

        return _overwrite

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

    def _build_modlist_checks(self, sources_resolver) -> tuple[MastersCheck | None, LimitsCheck | None]:
        """Construye los closures de los sensores de masters/límites (T-30w).

        Best-effort: si hoy no hay fuentes utilizables, devuelve ``(None, None)``
        para que el semáforo reporte "no configurado" en vez de mentir verde.
        """
        from sky_claw.local.validators.missing_masters import MissingMastersChecker
        from sky_claw.local.validators.plugin_limits import PluginLimitsChecker

        # Gate de honestidad al construir: solo cablear si HOY hay fuentes.
        initial = sources_resolver()
        if not initial.plugin_dirs or not initial.enabled_plugins:
            return None, None

        def _masters():
            sources = sources_resolver()
            return MissingMastersChecker(plugin_dirs=sources.plugin_dirs).check(sources.enabled_plugins)

        def _limits():
            sources = sources_resolver()
            return PluginLimitsChecker(plugin_dirs=sources.plugin_dirs).check(sources.enabled_plugins)

        return _masters, _limits

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

    def _ensure_loot_runner(self) -> LOOTRunner:
        """Lazily build the LOOTRunner, resolving the LOOT exe + game path on first use.

        The LOOT executable is taken from (in order) the injected ``loot_exe``,
        the path resolver (``LOOT_EXE``), then a bare ``loot.exe`` last resort —
        so a configured/discovered install is honored instead of always assuming
        ``loot.exe`` is on the cwd/PATH.
        """
        if self._loot_runner is not None:
            return self._loot_runner

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
            runner = self._ensure_loot_runner()
        except LOOTNotFoundError as exc:
            logger.error("LOOT runner unavailable: %s", exc)
            return {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}

        # T-06: snapshotear lo que LOOT realmente puede reescribir. Sin
        # candidatos (entorno no configurado) se degrada a serialización-sola,
        # el comportamiento previo — el resolver ya lo dejó logueado.
        load_order = self._ensure_load_order_resolver().resolve()
        target_files = list(load_order.files)
        rolled_back = False
        # T-21: se llena solo en el path de éxito (el validador es post-vuelo).
        post_run_payload: dict[str, Any] | None = None

        # Orden ANTES del sort para el diff del informe post-vuelo (T-28): se
        # lee acá porque LOOT reescribe el archivo al ordenar. El "después" será
        # ``result.sorted_plugins``. Best-effort: si no se puede leer, el informe
        # simplemente no lleva load_order_diff (nunca rompe el sort).
        before_order = _read_plugin_order(_primary_load_order_file(target_files))

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
                # T-26 (ADR 0002): emitir la "caja negra de vuelo" ANTES de
                # mutar. Si el journal está cableado y la emisión falla, el sort
                # NO procede (se lanza dentro del lock → __aexit__ revierte).
                if self._journal is not None:
                    journal_tx_id = await self._emit_action_manifest(tx, target_files, loot_version)
                result = await runner.sort(update_masterlist=update_masterlist)
                if not result.success:
                    # Lanzar DENTRO del lock para que __aexit__ restaure el snapshot.
                    raise _LootSortFailedError(result)
            # T-21: el lazo `validate` — re-correr los sensores DESPUÉS de la
            # mutación (best-effort: nunca rompe un sort exitoso). Corre también
            # para callers legacy sin journal; el resultado viaja en la
            # respuesta y, cuando hay journal, en el slot del FlightReport.
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
                    after_order=result.sorted_plugins,
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
        message = (
            ""
            if result.success
            else ("; ".join(str(e) for e in result.errors) or result.raw_stderr or result.raw_stdout or "")
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
        from sky_claw.antigravity.orchestrator.preview.action_manifest import build_action_manifest

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
        vs ``result.sorted_plugins``) se calcula acá y se adjunta al informe
        (review Codex #249). Best-effort con la misma disciplina que el commit:
        un fallo se loguea y NO rompe el contrato "siempre devolver dict" ni
        revierte el sort exitoso.
        """
        from sky_claw.antigravity.orchestrator.preview.flight_report import (
            compose_flight_report_from_journal,
        )
        from sky_claw.antigravity.orchestrator.preview.manifest import LoadOrderDiff

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

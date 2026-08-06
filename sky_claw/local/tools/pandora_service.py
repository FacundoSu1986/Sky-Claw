"""PandoraPipelineService — generación de animaciones (behavior graphs) bajo lock.

Follow-up A de la Fase 2. Pandora regenera los grafos de comportamiento del juego,
estado serializable que el resto de los runners mutantes (LOOT/xEdit/DynDOLOD) ya
protegen con :class:`SnapshotTransactionLock`. Este servicio expone la corrida real
bajo el mismo lock distribuido.

Espeja deliberadamente a :class:`~sky_claw.local.tools.loot_service.LootSortingService`
en la construcción perezosa del runner desde el ``PathResolutionService``. El lock
sigue con **snapshot diferido** (``target_files=[]``), pero eso ya no significa "sin
rollback": el rollback de la salida lo hace el **move-aside** de
:class:`~sky_claw.local.tools._dir_rollback.DirectoryRollback` (U-04), igual que en
``dyndolod_service``. La salida de Pandora es un ÁRBOL de behavior graphs, no un
archivo único, así que el snapshot copy-based del lock es la primitiva equivocada.

Pandora se spawnea directo con ``cwd=game_path`` y ``--output`` explícito. Su único
artefacto administrado es ``game.resolve() / Pandora_Output``: esa misma ruta se
declara en el manifiesto y se protege con rollback. El preflight sondea el output si
ya existe o su padre existente en el primer run, porque el checker omite inexistentes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import stat
from typing import TYPE_CHECKING, Any

from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.app.security.links import (
    link_kind_and_identity_or_raise_with_retry,
    same_direntry_identity,
    same_file_identity,
)
from sky_claw.local.tools._dir_rollback import DirectoryRollback
from sky_claw.local.tools.output_targets import pandora_output_target
from sky_claw.local.tools.pandora_runner import (
    PandoraConfig,
    PandoraExecutionError,
    PandoraResult,
    PandoraRunner,
)

if TYPE_CHECKING:
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.db.journal import OperationJournal
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.validators.preflight import PreflightReport, PreflightService

logger = logging.getLogger(__name__)

#: Lock resource id compartido para la regeneración de behavior graphs de Pandora.
BEHAVIOR_GRAPHS_RESOURCE_ID = "behavior-graphs"


class _ActionManifestError(Exception):
    """Interno (T-26): la emisión del manifiesto de vuelo falló. Se lanza DENTRO
    del lock (antes de mutar) para que Pandora NO proceda sin manifiesto — la
    caja negra no es opcional cuando el journal está cableado (espejo de
    ``loot_service``/``dyndolod_service._ActionManifestError``)."""


class _RunFallidoError(Exception):
    """Interno (U-04): convierte un ``PandoraResult`` fallido en excepción DENTRO
    del ``AsyncExitStack`` para disparar el rollback real de la salida.

    Existe por una propiedad del mecanismo, no por gusto: ``DirectoryRollback``
    restaura **sólo en la rama de excepción** (``if exc_type is not None`` en su
    ``__aexit__``), igual que ``SnapshotTransactionLock``. Un
    ``PandoraResult(success=False)`` —el modo de fallo por exit non-zero— sale
    *limpio* del ``async with``, así que el backup se descartaría y el árbol
    parcial quedaría en disco. Puentear el fallo a excepción es lo que la
    auditoría pedía con *"forzar el rollback cuando ``result.success is False``"*;
    la forma literal no es implementable porque no hay API para pedir el rollback
    desde el cuerpo del context manager.

    Lleva el ``PandoraResult`` completo para que el ``except`` arme el **mismo**
    dict de salida que el camino no-excepcional: el contrato de la tool no cambia,
    sólo gana el rollback que le faltaba. Mismo patrón que
    ``wrye_bash_service._RunFallidoError`` (#397) y que el ``raise`` que
    ``synthesis_service`` ya usaba en producción.
    """

    def __init__(self, result: PandoraResult) -> None:
        self.result = result
        super().__init__(result.stderr or result.stdout or f"exit code {result.return_code}")


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service``/``xedit_service``/``synthesis_service``/
    ``dyndolod_service`` (T-16b/T-16c): un semáforo verde no ensucia el dict;
    amarillo/rojo viajan como ``result["preflight"]`` para que el panel lo renderice.
    """
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


class PandoraPipelineService:
    """Corre Pandora (generación de animaciones) bajo el lock distribuido compartido.

    Dependencias inyectadas (DI). ``pandora_runner`` se construye perezosamente desde
    ``path_resolver`` en el primer uso porque las rutas de tools pueden no estar
    configuradas en construcción; también puede inyectarse directo para tests.
    """

    RESOURCE_ID: str = BEHAVIOR_GRAPHS_RESOURCE_ID
    AGENT_ID: str = "pandora-pipeline-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        path_resolver: PathResolutionService | None = None,
        pandora_exe: pathlib.Path | None = None,
        pandora_runner: PandoraRunner | None = None,
        preflight: PreflightService | None = None,
        journal: OperationJournal | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._path_resolver = path_resolver
        self._pandora_exe = pandora_exe
        self._pandora_runner = pandora_runner
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        self._preflight = preflight
        # T-26/T-28 (ADR 0002): cuando el journal está cableado (path GUI/dispatcher),
        # Pandora emite la caja negra de vuelo. El path del agente (system_tools.
        # run_pandora) construye el servicio SIN journal → no emite (honesto: no hay
        # journal que persistir), espejo del gating opcional de loot_service.
        self._journal = journal

    def _resolve_pandora_paths(
        self,
    ) -> tuple[pathlib.Path | None, pathlib.Path | None, pathlib.Path | None, pathlib.Path | None, pathlib.Path | None]:
        """Reúne ``(game, mo2, exe, raw_game, raw_mo2)`` desde el ``path_resolver``
        **O** el config del runner inyectado (review Codex #314).

        El runner inyectado domina ``game``/``raw_game``/``exe`` cuando su config
        contiene rutas: es quien construye los ``argv`` y ``cwd`` reales. El
        resolver queda como fallback y sigue siendo la única fuente de
        ``mo2``/``raw_mo2``, que el config de Pandora no conserva.
        """
        game = mo2 = exe = raw_game = raw_mo2 = None
        resolver = self._path_resolver
        if resolver is not None:
            g = resolver.get_skyrim_path()
            game = g if isinstance(g, pathlib.Path) else None
            m = resolver.get_mo2_path()
            mo2 = m if isinstance(m, pathlib.Path) else None
            rg = resolver.get_skyrim_path_raw()
            raw_game = rg if isinstance(rg, pathlib.Path) else None
            rm = resolver.get_mo2_path_raw()
            raw_mo2 = rm if isinstance(rm, pathlib.Path) else None
            e = resolver.get_pandora_exe()
            exe = e if isinstance(e, pathlib.Path) else None
        if self._pandora_exe is not None:
            exe = self._pandora_exe
        runner = self._pandora_runner
        if runner is not None:
            cfg = getattr(runner, "config", None)
            cfg_game = getattr(cfg, "game_path", None)
            cfg_exe = getattr(cfg, "pandora_exe", None)
            if isinstance(cfg_game, pathlib.Path):
                game = cfg_game
                raw_game = cfg_game
            if isinstance(cfg_exe, pathlib.Path):
                exe = cfg_exe
        # Fallback de raw: sin resolver (runner inyectado) no hay rutas crudas; usar
        # las resueltas es mejor que no cablear vfs (detecta symlinks en la ruta).
        raw_game = raw_game or game
        raw_mo2 = raw_mo2 or mo2
        return game, mo2, exe, raw_game, raw_mo2

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight de Pandora (T-16c·4, STAGE 4).

        Pandora regenera los behavior graphs (animaciones/IA). Sensores relevantes:
        **permisos de escritura** sobre el output administrado explícito o su padre
        en el primer run (ver ``_permission_targets``), **symlinks/junctions** en
        las rutas crudas, y **overwrite sucio** (un overwrite sucio hace el diff
        inatribuible aunque Pandora no escriba ahí). NO cablea masters/límites:
        Pandora procesa mods de ANIMACIÓN (estilo FNIS/Nemesis), no el load order
        de plugins.

        Se construye con lo que HAYA (review #314): basta game **o** exe **o** MO2
        resoluble (desde el resolver o el config del runner). El overwrite solo se
        cablea con MO2; en standalone (SKYRIM_PATH/PANDORA_EXE sin MO2_PATH) se
        omite ese sensor pero el gate igual protege el output administrado.
        Sin NINGUNA raíz → ``None`` (sin gate, honesto).
        """
        if self._preflight is not None:
            return self._preflight

        game, mo2, exe, raw_game, raw_mo2 = self._resolve_pandora_paths()
        if game is None and mo2 is None and exe is None:
            return None

        # Imports perezosos (anti-ciclo: validators.preflight llega a tools._process).
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.preflight_sensors import (
            build_mo2_profile_sources_resolver,
            build_overwrite_sensor,
            build_vfs_sensor,
            build_vfs_visibility_sensor,
        )
        from sky_claw.local.validators.write_permissions import WritePermissionsChecker

        # vfs sobre rutas CRUDAS (las resueltas ya siguieron los symlinks).
        # scan_mods_dir solo con MO2 VALIDADA (``mo2`` sale de get_mo2_path()):
        # enumerar mods/ sobre una raíz sin contraparte validada listaría
        # directorios arbitrarios (review Codex #240). Antes iba fijo en False,
        # lo que dejaba el scan ciego incluso con una instancia legítima (U-01).
        vfs_checker = build_vfs_sensor(raw_game=raw_game, raw_mo2=raw_mo2, scan_mods_dir=mo2 is not None)

        # Permisos: targets recalculados POR CORRIDA dentro del closure (freshness).
        def _permissions() -> Any:
            return WritePermissionsChecker(targets=self._permission_targets()).check()

        # El overwrite solo aplica con MO2; sin MO2 se omite (no se inventa un
        # sensor sin fuente — review #314 F3).
        overwrite_check = build_overwrite_sensor(mo2 / "overwrite") if mo2 is not None else None

        # U-01: Pandora procesa mods de ANIMACIÓN, no plugins — pero el modlist
        # habilitado sirve igual de PRUEBA de que la virtualización llega: si
        # ningún plugin del perfil se ve en Data, tampoco se ven los mods de
        # animación, y Pandora regeneraría behavior graphs del juego base.
        # Requiere game + MO2 (sin perfil resoluble el builder devuelve None).
        visibility_check = build_vfs_visibility_sensor(
            game=game,
            sources_resolver=(
                build_mo2_profile_sources_resolver(game=game, mo2=mo2, profile=self._path_resolver.get_active_profile())
                if game is not None and mo2 is not None and self._path_resolver is not None
                else None
            ),
        )

        self._preflight = PreflightService(
            vfs_checker=vfs_checker,
            permissions_check=_permissions,
            overwrite_check=overwrite_check,
            visibility_check=visibility_check,
            omit_unconfigured=True,
        )
        return self._preflight

    def _managed_output(self) -> pathlib.Path | None:
        """Devuelve el único output resuelto que Pandora recibe por ``--output``."""
        game, _, _, _, _ = self._resolve_pandora_paths()
        return pandora_output_target(game=game)

    def _permission_targets(self) -> list[pathlib.Path]:
        """Sondea el output administrado o su padre existente en el primer run.

        ``WritePermissionsChecker`` omite rutas inexistentes; usar el padre hasta
        que nazca ``Pandora_Output`` mantiene efectivo el gate de permisos.
        """
        output = self._managed_output()
        if output is None:
            return []
        return [output if output.exists() else output.parent]

    def _rollback_output_dirs(self) -> list[pathlib.Path]:
        """Protege únicamente el output explícito que Pandora regenera completo."""
        output = self._managed_output()
        return [output] if output is not None else []

    @staticmethod
    def _validar_output_generado(output: pathlib.Path) -> os.stat_result:
        """Exige un árbol físico estable con al menos un archivo no vacío."""

        try:
            tipo_raiz, identidad_raiz = link_kind_and_identity_or_raise_with_retry(output)
            if tipo_raiz is not None or identidad_raiz is None or not stat.S_ISDIR(identidad_raiz.st_mode):
                raise PandoraExecutionError(f"Pandora no generó un directorio físico seguro en '{output}'.")
            pendientes: list[tuple[pathlib.Path, os.stat_result, tuple[tuple[pathlib.Path, os.stat_result], ...]]] = [
                (output, identidad_raiz, ())
            ]
            observados: list[tuple[pathlib.Path, os.stat_result]] = []
            archivo_positivo = False
            while pendientes:
                actual, identidad_capturada, ancestros = pendientes.pop()
                for ancestro, identidad_ancestro in ancestros:
                    tipo_ancestro, identidad_actual = link_kind_and_identity_or_raise_with_retry(ancestro)
                    if tipo_ancestro is not None or not same_file_identity(identidad_ancestro, identidad_actual):
                        raise PandoraExecutionError(f"'{ancestro}' cambió durante la inspección de '{output}'.")
                tipo_actual, estado = link_kind_and_identity_or_raise_with_retry(actual)
                if tipo_actual is not None or not same_file_identity(identidad_capturada, estado):
                    raise PandoraExecutionError(f"'{actual}' cambió o es un enlace durante la inspección.")
                assert estado is not None
                observados.append((actual, estado))
                if stat.S_ISDIR(estado.st_mode):
                    with os.scandir(actual) as entradas:
                        tipo_despues, estado_despues = link_kind_and_identity_or_raise_with_retry(actual)
                        if tipo_despues is not None or not same_file_identity(estado, estado_despues):
                            raise PandoraExecutionError(f"'{actual}' cambió durante la inspección.")
                        hijos: list[tuple[pathlib.Path, os.stat_result]] = []
                        for entrada in entradas:
                            identidad_direntry = entrada.stat(follow_symlinks=False)
                            inode_direntry = entrada.inode()
                            hijo = actual / entrada.name
                            tipo_padre, identidad_padre = link_kind_and_identity_or_raise_with_retry(actual)
                            if tipo_padre is not None or not same_file_identity(estado, identidad_padre):
                                raise PandoraExecutionError(f"'{actual}' cambió durante la captura de sus hijos.")
                            tipo_hijo, identidad_hijo = link_kind_and_identity_or_raise_with_retry(hijo)
                            if tipo_hijo is not None or not same_direntry_identity(
                                identidad_direntry, inode_direntry, identidad_hijo
                            ):
                                raise PandoraExecutionError(f"'{hijo}' cambió o es un enlace durante la captura.")
                            tipo_padre, identidad_padre = link_kind_and_identity_or_raise_with_retry(actual)
                            if tipo_padre is not None or not same_file_identity(estado, identidad_padre):
                                raise PandoraExecutionError(f"'{actual}' cambió durante la captura de sus hijos.")
                            assert identidad_hijo is not None
                            hijos.append((hijo, identidad_hijo))
                    nuevos_ancestros = (*ancestros, (actual, estado))
                    pendientes.extend((hijo, identidad, nuevos_ancestros) for hijo, identidad in reversed(hijos))
                elif stat.S_ISREG(estado.st_mode):
                    archivo_positivo = archivo_positivo or estado.st_size > 0
                else:
                    raise PandoraExecutionError(f"'{actual}' no es un archivo regular ni un directorio.")
            for observado, identidad_observada in observados:
                tipo_final, identidad_final = link_kind_and_identity_or_raise_with_retry(observado)
                if tipo_final is not None or not same_file_identity(identidad_observada, identidad_final):
                    raise PandoraExecutionError(f"'{observado}' cambió al finalizar la inspección.")
            if not archivo_positivo:
                raise PandoraExecutionError(f"Pandora no generó ningún archivo regular con contenido en '{output}'.")
            return identidad_raiz
        except PandoraExecutionError:
            raise
        except OSError as exc:
            raise PandoraExecutionError(f"Pandora no dejó un output válido en '{output}': {exc}") from exc

    async def _cerrar_tx_tras_rollback(
        self,
        journal_tx_id: int | None,
        dir_rollbacks: list[DirectoryRollback],
        *,
        hubo_restore: bool,
    ) -> str:
        """Cierra la TX del journal SÓLO si el rollback fue honesto (P1 de #397).

        ``DirectoryRollback`` se traga el ``OSError`` del restore para no enmascarar
        la excepción del body y lo expone vía ``rollback_completed=False``. Marcar
        la TX ``rolled_back`` de todos modos escondería del recovery manual una
        salida parcial que sigue en disco, mientras el audit trail afirma que se
        recuperó — exactamente el defecto que Codex encontró en el hermano de este
        cambio.

        ``hubo_restore`` distingue si el error atravesó una ruta de recuperación:

        - **se esperaba recuperar estado** → cada ``DirectoryRollback`` observado
          participa aunque ``__aenter__`` no haya completado. True cubre tanto
          "preflight falló antes de mutar" como restore/undo confirmado; False
          significa que quedó una mutación sin resolver y la TX queda PENDING.
        - **el cuerpo terminó bien y la excepción vino después** (teardown o
          commit del journal) → no hubo restore y la mutación puede seguir en
          disco. La TX queda PENDING: marcarla ROLLED_BACK también mentiría.

        Devuelve una descripción del desenlace para el log del caller.
        """
        if not hubo_restore:
            logger.critical(
                "Cierre incierto de Pandora (TX %s): sin restore confirmado; TX pendiente.",
                journal_tx_id,
            )
            return "sin restore confirmado; TX pendiente"
        fallidos = [dr for dr in dir_rollbacks if not dr.rollback_completed]
        if not fallidos:
            await self._mark_journal_rolled_back(journal_tx_id)
            return "completado" if dir_rollbacks else "sin salida protegida que revertir"
        logger.critical(
            "Rollback de la salida de Pandora INCOMPLETO (TX %s): %s quedaron sin restaurar. "
            "La TX queda PENDIENTE a propósito, para que el recovery manual la vea.",
            journal_tx_id,
            [str(dr.target) for dr in fallidos],
        )
        return "FALLIDO — la salida parcial sigue en disco"

    @staticmethod
    def _dict_de_resultado(result: PandoraResult) -> dict[str, Any]:
        """Dict de salida del run, compartido por el camino limpio y el de fallo.

        Fuente única para que el ``except _RunFallidoError`` no arme una segunda
        forma del contrato que pudiera divergir de la del retorno normal.

        Contrato compartido (deuda #5): ``message`` canónico junto a los campos
        estructurados; en éxito queda vacío (el consumidor arma su copy).

        El ``message`` lo arma el RUNNER y acá sólo se propaga. Derivarlo de
        ``stderr or stdout`` era el defecto: Pandora es una app GUI y puede no
        escribir a ninguno de los dos, así que un fallo salía con ``message=""``
        y ``normalize_tool_result`` caía a "error desconocido" — el único texto
        que ``AGENTS.md`` declara reservado a su fallback. El runner tiene la
        causa real porque es quien lee ``Engine.log``. Se conserva el fallback a
        stderr/stdout para el caso en que el runner no la haya podido armar.
        """
        return {
            "status": "success" if result.success else "error",
            "success": result.success,
            "message": "" if result.success else (result.message or result.stderr or result.stdout or ""),
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": result.duration_seconds,
            "warnings": list(result.warnings),
        }

    def _ensure_runner(self) -> PandoraRunner:
        """Construye el :class:`PandoraRunner` resolviendo el exe + game path.

        El ejecutable se toma de (en orden) el ``pandora_exe`` inyectado o el resolver
        (``PANDORA_EXE``); el game path siempre del resolver (``SKYRIM_PATH``).

        Raises:
            PandoraExecutionError: Si faltan PANDORA_EXE o SKYRIM_PATH.
        """
        if self._pandora_runner is not None:
            return self._pandora_runner

        if self._path_resolver is None:
            raise PandoraExecutionError(
                "Cannot run Pandora: no pandora_runner injected and no path_resolver configured."
            )

        game_path = self._path_resolver.get_skyrim_path()
        if game_path is None:
            raise PandoraExecutionError("Cannot run Pandora: SKYRIM_PATH is not configured.")

        pandora_exe = self._pandora_exe or self._path_resolver.get_pandora_exe()
        if pandora_exe is None:
            raise PandoraExecutionError("Cannot run Pandora: PANDORA_EXE is not configured.")

        self._pandora_runner = PandoraRunner(
            PandoraConfig(pandora_exe=pandora_exe, game_path=game_path),
        )
        return self._pandora_runner

    # ------------------------------------------------------------------
    # Caja negra de vuelo (T-26/T-28, ADR 0002) — espejo de loot_service
    # ------------------------------------------------------------------

    async def _emit_action_manifest(
        self,
        tx: SnapshotTransactionLock,
        target_files: list[pathlib.Path],
    ) -> int:
        """Construye y persiste el ActionManifest de Pandora dentro del lock (T-26).

        Se llama ANTES de ``runner.run_pandora()`` con el journal ya cableado.
        Devuelve el id de la transacción del journal para commit/rollback posterior.

        NOTA: ``tx.snapshots`` queda vacío por diseño (``target_files=[]`` en el
        lock) y eso **no** significa "sin rollback" — el motivo ya no es la premisa
        falsa de U-01 (*"la salida es dependiente del entorno"*), que Pandora
        desmiente escribiendo físicamente. El rollback real de la salida lo hace el
        move-aside de ``DirectoryRollback`` (U-04), que no pasa por el snapshot
        store porque un árbol de behavior graphs no es un archivo. Los dos
        conceptos se separan igual que en ``dyndolod_service``: el manifiesto
        registra el único output explícito (``files_touched``, para auditoría);
        revertirlo es trabajo del move-aside.

        Se emite ANTES del move-aside (primera mutación de FS): si el proceso muere
        en el gap, el journal ya tiene el manifiesto de la mutación (review Codex
        #312 sobre el hermano DynDOLOD).

        Raises:
            _ActionManifestError: Si begin_transaction/persist falla — Pandora no
                debe proceder sin la caja negra emitida. La TX recién abierta se
                marca rolled-back para no dejarla PENDING (espejo de loot_service).
        """
        from sky_claw.app.orchestrator.preview.action_manifest import build_action_manifest

        assert self._journal is not None  # cableado verificado por el caller
        journal_tx_id: int | None = None
        try:
            journal_tx_id = await self._journal.begin_transaction(
                description="pandora_animations",
                agent_id=self.AGENT_ID,
            )
            manifest = build_action_manifest(
                ritual_id=f"pandora-animations-{journal_tx_id}",
                tool="Pandora",
                tool_version=None,  # Pandora no expone versión hoy (follow-up menor).
                target_files=[str(f) for f in target_files],
                snapshots=tx.snapshots,  # [] por el snapshot diferido — plan de rollback vacío por diseño.
                summary="Regenerar behavior graphs (animaciones) con Pandora.",
            )
            await self._journal.persist_action_manifest(
                manifest,
                agent_id=self.AGENT_ID,
                transaction_id=journal_tx_id,
            )
            return journal_tx_id
        except Exception as exc:  # noqa: BLE001 — boundary: cualquier fallo del journal/builder
            # No dejar la TX recién abierta en PENDING; convertir a _ActionManifestError
            # para que el caller devuelva un dict serializable en vez de propagar.
            await self._mark_journal_rolled_back(journal_tx_id)
            raise _ActionManifestError(str(exc)) from exc

    async def _emit_flight_report(self, journal_tx_id: int) -> None:
        """Compone y persiste el FlightReport de Pandora ya terminado (T-28).

        Post-vuelo y best-effort: lee la caja negra desde el journal (el manifiesto
        persistido + el estado REAL de la TX). Un fallo se loguea y NO rompe un run
        ya exitoso (misma disciplina que LOOT/DynDOLOD).
        """
        from sky_claw.app.orchestrator.preview.flight_report import (
            compose_flight_report_from_journal,
        )

        assert self._journal is not None  # cableado verificado por el caller
        try:
            report = await compose_flight_report_from_journal(self._journal, transaction_id=journal_tx_id)
            await self._journal.persist_flight_report(
                report,
                agent_id=self.AGENT_ID,
                transaction_id=journal_tx_id,
            )
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error("Fallo al persistir el informe de vuelo de la TX %d", journal_tx_id, exc_info=True)

    async def _mark_journal_rolled_back(self, journal_tx_id: int | None) -> None:
        """Marca la transacción del journal como rolled-back (best-effort).

        Se llama en los caminos de excepción; si el journal falla acá (sqlite/IO)
        NO debe enmascarar el error original ni romper el contrato de dict
        serializable (espejo de loot_service._mark_journal_rolled_back).
        """
        if journal_tx_id is None or self._journal is None:
            return
        try:
            await self._journal.mark_transaction_rolled_back(journal_tx_id)
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error("Fallo al marcar la TX del journal %d como rolled-back", journal_tx_id, exc_info=True)

    async def generate_animations(self) -> dict[str, Any]:
        """Regenera las animaciones con Pandora bajo el lock de behavior-graphs.

        Siempre devuelve un ``dict`` serializable para los modos de fallo conocidos
        (runner no disponible, contención de lock, error de ejecución) en vez de
        propagar la excepción, para que el dispatcher lo reenvíe verbatim.
        """
        # Preflight brutal ANTES de tocar nada (T-16c·4, STAGE 4): un semáforo ROJO
        # (p. ej. el dir de salida de behaviors sin permisos) cancela Pandora sin
        # correr el subproceso ni tomar el lock. Amarillo/verde no bloquean; el
        # reporte se surface al panel en todos los retornos.
        preflight = self._ensure_preflight()
        preflight_report: PreflightReport | None = None
        if preflight is not None:
            preflight_report = await preflight.run()
            if preflight_report.blocks_mutations:
                red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                logger.warning("Pandora (stage 4) bloqueado por preflight en rojo: %s", red)
                return {
                    "status": "error",
                    "success": False,
                    "reason": "PreflightBlocked",
                    "message": f"Preflight en rojo, Pandora cancelado: {red}",
                    "logs": red,
                    "preflight": preflight_report.to_dict(),
                }

        try:
            runner = self._ensure_runner()
        except PandoraExecutionError as exc:
            logger.error("Pandora runner unavailable: %s", exc)
            return _attach_preflight(
                {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}, preflight_report
            )

        managed_output = self._managed_output()
        if managed_output is None:
            detail = "Cannot run Pandora: SKYRIM_PATH is not configured."
            logger.error("Pandora runner unavailable: %s", detail)
            return _attach_preflight(
                {"status": "error", "success": False, "message": detail, "logs": detail}, preflight_report
            )
        rollback_outputs = [managed_output]

        # Lock ligado a una variable para leer sus snapshots (manifiesto) tras el with.
        tx = SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.RESOURCE_ID,
            agent_id=self.AGENT_ID,
            target_files=[],  # snapshot diferido — ver docstring del módulo
            metadata={"source": "pandora_animations"},
        )
        journal_tx_id: int | None = None
        # Una vez commiteada la TX, ninguna ruta posterior debe re-marcarla rolled-back:
        # una cancelación mientras se compone/persiste el informe (post-commit)
        # corrompería el audit trail de una TX ya exitosa (review Codex #249/#318).
        journal_committed = False
        # U-04: se rastrean para leer el resultado REAL del rollback
        # (dr.rollback_completed) en vez de asumir que hubo excepción ⇒ se revirtió.
        # El AsyncExitStack corre los __aexit__ (restore) ANTES que los except
        # handlers, así que los flags ya están seteados cuando se leen.
        dir_rollbacks: list[DirectoryRollback] = []
        identidades_validadas: dict[pathlib.Path, os.stat_result] = {}
        # Distingue "el cuerpo falló y el restore se intentó" de "el cuerpo terminó
        # y reventó el teardown" — sin esto, un LockLeaseLostError tras un run
        # exitoso dejaba la TX PENDING como si el rollback hubiera fallado, cuando
        # nunca hubo restore que hacer (review CodeRabbit #399).
        run_exitoso = False
        try:
            # AsyncExitStack: el lock se adquiere primero y se libera último, así los
            # move-aside se restauran DENTRO del lock (espejo de dyndolod_service).
            async with contextlib.AsyncExitStack() as tx_stack:
                await tx_stack.enter_async_context(tx)
                # T-26 (ADR 0002): emitir la caja negra ANTES de mutar. Si el journal
                # está cableado y la emisión falla, Pandora NO corre (se lanza dentro
                # del lock → __aexit__ libera; nada mutó todavía). El path del agente
                # (sin journal) salta esto — no hay caja negra que emitir.
                if self._journal is not None:
                    journal_tx_id = await self._emit_action_manifest(tx, [managed_output])

                # U-04: move-aside de los árboles de salida (primera mutación de FS,
                # después del manifiesto a propósito). Fail-closed: si el rename no
                # se puede hacer, DirectoryRollback lanza OSError y Pandora no corre
                # — mejor no correr que correr sin el rollback que se prometió.
                for output_dir in rollback_outputs:
                    # El veto de lease hace que el move-aside y el lock que lo
                    # envuelve apliquen el MISMO criterio: el lock saltea su
                    # rollback tras perder la lease para no pisar a un dueño
                    # concurrente, pero este context sale ANTES que él y sin el
                    # veto restauraría igual (review Codex #399).
                    def _target_final_valido(output: pathlib.Path = output_dir) -> bool:
                        esperada = identidades_validadas.get(output)
                        if esperada is None:
                            return False
                        tipo_actual, identidad_actual = link_kind_and_identity_or_raise_with_retry(output)
                        return tipo_actual is None and same_file_identity(esperada, identidad_actual)

                    dr = DirectoryRollback(
                        output_dir,
                        should_rollback=lambda: not tx.lease_lost,
                        validate_final_target=_target_final_valido,
                    )
                    dir_rollbacks.append(dr)
                    await tx_stack.enter_async_context(dr)

                result = await runner.run_pandora()

                # U-04: el puente fallo→excepción. Sin esto, un exit non-zero sale
                # limpio del stack, los DirectoryRollback DESCARTAN el backup y el
                # árbol parcial queda en disco. Ver _RunFallidoError.
                if not result.success:
                    raise _RunFallidoError(result)
                identidades_validadas[managed_output] = await asyncio.to_thread(
                    self._validar_output_generado, managed_output
                )
                run_exitoso = True
            finalizaciones_fallidas = [dr for dr in dir_rollbacks if not dr.finalization_completed]
            if finalizaciones_fallidas:
                raise PandoraExecutionError(
                    "Pandora generó salida, pero no pudo finalizar de forma segura "
                    "la protección de rollback; la transacción queda pendiente."
                )
            # Lock liberado y salida confirmada: el run fue exitoso (un fallo habría
            # salido por _RunFallidoError / PandoraExecutionError).
            if journal_tx_id is not None and self._journal is not None:
                # El run ya terminó y el lock se liberó; un fallo de commit es de
                # estado (el manifiesto ya quedó persistido), best-effort (no rompe
                # el contrato "siempre devolver dict"). Espejo de loot_service.
                try:
                    await self._journal.commit_transaction(journal_tx_id)
                    journal_committed = True
                except Exception:  # noqa: BLE001 — boundary best-effort del journal
                    logger.error(
                        "Fallo al commitear la TX del journal %d tras Pandora exitoso",
                        journal_tx_id,
                        exc_info=True,
                    )
                # T-28: cerrar la caja negra con el informe post-vuelo (best-effort).
                await self._emit_flight_report(journal_tx_id)
        except LockAcquisitionError as exc:
            # La contención ocurre en __aenter__, antes de emitir el manifiesto:
            # journal_tx_id sigue None, no hay TX que revertir.
            logger.warning("Lock contention on '%s': %s", self.RESOURCE_ID, exc)
            detail = f"Could not acquire behavior-graphs lock '{self.RESOURCE_ID}': {exc}"
            return _attach_preflight(
                {"status": "error", "success": False, "message": detail, "logs": detail}, preflight_report
            )
        except _ActionManifestError as exc:
            # La caja negra no se pudo emitir: Pandora no corrió (fail-closed). La TX
            # recién abierta ya fue marcada rolled-back dentro de _emit_action_manifest.
            logger.error("Pandora (stage 4): no se pudo emitir el ActionManifest; abortado: %s", exc)
            detail = f"Manifiesto de vuelo requerido no emitido: {exc}"
            return _attach_preflight(
                {
                    "status": "error",
                    "success": False,
                    "reason": "ActionManifestFailed",
                    "message": detail,
                    "logs": detail,
                },
                preflight_report,
            )
        except _RunFallidoError as exc:
            # U-04: exit non-zero. El move-aside ya restauró (o no, y el cierre de la
            # TX lo refleja). Se devuelve el MISMO dict que el camino limpio.
            desenlace = await self._cerrar_tx_tras_rollback(journal_tx_id, dir_rollbacks, hubo_restore=True)
            logger.error("Pandora falló (rc=%s); rollback de la salida: %s", exc.result.return_code, desenlace)
            return _attach_preflight(self._dict_de_resultado(exc.result), preflight_report)
        except PandoraExecutionError as exc:
            # Puede venir del runner DENTRO del stack (incluye PandoraTimeoutError,
            # U-10), tras atravesar los DirectoryRollback, o de la comprobación de
            # finalización DESPUÉS de un run exitoso. Este segundo caso no atravesó
            # una ruta de restore y debe quedar PENDING sin diagnosticar falsamente
            # una salida parcial.
            desenlace = await self._cerrar_tx_tras_rollback(
                journal_tx_id,
                dir_rollbacks,
                hubo_restore=not run_exitoso,
            )
            logger.error("Pandora execution failed: %s; desenlace de la salida: %s", exc, desenlace)
            return _attach_preflight(
                {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}, preflight_report
            )
        except asyncio.CancelledError:
            # Cancelación (shutdown / timeout del task) mientras corría run_pandora o
            # se cerraba la caja negra: cerrar la TX del journal para no dejarla PENDING
            # (review Codex #318). Si ya se commiteó (cancelación durante el informe
            # post-vuelo) NO se revierte — la TX fue exitosa y el audit trail no debe
            # mentir (#249). El __aexit__ del lock ya liberó; se re-lanza la cancelación.
            if not journal_committed:
                await self._cerrar_tx_tras_rollback(journal_tx_id, dir_rollbacks, hubo_restore=not run_exitoso)
            raise
        except Exception as exc:  # noqa: BLE001 — T11: SIEMPRE devolver dict serializable
            # Red de seguridad final: incluye LockLeaseLostError que __aexit__ del lock
            # puede lanzar en una salida limpia si el heartbeat perdió el lease durante
            # el run (renovación fallida / otro agente reclamó el lock). No es
            # LockAcquisitionError ni PandoraExecutionError, así que sin este catch
            # burbujearía rompiendo el contrato y dejaría la TX PENDING (review Codex
            # #318). Si no se commiteó, cerrar la TX según el rollback real (la
            # exclusividad no estuvo garantizada — reportar éxito mentiría).
            if not journal_committed:
                await self._cerrar_tx_tras_rollback(journal_tx_id, dir_rollbacks, hubo_restore=not run_exitoso)
            logger.error("Error inesperado en Pandora: %s", exc, exc_info=True)
            return _attach_preflight(
                {"status": "error", "success": False, "message": str(exc), "logs": str(exc)}, preflight_report
            )

        return _attach_preflight(self._dict_de_resultado(result), preflight_report)

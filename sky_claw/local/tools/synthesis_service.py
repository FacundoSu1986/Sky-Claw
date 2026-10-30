"""SynthesisPipelineService — servicio dedicado para el pipeline de Synthesis.

Extraído de ``supervisor.py`` como parte del Sprint 2 (Strangler Fig).
Reemplaza el manejo manual de snapshots/rollback con
:class:`SnapshotTransactionLock` para atomicidad y seguridad concurrente.

Diseño (LÓGICA, ARQUITECTURA, PREVENCIÓN):

LÓGICA:
    El pipeline de Synthesis genera un ESP (``Synthesis.esp``) combinando
    múltiples patchers. Si la ejecución falla o el ESP resultante está
    corrupto, se restaura automáticamente el snapshot previo.

ARQUITECTURA:
    Todas las dependencias se inyectan vía constructor (DI). El servicio
    no posee infraestructura propia — sólo coordina.

PREVENCIÓN:
    Las excepciones de dominio (``SynthesisExecutionError``,
    ``SynthesisValidationError``) burbujean hacia el ``__aexit__`` del
    ``SnapshotTransactionLock`` para activar rollback automático.
    No se captura ``Exception`` genérica dentro del context manager.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import pathlib
import time
from typing import TYPE_CHECKING, Any

from sky_claw.app.core.event_bus import CoreEventBus, Event
from sky_claw.app.core.event_payloads import (
    SynthesisPipelineCompletedPayload,
    SynthesisPipelineStartedPayload,
)
from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.app.security.path_validator import PathViolationError, assert_safe_component
from sky_claw.local.tools.output_targets import synthesis_output_target
from sky_claw.local.tools.patcher_pipeline import PatcherPipeline
from sky_claw.local.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisExecutionError,
    SynthesisResult,
    SynthesisRunner,
    SynthesisValidationError,
)

if TYPE_CHECKING:
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.db.journal import OperationJournal
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.validators.preflight import PreflightCheck, PreflightReport, PreflightService

logger = logging.getLogger(__name__)

# Directorio de staging de backups (compartido con supervisor)
_BACKUP_STAGING_DIR = ".skyclaw_backups/"


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service``/``xedit_service`` (T-16b/T-16c·1): un
    semáforo verde no ensucia el dict; amarillo/rojo viajan como
    ``result["preflight"]`` para que el panel vivo lo renderice.
    """
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


class _ActionManifestError(Exception):
    """Interno (T-26): la emisión del manifiesto de vuelo falló. Se lanza DENTRO
    del lock (antes de mutar) para que el Ritual NO proceda sin manifiesto — la
    caja negra no es opcional cuando el journal está cableado (espejo de
    ``loot_service``/``xedit_service._ActionManifestError``)."""


class SynthesisPipelineService:
    """Servicio dedicado para la ejecución del pipeline de Synthesis.

    Coordina ``SynthesisRunner``, ``PatcherPipeline``,
    ``SnapshotTransactionLock`` y ``CoreEventBus`` para ejecutar
    el pipeline con protección transaccional y observabilidad.
    """

    RESOURCE_ID: str = "Synthesis.esp"
    AGENT_ID: str = "synthesis-service"

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal,
        path_resolver: PathResolutionService,
        event_bus: CoreEventBus,
        pipeline_config_path: pathlib.Path | None = None,
        output_path: pathlib.Path | None = None,
        preflight: PreflightService | None = None,
        synthesis_settings_path: pathlib.Path | None = None,
        synthesis_profile_identifier: str | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._path_resolver = path_resolver
        self._event_bus = event_bus
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        self._preflight = preflight
        self._pipeline_config_path = pipeline_config_path or (
            pathlib.Path(_BACKUP_STAGING_DIR) / "synthesis_pipeline.json"
        )
        # T-27b: override del destino de salida. El sandbox de T-27 pasa
        # `SandboxClone.overwrite_copy` para que el run escriba en la copia y
        # no en el overwrite real (garantía de aislamiento). None = el cálculo
        # de siempre (overwrite real / "Synthesis Output"). Fijo por instancia:
        # un run sandboxeado construye su propio servicio (patrón ad-hoc de
        # system_tools.run_pandora).
        self._output_path = output_path
        # Settings V2 de Synthesis (no el JSON interno de PatcherPipeline).
        # La ruta explícita/env gana; si falta, se alinea con el default GUI
        # upstream: PipelineSettings.json en el directorio de trabajo actual.
        self._synthesis_settings_path = synthesis_settings_path
        # MO2 y Synthesis mantienen identificadores de perfil independientes.
        # El alias del perfil MO2 solo se usa como convención por defecto; si
        # difieren, se puede mapear explícitamente por parámetro o entorno.
        self._synthesis_profile_identifier = synthesis_profile_identifier

        # Lazy init — paths may not be available at construction time
        self._synthesis_runner: SynthesisRunner | None = None
        self._patcher_pipeline: PatcherPipeline | None = None

    # ------------------------------------------------------------------
    # Lazy initialization
    # ------------------------------------------------------------------

    def _ensure_synthesis_runner(self) -> SynthesisRunner:
        """Inicializa lazily el SynthesisRunner validando paths del entorno.

        Returns:
            SynthesisRunner inicializado.

        Raises:
            SynthesisExecutionError: Si las variables de entorno son inválidas.
        """
        if self._synthesis_runner is not None:
            return self._synthesis_runner

        game_path = self._path_resolver.get_skyrim_path()
        # Raíz de DATOS de la instancia (no instalación): el destino
        # (overwrite o mods/) cuelga de los datos. Ver output_targets.
        mo2_path = self._path_resolver.get_mo2_instance_data_root()
        synthesis_exe = self._path_resolver.get_synthesis_exe()

        if not game_path or not mo2_path or not synthesis_exe:
            raise SynthesisExecutionError(
                "Cannot initialize SynthesisRunner: "
                "SKYRIM_PATH, MO2_PATH, and SYNTHESIS_EXE environment variables "
                "must be valid paths"
            )

        if not synthesis_exe.exists():
            raise SynthesisExecutionError(f"Synthesis executable not found: {synthesis_exe}")

        # T-27b: el override (sandbox) manda; sin él, el destino de siempre.
        # Fuente única compartida con _ensure_preflight (U-01 parte 2): estaban
        # copiados con un comentario "mismo cálculo que…", y el preflight sondeando
        # un destino distinto del que el runner escribe es un falso verde silencioso.
        # mods_dir llega como callable: solo se resuelve en la rama mods (sin
        # overwrite), donde la declaración del INI es load-bearing — con
        # mod_directory declarado e irresoluble aborta con evidencia vía
        # get_mo2_mods_path_para_destino (no fabrica <datos>/mods en silencio).
        output_path = synthesis_output_target(
            mo2=mo2_path,
            override=self._output_path,
            mods_dir=self._path_resolver.get_mo2_mods_path_para_destino,
        )
        assert output_path is not None  # mo2_path ya validado arriba

        mo2_profile = self._path_resolver.get_active_profile()
        if not isinstance(mo2_profile, str) or not mo2_profile.strip():
            raise SynthesisExecutionError("Cannot initialize SynthesisRunner: the active MO2 profile is unresolved")
        try:
            mo2_profile = assert_safe_component(mo2_profile.strip(), field="active MO2 profile")
        except PathViolationError as exc:
            raise SynthesisExecutionError(f"Cannot resolve active MO2 profile safely: {exc}", stderr=str(exc)) from exc

        if self._synthesis_profile_identifier is not None:
            configured_profile = self._synthesis_profile_identifier
        elif "SYNTHESIS_PROFILE_IDENTIFIER" in os.environ:
            configured_profile = os.environ["SYNTHESIS_PROFILE_IDENTIFIER"]
        else:
            configured_profile = mo2_profile
        if not isinstance(configured_profile, str) or not configured_profile.strip():
            raise SynthesisExecutionError("SYNTHESIS_PROFILE_IDENTIFIER must be a non-empty Synthesis nickname or ID")
        synthesis_profile = configured_profile.strip()

        load_order_file = mo2_path / "profiles" / mo2_profile / "plugins.txt"
        if load_order_file.is_symlink() or not load_order_file.is_file():
            raise SynthesisExecutionError(
                f"The active MO2 profile {mo2_profile!r} has no regular plugins.txt under {mo2_path / 'profiles'}"
            )

        configured_settings = self._synthesis_settings_path or os.environ.get("SYNTHESIS_PIPELINE_SETTINGS")
        settings_path = (
            pathlib.Path(configured_settings) if configured_settings else pathlib.Path.cwd() / "PipelineSettings.json"
        )
        config = SynthesisConfig(
            game_path=game_path,
            mo2_path=mo2_path,
            output_path=output_path,
            synthesis_exe=synthesis_exe,
            timeout_seconds=300,
            profile=synthesis_profile,
            pipeline_settings_path=settings_path,
            data_folder_path=game_path / "Data",
            load_order_file_path=load_order_file,
            mo2_profile=mo2_profile,
        )

        self._synthesis_runner = SynthesisRunner(config)
        logger.info(
            "SynthesisRunner inicializado: game=%s, output=%s",
            game_path,
            output_path,
        )
        return self._synthesis_runner

    def _ensure_patcher_pipeline(self) -> PatcherPipeline:
        """Inicializa lazily el PatcherPipeline.

        Returns:
            PatcherPipeline inicializado.
        """
        if self._patcher_pipeline is not None:
            return self._patcher_pipeline

        if self._pipeline_config_path.exists():
            self._patcher_pipeline = PatcherPipeline.from_json(self._pipeline_config_path)
            logger.info(
                "PatcherPipeline cargado desde %s: %d patchers",
                self._pipeline_config_path,
                len(self._patcher_pipeline),
            )
        else:
            self._patcher_pipeline = PatcherPipeline(pipeline_config_path=self._pipeline_config_path)
            logger.info("PatcherPipeline inicializado vacío")

        return self._patcher_pipeline

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight de Synthesis (T-16c·2, STAGE 7).

        Synthesis procesa TODO el modlist, así que los sensores relevantes son:
        **permisos de escritura sobre el output** (overwrite / clon del sandbox —
        si no es escribible, Synthesis muere generando ``Synthesis.esp``),
        **límites full/light del load order** (salud general del engine: un load
        order ya al/por encima del límite de slots es un problema real),
        **masters faltantes**, **overwrite sucio** y **symlinks/junctions**. NO
        cablea la versión de LOOT (irrelevante para Synthesis). Reusa las
        primitivas compartidas; no toca ``loot_service``. Sensores no resolubles →
        ``None`` (omitidos con ``omit_unconfigured``).

        ``plugin_limits`` mide slots full/light del engine, NO el fan-in de
        masters del ESP generado. ``execute_pipeline`` agrega después un check
        separado con la cota superior de plugins efectivos del perfil MO2 y el
        bit ``SplitIfMaxMastersExceeded`` del perfil Synthesis V2.
        """
        if self._preflight is not None:
            return self._preflight

        game = self._path_resolver.get_skyrim_path()
        # Raíz de DATOS (no instalación): overwrite, perfil y fuentes cuelgan
        # de la instancia. El raw usa el mismo árbol (no existe representación
        # cruda sin resolver de la metadata; ver get_mo2_instance_data_root).
        mo2 = self._path_resolver.get_mo2_instance_data_root()
        if not isinstance(game, pathlib.Path) or not isinstance(mo2, pathlib.Path):
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

        # vfs sobre el árbol de DATOS (el que el runner opera) — builder
        # compartido (T-16d): coacciona no-Path y guarda "al menos una raíz".
        # scan_mods_dir: la raíz MO2 de acá ya está VALIDADA (el guard de arriba
        # exige un Path de datos), así que enumerar mods/ es seguro — el False
        # hardcodeado dejaba ciego el scan de symlinks (U-01).
        # mods_dir: el MODS_DIR DECLARADO (mod_directory custom ≠ <datos>/mods).
        mods_dir = self._path_resolver.get_mo2_mods_path_best_effort()
        vfs_checker = build_vfs_sensor(
            raw_game=self._path_resolver.get_skyrim_path_raw(),
            raw_mo2=mo2,
            scan_mods_dir=True,
            mods_dir=mods_dir,
        )

        # Output real donde Synthesis escribe — MISMO resolver y MISMA fuente
        # del destino que _ensure_synthesis_runner (U-01 parte 2), incluido el
        # callable de mods (rama mods solo; aborta con evidencia si la
        # declaración del INI es irresoluble — B de #555).
        output_dir = synthesis_output_target(
            mo2=mo2,
            override=self._output_path,
            mods_dir=self._path_resolver.get_mo2_mods_path_para_destino,
        )
        assert output_dir is not None  # mo2 ya validado por el guard de arriba

        def _permissions():
            # Re-probe por llamada (freshness): un cambio de permisos entre corridas se ve.
            return WritePermissionsChecker(targets=[output_dir]).check()

        overwrite_check = build_overwrite_sensor(mo2 / "overwrite")
        masters_check, limits_check = self._build_modlist_checks(game, mo2, mods_dir)
        # Synthesis (stage 7) corre sobre el load order ya estabilizado por LOOT
        # y parcheado por Wrye Bash: un master invertido es CTD, no un warning.
        order_check = self._build_master_order_check(game, mo2, mods_dir)

        # U-01: Synthesis procesa TODO el modlist; sin la USVFS heredada el patch
        # saldría del juego base. Mismo perfil que alimenta masters/límites.
        visibility_check = build_vfs_visibility_sensor(
            game=game,
            sources_resolver=build_mo2_profile_sources_resolver(
                game=game,
                mo2=mo2,
                profile=self._path_resolver.get_active_profile(),
                mods_dir=mods_dir,
            ),
        )

        self._preflight = PreflightService(
            vfs_checker=vfs_checker,
            permissions_check=_permissions,
            overwrite_check=overwrite_check,
            masters_check=masters_check,
            limits_check=limits_check,
            order_check=order_check,
            visibility_check=visibility_check,
            omit_unconfigured=True,
        )
        return self._preflight

    def _build_master_order_check(
        self,
        game: pathlib.Path,
        mo2: pathlib.Path,
        mods_dir: pathlib.Path | None = None,
    ) -> Any:
        """Closure del sensor de orden de masters del **perfil MO2 activo**.

        Mismo feed y mismo gate que :meth:`_build_modlist_checks`; sin
        perfil/fuentes resolubles → ``None`` → checkpoint "no configurado"
        (omitido). No miente verde (lección #250). ``mods_dir``: el MODS_DIR
        declarado por la instancia (ver builders T-16d).
        """
        from sky_claw.local.validators.preflight_sensors import (
            build_master_order_sensor,
            build_mo2_profile_sources_resolver,
        )

        resolver = build_mo2_profile_sources_resolver(
            game=game,
            mo2=mo2,
            profile=self._path_resolver.get_active_profile(),
            mods_dir=mods_dir,
        )
        if resolver is None:
            return None
        return build_master_order_sensor(resolver)

    def _build_modlist_checks(
        self,
        game: pathlib.Path,
        mo2: pathlib.Path,
        mods_dir: pathlib.Path | None = None,
    ) -> tuple[Any, Any]:
        """Closures de masters/límites del **perfil MO2 activo**.

        Lee el load order de ``profiles/<perfil>/plugins.txt`` — NO el
        ``%LOCALAPPDATA%\\plugins.txt`` global que reescribe LOOT fuera del VFS
        (review Codex #306). Delega en los builders compartidos (T-16c·3): el
        resolver del perfil MO2 y el cableado de masters/límites son idénticos a
        los de DynDOLOD. Sin perfil/fuentes resolubles → ``(None, None)`` →
        checkpoints "no configurado" (omitidos). No miente verde (lección #250).
        ``mods_dir``: el MODS_DIR declarado por la instancia (ver builders T-16d).
        """
        from sky_claw.local.validators.preflight_sensors import (
            build_mo2_profile_sources_resolver,
            build_modlist_sensors,
        )

        resolver = build_mo2_profile_sources_resolver(
            game=game,
            mo2=mo2,
            profile=self._path_resolver.get_active_profile(),
            mods_dir=mods_dir,
        )
        if resolver is None:
            return None, None
        return build_modlist_sensors(resolver)

    def _build_synthesis_visibility_check(self, runner: SynthesisRunner) -> PreflightCheck:
        """Fail closed si el proceso standalone no vería todos los plugins activos.

        ``SynthesisRunner`` usa ``run_capture`` y no hereda el overlay de MO2.
        El sensor genérico acepta visibilidad parcial para otros rituales; acá no
        alcanza: Synthesis procesa el modlist completo y una vista parcial puede
        producir un patch incorrecto. Hasta que exista un backend brokered para
        Synthesis, cada plugin de mod activo debe estar en el Data físico.
        """
        from sky_claw.local.validators.preflight import PreflightCheck as _PreflightCheck
        from sky_claw.local.validators.preflight import PreflightStatus as _PreflightStatus
        from sky_claw.local.validators.preflight_sensors import build_mo2_profile_sources_resolver
        from sky_claw.local.validators.vfs_visibility import VfsVisibilityChecker

        config = runner._config
        mods_dir = self._path_resolver.get_mo2_mods_path_best_effort()
        sources_resolver = build_mo2_profile_sources_resolver(
            game=config.game_path,
            mo2=config.mo2_path,
            profile=config.mo2_profile or config.profile,
            mods_dir=mods_dir,
        )
        if sources_resolver is None:
            return _PreflightCheck(
                name="synthesis_vfs_visibility",
                status=_PreflightStatus.RED,
                summary="No se pudo resolver el perfil MO2 para probar la visibilidad de Synthesis.",
            )

        try:
            sources = sources_resolver()
        except (OSError, RuntimeError, ValueError) as exc:
            return _PreflightCheck(
                name="synthesis_vfs_visibility",
                status=_PreflightStatus.RED,
                summary=f"No se pudo leer el load order del perfil MO2: {exc}",
            )
        if sources.activation_source_status == "unreadable":
            return _PreflightCheck(
                name="synthesis_vfs_visibility",
                status=_PreflightStatus.RED,
                summary="No se pudo probar la visibilidad: plugins.txt del perfil MO2 es ilegible.",
            )

        data_folder = config.data_folder_path
        if data_folder is None or not data_folder.is_dir():
            return _PreflightCheck(
                name="synthesis_vfs_visibility",
                status=_PreflightStatus.RED,
                summary=f"No se pudo probar la visibilidad de Synthesis en Data: {data_folder}",
            )
        scan = VfsVisibilityChecker(
            game_data_dir=data_folder,
            enabled_plugins=sources.effective_enabled_plugins,
        ).check()
        missing = tuple(
            name for name in scan.mod_plugins if name.casefold() not in {item.casefold() for item in scan.visible}
        )
        if missing:
            sample = ", ".join(missing[:10])
            return _PreflightCheck(
                name="synthesis_vfs_visibility",
                status=_PreflightStatus.RED,
                summary=(
                    f"Synthesis corre fuera de USVFS y {len(missing)}/{len(scan.mod_plugins)} "
                    "plugin(s) activos de mods no están visibles en el Data físico."
                ),
                details=(sample,),
            )
        return _PreflightCheck(
            name="synthesis_vfs_visibility",
            status=_PreflightStatus.GREEN,
            summary=f"Todos los {len(scan.mod_plugins)} plugin(s) activos de mods son visibles en el Data físico.",
        )

    def _build_master_overflow_check(self, runner: SynthesisRunner, patcher_ids: list[str]) -> PreflightCheck:
        """Gatea el fan-in conservador del perfil y el auto-split del settings V2.

        No existe un fan-in exacto antes de ejecutar: depende de qué records use
        cada patcher. El número de plugins efectivos de MO2 es una cota superior.
        Si esa cota rebasa 254 y el auto-split no está demostrado como activo, se
        bloquea antes de abrir una transacción. Con auto-split habilitado, el
        runner administra el conjunto ``Grupo.esp`` + ``Grupo_2.esp`` + ….
        """
        from sky_claw.local.validators.preflight import PreflightCheck as _PreflightCheck
        from sky_claw.local.validators.preflight import PreflightStatus as _PreflightStatus
        from sky_claw.local.validators.preflight_sensors import build_mo2_profile_sources_resolver

        profile = runner.inspect_settings(patcher_ids)
        config = runner._config
        mods_dir = self._path_resolver.get_mo2_mods_path_best_effort()
        sources_resolver = build_mo2_profile_sources_resolver(
            game=config.game_path,
            mo2=config.mo2_path,
            profile=config.mo2_profile or config.profile,
            mods_dir=mods_dir,
        )

        fan_in_upper_bound: int | None = None
        if sources_resolver is not None:
            try:
                sources = sources_resolver()
                if sources.activation_source_status != "unreadable" and sources.plugin_dirs:
                    fan_in_upper_bound = len(sources.effective_enabled_plugins)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("No se pudo estimar el fan-in de masters de Synthesis: %s", exc)

        details = ["La cantidad de plugins efectivos es una cota superior, no el fan-in exacto del patcher."]
        if fan_in_upper_bound is not None:
            details.append(f"Cota superior del perfil MO2: {fan_in_upper_bound} plugin(s); límite: 254 masters.")
        else:
            details.append("No se pudo resolver una fuente de plugins efectiva del perfil MO2.")

        split_enabled = profile.split_if_max_masters_exceeded
        if fan_in_upper_bound is not None and fan_in_upper_bound <= 254:
            status = _PreflightStatus.GREEN
            summary = f"Cota de fan-in {fan_in_upper_bound}/254; no se prevé overflow de masters."
        elif split_enabled is True:
            status = _PreflightStatus.YELLOW
            summary = "Fan-in de masters potencialmente superior a 254; auto-split de Synthesis habilitado."
        else:
            status = _PreflightStatus.RED
            if split_enabled is False:
                summary = "Fan-in potencialmente superior a 254 y SplitIfMaxMastersExceeded está deshabilitado."
            else:
                summary = "Fan-in no demostrable bajo el límite y SplitIfMaxMastersExceeded no está confirmado."

        return _PreflightCheck(
            name="synthesis_master_overflow",
            status=status,
            summary=summary,
            details=tuple(details),
        )

    @staticmethod
    def _append_preflight_check(report: PreflightReport | None, check: PreflightCheck) -> PreflightReport:
        """Agrega el sensor Synthesis al semáforo sin mutar el reporte original."""
        from sky_claw.local.validators.preflight import PreflightReport as _PreflightReport
        from sky_claw.local.validators.preflight import PreflightStatus as _PreflightStatus

        checks = (*report.checks, check) if report is not None else (check,)
        ranks = {
            _PreflightStatus.GREEN: 0,
            _PreflightStatus.YELLOW: 1,
            _PreflightStatus.RED: 2,
        }
        status = max((item.status for item in checks), key=ranks.__getitem__)
        return _PreflightReport(status=status, checks=checks)

    @staticmethod
    def _cleanup_synthesis_outputs(runner: SynthesisRunner, group_names: tuple[str, ...]) -> bool:
        """Retira salidas del perfil fijado para el run, incluso si cambió el JSON."""
        try:
            outputs = runner._discover_output_files_for_groups(group_names)
        except (SynthesisExecutionError, SynthesisValidationError, OSError) as exc:
            logger.critical("No se pudo descubrir el conjunto de salidas Synthesis para cleanup: %s", exc)
            return False

        completed = True
        for output in outputs:
            try:
                output.unlink(missing_ok=True)
            except OSError as exc:
                completed = False
                logger.critical("No se pudo retirar la salida parcial de Synthesis %s: %s", output, exc)
        return completed

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------

    async def execute_pipeline(
        self,
        patcher_ids: list[str] | None = None,
        create_snapshot: bool = True,
    ) -> dict[str, Any]:
        """Ejecuta el pipeline de Synthesis con protección transaccional.

        Usa ``SnapshotTransactionLock`` para garantizar atomicidad:
        si la ejecución falla o el ESP resultante está corrupto, el
        snapshot se restaura automáticamente.

        Args:
            patcher_ids: IDs de patchers a ejecutar. Si es ``None``,
                         usa los patchers habilitados del pipeline.
            create_snapshot: Si ``True``, crea snapshot antes de ejecutar.

        Returns:
            Diccionario con los campos de ``SynthesisResult``.
        """
        t0 = time.monotonic()

        # T-27b: dentro del sandbox el snapshot+rollback del servicio es
        # redundante (el clon ES el mecanismo de rollback: discard) y además
        # destructivo para la evidencia — ante un fallo restauraría el
        # Synthesis.esp previo del clon y el diff mostraría el estado pre-run
        # en vez de la salida parcial que el operador necesita ver
        # (review Codex #258). El lock se conserva (serialización).
        if self._output_path is not None and create_snapshot:
            logger.info("Run sandboxeado: snapshot del servicio deshabilitado (el clon es el rollback).")
            create_snapshot = False

        # Inicialización y resolución del contrato externo antes de abrir journal/lock.
        # La CLI requiere settings V2 y load order del perfil MO2; nunca se deja
        # que caiga silenciosamente al load order global del juego.
        try:
            runner = self._ensure_synthesis_runner()
            pipeline = self._ensure_patcher_pipeline()
        except (SynthesisExecutionError, RuntimeError) as exc:
            logger.error("Error inicializando Synthesis (stage 7): %s", exc)
            return self._error_dict(str(exc))

        if patcher_ids is None:
            enabled = pipeline.get_enabled_patchers()
            patcher_ids = [p.patcher_id for p in enabled]
            if not patcher_ids:
                try:
                    patcher_ids = list(runner.inspect_settings().patcher_ids)
                except SynthesisExecutionError as exc:
                    logger.error("No se pudo resolver el pipeline Synthesis configurado: %s", exc)
                    return self._error_dict(str(exc))

        if not patcher_ids:
            logger.warning("No hay patchers para ejecutar")
            return self._error_dict("No patchers configured or enabled")

        try:
            settings_fingerprint = runner.settings_fingerprint()
            load_order_fingerprint = runner.load_order_fingerprint()
            settings_profile = runner.inspect_settings(patcher_ids)
            if runner.settings_fingerprint() != settings_fingerprint:
                raise SynthesisExecutionError("Synthesis PipelineSettings.json changed while being validated")
            if runner.load_order_fingerprint() != load_order_fingerprint:
                raise SynthesisExecutionError("The active MO2 plugins.txt changed while being validated")
        except SynthesisExecutionError as exc:
            logger.error("Configuración Synthesis inválida (stage 7): %s", exc)
            return self._error_dict(str(exc))

        # Preflight brutal ANTES de tocar nada (T-16c·2, STAGE 7): un semáforo
        # ROJO cancela Synthesis sin correr el pipeline ni abrir transacción.
        try:
            preflight = self._ensure_preflight()
        except RuntimeError as exc:
            logger.error("Error en resolución para preflight de Synthesis (stage 7): %s", exc)
            return self._error_dict(str(exc))

        preflight_report = None
        if preflight is not None:
            preflight_report = await preflight.run()
        try:
            overflow_check = await asyncio.to_thread(self._build_master_overflow_check, runner, patcher_ids)
            visibility_check = await asyncio.to_thread(self._build_synthesis_visibility_check, runner)
            if runner.settings_fingerprint() != settings_fingerprint:
                raise SynthesisExecutionError("Synthesis PipelineSettings.json changed during preflight")
            if runner.load_order_fingerprint() != load_order_fingerprint:
                raise SynthesisExecutionError("The active MO2 plugins.txt changed during preflight")
        except SynthesisExecutionError as exc:
            logger.error("No se pudo validar el fan-in/visibilidad de Synthesis: %s", exc)
            return _attach_preflight(self._error_dict(str(exc)), preflight_report)
        preflight_report = self._append_preflight_check(preflight_report, overflow_check)
        preflight_report = self._append_preflight_check(preflight_report, visibility_check)
        if preflight_report.blocks_mutations:
            red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
            logger.warning("Synthesis (stage 7) bloqueado por preflight en rojo: %s", red)
            blocked = self._error_dict(f"Preflight en rojo, Synthesis cancelado: {red}")
            blocked["reason"] = "PreflightBlocked"
            blocked["preflight"] = preflight_report.to_dict()
            return blocked

        output_base_paths = tuple(runner._config.output_path / f"{name}.esp" for name in settings_profile.group_names)
        if not output_base_paths:
            return _attach_preflight(
                self._error_dict("Synthesis profile has no managed output group"), preflight_report
            )
        target_esp = output_base_paths[0]
        try:
            output_files_before = runner._discover_output_files_for_groups(settings_profile.group_names)
            if runner.settings_fingerprint() != settings_fingerprint:
                raise SynthesisExecutionError("Synthesis PipelineSettings.json changed before transaction setup")
            if runner.load_order_fingerprint() != load_order_fingerprint:
                raise SynthesisExecutionError("The active MO2 plugins.txt changed before transaction setup")
        except (SynthesisExecutionError, SynthesisValidationError) as exc:
            return _attach_preflight(self._error_dict(str(exc)), preflight_report)
        if output_files_before and not create_snapshot and self._output_path is None:
            return _attach_preflight(
                self._error_dict("Existing Synthesis outputs require a snapshot or an isolated sandbox before rerun"),
                preflight_report,
            )

        # --- Publish started event ---
        started_payload = SynthesisPipelineStartedPayload(
            patcher_ids=tuple(patcher_ids),
            target_esp=str(target_esp),
            snapshot_enabled=create_snapshot,
        )
        await self._event_bus.publish(
            Event(
                topic="synthesis.pipeline.started",
                payload=started_payload.to_log_dict(),
                source=self.AGENT_ID,
            )
        )

        # --- Transactional execution ---
        # Order: lock → snapshot → begin_transaction → execute → commit/abort.
        # El runner puede generar un conjunto split (base + _2 + _3 …), así que
        # el snapshot cubre todos los outputs existentes de los grupos elegidos.
        target_files = list(output_files_before) if create_snapshot else []

        result: SynthesisResult
        rolled_back = False
        manifest_failed = False
        mutation_started = False
        outputs_cleanup_completed = False
        tx_id: int | None = None
        # M-7 (misma lección que #295 en xedit_service): bindear el lock para
        # reportar el resultado REAL del rollback (tx_lock.rollback_completed)
        # en vez de inferirlo con flags — el flag anterior (in_lock_context)
        # declaraba rolled_back=True aunque la restauración del snapshot
        # hubiera fallado en __aexit__, dejando el .esp en estado parcial
        # mientras el caller creía que se había revertido.
        tx_lock: SnapshotTransactionLock | None = None

        try:
            async with SnapshotTransactionLock(
                lock_manager=self._lock_manager,
                snapshot_manager=self._snapshot_manager,
                resource_id=self.RESOURCE_ID,
                agent_id=self.AGENT_ID,
                target_files=target_files,
                metadata={"source": "synthesis_pipeline", "patchers": patcher_ids},
            ) as tx_lock:
                if runner.settings_fingerprint() != settings_fingerprint:
                    raise SynthesisExecutionError(
                        "Synthesis PipelineSettings.json changed while waiting for the transaction lock"
                    )
                if runner.load_order_fingerprint() != load_order_fingerprint:
                    raise SynthesisExecutionError(
                        "The active MO2 plugins.txt changed while waiting for the transaction lock"
                    )
                outputs_at_lock = runner._discover_output_files_for_groups(settings_profile.group_names)
                if set(outputs_at_lock) != set(output_files_before):
                    raise SynthesisExecutionError(
                        "Managed Synthesis outputs changed while waiting for the transaction lock; refusing to mutate"
                    )

                # Begin journal transaction AFTER lock+snapshot acquired
                tx_id = await self._journal.begin_transaction(
                    description="synthesis_pipeline",
                    agent_id=self.AGENT_ID,
                )

                # T-26 (ADR 0002): la caja negra ANTES de tocar el output
                # (fail-closed). Si el manifiesto no se puede emitir, se lanza
                # DENTRO del lock → __aexit__ restaura los snapshots y ningún
                # patcher corre (espejo de xedit_service).
                manifest_targets = list(dict.fromkeys([*output_base_paths, *output_files_before]))
                await self._emit_action_manifest(
                    tx_id=tx_id,
                    target_files=manifest_targets,
                    snapshots=tx_lock.snapshots,
                    summary=(
                        f"Ejecutar {len(patcher_ids)} patcher(s) de Synthesis → "
                        f"{', '.join(path.name for path in output_base_paths)}. "
                        "El manifiesto pre-run enumera bases y splits existentes; los splits nuevos "
                        "se descubren, validan y limpian dentro de la transacción."
                    ),
                )

                try:
                    mutation_started = True
                    # Una salida previa puede ser idéntica byte a byte tras el
                    # rerun. Retirar el conjunto completo de cada grupo hace que
                    # la atribución por presencia sea honesta y evita splits stale.
                    # Solo se hace con snapshot o dentro del clon descartable.
                    if output_files_before and (create_snapshot or self._output_path is not None):
                        for output_file in output_files_before:
                            output_file.unlink()

                    result = await runner.run_pipeline(
                        patcher_ids,
                        expected_settings_fingerprint=settings_fingerprint,
                        expected_load_order_fingerprint=load_order_fingerprint,
                    )
                    if runner.settings_fingerprint() != settings_fingerprint:
                        raise SynthesisExecutionError(
                            "Synthesis PipelineSettings.json changed while Synthesis was running"
                        )
                    if runner.load_order_fingerprint() != load_order_fingerprint:
                        raise SynthesisExecutionError("The active MO2 plugins.txt changed while Synthesis was running")

                    # Validar todos los ESP producidos (base y splits) dentro del
                    # lock; un solo miembro corrupto aborta el conjunto completo.
                    result_outputs = result.output_esps or ((result.output_esp,) if result.output_esp else ())
                    if result.success and not result_outputs:
                        raise SynthesisValidationError("Synthesis returned success without an output ESP")
                    if result.success:
                        for output_file in result_outputs:
                            if not await runner.validate_synthesis_esp(output_file):
                                raise SynthesisValidationError(
                                    "ESP validation failed: corrupted or invalid",
                                    esp_path=output_file,
                                )

                    # Pipeline failure → raise para activar rollback.
                    if not result.success:
                        raise SynthesisExecutionError(
                            "; ".join(result.errors) if result.errors else "Pipeline failed",
                            return_code=result.return_code,
                            stderr=result.stderr,
                        )
                except BaseException:
                    # SnapshotTransactionLock cubre archivos que existían antes;
                    # retirar también los nuevos _2/_3 es la mitad restante del
                    # rollback transaccional. En sandbox se conserva evidencia:
                    # el clon aún no se promueve y el operador puede inspeccionarla.
                    if self._output_path is None:
                        outputs_cleanup_completed = self._cleanup_synthesis_outputs(
                            runner, settings_profile.group_names
                        )
                    raise

            # Commit journal. Un fallo del commit conserva el camino de "rollback
            # honesto" (#295): propaga a ``except Exception`` → resultado fallido
            # + TX pendiente (la mutación quedó aplicada sin restauración).
            #
            # T-28 (FlightReport) NO se emite acá: Synthesis corre SIEMPRE en
            # sandbox (tool_dispatcher: "corre SIEMPRE en sandbox") con un
            # StagingJournal cuyo commit está DIFERIDO hasta la promoción, así que
            # un informe compuesto en este punto reflejaría un estado pre-promoción
            # (pending) y quedaría stale. El cierre post-vuelo del informe con las
            # rutas reales pertenece al promotion flow (ExecuteSynthesisPipeline-
            # Strategy / SandboxPromotionFlow) — follow-up documentado (review #309).
            if tx_id is not None:
                await self._journal.commit_transaction(tx_id)

        except _ActionManifestError as exc:
            # La caja negra no se pudo emitir: ningún patcher corrió. __aexit__ ya
            # restauró los snapshots; marcar la TX para no dejarla PENDING. Como
            # la mutación nunca ocurrió, marcar rolled_back es siempre seguro acá.
            manifest_failed = True
            rolled_back = bool(tx_lock and tx_lock.rollback_completed)
            if tx_id is not None:
                # Guardado: el journal ya falló al persistir el manifiesto; si
                # vuelve a fallar acá no debe enmascarar el resultado
                # ActionManifestFailed ni saltarse el evento completed (review #309).
                await self._safe_mark_rolled_back(tx_id)
            logger.error("Synthesis: no se pudo emitir el ActionManifest; abortado: %s", exc)
            detail = f"Manifiesto de vuelo requerido no emitido: {exc}"
            result = SynthesisResult(
                success=False,
                output_esp=None,
                return_code=-1,
                stdout="",
                stderr=detail,
                patchers_executed=[],
                errors=[detail],
            )

        except (SynthesisExecutionError, SynthesisValidationError) as exc:
            # El lock solo restaura los archivos que existían al entrar; la
            # limpieza de outputs nuevos/split también debe haber sido probada.
            snapshot_restored = bool(tx_lock and tx_lock.rollback_completed)
            rolled_back = (snapshot_restored and outputs_cleanup_completed) or (
                mutation_started and not target_files and outputs_cleanup_completed and self._output_path is None
            )
            journal_recovery_proven = (
                not mutation_started
                or self._output_path is not None  # el clon aislado aún no se promueve
                or rolled_back
            )
            if tx_id is not None and journal_recovery_proven:
                await self._journal.mark_transaction_rolled_back(tx_id)
            elif tx_id is not None:
                logger.critical(
                    "Rollback Synthesis incompleto para TX %d; se mantiene pendiente para recuperación manual.",
                    tx_id,
                )
            logger.error("Pipeline Synthesis falló: %s", exc)
            _rc = getattr(exc, "return_code", None)
            partial_outputs: tuple[pathlib.Path, ...] = ()
            if self._output_path is not None:
                try:
                    partial_outputs = tuple(runner._discover_output_files_for_groups(settings_profile.group_names))
                except (SynthesisExecutionError, SynthesisValidationError):
                    partial_outputs = ()
            result = SynthesisResult(
                success=False,
                output_esp=None,
                return_code=_rc if _rc is not None else -1,
                stdout="",
                stderr=str(exc),
                patchers_executed=[],
                errors=[str(exc)],
                output_esps=partial_outputs,
            )

        except LockAcquisitionError as exc:
            # Lock was never acquired — no journal TX started, no snapshots to restore
            logger.warning("Lock contention para %s: %s", self.RESOURCE_ID, exc)
            result = SynthesisResult(
                success=False,
                output_esp=None,
                return_code=-1,
                stdout="",
                stderr=str(exc),
                patchers_executed=[],
                errors=[f"Lock contention: {exc}"],
            )

        except Exception as exc:
            # Unexpected exception (OSError, asyncio.TimeoutError, etc.)
            # M-7: rolled_back sale de tx_lock.rollback_completed — True SOLO si
            # __aexit__ corrió y restauró todos los snapshots (≥1). Cubre ambos
            # casos que el flag anterior confundía: excepción fuera del lock
            # (commit_transaction falló → no hubo rollback) y restore fallido
            # dentro de __aexit__ (rollback_failures no vacío).
            # Note: asyncio.CancelledError is BaseException, not Exception —
            # it intentionally propagates through here uncaught.
            snapshot_restored = bool(tx_lock and tx_lock.rollback_completed)
            rolled_back = (snapshot_restored and outputs_cleanup_completed) or (
                mutation_started and not target_files and outputs_cleanup_completed and self._output_path is None
            )
            journal_recovery_proven = (
                not mutation_started
                or self._output_path is not None  # el clon aislado aún no se promueve
                or rolled_back
            )
            if tx_id is not None and journal_recovery_proven:
                try:
                    await self._journal.mark_transaction_rolled_back(tx_id)
                except Exception as rollback_exc:
                    logger.critical(
                        "Failed to mark journal TX %d rolled back after unexpected error: %s",
                        tx_id,
                        rollback_exc,
                        exc_info=True,
                    )
            elif tx_id is not None:
                logger.critical(
                    "Rollback Synthesis incompleto para TX %d; se mantiene pendiente para recuperación manual.",
                    tx_id,
                )
            logger.error("Unexpected exception in synthesis pipeline: %s", exc, exc_info=True)
            partial_outputs: tuple[pathlib.Path, ...] = ()
            if self._output_path is not None:
                try:
                    partial_outputs = tuple(runner._discover_output_files_for_groups(settings_profile.group_names))
                except (SynthesisExecutionError, SynthesisValidationError):
                    partial_outputs = ()
            result = SynthesisResult(
                success=False,
                output_esp=None,
                return_code=-1,
                stdout="",
                stderr=str(exc),
                patchers_executed=[],
                errors=[f"Unexpected error: {exc}"],
                output_esps=partial_outputs,
            )

        duration = time.monotonic() - t0

        # --- Publish completed event ---
        completed_payload = SynthesisPipelineCompletedPayload(
            patcher_ids=tuple(patcher_ids),
            target_esp=str(target_esp),
            success=result.success,
            patchers_executed=tuple(result.patchers_executed),
            errors=tuple(result.errors),
            duration_seconds=round(duration, 3),
            rolled_back=rolled_back,
        )
        await self._event_bus.publish(
            Event(
                topic="synthesis.pipeline.completed",
                payload=completed_payload.to_log_dict(),
                source=self.AGENT_ID,
            )
        )

        if result.success:
            logger.info(
                "Pipeline Synthesis exitoso: %s (%d patchers, %.1fs)",
                result.output_esp,
                len(result.patchers_executed),
                duration,
            )

        out = self._result_to_dict(result)
        if manifest_failed:
            out["reason"] = "ActionManifestFailed"
        return _attach_preflight(out, preflight_report)

    # ------------------------------------------------------------------
    # Caja negra de vuelo (T-26/T-28, ADR 0002) — espejo de xedit_service
    # ------------------------------------------------------------------

    async def _emit_action_manifest(
        self,
        *,
        tx_id: int,
        target_files: list[pathlib.Path],
        snapshots: Any,
        summary: str,
    ) -> None:
        """Construye y persiste el ActionManifest ANTES de mutar (T-26).

        Fail-closed: cualquier fallo del builder o del journal se convierte en
        :class:`_ActionManifestError` para que el caller aborte el pipeline sin
        mutar (la caja negra no es opcional cuando el journal está cableado).

        NOTA (review #309): en el path de producción (Synthesis SIEMPRE en
        sandbox) ``target_files`` apunta al clon (``clone.overwrite_copy``), que
        es lo que ESTE run efectivamente escribe. La traducción a las rutas
        reales del overwrite tras la promoción pertenece al promotion flow
        (dueño del mapeo clon→real) — follow-up documentado.
        """
        from sky_claw.app.orchestrator.preview.action_manifest import build_action_manifest

        try:
            manifest = build_action_manifest(
                ritual_id=f"synthesis-pipeline-{tx_id}",
                tool="Synthesis",
                tool_version=None,  # Synthesis no expone versión hoy (follow-up).
                target_files=[str(f) for f in target_files],
                snapshots=snapshots,
                summary=summary,
            )
            await self._journal.persist_action_manifest(
                manifest,
                agent_id=self.AGENT_ID,
                transaction_id=tx_id,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: cualquier fallo del journal/builder
            raise _ActionManifestError(str(exc)) from exc

    async def _safe_mark_rolled_back(self, tx_id: int) -> None:
        """Marca la TX como rolled_back sin propagar (best-effort, review #309).

        Se usa en el path de fallo del manifiesto: el journal que ya reventó al
        persistir no debe, si vuelve a fallar acá, enmascarar el resultado
        ``ActionManifestFailed`` ni impedir el evento ``completed`` (espejo del
        ``_mark_journal_rolled_back`` guardado de ``xedit_service``).
        """
        try:
            await self._journal.mark_transaction_rolled_back(tx_id)
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            logger.error("Fallo al marcar la TX %d rolled_back tras fallo del manifiesto", tx_id, exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_to_dict(result: SynthesisResult) -> dict[str, Any]:
        """Convierte SynthesisResult a dict serializable.

        Añade el ``message`` canónico del contrato compartido (deuda #5): vacío
        en éxito, y el detalle de stderr/errors en fallo.
        """
        raw = dataclasses.asdict(result)
        # Path → str para serialización
        if raw.get("output_esp") is not None:
            raw["output_esp"] = str(raw["output_esp"])
        raw["output_esps"] = [str(path) for path in raw.get("output_esps", ())]
        if raw.get("success"):
            raw["message"] = ""
        else:
            errors = raw.get("errors") or []
            raw["message"] = str(raw.get("stderr") or "; ".join(str(e) for e in errors) or "")
        return raw

    @staticmethod
    def _error_dict(message: str) -> dict[str, Any]:
        """Construye un dict de error para retornos tempranos."""
        return {
            "success": False,
            "message": message,
            "output_esp": None,
            "return_code": -1,
            "stdout": "",
            "stderr": message,
            "patchers_executed": [],
            "errors": [message],
        }

"""DynDOLODPipelineService — servicio transaccional para generación de LODs.

Extrae la lógica de ``execute_dyndolod_pipeline`` desde ``supervisor.py``
hacia un servicio con inyección de dependencias, locking multi-recurso
y eventos de ciclo de vida.

Sprint 2, Fase 3: Strangler Fig — desacoplamiento de ``supervisor.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import pathlib
import time
from typing import TYPE_CHECKING, Any

from sky_claw.app.core.event_bus import CoreEventBus, Event
from sky_claw.app.core.event_payloads import (
    DynDOLODPipelineCompletedPayload,
    DynDOLODPipelineStartedPayload,
)
from sky_claw.app.core.path_resolver import PathResolutionService
from sky_claw.app.db.handoffs import (
    DeploymentHandoff,
    HandoffState,
    clave_de_artifact,
    reconciliar_orphan_de_artifact,
)
from sky_claw.app.db.journal import JournalTransactionError, OperationJournal
from sky_claw.app.db.locks import (
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools._dir_rollback import DirectoryRollback, _commit_directory_rollbacks
from sky_claw.local.tools.artifact_digest import TreeDigest, digest_arbol
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODExecutionError,
    DynDOLODPipelineResult,
    DynDOLODRunner,
    DynDOLODTimeoutError,
)
from sky_claw.local.tools.output_targets import dyndolod_output_target
from sky_claw.logging_config import correlacion_de_transaccion

if TYPE_CHECKING:
    from sky_claw.local.validators.preflight import PreflightReport, PreflightService

logger = logging.getLogger("SkyClaw.DynDOLODPipelineService")

#: Índice de etapa de DynDOLOD en el DAG de ``sky_claw/local/AGENTS.md`` §1. La
#: mayoría de los registros de fallo de este módulo la emiten como
#: ``pipeline_stage`` (§5 regla 5); los que no —porque semánticamente no son un
#: fallo de la etapa 9— están enumerados como exención en
#: ``_REGISTROS_EXENTOS_DE_ETAPA`` (``tests/test_dyndolod_service.py``), no
#: dejados afuera en silencio. Se define UNA vez a propósito: duplicar el número
#: en cada registro garantiza que un reordenamiento del DAG deje algunos
#: desactualizados. No se pone un conteo fijo acá —"aparece en N registros"— a
#: propósito: quedó desactualizado dos veces en este mismo PR (16→13→14 según
#: fixes posteriores agregaban o quitaban registros); el conteo real lo miden
#: en vivo los anclas de `tests/test_dyndolod_service.py`, no este comentario.
#:
#: No se deriva de nada porque no hay de dónde: el orden del pipeline vive como
#: tabla en prosa en §1 —que la propia sección aclara que todavía no se hace
#: cumplir en tiempo de ejecución— y no existe un registro de etapas en código.
#: Si alguna vez se construye, este es el único sitio a cambiar en este servicio.
_ETAPA_DYNDOLOD = 9


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service``/``xedit_service``/``synthesis_service``
    (T-16b/T-16c): un semáforo verde no ensucia el dict; amarillo/rojo viajan
    como ``result["preflight"]`` para que el panel vivo lo renderice.

    También fija ``assisted`` UNA sola vez: la etapa 9 es asistida en TODOS los
    retornos (éxito, error de dominio, fallos tempranos), así que el consumidor
    del resultado no tiene que interpretar la ausencia de la clave. El retorno
    de preflight en rojo también pasa por acá para el mismo contrato.
    """
    result.setdefault("assisted", True)
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


class _ActionManifestError(Exception):
    """Interno (T-26): la emisión del manifiesto de vuelo falló. Se lanza DENTRO
    del lock (antes de mutar) para que el Ritual NO proceda sin manifiesto — la
    caja negra no es opcional cuando el journal está cableado (espejo de
    ``loot_service``/``xedit_service``/``synthesis_service._ActionManifestError``)."""


@dataclasses.dataclass(frozen=True, slots=True)
class _ResumeBloqueado:
    """Resume rechazado por el estado durable del handoff (D2).

    ``reason`` es el código estable que el payload expone; ``detail`` la
    explicación humana. El contrato: DynDOLOD NO se lanza y el motivo nunca se
    disfraza de fallo de herramienta.
    """

    reason: str
    detail: str


class DynDOLODPipelineService:
    """Servicio transaccional para el pipeline DynDOLOD (TexGen + DynDOLOD).

    Encapsula la lógica de ejecución con:
    - Locking multi-recurso vía :class:`SnapshotTransactionLock`
    - Snapshots automáticos para rollback
    - Registro de operaciones en :class:`OperationJournal`
    - Eventos de ciclo de vida en :class:`CoreEventBus`

    Args:
        lock_manager: Gestor de locks distribuidos.
        snapshot_manager: Gestor de snapshots de archivos.
        journal: Journal de operaciones para trazabilidad.
        path_resolver: Servicio de resolución de rutas validadas.
        event_bus: Bus de eventos para publicación de ciclo de vida.
    """

    def __init__(
        self,
        *,
        lock_manager: DistributedLockManager,
        snapshot_manager: FileSnapshotManager,
        journal: OperationJournal,
        path_resolver: PathResolutionService,
        event_bus: CoreEventBus,
        preflight: PreflightService | None = None,
        mo2_profile: str | None = None,
    ) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._journal = journal
        self._path_resolver = path_resolver
        self._event_bus = event_bus
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        self._preflight = preflight
        # D2 (PR #493): identidad esperada del DUEÑO del handoff durable. None
        # significa "no resoluble" y falla cerrado antes de mutar (nunca se crea
        # un handoff resumible sin dueño).
        self._mo2_profile = mo2_profile

        # Lazy init — runner requiere env vars que pueden no existir aún.
        self._runner: DynDOLODRunner | None = None

    # ------------------------------------------------------------------
    # Lazy initialization
    # ------------------------------------------------------------------

    def _ensure_runner(self) -> DynDOLODRunner:
        """Inicializa el :class:`DynDOLODRunner` bajo demanda.

        Variables de entorno requeridas:
        - ``DYNDLOD_EXE``: Ruta a DynDOLODx64.exe
        - ``TEXGEN_EXE``: Ruta a TexGenx64.exe (opcional)
        - ``SKYRIM_PATH``: Ruta al directorio de Skyrim SE/AE
        - ``MO2_PATH``: Ruta al directorio de MO2
        - ``MO2_MODS_PATH``: Ruta a la carpeta mods de MO2

        Returns:
            DynDOLODRunner inicializado.

        Raises:
            DynDOLODExecutionError: Si faltan variables de entorno requeridas.
        """
        if self._runner is not None:
            return self._runner

        game_path = self._path_resolver.get_skyrim_path()
        mo2_path = self._path_resolver.get_mo2_path()
        mo2_mods_path = self._path_resolver.get_mo2_mods_path()
        dyndolod_exe = self._path_resolver.get_dyndolod_exe()
        texgen_exe = self._path_resolver.get_texgen_exe()

        if not game_path or not mo2_path or not mo2_mods_path or not dyndolod_exe:
            raise DynDOLODExecutionError(
                "Cannot initialize DynDOLODRunner: "
                "SKYRIM_PATH, MO2_PATH, MO2_MODS_PATH, and DYNDLOD_EXE "
                "environment variables must be valid paths"
            )

        if not dyndolod_exe.exists():
            raise DynDOLODExecutionError(f"DynDOLOD executable not found: {dyndolod_exe}")

        config = DynDOLODConfig(
            game_path=game_path,
            mo2_path=mo2_path,
            mo2_mods_path=mo2_mods_path,
            dyndolod_exe=dyndolod_exe,
            texgen_exe=texgen_exe,
        )

        self._runner = DynDOLODRunner(config)
        logger.info(
            "DynDOLODRunner inicializado: game=%s, dyndolod=%s",
            game_path,
            dyndolod_exe,
        )
        return self._runner

    def _ensure_preflight(self) -> PreflightService | None:
        """Construye perezosamente el preflight de DynDOLOD (T-16c·3).

        DynDOLOD regenera GBs de LODs bajo ``mods/`` en un run de 30+ min, así que
        los sensores relevantes son: **permisos de escritura** sobre los dirs de
        salida (``mods/`` y los ``*/Output`` existentes — el clásico "output
        read-only mata el run a mitad"), **símbolos/junctions** en las rutas
        crudas, **masters faltantes** y **límites full/light** del perfil MO2
        activo (DynDOLOD lee todo el load order), y **overwrite sucio**. NO cablea
        la versión de LOOT (irrelevante). Reusa las primitivas compartidas
        (T-16d/T-16c·3); no toca los otros servicios. Sensores no resolubles →
        ``None`` (omitidos con ``omit_unconfigured``). Sin game/MO2 → ``None``
        (sin gate, mismo criterio que loot/Synthesis).
        """
        if self._preflight is not None:
            return self._preflight

        game = self._path_resolver.get_skyrim_path()
        mo2 = self._path_resolver.get_mo2_path()
        if not isinstance(game, pathlib.Path) or not isinstance(mo2, pathlib.Path):
            return None

        # Imports perezosos (anti-ciclo: validators.preflight llega a tools._process).
        from sky_claw.local.validators.preflight import PreflightService
        from sky_claw.local.validators.preflight_sensors import (
            build_master_order_sensor,
            build_mo2_profile_sources_resolver,
            build_modlist_sensors,
            build_overwrite_sensor,
            build_vfs_sensor,
            build_vfs_visibility_sensor,
        )
        from sky_claw.local.validators.write_permissions import WritePermissionsChecker

        # vfs sobre rutas CRUDAS (las resueltas ya siguieron los symlinks).
        # scan_mods_dir: la raíz MO2 de acá ya está VALIDADA (el guard de arriba
        # exige que get_mo2_path() sea un Path), así que enumerar mods/ es seguro
        # — el False hardcodeado dejaba ciego el scan de symlinks (U-01).
        vfs_checker = build_vfs_sensor(
            raw_game=self._path_resolver.get_skyrim_path_raw(),
            raw_mo2=self._path_resolver.get_mo2_path_raw(),
            scan_mods_dir=True,
        )

        # Permisos: los targets se recalculan POR CORRIDA dentro del closure
        # (freshness, review Codex #311) — un dir de salida creado read-only
        # después de construir el preflight cacheado debe verse igual.
        def _permissions() -> Any:
            return WritePermissionsChecker(targets=self._permission_targets()).check()

        overwrite_check = build_overwrite_sensor(mo2 / "overwrite")

        resolver = build_mo2_profile_sources_resolver(
            game=game, mo2=mo2, profile=self._path_resolver.get_active_profile()
        )
        masters_check, limits_check = build_modlist_sensors(resolver) if resolver is not None else (None, None)
        # DynDOLOD (stage 9) lee todo el load order ya estabilizado: un master
        # invertido invalida 30+ min de generación de LODs.
        order_check = build_master_order_sensor(resolver) if resolver is not None else None

        # U-01: DynDOLOD lee TODO el load order; si la USVFS no se heredó,
        # generaría LODs del juego base durante 30+ min y reportaría éxito.
        visibility_check = build_vfs_visibility_sensor(game=game, sources_resolver=resolver)

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

    def _permission_targets(self) -> list[pathlib.Path]:
        """Rutas que DynDOLOD reescribe, resueltas EN CADA corrida (review #311).

        - ``mods/`` (padre donde empaqueta los mods) + los mod dirs empaquetados
          (``DynDOLOD Output``/``TexGen Output``).
        - El **staging crudo** bajo la raíz administrada única
          (``output_targets.dyndolod_output_target`` → ``game/Sky-Claw/DynDOLOD``):
          el primer ancestro EXISTENTE de la raíz (para poder CREARLA en el primer
          run), ``root``, ``root/DynDOLOD_Output`` y ``root/textures``. El
          cwd y la raíz MO2 dejaron de ser raíces de staging con ``-o:``, así que
          no se sondean.
        - El **directorio del ejecutable**: no es staging de salida, pero SÍ es
          superficie de escritura de la herramienta, y una que este servicio
          depende de leer. ``DynDOLODRunner._leer_log`` busca el veredicto de la
          corrida en ``exe.parent/Logs/{Tool}_{modo}_log.txt``, y los binarios
          persisten su INI junto al exe. Con el tool en un árbol de solo lectura
          (Program Files), omitirlo dejaba el preflight en verde y el post-check
          sin log: degradaba a artefacto-solo, que es exactamente el camino del
          falso verde que el gate de frescura persigue. Se sondea el dir del exe
          y su ``Logs/`` — el segundo puede no existir todavía en el primer run.

        ``WritePermissionsChecker`` sondea solo los dirs existentes, así que
        incluir rutas aún inexistentes es seguro (se saltan) y las que aparezcan
        en runs futuros se sondean sin reconstruir el preflight cacheado. Por eso
        el ancestro de la raíz se resuelve al primero que EXISTE: con la raíz
        anidada bajo ``Sky-Claw/``, ``root.parent`` tampoco existe en el primer
        run y el sondeo quedaba mudo justo en el caso que vino a cubrir.
        """
        candidates: list[pathlib.Path] = []
        mods = self._path_resolver.get_mo2_mods_path()
        if isinstance(mods, pathlib.Path):
            candidates += [mods, mods / DynDOLODRunner.DYNDOLLOD_MOD_NAME, mods / DynDOLODRunner.TEXGEN_MOD_NAME]

        game = self._path_resolver.get_skyrim_path()
        root = dyndolod_output_target(game=game if isinstance(game, pathlib.Path) else None)
        if root is not None:
            ancestro = self._primer_ancestro_existente(root, tope=game if isinstance(game, pathlib.Path) else None)
            if ancestro is not None:
                candidates.append(ancestro)
            candidates += [
                root,
                root / DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME,
                root / DynDOLODRunner.TEXGEN_OUTPUT_NAME,
            ]

        # Dir del exe: donde la herramienta escribe su log y su INI.
        for exe in (self._path_resolver.get_dyndolod_exe(), self._path_resolver.get_texgen_exe()):
            if isinstance(exe, pathlib.Path):
                candidates += [exe.parent, exe.parent / "Logs"]

        seen: set[pathlib.Path] = set()
        return [p for p in candidates if not (p in seen or seen.add(p))]

    @staticmethod
    def _primer_ancestro_existente(ruta: pathlib.Path, *, tope: pathlib.Path | None) -> pathlib.Path | None:
        """Primer directorio existente subiendo desde ``ruta``, sin pasar de ``tope``.

        El sondeo de permisos se salta las rutas inexistentes, así que preguntar
        por un padre que tampoco existe no verifica nada: para responder "¿puedo
        CREAR la raíz administrada?" hay que preguntarle al primer eslabón que sí
        está en disco.

        ``tope`` (el directorio del juego) corta el ascenso. Sin él, un game path
        mal configurado o todavía no montado hacía subir hasta la raíz del volumen
        y el preflight terminaba sondeando —y aprobando— un directorio ajeno al
        juego: un verde sobre una configuración inválida, que recién fallaba
        después en ``DynDOLODConfig.__post_init__`` (review adversarial #441). Si
        ni el tope existe, no hay nada honesto que sondear: ``None``.

        **El tope se RESUELVE antes de comparar, y la comparación es de
        pertenencia, no de igualdad.** ``ruta`` viene de
        ``dyndolod_output_target``, que construye sobre ``game.resolve()``,
        mientras que el tope llega crudo del path resolver; ``Path.__eq__`` es
        léxico, así que un ``..`` o un junction en la ruta configurada hacía que
        el tope no fuera nunca ancestro literal de la raíz y el corte no
        disparara — el ascenso seguía hasta el volumen, que es justo lo que este
        parámetro vino a impedir (segunda review adversarial, PR #441).
        ``is_relative_to`` corta al SALIR del árbol del juego, sin depender de
        acertar el eslabón exacto.

        **El límite se evalúa ANTES que la existencia**, y ese orden es el
        invariante: al revés, un ancestro existente FUERA del árbol del juego se
        devuelve antes de llegar al corte. El caso realista no es exótico —disco
        montado y carpeta del juego ausente, o un typo en un path profundo— y
        deja al preflight sondeando y aprobando un directorio foráneo: el mismo
        verde sobre configuración inválida, por tercera vía (review adversarial
        #441). Preguntar "¿sigo dentro del juego?" antes que "¿existe?" es lo que
        hace que la respuesta no dependa de qué haya montado alrededor.
        """
        tope_resuelto = tope.resolve() if tope is not None else None
        for padre in ruta.parents:
            if tope_resuelto is not None and not padre.is_relative_to(tope_resuelto):
                return None
            if padre.exists():
                return padre
        return None

    def _primera_ruta_de_config_faltante(self, runner: DynDOLODRunner) -> pathlib.Path | None:
        """Primera ruta declarada por la config del runner que no existe (o ``None``).

        ``data_dir`` tiene default derivado (siempre se chequea); ``ini_dir``/
        ``plugins_file`` solo cuando el operador los configuró. Las no declaradas
        las resuelve la herramienta por registro — el chequeo no puede opinar
        sobre lo que no se le dijo.
        """
        config = runner._config
        for ruta in (config.data_dir, config.ini_dir, config.plugins_file):
            if ruta is not None and not ruta.exists():
                return ruta
        return None

    # ------------------------------------------------------------------
    # Handoff durable de deployment (D2, PR #493)
    # ------------------------------------------------------------------

    @staticmethod
    def _keys_de_identidad(runner: DynDOLODRunner) -> tuple[str, str, str]:
        """Identidades canónicas (game, mods-root, Data) para el handoff.

        ``data_dir`` tiene default derivado en ``DynDOLODConfig.__post_init__``,
        así que en producción nunca es ``None``; la rama negativa sólo la toman
        dobles de test con ``_config`` mockeado.
        """
        config = runner._config
        return (
            clave_de_artifact(config.game_path),
            clave_de_artifact(config.mo2_mods_path),
            clave_de_artifact(config.data_dir) if config.data_dir is not None else "desconocido",
        )

    async def _consultar_resume(
        self,
        runner: DynDOLODRunner,
    ) -> DeploymentHandoff | _ResumeBloqueado | None:
        """Decisión de resume por ESTADO DURABLE, nunca por ``Path.exists``.

        Devuelve el handoff ``AWAITING_DEPLOYMENT`` verificado (perfil + digest)
        cuando el resume es legítimo, ``None`` cuando no hay handoff activo NI
        evidencia durable de una corrida incompleta (legacy verbatim), o un
        :class:`_ResumeBloqueado` fail-closed. El gate de Data vive en el
        runner (byte a byte bajo el lock): acá se verifican identidad de dueño
        e identidad del artifact, que son las mitades que el runner no puede
        probar.

        F-002: ``consultar_handoff_activo() == None`` NO alcanza para declarar
        legacy. Antes del fallback se consulta la MISMA primitive que el
        reconciler de arranque: si hay una TX PENDING/ROLLED_BACK cuyo
        ActionManifest nombra el artifact y el mod sigue vivo, se materializa
        un INDETERMINATE conservador y el resume falla cerrado.
        """
        mods_path = runner._config.mo2_mods_path
        mod_texgen = mods_path / DynDOLODRunner.TEXGEN_MOD_NAME
        game_key, mods_root_key, data_key = self._keys_de_identidad(runner)
        activo = await reconciliar_orphan_de_artifact(
            journal=self._journal,
            mod_texgen=mod_texgen,
            game_key=game_key,
            mods_root_key=mods_root_key,
            data_key=data_key,
            expected_profile=self._mo2_profile or "desconocido",
            digest_arbol=digest_arbol,
        )
        if activo is None:
            return None  # legacy verbatim: sin handoff activo y sin evidencia durable
        if activo.state is HandoffState.INDETERMINATE:
            return _ResumeBloqueado(
                "HandoffIndeterminate",
                "Hay un handoff INDETERMINATE para este artifact: no se puede probar que el árbol "
                "pertenezca a la corrida que lo generó. Regenerá TexGen (run_texgen=True) para "
                "restaurar una identidad autorizada; el resume nunca lo promueve solo.",
            )
        if activo.state is HandoffState.SUPERSEDING:
            return _ResumeBloqueado(
                "HandoffSuperseding",
                "El handoff está en SUPERSEDING: una regeneración quedó a medias y su identidad "
                "aún no se resolvió. Reintentá cuando la corrida dueña termine o sea reconciliada.",
            )
        # AWAITING_DEPLOYMENT
        if self._mo2_profile is None:
            return _ResumeBloqueado(
                "HandoffProfileUnknown",
                "No hay un perfil MO2 resoluble para validar el dueño del handoff: falla cerrado.",
            )
        if activo.expected_profile != self._mo2_profile:
            return _ResumeBloqueado(
                "HandoffProfileMismatch",
                f"El handoff fue creado por el perfil '{activo.expected_profile}' y el caller es "
                f"'{self._mo2_profile}': el MISMO artifact físico no se retoma desde otro perfil "
                "sin regenerar.",
            )
        textures = mods_path / DynDOLODRunner.TEXGEN_MOD_NAME / DynDOLODRunner.TEXGEN_OUTPUT_NAME
        if not textures.is_dir():
            return _ResumeBloqueado(
                "ArtifactMissing",
                f"El handoff espera '{textures}' pero el artifact no está: no se lanza DynDOLOD "
                "sobre una identidad que desapareció (materializalo o regenerá TexGen).",
            )
        try:
            actual = await asyncio.to_thread(digest_arbol, textures)
        except OSError as e:
            return _ResumeBloqueado(
                "ArtifactUnreadable",
                f"No se pudo identificar el artifact '{textures}': {e}. DynDOLOD no se lanza.",
            )
        if (actual.digest, actual.files, actual.bytes) != (
            activo.expected_digest,
            activo.expected_files,
            activo.expected_bytes,
        ):
            return _ResumeBloqueado(
                "DigestMismatch",
                f"El artifact en '{textures}' no coincide con la identidad autorizada por la "
                "corrida que lo generó: no se lanza DynDOLOD sobre bytes que no se pueden atribuir.",
            )
        return activo

    async def _resolver_fallo_de_supersede(
        self,
        *,
        runner: DynDOLODRunner,
        handoff_previo: DeploymentHandoff,
        packaging_intentado: bool,
        create_snapshot: bool,
        tx_id: int | None,
    ) -> None:
        """Matriz de fallos del supersede (D2 §L): clases A, B y C.

        - **A**: el artifact empaquetado nunca se tocó (TexGen/veredicto falló
          antes del empaquetado) → el handoff vuelve a su estado previo.
        - **B**: el empaquetado se intentó, ``create_snapshot=True`` y el
          DirectoryRollback restauró el artifact byte-exacto → se VERIFICA el
          digest contra la identidad esperada y se vuelve al estado previo.
        - **C**: el empaquetado se intentó y no hay restauración exacta
          demostrable (``create_snapshot=False``, o digest post-restore
          distinto) → INDETERMINATE sin identidad autorizada; el resume fallará
          cerrado.
        """
        estado_previo = handoff_previo.state  # AWAITING o INDETERMINATE (siempre activo acá)
        mods_path = runner._config.mo2_mods_path
        textures = mods_path / DynDOLODRunner.TEXGEN_MOD_NAME / DynDOLODRunner.TEXGEN_OUTPUT_NAME
        observado: TreeDigest | None = None
        try:
            observado = await asyncio.to_thread(digest_arbol, textures)
        except OSError:
            observado = None

        if not packaging_intentado:
            logger.warning(
                "Supersede del handoff %d: fallo CLASE A (el artifact no se tocó) — vuelve a '%s'",
                handoff_previo.handoff_id,
                estado_previo.value,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
            await self._journal.transicionar_handoff(
                handoff_previo.handoff_id,
                desde=HandoffState.SUPERSEDING,
                hacia=estado_previo,
            )
            return

        restauracion_exacta = (
            create_snapshot
            and observado is not None
            and (
                (observado.digest, observado.files, observado.bytes)
                == (handoff_previo.expected_digest, handoff_previo.expected_files, handoff_previo.expected_bytes)
            )
        )
        if restauracion_exacta:
            logger.warning(
                "Supersede del handoff %d: fallo CLASE B (restore byte-exacto verificado) — vuelve a '%s'",
                handoff_previo.handoff_id,
                estado_previo.value,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
            await self._journal.transicionar_handoff(
                handoff_previo.handoff_id,
                desde=HandoffState.SUPERSEDING,
                hacia=estado_previo,
            )
            return

        logger.error(
            "Supersede del handoff %d: fallo CLASE C (sin restauración exacta demostrable) "
            "— cae a INDETERMINATE; el resume fallará cerrado",
            handoff_previo.handoff_id,
            extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
        )
        await self._journal.transicionar_handoff(
            handoff_previo.handoff_id,
            desde=HandoffState.SUPERSEDING,
            hacia=HandoffState.INDETERMINATE,
            expected_digest=None,
            expected_files=None,
            expected_bytes=None,
            observed_digest=observado.digest if observado is not None else None,
            observed_files=observado.files if observado is not None else None,
            observed_bytes=observado.bytes if observado is not None else None,
        )

    # ------------------------------------------------------------------
    # Caja negra de vuelo (T-26/T-28, ADR 0002) — espejo de xedit_service
    # ------------------------------------------------------------------

    async def _emit_action_manifest(
        self,
        *,
        tx_id: int,
        target_files: list[pathlib.Path],
        summary: str,
    ) -> None:
        """Construye y persiste el ActionManifest ANTES de mutar (T-26).

        Fail-closed: cualquier fallo del builder o del journal se convierte en
        :class:`_ActionManifestError` para que el caller aborte el pipeline sin
        mutar (la caja negra no es opcional cuando el journal está cableado).

        NOTA: el rollback de DynDOLOD es el move-aside de ``DirectoryRollback``
        (los ``Output/`` pesan GBs; snapshot copy-based sería carísimo), NO el
        snapshot manager — por eso el lock usa ``target_files=[]`` y el
        ``rollback_plan`` del manifiesto queda vacío por diseño (``snapshots=[]``).
        Un plan de rollback consciente del move-aside es follow-up.
        """
        from sky_claw.app.orchestrator.preview.action_manifest import build_action_manifest

        try:
            manifest = build_action_manifest(
                ritual_id=f"dyndolod-pipeline-{tx_id}",
                tool="DynDOLOD",
                tool_version=None,  # DynDOLOD no expone versión hoy (follow-up).
                target_files=[str(f) for f in target_files],
                snapshots=[],  # rollback = DirectoryRollback move-aside, no snapshots.
                summary=summary,
            )
            await self._journal.persist_action_manifest(
                manifest,
                agent_id="dyndolod-pipeline-service",
                transaction_id=tx_id,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: cualquier fallo del journal/builder
            raise _ActionManifestError(str(exc)) from exc

    async def _emit_flight_report(self, tx_id: int) -> None:
        """Compone y persiste el FlightReport del Ritual terminado (T-28).

        Post-vuelo y best-effort: lee la caja negra desde el journal (el
        manifiesto persistido + el estado REAL de la TX) y la persiste. Un fallo
        se loguea y NO rompe un pipeline ya exitoso (misma disciplina que LOOT/xEdit).
        """
        from sky_claw.app.orchestrator.preview.flight_report import (
            compose_flight_report_from_journal,
        )

        try:
            report = await compose_flight_report_from_journal(self._journal, transaction_id=tx_id)
            await self._journal.persist_flight_report(
                report,
                agent_id="dyndolod-pipeline-service",
                transaction_id=tx_id,
            )
        except Exception:  # noqa: BLE001 — boundary best-effort del journal
            # Sin pipeline_stage a propósito (review Qodo, PR #464, "Sobreconteo de
            # señal"): este método SOLO se alcanza tras un pipeline YA exitoso (ver
            # el único call site, en `execute`, después del commit). Etiquetarlo con
            # la etapa 9 haría que un fallo de infraestructura de journaling —que no
            # implica que DynDOLOD haya fallado— se contara como un fallo real de la
            # etapa 9 en cualquier alerta que agrupe por ese campo, inflando la tasa
            # de fallos exactamente como "Duplicación de señal" ya advertía para
            # registros duplicados. `operation_type` propio en su lugar, para que
            # sea identificable sin mentir sobre su origen. `tx_id` sí aplica: sigue
            # siendo correlacionable con la TX y su rollback si lo hubiera.
            logger.error(
                "DynDOLOD: fallo al persistir el informe de vuelo de la TX %d (pipeline ya exitoso)",
                tx_id,
                exc_info=True,
                extra={"operation_type": "dyndolod_flight_report_persist_failed", "tx_id": tx_id},
            )

    async def _preservar_mod_de_texgen(
        self,
        dir_rollbacks: list[DirectoryRollback],
        objetivo: pathlib.Path,
        *,
        tx_id: int | None,
    ) -> pathlib.Path | None:
        """Confirma el move-aside de ``objetivo`` para que sobreviva al fallo (F1).

        ``commit()`` sella ESE protector: descarta su backup y deja su ``__aexit__``
        en no-op, así que el desenrollado del ``AsyncExitStack`` restaura todo lo
        demás y deja este directorio como quedó. Es deliberadamente quirúrgico —un
        solo destino, nunca el lote— porque el staging crudo tiene que seguir
        revirtiendo.

        Devuelve el path preservado, o ``None`` si el protector no estaba en el
        lote. ``None`` NO significa que el mod no exista: con
        ``create_snapshot=False`` nunca se apartó y sobrevive sin intervención. Lo
        que el path expresa es "esta corrida confirmó una mutación", que es lo que
        el journal necesita saber para no afirmar un rollback total.
        """
        for rollback in dir_rollbacks:
            if rollback.target == objetivo:
                await rollback.commit()
                logger.warning(
                    "DynDOLOD (stage 9): se PRESERVA '%s' pese al fallo — es la salida de TexGen "
                    "que el operador tiene que materializar en el Data para poder continuar.",
                    objetivo,
                    extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
                )
                return objetivo
        return None

    async def _cerrar_tx_tras_rollback(
        self,
        tx_id: int | None,
        dir_rollbacks: list[DirectoryRollback],
        *,
        journal_committed: bool,
        mutation_started: bool,
        mutation_coverage_complete: bool,
        contexto: str,
        preservado_para_deployment: pathlib.Path | None = None,
    ) -> bool:
        """Marca ROLLED_BACK sólo cuando todos los move-aside quedaron resueltos.

        Un ``DirectoryRollback`` registrado antes de completar ``__aenter__``
        también participa: ``rollback_completed=True`` cubre preflight sin mutación
        o undo confirmado; False conserva la TX PENDING y el backup visible para
        recovery manual. Una vez iniciado el runner, además se exige cobertura de
        TODAS las superficies mutables esperadas; una sola salida/staging sin
        protección fuerza PENDING. Una TX ya commiteada es un punto de no-retorno y
        nunca se re-marca por una cancelación post-run.
        """
        if journal_committed:
            return False
        # F1: un protector CONFIRMADO a propósito no es un rollback que falló, así
        # que no entra en la pregunta "¿se resolvió todo?" — su `rollback_completed`
        # quedó en False justamente porque NO revirtió, que es lo que se quería. Se
        # excluye del universo medido y se reporta aparte; mezclarlos convertía una
        # decisión deliberada en una alarma de inconsistencia.
        pendientes = [dr for dr in dir_rollbacks if dr.target != preservado_para_deployment]
        # No usar ``all([])`` como prueba: lista vacía sólo es honesta antes de
        # que el runner pueda mutar. Después de empezarlo, ausencia de protectores
        # observados es falta de cobertura, no rollback exitoso.
        rollbacks_resueltos = all(dr.rollback_completed for dr in pendientes) if pendientes else not mutation_started
        rolled_back = rollbacks_resueltos and (not mutation_started or mutation_coverage_complete)
        if tx_id is None:
            return rolled_back
        if preservado_para_deployment is not None:
            # La TX queda PENDIENTE y `rolled_back` en False, que es la verdad: hay
            # una mutación viva en disco. El registro la NOMBRA para que el journal
            # y el filesystem cuenten la misma historia — sin esto, la TX pendiente
            # parecía un rollback a medio hacer y no un handoff esperando al
            # operador.
            logger.warning(
                "DynDOLOD (stage 9): TX %d queda PENDIENTE con una mutación PRESERVADA a propósito "
                "tras %s: '%s' contiene la salida de TexGen de esta corrida y espera que el operador "
                "la materialice en el Data. NO es un rollback incompleto.",
                tx_id,
                contexto,
                preservado_para_deployment,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
            return rolled_back
        if not rolled_back:
            if mutation_started and not mutation_coverage_complete and rollbacks_resueltos:
                logger.warning(
                    "DynDOLOD (stage 9): cobertura de staging no demostrada tras %s (TX %d): "
                    "la TX queda PENDIENTE hasta aislar los targets crudos.",
                    contexto,
                    tx_id,
                    extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
                )
            else:
                logger.critical(
                    "DynDOLOD (stage 9): rollback INCOMPLETO tras %s (TX %d): la TX queda PENDIENTE; "
                    "revisar backups move-aside y targets no cubiertos manualmente.",
                    contexto,
                    tx_id,
                    extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
                )
            return False
        try:
            await self._journal.mark_transaction_rolled_back(tx_id)
        except Exception as journal_exc:  # noqa: BLE001 — boundary best-effort del journal
            logger.error(
                "DynDOLOD (stage 9): no se pudo marcar la TX %d como rolled back tras %s: %s",
                tx_id,
                contexto,
                journal_exc,
                exc_info=True,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
        return True

    # ------------------------------------------------------------------
    # Pipeline principal
    # ------------------------------------------------------------------

    async def execute(
        self,
        preset: str = "Medium",
        run_texgen: bool = True,
        create_snapshot: bool = True,
        texgen_args: list[str] | None = None,
        dyndolod_args: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Ejecuta el pipeline completo de generación de LODs.

        Flujo transaccional:
        1. Publicar evento ``pipeline.dyndolod.started``
        2. Adquirir lock + snapshot vía :class:`SnapshotTransactionLock`
        3. Comenzar transacción en journal
        4. Ejecutar TexGen (si ``run_texgen=True``) → DynDOLOD
        5. Validar salida de DynDOLOD
        6. Commit en journal y publicar evento ``pipeline.dyndolod.completed``

        Args:
            preset: Nivel de calidad (Low, Medium, High).
            run_texgen: Si True, ejecuta TexGen antes de DynDOLOD.
            create_snapshot: Si True, crea snapshot para rollback.
            texgen_args: Argumentos adicionales para TexGen.
            dyndolod_args: Argumentos adicionales para DynDOLOD.
            dry_run: Si True, NO ejecuta TexGen/DynDOLOD; devuelve una estimación
                plan-only (``status="dry_run_preview"`` + ``change_set``) sin
                lock, journal ni eventos. DynDOLOD es la etapa más cara (GBs,
                30+ min), por eso el preview nunca la ejecuta.

        Returns:
            Diccionario con resultado del pipeline, o el preview plan-only
            cuando ``dry_run=True``.
        """
        if dry_run:
            return await self._preview(preset=preset, run_texgen=run_texgen)

        # Declarado ANTES del preflight (review Qodo, PR #464, "tx_id ausente"):
        # sin esto, `tx_id` ni siquiera existe todavía cuando el preflight bloquea
        # en rojo, así que ese registro no podía llevarlo. Con la variable ya en
        # scope (en `None`, honesto — no hay TX abierta antes del lock), TODO
        # registro de fallo del método puede incluir `tx_id` sin excepción: la
        # regla deja de tener un caso especial que documentar y verificar aparte.
        tx_id: int | None = None

        # Preflight brutal ANTES de tocar nada (T-16c·3): un semáforo ROJO (p. ej.
        # el dir de salida sin permisos) cancela el run de 30+ min / GBs sin adquirir
        # el lock, abrir transacción, ni publicar el evento de inicio. Amarillo/verde
        # no bloquean; el reporte se surface al panel en todos los retornos.
        preflight = self._ensure_preflight()
        preflight_report: PreflightReport | None = None
        if preflight is not None:
            preflight_report = await preflight.run()
            if preflight_report.blocks_mutations:
                red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                logger.warning(
                    "DynDOLOD (stage 9) bloqueado por preflight en rojo: %s",
                    red,
                    extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
                )
                return _attach_preflight(
                    {
                        "status": "error",
                        "success": False,
                        "reason": "PreflightBlocked",
                        "message": f"Preflight en rojo, DynDOLOD cancelado: {red}",
                        "errors": [red],
                    },
                    preflight_report,
                )

        start_time = time.monotonic()
        rolled_back = False
        journal_committed = False
        mutation_started = False
        mutation_coverage_complete = False
        # M-7: rastrear los DirectoryRollback para reportar el resultado REAL del
        # rollback (dr.rollback_completed) en vez de hardcodear rolled_back=True.
        # El AsyncExitStack ejecuta los __aexit__ (restore) ANTES de que corran los
        # except handlers, así que el flag ya está seteado cuando se leen.
        dir_rollbacks: list[DirectoryRollback] = []
        # F1: se leen desde el `except` de error de dominio, que es por donde sale
        # el corte por visibilidad. Viven acá afuera por la misma razón que
        # `mutation_started`: el handler no puede depender de que una variable
        # asignada dentro del `try` haya llegado a existir.
        needs_deployment = False
        # D2 (PR #493): estado durable del handoff leído por las ramas de éxito y
        # de fallo; asignado dentro del `try`, declarado acá por el mismo motivo
        # que `needs_deployment`.
        handoff_resume: DeploymentHandoff | None = None
        handoff_en_supersede: DeploymentHandoff | None = None
        texgen_packaging_intentado = False

        logger.info(
            "Iniciando pipeline DynDOLOD: preset=%s, texgen=%s, snapshot=%s",
            preset,
            run_texgen,
            create_snapshot,
        )

        # 1. Publicar evento de inicio
        await self._publish_started(preset=preset, run_texgen=run_texgen)

        # 2. Inicializar runner
        try:
            runner = self._ensure_runner()
        except DynDOLODExecutionError as exc:
            logger.error(
                "DynDOLOD (stage 9): error inicializando el runner: %s",
                exc,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
            duration = time.monotonic() - start_time
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=False,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "message": str(exc),
                    "errors": [str(exc)],
                    "duration_seconds": duration,
                },
                preflight_report,
            )

        # Rutas de config: fallar rápido ANTES del lock. Un fallo acá cuesta
        # segundos; el mismo fallo adentro cuesta un diálogo modal de la
        # herramienta y horas de timeout. Se chequea SOLO lo declarado: una ruta
        # no declarada la resuelve la herramienta por registro (frágil en el rig
        # con Documentos redirigida a OneDrive — por eso se declara).
        ruta_faltante = self._primera_ruta_de_config_faltante(runner)
        if ruta_faltante is not None:
            msg = f"Ruta declarada por la configuración de DynDOLOD no existe: {ruta_faltante}"
            duration = time.monotonic() - start_time
            logger.error("DynDOLOD (stage 9): %s", msg, extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id})
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(msg,),
                duration_seconds=duration,
                rolled_back=False,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "message": msg,
                    "errors": [msg],
                    "duration_seconds": duration,
                },
                preflight_report,
            )

        # D2 (PR #493): el gate de perfil corre ANTES de cualquier mutación
        # (lock, manifiesto, move-aside) pero DESPUÉS de la inicialización del
        # runner: sin identidad de dueño no existe handoff resumible posible, y
        # el veredicto needs_deployment EXIGE uno — fallar acá (segundos) es la
        # única forma de sostener esa invariante sin terminar en el peor caso
        # de todas las ramas (artifact preservado sin registro durable).
        # ``getattr``: un objeto construido con ``__new__`` (tests de contrato)
        # sin el atributo es "perfil desconocido", que es la semántica del gate.
        if run_texgen and getattr(self, "_mo2_profile", None) is None:
            msg = (
                "No hay un perfil MO2 resoluble para crear el handoff durable de deployment. "
                "Sky-Claw no muta nada sin poder registrar la identidad del dueño del artifact."
            )
            logger.error(
                "DynDOLOD (stage 9): %s",
                msg,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(msg,),
                duration_seconds=time.monotonic() - start_time,
                rolled_back=False,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "reason": "PerfilDesconocido",
                    "message": msg,
                    "errors": [msg],
                },
                preflight_report,
            )

        # D2 (PR #493): la decisión de resume consulta el estado DURABLE del
        # handoff — nunca ``Path.exists`` del mod. Corre ANTES del lock para no
        # sostenerlo durante el digest del árbol (GBs).
        if not run_texgen:
            consulta = await self._consultar_resume(runner)
            if isinstance(consulta, _ResumeBloqueado):
                logger.error(
                    "DynDOLOD (stage 9): resume bloqueado (%s): %s",
                    consulta.reason,
                    consulta.detail,
                    extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
                )
                duration = time.monotonic() - start_time
                await self._publish_completed(
                    preset=preset,
                    run_texgen=run_texgen,
                    success=False,
                    texgen_success=False,
                    dyndolod_success=False,
                    errors=(consulta.detail,),
                    duration_seconds=duration,
                    rolled_back=False,
                )
                return _attach_preflight(
                    {
                        "success": False,
                        "reason": consulta.reason,
                        "message": consulta.detail,
                        "errors": [consulta.detail],
                        "duration_seconds": duration,
                    },
                    preflight_report,
                )
            handoff_resume = consulta

        # DD-1: Directorios regenerados a proteger con rollback move-aside.
        # El backend de snapshots es copy-based/solo-archivos y ``Output/`` puede
        # pesar varios GB; renombrar el dir aparte es O(1) y da rollback real
        # byte-a-byte (evita que un fallo deje texturas/meshes parciales). El
        # move-aside del dir subsume el ``.esp``, así que el lock transaccional ya
        # no snapshotea archivos (``target_files=[]``).
        mods_path = runner._config.mo2_mods_path
        rollback_dirs: list[pathlib.Path] = []
        if create_snapshot:
            rollback_dirs.append(mods_path / runner.DYNDOLLOD_MOD_NAME)
            if run_texgen:
                rollback_dirs.append(mods_path / runner.TEXGEN_MOD_NAME)

        # B (review de #493): el staging CRUDO de TexGen también entra al
        # move-aside, y esta línea es lo que hace que el árbol pertenezca a ESTA
        # corrida en vez de sólo haber cambiado durante ella.
        #
        # El gate de frescura del runner es un predicado ∃ —"algo se escribió"—
        # y nunca pudo ser ∀: con un ``old.dds`` de la corrida anterior y un
        # ``new.dds`` de ésta, la firma agregada del árbol cambia igual, el gate
        # pasa, y el empaquetado copia los DOS al mod. Apartar el staging antes de
        # lanzar lo vuelve vacío por construcción, y a partir de ahí "lo que hay
        # adentro" y "lo que esta corrida generó" son el mismo conjunto.
        #
        # **No cuelga de ``create_snapshot``, y la asimetría es deliberada.** Los
        # otros dos destinos son comodidad de rollback: el operador puede
        # renunciar a ellos. Éste no protege nada — establece la precondición de
        # que el staging sea de la corrida, que es una propiedad del resultado y
        # no una preferencia. Un `create_snapshot=False` que reintrodujera el
        # árbol heredado devolvería el mismo mod contaminado por otra puerta.
        #
        # A-min es su precondición y el orden importa: el backup de move-aside
        # queda de HERMANO del staging, o sea colgando de la raíz administrada,
        # que es exactamente el directorio que el fallback de DynDOLOD podía
        # empaquetar entero. Con la raíz ya declarada no empaquetable, ese residuo
        # es inerte. Su barrido tras una muerte dura lo declara
        # ``rollback_reconciler.construir_productores_de_move_aside``.
        #
        # ``isinstance`` y no ``is not None``: en producción
        # ``DynDOLODConfig.__post_init__`` garantiza un ``Path`` (deriva de un
        # ``game_path`` que ya validó que existe), así que la rama negativa sólo
        # la toman los dobles de test con un ``_config`` mockeado.
        raiz_administrada = runner._config.output_root
        if run_texgen and isinstance(raiz_administrada, pathlib.Path):
            rollback_dirs.append(raiz_administrada / DynDOLODRunner.TEXGEN_OUTPUT_NAME)

        # T-26: los paths que el ritual reescribe (independiente del snapshot) —
        # el files_touched del ActionManifest. Incluye los mods de salida
        # empaquetados Y el staging crudo (DynDOLOD_Output/textures) que la
        # herramienta escribe antes de empaquetar, bajo la raíz MO2 y el dir del
        # exe (review Codex #312): son mutaciones persistentes que el operador
        # puede necesitar auditar/limpiar tras un run fallido.
        manifest_targets: list[pathlib.Path] = [mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME]
        _staging_names = [DynDOLODRunner.DYNDOLLOD_OUTPUT_NAME]
        if run_texgen:
            manifest_targets.append(mods_path / DynDOLODRunner.TEXGEN_MOD_NAME)
            _staging_names.append(DynDOLODRunner.TEXGEN_OUTPUT_NAME)
        # Staging crudo bajo la raíz administrada única (-o:): los candidatos que
        # el runner resuelve (subcarpeta por staging + la raíz como fallback
        # acotado). Ya no se enumeran raíces ajenas (mo2/exe/cwd).
        _staging_root = runner._config.output_root
        if _staging_root is not None:
            manifest_targets += [_staging_root / _name for _name in _staging_names]
            manifest_targets.append(_staging_root)

        # Cobertura honesta: DynDOLOD/TexGen también escriben staging crudo. Esas
        # ubicaciones pueden ser compartidas y todavía no están bajo move-aside;
        # no se amplía el rollback sin evidencia de propiedad exclusiva. Por tanto,
        # una vez que el runner empieza, no hay rollback TOTAL demostrable aunque
        # los outputs administrados sí vuelvan byte-a-byte.
        mutation_coverage_complete = False

        # 3. Ejecutar bajo lock transaccional + rollback de directorios.
        # AsyncExitStack: el lock se adquiere primero y se libera último; los
        # DirectoryRollback se restauran ANTES de soltar el lock.
        try:
            async with contextlib.AsyncExitStack() as tx_stack:
                # Ligado a variable para consultar su ``lease_lost`` desde el veto de
                # los DirectoryRollback de más abajo (review Codex #399).
                tx_lock = SnapshotTransactionLock(
                    lock_manager=self._lock_manager,
                    snapshot_manager=self._snapshot_manager,
                    resource_id="dyndolod-pipeline",
                    agent_id="dyndolod-pipeline-service",
                    target_files=[],
                    metadata={"preset": preset, "run_texgen": run_texgen},
                )
                await tx_stack.enter_async_context(tx_lock)
                # Comenzar transacción en journal DENTRO del lock.
                tx_id = await self._journal.begin_transaction(
                    description=f"DynDOLOD pipeline (preset={preset}, texgen={run_texgen})",
                    agent_id="dyndolod-pipeline-service",
                )

                # T-26 (ADR 0002): la caja negra ANTES de la primera mutación de
                # FS — se persiste el manifiesto ANTES del move-aside de los
                # DirectoryRollback (review Codex #312): si el proceso muere en el
                # gap, el journal ya tiene el manifiesto de la mutación. Fail-closed:
                # si no se puede emitir, se lanza y el pipeline NO corre (ningún
                # move-aside ocurrió todavía; espejo de xedit_service).
                await self._emit_action_manifest(
                    tx_id=tx_id,
                    target_files=manifest_targets,
                    summary=f"Generar LODs (preset={preset}, texgen={run_texgen}) → {len(manifest_targets)} mod(s).",
                )

                # D2 (PR #493): una regeneración con handoff activo lo marca
                # SUPERSEDING ANTES de la primera mutación de FS. Si la
                # transición no se puede hacer (estado concurrente), se aborta
                # sin mutar — nunca dos generaciones sobre el mismo owner.
                if run_texgen:
                    clave_artifact = clave_de_artifact(mods_path / DynDOLODRunner.TEXGEN_MOD_NAME)
                    activo = await self._journal.consultar_handoff_activo(clave_artifact)
                    if activo is not None:
                        ok = await self._journal.transicionar_handoff(
                            activo.handoff_id,
                            desde=activo.state,
                            hacia=HandoffState.SUPERSEDING,
                        )
                        if not ok:
                            raise DynDOLODExecutionError(
                                f"El handoff activo {activo.handoff_id} cambió de estado y no se pudo "
                                "marcar SUPERSEDING: no se muta nada sin ownership resuelto."
                            )
                        handoff_en_supersede = activo

                # Move-aside de los outputs regenerados (primera mutación de FS).
                # El veto de lease alinea este context con el lock que lo envuelve:
                # el lock saltea su rollback tras perder la lease para no pisar a un
                # dueño concurrente, pero los DirectoryRollback salen ANTES que él y
                # sin el veto restaurarían igual, borrando la salida del nuevo dueño
                # (review Codex #399 sobre el hermano Pandora — mismo agujero acá).
                for output_dir in rollback_dirs:
                    dr = DirectoryRollback(output_dir, should_rollback=lambda: not tx_lock.lease_lost)
                    dir_rollbacks.append(dr)
                    await tx_stack.enter_async_context(dr)

                # Ejecutar pipeline.
                #
                # `correlacion_de_transaccion` liga los registros de fallo del
                # RUNNER a esta transacción (SOP §5 regla 5). El runner no conoce
                # el journal —`tx_id` es un concepto de esta capa— pero sí ve el
                # exit code y la causa técnica que los handlers de abajo nunca
                # reconstruyen, así que emite `pipeline_stage` él mismo. Con las
                # dos capas etiquetando, `COUNT(*)` cuenta filas y no incidentes:
                # la métrica pasa a ser `COUNT(DISTINCT tx_id) WHERE
                # pipeline_stage=9`, y sin este `with` ese DISTINCT no existiría.
                #
                # Envuelve TODA la interacción con el runner (pipeline +
                # validación de salida), no solo el pipeline: los registros de
                # `validate_dyndolod_output` están exentos de la etapa pero
                # llevan `tx_id` igual, que es lo que permite unirlos al incidente
                # cuando la corrida termina fallando por otra razón.
                mutation_started = True
                with correlacion_de_transaccion(tx_id):
                    result = await runner.run_full_pipeline(
                        run_texgen=run_texgen,
                        preset=preset,
                        texgen_args=texgen_args,
                        dyndolod_args=dyndolod_args,
                    )

                    # Validar salida de DynDOLOD si fue exitoso
                    if result.success:
                        if result.dyndolod_result is None:
                            # U-06 (review Qodo): la otra mitad del guard encadenado. Hoy
                            # ``run_full_pipeline`` computa ``success`` exigiendo
                            # ``dyndolod_result is not None``, así que este estado NO es
                            # alcanzable — pero el tipo lo permite y el criterio del repo
                            # (U-11) es no reportar éxito sobre un estado indeterminado
                            # solo porque "no debería pasar".
                            # No se loguea acá: el `raise` lo hace `except (DynDOLODExecutionError,
                            # DynDOLODTimeoutError)` más abajo, que preserva `msg` vía `str(exc)`
                            # y agrega `pipeline_stage`/`tx_id`. Loguear también acá duplicaba el
                            # mismo incidente 2-3 veces sin id de correlación (review Qodo, PR #464,
                            # "Duplicación de señal") — una alerta contando por `pipeline_stage=9`
                            # sobrecontaba la tasa de fallos real.
                            msg = "DynDOLOD reportó éxito sin resultado de ejecución"
                            raise DynDOLODExecutionError(msg)

                        output_path = result.dyndolod_result.output_path
                        if output_path is None:
                            # U-06: ``_find_dyndolod_output`` devuelve None cuando no
                            # encuentra el output en ninguna ubicación candidata. Este
                            # guard estaba encadenado al ``if``, así que un exit 0 sin
                            # path SALTEABA la validación y commiteaba el journal como
                            # éxito: falso verde por exit-code sobre un estado donde no
                            # se sabe si DynDOLOD escribió algo.
                            msg = "DynDOLOD no dejó un directorio de salida localizable"
                            raise DynDOLODExecutionError(msg)  # el handler loguea, ver guard anterior

                        is_valid = await runner.validate_dyndolod_output(output_path)
                        if not is_valid:
                            msg = "DynDOLOD output validation failed"
                            raise DynDOLODExecutionError(msg)  # el handler loguea, ver guard anterior

                if not result.success:
                    texgen_packaging_intentado = result.texgen_packaging_attempted
                    # F1 (review de #493): el corte por visibilidad deja un mod de
                    # TexGen empaquetado y VALIDADO que es exactamente lo que el
                    # operador tiene que materializar para poder seguir. Revertirlo
                    # junto con el resto borraba ese artefacto, y como el mensaje de
                    # error le pide precisamente desplegarlo, el reintento
                    # regeneraba lo mismo para volver a fallar: un callejón sin
                    # salida en el default de la GUI (`create_snapshot=True`).
                    #
                    # Se confirma UN move-aside, no el lote: el staging crudo
                    # (`output_root/textures`) sigue revirtiendo, porque su
                    # move-aside no es comodidad de rollback sino la precondición
                    # de B — que el árbol nazca vacío y su contenido sea el de la
                    # corrida. Preservarlo reintroduciría el mod contaminado.
                    #
                    # Con `create_snapshot=False` el mod nunca entró al lote y
                    # sobrevive solo; el bucle no encuentra nada y no hay caso
                    # especial que escribir.
                    if result.needs_deployment:
                        await self._preservar_mod_de_texgen(
                            dir_rollbacks,
                            mods_path / runner.TEXGEN_MOD_NAME,
                            tx_id=tx_id,
                        )
                        needs_deployment = True
                    errors_str = "; ".join(result.errors) if result.errors else "Unknown error"
                    raise DynDOLODExecutionError(f"DynDOLOD pipeline failed: {errors_str}")

                # D2 (PR #493): el cierre del éxito depende de qué camino se
                # recorrió. En un RESUME el principio 10 exige FS seal ANTES del
                # boundary DB (handoff→COMPLETED + TX2→COMMITTED). En un supersede
                # completado, el MISMO orden sella el FS y cierra TX+historia en
                # un boundary (viejo→SUPERSEDED, nuevo→COMPLETED). El camino sin
                # handoff conserva su orden histórico, anclado por los tests
                # preexistentes de cancelación post-commit.
                if handoff_resume is not None:
                    await _commit_directory_rollbacks(dir_rollbacks)
                    await self._journal.completar_handoff_de_resume(tx_id, handoff_resume.handoff_id)
                    journal_committed = True
                elif handoff_en_supersede is not None:
                    mod_textures = mods_path / DynDOLODRunner.TEXGEN_MOD_NAME / DynDOLODRunner.TEXGEN_OUTPUT_NAME
                    digest_final = await asyncio.to_thread(digest_arbol, mod_textures)
                    game_key, mods_root_key, data_key = self._keys_de_identidad(runner)
                    registro_completado = DeploymentHandoff(
                        handoff_id=0,
                        source_tx_id=tx_id,
                        state=HandoffState.COMPLETED,
                        artifact_path=clave_de_artifact(mods_path / DynDOLODRunner.TEXGEN_MOD_NAME),
                        game_key=game_key,
                        mods_root_key=mods_root_key,
                        data_key=data_key,
                        expected_profile=self._mo2_profile or "desconocido",
                        expected_digest=digest_final.digest,
                        expected_files=digest_final.files,
                        expected_bytes=digest_final.bytes,
                        observed_digest=None,
                        observed_files=None,
                        observed_bytes=None,
                        created_at="",
                        updated_at="",
                        completed_at=None,
                        superseded_at=None,
                        superseded_by=None,
                    )
                    await _commit_directory_rollbacks(dir_rollbacks)
                    await self._journal.crear_handoff_de_deployment(
                        tx_id,
                        descripcion=None,
                        registro=registro_completado,
                        viejo=handoff_en_supersede,
                    )
                    journal_committed = True
                else:
                    # Commit en journal
                    await self._journal.commit_transaction(tx_id)
                    journal_committed = True

                    # F1 (review Codex #312): tras el commit el output es FINAL —
                    # confirmar los move-aside para que un fallo post-commit (incl. una
                    # CancelledError durante el informe best-effort, que evade el
                    # ``except Exception``) NO revierta una generación ya committeada al
                    # desenrollar el AsyncExitStack.
                    await _commit_directory_rollbacks(dir_rollbacks)

                # T-28: cerrar la caja negra tras el commit (best-effort — los
                # LODs ya se generaron; un fallo del informe no tumba el run).
                await self._emit_flight_report(tx_id)

                duration = time.monotonic() - start_time
                await self._log_result(result, preset, success=True)
                texgen_ok = result.texgen_result.success if result.texgen_result else False
                dyndolod_ok = result.dyndolod_result.success if result.dyndolod_result else False
                await self._publish_completed(
                    preset=preset,
                    run_texgen=run_texgen,
                    success=True,
                    texgen_success=texgen_ok,
                    dyndolod_success=dyndolod_ok,
                    errors=(),
                    duration_seconds=duration,
                    rolled_back=False,
                )

                logger.info(
                    "Pipeline DynDOLOD exitoso: texgen=%s, dyndolod=%s (%.1fs)",
                    result.texgen_mod_path,
                    result.dyndolod_mod_path,
                    duration,
                )

                # Normalizar pathlib.Path → str de forma recursiva para compatibilidad JSON/WS.
                def normalize_for_serialization(obj: Any) -> Any:
                    if isinstance(obj, pathlib.Path):
                        return str(obj)
                    if isinstance(obj, dict):
                        return {k: normalize_for_serialization(v) for k, v in obj.items()}
                    if isinstance(obj, list):
                        return [normalize_for_serialization(v) for v in obj]
                    return obj

                result_dict = normalize_for_serialization(dataclasses.asdict(result))

                return _attach_preflight(
                    {
                        "success": True,
                        "message": "",
                        **result_dict,
                        "duration_seconds": duration,
                    },
                    preflight_report,
                )

        except _ActionManifestError as exc:
            # El logger.error del incidente va PRIMERO, antes de cualquier await
            # cancelable (review Qodo, PR #464, "Pérdida de señal"): si la task
            # se cancela durante `_cerrar_tx_tras_rollback` —rollback de
            # directorios, potencialmente largo—, la CancelledError se propaga
            # desde dentro de ESTE handler y nada de lo que venga después se
            # ejecuta. Antes del fix de "Duplicación de señal" el guard logueaba
            # inmediatamente antes de lanzar, así que la señal sobrevivía
            # cualquier cancelación posterior; moverla acá restaura esa garantía.
            logger.error(
                "DynDOLOD (stage 9): no se pudo emitir el ActionManifest; abortado: %s",
                exc,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
            # La caja negra no se pudo emitir: ningún LOD se generó. Cerrar la TX
            # sólo si todos los DirectoryRollback observados confirman que no quedó
            # mutación sin resolver (M-7).
            rolled_back = await self._cerrar_tx_tras_rollback(
                tx_id,
                dir_rollbacks,
                journal_committed=journal_committed,
                mutation_started=mutation_started,
                mutation_coverage_complete=mutation_coverage_complete,
                contexto="fallo del manifiesto",
            )
            duration = time.monotonic() - start_time
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=rolled_back,
            )
            detail = f"Manifiesto de vuelo requerido no emitido: {exc}"
            return _attach_preflight(
                {
                    "success": False,
                    "reason": "ActionManifestFailed",
                    "message": detail,
                    "errors": [detail],
                    "duration_seconds": duration,
                    "rolled_back": rolled_back,
                },
                preflight_report,
            )

        except LockAcquisitionError as exc:
            duration = time.monotonic() - start_time
            logger.error(
                "DynDOLOD (stage 9): no se pudo adquirir el lock del pipeline: %s",
                exc,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(f"Lock acquisition failed: {exc}",),
                duration_seconds=duration,
                rolled_back=False,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "message": f"Lock acquisition failed: {exc}",
                    "errors": [f"Lock acquisition failed: {exc}"],
                    "duration_seconds": duration,
                },
                preflight_report,
            )

        except (DynDOLODExecutionError, DynDOLODTimeoutError) as exc:
            # El logger.error del incidente va PRIMERO, antes de cualquier await
            # cancelable (review Qodo, PR #464, "Pérdida de señal"): con el log
            # después de `_cerrar_tx_tras_rollback` —que puede tardar y es
            # cancelable—, una cancelación en esa ventana propagaba la
            # CancelledError desde dentro de ESTE handler y el registro del
            # incidente original (el caso de validación de output, el más común)
            # nunca se emitía. `rolled_back` ya no va en el mismo mensaje —se
            # calcula DESPUÉS— pero no se pierde: si el rollback queda
            # incompleto, `_cerrar_tx_tras_rollback` emite su propio
            # warning/critical con pipeline_stage y tx_id; si se completa, no
            # hay nada urgente que reportar.
            logger.error(
                "DynDOLOD (stage 9): error de dominio del pipeline: %s",
                exc,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )

            if needs_deployment and run_texgen:
                # D2 (PR #493): el veredicto needs_deployment cierra TX1 y crea el
                # handoff durable en UN boundary — TX1 nunca queda PENDING ni
                # ROLLED_BACK: su producto real (TexGen generado y empaquetado)
                # está vivo y autorizado. Si el supersede de un handoff previo
                # estaba en curso, la MISMA frontera lo resuelve (viejo →
                # SUPERSEDED enlazado al nuevo).
                mod_texgen = mods_path / runner.TEXGEN_MOD_NAME
                mod_textures = mod_texgen / runner.TEXGEN_OUTPUT_NAME
                try:
                    digest_final = await asyncio.to_thread(digest_arbol, mod_textures)
                    game_key, mods_root_key, data_key = self._keys_de_identidad(runner)
                    registro = DeploymentHandoff(
                        handoff_id=0,
                        source_tx_id=tx_id,
                        state=HandoffState.AWAITING_DEPLOYMENT,
                        artifact_path=clave_de_artifact(mod_texgen),
                        game_key=game_key,
                        mods_root_key=mods_root_key,
                        data_key=data_key,
                        expected_profile=self._mo2_profile or "desconocido",
                        expected_digest=digest_final.digest,
                        expected_files=digest_final.files,
                        expected_bytes=digest_final.bytes,
                        observed_digest=None,
                        observed_files=None,
                        observed_bytes=None,
                        created_at="",
                        updated_at="",
                        completed_at=None,
                        superseded_at=None,
                        superseded_by=None,
                    )
                    handoff_id = await self._journal.crear_handoff_de_deployment(
                        tx_id,
                        descripcion=(
                            "TexGen generado y empaquetado; esperando deployment en el Data (DynDOLOD pendiente)"
                        ),
                        registro=registro,
                        viejo=handoff_en_supersede,
                    )
                except (OSError, JournalTransactionError) as e:
                    # Ventana honesta: el artifact vive pero no se pudo autorizar.
                    # TX1 queda PENDING y el reconciler de arranque la degrada a
                    # INDETERMINATE (evidencia: TX que nombra el mod + mod vivo).
                    logger.critical(
                        "DynDOLOD (stage 9): '%s' quedó PRESERVADA a propósito pero el handoff "
                        "durable NO se pudo crear (%s): la TX queda para el reconciler de arranque.",
                        mod_texgen,
                        e,
                        extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
                    )
                    handoff_id = None
                rolled_back = False
                logger.warning(
                    "DynDOLOD (stage 9): '%s' queda PRESERVADA a propósito — %s. Es el artifact "
                    "que el operador tiene que materializar en el Data para poder continuar.",
                    mod_texgen,
                    (
                        f"TX {tx_id} COMMITTED con handoff {handoff_id} AWAITING_DEPLOYMENT"
                        if handoff_id is not None
                        else "sin handoff durable (lo degradará el reconciler de arranque)"
                    ),
                    extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
                )
            else:
                # M-7: reportar el resultado REAL del rollback. Los __aexit__ de los
                # DirectoryRollback ya corrieron (restore best-effort); rolled_back es
                # True sólo si TODOS completaron. Un rmtree/rename fallido deja el output
                # parcial en disco y debe reflejarse como rolled_back=False.
                rolled_back = await self._cerrar_tx_tras_rollback(
                    tx_id,
                    dir_rollbacks,
                    journal_committed=journal_committed,
                    mutation_started=mutation_started,
                    mutation_coverage_complete=mutation_coverage_complete,
                    contexto="error de dominio",
                )
                # D2: un supersede en curso que NO terminó en needs_deployment
                # resuelve su matriz de fallos A/B/C contra la identidad durable.
                if handoff_en_supersede is not None:
                    await self._resolver_fallo_de_supersede(
                        runner=runner,
                        handoff_previo=handoff_en_supersede,
                        packaging_intentado=texgen_packaging_intentado,
                        create_snapshot=create_snapshot,
                        tx_id=tx_id,
                    )

            duration = time.monotonic() - start_time

            await self._log_result_error(preset, str(exc), tx_id, rolled_back)
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=rolled_back,
            )
            # F1: el payload distingue "se rompió" de "está listo y falta
            # desplegarlo". `success` sigue en False —DynDOLOD no corrió y no hay
            # LODs— pero un rojo con `needs_deployment` tiene continuación, y el
            # path es la autoridad de esa continuación: es el artefacto que hay que
            # materializar, no una regeneración futura que podría diferir.
            payload: dict[str, Any] = {
                "success": False,
                "message": str(exc),
                "errors": [str(exc)],
                "duration_seconds": duration,
                "rolled_back": rolled_back,
            }
            if needs_deployment:
                payload["needs_deployment"] = True
                payload["dyndolod_started"] = False
                # D2 (F-D6): el path viaja en AMBAS formas — con y sin
                # create_snapshot, con y sin move-aside del mod — siempre que el
                # artifact empaquetado exista.
                mod_texgen = mods_path / runner.TEXGEN_MOD_NAME
                if mod_texgen.is_dir():
                    payload["texgen_mod_path"] = str(mod_texgen)
            return _attach_preflight(payload, preflight_report)

        except asyncio.CancelledError:
            # Cancelación de task — hacer cleanup mínimo y re-lanzar.
            duration = time.monotonic() - start_time
            if journal_committed:
                # Post-commit (review CodeRabbit, PR #464): `commit_transaction`
                # ya corrió, así que la etapa 9 YA tuvo éxito — lo cancelado es
                # post-proceso best-effort (confirmar rollbacks / emitir el
                # flight report), no la etapa en sí. Mismo criterio que
                # `_emit_flight_report`/`_log_result_error`: no pipeline_stage
                # para algo que ocurre después de que la etapa ya completó.
                logger.warning(
                    "DynDOLOD: cancelación post-commit tras %.1fs (TX %d ya committeada)",
                    duration,
                    tx_id,
                    extra={"operation_type": "dyndolod_post_commit_cancelled", "tx_id": tx_id},
                )
            else:
                logger.warning(
                    "DynDOLOD (stage 9): pipeline cancelado tras %.1fs",
                    duration,
                    extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
                )
            await self._cerrar_tx_tras_rollback(
                tx_id,
                dir_rollbacks,
                journal_committed=journal_committed,
                mutation_started=mutation_started,
                mutation_coverage_complete=mutation_coverage_complete,
                contexto="cancelación",
            )
            raise

        except Exception as exc:
            # El logger.error del incidente va PRIMERO, antes de cualquier await
            # cancelable (review Qodo, PR #464, "Pérdida de señal") — mismo
            # motivo que el handler de dominio arriba.
            logger.error(
                "DynDOLOD (stage 9): error inesperado del pipeline: %s",
                exc,
                exc_info=True,
                extra={"pipeline_stage": _ETAPA_DYNDOLOD, "tx_id": tx_id},
            )
            # PREVENCIÓN T11: red de seguridad final con resultado REAL del
            # rollback. Una TX queda PENDING a propósito si algún move-aside no
            # pudo confirmar su recuperación (ver handler de dominio arriba).
            rolled_back = await self._cerrar_tx_tras_rollback(
                tx_id,
                dir_rollbacks,
                journal_committed=journal_committed,
                mutation_started=mutation_started,
                mutation_coverage_complete=mutation_coverage_complete,
                contexto="error inesperado",
            )
            duration = time.monotonic() - start_time

            await self._log_result_error(preset, str(exc), tx_id, rolled_back)
            await self._publish_completed(
                preset=preset,
                run_texgen=run_texgen,
                success=False,
                texgen_success=False,
                dyndolod_success=False,
                errors=(str(exc),),
                duration_seconds=duration,
                rolled_back=rolled_back,
            )
            return _attach_preflight(
                {
                    "success": False,
                    "message": str(exc),
                    "errors": [str(exc)],
                    "duration_seconds": duration,
                    "rolled_back": rolled_back,
                },
                preflight_report,
            )

    # ------------------------------------------------------------------
    # Dry-run / preview (plan-only estimate)
    # ------------------------------------------------------------------

    async def _preview(self, *, preset: str, run_texgen: bool) -> dict[str, Any]:
        """Plan-only dry-run: estimate the LODs DynDOLOD WOULD generate.

        The TexGen/DynDOLOD executables are never launched (matrix: DynDOLOD is
        plan-only — it is the most expensive stage), so nothing is locked,
        journaled, or written.  Output directories are derived from the path
        resolver alone, so no DynDOLOD binary is required to preview.
        """
        # Local import to avoid an import-time cycle (local.tools -> orchestrator).
        from sky_claw.app.orchestrator.preview.manifest import LODPlan, StageChangeSet

        mo2_mods_path = self._path_resolver.get_mo2_mods_path()
        dyndolod_dir = (
            str(mo2_mods_path / DynDOLODRunner.DYNDOLLOD_MOD_NAME)
            if mo2_mods_path
            else DynDOLODRunner.DYNDOLLOD_MOD_NAME
        )

        would_generate = ["DynDOLOD.esp"]
        output_dirs = [dyndolod_dir]
        if run_texgen:
            # F-007: el nombre del mod sale de la constante canónica del runner —
            # el ancla AST de tests exige que el flujo durable no hardcodee el
            # literal.
            texgen_dir = (
                str(mo2_mods_path / DynDOLODRunner.TEXGEN_MOD_NAME) if mo2_mods_path else DynDOLODRunner.TEXGEN_MOD_NAME
            )
            would_generate.append("TexGen textures")
            output_dirs.append(texgen_dir)

        lod_plan = LODPlan(
            preset=preset,
            would_generate=would_generate,
            # The exact asset count is unknowable without running DynDOLOD; the
            # estimate is intentionally 0 and flagged as such in the warnings.
            estimated_assets=0,
            output_dirs=output_dirs,
        )
        change_set = StageChangeSet(
            stage="dyndolod",
            executed_for_real=False,
            files_touched=output_dirs,
            lod_plan=lod_plan,
            warnings=["LOD asset count is an estimate; TexGen/DynDOLOD are not run in preview."],
            summary=(
                f"Would generate LODs (preset={preset}, texgen={run_texgen}) into {dyndolod_dir} — DynDOLOD not run."
            ),
        )
        logger.info("DynDOLOD dry-run preview: %s", change_set.summary)
        return {
            "status": "dry_run_preview",
            "message": change_set.summary,
            "change_set": change_set.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------
    # Eventos
    # ------------------------------------------------------------------

    async def _publish_started(self, *, preset: str, run_texgen: bool) -> None:
        """Publica evento de inicio del pipeline (etapa 9 ASISTIDA)."""
        payload = DynDOLODPipelineStartedPayload(
            preset=preset,
            run_texgen=run_texgen,
            assisted=True,
        )
        await self._event_bus.publish(
            Event(
                topic="pipeline.dyndolod.started",
                payload=payload.to_log_dict(),
                source="dyndolod-pipeline-service",
            )
        )

    async def _publish_completed(
        self,
        *,
        preset: str,
        run_texgen: bool,
        success: bool,
        texgen_success: bool,
        dyndolod_success: bool,
        errors: tuple[str, ...],
        duration_seconds: float,
        rolled_back: bool,
    ) -> None:
        """Publica evento de finalización del pipeline."""
        payload = DynDOLODPipelineCompletedPayload(
            preset=preset,
            run_texgen=run_texgen,
            success=success,
            texgen_success=texgen_success,
            dyndolod_success=dyndolod_success,
            errors=errors,
            duration_seconds=duration_seconds,
            rolled_back=rolled_back,
        )
        await self._event_bus.publish(
            Event(
                topic="pipeline.dyndolod.completed",
                payload=payload.to_log_dict(),
                source="dyndolod-pipeline-service",
            )
        )

    # ------------------------------------------------------------------
    # Journal helpers
    # ------------------------------------------------------------------

    async def _log_result(
        self,
        result: DynDOLODPipelineResult,
        preset: str,
        *,
        success: bool,
    ) -> None:
        """Registra el resultado del pipeline mediante logging estructurado.

        El outcome transaccional ya queda persistido por
        ``commit_transaction``/``mark_transaction_rolled_back``; este helper
        añade un log estructurado con detalles del pipeline para observabilidad.
        """
        logger.info(
            "DynDOLOD pipeline result",
            extra={
                "agent_id": "dyndolod-pipeline-service",
                "operation_type": ("dyndolod_pipeline_complete" if success else "dyndolod_pipeline_failed"),
                "file_path": (str(result.dyndolod_mod_path) if result.dyndolod_mod_path else ""),
                "success": success,
                "preset": preset,
                "texgen_success": (result.texgen_result.success if result.texgen_result else False),
                "dyndolod_success": (result.dyndolod_result.success if result.dyndolod_result else False),
                "errors": result.errors,
            },
        )

    async def _log_result_error(
        self,
        preset: str,
        error_msg: str,
        tx_id: int | None = None,
        rolled_back: bool | None = None,
    ) -> None:
        """Registra el resultado fallido del pipeline mediante logging estructurado.

        Gemelo de `_log_result` (éxito) en el canal de fallo: ese usa ``info`` y
        ``operation_type="dyndolod_pipeline_complete"``; este usa ``error`` y
        ``operation_type="dyndolod_pipeline_failed"`` — ninguno de los dos lleva
        `pipeline_stage`, a propósito. Es el outcome-record del run, siempre
        emitido JUNTO al `logger.error` del handler que lo llama, nunca solo: por
        construcción es una repetición mecánica del MISMO incidente que ese
        handler ya reportó con la etapa, no un fallo adicional. Sí llevaba
        `pipeline_stage` hasta la review Qodo del PR #464 ("Sobreconteo de
        señal"): CodeRabbit/Codex pedían que este registro fuera identificable
        por stage para no quedar fuera de un filtro, pero eso hacía que un
        ``COUNT(*) WHERE pipeline_stage=9`` contara 2 por cada incidente de
        dominio/inesperado — el mismo sobreconteo que "Duplicación de señal" ya
        había señalado, reintroducido por el propio fix que lo cerraba. La
        identidad correcta para filtrar este registro YA existía:
        `operation_type`.
        """
        logger.error(
            "DynDOLOD pipeline failed",
            extra={
                "agent_id": "dyndolod-pipeline-service",
                "operation_type": "dyndolod_pipeline_failed",
                "file_path": "",
                "success": False,
                "preset": preset,
                "error": error_msg,
                # tx_id sí se mantiene: sin él, este registro y el del handler que
                # lo invoca no se pueden correlacionar entre sí ni con el rollback
                # posterior (review Qodo, PR #464, "Duplicación de señal"). `None`
                # es un caso real: los llamadores sin `tx_id` explícito son paths
                # donde la TX puede no haberse abierto aún.
                "tx_id": tx_id,
                # El resultado del rollback vive acá desde la review Qodo del PR
                # #464 ("Pérdida de señal", segunda vuelta). El fix de la primera
                # vuelta adelantó el `logger.error` del handler para que
                # sobreviviera a una cancelación, y al hacerlo perdió el
                # `rolled_back=%s` que ese mensaje llevaba —se calcula después—.
                # La justificación de entonces solo cubría la rama de FALLO (si el
                # rollback queda incompleto, `_cerrar_tx_tras_rollback` emite su
                # propio warning/critical): en la rama de ÉXITO no quedaba ningún
                # registro diciendo que el rollback se confirmó, así que en los
                # logs no se distinguía "rollback confirmado" de "rollback no
                # aplicable". Este es el sitio correcto para reponerlo: el
                # outcome-record ya se emite después de calcularlo, así que no
                # reintroduce la ventana de cancelación ni re-emite el incidente.
                # `None` = el llamador no lo computa (no todos los paths lo tienen).
                "rolled_back": rolled_back,
            },
        )

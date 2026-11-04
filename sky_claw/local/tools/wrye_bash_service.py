"""WryeBashPipelineService — las dos fases ASISTIDAS de la etapa 6 (ADR 0016).

El servicio nació como el ritual mutante serializado de Wrye Bash: tomaba los locks
``bashed-patch`` + ``load-order``, emitía el ``ActionManifest``, abría una TX y corría
``bash.py`` para construir el Bashed Patch. Ese camino **no puede construir nada** —Wrye
Bash no expone build headless: ``-b/--backup`` es el backup de settings y ningún flag
reconstruye el parche—, así que cada invocación tomaba los locks y la TX para fallar
cerrado. El ADR 0016 lo reemplazó por una etapa **asistida de dos fases**, y el PR C
retiró el camino headless entero (``execute_pipeline`` y sus helpers de caja negra):
después del PR B no quedaba **ningún** caller de producción.

Lo que queda es lo que el servicio hace hoy, y **no muta el artefacto, el perfil ni el
load order**: no toma locks, no abre TX ni emite caja negra. Ojo con leer eso como "no
escribe un solo byte": el sensor de **permisos** del preflight es un write-probe real
—crea ``.skyclaw_probe_<uuid>.tmp`` en el destino y lo borra— y en el dominio USVFS el
destino sondeado es el ``overwrite`` del perfil. Es la única escritura de la etapa, es
transitoria, y si el borrado falla el servicio **reporta el residuo** (amarillo, no
bloqueante) en vez de tragárselo (ver ``tests/test_wrye_bash_asistida.py``, contrato de
escritura de la fase 1).

1. ``preparar_etapa_asistida`` — instrucciones para el operador, huella previa del
   artefacto y estado del parche en el ``plugins.txt`` del perfil. No muta: no toma
   locks, no abre TX, no emite caja negra, y declara ``success=False`` con
   ``pendiente=True`` porque preparar no construye nada.
2. ``verificar_etapa_asistida`` — veredicto por evidencia en disco (existencia, regular,
   no vacío, habilitado en el perfil) y frescura **sólo** si el llamador trae el testigo
   de la fase 1. No se deriva de ningún exit code: no hay subproceso que consultar.
3. ``ejecutar_fase_asistida`` — el punto ÚNICO de enrutado que eligen las tres
   superficies de producción (dispatcher de la GUI, ``SupervisorAgent`` y la tool del
   agente LLM).

El preflight se construye **por dominio de visibilidad**: ``usvfs`` para el operador que
abre Wrye Bash desde MO2 —el único ejecutor que queda— y ``physical`` para lo que leía el
runner retirado, que sigue siendo un dominio válido del caché porque el namespace es una
decisión explícita del llamador. Un sensor del namespace equivocado no es conservador, es
un rojo falso que bloquea sin causa. El guard M-04 (límite de plugins) se **inyecta** desde
el supervisor en vez de vivir acá.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, read_enabled_mods
from sky_claw.local.tools.output_targets import ArtefactoEnOverlay, bashed_patch_en_overlay, bashed_patch_target
from sky_claw.local.tools.wrye_bash_asistida import (
    FASE_PREPARAR,
    FASE_VERIFICAR,
    FASES_ASISTIDAS,
    TestigoPrevio,
    preparacion_como_resultado,
    preparar_etapa_asistida,
    veredicto_como_resultado,
    veredicto_etapa_asistida,
)

if TYPE_CHECKING:
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.local.validators.preflight import PreflightReport, PreflightService

logger = logging.getLogger(__name__)

#: Número canónico de la etapa de Wrye Bash en el pipeline (AGENTS.md §5.5).
_ETAPA_WRYE_BASH = 6

#: Dominios de visibilidad de datos (ADR 0016). El ejecutor que queda es el OPERADOR,
#: que abre Wrye Bash desde MO2 y por lo tanto lee el OVERLAY del perfil bajo la USVFS;
#: ``DOMINIO_FISICO`` sobrevive porque el namespace es una decisión explícita del
#: llamador (y era el del runner spawneado que el PR C retiró). El preflight se
#: construye por dominio porque un sensor del namespace equivocado no es conservador:
#: es un rojo falso que bloquea al operador sin causa.
DOMINIO_FISICO = "physical"
DOMINIO_USVFS = "usvfs"

#: Separadores de ruta que no pueden aparecer en un nombre de perfil: ``_plugins_txt_path`` une
#: ``profiles/<perfil>/plugins.txt``, así que un separador o un componente ``.`` / ``..`` saldría del
#: árbol de la instancia (ADR 0016, fase 2).
_SEPARADORES_DE_PERFIL = ("/", "\\", ":")

#: Archivo de prioridad del perfil, hermano del ``plugins.txt``. Se nombra acá —y no se
#: reconstruye a mano— porque es el contrato que ``vfs_attestation.read_enabled_mods``
#: parsea; este servicio lo ubica, no lo interpreta.
_NOMBRE_MODLIST = "modlist.txt"


def _testigo_desde_resultado(testigo_previo: dict[str, object] | None) -> TestigoPrevio | None:
    """Reconstruye el ``TestigoPrevio`` que viajó por el tool-result, o ``None``.

    Falla cerrado: una forma inesperada (falta una clave, tipo equivocado) devuelve
    ``None`` — el veredicto sigue siendo válido pero declara la frescura no probada, en
    vez de inventar un testigo que después daría un ``SIN_CAMBIO`` falso.

    Eso incluye la combinación **imposible** ``{existia: True, sha256: None}``: "estaba,
    pero no sé cuál". Los tipos por separado la aceptaban (``bool`` y ``None`` son
    válidos cada uno) y el resultado era un testigo que hacía
    ``frescura_demostrable() == True`` por la vía de ``sha256 != huella.sha256`` aunque
    la fase 1 nunca hubiera podido identificar el archivo. El invariante vive en
    ``TestigoPrevio.__post_init__``: acá se traduce su rechazo a ``None``.
    """
    if not isinstance(testigo_previo, dict):
        return None
    existia, sha256 = testigo_previo.get("existia"), testigo_previo.get("sha256")
    if not isinstance(existia, bool) or not (sha256 is None or isinstance(sha256, str)):
        return None
    if sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        return None
    try:
        return TestigoPrevio(existia=existia, sha256=sha256)
    except ValueError:
        logger.warning("[FASE-6-asistida] testigo_previo inconsistente descartado: %r", testigo_previo)
        return None


def _normalizar_dominio(dominio: str | None, *, default: str = DOMINIO_FISICO) -> str:
    """Normaliza el alias 'virtual_usvfs' al canónico DOMINIO_USVFS."""
    dom = default if dominio is None else dominio
    if dom == "virtual_usvfs":
        return DOMINIO_USVFS
    return dom


def _perfil_es_usable(profile: str) -> bool:
    """True si *profile* puede unirse a ``profiles/<perfil>/`` sin escapar del árbol."""
    return bool(profile) and profile not in (".", "..") and not any(sep in profile for sep in _SEPARADORES_DE_PERFIL)


def _attach_preflight(result: dict[str, Any], report: PreflightReport | None) -> dict[str, Any]:
    """Adjunta el reporte de preflight al ``result`` cuando no está verde.

    Mismo criterio que ``loot_service``/``xedit_service``/``dyndolod_service``/
    ``pandora_service`` (T-16b/T-16c): un semáforo verde no ensucia el dict;
    amarillo/rojo viajan como ``result["preflight"]`` para que el panel lo renderice.
    """
    if report is not None and report.status.value != "green":
        result["preflight"] = report.to_dict()
    return result


class WryeBashPipelineService:
    """Las dos fases asistidas de la etapa 6 (ADR 0016): preparar y verificar.

    Dependencias inyectadas (DI): ``path_resolver`` para ubicar el artefacto, el perfil y
    el ``plugins.txt``; ``preflight`` para los sensores (inyectado = el llamador ya eligió
    el dominio); ``plugin_limit_guard`` (guard M-04 compartido) opcional — si no se inyecta,
    no se valida el límite de plugins, que es honesto: no hay gate que mienta verde.

    **Sin locks, sin snapshot manager y sin journal, a propósito.** Las dos fases no mutan
    el artefacto ni el estado del juego, así que no hay nada que serializar ni transacción
    que abrir; el PR C retiró esas dependencias junto con ``execute_pipeline`` (ver el
    docstring del módulo). Aceptarlas "por compatibilidad" invitaría a reconstruir el camino
    headless por partes. La excepción declarada es el write-probe del sensor de permisos,
    que no toca el parche: ver ese mismo docstring.
    """

    def __init__(
        self,
        *,
        path_resolver: PathResolutionService | None = None,
        plugin_limit_guard: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        preflight: PreflightService | None = None,
        default_domain: str = DOMINIO_FISICO,
    ) -> None:
        self._path_resolver = path_resolver
        self._plugin_limit_guard = plugin_limit_guard
        # Preflight inyectable (tests) o construido perezosamente en el primer uso.
        # Inyectado = el llamador ya eligió los sensores: vale para AMBOS dominios. Se
        # guarda aparte para no confundirlo con el caché, que este servicio sí distingue.
        self._preflight_inyectado = preflight
        # ``_preflight`` es el caché del dominio FÍSICO (el runner ya no existe; se
        # conserva el dominio porque ``_ensure_preflight`` distingue los dos namespaces).
        self._preflight = preflight
        # Preflight del dominio USVFS (etapa asistida): mismo set MENOS el sensor de
        # visibilidad, que mide el Data físico y ahí sería un rojo falso (ver
        # ``_ensure_preflight`` y el docstring de ``build_vfs_visibility_sensor``).
        self._preflight_usvfs: PreflightService | None = None
        self._preflight_cache: dict[tuple[str, str | None], PreflightService] = {}
        self._preflight_lock = threading.Lock()
        self._default_domain = default_domain

    def _ensure_preflight(self, *, dominio: str | None = None, profile: str | None = None) -> PreflightService | None:
        """Construye perezosamente el preflight de Wrye Bash (T-16c, PR B, FASE 6).

        Wrye Bash arma el Bashed Patch leyendo TODO el load order activo. Sensores
        relevantes — el mismo set que DynDOLOD/Synthesis, porque es un ritual
        plugin-based: **permisos de escritura** sobre el destino del Bashed Patch,
        **símbolos/junctions** en las rutas crudas, **masters faltantes** y
        **límites full/light** del perfil MO2 activo, y **overwrite sucio** (uno
        sucio hace el diff inatribuible aunque el patch no aterrice ahí).
        NO cablea la versión de LOOT (irrelevante). Reusa las primitivas compartidas
        (T-16d). Sensores no resolubles → ``None`` (omitidos con ``omit_unconfigured``).
        Sin game/MO2 resoluble → ``None`` (sin gate, mismo criterio que sus hermanos).

        **``dominio`` elige QUÉ namespace de datos se mide, y no es un detalle de
        estilo.** Después del PR C la etapa 6 tiene UN solo ejecutor —el humano, que abre
        Wrye Bash desde MO2— y ese proceso SÍ hereda la USVFS:

        * ``preparar_etapa_asistida`` / ``verificar_etapa_asistida`` **no spawnean
          nada**: el humano abre Wrye Bash desde MO2 y ese proceso SÍ hereda la USVFS.
          Medir el ``Data`` físico ahí da rojo sobre un perfil sano —un perfil que vive
          entero en ``mods/`` no tiene ninguno de sus plugins en ``Data``— y la
          preparación se bloqueaba con ``PreflightBlocked`` sin que hubiera nada que
          arreglar. El propio contrato del sensor lo dice: *"sólo cablearlo cuando el
          backend que ejecutará la corrida consume ese namespace (dominio
          ``physical``); los backends brokered MO2/USVFS leen el overlay del perfil y
          medir el Data físico ahí es un rojo falso"* (PR-586F).

        Por eso el dominio USVFS omite ESE sensor nomás: los demás (permisos,
        junctions, masters, límites, overwrite) miden hechos del disco que valen igual
        bajo la VFS. La visibilidad real del overlay en ese dominio no se declara
        verde: la aporta :meth:`_artefacto_asistido`, que busca el patch en las capas
        del perfil y lo encuentra —o no— con evidencia.
        """
        dom = _normalizar_dominio(dominio, default=self._default_domain)
        if dom not in (DOMINIO_FISICO, DOMINIO_USVFS):
            raise ValueError(f"dominio de datos desconocido: {dom!r}")
        with self._preflight_lock:
            if self._preflight_inyectado is not None:
                return self._preflight_inyectado
            if dom == DOMINIO_USVFS and profile is None and self._preflight_usvfs is not None:
                return self._preflight_usvfs
            if dom == DOMINIO_FISICO and profile is None and self._preflight is not None:
                return self._preflight
            cache_key = (dom, profile)
            if cache_key in self._preflight_cache:
                return self._preflight_cache[cache_key]
            if self._path_resolver is None:
                return None

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
                build_master_order_sensor,
                build_mo2_profile_sources_resolver,
                build_modlist_sensors,
                build_overwrite_sensor,
                build_vfs_sensor,
                build_vfs_visibility_sensor,
            )
            from sky_claw.local.validators.write_permissions import WritePermissionsChecker

            # vfs sobre el árbol de DATOS (el que los sensores leen).
            # scan_mods_dir: la raíz MO2 de acá ya está VALIDADA (el guard de arriba
            # exige un Path de datos), así que enumerar mods/ es seguro
            # — el False hardcodeado dejaba ciego el scan de symlinks (U-01).
            # mods_dir: el MODS_DIR DECLARADO (``mod_directory`` puede vivir fuera
            # de <datos>/mods; reconstruirlo a mano sería un scan ciego).
            mods_dir = self._path_resolver.get_mo2_mods_path_best_effort()
            vfs_checker = build_vfs_sensor(
                raw_game=self._path_resolver.get_skyrim_path_raw(),
                raw_mo2=mo2,
                scan_mods_dir=True,
                mods_dir=mods_dir,
            )

            # Permisos: targets recalculados POR CORRIDA dentro del closure (freshness,
            # patrón #252/#311) — un destino creado read-only entre corridas debe verse.
            def _permissions() -> Any:
                return WritePermissionsChecker(targets=self._permission_targets(dominio=dom)).check()

            overwrite_check = build_overwrite_sensor(mo2 / "overwrite")

            resolver = build_mo2_profile_sources_resolver(
                game=game,
                mo2=mo2,
                profile=profile or self._path_resolver.get_active_profile(),
                mods_dir=mods_dir,
            )
            masters_check, limits_check = build_modlist_sensors(resolver) if resolver is not None else (None, None)
            # El merge de Leveled Lists consume el load order ya estabilizado por
            # LOOT (stage 5): un master que carga después de su dependiente es CTD.
            order_check = build_master_order_sensor(resolver) if resolver is not None else None

            # U-01 SOLO en el dominio físico: el Bashed Patch spawneado se arma leyendo
            # TODO el load order activo sobre el Data físico; sin la USVFS heredada
            # saldría un patch del juego base, silenciosamente vacío. Bajo la USVFS del
            # operador ese mismo sensor mide un Data que nadie va a leer (rojo falso).
            visibility_check = (
                build_vfs_visibility_sensor(game=game, sources_resolver=resolver) if dom == DOMINIO_FISICO else None
            )

            preflight = PreflightService(
                vfs_checker=vfs_checker,
                permissions_check=_permissions,
                overwrite_check=overwrite_check,
                masters_check=masters_check,
                limits_check=limits_check,
                order_check=order_check,
                visibility_check=visibility_check,
                omit_unconfigured=True,
            )
            self._preflight_cache[cache_key] = preflight
            if profile is None:
                if dom == DOMINIO_USVFS and self._preflight_usvfs is None:
                    self._preflight_usvfs = preflight
                elif dom == DOMINIO_FISICO and self._preflight is None:
                    self._preflight = preflight
            return preflight

    def _permission_targets(self, *, dominio: str = DOMINIO_FISICO) -> list[pathlib.Path]:
        """Directorio donde el ejecutor de *dominio* escribe el artefacto de la etapa 6.

        **El dominio físico escribe en el ``Data`` del juego y solo ahí**: ``bash.py``
        corre con ``cwd=game_path`` y sin ruta de salida en el comando, y el subproceso
        se spawnea directo (no hereda la USVFS), así que la redirección al
        ``overwrite`` de MO2 —que este método sondeaba— **no puede ocurrir**. El
        invariante y su porqué viven en ``tools.output_targets`` (U-01 parte 2).

        **El dominio USVFS escribe en el ``overwrite`` del perfil.** La VFS *redirige*
        la escritura: no hereda los permisos del ``Data``. En una instalación de Steam
        ese ``Data`` suele estar bajo ``Program Files`` y ser de sólo lectura —que es
        justamente el escenario para el que existe MO2—, así que sondearlo acá da un
        rojo crítico y ``preparar_etapa_asistida`` bloquea un perfil sano. Es el mismo
        error que el sensor de visibilidad, en otro sensor: medir el namespace
        equivocado no es conservador, es un falso negativo del operador.

        El ``WritePermissionsChecker`` se salta los inexistentes y se resuelve por
        corrida (freshness), así que devolver una ruta aún ausente es seguro.
        """
        dom = _normalizar_dominio(dominio, default=DOMINIO_FISICO)
        if dom == DOMINIO_USVFS:
            if self._path_resolver is None:
                return []
            mo2 = self._path_resolver.get_mo2_instance_data_root()
            # ``overwrite`` es el destino canónico de la redirección USVFS para un
            # archivo nuevo: ningún mod lo provee, así que la VFS lo deja ahí.
            return [mo2 / "overwrite"] if isinstance(mo2, pathlib.Path) else []
        destino = self._bashed_patch_path()
        return [destino.parent] if destino is not None else []

    def _bashed_patch_path(self) -> pathlib.Path | None:
        """Ruta del Bashed Patch desde el resolver, o ``None`` si no resuelve."""
        game = self._path_resolver.get_skyrim_path() if self._path_resolver is not None else None
        return bashed_patch_target(game if isinstance(game, pathlib.Path) else None)

    def _mods_habilitados(self, profile: str) -> tuple[str, ...] | None:
        """Mods del perfil en prioridad CRECIENTE, o ``None`` si no se pudieron enumerar.

        Tres respuestas distintas, y la diferencia es el punto: la tupla vacía es una
        observación (el perfil no habilita ningún mod, así que ``overwrite`` + ``Data``
        son el overlay completo); ``None`` es un fracaso de lectura y **degrada la
        cobertura** del veredicto, que entonces no puede afirmar ausencia.

        Se lee con la primitiva canónica del repo (``read_enabled_mods``), no con un
        parser propio: la prioridad del overlay ya está resuelta ahí, con su formato y
        su normalización, y una segunda lectura sería la clase de divergencia que este
        repo ya pagó (``modlist.txt`` es el único contrato de prioridad de MO2).
        """
        plugins_txt = self._plugins_txt_path(profile)
        if plugins_txt is None:
            return None
        modlist = plugins_txt.parent / _NOMBRE_MODLIST
        # ``is_file()`` NO sirve para esta pregunta: devuelve ``False`` también cuando la
        # ruta es un directorio o cuando ``stat`` falla (traga el ``OSError``), y las dos
        # cosas significan lo contrario de "no hay mods". Sólo ``FileNotFoundError``
        # prueba la ausencia —MO2 escribe el modlist al tocar la lista—; cualquier otro
        # resultado lo decide el lector canónico, que falla cerrado.
        try:
            modlist.lstat()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            logger.warning("[FASE-6-asistida] modlist inaccesible para %r: %s", profile, exc)
            return None
        try:
            return read_enabled_mods(modlist)
        except VfsAttestationError as exc:
            logger.warning("[FASE-6-asistida] modlist ilegible para %r: %s", profile, exc)
            return None

    def _artefacto_asistido(self, profile: str) -> ArtefactoEnOverlay | None:
        """Dónde buscar el ``Bashed Patch`` en la etapa ASISTIDA: la capa que gana en el overlay.

        La verificación asistida mira un artefacto que construyó **el operador dentro de
        MO2**, con la USVFS viva: el patch puede estar en ``overwrite/`` o en un mod de
        salida habilitado. ``_bashed_patch_path`` (y ``output_targets.bashed_patch_target``)
        modelan la corrida SPAWNEADA, que escribe físicamente en ``Data``; usarlos acá
        producía ``ARTEFACTO_AUSENTE`` sobre un artefacto que el juego sí carga.

        Los dos resolvers se quedan, porque describen dos ejecutores distintos (ver el
        docstring de ``tools.output_targets``). Este usa el que corresponde al operador.
        """
        if self._path_resolver is None:
            return None
        game = self._path_resolver.get_skyrim_path()
        mo2 = self._path_resolver.get_mo2_instance_data_root()
        mods_dir = self._path_resolver.get_mo2_mods_path_best_effort()
        mods_habilitados = self._mods_habilitados(profile)
        encontrado = bashed_patch_en_overlay(
            game=game if isinstance(game, pathlib.Path) else None,
            mo2=mo2 if isinstance(mo2, pathlib.Path) else None,
            mods_dir=mods_dir if isinstance(mods_dir, pathlib.Path) else None,
            mods_habilitados=mods_habilitados,
        )
        if encontrado is not None and not encontrado.cobertura_completa:
            # Degradación con nombre: no se pudo enumerar alguna capa que podría estar
            # tapando al ganador, así que el llamador decide con eso a la vista en vez
            # de heredar un verde que nadie verificó (lección #250).
            logger.warning(
                "[FASE-6-asistida] cobertura parcial del overlay (mods=%s, mods_dir=%s): "
                "no se puede afirmar dónde está el artefacto",
                "ilegible" if mods_habilitados is None else len(mods_habilitados),
                mods_dir,
            )
        return encontrado

    def _plugins_txt_path(self, profile: str) -> pathlib.Path | None:
        """``<instancia MO2>/profiles/<perfil>/plugins.txt``, o ``None`` si no resuelve.

        El perfil COMPONE una ruta, así que la validación ocurre ANTES de unirla
        (:func:`_perfil_es_usable`): un separador o ``..`` la sacaría del árbol de la
        instancia y la verificación leería el ``plugins.txt`` de otro perfil. Acá solo
        queda la resolución; ``None`` significa "la instancia no resuelve", que es una
        causa DISTINTA de "el nombre del perfil es inválido" y por eso no se confunden
        en el ``reason`` que ve el operador.
        """
        if self._path_resolver is None:
            return None
        mo2 = self._path_resolver.get_mo2_instance_data_root()
        if not isinstance(mo2, pathlib.Path):
            return None
        return mo2 / "profiles" / profile / "plugins.txt"

    # ------------------------------------------------------------------
    # Etapa 6 ASISTIDA (ADR 0016) — preparar y verificar, sin spawnear
    # ------------------------------------------------------------------

    async def ejecutar_fase_asistida(
        self,
        *,
        profile: str,
        fase: str = FASE_PREPARAR,
        validate_limit: bool = True,
        testigo_previo: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """Punto ÚNICO de enrutado de la etapa 6 asistida: elige fase y delega.

        Existe porque la etapa tiene **tres superficies de producción** —el dispatcher de
        la GUI (`build_wrye_bash_pipeline`), el `SupervisorAgent` y la tool del agente LLM
        (`system_tools.generate_bashed_patch`)— y el defecto dominante de este repo es
        justamente cablear una y dejar las gemelas. Si cada superficie eligiera entre
        `preparar` y `verificar` por su cuenta, la tercera vez que alguien tocara una
        regla las otras dos quedarían atrás. Acá la decisión es una sola y las tres
        llaman al mismo método (anclado en `tests/test_wrye_bash_cableado_asistido.py`).

        ``fase`` falla cerrado: un valor fuera de :data:`FASES_ASISTIDAS` devuelve un
        dict de error en vez de caer en un default silencioso. La validación ocurre
        ANTES de tocar el perfil o de correr el preflight, así que una llamada mal
        formada no cuesta I/O ni bloquea al operador con un semáforo que no aplica.
        """
        # `isinstance` primero: `[] in frozenset` lanza TypeError (no hashable) en vez de caer acá.
        if not isinstance(fase, str) or fase not in FASES_ASISTIDAS:
            return {
                "success": False,
                "reason": "FaseInvalida",
                "message": (f"Fase asistida desconocida: {fase!r}. Las fases válidas son {sorted(FASES_ASISTIDAS)}."),
            }
        if fase == FASE_VERIFICAR:
            return await self.verificar_etapa_asistida(profile=profile, testigo_previo=testigo_previo)
        return await self.preparar_etapa_asistida(profile=profile, validate_limit=validate_limit)

    async def preparar_etapa_asistida(self, *, profile: str, validate_limit: bool = True) -> dict[str, Any]:
        """Fase 1 de la etapa 6: instrucciones para el operador + testigo previo.

        **No muta el artefacto, el perfil ni el load order**: no toma locks, no snapshotea
        y no abre TX. Ese es el punto de ADR 0016 — la caja negra se emite cuando algo
        puede mutar, y acá lo único que se hace es leer y decirle al operador qué hacer.
        Una invocación que no puede construir el parche no debe dejar una TX ``rolled_back``
        por ritual.

        **La única escritura es el write-probe de permisos**, y no es del parche: el sensor
        crea y borra un ``.skyclaw_probe_<uuid>.tmp`` en el destino (``overwrite`` en este
        dominio) para probar que se puede escribir ahí. Si el borrado falla, el reporte
        viaja con ``probe_residue`` en amarillo — no bloquea, pero el operador se entera.

        El preflight SÍ corre y **bloquea** en rojo: mandar al operador a un paso manual
        que ya sabemos que va a fallar (``Data`` sin permisos, master faltante, límite de
        plugins excedido) es gastar su tiempo y dejar un parche a medias.

        Siempre devuelve un ``dict`` serializable (contrato T11).
        """
        # Se inicializa ANTES del ``try``: el camino de excepción adjunta el reporte, y
        # una excepción anterior a ``preflight.run()`` tiene que poder pasar ``None``.
        preflight_report: PreflightReport | None = None
        try:
            if not _perfil_es_usable(profile):
                return {
                    "success": False,
                    "reason": "PerfilInvalido",
                    "message": (
                        f"Nombre de perfil no usable para ubicar plugins.txt: {profile!r}. "
                        "No puede estar vacío ni contener separadores de ruta."
                    ),
                }

            # Dominio USVFS: el ejecutor de esta etapa es el OPERADOR dentro de MO2, no
            # el runner spawneado. Medir el Data físico acá bloqueaba perfiles sanos
            # (ver `_ensure_preflight`).
            preflight = await asyncio.to_thread(
                self._ensure_preflight,
                dominio=DOMINIO_USVFS,
                profile=profile,
            )
            if preflight is not None:
                preflight_report = await preflight.run()
                if preflight_report.blocks_mutations:
                    red = "; ".join(c.summary for c in preflight_report.checks if c.status.value == "red")
                    logger.warning(
                        "Etapa 6 asistida no se ofrece: preflight en rojo (%s)",
                        red,
                        extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None},
                    )
                    return _attach_preflight(
                        {
                            "success": False,
                            "reason": "PreflightBlocked",
                            "error": red,
                            "message": f"Preflight en rojo: no se prepara la etapa 6 asistida. {red}",
                        },
                        preflight_report,
                    )

            if validate_limit and self._plugin_limit_guard is not None:
                guard_result = await self._plugin_limit_guard(profile)
                if not guard_result.get("valid", True):
                    detalle = guard_result.get("error") or ""
                    logger.error(
                        "[FASE-6-asistida] Abortando preparación: M-04 falló. %s",
                        detalle,
                        extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None},
                    )
                    return _attach_preflight(
                        {
                            "success": False,
                            "aborted_by": "plugin_limit_guard",
                            "plugin_count": guard_result.get("plugin_count"),
                            "error": detalle,
                            "message": detalle,
                        },
                        preflight_report,
                    )

            plugins_txt = self._plugins_txt_path(profile)
            # El artefacto de la etapa asistida vive en la superposición del perfil
            # (overwrite / mod de salida), no necesariamente en el Data físico: el
            # ejecutor es el operador dentro de MO2, bajo la USVFS.
            artefacto = await asyncio.to_thread(self._artefacto_asistido, profile)
            if plugins_txt is None or artefacto is None:
                return _attach_preflight(
                    {
                        "success": False,
                        "reason": "SinRutasResolubles",
                        "message": (
                            "No se pudo resolver la ruta del artefacto en el Data del juego ni en el "
                            "overlay del perfil: sin esa ruta no hay preparación ni verificación posible."
                        ),
                    },
                    preflight_report,
                )
            if not artefacto.cobertura_completa:
                return _attach_preflight(
                    {
                        "success": False,
                        "reason": "CoberturaDeOverlayIncompleta",
                        "message": (
                            "No se pudo inspeccionar alguna capa del perfil (overwrite o mods): "
                            "no se puede afirmar dónde está el artefacto."
                        ),
                    },
                    preflight_report,
                )

            preparacion = await asyncio.to_thread(
                preparar_etapa_asistida, artefacto=artefacto.ruta, plugins_txt=plugins_txt
            )
            return _attach_preflight(preparacion_como_resultado(preparacion), preflight_report)
        except Exception as exc:  # noqa: BLE001 — T11: SIEMPRE devolver dict serializable
            logger.error(
                "[FASE-6-asistida] Error inesperado preparando la etapa: %s",
                exc,
                exc_info=True,
                extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None},
            )
            # Este camino también adjunta el preflight. Era el ÚNICO retorno de la fase 1
            # sin ``_attach_preflight``: ante una excepción inesperada el panel perdía el
            # semáforo justo cuando algo se había roto.
            return _attach_preflight(
                {"success": False, "reason": "ErrorInesperado", "error": str(exc), "message": str(exc)},
                preflight_report,
            )

    async def verificar_etapa_asistida(
        self,
        *,
        profile: str,
        testigo_previo: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        """Fase 2: veredicto por evidencia en disco, sin spawnear y sin timeout.

        La lectura del artefacto (huella completa por chunks) va a un hilo: un Bashed
        Patch de decenas de MB no puede bloquear el event loop del daemon.

        ``testigo_previo`` es el campo homónimo que devolvió :meth:`preparar_etapa_asistida`.
        Sin él el veredicto puede ser verde pero declara ``frescura_probada=False``: no se
        inventa un cambio por ``mtime``. Con él, un archivo idéntico es ``SIN_CAMBIO`` y
        uno que no existía en la fase 1 prueba frescura por su sola aparición.

        Un diccionario malformado se descarta (``None``) en vez de adivinar: peor que no
        tener testigo es tener uno inventado.
        """
        try:
            if not _perfil_es_usable(profile):
                return {
                    "success": False,
                    "reason": "PerfilInvalido",
                    "message": (
                        f"Nombre de perfil no usable para ubicar plugins.txt: {profile!r}. "
                        "No puede estar vacío ni contener separadores de ruta."
                    ),
                }

            plugins_txt = self._plugins_txt_path(profile)
            # Mismo resolver que la fase 1: si las dos fases buscaran en lugares
            # distintos, una ausencia previa y una aparición serían incomparables.
            artefacto = await asyncio.to_thread(self._artefacto_asistido, profile)
            if plugins_txt is None or artefacto is None:
                return {
                    "success": False,
                    "reason": "SinRutasResolubles",
                    "message": (
                        "No se pudo resolver la ruta del artefacto en el Data del juego ni en el "
                        "overlay del perfil: sin esa ruta no hay verificación posible."
                    ),
                }
            if not artefacto.cobertura_completa:
                return {
                    "success": False,
                    "reason": "CoberturaDeOverlayIncompleta",
                    "message": (
                        "No se pudo inspeccionar alguna capa del perfil (overwrite o mods): "
                        "no se puede afirmar dónde está el artefacto."
                    ),
                }

            veredicto = await asyncio.to_thread(
                veredicto_etapa_asistida,
                artefacto=artefacto.ruta,
                plugins_txt=plugins_txt,
                testigo=_testigo_desde_resultado(testigo_previo),
            )
            return veredicto_como_resultado(veredicto)
        except Exception as exc:  # noqa: BLE001 — T11: SIEMPRE devolver dict serializable
            logger.error(
                "[FASE-6-asistida] Error inesperado verificando la etapa: %s",
                exc,
                exc_info=True,
                extra={"pipeline_stage": _ETAPA_WRYE_BASH, "tx_id": None},
            )
            return {"success": False, "reason": "ErrorInesperado", "error": str(exc), "message": str(exc)}

    # ------------------------------------------------------------------
    # Caja negra de vuelo (T-26/T-28, ADR 0002) — espejo de loot_service
    # ------------------------------------------------------------------

"""BodySlide Runner for Sky-Claw.

Implements M-03 BodySlide Runner specifications.

PR 1 (contrato headless):
- argv construido por el contrato puro (``bodyslide_headless.construir_argv``) —
  sin flags prohibidos, ``-q`` sólo con capacidad verificada, ``-p`` sólo con
  preset EXPLICITAMENTE verificado.
- Config.xml es precondición fail-closed (aborta antes del spawn) — ver
  ``bodyslide_config``.
- Orden transaccional (P0-1, auditoría): las GATES puros (capacidad, argv) no
  mutan nada; el snapshot byte a byte de los archivos del proceso se toma
  ANTES de cualquier mutación de Config.xml; el spawn; la restauración byte a
  byte del ORIGINAL está garantizada en todo caso (éxito, falla de siembra,
  falla del spawn). Un snapshot obtenido DESPUÉS de la siembra restauraría
  la versión sembrada — dejando la config del usuario jamás como era.
- El veredicto (SUCCESS/FAILURE/EVIDENCE_INCOMPLETE) sale del contrato puro —
  ``rc == 0 and artefactos > 0 and not fallo_parcial`` NO es más el criterio.

Limites honestos (ADR 0019 / PR 2):
- Snapshot/restore es best-effort en el borde de Python: un crash duro o un
  kill del host entre spawn y restore deja las mutaciones del proceso en su
  lugar. Protección con journal durable = PR 2.
- El manifiesto de artefactos no es verificable en el camino del agente (no hay
  catálogo hasta PR 3): sin ``manifest=`` el veredicto es EVIDENCE_INCOMPLETE,
  nunca SUCCESS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import time
from dataclasses import dataclass

from sky_claw.app.security.links import exigir_contencion_fisica, iter_archivos_propios
from sky_claw.local.tools._dir_rollback import mutar_hasta_terminal
from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.bodyslide_config import (
    BodySlideConfigPreconditionError,
    calcular_config_sembrada,
)
from sky_claw.local.tools.bodyslide_headless import (
    EvidenciaDeBuild,
    HeadlessContractError,
    ManifiestoArtefactos,
    construir_argv,
    evaluar_veredicto,
    exigir_destino_canonico,
    exigir_identidad_de_grupo_unica,
    marcadores_de_corrida,
)
from sky_claw.local.tools.bodyslide_journal import (
    ARCHIVOS_JOURNALEADOS,
    AlmacenDeJournal,
    EstadoDelJournal,
    InformeDeRecuperacion,
    JournalError,
    JournalOcupadoError,
    ModificacionConcurrenteError,
    SiembraColgadaError,
    SiembraConPlazo,
    TransaccionDeConfig,
    Veredicto,
    cuarentena_activa,
    identidad_actual,
    iniciar_transaccion,
)
from sky_claw.local.tools.bodyslide_lanzador import (
    LanzadorDeBodySlide,
    PedidoDeLanzamiento,
    SalidaDelLanzamiento,
    TerminalidadDesconocidaError,
)
from sky_claw.local.tools.output_targets import bodyslide_output_root, bodyslide_output_target

logger = logging.getLogger(__name__)

#: Extensiones que cuentan como salida NUEVA de un batch build (minúsculas).
#: ``.nif`` son las mallas y ``.tri`` los morfos.
_EXTENSIONES_DE_ARTEFACTO = (".nif", ".tri")

#: Nombre del log que ``Log::Initialize`` (``src/utils/Log.cpp``) crea al lado
#: del ejecutable. Es el ÚNICO diagnóstico que BodySlide emite: el binario se
#: linkea ``/SUBSYSTEM:Windows`` (``BodySlide.vcxproj``), así que no escribe a
#: stdout ni a stderr nunca.
_NOMBRE_DEL_LOG = "Log_BS.txt"

#: Cuánto del final del log se adjunta al mensaje de fallo. El log acumula hasta
#: cuatro corridas antes de rotar, así que leerlo entero traería ruido viejo.
_COLA_DEL_LOG_BYTES = 4096

#: Firma que ``Log::Initialize`` escribe al arrancar cada corrida
#: (``src/utils/Log.cpp``: ``signature = "[#Log"`` + fecha). Es el único
#: delimitador que permite atribuir una línea del log a ESTA corrida.
_FIRMA_DE_CORRIDA = "[#Log"

#: Tope de lo que se recorre hacia atrás buscando la firma. El log conserva hasta
#: cuatro corridas y un batch grande puede loguear megabytes, así que la búsqueda
#: necesita un techo; superarlo devuelve "no atribuible" (conservador) en vez de
#: cargar el archivo entero en memoria.
_MAX_BYTES_DE_BUSQUEDA = 8 * 1024 * 1024

#: Reintentos acotados del re-read del log (P1-8). El tamaño del log puede
#: colarse entre la terminal del proceso y la primera lectura (streams en
#: vuelo); un reintento acotado y de tiempo fijo acota el trabajo, nunca espera
#: indefinidamente. 3 intentos × 0.05 s — TOPE acotado por construcción.
_REINTENTOS_DEL_LOG = 3
_RETRASO_ENTRE_REINTENTOS_S = 0.05

#: Archivos de configuración que el proceso toca. Config.xml es la config del
#: usuario (donde BodySlide controla GameDataPath/OutputDataPath/WarnMissingGamePath);
#: BodySlide.xml es la config de la app (donde ``CommandLineBuild`` persiste
#: ``SelectedPreset``); BuildSelection.xml es la elección/zaps/build records del
#: usuario.
_ARCHIVOS_DE_CONFIGURACION = ARCHIVOS_JOURNALEADOS


class BodySlideExecutionError(Exception):
    pass


class BodySlideTimeoutError(BodySlideExecutionError):
    """Elevada cuando BodySlide excede su timeout (U-10).

    Dedicada (en vez de un ``BodySlideResult`` con ``success=False``) para que el
    timeout se PROPAGUE por el context manager del lock/rollback en lugar de
    salir limpio: prerequisito del rollback transaccional de salida (U-04).
    Deriva de :class:`BodySlideExecutionError`.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"BodySlide excedió el timeout de {timeout_seconds:g}s durante la generación batch")


@dataclass(frozen=True, slots=True)
class BodySlideConfig:
    bodyslide_exe: pathlib.Path
    game_path: pathlib.Path
    timeout_seconds: float = 600.0
    #: Capacidad VERIFICADA de ``-q`` (quieto, sin diálogos). Upstream v6.2.0
    #: declara ``q``/quiet; la verificación la hace el rig (PR 2) — el runner no
    #: adivina. False (default) ⇒ se devuelve CAPABILITY_UNKNOWN y NO se spawnea.
    quiet_verified: bool = False
    #: Juego objetivo EXPECTADO por el plan (P1-5, auditoría). ``None`` ⇒ no se
    #: valida contra ningún valor (comportamiento viejo). Cuando está seteado,
    #: el ``TargetGame`` del Config.xml debe coincidir EXACTAMENTE — un enum
    #: válido del juego equivocado NO es precondición válida.
    expected_target_game: int | None = None
    #: Journal durable de la config (PR 2b/2c-1). ``None`` ⇒ el almacén por defecto bajo
    #: ``runtime_state_dir()``; los tests inyectan uno propio.
    almacen_de_journal: AlmacenDeJournal | None = None
    #: Plazo de RESPUESTA de la siembra (C3, ``bodyslide_config_io_timeout_seconds`` del brief).
    #: Al vencer: cuarentena persistente y ``CONFIG_IO_HUNG``; el hilo no se suelta.
    plazo_de_siembra_segundos: float = 30.0
    #: Spawn inyectable (PR 2c-2b). ``None`` ⇒ :class:`LanzadorStandalone` (``run_capture`` con
    #: Job, el comportamiento de 2c-1). El backend brokered inyecta el suyo; la tabla de
    #: terminalidad es la misma para los dos (``bodyslide_lanzador``).
    lanzador: LanzadorDeBodySlide | None = None


class LanzadorStandalone:
    """Spawn directo con ``run_capture(require_job=True)``.

    En Windows el árbol queda en un Job ``KILL_ON_JOB_CLOSE`` que ``run_capture`` cierra en
    TODA salida: retorno, timeout o error demuestran terminalidad (mata, cosecha y cierra el
    Job). Por eso este lanzador nunca lanza :class:`TerminalidadDesconocidaError`.
    """

    async def lanzar(self, pedido: PedidoDeLanzamiento) -> SalidaDelLanzamiento:
        stdout, stderr, return_code = await run_capture(
            list(pedido.argv),
            timeout=pedido.timeout,
            cwd=str(pedido.cwd),
            require_job=True,
        )
        return SalidaDelLanzamiento(stdout=stdout, stderr=stderr, return_code=return_code)


@dataclass
class BodySlideResult:
    success: bool
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    #: Detalle canónico del fallo, vacío en éxito (contrato ``success``/``message``
    #: de AGENTS.md).
    message: str = ""
    #: Cantidad de ``.nif``/``.tri`` NUEVOS (creados por esta corrida, no
    #: preexistentes) encontrados bajo el destino.
    artifacts_built: int = 0
    #: Archivos de configuración que el proceso modificó y que restauramos byte a byte.
    config_restored: tuple[str, ...] = ()
    #: Estado del contrato headless del run (útil para diagnóstico del agente):
    #: SUCCESS | FAILURE | EVIDENCE_INCOMPLETE | CAPABILITY_UNKNOWN |
    #: CONFIG_PRECONDITION_FAILED | ARGS_INVALID | STAGING_UNDETERMINED |
    #: STAGING_UNVERIFIABLE.
    estado: str = "SUCCESS"


def _contar_archivos_nuevos(destino: pathlib.Path, previos: set[str]) -> tuple[int, int]:
    """Cuenta NUEVOS ``.nif``/``.tri`` bajo *destino*, recursivamente y sin enlaces.

    Filtrar por el conjunto previo (paths relativos) es lo que impide que un
    artefacto PREEXISTENTE cuente como salida nueva. ``iter_archivos_propios``
    y no ``Path.rglob`` (review Copilot #433): rglob decide si desciende con
    ``is_dir(follow_symlinks=False)``, que frena en un symlink pero NO en un
    junction de Windows —``IO_REPARSE_TAG_MOUNT_POINT`` no es un symlink para
    ``os.path.islink``—.
    """
    nuevos_nif = 0
    nuevos_tri = 0
    for ruta, _identidad in iter_archivos_propios(destino):
        try:
            relativo = ruta.relative_to(destino)
        except ValueError:
            continue
        if relativo.as_posix() in previos:
            continue
        sufijo = ruta.suffix.lower()
        if sufijo == ".nif":
            nuevos_nif += 1
        elif sufijo == ".tri":
            nuevos_tri += 1
    return nuevos_nif, nuevos_tri


def _conjunto_de_archivos_previos(destino: pathlib.Path) -> set[str]:
    """Paths relativos de los archivos existentes bajo *destino* antes del spawn."""
    previos: set[str] = set()
    for ruta, _identidad in iter_archivos_propios(destino):
        try:
            previos.add(ruta.relative_to(destino).as_posix())
        except ValueError:
            continue
    return previos


@dataclass(frozen=True, slots=True)
class _ColaDeLog:
    """Cola del log afín a esta corrida (atribuible o no)."""

    texto: str
    atribuible: bool
    fallo_parcial: bool
    conflicto_sin_ganador: bool
    conflicto_verificable: bool


def _leer_log(exe: pathlib.Path) -> _ColaDeLog:
    """Devuelve la cola atribuible al último ``[#Log`` de la corrida.

    ``Log::Initialize`` (``src/utils/Log.cpp``) escribe una firma ``[#Log <fecha>]``
    al arrancar cada corrida y **sólo rota el archivo tras la cuarta**, así que el
    log contiene hasta cuatro corridas superpuestas. Acotar por la ÚLTIMA firma es
    lo que separa "esta corrida falló" de "alguna de las tres anteriores falló".

    Best-effort ante I/O: si falta o no se puede atribuir, ``atribuible=False`` y
    JAMÁS puede contar como éxito.
    """
    log = exe.parent / _NOMBRE_DEL_LOG
    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            fin = handle.tell()
            crudo = b""
            while True:
                leido = len(crudo)
                if leido >= fin or leido >= _MAX_BYTES_DE_BUSQUEDA:
                    break
                paso = min(_COLA_DEL_LOG_BYTES, fin - leido, _MAX_BYTES_DE_BUSQUEDA - leido)
                handle.seek(fin - leido - paso)
                trozo = handle.read(paso)
                if not trozo:
                    # Sin progreso ⇒ se cortamos. El tamaño se capturó una vez al
                    # abrir; si BodySlide rota el log mientras leemos, los reads
                    # devuelven vacío y ``crudo`` deja de crecer.
                    break
                crudo = trozo + crudo
                if _FIRMA_DE_CORRIDA.encode() in crudo:
                    break
    except OSError:
        return _ColaDeLog("", False, False, False, False)

    texto = crudo.decode("utf-8", errors="replace")
    corte = texto.rfind(_FIRMA_DE_CORRIDA)
    if corte == -1:
        # No se pudo atribuir: se muestra la cola como diagnóstico pero no decide.
        return _ColaDeLog(texto[-_COLA_DEL_LOG_BYTES:].strip(), False, False, False, False)

    corrida = texto[corte:]
    fallo_parcial, conflicto_sin_ganador, conflicto_verificable = marcadores_de_corrida(corrida)
    return _ColaDeLog(
        corrida[-_COLA_DEL_LOG_BYTES:].strip(),
        True,
        fallo_parcial,
        conflicto_sin_ganador,
        conflicto_verificable,
    )


@dataclass(frozen=True, slots=True)
class _SnapshotDeLog:
    """Snapshot del log ANTES del spawn — la evidencia de P0-3/P1-D.

    ``crudo`` (bytes originales) se conserva para distinguir "el archivo no
    cambió" (byte-idéntico → la corrida no escribió → atribuible=False) de
    "el archivo creció/se reescribió" (un run NUEVO pudo escribirlo). Los
    conteos y la firma última son la evidencia que distingue la firma del run
    actual de la de un run anterior en un log con varias corridas (append) o con
    rotación (>= 4 firmas, Log::Initialize copia desde la 2ª firma y agrega la
    nueva).
    """

    tamaño: int
    mtime_ns: int
    ultima_firma: str
    total_firmas: int
    offset_ultima_firma: int  # offset del último ``[#Log`` (0 si no hay firma)
    crudo: bytes


def _snapshot_log_inicial(exe: pathlib.Path) -> _SnapshotDeLog | None:
    """Snapshot del log ANTES del spawn: objeto co-relacionado o ``None``.

    Es la evidencia de que la corrida no escribió log: el archivo byte-idéntico
    al snapshot (contenido) es prueba de que la corrida actual NO tocó el log —
    lo que la convierte en log ANTIGUO (P0-3, auditoría).

    El offset de la última firma (no sólo el texto) es lo que permite distinguir
    una firma NUEVA appenda (offset distinto/archivo crecido) de una firma vieja
    que se reestructura (contenido cambiado pero las firmas conservan offsets
    y conteo, P1-D). Sin offset no se puede separar "nuevo run" de "archivo
    editado in-place".
    """
    log = exe.parent / _NOMBRE_DEL_LOG
    # P1-B (revisión #1003): AUSENTE e ILEGIBLE no son lo mismo. Sólo un
    # archivo que no existe devuelve ``None`` ("no había log"). Un log que existe
    # pero no se pudo leer (AV, lock de otro proceso, permisos) NO prueba nada
    # sobre su contenido previo: si se tratara como ausente, el log VIEJO —
    # legible de nuevo tras la corrida— se atribuiría a la corrida nueva.
    # Fail-closed: se lanza y el runner aborta antes de mutar y de spawnear.
    try:
        st = log.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BodySlideExecutionError(
            f"No se puede inspeccionar '{log}' antes del spawn — la atribución del log sería imposible: {exc}"
        ) from exc
    try:
        crudo = log.read_bytes()
    except OSError as exc:
        raise BodySlideExecutionError(
            f"No se puede leer '{log}' antes del spawn — la atribución del log sería imposible: {exc}"
        ) from exc
    texto = crudo.decode("utf-8", errors="replace")
    corte = texto.rfind(_FIRMA_DE_CORRIDA)
    ultima_firma = _leer_firma(texto, corte) if corte != -1 else ""
    total = texto.count(_FIRMA_DE_CORRIDA)
    return _SnapshotDeLog(st.st_size, st.st_mtime_ns, ultima_firma, total, corte, crudo)


def _leer_firma(texto: str, corte: int) -> str:
    """Recorte de línea de firma ``[#Log ...]`` a partir de su offset ``corte``."""
    fin = texto.find("\n", corte)
    if fin == -1:
        fin = len(texto)
    return texto[corte:fin].strip()


def _atribuir_log_a_corrida(
    exe: pathlib.Path,
    pre_snapshot: _SnapshotDeLog | None,
    *,
    reintentos: int = _REINTENTOS_DEL_LOG,
    retraso: float = _RETRASO_ENTRE_REINTENTOS_S,
) -> _ColaDeLog:
    """Atribuye el log al run actual — con reparación acotada (P1-8).

    Regla (P0-3 + P1-D + P1-8):
    1. Archivo ausente → ``atribuible=False`` (el run no escribió log).
    2. Archivo byte-idéntico al snapshot (contenido) → la corrida no escribió
       log → ``atribuible=False``, SIN reintentos (no hay nada que esperar: el
       archivo no cambió).
    3. Archivo cambiado pero SIN firma NUEVA (ni append, ni rotación, ni
       contenido nuevo) → ``atribuible=False`` (P0-3 adversarial: la última
       firma está en el mismo offset, con el mismo conteo — el cambio fue en
       otro lugar o no es del run actual).
    4. Archivo cambiado con firma NUEVA — append (la última firma está en un
       offset nuevo / el archivo creció) o rotación (la firma última cambió de
       offset aunque el texto sea idéntico) → ``_leer_log`` con reintentos
       acotados (tope ``reintentos``).

    El reintento acotado es honesto: N intentos con retraso constante; nunca
    espera indefinidamente. Es la reparación que el upstream puede necesitar
    (streams wxLogStream en vuelo al terminar el proceso) sin dejar al caller
    girar.
    """
    try:
        actual = _snapshot_log_inicial(exe)
    except BodySlideExecutionError:
        # Ilegible DESPUÉS de la corrida: no hay evidencia, no se atribuye.
        return _ColaDeLog("", False, False, False, False)

    if actual is None:
        # Archivo ausente — el run no escribió log (ni nunca existió).
        return _ColaDeLog("", False, False, False, False)

    if pre_snapshot is not None:
        # (2) byte-idéntico → no hay nada que esperar.
        if actual.crudo == pre_snapshot.crudo:
            return _ColaDeLog("", False, False, False, False)

        # (3)/(4) firma nueva — append (offset nuevo) o rotación (offset distinto).
        firma_nueva = False
        if actual.total_firmas > pre_snapshot.total_firmas:
            # append: nuevas firmas después de la del snapshot.
            firma_nueva = True
        elif actual.offset_ultima_firma != pre_snapshot.offset_ultima_firma:
            # rotación/reescritura: la última firma vive en otro lugar
            # (el contenido de antes fue re-estructuró).
            firma_nueva = True
        elif actual.ultima_firma != pre_snapshot.ultima_firma:
            # la firma cambió de texto (misma posición, nombre nuevo) — nuevo run.
            firma_nueva = True

        if not firma_nueva:
            # P0-3 adversarial: cambio sin firma nueva (misma posición,
            # mismo conteo, misma firma) ⇒ el cambio no es del run actual.
            return _ColaDeLog("", False, False, False, False)

    cola = _leer_log(exe)
    for _ in range(1, reintentos):
        if cola.atribuible:
            break
        time.sleep(retraso)
        cola = _leer_log(exe)

    return cola


def _verificar_identidad_del_staging(raiz: pathlib.Path, candidato: pathlib.Path) -> None:
    """Exige que el staging y el destino se alcancen FÍSICAMENTE sin enlaces.

    La comparación léxica de ``construir_argv`` no ve lo que la filesystem
    materializa: la raíz administrada, sus ancestros existentes y el propio
    destino pueden ser symlinks/junctions que desvían la mutación fuera del
    staging — ``exigir_contencion_fisica`` recorre la cadena con ``lstat``
    sin seguir enlaces, revalida la identidad de cada componente y rechaza
    cualquier redirección. Si la raíz todavía no está materializada (primer run),
    no hay nada que verificar: queda el gate léxico. La revalidación física
    POSTERIOR al spawn no existe en PR 1 (diferida a PR 2, junto con la ventana
    TOCTOU entre este chequeo y el spawn).

    Raises:
        OSError: si la identidad no puede verificarse (enlace en la cadena,
            ancestro no inspeccionable, identidad cambiada, rechazo de premisas).
    """
    try:
        raiz.lstat()
    except FileNotFoundError:
        # El staging no está materializado todavía — no hay cadena física que
        # demostrar. El gate léxico lo cubrió; el post-spawn lo revalida.
        return
    exigir_contencion_fisica(raiz, candidato, exigir_existencia=False)


def verificar_identidad_de_grupo_en_staging(raiz: pathlib.Path, group: str) -> None:
    """Lista la raíz del staging y exige que *group* no colisione por mayúsculas (cierre #1003).

    Única implementación para los DOS caminos que llegan al staging: el runner
    (público) y ``run_bodyslide_batch``, que la llama ANTES de
    ``DirectoryRollback`` — después del move-aside la grafía original de la
    carpeta ya no existe y la colisión sería invisible. Raíz ausente ⇒ no hay
    carpetas con las que colisionar.

    Raises:
        HeadlessContractError: grupo equivalente por mayúsculas a una carpeta existente.
        OSError: la raíz existe pero no se pudo listar (el caller falla cerrado).
    """
    try:
        nombres = [entrada.name for entrada in os.scandir(raiz)]
    except FileNotFoundError:
        return
    exigir_identidad_de_grupo_unica(group, nombres)


class BodySlideRunner:
    """Asynchronous runner for BodySlide batch generation."""

    def __init__(self, config: BodySlideConfig):
        self.config = config

    async def run_batch(
        self,
        group: str,
        output_path: str,
        *,
        preset: str | None = None,
        preset_verified: bool = False,
        build_morphs: bool = True,
        manifest: ManifiestoArtefactos | None = None,
    ) -> BodySlideResult:
        """Execute BodySlide in batch mode for a specific group.

        Format (CLI): ``BodySlide.exe -gbuild <Group> -t <Output> [-p <Preset>] [-tri] [-q]``.

        Orden transaccional (P0-1):
        1. Gates de staging y capacidad — staging determinado, argv (confinamiento
           léxico), identidad física del staging — fail-closed, sin mutación.
        2. Snapshot byte a byte del ORIGINAL — antes de cualquier mutación.
        3. Siembra — mutación de Config.xml.
        4. Spawn — evidencia + veredicto.
        5. Restore byte a byte del ORIGINAL — garantizado en todo caso
           (finally): exito, falla de seed, falla del spawn.
        """
        start_time = time.monotonic()

        def resultado_de_fallo(estado: str, message: str) -> BodySlideResult:
            return BodySlideResult(
                success=False,
                return_code=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - start_time,
                message=message,
                artifacts_built=0,
                config_restored=(),
                estado=estado,
            )

        # 1a) Gate de capacidad — fail-closed, sin mutación.
        if not self.config.quiet_verified:
            logger.error("BodySlide aborted: quiet capability unverified (quiet_verified is not set)")
            return resultado_de_fallo(
                "CAPABILITY_UNKNOWN",
                "Quiet capability unverified — no se lanza el build autónomo. "
                "Verify the BodySlide binary against the upstream (v6.2.0+) or use the "
                "interactive GUI path.",
            )

        # 1b) El staging DEBE estar determinado — la raíz administrada que
        #     bodyslide_output_root deriva del juego. Sin juego no hay raíz:
        #     rechazar antes de cualquier mutación (fail-closed).
        raiz_de_salida = bodyslide_output_root(game=self.config.game_path)
        if raiz_de_salida is None:
            message = (
                "El staging no está determinado — game_path no configurado "
                "(bodyslide_output_root devolvió None). No se lanza el build."
            )
            logger.error("BodySlide aborted: %s", message)
            return resultado_de_fallo("STAGING_UNDETERMINED", message)

        # 1c) argv construido por el contrato — confinamiento léxico, sin flags
        #     no autorizados. El staging administra el destino: el target debe
        #     venir confinado a esta raíz.
        try:
            args = construir_argv(
                str(self.config.bodyslide_exe),
                group,
                output_path,
                preset=preset,
                preset_verified=preset_verified,
                build_morphs=build_morphs,
                quiet_verified=self.config.quiet_verified,
                stage_root=str(raiz_de_salida),
            )
        except HeadlessContractError as exc:
            message = str(exc)
            logger.error("BodySlide aborted: %s", message)
            return resultado_de_fallo("ARGS_INVALID", message)

        # 1c') P1-A (revisión #1003): el destino debe ser EXACTAMENTE el target
        #      canónico del grupo. El confinamiento de 1c es de prefijo: aceptaba
        #      la raíz compartida, la carpeta de OTRO grupo o un subdirectorio,
        #      y el move-aside por grupo sólo protege el target canónico.
        canonico = bodyslide_output_target(game=self.config.game_path, group=group)
        try:
            if canonico is None:
                raise HeadlessContractError(f"el grupo '{group}' no tiene destino canónico administrado")
            exigir_destino_canonico(output_path, str(canonico))
        except HeadlessContractError as exc:
            message = str(exc)
            logger.error("BodySlide aborted: %s", message)
            return resultado_de_fallo("ARGS_INVALID", message)

        # 1c'') Cierre #1003: el grupo no puede ser una identidad equivalente por
        #       mayúsculas a otra carpeta del staging (NTFS: misma carpeta física).
        try:
            await asyncio.to_thread(verificar_identidad_de_grupo_en_staging, raiz_de_salida, group)
        except HeadlessContractError as exc:
            message = str(exc)
            logger.error("BodySlide aborted: %s", message)
            return resultado_de_fallo("ARGS_INVALID", message)
        except OSError as exc:
            message = f"No se pudo listar el staging '{raiz_de_salida}': {exc}"
            logger.error("BodySlide aborted: %s", message)
            return resultado_de_fallo("STAGING_UNVERIFIABLE", message)

        # 1d) Identidad física del staging — la raíz y la cadena de ancestrales
        #     existentes deben ser directorios REALES (sin symlinks/junctions).
        #     Un destino a través de enlaces desvía la mutación fuera del
        #     staging aunque el argv haya pasado el gate léxico: rechaza el build.
        try:
            await asyncio.to_thread(_verificar_identidad_del_staging, raiz_de_salida, pathlib.Path(output_path))
        except OSError as exc:
            message = f"Staging con identidad no verificable: {exc}"
            logger.error("BodySlide aborted: %s", message)
            return resultado_de_fallo("STAGING_UNVERIFIABLE", message)

        # 3) Log y cuarentena — antes de mutar nada.
        #    Snapshot del log: evidencia la ausencia de escritura de este run (P0-3).
        exe_dir = self.config.bodyslide_exe.parent
        almacen = self.config.almacen_de_journal or AlmacenDeJournal()
        try:
            pre_snapshot_log = await asyncio.to_thread(_snapshot_log_inicial, self.config.bodyslide_exe)
        except BodySlideExecutionError as exc:
            message = str(exc)
            logger.error("BodySlide aborted before spawn: %s", message)
            return resultado_de_fallo("CONFIG_PRECONDITION_FAILED", message)
        if await asyncio.to_thread(cuarentena_activa, almacen):
            message = (
                "El recurso bodyslide-meshes está en cuarentena: una siembra anterior quedó en vuelo. "
                "No se lanza hasta que termine y se restaure, o el operador la libere con evidencia."
            )
            logger.error("BodySlide aborted: %s", message)
            return resultado_de_fallo("PRECONDITION_FAILED", message)

        # 3b) Journal (PR 2b/2c-1): la siembra se CALCULA sin escribir, y PREPARADO
        #     (originales + hash de la siembra) queda durable ANTES de la primera mutación.
        try:
            bytes_sembrados = await asyncio.to_thread(
                calcular_config_sembrada,
                exe_dir=exe_dir,
                game_path=self.config.game_path,
                output_root=raiz_de_salida,
                expected_target_game=self.config.expected_target_game,
            )
        except BodySlideConfigPreconditionError as exc:
            message = str(exc)
            logger.error("BodySlide aborted before spawn: %s", message)
            return resultado_de_fallo("CONFIG_PRECONDITION_FAILED", message)
        identidad = await asyncio.to_thread(identidad_actual, exe_dir)
        if identidad is None:
            message = (
                "No se pudo establecer la identidad de la máquina o de la instalación de BodySlide: "
                "sin ella el journal no sería recuperable, así que no se muta la config."
            )
            logger.error("BodySlide aborted before spawn: %s", message)
            return resultado_de_fallo("CONFIG_PRECONDITION_FAILED", message)
        abiertas: list[TransaccionDeConfig] = []

        def _abrir() -> TransaccionDeConfig:
            transaccion = iniciar_transaccion(
                almacen=almacen, identidad=identidad, exe_dir=exe_dir, bytes_sembrados=bytes_sembrados
            )
            abiertas.append(transaccion)
            return transaccion

        try:
            tx = await mutar_hasta_terminal(_abrir)
        except asyncio.CancelledError:
            # Review CodeRabbit #1021: el hilo pudo dejar PREPARADO durable antes de que
            # se propagara la cancelación. Nada se mutó todavía: se cierra sin cambios.
            if abiertas:
                with contextlib.suppress(JournalError, OSError):
                    await mutar_hasta_terminal(abiertas[0].abortar_sin_mutar)
            raise
        except JournalOcupadoError as exc:
            message = (
                f"Hay una corrida anterior de BodySlide sin recuperar ({exc}). El reconciliador la resuelve "
                "al arrancar; hasta entonces no se lanza otra."
            )
            logger.error("BodySlide aborted before spawn: %s", message)
            return resultado_de_fallo("PRECONDITION_FAILED", message)
        except (JournalError, OSError) as exc:
            # Incluye una config enlazada: el journal no escribe a través de enlaces.
            message = f"La configuración de BodySlide no admite una transacción segura: {exc}"
            logger.error("BodySlide aborted before spawn: %s", message)
            return resultado_de_fallo("CONFIG_PRECONDITION_FAILED", message)

        # 3c) Review CodeRabbit #1021: la siembra se calculó ANTES del snapshot del
        #     journal. Si Config.xml cambió en esa ventana, el journal registró el nuevo
        #     original con el hash de una siembra vieja. Se recalcula sobre lo que hay
        #     ahora (= el original registrado); si difiere, se cierra sin mutar.
        try:
            recalculada = await asyncio.to_thread(
                calcular_config_sembrada,
                exe_dir=exe_dir,
                game_path=self.config.game_path,
                output_root=raiz_de_salida,
                expected_target_game=self.config.expected_target_game,
            )
        except BodySlideConfigPreconditionError:
            recalculada = None
        if recalculada != bytes_sembrados:
            with contextlib.suppress(JournalError, OSError):
                await mutar_hasta_terminal(tx.abortar_sin_mutar)
            message = "Config.xml cambió entre el cálculo de la siembra y el snapshot del journal; no se siembra."
            logger.error("BodySlide aborted before spawn: %s", message)
            return resultado_de_fallo("CONCURRENT_MODIFICATION", message)

        # 4) PERÍMETRO TRANSACCIONAL — el journal decide qué se restaura y cuándo.
        #    - Siembra abandonada por plazo/cancelación: el hilo de la siembra es el
        #      último escritor y restaura él mismo (cuarentena hasta entonces).
        #    - LANZADO sin terminalidad demostrada (cancelación con el proceso en
        #      vuelo): NO se restaura; el journal queda en LANZADO (HOLD).
        #    - Cualquier otro final: TERMINAL (si se lanzó) y restauración verificada.
        resultado: BodySlideResult | None = None
        siembra = SiembraConPlazo(tx, plazo=self.config.plazo_de_siembra_segundos)
        lanzado = False
        terminal = False
        try:
            # 4a) Siembra con plazo de respuesta.
            try:
                await siembra.ejecutar()
            except SiembraColgadaError as exc:
                message = str(exc)
                logger.error("BodySlide aborted before spawn: %s", message)
                resultado = resultado_de_fallo("CONFIG_IO_HUNG", message)
                return resultado
            except ModificacionConcurrenteError as exc:
                message = f"Config.xml cambió por fuera de la corrida: {exc}"
                logger.error("BodySlide aborted before spawn: %s", message)
                resultado = resultado_de_fallo("CONCURRENT_MODIFICATION", message)
                return resultado
            except (JournalError, OSError) as exc:
                message = f"No se pudo sembrar Config.xml: {exc}"
                logger.error("BodySlide aborted before spawn: %s", message)
                resultado = resultado_de_fallo("CONFIG_PRECONDITION_FAILED", message)
                return resultado

            destino = pathlib.Path(output_path)
            previos = await asyncio.to_thread(_conjunto_de_archivos_previos, destino)
            destino_propio = not destino.is_symlink()
            preexistentes = len(previos)

            # 4b) LANZADO durable ANTES de pedir el spawn: sin esto un crash no
            #     distingue "nunca se lanzó" de "puede seguir vivo".
            try:
                await mutar_hasta_terminal(tx.marcar_lanzado)
            except asyncio.CancelledError:
                # Review Codex #1021: la cancelación llegó mientras se grababa LANZADO.
                # El spawn NUNCA se pidió, así que la terminalidad está demostrada: se
                # registra TERMINAL y se restaura en vez de dejar un LANZADO huérfano.
                lanzado = tx.estado is EstadoDelJournal.LANZADO
                terminal = True
                raise
            lanzado = True

            # 4c) Spawn por el lanzador (PR 2c-2b) — el lanzador reporta, el runner decide.
            #     Retorno, timeout o error ⇒ terminal. ``TerminalidadDesconocidaError`` ⇒ el
            #     proceso pudo seguir vivo: HOLD sin restaurar. Una cancelación tampoco prueba
            #     terminalidad (el reap puede quedar a medias). Tabla congelada en
            #     ``tests/test_bodyslide_lanzador.py``.
            lanzador = self.config.lanzador if self.config.lanzador is not None else LanzadorStandalone()
            pedido = PedidoDeLanzamiento(
                argv=tuple(args),
                exe=self.config.bodyslide_exe,
                cwd=self.config.game_path,
                timeout=self.config.timeout_seconds,
                group=group,
                target=output_path,
                preset=preset,
                preset_verified=preset_verified,
                build_morphs=build_morphs,
                quiet_verified=self.config.quiet_verified,
            )
            try:
                salida = await lanzador.lanzar(pedido)
            except asyncio.CancelledError:
                raise
            except TerminalidadDesconocidaError as exc:
                message = (
                    f"BodySlide se lanzó y no hay prueba de que haya terminado ({exc}). La config NO se "
                    "restaura: el journal queda en LANZADO hasta que el operador confirme la terminalidad "
                    "(--mode bodyslide-recovery --bodyslide-recovery-action confirm-terminal)."
                )
                logger.error("BodySlide: %s", message)
                resultado = resultado_de_fallo("HOLD_TERMINALITY_UNKNOWN", message)
                return resultado
            except TimeoutError as exc:
                terminal = True
                logger.error("BodySlide execution timed out.")
                raise BodySlideTimeoutError(self.config.timeout_seconds) from exc
            except Exception as e:
                terminal = True
                logger.error(f"BodySlide execution failed: {e}")
                raise BodySlideExecutionError(f"Failed to execute BodySlide: {e}") from e
            terminal = True
            stdout, stderr, return_code = salida.stdout, salida.stderr, salida.return_code

            nuevos_nif, nuevos_tri = await asyncio.to_thread(_contar_archivos_nuevos, destino, previos)
            log = await asyncio.to_thread(_atribuir_log_a_corrida, self.config.bodyslide_exe, pre_snapshot_log)

            evidencia = EvidenciaDeBuild(
                return_code=return_code,
                proceso_terminado=True,
                log_atribuible=log.atribuible,
                log_contiene_fallo_parcial=log.fallo_parcial,
                log_contiene_conflicto_sin_ganador=log.conflicto_sin_ganador,
                log_contiene_conflicto_verificable=log.conflicto_verificable,
                destino_propio=destino_propio,
                artefactos_nif=nuevos_nif,
                artefactos_tri=nuevos_tri,
                preexistentes=preexistentes,
                # ``-p`` sólo sale con un preset verificado (construir_argv rechaza
                # el resto): sin preset, BodySlide usa el SelectedPreset guardado.
                preset_efectivo_verificado=preset is not None and preset_verified,
            )
            veredicto = evaluar_veredicto(evidencia, manifest)

            artefactos = nuevos_nif + nuevos_tri
            exitoso = veredicto.estado == "SUCCESS"

            message = ""
            if not exitoso:
                message = self._diagnostico(
                    return_code=return_code,
                    nuevos_nif=nuevos_nif,
                    nuevos_tri=nuevos_tri,
                    destino=destino,
                    log=log,
                    veredicto=veredicto.estado,
                    razon=veredicto.razon,
                )
                logger.error("[M-03] BodySlide no produjo un build válido: %s", message)

            resultado = BodySlideResult(
                success=exitoso,
                return_code=return_code,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                duration_seconds=time.monotonic() - start_time,
                message=message,
                artifacts_built=artefactos,
                config_restored=(),
                estado=veredicto.estado,
            )
            return resultado
        finally:
            await self._cerrar_transaccion(tx, siembra=siembra, lanzado=lanzado, terminal=terminal, resultado=resultado)

    @staticmethod
    async def _cerrar_transaccion(
        tx: TransaccionDeConfig,
        *,
        siembra: SiembraConPlazo,
        lanzado: bool,
        terminal: bool,
        resultado: BodySlideResult | None,
    ) -> None:
        """Cierra el journal según lo DEMOSTRADO; nunca declara éxito con la config sin restaurar.

        ``mutar_hasta_terminal`` retiene al caller hasta que el hilo es terminal (review
        Codex #1003): la tool no puede soltar su lock con una restauración en vuelo.
        Un fallo de cierre con una excepción ya en vuelo no la enmascara: el journal
        queda durable y lo resuelve el reconciliador al arrancar.
        """
        if siembra.abandonada:
            return
        if lanzado and not terminal:
            logger.error(
                "BodySlide: corrida %s cancelada con el proceso lanzado y sin terminalidad demostrada; "
                "la config NO se restaura (journal en LANZADO, HOLD hasta tener evidencia).",
                tx.run_id,
            )
            return

        def _terminar_y_restaurar() -> InformeDeRecuperacion:
            # UN solo hilo retenido para las dos escrituras: con dos esperas separadas,
            # una cancelación durante TERMINAL se propagaba antes de restaurar.
            if lanzado:
                tx.marcar_terminal()
            return tx.restaurar()

        try:
            informe = await mutar_hasta_terminal(_terminar_y_restaurar)
        except (JournalError, OSError) as exc:
            logger.error("BodySlide: no se pudo cerrar la transacción %s: %s", tx.run_id, exc)
            if resultado is not None:
                resultado.success = False
                resultado.estado = "RESTORE_FAILED"
                resultado.message = f"No se pudo restaurar la configuración de BodySlide: {exc}"
            return
        if informe.veredicto in {Veredicto.RESTAURADO, Veredicto.SIN_CAMBIOS}:
            if resultado is not None:
                resultado.config_restored = informe.restaurados
            return
        logger.error(
            "BodySlide: la restauración de %s terminó en %s: %s", tx.run_id, informe.veredicto.value, informe.detalle
        )
        if resultado is not None:
            resultado.success = False
            resultado.estado = informe.veredicto.value
            resultado.message = (
                f"La configuración de BodySlide no se restauró ({informe.veredicto.value}): {informe.detalle}"
            )

    @staticmethod
    def _diagnostico(
        *,
        return_code: int,
        nuevos_nif: int,
        nuevos_tri: int,
        destino: pathlib.Path,
        log: _ColaDeLog,
        veredicto: str,
        razon: str,
    ) -> str:
        """Arma la causa legible del fallo, nombrando el destino inspeccionado."""
        artefactos = nuevos_nif + nuevos_tri
        if veredicto == "EVIDENCE_INCOMPLETE":
            causa = f"Evidencia insuficiente ({razon}) en '{destino}'."
        elif return_code != 0:
            causa = f"BodySlide terminó con código {return_code} (artefactos nuevos en '{destino}': {artefactos})."
        elif log.fallo_parcial:
            causa = (
                f"BodySlide construyó {artefactos} artefacto(s) en '{destino}' pero reportó outfits "
                "fallidos en su log. Es un build PARCIAL: el exit code sigue siendo 0, así que la "
                "única señal es el log. Las mallas de los outfits fallidos no existen."
            )
        elif log.conflicto_sin_ganador:
            causa = (
                f"BodySlide eligió un ganador por cuenta un conflicto en '{destino}': {razon}. "
                "La elección no fue declarada por el plan — no se declara éxito ni se acepta la salida."
            )
        elif log.conflicto_verificable:
            causa = (
                f"BodySlide aplicó un conflicto resuelto por selección guardada en '{destino}': {razon}. "
                "La selección guardada no es el ganador declarado por el plan — se exige evidencia adicional."
            )
        elif artefactos == 0:
            causa = (
                f"BodySlide terminó con código 0 pero no generó ningún artefacto nuevo en '{destino}'. "
                "Su exit code no refleja el resultado del build, así que un batch vacío —típicamente "
                "porque no encontró SliderSets visibles— sale como éxito."
            )
        else:
            causa = f"Veredicto {veredicto}: {razon}"
        return f"{causa}\n--- {_NOMBRE_DEL_LOG} ---\n{log.texto}" if log.texto else causa

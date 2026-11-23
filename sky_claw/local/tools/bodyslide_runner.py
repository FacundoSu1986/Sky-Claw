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
import logging
import os
import pathlib
import time
from dataclasses import dataclass

from sky_claw.app.security.links import exigir_contencion_fisica, iter_archivos_propios
from sky_claw.local.tools._process import run_capture
from sky_claw.local.tools.bodyslide_config import (
    BodySlideConfigPreconditionError,
    sembrar_config_de_bodyslide,
)
from sky_claw.local.tools.bodyslide_headless import (
    EvidenciaDeBuild,
    HeadlessContractError,
    ManifiestoArtefactos,
    construir_argv,
    evaluar_veredicto,
    marcadores_de_corrida,
)
from sky_claw.local.tools.output_targets import bodyslide_output_root

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
_ARCHIVOS_DE_CONFIGURACION = ("Config.xml", "BodySlide.xml", "BuildSelection.xml")


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
    try:
        st = log.stat()
    except OSError:
        return None
    try:
        crudo = log.read_bytes()
    except OSError:
        return None
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
    actual = _snapshot_log_inicial(exe)

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


@dataclass(frozen=True, slots=True)
class _SnapshotConfig:
    """Snapshot de un archivo de configuración: ruta original + byte + enlace."""

    kind: str  # "missing" | "regular" | "symlink"
    blob: bytes | None
    link_target: str | None


def _snapshot_y_restaurar_archivos(exe: pathlib.Path):
    """Devuelve ``(snapshot, restaurar)`` — snapshot byte a byte y su restore.

    Los archivos que el proceso pueda mutar se restauran byte a byte al estado
    ORIGINAL (antes de cualquier seed): el restore del original es lo que hace
    a la siembra transaccional, no best-effort (P0-1).

    Un symlink se lee a través del enlace (el destino externo) y se restaura
    a través del enlace — el enlace mismo NUNCA se conserva ni se borra
    (P0-2). El destino externo es el que se restaura.
    """
    snapshot: dict[str, _SnapshotConfig] = {}
    for nombre in _ARCHIVOS_DE_CONFIGURACION:
        ruta = exe.parent / nombre
        try:
            if ruta.is_symlink():
                try:
                    target = os.readlink(ruta)
                except OSError as exc:
                    raise BodySlideExecutionError(f"No se puede leer el destino del enlace '{ruta}': {exc}") from exc
                try:
                    blob = ruta.read_bytes()
                except OSError as exc:
                    raise BodySlideExecutionError(f"No se puede leer '{ruta}' (enlace a '{target}'): {exc}") from exc
                snapshot[nombre] = _SnapshotConfig("symlink", blob, target)
            elif ruta.exists():
                try:
                    snapshot[nombre] = _SnapshotConfig("regular", ruta.read_bytes(), None)
                except OSError as exc:
                    raise BodySlideExecutionError(
                        f"No se puede leer '{ruta}' — la restauración byte a byte post-proceso es imposible: {exc}"
                    ) from exc
            else:
                snapshot[nombre] = _SnapshotConfig("missing", None, None)
        except BodySlideExecutionError:
            raise
        except OSError as exc:
            raise BodySlideExecutionError(
                f"No se puede leer '{ruta}' — la restauración byte a byte post-proceso es imposible: {exc}"
            ) from exc

    def restaurar() -> list[str]:
        restaurados: list[str] = []
        for nombre, snap in snapshot.items():
            ruta = exe.parent / nombre
            try:
                if snap.kind == "missing":
                    # El archivo no existía al snapshot: si apareció, hay que
                    # quitarlo. Un symlink creado por el proceso se borra borrándolo
                    # (unlink de enlace no toca al destino externo).
                    if ruta.exists() or ruta.is_symlink():
                        ruta.unlink()
                        restaurados.append(nombre)
                    continue
                if snap.kind == "symlink":
                    if not ruta.is_symlink():
                        # El enlace desapareció durante el run — re-crear el
                        # enlace al destino original y escribir byte a byte.
                        ruta.symlink_to(snap.link_target)
                    # P0-B: verificar que el enlace sigue apuntando al destino
                    # que se snapshotó. Un enlace re-targeted durante el run
                    # (o un enlace nuevo) apunta a OTRO archivo — escribir a
                    # través del enlace sobrescribiría ese otro archivo.
                    # Fail-closed: abortar la restauración, no pisar.
                    try:
                        destino_actual = os.readlink(ruta)
                    except OSError as exc:
                        raise BodySlideExecutionError(
                            f"No se puede leer el destino del enlace '{ruta}' al restaurar: {exc}"
                        ) from exc
                    if destino_actual != snap.link_target:
                        raise BodySlideExecutionError(
                            f"El enlace '{ruta}' no apunta al destino snapshotado "
                            f"('{snap.link_target}') — destino actual '{destino_actual}'. "
                            "No se escribe a través de un enlace re-targeted (P0-B)"
                        )
                    # Escribimos a TRAVÉS del enlace: el destino externo es el
                    # que se restaura (P0-2). El enlace mismo queda intacto.
                    if ruta.read_bytes() != snap.blob:
                        ruta.write_bytes(snap.blob)
                        restaurados.append(nombre)
                    continue
                # Regular.
                if not ruta.exists():
                    # El proceso lo borró — recrearlo desde el snapshot.
                    ruta.write_bytes(snap.blob)
                    restaurados.append(nombre)
                    continue
                if ruta.is_symlink():
                    # El enlace apareció DURANTE el run — la config se perdió
                    # medio-cubierta (no es nuestro symlink). Conservador:
                    # abortar la restauración, no pisar.
                    raise BodySlideExecutionError(
                        f"'{ruta}' es un symlink al final del run (no era al snapshot) — no se restaura automáticamente"
                    )
                if ruta.read_bytes() != snap.blob:
                    ruta.write_bytes(snap.blob)
                    restaurados.append(nombre)
            except OSError as exc:
                logger.error("No se pudo restaurar '%s': %s", ruta, exc)
                raise BodySlideExecutionError(f"No se pudo restaurar '{ruta}': {exc}") from exc
        return restaurados

    return snapshot, restaurar


def _verificar_identidad_del_staging(raiz: pathlib.Path, candidato: pathlib.Path) -> None:
    """Exige que el staging y el destino se alcancen FÍSICAMENTE sin enlaces.

    La comparación léxica de ``construir_argv`` no ve lo que la filesystem
    materializa: la raíz administrada, sus ancestros existentes y el propio
    destino pueden ser symlinks/junctions que desvían la mutación fuera del
    staging — ``exigir_contencion_fisica`` recorre la cadena con ``lstat``
    sin seguir enlaces, revalida la identidad de cada componente y rechaza
    cualquier redirección. Si la raíz todavía no está materializada (primer run),
    no hay nada que verificar: se deja la propiedad al gate léxico y al post-spawn.

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

        # 3) Snapshot byte a byte ANTES de la siembra (original completo).
        #    Snapshot del log: evidenci la ausencia de escritura de este run (P0-3).
        try:
            _snapshot, restaurar = await asyncio.to_thread(_snapshot_y_restaurar_archivos, self.config.bodyslide_exe)
            pre_snapshot_log = await asyncio.to_thread(_snapshot_log_inicial, self.config.bodyslide_exe)
        except BodySlideExecutionError as exc:
            message = str(exc)
            logger.error("BodySlide aborted before spawn: %s", message)
            return resultado_de_fallo("CONFIG_PRECONDITION_FAILED", message)

        # 4) PERÍMETRO TRANSACCIONAL ÚNICO — todo lo que sigue a la primera
        #    mutación (sembrar Config.xml) queda dentro de un único try/finally
        #    que garantiza la restauración byte a byte del ORIGINAL
        #    (P0-A, auditoría). Cualquier excepción aquí —OSError, asyncio
        #    CancelledError, error inesperado— restaura el original y se
        #    propaga, nunca persiste cambios y declara éxito.
        resultado: BodySlideResult | None = None
        try:
            # 4a) Siembra fail-closed — precondición del spawn (aborta antes de
            #     cualquier mutación del proceso).
            try:
                if raiz_de_salida is not None:
                    await asyncio.to_thread(
                        sembrar_config_de_bodyslide,
                        exe_dir=self.config.bodyslide_exe.parent,
                        game_path=self.config.game_path,
                        output_root=raiz_de_salida,
                        expected_target_game=self.config.expected_target_game,
                    )
            except BodySlideConfigPreconditionError as exc:
                message = str(exc)
                logger.error("BodySlide aborted before spawn: %s", message)
                # Restore ocurre en el finally: el seed pudo escribir antes de
                # fallar en la validación final.
                return resultado_de_fallo("CONFIG_PRECONDITION_FAILED", message)

            destino = pathlib.Path(output_path)
            previos = await asyncio.to_thread(_conjunto_de_archivos_previos, destino)
            destino_propio = not destino.is_symlink()
            preexistentes = len(previos)

            # 4b) Spawn — evidencia + veredicto.
            try:
                stdout, stderr, return_code = await run_capture(
                    args,
                    timeout=self.config.timeout_seconds,
                    cwd=str(self.config.game_path),
                )
            except TimeoutError as exc:
                logger.error("BodySlide execution timed out.")
                raise BodySlideTimeoutError(self.config.timeout_seconds) from exc
            except Exception as e:
                logger.error(f"BodySlide execution failed: {e}")
                raise BodySlideExecutionError(f"Failed to execute BodySlide: {e}") from e

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
            # Restauración byte a byte (Post-Snapshot). Un fallo de restauración es un
            # desenlace de fallo veraz — no se declara éxito con config sin restaurar.
            restaurados = await asyncio.to_thread(restaurar)
            if resultado is not None:
                resultado.config_restored = tuple(restaurados)

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

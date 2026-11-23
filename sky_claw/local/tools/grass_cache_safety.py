"""Contrato de arranque del precache NGIO — NEW / RESUME / REBUILD (P0 SAFE CACHE).

**El hecho que obliga a este módulo (verificado en la fuente, no reportado).**
NGIO-NG ``src/GidFileCache.cpp::GidFileGenerationTask::Init()``, etiqueta
``1.0.13`` (leída el 2026-11-23 vía la API de contenidos de GitHub): lee el
archivo de progreso línea por línea, descarta las líneas en blanco
(``trim`` + ``continue``) y calcula ``IsResuming = !ProgressDone->empty()``;
**si queda vacío**, recorre ``Data/Grass`` y hace ``remove()`` de cada
``.dgid``, ``.cgid`` y ``.fail``. ``GidFileGenerationTask::End()`` borra el
archivo de progreso al terminar — de ahí que su ausencia signifique "terminó".
Consecuencia: un archivo de progreso vacío (no sólo inexistente) destruye el
caché, y cualquier línea no blanca lo preserva. Sky-Claw escribía el flag vacío al entrar a
``GrassCacheRunner.run()``, así que *cualquier* invocación —incluida una que
sólo pretendía reanudar un precache interrumpido— borraba horas de trabajo.
El binario de Nexus 2.2.0 no se pudo inspeccionar acá: el contrato de abajo es
fail-closed respecto de las dos versiones (nunca arranca una operación que
sería destructiva si la semántica de borrado se cumple).

**El contrato, enunciado una sola vez.** El arranque tiene exactamente tres
modos y cada uno tiene precondiciones verificables:

- ``new``: no hay caché ni progreso previo. Escribe el flag vacío. Si hay
  caché o un flag con progreso, **se niega**: no es una operación de ``new``.
- ``resume``: hay progreso demostrable —el flag con contenido, o la evidencia
  archivada por un run anterior cuya huella coincide con el caché actual—.
  **Nunca trunca** el flag: ése es el disparador del borrado en NGIO.
- ``rebuild``: destruye por diseño. Exige aprobación explícita (``REBUILD``)
  **y** un respaldo verificado por hash de todo el caché. Sin respaldo no hay
  arranque, y el respaldo se verifica releyendo el destino desde el disco —no
  el hash que quedó en memoria al copiar—.

**Por qué un módulo y no un ``if`` en el runner.** La misma pregunta ("¿puedo
arrancar?") la contestan el servicio (antes de clonar un perfil) y el runner
(justo antes de escribir el flag, que es el punto sin retorno). Contestarla dos
veces por separado es cómo se produce el defecto #1 del repo —arreglar un
hermano y no al otro—, así que ambos llaman a :func:`preparar_arranque`, que es
idempotente y es la única implementación. :func:`aplicar_plan` es la única
pieza que escribe el flag.

**Evidencia de progreso.** Neutralizar el flag al salir es obligatorio (un flag
residual haría que el juego del usuario arranque en modo precache), pero no
puede costar la reanudación: antes de borrarlo se archiva su contenido, atado a
la huella del caché en ese momento. ``resume`` lo restaura sólo si la huella
coincide; si no coincide, se niega en vez de reanudar sobre un caché que no es
el que dejó ese progreso.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import pathlib
import stat
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from sky_claw.app.security.links import link_kind_or_raise

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Nombre del flag que NGIO consume junto a ``SkyrimSE.exe``. Vive acá porque
#: el inventario, el plan y el runner tienen que hablar del MISMO archivo.
PRECACHE_FLAG: Final = "PrecacheGrass.txt"

#: Token de aprobación de una regeneración destructiva. Es literal a propósito:
#: tiene que poder reconocerse en un prompt HITL y en un log, sin ambigüedad.
APROBACION_REBUILD: Final = "REBUILD"

GrassCacheMode = Literal["new", "resume", "rebuild"]
MODOS_GRASS_CACHE: Final[tuple[GrassCacheMode, ...]] = ("new", "resume", "rebuild")

#: Extensiones que NGIO escribe en ``Data/Grass`` y que su ``Init()`` borra.
#: ``.fail`` se incluye porque es evidencia de celdas problemáticas: perderla
#: deja el diagnóstico sin fuente.
EXTENSIONES_CACHE: Final[tuple[str, ...]] = (".cgid", ".dgid", ".fail")

#: Tamaño de bloque de la copia con hash (1 MiB): el archivo se lee una sola
#: vez para copiar y hashear; la verificación relee el destino.
_BLOQUE = 1024 * 1024

#: Versión del manifiesto del respaldo. Un manifiesto de otra versión no se
#: interpreta: se considera no verificado (fail-closed).
_VERSION_MANIFIESTO: Final = 1

AccionFlag = Literal["escribir_vacio", "conservar", "restaurar"]


class GrassCacheSafetyError(Exception):
    """El arranque no cumple las precondiciones de su modo (fail-closed).

    ``codigo`` es estable y forma parte del contrato: los tests y las capas de
    reporte lo usan para distinguir "no hay caché que perder" de "hay caché y
    nadie decidió qué hacer con él".
    """

    def __init__(self, codigo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.codigo = codigo


# ---------------------------------------------------------------------------
# Inventario (read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InventarioCache:
    """Foto del caché y del flag en el momento del arranque.

    ``huella`` resume el CONJUNTO (nombre, tamaño) ordenado de los archivos del
    caché: dos inventarios con la misma huella describen el mismo conjunto de
    archivos con los mismos tamaños. No es un hash de contenido (un ``.cgid``
    reescrito con el mismo tamaño daría la misma huella), y por eso se usa para
    atestiguar *qué conjunto de archivos* respalda un progreso, no para
    certificar que el caché es correcto — eso es del veredicto (F-02).
    """

    cgid_count: int
    cgid_bytes: int
    dgid_count: int
    fail_count: int
    bytes_total: int
    flag_presente: bool
    flag_progreso: str | None
    huella: str

    @property
    def archivos_cache(self) -> int:
        """Archivos ``.cgid`` + ``.dgid`` + ``.fail`` (lo que NGIO puede borrar)."""
        return self.cgid_count + self.dgid_count + self.fail_count

    @property
    def bytes_cache(self) -> int:
        """Bytes de TODO lo que NGIO puede borrar (``.cgid`` + ``.tgid`` + ``.fail``).

        El respaldo copia las tres extensiones, así que la verificación de
        integridad tiene que comparar contra el mismo conjunto: contar sólo los
        ``.cgid`` daría un respaldo "incompleto" siempre y un ``rebuild`` jamás
        habilitado.
        """
        return self.bytes_total

    @property
    def tiene_cache(self) -> bool:
        return self.archivos_cache > 0

    def a_dict(self) -> dict[str, Any]:
        """Resumen serializable para resultados, journal y HITL."""
        return {
            "cgid_count": self.cgid_count,
            "cgid_bytes": self.cgid_bytes,
            "bytes_total": self.bytes_total,
            "dgid_count": self.dgid_count,
            "fail_count": self.fail_count,
            "flag_presente": self.flag_presente,
            "flag_con_progreso": self.flag_progreso is not None,
            "huella": self.huella,
            "archivos_cache": self.archivos_cache,
        }


def ruta_del_flag(game_path: pathlib.Path) -> pathlib.Path:
    """Ruta canónica del flag junto al ejecutable del juego."""
    return game_path / PRECACHE_FLAG


def _rechazar_enlace(ruta: pathlib.Path, codigo: str) -> None:
    """Fail-closed: nunca se lee, copia ni escribe a través de un enlace.

    Un ``.cgid`` que en realidad es un junction haría que el respaldo copiara
    el destino (potencialmente fuera del rig) y que un borrado lo alcanzara.
    """
    try:
        tipo = link_kind_or_raise(ruta)
    except OSError as exc:  # sin poder afirmar que no es un enlace, no se toca
        raise GrassCacheSafetyError(codigo, f"No se pudo clasificar {ruta}: {exc}") from exc
    if tipo is not None:
        raise GrassCacheSafetyError(codigo, f"{ruta} es un enlace ({tipo}): el caché no se toca a través de enlaces.")


def _leer_flag(flag_path: pathlib.Path) -> tuple[bool, str | None]:
    """``(presente, progreso)`` del flag. Progreso = contenido no vacío.

    No se interpreta el formato del progreso: cualquier contenido no blanco se
    trata como progreso. Un formato desconocido entonces *protege* (impide
    ``new``/``rebuild`` sin decisión), que es el lado seguro del error.
    """
    if not flag_path.exists():
        return (False, None)
    _rechazar_enlace(flag_path, "flag_es_enlace")
    if flag_path.is_dir():
        raise GrassCacheSafetyError("flag_es_directorio", f"{flag_path} es un directorio, no el flag de NGIO.")
    try:
        contenido = flag_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise GrassCacheSafetyError("flag_ilegible", f"No se pudo leer {flag_path}: {exc}") from exc
    return (True, contenido if contenido.strip() else None)


def huella_del_cache(entradas: Iterable[tuple[str, int]]) -> str:
    """sha256 de ``(nombre, tamaño)`` ordenado — identidad del conjunto cacheado."""
    digest = hashlib.sha256()
    for nombre, tamano in sorted(entradas):
        digest.update(f"{nombre}\x00{tamano}\n".encode())
    return digest.hexdigest()


def inventariar_cache(*, grass_dir: pathlib.Path, flag_path: pathlib.Path) -> InventarioCache:
    """Lee —sin mutar nada— el caché de ``grass_dir`` y el estado del flag."""
    cgid_count = 0
    cgid_bytes = 0
    dgid_count = 0
    fail_count = 0
    bytes_total = 0
    entradas: list[tuple[str, int]] = []

    try:
        candidatos = list(grass_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        # NGIO crea Grass/ al escribir el primer .cgid: ausente = caché vacío.
        candidatos = []
    except OSError as exc:
        raise GrassCacheSafetyError("cache_ilegible", f"No se pudo leer {grass_dir}: {exc}") from exc

    for archivo in candidatos:
        if archivo.suffix.lower() not in EXTENSIONES_CACHE:
            continue
        _rechazar_enlace(archivo, "cache_con_enlaces")
        try:
            info = archivo.stat()
        except OSError as exc:
            raise GrassCacheSafetyError("cache_ilegible", f"No se pudo leer {archivo}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            # Un directorio con nombre .cgid no es caché: no se copia ni se cuenta.
            logger.warning("Se ignora %s: no es un archivo regular.", archivo)
            continue
        entradas.append((archivo.name, int(info.st_size)))
        bytes_total += int(info.st_size)
        sufijo = archivo.suffix.lower()
        if sufijo == ".cgid":
            cgid_count += 1
            cgid_bytes += int(info.st_size)
        elif sufijo == ".dgid":
            dgid_count += 1
        else:
            fail_count += 1

    presente, progreso = _leer_flag(flag_path)
    return InventarioCache(
        cgid_count=cgid_count,
        cgid_bytes=cgid_bytes,
        dgid_count=dgid_count,
        fail_count=fail_count,
        bytes_total=bytes_total,
        flag_presente=presente,
        flag_progreso=progreso,
        huella=huella_del_cache(entradas),
    )


# ---------------------------------------------------------------------------
# Respaldo verificado
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RespaldoVerificado:
    """Respaldo de un conjunto de caché, con manifiesto releído del disco."""

    destino: pathlib.Path
    manifiesto: pathlib.Path
    archivos: int
    bytes: int
    huella: str

    def cubre(self, inventario: InventarioCache) -> bool:
        """True si ESTE respaldo corresponde exactamente a ese inventario."""
        return (
            self.huella == inventario.huella
            and self.archivos == inventario.archivos_cache
            and self.bytes == inventario.bytes_cache
        )


def _copiar_con_hash(origen: pathlib.Path, destino: pathlib.Path) -> tuple[int, str]:
    """Copia por bloques calculando sha256 del ORIGEN; devuelve (bytes, sha256)."""
    digest = hashlib.sha256()
    total = 0
    with origen.open("rb") as src, destino.open("wb") as dst:
        while True:
            bloque = src.read(_BLOQUE)
            if not bloque:
                break
            digest.update(bloque)
            dst.write(bloque)
            total += len(bloque)
        dst.flush()
        os.fsync(dst.fileno())
    return (total, digest.hexdigest())


def _hash_de_archivo(ruta: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with ruta.open("rb") as fh:
        while True:
            bloque = fh.read(_BLOQUE)
            if not bloque:
                break
            digest.update(bloque)
    return digest.hexdigest()


def _fsync_directorio(ruta: pathlib.Path) -> None:
    """Durabilidad del rename/creación del manifiesto donde el SO lo permite."""
    if os.name == "nt":  # abrir un directorio como fd falla en Windows
        return
    with contextlib.suppress(OSError):
        fd = os.open(ruta, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _leer_manifiesto(ruta: pathlib.Path) -> dict[str, Any] | None:
    try:
        crudo = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return crudo if isinstance(crudo, dict) else None


def _respaldo_existente_verifica(destino: pathlib.Path, inventario: InventarioCache) -> RespaldoVerificado | None:
    """Reusa un respaldo previo del MISMO conjunto sin recopiarlo.

    Verifica manifiesto (versión, huella, conteos, bytes) y el tamaño de cada
    archivo en el destino. No rehashea: el hash se verificó al crear el respaldo
    y la re-verificación por hash está reservada a la creación, que es donde un
    corte puede dejar un archivo a medias.
    """
    manifiesto = _leer_manifiesto(destino / "manifiesto.json")
    if manifiesto is None:
        return None
    if manifiesto.get("version") != _VERSION_MANIFIESTO or manifiesto.get("huella") != inventario.huella:
        return None
    archivos = manifiesto.get("archivos")
    if not isinstance(archivos, list):
        return None
    total = 0
    for entrada in archivos:
        if not isinstance(entrada, dict):
            return None
        nombre = entrada.get("nombre")
        tamano = entrada.get("bytes")
        if not isinstance(nombre, str) or not isinstance(tamano, int):
            return None
        ruta = destino / nombre
        try:
            if ruta.stat().st_size != tamano:
                return None
        except OSError:
            return None
        total += tamano
    if len(archivos) != inventario.archivos_cache or total != inventario.bytes_cache:
        return None
    return RespaldoVerificado(
        destino=destino,
        manifiesto=destino / "manifiesto.json",
        archivos=len(archivos),
        bytes=total,
        huella=inventario.huella,
    )


def respaldar_cache(
    *,
    grass_dir: pathlib.Path,
    destino: pathlib.Path,
    inventario: InventarioCache,
) -> RespaldoVerificado:
    """Copia el caché a *destino*, verifica en disco y escribe un manifiesto.

    Verificación real: cada archivo copiado se **relee del destino** y su
    sha256 tiene que coincidir con el del origen. Un corte a mitad de copia deja
    un archivo de tamaño distinto o de hash distinto ⇒ no hay respaldo
    verificado ⇒ ``rebuild`` no arranca.

    Idempotente: si ya existe un respaldo que verifica para este mismo
    inventario, se reusa en vez de recopiar gigabytes.
    """
    reusado = _respaldo_existente_verifica(destino, inventario) if destino.exists() else None
    if reusado is not None:
        logger.info("Respaldo de grass ya verificado en %s (huella %s); se reusa.", destino, inventario.huella)
        return reusado

    try:
        # Existencia exclusiva: dos respaldos concurrentes no comparten carpeta.
        # Un destino previo inválido (p. ej. de un corte) se aparta, no se pisa.
        destino.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        invalido = destino.with_name(f"{destino.name}.invalido-{int(time.time())}")
        with contextlib.suppress(OSError):
            os.replace(destino, invalido)
        destino.mkdir(parents=True, exist_ok=False)

    archivos: list[dict[str, Any]] = []
    total = 0
    for origen in sorted(grass_dir.iterdir(), key=lambda p: p.name):
        if origen.suffix.lower() not in EXTENSIONES_CACHE:
            continue
        if not origen.is_file():
            continue
        _rechazar_enlace(origen, "cache_con_enlaces")
        copia = destino / origen.name
        bytes_copiados, hash_origen = _copiar_con_hash(origen, copia)
        if copia.stat().st_size != bytes_copiados or _hash_de_archivo(copia) != hash_origen:
            raise GrassCacheSafetyError(
                "respaldo_no_verificado",
                f"El respaldo de {origen.name} no coincide con el origen (copia truncada o ilegible).",
            )
        archivos.append({"nombre": origen.name, "bytes": bytes_copiados, "sha256": hash_origen})
        total += bytes_copiados

    if len(archivos) != inventario.archivos_cache or total != inventario.bytes_cache:
        raise GrassCacheSafetyError(
            "respaldo_incompleto",
            f"El respaldo copió {len(archivos)} archivos/{total} bytes, pero el inventario "
            f"declara {inventario.archivos_cache}/{inventario.bytes_cache}: el caché cambió durante el respaldo.",
        )

    manifiesto = {
        "version": _VERSION_MANIFIESTO,
        "huella": inventario.huella,
        "archivos": archivos,
        "bytes": total,
        "creado": time.time(),
        "origen": str(grass_dir),
        "inventario": inventario.a_dict(),
    }
    ruta_manifiesto = destino / "manifiesto.json"
    tmp = destino / ".manifiesto.json.tmp"
    tmp.write_text(json.dumps(manifiesto, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ruta_manifiesto)
    _fsync_directorio(destino)

    # Releer del disco: el respaldo vale por lo que quedó escrito, no por el dict.
    releido = _leer_manifiesto(ruta_manifiesto)
    if releido is None or releido.get("huella") != inventario.huella:
        raise GrassCacheSafetyError("respaldo_no_verificado", f"El manifiesto {ruta_manifiesto} no se pudo releer.")
    return RespaldoVerificado(
        destino=destino,
        manifiesto=ruta_manifiesto,
        archivos=len(archivos),
        bytes=total,
        huella=inventario.huella,
    )


# ---------------------------------------------------------------------------
# Evidencia de progreso (reanudación)
# ---------------------------------------------------------------------------


def _dir_progreso(raiz_estado: pathlib.Path) -> pathlib.Path:
    return raiz_estado / "progreso"


def _ruta_progreso(raiz_estado: pathlib.Path, huella: str) -> pathlib.Path:
    return _dir_progreso(raiz_estado) / f"{huella}.json"


def archivar_progreso(raiz_estado: pathlib.Path, inventario: InventarioCache, contenido: str) -> pathlib.Path:
    """Guarda el contenido del flag atado a la huella del caché de ESE momento.

    Se llama antes de neutralizar el flag (el juego del usuario no puede quedar
    en modo precache) y antes de pisarlo en un ``rebuild``. Es best-effort por
    contrato del llamador: un fallo acá no puede impedir la limpieza del flag.
    """
    destino = _ruta_progreso(raiz_estado, inventario.huella)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".json.tmp")
    carga = {
        "version": _VERSION_MANIFIESTO,
        "huella": inventario.huella,
        "inventario": inventario.a_dict(),
        "contenido": contenido,
        "archivado": time.time(),
    }
    tmp.write_text(json.dumps(carga, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, destino)
    _fsync_directorio(destino.parent)
    return destino


def recuperar_progreso(raiz_estado: pathlib.Path | None, inventario: InventarioCache) -> str | None:
    """Progreso archivado para ESTE caché, o ``None``.

    La huella tiene que coincidir: reanudar con el progreso de otro conjunto de
    archivos haría que NGIO saltee celdas que nunca se escribieron.
    """
    if raiz_estado is None:
        return None
    ruta = _ruta_progreso(raiz_estado, inventario.huella)
    carga = _leer_manifiesto(ruta)
    if carga is None or carga.get("version") != _VERSION_MANIFIESTO or carga.get("huella") != inventario.huella:
        return None
    contenido = carga.get("contenido")
    if not isinstance(contenido, str) or not contenido.strip():
        return None
    return contenido


# ---------------------------------------------------------------------------
# Decisión y aplicación del plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanArranque:
    """Qué hacer con el flag, ya decidido y con el respaldo verificado adentro."""

    modo: GrassCacheMode
    accion: AccionFlag
    contenido: str
    inventario: InventarioCache
    respaldo: RespaldoVerificado | None = None

    def a_dict(self) -> dict[str, Any]:
        return {
            "mode": self.modo,
            "accion_flag": self.accion,
            "inventario": self.inventario.a_dict(),
            "respaldo": str(self.respaldo.destino) if self.respaldo is not None else None,
        }


def planificar_arranque(
    *,
    modo: GrassCacheMode,
    inventario: InventarioCache,
    aprobacion: str | None = None,
    respaldo: RespaldoVerificado | None = None,
    progreso_archivado: str | None = None,
) -> PlanArranque:
    """Decide la acción sobre el flag o levanta ``GrassCacheSafetyError``.

    Es función pura: no toca el disco. El respaldo se recibe ya verificado y se
    vuelve a contrastar contra el inventario — un respaldo de otro conjunto de
    archivos no habilita nada.
    """
    if modo == "new":
        if inventario.tiene_cache:
            raise GrassCacheSafetyError(
                "cache_existente_sin_decision",
                f"Hay un caché de grass preexistente ({inventario.cgid_count} .cgid, "
                f"{inventario.cgid_bytes} bytes) y el modo es NEW: NGIO borraría ese caché al arrancar "
                "con el flag sin progreso. Elegí mode='resume' para continuarlo o mode='rebuild' "
                f"(aprobación '{APROBACION_REBUILD}' + respaldo verificado) para regenerarlo.",
            )
        if inventario.flag_progreso is not None:
            raise GrassCacheSafetyError(
                "flag_con_progreso_sin_decision",
                f"{PRECACHE_FLAG} tiene progreso y el modo es NEW: escribir el flag vacío lo destruiría. "
                "Usá mode='resume' para continuar desde ese progreso.",
            )
        return PlanArranque(modo="new", accion="escribir_vacio", contenido="", inventario=inventario)

    if modo == "resume":
        if inventario.flag_progreso is not None:
            # Se conserva el contenido EXACTO: es la señal de reanudación de NGIO.
            return PlanArranque(
                modo="resume", accion="conservar", contenido=inventario.flag_progreso, inventario=inventario
            )
        if progreso_archivado is not None:
            return PlanArranque(modo="resume", accion="restaurar", contenido=progreso_archivado, inventario=inventario)
        raise GrassCacheSafetyError(
            "resume_sin_progreso",
            f"No hay progreso que reanudar: {PRECACHE_FLAG} no existe (o está vacío) y no hay evidencia "
            "archivada que corresponda a este caché. NGIO empezaría de cero y borraría el caché existente: "
            f"usá mode='rebuild' con aprobación '{APROBACION_REBUILD}' y respaldo verificado.",
        )

    # modo == "rebuild"
    if aprobacion != APROBACION_REBUILD:
        raise GrassCacheSafetyError(
            "rebuild_sin_aprobacion",
            f"El modo REBUILD regenera el caché desde cero y NGIO borra el anterior: exige "
            f"aprobación explícita ('{APROBACION_REBUILD}').",
        )
    if inventario.tiene_cache:
        if respaldo is None:
            raise GrassCacheSafetyError(
                "rebuild_sin_respaldo",
                "REBUILD con caché preexistente exige un respaldo verificado; no hay ninguno.",
            )
        if not respaldo.cubre(inventario):
            raise GrassCacheSafetyError(
                "respaldo_no_cubre_el_cache",
                f"El respaldo {respaldo.destino} no corresponde a este caché "
                f"({respaldo.archivos}/{respaldo.bytes} vs {inventario.archivos_cache}/{inventario.bytes_cache}).",
            )
    return PlanArranque(modo="rebuild", accion="escribir_vacio", contenido="", inventario=inventario, respaldo=respaldo)


def preparar_arranque(
    *,
    modo: GrassCacheMode,
    game_path: pathlib.Path,
    grass_dir: pathlib.Path,
    aprobacion: str | None = None,
    raiz_estado: pathlib.Path | None = None,
) -> PlanArranque:
    """Inventaría, respalda si el modo lo exige y devuelve el plan — idempotente.

    Es el único punto que contesta "¿puedo arrancar?"; lo llaman el servicio
    (antes de clonar el perfil) y el runner (antes de escribir el flag). Repetir
    la llamada no recopia el respaldo ni rearchiva el progreso.
    """
    inventario = inventariar_cache(grass_dir=grass_dir, flag_path=ruta_del_flag(game_path))
    respaldo: RespaldoVerificado | None = None
    if modo == "rebuild" and inventario.tiene_cache and aprobacion == APROBACION_REBUILD:
        if raiz_estado is None:
            raise GrassCacheSafetyError(
                "rebuild_sin_respaldo",
                "REBUILD necesita una raíz de estado durable donde dejar el respaldo verificado "
                "(no se recibió ninguna).",
            )
        respaldo = respaldar_cache(
            grass_dir=grass_dir,
            destino=raiz_estado / "respaldo" / inventario.huella,
            inventario=inventario,
        )
    if modo == "rebuild" and inventario.flag_progreso is not None and raiz_estado is not None:
        # El flag con progreso se pisa en un REBUILD: su contenido queda como
        # evidencia antes de perderlo (best-effort, no bloquea).
        with contextlib.suppress(OSError):
            archivar_progreso(raiz_estado, inventario, inventario.flag_progreso)
    progreso = recuperar_progreso(raiz_estado, inventario) if modo == "resume" else None
    return planificar_arranque(
        modo=modo,
        inventario=inventario,
        aprobacion=aprobacion,
        respaldo=respaldo,
        progreso_archivado=progreso,
    )


def aplicar_plan(plan: PlanArranque, flag_path: pathlib.Path) -> None:
    """Escribe el flag según el plan. Única función que lo muta.

    ``conservar`` no escribe nada: reescribir el mismo contenido sería inocuo
    hoy, pero cualquier normalización futura (saltos de línea, BOM, recorte)
    convertiría una reanudación correcta en un borrado de caché.
    """
    if plan.accion == "conservar":
        return
    _rechazar_enlace(flag_path, "flag_es_enlace")
    tmp = flag_path.with_name(f"{flag_path.name}.tmp")
    try:
        tmp.write_text(plan.contenido, encoding="utf-8")
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, flag_path)
    except OSError:
        # Un temporal a medias no puede quedar junto al exe: el juego no lo lee,
        # pero confundiría al operador que audita el directorio del juego.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    _fsync_directorio(flag_path.parent)


def neutralizar_flag(
    *,
    flag_path: pathlib.Path,
    grass_dir: pathlib.Path,
    raiz_estado: pathlib.Path | None,
) -> None:
    """Archiva el progreso (si lo hay) y borra el flag. Nunca propaga errores.

    El borrado es obligatorio: un flag residual haría que el juego del usuario
    arranque en modo precache. El archivado preserva lo único que permitiría
    reanudar sin destruir el caché, así que se intenta SIEMPRE antes.
    """
    try:
        hay_flag = flag_path.exists()
    except OSError:
        hay_flag = False
    if hay_flag and raiz_estado is not None:
        try:
            inventario = inventariar_cache(grass_dir=grass_dir, flag_path=flag_path)
            if inventario.flag_progreso is not None:
                archivar_progreso(raiz_estado, inventario, inventario.flag_progreso)
        except (GrassCacheSafetyError, OSError):
            logger.warning("No se pudo archivar el progreso del flag antes de neutralizarlo.", exc_info=True)
    with contextlib.suppress(OSError):
        flag_path.unlink(missing_ok=True)


def ruta_de_respaldo(plan: PlanArranque) -> str:
    """Ruta del respaldo del plan, o cadena vacía (para el resultado)."""
    return str(plan.respaldo.destino) if plan.respaldo is not None else ""


__all__ = [
    "APROBACION_REBUILD",
    "EXTENSIONES_CACHE",
    "MODOS_GRASS_CACHE",
    "PRECACHE_FLAG",
    "GrassCacheMode",
    "GrassCacheSafetyError",
    "InventarioCache",
    "PlanArranque",
    "RespaldoVerificado",
    "aplicar_plan",
    "archivar_progreso",
    "huella_del_cache",
    "inventariar_cache",
    "neutralizar_flag",
    "planificar_arranque",
    "preparar_arranque",
    "recuperar_progreso",
    "respaldar_cache",
    "ruta_de_respaldo",
    "ruta_del_flag",
]

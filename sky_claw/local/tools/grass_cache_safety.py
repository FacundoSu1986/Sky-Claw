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
caché, y cualquier línea no blanca lo preserva.

**El contrato, enunciado una sola vez.** El arranque tiene exactamente tres
modos y cada uno tiene precondiciones verificables:

- ``new``: no hay caché ni progreso previo. Escribe el flag vacío —el mismo
  disparador de borrado que ``rebuild``—, así que exige el reconocimiento de
  cobertura USVFS igual que ``rebuild``. Si hay caché o un flag con progreso,
  **se niega**: no es una operación de ``new``.
- ``resume``: hay progreso demostrable —el flag con contenido, o la evidencia
  archivada por un run anterior—. **Nunca trunca** el flag: ése es el
  disparador del borrado en NGIO. La evidencia archivada sólo se restaura si
  coinciden **la huella ``(nombre, tamaño)`` y la huella de CONTENIDO** del
  caché actual: un ``.cgid`` que cambió de bytes conservando el tamaño
  pertenece a otro caché y su progreso no se restaura.
- ``rebuild``: destruye por diseño. Exige aprobación explícita (``REBUILD``),
  un respaldo **verificado por contenido contra el caché actual** y el
  reconocimiento de que la cobertura USVFS no está demostrada (§ cobertura).

**Por qué un módulo y no un ``if`` en el runner.** La misma pregunta ("¿puedo
arrancar?") la contestan el servicio (antes de clonar un perfil) y el runner
(justo antes de escribir el flag, que es el punto sin retorno). Contestarla dos
veces por separado es cómo se produce el defecto #1 del repo —arreglar un
hermano y no al otro—, así que ambos llaman a :func:`preparar_arranque`, que es
idempotente y es la única implementación. :func:`aplicar_plan` es la única
pieza que escribe el flag.

**Cobertura USVFS: NO demostrada (HOLD explícito).** Este módulo inventaría y
respalda ``<mo2>/overwrite/Grass`` — lo que Sky-Claw puede ver y lo que MO2
escribe físicamente. NGIO, en cambio, enumera ``Data/Grass`` **a través de la
USVFS**, que superpone el ``Grass/`` de todos los mods habilitados; el propio
repo lo da por cierto en ``validators/etapas_previas_lods.py``, que busca
``*.cgid`` tanto en ``overwrite/Grass`` como en el ``Grass/`` de cada mod
habilitado. Consecuencias que no se pueden cerrar sin un rig Windows:

1. Un ``.cgid`` que vive en un mod habilitado **no está en el respaldo**.
2. No está demostrado si ``remove()`` sobre una ruta virtualizada borra
   físicamente el archivo del mod o deja un tombstone en ``overwrite``
   (``NO VERIFICADO``).

Por eso **todo arranque que escriba el flag vacío** (``new`` y ``rebuild``)
está en **HOLD por defecto**: sin ``ack_usvfs_no_verificado=True`` no arranca,
y el respaldo se declara "lo que se pudo ver", nunca "protección completa".
Que el inventario local esté vacío prueba que no se pierde nada *de
``overwrite/Grass``*; no dice nada del overlay. ``resume`` con progreso real no
escribe vacío y no necesita el reconocimiento. El reconocimiento es explícito,
trazado y visible en el prompt HITL — el mismo patrón que
``force_stage_guard``. Cerrar el HOLD exige evidencia de rig sobre qué hace
USVFS al borrar, y es trabajo de P2 (USVFS), no de este módulo.

**Qué significa ``ack_usvfs_no_verificado=True`` (y qué no).** Es **aceptación
de un riesgo**, no certificación de cobertura: no mide, no enumera ni demuestra
nada sobre la USVFS; sólo deja constancia de que quien lo activa sabe que el
``remove()`` de NGIO puede alcanzar archivos que Sky-Claw no inventarió. Por eso
el plan declara siempre ``cobertura_usvfs: no_verificada`` y el respaldo se
describe como "lo que se pudo ver". Consecuencia operativa: **no se ejecutan
operaciones destructivas sobre cachés reales** sin evidencia de rig o un
respaldo completo de los archivos alcanzables — el ack no reemplaza ninguna de
las dos cosas.

**Evidencia de progreso y limpieza verificable.** Neutralizar el flag al salir
es obligatorio (un flag residual haría que el juego del usuario arranque en
modo precache), pero no puede costar la reanudación: antes de borrarlo se
archiva su contenido **byte a byte** (base64 + sha256, sin decodificar como
texto), atado a la huella ``(nombre, tamaño)`` **y** a la huella de contenido
del caché en ese momento. ``resume`` lo restaura sólo si las dos coinciden.

La limpieza es explícita en tres sentidos: un ``unlink`` que falla deja
``flag_residual``; un archivado que falla al borrar deja ``progreso_perdido``
(la evidencia se perdió aunque el flag ya no esté); y una inspección o
clasificación incierta del flag **no autoriza borrarlo** (fail-closed: "no sé
qué es" no es "es un archivo normal"). Ninguno de los dos estados se reporta
como éxito.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import secrets
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from sky_claw.app.security.links import (
    ContencionFisicaVioladaError,
    exigir_arbol_copiable_sin_reparse,
    exigir_contencion_fisica,
    link_kind_or_raise,
    path_present,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: Nombre del flag que NGIO consume junto a ``SkyrimSE.exe``. Vive acá porque
#: el inventario, el plan y el runner tienen que hablar del MISMO archivo.
PRECACHE_FLAG: Final = "PrecacheGrass.txt"
#: Nombre del flag ALTERNATIVO que NGIO también lee. ``Util::getProgressFilePath``
#: (``src/Util.cpp``, tag 1.0.13) hace: si ``PrecacheGrass.txt`` existe lo usa;
#: si no, prueba ``PrecacheGrass.txt.txt`` —"Dumb user mode for .txt.txt file
#: name"—; si tampoco, el primario.
PRECACHE_FLAG_ALTERNATIVO: Final = "PrecacheGrass.txt.txt"

#: Token de aprobación de una regeneración destructiva. Es literal a propósito:
#: tiene que poder reconocerse en un prompt HITL y en un log, sin ambigüedad.
APROBACION_REBUILD: Final = "REBUILD"

GrassCacheMode = Literal["new", "resume", "rebuild"]
MODOS_GRASS_CACHE: Final[tuple[GrassCacheMode, ...]] = ("new", "resume", "rebuild")

#: Extensiones que NGIO escribe en ``Data/Grass`` y que su ``Init()`` borra.
#: ``.fail`` se incluye porque es evidencia de celdas problemáticas: perderla
#: deja el diagnóstico sin fuente.
EXTENSIONES_CACHE: Final[tuple[str, ...]] = (".cgid", ".dgid", ".fail")

#: Tamaño de bloque de la copia con hash (1 MiB).
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
    caché. Es deliberadamente **barata**: se calcula en cada arranque (dos
    veces por run) y sobre decenas de miles de archivos un hash de contenido
    costaría leer el caché entero en cada preflight. Por eso NO se usa para
    autorizar un borrado —para eso está el respaldo, que sí verifica contenido
    contra el origen (§ :func:`respaldar_cache`)— sino para atar la evidencia
    de progreso a un conjunto concreto de archivos.

    ``flag_progreso`` son los **bytes exactos** del flag (no un ``str``): el
    contenido se archiva y se restaura byte a byte, sin decodificar. Un flag
    con bytes que no son UTF-8 válido sigue siendo progreso para NGIO (que lee
    líneas y las recorta) y sigue siendo evidencia que no se puede perder.
    """

    cgid_count: int
    cgid_bytes: int
    dgid_count: int
    fail_count: int
    bytes_total: int
    flag_presente: bool
    flag_progreso: bytes | None
    huella: str

    @property
    def archivos_cache(self) -> int:
        """Archivos ``.cgid`` + ``.dgid`` + ``.fail`` (lo que NGIO puede borrar)."""
        return self.cgid_count + self.dgid_count + self.fail_count

    @property
    def bytes_cache(self) -> int:
        """Bytes de TODO lo que NGIO puede borrar (``.cgid`` + ``.dgid`` + ``.fail``).

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


def _validar_raiz_fisica(
    ruta: pathlib.Path,
    *,
    codigo_enlace: str = "cache_raiz_enlazada",
    codigo_no_absoluta: str = "cache_raiz_no_absoluta",
    codigo_ilegible: str = "cache_raiz_ilegible",
    que: str = "del caché de grass",
) -> None:
    """Exige que ``grass_dir`` cuelgue de una cadena de directorios REALES.

    Rechazar sólo los enlaces *dentro* del caché deja pasar el caso que importa:
    ``overwrite/Grass`` siendo un symlink o un junction a un árbol externo. Se
    reutiliza :func:`exigir_contencion_fisica` (la primitiva del repo: hace
    ``lstat`` de cada componente, no sigue enlaces y clasifica junctions de
    Windows) anclando en la raíz del volumen.

    Un componente inexistente no puede ser un enlace y el recorrido se detiene
    ahí (primer run, ``Grass/`` todavía no creado).
    """
    ancla = pathlib.Path(ruta.anchor)
    if not ruta.is_absolute() or ancla == ruta:
        raise GrassCacheSafetyError(
            codigo_no_absoluta,
            f"La raíz {que} debe ser una ruta absoluta bajo un directorio real: {ruta}",
        )
    try:
        exigir_contencion_fisica(ancla, ruta)
    except ContencionFisicaVioladaError as exc:
        raise GrassCacheSafetyError(
            codigo_enlace,
            f"La raíz {que} atraviesa un enlace o un componente no real: {exc}. "
            "No se escribe ni se lee a través de una redirección.",
        ) from exc
    except OSError as exc:
        raise GrassCacheSafetyError(codigo_ilegible, f"No se pudo validar la raíz {que} {ruta}: {exc}") from exc


#: Caracteres que NGIO recorta de cada línea del flag: `Util::StringHelpers::trim`
#: (`include/GrassControl/Util.h`, tag 1.0.13) quita EXACTAMENTE ``" \r\v\n"``.
#: No incluye el tab ni el form feed, y no interpreta comillas.
_NGIO_RECORTE = b" \r\v\n"


def _ngio_tiene_progreso(crudo: bytes) -> bool:
    """¿NGIO vería progreso en estos bytes? Réplica literal de 1.0.13.

    ``GidFileGenerationTask::Init()`` (``src/GidFileCache.cpp``) parte el archivo
    con ``std::getline`` —que corta **sólo** en ``\n``—, recorta cada línea con
    ``Util::StringHelpers::trim`` —que quita **sólo** ``" \r\v\n"``— y descarta
    las vacías: ``IsResuming = !ProgressDone->empty()``. Si el progreso queda
    vacío, NGIO **borra** ``*.cgid``/``*.dgid``/``*.fail`` de ``Data/Grass``.

    **Por qué una réplica y no ``str.strip()``.** ``str.strip()`` recorta más
    (tab, form feed, ``\x1c``-``\x1f``, espacios Unicode) y ``splitlines()``
    corta en más caracteres. La diferencia no es cosmética: si Sky-Claw viera
    progreso donde NGIO no lo ve, autorizaría un ``resume`` sobre un archivo que
    NGIO trata como vacío —y el ``remove()`` de ``Init()`` se llevaría el caché—.
    Este predicado es el de NGIO, byte a byte, y lo usan por igual el flag vivo y
    el contenido archivado (``neutralizar_flag`` archiva sólo si esto es ``True``).
    """
    return any(linea.strip(_NGIO_RECORTE) for linea in crudo.split(b"\n"))


def _leer_flag(flag_path: pathlib.Path) -> tuple[bool, bytes | None]:
    """``(presente, progreso)`` del flag, con el progreso en BYTES EXACTOS.

    No se decodifica como texto: archivar y restaurar tienen que devolver los
    mismos bytes, y ``errors="replace"`` cambiaría el contenido de un flag con
    codificación inesperada (o con bytes sueltos) justo cuando ese contenido es
    la única evidencia de reanudación.

    "Tiene progreso" lo decide :func:`_ngio_tiene_progreso`, la réplica del
    lector de NGIO 1.0.13. Un formato desconocido entonces *protege* —impide
    ``new``/``resume``/``rebuild`` sin decisión—, que es el lado seguro: de las
    dos formas de equivocarse, sólo "creer que hay progreso" puede terminar en
    un borrado del caché, así que la clasificación nunca es más laxa que NGIO.
    """
    if not path_present(flag_path):
        return (False, None)
    _rechazar_enlace(flag_path, "flag_es_enlace")
    if flag_path.is_dir():
        raise GrassCacheSafetyError("flag_es_directorio", f"{flag_path} es un directorio, no el flag de NGIO.")
    try:
        crudo = flag_path.read_bytes()
    except OSError as exc:
        raise GrassCacheSafetyError("flag_ilegible", f"No se pudo leer {flag_path}: {exc}") from exc
    hay_progreso = _ngio_tiene_progreso(crudo)
    return (True, crudo if hay_progreso else None)


def huella_del_cache(entradas: Iterable[tuple[str, int]]) -> str:
    """sha256 de ``(nombre, tamaño)`` ordenado — identidad del conjunto cacheado."""
    digest = hashlib.sha256()
    for nombre, tamano in sorted(entradas):
        digest.update(f"{nombre}\x00{tamano}\n".encode())
    return digest.hexdigest()


def inventariar_cache(*, grass_dir: pathlib.Path, flag_path: pathlib.Path) -> InventarioCache:
    """Lee —sin mutar nada— el caché de ``grass_dir`` y el estado del flag."""
    _validar_raiz_fisica(grass_dir)
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
# Respaldo verificado por CONTENIDO
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RespaldoVerificado:
    """Respaldo de un conjunto de caché, con manifiesto releído del disco.

    ``huella_contenido`` es sha256 sobre ``(nombre, bytes, sha256)`` ordenado:
    distingue dos cachés con **los mismos nombres y tamaños** y contenido
    distinto. Es lo que impide que un respaldo viejo se reutilice como si
    cubriera el caché actual.
    """

    destino: pathlib.Path
    manifiesto: pathlib.Path
    archivos: int
    bytes: int
    huella: str
    huella_contenido: str

    def cubre(self, inventario: InventarioCache) -> bool:
        """True si este respaldo corresponde a ese inventario (nombres y tamaños)."""
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


def _hash_de_archivo(ruta: pathlib.Path) -> tuple[int, str]:
    """``(bytes, sha256)`` leyendo el archivo del DISCO (sin caché en memoria)."""
    digest = hashlib.sha256()
    total = 0
    with ruta.open("rb") as fh:
        while True:
            bloque = fh.read(_BLOQUE)
            if not bloque:
                break
            digest.update(bloque)
            total += len(bloque)
    return (total, digest.hexdigest())


def _escribir_json_atomico(ruta: pathlib.Path, carga: dict[str, Any]) -> None:
    """Escribe *carga* como JSON con temporal EXCLUSIVO + rename, y lo relee.

    Se usa para todo metadato que Sky-Claw escribe (manifiesto del respaldo y
    evidencia de progreso): el temporal tiene nombre impredecible y se crea con
    ``O_CREAT|O_EXCL``, así que nada preexistente en ese camino —un enlace, por
    ejemplo— redirige la escritura fuera de ``ruta.parent``.
    """
    tmp = _ruta_temporal(ruta)
    _escribir_temporal_exclusivo(tmp, json.dumps(carga, ensure_ascii=False, indent=2).encode("utf-8"))
    try:
        os.replace(tmp, ruta)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise GrassCacheSafetyError(
            "metadato_no_escrito", f"No se pudo sustituir {ruta} con el temporal: {exc}"
        ) from exc
    with contextlib.suppress(OSError):
        tmp.unlink(missing_ok=True)  # no-op si el rename se lo llevó; limpia si fue un no-op
    _fsync_directorio(ruta.parent)
    if _leer_manifiesto(ruta) is None:
        raise GrassCacheSafetyError(
            "metadato_no_verificado", f"{ruta} no se pudo releer como JSON después de escribirlo."
        )


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


def _digests_del_cache(grass_dir: pathlib.Path) -> dict[str, tuple[int, str]]:
    """``nombre -> (bytes, sha256)`` de los artefactos REALES del caché.

    Es la lectura de contenido completa del origen, y **no** la paga sólo
    ``rebuild``: también la usan la verificación del respaldo, ``archivar_progreso``
    (la pasada de salida, sólo si hay progreso que archivar y raíz de estado) y
    ``recuperar_progreso`` al comprobar que la evidencia es de *este* caché. Es
    la única forma de afirmar que un respaldo o un progreso cubren *este*
    contenido y no uno con los mismos tamaños.
    """
    digests: dict[str, tuple[int, str]] = {}
    try:
        candidatos = sorted(grass_dir.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, NotADirectoryError):
        return digests
    except OSError as exc:
        raise GrassCacheSafetyError("cache_ilegible", f"No se pudo leer {grass_dir}: {exc}") from exc
    for archivo in candidatos:
        if archivo.suffix.lower() not in EXTENSIONES_CACHE:
            continue
        # El enlace se clasifica PRIMERO: `is_file()` sigue el enlace y devuelve
        # False para un enlace a directorio o roto, así que preguntarlo antes
        # descartaba la entrada en silencio — y el hash dejaba de ver un `.cgid`
        # que el borrado de NGIO sí atraviesa (fail-open, F5).
        _rechazar_enlace(archivo, "cache_con_enlaces")
        try:
            info = archivo.stat()
        except OSError as exc:
            raise GrassCacheSafetyError("cache_ilegible", f"No se pudo leer {archivo}: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            # Mismo criterio que `inventariar_cache`: un directorio con nombre de
            # caché no es caché, pero se dice (no se descarta en silencio).
            logger.warning("Se ignora %s: no es un archivo regular.", archivo)
            continue
        try:
            digests[archivo.name] = _hash_de_archivo(archivo)
        except OSError as exc:
            raise GrassCacheSafetyError("cache_ilegible", f"No se pudo leer {archivo}: {exc}") from exc
    return digests


def _huella_de_contenido(digests: dict[str, tuple[int, str]]) -> str:
    digest = hashlib.sha256()
    for nombre in sorted(digests):
        tamano, sha = digests[nombre]
        digest.update(f"{nombre}\x00{tamano}\x00{sha}\n".encode())
    return digest.hexdigest()


def _entradas_del_manifiesto(manifiesto: dict[str, Any]) -> dict[str, tuple[int, str]] | None:
    """``nombre -> (bytes, sha256)`` del manifiesto, o ``None`` si no es válido."""
    if manifiesto.get("version") != _VERSION_MANIFIESTO:
        return None
    archivos = manifiesto.get("archivos")
    if not isinstance(archivos, list):
        return None
    entradas: dict[str, tuple[int, str]] = {}
    for entrada in archivos:
        if not isinstance(entrada, dict):
            return None
        nombre = entrada.get("nombre")
        tamano = entrada.get("bytes")
        sha = entrada.get("sha256")
        if not isinstance(nombre, str) or not isinstance(tamano, int) or not isinstance(sha, str):
            return None
        entradas[nombre] = (tamano, sha)
    return entradas


def _problemas_de_independencia(
    origen_dir: pathlib.Path, destino: pathlib.Path, nombres: Iterable[str]
) -> list[tuple[str, str]]:
    """Enlaces o alias del ORIGEN dentro de los artefactos del respaldo.

    **Qué problema cierra.** Los hashes de verificación leen el archivo del
    respaldo *siguiendo el camino*: si esa entrada es un symlink, un junction o
    un **hardlink** al original, los bytes coinciden —son los mismos— y el
    respaldo "verifica". Pero no es un respaldo: cuando NGIO borre el original,
    la entrada del respaldo desaparece con él (mismo inode) o queda apuntando a
    la nada. El respaldo autorizaba así un borrado sin copia recuperable.

    **Cómo lo decide.** Por cada artefacto declarado: ``link_kind_or_raise``
    (la primitiva del repo: symlink/junction) y, si es un archivo regular,
    comparación de identidad física ``(st_dev, st_ino)`` contra el origen. Un
    hardlink comparte inode con el original; una copia independiente, no.
    Devuelve ``[(codigo, mensaje), …]``: el llamador decide si lo trata como
    "no reusar" (preparación, con recopia posible) o como fallo duro (antes de
    autorizar el borrado).
    """
    problemas: list[tuple[str, str]] = []
    for nombre in sorted(nombres):
        ruta = destino / nombre
        try:
            tipo = link_kind_or_raise(ruta)
        except OSError as exc:
            problemas.append(("respaldo_alterado", f"No se pudo clasificar {ruta}: {exc}"))
            continue
        if tipo is not None:
            problemas.append(
                (
                    "respaldo_enlazado",
                    f"{ruta} del respaldo es un enlace ({tipo}) que redirige al origen: no es una copia.",
                )
            )
            continue
        try:
            identidad_copia = ruta.lstat()
            identidad_origen = (origen_dir / nombre).lstat()
        except OSError as exc:
            problemas.append(("respaldo_alterado", f"No se pudo inspeccionar {ruta}: {exc}"))
            continue
        if (
            identidad_copia.st_ino
            and identidad_copia.st_ino == identidad_origen.st_ino
            and identidad_copia.st_dev == identidad_origen.st_dev
        ):
            problemas.append(
                (
                    "respaldo_aliaseado",
                    f"{ruta} del respaldo es el MISMO archivo que {origen_dir / nombre} (hardlink): "
                    "no sobrevive al borrado que el respaldo autoriza.",
                )
            )
    return problemas


def _respaldo_existente_verifica(
    destino: pathlib.Path,
    inventario: InventarioCache,
    origen: dict[str, tuple[int, str]],
    origen_dir: pathlib.Path,
) -> RespaldoVerificado | None:
    """Reusa un respaldo previo sólo si su CONTENIDO cubre el caché actual.

    Tres igualdades, y las tres por hash de contenido, no por metadatos:

    1. lo que el manifiesto declara == lo que hay en el origen **ahora**;
    2. lo que hay en el destino **ahora** == lo que el manifiesto declara;
    3. los conteos/bytes del inventario coinciden con el manifiesto.

    Esto es lo que cierra los dos casos que sólo mirando tamaños pasaban:
    origen ``AAAA`` → ``BBBB`` con los mismos cuatro bytes (mismo nombre y
    tamaño, contenido distinto), y destino alterado a ``XXXX`` conservando el
    tamaño. En cualquiera de los dos, el llamador recopia desde el origen.
    """
    ruta_manifiesto = destino / "manifiesto.json"
    if path_present(ruta_manifiesto):
        try:
            _rechazar_enlace(ruta_manifiesto, "respaldo_enlazado")
        except GrassCacheSafetyError:
            logger.warning("El manifiesto %s del respaldo es un enlace: no se reutiliza.", ruta_manifiesto)
            return None
    manifiesto = _leer_manifiesto(ruta_manifiesto)
    if manifiesto is None:
        return None
    declarado = _entradas_del_manifiesto(manifiesto)
    if declarado is None:
        return None
    if manifiesto.get("huella") != inventario.huella:
        return None
    if declarado != origen:
        return None
    total = sum(tamano for tamano, _ in origen.values())
    if len(origen) != inventario.archivos_cache or total != inventario.bytes_cache:
        return None
    # Un respaldo que enlaza o aliasea el origen no es un respaldo. Acá SÍ hay
    # reparación: no se reusa, y `respaldar_cache` lo aparta y recopia.
    problemas = _problemas_de_independencia(origen_dir, destino, origen)
    if problemas:
        logger.warning("El respaldo %s no es independiente del origen (%s): no se reutiliza.", destino, problemas[0][1])
        return None
    for nombre, (tamano, sha_esperado) in origen.items():
        ruta = destino / nombre
        try:
            if ruta.stat().st_size != tamano:
                return None
            _, sha_destino = _hash_de_archivo(ruta)
        except OSError:
            return None
        if sha_destino != sha_esperado:
            # El respaldo fue alterado en el destino con el mismo tamaño.
            logger.warning("Respaldo %s alterado (%s): no se reutiliza.", destino, nombre)
            return None
    return RespaldoVerificado(
        destino=destino,
        manifiesto=destino / "manifiesto.json",
        archivos=len(origen),
        bytes=total,
        huella=inventario.huella,
        huella_contenido=_huella_de_contenido(origen),
    )


def _ruta_fisica_normalizada(ruta: pathlib.Path) -> str:
    """Ruta FÍSICA y normalizada: resuelve enlaces y unifica mayúsculas.

    ``realpath`` resuelve la cadena de enlaces del sistema de archivos (no sólo
    el último componente) y ``normcase`` pliega mayúsculas en Windows: dos rutas
    que nombran el mismo directorio colapsan al mismo string.
    """
    return os.path.normcase(os.path.realpath(ruta))


def _contiene(raiz: str, otra: str) -> bool:
    """¿*otra* es *raiz* o vive dentro de *raiz*? (unidades distintas ⇒ no)."""
    try:
        return os.path.commonpath([raiz, otra]) == raiz
    except ValueError:
        return False  # rutas en unidades distintas: no pueden solaparse


def _exigir_respaldo_disjunto(grass_dir: pathlib.Path, destino: pathlib.Path) -> None:
    """El respaldo no puede ser el caché, ni estar dentro, ni contenerlo.

    **Qué problema cierra.** La rotación de un respaldo inválido hace
    ``os.replace(destino, <destino>.invalido-…)``. Si ``destino`` es el propio
    ``Grass/`` —o un ancestro suyo—, esa cuarentena **mueve el caché original**
    antes de copiar nada: el respaldo no existe y el origen ya no está en su
    lugar. Si ``destino`` vive DENTRO del caché, la copia se escribiría dentro de
    lo que se está respaldando (y el inventario la vería en la corrida
    siguiente).

    **Cómo lo decide.** Comparación física (``realpath`` + ``normcase``), no
    textual: ``<mo2>/overwrite/Grass`` y ``<mo2>/overwrite/./Grass/../Grass``
    son el mismo directorio, y un enlace intermedio no puede esquivar el chequeo.
    Unidades distintas no pueden solaparse.
    """
    origen = _ruta_fisica_normalizada(grass_dir)
    respaldo = _ruta_fisica_normalizada(destino)
    if origen == respaldo or _contiene(origen, respaldo) or _contiene(respaldo, origen):
        raise GrassCacheSafetyError(
            "respaldo_solapa_el_cache",
            f"El respaldo {destino} se solapa con el caché {grass_dir} (mismo directorio, dentro o "
            "por encima). Un respaldo así no copia: la rotación movería el caché original. Elegí una "
            "raíz de estado fuera del árbol del juego/MO2.",
        )


def _nombre_de_cuarentena(destino: pathlib.Path) -> pathlib.Path:
    """Nombre ÚNICO para apartar un respaldo inválido (nunca pisa a otro).

    ``int(time.time())`` colisiona entre dos corridas del mismo segundo, y el
    ``os.replace`` siguiente apuntaba al mismo directorio: o fallaba (silenciado)
    dejando el ``mkdir`` que revienta, o pisaba una cuarentena previa. El sufijo
    aleatorio hace la colisión despreciable y el chequeo ``lexists`` la vuelve
    imposible dentro de este proceso.
    """
    base = f"{destino.name}.invalido-{int(time.time())}"
    for intento in range(16):
        candidato = destino.with_name(
            f"{base}-{secrets.token_hex(4)}" if intento == 0 else f"{base}-{secrets.token_hex(8)}"
        )
        if not os.path.lexists(candidato):
            return candidato
    raise GrassCacheSafetyError(
        "respaldo_no_apartado",
        f"No se encontró un nombre libre para apartar el respaldo inválido en {destino.parent}.",
    )


def respaldar_cache(
    *,
    grass_dir: pathlib.Path,
    destino: pathlib.Path,
    inventario: InventarioCache,
) -> RespaldoVerificado:
    """Copia el caché a *destino*, verifica **contenido** en ambos lados y manifiesta.

    Verificación real, en tres pasos:

    1. se lee el origen completo y se calculan sus sha256;
    2. cada archivo copiado se **relee del destino** y su sha256 tiene que
       coincidir con el del origen (un corte a mitad de copia deja tamaño o
       hash distinto ⇒ no hay respaldo verificado ⇒ ``rebuild`` no arranca);
    3. el manifiesto se relee del disco y se contrasta contra el origen.

    Idempotente: si ya existe un respaldo cuyo contenido verifica contra el
    origen actual, se reusa sin recopiar. Un respaldo que no verifica (de otra
    corrida, de otro contenido, o alterado) se **aparta** con nombre explícito;
    nunca se pisa en silencio ni se borra.
    """
    _validar_raiz_fisica(grass_dir)
    # Antes de CUALQUIER replace: el respaldo no puede solaparse con el caché.
    _exigir_respaldo_disjunto(grass_dir, destino)
    _validar_raiz_fisica(
        destino,
        codigo_enlace="respaldo_raiz_enlazada",
        codigo_no_absoluta="respaldo_raiz_no_absoluta",
        codigo_ilegible="respaldo_raiz_ilegible",
        que="del respaldo",
    )
    if path_present(grass_dir):
        try:
            exigir_arbol_copiable_sin_reparse(grass_dir)
        except OSError as exc:
            raise GrassCacheSafetyError(
                "cache_con_enlaces",
                f"El árbol del caché contiene una redirección y no se puede respaldar: {exc}",
            ) from exc

    origen = _digests_del_cache(grass_dir)
    total = sum(tamano for tamano, _ in origen.values())
    if len(origen) != inventario.archivos_cache or total != inventario.bytes_cache:
        raise GrassCacheSafetyError(
            "respaldo_incompleto",
            f"El caché tiene {len(origen)} archivos/{total} bytes, pero el inventario declara "
            f"{inventario.archivos_cache}/{inventario.bytes_cache}: cambió entre el inventario y el respaldo.",
        )

    # Conteo y bytes no distinguen un RENOMBRADO (A.cgid → B.cgid conserva las dos
    # cifras): el respaldo se escribiría con los bytes correctos pero el
    # manifiesto identificaría el conjunto del inventario, que ya no es el suyo.
    # Misma protección que `archivar_progreso` (huella ``(nombre, tamaño)``).
    huella_del_origen = huella_del_cache((nombre, tamano) for nombre, (tamano, _) in origen.items())
    if huella_del_origen != inventario.huella:
        raise GrassCacheSafetyError(
            "respaldo_de_otro_conjunto",
            f"El conjunto (nombre, tamaño) del caché cambió entre el inventario ({inventario.huella}) y el "
            f"respaldo ({huella_del_origen}): no se escribe un respaldo con identidad ajena.",
        )

    reusado = _respaldo_existente_verifica(destino, inventario, origen, grass_dir) if path_present(destino) else None
    if reusado is not None:
        logger.info("Respaldo de grass verificado por contenido en %s; se reusa.", destino)
        return reusado

    try:
        destino.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        invalido = _nombre_de_cuarentena(destino)
        try:
            os.replace(destino, invalido)
        except OSError as exc:
            # Silenciarlo dejaba el `mkdir` siguiente fallando con un error que
            # no nombra la causa real (y una cuarentena a medias).
            raise GrassCacheSafetyError(
                "respaldo_no_apartado",
                f"No se pudo apartar el respaldo inválido {destino} a {invalido}: {exc}. "
                "No se copia encima de un respaldo que no verifica.",
            ) from exc
        logger.warning("El respaldo %s no verificaba: se apartó a %s (no se borra).", destino, invalido)
        destino.mkdir(parents=True, exist_ok=False)

    archivos: list[dict[str, Any]] = []
    copiados = 0
    for nombre, (tamano_origen, sha_origen) in origen.items():
        copia = destino / nombre
        bytes_copiados, hash_copia = _copiar_con_hash(grass_dir / nombre, copia)
        if bytes_copiados != tamano_origen:
            raise GrassCacheSafetyError(
                "respaldo_no_verificado",
                f"El respaldo de {nombre} copió {bytes_copiados} bytes de {tamano_origen}.",
            )
        _, hash_releido = _hash_de_archivo(copia)
        if hash_releido != sha_origen or hash_copia != sha_origen:
            raise GrassCacheSafetyError(
                "respaldo_no_verificado",
                f"El respaldo de {nombre} no coincide con el origen (copia truncada o alterada).",
            )
        archivos.append({"nombre": nombre, "bytes": tamano_origen, "sha256": sha_origen})
        copiados += bytes_copiados

    if copiados != total:
        raise GrassCacheSafetyError(
            "respaldo_incompleto",
            f"El respaldo copió {copiados} bytes de {total} declarados por el origen.",
        )

    manifiesto = {
        "version": _VERSION_MANIFIESTO,
        "huella": inventario.huella,
        "huella_contenido": _huella_de_contenido(origen),
        "archivos": archivos,
        "bytes": total,
        "creado": time.time(),
        "origen": str(grass_dir),
        "inventario": inventario.a_dict(),
    }
    ruta_manifiesto = destino / "manifiesto.json"
    _escribir_json_atomico(ruta_manifiesto, manifiesto)

    # Releer del disco: el respaldo vale por lo que quedó escrito, no por el dict.
    releido = _leer_manifiesto(ruta_manifiesto)
    entradas_releidas = _entradas_del_manifiesto(releido or {})
    if entradas_releidas != origen:
        raise GrassCacheSafetyError(
            "respaldo_no_verificado", f"El manifiesto {ruta_manifiesto} no coincide con el origen tras releerlo."
        )
    return RespaldoVerificado(
        destino=destino,
        manifiesto=ruta_manifiesto,
        archivos=len(origen),
        bytes=total,
        huella=inventario.huella,
        huella_contenido=_huella_de_contenido(origen),
    )


def verificar_respaldo_para_borrado(*, grass_dir: pathlib.Path, respaldo: RespaldoVerificado) -> None:
    """Verifica el respaldo contra el caché ACTUAL justo antes de autorizar el borrado.

    Es la última comprobación antes de escribir un flag vacío (que dispara el
    ``remove()`` de NGIO). Vuelve a leer el origen y el destino: si el caché
    cambió después del respaldo, o si el respaldo fue alterado, ``rebuild`` no
    arranca. El manifiesto histórico **no** alcanza por sí solo.
    """
    _validar_raiz_fisica(grass_dir)
    _validar_raiz_fisica(
        respaldo.destino,
        codigo_enlace="respaldo_raiz_enlazada",
        codigo_no_absoluta="respaldo_raiz_no_absoluta",
        codigo_ilegible="respaldo_raiz_ilegible",
        que="del respaldo",
    )
    if path_present(respaldo.manifiesto):
        _rechazar_enlace(respaldo.manifiesto, "respaldo_enlazado")
    manifiesto = _leer_manifiesto(respaldo.manifiesto)
    declarado = _entradas_del_manifiesto(manifiesto or {})
    if declarado is None:
        raise GrassCacheSafetyError(
            "respaldo_no_verificado", f"El manifiesto {respaldo.manifiesto} no se pudo interpretar."
        )
    actual = _digests_del_cache(grass_dir)
    if actual != declarado:
        raise GrassCacheSafetyError(
            "respaldo_desactualizado",
            "El caché cambió después del respaldo: el respaldo ya no cubre el contenido actual. "
            "Se niega el borrado (volvé a correr el ritual para respaldar el estado vigente).",
        )
    problemas = _problemas_de_independencia(grass_dir, respaldo.destino, declarado)
    if problemas:
        codigo, mensaje = problemas[0]
        raise GrassCacheSafetyError(
            codigo,
            f"{mensaje} Se niega el borrado del caché sin una copia independiente del origen.",
        )
    for nombre, (tamano, sha_esperado) in declarado.items():
        ruta = respaldo.destino / nombre
        try:
            if ruta.stat().st_size != tamano or _hash_de_archivo(ruta)[1] != sha_esperado:
                raise GrassCacheSafetyError(
                    "respaldo_alterado",
                    f"El archivo {ruta} del respaldo no coincide con el manifiesto: no se autoriza el borrado.",
                )
        except OSError as exc:
            raise GrassCacheSafetyError(
                "respaldo_alterado", f"No se pudo verificar {ruta} del respaldo: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Evidencia de progreso (reanudación)
# ---------------------------------------------------------------------------


def _dir_progreso(raiz_estado: pathlib.Path) -> pathlib.Path:
    return raiz_estado / "progreso"


def _ambito_de_progreso(grass_dir: pathlib.Path, perfil: str) -> str:
    """Identidad del ORIGEN del progreso: instalación (camino físico) + perfil.

    La huella del caché identifica el *contenido*, y eso no alcanza para saber a
    quién pertenece el progreso archivado: dos instalaciones MO2 con el mismo
    ``Grass/`` (mismo nombre, tamaño y bytes) producen la misma huella, y dos
    perfiles de la MISMA instalación comparten ``overwrite/Grass``. Sin el
    ámbito, la evidencia de una reanudaría el precache de la otra, y NGIO
    saltearía celdas que ese run todavía no generó.

    ``normcase`` + ``realpath``: el mismo directorio es el mismo ámbito aunque
    cambie el uso de mayúsculas (Windows) o haya un componente redundante.
    """
    identidad = os.path.normcase(os.path.realpath(grass_dir)) + "\x00" + perfil
    return hashlib.sha256(identidad.encode("utf-8")).hexdigest()[:16]


def _ruta_progreso(raiz_estado: pathlib.Path, huella: str, ambito: str) -> pathlib.Path:
    """``<estado>/progreso/<huella>-<ambito>.json``: contenido **y** origen."""
    return _dir_progreso(raiz_estado) / f"{huella}-{ambito}.json"


def _validar_progreso_fisico(directorio: pathlib.Path, *, que: str) -> None:
    """Exige que el archivo de evidencia cuelgue de una cadena de directorios REALES.

    **Qué problema cierra.** Validar ``raiz_estado`` no alcanza: el destino real
    es ``<raiz_estado>/<progreso>/<huella>.json`` y ``<progreso>`` es OTRO
    componente de la ruta de escritura. Un enlace ahí (``raiz_estado`` real,
    ``raiz_estado/progreso`` → árbol externo) pasaba los guards de la raíz y el
    JSON de evidencia se escribía fuera del árbol administrado, sobrescribiendo
    el archivo de otra aplicación. Lo mismo del lado de la lectura: la evidencia
    se recuperaba desde afuera.

    **Cómo lo decide.** Se reutiliza :func:`_validar_raiz_fisica`, que ancla en
    la raíz del volumen y hace ``lstat`` de CADA componente sin seguir enlaces
    (symlinks y junctions de Windows incluidos), así que cubre también un
    ancestro intermedio enlazado. Un componente inexistente no puede ser un
    enlace y el recorrido se corta ahí. Se valida el DIRECTORIO del destino
    (todos los componentes de la cadena son directorios) y el archivo en sí se
    clasifica aparte con :func:`_rechazar_enlace`.
    """
    _validar_raiz_fisica(
        directorio,
        codigo_enlace="progreso_raiz_enlazada",
        codigo_no_absoluta="progreso_raiz_no_absoluta",
        codigo_ilegible="progreso_raiz_ilegible",
        que=que,
    )


def archivar_progreso(
    raiz_estado: pathlib.Path,
    inventario: InventarioCache,
    contenido: bytes,
    *,
    grass_dir: pathlib.Path,
    perfil: str = "",
) -> pathlib.Path:
    """Guarda los BYTES del flag atados al CONTENIDO del caché de ESE momento.

    ``base64`` + ``sha256``: el contenido no se interpreta como texto, así que
    restaurarlo devuelve los mismos bytes aunque el flag tenga una codificación
    inesperada.

    Además se guarda ``huella_contenido`` (sha256 de nombre+tamaño+sha256 de
    cada artefacto). La huella barata ``(nombre, tamaño)`` NO distingue un
    ``.cgid`` que cambió de bytes conservando el tamaño, y reanudar con el
    progreso de otro contenido haría que NGIO saltara celdas que nunca se
    escribieron. Por eso ``grass_dir`` es obligatorio: sin saber a qué caché
    pertenece, la evidencia no es verificable y no se archiva.
    """
    _validar_raiz_fisica(
        raiz_estado,
        codigo_enlace="estado_raiz_enlazada",
        codigo_no_absoluta="estado_raiz_no_absoluta",
        codigo_ilegible="estado_raiz_ilegible",
        que="del estado de Sky-Claw",
    )
    digests = _digests_del_cache(grass_dir)
    # Conteo y bytes totales NO identifican el conjunto: renombrar ``A.cgid`` a
    # ``B.cgid`` entre el inventario y el hash deja las dos cifras iguales, y la
    # evidencia quedaría archivada bajo la clave vieja con el contenido nuevo —
    # un reintento con el nombre nuevo no la encontraría. La huella barata
    # ``(nombre, tamaño)`` sí distingue ese caso, y es la MISMA que calcula
    # ``inventariar_cache``.
    huella_de_los_digests = huella_del_cache((nombre, tamano) for nombre, (tamano, _) in digests.items())
    if (
        len(digests) != inventario.archivos_cache
        or sum(tamano for tamano, _ in digests.values()) != inventario.bytes_cache
        or huella_de_los_digests != inventario.huella
    ):
        raise GrassCacheSafetyError(
            "progreso_no_coincide_con_el_cache",
            f"El progreso es de un inventario ({inventario.archivos_cache} archivos/"
            f"{inventario.bytes_cache} bytes) que ya no coincide con {grass_dir} "
            f"({len(digests)} archivos): no se archiva evidencia que no corresponde.",
        )
    ambito = _ambito_de_progreso(grass_dir, perfil)
    destino = _ruta_progreso(raiz_estado, inventario.huella, ambito)
    # B10: la cadena física del DESTINO, no sólo ``raiz_estado``. El subdirectorio
    # de progreso es parte de la ruta de escritura: sin este guard, un enlace en
    # ``<estado>/progreso`` deja el JSON de evidencia fuera del árbol administrado.
    _validar_progreso_fisico(destino.parent, que="del progreso archivado de Sky-Claw")
    destino.parent.mkdir(parents=True, exist_ok=True)
    # Y se revalida con el subdirectorio ya creado: la escritura atómica elige
    # el nombre final y su directorio real recién acá.
    _validar_progreso_fisico(destino.parent, que="del progreso archivado de Sky-Claw")
    carga = {
        "version": _VERSION_MANIFIESTO,
        "huella": inventario.huella,
        "ambito": ambito,
        "origen": os.path.realpath(grass_dir),
        "perfil": perfil,
        "inventario": inventario.a_dict(),
        "huella_contenido": _huella_de_contenido(digests),
        "contenido_b64": base64.b64encode(contenido).decode("ascii"),
        "contenido_sha256": hashlib.sha256(contenido).hexdigest(),
        "contenido_bytes": len(contenido),
        "archivado": time.time(),
    }
    _escribir_json_atomico(destino, carga)
    return destino


def recuperar_progreso(
    raiz_estado: pathlib.Path | None,
    inventario: InventarioCache,
    *,
    grass_dir: pathlib.Path,
    perfil: str = "",
) -> bytes | None:
    """Progreso archivado para ESTE caché, o ``None``.

    Se exigen dos identidades, no una:

    1. la huella ``(nombre, tamaño)`` del inventario (barata);
    2. la huella de CONTENIDO del caché actual — un ``.cgid`` que cambió de
       bytes conservando el tamaño invalida la evidencia aunque (1) coincida.
       En ese caso se levanta ``progreso_de_otro_cache`` en vez de restaurar:
       reanudar con el progreso de otro contenido haría que NGIO saltee celdas
       que nunca se escribieron.

    El round-trip del contenido también se valida (sha256 archivado), así un
    archivo de evidencia corrupto no se restaura a medias.
    """
    if raiz_estado is None:
        return None
    _validar_raiz_fisica(
        raiz_estado,
        codigo_enlace="estado_raiz_enlazada",
        codigo_no_absoluta="estado_raiz_no_absoluta",
        codigo_ilegible="estado_raiz_ilegible",
        que="del estado de Sky-Claw",
    )
    ambito = _ambito_de_progreso(grass_dir, perfil)
    ruta = _ruta_progreso(raiz_estado, inventario.huella, ambito)
    # B10: tampoco se LEE evidencia que viva fuera del árbol administrado.
    _validar_progreso_fisico(ruta.parent, que="del progreso archivado de Sky-Claw")
    # Si NO hay evidencia para este origen pero SÍ para este contenido en otro,
    # se dice explícitamente: reanudar con el progreso de otra instalación/perfil
    # saltearía celdas que este run todavía no generó. (La exploración va DESPUÉS
    # de validar la cadena física: listar también es leer.)
    if not path_present(ruta):
        for otra in sorted(_dir_progreso(raiz_estado).glob(f"{inventario.huella}-*.json")):
            if otra != ruta:
                raise GrassCacheSafetyError(
                    "progreso_de_otra_instalacion",
                    f"El progreso archivado de este caché pertenece a otro origen ({otra.name}): "
                    f"no es de la instalación/perfil ({perfil or 'sin perfil'}) que está reanudando. "
                    "No se restaura evidencia de otro origen.",
                )
    if path_present(ruta):
        _rechazar_enlace(ruta, "progreso_es_enlace")
    carga = _leer_manifiesto(ruta)
    if carga is None or carga.get("version") != _VERSION_MANIFIESTO or carga.get("huella") != inventario.huella:
        return None
    if carga.get("ambito") != ambito:
        raise GrassCacheSafetyError(
            "progreso_de_otra_instalacion",
            f"El manifiesto {ruta} declara otro ámbito ({carga.get('ambito')!r}) que el de este "
            f"origen ({ambito!r}): la evidencia no se restaura.",
        )
    b64 = carga.get("contenido_b64")
    sha_esperado = carga.get("contenido_sha256")
    if not isinstance(b64, str) or not isinstance(sha_esperado, str):
        return None
    try:
        contenido = base64.b64decode(b64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    if hashlib.sha256(contenido).hexdigest() != sha_esperado:
        logger.warning("La evidencia de progreso %s no sobrevivió el round-trip; no se restaura.", ruta)
        return None
    archivada = carga.get("huella_contenido")
    if not isinstance(archivada, str):
        # Evidencia sin huella de contenido (formato viejo): no se puede probar
        # a qué caché pertenece, así que no habilita una reanudación.
        logger.warning("La evidencia de progreso %s no declara el contenido del caché; no se restaura.", ruta)
        return None
    actual = _huella_de_contenido(_digests_del_cache(grass_dir))
    if actual != archivada:
        raise GrassCacheSafetyError(
            "progreso_de_otro_cache",
            f"La evidencia de progreso {ruta} pertenece a otro contenido del caché "
            "(mismos nombres y tamaños, otros bytes): reanudar con ella haría que NGIO "
            "saltee celdas que nunca se escribieron. Se niega la reanudación.",
        )
    # El "¿NGIO vería progreso?" del contenido ARCHIVADO es el mismo predicado que
    # el del flag vivo: `str.strip()` recorta tab y form feed (que NGIO NO
    # recorta) y descartaría evidencia válida, empujando al operador a un
    # `rebuild` destructivo. Se reusa la réplica, no una segunda definición.
    return contenido if _ngio_tiene_progreso(contenido) else None


# ---------------------------------------------------------------------------
# Neutralización del flag (verificable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NeutralizacionFlag:
    """Resultado de archivar + borrar el flag. Nunca se pierde un fallo.

    ``borrado`` y ``progreso_perdido`` son ortogonales y los dos importan:

    - ``borrado=False`` ⇒ el flag SIGUE en disco y el juego del usuario puede
      arrancar en modo precache (``flag_residual`` en el resultado del run).
    - ``progreso_perdido=True`` ⇒ el flag se borró, pero su contenido no se pudo
      archivar: la única evidencia que permitía reanudar SIN destruir el caché
      se perdió. Borrar era obligatorio; declararlo "limpio" no lo es.

    ``alternativo_residual`` es el tercer eje: el flag ALTERNATIVO
    (``PrecacheGrass.txt.txt``) no es nuestro, así que no se borra —pertenece a
    otro dueño y destruirlo sería cambiar un borrado de caché por otro—, pero
    mientras exista NGIO puede abrirlo como flag de progreso cuando el primario
    falte: la limpieza **no** es completa y el resultado tiene que decirlo.

    ``limpio`` exige las tres cosas: que no quede flag, que no se haya perdido
    evidencia y que no quede un alternativo residual. Un ``unlink`` que funciona
    con un archivado que falla **no** es limpieza exitosa.
    """

    borrado: bool
    archivado: bool
    errores: tuple[str, ...] = ()
    progreso_perdido: bool = False
    alternativo_residual: bool = False

    @property
    def limpio(self) -> bool:
        return self.borrado and not self.progreso_perdido and not self.alternativo_residual


def neutralizar_flag(
    *,
    flag_path: pathlib.Path,
    grass_dir: pathlib.Path,
    raiz_estado: pathlib.Path | None,
    perfil: str = "",
    archivar: bool = True,
) -> NeutralizacionFlag:
    """Archiva el progreso (si lo hay) y borra el flag. Nunca propaga errores.

    ``archivar=False`` (P1, H2/H5): NO se archiva el progreso porque la terminalidad del juego no
    está demostrada y el caché puede seguir escribiéndose; archivar esa foto como evidencia de
    reanudación afirmaría una estabilidad que no existe. El **borrado sí ocurre**: dejar el flag
    mete al juego del usuario en modo precache. No se declara ``progreso_perdido`` —no se perdió
    nada: se decidió no archivar— y queda una nota en ``errores``.

    El borrado es obligatorio: un flag residual haría que el juego del usuario
    arranque en modo precache. Por eso el resultado es explícito —un
    ``unlink`` que falla NO es limpieza exitosa— y el llamador lo publica en el
    resultado del run.

    **Fail-closed en la inspección.** Si no se puede leer o clasificar el flag
    (permiso, bloqueo de AV/indexer, enlace), no se borra nada: "no sé qué es"
    no autoriza un ``unlink``. El caso queda en ``errores`` y el flag residual
    se reporta.

    **La pérdida de progreso se declara.** Si hay progreso y no se pudo
    archivar, el flag igual se borra (la seguridad del juego manda), pero
    ``progreso_perdido=True`` saca el resultado de "limpieza exitosa": lo único
    que permitía reanudar sin destruir el caché ya no existe.
    """
    errores: list[str] = []
    archivado = False

    try:
        presente = path_present(flag_path)
    except OSError as exc:
        # Sin poder verlo no se sabe qué hay ahí: no se borra.
        return NeutralizacionFlag(
            borrado=False,
            archivado=False,
            errores=(f"No se pudo inspeccionar {flag_path}: {exc}",),
        )

    progreso: bytes | None = None
    if presente:
        try:
            _, progreso = _leer_flag(flag_path)
        except GrassCacheSafetyError as exc:
            # Enlace, directorio o ilegible: no es el flag de NGIO (o no se
            # puede afirmar que lo sea) ⇒ no se borra.
            return NeutralizacionFlag(
                borrado=False,
                archivado=False,
                errores=(f"No se pudo clasificar {flag_path}: {exc}",),
            )

    if presente and progreso is not None:
        if not archivar:
            # H2/H5: no se archiva una foto de un caché que puede seguir escribiéndose.
            errores.append(
                "Archivado del progreso OMITIDO: la terminalidad del juego no está demostrada y el "
                "caché puede seguir escribiéndose. El flag igual se borra."
            )
        elif raiz_estado is None:
            errores.append(
                f"No hay raíz de estado donde archivar el progreso de {flag_path}: "
                "reanudar este caché ya no será posible"
            )
        else:
            try:
                inventario = inventariar_cache(grass_dir=grass_dir, flag_path=flag_path)
                archivar_progreso(raiz_estado, inventario, progreso, grass_dir=grass_dir, perfil=perfil)
                archivado = True
            except (GrassCacheSafetyError, OSError) as exc:
                errores.append(f"No se pudo archivar el progreso: {exc}")

    try:
        # Un enlace no es el flag de NGIO: se informa y no se borra (podría ser
        # un archivo del usuario apuntado desde otro lado). Y si la
        # clasificación falla, tampoco: sin saber qué es, no se toca.
        tipo = link_kind_or_raise(flag_path) if presente else None
    except OSError as exc:
        errores.append(f"No se pudo clasificar {flag_path} antes de borrarlo: {exc}")
        return NeutralizacionFlag(borrado=False, archivado=archivado, errores=tuple(errores))

    if tipo is not None:
        errores.append(f"{flag_path} es un enlace ({tipo}); no se borra")
        return NeutralizacionFlag(borrado=False, archivado=archivado, errores=tuple(errores))

    try:
        flag_path.unlink(missing_ok=True)
    except OSError as exc:
        errores.append(f"No se pudo eliminar {flag_path}: {exc}")

    try:
        borrado = not path_present(flag_path)
    except OSError:
        borrado = False
    if not borrado and not any("No se pudo eliminar" in e for e in errores):
        errores.append(f"{flag_path} sigue presente después del borrado")

    progreso_perdido = bool(archivar and borrado and progreso is not None and not archivado)
    if progreso_perdido:
        errores.append("Se borró el flag sin archivar su progreso: la evidencia de reanudación de este caché se perdió")
    return NeutralizacionFlag(
        borrado=borrado,
        archivado=archivado,
        errores=tuple(errores),
        progreso_perdido=progreso_perdido,
    )


# ---------------------------------------------------------------------------
# Decisión y aplicación del plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanArranque:
    """Qué hacer con el flag, ya decidido y con el respaldo verificado adentro."""

    modo: GrassCacheMode
    accion: AccionFlag
    contenido: bytes
    inventario: InventarioCache
    respaldo: RespaldoVerificado | None = None

    def a_dict(self) -> dict[str, Any]:
        return {
            "mode": self.modo,
            "accion_flag": self.accion,
            "inventario": self.inventario.a_dict(),
            "respaldo": str(self.respaldo.destino) if self.respaldo is not None else None,
            "cobertura_usvfs": "no_verificada",
        }


def exigir_cobertura_usvfs(ack_usvfs_no_verificado: bool) -> None:
    """HOLD de cobertura: escribir el flag VACÍO es lo que dispara el ``remove()``.

    Aplica a TODO modo que escriba vacío —``new`` y ``rebuild``—, no sólo al
    destructivo: desde ``new`` NGIO borra igual ``*.cgid``/``*.dgid``/``*.fail``
    de ``Data/Grass``, y a través de la USVFS ese ``Data/Grass`` incluye el
    ``Grass/`` de los mods habilitados, que este inventario NO ve. El inventario
    local vacío prueba que no se pierde nada *de ``overwrite/Grass``*; no prueba
    nada sobre el overlay. Mientras eso no esté demostrado en un rig, el
    contrato no puede presentar ``new`` como inofensivo.

    El ``ack`` es aceptación de riesgo, no certificación: no demuestra que los
    archivos alcanzables estén respaldados.
    """
    if ack_usvfs_no_verificado:
        return
    raise GrassCacheSafetyError(
        "usvfs_no_demostrado",
        "HOLD: la cobertura USVFS no está demostrada. Escribir "
        f"{PRECACHE_FLAG} vacío hace que NGIO borre .cgid/.dgid/.fail de Data/Grass, que a través "
        "de la USVFS superpone el Grass/ de TODOS los mods habilitados: un .cgid que viva en un "
        "mod NO está inventariado ni respaldado, ni en NEW ni en REBUILD. Para correr igual, "
        "pasá ack_usvfs_no_verificado=True (queda visible en el prompt HITL y en el journal).",
    )


def _exigir_aprobacion_y_cobertura(*, aprobacion: str | None, ack_usvfs_no_verificado: bool) -> None:
    """Precondiciones del modo destructivo: aprobación literal + reconocimiento VFS.

    Vive en una función porque la usan dos llamadores en momentos distintos:
    :func:`preparar_arranque` la corre **antes de respaldar** (un HOLD no debe
    tocar el disco) y :func:`planificar_arranque` la vuelve a exigir al decidir.
    Tenerla duplicada era la forma de que una de las dos se quedara atrás.
    """
    if aprobacion != APROBACION_REBUILD:
        raise GrassCacheSafetyError(
            "rebuild_sin_aprobacion",
            f"El modo REBUILD regenera el caché desde cero y NGIO borra el anterior: exige "
            f"aprobación explícita ('{APROBACION_REBUILD}').",
        )
    exigir_cobertura_usvfs(ack_usvfs_no_verificado)


def planificar_arranque(
    *,
    modo: GrassCacheMode,
    inventario: InventarioCache,
    aprobacion: str | None = None,
    respaldo: RespaldoVerificado | None = None,
    progreso_archivado: bytes | None = None,
    ack_usvfs_no_verificado: bool = False,
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
        # NEW escribe el flag vacío: el mismo disparador de borrado de NGIO que
        # REBUILD. Sin cobertura USVFS demostrada, HOLD (ver exigir_cobertura_usvfs).
        exigir_cobertura_usvfs(ack_usvfs_no_verificado)
        return PlanArranque(modo="new", accion="escribir_vacio", contenido=b"", inventario=inventario)

    if modo == "resume":
        if inventario.flag_progreso is not None:
            # Se conserva el contenido EXACTO (bytes): es la señal de reanudación de NGIO.
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
    _exigir_aprobacion_y_cobertura(aprobacion=aprobacion, ack_usvfs_no_verificado=ack_usvfs_no_verificado)
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
    return PlanArranque(
        modo="rebuild", accion="escribir_vacio", contenido=b"", inventario=inventario, respaldo=respaldo
    )


def ruta_del_flag_alternativo(game_path: pathlib.Path) -> pathlib.Path:
    """``<juego>/PrecacheGrass.txt.txt``: el segundo nombre que NGIO abre."""
    return game_path / PRECACHE_FLAG_ALTERNATIVO


def exigir_sin_flag_alternativo(game_path: pathlib.Path) -> None:
    """Se niega a arrancar si existe el flag alternativo de NGIO.

    **Qué problema cierra.** NGIO 1.0.13 no lee un nombre fijo:
    ``Util::getProgressFilePath()`` devuelve ``PrecacheGrass.txt`` si existe y,
    si no, ``PrecacheGrass.txt.txt``. Sky-Claw administraba sólo el primario, así
    que con un alternativo con progreso:

    1. ``resume`` no lo veía y se negaba (o, peor, veía "sin progreso" donde
       NGIO veía progreso);
    2. ``new`` escribía el primario vacío y **enmascaraba** el alternativo;
    3. al neutralizar el primario, el alternativo volvía a ser el flag efectivo
       y el próximo arranque manual del juego entraba en modo precache.

    **Cómo lo decide.** Fail-closed sin administrar el alternativo: si existe
    (aunque esté vacío, o sea un enlace roto: NGIO decide por ``exists()``), se
    levanta ``flag_alternativo_presente`` antes de escribir nada y se le pide al
    operador que lo resuelva con el juego cerrado. Administrarlo (archivarlo y
    neutralizarlo junto al primario) queda fuera de P0: toca dos nombres a la vez
    y no hay forma de verificar en este entorno cómo interactúa con la caché de
    ``_ovFilePath`` que NGIO guarda en memoria.
    """
    alternativo = ruta_del_flag_alternativo(game_path)
    if not path_present(alternativo):
        return
    raise GrassCacheSafetyError(
        "flag_alternativo_presente",
        f"NGIO abre {alternativo} como flag de progreso cuando {PRECACHE_FLAG} no existe "
        "(Util::getProgressFilePath, 1.0.13). Mientras exista: `resume` no puede leer su "
        "progreso, `new` lo enmascararía con el primario y, al neutralizar el primario, el "
        "alternativo volvería a poner el juego en modo precache. Cerrá el juego, renombrá o "
        "borrá ese archivo y volvé a intentar.",
    )


def preparar_arranque(
    *,
    modo: GrassCacheMode,
    game_path: pathlib.Path,
    grass_dir: pathlib.Path,
    aprobacion: str | None = None,
    raiz_estado: pathlib.Path | None = None,
    ack_usvfs_no_verificado: bool = False,
    perfil: str = "",
) -> PlanArranque:
    """Inventaría, respalda si el modo lo exige y devuelve el plan — idempotente.

    Es el único punto que contesta "¿puedo arrancar?"; lo llaman el servicio
    (antes de clonar el perfil) y el runner (antes de escribir el flag). Repetir
    la llamada no recopia el respaldo ni rearchiva el progreso: el respaldo se
    reusa sólo si su contenido verifica contra el caché actual.
    """
    # El flag alternativo se rechaza ANTES de cualquier decisión o escritura: con
    # él presente, ni el inventario ni el plan representan lo que NGIO va a leer.
    exigir_sin_flag_alternativo(game_path)
    if modo == "rebuild":
        # Fail-fast ANTES de tocar el disco: un HOLD no respalda nada.
        _exigir_aprobacion_y_cobertura(aprobacion=aprobacion, ack_usvfs_no_verificado=ack_usvfs_no_verificado)
    inventario = inventariar_cache(grass_dir=grass_dir, flag_path=ruta_del_flag(game_path))
    respaldo: RespaldoVerificado | None = None
    if modo == "rebuild" and inventario.tiene_cache:
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
    if modo == "rebuild" and inventario.flag_progreso is not None:
        # El flag con progreso se pisa en un REBUILD. Antes era best-effort
        # (``suppress(OSError)``): si el archivado fallaba, la evidencia se
        # perdía en silencio justo cuando nada la podía recuperar. Ahora se
        # niega el arranque: sin evidencia archivada no se pisa el progreso.
        if raiz_estado is None:
            # Sin raíz de estado no hay dónde archivar. Con caché vacío el
            # arranque ni siquiera pide respaldo, así que este era el camino
            # silencioso para destruir la única evidencia de reanudación.
            raise GrassCacheSafetyError(
                "progreso_sin_destino_de_archivo",
                f"REBUILD pisaría {PRECACHE_FLAG}, que tiene progreso, y no hay raíz de estado "
                "donde archivarlo: se niega el arranque para no perder la única evidencia de "
                "reanudación. Pasá raiz_estado (el directorio de estado de Sky-Claw).",
            )
        try:
            archivar_progreso(raiz_estado, inventario, inventario.flag_progreso, grass_dir=grass_dir, perfil=perfil)
        except (GrassCacheSafetyError, OSError) as exc:
            raise GrassCacheSafetyError(
                "progreso_no_archivado",
                f"REBUILD pisaría {PRECACHE_FLAG} y su progreso no se pudo archivar en "
                f"{raiz_estado}: {exc}. Se niega el arranque para no perder la única evidencia "
                "de reanudación (revisá permisos/espacio del directorio de estado).",
            ) from exc
    # El archivo se consulta SÓLO si el flag vivo no trae progreso: con progreso
    # vivo el plan es ``conservar`` (bytes exactos) y la evidencia archivada no
    # participa, así que un archivo de otro origen no puede abortar un `resume`
    # que nunca lo iba a mirar.
    progreso = (
        recuperar_progreso(raiz_estado, inventario, grass_dir=grass_dir, perfil=perfil)
        if modo == "resume" and inventario.flag_progreso is None
        else None
    )
    plan = planificar_arranque(
        modo=modo,
        inventario=inventario,
        aprobacion=aprobacion,
        respaldo=respaldo,
        progreso_archivado=progreso,
        ack_usvfs_no_verificado=ack_usvfs_no_verificado,
    )
    if plan.modo == "rebuild" and plan.respaldo is not None:
        # Última comprobación de contenido antes de autorizar el ``remove()`` de
        # NGIO: el respaldo tiene que cubrir el caché TAL COMO ESTÁ AHORA.
        verificar_respaldo_para_borrado(grass_dir=grass_dir, respaldo=plan.respaldo)
    if plan.accion == "escribir_vacio":
        logger.warning(
            "Grass cache: se escribirá %s vacío en %s. Cobertura USVFS NO demostrada: sólo se "
            "inventarió %s; un .cgid en un mod habilitado no está cubierto.",
            PRECACHE_FLAG,
            game_path,
            grass_dir,
        )
    return plan


@dataclass(slots=True)
class EstadoAplicacion:
    """Qué hizo ``aplicar_plan`` con el flag —también cuando termina en error—.

    Separa dos cosas que no son la misma:

    - ``escrito``: la **sustitución ocurrió**. Desde ese instante el flag en
      disco es de Sky-Claw y su limpieza queda bajo responsabilidad del
      llamador, aunque la verificación posterior falle (si no, quedaría un flag
      vacío que haría que el arranque siguiente del juego borre el caché).
    - ``verificado``: el contenido final se releyó y coincide con el plan.

    Un ``escrito=True, verificado=False`` es un fallo, pero un fallo que NO
    libera al llamador de limpiar.
    """

    escrito: bool = False
    verificado: bool = False


def exigir_flag_conservado(flag_path: pathlib.Path, esperado: bytes) -> None:
    """Revalida el flag JUSTO antes de dejar que NGIO reanude con él.

    Entre el preflight y la escritura hay una ventana: otro proceso (o el
    usuario) puede vaciar o borrar el flag. Aceptar ``conservar`` sobre un flag
    distinto convertiría una reanudación en un borrado del caché, así que se
    falla cerrado.
    """
    try:
        presente = path_present(flag_path)
    except OSError as exc:
        raise GrassCacheSafetyError(
            "flag_ilegible", f"No se pudo inspeccionar {flag_path} antes de reanudar: {exc}"
        ) from exc
    if not presente:
        raise GrassCacheSafetyError(
            "flag_cambio_despues_del_preflight",
            f"{flag_path} desapareció entre el preflight y el arranque: el progreso que se iba a "
            "reanudar ya no está y NGIO empezaría de cero. No se lanza el juego.",
        )
    _rechazar_enlace(flag_path, "flag_es_enlace")
    try:
        actual = flag_path.read_bytes()
    except OSError as exc:
        raise GrassCacheSafetyError("flag_ilegible", f"No se pudo leer {flag_path} antes de reanudar: {exc}") from exc
    if actual != esperado:
        raise GrassCacheSafetyError(
            "flag_cambio_despues_del_preflight",
            f"{flag_path} cambió entre el preflight ({len(esperado)} bytes) y el arranque "
            f"({len(actual)} bytes): el progreso que se iba a reanudar ya no es el mismo. "
            "No se lanza el juego.",
        )


def _ruta_temporal(flag_path: pathlib.Path) -> pathlib.Path:
    """Temporal impredecible en el MISMO directorio (``os.replace`` es atómico).

    Un nombre fijo (``PrecacheGrass.txt.tmp``) es una invitación: quien pueda
    crear ese nombre —un enlace a otro archivo, por ejemplo— redirige la
    escritura. Se extrae a función para poder fijarlo en los tests.
    """
    return flag_path.with_name(f".{flag_path.name}.{secrets.token_hex(8)}.tmp")


def _escribir_temporal_exclusivo(ruta: pathlib.Path, contenido: bytes) -> None:
    """Crea *ruta* con ``O_CREAT|O_EXCL`` y escribe BYTES. Nunca reutiliza.

    ``O_EXCL`` es la garantía que importa: si algo ocupa ese nombre (archivo,
    symlink o junction), el ``open`` falla en vez de seguir el enlace y escribir
    fuera del directorio. ``O_NOFOLLOW`` es defensa extra donde exista (POSIX).
    """
    banderas = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(ruta, banderas, 0o600)
    except FileExistsError as exc:
        raise GrassCacheSafetyError(
            "flag_temporal_inseguro",
            f"El temporal {ruta} ya existe: no se escribe sobre algo que no creamos nosotros.",
        ) from exc
    except OSError as exc:
        raise GrassCacheSafetyError("flag_temporal_inseguro", f"No se pudo crear el temporal {ruta}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(contenido)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        with contextlib.suppress(OSError):
            ruta.unlink(missing_ok=True)
        raise


def aplicar_plan(
    plan: PlanArranque,
    flag_path: pathlib.Path,
    *,
    estado: EstadoAplicacion | None = None,
) -> EstadoAplicacion:
    """Escribe el flag según el plan. Única función que lo muta.

    ``conservar`` no escribe nada: reescribir el mismo contenido sería inocuo
    hoy, pero cualquier normalización futura (saltos de línea, BOM, recorte)
    convertiría una reanudación correcta en un borrado de caché. Se escriben
    BYTES, sin recodificar. Antes de aceptarla se **revalida** el flag
    (presencia, tipo y contenido exacto): si cambió desde el preflight, se
    falla cerrado.

    El temporal es **exclusivo** y de nombre impredecible, y después del
    reemplazo se **relee el flag**: dar por escrito lo que no se pudo sustituir
    sería declarar éxito sin flag (o con un flag manipulado).

    Devuelve —y también deja en *estado*, si se pasa— qué se hizo con el flag.
    ``escrito`` se marca **antes** del postcheck: si éste falla, el llamador
    igual sabe que el flag en disco es suyo y tiene que limpiarlo.
    """
    estado = estado if estado is not None else EstadoAplicacion()
    if plan.accion == "conservar":
        exigir_flag_conservado(flag_path, plan.contenido)
        estado.verificado = True
        return estado
    _rechazar_enlace(flag_path, "flag_es_enlace")
    tmp = _ruta_temporal(flag_path)
    _escribir_temporal_exclusivo(tmp, plan.contenido)
    try:
        os.replace(tmp, flag_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise GrassCacheSafetyError(
            "flag_no_escrito", f"No se pudo sustituir {flag_path} con el temporal: {exc}"
        ) from exc
    # La sustitución OCURRIÓ: el flag en disco es nuestro desde acá, aunque el
    # postcheck de abajo falle.
    estado.escrito = True
    with contextlib.suppress(OSError):
        tmp.unlink(missing_ok=True)  # no-op si el rename se lo llevó; limpia si fue un no-op
    _fsync_directorio(flag_path.parent)
    _rechazar_enlace(flag_path, "flag_es_enlace")
    try:
        escrito = flag_path.read_bytes()
    except OSError as exc:
        raise GrassCacheSafetyError(
            "flag_no_verificado", f"No se pudo releer {flag_path} después de escribirlo: {exc}"
        ) from exc
    if escrito != plan.contenido:
        raise GrassCacheSafetyError(
            "flag_no_verificado",
            f"{flag_path} no quedó con el contenido planificado ({len(escrito)} de "
            f"{len(plan.contenido)} bytes): no se declara escrito el flag.",
        )
    estado.verificado = True
    return estado


def ruta_de_respaldo(plan: PlanArranque) -> str:
    """Ruta del respaldo del plan, o cadena vacía (para el resultado)."""
    return str(plan.respaldo.destino) if plan.respaldo is not None else ""


__all__ = [
    "APROBACION_REBUILD",
    "EXTENSIONES_CACHE",
    "MODOS_GRASS_CACHE",
    "PRECACHE_FLAG",
    "PRECACHE_FLAG_ALTERNATIVO",
    "ruta_del_flag_alternativo",
    "EstadoAplicacion",
    "GrassCacheMode",
    "GrassCacheSafetyError",
    "InventarioCache",
    "NeutralizacionFlag",
    "PlanArranque",
    "RespaldoVerificado",
    "aplicar_plan",
    "archivar_progreso",
    "exigir_cobertura_usvfs",
    "exigir_flag_conservado",
    "exigir_sin_flag_alternativo",
    "huella_del_cache",
    "inventariar_cache",
    "neutralizar_flag",
    "planificar_arranque",
    "preparar_arranque",
    "recuperar_progreso",
    "respaldar_cache",
    "ruta_de_respaldo",
    "ruta_del_flag",
    "verificar_respaldo_para_borrado",
]

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

- ``new``: no hay caché ni progreso previo. Escribe el flag vacío. Si hay
  caché o un flag con progreso, **se niega**: no es una operación de ``new``.
- ``resume``: hay progreso demostrable —el flag con contenido, o la evidencia
  archivada por un run anterior cuya huella coincide con el caché actual—.
  **Nunca trunca** el flag: ése es el disparador del borrado en NGIO.
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

Por eso ``rebuild`` está en **HOLD por defecto**: sin
``ack_usvfs_no_verificado=True`` no arranca, y el respaldo se declara "lo que
se pudo ver", nunca "protección completa". El reconocimiento es explícito,
trazado y visible en el prompt HITL — el mismo patrón que
``force_stage_guard``. Cerrar el HOLD exige evidencia de rig sobre qué hace
USVFS al borrar, y es trabajo de P2 (USVFS), no de este módulo.

**Evidencia de progreso.** Neutralizar el flag al salir es obligatorio (un flag
residual haría que el juego del usuario arranque en modo precache), pero no
puede costar la reanudación: antes de borrarlo se archiva su contenido **byte a
byte** (base64 + sha256, sin decodificar como texto), atado a la huella del
caché en ese momento. ``resume`` lo restaura sólo si la huella coincide.
"""

from __future__ import annotations

import base64
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


def _validar_raiz_fisica(grass_dir: pathlib.Path) -> None:
    """Exige que ``grass_dir`` cuelgue de una cadena de directorios REALES.

    Rechazar sólo los enlaces *dentro* del caché deja pasar el caso que importa:
    ``overwrite/Grass`` siendo un symlink o un junction a un árbol externo. Se
    reutiliza :func:`exigir_contencion_fisica` (la primitiva del repo: hace
    ``lstat`` de cada componente, no sigue enlaces y clasifica junctions de
    Windows) anclando en la raíz del volumen.

    Un componente inexistente no puede ser un enlace y el recorrido se detiene
    ahí (primer run, ``Grass/`` todavía no creado).
    """
    ancla = pathlib.Path(grass_dir.anchor)
    if not grass_dir.is_absolute() or ancla == grass_dir:
        raise GrassCacheSafetyError(
            "cache_raiz_no_absoluta",
            f"La raíz del caché de grass debe ser una ruta absoluta bajo un directorio real: {grass_dir}",
        )
    try:
        exigir_contencion_fisica(ancla, grass_dir)
    except ContencionFisicaVioladaError as exc:
        raise GrassCacheSafetyError(
            "cache_raiz_enlazada",
            f"La raíz del caché de grass atraviesa un enlace o un componente no real: {exc}. "
            "No se inventaría ni se respalda un caché a través de una redirección.",
        ) from exc
    except OSError as exc:
        raise GrassCacheSafetyError(
            "cache_raiz_ilegible", f"No se pudo validar la raíz del caché {grass_dir}: {exc}"
        ) from exc


def _leer_flag(flag_path: pathlib.Path) -> tuple[bool, bytes | None]:
    """``(presente, progreso)`` del flag, con el progreso en BYTES EXACTOS.

    No se decodifica como texto: archivar y restaurar tienen que devolver los
    mismos bytes, y ``errors="replace"`` cambiaría el contenido de un flag con
    codificación inesperada (o con bytes sueltos) justo cuando ese contenido es
    la única evidencia de reanudación.

    "Tiene progreso" se decide igual que NGIO: alguna línea con contenido no
    blanco (``splitlines`` + ``strip``). Un formato desconocido entonces
    *protege* —impide ``new``/``rebuild`` sin decisión—, que es el lado seguro.
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
    hay_progreso = any(linea.strip() for linea in crudo.splitlines())
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

    Es la lectura de contenido completa del origen. Sólo la paga ``rebuild``
    (que ya es una operación que el operador acepta como costosa): es la única
    forma de afirmar que el respaldo cubre *este* contenido y no uno con los
    mismos tamaños.
    """
    digests: dict[str, tuple[int, str]] = {}
    try:
        candidatos = sorted(grass_dir.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, NotADirectoryError):
        return digests
    except OSError as exc:
        raise GrassCacheSafetyError("cache_ilegible", f"No se pudo leer {grass_dir}: {exc}") from exc
    for archivo in candidatos:
        if archivo.suffix.lower() not in EXTENSIONES_CACHE or not archivo.is_file():
            continue
        _rechazar_enlace(archivo, "cache_con_enlaces")
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


def _respaldo_existente_verifica(
    destino: pathlib.Path,
    inventario: InventarioCache,
    origen: dict[str, tuple[int, str]],
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
    manifiesto = _leer_manifiesto(destino / "manifiesto.json")
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

    reusado = _respaldo_existente_verifica(destino, inventario, origen) if path_present(destino) else None
    if reusado is not None:
        logger.info("Respaldo de grass verificado por contenido en %s; se reusa.", destino)
        return reusado

    try:
        destino.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        invalido = destino.with_name(f"{destino.name}.invalido-{int(time.time())}")
        with contextlib.suppress(OSError):
            os.replace(destino, invalido)
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
    tmp = destino / ".manifiesto.json.tmp"
    tmp.write_text(json.dumps(manifiesto, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ruta_manifiesto)
    _fsync_directorio(destino)

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


def _ruta_progreso(raiz_estado: pathlib.Path, huella: str) -> pathlib.Path:
    return _dir_progreso(raiz_estado) / f"{huella}.json"


def archivar_progreso(raiz_estado: pathlib.Path, inventario: InventarioCache, contenido: bytes) -> pathlib.Path:
    """Guarda los BYTES del flag atados a la huella del caché de ESE momento.

    ``base64`` + ``sha256``: el contenido no se interpreta como texto, así que
    restaurarlo devuelve los mismos bytes aunque el flag tenga una codificación
    inesperada. Un fallo acá no puede impedir la limpieza del flag (el llamador
    lo trata como best-effort).
    """
    destino = _ruta_progreso(raiz_estado, inventario.huella)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".json.tmp")
    carga = {
        "version": _VERSION_MANIFIESTO,
        "huella": inventario.huella,
        "inventario": inventario.a_dict(),
        "contenido_b64": base64.b64encode(contenido).decode("ascii"),
        "contenido_sha256": hashlib.sha256(contenido).hexdigest(),
        "contenido_bytes": len(contenido),
        "archivado": time.time(),
    }
    tmp.write_text(json.dumps(carga, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, destino)
    _fsync_directorio(destino.parent)
    return destino


def recuperar_progreso(raiz_estado: pathlib.Path | None, inventario: InventarioCache) -> bytes | None:
    """Progreso archivado para ESTE caché, o ``None``.

    La huella tiene que coincidir (reanudar con el progreso de otro conjunto de
    archivos haría que NGIO saltee celdas que nunca se escribieron) y el
    contenido tiene que sobrevivir el round-trip: se valida contra el sha256
    archivado, así un archivo de archivo corrupto no se restaura a medias.
    """
    if raiz_estado is None:
        return None
    ruta = _ruta_progreso(raiz_estado, inventario.huella)
    carga = _leer_manifiesto(ruta)
    if carga is None or carga.get("version") != _VERSION_MANIFIESTO or carga.get("huella") != inventario.huella:
        return None
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
    return contenido if any(linea.strip() for linea in contenido.splitlines()) else None


# ---------------------------------------------------------------------------
# Neutralización del flag (verificable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NeutralizacionFlag:
    """Resultado de archivar + borrar el flag. Nunca se pierde un fallo."""

    borrado: bool
    archivado: bool
    errores: tuple[str, ...] = ()

    @property
    def limpio(self) -> bool:
        return self.borrado


def neutralizar_flag(
    *,
    flag_path: pathlib.Path,
    grass_dir: pathlib.Path,
    raiz_estado: pathlib.Path | None,
) -> NeutralizacionFlag:
    """Archiva el progreso (si lo hay) y borra el flag. Nunca propaga errores.

    El borrado es obligatorio: un flag residual haría que el juego del usuario
    arranque en modo precache. Por eso el resultado es explícito —un
    ``unlink`` que falla NO es limpieza exitosa— y el llamador lo publica en el
    resultado del run. El archivado preserva lo único que permitiría reanudar
    sin destruir el caché, así que se intenta siempre antes.
    """
    errores: list[str] = []
    archivado = False

    try:
        presente = path_present(flag_path)
    except OSError as exc:
        presente = False
        errores.append(f"No se pudo inspeccionar {flag_path}: {exc}")

    if presente and raiz_estado is not None:
        try:
            inventario = inventariar_cache(grass_dir=grass_dir, flag_path=flag_path)
            if inventario.flag_progreso is not None:
                archivar_progreso(raiz_estado, inventario, inventario.flag_progreso)
                archivado = True
        except (GrassCacheSafetyError, OSError) as exc:
            errores.append(f"No se pudo archivar el progreso: {exc}")

    try:
        # Un enlace no es el flag de NGIO: se informa y no se borra (podría ser
        # un archivo del usuario apuntado desde otro lado).
        tipo = link_kind_or_raise(flag_path) if presente else None
    except OSError as exc:
        tipo = None
        errores.append(f"No se pudo clasificar el flag antes de borrarlo: {exc}")

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
    return NeutralizacionFlag(borrado=borrado, archivado=archivado, errores=tuple(errores))


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
    if not ack_usvfs_no_verificado:
        # HOLD explícito: el respaldo cubre overwrite/Grass, no el overlay USVFS.
        raise GrassCacheSafetyError(
            "rebuild_usvfs_no_demostrado",
            "HOLD: la cobertura USVFS no está demostrada. El respaldo inventaría sólo "
            "<mo2>/overwrite/Grass, pero NGIO borra Data/Grass a través de la USVFS, que superpone el "
            "Grass/ de los mods habilitados: un .cgid que viva en un mod NO está respaldado. "
            "Para regenerar de todos modos, pasá ack_usvfs_no_verificado=True (queda visible en HITL).",
        )


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


def preparar_arranque(
    *,
    modo: GrassCacheMode,
    game_path: pathlib.Path,
    grass_dir: pathlib.Path,
    aprobacion: str | None = None,
    raiz_estado: pathlib.Path | None = None,
    ack_usvfs_no_verificado: bool = False,
) -> PlanArranque:
    """Inventaría, respalda si el modo lo exige y devuelve el plan — idempotente.

    Es el único punto que contesta "¿puedo arrancar?"; lo llaman el servicio
    (antes de clonar el perfil) y el runner (antes de escribir el flag). Repetir
    la llamada no recopia el respaldo ni rearchiva el progreso: el respaldo se
    reusa sólo si su contenido verifica contra el caché actual.
    """
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
    if modo == "rebuild" and inventario.flag_progreso is not None and raiz_estado is not None:
        # El flag con progreso se pisa en un REBUILD: su contenido queda como
        # evidencia antes de perderlo (best-effort, no bloquea).
        with contextlib.suppress(OSError):
            archivar_progreso(raiz_estado, inventario, inventario.flag_progreso)
    progreso = recuperar_progreso(raiz_estado, inventario) if modo == "resume" else None
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


def aplicar_plan(plan: PlanArranque, flag_path: pathlib.Path) -> None:
    """Escribe el flag según el plan. Única función que lo muta.

    ``conservar`` no escribe nada: reescribir el mismo contenido sería inocuo
    hoy, pero cualquier normalización futura (saltos de línea, BOM, recorte)
    convertiría una reanudación correcta en un borrado de caché. Se escriben
    BYTES, sin recodificar.
    """
    if plan.accion == "conservar":
        return
    _rechazar_enlace(flag_path, "flag_es_enlace")
    tmp = flag_path.with_name(f"{flag_path.name}.tmp")
    try:
        tmp.write_bytes(plan.contenido)
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
    "NeutralizacionFlag",
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
    "verificar_respaldo_para_borrado",
]

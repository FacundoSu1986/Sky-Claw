"""Escritor de la salida gestionada del generador nativo de parallax (NP-O0b2, ADR 0014 §2.5).

``output.py`` decide, sin tocar el disco, quién es dueño de qué y qué hacer con cada ruta. Este módulo es el ÚNICO que
toca el mod de salida: lo hace bajo un lock cross-process, con un protocolo que sobrevive a un corte en cualquier
momento (ver ADR 0014, «Actualización de NP-O0b1» y «Actualización de NP-O0b2»):

1. **Una sesión por raíz, bajo lock.** ``abrir_sesion_de_salida`` toma el lock de esa raíz (el mismo
  ``DistributedLockManager`` de todo el árbol, con su estado en un directorio durable y no relativo al ``cwd``) y
  RECUPERA primero cualquier corte anterior. Todo el que muta el mod pasa por una ``SesionDeSalida``; no hay otra
  puerta.
2. **Intención antes de tocar.** Antes de modificar un archivo se guarda el manifiesto «pendiente» (el que quedaría si
  todo sale bien), con escritura atómica.
3. **Apartar y verificar.** Un archivo nuestro que se reemplaza o se retira se MUEVE a una papelera y recién ahí se
  calcula su huella: la huella verificada es la del archivo que salió de la ruta, no la de un instante anterior. Si no
  es la autorizada lo tocó un tercero y se devuelve; nunca se pisa ni se borra. Es UNA propiedad para los dos caminos: si
  el proceso muere antes de verificar, la recuperación lo devuelve igual (si la ruta sigue vacía). Todo borrado de la
  papelera con un hueco desde la verificación rehace la huella justo antes de borrar.
4. **Compromiso al final.** El manifiesto definitivo se escribe al final; después se limpia y el pendiente se borra
  ÚLTIMO. Un archivo que no figura en el manifiesto no es nuestro (``PRESERVE_EXISTING``).

Qué NO hace: no genera texturas ni ejecuta el codificador de DDS (es del lote, NP-A0), no crea ``meta.ini`` ni habilita el mod en
MO2 (lo hace el adaptador de MO2, NP-A0) y no decide de dónde sale el ``entorno`` de la procedencia. Escribe
bloqueando solo el hilo de trabajo (``asyncio.to_thread``), nunca el loop.

Límites: la durabilidad ante un corte de luz se limita al contenido de los archivos (``fsync``); no se hace ``fsync`` de
los directorios. Entre «apartar» y «publicar» la ruta está vacía un instante; si el proceso muere ahí, la recuperación
devuelve el original.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import pathlib
import stat
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from sky_claw.app.db.locks import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_MAX,
    DEFAULT_MAX_RETRIES,
    DistributedLockManager,
    LockAcquisitionError,
    SnapshotTransactionLock,
)
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.app.security.links import (
    ContencionFisicaVioladaError,
    exigir_contencion_fisica,
    link_kind_and_identity_or_raise_with_retry,
    reparse_tag_or_zero,
)
from sky_claw.local.native_parallax.output import (
    NOMBRE_DEL_MOD_DE_SALIDA,
    TAMANO_MAXIMO_DEL_MANIFIESTO,
    AccionDeSalida,
    AccionPlanificada,
    EntradaDeManifiesto,
    EstadoTrasCorte,
    Manifiesto,
    ManifiestoInvalidoError,
    Observacion,
    PlanDeSalida,
    Procedencia,
    SalidaDeseada,
    SalidaGestionadaError,
    TipoObservado,
    clave_de_cache,
    clave_de_ruta,
    cota_del_manifiesto_tras_un_corte,
    crear_entrada,
    leer_manifiesto,
    planificar,
    reconciliar_tras_corte,
    serializar_manifiesto,
)

__all__ = [
    "CoordinacionDeSalida",
    "EfectoDeSalida",
    "FuenteDeSalida",
    "ResultadoDeAplicacion",
    "ResultadoPorRuta",
    "SalidaNoEscribibleError",
    "SalidaOcupadaError",
    "SesionDeSalida",
    "abrir_sesion_de_salida",
    "construir_coordinacion_de_salida",
    "ruta_de_estado_de_salida",
]

logger = logging.getLogger(__name__)

_CARPETA_DE_CONTROL: Final = ".skyclaw-output"
_MANIFIESTO: Final = "manifest.json"
_PENDIENTE: Final = "pending.json"
_ETAPAS: Final = "staging"
_PAPELERA: Final = "trash"
_SUBDIR_DE_ESTADO: Final = "native_parallax_output"
_ARCHIVO_DE_LOCKS: Final = "output_locks.db"
#: ``MAX_PATH`` de Windows menos el terminador: ni el juego ni MO2 cargan una textura cuya ruta completa lo supere.
_LARGO_MAXIMO_DE_RUTA_COMPLETA: Final = 259
_BLOQUE: Final = 1024 * 1024
#: Huella que se le asigna a un archivo más grande que cualquiera de los nuestros: no se lee entero, y no puede ser la de
#: ninguna entrada (nunca coincide con una huella real salvo una preimagen de SHA-256).
_HUELLA_DE_ARCHIVO_DESMEDIDO: Final = hashlib.sha256(b"sky-claw/native-parallax/archivo-desmedido/1\n").hexdigest()
_TAMANO_MAXIMO_DE_ARCHIVO: Final = 2**30
#: TTL nominal del lock de la sesión: lo renueva el latido mientras la sesión esté viva, no es un plazo de vida.
_TTL_DE_LA_SESION: Final = 60.0

#: Los lugares donde un test puede simular que el proceso muere (ver ``_punto_de_corte``). Congelado por igualdad.
_PUNTOS_DE_CORTE: Final = (
    "tras_pendiente",
    "tras_etapa",
    "tras_apartar",
    "tras_verificar",
    "tras_publicar",
    "tras_borrar",
    "antes_del_compromiso",
    "tras_compromiso",
    "tras_limpieza",
    "recuperacion_tras_papelera",
    "recuperacion_tras_manifiesto",
)

_FICHA: Final = object()


class SalidaNoEscribibleError(SalidaGestionadaError):
    """La sesión no puede arrancar o el lote no puede aplicarse: no se tocó nada que no se pueda recuperar."""


class SalidaOcupadaError(SalidaNoEscribibleError):
    """Otro proceso tiene el lock de esta raíz. Es reintentable: no se tocó nada."""


class EfectoDeSalida(Enum):
    PUBLICADA = "publicada"  # se creó donde no había nada
    REEMPLAZADA = "reemplazada"  # se reemplazó un archivo nuestro e intacto
    RETIRADA = "retirada"  # se borró un archivo nuestro e intacto que ya no se desea
    OMITIDA = "omitida"  # la caché estaba vigente
    PRESERVADA = "preservada"  # no se tocó: no es nuestro, cambió, o la ruta la ocupa otra cosa
    SOLTADA = "soltada"  # dejó de ser asunto nuestro (sale del manifiesto) sin tocar el disco
    CONFLICTO = "conflicto"  # el disco cambió entre planificar y aplicar: no se hizo nada que pisara a un tercero
    FALLIDA = "fallida"  # un error de entrada/salida o de validación: esa ruta no se hizo


@dataclass(frozen=True, slots=True)
class ResultadoPorRuta:
    ruta: str
    efecto: EfectoDeSalida
    detalle: str


@dataclass(frozen=True, slots=True)
class ResultadoDeAplicacion:
    """Qué pasó con cada ruta, y el manifiesto que quedó comprometido."""

    rutas: tuple[ResultadoPorRuta, ...]
    manifiesto: Manifiesto

    @property
    def completo(self) -> bool:
        """True si ninguna ruta terminó en conflicto o fallo."""
        return not any(r.efecto in (EfectoDeSalida.CONFLICTO, EfectoDeSalida.FALLIDA) for r in self.rutas)


@dataclass(frozen=True, slots=True)
class FuenteDeSalida:
    """El archivo ya generado (en el directorio de trabajo del lote) y la procedencia con la que se generó."""

    archivo: pathlib.Path
    procedencia: Procedencia

    def __post_init__(self) -> None:
        if not isinstance(self.archivo, pathlib.Path):
            raise SalidaNoEscribibleError(f"la fuente debe ser una ruta (Path), no {type(self.archivo).__name__}")
        if not isinstance(self.procedencia, Procedencia):
            raise SalidaNoEscribibleError("la procedencia de la fuente debe ser una Procedencia")


# ---------------------------------------------------------------------------
# Coordinación cross-process
# ---------------------------------------------------------------------------


def ruta_de_estado_de_salida(base: pathlib.Path | None = None) -> pathlib.Path:
    """Directorio de estado durable de la coordinación de la salida: por usuario, no relativo al ``cwd``.

    Es lo que hace que dos instancias lanzadas desde directorios distintos compartan el mismo lock: un lock en una base
    relativa al ``cwd`` no excluiría a nadie. ``base`` existe para los tests.
    """
    from sky_claw.config import SystemPaths

    raiz = base if base is not None else SystemPaths.runtime_state_dir()
    return raiz / _SUBDIR_DE_ESTADO


class CoordinacionDeSalida:
    """El lock cross-process de las sesiones de salida, sobre el ``DistributedLockManager`` del árbol.

    El acceso al manager es ``async`` y único (``sostener``): un lock sin abrir no excluye a nadie y no tiene que poder
    sostenerse. El recurso se deriva de la raíz del mod, normalizada: dos ortografías de la misma ruta compiten por el
    MISMO recurso.
    """

    def __init__(self, *, lock_manager: DistributedLockManager, snapshot_manager: FileSnapshotManager) -> None:
        self._lock_manager = lock_manager
        self._snapshot_manager = snapshot_manager
        self._inicializado = False
        self._guardia = asyncio.Lock()

    @staticmethod
    def resource_id_de_la_raiz(raiz: pathlib.Path) -> str:
        normalizada = os.path.normcase(os.path.abspath(os.fspath(raiz)))
        return "native-parallax-output-" + hashlib.sha256(normalizada.encode("utf-8")).hexdigest()[:32]

    async def initialize(self) -> None:
        """Abre la DB de locks. Idempotente; ``sostener`` la llama sola."""
        async with self._guardia:
            if self._inicializado:
                return
            await self._lock_manager.initialize()
            self._inicializado = True

    async def close(self) -> None:
        async with self._guardia:
            if not self._inicializado:
                return
            await self._lock_manager.close()
            self._inicializado = False

    @contextlib.asynccontextmanager
    async def sostener(self, *, raiz: pathlib.Path, agent_id: str) -> AsyncIterator[SnapshotTransactionLock]:
        """Sostiene el lock de la raíz mientras dure el bloque (con latido de renovación y ``assert_owned``).

        Raises:
            SalidaOcupadaError: otro proceso tiene el lock de esta raíz. No se esperó indefinidamente.
        """
        await self.initialize()
        sostenido = SnapshotTransactionLock(
            lock_manager=self._lock_manager,
            snapshot_manager=self._snapshot_manager,
            resource_id=self.resource_id_de_la_raiz(raiz),
            agent_id=agent_id,
            target_files=[],
            ttl=_TTL_DE_LA_SESION,
        )
        try:
            await sostenido.__aenter__()
        except LockAcquisitionError as exc:
            raise SalidaOcupadaError(
                "otro proceso está escribiendo la salida de parallax en esta raíz; reintentá cuando termine"
            ) from exc
        try:
            yield sostenido
        except BaseException as exc:
            await sostenido.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await sostenido.__aexit__(None, None, None)


def construir_coordinacion_de_salida(
    *,
    base: pathlib.Path | None = None,
    lifecycle: Any = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    backoff_max: float = DEFAULT_BACKOFF_MAX,
) -> CoordinacionDeSalida:
    """Arma la coordinación sobre el estado durable por usuario (ver ``ruta_de_estado_de_salida``)."""
    directorio = ruta_de_estado_de_salida(base)
    directorio.mkdir(parents=True, exist_ok=True)
    return CoordinacionDeSalida(
        lock_manager=DistributedLockManager(
            db_path=directorio / _ARCHIVO_DE_LOCKS,
            max_retries=max_retries,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
            lifecycle=lifecycle,
        ),
        snapshot_manager=FileSnapshotManager(snapshot_dir=directorio / "snapshots"),
    )


# ---------------------------------------------------------------------------
# Primitivas de disco (síncronas; corren en un hilo)
# ---------------------------------------------------------------------------


def _punto_de_corte(nombre: str) -> None:
    """Lugar donde un test puede simular que el proceso muere (``nombre`` está en ``_PUNTOS_DE_CORTE``). No hace nada."""


def _destino(raiz: pathlib.Path, ruta: str) -> pathlib.Path:
    return raiz.joinpath(*ruta.split("/"))


def _id_de_ruta(ruta: str) -> str:
    """Nombre estable de la etapa y de la papelera de una ruta: la recuperación los encuentra sin un registro aparte."""
    return hashlib.sha256(clave_de_ruta(ruta).encode("ascii")).hexdigest()


def _huella_de_archivo(ruta: pathlib.Path) -> str:
    """SHA-256 del contenido, o la huella de «desmedido» si pesa más que cualquiera de las nuestras."""
    if ruta.stat().st_size > _TAMANO_MAXIMO_DE_ARCHIVO:
        return _HUELLA_DE_ARCHIVO_DESMEDIDO
    digest = hashlib.sha256()
    with ruta.open("rb") as f:
        while bloque := f.read(_BLOQUE):
            digest.update(bloque)
    return digest.hexdigest()


def _observar_ruta(raiz: pathlib.Path, ruta: str) -> Observacion:
    """Qué hay en disco en *ruta*, sin seguir enlaces. Ante cualquier duda, ``OTRO`` (que nunca se toca)."""
    destino = _destino(raiz, ruta)
    try:
        exigir_contencion_fisica(raiz, destino.parent, permitir_raiz=True)
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise_with_retry(destino)
    except (ContencionFisicaVioladaError, OSError):
        return Observacion(TipoObservado.OTRO, None)
    if identidad is None:
        return Observacion(TipoObservado.AUSENTE, None)
    if tipo_de_enlace is not None or reparse_tag_or_zero(identidad) or not stat.S_ISREG(identidad.st_mode):
        return Observacion(TipoObservado.OTRO, None)
    try:
        return Observacion(TipoObservado.ARCHIVO, _huella_de_archivo(destino))
    except OSError:
        return Observacion(TipoObservado.OTRO, None)


def _observar_rutas(raiz: pathlib.Path, rutas: Iterable[str]) -> dict[str, Observacion]:
    return {clave_de_ruta(ruta): _observar_ruta(raiz, ruta) for ruta in rutas}


def _mover_sin_reemplazar(origen: pathlib.Path, destino: pathlib.Path) -> None:
    """Mueve *origen* a *destino* sin pisar nada: ``FileExistsError`` si *destino* ya existe.

    En Windows ``os.rename`` ya se niega a reemplazar. En POSIX ``rename`` reemplaza en silencio, así que se publica con un
    enlace duro (que falla si *destino* existe) y recién después se quita el nombre viejo: si el proceso muere en el medio
    quedan los dos nombres sobre el mismo archivo, que la recuperación sabe leer.
    """
    if os.name == "nt":
        os.rename(origen, destino)
    else:
        os.link(origen, destino)
        os.unlink(origen)


def _escribir_atomico(destino: pathlib.Path, datos: bytes) -> None:
    """Escribe *datos* en *destino* de forma atómica (temporal + ``fsync`` + ``os.replace``) en el mismo directorio.

    ``os.replace`` es legítimo acá: *destino* es un archivo de control PROPIO, nunca un archivo de salida.
    """
    temporal = destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(datos)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, destino)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise


def _leer_documento(ruta: pathlib.Path, que: str) -> bytes | None:
    """Lee un archivo de control: ``None`` si no existe. Falla cerrado si no es un archivo regular o es enorme."""
    try:
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise_with_retry(ruta)
    except OSError as exc:
        raise SalidaNoEscribibleError(f"no se pudo inspeccionar {que}: {exc}") from exc
    if identidad is None:
        return None
    if tipo_de_enlace is not None or not stat.S_ISREG(identidad.st_mode):
        raise SalidaNoEscribibleError(f"{que} no es un archivo regular: no se interpreta")
    if identidad.st_size > TAMANO_MAXIMO_DEL_MANIFIESTO:
        raise SalidaNoEscribibleError(f"{que} supera {TAMANO_MAXIMO_DEL_MANIFIESTO} bytes: no se interpreta")
    try:
        return ruta.read_bytes()
    except OSError as exc:
        raise SalidaNoEscribibleError(f"no se pudo leer {que}: {exc}") from exc


def _interpretar(datos: bytes | None, que: str) -> Manifiesto | None:
    if datos is None:
        return None
    try:
        return leer_manifiesto(datos)
    except ManifiestoInvalidoError as exc:
        raise SalidaNoEscribibleError(
            f"{que} es ilegible ({exc}): sin él no se sabe qué archivos son nuestros. No se tocó nada; revisalo o, si "
            "estás seguro, borralo (lo ya publicado quedará como ajeno)"
        ) from exc


def _crear_carpeta_contenida(raiz: pathlib.Path, carpeta: pathlib.Path, *, permitir_raiz: bool = False) -> None:
    """Crea *carpeta* (y las intermedias) dentro de *raiz*, validando la contención física antes y después de crearla.

    Antes: un enlace o junction ya puesto en el camino se rechaza sin crear nada. Después: si el camino cambió mientras se
    creaba, se rechaza antes de que el caller escriba debajo. Es la ÚNICA forma en que el escritor crea carpetas dentro
    del mod (la raíz misma la crea ``_recuperar``): las de salida, al publicar y al devolver, y las de control (la
    carpeta de control, las etapas y la papelera).
    """
    exigir_contencion_fisica(raiz, carpeta, permitir_raiz=permitir_raiz)
    os.makedirs(carpeta, exist_ok=True)
    exigir_contencion_fisica(raiz, carpeta, permitir_raiz=permitir_raiz, exigir_existencia=True)


def _es_carpeta_contenida(raiz: pathlib.Path, carpeta: pathlib.Path) -> bool:
    """True si *carpeta* existe y es una carpeta REAL dentro de *raiz*: ni enlace ni junction en todo el camino."""
    try:
        exigir_contencion_fisica(raiz, carpeta, exigir_existencia=True)
    except ContencionFisicaVioladaError:
        return False
    return True


def _asegurar_directorio_de_control(raiz: pathlib.Path) -> pathlib.Path:
    control = raiz / _CARPETA_DE_CONTROL
    _crear_carpeta_contenida(raiz, control)
    for nombre in (_ETAPAS, _PAPELERA):  # copiar, apartar o limpiar en una que fuera un enlace lo haría afuera del mod
        if os.path.lexists(control / nombre):
            exigir_contencion_fisica(raiz, control / nombre, exigir_existencia=True)
    return control


def _limpiar_control(control: pathlib.Path) -> None:
    """Borra lo que es nuestro por construcción y quedó de una sesión anterior: las copias de trabajo, los temporales de
    una escritura atómica cortada y la papelera si quedó vacía. Nunca toca un archivo de la papelera, ni recorre una
    carpeta que no sea real y contenida (un junction no es un symlink para ``is_symlink()``, pero lleva afuera igual)."""
    etapas = control / _ETAPAS
    if _es_carpeta_contenida(control.parent, etapas):
        for entrada in os.scandir(etapas):
            if entrada.is_file(follow_symlinks=False) or entrada.is_symlink():
                os.unlink(entrada.path)
        with contextlib.suppress(OSError):
            os.rmdir(etapas)
    for entrada in os.scandir(control):
        if entrada.name.startswith(".") and entrada.name.endswith(".tmp") and entrada.is_file(follow_symlinks=False):
            os.unlink(entrada.path)
    if _es_carpeta_contenida(control.parent, control / _PAPELERA):
        with contextlib.suppress(OSError):
            os.rmdir(control / _PAPELERA)  # solo prospera si está vacía


def _borrar_pendiente(control: pathlib.Path) -> None:
    """Quita la intención: es lo ÚLTIMO que hace una sesión, y solo después de que el manifiesto quedó comprometido."""
    os.unlink(control / _PENDIENTE)


def _huella_de_papelera(control: pathlib.Path, ruta: str) -> str | None:
    apartado = control / _PAPELERA / _id_de_ruta(ruta)
    try:
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise_with_retry(apartado)
    except OSError:
        return None
    if identidad is None or tipo_de_enlace is not None or not stat.S_ISREG(identidad.st_mode):
        return None
    try:
        return _huella_de_archivo(apartado)
    except OSError:
        return None


def _restaurar_desde_papelera(raiz: pathlib.Path, control: pathlib.Path, ruta: str) -> bool:
    """Devuelve el archivo apartado a su ruta sin pisar nada. False si la ruta ya está ocupada."""
    destino = _destino(raiz, ruta)
    apartado = control / _PAPELERA / _id_de_ruta(ruta)
    try:
        _crear_carpeta_contenida(raiz, destino.parent, permitir_raiz=True)
        _mover_sin_reemplazar(apartado, destino)
    except FileExistsError:
        return False
    return True


def _recuperar(raiz: pathlib.Path) -> Manifiesto | None:
    """Prepara la raíz y deja consistente cualquier corte anterior (corre bajo el lock). Devuelve el manifiesto vigente."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(raiz)
    control = _asegurar_directorio_de_control(raiz)
    vigente = _interpretar(_leer_documento(control / _MANIFIESTO, "el manifiesto"), "el manifiesto")
    pendiente = _interpretar(
        _leer_documento(control / _PENDIENTE, "el manifiesto pendiente"), "el manifiesto pendiente"
    )
    if pendiente is None:
        _limpiar_control(control)
        return vigente

    rutas = {e.ruta for e in pendiente.entradas} | {e.ruta for e in (vigente.entradas if vigente else ())}
    observado = {
        clave_de_ruta(ruta): EstadoTrasCorte(
            disco=_observar_ruta(raiz, ruta), papelera=_huella_de_papelera(control, ruta)
        )
        for ruta in rutas
    }
    recuperacion = reconciliar_tras_corte(vigente, pendiente, observado)
    for ruta in recuperacion.restaurar:
        if not _restaurar_desde_papelera(raiz, control, ruta):
            logger.warning("No se pudo devolver %s desde la papelera: la ruta ya está ocupada", ruta)
    for descarte in recuperacion.descartar:
        with contextlib.suppress(FileNotFoundError):
            apartado = control / _PAPELERA / _id_de_ruta(descarte.ruta)
            if not _borrar_si_es_el_autorizado(apartado, descarte.sha256_autorizado):
                logger.warning(
                    "No se descartó %s de la papelera: ya no tiene la huella que se observó (cambió o desapareció)",
                    descarte.ruta,
                )
    _punto_de_corte("recuperacion_tras_papelera")
    actual = serializar_manifiesto(vigente) if vigente is not None else serializar_manifiesto(Manifiesto(entradas=()))
    resultante = serializar_manifiesto(recuperacion.manifiesto)
    if resultante != actual:
        _escribir_atomico(control / _MANIFIESTO, resultante)
    _punto_de_corte("recuperacion_tras_manifiesto")
    _limpiar_control(control)
    papelera = control / _PAPELERA
    if _es_carpeta_contenida(raiz, papelera):
        sobrantes = sorted(p.name for p in papelera.iterdir())
        if sobrantes:
            logger.warning(
                "La papelera de la salida tiene %d archivo(s) que no son nuestros; no se tocan", len(sobrantes)
            )
    _borrar_pendiente(control)  # lo último: hasta acá, un segundo corte vuelve a reconciliar
    return recuperacion.manifiesto


# ---------------------------------------------------------------------------
# Acciones sobre el disco (síncronas; corren en un hilo, una por ruta)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Trabajo:
    """Lo que hace falta para publicar una ruta: la entrada que quedaría y de dónde sale el contenido."""

    entrada: EntradaDeManifiesto
    fuente: pathlib.Path


def _huella_y_tamano_de_la_fuente(fuente: pathlib.Path) -> tuple[str, int]:
    try:
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise_with_retry(fuente)
    except OSError as exc:
        raise SalidaNoEscribibleError(f"no se pudo inspeccionar la fuente {fuente}: {exc}") from exc
    if identidad is None:
        raise SalidaNoEscribibleError(f"la fuente {fuente} no existe")
    if tipo_de_enlace is not None or not stat.S_ISREG(identidad.st_mode):
        raise SalidaNoEscribibleError(f"la fuente {fuente} no es un archivo regular")
    if identidad.st_size == 0:
        raise SalidaNoEscribibleError(f"la fuente {fuente} está vacía")
    if identidad.st_size > _TAMANO_MAXIMO_DE_ARCHIVO:
        raise SalidaNoEscribibleError(f"la fuente {fuente} pesa más de {_TAMANO_MAXIMO_DE_ARCHIVO} bytes")
    try:
        return _huella_de_archivo(fuente), identidad.st_size
    except OSError as exc:
        raise SalidaNoEscribibleError(f"no se pudo leer la fuente {fuente}: {exc}") from exc


def _copiar_a_etapa(control: pathlib.Path, trabajo: _Trabajo) -> pathlib.Path:
    """Copia la fuente a la etapa (en el mismo volumen que el mod) verificando que sigue siendo lo que se hasheó."""
    etapas = control / _ETAPAS
    _crear_carpeta_contenida(control.parent, etapas)
    etapa = etapas / _id_de_ruta(trabajo.entrada.ruta)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(etapa)
    digest = hashlib.sha256()
    total = 0
    try:
        with trabajo.fuente.open("rb") as origen:
            fd = os.open(etapa, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
            with os.fdopen(fd, "wb") as destino:
                while bloque := origen.read(_BLOQUE):
                    digest.update(bloque)
                    total += len(bloque)
                    destino.write(bloque)
                destino.flush()
                os.fsync(destino.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(etapa)
        raise
    if digest.hexdigest() != trabajo.entrada.sha256 or total != trabajo.entrada.tamano:
        with contextlib.suppress(OSError):
            os.unlink(etapa)
        raise _FuenteCambioError(f"la fuente {trabajo.fuente} cambió mientras se copiaba")
    return etapa


class _FuenteCambioError(Exception):
    """La fuente ya no es la que se hasheó. No se publica nada de ella."""


def _preparar_padre(raiz: pathlib.Path, ruta: str) -> pathlib.Path:
    destino = _destino(raiz, ruta)
    _crear_carpeta_contenida(raiz, destino.parent, permitir_raiz=True)
    return destino


def _podar_directorios_vacios(raiz: pathlib.Path, ruta: str) -> None:
    """Quita los directorios que quedaron vacíos bajo ``textures`` (nunca ``textures`` ni la raíz). ``rmdir`` no borra nada lleno."""
    textures = raiz / "textures"
    actual = _destino(raiz, ruta).parent
    while actual != textures and actual != raiz:
        try:
            exigir_contencion_fisica(raiz, actual)
            os.rmdir(actual)
        except (ContencionFisicaVioladaError, OSError):
            return
        actual = actual.parent


def _apartar(control: pathlib.Path, destino: pathlib.Path, ruta: str) -> pathlib.Path:
    """Mueve *destino* a la papelera sin pisar nada (si la casilla ya está ocupada, falla)."""
    papelera = control / _PAPELERA
    _crear_carpeta_contenida(control.parent, papelera)
    apartado = papelera / _id_de_ruta(ruta)
    _mover_sin_reemplazar(destino, apartado)
    return apartado


def _huella_del_apartado(apartado: pathlib.Path) -> str | None:
    """Huella de lo apartado, o None si no se pudo leer (no se puede verificar, así que no se trata como autorizado)."""
    try:
        return _huella_de_archivo(apartado)
    except OSError:
        return None


def _borrar_si_es_el_autorizado(apartado: pathlib.Path, autorizado: str) -> bool:
    """Borra lo apartado solo si su huella, calculada AHORA, es *autorizado*; si no (o no se pudo leer), no lo toca.

    Borrar es lo único que no se deshace: lo usan los borrados de la papelera que tienen un hueco entre la verificación y
    el borrado (el descarte de la recuperación y el original de un reemplazo, con la publicación en el medio).
    """
    if _huella_del_apartado(apartado) != autorizado:
        return False
    os.unlink(apartado)
    return True


def _devolver(apartado: pathlib.Path, destino: pathlib.Path) -> bool:
    """Devuelve lo apartado a su ruta sin pisar nada. False si la ruta quedó ocupada (lo apartado se conserva)."""
    try:
        _mover_sin_reemplazar(apartado, destino)
    except FileExistsError:
        return False
    return True


def _construir(
    raiz: pathlib.Path, control: pathlib.Path, trabajo: _Trabajo
) -> tuple[ResultadoPorRuta, EntradaDeManifiesto | None]:
    ruta = trabajo.entrada.ruta
    try:
        etapa = _copiar_a_etapa(control, trabajo)
    except (_FuenteCambioError, OSError) as exc:
        return ResultadoPorRuta(ruta, EfectoDeSalida.FALLIDA, f"no se pudo copiar la fuente: {exc}"), None
    _punto_de_corte("tras_etapa")
    try:
        destino = _preparar_padre(raiz, ruta)
        _mover_sin_reemplazar(etapa, destino)
    except FileExistsError:
        with contextlib.suppress(OSError):
            os.unlink(etapa)
        return ResultadoPorRuta(ruta, EfectoDeSalida.CONFLICTO, "apareció algo en la ruta antes de publicar"), None
    except (ContencionFisicaVioladaError, OSError) as exc:
        with contextlib.suppress(OSError):
            os.unlink(etapa)
        return ResultadoPorRuta(ruta, EfectoDeSalida.FALLIDA, f"no se pudo publicar: {exc}"), None
    _punto_de_corte("tras_publicar")
    return ResultadoPorRuta(ruta, EfectoDeSalida.PUBLICADA, ""), trabajo.entrada


def _reemplazar(
    raiz: pathlib.Path,
    control: pathlib.Path,
    trabajo: _Trabajo,
    vigente: EntradaDeManifiesto,
    autorizado: str,
) -> tuple[ResultadoPorRuta, EntradaDeManifiesto | None]:
    ruta = trabajo.entrada.ruta
    try:
        etapa = _copiar_a_etapa(control, trabajo)
    except (_FuenteCambioError, OSError) as exc:
        return ResultadoPorRuta(ruta, EfectoDeSalida.FALLIDA, f"no se pudo copiar la fuente: {exc}"), vigente
    _punto_de_corte("tras_etapa")
    try:
        destino = _preparar_padre(raiz, ruta)
        apartado = _apartar(control, destino, ruta)
    except FileNotFoundError:
        with contextlib.suppress(OSError):
            os.unlink(etapa)
        return ResultadoPorRuta(ruta, EfectoDeSalida.CONFLICTO, "el archivo desapareció antes de reemplazarlo"), None
    except (ContencionFisicaVioladaError, OSError) as exc:
        with contextlib.suppress(OSError):
            os.unlink(etapa)
        return ResultadoPorRuta(ruta, EfectoDeSalida.FALLIDA, f"no se pudo apartar el original: {exc}"), vigente
    _punto_de_corte("tras_apartar")
    if _huella_del_apartado(apartado) != autorizado:  # lo tocó un tercero entre planificar y apartar
        with contextlib.suppress(OSError):
            os.unlink(etapa)
        if _devolver(apartado, destino):
            return ResultadoPorRuta(ruta, EfectoDeSalida.CONFLICTO, "lo modificó un tercero: no se tocó"), None
        return ResultadoPorRuta(ruta, EfectoDeSalida.CONFLICTO, f"lo modificó un tercero y quedó en {apartado}"), None
    _punto_de_corte("tras_verificar")
    try:
        _mover_sin_reemplazar(etapa, destino)
    except OSError as exc:  # alguien ocupó la ruta en el instante vacío, o falló el disco: se devuelve el original
        with contextlib.suppress(OSError):
            os.unlink(etapa)
        if _devolver(apartado, destino):
            return ResultadoPorRuta(ruta, EfectoDeSalida.CONFLICTO, f"no se pudo publicar el reemplazo: {exc}"), vigente
        logger.warning("La ruta %s quedó ocupada; el original verificado quedó en %s", ruta, apartado)
        return ResultadoPorRuta(
            ruta, EfectoDeSalida.CONFLICTO, f"la ruta quedó ocupada; el original está en {apartado}"
        ), None
    _punto_de_corte("tras_publicar")
    if not _borrar_si_es_el_autorizado(apartado, autorizado):  # cambió en la papelera después de verificarlo
        logger.warning("El original apartado de %s cambió después de verificarlo; quedó en %s", ruta, apartado)
        return ResultadoPorRuta(
            ruta,
            EfectoDeSalida.REEMPLAZADA,
            f"el original apartado cambió después de verificarlo y quedó en {apartado}",
        ), trabajo.entrada
    return ResultadoPorRuta(ruta, EfectoDeSalida.REEMPLAZADA, ""), trabajo.entrada


def _retirar(
    raiz: pathlib.Path, control: pathlib.Path, ruta: str, autorizado: str, vigente: EntradaDeManifiesto
) -> tuple[ResultadoPorRuta, EntradaDeManifiesto | None]:
    try:
        destino = _preparar_padre(raiz, ruta)
        apartado = _apartar(control, destino, ruta)
    except FileNotFoundError:
        return ResultadoPorRuta(ruta, EfectoDeSalida.SOLTADA, "ya no estaba"), None
    except (ContencionFisicaVioladaError, OSError) as exc:
        return ResultadoPorRuta(ruta, EfectoDeSalida.FALLIDA, f"no se pudo apartar: {exc}"), vigente
    _punto_de_corte("tras_apartar")
    if _huella_del_apartado(apartado) != autorizado:  # lo tocó un tercero (o no se pudo leer): es suyo, no lo borramos
        if _devolver(apartado, destino):
            return ResultadoPorRuta(ruta, EfectoDeSalida.SOLTADA, "lo modificó un tercero: no se borró"), None
        return ResultadoPorRuta(ruta, EfectoDeSalida.CONFLICTO, f"lo modificó un tercero y quedó en {apartado}"), None
    _punto_de_corte("tras_verificar")
    os.unlink(apartado)
    _punto_de_corte("tras_borrar")
    _podar_directorios_vacios(raiz, ruta)
    return ResultadoPorRuta(ruta, EfectoDeSalida.RETIRADA, ""), None


# ---------------------------------------------------------------------------
# La sesión
# ---------------------------------------------------------------------------


def _validar_raiz(raiz: pathlib.Path) -> pathlib.Path:
    if not isinstance(raiz, pathlib.Path):
        raise SalidaNoEscribibleError(f"la raíz debe ser una ruta (Path), no {type(raiz).__name__}")
    if not raiz.is_absolute():
        raise SalidaNoEscribibleError(f"la raíz del mod de salida debe ser absoluta: {raiz}")
    if raiz.name.casefold() != NOMBRE_DEL_MOD_DE_SALIDA.casefold():
        raise SalidaNoEscribibleError(
            f"el nombre de la carpeta del mod de salida debe ser {NOMBRE_DEL_MOD_DE_SALIDA!r}"
        )
    if not raiz.parent.is_dir():
        raise SalidaNoEscribibleError(f"la carpeta que contiene el mod de salida debe existir: {raiz.parent}")
    try:
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise_with_retry(raiz)
    except OSError as exc:
        raise SalidaNoEscribibleError(f"no se pudo inspeccionar la raíz {raiz}: {exc}") from exc
    if tipo_de_enlace is not None:
        raise SalidaNoEscribibleError(f"la raíz del mod de salida es un enlace ({tipo_de_enlace}): {raiz}")
    if identidad is not None and not stat.S_ISDIR(identidad.st_mode):
        raise SalidaNoEscribibleError(f"la raíz del mod de salida no es un directorio: {raiz}")
    return raiz


@contextlib.asynccontextmanager
async def abrir_sesion_de_salida(
    raiz: pathlib.Path, *, coordinacion: CoordinacionDeSalida
) -> AsyncIterator[SesionDeSalida]:
    """Abre una sesión de escritura sobre el mod de salida *raiz*, bajo el lock cross-process.

    Al entrar toma el lock, crea la raíz si no existe y RECUPERA cualquier corte anterior. Es la ÚNICA forma de obtener
    una ``SesionDeSalida``.

    Raises:
        SalidaOcupadaError: otro proceso tiene el lock de esta raíz.
        SalidaNoEscribibleError: la raíz no es admisible, o un manifiesto de control es ilegible (no se tocó nada).
    """
    raiz = _validar_raiz(raiz)
    agent_id = f"native-parallax-output-{uuid.uuid4().hex}"
    async with coordinacion.sostener(raiz=raiz, agent_id=agent_id) as lock:
        vigente = await asyncio.to_thread(_recuperar, raiz)
        yield SesionDeSalida(_FICHA, raiz=raiz, lock=lock, vigente=vigente)


class SesionDeSalida:
    """Una sesión de escritura con el lock tomado. No se construye a mano: ver ``abrir_sesion_de_salida``."""

    def __init__(
        self,
        ficha: object,
        *,
        raiz: pathlib.Path,
        lock: SnapshotTransactionLock | None,
        vigente: Manifiesto | None,
    ) -> None:
        if ficha is not _FICHA:
            raise TypeError("una SesionDeSalida solo se obtiene con abrir_sesion_de_salida()")
        self._raiz = raiz
        self._lock = lock
        self._vigente = vigente
        self._ultimo: tuple[PlanDeSalida, dict[str, SalidaDeseada]] | None = None
        self._ocupada = False

    @contextlib.contextmanager
    def _una_operacion_a_la_vez(self) -> Iterator[None]:
        """Una sesión atiende UNA operación por vez: dos ``aplicar`` superpuestos se pisarían el pendiente y la papelera."""
        if self._ocupada:
            raise SalidaNoEscribibleError("la sesión está ocupada con otra operación: esperá a que termine")
        self._ocupada = True
        try:
            yield
        finally:
            self._ocupada = False

    async def _asegurar_lock(self) -> None:
        if self._lock is None:
            raise SalidaNoEscribibleError("la sesión no tiene el lock")
        await self._lock.assert_owned()

    @property
    def manifiesto(self) -> Manifiesto:
        """El manifiesto vigente (el último comprometido)."""
        return self._vigente if self._vigente is not None else Manifiesto(entradas=())

    async def planificar(self, deseadas: Iterable[SalidaDeseada]) -> PlanDeSalida:
        """Observa el disco y decide qué hacer con cada ruta (ver ``output.planificar``). No modifica nada."""
        with self._una_operacion_a_la_vez():
            await self._asegurar_lock()
            lista = list(deseadas)
            rutas = {d.ruta for d in lista if isinstance(d, SalidaDeseada)}
            rutas |= {e.ruta for e in self.manifiesto.entradas}
            observado = await asyncio.to_thread(_observar_rutas, self._raiz, sorted(rutas))
            plan = planificar(lista, self._vigente, observado)
            self._ultimo = (plan, {clave_de_ruta(d.ruta): d for d in lista})
            return plan

    async def aplicar(self, plan: PlanDeSalida, fuentes: Mapping[str, FuenteDeSalida]) -> ResultadoDeAplicacion:
        """Aplica *plan* (el último que devolvió ``planificar``) con las *fuentes* de las rutas que hay que construir.

        Raises:
            SalidaNoEscribibleError: el plan no es el último, faltan o sobran fuentes, o una fuente no es válida. No se
                tocó el disco. Una ruta cuya acción falla o encuentra al disco cambiado NO levanta: queda en el resultado.
            LockLeaseLostError: se perdió el lock durante la sesión (se aborta antes de la siguiente mutación).
        """
        with self._una_operacion_a_la_vez():
            await self._asegurar_lock()
            if self._ultimo is None or plan != self._ultimo[0]:
                raise SalidaNoEscribibleError("el plan no es el último que devolvió planificar() en esta sesión")
            deseadas = self._ultimo[1]
            trabajos, fallos_previos = await asyncio.to_thread(self._preparar_trabajos, plan, deseadas, fuentes)
            resultado = await self._ejecutar(plan, trabajos, fallos_previos)
            self._ultimo = None
            return resultado

    # --- validación previa (no muta el disco) --------------------------------------------------------------------

    def _preparar_trabajos(
        self, plan: PlanDeSalida, deseadas: dict[str, SalidaDeseada], fuentes: Mapping[str, FuenteDeSalida]
    ) -> tuple[dict[str, _Trabajo], dict[str, ResultadoPorRuta]]:
        pedidas = {
            clave_de_ruta(a.ruta)
            for a in plan.acciones
            if a.accion in (AccionDeSalida.CONSTRUIR, AccionDeSalida.RECONSTRUIR)
        }
        entregadas = {clave_de_ruta(r): f for r, f in fuentes.items()}
        if len(entregadas) != len(fuentes) or set(entregadas) != pedidas:
            faltan, sobran = sorted(pedidas - set(entregadas)), sorted(set(entregadas) - pedidas)
            raise SalidaNoEscribibleError(f"las fuentes no coinciden con el plan: faltan {faltan} y sobran {sobran}")
        trabajos: dict[str, _Trabajo] = {}
        fallos: dict[str, ResultadoPorRuta] = {}
        for accion in plan.acciones:
            clave = clave_de_ruta(accion.ruta)
            if clave not in pedidas:
                continue
            fuente = entregadas[clave]
            if not isinstance(fuente, FuenteDeSalida):
                raise SalidaNoEscribibleError(f"la fuente de {accion.ruta} debe ser una FuenteDeSalida")
            if clave_de_cache(fuente.procedencia) != deseadas[clave].clave_de_cache:
                raise SalidaNoEscribibleError(
                    f"la clave de caché de la fuente de {accion.ruta} no es la que se pidió al planificar"
                )
            huella, tamano = _huella_y_tamano_de_la_fuente(fuente.archivo)
            completa = len(os.fspath(_destino(self._raiz, accion.ruta)))
            if completa > _LARGO_MAXIMO_DE_RUTA_COMPLETA:
                fallos[clave] = ResultadoPorRuta(
                    accion.ruta,
                    EfectoDeSalida.FALLIDA,
                    f"la ruta completa mide {completa} caracteres y el máximo es {_LARGO_MAXIMO_DE_RUTA_COMPLETA}",
                )
                continue
            entrada = crear_entrada(deseadas[clave].ruta, huella, tamano, fuente.procedencia)
            trabajos[clave] = _Trabajo(entrada=entrada, fuente=fuente.archivo)
        return trabajos, fallos

    # --- ejecución -----------------------------------------------------------------------------------------------

    async def _ejecutar(
        self, plan: PlanDeSalida, trabajos: dict[str, _Trabajo], fallos_previos: dict[str, ResultadoPorRuta]
    ) -> ResultadoDeAplicacion:
        vigente = {clave_de_ruta(e.ruta): e for e in self.manifiesto.entradas}
        control = self._raiz / _CARPETA_DE_CONTROL
        con_disco = [
            a for a in plan.acciones if a.accion in _ACCIONES_CON_DISCO and clave_de_ruta(a.ruta) not in fallos_previos
        ]
        if not con_disco:
            return await self._comprometer_sin_disco(plan, vigente, fallos_previos)

        # La intención, ANTES de tocar nada: el manifiesto que quedaría si todo sale bien.
        optimista = dict(vigente)
        for accion in plan.acciones:
            clave = clave_de_ruta(accion.ruta)
            if clave in trabajos:
                optimista[clave] = trabajos[clave].entrada
            elif accion.accion is not AccionDeSalida.OMITIR and not _conserva_si_falla(accion, fallos_previos):
                optimista.pop(clave, None)
        pendiente = Manifiesto.ordenado(optimista.values())
        # Tras un corte (o con rutas fallidas) queda, por ruta, la entrada vigente o la pendiente: si esa mezcla no se
        # pudiera serializar, ni el compromiso ni la recuperación podrían escribirla. Se rechaza ANTES de tocar nada.
        cota = await asyncio.to_thread(cota_del_manifiesto_tras_un_corte, self._vigente, pendiente)
        if cota > TAMANO_MAXIMO_DEL_MANIFIESTO:
            raise SalidaNoEscribibleError(
                f"tras un corte, este lote podría dejar un manifiesto de hasta {cota} bytes y el máximo es "
                f"{TAMANO_MAXIMO_DEL_MANIFIESTO}: dividí el lote. No se tocó nada"
            )
        await self._asegurar_lock()
        await asyncio.to_thread(_escribir_atomico, control / _PENDIENTE, serializar_manifiesto(pendiente))
        await asyncio.to_thread(_punto_de_corte, "tras_pendiente")

        quedan = dict(vigente)
        rutas: list[ResultadoPorRuta] = []
        for accion in plan.acciones:
            clave = clave_de_ruta(accion.ruta)
            if clave in fallos_previos:
                rutas.append(fallos_previos[clave])
                if not _conserva_si_falla(accion, fallos_previos):
                    quedan.pop(clave, None)
            elif accion.accion not in _ACCIONES_CON_DISCO:
                _aplicar_sin_disco(accion, quedan, rutas)
            else:
                await self._asegurar_lock()
                resultado, nueva = await asyncio.to_thread(
                    _actuar_en_disco, self._raiz, accion, trabajos.get(clave), vigente.get(clave)
                )
                rutas.append(resultado)
                if nueva is None:
                    quedan.pop(clave, None)
                else:
                    quedan[clave] = nueva

        await asyncio.to_thread(_punto_de_corte, "antes_del_compromiso")
        final = Manifiesto.ordenado(quedan.values())
        await self._asegurar_lock()
        await asyncio.to_thread(_escribir_atomico, control / _MANIFIESTO, serializar_manifiesto(final))
        await asyncio.to_thread(_punto_de_corte, "tras_compromiso")
        # Limpiar y borrar el pendiente también exigen el lock: si se perdió, otra sesión puede estar usando esas carpetas.
        await self._asegurar_lock()
        await asyncio.to_thread(_limpiar_control, control)
        await asyncio.to_thread(_punto_de_corte, "tras_limpieza")
        await self._asegurar_lock()
        await asyncio.to_thread(_borrar_pendiente, control)  # lo último
        self._vigente = final
        return ResultadoDeAplicacion(rutas=_ordenadas(rutas), manifiesto=final)

    async def _comprometer_sin_disco(
        self, plan: PlanDeSalida, vigente: dict[str, EntradaDeManifiesto], fallos_previos: dict[str, ResultadoPorRuta]
    ) -> ResultadoDeAplicacion:
        """Un plan que no toca archivos: a lo sumo cambia el manifiesto (preservar y soltar), sin pendiente."""
        quedan = dict(vigente)
        rutas: list[ResultadoPorRuta] = []
        for accion in plan.acciones:
            clave = clave_de_ruta(accion.ruta)
            if clave in fallos_previos:
                rutas.append(fallos_previos[clave])
                if not _conserva_si_falla(accion, fallos_previos):
                    quedan.pop(clave, None)
            else:
                _aplicar_sin_disco(accion, quedan, rutas)
        final = Manifiesto.ordenado(quedan.values())
        if final != self.manifiesto:
            await self._asegurar_lock()
            await asyncio.to_thread(
                _escribir_atomico, self._raiz / _CARPETA_DE_CONTROL / _MANIFIESTO, serializar_manifiesto(final)
            )
            self._vigente = final
        return ResultadoDeAplicacion(rutas=_ordenadas(rutas), manifiesto=final)


_ACCIONES_CON_DISCO: Final = (AccionDeSalida.CONSTRUIR, AccionDeSalida.RECONSTRUIR, AccionDeSalida.RETIRAR)


def _ordenadas(rutas: list[ResultadoPorRuta]) -> tuple[ResultadoPorRuta, ...]:
    return tuple(sorted(rutas, key=lambda r: clave_de_ruta(r.ruta)))


def _conserva_si_falla(accion: AccionPlanificada, fallos_previos: dict[str, ResultadoPorRuta]) -> bool:
    """Si la validación previa descartó esta ruta: un reemplazo fallido deja intacto el archivo (y su entrada)."""
    return clave_de_ruta(accion.ruta) in fallos_previos and accion.accion is AccionDeSalida.RECONSTRUIR


def _aplicar_sin_disco(
    accion: AccionPlanificada, quedan: dict[str, EntradaDeManifiesto], rutas: list[ResultadoPorRuta]
) -> None:
    """Efecto de una acción que no toca el disco: omitir no cambia nada; preservar y soltar sacan la entrada."""
    if accion.accion is AccionDeSalida.OMITIR:
        rutas.append(ResultadoPorRuta(accion.ruta, EfectoDeSalida.OMITIDA, ""))
        return
    efecto = EfectoDeSalida.PRESERVADA if accion.accion is AccionDeSalida.PRESERVAR else EfectoDeSalida.SOLTADA
    rutas.append(ResultadoPorRuta(accion.ruta, efecto, ""))
    quedan.pop(clave_de_ruta(accion.ruta), None)


def _actuar_en_disco(
    raiz: pathlib.Path,
    accion: AccionPlanificada,
    trabajo: _Trabajo | None,
    vigente: EntradaDeManifiesto | None,
) -> tuple[ResultadoPorRuta, EntradaDeManifiesto | None]:
    """Ejecuta UNA acción que toca el disco y devuelve su resultado y la entrada que queda para esa ruta."""
    control = raiz / _CARPETA_DE_CONTROL
    if accion.accion is AccionDeSalida.CONSTRUIR and trabajo is not None:
        return _construir(raiz, control, trabajo)
    autorizado = accion.sha256_autorizado
    if autorizado is None or vigente is None:  # el plan lo garantiza; si no, no se toca nada
        raise SalidaNoEscribibleError(f"la acción sobre {accion.ruta} no trae la huella que la autoriza")
    if accion.accion is AccionDeSalida.RECONSTRUIR and trabajo is not None:
        return _reemplazar(raiz, control, trabajo, vigente, autorizado)
    if accion.accion is AccionDeSalida.RETIRAR:
        return _retirar(raiz, control, accion.ruta, autorizado, vigente)
    raise SalidaNoEscribibleError(f"la acción {accion.accion.name} sobre {accion.ruta} no tiene su trabajo")

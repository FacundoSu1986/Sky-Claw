"""Proveedor de normales de carpeta plana (NP-A0b, ADR 0014 §2.5): descubre los ``*_n.dds`` aptos bajo ``<raíz>/textures``.

Es la mitad «qué texturas hay» del lote: dada la raíz de una carpeta (o de un mod), devuelve por cada normal lo que el lote
necesita para planificar la salida (ruta de origen, ruta de salida ya validada, archivo, tamaño y huella del contenido) y,
aparte, qué omitió y por qué. Reemplaza a la enumeración PROVISORIA de ``cli.py`` (que sigue en uso hasta que se la cablee a
este módulo) y es lo que reutilizará el adaptador de MO2.

Propiedades (cada una con su test, y las familias con su ancla que enumera):

- **Solo lectura y sin política.** No crea, mueve ni borra nada, no usa red ni subprocesos, no decodifica (ni numpy ni Pillow)
  y no sabe del exportador DDS, del gate, del escritor ni de MO2: solo mira archivos y devuelve datos. Sus imports y sus
  llamadas al disco están congelados por AST.
- **Nunca sigue enlaces.** Un symlink o un junction dentro del árbol se omite (``ENLACE``) sin entrar a su destino; una raíz
  o una carpeta ``textures`` que sea un enlace se rechaza. En Windows ``DirEntry.is_dir(follow_symlinks=False)`` acepta un
  junction, así que TODO se inspecciona con ``link_kind_and_identity_or_raise`` (que lee el ``reparse tag``), igual que en
  ``iter_archivos_propios``. Cada carpeta, la raíz incluida, se revalida DESPUÉS de abrirla: una carpeta reemplazada por un
  enlace entre su ``lstat`` y su ``scandir`` haría entrar al árbol de afuera. Los ANCESTROS de la raíz sí se aceptan: los elige el operador
  (MO2 puede vivir bajo un junction en otro disco), a diferencia de lo que hay adentro, que puede ser ajeno.
- **Lo no observado no se da por ausente.** Una carpeta que no se pudo listar o un archivo que no se pudo inspeccionar ni leer
  quedan como omisiones (``CARPETA_ILEGIBLE``, ``ILEGIBLE``) y dejan ``completo=False``: quien retira salidas por «ya no
  figura» (el lote, con ``--retirar-no-incluidas``) la necesita completa. Las omisiones deliberadas (enlace, un ``*_p.dds``
  autoral con la misma ruta de salida, ruta inadmisible, vacío, demasiado grande, duplicado) no son errores de lectura y no la
  afectan.
- **El autoral no se pisa y se compara como la salida.** Una normal se omite si en CUALQUIER carpeta del árbol hay una
  ``*_p.dds`` con su misma ``clave_de_ruta`` (sin distinguir mayúsculas, que es como Windows y Skyrim resuelven el destino):
  ``Muros/x_n.dds`` y ``muros/x_p.dds`` son el mismo destino aunque un sistema que distingue mayúsculas los guarde en carpetas
  distintas. Se decide después del recorrido, así que el orden en que se visitaron las carpetas no cuenta.
- **Acotado por el tamaño validado.** La huella se calcula en bloques y no lee más de un byte pasado el tamaño con el que se
  validó el archivo; si el archivo crece o se achica mientras se lee, queda ``ILEGIBLE``. Solo se hashea lo que SOBREVIVE a los
  demás filtros y a la detección de duplicados (hashear lo que se va a omitir sería leerlo, hasta 1 GiB, para nada).
- **Un archivo que falla no aborta el descubrimiento; un exceso de entradas sí** (``DemasiadosArchivosError``): nunca trunca en
  silencio. El tope cuenta normales candidatas y omisiones juntas, porque las dos ocupan memoria.
- **Determinista.** Mismo árbol → misma salida, byte a byte: normales y omisiones van ordenadas por ``clave_de_ruta`` y no por
  el orden en que el sistema las entrega.
- **Vocabulario cerrado.** ``MotivoDeOmision`` tiene un caso por miembro y ``MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO`` lo clasifica:
  un motivo nuevo obliga a decidir si deja lo no observado.

Límites declarados:

- Entre el ``lstat`` de un archivo y su lectura hay una ventana en la que podría reemplazarse por un enlace (``open`` sigue
  enlaces). Es de solo lectura (a lo sumo se hashea el destino) y el lote vuelve a inspeccionar el origen, antes y después de
  decodificarlo, con su propia huella; no se cierra con ``O_NOFOLLOW`` porque no existe en Windows y no pude verificar acá una
  alternativa.
- Lo mismo vale para una carpeta (o un ancestro) que otro proceso reemplace por un enlace DESPUÉS de revalidarla, mientras se
  inspecciona lo que hay adentro o antes de que se calcule la huella, que ocurre tras el recorrido completo: las llamadas van
  por ruta y no por descriptor, y la biblioteca estándar no permite atar los hijos a la carpeta abierta en Windows (no hay
  ``dir_fd``). Revalidar al terminar cada carpeta solo achicaría la ventana, no la cierra. Exige otro proceso con permiso de
  escritura sobre la carpeta del operador mientras corre, y el efecto es el de arriba (de solo lectura).
- Un archivo regular que se reemplace por un FIFO entre su inspección y su lectura bloquearía el ``open`` (solo POSIX; en
  Windows no existen). Lo que YA es un FIFO (o cualquier cosa que no sea un archivo regular) al recorrer se omite como
  ``ILEGIBLE`` antes de abrirlo. Cerrar la ventana pide ``O_NONBLOCK``/``fstat`` sobre el descriptor, que no pude verificar en
  Windows.
- La huella se lee en el hilo que llama: es una función SÍNCRONA y puede tardar (lee todo lo que va a procesar el lote). Quien
  la use desde código ``async`` la corre con ``asyncio.to_thread``.
- La lectura acotada está hoy duplicada con ``lote._huella_acotada`` (el lote la hace antes y después de decodificar); el
  cableado de NP-A0 las unifica con un ancla que enumere. Nada de esto se ejerció sobre NTFS real ni con un junction real
  desde el desarrollo: la CI de Windows corre los tests (el junction, con ``mklink /J``).

Ver ADR 0014, «Actualización de NP-A0b».
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import stat
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final

from sky_claw.app.security.links import link_kind_and_identity_or_raise, same_file_identity
from sky_claw.local.native_parallax.output import (
    TAMANO_MAXIMO_DEL_ARCHIVO,
    OrigenDeEntrada,
    RutaDeSalidaInvalidaError,
    clave_de_ruta,
    validar_ruta_de_salida,
)

__all__ = [
    "MAXIMO_DE_ARCHIVOS_POR_DEFECTO",
    "MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO",
    "DemasiadosArchivosError",
    "Descubrimiento",
    "LimiteInvalidoError",
    "MotivoDeOmision",
    "NormalDescubierta",
    "Omitida",
    "ProveedorError",
    "RaizInvalidaError",
    "descubrir_normales",
]

#: Tope por defecto de entradas relevantes (normales candidatas y omisiones). Es una cota de cordura PROPIA, no un umbral de
#: calidad ni el límite de una fuente externa: acota la memoria de las dos listas y el trabajo de hashear.
MAXIMO_DE_ARCHIVOS_POR_DEFECTO: Final = 100_000

_NOMBRE_DE_TEXTURES: Final = "textures"
_SUFIJO_DE_ENTRADA: Final = "_n.dds"
_SUFIJO_DE_SALIDA: Final = "_p.dds"
_BLOQUE: Final = 1024 * 1024


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


class ProveedorError(Exception):
    """Base de los errores propios del proveedor."""


class RaizInvalidaError(ProveedorError, ValueError):
    """La raíz (o su carpeta ``textures``) no es admisible: no existe, es un enlace, no se puede leer o es ambigua."""


class DemasiadosArchivosError(ProveedorError):
    """Hay más entradas relevantes que el tope: se rechaza en vez de truncar en silencio."""


class LimiteInvalidoError(ProveedorError, ValueError):
    """El tope de entradas no es un entero positivo."""


# ---------------------------------------------------------------------------
# Vocabulario y valores
# ---------------------------------------------------------------------------


class MotivoDeOmision(Enum):
    """Por qué una textura (o una carpeta) quedó fuera. Vocabulario CERRADO: cada miembro tiene su caso en los tests."""

    #: Existe una ``*_p.dds`` con la misma ruta de salida, sin distinguir mayúsculas y en cualquier carpeta (puede ser autoral).
    YA_TIENE_PARALLAX = auto()
    #: Symlink o junction (archivo o carpeta): no se sigue.
    ENLACE = auto()
    #: La ruta de salida no pasa ``validar_ruta_de_salida`` (el detalle trae el error).
    RUTA_INVALIDA = auto()
    ARCHIVO_VACIO = auto()
    #: Pesa más que ``TAMANO_MAXIMO_DEL_ARCHIVO``.
    DEMASIADO_GRANDE = auto()
    #: ``OSError`` al inspeccionar o leer ESE archivo, o no es un archivo regular: no aborta el descubrimiento.
    ILEGIBLE = auto()
    #: ``OSError`` al listar ESA carpeta (o cambió mientras se abría): todo lo que hay adentro queda SIN OBSERVAR.
    CARPETA_ILEGIBLE = auto()
    #: Dos rutas con la misma ``clave_de_ruta``: fail-closed, se omiten AMBAS.
    DUPLICADA_POR_MAYUSCULAS = auto()


#: Los motivos que dejan algo SIN OBSERVAR. Los demás son decisiones de la política sobre algo que sí se vio.
MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO: Final = frozenset({MotivoDeOmision.ILEGIBLE, MotivoDeOmision.CARPETA_ILEGIBLE})


@dataclass(frozen=True, slots=True)
class NormalDescubierta:
    """Una normal apta para procesar. Cumple ``lote.NormalAProcesar`` (``ruta_de_origen``, ``ruta_de_salida``, ``ruta_absoluta``)."""

    ruta_de_origen: str  # relativa a la raíz, con "/", p. ej. "textures/muros/piedra_n.dds"
    ruta_de_salida: str  # "textures/muros/piedra_p.dds", ya validada con ``validar_ruta_de_salida``
    ruta_absoluta: pathlib.Path
    tamano: int
    origen: OrigenDeEntrada  # rol "normal" y la huella SHA-256 del contenido


@dataclass(frozen=True, slots=True)
class Omitida:
    ruta_de_origen: str
    motivo: MotivoDeOmision
    detalle: str


@dataclass(frozen=True, slots=True)
class Descubrimiento:
    normales: tuple[NormalDescubierta, ...]  # ordenadas por ``clave_de_ruta`` de la salida
    omitidas: tuple[Omitida, ...]  # ordenadas por ``clave_de_ruta`` del origen
    #: False si hubo algo que no se pudo observar (``MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO``): no se puede dar por ausente.
    completo: bool


# ---------------------------------------------------------------------------
# La raíz
# ---------------------------------------------------------------------------


def _exigir_limite(maximo_de_archivos: object) -> int:
    if isinstance(maximo_de_archivos, bool) or not isinstance(maximo_de_archivos, int) or maximo_de_archivos < 1:
        raise LimiteInvalidoError(f"maximo_de_archivos debe ser un entero positivo, no {maximo_de_archivos!r}")
    return maximo_de_archivos


def _resolver_textures(raiz: pathlib.Path) -> tuple[pathlib.Path, str, os.stat_result]:
    """La carpeta ``textures`` real de *raiz*, su nombre en disco y su identidad. Rechaza lo que no es una carpeta real."""
    if not isinstance(raiz, pathlib.Path):
        raise RaizInvalidaError(f"la raíz debe ser una ruta (Path), no {type(raiz).__name__}")
    absoluta = pathlib.Path(os.path.abspath(raiz))
    try:
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise(absoluta)
    except OSError as exc:
        raise RaizInvalidaError(f"no se pudo inspeccionar la raíz {absoluta}: {exc}") from exc
    if identidad is None:
        raise RaizInvalidaError(f"la raíz no existe: {absoluta}")
    if tipo_de_enlace is not None:
        raise RaizInvalidaError(f"la raíz es un enlace ({tipo_de_enlace}) y no se sigue: {absoluta}")
    if not stat.S_ISDIR(identidad.st_mode):
        raise RaizInvalidaError(f"la raíz no es una carpeta: {absoluta}")
    try:
        entradas = _listar(absoluta, identidad)
    except OSError as exc:
        raise RaizInvalidaError(f"no se pudo leer la carpeta {absoluta}: {exc}") from exc
    nombres = sorted(entrada.name for entrada in entradas if entrada.name.casefold() == _NOMBRE_DE_TEXTURES)
    if not nombres:
        raise RaizInvalidaError(f"la raíz debe tener una subcarpeta '{_NOMBRE_DE_TEXTURES}': {absoluta}")
    if len(nombres) > 1:
        raise RaizInvalidaError(
            f"la raíz es ambigua: hay varias carpetas '{_NOMBRE_DE_TEXTURES}' que solo se distinguen por mayúsculas "
            f"({', '.join(nombres)}): {absoluta}"
        )
    textures = absoluta / nombres[0]
    try:
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise(textures)
    except OSError as exc:
        raise RaizInvalidaError(f"no se pudo inspeccionar {textures}: {exc}") from exc
    if identidad is None:
        raise RaizInvalidaError(f"'{_NOMBRE_DE_TEXTURES}' desapareció mientras se abría la raíz: {textures}")
    if tipo_de_enlace is not None:
        raise RaizInvalidaError(f"'{_NOMBRE_DE_TEXTURES}' es un enlace ({tipo_de_enlace}) y no se sigue: {textures}")
    if not stat.S_ISDIR(identidad.st_mode):
        raise RaizInvalidaError(f"'{_NOMBRE_DE_TEXTURES}' no es una carpeta: {textures}")
    return textures, nombres[0], identidad


# ---------------------------------------------------------------------------
# El recorrido
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidata:
    """Una normal que pasó los filtros de su carpeta; falta detectar duplicados y calcular su huella."""

    ruta_de_origen: str
    ruta_de_salida: str
    ruta: pathlib.Path
    tamano: int


class _Acumulador:
    """Lo que se junta durante el recorrido, con el tope de entradas que acota la memoria."""

    def __init__(self, maximo_de_archivos: int) -> None:
        self._maximo = maximo_de_archivos
        self.candidatas: list[_Candidata] = []
        self.omitidas: list[Omitida] = []
        #: ``clave_de_ruta`` de CADA ``*_p.dds`` del árbol (de cualquier tipo de entrada): puede ser autoral y no se pisa.
        self.claves_de_parallax: set[str] = set()

    def _contar(self) -> None:
        if len(self.candidatas) + len(self.omitidas) > self._maximo:
            raise DemasiadosArchivosError(
                f"la carpeta tiene más de {self._maximo} entradas relevantes (normales y omisiones): dividila"
            )

    def omitir(self, ruta_de_origen: str, motivo: MotivoDeOmision, detalle: str) -> None:
        self.omitidas.append(Omitida(ruta_de_origen, motivo, detalle))
        self._contar()

    def agregar(self, candidata: _Candidata) -> None:
        self.candidatas.append(candidata)
        self._contar()


def _listar(carpeta: pathlib.Path, identidad_antes: os.stat_result) -> list[os.DirEntry[str]]:
    """Las entradas de *carpeta* ordenadas por nombre, revalidando DESPUÉS de abrirla (como ``iter_archivos_propios``).

    Sin esto, una carpeta reemplazada por un enlace entre su ``lstat`` y este ``scandir`` haría entrar al árbol de afuera.
    """
    with os.scandir(carpeta) as entradas:
        tipo_de_enlace, identidad_despues = link_kind_and_identity_or_raise(carpeta)
        if tipo_de_enlace is not None or not same_file_identity(identidad_antes, identidad_despues):
            raise OSError("la carpeta cambió mientras se abría")
        return sorted(entradas, key=lambda entrada: (entrada.name.casefold(), entrada.name))


def _examinar(
    entrada: os.DirEntry[str],
    relativa_de_carpeta: str,
    pendientes: list[tuple[pathlib.Path, str, os.stat_result]],
    acumulador: _Acumulador,
) -> None:
    nombre = entrada.name
    relativa = f"{relativa_de_carpeta}/{nombre}"
    ruta = pathlib.Path(entrada.path)
    try:
        tipo_de_enlace, identidad = link_kind_and_identity_or_raise(ruta)
    except OSError as exc:
        acumulador.omitir(relativa, MotivoDeOmision.ILEGIBLE, f"no se pudo inspeccionar: {exc}")
        return
    if identidad is None:  # desapareció entre el listado y la inspección: ya no está
        return
    if tipo_de_enlace is not None:
        acumulador.omitir(relativa, MotivoDeOmision.ENLACE, f"es un {tipo_de_enlace} y no se sigue")
        return
    if stat.S_ISDIR(identidad.st_mode):
        pendientes.append((ruta, relativa, identidad))
        return
    if not nombre.casefold().endswith(_SUFIJO_DE_ENTRADA):
        return
    if not stat.S_ISREG(identidad.st_mode):
        acumulador.omitir(relativa, MotivoDeOmision.ILEGIBLE, "no es un archivo regular")
        return
    tamano = identidad.st_size
    if tamano == 0:
        acumulador.omitir(relativa, MotivoDeOmision.ARCHIVO_VACIO, "el archivo está vacío")
    elif tamano > TAMANO_MAXIMO_DEL_ARCHIVO:
        acumulador.omitir(
            relativa,
            MotivoDeOmision.DEMASIADO_GRANDE,
            f"pesa {tamano} bytes y el tope es {TAMANO_MAXIMO_DEL_ARCHIVO}",
        )
    else:
        try:
            salida = validar_ruta_de_salida(relativa[: -len(_SUFIJO_DE_ENTRADA)] + _SUFIJO_DE_SALIDA)
        except RutaDeSalidaInvalidaError as exc:
            acumulador.omitir(relativa, MotivoDeOmision.RUTA_INVALIDA, f"la ruta de salida no es admisible: {exc}")
        else:
            acumulador.agregar(_Candidata(relativa, salida, ruta, tamano))


def _recorrer(
    textures: pathlib.Path, nombre_de_textures: str, identidad_de_textures: os.stat_result, acumulador: _Acumulador
) -> None:
    pendientes = [(textures, nombre_de_textures, identidad_de_textures)]
    while pendientes:
        carpeta, relativa_de_carpeta, identidad_antes = pendientes.pop()
        try:
            contenido = _listar(carpeta, identidad_antes)
        except OSError as exc:
            acumulador.omitir(
                relativa_de_carpeta, MotivoDeOmision.CARPETA_ILEGIBLE, f"no se pudo leer la carpeta ({exc})"
            )
            continue
        for entrada in contenido:
            if entrada.name.casefold().endswith(_SUFIJO_DE_SALIDA):
                acumulador.claves_de_parallax.add(clave_de_ruta(f"{relativa_de_carpeta}/{entrada.name}"))
            _examinar(entrada, relativa_de_carpeta, pendientes, acumulador)


# ---------------------------------------------------------------------------
# Duplicados, huella y resultado
# ---------------------------------------------------------------------------


def _huella_acotada(ruta: pathlib.Path, tamano_esperado: int) -> str:
    """SHA-256 de *ruta*, leyendo como máximo ``tamano_esperado + 1`` bytes (nunca más, aunque el archivo crezca).

    ``tamano_esperado`` es el tamaño con el que se validó el archivo. Si lo leído no coincide, el archivo cambió mientras se
    leía y se lanza ``OSError`` (el llamador lo traduce a ``ILEGIBLE``).
    """
    hasher = hashlib.sha256()
    leidos = 0
    with open(ruta, "rb") as archivo:
        while True:
            bloque = archivo.read(min(_BLOQUE, tamano_esperado + 1 - leidos))
            if not bloque:
                break
            leidos += len(bloque)
            if leidos > tamano_esperado:
                raise OSError("el archivo creció mientras se leía y pasó el tamaño con el que se validó")
            hasher.update(bloque)
    if leidos != tamano_esperado:
        raise OSError(
            f"el archivo cambió mientras se leía: se leyeron {leidos} bytes y se había medido en {tamano_esperado}"
        )
    return hasher.hexdigest()


def _es_completo(omitidas: tuple[Omitida, ...]) -> bool:
    return not any(omitida.motivo in MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO for omitida in omitidas)


def _sin_parallax_autoral(
    candidatas: list[_Candidata], claves_de_parallax: set[str], omitidas: list[Omitida]
) -> list[_Candidata]:
    """Las candidatas cuya salida no existe ya en el árbol, comparando como la salida (``clave_de_ruta``) y no por carpeta física.

    Es global y se decide DESPUÉS del recorrido: ``Muros/x_n.dds`` y ``muros/x_p.dds`` son el mismo destino aunque el sistema
    de archivos los guarde en carpetas distintas, y el orden en que se visitaron no puede decidir el resultado.
    """
    sobreviven: list[_Candidata] = []
    for candidata in candidatas:
        if clave_de_ruta(candidata.ruta_de_salida) in claves_de_parallax:
            omitidas.append(
                Omitida(
                    candidata.ruta_de_origen,
                    MotivoDeOmision.YA_TIENE_PARALLAX,
                    f"ya hay un {_SUFIJO_DE_SALIDA} con esa ruta de salida (puede ser autoral)",
                )
            )
        else:
            sobreviven.append(candidata)
    return sobreviven


def _sin_duplicadas(candidatas: list[_Candidata], omitidas: list[Omitida]) -> list[_Candidata]:
    """Las candidatas cuya salida no choca con otra (sin distinguir mayúsculas); de un choque se omiten TODAS."""
    por_clave: dict[str, list[_Candidata]] = {}
    for candidata in candidatas:
        por_clave.setdefault(clave_de_ruta(candidata.ruta_de_salida), []).append(candidata)
    sobreviven: list[_Candidata] = []
    for clave, grupo in por_clave.items():
        if len(grupo) == 1:
            sobreviven.append(grupo[0])
            continue
        omitidas.extend(
            Omitida(
                candidata.ruta_de_origen,
                MotivoDeOmision.DUPLICADA_POR_MAYUSCULAS,
                f"dos archivos de entrada dan la misma salida ({clave}): se omiten los dos",
            )
            for candidata in grupo
        )
    return sobreviven


def descubrir_normales(
    raiz: pathlib.Path, *, maximo_de_archivos: int = MAXIMO_DE_ARCHIVOS_POR_DEFECTO
) -> Descubrimiento:
    """Los ``*_n.dds`` aptos bajo ``<raiz>/textures`` y lo que se omitió, sin tocar nada. Es SÍNCRONA y puede tardar.

    Raises:
        RaizInvalidaError: la raíz o su carpeta ``textures`` no existe, es un enlace, no se puede leer o es ambigua.
        DemasiadosArchivosError: hay más de ``maximo_de_archivos`` entradas relevantes (normales y omisiones).
        LimiteInvalidoError: ``maximo_de_archivos`` no es un entero positivo.
    """
    maximo = _exigir_limite(maximo_de_archivos)
    textures, nombre_de_textures, identidad_de_textures = _resolver_textures(raiz)
    acumulador = _Acumulador(maximo)
    _recorrer(textures, nombre_de_textures, identidad_de_textures, acumulador)
    omitidas = acumulador.omitidas
    sin_autoral = _sin_parallax_autoral(acumulador.candidatas, acumulador.claves_de_parallax, omitidas)
    sobreviven = _sin_duplicadas(sin_autoral, omitidas)
    normales: list[NormalDescubierta] = []
    for candidata in sorted(sobreviven, key=lambda c: (clave_de_ruta(c.ruta_de_salida), c.ruta_de_salida)):
        try:
            huella = _huella_acotada(candidata.ruta, candidata.tamano)
        except OSError as exc:
            omitidas.append(Omitida(candidata.ruta_de_origen, MotivoDeOmision.ILEGIBLE, f"no se pudo leer: {exc}"))
            continue
        normales.append(
            NormalDescubierta(
                ruta_de_origen=candidata.ruta_de_origen,
                ruta_de_salida=candidata.ruta_de_salida,
                ruta_absoluta=candidata.ruta,
                tamano=candidata.tamano,
                origen=OrigenDeEntrada("normal", huella),
            )
        )
    omitidas_ordenadas = tuple(
        sorted(omitidas, key=lambda o: (clave_de_ruta(o.ruta_de_origen), o.ruta_de_origen, o.motivo.name, o.detalle))
    )
    return Descubrimiento(
        normales=tuple(normales), omitidas=omitidas_ordenadas, completo=_es_completo(omitidas_ordenadas)
    )

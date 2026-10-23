"""Diagnóstico previo del ``texconv.exe`` configurado (NP-A0c, ADR 0014).

Para exportar, el generador nativo de parallax ejecuta el ``texconv`` oficial de DirectXTex fijado por huella SHA-256
(``export.TEXCONV_PIN_VIGENTE``). Si el operador apunta a otro binario, el error aparece tarde —cuando ya se decodificó,
integró y midió una textura— y seco (``TexconvNoCoincideError``). Este módulo contesta ANTES, en lenguaje claro, qué le
pasa al ``texconv`` configurado.

Es **puro**: no ejecuta ``texconv``, no lanza procesos, no usa la red y **no calcula ninguna huella**. La comparación se
delega entera en ``export.verificar_texconv`` (la única implementación del pin y de la huella); acá solo se traduce su
resultado a un vocabulario cerrado de estados con un mensaje accionable. Por eso ``hashlib`` no se importa.

De dónde sale la ruta: el argumento ``ruta`` gana sobre la variable de entorno ``SKYCLAW_TEXCONV_EXE`` (la misma que
lee ``cli._resolver_texconv``). Un valor vacío o solo con espacios **no cuenta como ruta**: si el argumento viene en
blanco se mira la variable y, si las dos están en blanco, el estado es ``NO_CONFIGURADO``. La ruta NO se recorta: se usa
tal como la escribió el llamador (normalizada por ``pathlib``, sin resolverla ni hacerla absoluta) y en el mensaje se
muestra entre comillas angulares. Recortarla haría que el diagnóstico dijera «correcto» con una ruta que la exportación
va a rechazar —la CLI no recorta nada—, y un diagnóstico que aprueba lo que el exportador rechaza no sirve de nada.

Divergencia declarada con ``cli._resolver_texconv``: la CLI solo deja caer la cadena vacía
(``argumento or entorno.get(...)``), así que un argumento de SOLO espacios lo usa como ruta y falla por huella, mientras
que acá ese blanco cuenta como no configurado (contrato de NP-A0c). Todo lo demás coincide.

El pin se lee de ``export.TEXCONV_PIN_VIGENTE`` **en el momento de la llamada** (no se copia al importar): ``pin=None``
significa «usá el vigente», no «no hay pin». Para diagnosticar sin pin se pasa uno explícito o se corre con el vigente en
``None``.

Límites declarados:

- No ejecuta el binario: que el ``texconv`` fijado exporte de verdad se mide en Windows (``tests/test_texconv_real.py``
  y ``autoprueba`` de la CLI).
- Un archivo que desaparece entre la comprobación del delegado y su lectura se informa como ``NO_SE_PUDO_LEER`` con el
  mensaje del sistema operativo: el diagnóstico no adivina una carrera, la reporta.
- Todavía no está cableado a la CLI ni al registro de herramientas (eso es NP-A0): hoy se usa desde código.
"""

from __future__ import annotations

import dataclasses
import enum
import os
import pathlib
import stat
from collections.abc import Mapping
from typing import Final, Literal

from sky_claw.local.native_parallax import export

__all__ = [
    "ENTORNO_TEXCONV",
    "DiagnosticoDeTexconv",
    "EstadoDeTexconv",
    "OrigenDeLaRuta",
    "diagnosticar_texconv",
]

#: La misma variable que leen ``cli._resolver_texconv`` y el smoke real: una sola forma de decir dónde está el binario.
ENTORNO_TEXCONV: Final = "SKYCLAW_TEXCONV_EXE"

#: De dónde salió la ruta diagnosticada: del argumento o de la variable de entorno.
OrigenDeLaRuta = Literal["argumento", "entorno"]


class EstadoDeTexconv(enum.Enum):
    """Vocabulario CERRADO del estado del ``texconv`` configurado.

    Un estado nuevo obliga a escribir su escenario y su mensaje: ``tests/test_native_parallax_diagnostico_texconv.py``
    enumera los siete y falla si alguno queda sin caso.
    """

    CORRECTO = "correcto"
    NO_CONFIGURADO = "no_configurado"
    NO_ENCONTRADO = "no_encontrado"
    NO_ES_UN_ARCHIVO = "no_es_un_archivo"
    NO_SE_PUDO_LEER = "no_se_pudo_leer"
    HUELLA_DISTINTA = "huella_distinta"
    SIN_VERSION_FIJADA = "sin_version_fijada"


@dataclasses.dataclass(frozen=True, slots=True)
class DiagnosticoDeTexconv:
    """Qué le pasa al ``texconv`` configurado, en datos y en un mensaje para el operador.

    ``ruta`` es la que se usó (limpia) y ``origen_de_la_ruta`` de dónde salió; las dos van juntas o no van (una ruta sin
    origen, o un origen sin ruta, serían un dato ambiguo y no se pueden construir). ``version_esperada`` es la del pin
    con el que se comparó, si hay alguno. ``mensaje`` nunca está vacío: dice qué pasó y qué hacer.
    """

    estado: EstadoDeTexconv
    ruta: str | None
    origen_de_la_ruta: OrigenDeLaRuta | None
    version_esperada: str | None
    mensaje: str

    def __post_init__(self) -> None:
        if not isinstance(self.mensaje, str) or not self.mensaje.strip():
            raise ValueError("el diagnóstico necesita un mensaje: es lo que se le muestra al operador")
        if (self.ruta is None) != (self.origen_de_la_ruta is None):
            raise ValueError("la ruta y su origen van juntos: una ruta sin origen no dice de dónde salió")


def _ruta_configurada(
    ruta: pathlib.Path | str | None, variables: Mapping[str, str]
) -> tuple[str | None, OrigenDeLaRuta | None]:
    """La ruta a diagnosticar y de dónde salió; ``(None, None)`` si no hay ninguna (ausente, vacía o solo espacios).

    El blanco decide si HAY valor; no recorta el valor. Se usa tal como lo escribió el llamador porque eso es lo que la
    exportación va a usar (``cli._resolver_texconv`` tampoco recorta): aprobar una ruta recortada sería aprobar una ruta
    que el exportador rechaza.
    """
    candidatas: tuple[tuple[pathlib.Path | str | None, OrigenDeLaRuta], ...] = (
        (ruta, "argumento"),
        (variables.get(ENTORNO_TEXCONV), "entorno"),
    )
    for candidata, origen in candidatas:
        if candidata is None:
            continue
        texto = str(candidata)
        if texto.strip():
            return str(pathlib.Path(texto)), origen
    return None, None


def _como_se_ve(ruta: str | None) -> str:
    """La ruta entre comillas angulares y con los caracteres de control escapados.

    Los espacios de más se ven (el operador tiene que poder ver qué ruta va a usar la exportación) y un salto de línea
    del llamador no puede dibujar una línea falsa delante de él.
    """
    if ruta is None:
        return "sin ruta"
    visible = "".join(repr(caracter)[1:-1] if caracter < " " or caracter == "\x7f" else caracter for caracter in ruta)
    return f"«{visible}»"


def _clasificar_fallo(archivo: pathlib.Path, exc: export.TexconvNoCoincideError) -> tuple[EstadoDeTexconv, str | None]:
    """Separa las cuatro causas que ``verificar_texconv`` mete en un mismo tipo de error.

    El delegado distingue «no se pudo leer» con ``raise ... from OSError``; las otras (no existe, no es un archivo,
    huella distinta) llegan sin causa y se separan mirando el archivo, no adivinando por el texto del mensaje. El
    segundo valor es el motivo del sistema operativo, cuando lo hubo: un `ELOOP` o un `EACCES` del ``stat`` no se
    esconden detrás de «el sistema no dio un motivo».
    """
    if isinstance(exc.__cause__, OSError):
        return EstadoDeTexconv.NO_SE_PUDO_LEER, str(exc.__cause__)
    try:
        modo = archivo.stat().st_mode
    except (FileNotFoundError, NotADirectoryError, ValueError):
        # ``ValueError``: un nombre que el sistema no puede interpretar (p. ej. con un byte NUL) no nombra ningún archivo.
        return EstadoDeTexconv.NO_ENCONTRADO, None
    except OSError as err:
        return EstadoDeTexconv.NO_SE_PUDO_LEER, str(err)
    if stat.S_ISREG(modo):
        return EstadoDeTexconv.HUELLA_DISTINTA, None
    return EstadoDeTexconv.NO_ES_UN_ARCHIVO, None


def _mensaje(
    estado: EstadoDeTexconv,
    *,
    ruta: str | None,
    pin: export.TexconvPin | None,
    detalle: str | None = None,
) -> str:
    """El texto que ve el operador: qué pasó y qué hacer, sin jerga. Uno por estado, sin excepciones."""
    esperado = (
        f"el texconv oficial fijado (versión {pin.version}, sha256 {pin.sha256})"
        if pin is not None
        else "el texconv fijado"
    )
    match estado:
        case EstadoDeTexconv.CORRECTO:
            return f"El texconv configurado es {esperado}: {_como_se_ve(ruta)}. No hay nada que corregir."
        case EstadoDeTexconv.NO_CONFIGURADO:
            return (
                "No me dijeron dónde está el texconv: pasá --texconv RUTA o definí la variable "
                f"{ENTORNO_TEXCONV} con la ruta del binario oficial."
            )
        case EstadoDeTexconv.NO_ENCONTRADO:
            return f"No existe ningún archivo en {_como_se_ve(ruta)}: revisá la ruta y descargá {esperado}."
        case EstadoDeTexconv.NO_ES_UN_ARCHIVO:
            return (
                f"{_como_se_ve(ruta)} existe pero no es un archivo (¿es una carpeta?): apuntá al texconv.exe, no a la "
                "carpeta que lo contiene."
            )
        case EstadoDeTexconv.NO_SE_PUDO_LEER:
            return (
                f"No se pudo leer {_como_se_ve(ruta)}: {detalle or 'el sistema no dio un motivo'}. Revisá los permisos "
                "y que ningún programa lo tenga tomado."
            )
        case EstadoDeTexconv.HUELLA_DISTINTA:
            return (
                f"El archivo {_como_se_ve(ruta)} no es {esperado}: su huella SHA-256 es otra. Descargá el binario "
                "oficial de esa versión y volvé a apuntar la ruta; con un texconv distinto no se exporta nada."
            )
        case EstadoDeTexconv.SIN_VERSION_FIJADA:
            return (
                "No hay ninguna versión de texconv fijada en el proyecto (TEXCONV_PIN_VIGENTE es None), así que no hay "
                "contra qué comparar el binario: sin versión fijada no se exporta."
            )
    raise AssertionError(f"estado sin mensaje: {estado!r}")


def _resultado(
    estado: EstadoDeTexconv,
    *,
    ruta: str | None,
    origen: OrigenDeLaRuta | None,
    pin: export.TexconvPin | None,
    detalle: str | None = None,
) -> DiagnosticoDeTexconv:
    return DiagnosticoDeTexconv(
        estado=estado,
        ruta=ruta,
        origen_de_la_ruta=origen,
        version_esperada=pin.version if pin is not None else None,
        mensaje=_mensaje(estado, ruta=ruta, pin=pin, detalle=detalle),
    )


def diagnosticar_texconv(
    ruta: pathlib.Path | str | None = None,
    *,
    entorno: Mapping[str, str] | None = None,
    pin: export.TexconvPin | None = None,
) -> DiagnosticoDeTexconv:
    """Diagnostica el ``texconv`` configurado sin ejecutarlo; nunca lanza por un archivo ausente o ilegible.

    ``ruta`` gana sobre la variable ``SKYCLAW_TEXCONV_EXE`` de ``entorno`` (por defecto ``os.environ``). ``pin`` gana
    sobre ``export.TEXCONV_PIN_VIGENTE``, que se lee en este momento. Devuelve siempre un ``DiagnosticoDeTexconv``: el
    estado, la ruta usada, de dónde salió, la versión esperada y un mensaje en español para el operador.
    """
    variables = os.environ if entorno is None else entorno
    elegida, origen = _ruta_configurada(ruta, variables)
    efectivo = pin if pin is not None else export.TEXCONV_PIN_VIGENTE
    if elegida is None:
        return _resultado(EstadoDeTexconv.NO_CONFIGURADO, ruta=None, origen=None, pin=efectivo)
    if efectivo is None:
        return _resultado(EstadoDeTexconv.SIN_VERSION_FIJADA, ruta=elegida, origen=origen, pin=None)
    archivo = pathlib.Path(elegida)
    try:
        export.verificar_texconv(archivo, pin=efectivo)
    except export.TexconvNoCoincideError as exc:
        estado, detalle = _clasificar_fallo(archivo, exc)
        return _resultado(estado, ruta=elegida, origen=origen, pin=efectivo, detalle=detalle)
    return _resultado(EstadoDeTexconv.CORRECTO, ruta=elegida, origen=origen, pin=efectivo)

"""Escritura VERIFICABLE de ``modorganizer.ini`` (P5-C1, FIX 8).

Tracker: #672
ADR 0012 §13, §29.4

Por qué existe este módulo, separado del lector
-----------------------------------------------
``effective_runtime.mo2_ini`` es **read-only** por contrato (P5-C0). La
escritura del ``gamePath`` vivía dentro del binder y tenía dos defectos
medibles:

1. **BOM duplicado.** ``bytes.decode("utf-8", "surrogateescape")`` deja
   ``\\ufeff`` como primer carácter del texto; al re-encodear ese texto se
   emiten otra vez los tres bytes del BOM y encima se anteponía un BOM extra
   «porque el original lo tenía». El archivo quedaba con dos marcas.
2. **Re-serialización completa.** El INI se decodificaba entero y se volvía a
   escribir. Cualquier normalización (saltos de línea, espacios, orden) era una
   pérdida silenciosa de bytes ajenos.

La propiedad que este módulo hace cumplir: **la escritura es quirúrgica a nivel
de bytes**. Sólo la línea ``gamePath`` cambia; todo lo demás se preserva byte a
byte, y eso se VERIFICA estructuralmente (la lista de líneas difiere en
exactamente un elemento) además de con el readback del lector de MO2.

Equivalencia con Qt — qué se afirma y qué no
--------------------------------------------
``QT_QSETTINGS_EQUIVALENCE = NOT_CLAIMED``. En este entorno no hay Qt
instalado, así que no se puede DEMOSTRAR ejecutando ``QSettings`` que el archivo
que escribimos es el que Qt escribiría. Lo que sí se hace:

- El valor se emite en **ASCII puro** con la semántica de escape que
  ``_desescapar_bytes`` emula de Qt 5.15 (``\\\\`` para la barra, ``\\xNN`` para
  el resto, sin dígitos hex ambiguos después de un escape).
- Se **restringen los formatos admitidos**: UTF-8/ASCII de 8 bits con BOM
  opcional único, saltos LF o CRLF uniformes, sin NUL, sin UTF-16/32. Lo que no
  entra, falla cerrado ANTES de tocar el archivo.

Un formato no admitido no deja el INI mutado: la admisión corre primero.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import pathlib
import tempfile
from collections.abc import Callable
from typing import Protocol

from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import leer_game_path_de_mo2
from sky_claw.local.frozen_runtime.effective_runtime.models import ObservationState
from sky_claw.local.frozen_runtime.errors import FrozenRuntimeError

__all__ = [
    "EscrituraIniNoVerificadaError",
    "FormatoIni",
    "FormatoIniNoAdmitidoError",
    "Mo2IniEscrituraError",
    "IniRetenido",
    "actualizar_game_path_en_ini",
    "actualizar_game_path_por_handle",
    "admitir_formato_ini",
    "prever_bytes",
    "codificar_qt_byte_array",
]

_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_NO_ADMITIDOS = (
    b"\xff\xfe",  # UTF-16 LE
    b"\xfe\xff",  # UTF-16 BE
    b"\xff\xfe\x00\x00",  # UTF-32 LE (prefijo de UTF-16 LE: se chequea primero el largo)
    b"\x00\x00\xfe\xff",  # UTF-32 BE
)

#: Bytes que se emiten LITERALES dentro de ``@ByteArray(...)``. Es una lista
#: blanca, no negra: agregar un carácter acá es una decisión explícita sobre qué
#: puede aparecer crudo en un valor que Qt parsea. Quedan afuera la barra
#: (escape), el ``)`` (cierra el ``@ByteArray``), y los metacaracteres del parser
#: INI de Qt (``;`` comenta, ``#`` comenta en algunas variantes, ``,``/``=``/``[``/
#: ``]`` separan valores y listas). Todos ellos se emiten como ``\\xNN``, que Qt
#: decodifica al byte exacto.
_LITERALES_PERMITIDOS = frozenset(chr(c) for c in range(0x20, 0x7F) if chr(c) not in "\\;,=[]()#")
_DIGITOS_HEX = frozenset("0123456789abcdefABCDEF")


class Mo2IniEscrituraError(FrozenRuntimeError):
    """Error base de la escritura de ``modorganizer.ini``."""


class FormatoIniNoAdmitidoError(Mo2IniEscrituraError):
    """El INI está en un formato cuya preservación no se puede demostrar."""


class EscrituraIniNoVerificadaError(Mo2IniEscrituraError):
    """La escritura ocurrió pero el readback no devuelve el valor pedido."""


@dataclasses.dataclass(frozen=True, slots=True)
class FormatoIni:
    """Formato admitido de un INI, ya validado.

    ``tiene_bom`` y ``crlf`` son las dos únicas decisiones de serialización que
    el escritor toma. Todo lo demás lo dicta el archivo original.
    """

    tiene_bom: bool
    crlf: bool

    @property
    def separador(self) -> str:
        return "\r\n" if self.crlf else "\n"


def _clases_de_salto(texto: str) -> set[str]:
    """Clases de salto de línea presentes: ``crlf``, ``lf`` o ``cr``.

    Se cuenta por barrido y no con ``splitlines`` porque ``str.splitlines``
    también parte en ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``, ``\\x85`` y
    ``U+2028``/``U+2029``. Una ruta con ``\\x85`` (NEL) haría que el escritor
    viera dos líneas donde el archivo tiene una y metiera un salto en medio del
    valor: corrupción silenciosa del ``gamePath``.
    """
    clases: set[str] = set()
    indice = 0
    largo = len(texto)
    while indice < largo:
        caracter = texto[indice]
        if caracter == "\r":
            if indice + 1 < largo and texto[indice + 1] == "\n":
                clases.add("crlf")
                indice += 2
                continue
            clases.add("cr")
        elif caracter == "\n":
            clases.add("lf")
        indice += 1
    return clases


def admitir_formato_ini(contenido: bytes) -> FormatoIni:
    """Valida que el INI esté en un formato cuya preservación es demostrable.

    Raises:
        FormatoIniNoAdmitidoError: BOM UTF-16/32, byte NUL, saltos de línea
            mezclados, o un contenido que no re-encodea byte a byte.
    """
    for bom in _BOM_NO_ADMITIDOS:
        if contenido.startswith(bom):
            raise FormatoIniNoAdmitidoError(
                f"modorganizer.ini empieza con el BOM {bom.hex()}: sólo se admite UTF-8 "
                "(con o sin BOM) o bytes de 8 bits. La preservación de UTF-16/32 no es "
                "demostrable en este entorno (QT_QSETTINGS_EQUIVALENCE = NOT_CLAIMED)"
            )
    if b"\x00" in contenido:
        raise FormatoIniNoAdmitidoError(
            "modorganizer.ini contiene bytes NUL: es un archivo UTF-16/32 sin BOM o está "
            "corrupto. Escribirlo re-serializado destruiría el contenido"
        )

    tiene_bom = contenido.startswith(_BOM_UTF8)
    cuerpo = contenido[len(_BOM_UTF8) :] if tiene_bom else contenido
    texto = cuerpo.decode("utf-8", "surrogateescape")
    if texto.encode("utf-8", "surrogateescape") != cuerpo:
        # Invariante del codec: con ``surrogateescape`` siempre se cumple. Se
        # deja como puerta porque de ella depende TODA la afirmación de
        # preservación byte a byte de este módulo.
        raise FormatoIniNoAdmitidoError(
            "el contenido no re-encodea byte a byte con utf-8/surrogateescape: no se puede "
            "afirmar que la escritura preserve los bytes ajenos"
        )

    clases = _clases_de_salto(texto)
    if "cr" in clases:
        raise FormatoIniNoAdmitidoError(
            "modorganizer.ini usa retorno de carro solo (\\r) como salto de línea: es el formato "
            "clásico de Mac, que Qt no parsea como líneas. Convertirlo sería normalizar el archivo"
        )
    if len(clases) > 1:
        raise FormatoIniNoAdmitidoError(
            f"modorganizer.ini mezcla clases de salto de línea {sorted(clases)}: no se puede "
            "elegir un separador sin normalizar el archivo (y normalizarlo pierde bytes ajenos)"
        )
    return FormatoIni(tiene_bom=tiene_bom, crlf=clases == {"crlf"})


def codificar_qt_byte_array(ruta: str | pathlib.Path) -> str:
    """Serializa una ruta en ``@ByteArray(...)`` con escape de byte array de Qt.

    Reglas (las mismas que ``mo2_ini._desescapar_bytes`` emula del lector):

    - ``\\\\`` para la barra invertida;
    - ``\\xNN`` para todo byte fuera de la lista blanca;
    - **nunca** un dígito hexadecimal literal inmediatamente después de un
      ``\\xNN``: el lector acumula todos los dígitos hex sucesivos, así que
      ``\\xc3`` + ``1`` sería el byte ``0xC31`` (fuera de Latin-1) y el valor
      quedaría irrecuperable.

    El resultado es ASCII puro: es lo único que hace la codificación
    independiente del codec con que Qt lea el archivo.
    """
    cadena = os.fspath(ruta)
    datos = cadena.encode("utf-8", "surrogatepass")
    piezas: list[str] = []
    escape_hex_previo = False
    for byte in datos:
        caracter = chr(byte)
        if byte == 0x5C:  # barra invertida
            piezas.append("\\\\")
            escape_hex_previo = False
            continue
        if caracter in _LITERALES_PERMITIDOS and not (escape_hex_previo and caracter in _DIGITOS_HEX):
            piezas.append(caracter)
            escape_hex_previo = False
            continue
        piezas.append(f"\\x{byte:02x}")
        escape_hex_previo = True
    return f"@ByteArray({''.join(piezas)})"


def _terminador_de(linea: str) -> str:
    return "\r" if linea.endswith("\r") else ""


def _con_terminador(linea: str, formato: FormatoIni) -> str:
    """Garantiza que *linea* termine con el separador del archivo (sin el ``\\n``)."""
    if formato.crlf and not linea.endswith("\r"):
        return linea + "\r"
    return linea


def _es_encabezado(linea: str) -> str | None:
    limpia = linea.strip()
    if limpia.startswith("[") and limpia.endswith("]"):
        return limpia[1:-1].strip()
    return None


def _es_game_path(linea: str) -> bool:
    return linea.strip().lower().startswith("gamepath=")


def _reemplazar_valor(linea: str, valor: str) -> str:
    """Conserva el prefijo (``gamePath=`` y su indentación) y el terminador."""
    terminador = _terminador_de(linea)
    cuerpo = linea[: -len(terminador)] if terminador else linea
    indice = cuerpo.index("=")
    return f"{cuerpo[: indice + 1]}{valor}{terminador}"


def _construir_lineas(texto: str, valor: str, formato: FormatoIni) -> tuple[list[str], tuple[int, ...]]:
    """Devuelve las líneas modificadas y los índices de las líneas tocadas.

    Los índices son la única forma de verificar después que la edición fue
    quirúrgica: comparar por contenido no distingue «inserté una línea» de
    «reemplacé una y perdí otra».
    """
    lineas = texto.split("\n")
    en_general = False
    seccion_general_vista = False
    tocadas: list[int] = []
    salida: list[str] = []

    for linea in lineas:
        encabezado = _es_encabezado(linea)
        if encabezado is not None:
            if encabezado == "General":
                en_general = True
                seccion_general_vista = True
                salida.append(linea)
                continue
            if en_general and not tocadas:
                tocadas.append(len(salida))
                salida.append(_con_terminador(f"gamePath={valor}", formato))
            en_general = False

        if en_general and not tocadas and _es_game_path(linea):
            tocadas.append(len(salida))
            salida.append(_reemplazar_valor(linea, valor))
            continue

        salida.append(linea)

    if not seccion_general_vista:
        # No había [General]: se agrega al principio, conservando el resto. La
        # línea `gamePath` lleva el terminador del archivo: sin él, el `"\n".join`
        # dejaría un LF suelto en un INI CRLF (CodeRabbit sobre #843).
        salida.insert(0, "[General]" + ("\r" if formato.crlf else ""))
        salida.insert(1, _con_terminador(f"gamePath={valor}", formato))
        tocadas = [0, 1]
    elif en_general and not tocadas:
        # [General] era la última sección: se agrega al final de ella.
        if salida and salida[-1] == "":
            # El archivo termina con salto de línea: la línea nueva va ANTES del
            # último elemento vacío para no dejar un renglón en blanco de más.
            tocadas = [len(salida) - 1]
            salida.insert(len(salida) - 1, _con_terminador(f"gamePath={valor}", formato))
        else:
            salida[-1] = _con_terminador(salida[-1], formato)
            tocadas = [len(salida)]
            salida.append(f"gamePath={valor}")

    return salida, tuple(tocadas)


def _verificar_preservacion(
    antes: list[str],
    despues: list[str],
    *,
    tocadas: tuple[int, ...],
) -> None:
    """Comprueba que la edición se limitó a las líneas declaradas.

    Es la verificación estructural de «preservar bytes ajenos»: si el escritor
    normalizó un salto, reordenó una clave o perdió una línea, el resto de la
    lista deja de ser idéntica al original.
    """
    if not tocadas:
        raise EscrituraIniNoVerificadaError("verificación de preservación: la edición no tocó ninguna línea")

    if len(despues) == len(antes):
        # REEMPLAZO en el lugar: sólo pueden diferir las líneas declaradas, y
        # tanto la versión previa como la nueva tienen que ser gamePath.
        #
        # ``despues == antes`` es un NO-OP legítimo: el valor ya era el pedido.
        # Antes se rechazaba, así que llamar dos veces al escritor con la misma
        # ruta fallaba en la segunda (Codex sobre #843) y la rama de idempotencia
        # de ``actualizar_game_path_en_ini`` era inalcanzable.
        if despues == antes:
            return
        distintas = [indice for indice, (a, d) in enumerate(zip(antes, despues, strict=True)) if a != d]
        if distintas != list(tocadas):
            raise EscrituraIniNoVerificadaError(
                f"verificación de preservación: se esperaba modificar {list(tocadas)} y se modificó {distintas}"
            )
        for indice in tocadas:
            if not _es_game_path(antes[indice]) or not _es_game_path(despues[indice]):
                raise EscrituraIniNoVerificadaError(
                    f"verificación de preservación: la línea {indice} no es gamePath en ambos lados"
                )
        return

    # INSERCIÓN: quitando las líneas declaradas, el resto tiene que ser el original.
    resto = [linea for indice, linea in enumerate(despues) if indice not in set(tocadas)]
    if resto != antes:
        raise EscrituraIniNoVerificadaError(
            "verificación de preservación: la edición de gamePath alteró líneas ajenas "
            f"({len(antes)} líneas antes, {len(despues)} después, tocadas {tocadas})"
        )
    for indice in tocadas:
        linea = despues[indice]
        if _es_game_path(linea) or _es_encabezado(linea) == "General":
            continue
        raise EscrituraIniNoVerificadaError(
            f"verificación de preservación: la línea {indice} no es gamePath ni [General] ({linea!r})"
        )


def prever_bytes(contenido: bytes, nuevo_game_path: str | pathlib.Path) -> bytes:
    """Bytes que ``actualizar_game_path_en_ini`` escribiría sobre *contenido*.

    Es una función PURA sobre el contenido previo y no toca el disco. Existe por
    dos razones, y las dos son de corrección:

    1. El escritor y la previsión no pueden divergir: ``actualizar_game_path_en_ini``
       la usa para construir lo que escribe.
    2. La restauración tras un reinicio necesita distinguir «estos bytes los
       escribió la transición» de «los escribió un tercero» sin tener el proceso
       vivo. Con la previsión se compara contra lo que la transición HABRÍA
       escrito, que es exactamente el discriminador que le falta a un rollback
       que sólo tiene el snapshot.

    Raises:
        FormatoIniNoAdmitidoError: el formato no se admite.
        EscrituraIniNoVerificadaError: la edición no se limitó a ``gamePath``.
    """
    formato = admitir_formato_ini(contenido)
    cuerpo = contenido[len(_BOM_UTF8) :] if formato.tiene_bom else contenido
    texto = cuerpo.decode("utf-8", "surrogateescape")
    valor = codificar_qt_byte_array(nuevo_game_path)
    lineas_antes = texto.split("\n")
    lineas_despues, tocadas = _construir_lineas(texto, valor, formato)
    _verificar_preservacion(lineas_antes, lineas_despues, tocadas=tocadas)
    nuevo_cuerpo = "\n".join(lineas_despues).encode("utf-8", "surrogateescape")
    return (_BOM_UTF8 if formato.tiene_bom else b"") + nuevo_cuerpo


def actualizar_game_path_en_ini(
    ini_path: pathlib.Path,
    nuevo_game_path: str | pathlib.Path,
) -> None:
    """Actualiza ``gamePath`` bajo ``[General]`` preservando el resto byte a byte.

    Orden de operaciones, deliberado:

    1. **Admisión de formato** (antes de cualquier escritura): un formato no
       admitido no muta el archivo.
    2. **Edición quirúrgica + verificación estructural** (``prever_bytes``).
    3. **Re-lectura inmediatamente antes del reemplazo**: si el archivo cambió
       desde la lectura, se falla cerrado. ``os.replace`` es atómico para la
       ESCRITURA; no comprueba que el destino siga siendo el que se leyó, y esa
       ventana es la que MO2 puede llenar (CodeRabbit sobre #843).
    4. **Escritura atómica**: temporal en el MISMO directorio, ``fsync``,
       ``os.replace``.
    5. **Readback con el lector de MO2**: si no devuelve la ruta pedida, se
       levanta ``EscrituraIniNoVerificadaError``. Un escritor que no verifica su
       propio readback no escribe: publica una intención.

    Raises:
        FormatoIniNoAdmitidoError: el archivo no existe o su formato no se admite.
        EscrituraIniNoVerificadaError: la preservación, la re-lectura o el readback fallaron.
    """
    ruta = pathlib.Path(os.path.abspath(os.fspath(ini_path)))
    try:
        contenido = ruta.read_bytes()
    except FileNotFoundError as exc:
        raise FormatoIniNoAdmitidoError(f"modorganizer.ini no existe en {ruta}: no hay formato que preservar") from exc
    except OSError as exc:
        raise FormatoIniNoAdmitidoError(f"modorganizer.ini no se pudo leer ({ruta}): {exc}") from exc

    bytes_finales = prever_bytes(contenido, nuevo_game_path)

    if bytes_finales != contenido:
        # Cierre de la ventana lectura→reemplazo DENTRO del escritor. El binder
        # ya compara la identidad del INI bajo su lock, pero el lock no cubre a
        # MO2 ni a otro escritor externo.
        try:
            actuales = ruta.read_bytes()
        except OSError as exc:
            raise EscrituraIniNoVerificadaError(f"no se pudo releer {ruta} antes del reemplazo: {exc}") from exc
        if actuales != contenido:
            raise EscrituraIniNoVerificadaError(
                f"{ruta}: el archivo cambió entre la lectura y el reemplazo "
                f"(sha256 {hashlib.sha256(actuales).hexdigest()} != {hashlib.sha256(contenido).hexdigest()}): "
                "no se sobrescribe una escritura ajena"
            )
        _escribir_atomico(ruta, bytes_finales)
    # Si ``bytes_finales == contenido`` el valor ya era el pedido: no se toca el
    # archivo (ni se reemplaza el inodo). Reemplazarlo cambiaría el mtime y
    # dispararía la detección de estabilidad del preflight sin motivo.

    evidencia = leer_game_path_de_mo2(ruta)
    esperado = os.fspath(nuevo_game_path)
    if evidencia.state is not ObservationState.OBSERVED or evidencia.path != esperado:
        raise EscrituraIniNoVerificadaError(
            f"verificación post-escritura de modorganizer.ini: se escribió {esperado!r} y el lector "
            f"devolvió state={evidencia.state.value} path={evidencia.path!r} ({evidencia.detail})"
        )


def _escribir_atomico(ruta: pathlib.Path, contenido: bytes) -> None:
    """Temporal en el mismo directorio + ``fsync`` + ``os.replace``."""
    try:
        fd, tmp_ruta = tempfile.mkstemp(dir=ruta.parent, prefix=".modorganizer.ini.", suffix=".tmp")
    except OSError as exc:
        raise Mo2IniEscrituraError(f"no se pudo crear el temporal de modorganizer.ini: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contenido)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_ruta, ruta)
    except BaseException as exc:
        if os.path.exists(tmp_ruta):
            with contextlib.suppress(OSError):
                os.unlink(tmp_ruta)
        if isinstance(exc, Exception):
            raise Mo2IniEscrituraError(f"no se pudo escribir modorganizer.ini: {exc}") from exc
        raise


class IniRetenido(Protocol):
    """Lo mínimo que el escritor por handle necesita de un INI retenido.

    Es un ``Protocol`` y no la clase concreta de ``mo2_ini_exclusion`` para que
    este módulo no dependa del gate: el escritor sólo necesita leer y escribir
    por un handle que alguien más sostiene y garantiza.
    """

    def leer_bytes(self) -> bytes:
        """Bytes actuales del archivo, por el handle retenido."""
        ...

    def escribir_bytes(self, datos: bytes, *, al_escribir: Callable[[], None] | None = None) -> None:
        """Reemplaza el contenido del archivo por ``datos``, por el handle retenido.

        ``al_escribir`` se invoca inmediatamente antes de la primera operación
        que muta. Un doble de test que lo ignore deja el flag de intento sin
        marcar y el rollback trata la escritura propia como ajena.
        """
        ...


def actualizar_game_path_por_handle(
    retenido: IniRetenido,
    nuevo_game_path: str | pathlib.Path,
    *,
    al_escribir: Callable[[], None] | None = None,
) -> bytes:
    """Hermano de :func:`actualizar_game_path_en_ini` para un INI ya retenido.

    Existe porque la exclusión de ``modorganizer.ini`` (``mo2_ini_exclusion``)
    se toma con ``DELETE`` en el acceso propio, y con esa configuración el
    reemplazo por ruta es imposible: el handle se bloquea a sí mismo. Medido:
    ``os.replace`` → ``ERROR_ACCESS_DENIED`` (5). La única mutación posible
    mientras se sostiene la exclusión es escribir por el propio handle.

    Lo que COMPARTE con el hermano, y es lo que importa:

    - la misma edición quirúrgica (``prever_bytes``), así que los dos escriben
      exactamente los mismos bytes para el mismo par (contenido, ruta);
    - la misma verificación de preservación estructural;
    - el mismo readback obligatorio antes de declarar éxito;
    - el mismo no-op cuando el valor ya era el pedido.

    Lo que NO comparte, y no puede: la atomicidad. ``os.replace`` publica el
    archivo nuevo de una vez; escribir por handle lo deja a medio escribir si el
    proceso muere. Esa mitad la cubre ``binder_recovery``, que captura los bytes
    previos antes de mutar.

    Args:
        retenido: handle con exclusión de mutación ya establecida.
        nuevo_game_path: ruta que debe quedar en ``gamePath``.
        al_escribir: se invoca INMEDIATAMENTE antes de mutar el archivo, y sólo
            si se va a mutar. El binder lo usa para marcar «esta transición
            intentó escribir» sin tener que adivinar: entre el inicio de esta
            función y la escritura están la admisión de formato y el no-op, que
            pueden terminar sin tocar el archivo. Marcar antes de llamarla haría
            que un estado intermedio de un TERCERO se atribuyera a la transición
            (auditoría del PR #851).

    Returns:
        Los bytes que quedaron en el archivo.

    Raises:
        FormatoIniNoAdmitidoError: el contenido retenido no tiene un formato admitido.
        EscrituraIniNoVerificadaError: la preservación o el readback fallaron.
    """
    contenido = retenido.leer_bytes()
    bytes_finales = prever_bytes(contenido, nuevo_game_path)

    if bytes_finales == contenido:
        # El valor ya era el pedido. No se escribe: escribir cambiaría el mtime
        # y dispararía la detección de estabilidad del preflight sin motivo.
        return contenido

    try:
        # El hook viaja HASTA el punto de mutación real: dentro de
        # ``escribir_bytes`` hay una revalidación de identidad y de hardlinks que
        # puede abortar sin escribir. Invocarlo acá marcaría «intenté escribir»
        # en un camino donde el archivo nunca se tocó, y el rollback atribuiría
        # a la transición un estado intermedio de un tercero (CodeRabbit sobre
        # #851).
        retenido.escribir_bytes(bytes_finales, al_escribir=al_escribir)
    except Mo2IniEscrituraError:
        raise
    except Exception as exc:  # noqa: BLE001 - el gate lanza sus propios tipos
        raise Mo2IniEscrituraError(f"no se pudo escribir modorganizer.ini por el handle: {exc}") from exc

    leidos = retenido.leer_bytes()
    if leidos != bytes_finales:
        raise EscrituraIniNoVerificadaError(
            "verificación post-escritura por handle: el archivo no devuelve los bytes escritos "
            f"({len(leidos)} B leídos, {len(bytes_finales)} B escritos)"
        )
    return leidos

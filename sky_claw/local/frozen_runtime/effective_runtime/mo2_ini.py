"""Lectura read-only de la instancia MO2: ``gamePath`` y ``[customExecutables]``.

El ADR 0012 (§13) registra que ``path_resolver.py`` **no decodifica** el
``gamePath`` de MO2 porque va serializado como ``@ByteArray`` de Qt, y deja
"editar ``gamePath``" como ítem abierto de P5. Este módulo cubre la mitad que
no requiere decidir el binding: **leerlo**. Nada acá escribe.

Formato, verificado contra un ``modorganizer.ini`` real (no inferido)::

    [General]
    gamePath=@ByteArray(G:\\\\Modding\\\\Skyrim_Runtime_1.6.1170)

El interior de ``@ByteArray(...)`` es una cadena con escapes de Qt, no un path
de Windows: ``\\\\`` es una barra literal y ``\\\\n`` es **barra + ene**, no un
salto de línea. Confundir ambos rompe cualquier ruta que contenga ``n``, ``t``
o ``r`` — es decir, casi todas (``\\\\nueva``, ``\\\\runtime``, ``\\\\temp``).

El desescape NO se implementa "según lo que parece razonable": se implementa
según el autómata de Qt, porque MO2 lee con Qt y una diferencia de un byte
produce una ruta que MO2 nunca resolvería. Referencia exacta:
``qtbase 5.15`` ``src/corelib/io/qsettings.cpp`` —
``QSettingsPrivate::iniUnescapedStringList`` (líneas 728-900, ``StHexEscape``
en 854, ``StOctEscape`` en 873) y ``stringToVariant`` (línea 477, que angosta
el resultado con ``toLatin1()``).

``[customExecutables]`` es distinto: sus claves son ``N\\\\binary`` /
``N\\\\title`` / ``N\\\\workingDirectory`` y los valores son **texto plano** con
separadores ``/``. No se decodifican como byte array, y no se les quita las
comillas: ``N\\\\arguments`` las contiene de forma significativa.
"""

from __future__ import annotations

import dataclasses
import pathlib

from sky_claw.local.frozen_runtime.effective_runtime.models import (
    ObservationState,
)

_PREFIJO_BYTE_ARRAY = "@ByteArray("
#: ``gamePath`` NO vive en ``[Settings]``: MO2 lo persiste en ``[General]``, junto
#: a ``gameName``/``selected_profile``/``game_edition``. Verificado contra un
#: ``modorganizer.ini`` real (MO2 2.5.2), donde la línea 4 es
#: ``gamePath=@ByteArray(G:\\\\Modding\\\\Skyrim_Runtime_1.6.1170)`` dentro de
#: ``[General]`` y ``[Settings]`` empieza recién en la línea 89. Un lector que
#: asuma ``[Settings]`` —el error que esta constante documenta— reporta "no
#: configurado" sobre una instancia perfectamente configurada.
_SECCION_GENERAL = "General"
_SECCION_EJECUTABLES = "customExecutables"
_CLAVE_GAME_PATH = "gamePath"

#: Escapes de una letra que Qt interpreta dentro de un byte array. El valor es
#: el byte que representa (``\\a`` = BEL, no la letra ``a``). Es la tabla
#: ``escapeCodes`` de ``qsettings.cpp`` completa: ``"``, ``?``, ``'`` y ``\\``
#: también están, y no son decorativas — todo escape que NO esté acá se
#: DESCARTA (Qt: "the character is skipped"), así que sacar una entrada cambia
#: el resultado de un ``@ByteArray`` con esa secuencia.
_ESCAPES_DE_UNA_LETRA: dict[str, int] = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    '"': 0x22,
    "?": 0x3F,
    "'": 0x27,
    "\\": 0x5C,
}
_DIGITOS_HEX = frozenset("0123456789abcdefABCDEF")
_DIGITOS_OCT = frozenset("01234567")


class QtByteArrayIrrecuperableError(ValueError):
    """Escape que Qt angostaría con pérdida (``toLatin1()`` lo vuelve ``?``).

    No es "malformado": es un valor que Qt SÍ decodifica, pero a un byte que no
    es el que el acumulador representa. Presentarlo como ruta sería inventar
    una ruta que MO2 no usa; el caller lo traduce a ``UNREADABLE``.
    """


def _acumular_escape(
    interior: str,
    indice: int,
    *,
    base: int,
    desplazamiento: int,
    digitos: frozenset[str],
    inicial: int = 0,
) -> tuple[int, int]:
    """Consume dígitos sucesivos como ``char16_t``: 16 bits, con wrap.

    Qt acumula en un ``char16_t`` y no se detiene en dos dígitos hex ni en tres
    octales. El wrap es observable (``\\x10041`` da 0x41), así que se emula en
    vez de suponer que no ocurre.
    """
    valor = inicial
    while indice < len(interior) and interior[indice] in digitos:
        valor = (valor << desplazamiento) & 0xFFFF
        valor = (valor + int(interior[indice], base)) & 0xFFFF
        indice += 1
    return valor, indice


def _desescapar_bytes(interior: str) -> bytes:
    """Desescape de byte array con la semántica EXACTA de Qt 5.15.

    Reglas, todas verificadas contra ``iniUnescapedStringList`` y no inferidas:

    - ``\\\\`` + letra de ``_ESCAPES_DE_UNA_LETRA`` ⇒ ese byte;
    - ``\\\\x`` + dígitos hex ⇒ **todos** los dígitos sucesivos, sin tope
      (``StHexEscape``); ``\\\\x1\\\\xd9`` son dos bytes y ``\\\\x0d9`` es UNO
      (0xD9);
    - ``\\\\`` + dígito octal ⇒ **todos** los octales sucesivos (``StOctEscape``);
      ``\\\\0`` es NUL, que es justo lo que el escritor de Qt emite para un NUL;
    - ``\\\\`` + cualquier otra cosa ⇒ **nada**: Qt descarta el carácter;
    - ``\\\\`` al final ⇒ nada.

    Raises:
        QtByteArrayIrrecuperableError: el acumulador pasa de 0xFF y ``toLatin1()``
            lo sustituiría por ``?``. ``UNKNOWN`` antes que una ruta inventada.
    """
    salida = bytearray()
    indice = 0
    largo = len(interior)
    while indice < largo:
        caracter = interior[indice]
        if caracter != "\\":
            salida.extend(caracter.encode("utf-8", "surrogateescape"))
            indice += 1
            continue
        indice += 1
        if indice >= largo:
            # Qt: ``goto end`` sin añadir nada. Conservar la barra fabricaría
            # ``C:\\`` (con barra) donde Qt resuelve ``C:``.
            break
        caracter = interior[indice]
        indice += 1
        if caracter in _ESCAPES_DE_UNA_LETRA:
            salida.append(_ESCAPES_DE_UNA_LETRA[caracter])
            continue
        if caracter == "x":
            if indice >= largo:
                break
            if interior[indice] not in _DIGITOS_HEX:
                # ``\\\\x`` sin dígitos: se descarta el escape y el carácter
                # siguiente se procesa normal (``\\\\xZ`` ⇒ ``Z``).
                continue
            valor, indice = _acumular_escape(interior, indice, base=16, desplazamiento=4, digitos=_DIGITOS_HEX)
            if valor > 0xFF:
                raise QtByteArrayIrrecuperableError(f"\\x con valor U+{valor:04X} fuera de Latin-1")
            salida.append(valor)
            continue
        if caracter in _DIGITOS_OCT:
            valor, indice = _acumular_escape(
                interior,
                indice,
                base=8,
                desplazamiento=3,
                digitos=_DIGITOS_OCT,
                inicial=int(caracter, 8),
            )
            if valor > 0xFF:
                raise QtByteArrayIrrecuperableError(f"escape octal con valor U+{valor:04X} fuera de Latin-1")
            salida.append(valor)
            continue
        # Incluye ``\\\\n``/``\\\\r`` (continuación de línea) y todo escape
        # desconocido: Qt no añade nada.
    return bytes(salida)


def _interior_del_byte_array(raw: str) -> str | None:
    """Interior de ``@ByteArray(...)``, o ``None`` si no lo es o está truncado."""
    texto = raw.strip()
    if not texto.startswith(_PREFIJO_BYTE_ARRAY):
        return None
    if not texto.endswith(")"):
        return None
    return texto[len(_PREFIJO_BYTE_ARRAY) : -1]


def decode_qt_byte_array(raw: str) -> bytes | None:
    """Bytes de un valor ``@ByteArray(...)``, o ``None`` si no se puede leer.

    ``None`` **no** significa "vacío": significa "no es un byte array", "está
    malformado" o "usa un escape que Qt angostaría con pérdida". El caller
    distingue los casos por ``decode_qt_scalar`` y por el ``ObservationState``
    que construye; devolver ``b""`` para un valor truncado convertiría un ini
    corrupto en "ruta vacía" y el oráculo diría ``ABSENT`` donde debe decir
    ``UNREADABLE``.
    """
    interior = _interior_del_byte_array(raw)
    if interior is None:
        return None
    try:
        return _desescapar_bytes(interior)
    except QtByteArrayIrrecuperableError:
        return None


def decode_qt_scalar(raw: str) -> str:
    """Valor de texto de un escalar Qt: decodifica el byte array o deja el plano.

    Los valores planos se devuelven **sin tocar**: ``base_directory`` y las
    claves de ``[customExecutables]`` son texto con separadores ``/`` y
    desescaparlos corrompería nombres legítimos.
    """
    bytes_decodificados = decode_qt_byte_array(raw)
    if bytes_decodificados is None:
        return raw
    return bytes_decodificados.decode("utf-8", "surrogateescape")


def _leer_seccion(contenido: str, seccion: str) -> dict[str, str]:
    """``clave=valor`` de UNA sección de un INI estilo Qt, con el valor CRUDO.

    Barrido manual, igual que ``path_resolver._leer_seccion_ini`` y por la misma
    razón (MO2 escribe valores binarios que ``configparser`` rechazaría), pero
    con una diferencia deliberada: **no** hace ``strip('"')``. En
    ``[customExecutables]`` las comillas de ``N\\\\arguments`` son parte del
    valor y quitarlas cambiaría lo que MO2 lanzaría.
    """
    valores: dict[str, str] = {}
    seccion_actual: str | None = None
    for linea_bruta in contenido.splitlines():
        linea = linea_bruta.strip().lstrip("\ufeff").strip()
        if not linea or linea.startswith(";") or linea.startswith("#"):
            continue
        if linea.startswith("[") and linea.endswith("]"):
            nombre = linea[1:-1].strip()
            if nombre == seccion:
                seccion_actual = seccion
                continue
            if seccion_actual == seccion:
                break
            continue
        if seccion_actual != seccion or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        valores[clave.strip()] = valor
    return valores


def _leer_texto(ini_path: pathlib.Path) -> tuple[str | None, str]:
    """Texto del ini y el motivo si no se pudo leer. Read-only, sin excepciones."""
    try:
        crudo = pathlib.Path(ini_path).read_bytes()
    except FileNotFoundError:
        return None, "el ini no existe"
    except OSError as exc:
        return None, f"el ini no se pudo leer: {exc}"
    # ``surrogateescape`` preserva byte a byte cualquier INI ANSI del juego.
    return crudo.decode("utf-8", "surrogateescape"), ""


def _seccion_que_declara(contenido: str, clave_buscada: str) -> str | None:
    """Nombre de la primera sección que declara *clave_buscada*, o ``None``.

    Sólo sirve para DIAGNOSTICAR una ubicación inesperada: no se usa para leer
    el valor.
    """
    seccion_actual: str | None = None
    for linea_bruta in contenido.splitlines():
        linea = linea_bruta.strip().lstrip("\ufeff").strip()
        if not linea or linea.startswith(";") or linea.startswith("#"):
            continue
        if linea.startswith("[") and linea.endswith("]"):
            seccion_actual = linea[1:-1].strip()
            continue
        if "=" not in linea:
            continue
        clave, _, _valor = linea.partition("=")
        if clave.strip() == clave_buscada and seccion_actual is not None:
            return seccion_actual
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class Mo2GamePathEvidence:
    """Evidencia de ``[Settings] gamePath``: valor crudo, decodificado y estado."""

    state: ObservationState
    raw: str | None = None
    path: str | None = None
    detail: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Mo2Executable:
    """Una entrada de ``[customExecutables]`` (el ejecutable que MO2 lanza)."""

    index: int
    title: str
    binary: str
    working_directory: str | None = None
    arguments: str | None = None


def leer_game_path_de_mo2(ini_path: pathlib.Path) -> Mo2GamePathEvidence:
    """Lee ``[General] gamePath`` de un ``modorganizer.ini``. Nunca lanza.

    Tres desenlaces distintos, y la distinción es el punto:

    - ``ABSENT`` — el ini no existe o no declara ``gamePath`` (instancia MO2
      todavía sin configurar). No es un error.
    - ``UNREADABLE`` — declara ``gamePath`` pero el valor no se puede
      interpretar (``@ByteArray`` truncado). **No** se degrada a ``ABSENT``.
    - ``OBSERVED`` — hay ruta decodificada.

    Si ``gamePath`` aparece en OTRA sección se devuelve ``ABSENT`` con un
    ``detail`` que la nombra: se prefiere un "no sé" diagnosticable a leer una
    clave homónima de un lugar donde MO2 no la escribe (un MO2 futuro que mueva
    la clave falla cerrado y deja el rastro, en vez de alimentar al oráculo con
    un valor que quizá no signifique lo mismo).
    """
    contenido, motivo = _leer_texto(ini_path)
    if contenido is None:
        return Mo2GamePathEvidence(state=ObservationState.ABSENT, detail=motivo)

    seccion = _leer_seccion(contenido, _SECCION_GENERAL)
    crudo = seccion.get(_CLAVE_GAME_PATH)
    if crudo is None:
        elsewhere = _seccion_que_declara(contenido, _CLAVE_GAME_PATH)
        if elsewhere is not None:
            return Mo2GamePathEvidence(
                state=ObservationState.ABSENT,
                detail=(
                    f"gamePath aparece en [{elsewhere}] pero MO2 lo declara en "
                    f"[{_SECCION_GENERAL}]: formato no reconocido"
                ),
            )
        return Mo2GamePathEvidence(
            state=ObservationState.ABSENT,
            detail=f"la instancia MO2 no declara [{_SECCION_GENERAL}] gamePath",
        )

    if crudo.strip().startswith(_PREFIJO_BYTE_ARRAY):
        interior = _interior_del_byte_array(crudo)
        if interior is None:
            return Mo2GamePathEvidence(
                state=ObservationState.UNREADABLE,
                raw=crudo,
                detail="gamePath es un @ByteArray malformado (truncado o sin cerrar)",
            )
        try:
            ruta = _desescapar_bytes(interior).decode("utf-8", "surrogateescape")
        except QtByteArrayIrrecuperableError as exc:
            # No es un ini corrupto: es un valor que Qt decodifica a '?' vía
            # ``toLatin1()``. Devolverlo como ruta daría OBSERVED sobre una
            # ruta que MO2 jamás resolvería, y el oráculo compararía contra
            # ella. Se declara ilegible y el veredicto queda en UNKNOWN.
            return Mo2GamePathEvidence(
                state=ObservationState.UNREADABLE,
                raw=crudo,
                detail=f"gamePath usa un escape que Qt angostaría a Latin-1 con pérdida: {exc}",
            )
    else:
        ruta = crudo

    if not ruta.strip():
        return Mo2GamePathEvidence(
            state=ObservationState.ABSENT,
            raw=crudo,
            detail="gamePath vacío (MO2 lo trata como no configurado)",
        )
    return Mo2GamePathEvidence(state=ObservationState.OBSERVED, raw=crudo, path=ruta)


def leer_ejecutables_de_mo2(ini_path: pathlib.Path) -> tuple[Mo2Executable, ...]:
    """Entradas de ``[customExecutables]``, ordenadas por índice. Nunca lanza.

    Una entrada sin ``N\\\\binary`` se omite: sin binario no hay ejecutable
    seleccionado que observar, y fabricar una ruta a partir del ``title`` sería
    inventar evidencia. El índice se parsea del prefijo numérico de la clave.
    """
    contenido, _motivo = _leer_texto(ini_path)
    if contenido is None:
        return ()
    seccion = _leer_seccion(contenido, _SECCION_EJECUTABLES)

    por_indice: dict[int, dict[str, str]] = {}
    for clave, valor in seccion.items():
        prefijo, separador, campo = clave.partition("\\")
        if not separador or not prefijo.isdigit():
            continue
        por_indice.setdefault(int(prefijo), {})[campo] = valor

    ejecutables: list[Mo2Executable] = []
    for indice in sorted(por_indice):
        campos = por_indice[indice]
        binario = campos.get("binary", "").strip()
        if not binario:
            continue
        ejecutables.append(
            Mo2Executable(
                index=indice,
                title=campos.get("title", "").strip(),
                binary=binario,
                working_directory=campos.get("workingDirectory", "").strip() or None,
                arguments=campos.get("arguments"),
            )
        )
    return tuple(ejecutables)

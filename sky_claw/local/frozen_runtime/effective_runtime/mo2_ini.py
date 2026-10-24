"""Lectura read-only de la instancia MO2: ``gamePath`` y ``[customExecutables]``.

El ADR 0012 (§13) registra que ``path_resolver.py`` **no decodifica** el
``gamePath`` de MO2 porque va serializado como ``@ByteArray`` de Qt, y deja
"editar ``gamePath``" como ítem abierto de P5. Este módulo cubre la mitad que
no requiere decidir el binding: **leerlo**. Nada acá escribe.

Formato, verificado contra un ``modorganizer.ini`` real (no inferido)::

    [Settings]
    gamePath=@ByteArray(G:\\\\Modding\\\\Skyrim_Runtime_1.6.1170)

El interior de ``@ByteArray(...)`` es una cadena con escapes de Qt, no un path
de Windows: ``\\\\`` es una barra literal y ``\\\\n`` es **barra + ene**, no un
salto de línea. Confundir ambos rompe cualquier ruta que contenga ``n``, ``t``
o ``r`` — es decir, casi todas (``\\\\nueva``, ``\\\\runtime``, ``\\\\temp``).

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
#: el byte que representa (``\\a`` = BEL, no la letra ``a``).
_ESCAPES_DE_UNA_LETRA: dict[str, int] = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
}
_DIGITOS_HEX = frozenset("0123456789abcdefABCDEF")


def _desescapar_bytes(interior: str) -> bytes:
    """Aplica la semántica de desescape de byte array de Qt a *interior*.

    Reglas (las tres importan y las tres tienen test propio):

    - ``\\\\`` seguido de una letra de ``_ESCAPES_DE_UNA_LETRA`` produce ese byte;
    - ``\\\\x`` consume **hasta dos** dígitos hexadecimales y se detiene en el
      primero que no lo sea — por eso ``\\\\x1\\\\xd9`` son dos bytes y no uno;
    - ``\\\\`` seguido de cualquier otra cosa produce ese carácter literal.
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
            # Barra final sin nada que escape: se conserva (no se inventa).
            salida.extend(b"\\")
            break
        caracter = interior[indice]
        if caracter in _ESCAPES_DE_UNA_LETRA:
            salida.append(_ESCAPES_DE_UNA_LETRA[caracter])
            indice += 1
            continue
        if caracter == "x":
            indice += 1
            valor = 0
            digitos = 0
            while indice < largo and digitos < 2 and interior[indice] in _DIGITOS_HEX:
                valor = valor * 16 + int(interior[indice], 16)
                indice += 1
                digitos += 1
            if digitos == 0:
                # ``\\x`` sin dígitos: no es un byte, se preserva literal.
                salida.extend(b"\\x")
            else:
                salida.append(valor)
            continue
        salida.extend(caracter.encode("utf-8", "surrogateescape"))
        indice += 1
    return bytes(salida)


def decode_qt_byte_array(raw: str) -> bytes | None:
    """Bytes de un valor ``@ByteArray(...)``, o ``None`` si no lo es o está truncado.

    ``None`` **no** significa "vacío": significa "no es un byte array" o "está
    malformado". El caller distingue ambos por ``decode_qt_scalar`` y por el
    ``ObservationState`` que construye; devolver ``b""`` para un valor truncado
    convertiría un ini corrupto en "ruta vacía" y el oráculo diría ``ABSENT``
    donde debe decir ``UNREADABLE``.
    """
    texto = raw.strip()
    if not texto.startswith(_PREFIJO_BYTE_ARRAY):
        return None
    if not texto.endswith(")"):
        return None
    interior = texto[len(_PREFIJO_BYTE_ARRAY) : -1]
    return _desescapar_bytes(interior)


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
        bytes_ruta = decode_qt_byte_array(crudo)
        if bytes_ruta is None:
            return Mo2GamePathEvidence(
                state=ObservationState.UNREADABLE,
                raw=crudo,
                detail="gamePath es un @ByteArray malformado (truncado o sin cerrar)",
            )
        ruta = bytes_ruta.decode("utf-8", "surrogateescape")
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

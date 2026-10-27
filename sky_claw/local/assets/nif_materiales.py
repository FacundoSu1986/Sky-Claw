"""Lector de materiales de un NIF de Skyrim: shader, flags y texturas de cada forma (NP-A0j).

El plugin de relieve automático (Auto Parallax) decide por forma —un ``BSGeometry``— a partir de lo que dice el NIF: el tipo del
``BSLightingShaderProperty``, sus flags, si la forma tiene una propiedad de alfa y las rutas del ``BSShaderTextureSet``. Este módulo
extrae exactamente eso y nada más, para que el generador nativo de parallax pueda decir de antemano si una textura que publica
tiene alguna malla donde el plugin la use (``EFECTO_POSIBLE``).

**Formato.** Solo ``Gamebryo File Format, Version 20.2.0.7`` con ``BS Version`` 83 (Skyrim LE) o 100 (Skyrim SE), little-endian. La
distribución de los bloques sale de la especificación pública de NifTools (``nif.xml``); no se copió código de ninguna librería. El
lector NO interpreta geometría: se apoya en la tabla de tamaños de bloque de la cabecera y solo abre las formas, sus propiedades
de shader y alfa y el juego de texturas.

**Falla cerrado.** Una cabecera inesperada, un archivo truncado, un tamaño que se pasa, una referencia o un índice de texto fuera de
rango, o una cantidad absurda levantan ``NifError``; el llamador lo traduce a «no verificado», nunca a «sin efecto». Las formas de
un tipo que el lector no sabe abrir no se ignoran en silencio: quedan en ``formas_no_soportadas``.

**Lo que NO hace.** No lee mallas dentro de un ``.bsa``, no sabe de qué tipo de objeto del juego cuelga la malla (el plugin solo
actúa sobre algunos) y no prueba que el juego muestre algo: es una lectura estructural del archivo.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

__all__ = [
    "TOPE_DE_BLOQUES",
    "FormaDeNif",
    "LecturaDeNif",
    "NifError",
    "leer_materiales",
]

#: Un NIF de Skyrim real tiene decenas o cientos de bloques; esto es un techo de cordura, no un tamaño esperado.
TOPE_DE_BLOQUES: Final[int] = 100_000
_TOPE_DE_RANURAS_DE_TEXTURAS: Final = 16
_LONGITUD_MAXIMA_DE_TEXTO: Final = 4096

_CABECERA: Final = b"Gamebryo File Format, Version 20.2.0.7\n"
_VERSION: Final = 0x14020007
_VERSIONES_DE_BETHESDA: Final = frozenset({83, 100})  # Skyrim LE y Skyrim SE

#: Formas de Skyrim SE: NiAVObject, esfera envolvente, ``Skin``, ``Shader Property`` y ``Alpha Property``.
_FORMAS_BS: Final = frozenset({"BSTriShape", "BSSubIndexTriShape", "BSDynamicTriShape"})
#: Formas heredadas de ``NiTriBasedGeom``/``NiTriShape``: NiAVObject, ``Data``, ``Skin Instance``, ``Material Data``, shader y alfa.
_FORMAS_NI: Final = frozenset({"NiTriShape", "NiTriStrips", "BSLODTriShape", "BSSegmentedTriShape"})

_SHADER_DE_ILUMINACION: Final = "BSLightingShaderProperty"
_JUEGO_DE_TEXTURAS: Final = "BSShaderTextureSet"

#: Tipos de geometría que el lector NO abre y que, si aparecen, hay que declarar (nunca ignorar en silencio).
_PREFIJOS_DE_GEOMETRIA_NO_SOPORTADA: Final = ("NiParticle", "BSStripParticle")
_SUFIJOS_DE_GEOMETRIA_NO_SOPORTADA: Final = ("TriShape", "TriStrips")
_GEOMETRIA_NO_SOPORTADA_EXACTA: Final = frozenset({"NiLines", "NiParticles", "NiPoints"})


class NifError(Exception):
    """El archivo no es un NIF de Skyrim legible por este lector: ni se afirma ni se descarta nada sobre sus formas."""


@dataclass(frozen=True, slots=True)
class FormaDeNif:
    """Lo que el plugin de relieve mira de una forma."""

    tipo_de_bloque: str
    nombre: str
    propiedad_de_shader: (
        str | None
    )  #: Tipo del bloque al que apunta ``Shader Property``; ``None`` si no apunta a ninguno.
    tipo_de_shader: int | None  #: Solo en un ``BSLightingShaderProperty`` (0 = Default, 3 = Parallax…).
    flags_1: int | None  #: ``SkyrimShaderPropertyFlags1`` crudo; ``None`` si no hay propiedad de iluminación.
    flags_2: int | None  #: ``SkyrimShaderPropertyFlags2`` crudo.
    tiene_propiedad_de_alfa: bool  #: ``Alpha Property`` apunta a un bloque (el plugin solo mira que exista).
    texturas: tuple[str, ...] | None  #: Ranuras del ``BSShaderTextureSet`` en orden, con ``""`` en las vacías.


@dataclass(frozen=True, slots=True)
class LecturaDeNif:
    version_bs: int
    formas: tuple[FormaDeNif, ...]
    formas_no_soportadas: tuple[
        str, ...
    ]  #: Tipos de geometría presentes que este lector no sabe abrir (ordenados, sin repetir).


class _Lector:
    """Cursor acotado a ``[posicion, fin)``: ninguna lectura puede salirse de su zona."""

    __slots__ = ("_datos", "_fin", "_posicion", "contexto")

    def __init__(self, datos: bytes, posicion: int, fin: int, contexto: str) -> None:
        self._datos = datos
        self._posicion = posicion
        self._fin = fin
        self.contexto = contexto

    @property
    def restante(self) -> int:
        return self._fin - self._posicion

    def _tomar(self, formato: struct.Struct) -> int:
        if formato.size > self.restante:
            raise NifError(f"{self.contexto}: datos insuficientes")
        (valor,) = formato.unpack_from(self._datos, self._posicion)
        self._posicion += formato.size
        return int(valor)

    def u8(self) -> int:
        return self._tomar(_U8)

    def u16(self) -> int:
        return self._tomar(_U16)

    def u32(self) -> int:
        return self._tomar(_U32)

    def i32(self) -> int:
        return self._tomar(_I32)

    def saltar(self, cantidad: int) -> None:
        if cantidad > self.restante:
            raise NifError(f"{self.contexto}: datos insuficientes")
        self._posicion += cantidad

    def texto(self, longitud: int) -> str:
        if longitud > min(self.restante, _LONGITUD_MAXIMA_DE_TEXTO):
            raise NifError(f"{self.contexto}: texto de longitud inválida ({longitud})")
        bruto = self._datos[self._posicion : self._posicion + longitud]
        self._posicion += longitud
        # latin-1 no puede fallar: las rutas de Skyrim son ASCII y un byte raro no debe tumbar la lectura.
        return bruto.decode("latin-1")


_U8: Final = struct.Struct("<B")
_U16: Final = struct.Struct("<H")
_U32: Final = struct.Struct("<I")
_I32: Final = struct.Struct("<i")


def _leer_cabecera(datos: bytes) -> tuple[int, list[str], list[int], list[str], int]:
    """``(BS Version, tipo de cada bloque, tamaño de cada bloque, tabla de textos, inicio de los bloques)``."""
    if not datos.startswith(_CABECERA):
        raise NifError("cabecera: no es un NIF 'Gamebryo File Format, Version 20.2.0.7'")
    lector = _Lector(datos, len(_CABECERA), len(datos), "cabecera")
    if lector.u32() != _VERSION:
        raise NifError("versión: el número de versión no es 20.2.0.7")
    if lector.u8() != 1:
        raise NifError("endian: solo se leen NIFs little-endian")
    lector.u32()  # versión de usuario
    cantidad_de_bloques = lector.u32()
    if cantidad_de_bloques > TOPE_DE_BLOQUES:
        raise NifError(f"bloques: {cantidad_de_bloques} supera el tope de {TOPE_DE_BLOQUES}")
    version_bs = lector.u32()
    if version_bs not in _VERSIONES_DE_BETHESDA:
        raise NifError(f"Bethesda: BS Version {version_bs} no es Skyrim (se leen {sorted(_VERSIONES_DE_BETHESDA)})")
    # Cadenas de exportación: autor, script de proceso (BS < 131) y script de exportación. La ruta máxima solo existe desde BS 103 y
    # las versiones aceptadas (83 y 100) son anteriores.
    for _ in range(3):
        lector.saltar(lector.u8())
    cantidad_de_tipos = lector.u16()
    nombres_de_tipos = [lector.texto(lector.u32()) for _ in range(cantidad_de_tipos)]
    tipos_de_bloques: list[str] = []
    for _ in range(cantidad_de_bloques):
        indice = lector.u16() & 0x7FFF  # el bit alto marca bloques de PhysX
        if indice >= len(nombres_de_tipos):
            raise NifError(f"tipo: el bloque usa el tipo {indice}, fuera de la tabla de {len(nombres_de_tipos)}")
        tipos_de_bloques.append(nombres_de_tipos[indice])
    tamanos = [lector.u32() for _ in range(cantidad_de_bloques)]
    cantidad_de_textos = lector.u32()
    lector.u32()  # longitud máxima de texto
    textos = [lector.texto(lector.u32()) for _ in range(cantidad_de_textos)]
    for _ in range(lector.u32()):  # grupos
        lector.u32()
    inicio_de_bloques = len(datos) - lector.restante
    if sum(tamanos) > len(datos) - inicio_de_bloques:
        raise NifError("bloques: los tamaños declarados se pasan del archivo")
    return version_bs, tipos_de_bloques, tamanos, textos, inicio_de_bloques


def _referencia(valor: int, cantidad_de_bloques: int) -> int | None:
    if valor == -1:
        return None
    if not 0 <= valor < cantidad_de_bloques:
        raise NifError(f"referencia: el bloque {valor} no existe (hay {cantidad_de_bloques})")
    return valor


def _texto_de_la_tabla(indice: int, textos: list[str]) -> str:
    if indice == -1:
        return ""
    if not 0 <= indice < len(textos):
        raise NifError(f"texto: el índice {indice} está fuera de la tabla de {len(textos)}")
    return textos[indice]


def _saltar_niavobject(lector: _Lector) -> int:
    """Salta ``NiObjectNET`` + ``NiAVObject`` de Skyrim y devuelve el índice del nombre."""
    nombre = lector.i32()
    lector.saltar(lector.u32() * 4)  # lista de extra data
    lector.saltar(4)  # controlador
    lector.saltar(4 + 12 + 36 + 4 + 4)  # flags, traslación, rotación, escala, objeto de colisión
    return nombre


def _leer_forma_bs(lector: _Lector) -> tuple[int, int, int]:
    nombre = _saltar_niavobject(lector)
    lector.saltar(16 + 4)  # esfera envolvente y ``Skin``
    shader = lector.i32()
    alfa = lector.i32()
    return nombre, shader, alfa


def _leer_forma_ni(lector: _Lector) -> tuple[int, int, int]:
    nombre = _saltar_niavobject(lector)
    lector.saltar(4 + 4)  # ``Data`` y ``Skin Instance``
    materiales = lector.u32()
    lector.saltar(materiales * 8)  # nombre y extra data de cada material
    lector.saltar(4 + 1)  # material activo y «necesita actualizar»
    shader = lector.i32()
    alfa = lector.i32()
    return nombre, shader, alfa


def _leer_shader_de_iluminacion(lector: _Lector) -> tuple[int, int, int, int]:
    """``(tipo de shader, flags 1, flags 2, referencia al juego de texturas)``; el tipo viaja ANTES del nombre."""
    tipo = lector.u32()
    lector.saltar(4)  # nombre
    lector.saltar(lector.u32() * 4)  # lista de extra data
    lector.saltar(4)  # controlador
    flags_1 = lector.u32()
    flags_2 = lector.u32()
    lector.saltar(8 + 8)  # desplazamiento y escala de UV
    return tipo, flags_1, flags_2, lector.i32()


def _leer_juego_de_texturas(lector: _Lector) -> tuple[str, ...]:
    cantidad = lector.u32()
    if cantidad > _TOPE_DE_RANURAS_DE_TEXTURAS:
        raise NifError(
            f"{lector.contexto}: {cantidad} ranuras de textura supera el tope de {_TOPE_DE_RANURAS_DE_TEXTURAS}"
        )
    return tuple(lector.texto(lector.u32()) for _ in range(cantidad))


def _es_geometria_no_soportada(tipo: str) -> bool:
    if tipo in _FORMAS_BS or tipo in _FORMAS_NI:
        return False
    return (
        tipo in _GEOMETRIA_NO_SOPORTADA_EXACTA
        or tipo.endswith(_SUFIJOS_DE_GEOMETRIA_NO_SOPORTADA)
        or tipo.startswith(_PREFIJOS_DE_GEOMETRIA_NO_SOPORTADA)
    )


def leer_materiales(datos: bytes) -> LecturaDeNif:
    """Lee las formas de un NIF de Skyrim con su shader, flags, propiedad de alfa y texturas.

    Raises:
        NifError: el archivo no es un NIF 20.2.0.7 de Skyrim, está truncado o inconsistente.
    """
    if not isinstance(datos, (bytes, bytearray, memoryview)):
        raise NifError("los datos del NIF deben ser bytes")
    datos = bytes(datos)
    version_bs, tipos, tamanos, textos, inicio = _leer_cabecera(datos)

    zonas: list[tuple[int, int]] = []
    posicion = inicio
    for tamano in tamanos:
        zonas.append((posicion, posicion + tamano))
        posicion += tamano

    def lector_de(indice: int) -> _Lector:
        desde, hasta = zonas[indice]
        return _Lector(datos, desde, hasta, tipos[indice])

    def shader_de(referencia: int | None) -> tuple[str | None, tuple[int, int, int, int] | None]:
        if referencia is None:
            return None, None
        tipo = tipos[referencia]
        if tipo != _SHADER_DE_ILUMINACION:
            return tipo, None
        return tipo, _leer_shader_de_iluminacion(lector_de(referencia))

    formas: list[FormaDeNif] = []
    for indice, tipo in enumerate(tipos):
        if tipo in _FORMAS_BS:
            nombre, shader, alfa = _leer_forma_bs(lector_de(indice))
        elif tipo in _FORMAS_NI:
            nombre, shader, alfa = _leer_forma_ni(lector_de(indice))
        else:
            continue
        referencia_de_shader = _referencia(shader, len(tipos))
        referencia_de_alfa = _referencia(alfa, len(tipos))
        propiedad, iluminacion = shader_de(referencia_de_shader)
        texturas: tuple[str, ...] | None = None
        tipo_de_shader = flags_1 = flags_2 = None
        if iluminacion is not None:
            tipo_de_shader, flags_1, flags_2, juego = iluminacion
            referencia_del_juego = _referencia(juego, len(tipos))
            if referencia_del_juego is not None:
                if tipos[referencia_del_juego] != _JUEGO_DE_TEXTURAS:
                    raise NifError(f"referencia: el juego de texturas apunta a un {tipos[referencia_del_juego]}")
                texturas = _leer_juego_de_texturas(lector_de(referencia_del_juego))
        formas.append(
            FormaDeNif(
                tipo_de_bloque=tipo,
                nombre=_texto_de_la_tabla(nombre, textos),
                propiedad_de_shader=propiedad,
                tipo_de_shader=tipo_de_shader,
                flags_1=flags_1,
                flags_2=flags_2,
                tiene_propiedad_de_alfa=referencia_de_alfa is not None,
                texturas=texturas,
            )
        )
    no_soportadas = tuple(sorted({tipo for tipo in tipos if _es_geometria_no_soportada(tipo)}))
    return LecturaDeNif(version_bs=version_bs, formas=tuple(formas), formas_no_soportadas=no_soportadas)

"""Constructor de NIFs sintéticos para los tests del lector de materiales (NP-A0j).

Escrito desde la especificación pública ``nif.xml`` de NifTools (versión 20.2.0.7, ``BS Version`` 83 = Skyrim LE y 100 = Skyrim
SE), de forma INDEPENDIENTE del lector: no importa nada de ``sky_claw``. Un NIF sintético prueba que el lector concuerda con la
especificación tal como la leyó quien escribió este helper, NO que lea bien un archivo real del juego; para eso están la
verificación cruzada con una implementación ajena (en el PR) y la tabla que devuelve el rig.

Solo se construyen las partes que el lector interpreta (cabecera, formas, propiedad de shader, juego de texturas, propiedad de
alfa); el resto de cada bloque (vértices, triángulos…) es relleno de ceros, porque el lector se apoya en la tabla de tamaños de
bloque de la cabecera para no tocarlo.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

__all__ = [
    "Bloque",
    "bloque_alfa",
    "bloque_forma_bs",
    "bloque_forma_ni",
    "bloque_juego_de_texturas",
    "bloque_nodo",
    "bloque_shader_de_efecto",
    "bloque_shader_de_iluminacion",
    "construir_nif",
]

_CABECERA: Final = b"Gamebryo File Format, Version 20.2.0.7\n"
_VERSION: Final = 0x14020007
_USUARIO: Final = 12


@dataclass(frozen=True, slots=True)
class Bloque:
    tipo: str
    cuerpo: bytes


def _niobjectnet(nombre: int, extra: tuple[int, ...] = (), controlador: int = -1) -> bytes:
    return (
        struct.pack("<iI", nombre, len(extra))
        + b"".join(struct.pack("<i", ref) for ref in extra)
        + struct.pack("<i", controlador)
    )


def _niavobject(nombre: int, extra: tuple[int, ...] = ()) -> bytes:
    return (
        _niobjectnet(nombre, extra)
        + struct.pack("<I", 0x8000E)  # Flags (uint porque BS Version > 26)
        + struct.pack("<3f", 0.0, 0.0, 0.0)  # Translation
        + struct.pack("<9f", 1, 0, 0, 0, 1, 0, 0, 0, 1)  # Rotation
        + struct.pack("<f", 1.0)  # Scale
        + struct.pack("<i", -1)  # Collision Object
    )


def bloque_nodo(nombre: int = -1, hijos: tuple[int, ...] = ()) -> Bloque:
    cuerpo = _niavobject(nombre) + struct.pack("<I", len(hijos)) + b"".join(struct.pack("<i", h) for h in hijos)
    return Bloque("NiNode", cuerpo + struct.pack("<i", -1))


def bloque_forma_bs(
    *,
    nombre: int = -1,
    shader: int = -1,
    alfa: int = -1,
    piel: int = -1,
    tipo: str = "BSTriShape",
    relleno: int = 24,
    extra: tuple[int, ...] = (),
) -> Bloque:
    """``BSTriShape`` (Skyrim SE): NiAVObject, esfera envolvente, ``Skin``, ``Shader Property``, ``Alpha Property`` y datos."""
    cuerpo = (
        _niavobject(nombre, extra)
        + struct.pack("<4f", 0.0, 0.0, 0.0, 1.0)  # Bounding Sphere
        + struct.pack("<iii", piel, shader, alfa)
        + b"\x00" * relleno  # Vertex Desc, contadores y vértices: el lector no los toca
    )
    return Bloque(tipo, cuerpo)


def bloque_forma_ni(
    *,
    nombre: int = -1,
    shader: int = -1,
    alfa: int = -1,
    datos: int = -1,
    piel: int = -1,
    materiales: int = 0,
    tipo: str = "NiTriShape",
    extra: tuple[int, ...] = (),
) -> Bloque:
    """``NiTriShape`` (Skyrim LE y SE heredado): NiAVObject, ``Data``, ``Skin Instance``, ``Material Data``, shader y alfa."""
    cuerpo = (
        _niavobject(nombre, extra)
        + struct.pack("<ii", datos, piel)
        + struct.pack("<I", materiales)
        + b"".join(struct.pack("<i", -1) for _ in range(materiales))  # Material Name (índice a la tabla de textos)
        + b"".join(struct.pack("<i", -1) for _ in range(materiales))  # Material Extra Data
        + struct.pack("<i", -1)  # Active Material
        + struct.pack("<B", 0)  # Material Needs Update
        + struct.pack("<ii", shader, alfa)
    )
    return Bloque(tipo, cuerpo)


def bloque_shader_de_iluminacion(
    *,
    tipo_de_shader: int = 0,
    flags_1: int = 0,
    flags_2: int = 0,
    juego_de_texturas: int = -1,
    nombre: int = -1,
    extra: tuple[int, ...] = (),
) -> Bloque:
    """``BSLightingShaderProperty`` (Skyrim): el tipo de shader viaja ANTES del nombre, y le siguen flags, UV y juego de texturas."""
    cuerpo = (
        struct.pack("<I", tipo_de_shader)
        + _niobjectnet(nombre, extra)
        + struct.pack("<II", flags_1, flags_2)
        + struct.pack("<2f", 0.0, 0.0)  # UV Offset
        + struct.pack("<2f", 1.0, 1.0)  # UV Scale
        + struct.pack("<i", juego_de_texturas)
        + b"\x00" * 40  # emisivo, brillo, especular…: el lector no los toca
    )
    return Bloque("BSLightingShaderProperty", cuerpo)


def bloque_shader_de_efecto(*, nombre: int = -1) -> Bloque:
    """``BSEffectShaderProperty``: otro tipo de propiedad de shader; el plugin de relieve no lo toca."""
    return Bloque("BSEffectShaderProperty", _niobjectnet(nombre) + struct.pack("<II", 0, 0) + b"\x00" * 40)


def bloque_juego_de_texturas(rutas: tuple[str, ...]) -> Bloque:
    cuerpo = struct.pack("<I", len(rutas))
    for ruta in rutas:
        bruto = ruta.encode("latin-1")
        cuerpo += struct.pack("<I", len(bruto)) + bruto
    return Bloque("BSShaderTextureSet", cuerpo)


def bloque_alfa(flags: int = 0x00ED, umbral: int = 128) -> Bloque:
    return Bloque("NiAlphaProperty", _niobjectnet(-1) + struct.pack("<HB", flags, umbral))


def _cadena_de_exportacion(texto: str) -> bytes:
    bruto = texto.encode("latin-1") + b"\x00"
    return struct.pack("<B", len(bruto)) + bruto


def construir_nif(
    bloques: list[Bloque],
    *,
    textos: tuple[str, ...] = (),
    version_bs: int = 100,
    raices: tuple[int, ...] = (0,),
    version: int = _VERSION,
    cabecera: bytes = _CABECERA,
    little_endian: int = 1,
    grupos: tuple[int, ...] = (),
    con_pie: bool = True,
) -> bytes:
    """Arma un NIF completo: cabecera con la tabla de tipos y de tamaños, tabla de textos, bloques y pie."""
    tipos: list[str] = []
    for bloque in bloques:
        if bloque.tipo not in tipos:
            tipos.append(bloque.tipo)
    salida = cabecera + struct.pack("<IBII", version, little_endian, _USUARIO, len(bloques))
    salida += struct.pack("<I", version_bs)
    salida += _cadena_de_exportacion("autor")
    if version_bs < 131:
        salida += _cadena_de_exportacion("")  # Process Script
    salida += _cadena_de_exportacion("exportador")
    if version_bs >= 103:
        salida += _cadena_de_exportacion("")  # Max Filepath
    salida += struct.pack("<H", len(tipos))
    for tipo in tipos:
        bruto = tipo.encode("ascii")
        salida += struct.pack("<I", len(bruto)) + bruto
    salida += b"".join(struct.pack("<H", tipos.index(b.tipo)) for b in bloques)
    salida += b"".join(struct.pack("<I", len(b.cuerpo)) for b in bloques)
    brutos = [t.encode("latin-1") for t in textos]
    salida += struct.pack("<II", len(brutos), max((len(t) for t in brutos), default=0))
    salida += b"".join(struct.pack("<I", len(t)) + t for t in brutos)
    salida += struct.pack("<I", len(grupos)) + b"".join(struct.pack("<I", g) for g in grupos)
    salida += b"".join(b.cuerpo for b in bloques)
    if con_pie:
        salida += struct.pack("<I", len(raices)) + b"".join(struct.pack("<i", r) for r in raices)
    return salida

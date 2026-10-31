"""Paridad del predicado «textura 2-D simple» entre el exportador del generador y el auditor de la salida (#834 ítem 5).

``native_parallax.export.inspeccionar_dds_bc4`` (lo que el generador exige antes de publicar) y ``parallax_unattended.auditor`` (lo que
verifica al releer la salida) deciden cada uno por su cuenta si una cabecera DDS es una textura 2-D sin cubemap, volumen ni arreglo.
El auditor NO puede importar el exportador (es una verificación independiente del producto), así que son dos copias del mismo
predicado: si una cambia y la otra no, el generador publicaría lo que el auditor rechaza (o al revés, el auditor aprobaría lo que el
generador no se atrevería a escribir). Este archivo las compara sobre TODA la matriz de cabeceras, no sobre un caso escrito a mano.

El helper arma la cabecera con la disposición pública de Microsoft y no reusa código de ninguno de los dos módulos.
"""

from __future__ import annotations

import itertools
import struct

import pytest

from sky_claw.local.native_parallax.export import ContenedorDdsInvalidoError, inspeccionar_dds_bc4
from sky_claw.local.parallax_unattended import auditor

_DDSD_BASICO = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000  # CAPS | HEIGHT | WIDTH | PIXELFORMAT | LINEARSIZE
_DDSD_DEPTH = 0x800000
_DDSCAPS2_CUBEMAP = 0x200
_DDSCAPS2_VOLUMEN = 0x200000
_DX10_MISC_CUBEMAP = 0x4
_DIMENSION_1D, _DIMENSION_2D, _DIMENSION_3D = 2, 3, 4
_DXGI_BC4_UNORM = 80
_LADO = 16
_PAYLOAD = b"\x00" * ((_LADO // 4) ** 2 * 8)  # BC4: 8 bytes por bloque de 4x4, un solo nivel


def _dds(*, caps2: int, banderas_extra: int, dx10: tuple[int, int, int] | None) -> bytes:
    """BC4 de 16x16 de un nivel; ``dx10`` = ``(dimension, banderas_misc, tamano_del_arreglo)`` o ``None`` para el FourCC ``BC4U``."""
    fourcc = b"DX10" if dx10 is not None else b"BC4U"
    formato_de_pixel = struct.pack("<II4sIIIII", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    cabecera = struct.pack("<7I", 124, _DDSD_BASICO | banderas_extra, _LADO, _LADO, len(_PAYLOAD), 0, 1)
    cabecera += b"\0" * 44 + formato_de_pixel + struct.pack("<5I", 0x1000, caps2, 0, 0, 0)
    extension = struct.pack("<5I", _DXGI_BC4_UNORM, *dx10, 0) if dx10 is not None else b""
    return b"DDS " + cabecera + extension + _PAYLOAD


_CAPS2 = (0, _DDSCAPS2_CUBEMAP, _DDSCAPS2_VOLUMEN)
_BANDERAS = (0, _DDSD_DEPTH)
_DX10 = (
    None,
    *itertools.product((_DIMENSION_1D, _DIMENSION_2D, _DIMENSION_3D), (0, _DX10_MISC_CUBEMAP), (1, 6)),
)
_MATRIZ = list(itertools.product(_CAPS2, _BANDERAS, _DX10))

#: La única decisión que importa, congelada: solo estas cabeceras son «2-D simple». Sin esto, las dos copias podrían estar mal juntas.
_SIMPLES = {(0, 0, None), (0, 0, (_DIMENSION_2D, 0, 1))}


def _el_exportador_la_acepta(datos: bytes) -> bool:
    try:
        inspeccionar_dds_bc4(datos)
    except ContenedorDdsInvalidoError:
        return False
    return True


def _el_auditor_la_ve_simple(datos: bytes) -> bool:
    cabecera = auditor._leer_cabecera(datos)
    return cabecera.es_2d_simple and cabecera.profundidad == 1 and auditor._es_bc4_unorm(cabecera)


def test_la_matriz_cubre_las_combinaciones_esperadas() -> None:
    assert len(_MATRIZ) == 3 * 2 * (1 + 3 * 2 * 2)
    assert set(_MATRIZ) >= _SIMPLES


@pytest.mark.parametrize(("caps2", "banderas", "dx10"), _MATRIZ, ids=[repr(c) for c in _MATRIZ])
def test_el_exportador_y_el_auditor_deciden_igual_si_una_cabecera_es_2d_simple(
    caps2: int, banderas: int, dx10: tuple[int, int, int] | None
) -> None:
    datos = _dds(caps2=caps2, banderas_extra=banderas, dx10=dx10)

    esperado = (caps2, banderas, dx10) in _SIMPLES

    assert _el_exportador_la_acepta(datos) is esperado
    assert _el_auditor_la_ve_simple(datos) is esperado

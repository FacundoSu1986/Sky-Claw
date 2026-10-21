"""El decodificador/codificador BC4 de ``tests/_bc4.py`` se valida contra Pillow, un decodificador independiente.

Sin esto, el smoke real de ``texconv`` compararía contra un decodificador que nadie verificó.
"""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest
from PIL import Image

from sky_claw.local.native_parallax.export import cuantizar_a_8_bits, generar_mips
from tests._bc4 import (
    cadena_box_con_redondeo_a_8_bits,
    cadena_con_curva_srgb,
    cadena_con_filtro_point,
    campo_sintetico,
    codificar_nivel_ingenuo,
    decodificar_nivel,
    dentro_de_tolerancia,
    paleta,
    partir_niveles,
)


def _dds_bc4(ancho: int, alto: int, carga: bytes, niveles: int = 1) -> bytes:
    banderas = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000 | (0x20000 if niveles > 1 else 0)
    formato_pixel = struct.pack("<II4sIIIII", 32, 0x4, b"BC4U", 0, 0, 0, 0, 0)
    cabecera = struct.pack("<7I", 124, banderas, alto, ancho, 0, 0, niveles if niveles > 1 else 0)
    cabecera += b"\0" * 44 + formato_pixel + struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return b"DDS " + cabecera + carga


def _campo(forma: tuple[int, int], semilla: int) -> np.ndarray:
    rng = np.random.default_rng(semilla)
    base = rng.integers(0, 256, size=forma, dtype=np.uint8)
    return np.clip(base.astype(np.int64) // 2 + np.linspace(0, 120, forma[1]).astype(np.int64), 0, 255).astype(np.uint8)


def test_la_paleta_tiene_ocho_valores_en_los_dos_modos() -> None:
    p8 = paleta(200, 40)
    p6 = paleta(40, 200)

    assert len(p8) == len(p6) == 8
    assert p8[:2] == [200.0, 40.0] and p8[2] == pytest.approx((6 * 200 + 1 * 40) / 7)
    assert p6[:2] == [40.0, 200.0] and p6[2] == pytest.approx((4 * 40 + 1 * 200) / 5) and p6[6:] == [0.0, 255.0]


def test_un_bloque_constante_se_decodifica_constante() -> None:
    nivel = np.full((4, 4), 77, dtype=np.uint8)

    assert np.array_equal(decodificar_nivel(codificar_nivel_ingenuo(nivel), 4, 4), nivel)


@pytest.mark.parametrize("forma", [(4, 4), (16, 8), (32, 32), (2, 2), (1, 1), (2, 8)])
def test_el_decodificador_coincide_con_pillow_en_el_nivel_base(forma: tuple[int, int]) -> None:
    nivel = _campo(forma, semilla=forma[0] * 31 + forma[1])
    carga = codificar_nivel_ingenuo(nivel)

    propio = decodificar_nivel(carga, forma[1], forma[0])

    with Image.open(io.BytesIO(_dds_bc4(forma[1], forma[0], carga))) as imagen:
        imagen.load()
        de_pillow = np.asarray(imagen).astype(np.float64)
    # Pillow decodifica con aritmética entera; la diferencia posible es de redondeo, no de interpretación.
    assert propio.shape == de_pillow.shape == forma
    assert np.max(np.abs(propio - de_pillow)) <= 1.0


def test_el_error_del_codificador_ingenuo_es_acotado_en_un_campo_suave() -> None:
    # Rampa horizontal: dentro de un bloque de 4 px el rango es ~25, y con 8 valores el error cabe en rango / 14.
    nivel = np.tile(np.linspace(0, 255, 32).astype(np.uint8), (32, 1))

    decodificado = decodificar_nivel(codificar_nivel_ingenuo(nivel), 32, 32)

    assert np.max(np.abs(decodificado - nivel.astype(np.float64))) <= 3.0


def test_partir_niveles_respeta_el_piso_de_un_bloque_y_la_suma_exacta() -> None:
    carga = bytes(range(256)) * 4
    # 16x16: 16 + 4 + 1 + 1 + 1 bloques de 8 bytes = 23 bloques = 184 bytes
    partes = partir_niveles(carga[:184], 16, 16, 5)

    assert [len(parte) for parte in partes] == [128, 32, 8, 8, 8]
    with pytest.raises(ValueError):
        partir_niveles(carga[:185], 16, 16, 5)


def test_decodificar_rechaza_una_carga_de_tamano_equivocado() -> None:
    with pytest.raises(ValueError):
        decodificar_nivel(b"\0" * 7, 4, 4)


def _niveles_dentro_de_tolerancia(cadena: list[np.ndarray], referencia: list[np.ndarray]) -> list[bool]:
    return [dentro_de_tolerancia(obtenido, esperado) for obtenido, esperado in zip(cadena, referencia, strict=True)]


def test_el_oraculo_de_mips_distingue_los_filtros_equivocados_de_los_legitimos() -> None:
    # Es lo que sostiene el smoke real del texconv: si alguien afloja la tolerancia hasta que un filtro POINT o una
    # curva sRGB pasen, este test falla. Los extremos 1x1..4x4 son los que delatan la curva sRGB en este campo.
    referencia = [cuantizar_a_8_bits(nivel) for nivel in generar_mips(campo_sintetico(256, 256, semilla=3))]
    base = referencia[0]

    assert all(_niveles_dentro_de_tolerancia(referencia, referencia))  # la cadena propia, trivialmente
    assert all(_niveles_dentro_de_tolerancia(cadena_box_con_redondeo_a_8_bits(base), referencia))  # legítima
    assert not all(_niveles_dentro_de_tolerancia(cadena_con_filtro_point(base), referencia))
    assert not all(_niveles_dentro_de_tolerancia(cadena_con_curva_srgb(base), referencia))


def test_el_oraculo_detecta_el_filtro_point_desde_los_niveles_intermedios() -> None:
    referencia = [cuantizar_a_8_bits(nivel) for nivel in generar_mips(campo_sintetico(256, 256, semilla=3))]

    veredicto = _niveles_dentro_de_tolerancia(cadena_con_filtro_point(referencia[0]), referencia)

    assert veredicto[:2] == [True, True]  # los dos primeros niveles se parecen aunque se submuestree
    assert not any(veredicto[2:])  # del nivel 2 en adelante POINT se aleja de la cadena de promedio de área


def test_el_oraculo_detecta_la_curva_srgb_en_los_niveles_chicos() -> None:
    referencia = [cuantizar_a_8_bits(nivel) for nivel in generar_mips(campo_sintetico(256, 256, semilla=3))]

    veredicto = _niveles_dentro_de_tolerancia(cadena_con_curva_srgb(referencia[0]), referencia)

    assert all(veredicto[:6])  # a media escala y bajo contraste la curva casi no se nota...
    assert not any(veredicto[6:])  # ...pero en 4x4, 2x2 y 1x1 sí


def test_el_oraculo_detecta_un_bloque_corrupto_aunque_el_error_medio_sea_bajo() -> None:
    # El límite del error MÁXIMO existe para esto: un bloque entero mal comprimido apenas mueve el promedio.
    referencia = [cuantizar_a_8_bits(nivel) for nivel in generar_mips(campo_sintetico(256, 256, semilla=3))]
    corrupto = referencia[3].astype(np.int64)
    corrupto[0:4, 0:4] += 60

    assert dentro_de_tolerancia(referencia[3], referencia[3])
    assert not dentro_de_tolerancia(np.clip(corrupto, 0, 255).astype(np.uint8), referencia[3])

"""NP-C0: ``NormalDecoder`` del generador nativo de parallax (ADR 0014, §2.5).

Qué se prueba y con qué evidencia:

- PNG/uncompressed/BC1: el camino RGB. El vector se reescala a [-1, 1]³ y se renormaliza, igual que el
  oráculo ``research.authored_dataset.decode_normal_image`` (ADR 0014, decisión 2: el producto NO
  importa research; este test sí, como oráculo de equivalencia).
- BC5: Pillow 11.3.0 lo decodifica como RGB con B = 0 (verificado ejecutando); el decodificador
  reconstruye Z = sqrt(1 − x² − y²) y cuenta los píxeles fuera del disco unidad (Z = 0).
- BC7: bloques de modo 6 con índices en cero, armados a mano (colores sólidos).
- Convención: la salida es DirectX; una fuente OpenGL se pasa a DirectX invirtiendo Y.
- Errores tipados y dependencias perezosas.

Límite declarado: las fixtures son SINTÉTICAS (bloques armados a mano o guardados por Pillow). Que Pillow
decodifique correctamente BC5/BC7 REALES de Skyrim sigue sin verificarse (EXP-003, con material real).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import struct
import sys
import warnings
import zlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from sky_claw.local.native_parallax import decode
from sky_claw.local.native_parallax.decode import (
    ArchivoNormalInvalidoError,
    ConvencionNormal,
    DependenciaNoDisponibleError,
    FormatoNoSoportadoError,
    NormalDecoder,
)
from sky_claw.local.native_parallax.research.authored_dataset import decode_normal_image

# ---------------------------------------------------------------------------
# Fixtures sintéticas
# ---------------------------------------------------------------------------

_DDSD_BASICO = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000  # CAPS | HEIGHT | WIDTH | PIXELFORMAT | LINEARSIZE


def _dds(ancho: int, alto: int, payload: bytes, *, fourcc: bytes, dxgi: int | None = None) -> bytes:
    """DDS mínimo de un solo nivel: cabecera legacy con FourCC (+ extensión DX10 si ``dxgi``)."""
    formato_de_pixel = struct.pack("<II4sIIIII", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    cabecera = struct.pack("<7I", 124, _DDSD_BASICO, alto, ancho, len(payload), 0, 1)
    cabecera += b"\0" * 44 + formato_de_pixel + struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    extension = struct.pack("<5I", dxgi, 3, 0, 1, 0) if dxgi is not None else b""
    return b"DDS " + cabecera + extension + payload


def _bloque_bc5(r: int, g: int) -> bytes:
    """Bloque BC5 de 4x4 con R y G sólidos: extremo 0 = valor, índices en cero."""
    return bytes([r, 255]) + b"\0" * 6 + bytes([g, 255]) + b"\0" * 6


def _bloque_bc7_modo6(r: int, g: int, b: int, a: int = 127, p: int = 1) -> bytes:
    """Bloque BC7 modo 6 sólido: extremos iguales (7 bits por canal + bit p), índices en cero.

    El valor de 8 bits resultante es ``(c << 1) | p`` para cada canal.
    """
    bits = 0
    posicion = 0

    def poner(valor: int, ancho: int) -> None:
        nonlocal bits, posicion
        bits |= (valor & ((1 << ancho) - 1)) << posicion
        posicion += ancho

    poner(0b1000000, 7)  # modo 6: seis ceros y un uno
    for canal in (r, g, b, a):
        poner(canal, 7)  # extremo 0
        poner(canal, 7)  # extremo 1
    poner(p, 1)
    poner(p, 1)
    return bits.to_bytes(16, "little")


def _escribir(tmp_path: Path, nombre: str, contenido: bytes) -> Path:
    ruta = tmp_path / nombre
    ruta.write_bytes(contenido)
    return ruta


def _png(tmp_path: Path, pixeles: np.ndarray, nombre: str = "n.png") -> Path:
    ruta = tmp_path / nombre
    Image.fromarray(pixeles).save(ruta)
    return ruta


def _png_16_bits(tmp_path: Path, tipo_de_color: int) -> Path:
    """PNG de 16 bits por canal armado a mano (Pillow no escribe RGB de 16 bits): ``2`` = RGB, ``6`` = RGBA."""

    def trozo(tipo: bytes, datos: bytes) -> bytes:
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)

    canales = {2: 3, 6: 4}[tipo_de_color]
    fila = b"\x00" + b"\x80\x00" * (canales * 2)
    contenido = (
        b"\x89PNG\r\n\x1a\n"
        + trozo(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 16, tipo_de_color, 0, 0, 0))
        + trozo(b"IDAT", zlib.compress(fila * 2))
        + trozo(b"IEND", b"")
    )
    return _escribir(tmp_path, "n16.png", contenido)


def _dds_clasico(flags: int, bits: int, masc_r: int, masc_g: int, masc_b: int, masc_a: int, payload: bytes) -> bytes:
    """DDS sin comprimir de cabecera clásica: el formato de píxel lo describen las máscaras de canal."""
    formato_de_pixel = struct.pack("<II4sIIIII", 32, flags, b"\0\0\0\0", bits, masc_r, masc_g, masc_b, masc_a)
    cabecera = struct.pack("<7I", 124, _DDSD_BASICO, 4, 4, len(payload), 0, 1)
    cabecera += b"\0" * 44 + formato_de_pixel + struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return b"DDS " + cabecera + payload


def _vector(valor8: tuple[int, int, int]) -> np.ndarray:
    """Vector unitario esperado para un píxel RGB de 8 bits (misma fórmula que el contrato)."""
    v = np.asarray(valor8, dtype=np.float64) / 255.0 * 2.0 - 1.0
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------------------
# Camino RGB (PNG, uncompressed, BC1)
# ---------------------------------------------------------------------------


def test_png_normal_plana_devuelve_vector_unitario_directx(tmp_path: Path) -> None:
    ruta = _png(tmp_path, np.full((4, 4, 3), (128, 128, 255), dtype=np.uint8))

    resultado = NormalDecoder().decode(ruta)

    assert resultado.normal.shape == (4, 4, 3)
    np.testing.assert_allclose(resultado.normal[2, 3], _vector((128, 128, 255)), rtol=0, atol=1e-12)
    np.testing.assert_allclose(np.linalg.norm(resultado.normal, axis=-1), 1.0, rtol=0, atol=1e-12)
    assert resultado.source_format == "PNG"
    assert resultado.z_reconstructed is False
    assert resultado.clamped_pixels == 0


def test_png_equivale_al_oraculo_de_research(tmp_path: Path) -> None:
    pixeles = np.random.default_rng(7).integers(0, 256, size=(16, 16, 3), dtype=np.uint8)
    ruta = _png(tmp_path, pixeles)

    producto = NormalDecoder().decode(ruta).normal
    oraculo = decode_normal_image(ruta)

    np.testing.assert_allclose(producto, oraculo, rtol=0, atol=1e-12)


def test_textura_rectangular_conserva_alto_y_ancho(tmp_path: Path) -> None:
    pixeles = np.random.default_rng(3).integers(0, 256, size=(3, 5, 3), dtype=np.uint8)
    ruta = _png(tmp_path, pixeles)

    resultado = NormalDecoder().decode(ruta)

    assert resultado.normal.shape == (3, 5, 3)
    assert resultado.confidence.shape == (3, 5)


def test_el_canal_alfa_se_ignora(tmp_path: Path) -> None:
    rgb = np.random.default_rng(5).integers(0, 256, size=(4, 4, 3), dtype=np.uint8)
    alfa = np.random.default_rng(6).integers(0, 256, size=(4, 4, 1), dtype=np.uint8)
    con_alfa = _png(tmp_path, np.concatenate([rgb, alfa], axis=-1), "rgba.png")
    sin_alfa = _png(tmp_path, rgb, "rgb.png")

    a = NormalDecoder().decode(con_alfa).normal
    b = NormalDecoder().decode(sin_alfa).normal

    np.testing.assert_array_equal(a, b)


def test_dds_sin_comprimir_equivale_al_png(tmp_path: Path) -> None:
    pixeles = np.random.default_rng(11).integers(0, 256, size=(8, 8, 4), dtype=np.uint8)
    ruta_dds = tmp_path / "n.dds"
    Image.fromarray(pixeles).save(ruta_dds, format="DDS")
    ruta_png = _png(tmp_path, pixeles[..., :3])

    dds = NormalDecoder().decode(ruta_dds)
    png = NormalDecoder().decode(ruta_png)

    np.testing.assert_allclose(dds.normal, png.normal, rtol=0, atol=1e-12)
    assert dds.source_format == "DDS"
    assert dds.dds_pixel_format is None  # Pillow solo informa el formato de píxel de los DDS de bloques
    assert dds.z_reconstructed is False


def test_dds_bc1_usa_el_camino_rgb_sin_reconstruir_z(tmp_path: Path) -> None:
    # bloque BC1: color0 = (R5=16, G6=32, B5=31), color1 = 0, índices en cero => todos los píxeles = color0
    color0 = (16 << 11) | (32 << 5) | 31
    bloque = struct.pack("<HHI", color0, 0, 0)
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, bloque, fourcc=b"DXT1"))

    resultado = NormalDecoder().decode(ruta)

    esperado_rgb = np.asarray(Image.open(ruta).convert("RGB"))[0, 0]
    np.testing.assert_allclose(resultado.normal[0, 0], _vector(tuple(int(c) for c in esperado_rgb)), atol=1e-12)
    assert resultado.z_reconstructed is False
    assert resultado.dds_pixel_format == "DXT1"


def test_dds_bc7_modo6_solido_da_el_valor_esperado(tmp_path: Path) -> None:
    # (c << 1) | p con p = 1: R = 201, G = 129, B = 255
    bloque = _bloque_bc7_modo6(r=100, g=64, b=127)
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, bloque, fourcc=b"DX10", dxgi=98))

    resultado = NormalDecoder().decode(ruta)

    np.testing.assert_allclose(resultado.normal[1, 2], _vector((201, 129, 255)), rtol=0, atol=1e-12)
    assert resultado.source_format == "DDS"
    assert resultado.dds_pixel_format == "BC7"
    assert resultado.z_reconstructed is False


# ---------------------------------------------------------------------------
# BC5: reconstrucción de Z
# ---------------------------------------------------------------------------


def test_bc5_reconstruye_z_y_devuelve_vector_unitario(tmp_path: Path) -> None:
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, _bloque_bc5(200, 64), fourcc=b"ATI2"))

    resultado = NormalDecoder().decode(ruta)

    x, y = 200 / 255 * 2 - 1, 64 / 255 * 2 - 1
    esperado = np.array([x, y, np.sqrt(1.0 - x * x - y * y)])
    np.testing.assert_allclose(resultado.normal[0, 0], esperado, rtol=0, atol=1e-12)
    np.testing.assert_allclose(np.linalg.norm(resultado.normal, axis=-1), 1.0, rtol=0, atol=1e-12)
    assert resultado.z_reconstructed is True
    assert resultado.clamped_pixels == 0
    assert resultado.source_format == "DDS"
    assert resultado.dds_pixel_format == "BC5"


def test_bc5_fuera_del_disco_unidad_fija_z_en_cero_y_lo_cuenta(tmp_path: Path) -> None:
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, _bloque_bc5(255, 255), fourcc=b"ATI2"))

    resultado = NormalDecoder().decode(ruta)

    np.testing.assert_allclose(resultado.normal[0, 0], [np.sqrt(0.5), np.sqrt(0.5), 0.0], rtol=0, atol=1e-12)
    assert resultado.clamped_pixels == 16
    assert resultado.z_reconstructed is True


def test_bc5_de_dos_bloques_respeta_la_geometria_rectangular(tmp_path: Path) -> None:
    payload = _bloque_bc5(128, 128) + _bloque_bc5(200, 64)  # ancho 8, alto 4: bloque izquierdo y derecho
    ruta = _escribir(tmp_path, "n.dds", _dds(8, 4, payload, fourcc=b"ATI2"))

    resultado = NormalDecoder().decode(ruta)

    assert resultado.normal.shape == (4, 8, 3)
    # Z se reconstruye, así que el vector ya es unitario y X conserva el valor decodificado
    assert resultado.normal[0, 0, 0] == pytest.approx(128 / 255 * 2 - 1, abs=1e-12)
    assert resultado.normal[0, 7, 0] == pytest.approx(200 / 255 * 2 - 1, abs=1e-12)
    izquierda, derecha = resultado.normal[:, :4], resultado.normal[:, 4:]
    assert np.ptp(izquierda[..., 0]) == 0.0
    assert np.ptp(derecha[..., 0]) == 0.0
    assert derecha[0, 0, 0] > izquierda[0, 0, 0]


def test_dds_sin_formato_de_pixel_conocido_falla_cerrado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, _bloque_bc5(200, 64), fourcc=b"ATI2"))
    monkeypatch.setattr(decode, "_formato_de_pixel_dds", lambda imagen: None)

    # sin saber que es BC5 se leería B = 0 como un vector inclinado: tiene que rechazarse
    with pytest.raises(FormatoNoSoportadoError, match="no verificado"):
        NormalDecoder().decode(ruta)


def test_bc5_con_signo_se_rechaza(tmp_path: Path) -> None:
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, _bloque_bc5(0, 0), fourcc=b"BC5S"))

    with pytest.raises(FormatoNoSoportadoError, match="BC5S"):
        NormalDecoder().decode(ruta)


# ---------------------------------------------------------------------------
# Convención
# ---------------------------------------------------------------------------


def test_fuente_opengl_se_pasa_a_directx_invirtiendo_y(tmp_path: Path) -> None:
    ruta = _png(tmp_path, np.full((2, 2, 3), (200, 64, 230), dtype=np.uint8))

    directx = NormalDecoder().decode(ruta).normal
    opengl = NormalDecoder(source_convention=ConvencionNormal.OPENGL).decode(ruta).normal

    np.testing.assert_array_equal(opengl[..., 0], directx[..., 0])
    np.testing.assert_array_equal(opengl[..., 1], -directx[..., 1])
    np.testing.assert_array_equal(opengl[..., 2], directx[..., 2])


def test_la_fuente_por_defecto_es_directx_y_no_invierte(tmp_path: Path) -> None:
    ruta = _png(tmp_path, np.full((2, 2, 3), (200, 64, 230), dtype=np.uint8))

    resultado = NormalDecoder().decode(ruta).normal

    np.testing.assert_allclose(resultado[0, 0], _vector((200, 64, 230)), rtol=0, atol=1e-12)


# ---------------------------------------------------------------------------
# Confianza por nz
# ---------------------------------------------------------------------------


def test_la_confianza_es_nz_acotado_a_cero_uno(tmp_path: Path) -> None:
    pixeles = np.array([[(128, 128, 255), (255, 128, 128), (255, 128, 0)]], dtype=np.uint8)
    ruta = _png(tmp_path, pixeles)

    resultado = NormalDecoder().decode(ruta)

    nz = resultado.normal[..., 2]
    np.testing.assert_array_equal(resultado.confidence, np.clip(nz, 0.0, 1.0))
    assert resultado.confidence[0, 0] > 0.99
    assert resultado.confidence[0, 2] == 0.0
    assert resultado.confidence.min() >= 0.0 and resultado.confidence.max() <= 1.0


def test_el_resultado_es_inmutable_y_determinista(tmp_path: Path) -> None:
    ruta = _png(tmp_path, np.random.default_rng(1).integers(0, 256, size=(4, 4, 3), dtype=np.uint8))

    a = NormalDecoder().decode(ruta)
    b = NormalDecoder().decode(ruta)

    np.testing.assert_array_equal(a.normal, b.normal)
    assert not a.normal.flags.writeable
    assert not a.confidence.flags.writeable
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.clamped_pixels = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Errores tipados
# ---------------------------------------------------------------------------


def test_archivo_inexistente_da_error_tipado(tmp_path: Path) -> None:
    with pytest.raises(ArchivoNormalInvalidoError):
        NormalDecoder().decode(tmp_path / "no_existe.png")


def test_archivo_que_no_es_imagen_da_error_tipado(tmp_path: Path) -> None:
    ruta = _escribir(tmp_path, "basura.png", b"esto no es una imagen")

    with pytest.raises(ArchivoNormalInvalidoError):
        NormalDecoder().decode(ruta)


def test_png_corrupto_en_los_datos_da_error_tipado(tmp_path: Path) -> None:
    ruta = _png(tmp_path, np.zeros((4, 4, 3), dtype=np.uint8))
    contenido = bytearray(ruta.read_bytes())
    contenido[contenido.index(b"IDAT") + 6] ^= 0xFF  # un byte dentro del bloque de datos
    ruta.write_bytes(bytes(contenido))

    with pytest.raises(ArchivoNormalInvalidoError):
        NormalDecoder().decode(ruta)


def test_value_error_de_pillow_se_traduce_a_error_tipado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ruta = _png(tmp_path, np.zeros((4, 4, 3), dtype=np.uint8))

    def abrir(*args: object, **kwargs: object) -> Image.Image:
        raise ValueError("cabecera inválida")

    monkeypatch.setattr(Image, "open", abrir)

    with pytest.raises(ArchivoNormalInvalidoError, match="cabecera inválida"):
        NormalDecoder().decode(ruta)


@pytest.mark.parametrize(
    ("modo", "pixeles"),
    [
        ("L", np.zeros((4, 4), dtype=np.uint8)),
        ("I;16", np.zeros((4, 4), dtype=np.uint16)),
    ],
)
def test_modos_que_no_son_normales_rgb_se_rechazan(tmp_path: Path, modo: str, pixeles: np.ndarray) -> None:
    ruta = tmp_path / "n.png"
    Image.fromarray(pixeles).save(ruta)
    with Image.open(ruta) as leida:
        assert leida.mode == modo

    with pytest.raises(FormatoNoSoportadoError, match=re.escape(f"modo '{modo}'")):
        NormalDecoder().decode(ruta)


def test_paleta_se_rechaza(tmp_path: Path) -> None:
    ruta = tmp_path / "n.png"
    Image.new("P", (4, 4)).save(ruta)

    with pytest.raises(FormatoNoSoportadoError, match=re.escape("modo 'P'")):
        NormalDecoder().decode(ruta)


# ---------------------------------------------------------------------------
# Entradas no verificadas (revisión de Codex en #756)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contenedor", ["JPEG", "BMP", "WEBP"])
def test_contenedores_fuera_de_png_y_dds_se_rechazan(tmp_path: Path, contenedor: str) -> None:
    ruta = tmp_path / f"n.{contenedor.lower()}"
    Image.fromarray(np.full((8, 8, 3), (128, 128, 255), dtype=np.uint8)).save(ruta, format=contenedor)

    with pytest.raises(FormatoNoSoportadoError, match=contenedor):
        NormalDecoder().decode(ruta)


@pytest.mark.parametrize(
    ("fourcc", "dxgi"),
    [(b"XXXX", None), (b"DX10", 9999)],
    ids=["fourcc-desconocido", "dxgi-desconocido"],
)
def test_dds_con_formato_que_pillow_no_implementa_da_error_tipado(
    tmp_path: Path, fourcc: bytes, dxgi: int | None
) -> None:
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, b"\0" * 16, fourcc=fourcc, dxgi=dxgi))

    with pytest.raises(FormatoNoSoportadoError):
        NormalDecoder().decode(ruta)


@pytest.mark.parametrize(
    ("fourcc", "dxgi", "formato"),
    [
        (b"DXT3", None, "DXT3"),
        (b"DXT5", None, "DXT5"),  # DXT5nm guarda X en el alfa: leerlo como RGB daría normales incorrectas
        (b"DX10", 95, "BC6H"),
        (b"DX10", 96, "BC6HS"),
    ],
    ids=["bc2", "bc3", "bc6h", "bc6h-con-signo"],
)
def test_dds_de_bloques_fuera_de_la_lista_verificada_se_rechaza(
    tmp_path: Path, fourcc: bytes, dxgi: int | None, formato: str
) -> None:
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, b"\0" * 16, fourcc=fourcc, dxgi=dxgi))

    with pytest.raises(FormatoNoSoportadoError, match=formato):
        NormalDecoder().decode(ruta)


def test_dds_sin_comprimir_con_cabecera_dx10_se_acepta(tmp_path: Path) -> None:
    # DXGI_FORMAT_R8G8B8A8_UNORM (28): Pillow lo entrega como RGBA por el códec ``raw`` y sin pixel_format
    pixeles = bytes((200, 64, 230, 255)) * 16
    ruta = _escribir(tmp_path, "n.dds", _dds(4, 4, pixeles, fourcc=b"DX10", dxgi=28))

    resultado = NormalDecoder().decode(ruta)

    np.testing.assert_allclose(resultado.normal[2, 1], _vector((200, 64, 230)), rtol=0, atol=1e-12)
    assert resultado.dds_pixel_format is None
    assert resultado.z_reconstructed is False


@pytest.mark.parametrize("tope", [10, 7], ids=["entre-1x-y-2x", "mas-de-2x"])
def test_bomba_de_descompresion_en_las_dos_zonas_de_pillow_da_error_tipado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tope: int
) -> None:
    """Con 16 píxeles: tope 10 cae entre 1x y 2x (Pillow solo ADVIERTE y seguiría decodificando); tope 7
    supera 2x (Pillow lanza). Las dos deben terminar en el error tipado antes de reservar los arrays."""
    ruta = _png(tmp_path, np.zeros((4, 4, 3), dtype=np.uint8))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", tope)

    # ``DecompressionBombWarning`` hereda de ``RuntimeWarning`` y pyproject ya eleva los RuntimeWarning a error
    # dentro de pytest: sin esta línea la prueba pasaría aunque el decodificador no hiciera nada (el mutante
    # «la bomba solo advierte» sobrevivía). Acá el filtro es el del proceso real: la advertencia se ignora, y
    # solo el filtro propio del decodificador puede convertirla en error.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(ArchivoNormalInvalidoError):
            NormalDecoder().decode(ruta)


_DDPF_RGB = 0x40
_DDPF_RGB_ALFA = 0x41

# (nombre, flags, bits, máscaras R, G, B, A). Un DDS sin comprimir de cabecera clásica lo describen sus máscaras
# y Pillow acepta cualquiera (verificado ejecutando): estas son TODAS las familias que importan, enumeradas.
_DDS_CLASICOS_DE_8_BITS = [
    ("rgb24-bgr", _DDPF_RGB, 24, 0xFF0000, 0xFF00, 0xFF, 0),
    ("rgb24-rgb", _DDPF_RGB, 24, 0xFF, 0xFF00, 0xFF0000, 0),
    ("rgba32-bgra", _DDPF_RGB_ALFA, 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000),
    ("rgba32-rgba", _DDPF_RGB_ALFA, 32, 0xFF, 0xFF00, 0xFF0000, 0xFF000000),
    ("x8r8g8b8", _DDPF_RGB, 32, 0xFF0000, 0xFF00, 0xFF, 0),
]
_DDS_CLASICOS_QUE_NO_SON_DE_8_BITS = [
    ("r5g6b5", _DDPF_RGB, 16, 0xF800, 0x07E0, 0x001F, 0),
    ("a1r5g5b5", _DDPF_RGB_ALFA, 16, 0x7C00, 0x03E0, 0x001F, 0x8000),
    ("a4r4g4b4", _DDPF_RGB_ALFA, 16, 0x0F00, 0x00F0, 0x000F, 0xF000),
    ("r16g16-sin-azul", _DDPF_RGB, 32, 0xFFFF, 0xFFFF0000, 0, 0),
    ("rgba64", _DDPF_RGB_ALFA, 64, 0xFFFF, 0xFFFF0000, 0, 0),
    ("mascaras-repetidas", _DDPF_RGB, 24, 0xFF, 0xFF, 0xFF, 0),
    ("mascara-de-dos-bytes", _DDPF_RGB, 24, 0xFFFF, 0xFF0000, 0xFF, 0),
    ("8-bits-por-pixel", _DDPF_RGB, 8, 0xFF, 0xFF, 0xFF, 0),
    # máscaras de un byte pero bits por píxel que no cuadran con ellas: la lectura sería otra
    ("16-bits-con-mascaras-de-24", _DDPF_RGB, 16, 0xFF, 0xFF00, 0xFF0000, 0),
    ("8-bits-con-mascaras-distintas", _DDPF_RGB, 8, 0xFF, 0xFF00, 0xFF0000, 0),
    # una máscara por encima del ancho del píxel: Pillow lee ``bits // 8`` bytes, así que ese canal vale 0 siempre
    ("24-bits-con-azul-fuera-del-pixel", _DDPF_RGB, 24, 0xFF, 0xFF00, 0xFF000000, 0),
    ("24-bits-con-alfa-fuera-del-pixel", _DDPF_RGB_ALFA, 24, 0xFF0000, 0xFF00, 0xFF, 0xFF000000),
]


@pytest.mark.parametrize("tipo_de_color", [2, 6], ids=["rgb16", "rgba16"])
def test_png_de_16_bits_por_canal_se_rechaza(tmp_path: Path, tipo_de_color: int) -> None:
    # Pillow abre estos PNG como modo RGB/RGBA (con rawmode ``RGB;16B``) y los reduce a 8 bits al cargar: sin
    # este rechazo darían normales cuantizadas sin ningún error (revisión de CodeRabbit en #756).
    ruta = _png_16_bits(tmp_path, tipo_de_color)
    with Image.open(ruta) as leida:
        assert leida.mode in ("RGB", "RGBA")

    with pytest.raises(FormatoNoSoportadoError, match="8 bits"):
        NormalDecoder().decode(ruta)


@pytest.mark.parametrize(
    ("flags", "bits", "mr", "mg", "mb", "ma"),
    [caso[1:] for caso in _DDS_CLASICOS_DE_8_BITS],
    ids=[caso[0] for caso in _DDS_CLASICOS_DE_8_BITS],
)
def test_dds_clasico_de_8_bits_por_canal_se_acepta_con_el_valor_correcto(
    tmp_path: Path, flags: int, bits: int, mr: int, mg: int, mb: int, ma: int
) -> None:
    # Cada pixel vale R=200, G=64, B=230 (+ alfa 255 si hay máscara): se arma según las máscaras.
    valores = {mr: 200, mg: 64, mb: 230, ma: 255}
    palabra = sum(valor * (mascara & -mascara) for mascara, valor in valores.items() if mascara)
    pixeles = palabra.to_bytes(bits // 8, "little") * 16
    ruta = _escribir(tmp_path, "n.dds", _dds_clasico(flags, bits, mr, mg, mb, ma, pixeles))

    resultado = NormalDecoder().decode(ruta)

    np.testing.assert_allclose(resultado.normal[1, 2], _vector((200, 64, 230)), rtol=0, atol=1e-12)


@pytest.mark.parametrize(
    ("flags", "bits", "mr", "mg", "mb", "ma"),
    [caso[1:] for caso in _DDS_CLASICOS_QUE_NO_SON_DE_8_BITS],
    ids=[caso[0] for caso in _DDS_CLASICOS_QUE_NO_SON_DE_8_BITS],
)
def test_dds_clasico_que_no_es_de_8_bits_por_canal_se_rechaza(
    tmp_path: Path, flags: int, bits: int, mr: int, mg: int, mb: int, ma: int
) -> None:
    # Hermano del PNG de 16 bits: el códec ``dds_rgb`` de Pillow acepta cualquier máscara y escala cada canal a
    # 8 bits en silencio (565, 1555, 4444) o deja un canal en cero (R16G16 sin azul).
    ruta = _escribir(tmp_path, "n.dds", _dds_clasico(flags, bits, mr, mg, mb, ma, b"\0" * (bits // 8) * 16))
    with Image.open(ruta) as leida:
        assert leida.mode in ("RGB", "RGBA")

    with pytest.raises(FormatoNoSoportadoError, match="8 bits"):
        NormalDecoder().decode(ruta)


@pytest.mark.parametrize(
    "tiles",
    [
        (),
        (("?", None),),
        (("zip", None),),
        (("zip", ("RGB",)),),
        (("zip", ["RGB"]),),
        (("zip", "RGB;16B"),),
        (("raw", "L"),),
        (("dds_rgb", None),),
        (("dds_rgb", (32,)),),
        (("dds_rgb", (32, "mascaras")),),
        (("dds_rgb", (32, ([1], 2, 3))),),
        (("dds_rgb", (32, (0xFF, 0xFF00))),),
        (("codec-desconocido", "RGB"),),
        (("zip", "RGB"), ("codec-desconocido", None)),
    ],
    ids=lambda tiles: repr(tiles)[:48],
)
def test_lo_que_no_se_reconoce_cuenta_como_que_no_es_de_8_bits(tiles: tuple[tuple[str, object], ...]) -> None:
    assert decode._ocho_bits_por_canal(tiles) is False


def test_un_tile_ilegible_falla_cerrado() -> None:
    # Si Pillow cambiara la forma de ``tile`` o de sus elementos, el decodificador debe rechazar y no suponer.
    assert decode._tiles(object()) == (("?", None),)
    assert decode._tiles(SimpleNamespace(tile=None)) == (("?", None),)
    assert decode._tiles(SimpleNamespace(tile=[object()])) == (("?", None),)
    assert decode._tiles(SimpleNamespace(tile=[SimpleNamespace(codec_name="zip", args="RGB")])) == (("zip", "RGB"),)
    assert decode._ocho_bits_por_canal(decode._tiles(object())) is False


def test_la_lista_cerrada_de_layouts_de_8_bits_esta_congelada() -> None:
    assert frozenset({"RGB", "RGBA"}) == decode._RAWMODES_DE_8_BITS
    assert frozenset({0xFF, 0xFF00, 0xFF0000, 0xFF000000}) == decode._MASCARAS_DE_8_BITS


# ---------------------------------------------------------------------------
# Dependencias perezosas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ausente", ["numpy", "PIL"])
def test_sin_la_dependencia_el_uso_da_error_tipado_con_instrucciones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ausente: str
) -> None:
    ruta = _png(tmp_path, np.zeros((4, 4, 3), dtype=np.uint8))
    monkeypatch.setitem(sys.modules, ausente, None)

    with pytest.raises(DependenciaNoDisponibleError, match=r"sky-claw\[native-parallax\]"):
        NormalDecoder().decode(ruta)


def test_importar_el_modulo_no_requiere_numpy_ni_pillow(monkeypatch: pytest.MonkeyPatch) -> None:
    # Se carga una COPIA aislada del archivo. Recargar ``decode`` en el lugar rehace sus clases de error en el
    # mismo espacio de nombres y deja a los demás tests comparando contra clases viejas (CodeRabbit, #756).
    nombre = "decode_sin_el_extra"
    spec = importlib.util.spec_from_file_location(nombre, Path(decode.__file__))
    assert spec is not None
    assert spec.loader is not None
    copia = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "numpy", None)
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, nombre, copia)  # ``dataclass`` necesita el módulo registrado

    spec.loader.exec_module(copia)

    assert hasattr(copia, "NormalDecoder")
    assert copia.NormalDecoder is not NormalDecoder
    # El módulo real quedó intacto: sus clases son las que importó este archivo.
    assert decode.FormatoNoSoportadoError is FormatoNoSoportadoError
    assert decode.NormalDecoder is NormalDecoder

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
import importlib
import re
import struct
import sys
import warnings
from pathlib import Path

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


def test_importar_el_modulo_no_requiere_numpy_ni_pillow() -> None:
    previos = {nombre: sys.modules.get(nombre) for nombre in ("numpy", "PIL")}
    try:
        sys.modules["numpy"] = None  # type: ignore[assignment]
        sys.modules["PIL"] = None  # type: ignore[assignment]
        recargado = importlib.reload(decode)
        assert hasattr(recargado, "NormalDecoder")
    finally:
        for nombre, modulo in previos.items():
            if modulo is None:
                sys.modules.pop(nombre, None)
            else:
                sys.modules[nombre] = modulo
        importlib.reload(decode)

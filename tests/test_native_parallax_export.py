"""NP-E0a: núcleo puro del exportador DDS del generador nativo de parallax (ADR 0014 §2.5).

Cubre lo que NO necesita el ``texconv`` real: el candado de versión fijada (fail-closed), los mips en float, la
cuantización a 8 bits y el validador del contenedor BC4 que entregue ``texconv``. Los flags de ``texconv`` y su
ejecución son NP-E0b, cuando se verifiquen contra el binario real (ver el docstring de ``export.py``).

Los contenedores DDS de estos tests se arman a mano con la disposición pública de Microsoft ("Programming Guide for
DDS": cabecera de 124 bytes tras la marca, extensión DX10 de 20 bytes, BC4 = 8 bytes por bloque de 4x4). El
helper NO reusa código del módulo: si lo hiciera, un error de disposición se confirmaría a sí mismo. Pillow actúa
de lector independiente para el nivel base.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from sky_claw.local.native_parallax import export
from sky_claw.local.native_parallax.export import (
    TEXCONV_PIN_VIGENTE,
    AlturaNoExportableError,
    ContenedorDdsInvalidoError,
    DimensionesNoExportablesError,
    ExportError,
    PinInvalidoError,
    TexconvNoCoincideError,
    TexconvNoFijadoError,
    TexconvPin,
    cantidad_de_niveles,
    construir_dds_l8,
    cuantizar_a_8_bits,
    generar_mips,
    inspeccionar_dds_bc4,
    tamano_de_nivel_bc4,
    verificar_texconv,
)

_ORIGEN_OFICIAL = "https://github.com/microsoft/DirectXTex/releases/tag/oct2026"
_SHA = "a" * 64

# ---------------------------------------------------------------------------
# Helper independiente: arma un DDS BC4 con la disposición pública de Microsoft
# ---------------------------------------------------------------------------

_DDSD_BASICO = 0x1 | 0x2 | 0x4 | 0x1000  # CAPS | HEIGHT | WIDTH | PIXELFORMAT
_DDSD_HEIGHT = 0x2
_DDSD_WIDTH = 0x4
_DDSD_DEPTH = 0x800000
_DDSD_MIPMAPCOUNT = 0x20000
_DDSD_LINEARSIZE = 0x80000
_DDPF_FOURCC = 0x4
_DDSCAPS_TEXTURE = 0x1000
_DDSCAPS_COMPLEX = 0x8
_DDSCAPS_MIPMAP = 0x400000
_DXGI_BC4_UNORM = 80
_DXGI_BC4_SNORM = 81
_DXGI_BC5_UNORM = 83
_BLOQUE_BC4 = bytes([200, 200]) + b"\0" * 6  # extremos iguales: todo el bloque vale 200


def _bytes_de_nivel(ancho: int, alto: int) -> int:
    return max(1, (ancho + 3) // 4) * max(1, (alto + 3) // 4) * 8


def _dds(
    ancho: int,
    alto: int,
    niveles: int,
    *,
    fourcc: bytes = b"BC4U",
    dxgi: int | None = None,
    payload_extra: int = 0,
    mipmapcount_declarado: int | None = None,
    caps2: int = 0,
    tamano_cabecera: int = 124,
    marca: bytes = b"DDS ",
    tamano_pf: int = 32,
    recorte: int = 0,
    con_bandera_de_mips: bool | None = None,
    array_size: int = 1,
    dimension: int = 3,
    misc: int = 0,
    quitar_banderas: int = 0,
    agregar_banderas: int = 0,
    profundidad: int = 0,
    banderas_pf: int = _DDPF_FOURCC,
) -> bytes:
    tamanos = [_bytes_de_nivel(max(1, ancho >> i), max(1, alto >> i)) for i in range(niveles)]
    hay_mips = niveles > 1 if con_bandera_de_mips is None else con_bandera_de_mips
    banderas = (
        _DDSD_BASICO | _DDSD_LINEARSIZE | (_DDSD_MIPMAPCOUNT if hay_mips else 0)
    ) & ~quitar_banderas | agregar_banderas
    caps = _DDSCAPS_TEXTURE | ((_DDSCAPS_COMPLEX | _DDSCAPS_MIPMAP) if hay_mips else 0)
    contados = niveles if mipmapcount_declarado is None else mipmapcount_declarado
    formato_de_pixel = struct.pack("<II4sIIIII", tamano_pf, banderas_pf, fourcc, 0, 0, 0, 0, 0)
    cabecera = struct.pack("<7I", tamano_cabecera, banderas, alto, ancho, tamanos[0], profundidad, contados)
    cabecera += b"\0" * 44 + formato_de_pixel + struct.pack("<5I", caps, caps2, 0, 0, 0)
    extension = struct.pack("<5I", dxgi, dimension, misc, array_size, 0) if dxgi is not None else b""
    payload = _BLOQUE_BC4 * (sum(tamanos) // 8) + b"\0" * payload_extra
    contenedor = marca + cabecera + extension + payload
    return contenedor[: len(contenedor) - recorte] if recorte else contenedor


def _dds_dx10(ancho: int, alto: int, niveles: int, dxgi: int = _DXGI_BC4_UNORM, **kwargs: object) -> bytes:
    return _dds(ancho, alto, niveles, fourcc=b"DX10", dxgi=dxgi, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# generar_mips: área-average en float, determinista
# ---------------------------------------------------------------------------


def test_la_cadena_de_mips_llega_hasta_1x1_y_respeta_la_geometria_no_cuadrada() -> None:
    altura = np.full((4, 16), 0.5)

    niveles = generar_mips(altura)

    assert [n.shape for n in niveles] == [(4, 16), (2, 8), (1, 4), (1, 2), (1, 1)]
    assert len(niveles) == cantidad_de_niveles(16, 4)


def test_el_primer_nivel_es_la_entrada_en_float32_y_los_niveles_son_de_solo_lectura() -> None:
    rng = np.random.default_rng(3)
    altura = rng.random((8, 8))

    niveles = generar_mips(altura)

    assert all(n.dtype == np.float32 for n in niveles)
    np.testing.assert_array_equal(niveles[0], altura.astype(np.float32))
    assert all(not n.flags.writeable for n in niveles)


def test_cada_nivel_es_el_promedio_de_area_exacto_de_su_nivel_anterior() -> None:
    # Todos los valores y promedios son exactos en binario: la igualdad es a la letra, sin tolerancia.
    altura = np.array(
        [
            [0.0, 0.0, 1.0, 1.0],
            [0.0, 1.0, 1.0, 1.0],
            [0.5, 0.5, 1.0, 0.0],
            [0.0, 0.0, 0.0, 0.5],
        ]
    )

    niveles = generar_mips(altura)

    np.testing.assert_array_equal(niveles[1], np.array([[0.25, 1.0], [0.25, 0.375]], dtype=np.float32))
    np.testing.assert_array_equal(niveles[2], np.array([[0.46875]], dtype=np.float32))


def test_los_mips_se_promedian_en_float64_y_se_redondean_una_sola_vez_a_float32() -> None:
    # Promedio exacto del bloque = 0.25 + 3 * 2**-26: cae justo en el empate entre dos float32 y gana el par
    # (0.25 + 2**-24). Acumular en float32 redondea de más en el camino y da 0.25 + 2**-25.
    altura = np.zeros((4, 4), dtype=np.float32)
    altura[0, 0] = 1.0
    altura[0, 1] = altura[1, 0] = altura[1, 1] = np.float32(2.0**-24)

    niveles = generar_mips(altura)

    assert niveles[1][0, 0] == np.float32(0.25 + 2.0**-24)


def test_el_promedio_de_la_textura_se_conserva_en_todos_los_niveles() -> None:
    rng = np.random.default_rng(11)
    altura = rng.random((64, 32))

    niveles = generar_mips(altura)

    for nivel in niveles:
        assert float(np.mean(nivel, dtype=np.float64)) == pytest.approx(float(np.mean(altura)), abs=1e-6)


def test_un_campo_constante_sigue_constante_en_toda_la_cadena() -> None:
    niveles = generar_mips(np.full((8, 8), 0.3))

    for nivel in niveles:
        np.testing.assert_array_equal(nivel, np.float32(0.3))


def test_generar_mips_es_determinista_bit_a_bit() -> None:
    rng = np.random.default_rng(5)
    altura = rng.random((32, 32))

    a, b = generar_mips(altura), generar_mips(altura)

    assert all(x.tobytes() == y.tobytes() for x, y in zip(a, b, strict=True))


@pytest.mark.parametrize(
    "altura",
    [
        np.full((8, 8), np.nan),
        np.full((8, 8), np.inf),
        np.full((8, 8), -0.001),
        np.full((8, 8), 1.001),
        np.zeros(8),
        np.zeros((2, 8, 8)),
        np.zeros((8, 8), dtype=np.uint8),
        np.zeros((8, 8), dtype=bool),
        np.zeros((8, 8), dtype=np.complex128),
    ],
    ids=["nan", "inf", "negativa", "mayor-a-1", "1d", "3d", "uint8", "bool", "complejo"],
)
def test_generar_mips_rechaza_alturas_que_no_son_un_campo_float_finito_en_0_1(altura: np.ndarray) -> None:
    with pytest.raises(AlturaNoExportableError):
        generar_mips(altura)


@pytest.mark.parametrize("forma", [(8, 12), (12, 8), (6, 6), (8, 2), (2, 8), (3, 8), (5, 5)])
def test_generar_mips_rechaza_dimensiones_que_no_son_potencia_de_2_o_menores_que_4(forma: tuple[int, int]) -> None:
    with pytest.raises(DimensionesNoExportablesError):
        generar_mips(np.zeros(forma))


def test_generar_mips_rechaza_una_entrada_que_no_es_un_ndarray() -> None:
    with pytest.raises(AlturaNoExportableError):
        generar_mips([[0.5] * 8] * 8)  # type: ignore[arg-type]


def test_los_errores_de_exportacion_comparten_una_base() -> None:
    for tipo in (
        AlturaNoExportableError,
        DimensionesNoExportablesError,
        ContenedorDdsInvalidoError,
        PinInvalidoError,
        TexconvNoFijadoError,
        TexconvNoCoincideError,
    ):
        assert issubclass(tipo, ExportError)


# ---------------------------------------------------------------------------
# cuantizar_a_8_bits
# ---------------------------------------------------------------------------


def test_la_cuantizacion_lleva_0_a_0_y_1_a_255_y_redondea_al_par_en_los_empates() -> None:
    nivel = np.array([[0.0, 1.0], [0.5, 1.0 / 255.0]], dtype=np.float32)

    cuantizado = cuantizar_a_8_bits(nivel)

    assert cuantizado.dtype == np.uint8
    # 0.5 * 255 = 127.5: el empate va al par (128), igual en cualquier plataforma
    np.testing.assert_array_equal(cuantizado, [[0, 255], [128, 1]])


def test_la_cuantizacion_redondea_al_entero_mas_cercano_a_cada_lado_del_empate() -> None:
    nivel = np.array([[10.4995 / 255.0, 10.5005 / 255.0]], dtype=np.float64)

    np.testing.assert_array_equal(cuantizar_a_8_bits(nivel), [[10, 11]])


def test_el_resultado_de_la_cuantizacion_es_de_solo_lectura() -> None:
    assert not cuantizar_a_8_bits(np.full((2, 2), 0.5)).flags.writeable


def test_la_cuantizacion_es_monotona_y_conserva_la_forma() -> None:
    nivel = np.linspace(0.0, 1.0, 1000, dtype=np.float64).reshape(10, 100)

    cuantizado = cuantizar_a_8_bits(nivel)

    assert cuantizado.shape == nivel.shape
    assert np.all(np.diff(cuantizado.ravel().astype(np.int16)) >= 0)
    assert int(cuantizado.min()) == 0 and int(cuantizado.max()) == 255


@pytest.mark.parametrize("valor", [np.nan, np.inf, -0.5, 1.5])
def test_la_cuantizacion_rechaza_valores_fuera_de_0_1_o_no_finitos(valor: float) -> None:
    with pytest.raises(AlturaNoExportableError):
        cuantizar_a_8_bits(np.array([[0.2, valor]]))


def test_la_cuantizacion_rechaza_un_nivel_vacio_o_que_no_es_un_ndarray() -> None:
    with pytest.raises(AlturaNoExportableError):
        cuantizar_a_8_bits(np.zeros((0, 4)))
    with pytest.raises(AlturaNoExportableError):
        cuantizar_a_8_bits([[0.5, 0.5]])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TexconvPin: fail-closed
# ---------------------------------------------------------------------------


def test_el_pin_vigente_es_el_texconv_oficial_may2026_verificado() -> None:
    # Congelado por igualdad: cambiar la versión de texconv es una decisión con evidencia (huella del binario
    # oficial verificada por dos vías independientes, ver ADR 0014 «Actualización de NP-E0b») y cambia este test.
    esperado = TexconvPin(
        version="may2026",
        sha256="dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06",
        origen="https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe",
    )

    assert esperado == TEXCONV_PIN_VIGENTE


def test_un_pin_valido_se_construye() -> None:
    pin = TexconvPin(version="oct2026", sha256=_SHA, origen=_ORIGEN_OFICIAL)

    assert pin.version == "oct2026" and pin.sha256 == _SHA


@pytest.mark.parametrize(
    "campos",
    [
        {"version": "", "sha256": _SHA, "origen": _ORIGEN_OFICIAL},
        {"version": "   ", "sha256": _SHA, "origen": _ORIGEN_OFICIAL},
        {"version": None, "sha256": _SHA, "origen": _ORIGEN_OFICIAL},
        {"version": "oct2026", "sha256": None, "origen": _ORIGEN_OFICIAL},
        {"version": "oct2026", "sha256": _SHA, "origen": None},
        {"version": "oct2026", "sha256": "a" * 65, "origen": _ORIGEN_OFICIAL},
        {"version": "oct2026", "sha256": _SHA + "\n", "origen": _ORIGEN_OFICIAL},
        {"version": "oct2026", "sha256": _SHA, "origen": _ORIGEN_OFICIAL + " x"},
        {"version": "oct2026", "sha256": _SHA, "origen": _ORIGEN_OFICIAL + "\nx"},
        {"version": "oct2026", "sha256": _SHA, "origen": _ORIGEN_OFICIAL + "%2e%2e/%2e%2e/x"},
        {"version": "oct2026", "sha256": _SHA, "origen": _ORIGEN_OFICIAL + "tag/%2E%2E"},
        {"version": "oct2026", "sha256": "", "origen": _ORIGEN_OFICIAL},
        {"version": "oct2026", "sha256": "A" * 64, "origen": _ORIGEN_OFICIAL},
        {"version": "oct2026", "sha256": "a" * 63, "origen": _ORIGEN_OFICIAL},
        {"version": "oct2026", "sha256": "g" * 64, "origen": _ORIGEN_OFICIAL},
        {"version": "oct2026", "sha256": _SHA, "origen": ""},
        {"version": "oct2026", "sha256": _SHA, "origen": "https://example.com/texconv.exe"},
        {"version": "oct2026", "sha256": _SHA, "origen": "http://github.com/microsoft/DirectXTex/releases/x"},
        {"version": "oct2026", "sha256": _SHA, "origen": "https://github.com/microsoft/DirectXTex-fork/releases/x"},
        {"version": "oct2026", "sha256": _SHA, "origen": "https://github.com/microsoft/DirectXTex/releases/../../x"},
    ],
)
def test_un_pin_con_version_huella_u_origen_no_oficial_se_rechaza(campos: dict[str, object]) -> None:
    with pytest.raises(PinInvalidoError):
        TexconvPin(**campos)  # type: ignore[arg-type]


def test_verificar_texconv_sin_pin_vigente_falla_cerrado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export, "TEXCONV_PIN_VIGENTE", None)
    exe = tmp_path / "texconv.exe"
    exe.write_bytes(b"cualquier cosa")

    with pytest.raises(TexconvNoFijadoError):
        verificar_texconv(exe)


def test_verificar_texconv_acepta_el_binario_cuya_huella_coincide(tmp_path: Path) -> None:
    exe = tmp_path / "texconv.exe"
    exe.write_bytes(b"binario de prueba")
    pin = TexconvPin("oct2026", hashlib.sha256(b"binario de prueba").hexdigest(), _ORIGEN_OFICIAL)

    assert verificar_texconv(exe, pin=pin) is pin


def test_verificar_texconv_rechaza_un_binario_que_cambio_un_solo_byte(tmp_path: Path) -> None:
    exe = tmp_path / "texconv.exe"
    exe.write_bytes(b"binario de pruebb")
    pin = TexconvPin("oct2026", hashlib.sha256(b"binario de prueba").hexdigest(), _ORIGEN_OFICIAL)

    with pytest.raises(TexconvNoCoincideError):
        verificar_texconv(exe, pin=pin)


def test_verificar_texconv_rechaza_un_archivo_inexistente_o_un_directorio(tmp_path: Path) -> None:
    pin = TexconvPin("oct2026", _SHA, _ORIGEN_OFICIAL)

    with pytest.raises(TexconvNoCoincideError):
        verificar_texconv(tmp_path / "no-existe.exe", pin=pin)
    with pytest.raises(TexconvNoCoincideError):
        verificar_texconv(tmp_path, pin=pin)


@pytest.mark.skipif(sys.platform == "win32", reason="os.devnull no es un archivo regular solo en POSIX")
def test_verificar_texconv_no_acepta_un_dispositivo_aunque_su_contenido_coincida_con_la_huella() -> None:
    # /dev/null "contiene" 0 bytes, cuya huella es la del pin: sin exigir un archivo regular lo aceptaría.
    pin = TexconvPin("oct2026", hashlib.sha256(b"").hexdigest(), _ORIGEN_OFICIAL)

    with pytest.raises(TexconvNoCoincideError):
        verificar_texconv(os.devnull, pin=pin)


def test_verificar_texconv_traduce_un_error_al_consultar_la_ruta_en_un_fallo_cerrado(tmp_path: Path) -> None:
    # Un componente de 300 caracteres supera NAME_MAX en POSIX y hace fallar el stat con OSError (ENAMETOOLONG);
    # el contrato es que el llamador solo necesita atrapar ExportError.
    pin = TexconvPin("oct2026", _SHA, _ORIGEN_OFICIAL)

    with pytest.raises(TexconvNoCoincideError):
        verificar_texconv(tmp_path / ("a" * 300), pin=pin)


def test_verificar_texconv_traduce_un_error_de_lectura_en_un_fallo_cerrado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "texconv.exe"
    exe.write_bytes(b"binario de prueba")
    pin = TexconvPin("oct2026", hashlib.sha256(b"binario de prueba").hexdigest(), _ORIGEN_OFICIAL)

    def _sin_permiso(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("denegado")

    monkeypatch.setattr(Path, "open", _sin_permiso)

    with pytest.raises(TexconvNoCoincideError):
        verificar_texconv(exe, pin=pin)


def test_verificar_texconv_hashea_el_archivo_completo_aunque_supere_un_bloque_de_lectura(tmp_path: Path) -> None:
    contenido = b"x" * (export.TAMANO_DE_BLOQUE_DE_LECTURA * 3 + 7)
    exe = tmp_path / "texconv.exe"
    exe.write_bytes(contenido)
    pin = TexconvPin("oct2026", hashlib.sha256(contenido).hexdigest(), _ORIGEN_OFICIAL)

    verificar_texconv(exe, pin=pin)


def test_verificar_texconv_usa_el_pin_vigente_cuando_existe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "texconv.exe"
    exe.write_bytes(b"binario de prueba")
    pin = TexconvPin("oct2026", hashlib.sha256(b"binario de prueba").hexdigest(), _ORIGEN_OFICIAL)
    monkeypatch.setattr(export, "TEXCONV_PIN_VIGENTE", pin)

    assert verificar_texconv(exe) is pin


# ---------------------------------------------------------------------------
# tamano_de_nivel_bc4 / cantidad_de_niveles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ancho", "alto", "esperado"),
    [(4, 4, 8), (8, 4, 16), (16, 16, 128), (2, 2, 8), (1, 1, 8), (1, 4, 8), (5, 5, 32), (1024, 1024, 524288)],
)
def test_el_tamano_de_un_nivel_bc4_es_8_bytes_por_bloque_de_4x4_con_piso_de_un_bloque(
    ancho: int, alto: int, esperado: int
) -> None:
    assert tamano_de_nivel_bc4(ancho, alto) == esperado


@pytest.mark.parametrize(
    ("ancho", "alto", "esperado"), [(1, 1, 1), (4, 4, 3), (16, 4, 5), (4, 16, 5), (1024, 1024, 11)]
)
def test_la_cantidad_de_niveles_es_log2_del_lado_mayor_mas_uno(ancho: int, alto: int, esperado: int) -> None:
    assert cantidad_de_niveles(ancho, alto) == esperado


@pytest.mark.parametrize("dimensiones", [(0, 4), (4, 0), (-4, 4), (True, 4), (4, False), (4.0, 4), (4, "4")])
def test_tamano_y_niveles_rechazan_dimensiones_que_no_son_enteros_positivos(dimensiones: tuple[object, object]) -> None:
    with pytest.raises(DimensionesNoExportablesError):
        tamano_de_nivel_bc4(*dimensiones)  # type: ignore[arg-type]
    with pytest.raises(DimensionesNoExportablesError):
        cantidad_de_niveles(*dimensiones)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# construir_dds_l8: la entrada sin comprimir que se le entrega a texconv
# ---------------------------------------------------------------------------


def _cadena_de_uint8(lado_alto: int, lado_ancho: int, semilla: int = 0) -> list[np.ndarray]:
    rng = np.random.default_rng(semilla)
    niveles = []
    for i in range(cantidad_de_niveles(lado_ancho, lado_alto)):
        niveles.append(rng.integers(0, 256, size=(max(1, lado_alto >> i), max(1, lado_ancho >> i)), dtype=np.uint8))
    return niveles


def _leer_cabecera_l8(datos: bytes) -> dict[str, int]:
    """Lectura independiente de la cabecera legacy: no usa nada del módulo bajo prueba."""
    tamano, banderas, alto, ancho, pitch, profundidad, mips = struct.unpack_from("<7I", datos, 4)
    tamano_pf, banderas_pf, fourcc, bits, mascara_r, mascara_g, mascara_b, mascara_a = struct.unpack_from(
        "<II4sIIIII", datos, 76
    )
    capacidades, capacidades_2 = struct.unpack_from("<2I", datos, 108)
    return {
        "tamano": tamano,
        "banderas": banderas,
        "alto": alto,
        "ancho": ancho,
        "pitch": pitch,
        "profundidad": profundidad,
        "mips": mips,
        "tamano_pf": tamano_pf,
        "banderas_pf": banderas_pf,
        "fourcc": int.from_bytes(fourcc, "little"),
        "bits": bits,
        "mascara_r": mascara_r,
        "mascara_g": mascara_g,
        "mascara_b": mascara_b,
        "mascara_a": mascara_a,
        "capacidades": capacidades,
        "capacidades_2": capacidades_2,
    }


def test_el_dds_l8_tiene_la_cabecera_de_luminancia_de_8_bits_y_los_niveles_en_orden() -> None:
    niveles = _cadena_de_uint8(8, 16)

    datos = construir_dds_l8(niveles)

    cab = _leer_cabecera_l8(datos)
    assert datos[:4] == b"DDS "
    assert (cab["tamano"], cab["tamano_pf"]) == (124, 32)
    assert (cab["ancho"], cab["alto"], cab["pitch"], cab["mips"]) == (16, 8, 16, 5)
    # CAPS | HEIGHT | WIDTH | PITCH | PIXELFORMAT | MIPMAPCOUNT
    assert cab["banderas"] == 0x1 | 0x2 | 0x4 | 0x8 | 0x1000 | 0x20000
    # DDPF_LUMINANCE (0x20000), 8 bits, máscara de luminancia 0xFF en el canal R
    assert (cab["banderas_pf"], cab["fourcc"], cab["bits"], cab["mascara_r"]) == (0x20000, 0, 8, 0xFF)
    assert (cab["mascara_g"], cab["mascara_b"], cab["mascara_a"]) == (0, 0, 0)
    # TEXTURE | COMPLEX | MIPMAP, sin cubemap ni volumen
    assert (cab["capacidades"], cab["capacidades_2"]) == (0x1000 | 0x8 | 0x400000, 0)
    assert datos[128:] == b"".join(nivel.tobytes() for nivel in niveles)
    assert len(datos) == 128 + sum(nivel.size for nivel in niveles)


def test_el_dds_l8_de_un_solo_nivel_no_declara_mips() -> None:
    # La única cadena completa de un solo nivel es la de una textura de 1x1.
    nivel = np.array([[7]], dtype=np.uint8)

    datos = construir_dds_l8([nivel])

    cab = _leer_cabecera_l8(datos)
    assert (cab["ancho"], cab["alto"], cab["mips"]) == (1, 1, 0)
    assert cab["banderas"] & 0x20000 == 0
    assert cab["capacidades"] == 0x1000
    assert datos[128:] == nivel.tobytes()


def test_el_dds_l8_de_dos_niveles_declara_los_mips() -> None:
    niveles = [np.arange(2, dtype=np.uint8).reshape(2, 1), np.array([[9]], dtype=np.uint8)]

    cab = _leer_cabecera_l8(construir_dds_l8(niveles))

    assert (cab["ancho"], cab["alto"], cab["mips"]) == (1, 2, 2)
    assert cab["banderas"] & 0x20000 and cab["capacidades"] == 0x1000 | 0x8 | 0x400000


def test_pillow_lee_el_nivel_base_del_dds_l8_igual_que_lo_entregado() -> None:
    niveles = _cadena_de_uint8(8, 8, semilla=4)

    datos = construir_dds_l8(niveles)

    with Image.open(io.BytesIO(datos)) as imagen:
        assert (imagen.mode, imagen.size) == ("L", (8, 8))
        imagen.load()
        assert np.array_equal(np.asarray(imagen), niveles[0])


def test_construir_dds_l8_es_determinista_y_no_modifica_la_entrada() -> None:
    niveles = _cadena_de_uint8(16, 16, semilla=9)
    copia = [nivel.copy() for nivel in niveles]

    assert construir_dds_l8(niveles) == construir_dds_l8(tuple(niveles))
    assert all(np.array_equal(a, b) for a, b in zip(niveles, copia, strict=True))


def test_el_dds_l8_se_arma_a_partir_de_la_cadena_real_de_mips_cuantizados() -> None:
    altura = np.random.default_rng(2).random((16, 8))

    niveles = [cuantizar_a_8_bits(nivel) for nivel in generar_mips(altura)]
    datos = construir_dds_l8(niveles)

    assert _leer_cabecera_l8(datos)["mips"] == cantidad_de_niveles(8, 16) == len(niveles)


@pytest.mark.parametrize(
    "niveles",
    [
        [],
        [np.zeros((4, 4), dtype=np.uint8)] * 2,  # el segundo no es 2x2
        [np.zeros((4, 4), dtype=np.uint8), np.zeros((2, 2), dtype=np.uint8)],  # falta la cola hasta 1x1
        [np.zeros((4, 4), dtype=np.uint8), np.zeros((2, 2), dtype=np.uint8), np.zeros((1, 1), dtype=np.uint8)] * 2,
        [np.zeros((4, 4), dtype=np.uint16), np.zeros((2, 2), dtype=np.uint16), np.zeros((1, 1), dtype=np.uint16)],
        [np.zeros((4, 4), dtype=np.float32), np.zeros((2, 2), dtype=np.float32), np.zeros((1, 1), dtype=np.float32)],
        np.zeros((3, 4, 4), dtype=np.uint8),
        "abc",
        [np.zeros((4, 4, 1), dtype=np.uint8)],
        [np.zeros(16, dtype=np.uint8)],
        [np.zeros((0, 4), dtype=np.uint8)],
        [[[0, 1], [2, 3]]],
        [None],
    ],
    ids=[
        "vacio",
        "segundo-nivel-con-forma-equivocada",
        "cadena-incompleta",
        "cadena-repetida",
        "uint16-con-cadena-completa",
        "float32-con-cadena-completa",
        "un-solo-arreglo-3d",
        "cadena-de-texto",
        "tres-dimensiones",
        "una-dimension",
        "sin-filas",
        "lista-de-listas",
        "none",
    ],
)
def test_construir_dds_l8_rechaza_cadenas_que_no_son_una_cadena_de_mips_uint8_completa(niveles: list) -> None:
    with pytest.raises(ExportError):
        construir_dds_l8(niveles)


# ---------------------------------------------------------------------------
# inspeccionar_dds_bc4: el validador de lo que entregue texconv
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("datos", "dx10"),
    [
        (_dds(16, 8, 5, fourcc=b"BC4U"), False),
        (_dds(16, 8, 5, fourcc=b"ATI1"), False),
        (_dds_dx10(16, 8, 5), True),
    ],
    ids=["BC4U", "ATI1", "DX10-BC4_UNORM"],
)
def test_el_validador_acepta_los_tres_contenedores_bc4_unorm_y_cuenta_niveles_y_payload(
    datos: bytes, dx10: bool
) -> None:
    info = inspeccionar_dds_bc4(datos)

    assert (info.ancho, info.alto, info.niveles) == (16, 8, 5)
    assert info.formato == "BC4_UNORM"
    assert info.con_extension_dx10 is dx10
    esperado = sum(tamano_de_nivel_bc4(max(1, 16 >> i), max(1, 8 >> i)) for i in range(5))
    assert info.bytes_de_payload == esperado
    assert len(datos) == 4 + 124 + (20 if dx10 else 0) + esperado


@pytest.mark.parametrize(
    "datos",
    [_dds(16, 8, 5), _dds(16, 8, 5, fourcc=b"ATI1"), _dds_dx10(16, 8, 5)],
    ids=["BC4U", "ATI1", "DX10"],
)
def test_pillow_lee_el_mismo_nivel_base_que_declara_el_validador(datos: bytes) -> None:
    info = inspeccionar_dds_bc4(datos)

    with Image.open(io.BytesIO(datos)) as imagen:
        assert imagen.size == (info.ancho, info.alto)
        assert imagen.info.get("pixel_format", getattr(imagen, "pixel_format", None)) == "BC4"
        imagen.load()
        assert imagen.getpixel((0, 0)) == 200


def test_sin_bandera_de_mips_el_validador_cuenta_un_solo_nivel() -> None:
    info = inspeccionar_dds_bc4(_dds(16, 8, 1))

    assert info.niveles == 1


def test_un_mipmapcount_en_cero_con_la_bandera_puesta_cuenta_como_un_nivel() -> None:
    datos = _dds(16, 8, 1, con_bandera_de_mips=True, mipmapcount_declarado=0)

    assert inspeccionar_dds_bc4(datos).niveles == 1


@pytest.mark.parametrize(
    "datos",
    [
        b"",
        b"DDS ",
        _dds(16, 8, 5)[:100],
        _dds(16, 8, 5, marca=b"DDX "),
        _dds(16, 8, 5, tamano_cabecera=120),
        _dds(16, 8, 5, tamano_pf=24),
        _dds(16, 8, 5, recorte=1),
        _dds(16, 8, 5, payload_extra=1),
        _dds(16, 8, 5, payload_extra=8),
        _dds(16, 8, 5, mipmapcount_declarado=6),
        _dds(16, 8, 5, mipmapcount_declarado=4),
        _dds(16, 8, 6),
        _dds(16, 8, 7),
        _dds(16, 8, 5, con_bandera_de_mips=False),
        _dds(16, 8, 2, con_bandera_de_mips=False),
        _dds(16, 8, 5, quitar_banderas=_DDSD_WIDTH),
        _dds(16, 8, 5, quitar_banderas=_DDSD_HEIGHT),
        _dds(16, 8, 5, banderas_pf=0),
        _dds(16, 8, 5, agregar_banderas=_DDSD_DEPTH, profundidad=4),
        _dds(16, 8, 5, agregar_banderas=_DDSD_DEPTH),
        _dds(0, 8, 1),
        _dds(16, 0, 1),
        _dds(16, 8, 5, fourcc=b"DXT5"),
        _dds(16, 8, 5, fourcc=b"BC5U"),
        _dds(16, 8, 5, fourcc=b"BC4S"),
        _dds(16, 8, 5, caps2=0x200),
        _dds(16, 8, 5, caps2=0x200000),
        _dds_dx10(16, 8, 5, dxgi=_DXGI_BC4_SNORM),
        _dds_dx10(16, 8, 5, dxgi=_DXGI_BC5_UNORM),
        _dds_dx10(16, 8, 5, array_size=2),
        _dds_dx10(16, 8, 5, dimension=4),
        _dds_dx10(16, 8, 5, misc=0x4),
        _dds_dx10(16, 8, 5)[: 4 + 124 + 5],
        _dds_dx10(16, 8, 5, recorte=10),
    ],
    ids=[
        "vacio",
        "solo-marca",
        "cabecera-truncada",
        "marca-incorrecta",
        "tamano-de-cabecera-incorrecto",
        "tamano-de-pixelformat-incorrecto",
        "payload-corto-por-1-byte",
        "payload-largo-por-1-byte",
        "payload-largo-por-un-bloque",
        "mips-declarados-de-mas",
        "mips-declarados-de-menos",
        "seis-mips-con-payload-de-seis",
        "siete-mips-con-payload-de-siete",
        "payload-de-cinco-mips-sin-bandera",
        "dos-mips-declarados-sin-bandera",
        "sin-bandera-de-ancho",
        "sin-bandera-de-alto",
        "pixelformat-sin-bandera-fourcc",
        "bandera-de-profundidad-con-dwdepth",
        "bandera-de-profundidad-sin-dwdepth",
        "ancho-cero",
        "alto-cero",
        "fourcc-dxt5",
        "fourcc-bc5",
        "fourcc-bc4-con-signo",
        "cubemap",
        "volumen",
        "dx10-bc4-con-signo",
        "dx10-bc5",
        "dx10-array",
        "dx10-no-2d",
        "dx10-cubemap",
        "dx10-extension-truncada",
        "dx10-payload-corto",
    ],
)
def test_el_validador_rechaza_todo_lo_que_no_sea_un_bc4_unorm_2d_consistente(datos: bytes) -> None:
    with pytest.raises(ContenedorDdsInvalidoError):
        inspeccionar_dds_bc4(datos)


@pytest.mark.parametrize(
    "datos",
    [
        "DDS ",
        None,
        # un memoryview de elementos de 4 bytes contaría ``len`` en elementos, no en bytes: se rechaza en vez de leerlo mal
        memoryview(bytearray(_dds(16, 8, 5))).cast("I", shape=[len(_dds(16, 8, 5)) // 4]),
        memoryview(_dds(16, 8, 5)),
    ],
    ids=["str", "none", "memoryview-de-uint32", "memoryview-de-bytes"],
)
def test_el_validador_solo_acepta_bytes_o_bytearray(datos: object) -> None:
    with pytest.raises(ContenedorDdsInvalidoError):
        inspeccionar_dds_bc4(datos)  # type: ignore[arg-type]


def test_el_validador_acepta_un_bytearray_igual_que_un_bytes() -> None:
    datos = _dds(16, 8, 5)

    assert inspeccionar_dds_bc4(bytearray(datos)) == inspeccionar_dds_bc4(datos)


# Primeros 148 bytes de la salida REAL de ``texconv`` ``may2026`` (informe de la PC con Windows, 2026-10-21): entrada DDS L8
# de 64x64 con 7 mips, ``-f BC4_UNORM -m 0 -y``; salida de 2872 bytes = 128 de cabecera + 2744 de carga. Es la única
# cabecera real de la que hay constancia; los 20 bytes finales son el comienzo de la carga del primer nivel.
_SALIDA_REAL_DE_TEXCONV_MAY2026 = bytes.fromhex(
    "444453207c00000007100a004000000040000000000800000100000007000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000200000000400000042433455000000000000000000000000"
    "00000000000000000810400000000000000000000000000000000000ee07c4217fb659a5f421b62ca498e6e6e9088c88"
)


def test_la_constante_de_la_salida_real_tiene_el_largo_y_la_marca_esperados() -> None:
    assert len(_SALIDA_REAL_DE_TEXCONV_MAY2026) == 148
    assert _SALIDA_REAL_DE_TEXCONV_MAY2026[:4] == b"DDS "


def test_el_validador_acepta_la_cabecera_real_que_escribe_texconv_may2026() -> None:
    cabecera = _SALIDA_REAL_DE_TEXCONV_MAY2026[:128]
    datos = cabecera + _BLOQUE_BC4 * (2744 // 8)

    info = inspeccionar_dds_bc4(datos)

    assert (info.ancho, info.alto, info.niveles) == (64, 64, 7)
    assert info.formato == "BC4_UNORM" and info.con_extension_dx10 is False
    assert len(datos) == 2872


def test_texconv_escribe_dwdepth_igual_a_1_sin_la_bandera_de_profundidad_en_una_2d() -> None:
    # Por eso el validador rechaza la BANDERA DDSD_DEPTH y no el valor de dwDepth: mirar dwDepth habría descartado la
    # salida válida del binario real. Este test congela la observación (no es una regla de negocio nuestra).
    cabecera = _SALIDA_REAL_DE_TEXCONV_MAY2026[:128]
    banderas, profundidad = struct.unpack_from("<I", cabecera, 8)[0], struct.unpack_from("<I", cabecera, 24)[0]

    assert profundidad == 1
    assert banderas & _DDSD_DEPTH == 0


def test_la_superficie_publica_del_modulo_esta_congelada() -> None:
    esperado = [
        "AlturaNoExportableError",
        "ContenedorDdsInvalidoError",
        "DimensionesNoExportablesError",
        "ExportError",
        "InfoDds",
        "PinInvalidoError",
        "TAMANO_DE_BLOQUE_DE_LECTURA",
        "TEXCONV_PIN_VIGENTE",
        "TexconvNoCoincideError",
        "TexconvNoFijadoError",
        "TexconvPin",
        "cantidad_de_niveles",
        "construir_dds_l8",
        "cuantizar_a_8_bits",
        "generar_mips",
        "inspeccionar_dds_bc4",
        "tamano_de_nivel_bc4",
        "verificar_texconv",
    ]
    assert sorted(export.__all__) == esperado

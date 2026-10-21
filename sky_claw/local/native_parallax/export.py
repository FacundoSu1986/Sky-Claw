"""Núcleo puro del exportador DDS del generador nativo de parallax (NP-E0a, ADR 0014 §2.4–§2.5).

El exportador escribe el mapa de altura como ``*_p.dds`` BC4 con mips. Este módulo es la parte que NO necesita el
``texconv`` real y que se puede probar entera sin él; la ejecución de ``texconv`` y sus flags son NP-E0b:

- **Candado de versión** (``TexconvPin``, ``TEXCONV_PIN_VIGENTE``, ``verificar_texconv``): ``texconv`` es un
  binario externo (``DYNAMIC_EXTERNAL_TOOL``) y su salida decide qué bytes llegan al juego. Sin una versión y una
  huella SHA-256 fijadas, el exportador no corre nada (``TEXCONV_PIN_VIGENTE is None``, congelado por igualdad en
  ``tests/test_native_parallax_export.py``): fijarlo es una decisión del operador con evidencia del binario
  oficial. El origen del pin solo puede ser la sección Releases de ``microsoft/DirectXTex``. ``verificar_texconv``
  compara la huella del archivo, NO protege contra que alguien lo reemplace después: quien lo ejecute debe hacerlo
  desde una ubicación que terceros no puedan escribir.
- **Mips en float** (``generar_mips``): promedio de área exacto, 2x2 por nivel, en float64 y redondeado a float32 por
  nivel. Para un campo escalar continuo como la altura el promedio de área es el filtro correcto; POINT o un
  filtro con curva sRGB destruyen el relieve (hipótesis a medir en EXP-010, ver ADR 0014). Las dimensiones deben ser
  potencia de 2 (el promedio es exacto solo así) y al menos 4 (un bloque BC).
- **Cuantización** (``cuantizar_a_8_bits``): ``rint(x · 255)``, con empate al par. BC4 guarda extremos de 8 bits,
  así que 8 bits de entrada no pierden precisión útil.
- **Validador de contenedor** (``inspeccionar_dds_bc4``): comprueba que lo que devolvió ``texconv`` sea un DDS BC4
  UNORM 2D consistente (cabecera, formato, cantidad de niveles y tamaño exacto del payload) con la disposición
  pública de Microsoft. Acepta ``BC4U``, ``ATI1`` y la extensión DX10 con ``DXGI_FORMAT_BC4_UNORM``; rechaza BC4
  con signo, cubemaps, volúmenes, arreglos y cualquier otro formato.

Límites declarados (qué NO está verificado):

- Ningún flag de ``texconv`` está fijado ni probado acá (NP-E0b, contra el binario real en Windows).
- El validador cuenta los niveles con la fórmula de la disposición pública; Pillow (lector independiente en los
  tests) decodifica solo el nivel base.
- Que Skyrim cargue el archivo no está verificado (NP-RIG).

Puro: no ejecuta procesos, no escribe y no importa ``research``. ``numpy`` se importa de forma perezosa por
``_extra.importar_numpy``.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sky_claw.local.native_parallax._extra import importar_numpy

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "TAMANO_DE_BLOQUE_DE_LECTURA",
    "TEXCONV_PIN_VIGENTE",
    "AlturaNoExportableError",
    "ContenedorDdsInvalidoError",
    "DimensionesNoExportablesError",
    "ExportError",
    "InfoDds",
    "PinInvalidoError",
    "TexconvNoCoincideError",
    "TexconvNoFijadoError",
    "TexconvPin",
    "cantidad_de_niveles",
    "cuantizar_a_8_bits",
    "generar_mips",
    "inspeccionar_dds_bc4",
    "tamano_de_nivel_bc4",
    "verificar_texconv",
]

TAMANO_DE_BLOQUE_DE_LECTURA = 1 << 20
_ORIGEN_OFICIAL = "https://github.com/microsoft/DirectXTex/releases/"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LADO_MINIMO = 4  # un bloque BC es de 4x4

# Disposición pública de DDS (Microsoft, "Programming Guide for DDS").
_MARCA = b"DDS "
_TAMANO_DE_CABECERA = 124
_TAMANO_DE_FORMATO_DE_PIXEL = 32
_INICIO_DEL_PAYLOAD = 4 + _TAMANO_DE_CABECERA  # marca + DDS_HEADER
_TAMANO_DE_EXTENSION_DX10 = 20
_DDSD_ALTO_Y_ANCHO = 0x2 | 0x4
_DDSD_MIPMAPCOUNT = 0x20000
_DDPF_FOURCC = 0x4
_DDSCAPS2_CUBEMAP = 0x200
_DDSCAPS2_VOLUMEN = 0x200000
_DXGI_BC4_UNORM = 80
_DIMENSION_TEXTURA_2D = 3
_DX10_MISC_CUBEMAP = 0x4
_FOURCC_BC4_UNORM = frozenset({b"BC4U", b"ATI1"})
_FOURCC_DX10 = b"DX10"
_FORMATO_BC4_UNORM = "BC4_UNORM"
_BYTES_POR_BLOQUE_BC4 = 8


class ExportError(Exception):
    """Base de los errores del exportador DDS."""


class AlturaNoExportableError(ExportError, ValueError):
    """La altura no es un campo float finito en [0, 1]."""


class DimensionesNoExportablesError(ExportError, ValueError):
    """Dimensiones que no son enteros positivos, o no son potencia de 2 con lado mínimo de un bloque BC."""


class ContenedorDdsInvalidoError(ExportError):
    """El DDS no es un BC4 UNORM 2D consistente con la disposición pública."""


class PinInvalidoError(ExportError, ValueError):
    """Versión, huella u origen del pin de ``texconv`` fuera de contrato."""


class TexconvNoFijadoError(ExportError):
    """No hay versión de ``texconv`` fijada: el exportador no ejecuta nada (fail-closed)."""


class TexconvNoCoincideError(ExportError):
    """El ``texconv`` de disco no existe, no se puede leer o su huella no es la del pin."""


@dataclass(frozen=True, slots=True)
class TexconvPin:
    """Versión de ``texconv`` aceptada: tag oficial, huella SHA-256 del binario y de dónde salió.

    ``origen`` solo puede ser una URL de la sección Releases de ``microsoft/DirectXTex``, sin espacios, sin segmentos
    ``..`` y sin ``%`` (no se decodifica: ``%2e%2e`` sería un ``..`` que un cliente sí resolvería).
    """

    version: str
    sha256: str
    origen: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise PinInvalidoError("la versión del pin de texconv no puede estar vacía")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise PinInvalidoError("la huella del pin de texconv debe ser SHA-256 en 64 hex minúsculas")
        if (
            not isinstance(self.origen, str)
            or not self.origen.startswith(_ORIGEN_OFICIAL)
            or any(c.isspace() for c in self.origen)
            or ".." in self.origen.split("/")
            or "%" in self.origen
        ):
            raise PinInvalidoError(f"el origen del pin de texconv debe ser una URL bajo {_ORIGEN_OFICIAL}")


# Sin versión fijada el exportador no ejecuta texconv. Congelado por igualdad en los tests: fijarlo es una
# decisión del operador con evidencia (versión + huella del binario oficial) y cambia el ancla a propósito.
TEXCONV_PIN_VIGENTE: TexconvPin | None = None


def verificar_texconv(ruta: Path | str, *, pin: TexconvPin | None = None) -> None:
    """Exige que el archivo ``ruta`` tenga la huella SHA-256 del pin; falla cerrado en cualquier otro caso.

    Sin ``pin`` usa ``TEXCONV_PIN_VIGENTE``; si tampoco hay, lanza ``TexconvNoFijadoError``.
    """
    efectivo = pin if pin is not None else TEXCONV_PIN_VIGENTE
    if efectivo is None:
        raise TexconvNoFijadoError(
            "no hay una versión de texconv fijada (TEXCONV_PIN_VIGENTE es None): el exportador no ejecuta nada"
        )
    archivo = Path(ruta)
    if not archivo.is_file():
        raise TexconvNoCoincideError(f"texconv no existe o no es un archivo: {archivo}")
    resumen = hashlib.sha256()
    try:
        with archivo.open("rb") as fuente:
            while bloque := fuente.read(TAMANO_DE_BLOQUE_DE_LECTURA):
                resumen.update(bloque)
    except OSError as exc:
        raise TexconvNoCoincideError(f"no se pudo leer texconv: {archivo} ({exc})") from exc
    if resumen.hexdigest() != efectivo.sha256:
        raise TexconvNoCoincideError(
            f"la huella de {archivo.name} no coincide con la del pin {efectivo.version} (sha256 {efectivo.sha256})"
        )


def _entero_positivo(valor: object, nombre: str) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise DimensionesNoExportablesError(f"{nombre} debe ser un entero positivo, no {valor!r}")
    return valor


def cantidad_de_niveles(ancho: int, alto: int) -> int:
    """Niveles de una cadena de mips completa hasta 1x1: ``floor(log2(max(ancho, alto))) + 1``."""
    return max(_entero_positivo(ancho, "ancho"), _entero_positivo(alto, "alto")).bit_length()


def tamano_de_nivel_bc4(ancho: int, alto: int) -> int:
    """Bytes de un nivel BC4: 8 por bloque de 4x4, con piso de un bloque."""
    columnas = (_entero_positivo(ancho, "ancho") + 3) // 4
    filas = (_entero_positivo(alto, "alto") + 3) // 4
    return columnas * filas * _BYTES_POR_BLOQUE_BC4


def _exigir_campo_float(campo: object, nombre: str) -> NDArray[np.floating]:
    """Valida tipo, dtype float, finitud y rango [0, 1]; las dimensiones las valida quien llama."""
    np_ = importar_numpy()
    if not isinstance(campo, np_.ndarray):
        raise AlturaNoExportableError(f"{nombre} debe ser un numpy.ndarray, no {type(campo).__name__}")
    if not np_.issubdtype(campo.dtype, np_.floating):
        raise AlturaNoExportableError(f"{nombre} debe ser float, no {campo.dtype}")
    return cast("NDArray[np.floating]", campo)


def _exigir_valores_en_0_1(campo: NDArray[np.floating], nombre: str) -> None:
    np_ = importar_numpy()
    if campo.size == 0 or not bool(np_.isfinite(campo).all()):
        raise AlturaNoExportableError(f"{nombre} debe ser finita y no vacía")
    if float(campo.min()) < 0.0 or float(campo.max()) > 1.0:
        raise AlturaNoExportableError(f"{nombre} debe estar en [0, 1]")


def generar_mips(altura: NDArray[np.floating]) -> tuple[NDArray[np.float32], ...]:
    """Cadena de mips por promedio de área exacto, del nivel base hasta 1x1 (float32, solo lectura).

    ``altura``: (alto, ancho) float finito en [0, 1], ambos lados potencia de 2 y >= 4. La cadena entera se promedia
    en float64 (cada nivel sale del anterior) y solo la SALIDA de cada nivel se redondea a float32, así que los
    redondeos de float32 no se acumulan entre niveles; cuando un lado llega a 1 solo se reduce el otro. El promedio de
    la textura se conserva en todos los niveles.
    """
    np_ = importar_numpy()
    campo = _exigir_campo_float(altura, "la altura")
    if campo.ndim != 2:
        raise AlturaNoExportableError(f"la altura debe ser 2-D (alto, ancho), no {campo.ndim}-D")
    alto, ancho = campo.shape
    for nombre, lado in (("alto", alto), ("ancho", ancho)):
        if lado < _LADO_MINIMO or lado & (lado - 1) != 0:
            raise DimensionesNoExportablesError(
                f"el {nombre} debe ser potencia de 2 y al menos {_LADO_MINIMO}, no {lado}"
            )
    _exigir_valores_en_0_1(campo, "la altura")

    actual = campo.astype(np_.float64)
    niveles: list[NDArray[np.float32]] = []
    while True:
        nivel = actual.astype(np_.float32)
        nivel.setflags(write=False)
        niveles.append(nivel)
        filas, columnas = actual.shape
        if filas == 1 and columnas == 1:
            break
        if filas > 1:
            actual = (actual[0::2, :] + actual[1::2, :]) / 2.0
        if columnas > 1:
            actual = (actual[:, 0::2] + actual[:, 1::2]) / 2.0
    return tuple(niveles)


def cuantizar_a_8_bits(nivel: NDArray[np.floating]) -> NDArray[np.uint8]:
    """``rint(nivel · 255)`` como uint8 (solo lectura); el empate va al par, igual en toda plataforma."""
    np_ = importar_numpy()
    campo = _exigir_campo_float(nivel, "el nivel")
    _exigir_valores_en_0_1(campo, "el nivel")
    cuantizado = np_.rint(campo.astype(np_.float64) * 255.0).astype(np_.uint8)
    cuantizado.setflags(write=False)
    return cast("NDArray[np.uint8]", cuantizado)


@dataclass(frozen=True, slots=True)
class InfoDds:
    """Lo que ``inspeccionar_dds_bc4`` comprobó de un contenedor BC4 UNORM 2D.

    ``niveles``: cantidad de mips (1 si el conteo es 0 o 1).
    ``con_extension_dx10``: ``True`` si trae la extensión DX10 de 20 bytes.
    ``bytes_de_payload``: bytes de datos tras las cabeceras, igual a la suma exacta de los niveles.
    """

    ancho: int
    alto: int
    niveles: int
    formato: str
    con_extension_dx10: bool
    bytes_de_payload: int


def _leer(vista: memoryview, formato: str, desplazamiento: int) -> tuple[int, ...]:
    return struct.unpack_from(formato, vista, desplazamiento)


def inspeccionar_dds_bc4(datos: bytes | bytearray) -> InfoDds:
    """Valida un contenedor DDS BC4 UNORM 2D y devuelve su descripción; lanza ``ContenedorDdsInvalidoError`` si no.

    Exige marca, tamaños de cabecera, formato BC4 UNORM (``BC4U``, ``ATI1`` o DX10 con ``DXGI_FORMAT_BC4_UNORM``),
    textura 2-D sin cubemap, volumen ni arreglo, conteo de mips coherente con las dimensiones y un payload cuyo
    tamaño sea EXACTAMENTE la suma de sus niveles (ni un byte de más ni de menos).
    """
    if not isinstance(datos, (bytes, bytearray)):
        raise ContenedorDdsInvalidoError(f"se esperaban bytes, no {type(datos).__name__}")
    vista = memoryview(datos)
    if len(vista) < _INICIO_DEL_PAYLOAD:
        raise ContenedorDdsInvalidoError("el DDS es más corto que su cabecera")
    if bytes(vista[:4]) != _MARCA:
        raise ContenedorDdsInvalidoError("falta la marca 'DDS '")
    (tamano_cabecera, banderas, alto, ancho, _lineal, _profundidad, mips_declarados) = _leer(vista, "<7I", 4)
    (tamano_pf, banderas_pf) = _leer(vista, "<2I", 76)
    fourcc = bytes(vista[84:88])
    (_capacidades, capacidades_2) = _leer(vista, "<2I", 108)
    if tamano_cabecera != _TAMANO_DE_CABECERA or tamano_pf != _TAMANO_DE_FORMATO_DE_PIXEL:
        raise ContenedorDdsInvalidoError("los tamaños de la cabecera o del formato de píxel no son los del estándar")
    if banderas & _DDSD_ALTO_Y_ANCHO != _DDSD_ALTO_Y_ANCHO or ancho == 0 or alto == 0:
        raise ContenedorDdsInvalidoError("el DDS no declara un alto y un ancho positivos")
    if capacidades_2 & (_DDSCAPS2_CUBEMAP | _DDSCAPS2_VOLUMEN):
        raise ContenedorDdsInvalidoError("cubemaps y volúmenes no están soportados")
    if banderas_pf & _DDPF_FOURCC == 0:
        raise ContenedorDdsInvalidoError("el formato de píxel no es un FourCC")

    inicio_del_payload = _INICIO_DEL_PAYLOAD
    con_dx10 = fourcc == _FOURCC_DX10
    if con_dx10:
        inicio_del_payload += _TAMANO_DE_EXTENSION_DX10
        if len(vista) < inicio_del_payload:
            raise ContenedorDdsInvalidoError("falta la extensión DX10 completa")
        (formato_dxgi, dimension, banderas_misc, tamano_del_arreglo) = _leer(vista, "<4I", _INICIO_DEL_PAYLOAD)
        if formato_dxgi != _DXGI_BC4_UNORM:
            raise ContenedorDdsInvalidoError(f"DXGI_FORMAT {formato_dxgi} no es BC4_UNORM ({_DXGI_BC4_UNORM})")
        if dimension != _DIMENSION_TEXTURA_2D or tamano_del_arreglo != 1 or banderas_misc & _DX10_MISC_CUBEMAP:
            raise ContenedorDdsInvalidoError("solo se admite una textura 2-D sin cubemap ni arreglo")
    elif fourcc not in _FOURCC_BC4_UNORM:
        raise ContenedorDdsInvalidoError(f"FourCC {fourcc!r} no es BC4 UNORM (BC4U, ATI1 o DX10)")

    if mips_declarados > 1 and not banderas & _DDSD_MIPMAPCOUNT:
        raise ContenedorDdsInvalidoError("declara varios mips sin la bandera de mipmaps: la cabecera es ambigua")
    niveles_posibles = cantidad_de_niveles(ancho, alto)
    niveles = max(1, mips_declarados)
    if niveles > niveles_posibles:
        raise ContenedorDdsInvalidoError(
            f"declara {niveles} mips pero {ancho}x{alto} admite como máximo {niveles_posibles}"
        )
    esperado = sum(tamano_de_nivel_bc4(max(1, ancho >> i), max(1, alto >> i)) for i in range(niveles))
    real = len(vista) - inicio_del_payload
    if real != esperado:
        raise ContenedorDdsInvalidoError(f"el payload tiene {real} bytes y {niveles} niveles BC4 suman {esperado}")
    return InfoDds(
        ancho=ancho,
        alto=alto,
        niveles=niveles,
        formato=_FORMATO_BC4_UNORM,
        con_extension_dx10=con_dx10,
        bytes_de_payload=real,
    )

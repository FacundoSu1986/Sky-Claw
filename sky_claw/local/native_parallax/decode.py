"""``NormalDecoder`` del generador nativo de parallax (NP-C0, ADR 0014 §2.5).

Lee un mapa de normales (PNG o DDS) y devuelve un campo de vectores unitarios en la convención DirectX
más una señal de confianza por píxel. Es PURO: no escribe, no consulta el entorno y no importa
``research`` (ADR 0014, decisión 2: lo probado en research se reimplementa y un test lo compara como
oráculo).

Contenedores y formatos (todo lo demás se RECHAZA con ``FormatoNoSoportadoError``, no se adivina):

- Solo **PNG y DDS**. JPEG, WebP, BMP y cualquier otro contenedor que Pillow sepa leer se rechazan: un
  formato con pérdida o sin verificar alimentaría normales incorrectas al resto del pipeline.
- **Camino RGB** (PNG, DDS sin comprimir —cabecera clásica o DX10—, BC1/DXT1 y BC7): ``v = rgb / 255 * 2 − 1``
  y renormalización a longitud 1. El canal alfa se ignora (en un ``_n.dds`` de Skyrim suele llevar otra
  señal). Solo RGB/RGBA de **8 bits por canal**, comprobado sobre el layout real y no solo sobre el modo
  que Pillow informa: en un PNG el ``rawmode`` del tile (``RGB;16B`` es de 16 bits y Pillow lo abre como
  ``RGB``), y en un DDS sin comprimir las máscaras de canal (565, 1555 y 4444 serían cuantizados en
  silencio, y R16G16 deja un canal en cero). Grises, paleta y 16 bits se rechazan.
- **BC5**: guarda solo X e Y. Pillow 11.3.0 y 12.3.0 lo devuelven como RGB con B = 0 (verificado
  ejecutando con un bloque armado a mano, NP-C0), así que Z se reconstruye como
  ``sqrt(1 − x² − y²)``. Los píxeles con ``x² + y² > 1`` quedan con Z = 0, se renormalizan y se
  cuentan en ``clamped_pixels``.
- **Lista cerrada de DDS de bloques**: ``DXT1``, ``BC5`` y ``BC7``, que son los que tienen prueba. Se rechazan
  BC5 con signo, BC6H (HDR), y DXT3/DXT5: DXT5nm guarda X en el alfa, así que leerlo como RGB daría
  normales incorrectas sin ningún error.

Entrada no confiable: los archivos vienen de mods. Las advertencias de descompresión-bomba de Pillow
(entre ``MAX_IMAGE_PIXELS`` y el doble, que solo advierte) se elevan a error ANTES de reservar los arrays,
y un formato que Pillow no implementa (``NotImplementedError`` al abrir) se traduce a un error tipado. El
filtro de advertencias es global al proceso: no se llama a ``decode`` concurrentemente desde hilos. No se
fija un tope de memoria propio: los arrays de salida ocupan 32 bytes por píxel (``normal`` 24 y
``confidence`` 8), más temporales.

Convención: la salida es DirectX. Una fuente OpenGL se pasa a DirectX invirtiendo Y. Decidir cuál es la
convención REAL de un asset (diagnóstico por signo, EXP-005) es de NP-X0: este módulo no la adivina.

Confianza: ``confidence = clip(nz, 0, 1)`` por píxel. Es una SEÑAL, no una puerta: los umbrales de
aceptación/revisión/rechazo se derivan del benchmark y se versionan (ADR 0014 §6), nunca se fijan acá.

Dependencias: ``numpy`` y ``Pillow`` pertenecen al extra opcional ``native-parallax`` y se importan de
forma perezosa; sin ellas el uso lanza ``DependenciaNoDisponibleError``.

Límite declarado: las pruebas usan fixtures sintéticas. Que Pillow decodifique bien BC5/BC7 REALES de
Skyrim no está verificado (EXP-003). Solo se lee el primer nivel de mip.
"""

from __future__ import annotations

import enum
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "ArchivoNormalInvalidoError",
    "ConvencionNormal",
    "DecodedNormal",
    "DependenciaNoDisponibleError",
    "FormatoNoSoportadoError",
    "NormalDecodeError",
    "NormalDecoder",
]

_MENSAJE_EXTRA = (
    "NormalDecoder necesita numpy y Pillow, que pertenecen al extra opcional: instalá "
    "'sky-claw[native-parallax]' (o pip install --require-hashes -r requirements-native-parallax.lock)."
)

# Evita dividir por cero con un vector de longitud nula. Mismo valor que el oráculo de research; con
# 8 bits por canal ninguna combinación RGB da exactamente cero (255 / 2 no es entero).
_LONGITUD_MINIMA = 1e-12

_MODOS_ADMITIDOS = ("RGB", "RGBA")


class NormalDecodeError(Exception):
    """Base de los errores del decodificador de normales."""


class DependenciaNoDisponibleError(NormalDecodeError):
    """Falta numpy o Pillow: se instalan con el extra ``native-parallax``."""


class FormatoNoSoportadoError(NormalDecodeError):
    """El archivo se leyó pero su formato no está verificado (BC5S, modos no RGB, DDS sin formato)."""


class ArchivoNormalInvalidoError(NormalDecodeError):
    """No existe, no es una imagen legible o excede los límites de tamaño de Pillow."""


class ConvencionNormal(enum.Enum):
    """Convención del eje Y de la fuente: DirectX es la de Skyrim; OpenGL invierte Y."""

    DIRECTX = "directx"
    OPENGL = "opengl"


@dataclass(frozen=True, slots=True)
class DecodedNormal:
    """Resultado inmutable de ``NormalDecoder.decode`` (los arrays son de solo lectura).

    ``normal``: (alto, ancho, 3) float64, vectores unitarios en convención DirectX.
    ``confidence``: (alto, ancho) float64 en [0, 1], ``clip(nz, 0, 1)``. Señal, no puerta.
    ``source_format``: formato del contenedor según Pillow (``PNG``, ``DDS``...).
    ``dds_pixel_format``: formato de píxel del DDS (``BC5``, ``BC7``, ``DXT1``...), ``None`` si no es DDS.
    ``z_reconstructed``: ``True`` si Z se reconstruyó (BC5).
    ``clamped_pixels``: píxeles BC5 con ``x² + y² > 1`` cuyo Z se fijó en 0; 0 fuera de BC5.
    """

    normal: NDArray[np.float64]
    confidence: NDArray[np.float64]
    source_format: str
    dds_pixel_format: str | None
    z_reconstructed: bool
    clamped_pixels: int


def _exigir_dependencias() -> None:
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise DependenciaNoDisponibleError(_MENSAJE_EXTRA) from exc


# Contenedores admitidos. Pillow lee muchos más (JPEG, WebP, BMP...) y los entrega como RGB, pero no están
# verificados para normales (los formatos con pérdida las deforman).
_CONTENEDORES_ADMITIDOS = ("PNG", "DDS")

# Códecs del plugin DDS de Pillow (11.3.0 y 12.3.0, verificado ejecutando): ``dds_rgb`` (DDS sin comprimir
# con máscaras de la cabecera clásica) y ``raw`` (sin comprimir con cabecera DX10), ambos SIN pixel_format;
# ``bcn`` para los de bloques, con pixel_format. Cualquier otro se rechaza: no está verificado.
_CODECS_DDS_SIN_COMPRIMIR = frozenset({"dds_rgb", "raw"})
_CODEC_DDS_BLOQUES = "bcn"
# Formatos de bloques con prueba en tests/test_native_parallax_decode.py (bloques sintéticos armados a mano).
_FORMATOS_DDS_DE_BLOQUES_VERIFICADOS = frozenset({"DXT1", "BC5", "BC7"})


# Layouts de 8 bits por canal que Pillow entrega como RGB/RGBA (verificado ejecutando, Pillow 12.3.0): el
# ``rawmode`` del tile de un PNG y de un DDS DX10 sin comprimir, y las máscaras de un DDS sin comprimir de
# cabecera clásica (el códec ``dds_rgb`` acepta CUALQUIER máscara y reescala cada canal a 8 bits sin avisar).
_RAWMODES_DE_8_BITS = frozenset({"RGB", "RGBA"})
_MASCARAS_DE_8_BITS = frozenset({0xFF, 0xFF00, 0xFF0000, 0xFF000000})
_CODECS_CON_RAWMODE = frozenset({"zip", "raw"})  # ``zip``: PNG; ``raw``: DDS DX10 sin comprimir
_CODEC_DDS_MASCARAS = "dds_rgb"


def _tiles(imagen: object) -> tuple[tuple[str, object], ...]:
    """Pares ``(códec, args)`` de ``imagen.tile``. Se lee ANTES de ``load()``, que vacía la lista.

    Un elemento sin nombre de códec legible cuenta como ``"?"``: así un cambio en Pillow falla cerrado.
    """
    tile = getattr(imagen, "tile", None)
    if not isinstance(tile, list):
        return (("?", None),)
    pares: list[tuple[str, object]] = []
    for elemento in tile:
        nombre = getattr(elemento, "codec_name", None)
        pares.append((nombre if isinstance(nombre, str) else "?", getattr(elemento, "args", None)))
    return tuple(pares)


def _mascaras_de_8_bits(args: object) -> bool:
    """``args`` del códec ``dds_rgb``: ``(bits por píxel, máscaras)`` con un byte entero por canal."""
    if not isinstance(args, tuple) or len(args) != 2:
        return False
    bits, mascaras = args
    if bits not in (24, 32) or not isinstance(mascaras, tuple) or len(mascaras) not in (3, 4):
        return False
    un_byte_por_canal = all(isinstance(m, int) and m in _MASCARAS_DE_8_BITS for m in mascaras)
    return un_byte_por_canal and len(set(mascaras)) == len(mascaras)


def _ocho_bits_por_canal(tiles: tuple[tuple[str, object], ...]) -> bool:
    """``True`` si TODOS los tiles son de 8 bits por canal. Lo que no se reconoce cuenta como que no lo es."""
    if not tiles:
        return False
    for codec, args in tiles:
        if codec in _CODECS_CON_RAWMODE:
            verificado = isinstance(args, str) and args in _RAWMODES_DE_8_BITS
        elif codec == _CODEC_DDS_MASCARAS:
            verificado = _mascaras_de_8_bits(args)
        else:
            # Los DDS de bloques salen de Pillow a 8 bits; qué formato de bloques vale lo decide ``_dds_verificado``.
            verificado = codec == _CODEC_DDS_BLOQUES
        if not verificado:
            return False
    return True


def _formato_de_pixel_dds(imagen: object) -> str | None:
    """``pixel_format`` que el plugin DDS de Pillow deja en las imágenes COMPRIMIDAS (``BC5``, ``BC7``...).

    Es un atributo del plugin y no una API documentada: si falta en un DDS de bloques el llamador FALLA
    cerrado, porque sin saber que es BC5 se leería B = 0 como un vector inclinado. Los DDS sin comprimir
    no lo tienen (verificado ejecutando).
    """
    valor = getattr(imagen, "pixel_format", None)
    return valor if isinstance(valor, str) else None


def _dds_verificado(codecs: frozenset[str], formato_dds: str | None) -> bool:
    """DDS sin comprimir (sin formato de píxel), o de bloques con un formato de la lista verificada."""
    if formato_dds is None:
        return len(codecs) == 1 and codecs <= _CODECS_DDS_SIN_COMPRIMIR
    return codecs == {_CODEC_DDS_BLOQUES} and formato_dds in _FORMATOS_DDS_DE_BLOQUES_VERIFICADOS


def _exigir_formato_verificado(
    nombre: str, formato: str, modo: str, tiles: tuple[tuple[str, object], ...], formato_dds: str | None
) -> None:
    """Rechaza lo no verificado ANTES de decodificar los píxeles (``load()`` es lo caro y lo riesgoso)."""
    if formato not in _CONTENEDORES_ADMITIDOS:
        raise FormatoNoSoportadoError(f"{nombre}: contenedor '{formato}' no soportado (solo PNG y DDS)")
    if modo not in _MODOS_ADMITIDOS:
        raise FormatoNoSoportadoError(f"{nombre}: modo '{modo}' no soportado (se espera RGB o RGBA de 8 bits)")
    if not _ocho_bits_por_canal(tiles):
        raise FormatoNoSoportadoError(
            f"{nombre}: {formato} con canales que no son de 8 bits ({list(tiles)!r}); se espera RGB o RGBA de 8 bits"
        )
    codecs = frozenset(codec for codec, _ in tiles)
    if formato == "DDS" and not _dds_verificado(codecs, formato_dds):
        raise FormatoNoSoportadoError(
            f"{nombre}: DDS con códec {sorted(codecs)} y formato de píxel {formato_dds!r} no verificado"
        )


@dataclass(frozen=True, slots=True)
class NormalDecoder:
    """Decodifica mapas de normales PNG/DDS a vectores unitarios DirectX (ver el docstring del módulo)."""

    source_convention: ConvencionNormal = ConvencionNormal.DIRECTX

    def decode(self, path: Path | str) -> DecodedNormal:
        _exigir_dependencias()
        import numpy as np
        from PIL import Image

        ruta = Path(path)
        try:
            with warnings.catch_warnings():
                # Entre MAX_IMAGE_PIXELS y el doble Pillow solo ADVIERTE y seguiría decodificando: se eleva a error.
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(ruta) as imagen:
                    formato = imagen.format or "DESCONOCIDO"
                    modo = imagen.mode
                    tiles = _tiles(imagen)
                    formato_dds = _formato_de_pixel_dds(imagen) if formato == "DDS" else None
                    _exigir_formato_verificado(ruta.name, formato, modo, tiles, formato_dds)
                    imagen.load()
                    pixeles = np.asarray(imagen)
        # Un DDS con FourCC o formato DXGI que Pillow no implementa lanza NotImplementedError al abrir.
        except NotImplementedError as exc:
            raise FormatoNoSoportadoError(f"{ruta.name}: Pillow no implementa este formato ({exc})") from exc
        # Excepciones de Pillow: OSError (FileNotFoundError, UnidentifiedImageError y los fallos de
        # decodificación, verificado ejecutando), ValueError (documentada en Image.open),
        # DecompressionBombError (NO es un OSError) y la advertencia de bomba elevada a error.
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ArchivoNormalInvalidoError(f"{ruta.name}: no se pudo leer como imagen ({exc})") from exc

        vector = pixeles[..., :3].astype(np.float64) / 255.0 * 2.0 - 1.0
        reconstruye_z = formato_dds == "BC5"
        fijados = 0
        if reconstruye_z:
            # BC5 solo trae X e Y; Pillow deja B = 0. Fuera del disco unidad Z no existe: se fija en 0.
            suma = vector[..., 0] ** 2 + vector[..., 1] ** 2
            fijados = int(np.count_nonzero(suma > 1.0))
            vector[..., 2] = np.sqrt(np.maximum(1.0 - suma, 0.0))
        if self.source_convention is ConvencionNormal.OPENGL:
            vector[..., 1] = -vector[..., 1]

        longitud = np.maximum(np.linalg.norm(vector, axis=-1, keepdims=True), _LONGITUD_MINIMA)
        normal = np.asarray(vector / longitud, dtype=np.float64)
        confianza = np.clip(normal[..., 2], 0.0, 1.0)
        normal.flags.writeable = False
        confianza.flags.writeable = False
        return DecodedNormal(
            normal=normal,
            confidence=confianza,
            source_format=formato,
            dds_pixel_format=formato_dds,
            z_reconstructed=reconstruye_z,
            clamped_pixels=fijados,
        )

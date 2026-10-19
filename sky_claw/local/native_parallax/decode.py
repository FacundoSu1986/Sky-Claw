"""``NormalDecoder`` del generador nativo de parallax (NP-C0, ADR 0014 §2.5).

Lee un mapa de normales (PNG o DDS) y devuelve un campo de vectores unitarios en la convención DirectX
más una señal de confianza por píxel. Es PURO: no escribe, no consulta el entorno y no importa
``research`` (ADR 0014, decisión 2: lo probado en research se reimplementa y un test lo compara como
oráculo).

Formatos:

- **Camino RGB** (PNG, DDS sin comprimir, BC1, BC7 y cualquier formato que Pillow entregue como RGB/RGBA
  de 8 bits): ``v = rgb / 255 * 2 − 1`` y renormalización a longitud 1. El canal alfa se ignora
  (en un ``_n.dds`` de Skyrim suele llevar otra señal).
- **BC5**: guarda solo X e Y. Pillow 11.3.0 y 12.3.0 lo devuelven como RGB con B = 0 (verificado
  ejecutando con un bloque armado a mano, NP-C0), así que Z se reconstruye como
  ``sqrt(1 − x² − y²)``. Los píxeles con ``x² + y² > 1`` quedan con Z = 0, se renormalizan y se
  cuentan en ``clamped_pixels``.
- **BC5 con signo (BC5S)** y los modos que no son RGB/RGBA (grises, paleta, 16 bits) se RECHAZAN con
  un error tipado: no hay evidencia de que decodifiquen bien.

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


# Códecs del plugin DDS de Pillow (11.3.0 y 12.3.0, verificado ejecutando): ``dds_rgb`` para DDS sin
# comprimir y ``bcn`` para los de bloques (BC1-BC7). Cualquier otro se rechaza: no está verificado.
_CODEC_DDS_SIN_COMPRIMIR = "dds_rgb"
_CODEC_DDS_BLOQUES = "bcn"


def _codecs(imagen: object) -> frozenset[str]:
    """Nombres de códec de ``imagen.tile``. Se lee ANTES de ``load()``, que vacía la lista.

    Un elemento sin nombre de códec legible cuenta como ``"?"``: así un cambio en Pillow falla cerrado.
    """
    tile = getattr(imagen, "tile", None)
    if not isinstance(tile, list):
        return frozenset({"?"})
    nombres = (getattr(elemento, "codec_name", None) for elemento in tile)
    return frozenset(nombre if isinstance(nombre, str) else "?" for nombre in nombres)


def _formato_de_pixel_dds(imagen: object) -> str | None:
    """``pixel_format`` que el plugin DDS de Pillow deja en las imágenes COMPRIMIDAS (``BC5``, ``BC7``...).

    Es un atributo del plugin y no una API documentada: si falta en un DDS de bloques el llamador FALLA
    cerrado, porque sin saber que es BC5 se leería B = 0 como un vector inclinado. Los DDS sin comprimir
    no lo tienen (verificado ejecutando).
    """
    valor = getattr(imagen, "pixel_format", None)
    return valor if isinstance(valor, str) else None


def _dds_verificado(codecs: frozenset[str], formato_dds: str | None) -> bool:
    """Un DDS se acepta si es sin comprimir, o de bloques con el formato de píxel informado por Pillow."""
    if codecs == {_CODEC_DDS_SIN_COMPRIMIR}:
        return True
    return codecs == {_CODEC_DDS_BLOQUES} and formato_dds is not None


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
            with Image.open(ruta) as imagen:
                codecs = _codecs(imagen)
                imagen.load()
                formato = imagen.format or "DESCONOCIDO"
                modo = imagen.mode
                formato_dds = _formato_de_pixel_dds(imagen) if formato == "DDS" else None
                if modo in _MODOS_ADMITIDOS:
                    pixeles = np.asarray(imagen)
        # Excepciones de Pillow: OSError (FileNotFoundError, UnidentifiedImageError y los fallos de
        # decodificación, verificado ejecutando), ValueError (documentada en Image.open) y
        # DecompressionBombError, que NO es un OSError.
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ArchivoNormalInvalidoError(f"{ruta.name}: no se pudo leer como imagen ({exc})") from exc

        if modo not in _MODOS_ADMITIDOS:
            raise FormatoNoSoportadoError(f"{ruta.name}: modo '{modo}' no soportado (se espera RGB o RGBA de 8 bits)")
        if formato == "DDS" and not _dds_verificado(codecs, formato_dds):
            raise FormatoNoSoportadoError(
                f"{ruta.name}: DDS con códec {sorted(codecs)} y formato de píxel {formato_dds!r} no verificado"
            )
        if formato_dds == "BC5S":
            raise FormatoNoSoportadoError(f"{ruta.name}: BC5S (BC5 con signo) no está verificado")

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

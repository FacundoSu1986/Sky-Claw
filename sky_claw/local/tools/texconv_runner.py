"""Ejecutor del ``texconv`` fijado para el exportador DDS del generador nativo de parallax (NP-E0b, ADR 0014).

``exportar_altura_bc4`` toma un mapa de altura en float, arma la cadena de mips propia (``generar_mips`` +
``cuantizar_a_8_bits``), se la entrega a ``texconv`` como un DDS de luminancia de 8 bits y devuelve el ``*_p.dds`` BC4
que ``texconv`` produjo, ya validado. NO escribe en ningún destino: devuelve los bytes (``ManagedOutput``, NP-O0, es
el dueño de dónde y cómo se escribe).

Propiedades del mecanismo, cada una con su ancla en ``tests/test_texconv_runner.py``:

- **Solo corre el binario fijado.** ``texconv`` se COPIA a un directorio de trabajo privado (``mkdtemp``, 0700 en
  POSIX) y se verifica la huella SHA-256 de la COPIA contra el pin (``export.TEXCONV_PIN_VIGENTE``); lo que se
  ejecuta es esa copia, no la ruta original. Así se cierra la ventana verificar-y-después-ejecutar frente a terceros
  que escriban en la carpeta original (observación del revisor automático de #771). Sin pin no se ejecuta nada.
- **Solo los flags verificados ejecutando.** ``-f BC4_UNORM -m 0 -y -o <salida> <entrada>`` (``ARGUMENTOS_BC4``):
  los corrió el binario real en Windows (informe del 2026-10-21): código 0, BC4 con la cadena completa de mips,
  idéntico byte a byte en dos corridas. Cualquier flag extra necesita su propia verificación con el binario real.
- **Los mips son los propios.** La entrada es un DDS con la cadena ya hecha y ``-m 0``: el código fuente de
  ``texconv`` (tag ``may2026``) conserva los mips de una entrada DDS en vez de regenerarlos, así que ``texconv`` solo
  comprime. Eso está leído en el código fuente, NO ejecutado: lo verifica ``tests/test_texconv_real.py`` en Windows.
- **Toda salida se valida.** Exactamente un archivo regular ``entrada.dds`` bajo un tope de tamaño calculado de las
  dimensiones, que ``export.inspeccionar_dds_bc4`` acepta como BC4 UNORM 2-D, con las MISMAS dimensiones y la cadena
  completa de mips. Cualquier otra cosa es ``SalidaDeTexconvInvalidaError``.
- **Nunca un proceso huérfano ni un directorio de sobra.** El proceso lo supervisa ``_process.run_capture`` (timeout,
  kill del árbol y reap); el directorio de trabajo se borra en TODA salida, incluida la cancelación.

Límites declarados:

- El directorio privado depende de los permisos del directorio temporal del usuario: en POSIX ``mkdtemp`` lo crea
  0700; en Windows hereda las ACL del padre (el temporal del usuario). No fija ACL propias.
- No es un tool del registro ni tiene superficie GUI/agente todavía (NP-A0): por eso no emite ``success``/``message``.
  Cuando se cablee, el borde traduce con ``normalize_tool_result`` y las dos superficies pasan por esta función.
- ``mypy`` no cubre este módulo (``sky_claw.local.tools.*`` está exento en ``pyproject.toml``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from sky_claw.local.native_parallax.export import (
    ContenedorDdsInvalidoError,
    ExportError,
    InfoDds,
    TexconvNoCoincideError,
    TexconvPin,
    construir_dds_l8,
    cuantizar_a_8_bits,
    generar_mips,
    inspeccionar_dds_bc4,
    tamano_de_nivel_bc4,
    verificar_texconv,
)
from sky_claw.local.tools._process import run_capture

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "ARGUMENTOS_BC4",
    "TIMEOUT_POR_DEFECTO_S",
    "EspacioDeTrabajoError",
    "ResultadoDeExportacion",
    "SalidaDeTexconvInvalidaError",
    "TexconvCodigoDeSalidaError",
    "TexconvNoEjecutadoError",
    "TexconvRunnerError",
    "TexconvTimeoutError",
    "TimeoutInvalidoError",
    "exportar_altura_bc4",
]

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")

#: Flags de ``texconv`` verificados ejecutando el binario real (informe de Windows del 2026-10-21): formato BC4
#: UNORM, cadena completa de mips (con una entrada DDS que ya trae mips se conservan), sobrescribir y directorio de
#: salida. A continuación van el directorio de salida y el archivo de entrada. Congelado por igualdad en los tests.
ARGUMENTOS_BC4: tuple[str, ...] = ("-f", "BC4_UNORM", "-m", "0", "-y", "-o")

#: Plazo por defecto de una ejecución de ``texconv`` (segundos): un BC4 de 4096x4096 tarda una fracción de esto.
TIMEOUT_POR_DEFECTO_S = 120.0

_NOMBRE_DE_ENTRADA = "entrada.dds"
_NOMBRE_DEL_BINARIO = "texconv.exe"
_BYTES_DE_CABECERA_MAXIMOS = 4 + 124 + 20  # marca + DDS_HEADER + extensión DX10 (opcional)
_COLA_DE_SALIDA_CARACTERES = 1500


class TexconvRunnerError(ExportError):
    """Base de los errores del ejecutor de ``texconv``."""


class EspacioDeTrabajoError(TexconvRunnerError):
    """No se pudo crear o preparar el directorio de trabajo privado."""


class TexconvNoEjecutadoError(TexconvRunnerError):
    """El proceso de ``texconv`` no se pudo lanzar (no existe, sin permiso o error del sistema)."""


class TexconvTimeoutError(TexconvRunnerError):
    """``texconv`` excedió el plazo; el proceso se mató y se hizo reap antes de devolver."""


class TexconvCodigoDeSalidaError(TexconvRunnerError):
    """``texconv`` terminó con un código distinto de cero."""


class SalidaDeTexconvInvalidaError(TexconvRunnerError):
    """La salida de ``texconv`` no es exactamente el BC4 esperado (archivos, tamaño, formato, dimensiones o mips)."""


class TimeoutInvalidoError(TexconvRunnerError, ValueError):
    """El plazo no es un número finito y positivo."""


@dataclass(frozen=True, slots=True)
class ResultadoDeExportacion:
    """Lo que produjo una exportación: los bytes del DDS y de dónde salieron (para el manifest de NP-O0).

    ``datos``: el ``*_p.dds`` BC4 completo. ``sha256``: su huella. ``texconv_version`` / ``texconv_sha256``: el pin
    contra el que se verificó el binario que corrió. ``argumentos``: los flags fijos (sin rutas temporales).
    """

    datos: bytes
    info: InfoDds
    sha256: str
    texconv_version: str
    texconv_sha256: str
    argumentos: tuple[str, ...]


def _validar_plazo(timeout_s: object) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TimeoutInvalidoError(f"el plazo debe ser un número, no {timeout_s!r}")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise TimeoutInvalidoError(f"el plazo debe ser finito y positivo, no {timeout_s!r}")
    return float(timeout_s)


async def _en_hilo(funcion: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """``asyncio.to_thread`` que, si lo cancelan, ESPERA a que el hilo termine antes de propagar la cancelación.

    Un hilo no se puede interrumpir. Sin esto, el ``finally`` del llamador borraría el directorio de trabajo mientras
    el hilo todavía copia o lee dentro de él (en Windows un archivo abierto ni siquiera se deja borrar) y la
    cancelación terminaría dejando restos. Una segunda cancelación durante la espera sí se propaga de inmediato.
    """
    trabajo = asyncio.ensure_future(asyncio.to_thread(funcion, *args, **kwargs))
    try:
        return await asyncio.shield(trabajo)
    except asyncio.CancelledError:
        await asyncio.wait([trabajo])  # ``shield`` ya marca como recogida una excepción tardía del hilo
        raise


def _preparar_entrada(altura: NDArray[np.floating]) -> tuple[bytes, int, int, int]:
    """Cadena de mips cuantizada como DDS L8 y las dimensiones esperadas: (bytes, ancho, alto, niveles)."""
    niveles = generar_mips(altura)  # valida el dominio (float finito en [0, 1], potencia de 2, >= 4)
    cuantizados = [cuantizar_a_8_bits(nivel) for nivel in niveles]
    alto, ancho = niveles[0].shape
    return construir_dds_l8(cuantizados), ancho, alto, len(niveles)


def _crear_espacio(base: Path | str | None) -> Path:
    try:
        creado = tempfile.mkdtemp(prefix="skyclaw-texconv-", dir=None if base is None else os.fspath(base))
    except OSError as exc:
        raise EspacioDeTrabajoError(f"no se pudo crear el directorio de trabajo de texconv: {exc}") from exc
    return Path(os.path.abspath(creado))  # absoluta: nunca una ruta que ``texconv`` pueda leer como flag


def _borrar_espacio(espacio: Path) -> None:
    shutil.rmtree(espacio, ignore_errors=True)
    if espacio.exists():
        logger.warning("no se pudo borrar por completo el directorio de trabajo de texconv: %s", espacio)


def _copiar_y_verificar(
    origen: Path, espacio: Path, entrada_dds: bytes, pin: TexconvPin | None
) -> tuple[Path, TexconvPin]:
    """Copia el binario al espacio privado, verifica la COPIA contra el pin y escribe la entrada."""
    carpeta_bin = espacio / "bin"
    try:
        carpeta_bin.mkdir()
        (espacio / "salida").mkdir()
    except OSError as exc:
        raise EspacioDeTrabajoError(f"no se pudo preparar el directorio de trabajo: {exc}") from exc
    copia = carpeta_bin / _NOMBRE_DEL_BINARIO
    try:
        shutil.copy2(origen, copia)
    except OSError as exc:
        raise TexconvNoCoincideError(f"no se pudo copiar texconv desde {origen}: {exc}") from exc
    usado = verificar_texconv(copia, pin=pin)
    try:
        (espacio / _NOMBRE_DE_ENTRADA).write_bytes(entrada_dds)
    except OSError as exc:
        raise EspacioDeTrabajoError(f"no se pudo escribir la entrada de texconv: {exc}") from exc
    return copia, usado


def _construir_argumentos(binario: Path, entrada: Path, salida: Path) -> list[str]:
    return [str(binario), *ARGUMENTOS_BC4, str(salida), str(entrada)]


def _cola(stdout: bytes, stderr: bytes) -> str:
    texto = (stdout + b"\n" + stderr).decode("utf-8", errors="replace").strip()
    return texto[-_COLA_DE_SALIDA_CARACTERES:]


def _leer_acotado(ruta: Path, tope: int) -> bytes:
    with ruta.open("rb") as fuente:
        datos = fuente.read(tope + 1)
    if len(datos) > tope:
        raise SalidaDeTexconvInvalidaError(f"la salida de texconv supera el tope de {tope} bytes")
    return datos


def _leer_salida(carpeta: Path, ancho: int, alto: int, niveles: int) -> tuple[bytes, InfoDds]:
    """Lee y valida lo que dejó ``texconv``: un solo archivo regular, acotado, BC4 con las dimensiones esperadas."""
    tope = _BYTES_DE_CABECERA_MAXIMOS + sum(
        tamano_de_nivel_bc4(max(1, ancho >> i), max(1, alto >> i)) for i in range(niveles)
    )
    try:
        nombres = sorted(entrada.name for entrada in os.scandir(carpeta))
    except OSError as exc:
        raise SalidaDeTexconvInvalidaError(f"no se pudo listar la salida de texconv: {exc}") from exc
    if nombres != [_NOMBRE_DE_ENTRADA]:
        raise SalidaDeTexconvInvalidaError(f"texconv debía dejar solo {_NOMBRE_DE_ENTRADA} y dejó {nombres}")
    ruta = carpeta / _NOMBRE_DE_ENTRADA
    try:
        estado = os.lstat(ruta)
        if not stat.S_ISREG(estado.st_mode):
            raise SalidaDeTexconvInvalidaError("la salida de texconv no es un archivo regular")
        if estado.st_size > tope:
            raise SalidaDeTexconvInvalidaError(f"la salida de texconv pesa {estado.st_size} bytes y el tope es {tope}")
        datos = _leer_acotado(ruta, tope)
    except OSError as exc:
        raise SalidaDeTexconvInvalidaError(f"no se pudo leer la salida de texconv: {exc}") from exc
    try:
        info = inspeccionar_dds_bc4(datos)
    except ContenedorDdsInvalidoError as exc:
        raise SalidaDeTexconvInvalidaError(f"la salida de texconv no es un BC4 UNORM válido: {exc}") from exc
    if (info.ancho, info.alto, info.niveles) != (ancho, alto, niveles):
        raise SalidaDeTexconvInvalidaError(
            f"se esperaba {ancho}x{alto} con {niveles} niveles y texconv entregó "
            f"{info.ancho}x{info.alto} con {info.niveles}"
        )
    return datos, info


async def exportar_altura_bc4(
    altura: NDArray[np.floating],
    *,
    texconv: Path | str,
    timeout_s: float = TIMEOUT_POR_DEFECTO_S,
    pin: TexconvPin | None = None,
    directorio_temporal: Path | str | None = None,
) -> ResultadoDeExportacion:
    """Exporta ``altura`` ((alto, ancho) float en [0, 1], potencia de 2 >= 4) como BC4 con mips usando el ``texconv`` fijado.

    ``texconv``: ruta del ``texconv.exe`` instalado; se copia y se verifica contra ``pin`` (por defecto
    ``export.TEXCONV_PIN_VIGENTE``). ``timeout_s``: plazo de la ejecución. ``directorio_temporal``: dónde crear el
    espacio privado (por defecto el temporal del sistema). Falla cerrado: ver los errores de ``__all__``.
    """
    plazo = _validar_plazo(timeout_s)
    entrada_dds, ancho, alto, niveles = await asyncio.to_thread(_preparar_entrada, altura)
    # Síncrono a propósito (un ``mkdir``): no hay punto de cancelación entre crear el directorio y protegerlo con el
    # ``try``; en un hilo, cancelar a mitad de la espera lo dejaría creado y sin dueño.
    espacio = _crear_espacio(directorio_temporal)
    try:
        binario, pin_usado = await _en_hilo(_copiar_y_verificar, Path(texconv), espacio, entrada_dds, pin)
        argv = _construir_argumentos(binario, espacio / _NOMBRE_DE_ENTRADA, espacio / "salida")
        try:
            stdout, stderr, codigo = await run_capture(argv, timeout=plazo, cwd=str(espacio))
        except TimeoutError as exc:
            raise TexconvTimeoutError(f"texconv excedió el plazo de {plazo} s") from exc
        except OSError as exc:  # incluye FileNotFoundError y PermissionError
            raise TexconvNoEjecutadoError(f"no se pudo ejecutar texconv: {exc}") from exc
        if codigo != 0:
            raise TexconvCodigoDeSalidaError(f"texconv terminó con código {codigo}: {_cola(stdout, stderr)}")
        datos, info = await _en_hilo(_leer_salida, espacio / "salida", ancho, alto, niveles)
    finally:
        _borrar_espacio(espacio)
    logger.info(
        "texconv %s exportó %dx%d con %d niveles (%d bytes)", pin_usado.version, ancho, alto, niveles, len(datos)
    )
    return ResultadoDeExportacion(
        datos=datos,
        info=info,
        sha256=hashlib.sha256(datos).hexdigest(),
        texconv_version=pin_usado.version,
        texconv_sha256=pin_usado.sha256,
        argumentos=ARGUMENTOS_BC4[:-1],
    )

"""Línea de comandos del lote de carpeta plana del generador nativo de parallax (NP-A0a, ADR 0014 §2.5).

Uso (desde la raíz del repo, con el entorno del extra instalado)::

    python -m sky_claw.local.native_parallax.cli autoprueba --texconv C:\\ruta\\texconv.exe
    python -m sky_claw.local.native_parallax.cli generar --carpeta D:\\entrada --mods D:\\MO2\\mods --texconv C:\\ruta\\texconv.exe
    python -m sky_claw.local.native_parallax.cli perfil --mod "Mi mod de texturas" --mods D:\\MO2\\mods --data-root D:\\MO2
    python -m sky_claw.local.native_parallax.cli elegibilidad --mod "Mi mod de texturas" --mods D:\\MO2\\mods --data-root D:\\MO2

- ``autoprueba`` corre la tubería COMPLETA sobre una textura sintética en un mod temporal: lee, integra, mide, exporta con
  el ``texconv`` real y escribe con el escritor sobre el disco de verdad. No toca nada tuyo. Es lo primero que hay que
  correr en una máquina nueva (en Windows ejerce, por primera vez fuera de la CI, el ``texconv`` y el escritor sobre NTFS).
- ``generar`` procesa los ``*_n.dds`` bajo ``<carpeta>/textures`` y deja los ``*_p.dds`` en el mod de salida
  ``<mods>/<mod de salida gestionado>`` (el nombre lo fija ``output.NOMBRE_DEL_MOD_DE_SALIDA``). La carpeta de entrada solo se LEE.
- ``perfil`` (NP-A0e) es un diagnóstico de SOLO lectura: con el ``modlist.txt`` de un perfil de MO2 dice, por cada textura de un
  mod, quién gana la ruta de entrada y quién la de salida (``mo2/ganadores.py``). Avisa cuando otro mod, ``overwrite`` o un mod de
  control (#792 H12) tapa lo que el generador mediría o publicaría. No genera nada y no necesita ``texconv``.
- ``elegibilidad`` (NP-A0j, #816) es otro diagnóstico de SOLO lectura: por cada textura de un mod dice si alguna malla suelta del perfil
  haría que el plugin de relieve automático use el ``*_p.dds`` que el generador publicaría (``SI``), si seguro no (``NO``) o si no se
  puede afirmar (``NO_VERIFICADO``), y por qué. No genera nada y no necesita ``texconv``.

Es un borde delgado sobre ``lote.procesar_lote``: parsea, valida, arma el exportador y muestra el informe. La carpeta de
entrada la enumera ``provider.py`` (NP-A0b): no sigue enlaces, ignora lo que no es ``*_n.dds`` y omite (y dice) lo que ya
tiene un ``*_p.dds`` autoral con la misma ruta de salida. Acá solo se traducen sus errores y sus omisiones a mensajes.

Códigos de salida: 0 todo bien; 1 alguna textura falló; 2 uso o configuración incorrectos; 3 otra instancia está
escribiendo esa salida; 4 (solo ``perfil``) hay texturas tapadas o la salida no está habilitada; 130 interrumpido. ``elegibilidad`` sale
en 0 cuando el informe se produjo (sea cual sea su contenido) y en 2 si no se pudo afirmar nada del perfil.

No hay gate validado todavía (``gates.py`` es fail-closed): TODA salida queda en revisión y solo se escribe con
``--incluir-revision``. Los parámetros por defecto (DirectX, escalas 1, bordes periódicos, min-max) son los de la referencia
sintética de NP-D0, NO una calibración para Skyrim: para eso están los informes de esta herramienta.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import io
import json
import os
import pathlib
import struct
import sys
import tempfile
import zlib
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, TextIO, cast

from sky_claw.app.security.links import rmtree_link_aware
from sky_claw.local.native_parallax._extra import importar_numpy
from sky_claw.local.native_parallax.decode import ConvencionNormal, DependenciaNoDisponibleError
from sky_claw.local.native_parallax.diagnostico_perfil import (
    MAXIMO_DE_ARCHIVOS_DE_ENTRADA,
    SUFIJO_DE_ENTRADA,
    TIPOS_QUE_IMPIDEN_MEDIR,
    Hallazgo,
    PerfilError,
    cargar_perfil,
    descubrir_normales_del_mod,
    diagnosticar_perfil,
    hallazgos_del_perfil,
    linea_de_hallazgo,
    texto_de_omision,
)
from sky_claw.local.native_parallax.diagnostico_texconv import (
    ENTORNO_TEXCONV,
    EstadoDeTexconv,
    diagnosticar_texconv,
    ruta_configurada,
)
from sky_claw.local.native_parallax.elegibilidad import EfectoPosible, evaluar_elegibilidad
from sky_claw.local.native_parallax.export import (
    ExportError,
    inspeccionar_dds_bc4,
)
from sky_claw.local.native_parallax.integrate import Bordes, ModoNormalizacion
from sky_claw.local.native_parallax.lote import (
    MOTIVO_SIN_CONTENIDO,
    EstadoDeArchivo,
    ExportadorDeAltura,
    InformeDelLote,
    LoteError,
    ParametrosDelLote,
    ResultadoDeArchivo,
    ResultadoDelExportador,
    procesar_lote,
)
from sky_claw.local.native_parallax.output import NOMBRE_DEL_MOD_DE_SALIDA, leer_manifiesto
from sky_claw.local.native_parallax.output_writer import (
    CoordinacionDeSalida,
    SalidaNoEscribibleError,
    SalidaOcupadaError,
    construir_coordinacion_de_salida,
)
from sky_claw.local.native_parallax.provider import (
    NormalDescubierta,
    RaizInvalidaError,
    carpeta_de_textures,
)

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "CODIGO_CON_FALLOS",
    "CODIGO_DE_USO",
    "CODIGO_INTERRUMPIDO",
    "CODIGO_OK",
    "CODIGO_CON_TAPADOS",
    "CODIGO_SALIDA_OCUPADA",
    "ENTORNO_TEXCONV",
    "FabricaDeExportador",
    "NormalDeCarpeta",
    "construir_parser",
    "crear_exportador_texconv",
    "descubrir_normales_de_carpeta",
    "main",
]

CODIGO_OK: Final = 0
CODIGO_CON_FALLOS: Final = 1
CODIGO_DE_USO: Final = 2
CODIGO_SALIDA_OCUPADA: Final = 3
CODIGO_CON_TAPADOS: Final = 4
CODIGO_INTERRUMPIDO: Final = 130

INFORME_POR_DEFECTO: Final = "informe_parallax.json"
_MAXIMO_DE_ARCHIVOS_DE_ENTRADA: Final = MAXIMO_DE_ARCHIVOS_DE_ENTRADA
_SUFIJO_DE_ENTRADA: Final = SUFIJO_DE_ENTRADA
_LADO_DE_LA_AUTOPRUEBA: Final = 128

FabricaDeExportador = Callable[[pathlib.Path], ExportadorDeAltura]


class UsoIncorrectoError(Exception):
    """Algo que el operador pasó mal o que falta en su máquina: se muestra su mensaje, sin traceback."""


# ---------------------------------------------------------------------------
# La carpeta de entrada: la enumera ``provider.py``
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class NormalDeCarpeta:
    """Una normal que NO viene del proveedor: la textura sintética de la autoprueba (un PNG que el proveedor no descubriría)."""

    ruta_de_origen: str
    ruta_de_salida: str
    ruta_absoluta: pathlib.Path


@contextlib.contextmanager
def _perfil_error_como_uso() -> Iterator[None]:
    """``PerfilError`` es el error de la pieza compartida; en la línea de comandos se muestra como error de uso, sin traceback."""
    try:
        yield
    except PerfilError as exc:
        raise UsoIncorrectoError(str(exc)) from exc


def descubrir_normales_de_carpeta(
    carpeta: pathlib.Path, *, exigir_completa: bool = False
) -> tuple[list[NormalDescubierta], list[str]]:
    """Los ``*_n.dds`` bajo ``<carpeta>/textures`` y los motivos por los que se omitió algo. Solo lee.

    Adaptador delgado de ``diagnostico_perfil.descubrir_normales_del_mod`` (que a su vez envuelve a ``provider.descubrir_normales``):
    traduce sus errores a ``UsoIncorrectoError`` (se muestran sin traceback). La CLI pone su propio tope de entradas (más bajo que el
    del proveedor: es una herramienta experimental para probar en un rig).

    ``exigir_completa``: una carpeta que no se pudo leer o una entrada que no se pudo inspeccionar levanta
    ``UsoIncorrectoError`` en vez de omitirse. Lo pide quien actúa sobre lo que NO figura (retirar salidas): sin ver una
    carpeta no se sabe qué hay en ella.
    """
    with _perfil_error_como_uso():
        return descubrir_normales_del_mod(
            carpeta, exigir_completa=exigir_completa, maximo_de_archivos=_MAXIMO_DE_ARCHIVOS_DE_ENTRADA
        )


# ---------------------------------------------------------------------------
# Exportador real y autoprueba
# ---------------------------------------------------------------------------


def crear_exportador_texconv(texconv: pathlib.Path) -> ExportadorDeAltura:
    """El exportador real: diagnostica el binario contra el pin ANTES de empezar y lo ejecuta con el ejecutor fijado.

    Falla cerrado y temprano, y con la frase del diagnóstico (qué le pasa al binario y qué hacer) en vez del error seco
    de la verificación. Recibe la ruta ya resuelta: el entorno va vacío para que la variable se lea UNA vez, en
    ``_resolver_texconv``.
    """
    diagnostico = diagnosticar_texconv(texconv, entorno={})
    if diagnostico.estado is not EstadoDeTexconv.CORRECTO:
        raise UsoIncorrectoError(diagnostico.mensaje)
    from sky_claw.local.tools.texconv_runner import exportar_altura_bc4

    async def exportar(altura: NDArray[np.floating[Any]]) -> ResultadoDelExportador:
        return await exportar_altura_bc4(altura, texconv=texconv)

    return exportar


def _png_rgb(rgb: bytes, ancho: int, alto: int) -> bytes:
    """PNG RGB de 8 bits sin dependencias (la autoprueba no debe necesitar Pillow para fabricar su entrada)."""

    def trozo(etiqueta: bytes, datos: bytes) -> bytes:
        cuerpo = etiqueta + datos
        return struct.pack(">I", len(datos)) + cuerpo + struct.pack(">I", zlib.crc32(cuerpo) & 0xFFFFFFFF)

    filas = b"".join(b"\x00" + rgb[f * ancho * 3 : (f + 1) * ancho * 3] for f in range(alto))
    return (
        b"\x89PNG\r\n\x1a\n"
        + trozo(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0))
        + trozo(b"IDAT", zlib.compress(filas))
        + trozo(b"IEND", b"")
    )


def _escribir_normal_sintetica(ruta: pathlib.Path, lado: int) -> None:
    """Normal DirectX de una superficie periódica suave (la misma familia que los tests), codificada en 8 bits."""
    np = importar_numpy()
    y, x = np.mgrid[0:lado, 0:lado] / lado
    pendiente_x = 0.5 * np.cos(2 * np.pi * x) * np.cos(2 * np.pi * y)
    pendiente_y = -0.5 * np.sin(2 * np.pi * x) * np.sin(2 * np.pi * y)
    vector = np.stack((-pendiente_x, -pendiente_y, np.ones_like(x)), axis=-1)
    vector /= np.linalg.norm(vector, axis=-1, keepdims=True)
    rgb = np.round((vector * 0.5 + 0.5) * 255).astype(np.uint8)
    ruta.write_bytes(_png_rgb(rgb.tobytes(), lado, lado))


# ---------------------------------------------------------------------------
# Presentación
# ---------------------------------------------------------------------------


def _linea_de_archivo(a: ResultadoDeArchivo) -> str:
    efecto = f" → {a.efecto.name}" if a.efecto is not None else ""
    linea = f"  [{a.estado.name}{efecto}] {a.ruta_de_salida}"
    if a.metricas is not None:
        m = a.metricas
        linea += (
            f"\n      acuerdo p95 {m.acuerdo_p95_grados:.2f}°, signo {m.signo:+d}, "
            f"residuo {m.residuo_de_integrabilidad:.4f}, costura {m.razon_de_costura:.3f}"
        )
    if a.veredicto is not None:
        linea += f"\n      gate: {a.veredicto.name}" + (f" ({', '.join(a.motivos)})" if a.motivos else "")
    if a.detalle and a.estado is not EstadoDeArchivo.EN_REVISION:
        linea += f"\n      {a.detalle}"
    return linea


# Lo que lanza una consola que ya no acepta texto: cerrada (``ValueError``) o rota (``OSError``: tubería cortada,
# ventana cerrada). ``UnicodeEncodeError`` también es un ``ValueError``, pero ese se escapa y se vuelve a intentar.
_ERRORES_DE_LA_CONSOLA = (OSError, ValueError)


class _SalidaSinCodepage(io.TextIOBase):
    """Escribe en ``flujo`` sin que la consola pueda abortar la corrida.

    Dos propiedades (rig 2026-10-24: «→» en cp1252 tiraba ``UnicodeEncodeError`` después de escribir la salida y antes
    del informe JSON, y la corrida quedaba sin evidencia):

    - **No depende de la codificación:** lo que el flujo no representa se escapa visible (``\\u2192``) en vez de reventar.
    - **Imprimir nunca aborta:** si la consola deja de aceptar texto, lo que no se pudo mostrar se descarta. El informe
      JSON y el código de salida no pasan por acá. Un error que no es de E/S (un bug) sigue subiendo.

    No se reconfigura el ``sys.stdout`` del proceso: solo cambia lo que ESTA herramienta escribe. Es un flujo de texto
    de solo escritura: lo demás de ``TextIOBase`` (``read``, ``fileno``...) lanza ``io.UnsupportedOperation``.
    """

    def __init__(self, flujo: TextIO) -> None:
        super().__init__()
        self._flujo = flujo

    def writable(self) -> bool:
        return True

    def write(self, texto: str) -> int:
        # Si la consola ya no muestra nada, se descarta: la corrida y su informe siguen.
        with contextlib.suppress(*_ERRORES_DE_LA_CONSOLA):
            try:
                self._flujo.write(texto)
            except UnicodeEncodeError:
                codificacion = getattr(self._flujo, "encoding", None) or "utf-8"
                self._flujo.write(texto.encode(codificacion, errors="backslashreplace").decode(codificacion))
        return len(texto)

    def flush(self) -> None:
        with contextlib.suppress(*_ERRORES_DE_LA_CONSOLA):
            self._flujo.flush()


def _sin_codepage(flujo: TextIO) -> TextIO:
    # ``TextIOBase`` es un flujo de texto completo, pero para mypy no es ``typing.TextIO``.
    return cast("TextIO", _SalidaSinCodepage(flujo))


def _resumen(informe: InformeDelLote, omitidas: Sequence[str], salida: TextIO) -> None:
    print("", file=salida)
    for archivo in informe.archivos:
        print(_linea_de_archivo(archivo), file=salida)
    for otra in informe.otras_rutas:
        print(f"  [{otra.efecto.name}] {otra.ruta} (corrida anterior) {otra.detalle}".rstrip(), file=salida)
    for motivo in omitidas:
        print(f"  [OMITIDA] {motivo}", file=salida)
    conteos = informe.contar()
    print(
        "\nResumen: "
        + ", ".join(f"{conteos[e]} {e.name.lower()}" for e in EstadoDeArchivo)
        + f", {len(omitidas)} omitidas al enumerar.",
        file=salida,
    )
    if informe.interrupcion is not None:
        print(
            f"ESCRITURA INTERRUMPIDA: {informe.interrupcion}\n"
            "Lo marcado SIN_CONFIRMAR puede o no estar en el mod, y lo que se retiraba pudo quedar a medias: volvé a correr el lote "
            "(al abrir la salida se recupera lo que haya quedado a medias).",
            file=salida,
        )
    sin_contenido = sum(1 for archivo in informe.archivos if MOTIVO_SIN_CONTENIDO in archivo.motivos)
    if sin_contenido:
        print(
            f"{sin_contenido} sin contenido: la altura no se mueve lo bastante y no se escribe nunca, ni con --incluir-revision.",
            file=salida,
        )
    escribibles_en_revision = sum(
        1
        for archivo in informe.archivos
        if archivo.estado is EstadoDeArchivo.EN_REVISION and MOTIVO_SIN_CONTENIDO not in archivo.motivos
    )
    if escribibles_en_revision and not informe.parametros.escribir_revision:
        print(
            "Nada en revisión se escribió: hoy no hay umbrales validados y el gate manda todo a revisión. "
            "Agregá --incluir-revision para escribirlas igual (quedan registradas como sin validar).",
            file=salida,
        )


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sky_claw.local.native_parallax.cli",
        description="Generador nativo de parallax: lote de carpeta plana (experimental, sin umbrales validados).",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    def comunes(p: argparse.ArgumentParser) -> None:
        p.add_argument("--texconv", help=f"ruta del texconv.exe fijado (o la variable {ENTORNO_TEXCONV})")
        p.add_argument(
            "--informe",
            default=INFORME_POR_DEFECTO,
            help=f"dónde escribir el informe JSON (por defecto ./{INFORME_POR_DEFECTO})",
        )

    prueba = sub.add_parser(
        "autoprueba", help="corre la tubería completa sobre una textura sintética en un mod temporal"
    )
    comunes(prueba)

    gen = sub.add_parser("generar", help="procesa los *_n.dds de una carpeta y deja los *_p.dds en el mod de salida")
    comunes(gen)
    gen.add_argument("--carpeta", required=True, help="carpeta con una subcarpeta 'textures' (solo se lee)")
    gen.add_argument(
        "--mods", required=True, help=f"carpeta 'mods' de MO2: la salida va a <mods>/{NOMBRE_DEL_MOD_DE_SALIDA}"
    )
    gen.add_argument(
        "--convencion", choices=[c.value for c in ConvencionNormal], default=ConvencionNormal.DIRECTX.value
    )
    gen.add_argument("--bordes", choices=[b.value for b in Bordes], default=Bordes.PERIODICOS.value)
    gen.add_argument("--escala-x", type=float, default=1.0)
    gen.add_argument("--escala-y", type=float, default=1.0)
    gen.add_argument(
        "--normalizacion", choices=[m.value for m in ModoNormalizacion], default=ModoNormalizacion.MIN_MAX.value
    )
    gen.add_argument("--percentil-bajo", type=float, default=None)
    gen.add_argument("--percentil-alto", type=float, default=None)
    gen.add_argument("--incluir-revision", action="store_true", help="escribe también lo que el gate manda a revisión")
    gen.add_argument(
        "--retirar-no-incluidas",
        action="store_true",
        help="borra las salidas NUESTRAS de corridas anteriores cuyas texturas ya no están en la carpeta de entrada",
    )
    gen.add_argument(
        "--data-root",
        help="con --mod: raíz de datos de MO2; no mide lo que otro mod tapa y avisa si la salida quedaría tapada (ver 'perfil')",
    )
    gen.add_argument("--mod", help="con --data-root: nombre del mod de MO2 que es --carpeta (<mods>/<mod>)")
    gen.add_argument("--perfil", help="perfil de MO2 cuyo modlist.txt se lee (por defecto Default)")

    perfil = sub.add_parser(
        "perfil",
        help="diagnóstico de solo lectura: quién gana, en el perfil de MO2, las rutas de entrada y de salida de un mod",
        description="Diagnóstico de solo lectura del overlay de un perfil de MO2: no genera nada y no necesita texconv.",
        epilog=(
            "Códigos de salida: 0 nada tapado; 2 uso o configuración incorrectos (lo que no se puede afirmar falla cerrado); "
            "4 hay texturas tapadas, fuera del overlay o la salida no está habilitada."
        ),
    )
    perfil.add_argument("--mod", required=True, help="nombre del mod (carpeta de <mods>) cuyas texturas se revisan")
    perfil.add_argument("--mods", required=True, help="carpeta 'mods' de MO2")
    perfil.add_argument(
        "--data-root", required=True, help="raíz de datos de MO2 (donde viven 'profiles' y 'overwrite')"
    )
    perfil.add_argument(
        "--perfil", default="Default", help="perfil de MO2 cuyo modlist.txt se lee (por defecto Default)"
    )
    elegibilidad = sub.add_parser(
        "elegibilidad",
        help="diagnóstico de solo lectura: por cada textura de un mod, ¿alguna malla del perfil haría que el plugin de relieve use la salida?",
        description=(
            "Diagnóstico de solo lectura: por cada *_n.dds de un mod, dice si alguna malla suelta del perfil de MO2 haría que el "
            "plugin de relieve automático use el *_p.dds que el generador publicaría. No genera nada y no necesita texconv."
        ),
        epilog=(
            "Resultados por textura: SI (una forma elegible la usaría), NO (ninguna forma usaría esta salida: todas las que la "
            "referencian quedan fuera, con su razón, o el plugin elegiría antes otra textura suelta, o el generador no la "
            "publicaría porque otro mod tapa la normal de entrada, o el juego no cargaría lo publicado porque otro mod de mayor "
            "prioridad gana la ruta de salida o el mod de salida no está habilitado) o NO_VERIFICADO (no se puede afirmar: sin mallas sueltas que la "
            "referencien, búsqueda que depende de archivos que no se pueden comprobar, rutas ambiguas o mallas que no se pudieron "
            "leer). --mod tiene que ser un mod habilitado del perfil (una carpeta dentro de --mods). Códigos de salida: 0 el informe "
            "se produjo (sea cual sea su contenido); 2 uso o configuración incorrectos (lo que no se puede afirmar falla cerrado)."
        ),
    )
    elegibilidad.add_argument(
        "--mod", required=True, help="nombre del mod (carpeta de <mods>) cuyas texturas se revisan"
    )
    elegibilidad.add_argument("--mods", required=True, help="carpeta 'mods' de MO2")
    elegibilidad.add_argument(
        "--data-root", required=True, help="raíz de datos de MO2 (donde viven 'profiles' y 'overwrite')"
    )
    elegibilidad.add_argument(
        "--perfil", default="Default", help="perfil de MO2 cuyo modlist.txt se lee (por defecto Default)"
    )
    return parser


def _resolver_texconv(argumento: str | None, entorno: Mapping[str, str]) -> pathlib.Path:
    """La ruta del ``texconv`` según la regla ÚNICA del diagnóstico (argumento, si no la variable; solo lo vacío cae)."""
    ruta, _origen = ruta_configurada(argumento, entorno)
    if ruta is None:
        # Sin ruta el diagnóstico no toca el disco: solo arma la frase de «no configurado», la misma para todas las superficies.
        raise UsoIncorrectoError(diagnosticar_texconv(argumento, entorno=entorno).mensaje)
    return pathlib.Path(ruta)


def _clave_fisica(ruta: pathlib.Path) -> pathlib.PurePath:
    """La ruta con los enlaces resueltos y normalizada para comparar, aunque todavía no exista."""
    return pathlib.PurePath(os.path.normcase(os.path.realpath(ruta)))


def _esta_dentro_de(destino: pathlib.PurePath, contenedor: pathlib.PurePath) -> bool:
    return destino == contenedor or contenedor in destino.parents


def _exigir_informe_fuera_de_la_salida(informe: str, raiz: pathlib.Path, carpeta: pathlib.Path) -> None:
    """El informe se escribe DESPUÉS de cerrar la sesión: dentro del mod saltearía el lock, la procedencia y la recuperación
    (``output_writer`` es el único que toca el mod), y dentro de las texturas de entrada pisaría algo del operador.

    Se prohíbe TODO el árbol ``textures`` de la entrada y no solo las normales a procesar: una textura omitida (por tener un
    ``*_p.dds`` autoral al lado, por ejemplo) o el propio ``*_p.dds`` autoral siguen siendo del operador.
    """
    destino = _clave_fisica(pathlib.Path(informe))
    if _esta_dentro_de(destino, _clave_fisica(raiz)):
        raise UsoIncorrectoError(f"--informe no puede apuntar dentro del mod de salida ({raiz}): elegí otra ubicación")
    try:
        textures = carpeta_de_textures(carpeta)
    except RaizInvalidaError as exc:  # la entrada cambió desde que se enumeró: no se puede garantizar nada, se corta
        raise UsoIncorrectoError(str(exc)) from exc
    if _esta_dentro_de(destino, _clave_fisica(textures)):
        raise UsoIncorrectoError(
            f"--informe no puede apuntar dentro de las texturas de entrada ({textures}): elegí otra ubicación"
        )


def _escribir_informe(informe: InformeDelLote, omitidas: Sequence[str], destino: str, salida: TextIO) -> bool:
    """Escribe el informe de forma ATÓMICA y dice si quedó: ``True`` si el archivo final es el nuevo.

    Temporal en la misma carpeta + ``os.replace``: un corte a mitad de la escritura no deja un JSON truncado que el consumidor
    desatendido leería como informe. Un fallo se avisa a la consola y se devuelve ``False``; quien llama NO puede salir con 0,
    porque sin el informe el resultado de la corrida no es auditable.
    """
    ruta = pathlib.Path(destino)
    contenido = (
        json.dumps(
            {**informe.a_diccionario(), "omitidas_al_enumerar": list(omitidas)},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )
    temporal = ruta.with_name(f".{ruta.name}.{os.getpid()}.tmp")
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        temporal.write_text(contenido, encoding="utf-8")
        os.replace(temporal, ruta)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporal.unlink(missing_ok=True)
        print(f"No se pudo escribir el informe en {ruta}: {exc}", file=salida)
        return False
    print(f"Informe: {ruta.resolve()}", file=salida)
    return True


def _informar(informe: InformeDelLote, omitidas: Sequence[str], destino: str, salida: TextIO) -> bool:
    """El informe primero y el resumen después: mostrar es lo último que puede fallar y no se lleva la evidencia.

    Es la ÚNICA salida de los dos comandos (``generar`` y ``autoprueba``): antes cada uno mostraba el resumen y recién
    después escribía el informe, y un fallo de la consola lo perdía (rig 2026-10-24). Devuelve si el informe quedó escrito.
    """
    escrito = _escribir_informe(informe, omitidas, destino, salida)
    _resumen(informe, omitidas, salida)
    return escrito


def _progreso(salida: TextIO) -> Callable[[int, int, str], None]:
    def mostrar(indice: int, total: int, ruta: str) -> None:
        print(f"[{indice}/{total}] {ruta}", file=salida, flush=True)

    return mostrar


def _codigo(informe: InformeDelLote, *, informe_escrito: bool) -> int:
    """0 solo si el lote está completo Y su informe quedó escrito: sin informe el resultado no se puede auditar."""
    return CODIGO_OK if informe.completo and informe_escrito else CODIGO_CON_FALLOS


# ---------------------------------------------------------------------------
# ``perfil``: quién gana las rutas de un mod en el perfil de MO2 (solo lectura)
# ---------------------------------------------------------------------------


def _perfil(args: argparse.Namespace, salida: TextIO) -> int:
    """Diagnóstico de SOLO lectura del perfil. No escribe, no coordina y no exporta (no pide ``texconv``)."""
    with _perfil_error_como_uso():
        informe = diagnosticar_perfil(
            mods_dir=pathlib.Path(args.mods),
            data_root=pathlib.Path(args.data_root),
            perfil=args.perfil,
            mod=args.mod,
            maximo_de_archivos=_MAXIMO_DE_ARCHIVOS_DE_ENTRADA,
        )
    plural = "textura" if informe.n_texturas == 1 else "texturas"
    print(
        f"Perfil {informe.perfil!r}: {informe.n_habilitados} mods habilitados; {informe.n_texturas} {plural} revisadas en "
        f"{informe.mod!r}.",
        file=salida,
    )
    for h in informe.hallazgos:
        print(linea_de_hallazgo(h), file=salida)
    if not informe.hallazgos:
        print("Sin texturas tapadas: el overlay entrega lo que el generador mediría y publicaría.", file=salida)
        return CODIGO_OK
    palabra = "hallazgo" if len(informe.hallazgos) == 1 else "hallazgos"
    print(
        f"{len(informe.hallazgos)} {palabra}. Esta vista es estructural: no prueba lo que se ve en el juego.",
        file=salida,
    )
    return CODIGO_CON_TAPADOS


# ---------------------------------------------------------------------------
# ``elegibilidad``: ¿alguna malla del perfil usaría la salida? (solo lectura)
# ---------------------------------------------------------------------------

_MAXIMO_DE_MALLAS_NO_LEIDAS_LISTADAS: Final = 5


def _elegibilidad(args: argparse.Namespace, salida: TextIO) -> int:
    """Diagnóstico de SOLO lectura de la elegibilidad. No escribe, no coordina y no exporta (no pide ``texconv``)."""
    with _perfil_error_como_uso():
        informe = evaluar_elegibilidad(
            mods_dir=pathlib.Path(args.mods),
            data_root=pathlib.Path(args.data_root),
            perfil=args.perfil,
            mod=args.mod,
        )
    textura = "textura" if len(informe.texturas) == 1 else "texturas"
    malla = "malla leída" if informe.n_mallas_leidas == 1 else "mallas leídas"
    print(
        f"Perfil {informe.perfil!r}: {informe.n_habilitados} mods habilitados; {len(informe.texturas)} {textura} de "
        f"{informe.mod!r} evaluadas contra {informe.n_mallas_leidas} {malla}.",
        file=salida,
    )
    for t in informe.texturas:
        print(f"  [{t.efecto.value}] {t.ruta_de_salida}: {t.razon}", file=salida)
        for ruta in t.mallas:
            print(f"      {ruta}", file=salida)
        if t.n_mallas > len(t.mallas):
            print(f"      … y {t.n_mallas - len(t.mallas)} más", file=salida)
    conteo = {e: sum(1 for t in informe.texturas if t.efecto is e) for e in EfectoPosible}
    print(
        f"Resumen: {conteo[EfectoPosible.SI]} SI, {conteo[EfectoPosible.NO]} NO, "
        f"{conteo[EfectoPosible.NO_VERIFICADO]} NO_VERIFICADO.",
        file=salida,
    )
    if informe.mallas_no_leidas:
        listadas = informe.mallas_no_leidas[:_MAXIMO_DE_MALLAS_NO_LEIDAS_LISTADAS]
        resto = len(informe.mallas_no_leidas) - len(listadas)
        mas = f" y {resto} más" if resto else ""
        print(
            f"{len(informe.mallas_no_leidas)} mallas no se pudieron leer por completo: {'; '.join(listadas)}{mas}.",
            file=salida,
        )
    for aviso in informe.plugin.avisos():
        print(f"Aviso: {aviso}.", file=salida)
    print(f"Alcance: {informe.alcance}", file=salida)
    return CODIGO_OK


def _exigir_perfil_completo_o_ninguno(args: argparse.Namespace) -> bool:
    """``True`` si ``generar`` corre con perfil. ``--data-root`` y ``--mod`` van juntos; ``--perfil`` solo no alcanza."""
    pedidos = {"--data-root": args.data_root, "--mod": args.mod, "--perfil": args.perfil}
    if not any(pedidos.values()):
        return False
    if not (args.data_root and args.mod):
        raise UsoIncorrectoError("el perfil de MO2 pide --data-root y --mod juntos (--perfil es opcional)")
    if args.retirar_no_incluidas:
        raise UsoIncorrectoError(
            "no se combina --retirar-no-incluidas con el perfil de MO2: lo que se deja sin medir por estar tapado "
            "no está ausente, y retirar su salida anterior sería borrarla por error"
        )
    return True


def _aplicar_perfil(
    args: argparse.Namespace,
    mods: pathlib.Path,
    normales: list[NormalDescubierta],
    omitidas: list[str],
) -> tuple[list[NormalDescubierta], list[str], list[Hallazgo]]:
    """Quita de ``normales`` las que el overlay no entrega como el generador supone y devuelve los avisos de la salida."""
    with _perfil_error_como_uso():
        resuelto = cargar_perfil(
            mods_dir=mods, data_root=pathlib.Path(args.data_root), perfil=args.perfil or "Default", mod=args.mod
        )
    if _clave_fisica(pathlib.Path(args.carpeta)) != _clave_fisica(resuelto.carpeta_del_mod):
        raise UsoIncorrectoError(
            f"--carpeta ({args.carpeta}) no es la carpeta del mod {resuelto.mod!r} ({resuelto.carpeta_del_mod}): "
            "«lo gana otro mod» se compara contra esa carpeta y no tendría sentido"
        )
    hallazgos = hallazgos_del_perfil(resuelto.indice, resuelto.enabled, resuelto.mod, normales)
    sin_medir = {h.ruta: h for h in hallazgos if h.tipo in TIPOS_QUE_IMPIDEN_MEDIR}
    restantes = [n for n in normales if n.ruta_de_origen not in sin_medir]
    omitidas = [*omitidas, *(texto_de_omision(r, f"{h.tipo}: {h.detalle}") for r, h in sorted(sin_medir.items()))]
    if not restantes:
        detalle = "\n".join(linea_de_hallazgo(h) for h in hallazgos)
        raise UsoIncorrectoError(
            f"todas las texturas de {resuelto.mod!r} están tapadas o fuera del overlay: no hay nada que medir\n{detalle}"
        )
    # Primero lo que se deja sin medir y después lo de la salida: el operador lo ve ANTES de procesar y de escribir.
    avisos = [*sin_medir.values(), *(h for h in hallazgos if h.tipo not in TIPOS_QUE_IMPIDEN_MEDIR)]
    return restantes, omitidas, avisos


async def _generar(
    args: argparse.Namespace,
    coordinacion: CoordinacionDeSalida,
    exportador: ExportadorDeAltura,
    salida: TextIO,
) -> int:
    parametros = ParametrosDelLote(
        convencion=ConvencionNormal(args.convencion),
        bordes=Bordes(args.bordes),
        escala_x=args.escala_x,
        escala_y=args.escala_y,
        normalizacion=ModoNormalizacion(args.normalizacion),
        percentil_bajo=args.percentil_bajo,
        percentil_alto=args.percentil_alto,
        escribir_revision=args.incluir_revision,
        retirar_no_incluidas=args.retirar_no_incluidas,
    )
    mods = pathlib.Path(args.mods)
    con_perfil = _exigir_perfil_completo_o_ninguno(args)
    # Retirar actúa sobre lo que NO figura: con una enumeración incompleta, «no visto» se confundiría con «ausente». Con perfil,
    # lo que se deja sin medir por estar tapado tampoco es «ausente»: no se combinan.
    normales, omitidas = descubrir_normales_de_carpeta(
        pathlib.Path(args.carpeta), exigir_completa=args.retirar_no_incluidas or con_perfil
    )
    if not normales:
        raise UsoIncorrectoError(f"no encontré ningún *{_SUFIJO_DE_ENTRADA} procesable bajo {args.carpeta}/textures")
    avisos: list[Hallazgo] = []
    if con_perfil:
        normales, omitidas, avisos = _aplicar_perfil(args, mods, normales, omitidas)
    raiz_de_salida = (mods / NOMBRE_DEL_MOD_DE_SALIDA).absolute()
    _exigir_informe_fuera_de_la_salida(args.informe, raiz_de_salida, pathlib.Path(args.carpeta))
    for aviso in avisos:  # ANTES de procesar y de escribir: el operador ve si la salida va a quedar tapada
        print(linea_de_hallazgo(aviso), file=salida)
    print(f"{len(normales)} texturas por procesar; salida: {mods / NOMBRE_DEL_MOD_DE_SALIDA}", file=salida)
    informe = await procesar_lote(
        normales,
        raiz_de_salida=raiz_de_salida,
        coordinacion=coordinacion,
        exportador=exportador,
        parametros=parametros,
        progreso=_progreso(salida),
    )
    escrito = _informar(informe, omitidas, args.informe, salida)
    return _codigo(informe, informe_escrito=escrito)


def _borrar_temporal(carpeta: pathlib.Path, salida: TextIO) -> None:
    """Borra la carpeta temporal PROPIA sin atravesar enlaces y SIN lanzar: corre en un ``finally`` y no debe tapar el
    resultado de la prueba. Usa la primitiva del repo (``rmtree_link_aware``), no ``tempfile.TemporaryDirectory`` (que borra con
    ``shutil.rmtree``): que el árbol sea propio no habilita la excepción (``tests/test_borrado_recursivo.py``). No pide
    ``limpiar_readonly``: solo contiene lo que este proceso y el escritor crearon. Un fallo (en Windows, un antivirus con un
    archivo abierto) se avisa al operador y no cambia el resultado."""
    try:
        rmtree_link_aware(carpeta)
    except OSError as exc:
        print(f"Aviso: no se pudo borrar la carpeta temporal {carpeta}: {exc}", file=salida)


async def _autoprueba(
    args: argparse.Namespace,
    coordinacion: CoordinacionDeSalida,
    exportador: ExportadorDeAltura,
    salida: TextIO,
) -> int:
    base = pathlib.Path(tempfile.mkdtemp(prefix="skyclaw-autoprueba-"))
    try:
        (base / "salida").mkdir()
        origen = base / "entrada" / "prueba_n.png"
        origen.parent.mkdir()
        _escribir_normal_sintetica(origen, _LADO_DE_LA_AUTOPRUEBA)
        ruta_de_salida = "textures/skyclaw_autoprueba/prueba_p.dds"
        raiz = base / "salida" / NOMBRE_DEL_MOD_DE_SALIDA
        print(
            f"Autoprueba sobre una textura sintética de {_LADO_DE_LA_AUTOPRUEBA}x{_LADO_DE_LA_AUTOPRUEBA} (mod temporal).",
            file=salida,
        )
        informe = await procesar_lote(
            [NormalDeCarpeta("textures/skyclaw_autoprueba/prueba_n.dds", ruta_de_salida, origen)],
            raiz_de_salida=raiz,
            coordinacion=coordinacion,
            exportador=exportador,
            parametros=ParametrosDelLote(escribir_revision=True),
            progreso=_progreso(salida),
        )
        escrito = _informar(informe, [], args.informe, salida)
        fallos = _verificar_autoprueba(raiz, ruta_de_salida, informe)
        if not escrito:
            fallos.append("el informe no se pudo escribir (ver arriba)")
    finally:
        _borrar_temporal(base, salida)
    if fallos:
        print("\nAUTOPRUEBA FALLÓ:", file=salida)
        for fallo in fallos:
            print(f"  - {fallo}", file=salida)
        return CODIGO_CON_FALLOS
    print("\nAUTOPRUEBA OK: la tubería completa funciona en esta máquina (texconv, escritor y disco).", file=salida)
    return CODIGO_OK


def _verificar_autoprueba(raiz: pathlib.Path, ruta_de_salida: str, informe: InformeDelLote) -> list[str]:
    fallos: list[str] = []
    if not informe.completo or informe.contar()[EstadoDeArchivo.ESCRITA] != 1:
        fallos.append("la textura sintética no quedó escrita (ver arriba)")
        return fallos
    destino = raiz.joinpath(*ruta_de_salida.split("/"))
    try:
        info = inspeccionar_dds_bc4(destino.read_bytes())
    except (OSError, ExportError) as exc:
        fallos.append(f"la salida no es un BC4 válido: {exc}")
        return fallos
    if (info.ancho, info.alto) != (_LADO_DE_LA_AUTOPRUEBA, _LADO_DE_LA_AUTOPRUEBA):
        fallos.append(
            f"el BC4 tiene {info.ancho}x{info.alto} y se esperaba {_LADO_DE_LA_AUTOPRUEBA}x{_LADO_DE_LA_AUTOPRUEBA}"
        )
    try:
        manifiesto = leer_manifiesto((raiz / ".skyclaw-output" / "manifest.json").read_bytes())
    except (OSError, ValueError) as exc:
        fallos.append(f"el manifiesto no se pudo leer: {exc}")
    else:
        if [e.ruta for e in manifiesto.entradas] != [ruta_de_salida]:
            fallos.append("el manifiesto no describe exactamente la salida de la prueba")
    return fallos


def main(
    argv: Sequence[str] | None = None,
    *,
    entorno: Mapping[str, str] | None = None,
    fabrica_de_exportador: FabricaDeExportador | None = None,
    base_de_estado: pathlib.Path | None = None,
    salida: TextIO | None = None,
) -> int:
    """Punto de entrada. Los argumentos con nombre existen para los tests; en uso normal van todos por defecto."""
    destino = _sin_codepage(salida if salida is not None else sys.stdout)
    variables = entorno if entorno is not None else os.environ
    fabrica = fabrica_de_exportador if fabrica_de_exportador is not None else crear_exportador_texconv
    args = construir_parser().parse_args(argv)
    try:
        if args.comando == "perfil":  # diagnóstico de solo lectura: ni texconv ni coordinación de salida
            return _perfil(args, destino)
        if args.comando == "elegibilidad":  # ídem: lee mallas y texturas del perfil, no escribe
            return _elegibilidad(args, destino)
        exportador = fabrica(_resolver_texconv(args.texconv, variables))
        coordinacion = construir_coordinacion_de_salida(base=base_de_estado)
        comando = _autoprueba if args.comando == "autoprueba" else _generar
        return asyncio.run(_con_cierre(comando, args, coordinacion, exportador, destino))
    except SalidaOcupadaError as exc:
        print(f"La salida está en uso por otra instancia: {exc}", file=destino)
        return CODIGO_SALIDA_OCUPADA
    except (UsoIncorrectoError, LoteError, SalidaNoEscribibleError, ExportError, DependenciaNoDisponibleError) as exc:
        print(f"Error: {exc}", file=destino)
        return CODIGO_DE_USO
    except KeyboardInterrupt:
        print("Interrumpido. Si había un lote a medias, la próxima corrida lo recupera sola.", file=destino)
        return CODIGO_INTERRUMPIDO


async def _con_cierre(
    comando: Callable[[argparse.Namespace, CoordinacionDeSalida, ExportadorDeAltura, TextIO], Awaitable[int]],
    args: argparse.Namespace,
    coordinacion: CoordinacionDeSalida,
    exportador: ExportadorDeAltura,
    salida: TextIO,
) -> int:
    try:
        return await comando(args, coordinacion, exportador, salida)
    finally:
        await coordinacion.close()


if __name__ == "__main__":
    raise SystemExit(main())

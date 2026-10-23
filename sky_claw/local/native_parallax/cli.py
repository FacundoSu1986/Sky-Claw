"""Línea de comandos del lote de carpeta plana del generador nativo de parallax (NP-A0a, ADR 0014 §2.5).

Uso (desde la raíz del repo, con el entorno del extra instalado)::

    python -m sky_claw.local.native_parallax.cli autoprueba --texconv C:\\ruta\\texconv.exe
    python -m sky_claw.local.native_parallax.cli generar --carpeta D:\\entrada --mods D:\\MO2\\mods --texconv C:\\ruta\\texconv.exe

- ``autoprueba`` corre la tubería COMPLETA sobre una textura sintética en un mod temporal: lee, integra, mide, exporta con
  el ``texconv`` real y escribe con el escritor sobre el disco de verdad. No toca nada tuyo. Es lo primero que hay que
  correr en una máquina nueva (en Windows ejerce, por primera vez fuera de la CI, el ``texconv`` y el escritor sobre NTFS).
- ``generar`` procesa los ``*_n.dds`` bajo ``<carpeta>/textures`` y deja los ``*_p.dds`` en el mod de salida
  ``<mods>/<mod de salida gestionado>`` (el nombre lo fija ``output.NOMBRE_DEL_MOD_DE_SALIDA``). La carpeta de entrada solo se LEE.

Es un borde delgado sobre ``lote.procesar_lote``: parsea, valida, arma el exportador y muestra el informe. La carpeta de
entrada la enumera ``provider.py`` (NP-A0b): no sigue enlaces, ignora lo que no es ``*_n.dds`` y omite (y dice) lo que ya
tiene un ``*_p.dds`` autoral con la misma ruta de salida. Acá solo se traducen sus errores y sus omisiones a mensajes.

Códigos de salida: 0 todo bien; 1 alguna textura falló; 2 uso o configuración incorrectos; 3 otra instancia está
escribiendo esa salida; 130 interrumpido.

No hay gate validado todavía (``gates.py`` es fail-closed): TODA salida queda en revisión y solo se escribe con
``--incluir-revision``. Los parámetros por defecto (DirectX, escalas 1, bordes periódicos, min-max) son los de la referencia
sintética de NP-D0, NO una calibración para Skyrim: para eso están los informes de esta herramienta.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib
import struct
import sys
import tempfile
import zlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, TextIO

from sky_claw.app.security.links import rmtree_link_aware
from sky_claw.local.native_parallax._extra import importar_numpy
from sky_claw.local.native_parallax.decode import ConvencionNormal, DependenciaNoDisponibleError
from sky_claw.local.native_parallax.export import (
    ExportError,
    inspeccionar_dds_bc4,
    verificar_texconv,
)
from sky_claw.local.native_parallax.integrate import Bordes, ModoNormalizacion
from sky_claw.local.native_parallax.lote import (
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
    MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO,
    DemasiadosArchivosError,
    NormalDescubierta,
    RaizInvalidaError,
    carpeta_de_textures,
    descubrir_normales,
)

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "CODIGO_CON_FALLOS",
    "CODIGO_DE_USO",
    "CODIGO_INTERRUMPIDO",
    "CODIGO_OK",
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
CODIGO_INTERRUMPIDO: Final = 130

#: Misma variable que usa el smoke real (``tests/test_texconv_real.py``): una sola forma de decir dónde está el binario.
ENTORNO_TEXCONV: Final = "SKYCLAW_TEXCONV_EXE"
INFORME_POR_DEFECTO: Final = "informe_parallax.json"
_MAXIMO_DE_ARCHIVOS_DE_ENTRADA: Final = 10_000
_SUFIJO_DE_ENTRADA: Final = "_n.dds"
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


def _texto_de_omision(ruta: str, detalle: str) -> str:
    return f"{ruta}: {detalle}"


def descubrir_normales_de_carpeta(
    carpeta: pathlib.Path, *, exigir_completa: bool = False
) -> tuple[list[NormalDescubierta], list[str]]:
    """Los ``*_n.dds`` bajo ``<carpeta>/textures`` y los motivos por los que se omitió algo. Solo lee.

    Adaptador delgado de ``provider.descubrir_normales``: traduce sus errores a ``UsoIncorrectoError`` (se muestran sin
    traceback) y sus omisiones a una línea de texto cada una. La CLI pone su propio tope de entradas (más bajo que el del
    proveedor: es una herramienta experimental para probar en un rig).

    ``exigir_completa``: una carpeta que no se pudo leer o una entrada que no se pudo inspeccionar levanta
    ``UsoIncorrectoError`` en vez de omitirse. Lo pide quien actúa sobre lo que NO figura (retirar salidas): sin ver una
    carpeta no se sabe qué hay en ella. Las omisiones deliberadas (enlaces, un ``*_p.dds`` autoral, rutas inadmisibles,
    duplicados) no son errores de lectura y no la hacen fallar (``Descubrimiento.completo``).
    """
    try:
        descubrimiento = descubrir_normales(carpeta, maximo_de_archivos=_MAXIMO_DE_ARCHIVOS_DE_ENTRADA)
    except (RaizInvalidaError, DemasiadosArchivosError) as exc:
        raise UsoIncorrectoError(str(exc)) from exc
    if exigir_completa and not descubrimiento.completo:
        sin_ver = "; ".join(
            _texto_de_omision(o.ruta_de_origen, o.detalle)
            for o in descubrimiento.omitidas
            if o.motivo in MOTIVOS_QUE_DEJAN_LO_NO_OBSERVADO
        )
        raise UsoIncorrectoError(
            f"la enumeración quedó incompleta (lo que no se pudo ver no se puede dar por ausente): {sin_ver}"
        )
    return list(descubrimiento.normales), [
        _texto_de_omision(o.ruta_de_origen, o.detalle) for o in descubrimiento.omitidas
    ]


# ---------------------------------------------------------------------------
# Exportador real y autoprueba
# ---------------------------------------------------------------------------


def crear_exportador_texconv(texconv: pathlib.Path) -> ExportadorDeAltura:
    """El exportador real: verifica el binario contra el pin ANTES de empezar y lo ejecuta con el ejecutor fijado."""
    verificar_texconv(texconv)  # falla cerrado y temprano, con un mensaje que dice qué huella se esperaba
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
    if conteos[EstadoDeArchivo.EN_REVISION] and not informe.parametros.escribir_revision:
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
    return parser


def _resolver_texconv(argumento: str | None, entorno: Mapping[str, str]) -> pathlib.Path:
    valor = argumento or entorno.get(ENTORNO_TEXCONV)
    if not valor:
        raise UsoIncorrectoError(
            f"falta el texconv.exe: pasá --texconv RUTA o definí la variable {ENTORNO_TEXCONV} "
            "(tiene que ser el binario oficial fijado por el proyecto: se verifica su huella SHA-256)"
        )
    return pathlib.Path(valor)


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


def _escribir_informe(informe: InformeDelLote, destino: str, salida: TextIO) -> None:
    ruta = pathlib.Path(destino)
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(
            json.dumps(informe.a_diccionario(), indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        print(f"No se pudo escribir el informe en {ruta}: {exc}", file=salida)
        return
    print(f"Informe: {ruta.resolve()}", file=salida)


def _progreso(salida: TextIO) -> Callable[[int, int, str], None]:
    def mostrar(indice: int, total: int, ruta: str) -> None:
        print(f"[{indice}/{total}] {ruta}", file=salida, flush=True)

    return mostrar


def _codigo(informe: InformeDelLote) -> int:
    return CODIGO_OK if informe.completo else CODIGO_CON_FALLOS


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
    # Retirar actúa sobre lo que NO figura: con una enumeración incompleta, «no visto» se confundiría con «ausente».
    normales, omitidas = descubrir_normales_de_carpeta(
        pathlib.Path(args.carpeta), exigir_completa=args.retirar_no_incluidas
    )
    if not normales:
        raise UsoIncorrectoError(f"no encontré ningún *{_SUFIJO_DE_ENTRADA} procesable bajo {args.carpeta}/textures")
    mods = pathlib.Path(args.mods)
    raiz_de_salida = (mods / NOMBRE_DEL_MOD_DE_SALIDA).absolute()
    _exigir_informe_fuera_de_la_salida(args.informe, raiz_de_salida, pathlib.Path(args.carpeta))
    print(f"{len(normales)} texturas por procesar; salida: {mods / NOMBRE_DEL_MOD_DE_SALIDA}", file=salida)
    informe = await procesar_lote(
        normales,
        raiz_de_salida=raiz_de_salida,
        coordinacion=coordinacion,
        exportador=exportador,
        parametros=parametros,
        progreso=_progreso(salida),
    )
    _resumen(informe, omitidas, salida)
    _escribir_informe(informe, args.informe, salida)
    return _codigo(informe)


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
        _resumen(informe, [], salida)
        _escribir_informe(informe, args.informe, salida)
        fallos = _verificar_autoprueba(raiz, ruta_de_salida, informe)
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
    destino = salida if salida is not None else sys.stdout
    variables = entorno if entorno is not None else os.environ
    fabrica = fabrica_de_exportador if fabrica_de_exportador is not None else crear_exportador_texconv
    args = construir_parser().parse_args(argv)
    try:
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

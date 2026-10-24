"""Lote de carpeta plana del generador nativo de parallax (NP-A0a, ADR 0014 §2.5).

Arma la tubería completa sobre piezas que ya existen —no agrega matemática propia—: por cada normal, la lee
(``decode``), la integra (``integrate``), la mide (``metrics``), el gate decide (``gates``), el exportador la lleva a
``*_p.dds`` y el escritor (``output_writer``) la publica en la salida gestionada, con la procedencia de lo que de verdad
corrió. Es la composición; las decisiones de política que no son nuestras (umbrales, qué convención tiene un asset real)
llegan como parámetros explícitos y quedan registradas en la procedencia.

Propiedades del mecanismo, cada una con su ancla en ``tests/test_native_parallax_lote.py``:

- **Dos fases.** (1) Se calcula todo sin tocar la salida (leer, integrar, medir, exportar a un directorio de trabajo
  privado). (2) Se abre UNA sesión del escritor, que toma el lock, se planifica el conjunto y se aplica. La salida se
  toca una sola vez y bajo el lock; un archivo que falla en (1) no frena a los demás ni deja nada a medias.
- **El gate es fail-closed y la política es explícita.** Sin umbrales validados todo es ``REVIEW`` (``gates.py``). Una
  salida en ``REVIEW`` NO se escribe salvo ``escribir_revision=True``; sus métricas se miden y se informan igual, porque
  son los datos con los que se calibran los umbrales. ``escribir_revision`` y ``retirar_no_incluidas`` son política:
  no entran a los bytes ni a la clave de caché.
- **La procedencia es la de lo que corrió.** La huella de la normal se calcula ANTES y DESPUÉS de decodificarla y tienen
  que coincidir (si no, el archivo cambió mientras se procesaba); el encoder es el que DEVOLVIÓ el exportador, no el que
  se esperaba; los parámetros que cambian los bytes (``a_parametros_de_procedencia``) viajan a la clave de caché.
- **Los parámetros viajan IGUALES** a la integración y a cada métrica (``bordes``, ``escala_x``, ``escala_y``): un
  cableado que fijara los bordes en una sola llamada mezclaría dos dominios en una misma medición.
- **Se conserva lo anterior.** Las salidas de corridas previas que esta corrida no regenera se declaran como deseadas con
  su clave vigente (caché: no se tocan). Solo ``retirar_no_incluidas`` retira las de texturas que NO figuran en la
  entrada; una textura que figura pero falló o quedó en revisión conserva su salida anterior.
- **Errores tipados, nunca ``except Exception``.** ``ERRORES_POR_ARCHIVO`` enumera lo que falla UNA ruta; cualquier otra
  excepción es un bug y sube.

Límites declarados:

- El exportador y el decodificador son síncronos/CPU-bound: la lectura y la medición corren en un hilo
  (``asyncio.to_thread``) para que el latido del lock no se detenga. Cancelar el ``await`` NO interrumpe el hilo
  (``integrate.integrar`` lo documenta): el cálculo en curso termina antes de que la cancelación avance.
- La entrada se procesa de a una (la integración puede pasar de 1 GiB de pico a 4096²): no hay concurrencia acá.
- No se saltea el cómputo de una textura con caché vigente: se recalcula y el escritor la deja como ``OMITIDA``
  (optimizar eso exige planificar antes de calcular; no es de este PR).
- No decide qué texturas procesar (eso es del proveedor de NP-A0) ni conecta con MO2 (``meta.ini``, habilitar el mod).

Dependencias: ``numpy`` y Pillow pertenecen al extra opcional ``native-parallax``; este módulo no los importa (los usan
``decode``, ``integrate`` y ``metrics``, que los piden de forma perezosa).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import math
import os
import pathlib
import platform
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Protocol

from sky_claw.app.security.links import rmtree_link_aware
from sky_claw.local.native_parallax.decode import ConvencionNormal, NormalDecodeError, NormalDecoder
from sky_claw.local.native_parallax.export import ExportError
from sky_claw.local.native_parallax.gates import Decision, GateError, MetricasDeCalidad, Veredicto, decidir
from sky_claw.local.native_parallax.integrate import (
    PONDERACION_DEL_PRODUCTO,
    Bordes,
    IntegracionError,
    ModoNormalizacion,
    _exigir_escala,
    _exigir_percentiles,
    integrar,
    normalizar_altura,
)
from sky_claw.local.native_parallax.metrics import (
    acuerdo_normal,
    contar_no_finitos,
    razon_de_costura,
    residuo_de_integrabilidad,
)
from sky_claw.local.native_parallax.output import (
    TAMANO_MAXIMO_DEL_ARCHIVO,
    AccionDeSalida,
    EncoderUsado,
    NivelDeReproducibilidad,
    OrigenDeEntrada,
    Procedencia,
    SalidaDeseada,
    SalidaGestionadaError,
    clave_de_cache,
    clave_de_ruta,
    parametros_canonicos,
)
from sky_claw.local.native_parallax.output_writer import (
    CoordinacionDeSalida,
    EfectoDeSalida,
    FuenteDeSalida,
    abrir_sesion_de_salida,
    validar_raiz_de_salida,
)
from sky_claw.local.native_parallax.provider import huella_acotada
from sky_claw.local.thread_bridge import esperar_hilo_ininterrumpible

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "ERRORES_POR_ARCHIVO",
    "EXPORTADOR_ID",
    "VERSION_DEL_LOTE",
    "EstadoDeArchivo",
    "ExportadorDeAltura",
    "InformeDelLote",
    "LoteError",
    "LoteInvalidoError",
    "NormalAProcesar",
    "ParametrosDelLote",
    "ParametrosDelLoteInvalidosError",
    "ResultadoDeArchivo",
    "ResultadoDeRuta",
    "ResultadoDelExportador",
    "procesar_lote",
]

#: Versión de la DEFINICIÓN de la tubería: entra a los parámetros de procedencia, así que cambiarla invalida las cachés.
VERSION_DEL_LOTE: Final = "np-a0a.v1"
#: Identidad de nuestra tubería de exportación (altura → mips propios → BC4 con el ``texconv`` fijado), para el manifiesto.
EXPORTADOR_ID: Final = "legacy_parallax_bc4_v1"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


class LoteError(Exception):
    """Base de los errores propios del lote."""


class LoteInvalidoError(LoteError, ValueError):
    """La entrada del lote viola su contrato (se rechaza entera, antes de tocar nada)."""


class ParametrosDelLoteInvalidosError(LoteError, ValueError):
    """Un parámetro del lote fuera de su dominio."""


class OrigenInvalidoError(LoteError):
    """El archivo de origen no es admisible, o cambió mientras se procesaba (falla esa ruta, no el lote)."""


#: Lo que hace fallar UNA ruta sin frenar al lote. Todo lo demás es un bug y sube. ``OSError`` cubre la lectura del origen
#: y la escritura del directorio de trabajo; ``SalidaGestionadaError`` cubre una procedencia que el exportador armó mal.
ERRORES_POR_ARCHIVO: Final[tuple[type[BaseException], ...]] = (
    NormalDecodeError,
    IntegracionError,
    GateError,
    ExportError,
    SalidaGestionadaError,
    OrigenInvalidoError,
    OSError,
)


# ---------------------------------------------------------------------------
# Contratos
# ---------------------------------------------------------------------------


class NormalAProcesar(Protocol):
    """Una textura de entrada: lo que entrega el proveedor (``provider.py``, NP-A0) y consume el lote."""

    @property
    def ruta_de_origen(self) -> str: ...

    @property
    def ruta_de_salida(self) -> str: ...

    @property
    def ruta_absoluta(self) -> pathlib.Path: ...


class ResultadoDelExportador(Protocol):
    """Lo que devuelve el exportador: los bytes del ``*_p.dds`` y la identidad de lo que los produjo."""

    @property
    def datos(self) -> bytes: ...

    @property
    def texconv_version(self) -> str: ...

    @property
    def texconv_sha256(self) -> str: ...

    @property
    def argumentos(self) -> tuple[str, ...]: ...


ExportadorDeAltura = Callable[["NDArray[np.floating[Any]]"], Awaitable[ResultadoDelExportador]]
Decisor = Callable[[MetricasDeCalidad], Decision]


class EstadoDeArchivo(Enum):
    """Vocabulario CERRADO del destino de cada textura de la entrada."""

    ESCRITA = "escrita"  # el escritor la dejó en la salida (publicada, reemplazada o con la caché vigente)
    EN_REVISION = "en_revision"  # el gate la mandó a revisión y no se pidió escribirla
    NO_PROCESADA = "no_procesada"  # falló antes de tener algo que escribir
    NO_ESCRITA = "no_escrita"  # el escritor no la dejó en la salida (preservada, conflicto o fallida)


_EFECTOS_QUE_DEJAN_LA_SALIDA = frozenset({EfectoDeSalida.PUBLICADA, EfectoDeSalida.REEMPLAZADA, EfectoDeSalida.OMITIDA})


@dataclass(frozen=True, slots=True)
class ParametrosDelLote:
    """Qué parámetros de la tubería se usan. Los que cambian los bytes van a la procedencia; la política, no.

    Ninguno tiene un valor «validado»: son los de la referencia de NP-D0 (DirectX, escalas 1, bordes periódicos, min-max)
    y el operador los cambia con evidencia (NP-X0a: la convención la fija la fuente del archivo, no esta tubería).
    """

    convencion: ConvencionNormal = ConvencionNormal.DIRECTX
    bordes: Bordes = Bordes.PERIODICOS
    escala_x: float = 1.0
    escala_y: float = 1.0
    normalizacion: ModoNormalizacion = ModoNormalizacion.MIN_MAX
    percentil_bajo: float | None = None
    percentil_alto: float | None = None
    escribir_revision: bool = False
    retirar_no_incluidas: bool = False

    def __post_init__(self) -> None:
        for nombre, tipo in (
            ("convencion", ConvencionNormal),
            ("bordes", Bordes),
            ("normalizacion", ModoNormalizacion),
        ):
            if not isinstance(getattr(self, nombre), tipo):
                raise ParametrosDelLoteInvalidosError(
                    f"{nombre} debe ser un {tipo.__name__}: {getattr(self, nombre)!r}"
                )
        for nombre in ("escribir_revision", "retirar_no_incluidas"):
            if not isinstance(getattr(self, nombre), bool):
                raise ParametrosDelLoteInvalidosError(f"{nombre} debe ser un booleano: {getattr(self, nombre)!r}")
        try:
            _exigir_escala("escala_x", self.escala_x)
            _exigir_escala("escala_y", self.escala_y)
            if self.normalizacion is ModoNormalizacion.PERCENTILES:
                _exigir_percentiles(self.percentil_bajo, self.percentil_alto)
            elif self.percentil_bajo is not None or self.percentil_alto is not None:
                raise ParametrosDelLoteInvalidosError(
                    "MIN_MAX no usa percentiles: no se pasan percentil_bajo ni percentil_alto"
                )
        except IntegracionError as exc:
            raise ParametrosDelLoteInvalidosError(str(exc)) from exc

    def a_parametros_de_procedencia(self) -> dict[str, Any]:
        """Los parámetros que determinan los bytes de la salida (la política no entra)."""
        return {
            "version_del_lote": VERSION_DEL_LOTE,
            "convencion": self.convencion.value,
            "bordes": self.bordes.value,
            "escala_x": float(self.escala_x),
            "escala_y": float(self.escala_y),
            "normalizacion": self.normalizacion.value,
            "percentil_bajo": None if self.percentil_bajo is None else float(self.percentil_bajo),
            "percentil_alto": None if self.percentil_alto is None else float(self.percentil_alto),
        }

    def a_diccionario(self) -> dict[str, Any]:
        return {
            **self.a_parametros_de_procedencia(),
            "escribir_revision": self.escribir_revision,
            "retirar_no_incluidas": self.retirar_no_incluidas,
        }


@dataclass(frozen=True, slots=True)
class ResultadoDeArchivo:
    """Qué pasó con una textura de la entrada."""

    ruta_de_origen: str
    ruta_de_salida: str
    estado: EstadoDeArchivo
    efecto: EfectoDeSalida | None
    veredicto: Veredicto | None
    motivos: tuple[str, ...]
    metricas: MetricasDeCalidad | None
    detalle: str


@dataclass(frozen=True, slots=True)
class ResultadoDeRuta:
    """Qué hizo el escritor con una ruta que NO es de la entrada (una salida anterior conservada, retirada, etc.)."""

    ruta: str
    efecto: EfectoDeSalida
    detalle: str


def _numero_seguro(valor: float) -> float | None:
    return float(valor) if math.isfinite(valor) else None


@dataclass(frozen=True, slots=True)
class InformeDelLote:
    """Todo lo que el operador necesita saber de una corrida. Solo rutas relativas: nunca rutas absolutas."""

    parametros: ParametrosDelLote
    archivos: tuple[ResultadoDeArchivo, ...]
    otras_rutas: tuple[ResultadoDeRuta, ...]
    encoder: tuple[str, str] | None  # (versión, huella) del texconv que corrió, si corrió

    @property
    def completo(self) -> bool:
        """True si ninguna textura quedó sin procesar ni sin escribir. Una en revisión es una decisión, no un fallo."""
        return not any(a.estado in (EstadoDeArchivo.NO_PROCESADA, EstadoDeArchivo.NO_ESCRITA) for a in self.archivos)

    @property
    def conservadas(self) -> tuple[str, ...]:
        """Salidas anteriores que esta corrida dejó como estaban. Vacía si no se abrió la sesión (nada que escribir)."""
        return tuple(sorted(r.ruta for r in self.otras_rutas if r.efecto is EfectoDeSalida.OMITIDA))

    @property
    def retiradas(self) -> tuple[str, ...]:
        return tuple(sorted(r.ruta for r in self.otras_rutas if r.efecto is EfectoDeSalida.RETIRADA))

    def contar(self) -> dict[EstadoDeArchivo, int]:
        return {estado: sum(1 for a in self.archivos if a.estado is estado) for estado in EstadoDeArchivo}

    def a_diccionario(self) -> dict[str, Any]:
        def metricas(m: MetricasDeCalidad | None) -> dict[str, Any] | None:
            if m is None:
                return None
            return {
                "acuerdo_p95_grados": _numero_seguro(m.acuerdo_p95_grados),
                "signo": m.signo,
                "residuo_de_integrabilidad": _numero_seguro(m.residuo_de_integrabilidad),
                "razon_de_costura": _numero_seguro(m.razon_de_costura),
                "no_finitos": m.no_finitos,
            }

        return {
            "version_del_lote": VERSION_DEL_LOTE,
            "parametros": self.parametros.a_diccionario(),
            "encoder": None
            if self.encoder is None
            else {"texconv_version": self.encoder[0], "texconv_sha256": self.encoder[1]},
            "conteos": {estado.name: n for estado, n in self.contar().items()},
            "archivos": [
                {
                    "ruta_de_origen": a.ruta_de_origen,
                    "ruta_de_salida": a.ruta_de_salida,
                    "estado": a.estado.name,
                    "efecto": None if a.efecto is None else a.efecto.name,
                    "veredicto": None if a.veredicto is None else a.veredicto.name,
                    "motivos": list(a.motivos),
                    "metricas": metricas(a.metricas),
                    "detalle": a.detalle,
                }
                for a in self.archivos
            ],
            "otras_rutas": [{"ruta": r.ruta, "efecto": r.efecto.name, "detalle": r.detalle} for r in self.otras_rutas],
        }


# ---------------------------------------------------------------------------
# Fase 1: leer, integrar, medir, decidir (síncrono; corre en un hilo)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Medida:
    altura: NDArray[np.floating[Any]]
    algoritmo: str
    huella_de_la_normal: str
    metricas: MetricasDeCalidad
    decision: Decision


def _huella_acotada(ruta: pathlib.Path) -> str:
    """SHA-256 de un archivo regular, acotado por el tamaño con el que se lo validó (nunca más, aunque el archivo crezca).

    Valida el origen (regular y entre 1 byte y el tope) y delega la lectura en ``provider.huella_acotada``: es LA lectura
    acotada de las texturas de entrada, no una copia propia (``tests/test_native_parallax_producto_invariant.py`` enumera los
    lectores con huella del paquete). Que el archivo crezca O se achique entre la medida y la lectura es ``OrigenInvalidoError``.
    """
    try:
        info = os.lstat(ruta)
    except OSError as exc:
        raise OrigenInvalidoError(f"no se pudo inspeccionar el origen: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise OrigenInvalidoError(
            "el origen no es un archivo regular (un enlace, una carpeta u otra cosa no se procesa)"
        )
    if not 1 <= info.st_size <= TAMANO_MAXIMO_DEL_ARCHIVO:
        raise OrigenInvalidoError(
            f"el origen debe pesar entre 1 y {TAMANO_MAXIMO_DEL_ARCHIVO} bytes, no {info.st_size}"
        )
    try:
        return huella_acotada(ruta, info.st_size)
    except OSError as exc:
        raise OrigenInvalidoError(f"el origen {exc}") from exc


def _medir(ruta: pathlib.Path, parametros: ParametrosDelLote, decisor: Decisor) -> _Medida:
    antes = _huella_acotada(ruta)
    decodificador = NormalDecoder(source_convention=parametros.convencion)
    decodificada = decodificador.decode(ruta)
    if _huella_acotada(ruta) != antes:
        raise OrigenInvalidoError("el origen cambió mientras se procesaba: no se sabe qué bytes se decodificaron")
    normal = decodificada.normal
    # Ponderada: un píxel con nz ~ 0 (un borde que BC7 cuantizó bajo el horizonte) no decide la altura de toda la textura.
    integrada = integrar(
        normal,
        bordes=parametros.bordes,
        escala_x=parametros.escala_x,
        escala_y=parametros.escala_y,
        ponderacion=PONDERACION_DEL_PRODUCTO,
    )
    altura = normalizar_altura(
        integrada.altura,
        parametros.normalizacion,
        percentil_bajo=parametros.percentil_bajo,
        percentil_alto=parametros.percentil_alto,
    )
    acuerdo = acuerdo_normal(
        normal, integrada.altura, bordes=parametros.bordes, escala_x=parametros.escala_x, escala_y=parametros.escala_y
    )
    metricas = MetricasDeCalidad.desde_metricas(
        acuerdo,
        residuo_de_integrabilidad=residuo_de_integrabilidad(
            normal, bordes=parametros.bordes, escala_x=parametros.escala_x, escala_y=parametros.escala_y
        ),
        razon_de_costura=razon_de_costura(integrada.altura),
        no_finitos=contar_no_finitos(integrada.altura),
    )
    return _Medida(altura, integrada.algoritmo, antes, metricas, decisor(metricas))


def _entorno() -> tuple[tuple[str, str], ...]:
    """Las versiones que condicionan los bytes (ordenadas por nombre, como exige la procedencia)."""
    try:
        pares = {
            "numpy": importlib.metadata.version("numpy"),
            "pillow": importlib.metadata.version("pillow"),
            "plataforma": sys.platform,
            "python": platform.python_version(),
        }
    except importlib.metadata.PackageNotFoundError as exc:
        raise LoteError(
            f"no se pudo determinar la versión de una dependencia del extra native-parallax: {exc}"
        ) from exc
    return tuple(sorted(pares.items()))


# ---------------------------------------------------------------------------
# El lote
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Parcial:
    """El estado de una textura entre las dos fases."""

    entrada: NormalAProcesar
    estado: EstadoDeArchivo
    detalle: str = ""
    veredicto: Veredicto | None = None
    motivos: tuple[str, ...] = ()
    metricas: MetricasDeCalidad | None = None
    fuente: FuenteDeSalida | None = None
    efecto: EfectoDeSalida | None = None


def _exigir_entradas(normales: Sequence[NormalAProcesar]) -> None:
    vistas: dict[str, str] = {}
    for normal in normales:
        clave = clave_de_ruta(normal.ruta_de_salida)
        if clave in vistas:
            raise LoteInvalidoError(
                f"dos entradas apuntan a la misma ruta de salida ({vistas[clave]!r} y {normal.ruta_de_origen!r}): "
                "el proveedor debe resolver los duplicados antes"
            )
        vistas[clave] = normal.ruta_de_origen


async def _fase_de_calculo(
    normales: Sequence[NormalAProcesar],
    parametros: ParametrosDelLote,
    exportador: ExportadorDeAltura,
    decisor: Decisor,
    trabajo: pathlib.Path,
    progreso: Callable[[int, int, str], None] | None,
) -> tuple[list[_Parcial], tuple[str, str] | None]:
    parciales: list[_Parcial] = []
    encoder_visto: tuple[str, str] | None = None
    entorno = _entorno()
    texto_de_parametros = parametros_canonicos(parametros.a_parametros_de_procedencia())
    for indice, entrada in enumerate(normales):
        if progreso is not None:
            progreso(indice + 1, len(normales), entrada.ruta_de_origen)
        try:
            medida = await asyncio.to_thread(_medir, entrada.ruta_absoluta, parametros, decisor)
        except ERRORES_POR_ARCHIVO as exc:
            parciales.append(_Parcial(entrada, EstadoDeArchivo.NO_PROCESADA, f"{type(exc).__name__}: {exc}"))
            continue
        decision = medida.decision
        parcial = _Parcial(
            entrada,
            EstadoDeArchivo.EN_REVISION,
            veredicto=decision.veredicto,
            motivos=tuple(m.name for m in decision.motivos),
            metricas=medida.metricas,
        )
        if decision.veredicto is not Veredicto.AUTO_ACCEPT and not parametros.escribir_revision:
            parcial.detalle = "el gate la mandó a revisión y no se pidió escribir las salidas en revisión"
            parciales.append(parcial)
            continue
        try:
            exportado = await exportador(medida.altura)
            archivo = trabajo / f"{indice:05d}_p.dds"
            # Sin esperar al hilo, cancelar (Ctrl+C) dejaría que el borrado del trabajo corra contra el archivo abierto.
            await esperar_hilo_ininterrumpible(archivo.write_bytes, exportado.datos)
            procedencia = Procedencia(
                algoritmo_id=medida.algoritmo,
                parametros=texto_de_parametros,
                origenes=(OrigenDeEntrada(rol="normal", sha256=medida.huella_de_la_normal),),
                encoder=EncoderUsado(
                    exportador_id=EXPORTADOR_ID,
                    texconv_version=exportado.texconv_version,
                    texconv_sha256=exportado.texconv_sha256,
                    argumentos=tuple(exportado.argumentos),
                ),
                entorno=entorno,
                nivel=NivelDeReproducibilidad.DETERMINISTA_MISMO_ENTORNO,
            )
        except ERRORES_POR_ARCHIVO as exc:
            parcial.estado = EstadoDeArchivo.NO_PROCESADA
            parcial.detalle = f"{type(exc).__name__}: {exc}"
            parciales.append(parcial)
            continue
        encoder_visto = (exportado.texconv_version, exportado.texconv_sha256)
        parcial.estado = EstadoDeArchivo.ESCRITA  # provisional: la fase 2 dice qué hizo el escritor
        parcial.fuente = FuenteDeSalida(archivo=archivo, procedencia=procedencia)
        parciales.append(parcial)
    return parciales, encoder_visto


async def _fase_de_escritura(
    parciales: list[_Parcial],
    normales: Sequence[NormalAProcesar],
    parametros: ParametrosDelLote,
    raiz_de_salida: pathlib.Path,
    coordinacion: CoordinacionDeSalida,
) -> tuple[ResultadoDeRuta, ...]:
    generadas = {clave_de_ruta(p.entrada.ruta_de_salida): p for p in parciales if p.fuente is not None}
    if not generadas and not parametros.retirar_no_incluidas:
        return ()  # nada que escribir ni que retirar: no se abre la sesión ni se crea el mod de salida
    incluidas = {clave_de_ruta(n.ruta_de_salida) for n in normales}
    async with abrir_sesion_de_salida(raiz_de_salida, coordinacion=coordinacion) as sesion:
        deseadas = [
            SalidaDeseada(p.entrada.ruta_de_salida, clave_de_cache(p.fuente.procedencia))
            for p in generadas.values()
            if p.fuente is not None
        ]
        for previa in sesion.manifiesto.entradas:
            clave = clave_de_ruta(previa.ruta)
            if clave in generadas:
                continue
            if clave in incluidas or not parametros.retirar_no_incluidas:
                deseadas.append(
                    SalidaDeseada(previa.ruta, previa.clave_de_cache)
                )  # se conserva: su clave sigue vigente
        plan = await sesion.planificar(deseadas)
        pedidas = {
            clave_de_ruta(a.ruta)
            for a in plan.acciones
            if a.accion in (AccionDeSalida.CONSTRUIR, AccionDeSalida.RECONSTRUIR)
        }
        fuentes = {
            p.entrada.ruta_de_salida: p.fuente
            for clave, p in generadas.items()
            if p.fuente is not None and clave in pedidas
        }
        resultado = await sesion.aplicar(plan, fuentes)
    otras: list[ResultadoDeRuta] = []
    for ruta in resultado.rutas:
        parcial = generadas.get(clave_de_ruta(ruta.ruta))
        if parcial is None:
            otras.append(ResultadoDeRuta(ruta.ruta, ruta.efecto, ruta.detalle))
            continue
        parcial.efecto = ruta.efecto
        parcial.detalle = ruta.detalle
        parcial.estado = (
            EstadoDeArchivo.ESCRITA if ruta.efecto in _EFECTOS_QUE_DEJAN_LA_SALIDA else EstadoDeArchivo.NO_ESCRITA
        )
    return tuple(sorted(otras, key=lambda r: clave_de_ruta(r.ruta)))


def _borrar_trabajo(trabajo: pathlib.Path) -> None:
    """Borra el directorio de trabajo propio sin atravesar enlaces y SIN lanzar: corre en un ``finally``.

    Usa la primitiva del repo (``rmtree_link_aware``) y no ``shutil.rmtree``, que en Windows atraviesa un junction y
    borra su destino; que el árbol sea propio (``mkdtemp``) no habilita la excepción (``tests/test_borrado_recursivo.py``).
    No pide ``limpiar_readonly``: solo contiene lo que este proceso escribió con ``write_bytes``. Un fallo al borrar se
    registra y no enmascara el resultado de la corrida.
    """
    try:
        rmtree_link_aware(trabajo)
    except OSError as exc:
        logger.warning("no se pudo borrar por completo el directorio de trabajo %s: %s", trabajo, exc)


async def procesar_lote(
    normales: Iterable[NormalAProcesar],
    *,
    raiz_de_salida: pathlib.Path,
    coordinacion: CoordinacionDeSalida,
    exportador: ExportadorDeAltura,
    parametros: ParametrosDelLote | None = None,
    decisor: Decisor = decidir,
    directorio_de_trabajo: pathlib.Path | None = None,
    progreso: Callable[[int, int, str], None] | None = None,
) -> InformeDelLote:
    """Procesa *normales* y deja el resultado en la salida gestionada *raiz_de_salida* (ver el docstring del módulo).

    ``exportador``: recibe la altura normalizada a [0, 1] y devuelve los bytes del ``*_p.dds`` con la identidad de lo que
    los produjo. ``decisor``: el gate (por defecto el vigente; los tests y NP-X0 inyectan otro). ``directorio_de_trabajo``:
    dónde crear el espacio privado de las salidas intermedias (por defecto el temporal del sistema); se borra siempre.

    Raises:
        LoteInvalidoError: dos entradas apuntan a la misma ruta de salida (no se tocó nada).
        SalidaNoEscribibleError: la raíz de salida no es admisible (se comprueba ANTES de procesar ninguna textura; el escritor
            lo repite al abrir) o el plan no es válido.
        SalidaOcupadaError: otra sesión tiene el lock.
    """
    lista = list(normales)
    _exigir_entradas(lista)
    validar_raiz_de_salida(raiz_de_salida)  # una configuración mala se dice antes de gastar minutos y GiB por textura
    efectivos = parametros if parametros is not None else ParametrosDelLote()
    trabajo = pathlib.Path(tempfile.mkdtemp(prefix="skyclaw-parallax-", dir=directorio_de_trabajo))
    try:
        parciales, encoder = await _fase_de_calculo(lista, efectivos, exportador, decisor, trabajo, progreso)
        otras = await _fase_de_escritura(parciales, lista, efectivos, raiz_de_salida, coordinacion)
    finally:
        _borrar_trabajo(trabajo)
    archivos = tuple(
        ResultadoDeArchivo(
            ruta_de_origen=p.entrada.ruta_de_origen,
            ruta_de_salida=p.entrada.ruta_de_salida,
            estado=p.estado,
            efecto=p.efecto,
            veredicto=p.veredicto,
            motivos=p.motivos,
            metricas=p.metricas,
            detalle=p.detalle,
        )
        for p in parciales
    )
    return InformeDelLote(parametros=efectivos, archivos=archivos, otras_rutas=otras, encoder=encoder)

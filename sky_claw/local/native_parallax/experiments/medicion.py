"""Mide texturas con las funciones que ya existen y arma el corpus sintético medido (NP-X0b, ADR 0014 §6).

Es el lado con ``numpy`` de ``revision.py``: toma una normal (y la altura verdadera, si la hay), la integra con
``integrar``, calcula las métricas que el gate mira con ``metrics.py`` y devuelve una ``Medicion``. No agrega
matemática propia salvo el ``error_de_altura`` (RMSE entre las alturas normalizadas min-max), que es lo que el ground
truth permite y la producción no tiene. **Mide, no decide**: no hay umbrales acá.

Decisiones de definición (nuestras, ancladas por ``tests/test_native_parallax_x0_medicion.py``):

- **Una sola convención.** Las normales del corpus se generan DirectX (``escala_y = +1``, la referencia de NP-D0), así
  que se integran y se miden con ``escala_x = escala_y = 1``. Qué convención tiene un asset real NO se decide acá
  (NP-X0a: la fija la fuente del archivo).
- **Los bordes son un parámetro obligatorio** y viajan IGUALES a ``integrar`` y a las tres métricas que los usan: un
  cableado que fijara los bordes en una sola llamada mezclaría dos dominios en una misma medición.
- **El corpus se mide solo con bordes periódicos.** Los controles negativos (N01, N02) lo son porque violan la
  periodicidad: son del integrador periódico. Bajo Neumann son representables, y etiquetarlos «malos» corrompería las
  tasas; qué significa «mala» con otros bordes no está decidido, así que ``medir_corpus_sintetico`` se niega en vez de
  adivinarlo (``medir_textura`` sí es genérico: quien llama dice si la textura es un control).
- **Plano contra plano no tiene error** (vale 0); **plano contra relieve no tiene valor** (``None``): el relieve que
  aparece sobre una verdad plana (ruido integrado) o la altura plana que no recupera un relieve no se pueden comparar
  normalizados, y ``revision.es_mala`` los llama malos.
- **Los escenarios salen de la especificación** de NP-D0 (limpio, ruido gaussiano de cada sigma y cuantización de cada
  profundidad × modelo): cambiar la especificación cambia el benchmark, y la descripción del corpus medido lo registra.

Límite declarado: el corpus es sintético. Estas mediciones no validan ningún umbral (EXP-M2) y no incluyen compresión
por bloques (llega con NP-E0).

Dependencias: ``numpy`` pertenece al extra opcional ``native-parallax`` y se importa de forma perezosa por
``_extra.importar_numpy``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sky_claw.local.native_parallax._extra import importar_numpy
from sky_claw.local.native_parallax.bench.sintetico import (
    ESPECIFICACION_V1,
    IDS_CONTROLES,
    ModeloCuantizacion,
    cuantizar_normal,
    huella_especificacion,
    iterar_corpus,
    perturbar_normal,
)
from sky_claw.local.native_parallax.experiments.revision import Medicion, ParametroDeRevisionInvalidoError
from sky_claw.local.native_parallax.gates import VERSION_DE_METRICAS, MetricasDeCalidad
from sky_claw.local.native_parallax.integrate import (
    PONDERACION_DEL_PRODUCTO,
    AlturaPlanaError,
    Bordes,
    EntradaInvalidaError,
    ModoNormalizacion,
    integrar,
    normalizar_altura,
)
from sky_claw.local.native_parallax.metrics import (
    acuerdo_normal,
    contar_no_finitos,
    razon_de_costura,
    residuo_de_integrabilidad,
)

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "VERSION_DE_LA_MEDICION",
    "CorpusMedido",
    "Escenario",
    "escenarios_de_la_especificacion",
    "medir_corpus_sintetico",
    "medir_textura",
]

VERSION_DE_LA_MEDICION = "np-x0b.v2"
"""Definición de lo que se mide (métricas del gate y ``error_de_altura``); entra en la descripción del corpus medido.

v2: la altura sale de la integración ponderada (``PONDERACION_DEL_PRODUCTO``), la misma que publica el lote."""


@dataclass(frozen=True, slots=True)
class Escenario:
    """Una degradación de la normal: ``degradar(normal, caso_id)`` devuelve la normal degradada (de solo lectura)."""

    nombre: str
    degradar: Callable[[NDArray[np.float64], str], NDArray[np.float64]]


@dataclass(frozen=True, slots=True)
class CorpusMedido:
    """Las mediciones del corpus sintético, su huella y la descripción exacta de lo que se midió.

    ``huella`` es la de la ESPECIFICACIÓN de NP-D0 (``huella_especificacion(ESPECIFICACION_V1)``): el corpus medido es un
    subconjunto de ese corpus, y esa es la huella que ``gates.py`` rechaza como corpus de validación. Una huella propia
    del corpus medido habría sido un segundo hash sintético que el guard no conoce (Codex, #763). Lo que distingue una
    medición de otra (grilla, bordes, versiones, escenarios) va en ``descripcion``, un JSON canónico que NO es un hash
    y por lo tanto no se presta a usarlo como ``huella_del_corpus``.
    """

    mediciones: tuple[Medicion, ...]
    huella: str
    descripcion: str


def _sin_degradar(normal: NDArray[np.float64], caso_id: str) -> NDArray[np.float64]:
    return normal


def _con_ruido(sigma: float) -> Callable[[NDArray[np.float64], str], NDArray[np.float64]]:
    def degradar(normal: NDArray[np.float64], caso_id: str) -> NDArray[np.float64]:
        return perturbar_normal(normal, sigma, caso_id)

    return degradar


def _cuantizada(bits: int, modelo: ModeloCuantizacion) -> Callable[[NDArray[np.float64], str], NDArray[np.float64]]:
    def degradar(normal: NDArray[np.float64], caso_id: str) -> NDArray[np.float64]:
        return cuantizar_normal(normal, bits, modelo=modelo)

    return degradar


def escenarios_de_la_especificacion() -> tuple[Escenario, ...]:
    """``limpio``, un ruido por cada sigma y una cuantización por cada profundidad × modelo de ``ESPECIFICACION_V1``."""
    escenarios = [Escenario("limpio", _sin_degradar)]
    for sigma in ESPECIFICACION_V1.sigmas_ruido:
        escenarios.append(Escenario(f"ruido_{sigma * 255:g}_255", _con_ruido(sigma)))
    for bits in ESPECIFICACION_V1.bits_cuantizacion:
        for nombre_del_modelo in ESPECIFICACION_V1.modelos_cuantizacion:
            modelo = ModeloCuantizacion[nombre_del_modelo]
            escenarios.append(Escenario(f"cuantizacion_{bits}b_{modelo.value}", _cuantizada(bits, modelo)))
    return tuple(escenarios)


def _normalizada_o_none(altura: NDArray[np.float64]) -> NDArray[np.float64] | None:
    try:
        return normalizar_altura(altura, ModoNormalizacion.MIN_MAX)
    except AlturaPlanaError:
        return None


def _error_de_altura(altura_integrada: NDArray[np.float64], altura_verdadera: NDArray[np.float64]) -> float | None:
    """RMSE entre las alturas normalizadas min-max; 0 si las dos son planas, ``None`` si solo una lo es."""
    _np = importar_numpy()
    if altura_verdadera.shape != altura_integrada.shape:
        raise EntradaInvalidaError(
            f"altura_verdadera {altura_verdadera.shape} no tiene la forma de la normal {altura_integrada.shape}"
        )
    integrada = _normalizada_o_none(altura_integrada)
    verdadera = _normalizada_o_none(altura_verdadera)
    if integrada is None and verdadera is None:
        return 0.0
    if integrada is None or verdadera is None:
        return None
    return float(_np.sqrt(_np.mean((integrada - verdadera) ** 2)))


def medir_textura(
    normal: NDArray[np.float64],
    *,
    caso: str,
    escenario: str,
    es_control_negativo: bool,
    altura_verdadera: NDArray[np.float64],
    bordes: Bordes,
) -> Medicion:
    """Integra ``normal``, calcula las métricas del gate con los MISMOS ``bordes`` y el error contra la verdad.

    ``razon_de_costura`` no recibe bordes (``metrics.py`` la define sobre el salto de wrap de la altura): mide cuánto se
    vería la costura al TESELAR la altura, sea cual sea el integrador que la produjo. La ``Medicion`` declara los bordes
    con que se hizo para que no se mezclen mediciones de bordes distintos en una misma derivación.
    """
    # La MISMA integración que publica el lote: si no, los límites derivados medirían otro algoritmo.
    integrada = integrar(normal, bordes=bordes, ponderacion=PONDERACION_DEL_PRODUCTO)
    acuerdo = acuerdo_normal(normal, integrada.altura, bordes=bordes)
    metricas = MetricasDeCalidad.desde_metricas(
        acuerdo,
        residuo_de_integrabilidad=residuo_de_integrabilidad(normal, bordes=bordes),
        razon_de_costura=razon_de_costura(integrada.altura),
        no_finitos=contar_no_finitos(integrada.altura),
    )
    return Medicion(
        caso=caso,
        escenario=escenario,
        bordes=bordes,
        es_control_negativo=es_control_negativo,
        metricas=metricas,
        error_de_altura=_error_de_altura(integrada.altura, altura_verdadera),
    )


def _descripcion_del_corpus_medido(n_filas: int, n_cols: int, bordes: Bordes, escenarios: tuple[Escenario, ...]) -> str:
    """JSON canónico de lo que se midió y cómo (incluye la versión de las métricas y la de la medición)."""
    contenido = {
        "version_de_la_medicion": VERSION_DE_LA_MEDICION,
        "version_de_metricas": VERSION_DE_METRICAS,
        "especificacion": huella_especificacion(ESPECIFICACION_V1),
        "grilla": [n_filas, n_cols],
        "bordes": bordes.value,
        "convencion": "directx",
        "escenarios": [escenario.nombre for escenario in escenarios],
    }
    return json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def medir_corpus_sintetico(n_filas: int, n_cols: int, *, bordes: Bordes) -> CorpusMedido:
    """Mide cada caso de ``ESPECIFICACION_V1`` en cada escenario, en grilla ``(n_filas, n_cols)``.

    Solo con bordes periódicos (ver el docstring del módulo). La grilla y los bordes se validan ANTES de generar nada.
    Un caso que no se puede medir (una normal inválida) detiene el benchmark en vez de omitirse: un corpus medido con
    huecos silenciosos daría tasas de revisión engañosas.
    """
    if bordes is not Bordes.PERIODICOS:
        raise ParametroDeRevisionInvalidoError(
            "el corpus se mide solo con bordes periódicos (Bordes.PERIODICOS): los controles negativos lo son del "
            f"integrador periódico y qué significa «mala» con otros bordes no está decidido, no {bordes!r}"
        )
    elementos = iterar_corpus(n_filas, n_cols)
    escenarios = escenarios_de_la_especificacion()
    mediciones: list[Medicion] = []
    for elemento in elementos:
        es_control = elemento.caso.id in IDS_CONTROLES
        for escenario in escenarios:
            mediciones.append(
                medir_textura(
                    escenario.degradar(elemento.normal, elemento.caso.id),
                    caso=elemento.caso.id,
                    escenario=escenario.nombre,
                    es_control_negativo=es_control,
                    altura_verdadera=elemento.altura,
                    bordes=bordes,
                )
            )
    return CorpusMedido(
        mediciones=tuple(mediciones),
        huella=huella_especificacion(ESPECIFICACION_V1),
        descripcion=_descripcion_del_corpus_medido(n_filas, n_cols, bordes, escenarios),
    )

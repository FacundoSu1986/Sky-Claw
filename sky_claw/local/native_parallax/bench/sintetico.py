"""Corpus sintético del benchmark del generador nativo de parallax (NP-D0, ADR 0014 §2.5).

Genera superficies de altura con ground truth perfecto, su normal de referencia (H → N) y las perturbaciones
que degradan esa normal (ruido gaussiano y cuantización entera). Es la base de la que se derivan los
thresholds de aceptación (ADR 0014 §6: nunca se fijan a mano). Es PURO y determinista: no escribe, no
consulta el entorno, no lee archivos y no importa ``research`` (ADR 0014, decisión 2: lo probado en research
se reimplementa y ``tests/test_native_parallax_bench_sintetico.py`` lo compara como oráculo, caso por caso).

Convención de muestreo:

- Grilla ``(n_filas, n_cols)``: la fila ``i`` es ``y`` (hacia abajo) y la columna ``j`` es ``x``;
  ``x_j = j / n_cols`` e ``y_i = i / n_filas`` sobre el dominio ``[0, 1)²`` interpretado como toro.
- Los casos ``S*`` son matemáticamente periódicos en el toro; los controles ``N*`` violan la periodicidad
  a propósito (el integrador periódico debe detectarlos, no ocultarlos).
- ``float64``. Toda aleatoriedad usa ``numpy.random.default_rng`` con semilla fija en el código: ningún caso
  depende del estado global de numpy ni de ``hash()`` (que se randomiza por proceso).
- Los arrays devueltos son de solo lectura.

Lo que NO está acá y por qué:

- Compresión por bloques (BC): hace falta un codificador y el producto no tiene uno propio (ADR 0014,
  decisión 4: texconv externo, NP-E0). Las variantes con error de bloque llegan con NP-E0.
- El fixture MatSynth CC0 (ver ``bench/__init__.py``).
- La inversa N → H y el integrador: son de ``integrate.py`` (NP-I0), que comparte con este módulo la
  convención de frecuencias (``espectral.ejes_de_frecuencia``): una sola definición, congelada por un ancla
  de AST, porque un solver con otra daría gradientes escalados por la resolución (bug real de NP-M0).

Reproducibilidad: ``huella_especificacion`` fija la DEFINICIÓN del corpus (ids, clases, grillas, ruido y
bits). Las fórmulas las congela una tabla de huellas numéricas con tolerancia en el test, no un hash de los
bytes de ``float64``: ``np.sin``/``np.exp`` no garantizan los mismos bits entre CPUs y rutas SIMD (no
verificado entre plataformas), así que ese hash no sería un contrato.

Dependencias: ``numpy`` pertenece al extra opcional ``native-parallax`` y se importa de forma perezosa por
``_extra.importar_numpy``; sin él se lanza ``DependenciaNoDisponibleError`` (la misma clase del decodificador).
"""

from __future__ import annotations

import hashlib
import json
import math
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, cast

from sky_claw.local.native_parallax._extra import importar_numpy
from sky_claw.local.native_parallax.espectral import ejes_de_frecuencia

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "ESPECIFICACION_V1",
    "IDS_CONTROLES",
    "IDS_PERIODICOS",
    "TAMANO_MINIMO",
    "BenchError",
    "CasoDesconocidoError",
    "CasoSintetico",
    "ElementoCorpus",
    "EspecificacionDataset",
    "ParametroInvalidoError",
    "cuantizar_normal",
    "generar_altura",
    "huella_especificacion",
    "iterar_corpus",
    "normal_desde_altura",
    "perturbar_normal",
    "razon_borde",
    "semilla_estable",
]

# Con 32 ciclos en S11, 64 muestras serían exactamente Nyquist (el seno vale 0 en cada muestra). 128 deja
# esa frecuencia por debajo de Nyquist y es el piso de las grillas de la especificación.
TAMANO_MINIMO = 128

# Semillas fijas del corpus (mismas que research, para que el oráculo coincida bit a bit).
_SEMILLA_BULTOS = 7
_SEMILLA_RUIDO_PERIODICO = 20260921

_CLASE_PERIODICO = "periodico"
_CLASE_CONTROL = "control_negativo"


class BenchError(Exception):
    """Base de los errores del corpus sintético."""


class CasoDesconocidoError(BenchError):
    """El id no es uno de los casos de la especificación."""


class ParametroInvalidoError(BenchError, ValueError):
    """Dimensión, escala, sigma, bits o array fuera de contrato."""


# ---------------------------------------------------------------------------
# Superficies de altura. Cada generador recibe el módulo numpy ya importado.
# ---------------------------------------------------------------------------


def _malla(_np: ModuleType, n_filas: int, n_cols: int) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """``(xx, yy)`` de forma ``(n_filas, n_cols)``: ``xx`` varía por columnas, ``yy`` por filas."""
    y = _np.arange(n_filas, dtype=_np.float64) / n_filas
    x = _np.arange(n_cols, dtype=_np.float64) / n_cols
    xx, yy = _np.meshgrid(x, y, indexing="xy")
    return cast("tuple[NDArray[np.float64], NDArray[np.float64]]", (xx, yy))


def _distancia_toroidal(_np: ModuleType, u: NDArray[np.float64], c: float) -> NDArray[np.float64]:
    """Distancia a ``c`` en [0, 0.5] considerando el wrap del toro."""
    d = _np.abs(u - c) % 1.0  # en [0, 1) el módulo es neutro: se conserva por fidelidad con research
    return cast("NDArray[np.float64]", _np.minimum(d, 1.0 - d))


def _fraccion(_np: ModuleType, u: NDArray[np.float64]) -> NDArray[np.float64]:
    return cast("NDArray[np.float64]", _np.asarray(u - _np.floor(u), dtype=_np.float64))


def _s01_plano(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    return cast("NDArray[np.float64]", _np.zeros((n_filas, n_cols), dtype=_np.float64))


def _s02_seno_x(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, _ = _malla(_np, n_filas, n_cols)
    return cast("NDArray[np.float64]", 0.8 * _np.sin(2.0 * _np.pi * 4.0 * xx))


def _s03_seno_y(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    _, yy = _malla(_np, n_filas, n_cols)
    return cast("NDArray[np.float64]", 0.8 * _np.sin(2.0 * _np.pi * 4.0 * yy))


def _s04_separable(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    return cast(
        "NDArray[np.float64]",
        0.6 * _np.sin(2.0 * _np.pi * 3.0 * xx) + 0.4 * _np.sin(2.0 * _np.pi * 5.0 * yy),
    )


def _s05_diagonal(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    return cast("NDArray[np.float64]", _np.sin(2.0 * _np.pi * (3.0 * xx + 2.0 * yy)))


def _s06_multifrecuencia(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    return cast(
        "NDArray[np.float64]",
        0.45 * _np.sin(2.0 * _np.pi * xx + 0.3)
        + 0.30 * _np.sin(2.0 * _np.pi * 3.0 * yy + 1.1)
        + 0.15 * _np.sin(2.0 * _np.pi * (4.0 * xx + 2.0 * yy) + 2.0)
        + 0.10 * _np.sin(2.0 * _np.pi * (7.0 * xx + 5.0 * yy) + 0.7),
    )


def _s07_bultos(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    rng = _np.random.default_rng(_SEMILLA_BULTOS)
    h = _np.zeros((n_filas, n_cols), dtype=_np.float64)
    for _ in range(12):
        cx, cy = float(rng.uniform(0.0, 1.0)), float(rng.uniform(0.0, 1.0))
        sigma = float(rng.uniform(0.05, 0.12))
        amplitud = float(rng.uniform(0.3, 1.0))
        dx = _distancia_toroidal(_np, xx, cx)
        dy = _distancia_toroidal(_np, yy, cy)
        h += amplitud * _np.exp(-(dx * dx + dy * dy) / (2.0 * sigma * sigma))
    return cast("NDArray[np.float64]", h)


def _s08_crestas(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, _ = _malla(_np, n_filas, n_cols)
    s = _fraccion(_np, 6.0 * xx)
    return cast("NDArray[np.float64]", 1.0 - 2.0 * _np.abs(2.0 * s - 1.0))


def _s09_ladrillos(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    fila = _np.floor(3.0 * yy)
    fase_fila = 0.5 * (fila % 2.0)
    u = _fraccion(_np, (xx + fase_fila) * 3.0)
    dx_b = _np.minimum(u, 1.0 - u) / 3.0
    dy_b = _np.abs(_fraccion(_np, 3.0 * yy + 0.5) - 0.5) / 3.0
    sigma = 0.09
    return cast(
        "NDArray[np.float64]",
        _np.asarray(_np.exp(-(dx_b * dx_b + dy_b * dy_b) / (2.0 * sigma * sigma)), dtype=_np.float64),
    )


def _perfil_periodico(
    _np: ModuleType, u: NDArray[np.float64], c1: float, s1: float, c2: float, s2: float
) -> NDArray[np.float64]:
    """Perfil 1D periódico asimétrico: dos gaussianas toroidales de distinto peso."""
    return cast(
        "NDArray[np.float64]",
        _np.exp(-(_distancia_toroidal(_np, u, c1) ** 2) / (2.0 * s1 * s1))
        - 0.6 * _np.exp(-(_distancia_toroidal(_np, u, c2) ** 2) / (2.0 * s2 * s2)),
    )


def _s10_asimetrico(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    gx = _perfil_periodico(_np, _fraccion(_np, 4.0 * xx), 0.35, 0.10, 0.80, 0.15)
    gy = _perfil_periodico(_np, _fraccion(_np, 4.0 * yy), 0.20, 0.12, 0.70, 0.18)
    return 0.8 * gx + 0.4 * gy


def _s11_alta_frecuencia(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    return cast(
        "NDArray[np.float64]",
        0.5 * _np.sin(2.0 * _np.pi * 32.0 * xx) + 0.3 * _np.sin(2.0 * _np.pi * 21.0 * yy),
    )


def _s12_escalas_mezcladas(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    return cast(
        "NDArray[np.float64]",
        0.7 * _np.sin(2.0 * _np.pi * 1.0 * xx + 0.5)
        + 0.2 * _np.sin(2.0 * _np.pi * (8.0 * xx + 8.0 * yy))
        + 0.1 * _np.sin(2.0 * _np.pi * 24.0 * yy),
    )


def _s13_casi_plano(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    return 0.01 * _s06_multifrecuencia(_np, n_filas, n_cols)


def _s14_empinado(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    return cast(
        "NDArray[np.float64]",
        2.5 * _np.sin(2.0 * _np.pi * 6.0 * xx) + 2.0 * _np.cos(2.0 * _np.pi * 5.0 * yy),
    )


def _s15_ruido_periodico(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    rng = _np.random.default_rng(_SEMILLA_RUIDO_PERIODICO)
    h = _np.zeros((n_filas, n_cols), dtype=_np.float64)
    for _ in range(96):
        kx = int(rng.integers(-24, 25))
        ky = int(rng.integers(-24, 25))
        if kx == 0 and ky == 0:
            continue
        amplitud = float(rng.uniform(0.02, 0.15)) / _np.sqrt(kx * kx + ky * ky + 1.0)
        fase = float(rng.uniform(0.0, 2.0 * _np.pi))
        h += amplitud * _np.sin(2.0 * _np.pi * (kx * xx + ky * yy) + fase)
    return cast("NDArray[np.float64]", h)


def _n01_rampa(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, _ = _malla(_np, n_filas, n_cols)
    return xx - 0.5


def _n02_bulto_en_el_borde(_np: ModuleType, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    xx, yy = _malla(_np, n_filas, n_cols)
    sigma = 0.15
    return cast(
        "NDArray[np.float64]",
        _np.exp(-((xx - 0.15) ** 2 + (yy - 0.5) ** 2) / (2.0 * sigma * sigma)),
    )


@dataclass(frozen=True, slots=True)
class CasoSintetico:
    """Un caso del corpus: ``id`` estable, ``clase`` (``periodico`` o ``control_negativo``) y su descripción."""

    id: str
    clase: str
    descripcion: str


# Única fuente de verdad del corpus: de esta tabla salen los ids, la especificación y el despacho. Agregar
# un caso acá sin tocar el test literal de ids lo rompe a propósito.
_TABLA: tuple[tuple[CasoSintetico, Callable[[ModuleType, int, int], NDArray[np.float64]]], ...] = (
    (CasoSintetico("S01_flat", _CLASE_PERIODICO, "plano exacto: la invariante 'plano da plano'"), _s01_plano),
    (
        CasoSintetico("S02_sine_x", _CLASE_PERIODICO, "seno único en x, 4 ciclos: caso analítico de referencia"),
        _s02_seno_x,
    ),
    (CasoSintetico("S03_sine_y", _CLASE_PERIODICO, "seno único en y, 4 ciclos: ejercita el eje de filas"), _s03_seno_y),
    (CasoSintetico("S04_separable", _CLASE_PERIODICO, "suma separable de 3 ciclos en x y 5 en y"), _s04_separable),
    (
        CasoSintetico("S05_diagonal", _CLASE_PERIODICO, "sinusoide diagonal (3, 2): energía fuera de los ejes"),
        _s05_diagonal,
    ),
    (
        CasoSintetico("S06_multifreq", _CLASE_PERIODICO, "cuatro sinusoides de frecuencia entera y fase distinta"),
        _s06_multifrecuencia,
    ),
    (CasoSintetico("S07_bumps", _CLASE_PERIODICO, "12 gaussianas toroidales con semilla fija"), _s07_bultos),
    (
        CasoSintetico(
            "S08_ridges", _CLASE_PERIODICO, "crestas triangulares en x, 6 períodos: con pliegues, no band-limited"
        ),
        _s08_crestas,
    ),
    (
        CasoSintetico("S09_bricks", _CLASE_PERIODICO, "ladrillo y mortero, 3x3 con juntas desfasadas por fila"),
        _s09_ladrillos,
    ),
    (
        CasoSintetico(
            "S10_asymmetric", _CLASE_PERIODICO, "relieve asimétrico repetido 4x4: sensible a la inversión de signo"
        ),
        _s10_asimetrico,
    ),
    (
        CasoSintetico("S11_highfreq", _CLASE_PERIODICO, "alta frecuencia válida: 32 ciclos en x y 21 en y"),
        _s11_alta_frecuencia,
    ),
    (
        CasoSintetico("S12_mixed_scale", _CLASE_PERIODICO, "mezcla de baja, media y alta frecuencia"),
        _s12_escalas_mezcladas,
    ),
    (
        CasoSintetico("S13_near_flat", _CLASE_PERIODICO, "la forma de S06 con amplitud 0.01: rango dinámico mínimo"),
        _s13_casi_plano,
    ),
    (CasoSintetico("S14_steep", _CLASE_PERIODICO, "deliberadamente empinado: nz mínimo cercano a 1e-2"), _s14_empinado),
    (
        CasoSintetico("S15_periodic_noise", _CLASE_PERIODICO, "96 sinusoides de frecuencia entera con semilla fija"),
        _s15_ruido_periodico,
    ),
    (CasoSintetico("N01_ramp", _CLASE_CONTROL, "rampa lineal: no representable en el toro"), _n01_rampa),
    (
        CasoSintetico(
            "N02_bump_boundary", _CLASE_CONTROL, "gaussiana sin wrap junto al borde: salta al cerrar el toro"
        ),
        _n02_bulto_en_el_borde,
    ),
)

_GENERADORES: dict[str, Callable[[ModuleType, int, int], NDArray[np.float64]]] = {caso.id: gen for caso, gen in _TABLA}

IDS_PERIODICOS: tuple[str, ...] = tuple(caso.id for caso, _ in _TABLA if caso.clase == _CLASE_PERIODICO)
IDS_CONTROLES: tuple[str, ...] = tuple(caso.id for caso, _ in _TABLA if caso.clase == _CLASE_CONTROL)


@dataclass(frozen=True, slots=True)
class EspecificacionDataset:
    """Definición inmutable y versionada del corpus.

    ``resoluciones``: grillas ``(n_filas, n_cols)``, incluidas las rectangulares en los dos sentidos.
    ``sigmas_ruido``: desvíos del ruido gaussiano sobre la normal (en unidades de la normal, ``k / 255``).
    ``bits_cuantizacion``: profundidades de la cuantización entera de la normal.
    """

    version: str
    casos: tuple[CasoSintetico, ...]
    resoluciones: tuple[tuple[int, int], ...]
    sigmas_ruido: tuple[float, ...]
    bits_cuantizacion: tuple[int, ...]


ESPECIFICACION_V1 = EspecificacionDataset(
    version="np-d0.v1",
    casos=tuple(caso for caso, _ in _TABLA),
    resoluciones=((128, 128), (256, 256), (512, 512), (128, 256), (256, 128)),
    sigmas_ruido=(1 / 255, 2 / 255, 4 / 255, 8 / 255),
    bits_cuantizacion=(8, 10, 16),
)


def huella_especificacion(especificacion: EspecificacionDataset) -> str:
    """SHA-256 de la definición del corpus (versión, ids, clases, grillas, ruido y bits).

    La descripción NO entra: corregir un typo no debe invalidar el corpus. Las fórmulas tampoco: las
    congela la tabla de huellas numéricas del test (ver el docstring del módulo).
    """
    contenido = {
        "version": especificacion.version,
        "casos": [[caso.id, caso.clase] for caso in especificacion.casos],
        "resoluciones": [list(grilla) for grilla in especificacion.resoluciones],
        "sigmas_ruido": list(especificacion.sigmas_ruido),
        "bits_cuantizacion": list(especificacion.bits_cuantizacion),
    }
    texto = json.dumps(contenido, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(texto.encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# Validación de entradas
# ---------------------------------------------------------------------------


def _es_entero(valor: object) -> bool:
    # ``bool`` es subclase de ``int``, pero True/False (1/0) nunca llegan a ser una dimensión >= TAMANO_MINIMO ni
    # una profundidad de la especificación: los rechazan las comparaciones que siguen, sin excluirlos acá.
    return isinstance(valor, int)


def _es_real_finito(valor: object) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _exigir_grilla(n_filas: object, n_cols: object) -> None:
    for nombre, valor in (("n_filas", n_filas), ("n_cols", n_cols)):
        if not _es_entero(valor) or cast("int", valor) < TAMANO_MINIMO:
            raise ParametroInvalidoError(f"{nombre} debe ser un entero >= {TAMANO_MINIMO}, no {valor!r}")


def _exigir_caso(caso_id: object) -> str:
    if not isinstance(caso_id, str) or caso_id not in _GENERADORES:
        raise CasoDesconocidoError(f"caso sintético desconocido: {caso_id!r}")
    return caso_id


def _exigir_campo_2d(_np: ModuleType, valor: object, nombre: str) -> NDArray[np.float64]:
    if not isinstance(valor, _np.ndarray) or valor.dtype != _np.float64 or valor.ndim != 2 or min(valor.shape) < 2:
        raise ParametroInvalidoError(f"{nombre} debe ser un ndarray float64 2D de al menos 2x2")
    if not bool(_np.all(_np.isfinite(valor))):
        raise ParametroInvalidoError(f"{nombre} tiene valores no finitos")
    return cast("NDArray[np.float64]", valor)


def _exigir_normal(_np: ModuleType, valor: object) -> NDArray[np.float64]:
    if not isinstance(valor, _np.ndarray) or valor.dtype != _np.float64 or valor.ndim != 3 or valor.shape[-1] != 3:
        raise ParametroInvalidoError("normal debe ser un ndarray float64 de forma (alto, ancho, 3)")
    if not bool(_np.all(_np.isfinite(valor))):
        raise ParametroInvalidoError("normal tiene valores no finitos")
    return cast("NDArray[np.float64]", valor)


def _congelar(_np: ModuleType, arreglo: NDArray[np.float64]) -> NDArray[np.float64]:
    arreglo.setflags(write=False)
    return arreglo


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def generar_altura(caso_id: str, n_filas: int, n_cols: int) -> NDArray[np.float64]:
    """Superficie de altura ``float64`` de forma ``(n_filas, n_cols)``, de solo lectura."""
    clave = _exigir_caso(caso_id)
    _exigir_grilla(n_filas, n_cols)
    _np = importar_numpy()
    return _congelar(_np, _GENERADORES[clave](_np, n_filas, n_cols))


def razon_borde(altura: NDArray[np.float64]) -> float:
    """Consistencia de frontera del ground truth: ``mean(|paso de wrap|) / mean(|paso interior|)`` por eje.

    Una superficie muestreada de un campo suave del toro da una razón cercana a 1 (se acepta [0.2, 5] por
    el contenido con pliegues de S08/S09); una no periódica da una razón mucho mayor (rampa) o localizada
    (N02). Devuelve el máximo de ambos ejes. Un eje constante cuenta como periódico (razón 1).
    """
    _np = importar_numpy()
    campo = _exigir_campo_2d(_np, altura, "altura")
    razones: list[float] = []
    for eje in (0, 1):
        salto = _np.abs(_np.take(campo, 0, axis=eje) - _np.take(campo, -1, axis=eje))
        interior = _np.abs(_np.take(campo, 1, axis=eje) - _np.take(campo, 0, axis=eje))
        if float(interior.mean()) < 1e-12:
            razones.append(1.0)
            continue
        razones.append(float(salto.mean()) / float(interior.mean()))
    return max(razones)


def normal_desde_altura(
    altura: NDArray[np.float64], escala_x: float = 1.0, escala_y: float = 1.0
) -> NDArray[np.float64]:
    """Normal de referencia ``(alto, ancho, 3)`` unitaria, de solo lectura: ``N = normalize(-sx·p, -sy·q, 1)``.

    ``(p, q) = (∂h/∂x, ∂h/∂y)`` son las derivadas espectrales periódicas. Para superficies band-limited son
    las exactas; con pliegues (S08/S09) son las de la proyección band-limited, que es lo que reconstruye el
    integrador periódico. Los signos ``escala_x``/``escala_y`` parametrizan la convención: este módulo no
    declara cuál es la de Skyrim (eso es de NP-X0).
    """
    _np = importar_numpy()
    campo = _exigir_campo_2d(_np, altura, "altura")
    for nombre, escala in (("escala_x", escala_x), ("escala_y", escala_y)):
        if not _es_real_finito(escala):
            raise ParametroInvalidoError(f"{nombre} debe ser un real finito, no {escala!r}")
    n_filas, n_cols = campo.shape
    wy, wx = ejes_de_frecuencia(_np, n_filas, n_cols)
    h_hat = _np.fft.fft2(campo)
    p = _np.real(_np.fft.ifft2(1j * wx * h_hat))
    q = _np.real(_np.fft.ifft2(1j * wy * h_hat))
    nx = -escala_x * p
    ny = -escala_y * q
    nz = _np.ones_like(p)
    norma = _np.sqrt(nx * nx + ny * ny + 1.0)
    normal = _np.stack((nx / norma, ny / norma, nz / norma), axis=-1)
    return _congelar(_np, cast("NDArray[np.float64]", normal))


def semilla_estable(caso_id: str, etiqueta: str) -> int:
    """Semilla determinista por (caso, perturbación): CRC32, no ``hash()``.

    ``hash()`` de ``str`` se randomiza por proceso (``PYTHONHASHSEED``) y rompería la reproducibilidad.
    """
    return zlib.crc32(f"{caso_id}|{etiqueta}".encode()) & 0x7FFFFFFF


def perturbar_normal(normal: NDArray[np.float64], sigma: float, caso_id: str) -> NDArray[np.float64]:
    """Normal con ruido gaussiano de desvío ``sigma`` por componente, renormalizada; de solo lectura.

    La semilla sale de ``semilla_estable(caso_id, f"noise{sigma:.6f}")``: misma entrada, mismo ruido, en
    cualquier proceso. No modifica ``normal``.
    """
    _np = importar_numpy()
    base = _exigir_normal(_np, normal)
    if not _es_real_finito(sigma) or sigma <= 0:
        raise ParametroInvalidoError(f"sigma debe ser un real finito > 0, no {sigma!r}")
    if not isinstance(caso_id, str):
        raise ParametroInvalidoError(f"caso_id debe ser texto, no {caso_id!r}")
    rng = _np.random.default_rng(semilla_estable(caso_id, f"noise{sigma:.6f}"))
    ruidoso = base + rng.normal(0.0, sigma, size=base.shape)
    resultado = ruidoso / _np.linalg.norm(ruidoso, axis=-1, keepdims=True)
    return _congelar(_np, cast("NDArray[np.float64]", resultado))


def cuantizar_normal(normal: NDArray[np.float64], bits: int) -> NDArray[np.float64]:
    """Simula el almacenamiento entero de la normal (sin DDS): codifica a ``bits`` bits, decodifica y renormaliza.

    Solo se admiten las profundidades de la especificación (8, 10 y 16). Q8 es el caso relevante para
    texturas de 8 bits; Q10 y Q16 son controles.
    """
    _np = importar_numpy()
    base = _exigir_normal(_np, normal)
    if not _es_entero(bits) or bits not in ESPECIFICACION_V1.bits_cuantizacion:
        raise ParametroInvalidoError(f"bits debe ser uno de {ESPECIFICACION_V1.bits_cuantizacion}, no {bits!r}")
    niveles = float(2**bits - 1)
    q01 = _np.rint((base + 1.0) * 0.5 * niveles) / niveles
    decodificada = q01 * 2.0 - 1.0
    resultado = decodificada / _np.linalg.norm(decodificada, axis=-1, keepdims=True)
    return _congelar(_np, cast("NDArray[np.float64]", _np.asarray(resultado, dtype=_np.float64)))


@dataclass(frozen=True, slots=True)
class ElementoCorpus:
    """Un caso generado: su definición, la altura y la normal de referencia."""

    caso: CasoSintetico
    altura: NDArray[np.float64]
    normal: NDArray[np.float64]


def iterar_corpus(n_filas: int, n_cols: int) -> Iterator[ElementoCorpus]:
    """Recorre los casos de ``ESPECIFICACION_V1`` en orden. La grilla se valida ANTES de generar nada."""
    _exigir_grilla(n_filas, n_cols)
    return _recorrer(n_filas, n_cols)


def _recorrer(n_filas: int, n_cols: int) -> Iterator[ElementoCorpus]:
    for caso in ESPECIFICACION_V1.casos:
        altura = generar_altura(caso.id, n_filas, n_cols)
        yield ElementoCorpus(caso=caso, altura=altura, normal=normal_desde_altura(altura))

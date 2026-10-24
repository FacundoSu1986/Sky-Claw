"""Integrador normal → altura del generador nativo de parallax (NP-I0, ADR 0014 §2.5).

Dado un campo de normales unitarias (convención DirectX, ver ``decode.py``) reconstruye la altura de media cero
cuyo gradiente las produce, en unidades de TEXTURA (la textura mide 1 en x y en y; no por muestra). Es PURO: no
escribe, no consulta el entorno y no importa ``research`` (ADR 0014, decisión 2: lo probado en research se
reimplementa y ``tests/test_native_parallax_integrate.py`` lo compara como oráculo).

Cadena: normal → gradientes (p, q) = (∂h/∂x, ∂h/∂y) → ecuación de Poisson ``Δh = ∂p/∂x + ∂q/∂y`` resuelta por
FFT (Frankot–Chellappa 1988; Quéau, Durou y Aujol 2017 §3.3). La inversa DIVIDE por la escala:

    p = -nx / (sx · nz)        q = -ny / (sy · nz)

(PR-MATH-A §8; la forma ``-sx · nx / nz`` solo coincide con ella cuando ``sx² = 1`` y ocultó el error en NP-M0).

Variantes de borde (``Bordes``):

- ``PERIODICOS`` (``normal_fft_periodic_v1``): la textura es un toro. Es la de los mosaicos, que son la mayoría de
  las texturas de Skyrim.
- ``NEUMANN`` (``normal_fft_neumann_v1``): derivada normal nula en los cuatro bordes, para texturas que NO
  mosaican. Se resuelve extendiendo el gradiente por espejo a ``2H × 2W`` y usando el mismo solver periódico
  (equivale a la transformada coseno, sin dependencias nuevas). ASUME la hipótesis de borde: si la altura real
  tiene pendiente en el borde no la recupera, y este módulo no lo corrige ni lo adivina.

La media de la altura NO es observable desde sus gradientes: la salida tiene media cero.

El piso de ``nz`` (``nz_piso``) es una guarda NUMÉRICA contra ``1/0``, no una política de confianza: los píxeles
por debajo se CUENTAN y se devuelven (``nz_bajo_piso``), la singularidad no se esconde. Las políticas de
confianza (REVIEW/REJECT, regularización de Tikhonov de EXP-M1) esperan a los thresholds derivados del benchmark
(ADR 0014 §6) y no están acá.

Ponderación (``Ponderacion``):

- ``UNIFORME`` (por defecto): Frankot–Chellappa tal cual, el solver que se compara con research como oráculo. Cada
  píxel pesa lo mismo, así que un píxel con ``nz → 0`` aporta un gradiente ~``1/nz`` SIN cota y lo reparte como un
  dipolo sobre toda la textura. Medido en el rig (2026-10-24, ``wrcitywall01_n.dds`` de Skyland AIO, BC7 2048²):
  7 píxeles de 4,2 millones con ``nz < 0`` dejaban la altura en dos dipolos sin rastro de las piedras (acuerdo p95
  44.8°, correlación con el difuso −0.015).
- ``NZ`` (la del producto, ``PONDERACION_DEL_PRODUCTO``): mínimos cuadrados ponderados por ``nz``,
  ``min Σ w · (sx² (∂h/∂x − p)² + sy² (∂h/∂y − q)²)``. Donde el gradiente está definido (``nz >= nz_piso``) ``w = nz``
  y, como ``nz · p = −nx / sx``, el aporte de cada píxel al sistema queda acotado por su ``nx``: ningún píxel puede
  decidir la altura del resto. ``nz`` es la MENOR potencia que logra esa cota; con ``nz²`` (la ponderación con que
  ``metrics.acuerdo_normal`` ajusta la escala) la solución es casi la misma (wrcitywall01: p95 10.0° contra 10.2°) pero
  el sistema queda peor condicionado (corpus sintético de NP-D0 a 128², 187 normales: hasta 815 iteraciones contra
  117). Donde NO está definido (``nz`` bajo el piso: bajo el horizonte, o BC5 fuera del disco unidad) el píxel pide
  pendiente nula con el peso medio de los definidos: un relleno suave en vez de una región de peso cero, que deja al
  sistema casi singular (sin el relleno, 3 de esas 187 normales no convergían en 3000 iteraciones). En un campo
  integrable y todo definido la altura es la MISMA que la uniforme (cualquier peso positivo tiene el mismo mínimo).
  Se resuelve ``Dᵀ W D h = Dᵀ W g`` con gradiente conjugado precondicionado por el Poisson uniforme de peso medio
  (mínimos cuadrados ponderados y dominios con datos faltantes: Quéau, Durou y Aujol 2018, «Normal Integration: A
  Survey», J. Math. Imaging Vis. 60; el precondicionador espectral es el solver uniforme). Sobre wrcitywall01:
  acuerdo p95 10.2°, correlación con el difuso +0.18, 24 iteraciones y ~9 s.

Normalización (``normalizar_altura``): lleva la altura a [0, 1] por rango completo o por percentiles. Los
percentiles NO tienen valor por defecto: el brief sugiere p2…p98 como intención de authoring, no como
consecuencia de la matemática (ADR 0014 §4), y un número fijado a mano en el código es justo lo que el ADR §6
prohíbe. El que llama los elige.

Límite declarado: las pruebas usan normales sintéticas y limpias. Cómo se comporta con ruido, cuantización o
normales reales (EXP-001b) está fuera de este módulo.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, cast

from sky_claw.local.native_parallax._extra import importar_numpy
from sky_claw.local.native_parallax.espectral import ejes_de_frecuencia

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "ALGORITMO_NEUMANN",
    "ALGORITMO_PERIODICO",
    "ALGORITMO_PONDERADO_NEUMANN",
    "ALGORITMO_PONDERADO_PERIODICO",
    "MAX_ITERACIONES",
    "NZ_PISO_NUMERICO",
    "PONDERACION_DEL_PRODUCTO",
    "TOLERANCIA_RELATIVA",
    "AlturaIntegrada",
    "AlturaPlanaError",
    "Bordes",
    "EntradaInvalidaError",
    "GradientesDeNormal",
    "IntegracionError",
    "ModoNormalizacion",
    "Ponderacion",
    "SinConvergenciaError",
    "gradientes_de_normal",
    "integrar",
    "normalizar_altura",
]

ALGORITMO_PERIODICO = "normal_fft_periodic_v1"
ALGORITMO_NEUMANN = "normal_fft_neumann_v1"
ALGORITMO_PONDERADO_PERIODICO = "normal_wls_nz_periodic_v1"
ALGORITMO_PONDERADO_NEUMANN = "normal_wls_nz_neumann_v1"

# Guardas NUMÉRICAS del gradiente conjugado (no son umbrales de calidad). Medido sobre wrcitywall01 (2048², BC7 real):
# con 1e-8 la altura normalizada difiere en 9e-9 de la de 1e-12 (24 contra 36 iteraciones), muy por debajo del paso
# de 8 bits del canal (1/255), y una normal sintética limpia se recupera con acuerdo p95 < 1e-6°. En el corpus
# sintético de NP-D0 (187 normales a 128², ruido y cuantización incluidos) el máximo fue 117 iteraciones: el tope
# deja más de 4× de margen.
TOLERANCIA_RELATIVA = 1e-8
MAX_ITERACIONES = 500

# Guarda numérica contra 1/0 (mismo valor que NP-M0 y PR-MATH-A); NO es un umbral de aceptación.
NZ_PISO_NUMERICO = 1e-6


class IntegracionError(Exception):
    """Base de los errores del integrador."""


class EntradaInvalidaError(IntegracionError, ValueError):
    """Array, escala, piso, borde, modo o percentil fuera de contrato."""


class AlturaPlanaError(IntegracionError):
    """La altura no tiene rango: normalizarla sería dividir por cero o inventar contraste."""


class SinConvergenciaError(IntegracionError):
    """El gradiente conjugado de la ponderada no llegó a ``TOLERANCIA_RELATIVA`` en ``MAX_ITERACIONES``."""


class Bordes(enum.Enum):
    """Hipótesis de borde de la textura."""

    PERIODICOS = "periodicos"
    NEUMANN = "neumann"


class Ponderacion(enum.Enum):
    """Cuánto pesa cada píxel en los mínimos cuadrados (ver el docstring del módulo)."""

    UNIFORME = "uniforme"
    NZ = "nz"


# La que usan TODOS los llamadores de producto (lote y medición; ancla por AST en
# ``tests/test_native_parallax_normales_fuera_de_dominio.py``): cambiarla es cambiarla para los dos a la vez.
PONDERACION_DEL_PRODUCTO = Ponderacion.NZ


class ModoNormalizacion(enum.Enum):
    """Cómo llevar la altura a [0, 1]."""

    MIN_MAX = "min_max"
    PERCENTILES = "percentiles"


@dataclass(frozen=True, slots=True)
class AlturaIntegrada:
    """Resultado inmutable de ``integrar`` (el array es de solo lectura).

    ``altura``: (alto, ancho) float64 de media cero, en unidades de textura.
    ``algoritmo``: ``ALGORITMO_PERIODICO``/``ALGORITMO_NEUMANN`` (uniforme) o ``ALGORITMO_PONDERADO_*``.
    ``nz_bajo_piso``: píxeles con ``nz < nz_piso`` (uniforme: su gradiente se calculó con el piso; ponderada: se
    rellenan con pendiente nula).
    ``nz_minimo``: el ``nz`` más bajo de la entrada (puede ser negativo).
    ``residuo_imaginario_max``: parte imaginaria máxima tras la FFT inversa; debe ser del orden del redondeo (la
    ponderada trabaja con FFT reales y es 0).
    ``iteraciones``: iteraciones del gradiente conjugado (0 en la uniforme, que es directa).
    """

    altura: NDArray[np.float64]
    algoritmo: str
    nz_bajo_piso: int
    nz_minimo: float
    residuo_imaginario_max: float
    iteraciones: int = 0


@dataclass(frozen=True, slots=True)
class GradientesDeNormal:
    """Inversa normal → gradientes, de solo lectura: ``p = ∂h/∂x`` y ``q = ∂h/∂y`` (alto, ancho) float64.

    ``nz_bajo_piso``: píxeles con ``nz < nz_piso`` (su gradiente se calculó con el piso).
    ``nz_minimo``: el ``nz`` más bajo de la entrada (puede ser negativo).
    """

    p: NDArray[np.float64]
    q: NDArray[np.float64]
    nz_bajo_piso: int
    nz_minimo: float


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------


def _es_real_finito(valor: object) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _exigir_normal(_np: ModuleType, valor: object) -> NDArray[np.float64]:
    if (
        not isinstance(valor, _np.ndarray)
        or valor.dtype != _np.float64
        or valor.ndim != 3
        or valor.shape[-1] != 3
        or min(valor.shape[:2]) < 2
    ):
        raise EntradaInvalidaError("normal debe ser un ndarray float64 de forma (alto >= 2, ancho >= 2, 3)")
    if not bool(_np.all(_np.isfinite(valor))):
        raise EntradaInvalidaError("normal tiene valores no finitos")
    return cast("NDArray[np.float64]", valor)


def _exigir_ejes_representables(forma: tuple[int, ...], bordes: Bordes) -> None:
    # El solver resuelve sobre el toro: con 2 muestras el único bin no continuo es Nyquist, cuya derivada es nula
    # por convención, así que la altura saldría plana SIN avisar. Neumann resuelve sobre 2n muestras, donde 2 sí
    # alcanzan: el umbral sigue al largo de la grilla que se resuelve, no al de la entrada.
    if bordes is Bordes.PERIODICOS and min(forma[:2]) < 3:
        raise EntradaInvalidaError(
            f"con bordes periódicos cada eje necesita al menos 3 muestras (con 2 solo queda Nyquist, de derivada nula), "
            f"no {forma[0]}x{forma[1]}"
        )


def _exigir_campo(_np: ModuleType, valor: object, nombre: str) -> NDArray[np.float64]:
    if not isinstance(valor, _np.ndarray) or valor.dtype != _np.float64 or valor.ndim != 2 or min(valor.shape) < 2:
        raise EntradaInvalidaError(f"{nombre} debe ser un ndarray float64 2D de al menos 2x2")
    if not bool(_np.all(_np.isfinite(valor))):
        raise EntradaInvalidaError(f"{nombre} tiene valores no finitos")
    return cast("NDArray[np.float64]", valor)


def _exigir_escala(nombre: str, valor: object) -> float:
    # sx = 0 haría indefinida la inversa p = -nx / (sx · nz): se rechaza y no se inventa un épsilon.
    if not _es_real_finito(valor) or valor == 0:
        raise EntradaInvalidaError(f"{nombre} debe ser un real finito distinto de cero, no {valor!r}")
    return float(cast("float", valor))


def _exigir_piso(valor: object) -> float:
    if not _es_real_finito(valor) or not 0.0 < cast("float", valor) <= 1.0:
        raise EntradaInvalidaError(f"nz_piso debe ser un real en (0, 1], no {valor!r}")
    return float(cast("float", valor))


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


def _invertir_normal(
    _np: ModuleType, campo: NDArray[np.float64], sx: float, sy: float, piso: float
) -> tuple[NDArray[np.float64], NDArray[np.float64], int, float]:
    """``p = -nx / (sx · nz)`` y ``q = -ny / (sy · nz)`` con ``nz`` llevado al piso: la ÚNICA copia de la inversa."""
    nx, ny, nz = campo[..., 0], campo[..., 1], campo[..., 2]
    nz_efectivo = _np.maximum(nz, piso)
    bajo_piso = int(_np.count_nonzero(nz < piso))
    p = -nx / (sx * nz_efectivo)
    q = -ny / (sy * nz_efectivo)
    return p, q, bajo_piso, float(nz.min())


def gradientes_de_normal(
    normal: NDArray[np.float64],
    *,
    escala_x: float = 1.0,
    escala_y: float = 1.0,
    nz_piso: float = NZ_PISO_NUMERICO,
) -> GradientesDeNormal:
    """Gradientes ``(p, q)`` que producen ``normal`` (``N = normalize(-sx·p, -sy·q, 1)``), de solo lectura.

    Es la inversa que usa ``integrar``; las métricas de calidad la reutilizan para no tener una segunda copia.
    """
    _np = importar_numpy()
    campo = _exigir_normal(_np, normal)
    sx = _exigir_escala("escala_x", escala_x)
    sy = _exigir_escala("escala_y", escala_y)
    piso = _exigir_piso(nz_piso)
    p, q, bajo_piso, nz_minimo = _invertir_normal(_np, campo, sx, sy, piso)
    p.setflags(write=False)
    q.setflags(write=False)
    return GradientesDeNormal(p=p, q=q, nz_bajo_piso=bajo_piso, nz_minimo=nz_minimo)


def _extender_p_por_espejo(_np: ModuleType, p: NDArray[np.float64]) -> NDArray[np.float64]:
    """``∂h/∂x`` de la extensión par de ``h`` a ``2H × 2W``: impar en x y par en y."""
    return cast("NDArray[np.float64]", _np.block([[p, -p[:, ::-1]], [p[::-1, :], -p[::-1, ::-1]]]))


def _extender_q_por_espejo(_np: ModuleType, q: NDArray[np.float64]) -> NDArray[np.float64]:
    """``∂h/∂y`` de la extensión par de ``h`` a ``2H × 2W``: par en x e impar en y."""
    return cast("NDArray[np.float64]", _np.block([[q, q[:, ::-1]], [-q[::-1, :], -q[::-1, ::-1]]]))


def _poisson(
    _np: ModuleType, p: NDArray[np.float64], q: NDArray[np.float64], bordes: Bordes
) -> tuple[NDArray[np.float64], float]:
    """``h`` de media cero con ``Δh = ∂p/∂x + ∂q/∂y``: ``ĥ = (-i·wx·p̂ - i·wy·q̂) / (wx² + wy²)``.

    Sobre el toro de la entrada (``PERIODICOS``) o de su extensión par a ``2H × 2W`` (``NEUMANN``). Devuelve la
    altura de la grilla RESUELTA (en Neumann, la extendida) y el residuo imaginario máximo.
    """
    n_filas, n_cols = p.shape
    if bordes is Bordes.PERIODICOS:
        h_hat, q_hat = _np.fft.fft2(p), _np.fft.fft2(q)
    else:
        # De a UN campo: cada extensión 2H×2W (512 MiB en 4K) se libera al transformarse, en vez de convivir las
        # dos con los espectros (hallazgo de Codex en #759: el pico en 4K era de ~7 GiB).
        h_hat = _np.fft.fft2(_extender_p_por_espejo(_np, p))
        q_hat = _np.fft.fft2(_extender_q_por_espejo(_np, q))
    filas_resueltas, cols_resueltas = h_hat.shape
    wy, wx = ejes_de_frecuencia(_np, filas_resueltas, cols_resueltas, n_filas, n_cols)
    denominador = wx * wx + wy * wy
    # El denominador se anula en CUATRO bins: DC y los tres Nyquist autoparejados (la derivada se anula allí).
    # En todos el numerador también es 0 → 0/0 = NaN que contaminaría el espectro entero: se fijan a modo nulo.
    nulo = denominador == 0.0
    denominador[nulo] = 1.0
    # ĥ = (-i·wx·p̂ - i·wy·q̂) / (wx² + wy²) operado IN SITU sobre p̂: son las mismas operaciones elemento a
    # elemento que con temporales, pero sin materializar tres espectros complejos más (en 4K Neumann son GiB).
    h_hat *= -1j * wx
    q_hat *= 1j * wy
    h_hat -= q_hat
    del q_hat
    h_hat /= denominador
    del denominador
    h_hat[nulo] = 0.0
    h_complejo = _np.fft.ifft2(h_hat)
    residuo = float(_np.max(_np.abs(h_complejo.imag)))
    return cast("NDArray[np.float64]", _np.ascontiguousarray(h_complejo.real)), residuo


def _extender_peso_por_espejo(_np: ModuleType, peso: NDArray[np.float64]) -> NDArray[np.float64]:
    """El peso de la extensión par a ``2H × 2W``: par en los dos ejes (es una magnitud, no una derivada)."""
    return cast("NDArray[np.float64]", _np.block([[peso, peso[:, ::-1]], [peso[::-1, :], peso[::-1, ::-1]]]))


def _poisson_ponderado(
    _np: ModuleType,
    p: NDArray[np.float64],
    q: NDArray[np.float64],
    peso_de_los_datos: NDArray[np.float64],
    peso: NDArray[np.float64],
    sx2: float,
    sy2: float,
    bordes: Bordes,
) -> tuple[NDArray[np.float64], int]:
    """``h`` de media cero que minimiza ``Σ peso · (sx² (Dx h − p̃)² + sy² (Dy h − q̃)²)``, por gradiente conjugado.

    ``p̃ = peso_de_los_datos · p / peso``: donde el gradiente está definido los dos pesos coinciden y ``p̃ = p``; donde
    no, ``peso_de_los_datos`` es 0 y el objetivo es la pendiente nula (el relleno que elige ``integrar``).

    ``D`` es la derivada espectral de ``ejes_de_frecuencia`` (real y antisimétrica: ``Dᵀ = −D``), así que el sistema
    normal ``Dᵀ W D h = Dᵀ W g`` es simétrico semidefinido; su núcleo (la constante y los modos de Nyquist) se descarta
    en el precondicionador, igual que en ``_poisson``. Devuelve la altura de la grilla RESUELTA y las iteraciones.
    """
    n_filas, n_cols = p.shape
    if bordes is Bordes.NEUMANN:
        p, q = _extender_p_por_espejo(_np, p), _extender_q_por_espejo(_np, q)
        peso_de_los_datos = _extender_peso_por_espejo(_np, peso_de_los_datos)
        peso = _extender_peso_por_espejo(_np, peso)
    forma = p.shape
    wy, wx_completo = ejes_de_frecuencia(_np, forma[0], forma[1], n_filas, n_cols)
    # Las FFT reales usan la mitad no negativa del eje de columnas: son las primeras frecuencias del eje completo.
    wx = wx_completo[:, : forma[1] // 2 + 1]
    peso_medio = float(peso.mean())
    denominador = sx2 * peso_medio * wx * wx + sy2 * peso_medio * wy * wy
    nulo = denominador == 0.0
    denominador[nulo] = 1.0

    def adjunta(u: NDArray[np.float64], v: NDArray[np.float64]) -> NDArray[np.float64]:
        """``Dxᵀ u + Dyᵀ v`` (``Dᵀ = −D``)."""
        espectro = _np.fft.rfft2(u)
        espectro *= -1j * wx
        espectro -= 1j * wy * _np.fft.rfft2(v)
        return cast("NDArray[np.float64]", _np.fft.irfft2(espectro, s=forma))

    def operador(h: NDArray[np.float64]) -> NDArray[np.float64]:
        """``Dᵀ W D h``."""
        espectro = _np.fft.rfft2(h)
        gx = _np.fft.irfft2(1j * wx * espectro, s=forma)
        gy = _np.fft.irfft2(1j * wy * espectro, s=forma)
        del espectro
        gx *= peso
        gx *= sx2
        gy *= peso
        gy *= sy2
        return adjunta(gx, gy)

    def precondicionar(r: NDArray[np.float64]) -> NDArray[np.float64]:
        espectro = _np.fft.rfft2(r)
        espectro /= denominador
        espectro[nulo] = 0.0
        return cast("NDArray[np.float64]", _np.fft.irfft2(espectro, s=forma))

    altura = _np.zeros(forma)
    if peso_medio == 0.0:
        return altura, 0
    # El lado derecho ``Dᵀ W g``: ``W · g = nz · (−nx / (s · nz))`` queda acotado por ``nx`` aunque ``g`` no lo esté.
    residuo = adjunta(peso_de_los_datos * sx2 * p, peso_de_los_datos * sy2 * q)
    norma_del_lado_derecho = float(_np.linalg.norm(residuo))
    if norma_del_lado_derecho == 0.0:
        return altura, 0
    direccion = precondicionar(residuo)
    rz = float(_np.vdot(residuo, direccion))
    for iteracion in range(1, MAX_ITERACIONES + 1):
        imagen = operador(direccion)
        paso = rz / float(_np.vdot(direccion, imagen))
        altura += paso * direccion
        residuo -= paso * imagen
        del imagen
        if float(_np.linalg.norm(residuo)) <= TOLERANCIA_RELATIVA * norma_del_lado_derecho:
            altura -= altura.mean()
            return altura, iteracion
        precondicionado = precondicionar(residuo)
        rz_nuevo = float(_np.vdot(residuo, precondicionado))
        direccion *= rz_nuevo / rz
        direccion += precondicionado
        del precondicionado
        rz = rz_nuevo
    relativo = float(_np.linalg.norm(residuo)) / norma_del_lado_derecho
    raise SinConvergenciaError(
        f"la integración ponderada no convergió en {MAX_ITERACIONES} iteraciones "
        f"(residuo relativo {relativo:.3g}, se exige {TOLERANCIA_RELATIVA})"
    )


def integrar(
    normal: NDArray[np.float64],
    *,
    bordes: Bordes,
    escala_x: float = 1.0,
    escala_y: float = 1.0,
    nz_piso: float = NZ_PISO_NUMERICO,
    ponderacion: Ponderacion = Ponderacion.UNIFORME,
) -> AlturaIntegrada:
    """Altura de media cero (unidades de textura) cuyo gradiente reproduce ``normal``.

    ``escala_x``/``escala_y`` parametrizan la convención de signos (``N = normalize(-sx·p, -sy·q, 1)``): este
    módulo no declara cuál es la de Skyrim (eso es de NP-X0). La salida es de solo lectura.

    Costo: es CPU-bound y SÍNCRONA, y bloquea al hilo que la llama durante toda la integración. Orden de
    magnitud medido UNA vez (ni contrato ni experimento versionado): a 4096² (float64, 4 núcleos, numpy 2.4.6)
    ``PERIODICOS`` ~5 s con ~1.8 GiB de pico del proceso y ``NEUMANN`` ~24 s con ~4.7 GiB (la extensión por
    espejo a ``2H × 2W`` cuadruplica los píxeles). Desde código ``async`` se delega con ``asyncio.to_thread``
    (a 2048² con ``NEUMANN``, la llamada directa dejó el loop 4.3 s sin latir; con ``to_thread``, el mayor hueco
    fue de 46 ms), pero cancelar el ``await`` NO interrumpe el hilo: la integración sigue consumiendo CPU y
    memoria hasta terminar (cancelada a los 0.3 s, el hilo siguió hasta los 6.5 s). Quien la llame con timeout
    o reintentos debe acotar la concurrencia (por ejemplo, un semáforo) y no lanzar otra mientras la anterior
    corre, o aislarla en un proceso. Acá no se fija un tope de tamaño ni de concurrencia: lo decide quien llama.

    ``ponderacion``: ``UNIFORME`` (Frankot–Chellappa, el oráculo) o ``NZ`` (la del producto, ``PONDERACION_DEL_PRODUCTO``;
    ver el docstring del módulo). La ponderada es iterativa: a 2048² con ``PERIODICOS``, 24 iteraciones y ~9 s sobre una textura real;
    con ``NEUMANN`` resuelve sobre la extensión por espejo (4× los píxeles) y cuesta en proporción. Si no converge
    lanza ``SinConvergenciaError``: nunca devuelve una altura a medias.
    """
    _np = importar_numpy()
    campo = _exigir_normal(_np, normal)
    if not isinstance(bordes, Bordes):
        raise EntradaInvalidaError(f"bordes debe ser un miembro de Bordes, no {bordes!r}")
    _exigir_ejes_representables(campo.shape, bordes)
    sx = _exigir_escala("escala_x", escala_x)
    sy = _exigir_escala("escala_y", escala_y)
    piso = _exigir_piso(nz_piso)
    if not isinstance(ponderacion, Ponderacion):
        raise EntradaInvalidaError(f"ponderacion debe ser un miembro de Ponderacion, no {ponderacion!r}")

    p, q, bajo_piso, nz_minimo = _invertir_normal(_np, campo, sx, sy, piso)

    n_filas, n_cols = p.shape
    iteraciones = 0
    if ponderacion is Ponderacion.UNIFORME:
        altura, residuo = _poisson(_np, p, q, bordes)
        periodico, neumann = ALGORITMO_PERIODICO, ALGORITMO_NEUMANN
    else:
        nz = campo[..., 2]
        definido = nz >= piso
        peso_de_los_datos = cast("NDArray[np.float64]", _np.where(definido, nz, 0.0))
        # Bajo el piso el gradiente no está definido: el píxel pide pendiente nula con el peso medio de los definidos
        # (relleno suave) en vez de pesar su inversa. Sin ningún píxel definido no hay altura que reconstruir.
        relleno = float(peso_de_los_datos[definido].mean()) if bool(definido.any()) else 0.0
        peso = cast("NDArray[np.float64]", _np.where(definido, nz, relleno))
        altura, iteraciones = _poisson_ponderado(_np, p, q, peso_de_los_datos, peso, sx * sx, sy * sy, bordes)
        residuo = 0.0
        periodico, neumann = ALGORITMO_PONDERADO_PERIODICO, ALGORITMO_PONDERADO_NEUMANN
    if bordes is Bordes.PERIODICOS:
        algoritmo = periodico
    else:
        altura = cast("NDArray[np.float64]", _np.ascontiguousarray(altura[:n_filas, :n_cols]))
        if ponderacion is Ponderacion.NZ:
            # La extensión es simétrica solo hasta la tolerancia del gradiente conjugado: se centra el cuadrante.
            altura -= altura.mean()
        algoritmo = neumann
    altura.setflags(write=False)
    return AlturaIntegrada(
        altura=altura,
        algoritmo=algoritmo,
        nz_bajo_piso=bajo_piso,
        nz_minimo=nz_minimo,
        residuo_imaginario_max=residuo,
        iteraciones=iteraciones,
    )


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------


def _exigir_percentiles(bajo: object, alto: object) -> tuple[float, float]:
    if bajo is None or alto is None:
        raise EntradaInvalidaError(
            "PERCENTILES exige percentil_bajo y percentil_alto: no hay valor por defecto (ADR 0014 §4 y §6)"
        )
    if not _es_real_finito(bajo) or not _es_real_finito(alto):
        raise EntradaInvalidaError(f"los percentiles deben ser reales finitos, no {bajo!r} y {alto!r}")
    p_bajo, p_alto = float(cast("float", bajo)), float(cast("float", alto))
    if not 0.0 <= p_bajo < p_alto <= 100.0:
        raise EntradaInvalidaError(f"se exige 0 <= percentil_bajo < percentil_alto <= 100, no {p_bajo} y {p_alto}")
    return p_bajo, p_alto


def normalizar_altura(
    altura: NDArray[np.float64],
    modo: ModoNormalizacion,
    *,
    percentil_bajo: float | None = None,
    percentil_alto: float | None = None,
) -> NDArray[np.float64]:
    """Lleva ``altura`` a [0, 1] (float64, solo lectura). Una altura sin rango lanza ``AlturaPlanaError``.

    ``MIN_MAX`` usa el rango completo y rechaza los percentiles (no los ignora en silencio). ``PERCENTILES``
    recorta las colas fuera de ``[percentil_bajo, percentil_alto]``, que el que llama debe elegir.
    """
    _np = importar_numpy()
    campo = _exigir_campo(_np, altura, "altura")
    if modo is ModoNormalizacion.MIN_MAX:
        if percentil_bajo is not None or percentil_alto is not None:
            raise EntradaInvalidaError("MIN_MAX no usa percentiles: no se pasan percentil_bajo ni percentil_alto")
        bajo, alto = float(campo.min()), float(campo.max())
    elif modo is ModoNormalizacion.PERCENTILES:
        p_bajo, p_alto = _exigir_percentiles(percentil_bajo, percentil_alto)
        bajo, alto = (float(v) for v in _np.percentile(campo, [p_bajo, p_alto]))
    else:
        raise EntradaInvalidaError(f"modo debe ser un miembro de ModoNormalizacion, no {modo!r}")
    if not alto > bajo:
        raise AlturaPlanaError(f"la altura no tiene rango para normalizar ([{bajo}, {alto}])")
    salida = cast("NDArray[np.float64]", _np.clip((campo - bajo) / (alto - bajo), 0.0, 1.0))
    salida.setflags(write=False)
    return salida

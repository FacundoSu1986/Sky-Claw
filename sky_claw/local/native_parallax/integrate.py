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
    "NZ_PISO_NUMERICO",
    "AlturaIntegrada",
    "AlturaPlanaError",
    "Bordes",
    "EntradaInvalidaError",
    "GradientesDeNormal",
    "IntegracionError",
    "ModoNormalizacion",
    "gradientes_de_normal",
    "integrar",
    "normalizar_altura",
]

ALGORITMO_PERIODICO = "normal_fft_periodic_v1"
ALGORITMO_NEUMANN = "normal_fft_neumann_v1"

# Guarda numérica contra 1/0 (mismo valor que NP-M0 y PR-MATH-A); NO es un umbral de aceptación.
NZ_PISO_NUMERICO = 1e-6


class IntegracionError(Exception):
    """Base de los errores del integrador."""


class EntradaInvalidaError(IntegracionError, ValueError):
    """Array, escala, piso, borde, modo o percentil fuera de contrato."""


class AlturaPlanaError(IntegracionError):
    """La altura no tiene rango: normalizarla sería dividir por cero o inventar contraste."""


class Bordes(enum.Enum):
    """Hipótesis de borde de la textura."""

    PERIODICOS = "periodicos"
    NEUMANN = "neumann"


class ModoNormalizacion(enum.Enum):
    """Cómo llevar la altura a [0, 1]."""

    MIN_MAX = "min_max"
    PERCENTILES = "percentiles"


@dataclass(frozen=True, slots=True)
class AlturaIntegrada:
    """Resultado inmutable de ``integrar`` (el array es de solo lectura).

    ``altura``: (alto, ancho) float64 de media cero, en unidades de textura.
    ``algoritmo``: ``ALGORITMO_PERIODICO`` o ``ALGORITMO_NEUMANN``.
    ``nz_bajo_piso``: píxeles con ``nz < nz_piso`` (su gradiente se calculó con el piso).
    ``nz_minimo``: el ``nz`` más bajo de la entrada (puede ser negativo).
    ``residuo_imaginario_max``: parte imaginaria máxima tras la FFT inversa; debe ser del orden del redondeo.
    """

    altura: NDArray[np.float64]
    algoritmo: str
    nz_bajo_piso: int
    nz_minimo: float
    residuo_imaginario_max: float


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


def integrar(
    normal: NDArray[np.float64],
    *,
    bordes: Bordes,
    escala_x: float = 1.0,
    escala_y: float = 1.0,
    nz_piso: float = NZ_PISO_NUMERICO,
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
    """
    _np = importar_numpy()
    campo = _exigir_normal(_np, normal)
    if not isinstance(bordes, Bordes):
        raise EntradaInvalidaError(f"bordes debe ser un miembro de Bordes, no {bordes!r}")
    _exigir_ejes_representables(campo.shape, bordes)
    sx = _exigir_escala("escala_x", escala_x)
    sy = _exigir_escala("escala_y", escala_y)
    piso = _exigir_piso(nz_piso)

    p, q, bajo_piso, nz_minimo = _invertir_normal(_np, campo, sx, sy, piso)

    n_filas, n_cols = p.shape
    altura, residuo = _poisson(_np, p, q, bordes)
    if bordes is Bordes.PERIODICOS:
        algoritmo = ALGORITMO_PERIODICO
    else:
        altura = cast("NDArray[np.float64]", _np.ascontiguousarray(altura[:n_filas, :n_cols]))
        algoritmo = ALGORITMO_NEUMANN
    altura.setflags(write=False)
    return AlturaIntegrada(
        altura=altura,
        algoritmo=algoritmo,
        nz_bajo_piso=bajo_piso,
        nz_minimo=nz_minimo,
        residuo_imaginario_max=residuo,
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

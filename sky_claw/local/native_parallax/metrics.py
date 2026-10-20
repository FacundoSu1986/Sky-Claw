"""Métricas de calidad deterministas del generador nativo de parallax (NP-Q0a, ADR 0014 §2.5).

Son las métricas de la tabla §I de la arquitectura A que se calculan SIN ground truth de altura: sirven igual en
producción (donde solo hay una normal de entrada y la altura que se le integró) y sobre el benchmark de NP-D0.
Son PURAS: no escriben, no consultan el entorno y no importan ``research`` (ADR 0014, decisión 2).

**No deciden nada.** Acá no hay umbrales ni gates: los umbrales se DERIVAN del benchmark y se versionan
(ADR 0014 §6, NP-Q0b) y nunca se fijan a mano. Tampoco hay percentiles por defecto: el rango dinámico los pide.

Qué entra y qué no, con el motivo:

- ``acuerdo_normal`` (``normal_agreement``), ``residuo_de_integrabilidad`` (``integrability_residual``),
  ``razon_de_costura`` (``seam_score``), ``rango_dinamico`` (``dynamic_range``), ``razon_de_recorte``
  (``clip_ratio``) y ``contar_no_finitos`` (``nan_inf``).
- Quedan FUERA y declaradas pendientes: ``banding_index`` (necesita la altura ya codificada a BC4, es de NP-E0),
  ``mip_consistency`` (necesita mips, también de NP-E0), ``flatness_hf`` (la tabla no la define con precisión) y
  ``tileability_continuity``: el coseno entre gradientes de bordes opuestos NO distingue un salto de valor puro
  (la derivada de una rampa periodizada es simétrica respecto de la costura y da 1.0), que es justo lo que
  ``razon_de_costura`` ya mide; no se publica una métrica para la que no hay un caso que la haga fallar.

Decisiones de definición (la tabla §I deja huecos; estas son nuestras y están ancladas por tests):

- **Acuerdo de normal.** Se re-deriva la normal de la altura reconstruida y se compara con la de entrada. La
  altura sale con una escala y un signo arbitrarios (la media no es observable y un flip global de gradientes
  produce ``-altura`` exacto), así que se ajusta el escalar ``a`` que mejor alinea ``a·∇h`` con los gradientes de la
  normal. El ajuste es de mínimos cuadrados PONDERADO por ``nz² · s²`` en cada eje: equivale a ajustar en el
  espacio de la normal, donde un píxel con ``nz → 0`` pesa ~0 en vez de dominar (la inversa vale ~1e6 ahí y
  arrastraría la escala de TODA la imagen). El signo de ``a`` se reporta aparte: es lo único que delata una
  inversión sistemática. El ángulo se calcula con ``atan2(|n × m|, n · m)`` y no con ``arccos``: ``arccos`` pierde
  la mitad de las cifras para errores diminutos.
- **Razón de costura.** ``mean|paso de wrap| / mean|paso interior|`` por eje, el máximo de los dos: es adimensional
  y no depende de la amplitud. Es LA MISMA matemática que ``bench.sintetico.razon_borde`` (que caracteriza el
  ground truth); ``tests/test_native_parallax_metrics.py`` enumera los 17 casos para que no se separen.
- **Residuo de integrabilidad.** ``mean|∂p/∂y − ∂q/∂x|`` sobre la suma de las medias de las cuatro derivadas del
  campo de gradientes (el Hessiano completo) ∈ [0, 1]: 0 es un campo que es gradiente de una altura; 1 es rotor
  puro. Se normaliza por el Hessiano y no por ``|∂p/∂y| + |∂q/∂x|`` porque en un campo separable esas dos
  magnitudes son ruido de redondeo. Con bordes Neumann se extiende por espejo igual que el integrador.

Límite declarado: las pruebas son sobre normales sintéticas. Que estas métricas PREDIGAN el riesgo en texturas
reales no está demostrado: EXP-M2 midió lo contrario para señales solo-normal (mejor AUC 0.62), así que NO
justifican por sí solas un ``AUTO_ACCEPT``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING

from sky_claw.local.native_parallax._extra import importar_numpy
from sky_claw.local.native_parallax.espectral import gradientes_espectrales

# Ayudantes del integrador: la MISMA validación de entradas y la MISMA extensión por espejo que usa ``integrar``.
from sky_claw.local.native_parallax.integrate import (
    NZ_PISO_NUMERICO,
    Bordes,
    EntradaInvalidaError,
    _exigir_campo,
    _exigir_ejes_representables,
    _exigir_percentiles,
    _extender_p_por_espejo,
    _extender_q_por_espejo,
    gradientes_de_normal,
)

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

__all__ = [
    "AcuerdoNormal",
    "acuerdo_normal",
    "contar_no_finitos",
    "rango_dinamico",
    "razon_de_costura",
    "razon_de_recorte",
    "residuo_de_integrabilidad",
]

# Cota numérica para decir "esta magnitud es cero" al dividir (no es un umbral de calidad).
_EPS = 1e-12


@dataclass(frozen=True, slots=True)
class AcuerdoNormal:
    """Resultado de ``acuerdo_normal``.

    ``media_grados``/``mediana_grados``/``p95_grados``: error angular entre la normal de entrada y la re-derivada de
    la altura (ya escalada por ``escala``). ``escala``: el escalar ``a`` ajustado (``a·∇h ≈ ∇h_normal``); es 1 si la
    altura está en las unidades de la normal. ``signo``: ``+1`` o ``-1`` según el signo de ``escala``; ``-1`` es una
    inversión sistemática que el error angular, por haberla absorbido en ``a``, no muestra.
    """

    media_grados: float
    mediana_grados: float
    p95_grados: float
    escala: float
    signo: int


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------


def _exigir_bordes(bordes: object) -> Bordes:
    if not isinstance(bordes, Bordes):
        raise EntradaInvalidaError(f"bordes debe ser un miembro de Bordes, no {bordes!r}")
    return bordes


# ---------------------------------------------------------------------------
# Derivadas según el borde
# ---------------------------------------------------------------------------


def _gradientes_de_altura(
    _np: ModuleType, altura: NDArray[np.float64], bordes: Bordes
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """``(∂h/∂x, ∂h/∂y)``: espectral sobre el toro, o sobre la extensión PAR a ``2H × 2W`` en Neumann."""
    if bordes is Bordes.PERIODICOS:
        return gradientes_espectrales(_np, altura)
    n_filas, n_cols = altura.shape
    extendida = _np.block([[altura, altura[:, ::-1]], [altura[::-1, :], altura[::-1, ::-1]]])
    p, q = gradientes_espectrales(_np, extendida, n_filas, n_cols)
    return p[:n_filas, :n_cols], q[:n_filas, :n_cols]


# ---------------------------------------------------------------------------
# Métricas con normal
# ---------------------------------------------------------------------------


def acuerdo_normal(
    normal: NDArray[np.float64],
    altura: NDArray[np.float64],
    *,
    bordes: Bordes,
    escala_x: float = 1.0,
    escala_y: float = 1.0,
) -> AcuerdoNormal:
    """Qué tan bien la ``altura`` reproduce la ``normal`` de la que salió (ver el docstring del módulo)."""
    _np = importar_numpy()
    modo = _exigir_bordes(bordes)
    gradientes = gradientes_de_normal(normal, escala_x=escala_x, escala_y=escala_y)
    campo_altura = _exigir_campo(_np, altura, "altura")
    if campo_altura.shape != normal.shape[:2]:
        raise EntradaInvalidaError(f"altura {campo_altura.shape} no tiene la forma de la normal {normal.shape[:2]}")
    _exigir_ejes_representables(campo_altura.shape, modo)
    if bool(_np.any(_np.all(normal == 0.0, axis=-1))):
        raise EntradaInvalidaError("normal tiene vectores de norma cero")

    p_alt, q_alt = _gradientes_de_altura(_np, campo_altura, modo)
    peso = _np.maximum(normal[..., 2], NZ_PISO_NUMERICO) ** 2
    peso_x, peso_y = peso * escala_x**2, peso * escala_y**2
    numerador = float(_np.sum(peso_x * p_alt * gradientes.p + peso_y * q_alt * gradientes.q))
    denominador = float(_np.sum(peso_x * p_alt * p_alt + peso_y * q_alt * q_alt))
    escala = numerador / denominador if denominador > 0.0 else 0.0

    nx = -escala_x * escala * p_alt
    ny = -escala_y * escala * q_alt
    norma_rec = _np.sqrt(nx * nx + ny * ny + 1.0)
    reconstruida = _np.stack((nx / norma_rec, ny / norma_rec, 1.0 / norma_rec), axis=-1)
    # ``atan2`` es homogénea en el vector de entrada (|n × m| y n · m escalan igual): una normal NO unitaria, como las
    # cuantizadas a 2 canales, da el mismo ángulo sin normalizarla.
    producto_cruzado = _np.linalg.norm(_np.cross(reconstruida, normal), axis=-1)
    producto_punto = _np.sum(reconstruida * normal, axis=-1)
    grados = _np.degrees(_np.arctan2(producto_cruzado, producto_punto))
    return AcuerdoNormal(
        media_grados=float(grados.mean()),
        mediana_grados=float(_np.median(grados)),
        p95_grados=float(_np.percentile(grados, 95)),
        escala=float(escala),
        signo=1 if escala >= 0.0 else -1,
    )


def residuo_de_integrabilidad(
    normal: NDArray[np.float64],
    *,
    bordes: Bordes,
    escala_x: float = 1.0,
    escala_y: float = 1.0,
    nz_piso: float = NZ_PISO_NUMERICO,
) -> float:
    """Cuánto se aparta el campo de gradientes de la ``normal`` de ser el de alguna altura (0 = exacto, 1 = rotor)."""
    _np = importar_numpy()
    modo = _exigir_bordes(bordes)
    gradientes = gradientes_de_normal(normal, escala_x=escala_x, escala_y=escala_y, nz_piso=nz_piso)
    _exigir_ejes_representables(gradientes.p.shape, modo)
    if modo is Bordes.PERIODICOS:
        p_x, p_y = gradientes_espectrales(_np, gradientes.p)
        q_x, q_y = gradientes_espectrales(_np, gradientes.q)
    else:
        n_filas, n_cols = gradientes.p.shape
        p_x, p_y = gradientes_espectrales(_np, _extender_p_por_espejo(_np, gradientes.p), n_filas, n_cols)
        q_x, q_y = gradientes_espectrales(_np, _extender_q_por_espejo(_np, gradientes.q), n_filas, n_cols)
    # Con bordes Neumann los cuatro términos se promedian sobre la grilla EXTENDIDA: el espejo hace simétrico a cada
    # uno de ellos, así que su media vale lo mismo que en el cuadrante de la textura original (verificado a 15 dígitos).
    # Se normaliza por la escala del Hessiano COMPLETO y no por |p_y| + |q_x|: en un campo separable
    # (h = f(x) + g(y)) la derivada cruzada es 0 y esas dos magnitudes son ruido de redondeo, así que el cociente
    # 0/0 daba ~0.5 justo en los campos más perfectos.
    denominador = float(_np.abs(p_x).mean() + _np.abs(p_y).mean() + _np.abs(q_x).mean() + _np.abs(q_y).mean())
    if denominador == 0.0:
        return 0.0
    return float(_np.abs(p_y - q_x).mean()) / denominador


# ---------------------------------------------------------------------------
# Métricas solo de altura
# ---------------------------------------------------------------------------


def razon_de_costura(altura: NDArray[np.float64]) -> float:
    """``mean(|paso de wrap|) / mean(|paso interior|)`` por eje; devuelve el máximo de los dos.

    Misma matemática que ``bench.sintetico.razon_borde``. El paso interior es el del borde a su vecino; si es nulo
    (un campo que arranca plano) se usa el promedio de TODOS los pasos adyacentes, y solo un eje realmente
    constante cuenta como periódico (razón 1).
    """
    _np = importar_numpy()
    campo = _exigir_campo(_np, altura, "altura")
    # "Paso nulo" se decide RELATIVO a la escala del campo: con un épsilon absoluto, una altura de amplitud menor que
    # ~1e-11 perdía su costura y reportaba 1.0 aunque la razón es adimensional (revisión de Codex en #760). Un eje
    # constante que sale de una FFT trae pasos del orden del redondeo de la escala del campo, y esos siguen contando
    # como cero.
    paso_nulo = _EPS * float(_np.abs(campo).max())
    razones: list[float] = []
    for eje in (0, 1):
        salto = float(_np.abs(_np.take(campo, 0, axis=eje) - _np.take(campo, -1, axis=eje)).mean())
        vecino = float(_np.abs(_np.take(campo, 1, axis=eje) - _np.take(campo, 0, axis=eje)).mean())
        if vecino <= paso_nulo:
            vecino = float(_np.abs(_np.diff(campo, axis=eje)).mean())
        if vecino <= paso_nulo:
            razones.append(1.0)
            continue
        razones.append(salto / vecino)
    return max(razones)


def rango_dinamico(altura: NDArray[np.float64], *, percentil_bajo: float, percentil_alto: float) -> float:
    """Diferencia entre los percentiles ``percentil_alto`` y ``percentil_bajo`` de la altura.

    Los percentiles son obligatorios y no tienen valor por defecto: elegirlos es una decisión de authoring
    (ADR 0014 §4 y §6), no de la matemática.
    """
    _np = importar_numpy()
    campo = _exigir_campo(_np, altura, "altura")
    p_bajo, p_alto = _exigir_percentiles(percentil_bajo, percentil_alto)
    bajo, alto = (float(v) for v in _np.percentile(campo, [p_bajo, p_alto]))
    return alto - bajo


def razon_de_recorte(altura: NDArray[np.float64]) -> float:
    """Fracción de píxeles EXACTAMENTE en 0 o en 1; exige una altura ya normalizada a ``[0, 1]``."""
    _np = importar_numpy()
    campo = _exigir_campo(_np, altura, "altura")
    if float(campo.min()) < 0.0 or float(campo.max()) > 1.0:
        raise EntradaInvalidaError("altura debe estar normalizada en [0, 1]")
    return int(_np.count_nonzero((campo == 0.0) | (campo == 1.0))) / campo.size


def contar_no_finitos(*campos: NDArray[np.float64]) -> int:
    """Cantidad de NaN e infinitos en todos los ``campos`` (arrays flotantes, al menos uno)."""
    _np = importar_numpy()
    if not campos:
        raise EntradaInvalidaError("contar_no_finitos exige al menos un campo")
    total = 0
    for campo in campos:
        if not isinstance(campo, _np.ndarray) or not _np.issubdtype(campo.dtype, _np.floating):
            raise EntradaInvalidaError("cada campo debe ser un ndarray de punto flotante")
        total += int(campo.size - _np.count_nonzero(_np.isfinite(campo)))
    return total

"""Harness adversarial del contrato H4 (Slice 2, PR #723) — insumo externo.

Origen: revisión de código y matemáticas del 2026-10-15 (sesión Arena, rama
``arena/1ce834fb-sky-claw``). Cada test ataca una arista del contrato documentado en
``docs/design/research/native-parallax/h4-resize-normal-preflight.md`` y
``h4-resize-normal-authorization-gate.md`` (V-01..V-05, D-06, D-07, N-11, F01) contra
``resize_normal_diagnostic`` / ``resize_normal`` de
``sky_claw/local/native_parallax/research/authored_dataset.py``.

Diseño:
- Los casos son INDEPENDIENTES de la implementación: la referencia del kernel
  bilineal de Pillow para downscale 2× es el filtro triangular escalado de 4 taps
  ``(1/8, 3/8, 3/8, 1/8)`` sobre las columnas ``(2j−1, 2j, 2j+1, 2j+2)`` con clamp de
  borde (verificado con respuesta al impulso: un delta en la columna 5 de 16→8
  reparte 3/8 y 1/8 en las salidas 2 y 3). Se deriva de la especificación del
  filtro, no de la implementación bajo contrato.
- El hallazgo H4 (auditoría 2026-10-07, ``H4_IMPLEMENTATION_DEFECT = CONFIRMED``)
  queda fijado como caso RED permanente: normal plana ⇒ ``mean(nx)`` exactamente 0
  en el camino float (el camino uint8 histórico producía −0.0039215083).
- El barrido de fase (familia E) codifica el gate #717 §6.1: la cancelación del
  kernel depende de la FASE de muestreo; el contrato (degenerado marcado con crudo
  preservado, o postcondición de norma unitaria) debe sostenerse para TODAS las
  fases, con el censo predicho por la referencia independiente.

En ``main`` (donde el contrato Slice 2 aún no está implementado) la suite se SALTEA
completa: es un insumo para el PR #723, no un test del estado vigente.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "sky_claw" / "local" / "native_parallax" / "research" / "authored_dataset.py"
)


def _load_contract_module():
    """Carga ``authored_dataset`` por ruta (sin la cadena de ``__init__`` del paquete).

    Debe registrarse en ``sys.modules`` ANTES de ``exec_module``: ``@dataclass``
    resuelve anotaciones vía ``sys.modules[cls.__module__]`` y sin el registro lanza
    ``AttributeError`` en el procesamiento de la clase."""
    spec = importlib.util.spec_from_file_location(
        "_h4_contract_authored_dataset", _MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _contract_api_available() -> bool:
    if not _MODULE_PATH.exists():
        return False
    try:
        mod = _load_contract_module()
    except Exception:
        return False
    return all(
        hasattr(mod, name)
        for name in (
            "resize_normal_diagnostic",
            "resize_normal",
            "DEGENERATE_THRESHOLD",
            "UNIT_NORM_TOLERANCE",
            "NormalResizeResult",
            "InvalidNormalShapeError",
            "DegenerateNormalError",
        )
    )


pytestmark = pytest.mark.skipif(
    not _contract_api_available(),
    reason=(
        "Requiere el contrato H4 Slice 2 (PR #723: resize_normal_diagnostic y "
        "excepciones tipadas en authored_dataset). En main la suite es insumo, no test."
    ),
)


@pytest.fixture(scope="module")
def m():
    return _load_contract_module()


# ============================================================================
# Referencias independientes (derivadas del kernel bilineal de Pillow a factor 2)
# ============================================================================

# Filtro triangular escalado para downscale por factor 2: pesos exactos en binario.
_KERNEL2_WEIGHTS = (0.125, 0.375, 0.375, 0.125)  # taps: (2j-1, 2j, 2j+1, 2j+2)


def _down2_axis(a: np.ndarray, axis: int) -> np.ndarray:
    """Convolución separable con _KERNEL2_WEIGHTS + subsample 2.

    Semántica de borde de Pillow (verificada con respuesta al impulso): los taps
    fuera de rango se DESCARTAN y los pesos se renormalizan sobre los válidos
    (impulso en col 0 de 16→8 ⇒ 3/7 en la salida 0; impulso en col 15 ⇒ 3/7 en la
    salida 7). El clamp por duplicado de borde NO es el comportamiento de Pillow."""
    n = a.shape[axis]
    out_n = n // 2
    taps = np.arange(out_n)[None, :] * 2 - 1 + np.arange(4)[:, None]  # (4, out_n)
    valid = (taps >= 0) & (taps < n)
    w = np.where(valid, np.array(_KERNEL2_WEIGHTS)[:, None], 0.0)
    w = w / w.sum(axis=0, keepdims=True)  # renormalización de Pillow en los bordes
    bshape = [1] * a.ndim
    bshape[axis] = out_n  # pesos por-posición broadcastables al rango de `a`
    return sum(
        w[k].reshape(bshape) * np.take(a, np.where(valid[k], taps[k], 0), axis=axis)
        for k in range(4)
    )


def _down2_kernel_reference(src: np.ndarray) -> np.ndarray:
    """Referencia analítica del resize bilineal Pillow 2× (separable en filas y
    columnas), ANTES de renormalizar."""
    return _down2_axis(_down2_axis(src, axis=0), axis=1)


def test_kernel_reference_sanity_1e_impulso_y_normalizacion():
    """La referencia reproduce la respuesta al impulso medida de Pillow 16→8:
    delta en col 0 ⇒ 3/7 en la salida 0 (renormalización de borde); delta en col 5
    ⇒ 3/8 en la salida 2 y 1/8 en la salida 3; delta en col 15 ⇒ 3/7 en la salida 7."""
    imp = np.zeros((1, 16, 1))
    imp[0, 0, 0] = 1.0
    out0 = _down2_axis(imp, axis=1)[0, :, 0]
    np.testing.assert_allclose(out0, np.array([3 / 7] + [0.0] * 7), atol=1e-7)

    imp[0, 0, 0] = 0.0
    imp[0, 5, 0] = 1.0
    out5 = _down2_axis(imp, axis=1)[0, :, 0]
    esperado5 = np.zeros(8)
    esperado5[2] = 0.375
    esperado5[3] = 0.125
    np.testing.assert_array_equal(out5, esperado5)  # interior: pesos exactos

    imp[0, 5, 0] = 0.0
    imp[0, 15, 0] = 1.0
    out15 = _down2_axis(imp, axis=1)[0, :, 0]
    np.testing.assert_allclose(out15, np.array([0.0] * 7 + [3 / 7]), atol=1e-7)
    # pesos normalizados a 1 y exactos en binario (interior)
    assert sum(_KERNEL2_WEIGHTS) == 1.0


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _smooth_unit_field(n: int, seed: int = 7) -> np.ndarray:
    """Campo de normales unitarias suave y periódico (band-limited)."""
    i = np.arange(n)
    X, Y = np.meshgrid(i, i, indexing="xy")
    k = 2 * np.pi / n
    h = 0.2 * np.sin(2 * k * X) * np.cos(k * Y) + 0.1 * np.sin(3 * k * X + 2 * k * Y)
    p = np.gradient(h, axis=1)
    q = np.gradient(h, axis=0)
    return _unit(np.stack([-p, -q, np.ones_like(p)], axis=-1))


# ============================================================================
# Familia A — envolvente estructural (#717 §3.4, F1): fail-closed en TODOS los caminos
# ============================================================================


def test_a1_forma_hw4_rechazada_sin_descartar_canal(m):
    with pytest.raises(m.InvalidNormalShapeError):
        m.resize_normal(np.zeros((8, 8, 4)), 4)


def test_a2_ndim2_rechazada(m):
    with pytest.raises(m.InvalidNormalShapeError):
        m.resize_normal(np.zeros((8, 8)), 4)


def test_a3_dimensiones_vacias_rechazadas(m):
    with pytest.raises(m.InvalidNormalShapeError):
        m.resize_normal(np.zeros((0, 8, 3)), 4)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_a4_no_finito_rechazado_en_identidad(m, bad):
    n = np.zeros((8, 8, 3))
    n[..., 2] = 1.0
    n[0, 0, 0] = bad  # identidad: la validación NO se saltea (bypass histórico)
    with pytest.raises(m.InvalidNormalShapeError):
        m.resize_normal(n, 8)


def test_a5_no_finito_rechazado_en_resize(m):
    n = np.zeros((16, 16, 3))
    n[..., 2] = 1.0
    n[3, 3, 1] = np.nan
    with pytest.raises(m.InvalidNormalShapeError):
        m.resize_normal(n, 8)


@pytest.mark.parametrize("bad_size", [True, False, 2.0, "8", 0, -4])
def test_a6_size_no_entero_o_invalido_rechazado(m, bad_size):
    n = np.zeros((8, 8, 3))
    n[..., 2] = 1.0
    with pytest.raises(m.InvalidNormalShapeError):
        m.resize_normal(n, bad_size)


def test_a7_entrada_no_convertible_rechazada(m):
    n = np.empty((8, 8, 3), dtype=object)
    n[:] = "x"
    with pytest.raises(m.InvalidNormalShapeError):
        m.resize_normal(n, 4)


# ============================================================================
# Familia B — rango (N-11: FUERA_DE_RANGO_FINITO = CLIP, TOLERADO)
# ============================================================================


def test_b1_clip_en_identidad_tolera_y_renormaliza_con_direccion_preservada(m):
    n = np.zeros((8, 8, 3))
    n[..., 0] = 1000.0
    n[..., 2] = 0.5
    out = m.resize_normal(n, 8)  # identidad: clip -> (1, 0, 0.5) -> renormalizar
    esperado = _unit(
        np.stack([np.ones((8, 8)), np.zeros((8, 8)), np.full((8, 8), 0.5)], axis=-1)
    )
    np.testing.assert_allclose(out, esperado, rtol=0, atol=1e-15)
    assert float(np.max(np.abs(np.linalg.norm(out, axis=-1) - 1.0))) <= m.UNIT_NORM_TOLERANCE


def test_b2_clip_en_resize_tolera_con_postcondicion_de_norma(m):
    n = np.zeros((16, 16, 3))
    n[..., 1] = -700.0
    n[..., 2] = 3.0
    out = m.resize_normal(n, 8)
    # N-11: el clip a [-1,1] corre ANTES de bifurcar ⇒ (0,-1,1); al ser constante,
    # el resize la preserva y la renormalización agota la postcondición §3.2.
    esperado = _unit(np.array([0.0, -1.0, 1.0]))
    assert float(np.max(np.abs(np.linalg.norm(out, axis=-1) - 1.0))) <= 1e-12
    # float32 en la interpolación (pesos de borde 3/7 no exactos): 1e-7, no 1e-15
    np.testing.assert_allclose(out[0, 0], esperado, rtol=0, atol=5e-6)
    np.testing.assert_allclose(out, np.broadcast_to(esperado, out.shape), rtol=0, atol=5e-6)


# ============================================================================
# Familia C — camino identidad (D-07/V-01, adjudicación H4-F01)
# ============================================================================


def test_c1_identidad_de_entrada_unitaria_es_noop_bit_a_bit(m):
    n = _smooth_unit_field(8, seed=3)
    n64 = np.asarray(n, dtype=np.float64)
    res = m.resize_normal_diagnostic(n64, 8)
    assert not res.degenerate and res.degenerate_count == 0
    assert np.array_equal(res.array, n64)  # valores bit a bit; sin renormalizar
    assert res.array.dtype == np.float64


def test_c2_identidad_direccion_confiable_norma_no_unitaria_renormaliza(m):
    n = np.zeros((8, 8, 3))
    n[..., 0] = 1.2
    n[..., 2] = 1.6  # ‖·‖ = 2 exacto; dirección confiable
    res = m.resize_normal_diagnostic(n, 8)
    assert not res.degenerate
    assert float(np.max(np.abs(np.linalg.norm(res.array, axis=-1) - 1.0))) <= m.UNIT_NORM_TOLERANCE


def test_c3_identidad_subumbral_marca_y_preserva_crudo_bit_a_bit(m):
    n = np.zeros((8, 8, 3))
    n[..., 0] = 0.1  # ‖·‖ = 0.1 < 5/16
    crudo = n.copy()
    res = m.resize_normal_diagnostic(n, 8)
    assert res.degenerate and res.degenerate_count == 64
    assert res.threshold == pytest.approx(5.0 / 16.0)
    assert np.array_equal(res.array, crudo)  # D-06: NO se renormaliza NADA


def test_c4_identidad_con_norma_bajo_umbral_la_fachada_lanza(m):
    n = np.zeros((8, 8, 3))
    n[..., 0] = 0.1
    with pytest.raises(m.DegenerateNormalError) as excinfo:
        m.resize_normal(n, 8)
    assert getattr(excinfo.value, "degenerate_count", None) == 64
    assert getattr(excinfo.value, "threshold", None) == pytest.approx(5.0 / 16.0)
    assert getattr(excinfo.value, "fraction", None) == pytest.approx(1.0)


# ============================================================================
# Familia D — resize en float: el defecto H4 no puede regresar
# ============================================================================


def test_d1_caso_red_del_audit_normal_plana_mean_nx_exactamente_cero(m):
    """Auditoría 2026-10-07: el camino uint8 producía mean(nx) = −0.0039215083."""
    n = np.zeros((512, 512, 3))
    n[..., 2] = 1.0
    out = m.resize_normal(n, 256)
    assert float(np.max(np.abs(out[..., 0]))) == 0.0
    assert float(np.max(np.abs(out[..., 1]))) == 0.0


def test_d2_resize_preserva_postcondicion_norma_unitaria(m):
    n = _smooth_unit_field(64, seed=11)
    out = m.resize_normal(n, 32)
    assert float(np.max(np.abs(np.linalg.norm(out, axis=-1) - 1.0))) <= 1e-12
    assert out.dtype == np.float64


def test_d3_resize_coincide_con_referencia_manual_del_kernel(m):
    """64→32: la interpolación canal a canal debe igualar la referencia del kernel
    (float32: tolerancia de redondeo, no de contrato), y la renormalización sobre
    esa referencia debe agotar la diferencia."""
    n = _smooth_unit_field(64, seed=5)
    crudo_ref = _down2_kernel_reference(n)
    esperado = _unit(crudo_ref)
    out = m.resize_normal(n, 32)
    np.testing.assert_allclose(out, esperado, rtol=0, atol=5e-6)


def test_d4_checkerboard_ecuatorial_cancelacion_exacta_es_degenerada(m):
    """Vectores unitarios opuestos (±1,0,0) entrelazados por columna: el kernel los
    cancela EXACTAMENTE en el interior (norma 0) y deja 1/4 en los bordes clampeados
    (‖·‖ = 0.25 < 5/16). DEBE marcarse degenerado con crudo preservado, jamás
    renormalizarse en silencio (D-06; la cancelación alcanzable es ancla de §6.1)."""
    n = np.zeros((16, 16, 3))
    patron = np.arange(16) % 2 == 0
    n[:, patron, 0] = 1.0
    n[:, ~patron, 0] = -1.0
    crudo_ref = _down2_kernel_reference(n)
    res = m.resize_normal_diagnostic(n, 8)
    assert res.degenerate
    assert res.degenerate_count == 64  # todo píxel de salida queda bajo 5/16
    np.testing.assert_allclose(res.array, crudo_ref, rtol=0, atol=1e-6)
    assert float(np.max(np.linalg.norm(res.array, axis=-1))) < 5.0 / 16.0
    with pytest.raises(m.DegenerateNormalError):
        m.resize_normal(n, 8)


def test_d5_campo_constante_es_punto_fijo_exacto_del_resize(m):
    n = np.zeros((16, 16, 3))
    n[..., 0] = 0.6
    n[..., 2] = 0.8
    n = _unit(n)
    out = m.resize_normal(n, 8)
    np.testing.assert_array_equal(out, np.broadcast_to(out[0, 0], out.shape))


# ============================================================================
# Familia E — barrido de fase del kernel (gate #717 §6.1: la cancelación depende
# de la FASE; el censo lo predice la referencia independiente, no la implementación)
# ============================================================================


@pytest.mark.parametrize("c", list(range(0, 17)))
def test_e1_transicion_barrida_por_fase_contrato_sostenido(m, c):
    """Salto angular 180° en la columna c de un campo 16→8 (ecuatorial). Para cada
    fase el censo de degenerados es el que predice la referencia del kernel; el
    contrato se sostiene en las dos ramas: crudo preservado si degenera, postcondición
    de norma si no."""
    n = np.zeros((16, 16, 3))
    columnas = np.arange(16)
    n[:, columnas < c, 0] = 1.0
    n[:, columnas >= c, 0] = -1.0

    crudo_ref = _down2_kernel_reference(n)
    normas_ref = np.linalg.norm(crudo_ref, axis=-1)
    mask_ref = normas_ref < 5.0 / 16.0

    res = m.resize_normal_diagnostic(n, 8)
    assert res.degenerate == bool(mask_ref.any())
    assert res.degenerate_count == int(mask_ref.sum())
    if mask_ref.any():
        np.testing.assert_allclose(res.array[mask_ref], crudo_ref[mask_ref], rtol=0, atol=1e-6)
        with pytest.raises(m.DegenerateNormalError):
            m.resize_normal(n, 8)
    else:
        assert float(np.max(np.abs(np.linalg.norm(res.array, axis=-1) - 1.0))) <= 1e-12


def test_e2_fase_de_cruce_exacto_ancla_infimo_cero(m):
    """La fase que hace coincidir la transición con un tap del kernel cancela
    EXACTAMENTE (norma 0) — el ínfimo 0 de §6.1 es alcanzable, no sólo teórico."""
    n = np.zeros((16, 16, 3))
    n[:, :9, 0] = 1.0
    n[:, 9:, 0] = -1.0
    res = m.resize_normal_diagnostic(n, 8)
    assert res.degenerate and res.degenerate_count == 8
    # la columna de salida cuyo tap cae sobre la transición cancela exactamente
    crudo_ref = _down2_kernel_reference(n)
    normas_ref = np.linalg.norm(crudo_ref, axis=-1)
    assert int((normas_ref == 0.0).sum()) >= 8
    assert float(np.max(np.abs(res.array[normas_ref == 0.0]))) == 0.0


# ============================================================================
# Familia F — diagnóstico y contrato del vehículo (§5.3.1)
# ============================================================================


def test_f1_vehiculo_censo_consistente(m):
    n = _smooth_unit_field(16, seed=2)
    res = m.resize_normal_diagnostic(n, 8)
    assert res.total_pixels == 64
    assert res.threshold == pytest.approx(5.0 / 16.0)
    assert res.degenerate_count == 0 and res.degenerate is False


def test_f2_umbral_congelado_5_16_no_recalibrado(m):
    """§6.1: THRESHOLD_FIXED_HERE=NO fue cerrado por #723 con 5/16 (heurística RMS,
    prohibido recalibrar con corpus). El valor es parte del contrato público."""
    assert m.DEGENERATE_THRESHOLD == 5.0 / 16.0
    assert m.UNIT_NORM_TOLERANCE == 1e-6


def test_f3_degeneracion_parcial_bandera_global_y_crudo_en_zonas_degeneradas(m):
    """Un bloque ecuatorial alternante de 3 filas degrada SOLO los píxeles de salida
    que el kernel alimenta mayoritariamente desde el bloque (predicho por la
    referencia): la bandera es GLOBAL, el censo exacto y el crudo se preserva en las
    zonas degeneradas (decisión del llamador con identidad del asset)."""
    n = _smooth_unit_field(16, seed=4)
    patron = np.arange(16) % 2 == 0
    for fila in (5, 6, 7):
        n[fila, :, :] = 0.0
        n[fila, patron, 0] = 1.0
        n[fila, ~patron, 0] = -1.0
    crudo_ref = _down2_kernel_reference(n)
    mask_ref = np.linalg.norm(crudo_ref, axis=-1) < 5.0 / 16.0
    assert mask_ref.any() and not mask_ref.all()  # el caso es genuinamente parcial

    res = m.resize_normal_diagnostic(n, 8)
    assert res.degenerate == bool(mask_ref.any())
    assert res.degenerate_count == int(mask_ref.sum())
    np.testing.assert_allclose(res.array[mask_ref], crudo_ref[mask_ref], rtol=0, atol=1e-6)

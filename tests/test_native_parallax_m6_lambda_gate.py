"""Gate automatizado del contrato matemático M6 — núcleo FFT periódico sin
ponderar (research-only, insumo de #675 / EXP-M6).

Congela los recibos numéricos del runner canónico
``scripts/run_lambda_study_m6.py`` (SEED=20261015): pesos de Parseval rfft2
(toda paridad), curva E(λ) construida con los operadores D del solver,
límite continuo V∞(λ) del término de ruido con λ>0 (REV 6), y el selftest
completo del runner (contraejemplo Nyquist, espacio nulo, ceguera de la
métrica de pendientes ante altura nula, MC de espectro completo). Cualquier
regresión de ponderación espectral, de símbolos d, de la curva o del límite
falla aquí.

Fail-closed estructural (REV 6): la ausencia del runner canónico es **FAIL
siempre** (test de presencia incondicional) — no depende de variables de
entorno y no admite verde falso en CI. Los tests numéricos se marcan SKIP
con razón explícita sólo cuando el runner falta (y el job ya está en rojo
por el test de presencia).

Ejecución: ``pytest tests/test_native_parallax_m6_lambda_gate.py --noconftest
-q`` (numpy-only; sin dependencias de sky_claw; runtime < 10 s).

Valores dorados verificados por DOS partes independientes al dígito
(ronda 5, 2026-10-15) — MSE(λ=0, régimen B, sólo-ruido, σ=0.1):

======  ===========
N       MSE
======  ===========
32      0.006793849
64      0.007955333
128     0.009087217
256     0.010204638
512     0.011314910
======  ===========

Límite continuo (REV 6, ronda 6 — dos vías independientes, ver §8.4 del doc):
V∞(λ=1e-3, σ=0.1) = 0.006698379 (disco en forma cerrada + esquinas
Gauss-Legendre m=200/400/800, estable a 2e-12; extrapolación discreta de 3
puntos: 0.006698378). La aproximación log σ²/(2π)·ln(π/√λ)=0.007318911 NO es
el límite (+9.22 %).
"""

from __future__ import annotations

import importlib.util
import math
import pathlib
import sys

import numpy as np
import pytest

RUNNER_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_lambda_study_m6.py"

# Valores dorados (dos partes independientes, ronda 5): MSE(λ=0, rég. B,
# sólo-ruido, σ=0.1). Pin a ±5e-9: detecta CUALQUIER cambio de pesos,
# símbolos o normalización.
GOLDEN_MSE_LAMBDA0_REGIMEN_B = {
    32: 0.006793849,
    64: 0.007955333,
    128: 0.009087217,
    256: 0.010204638,
    512: 0.011314910,
}

GOLDEN_MSE_LAMBDA_1E3_N1024 = 0.006849252  # valor DISCRETO (ancla REV 5, intacta)
GOLDEN_V_INF_LAMBDA_1E3 = 0.006698379  # límite continuo EXACTO (REV 6, dos vías)


def _runner():
    spec = importlib.util.spec_from_file_location("run_lambda_study_m6", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _requiere_runner() -> None:
    if not RUNNER_PATH.exists():
        pytest.skip("runner M6 ausente (el test de presencia ya puso el job en FAIL)")


def _v_inf_oracle(lam: float, sigma: float, m: int = 300) -> float:
    """Oráculo INDEPENDIENTE del límite continuo del término de ruido:

    V∞(λ) = (σ²/(2π)²)·∫∫_{[-π,π]²} k²/(k²+λ)² dkx dky,

    evaluado como disco r≤π en forma cerrada (polar) + 4 esquinas por
    Gauss-Legendre anidado. NO importa ni reutiliza analytic_curve (suma
    discreta de la grilla): derivación y cuadratura son independientes."""
    x, wx = np.polynomial.legendre.leggauss(m)
    y, wy = np.polynomial.legendre.leggauss(m)
    xn = np.pi * (x + 1) / 2
    wxn = (np.pi / 2) * wx
    total = 0.0
    for xi, wi in zip(xn, wxn, strict=False):
        y0 = float(np.sqrt(max(np.pi**2 - xi**2, 0.0)))
        yy = (y + 1) / 2 * (np.pi - y0) + y0
        ww = ((np.pi - y0) / 2) * wy
        k2 = xi**2 + yy**2
        total += wi * float(np.sum(ww * k2 / (k2 + lam) ** 2))
    disco = np.pi * (np.log((np.pi**2 + lam) / lam) + lam / (np.pi**2 + lam) - 1.0)
    return (sigma**2 / (2 * np.pi) ** 2) * float(disco + 4.0 * total)


def test_runner_canonico_presente():
    """Fail-closed estructural (REV 6): la ausencia del runner canónico es
    FAIL SIEMPRE — sin variables de entorno, sin verde falso posible en CI."""
    if not RUNNER_PATH.exists():
        pytest.fail(
            "runner canónico scripts/run_lambda_study_m6.py AUSENTE: el contrato "
            "M6 no puede verificarse (FAIL, no SKIP silencioso)"
        )


def test_parseval_pesos_exactos_toda_paridad():
    """P2-1 (ronda 5): los pesos deben reproducir Σ|fft2(h)|² para par E impar.

    La regla histórica 'w[:, -1] = 1' sólo es correcta si la última columna
    rfft es Nyquist (N par); para N impar su espejo NO está almacenado y debe
    pesar 2. Este test falla si alguien regresa a esa regla."""
    _requiere_runner()
    runner = _runner()
    rng = np.random.default_rng(9)
    for n in (3, 4, 5, 8, 33, 64):
        h = rng.standard_normal((n, n))
        real = float(np.sum(np.abs(np.fft.fft2(h)) ** 2))
        con_pesos = float(np.sum(runner._parseval_weights(n) * np.abs(np.fft.rfft2(h)) ** 2))
        assert np.isclose(con_pesos, real, rtol=1e-12), (n, con_pesos / real)


def test_curva_lambda0_regimen_b_congelada():
    """P1-1: la curva usa los operadores D del solver; valores dorados fijos."""
    _requiere_runner()
    runner = _runner()
    for n, esperado in GOLDEN_MSE_LAMBDA0_REGIMEN_B.items():
        medido = float(runner.analytic_curve(n, 2.0, 0.1, [0.0], False, include_signal=False)[0] ** 2)
        assert abs(medido - esperado) < 5e-9, (n, medido, esperado)


def test_limite_continuo_v_infinito():
    """P1 (ronda 6): el límite del MSE de ruido con λ>0 fijo es V∞(λ) (integral
    continua del operador), NO la aproximación log σ²/(2π)·ln(π/√λ).

    - El oráculo del runner coincide con el cálculo independiente de este test.
    - La suma discreta converge a V∞ (decreciente; exceso <3 % ya en 1024²,
      medido ∝1/N).
    - La fórmula log queda >5 % por encima de V∞ en λ=1e-3: sólo es el
      comportamiento dominante para λ→0 (guarda anti-regresión documental)."""
    _requiere_runner()
    runner = _runner()
    v_oraculo = _v_inf_oracle(1e-3, 0.1)
    assert abs(v_oraculo - GOLDEN_V_INF_LAMBDA_1E3) < 5e-9, v_oraculo
    v_runner = float(runner.noise_mse_limit(1e-3, 0.1))
    assert abs(v_runner - v_oraculo) / v_oraculo < 1e-6, (v_runner, v_oraculo)
    v1024 = float(runner.analytic_curve(1024, 2.0, 0.1, [1e-3], False, include_signal=False)[0] ** 2)
    assert abs(v1024 - GOLDEN_MSE_LAMBDA_1E3_N1024) < 5e-9  # ancla REV 5 intacta
    assert abs(v1024 - v_oraculo) / v_oraculo < 0.03, (v1024, v_oraculo)
    v512 = float(runner.analytic_curve(512, 2.0, 0.1, [1e-3], False, include_signal=False)[0] ** 2)
    assert abs(v512 - v_oraculo) > abs(v1024 - v_oraculo)  # converge monotónicamente
    log_aprox = 0.1**2 / (2 * np.pi) * math.log(np.pi / math.sqrt(1e-3))
    assert abs(log_aprox - v_oraculo) / v_oraculo > 0.05, "la fórmula log NO es el límite"


def test_conjugacion_numerador_por_modo():
    """§3 (ronda 6): el numerador del estimador es conj(dx)·p̂ + conj(dy)·q̂
    (§2.2), NO la conjugación de la suma completa conj(dx·p̂ + dy·q̂).

    Modo único recuperable p=sin(3k0·x), q=0: la altura exacta es
    h(x)=−cos(3k0·x)/(3k0) (integrable; el solver la recupera a épsilon de
    máquina). La convención errónea conj(dx·p̂+dy·q̂) invierte el signo de la
    altura reconstruida y este test lo detecta (RMSE ≈ 2·RMS(h) > 1 en vez de
    ~0)."""
    _requiere_runner()
    runner = _runner()
    n = 64
    eje_x = 2 * np.pi * np.arange(n) / n
    k0 = 3.0 * 2 * np.pi / n  # modo 3 en radianes/muestra
    p = np.sin(3.0 * eje_x)[None, :] * np.ones((n, 1))
    h_est = runner.solve_screened(p, np.zeros((n, n)))
    h_ref = np.broadcast_to(-np.cos(3.0 * eje_x)[None, :] / k0, (n, n))
    rmse_ok = float(np.sqrt(np.mean((h_est - h_ref) ** 2)))
    assert rmse_ok < 1e-10, rmse_ok
    rmse_signo_invertido = float(np.sqrt(np.mean((h_est + h_ref) ** 2)))
    assert rmse_signo_invertido > 1.0  # la convención errónea es detectable


def test_ruido_lambda_positivo_acotado():
    """P1-2: λ=1e-3 acota el ruido; λ=0 crece ~ln N (misma curva, referencias
    separadas: valor discreto ancla vs límite continuo)."""
    _requiere_runner()
    runner = _runner()
    con_lambda = float(runner.analytic_curve(1024, 2.0, 0.1, [1e-3], False, include_signal=False)[0] ** 2)
    assert abs(con_lambda - GOLDEN_MSE_LAMBDA_1E3_N1024) < 5e-9
    asintota = 0.1**2 / (2 * np.pi) * math.log(np.pi / math.sqrt(1e-3))
    assert abs(con_lambda - asintota) / asintota < 0.15
    sin_lambda = float(runner.analytic_curve(1024, 2.0, 0.1, [0.0], False, include_signal=False)[0] ** 2)
    assert sin_lambda > con_lambda  # λ=0 crece; λ>0 acota


def test_selftest_completo_del_runner():
    """Los 9 recibos del runner (contraejemplo, null space, P1-3, MC×3,
    límite V∞ con separación log/límite, Parseval par/impar, exactitud
    integrable)."""
    _requiere_runner()
    assert _runner().selftest() == 9

"""Gate automatizado del contrato matemático M6 — núcleo FFT periódico sin
ponderar (research-only, insumo de #675 / EXP-M6).

Congela los recibos numéricos del runner canónico
``scripts/run_lambda_study_m6.py`` (SEED=20261015): pesos de Parseval rfft2
(toda paridad), curva E(λ) construida con los operadores D del solver,
asíntota de ruido con λ>0, y el selftest completo del runner (contraejemplo
Nyquist, espacio nulo, ceguera de la métrica de pendientes ante altura nula,
MC de espectro completo). Cualquier regresión de ponderación espectral, de
símbolos d o de la curva falla aquí.

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

El estudio completo (28 celdas) NO corre en CI: sólo el gate de recibos.
"""

from __future__ import annotations

import importlib.util
import math
import pathlib
import sys

import numpy as np
import pytest

RUNNER_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_lambda_study_m6.py"
)

pytestmark = pytest.mark.skipif(
    not RUNNER_PATH.exists(), reason="runner M6 (scripts/run_lambda_study_m6.py) no presente"
)

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

GOLDEN_MSE_LAMBDA_1E3_N1024 = 0.006849252  # vs asíntota (σ²/2π)·ln(π/√λ) = 0.007318911


def _runner():
    spec = importlib.util.spec_from_file_location("run_lambda_study_m6", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parseval_pesos_exactos_toda_paridad():
    """P2-1 (ronda 5): los pesos deben reproducir Σ|fft2(h)|² para par E impar.

    La regla histórica 'w[:, -1] = 1' sólo es correcta si la última columna
    rfft es Nyquist (N par); para N impar su espejo NO está almacenado y debe
    pesar 2. Este test falla si alguien regresa a esa regla."""
    runner = _runner()
    rng = np.random.default_rng(9)
    for n in (3, 4, 5, 8, 33, 64):
        h = rng.standard_normal((n, n))
        real = float(np.sum(np.abs(np.fft.fft2(h)) ** 2))
        con_pesos = float(
            np.sum(runner._parseval_weights(n) * np.abs(np.fft.rfft2(h)) ** 2)
        )
        assert np.isclose(con_pesos, real, rtol=1e-12), (n, con_pesos / real)


def test_curva_lambda0_regimen_b_congelada():
    """P1-1: la curva usa los operadores D del solver; valores dorados fijos."""
    runner = _runner()
    for n, esperado in GOLDEN_MSE_LAMBDA0_REGIMEN_B.items():
        medido = float(
            runner.analytic_curve(n, 2.0, 0.1, [0.0], False, include_signal=False)[0]
            ** 2
        )
        assert abs(medido - esperado) < 5e-9, (n, medido, esperado)


def test_asintota_ruido_lambda_positivo():
    """P1-2: λ=1e-3 acota el ruido en (σ²/2π)·ln(π/√λ); λ=0 crece ~ln N."""
    runner = _runner()
    con_lambda = float(
        runner.analytic_curve(1024, 2.0, 0.1, [1e-3], False, include_signal=False)[0]
        ** 2
    )
    assert abs(con_lambda - GOLDEN_MSE_LAMBDA_1E3_N1024) < 5e-9
    asintota = 0.1**2 / (2 * np.pi) * math.log(np.pi / math.sqrt(1e-3))
    assert abs(con_lambda - asintota) / asintota < 0.15
    sin_lambda = float(
        runner.analytic_curve(1024, 2.0, 0.1, [0.0], False, include_signal=False)[0]
        ** 2
    )
    assert sin_lambda > con_lambda  # λ=0 crece; λ>0 acota


def test_selftest_completo_del_runner():
    """Los 9 recibos del runner (contraejemplo, null space, P1-3, MC×3,
    asíntota, Parseval par/impar, exactitud integrable)."""
    assert _runner().selftest() == 9

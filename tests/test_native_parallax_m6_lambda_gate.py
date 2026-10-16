"""Gate automatizado del contrato matemático M6 — núcleo FFT periódico sin
ponderar (research-only, insumo de #675 / EXP-M6).

Congela los recibos numéricos del runner canónico
``scripts/run_lambda_study_m6.py`` (SEED=20261015): pesos de Parseval rfft2
(toda paridad), curva E(λ) construida con los operadores D del solver,
límite continuo V∞(λ) del término de ruido con λ>0 (REV 6), el Nyquist mixto
recuperable y los cuatro bins nulos de N par (REV 6.1), porcentajes calculados
con Decimal y anclas documentales. Cualquier regresión de ponderación
espectral, símbolos d, pseudoinversa, curva, límite o adjudicación documental
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
puntos: 0.006698378). La aproximación log es 0.007318911 (+9.2639% ≈ +9.26%);
N=1024, 0.006849252 (+2.2524% ≈ +2.25%); N=2048, 0.006773816
(+1.1262% ≈ +1.13%).
"""

from __future__ import annotations
from typing import Any

import importlib.util
import math
import pathlib
import sys
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pytest

RUNNER_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_lambda_study_m6.py"
DOC_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "docs"
    / "design"
    / "research"
    / "native-parallax"
    / "exp-m6-screen-lambda-derivation-proposal.md"
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

GOLDEN_MSE_LAMBDA_1E3_N1024 = 0.006849252  # valor DISCRETO (ancla REV 5, intacta)
GOLDEN_V_INF_LAMBDA_1E3 = 0.006698379  # límite continuo EXACTO (REV 6, dos vías)


def _runner() -> Any:
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
    - La suma discreta converge a V∞ (exceso <3 % en 1024²); en los tamaños
      registrados el exceso es aproximadamente proporcional a 1/N, no un
      teorema general.
    - Los porcentajes se verifican por separado desde los valores registrados;
      la fórmula log es sólo dominante para λ→0 (anti-regresión documental)."""
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


def _oraculo_derivadas_espectrales(altura: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Derivadas y proyección nula de referencia, sin usar el runner M6."""
    n = altura.shape[0]
    kx = (2 * np.pi * np.fft.fftfreq(n))[None, :]
    ky = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    dx, dy = 1j * kx, 1j * ky
    if n % 2 == 0:
        dx[:, n // 2] = 0.0
        dy[n // 2, :] = 0.0
    d = np.abs(dx) ** 2 + np.abs(dy) ** 2
    h_hat = np.fft.fft2(altura)
    p = np.fft.ifft2(dx * h_hat).real
    q = np.fft.ifft2(dy * h_hat).real
    recuperable = np.fft.ifft2(np.where(d > 0.0, h_hat, 0.0)).real
    return p, q, d, recuperable


def test_nyquist_mixto_x_reconstruible_y_fila_nyquist_no_se_elimina():
    """El Nyquist de un eje no anula el modo si el otro eje deriva.

    Oráculo analítico independiente para h=(-1)^x sin(6πy/64): p=0,
    q=(-1)^x(6π/64)cos(6πy/64), dx=0, dy=i·6π/64 y D>0. El modo simétrico
    con Nyquist en Y además impide borrar una fila Nyquist completa."""
    _requiere_runner()
    runner = _runner()
    n = 64
    x = np.arange(n)[None, :]
    y = np.arange(n)[:, None]
    ky = 6 * np.pi / n

    altura_x_nyquist = (-1.0) ** x * np.sin(6 * np.pi * y / n)
    p_x_nyquist = np.zeros_like(altura_x_nyquist)
    q_x_nyquist = (-1.0) ** x * ky * np.cos(6 * np.pi * y / n)
    dx, dy = 0.0j, 1j * ky
    d_mixto_x = abs(dx) ** 2 + abs(dy) ** 2
    assert np.max(np.abs(p_x_nyquist)) == 0.0
    assert d_mixto_x > 0.0
    recuperada = runner.solve_screened(p_x_nyquist, q_x_nyquist, 0.0)
    error = float(np.sqrt(np.mean((recuperada - altura_x_nyquist) ** 2)))
    assert error < 1e-12, error

    altura_y_nyquist = (-1.0) ** y * np.sin(6 * np.pi * x / n)
    p_y_nyquist = (-1.0) ** y * ky * np.cos(6 * np.pi * x / n)
    q_y_nyquist = np.zeros_like(altura_y_nyquist)
    dx, dy = 1j * ky, 0.0j
    d_mixto_y = abs(dx) ** 2 + abs(dy) ** 2
    assert np.max(np.abs(q_y_nyquist)) == 0.0
    assert d_mixto_y > 0.0
    recuperada_y = runner.solve_screened(p_y_nyquist, q_y_nyquist, 0.0)
    error_y = float(np.sqrt(np.mean((recuperada_y - altura_y_nyquist) ** 2)))
    assert error_y < 1e-12, error_y


def test_pseudoinversa_coherente_y_espacio_nulo_exacto_par_impar():
    """Compara el solver con derivadas FFT de referencia y enumera los bins nulos.

    En rfft2, N par tiene exactamente cuatro bins nulos; N impar no tiene
    Nyquist exacto y conserva sólo el bin de media constante como espacio nulo.
    El oráculo construye dx/dy directamente desde fftfreq, sin importar helpers
    ni máscaras del runner."""
    _requiere_runner()
    runner = _runner()
    for n in (64, 63):
        h = np.random.default_rng(n).standard_normal((n, n))
        p, q, d_full, h_recuperable = _oraculo_derivadas_espectrales(h)
        bins_nulos = set(zip(*np.where(d_full == 0.0), strict=True))
        esperado = {(0, 0), (0, n // 2), (n // 2, 0), (n // 2, n // 2)} if n % 2 == 0 else {(0, 0)}
        assert bins_nulos == esperado, (n, bins_nulos)

        kx = (2 * np.pi * np.fft.rfftfreq(n))[None, :]
        ky = (2 * np.pi * np.fft.fftfreq(n))[:, None]
        dx, dy = 1j * kx, 1j * ky
        if n % 2 == 0:
            dx[:, n // 2] = 0.0
            dy[n // 2, :] = 0.0
        d_rfft_oraculo = np.abs(dx) ** 2 + np.abs(dy) ** 2
        d_rfft_runner = runner._D_matrix(n)
        assert np.allclose(d_rfft_runner, d_rfft_oraculo, rtol=1e-13, atol=1e-15), n
        assert np.array_equal(d_rfft_runner == 0.0, d_rfft_oraculo == 0.0), n

        reconstruida = runner.solve_screened(p, q, 0.0)
        error = float(np.sqrt(np.mean((reconstruida - h_recuperable) ** 2)))
        assert error < 1e-11, (n, error)


def test_porcentajes_reconciliados_desde_los_valores_registrados() -> None:
    """Los porcentajes usan los datos congelados; el redondeo es sólo presentación."""
    limite = Decimal("0.006698379")
    valores = {
        "log": (Decimal("0.007318911"), Decimal("9.2639"), Decimal("9.26")),
        "N=1024": (Decimal("0.006849252"), Decimal("2.2524"), Decimal("2.25")),
        "N=2048": (Decimal("0.006773816"), Decimal("1.1262"), Decimal("1.13")),
    }
    for etiqueta, (valor, esperado_preciso, esperado_presentacion) in valores.items():
        porcentaje = (valor - limite) / limite * Decimal("100")
        preciso = porcentaje.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        presentacion = porcentaje.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert preciso == esperado_preciso, (etiqueta, preciso)
        assert presentacion == esperado_presentacion, (etiqueta, presentacion)


def test_documentacion_rev61_reconcilia_nyquist_y_porcentajes() -> None:
    """Ancla documental puntual: evita reintroducir las dos erratas adjudicadas."""
    assert DOC_PATH.is_file(), f"documento de REV 6.1 ausente: {DOC_PATH}"
    doc = DOC_PATH.read_text(encoding="utf-8")
    assert "REV_6_1_NYQUIST_NULLSPACE: CORRECTED" in doc
    assert "REV_6_1_NUMERIC_PERCENTAGES: RECONCILED" in doc
    adjudicacion = doc.split("### 8.5 REV 6.1 adjudicación", maxsplit=1)[1]
    for coordenada in (
        "(ky=0, kx=0)",
        "(ky=0, kx=N/2)",
        "(ky=N/2, kx=0)",
        "(ky=N/2, kx=N/2)",
    ):
        assert coordenada in adjudicacion
    assert "no se ha demostrado como teorema general" in " ".join(adjudicacion.split())

    seccion_vigente = doc.split("### 4.1", maxsplit=1)[1].split("### 4.2", maxsplit=1)[0]
    assert "D=0 — incluidos los Nyquist mixtos" not in seccion_vigente
    for porcentaje in ("+9.2639%", "+9.26%", "+2.2524%", "+2.25%", "+1.1262%", "+1.13%"):
        assert porcentaje in doc

    # Los porcentajes REV 6 originales se conservan sólo como errata fechada,
    # no como cifras activas en §4.1, docstrings ni Apéndices.
    for cifra_historica in ("+9.22 %", "+2.21 %", "+1.09 %"):
        posiciones = [i for i in range(len(doc)) if doc.startswith(cifra_historica, i)]
        assert posiciones, f"se perdió la procedencia histórica de {cifra_historica}"
        assert all(i >= doc.index("### 8.5 REV 6.1 adjudicación") for i in posiciones), cifra_historica

    runner = _runner()
    analytic_doc = runner.analytic_curve.__doc__ or ""
    limit_doc = runner.noise_mse_limit.__doc__ or ""
    assert "Nyquist mixtos — aportan 0" not in limit_doc
    assert "+9.2639%" in limit_doc
    assert "+2.2524%" in limit_doc
    assert "+1.1262%" in limit_doc
    assert "+9.22 %" not in limit_doc
    assert "acota a ≈(σ²/2π)·ln" not in analytic_doc

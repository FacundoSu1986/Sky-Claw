"""Estudio sintético de derivación de screen_lambda para EXP-M6 — v5.

REV 5 (2026-10-15, ronda 5): reconciliación acotada tras la revisión de la REV 4.
Cambios sobre v4 (commit 5c473d5), tres P2 aceptados:

1. PESOS DE PARSEVAL GENERALES (P2-1): la regla de columnas de v4 asumía N par
   (w[:, -1] = 1). La regla general —col. 0 peso 1; col. Nyquist peso 1 SÓLO si
   N es par; resto 2— es exacta para toda paridad (brute force contra fft2,
   N∈{3,5,7,8,32,33,64}; recibo de selftest). Mallas CUADRADAS; rectangulares
   FUERA de contrato (la extensión por eje-x es directa, no se usa aquí).
2. REWORDING λ* (P2-2): el escalado λ* ≈ c/N² es una OBSERVACIÓN bajo el prior
   declarado (no un teorema); la ganancia esperada es < 0.06 % en los escenarios
   examinados (máx. 0.053 % @128² régimen B; el enunciado "≤ 0.05 %" era falso).
3. GATE AUTOMATIZADO (P2-3): este archivo es el runner canónico en el repo;
   tests/test_native_parallax_m6_lambda_gate.py congela los recibos numéricos
   (SEED=20261015) y falla ante cualquier regresión de ponderación espectral,
   de símbolos d o de la curva E(λ).

Historial (detalle en docs/.../exp-m6-screen-lambda-derivation-proposal.md):
v3 (db5e5bd7) corrigió el defecto Nyquist del solver (contraejemplo RMSE
0.707107 → 4.8e-16) pero su curva analítica usaba una fórmula de dispersión y
pesos incorrectos. v4 (5c473d5) reconstruyó la curva con los operadores D del
solver y la regla de pesos rfft2 correcta para N par (valores λ=0 rég. B:
0.006794/0.007955/0.009087/0.010205 @N=32/64/128/256, σ=0.1 — verificados por
dos partes independientes al dígito). v5: esta.
"""

from __future__ import annotations

import sys

import numpy as np

N_VALUES = (256, 128)
BETAS = (2.0, 4.0)
SIGMAS = (0.01, 0.03, 0.1)
LAMBDAS = (0.0, 1e-5, 3e-5, 1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1, 3e-1, 1.0)
SEED = 20261015


# ---------------------------------------------------------------------------
# Operadores y solver (idénticos a v3: símbolos d coherentes)
# ---------------------------------------------------------------------------
def _d_symbols(n: int) -> tuple[np.ndarray, np.ndarray]:
    kx = (2 * np.pi * np.fft.rfftfreq(n))[None, :]
    ky = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    dx, dy = 1j * kx, 1j * ky
    if n % 2 == 0:
        dx[:, -1] = 0.0
        dy[n // 2, :] = 0.0
    return dx, dy


def solve_screened(p: np.ndarray, q: np.ndarray, lam: float = 0.0) -> np.ndarray:
    n = p.shape[0]
    dx, dy = _d_symbols(n)
    fp, fq = np.fft.rfft2(p), np.fft.rfft2(q)
    num = np.conj(dx) * fp + np.conj(dy) * fq
    num[0, 0] = 0.0
    denom = np.abs(dx) ** 2 + np.abs(dy) ** 2 + lam
    denom[denom == 0.0] = 1.0  # espacio nulo declarado: num ya es 0 ahí
    return np.fft.irfft2(num / denom, s=(n, n))


def _parseval_weights(n: int) -> np.ndarray:
    """Pesos de Parseval en la grilla rfft2 de una señal 1-D de largo n
    (shape (n, n//2+1); filas = ky completo, columnas = kx):
      - columna kx=0: completamente materializada (es su propio espejo) → 1
      - columna kx=n/2 (Nyquist, SÓLO si n es par): espejo en la MISMA
        columna ((-n/2) ≡ n/2 mod n) → 1
      - resto: bin propio + espejo no almacenado n-kx → 2
    Exacta para CUALQUIER paridad (verificado brute force contra fft2,
    N∈{3,5,7,8,32,33,64}; recibos del selftest). CONTRATO: mallas CUADRADAS;
    rectangulares FUERA de contrato (el espejo de rfft2 es sólo eje-x, la
    extensión es directa pero no se usa en este estudio)."""
    w = np.full((n, n // 2 + 1), 2.0)
    w[:, 0] = 1.0
    if n % 2 == 0:
        w[:, n // 2] = 1.0
    return w


def _D_matrix(n: int) -> np.ndarray:
    dx, dy = _d_symbols(n)
    d_mat: np.ndarray = np.abs(dx) ** 2 + np.abs(dy) ** 2
    return d_mat


def null_slope_energy_fraction(p: np.ndarray, q: np.ndarray) -> float:
    """Fracción de la ENERGÍA DE PENDIENTES que cae en el espacio nulo del
    solver (bins con D=0). IMPORTANTE (P1-3): NO mide altura perdida — una
    altura puramente en el espacio nulo deja pendientes nulas y esta métrica
    da 0. La energía de altura irrecuperable es UNKNOWN sin oráculo de altura;
    debe reportarse como tal en el manifest, junto a esta métrica."""
    n = p.shape[0]
    D = _D_matrix(n)
    null = D == 0.0
    fp, fq = np.fft.rfft2(p), np.fft.rfft2(q)
    w = _parseval_weights(n)  # pesos corregidos: sin ellos el numerador
    ep, eq = w * np.abs(fp) ** 2, w * np.abs(fq) ** 2  # (bins frontera) no es
    tot = float(np.sum(ep) + np.sum(eq)) + 1e-300  # comparable al denominador
    return float((np.sum(ep[null]) + np.sum(eq[null])) / tot)


# ---------------------------------------------------------------------------
# Modelo declarado
# ---------------------------------------------------------------------------
def make_signal(n: int, beta: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    idx = np.fft.fftfreq(n) * n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2 + KY**2) * (2 * np.pi / n)
    nyq = (np.abs(KX) == n // 2) | (np.abs(KY) == n // 2)
    band = (kn >= 2 * np.pi / n) & (kn <= np.pi / 4) & ~nyq
    coef = rng.standard_normal((n, n)) + 1j * rng.standard_normal((n, n))
    amp = np.zeros((n, n))
    amp[band] = kn[band] ** (-(beta + 2) / 2)
    h_hat = coef * amp
    h_hat[0, 0] = 0.0
    h = np.real(np.fft.ifft2(h_hat))
    h /= np.sqrt(np.mean(h**2))
    kxf = (2 * np.pi * np.fft.fftfreq(n))[None, :]
    kyf = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    hn = np.fft.fft2(h)
    hn[0, 0] = 0.0
    return h, np.real(np.fft.ifft2(1j * kxf * hn)), np.real(np.fft.ifft2(1j * kyf * hn))


def q8_noise(p: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Q8 SINTÉTICO: cuantización a 8 bits del mapa de normales. NO equivale a
    normales authored reales (eso es EXP-M2)."""
    nz = np.ones_like(p)
    nx, ny = -p, -q
    nrm = np.sqrt(nx**2 + ny**2 + nz**2)
    nx, ny, nz = nx / nrm, ny / nrm, nz / nrm
    dec_x = np.rint((nx * 0.5 + 0.5) * 255) / 255 * 2 - 1
    dec_y = np.rint((ny * 0.5 + 0.5) * 255) / 255 * 2 - 1
    return -dec_x / nz, -dec_y / nz


def drift_noise(p: np.ndarray, q: np.ndarray, sigma: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    n = p.shape[0]
    idx = np.fft.fftfreq(n) * n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2 + KY**2) * (2 * np.pi / n)
    band = (kn > 0) & (kn <= 16 * 2 * np.pi / n)
    ep = np.real(np.fft.ifft2((rng.standard_normal((n, n)) + 1j * rng.standard_normal((n, n))) * band))
    eq = np.real(np.fft.ifft2((rng.standard_normal((n, n)) + 1j * rng.standard_normal((n, n))) * band))
    ep *= sigma / np.sqrt(np.mean(ep**2))
    eq *= sigma / np.sqrt(np.mean(eq**2))
    return p + ep, q + eq


def _bandpass_mask(n: int) -> np.ndarray:
    idx = np.fft.fftfreq(n) * n
    KX, KY = np.meshgrid(idx, idx, indexing="xy")
    kn = np.sqrt(KX**2 + KY**2) * (2 * np.pi / n)
    nyq = (np.abs(KX) == n // 2) | (np.abs(KY) == n // 2)
    mask: np.ndarray = (kn >= 2 * np.pi / n) & (kn <= np.pi / 4) & ~nyq
    return mask


# ---------------------------------------------------------------------------
# Curva analítica CORREGIDA: D(k) de los símbolos del solver
# ---------------------------------------------------------------------------
def analytic_curve(
    n: int,
    beta: float,
    sigma: float,
    lams: np.ndarray | tuple[float, ...] | list[float],
    noise_filtered: bool,
    include_signal: bool = True,
) -> np.ndarray:
    """E[MSE(λ)] = (1/N⁴)·Σ_rfft w·(D/(D+λ))²·E|ĥ|²  [sesgo de señal]
                 + (1/N²)·Σ_rfft w·(D/(D+λ)²)·σ²              [ruido]
    con D(k)=|dx|²+|dy|² de los MISMOS símbolos del solver (P1-1) y pesos de
    pesos de Parseval de _parseval_weights (exacto para toda paridad). Régimen A (noise_filtered=True): ruido sólo en banda;
    Régimen B (False): ruido blanco en TODO bin con D>0. Con λ>0 fijo el ruido
    de B acota a ≈(σ²/2π)·ln(π/√λ); con λ=0 crece como (σ²/2π)·ln N (P1-2).
    include_signal=False expone sólo el término de ruido (para MC de ruido puro)."""
    kxr = (2 * np.pi * np.fft.rfftfreq(n))[None, :]
    kyr = (2 * np.pi * np.fft.fftfreq(n))[:, None]
    k = np.sqrt(kxr**2 + kyr**2)
    band = (k >= 2 * np.pi / n) & (k <= np.pi / 4)  # π/4 < π/2: sin Nyquist
    amp = np.zeros(band.shape)
    amp[band] = k[band] ** (-(beta + 2) / 2)
    eh = 2 * amp**2
    D = _D_matrix(n)
    w = _parseval_weights(n)
    eh *= n**4 / (w * eh).sum()  # RMS(altura)=1: Σ w·eh = N⁴
    out = []
    for lam in lams:
        mse = 0.0
        if include_signal:
            # sesgo = (ĥ_est − ĥ) = −λ/(D+λ)·ĥ  [NO la transmisión D/(D+λ)²]
            bias_fac = np.where(D > 0, (lam / np.maximum(D + lam, 1e-300)) ** 2, 0.0)
            mse += float(np.sum(w * bias_fac * eh)) / n**4
        if noise_filtered:
            nmask = band & (D > 0)
        else:
            nmask = D > 0
        if lam == 0.0:
            noise_sum = float(np.sum(w[nmask] / D[nmask]) * sigma**2)
        else:
            noise_sum = float(np.sum(w[nmask] * D[nmask] / (D[nmask] + lam) ** 2) * sigma**2)
        mse += noise_sum / n**2
        out.append(float(np.sqrt(mse)))
    return np.array(out)


def noise_mse_limit(lam: float, sigma: float, m: int = 300) -> float:
    """Límite continuo EXACTO del término de ruido con λ>0 fijo (REV 6, P1 de la
    ronda 6):

        V∞(λ) = (σ²/(2π)²)·∫∫_{[-π,π]²} k²/(k²+λ)² dkx dky,

    derivado de la suma discreta (1/N²)·Σ w·D/(D+λ)² → integral en el límite
    N→∞ (los bins con D=0 — incluidos los Nyquist mixtos — aportan 0 y tienen
    medida nula en el continuo). Evaluación INDEPENDIENTE de analytic_curve:
    disco r≤π en forma cerrada (polar) + 4 esquinas por Gauss-Legendre anidado
    (m×m por esquina; estable a 2e-12 desde m=200).

    IMPORTANTE: la aproximación log σ²/(2π)·ln(π/√λ) es sólo el comportamiento
    dominante para λ→0, NO el límite: en λ=1e-3 queda +9.22 % por encima de
    V∞ (0.007318911 vs 0.006698379 con σ=0.1). λ>0 acota el ruido pero NO
    recupera la señal atenuada (el sesgo (λ/(D+λ))² persiste)."""
    x, wx = np.polynomial.legendre.leggauss(m)
    y, wy = np.polynomial.legendre.leggauss(m)
    xn = np.pi * (x + 1) / 2
    wxn = (np.pi / 2) * wx
    esquinas = 0.0
    for xi, wi in zip(xn, wxn, strict=False):
        y0 = float(np.sqrt(max(np.pi**2 - xi**2, 0.0)))
        yy = (y + 1) / 2 * (np.pi - y0) + y0
        ww = ((np.pi - y0) / 2) * wy
        k2 = xi**2 + yy**2
        esquinas += wi * float(np.sum(ww * k2 / (k2 + lam) ** 2))
    disco = np.pi * (np.log((np.pi**2 + lam) / lam) + lam / (np.pi**2 + lam) - 1.0)
    integral: float = float(disco + 4.0 * esquinas)
    return (sigma**2 / (2 * np.pi) ** 2) * integral


def lambda_star_continuous(
    n: int, beta: float, sigma: float, noise_filtered: bool = True
) -> tuple[float, float, float]:
    fine = np.geomspace(1e-12, 1.0, 20000)
    e = analytic_curve(n, beta, sigma, fine, noise_filtered)
    i = int(np.argmin(e))
    e0 = analytic_curve(n, beta, sigma, [0.0], noise_filtered)[0]
    return float(fine[i]), float(e[i]), float(e0)


# ---------------------------------------------------------------------------
# Recibos (--check)
# ---------------------------------------------------------------------------
def selftest() -> int:
    ok = 0

    # 1. Contraejemplo Nyquist (REV 3, se mantiene).
    N = 64
    x = np.arange(N)[None, :]
    y = np.arange(N)[:, None]
    h = (-1.0) ** x * np.sin(6 * np.pi * y / N)
    p0 = np.zeros_like(h)
    q0 = (-1.0) ** x * (6 * np.pi / N) * np.cos(6 * np.pi * y / N)
    rmse = float(np.sqrt(np.mean((solve_screened(p0, q0) - h) ** 2)))
    assert rmse < 1e-10, rmse
    print(f"[ok] contraejemplo Nyquist-X recuperado por derivada-Y: RMSE={rmse:.2e}")
    ok += 1

    # 2. Espacio nulo real: tablero sigue nulo (P1-3: en su variante PENDIENTES).
    cb = (np.indices((64, 64)).sum(axis=0) % 2) * 2 - 1
    std = float(np.std(solve_screened(cb * 0.5, cb * 0.0)))
    assert std < 1e-12, std
    print(f"[ok] tablero (pendientes en bin nulo): std={std:.1e}")
    ok += 1

    # 3. P1-3: altura en el espacio nulo deja pendientes nulas — la métrica de
    #    pendientes NO puede verla; se declara UNKNOWN (sin oráculo).
    i = np.arange(64)
    hcb = (-1.0) ** (i[:, None] + i[None, :])
    p_cD = 0.5 * (np.roll(hcb, -1, 1) - np.roll(hcb, 1, 1))
    q_cD = 0.5 * (np.roll(hcb, -1, 0) - np.roll(hcb, 1, 0))
    assert float(np.abs(p_cD).max()) == 0.0 and float(np.abs(q_cD).max()) == 0.0
    assert null_slope_energy_fraction(p_cD, q_cD) == 0.0
    print(
        "[ok] h=(-1)^(x+y): pendientes≡0 ⇒ fracción pendiente-nula=0 — altura "
        "perdida UNKNOWN (requiere oráculo; separado en el manifest)"
    )
    ok += 1

    # 4. P1-1/P1-2: MC multi-resolución de ruido blanco ESPECTRO COMPLETO (λ=0)
    #    vs curva corregida (Régimen B) — incluye bandas mixtas de Nyquist.
    rng = np.random.default_rng(5)
    for n in (32, 64, 128):
        mses = []
        for _ in range(100):
            pe = 0.1 * rng.standard_normal((n, n))
            qe = 0.1 * rng.standard_normal((n, n))
            mses.append(float(np.mean(solve_screened(pe, qe) ** 2)))
        pred = analytic_curve(n, 2.0, 0.1, [0.0], noise_filtered=False, include_signal=False)[0] ** 2
        ratio = float(np.mean(mses)) / pred
        assert 0.93 < ratio < 1.07, (n, ratio)
        print(f"[ok] MC(100, N={n}, espectro completo) vs curva D: ratio={ratio:.4f}")
        ok += 1

    # 5. P1-2: λ fijo acota el ruido; λ=0 diverge con N (fórmula y números).
    lam = 1e-3
    vals = [
        analytic_curve(n, 2.0, 0.1, [lam], noise_filtered=False, include_signal=False)[0] ** 2
        for n in (128, 256, 512, 1024)
    ]
    assert vals[-1] < vals[1], vals  # decrece hacia V∞
    v_inf = noise_mse_limit(lam, 0.1)
    assert abs(v_inf - 0.006698379) < 5e-9, v_inf  # límite continuo (2 vías, ronda 6)
    assert abs(vals[-1] - v_inf) / v_inf < 0.03, (vals[-1], v_inf)  # exceso +2.21% @1024²
    asymp_log = 0.1**2 / (2 * np.pi) * np.log(np.pi / np.sqrt(lam))
    assert abs(asymp_log - v_inf) / v_inf > 0.05, (asymp_log, v_inf)  # log ≠ límite
    lam0_1024 = analytic_curve(1024, 2.0, 0.1, [0.0], noise_filtered=False, include_signal=False)[0] ** 2
    print(
        f"[ok] λ=1e-3: V∞(λ)={v_inf:.9f} es el límite continuo exacto; discreto "
        f"1024²={vals[-1]:.9f} (+{100 * (vals[-1] - v_inf) / v_inf:.2f}%, exceso ∝1/N); "
        f"la fórmula log σ²/(2π)·ln(π/√λ)={asymp_log:.9f} NO es el límite "
        f"(+{100 * (asymp_log - v_inf) / v_inf:.1f}%); λ=0 en 1024²: {lam0_1024:.6f}"
    )
    ok += 1

    # 6. Parseval rfft2 exacto para par E impar (P2-1, ronda 5): contra fft2.
    rngp = np.random.default_rng(9)
    for np_ in (4, 5, 8, 33, 64):
        hh = rngp.standard_normal((np_, np_))
        real = float(np.sum(np.abs(np.fft.fft2(hh)) ** 2))
        mía = float(np.sum(_parseval_weights(np_) * np.abs(np.fft.rfft2(hh)) ** 2))
        assert np.isclose(mía, real, rtol=1e-12), (np_, mía / real)
    print("[ok] Parseval rfft2 exacto (brute force vs fft2) N∈{4,5,8,33,64}")
    ok += 1

    # 7. Exactitud integrable sin Nyquist (regresión del solver).
    n = 128
    h2, p2, q2 = make_signal(n, 2.0, np.random.default_rng(1))
    e = float(np.sqrt(np.mean((solve_screened(p2, q2, 0.0) - h2) ** 2)))
    assert e < 1e-12, e
    print(f"[ok] campo integrable sin Nyquist: RMSE={e:.2e}")
    ok += 1

    print(f"selftest: {ok}/9 recibos OK")
    return ok


def main() -> None:
    if "--check" in sys.argv:
        selftest()
        return
    rng = np.random.default_rng(SEED)
    print(
        f"SEED={SEED} · banda [2π/N, π/4] sin Nyquist estricto · RMS(altura)=1 · "
        f"curva con operadores D del solver v3 + pesos Parseval rfft2 (col.0/Nyquist=1, resto=2) · "
        f"regímenes A(filtrado)/B(espectro completo) separados · mallas CUADRADAS (toda paridad; rectangulares fuera de contrato)"
    )
    for n in N_VALUES:
        for beta in BETAS:
            h, p, q = make_signal(n, beta, rng)
            lam_star_A, e_star_A, e0_A = lambda_star_continuous(n, beta, 0.1, True)
            lam_star_B, e_star_B, e0_B = lambda_star_continuous(n, beta, 0.1, False)
            casos = {
                **{
                    f"gauss σ={s}": (p + s * rng.standard_normal(p.shape), q + s * rng.standard_normal(p.shape), s)
                    for s in SIGMAS
                },
                "Q8 sintético": (*q8_noise(p, q), None),
                "limpio": (p, q, None),
                "driftLF σ=.05": (*drift_noise(p, q, 0.05, rng), 0.05),
                "driftLF σ=.5": (*drift_noise(p, q, 0.5, rng), 0.5),
            }
            for label, (pn, qn, sigma) in casos.items():
                sigma_eff = sigma if sigma is not None else float(np.sqrt(np.mean((pn - p) ** 2 + (qn - q) ** 2) / 2))
                rmses = [float(np.sqrt(np.mean((solve_screened(pn, qn, lam) - h) ** 2))) for lam in LAMBDAS]
                i_opt = int(np.argmin(rmses))
                e_f = analytic_curve(n, beta, sigma_eff, LAMBDAS, True)
                e_u = analytic_curve(n, beta, sigma_eff, LAMBDAS, False)
                print(
                    f"N={n:4d} β={beta:.0f} {label:13s} σ_eff={sigma_eff:.4f} | "
                    f"λ_opt_emp={LAMBDAS[i_opt]:g} (RMSE {rmses[i_opt]:.4f}) "
                    f"λ=0: {rmses[0]:.4f} (×{rmses[0] / rmses[i_opt]:.3f}) | "
                    f"E λ* A(filtr)={LAMBDAS[int(np.argmin(e_f))]:g} "
                    f"B(compl)={LAMBDAS[int(np.argmin(e_u))]:g} | "
                    f"λ*riesgo A(σ=.1)={lam_star_A:.1e} B={lam_star_B:.1e}"
                )


if __name__ == "__main__":
    main()

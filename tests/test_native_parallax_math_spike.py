"""Spike NP-M0: tests del núcleo matemático (research-only, sin production).

Estructura (§27 del brief):

- **Invariantes matemáticos**: estrictos, con tolerancias justificadas por eps de máquina.
- **Oracle de mutaciones (M1–M7)**: cada mutación conocida de la literatura DEBE ser
  detectada por las métricas; si no, el oracle es insuficiente.
- **Regresión relacional**: comparaciones entre corridas (A peor/mejor que B), sin
  thresholds absolutos congelados — los valores absolutos se registran en
  ``docs/design/research/native-parallax/np-m0-results.md`` y se analizan ahí.

Los targets provisionales del research (RMSE<0.05, seam<0.01, ángulo<15°) NO son
criterios de estos tests: aquí solo valen invariantes, detecciones de mutación y
relaciones de orden.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from sky_claw.local.native_parallax.research import metrics  # noqa: E402
from sky_claw.local.native_parallax.research.alignment import affine_fit, best_affine  # noqa: E402
from sky_claw.local.native_parallax.research.normal_fft_periodic import (  # noqa: E402
    freq_axes,
    integrate_periodic,
)
from sky_claw.local.native_parallax.research.normal_from_height import (  # noqa: E402
    gradients_from_normal,
    normals_from_gradients,
    quantize_decode,
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.synthetic_height import (  # noqa: E402
    NEGATIVE_CONTROLS,
    PERIODIC_CASES,
    boundary_wrap_ratio,
)

RES = 128  # resolución de tests: rápida y con margen respecto de Nyquist para S11


# ---------------------------------------------------------------------------
# A. INVARIANTES MATEMÁTICOS (estrictos)
# ---------------------------------------------------------------------------


class TestInvariantesMatematicos:
    def test_normales_unitarias_y_finitas(self) -> None:
        """§12: ‖N‖≈1 y sin NaN/Inf para todo el corpus (incluye controles)."""
        for name, fn in {**PERIODIC_CASES, **NEGATIVE_CONTROLS}.items():
            h = fn(64, 64)
            p, q = spectral_gradients(h)
            n = normals_from_gradients(p, q)
            assert np.all(np.isfinite(n)), name
            np.testing.assert_allclose(np.linalg.norm(n, axis=-1), 1.0, atol=1e-12, err_msg=name)

    def test_frecuencias_en_espacio_de_textura(self) -> None:
        """Convención documentada: wx = 2π·k (rad por unidad de x), Nyquist anulado."""
        wy, wx = freq_axes(8, 16)
        assert wx[0, 8] == 0.0  # Nyquist de W=16 (fftfreq lo da como -0.5)
        assert wy[4, 0] == 0.0  # Nyquist de H=8
        assert wx[0, 1] == pytest.approx(2 * np.pi)  # k=1 → 2π rad por unidad de x
        assert wy[1, 0] == pytest.approx(2 * np.pi)

    def test_superficie_plana_reconstruye_plana(self) -> None:
        rec, info = integrate_periodic(np.zeros((32, 32)), np.zeros((32, 32)))
        np.testing.assert_allclose(rec, 0.0, atol=1e-15)
        assert abs(info["mean"]) < 1e-15

    def test_seno_unico_reconstruccion_exacta(self) -> None:
        """S02 es band-limited: la reconstrucción debe ser EXACTA salvo DC (~eps)."""
        h = PERIODIC_CASES["S02_sine_x"](64, 64)
        _h, _p, _q, _n, rec, info = metrics.reconstruct_case(h)
        np.testing.assert_allclose(rec, h - h.mean(), atol=1e-10)
        assert info["max_imag_residual"] < 1e-12 * max(1.0, np.abs(h).max())

    def test_media_cero_en_todos_los_casos(self) -> None:
        """§15: el solver devuelve media definida ~0; NO se cuenta como error."""
        for name, fn in PERIODIC_CASES.items():
            h = fn(64, 64)
            _h, _p, _q, _n, rec, _info = metrics.reconstruct_case(h)
            assert abs(rec.mean()) < 1e-10 * max(1.0, np.abs(h).max()), name

    def test_residuo_imaginario_despreciable(self) -> None:
        """§41: la IFFT de un espectro Hermitiano debe dar campo real (~eps)."""
        for name in ("S06_multifreq", "S15_periodic_noise"):
            h = PERIODIC_CASES[name](64, 64)
            _h, _p, _q, _n, rec, info = metrics.reconstruct_case(h)
            assert info["max_imag_residual"] < 1e-10 * max(1e-300, np.abs(rec).max()), name

    def test_roundtrip_gradiente_por_normal_exacto(self) -> None:
        """N → (p,q) con los mismos signos recupera el gradiente exacto (float64)."""
        h = PERIODIC_CASES["S15_periodic_noise"](64, 64)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        p2, q2, hits = gradients_from_normal(n)
        np.testing.assert_allclose(p2, p, atol=1e-12)
        np.testing.assert_allclose(q2, q, atol=1e-12)
        assert hits == 0

    def test_determinismo_bit_a_bit(self) -> None:
        """§29: misma entrada → mismo output (S15 tiene RNG interna con seed fija)."""
        h1 = PERIODIC_CASES["S15_periodic_noise"](64, 64)
        h2 = PERIODIC_CASES["S15_periodic_noise"](64, 64)
        np.testing.assert_array_equal(h1, h2)
        _h, _p, _q, _n, rec1, _i = metrics.reconstruct_case(h1)
        _h, _p, _q, _n, rec2, _i = metrics.reconstruct_case(h2)
        np.testing.assert_array_equal(rec1, rec2)

    def test_periodicidad_del_dataset(self) -> None:
        """§11: S* periódicos por MÉTRICA (no a ojo); N* claramente fuera del contrato."""
        for name, fn in PERIODIC_CASES.items():
            ratio = boundary_wrap_ratio(fn(64, 64))
            assert 0.2 <= ratio <= 5.0, f"{name} ratio={ratio:.3f}"
        for name, fn in NEGATIVE_CONTROLS.items():
            ratio = boundary_wrap_ratio(fn(64, 64))
            assert ratio > 5.0, f"{name} ratio={ratio:.3f}"

    def test_metricas_finitas_en_corpus(self) -> None:
        for name in ("S01_flat", "S06_multifreq", "S13_near_flat", "N01_ramp", "N02_bump_boundary"):
            fn = PERIODIC_CASES.get(name) or NEGATIVE_CONTROLS[name]
            h = fn(64, 64)
            _h, p, q, n, rec, _info = metrics.reconstruct_case(h)
            m = metrics.compute_all(h, rec, p, q, n)
            assert m["finite"], name
            for key in ("rmse", "gradient_rmse", "normal_angle_mean", "seam_height"):
                assert np.isfinite(m[key]), (name, key)


# ---------------------------------------------------------------------------
# B. ORACLE DE MUTACIONES (§42): cada mutación DEBE ser detectada
# ---------------------------------------------------------------------------


def _integrate_sin_2pi(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """M1: frecuencias SIN el factor 2π (bug clásico documentado en el survey).

    Usa fftfreq·W (k entero) en vez de 2π·k → solución escalada por 1/(2π)."""
    h_, w_ = p.shape
    wx = (np.fft.fftfreq(w_) * w_).reshape(1, w_)
    wy = (np.fft.fftfreq(h_) * h_).reshape(h_, 1)
    den = wx * wx + wy * wy
    nulo = den == 0.0
    den[nulo] = 1.0
    h_hat = (-1j * wx * np.fft.fft2(p) - 1j * wy * np.fft.fft2(q)) / den
    h_hat[nulo] = 0.0
    return np.real(np.fft.ifft2(h_hat))  # type: ignore[no-any-return]


def _integrate_ejes_intercambiados(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """M3: usa wy para p y wx para q (swap de ejes de la FFT)."""
    h_, w_ = p.shape
    wy, wx = freq_axes(h_, w_)
    den = wx * wx + wy * wy
    nulo = den == 0.0
    den[nulo] = 1.0
    h_hat = (-1j * wy * np.fft.fft2(p) - 1j * wx * np.fft.fft2(q)) / den
    h_hat[nulo] = 0.0
    return np.real(np.fft.ifft2(h_hat))  # type: ignore[no-any-return]


class TestOracleMutaciones:
    def setup_method(self) -> None:
        h = PERIODIC_CASES["S06_multifreq"](64, 64)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        p2, q2, _ = gradients_from_normal(n)
        rec, _info = integrate_periodic(p2, q2)
        self.m = metrics.compute_all(h, rec, p, q, n)

    def test_m1_factor_2pi_escala_y_el_rmse_alineado_lo_absorbe(self) -> None:
        """§35 (el hallazgo central del audit): omitir 2π escala la solución ~2π; el
        fitting afín lo ABSORBE (rmse alineado ~0) pero gradient_rmse y ángulo normal
        explotan. Si el oracle no lo viera, el oráculo es insuficiente."""
        h = PERIODIC_CASES["S06_multifreq"](64, 64)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        rec_bad = _integrate_sin_2pi(p, q)
        m_bad = metrics.compute_all(h, rec_bad, p, q, n)
        # La trampa existe de verdad (documentarla en el test):
        assert m_bad["rmse"] < 1e-4, "2π debería ser absorbido por el fitting afín"
        # Y el oracle lo detecta igual:
        assert m_bad["gradient_rmse"] > 100.0 * self.m["gradient_rmse"]
        assert m_bad["normal_angle_mean"] > 1e3 * max(self.m["normal_angle_mean"], 1e-9)
        assert m_bad["scale_a"] == pytest.approx(1.0 / (2.0 * np.pi), rel=1e-6)

    def test_m2_flip_y_en_decodificacion_degrada_fuerte(self) -> None:
        h = PERIODIC_CASES["S06_multifreq"](64, 64)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        _p2, q2, _ = gradients_from_normal(n, sy=-1.0)  # convención equivocada de Y
        rec_bad, _info = integrate_periodic(p, q2)
        m_bad = metrics.compute_all(h, rec_bad, p, q, n)
        assert m_bad["rmse"] > 10.0 * self.m["rmse"]
        assert m_bad["normal_angle_mean"] > 10.0 * max(self.m["normal_angle_mean"], 1e-9)

    def test_m3_ejes_fft_intercambiados_en_rojo(self) -> None:
        h = PERIODIC_CASES["S06_multifreq"](64, 64)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        rec_bad = _integrate_ejes_intercambiados(p, q)
        m_bad = metrics.compute_all(h, rec_bad, p, q, n)
        assert m_bad["rmse"] > 10.0 * self.m["rmse"]

    def test_m4_sin_manejo_dc_nada_nan(self) -> None:
        """M4: sin guard, el 0/0 del modo DC produce valor inválido — numpy con
        ``errstate(invalid='raise')`` lo convierte en FloatingPointError (y la config
        del repo, en RuntimeWarning-as-error para divides sin errstate). El guard del
        solver es necesario y testeado."""
        p = np.zeros((8, 8))
        q = np.zeros((8, 8))
        wx_bad = 2.0 * np.pi * np.fft.fftfreq(8).reshape(1, 8) * 8
        wy_bad = 2.0 * np.pi * np.fft.fftfreq(8).reshape(8, 1) * 8
        den = wx_bad * wx_bad + wy_bad * wy_bad  # den[0,0] == 0 SIN guard
        with pytest.raises(FloatingPointError), np.errstate(invalid="raise"):
            _ = (-1j * wx_bad * np.fft.fft2(p) - 1j * wy_bad * np.fft.fft2(q)) / den
        # El solver real no explota con los mismos datos:
        rec, _info = integrate_periodic(p, q)
        assert np.all(np.isfinite(rec))

    def test_m5_gradientes_no_periodicos_detectados_por_seam(self) -> None:
        """M5: ground truth con diferencias finitas NO periódicas (np.gradient con bordes
        one-sided) → el error de frontera debe disparar seam_height vs el GT espectral."""
        h = PERIODIC_CASES["S06_multifreq"](128, 128)
        p_fd = np.gradient(h, axis=1)
        q_fd = np.gradient(h, axis=0)
        rec_fd, _info = integrate_periodic(p_fd, q_fd)
        m_fd = metrics.compute_all(h, rec_fd, *metrics.reconstruct_case(h)[1:3], normals_from_gradients(p_fd, q_fd))
        assert m_fd["seam_height"] > 50.0 * self.m["seam_height"] + 1e-12
        assert m_fd["rmse"] > 10.0 * self.m["rmse"]

    def test_m6_normales_sin_normalizar_son_invariantes_en_decode(self) -> None:
        """M6: nx/nz es invariante a escala — la normalización NO la protege el decode;
        por eso existe el invariante explícito ‖N‖=1 (contrato de formato, test arriba)."""
        h = PERIODIC_CASES["S06_multifreq"](32, 32)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        escalada = n * 0.5
        assert not np.allclose(np.linalg.norm(escalada, axis=-1), 1.0)
        p_a, q_a, _ = gradients_from_normal(n)
        p_b, q_b, _ = gradients_from_normal(escalada)
        np.testing.assert_allclose(p_a, p_b, atol=1e-15)
        np.testing.assert_allclose(q_a, q_b, atol=1e-15)

    def test_m7_alineamiento_es_solo_afin(self) -> None:
        """M7: ``a·rec+b`` NO puede absorber daño no lineal — si pudiera, el oracle
        estaría truncando errores. API expone solo fit afín; el daño no-afín sobrevive."""
        h = PERIODIC_CASES["S06_multifreq"](64, 64)
        _h, _p, _q, _n, rec, _info = metrics.reconstruct_case(h)
        danado = rec + 0.1 * np.sin(10.0 * rec)  # distorsión no afín
        a, b = affine_fit(danado, h)
        rmse_tras_fit = float(np.sqrt(np.mean((h - (a * danado + b)) ** 2)))
        assert rmse_tras_fit > 0.005  # el fitting no arregla lo no-afín
        assert best_affine(danado, h)["best_sign"] in (1.0, -1.0)


# ---------------------------------------------------------------------------
# C. REGRESIÓN RELACIONAL (sin thresholds absolutos congelados)
# ---------------------------------------------------------------------------


class TestRegresionRelacional:
    def test_corpus_periodico_completo_sano(self) -> None:
        """Sanity de regresión (NO criterio de aceptación): corr > 0.9 en todo S* a 128².
        S08/S09 contienen creases (no band-limited): se espera algo menos que 1 pero
        muy por encima de este piso."""
        for name, fn in PERIODIC_CASES.items():
            h = fn(RES, RES)
            _h, p, q, n, rec, _info = metrics.reconstruct_case(h)
            m = metrics.compute_all(h, rec, p, q, n)
            assert m["corr"] > 0.9, (name, m["corr"])
            assert m["best_sign"] == 1.0, (name, m["best_sign"])

    def test_q8_degrada_monotonamente_con_los_bits(self) -> None:
        h = PERIODIC_CASES["S06_multifreq"](RES, RES)
        _h, p, q, n, rec0, _i = metrics.reconstruct_case(h)
        m0 = metrics.compute_all(h, rec0, p, q, n)
        rmses = [m0["rmse"]]
        for bits in (16, 10, 8):
            _h, p, q, n, rec, _i = metrics.reconstruct_case(h)
            nd = quantize_decode(n, bits=bits)
            p2, q2, _hits = gradients_from_normal(nd)
            rec, _info = integrate_periodic(p2, q2)
            rmses.append(metrics.compute_all(h, rec, p, q, n)["rmse"])
        perfect, q16, q10, q8 = rmses
        assert q8 > q10 > q16 > perfect

    def test_ruido_degrada_monotonamente(self) -> None:
        h = PERIODIC_CASES["S06_multifreq"](RES, RES)
        _h, p, q, n, rec, _i = metrics.reconstruct_case(h)
        m0 = metrics.compute_all(h, rec, p, q, n)["rmse"]
        rng = np.random.default_rng(12345)
        rmses = []
        for sigma in (1 / 255, 2 / 255, 4 / 255, 8 / 255):
            nn = n + rng.normal(0.0, sigma, size=n.shape)
            nn = nn / np.linalg.norm(nn, axis=-1, keepdims=True)
            p2, q2, _h = gradients_from_normal(nn)
            rec, _i = integrate_periodic(p2, q2)
            rmses.append(metrics.compute_all(h, rec, p, q, n)["rmse"])
        assert rmses[3] > rmses[2] > rmses[1] > rmses[0] > m0

    def test_flip_xy_es_inversion_pura_visible_solo_por_signo(self) -> None:
        """§34-Q10: flip X+Y produce EXACTAMENTE -H (inversión sistemática). El RMSE
        alineado NO lo ve (a=-1 absorbe todo); lo ven best_sign, corr y normal_angle."""
        h = PERIODIC_CASES["S06_multifreq"](64, 64)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        p2, q2, _ = gradients_from_normal(n, sx=-1.0, sy=-1.0)
        rec, _info = integrate_periodic(p2, q2)
        m = metrics.compute_all(h, rec, p, q, n)
        assert m["rmse"] < 1e-8  # el fitting afín lo "arregla" con a=-1
        assert m["best_sign"] == -1.0
        assert m["corr"] < -0.999
        # HALLAZGO del spike: el ángulo normal MEDIO apenas rota en pendientes suaves
        # (las regiones planas dominan la media) — la inversión global la detectan
        # best_sign/corr, no el ángulo. Umbral bajo = sanity, análisis en el reporte.
        assert m["normal_angle_mean"] > 5.0

    def test_controles_no_periodicos_peores_que_periodicos(self) -> None:
        """§24: el solver debe ser bueno donde su contrato aplica y peor claramente fuera."""
        rmses_s = []
        seams_s = []
        for name, fn in PERIODIC_CASES.items():
            if name == "S01_flat":
                continue
            h = fn(RES, RES)
            _h, p, q, n, rec, _i = metrics.reconstruct_case(h)
            m = metrics.compute_all(h, rec, p, q, n)
            rmses_s.append(m["rmse"])
            seams_s.append(m["seam_height"])
        med_rmse = float(np.median(rmses_s))
        med_seam = float(np.median(seams_s))
        for name, fn in NEGATIVE_CONTROLS.items():
            h = fn(RES, RES)
            _h, p, q, n, rec, _i = metrics.reconstruct_case(h)
            m = metrics.compute_all(h, rec, p, q, n)
            assert m["rmse"] > 20.0 * med_rmse, (name, m["rmse"], med_rmse)
            assert m["seam_height"] > 50.0 * med_seam, (name, m["seam_height"], med_seam)

    def test_steep_nz_floor_se_cuenta_y_degrada(self) -> None:
        """§23: el floor no se esconde — se cuenta; clamping agresivo degrada el caso."""
        base = PERIODIC_CASES["S14_steep"](RES, RES)  # nz_min ~ 1e-2
        _h, _p, _q, n, _rec, _i = metrics.reconstruct_case(base)
        nz_min = float(n[..., 2].min())
        assert nz_min < 5e-2  # el caso es genuinamente empinado
        _hp, p, q, _n, rec6, _i = metrics.reconstruct_case(base, nz_floor=1e-6)
        _, _, _, _nn, rec2, _i2 = metrics.reconstruct_case(base, nz_floor=1e-2)
        m6 = metrics.compute_all(base, rec6, p, q, n)
        m2 = metrics.compute_all(base, rec2, p, q, n)
        assert m2["rmse"] >= m6["rmse"]  # clamping nunca mejora; puede degradar

    def test_banda_baja_concentra_el_error_de_ruido(self) -> None:
        """§18: la amplificación 1/|k|² del ruido de gradientes debe concentrar el error
        en la banda LOW (hipótesis relacional; si falla, el análisis del reporte cambia)."""
        h = PERIODIC_CASES["S06_multifreq"](RES, RES)
        _h, p, q, n, rec, _i = metrics.reconstruct_case(h)
        m0 = metrics.compute_all(h, rec, p, q, n)
        rng = np.random.default_rng(777)
        nn = n + rng.normal(0.0, 8 / 255, size=n.shape)
        nn = nn / np.linalg.norm(nn, axis=-1, keepdims=True)
        p2, q2, _h = gradients_from_normal(nn)
        rec, _i = integrate_periodic(p2, q2)
        m8 = metrics.compute_all(h, rec, p, q, n)
        assert m8["low_band_error"] > m0["low_band_error"] * 10.0
        assert m8["low_band_error"] > m8["high_band_error"]

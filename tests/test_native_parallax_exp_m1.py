"""EXP-M1: tests del spike de políticas nz/trust (research-only).

Estructura igual que NP-M0 (§27/§36 del brief):

- **Invariantes**: finitud, acotamiento de la regularización, contratos de estadística,
  sin defaults que permitan promover constantes absolutas (M5).
- **Mutaciones M1–M7**: cada mutación conocida DEBE ser detectada por la suite/métricas.
- **Regresión relacional**: órdenes entre políticas, sin thresholds absolutos congelados.

Los tests NO fijan la política ganadora: eso lo decide el experimento + revisión.
"""

from __future__ import annotations

import inspect

import pytest

np = pytest.importorskip("numpy")

from sky_claw.local.native_parallax.research import metrics as np_m0_metrics  # noqa: E402
from sky_claw.local.native_parallax.research.normal_fft_periodic import integrate_periodic  # noqa: E402
from sky_claw.local.native_parallax.research.normal_from_height import (  # noqa: E402
    normals_from_gradients,
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.nz_policies import (  # noqa: E402
    POLICIES,
    anti_flatten_metrics,
    decode_gradients_policy,
    quantize_xy_reconstruct_z,
)
from sky_claw.local.native_parallax.research.run_exp_m1 import (  # noqa: E402
    execute_case,
    nz_sweep_surface,
)
from sky_claw.local.native_parallax.research.synthetic_height import PERIODIC_CASES  # noqa: E402

SIGMA = 2.0 / 255


def _noisy_n(
    name: str = "S06_multifreq", res: int = 64, sigma: float = SIGMA, seed: int = 99
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    h = PERIODIC_CASES[name](res, res)
    p, q = spectral_gradients(h)
    n = normals_from_gradients(p, q)
    if sigma > 0:
        rng = np.random.default_rng(seed)
        n = n + rng.normal(0.0, sigma, size=n.shape)
        n = n / np.linalg.norm(n, axis=-1, keepdims=True)
    return h, p, q, n


def _reconstruct(n: np.ndarray, policy: str, lam: float, sigma: float) -> np.ndarray:
    p2, q2, _stats = decode_gradients_policy(n, 1.0, 1.0, policy, lam, sigma)
    rec, _info = integrate_periodic(p2, q2)
    return rec


# ---------------------------------------------------------------------------
# A. INVARIANTES
# ---------------------------------------------------------------------------


class TestInvariantes:
    def test_finito_en_regimen_extremo(self) -> None:
        """Todas las políticas devuelven gradientes finitos con nz≈0.001 y σ=8/255."""
        _h, _p, _q, n = _case_extremo()[:4]
        for policy in POLICIES:
            p2, q2, _s = decode_gradients_policy(n, 1.0, 1.0, policy, 8.0 / 255, 8.0 / 255)
            assert np.all(np.isfinite(p2)) and np.all(np.isfinite(q2)), policy

    def test_lam_y_sigma_sin_default(self) -> None:
        """M5 (invariante de diseño): ninguna política puede promoverse con una
        constante absoluta escondida — ``lam`` y ``sigma`` son obligatorios."""
        params = inspect.signature(decode_gradients_policy).parameters
        assert params["lam"].default is inspect.Parameter.empty
        assert params["sigma"].default is inspect.Parameter.empty
        # y no existe una política 'ABS' que disimule nz<0:
        assert all("ABS" not in p for p in POLICIES)

    def test_soft_tikhonov_acotado(self) -> None:
        """|g*| ≤ 1/(2λ) ⟹ gradiente por componente ≤ 1/(2λ); verificado empíricamente."""
        _h, _p, _q, n = _noisy_n()
        lam = 4.0 / 255
        p2, q2, stats = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", lam, SIGMA)
        bound = np.sqrt(2.0) / (2.0 * lam)  # |p|,|q| ≤ 1/(2λ) cada uno
        assert float(np.abs(p2).max()) <= 1.0 / (2.0 * lam) + 1e-12
        assert stats.max_gradient <= bound + 1e-9

    def test_soft_converge_a_raw_para_nz_grande(self) -> None:
        """nz ≫ λ ⟹ g* ≈ 1/nz: la regularización no toca la zona confiable.

        Se usa S13 (near-flat) porque S06 vive con nz~0.1-0.3 (poca zona confiable)."""
        _h, _p, _q, n = _noisy_n(name="S13_near_flat")
        nz = n[..., 2]
        lam = 2.0 / 255
        p_raw, q_raw, _ = decode_gradients_policy(n, 1.0, 1.0, "RAW", 0.0, SIGMA)
        p_soft, q_soft, _ = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", lam, SIGMA)
        mask = nz > 0.5  # nz/λ ≥ 64: desvío relativo teórico < λ²/nz² ≈ 2.4e-4
        assert mask.mean() > 0.9  # S13 es casi plana: casi todo es zona confiable
        np.testing.assert_allclose(p_soft[mask], p_raw[mask], rtol=2e-3)
        np.testing.assert_allclose(q_soft[mask], q_raw[mask], rtol=2e-3)

    def test_floor_zero_cuenta_invalidos_exactos(self) -> None:
        h, _amp = nz_sweep_surface(64, 64, 0.005)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        rng = np.random.default_rng(31)
        sigma = 4.0 / 255
        n = n + rng.normal(0.0, sigma, size=n.shape)
        n = n / np.linalg.norm(n, axis=-1, keepdims=True)
        nz = n[..., 2]
        assert np.count_nonzero(nz <= 0.0) > 0  # el caso debe tener inválidos reales
        p2, q2, stats = decode_gradients_policy(n, 1.0, 1.0, "FLOOR_ZERO", 4.0 / 255, SIGMA)
        invalidos = int(np.count_nonzero(nz <= 0.0))
        assert stats.negative_nz_fraction == pytest.approx(invalidos / nz.size)
        assert stats.activation_fraction == pytest.approx(invalidos / nz.size)
        assert np.all(p2[nz <= 0.0] == 0.0)
        assert np.all(q2[nz <= 0.0] == 0.0)

    def test_estadistica_negativa_independiente_de_politica(self) -> None:
        """M3: la evidencia nz<0 se reporta SIEMPRE (viene de la normal de entrada),
        incluso con políticas que la silencian en el gradiente (clamp/zero)."""
        _h, _p, _q, n = _noisy_n()
        nz = n[..., 2]
        rng = np.random.default_rng(7)
        n = n.copy()
        n[..., 2] = np.where(rng.random(nz.shape) < 0.01, -nz, nz)
        esperado = float(np.count_nonzero(n[..., 2] <= 0.0)) / n[..., 2].size
        for policy in POLICIES:
            _p2, _q2, stats = decode_gradients_policy(n, 1.0, 1.0, policy, 2.0 / 255, SIGMA)
            assert stats.negative_nz_fraction == pytest.approx(esperado), policy
        assert esperado > 0.0

    def test_q8xy_reconstruye_z_no_negativo(self) -> None:
        """Q8_XY_RECONSTRUCT_Z: nz ≥ 0 por construcción; el caso ' superficie plana'
        mantiene nz ≈ 1 con error de cuantización ~1 nivel."""
        n = np.zeros((32, 32, 3))
        n[..., 2] = 1.0
        n_q = quantize_xy_reconstruct_z(n, bits=8)
        assert np.all(n_q[..., 2] >= 0.0)
        assert float(n_q[..., 2].min()) > 0.995
        # y en pendiente extrema puede producir nz=0 exacto (contrato explícito):
        n_e = np.zeros((8, 8, 3))
        n_e[..., 0] = 1.0
        assert float(quantize_xy_reconstruct_z(n_e, bits=8)[..., 2].max()) == 0.0

    def test_determinismo(self) -> None:
        _h, _p, _q, n = _noisy_n()
        a = _reconstruct(n, "SOFT_TIKHONOV", 2.0 / 255, SIGMA)
        b = _reconstruct(n, "SOFT_TIKHONOV", 2.0 / 255, SIGMA)
        np.testing.assert_array_equal(a, b)


def _case_extremo() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, float]]:
    """Caso adversarial: nz_target=0.001, σ=8/255, todas las políticas."""
    h, _amp = nz_sweep_surface(64, 64, 0.001)
    p, q = spectral_gradients(h)
    n = normals_from_gradients(p, q)
    rng = np.random.default_rng(1234)
    n = n + rng.normal(0.0, 8.0 / 255, size=n.shape)
    n = n / np.linalg.norm(n, axis=-1, keepdims=True)
    p2, q2, _s = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", 8.0 / 255, 8.0 / 255)
    rec, info = integrate_periodic(p2, q2)
    return h, p, q, n, rec, info


# ---------------------------------------------------------------------------
# B. MUTACIONES (M1–M7)
# ---------------------------------------------------------------------------


class TestMutaciones:
    def test_m1_lambda_cero_degenera_a_raw(self) -> None:
        """SOFT con λ→0 es RAW (sin guard): si el test no lo viera, la familia soft
        estaría cambiando el comportamiento base sin que la suite lo note."""
        _h, _p, _q, n = _noisy_n()
        nz = n[..., 2]
        p_raw, q_raw, _ = decode_gradients_policy(n, 1.0, 1.0, "RAW", 0.0, SIGMA)
        p_s0, q_s0, _ = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", 0.0, SIGMA)
        mask = np.abs(nz) > 1e-9
        np.testing.assert_allclose(p_s0[mask], p_raw[mask], rtol=1e-12)
        np.testing.assert_allclose(q_s0[mask], q_raw[mask], rtol=1e-12)
        # FLOOR_CLAMP con λ=0 también es RAW (max(nz,0)=nz para nz>0; nz<=0 explota igual):
        p_c0, q_c0, _ = decode_gradients_policy(n, 1.0, 1.0, "FLOOR_CLAMP", 0.0, SIGMA)
        np.testing.assert_allclose(p_c0[mask & (nz > 0)], p_raw[mask & (nz > 0)], rtol=1e-12)
        assert p_c0.shape == p_raw.shape

    def test_m2_lambda_enorme_aplana_y_el_guard_lo_ve(self) -> None:
        """λ gigante 'resuelve' la inestabilidad aplastando el relieve: variance_ratio
        debe dejarlo visible (la métrica anti-flatten no es decorativa)."""
        h, p, q, n = _noisy_n(res=64)
        p2, q2, _ = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", 2.0 / 255, SIGMA)
        rec_ok, _ = integrate_periodic(p2, q2)
        p3, q3, _ = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", 64.0 / 255, SIGMA)
        rec_flat, _ = integrate_periodic(p3, q3)
        m_ok = anti_flatten_metrics(rec_ok, h, p, q)
        m_flat = anti_flatten_metrics(rec_flat, h, p, q)
        assert m_flat["variance_ratio"] < 0.5 * m_ok["variance_ratio"]
        assert m_ok["variance_ratio"] > 0.9  # la k razonable preserva el relieve

    def test_m3_abs_no_existe_como_politica(self) -> None:
        """M3 complementario: la única forma de 'abs(nz)' sería agregar la política;
        el invariante de diseño (sin ABS + stats siempre reportadas) lo bloquea."""
        assert not any("ABS" in p.upper() for p in POLICIES)
        _h, _p, _q, n = _noisy_n()
        for policy in POLICIES:
            _p2, _q2, stats = decode_gradients_policy(n, 1.0, 1.0, policy, 2.0 / 255, SIGMA)
            assert 0.0 <= stats.negative_nz_fraction <= 1.0
            assert 0.0 <= stats.activation_fraction <= 1.0

    def test_m4_fila_contracto_estadisticas(self) -> None:
        """M4/M6: toda fila del experimento trae activation/negative fractions y, si
        corresponde, las métricas anti-flatten (el RMSE alineado solo NO alcanza)."""
        h, _amp = nz_sweep_surface(64, 64, 0.005)
        row = execute_case(
            "T|contracto", h, sigma=SIGMA, quant="Q8", policy="SOFT_TIKHONOV", k=2.0, with_antiflatten=True
        )
        for key in ("activation_fraction", "negative_nz_fraction", "low_trust_fraction"):
            assert key in row
        for key in ("variance_ratio", "gradient_energy_ratio", "hf_energy_ratio"):
            assert key in row
        row_sin = execute_case("T|contracto2", h, sigma=SIGMA, quant="Q8", policy="RAW", k=0.0, with_antiflatten=False)
        assert "variance_ratio" not in row_sin  # solo se pide donde se declara

    def test_m7_relacion_escala_sigma_lambda(self) -> None:
        """Si λ se define relativo a σ, duplicar (σ, λ) juntos NO cambia los
        gradientes; cambiar σ sin λ sí. Ruptura de esa relación = policy malpecified."""
        _h, _p, _q, n = _noisy_n(sigma=0.0)  # normal limpia; σ solo entra en stats/trust
        _p_a, _q_a, stats_a = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", 2.0 * (1.0 / 255), 1.0 / 255)
        _p_b, _q_b, stats_b = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", 4.0 * (1.0 / 255), 2.0 / 255)
        assert stats_a.activation_fraction == pytest.approx(stats_b.activation_fraction)
        assert stats_a.low_trust_fraction == pytest.approx(stats_b.low_trust_fraction)
        _p_c, _q_c, stats_c = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", 2.0 * (1.0 / 255), 2.0 / 255)
        assert stats_c.low_trust_fraction >= stats_a.low_trust_fraction


# ---------------------------------------------------------------------------
# C. REGRESIÓN RELACIONAL
# ---------------------------------------------------------------------------


class TestRegresionRelacional:
    def test_politica_contiene_la_explosion_mejor_que_raw(self) -> None:
        """En el régimen catastrófico (nz_p01 < σ), las políticas acotan max_gradient
        órdenes de magnitud por debajo de RAW ( backbone del hallazgo esperado)."""
        h, _amp = nz_sweep_surface(64, 64, 0.005)
        p, q = spectral_gradients(h)
        n = normals_from_gradients(p, q)
        rng = np.random.default_rng(4242)
        sigma = 4.0 / 255
        n = n + rng.normal(0.0, sigma, size=n.shape)
        n = n / np.linalg.norm(n, axis=-1, keepdims=True)
        _, _q2, s_raw = decode_gradients_policy(n, 1.0, 1.0, "RAW", 0.0, sigma)
        _, _q3, s_clamp = decode_gradients_policy(n, 1.0, 1.0, "FLOOR_CLAMP", 2.0 * sigma, sigma)
        _, _q4, s_soft = decode_gradients_policy(n, 1.0, 1.0, "SOFT_TIKHONOV", 2.0 * sigma, sigma)
        assert s_clamp.max_gradient < s_raw.max_gradient / 100.0
        assert s_soft.max_gradient < s_raw.max_gradient / 100.0

    def test_r_ordena_la_inestabilidad(self) -> None:
        """A igual σ, menor nz_target ⇒ RAW monótonamente peor (curva de riesgo con
        pendiente, no ruido): ordena por nz_p01/σ."""
        gradient_rmses = []
        for nz_target in (0.25, 0.10, 0.05, 0.025):
            h, _amp = nz_sweep_surface(64, 64, nz_target)
            p, q = spectral_gradients(h)
            n = normals_from_gradients(p, q)
            rng = np.random.default_rng(555)
            n = n + rng.normal(0.0, SIGMA, size=n.shape)
            n = n / np.linalg.norm(n, axis=-1, keepdims=True)
            p2, q2, _ = decode_gradients_policy(n, 1.0, 1.0, "RAW", 0.0, SIGMA)
            rec, _i = integrate_periodic(p2, q2)
            m = np_m0_metrics.compute_all(h, rec, p, q, n)
            gradient_rmses.append(m["gradient_rmse"])
        assert gradient_rmses == sorted(gradient_rmses), gradient_rmses

    def test_anti_flatten_guardia_plano(self) -> None:
        h, p, q, _n = _noisy_n(sigma=0.0)
        m = anti_flatten_metrics(np.zeros_like(h), h, p, q)
        assert m["variance_ratio"] < 1e-9  # un mapa plano no puede pasar como éxito
        assert m["gradient_energy_ratio"] < 1e-9


class TestSelectorDosRegimenes:
    """Mecánica del selector (§24): guardia de detalle SOLO en survivable;
    contención excluye blasts; todo derivado de filas, sin constantes mágicas."""

    def _rows_fabricadas(self) -> list[dict]:
        rows = []
        for k in (1.0, 2.0, 4.0):
            rows.append(
                {
                    "case": "B|X|nz0.05|Q8",
                    "policy": "SOFT_TIKHONOV",
                    "k": k,
                    "sigma": 0.01,
                    "raw_centered_rmse": 0.1 * k,
                    "variance_ratio": 1.0,
                    "max_gradient": 10.0,
                }
            )
            rows.append(
                {
                    "case": "B|X|nz0.005|Q8",
                    "policy": "SOFT_TIKHONOV",
                    "k": k,
                    "sigma": 0.01,
                    "raw_centered_rmse": 5.0,
                    "variance_ratio": 0.01 * k,
                    "max_gradient": 100.0 * k,
                }
            )
        rows.append(
            {
                "case": "B|X|nz0.005|Q8",
                "policy": "RAW",
                "k": 0.0,
                "sigma": 0.01,
                "raw_centered_rmse": 99.0,
                "variance_ratio": 9.9,
                "max_gradient": 100000.0,
            }
        )
        return rows

    def test_guardia_detalle_ignora_regimen_duro(self) -> None:
        from sky_claw.local.native_parallax.research.run_exp_m1 import select_candidate_k

        k, med, mv = select_candidate_k(self._rows_fabricadas(), "SOFT_TIKHONOV")
        # k=1 gana por mediana survivable (0.1) pese a que en DURO su blast (100) es mayor;
        # la mediana survivable es la que ordena y la var del régimen duro NO participationa.
        assert k == 1.0
        assert mv >= 0.90
        assert med == pytest.approx(0.1)

    def test_contencion_excluye_blast(self) -> None:
        from sky_claw.local.native_parallax.research.run_exp_m1 import select_candidate_k

        rows = self._rows_fabricadas()
        # k=4 tiene blast duro de 400 = 0.4% del RAW (100k): pasa contención.
        # Forzamos un k=2 con blast desproporcionado para ver la exclusión:
        for r in rows:
            if r["k"] == 2.0 and "nz0.005" in r["case"]:
                r["max_gradient"] = 50000.0  # 50% del RAW: no contiene
        k, _med, _mv = select_candidate_k(rows, "SOFT_TIKHONOV")
        assert k in (1.0, 4.0)  # k=2 quedó excluido por contención

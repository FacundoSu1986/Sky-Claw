"""PR-MATH-A — invariantes canónicos de las primitivas matemáticas del proxy.

Congela UNA convención por primitiva y la ancla con campos ANALÍTICOS (sin corpus
externo, sin artefactos por-asset — §34 del brief PR-MATH-A):

- ``curl_z := ∂q/∂x − ∂p/∂y`` (§4). El negativo global sería equivalente sólo para
  métricas cuadráticas, por eso hay un test **firmado** y no sólo de energía (§29-M2).
- ``N = normalize((−sx·p, −sy·q, 1))`` ⟺ ``p = −nx/(sx·nz)``, ``q = −ny/(sy·nz)`` (§8).
  La inversa histórica ``p = −sx·nx/nz`` sólo coincide con la canónica cuando
  ``sx² = sy² = 1`` (§31).
- ``projection_residual`` y ``normal_height_residual_oracle`` reproyectan con
  ``normals_from_gradients`` — NO con ``nz = sqrt(max(0, 1−p²−q²))`` (§15/§16/§19).

Valores medidos que fijan las tolerancias (§30). Los tres primeros son la evidencia
RED: la implementación pre-fix daba la columna "defecto", no la "canónica".

===========================  ==================  ======================
magnitud                     canónica (piso)     defecto histórico
===========================  ==================  ======================
curl en campo integrable     5.8e-13 (max)       2.866e+02 (max)
round-trip sx=1.7, sy=0.6    2.7e-15 (max)        2.070e+01 (max)
projection_residual sx=1     0.0 deg (mediana)    169.95 deg (mediana)
projection_residual 1.7/0.6  0.0 deg (mediana)    157.46 deg (mediana)
oráculo (grid=100)           0.0 deg              9.42 deg
identidad de Hodge           1.3e-16 (rel)        n/a
===========================  ==================  ======================

Los tests son de **convención y geometría**: no fijan thresholds de M4/M5, no
rerunean M2/M3 y no tocan el corpus (§23/§24/§25/§34).
"""

from __future__ import annotations

import inspect

import pytest

np = pytest.importorskip("numpy")

from sky_claw.local.native_parallax.research import trust_proxies as tp  # noqa: E402
from sky_claw.local.native_parallax.research.normal_fft_periodic import (  # noqa: E402
    freq_axes,
    integrate_periodic,
)
from sky_claw.local.native_parallax.research.normal_from_height import (  # noqa: E402
    gradients_from_normal,
    normals_from_gradients,
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.nz_policies import (  # noqa: E402
    NZ_EPS_RAW,
    POLICIES,
    decode_gradients_policy,
)
from sky_claw.local.native_parallax.research.synthetic_height import PERIODIC_CASES  # noqa: E402

# Tolerancias derivadas del piso medido (float64 + FFT round-trip), no elegidas a ojo.
# 1e-9 está ~4 órdenes por encima del piso (5.8e-13) y ~11 por debajo del defecto (2.9e+02).
ATOL_CURL = 1e-9
ATOL_ROUNDTRIP = 1e-12
ATOL_IDENTIDAD_REL = 1e-10
PISO_ANGULAR_DEG = 1e-6


# ---------------------------------------------------------------------------
# Fixtures analíticos
# ---------------------------------------------------------------------------


def _campo_integrable(res: int = 64) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(h, p, q)`` de un height periódico band-limited con contenido en X e Y.

    Se elige S06 (modos hasta (7,5)) y NO un campo separable de un solo eje: un
    ``h`` accidentalmente simétrico con ``h_xx == h_yy`` escondería el defecto
    histórico ``dp/dx − dq/dy`` (§5 del brief). La no-degeneración se verifica en
    ``test_fixture_integrable_no_es_degenerado``.
    """
    h = PERIODIC_CASES["S06_multifreq"](res, res)
    p, q = spectral_gradients(h)
    return h, p, q


def _campo_no_integrable(res: int = 64) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(p, q, curl_analitico)`` de un campo vectorial que NO es gradiente de un escalar.

    ``p = −sin(2πy)``, ``q = sin(2πx)`` ⟹ ``∂q/∂x − ∂p/∂y = 2π(cos 2πx + cos 2πy)``.

    Propiedad que lo hace un fixture fuerte: el campo histórico
    ``∂p/∂x − ∂q/∂y`` vale **exactamente 0** sobre él (``p`` no depende de ``x`` y
    ``q`` no depende de ``y``), es decir reporta "perfectamente integrable" para un
    campo que no lo es. Sobre un toro el curl no puede tener signo constante
    (∫curl dA = ∮g·dl = 0), así que el campo tiene ambos signos por construcción.
    """
    xv = np.arange(res, dtype=np.float64) / res
    yv = np.arange(res, dtype=np.float64) / res
    xx, yy = np.meshgrid(xv, yv, indexing="xy")
    p = -np.sin(2.0 * np.pi * yy)
    q = np.sin(2.0 * np.pi * xx)
    curl_analitico = 2.0 * np.pi * (np.cos(2.0 * np.pi * xx) + np.cos(2.0 * np.pi * yy))
    return p, q, curl_analitico


def _curl_canonico(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """``curl_z = ∂q/∂x − ∂p/∂y`` (§4) — forma canónica, escrita explícitamente.

    Se escribe aquí a mano (y no se importa) para que el test congele la CONVENCIÓN
    y no la implementación: si alguien invierte el signo en ``trust_proxies``, este
    oráculo no lo sigue.
    """
    dq_dx = spectral_gradients(q)[0]
    dp_dy = spectral_gradients(p)[1]
    return dq_dx - dp_dy


def _rejilla_fourier_radianes(n_rows: int, n_cols: int) -> tuple[np.ndarray, np.ndarray]:
    """Rejilla de frecuencias ANGULARES derivada explícitamente para la identidad de Hodge (§7).

    ``wx = 2π·k`` con ``k = fftfreq(n_cols)·n_cols`` (ciclos enteros por unidad de
    textura) y ``wy`` análoga, con broadcast ``(wy (H,1), wx (1,W))``.

    Se restringe a tamaños **IMPARES**: en grillas pares ``freq_axes`` anula el bin de
    Nyquist (convención del solver) y esa anulación cambiaría tanto el operador curl
    como la ``|k|`` de la identidad — exactamente el motivo por el que el brief pide
    derivar la rejilla aquí en vez de reutilizar un ``rho`` genérico.
    """
    fx = np.fft.fftfreq(n_cols)
    fy = np.fft.fftfreq(n_rows)
    wx = 2.0 * np.pi * fx * n_cols
    wy = 2.0 * np.pi * fy * n_rows
    return wy.reshape(n_rows, 1), wx.reshape(1, n_cols)


# ---------------------------------------------------------------------------
# §3/§4/§5/§6 — curl_proxy
# ---------------------------------------------------------------------------


class TestCurlCanonico:
    def test_fixture_integrable_no_es_degenerado(self) -> None:
        """Guard de fixture (§5): el campo debe exponer el defecto, no esconderlo.

        Un ``h`` con ``h_xx == h_yy`` daría ``dp/dx − dq/dy ≡ 0`` y el test de curl
        pasaría por la razón equivocada. Se verifica con derivadas segundas
        espectrales, independientes de la convención bajo prueba.
        """
        _h, p, q = _campo_integrable()
        h_xx = spectral_gradients(p)[0]
        h_yy = spectral_gradients(q)[1]
        h_xy = spectral_gradients(p)[1]
        h_yx = spectral_gradients(q)[0]
        assert float(np.abs(h_xx - h_yy).max()) > 1.0, "fixture degenerado: h_xx ≈ h_yy"
        assert float(np.abs(h_xy - h_yx).max()) < ATOL_CURL, "las derivadas cruzadas no conmutan"

    def test_curl_integrable_es_piso_numerico(self) -> None:
        """§5: para ``p,q`` derivados de un height, el curl canónico es piso de float64.

        Medido: canónico 5.8e-13 (max) vs defecto histórico 2.866e+02 (max) — 15 órdenes.
        """
        _h, p, q = _campo_integrable()
        curl = _curl_canonico(p, q)
        assert float(np.abs(curl).max()) < ATOL_CURL

    def test_convencion_firmada_dq_dx_menos_dp_dy(self) -> None:
        """§29-M2: el test debe ser FIRMADO, no sólo de energía.

        Se compara elemento a elemento contra el curl analítico. Un test de energía
        (``sum |curl|²``) no distingue ``dq/dx − dp/dy`` de su negativo; éste sí,
        porque el campo tiene ambos signos y la comparación es punto a punto.
        """
        p, q, curl_analitico = _campo_no_integrable()
        curl = _curl_canonico(p, q)
        np.testing.assert_allclose(curl, curl_analitico, atol=ATOL_CURL)
        assert float(np.abs(curl_analitico).max()) > 1.0, "fixture sin curl: el test no discrimina"
        assert float(curl_analitico.min()) < -1.0 and float(curl_analitico.max()) > 1.0

    def test_curl_no_integrable_es_robustamente_no_nulo(self) -> None:
        """§6: sobre un campo no-integrable el curl canónico NO es piso numérico.

        Medido: mediana 4.579, max 12.57. Se exige separación de órdenes respecto del
        piso (1e-9), no un umbral ajustado al campo.
        """
        p, q, _ = _campo_no_integrable()
        curl = _curl_canonico(p, q)
        assert float(np.median(np.abs(curl))) > 1e-3
        assert float(np.abs(curl).max()) > 1.0

    def test_curl_distingue_integrable_de_no_integrable(self) -> None:
        """§6: el discriminante es la integrabilidad, verificada por la reconstrucción.

        ``integrate_periodic`` es la proyección L2 sobre campos integrables: si el
        campo es gradiente, la reconstrucción lo recupera; si no, el residuo no se
        anula. Se cruzan ambas señales (curl + residuo) para no depender de una sola.
        """
        _h, p_int, q_int = _campo_integrable()
        p_no, q_no, _ = _campo_no_integrable()

        curl_int = float(np.abs(_curl_canonico(p_int, q_int)).max())
        curl_no = float(np.abs(_curl_canonico(p_no, q_no)).max())
        assert curl_no > 1e6 * max(curl_int, 1e-300), "curl no separa integrable de no-integrable"

        h_rec_int, _ = integrate_periodic(p_int, q_int)
        pi_rec, qi_rec = spectral_gradients(h_rec_int)
        residuo_int = max(float(np.abs(pi_rec - p_int).max()), float(np.abs(qi_rec - q_int).max()))

        h_rec_no, _ = integrate_periodic(p_no, q_no)
        pn_rec, qn_rec = spectral_gradients(h_rec_no)
        residuo_no = max(float(np.abs(pn_rec - p_no).max()), float(np.abs(qn_rec - q_no).max()))

        assert residuo_int < ATOL_CURL
        assert residuo_no > 1e-2

    def test_convencion_firmada_en_la_primitiva_publica(self) -> None:
        """§29-M2: el signo debe ser OBSERVABLE en la primitiva pública, no sólo agregado.

        Hallazgo del propio mutation testing: ``curl_proxy`` expone únicamente
        ``median_abs``/``mad``/``p95_abs``, todas invariantes al signo — un test apoyado
        en ellas deja sobrevivir al mutante ``dp/dy − dq/dx``. Por eso la convención se
        expone como campo (``curl_field``) y se compara punto a punto contra el curl
        analítico. La invariancia del agregado se deja documentada como tal.
        """
        p, q, curl_analitico = _campo_no_integrable()
        np.testing.assert_allclose(tp.curl_field(p, q), curl_analitico, atol=ATOL_CURL)
        # El agregado es ciego al signo por construcción (no es un bug, es el contrato):
        reportado = tp.curl_proxy(p, q)
        assert reportado["curl_median_abs"] == pytest.approx(float(np.median(np.abs(curl_analitico))), abs=1e-12)

    def test_curl_proxy_publico_usa_la_convencion_canonica(self) -> None:
        """La primitiva pública ``curl_proxy`` debe reportar el curl canónico, no otro.

        Se compara la estadística que expone contra el cálculo explícito del test.
        """
        p, q, _ = _campo_no_integrable()
        reportado = tp.curl_proxy(p, q)
        curl = _curl_canonico(p, q)
        assert reportado["curl_median_abs"] == pytest.approx(float(np.median(np.abs(curl))), abs=1e-12)
        assert reportado["curl_p95_abs"] == pytest.approx(float(np.percentile(np.abs(curl), 95)), abs=1e-12)

    def test_curl_proxy_integrable_reporta_piso(self) -> None:
        """Sobre un height real el proxy debe reportar piso, no ``h_xx − h_yy``."""
        _h, p, q = _campo_integrable()
        assert tp.curl_proxy(p, q)["curl_median_abs"] < ATOL_CURL


# ---------------------------------------------------------------------------
# §7 — identidad de Hodge / Parseval
# ---------------------------------------------------------------------------


class TestIdentidadHodge:
    def test_rejilla_explicita_coincide_con_el_solver(self) -> None:
        """La rejilla derivada en el test debe ser la MISMA que usa el operador curl."""
        wy, wx = _rejilla_fourier_radianes(63, 65)
        wy_ref, wx_ref = freq_axes(63, 65)
        np.testing.assert_allclose(wy, wy_ref, atol=1e-12)
        np.testing.assert_allclose(wx, wx_ref, atol=1e-12)
        assert wx[0, 32] != 0.0, "grilla impar: no debe haber bins de Nyquist anulados"

    def test_parseval_curl_energia_espectral(self) -> None:
        """§7: ``sum |curl|² ≈ sum |k|² |g_perp|²`` para la convención Fourier del repo.

        Derivación explícita (no copiada). Con ``F{∂/∂x} = i·wx`` y ``F{∂/∂y} = i·wy``:

            F{curl} = i·wx·q̂ − i·wy·p̂  ⟹  A_k := wx·q̂ − wy·p̂,  |F{curl}|² = |A_k|²

        ``A_k`` es la componente transversal (``k × ĝ``) del campo ``g = (p, q)``; con
        ``|k| = sqrt(wx² + wy²)`` se tiene ``|A_k|² = |k|²·|ĝ_perp|²``. Por Parseval de
        ``numpy.fft`` (``sum_x |f|² = (1/N)·sum_k |f̂|²``, con ``N = H·W``) queda

            sum_pixeles |curl|² = (1/N) · sum_k |k|² · |ĝ_perp|²

        El modo DC (``k = 0``) se excluye: allí ``|k| = 0`` y ``ĝ_perp`` no está definido
        (ambos miembros valen 0 de todos modos).
        """
        rng = np.random.default_rng(12345)
        n_rows, n_cols = 63, 65  # impares: sin bins de Nyquist (§7)
        p = rng.normal(0.0, 1.0, (n_rows, n_cols))
        q = rng.normal(0.0, 1.0, (n_rows, n_cols))

        curl = _curl_canonico(p, q)
        energia_pixeles = float(np.sum(curl * curl))

        wy, wx = _rejilla_fourier_radianes(n_rows, n_cols)
        k2 = wx * wx + wy * wy
        no_dc = k2 > 0.0
        a_k = wx * np.fft.fft2(q) - wy * np.fft.fft2(p)
        g_perp2 = np.zeros_like(k2)
        g_perp2[no_dc] = np.abs(a_k[no_dc]) ** 2 / k2[no_dc]

        energia_espectral = float(np.sum(k2 * g_perp2)) / (n_rows * n_cols)
        assert energia_pixeles > 1.0, "fixture sin energía: la identidad no se ejercita"
        np.testing.assert_allclose(energia_espectral, energia_pixeles, rtol=ATOL_IDENTIDAD_REL)

        # El puente intermedio |A_k|² también se congela (evita que un error en
        # g_perp compense un error en k²).
        np.testing.assert_allclose(
            float(np.sum(np.abs(a_k) ** 2)) / (n_rows * n_cols),
            energia_pixeles,
            rtol=ATOL_IDENTIDAD_REL,
        )


# ---------------------------------------------------------------------------
# §8/§9/§11/§12/§31 — contrato normal ↔ gradiente
# ---------------------------------------------------------------------------


class TestContratoNormalGradiente:
    @pytest.mark.parametrize(("sx", "sy"), [(1.0, 1.0), (1.7, 0.6), (0.5, 2.0)])
    def test_roundtrip_anisotropico_exacto(self, sx: float, sy: float) -> None:
        """§11: ``(p,q) → N → (p,q)`` exacto cuando no hay ``nz_floor_hits``.

        Medido: error canónico 1.8e-15 … 2.7e-15 (max). El caso ``sx=sy=1`` permanece
        compatible con el histórico (§31).
        """
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=sx, sy=sy)
        p2, q2, hits = gradients_from_normal(n, sx=sx, sy=sy)
        assert hits == 0, "el fixture no debe activar el floor"
        np.testing.assert_allclose(p2, p, atol=ATOL_ROUNDTRIP)
        np.testing.assert_allclose(q2, q, atol=ATOL_ROUNDTRIP)

    def test_compatibilidad_sx_sy_uno(self) -> None:
        """§31: con ``sx=sy=1`` canónica e histórica son numéricamente idénticas.

        Protege la interpretación "M4/M5 primary no invalidado por la corrección del
        factor geométrico": en esa ruta el factor no cambia nada.
        """
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=1.0, sy=1.0)
        p_canon, q_canon, _ = gradients_from_normal(n, sx=1.0, sy=1.0)
        nz_eff = np.maximum(n[..., 2], 1e-6)
        p_hist = -1.0 * n[..., 0] / nz_eff
        q_hist = -1.0 * n[..., 1] / nz_eff
        np.testing.assert_array_equal(p_canon, p_hist)
        np.testing.assert_array_equal(q_canon, q_hist)

    def test_inversa_historica_falla_con_sx_no_unitario(self) -> None:
        """§12/§29-M3: ``p = −sx·nx/nz`` sólo vale si ``sx² = 1``.

        Se congela el error relativo medido (7.8e+15 × el piso) para que el test siga
        discriminando si alguien reintroduce la fórmula histórica.
        """
        _h, p, q = _campo_integrable()
        sx, sy = 1.7, 0.6
        n = normals_from_gradients(p, q, sx=sx, sy=sy)
        p_hist = -sx * n[..., 0] / np.maximum(n[..., 2], 1e-6)
        q_hist = -sy * n[..., 1] / np.maximum(n[..., 2], 1e-6)
        err_hist = max(float(np.abs(p_hist - p).max()), float(np.abs(q_hist - q).max()))
        assert err_hist > 1.0, "la inversa histórica debería fallar con sx≠1"

        p_canon, q_canon, _ = gradients_from_normal(n, sx=sx, sy=sy)
        err_canon = max(float(np.abs(p_canon - p).max()), float(np.abs(q_canon - q).max()))
        assert err_hist > 1e6 * max(err_canon, 1e-300)

    @pytest.mark.parametrize(("sx", "sy", "eje"), [(0.0, 1.0, "sx"), (1.0, 0.0, "sy")])
    def test_guard_sx_sy_cero(self, sx: float, sy: float, eje: str) -> None:
        """§9: ``sx == 0`` / ``sy == 0`` es error explícito, no división silenciosa.

        No se inventa epsilon: el contrato es ``ValueError`` (misma semántica en la
        inversa y en las políticas).
        """
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=1.0, sy=1.0)
        with pytest.raises(ValueError, match=eje):
            gradients_from_normal(n, sx=sx, sy=sy)

    def test_guard_sx_sy_cero_en_politicas(self) -> None:
        """§9: ``decode_gradients_policy`` comparte el mismo guard."""
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=1.0, sy=1.0)
        with pytest.raises(ValueError, match="sx"):
            decode_gradients_policy(n, 0.0, 1.0, "RAW", 0.0, 2.0 / 255)
        with pytest.raises(ValueError, match="sy"):
            decode_gradients_policy(n, 1.0, 0.0, "RAW", 0.0, 2.0 / 255)


# ---------------------------------------------------------------------------
# §13 — geometría de las políticas nz
# ---------------------------------------------------------------------------


class TestPoliticasNzGeometria:
    @pytest.mark.parametrize("policy", POLICIES)
    def test_factor_geometrico_divide_no_multiplica(self, policy: str) -> None:
        """§13: el factor ``sx/sy`` DIVIDE. Cada policy se compara con su forma explícita.

        Las policies que regulan ``1/nz`` deben aplicar esa regularización a ``nx/sx``,
        ``ny/sy`` — no volver a multiplicar por ``sx/sy``.
        """
        sx, sy = 1.7, 0.6
        lam = 1e-3
        sigma = 2.0 / 255
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=sx, sy=sy)
        nx, ny, nz = n[..., 0], n[..., 1], n[..., 2]

        p_out, q_out, _stats = decode_gradients_policy(n, sx, sy, policy, lam, sigma)

        if policy == "RAW":
            nz_guard = np.where(np.abs(nz) < NZ_EPS_RAW, NZ_EPS_RAW, nz)
            exp_p, exp_q = -nx / (sx * nz_guard), -ny / (sy * nz_guard)
        elif policy == "FLOOR_CLAMP":
            nz_eff = np.maximum(nz, lam)
            exp_p, exp_q = -nx / (sx * nz_eff), -ny / (sy * nz_eff)
        elif policy == "FLOOR_ZERO":
            valid = nz > 0.0
            safe = np.where(valid, nz, 1.0)
            exp_p = np.where(valid, -nx / (sx * safe), 0.0)
            exp_q = np.where(valid, -ny / (sy * safe), 0.0)
        else:  # SOFT_TIKHONOV
            inv_reg = nz / (nz * nz + lam * lam)
            exp_p, exp_q = -nx * inv_reg / sx, -ny * inv_reg / sy

        np.testing.assert_allclose(p_out, exp_p, atol=ATOL_ROUNDTRIP)
        np.testing.assert_allclose(q_out, exp_q, atol=ATOL_ROUNDTRIP)

    def test_roundtrip_politica_raw_anisotropica(self) -> None:
        """§13: con ``RAW`` (sin regularización activa) el decode recupera el GT exacto."""
        sx, sy = 1.7, 0.6
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=sx, sy=sy)
        p2, q2, _ = decode_gradients_policy(n, sx, sy, "RAW", 0.0, 2.0 / 255)
        np.testing.assert_allclose(p2, p, atol=ATOL_ROUNDTRIP)
        np.testing.assert_allclose(q2, q, atol=ATOL_ROUNDTRIP)


# ---------------------------------------------------------------------------
# §15/§16/§17/§18 — projection_residual
# ---------------------------------------------------------------------------


class TestProjectionResidual:
    def test_floor_del_proxy_es_explicito(self) -> None:
        """§17: la ruta del proxy fija su piso numérico explícitamente (histórico 1e-30).

        ``gradients_from_normal`` tiene default 1e-6; el proxy usaba 1e-30. El fix no
        debe cambiar la política numérica del proxy como efecto lateral.
        """
        assert tp.PROXY_NZ_FLOOR == 1e-30
        assert inspect.signature(tp.projection_residual).parameters["sx"].default is inspect.Parameter.empty

    @pytest.mark.parametrize(("sx", "sy"), [(1.0, 1.0), (1.7, 0.6), (0.5, 2.0)])
    def test_residuo_canonico_es_piso(self, sx: float, sy: float) -> None:
        """§18: height conocido → normal canónica → residuo angular ≈ piso numérico.

        Medido: canónico 0.0 deg vs defecto histórico 169.95 / 157.46 / 167.08 deg.
        Incluye caso anisotrópico (§18).
        """
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=sx, sy=sy)
        res = tp.projection_residual(n, sx=sx, sy=sy)
        assert res["projection_residual_median_deg"] < PISO_ANGULAR_DEG
        assert res["projection_residual_p95_deg"] < 1e-3

    def test_residuo_no_usa_sqrt_de_pendientes(self) -> None:
        """§15: la reproyección no puede reconstruir la normal con ``sqrt(1−p²−q²)``.

        Esa fórmula es geométricamente incorrecta: sobre un campo con pendientes
        grandes produce residuales de decenas de grados. Se verifica contra el valor
        medido del defecto (157 deg) para dejar margen de órdenes.
        """
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=1.7, sy=0.6)
        res = tp.projection_residual(n, sx=1.7, sy=0.6)
        assert res["projection_residual_median_deg"] < 1.0

    def test_residuo_detecta_normal_incoherente(self) -> None:
        """Contra-prueba: una normal que NO viene de un height debe dar residual alto.

        Sin esto, un ``projection_residual`` que devolviera 0 siempre pasaría los
        tests anteriores. Se perturba la normal de forma no integrable.
        """
        _h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=1.0, sy=1.0)
        p_no, q_no, _ = _campo_no_integrable()
        n_no = normals_from_gradients(p_no, q_no, sx=1.0, sy=1.0)
        coherente = tp.projection_residual(n, sx=1.0, sy=1.0)["projection_residual_median_deg"]
        incoherente = tp.projection_residual(n_no, sx=1.0, sy=1.0)["projection_residual_median_deg"]
        assert incoherente > 1e3 * max(coherente, 1e-12)


# ---------------------------------------------------------------------------
# §19/§21 — normal_height_residual_oracle
# ---------------------------------------------------------------------------


class TestOraculoNormalHeight:
    def test_oraculo_floor_fuerte_con_strength_en_la_rejilla(self) -> None:
        """§21: con el strength correcto DENTRO de la rejilla, el residuo es piso exacto.

        ``linspace(0.05, 5.0, 100)`` tiene paso 0.05 ⟹ contiene ``s = 1.0``. Medido:
        canónico 0.0 deg con ``best_strength = 1.0``; histórico 9.42 deg con
        ``best_strength = −0.20`` (el signo negativo delata que el oráculo histórico
        compensaba el flip XY invirtiendo la superficie entera).
        """
        h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=1.0, sy=1.0)
        res = tp.OracleOnly.normal_height_residual_oracle(n, h, strength_grid=100)
        assert res["height_normal_oracle_agreement_deg"] < PISO_ANGULAR_DEG
        assert res["oracle_best_strength"] == pytest.approx(1.0, abs=1e-9)

    @pytest.mark.parametrize(("sx", "sy"), [(1.0, 1.0), (1.7, 0.6)])
    def test_oraculo_grid_default_bajo_un_grado(self, sx: float, sy: float) -> None:
        """§21: con el grid por defecto (25) el piso lo fija la discretización, no la fórmula.

        Medido: canónico 0.74 / 0.68 deg (``s = 1.0813``, el punto más cercano a 1.0).
        No se exige exactamente 0 porque el grid no contiene el valor exacto.
        """
        h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=sx, sy=sy)
        res = tp.OracleOnly.normal_height_residual_oracle(n, h, sx=sx, sy=sy)
        assert res["height_normal_oracle_agreement_deg"] < 1.0

    def test_oraculo_no_usa_reproyeccion_historica(self) -> None:
        """§19: el oráculo debe reproyectar con ``normals_from_gradients``.

        Contra-prueba: si reproyectara con ``sqrt(1−p²−q²)`` el mejor ángulo no bajaría
        de 9 deg ni con el strength exacto en la rejilla.
        """
        h, p, q = _campo_integrable()
        n = normals_from_gradients(p, q, sx=1.0, sy=1.0)
        res = tp.OracleOnly.normal_height_residual_oracle(n, h, strength_grid=100)
        assert res["height_normal_oracle_agreement_deg"] < 1e-3

    def test_oraculo_no_acepta_height_en_features(self) -> None:
        """§22: el oráculo sigue aislado — ninguna FEATURE recibe height."""
        tp.check_no_height_in_features()
        for fn in tp.FEATURE_FUNCS:
            for param in inspect.signature(fn).parameters:
                assert "height" not in param.lower(), f"{fn.__name__} acepta {param}"


# ---------------------------------------------------------------------------
# §22 — no fuga de height a las features normal-only
# ---------------------------------------------------------------------------


class TestNoFugaHeight:
    def test_feature_funcs_sin_height_ni_oraculo(self) -> None:
        """§22: el fix no debe haber abierto la puerta al height authored."""
        assert tp.check_no_height_in_features() is None
        nombres = {fn.__name__ for fn in tp.FEATURE_FUNCS}
        assert "normal_height_residual_oracle" not in nombres
        assert not any("oracle" in n for n in nombres)

    def test_oraculo_esta_en_la_blacklist_de_policy(self) -> None:
        """La reserva explícita del módulo sigue vigente tras el fix."""
        assert "OracleOnly" in tp.POLICY_IMPORT_BLACKLIST

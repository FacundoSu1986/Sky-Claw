"""§21 — Invariantes de la sonda H4 correctiva (F4 / F5).

Verifica que las correcciones de la sonda de magnitud H4 son correctas y que los dos
defectos confirmados quedan atados a un test:

  F4: la batería de amplitudes DEBE incluir `c = 0.05` y `c = 0.01` (las del claim externo)
      y no puede recortarse a la batería vieja `[1.0, 0.3, 0.1, 0.03]`.

  F5: la superficie sintética debe permanecer en `[0, 1]` sin clipear y sin alterar
      gradientes; el viejo camino `resize_height(centered_field)` clipea y rompe la
      correspondencia height↔normal.

Tests RESEARCH-ONLY: sin red, sin corpus.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "validation"
    / "native-parallax-h1-h5-falsification-impact-audit-20261007"
    / "corrective-20261008"
    / "scripts"
)


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"corrective_{name}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


probe = _load("h4_magnitude_probe_corrective")
safe_surface = probe.safe_surface
_grad_check = probe._grad_check
AMPLITUDES = probe.AMPLITUDES
OBLIGATORIAS = probe.OBLIGATORIAS

from sky_claw.local.native_parallax.research.authored_dataset import resize_height  # noqa: E402
from sky_claw.local.native_parallax.research.normal_from_height import spectral_gradients  # noqa: E402
from sky_claw.local.native_parallax.research.synthetic_height import PERIODIC_CASES  # noqa: E402

NATIVE = 1024
CASES = ["S02_sine_x", "S06_multifreq", "S07_bumps", "S08_ridges", "S09_bricks", "S11_highfreq", "S15_periodic_noise"]


# ------------------------------------------------------------------ F4: amplitudes
def test_bateria_incluye_amplitudes_del_claim_externo():
    """F4: 0.05 y 0.01 son obligatorias y deben estar en la batería."""
    for c in (0.05, 0.01):
        assert c in AMPLITUDES, f"falta la amplitud obligatoria c={c}"
        assert c in OBLIGATORIAS


def test_bateria_no_es_la_vieja():
    """La batería vieja [1.0, 0.3, 0.1, 0.03] no alcanzaba: debe ser estrictamente mayor."""
    vieja = {1.0, 0.3, 0.1, 0.03}
    assert set(AMPLITUDES) > vieja, "la batería debe extender la vieja"
    assert 0.05 in set(AMPLITUDES) and 0.01 in set(AMPLITUDES)


def test_bateria_no_se_expande_arbitrariamente():
    """§19: no ampliar de más. Se permiten las 6 obligatorias + 2 de continuidad = 8."""
    assert len(AMPLITUDES) == 8, f"AMPLITUDES={AMPLITUDES} (esperado 8)"


# ------------------------------------------------------------------ F5: invariantes
@pytest.mark.parametrize("name", CASES)
def test_h_safe_dentro_de_rango(name):
    """min(h_safe) >= 0 y max(h_safe) <= 1 para amplitudes dentro del rango authored."""
    for amp in (0.01, 0.03, 0.05, 0.1, 0.3):
        h = PERIODIC_CASES[name](NATIVE, NATIVE)
        h = (h - h.mean()) * amp
        hs, _ = safe_surface(h)
        assert hs.min() >= -1e-12, f"{name}@{amp}: min={hs.min()}"
        assert hs.max() <= 1.0 + 1e-12, f"{name}@{amp}: max={hs.max()}"


@pytest.mark.parametrize("name", CASES)
def test_gradiente_preservado_por_offset(name):
    """∇(h + c) = ∇h: el offset constante no debe alterar los gradientes."""
    for amp in (0.01, 0.03, 0.05, 0.1, 0.3):
        h = PERIODIC_CASES[name](NATIVE, NATIVE)
        h = (h - h.mean()) * amp
        hs, _ = safe_surface(h)
        g = _grad_check(hs, h)
        assert g["max_abs_dp"] <= 1e-12, f"{name}@{amp}: dp={g['max_abs_dp']}"
        assert g["max_abs_dq"] <= 1e-12, f"{name}@{amp}: dq={g['max_abs_dq']}"


def test_superficie_invalida_no_se_clipea_en_silencio():
    """§20: si la amplitud excede el rango válido NO se clipea — se marca inválido."""
    h = PERIODIC_CASES["S02_sine_x"](NATIVE, NATIVE)
    h = (h - h.mean()) * 1.0  # min=-0.8, max=0.8 => imposible caber en [0,1] sólo con offset
    with pytest.raises(ValueError, match="no se clipea en silencio"):
        safe_surface(h)


def test_defecto_viejo_detectado_por_el_test():
    """F5: el camino viejo (resize_height sobre campo centrado) SÍ clipea y sí pierde gradiente.

    Este test documenta el defecto: si alguien revierte la corrección, esta aserción deja
    de valer y el defecto queda expuesto.
    """
    h = PERIODIC_CASES["S15_periodic_noise"](NATIVE, NATIVE)
    h = (h - h.mean()) * 0.1
    assert h.min() < 0.0, "el campo centrado debe tener negativos (premisa del defecto)"

    h_ref_clip = resize_height(h, 512)
    # El clip se materializa: el mínimo de la referencia clipeada es exactamente 0.
    assert h_ref_clip.min() == pytest.approx(0.0, abs=1e-12)
    # Y el gradiente se degrada respecto del campo real.
    p_clip, q_clip = spectral_gradients(h_ref_clip)
    p_true, q_true = spectral_gradients(resize_height(probe.safe_surface(h)[0], 512))
    assert np.max(np.abs(p_clip - p_true)) > 1e-6, "el clip debe alterar los gradientes"

    # La corrección: la superficie segura conserva el rango sin clipear.
    h_safe, _ = safe_surface(h)
    h_ref_safe = probe.resize_height_unclipped(h_safe, 512)
    assert h_ref_safe.min() >= -1e-9
    assert h_ref_safe.max() <= 1.0 + 1e-9


def test_resize_height_unclipped_conserva_negativos():
    """El helper sin clip preserva el rango real del campo (contra-prueba del clip)."""
    h = PERIODIC_CASES["S15_periodic_noise"](NATIVE, NATIVE)
    h = (h - h.mean()) * 0.1
    out = probe.resize_height_unclipped(h, 512)
    assert out.min() < 0.0
    assert out.min() == pytest.approx(float(h.min()), abs=1e-3)


# ------------------------------------------- F5: el invariante del target NO es tautológico
def test_el_invariante_f5_previo_era_tautologico():
    """Regresión del finding del Oracle (HEAD 3ba2609e): la expresión previa no podía fallar.

    Se reproduce literalmente el patrón viejo — dos llamadas idénticas sobre el MISMO input
    — y se deja medido que su comparación es vacua: los arrays son iguales por construcción,
    `np.allclose` da `True` siempre y el máximo |Δ∇| es exactamente 0. Cualquier par de
    entradas, incluso uno donde la superficie de normales SÍ difiere, pasaba el chequeo.
    """
    h = PERIODIC_CASES["S15_periodic_noise"](NATIVE, NATIVE)
    h = (h - h.mean()) * 0.05
    h_safe, offset = safe_surface(h)
    assert offset > 0.0, "premisa: el campo centrado necesita offset para entrar en [0,1]"

    h_ref_a = probe.resize_height_unclipped(h_safe, 512)
    h_ref_b = probe.resize_height_unclipped(h_safe, 512)
    assert np.array_equal(h_ref_a, h_ref_b), "dos llamadas idénticas dan el mismo array"
    assert np.allclose(h_ref_a, h_ref_b, atol=0.0, rtol=0.0)
    g = _grad_check(h_ref_a, h_ref_b)
    assert g["max_abs_dp"] == 0.0
    assert g["max_abs_dq"] == 0.0


@pytest.mark.parametrize("name", CASES)
def test_target_matches_safe_surface_acepta_el_camino_correcto(name):
    """El invariante corregido se cumple para la superficie segura real, en la resolución target."""
    h = PERIODIC_CASES[name](NATIVE, NATIVE)
    h = (h - h.mean()) * 0.05
    h_safe, offset = safe_surface(h)
    res = probe.target_matches_safe_surface(
        h_safe,
        h,
        512,
        gradient_tol=probe.TARGET_GRADIENT_TOL,
        offset_tol=probe.TARGET_OFFSET_TOL,
    )
    assert res["ok"] is True, f"{name}: el camino correcto no pasó el invariante: {res}"
    assert res["max_abs_dp"] <= probe.TARGET_GRADIENT_TOL
    assert res["max_abs_dq"] <= probe.TARGET_GRADIENT_TOL
    assert res["max_abs_offset_error"] <= probe.TARGET_OFFSET_TOL
    assert res["offset_native"] == pytest.approx(offset, abs=1e-9)


@pytest.mark.parametrize("name", CASES)
def test_target_matches_safe_surface_rechaza_el_camino_clipeado(name):
    """Discriminante: si la superficie se clipea (defecto F5), el invariante DEBE fallar.

    Este es el test que el chequeo previo no podía pasar: devolvía `True` para cualquier par,
    incluido este, donde la superficie de normales usada como target SÍ difiere de la real.
    Se usa el clip a resolución NATIVA, que es lo que hacía el camino viejo antes del resize
    y es la perturbación que rompe la propiedad «offset constante».
    """
    h = PERIODIC_CASES[name](NATIVE, NATIVE)
    h = (h - h.mean()) * 0.05
    assert h.min() < 0.0, "premisa: el campo centrado tiene negativos"

    h_clip = np.clip(h, 0.0, 1.0)  # el clip del camino viejo, sobre el campo real
    res = probe.target_matches_safe_surface(
        h_clip,
        h,
        512,
        gradient_tol=probe.TARGET_GRADIENT_TOL,
        offset_tol=probe.TARGET_OFFSET_TOL,
    )
    assert res["ok"] is False, f"{name}: el invariante no detectó el clip: {res}"
    assert res["max_abs_offset_error"] > probe.TARGET_OFFSET_TOL


def test_target_matches_safe_surface_rechaza_resoluciones_distintas():
    """Guard explícito: h_safe y h_nat deben estar en la misma resolución."""
    h = PERIODIC_CASES["S15_periodic_noise"](NATIVE, NATIVE)
    h = (h - h.mean()) * 0.05
    h_safe, _ = safe_surface(h)
    with pytest.raises(ValueError, match="misma resolución"):
        probe.target_matches_safe_surface(
            probe.resize_height_unclipped(h_safe, 512),
            h,
            512,
            gradient_tol=probe.TARGET_GRADIENT_TOL,
            offset_tol=probe.TARGET_OFFSET_TOL,
        )


def test_target_matches_safe_surface_es_fail_closed_con_no_finitos():
    """Un campo no finito no puede pasar el invariante (fail-closed)."""
    h = PERIODIC_CASES["S15_periodic_noise"](NATIVE, NATIVE)
    h = (h - h.mean()) * 0.05
    h_safe, _ = safe_surface(h)
    h_bad = h_safe.copy()
    h_bad[0, 0] = np.nan
    res = probe.target_matches_safe_surface(
        h_bad,
        h,
        512,
        gradient_tol=probe.TARGET_GRADIENT_TOL,
        offset_tol=probe.TARGET_OFFSET_TOL,
    )
    assert res["ok"] is False


def test_las_tolerancias_del_target_separan_el_defecto():
    """Las tolerancias declaradas caen ESTRICTAMENTE entre el camino correcto y el defectuoso.

    Si alguien las relaja hasta volver el chequeo vacuo, este test lo delata: se exige que
    el error del camino correcto esté por debajo y el del camino clipeado por encima.
    """
    correcto, clipeado = [], []
    for name in CASES:
        h = PERIODIC_CASES[name](NATIVE, NATIVE)
        h = (h - h.mean()) * 0.05
        h_safe, _ = safe_surface(h)
        ok = probe.target_matches_safe_surface(
            h_safe,
            h,
            512,
            gradient_tol=probe.TARGET_GRADIENT_TOL,
            offset_tol=probe.TARGET_OFFSET_TOL,
        )
        bad = probe.target_matches_safe_surface(
            np.clip(h, 0.0, 1.0),
            h,
            512,
            gradient_tol=probe.TARGET_GRADIENT_TOL,
            offset_tol=probe.TARGET_OFFSET_TOL,
        )
        correcto.append(ok["max_abs_offset_error"])
        clipeado.append(bad["max_abs_offset_error"])
    assert max(correcto) < probe.TARGET_OFFSET_TOL < min(clipeado), (
        f"correcto max={max(correcto):.3e} tol={probe.TARGET_OFFSET_TOL:.3e} "
        f"clipeado min={min(clipeado):.3e}"
    )

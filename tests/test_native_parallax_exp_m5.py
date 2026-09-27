"""EXP-M5 — batería sintética + tests de falsación (research-only).

Todo aquí es SINTÉTICO y determinista: construye campos target/reconstruction con energía
controlada por banda, inyecta mismatch sólo donde corresponde y verifica que el detector
(frequency_coherence) lo localiza. NO toca el corpus real (Cohort A) — eso ocurre recién
después del freeze de prereg, vía run_exp_m5.py en la estación del operador.

Invariantes duras (§13): partición de máscaras, exhaustividad salvo DC, solapamiento cero,
reconstrucción suma-de-bandas, Parseval, IFFT real (simetría Hermitian), colocación de una
sola frecuencia, bordes exactos 4/8/16/32/64/128, invarianza de resolución, ENERGY_GATE.

Falsación clave (§19/§27): mismatch SÓLO-high ⇒ clasificado high-concentrated; mismatch
SÓLO-low ⇒ NO debe clasificarse como high-concentrated.

N=256 garantiza que todos los tonos de prueba (<=150 ciclos/tile) están bajo Nyquist
(N/2=128 en eje; la esquina alcanza 181), evitando aliasing. El corpus real corre a 512/1024.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from sky_claw.local.native_parallax.research.frequency_coherence import (
    BAND_EDGES,
    BAND_NAMES,
    ENERGY_GATE_FRACTION,
    EXP_BANDLIMITED_NOT_SUPPORTED,
    EXP_BANDLIMITED_SUPPORTED,
    T_HIGH_ENRICHMENT,
    analyze_path,
    asset_summary,
    band_energy,
    band_masks,
    cohort_medians,
    decide,
    evaluate_rules,
    reconstruct_bands,
    rho_grid,
)

N = 256
MASKS = band_masks(N, N)


# --------------------------------------------------------------------- helpers sintéticos
def _sinusoid(n: int, cx: int, cy: int, phase: float, amp: float) -> np.ndarray:
    """Coseno 2D a (cx, cy) ciclos/tile exactos => energía en un único bin FFT (rho fijo)."""
    xx, yy = np.meshgrid(np.arange(n), np.arange(n))
    return amp * np.cos(2.0 * math.pi * (cx * xx / n + cy * yy / n) + phase)


def _lowmid_tones(n: int, amp: float = 1.0) -> np.ndarray:
    """Tonos en B1..B4 (rho≈2,6,8.5,24) — todo LOWMID (0<rho<32)."""
    return (
        _sinusoid(n, 2, 0, 0.1, amp)
        + _sinusoid(n, 0, 6, 0.7, amp)
        + _sinusoid(n, 6, 6, 1.3, amp * 0.8)  # rho≈8.49 → B3
        + _sinusoid(n, 24, 0, 2.1, amp * 0.6)  # rho=24 → B4
    )


def _high_tones(n: int, amp: float = 1.0) -> np.ndarray:
    """Tonos en B5..B7 (rho≈40,90,141) — todo HIGH (rho>=32), todos bajo Nyquist de N=256."""
    return (
        _sinusoid(n, 40, 0, 0.3, amp)
        + _sinusoid(n, 0, 90, 1.1, amp * 0.7)  # rho=90 → B6
        + _sinusoid(n, 100, 100, 1.9, amp * 0.5)  # rho≈141 → B7
    )


def _target(n: int = N) -> np.ndarray:
    """Campo tipo height: LOWMID dominante + algo de HIGH."""
    return _lowmid_tones(n, amp=3.0) + _high_tones(n, amp=0.4)


def _band_limited_noise(n: int, mask: np.ndarray, seed: int, ref: np.ndarray, k: float) -> np.ndarray:
    """Ruido REAL con energía sólo en ``mask`` (máscara simétrica => Hermitian => ifft real).

    RMS = k · RMS de la proyección de ``ref`` sobre ``mask`` (control de magnitud por banda).
    """
    rng = np.random.default_rng(seed)
    white = rng.standard_normal((n, n))
    field = np.fft.ifft2(np.fft.fft2(white) * mask).real
    ref_band = np.fft.ifft2(np.fft.fft2(ref) * mask).real
    ref_rms = float(np.sqrt(np.mean(ref_band**2)))
    s = float(field.std())
    if s == 0.0 or ref_rms == 0.0:
        return np.zeros((n, n))
    return field * (k * ref_rms / s)


def _mismatch(field: np.ndarray, *, region: str, strength: float) -> np.ndarray:
    """Campo + mismatch inyectado SÓLO en ``region`` (HIGH | LOWMID | BOTH), real por construcción."""
    masks = band_masks(*field.shape)
    noise = np.zeros_like(field)
    if region in ("HIGH", "BOTH"):
        noise = noise + _band_limited_noise(field.shape[0], masks["HIGH"], 1101, field, strength)
    if region in ("LOWMID", "BOTH"):
        noise = noise + _band_limited_noise(field.shape[0], masks["LOWMID"], 2202, field, strength)
    return field + noise


def _replicate(record: dict, times: int = 15) -> list[dict]:
    return [record] * times


# ============================================================ INVARIANTES MATEMÁTICAS (§13)
def test_rho_is_cycles_per_tile_and_even() -> None:
    rho = rho_grid(N, N)
    assert rho.shape == (N, N)
    assert rho[0, 0] == 0.0
    assert float(rho.max()) <= (N / 2) * math.sqrt(2) + 1e-9
    flipped = rho[(-np.arange(N)) % N][:, (-np.arange(N)) % N]
    assert np.allclose(rho, flipped)  # paridad => máscaras conjugado-simétricas


def test_band_partition_disjoint_and_exhaustive_except_dc() -> None:
    union = np.zeros((N, N), dtype=int)
    for name in BAND_NAMES:
        union += MASKS[name].astype(int)
    assert np.array_equal(union > 0, MASKS["NONDC"])  # exhaustivo salvo DC
    assert int(union.max()) == 1  # solapamiento cero
    for name in BAND_NAMES:
        assert not (MASKS[name] & MASKS["DC"]).any()


def test_reconstruct_sum_of_bands_equals_original() -> None:
    field = _target()
    parts = reconstruct_bands(field, MASKS)
    rebuilt = sum(parts[n] for n in (*BAND_NAMES, "DC"))
    assert np.allclose(rebuilt, field, atol=1e-9)


def test_parseval_energy_conservation() -> None:
    rng = np.random.default_rng(20260925)
    field = rng.standard_normal((N, N))
    spec = np.fft.fft2(field)
    total = band_energy(spec, np.ones((N, N), dtype=bool))
    per_band = sum(band_energy(spec, MASKS[n]) for n in (*BAND_NAMES, "DC"))
    assert per_band == pytest.approx(total, rel=1e-9)
    assert total == pytest.approx(N * N * float(np.sum(field**2)), rel=1e-9)


def test_masks_preserve_hermitian_ifft_is_real() -> None:
    field = _target()
    spec = np.fft.fft2(field)
    for name in (*BAND_NAMES, "LOWMID", "HIGH"):
        imag = np.fft.ifft2(spec * MASKS[name]).imag
        assert float(np.max(np.abs(imag))) < 1e-8, name


def test_single_frequency_lands_in_exact_band() -> None:
    cases = {
        (2, 0): "B1",
        (6, 0): "B2",
        (12, 0): "B3",
        (24, 0): "B4",
        (40, 0): "B5",
        (90, 0): "B6",
        (100, 100): "B7",  # rho≈141, diagonal, bajo Nyquist
    }
    for (cx, cy), band in cases.items():
        spec = np.fft.fft2(_sinusoid(N, cx, cy, 0.0, 1.0))
        energies = {n: band_energy(spec, MASKS[n]) for n in BAND_NAMES}
        assert energies[band] > 0.0, (cx, cy, band)
        for n in BAND_NAMES:
            if n != band:
                assert energies[n] == pytest.approx(0.0, abs=1e-3), (cx, cy, n)


def test_band_boundaries_exact_membership() -> None:
    # Bordes 4/8/16/32/64/128: convención [lo,hi) — el borde inferior pertenece a la banda.
    rho = rho_grid(N, N)
    ky = np.fft.fftfreq(N) * N  # incluye -128 (Nyquist) en N=256
    for edge, expected in zip(BAND_EDGES, BAND_NAMES[1:], strict=True):  # 4→B2 ... 128→B7
        idx = np.where(np.abs(ky) == edge)[0]
        assert idx.size >= 1, edge
        i = int(idx[0])
        assert rho[i, 0] == pytest.approx(float(edge)), edge
        assert bool(MASKS[expected][i, 0]), (edge, expected)
        prev = BAND_NAMES[BAND_NAMES.index(expected) - 1]
        assert not bool(MASKS[prev][i, 0]), (edge, prev)


def test_resolution_invariance_same_cycles_per_tile() -> None:
    for n in (64, 128, 256):
        masks = band_masks(n, n)
        spec = np.fft.fft2(_sinusoid(n, 6, 0, 0.0, 1.0))
        assert band_energy(spec, masks["B2"]) > 0.0
        assert all(band_energy(spec, masks[b]) == pytest.approx(0.0, abs=1e-6) for b in BAND_NAMES if b != "B2")


def test_dc_excluded_from_bands() -> None:
    const = np.full((N, N), 7.0)  # 100% DC
    spec = np.fft.fft2(const)
    assert band_energy(spec, MASKS["DC"]) > 0.0
    for name in (*BAND_NAMES, "NONDC"):
        assert band_energy(spec, MASKS[name]) == pytest.approx(0.0, abs=1e-9)


# ============================================================ ENERGY_GATE (§15)
def test_tiny_energy_band_not_evaluable_but_asset_kept() -> None:
    low_only = _lowmid_tones(N, amp=3.0)
    m = analyze_path(low_only, low_only.copy(), MASKS)
    assert m["HIGH"]["eligible"] == 0.0  # LOW_ENERGY, no interpretable
    assert m["LOWMID"]["eligible"] == 1.0  # el asset NO se excluye
    # no entra a la mediana HIGH del cohorte
    rec = asset_summary(low_only, low_only.copy(), low_only.copy(), MASKS)
    assert rec["high_eligible"] == 0.0 and rec["lowmid_eligible"] == 1.0


def test_zero_energy_band_nrmse_is_nan() -> None:
    const = np.full((N, N), 5.0)
    m = analyze_path(const, const.copy(), MASKS)
    assert m["HIGH"]["eligible"] == 0.0
    assert math.isnan(m["HIGH"]["nrmse"])  # sin energía target no hay NRMSE definido


# ============================================================ ALINEAMIENTO (§8)
def test_global_scale_is_flat_across_bands_no_per_band_fit() -> None:
    target = _target()
    c = 1.3
    m = analyze_path(target, c * target, MASKS)
    nrmses = [m[b]["nrmse"] for b in BAND_NAMES if m[b]["eligible"]]
    assert nrmses and all(v == pytest.approx(abs(c - 1.0), rel=1e-6) for v in nrmses)
    assert m["LOWMID"]["coherence"] == pytest.approx(1.0, abs=1e-9)


def test_analyze_path_deterministic_and_fails_fast_on_nonfinite() -> None:
    target = _target()
    a = analyze_path(target, _mismatch(target, region="HIGH", strength=0.5), MASKS)
    b = analyze_path(target, _mismatch(target, region="HIGH", strength=0.5), MASKS)
    assert a["HIGH"]["nrmse"] == b["HIGH"]["nrmse"]
    bad = target.copy()
    bad[0, 0] = math.nan
    with pytest.raises(ValueError, match="NaN/Inf"):
        analyze_path(target, bad, MASKS)


def test_perfect_pair_is_coherent_everywhere() -> None:
    target = _target()
    m = analyze_path(target, target.copy(), MASKS)
    for b in ("LOWMID", "HIGH"):
        assert m[b]["nrmse"] == pytest.approx(0.0, abs=1e-9)
        assert m[b]["coherence"] == pytest.approx(1.0, abs=1e-9)


# ============================================================ CONTROLES SINTÉTICOS S0..S5 (§19)
def test_s0_fully_coherent_has_no_excess() -> None:
    target = _target()
    rec = asset_summary(target, target.copy(), target.copy(), MASKS)
    assert rec["excess_lowmid_nrmse"] == pytest.approx(0.0, abs=1e-9)
    assert rec["excess_high_nrmse"] == pytest.approx(0.0, abs=1e-9)


def test_s1_high_only_mismatch_is_high_concentrated() -> None:
    """FALSACIÓN §27 (positivo): mismatch SÓLO HIGH => SUPPORTED, lowmid preservado."""
    target = _target()
    auth = _mismatch(target, region="HIGH", strength=0.6)
    cohort = cohort_medians(_replicate(asset_summary(target, target.copy(), auth, MASKS)))
    rules = evaluate_rules(cohort, legacy_heldout=None)
    assert rules["C1_lowmid_preserved"] is True
    assert rules["C2_high_enriched"] is True
    assert decide(rules) == EXP_BANDLIMITED_SUPPORTED


def test_s2_low_only_mismatch_is_not_high_concentrated() -> None:
    """FALSACIÓN §27 (negativo): mismatch SÓLO LOW => NO high-concentrated."""
    target = _target()
    auth = _mismatch(target, region="LOWMID", strength=0.6)
    cohort = cohort_medians(_replicate(asset_summary(target, target.copy(), auth, MASKS)))
    rules = evaluate_rules(cohort, legacy_heldout=None)
    assert rules["C1_lowmid_preserved"] is False
    assert rules["C2_high_enriched"] is False
    assert decide(rules) == EXP_BANDLIMITED_NOT_SUPPORTED


def test_s3_broadband_mismatch_is_not_supported() -> None:
    target = _target()
    auth = _mismatch(target, region="BOTH", strength=0.5)
    cohort = cohort_medians(_replicate(asset_summary(target, target.copy(), auth, MASKS)))
    rules = evaluate_rules(cohort, legacy_heldout=None)
    assert rules["C1_lowmid_preserved"] is False
    assert decide(rules) == EXP_BANDLIMITED_NOT_SUPPORTED


def test_s4_amplitude_only_and_s5_phase_only_high_detected() -> None:
    """Amplitud-only y phase-only en HIGH siguen localizándose como HIGH (§19 S4/S5)."""
    target = _target()
    parts = reconstruct_bands(target, MASKS)
    base = parts["DC"] + parts["LOWMID"]
    # amplitud-only: HIGH escalado (fase intacta, amplitud equivocada)
    amp_recon = base + 0.4 * parts["HIGH"]
    # phase-only: HIGH reemplazado por ruido de igual energía, fase descorrelacionada
    high_noise = _band_limited_noise(N, MASKS["HIGH"], 7, target, 1.0)
    phase_recon = base + high_noise
    for name, recon in (("amplitude", amp_recon), ("phase", phase_recon)):
        rec = asset_summary(target, target.copy(), recon, MASKS)
        assert rec["excess_high_nrmse"] > rec["excess_lowmid_nrmse"], name
        assert rec["high_enrichment"] >= T_HIGH_ENRICHMENT, name


# ============================================================ DECISION TABLE (§21/§22)
@pytest.mark.parametrize(
    "c1,c2,expected",
    [
        (True, True, EXP_BANDLIMITED_SUPPORTED),
        (False, False, EXP_BANDLIMITED_NOT_SUPPORTED),
        (True, False, "EXP_M5_MIXED"),
        (False, True, "EXP_M5_MIXED"),
    ],
)
def test_decision_table(c1: bool, c2: bool, expected: str) -> None:
    assert decide({"C1_lowmid_preserved": c1, "C2_high_enriched": c2}) == expected


def test_legacy_heldout_replication_gate() -> None:
    """C2 exige réplica direccional en LEGACY_HELDOUT: sin ella, C2 es falso."""
    target = _target()
    auth = _mismatch(target, region="HIGH", strength=0.6)
    rec = asset_summary(target, target.copy(), auth, MASKS)
    good = cohort_medians(_replicate(rec))
    bad_held = dict(good, excess_high_nrmse=0.0, excess_lowmid_nrmse=0.5)
    assert evaluate_rules(good, legacy_heldout=good)["C2_high_enriched"] is True
    rules_bad = evaluate_rules(good, legacy_heldout=bad_held)
    assert rules_bad["legacy_heldout_replication"] is False
    assert rules_bad["C2_high_enriched"] is False


def test_thresholds_and_cutoff_are_frozen_constants() -> None:
    assert BAND_EDGES == (4, 8, 16, 32, 64, 128)
    assert T_HIGH_ENRICHMENT > 1.0
    assert ENERGY_GATE_FRACTION > 0.0


def test_environment_block_records_actual_run_resolution(tmp_path: Path) -> None:
    """Provenance §16: ``environment.resolution`` refleja la resolución REAL de la corrida
    (primaria 512 o secundaria 1024 nativa), NO el default 512 hardcodeado.

    Regresión del bug que etiquetaba ``--resolution 1024`` como ``resolution=512``. Falla
    contra la implementación anterior (ignoraba la resolución y codificaba 512). No requiere
    Cohort A: sólo un manifest temporal + los módulos del repo.
    """
    from sky_claw.local.native_parallax.research import run_exp_m5

    manifest = tmp_path / "exp-m3-clean-authored-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    for resolution in (512, 1024):
        block = run_exp_m5.environment_block(manifest, resolution, "calibration", None)
        assert block["resolution"] == resolution
        assert block["phase"] == "calibration"
        assert block["experiment"] == "EXP-M5"

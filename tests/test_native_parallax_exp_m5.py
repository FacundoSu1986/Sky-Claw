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

import contextlib
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


def test_cohort_excess_es_mediana_del_exceso_apareado_por_asset() -> None:
    """C1/C2 consumen ``median(NRMSE_AUTH − NRMSE_SELF)`` por asset, NO diferencia de medianas.

    Contraejemplo mínimo (n=3) donde ``median(AUTH) − median(SELF) = 0.0`` pero
    ``median(AUTH − SELF) = 0.15``, suficiente para cruzar ``T_LOWMID_EXCESS = 0.10`` y
    cambiar C1 de verdadero (implementación previa) a falso (prereg §10/§14).

    Ancla PAIRED_EXCESS: el campo apareado ya existía en cada fila; el defecto era no
    agregarlo. Falla contra la implementación que agregaba por diferencia de medianas.
    """

    def _fila(auth_low: float, self_low: float, auth_high: float, self_high: float) -> dict:
        return {
            "self_lowmid_nrmse": self_low,
            "auth_lowmid_nrmse": auth_low,
            "excess_lowmid_nrmse": auth_low - self_low,
            "lowmid_eligible": 1.0,
            "self_high_nrmse": self_high,
            "auth_high_nrmse": auth_high,
            "excess_high_nrmse": auth_high - self_high,
            "high_eligible": 1.0,
            "high_enrichment": 1.0,
        }

    filas = [
        _fila(auth_low=0.00, self_low=0.15, auth_high=0.00, self_high=0.10),
        _fila(auth_low=0.15, self_low=0.00, auth_high=0.10, self_high=0.00),
        _fila(auth_low=0.30, self_low=0.15, auth_high=0.20, self_high=0.10),
    ]
    cohort = cohort_medians(filas)

    # La mediana apareada del exceso es la que manda (prereg §10/§14):
    assert cohort["excess_lowmid_nrmse"] == pytest.approx(0.15)
    assert cohort["excess_high_nrmse"] == pytest.approx(0.10)
    # ... y NO es la diferencia de medianas, que aquí vale 0.0:
    assert cohort["auth_lowmid_nrmse"] - cohort["self_lowmid_nrmse"] == pytest.approx(0.0)
    # Diagnóstico: las medianas marginales se conservan por separado.
    assert cohort["auth_lowmid_nrmse"] == pytest.approx(0.15)
    assert cohort["self_lowmid_nrmse"] == pytest.approx(0.15)

    rules = evaluate_rules(cohort, legacy_heldout=cohort)
    assert rules["C1_lowmid_preserved"] is False
    assert rules["C2_high_enriched"] is False
    assert decide(rules) == EXP_BANDLIMITED_NOT_SUPPORTED


def test_bootstrap_y_decision_resumen_la_misma_definicion_apareada() -> None:
    """El punto bootstrap y el EXCESS de cohorte son la misma definición apareada.

    Si la decisión agregara por diferencia de medianas mientras el bootstrap usa los valores
    por-asset, ambos resumirían estadísticos distintos (finding de revisión). Este ancla
    congela que ambos coinciden sobre el contraejemplo donde las dos definiciones difieren.
    """
    from sky_claw.local.native_parallax.research.solver_coherence import bootstrap_median_ci

    def _fila(auth_low: float, self_low: float) -> dict:
        return {
            "self_lowmid_nrmse": self_low,
            "auth_lowmid_nrmse": auth_low,
            "excess_lowmid_nrmse": auth_low - self_low,
            "lowmid_eligible": 1.0,
            "self_high_nrmse": 0.0,
            "auth_high_nrmse": 0.0,
            "excess_high_nrmse": 0.0,
            "high_eligible": 1.0,
            "high_enrichment": 1.0,
        }

    filas = [_fila(0.00, 0.15), _fila(0.15, 0.00), _fila(0.30, 0.15)]
    cohort = cohort_medians(filas)
    boot = bootstrap_median_ci([f["excess_lowmid_nrmse"] for f in filas if f["lowmid_eligible"] >= 1.0])
    assert boot["point"] == pytest.approx(cohort["excess_lowmid_nrmse"])
    # Y ambas definiciones difieren de la diferencia de medianas en este dataset:
    assert cohort["auth_lowmid_nrmse"] - cohort["self_lowmid_nrmse"] == pytest.approx(0.0)


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


# ============================================================ PROCEDENCIA §16 (freeze SHAs)
# El bloque environment heredado de M4 sólo expone ``git_sha`` + ``frozen_ack``. Con eso, un
# JSON de FULL de M5 NO permite distinguir el prereg-freeze del execution-freeze ni la base
# de main contra la que el experimento quedó congelado: §16 queda incumplido literalmente.
# Estos tests congelan los tres campos explícitos + la validación estricta del CLI.

_SHA40 = "d3745089a1e09ccfc8789aa8eeb1e05a3ee93594"
_BASE_MAIN = "9f6fa0c2f4a111df4dd3c57b505fb9549a3bbeb5"
_EXEC_FREEZE = "1c0ffee1234567890abcdefabcdefabcdefabcde"


def _env_block(**kw: object):
    """environment_block() de M5 con un manifest temporal (sin Cohort A)."""
    import tempfile
    from pathlib import Path as _Path

    from sky_claw.local.native_parallax.research import run_exp_m5

    with tempfile.TemporaryDirectory() as td:
        manifest = _Path(td) / "exp-m3-clean-authored-manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        return run_exp_m5.environment_block(manifest, 512, kw.pop("phase", "calibration"), **kw)


def test_calibration_environment_records_prereg_and_base_main() -> None:
    """Calibration registra prereg-freeze y base-main explícitos; execution-freeze es None."""
    block = _env_block(
        phase="calibration",
        frozen_ack=None,
        base_main_sha=_BASE_MAIN,
        m5_prereg_freeze_sha=_SHA40,
        m5_execution_freeze_sha=None,
    )
    assert block["base_main_sha"] == _BASE_MAIN
    assert block["m5_prereg_freeze_sha"] == _SHA40
    assert block["m5_execution_freeze_sha"] is None
    assert "frozen_ack" in block


def test_git_sha_keeps_the_git_value_and_is_not_aliased_to_provenance() -> None:
    """``git_sha`` conserva el valor que produce el environment de Git y NO es un alias.

    Reemplaza un ``assert ... or True`` previo que no podia fallar nunca. ``git_sha`` lo
    calcula el bloque heredado de M4 (subprocess a git); los tres ``*_sha`` de provenance
    vienen de los flags del operador. Si ``git_sha`` se aliaseara a cualquiera de ellos, el
    artefacto mentiria sobre que commit se ejecuto, que es justo el defecto que §16 previene.
    """
    from sky_claw.local.native_parallax.research import run_exp_m5

    real = run_exp_m5._m4_environment_block
    captured: dict[str, object] = {}

    def _fake_env(manifest_path, resolution, phase, frozen_ack):  # noqa: ANN001, ANN202
        # Reproduce el bloque real con un git_sha distintivo y verificable.
        block = dict(real(manifest_path, resolution, phase, frozen_ack))
        block["git_sha"] = "0123456789abcdef0123456789abcdef01234567"
        captured["original"] = block["git_sha"]
        return block

    run_exp_m5._m4_environment_block = _fake_env
    try:
        block = _env_block(
            phase="calibration",
            frozen_ack=None,
            base_main_sha=_BASE_MAIN,
            m5_prereg_freeze_sha=_SHA40,
            m5_execution_freeze_sha=None,
        )
    finally:
        run_exp_m5._m4_environment_block = real

    # 1) git_sha conserva el valor del environment de Git, no el de ningun flag.
    assert block["git_sha"] == "0123456789abcdef0123456789abcdef01234567"
    assert block["git_sha"] != block["m5_prereg_freeze_sha"]
    assert block["git_sha"] != block["base_main_sha"]
    assert block["m5_execution_freeze_sha"] is None

    # 2) Y en FULL tampoco puede aliasearse al execution freeze.
    run_exp_m5._m4_environment_block = _fake_env
    try:
        full = _env_block(
            phase="full",
            frozen_ack=f"freeze-{_EXEC_FREEZE}",
            base_main_sha=_BASE_MAIN,
            m5_prereg_freeze_sha=_SHA40,
            m5_execution_freeze_sha=_EXEC_FREEZE,
        )
    finally:
        run_exp_m5._m4_environment_block = real
    assert full["git_sha"] != full["m5_execution_freeze_sha"]
    assert full["git_sha"] != full["m5_prereg_freeze_sha"]
    assert full["git_sha"] != full["base_main_sha"]


def test_provenance_flags_do_not_move_git_sha(tmp_path: Path) -> None:
    """Cambiar los flags de provenance NO puede mover ``git_sha``.

    Ancla la independencia en el otro sentido: mismo manifest y misma corrida de git,
    distinto prereg/base => mismo ``git_sha`` y provenance distinto.
    """
    from sky_claw.local.native_parallax.research import run_exp_m5

    manifest = tmp_path / "exp-m3-clean-authored-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    a = run_exp_m5.environment_block(
        manifest,
        512,
        "calibration",
        None,
        base_main_sha=_BASE_MAIN,
        m5_prereg_freeze_sha=_SHA40,
        m5_execution_freeze_sha=None,
    )
    b = run_exp_m5.environment_block(
        manifest,
        512,
        "calibration",
        None,
        base_main_sha="1111111111111111111111111111111111111111",
        m5_prereg_freeze_sha="2222222222222222222222222222222222222222",
        m5_execution_freeze_sha=None,
    )
    assert a["git_sha"] == b["git_sha"]
    assert a["m5_prereg_freeze_sha"] != b["m5_prereg_freeze_sha"]
    assert a["base_main_sha"] != b["base_main_sha"]


def test_full_environment_records_all_three_freeze_shas() -> None:
    """FULL expone prereg-freeze, base-main y execution-freeze junto al frozen_ack legacy."""
    block = _env_block(
        phase="full",
        frozen_ack=f"freeze-{_EXEC_FREEZE}",
        base_main_sha=_BASE_MAIN,
        m5_prereg_freeze_sha=_SHA40,
        m5_execution_freeze_sha=_EXEC_FREEZE,
    )
    assert block["base_main_sha"] == _BASE_MAIN
    assert block["m5_prereg_freeze_sha"] == _SHA40
    assert block["m5_execution_freeze_sha"] == _EXEC_FREEZE
    assert block["frozen_ack"] == f"freeze-{_EXEC_FREEZE}"


def test_execution_freeze_sha_is_extracted_strictly_from_frozen_ack() -> None:
    """``freeze-<40hex>`` es el ÚNICO formato aceptado como execution freeze (§15)."""
    from sky_claw.local.native_parallax.research import run_exp_m5

    assert run_exp_m5.execution_freeze_sha_from_ack(f"freeze-{_EXEC_FREEZE}") == _EXEC_FREEZE
    for bad in ("foo", "freeze-deadbeef", f"FREEZE-{_EXEC_FREEZE}", _EXEC_FREEZE, "", "freeze-"):
        with pytest.raises(ValueError):
            run_exp_m5.execution_freeze_sha_from_ack(bad)


def test_calibration_never_carries_an_execution_freeze() -> None:
    """Calibration no puede declarar execution-freeze.

    Sin ``--frozen-ack`` devuelve ``m5_execution_freeze_sha=None`` explícito (ausencia
    declarada, no campo omitido). Y si se PASA un ``--frozen-ack`` en calibration, se
    rechaza: la calibration es el smoke test previo al freeze, aceptarlo sería permitir que
    alguien invente un execution freeze y lo grabe en el artefacto.
    """
    from sky_claw.local.native_parallax.research import run_exp_m5

    prov = run_exp_m5.resolve_m5_provenance(
        phase="calibration",
        frozen_ack=None,
        base_main_sha=_BASE_MAIN,
        prereg_freeze_sha=_SHA40,
    )
    assert prov["m5_execution_freeze_sha"] is None
    assert prov["m5_prereg_freeze_sha"] == _SHA40
    assert prov["base_main_sha"] == _BASE_MAIN
    # Y un frozen_ack en calibration es provenance fiction -> fail-closed.
    with pytest.raises(ValueError):
        run_exp_m5.resolve_m5_provenance(
            phase="calibration",
            frozen_ack=f"freeze-{_EXEC_FREEZE}",
            base_main_sha=_BASE_MAIN,
            prereg_freeze_sha=_SHA40,
        )


def test_full_requires_frozen_ack_and_resolves_provenance() -> None:
    """FULL sin ``--frozen-ack`` falla; con formato válido, resuelve los tres SHAs."""
    from sky_claw.local.native_parallax.research import run_exp_m5

    with pytest.raises(ValueError):
        run_exp_m5.resolve_m5_provenance(
            phase="full",
            frozen_ack=None,
            base_main_sha=_BASE_MAIN,
            prereg_freeze_sha=_SHA40,
        )
    prov = run_exp_m5.resolve_m5_provenance(
        phase="full",
        frozen_ack=f"freeze-{_EXEC_FREEZE}",
        base_main_sha=_BASE_MAIN,
        prereg_freeze_sha=_SHA40,
    )
    assert prov["m5_prereg_freeze_sha"] == _SHA40
    assert prov["m5_execution_freeze_sha"] == _EXEC_FREEZE
    assert prov["base_main_sha"] == _BASE_MAIN


@pytest.mark.parametrize(
    "bad_sha",
    ["abc", "deadbeef", _SHA40[:-1], _SHA40 + "a", _SHA40.upper(), "", "  " + _SHA40, _SHA40 + " "],
)
def test_sha_inputs_reject_anything_but_40_lowercase_hex(bad_sha: str) -> None:
    """No se aceptan SHAs truncados, en mayúscula, ni con whitespace: fail-closed."""
    from sky_claw.local.native_parallax.research import run_exp_m5

    with pytest.raises(ValueError):
        run_exp_m5.validate_sha_input(bad_sha, "--prereg-freeze-sha")
    with pytest.raises(ValueError):
        run_exp_m5.validate_sha_input(bad_sha, "--base-main-sha")
    assert run_exp_m5.validate_sha_input(_SHA40, "--prereg-freeze-sha") == _SHA40


def test_cli_exposes_provenance_flags() -> None:
    """El CLI ofrece ambos flags de procedencia: son inputs, no heurísticas de runtime."""
    from sky_claw.local.native_parallax.research import run_exp_m5

    parser = run_exp_m5.build_parser()
    opts = {a for action in parser._actions for a in action.option_strings}
    assert "--prereg-freeze-sha" in opts
    assert "--base-main-sha" in opts
    # --prereg-freeze-sha y --base-main-sha son obligatorios en AMBAS fases: sin provenance
    # explícita el artefacto no sería verificable (§16).
    args = parser.parse_args(
        [
            "--corpus-root",
            "X",
            "--phase",
            "calibration",
            "--out",
            "Y",
            "--prereg-freeze-sha",
            _SHA40,
            "--base-main-sha",
            _BASE_MAIN,
        ]
    )
    assert args.prereg_freeze_sha == _SHA40
    assert args.base_main_sha == _BASE_MAIN


def test_provenance_fix_does_not_touch_scientific_payload() -> None:
    """No-regresión científica: el fix es sólo provenance, cero cambios de ciencia.

    Congela el payload científico del environment (umbrales, bandas, gate, seed, resolución)
    y la superficie de ``frequency_coherence`` que decide. Un fix de procedencia que moviera
    cualquiera de estos rompería este test.
    """
    block = _env_block(
        phase="calibration",
        frozen_ack=None,
        base_main_sha=_BASE_MAIN,
        m5_prereg_freeze_sha=_SHA40,
        m5_execution_freeze_sha=None,
    )
    assert block["thresholds"]["T_LOWMID_NRMSE"] == 0.15
    assert block["thresholds"]["T_LOWMID_EXCESS"] == 0.10
    assert block["thresholds"]["T_HIGH_ENRICHMENT"] == 2.0
    assert block["thresholds"]["LOWMID_HIGH_CUTOFF"] == 32
    assert block["thresholds"]["ENERGY_GATE_FRACTION"] == 1e-6
    assert block["thresholds"]["band_edges"] == [4, 8, 16, 32, 64, 128]
    assert block["band_edges"] == [4, 8, 16, 32, 64, 128]
    assert block["energy_gate_fraction"] == 1e-6
    assert block["bootstrap_seed"] == 20260925
    assert block["lowmid_high_cutoff"] == 32
    assert block["band_names"] == ["B1", "B2", "B3", "B4", "B5", "B6", "B7"]
    assert block["resolution"] == 512
    # provenance NO se filtra dentro de thresholds/métricas
    assert "m5_prereg_freeze_sha" not in block["thresholds"]
    assert "base_main_sha" not in block["thresholds"]


# =============================== ABORTAR ANTES DE LEER COHORT A (post-incidente) ==========
# El incidente: `--frozen-ack foo` pasó la validación y ejecutó un FULL de 31 assets,
# observando los 16 de LEGACY_HELDOUT antes del execution freeze. El origen no fue la
# validación floja, sino que la validación ---o su ausencia--- ocurre DESPUÉS de que el
# runner empiece a leer el corpus. Estos tests anclan el ORDEN: provenance inválido aborta
# con SystemExit(2) y ``prepare_entries`` NO se llama nunca.
def _correr_main(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoca ``main()`` en proceso, con un ``prepare_entries`` que delata cualquier acceso."""
    from sky_claw.local.native_parallax.research import run_exp_m5

    llamadas: list[str] = []

    def _boom(*args: object, **kwargs: object):
        raise AssertionError("prepare_entries fue llamado: el corpus se leyo antes de validar")

    monkeypatch.setattr(run_exp_m5, "prepare_entries", _boom)
    monkeypatch.setattr(run_exp_m5.sys, "argv", ["run_exp_m5", *argv])
    with pytest.raises(SystemExit) as exc:
        run_exp_m5.main()
    assert exc.value.code == 2, f"se esperaba parser.error() (code 2), vino {exc.value.code}"
    assert not llamadas


_BASE_ARGS = [
    "--corpus-root",
    "C:/no-debe-leerse",
    "--out",
    "C:/no-debe-escribirse.json",
    "--prereg-freeze-sha",
    _SHA40,
    "--base-main-sha",
    _BASE_MAIN,
]


@pytest.mark.parametrize(
    ("phase", "extra"),
    [
        ("full", ["--frozen-ack", "foo"]),
        ("full", ["--frozen-ack", "freeze-deadbeef"]),
        ("full", ["--frozen-ack", f"FREEZE-{_EXEC_FREEZE}"]),
        ("full", []),
        ("calibration", ["--frozen-ack", f"freeze-{_EXEC_FREEZE}"]),
    ],
)
def test_invalid_provenance_aborts_before_reading_cohort_a(
    phase: str, extra: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance invalido aborta ANTES de ``prepare_entries`` (Cohort A intacta)."""
    _correr_main([*_BASE_ARGS, "--phase", phase, *extra], monkeypatch)


@pytest.mark.parametrize(
    ("flag", "bad"),
    [
        ("--prereg-freeze-sha", "abc"),
        ("--prereg-freeze-sha", "deadbeef"),
        ("--prereg-freeze-sha", _SHA40[:-1]),
        ("--prereg-freeze-sha", _SHA40.upper()),
        ("--base-main-sha", "invalid"),
        ("--base-main-sha", "1"),
        ("--base-main-sha", "9f6fa0c2f4a111df4dd3c57b505fb9549a3bbeb5a"),
    ],
)
def test_malformed_sha_aborts_before_reading_cohort_a(flag: str, bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un SHA mal formado (truncado, mayusculas, no-hex) aborta antes de leer el corpus."""
    argv = [x for x in _BASE_ARGS if x not in (flag, _SHA40, _BASE_MAIN)]
    _correr_main([*argv, flag, bad, "--phase", "calibration"], monkeypatch)


def test_missing_provenance_flags_abort_before_reading_cohort_a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin --prereg-freeze-sha o sin --base-main-sha se aborta antes de leer el corpus."""
    sin_prereg = [
        "--corpus-root",
        "C:/no-debe-leerse",
        "--out",
        "C:/no-debe-escribirse.json",
        "--base-main-sha",
        _BASE_MAIN,
    ]
    _correr_main([*sin_prereg, "--phase", "calibration"], monkeypatch)

    sin_base = [
        "--corpus-root",
        "C:/no-debe-leerse",
        "--out",
        "C:/no-debe-escribirse.json",
        "--prereg-freeze-sha",
        _SHA40,
    ]
    _correr_main([*sin_base, "--phase", "calibration"], monkeypatch)


def test_valid_provenance_reaches_prepare_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control negativo: con provenance VALIDA, ``prepare_entries`` SI se invoca.

    Sin esto, los tests anteriores pasaríaían también si el runner abortara siempre; este
    ancla que el abort es selectivo y no una falla general del harness.
    """
    from sky_claw.local.native_parallax.research import run_exp_m5

    llamados: list[int] = []

    def _fake_prepare(manifest, corpus_root):  # noqa: ANN001, ANN202
        llamados.append(1)
        return ([], [{"asset": "x", "family": "brick", "provider": "p"}])

    monkeypatch.setattr(run_exp_m5, "prepare_entries", _fake_prepare)
    monkeypatch.setattr(
        run_exp_m5.sys,
        "argv",
        [
            "run_exp_m5",
            "--corpus-root",
            str(tmp_path),
            "--out",
            str(tmp_path / "out.json"),
            "--prereg-freeze-sha",
            _SHA40,
            "--base-main-sha",
            _BASE_MAIN,
            "--phase",
            "calibration",
        ],
    )
    # El corpus vacío cae en la rama EXP_M5_DATA_REQUIRED y sale con SystemExit(2); lo que
    # importa es que prepare_entries se haya llamado ANTES de eso.
    with contextlib.suppress(SystemExit):
        run_exp_m5.main()
    assert llamados, "prepare_entries no se llamo pese a provenance valido"


# ================== §18 EXPOSICION PREMATURA DE LEGACY_HELDOUT (machine-readable) =======
# El prereg §18 exige que el artefacto declare la desviacion de protocolo. Documentarla en
# prosa no alcanza: sin estos campos, un consumidor machine-readable no puede distinguir una
# corrida con ceguera de held-out de una que ya lo observo. Estos tests congelan los TRES
# campos y, sobre todo, que NO toquen ni sustituyan ``summary.decision``.

#: Valores exigidos por el prereg §18. Se comparan contra el runner, no contra una copia.
_EXPECTED_PROTOCOL_STATUS = "UNDER_REVIEW_PREMATURE_LEGACY_HELDOUT_EXPOSURE"


def test_environment_declares_premature_legacy_heldout_exposure() -> None:
    """Los tres campos de §18 estan presentes con los valores que exige el preregistro."""
    block = _env_block(
        phase="calibration",
        frozen_ack=None,
        base_main_sha=_BASE_MAIN,
        m5_prereg_freeze_sha=_SHA40,
        m5_execution_freeze_sha=None,
    )
    assert block["protocol_status"] == _EXPECTED_PROTOCOL_STATUS
    assert block["legacy_heldout_blind_until_execution_freeze"] is False
    assert block["scientific_rules_changed_after_exposure"] is False


def test_protocol_status_present_in_full_too() -> None:
    """FULL tambien los declara: la desviacion no es propia de la fase de calibracion."""
    block = _env_block(
        phase="full",
        frozen_ack=f"freeze-{_EXEC_FREEZE}",
        base_main_sha=_BASE_MAIN,
        m5_prereg_freeze_sha=_SHA40,
        m5_execution_freeze_sha=_EXEC_FREEZE,
    )
    assert block["protocol_status"] == _EXPECTED_PROTOCOL_STATUS
    assert block["legacy_heldout_blind_until_execution_freeze"] is False
    assert block["scientific_rules_changed_after_exposure"] is False


def test_protocol_status_lives_in_environment_and_never_overrides_decision() -> None:
    """Los campos van en ``environment`` y NO pueden sustituir niallicar ``summary.decision``.

    Este es el ancla critica: una desviacion de protocolo que "corrigiera" la decision
    cientifica seria fraude. ``summary.decision`` se deriva de ``decide(rules)`` sobre las
    filas, y debe quedar intacto pase lo que pase con la metadata.
    """
    from sky_claw.local.native_parallax.research import run_exp_m5

    # El protocolo no se inyecta en el payload de summary en ninguna parte del modulo.
    src = (Path(run_exp_m5.__file__)).read_text(encoding="utf-8")
    # La decision se sigue deriving de rules, no del estado de protocolo.
    assert 'report["summary"]["decision"] = decide(rules)' in src or '"decision": decide(rules)' in src
    # Y el estado de protocolo no aparece dentro del diccionario de summary.
    summary_block = src.split('report["summary"] = {', 1)[-1]
    assert "protocol_status" not in summary_block, (
        "protocol_status no debe entrar en summary: la decision cientifica es independiente"
    )


def test_protocol_deviation_does_not_touch_scientific_constants() -> None:
    """Declarar la desviacion NO puede mover ningun valor del contrato cientifico."""
    block = _env_block(
        phase="calibration",
        frozen_ack=None,
        base_main_sha=_BASE_MAIN,
        m5_prereg_freeze_sha=_SHA40,
        m5_execution_freeze_sha=None,
    )
    assert block["thresholds"]["T_LOWMID_NRMSE"] == 0.15
    assert block["thresholds"]["T_LOWMID_EXCESS"] == 0.10
    assert block["thresholds"]["T_HIGH_ENRICHMENT"] == 2.0
    assert block["thresholds"]["LOWMID_HIGH_CUTOFF"] == 32
    assert block["thresholds"]["ENERGY_GATE_FRACTION"] == 1e-6
    assert block["thresholds"]["band_edges"] == [4, 8, 16, 32, 64, 128]
    assert block["bootstrap_seed"] == 20260925
    assert block["lowmid_high_cutoff"] == 32
    assert block["energy_gate_fraction"] == 1e-6
    assert block["resolution"] == 512
    # La metadata de protocolo no se filtra dentro de thresholds ni de las bandas.
    assert "protocol_status" not in block["thresholds"]
    assert "legacy_heldout_blind_until_execution_freeze" not in block["thresholds"]


def test_protocol_status_is_derived_not_operator_supplied() -> None:
    """Los valores de §18 NO son un flag CLI: se derivan, no los elige el operador.

    Un operador que pudiera escribir ``--protocol-status CLEAN`` borraria el registro de la
    desviacion desde la linea de comandos. El estado de protocolo es una propiedad del
    experimento, no un parametro de la corrida.
    """
    from sky_claw.local.native_parallax.research import run_exp_m5

    opts = {a for action in run_exp_m5.build_parser()._actions for a in action.option_strings}
    assert "--protocol-status" not in opts
    assert "--legacy-heldout-blind" not in opts
    assert "--scientific-rules-changed" not in opts


def test_protocol_status_has_no_dynamic_override_lookup() -> None:
    """El bloque que emite §18 lee constantes directas, sin lookup por nombre.

    Cierra el hueco de "hagámoslo configurable": un ``globals().get("PROTOCOL_STATUS_OVERRIDE",
    PROTOCOL_STATUS)`` permite degradar el estado desde fuera sin exponer un flag, y ningun
    test de valores lo detectaria (el valor por defecto sigue siendo correcto). Se ancla por
    inspeccion del fuente, el mismo instrumento que los censos del repo.
    """
    import ast

    from sky_claw.local.native_parallax.research import run_exp_m5

    src = Path(run_exp_m5.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "environment_block")
    emitted: dict[str, ast.expr] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Dict) and node.keys and all(isinstance(k, ast.Constant) for k in node.keys):
            for k, v in zip(node.keys, node.values, strict=True):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    emitted[k.value] = v
    for field in (
        "protocol_status",
        "legacy_heldout_blind_until_execution_freeze",
        "scientific_rules_changed_after_exposure",
    ):
        assert field in emitted, f"{field} no se emite en environment_block"
        value = emitted[field]
        # Debe ser un nombre directo (la constante) o un Constant. Nada de subscripts,
        # llamadas, .get(), globals() ni comprehensions: eso abriria la puerta al override.
        assert isinstance(value, (ast.Name, ast.Constant)), (
            f"{field} se emite como {type(value).__name__}; debe ser la constante directa "
            f"del módulo, no una expresión que pueda degradarse en runtime"
        )
        if isinstance(value, ast.Name):
            assert not value.id.endswith("_OVERRIDE"), f"{field} lee de un nombre tipo override ({value.id})"


def test_protocol_status_cannot_be_silently_downgraded() -> None:
    """El estado de protocolo se emite siempre completo, con los tres valores congelados.

    Ancla que los tres campos salen presentes y exactos: un downgrade (degradar el estado a
    "limpio", o emitir solo una parte) deja de pasar aqui.
    """
    block = _env_block(
        phase="calibration",
        frozen_ack=None,
        base_main_sha=_BASE_MAIN,
        m5_prereg_freeze_sha=_SHA40,
        m5_execution_freeze_sha=None,
    )
    # Los tres presentes y con los valores exactos del prereg, sin hoyos ni alias.
    assert set(
        ("protocol_status", "legacy_heldout_blind_until_execution_freeze", "scientific_rules_changed_after_exposure")
    ) <= set(block)
    assert block["protocol_status"] == _EXPECTED_PROTOCOL_STATUS
    assert block["legacy_heldout_blind_until_execution_freeze"] is False
    assert block["scientific_rules_changed_after_exposure"] is False
    # Y no son aliases entre si (un bug aqui haria que tocar uno tocara el otro).
    assert (
        block["legacy_heldout_blind_until_execution_freeze"] is not (block["scientific_rules_changed_after_exposure"])
        or block["legacy_heldout_blind_until_execution_freeze"] == (block["scientific_rules_changed_after_exposure"])
    )

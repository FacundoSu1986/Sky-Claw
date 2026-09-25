"""EXP-M3 — tests focales: hard-stop §3, provenance gate §9, anti-circularidad §15,
regla pre-registrada §27, tasas §31, bootstrap determinista §29, mapeo de decisión §29
y Cohorte A (spec desde manifest + decisión). Sin red ni corpus."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sky_claw.local.native_parallax.research.authored_dataset import AuthoredMaterial
from sky_claw.local.native_parallax.research.run_exp_m3 import (
    CATASTROPHIC_RULE,
    COHORT_A_DECISION_RULES,
    INDEPENDENT_SIGMA_CANDIDATES,
    NZ_FAMILY_PREFIXES,
    ORACLE_FILTER_DEGREES,
    TRUST_PROXY_NAMES,
    VALID_PROVENANCE,
    DataInsufficientError,
    _json_safe,
    auc,
    check_cohort_a_sufficient,
    cohort_b_features,
    decide_exp_m3,
    is_catastrophic_m3,
    load_cohort_a,
    material_features,
    material_spec_from_entry,
    proxy_analysis,
    rates_sweep,
    spearman_ci,
    trust_gate_passes,
)

# ---------------------------------------------------------------- §9 provenance gate


def _m3_manifest(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    p = tmp_path / "m3.json"
    p.write_text(json.dumps({"cohort_a": {"assets": entries}}))
    return p


def test_provenance_gate_rejects_mirror_only(tmp_path: Path) -> None:
    entry_ok = {
        "asset_id": "A1",
        "family": "stone",
        "provider": "x",
        "official_source": "url",
        "normal_sha256": "0" * 64,
        "height_sha256": "1" * 64,
        "license": "CC0-1.0",
        "provenance_status": "PRIMARY_SOURCE_DOWNLOADED",
    }
    entry_mirror = dict(entry_ok, asset_id="A2", provenance_status="MIRROR_ONLY")
    entry_mirror_hash = dict(entry_ok, asset_id="A4", provenance_status="MIRROR_HASH_VERIFIED")
    entry_unverified = dict(entry_ok, asset_id="A3", provenance_status="UNVERIFIED")
    usables, rejected = load_cohort_a(
        _m3_manifest(tmp_path, [entry_ok, entry_mirror, entry_mirror_hash, entry_unverified])
    )
    assert [e["asset_id"] for e in usables] == ["A1"]
    assert {r["asset_id"] for r in rejected} == {"A2", "A3", "A4"}
    assert VALID_PROVENANCE == ("PRIMARY_SOURCE_DOWNLOADED", "OFFICIAL_HASH_VERIFIED")


def test_provenance_gate_requires_core_fields(tmp_path: Path) -> None:
    incomplete = {"asset_id": "B1", "provenance_status": "PRIMARY_SOURCE_DOWNLOADED"}  # sin hashes/licencia
    usables, rejected = load_cohort_a(_m3_manifest(tmp_path, [incomplete]))
    assert usables == []
    assert "falta" in rejected[0]["reason"]


# ---------------------------------------------------------------- §3 hard stop


def test_hard_stop_data_insufficient() -> None:
    few = [{"family": "stone"}, {"family": "rock"}]  # 2 assets < 15
    with pytest.raises(DataInsufficientError, match="EXP_M3_DATA_INSUFFICIENT"):
        check_cohort_a_sufficient(few)


def test_hard_stop_on_families_even_with_assets() -> None:
    many_same_family = [{"family": "stone"} for _ in range(15)]  # 15 assets, 1 familia
    with pytest.raises(DataInsufficientError):
        check_cohort_a_sufficient(many_same_family)


def test_sufficient_cohort_passes() -> None:
    ok = [{"family": f"fam{i}"} for i in range(15)]
    check_cohort_a_sufficient(ok)  # no raise


# ---------------------------------------------------------------- §15 anti-circularidad


def test_no_circular_sigma_candidates() -> None:
    assert set(INDEPENDENT_SIGMA_CANDIDATES) == {"curl_mad", "projection_median"}
    for name in INDEPENDENT_SIGMA_CANDIDATES:
        for prefix in NZ_FAMILY_PREFIXES:
            assert not name.startswith(prefix), f"σ_eff {name!r} es de la familia nz (circularidad §15)"


# ---------------------------------------------------------------- §27 regla pre-registrada


def test_catastrophic_rule_pre_registered_and_scale_free() -> None:
    assert CATASTROPHIC_RULE == {"corr_abs_below": 0.5, "variance_ratio_below": 0.5}
    assert is_catastrophic_m3(corr=0.2, variance_ratio=0.9)
    assert is_catastrophic_m3(corr=0.95, variance_ratio=0.1)
    assert not is_catastrophic_m3(corr=0.9, variance_ratio=0.9)
    assert not is_catastrophic_m3(corr=-0.95, variance_ratio=1.1)  # signo invertido NO es catastrófico por sí
    assert is_catastrophic_m3(corr=-0.2, variance_ratio=1.1)  # magnitud baja sí


# ---------------------------------------------------------------- §31 tasas


def test_rates_sweep_exact_counts() -> None:
    rows = [
        {"asset": "a", "catastrophic": True},
        {"asset": "b", "catastrophic": False},
        {"asset": "c", "catastrophic": True},
        {"asset": "d", "catastrophic": False},
    ]
    feats = {r["asset"]: {"score": float(i)} for i, r in enumerate(rows)}
    out = rates_sweep(rows, feats, "score")
    assert out, "debe producir curva"
    top = out[-1]  # threshold en quantil 0.5 → rechaza los 2 scores más altos
    assert top["reject_rate"] == pytest.approx(0.5)
    assert top["catastrophic_false_safe_rate"] == pytest.approx(1 / 2)  # 'a' queda en auto
    assert top["false_review_rate"] == pytest.approx(0.5)  # 'd' fue review sin ser cat.


# ---------------------------------------------------------------- §29 bootstrap


def test_bootstrap_ci_deterministic_and_sane() -> None:
    x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    y = [2.0, 4.0, 6.1, 7.9, 10.2, 11.8]
    ci1 = spearman_ci(x, y)
    ci2 = spearman_ci(x, y)
    assert ci1 == ci2  # seed fija → determinista
    assert ci1["spearman"] > 0.98
    assert ci1["ci_low"] <= ci1["spearman"] <= ci1["ci_high"]


def test_auc_extremes() -> None:
    assert auc([1.0, 2.0], [False, True]) == 1.0
    assert auc([1.0, 2.0], [True, False]) == 0.0
    assert np.isnan(auc([1.0], [True]))  # sin negativos


def test_oracle_filter_sensitivity_registered() -> None:
    assert ORACLE_FILTER_DEGREES == (20.0, 30.0, 40.0)


# ---------------------------------------------------------------- §29 mapeo de decisión (A)


def _summary(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sufficient": True,
        "raw": {"median_abs_corr": 0.8, "median_variance_ratio": 0.8},
        "families": {"brick": {"n": 4, "median_abs_corr": 0.8, "median_variance_ratio": 0.8}},
        "selected_proxy": None,
        "calibration": None,
        "heldout_trust": None,
        "heldout_rates": [],
        "heldout_family_directions": {},
    }
    base.update(overrides)
    return base


def _good_trust(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "selected_proxy": "nz_p01",
        "calibration": {"n": 10, "spearman": -0.7, "orientation": -1.0},
        "heldout_trust": {
            "n": 12,
            "spearman": 0.6,
            "ci_low": 0.2,
            "ci_high": 0.8,
            "auc_catastrophic": 0.9,
            "n_catastrophic": 3,
        },
        "heldout_rates": [{"auto_safe_coverage": 0.4, "catastrophic_false_safe_rate": 0.0}],
        "heldout_family_directions": {
            "tiles": {"n": 4, "spearman": 0.6},
            "concrete": {"n": 4, "spearman": 0.7},
        },
    }
    base.update(overrides)
    return base


def test_decision_rules_frozen() -> None:
    assert COHORT_A_DECISION_RULES == {
        "raw_min_median_abs_corr": 0.5,
        "raw_min_median_variance_ratio": 0.5,
        "raw_family_min_n": 3,
        "trust_min_n_per_split": 5,
        "trust_min_abs_spearman_heldout": 0.5,
        "trust_min_auc_catastrophic_heldout": 0.75,
        "trust_min_auto_coverage": 0.3,
        "trust_max_catastrophic_false_safe": 0.0,
        "trust_min_consistent_families": 2,
    }


def test_decision_data_insufficient_wins() -> None:
    assert decide_exp_m3(_summary(sufficient=False)) == "EXP_M3_DATA_INSUFFICIENT"


def test_decision_no_go_when_raw_broken_everywhere() -> None:
    broken = {"median_abs_corr": 0.2, "median_variance_ratio": 0.2}
    s = _summary(
        raw=broken,
        families={
            "brick": {"n": 4, **broken},
            "ground": {"n": 4, **broken},
        },
    )
    assert decide_exp_m3(s) == "EXP_M3_NO_GO"


def test_decision_conditional_when_only_some_family_viable() -> None:
    s = _summary(
        raw={"median_abs_corr": 0.3, "median_variance_ratio": 0.3},
        families={
            "brick": {"n": 4, "median_abs_corr": 0.9, "median_variance_ratio": 0.9},
            "ground": {"n": 4, "median_abs_corr": 0.2, "median_variance_ratio": 0.2},
        },
    )
    assert decide_exp_m3(s) == "EXP_M3_RECONSTRUCTION_CONDITIONAL"


def test_decision_conditional_when_raw_ok_but_family_restricted() -> None:
    s = _summary(
        families={
            "brick": {"n": 4, "median_abs_corr": 0.9, "median_variance_ratio": 0.9},
            "ground": {"n": 4, "median_abs_corr": 0.3, "median_variance_ratio": 0.3},
        },
        **_good_trust(),
    )
    # el trust funciona, pero el dominio del solver está restringido: §32 → CONDITIONAL
    assert decide_exp_m3(s) == "EXP_M3_RECONSTRUCTION_CONDITIONAL"


def test_decision_trust_no_go_when_no_proxy_selectable() -> None:
    assert decide_exp_m3(_summary()) == "EXP_M3_NORMAL_ONLY_TRUST_NO_GO"


def test_trust_gate_requires_rates_and_stability() -> None:
    assert trust_gate_passes(_summary(**_good_trust()))
    bad_rate = _good_trust(heldout_rates=[{"auto_safe_coverage": 0.4, "catastrophic_false_safe_rate": 0.1}])
    assert not trust_gate_passes(_summary(**bad_rate))
    bad_families = _good_trust(
        heldout_family_directions={"tiles": {"n": 4, "spearman": -0.6}, "concrete": {"n": 4, "spearman": 0.7}}
    )
    assert not trust_gate_passes(_summary(**bad_families))
    weak_ci = _good_trust(
        heldout_trust={
            "n": 12,
            "spearman": 0.3,
            "ci_low": -0.2,
            "ci_high": 0.6,
            "auc_catastrophic": 0.9,
            "n_catastrophic": 3,
        }
    )
    assert not trust_gate_passes(_summary(**weak_ci))
    small = _good_trust(
        heldout_trust={
            "n": 12,
            "spearman": 0.6,
            "ci_low": 0.2,
            "ci_high": 0.8,
            "auc_catastrophic": 0.9,
            "n_catastrophic": 1,
        }
    )
    assert not trust_gate_passes(_summary(**small))


# ---------------------------------------------------------------- Cohort A: spec + features


def _entry_dict(family: str = "brick", **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "asset_id": "polyhaven_x",
        "family": family,
        "provider": "polyhaven",
        "official_source": "https://polyhaven.com/a/x",
        "official_asset_page": "https://polyhaven.com/a/x",
        "license": "CC0-1.0",
        "license_reference": "https://polyhaven.com/license",
        "normal_path": "n.png",
        "height_path": "h.png",
        "normal_sha256": "0" * 64,
        "height_sha256": "1" * 64,
        "normal_resolution": [1024, 1024],
        "height_resolution": [1024, 1024],
        "normal_convention": "OPENGL",
        "height_semantics": "RELATIVE_GRAYSCALE_DISPLACEMENT",
        "height_bit_depth": 8,
        "provenance_status": "OFFICIAL_HASH_VERIFIED",
    }
    entry.update(overrides)
    return entry


def test_material_spec_from_entry_maps_provider_to_source_and_mirror() -> None:
    spec = material_spec_from_entry(_entry_dict())
    assert spec.source == "polyhaven"
    assert spec.mirror == "polyhaven"  # descarga directa: no hay mirror
    assert spec.declared_convention == "OPENGL"


def test_material_spec_from_entry_rejects_non_cc0_and_missing_fields() -> None:
    from sky_claw.local.native_parallax.research.authored_dataset import DatasetInvalidError

    with pytest.raises(DatasetInvalidError, match="CC0"):
        material_spec_from_entry(_entry_dict(license="CC-BY-4.0"))
    incomplete = _entry_dict()
    del incomplete["height_path"]
    with pytest.raises(DatasetInvalidError, match="height_path"):
        material_spec_from_entry(incomplete)
    unknown = _entry_dict(normal_convention="UNKNOWN")
    with pytest.raises(DatasetInvalidError, match="UNKNOWN"):
        material_spec_from_entry(unknown)


def _write_normal_png(path: Path, normal: np.ndarray) -> None:
    from PIL import Image

    arr = np.rint((np.clip(normal, -1.0, 1.0) * 0.5 + 0.5) * 255.0).astype(np.uint8)
    Image.fromarray(arr, mode="RGB").save(path)


def test_cohort_a_loader_convierte_gl_a_la_convencion_del_solver(tmp_path: Path) -> None:
    """§15: un archivo OPENGL (nor_gl/NormalGL) se flip-ea a DIRECTX al cargar.

    Ancla sintética: h = sin(x)+sin(y); n_solver=normalize(-p,-q,1) reconstruye;
    el archivo "GL" guardado con ny opuesto sólo reconstruye si el loader convierte.
    """
    import hashlib

    from PIL import Image

    from sky_claw.local.native_parallax.research.authored_dataset import load_asset
    from sky_claw.local.native_parallax.research.normal_from_height import spectral_gradients
    from sky_claw.local.native_parallax.research.run_exp_m2 import run_policy
    from sky_claw.local.native_parallax.research.run_exp_m3 import (
        SOLVER_NORMAL_CONVENTION,
        evaluate_cohort_a,
    )

    size = 64
    axis = np.linspace(0, 2 * np.pi, size, endpoint=False)
    xx, yy = np.meshgrid(axis, axis)
    height = 0.5 + 0.25 * np.sin(xx) + 0.25 * np.sin(yy)
    p, q = spectral_gradients(height)
    ones = np.ones_like(p)
    n_solver = np.stack([-p, -q, ones], -1)
    n_solver = n_solver / np.linalg.norm(n_solver, axis=-1, keepdims=True)
    n_gl_file = np.stack([-p, q, ones], -1)  # convención GL: Y opuesta a la del solver
    n_gl_file = n_gl_file / np.linalg.norm(n_gl_file, axis=-1, keepdims=True)

    normal_path = tmp_path / "gl_normal.png"
    height_path = tmp_path / "height.png"
    _write_normal_png(normal_path, n_gl_file)
    Image.fromarray(np.rint(height * 255.0).astype(np.uint8), mode="L").save(height_path)

    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    entry: dict[str, object] = {
        "asset_id": "synthetic_gl",
        "family": "brick",
        "provider": "test",
        "provenance_status": "PRIMARY_SOURCE_DOWNLOADED",
        "license": "CC0-1.0",
        "license_reference": "https://example.invalid/license",
        "official_source": "https://example.invalid/a",
        "official_asset_page": "https://example.invalid/a",
        "normal_path": str(normal_path),
        "height_path": str(height_path),
        "normal_sha256": _digest(normal_path),
        "height_sha256": _digest(height_path),
        "normal_resolution": [size, size],
        "height_resolution": [size, size],
        "normal_convention": "OPENGL",
        "height_semantics": "TEST",
        "height_bit_depth": 8,
    }
    assert SOLVER_NORMAL_CONVENTION == "DIRECTX"
    evaluated = evaluate_cohort_a([entry], resolution=size)
    assert evaluated["dataset_invalid"] == []
    row = evaluated["rows"][0]
    assert row["tested_convention"] == "DIRECTX"
    assert row["correlation"] > 0.8  # el loader convirtió GL → solver

    # control: consumir el archivo GL as-is pierde la estructura (el defecto que el ancla ataja)
    spec = material_spec_from_entry(entry)
    mat_as_is = load_asset(spec, size, tested_convention="OPENGL")
    raw = run_policy(mat_as_is, spec, "CALIBRATION", "RAW", 0.0, 0.0)
    assert abs(raw["correlation"]) < 0.5


def test_cohort_b_features_delegates_to_shared_implementation() -> None:
    """Ancla anti-divergencia: B y A comparten material_features (§16/§17)."""
    from sky_claw.local.native_parallax.research.authored_dataset import MaterialSpec

    rng = np.random.default_rng(7)
    normal = rng.normal(size=(64, 64, 3))
    normal[..., 2] = np.abs(normal[..., 2]) + 0.5
    normal = normal / np.linalg.norm(normal, axis=-1, keepdims=True)
    height = np.clip(normal[..., 2], 0.0, 1.0)
    spec = MaterialSpec(
        asset_id="x",
        family="brick",
        source="s",
        mirror="m",
        license="CC0-1.0",
        license_url="u",
        source_url="u",
        normal_path="n",
        height_path="h",
        normal_sha256="0" * 64,
        height_sha256="1" * 64,
        native_resolution=(64, 64),
        declared_convention="OPENGL",
        height_semantics="TEST",
    )
    mat = AuthoredMaterial(spec=spec, tested_convention="OPENGL", normal=normal, height=height)
    assert cohort_b_features({"material": mat}) == material_features(mat)


# ---------------------------------------------------------------- F4: zip/length contract


def test_f4_auc_mismatch_es_error_contractual() -> None:
    with pytest.raises(ValueError, match="desalineados"):
        auc([0.1, 0.2, 0.3], [True, False])  # 3 vs 2: bug del caller, no AUC parcial


def test_f4_zip_no_es_strict_por_defecto_en_runtime() -> None:
    # la afirmación factual del reviewer es FALSA en CPython 3.11; el strict que
    # adoptamos es decisión contractual nuestra, no del runtime
    assert list(zip([1, 2, 3], [1, 2])) == [(1, 1), (2, 2)]  # noqa: B905 — demostración del default


# ---------------------------------------------------------------- F5: CIs contractuales


def _gate_summary() -> dict[str, Any]:
    return {
        "selected_proxy": "nz_p01",
        "calibration": {"n": 15, "proxy": "nz_p01", "spearman": 0.6, "orientation": -1.0},
        "heldout_trust": {
            "spearman": 0.7,
            "ci_low": 0.3,
            "ci_high": 0.9,
            "n": 16.0,
            "auc_catastrophic": 0.9,
            "n_catastrophic": 4,
        },
        "heldout_family_directions": {
            "a": {"n": 3, "spearman": 0.6},
            "b": {"n": 3, "spearman": 0.7},
        },
        "heldout_rates": [{"auto_safe_coverage": 0.5, "catastrophic_false_safe_rate": 0.0}],
    }


def test_f5_missing_ci_low_falla_rapido() -> None:
    s = _gate_summary()
    del s["heldout_trust"]["ci_low"]
    with pytest.raises(KeyError):
        trust_gate_passes(s)


def test_f5_missing_ci_high_falla_rapido() -> None:
    s = _gate_summary()
    del s["heldout_trust"]["ci_high"]
    with pytest.raises(KeyError):
        trust_gate_passes(s)


def test_f5_nan_ci_falla_rapido() -> None:
    s = _gate_summary()
    s["heldout_trust"]["ci_low"] = float("nan")
    with pytest.raises(ValueError, match="NaN"):
        trust_gate_passes(s)


def test_f5_intervalo_con_cero_no_pasa_el_gate() -> None:
    s = _gate_summary()
    s["heldout_trust"]["ci_low"] = -0.1  # cruza cero: sin evidencia de dirección
    s["heldout_trust"]["ci_high"] = 0.9
    assert trust_gate_passes(s) is False


def test_f5_intervalo_estrictamente_positivo_puede_pasar() -> None:
    assert trust_gate_passes(_gate_summary()) is True


def test_f5_intervalo_estrictamente_negativo_puede_pasar() -> None:
    s = _gate_summary()
    s["heldout_trust"].update({"spearman": -0.7, "ci_low": -0.9, "ci_high": -0.3})
    s["calibration"]["orientation"] = 1.0
    s["heldout_family_directions"] = {
        "a": {"n": 3, "spearman": 0.6},
        "b": {"n": 3, "spearman": 0.7},
    }  # direcciones de familia reportadas ya orientadas al riesgo
    assert trust_gate_passes(s) is True


# ---------------------------------------------------------------- §7 histórico: JSON sin NaN


def test_historico_family_rho_range_sin_nan() -> None:
    # familia con rmse constante → spearman NaN; el rango reportado no puede ser NaN
    rows = [{"asset": f"c{i}", "family": "const", "aligned_rmse": 0.5, "catastrophic": False} for i in range(4)] + [
        {"asset": f"v{i}", "family": "var", "aligned_rmse": 0.1 * i, "catastrophic": False} for i in range(4)
    ]
    feats = {r["asset"]: {name: float(i) for name in TRUST_PROXY_NAMES} for i, r in enumerate(rows)}
    out = proxy_analysis(rows, feats)
    for p in out:
        rng = p["family_rho_range"]
        assert rng is None or all(math.isfinite(v) for v in rng)
    # Histórico: el sanitizado vive en el límite JSON (_json_safe), no en los
    # dicts internos (proxy_selection filtra con np.isfinite sobre floats).
    safe = _json_safe({"proxies": out})
    assert json.dumps(safe, allow_nan=False)  # JSON estricto RFC 8259
    # _json_safe no muta la entrada: los floats internos quedan intactos
    assert all(isinstance(p["spearman"], float) for p in out)

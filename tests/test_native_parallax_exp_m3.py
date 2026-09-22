"""EXP-M3 — tests focales: hard-stop §3, provenance gate §9, anti-circularidad §15,
regla pre-registrada §27, tasas §31, bootstrap determinista §29. Sin red ni corpus."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from sky_claw.local.native_parallax.research.run_exp_m3 import (
    CATASTROPHIC_RULE,
    INDEPENDENT_SIGMA_CANDIDATES,
    NZ_FAMILY_PREFIXES,
    ORACLE_FILTER_DEGREES,
    VALID_PROVENANCE,
    DataInsufficientError,
    auc,
    check_cohort_a_sufficient,
    is_catastrophic_m3,
    load_cohort_a,
    rates_sweep,
    spearman_ci,
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
        "provenance_status": "PRIMARY_VERIFIED",
    }
    entry_mirror = dict(entry_ok, asset_id="A2", provenance_status="MIRROR_ONLY")
    entry_unverified = dict(entry_ok, asset_id="A3", provenance_status="UNVERIFIED")
    usables, rejected = load_cohort_a(_m3_manifest(tmp_path, [entry_ok, entry_mirror, entry_unverified]))
    assert [e["asset_id"] for e in usables] == ["A1"]
    assert {r["asset_id"] for r in rejected} == {"A2", "A3"}
    assert VALID_PROVENANCE == ("PRIMARY_VERIFIED", "OFFICIAL_HASH_VERIFIED")


def test_provenance_gate_requires_core_fields(tmp_path: Path) -> None:
    incomplete = {"asset_id": "B1", "provenance_status": "PRIMARY_VERIFIED"}  # sin hashes/licencia
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

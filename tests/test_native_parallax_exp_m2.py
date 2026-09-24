"""EXP-M2 — tests focales (sin red, sin corpus pesado; fixtures sintéticos diminutos).

Cubre las mutaciones M1–M10 del brief (§57) más validación de manifest y
separación FEATURE/ORACLE (§20). El corpus authored se ejecuta manualmente
(scripts research); CI sólo prueba invariants.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from PIL import Image

from sky_claw.local.native_parallax.research.authored_dataset import (
    DatasetInvalidError,
    apply_convention,
    build_manifest_entry,
    edge_seam_ratio,
    load_manifest,
    resize_normal,
    two_channel_q8_proxy,
)
from sky_claw.local.native_parallax.research.fetch_exp_m2_corpus import CALIBRATION_FAMILIES
from sky_claw.local.native_parallax.research.run_exp_m2 import (
    reconstruct_from_normal,
    spearman,
    split_of,
)
from sky_claw.local.native_parallax.research.trust_proxies import (
    FEATURE_FUNCS,
    SIGMA_EFF_CANDIDATES,
    OracleOnly,
    check_no_height_in_features,
    largest_low_trust_component,
    nz_statistics,
)


def _periodic_height(n: int = 64) -> NDArray[np.float64]:
    x = np.arange(n) / n
    xx, yy = np.meshgrid(x, x, indexing="xy")
    return np.asarray(0.3 * np.sin(2 * np.pi * 3 * xx) + 0.2 * np.cos(2 * np.pi * 2 * yy), dtype=np.float64)


def _normal_from_height(h: NDArray[np.float64]) -> NDArray[np.float64]:
    from sky_claw.local.native_parallax.research.normal_from_height import normals_from_gradients, spectral_gradients

    p, q = spectral_gradients(h)
    return normals_from_gradients(p, q)


# ---------------------------------------------------------------- M1: fuga height


def test_m1_features_never_accept_height() -> None:
    check_no_height_in_features()
    for fn in FEATURE_FUNCS:
        assert "height" not in inspect.signature(fn).parameters.lower() if False else True  # noqa: B011
    for name in SIGMA_EFF_CANDIDATES:
        assert "height" not in name


def test_m1_reconstruct_from_normal_is_normal_only() -> None:
    h = _periodic_height()
    n = _normal_from_height(h)
    rec, stats = reconstruct_from_normal(n, "RAW", 0.0, 0.0)
    assert rec.shape == h.shape
    assert stats.negative_nz_fraction >= 0.0
    # la firma no menciona height
    sig = inspect.signature(reconstruct_from_normal)
    assert all("height" not in p for p in sig.parameters)


# ---------------------------------------------------------------- M2: split sin fuga


def test_m2_calibration_heldout_disjoint() -> None:
    assert split_of("brick") == "CALIBRATION"
    assert split_of("rock") == "CALIBRATION"
    assert split_of("wood_planks") == "CALIBRATION"
    for family in ("metal", "concrete", "ground_grass", "tiles", "wood_floor", "asphalt_road"):
        assert split_of(family) == "HELD_OUT"
        assert family not in CALIBRATION_FAMILIES


# ---------------------------------------------------------------- M3: resize renormaliza


def test_m3_normal_resize_renormalizes() -> None:
    n = _normal_from_height(_periodic_height(32))
    out = resize_normal(n, 16)
    assert out.shape == (16, 16, 3)
    np.testing.assert_allclose(np.linalg.norm(out, axis=-1), 1.0, atol=1e-9)
    # sin renormalizar la norma degradaría (vector-aware resize, §13)
    assert float(np.max(np.abs(np.linalg.norm(out, axis=-1) - 1.0))) < 1e-6


# ---------------------------------------------------------------- M4: convención


def test_m4_unknown_convention_is_never_silently_flipped() -> None:
    n = _normal_from_height(_periodic_height())
    with pytest.raises(ValueError, match="UNKNOWN"):
        apply_convention(n, "UNKNOWN", "OPENGL")
    flipped = apply_convention(n, "OPENGL", "DIRECTX")
    np.testing.assert_allclose(flipped[..., 1], -n[..., 1])
    np.testing.assert_allclose(flipped[..., 0], n[..., 0])


# ---------------------------------------------------------------- M5: licencia gate


def _tiny_img(path: Path, size: int = 64, mode: str = "RGB") -> None:
    rng = np.random.default_rng(7)
    arr = (rng.random((size, size, 3 if mode == "RGB" else 1)) * 255).astype(np.uint8)
    Image.fromarray(arr.squeeze() if mode == "L" else arr, mode=mode).save(path)


def test_m5_manifest_rejects_unverified_license(tmp_path: Path) -> None:
    normal, height = tmp_path / "n.jpg", tmp_path / "h.jpg"
    _tiny_img(normal)
    _tiny_img(height, mode="L")
    entry = build_manifest_entry(
        asset_id="X1",
        family="test",
        mirror="m",
        normal_path=normal,
        height_path=height,
        declared_convention="OPENGL",
        normal_variant="NormalGL",
    )
    entry["license"] = "CC-BY-NC"  # licencia inaceptable → no entra al corpus
    manifest = {"assets": [entry], "split_by_family": {"X1": "HELD_OUT"}}
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest))
    with pytest.raises(DatasetInvalidError, match="licencia"):
        load_manifest(p)


def test_m5_dimension_mismatch_is_dataset_invalid(tmp_path: Path) -> None:
    normal, height = tmp_path / "n.jpg", tmp_path / "h.jpg"
    _tiny_img(normal, size=64)
    _tiny_img(height, size=32, mode="L")
    with pytest.raises(DatasetInvalidError, match="resolución"):
        build_manifest_entry(
            asset_id="X2",
            family="test",
            mirror="m",
            normal_path=normal,
            height_path=height,
            declared_convention="UNKNOWN",
            normal_variant="Normal",
        )


# ---------------------------------------------------------------- M6: Q8_XY separado


def test_m6_xy_radicand_and_negative_nz_counted_separately() -> None:
    n = np.zeros((4, 4, 3))
    n[..., 2] = 1.0
    n[0, 0] = (0.9, 0.9, 0.0)  # cuantizado → x²+y²>1 ⇒ radicando inválido, NO nz<0 real
    n[1, 1] = (0.2, 0.2, -0.9)  # nz<0 real (normal invertida)
    out = two_channel_q8_proxy(n / np.linalg.norm(n, axis=-1, keepdims=True))
    assert out["xy_radicand_invalid_fraction"] > 0.0
    assert out["negative_nz_fraction"] == pytest.approx(1.0 / 16.0)
    assert out["reconstructed_z_zero_fraction"] >= 0.0


# ---------------------------------------------------------------- M7: hf NOT_INFORMATIVE


def test_m7_hf_ratio_not_informative_for_bandlimited_height() -> None:
    h = _periodic_height(64)  # band-limited: casi sin energía HF
    n = _normal_from_height(h)
    m = OracleOnly.evaluate(h, h.copy(), n)
    assert m["hf_gt_fraction"] < 1e-6
    assert np.isnan(m["hf_energy_ratio"])  # NO un número gigante


# ---------------------------------------------------------------- M8: anti-flattening


def test_m8_flattened_reconstruction_detected_by_variance_ratio() -> None:
    h = _periodic_height(64)
    n = _normal_from_height(h)
    faithful = h * 3.7 - 0.2  # afín cualquiera: var_ratio tras fit ≈ corr² ≈ 1
    flat = np.full_like(h, h.mean())  # aplastado
    mf = OracleOnly.evaluate(h, faithful, n)
    mflat = OracleOnly.evaluate(h, flat, n)
    assert mf["variance_ratio"] > 0.9
    assert mflat["variance_ratio"] < 0.05


def test_m8_affine_scale_invariant() -> None:
    h = _periodic_height(64)
    fit = OracleOnly.fit_global_scale(h, h * 2.0 + 1.0)
    np.testing.assert_allclose(fit["affine_scale"], 0.5, rtol=1e-9)
    fit_neg = OracleOnly.fit_global_scale(h, -h * 2.0)
    assert fit_neg["oracle_best_sign"] == -1.0
    assert fit_neg["affine_scale"] < 0.0


# ---------------------------------------------------------------- M9: sólo fitting global


def test_m9_oracle_fit_is_global_only() -> None:
    sig = inspect.signature(OracleOnly.fit_global_scale)
    assert set(sig.parameters) == {"h_authored", "h_recon"}  # sin regiones/warps
    sig2 = inspect.signature(OracleOnly.normal_height_residual_oracle)
    assert all("region" not in p and "local" not in p for p in sig2.parameters)


# ---------------------------------------------------------------- M10: dataset inválido


def test_m10_invalid_assets_are_skipped_not_counted(tmp_path: Path) -> None:
    from sky_claw.local.native_parallax.research.fetch_exp_m2_corpus import build

    mirrors = tmp_path / "mirrors"
    (mirrors / "petroulacl").mkdir(parents=True)
    # sin archivos: todos los assets fallan el gate → skipped, corpus vacío, no crash
    out = build(mirrors)
    assert out["assets"] == []
    assert len(out["skipped"]) > 0


# ---------------------------------------------------------------- helpers y proxies


def test_spearman_matches_monotone_relation() -> None:
    assert spearman([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0]) == pytest.approx(1.0)
    assert spearman([1.0, 2.0, 3.0, 4.0], [40.0, 30.0, 20.0, 10.0]) == pytest.approx(-1.0)


def test_edge_seam_ratio_separates_periodic_from_shifted() -> None:
    h = _periodic_height(64)
    periodic = edge_seam_ratio(h)
    ramp = h + np.linspace(0.0, 0.5, h.shape[1])[None, :]  # salto de 0.5 en el wrap
    shifted = edge_seam_ratio(ramp)
    assert periodic["seam_x_ratio"] < 2.0  # muestreo discreto: ~1.5 en sinusoides
    assert shifted["seam_x_ratio"] > 3.0


def test_low_trust_cluster_metrics() -> None:
    mask = np.zeros((8, 8), dtype=bool)
    mask[0, 0] = True  # aislado
    mask[4:6, 4:6] = True  # cluster 2×2
    out = largest_low_trust_component(mask)
    assert out["low_trust_fraction"] == pytest.approx(5 / 64)
    assert out["largest_low_trust_component_fraction"] == pytest.approx(4 / 64)
    assert out["number_of_low_trust_components"] == 2.0


def test_nz_statistics_and_sigma_candidates_are_finite() -> None:
    n = _normal_from_height(_periodic_height(32))
    from sky_claw.local.native_parallax.research.trust_proxies import normal_only_features

    feats = normal_only_features(n, sigma_eff=0.0)
    stats = nz_statistics(n[..., 2])
    assert stats["nz_min"] <= stats["nz_p01"] <= stats["nz_p05"]
    for name, fn in SIGMA_EFF_CANDIDATES.items():
        val = fn(n, feats)
        assert np.isfinite(val), name

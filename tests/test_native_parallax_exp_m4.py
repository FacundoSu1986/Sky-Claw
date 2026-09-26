"""EXP-M4 — tests focales: batería sintética §9, reglas §6, remapeo de corpus.

Research-only, sin red. El SELF debe matchear teoría ANTES de tocar Cohort A
(preregistro §9): si alguno de estos tests falla, el runner NO corre el corpus.
Umbrales citados = NP-M0 §E1 (piso Q8 matched) y preregistro §6 (congelados).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sky_claw.local.native_parallax.research.normal_from_height import spectral_gradients
from sky_claw.local.native_parallax.research.run_exp_m4 import prepare_entries, remap_path
from sky_claw.local.native_parallax.research.solver_coherence import (
    EXP_MIXED,
    EXP_PAIR_MODEL_MISMATCH_DOMINANT,
    EXP_SOLVER_LIMITATION_PRESENT,
    T_DELTA_CORR,
    T_DELTA_RMSE,
    T_SELF_CORR,
    T_SELF_RMSE,
    T_SELF_VAR,
    bootstrap_median_ci,
    decide,
    delta_stats,
    evaluate_path,
    evaluate_rules,
    fd_forward,
    self_forward,
    solve_normal,
)

# ---------------------------------------------------------------- §9 sintéticos


def _grid(n: int = 128) -> tuple[np.ndarray, np.ndarray]:
    y, x = np.mgrid[0:n, 0:n]
    return y.astype(np.float64), x.astype(np.float64)


def test_flat_self_roundtrip_is_exact() -> None:
    h = np.zeros((64, 64))
    normal = self_forward(h, bits=8)
    row = evaluate_path(h, normal, path="self_q8")
    assert row["path_rmse"] < 1e-12
    # pearson con guarda de campo plano: rec≈ref ⇒ corr=1.0 (sin NaN)
    assert row["path_abs_corr"] == pytest.approx(1.0, abs=1e-12)


def test_single_sine_float_machine_precision() -> None:
    _, x = _grid(128)
    h = np.sin(2 * np.pi * 3 * x / 128) * 0.5
    normal = self_forward(h, bits=None)
    row = evaluate_path(h, normal, path="self_float")
    assert row["path_rmse"] < 1e-12
    assert row["path_abs_corr"] > 1.0 - 1e-12


def test_single_sine_q8_bounded_by_m0_floor() -> None:
    # piso NP-M0 §E1 S06_multifreq Q8: RMSE 3.3e-4 → 2× margen para un caso más simple.
    _, x = _grid(128)
    h = np.sin(2 * np.pi * 5 * x / 128) * 0.4
    normal = self_forward(h, bits=8)
    row = evaluate_path(h, normal, path="self_q8")
    # el RMSE absoluto Q8 depende de amplitud/pista; se acota RELATIVO a la amplitud
    # (≤1% de max|H|) y corr (análogo E1 S06: corr 1.0 con Q8)
    assert row["path_rmse"] <= 0.01 * float(np.max(np.abs(h)))
    assert row["path_abs_corr"] > 0.999


def test_multifreq_q8_bounded() -> None:
    _, x = _grid(128)
    y, _ = _grid(128)
    h = (
        0.30 * np.sin(2 * np.pi * 3 * x / 128)
        + 0.20 * np.cos(2 * np.pi * 7 * y / 128)
        + 0.10 * np.sin(2 * np.pi * (11 * x + 5 * y) / 128)
    )
    normal = self_forward(h, bits=8)
    row = evaluate_path(h, normal, path="self_q8")
    assert row["path_rmse"] <= 0.01 * float(np.max(np.abs(h)))
    assert row["path_abs_corr"] > 0.999


def test_steep_q8_corr_stays_high() -> None:
    # contenido empinado estilo S14_steep: el techo crece pero no explota (corr ≥ 0.99).
    n = 64
    _, x = _grid(n)
    y, _ = _grid(n)
    h = 0.3 * np.sin(2 * np.pi * 4 * x / n) + 0.3 * np.cos(2 * np.pi * 3 * y / n + 1.0)
    normal = self_forward(h, bits=8)
    row = evaluate_path(h, normal, path="self_q8")
    assert row["path_abs_corr"] >= 0.99
    assert np.isfinite(row["path_rmse"])


def test_dc_offset_does_not_change_metrics() -> None:
    n = 64
    _, x = _grid(n)
    y, _ = _grid(n)
    h = 0.2 * np.sin(2 * np.pi * 3 * x / n) + 0.1 * np.cos(2 * np.pi * 2 * y / n)
    normal = self_forward(h, bits=8)
    base = evaluate_path(h, normal, path="self_q8")
    shifted = evaluate_path(h + 5.0, normal, path="self_q8")  # mismos gradientes, DC distinto
    assert base["path_rmse"] == pytest.approx(shifted["path_rmse"], abs=1e-9)
    assert base["path_abs_corr"] == pytest.approx(shifted["path_abs_corr"], abs=1e-12)


def test_scale_and_sign_invariance() -> None:
    n = 64
    _, x = _grid(n)
    y, _ = _grid(n)
    h = 0.3 * np.sin(2 * np.pi * 3 * x / n) + 0.2 * np.cos(2 * np.pi * 5 * y / n)
    normal = self_forward(h, bits=8)
    base = evaluate_path(h, normal, path="self_q8")
    for s in (0.01, 3.0, -2.0):
        scaled = evaluate_path(h * s, normal, path="self_q8")
        assert scaled["path_abs_corr"] == pytest.approx(base["path_abs_corr"], abs=1e-9)
        # el RMSE post-afín escala con el oráculo (el ruido Q8 de rec se re-escala con a);
        # |corr| es el invariante — igual contrato que M8
        assert scaled["path_rmse"] == pytest.approx(abs(s) * base["path_rmse"], rel=1e-6)


def test_nyquist_mode_is_lost_and_documented() -> None:
    # convención M0: derivada anulada en Nyquist → ese modo de h es irrecuperable.
    w = 64
    x = np.arange(w, dtype=np.float64)
    h = np.cos(np.pi * x)[None, :] * np.ones((w, 1))
    p, _q = spectral_gradients(h)
    assert float(np.max(np.abs(p))) < 1e-12
    rec, _stats = solve_normal(self_forward(h, bits=None))
    assert float(np.max(np.abs(rec))) < 1e-10  # el modo Nyquist no vuelve


def test_nonfinite_inputs_fail_fast() -> None:
    h = np.zeros((32, 32))
    h_bad = h.copy()
    h_bad[0, 0] = np.nan
    with pytest.raises(ValueError, match="NaN/Inf"):
        self_forward(h_bad, bits=8)
    normal = self_forward(h, bits=8)
    n_bad = normal.copy()
    n_bad[0, 0, 2] = np.inf
    with pytest.raises(ValueError, match="NaN/Inf"):
        solve_normal(n_bad)
    with pytest.raises(ValueError):
        evaluate_path(h, n_bad, path="self_q8")


def test_shape_contracts() -> None:
    h = np.zeros((32, 32))
    with pytest.raises(ValueError, match="2D"):
        self_forward(h.ravel())
    with pytest.raises(ValueError, match=r"\(H, W, 3\)"):
        solve_normal(h)
    normal = self_forward(h, bits=8)
    assert normal.shape == (32, 32, 3)
    # no cuadrada PAR soportada end-to-end
    h_rect = np.zeros((64, 128))
    row = evaluate_path(h_rect, self_forward(h_rect, bits=8), path="self_q8")
    assert row["path_rmse"] < 1e-12


def test_determinism_bitwise() -> None:
    n = 64
    _, x = _grid(n)
    y, _ = _grid(n)
    h = 0.3 * np.sin(2 * np.pi * 3 * x / n) + 0.2 * np.cos(2 * np.pi * 5 * y / n)
    normal = self_forward(h, bits=8)
    a = evaluate_path(h, normal, path="self_q8")
    b = evaluate_path(h, normal, path="self_q8")
    assert a.keys() == b.keys()
    assert all(a[k] == b[k] for k in a)


def test_fd_forward_diagnostic_close_on_bandlimited() -> None:
    # control §8: en contenido band-limited suave, FD ≈ espectral (diagnóstico, no primario).
    n = 128
    _, x = _grid(n)
    h = 0.2 * np.sin(2 * np.pi * 3 * x / n)
    row_spec = evaluate_path(h, self_forward(h, bits=8), path="self_q8")
    row_fd = evaluate_path(h, fd_forward(h, bits=8), path="fd_q8")
    assert row_fd["path_rmse"] < 10 * row_spec["path_rmse"] + 1e-3


# ---------------------------------------------------------------- §6 reglas


def _stats(rmse_s: float, rmse_a: float, corr_s: float, corr_a: float, var_s: float, var_a: float) -> dict[str, float]:
    return {
        "rmse_self_median": rmse_s,
        "rmse_auth_median": rmse_a,
        "delta_rmse_median": rmse_a - rmse_s,
        "abs_corr_self_median": corr_s,
        "abs_corr_auth_median": corr_a,
        "var_self_median": var_s,
        "var_auth_median": var_a,
    }


def test_rules_truth_table() -> None:
    good_self = _stats(0.03, 0.12, 0.97, 0.84, 0.90, 0.70)
    heldout_yes = _stats(0.03, 0.10, 0.97, 0.85, 0.90, 0.72)
    r = evaluate_rules(good_self, heldout_yes)
    assert r == {"C1_self_good": True, "C2_auth_worse": True}
    assert decide(True, True) == EXP_PAIR_MODEL_MISMATCH_DOMINANT

    bad_self = _stats(0.20, 0.22, 0.80, 0.78, 0.60, 0.58)
    heldout_no = _stats(0.20, 0.21, 0.80, 0.79, 0.60, 0.59)
    r = evaluate_rules(bad_self, heldout_no)
    assert r == {"C1_self_good": False, "C2_auth_worse": False}
    assert decide(False, False) == EXP_SOLVER_LIMITATION_PRESENT

    # C1 sin C2: techo bueno, authored NO claramente peor (delta full < barra 0.02)
    r = evaluate_rules(good_self, _stats(0.03, 0.04, 0.97, 0.96, 0.90, 0.88))
    assert r == {"C1_self_good": True, "C2_auth_worse": False}
    assert decide(True, False) == EXP_MIXED
    # enmienda §24-D: delta full fuerte pero held-out trivialmente pequeño (< 0.02) ⇒ C2 off
    r = evaluate_rules(good_self, _stats(0.03, 0.035, 0.97, 0.85, 0.90, 0.71))
    assert r == {"C1_self_good": True, "C2_auth_worse": False}

    # ¬C1 con C2: ambos factores presentes → MIXED (delta domina aunque el techo sea malo)
    bad_self_big_delta = _stats(0.20, 0.45, 0.80, 0.60, 0.60, 0.50)
    heldout_big_delta = _stats(0.20, 0.40, 0.80, 0.70, 0.60, 0.55)
    r = evaluate_rules(bad_self_big_delta, heldout_big_delta)
    assert r == {"C1_self_good": False, "C2_auth_worse": True}
    assert decide(False, True) == EXP_MIXED


def test_c2_heldout_direction_guard() -> None:
    # delta positivo en full pero negado en held-out ⇒ C2 no puede dispararse.
    full = _stats(0.03, 0.12, 0.97, 0.84, 0.90, 0.70)
    heldout_flip = _stats(0.03, 0.02, 0.97, 0.98, 0.90, 0.90)
    assert evaluate_rules(full, heldout_flip)["C2_auth_worse"] is False


def test_thresholds_match_prereg() -> None:
    # ancla contra §6 del preregistro (cualquier drift se ve en CI)
    assert (T_SELF_RMSE, T_SELF_CORR, T_SELF_VAR, T_DELTA_RMSE, T_DELTA_CORR) == (0.10, 0.95, 0.85, 0.02, 0.10)


def test_delta_stats_medians() -> None:
    rows = [
        {
            "asset": f"a{i}",
            "self_rmse": float(i),
            "auth_rmse": float(i) + 2.0,
            "self_abs_corr": 0.9,
            "auth_abs_corr": 0.8,
            "self_var": 0.9,
            "auth_var": 0.7,
            "delta_rmse": 2.0,
        }
        for i in range(5)
    ]
    s = delta_stats(rows)
    assert s["rmse_self_median"] == 2.0
    assert s["delta_rmse_median"] == 2.0
    assert s["abs_corr_self_median"] == pytest.approx(0.9)


def test_delta_stats_rejects_nonfinite() -> None:
    rows = [
        {
            "asset": "x",
            "self_rmse": float("nan"),
            "auth_rmse": 1.0,
            "self_abs_corr": 0.9,
            "auth_abs_corr": 0.8,
            "self_var": 0.9,
            "auth_var": 0.7,
            "delta_rmse": 1.0,
        }
    ]
    with pytest.raises(ValueError, match="no finito"):
        delta_stats(rows)


def test_bootstrap_median_ci_deterministic_and_sane() -> None:
    vals = [float(i) for i in range(31)]
    a = bootstrap_median_ci(vals)
    b = bootstrap_median_ci(vals)
    assert a == b  # seed preregistrada 20260925
    assert a["ci95_low"] <= a["point"] <= a["ci95_high"]
    assert a["point"] == 15.0


# ---------------------------------------------------------------- corpus remapeo


def test_remap_path_maps_originals_subtree(tmp_path: Path) -> None:
    win = r"C:\SkyClawResearch\NativeParallax\EXP-M3\originals\polyhaven\brick_4\f_nor.png"
    out = remap_path(win, tmp_path)
    assert out == tmp_path / "originals" / "polyhaven" / "brick_4" / "f_nor.png"
    with pytest.raises(ValueError, match="originals"):
        remap_path(r"C:\otra\cosa\f.png", tmp_path)


def _corpus_entry(asset_id: str, family: str, split: str) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "provider": "polyhaven",
        "family": family,
        "official_source": "https://example.invalid",
        "license": "CC0-1.0",
        "provenance_status": "OFFICIAL_HASH_VERIFIED",
        "normal_convention": "OPENGL",
        "height_semantics": "Displacement",
        "normal_resolution": [64, 64],
        "normal_path": rf"C:\SkyClawResearch\NativeParallax\EXP-M3\originals\polyhaven\{asset_id}\{asset_id}_nor.png",
        "height_path": rf"C:\SkyClawResearch\NativeParallax\EXP-M3\originals\polyhaven\{asset_id}\{asset_id}_disp.png",
        "_split": split,
    }


def _write_corpus(root: Path, entries: list[dict[str, Any]]) -> Path:
    import struct
    import zlib

    def png_bytes() -> bytes:
        # PNG mínimo válido (1x1 gris) — a prepare_entries sólo le importan bytes+sha.
        def chunk(typ: bytes, data: bytes) -> bytes:
            c = struct.pack(">I", len(data)) + typ + data
            return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
        idat = zlib.compress(b"\x00\xff")
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    blob = png_bytes()
    manifest_entries = []
    for e in entries:
        ndir = root / "originals" / "polyhaven" / str(e["asset_id"])
        ndir.mkdir(parents=True)
        (ndir / f"{e['asset_id']}_nor.png").write_bytes(blob)
        (ndir / f"{e['asset_id']}_disp.png").write_bytes(blob)
        d = {k: v for k, v in e.items() if k != "_split"}
        d["normal_sha256"] = hashlib.sha256(blob).hexdigest()
        d["height_sha256"] = hashlib.sha256(blob).hexdigest()
        manifest_entries.append(d)
    manifest = {
        "dataset": "test",
        "cohort_a": {
            "status": "READY",
            "assets": manifest_entries,
            "split_by_asset": {str(e["asset_id"]): e["_split"] for e in entries},
        },
    }
    p = root / "manifest.json"
    p.write_text(json.dumps(manifest))
    return p


def test_prepare_entries_remaps_and_verifies(tmp_path: Path) -> None:
    entries = [
        # 15 assets / 3 familias para pasar el gate §3 de M3 (mismo mínimo)
        *[_corpus_entry(f"brick_{i:02d}", "brick", "CALIBRATION") for i in range(5)],
        *[_corpus_entry(f"stone_{i:02d}", "stone", "HELD_OUT") for i in range(5)],
        *[_corpus_entry(f"rock_{i:02d}", "rock", "CALIBRATION") for i in range(5)],
    ]
    manifest = _write_corpus(tmp_path / "corpus", entries)
    prepared, exclusions = prepare_entries(manifest, tmp_path / "corpus")
    assert len(prepared) == 15
    assert exclusions == []
    first = prepared[0]
    assert tmp_path / "corpus" / "originals" / "polyhaven" in Path(first["normal_path"]).parents
    assert Path(first["normal_path"]).is_file()


def test_prepare_entries_registers_objective_exclusions(tmp_path: Path) -> None:
    entries = [
        *[_corpus_entry(f"brick_{i:02d}", "brick", "CALIBRATION") for i in range(5)],
        *[_corpus_entry(f"stone_{i:02d}", "stone", "HELD_OUT") for i in range(5)],
        *[_corpus_entry(f"rock_{i:02d}", "rock", "CALIBRATION") for i in range(5)],
    ]
    corpus_root = tmp_path / "corpus"
    manifest = _write_corpus(corpus_root, entries)
    # corrupción objetiva: bytes cambian ⇒ sha256 difiere del manifest; y un asset sin archivo
    victim = corpus_root / "originals" / "polyhaven" / "rock_03" / "rock_03_disp.png"
    victim.write_bytes(b"corrupted")
    shutil.rmtree(corpus_root / "originals" / "polyhaven" / "rock_04")
    prepared, exclusions = prepare_entries(manifest, corpus_root)
    reasons = {e["asset_id"]: e["reason"] for e in exclusions}
    assert "sha256_mismatch: height rock_03_disp.png" in reasons["rock_03"]
    assert "file_missing" in reasons["rock_04"]
    assert len(prepared) == 13


def test_prepare_entries_hard_stops_on_split_mismatch(tmp_path: Path) -> None:
    entries = [
        *[_corpus_entry(f"brick_{i:02d}", "brick", "CALIBRATION") for i in range(5)],
        *[_corpus_entry(f"stone_{i:02d}", "stone", "HELD_OUT") for i in range(5)],
        *[_corpus_entry(f"rock_{i:02d}", "rock", "CALIBRATION") for i in range(5)],
    ]
    # miento el split del manifest para rock_00 (la regla dice CALIBRATION)
    entries[10]["_split"] = "HELD_OUT"
    manifest = _write_corpus(tmp_path / "corpus", entries)
    with pytest.raises(RuntimeError, match="split inconsistente"):
        prepare_entries(manifest, tmp_path / "corpus")


# ---------------------------------------------------------------- E2E runner (corpus sintético)


def _write_synthetic_asset(root: Path, asset_id: str, n: int = 64, seed: int = 0) -> tuple[str, str]:
    """Par normal+height COHERENTE por construcción (forward SELF de un height sintético).

    Control positivo del runner: para un par coherente, SELF y AUTH deben dar métricas
    cercanas — sólo los diferencia resize+renormalización de la normal authored (medido:
    delta ≈ 0.017 con upsample 8× en pink noise). La normal se declara en SU convención
    real (DIRECTX-form, la que produce normals_from_gradients): si se declarara OPENGL,
    el loader le haría flip-Y (correcto para nor_gl reales) y decoherizaría el par
    sintético — el bug que este control positivo cazó.
    """
    from PIL import Image

    from sky_claw.local.native_parallax.research.solver_coherence import self_forward

    rng = np.random.default_rng(seed)
    fx = np.fft.fftfreq(n)
    wy, wx = np.meshgrid(2 * np.pi * fx * n, 2 * np.pi * fx * n, indexing="ij")
    spec = rng.normal(size=(n, n)) * np.exp(-0.15 * np.sqrt(wx**2 + wy**2) / (2 * np.pi))
    h = np.real(np.fft.ifft2(spec))
    h = (h - h.min()) / (h.max() - h.min())
    normal = self_forward(h, bits=8)
    rgb = np.clip((normal * 0.5 + 0.5) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    h8 = np.clip(h * 255.0 + 0.5, 0, 255).astype(np.uint8)
    adir = root / "originals" / "polyhaven" / asset_id
    adir.mkdir(parents=True)
    Image.fromarray(rgb, mode="RGB").save(adir / f"{asset_id}_nor.png")
    Image.fromarray(h8, mode="L").save(adir / f"{asset_id}_disp.png")
    return (
        hashlib.sha256((adir / f"{asset_id}_nor.png").read_bytes()).hexdigest(),
        hashlib.sha256((adir / f"{asset_id}_disp.png").read_bytes()).hexdigest(),
    )


def _synthetic_corpus(root: Path, n_assets: int = 15) -> Path:
    """Corpus sintético completo (5 brick/5 stone/5 rock, 64²) con paths estilo manifest M3."""
    families = [("brick", "CALIBRATION"), ("stone", "HELD_OUT"), ("rock", "CALIBRATION")]
    entries = []
    for i in range(n_assets):
        family, split = families[i % 3]
        asset_id = f"syn_{family}_{i:02d}"
        nsha, hsha = _write_synthetic_asset(root, asset_id, seed=i)
        entries.append(
            {
                "asset_id": asset_id,
                "provider": "polyhaven",
                "family": family,
                "official_source": "https://example.invalid",
                "normal_sha256": nsha,
                "height_sha256": hsha,
                "license": "CC0-1.0",
                "provenance_status": "OFFICIAL_HASH_VERIFIED",
                "normal_convention": "DIRECTX",
                "height_semantics": "Displacement",
                "normal_resolution": [64, 64],
                "height_bit_depth": 8,
                "notes": "",
                "license_reference": "https://example.invalid/license",
                "official_download_url_or_identifier": asset_id,
                "material_name": asset_id,
                "official_hash": None,
                "download_timestamp": "2026-09-25T00:00:00Z",
                "normal_filename_original": f"{asset_id}_nor.png",
                "height_filename_original": f"{asset_id}_disp.png",
                "normal_size_bytes": 0,
                "height_size_bytes": 0,
                "height_resolution": [64, 64],
                "normal_format": "PNG",
                "height_format": "PNG",
                "normal_path": rf"C:\SkyClawResearch\NativeParallax\EXP-M3\originals\polyhaven\{asset_id}\{asset_id}_nor.png",
                "height_path": rf"C:\SkyClawResearch\NativeParallax\EXP-M3\originals\polyhaven\{asset_id}\{asset_id}_disp.png",
                "_split": split,
            }
        )
    manifest = {
        "dataset": "synthetic e2e",
        "cohort_a": {
            "status": "READY",
            "assets": [{k: v for k, v in e.items() if k != "_split"} for e in entries],
            "split_by_asset": {e["asset_id"]: e["_split"] for e in entries},
        },
    }
    p = root / "manifest.json"
    p.write_text(json.dumps(manifest))
    return p


def test_run_exp_m4_end_to_end_synthetic_calibration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """E2E §9/§13: el runner completo corre sin red sobre un corpus sintético coherente."""
    import sys

    from sky_claw.local.native_parallax.research import run_exp_m4

    corpus = tmp_path / "corpus"
    manifest = _synthetic_corpus(corpus)
    out = tmp_path / "cal.json"
    argv = sys.argv
    sys.argv = [
        "run_exp_m4",
        "--m3-manifest",
        str(manifest),
        "--corpus-root",
        str(corpus),
        "--out",
        str(out),
        "--phase",
        "calibration",
    ]
    try:
        run_exp_m4.main()
    finally:
        sys.argv = argv
    report = json.loads(out.read_text())
    assert report["experiment"] == "EXP-M4"
    assert report["phase"] == "calibration"
    assert report["summary"]["decision"] == "PENDING_FREEZE"
    assert report["dataset"]["n_rows"] == 10  # sólo CALIBRATION (brick+rock)
    for row in report["rows"]:
        assert row["tested_convention"] == "DIRECTX"
        # control positivo: par coherente ⇒ AUTH ≈ SELF (sólo los diferencia resize+renorm)
        assert abs(row["delta_rmse"]) < 0.05
        assert row["self_abs_corr"] > 0.95
    assert "self_float" in report["summary"]["secondary"]
    _ = capsys  # el runner imprime progreso; no lo validamos


def test_run_exp_m4_full_phase_requires_frozen_ack(tmp_path: Path) -> None:
    import sys

    import pytest as _pytest

    from sky_claw.local.native_parallax.research import run_exp_m4

    corpus = tmp_path / "corpus"
    manifest = _synthetic_corpus(corpus)
    argv = sys.argv
    sys.argv = [
        "run_exp_m4",
        "--m3-manifest",
        str(manifest),
        "--corpus-root",
        str(corpus),
        "--out",
        str(tmp_path / "full.json"),
        "--phase",
        "full",
    ]
    try:
        with _pytest.raises(SystemExit):
            run_exp_m4.main()  # sin --frozen-ack: parser.error
    finally:
        sys.argv = argv


def test_run_exp_m4_full_phase_decision_vocabulary(tmp_path: Path) -> None:
    import sys

    from sky_claw.local.native_parallax.research import run_exp_m4
    from sky_claw.local.native_parallax.research.solver_coherence import (
        EXP_DATA_INSUFFICIENT,
        EXP_INVALIDATED_BY_UPSTREAM_BUG,
    )
    from sky_claw.local.native_parallax.research.solver_coherence import (
        EXP_MIXED as _MIXED,
    )
    from sky_claw.local.native_parallax.research.solver_coherence import (
        EXP_PAIR_MODEL_MISMATCH_DOMINANT as _PAIR,
    )
    from sky_claw.local.native_parallax.research.solver_coherence import (
        EXP_SOLVER_LIMITATION_PRESENT as _SOLVER,
    )

    assert {_PAIR, _SOLVER, _MIXED, EXP_DATA_INSUFFICIENT, EXP_INVALIDATED_BY_UPSTREAM_BUG} == {
        "EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT",
        "EXP_M4_SOLVER_LIMITATION_PRESENT",
        "EXP_M4_MIXED",
        "EXP_M4_DATA_INSUFFICIENT",
        "EXP_M4_INVALIDATED_BY_UPSTREAM_BUG",
    }
    corpus = tmp_path / "corpus"
    manifest = _synthetic_corpus(corpus)
    out = tmp_path / "full.json"
    argv = sys.argv
    sys.argv = [
        "run_exp_m4",
        "--m3-manifest",
        str(manifest),
        "--corpus-root",
        str(corpus),
        "--out",
        str(out),
        "--phase",
        "full",
        "--frozen-ack",
        "freeze-test-synthetic",
    ]
    try:
        run_exp_m4.main()
    finally:
        sys.argv = argv
    report = json.loads(out.read_text())
    assert report["environment"]["frozen_ack"] == "freeze-test-synthetic"
    assert report["summary"]["decision"] in {
        "EXP_M4_PAIR_MODEL_MISMATCH_DOMINANT",
        "EXP_M4_SOLVER_LIMITATION_PRESENT",
        "EXP_M4_MIXED",
    }
    assert (
        report["summary"]["bootstrap"]["delta_rmse_full"]["ci95_low"]
        <= report["summary"]["bootstrap"]["delta_rmse_full"]["point"]
    )
    assert report["dataset"]["split_counts"] == {"CALIBRATION": 10, "HELD_OUT": 5}

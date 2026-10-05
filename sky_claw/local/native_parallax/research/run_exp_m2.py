"""EXP-M2 — runner (research-only, §40): authored normals → trust → reconstrucción.

Stages:
  M2-B compatibilidad normal↔height (oráculo, edge seams, TWO_CHANNEL_Q8_PROXY)
  M2-C baseline RAW
  M2-D comparación de proxies normal-only vs error (selección SOLO en CALIBRATION)
  M2-E transfer test con parámetros EXP-M1 SIN retuning (SOFT λ=1·σ_eff, CLAMP λ=1·σ_eff)
  M2-F sweep k sólo si el transfer falla (queda registrado igual)
  M2-G evaluación held-out por familia/fuente
  M2-H sensibilidad de resolución (subset 1024²)

Oráculo (authored height) SOLO para evaluar, jamás para construir la policy (§20).
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from sky_claw.local.native_parallax.research import metrics as np_m0_metrics
from sky_claw.local.native_parallax.research.authored_dataset import (
    AuthoredMaterial,
    MaterialSpec,
    edge_seam_ratio,
    load_asset,
    load_manifest,
    two_channel_q8_proxy,
)
from sky_claw.local.native_parallax.research.fetch_exp_m2_corpus import CALIBRATION_FAMILIES
from sky_claw.local.native_parallax.research.normal_fft_periodic import integrate_periodic
from sky_claw.local.native_parallax.research.nz_policies import decode_gradients_policy
from sky_claw.local.native_parallax.research.trust_proxies import (
    SIGMA_EFF_CANDIDATES,
    OracleOnly,
    normal_only_features,
)

RES_MAIN = 512
CATASTROPHIC_RMSE = 0.25  # tras alineación afín a altura en [0,1]
CATASTROPHIC_VAR = 0.5
K_TRANSFER = 1.0  # EXP-M1: SOFT k≈1, evaluado SIN retuning (§32)
K_SWEEP = (0.5, 2.0, 4.0)

COLUMNS = (
    "asset",
    "family",
    "split",
    "policy",
    "k",
    "sigma_eff",
    "r_p01_proxy",
    "nz_min",
    "nz_p01",
    "nz_p05",
    "negative_nz_fraction",
    "activation_fraction",
    "low_trust_fraction",
    "aligned_rmse",
    "raw_centered_rmse",
    "gradient_rmse",
    "normal_angle_mean",
    "correlation",
    "best_sign",
    "seam_height",
    "seam_gradient",
    "variance_ratio",
    "gradient_energy_ratio",
    "max_gradient",
    "p99_gradient",
    "catastrophic",
    "affine_scale",
    "runtime_ms",
)


def _is_catastrophic(m: dict[str, float]) -> bool:
    return bool(m["aligned_rmse"] > CATASTROPHIC_RMSE or m["variance_ratio"] < CATASTROPHIC_VAR)


def reconstruct_from_normal(
    normal: NDArray[np.float64],
    policy: str,
    lam: float,
    sigma_eff: float,
) -> tuple[NDArray[np.float64], Any]:
    """Camino de reconstrucción SOLO-normal (§20): nunca recibe height (M1)."""
    p, q, stats = decode_gradients_policy(normal, 1.0, 1.0, policy, lam, sigma_eff)
    rec, _info = integrate_periodic(p, q)
    return rec, stats


def split_of(family: str) -> str:
    return "CALIBRATION" if family in CALIBRATION_FAMILIES else "HELD_OUT"


def run_policy(
    mat: AuthoredMaterial,
    spec: MaterialSpec,
    split: str,
    policy: str,
    k: float,
    sigma_eff: float,
    *,
    scale_ref: float | None = None,
    sign_ref: float = 1.0,
) -> dict[str, Any]:
    """Una reconstrucción + evaluación contra el oráculo.

    ``scale_ref``/``sign_ref``: conversión de unidades fitada en el RAW del MISMO asset
    (evaluation-only, §11). Compartirla entre policies es lo que hace que
    ``variance_ratio`` mida aplanado y no el mismatch de unidades authored.
    """
    t0 = time.perf_counter()
    lam = 0.0 if policy == "RAW" else k * sigma_eff
    rec, stats = reconstruct_from_normal(mat.normal, policy, lam, sigma_eff)
    runtime_ms = (time.perf_counter() - t0) * 1000.0
    fit = OracleOnly.fit_global_scale(mat.height, rec)
    scale = fit["affine_scale"] if scale_ref is None else scale_ref
    sign = fit["oracle_best_sign"] if scale_ref is None else sign_ref
    # la escala YA incluye el signo (cov/var): NO multiplicar además por best_sign
    # (doble negación — bug detectado en revisión; best_sign queda como diagnóstico §36)
    rec_eval = rec * scale
    m = OracleOnly.evaluate(mat.height, rec_eval, mat.normal)
    feats = normal_only_features(mat.normal, sigma_eff=sigma_eff if sigma_eff > 0 else 0.0)
    row: dict[str, Any] = {
        "asset": spec.asset_id,
        "family": spec.family,
        "split": split,
        "policy": policy,
        "k": k,
        "sigma_eff": sigma_eff,
        "r_p01_proxy": feats["nz_p01"] / sigma_eff if sigma_eff > 0 else float("inf"),
        "activation_fraction": stats.activation_fraction,
        "negative_nz_fraction": stats.negative_nz_fraction,
        "low_trust_fraction": stats.low_trust_fraction,
        "max_gradient": stats.max_gradient,
        "p99_gradient": stats.p99_gradient,
        "affine_scale": fit["affine_scale"],
        "oracle_best_sign": sign,
        "catastrophic": _is_catastrophic(m),
        "runtime_ms": runtime_ms,
    }
    row.update(m)
    return row


def characterize_asset(mat: AuthoredMaterial) -> dict[str, Any]:
    """M2-B: compatibilidad y caracterización (features + oráculo, sin policies)."""
    feats = normal_only_features(mat.normal, sigma_eff=0.0)
    oracle_agree = OracleOnly.normal_height_residual_oracle(mat.normal, mat.height)
    return {
        "features": feats,
        "oracle_agreement_deg": oracle_agree["height_normal_oracle_agreement_deg"],
        "oracle_best_strength": oracle_agree["oracle_best_strength"],
        "normal_seams": edge_seam_ratio(mat.normal[..., 0]),
        "height_seams": edge_seam_ratio(mat.height),
        "two_channel_proxy": two_channel_q8_proxy(mat.normal),
    }


def spearman(x: list[float], y: list[float]) -> float:
    """Wrapper delgado sobre la primitiva canónica ``metrics.spearman`` (sin scipy).

    Conserva la API histórica de M2 (guarda ``n < 3`` ⇒ ``nan``) pero NO reimplementa
    el ranking: la corrección de empates (rango medio del grupo) vive en un solo lugar.
    Antes esto era un segundo doble ``argsort`` independiente, que asignaba rangos
    ordinales distintos a valores iguales.
    """
    if len(x) < 3:
        return float("nan")
    return np_m0_metrics.spearman(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))


def select_sigma_eff(characs: dict[str, dict[str, Any]], assets: dict[str, MaterialSpec]) -> dict[str, Any]:
    """M2-D: elegir σ_eff candidata SOLO con CALIBRATION (§41)."""
    calib = {a: c for a, c in characs.items() if split_of(assets[a].family) == "CALIBRATION"}
    baseline_err = {a: c["raw_eval"]["aligned_rmse"] for a, c in calib.items()}
    grad_err = {a: c["raw_eval"]["gradient_rmse"] for a, c in calib.items()}
    table: list[dict[str, Any]] = []
    for name, fn in SIGMA_EFF_CANDIDATES.items():
        sigma_per_asset = {a: float(fn(characs[a]["_normal"], characs[a]["features"])) for a in calib}
        for target, errs in (("aligned_rmse", baseline_err), ("gradient_rmse", grad_err)):
            rho = spearman(
                [-np.log10(max(sigma_per_asset[a], 1e-9)) for a in calib],
                [np.log10(max(errs[a], 1e-9)) for a in calib],
            )
            table.append(
                {
                    "candidate": name,
                    "target": target,
                    "spearman": rho,
                    "sigma_median": float(np.median(list(sigma_per_asset.values()))),
                }
            )
    # score principal: spearman vs aligned_rmse (riesgo ⇒ σ_eff alta ⇒ r bajo ⇒ error alto)
    # §14: un rho no evaluable (NaN, p.ej. calibración < 3 assets o candidato degenerado)
    # NO puede entrar al ordenamiento — `sorted` con claves NaN elige por accidente.
    ranked = sorted(
        (t for t in table if t["target"] == "aligned_rmse" and np.isfinite(t["spearman"])),
        key=lambda t: -t["spearman"],
    )
    # §2 MATH-B.1 — fail-closed: sin NINGÚN candidato evaluable no hay ganador. El
    # fallback histórico a "nz_p01" no era fail-closed: elegía un σ_eff sin respaldo
    # estadístico y M2-E/F corrían igual, así que un corpus donde la selección no es
    # medible producía resultados presentables como si lo fuera.
    if not ranked:
        return {"table": table, "winner": None, "status": "NO_EVALUABLE_SIGMA_CANDIDATE", "per_asset": None}
    return {"table": table, "winner": ranked[0]["candidate"], "status": "EVALUABLE", "per_asset": None}


def evaluate_sigma_policies(
    specs: list[MaterialSpec],
    characs: dict[str, dict[str, Any]],
    sel: dict[str, Any],
    resolution: int,
) -> list[dict[str, Any]]:
    """M2-E (transfer, k=1) + M2-F (sweep de k) — SOLO con un σ_eff elegido válidamente.

    Fail-closed (§3 MATH-B.1): sin ganador no hay σ_eff, y sin σ_eff no se fabrica
    política. Devuelve la lista vacía y no toca ``sel["per_asset"]``; el llamador
    conserva las filas RAW ya calculadas como evidencia. ``NO_WINNER`` ⇒
    ``NO_TRANSFER_POLICY_EXECUTION``, anclado por test.
    """
    if sel["winner"] is None:
        return []
    sigma_fn = SIGMA_EFF_CANDIDATES[sel["winner"]]
    sigma_eff: dict[str, float] = {
        a: max(float(sigma_fn(c["_normal"], c["features"])), 1e-6) for a, c in characs.items()
    }
    sel["per_asset"] = sigma_eff

    rows: list[dict[str, Any]] = []
    for spec in specs:
        split = split_of(spec.family)
        mat = load_asset(spec, resolution)
        s = sigma_eff[spec.asset_id]
        raw_row = characs[spec.asset_id]["raw_eval"]
        refs = {"scale_ref": raw_row["affine_scale"], "sign_ref": raw_row["oracle_best_sign"]}
        rows.append(run_policy(mat, spec, split, "SOFT_TIKHONOV", K_TRANSFER, s, **refs))
        rows.append(run_policy(mat, spec, split, "FLOOR_CLAMP", K_TRANSFER, s, **refs))
        if split == "CALIBRATION":
            for k in K_SWEEP:
                rows.append(run_policy(mat, spec, split, "SOFT_TIKHONOV", k, s, **refs))
                rows.append(run_policy(mat, spec, split, "FLOOR_CLAMP", k, s, **refs))
    return rows


def _json_safe(obj: Any) -> Any:
    """Sanea recursivamente el artefacto M2 al boundary JSON: no finito → ``None``.

    ``NaN``/``Infinity``/``-Infinity`` no pertenecen al JSON estándar (RFC 8259 §6).
    ``json.dumps`` con ``allow_nan=True`` —su default— los puede EMITIR igual, y el
    ``json.loads`` de Python los vuelve a leer por extensión propia: el artefacto
    queda, en los hechos, legible sólo por Python. Esos tokens no existen en el
    estándar, así que ``JSON.parse`` de JavaScript los RECHAZA (y ``jq`` o cualquier
    consumidor estricto también), igual que rechazaría un ``undefined``.

    Los no finitos son alcanzables por resultados LEGÍTIMOS de M2, no por bug:

    - ``hf_energy_ratio`` = NaN cuando el height es band-limited (NOT_INFORMATIVE, M7);
    - ``r_p01_proxy``/``r_p05_proxy``/``r_min_proxy`` = ±inf en la fila RAW, porque
      σ_eff=0 por definición (``normal_only_features``);
    - ``spearman`` = NaN cuando ningún candidato de σ_eff es evaluable (MATH-B.1).

    Por eso el saneado es del ARTEFACTO COMPLETO y no de una clave conocida: si
    mañana otro diagnóstico (``oracle``, ``hf_energy_ratio``, una tabla anidada)
    devuelve un no finito, ya está cubierto. ``None`` es la única traducción: no
    0, no ``-1``, no ``"NaN"`` — un valor numérico significaría un dato que la
    corrida NO midió. ``winner=None`` (NO_EVALUABLE_SIGMA_CANDIDATE) conserva su
    significado exacto.

    Preserva sin alteración números finitos, ``int``, ``bool``, ``str`` y ``None``;
    un objeto de tipo desconocido se devuelve tal cual (``json.dumps`` fallará con
    TypeError en vez de convertirlo en silencio).
    """
    if isinstance(obj, bool):  # antes de los números: un bool no se sanea
        return obj
    if isinstance(obj, (float, np.floating)):  # np.float64 ya es subclase de float
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):  # el payload los serializa como arrays
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _json_safe(obj.tolist())
    return obj


def _write_json(path: Path, payload: Any) -> None:
    """Escribe un artefacto M2 saneado y ESTRICTO (``allow_nan=False``).

    Punto de escritura único de los dos artefactos de ``main`` (``rows.json`` y
    ``characs.json``): así el contrato no se puede cumplir en uno y olvidar en el
    otro. ``allow_nan=False`` no es cosmético — convierte un no finito que el
    saneado dejara pasar en un fallo ruidoso en vez de en un literal no estándar.
    """
    path.write_text(json.dumps(_json_safe(payload), indent=1, allow_nan=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resolution", type=int, default=RES_MAIN)
    args = parser.parse_args()
    specs = load_manifest(args.manifest)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    characs: dict[str, dict[str, Any]] = {}
    assets: dict[str, MaterialSpec] = {}
    rows: list[dict[str, Any]] = []
    for spec in specs:
        split = split_of(spec.family)
        mat = load_asset(spec, args.resolution)
        assets[spec.asset_id] = spec
        c = characterize_asset(mat)
        raw = run_policy(mat, spec, split, "RAW", 0.0, 0.0)
        c["raw_eval"] = raw
        c["_normal"] = mat.normal
        # diagnóstico §12 (evaluación-only) para convención UNKNOWN
        if spec.declared_convention == "UNKNOWN":
            flipped = AuthoredMaterial(
                spec=spec,
                tested_convention="FLIPPED",
                normal=np.stack([mat.normal[..., 0], -mat.normal[..., 1], mat.normal[..., 2]], -1),
                height=mat.height,
            )
            raw_flip = run_policy(flipped, spec, split, "RAW", 0.0, 0.0)
            c["oracle_best_convention"] = "FLIPPED" if raw_flip["aligned_rmse"] < raw["aligned_rmse"] else "AS_IS"
            c["convention_margin_rmse"] = abs(raw["aligned_rmse"] - raw_flip["aligned_rmse"])
        characs[spec.asset_id] = c
        rows.append(raw)
        print(
            f"{spec.asset_id:>16} {split:>11} raw_rmse={raw['aligned_rmse']:.4f} "
            f"grad={raw['gradient_rmse']:.3g} var={raw['variance_ratio']:.3f} "
            f"oracle_agree={c['oracle_agreement_deg']:.1f}° cat={raw['catastrophic']}"
        )

    sel = select_sigma_eff(characs, assets)
    # M2-E transfer: k=1 fijo, TODOS los assets, sin retuning; M2-F sweep k sólo
    # calibración. Sin winner no se ejecuta nada de esto (§3 MATH-B.1).
    rows.extend(evaluate_sigma_policies(specs, characs, sel, args.resolution))

    characs_out = {a: {k: v for k, v in c.items() if k != "_normal"} for a, c in characs.items()}
    _write_json(out / "characs.json", characs_out)
    _write_json(
        out / "rows.json",
        {
            "columns": COLUMNS,
            "rows": rows,
            "sigma_selection": {"winner": sel["winner"], "status": sel["status"], "table": sel["table"]},
        },
    )
    if sel["winner"] is None:
        print(
            f"\nSIGMA_SELECTION_STATUS={sel['status']}: ningún candidato de σ_eff es evaluable sobre "
            f"CALIBRATION — no se ejecutan las policies dependientes de σ_eff (M2-E/F). "
            f"Se conservan las {len(rows)} filas RAW ya calculadas."
        )
    print(f"\nrows={len(rows)} sigma_eff_winner={sel['winner']} status={sel['status']} → {out / 'rows.json'}")


if __name__ == "__main__":
    main()

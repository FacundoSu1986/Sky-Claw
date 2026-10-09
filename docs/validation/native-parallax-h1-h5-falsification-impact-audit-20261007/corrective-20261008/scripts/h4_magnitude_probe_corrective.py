"""F4 + F5 — Sonda de magnitud H4 correctiva (AUDIT_ONLY).

Corrige los dos defectos del `h4_magnitude_probe.py` original:

  F4  La sonda original probaba amplitudes `[1.0, 0.3, 0.1, 0.03]`, pero el claim externo
      adjudicado se midió en `c = 0.05` y `c = 0.01` (audit externo, S07/S15, 1024→512).
      Como H4 es cuantización y el efecto es NO LINEAL con la magnitud de la pendiente,
      los valores viejos no falsan el claim citado.
      => Batería mínima exigida: `1.0, 0.3, 0.1, 0.05, 0.03, 0.01` (+ `0.07`, `0.02` de
      continuidad). `0.05` y `0.01` son obligatorios.

  F5  `h_nat` está centrado y contiene negativos; `resize_height` clipea a `[0, 1]`; pero
      `n_nat = self_forward(h_nat)` sale del campo NO clipeado. Resultado: el height target
      y la superficie usada para las normales NO describen la misma superficie — el RMSE
      downstream medía en su mayoría el escalón artificial del clip.
      => Se construye la superficie equivalente  `h_safe = h_nat + offset`  con offset
      suficiente para mantener `0 <= h_safe <= 1`, SIN scaling (el offset constante
      preserva gradientes: ∇(h + c) = ∇h, y con gradientes periódicos espectrales el
      offset desaparece exactamente al derivar).

      Se emiten los invariantes sintéticos (§21):
        - min(h_safe) >= 0, max(h_safe) <= 1
        - gradient(h_safe) == gradient(h_nat) dentro de la tolerancia numérica
        - el target de normales corresponde a h_safe

      Corregido en la quinta ronda (hallazgo del Oracle sobre el HEAD 3ba2609e): el tercer
      invariante era TAUTOLÓGICO. Comparaba `resize_height_unclipped(h_safe, TARGET)` con
      una segunda llamada idéntica sobre el mismo input, así que `np.allclose` daba `True`
      por construcción y no podía fallar. Ahora la comparación es contra `resize(h_nat)`
      (ver `target_matches_safe_surface`), con tolerancias medidas sobre la batería real:
      el camino correcto queda a <=8.3e-5 y el camino clipeado que F5 elimina a >=6.2e-2.
      El resultado del corpus no cambió (`target_corresponds_to_h_safe` sigue siendo `True`
      en todas las combinaciones); lo que cambió es que ahora es evidencia y no una aserción
      vacía.

      Se reportan AMBOS brazos (clipeado viejo y seguro nuevo) para medir cuánto del efecto
      reportado era artefacto del clip.

Uso:
  PYTHONPATH=<worktree>:<corrective_scripts> python h4_magnitude_probe_corrective.py --out <json>
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from sky_claw.local.native_parallax.research.authored_dataset import resize_height, resize_normal
from sky_claw.local.native_parallax.research.normal_from_height import (
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.solver_coherence import evaluate_path, self_forward
from sky_claw.local.native_parallax.research.synthetic_height import PERIODIC_CASES

NATIVE = 1024
TARGET = 512

# F4: batería mínima exigida por el brief §19. 0.05 y 0.01 son OBLIGATORIAS.
AMPLITUDES_REQUIRED = [1.0, 0.3, 0.1, 0.05, 0.03, 0.01]
AMPLITUDES_CONTINUITY = [0.07, 0.02]
AMPLITUDES = sorted(set(AMPLITUDES_REQUIRED + AMPLITUDES_CONTINUITY), reverse=True)
OBLIGATORIAS = [0.05, 0.01]

# Tolerancias de los invariantes sintéticos (§21).
# El offset de F5 se aplica en float64 y se deriva con FFT: la igualdad es exacta en
# matemática (∇(h+c) = ∇h) y sólo se pierde por redondeo de punto flotante acumulado
# sobre 1024². 1e-9 es holgadamente más estricto que cualquier efecto físico medido
# (el efecto del clip es O(1e-1)); 1e-12 resultaba por debajo del ruido de la FFT.
GRADIENT_TOL = 1e-9
RANGE_TOL = 1e-12

# Tolerancias del invariante F5 EN LA RESOLUCIÓN TARGET (ver
# `target_matches_safe_surface`). Son medidas, no elegidas a ojo: separan el camino
# correcto (`resize(h_safe)` vs `resize(h_nat)`: <=8.3e-5 en gradiente y <=8.3e-8 en
# offset, sobre las 51 combinaciones válidas de la batería) del camino clipeado que la
# corrección elimina (>=6.2e-2 y >=2.1e-3 respectivamente). Cada tolerancia queda al
# menos 12x por encima del régimen correcto y 62x por debajo del defectuoso.
TARGET_GRADIENT_TOL = 1e-3
TARGET_OFFSET_TOL = 1e-5


def resize_normal_float_counterfactual(n: np.ndarray, size: int) -> np.ndarray:
    """Contrafactual FLOAT del resize de normal (idéntico al de la auditoría original)."""
    if n.shape[0] == size and n.shape[1] == size:
        return np.asarray(n, dtype=np.float64)
    ch = []
    for c in range(3):
        im = Image.fromarray(np.asarray(np.clip(n[..., c], -1.0, 1.0), dtype=np.float32))
        ch.append(np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64))
    out = np.stack(ch, axis=-1)
    return np.asarray(out / np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-12), dtype=np.float64)


def resize_height_unclipped(h: np.ndarray, size: int) -> np.ndarray:
    """Resize float SIN clip a [0,1] — espejo del camino que usa `resize_height` internamente.

    Pillow modo F acepta valores fuera de [0,1]; el único clip es el explícito de
    `resize_height`. Este helper lo evita para medir la superficie real.
    """
    if h.shape[0] == size and h.shape[1] == size:
        return np.asarray(h, dtype=np.float64)
    im = Image.fromarray(np.asarray(h, dtype=np.float32))
    return np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64)


def safe_surface(h_centered: np.ndarray) -> tuple[np.ndarray, float]:
    """F5: desplaza el campo centrado al rango [0,1] SIN cambiar gradientes.

    Devuelve `(h_safe, offset)`. El offset es el mínimo necesario para que
    `0 <= h_safe <= 1`; no se aplica scaling.
    """
    lo = float(h_centered.min())
    hi = float(h_centered.max())
    # offset suficiente para subir el mínimo a 0 (si hace falta) sin que el max supere 1.
    offset = max(0.0, -lo)
    if hi + offset > 1.0:
        # la amplitud excede el rango authored: inflar hacia arriba no alcanza.
        raise ValueError(
            f"safe_surface: amplitud fuera de rango válido (min={lo:.6f}, max={hi:.6f}); no se clipea en silencio"
        )
    return h_centered + offset, offset


def _grad_check(h_safe: np.ndarray, h_nat: np.ndarray) -> dict[str, float]:
    p1, q1 = spectral_gradients(h_safe)
    p2, q2 = spectral_gradients(h_nat)
    return {
        "max_abs_dp": float(np.max(np.abs(p1 - p2))),
        "max_abs_dq": float(np.max(np.abs(q1 - q2))),
    }


def target_matches_safe_surface(
    h_safe: np.ndarray,
    h_nat: np.ndarray,
    size: int,
    *,
    gradient_tol: float,
    offset_tol: float,
) -> dict[str, Any]:
    """¿El target de normales derivado de `resize(h_safe)` corresponde a `h_safe`?

    Contrato (finding F5, corregido tras la revisión del Oracle): la superficie segura es
    `h_safe = h_nat + offset` con `offset` constante. El resize bilineal es **lineal**, de
    modo que `resize(h_safe) == resize(h_nat) + offset`; y como la superficie de normales
    depende sólo de los gradientes, con ∇(h + c) = ∇h el offset no debe alterarla.

    Verificar eso exige comparar `resize(h_safe)` contra `resize(h_nat)` — **no** contra sí
    misma. La versión previa de esta sonda comparaba `resize_height_unclipped(h_safe)` con
    una segunda llamada idéntica sobre el MISMO input: los dos arrays eran iguales por
    construcción, `np.allclose` daba `True` siempre y el invariante no podía fallar. El
    defecto F5 que la corrección dice eliminar quedaba, por lo tanto, sin evidencia.

    Se verifican dos propiedades, ambas necesarias:

    1. **Gradientes**: `∇resize(h_safe) ≈ ∇resize(h_nat)` — la superficie de normales en la
       resolución target es la misma que la del campo natural.
    2. **Offset superviviente**: `resize(h_safe) - resize(h_nat) ≈ offset` en todo el campo
       — el desplazamiento DC atraviesa el resize sin deformarse. Sin esta condición, un
       resize que escalara o recortara el campo podría dejar pasar la propiedad 1.

    Las tolerancias se midieron sobre la batería real (7 casos x amplitudes válidas = 51
    combinaciones), comparando el camino correcto contra el camino clipeado (el defecto F5):

        canal             correcto (max)   clipeado (min)   tolerancia
        |d∇| dp y dq      8.3e-5           6.2e-2           1e-3
        error de offset   8.3e-8           2.1e-3           1e-5

    El canal `dq` puede ser exactamente 0 en ambos brazos (casos con `q ≡ 0`), así que no
    discrimina por sí solo: por eso se exigen los tres canales a la vez y el error de offset
    es el discriminante universal. Fail-closed: valores no finitos no pasan.

    `h_safe` y `h_nat` deben estar en la MISMA resolución (la nativa): el helper hace el
    resize de ambos a `size`. Se valida explícitamente para que un uso incorrecto falle con
    un mensaje claro en vez de un error de broadcasting.
    """
    if h_safe.shape != h_nat.shape:
        raise ValueError(
            f"target_matches_safe_surface: h_safe {h_safe.shape} y h_nat {h_nat.shape} "
            "deben tener la misma resolución (la nativa)"
        )
    h_ref_safe = resize_height_unclipped(h_safe, size)
    h_ref_nat = resize_height_unclipped(h_nat, size)
    g = _grad_check(h_ref_safe, h_ref_nat)
    offset_native = float(np.mean(h_safe - h_nat))
    max_abs_offset_error = float(np.max(np.abs((h_ref_safe - h_ref_nat) - offset_native)))
    finite = bool(
        math.isfinite(g["max_abs_dp"])
        and math.isfinite(g["max_abs_dq"])
        and math.isfinite(max_abs_offset_error)
    )
    ok = bool(
        finite
        and g["max_abs_dp"] <= gradient_tol
        and g["max_abs_dq"] <= gradient_tol
        and max_abs_offset_error <= offset_tol
    )
    return {
        "ok": ok,
        "max_abs_dp": g["max_abs_dp"],
        "max_abs_dq": g["max_abs_dq"],
        "offset_native": offset_native,
        "max_abs_offset_error": max_abs_offset_error,
    }


def main() -> None:  # noqa: C901
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    cases = [
        "S02_sine_x",
        "S06_multifreq",
        "S07_bumps",
        "S08_ridges",
        "S09_bricks",
        "S11_highfreq",
        "S15_periodic_noise",
    ]
    out: dict[str, Any] = {}
    invariants: dict[str, Any] = {}

    for name in cases:
        for amp in AMPLITUDES:
            h_nat = PERIODIC_CASES[name](NATIVE, NATIVE)
            h_nat = (h_nat - h_nat.mean()) * amp

            # ---- F5: superficie segura (offset), invariantes verificados
            try:
                h_safe, offset = safe_surface(h_nat)
                range_ok = bool(h_safe.min() >= -RANGE_TOL and h_safe.max() <= 1.0 + RANGE_TOL)
                g = _grad_check(h_safe, h_nat)
                grad_ok = bool(g["max_abs_dp"] <= GRADIENT_TOL and g["max_abs_dq"] <= GRADIENT_TOL)
                invariants[f"{name}@{amp}"] = {
                    "offset": float(offset),
                    "h_safe_min": float(h_safe.min()),
                    "h_safe_max": float(h_safe.max()),
                    "range_invariant_ok": range_ok,
                    "max_abs_dp": g["max_abs_dp"],
                    "max_abs_dq": g["max_abs_dq"],
                    "gradient_invariant_ok": grad_ok,
                }
                valid = range_ok and grad_ok
            except ValueError as exc:
                h_safe, offset, valid = None, float("nan"), False
                invariants[f"{name}@{amp}"] = {"error": str(exc), "valid": False}

            # ---- brazo VIEJO (clipeado): reproduce el defecto tal como se reportó
            h_ref_clip = resize_height(h_nat, TARGET)
            n_nat = self_forward(h_nat, bits=None)
            n_old = resize_normal(n_nat, TARGET)
            n_new = resize_normal_float_counterfactual(n_nat, TARGET)
            n_ideal_clip = self_forward(h_ref_clip, bits=None)
            m_old_clip = evaluate_path(h_ref_clip, n_old, path="old")
            m_new_clip = evaluate_path(h_ref_clip, n_new, path="new")

            # ---- brazo NUEVO (superficie segura, sin clip)
            tgt: dict[str, Any] | None = None
            if valid:
                h_ref_safe = resize_height_unclipped(h_safe, TARGET)
                # El offset constante sobrevive al resize bilineal (es lineal) y no altera
                # gradientes; no hace falta restarlo para el chequeo de invariantes, que se
                # hace en la MISMA resolución (ver `target_matches_safe_surface`).
                n_ideal_safe = self_forward(h_ref_safe, bits=None)
                m_old_safe = evaluate_path(h_ref_safe, n_old, path="old")
                m_new_safe = evaluate_path(h_ref_safe, n_new, path="new")
                # Invariante F5: el target de normales derivado de `resize(h_safe)` debe
                # describir la MISMA superficie que `h_nat`. La comparación es contra
                # `resize(h_nat)` — nunca contra una segunda llamada idéntica, que es lo que
                # volvía tautológico al chequeo previo (hallazgo del Oracle, corregido acá).
                tgt = target_matches_safe_surface(
                    h_safe,
                    h_nat,
                    TARGET,
                    gradient_tol=TARGET_GRADIENT_TOL,
                    offset_tol=TARGET_OFFSET_TOL,
                )
                target_ok = bool(tgt["ok"])
            else:
                h_ref_safe = n_ideal_safe = None
                m_old_safe = m_new_safe = None
                target_ok = False

            key = f"{name}@{amp}"
            entry: dict[str, Any] = {
                "amplitude": amp,
                "obligatoria": amp in OBLIGATORIAS,
                "valid_surface": valid,
                # --- viejo (clipeado)
                "clipped": {
                    "normal_rmse_old_vs_new": float(np.sqrt(np.mean((n_old - n_new) ** 2))),
                    "normal_rmse_old_vs_ideal": float(np.sqrt(np.mean((n_old - n_ideal_clip) ** 2))),
                    "normal_rmse_new_vs_ideal": float(np.sqrt(np.mean((n_new - n_ideal_clip) ** 2))),
                    "downstream_aligned_old": float(m_old_clip["path_rmse"]),
                    "downstream_aligned_new": float(m_new_clip["path_rmse"]),
                    "downstream_aligned_ratio": (
                        float(m_old_clip["path_rmse"] / m_new_clip["path_rmse"])
                        if m_new_clip["path_rmse"] > 0
                        else None
                    ),
                    "downstream_delta": float(m_old_clip["path_rmse"] - m_new_clip["path_rmse"]),
                },
            }
            if valid and m_old_safe is not None and m_new_safe is not None:
                entry["safe_surface"] = {
                    "normal_rmse_old_vs_new": float(np.sqrt(np.mean((n_old - n_new) ** 2))),
                    "normal_rmse_old_vs_ideal": float(np.sqrt(np.mean((n_old - n_ideal_safe) ** 2))),
                    "normal_rmse_new_vs_ideal": float(np.sqrt(np.mean((n_new - n_ideal_safe) ** 2))),
                    "downstream_aligned_old": float(m_old_safe["path_rmse"]),
                    "downstream_aligned_new": float(m_new_safe["path_rmse"]),
                    "downstream_aligned_ratio": (
                        float(m_old_safe["path_rmse"] / m_new_safe["path_rmse"])
                        if m_new_safe["path_rmse"] > 0
                        else None
                    ),
                    "downstream_delta": float(m_old_safe["path_rmse"] - m_new_safe["path_rmse"]),
                    "target_corresponds_to_h_safe": target_ok,
                    # Diagnóstico del invariante F5 en la resolución target (auditable).
                    "target_grad_max_abs_dp": (tgt or {}).get("max_abs_dp"),
                    "target_grad_max_abs_dq": (tgt or {}).get("max_abs_dq"),
                    "target_offset_error_after_resize": (tgt or {}).get("max_abs_offset_error"),
                }
            out[key] = entry

    # ---- resúmenes por brazo
    def _summary(arm: str) -> dict[str, Any]:
        vals = [v[arm] for v in out.values() if arm in v]
        if not vals:
            return {"n": 0}
        deltas = [abs(v["downstream_delta"]) for v in vals]
        ratios = [v["downstream_aligned_ratio"] for v in vals if v["downstream_aligned_ratio"]]
        return {
            "n": len(vals),
            "max_normal_rmse_old_vs_new": float(max(v["normal_rmse_old_vs_new"] for v in vals)),
            "max_abs_downstream_delta": float(max(deltas)),
            "max_downstream_aligned_ratio": float(max(ratios)) if ratios else None,
            "n_cases_downstream_delta_ge_t": int(sum(1 for d in deltas if d >= 0.02)),
        }

    # ---- valores específicos del claim externo (c=0.05, c=0.01) en S07/S15
    external: dict[str, Any] = {}
    for name in ("S07_bumps", "S15_periodic_noise"):
        for c in (0.05, 0.01):
            k = f"{name}@{c}"
            if k not in out:
                continue
            e = out[k]
            external[k] = {
                "amplitude": c,
                "obligatoria": True,
                "clipped": {
                    "ratio": e["clipped"]["downstream_aligned_ratio"],
                    "delta": e["clipped"]["downstream_delta"],
                    "normal_rmse_old_vs_new": e["clipped"]["normal_rmse_old_vs_new"],
                },
                "safe_surface": (
                    {
                        "ratio": e["safe_surface"]["downstream_aligned_ratio"],
                        "delta": e["safe_surface"]["downstream_delta"],
                        "normal_rmse_old_vs_new": e["safe_surface"]["normal_rmse_old_vs_new"],
                    }
                    if "safe_surface" in e
                    else None
                ),
            }

    payload = {
        "probe": "H4_MAGNITUDE_CORRECTIVE",
        "audit_only": True,
        "corrective": True,
        "correction_parent_head": "d260b7c9b8afff33a6720106ac8c5f6d95891ecd",
        "native": NATIVE,
        "target": TARGET,
        "t_delta_rmse": 0.02,
        "amplitudes_required": AMPLITUDES_REQUIRED,
        "amplitudes_continuity": AMPLITUDES_CONTINUITY,
        "amplitudes_obligatorias": OBLIGATORIAS,
        "invariants": invariants,
        "cases": out,
        "external_claim_probe_c_0_05_0_01": external,
        "summary": {
            "clipped_old_arm": _summary("clipped"),
            "safe_surface_new_arm": _summary("safe_surface"),
            "n_invariants_ok": int(
                sum(1 for v in invariants.values() if v.get("range_invariant_ok") and v.get("gradient_invariant_ok"))
            ),
            "n_invariants_total": len(invariants),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, allow_nan=False)
    print(f"OK -> {args.out}")
    print(json.dumps(payload["summary"], indent=1))
    print("claim externo (c=0.05, c=0.01):")
    for k, v in external.items():
        print(
            f"  {k:26s} clipped ratio={v['clipped']['ratio']}  safe ratio="
            f"{v['safe_surface']['ratio'] if v['safe_surface'] else None}"
        )


if __name__ == "__main__":
    main()

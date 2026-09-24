"""Runner del experimento NP-M0 (E0–E6). Research-only; produce la tabla de §32.

Uso:
    python -m sky_claw.local.native_parallax.research.run_np_m0 [--resolution 512] \
        [--assets-dir DIR] [--groups E0,E1,E2,E3,E4,E5,E6]

Requiere numpy (deliberadamente NO agregado a pyproject en este spike; justificación en
np-m0-results.md §Environment). Escribe la tabla markdown a stdout y, si se pide,
PNGs de diagnóstico (escritor stdlib, sin Pillow, §31).
"""

from __future__ import annotations

import argparse
import platform
import struct
import time
import tracemalloc
import zlib
from collections.abc import Callable
from pathlib import Path

import numpy as np

from sky_claw.local.native_parallax.research import metrics
from sky_claw.local.native_parallax.research.alignment import best_affine
from sky_claw.local.native_parallax.research.normal_fft_periodic import integrate_periodic
from sky_claw.local.native_parallax.research.normal_from_height import (
    gradients_from_normal,
    normals_from_gradients,
    quantize_decode,
    spectral_gradients,
)
from sky_claw.local.native_parallax.research.synthetic_height import (
    NEGATIVE_CONTROLS,
    PERIODIC_CASES,
    boundary_wrap_ratio,
)

# Subconjunto representativo para grupos de estrés E1–E3 (duración acotada).
STRESS_CASES = (
    "S06_multifreq",
    "S08_ridges",
    "S09_bricks",
    "S10_asymmetric",
    "S12_mixed_scale",
    "S14_steep",
)
NOISE_SIGMAS = (1 / 255, 2 / 255, 4 / 255, 8 / 255)
QUANT_BITS = (8, 10, 16)
STEEP_AMPS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
NZ_FLOORS = (1e-2, 1e-3, 1e-6)


def _stable_seed(case: str, tag: str) -> int:
    """Seed determinista por (caso, perturbación).

    NOTA: ``hash()`` de str es randomizado por proceso (PYTHONHASHSEED) y rompería la
    reproducibilidad (§29); se usa CRC32 que es estable.
    """
    return zlib.crc32(f"{case}|{tag}".encode()) & 0x7FFFFFFF


def _case_pipeline(
    h: np.ndarray, sx: float = 1.0, sy: float = 1.0, nz_floor: float = 1e-6
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict[str, float]]:
    """H → (p,q) → N → [camino de reconstrucción solo ve N] → H'."""
    p, q = spectral_gradients(h)
    n = normals_from_gradients(p, q, sx=sx, sy=sy)
    p2, q2, _hits = gradients_from_normal(n, sx=sx, sy=sy, nz_floor=nz_floor)
    rec, info = integrate_periodic(p2, q2)
    return h, p, q, n, rec, info


def _run_case(
    name: str,
    h: np.ndarray,
    *,
    sx: float = 1.0,
    sy: float = 1.0,
    nz_floor: float = 1e-6,
    quantize_bits: int | None = None,
    noise_sigma: float | None = None,
    grad_flip: str = "none",
) -> dict[str, float | str | int]:
    """Pipeline completo con perturbaciones EN LA NORMAL + métricas (fila de §32)."""
    t0 = time.perf_counter()
    p, q = spectral_gradients(h)
    n = normals_from_gradients(p, q, sx=sx, sy=sy)

    if quantize_bits is not None:
        n = quantize_decode(n, bits=quantize_bits)
    if noise_sigma is not None:
        rng = np.random.default_rng(_stable_seed(name, f"noise{noise_sigma:.6f}"))
        n = n + rng.normal(0.0, noise_sigma, size=n.shape)
        n = n / np.linalg.norm(n, axis=-1, keepdims=True)

    dx = -1.0 if grad_flip in ("flip_x", "flip_xy") else 1.0
    dy = -1.0 if grad_flip in ("flip_y", "flip_xy") else 1.0
    p2, q2, hits = gradients_from_normal(n, sx=dx, sy=dy, nz_floor=nz_floor)
    rec, info = integrate_periodic(p2, q2)
    runtime_ms = (time.perf_counter() - t0) * 1000.0

    row: dict[str, float | str | int] = {
        "case": name,
        "resolution": f"{h.shape[0]}x{h.shape[1]}",
        "noise": f"{noise_sigma * 255:.0f}/255" if noise_sigma else "0",
        "quantization": f"Q{quantize_bits}" if quantize_bits else "-",
    }
    # Para flips medimos contra la CONVENCIÓN original (sx=sy=1): el flip debe degradar.
    for key, val in metrics.compute_all(h, rec, p, q, n, sx=1.0, sy=1.0).items():
        row[key] = val
    row["nz_min"] = float(n[..., 2].min())
    row["nz_floor_hits"] = hits
    row["max_imag_residual"] = info["max_imag_residual"]
    row["runtime_ms"] = runtime_ms
    return row


COLUMNS = (
    "case",
    "resolution",
    "noise",
    "quantization",
    "rmse",
    "gradient_rmse",
    "raw_centered_rmse",
    "normal_angle_mean",
    "normal_angle_p95",
    "corr",
    "seam_height",
    "seam_gradient",
    "low_band_error",
    "mid_band_error",
    "high_band_error",
    "ssim",
    "best_sign",
    "scale_a",
    "offset_b",
    "nz_min",
    "nz_floor_hits",
    "runtime_ms",
)


def format_row(row: dict[str, float | str | int]) -> str:
    def fmt(k: str) -> str:
        v = row.get(k, "")
        if isinstance(v, float):
            if k == "best_sign":
                return f"{int(v):+d}"
            if v == 0.0:
                return "0"
            return f"{v:.6g}"
        return str(v)

    return "| " + " | ".join(fmt(c) for c in COLUMNS) + " |"


def markdown_table(rows: list[dict[str, float | str | int]]) -> str:
    head = "| " + " | ".join(COLUMNS) + " |"
    sep = "|" + "|".join("---:" for _ in COLUMNS) + "|"
    return "\n".join([head, sep, *(format_row(r) for r in rows)])


def write_png_grayscale(path: Path, img01: np.ndarray) -> None:
    """PNG 8-bit escala de grises con stdlib (sin Pillow) para diagnóstico §31."""
    data = (np.clip(img01, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    h, w = data.shape
    raw = b"".join(b"\x00" + data[i].tobytes() for i in range(h))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    )


def _norm01(a: np.ndarray) -> np.ndarray:
    span = float(a.max() - a.min())
    return (a - a.min()) / span if span > 1e-300 else a * 0.0


def run_e0(res: int) -> list[dict[str, float | str | int]]:
    """E0 — PERFECT PERIODIC: datos exactos, sin ruido ni cuantización (§19)."""
    return [_run_case(name, fn(res, res)) for name, fn in PERIODIC_CASES.items()]


def run_e0_controls(res: int) -> list[dict[str, float | str | int]]:
    """E5 — NON-PERIODIC NEGATIVE CONTROLS (§24): el solver debe fallar claramente."""
    return [_run_case(name, fn(res, res)) for name, fn in NEGATIVE_CONTROLS.items()]


def run_e1(res: int) -> list[dict[str, float | str | int]]:
    """E1 — QUANTIZED: normal 8/10/16-bit (§20). Sin DDS, solo cuantización."""
    rows = []
    for name in STRESS_CASES:
        h = PERIODIC_CASES[name](res, res)
        for bits in QUANT_BITS:
            rows.append(_run_case(name, h, quantize_bits=bits))
    return rows


def run_e2(res: int) -> list[dict[str, float | str | int]]:
    """E2 — NOISE: ruido gaussiano en la normal, renormalizada (§21). Seeds estables."""
    rows = []
    for name in STRESS_CASES:
        h = PERIODIC_CASES[name](res, res)
        for sigma in NOISE_SIGMAS:
            rows.append(_run_case(name, h, noise_sigma=sigma))
    return rows


def run_e3(res: int) -> list[dict[str, float | str | int]]:
    """E3 — WRONG SIGN: flip X / Y / XY en la decodificación (§22)."""
    rows = []
    for name in STRESS_CASES:
        h = PERIODIC_CASES[name](res, res)
        for flip in ("flip_x", "flip_y", "flip_xy"):
            rows.append(_run_case(name, h, grad_flip=flip))
    return rows


def _steep_surface(res: int, amp: float) -> np.ndarray:
    y = np.arange(res) / res
    x = np.arange(res) / res
    xx, yy = np.meshgrid(x, y, indexing="xy")
    return amp * (np.sin(2.0 * np.pi * 6.0 * xx) + 0.8 * np.cos(2.0 * np.pi * 5.0 * yy))


def run_e4(res: int) -> list[dict[str, float | str | int]]:
    """E4 — STEEP SLOPES: barrido de amplitud perfecto/con ruido y sensibilidad a nz_floor (§23)."""
    rows = []
    for amp in STEEP_AMPS:
        h = _steep_surface(res, amp)
        rows.append(_run_case(f"E4_A{amp:g}", h))
        rows.append(_run_case(f"E4_A{amp:g}_noise", h, noise_sigma=1 / 255))
    for amp in (16.0, 32.0):
        h = _steep_surface(res, amp)
        for floor in NZ_FLOORS:
            rows.append(_run_case(f"E4_A{amp:g}_floor{floor:g}", h, nz_floor=floor, noise_sigma=1 / 255))
    return rows


def run_e6(_res: int) -> list[dict[str, float | str | int]]:
    """E6 — RESOLUTION/PERFORMANCE: 256²→2048², solve (mediana de 3) y pipeline (§30)."""
    rows: list[dict[str, float | str | int]] = []
    h_full = PERIODIC_CASES["S06_multifreq"](2048, 2048)
    for size in (256, 512, 1024, 2048):
        h = h_full[:size, :size].copy()
        p, q = spectral_gradients(h)
        solves = []
        for _ in range(3):
            t0 = time.perf_counter()
            integrate_periodic(p, q)
            solves.append((time.perf_counter() - t0) * 1000.0)
        t0 = time.perf_counter()
        _run_case("S06_multifreq", h)
        pipe_ms = (time.perf_counter() - t0) * 1000.0
        base = {"resolution": f"{size}x{size}", "noise": "-", "quantization": "-"}
        rows.append({**base, "case": f"E6_solve_{size}x{size}", "runtime_ms": sorted(solves)[1]})
        rows.append({**base, "case": f"E6_pipeline_{size}x{size}", "runtime_ms": pipe_ms})
    del p, q
    tracemalloc.start()
    integrate_periodic(*spectral_gradients(h_full))
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rows.append(
        {"case": "E6_peak_mem_MB_solve_2048", "resolution": "2048x2048", "runtime_ms": peak / (1024.0 * 1024.0)}
    )
    del h_full
    return rows


def environment_block() -> str:
    cpu = platform.processor() or "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return (
        f"- Python: {platform.python_version()} ({platform.python_implementation()})\n"
        f"- NumPy: {np.__version__}\n"
        f"- SciPy: no usada (métricas implementadas sobre NumPy)\n"
        f"- Plataforma: {platform.platform()}\n"
        f"- CPU: {cpu}\n"
    )


def dataset_block(res: int) -> str:
    """Consistencia de frontera del ground truth por caso (§11 del brief)."""
    lines = ["| case | boundary_wrap_ratio |", "|---|---:|"]
    for name, fn in {**PERIODIC_CASES, **NEGATIVE_CONTROLS}.items():
        lines.append(f"| {name} | {boundary_wrap_ratio(fn(res, res)):.3f} |")
    return "\n".join(lines)


GROUPS: dict[str, Callable[[int], list[dict[str, float | str | int]]]] = {
    "E0": run_e0,
    "E5": run_e0_controls,
    "E1": run_e1,
    "E2": run_e2,
    "E3": run_e3,
    "E4": run_e4,
    "E6": run_e6,
}


def _dump_assets(assets_dir: str, res: int) -> None:
    """PNGs de diagnóstico §31: un caso periódico y un control negativo."""
    assets = Path(assets_dir)
    h = PERIODIC_CASES["S08_ridges"](res, res)
    hh, _p, _q, _n, rec, _info = _case_pipeline(h)
    fit = best_affine(rec, hh)
    err = np.abs(fit["a"] * rec + fit["b"] - hh)
    write_png_grayscale(assets / "S08_gt.png", _norm01(hh))
    write_png_grayscale(assets / "S08_rec.png", _norm01(rec))
    write_png_grayscale(assets / "S08_err.png", _norm01(err))
    h2 = NEGATIVE_CONTROLS["N02_bump_boundary"](res, res)
    _, _p2, _q2, _n2, rec2, _i2 = _case_pipeline(h2)
    write_png_grayscale(assets / "N02_gt.png", _norm01(h2))
    write_png_grayscale(assets / "N02_rec.png", _norm01(rec2))


def main() -> None:
    parser = argparse.ArgumentParser(description="NP-M0: spike numérico adversarial")
    parser.add_argument("--resolution", type=int, default=512)
    parser.add_argument("--groups", type=str, default="E0,E1,E2,E3,E4,E5,E6")
    parser.add_argument("--assets-dir", type=str, default="")
    args = parser.parse_args()

    print("## Environment\n")
    print(environment_block())
    print("## Boundary consistency del dataset (ground truth)\n")
    print(dataset_block(min(args.resolution, 256)))

    for group in args.groups.split(","):
        group = group.strip()
        if group not in GROUPS:
            continue
        print(f"\n### Grupo {group}\n")
        print(markdown_table(GROUPS[group](args.resolution)))

    if args.assets_dir:
        _dump_assets(args.assets_dir, args.resolution)


if __name__ == "__main__":
    main()

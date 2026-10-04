"""EXP-M5 — localización espectral del mismatch authored normal<->height (research-only).

Modulo PURO (solo numpy, sin imports de la app) para que la bateria sintetica y las
invariantes duras (particion de mascaras, Parseval, simetria Hermitiana, colocacion de
una sola frecuencia, invarianza 512<->1024) sean verificables SIN corpus ni GUI.

La orquestacion con corpus real vive en ``run_exp_m5.py``: REUSA la matematica M4
(``solve_normal``/``self_forward``/``OracleOnly.fit_global_scale``) y solo delega aqui la
descomposicion espectral. Este modulo NUNCA fitea por banda ni filtra el normal antes del
solver — eso esta prohibido por el preregistro (exp-m5-frequency-band-coherence.md §8).

Contrato espectral (§9–§17 del preregistro):

- Eje de frecuencia normalizado por textura: ``rho = sqrt(kx^2+ky^2)`` con
  ``kx = fftfreq(W)*W``, ``ky = fftfreq(H)*H`` => ``rho`` = ciclos por tile/textura,
  independiente de la resolucion (512^2 y 1024^2 comparten escala).
- Bandas diagnosticas octave-like disjuntas B1..B7 y agregados primarios
  ``LOWMID = 0<rho<32`` / ``HIGH = rho>=32``. ``DC`` (rho=0) excluido de la decision.
- Mascaras duras: disjuntas, exhaustivas salvo DC, conjugado-simietricas (radiales => pares
  Hermitian preservados => IFFT real dentro de tolerancia).
- Metrica primaria por banda: ``NRMSE_b = sqrt(residual_energy_b / target_energy_b)`` con
  ``EXCESS_NRMSE_b = NRMSE_AUTH_b - NRMSE_SELF_b`` (resta el piso del solver/quantizacion).
- Enriquecimiento HIGH: ``HIGH_ENRICHMENT = HIGH_RESIDUAL_SHARE / HIGH_TARGET_SHARE``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

# ----------------------------------------------------------------------------- bandas
# Bordes octave-like preregistrados (§11). Convencion de borde: [lo, hi) — el borde
# inferior pertenece a la banda, el superior a la siguiente. DC (rho=0) fuera de todas.
BAND_EDGES: tuple[int, ...] = (4, 8, 16, 32, 64, 128)
BAND_NAMES: tuple[str, ...] = ("B1", "B2", "B3", "B4", "B5", "B6", "B7")

# Corte primario LOWMID/HIGH preregistrado (§10): 32 ciclos/tile. Congelado ANTES de M5 real.
LOWMID_HIGH_CUTOFF: int = 32

# ENERGY_GATE (§15): una banda cuyo target aporta < esta fraccion de la energia no-DC se
# marca LOW_ENERGY y su NRMSE NO entra a las medianas (no excluye el asset). Derivado de
# precision float64 + error de round-trip FFT (~1e-12) con margen 1e6x => 1e-6.
ENERGY_GATE_FRACTION: float = 1e-6


def rho_grid(height: int, width: int) -> np.ndarray:
    """``rho`` = ciclos por tile/textura, shape (height, width), independiente de resolucion.

    ``kx = fftfreq(W)*W`` y ``ky = fftfreq(H)*H`` dan enteros de ciclos por tile; ``rho`` es
    par bajo (i,j)->(-i,-j), garantizando mascaras conjugado-simetricas (§12/§13).
    """
    if height <= 0 or width <= 0:
        raise ValueError(f"rho_grid: dimensiones invalidas ({height},{width})")
    kx = np.fft.fftfreq(width) * width
    ky = np.fft.fftfreq(height) * height
    kxx, kyy = np.meshgrid(kx, ky)  # (height, width): kxx varia en axis=1, kyy en axis=0
    rho: np.ndarray = np.sqrt(kxx * kxx + kyy * kyy)
    return rho


def _finite(name: str, arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float64)
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{name}: contiene NaN/Inf (fail-fast M5)")
    return a


def band_masks(height: int, width: int) -> dict[str, np.ndarray]:
    """Mascaras booleanas disjuntas/exhaustivas-salvo-DC (§12/§13).

    Devuelve ``DC``, ``NONDC``, ``B1..B7``, ``LOWMID`` (=B1|B2|B3|B4), ``HIGH`` (=B5|B6|B7).
    Cada coeficiente no-DC pertenece a EXACTAMENTE una de B1..B7.
    """
    rho = rho_grid(height, width)
    dc = rho == 0.0
    non_dc = ~dc
    edges = BAND_EDGES
    masks: dict[str, np.ndarray] = {"DC": dc, "NONDC": non_dc}
    lo = 0.0
    for i, hi in enumerate((*edges, math.inf)):
        name = f"B{i + 1}"
        masks[name] = non_dc & (rho >= lo) & (rho < hi)
        lo = float(hi)
    masks["LOWMID"] = masks["B1"] | masks["B2"] | masks["B3"] | masks["B4"]
    masks["HIGH"] = masks["B5"] | masks["B6"] | masks["B7"]
    return masks


def band_energy(spectrum: np.ndarray, mask: np.ndarray) -> float:
    """Energia de una banda = suma de |coeficientes_en_la_banda|^2 (atribucion exacta)."""
    return float(np.sum(np.abs(spectrum[mask]) ** 2))


def _spectrum(field: np.ndarray) -> np.ndarray:
    return np.fft.fft2(np.asarray(field, dtype=np.float64))


def reconstruct_bands(field: np.ndarray, masks: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Reconstruccion espacial por banda: ifft2(FFT(field)*mask). DC incluido como ``DC``."""
    spec = _spectrum(field)
    return {name: np.fft.ifft2(spec * mask).real for name, mask in masks.items()}


def _coherence(target_spec: np.ndarray, recon_spec: np.ndarray, mask: np.ndarray) -> float:
    """|suma H_b*conj(R_b)| / sqrt(suma|H_b|^2 * suma|R_b|^2) — similitud compleja en b."""
    num = abs(np.sum(target_spec[mask] * np.conj(recon_spec[mask])))
    den = math.sqrt(band_energy(target_spec, mask) * band_energy(recon_spec, mask))
    return float(num / den) if den > 0.0 else float("nan")


def analyze_path(
    target: np.ndarray,
    recon_aligned: np.ndarray,
    masks: dict[str, np.ndarray],
    *,
    energy_gate: float = ENERGY_GATE_FRACTION,
) -> dict[str, dict[str, float]]:
    """Metricas por banda para UN camino (target vs reconstruccion YA alineada globalmente).

    NO fitea nada: la reconstruccion debe venir con la escala global M4 aplicada (§8).
    """
    t = _finite("analyze_path.target", target)
    r = _finite("analyze_path.recon_aligned", recon_aligned)
    if t.shape != r.shape:
        raise ValueError(f"analyze_path: shapes distintas target{t.shape} vs recon{r.shape}")
    tspec = _spectrum(t)
    rspec = _spectrum(r)
    dspec = _spectrum(r - t)
    non_dc_target_energy = band_energy(tspec, masks["NONDC"])
    out: dict[str, dict[str, float]] = {}
    for name in ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "LOWMID", "HIGH"):
        mask = masks[name]
        te = band_energy(tspec, mask)
        ee = band_energy(dspec, mask)
        frac = te / non_dc_target_energy if non_dc_target_energy > 0 else float("nan")
        eligible = bool(non_dc_target_energy > 0 and frac >= energy_gate)
        out[name] = {
            "target_energy": te,
            "recon_energy": band_energy(rspec, mask),
            "residual_energy": ee,
            "target_energy_fraction": frac,
            "nrmse": math.sqrt(ee / te) if te > 0 else float("nan"),
            "coherence": _coherence(tspec, rspec, mask),
            "eligible": 1.0 if eligible else 0.0,
        }
    return out


def excess_nrmse(auth: dict[str, dict[str, float]], self_: dict[str, dict[str, float]], band: str) -> float:
    """EXCESS_NRMSE_b = NRMSE_AUTH_b - NRMSE_SELF_b (resta el piso solver/quantizacion)."""
    a = auth[band]["nrmse"]
    s = self_[band]["nrmse"]
    if math.isnan(a) or math.isnan(s):
        return float("nan")
    return a - s


def high_enrichment(
    target: np.ndarray,
    recon_self_aligned: np.ndarray,
    recon_auth_aligned: np.ndarray,
    masks: dict[str, np.ndarray],
    *,
    energy_gate: float = ENERGY_GATE_FRACTION,
) -> float:
    """HIGH_ENRICHMENT (§17) = HIGH_RESIDUAL_SHARE / HIGH_TARGET_SHARE.

    RESIDUAL usa energia de EXCESO positivo (AUTH-SELF, flooring en 0) para no atribuir
    al par el piso comun del solver. Shares sobre energia no-DC. NaN si no interpretable.
    """
    t = _finite("high_enrichment.target", target)
    tspec = _spectrum(t)
    ss = _spectrum(_finite("high_enrichment.self", recon_self_aligned) - t)
    aa = _spectrum(_finite("high_enrichment.auth", recon_auth_aligned) - t)
    t_high = band_energy(tspec, masks["HIGH"])
    t_low = band_energy(tspec, masks["LOWMID"])
    t_non_dc = t_high + t_low
    if t_non_dc <= 0:
        return float("nan")
    high_target_share = t_high / t_non_dc
    if high_target_share < energy_gate:
        return float("nan")
    exc_high = max(0.0, band_energy(aa, masks["HIGH"]) - band_energy(ss, masks["HIGH"]))
    exc_low = max(0.0, band_energy(aa, masks["LOWMID"]) - band_energy(ss, masks["LOWMID"]))
    exc_total = exc_high + exc_low
    if exc_total <= 0:
        return float("nan")
    return float((exc_high / exc_total) / high_target_share)


# ------------------------------------------------------------------------- decision M5
# Umbrales C1/C2 PREREGISTRADOS (ETAPA B). Derivados de controles sinteticos + piso SELF
# publicados en M4 + matematica; NUNCA de Cohort A M5. Congelados en el commit de prereg.
T_LOWMID_NRMSE = 0.15  # C1: NRMSE_AUTH LOWMID (mediana) <= 0.15
T_LOWMID_EXCESS = 0.10  # C1: EXCESS_NRMSE LOWMID (mediana) <= 0.10 (pair sobre piso SELF)
T_HIGH_ENRICHMENT = 2.0  # C2: HIGH_ENRICHMENT (mediana) >= 2.0

# Seed de bootstrap asset-level preregistrada (§14). CI de medianas; NUNCA se exige
# CI_low > threshold salvo que eso hubiera sido preregistrado (no lo fue).
M5_BOOTSTRAP_SEED = 20260925
M5_BOOTSTRAP_N = 2000

# Snapshot congelado de umbrales/corte/gate para el bloque de reproducibilidad (§16).
M5_THRESHOLDS: dict[str, Any] = {
    "T_LOWMID_NRMSE": T_LOWMID_NRMSE,
    "T_LOWMID_EXCESS": T_LOWMID_EXCESS,
    "T_HIGH_ENRICHMENT": T_HIGH_ENRICHMENT,
    "LOWMID_HIGH_CUTOFF": LOWMID_HIGH_CUTOFF,
    "ENERGY_GATE_FRACTION": ENERGY_GATE_FRACTION,
    "band_edges": list(BAND_EDGES),
}

EXP_BANDLIMITED_SUPPORTED = "EXP_M5_BANDLIMITED_RECOVERY_SUPPORTED"
EXP_BANDLIMITED_NOT_SUPPORTED = "EXP_M5_BANDLIMITED_RECOVERY_NOT_SUPPORTED"
EXP_MIXED = "EXP_M5_MIXED"
EXP_DATA_INSUFFICIENT = "EXP_M5_DATA_INSUFFICIENT"
EXP_INVALIDATED_BY_UPSTREAM_BUG = "EXP_M5_INVALIDATED_BY_UPSTREAM_BUG"


def _median(values: list[float]) -> float:
    finite = [v for v in values if not math.isnan(v)]
    return float(np.median(finite)) if finite else float("nan")


def _finite_number(value: Any) -> float | None:
    """Valor como float finito, o None si no es un numero finito (NaN/Inf/None/no numerico)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _le_threshold(value: Any, threshold: float) -> bool:
    f = _finite_number(value)
    return f is not None and f <= threshold


def _ge_threshold(value: Any, threshold: float) -> bool:
    f = _finite_number(value)
    return f is not None and f >= threshold


def _gt(left: Any, right: Any) -> bool:
    a, b = _finite_number(left), _finite_number(right)
    return a is not None and b is not None and a > b


def evaluate_rules(cohort: dict[str, Any], legacy_heldout: dict[str, Any] | None) -> dict[str, Any]:
    """C1_LOWMID_PRESERVED y C2_HIGH_ENRICHED (§21) sobre medianas ya calculadas.

    Fail-closed en dos frentes: un valor no finito (NaN/None) nunca satisface una comparacion,
    y una cohorte LEGACY_HELDOUT ausente hace ``legacy_heldout_replication=false``. C2 exige
    replica direccional explicita: ``None`` NO es una replica exitosa.
    """
    c1 = _le_threshold(cohort.get("auth_lowmid_nrmse"), T_LOWMID_NRMSE) and _le_threshold(
        cohort.get("excess_lowmid_nrmse"), T_LOWMID_EXCESS
    )
    contrast = _gt(cohort.get("excess_high_nrmse"), cohort.get("excess_lowmid_nrmse"))
    enriched = _ge_threshold(cohort.get("high_enrichment"), T_HIGH_ENRICHMENT)
    replication = False
    if legacy_heldout is not None:
        replication = _gt(legacy_heldout.get("excess_high_nrmse"), legacy_heldout.get("excess_lowmid_nrmse"))
    return {
        "C1_lowmid_preserved": c1,
        "C2_high_enriched": bool(contrast and enriched and replication),
        "high_gt_lowmid_excess": contrast,
        "high_enrichment_ge_threshold": enriched,
        "legacy_heldout_replication": replication,
    }


def decide(rules: dict[str, Any]) -> str:
    """Vocabulario final EXACTO (§22): una sola decision a partir de C1/C2."""
    c1 = bool(rules["C1_lowmid_preserved"])
    c2 = bool(rules["C2_high_enriched"])
    if c1 and c2:
        return EXP_BANDLIMITED_SUPPORTED
    if not c1 and not c2:
        return EXP_BANDLIMITED_NOT_SUPPORTED
    return EXP_MIXED


def asset_summary(
    target: np.ndarray,
    recon_self_aligned: np.ndarray,
    recon_auth_aligned: np.ndarray,
    masks: dict[str, np.ndarray],
    *,
    energy_gate: float = ENERGY_GATE_FRACTION,
) -> dict[str, Any]:
    """Record por asset listo para ``cohort_medians``/``evaluate_rules``.

    Corre SELF y AUTH (ambos YA alineados con la escala global M4), calcula EXCESS por
    agregado LOWMID/HIGH, elegibilidad ENERGY_GATE y HIGH_ENRICHMENT. Un agregado es
    elegible sólo si AMBOS caminos lo son (evita promediar un NRMSE de banda vacía).
    """
    self_m = analyze_path(target, recon_self_aligned, masks, energy_gate=energy_gate)
    auth_m = analyze_path(target, recon_auth_aligned, masks, energy_gate=energy_gate)

    def agg(name: str) -> dict[str, float]:
        return {
            "self_nrmse": self_m[name]["nrmse"],
            "auth_nrmse": auth_m[name]["nrmse"],
            "excess": excess_nrmse(auth_m, self_m, name),
            "eligible": min(self_m[name]["eligible"], auth_m[name]["eligible"]),
        }

    lowmid, high = agg("LOWMID"), agg("HIGH")
    return {
        "bands": {"self": self_m, "auth": auth_m},
        "self_lowmid_nrmse": lowmid["self_nrmse"],
        "auth_lowmid_nrmse": lowmid["auth_nrmse"],
        "excess_lowmid_nrmse": lowmid["excess"],
        "lowmid_eligible": lowmid["eligible"],
        "self_high_nrmse": high["self_nrmse"],
        "auth_high_nrmse": high["auth_nrmse"],
        "excess_high_nrmse": high["excess"],
        "high_eligible": high["eligible"],
        "high_enrichment": high_enrichment(
            target, recon_self_aligned, recon_auth_aligned, masks, energy_gate=energy_gate
        ),
    }


def cohort_medians(per_asset: list[dict[str, Any]]) -> dict[str, float]:
    """Medianas de cohorte sobre assets ELEGIBLES por agregado.

    ``per_asset``: dicts con ``auth_lowmid_nrmse``/``self_lowmid_nrmse`` (flag
    ``lowmid_eligible``), ``auth_high_nrmse``/``self_high_nrmse`` (flag ``high_eligible``)
    y ``high_enrichment``. Una metrica solo promedia los assets elegibles de ese agregado.

    El EXCESS se agrega como ``median(NRMSE_AUTH_b - NRMSE_SELF_b)`` sobre el campo apareado
    por asset (``excess_lowmid_nrmse``/``excess_high_nrmse``), que es lo que exige el prereg
    (§10/§14). NO como ``median(AUTH) - median(SELF)``: esas dos cantidades no son
    equivalentes en general (con assets heterogeneos pueden caer a distinto lado de un
    threshold o invertir la comparacion HIGH vs LOWMID). Las medianas marginales de AUTH y
    SELF se conservan por separado, solo para diagnostico.
    """

    def elig(flag: str, val: str) -> float:
        return _median([a[val] for a in per_asset if a.get(flag, 0.0) >= 1.0])

    auth_low = elig("lowmid_eligible", "auth_lowmid_nrmse")
    self_low = elig("lowmid_eligible", "self_lowmid_nrmse")
    excess_low = elig("lowmid_eligible", "excess_lowmid_nrmse")
    auth_high = elig("high_eligible", "auth_high_nrmse")
    self_high = elig("high_eligible", "self_high_nrmse")
    excess_high = elig("high_eligible", "excess_high_nrmse")
    enrich = _median([a["high_enrichment"] for a in per_asset if not math.isnan(a["high_enrichment"])])
    return {
        "auth_lowmid_nrmse": auth_low,
        "self_lowmid_nrmse": self_low,
        "excess_lowmid_nrmse": excess_low,
        "auth_high_nrmse": auth_high,
        "self_high_nrmse": self_high,
        "excess_high_nrmse": excess_high,
        "high_enrichment": enrich,
    }

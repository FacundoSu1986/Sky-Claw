"""Alineamiento afín ``ref ≈ a·rec + b`` — SOLO oracle de evaluación (NP-M0 §16/§35).

El algoritmo productivo jamás verá ground truth: estas funciones existen para eliminar la
ambigüedad conocida offset/escala al comparar H vs H'. Riesgo auditado (§35): un ajuste
afín puede ocultar errores de ESCALA (p.ej. el bug 2π de M1). Por eso las métricas del
reporte siempre acompañan el RMSE alineado con ``raw_centered_rmse``, ``gradient_rmse``
y ``normal_angle`` — cantidades que el fitting afín NO puede arreglar.
"""

from __future__ import annotations

import numpy as np

_VAR_EPS = 1e-300


def affine_fit(rec: np.ndarray, ref: np.ndarray, sign: str = "any") -> tuple[float, float]:
    """Mínimos cuadrados cerrado de ``ref ≈ a·rec + b``.

    ``sign``: ``"any"`` libre; ``"pos"`` fuerza ``a ≥ 0``; ``"neg"`` fuerza ``a ≤ 0``
    (en el borde, ``a = 0`` ⇒ ``b = mean(ref)``, i.e. el baseline sin señal).
    Si ``rec`` es plana (varianza 0) devuelve ``a = 0`` (no hay escala recuperable).
    """
    r = rec.ravel()
    f = ref.ravel()
    r_mean = float(r.mean())
    r_c = r - r_mean
    var = float(np.dot(r_c, r_c))
    if var < _VAR_EPS:
        return 0.0, float(f.mean())
    a = float(np.dot(r_c, f - f.mean())) / var
    if sign == "pos":
        a = max(a, 0.0)
    elif sign == "neg":
        a = min(a, 0.0)
    b = float(f.mean()) - a * r_mean
    return a, b


def best_affine(rec: np.ndarray, ref: np.ndarray) -> dict[str, float]:
    """Evalúa ``any`` y reporta el mejor ajuste con su signo.

    Devuelve ``{"a", "b", "best_sign", "rmse"}`` donde ``best_sign`` es ``+1``/``-1``
    (signo de ``a``). IMPORTANTE (§34-Q10): un flip global X+Y de los gradientes produce
    ``rec ≈ -ref`` EXACTO, y el fitting lo absorbe con ``a ≈ -1`` — la inversión
    sistemática SOLO es visible a través de ``best_sign``, nunca por RMSE alineado.
    """
    a, b = affine_fit(rec, ref)
    residual = ref - (a * rec + b)
    return {
        "a": a,
        "b": b,
        "best_sign": 1.0 if a >= 0.0 else -1.0,
        "rmse": float(np.sqrt(np.mean(residual * residual))),
    }

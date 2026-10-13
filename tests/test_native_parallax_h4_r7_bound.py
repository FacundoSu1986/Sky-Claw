"""H4 — R7: cota determinista del error angular de `resize_normal`.

Modelo de cuantizacion DECLARADO (contrato #717 §6.1 + comentario de produccion):
la entrada decodificada vive en una reticula 8-bit de paso ``q = 2/255``; el error por
canal esta acotado por ``|eps_c| <= q/2``; y el vector perturbado se RENORMALIZA:
``n' = normalize(n + eps)``. El modelo NO exige independencia entre pixeles.

Cadena de propagacion (lo que se demuestra, no se supone):

1. Deflexion por pixel. ``delta = normalize(n+eps) - n`` con ``eps`` en el cubo
   ``[-q/2, q/2]^3``. El maximo se alcanza en un VERTICE del cubo y vale
   ``Delta_max = (q/2) * sqrt(3)`` (con ``eps`` perpendicular a ``n``).
2. Interpolacion. El filtro bilineal de Pillow es una COMBINACION CONVEXA por pixel de
   salida (pesos >= 0 que suman 1, tambien con la ventana recortada del borde), asi que
   ``||dv|| = ||sum_i w_i * delta_i|| <= max_i ||delta_i|| <= Delta_max``.
   Esto vale para CUALQUIER correlacion entre pixeles: no se invoca independencia.
3. Angulo. Para ``||v|| = largo > Delta_max`` y ``||dv|| <= Delta_max``,
   ``delta_theta <= asin(||dv||/largo) <= asin(Delta_max/largo)``.

Consecuencia (cruce conservador): ``delta_theta <= q_ang`` queda garantizado para
``largo >= Delta_max / sin(q_ang) ~= sqrt(3)/2 ~= 0.866``.

La cota es ALCANZABLE, no vacua: la construccion cerrada de ``_campo_extremo`` produce
``delta_theta = atan(Delta_max/largo)`` EXACTO (``asin`` y ``atan`` coinciden a
``O((D/largo)^3)``, ~1e-6 relativo, con ``D/largo <= 0.011``).

Relacion con la reconciliacion (#723). Aquel documento midio el cruce en ``1/2`` con un
adversario COHERENTE de magnitud ``q/2`` en UN SOLO canal (``d[...,1] = q/2``). El modelo
declarado admite el vertice del cubo, de magnitud ``(q/2)*sqrt(3)``: el cruce escala por
``sqrt(3)``, ``0.5 * sqrt(3) = 0.866``. Los dos numeros son correctos para familias
distintas; el de este archivo es el del modelo COMPLETO.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

# Cuanto de la reticula 8-bit (`decode_normal_image`: rgb/255*2-1).
Q8 = 2.0 / 255.0
# Cota por canal del modelo declarado.
T = Q8 / 2.0
# Cuanto angular declarado.
Q_ANG = Q8
# Deflexion maxima por pixel: vertice del cubo, perpendicular a la normal.
DELTA_MAX = T * math.sqrt(3.0)
# Cruce conservador de la cota.
L_CRUCE_ASIN = DELTA_MAX / math.sin(Q_ANG)
# Cruce de la construccion extrema (atan).
L_CRUCE_ATAN = DELTA_MAX / math.tan(Q_ANG)

# Vertice del cubo (direccion de la diagonal) y base del plano perpendicular.
_SIGMA = np.array([1.0, 1.0, 1.0]) / math.sqrt(3.0)
_V_HAT = np.array([1.0, -1.0, 0.0]) / math.sqrt(2.0)
_U_HAT = np.cross(_SIGMA, _V_HAT)
_EPS_VERTICE = T * np.array([1.0, 1.0, 1.0])


def _renorm(x: np.ndarray) -> np.ndarray:
    return np.asarray(x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-300), dtype=np.float64)


def _angulo(a: np.ndarray, b: np.ndarray) -> float:
    return float(math.acos(float(np.clip(np.dot(_renorm(a), _renorm(b)), -1.0, 1.0))))


def _cota_angular(largo: float) -> float:
    return math.asin(min(1.0, DELTA_MAX / largo)) if largo > DELTA_MAX else float("inf")


def _campo_extremo(largo: float) -> tuple[np.ndarray, np.ndarray]:
    """Par de normales unitarias cuyo interpolado (pesos 1/2, 1/2) tiene norma exacta.

    ``n_a = largo*v_hat + sqrt(1-largo^2)*u_hat``, ``n_b = largo*v_hat - sqrt(...)*u_hat``,
    con ``v_hat`` y ``u_hat`` perpendiculares a ``_SIGMA``. El vector interpolado limpio es
    ``v = largo*v_hat`` (norma ``largo``, exacta).
    """
    r = math.sqrt(max(0.0, 1.0 - largo * largo))
    return largo * _V_HAT + r * _U_HAT, largo * _V_HAT - r * _U_HAT


def _delta_theta_extremo(largo: float) -> float:
    """`delta_theta` de la construccion cerrada: eps = vertice del cubo, igual en ambos valores."""
    n_a, n_b = _campo_extremo(largo)
    v_limpio = 0.5 * (n_a + n_b)
    v_pert = 0.5 * (_renorm(n_a + _EPS_VERTICE) + _renorm(n_b + _EPS_VERTICE))
    return _angulo(v_limpio, v_pert)


# ---------------------------------------------------------------------------
# 1. Deflexion maxima por pixel
# ---------------------------------------------------------------------------
def test_r7_delta_max_por_pixel_es_q_medio_por_raiz_de_tres() -> None:
    """El sup de `||normalize(n+eps)-n||` sobre el cubo vale `(q/2)*sqrt(3)`."""
    corners = np.array([[sx, sy, sz] for sx in (-T, T) for sy in (-T, T) for sz in (-T, T)])
    idx = np.arange(1500)
    phi = math.pi * (3.0 - math.sqrt(5.0))
    z = 1.0 - 2.0 * (idx + 0.5) / 1500
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    dirs = np.stack([r * np.cos(phi * idx), r * np.sin(phi * idx), z], axis=-1)
    dirs = np.concatenate([dirs, _SIGMA[None, :], _V_HAT[None, :]], axis=0)

    mejor = 0.0
    for n in dirs:
        delta = _renorm(n[None, :] + corners) - n[None, :]
        mejor = max(mejor, float(np.linalg.norm(delta, axis=-1).max()))

    assert mejor == pytest.approx(DELTA_MAX, rel=1e-4)
    # no vacua: se acerca al sup por abajo (el sup exacto se alcanza con eps perp a n)
    assert mejor >= DELTA_MAX * 0.9995
    # la estructura del optimo: eps perpendicular a n (no lo escala, lo rota)
    assert abs(float(np.dot(_SIGMA, _EPS_VERTICE)) - DELTA_MAX) < 1e-12


# ---------------------------------------------------------------------------
# 2. Cota analitica y cruce
# ---------------------------------------------------------------------------
def test_r7_la_cota_angular_y_su_cruce_son_raiz_de_tres_medios() -> None:
    """`delta_theta <= asin(Delta_max/largo)` cruza `q_ang` en `~sqrt(3)/2`, no en 1/2."""
    raiz_de_tres_medios = math.sqrt(3) / 2
    assert math.isclose(L_CRUCE_ASIN, raiz_de_tres_medios, abs_tol=2e-3)
    assert math.isclose(L_CRUCE_ATAN, raiz_de_tres_medios, abs_tol=2e-3)
    # el cruce del modelo COMPLETO esta muy por encima de los umbrales vigentes
    assert L_CRUCE_ASIN > 0.5
    assert L_CRUCE_ASIN > 5.0 / 16.0
    # y la cota es decreciente en largo
    assert _cota_angular(0.9) < _cota_angular(0.8) < _cota_angular(0.7)


# ---------------------------------------------------------------------------
# 3. La cota es ALCANZABLE (no vacua): construccion cerrada exacta
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("largo", [0.55, 0.70, 0.80, 0.866, 0.95, 1.0])
def test_r7_construccion_extrema_alcanza_atan_delta_max_sobre_largo(largo: float) -> None:
    """`delta_theta = atan(Delta_max/largo)` EXACTO en la construccion cerrada."""
    dt = _delta_theta_extremo(largo)
    assert math.isclose(dt, math.atan(DELTA_MAX / largo), rel_tol=1e-9)
    assert dt <= _cota_angular(largo) + 1e-12  # respeta la cota
    assert dt >= 0.99 * _cota_angular(largo)  # y la alcanza


def test_r7_la_cota_no_es_vacua_en_el_cruce() -> None:
    """Justo sobre el cruce la cota sigue siendo alcanzable (no es una asercion hueca)."""
    dt = _delta_theta_extremo(L_CRUCE_ASIN)
    assert math.isclose(dt, Q_ANG, rel_tol=5e-3)


# ---------------------------------------------------------------------------
# 4. Controles negativos
# ---------------------------------------------------------------------------
def test_r7_control_negativo_eps_paralelo_a_v_mide_cero() -> None:
    """Si `eps` queda PARALELO al vector interpolado, la deflexion es nula.

    Es el error de construccion que produce un falso verde por el lado opuesto: elegir
    `v_hat = sigma` hace que `eps` solo escale `v` y `delta_theta == 0`. La construccion
    correcta exige `v_hat` en el plano perpendicular a `sigma`.
    """
    largo = 0.8
    r = math.sqrt(1.0 - largo * largo)
    n_a = largo * _SIGMA + r * _V_HAT
    n_b = largo * _SIGMA - r * _V_HAT
    v_limpio = 0.5 * (n_a + n_b)
    v_pert = 0.5 * (_renorm(n_a + _EPS_VERTICE) + _renorm(n_b + _EPS_VERTICE))
    # `acos` cerca de 1 tiene un piso de precision de 2**-26 ~ 1.5e-8; el control debe
    # leerse contra ese piso, no contra cero exacto.
    assert _angulo(v_limpio, v_pert) < 1e-6
    # y la construccion correcta, con el MISMO eps, si mide
    assert _delta_theta_extremo(largo) > 0.9 * _cota_angular(largo)


def test_r7_control_negativo_medir_largo_tras_renormalizar_invierte_el_veredicto() -> None:
    """Medir `largo` sobre la salida YA renormalizada da 1 siempre -> falso veredicto.

    La cantidad que la cota mide es la norma del vector INTERPOLADO antes de renormalizar.
    Si se mide despues, `largo = 1` y `asin(Delta_max/1) < q_ang` para todo caso, con lo que
    la comparacion contra `q_ang` deja de discriminar.
    """
    largo = 0.55
    n_a, n_b = _campo_extremo(largo)
    v_limpio = 0.5 * (n_a + n_b)
    assert math.isclose(float(np.linalg.norm(v_limpio)), largo, rel_tol=1e-12)
    # tras renormalizar, la norma es 1 -> el umbral quedaria mal comparado
    assert math.isclose(float(np.linalg.norm(_renorm(v_limpio))), 1.0, rel_tol=1e-12)
    # con el largo real el caso SUPERA q_ang; con largo=1 lo cumpliria -> el bug invierte el signo
    assert _delta_theta_extremo(largo) > Q_ANG
    assert _cota_angular(1.0) < Q_ANG


# ---------------------------------------------------------------------------
# 5. Implicaciones sobre los umbrales vigentes
# ---------------------------------------------------------------------------
def test_r7_los_umbrales_vigentes_no_garantizan_q_ang_bajo_el_modelo() -> None:
    """`5/16` y `1/2` dejan pasar `delta_theta` mayor que `q_ang`."""
    assert math.isclose(_cota_angular(5.0 / 16.0), 2.7715 * Q_ANG, rel_tol=2e-3)
    assert math.isclose(_cota_angular(0.5), 1.7321 * Q_ANG, rel_tol=2e-3)
    # el cruce del modelo completo escala el de la reconciliacion por sqrt(3)
    assert math.isclose(L_CRUCE_ATAN / 0.5, math.sqrt(3), rel_tol=2e-3)
    # y solo en el cruce derivado la cota vale exactamente q_ang
    assert math.isclose(_cota_angular(L_CRUCE_ASIN), Q_ANG, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# 6. Bordes: el kernel recortado sigue siendo convexo
# ---------------------------------------------------------------------------
def test_r7_el_borde_no_tiene_un_modo_de_cancelacion_peor() -> None:
    """La ventana recortada del borde tiene pesos >= 0 que suman 1 => misma cota."""
    for n_in, n_out in ((64, 32), (64, 16), (128, 16)):
        escala = n_in / n_out
        fs = max(1.0, escala)
        for i in (0, n_out - 1, n_out // 2):
            centro = (i + 0.5) * escala - 0.5
            x0 = max(0, math.floor(centro - fs + 0.5))
            x1 = min(n_in, math.ceil(centro + fs + 0.5))
            xs = np.arange(x0, x1)
            w = np.maximum(0.0, 1.0 - np.abs(xs - centro) / fs)
            assert float(w.min()) >= 0.0
            assert math.isclose(float((w / w.sum()).sum()), 1.0, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# 7. La implementacion real (Pillow modo F) alcanza la cota
# ---------------------------------------------------------------------------
def test_r7_la_implementacion_real_alcanza_la_cota() -> None:
    """El peor caso construido, medido sobre `resize_normal` real, toca la cota."""
    from PIL import Image

    import sky_claw.local.native_parallax.research.authored_dataset as ad

    # Procedencia: el modulo importado debe vivir en ESTE arbol de trabajo, no en el
    # worktree principal que apunta el `.pth` editable (trampa ya sufrida en este repo).
    raiz = Path(__file__).resolve().parents[1]
    assert Path(ad.__file__).resolve().is_relative_to(raiz)

    largo = 0.80
    n_a, n_b = _campo_extremo(largo)
    s = np.where(np.arange(64) % 2 == 0, 1.0, -1.0)
    fila_limpia = np.where(s[:, None] > 0, n_a[None, :], n_b[None, :])
    campo = np.repeat(fila_limpia[None, :, :], 64, axis=0)
    eps = np.repeat(_EPS_VERTICE[None, None, :], 64, axis=0)
    eps = np.repeat(eps, 64, axis=1)
    pert = _renorm(campo + eps)

    def _crudo(n: np.ndarray) -> np.ndarray:
        arr = np.clip(np.asarray(n, dtype=np.float64), -1.0, 1.0)
        ch = [
            np.asarray(
                Image.fromarray(np.ascontiguousarray(arr[..., c], dtype=np.float32)).resize(
                    (32, 32), Image.Resampling.BILINEAR
                ),
                dtype=np.float64,
            )
            for c in range(3)
        ]
        return np.stack(ch, axis=-1)

    limpio = _crudo(campo)[16, 16]
    perturbado = _crudo(pert)[16, 16]
    largo_real = float(np.linalg.norm(limpio))  # ANTES de renormalizar
    dt = _angulo(limpio, perturbado)

    assert largo_real == pytest.approx(largo, abs=0.02)
    assert dt <= _cota_angular(largo_real) + 1e-9
    assert dt >= 0.95 * _cota_angular(largo_real)

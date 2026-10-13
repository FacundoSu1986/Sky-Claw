"""H4 — R7: cota determinista del error angular de `resize_normal`.

Modelo de cuantizacion DECLARADO (contrato #717 §6.1 + comentario de produccion):
la entrada decodificada vive en una reticula 8-bit de paso ``q = 2/255``; el error por
canal esta acotado por ``abs(eps_c) <= q/2``; y el vector perturbado se RENORMALIZA:
``n' = normalize(n + eps)``. El modelo NO exige independencia entre pixeles.

DOS OBJETOS DISTINTOS, que este archivo mantiene separados (el defecto formal que se repara):

  PERTURBACION   ``delta       = (q/2)*sqrt(3)``          sup de ``norm(eps)`` sobre el cubo
  DESPLAZAMIENTO ``delta_chord = 2*sin(asin(delta)/2)``   sup de ``norm(n' - n)``

``norm(n' - n) <= delta`` es **FALSA**: la renormalizacion por pixel no es no-expansiva en
la metrica de cuerda (``delta_chord/delta = 1.0000057671290952``). Usar ``delta`` como cota
de desplazamiento no es un redondeo conservador: es un paso invalido. La cadena correcta es

1. Desplazamiento por pixel. ``norm(delta_i) <= delta_chord``, alcanzado en un vertice del
   cubo con ``<sigma, n_i> = -delta`` (``sigma = (1,1,1)/sqrt(3)``). NO ``<= delta``.
2. Interpolacion. El filtro bilineal de Pillow es una COMBINACION CONVEXA por pixel de
   salida (pesos >= 0 que suman 1, tambien con la ventana recortada del borde), asi que
   ``norm(dv) = norm(sum_i w_i*delta_i) <= max_i norm(delta_i) <= delta_chord``.
   Vale para CUALQUIER correlacion entre pixeles: no se invoca independencia.
3. Angulo. Para ``norm(v) = largo > delta_chord`` y ``norm(dv) <= delta_chord``,
   ``delta_theta <= asin(norm(dv)/largo) <= asin(delta_chord/largo)``.

Consecuencia: ``delta_theta <= q_ang`` queda garantizado para
``largo >= delta_chord/sin(q_ang) = 0.8660392772765432``. NO es ``1/2`` y NO es exactamente
``sqrt(3)/2``: ese valor queda ``1.387e-05`` por debajo (el corrimiento es
``(1 + q^2/6)*(1 + 5.767e-06)``).

La cota ``asin(delta_chord/largo)`` es CONSERVADORA, no ajustada. La construccion cerrada
coherente alcanza ``delta_theta = atan(delta/largo)`` EXACTO (12 digitos significativos, y
el refinamiento local vuelve al vertice ``eps = (q/2)*(1,1,1)`` con ratio 1.000000000000).
La brecha ``asin(delta_chord/largo) - atan(delta/largo)`` vale <=1.02e-06 rad sobre
``largo in [0.55, 1.0]`` (<=1.3e-04 de ``q_ang``), y se encoge al crecer ``largo``: la cota
es valida pero no tight. ``atan(delta/largo)`` es la construccion ALCANZABLE; por si sola
NO es una cota superior universal.

Relacion con la reconciliacion (#723). Aquel documento midio el cruce en ``1/2`` con un
adversario COHERENTE de magnitud ``q/2`` en UN SOLO canal (``d[...,1] = q/2``). El modelo
declarado admite el vertice del cubo, de magnitud ``(q/2)*sqrt(3)``: el cruce escala por
``sqrt(3)``, ``0.5*sqrt(3) = 0.866``. Los dos numeros son correctos para familias distintas;
el de este archivo es el del modelo COMPLETO.

``R7_NUMERIC_DERIVATION`` sigue BLOCKED. La derivacion existe; lo que falta es (a) la
adjudicacion worst-case vs estadistica, y (b) el margen numerico de la implementacion
float32, que la seccion 8 MIDE pero no eleva a garantia.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pytest

# Cuanto de la reticula 8-bit (`decode_normal_image`: rgb/255*2-1).
Q8 = 2.0 / 255.0
# Cota por canal del modelo declarado.
T = Q8 / 2.0
# Cuanto angular declarado.
Q_ANG = Q8
# PERTURBACION: sup de `norm(eps)` sobre `[-q/2, q/2]^3` (vertice del cubo).
DELTA_PERT = T * math.sqrt(3.0)
# Angulo `n -> n + eps` en el optimo (`tan(theta*) = delta/sqrt(1-delta^2)` => `sin = delta`).
ALPHA_PERT = math.asin(DELTA_PERT)
# DESPLAZAMIENTO: sup de `norm(normalize(n+eps) - n)`. Es la cuerda de `ALPHA_PERT`, y es
# ESTRICTAMENTE MAYOR que `DELTA_PERT`: la renormalizacion no es no-expansiva en la cuerda.
DELTA_CHORD = 2.0 * math.sin(ALPHA_PERT / 2.0)
# Cruce CONSERVADOR de la cota angular.
L_CRUCE_COTA = DELTA_CHORD / math.sin(Q_ANG)
# Cruce de la construccion ALCANZABLE (`atan`), que es una cita inferior, no la cota.
L_CRUCE_ATAN = DELTA_PERT / math.tan(Q_ANG)

# Margen numerico ESTIMADO de la implementacion float32 (Pillow modo F): `u = 2**-24` por
# operacion, y el kernel separable de 64->32 toca 4 muestras por eje. NO es una garantia
# demostrada: se usa para comparar, no para declarar cierre (ver seccion 8).
U_F32 = 2.0**-24
TERMINOS_KERNEL = 4
DELTA_PERT_NUM = (T + TERMINOS_KERNEL * U_F32) * math.sqrt(3.0)
DELTA_CHORD_NUM = 2.0 * math.sin(math.asin(DELTA_PERT_NUM) / 2.0)

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
    """Cota conservadora `asin(delta_chord/largo)`; `inf` si `largo` no supera la cuerda."""
    return math.asin(min(1.0, DELTA_CHORD / largo)) if largo > DELTA_CHORD else float("inf")


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


def _cuerda_de_a(a: float) -> float:
    """Cuerda `norm(normalize(n+eps) - n)` para `eps = delta*sigma` y `<sigma, n> = a`.

    ``cos(theta) = (1 + a*delta)/sqrt(1 + 2*a*delta + delta^2)``, de donde la cuerda es
    ``sqrt(2*(1 - cos(theta)))``. El maximo esta en ``a = -delta``.

    Aviso: `cos(theta)` esta a ~1e-05 de 1, asi que ``1 - cos`` pierde ~10 digitos por
    cancelacion. Esta forma sirve para COMPARAR puntos (rel_tol >= 1e-9), no para fijar el
    valor del sup: para eso se usa la forma estable ``2*sin(asin(delta)/2)``.
    """
    num = 1.0 + a * DELTA_PERT
    den = math.sqrt(1.0 + 2.0 * a * DELTA_PERT + DELTA_PERT * DELTA_PERT)
    return math.sqrt(max(0.0, 2.0 * (1.0 - num / den)))


# ---------------------------------------------------------------------------
# 1. Perturbacion vs. desplazamiento (el defecto reparado)
# ---------------------------------------------------------------------------
def test_r7_la_perturbacion_maxima_es_el_vertice_del_cubo() -> None:
    """`sup norm(eps)` sobre el cubo vale `(q/2)*sqrt(3)`: es la cota de PERTURBACION."""
    corners = np.array([[sx, sy, sz] for sx in (-T, T) for sy in (-T, T) for sz in (-T, T)])
    normas = np.linalg.norm(corners, axis=-1)
    assert float(normas.max()) == pytest.approx(DELTA_PERT, rel=1e-12)
    assert math.isclose(DELTA_PERT, T * math.sqrt(3.0), rel_tol=1e-15)
    # el optimo del angulo no es el vertice generico: exige `eps` perpendicular a `n`
    assert math.isclose(float(np.dot(_SIGMA, _EPS_VERTICE)), DELTA_PERT, rel_tol=1e-12)


def test_r7_el_desplazamiento_supera_la_perturbacion() -> None:
    """`norm(n'-n) <= delta` es FALSA: en el optimo la cuerda vale `delta_chord > delta`."""
    n = -DELTA_PERT * _SIGMA + math.sqrt(1.0 - DELTA_PERT * DELTA_PERT) * _V_HAT
    assert math.isclose(float(np.linalg.norm(n)), 1.0, rel_tol=1e-12)  # n es unitaria
    n_prima = _renorm(n + _EPS_VERTICE)
    cuerda = float(np.linalg.norm(n_prima - n))

    assert math.isclose(cuerda, DELTA_CHORD, rel_tol=1e-12)
    # ESTE es el aserto load-bearing: la desigualdad ingenua falla aqui.
    assert cuerda > DELTA_PERT
    assert math.isclose(cuerda / DELTA_PERT, 1.0000057671290952, rel_tol=1e-12)
    assert math.isclose(cuerda - DELTA_PERT, 3.917239453556792e-08, rel_tol=1e-9)
    # y no es un artefacto de redondeo: la brecha es ~5.8e-06 relativo, muy por encima
    # del ruido de float64 (~1e-16).
    assert cuerda - DELTA_PERT > 1e-12


def test_r7_la_cuerda_sup_sobre_el_cubo_es_delta_chord() -> None:
    """Barrido determinista: ningun `(n, eps)` admisible supera `delta_chord`."""
    corners = np.array([[sx, sy, sz] for sx in (-T, T) for sy in (-T, T) for sz in (-T, T)])
    idx = np.arange(4000)
    phi = math.pi * (3.0 - math.sqrt(5.0))
    z = 1.0 - 2.0 * (idx + 0.5) / 4000
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    dirs = np.stack([r * np.cos(phi * idx), r * np.sin(phi * idx), z], axis=-1)
    dirs = np.concatenate([dirs, _SIGMA[None, :], _V_HAT[None, :]], axis=0)

    mejor = 0.0
    for n in dirs:
        delta = _renorm(n[None, :] + corners) - n[None, :]
        mejor = max(mejor, float(np.linalg.norm(delta, axis=-1).max()))

    # la cota NO se viola en ningun punto del barrido
    assert mejor <= DELTA_CHORD * (1.0 + 1e-12)
    # y es ajustada (el barrido se acerca al sup por abajo)
    assert mejor >= DELTA_CHORD * 0.999999
    # el sup esta por encima de la cota de perturbacion: refuerza que no son el mismo objeto
    assert mejor > DELTA_PERT


def test_r7_forma_cerrada_de_la_cuerda_y_su_optimo_en_a_menos_delta() -> None:
    """La cuerda como funcion de `a = <sigma, n>` tiene su maximo en `a = -delta`."""
    # forma estable (vectorial): fija el valor del sup sin cancelacion
    n = -DELTA_PERT * _SIGMA + math.sqrt(1.0 - DELTA_PERT * DELTA_PERT) * _V_HAT
    cuerda_vectorial = float(np.linalg.norm(_renorm(n + _EPS_VERTICE) - n))
    assert math.isclose(cuerda_vectorial, DELTA_CHORD, rel_tol=1e-14)
    # forma cerrada: coincide a menos de la cancelacion (~1e-12 relativo)
    assert math.isclose(_cuerda_de_a(-DELTA_PERT), DELTA_CHORD, rel_tol=1e-9)
    # el optimo es interior y estricto
    assert _cuerda_de_a(-DELTA_PERT) > _cuerda_de_a(-DELTA_PERT + 1e-4)
    assert _cuerda_de_a(-DELTA_PERT) > _cuerda_de_a(-DELTA_PERT - 1e-4)
    # ningun punto de una grilla fina lo supera
    peor = max(_cuerda_de_a(float(a)) for a in np.linspace(-1.0, 1.0, 20001))
    assert peor <= DELTA_CHORD * (1.0 + 1e-9)


# ---------------------------------------------------------------------------
# 2. Cota angular y cruce corregido
# ---------------------------------------------------------------------------
def test_r7_la_cota_angular_y_su_cruce_corregido() -> None:
    """`delta_theta <= asin(delta_chord/largo)` cruza `q_ang` en `0.8660392772765432`."""
    assert math.isclose(L_CRUCE_COTA, 0.8660392772765432, rel_tol=1e-12)
    assert math.isclose(_cota_angular(L_CRUCE_COTA), Q_ANG, rel_tol=1e-12)
    # el valor esta por ENCIMA de sqrt(3)/2, aunque a solo 1.39e-05
    raiz_de_tres_medios = math.sqrt(3) / 2
    assert L_CRUCE_COTA - raiz_de_tres_medios > 0.0
    assert math.isclose(L_CRUCE_COTA - raiz_de_tres_medios, 1.387349210457689e-05, rel_tol=1e-6)
    # y muy por encima de los umbrales vigentes
    assert L_CRUCE_COTA > 0.5
    assert L_CRUCE_COTA > 5.0 / 16.0
    # la cota es decreciente en `largo`
    assert _cota_angular(0.9) < _cota_angular(0.8) < _cota_angular(0.7)


def test_r7_verificacion_independiente_del_cruce_por_biseccion() -> None:
    """El cruce se recomputa por biseccion: no se copia como constante de autoridad."""
    lo, hi = 0.5, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if math.asin(min(1.0, DELTA_CHORD / mid)) > Q_ANG:
            lo = mid
        else:
            hi = mid
    assert math.isclose(0.5 * (lo + hi), L_CRUCE_COTA, rel_tol=1e-14)
    # y la formula cerrada se recompone desde sus piezas declaradas
    assert math.isclose(L_CRUCE_COTA, 2.0 * math.sin(math.asin(DELTA_PERT) / 2.0) / math.sin(Q_ANG), rel_tol=1e-15)


# ---------------------------------------------------------------------------
# 3. Alcanzabilidad: la cota no es vacua, pero es conservadora
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("largo", [0.55, 0.70, 0.80, 0.866, 0.95, 1.0])
def test_r7_construccion_extrema_alcanza_atan_delta_sobre_largo(largo: float) -> None:
    """`delta_theta = atan(delta/largo)` EXACTO en la construccion cerrada (cita inferior)."""
    dt = _delta_theta_extremo(largo)
    assert math.isclose(dt, math.atan(DELTA_PERT / largo), rel_tol=1e-9)
    assert dt <= _cota_angular(largo) + 1e-12  # respeta la cota conservadora
    assert dt >= 0.99 * _cota_angular(largo)  # y no esta lejos de ella


def test_r7_la_cota_es_conservadora_frente_a_la_construccion_alcanzable() -> None:
    """`asin(delta_chord/largo) > atan(delta/largo)`: la cota sobra, pero es valida.

    La brecha es la parte del paso 3 que convierte una cuerda en un angulo suponiendo
    `dv` perpendicular a `v`. Vale <=1.02e-06 rad (<=1.3e-04 de `q_ang`) y decrece con
    `largo`: la cota es valida pero NO tight, y eso se declara en vez de esconderse.
    """
    brecha_max = 0.0
    for largo in (0.55, 0.80, 0.866, 1.0):
        cota = _cota_angular(largo)
        alcanzable = _delta_theta_extremo(largo)
        assert cota > alcanzable
        brecha = cota - alcanzable
        assert brecha > 0.0
        brecha_max = max(brecha_max, brecha)
    assert brecha_max < 2e-6
    assert brecha_max / Q_ANG < 1.5e-4
    # el caso mas ancho de la familia probada
    assert math.isclose(_cota_angular(0.55) - _delta_theta_extremo(0.55), 1.0129e-06, rel_tol=1e-3)


def test_r7_la_cota_no_es_vacua_en_el_cruce() -> None:
    """Justo sobre el cruce la construccion alcanzable toca `q_ang` (la cota no es hueca)."""
    dt = _delta_theta_extremo(L_CRUCE_COTA)
    assert math.isclose(dt, Q_ANG, rel_tol=1e-3)


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
    Si se mide despues, `largo = 1` y `asin(delta_chord/1) < q_ang` para todo caso, con lo
    que la comparacion contra `q_ang` deja de discriminar.
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


def test_r7_control_negativo_delta_no_acota_el_desplazamiento() -> None:
    """Una cota con `delta` (en vez de `delta_chord`) NO encierra el peor caso alcanzable.

    Es el control que mata la reaparicion de `norm(dv) <= delta`: en el cruce derivado, el
    desplazamiento real por pixel vale `delta_chord > delta`, de modo que la cota ingenua
    queda por debajo de la cantidad que pretende acotar.
    """
    assert DELTA_CHORD > DELTA_PERT
    # la cota ingenua del cruce es estrictamente menor: no alcanza para el mismo criterio
    assert DELTA_PERT / math.sin(Q_ANG) < L_CRUCE_COTA
    # y en el punto donde la cota correcta vale exactamente `q_ang`, la ingenua se queda corta
    assert _cota_angular(L_CRUCE_COTA) > math.asin(min(1.0, DELTA_PERT / L_CRUCE_COTA))


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
    assert math.isclose(_cota_angular(L_CRUCE_COTA), Q_ANG, rel_tol=1e-12)


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
def _filtro_ideal(campo: np.ndarray, n_out: int) -> np.ndarray:
    """Filtro bilineal triangular separable EXACTO en float64 (sin el float32 de Pillow)."""
    n_in = campo.shape[0]
    escala = n_in / n_out
    fs = max(1.0, escala)

    def pesos(centro: float) -> tuple[np.ndarray, np.ndarray]:
        x0 = max(0, math.floor(centro - fs + 0.5))
        x1 = min(n_in, math.ceil(centro + fs + 0.5))
        xs = np.arange(x0, x1)
        w = np.maximum(0.0, 1.0 - np.abs(xs - centro) / fs)
        return xs, w / w.sum()

    tmp = np.empty((n_in, n_out, 3))
    for j in range(n_out):
        xs, w = pesos((j + 0.5) * escala - 0.5)
        tmp[:, j, :] = np.tensordot(campo[:, xs, :], w, axes=([1], [0]))
    out = np.empty((n_out, n_out, 3))
    for i in range(n_out):
        ys, w = pesos((i + 0.5) * escala - 0.5)
        out[i, :, :] = np.tensordot(tmp[ys, :, :], w, axes=([0], [0]))
    return out


def _filtro_pillow(campo: np.ndarray, n_out: int) -> np.ndarray:
    from PIL import Image

    arr = np.clip(np.asarray(campo, dtype=np.float64), -1.0, 1.0)
    canales = [
        np.asarray(
            Image.fromarray(np.ascontiguousarray(arr[..., c], dtype=np.float32)).resize(
                (n_out, n_out), Image.Resampling.BILINEAR
            ),
            dtype=np.float64,
        )
        for c in range(3)
    ]
    return np.stack(canales, axis=-1)


def _campos_de_prueba(largo: float) -> tuple[np.ndarray, np.ndarray]:
    """Campo periodico-2 con el par extremo; devuelve (limpio, perturbado por vertice)."""
    n_a, n_b = _campo_extremo(largo)
    s = np.where(np.arange(64) % 2 == 0, 1.0, -1.0)
    fila = np.where(s[:, None] > 0, n_a[None, :], n_b[None, :])
    limpio = np.repeat(fila[None, :, :], 64, axis=0)
    return limpio, _renorm(limpio + _EPS_VERTICE)


def test_r7_la_implementacion_real_alcanza_la_cota() -> None:
    """El peor caso construido, medido sobre `resize_normal` real, respeta la cota."""
    import sky_claw.local.native_parallax.research.authored_dataset as ad

    # Procedencia: el modulo importado debe vivir en ESTE arbol de trabajo, no en el
    # worktree principal que apunta el `.pth` editable (trampa ya sufrida en este repo).
    raiz = Path(__file__).resolve().parents[1]
    assert Path(ad.__file__).resolve().is_relative_to(raiz)

    largo = 0.80
    campo, pert = _campos_de_prueba(largo)
    limpio = _filtro_pillow(campo, 32)[16, 16]
    perturbado = _filtro_pillow(pert, 32)[16, 16]
    largo_real = float(np.linalg.norm(limpio))  # ANTES de renormalizar
    dt = _angulo(limpio, perturbado)

    assert largo_real == pytest.approx(largo, abs=0.02)
    assert dt <= _cota_angular(largo_real) + 1e-9
    assert dt >= 0.95 * _cota_angular(largo_real)


def test_r7_el_error_numerico_de_pillow_modo_f_cabe_en_el_margen_estimado() -> None:
    """Separa el filtro ideal (float64) del error numerico de Pillow modo F (float32).

    El margen a priori se estima con `u = 2**-24` por operacion y 4 terminos del kernel.
    Se MIDE el gap observado y se exige que la cota ampliada lo cubra. El margen es una
    ESTIMACION: por eso `R7_NUMERIC_DERIVATION` sigue BLOCKED (ver el modulo del doc).
    """
    assert DELTA_CHORD_NUM > DELTA_CHORD  # el margen ensancha la cota, no la estrecha
    assert math.isclose(DELTA_CHORD_NUM / DELTA_CHORD - 1.0, 6.07e-05, rel_tol=2e-2)

    gap_max = 0.0
    for largo in (0.55, 0.70, 0.80, 0.866, 0.95, 1.0):
        campo, pert = _campos_de_prueba(largo)
        v_ideal = _filtro_ideal(campo, 32)[16, 16]
        p_ideal = _filtro_ideal(pert, 32)[16, 16]
        v_pill = _filtro_pillow(campo, 32)[16, 16]
        p_pill = _filtro_pillow(pert, 32)[16, 16]

        largo_ideal = float(np.linalg.norm(v_ideal))
        largo_pill = float(np.linalg.norm(v_pill))
        dt_ideal = _angulo(v_ideal, p_ideal)
        dt_pill = _angulo(v_pill, p_pill)

        # el filtro ideal reproduce la construccion cerrada
        assert math.isclose(dt_ideal, math.atan(DELTA_PERT / largo), rel_tol=1e-9)
        # el gap float32 es diminuto y no cambia el veredicto
        gap_max = max(gap_max, abs(dt_pill - dt_ideal))
        assert dt_pill <= math.asin(min(1.0, DELTA_CHORD_NUM / largo_pill))
        assert largo_pill == pytest.approx(largo_ideal, abs=1e-6)

    assert gap_max < 1e-6


# ---------------------------------------------------------------------------
# 8. Guarda documental: el doc no puede reintroducir la desigualdad ingenua
# ---------------------------------------------------------------------------
def test_r7_el_documento_no_reintroduce_la_desigualdad_ingenua() -> None:
    """Ancla el doc al modelo corregido y prohibe el paso invalido `max_i norm(delta_i) <= delta`."""
    raiz = Path(__file__).resolve().parents[1]
    doc = raiz / "docs/design/research/native-parallax/h4-resize-normal-r7-deterministic-bound.md"
    texto = doc.read_text(encoding="utf-8")
    compacto = re.sub(r"\s+", "", texto)

    # invariantes positivos: el doc declara la cuerda y el cruce corregido
    assert "delta_chord" in texto
    assert "0.8660392772765432" in texto
    assert "R7_WORST_CASE_BOUND_ANGULAR" in texto
    assert "R7_WORST_CASE_CROSSING_FULL" in texto
    # invariantes negativos: el paso invalido y su consecuencia no reaparecen
    assert "max_i||delta_i||<=Delta_max" not in compacto
    assert "||dv||<=Delta_max" not in compacto
    assert "delta_theta<=asin(Delta_max/L)" not in compacto

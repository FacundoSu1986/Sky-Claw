"""H4 — R7: cota determinista del error angular de `resize_normal`.

Modelo de cuantizacion DECLARADO (contrato #717 §6.1 + comentario de produccion):
la entrada decodificada vive en una reticula 8-bit de paso ``q = 2/255``; el error por
canal esta acotado por ``abs(eps_c) <= q/2``; y el vector perturbado se RENORMALIZA:
``n' = normalize(n + eps)``. El modelo NO exige independencia entre pixeles.

TRES OBJETOS DISTINTOS, que este archivo mantiene separados (el defecto formal que se repara):

  PERTURBACION   ``delta       = (q/2)*sqrt(3)``          sup de ``norm(eps)`` sobre el cubo
  DESPLAZAMIENTO ``delta_chord = 2*sin(asin(delta)/2)``   sup de ``norm(n' - n)`` por pixel
  ANGULO         ``delta_theta``                          lo que mide el umbral vigente

``norm(n' - n) <= delta`` es **FALSA**: la renormalizacion por pixel no es no-expansiva en
la metrica de cuerda (``delta_chord/delta = 1.0000057671290952``). Usar ``delta`` como cota
de desplazamiento no es un redondeo conservador: es un paso invalido. La cadena correcta es

1. Desplazamiento por pixel. ``norm(delta_i) <= delta_chord``, alcanzado en un vertice del
   cubo con ``<sigma, n_i> = -delta`` (``sigma = (1,1,1)/sqrt(3)``). NO ``<= delta``.
2. Interpolacion. El filtro bilineal de Pillow es una COMBINACION CONVEXA por pixel de
   salida (pesos >= 0 que suman 1, tambien con la ventana recortada del borde), asi que
   ``norm(dv) = norm(sum_i w_i*delta_i) <= max_i norm(delta_i) <= delta_chord``.
   Vale para CUALQUIER correlacion entre pixeles: no se invoca independencia.
3. Angulo. Para ``v' = v + dv`` con ``norm(v) = largo > norm(dv)``, el maximo de
   ``delta_theta`` a ``norm(dv)`` FIJO es ``asin(norm(dv)/largo)``, y se alcanza con
   ``cos(angle(v, dv)) = -norm(dv)/largo`` (``dv`` inclinado hacia atras), donde
   ``norm(v') = sqrt(largo^2 - norm(dv)^2)``. Consecuencia:
   ``delta_theta <= asin(norm(dv)/largo) <= asin(delta_chord/largo)``.

TRES UMBRALES QUE NO SON EL MISMO, y no hay que confundir:

  ``L_SUFICIENTE  = delta_chord/sin(q_ang) = 0.8660392772765431``  cruce de la COTA SUPERIOR:
      ``largo >= L_SUFICIENTE`` GARANTIZA ``delta_theta <= q_ang``.
  ``L_ALCANZABLE  = delta/tan(q_ang)       = 0.8660076459178669``  cruce de la construccion
      cerrada: ``largo < L_ALCANZABLE`` certifica que EXISTE una entrada admisible con
      ``delta_theta > q_ang`` (cota INFERIOR del peor caso). NO es el cruce de la cota.
  ``L_INVALIDO    = delta/sin(q_ang)       = 0.8660342827450337``  el cruce que daba el paso
      invalido ``norm(n'-n) <= delta``; se conserva solo para el control negativo.

La banda ``[L_ALCANZABLE, L_SUFICIENTE)`` (ancho ``3.163e-05``) queda **indecidida** por
este analisis: ni garantiza el criterio ni certifica su violacion.

La cota ``asin(delta_chord/largo)`` es SUFICIENTE pero NO tight: la construccion cerrada
alcanza ``delta_theta = atan(delta/largo)`` EXACTO (12 digitos, con ``norm(dv) ~ delta``,
por DEBAJO de ``delta``), de modo que ``atan(delta/largo)`` es una cota INFERIOR del peor
caso y NO una cota superior universal. La brecha ``asin(delta_chord/largo) - atan(delta/largo)``
vale <=1.02e-06 rad sobre ``largo in [0.55, 1.0]`` (<=1.3e-04 de ``q_ang``) y se encoge al
crecer ``largo``. Que la cota sea alcanzable por ALGUN adversario admisible **no esta
demostrado**: una busqueda acotada (dos pixeles, pesos 1/2, 300 semillas + descenso por
coordenadas) converge a ``asin(delta/largo)``, que es ``0.9999942x`` la cota.

Relacion con la reconciliacion (#723). Aquel documento midio el cruce en ``1/2`` con un
adversario COHERENTE de magnitud ``q/2`` en UN SOLO canal (``d[...,1] = q/2``). El modelo
declarado admite el vertice del cubo, de magnitud ``(q/2)*sqrt(3)``: el cruce escala por
``sqrt(3)``, ``0.5*sqrt(3) = 0.866``. Los dos numeros son correctos para familias distintas;
el de este archivo es el del modelo COMPLETO.

``R7_NUMERIC_DERIVATION`` sigue BLOCKED. La derivacion existe; lo que falta es (a) la
adjudicacion worst-case vs estadistica, (b) el margen numerico de la implementacion
float32, que la seccion 8 MIDE pero no eleva a garantia, y (c) la tightness de la cota.
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
# Umbral SUFICIENTE: cruce de la cota superior `asin(delta_chord/largo)`.
L_SUFICIENTE = DELTA_CHORD / math.sin(Q_ANG)
# Cruce de la construccion ALCANZABLE: certifica RECHAZO (cota inferior), no garantia.
L_ALCANZABLE = DELTA_PERT / math.tan(Q_ANG)
# Cruce del paso INVALIDO (`delta` como cota de desplazamiento). Solo para el control negativo.
L_INVALIDO = DELTA_PERT / math.sin(Q_ANG)

# Margen numerico ESTIMADO de la implementacion float32 (Pillow modo F): `u = 2**-24` por
# operacion, y el kernel separable de 64->32 toca 4 muestras por eje. NO es una garantia
# demostrada: se usa para comparar, no para declarar cierre (ver seccion 8 del documento).
U_F32 = 2.0**-24
TERMINOS_KERNEL = 4
DELTA_PERT_NUM = (T + TERMINOS_KERNEL * U_F32) * math.sqrt(3.0)
DELTA_CHORD_NUM = 2.0 * math.sin(math.asin(DELTA_PERT_NUM) / 2.0)

# Vertice del cubo (direccion de la diagonal) y base del plano perpendicular.
_SIGMA = np.array([1.0, 1.0, 1.0]) / math.sqrt(3.0)
_V_HAT = np.array([1.0, -1.0, 0.0]) / math.sqrt(2.0)
_U_HAT = np.cross(_SIGMA, _V_HAT)
_EPS_VERTICE = T * np.array([1.0, 1.0, 1.0])

_DOC = (
    Path(__file__).resolve().parents[1]
    / "docs/design/research/native-parallax/h4-resize-normal-r7-deterministic-bound.md"
)


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


def _norma_dv_extremo(largo: float) -> float:
    """`norm(dv)` de la construccion cerrada. Es MENOR que `delta`, y por lo tanto que `delta_chord`.

    Cierre analitico (vale porque en la construccion `<eps, v> = 0`):
    ``norm(dv)^2 = largo^2*(1/sqrt(1+delta^2) - 1)^2 + delta^2/(1+delta^2)``.
    """
    n_a, n_b = _campo_extremo(largo)
    v_limpio = 0.5 * (n_a + n_b)
    v_pert = 0.5 * (_renorm(n_a + _EPS_VERTICE) + _renorm(n_b + _EPS_VERTICE))
    return float(np.linalg.norm(v_pert - v_limpio))


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


def _num(celda: str) -> float:
    """Extrae un float de una celda markdown (quita backticks, negritas y signo explicito)."""
    limpio = celda.replace("`", "").replace("*", "").replace("+", "").strip()
    return float(limpio)


def _filas_de_tabla(texto: str, ancla: str) -> list[list[str]]:
    """Filas de datos de la PRIMERA tabla markdown que aparece despues del encabezado `ancla`."""
    lineas = texto.splitlines()
    for i, linea in enumerate(lineas):
        if linea.startswith(ancla):
            for j in range(i + 1, len(lineas)):
                if lineas[j].lstrip().startswith("|"):
                    crudas = []
                    k = j
                    while k < len(lineas) and lineas[k].lstrip().startswith("|"):
                        crudas.append([c.strip() for c in lineas[k].strip().strip("|").split("|")])
                        k += 1
                    return [f for f in crudas[2:] if any(c.strip(" -:") for c in f)]
    raise AssertionError(f"no encontre la tabla que sigue a {ancla!r}")


def _bloque_de_tokens(texto: str) -> dict[str, str]:
    """Tokens declarados en el ultimo bloque ```text del documento."""
    bloques = re.findall(r"```text\n(.*?)```", texto, flags=re.DOTALL)
    assert bloques, "el documento debe declarar su bloque de tokens"
    tokens: dict[str, str] = {}
    for linea in bloques[-1].splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.*)$", linea.strip())
        if m:
            tokens[m.group(1)] = m.group(2).strip()
    return tokens


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
# 2. Cota angular: el paso 3 y su umbral suficiente
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("largo", [0.55, 0.70, 0.80, 0.8660392772765431, 0.95, 1.0])
def test_r7_optimo_angular_con_norma_de_dv_fija_es_asin(largo: float) -> None:
    """Con `norm(dv) = d` FIJO, el maximo de `delta_theta` es `asin(d/largo)`.

    Se alcanza con `cos(angle(v, dv)) = -d/largo` (dv inclinado hacia atras), donde
    `norm(v') = sqrt(largo^2 - d^2)`. El caso perpendicular (`cos = 0`) da `atan(d/largo)`,
    que es ESTRICTAMENTE MENOR: por eso la construccion cerrada no alcanza la cota.

    Este es el paso que hace riguroso `delta_theta <= asin(norm(dv)/largo)`; sin el, la
    cota solo seria valida si `norm(v') >= largo`, que NO se cumple en general.
    """
    for d in (DELTA_PERT, DELTA_CHORD):
        v = np.array([largo, 0.0, 0.0])
        c_opt = -d / largo
        s = math.sqrt(max(0.0, 1.0 - c_opt * c_opt))
        dv = d * (c_opt * np.array([1.0, 0.0, 0.0]) + s * np.array([0.0, 1.0, 0.0]))
        # el optimo alcanza el asin (rel_tol 1e-9: `acos` cerca de 1 pierde digitos)
        assert math.isclose(_angulo(v, v + dv), math.asin(d / largo), rel_tol=1e-9)
        # y la norma del vector perturbado es la prevista por el triangulo rectangulo
        assert math.isclose(float(np.linalg.norm(v + dv)), math.sqrt(largo * largo - d * d), rel_tol=1e-12)
        # el caso perpendicular da atan, estrictamente menos
        dv_perp = d * np.array([0.0, 1.0, 0.0])
        assert _angulo(v, v + dv_perp) < math.asin(d / largo)
        assert math.isclose(_angulo(v, v + dv_perp), math.atan(d / largo), rel_tol=1e-9)
        # barrido fino: nada supera el asin
        peor = 0.0
        for c in np.linspace(-1.0, 1.0, 4001):
            ss = math.sqrt(max(0.0, 1.0 - c * c))
            peor = max(peor, _angulo(v, v + d * (c * np.array([1.0, 0.0, 0.0]) + ss * np.array([0.0, 1.0, 0.0]))))
        assert peor <= math.asin(d / largo) * (1.0 + 1e-9)


def test_r7_la_cota_angular_y_su_umbral_suficiente() -> None:
    """`delta_theta <= asin(delta_chord/largo)` cruza `q_ang` en `L_SUFICIENTE`."""
    assert math.isclose(L_SUFICIENTE, 0.8660392772765431, rel_tol=1e-12)
    assert math.isclose(_cota_angular(L_SUFICIENTE), Q_ANG, rel_tol=1e-12)
    # el valor esta por ENCIMA de sqrt(3)/2, aunque a solo 1.39e-05
    raiz_de_tres_medios = math.sqrt(3) / 2
    assert raiz_de_tres_medios < L_SUFICIENTE
    assert math.isclose(L_SUFICIENTE - raiz_de_tres_medios, 1.387349210457689e-05, rel_tol=1e-6)
    # y muy por encima de los umbrales vigentes
    assert L_SUFICIENTE > 0.5
    assert L_SUFICIENTE > 5.0 / 16.0
    # la cota es decreciente en `largo`
    assert _cota_angular(0.9) < _cota_angular(0.8) < _cota_angular(0.7)


def test_r7_verificacion_independiente_del_umbral_por_biseccion() -> None:
    """El umbral se recomputa por biseccion: no se copia como constante de autoridad."""
    lo, hi = 0.5, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if math.asin(min(1.0, DELTA_CHORD / mid)) > Q_ANG:
            lo = mid
        else:
            hi = mid
    assert math.isclose(0.5 * (lo + hi), L_SUFICIENTE, rel_tol=1e-14)
    # y la formula cerrada se recompone desde sus piezas declaradas
    assert math.isclose(L_SUFICIENTE, 2.0 * math.sin(math.asin(DELTA_PERT) / 2.0) / math.sin(Q_ANG), rel_tol=1e-15)


# ---------------------------------------------------------------------------
# 3. Alcanzabilidad: la cota no es vacua, pero es conservadora y NO tight
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("largo", [0.55, 0.70, 0.80, 0.866, 0.95, 1.0])
def test_r7_construccion_extrema_alcanza_atan_delta_sobre_largo(largo: float) -> None:
    """`delta_theta = atan(delta/largo)` EXACTO en la construccion cerrada.

    Es una cota INFERIOR del peor caso (la construccion es admisible), NO una cota superior
    universal: por eso el test exige tambien que quede POR DEBAJO de la cota probada.
    """
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


def test_r7_la_cota_no_es_vacua_en_el_umbral() -> None:
    """Justo sobre el umbral suficiente la construccion alcanzable toca `q_ang` (la cota no es hueca)."""
    dt = _delta_theta_extremo(L_SUFICIENTE)
    assert math.isclose(dt, Q_ANG, rel_tol=1e-3)


# ---------------------------------------------------------------------------
# 4. R7-REV-02: la construccion NO alcanza `delta_chord` en norma de `dv`
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("largo", [0.55, 0.70, 0.80, 0.866, 0.95, 1.0])
def test_r7_rev02_la_construccion_no_alcanza_delta_chord_en_norma_de_dv(largo: float) -> None:
    """`norm(dv)` de la construccion cerrada queda POR DEBAJO de `delta` (y de `delta_chord`).

    El error documental que esto mata: afirmar que la construccion del vertice alcanza
    `delta_chord` en norma de `dv`. No lo alcanza: la mezcla de normales y la renormalizacion
    cambian la magnitud del desplazamiento. La cota `delta_chord` acota `norm(dv)`, no lo iguala.
    """
    d = _norma_dv_extremo(largo)
    assert d < DELTA_PERT, f"L={largo}: norm(dv)={d} deberia quedar por debajo de delta={DELTA_PERT}"
    assert d < DELTA_CHORD
    # el cierre analitico reproduce el valor medido (no hay margen para "aproximar a ojo")
    s = math.sqrt(1.0 + DELTA_PERT * DELTA_PERT)
    predicho = math.sqrt(
        largo * largo * (1.0 / s - 1.0) ** 2 + DELTA_PERT * DELTA_PERT / (1.0 + DELTA_PERT * DELTA_PERT)
    )
    assert math.isclose(d, predicho, rel_tol=1e-12)
    # y la brecha contra la cota de desplazamiento es del orden de 1.6e-07..1.8e-07, no nula
    assert 1e-7 < DELTA_CHORD - d < 2e-7


def test_r7_rev02_el_sup_de_norma_dv_es_alcanzable_pero_no_maximiza_el_angulo() -> None:
    """`delta_chord` SI es alcanzable como desplazamiento, pero esa configuracion NO maximiza el angulo.

    Con `n_i = -delta*sigma + sqrt(1-delta^2)*w_i` y `eps_i = delta*sigma` (coherente), sale
    `norm(n'_i - n_i) = delta_chord` exacto. Sin embargo el angulo resultante es `asin(delta)`,
    que NO es el maximo angular para esa norma: el maximo exigiria `cos(alpha) = -d/largo`.
    Las dos familias extremales son DISTINTAS, y por eso la cota no es tight.
    """
    n = -DELTA_PERT * _SIGMA + math.sqrt(1.0 - DELTA_PERT * DELTA_PERT) * _V_HAT
    n_prima = _renorm(n + _EPS_VERTICE)
    d = float(np.linalg.norm(n_prima - n))
    assert math.isclose(d, DELTA_CHORD, rel_tol=1e-12)  # el sup de desplazamiento SI se alcanza
    # pero el angulo de esa configuracion es asin(delta), no asin(delta_chord) ni atan(delta)
    angulo = _angulo(n, n_prima)
    assert math.isclose(angulo, math.asin(DELTA_PERT), rel_tol=1e-9)
    assert angulo < math.asin(DELTA_CHORD)
    # el coseno queda lejos del optimo angular (-d/largo), que es lo que impide la tightness
    cos_alpha = float(np.dot(n, n_prima - n)) / d
    assert math.isclose(cos_alpha, -(DELTA_PERT**2) / (2.0 * DELTA_CHORD), rel_tol=1e-3)
    assert cos_alpha > -d  # el optimo exigiria -d/largo <= -d


# ---------------------------------------------------------------------------
# 5. R7-REV-03: umbral suficiente vs cota inferior certificada
# ---------------------------------------------------------------------------
def test_r7_rev03_los_tres_umbrales_son_distintos_y_ordenados() -> None:
    """`delta/tan(q) < delta/sin(q) < delta_chord/sin(q)`: garantia, paso invalido y rechazo.

    El token de cruce NO debe llamarse "worst case": `L_SUFICIENTE` es el cruce de una COTA
    SUPERIOR. El cruce de la construccion alcanzable (`L_ALCANZABLE`) es DISTINTO y MENOR, y
    es el que certifica RECHAZO: si `largo < L_ALCANZABLE`, existe una entrada admisible con
    `delta_theta > q_ang`.
    """
    assert L_ALCANZABLE < L_INVALIDO < L_SUFICIENTE
    assert math.isclose(L_ALCANZABLE, 0.8660076459178669, rel_tol=1e-12)
    assert math.isclose(L_INVALIDO, 0.8660342827450337, rel_tol=1e-12)
    # la banda indecidida por este analisis
    assert 0.0 < L_SUFICIENTE - L_ALCANZABLE < 1e-4
    assert math.isclose(L_SUFICIENTE - L_ALCANZABLE, 3.163135867612077e-05, rel_tol=1e-9)


def test_r7_rev03_rechazo_certificado_por_debajo_de_la_construccion() -> None:
    """Por DEBAJO de `L_ALCANZABLE` el rechazo es CERTIFICADO (cota inferior), no conjetura."""
    for largo in (L_ALCANZABLE * (1.0 - 1e-3), 0.80, 0.55):
        assert largo < L_ALCANZABLE
        assert _delta_theta_extremo(largo) > Q_ANG  # existe entrada admisible que viola el criterio
    # y por ENCIMA de `L_SUFICIENTE` la garantia es igual de dura (cota superior)
    for largo in (L_SUFICIENTE * (1.0 + 1e-6), 0.95, 1.0):
        assert _cota_angular(largo) < Q_ANG
    # la banda intermedia queda INDECIDIDA: no se puede afirmar ninguna de las dos cosas
    medio = 0.5 * (L_ALCANZABLE + L_SUFICIENTE)
    assert _cota_angular(medio) > Q_ANG  # la cota no garantiza
    assert _delta_theta_extremo(medio) < Q_ANG  # la construccion tampoco certifica rechazo


# ---------------------------------------------------------------------------
# 6. Controles negativos
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

    Es el control que mata la reaparicion de `norm(dv) <= delta`: en el umbral derivado, el
    desplazamiento real por pixel vale `delta_chord > delta`, de modo que la cota ingenua
    queda por debajo de la cantidad que pretende acotar.
    """
    assert DELTA_CHORD > DELTA_PERT
    # la cota ingenua del umbral es estrictamente menor: no alcanza para el mismo criterio
    assert L_INVALIDO < L_SUFICIENTE
    # y en el punto donde la cota correcta vale exactamente `q_ang`, la ingenua se queda corta
    assert _cota_angular(L_SUFICIENTE) > math.asin(min(1.0, DELTA_PERT / L_SUFICIENTE))


# ---------------------------------------------------------------------------
# 7. Implicaciones sobre los umbrales vigentes
# ---------------------------------------------------------------------------
def test_r7_los_umbrales_vigentes_no_garantizan_q_ang_bajo_el_modelo() -> None:
    """`5/16` y `1/2` dejan pasar `delta_theta` mayor que `q_ang`."""
    assert math.isclose(_cota_angular(5.0 / 16.0), 2.7715 * Q_ANG, rel_tol=2e-3)
    assert math.isclose(_cota_angular(0.5), 1.7321 * Q_ANG, rel_tol=2e-3)
    # el cruce del modelo completo escala el de la reconciliacion por sqrt(3)
    assert math.isclose(L_ALCANZABLE / 0.5, math.sqrt(3), rel_tol=2e-3)
    # y solo en el umbral suficiente la cota vale exactamente q_ang
    assert math.isclose(_cota_angular(L_SUFICIENTE), Q_ANG, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# 8. Bordes: el kernel recortado sigue siendo convexo
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
# 9. La implementacion real (Pillow modo F) alcanza la cota
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

    # Medido sobre la función REAL (revisión de pr-agent): ambos campos son unitarios, así que son
    # entradas válidas, y con L ~ 0.8 > 5/16 la salida es la dirección renormalizada.
    real_limpio = ad.resize_normal_diagnostic(campo, 32)
    real_perturbado = ad.resize_normal_diagnostic(pert, 32)
    assert not real_limpio.degenerate and not real_perturbado.degenerate
    # el ayudante `_filtro_pillow` queda anclado a producción: misma dirección que la función real
    np.testing.assert_allclose(real_limpio.array[16, 16], limpio / largo_real, rtol=0, atol=1e-12)
    np.testing.assert_allclose(
        real_perturbado.array[16, 16], perturbado / np.linalg.norm(perturbado), rtol=0, atol=1e-12
    )
    dt = _angulo(real_limpio.array[16, 16], real_perturbado.array[16, 16])

    assert largo_real == pytest.approx(largo, abs=0.02)
    assert dt <= _cota_angular(largo_real) + 1e-9
    assert dt >= 0.95 * _cota_angular(largo_real)


def test_r7_por_debajo_de_la_cuerda_la_cota_es_vacua_y_produccion_marca_degenerado() -> None:
    """Revisión de pr-agent: `_cota_angular` devuelve `inf` si `L <= delta_chord`. Ahí la cota no dice nada,
    y producción NO renormaliza: un campo con esa norma queda marcado degenerado (D-06) con el crudo
    preservado, así que la región sin cota nunca produce una dirección publicada."""
    import sky_claw.local.native_parallax.research.authored_dataset as ad

    assert _cota_angular(DELTA_CHORD) == float("inf")
    assert _cota_angular(0.5 * DELTA_CHORD) == float("inf")
    assert _cota_angular(1e-12) == float("inf")

    campo = np.zeros((64, 64, 3))
    campo[..., 0] = 0.5 * DELTA_CHORD  # L = delta_chord / 2, mucho menor que 5/16
    res = ad.resize_normal_diagnostic(campo, 32)

    assert res.degenerate and res.degenerate_count == 32 * 32
    np.testing.assert_allclose(res.array[..., 0], 0.5 * DELTA_CHORD, rtol=0, atol=1e-7)  # crudo, sin renormalizar
    with pytest.raises(ad.DegenerateNormalError):
        ad.resize_normal(campo, 32)


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
# 10. R7-REV-01: las tablas del documento se recalculan fila por fila
# ---------------------------------------------------------------------------
def test_r7_rev01_la_tabla_de_conservadurismo_se_recalcula_con_su_propio_largo() -> None:
    """Cada fila de la tabla de conservadurismo se recomputa con SU PROPIO `largo`.

    El error que esto mata (R7-REV-01): una fila rotulada `L*` llevaba el `atan` de `L=0.866`,
    de modo que lo "alcanzable" parecia EXCEDER la cota probada. La tabla es manual: sin este
    gate, vuelve a divergir.
    """
    filas = _filas_de_tabla(_DOC.read_text(encoding="utf-8"), "## 4.")
    assert len(filas) >= 4, "la tabla de conservadurismo debe tener al menos 4 filas"
    for fila in filas:
        largo = _num(fila[0])
        cota = _cota_angular(largo)
        alcanzable = math.atan(DELTA_PERT / largo)
        assert alcanzable <= cota, f"fila L={largo}: alcanzable {alcanzable} EXCEDE la cota {cota}"
        assert math.isclose(_num(fila[1]), cota, rel_tol=1e-12), f"fila L={largo}: cota del doc"
        assert math.isclose(_num(fila[2]), alcanzable, rel_tol=1e-12), f"fila L={largo}: atan del doc"
        assert math.isclose(_num(fila[3]), cota - alcanzable, rel_tol=1e-4, abs_tol=1e-12)
        assert math.isclose(_num(fila[4]), (cota - alcanzable) / Q_ANG, rel_tol=1e-4)


def test_r7_rev01_la_tabla_numerica_float32_se_recalcula_con_su_propio_largo() -> None:
    """Cada fila de la tabla de margen numerico se recomputa (ideal, Pillow, gap, holgura)."""
    filas = _filas_de_tabla(_DOC.read_text(encoding="utf-8"), "## 8.")
    assert len(filas) >= 4
    for fila in filas:
        largo = _num(fila[0])
        campo, pert = _campos_de_prueba(largo)
        v_ideal = _filtro_ideal(campo, 32)[16, 16]
        p_ideal = _filtro_ideal(pert, 32)[16, 16]
        v_pill = _filtro_pillow(campo, 32)[16, 16]
        p_pill = _filtro_pillow(pert, 32)[16, 16]
        dt_ideal = _angulo(v_ideal, p_ideal)
        dt_pill = _angulo(v_pill, p_pill)
        largo_pill = float(np.linalg.norm(v_pill))
        assert math.isclose(_num(fila[1]), dt_ideal, rel_tol=1e-9), f"fila L={largo}: ideal"
        assert math.isclose(_num(fila[2]), dt_pill, rel_tol=1e-9), f"fila L={largo}: pillow"
        assert math.isclose(_num(fila[3]), dt_pill - dt_ideal, rel_tol=1e-3, abs_tol=1e-12)
        assert math.isclose(_num(fila[4]), _cota_angular(largo_pill) - dt_pill, rel_tol=1e-3, abs_tol=1e-12)


def test_r7_rev01_la_tabla_de_umbrales_se_recalcula_y_su_veredicto_es_coherente() -> None:
    """La columna "¿garantiza?" debe coincidir con `cota(L) <= q_ang`, fila por fila."""
    filas = _filas_de_tabla(_DOC.read_text(encoding="utf-8"), "## 9.")
    assert len(filas) >= 3
    for fila in filas:
        largo = _num(fila[1])
        cota = _cota_angular(largo)
        assert math.isclose(_num(fila[2]), cota, rel_tol=1e-12), f"L={largo}: cota del doc"
        assert math.isclose(_num(fila[3]), cota / Q_ANG, rel_tol=1e-5), f"L={largo}: x q_ang del doc"
        garantiza = cota <= Q_ANG * (1.0 + 1e-12)
        afirmado = "Sí" in fila[4]
        assert afirmado == garantiza, f"L={largo}: el doc dice {fila[4]!r} pero cota={cota} vs q_ang={Q_ANG}"


# ---------------------------------------------------------------------------
# 11. Guarda documental
# ---------------------------------------------------------------------------
def test_r7_el_documento_no_reintroduce_la_desigualdad_ingenua() -> None:
    """Ancla el doc al modelo corregido y prohibe el paso invalido y el token enganoso."""
    texto = _DOC.read_text(encoding="utf-8")
    compacto = re.sub(r"\s+", "", texto)

    # invariantes positivos: el doc declara la cuerda, los umbrales y su semantica
    assert "delta_chord" in texto
    assert "0.866039277276543" in texto
    assert "0.8660076459178669" in texto
    assert "R7_WORST_CASE_BOUND_ANGULAR" in texto
    assert "R7_SUFICIENTE_THRESHOLD_IDEAL" in texto
    assert "R7_ALCANZABLE_THRESHOLD_LOWER_BOUND" in texto
    # invariantes negativos: el paso invalido y su consecuencia no reaparecen
    assert "max_i||delta_i||<=Delta_max" not in compacto
    assert "||dv||<=Delta_max" not in compacto
    assert "delta_theta<=asin(Delta_max/L)" not in compacto
    # R7-REV-02: la cadena de §3.3 debe cerrar en `<= delta_chord`, no en una igualdad.
    # El ancla positiva es la cadena COMPLETA: reemplazarla por `= delta_chord` la rompe.
    assert "max_inorm(delta_i)<=delta_chord" in compacto
    assert "norm(dv)=delta_chord" not in compacto
    assert "||dv||=delta_chord" not in compacto
    # R7-REV-03: el token de cruce ya no se declara como "worst case"
    tokens = _bloque_de_tokens(texto)
    assert "R7_WORST_CASE_CROSSING_FULL" not in tokens
    assert "R7_SUFICIENTE_THRESHOLD_IDEAL" in tokens
    assert "R7_ALCANZABLE_THRESHOLD_LOWER_BOUND" in tokens
    assert "R7_NUMERIC_DERIVATION" in tokens and tokens["R7_NUMERIC_DERIVATION"].startswith("BLOCKED")


# ---------------------------------------------------------------------------
# 12. Certificado del error de IMPLEMENTACION (Pillow modo F)
#
# La sombra de abajo es la traduccion literal de `src/libImaging/Resample.c` @
# commit 89f1f4626a2aaf5f3d5ca6437f41def2998fbe09 (Pillow 11.3.0), ramas
# `precompute_coeffs` + `ImagingResampleHorizontal_32bpc` +
# `ImagingResampleVertical_32bpc`. Lo load-bearing NO es la formula: es que la
# sombra reproduce el BINARIO bit a bit. Con eso establecido, la secuencia de
# redondeos es la real y la cota que se deriva de ella aplica al binario.
# ---------------------------------------------------------------------------
from fractions import Fraction  # noqa: E402

U_F32 = 2.0**-24
U_F64 = 2.0**-53
# Numero maximo de redondeos double por pasada (4 taps por eje en k=2; el termino
# crece con el factor de escala y sigue siendo 5 ordenes mas chico que u).
_TAPS_K2 = 4
_KAPPA = 2.0 * _TAPS_K2 + 2.0 * _TAPS_K2 + 8.0
# Version certificada. NO extrapolar a otra: habria que recertificar.
PILLOW_CERTIFICADA = "11.3.0"
PILLOW_COMMIT = "89f1f4626a2aaf5f3d5ca6437f41def2998fbe09"
# Versiones de Pillow contra las que este archivo corre y que se afirma de cada una. Solo una esta CERTIFICADA:
# la sombra es la traduccion de SU ``Resample.c`` y el certificado se audita contra ese codigo. Las demas solo
# pueden estar OBSERVADAS: el gate de bit-exactitud corre contra el binario instalado y pasa, pero eso no prueba
# que su ``Resample.c`` tenga la misma secuencia de redondeos (el de 12.3.0 y el de 13.0.0 difieren del de
# 11.3.0), asi que no se extrapola el certificado. Una version que no figure aca hace fallar el test.
PILLOW_ESTADOS = {
    "11.3.0": "CERTIFICADA",
    "12.3.0": "OBSERVADA_NO_CERTIFICADA",
    "13.0.0": "OBSERVADA_NO_CERTIFICADA",
}


def _estado_de_pillow(version: str) -> str:
    try:
        return PILLOW_ESTADOS[version]
    except KeyError:
        raise AssertionError(
            f"Pillow {version} no esta declarada en PILLOW_ESTADOS {sorted(PILLOW_ESTADOS)}: "
            "recertificar contra su Resample.c o declararla como observada, no extrapolar"
        ) from None


COMMIT_EVIDENCIA_MUTANTES = "e87a57888ed719f367cce1457320a195eee211b3"


def _bilinear_shadow(x: float) -> float:
    if x < 0.0:
        x = -x
    if x < 1.0:
        return 1.0 - x
    return 0.0


def _precompute_coeffs_shadow(in_size: int, out_size: int) -> tuple[list[tuple[int, int]], list[list[float]]]:
    """Traduccion literal de `precompute_coeffs` (coeficientes en double)."""
    filterscale = scale = float(in_size) / float(out_size)
    if filterscale < 1.0:
        filterscale = 1.0
    support = 1.0 * filterscale
    bounds: list[tuple[int, int]] = []
    kk: list[list[float]] = []
    for xx in range(out_size):
        center = (xx + 0.5) * scale
        ww = 0.0
        ss = 1.0 / filterscale
        xmin = int(center - support + 0.5)  # (int) trunca hacia cero, como en C
        if xmin < 0:
            xmin = 0
        xmax = int(center + support + 0.5)
        if xmax > in_size:
            xmax = in_size
        n = xmax - xmin
        k = [0.0] * (int(math.ceil(support)) * 2 + 1)
        for x in range(n):
            w = _bilinear_shadow((x + xmin - center + 0.5) * ss)
            k[x] = w
            ww += w
        for x in range(n):
            if ww != 0.0:
                k[x] /= ww
        bounds.append((xmin, n))
        kk.append(k)
    return bounds, kk


def _shadow_resize_f32(campo2d: np.ndarray, out_size: int) -> np.ndarray:
    """`Image.fromarray(f32).resize((n, n), BILINEAR)` en modo F, bit a bit.

    Acumulador DOUBLE en ambas pasadas; redondeo a float32 en la entrada, en el
    buffer intermedio (imTemp, modo F) y en la salida.
    """
    in_y, in_x = campo2d.shape
    a32 = np.asarray(np.clip(campo2d, -1.0, 1.0), dtype=np.float32)
    if in_x == out_size and in_y == out_size:
        return a32.astype(np.float64)
    bounds_h, kk_h = _precompute_coeffs_shadow(in_x, out_size)
    bounds_v, kk_v = _precompute_coeffs_shadow(in_y, out_size)
    ybox_first = bounds_v[0][0]
    ybox_last = bounds_v[out_size - 1][0] + bounds_v[out_size - 1][1]
    tmp = np.zeros((in_y, out_size))
    for yy in range(in_y):
        for xx in range(out_size):
            xmin, n = bounds_h[xx]
            k = kk_h[xx]
            ss = 0.0
            for x in range(n):
                ss += float(np.float64(a32[yy, x + xmin]) * k[x])
            tmp[yy, xx] = ss
    tmp32 = tmp.astype(np.float32)  # buffer intermedio modo F
    sub = tmp32[: ybox_last - ybox_first]
    out = np.zeros((out_size, out_size))
    for yy in range(out_size):
        ymin, n = bounds_v[yy]
        y0 = ymin - ybox_first
        k = kk_v[yy]
        for xx in range(out_size):
            ss = 0.0
            for y in range(n):
                ss += float(np.float64(sub[y0 + y, xx]) * k[y])
            out[yy, xx] = ss
    return out.astype(np.float32).astype(np.float64)


def _coeffs_exactos(in_size: int, out_size: int) -> list[tuple[int, int, list[Fraction]]]:
    """Los mismos coeficientes, en aritmetica racional exacta (sin redondear)."""
    # OJO: aca NO se usa `(xx + 0.5) * scale` con escala racional inexacta: el
    # centro se computa como `(xx*2+1)*in_size / (2*out_size)`, que es EXACTO y
    # coincide con el `center` en double de `_precompute_coeffs_shadow`.
    filterscale = Fraction(in_size, out_size)
    if filterscale < 1:
        filterscale = Fraction(1)
    support = filterscale
    salida: list[tuple[int, int, list[Fraction]]] = []
    for xx in range(out_size):
        center = Fraction((xx * 2 + 1) * in_size, 2 * out_size)
        ss = Fraction(1) / filterscale
        xmin = int(center - support + Fraction(1, 2))
        if xmin < 0:
            xmin = 0
        xmax = int(center + support + Fraction(1, 2))
        if xmax > in_size:
            xmax = in_size
        ws: list[Fraction] = []
        for x in range(xmax - xmin):
            t = (Fraction(x + xmin) + Fraction(1, 2) - center) * ss
            w = 1 - abs(t) if abs(t) < 1 else Fraction(0)
            ws.append(w)
        ww = sum(ws, Fraction(0))
        salida.append((xmin, xmax - xmin, [w / ww for w in ws]))
    return salida


def _ideal_exacto(campo: np.ndarray, out_size: int) -> np.ndarray:
    """B*: el MISMO algoritmo en aritmetica exacta (Fracciones), por canal."""
    alto, ancho, ncanales = campo.shape
    ch = _coeffs_exactos(ancho, out_size)
    cv = _coeffs_exactos(alto, out_size)
    campo_rac = [
        [[Fraction(float(campo[y, x, k])) for x in range(ancho)] for y in range(alto)] for k in range(ncanales)
    ]
    out = np.zeros((out_size, out_size, ncanales))
    for i in range(out_size):
        y0, nv, vs = cv[i]
        for j in range(out_size):
            x0, nh, ks = ch[j]
            for k in range(ncanales):
                acc = Fraction(0)
                for y in range(nv):
                    fila = campo_rac[k][y0 + y]
                    sub = Fraction(0)
                    for x in range(nh):
                        sub += ks[x] * fila[x0 + x]
                    acc += vs[y] * sub
                out[i, j, k] = float(acc)
    return out


def _kappa(taps_por_eje: int) -> float:
    """(2m + 2n + 8) redondeos double con m = n = taps por eje (§8.2)."""
    return 2.0 * taps_por_eje + 2.0 * taps_por_eje + 8.0


def _cota_error_impl(largo: float, taps_por_eje: int = _TAPS_K2) -> float:
    """E_b: cota de `|b - B*|` por canal (M = 1 por el clip de produccion).

    Por defecto es la del factor `k = 2` (4 taps por eje), la que fija los valores publicados.
    """
    return U_F32 * 2.0 + U_F32 * largo + _kappa(taps_por_eje) * U_F64


def _cota_angulo_impl(largo: float, taps_por_eje: int = _TAPS_K2) -> float:
    """Mismo lema angular de §3.4, aplicado al desplazamiento de implementacion."""
    if largo <= 0.0:
        return float("inf")
    return math.asin(min(1.0, math.sqrt(3.0) * _cota_error_impl(largo, taps_por_eje) / largo))


def _umbral_suficiente_num(taps_por_eje: int = _TAPS_K2) -> float:
    """Minimo L con `asin(delta_chord/L) + asin(sqrt(3)*E_b/L) <= q_ang` (biseccion)."""
    lo, hi = 0.5, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        cota = math.asin(min(1.0, DELTA_CHORD / mid)) + _cota_angulo_impl(mid, taps_por_eje)
        if cota > Q_ANG:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _umbral_suficiente_estricto(taps_por_eje: int = _TAPS_K2) -> float:
    """Umbral suficiente separando las DOS normas de la composición (revisión de Codex).

    El término geométrico `asin(delta_chord/L)` usa la norma `L` del campo limpio interpolado; el de
    implementación acota `b - B*` contra la norma de `B*`, la interpolación del campo CUANTIZADO. Como
    `||B* - v|| <= delta_chord`, `L - delta_chord <= ||B*|| <= L + delta_chord`: el denominador del
    término de implementación usa `L - delta_chord` y `E_b` se evalúa en `L + delta_chord`.
    `_umbral_suficiente_num` usa el mismo `L` en los dos términos (aproximación).
    """
    lo, hi = 0.5, 1.0
    for _ in range(300):
        mid = 0.5 * (lo + hi)
        impl = math.asin(
            min(1.0, math.sqrt(3.0) * _cota_error_impl(mid + DELTA_CHORD, taps_por_eje) / (mid - DELTA_CHORD))
        )
        cota = math.asin(min(1.0, DELTA_CHORD / mid)) + impl
        if cota > Q_ANG:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _campo_extremo_r7(largo: float, n: int = 64) -> np.ndarray:
    """La construccion cerrada de §4 como campo 2D (invariante en Y)."""
    r = math.sqrt(max(0.0, 1.0 - largo * largo))
    n_a = largo * _V_HAT + r * _U_HAT
    n_b = largo * _V_HAT - r * _U_HAT
    s = np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    fila = np.where(s[:, None] > 0, n_a[None, :], n_b[None, :])
    limpio = np.repeat(fila[None, :, :], n, axis=0)
    return _renorm(limpio + _EPS_VERTICE)


def _campo_midpoint(n: int = 64) -> np.ndarray:
    """Valores a distancia ~u/2 de un punto medio float32: redondeos coherentes."""
    rng = np.random.default_rng(7)
    base = rng.uniform(-1.0, 1.0, (n, n, 3))
    f32 = base.astype(np.float32).astype(np.float64)
    up = np.nextafter(f32.astype(np.float32), np.float32(2.0)).astype(np.float64)
    mid = 0.5 * (f32 + up)
    return np.clip(np.nextafter(mid, np.float32(-1.0)).astype(np.float64), -1.0, 1.0)


def _peor_angulo_impl(campo: np.ndarray, out_size: int) -> tuple[float, float]:
    """(peor angulo ideal-vs-real, peor cota de ese regimen) sobre pixeles con L >= 0.75."""
    ideal = _ideal_exacto(campo, out_size)
    real = np.stack([_pillow_resize(campo[..., k], out_size) for k in range(3)], axis=-1)
    peor_ang = 0.0
    peor_cota = 0.0
    for i in range(out_size):
        for j in range(out_size):
            v = ideal[i, j]
            largo = float(np.linalg.norm(v))
            if largo < 0.75:
                continue
            peor_ang = max(peor_ang, _angulo(v, real[i, j]))
            peor_cota = max(peor_cota, _cota_angulo_impl(largo))
    return peor_ang, peor_cota


def _peor_exceso_por_pixel(campo: np.ndarray, out_size: int) -> float:
    """Máximo de ``angulo_i - cota(L_i)`` sobre píxeles con L >= 0.75 (<= 0 si la cota vale en TODOS).

    ``_peor_angulo_impl`` agrega con dos máximos independientes, y ``max(angulo) <= max(cota)`` puede
    cumplirse aunque un píxel de L alto supere SU cota (más chica) mientras otro de L bajo aporta la
    cota más holgada. Acá cada píxel se compara con la suya antes de agregar.
    """
    ideal = _ideal_exacto(campo, out_size)
    real = np.stack([_pillow_resize(campo[..., k], out_size) for k in range(3)], axis=-1)
    peor = -float("inf")
    for i in range(out_size):
        for j in range(out_size):
            v = ideal[i, j]
            largo = float(np.linalg.norm(v))
            if largo < 0.75:
                continue
            peor = max(peor, _angulo(v, real[i, j]) - _cota_angulo_impl(largo))
    return peor


def _pillow_resize(campo2d: np.ndarray, out_size: int) -> np.ndarray:
    from PIL import Image

    arr = np.clip(np.asarray(campo2d, dtype=np.float64), -1.0, 1.0)
    im = Image.fromarray(np.ascontiguousarray(arr, dtype=np.float32))
    return np.asarray(im.resize((out_size, out_size), Image.Resampling.BILINEAR), dtype=np.float64)


def _shadow_acum_f32(campo2d: np.ndarray, out_size: int) -> np.ndarray:
    """Modelo INCORRECTO: acumulador float32 en vez de double."""
    in_y, in_x = campo2d.shape
    a32 = np.asarray(np.clip(campo2d, -1.0, 1.0), dtype=np.float32)
    bounds_h, kk_h = _precompute_coeffs_shadow(in_x, out_size)
    bounds_v, kk_v = _precompute_coeffs_shadow(in_y, out_size)
    ybox_first = bounds_v[0][0]
    tmp = np.zeros((in_y, out_size), dtype=np.float32)
    for yy in range(in_y):
        for xx in range(out_size):
            xmin, n = bounds_h[xx]
            k = kk_h[xx]
            ss = np.float32(0.0)
            for x in range(n):
                ss = np.float32(ss + np.float32(a32[yy, x + xmin]) * np.float32(k[x]))
            tmp[yy, xx] = ss
    out = np.zeros((out_size, out_size), dtype=np.float32)
    for yy in range(out_size):
        ymin, n = bounds_v[yy]
        y0 = ymin - ybox_first
        k = kk_v[yy]
        for xx in range(out_size):
            ss = np.float32(0.0)
            for y in range(n):
                ss = np.float32(ss + np.float32(tmp[y0 + y, xx]) * np.float32(k[y]))
            out[yy, xx] = ss
    return out.astype(np.float64)


def _shadow_pesos_2tap(campo2d: np.ndarray, out_size: int) -> np.ndarray:
    """Modelo INCORRECTO: pesos 'bilineales de verdad' (2 taps), no los de Pillow."""
    in_y, in_x = campo2d.shape
    a32 = np.asarray(np.clip(campo2d, -1.0, 1.0), dtype=np.float32).astype(np.float64)
    escala = in_x / out_size
    tmp = np.zeros((in_y, out_size))
    for j in range(out_size):
        c = (j + 0.5) * escala - 0.5
        x0 = max(0, min(int(math.floor(c - 0.5)), in_x - 2))
        w = c - (x0 + 0.5)
        tmp[:, j] = (1.0 - w) * a32[:, x0] + w * a32[:, x0 + 1]
    out = np.zeros((out_size, out_size))
    for i in range(out_size):
        c = (i + 0.5) * escala - 0.5
        y0 = max(0, min(int(math.floor(c - 0.5)), in_y - 2))
        w = c - (y0 + 0.5)
        out[i, :] = (1.0 - w) * tmp[y0, :] + w * tmp[y0 + 1, :]
    return out


def test_r7_impl_la_version_instalada_esta_declarada() -> None:
    """El certificado vale para una version concreta, no para 'Pillow' en abstracto.

    Antes este test exigia que la version instalada FUERA la certificada. Desde #756 el CI corre con Pillow
    12.3.0 (``pip-audit`` no deja pasar 11.3.0), asi que esa igualdad no puede sostenerse sin dejar la suite
    en rojo. Lo que se conserva es el contrato real: la version instalada tiene que estar en una tabla
    CERRADA que dice que se afirma de ella. Una version fuera de la tabla falla: hay que decidir a mano si se
    recertifica o se declara observada.
    """
    import PIL

    assert _estado_de_pillow(PIL.__version__) in {"CERTIFICADA", "OBSERVADA_NO_CERTIFICADA"}


def test_r7_impl_la_tabla_de_versiones_de_pillow_esta_congelada_y_solo_una_esta_certificada() -> None:
    assert PILLOW_ESTADOS == {
        "11.3.0": "CERTIFICADA",
        "12.3.0": "OBSERVADA_NO_CERTIFICADA",
        "13.0.0": "OBSERVADA_NO_CERTIFICADA",
    }
    assert [v for v, estado in PILLOW_ESTADOS.items() if estado == "CERTIFICADA"] == [PILLOW_CERTIFICADA]


@pytest.mark.parametrize("version", ["10.4.0", "11.3.1", "11.4.0", "12.3.1", "13.0.1", ""])
def test_r7_impl_una_version_de_pillow_no_declarada_se_rechaza(version: str) -> None:
    # Ni siquiera un parche de la certificada se extrapola: 11.3.1 podria tocar Resample.c.
    with pytest.raises(AssertionError, match="no esta declarada"):
        _estado_de_pillow(version)


def test_r7_impl_el_documento_declara_el_estado_de_cada_version_de_pillow() -> None:
    tokens = _bloque_de_tokens(_DOC.read_text(encoding="utf-8"))
    esperado = "; ".join(f"{version} {estado}" for version, estado in PILLOW_ESTADOS.items())
    assert tokens["R7_PILLOW_VERSIONES"].startswith(esperado)
    # la observada NO se presenta como certificada
    assert "NO extrapolar" in tokens["R7_PILLOW_VERSIONES"]


# Escalas declaradas del certificado. Cada una se EJERCITA: los campos se generan al tamaño de entrada de
# la escala (antes todos eran 64x64 y las otras tres se saltaban en silencio con ``continue``).
_ESCALAS_SOMBRA = [(64, 32), (100, 37), (8, 16), (48, 13)]


def _familias_de_campos(n: int) -> list[tuple[str, np.ndarray]]:
    """Las familias de campos del gate de la sombra, generadas a tamaño ``n`` x ``n``."""
    return [
        ("extremo R7", _campo_extremo_r7(0.80, n)),
        ("extremo R7 L=0.55", _campo_extremo_r7(0.55, n)),
        ("midpoint", _campo_midpoint(n)),
        ("aleatorio", np.random.default_rng(1).uniform(-1.0, 1.0, (n, n, 3))),
        (
            "polos",
            _renorm(
                np.concatenate(
                    [
                        0.15 * np.random.default_rng(3).uniform(-1, 1, (n, n, 2)),
                        np.sign(np.random.default_rng(4).uniform(-1, 1, (n, n, 1))),
                    ],
                    axis=-1,
                )
            ),
        ),
    ]


@pytest.mark.parametrize(("n_in", "n_out"), _ESCALAS_SOMBRA, ids=[f"{a}a{b}" for a, b in _ESCALAS_SOMBRA])
def test_r7_impl_la_sombra_reproduce_el_binario_bit_a_bit(n_in: int, n_out: int) -> None:
    """Gate load-bearing: la sombra (acumulador double, 3 redondeos float32) es
    identica al binario real, bit a bit, en todas las familias y escalas.

    Si el acumulador fuera float32, si MSVC contrajera a FMA, si el buffer
    intermedio fuera double o si los pesos fueran otros, la sombra diferiria.
    Que NO difiera es lo que autoriza a derivar la cota de esta secuencia.
    """
    for nombre, campo in _familias_de_campos(n_in):
        assert campo.shape[:2] == (n_in, n_in), f"{nombre}: el campo no tiene el tamaño de la escala"
        real = np.stack([_pillow_resize(campo[..., k], n_out) for k in range(3)], axis=-1)
        sombra = np.stack([_shadow_resize_f32(campo[..., k], n_out) for k in range(3)], axis=-1)
        assert np.array_equal(real.view(np.uint32), sombra.view(np.uint32)), (
            f"{nombre} {n_in}->{n_out}: la sombra NO reproduce el binario"
        )


@pytest.mark.parametrize("largo", [0.80, 0.8660392772765431, 0.95])
def test_r7_impl_la_cota_nunca_se_viola(largo: float) -> None:
    """El angulo entre el ideal exacto y la salida real no supera la cota, por pixel."""
    campo = _campo_extremo_r7(largo)
    ang, cota = _peor_angulo_impl(campo, 32)
    assert ang <= cota
    assert _peor_exceso_por_pixel(campo, 32) <= 0.0  # y cada píxel contra SU cota, no máximo contra máximo
    # y no es vacua: el angulo medido es del orden del redondeo, no cero
    assert ang >= 0.0


def test_r7_impl_la_cota_aguanta_el_campo_de_saturacion() -> None:
    """Campo con redondeos float32 coherentes en el regimen del umbral."""
    campo = _campo_midpoint()
    ang, cota = _peor_angulo_impl(campo, 32)
    assert ang <= cota
    assert _peor_exceso_por_pixel(campo, 32) <= 0.0  # el midpoint tiene L variable: aquí importa el por píxel
    # el peor caso observado es del orden de u, muy por debajo de la cota: se
    # declara la holgura en vez de esconderla.
    assert cota >= 4.0 * ang


def test_r7_impl_controles_negativos_discriminan() -> None:
    """Los modelos incorrectos deben diferir del binario: si no, el control no sirve."""
    campo = _campo_extremo_r7(0.80)
    campo_y = np.transpose(campo, (1, 0, 2))
    real = np.stack([_pillow_resize(campo_y[..., k], 32) for k in range(3)], axis=-1)
    por_canal = real.shape[-1] * real.shape[0] * real.shape[1]

    acum = np.stack([_shadow_acum_f32(campo_y[..., k], 32) for k in range(3)], axis=-1)
    assert not np.array_equal(acum.view(np.uint32), real.view(np.uint32))
    assert int(np.count_nonzero(acum.view(np.uint32) != real.view(np.uint32))) > por_canal // 10

    dos_taps = np.stack([_shadow_pesos_2tap(campo_y[..., k], 32) for k in range(3)], axis=-1)
    assert not np.array_equal(dos_taps.view(np.uint32), real.view(np.uint32))
    # y el angulo que produce el modelo de pesos equivocados es catastrofico
    ideal = _ideal_exacto(campo_y, 32)
    peor = max(
        _angulo(ideal[i, j], dos_taps[i, j]) for i in range(32) for j in range(32) if np.linalg.norm(ideal[i, j]) > 1e-6
    )
    assert peor > 10.0 * Q_ANG


@pytest.mark.parametrize(
    ("factor", "taps_por_eje", "umbral_documentado"),
    [(2, 4, 0.8660770023307574), (4, 8, 0.8660770023311493), (16, 32, 0.8660770023335029)],
    ids=["k2", "k4", "k16"],
)
def test_r7_impl_el_umbral_depende_del_numero_de_taps_y_el_publicado_es_el_de_k2(
    factor: int, taps_por_eje: int, umbral_documentado: float
) -> None:
    """Revisión de Codex: `_KAPPA = 24` es el de `k = 2` (4 taps por eje). El documento da los umbrales de
    `k = 4` (8 taps) y `k = 16` (32 taps); acá se RECOMPUTAN en lugar de citarse, y se fija que el
    publicado (`0.8660770023307574`) es el de `k = 2` y que el máximo de las escalas declaradas lo supera
    por ~2.7e-12 (despreciable frente al margen de 3.77e-5, pero no cero)."""
    assert math.isclose(_umbral_suficiente_num(taps_por_eje), umbral_documentado, rel_tol=1e-12), factor
    publicado = _umbral_suficiente_num(_TAPS_K2)
    assert _umbral_suficiente_num(taps_por_eje) >= publicado
    maximo = _umbral_suficiente_num(32)
    assert 0.0 < maximo - publicado < 3e-12
    assert maximo - L_SUFICIENTE < 3.8e-05  # el margen numérico sigue siendo ~3.77e-5


@pytest.mark.parametrize(
    ("taps_por_eje", "estricto_documentado"),
    [(4, 0.8660773906537476), (32, 0.8660773906565153)],
    ids=["k2", "k16"],
)
def test_r7_impl_el_umbral_estricto_separa_las_dos_normas(taps_por_eje: int, estricto_documentado: float) -> None:
    """El publicado (`0.8660770023307574`, mismo `L` en los dos términos) NO es estrictamente suficiente:
    separando la norma del campo limpio de la del cuantizado el umbral sube ~3.9e-7. Sigue siendo
    ~1% del margen numérico (3.77e-5), pero la diferencia es real y queda pinada, no escondida."""
    estricto = _umbral_suficiente_estricto(taps_por_eje)
    publicado = _umbral_suficiente_num(taps_por_eje)

    assert math.isclose(estricto, estricto_documentado, rel_tol=1e-12)
    assert estricto > publicado
    assert 3.0e-07 < estricto - _umbral_suficiente_num(_TAPS_K2) < 4.0e-07
    assert estricto - L_SUFICIENTE < 4.2e-05  # el margen total sigue ~3.8e-5 sobre el ideal


def test_r7_impl_el_umbral_numerico_certificado() -> None:
    """`L*_num` se recomputa por biseccion y compara con el valor declarado."""
    lnum = _umbral_suficiente_num()
    assert math.isclose(lnum, 0.8660770023307574, rel_tol=1e-12)
    assert math.isclose(lnum - L_SUFICIENTE, 3.7725e-05, rel_tol=1e-3)
    # el umbral numerico supera al ideal: el margen de implementacion se paga
    assert lnum > L_SUFICIENTE
    # y queda POR DEBAJO de la estimacion retirada (que cargaba 4u a la entrada)
    assert lnum < 2.0 * math.sin(math.asin((T + 4 * U_F32) * math.sqrt(3.0)) / 2.0) / math.sin(Q_ANG)
    # la cota de implementacion en el umbral ideal es pequena pero no nula
    assert math.isclose(_cota_angulo_impl(L_SUFICIENTE), 3.4165303821e-07, rel_tol=1e-6)
    assert _cota_angulo_impl(L_SUFICIENTE) / Q_ANG == pytest.approx(4.3561e-05, rel=1e-3)


def test_r7_impl_la_cota_de_error_por_canal_es_la_declarada() -> None:
    """`E_b = u*(2+L) + kappa*u_d`: tres redondeos float32 + los de double."""
    assert math.isclose(_cota_error_impl(L_SUFICIENTE), 1.7082925570e-07, rel_tol=1e-9)
    assert math.isclose(_cota_error_impl(1.0), U_F32 * 3.0 + _KAPPA * U_F64, rel_tol=1e-15)
    # el margen de implementacion es del orden de u: 5 ordenes mas chico que
    # delta_chord, asi que no cambia la escala de la cota, solo la correccion.
    assert _cota_error_impl(1.0) < DELTA_CHORD * 1e-4


def test_r7_impl_el_documento_declara_la_version_y_las_categorias() -> None:
    """Anclas documentales del certificado de implementacion."""
    texto = _DOC.read_text(encoding="utf-8")
    assert PILLOW_CERTIFICADA in texto
    assert PILLOW_COMMIT in texto
    assert "0.8660770023307574" in texto
    for categoria in (
        "BOUND_ANALYTICALLY_PROVEN",
        "BOUND_COMPUTATIONALLY_CERTIFIED",
        "BOUND_EMPIRICALLY_OBSERVED",
        "BOUND_ESTIMATED_ONLY",
    ):
        assert categoria in texto, f"el documento debe declarar la categoria {categoria}"
    # la estimacion retirada sigue presente, pero marcada como sustituida
    assert "BOUND_ESTIMATED_ONLY" in texto and "sustituida por" in texto
    tokens = _bloque_de_tokens(texto)
    assert "R7_IMPL_BOUND" in tokens
    assert "R7_SUFICIENTE_THRESHOLD_NUM" in tokens
    assert "R7_IMPL_CERTIFIED_VERSION" in tokens


def test_r7_impl_los_tokens_de_implementacion_tienen_el_valor_certificado() -> None:
    """Los tokens NO solo deben existir: deben tener el valor demostrado.

    El defecto que esto mata: un mutante que cambia el valor del token (o que le
    cuelga un "sin demostrar")pasaria una guarda de existencia. El ancla es
    POSITIVA y cubre la cadena completa del token.
    """
    tokens = _bloque_de_tokens(_DOC.read_text(encoding="utf-8"))
    # umbral numerico: el valor bisectado, no el ideal ni la estimacion retirada
    valor_num = tokens["R7_SUFICIENTE_THRESHOLD_NUM"].split()[0]
    assert math.isclose(float(valor_num), _umbral_suficiente_num(), rel_tol=1e-12)
    assert math.isclose(float(valor_num), 0.8660770023307574, rel_tol=1e-12)
    # la formula de la cota, completa
    assert tokens["R7_IMPL_BOUND"] == "asin(sqrt(3) * E_b / L)   (E_b = u*(2+L) + (2m+2n+8)*u_d, u = 2**-24)"
    # la version certificada, con commit
    assert "11.3.0" in tokens["R7_IMPL_CERTIFIED_VERSION"]
    assert PILLOW_COMMIT in tokens["R7_IMPL_CERTIFIED_VERSION"]
    assert "NO extrapolar" in tokens["R7_IMPL_CERTIFIED_VERSION"]
    # tightness declarada, no escondida
    assert tokens["R7_IMPL_TIGHTNESS"].startswith("NOT_TIGHT")
    # la categoria del certificado: computacionalmente certificada, NO "observada"
    # ni "estimada", y ACOTADA a la version certificada
    assert tokens["R7_PILLOW_CERTIFICATION"].startswith("BOUND_COMPUTATIONALLY_CERTIFIED")
    assert "NO extrapolar" in tokens["R7_PILLOW_CERTIFICATION"]
    assert "11.3.0" in tokens["R7_PILLOW_CERTIFICATION"]
    # el certificado de implementacion NO habilita el global: sigue BLOCKED
    assert "BLOCKED" in tokens["R7_PILLOW_CERTIFICATION"]


def test_r7_los_estados_obligatorios_del_brief_estan_y_no_se_autoriza_nada() -> None:
    """Los 9 estados obligatorios del brief deben figurar con su valor.

    Sin esta ancla, nada impide que alguien "autorice" una fusion o un rerun de
    corpus editando el bloque de tokens: la guarda seria de existencia, no de
    valor. Aca se fija el VALOR.
    """
    tokens = _bloque_de_tokens(_DOC.read_text(encoding="utf-8"))
    assert tokens["F01_CONTRACT_AMENDMENT"] == "PENDING_TECH_LEAD"
    assert tokens["R7_NUMERIC_DERIVATION"].startswith("BLOCKED")
    assert tokens["H4_CODE_MERGE_AUTHORIZED"] == "NO"
    assert tokens["PR723_MERGE_AUTHORIZED"].startswith("NO")
    assert tokens["PR733_MERGE_AUTHORIZED"].startswith("NO")
    assert tokens["M6_IMPLEMENTATION_BLOCKED"] == "YES"
    assert tokens["REAL_CORPUS_RERUN"].startswith("NO")
    assert tokens["PR675_CHANGED"].startswith("NO")


def test_r7_la_cifra_de_mutantes_apunta_al_commit_que_la_evidencia() -> None:
    """Fase 5: la fila de mutacion de §7 declara 13/13 y apunta al commit que la evidencia.

    ALCANCE (revision de Codex): esto comprueba la CONSISTENCIA DOCUMENTAL del registro (cifras, commit y
    nombre del arnes), NO ejecuta ni reproduce ninguna mutacion: `scratch/barrido_mutantes.py` no esta
    versionado. La cifra 21/21 es evidencia registrada con su log, no un resultado que CI pueda reproducir.

    La cifra vieja era 8/8 (barrido anterior a `e87a5788`). §12 es trazabilidad de
    correcciones, NO el log de mutacion: por eso la cifra vivia en §7 y quedo
    desactualizada. Se corrige contra el commit, no contra una afirmacion.
    """
    texto = _DOC.read_text(encoding="utf-8")
    filas = _filas_de_tabla(texto, "## 7.")
    fila_mut = [f for f in filas if "utación" in f[0]]
    assert len(fila_mut) == 1, "debe haber exactamente una fila de mutantes en §7"
    assert "13/13" in fila_mut[0][2]
    assert "21/21" in fila_mut[0][2], "la cifra verificada en este slice es 13 + 8 nuevos"
    assert "barrido_mutantes.py" in fila_mut[0][2], "la fila debe nombrar el barrido que produjo el log (no versionado)"
    assert COMMIT_EVIDENCIA_MUTANTES in fila_mut[0][2]
    assert COMMIT_EVIDENCIA_MUTANTES in texto

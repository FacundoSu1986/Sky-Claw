"""H4 `resize_normal` — corrección float fail-closed (Slice 2, issue #721).

Familias A–F del preflight `docs/design/research/native-parallax/h4-resize-normal-preflight.md`
§8 y del contrato `h4-resize-normal-authorization-gate.md` §§3.4/3.4.1/5.3/5.4/6.1.

  A — Error de cuantización (RED obligatorio contra el histórico)
  B — Contrato geométrico, envolvente estructural y camino identidad
  C — Interpolación contra un oráculo INDEPENDIENTE (no otra llamada a Pillow)
  D — Integración (`load_asset`, `resize_height`, resolución primaria)
  E — Ancla de enumeración por AST (censo + orden de handlers + allowlist histórica)
  F — Propagación fail-closed en M2/M3/M4/M5
  G — Derivación del umbral (H4-F02), independiente del valor de producción

**H4-F01 — dominio geométrico del camino identidad.** El camino identidad medía CERO
normas: publicaba el vector nulo, un campo de norma 0.5 o `(1,1,1)` de norma `sqrt(3)`
como si fueran evaluables, declarando `degenerate=False`. §3.2 (salida: norma `≈ 1` o
`NON_EVALUABLE`) no tiene excepción de identidad. La familia B ancla las cuatro clases
discriminatorias —cero, norma 0.5, unitaria válida, clip de `1000.0`— en **las dos
superficies** (fachada y API diagnóstica), y exige simetría con el hermano `resize`.
La adjudicación entre §3.2 y §3.4.1 paso (4) está registrada en la producción
(`UNIT_NORM_TOLERANCE`).

**H4-F02 — el umbral es heurística, no garantía.** `5/16` es `sum_k w_k^2`, no
`sqrt(sum_k w_k^2)` (que vale `0.5590`), y sale de una propagación **RMS**, que no es
cota superior de error angular: el peor caso de la misma celda es `(q/2)*sqrt(3)`,
`2.771x` mayor. La familia G **deriva su propio cruce** con oráculo, semilla y factores
propios (sin leer `DEGENERATE_THRESHOLD` para derivarlo) y verifica lo único que se
sostiene: que el valor de producción es conservador en `k in {2, 4, 8, 16}`.

**Por qué los símbolos nuevos se acceden como `ad.X`.** El contrato #717 introduce
`DEGENERATE_THRESHOLD`, `NormalResizeResult`, `resize_normal_diagnostic`,
`DegenerateNormalError` e `InvalidNormalShapeError`, que **no existen** en el baseline
histórico. Con `from ... import X` el módulo de tests no colecciona y el rojo se pierde en un
error de importación. Accediendo por atributo, cada test **falla en su propia aserción** contra
el baseline (evidencia RED por test) y pasa contra la implementación corregida.

Sin red, sin corpus, sin escritura. Sólo campos sintéticos en memoria.
"""

from __future__ import annotations

import ast
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from PIL import Image

from sky_claw.local.native_parallax.research import authored_dataset as ad
from sky_claw.local.native_parallax.research.authored_dataset import (
    DatasetInvalidError,
    resize_height,
    resize_normal,
)

_REPO = Path(__file__).resolve().parents[1]
_PRODUCCION = _REPO / "sky_claw" / "local" / "native_parallax" / "research"

# ---------------------------------------------------------------------------
# Constantes del contrato
# ---------------------------------------------------------------------------

# Cuanto de la retícula del decodificador 8-bit (`decode_normal_image`: rgb/255*2-1).
Q8 = 2.0 / 255.0
# Tolerancia de ida/vuelta float32 -> float64 (el pipeline pasa por modo F).
TOL_F32 = 1e-6


# ---------------------------------------------------------------------------
# Oráculo INDEPENDIENTE: reducción separable bilineal en NumPy float64
# ---------------------------------------------------------------------------
def _coeficientes(n_in: int, n_out: int) -> list[tuple[int, NDArray[np.float64]]]:
    """Coeficientes por píxel de salida del filtro triangular separado.

    Modelo derivado de la respuesta a impulsos del filtro (validado contra él en
    `test_c_el_oraculo_independiente_coincide_con_el_resizer`):

        centro      = (i + 0.5) * escala - 0.5
        filterscale = max(1, escala)
        w(x)        = max(0, 1 - |x - centro| / filterscale), normalizado
    """
    escala = n_in / n_out
    filterscale = max(1.0, escala)
    filas: list[tuple[int, NDArray[np.float64]]] = []
    for i in range(n_out):
        centro = (i + 0.5) * escala - 0.5
        x0 = max(0, math.floor(centro - filterscale + 0.5))
        x1 = min(n_in, math.ceil(centro + filterscale + 0.5))
        xs = np.arange(x0, x1)
        w = np.maximum(0.0, 1.0 - np.abs(xs - centro) / filterscale)
        filas.append((x0, w / w.sum()))
    return filas


def _eje(a: NDArray[np.float64], n_out: int, eje: int) -> NDArray[np.float64]:
    b = np.moveaxis(a, eje, 0)
    filas = _coeficientes(b.shape[0], n_out)
    out = np.zeros((n_out,) + b.shape[1:], dtype=np.float64)
    for i, (x0, w) in enumerate(filas):
        out[i] = np.tensordot(w, b[x0 : x0 + len(w)], axes=(0, 0))
    return np.moveaxis(out, 0, eje)


def _oraculo(n: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    """Reducción separable float64 — NO llama a Pillow."""
    return _eje(_eje(np.asarray(n, dtype=np.float64), size, 0), size, 1)


# ---------------------------------------------------------------------------
# Campos sintéticos
# ---------------------------------------------------------------------------
def _unitaria(v: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.asarray(v / np.linalg.norm(v, axis=-1, keepdims=True), dtype=np.float64)


def _normal_plana(size: int) -> NDArray[np.float64]:
    n = np.zeros((size, size, 3), dtype=np.float64)
    n[..., 2] = 1.0
    return n


def _campo_de_norma(size: int, largo: float) -> NDArray[np.float64]:
    """Campo constante `(0, 0, largo)`: dirección confiable, norma exactamente `largo`."""
    n = np.zeros((size, size, 3), dtype=np.float64)
    n[..., 2] = largo
    return n


def _normal_sub_lsb(size: int) -> NDArray[np.float64]:
    """Componente `1/(4*255)`: por debajo del LSB de la retícula uint8 del histórico."""
    nx = 1.0 / (4 * 255)
    n = np.zeros((size, size, 3), dtype=np.float64)
    n[..., 0] = nx
    n[..., 2] = math.sqrt(1.0 - nx * nx)
    return n


def _normal_simetrica(size: int) -> NDArray[np.float64]:
    x = np.arange(size) / size
    xx, yy = np.meshgrid(x, x, indexing="xy")
    nx = 0.5 * np.sin(2 * np.pi * 2 * xx)
    ny = 0.5 * np.cos(2 * np.pi * 3 * yy)
    nz = np.sqrt(np.maximum(1.0 - nx**2 - ny**2, 0.0))
    return _unitaria(np.stack([nx, ny, nz], axis=-1))


def _normal_nyquist(size: int) -> NDArray[np.float64]:
    """Normales unitarias `(+-1, 0, 0)` alternadas: cancelación exacta a factor 2."""
    s = np.where(np.arange(size) % 2 == 0, 1.0, -1.0)
    n = np.zeros((size, size, 3), dtype=np.float64)
    n[..., 0] = s[None, :]
    return n


def _normal_por_largo(size: int, largo: float) -> NDArray[np.float64]:
    """Campo unitario cuyo vector interpolado (factor 2) tiene norma `largo` en el interior.

    `n(i,j) = (sin(theta)*s(i), 0, cos(theta))` con `s` alternada. El kernel 1-D a
    factor 2 suma cero sobre `s`, así que `v = (0, 0, cos(theta))` y `largo = cos(theta)`
    en el interior. En el borde el kernel se recorta y `largo` sube — el test lo tiene en
    cuenta: basta UN píxel por debajo del umbral para declarar el campo degenerado.
    """
    theta = math.acos(largo)
    s = np.where(np.arange(size) % 2 == 0, 1.0, -1.0)
    n = np.zeros((size, size, 3), dtype=np.float64)
    n[..., 0] = (math.sin(theta) * s)[None, :]
    n[..., 2] = math.cos(theta)
    return n


# ---------------------------------------------------------------------------
# Referencia del camino histórico (el defecto que se está corrigiendo)
# ---------------------------------------------------------------------------
def _camino_historico(n: NDArray[np.float64], size: int) -> NDArray[np.float64]:
    """Copia del `resize_normal` retirado (baseline `da738b75`, líneas 237-253).

    Se conserva como *régimen defectuoso* explícito: los tests de familia A exigen que
    esta referencia realmente reproduzca el sesgo, y sólo entonces comparan contra el
    camino corregido. Si la referencia dejara de ser defectuosa, la aserción lo delata
    en vez de dejar pasar una comparación vacía.

    `Image.fromarray` sin `mode=` infiere `L` para un array 2-D `uint8`, que es
    exactamente lo que hacía el original (`mode="L"`), sin el `DeprecationWarning`.
    """
    canales = []
    for c in range(3):
        im = Image.fromarray((np.clip(n[..., c], -1.0, 1.0) * 127.5 + 127.5).astype(np.uint8))
        canales.append(
            (np.asarray(im.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float64) - 127.5) / 127.5
        )
    out = np.stack(canales, axis=-1)
    return out / np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-12)


# ===========================================================================
# Familia A — error de cuantización (RED obligatorio contra el histórico)
# ===========================================================================
def test_a_normal_plana_no_reintroduce_el_sesgo_xy() -> None:
    """El histórico sesga `mean(nx) = mean(ny) = -0.5 LSB` (medido -0.0039215)."""
    out = resize_normal(_normal_plana(16), 8)
    assert abs(float(out[..., 0].mean())) < TOL_F32, f"sesgo residual en nx: {out[..., 0].mean()}"
    assert abs(float(out[..., 1].mean())) < TOL_F32, f"sesgo residual en ny: {out[..., 1].mean()}"
    np.testing.assert_allclose(out[..., 2], 1.0, atol=TOL_F32)


def test_a_componente_sub_lsb_conserva_el_signo() -> None:
    """`1/(4*255)` es sub-LSB: el histórico la trunca a byte 127 y le invierte el signo."""
    n = _normal_sub_lsb(16)
    out = resize_normal(n, 8)
    assert float(out[0, 0, 0]) > 0.0, f"signo invertido: {out[0, 0, 0]}"
    np.testing.assert_allclose(out[..., 0], 1.0 / (4 * 255), atol=TOL_F32)


def test_a_campo_simetrico_no_tiene_sesgo() -> None:
    """El sesgo de cuantización debe desaparecer — medido contra el régimen histórico.

    No se fija una tolerancia a ojo: se miden LOS DOS regímenes en la misma corrida y se
    exigen tres cosas independientes.

    (1) **Aserción load-bearing**: el camino histórico DEBE sesgar (`> 1e-3`). Si no lo
        hiciera, la comparación sería vacía y el test lo delata en vez de pasar.
    (2) El camino corregido reduce ese sesgo **al menos 5x**. Medido en esta máquina:
        `nx` 148x (`1.63e-5` vs `-2.42e-3`), `ny` 11.8x (`-2.80e-4` vs `-3.30e-3`). El
        margen se deja en 5x —y no en 10x— porque (1)+(3) ya cargan el peso científico
        y una guarda robusta no debe depender del camino interno de Pillow. Un mutante
        que reintroduzca cuantización lleva el cociente a ~1 y falla.
    (3) **El residual es del campo, no del código**: `mean(ny) = -2.80e-4` sobrevive al
        fix, y es idéntico en el oráculo float64 puro porque el kernel de borde recortado
        aplicado a `cos(2*pi*3*y)` no conserva la media nula. Sin este anclaje el
        residual parecería un sesgo residual del fix.
    """
    n = _normal_simetrica(64)
    out = resize_normal(n, 32)
    hist = _camino_historico(n, 32)
    orac = _oraculo(n, 32)
    orac = orac / np.linalg.norm(orac, axis=-1, keepdims=True)

    for c, eje in ((0, "nx"), (1, "ny")):
        b_corr = abs(float(out[..., c].mean()))
        b_hist = abs(float(hist[..., c].mean()))
        assert b_hist > 1e-3, f"{eje}: la referencia histórica no reproduce el sesgo ({b_hist:.3e})"
        assert b_corr * 5.0 < b_hist, f"{eje}: corregido {b_corr:.3e} vs histórico {b_hist:.3e} (<5x)"
        assert abs(float(out[..., c].mean()) - float(orac[..., c].mean())) < TOL_F32, (
            f"{eje}: el residuo no lo explica el oráculo independiente"
        )


def test_a_campo_constante_sobrevive_al_redondeo_float32() -> None:
    """Un campo constante no debe moverse más que el redondeo de ida/vuelta float32."""
    n = _normal_plana(32)
    out = resize_normal(n, 16)
    assert float(np.abs(out - n[0, 0]).max()) < TOL_F32, "el pipeline desplazó un campo constante"


def test_a_no_hay_reticula_uint8_intermedia() -> None:
    """Ninguna salida puede caer en la retícula `(k-127.5)/127.5` del camino uint8.

    Un valor intermedio de esa retícula (p. ej. `-1/255`) delata cuantización. Se
    elige un campo constante con una componente que NO pertenece a la retícula.
    """
    valor = 0.37  # no pertenece a {(k-127.5)/127.5}
    assert min(abs(valor - (k - 127.5) / 127.5) for k in range(256)) > 1e-3
    n = _unitaria(np.array([[[valor, 0.0, 1.0]] * 16] * 16, dtype=np.float64))
    out = resize_normal(n, 8)
    assert float(np.abs(out - n[0, 0]).max()) < TOL_F32, "la salida cayó en la retícula uint8"


# ===========================================================================
# Familia B — contrato geométrico, envolvente y camino identidad
# ===========================================================================
def test_b_norma_unitaria_bajo_tolerancia_derivada() -> None:
    out = resize_normal(_normal_simetrica(64), 32)
    normas = np.linalg.norm(out, axis=-1)
    assert float(np.abs(normas - 1.0).max()) < TOL_F32


def test_b_degenerado_lanza_degenerate_normal_error() -> None:
    """Cancelación exacta: la fachada NO puede devolver el array en silencio (RC-1)."""
    with pytest.raises(ad.DegenerateNormalError):
        resize_normal(_normal_nyquist(128), 64)


def test_b_degenerado_reporta_censo_total_y_umbral() -> None:
    with pytest.raises(ad.DegenerateNormalError) as exc:
        resize_normal(_normal_nyquist(128), 64)
    err = exc.value
    assert err.degenerate_count > 0
    assert err.total_pixels == 64 * 64
    assert err.threshold == ad.DEGENERATE_THRESHOLD
    assert err.fraction == pytest.approx(err.degenerate_count / err.total_pixels)
    assert err.asset_id is None  # la fachada no conoce el asset: degradación declarada


def test_b_degenerado_no_se_reemplaza_por_un_vector_inventado() -> None:
    """D-06: prohibido sustituir por `(0,0,1)`, devolver el nulo como válido, o inventar."""
    n = _normal_nyquist(64)
    diag = ad.resize_normal_diagnostic(n, 32)
    assert diag.degenerate is True
    # El array crudo puede contener el nulo; lo que NO puede es publicarse como normal.
    with pytest.raises(ad.DegenerateNormalError):
        resize_normal(n, 32)


def test_b_casi_nulo_bajo_el_umbral_es_non_evaluable() -> None:
    """Falsación, lado A: apenas por debajo del umbral -> NON_EVALUABLE.

    Alcance declarado (H4-F02): esto verifica la RAMA de clasificación, **no** el
    VALOR del umbral — lee `ad.DEGENERATE_THRESHOLD` de producción. La derivación
    independiente del número vive en la familia G.
    """
    largo = ad.DEGENERATE_THRESHOLD * 0.96
    diag = ad.resize_normal_diagnostic(_normal_por_largo(128, largo), 64)
    assert diag.degenerate is True, f"norma objetivo {largo} no fue detectada como degenerada"
    assert diag.degenerate_count >= 1


def test_b_justo_sobre_el_umbral_es_evaluable() -> None:
    """Falsación, lado B: apenas por encima del umbral -> evaluable y unitario.

    Alcance declarado (H4-F02): igual que el lado A, verifica la rama y no el valor.
    """
    largo = ad.DEGENERATE_THRESHOLD * 1.02
    n = _normal_por_largo(128, largo)
    diag = ad.resize_normal_diagnostic(n, 64)
    assert diag.degenerate is False, f"norma objetivo {largo} fue rechazada"
    assert diag.degenerate_count == 0
    assert float(np.abs(np.linalg.norm(diag.array, axis=-1) - 1.0).max()) < TOL_F32


def test_b_el_umbral_reportado_es_el_usado() -> None:
    diag = ad.resize_normal_diagnostic(_normal_plana(32), 16)
    assert diag.threshold == ad.DEGENERATE_THRESHOLD


def test_b_camino_identidad_preserva_el_noop() -> None:
    """D-07: `H == W == size` sobre entrada admisible -> sin interpolar ni renormalizar."""
    n = _normal_simetrica(32)
    out = resize_normal(n, 32)
    np.testing.assert_array_equal(out, n)


def test_b_identidad_fuera_de_rango_clipea_no_devuelve_crudo() -> None:
    """§3.4.1 + §3.2: `RANGO_ES_POSTCONDICION` vale en el camino identidad, y la salida
    sigue siendo **unitaria**: el clip no exime de la postcondición de norma.

    `1000.0` se clipea a `(1,1,1)` —norma `sqrt(3)`, no 1—, así que la salida publicada
    es `(1,1,1)/sqrt(3)`, el mismo resultado que da el hermano `resize` (H4-F01). La
    aserción previa (`max == min == 1.0`) anclaba la norma `sqrt(3)` como aceptable.
    """
    n = np.full((4, 4, 3), 1000.0, dtype=np.float64)
    out = resize_normal(n, 4)
    assert float(out.max()) <= 1.0
    assert float(out.min()) >= -1.0
    assert float(out.max()) < 1.0, "publicó la norma sqrt(3) del clip (H4-F01)"
    np.testing.assert_allclose(out, 1.0 / math.sqrt(3.0), atol=TOL_F32)
    np.testing.assert_allclose(np.linalg.norm(out, axis=-1), 1.0, atol=TOL_F32)


def test_b_identidad_no_finita_rechaza() -> None:
    for valor in (np.nan, np.inf, -np.inf):
        n = _normal_plana(4)
        n[0, 0, 0] = valor
        with pytest.raises(ad.InvalidNormalShapeError):
            resize_normal(n, 4)


@pytest.mark.parametrize("forma", [(4, 4, 4), (4, 4, 2), (4, 4, 1), (4, 4, 0), (4, 4), (4, 4, 3, 1)])
def test_b_identidad_forma_invalida_rechaza(forma: tuple[int, ...]) -> None:
    """F1: `ndim != 3` / `shape[-1] != 3` se rechazan; no se difieren por V-03."""
    n = np.zeros(forma, dtype=np.float64)
    with pytest.raises(ad.InvalidNormalShapeError):
        resize_normal(n, forma[0] if len(forma) > 1 else 4)


def test_b_identidad_dimension_vacia_rechaza() -> None:
    for forma in ((0, 4, 3), (4, 0, 3)):
        with pytest.raises(ad.InvalidNormalShapeError):
            resize_normal(np.zeros(forma, dtype=np.float64), 4)


@pytest.mark.parametrize("size", [0, -1, -64, 2.5, "4", None, True])
def test_b_size_invalido_rechaza(size: object) -> None:
    with pytest.raises(ad.InvalidNormalShapeError):
        resize_normal(_normal_plana(8), size)  # type: ignore[arg-type]


def test_b_forma_malformada_no_es_degeneracion() -> None:
    """F1 y D-06 son señales distintas: una forma inválida no es una degeneración."""
    with pytest.raises(ad.InvalidNormalShapeError) as exc:
        resize_normal(np.zeros((4, 4, 2), dtype=np.float64), 4)
    assert not isinstance(exc.value, ad.DegenerateNormalError)


def test_b_sin_nan_inf_publicados() -> None:
    n = _normal_plana(32)
    n[3, 3, 1] = np.nan
    with pytest.raises(ad.InvalidNormalShapeError):
        resize_normal(n, 16)


def test_b_fuera_de_rango_finito_es_clip_no_rechazo() -> None:
    """N-11: `[-1,1]` es postcondición. Un valor finito fuera de rango se clipea.

    `nx = 1000.0` se clipea a `1.0` antes de interpolar; el campo queda `(1,0,1)`
    constante y la renormalización lo lleva a `(1/sqrt(2), 0, 1/sqrt(2))`.
    """
    n = _normal_plana(16)
    n[..., 0] = 1000.0
    out = resize_normal(n, 8)
    np.testing.assert_allclose(out[..., 0], 1.0 / math.sqrt(2.0), atol=TOL_F32)
    np.testing.assert_allclose(out[..., 2], 1.0 / math.sqrt(2.0), atol=TOL_F32)


def test_b_convencion_y_orden_de_canales_no_se_alteran() -> None:
    """M-03/M-04: no se invierte Y, no se permutan canales, no se reordena nada."""
    n = np.zeros((16, 16, 3), dtype=np.float64)
    n[..., 0] = 0.3
    n[..., 1] = 0.4
    n[..., 2] = 0.8660254037844386
    out = resize_normal(n, 8)
    assert float(out[..., 0].mean()) > 0.0
    assert float(out[..., 1].mean()) > 0.0
    assert float(out[..., 2].mean()) > 0.0
    np.testing.assert_allclose(out[..., 0], n[0, 0, 0], atol=TOL_F32)
    np.testing.assert_allclose(out[..., 1], n[0, 0, 1], atol=TOL_F32)


# ---------------------------------------------------------------------------
# H4-F01 — dominio geométrico del camino identidad, en LAS DOS superficies
# ---------------------------------------------------------------------------
def test_b_f01_el_cero_del_camino_identidad_es_non_evaluable() -> None:
    """H4-F01 (a): `L = 0` es degeneración (D-06), no un no-op.

    El bypass histórico devolvía el vector nulo declarando `degenerate=False`. Las dos
    superficies deben respetar la señal: diagnóstico con `degenerate=True` y censo
    completo; fachada que **lanza** en vez de publicar el nulo.
    """
    n = _campo_de_norma(4, 0.0)
    diag = ad.resize_normal_diagnostic(n, 4)
    assert diag.degenerate is True, "el camino identidad publicó un vector nulo como evaluable"
    assert diag.degenerate_count == 16
    assert diag.total_pixels == 16
    assert diag.threshold == ad.DEGENERATE_THRESHOLD
    with pytest.raises(ad.DegenerateNormalError) as exc:
        resize_normal(n, 4)
    assert exc.value.degenerate_count == 16


def test_b_f01_la_norma_no_unitaria_no_es_un_noop_valido() -> None:
    """H4-F01 (b): `L = 0.5` tiene dirección confiable pero **no** es un no-op válido.

    La versión previa anclaba `assert_array_equal(out, n)` para esta entrada,
    contradiciendo §3.2. La dirección es confiable (queda sobre el umbral), así que no
    es degeneración: se renormaliza, igual que en el hermano `resize`.
    """
    n = _campo_de_norma(8, 0.5)
    diag = ad.resize_normal_diagnostic(n, 8)
    assert diag.degenerate is False
    assert diag.degenerate_count == 0
    np.testing.assert_allclose(np.linalg.norm(diag.array, axis=-1), 1.0, atol=TOL_F32)
    out = resize_normal(n, 8)
    np.testing.assert_allclose(out, np.broadcast_to(np.array([0.0, 0.0, 1.0]), out.shape), atol=TOL_F32)
    assert not np.array_equal(out, n), "devolvió la entrada cruda de norma 0.5"


def test_b_f01_el_clip_no_publica_norma_sqrt3() -> None:
    """H4-F01 (d): `1000.0` -> clip `(1,1,1)`, norma `sqrt(3)`; la salida debe ser
    unitaria (o rechazo). Publicar `sqrt(3)` viola §3.2 y no lo hace el hermano."""
    n = np.full((4, 4, 3), 1000.0, dtype=np.float64)
    diag = ad.resize_normal_diagnostic(n, 4)
    assert diag.degenerate is False
    normas = np.linalg.norm(diag.array, axis=-1)
    assert float(np.abs(normas - 1.0).max()) < TOL_F32, f"norma publicada {float(normas.max()):.6f}"
    assert not math.isclose(float(normas.max()), math.sqrt(3.0), abs_tol=1e-3)
    np.testing.assert_allclose(np.linalg.norm(resize_normal(n, 4), axis=-1), 1.0, atol=TOL_F32)


def test_b_f01_la_entrada_unitaria_conserva_el_noop_exacto() -> None:
    """H4-F01 (c): la ÚNICA entrada para la que D-07 justifica el no-op bit a bit.

    `decode_normal_image` produce exactamente esto aguas arriba, así que el camino
    identidad del pipeline real sigue sin tocar un solo bit.
    """
    n = _normal_simetrica(32)
    assert float(np.abs(np.linalg.norm(n, axis=-1) - 1.0).max()) <= ad.UNIT_NORM_TOLERANCE
    np.testing.assert_array_equal(resize_normal(n, 32), n)
    np.testing.assert_array_equal(ad.resize_normal_diagnostic(n, 32).array, n)


def test_b_f01_identidad_y_resize_son_simetricos_en_la_norma() -> None:
    """§5.3.4.7 (simetría de hermanos): la misma entrada, por los dos caminos, debe
    publicar la misma postcondición de norma. El único paso que difiere es interpolar."""
    for largo in (0.4, 0.5, 1.0, 1.7):
        identidad = _campo_de_norma(8, largo)
        escalada = np.repeat(np.repeat(_campo_de_norma(4, largo), 2, axis=0), 2, axis=1)
        n_identidad = np.linalg.norm(resize_normal(identidad, 8), axis=-1)
        n_resize = np.linalg.norm(resize_normal(escalada, 4), axis=-1)
        np.testing.assert_allclose(n_identidad, 1.0, atol=TOL_F32, err_msg=f"identidad L={largo}")
        np.testing.assert_allclose(n_resize, 1.0, atol=TOL_F32, err_msg=f"resize L={largo}")


def test_b_f01_la_tolerancia_de_identidad_se_deriva_de_float32() -> None:
    """§3.5: la tolerancia del camino identidad sale de la cota **analítica** de la
    ida/vuelta `float32 -> float64`, no de ninguna medición del corpus."""
    cota_analitica = math.sqrt(3.0) * 2.0**-24
    assert cota_analitica < ad.UNIT_NORM_TOLERANCE <= 1e-5, (
        "la tolerancia debe cubrir la cota analítica sin inflarse hasta volverse inútil"
    )
    # Medición sintética independiente: el peor desvío real queda por debajo.
    rng = np.random.default_rng(20261012)
    v = rng.normal(size=(64, 64, 3))
    v = v / np.linalg.norm(v, axis=-1, keepdims=True)
    ida_vuelta = v.astype(np.float32).astype(np.float64)
    desvio = float(np.abs(np.linalg.norm(ida_vuelta, axis=-1) - 1.0).max())
    assert desvio < ad.UNIT_NORM_TOLERANCE, f"desvío medido {desvio:.3e} excede la tolerancia declarada"


# ===========================================================================
# Familia C — interpolación contra un oráculo independiente
# ===========================================================================
def test_c_el_oraculo_no_es_una_llamada_a_pillow() -> None:
    """El oráculo debe estar escrito aparte, no delegar en la misma primitiva."""
    fuente = Path(__file__).read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name in {"_oraculo", "_eje", "_coeficientes"}:
            nombres = {n.id for n in ast.walk(nodo) if isinstance(n, ast.Name)}
            assert "Image" not in nombres, f"{nodo.name} usa Pillow: no es un oráculo independiente"


def test_c_el_oraculo_independiente_coincide_con_el_resizer() -> None:
    """El resizer corregido y la referencia NumPy float64 coinciden dentro de float32."""
    for size_in, size_out in ((128, 64), (128, 32), (100, 37), (64, 16)):
        x = np.linspace(0.0, 2 * np.pi, size_in, endpoint=False)
        xx, yy = np.meshgrid(x, x, indexing="xy")
        n = _unitaria(np.stack([0.3 * np.sin(xx), 0.3 * np.cos(yy), np.ones_like(xx)], axis=-1))
        esperado = _oraculo(n, size_out)
        obtenido = ad.resize_normal_diagnostic(n, size_out)
        assert obtenido.degenerate is False, f"{size_in}->{size_out} degeneró un campo suave"
        np.testing.assert_allclose(
            obtenido.array,
            _unitaria(esperado),
            atol=1e-5,
            err_msg=f"{size_in}->{size_out}",
        )


def test_c_campo_constante_es_constante() -> None:
    n = _normal_plana(64)
    out = resize_normal(n, 16)
    esperado = np.broadcast_to(n[0, 0], out.shape)
    np.testing.assert_allclose(out, esperado, atol=TOL_F32)


def test_c_campo_afin_se_reproduce_analiticamente() -> None:
    """Un filtro normalizado y simétrico reproduce funciones afines.

    Predicción analítica cerrada: el píxel de salida `i` muestrea la coordenada de
    entrada `(i + 0.5) * escala - 0.5`. El oráculo es la fórmula, no otra corrida.
    """
    size_in, size_out = 128, 32
    escala = size_in / size_out
    idx = np.arange(size_out)
    centros = (idx + 0.5) * escala - 0.5
    # Campo afín en x (no unitario a propósito: la reproducción afín es del filtro).
    n = np.zeros((size_in, size_in, 3), dtype=np.float64)
    n[..., 0] = np.linspace(-0.8, 0.8, size_in)[None, :]
    n[..., 2] = 1.0
    crudo = _oraculo(n, size_out)
    esperado = np.linspace(-0.8, 0.8, size_in)[np.clip(np.round(centros).astype(int), 0, size_in - 1)]
    # Se compara el oráculo contra la fórmula cerrada (error de discretización acotado).
    np.testing.assert_allclose(crudo[0, :, 0], esperado, atol=0.02)
    # Y el resizer real reproduce el mismo campo afín.
    obtenido = ad.resize_normal_diagnostic(n, size_out)
    assert obtenido.degenerate is False
    np.testing.assert_allclose(_unitaria(obtenido.array)[..., 0], _unitaria(crudo)[..., 0], atol=1e-4)


def test_c_patron_de_alta_frecuencia_no_publica_basura() -> None:
    """Contrato, no valor: o se rechaza, o la salida es finita y unitaria. Nunca basura."""
    rng = np.random.default_rng(3)
    v = rng.normal(size=(256, 256, 3))
    v[..., 2] = np.abs(v[..., 2]) + 0.2
    n = _unitaria(v)
    try:
        out = resize_normal(n, 64)
    except ad.DegenerateNormalError as exc:
        assert exc.degenerate_count > 0
        return
    assert np.all(np.isfinite(out))
    np.testing.assert_allclose(np.linalg.norm(out, axis=-1), 1.0, atol=TOL_F32)


# ===========================================================================
# Familia D — integración
# ===========================================================================
def test_d_load_asset_usa_la_api_de_diagnostico() -> None:
    fuente = (_PRODUCCION / "authored_dataset.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "load_asset")
    llamados = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "resize_normal_diagnostic" in llamados, (
        "load_asset debe recibir la evidencia por la API de diagnóstico, no por la fachada"
    )


def test_d_load_asset_propaga_degeneracion_con_identidad_del_asset(tmp_path: Path) -> None:
    """El error de `load_asset` lleva el `asset_id`: la fachada no puede, esta ruta sí.

    El bitmap alterna `(255,0,0)` / `(0,255,255)`, cuyos decodificados son antípodas
    exactas `(1,-1,-1)/sqrt(3)` y `(-1,1,1)/sqrt(3)`: el kernel a factor 2 los cancela.
    """
    import json

    from PIL import Image

    from sky_claw.local.native_parallax.research.authored_dataset import MaterialSpec, _sha256

    ancho = np.where(np.arange(64) % 2 == 0, 255, 0).astype(np.uint8)[None, :]
    normal = np.zeros((64, 64, 3), dtype=np.uint8)
    normal[..., 0] = ancho
    normal[..., 1] = 255 - ancho
    normal[..., 2] = 255 - ancho
    ruta_n = tmp_path / "n.png"
    Image.fromarray(normal).save(ruta_n)
    ruta_h = tmp_path / "h.png"
    Image.fromarray(np.zeros((64, 64), dtype=np.uint8)).save(ruta_h)

    spec = MaterialSpec(
        asset_id="asset-degenerado",
        family="brick",
        source="ambientCG",
        mirror="ambientCG",
        license="CC0-1.0",
        license_url="https://example.invalid",
        source_url="https://example.invalid",
        normal_path=str(ruta_n),
        height_path=str(ruta_h),
        normal_sha256=_sha256(ruta_n),
        height_sha256=_sha256(ruta_h),
        native_resolution=(64, 64),
        declared_convention="OPENGL",
        height_semantics="UNKNOWN_SCALE_RELATIVE_GRAYSCALE",
    )
    with pytest.raises(ad.DegenerateNormalError) as exc:
        ad.load_asset(spec, 32)
    assert exc.value.asset_id == "asset-degenerado"
    assert exc.value.total_pixels == 32 * 32
    assert json.dumps(exc.value.fraction)  # fracción serializable, no un NaN


def test_d_resize_height_no_regresiona() -> None:
    """M-07 / #653: el hermano escalar conserva modo F, sin uint8, sin clip en resize."""
    ramp = np.tile(np.linspace(0.0, 1.0, 1024, dtype=np.float64), (1024, 1))
    out = resize_height(ramp, 512)
    assert out.shape == (512, 512)
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0
    # 16-bit: una rampa de 1024 niveles no debe colapsar a 256 niveles.
    assert len(np.unique(np.round(out[0, :], 6))) > 256
    constante = np.full((32, 32), 0.375, dtype=np.float64)
    np.testing.assert_allclose(resize_height(constante, 16), 0.375, atol=1e-7)


def test_d_la_resolucion_primaria_del_runner_sigue_siendo_512() -> None:
    fuente = (_PRODUCCION / "run_exp_m3.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    defaults = {
        n.args[0].value
        for n in ast.walk(arbol)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "add_argument"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "--resolution"
    }
    assert defaults == {"--resolution"}
    assert "default=512" in fuente.replace(" ", ""), "PRIMARY_RESOLUTION = 512 no debe cambiar"


# ===========================================================================
# Familia E — ancla de enumeración por AST
# ===========================================================================
_SIMBOLOS = ("resize_normal", "resize_normal_diagnostic", "load_asset")

_CENSO_PRODUCCION_ESPERADO = frozenset(
    {
        # `authored_dataset` ya no referencia `resize_normal` como `ast.Name`: la fachada
        # define la función y delega en `resize_normal_diagnostic`. El censo sigue
        # cubriendo el módulo — por la superficie nueva, que es la que el contrato §5.4
        # manda enumerar ("o a la varita `resize_normal_diagnostic`").
        ("sky_claw/local/native_parallax/research/authored_dataset.py", "resize_normal_diagnostic"),
        ("sky_claw/local/native_parallax/research/run_exp_m2.py", "load_asset"),
        ("sky_claw/local/native_parallax/research/run_exp_m3.py", "load_asset"),
        ("sky_claw/local/native_parallax/research/run_exp_m4.py", "load_asset"),
        ("sky_claw/local/native_parallax/research/run_exp_m5.py", "load_asset"),
    }
)

_ALLOWLIST_HISTORICA = frozenset(
    {
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/scripts/h4_magnitude_probe.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/scripts/phase_a_synthetic.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/scripts/phase_c2_h4_corpus.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/scripts/phase_c_real_impact.py",
            "load_asset",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/"
            "corrective-20261008/scripts/h4_magnitude_probe_corrective.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/"
            "corrective-20261008/scripts/phase_c2_h4_corpus_corrective.py",
            "resize_normal",
        ),
        (
            "docs/validation/native-parallax-h1-h5-falsification-impact-audit-20261007/"
            "corrective-20261008/scripts/phase_c_corrective.py",
            "load_asset",
        ),
    }
)

# Handlers de degeneración admitidos: ninguno puede `continue`.
_HANDLER_QUE_ABORTA = "DegenerateNormalError"


def _tracked_py() -> list[str]:
    listado = subprocess.run(["git", "ls-files", "*.py"], cwd=_REPO, capture_output=True, text=True, check=True)
    return [rel.strip() for rel in listado.stdout.splitlines() if rel.strip()]


def _censo_consumidores() -> dict[str, set[tuple[str, str]]]:
    """Censo por AST de referencias (`ast.Name`), no sólo llamadas (N-13)."""
    censo: dict[str, set[tuple[str, str]]] = {"produccion": set(), "historico": set()}
    for rel in _tracked_py():
        ruta = _REPO / rel
        if not ruta.is_file():
            continue
        try:
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        hallados = {n.id for n in ast.walk(arbol) if isinstance(n, ast.Name) and n.id in _SIMBOLOS}
        if not hallados:
            continue
        if rel.startswith("sky_claw/"):
            destino = "produccion"
        elif rel.startswith("docs/validation/"):
            destino = "historico"
        else:
            continue
        for simbolo in hallados:
            censo[destino].add((rel, simbolo))
    return censo


def _handlers_de_excepciones(rel: str) -> list[ast.ExceptHandler]:
    arbol = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
    return [n for n in ast.walk(arbol) if isinstance(n, ast.ExceptHandler)]


def _nombre_de_handler(handler: ast.ExceptHandler) -> str:
    tipo = handler.type
    if isinstance(tipo, ast.Name):
        return tipo.id
    if isinstance(tipo, ast.Tuple):
        return "+".join(t.id for t in tipo.elts if isinstance(t, ast.Name))
    return ""


def _contiene_continue(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Continue) for n in ast.walk(handler))


_LLAMADAS_QUE_CARGAN_NORMALES = {"load_asset", "resize_normal", "resize_normal_diagnostic"}


def _trys_que_cargan_normales(rel: str) -> list[ast.Try]:
    """`try` cuyo cuerpo carga una normal — las únicas superficies donde el orden importa."""
    arbol = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
    objetivo: list[ast.Try] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Try):
            continue
        llamados = {n.func.id for n in ast.walk(nodo) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if llamados & _LLAMADAS_QUE_CARGAN_NORMALES:
            objetivo.append(nodo)
    return objetivo


def _indices_de_handlers(t: ast.Try) -> list[str]:
    return [_nombre_de_handler(h) for h in t.handlers]


def test_e_el_censo_productivo_esta_congelado() -> None:
    censo = _censo_consumidores()
    assert censo["produccion"] == set(_CENSO_PRODUCCION_ESPERADO), (
        f"nuevos={sorted(censo['produccion'] - set(_CENSO_PRODUCCION_ESPERADO))} "
        f"faltantes={sorted(set(_CENSO_PRODUCCION_ESPERADO) - censo['produccion'])}"
    )


def test_e_la_allowlist_historica_es_exacta_y_de_siete() -> None:
    """N-13: SIETE invocadores en `docs/validation/`, comparados por conjunto exacto."""
    assert len(_ALLOWLIST_HISTORICA) == 7
    censo = _censo_consumidores()
    assert censo["historico"] == set(_ALLOWLIST_HISTORICA), (
        f"nuevos={sorted(censo['historico'] - set(_ALLOWLIST_HISTORICA))} "
        f"faltantes={sorted(set(_ALLOWLIST_HISTORICA) - censo['historico'])}"
    )


def test_e_ningun_handler_de_degeneracion_usa_continue() -> None:
    """D-06.e: `continue` está PROHIBIDO para la señal de degeneración."""
    ofensores: list[str] = []
    for rel in _tracked_py():
        if not rel.startswith("sky_claw/"):
            continue
        try:
            handlers = _handlers_de_excepciones(rel)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for handler in handlers:
            if _HANDLER_QUE_ABORTA in _nombre_de_handler(handler) and _contiene_continue(handler):
                ofensores.append(f"{rel}:{handler.lineno}")
    assert not ofensores, f"handlers de {_HANDLER_QUE_ABORTA} con `continue` (prohibido): {ofensores}"


def test_e_el_handler_de_degeneracion_precede_al_de_dataset_invalid() -> None:
    """Una subclase capturada después de su superclase es código muerto.

    El orden de las cláusulas es load-bearing: `DegenerateNormalError` hereda de
    `DatasetInvalidError`, así que si aparece después, el `continue` de integridad la
    absorbe y la degeneración desaparece sin señal. Se enumeran **todos** los `try`
    de producción que cargan una normal, no sólo el de M3.
    """
    revisados = 0
    for rel in sorted({r for r, _ in _CENSO_PRODUCCION_ESPERADO}):
        for t in _trys_que_cargan_normales(rel):
            nombres = _indices_de_handlers(t)
            if not any("DatasetInvalidError" in n for n in nombres):
                continue
            revisados += 1
            i_degen = next((k for k, n in enumerate(nombres) if _HANDLER_QUE_ABORTA in n), None)
            assert i_degen is not None, (
                f"{rel}:{t.lineno} carga una normal y captura DatasetInvalidError sin tratar "
                f"{_HANDLER_QUE_ABORTA} antes (handlers={nombres})"
            )
            assert i_degen < nombres.index("DatasetInvalidError"), (
                f"{rel}:{t.lineno} {_HANDLER_QUE_ABORTA} debe preceder a DatasetInvalidError (handlers={nombres})"
            )
    assert revisados >= 1, "no se encontró ningún try de producción que cargue normales"


def test_e_la_fachada_no_expone_un_array_degenerado_en_silencio() -> None:
    """RC-1: la fachada inspecciona el diagnóstico y lanza. No hay `return res.array` crudo."""
    fuente = (_PRODUCCION / "authored_dataset.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "resize_normal")
    tiene_guard = any(
        isinstance(n, ast.If) and any(isinstance(s, ast.Attribute) and s.attr == "degenerate" for s in ast.walk(n.test))
        for n in ast.walk(fn)
    )
    lanza = any(
        isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Call)
        and getattr(n.exc.func, "id", "") == _HANDLER_QUE_ABORTA
        for n in ast.walk(fn)
    )
    assert tiene_guard, "resize_normal debe inspeccionar `res.degenerate` (RC-1)"
    assert lanza, f"resize_normal debe lanzar {_HANDLER_QUE_ABORTA} ante degeneración (RC-1)"


def test_e_el_umbral_vive_en_un_solo_lugar() -> None:
    """El umbral no se recalcula ni se duplica: una sola definición en producción."""
    apariciones: list[str] = []
    for rel in _tracked_py():
        if not rel.startswith("sky_claw/"):
            continue
        texto = (_REPO / rel).read_text(encoding="utf-8")
        if "DEGENERATE_THRESHOLD" in texto:
            apariciones.append(rel)
    assert apariciones == ["sky_claw/local/native_parallax/research/authored_dataset.py"], (
        f"DEGENERATE_THRESHOLD debe definirse en un único módulo de producción: {apariciones}"
    )


def test_e_la_referencia_independiente_no_entra_en_produccion() -> None:
    """El oráculo es instrumento de medición: no puede filtrarse al paquete."""
    fuente = (_PRODUCCION / "authored_dataset.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    assert not any(
        isinstance(n, ast.FunctionDef) and n.name in {"_oraculo", "_eje", "_coeficientes"} for n in ast.walk(arbol)
    )


# ===========================================================================
# Familia F — propagación fail-closed en M2/M3/M4/M5
# ===========================================================================
_ENTRADA_M3: dict[str, object] = {
    "asset_id": "asset-m3",
    "family": "brick",
    "provider": "ambientCG",
    "normal_path": "n.png",
    "height_path": "h.png",
    "normal_convention": "OPENGL",
    "height_semantics": "UNKNOWN_SCALE_RELATIVE_GRAYSCALE",
    "license": "CC0-1.0",
    "official_source": "https://example.invalid",
    "normal_sha256": "0" * 64,
    "height_sha256": "0" * 64,
    "normal_resolution": [1024, 1024],
    "provenance_status": "PRIMARY_SOURCE_DOWNLOADED",
    "height_bit_depth": 8,
}


def _error_degenerado() -> object:
    return ad.DegenerateNormalError(
        asset_id="asset-m3",
        degenerate_count=7,
        total_pixels=512 * 512,
        threshold=ad.DEGENERATE_THRESHOLD,
        fraction=7 / (512 * 512),
        reason="NON_EVALUABLE: sintético de test",
    )


def test_f_m3_aborta_ante_degeneracion_y_no_excluye(monkeypatch: pytest.MonkeyPatch) -> None:
    """El punto crítico (§5.2): `except DatasetInvalidError: continue` NO puede absorberla."""
    from sky_claw.local.native_parallax.research import run_exp_m3

    def _revienta(*_a: object, **_k: object) -> None:
        raise _error_degenerado()  # type: ignore[misc]

    monkeypatch.setattr(run_exp_m3, "load_asset", _revienta)
    with pytest.raises(ad.DegenerateNormalError):
        run_exp_m3.evaluate_cohort_a([dict(_ENTRADA_M3)], 512)


def test_f_m3_la_exclusion_de_integridad_sigue_siendo_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-06.e / §5.3.4.6: no se reclasifica la integridad como H4."""
    from sky_claw.local.native_parallax.research import run_exp_m3

    def _revienta(*_a: object, **_k: object) -> None:
        raise DatasetInvalidError("asset-m3: hash cambió para n.png (§43)")

    monkeypatch.setattr(run_exp_m3, "load_asset", _revienta)
    ev = run_exp_m3.evaluate_cohort_a([dict(_ENTRADA_M3)], 512)
    assert ev["rows"] == []
    assert [d["asset_id"] for d in ev["dataset_invalid"]] == ["asset-m3"]


def test_f_m3_no_publica_artefacto_cientifico(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Un H4 degenerado NO puede producir `exp_m3_results.json` (fail-closed end-to-end).

    El único `write_text` del artefacto vive al final de `main()`, después de toda la
    evaluación. Si la degeneración aborta la corrida antes, el archivo no existe: es la
    prueba de que el error no se degrada en un resultado científico publicado sobre una
    cohorte alterada (§5.3.4.4). El test no confía en el orden por lectura: lo ejecuta.
    """
    from sky_claw.local.native_parallax.research import run_exp_m3

    def _revienta(*_a: object, **_k: object) -> None:
        raise _error_degenerado()  # type: ignore[misc]

    monkeypatch.setattr(run_exp_m3, "load_asset", _revienta)
    monkeypatch.setattr(run_exp_m3, "load_cohort_a", lambda *_a, **_k: ([dict(_ENTRADA_M3)], []))
    monkeypatch.setattr(run_exp_m3, "check_cohort_a_sufficient", lambda *_a, **_k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_exp_m3", "--m3-manifest", "m3.json", "--m2-manifest", "m2.json", "--out", str(tmp_path)],
    )
    with pytest.raises(ad.DegenerateNormalError):
        run_exp_m3.main()
    assert not (tmp_path / "exp_m3_results.json").exists(), "se publicó un artefacto sobre una cohorte alterada"


def test_f_m2_m4_m5_no_capturan_dataset_invalid() -> None:
    """M2/M4/M5 propagan por defecto: cero capturas alrededor de `load_asset`."""
    for rel in (
        "sky_claw/local/native_parallax/research/run_exp_m2.py",
        "sky_claw/local/native_parallax/research/run_exp_m4.py",
        "sky_claw/local/native_parallax/research/run_exp_m5.py",
    ):
        capturas = [h for h in _handlers_de_excepciones(rel) if "DatasetInvalidError" in _nombre_de_handler(h)]
        assert not capturas, f"{rel} captura DatasetInvalidError en {[h.lineno for h in capturas]}"


def test_f_m3_declara_el_handler_de_degeneracion_antes_del_de_integridad() -> None:
    """El `try` crítico de M3: subclase antes que superclase, y aborta (no `continue`)."""
    rel = "sky_claw/local/native_parallax/research/run_exp_m3.py"
    objetivo = [
        t for t in _trys_que_cargan_normales(rel) if any("DatasetInvalidError" in n for n in _indices_de_handlers(t))
    ]
    assert objetivo, "no se encontró el try crítico de M3 que carga normales y captura DatasetInvalidError"
    for t in objetivo:
        nombres = _indices_de_handlers(t)
        i_degen = next((k for k, n in enumerate(nombres) if _HANDLER_QUE_ABORTA in n), None)
        assert i_degen is not None, f"{rel}:{t.lineno}: el try crítico no trata {_HANDLER_QUE_ABORTA}"
        assert i_degen < nombres.index("DatasetInvalidError"), (
            f"{rel}:{t.lineno}: {_HANDLER_QUE_ABORTA} debe preceder a DatasetInvalidError (handlers={nombres})"
        )
        handler = t.handlers[i_degen]
        assert not _contiene_continue(handler), f"{rel}:{handler.lineno}: `continue` prohibido para la degeneración"
        assert any(isinstance(n, ast.Raise) for n in ast.walk(handler)), (
            f"{rel}:{handler.lineno}: el handler de {_HANDLER_QUE_ABORTA} debe abortar (`raise`)"
        )


# ===========================================================================
# Familia G — derivación del umbral (H4-F02), independiente del valor de producción
# ===========================================================================
# `ad.DEGENERATE_THRESHOLD` se lee SÓLO para COMPARAR contra el cruce derivado acá. Un
# test que leyera el umbral para "derivarlo" validaría la rama, no el valor — que es
# exactamente lo que el hallazgo H4-F02 señala de los tests previos.
_Q8 = 2.0 / 255.0
_SEMILLA_F02 = 20261012
_FACTORES_SOPORTADOS = (2, 4, 8, 16)
_TAMANO_F02 = 128


def _pesos_1d_del_factor_dos() -> NDArray[np.float64]:
    """Pesos 1-D del kernel separable a factor 2, extraídos del oráculo independiente."""
    filas = _coeficientes(128, 64)
    return max((w for _, w in filas), key=len)


def _familia_f02(size: int, largo: float, fase: int = 0) -> NDArray[np.float64]:
    """`n(i,j) = (sin(theta)*s(i), 0, cos(theta))` con `s` alternada y FASE desplazable.

    El kernel a factor 2 suma cero sobre `s`, así que el vector interpolado del interior
    es `(0, 0, cos(theta))` y su norma `L = cos(theta)` es **analítica**, no medida.

    `fase` desplaza el patrón (`s(i) = +1 si (i + fase) % 2 == 0`). El contrato §6.1
    punto 1 exige barrer ratio, **fase** y borde, no sólo Nyquist a fase fija: una grilla
    de fase única puede no alcanzar el mínimo real del kernel.
    """
    theta = math.acos(min(1.0, max(0.0, largo)))
    s = np.where((np.arange(size) + fase) % 2 == 0, 1.0, -1.0)
    n = np.zeros((size, size, 3), dtype=np.float64)
    n[..., 0] = (math.sin(theta) * s)[None, :]
    n[..., 2] = math.cos(theta)
    return n


def _delta_theta(v_limpio: NDArray[np.float64], v_pert: NDArray[np.float64]) -> NDArray[np.float64]:
    a = v_limpio / np.linalg.norm(v_limpio, axis=-1, keepdims=True)
    b = v_pert / np.maximum(np.linalg.norm(v_pert, axis=-1, keepdims=True), 1e-300)
    return np.arccos(np.clip(np.sum(a * b, axis=-1), -1.0, 1.0))


def _delta_theta_mc(size_in: int, size_out: int, largo: float, trials: int, rng: np.random.Generator):
    """Monte Carlo: perturba cada píxel dentro de su celda de retícula 8-bit
    (`U(-q/2, q/2)^3`, renormalizado) y mide el cambio de dirección inducido sobre el
    vector interpolado. Oráculo NumPy float64 — no otra llamada a Pillow."""
    base = _familia_f02(size_in, largo)
    v_limpio = _oraculo(base, size_out)
    interior = np.zeros((size_out, size_out), dtype=bool)
    interior[2:-2, 2:-2] = True
    acum: list[NDArray[np.float64]] = []
    for _ in range(trials):
        pert = base + rng.uniform(-_Q8 / 2, _Q8 / 2, size=base.shape)
        pert = pert / np.linalg.norm(pert, axis=-1, keepdims=True)
        acum.append(_delta_theta(v_limpio, _oraculo(pert, size_out))[interior])
    return np.concatenate(acum)


def _cruce_rms(size_in: int, size_out: int, rng: np.random.Generator) -> float:
    """Primer `L` de la grilla donde el RMS de `delta_theta` cae por debajo de `q_ang`.

    Devuelve `nan` si el criterio **nunca** se cumple en la grilla. Un centinela
    "optimista" (p. ej. el extremo inferior) haría pasar la aserción de conservaduría sin
    haber medido nada: es exactamente el verde falso que la familia G debe evitar.
    """
    for largo in np.linspace(0.02, 0.60, 25):
        dt = _delta_theta_mc(size_in, size_out, float(largo), 16, rng)
        if float(np.sqrt(np.mean(dt**2))) <= _Q8:
            return float(largo)
    return float("nan")


def _cruce_peor_caso_dirigido(size_in: int, size_out: int) -> float:
    """Cruce bajo peor caso dirigido: el signo de la perturbación se ELIGE, no se sortea.

    Devuelve `nan` si el criterio **nunca** se cumple en la grilla — mismo contrato que
    `_cruce_rms`, y por la misma razón. Un centinela optimista (p. ej. el extremo inferior
    `0.02`) haría pasar la aserción de conservaduría sin haber medido nada: el verde falso
    que la familia G existe para evitar. Con `nan`, `nan < umbral` es `False` y el fallo es
    ruidoso.
    """
    s = np.where(np.arange(size_in) % 2 == 0, 1.0, -1.0)
    for largo in np.linspace(0.02, 0.60, 25):
        base = _familia_f02(size_in, largo)
        v_limpio = _oraculo(base, size_out)
        peor = 0.0
        for signo in (1.0, -1.0):
            d = np.zeros_like(base)
            d[..., 0] = signo * _Q8 / 2 * s[None, :]
            pert = base + d
            pert = pert / np.linalg.norm(pert, axis=-1, keepdims=True)
            dt = _delta_theta(v_limpio, _oraculo(pert, size_out))[2:-2, 2:-2]
            peor = max(peor, float(np.percentile(dt, 99.0)))
        if peor <= _Q8:
            return float(largo)
    return float("nan")


def test_g_la_aritmetica_declarada_es_sum_w2_no_sqrt() -> None:
    """H4-F02: `5/16` es `sum(w^2)`, NO `sqrt(sum(w^2))` — que vale `0.5590`.

    La justificación previa confundía ambas cantidades. Acá los pesos se derivan del
    oráculo independiente y se verifica cuál de las dos coincide con producción.
    """
    w = _pesos_1d_del_factor_dos()
    assert len(w) == 4, f"el kernel a factor 2 debe tener 4 taps en el interior: {w}"
    np.testing.assert_allclose(w, [1 / 8, 3 / 8, 3 / 8, 1 / 8], atol=1e-12)
    suma_cuadrados = float(np.sum(w**2))
    assert suma_cuadrados == pytest.approx(5 / 16, abs=1e-15)
    assert math.sqrt(suma_cuadrados) == pytest.approx(0.5590169943749475, abs=1e-12)
    assert not math.isclose(math.sqrt(suma_cuadrados), 5 / 16, abs_tol=1e-3), (
        "sqrt(sum w^2) NO es 5/16: la justificación previa confundía las dos cantidades"
    )
    assert suma_cuadrados == pytest.approx(ad.DEGENERATE_THRESHOLD, abs=1e-15), (
        "el umbral de producción debe ser sum(w^2), no sqrt(sum(w^2))"
    )


def test_g_la_propagacion_rms_no_es_cota_worst_case() -> None:
    """H4-F02: un RMS **no** acota el peor caso. La cota *worst-case* de la MISMA celda
    declarada es `(q/2)*sqrt(3)` — `2.771x` el factor RMS."""
    w = _pesos_1d_del_factor_dos()
    rms = float(np.sum(w**2)) * _Q8
    worst_case = (_Q8 / 2) * math.sqrt(3.0) * float(np.sum(w) ** 2)
    assert worst_case > rms, "el peor caso no puede ser menor que el RMS"
    assert worst_case / rms == pytest.approx(2.771281292110204, rel=1e-9)
    assert worst_case / _Q8 > ad.DEGENERATE_THRESHOLD, (
        "5/16 no puede presentarse como cota worst-case: el peor caso la supera"
    )


def test_g_el_cruce_medido_es_conservador_en_los_factores_soportados() -> None:
    """H4-F02: prueba sintética REPRODUCIBLE e independiente del valor de producción.

    Oráculo: filtro separable bilineal en NumPy float64. Familia: `L = cos(theta)`
    analítico. Perturbación: `U(-q/2, q/2)^3` renormalizada, `q = 2/255`. Métrica:
    `delta_theta` entre el vector interpolado limpio y el perturbado. Criterio (fijado
    ANTES de medir): el umbral es conservador si el cruce `delta_theta(L) = q_ang` cae
    por debajo de `5/16` en todos los factores soportados.

    Lo que NO prueba: que `5/16` sea una garantía matemática universal — no lo es (ver
    `test_g_la_propagacion_rms_no_es_cota_worst_case`). Prueba que el valor vigente
    rechaza un superconjunto de la banda no confiable.
    """
    rng = np.random.default_rng(_SEMILLA_F02)
    medidos: dict[int, float] = {}
    for k in _FACTORES_SOPORTADOS:
        cruce = _cruce_rms(_TAMANO_F02, _TAMANO_F02 // k, rng)
        medidos[k] = cruce
        assert cruce < ad.DEGENERATE_THRESHOLD, (
            f"k={k}: el cruce medido {cruce:.4f} NO queda por debajo de {ad.DEGENERATE_THRESHOLD:.4f}"
        )
    assert medidos[2] >= medidos[16], f"el cruce debe decrecer con k: {medidos}"


def test_g_el_peor_caso_dirigido_tambien_queda_bajo_el_umbral() -> None:
    """H4-F02: incluso ELIGIENDO el signo de la perturbación (no al azar), el cruce
    queda por debajo del umbral en los factores soportados."""
    for k in _FACTORES_SOPORTADOS:
        cruce = _cruce_peor_caso_dirigido(_TAMANO_F02, _TAMANO_F02 // k)
        assert cruce < ad.DEGENERATE_THRESHOLD, f"k={k}: peor caso dirigido en {cruce:.4f}"


def test_g_la_produccion_declara_el_estado_de_la_derivacion() -> None:
    """H4-F02: la producción no puede presentar el umbral como garantía derivada.

    El estado real es `R7_NUMERIC_DERIVATION = BLOCKED` (heurística RMS, no cota) y la
    corrección aritmética debe estar declarada en el módulo, no sólo en este test.

    Además la premisa de Nyquist debe quedar **alineada con el gate #717 §6.1**, que está
    `FROZEN` y fija `CONJECTURE_NOT_FALSIFIED` por N-12. Un comentario de producción no
    puede reescribir el estado declarado de un contrato congelado: la evidencia nueva de
    alcance del ínfimo se registra bajo su propio nombre (`R7_NYQUIST_ATTAINMENT`).
    """
    fuente = (_PRODUCCION / "authored_dataset.py").read_text(encoding="utf-8")
    assert "R7_NUMERIC_DERIVATION = BLOCKED" in fuente, (
        "la producción debe declarar que la derivación está BLOCKED (heurística, no garantía)"
    )
    assert "sqrt(sum_k w_k^2) = sqrt(5/16) = 0.5590" in fuente, (
        "la producción debe declarar la corrección aritmética sum(w^2) vs sqrt(sum(w^2))"
    )
    assert "R7_WORST_CASE_PREMISE = CONJECTURE_NOT_FALSIFIED" in fuente, (
        "la producción no puede declarar la premisa de Nyquist como establecida: el gate "
        "#717 §6.1 (FROZEN, N-12) la fija como CONJECTURE_NOT_FALSIFIED"
    )
    assert "R7_NYQUIST_ATTAINMENT" in fuente, (
        "la evidencia de alcance del ínfimo debe declararse bajo su propio nombre, no reemplazando el estado del gate"
    )


# ---------------------------------------------------------------------------
# H4-F02 (cont.) — barrido de factor, FASE y BORDE que exige §6.1 punto 1
# ---------------------------------------------------------------------------
def _minimos_por_fase(k: int) -> list[tuple[float, float]]:
    """`(min L interior, min L borde)` para cada fase del patrón alternado.

    El borde se mide sobre el anillo exterior completo (fila 0, última fila y columnas
    extremas), que es donde el kernel se recorta y se renormaliza.
    """
    size_in = _TAMANO_F02
    size_out = size_in // k
    res: list[tuple[float, float]] = []
    for fase in range(k):
        norma = np.linalg.norm(_oraculo(_familia_f02(size_in, 0.0, fase), size_out), axis=-1)
        anillo = np.concatenate([norma[0, :], norma[-1, :], norma[1:-1, 0], norma[1:-1, -1]])
        res.append((float(norma[1:-1, 1:-1].min()), float(anillo.min())))
    return res


def test_g_nyquist_alcanza_el_infimo_en_todo_factor_y_fase() -> None:
    """§6.1 punto 1: "Nyquist = peor caso" deja de ser CONJETURA y pasa a ser COTA ALCANZADA.

    Se barre factor `k in {2,4,8,16}` x fase `fase in [0, k)` (30 combinaciones). En todas,
    el vector interpolado alcanza `L = 0` a precisión de máquina (`~6.1e-17`, que es el
    `cos(pi/2)` de la familia analítica). Como `L = ||v|| >= 0` para TODO vector, ninguna
    configuración soportada puede producir una norma menor: el ínfimo es `0` y Nyquist lo
    alcanza. Por eso la conclusión del punto 1 del procedimiento no necesita cita externa.

    Discriminación: se verifica que el cero NO sea un artefacto del oráculo, midiendo una
    familia de `L` analítico distinto (0.5) que debe dar norma mínima `~0.5`.

    **Brazos (declarado a propósito).** Esta prueba NO discrimina el fix H4: el kernel
    bilineal de Pillow es el MISMO en el brazo histórico y en el corregido, así que pasa en
    los dos. Es evidencia de la **derivación** de F02 —de qué cota es alcanzable—, no de la
    corrección del resizer. Se declara para que nadie la cuente como RED→GREEN.
    """
    combinaciones = 0
    for k in _FACTORES_SOPORTADOS:
        for interior, borde in _minimos_por_fase(k):
            assert interior <= 1e-12, f"k={k}: el interior no alcanza el ínfimo ({interior:.3e})"
            assert borde <= 1e-12, f"k={k}: el borde no alcanza el ínfimo ({borde:.3e})"
            combinaciones += 1
    assert combinaciones == sum(_FACTORES_SOPORTADOS), f"combinaciones barridas: {combinaciones}"

    # Control negativo: la familia de `L` analítico = 0.5 NO debe dar 0 — si lo diera, el
    # cero medido arriba sería un artefacto del oráculo y no una cancelación real.
    norma_control = np.linalg.norm(_oraculo(_familia_f02(_TAMANO_F02, 0.5, 0), _TAMANO_F02 // 2), axis=-1)
    assert 0.4 <= float(norma_control[2:-2, 2:-2].min()) <= 0.5, (
        f"el control negativo no reproduce el L analítico: {float(norma_control.min()):.4f}"
    )


def test_g_el_borde_no_es_mas_benigno_que_el_interior() -> None:
    """H4-F02: falsación de una afirmación del comentario de producción que NUNCA se midió.

    El comentario afirmaba que "el recorte del kernel en el borde es MÁS benigno (no cancela
    del todo)". La medición lo contradice: en las 30 combinaciones (factor x fase) el borde
    alcanza el MISMO ínfimo que el interior. La afirmación se retiró del módulo y esta prueba
    la mantiene retirada: si alguien la reintrodujera, la primera aserción falla.
    """
    for k in _FACTORES_SOPORTADOS:
        for interior, borde in _minimos_por_fase(k):
            assert borde <= interior + 1e-12, (
                f"k={k}: el borde resultó MENOS degenerado que el interior "
                f"({borde:.3e} > {interior:.3e}); la afirmación retirada volvería a ser cierta"
            )
    fuente = (_PRODUCCION / "authored_dataset.py").read_text(encoding="utf-8")
    assert "H4-F02-ANCLA-BORDE" in fuente, "la producción debe declarar la falsación del borde"
    assert "no es más benigno" in fuente


def test_g_la_cota_rms_decrece_con_el_factor_y_la_secuencia_declarada_es_la_medida() -> None:
    """H4-F02: la secuencia de cotas por factor está MEDIDA, no estimada.

    El comentario declaraba `0.3125 -> 0.2346 -> 0.1641 -> ...`. El valor `0.2346` no
    corresponde a ningún factor del kernel: la secuencia real —derivada del oráculo por
    respuesta a impulsos— es la que se verifica acá contra el ancla de producción.
    """
    medidos: list[float] = []
    for k in _FACTORES_SOPORTADOS:
        w = max((x for _, x in _coeficientes(_TAMANO_F02, _TAMANO_F02 // k)), key=len)
        assert float(np.sum(w)) == pytest.approx(1.0, abs=1e-12), f"k={k}: los pesos no suman 1"
        np.testing.assert_allclose(w, w[::-1], atol=1e-15, err_msg=f"k={k}: kernel no simétrico")
        medidos.append(float(np.sum(w**2)))
    assert all(b < a for a, b in zip(medidos, medidos[1:], strict=False)), f"la cota no decrece con k: {medidos}"
    assert medidos[0] == pytest.approx(ad.DEGENERATE_THRESHOLD, abs=1e-15), (
        "el umbral de producción debe ser sum(w^2) del factor 2, no otra cantidad"
    )
    # La secuencia declarada se LEE de producción y se compara con la medida: una copia
    # literal en el test no anclaría nada (el mutante que reintrodujera `0.2346` sobreviviría).
    fuente = (_PRODUCCION / "authored_dataset.py").read_text(encoding="utf-8")
    ancla = [ln for ln in fuente.splitlines() if "H4-F02-ANCLA-COTA" in ln]
    assert ancla, "la producción debe declarar la secuencia medida de cotas por factor"
    for valor in medidos:
        assert f"{valor:.7f}" in ancla[0], f"el valor medido {valor:.7f} no está declarado en producción: {ancla[0]!r}"


def test_g_el_riesgo_residual_de_factores_sub_dos_esta_medido() -> None:
    """§6.1 punto 1: el barrido de RATIO debe cubrir también los factores no soportados.

    El comentario declara `RESIDUAL_RISK = DECLARED` para `1 < k < 2` porque "la cota es
    mayor y el umbral deja de dominarla". Eso es una afirmación sobre el kernel: se mide.
    """
    for k in (1.1, 1.28, 1.5, 1.75):
        w = max((x for _, x in _coeficientes(1000, int(round(1000 / k)))), key=len)
        amplificacion = float(np.sum(w**2))
        assert amplificacion > ad.DEGENERATE_THRESHOLD, (
            f"k={k}: la amplificación {amplificacion:.4f} NO supera el umbral "
            f"{ad.DEGENERATE_THRESHOLD:.4f}; el riesgo residual declarado no estaría respaldado"
        )
    for k in _FACTORES_SOPORTADOS:
        w = max((x for _, x in _coeficientes(_TAMANO_F02, _TAMANO_F02 // k)), key=len)
        assert float(np.sum(w**2)) <= ad.DEGENERATE_THRESHOLD, (
            f"k={k}: el umbral no domina la amplificación del factor soportado"
        )


def test_g_el_criterio_se_sostiene_en_toda_la_cola_y_discrimina() -> None:
    """§6.1 punto 3: el umbral es el MENOR `L` tal que para TODO `L' >= L` se cumple
    `delta_theta(L') <= q_ang`. Se cierra la cláusula "para todo" sobre la malla declarada.

    Discriminación (lo que impide un verde falso): el criterio NO puede ser trivialmente
    cierto. Muy por debajo del cruce debe FALLAR; si se cumpliera en todas partes, cualquier
    valor de umbral lo satisfaría y la prueba no probaría nada.
    """
    rng = np.random.default_rng(_SEMILLA_F02)
    size_out = _TAMANO_F02 // 2
    malla = (0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.60)
    rms = {
        largo: float(np.sqrt(np.mean(_delta_theta_mc(_TAMANO_F02, size_out, largo, 12, rng) ** 2))) for largo in malla
    }
    assert rms[0.02] > _Q8, (
        f"el criterio es trivialmente cierto en L=0.02 ({rms[0.02]:.3e} <= q={_Q8:.3e}): "
        "la prueba no discriminaría nada"
    )
    cruce = next((largo for largo in malla if rms[largo] <= _Q8), None)
    assert cruce is not None, f"el criterio no se cumple en ninguna parte de la malla: {rms}"
    for largo in malla:
        if largo >= cruce:
            assert rms[largo] <= _Q8, f"L={largo}: la cola no se sostiene ({rms[largo]:.3e} > q)"
    assert cruce < ad.DEGENERATE_THRESHOLD, (
        f"el cruce medido {cruce} no queda por debajo del umbral {ad.DEGENERATE_THRESHOLD}"
    )

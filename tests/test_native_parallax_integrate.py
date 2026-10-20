"""NP-I0: integrador normal → altura (ADR 0014 §2.5) y su convención de frecuencias compartida.

El producto REIMPLEMENTA el solver de research (``normal_fft_periodic.py`` y la inversa de
``normal_from_height.py``): ADR 0014, decisión 2, prohíbe importarlo. Este archivo lo compara con research como
ORÁCULO sobre los 17 casos del corpus de NP-D0 y varias grillas (rectangulares en los dos sentidos, #676), y
además fija propiedades que el oráculo no ve:

- la inversa ANALÍTICA de senos puros (el oráculo y la copia podrían moverse juntos; esto no);
- el criterio EXP-001 del brief (RMSE < 0.05) sin que el ajuste afín pueda esconder un error de escala: se mide
  el error CENTRADO CRUDO, que el ajuste no puede arreglar (NP-M0 §35);
- la convención de signos y escalas (PR-MATH-A §8): la inversa DIVIDE por la escala;
- la variante Neumann, con su solución analítica (coseno) y la demostración de que el periódico no la reproduce;
- que la normalización de altura NO trae percentiles por defecto: ADR 0014 §6 manda derivar, nunca fijar a mano.

Límite declarado: las normales de entrada son sintéticas y limpias. Nada acá dice cómo se comporta el integrador
con ruido, cuantización o normales reales (eso es de NP-Q0 y del benchmark).
"""

from __future__ import annotations

import dataclasses
import math
import sys

import numpy as np
import pytest

from sky_claw.local.native_parallax import espectral
from sky_claw.local.native_parallax.bench.sintetico import (
    IDS_CONTROLES,
    IDS_PERIODICOS,
    generar_altura,
    normal_desde_altura,
    razon_borde,
)
from sky_claw.local.native_parallax.decode import DependenciaNoDisponibleError
from sky_claw.local.native_parallax.integrate import (
    ALGORITMO_NEUMANN,
    ALGORITMO_PERIODICO,
    NZ_PISO_NUMERICO,
    AlturaIntegrada,
    AlturaPlanaError,
    Bordes,
    EntradaInvalidaError,
    IntegracionError,
    ModoNormalizacion,
    integrar,
    normalizar_altura,
)
from sky_claw.local.native_parallax.research import alignment as r_alineacion
from sky_claw.local.native_parallax.research import normal_fft_periodic as r_solver
from sky_claw.local.native_parallax.research import normal_from_height as r_normal
from sky_claw.local.native_parallax.research.metrics import seam_height as r_seam

TODOS = IDS_PERIODICOS + IDS_CONTROLES
GRILLAS = ((128, 128), (128, 256), (256, 128))

# Casos de banda limitada exacta: el solver los recupera al redondeo. Los otros tres (bultos, ladrillos y el
# relieve asimétrico) tienen contenido sobre Nyquist y se recuperan con error de truncado (medido <= 2.5e-5).
ANALITICOS = tuple(i for i in IDS_PERIODICOS if i not in ("S07_bumps", "S09_bricks", "S10_asymmetric"))


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x * x)))


# ---------------------------------------------------------------------------
# Convención de frecuencias: UNA sola definición
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tamano", [(8, 8), (16, 32), (33, 17), (128, 256), (7, 10)])
def test_los_ejes_periodicos_son_los_de_research(tamano: tuple[int, int]) -> None:
    n_filas, n_cols = tamano

    wy, wx = espectral.ejes_de_frecuencia(np, n_filas, n_cols)
    wy_r, wx_r = r_solver.freq_axes(n_filas, n_cols)

    np.testing.assert_array_equal(wy, wy_r)
    np.testing.assert_array_equal(wx, wx_r)
    assert wy.shape == (n_filas, 1)
    assert wx.shape == (1, n_cols)


def test_los_ejes_de_la_extension_neumann_usan_muestras_por_unidad_de_la_textura() -> None:
    # Extendida 2x2 pero con las mismas muestras por unidad de textura: wx = 2π · fftfreq(2W) · W.
    n_filas, n_cols = 16, 24

    wy, wx = espectral.ejes_de_frecuencia(np, 2 * n_filas, 2 * n_cols, n_filas, n_cols)

    esperado_x = 2.0 * np.pi * np.fft.fftfreq(2 * n_cols) * n_cols
    esperado_y = 2.0 * np.pi * np.fft.fftfreq(2 * n_filas) * n_filas
    esperado_x[n_cols] = 0.0  # Nyquist de la extendida
    esperado_y[n_filas] = 0.0
    np.testing.assert_array_equal(wx.ravel(), esperado_x)
    np.testing.assert_array_equal(wy.ravel(), esperado_y)


def test_la_derivada_se_anula_en_los_bins_de_nyquist() -> None:
    wy, wx = espectral.ejes_de_frecuencia(np, 16, 24)

    assert wx[0, 12] == 0.0
    assert wy[8, 0] == 0.0
    assert np.count_nonzero(wx == 0.0) == 2  # DC y Nyquist


def test_los_ejes_rechazan_tamanos_invalidos() -> None:
    for malo in (0, 1, -4, 2.0, True, "8", None):
        with pytest.raises(ValueError, match="entero"):
            espectral.ejes_de_frecuencia(np, malo, 8)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="entero"):
            espectral.ejes_de_frecuencia(np, 8, malo)  # type: ignore[arg-type]
    # Las muestras por unidad son opcionales (None = igual a la FFT), pero si se pasan deben ser válidas.
    for malo in (0, 1, -4, 2.0, True, "8"):
        with pytest.raises(ValueError, match="entero"):
            espectral.ejes_de_frecuencia(np, 8, 8, malo, 8)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="entero"):
            espectral.ejes_de_frecuencia(np, 8, 8, 8, malo)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Oráculo: el periódico de producto es el de research, bit a bit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grilla", GRILLAS, ids=lambda g: f"{g[0]}x{g[1]}")
@pytest.mark.parametrize("caso_id", TODOS)
def test_el_periodico_de_producto_es_identico_al_de_research(caso_id: str, grilla: tuple[int, int]) -> None:
    normal = normal_desde_altura(generar_altura(caso_id, *grilla))
    p, q, golpes = r_normal.gradients_from_normal(normal, sx=1.0, sy=1.0, nz_floor=NZ_PISO_NUMERICO)
    esperada, info = r_solver.integrate_periodic(p, q)

    resultado = integrar(normal, bordes=Bordes.PERIODICOS)

    np.testing.assert_array_equal(resultado.altura, esperada)
    assert resultado.nz_bajo_piso == golpes
    assert resultado.residuo_imaginario_max == info["max_imag_residual"]
    assert resultado.algoritmo == ALGORITMO_PERIODICO
    assert resultado.altura.shape == grilla


@pytest.mark.parametrize("escalas", [(2.0, 0.5), (-1.0, 1.0), (1.0, -1.0), (0.25, 4.0)], ids=str)
@pytest.mark.parametrize("caso_id", ["S06_multifreq", "S10_asymmetric", "S14_steep"])
def test_las_escalas_anisotropicas_siguen_a_research(caso_id: str, escalas: tuple[float, float]) -> None:
    sx, sy = escalas
    normal = normal_desde_altura(generar_altura(caso_id, 128, 256), escala_x=sx, escala_y=sy)
    p, q, _ = r_normal.gradients_from_normal(normal, sx=sx, sy=sy, nz_floor=NZ_PISO_NUMERICO)
    esperada, _ = r_solver.integrate_periodic(p, q)

    resultado = integrar(normal, bordes=Bordes.PERIODICOS, escala_x=sx, escala_y=sy)

    np.testing.assert_array_equal(resultado.altura, esperada)


# ---------------------------------------------------------------------------
# EXP-001 / EXP-002 del brief, sobre el corpus limpio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grilla", [(256, 256), (128, 256)], ids=lambda g: f"{g[0]}x{g[1]}")
@pytest.mark.parametrize("caso_id", IDS_PERIODICOS)
def test_exp001_el_periodico_recupera_la_altura_limpia(caso_id: str, grilla: tuple[int, int]) -> None:
    altura = generar_altura(caso_id, *grilla)

    resultado = integrar(normal_desde_altura(altura), bordes=Bordes.PERIODICOS)

    # Error CRUDO centrado: el ajuste afín no puede esconder un error de escala (el 2π de NP-M0 §35).
    assert _rms(resultado.altura - (altura - altura.mean())) < 1e-4
    # El criterio literal del brief (RMSE alineado < 0.05).
    assert r_alineacion.best_affine(resultado.altura, altura)["rmse"] < 0.05


@pytest.mark.parametrize("caso_id", ANALITICOS)
def test_exp001_los_casos_de_banda_limitada_se_recuperan_al_redondeo(caso_id: str) -> None:
    altura = generar_altura(caso_id, 256, 256)

    resultado = integrar(normal_desde_altura(altura), bordes=Bordes.PERIODICOS)

    assert _rms(resultado.altura - (altura - altura.mean())) < 1e-12


@pytest.mark.parametrize("caso_id", IDS_PERIODICOS)
def test_exp002_la_costura_del_toro_se_conserva(caso_id: str) -> None:
    altura = generar_altura(caso_id, 256, 256)

    resultado = integrar(normal_desde_altura(altura), bordes=Bordes.PERIODICOS)

    assert r_seam(resultado.altura, altura) < 0.01
    assert razon_borde(resultado.altura) <= 5.0  # dentro de la banda de los casos periódicos


def test_la_altura_de_media_cero_de_solo_lectura_y_con_su_algoritmo() -> None:
    normal = normal_desde_altura(generar_altura("S07_bumps", 128, 128))

    resultado = integrar(normal, bordes=Bordes.PERIODICOS)

    assert abs(float(resultado.altura.mean())) < 1e-12
    assert resultado.altura.dtype == np.float64
    assert not resultado.altura.flags.writeable
    assert isinstance(resultado, AlturaIntegrada)
    assert resultado.nz_minimo == pytest.approx(float(normal[..., 2].min()), rel=0, abs=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        resultado.algoritmo = "otro"  # type: ignore[misc]


def test_los_nombres_de_algoritmo_estan_congelados() -> None:
    assert ALGORITMO_PERIODICO == "normal_fft_periodic_v1"
    assert ALGORITMO_NEUMANN == "normal_fft_neumann_v1"


# ---------------------------------------------------------------------------
# Inversa analítica (independiente del oráculo)
# ---------------------------------------------------------------------------


def _normal_de_gradientes(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    norma = np.sqrt(p * p + q * q + 1.0)
    return np.stack((-p / norma, -q / norma, 1.0 / norma), axis=-1)


def test_un_seno_en_x_se_recupera_con_su_amplitud_exacta() -> None:
    # h = 0.8·sin(2π·4·x)  ⇒  p = 0.8·2π·4·cos(2π·4·x), q = 0
    n_filas, n_cols = 128, 256
    x = np.arange(n_cols) / n_cols
    p = np.broadcast_to(0.8 * 2.0 * np.pi * 4.0 * np.cos(2.0 * np.pi * 4.0 * x), (n_filas, n_cols))
    normal = _normal_de_gradientes(np.array(p), np.zeros((n_filas, n_cols)))
    esperada = np.broadcast_to(0.8 * np.sin(2.0 * np.pi * 4.0 * x), (n_filas, n_cols))

    resultado = integrar(normal, bordes=Bordes.PERIODICOS)

    np.testing.assert_allclose(resultado.altura, esperada, atol=1e-10)


def test_un_seno_en_y_usa_el_eje_de_filas() -> None:
    n_filas, n_cols = 256, 128
    y = np.arange(n_filas) / n_filas
    q = np.broadcast_to((0.8 * 2.0 * np.pi * 4.0 * np.cos(2.0 * np.pi * 4.0 * y))[:, None], (n_filas, n_cols))
    normal = _normal_de_gradientes(np.zeros((n_filas, n_cols)), np.array(q))
    esperada = np.broadcast_to((0.8 * np.sin(2.0 * np.pi * 4.0 * y))[:, None], (n_filas, n_cols))

    resultado = integrar(normal, bordes=Bordes.PERIODICOS)

    np.testing.assert_allclose(resultado.altura, esperada, atol=1e-10)


def test_la_altura_no_depende_de_la_resolucion() -> None:
    # Unidades por unidad de textura y no por muestra (bug real cazado en NP-M0): misma superficie, otra grilla.
    chica = integrar(normal_desde_altura(generar_altura("S02_sine_x", 128, 128)), bordes=Bordes.PERIODICOS)
    grande = integrar(normal_desde_altura(generar_altura("S02_sine_x", 256, 256)), bordes=Bordes.PERIODICOS)

    np.testing.assert_allclose(chica.altura[0, :], grande.altura[0, ::2], atol=1e-10)


# ---------------------------------------------------------------------------
# Signos y escalas (PR-MATH-A §8: la inversa DIVIDE por la escala)
# ---------------------------------------------------------------------------


def test_invertir_las_dos_escalas_invierte_la_altura() -> None:
    normal = normal_desde_altura(generar_altura("S10_asymmetric", 128, 128))

    directa = integrar(normal, bordes=Bordes.PERIODICOS)
    invertida = integrar(normal, bordes=Bordes.PERIODICOS, escala_x=-1.0, escala_y=-1.0)

    np.testing.assert_allclose(invertida.altura, -directa.altura, atol=1e-15)


def test_invertir_solo_un_eje_se_distingue_de_invertir_los_dos() -> None:
    # El ajuste afín absorbe el flip global X+Y; un flip de UN eje no se absorbe (NP-M0 §34-Q10).
    altura = generar_altura("S10_asymmetric", 128, 128)
    normal = normal_desde_altura(altura)

    un_eje = integrar(normal, bordes=Bordes.PERIODICOS, escala_x=-1.0)

    assert r_alineacion.best_affine(un_eje.altura, altura)["rmse"] > 0.05


def test_la_escala_se_divide_y_se_recupera_la_altura_original() -> None:
    altura = generar_altura("S06_multifreq", 128, 256)
    normal = normal_desde_altura(altura, escala_x=2.0, escala_y=0.5)

    coherente = integrar(normal, bordes=Bordes.PERIODICOS, escala_x=2.0, escala_y=0.5)
    sin_escalas = integrar(normal, bordes=Bordes.PERIODICOS)

    np.testing.assert_allclose(coherente.altura, altura - altura.mean(), atol=1e-9)
    assert _rms(sin_escalas.altura - (altura - altura.mean())) > 1e-3


# ---------------------------------------------------------------------------
# El piso de nz: la singularidad se mide, no se esconde
# ---------------------------------------------------------------------------


def _normal_plana(n_filas: int = 64, n_cols: int = 64) -> np.ndarray:
    normal = np.zeros((n_filas, n_cols, 3))
    normal[..., 2] = 1.0
    return normal


def test_los_pixeles_bajo_el_piso_se_cuentan_y_el_resultado_sigue_finito() -> None:
    normal = _normal_plana()
    normal[10:14, 20:24] = (1.0, 0.0, 0.0)  # nz = 0 exacto: 16 píxeles
    normal[40, 40] = (0.6, 0.0, -0.8)  # nz negativo: 1 píxel

    resultado = integrar(normal, bordes=Bordes.PERIODICOS)

    assert resultado.nz_bajo_piso == 17
    assert resultado.nz_minimo == pytest.approx(-0.8)
    assert bool(np.all(np.isfinite(resultado.altura)))


def test_un_piso_mas_alto_cuenta_mas_pixeles() -> None:
    normal = normal_desde_altura(generar_altura("S14_steep", 128, 128))

    bajo = integrar(normal, bordes=Bordes.PERIODICOS, nz_piso=1e-6)
    alto = integrar(normal, bordes=Bordes.PERIODICOS, nz_piso=0.05)

    assert bajo.nz_bajo_piso == 0
    assert alto.nz_bajo_piso > 0


def test_el_piso_numerico_es_el_de_research() -> None:
    assert r_normal.NZ_FLOOR_DEFAULT == NZ_PISO_NUMERICO


def test_un_pixel_exactamente_en_el_piso_no_cuenta_como_bajo_el_piso() -> None:
    normal = _normal_plana()
    normal[5, 5] = (math.sqrt(1.0 - 0.05**2), 0.0, 0.05)

    resultado = integrar(normal, bordes=Bordes.PERIODICOS, nz_piso=0.05)

    assert resultado.nz_bajo_piso == 0  # `<` estricto: un nz igual al piso se usa tal cual


def test_el_piso_uno_es_valido_y_el_borde_superior_se_acepta() -> None:
    resultado = integrar(_normal_plana(), bordes=Bordes.PERIODICOS, nz_piso=1.0)

    assert resultado.nz_bajo_piso == 0


@pytest.mark.parametrize("malo", [0.0, -1e-6, 1.5, math.nan, math.inf, True, "1e-6", None])
def test_un_piso_invalido_se_rechaza(malo: object) -> None:
    with pytest.raises(EntradaInvalidaError):
        integrar(_normal_plana(), bordes=Bordes.PERIODICOS, nz_piso=malo)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Variante Neumann
# ---------------------------------------------------------------------------


def _coseno(n_filas: int, n_cols: int, a: int, b: int) -> np.ndarray:
    """h = cos(π·a·x)·cos(π·b·y) en centros de píxel: derivada normal nula en los cuatro bordes."""
    x = (np.arange(n_cols) + 0.5) / n_cols
    y = (np.arange(n_filas) + 0.5) / n_filas
    return np.cos(np.pi * a * x)[None, :] * np.cos(np.pi * b * y)[:, None]


def _normal_del_coseno(n_filas: int, n_cols: int, a: int, b: int) -> np.ndarray:
    x = (np.arange(n_cols) + 0.5) / n_cols
    y = (np.arange(n_filas) + 0.5) / n_filas
    p = -np.pi * a * np.sin(np.pi * a * x)[None, :] * np.cos(np.pi * b * y)[:, None]
    q = -np.pi * b * np.cos(np.pi * a * x)[None, :] * np.sin(np.pi * b * y)[:, None]
    return _normal_de_gradientes(p, q)


@pytest.mark.parametrize(("a", "b"), [(1, 1), (3, 2), (5, 7)])
@pytest.mark.parametrize("grilla", [(128, 128), (96, 192), (192, 96)], ids=lambda g: f"{g[0]}x{g[1]}")
def test_neumann_recupera_un_coseno_con_derivada_normal_nula(grilla: tuple[int, int], a: int, b: int) -> None:
    n_filas, n_cols = grilla
    altura = _coseno(n_filas, n_cols, a, b)

    resultado = integrar(_normal_del_coseno(n_filas, n_cols, a, b), bordes=Bordes.NEUMANN)

    np.testing.assert_allclose(resultado.altura, altura - altura.mean(), atol=1e-9)
    assert resultado.altura.shape == grilla
    assert resultado.algoritmo == ALGORITMO_NEUMANN
    assert abs(float(resultado.altura.mean())) < 1e-12
    assert not resultado.altura.flags.writeable


def test_el_periodico_no_reproduce_lo_que_neumann_si() -> None:
    # Una frecuencia de medio ciclo no es periódica en el toro: la variante existe por esto.
    n_filas, n_cols = 128, 128
    altura = _coseno(n_filas, n_cols, 3, 2)
    normal = _normal_del_coseno(n_filas, n_cols, 3, 2)

    neumann = integrar(normal, bordes=Bordes.NEUMANN)
    periodico = integrar(normal, bordes=Bordes.PERIODICOS)

    error_neumann = _rms(neumann.altura - (altura - altura.mean()))
    error_periodico = _rms(periodico.altura - (altura - altura.mean()))
    assert error_neumann < 1e-9
    assert error_periodico > 1e-2


def test_neumann_no_corrige_un_borde_que_no_cumple_su_hipotesis() -> None:
    # Declarado: la variante ASUME derivada normal nula. Un seno de 4 ciclos enteros SÍ es periódico (el periódico
    # lo recupera al redondeo) pero su pendiente en el borde no es nula: Neumann deja un error medible (~3e-3 a
    # 128x128). No hay magia: el integrador no adivina el borde.
    altura = generar_altura("S02_sine_x", 128, 128)
    normal = normal_desde_altura(altura)

    neumann = integrar(normal, bordes=Bordes.NEUMANN)
    periodico = integrar(normal, bordes=Bordes.PERIODICOS)

    centrada = altura - altura.mean()
    assert _rms(periodico.altura - centrada) < 1e-12
    assert _rms(neumann.altura - centrada) > 1e-3


def test_neumann_de_una_normal_plana_es_plana() -> None:
    resultado = integrar(_normal_plana(32, 48), bordes=Bordes.NEUMANN)

    assert not np.any(resultado.altura)
    assert resultado.nz_bajo_piso == 0


# ---------------------------------------------------------------------------
# Entradas inválidas y dependencias
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malo",
    [
        np.zeros((8, 8)),
        np.zeros((8, 8, 2)),
        np.zeros((8, 8, 3), dtype=np.float32),
        np.full((8, 8, 3), np.nan),
        np.zeros((1, 8, 3)),
        np.zeros((8, 1, 3)),
        [[[0.0, 0.0, 1.0]] * 8] * 8,
        None,
    ],
    ids=["2d", "dos-canales", "float32", "nan", "una-fila", "una-columna", "lista", "none"],
)
def test_una_normal_invalida_se_rechaza(malo: object) -> None:
    with pytest.raises(EntradaInvalidaError):
        integrar(malo, bordes=Bordes.PERIODICOS)  # type: ignore[arg-type]


@pytest.mark.parametrize("bordes", ["periodicos", "NEUMANN", None, 1])
def test_unos_bordes_invalidos_se_rechazan(bordes: object) -> None:
    with pytest.raises(EntradaInvalidaError):
        integrar(_normal_plana(), bordes=bordes)  # type: ignore[arg-type]


@pytest.mark.parametrize("malo", [0.0, math.nan, math.inf, -math.inf, True, "1", None])
def test_una_escala_invalida_se_rechaza_incluido_el_cero(malo: object) -> None:
    # sx = 0 haría indefinida la inversa p = -nx / (sx·nz): se rechaza, no se inventa un épsilon.
    with pytest.raises(EntradaInvalidaError):
        integrar(_normal_plana(), bordes=Bordes.PERIODICOS, escala_x=malo)  # type: ignore[arg-type]
    with pytest.raises(EntradaInvalidaError):
        integrar(_normal_plana(), bordes=Bordes.PERIODICOS, escala_y=malo)  # type: ignore[arg-type]


def test_los_errores_comparten_una_base_y_la_de_entrada_es_value_error() -> None:
    assert issubclass(EntradaInvalidaError, IntegracionError)
    assert issubclass(EntradaInvalidaError, ValueError)
    assert issubclass(AlturaPlanaError, IntegracionError)


def test_sin_numpy_se_lanza_el_error_tipado_con_la_receta(monkeypatch: pytest.MonkeyPatch) -> None:
    normal = _normal_plana()
    monkeypatch.setitem(sys.modules, "numpy", None)

    with pytest.raises(DependenciaNoDisponibleError, match=r"sky-claw\[native-parallax\]"):
        integrar(normal, bordes=Bordes.PERIODICOS)
    with pytest.raises(DependenciaNoDisponibleError, match=r"sky-claw\[native-parallax\]"):
        normalizar_altura(np.zeros((4, 4)), ModoNormalizacion.MIN_MAX)


# ---------------------------------------------------------------------------
# Normalización de altura: sin percentiles por defecto (ADR 0014 §6)
# ---------------------------------------------------------------------------


def _altura_con_picos() -> np.ndarray:
    return integrar(normal_desde_altura(generar_altura("S07_bumps", 128, 128)), bordes=Bordes.PERIODICOS).altura


def test_min_max_lleva_el_rango_completo_a_cero_uno() -> None:
    altura = _altura_con_picos()

    salida = normalizar_altura(altura, ModoNormalizacion.MIN_MAX)

    assert float(salida.min()) == 0.0
    assert float(salida.max()) == 1.0
    np.testing.assert_allclose(salida, (altura - altura.min()) / (altura.max() - altura.min()), atol=1e-15)
    assert not salida.flags.writeable
    assert salida.dtype == np.float64


def test_los_percentiles_recortan_las_colas_y_conservan_el_orden() -> None:
    altura = _altura_con_picos()

    salida = normalizar_altura(altura, ModoNormalizacion.PERCENTILES, percentil_bajo=2.0, percentil_alto=98.0)

    assert float(salida.min()) == 0.0
    assert float(salida.max()) == 1.0
    # Cerca del 2 % queda recortado en cada cola (continuo: sin empates masivos).
    assert 0.015 <= float(np.mean(salida == 0.0)) <= 0.03
    assert 0.015 <= float(np.mean(salida == 1.0)) <= 0.03
    # Monótona: dos píxeles ordenados antes siguen ordenados (no estrictos por el recorte).
    orden = np.argsort(altura.ravel(), kind="stable")
    assert bool(np.all(np.diff(salida.ravel()[orden]) >= 0.0))


def test_los_percentiles_cero_y_cien_equivalen_al_rango_completo() -> None:
    altura = _altura_con_picos()

    extremos = normalizar_altura(altura, ModoNormalizacion.PERCENTILES, percentil_bajo=0.0, percentil_alto=100.0)

    np.testing.assert_allclose(extremos, normalizar_altura(altura, ModoNormalizacion.MIN_MAX), atol=1e-15)


def test_los_percentiles_son_obligatorios_y_no_hay_default() -> None:
    altura = _altura_con_picos()

    with pytest.raises(EntradaInvalidaError, match="percentil"):
        normalizar_altura(altura, ModoNormalizacion.PERCENTILES)
    with pytest.raises(EntradaInvalidaError, match="percentil"):
        normalizar_altura(altura, ModoNormalizacion.PERCENTILES, percentil_bajo=2.0)
    with pytest.raises(EntradaInvalidaError, match="percentil"):
        normalizar_altura(altura, ModoNormalizacion.PERCENTILES, percentil_alto=98.0)


def test_min_max_rechaza_percentiles_en_vez_de_ignorarlos() -> None:
    altura = _altura_con_picos()

    with pytest.raises(EntradaInvalidaError, match="MIN_MAX"):
        normalizar_altura(altura, ModoNormalizacion.MIN_MAX, percentil_bajo=2.0)
    with pytest.raises(EntradaInvalidaError, match="MIN_MAX"):
        normalizar_altura(altura, ModoNormalizacion.MIN_MAX, percentil_alto=98.0)


@pytest.mark.parametrize(
    ("bajo", "alto"),
    [
        (98.0, 2.0),
        (50.0, 50.0),
        (-1.0, 98.0),
        (2.0, 101.0),
        (math.nan, 98.0),
        (2.0, math.inf),
        (True, 98.0),
        ("2", 98.0),
    ],
)
def test_unos_percentiles_invalidos_se_rechazan(bajo: object, alto: object) -> None:
    with pytest.raises(EntradaInvalidaError):
        normalizar_altura(
            _altura_con_picos(),
            ModoNormalizacion.PERCENTILES,
            percentil_bajo=bajo,
            percentil_alto=alto,  # type: ignore[arg-type]
        )


def test_un_modo_desconocido_se_rechaza() -> None:
    for malo in ("MIN_MAX", None, 3):
        with pytest.raises(EntradaInvalidaError, match="modo"):
            normalizar_altura(_altura_con_picos(), malo)  # type: ignore[arg-type]


def test_una_altura_plana_no_se_normaliza_en_silencio() -> None:
    plana = np.zeros((16, 16))
    with pytest.raises(AlturaPlanaError):
        normalizar_altura(plana, ModoNormalizacion.MIN_MAX)
    with pytest.raises(AlturaPlanaError):
        normalizar_altura(plana, ModoNormalizacion.PERCENTILES, percentil_bajo=2.0, percentil_alto=98.0)


def test_si_los_percentiles_coinciden_el_rango_es_degenerado() -> None:
    # Casi todo plano con un único pico: p2 == p98, así que recortar dejaría la altura sin información.
    altura = np.zeros((32, 32))
    altura[3, 3] = 1.0
    assert normalizar_altura(altura, ModoNormalizacion.MIN_MAX)[3, 3] == 1.0
    with pytest.raises(AlturaPlanaError):
        normalizar_altura(altura, ModoNormalizacion.PERCENTILES, percentil_bajo=2.0, percentil_alto=98.0)


@pytest.mark.parametrize(
    "malo",
    [
        np.zeros(8),
        np.zeros((4, 4, 3)),
        np.full((4, 4), np.nan),
        np.zeros((4, 4), dtype=np.float32),
        np.zeros((1, 8)),
        None,
    ],
)
def test_una_altura_invalida_se_rechaza(malo: object) -> None:
    with pytest.raises(EntradaInvalidaError):
        normalizar_altura(malo, ModoNormalizacion.MIN_MAX)  # type: ignore[arg-type]


def test_las_dos_normalizaciones_difieren_donde_hay_picos() -> None:
    # El motivo del estudio de ADR 0014 §4: con picos el rango completo comprime el grueso de la señal.
    altura = _altura_con_picos()

    completa = normalizar_altura(altura, ModoNormalizacion.MIN_MAX)
    robusta = normalizar_altura(altura, ModoNormalizacion.PERCENTILES, percentil_bajo=2.0, percentil_alto=98.0)

    assert float(np.std(robusta)) > float(np.std(completa))

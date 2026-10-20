"""NP-D0: el corpus sintético del benchmark (ADR 0014 §2.5) y su contrato.

El generador de producto (``native_parallax/bench/sintetico.py``) REIMPLEMENTA las fórmulas probadas en
``research/synthetic_height.py`` y ``research/normal_from_height.py`` (ADR 0014, decisión 2: el producto no
importa ``research``). Para que la copia no derive en silencio, este archivo la compara con el original
como ORÁCULO sobre TODOS los casos y varias grillas, incluidas las rectangulares (#676), y además fija:

- la especificación del corpus por igualdad literal (ids, clases, grillas, ruido, bits) y su huella;
- una tabla de huellas numéricas por caso, independiente del oráculo: si research cambiara una fórmula, el
  oráculo y la copia se moverían juntos y solo esta tabla lo vería;
- la inversa analítica de un caso cerrado (seno puro), también independiente del oráculo.

Límite declarado: la tabla de huellas usa tolerancia (1e-9) y NO un hash de los bytes de float64. ``np.sin`` y
``np.exp`` no garantizan los mismos bits entre CPUs y rutas SIMD (no verificado entre plataformas), así que
un hash exacto de los arrays sería frágil y no un contrato.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import sys
import zlib

import numpy as np
import pytest

from sky_claw.local.native_parallax._extra import importar_numpy
from sky_claw.local.native_parallax.bench import sintetico
from sky_claw.local.native_parallax.bench.sintetico import (
    ESPECIFICACION_V1,
    IDS_CONTROLES,
    IDS_PERIODICOS,
    TAMANO_MINIMO,
    BenchError,
    CasoDesconocidoError,
    CasoSintetico,
    EspecificacionDataset,
    ModeloCuantizacion,
    ParametroInvalidoError,
    cuantizar_normal,
    generar_altura,
    huella_especificacion,
    iterar_corpus,
    normal_desde_altura,
    perturbar_normal,
    razon_borde,
    semilla_estable,
)
from sky_claw.local.native_parallax.decode import DependenciaNoDisponibleError
from sky_claw.local.native_parallax.research import normal_from_height as r_normal
from sky_claw.local.native_parallax.research import nz_policies as r_nz
from sky_claw.local.native_parallax.research import run_np_m0 as r_corrida
from sky_claw.local.native_parallax.research import synthetic_height as r_altura

IDS_PERIODICOS_LITERAL = (
    "S01_flat",
    "S02_sine_x",
    "S03_sine_y",
    "S04_separable",
    "S05_diagonal",
    "S06_multifreq",
    "S07_bumps",
    "S08_ridges",
    "S09_bricks",
    "S10_asymmetric",
    "S11_highfreq",
    "S12_mixed_scale",
    "S13_near_flat",
    "S14_steep",
    "S15_periodic_noise",
)
IDS_CONTROLES_LITERAL = ("N01_ramp", "N02_bump_boundary")
TODOS_LOS_IDS = IDS_PERIODICOS_LITERAL + IDS_CONTROLES_LITERAL

# Rectangulares en los dos sentidos: un eje cambiado de lugar solo se ve si filas != columnas.
GRILLAS = ((128, 128), (128, 256), (256, 128))

# (media, desvío, mínimo, máximo, razón de borde, nz mínimo) a 128x128, calculados con la implementación de
# research y fijados acá. Tolerancia 1e-9: ver el límite declarado en el docstring del módulo.
HUELLAS_128 = {
    "S01_flat": (0.0, 0.0, 0.0, 0.0, 1.0, 1.0),
    "S02_sine_x": (0.0, 5.656854249492e-01, -8.0e-01, 8.0e-01, 1.0, 4.967451868669e-02),
    "S03_sine_y": (0.0, 5.656854249492e-01, -8.0e-01, 8.0e-01, 1.0, 4.967451868669e-02),
    "S04_separable": (0.0, 5.099019513593e-01, -1.0, 1.0, 1.0, 5.904626756383e-02),
    "S05_diagonal": (0.0, 7.071067811865e-01, -1.0, 1.0, 1.0, 4.409869717980e-02),
    "S06_multifreq": (
        0.0,
        4.031128874149e-01,
        -8.759800042583e-01,
        8.914187824144e-01,
        1.165809694,
        6.903806666857e-02,
    ),
    "S07_bumps": (
        3.382681642763e-01,
        2.656021001558e-01,
        6.388076861199e-03,
        1.883900531099,
        1.128904766,
        5.673033436229e-02,
    ),
    "S08_ridges": (0.0, 5.779138127783e-01, -1.0, 1.0, 1.0, 3.005351282315e-02),
    "S09_bricks": (4.012344023051e-01, 2.529944133840e-01, 3.240782797473e-02, 1.0, 1.0, 1.356840474763e-02),
    "S10_asymmetric": (
        3.282021450626e-02,
        4.602760247914e-01,
        -7.176357967396e-01,
        1.185443645950,
        1.082681902,
        4.305647152159e-02,
    ),
    "S11_highfreq": (0.0, 4.123105625618e-01, -8.0e-01, 8.0e-01, 1.0, 9.255144672955e-03),
    "S12_mixed_scale": (
        0.0,
        5.196152422707e-01,
        -9.959741443871e-01,
        9.959741443871e-01,
        1.003768908,
        3.457776023738e-02,
    ),
    "S13_near_flat": (
        0.0,
        4.031128874149e-03,
        -8.759800042583e-03,
        8.914187824144e-03,
        1.165809694,
        9.897202921657e-01,
    ),
    "S14_steep": (0.0, 2.263846284534, -4.5, 4.5, 1.0, 8.827983799275e-03),
    "S15_periodic_noise": (
        0.0,
        5.445700314776e-02,
        -2.062695634785e-01,
        1.969280793022e-01,
        1.200375625,
        7.284095244951e-02,
    ),
    "N01_ramp": (-3.90625e-03, 2.886663247944e-01, -5.0e-01, 4.921875e-01, 127.0, 1.139963411279e-02),
    "N02_bump_boundary": (
        1.197223017263e-01,
        2.262040188344e-01,
        5.520797756283e-10,
        9.999457479995e-01,
        19.21759153,
        1.790832771978e-02,
    ),
}


def _oraculo_altura(caso_id: str, n_filas: int, n_cols: int) -> np.ndarray:
    tabla = {**r_altura.PERIODIC_CASES, **r_altura.NEGATIVE_CONTROLS}
    return tabla[caso_id](n_filas, n_cols)


def _oraculo_normal(altura: np.ndarray, sx: float, sy: float) -> np.ndarray:
    p, q = r_normal.spectral_gradients(altura)
    return r_normal.normals_from_gradients(p, q, sx=sx, sy=sy)


# ---------------------------------------------------------------------------
# Especificación: se enumera y se congela
# ---------------------------------------------------------------------------


def test_los_ids_del_corpus_estan_congelados_por_igualdad_literal() -> None:
    assert IDS_PERIODICOS == IDS_PERIODICOS_LITERAL
    assert IDS_CONTROLES == IDS_CONTROLES_LITERAL
    assert tuple(c.id for c in ESPECIFICACION_V1.casos) == TODOS_LOS_IDS


def test_las_clases_de_cada_caso_estan_congeladas() -> None:
    esperado = {i: "periodico" for i in IDS_PERIODICOS_LITERAL} | {i: "control_negativo" for i in IDS_CONTROLES_LITERAL}
    assert {c.id: c.clase for c in ESPECIFICACION_V1.casos} == esperado


def test_la_grilla_el_ruido_y_los_bits_de_la_especificacion_estan_congelados() -> None:
    assert ESPECIFICACION_V1.version == "np-d0.v1"
    assert ESPECIFICACION_V1.resoluciones == ((128, 128), (256, 256), (512, 512), (128, 256), (256, 128))
    assert ESPECIFICACION_V1.sigmas_ruido == (1 / 255, 2 / 255, 4 / 255, 8 / 255)
    assert ESPECIFICACION_V1.bits_cuantizacion == (8, 10, 16)
    assert ESPECIFICACION_V1.modelos_cuantizacion == ("TRES_CANALES", "DOS_CANALES_RECONSTRUYE_Z")


def test_la_especificacion_incluye_grillas_rectangulares_en_los_dos_sentidos() -> None:
    # #676 (y el ADR 0014 §5) exigen cubrir texturas rectangulares; un solo sentido no ve un eje cambiado.
    assert (128, 256) in ESPECIFICACION_V1.resoluciones
    assert (256, 128) in ESPECIFICACION_V1.resoluciones


def test_los_ids_de_producto_coinciden_con_los_de_research() -> None:
    # Si research agrega o quita un caso, el oráculo deja de cubrir al producto: que lo decida una persona.
    assert set(TODOS_LOS_IDS) == set(r_altura.PERIODIC_CASES) | set(r_altura.NEGATIVE_CONTROLS)
    assert set(IDS_PERIODICOS_LITERAL) == set(r_altura.PERIODIC_CASES)
    assert set(IDS_CONTROLES_LITERAL) == set(r_altura.NEGATIVE_CONTROLS)


def test_los_parametros_de_perturbacion_coinciden_con_los_de_la_corrida_de_research() -> None:
    assert ESPECIFICACION_V1.sigmas_ruido == tuple(r_corrida.NOISE_SIGMAS)
    assert ESPECIFICACION_V1.bits_cuantizacion == tuple(r_corrida.QUANT_BITS)


# Texto canónico que se hashea, escrito a mano: así el literal documenta qué entra en la huella y el test no
# depende de que ``json.dumps`` de la implementación ordene o separe como esperamos.
TEXTO_CANONICO_V1 = (
    '{"bits_cuantizacion":[8,10,16],'
    '"casos":[["S01_flat","periodico"],["S02_sine_x","periodico"],["S03_sine_y","periodico"],'
    '["S04_separable","periodico"],["S05_diagonal","periodico"],["S06_multifreq","periodico"],'
    '["S07_bumps","periodico"],["S08_ridges","periodico"],["S09_bricks","periodico"],'
    '["S10_asymmetric","periodico"],["S11_highfreq","periodico"],["S12_mixed_scale","periodico"],'
    '["S13_near_flat","periodico"],["S14_steep","periodico"],["S15_periodic_noise","periodico"],'
    '["N01_ramp","control_negativo"],["N02_bump_boundary","control_negativo"]],'
    '"modelos_cuantizacion":["TRES_CANALES","DOS_CANALES_RECONSTRUYE_Z"],'
    '"resoluciones":[[128,128],[256,256],[512,512],[128,256],[256,128]],'
    '"sigmas_ruido":[0.00392156862745098,0.00784313725490196,0.01568627450980392,0.03137254901960784],'
    '"version":"np-d0.v1"}'
)
HUELLA_V1 = "6ca1573be23f132234e8c9aad3dedef11c00a5db8261b10a13a467e3009a61be"


def test_la_huella_de_la_especificacion_esta_fijada() -> None:
    assert huella_especificacion(ESPECIFICACION_V1) == HUELLA_V1


def test_la_huella_es_el_sha256_del_texto_canonico_escrito_a_mano() -> None:
    assert hashlib.sha256(TEXTO_CANONICO_V1.encode("ascii")).hexdigest() == HUELLA_V1


def test_cambiar_cualquier_campo_de_la_especificacion_cambia_la_huella() -> None:
    base = huella_especificacion(ESPECIFICACION_V1)
    variantes = (
        dataclasses.replace(ESPECIFICACION_V1, version="np-d0.v2"),
        dataclasses.replace(ESPECIFICACION_V1, resoluciones=ESPECIFICACION_V1.resoluciones[:-1]),
        dataclasses.replace(ESPECIFICACION_V1, sigmas_ruido=ESPECIFICACION_V1.sigmas_ruido[:-1]),
        dataclasses.replace(ESPECIFICACION_V1, bits_cuantizacion=ESPECIFICACION_V1.bits_cuantizacion[:-1]),
        dataclasses.replace(ESPECIFICACION_V1, modelos_cuantizacion=ESPECIFICACION_V1.modelos_cuantizacion[:-1]),
        dataclasses.replace(ESPECIFICACION_V1, casos=ESPECIFICACION_V1.casos[:-1]),
        dataclasses.replace(
            ESPECIFICACION_V1,
            casos=(
                dataclasses.replace(ESPECIFICACION_V1.casos[0], clase="control_negativo"),
                *ESPECIFICACION_V1.casos[1:],
            ),
        ),
    )
    for variante in variantes:
        assert huella_especificacion(variante) != base


def test_la_descripcion_no_entra_en_la_huella() -> None:
    # Corregir un typo de la descripción no debe invalidar el corpus; las fórmulas las congela la tabla numérica.
    retocado = dataclasses.replace(
        ESPECIFICACION_V1,
        casos=(dataclasses.replace(ESPECIFICACION_V1.casos[0], descripcion="otra"), *ESPECIFICACION_V1.casos[1:]),
    )
    assert huella_especificacion(retocado) == huella_especificacion(ESPECIFICACION_V1)


def test_la_especificacion_es_inmutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        ESPECIFICACION_V1.version = "otra"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ESPECIFICACION_V1.casos[0].id = "otra"  # type: ignore[misc]
    assert isinstance(ESPECIFICACION_V1, EspecificacionDataset)
    assert all(isinstance(c, CasoSintetico) for c in ESPECIFICACION_V1.casos)


# ---------------------------------------------------------------------------
# Altura: oráculo + huellas independientes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grilla", GRILLAS, ids=lambda g: f"{g[0]}x{g[1]}")
@pytest.mark.parametrize("caso_id", TODOS_LOS_IDS)
def test_la_altura_de_producto_es_identica_a_la_de_research(caso_id: str, grilla: tuple[int, int]) -> None:
    n_filas, n_cols = grilla

    altura = generar_altura(caso_id, n_filas, n_cols)

    np.testing.assert_array_equal(altura, _oraculo_altura(caso_id, n_filas, n_cols))
    assert altura.shape == (n_filas, n_cols)
    assert altura.dtype == np.float64
    assert bool(np.all(np.isfinite(altura)))
    assert not altura.flags.writeable


@pytest.mark.parametrize("caso_id", TODOS_LOS_IDS)
def test_la_huella_numerica_de_cada_caso_no_deriva(caso_id: str) -> None:
    altura = generar_altura(caso_id, 128, 128)
    normal = normal_desde_altura(altura)
    media, desvio, minimo, maximo, razon, nz_minimo = HUELLAS_128[caso_id]

    observado = (
        float(altura.mean()),
        float(altura.std()),
        float(altura.min()),
        float(altura.max()),
        razon_borde(altura),
        float(normal[..., 2].min()),
    )

    esperado = (media, desvio, minimo, maximo, razon, nz_minimo)
    assert observado == pytest.approx(esperado, rel=1e-9, abs=1e-9)


def test_la_tabla_de_huellas_cubre_exactamente_el_corpus() -> None:
    assert tuple(HUELLAS_128) == TODOS_LOS_IDS


def test_dos_llamadas_dan_el_mismo_arreglo_y_no_comparten_memoria() -> None:
    a = generar_altura("S15_periodic_noise", 128, 128)
    b = generar_altura("S15_periodic_noise", 128, 128)

    np.testing.assert_array_equal(a, b)
    assert not np.shares_memory(a, b)


def test_los_casos_aleatorios_no_dependen_del_estado_global_de_numpy() -> None:
    np.random.seed(1)
    a = generar_altura("S07_bumps", 128, 128)
    np.random.seed(2)
    b = generar_altura("S07_bumps", 128, 128)

    np.testing.assert_array_equal(a, b)


def test_la_altura_plana_es_exactamente_cero() -> None:
    assert not np.any(generar_altura("S01_flat", 128, 128))


# ---------------------------------------------------------------------------
# Razón de borde
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caso_id", TODOS_LOS_IDS)
def test_la_razon_de_borde_es_la_de_research(caso_id: str) -> None:
    altura = generar_altura(caso_id, 128, 256)

    assert razon_borde(altura) == r_altura.boundary_wrap_ratio(altura)


def test_los_periodicos_quedan_dentro_de_la_banda_y_los_controles_fuera() -> None:
    # La banda [0.2, 5] viene de la docstring de research; los controles negativos deben violarla A PROPÓSITO.
    for caso_id in IDS_PERIODICOS_LITERAL:
        assert 0.2 <= razon_borde(generar_altura(caso_id, 256, 256)) <= 5.0, caso_id
    for caso_id in IDS_CONTROLES_LITERAL:
        assert razon_borde(generar_altura(caso_id, 256, 256)) > 5.0, caso_id


def _campo_que_arranca_plano(n: int = 16) -> np.ndarray:
    """Plano en las primeras filas y con una subida al final: el paso del borde a su vecino es nulo."""
    campo = np.zeros((n, n))
    campo[n - 4 :, :] = np.arange(1, 5, dtype=np.float64)[:, None]
    return campo


def test_un_campo_que_arranca_plano_no_se_declara_periodico_por_tener_un_primer_paso_nulo() -> None:
    # Revisión de Codex en #758: con el paso 0→1 nulo la razón valía 1.0 sin mirar el resto del interior ni el
    # salto de wrap, y un control no periódico entraba al banco periódico. Acá el wrap vale 4 y el paso medio ~0.27.
    campo = _campo_que_arranca_plano()

    assert razon_borde(campo) > 5.0
    assert razon_borde(campo.T.copy()) > 5.0  # el mismo defecto por el otro eje


def test_el_primer_paso_nulo_usa_el_promedio_de_todos_los_pasos_interiores() -> None:
    campo = _campo_que_arranca_plano()
    esperado = float(np.abs(campo[0] - campo[-1]).mean()) / float(np.abs(np.diff(campo, axis=0)).mean())

    assert razon_borde(campo) == pytest.approx(esperado, rel=1e-12)


def test_el_defecto_latente_de_research_queda_documentado() -> None:
    # research devuelve 1.0 para ese campo (el mismo caso especial). El producto lo corrige; si research se
    # corrige, este test falla y obliga a sincronizar el comentario y el oráculo.
    assert r_altura.boundary_wrap_ratio(_campo_que_arranca_plano()) == 1.0


def test_un_campo_constante_sigue_siendo_periodico() -> None:
    assert razon_borde(np.full((8, 8), 3.0)) == 1.0
    constante_en_un_eje = np.tile(np.arange(8, dtype=np.float64)[None, :], (8, 1))
    assert razon_borde(constante_en_un_eje) == pytest.approx(7.0)  # el eje que varía es una rampa: wrap = 7 pasos


def test_razon_borde_rechaza_lo_que_no_es_un_campo_2d_finito() -> None:
    # (1, 8) y (8, 1): con un eje de largo 1 no hay "paso interior" y np.take(campo, 1) se saldría del arreglo.
    for malo in (
        np.zeros(8),
        np.zeros((4, 4, 3)),
        np.full((4, 4), np.nan),
        np.zeros((1, 8)),
        np.zeros((8, 1)),
        [[0.0, 1.0], [1.0, 0.0]],
    ):
        with pytest.raises(ParametroInvalidoError):
            razon_borde(malo)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Normal de referencia (ground truth)
# ---------------------------------------------------------------------------

ESCALAS = ((1.0, 1.0), (1.0, -1.0), (-1.0, 1.0), (2.0, 0.5))


@pytest.mark.parametrize("escalas", ESCALAS, ids=lambda e: f"sx{e[0]}_sy{e[1]}")
@pytest.mark.parametrize("caso_id", TODOS_LOS_IDS)
def test_la_normal_de_producto_es_identica_a_la_de_research(caso_id: str, escalas: tuple[float, float]) -> None:
    sx, sy = escalas
    altura = generar_altura(caso_id, 128, 256)

    normal = normal_desde_altura(altura, escala_x=sx, escala_y=sy)

    np.testing.assert_array_equal(normal, _oraculo_normal(altura, sx, sy))
    assert normal.shape == (128, 256, 3)
    assert normal.dtype == np.float64
    assert not normal.flags.writeable


@pytest.mark.parametrize("caso_id", TODOS_LOS_IDS)
def test_la_normal_es_unitaria_y_mira_hacia_arriba(caso_id: str) -> None:
    normal = normal_desde_altura(generar_altura(caso_id, 128, 128))

    np.testing.assert_allclose(np.linalg.norm(normal, axis=-1), 1.0, atol=1e-12)
    assert bool(np.all(normal[..., 2] > 0.0))


def test_una_altura_plana_da_la_normal_plana_exacta() -> None:
    normal = normal_desde_altura(generar_altura("S01_flat", 128, 128))

    assert not np.any(normal[..., 0])
    assert not np.any(normal[..., 1])
    assert bool(np.all(normal[..., 2] == 1.0))


def test_la_normal_de_un_seno_puro_coincide_con_la_derivada_analitica() -> None:
    # Independiente del oráculo: h = 0.8·sin(2π·4·x) ⇒ p = ∂h/∂x = 0.8·2π·4·cos(2π·4·x), q = 0,
    # y N = normalize(-p, 0, 1). Un signo o una escala mal puestos no pasan.
    n_filas, n_cols = 128, 256
    altura = generar_altura("S02_sine_x", n_filas, n_cols)
    x = np.arange(n_cols) / n_cols
    p = 0.8 * 2.0 * np.pi * 4.0 * np.cos(2.0 * np.pi * 4.0 * x)
    norma = np.sqrt(p * p + 1.0)
    esperado = np.empty((n_filas, n_cols, 3))
    esperado[..., 0] = (-p / norma)[None, :]
    esperado[..., 1] = 0.0
    esperado[..., 2] = (1.0 / norma)[None, :]

    normal = normal_desde_altura(altura)

    np.testing.assert_allclose(normal, esperado, atol=1e-10)


def test_la_normal_de_un_seno_en_y_usa_el_eje_de_filas() -> None:
    n_filas, n_cols = 256, 128
    altura = generar_altura("S03_sine_y", n_filas, n_cols)
    y = np.arange(n_filas) / n_filas
    q = 0.8 * 2.0 * np.pi * 4.0 * np.cos(2.0 * np.pi * 4.0 * y)
    norma = np.sqrt(q * q + 1.0)

    normal = normal_desde_altura(altura)

    np.testing.assert_allclose(normal[..., 1], np.repeat((-q / norma)[:, None], n_cols, axis=1), atol=1e-10)
    np.testing.assert_allclose(normal[..., 0], 0.0, atol=1e-10)


def test_la_normal_no_depende_de_la_resolucion() -> None:
    # Unidades: el gradiente es por unidad de textura y no por muestra (bug real cazado en NP-M0).
    chica = normal_desde_altura(generar_altura("S02_sine_x", 128, 128))
    grande = normal_desde_altura(generar_altura("S02_sine_x", 256, 256))

    np.testing.assert_allclose(chica[0, ::1, 0], grande[0, ::2, 0], atol=1e-10)


def test_las_escalas_invalidas_se_rechazan() -> None:
    altura = generar_altura("S01_flat", 128, 128)
    for malo in (math.nan, math.inf, -math.inf, True, "1", None):
        with pytest.raises(ParametroInvalidoError):
            normal_desde_altura(altura, escala_x=malo)  # type: ignore[arg-type]
        with pytest.raises(ParametroInvalidoError):
            normal_desde_altura(altura, escala_y=malo)  # type: ignore[arg-type]


def test_normal_desde_altura_rechaza_lo_que_no_es_un_campo_2d_finito() -> None:
    for malo in (
        np.zeros(8),
        np.zeros((4, 4, 3)),
        np.full((128, 128), np.nan),
        np.zeros((128, 128), dtype=np.float32),
        np.zeros((1, 8)),
        np.zeros((8, 1)),
    ):
        with pytest.raises(ParametroInvalidoError):
            normal_desde_altura(malo)


# ---------------------------------------------------------------------------
# Perturbaciones deterministas
# ---------------------------------------------------------------------------


def test_la_semilla_es_el_crc32_y_no_el_hash_de_python() -> None:
    # hash() de str se randomiza por proceso (PYTHONHASHSEED) y rompería la reproducibilidad. El literal sale
    # de zlib.crc32(b"S06_multifreq|noise0.003922") & 0x7FFFFFFF.
    assert semilla_estable("S06_multifreq", "noise0.003922") == 761778228
    assert semilla_estable("S06_multifreq", "noise0.003922") == zlib.crc32(b"S06_multifreq|noise0.003922") & 0x7FFFFFFF


def test_la_semilla_depende_del_caso_y_de_la_etiqueta() -> None:
    base = semilla_estable("S06_multifreq", "noise0.003922")

    assert semilla_estable("S07_bumps", "noise0.003922") != base
    assert semilla_estable("S06_multifreq", "noise0.007843") != base


def _normal_de_prueba() -> np.ndarray:
    return normal_desde_altura(generar_altura("S06_multifreq", 128, 128))


def test_el_ruido_sigue_la_receta_de_research() -> None:
    normal = _normal_de_prueba()
    sigma = 2 / 255
    rng = np.random.default_rng(semilla_estable("S06_multifreq", f"noise{sigma:.6f}"))
    ruidoso = normal + rng.normal(0.0, sigma, size=normal.shape)
    esperado = ruidoso / np.linalg.norm(ruidoso, axis=-1, keepdims=True)

    obtenido = perturbar_normal(normal, sigma, "S06_multifreq")

    np.testing.assert_array_equal(obtenido, esperado)


def test_el_ruido_es_reproducible_unitario_y_no_modifica_la_entrada() -> None:
    normal = _normal_de_prueba()
    copia = normal.copy()

    a = perturbar_normal(normal, 4 / 255, "S06_multifreq")
    b = perturbar_normal(normal, 4 / 255, "S06_multifreq")

    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(normal, copia)
    np.testing.assert_allclose(np.linalg.norm(a, axis=-1), 1.0, atol=1e-12)
    assert not a.flags.writeable
    assert not np.shares_memory(a, normal)


def test_el_ruido_cambia_con_sigma_y_con_el_caso() -> None:
    normal = _normal_de_prueba()

    base = perturbar_normal(normal, 2 / 255, "S06_multifreq")

    assert not np.array_equal(base, perturbar_normal(normal, 4 / 255, "S06_multifreq"))
    assert not np.array_equal(base, perturbar_normal(normal, 2 / 255, "S07_bumps"))


def test_mas_ruido_se_aleja_mas_de_la_normal_limpia() -> None:
    normal = _normal_de_prueba()
    errores = [
        float(np.abs(perturbar_normal(normal, s, "S06_multifreq") - normal).mean())
        for s in ESPECIFICACION_V1.sigmas_ruido
    ]

    assert errores == sorted(errores)
    assert errores[0] > 0.0


def test_sigma_invalido_se_rechaza() -> None:
    normal = _normal_de_prueba()
    for malo in (0.0, -1.0, math.nan, math.inf, True, "0.1", None):
        with pytest.raises(ParametroInvalidoError):
            perturbar_normal(normal, malo, "S06_multifreq")  # type: ignore[arg-type]


def test_perturbar_rechaza_una_normal_con_forma_o_tipo_incorrecto() -> None:
    for malo in (
        np.zeros((4, 4)),
        np.zeros((4, 4, 2)),
        np.zeros((4, 4, 3), dtype=np.float32),
        np.full((4, 4, 3), np.nan),
    ):
        with pytest.raises(ParametroInvalidoError):
            perturbar_normal(malo, 1 / 255, "S06_multifreq")


TRES = ModeloCuantizacion.TRES_CANALES
DOS = ModeloCuantizacion.DOS_CANALES_RECONSTRUYE_Z


def test_los_modelos_de_cuantizacion_de_la_especificacion_son_los_del_enum() -> None:
    assert tuple(m.name for m in ModeloCuantizacion) == ESPECIFICACION_V1.modelos_cuantizacion


@pytest.mark.parametrize("bits", ESPECIFICACION_V1.bits_cuantizacion)
def test_la_cuantizacion_de_tres_canales_es_la_de_research(bits: int) -> None:
    normal = _normal_de_prueba()

    obtenida = cuantizar_normal(normal, bits, modelo=TRES)

    np.testing.assert_array_equal(obtenida, r_normal.quantize_decode(normal, bits=bits))
    np.testing.assert_allclose(np.linalg.norm(obtenida, axis=-1), 1.0, atol=1e-12)
    assert not obtenida.flags.writeable


@pytest.mark.parametrize("bits", ESPECIFICACION_V1.bits_cuantizacion)
@pytest.mark.parametrize("caso_id", ["S06_multifreq", "S09_bricks", "S11_highfreq", "S14_steep"])
def test_la_cuantizacion_de_dos_canales_es_la_de_research(caso_id: str, bits: int) -> None:
    # El modelo de almacenamiento de BC5 (el shader reconstruye Z): lo identificó EXP-M1 como el que desplaza la
    # frontera de riesgo (revisión de Codex en #758: el de tres canales es una cota optimista).
    normal = normal_desde_altura(generar_altura(caso_id, 128, 128))

    obtenida = cuantizar_normal(normal, bits, modelo=DOS)

    np.testing.assert_array_equal(obtenida, r_nz.quantize_xy_reconstruct_z(np.array(normal), bits))
    assert not obtenida.flags.writeable
    assert obtenida.shape == normal.shape


def test_dos_canales_reconstruye_z_sin_renormalizar_y_deja_nz_cero_fuera_del_disco_unidad() -> None:
    normal = normal_desde_altura(generar_altura("S14_steep", 128, 128))

    obtenida = cuantizar_normal(normal, 8, modelo=DOS)

    x, y, z = obtenida[..., 0], obtenida[..., 1], obtenida[..., 2]
    suma = x * x + y * y
    np.testing.assert_array_equal(z, np.sqrt(np.maximum(0.0, 1.0 - suma)))
    assert bool(np.all(z >= 0.0))
    # fuera del disco unidad no hay Z: queda exactamente 0 y el vector deja de ser unitario
    fuera = suma > 1.0
    assert bool(np.all(z[fuera] == 0.0))
    assert bool(np.all(np.linalg.norm(obtenida, axis=-1)[fuera] > 1.0))
    # donde SÍ hay Z, el vector es unitario (sin renormalizar, por construcción)
    np.testing.assert_allclose(np.linalg.norm(obtenida, axis=-1)[~fuera], 1.0, atol=1e-12)


def test_en_pendientes_empinadas_dos_canales_pierde_nz_y_tres_canales_no() -> None:
    # Por esto el de tres canales es una cota optimista: sobre S14 a Q8 el de dos canales deja la mitad de los
    # píxeles con nz == 0 y el de tres, ninguno. Calibrar umbrales con el segundo solo sería optimista para BC5.
    normal = normal_desde_altura(generar_altura("S14_steep", 128, 128))

    tres = cuantizar_normal(normal, 8, modelo=TRES)
    dos = cuantizar_normal(normal, 8, modelo=DOS)

    assert int(np.count_nonzero(tres[..., 2] == 0.0)) == 0
    assert int(np.count_nonzero(dos[..., 2] == 0.0)) > normal.shape[0] * normal.shape[1] // 4


@pytest.mark.parametrize("modelo", [TRES, DOS], ids=lambda m: m.name)
def test_mas_bits_dan_menos_error_de_cuantizacion(modelo: ModeloCuantizacion) -> None:
    normal = _normal_de_prueba()
    errores = [
        float(np.abs(cuantizar_normal(normal, b, modelo=modelo) - normal).max())
        for b in ESPECIFICACION_V1.bits_cuantizacion
    ]

    assert errores[0] > errores[1] > errores[2]


def test_el_modelo_de_cuantizacion_es_obligatorio_y_no_hay_default() -> None:
    normal = _normal_de_prueba()

    with pytest.raises(TypeError):
        cuantizar_normal(normal, 8)  # type: ignore[call-arg]


@pytest.mark.parametrize("modelo", ["TRES_CANALES", "tres_canales", None, 1, True])
def test_un_modelo_de_cuantizacion_invalido_se_rechaza(modelo: object) -> None:
    with pytest.raises(ParametroInvalidoError, match="modelo"):
        cuantizar_normal(_normal_de_prueba(), 8, modelo=modelo)  # type: ignore[arg-type]


@pytest.mark.parametrize("modelo", [TRES, DOS], ids=lambda m: m.name)
def test_los_bits_fuera_de_la_especificacion_se_rechazan(modelo: ModeloCuantizacion) -> None:
    normal = _normal_de_prueba()
    for malo in (0, 1, 7, 9, 12, 17, 32, -8, True, 8.0, "8", None):
        with pytest.raises(ParametroInvalidoError):
            cuantizar_normal(normal, malo, modelo=modelo)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Entradas inválidas
# ---------------------------------------------------------------------------


def test_un_caso_desconocido_se_rechaza_con_su_id_en_el_mensaje() -> None:
    with pytest.raises(CasoDesconocidoError, match="S99_inexistente"):
        generar_altura("S99_inexistente", 128, 128)


def test_los_ids_se_comparan_exactos_sin_normalizar() -> None:
    for malo in ("s01_flat", " S01_flat", "S01_flat ", "", "S01"):
        with pytest.raises(CasoDesconocidoError):
            generar_altura(malo, 128, 128)


def test_un_id_que_no_es_texto_se_rechaza() -> None:
    for malo in (None, 1, b"S01_flat", ("S01_flat",)):
        with pytest.raises(CasoDesconocidoError):
            generar_altura(malo, 128, 128)  # type: ignore[arg-type]


def test_el_tamano_minimo_deja_el_seno_de_32_ciclos_bajo_nyquist() -> None:
    # S11 usa 32 ciclos: con 64 muestras sería exactamente Nyquist (seno = 0 en cada muestra).
    assert TAMANO_MINIMO == 128
    assert TAMANO_MINIMO / 2 > 32


@pytest.mark.parametrize(
    ("n_filas", "n_cols"),
    [
        (0, 128),
        (128, 0),
        (-128, 128),
        (128, -1),
        (127, 128),
        (128, 127),
        (64, 64),
        (128.0, 128),
        (128, 128.0),
        (True, 128),
        (128, True),
        ("128", 128),
        (None, 128),
    ],
)
def test_las_dimensiones_invalidas_se_rechazan(n_filas: object, n_cols: object) -> None:
    with pytest.raises(ParametroInvalidoError):
        generar_altura("S01_flat", n_filas, n_cols)  # type: ignore[arg-type]


def test_el_tamano_minimo_se_acepta() -> None:
    assert generar_altura("S11_highfreq", TAMANO_MINIMO, TAMANO_MINIMO).shape == (TAMANO_MINIMO, TAMANO_MINIMO)


def test_los_errores_comparten_una_base_y_los_de_parametro_son_value_error() -> None:
    assert issubclass(CasoDesconocidoError, BenchError)
    assert issubclass(ParametroInvalidoError, BenchError)
    assert issubclass(ParametroInvalidoError, ValueError)


# ---------------------------------------------------------------------------
# Recorrido del corpus
# ---------------------------------------------------------------------------


def test_iterar_corpus_recorre_los_17_casos_en_el_orden_de_la_especificacion() -> None:
    elementos = list(iterar_corpus(128, 128))

    assert tuple(e.caso.id for e in elementos) == TODOS_LOS_IDS
    assert [e.caso for e in elementos] == list(ESPECIFICACION_V1.casos)


def test_iterar_corpus_entrega_lo_mismo_que_las_funciones_sueltas() -> None:
    for elemento in iterar_corpus(128, 256):
        altura = generar_altura(elemento.caso.id, 128, 256)
        np.testing.assert_array_equal(elemento.altura, altura)
        np.testing.assert_array_equal(elemento.normal, normal_desde_altura(altura))


def test_iterar_corpus_valida_la_grilla_antes_de_generar_nada() -> None:
    # Sin ``next``: la validación es al llamar, no al empezar a iterar.
    with pytest.raises(ParametroInvalidoError):
        iterar_corpus(64, 64)


# ---------------------------------------------------------------------------
# Dependencias: numpy es del extra opcional
# ---------------------------------------------------------------------------


def test_sin_numpy_se_lanza_el_error_tipado_con_la_receta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", None)

    with pytest.raises(DependenciaNoDisponibleError, match=r"sky-claw\[native-parallax\]"):
        generar_altura("S01_flat", 128, 128)


def test_el_corpus_usa_el_punto_unico_de_importacion_perezosa_de_numpy() -> None:
    # No tiene un acceso propio a numpy: el ancla de producto congela quiénes lo importan dentro de funciones.
    assert sintetico.importar_numpy is importar_numpy
    assert not hasattr(sintetico, "_numpy")

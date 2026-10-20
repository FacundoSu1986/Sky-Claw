"""NP-X0a (EXP-005 mecánico): qué se puede saber de la convención de una normal MIRANDO SOLO sus píxeles.

Pregunta: dado un archivo de normales, ¿se puede decir si su canal G es DirectX (Y abajo, ``escala_y = +1``) u
OpenGL (``escala_y = -1``)? El informe del ecosistema dice que no hay detección general solo por píxeles (PyNifly
#61); este archivo mide hasta dónde llega y por qué, sobre el corpus sintético de NP-D0.

Qué se ancla (y con qué):

- **El mecanismo, en forma cerrada.** Leída con la convención equivocada, una superficie integrable da un residuo de
  ``2m / (a + 2m + b)``, con ``m = media|h_xy|`` y ``a``, ``b`` las medias de ``|h_xx|`` y ``|h_yy|``. Se contrasta con
  el residuo medido en los 15 casos periódicos. La detectabilidad es la derivada MIXTA de la superficie.
- **La partición que eso produce**: 7 casos detectables y 8 que no (planos, senos en un solo eje, separables), con
  una brecha de más de 10 órdenes de magnitud entre unos y otros.
- **Las dos ambigüedades que ninguna métrica puede resolver**: la inversión global (X e Y a la vez niega la altura
  EXACTAMENTE y deja idénticas todas las métricas) y la escala uniforme (no cambia la altura normalizada).
- **Qué pasa con ruido y cuantización**, como afirmaciones cualitativas con margen holgado: no se congelan conteos de
  casos marginales.

Límite declarado: es el corpus SINTÉTICO y el ruido es gaussiano. EXP-M2 midió que lo sintético no predice texturas
authored reales, y el ruido de compresión por bloques (BC5/BC7) no está simulado. Esto delimita lo que se PUEDE decir;
no valida ningún umbral.
"""

from __future__ import annotations

import math
import sys

import numpy as np
import pytest

from sky_claw.local.native_parallax.bench.sintetico import (
    IDS_PERIODICOS,
    ModeloCuantizacion,
    cuantizar_normal,
    generar_altura,
    normal_desde_altura,
    perturbar_normal,
)
from sky_claw.local.native_parallax.espectral import gradientes_espectrales
from sky_claw.local.native_parallax.experiments.convencion import (
    PISO_NUMERICO,
    LecturaDeConvencion,
    leer_convencion,
)
from sky_claw.local.native_parallax.integrate import (
    Bordes,
    EntradaInvalidaError,
    ModoNormalizacion,
    integrar,
    normalizar_altura,
)
from sky_claw.local.native_parallax.metrics import acuerdo_normal, razon_de_costura, residuo_de_integrabilidad

N = 128
DETECTABLES = frozenset(
    {
        "S05_diagonal",
        "S06_multifreq",
        "S07_bumps",
        "S09_bricks",
        "S12_mixed_scale",
        "S13_near_flat",
        "S15_periodic_noise",
    }
)
NO_DETECTABLES = frozenset(IDS_PERIODICOS) - DETECTABLES
# Superficies cuyos modos de Fourier comparten dirección (senos en un eje, onda plana diagonal, crestas).
MODOS_EN_UNA_SOLA_DIRECCION = frozenset({"S02_sine_x", "S03_sine_y", "S05_diagonal", "S08_ridges"})


def _altura(caso: str) -> np.ndarray:
    return generar_altura(caso, N, N)


def _directx(caso: str) -> np.ndarray:
    """La normal de referencia de NP-D0: ``N = normalize(-sx·p, -sy·q, 1)`` con ``sx = sy = +1`` (DirectX)."""
    return normal_desde_altura(_altura(caso))


def _con_g_invertido(normal: np.ndarray) -> np.ndarray:
    """El MISMO relieve guardado con el canal G invertido (lo que escribiría una herramienta OpenGL)."""
    salida = normal.copy()
    salida[..., 1] = -salida[..., 1]
    return salida


def _con_x_e_y_invertidos(normal: np.ndarray) -> np.ndarray:
    salida = normal.copy()
    salida[..., 0] = -salida[..., 0]
    salida[..., 1] = -salida[..., 1]
    return salida


def _participacion_de_la_derivada_mixta(altura: np.ndarray) -> tuple[float, float, float]:
    """``(a, m, b)`` = medias de ``|h_xx|``, ``|h_xy|`` y ``|h_yy|`` con la derivada espectral del producto."""
    p, q = gradientes_espectrales(np, altura)
    p_x, p_y = gradientes_espectrales(np, p)
    _, q_y = gradientes_espectrales(np, q)
    return float(np.abs(p_x).mean()), float(np.abs(p_y).mean()), float(np.abs(q_y).mean())


def _residuo_esperado_con_la_convencion_equivocada(altura: np.ndarray) -> float:
    a, m, b = _participacion_de_la_derivada_mixta(altura)
    denominador = a + 2.0 * m + b
    return 0.0 if denominador == 0.0 else 2.0 * m / denominador


# ---------------------------------------------------------------------------
# El resultado de la lectura
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("directx", "opengl", "esperado"),
    [(0.0, 0.0, 0.0), (0.25, 0.25, 0.0), (1.0, 0.0, 1.0), (0.0, 1.0, 1.0), (1.0, 3.0, 0.5), (3.0, 1.0, 0.5)],
)
def test_el_margen_relativo_va_de_empate_a_una_lectura_perfecta(directx: float, opengl: float, esperado: float) -> None:
    assert LecturaDeConvencion(directx, opengl).margen_relativo == pytest.approx(esperado)


@pytest.mark.parametrize(
    ("directx", "opengl", "esperada"),
    [(0.1, 0.5, "directx"), (0.5, 0.1, "opengl"), (0.3, 0.3, None), (0.0, 0.0, None)],
)
def test_la_convencion_elegida_es_la_de_menor_residuo_y_un_empate_no_elige(
    directx: float, opengl: float, esperada: str | None
) -> None:
    assert LecturaDeConvencion(directx, opengl).elegida == esperada


def test_el_piso_numerico_es_la_raiz_del_epsilon_de_float64() -> None:
    # No es un umbral de calidad: es la definicion estandar de "cero numerico" (el error de un residuo
    # calculado con FFT en float64 queda varios ordenes por debajo).
    assert PISO_NUMERICO == math.sqrt(sys.float_info.epsilon)


def test_dos_residuos_en_el_piso_numerico_no_son_una_lectura_aunque_difieran_entre_si() -> None:
    # Con datos float64 exactos, ambos residuos son ruido de redondeo; su cociente relativo no dice nada.
    lectura = LecturaDeConvencion(9.9e-16, 9.3e-15)

    assert lectura.tiene_senal is False
    assert lectura.margen_relativo == 0.0
    assert lectura.elegida is None


def test_el_piso_numerico_es_inclusivo_y_un_residuo_apenas_por_encima_es_senal() -> None:
    arriba = math.nextafter(PISO_NUMERICO, math.inf)

    assert LecturaDeConvencion(PISO_NUMERICO, 0.0).tiene_senal is False
    assert LecturaDeConvencion(0.0, PISO_NUMERICO).tiene_senal is False
    assert LecturaDeConvencion(PISO_NUMERICO, PISO_NUMERICO / 2.0).tiene_senal is False
    con_directx_alto = LecturaDeConvencion(arriba, 0.0)
    con_opengl_alto = LecturaDeConvencion(0.0, arriba)
    assert (con_directx_alto.tiene_senal, con_directx_alto.elegida, con_directx_alto.margen_relativo) == (
        True,
        "opengl",
        1.0,
    )
    assert (con_opengl_alto.tiene_senal, con_opengl_alto.elegida, con_opengl_alto.margen_relativo) == (
        True,
        "directx",
        1.0,
    )


@pytest.mark.parametrize("valor", [-1e-9, float("nan"), float("inf")])
@pytest.mark.parametrize("campo", ["residuo_directx", "residuo_opengl"])
def test_un_residuo_negativo_o_no_finito_no_es_una_lectura(campo: str, valor: float) -> None:
    argumentos = {"residuo_directx": 0.1, "residuo_opengl": 0.1, campo: valor}

    with pytest.raises(EntradaInvalidaError):
        LecturaDeConvencion(**argumentos)


def test_leer_la_convencion_lee_el_mismo_archivo_con_las_dos_hipotesis_de_escala_y() -> None:
    normal = _directx("S05_diagonal")

    lectura = leer_convencion(normal, bordes=Bordes.PERIODICOS)

    assert lectura.residuo_directx == residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS, escala_y=1.0)
    assert lectura.residuo_opengl == residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS, escala_y=-1.0)


def test_leer_la_convencion_respeta_los_bordes() -> None:
    normal = _directx("S05_diagonal")

    lectura = leer_convencion(normal, bordes=Bordes.NEUMANN)
    periodica = leer_convencion(normal, bordes=Bordes.PERIODICOS)

    # Los DOS residuos respetan los bordes: si una lectura fijara los bordes por su
    # cuenta, la comparacion directx/opengl mezclaria dos dominios distintos.
    assert lectura.residuo_directx == residuo_de_integrabilidad(normal, bordes=Bordes.NEUMANN, escala_y=1.0)
    assert lectura.residuo_opengl == residuo_de_integrabilidad(normal, bordes=Bordes.NEUMANN, escala_y=-1.0)
    assert lectura.residuo_directx != periodica.residuo_directx
    assert lectura.residuo_opengl != periodica.residuo_opengl


def test_leer_la_convencion_rechaza_una_normal_invalida_con_el_error_del_integrador() -> None:
    with pytest.raises(EntradaInvalidaError):
        leer_convencion(np.zeros((8, 8)), bordes=Bordes.PERIODICOS)


@pytest.mark.parametrize("caso", sorted(DETECTABLES))
def test_un_archivo_directx_y_el_mismo_con_g_invertido_se_distinguen_en_los_casos_detectables(caso: str) -> None:
    normal = _directx(caso)

    como_directx = leer_convencion(normal, bordes=Bordes.PERIODICOS)
    como_opengl = leer_convencion(_con_g_invertido(normal), bordes=Bordes.PERIODICOS)

    assert (como_directx.elegida, como_opengl.elegida) == ("directx", "opengl")
    assert como_directx.margen_relativo > 0.99
    assert como_opengl.margen_relativo > 0.99


# ---------------------------------------------------------------------------
# El mecanismo: la derivada mixta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caso", IDS_PERIODICOS)
def test_el_residuo_con_la_convencion_equivocada_es_exactamente_la_participacion_de_la_derivada_mixta(
    caso: str,
) -> None:
    lectura = leer_convencion(_directx(caso), bordes=Bordes.PERIODICOS)

    assert lectura.residuo_directx == pytest.approx(0.0, abs=1e-12)
    assert lectura.residuo_opengl == pytest.approx(
        _residuo_esperado_con_la_convencion_equivocada(_altura(caso)), abs=1e-12
    )


def test_los_casos_detectables_son_los_que_tienen_derivada_mixta_y_la_brecha_es_de_mas_de_diez_ordenes() -> None:
    participacion = {}
    for caso in IDS_PERIODICOS:
        a, m, b = _participacion_de_la_derivada_mixta(_altura(caso))
        participacion[caso] = 0.0 if a + 2.0 * m + b == 0.0 else m / (a + 2.0 * m + b)

    detectables = {caso for caso, valor in participacion.items() if valor > 1e-6}

    assert detectables == DETECTABLES
    assert max(participacion[caso] for caso in NO_DETECTABLES) < 1e-12
    assert min(participacion[caso] for caso in DETECTABLES) > 1e-2


@pytest.mark.parametrize("caso", sorted(NO_DETECTABLES))
def test_los_no_detectables_limpios_se_abstienen_aunque_el_redondeo_los_separe(caso: str) -> None:
    # S04, S10, S11 y S14 dan residuos ~1e-15 distintos entre si (margen relativo de hasta 0.8): sin un piso
    # absoluto, esa diferencia de redondeo se leeria como una convencion con mucha confianza.
    for archivo in (_directx(caso), _con_g_invertido(_directx(caso))):
        lectura = leer_convencion(archivo, bordes=Bordes.PERIODICOS)

        assert max(lectura.residuo_directx, lectura.residuo_opengl) <= PISO_NUMERICO
        assert (lectura.tiene_senal, lectura.margen_relativo, lectura.elegida) == (False, 0.0, None)


@pytest.mark.parametrize("caso", sorted(DETECTABLES))
def test_el_piso_numerico_deja_pasar_la_senal_de_los_detectables(caso: str) -> None:
    lectura = leer_convencion(_directx(caso), bordes=Bordes.PERIODICOS)

    assert lectura.tiene_senal is True
    assert max(lectura.residuo_directx, lectura.residuo_opengl) > 1e6 * PISO_NUMERICO


# ---------------------------------------------------------------------------
# Lo que NINGUNA métrica puede resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caso", IDS_PERIODICOS)
def test_invertir_x_e_y_a_la_vez_niega_la_altura_exacta_y_deja_identicas_las_metricas(caso: str) -> None:
    normal = _directx(caso)
    invertida = _con_x_e_y_invertidos(normal)

    altura = integrar(normal, bordes=Bordes.PERIODICOS).altura
    altura_invertida = integrar(invertida, bordes=Bordes.PERIODICOS).altura

    assert np.array_equal(altura_invertida, -altura)
    assert residuo_de_integrabilidad(invertida, bordes=Bordes.PERIODICOS) == residuo_de_integrabilidad(
        normal, bordes=Bordes.PERIODICOS
    )
    assert razon_de_costura(altura_invertida) == razon_de_costura(altura)
    assert (
        acuerdo_normal(invertida, altura_invertida, bordes=Bordes.PERIODICOS).p95_grados
        == acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS).p95_grados
    )
    assert leer_convencion(invertida, bordes=Bordes.PERIODICOS) == leer_convencion(normal, bordes=Bordes.PERIODICOS)


@pytest.mark.parametrize("escala", [0.25, 2.0, 10.0])
@pytest.mark.parametrize("caso", [c for c in IDS_PERIODICOS if c != "S01_flat"])
def test_una_escala_uniforme_equivocada_no_cambia_la_altura_normalizada(caso: str, escala: float) -> None:
    normal = _directx(caso)

    referencia = normalizar_altura(integrar(normal, bordes=Bordes.PERIODICOS).altura, ModoNormalizacion.MIN_MAX)
    escalada = normalizar_altura(
        integrar(normal, bordes=Bordes.PERIODICOS, escala_x=escala, escala_y=escala).altura, ModoNormalizacion.MIN_MAX
    )

    assert np.allclose(escalada, referencia, atol=1e-12, rtol=0.0)


def test_una_relacion_equivocada_entre_las_escalas_de_x_e_y_cambia_la_altura_salvo_que_todos_los_modos_compartan_direccion() -> (
    None
):
    """Con ``escala_y != escala_x`` cada modo de Fourier se multiplica por ``(wx² + wy²/s) / (wx² + wy²)``, que depende
    de su DIRECCIÓN: la forma solo se conserva si todos los modos apuntan igual (estructuras 1D o una onda plana)."""
    cambio = {}
    for caso in IDS_PERIODICOS:
        if caso == "S01_flat":
            continue
        normal = _directx(caso)
        isotropa = normalizar_altura(integrar(normal, bordes=Bordes.PERIODICOS).altura, ModoNormalizacion.MIN_MAX)
        anisotropa = normalizar_altura(
            integrar(normal, bordes=Bordes.PERIODICOS, escala_x=1.0, escala_y=2.0).altura, ModoNormalizacion.MIN_MAX
        )
        cambio[caso] = float(np.abs(anisotropa - isotropa).max())

    invariantes = {caso for caso, valor in cambio.items() if valor < 1e-12}

    assert invariantes == MODOS_EN_UNA_SOLA_DIRECCION
    assert min(valor for caso, valor in cambio.items() if caso not in invariantes) > 1e-2


# ---------------------------------------------------------------------------
# Ruido y cuantización: qué sobrevive (afirmaciones cualitativas, con margen)
# ---------------------------------------------------------------------------


def _lecturas_de_los_detectables(escenario) -> list[tuple[LecturaDeConvencion, str]]:  # type: ignore[no-untyped-def]
    """Por cada caso detectable, el archivo DirectX y el mismo con G invertido, ya degradados como el escenario."""
    resultado = []
    for caso in sorted(DETECTABLES):
        degradada = escenario(_directx(caso), caso)
        resultado.append((leer_convencion(degradada, bordes=Bordes.PERIODICOS), "directx"))
        resultado.append((leer_convencion(_con_g_invertido(degradada), bordes=Bordes.PERIODICOS), "opengl"))
    return resultado


def _aciertos(lecturas: list[tuple[LecturaDeConvencion, str]]) -> float:
    return sum(lectura.elegida == real for lectura, real in lecturas) / len(lecturas)


def _margenes_de_los_no_detectables(escenario) -> list[float]:  # type: ignore[no-untyped-def]
    margenes = []
    for caso in sorted(NO_DETECTABLES - {"S01_flat"}):
        degradada = escenario(_directx(caso), caso)
        for archivo in (degradada, _con_g_invertido(degradada)):
            margenes.append(leer_convencion(archivo, bordes=Bordes.PERIODICOS).margen_relativo)
    return margenes


def test_sin_degradar_los_casos_detectables_se_leen_siempre_bien() -> None:
    assert _aciertos(_lecturas_de_los_detectables(lambda normal, caso: normal)) == 1.0


def test_con_ruido_de_un_255avo_los_detectables_se_separan_con_holgura_de_los_que_no_lo_son() -> None:
    def escenario(normal, caso):  # type: ignore[no-untyped-def]
        return perturbar_normal(normal, 1 / 255, caso)

    margenes_detectables = [lectura.margen_relativo for lectura, _ in _lecturas_de_los_detectables(escenario)]

    assert _aciertos(_lecturas_de_los_detectables(escenario)) == 1.0
    assert min(margenes_detectables) > 0.1
    assert max(_margenes_de_los_no_detectables(escenario)) < 0.05


def test_con_ruido_de_cuatro_255avos_ya_no_se_alcanza_el_95_por_ciento_del_criterio_de_exp_005() -> None:
    def escenario(normal, caso):  # type: ignore[no-untyped-def]
        return perturbar_normal(normal, 4 / 255, caso)

    assert _aciertos(_lecturas_de_los_detectables(escenario)) < 0.95


def test_con_cuantizacion_de_8_bits_y_dos_canales_como_bc5_tampoco_se_alcanza_el_95_por_ciento() -> None:
    def escenario(normal, caso):  # type: ignore[no-untyped-def]
        return cuantizar_normal(normal, 8, modelo=ModeloCuantizacion.DOS_CANALES_RECONSTRUYE_Z)

    assert _aciertos(_lecturas_de_los_detectables(escenario)) < 0.95


def test_con_cuantizacion_de_8_bits_y_tres_canales_la_lectura_sobrevive() -> None:
    def escenario(normal, caso):  # type: ignore[no-untyped-def]
        return cuantizar_normal(normal, 8, modelo=ModeloCuantizacion.TRES_CANALES)

    assert _aciertos(_lecturas_de_los_detectables(escenario)) == 1.0

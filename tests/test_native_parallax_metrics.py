"""NP-Q0a: métricas de calidad deterministas del generador nativo de parallax (ADR 0014 §2.5, tabla §I de A).

Son las métricas que se calculan SIN ground truth de altura —sirven igual en producción y en el benchmark de
NP-D0—: acuerdo de normal, residuo de integrabilidad, razón de costura, rango dinámico, razón de recorte
y conteo de no finitos. No deciden nada: NO hay umbrales ni gates acá (los umbrales se
DERIVAN del benchmark en NP-Q0b y nunca se fijan a mano, ADR 0014 §6).

Cómo se verifican:

- fixtures ANALÍTICOS (senos, cosenos, rampas) cuyo valor exacto se conoce sin correr el código;
- la razón de costura se compara con la del corpus de NP-D0 (misma matemática, dos módulos: el test enumera los
  17 casos para que no se separen);
- el escalar de alineación del acuerdo de normal se contrasta con un argmin por fuerza bruta, independiente de la
  fórmula cerrada.

Límite declarado: sobre normales sintéticas. Que estas métricas PREDIGAN el riesgo en texturas reales no está
demostrado —EXP-M2 midió lo contrario para señales solo-normal (mejor AUC 0.62)—, y por eso este PR no las
convierte en política.
"""

from __future__ import annotations

import math

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
from sky_claw.local.native_parallax.integrate import Bordes, EntradaInvalidaError, integrar
from sky_claw.local.native_parallax.metrics import (
    AcuerdoNormal,
    acuerdo_normal,
    contar_no_finitos,
    rango_dinamico,
    razon_de_costura,
    razon_de_recorte,
    residuo_de_integrabilidad,
)

TODOS = IDS_PERIODICOS + IDS_CONTROLES
GRILLAS = ((128, 128), (128, 256), (256, 128))
ANALITICOS = tuple(i for i in IDS_PERIODICOS if i not in ("S07_bumps", "S09_bricks", "S10_asymmetric"))


def _seno_x(n_filas: int = 64, n_cols: int = 64, k: int = 3, amplitud: float = 0.05) -> np.ndarray:
    x = np.arange(n_cols) / n_cols
    return np.tile(amplitud * np.sin(2.0 * np.pi * k * x), (n_filas, 1))


def _normal_plana(n_filas: int = 32, n_cols: int = 32) -> np.ndarray:
    normal = np.zeros((n_filas, n_cols, 3))
    normal[..., 2] = 1.0
    return normal


def _normal_desde_gradientes(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Normal ``normalize(-p, -q, 1)`` (escalas unitarias) construida a mano, sin pasar por el producto."""
    norma = np.sqrt(p * p + q * q + 1.0)
    return np.stack((-p / norma, -q / norma, 1.0 / norma), axis=-1)


# ---------------------------------------------------------------------------
# acuerdo_normal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caso_id", ANALITICOS)
def test_la_reconstruccion_exacta_da_acuerdo_perfecto(caso_id: str) -> None:
    normal = normal_desde_altura(generar_altura(caso_id, 128, 128))
    altura = integrar(normal, bordes=Bordes.PERIODICOS).altura

    resultado = acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS)

    assert resultado.media_grados < 1e-6
    assert resultado.p95_grados < 1e-6
    assert resultado.signo == 1
    # Una superficie plana no tiene escala recuperable (∇h = 0): se reporta 0 y el error angular sigue siendo 0.
    assert resultado.escala == pytest.approx(0.0 if caso_id == "S01_flat" else 1.0, abs=1e-9)


def test_la_escala_de_la_altura_se_reporta_y_no_cuenta_como_error() -> None:
    normal = normal_desde_altura(_seno_x())
    altura = integrar(normal, bordes=Bordes.PERIODICOS).altura

    triple = acuerdo_normal(normal, 3.0 * altura, bordes=Bordes.PERIODICOS)

    assert triple.escala == pytest.approx(1.0 / 3.0, abs=1e-9)
    assert triple.media_grados < 1e-6
    assert triple.signo == 1


def test_una_altura_invertida_se_absorbe_pero_el_signo_la_delata() -> None:
    # Un flip global de los gradientes produce rec = -ref EXACTO: el ajuste lo absorbe con a = -1, y la inversión
    # sistemática SOLO es visible por el signo (research/alignment.py, §34-Q10).
    normal = normal_desde_altura(_seno_x())
    altura = integrar(normal, bordes=Bordes.PERIODICOS).altura

    invertida = acuerdo_normal(normal, -altura, bordes=Bordes.PERIODICOS)

    assert invertida.signo == -1
    assert invertida.escala == pytest.approx(-1.0, abs=1e-9)
    assert invertida.media_grados < 1e-6


def test_una_altura_plana_no_tiene_escala_recuperable_y_el_error_es_la_pendiente_de_la_normal() -> None:
    angulo = math.radians(20.0)
    normal = _normal_plana()
    normal[..., 0] = math.sin(angulo)
    normal[..., 2] = math.cos(angulo)

    resultado = acuerdo_normal(normal, np.zeros((32, 32)), bordes=Bordes.PERIODICOS)

    assert resultado.escala == 0.0
    assert resultado.signo == 1
    assert resultado.media_grados == pytest.approx(20.0, abs=1e-9)
    assert resultado.mediana_grados == pytest.approx(20.0, abs=1e-9)
    assert resultado.p95_grados == pytest.approx(20.0, abs=1e-9)


@pytest.mark.parametrize("escalas", [(1.0, 1.0), (0.5, 2.0)], ids=["unitarias", "anisotropicas"])
def test_el_escalar_de_alineacion_es_el_argmin_de_la_formula_ponderada_por_fuerza_bruta(
    escalas: tuple[float, float],
) -> None:
    # Contraste independiente de la fórmula cerrada: se minimiza numéricamente sobre una grilla fina de escalas el
    # error ponderado por (nz·s)², que equivale a ajustar en el espacio de la normal.
    sx, sy = escalas
    altura_verdad = generar_altura("S06_multifreq", 128, 128)
    normal = normal_desde_altura(altura_verdad, sx, sy)
    altura = 0.7 * altura_verdad + 0.02 * np.random.default_rng(3).standard_normal((128, 128))
    p_alt, q_alt = espectral.gradientes_espectrales(np, altura)
    nz = normal[..., 2]
    objetivo_p, objetivo_q = -normal[..., 0] / (sx * nz), -normal[..., 1] / (sy * nz)
    candidatas = np.linspace(-3.0, 3.0, 3001)
    costos = [
        float(np.sum(nz**2 * (sx**2 * (a * p_alt - objetivo_p) ** 2 + sy**2 * (a * q_alt - objetivo_q) ** 2)))
        for a in candidatas
    ]
    mejor = float(candidatas[int(np.argmin(costos))])

    resultado = acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS, escala_x=sx, escala_y=sy)

    assert resultado.escala == pytest.approx(mejor, abs=2e-3)


def test_mas_ruido_en_la_altura_empeora_el_acuerdo_de_forma_monotona() -> None:
    normal = normal_desde_altura(generar_altura("S06_multifreq", 128, 128))
    base = integrar(normal, bordes=Bordes.PERIODICOS).altura
    rng = np.random.default_rng(11)
    ruido = rng.standard_normal(base.shape)

    medias = [
        acuerdo_normal(normal, base + sigma * ruido, bordes=Bordes.PERIODICOS).media_grados
        for sigma in (0.0, 0.001, 0.004, 0.016)
    ]

    assert medias[0] < 1e-6
    assert medias[0] < medias[1] < medias[2] < medias[3]


def test_un_pixel_con_nz_cero_no_envenena_la_escala() -> None:
    # Con nz = 0 el gradiente de la inversa vale ~1e6: un ajuste sin ponderar dejaría que ese píxel domine la
    # escala de TODA la imagen. Con peso nz² su aporte es ~1e-12.
    normal = normal_desde_altura(_seno_x())
    altura = integrar(normal, bordes=Bordes.PERIODICOS).altura
    envenenada = normal.copy()
    envenenada[10, 10] = (1.0, 0.0, 0.0)
    envenenada[20, 20] = (0.6, 0.0, -0.8)  # de espaldas: nz < 0 también va al piso en los pesos

    resultado = acuerdo_normal(envenenada, altura, bordes=Bordes.PERIODICOS)

    assert resultado.escala == pytest.approx(1.0, abs=1e-6)
    assert resultado.mediana_grados < 1e-6


def test_neumann_reconoce_un_coseno_de_derivada_normal_nula() -> None:
    n_filas, n_cols = 64, 64
    x = (np.arange(n_cols) + 0.5) / n_cols
    altura = np.tile(0.05 * np.cos(np.pi * x), (n_filas, 1))
    pendiente = np.tile(-0.05 * np.pi * np.sin(np.pi * x), (n_filas, 1))
    normal = _normal_desde_gradientes(pendiente, np.zeros_like(pendiente))

    neumann = acuerdo_normal(normal, altura, bordes=Bordes.NEUMANN)
    periodico = acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS)

    assert neumann.escala == pytest.approx(1.0, abs=2e-2)
    assert neumann.media_grados < 0.1
    # Con la hipótesis de borde equivocada la derivada periódica del coseno (que no cierra el toro) se ensucia.
    assert periodico.media_grados > 10 * neumann.media_grados


def test_el_resultado_es_inmutable_y_con_tipos_nativos() -> None:
    normal = normal_desde_altura(_seno_x())
    altura = integrar(normal, bordes=Bordes.PERIODICOS).altura

    resultado = acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS)

    assert isinstance(resultado, AcuerdoNormal)
    assert type(resultado.media_grados) is float
    assert type(resultado.mediana_grados) is float
    assert type(resultado.p95_grados) is float
    assert type(resultado.escala) is float
    assert type(resultado.signo) is int
    with pytest.raises(AttributeError):
        resultado.media_grados = 0.0  # type: ignore[misc]


def test_las_estadisticas_son_coherentes_entre_si() -> None:
    normal = normal_desde_altura(generar_altura("S15_periodic_noise", 128, 128))
    base = integrar(normal, bordes=Bordes.PERIODICOS).altura
    ruido = np.random.default_rng(5).standard_normal(base.shape)

    resultado = acuerdo_normal(normal, base + 0.003 * ruido, bordes=Bordes.PERIODICOS)

    assert 0.0 < resultado.mediana_grados <= resultado.p95_grados
    assert resultado.media_grados > 0.0


def test_media_mediana_y_p95_son_las_de_una_mezcla_angular_conocida() -> None:
    # 360 píxeles inclinados 10° y 40 inclinados 30°, sobre una altura plana (escala 0): el error de cada píxel es su
    # propia inclinación. Media = 0.9·10 + 0.1·30 = 12°, mediana = 10° y el percentil 95 cae dentro del bloque de 30°.
    n = 20
    angulos = np.full((n, n), 10.0)
    angulos[:, :2] = 30.0
    normal = _normal_plana(n, n)
    normal[..., 0] = np.sin(np.radians(angulos))
    normal[..., 2] = np.cos(np.radians(angulos))

    resultado = acuerdo_normal(normal, np.zeros((n, n)), bordes=Bordes.PERIODICOS)

    assert resultado.media_grados == pytest.approx(12.0, abs=1e-9)
    assert resultado.mediana_grados == pytest.approx(10.0, abs=1e-9)
    assert resultado.p95_grados == pytest.approx(30.0, abs=1e-9)


@pytest.mark.parametrize("forma", [(64, 64), (48, 96), (96, 48)], ids=lambda f: f"{f[0]}x{f[1]}")
def test_neumann_reconoce_un_campo_de_derivada_normal_nula_en_los_dos_ejes(forma: tuple[int, int]) -> None:
    # h = a·cos(πx)·cos(πy): la extensión par la cierra en un toro de período 2, y las dos derivadas son exactas.
    # Las formas rectangulares en los dos sentidos distinguen filas de columnas en las muestras por unidad.
    n_filas, n_cols = forma
    x = ((np.arange(n_cols) + 0.5) / n_cols)[None, :]
    y = ((np.arange(n_filas) + 0.5) / n_filas)[:, None]
    a = 0.05
    altura = a * np.cos(np.pi * x) * np.cos(np.pi * y)
    p = -a * np.pi * np.sin(np.pi * x) * np.cos(np.pi * y)
    q = -a * np.pi * np.cos(np.pi * x) * np.sin(np.pi * y)
    normal = _normal_desde_gradientes(p, q)

    neumann = acuerdo_normal(normal, altura, bordes=Bordes.NEUMANN)
    periodico = acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS)

    assert neumann.media_grados < 0.1
    assert neumann.escala == pytest.approx(1.0, abs=2e-2)
    assert periodico.media_grados > 10 * neumann.media_grados
    assert residuo_de_integrabilidad(normal, bordes=Bordes.NEUMANN) < 1e-6


@pytest.mark.parametrize("bordes", list(Bordes), ids=lambda b: b.name.lower())
def test_una_normal_no_unitaria_da_el_mismo_acuerdo_que_su_version_unitaria(bordes: Bordes) -> None:
    # Las normales cuantizadas a 2 canales (el contrato de BC5) pueden no ser unitarias: el ángulo es de DIRECCIÓN.
    altura_verdad = generar_altura("S06_multifreq", 128, 128)
    normal = normal_desde_altura(altura_verdad)
    altura = integrar(normal, bordes=bordes).altura + 0.003 * np.random.default_rng(9).standard_normal((128, 128))

    unitaria = acuerdo_normal(normal, altura, bordes=bordes)
    triple = acuerdo_normal(3.0 * normal, altura, bordes=bordes)

    assert triple.media_grados == pytest.approx(unitaria.media_grados, rel=1e-9)
    assert triple.escala == pytest.approx(unitaria.escala, rel=1e-9)


@pytest.mark.parametrize("bordes", list(Bordes), ids=lambda b: b.name.lower())
@pytest.mark.parametrize(
    "caso",
    ["normal_2d", "normal_float32", "normal_nan", "normal_cero", "altura_otra_forma", "altura_nan", "altura_lista"],
)
def test_acuerdo_normal_rechaza_entradas_invalidas(caso: str, bordes: Bordes) -> None:
    normal = normal_desde_altura(_seno_x())
    altura = np.zeros((64, 64))
    entradas = {
        "normal_2d": (normal[..., 0], altura),
        "normal_float32": (normal.astype(np.float32), altura),
        "normal_nan": (np.where(np.eye(64)[..., None] > 0, np.nan, normal), altura),
        "normal_cero": (np.where(np.eye(64)[..., None] > 0, 0.0, normal), altura),
        "altura_otra_forma": (normal, np.zeros((32, 64))),
        "altura_nan": (normal, np.where(np.eye(64) > 0, np.nan, altura)),
        "altura_lista": (normal, altura.tolist()),
    }
    entrada_normal, entrada_altura = entradas[caso]

    with pytest.raises(EntradaInvalidaError):
        acuerdo_normal(entrada_normal, entrada_altura, bordes=bordes)  # type: ignore[arg-type]


@pytest.mark.parametrize("bordes", ["periodicos", None, 1])
def test_acuerdo_normal_rechaza_bordes_invalidos(bordes: object) -> None:
    normal = normal_desde_altura(_seno_x())

    with pytest.raises(EntradaInvalidaError):
        acuerdo_normal(normal, np.zeros((64, 64)), bordes=bordes)  # type: ignore[arg-type]


def test_acuerdo_normal_aplica_la_guarda_de_ejes_del_integrador() -> None:
    # Con bordes periódicos un eje de 2 muestras solo tiene Nyquist: la derivada sería nula sin avisar.
    normal = _normal_plana(2, 8)

    with pytest.raises(EntradaInvalidaError, match="Nyquist"):
        acuerdo_normal(normal, np.zeros((2, 8)), bordes=Bordes.PERIODICOS)

    acuerdo_normal(normal, np.zeros((2, 8)), bordes=Bordes.NEUMANN)  # Neumann resuelve sobre 4 muestras: se acepta


@pytest.mark.parametrize("malo", [0.0, math.nan, math.inf, True, "1", None])
def test_acuerdo_normal_rechaza_escalas_invalidas(malo: object) -> None:
    normal = normal_desde_altura(_seno_x())

    with pytest.raises(EntradaInvalidaError):
        acuerdo_normal(normal, np.zeros((64, 64)), bordes=Bordes.PERIODICOS, escala_x=malo)  # type: ignore[arg-type]
    with pytest.raises(EntradaInvalidaError):
        acuerdo_normal(normal, np.zeros((64, 64)), bordes=Bordes.PERIODICOS, escala_y=malo)  # type: ignore[arg-type]


def test_acuerdo_normal_sigue_las_escalas_anisotropicas() -> None:
    altura_verdad = generar_altura("S06_multifreq", 128, 128)
    normal = normal_desde_altura(altura_verdad, 0.5, 2.0)
    altura = integrar(normal, bordes=Bordes.PERIODICOS, escala_x=0.5, escala_y=2.0).altura

    con_escalas = acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS, escala_x=0.5, escala_y=2.0)
    sin_escalas = acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS)

    assert con_escalas.media_grados < 1e-6
    assert sin_escalas.media_grados > 1.0  # usar la convención equivocada se nota


# ---------------------------------------------------------------------------
# residuo_de_integrabilidad
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caso_id", ANALITICOS)
def test_los_gradientes_de_una_altura_real_son_integrables(caso_id: str) -> None:
    # Un campo que ES el gradiente de una altura tiene rotor nulo: ∂p/∂y = ∂q/∂x.
    normal = normal_desde_altura(generar_altura(caso_id, 128, 128))

    assert residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS) < 1e-6


def test_un_campo_separable_tiene_residuo_cero_aunque_su_derivada_cruzada_sea_ruido() -> None:
    # h = f(x) + g(y) tiene ∂²h/∂x∂y = 0: p_y y q_x son ruido de redondeo (~1e-12). Normalizar el residuo por esas
    # mismas magnitudes daba ~0.5 justo acá; se normaliza por el Hessiano completo (p_x y q_y NO son nulas).
    normal = normal_desde_altura(generar_altura("S11_highfreq", 128, 128))

    assert residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS) < 1e-9


def test_un_campo_con_rotor_puro_tiene_residuo_uno() -> None:
    # p = c·sin(2π·y), q = 0 → ∂p/∂y = 2π·c·cos(2π·y) y ∂q/∂x = 0: |p_y − q_x| / (|p_y| + |q_x|) = 1 exacto.
    n = 64
    y = (np.arange(n) / n)[:, None]
    p = np.tile(0.05 * np.sin(2.0 * np.pi * y), (1, n))
    normal = _normal_desde_gradientes(p, np.zeros_like(p))

    assert residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("a", [0.03, 0.06])
def test_el_residuo_pondera_las_cuatro_derivadas_del_hessiano(a: float) -> None:
    # Con q = b·f(y) y p = a·f(y) (f = sin 2π·): rotor |p_y| = 4a y curvatura |q_y| = 4b → residuo = a / (a + b).
    # Con las mismas amplitudes en x: rotor |q_x| = 4b y curvatura |p_x| = 4a → residuo = b / (a + b).
    # Un denominador al que le falte p_x o q_y no da estos valores.
    b = 0.045
    n = 128
    seno = np.sin(2.0 * np.pi * np.arange(n) / n)
    en_y = (np.tile(seno[:, None], (1, n)) * a, np.tile(seno[:, None], (1, n)) * b)
    en_x = (np.tile(seno[None, :], (n, 1)) * a, np.tile(seno[None, :], (n, 1)) * b)

    residuo_y = residuo_de_integrabilidad(_normal_desde_gradientes(*en_y), bordes=Bordes.PERIODICOS)
    residuo_x = residuo_de_integrabilidad(_normal_desde_gradientes(*en_x), bordes=Bordes.PERIODICOS)

    assert residuo_y == pytest.approx(a / (a + b), abs=2e-3)
    assert residuo_x == pytest.approx(b / (a + b), abs=2e-3)


def test_dos_senos_cruzados_dan_el_residuo_analitico_2_sobre_pi() -> None:
    # p = c·sin(2π·y), q = -c·sin(2π·x): p_y − q_x = 2π·c·(cos 2πy + cos 2πx). Con E|cos a + cos b| = 8/π²:
    # numerador 16c/π, denominador 4c + 4c → residuo = 2/π.
    n = 128
    x = (np.arange(n) / n)[None, :]
    y = (np.arange(n) / n)[:, None]
    p = np.tile(0.03 * np.sin(2.0 * np.pi * y), (1, n))
    q = np.tile(-0.03 * np.sin(2.0 * np.pi * x), (n, 1))
    normal = _normal_desde_gradientes(p, q)

    assert residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS) == pytest.approx(2.0 / math.pi, abs=5e-3)


def test_una_normal_plana_tiene_residuo_cero_y_no_nan() -> None:
    for bordes in Bordes:
        assert residuo_de_integrabilidad(_normal_plana(), bordes=bordes) == 0.0


def test_neumann_ve_un_campo_integrable_y_un_rotor_con_su_valor_analitico() -> None:
    n = 128
    x = ((np.arange(n) + 0.5) / n)[None, :]
    y = ((np.arange(n) + 0.5) / n)[:, None]
    # ∇(cos πx): p = -π·sin(πx), q = 0 → sin rotor.
    p_integrable = np.tile(-0.05 * np.pi * np.sin(np.pi * x), (n, 1))
    integrable = _normal_desde_gradientes(p_integrable, np.zeros_like(p_integrable))
    # p = c·sin(2πx)·cos(πy), q = 0 respeta la paridad del espejo (impar en x, par en y) y tiene rotor puro:
    # p_y = -π·c·sin(2πx)·sin(πy), q_x = 0, p_x = 2π·c·cos(2πx)·cos(πy). Con E|sen·sen| = E|cos·cos| = 4/π² sobre
    # el período: |p_y| = 4c/π y |p_x| = 8c/π → residuo = (4/π) / (4/π + 8/π) = 1/3.
    p_rotor = 0.05 * np.sin(2.0 * np.pi * x) * np.cos(np.pi * y)
    con_rotor = _normal_desde_gradientes(p_rotor, np.zeros_like(p_rotor))

    assert residuo_de_integrabilidad(integrable, bordes=Bordes.NEUMANN) < 1e-6
    assert residuo_de_integrabilidad(con_rotor, bordes=Bordes.NEUMANN) == pytest.approx(1.0 / 3.0, abs=2e-2)


def test_el_residuo_de_integrabilidad_usa_las_escalas() -> None:
    normal = normal_desde_altura(generar_altura("S06_multifreq", 128, 128), 0.5, 2.0)

    bien = residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS, escala_x=0.5, escala_y=2.0)
    mal = residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS)

    assert bien < 1e-6
    assert mal > 0.1


@pytest.mark.parametrize("bordes", list(Bordes), ids=lambda b: b.name.lower())
def test_residuo_de_integrabilidad_rechaza_entradas_invalidas(bordes: Bordes) -> None:
    for malo in (np.zeros((8, 8)), _normal_plana().astype(np.float32), np.full((8, 8, 3), np.nan), None):
        with pytest.raises(EntradaInvalidaError):
            residuo_de_integrabilidad(malo, bordes=bordes)  # type: ignore[arg-type]
    for malo in (0.0, math.nan, True, "1"):
        with pytest.raises(EntradaInvalidaError):
            residuo_de_integrabilidad(_normal_plana(), bordes=bordes, escala_x=malo)  # type: ignore[arg-type]
        with pytest.raises(EntradaInvalidaError):
            residuo_de_integrabilidad(_normal_plana(), bordes=bordes, escala_y=malo)  # type: ignore[arg-type]
        with pytest.raises(EntradaInvalidaError):
            residuo_de_integrabilidad(_normal_plana(), bordes=bordes, nz_piso=malo)  # type: ignore[arg-type]


def test_residuo_de_integrabilidad_rechaza_bordes_invalidos_y_ejes_de_dos_muestras() -> None:
    with pytest.raises(EntradaInvalidaError):
        residuo_de_integrabilidad(_normal_plana(), bordes="periodicos")  # type: ignore[arg-type]
    with pytest.raises(EntradaInvalidaError, match="Nyquist"):
        residuo_de_integrabilidad(_normal_plana(2, 8), bordes=Bordes.PERIODICOS)


# ---------------------------------------------------------------------------
# razon_de_costura: la matemática del corpus de NP-D0 en el lado de producción
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grilla", GRILLAS, ids=lambda g: f"{g[0]}x{g[1]}")
@pytest.mark.parametrize("caso_id", TODOS)
def test_la_razon_de_costura_es_la_del_corpus_en_los_17_casos(caso_id: str, grilla: tuple[int, int]) -> None:
    # Misma definición en dos módulos (el corpus caracteriza el ground truth; esta mide la salida): el test
    # enumera el corpus entero para que no se separen.
    altura = generar_altura(caso_id, *grilla)

    assert razon_de_costura(altura) == razon_borde(altura)


def test_una_rampa_tiene_una_costura_de_n_menos_uno_veces_el_paso() -> None:
    n = 64
    rampa = np.tile(np.arange(n) / (n - 1), (n, 1))

    assert razon_de_costura(rampa) == pytest.approx(n - 1, rel=1e-12)


def test_una_altura_constante_tiene_razon_uno() -> None:
    assert razon_de_costura(np.full((16, 16), 0.3)) == 1.0


def test_un_campo_que_arranca_plano_pero_no_es_periodico_no_se_declara_periodico() -> None:
    # Defecto latente de research que el corpus ya corrigió (revisión de Codex en #758): filas [0, 0, ..., 0, 1].
    altura = np.zeros((16, 16))
    altura[-1, :] = 1.0

    assert razon_de_costura(altura) > 2.0


def test_razon_de_costura_rechaza_alturas_invalidas() -> None:
    for malo in (np.zeros(8), np.zeros((8, 8), dtype=np.float32), np.full((8, 8), np.nan), np.zeros((1, 8)), None):
        with pytest.raises(EntradaInvalidaError):
            razon_de_costura(malo)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# rango_dinamico
# ---------------------------------------------------------------------------


def test_el_rango_dinamico_de_una_rampa_es_la_diferencia_de_los_percentiles() -> None:
    rampa = np.linspace(0.0, 1.0, 100 * 100).reshape(100, 100)

    assert rango_dinamico(rampa, percentil_bajo=2.0, percentil_alto=98.0) == pytest.approx(0.96, abs=1e-3)
    assert rango_dinamico(rampa, percentil_bajo=0.0, percentil_alto=100.0) == pytest.approx(1.0, abs=1e-12)


def test_el_rango_dinamico_de_una_altura_plana_es_cero() -> None:
    assert rango_dinamico(np.full((8, 8), 0.5), percentil_bajo=2.0, percentil_alto=98.0) == 0.0


def test_el_rango_dinamico_no_trae_percentiles_por_defecto() -> None:
    # ADR 0014 §4/§6: un p2/p98 escrito a mano en el código es justo lo que se prohíbe; los elige el que llama.
    with pytest.raises(TypeError):
        rango_dinamico(np.zeros((8, 8)))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        rango_dinamico(np.zeros((8, 8)), percentil_bajo=2.0)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("bajo", "alto"),
    [(None, 98.0), (2.0, None), (50.0, 50.0), (60.0, 40.0), (-1.0, 98.0), (2.0, 101.0), (math.nan, 98.0), (True, 98.0)],
)
def test_el_rango_dinamico_rechaza_percentiles_invalidos(bajo: object, alto: object) -> None:
    with pytest.raises(EntradaInvalidaError):
        rango_dinamico(np.zeros((8, 8)), percentil_bajo=bajo, percentil_alto=alto)  # type: ignore[arg-type]


def test_el_rango_dinamico_rechaza_alturas_invalidas() -> None:
    for malo in (np.zeros(8), np.full((8, 8), np.inf), np.zeros((8, 8), dtype=np.float32), None):
        with pytest.raises(EntradaInvalidaError):
            rango_dinamico(malo, percentil_bajo=2.0, percentil_alto=98.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# razon_de_recorte
# ---------------------------------------------------------------------------


def test_la_razon_de_recorte_cuenta_los_pixeles_exactamente_en_cero_o_uno() -> None:
    altura = np.array([[0.0, 0.5, 1.0, 0.25], [0.0, 0.0, 0.75, 1.0]])

    assert razon_de_recorte(altura) == pytest.approx(5.0 / 8.0)


def test_una_altura_sin_recorte_tiene_razon_cero() -> None:
    assert razon_de_recorte(np.full((4, 4), 0.5)) == 0.0


def test_una_altura_entera_recortada_tiene_razon_uno() -> None:
    assert razon_de_recorte(np.zeros((4, 4))) == 1.0
    assert razon_de_recorte(np.ones((4, 4))) == 1.0


@pytest.mark.parametrize("malo", [-0.01, 1.01, math.nan, math.inf])
def test_razon_de_recorte_exige_una_altura_normalizada(malo: float) -> None:
    altura = np.full((4, 4), 0.5)
    altura[1, 1] = malo

    with pytest.raises(EntradaInvalidaError):
        razon_de_recorte(altura)


def test_razon_de_recorte_rechaza_alturas_invalidas() -> None:
    for malo in (np.zeros(8), np.zeros((4, 4), dtype=np.float32), None, [[0.5, 0.5]]):
        with pytest.raises(EntradaInvalidaError):
            razon_de_recorte(malo)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# contar_no_finitos
# ---------------------------------------------------------------------------


def test_contar_no_finitos_suma_nan_e_infinitos_de_todos_los_campos() -> None:
    a = np.array([[1.0, math.nan], [math.inf, 2.0]])
    b = np.array([-math.inf, 0.0, math.nan])

    assert contar_no_finitos(a) == 2
    assert contar_no_finitos(a, b) == 4
    assert contar_no_finitos(np.zeros((3, 3))) == 0


def test_contar_no_finitos_devuelve_un_entero_nativo() -> None:
    assert type(contar_no_finitos(np.zeros(4))) is int


def test_contar_no_finitos_exige_campos_flotantes_y_al_menos_uno() -> None:
    with pytest.raises(EntradaInvalidaError):
        contar_no_finitos()
    for malo in (np.zeros(4, dtype=np.int64), [1.0, 2.0], None, "x"):
        with pytest.raises(EntradaInvalidaError):
            contar_no_finitos(malo)  # type: ignore[arg-type]
    with pytest.raises(EntradaInvalidaError):
        contar_no_finitos(np.zeros(4), [1.0])  # type: ignore[arg-type]

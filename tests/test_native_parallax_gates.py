"""NP-Q0b: el gate de calidad del generador nativo de parallax es FAIL-CLOSED (ADR 0014 §2.5 y §6).

Qué se verifica y con qué:

- **Sin umbrales validados, todo va a revisión.** Hoy el repo no versiona ningún registro de umbrales
  (``UMBRALES_VIGENTES is None``, congelado por igualdad) y hasta la mejor normal sintética termina en ``REVIEW``:
  EXP-M2 midió que las señales solo-normal NO predicen el riesgo en texturas authored reales (mejor AUC 0.62).
- **``AUTO_ACCEPT`` exige evidencia contra un corpus real, calculada y no declarada.** El registro lleva los
  CONTEOS crudos de la validación y el gate calcula la cota superior binomial de los falsos aceptos; no hay un
  booleano ``validado`` que alguien pueda poner en verdadero. La cota se contrasta con el valor exacto de la
  función de distribución binomial calculada en aritmética racional, independiente de la implementación.
- **La tabla de decisión se enumera**: cada límite, cada métrica no finita y cada forma de validación
  insuficiente tienen su caso; un campo nuevo en el registro rompe la enumeración hasta que se le escribe su caso.
- **El gate se mantiene sincronizado con ``metrics.py``**: una métrica pública nueva rompe el ancla hasta que se
  decide si es gateada o no y por qué.

Límites declarados (no verificados acá): que un corpus declarado "real" lo sea (el código solo excluye el corpus
sintético que conoce) y que subir ``VERSION_DE_METRICAS`` al cambiar una definición sea más que una convención
(los valores analíticos de ``test_native_parallax_metrics.py`` son las anclas de esas definiciones).
"""

from __future__ import annotations

import dataclasses
import math
from fractions import Fraction

import pytest

from sky_claw.local.native_parallax import gates, metrics
from sky_claw.local.native_parallax.bench.sintetico import (
    ESPECIFICACION_V1,
    generar_altura,
    huella_especificacion,
    normal_desde_altura,
)
from sky_claw.local.native_parallax.gates import (
    METRICAS_GATEADAS,
    METRICAS_NO_GATEADAS,
    UMBRALES_VIGENTES,
    VERSION_DE_METRICAS,
    Decision,
    LimitesDeAutoaceptacion,
    MetricasDeCalidad,
    MetricasInvalidasError,
    Motivo,
    Umbrales,
    UmbralesInvalidosError,
    ValidacionContraCorpusReal,
    Veredicto,
    cota_superior_binomial,
    decidir,
    decidir_con,
)
from sky_claw.local.native_parallax.integrate import Bordes, integrar
from sky_claw.local.native_parallax.metrics import (
    AcuerdoNormal,
    acuerdo_normal,
    contar_no_finitos,
    razon_de_costura,
    residuo_de_integrabilidad,
)

HUELLA_REAL = "ab" * 32
HUELLA_DERIVACION = "cd" * 32


def _validacion(**cambios: object) -> ValidacionContraCorpusReal:
    """Una validación que PASA: 0 malas en 600 aceptadas (cota 95 % ≈ 0.005) y 40 % de revisión."""
    base: dict[str, object] = {
        "id_del_corpus": "corpus-real-de-prueba",
        "huella_del_corpus": HUELLA_REAL,
        "criterio_de_etiqueta": "criterio de etiqueta de prueba",
        "n_total": 1000,
        "n_aceptadas": 600,
        "n_aceptadas_malas": 0,
        "confianza": 0.95,
        "alpha_objetivo": 0.01,
        "tasa_de_revision_maxima": 0.5,
    }
    base.update(cambios)
    return ValidacionContraCorpusReal(**base)  # type: ignore[arg-type]


def _limites(**cambios: float) -> LimitesDeAutoaceptacion:
    base = {"acuerdo_p95_grados": 2.0, "residuo_de_integrabilidad": 0.05, "razon_de_costura": 2.0}
    base.update(cambios)
    return LimitesDeAutoaceptacion(**base)


def _umbrales(**cambios: object) -> Umbrales:
    """Un registro VÁLIDO y VALIDADO contra un corpus real: el único camino a ``AUTO_ACCEPT``."""
    base: dict[str, object] = {
        "etiqueta": "umbrales-de-prueba",
        "version_de_metricas": VERSION_DE_METRICAS,
        "huella_de_derivacion": HUELLA_DERIVACION,
        "signo_esperado": 1,
        "limites": _limites(),
        "validacion": _validacion(),
    }
    base.update(cambios)
    return Umbrales(**base)  # type: ignore[arg-type]


def _metricas(**cambios: float) -> MetricasDeCalidad:
    base = {
        "acuerdo_p95_grados": 1.0,
        "signo": 1,
        "residuo_de_integrabilidad": 0.01,
        "razon_de_costura": 1.0,
        "no_finitos": 0,
    }
    base.update(cambios)
    return MetricasDeCalidad(**base)


CAMPOS_DE_LIMITE = tuple(campo.name for campo in dataclasses.fields(LimitesDeAutoaceptacion))
NO_FINITOS = (math.nan, math.inf, -math.inf)


# ---------------------------------------------------------------------------
# Fail-closed: sin umbrales validados, todo va a revisión
# ---------------------------------------------------------------------------


def test_el_repo_no_versiona_ningun_registro_de_umbrales() -> None:
    # Cambiar esto es una decisión de política que exige evidencia contra un corpus real, no un detalle de código.
    assert UMBRALES_VIGENTES is None


def test_sin_umbrales_hasta_la_metrica_perfecta_va_a_revision() -> None:
    decision = decidir_con(_metricas(acuerdo_p95_grados=0.0, residuo_de_integrabilidad=0.0), None)

    assert decision == Decision(Veredicto.REVIEW, (Motivo.SIN_UMBRALES,), ())


def test_decidir_sin_registro_vigente_va_a_revision() -> None:
    assert decidir(_metricas()) == Decision(Veredicto.REVIEW, (Motivo.SIN_UMBRALES,), ())


def test_decidir_lee_los_umbrales_vigentes_al_momento_de_decidir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates, "UMBRALES_VIGENTES", _umbrales())

    assert decidir(_metricas()) == Decision(Veredicto.AUTO_ACCEPT, (), ())


def test_una_normal_sintetica_limpia_va_a_revision_de_punta_a_punta() -> None:
    """El producto de hoy: ni la normal más limpia del corpus se acepta sola, porque no hay umbrales validados."""
    altura = generar_altura("S02_sine_x", 128, 128)
    normal = normal_desde_altura(altura)
    integrada = integrar(normal, bordes=Bordes.PERIODICOS).altura
    metricas = MetricasDeCalidad.desde_metricas(
        acuerdo_normal(normal, integrada, bordes=Bordes.PERIODICOS),
        residuo_de_integrabilidad=residuo_de_integrabilidad(normal, bordes=Bordes.PERIODICOS),
        razon_de_costura=razon_de_costura(integrada),
        no_finitos=contar_no_finitos(normal, integrada),
    )

    assert metricas.acuerdo_p95_grados < 1e-6  # el dato ES limpio: lo único que falta es un umbral validado
    assert decidir(metricas).veredicto is Veredicto.REVIEW


# ---------------------------------------------------------------------------
# Tabla de decisión enumerada (registro válido y validado)
# ---------------------------------------------------------------------------


def test_con_un_registro_validado_y_metricas_dentro_de_los_limites_se_acepta() -> None:
    assert decidir_con(_metricas(), _umbrales()) == Decision(Veredicto.AUTO_ACCEPT, (), ())


@pytest.mark.parametrize("campo", CAMPOS_DE_LIMITE)
def test_una_metrica_justo_en_su_limite_se_acepta(campo: str) -> None:
    limites = _limites()
    metricas = _metricas(**{campo: getattr(limites, campo)})

    assert decidir_con(metricas, _umbrales(limites=limites)).veredicto is Veredicto.AUTO_ACCEPT


@pytest.mark.parametrize("campo", CAMPOS_DE_LIMITE)
def test_una_metrica_sobre_su_limite_va_a_revision_y_nombra_cual(campo: str) -> None:
    limites = _limites()
    metricas = _metricas(**{campo: math.nextafter(getattr(limites, campo), math.inf)})

    decision = decidir_con(metricas, _umbrales(limites=limites))

    assert decision == Decision(Veredicto.REVIEW, (Motivo.SOBRE_EL_LIMITE,), (campo,))


def test_varias_metricas_sobre_su_limite_se_nombran_todas_en_orden_de_campo() -> None:
    metricas = _metricas(acuerdo_p95_grados=9.0, razon_de_costura=9.0)

    decision = decidir_con(metricas, _umbrales())

    assert decision.sobre_el_limite == ("acuerdo_p95_grados", "razon_de_costura")


@pytest.mark.parametrize("valor", NO_FINITOS)
@pytest.mark.parametrize("campo", CAMPOS_DE_LIMITE)
def test_una_metrica_no_finita_va_a_revision_sin_compararse_con_el_limite(campo: str, valor: float) -> None:
    decision = decidir_con(_metricas(**{campo: valor}), _umbrales())

    assert decision == Decision(Veredicto.REVIEW, (Motivo.METRICA_NO_FINITA,), ())


def test_un_signo_distinto_del_esperado_va_a_revision() -> None:
    # Un flip global de gradientes produce ``-altura`` EXACTA: el error angular lo absorbe y solo el signo lo delata.
    decision = decidir_con(_metricas(signo=-1), _umbrales(signo_esperado=1))

    assert decision == Decision(Veredicto.REVIEW, (Motivo.SIGNO_DISTINTO_DEL_ESPERADO,), ())


def test_el_signo_esperado_lo_fija_el_registro_y_no_el_codigo() -> None:
    assert decidir_con(_metricas(signo=-1), _umbrales(signo_esperado=-1)).veredicto is Veredicto.AUTO_ACCEPT
    assert decidir_con(_metricas(signo=1), _umbrales(signo_esperado=-1)).veredicto is Veredicto.REVIEW


def test_valores_no_finitos_en_la_altura_van_a_revision() -> None:
    decision = decidir_con(_metricas(no_finitos=1), _umbrales())

    assert decision == Decision(Veredicto.REVIEW, (Motivo.NO_FINITOS_EN_LA_ALTURA,), ())


def test_un_registro_sin_validacion_contra_corpus_real_va_a_revision() -> None:
    decision = decidir_con(_metricas(), _umbrales(validacion=None))

    assert decision == Decision(Veredicto.REVIEW, (Motivo.SIN_VALIDACION_CONTRA_CORPUS_REAL,), ())


def test_un_registro_derivado_con_otra_version_de_metricas_va_a_revision() -> None:
    decision = decidir_con(_metricas(), _umbrales(version_de_metricas="np-q0a.v0"))

    assert decision == Decision(Veredicto.REVIEW, (Motivo.VERSION_DE_METRICAS_DISTINTA,), ())


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"n_aceptadas_malas": 10}, id="falsos_aceptos_por_encima_de_la_tolerancia"),
        pytest.param({"n_aceptadas": 100}, id="muestra_chica_la_cota_no_alcanza"),
        pytest.param({"n_aceptadas": 0}, id="nada_aceptado_no_hay_evidencia"),
        pytest.param({"n_aceptadas": 400}, id="se_revisa_mas_de_lo_viable"),
        pytest.param({"confianza": 0.999}, id="mas_confianza_exigida_que_la_muestra"),
        pytest.param({"alpha_objetivo": 0.004}, id="tolerancia_mas_estricta_que_la_muestra"),
    ],
)
def test_una_validacion_que_no_alcanza_la_evidencia_va_a_revision(cambios: dict[str, object]) -> None:
    decision = decidir_con(_metricas(), _umbrales(validacion=_validacion(**cambios)))

    assert decision == Decision(Veredicto.REVIEW, (Motivo.VALIDACION_INSUFICIENTE,), ())


def test_la_validacion_justo_en_la_tasa_de_revision_maxima_alcanza() -> None:
    # 500 de 1000 aceptadas = 50 % de revisión, el máximo declarado (``<=``, no ``<``).
    validacion = _validacion(n_aceptadas=500, tasa_de_revision_maxima=0.5)

    assert decidir_con(_metricas(), _umbrales(validacion=validacion)).veredicto is Veredicto.AUTO_ACCEPT


def test_los_motivos_se_acumulan_en_el_orden_del_enum() -> None:
    umbrales = _umbrales(version_de_metricas="np-q0a.v0", validacion=_validacion(n_aceptadas_malas=10))
    metricas = _metricas(acuerdo_p95_grados=math.nan, residuo_de_integrabilidad=9.0, no_finitos=3, signo=-1)

    decision = decidir_con(metricas, umbrales)

    assert decision.veredicto is Veredicto.REVIEW
    assert decision.motivos == (
        Motivo.VERSION_DE_METRICAS_DISTINTA,
        Motivo.VALIDACION_INSUFICIENTE,
        Motivo.METRICA_NO_FINITA,
        Motivo.NO_FINITOS_EN_LA_ALTURA,
        Motivo.SIGNO_DISTINTO_DEL_ESPERADO,
        Motivo.SOBRE_EL_LIMITE,
    )
    assert decision.sobre_el_limite == ("residuo_de_integrabilidad",)
    assert list(decision.motivos) == sorted(decision.motivos, key=list(Motivo).index)


@pytest.mark.parametrize(
    ("motivos", "sobre_el_limite"),
    [
        pytest.param((Motivo.SOBRE_EL_LIMITE,), (), id="solo_motivos"),
        pytest.param((), ("razon_de_costura",), id="solo_sobre_el_limite"),
        pytest.param((Motivo.SOBRE_EL_LIMITE,), ("razon_de_costura",), id="ambos"),
    ],
)
def test_auto_accept_no_puede_traer_motivos(motivos: tuple[Motivo, ...], sobre_el_limite: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="AUTO_ACCEPT"):
        Decision(Veredicto.AUTO_ACCEPT, motivos, sobre_el_limite)


def test_review_tiene_que_decir_por_que() -> None:
    with pytest.raises(ValueError, match="REVIEW"):
        Decision(Veredicto.REVIEW, (), ())


def test_el_vocabulario_de_veredictos_y_motivos_esta_congelado() -> None:
    # REJECT no existe: no hay base para derivar un umbral de rechazo y REVIEW ya impide el auto-aceptado.
    assert {v.name for v in Veredicto} == {"AUTO_ACCEPT", "REVIEW"}
    assert [m.name for m in Motivo] == [
        "SIN_UMBRALES",
        "VERSION_DE_METRICAS_DISTINTA",
        "SIN_VALIDACION_CONTRA_CORPUS_REAL",
        "VALIDACION_INSUFICIENTE",
        "METRICA_NO_FINITA",
        "NO_FINITOS_EN_LA_ALTURA",
        "SIGNO_DISTINTO_DEL_ESPERADO",
        "SOBRE_EL_LIMITE",
    ]


def test_las_metricas_de_calidad_toman_el_p95_del_acuerdo_y_no_otro_estadistico() -> None:
    acuerdo = AcuerdoNormal(media_grados=1.0, mediana_grados=2.0, p95_grados=3.0, escala=-1.0, signo=-1)

    resultado = MetricasDeCalidad.desde_metricas(
        acuerdo, residuo_de_integrabilidad=0.25, razon_de_costura=1.5, no_finitos=0
    )

    assert resultado == MetricasDeCalidad(
        acuerdo_p95_grados=3.0, signo=-1, residuo_de_integrabilidad=0.25, razon_de_costura=1.5, no_finitos=0
    )


# ---------------------------------------------------------------------------
# El registro no se puede construir inválido
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"id_del_corpus": ""}, id="id_vacio"),
        pytest.param({"id_del_corpus": None}, id="id_none"),
        pytest.param({"criterio_de_etiqueta": 5}, id="criterio_no_es_texto"),
        pytest.param({"huella_del_corpus": "ab" * 32 + "a"}, id="huella_de_65_caracteres"),
        pytest.param({"huella_del_corpus": None}, id="huella_none"),
        pytest.param({"criterio_de_etiqueta": "  "}, id="criterio_en_blanco"),
        pytest.param({"huella_del_corpus": "abc"}, id="huella_corta"),
        pytest.param({"huella_del_corpus": "AB" * 32}, id="huella_en_mayusculas"),
        pytest.param({"huella_del_corpus": "zz" * 32}, id="huella_no_hex"),
        pytest.param({"huella_del_corpus": huella_especificacion(ESPECIFICACION_V1)}, id="corpus_sintetico_conocido"),
        pytest.param({"n_total": 0, "n_aceptadas": 0, "n_aceptadas_malas": 0}, id="n_total_cero_divide_por_cero"),
        pytest.param({"n_aceptadas": 1001}, id="mas_aceptadas_que_total"),
        pytest.param({"n_aceptadas": -1}, id="aceptadas_negativas"),
        pytest.param({"n_aceptadas_malas": 601}, id="mas_malas_que_aceptadas"),
        pytest.param({"n_aceptadas_malas": -1}, id="malas_negativas"),
        pytest.param({"n_total": True}, id="conteo_booleano"),
        pytest.param({"n_aceptadas": 600.0}, id="conteo_flotante"),
        pytest.param({"confianza": 0.0}, id="confianza_cero"),
        pytest.param({"confianza": 1.0}, id="confianza_uno"),
        pytest.param({"alpha_objetivo": 0.0}, id="alpha_cero"),
        pytest.param({"alpha_objetivo": 1.0}, id="alpha_uno"),
        pytest.param({"alpha_objetivo": math.nan}, id="alpha_nan"),
        pytest.param({"tasa_de_revision_maxima": -0.1}, id="tasa_negativa"),
        pytest.param({"tasa_de_revision_maxima": 1.1}, id="tasa_mayor_que_uno"),
        pytest.param({"tasa_de_revision_maxima": math.inf}, id="tasa_infinita"),
    ],
)
def test_una_validacion_inconsistente_no_se_puede_construir(cambios: dict[str, object]) -> None:
    with pytest.raises(UmbralesInvalidosError):
        _validacion(**cambios)


@pytest.mark.parametrize("campo", CAMPOS_DE_LIMITE)
@pytest.mark.parametrize("valor", [-1.0, math.nan, math.inf, True, "2.0", None])
def test_un_limite_negativo_no_finito_o_de_otro_tipo_no_se_puede_construir(campo: str, valor: object) -> None:
    with pytest.raises(UmbralesInvalidosError):
        _limites(**{campo: valor})  # type: ignore[arg-type]


def test_los_bordes_de_las_probabilidades_y_de_los_conteos_son_validos() -> None:
    # Tasa de revisión máxima 0 y 1 son tolerancias extremas pero coherentes; los conteos pueden ser todos iguales.
    assert _validacion(tasa_de_revision_maxima=0.0).tasa_de_revision_maxima == 0.0
    assert _validacion(tasa_de_revision_maxima=1.0).tasa_de_revision_maxima == 1.0
    todos_iguales = _validacion(n_total=10, n_aceptadas=10, n_aceptadas_malas=10)
    assert todos_iguales.cota_superior_de_aceptadas_malas == 1.0
    assert not todos_iguales.alcanza
    assert _validacion(n_total=1, n_aceptadas=1, n_aceptadas_malas=0).n_total == 1


def test_con_tolerancia_de_revision_cero_solo_alcanza_si_se_acepta_todo() -> None:
    aceptando_todo = _validacion(n_total=600, n_aceptadas=600, tasa_de_revision_maxima=0.0)
    revisando_una = _validacion(n_total=601, n_aceptadas=600, tasa_de_revision_maxima=0.0)

    assert aceptando_todo.alcanza
    assert not revisando_una.alcanza


def test_si_no_se_acepta_nada_la_cota_es_uno_y_la_validacion_no_alcanza() -> None:
    sin_aceptar = _validacion(n_aceptadas=0, tasa_de_revision_maxima=1.0)

    assert sin_aceptar.cota_superior_de_aceptadas_malas == 1.0
    assert sin_aceptar.tasa_de_revision == 1.0
    assert not sin_aceptar.alcanza


def test_un_limite_en_cero_es_valido() -> None:
    assert _limites(razon_de_costura=0.0).razon_de_costura == 0.0


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"etiqueta": ""}, id="etiqueta_vacia"),
        pytest.param({"etiqueta": None}, id="etiqueta_none"),
        pytest.param({"version_de_metricas": 1}, id="version_no_es_texto"),
        pytest.param({"huella_de_derivacion": "cd" * 32 + "c"}, id="huella_de_derivacion_de_65"),
        pytest.param({"version_de_metricas": " "}, id="version_en_blanco"),
        pytest.param({"huella_de_derivacion": "xyz"}, id="huella_invalida"),
        pytest.param({"signo_esperado": 0}, id="signo_esperado_cero"),
        pytest.param({"signo_esperado": 2}, id="signo_esperado_fuera_de_rango"),
        pytest.param({"signo_esperado": True}, id="signo_esperado_booleano"),
        pytest.param({"signo_esperado": "1"}, id="signo_esperado_texto"),
        pytest.param({"limites": None}, id="sin_limites"),
        pytest.param({"validacion": "validado"}, id="validacion_que_no_es_una_validacion"),
    ],
)
def test_un_registro_inconsistente_no_se_puede_construir(cambios: dict[str, object]) -> None:
    with pytest.raises(UmbralesInvalidosError):
        _umbrales(**cambios)


def test_el_registro_derivado_del_corpus_sintetico_es_valido_pero_no_acepta_sin_validacion_real() -> None:
    """Derivar en el sintético se puede (queda registrado); lo que no se puede es validar con él."""
    huella_sintetica = huella_especificacion(ESPECIFICACION_V1)

    sin_validar = _umbrales(huella_de_derivacion=huella_sintetica, validacion=None)

    assert decidir_con(_metricas(), sin_validar).veredicto is Veredicto.REVIEW


@pytest.mark.parametrize(
    "cambios",
    [
        pytest.param({"acuerdo_p95_grados": True}, id="booleano"),
        pytest.param({"residuo_de_integrabilidad": "0.1"}, id="texto"),
        pytest.param({"razon_de_costura": None}, id="none"),
        pytest.param({"no_finitos": 1.0}, id="conteo_flotante"),
        pytest.param({"no_finitos": -1}, id="conteo_negativo"),
        pytest.param({"no_finitos": True}, id="conteo_booleano"),
        pytest.param({"signo": 0}, id="signo_cero"),
        pytest.param({"signo": 2}, id="signo_fuera_de_rango"),
        pytest.param({"signo": True}, id="signo_booleano"),
        pytest.param({"signo": 1.0}, id="signo_flotante"),
    ],
)
def test_unas_metricas_de_otro_tipo_no_se_pueden_construir(cambios: dict[str, object]) -> None:
    with pytest.raises(MetricasInvalidasError):
        _metricas(**cambios)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# La cota binomial: se contrasta con la distribución exacta en racionales
# ---------------------------------------------------------------------------


def _cdf_binomial_exacta(k: int, n: int, p: float) -> float:
    q = Fraction(p)
    return float(sum(math.comb(n, i) * q**i * (1 - q) ** (n - i) for i in range(k + 1)))


@pytest.mark.parametrize(
    ("k", "n", "confianza"),
    [(0, 10, 0.95), (1, 10, 0.95), (3, 50, 0.9), (0, 600, 0.95), (5, 100, 0.99), (49, 50, 0.95), (0, 1, 0.5)],
)
def test_la_cota_binomial_cumple_su_definicion_contra_la_distribucion_exacta(k: int, n: int, confianza: float) -> None:
    cota = cota_superior_binomial(k, n, confianza)

    # P(X <= k | n, cota) = 1 - confianza: la cota es el mayor ``p`` que todavía no se rechaza con esa confianza.
    assert _cdf_binomial_exacta(k, n, cota) == pytest.approx(1.0 - confianza, abs=1e-9)


@pytest.mark.parametrize("n", [1, 2, 10, 600, 100_000])
def test_sin_fallos_la_cota_es_la_forma_cerrada(n: int) -> None:
    assert cota_superior_binomial(0, n, 0.95) == pytest.approx(1.0 - 0.05 ** (1.0 / n), rel=1e-9)


def test_con_una_confianza_extrema_la_cota_no_revienta_en_el_borde_del_intervalo() -> None:
    # 1 - 1.1e-16: la bisección converge pegada a 1 y el punto medio redondea a 1.0, donde ``log1p(-p)`` no existe.
    cota = cota_superior_binomial(0, 1, 1.0 - 2.0**-53)

    assert 0.999999999999999 < cota <= 1.0


def test_la_cota_de_todo_fallo_es_uno() -> None:
    assert cota_superior_binomial(7, 7, 0.95) == 1.0


def test_la_cota_crece_con_los_fallos_baja_con_la_muestra_y_sube_con_la_confianza() -> None:
    assert (
        cota_superior_binomial(0, 100, 0.95)
        < cota_superior_binomial(1, 100, 0.95)
        < cota_superior_binomial(2, 100, 0.95)
    )
    assert cota_superior_binomial(0, 200, 0.95) < cota_superior_binomial(0, 100, 0.95)
    assert cota_superior_binomial(0, 100, 0.90) < cota_superior_binomial(0, 100, 0.99)


@pytest.mark.parametrize(
    ("k", "n", "confianza"),
    [(-1, 10, 0.95), (11, 10, 0.95), (0, 0, 0.95), (0, 10, 0.0), (0, 10, 1.0), (True, 10, 0.95), (0, 10.0, 0.95)],
)
def test_la_cota_rechaza_argumentos_invalidos(k: object, n: object, confianza: float) -> None:
    with pytest.raises(UmbralesInvalidosError):
        cota_superior_binomial(k, n, confianza)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Anclas: el gate y metrics.py no se separan
# ---------------------------------------------------------------------------


def _metricas_publicas() -> frozenset[str]:
    """Funciones públicas de ``metrics.py`` (los tipos de resultado, como ``AcuerdoNormal``, no son métricas)."""
    return frozenset(
        nombre
        for nombre in metrics.__all__
        if callable(getattr(metrics, nombre)) and not isinstance(getattr(metrics, nombre), type)
    )


def test_toda_metrica_publica_esta_gateada_o_excluida_con_motivo() -> None:
    assert set(METRICAS_GATEADAS) | set(METRICAS_NO_GATEADAS) == _metricas_publicas()
    assert not set(METRICAS_GATEADAS) & set(METRICAS_NO_GATEADAS)
    assert all(motivo.strip() for motivo in METRICAS_NO_GATEADAS.values())


def test_la_clasificacion_de_las_metricas_esta_congelada() -> None:
    assert METRICAS_GATEADAS == {
        "acuerdo_normal": ("acuerdo_p95_grados", "signo"),
        "residuo_de_integrabilidad": ("residuo_de_integrabilidad",),
        "razon_de_costura": ("razon_de_costura",),
        "contar_no_finitos": ("no_finitos",),
    }
    assert set(METRICAS_NO_GATEADAS) == {"rango_dinamico", "razon_de_recorte"}


def test_los_campos_de_las_metricas_de_calidad_son_los_de_las_metricas_gateadas() -> None:
    campos = {campo.name for campo in dataclasses.fields(MetricasDeCalidad)}

    assert campos == {campo for campos_de_la_metrica in METRICAS_GATEADAS.values() for campo in campos_de_la_metrica}


def test_hay_un_limite_por_cada_campo_gateado_salvo_el_conteo_de_no_finitos_y_el_signo() -> None:
    # ``no_finitos`` y ``signo`` no tienen límite calibrable: cualquier no finito impide aceptar y el signo se
    # compara con el esperado por el registro.
    campos = {campo.name for campo in dataclasses.fields(MetricasDeCalidad)}

    assert set(CAMPOS_DE_LIMITE) == campos - {"no_finitos", "signo"}

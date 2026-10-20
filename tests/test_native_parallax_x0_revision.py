"""NP-X0b (parte pura): REVIEW_RATE simulada y candidatos derivados, sobre mediciones hechas a mano.

Qué se ancla (y con qué):

- **La etiqueta «mala» es declarada y enumerada**: un control negativo lo es por construcción, una altura sin rango
  también, y el resto según el error de altura contra una tolerancia que NO tiene valor por defecto.
- **La derivación es por cuantil de rango más próximo**, leído como el decimal más corto que escribe quien llama
  (``0.28 * 25`` en flotante da ``7.000000000000001`` y elegiría el octavo valor).
- **La simulación no compara límites por su cuenta**: se apoya en los motivos que informa ``decidir_con``. La
  partición de ``Motivo`` en «del registro», «de la validación» y «de la textura» se congela por igualdad: un motivo
  nuevo rompe el test hasta que se lo clasifica.
- **Dejar un caso fuera**: los límites con que se evalúa un caso se derivan SIN ese caso. Evaluar con límites
  derivados de los mismos casos acepta de más.

Límite declarado: son mediciones fabricadas, no una afirmación sobre texturas reales. Nada de esto valida un umbral.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import fields

import pytest

from sky_claw.local.native_parallax.experiments.revision import (
    MOTIVOS_DE_LA_TEXTURA,
    MOTIVOS_DE_LA_VALIDACION,
    MOTIVOS_DEL_REGISTRO,
    Conteo,
    Medicion,
    MedicionInvalidaError,
    ParametroDeRevisionInvalidoError,
    ReporteDeRevision,
    RevisionError,
    SinMedicionesBuenasError,
    _huella_del_pliegue,
    candidato_desde_limites,
    criterio_de_etiqueta,
    derivar_limites,
    es_mala,
    simular_dejando_un_caso_fuera,
    simular_revision,
)
from sky_claw.local.native_parallax.gates import (
    VERSION_DE_METRICAS,
    LimitesDeAutoaceptacion,
    MetricasDeCalidad,
    Motivo,
    Umbrales,
    ValidacionContraCorpusReal,
    Veredicto,
    decidir_con,
    huella_del_candidato,
)
from sky_claw.local.native_parallax.integrate import Bordes

TOLERANCIA = 0.05
HUELLA_A = "a" * 64
HUELLA_DEL_CORPUS = "b" * 64
HUELLA_REAL = "c" * 64


def _metricas(
    acuerdo: float = 1.0, residuo: float = 0.01, costura: float = 1.0, signo: int = 1, no_finitos: int = 0
) -> MetricasDeCalidad:
    return MetricasDeCalidad(
        acuerdo_p95_grados=acuerdo,
        signo=signo,
        residuo_de_integrabilidad=residuo,
        razon_de_costura=costura,
        no_finitos=no_finitos,
    )


def _medicion(
    caso: str = "A",
    escenario: str = "limpio",
    *,
    control: bool = False,
    error: float | None = 0.0,
    bordes: Bordes = Bordes.PERIODICOS,
    **metricas: float,
) -> Medicion:
    return Medicion(
        caso=caso,
        escenario=escenario,
        bordes=bordes,
        es_control_negativo=control,
        metricas=_metricas(**metricas),  # type: ignore[arg-type]
        error_de_altura=error,
    )


def _limites(acuerdo: float = 5.0, residuo: float = 0.1, costura: float = 1.5) -> LimitesDeAutoaceptacion:
    return LimitesDeAutoaceptacion(
        acuerdo_p95_grados=acuerdo, residuo_de_integrabilidad=residuo, razon_de_costura=costura
    )


def _umbrales(limites: LimitesDeAutoaceptacion | None = None, **cambios: object) -> Umbrales:
    argumentos: dict[str, object] = {
        "etiqueta": "candidato",
        "version_de_metricas": VERSION_DE_METRICAS,
        "huella_de_derivacion": HUELLA_A,
        "signo_esperado": 1,
        "limites": limites or _limites(),
        "validacion": None,
    }
    argumentos.update(cambios)
    return Umbrales(**argumentos)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# La medición
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("campo", ["caso", "escenario"])
@pytest.mark.parametrize("valor", ["", "   ", None, 3])
def test_una_medicion_exige_caso_y_escenario_como_texto_no_vacio(campo: str, valor: object) -> None:
    argumentos = {"caso": "A", "escenario": "limpio", campo: valor}

    with pytest.raises(MedicionInvalidaError):
        Medicion(
            bordes=Bordes.PERIODICOS,
            caso=argumentos["caso"],  # type: ignore[arg-type]
            escenario=argumentos["escenario"],  # type: ignore[arg-type]
            es_control_negativo=False,
            metricas=_metricas(),
            error_de_altura=0.0,
        )


@pytest.mark.parametrize("valor", [None, "periodicos", 1, "neumann"])
def test_una_medicion_exige_los_bordes_como_miembro_de_bordes(valor: object) -> None:
    with pytest.raises(MedicionInvalidaError, match="bordes"):
        Medicion(
            caso="A",
            escenario="limpio",
            bordes=valor,  # type: ignore[arg-type]
            es_control_negativo=False,
            metricas=_metricas(),
            error_de_altura=0.0,
        )


@pytest.mark.parametrize("valor", [1, 0, None, "si"])
def test_un_control_negativo_es_un_booleano_de_verdad(valor: object) -> None:
    with pytest.raises(MedicionInvalidaError):
        Medicion(
            bordes=Bordes.PERIODICOS,
            caso="A",
            escenario="limpio",
            es_control_negativo=valor,  # type: ignore[arg-type]
            metricas=_metricas(),
            error_de_altura=0.0,
        )


def test_una_medicion_exige_metricas_de_calidad() -> None:
    with pytest.raises(MedicionInvalidaError):
        Medicion(
            bordes=Bordes.PERIODICOS,
            caso="A",
            escenario="limpio",
            es_control_negativo=False,
            metricas={"acuerdo_p95_grados": 1.0},  # type: ignore[arg-type]
            error_de_altura=0.0,
        )


@pytest.mark.parametrize("valor", [-1e-9, math.nan, math.inf, True, "0.1"])
def test_el_error_de_altura_es_none_o_un_real_finito_no_negativo(valor: object) -> None:
    with pytest.raises(MedicionInvalidaError):
        Medicion(
            bordes=Bordes.PERIODICOS,
            caso="A",
            escenario="limpio",
            es_control_negativo=False,
            metricas=_metricas(),
            error_de_altura=valor,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("valor", [None, 0.0, 0.3, 7])
def test_el_error_de_altura_admite_none_cero_y_valores_positivos(valor: float | None) -> None:
    assert _medicion(error=valor).error_de_altura == valor


# ---------------------------------------------------------------------------
# La etiqueta «mala»
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("control", "error", "esperada"),
    [
        (False, 0.0, False),
        (False, TOLERANCIA, False),  # el borde es inclusivo: mala es ESTRICTAMENTE mayor que la tolerancia
        (False, math.nextafter(TOLERANCIA, math.inf), True),
        (False, 0.4, True),
        (False, None, True),  # sin rango: no hay altura recuperable
        (True, 0.0, True),  # un control negativo lo es por construcción, aunque la altura salga bien
        (True, None, True),
    ],
)
def test_la_etiqueta_mala_se_enumera(control: bool, error: float | None, esperada: bool) -> None:
    assert es_mala(_medicion(control=control, error=error), tolerancia_de_altura=TOLERANCIA) is esperada


@pytest.mark.parametrize("tolerancia", [0, 0.0, -0.1, math.nan, math.inf, True, "0.05", None])
def test_la_tolerancia_de_altura_es_un_real_positivo_y_finito(tolerancia: object) -> None:
    with pytest.raises(ParametroDeRevisionInvalidoError):
        es_mala(_medicion(), tolerancia_de_altura=tolerancia)  # type: ignore[arg-type]


def test_el_criterio_de_etiqueta_dice_la_tolerancia_que_se_uso() -> None:
    texto = criterio_de_etiqueta(0.05)

    assert "0.05" in texto
    assert "control negativo" in texto
    assert criterio_de_etiqueta(0.1) != texto


# ---------------------------------------------------------------------------
# Derivación de límites por cuantil
# ---------------------------------------------------------------------------


def _buenas(valores_de_acuerdo: list[float]) -> list[Medicion]:
    """Una medición buena por valor, con residuo y costura crecientes a otra escala (independientes del acuerdo)."""
    return [
        _medicion(caso=f"C{i}", acuerdo=v, residuo=0.01 * (i + 1), costura=1.0 + 0.1 * (len(valores_de_acuerdo) - i))
        for i, v in enumerate(valores_de_acuerdo)
    ]


@pytest.mark.parametrize(
    ("cuantil", "posicion"),  # posición 1-based del rango más próximo sobre 10 valores
    [(1.0, 10), (0.9, 9), (0.95, 10), (0.7, 7), (0.5, 5), (0.1, 1), (0.01, 1), (0.99, 10)],
)
def test_el_limite_es_el_valor_de_rango_mas_proximo_del_cuantil(cuantil: float, posicion: int) -> None:
    mediciones = _buenas([float(v) for v in range(1, 11)])  # acuerdo 1..10

    limites = derivar_limites(mediciones, cuantil=cuantil, tolerancia_de_altura=TOLERANCIA)

    assert limites.acuerdo_p95_grados == float(posicion)


def test_el_cuantil_se_lee_como_el_decimal_que_escribio_quien_llama() -> None:
    # 0.28 * 25 == 7.000000000000001 en flotante (verificado ejecutando): con ceil() directo saldría el octavo valor.
    assert 0.28 * 25 > 7.0
    assert math.ceil(0.28 * 25) == 8
    mediciones = _buenas([float(v) for v in range(1, 26)])

    limites = derivar_limites(mediciones, cuantil=0.28, tolerancia_de_altura=TOLERANCIA)

    assert limites.acuerdo_p95_grados == 7.0


def test_cada_metrica_tiene_su_propio_cuantil_independiente() -> None:
    mediciones = _buenas([float(v) for v in range(1, 11)])

    limites = derivar_limites(mediciones, cuantil=0.5, tolerancia_de_altura=TOLERANCIA)

    # acuerdo crece con i; residuo crece con i; costura DECRECE con i: el quinto menor no es el mismo caso.
    assert limites.acuerdo_p95_grados == 5.0
    assert limites.residuo_de_integrabilidad == pytest.approx(0.05)
    assert limites.razon_de_costura == pytest.approx(1.5)  # los 10 valores son 1.1 … 2.0: el quinto menor


def test_la_derivacion_solo_usa_mediciones_buenas() -> None:
    buenas = _buenas([1.0, 2.0, 3.0])
    mala_por_error = _medicion(caso="M", acuerdo=170.0, error=0.4)
    control = _medicion(caso="N", acuerdo=170.0, control=True)
    sin_rango = _medicion(caso="R", acuerdo=170.0, error=None)

    limites = derivar_limites(
        [*buenas, mala_por_error, control, sin_rango], cuantil=1.0, tolerancia_de_altura=TOLERANCIA
    )

    assert limites.acuerdo_p95_grados == 3.0


def test_sin_mediciones_buenas_no_hay_limites_que_derivar() -> None:
    solo_malas = [_medicion(error=0.4), _medicion(control=True)]

    with pytest.raises(SinMedicionesBuenasError):
        derivar_limites(solo_malas, cuantil=1.0, tolerancia_de_altura=TOLERANCIA)
    with pytest.raises(SinMedicionesBuenasError):
        derivar_limites([], cuantil=1.0, tolerancia_de_altura=TOLERANCIA)


@pytest.mark.parametrize("cuantil", [0, 0.0, -0.1, 1.0000001, 2, math.nan, math.inf, True, "0.9", None])
def test_el_cuantil_esta_en_cero_uno_abierto_por_abajo(cuantil: object) -> None:
    with pytest.raises(ParametroDeRevisionInvalidoError):
        derivar_limites(_buenas([1.0, 2.0]), cuantil=cuantil, tolerancia_de_altura=TOLERANCIA)  # type: ignore[arg-type]


# Cada métrica gateada con sus valores IMPOSIBLES por definición (el dominio de ``DOMINIO_DE_LAS_METRICAS``) y los no
# finitos. Una medición buena corrupta no se puede dejar en la muestra: movería el rango del cuantil sin fallar.
_MEDICIONES_IMPOSIBLES = [
    ("acuerdo", math.nan),
    ("acuerdo", math.inf),
    ("acuerdo", -1.0),  # un error angular negativo
    ("acuerdo", 181.0),  # más de media vuelta
    ("residuo", math.nan),
    ("residuo", math.inf),
    ("residuo", -0.1),
    ("residuo", 1.5),  # el residuo normalizado por el Hessiano vive en [0, 1]
    ("costura", math.nan),
    ("costura", math.inf),
    ("costura", -0.5),
]


@pytest.mark.parametrize(("campo", "valor"), _MEDICIONES_IMPOSIBLES)
def test_una_medicion_buena_imposible_no_se_deriva_en_silencio(campo: str, valor: float) -> None:
    mediciones = [*_buenas([1.0, 2.0]), _medicion(caso="X", **{campo: valor})]

    with pytest.raises(MedicionInvalidaError, match="dominio"):
        derivar_limites(mediciones, cuantil=1.0, tolerancia_de_altura=TOLERANCIA)


def test_una_fila_corrupta_no_cambia_en_silencio_el_rango_aunque_el_cuantil_caiga_en_otro_valor() -> None:
    # Con la fila corrupta (-5) el cuantil 0.5 de 4 valores cae en el 1.0 en vez de en el 2.0: el límite se movería.
    sanas = _buenas([1.0, 2.0, 3.0])

    assert derivar_limites(sanas, cuantil=0.5, tolerancia_de_altura=TOLERANCIA).acuerdo_p95_grados == 2.0
    with pytest.raises(MedicionInvalidaError):
        derivar_limites([*sanas, _medicion(caso="D", acuerdo=-5.0)], cuantil=0.5, tolerancia_de_altura=TOLERANCIA)


def test_las_mediciones_malas_imposibles_no_impiden_derivar_porque_no_entran_en_la_muestra() -> None:
    sanas = _buenas([1.0, 2.0, 3.0])
    mala_e_imposible = _medicion(caso="M", acuerdo=-5.0, error=0.4)

    limites = derivar_limites([*sanas, mala_e_imposible], cuantil=1.0, tolerancia_de_altura=TOLERANCIA)

    assert limites.acuerdo_p95_grados == 3.0


def test_los_extremos_del_dominio_son_mediciones_validas_y_se_derivan() -> None:
    # 0° (una normal perfecta), 180° (la opuesta), residuo 0 y 1, costura 0: están DENTRO del dominio (inclusivo).
    abajo = _medicion(caso="A", acuerdo=0.0, residuo=0.0, costura=0.0)
    arriba = _medicion(caso="B", acuerdo=180.0, residuo=1.0, costura=1.0)

    limites = derivar_limites([abajo, arriba], cuantil=1.0, tolerancia_de_altura=TOLERANCIA)

    assert (limites.acuerdo_p95_grados, limites.residuo_de_integrabilidad) == (180.0, 1.0)
    assert derivar_limites([abajo], cuantil=1.0, tolerancia_de_altura=TOLERANCIA).acuerdo_p95_grados == 0.0


def test_los_valores_imposibles_cubren_cada_campo_gateado() -> None:
    cubiertos = {campo for campo, _ in _MEDICIONES_IMPOSIBLES}

    assert len(fields(LimitesDeAutoaceptacion)) == len(cubiertos) == 3  # uno por cada campo de LimitesDeAutoaceptacion


def _mezcla_de_bordes() -> list[Medicion]:
    return [_medicion(caso="A", bordes=Bordes.PERIODICOS), _medicion(caso="B", bordes=Bordes.NEUMANN)]


def _derivar(mediciones: list[Medicion]) -> object:
    return derivar_limites(mediciones, cuantil=1.0, tolerancia_de_altura=TOLERANCIA)


def _simular(mediciones: list[Medicion]) -> object:
    return simular_revision(mediciones, _umbrales(), tolerancia_de_altura=TOLERANCIA)


def _simular_sin_un_caso(mediciones: list[Medicion]) -> object:
    return simular_dejando_un_caso_fuera(
        mediciones,
        cuantil=1.0,
        tolerancia_de_altura=TOLERANCIA,
        signo_esperado=1,
        huella_del_corpus=HUELLA_DEL_CORPUS,
    )


# Las TRES funciones públicas que consumen mediciones: la regla vive en un solo validador y se enumera acá.
_CONSUMIDORES_DE_MEDICIONES = [_derivar, _simular, _simular_sin_un_caso]


@pytest.mark.parametrize("consumidor", _CONSUMIDORES_DE_MEDICIONES, ids=lambda f: f.__name__)
def test_no_se_mezclan_mediciones_de_bordes_distintos(consumidor: Callable[[list[Medicion]], object]) -> None:
    """pr-agent, #763: un límite derivado con bordes periódicos y Neumann mezclados no vale para ninguno de los dos."""
    with pytest.raises(ParametroDeRevisionInvalidoError, match="bordes"):
        consumidor(_mezcla_de_bordes())


@pytest.mark.parametrize("consumidor", _CONSUMIDORES_DE_MEDICIONES, ids=lambda f: f.__name__)
@pytest.mark.parametrize("bordes", list(Bordes))
def test_mediciones_de_los_mismos_bordes_si_se_consumen(
    consumidor: Callable[[list[Medicion]], object], bordes: Bordes
) -> None:
    homogeneas = [_medicion(caso="A", bordes=bordes), _medicion(caso="B", bordes=bordes)]

    assert consumidor(homogeneas) is not None


def test_la_derivacion_no_depende_del_orden_de_las_mediciones() -> None:
    mediciones = _buenas([5.0, 1.0, 4.0, 2.0, 3.0])

    adelante = derivar_limites(mediciones, cuantil=0.6, tolerancia_de_altura=TOLERANCIA)
    atras = derivar_limites(list(reversed(mediciones)), cuantil=0.6, tolerancia_de_altura=TOLERANCIA)

    assert adelante == atras


def test_el_candidato_derivado_no_tiene_validacion_y_declara_version_y_signo() -> None:
    limites = _limites()

    candidato = candidato_desde_limites(limites, etiqueta="x", huella_de_derivacion=HUELLA_DEL_CORPUS, signo_esperado=1)

    assert candidato.validacion is None
    assert candidato.version_de_metricas == VERSION_DE_METRICAS
    assert candidato.huella_de_derivacion == HUELLA_DEL_CORPUS
    assert (candidato.signo_esperado, candidato.limites, candidato.etiqueta) == (1, limites, "x")


# ---------------------------------------------------------------------------
# La partición de los motivos del gate
# ---------------------------------------------------------------------------


def test_los_motivos_del_gate_se_particionan_en_registro_validacion_y_textura() -> None:
    assert {Motivo.SIN_UMBRALES, Motivo.VERSION_DE_METRICAS_DISTINTA} == MOTIVOS_DEL_REGISTRO
    assert {Motivo.SIN_VALIDACION_CONTRA_CORPUS_REAL, Motivo.VALIDACION_INSUFICIENTE} == MOTIVOS_DE_LA_VALIDACION
    assert {
        Motivo.METRICA_FUERA_DE_DOMINIO,
        Motivo.NO_FINITOS_EN_LA_ALTURA,
        Motivo.SIGNO_DISTINTO_DEL_ESPERADO,
        Motivo.SOBRE_EL_LIMITE,
    } == MOTIVOS_DE_LA_TEXTURA
    # Disjuntos y exhaustivos: un Motivo nuevo en gates.py rompe esto hasta que se lo clasifica.
    union = MOTIVOS_DEL_REGISTRO | MOTIVOS_DE_LA_VALIDACION | MOTIVOS_DE_LA_TEXTURA
    assert union == set(Motivo)
    assert len(MOTIVOS_DEL_REGISTRO) + len(MOTIVOS_DE_LA_VALIDACION) + len(MOTIVOS_DE_LA_TEXTURA) == len(Motivo)


# ---------------------------------------------------------------------------
# La simulación
# ---------------------------------------------------------------------------


def test_un_candidato_sin_validacion_manda_todo_a_revision_en_el_gate_pero_la_simulacion_mide_los_limites() -> None:
    umbrales = _umbrales()
    ok = _medicion(caso="A", acuerdo=1.0)

    decision = decidir_con(ok.metricas, umbrales)
    reporte = simular_revision([ok], umbrales, tolerancia_de_altura=TOLERANCIA)

    assert decision.veredicto is Veredicto.REVIEW
    assert Motivo.SIN_VALIDACION_CONTRA_CORPUS_REAL in decision.motivos
    assert reporte.total.n_aceptadas == 1  # el motivo de validación no es de la textura


def test_el_conteo_de_una_simulacion_hecha_a_mano() -> None:
    mediciones = [
        _medicion(caso="A", acuerdo=1.0, error=0.0),  # dentro de los límites, buena
        _medicion(caso="B", acuerdo=1.0, error=0.4),  # dentro de los límites, MALA: falso aceptado
        _medicion(caso="C", acuerdo=50.0, error=0.0),  # fuera, buena: revisión innecesaria
        _medicion(caso="D", acuerdo=50.0, error=0.4),  # fuera, mala: revisión acertada
    ]

    reporte = simular_revision(mediciones, _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert reporte.total == Conteo(n_total=4, n_aceptadas=2, n_malas=2, n_aceptadas_malas=1)
    assert reporte.total.n_en_revision == 2
    assert reporte.total.n_malas_en_revision == 1  # D: la revisión acertada
    assert reporte.total.n_buenas_en_revision == 1  # C: la revisión innecesaria
    assert reporte.falsos_aceptos == (("B", "limpio"),)  # quién se coló, para poder mirarlo
    assert reporte.total.tasa_de_revision == 0.5
    assert reporte.total.tasa_de_falsos_aceptos == 0.5
    assert reporte.tolerancia_de_altura == TOLERANCIA
    assert reporte.criterio_de_etiqueta == criterio_de_etiqueta(TOLERANCIA)


def test_sin_aceptadas_la_tasa_de_falsos_aceptos_no_esta_definida() -> None:
    reporte = simular_revision([_medicion(acuerdo=50.0)], _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert reporte.total.n_aceptadas == 0
    assert reporte.total.tasa_de_falsos_aceptos is None
    assert reporte.total.tasa_de_revision == 1.0


def test_un_limite_es_inclusivo_en_la_simulacion_igual_que_en_el_gate() -> None:
    en_el_borde = _medicion(acuerdo=5.0)
    apenas_arriba = _medicion(acuerdo=math.nextafter(5.0, math.inf))

    reporte = simular_revision([en_el_borde, apenas_arriba], _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert reporte.total.n_aceptadas == 1


_CASOS_POR_MOTIVO: dict[Motivo, Callable[[], Medicion]] = {
    Motivo.SOBRE_EL_LIMITE: lambda: _medicion(acuerdo=50.0),
    Motivo.SIGNO_DISTINTO_DEL_ESPERADO: lambda: _medicion(signo=-1),
    Motivo.NO_FINITOS_EN_LA_ALTURA: lambda: _medicion(no_finitos=3),
    Motivo.METRICA_FUERA_DE_DOMINIO: lambda: _medicion(acuerdo=181.0),
}


def test_hay_un_caso_de_revision_por_cada_motivo_de_la_textura() -> None:
    assert set(_CASOS_POR_MOTIVO) == MOTIVOS_DE_LA_TEXTURA  # enumera la familia, no la muestrea


@pytest.mark.parametrize("motivo", sorted(MOTIVOS_DE_LA_TEXTURA, key=lambda m: m.value))
def test_cada_motivo_de_la_textura_manda_a_revision_y_se_cuenta(motivo: Motivo) -> None:
    medicion = _CASOS_POR_MOTIVO[motivo]()

    reporte = simular_revision([medicion], _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert reporte.total.n_aceptadas == 0
    assert dict(reporte.motivos)[motivo] == 1
    assert sum(cuenta for _, cuenta in reporte.motivos) >= 1


def test_los_motivos_del_reporte_cubren_todos_los_de_la_textura_en_el_orden_del_gate() -> None:
    reporte = simular_revision([_medicion()], _umbrales(), tolerancia_de_altura=TOLERANCIA)

    esperado = tuple(m for m in Motivo if m in MOTIVOS_DE_LA_TEXTURA)
    assert tuple(motivo for motivo, _ in reporte.motivos) == esperado
    assert all(cuenta == 0 for _, cuenta in reporte.motivos)


def test_una_textura_con_varios_motivos_cuenta_en_cada_uno() -> None:
    medicion = _medicion(acuerdo=50.0, signo=-1)

    reporte = simular_revision([medicion], _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert dict(reporte.motivos)[Motivo.SOBRE_EL_LIMITE] == 1
    assert dict(reporte.motivos)[Motivo.SIGNO_DISTINTO_DEL_ESPERADO] == 1
    assert reporte.total.n_en_revision == 1  # una sola textura en revisión


def test_el_reporte_cuenta_en_que_metrica_se_pasa_el_limite_en_el_orden_de_los_limites() -> None:
    mediciones = [
        _medicion(caso="A", acuerdo=50.0),  # solo el acuerdo
        _medicion(caso="B", acuerdo=50.0, residuo=0.5),  # acuerdo y residuo
        _medicion(caso="C", costura=9.0),  # solo la costura
        _medicion(caso="D"),  # ninguna
    ]

    reporte = simular_revision(mediciones, _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert reporte.sobre_el_limite == (
        ("acuerdo_p95_grados", 2),
        ("residuo_de_integrabilidad", 1),
        ("razon_de_costura", 1),
    )
    # Una sola fuente de verdad: las claves son exactamente los campos de LimitesDeAutoaceptacion.
    assert tuple(campo for campo, _ in reporte.sobre_el_limite) == tuple(
        campo.name for campo in fields(LimitesDeAutoaceptacion)
    )


def test_una_metrica_fuera_de_dominio_no_cuenta_como_pasada_de_limite() -> None:
    reporte = simular_revision([_medicion(acuerdo=181.0)], _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert dict(reporte.sobre_el_limite)["acuerdo_p95_grados"] == 0
    assert dict(reporte.motivos)[Motivo.METRICA_FUERA_DE_DOMINIO] == 1


def test_los_motivos_de_la_validacion_no_cuentan_ni_siquiera_con_evidencia_insuficiente() -> None:
    limites = _limites()
    huella = huella_del_candidato(VERSION_DE_METRICAS, 1, limites)
    validacion = ValidacionContraCorpusReal(
        id_del_corpus="real",
        huella_del_corpus=HUELLA_REAL,
        criterio_de_etiqueta="x",
        huella_del_candidato=huella,
        n_total=10,
        n_aceptadas=5,
        n_aceptadas_malas=5,
        confianza=0.9,
        alpha_objetivo=0.01,
        tasa_de_revision_maxima=0.5,
    )
    umbrales = _umbrales(limites, validacion=validacion)
    assert not validacion.alcanza
    ok = _medicion(acuerdo=1.0)
    assert Motivo.VALIDACION_INSUFICIENTE in decidir_con(ok.metricas, umbrales).motivos

    reporte = simular_revision([ok], umbrales, tolerancia_de_altura=TOLERANCIA)

    assert reporte.total.n_aceptadas == 1


def test_un_registro_con_otra_version_de_metricas_no_se_simula() -> None:
    umbrales = _umbrales(version_de_metricas="np-q0a.v0")

    with pytest.raises(RevisionError, match="registro"):
        simular_revision([_medicion()], umbrales, tolerancia_de_altura=TOLERANCIA)


@pytest.mark.parametrize("umbrales", [None, "x", 3])
def test_la_simulacion_exige_un_registro_de_umbrales(umbrales: object) -> None:
    with pytest.raises(ParametroDeRevisionInvalidoError):
        simular_revision([_medicion()], umbrales, tolerancia_de_altura=TOLERANCIA)  # type: ignore[arg-type]


def test_la_simulacion_exige_mediciones() -> None:
    with pytest.raises(ParametroDeRevisionInvalidoError, match="al menos una medición"):
        simular_revision([], _umbrales(), tolerancia_de_altura=TOLERANCIA)


def test_la_simulacion_rechaza_elementos_que_no_son_mediciones() -> None:
    with pytest.raises(ParametroDeRevisionInvalidoError):
        simular_revision([_metricas()], _umbrales(), tolerancia_de_altura=TOLERANCIA)  # type: ignore[list-item]


def test_el_reporte_por_escenario_suma_el_total_y_conserva_el_orden_de_aparicion() -> None:
    mediciones = [
        _medicion(caso="A", escenario="ruido", acuerdo=50.0, error=0.4),
        _medicion(caso="A", escenario="limpio", acuerdo=1.0, error=0.0),
        _medicion(caso="B", escenario="ruido", acuerdo=1.0, error=0.4),
        _medicion(caso="B", escenario="limpio", acuerdo=1.0, error=0.0),
    ]

    reporte = simular_revision(mediciones, _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert tuple(nombre for nombre, _ in reporte.por_escenario) == ("ruido", "limpio")
    por = dict(reporte.por_escenario)
    assert por["ruido"] == Conteo(n_total=2, n_aceptadas=1, n_malas=2, n_aceptadas_malas=1)
    assert por["limpio"] == Conteo(n_total=2, n_aceptadas=2, n_malas=0, n_aceptadas_malas=0)
    assert sum(c.n_total for c in por.values()) == reporte.total.n_total
    assert sum(c.n_aceptadas for c in por.values()) == reporte.total.n_aceptadas
    assert sum(c.n_malas for c in por.values()) == reporte.total.n_malas
    assert sum(c.n_aceptadas_malas for c in por.values()) == reporte.total.n_aceptadas_malas


# ---------------------------------------------------------------------------
# El conteo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argumentos",
    [
        {"n_total": 0, "n_aceptadas": 0, "n_malas": 0, "n_aceptadas_malas": 0},
        {"n_total": 2, "n_aceptadas": 3, "n_malas": 0, "n_aceptadas_malas": 0},
        {"n_total": 2, "n_aceptadas": 1, "n_malas": 3, "n_aceptadas_malas": 0},
        {"n_total": 4, "n_aceptadas": 1, "n_malas": 3, "n_aceptadas_malas": 2},
        {"n_total": 4, "n_aceptadas": 3, "n_malas": 1, "n_aceptadas_malas": 2},
        # 3 malas con 1 aceptada: 2 malas tendrían que estar en revisión, pero solo hay 1 textura en revisión.
        {"n_total": 4, "n_aceptadas": 3, "n_malas": 3, "n_aceptadas_malas": 1},
        {"n_total": -1, "n_aceptadas": 0, "n_malas": 0, "n_aceptadas_malas": 0},
        {"n_total": True, "n_aceptadas": 1, "n_malas": 0, "n_aceptadas_malas": 0},
        {"n_total": 2.0, "n_aceptadas": 1, "n_malas": 0, "n_aceptadas_malas": 0},
    ],
)
def test_un_conteo_incoherente_no_se_construye(argumentos: dict[str, object]) -> None:
    with pytest.raises(RevisionError):
        Conteo(**argumentos)  # type: ignore[arg-type]


def test_la_tasa_de_revision_es_un_solo_cociente_de_enteros_igual_que_en_el_gate() -> None:
    # 1000 con 700 aceptadas: (1000 - 700) / 1000 == 0.3; 1.0 - 0.7 daría 0.30000000000000004.
    conteo = Conteo(n_total=1000, n_aceptadas=700, n_malas=0, n_aceptadas_malas=0)

    assert conteo.tasa_de_revision == 0.3


# ---------------------------------------------------------------------------
# Dejar un caso fuera
# ---------------------------------------------------------------------------


def _tres_casos_con_acuerdo_creciente() -> list[Medicion]:
    return [_medicion(caso="A", acuerdo=1.0), _medicion(caso="B", acuerdo=2.0), _medicion(caso="C", acuerdo=3.0)]


def test_con_los_limites_de_los_mismos_casos_todo_se_acepta_y_dejando_uno_fuera_no() -> None:
    mediciones = _tres_casos_con_acuerdo_creciente()

    limites_de_todos = derivar_limites(mediciones, cuantil=1.0, tolerancia_de_altura=TOLERANCIA)
    en_la_muestra = simular_revision(mediciones, _umbrales(limites_de_todos), tolerancia_de_altura=TOLERANCIA)
    fuera_de_la_muestra = simular_dejando_un_caso_fuera(
        mediciones,
        cuantil=1.0,
        tolerancia_de_altura=TOLERANCIA,
        signo_esperado=1,
        huella_del_corpus=HUELLA_DEL_CORPUS,
    )

    assert en_la_muestra.total.n_aceptadas == 3
    # A y B se evalúan con límite 3; C con límite 2 (derivado de A y B): C queda en revisión.
    assert fuera_de_la_muestra.total.n_aceptadas == 2
    assert fuera_de_la_muestra.total.n_total == 3


def test_dejar_un_caso_fuera_deja_fuera_todos_los_escenarios_de_ese_caso() -> None:
    mediciones = [
        _medicion(caso="A", escenario="limpio", acuerdo=1.0),
        _medicion(caso="A", escenario="ruido", acuerdo=3.0),
        _medicion(caso="B", escenario="limpio", acuerdo=2.0),
    ]

    reporte = simular_dejando_un_caso_fuera(
        mediciones,
        cuantil=1.0,
        tolerancia_de_altura=TOLERANCIA,
        signo_esperado=1,
        huella_del_corpus=HUELLA_DEL_CORPUS,
    )

    # Fuera A: límite = 2 (solo B). A/limpio (1) pasa, A/ruido (3) no. Fuera B: límite = 3. B (2) pasa.
    assert reporte.total.n_aceptadas == 2
    assert dict(reporte.por_escenario)["ruido"].n_aceptadas == 0


def test_dejar_un_caso_fuera_evalua_cada_medicion_exactamente_una_vez() -> None:
    mediciones = [_medicion(caso=c, escenario=e) for c in "ABC" for e in ("limpio", "ruido")]

    reporte = simular_dejando_un_caso_fuera(
        mediciones,
        cuantil=1.0,
        tolerancia_de_altura=TOLERANCIA,
        signo_esperado=1,
        huella_del_corpus=HUELLA_DEL_CORPUS,
    )

    assert reporte.total.n_total == len(mediciones)


def test_un_control_negativo_dejado_fuera_se_evalua_contra_limites_derivados_de_los_casos_buenos() -> None:
    mediciones = [
        _medicion(caso="A", acuerdo=1.0, costura=1.0),
        _medicion(caso="B", acuerdo=1.0, costura=1.1),
        _medicion(caso="N", acuerdo=1.0, costura=127.0, control=True),
    ]

    reporte = simular_dejando_un_caso_fuera(
        mediciones,
        cuantil=1.0,
        tolerancia_de_altura=TOLERANCIA,
        signo_esperado=1,
        huella_del_corpus=HUELLA_DEL_CORPUS,
    )

    # El control (costura 127) se evalúa con límite de costura 1.1: queda en revisión y es una mala bien detectada.
    assert reporte.total.n_malas == 1
    assert reporte.total.n_aceptadas_malas == 0


def test_un_control_invisible_para_las_metricas_queda_identificado_como_falso_aceptado() -> None:
    mediciones = [
        _medicion(caso="A", costura=1.0),
        _medicion(caso="B", costura=1.1),
        _medicion(caso="N", costura=1.0, control=True),  # un control cuyas métricas parecen las de un caso bueno
    ]

    reporte = simular_dejando_un_caso_fuera(
        mediciones,
        cuantil=1.0,
        tolerancia_de_altura=TOLERANCIA,
        signo_esperado=1,
        huella_del_corpus=HUELLA_DEL_CORPUS,
    )

    assert reporte.falsos_aceptos == (("N", "limpio"),)


def test_dejar_un_caso_fuera_necesita_al_menos_dos_casos() -> None:
    with pytest.raises(ParametroDeRevisionInvalidoError):
        simular_dejando_un_caso_fuera(
            [_medicion(caso="A"), _medicion(caso="A", escenario="ruido")],
            cuantil=1.0,
            tolerancia_de_altura=TOLERANCIA,
            signo_esperado=1,
            huella_del_corpus=HUELLA_DEL_CORPUS,
        )


def test_si_al_dejar_un_caso_fuera_no_quedan_buenas_la_simulacion_falla_en_voz_alta() -> None:
    mediciones = [_medicion(caso="A"), _medicion(caso="B", control=True)]

    with pytest.raises(SinMedicionesBuenasError):
        simular_dejando_un_caso_fuera(
            mediciones,
            cuantil=1.0,
            tolerancia_de_altura=TOLERANCIA,
            signo_esperado=1,
            huella_del_corpus=HUELLA_DEL_CORPUS,
        )


@pytest.mark.parametrize("huella", ["", "abc", "A" * 64, None, 3])
def test_la_huella_del_corpus_es_un_sha256_en_hexadecimal_minuscula(huella: object) -> None:
    with pytest.raises(ParametroDeRevisionInvalidoError, match="huella"):
        simular_dejando_un_caso_fuera(
            _tres_casos_con_acuerdo_creciente(),
            cuantil=1.0,
            tolerancia_de_altura=TOLERANCIA,
            signo_esperado=1,
            huella_del_corpus=huella,  # type: ignore[arg-type]
        )


def test_la_huella_de_cada_pliegue_depende_del_corpus_y_del_caso_que_se_deja_fuera() -> None:
    otra_huella_del_corpus = "d" * 64

    de_a = _huella_del_pliegue(HUELLA_DEL_CORPUS, "A")

    assert re.fullmatch(r"[0-9a-f]{64}", de_a)
    assert de_a == _huella_del_pliegue(HUELLA_DEL_CORPUS, "A")
    assert de_a != _huella_del_pliegue(HUELLA_DEL_CORPUS, "B")
    assert de_a != _huella_del_pliegue(otra_huella_del_corpus, "A")


def test_el_orden_de_las_mediciones_no_cambia_el_resultado_de_dejar_un_caso_fuera() -> None:
    mediciones = _tres_casos_con_acuerdo_creciente()
    argumentos = {
        "cuantil": 1.0,
        "tolerancia_de_altura": TOLERANCIA,
        "signo_esperado": 1,
        "huella_del_corpus": HUELLA_DEL_CORPUS,
    }

    adelante = simular_dejando_un_caso_fuera(mediciones, **argumentos)  # type: ignore[arg-type]
    atras = simular_dejando_un_caso_fuera(list(reversed(mediciones)), **argumentos)  # type: ignore[arg-type]

    assert adelante.total == atras.total


def test_el_reporte_es_inmutable() -> None:
    reporte = simular_revision([_medicion()], _umbrales(), tolerancia_de_altura=TOLERANCIA)

    assert isinstance(reporte, ReporteDeRevision)
    with pytest.raises(AttributeError):
        reporte.tolerancia_de_altura = 1.0  # type: ignore[misc]

"""NP-X0b (parte con numpy): medir una textura y el corpus sintético completo con las funciones que ya existen.

Qué se ancla (y con qué):

- **El cableado**: ``medir_textura`` llama a ``integrar`` y a las métricas del gate con los MISMOS bordes y la misma
  convención. Se compara contra un recálculo independiente hecho acá, y con bordes distintos los números cambian
  (un cableado que fijara los bordes en una llamada no lo vería un test de un solo borde).
- **Los escenarios salen de la especificación** (NP-D0), enumerados por igualdad literal: cambiar la especificación
  sin revisar este benchmark rompe el test.
- **El corpus completo se enumera**: cada caso × cada escenario, una vez, con los controles marcados.
- **Qué es el error de altura**: RMSE entre las alturas normalizadas min-max; «plano contra plano» es 0 y «plano contra
  relieve» no tiene valor (``None``).

Límite declarado: es el corpus sintético. Estos números no validan ningún umbral.
"""

from __future__ import annotations

import json
import re

import numpy as np
import pytest

from sky_claw.local.native_parallax.bench.sintetico import (
    ESPECIFICACION_V1,
    IDS_CONTROLES,
    IDS_PERIODICOS,
    ModeloCuantizacion,
    ParametroInvalidoError,
    cuantizar_normal,
    generar_altura,
    huella_especificacion,
    normal_desde_altura,
    perturbar_normal,
)
from sky_claw.local.native_parallax.experiments import medicion
from sky_claw.local.native_parallax.experiments.medicion import (
    CorpusMedido,
    Escenario,
    _descripcion_del_corpus_medido,
    escenarios_de_la_especificacion,
    medir_corpus_sintetico,
    medir_textura,
)
from sky_claw.local.native_parallax.experiments.revision import Medicion, ParametroDeRevisionInvalidoError
from sky_claw.local.native_parallax.gates import (
    VERSION_DE_METRICAS,
    UmbralesInvalidosError,
    ValidacionContraCorpusReal,
)
from sky_claw.local.native_parallax.integrate import (
    Bordes,
    EntradaInvalidaError,
    ModoNormalizacion,
    integrar,
    normalizar_altura,
)
from sky_claw.local.native_parallax.metrics import (
    acuerdo_normal,
    contar_no_finitos,
    razon_de_costura,
    residuo_de_integrabilidad,
)

N = 128

NOMBRES_DE_LOS_ESCENARIOS = (
    "limpio",
    "ruido_1_255",
    "ruido_2_255",
    "ruido_4_255",
    "ruido_8_255",
    "cuantizacion_8b_tres_canales",
    "cuantizacion_8b_dos_canales_reconstruye_z",
    "cuantizacion_10b_tres_canales",
    "cuantizacion_10b_dos_canales_reconstruye_z",
    "cuantizacion_16b_tres_canales",
    "cuantizacion_16b_dos_canales_reconstruye_z",
)


def _normal(caso: str) -> np.ndarray:
    return normal_desde_altura(generar_altura(caso, N, N))


# ---------------------------------------------------------------------------
# Los escenarios
# ---------------------------------------------------------------------------


def test_los_escenarios_salen_de_la_especificacion_y_se_enumeran() -> None:
    escenarios = escenarios_de_la_especificacion()

    assert tuple(e.nombre for e in escenarios) == NOMBRES_DE_LOS_ESCENARIOS
    assert all(isinstance(e, Escenario) for e in escenarios)
    # 1 limpio + ruidos + bits × modelos de la especificación vigente.
    esperados = (
        1
        + len(ESPECIFICACION_V1.sigmas_ruido)
        + len(ESPECIFICACION_V1.bits_cuantizacion) * len(ESPECIFICACION_V1.modelos_cuantizacion)
    )
    assert len(escenarios) == esperados


def test_el_escenario_limpio_devuelve_la_normal_tal_cual() -> None:
    normal = _normal("S05_diagonal")

    limpio = escenarios_de_la_especificacion()[0]

    assert limpio.nombre == "limpio"
    assert limpio.degradar(normal, "S05_diagonal") is normal


@pytest.mark.parametrize("sigma", ESPECIFICACION_V1.sigmas_ruido)
def test_cada_escenario_de_ruido_aplica_el_ruido_de_su_sigma_con_la_semilla_del_caso(sigma: float) -> None:
    normal = _normal("S07_bumps")
    nombre = f"ruido_{sigma * 255:g}_255"
    escenario = next(e for e in escenarios_de_la_especificacion() if e.nombre == nombre)

    obtenida = escenario.degradar(normal, "S07_bumps")

    np.testing.assert_array_equal(obtenida, perturbar_normal(normal, sigma, "S07_bumps"))


@pytest.mark.parametrize("bits", ESPECIFICACION_V1.bits_cuantizacion)
@pytest.mark.parametrize("modelo", list(ModeloCuantizacion))
def test_cada_escenario_de_cuantizacion_aplica_sus_bits_y_su_modelo(bits: int, modelo: ModeloCuantizacion) -> None:
    normal = _normal("S07_bumps")
    escenario = next(e for e in escenarios_de_la_especificacion() if e.nombre == f"cuantizacion_{bits}b_{modelo.value}")

    obtenida = escenario.degradar(normal, "S07_bumps")

    np.testing.assert_array_equal(obtenida, cuantizar_normal(normal, bits, modelo=modelo))


def test_los_escenarios_de_ruido_distintos_dan_normales_distintas() -> None:
    normal = _normal("S07_bumps")
    ruidos = [e for e in escenarios_de_la_especificacion() if e.nombre.startswith("ruido_")]

    resultados = [e.degradar(normal, "S07_bumps") for e in ruidos]

    for i, a in enumerate(resultados):
        for b in resultados[i + 1 :]:
            assert not np.array_equal(a, b)


# ---------------------------------------------------------------------------
# Medir una textura
# ---------------------------------------------------------------------------


def _medir(normal: np.ndarray, caso: str, bordes: Bordes = Bordes.PERIODICOS, escenario: str = "x") -> Medicion:
    return medir_textura(
        normal,
        caso=caso,
        escenario=escenario,
        es_control_negativo=False,
        altura_verdadera=generar_altura(caso, N, N),
        bordes=bordes,
    )


def test_una_normal_limpia_se_mide_perfecta() -> None:
    medicion = _medir(_normal("S05_diagonal"), "S05_diagonal")

    assert medicion.caso == "S05_diagonal"
    assert medicion.es_control_negativo is False
    assert medicion.metricas.acuerdo_p95_grados < 1e-6
    assert medicion.metricas.residuo_de_integrabilidad < 1e-9
    assert medicion.metricas.razon_de_costura == pytest.approx(1.0, abs=0.5)
    assert medicion.metricas.signo == 1
    assert medicion.metricas.no_finitos == 0
    assert medicion.error_de_altura is not None
    assert medicion.error_de_altura < 1e-9


@pytest.mark.parametrize("bordes", list(Bordes))
@pytest.mark.parametrize("escenario", ["limpio", "ruido_4_255", "cuantizacion_8b_dos_canales_reconstruye_z"])
def test_medir_cablea_las_metricas_del_gate_con_los_mismos_bordes(bordes: Bordes, escenario: str) -> None:
    caso = "S06_multifreq"
    degradar = next(e for e in escenarios_de_la_especificacion() if e.nombre == escenario).degradar
    normal = degradar(_normal(caso), caso)

    medicion = _medir(normal, caso, bordes, escenario)

    integrada = integrar(normal, bordes=bordes)
    acuerdo = acuerdo_normal(normal, integrada.altura, bordes=bordes)
    assert medicion.metricas.acuerdo_p95_grados == acuerdo.p95_grados
    assert medicion.metricas.signo == acuerdo.signo
    assert medicion.metricas.residuo_de_integrabilidad == residuo_de_integrabilidad(normal, bordes=bordes)
    assert medicion.metricas.razon_de_costura == razon_de_costura(integrada.altura)
    assert medicion.metricas.no_finitos == contar_no_finitos(integrada.altura)


def test_los_bordes_cambian_las_mediciones_de_una_normal_con_ruido() -> None:
    caso = "S06_multifreq"
    normal = perturbar_normal(_normal(caso), 4 / 255, caso)

    periodicos = _medir(normal, caso, Bordes.PERIODICOS)
    neumann = _medir(normal, caso, Bordes.NEUMANN)

    assert periodicos.metricas != neumann.metricas


def test_el_error_de_altura_es_el_rmse_de_las_alturas_normalizadas_min_max() -> None:
    caso = "S06_multifreq"
    normal = perturbar_normal(_normal(caso), 4 / 255, caso)
    integrada = integrar(normal, bordes=Bordes.PERIODICOS)
    a = normalizar_altura(integrada.altura, ModoNormalizacion.MIN_MAX)
    b = normalizar_altura(generar_altura(caso, N, N), ModoNormalizacion.MIN_MAX)
    esperado = float(np.sqrt(np.mean((a - b) ** 2)))

    medicion = _medir(normal, caso)

    assert esperado > 1e-6  # con ruido hay error de verdad: no es un cero que pase por casualidad
    assert medicion.error_de_altura == pytest.approx(esperado, rel=1e-12)


def test_plano_contra_plano_no_tiene_error() -> None:
    medicion = _medir(_normal("S01_flat"), "S01_flat")

    assert medicion.error_de_altura == 0.0
    assert medicion.metricas.no_finitos == 0


def test_un_relieve_espurio_sobre_una_verdad_plana_no_tiene_error_definido() -> None:
    caso = "S01_flat"
    ruidosa = perturbar_normal(_normal(caso), 1 / 255, caso)

    medicion = _medir(ruidosa, caso)

    assert medicion.error_de_altura is None


CASOS_NO_PLANOS = tuple(caso for caso in IDS_PERIODICOS if caso != "S01_flat")


def _con_x_e_y_invertidos(normal: np.ndarray) -> np.ndarray:
    salida = normal.copy()
    salida[..., 0] = -salida[..., 0]
    salida[..., 1] = -salida[..., 1]
    return salida


@pytest.mark.parametrize("caso", CASOS_NO_PLANOS)
def test_la_inversion_global_no_se_ve_en_el_signo_cuando_la_altura_sale_de_la_misma_normal(caso: str) -> None:
    """Hallazgo de NP-X0b: el signo de ``acuerdo_normal`` es +1 POR CONSTRUCCIÓN si la altura es la integrada de esa
    misma normal, así que ``SIGNO_DISTINTO_DEL_ESPERADO`` no puede dispararse en el pipeline normal → altura.
    Solo el ground truth (``error_de_altura``) delata una normal con X e Y invertidos.
    """
    invertida = _con_x_e_y_invertidos(_normal(caso))

    medicion = _medir(invertida, caso)

    assert medicion.metricas.signo == 1
    assert medicion.error_de_altura is not None
    assert medicion.error_de_altura > 0.1


@pytest.mark.parametrize("caso", CASOS_NO_PLANOS)
def test_el_signo_si_delata_la_inversion_global_contra_una_altura_independiente(caso: str) -> None:
    """La otra mitad de la propiedad: con una altura que NO sale de la normal, el signo vale -1 si está invertida."""
    altura = generar_altura(caso, N, N)
    normal = _normal(caso)

    sin_invertir = acuerdo_normal(normal, altura, bordes=Bordes.PERIODICOS)
    invertida = acuerdo_normal(_con_x_e_y_invertidos(normal), altura, bordes=Bordes.PERIODICOS)

    assert (sin_invertir.signo, invertida.signo) == (1, -1)


def test_la_altura_verdadera_tiene_que_tener_la_forma_de_la_normal() -> None:
    with pytest.raises(EntradaInvalidaError):
        medir_textura(
            _normal("S05_diagonal"),
            caso="S05_diagonal",
            escenario="x",
            es_control_negativo=False,
            altura_verdadera=generar_altura("S05_diagonal", N, 2 * N),
            bordes=Bordes.PERIODICOS,
        )


def test_el_control_negativo_se_marca_en_la_medicion_pero_no_cambia_las_metricas() -> None:
    normal = _normal("N01_ramp")
    altura = generar_altura("N01_ramp", N, N)

    como_control = medir_textura(
        normal,
        caso="N01_ramp",
        escenario="x",
        es_control_negativo=True,
        altura_verdadera=altura,
        bordes=Bordes.PERIODICOS,
    )
    como_comun = medir_textura(
        normal,
        caso="N01_ramp",
        escenario="x",
        es_control_negativo=False,
        altura_verdadera=altura,
        bordes=Bordes.PERIODICOS,
    )

    assert como_control.es_control_negativo is True
    assert como_control.metricas == como_comun.metricas
    assert como_control.metricas.razon_de_costura > 10  # la costura delata el control aunque la altura salga bien


# ---------------------------------------------------------------------------
# El corpus sintético completo
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> CorpusMedido:
    return medir_corpus_sintetico(N, N, bordes=Bordes.PERIODICOS)


def test_el_corpus_mide_cada_caso_por_cada_escenario_exactamente_una_vez(corpus: CorpusMedido) -> None:
    pares = [(m.caso, m.escenario) for m in corpus.mediciones]

    assert len(pares) == len(set(pares))
    esperados = {(caso, nombre) for caso in (*IDS_PERIODICOS, *IDS_CONTROLES) for nombre in NOMBRES_DE_LOS_ESCENARIOS}
    assert set(pares) == esperados
    assert len(corpus.mediciones) == len(esperados)


def test_los_controles_negativos_son_exactamente_los_de_la_especificacion(corpus: CorpusMedido) -> None:
    marcados = {m.caso for m in corpus.mediciones if m.es_control_negativo}

    assert marcados == set(IDS_CONTROLES)


def test_en_el_escenario_limpio_toda_la_altura_se_recupera_con_poco_error(corpus: CorpusMedido) -> None:
    limpias = [m for m in corpus.mediciones if m.escenario == "limpio"]

    assert all(m.error_de_altura is not None and m.error_de_altura < 0.01 for m in limpias)


def test_la_costura_separa_los_controles_de_los_periodicos_en_el_escenario_limpio(corpus: CorpusMedido) -> None:
    limpias = [m for m in corpus.mediciones if m.escenario == "limpio"]
    periodicos = [m.metricas.razon_de_costura for m in limpias if not m.es_control_negativo]
    controles = [m.metricas.razon_de_costura for m in limpias if m.es_control_negativo]

    assert max(periodicos) < 2.0
    assert min(controles) > 10.0


def test_medir_dos_veces_da_el_mismo_corpus(corpus: CorpusMedido) -> None:
    otra_vez = medir_corpus_sintetico(N, N, bordes=Bordes.PERIODICOS)

    assert otra_vez == corpus


def test_la_huella_del_corpus_medido_es_la_de_la_especificacion_de_np_d0(corpus: CorpusMedido) -> None:
    assert corpus.huella == huella_especificacion(ESPECIFICACION_V1)
    assert re.fullmatch(r"[0-9a-f]{64}", corpus.huella)


def test_el_gate_rechaza_como_validacion_la_huella_del_corpus_medido(corpus: CorpusMedido) -> None:
    """Codex, #763: la huella del corpus medido era un hash sintético NUEVO que el guard del gate no conocía, y con
    ella una validación con conteos favorables daba ``alcanza`` verdadero. El corpus medido ES un subconjunto del de
    NP-D0 (que el gate ya rechaza), así que lleva SU huella y el guard lo cubre."""
    with pytest.raises(UmbralesInvalidosError, match="sintético"):
        ValidacionContraCorpusReal(
            id_del_corpus="corpus-medido",
            huella_del_corpus=corpus.huella,
            criterio_de_etiqueta="x",
            huella_del_candidato="a" * 64,
            n_total=1000,
            n_aceptadas=900,
            n_aceptadas_malas=0,
            confianza=0.9,
            alpha_objetivo=0.05,
            tasa_de_revision_maxima=0.5,
        )


def test_la_huella_es_la_misma_en_cualquier_grilla_y_la_descripcion_distingue_lo_medido(corpus: CorpusMedido) -> None:
    otra = medir_corpus_sintetico(N, 2 * N, bordes=Bordes.PERIODICOS)

    assert otra.huella == corpus.huella  # el mismo corpus de NP-D0, medido en otra grilla
    assert otra.descripcion != corpus.descripcion


# Valor de referencia de lo que se mide y cómo: cambiar cualquier elemento invalida en silencio toda derivación
# registrada contra esta descripción. Si este test falla, el cambio fue deliberado: actualizá el valor Y subí
# ``VERSION_DE_LA_MEDICION``.
def _descripcion_de_referencia() -> dict[str, object]:
    return {
        "version_de_la_medicion": "np-x0b.v1",
        "version_de_metricas": VERSION_DE_METRICAS,
        "especificacion": huella_especificacion(ESPECIFICACION_V1),
        "grilla": [N, N],
        "bordes": "periodicos",
        "convencion": "directx",
        "escenarios": list(NOMBRES_DE_LOS_ESCENARIOS),
    }


def test_la_descripcion_del_corpus_medido_tiene_un_valor_de_referencia_congelado(corpus: CorpusMedido) -> None:
    assert json.loads(corpus.descripcion) == _descripcion_de_referencia()
    assert VERSION_DE_METRICAS == "np-q0a.v1"  # si sube, esta referencia sube con ella (a propósito)


def test_la_descripcion_es_json_canonico_para_poder_compararla_como_texto(corpus: CorpusMedido) -> None:
    reserializada = json.dumps(json.loads(corpus.descripcion), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    assert corpus.descripcion == reserializada


def test_la_descripcion_cambia_si_cambia_la_version_de_las_metricas(
    corpus: CorpusMedido, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Al subir ``VERSION_DE_METRICAS`` (cambió una definición) las mediciones cambian: la descripción tiene que decirlo."""
    monkeypatch.setattr(medicion, "VERSION_DE_METRICAS", "np-q0a.vX")

    nueva = _descripcion_del_corpus_medido(N, N, Bordes.PERIODICOS, escenarios_de_la_especificacion())

    assert nueva != corpus.descripcion


def test_la_descripcion_depende_de_los_bordes_y_de_los_escenarios_que_se_midieron() -> None:
    escenarios = escenarios_de_la_especificacion()

    base = _descripcion_del_corpus_medido(N, N, Bordes.PERIODICOS, escenarios)

    assert _descripcion_del_corpus_medido(N, N, Bordes.NEUMANN, escenarios) != base
    assert _descripcion_del_corpus_medido(N, N, Bordes.PERIODICOS, escenarios[:-1]) != base


def test_la_grilla_se_valida_antes_de_medir_nada() -> None:
    with pytest.raises(ParametroInvalidoError):
        medir_corpus_sintetico(64, 64, bordes=Bordes.PERIODICOS)


@pytest.mark.parametrize("bordes", [Bordes.NEUMANN, None, "periodicos", 1])
def test_el_corpus_solo_se_mide_con_bordes_periodicos(bordes: object) -> None:
    """Los controles negativos (N01/N02) lo son PORQUE violan la periodicidad: son del integrador periódico. Bajo
    Neumann son representables, así que etiquetarlos «malos» corrompería las tasas. Qué significa «mala» con otros
    bordes no está decidido: el runner se niega en vez de adivinarlo."""
    with pytest.raises(ParametroDeRevisionInvalidoError, match="peri"):
        medir_corpus_sintetico(N, N, bordes=bordes)  # type: ignore[arg-type]

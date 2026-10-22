"""NP-O0b1: reconciliación PURA de un corte a mitad de la escritura de la salida gestionada.

El escritor (NP-O0b2) guarda su intención en un manifiesto «pendiente» ANTES de tocar el disco y escribe el manifiesto
definitivo AL FINAL (el punto de compromiso). Si el proceso muere en el medio, el próximo arranque tiene tres cosas: el
manifiesto vigente, el pendiente y lo que hay en disco (más una «papelera» donde el escritor aparta un archivo antes de
verificarlo). `reconciliar_tras_corte` decide, sin tocar nada, qué manifiesto queda en pie y qué hacer con la papelera.

La tabla se ENUMERA entera (4 relaciones × 5 estados de disco × 3 de papelera = 60 estados) y se compara por igualdad
literal contra una tabla escrita a mano: agregar una dimensión sin ampliarla rompe el test.
"""

from __future__ import annotations

import itertools
from enum import Enum
from typing import Any

import pytest

from sky_claw.local.native_parallax import output
from sky_claw.local.native_parallax.output import (
    EncoderUsado,
    EntradaDeManifiesto,
    EstadoTrasCorte,
    Manifiesto,
    NivelDeReproducibilidad,
    Observacion,
    OrigenDeEntrada,
    PlanInvalidoError,
    Procedencia,
    RecuperacionDeCorte,
    RutaDeSalidaInvalidaError,
    TipoObservado,
    clave_de_ruta,
    crear_entrada,
    parametros_canonicos,
    reconciliar_tras_corte,
)

H_M = "a" * 64  # los bytes de la entrada vigente (la del manifiesto)
H_P = "b" * 64  # los bytes de la entrada pendiente
H_X = "c" * 64  # bytes de un tercero
RUTA = "textures/architecture/whiterun/muro01_p.dds"


def _procedencia(escala: float = 1.5) -> Procedencia:
    return Procedencia(
        algoritmo_id="normal_fft_periodic_v1",
        parametros=parametros_canonicos({"borde": "periodico", "escala": escala}),
        origenes=(OrigenDeEntrada(rol="normal", sha256=H_X),),
        encoder=EncoderUsado(
            exportador_id="np_e0b_bc4_v1",
            texconv_version="may2026",
            texconv_sha256=H_X,
            argumentos=("-f", "BC4_UNORM"),
        ),
        entorno=(("numpy", "2.4.6"), ("plataforma", "win32")),
        nivel=NivelDeReproducibilidad.DETERMINISTA_MISMO_ENTORNO,
    )


def _entrada_m(ruta: str = RUTA) -> EntradaDeManifiesto:
    return crear_entrada(ruta, H_M, 1000, _procedencia(1.5))


def _entrada_p(ruta: str = RUTA) -> EntradaDeManifiesto:
    return crear_entrada(ruta, H_P, 2000, _procedencia(2.5))


def _manifiesto(*entradas: EntradaDeManifiesto) -> Manifiesto:
    return Manifiesto.ordenado(entradas)


def _estado(disco: Observacion, papelera: str | None = None) -> EstadoTrasCorte:
    return EstadoTrasCorte(disco=disco, papelera=papelera)


AUSENTE = Observacion(TipoObservado.AUSENTE, None)
OTRO = Observacion(TipoObservado.OTRO, None)


def _archivo(sha256: str) -> Observacion:
    return Observacion(TipoObservado.ARCHIVO, sha256)


# ---------------------------------------------------------------------------
# La tabla enumerada
# ---------------------------------------------------------------------------


class Rel(Enum):
    """Cómo se relacionan el manifiesto vigente (m) y el pendiente (p) en UNA ruta."""

    SOLO_P = "solo_p"  # se quería crear: m no la tiene
    SOLO_M = "solo_m"  # se quería soltar o retirar: p no la tiene
    IGUAL = "igual"  # m y p tienen la MISMA entrada
    DISTINTO = "distinto"  # se quería reemplazar: p tiene otra entrada


class Disco(Enum):
    AUSENTE = "ausente"
    ARCHIVO_M = "archivo_m"  # un archivo con los bytes de m
    ARCHIVO_P = "archivo_p"  # un archivo con los bytes de p
    ARCHIVO_X = "archivo_x"  # un archivo con otros bytes
    OTRO = "otro"  # un directorio, un enlace...


class Papelera(Enum):
    NINGUNA = "ninguna"
    DE_M = "de_m"  # un archivo apartado con los bytes de m: es nuestro
    AJENA = "ajena"  # un archivo apartado con otros bytes: NO es nuestro


class Efecto(Enum):
    NADA = "nada"  # la papelera no cambia nada
    DESCARTAR = "descartar"  # un archivo nuestro apartado que ya no hace falta
    RESTAURAR = "restaurar"  # el archivo nuestro apartado vuelve a su ruta


# (manifiesto que queda SIN papelera de m, manifiesto que queda CON papelera de m, qué pasa con la papelera de m).
# «m» y «p» nombran la entrada vigente y la pendiente. Escrita a mano, fila por fila, sin mirar la implementación.
_FILAS: dict[tuple[Rel, Disco], tuple[str | None, str | None, Efecto]] = {
    (Rel.SOLO_P, Disco.AUSENTE): (None, None, Efecto.NADA),
    (Rel.SOLO_P, Disco.ARCHIVO_M): (None, None, Efecto.NADA),
    (Rel.SOLO_P, Disco.ARCHIVO_P): ("p", "p", Efecto.NADA),
    (Rel.SOLO_P, Disco.ARCHIVO_X): (None, None, Efecto.NADA),
    (Rel.SOLO_P, Disco.OTRO): (None, None, Efecto.NADA),
    (Rel.SOLO_M, Disco.AUSENTE): (None, None, Efecto.DESCARTAR),
    (Rel.SOLO_M, Disco.ARCHIVO_M): ("m", "m", Efecto.DESCARTAR),
    (Rel.SOLO_M, Disco.ARCHIVO_P): (None, None, Efecto.DESCARTAR),
    (Rel.SOLO_M, Disco.ARCHIVO_X): (None, None, Efecto.DESCARTAR),
    (Rel.SOLO_M, Disco.OTRO): (None, None, Efecto.DESCARTAR),
    (Rel.IGUAL, Disco.AUSENTE): ("m", "m", Efecto.RESTAURAR),
    (Rel.IGUAL, Disco.ARCHIVO_M): ("m", "m", Efecto.DESCARTAR),
    (Rel.IGUAL, Disco.ARCHIVO_P): ("m", "m", Efecto.DESCARTAR),
    (Rel.IGUAL, Disco.ARCHIVO_X): ("m", "m", Efecto.DESCARTAR),
    (Rel.IGUAL, Disco.OTRO): ("m", "m", Efecto.DESCARTAR),
    (Rel.DISTINTO, Disco.AUSENTE): (None, "m", Efecto.RESTAURAR),
    (Rel.DISTINTO, Disco.ARCHIVO_M): ("m", "m", Efecto.DESCARTAR),
    (Rel.DISTINTO, Disco.ARCHIVO_P): ("p", "p", Efecto.DESCARTAR),
    (Rel.DISTINTO, Disco.ARCHIVO_X): (None, None, Efecto.DESCARTAR),
    (Rel.DISTINTO, Disco.OTRO): (None, None, Efecto.DESCARTAR),
}


def _tabla_esperada() -> dict[tuple[Rel, Disco, Papelera], tuple[str | None, bool, bool]]:
    """Expande las 20 filas a los 60 estados: (manifiesto que queda, ¿restaurar?, ¿descartar?)."""
    tabla: dict[tuple[Rel, Disco, Papelera], tuple[str | None, bool, bool]] = {}
    for (rel, disco), (sin_papelera, con_papelera, efecto) in _FILAS.items():
        for papelera in (Papelera.NINGUNA, Papelera.AJENA):  # una papelera ajena no es nuestra: no cambia nada
            tabla[(rel, disco, papelera)] = (sin_papelera, False, False)
        tabla[(rel, disco, Papelera.DE_M)] = (
            con_papelera,
            efecto is Efecto.RESTAURAR,
            efecto is Efecto.DESCARTAR,
        )
    return tabla


TABLA_ESPERADA = _tabla_esperada()


def _escenario(rel: Rel, disco: Disco, papelera: Papelera) -> tuple[Manifiesto | None, Manifiesto, EstadoTrasCorte]:
    m, p = _entrada_m(), _entrada_p()
    previo, pendiente = {
        Rel.SOLO_P: (None, _manifiesto(p)),
        Rel.SOLO_M: (_manifiesto(m), _manifiesto()),
        Rel.IGUAL: (_manifiesto(m), _manifiesto(m)),
        Rel.DISTINTO: (_manifiesto(m), _manifiesto(p)),
    }[rel]
    observacion = {
        Disco.AUSENTE: AUSENTE,
        Disco.ARCHIVO_M: _archivo(H_M),
        Disco.ARCHIVO_P: _archivo(H_P),
        Disco.ARCHIVO_X: _archivo(H_X),
        Disco.OTRO: OTRO,
    }[disco]
    huella_de_papelera = {Papelera.NINGUNA: None, Papelera.DE_M: H_M, Papelera.AJENA: H_X}[papelera]
    return previo, pendiente, _estado(observacion, huella_de_papelera)


def _quien(manifiesto: Manifiesto) -> str | None:
    """«m», «p» o None según qué entrada quedó para ``RUTA`` (se distinguen por sus bytes)."""
    por_ruta = {clave_de_ruta(e.ruta): e for e in manifiesto.entradas}
    entrada = por_ruta.get(clave_de_ruta(RUTA))
    if entrada is None:
        return None
    return {H_M: "m", H_P: "p"}[entrada.sha256]


def test_la_tabla_enumera_todas_las_combinaciones() -> None:
    todas = set(itertools.product(Rel, Disco, Papelera))
    assert set(TABLA_ESPERADA) == todas
    assert len(todas) == 60


@pytest.mark.parametrize(("rel", "disco", "papelera"), sorted(TABLA_ESPERADA, key=lambda k: tuple(x.value for x in k)))
def test_cada_estado_tiene_la_decision_de_la_tabla(rel: Rel, disco: Disco, papelera: Papelera) -> None:
    previo, pendiente, estado = _escenario(rel, disco, papelera)

    recuperacion = reconciliar_tras_corte(previo, pendiente, {clave_de_ruta(RUTA): estado})

    final, restaurar, descartar = TABLA_ESPERADA[(rel, disco, papelera)]
    assert _quien(recuperacion.manifiesto) == final
    assert recuperacion.restaurar == ((RUTA,) if restaurar else ())
    assert recuperacion.descartar == ((RUTA,) if descartar else ())


@pytest.mark.parametrize(("rel", "disco", "papelera"), sorted(TABLA_ESPERADA, key=lambda k: tuple(x.value for x in k)))
def test_reconciliar_dos_veces_da_lo_mismo(rel: Rel, disco: Disco, papelera: Papelera) -> None:
    """Un segundo arranque tras el primero (aunque el primero también se corte) no cambia nada.

    Se simula lo que el escritor hace con el resultado: restaurar devuelve el archivo a su ruta y descartar vacía la
    papelera; el manifiesto que quedó pasa a ser el vigente y el pendiente sigue siendo el mismo.
    """
    previo, pendiente, estado = _escenario(rel, disco, papelera)
    clave = clave_de_ruta(RUTA)
    primera = reconciliar_tras_corte(previo, pendiente, {clave: estado})

    disco_despues = _archivo(H_M) if primera.restaurar else estado.disco
    papelera_despues = None if (primera.restaurar or primera.descartar) else estado.papelera
    segunda = reconciliar_tras_corte(primera.manifiesto, pendiente, {clave: _estado(disco_despues, papelera_despues)})

    assert segunda.manifiesto == primera.manifiesto
    assert segunda.restaurar == ()
    assert segunda.descartar == ()


# ---------------------------------------------------------------------------
# Casos de borde de la decisión
# ---------------------------------------------------------------------------


def test_un_manifiesto_previo_vacio_equivale_a_ninguno() -> None:
    estado = {clave_de_ruta(RUTA): _estado(_archivo(H_P))}

    sin_previo = reconciliar_tras_corte(None, _manifiesto(_entrada_p()), estado)
    con_previo_vacio = reconciliar_tras_corte(_manifiesto(), _manifiesto(_entrada_p()), estado)

    assert sin_previo == con_previo_vacio
    assert _quien(sin_previo.manifiesto) == "p"


def test_si_los_bytes_nuevos_son_iguales_a_los_viejos_se_adopta_la_entrada_pendiente() -> None:
    """Reemplazo con la misma salida pero otra procedencia: el archivo ya tiene los bytes de la pendiente."""
    vieja = crear_entrada(RUTA, H_M, 1000, _procedencia(1.5))
    nueva = crear_entrada(RUTA, H_M, 1000, _procedencia(2.5))
    assert vieja.clave_de_cache != nueva.clave_de_cache

    recuperacion = reconciliar_tras_corte(
        _manifiesto(vieja), _manifiesto(nueva), {clave_de_ruta(RUTA): _estado(_archivo(H_M))}
    )

    assert recuperacion.manifiesto == _manifiesto(nueva)


def test_la_ortografia_de_la_ruta_es_la_de_la_entrada_y_las_observaciones_se_indexan_por_clave() -> None:
    en_mayusculas = "Textures/Architecture/Whiterun/Muro01_P.dds"
    m = crear_entrada(en_mayusculas, H_M, 1000, _procedencia(1.5))

    recuperacion = reconciliar_tras_corte(
        _manifiesto(m), _manifiesto(), {clave_de_ruta(en_mayusculas): _estado(AUSENTE, H_M)}
    )

    assert recuperacion.descartar == (en_mayusculas,)
    with pytest.raises(PlanInvalidoError, match=clave_de_ruta(en_mayusculas)):
        reconciliar_tras_corte(_manifiesto(m), _manifiesto(), {en_mayusculas: _estado(AUSENTE, H_M)})


def test_varias_rutas_a_la_vez_salen_ordenadas_por_clave_y_cada_una_con_su_decision() -> None:
    ruta_a, ruta_b, ruta_c, ruta_d = (f"textures/{n}_p.dds" for n in "abcd")
    previo = _manifiesto(
        crear_entrada(ruta_a, H_M, 10, _procedencia(1.5)),
        crear_entrada(ruta_b, H_M, 10, _procedencia(1.5)),
        crear_entrada(ruta_d, H_M, 10, _procedencia(1.5)),
    )
    pendiente = _manifiesto(
        crear_entrada(ruta_a, H_P, 20, _procedencia(2.5)),  # reemplazo: el archivo nuevo ya está publicado
        crear_entrada(ruta_c, H_P, 20, _procedencia(2.5)),  # creación que no llegó a publicarse
        crear_entrada(ruta_d, H_P, 20, _procedencia(2.5)),  # reemplazo cortado con el original en la papelera
    )
    observado = {
        clave_de_ruta(ruta_a): _estado(_archivo(H_P), H_M),
        clave_de_ruta(ruta_b): _estado(AUSENTE, H_M),  # retiro cortado con el archivo en la papelera
        clave_de_ruta(ruta_c): _estado(AUSENTE),
        clave_de_ruta(ruta_d): _estado(AUSENTE, H_M),
    }

    recuperacion = reconciliar_tras_corte(previo, pendiente, observado)

    assert [e.ruta for e in recuperacion.manifiesto.entradas] == [ruta_a, ruta_d]
    assert [e.sha256 for e in recuperacion.manifiesto.entradas] == [H_P, H_M]
    assert recuperacion.restaurar == (ruta_d,)
    assert recuperacion.descartar == (ruta_a, ruta_b)


def test_las_observaciones_de_rutas_ajenas_se_ignoran() -> None:
    observado = {
        clave_de_ruta(RUTA): _estado(_archivo(H_P)),
        clave_de_ruta("textures/otra_p.dds"): _estado(_archivo(H_X), H_X),
    }

    recuperacion = reconciliar_tras_corte(None, _manifiesto(_entrada_p()), observado)

    assert _quien(recuperacion.manifiesto) == "p"
    assert recuperacion.descartar == ()


def test_sin_pendiente_ni_previo_no_hay_nada_que_hacer() -> None:
    recuperacion = reconciliar_tras_corte(None, _manifiesto(), {})

    assert recuperacion == RecuperacionDeCorte(manifiesto=_manifiesto(), restaurar=(), descartar=())


# ---------------------------------------------------------------------------
# Validación de los argumentos
# ---------------------------------------------------------------------------


def test_falta_la_observacion_de_una_ruta_relevante() -> None:
    with pytest.raises(PlanInvalidoError, match="falta"):
        reconciliar_tras_corte(None, _manifiesto(_entrada_p()), {})
    with pytest.raises(PlanInvalidoError, match="falta"):
        reconciliar_tras_corte(_manifiesto(_entrada_m()), _manifiesto(), {})


@pytest.mark.parametrize(
    ("previo", "pendiente", "observado"),
    [
        ("manifiesto", _manifiesto(), {}),
        (None, "manifiesto", {}),
        (None, None, {}),
        (None, _manifiesto(), []),
        (None, _manifiesto(), {"textures/a_p.dds": "estado"}),
        (None, _manifiesto(), {"textures/a_p.dds": Observacion(TipoObservado.AUSENTE, None)}),
    ],
)
def test_los_argumentos_con_tipos_equivocados_se_rechazan(previo: Any, pendiente: Any, observado: Any) -> None:
    with pytest.raises(PlanInvalidoError):
        reconciliar_tras_corte(previo, pendiente, observado)


def test_un_estado_exige_una_observacion_y_una_huella_valida_en_la_papelera() -> None:
    with pytest.raises(PlanInvalidoError):
        EstadoTrasCorte(disco="ausente", papelera=None)  # type: ignore[arg-type]
    for invalida in ("", "a" * 63, "A" * 64, "g" * 64, 5, b"a" * 64):
        with pytest.raises(PlanInvalidoError):
            EstadoTrasCorte(disco=AUSENTE, papelera=invalida)  # type: ignore[arg-type]
    assert EstadoTrasCorte(disco=AUSENTE, papelera=None).papelera is None
    assert EstadoTrasCorte(disco=AUSENTE, papelera=H_M).papelera == H_M


def test_la_recuperacion_valida_sus_campos() -> None:
    ruta_a, ruta_b = "textures/a_p.dds", "textures/b_p.dds"
    base = _manifiesto(crear_entrada(ruta_a, H_M, 10, _procedencia()))

    with pytest.raises(PlanInvalidoError):
        RecuperacionDeCorte(manifiesto="m", restaurar=(), descartar=())  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError):
        RecuperacionDeCorte(manifiesto=base, restaurar=[ruta_a], descartar=())  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError):
        RecuperacionDeCorte(manifiesto=base, restaurar=(), descartar=[ruta_a])  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError):  # restaurar solo tiene sentido para una ruta que sigue en el manifiesto
        RecuperacionDeCorte(manifiesto=base, restaurar=(ruta_b,), descartar=())
    with pytest.raises(PlanInvalidoError):  # no se restaura y se descarta la misma ruta
        RecuperacionDeCorte(manifiesto=base, restaurar=(ruta_a,), descartar=(ruta_a,))
    with pytest.raises(PlanInvalidoError):  # sin orden canónico
        RecuperacionDeCorte(manifiesto=_manifiesto(), restaurar=(), descartar=("textures/b_p.dds", "textures/a_p.dds"))
    with pytest.raises(RutaDeSalidaInvalidaError):  # ruta inválida
        RecuperacionDeCorte(manifiesto=_manifiesto(), restaurar=(), descartar=("../a_p.dds",))


# ---------------------------------------------------------------------------
# El módulo sigue siendo puro
# ---------------------------------------------------------------------------


def test_los_nombres_nuevos_estan_en_all() -> None:
    for nombre in ("EstadoTrasCorte", "RecuperacionDeCorte", "reconciliar_tras_corte"):
        assert nombre in output.__all__

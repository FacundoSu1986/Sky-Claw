"""NP-O0b1: reconciliación PURA de un corte a mitad de la escritura de la salida gestionada.

El escritor (NP-O0b2) guarda su intención en un manifiesto «pendiente» ANTES de tocar el disco y escribe el manifiesto
definitivo AL FINAL (el punto de compromiso). Si el proceso muere en el medio, el próximo arranque tiene tres cosas: el
manifiesto vigente, el pendiente y lo que hay en disco (más una «papelera» donde el escritor aparta un archivo antes de
verificarlo). `reconciliar_tras_corte` decide, sin tocar nada, qué manifiesto queda en pie y qué hacer con la papelera.

La tabla se ENUMERA entera (4 relaciones × 5 estados de disco × 3 de papelera = 60 estados) y se compara por igualdad
literal contra una tabla escrita a mano: agregar una dimensión sin ampliarla rompe el test.

`cota_del_manifiesto_tras_un_corte` acota el manifiesto que puede quedar en pie: se verifica contra TODAS las elecciones
posibles por ruta (la entrada vigente, la pendiente o ninguna), no contra un caso elegido.
"""

from __future__ import annotations

import dataclasses
import itertools
from enum import Enum
from typing import Any

import pytest

from sky_claw.local.native_parallax import output
from sky_claw.local.native_parallax.output import (
    DescarteDePapelera,
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
    cota_del_manifiesto_tras_un_corte,
    crear_entrada,
    parametros_canonicos,
    reconciliar_tras_corte,
    serializar_manifiesto,
)

H_M = "a" * 64  # los bytes de la entrada vigente (la del manifiesto)
H_P = "b" * 64  # los bytes de la entrada pendiente
H_X = "c" * 64  # bytes de un tercero
RUTA = "textures/architecture/whiterun/muro01_p.dds"


def _procedencia(escala: float = 1.5, relleno: int = 0) -> Procedencia:
    """``relleno`` agrega un parámetro de ese largo: sirve para que dos entradas pesen distinto al serializarse."""
    parametros: dict[str, Any] = {"borde": "periodico", "escala": escala}
    if relleno:
        parametros["relleno"] = "x" * relleno
    return Procedencia(
        algoritmo_id="normal_fft_periodic_v1",
        parametros=parametros_canonicos(parametros),
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
    NADA = "nada"  # el archivo apartado se queda donde está
    DESCARTAR = "descartar"  # un archivo nuestro apartado que ya no hace falta
    RESTAURAR = "restaurar"  # el archivo apartado vuelve a su ruta, que está vacía


# (manifiesto que queda SIN papelera de m, manifiesto que queda CON papelera de m, qué pasa con la papelera de m, qué
# pasa con una papelera ajena). «m» y «p» nombran la entrada vigente y la pendiente. Escrita a mano, fila por fila, sin
# mirar la implementación. Una papelera ajena nunca cambia el manifiesto ni se descarta; si la ruta está vacía, vuelve:
# es lo mismo que hace el escritor sin corte cuando lo apartado no era lo autorizado. En una creación (SOLO_P) no hay
# entrada vigente, así que la papelera «de m» tampoco es nuestra y se comporta como una ajena.
_FILAS: dict[tuple[Rel, Disco], tuple[str | None, str | None, Efecto, Efecto]] = {
    (Rel.SOLO_P, Disco.AUSENTE): (None, None, Efecto.RESTAURAR, Efecto.RESTAURAR),
    (Rel.SOLO_P, Disco.ARCHIVO_M): (None, None, Efecto.NADA, Efecto.NADA),
    (Rel.SOLO_P, Disco.ARCHIVO_P): ("p", "p", Efecto.NADA, Efecto.NADA),
    (Rel.SOLO_P, Disco.ARCHIVO_X): (None, None, Efecto.NADA, Efecto.NADA),
    (Rel.SOLO_P, Disco.OTRO): (None, None, Efecto.NADA, Efecto.NADA),
    (Rel.SOLO_M, Disco.AUSENTE): (None, None, Efecto.DESCARTAR, Efecto.RESTAURAR),
    (Rel.SOLO_M, Disco.ARCHIVO_M): ("m", "m", Efecto.DESCARTAR, Efecto.NADA),
    (Rel.SOLO_M, Disco.ARCHIVO_P): (None, None, Efecto.DESCARTAR, Efecto.NADA),
    (Rel.SOLO_M, Disco.ARCHIVO_X): (None, None, Efecto.DESCARTAR, Efecto.NADA),
    (Rel.SOLO_M, Disco.OTRO): (None, None, Efecto.DESCARTAR, Efecto.NADA),
    (Rel.IGUAL, Disco.AUSENTE): ("m", "m", Efecto.RESTAURAR, Efecto.RESTAURAR),
    (Rel.IGUAL, Disco.ARCHIVO_M): ("m", "m", Efecto.DESCARTAR, Efecto.NADA),
    (Rel.IGUAL, Disco.ARCHIVO_P): ("m", "m", Efecto.DESCARTAR, Efecto.NADA),
    (Rel.IGUAL, Disco.ARCHIVO_X): ("m", "m", Efecto.DESCARTAR, Efecto.NADA),
    (Rel.IGUAL, Disco.OTRO): ("m", "m", Efecto.DESCARTAR, Efecto.NADA),
    (Rel.DISTINTO, Disco.AUSENTE): (None, "m", Efecto.RESTAURAR, Efecto.RESTAURAR),
    (Rel.DISTINTO, Disco.ARCHIVO_M): ("m", "m", Efecto.DESCARTAR, Efecto.NADA),
    (Rel.DISTINTO, Disco.ARCHIVO_P): ("p", "p", Efecto.DESCARTAR, Efecto.NADA),
    (Rel.DISTINTO, Disco.ARCHIVO_X): (None, None, Efecto.DESCARTAR, Efecto.NADA),
    (Rel.DISTINTO, Disco.OTRO): (None, None, Efecto.DESCARTAR, Efecto.NADA),
}


def _tabla_esperada() -> dict[tuple[Rel, Disco, Papelera], tuple[str | None, bool, bool]]:
    """Expande las 20 filas a los 60 estados: (manifiesto que queda, ¿restaurar?, ¿descartar?)."""
    tabla: dict[tuple[Rel, Disco, Papelera], tuple[str | None, bool, bool]] = {}
    for (rel, disco), (sin_papelera, con_papelera, efecto, efecto_ajena) in _FILAS.items():
        assert efecto_ajena is not Efecto.DESCARTAR, "una papelera ajena no se descarta nunca"
        tabla[(rel, disco, Papelera.NINGUNA)] = (sin_papelera, False, False)
        tabla[(rel, disco, Papelera.AJENA)] = (sin_papelera, efecto_ajena is Efecto.RESTAURAR, False)
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
    # Lo que se descarta lleva la huella que lo autoriza (la vigente): el escritor la vuelve a comprobar antes de borrar.
    assert recuperacion.descartar == ((DescarteDePapelera(RUTA, H_M),) if descartar else ())


@pytest.mark.parametrize(("rel", "disco", "papelera"), sorted(TABLA_ESPERADA, key=lambda k: tuple(x.value for x in k)))
def test_reconciliar_dos_veces_da_lo_mismo(rel: Rel, disco: Disco, papelera: Papelera) -> None:
    """Un segundo arranque tras el primero (aunque el primero también se corte) no cambia nada.

    Se simula lo que el escritor hace con el resultado: restaurar devuelve el archivo apartado (sea nuestro o no) a su
    ruta y descartar vacía la papelera; el manifiesto que quedó pasa a ser el vigente y el pendiente sigue siendo el mismo.
    """
    previo, pendiente, estado = _escenario(rel, disco, papelera)
    clave = clave_de_ruta(RUTA)
    primera = reconciliar_tras_corte(previo, pendiente, {clave: estado})

    assert not primera.restaurar or estado.papelera is not None
    disco_despues = _archivo(estado.papelera) if primera.restaurar and estado.papelera else estado.disco
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

    assert recuperacion.descartar == (DescarteDePapelera(en_mayusculas, H_M),)
    with pytest.raises(PlanInvalidoError, match=clave_de_ruta(en_mayusculas)):
        reconciliar_tras_corte(_manifiesto(m), _manifiesto(), {en_mayusculas: _estado(AUSENTE, H_M)})


def test_restaurar_usa_la_ortografia_de_la_entrada_vigente_que_es_donde_estaba_el_archivo() -> None:
    """En un sistema que distingue mayúsculas, el archivo apartado vuelve a la ruta con la que se publicó."""
    como_se_publico = "Textures/Muro_P.dds"
    como_se_pide_ahora = "textures/muro_p.dds"
    vieja = crear_entrada(como_se_publico, H_M, 1000, _procedencia(1.5))
    nueva = crear_entrada(como_se_pide_ahora, H_P, 2000, _procedencia(2.5))

    recuperacion = reconciliar_tras_corte(
        _manifiesto(vieja), _manifiesto(nueva), {clave_de_ruta(como_se_publico): _estado(AUSENTE, H_M)}
    )

    assert recuperacion.restaurar == (como_se_publico,)
    assert recuperacion.manifiesto == _manifiesto(vieja)


@pytest.mark.parametrize("rel", [Rel.SOLO_M, Rel.DISTINTO, Rel.IGUAL])
def test_un_apartado_ajeno_vuelve_con_la_ortografia_de_la_entrada_vigente(rel: Rel) -> None:
    """Lo apartado salió de la ruta con la que se publicó la entrada vigente: vuelve ahí, aunque no sea nuestro."""
    como_se_publico = "Textures/Muro_P.dds"
    como_se_pide_ahora = "textures/muro_p.dds"
    vieja = crear_entrada(como_se_publico, H_M, 1000, _procedencia(1.5))
    pendiente = {
        Rel.SOLO_M: _manifiesto(),
        Rel.DISTINTO: _manifiesto(crear_entrada(como_se_pide_ahora, H_P, 2000, _procedencia(2.5))),
        Rel.IGUAL: _manifiesto(vieja),
    }[rel]

    recuperacion = reconciliar_tras_corte(
        _manifiesto(vieja), pendiente, {clave_de_ruta(como_se_publico): _estado(AUSENTE, H_X)}
    )

    assert recuperacion.restaurar == (como_se_publico,)
    assert recuperacion.descartar == ()


def test_un_apartado_en_una_ruta_que_solo_figura_en_el_pendiente_vuelve_con_la_ortografia_del_pendiente() -> None:
    """Sin entrada vigente no hay otra ortografía: se devuelve con la de la intención (y no se adopta)."""
    como_se_pide = "Textures/Nuevo_P.dds"
    nueva = crear_entrada(como_se_pide, H_P, 2000, _procedencia(2.5))

    recuperacion = reconciliar_tras_corte(
        None, _manifiesto(nueva), {clave_de_ruta(como_se_pide): _estado(AUSENTE, H_X)}
    )

    assert recuperacion.restaurar == (como_se_pide,)
    assert recuperacion.manifiesto == _manifiesto()


def test_un_apartado_ajeno_con_la_ruta_ocupada_se_queda_en_la_papelera_y_no_se_descarta() -> None:
    for observacion in (_archivo(H_M), _archivo(H_P), _archivo(H_X), OTRO):
        recuperacion = reconciliar_tras_corte(
            _manifiesto(_entrada_m()), _manifiesto(_entrada_p()), {clave_de_ruta(RUTA): _estado(observacion, H_X)}
        )

        assert recuperacion.restaurar == ()
        assert recuperacion.descartar == ()


def test_varias_rutas_a_la_vez_salen_ordenadas_por_clave_y_cada_una_con_su_decision() -> None:
    ruta_a, ruta_b, ruta_c, ruta_d, ruta_e = (f"textures/{n}_p.dds" for n in "abcde")
    previo = _manifiesto(
        crear_entrada(ruta_a, H_M, 10, _procedencia(1.5)),
        crear_entrada(ruta_b, H_M, 10, _procedencia(1.5)),
        crear_entrada(ruta_d, H_M, 10, _procedencia(1.5)),
        crear_entrada(ruta_e, H_M, 10, _procedencia(1.5)),
    )
    pendiente = _manifiesto(
        crear_entrada(ruta_a, H_P, 20, _procedencia(2.5)),  # reemplazo: el archivo nuevo ya está publicado
        crear_entrada(ruta_c, H_P, 20, _procedencia(2.5)),  # creación que no llegó a publicarse
        crear_entrada(ruta_d, H_P, 20, _procedencia(2.5)),  # reemplazo cortado con el original en la papelera
        crear_entrada(ruta_e, H_P, 20, _procedencia(2.5)),  # reemplazo cortado: lo apartado lo había editado otro
    )
    observado = {
        clave_de_ruta(ruta_a): _estado(_archivo(H_P), H_M),
        clave_de_ruta(ruta_b): _estado(AUSENTE, H_M),  # retiro cortado con el archivo en la papelera
        clave_de_ruta(ruta_c): _estado(AUSENTE),
        clave_de_ruta(ruta_d): _estado(AUSENTE, H_M),
        clave_de_ruta(ruta_e): _estado(AUSENTE, H_X),
    }

    recuperacion = reconciliar_tras_corte(previo, pendiente, observado)

    assert [e.ruta for e in recuperacion.manifiesto.entradas] == [ruta_a, ruta_d]
    assert [e.sha256 for e in recuperacion.manifiesto.entradas] == [H_P, H_M]
    assert recuperacion.restaurar == (ruta_d, ruta_e)
    assert recuperacion.descartar == (DescarteDePapelera(ruta_a, H_M), DescarteDePapelera(ruta_b, H_M))


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
# La cota del manifiesto que puede quedar en pie
# ---------------------------------------------------------------------------


def _tamano(entradas: Any) -> int:
    return len(serializar_manifiesto(Manifiesto.ordenado(entradas)))


def test_la_cota_nunca_se_queda_corta_ante_ninguna_eleccion_posible() -> None:
    """Tras un corte (o al comprometer un lote con fallos) queda, por ruta, la entrada vigente, la pendiente o ninguna.

    Se enumeran TODAS esas elecciones sobre rutas que cubren cada relación (solo en uno, la misma en ambos, distintas con
    la vigente más pesada y con la pendiente más pesada): ninguna serializa más que la cota, y la cota no sobra más que
    unos pocos bytes por ruta sobre la más pesada.
    """
    r1, r2, r3, r4, r5 = (f"textures/r{i}_p.dds" for i in range(1, 6))
    previo = _manifiesto(
        crear_entrada(r1, H_M, 10, _procedencia(1.5)),
        crear_entrada(r3, H_M, 10, _procedencia(1.5, relleno=40)),
        crear_entrada(r4, H_M, 10, _procedencia(1.5, relleno=300)),
        crear_entrada(r5, H_M, 10, _procedencia(1.5)),
    )
    pendiente = _manifiesto(
        crear_entrada(r2, H_P, 20, _procedencia(2.5, relleno=120)),
        crear_entrada(r3, H_M, 10, _procedencia(1.5, relleno=40)),
        crear_entrada(r4, H_P, 20, _procedencia(2.5)),
        crear_entrada(r5, H_P, 20, _procedencia(2.5, relleno=500)),
    )
    por_ruta: dict[str, list[EntradaDeManifiesto | None]] = {}
    for entrada in (*previo.entradas, *pendiente.entradas):
        opciones = por_ruta.setdefault(clave_de_ruta(entrada.ruta), [None])
        if entrada not in opciones:
            opciones.append(entrada)
    assert sorted(len(o) for o in por_ruta.values()) == [2, 2, 2, 3, 3]

    cota = cota_del_manifiesto_tras_un_corte(previo, pendiente)

    tamanos = [_tamano(e for e in eleccion if e is not None) for eleccion in itertools.product(*por_ruta.values())]
    assert len(tamanos) == 72
    assert max(tamanos) <= cota
    assert cota - max(tamanos) <= 2 * len(por_ruta)


def test_la_cota_de_un_lote_que_no_cambia_nada_es_el_manifiesto_vacio() -> None:
    vacio = len(serializar_manifiesto(_manifiesto()))

    assert cota_del_manifiesto_tras_un_corte(None, _manifiesto()) == vacio
    assert cota_del_manifiesto_tras_un_corte(_manifiesto(), _manifiesto()) == vacio


def test_la_entrada_que_figura_igual_en_ambos_cuenta_una_sola_vez() -> None:
    m = _manifiesto(_entrada_m())

    assert cota_del_manifiesto_tras_un_corte(m, m) == cota_del_manifiesto_tras_un_corte(None, m)
    assert cota_del_manifiesto_tras_un_corte(m, _manifiesto()) == cota_del_manifiesto_tras_un_corte(None, m)
    assert cota_del_manifiesto_tras_un_corte(None, m) == _tamano(m.entradas)


def test_dos_ortografias_de_la_misma_ruta_son_una_sola_ruta_en_la_cota() -> None:
    vieja = crear_entrada("Textures/Muro_P.dds", H_M, 10, _procedencia(1.5, relleno=200))
    nueva = crear_entrada("textures/muro_p.dds", H_P, 20, _procedencia(2.5))

    cota = cota_del_manifiesto_tras_un_corte(_manifiesto(vieja), _manifiesto(nueva))

    assert cota == max(_tamano([vieja]), _tamano([nueva]))


def test_rutas_disjuntas_suman_los_dos_manifiestos() -> None:
    """El caso que la cota existe para atajar: un lote que cambia casi todas las rutas puede dejar la UNIÓN tras un corte.

    Cada manifiesto cabe por separado; lo que puede quedar en pie es más grande que cualquiera de los dos.
    """
    previo = _manifiesto(*(crear_entrada(f"textures/viejo{i}_p.dds", H_M, 10, _procedencia(1.5)) for i in range(4)))
    pendiente = _manifiesto(*(crear_entrada(f"textures/nuevo{i}_p.dds", H_P, 20, _procedencia(2.5)) for i in range(4)))
    vacio = len(serializar_manifiesto(_manifiesto()))

    cota = cota_del_manifiesto_tras_un_corte(previo, pendiente)

    union = _tamano((*previo.entradas, *pendiente.entradas))
    assert cota >= union > max(_tamano(previo.entradas), _tamano(pendiente.entradas))
    assert cota >= _tamano(previo.entradas) + _tamano(pendiente.entradas) - vacio


@pytest.mark.parametrize(
    ("previo", "pendiente"),
    [("manifiesto", _manifiesto()), (None, "manifiesto"), (None, None), (_manifiesto(), (_entrada_m(),))],
)
def test_la_cota_rechaza_argumentos_que_no_son_manifiestos(previo: Any, pendiente: Any) -> None:
    with pytest.raises(PlanInvalidoError):
        cota_del_manifiesto_tras_un_corte(previo, pendiente)


# ---------------------------------------------------------------------------
# Validación de los argumentos
# ---------------------------------------------------------------------------


def test_falta_la_observacion_de_una_ruta_relevante() -> None:
    with pytest.raises(PlanInvalidoError, match="falta"):
        reconciliar_tras_corte(None, _manifiesto(_entrada_p()), {})
    with pytest.raises(PlanInvalidoError, match="falta"):
        reconciliar_tras_corte(_manifiesto(_entrada_m()), _manifiesto(), {})


@pytest.mark.parametrize(
    ("previo", "pendiente", "observado", "mensaje"),
    [
        ("manifiesto", _manifiesto(), {}, "el manifiesto previo"),
        (None, "manifiesto", {}, "el manifiesto pendiente"),
        (None, None, {}, "el manifiesto pendiente"),
        (None, _manifiesto(), [], "las observaciones deben ser un mapa"),
        (None, _manifiesto(), {"textures/a_p.dds": "estado"}, "cada observación debe ser un EstadoTrasCorte"),
        (
            None,
            _manifiesto(),
            {"textures/a_p.dds": Observacion(TipoObservado.AUSENTE, None)},
            "cada observación debe ser un EstadoTrasCorte",
        ),
    ],
)
def test_los_argumentos_con_tipos_equivocados_se_rechazan(
    previo: Any, pendiente: Any, observado: Any, mensaje: str
) -> None:
    with pytest.raises(PlanInvalidoError, match=mensaje):
        reconciliar_tras_corte(previo, pendiente, observado)


def test_un_estado_exige_una_observacion_y_una_huella_valida_en_la_papelera() -> None:
    with pytest.raises(PlanInvalidoError, match="lo observado en disco"):
        EstadoTrasCorte(disco="ausente", papelera=None)  # type: ignore[arg-type]
    for invalida in ("", "a" * 63, "A" * 64, "g" * 64, 5, b"a" * 64):
        with pytest.raises(PlanInvalidoError, match="la huella de la papelera"):
            EstadoTrasCorte(disco=AUSENTE, papelera=invalida)  # type: ignore[arg-type]
    assert EstadoTrasCorte(disco=AUSENTE, papelera=None).papelera is None
    assert EstadoTrasCorte(disco=AUSENTE, papelera=H_M).papelera == H_M


def test_un_descarte_exige_una_ruta_valida_y_la_huella_que_lo_autoriza() -> None:
    for invalida in ("", "a" * 63, "A" * 64, "g" * 64, None, 5, b"a" * 64):
        with pytest.raises(PlanInvalidoError, match="la huella que autoriza el descarte"):
            DescarteDePapelera(RUTA, invalida)  # type: ignore[arg-type]
    with pytest.raises(RutaDeSalidaInvalidaError):
        DescarteDePapelera("../a_p.dds", H_M)
    assert DescarteDePapelera(RUTA, H_M).sha256_autorizado == H_M


def test_la_recuperacion_valida_sus_campos() -> None:
    ruta_a, ruta_b = "textures/a_p.dds", "textures/b_p.dds"
    base = _manifiesto(crear_entrada(ruta_a, H_M, 10, _procedencia()))
    descarte_a, descarte_b = DescarteDePapelera(ruta_a, H_M), DescarteDePapelera(ruta_b, H_M)

    with pytest.raises(PlanInvalidoError, match="el manifiesto de la recuperación"):
        RecuperacionDeCorte(manifiesto="m", restaurar=(), descartar=())  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError, match="restaurar debe ser una tupla de rutas"):
        RecuperacionDeCorte(manifiesto=base, restaurar=[ruta_a], descartar=())  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError, match="restaurar debe ser una tupla de rutas"):
        RecuperacionDeCorte(manifiesto=base, restaurar=(5,), descartar=())  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError, match="descartar debe ser una tupla de DescarteDePapelera"):
        RecuperacionDeCorte(manifiesto=base, restaurar=(), descartar=[descarte_a])  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError, match="descartar debe ser una tupla de DescarteDePapelera"):  # sin su huella
        RecuperacionDeCorte(manifiesto=base, restaurar=(), descartar=(ruta_a,))  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError, match="no se restaura y se descarta a la vez"):
        RecuperacionDeCorte(manifiesto=base, restaurar=(ruta_a,), descartar=(descarte_a,))
    with pytest.raises(PlanInvalidoError, match="descartar debe estar ordenado"):
        RecuperacionDeCorte(manifiesto=_manifiesto(), restaurar=(), descartar=(descarte_b, descarte_a))
    with pytest.raises(PlanInvalidoError, match="restaurar debe estar ordenado"):
        RecuperacionDeCorte(manifiesto=_manifiesto(), restaurar=(ruta_b, ruta_a), descartar=())
    with pytest.raises(PlanInvalidoError, match="sin rutas repetidas"):  # repetida (ignorando mayúsculas)
        RecuperacionDeCorte(manifiesto=_manifiesto(), restaurar=(ruta_a, ruta_a.upper()), descartar=())
    with pytest.raises(RutaDeSalidaInvalidaError):  # ruta inválida
        RecuperacionDeCorte(manifiesto=_manifiesto(), restaurar=("../a_p.dds",), descartar=())
    # Se puede devolver una ruta que NO queda en el manifiesto: lo apartado era de un tercero y vuelve a su lugar.
    ajena = RecuperacionDeCorte(manifiesto=base, restaurar=(ruta_b,), descartar=())
    assert ajena.restaurar == (ruta_b,)


@pytest.mark.parametrize(
    "objeto",
    [
        EstadoTrasCorte(disco=AUSENTE, papelera=None),
        DescarteDePapelera(RUTA, H_M),
        RecuperacionDeCorte(manifiesto=_manifiesto(), restaurar=(), descartar=()),
    ],
    ids=["EstadoTrasCorte", "DescarteDePapelera", "RecuperacionDeCorte"],
)
def test_los_resultados_son_inmutables_y_sin_diccionario(objeto: Any) -> None:
    """Son valores que viajan entre el planificador y el escritor: nadie los muta por el camino."""
    campo = dataclasses.fields(objeto)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(objeto, campo, None)
    assert not hasattr(objeto, "__dict__")


# ---------------------------------------------------------------------------
# El módulo sigue siendo puro
# ---------------------------------------------------------------------------


def test_los_nombres_nuevos_estan_en_all() -> None:
    for nombre in (
        "DescarteDePapelera",
        "EstadoTrasCorte",
        "RecuperacionDeCorte",
        "cota_del_manifiesto_tras_un_corte",
        "reconciliar_tras_corte",
    ):
        assert nombre in output.__all__

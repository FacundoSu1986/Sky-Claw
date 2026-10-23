"""NP-O0a (ADR 0014): salida gestionada del generador nativo de parallax — manifiesto, clave de caché y planificador.

El módulo es PURO: no toca el disco. Estos tests fijan tres cosas que el escritor (NP-O0b) va a dar por sentadas:

1. **El manifiesto es la única prueba de propiedad.** Un archivo que no figura en él NO es nuestro y no se pisa
   (``PRESERVE_EXISTING``); uno que figura pero cambió de contenido tampoco: lo tocó un tercero.
2. **La clave de caché cubre todo lo que determina los bytes** (algoritmo, parámetros, orígenes, encoder, entorno y
   nivel de reproducibilidad). Se enumeran los campos por introspección: un campo nuevo que no entre a la clave rompe
   el test, en vez de dejar una caché que sirve resultados viejos.
3. **El planificador se enumera, no se muestrea**: la tabla estado → acción se compara por igualdad literal contra
   TODAS las combinaciones posibles y la escribí a mano, sin reusar la lógica del módulo.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import itertools
import json
import random
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from sky_claw.local.native_parallax import output
from sky_claw.local.native_parallax.output import (
    NOMBRE_DEL_MOD_DE_SALIDA,
    AccionDeSalida,
    AccionPlanificada,
    EncoderUsado,
    EntradaDeManifiesto,
    Manifiesto,
    ManifiestoInvalidoError,
    MotivoDeSalida,
    NivelDeReproducibilidad,
    Observacion,
    OrigenDeEntrada,
    PlanDeSalida,
    PlanInvalidoError,
    Procedencia,
    ProcedenciaInvalidaError,
    RutaDeSalidaInvalidaError,
    SalidaDeseada,
    TipoObservado,
    clave_de_cache,
    clave_de_ruta,
    crear_entrada,
    huella_del_manifiesto,
    leer_manifiesto,
    parametros_canonicos,
    planificar,
    serializar_manifiesto,
    validar_ruta_de_salida,
)

H_A = "a" * 64
H_B = "b" * 64
H_C = "c" * 64
H_D = "d" * 64
RUTA = "textures/architecture/whiterun/muro01_p.dds"


# ---------------------------------------------------------------------------
# Fábricas
# ---------------------------------------------------------------------------


def _encoder(**cambios: Any) -> EncoderUsado:
    base: dict[str, Any] = {
        "exportador_id": "np_e0b_bc4_v1",
        "texconv_version": "may2026",
        "texconv_sha256": H_C,
        "argumentos": ("-f", "BC4_UNORM", "-m", "0", "-y", "-o"),
    }
    base.update(cambios)
    return EncoderUsado(**base)


def _procedencia(**cambios: Any) -> Procedencia:
    base: dict[str, Any] = {
        "algoritmo_id": "normal_fft_periodic_v1",
        "parametros": parametros_canonicos({"borde": "periodico", "escala": 1.5}),
        "origenes": (OrigenDeEntrada(rol="normal", sha256=H_A),),
        "encoder": _encoder(),
        "entorno": (("numpy", "2.4.6"), ("plataforma", "win32")),
        "nivel": NivelDeReproducibilidad.DETERMINISTA_MISMO_ENTORNO,
    }
    base.update(cambios)
    return Procedencia(**base)


def _entrada(ruta: str = RUTA, sha256: str = H_B, tamano: int = 349_680, **proc: Any) -> EntradaDeManifiesto:
    return crear_entrada(ruta, sha256, tamano, _procedencia(**proc))


def _manifiesto(*entradas: EntradaDeManifiesto) -> Manifiesto:
    return Manifiesto.ordenado(entradas)


# ---------------------------------------------------------------------------
# Rutas de salida
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ruta",
    [
        "textures/a_p.dds",
        "textures/architecture/whiterun/muro01_p.dds",
        "Textures/Architecture/Muro_P.DDS",  # Skyrim no distingue mayúsculas
        "textures/a-b c.d/e_f_p.dds",
        "textures/" + "x" * 150 + "_p.dds",
    ],
)
def test_una_ruta_de_salida_valida_se_devuelve_tal_cual(ruta: str) -> None:
    assert validar_ruta_de_salida(ruta) == ruta


@pytest.mark.parametrize(
    "ruta",
    [
        "",
        "/textures/a_p.dds",  # absoluta
        "C:/textures/a_p.dds",  # unidad
        "textures/a:b_p.dds",  # ':' abre un flujo alterno en NTFS
        "textures\\a_p.dds",  # barra invertida
        "textures/../a_p.dds",
        "textures/./a_p.dds",
        "textures//a_p.dds",  # segmento vacío
        "textures/a_p.dds/",  # termina en barra
        "textures/con/a_p.dds",  # nombre reservado de Windows
        "textures/NUL/a_p.dds",
        "textures/com1/a_p.dds",
        "textures/lpt9.txt/a_p.dds",  # el nombre reservado vale aunque tenga extensión
        "textures/aux.x/a_p.dds",
        "textures/dir./a_p.dds",  # Windows recorta el punto final
        "textures/dir /a_p.dds",  # y el espacio final
        "textures/a<b_p.dds",
        "textures/a>b_p.dds",
        'textures/a"b_p.dds',
        "textures/a|b_p.dds",
        "textures/a?b_p.dds",
        "textures/a*b_p.dds",
        "textures/a\x00b_p.dds",
        "textures/a\x1fb_p.dds",
        "textures/a\x7fb_p.dds",
        "textures/ñandú_p.dds",  # no ASCII: la normalización cambia entre sistemas de archivos
        "meshes/a_p.dds",  # fuera de textures/
        "textures/a_n.dds",  # no es un mapa de altura
        "textures/a_p.dds.bak",
        "textures/_p.dds",  # sin nombre antes del sufijo
        "textures/a_p.png",
        "textures",
        "textures/",
        "textures/" + "x" * 200 + "_p.dds",  # demasiado larga para MAX_PATH con la raíz del mod
    ],
)
def test_una_ruta_de_salida_invalida_se_rechaza(ruta: str) -> None:
    with pytest.raises(RutaDeSalidaInvalidaError):
        validar_ruta_de_salida(ruta)


@pytest.mark.parametrize("ruta", [None, 3, b"textures/a_p.dds", ["textures/a_p.dds"]])
def test_una_ruta_que_no_es_texto_se_rechaza(ruta: object) -> None:
    with pytest.raises(RutaDeSalidaInvalidaError):
        validar_ruta_de_salida(ruta)  # type: ignore[arg-type]


def test_el_error_de_ruta_es_un_error_de_valor_y_de_la_familia() -> None:
    assert issubclass(RutaDeSalidaInvalidaError, ValueError)
    assert issubclass(RutaDeSalidaInvalidaError, output.SalidaGestionadaError)


def test_la_clave_de_ruta_ignora_mayusculas() -> None:
    assert clave_de_ruta("Textures/A_P.dds") == clave_de_ruta("textures/a_p.DDS")
    assert clave_de_ruta("textures/a_p.dds") != clave_de_ruta("textures/b_p.dds")


# ---------------------------------------------------------------------------
# Parámetros canónicos
# ---------------------------------------------------------------------------


def test_los_parametros_canonicos_son_json_compacto_con_claves_ordenadas() -> None:
    texto = parametros_canonicos({"b": 1, "a": [1.5, "x", None, True, {"z": 0, "y": False}]})

    assert texto == '{"a":[1.5,"x",null,true,{"y":false,"z":0}],"b":1}'


def test_los_parametros_canonicos_no_dependen_del_orden_de_insercion() -> None:
    assert parametros_canonicos({"a": 1, "b": 2}) == parametros_canonicos({"b": 2, "a": 1})


def test_los_parametros_distinguen_booleano_entero_y_flotante() -> None:
    assert len({parametros_canonicos({"v": v}) for v in (True, 1, 1.0)}) == 3


@pytest.mark.parametrize(
    "parametros",
    [
        {"v": float("nan")},
        {"v": float("inf")},
        {"v": float("-inf")},
        {1: "x"},  # claves que no son texto
        {"v": {1, 2}},
        {"v": b"bytes"},
        {"v": object()},
        {"v": (1, 2)},  # la tupla no es JSON: que el llamador decida si es lista
        {"v": 2**63},  # fuera de int64
        {"v": -(2**63) - 1},
        {"v": "ñ"},  # solo ASCII: dos escrituras del mismo texto no pueden dar claves distintas
        {"v": "x" * 70_000},
        {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}},  # demasiado profundo
    ],
)
def test_los_parametros_no_serializables_se_rechazan(parametros: dict[Any, Any]) -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        parametros_canonicos(parametros)


def test_los_parametros_deben_ser_un_objeto() -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        parametros_canonicos([1, 2])  # type: ignore[arg-type]


def test_un_mapping_que_no_es_dict_se_rechaza_y_la_firma_lo_dice() -> None:
    # La firma pide ``dict``: un ``Mapping`` cualquiera pasaría mypy y fallaría recién en ejecución. El ``type: ignore``
    # es la prueba de que mypy lo ve (con ``warn_unused_ignores`` sobraría si la firma volviera a decir ``Mapping``).
    with pytest.raises(ProcedenciaInvalidaError, match="mappingproxy"):
        parametros_canonicos(MappingProxyType({"a": 1}))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Procedencia
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cambios",
    [
        {"algoritmo_id": ""},
        {"algoritmo_id": "Normal"},  # mayúscula
        {"algoritmo_id": "1abc"},  # empieza con dígito
        {"algoritmo_id": "a b"},
        {"algoritmo_id": "a" * 65},
        {"algoritmo_id": 3},
        {"parametros": '{"b":1, "a":2}'},  # no canónico (espacio y orden)
        {"parametros": '{"a":1,"a":2}'},  # clave duplicada
        {"parametros": "no json"},
        {"parametros": "[1]"},  # no es un objeto
        {"parametros": {"a": 1}},  # tiene que venir ya canónico, como texto
        {"origenes": ()},  # sin orígenes la clave no depende de ninguna entrada
        {"origenes": [OrigenDeEntrada(rol="normal", sha256=H_A)]},  # lista en vez de tupla
        {"origenes": (OrigenDeEntrada(rol="normal", sha256=H_A), OrigenDeEntrada(rol="normal", sha256=H_B))},
        {"origenes": (OrigenDeEntrada(rol="zeta", sha256=H_A), OrigenDeEntrada(rol="alfa", sha256=H_B))},  # desordenado
        {"origenes": ("normal",)},
        {"entorno": ()},  # sin entorno la clave se vuelve portable por error
        {"entorno": (("numpy", "2.4.6"), ("numpy", "2.4.7"))},
        {"entorno": (("python", "3.11"), ("numpy", "2.4.6"))},  # desordenado
        {"entorno": (("numpy",),)},
        {"entorno": (("Numpy", "2.4.6"),)},
        {"entorno": (("numpy", ""),)},
        {"entorno": {"numpy": "2.4.6"}},
        {"nivel": "determinista_mismo_entorno"},  # tiene que ser el enum
        {"encoder": "texconv"},
    ],
)
def test_una_procedencia_invalida_se_rechaza(cambios: dict[str, Any]) -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        _procedencia(**cambios)


@pytest.mark.parametrize(
    "cambios",
    [
        {"exportador_id": ""},
        {"exportador_id": "Np"},
        {"texconv_version": ""},
        {"texconv_version": "may 2026"},
        {"texconv_version": "x" * 33},
        {"texconv_sha256": "C" * 64},  # mayúsculas
        {"texconv_sha256": "c" * 63},
        {"texconv_sha256": "g" * 64},
        {"argumentos": ["-f"]},  # lista en vez de tupla
        {"argumentos": ("-f", "")},
        {"argumentos": ("-f", "a\x00b")},
        {"argumentos": ("-f", 3)},
        {"argumentos": tuple(["-x"] * 65)},
        {"argumentos": ("a" * 257,)},
    ],
)
def test_un_encoder_invalido_se_rechaza(cambios: dict[str, Any]) -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        _encoder(**cambios)


@pytest.mark.parametrize("sha256", ["", "A" * 64, "a" * 63, "a" * 65, "z" * 64, 3, None])
def test_un_origen_con_huella_invalida_se_rechaza(sha256: object) -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        OrigenDeEntrada(rol="normal", sha256=sha256)  # type: ignore[arg-type]


@pytest.mark.parametrize("rol", ["", "Normal", "1n", "n n", "x" * 33, 3])
def test_un_origen_con_rol_invalido_se_rechaza(rol: object) -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        OrigenDeEntrada(rol=rol, sha256=H_A)  # type: ignore[arg-type]


def test_los_errores_de_validacion_son_errores_de_valor() -> None:
    assert issubclass(ProcedenciaInvalidaError, ValueError)
    assert issubclass(ManifiestoInvalidoError, ValueError)
    assert issubclass(PlanInvalidoError, ValueError)


# ---------------------------------------------------------------------------
# Clave de caché
# ---------------------------------------------------------------------------


def test_la_clave_de_cache_es_una_huella_sha256_en_hex() -> None:
    clave = clave_de_cache(_procedencia())

    assert len(clave) == 64
    assert clave == clave.lower()
    int(clave, 16)


def test_la_misma_procedencia_da_la_misma_clave() -> None:
    assert clave_de_cache(_procedencia()) == clave_de_cache(_procedencia())


def test_la_clave_no_depende_del_orden_de_las_claves_de_los_parametros() -> None:
    uno = _procedencia(parametros=parametros_canonicos({"a": 1, "b": 2}))
    otro = _procedencia(parametros=parametros_canonicos({"b": 2, "a": 1}))

    assert clave_de_cache(uno) == clave_de_cache(otro)


def _variantes_por_campo() -> list[tuple[str, Procedencia]]:
    """UNA variante por cada campo de ``Procedencia`` y de las estructuras que contiene."""
    base = _procedencia()
    variantes: list[tuple[str, Procedencia]] = [
        ("algoritmo_id", _procedencia(algoritmo_id="normal_fft_periodic_v2")),
        ("parametros", _procedencia(parametros=parametros_canonicos({"borde": "neumann", "escala": 1.5}))),
        ("parametros.valor", _procedencia(parametros=parametros_canonicos({"borde": "periodico", "escala": 1.6}))),
        ("origenes.sha256", _procedencia(origenes=(OrigenDeEntrada(rol="normal", sha256=H_B),))),
        ("origenes.rol", _procedencia(origenes=(OrigenDeEntrada(rol="altura", sha256=H_A),))),
        (
            "origenes.extra",
            _procedencia(
                origenes=(OrigenDeEntrada(rol="altura", sha256=H_D), OrigenDeEntrada(rol="normal", sha256=H_A))
            ),
        ),
        ("encoder.exportador_id", _procedencia(encoder=_encoder(exportador_id="np_e0b_bc4_v2"))),
        ("encoder.texconv_version", _procedencia(encoder=_encoder(texconv_version="jun2026"))),
        ("encoder.texconv_sha256", _procedencia(encoder=_encoder(texconv_sha256=H_D))),
        ("encoder.argumentos", _procedencia(encoder=_encoder(argumentos=("-f", "BC4_UNORM", "-m", "1", "-y", "-o")))),
        (
            "encoder.argumentos.orden",
            _procedencia(encoder=_encoder(argumentos=("BC4_UNORM", "-f", "-m", "0", "-y", "-o"))),
        ),
        ("entorno.valor", _procedencia(entorno=(("numpy", "2.4.7"), ("plataforma", "win32")))),
        ("entorno.clave", _procedencia(entorno=(("numpy", "2.4.6"), ("sistema", "win32")))),
        ("entorno.extra", _procedencia(entorno=(("numpy", "2.4.6"), ("pillow", "13.0.0"), ("plataforma", "win32")))),
    ]
    assert base not in [v for _, v in variantes]
    return variantes


@pytest.mark.parametrize(("campo", "variante"), _variantes_por_campo(), ids=lambda v: v if isinstance(v, str) else "")
def test_cada_cambio_de_procedencia_cambia_la_clave(campo: str, variante: Procedencia) -> None:
    assert clave_de_cache(variante) != clave_de_cache(_procedencia()), campo


def test_las_variantes_cubren_todos_los_campos_de_la_procedencia_y_del_encoder() -> None:
    # Si alguien agrega un campo a ``Procedencia`` o a ``EncoderUsado`` sin variante, este test rompe: un campo que
    # no entra a la clave deja una caché que sirve resultados viejos.
    campos = {f.name for f in dataclasses.fields(Procedencia)} - {"nivel"}
    cubiertos = {nombre.split(".")[0] for nombre, _ in _variantes_por_campo()}
    assert campos == cubiertos

    campos_encoder = {f.name for f in dataclasses.fields(EncoderUsado)}
    cubiertos_encoder = {nombre.split(".")[1] for nombre, _ in _variantes_por_campo() if nombre.startswith("encoder.")}
    assert campos_encoder == cubiertos_encoder

    campos_origen = {f.name for f in dataclasses.fields(OrigenDeEntrada)}
    cubiertos_origen = {nombre.split(".")[1] for nombre, _ in _variantes_por_campo() if nombre.startswith("origenes.")}
    assert campos_origen <= cubiertos_origen


def test_el_nivel_de_reproducibilidad_entra_a_la_clave(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hoy el enum tiene un solo miembro: se amplía de mentira para probar que el campo se usa en la clave.
    class Ampliado(Enum):
        DETERMINISTA_MISMO_ENTORNO = "determinista_mismo_entorno"
        OTRO = "otro_nivel"

    monkeypatch.setattr(output, "NivelDeReproducibilidad", Ampliado)

    uno = _procedencia(nivel=Ampliado.DETERMINISTA_MISMO_ENTORNO)
    otro = _procedencia(nivel=Ampliado.OTRO)

    assert clave_de_cache(uno) != clave_de_cache(otro)


def test_el_enum_de_reproducibilidad_esta_congelado() -> None:
    assert {m.name: m.value for m in NivelDeReproducibilidad} == {
        "DETERMINISTA_MISMO_ENTORNO": "determinista_mismo_entorno"
    }


def test_la_clave_coincide_con_una_reimplementacion_independiente() -> None:
    proc = _procedencia()
    documento = {
        "esquema": "sky-claw.native-parallax.output-manifest/1",
        "algoritmo_id": "normal_fft_periodic_v1",
        "parametros": {"borde": "periodico", "escala": 1.5},
        "origenes": [{"rol": "normal", "sha256": H_A}],
        "encoder": {
            "exportador_id": "np_e0b_bc4_v1",
            "texconv_version": "may2026",
            "texconv_sha256": H_C,
            "argumentos": ["-f", "BC4_UNORM", "-m", "0", "-y", "-o"],
        },
        "entorno": {"numpy": "2.4.6", "plataforma": "win32"},
        "nivel": "determinista_mismo_entorno",
    }
    cuerpo = json.dumps(documento, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    esperado = hashlib.sha256(b"sky-claw/native-parallax/clave-de-cache/1\n" + cuerpo.encode("ascii")).hexdigest()

    assert clave_de_cache(proc) == esperado


def test_la_clave_de_referencia_esta_congelada() -> None:
    # Cambiar el formato de la clave invalida TODA caché existente: exige subir ``ESQUEMA_DEL_MANIFIESTO``.
    assert clave_de_cache(_procedencia()) == "915ece6eec7bdeeb75fd717ef83e3fdddd8097f62ab06b00da87ba131f78fe75"
    assert output.ESQUEMA_DEL_MANIFIESTO == "sky-claw.native-parallax.output-manifest/1"


# ---------------------------------------------------------------------------
# Entrada de manifiesto
# ---------------------------------------------------------------------------


def test_crear_entrada_deriva_la_clave_de_la_procedencia() -> None:
    entrada = _entrada()

    assert entrada.clave_de_cache == clave_de_cache(entrada.procedencia)
    assert (entrada.ruta, entrada.sha256, entrada.tamano) == (RUTA, H_B, 349_680)


def test_una_entrada_con_la_clave_equivocada_se_rechaza() -> None:
    with pytest.raises(ManifiestoInvalidoError):
        EntradaDeManifiesto(ruta=RUTA, sha256=H_B, tamano=10, clave_de_cache=H_D, procedencia=_procedencia())


@pytest.mark.parametrize(
    "cambios",
    [
        {"ruta": "meshes/a_p.dds"},
        {"sha256": "B" * 64},
        {"sha256": "b" * 63},
        {"tamano": 0},
        {"tamano": -1},
        {"tamano": True},
        {"tamano": 1.0},
        {"tamano": "10"},
        {"tamano": 2**40},
    ],
)
def test_una_entrada_invalida_se_rechaza(cambios: dict[str, Any]) -> None:
    base: dict[str, Any] = {"ruta": RUTA, "sha256": H_B, "tamano": 10, "procedencia": _procedencia()}
    base.update(cambios)
    with pytest.raises((ManifiestoInvalidoError, RutaDeSalidaInvalidaError)):
        crear_entrada(**base)


def test_una_entrada_con_procedencia_que_no_es_procedencia_se_rechaza() -> None:
    with pytest.raises(ManifiestoInvalidoError):
        crear_entrada(RUTA, H_B, 10, {"algoritmo_id": "x"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Manifiesto
# ---------------------------------------------------------------------------


def test_un_manifiesto_vacio_es_valido() -> None:
    assert Manifiesto(entradas=()).entradas == ()
    assert leer_manifiesto(serializar_manifiesto(Manifiesto(entradas=()))) == Manifiesto(entradas=())


def test_ordenado_ordena_por_la_clave_de_ruta() -> None:
    a, b, c = _entrada("textures/b_p.dds"), _entrada("textures/A_p.dds"), _entrada("textures/c_p.dds")

    manifiesto = Manifiesto.ordenado([c, a, b])

    assert [e.ruta for e in manifiesto.entradas] == ["textures/A_p.dds", "textures/b_p.dds", "textures/c_p.dds"]


def test_el_constructor_exige_el_orden_canonico() -> None:
    a, b = _entrada("textures/a_p.dds"), _entrada("textures/b_p.dds")

    with pytest.raises(ManifiestoInvalidoError):
        Manifiesto(entradas=(b, a))


def test_un_manifiesto_con_rutas_repetidas_ignorando_mayusculas_se_rechaza() -> None:
    with pytest.raises(ManifiestoInvalidoError):
        Manifiesto.ordenado([_entrada("textures/a_p.dds"), _entrada("Textures/A_P.dds")])


def test_el_manifiesto_exige_una_tupla_de_entradas() -> None:
    with pytest.raises(ManifiestoInvalidoError):
        Manifiesto(entradas=[_entrada()])  # type: ignore[arg-type]
    with pytest.raises(ManifiestoInvalidoError):
        Manifiesto(entradas=("no es una entrada",))  # type: ignore[arg-type]


def test_la_serializacion_es_determinista_y_no_depende_del_orden_de_entrada() -> None:
    entradas = [_entrada(f"textures/t{i}_p.dds", sha256=f"{i:x}" * 64) for i in range(1, 6)]
    referencia = serializar_manifiesto(Manifiesto.ordenado(entradas))

    for _ in range(5):
        mezcla = entradas[:]
        random.Random(7).shuffle(mezcla)
        assert serializar_manifiesto(Manifiesto.ordenado(mezcla)) == referencia


def test_la_serializacion_es_ascii_con_salto_final_y_claves_ordenadas() -> None:
    datos = serializar_manifiesto(_manifiesto(_entrada()))

    texto = datos.decode("ascii")
    assert texto.endswith("}\n")
    assert json.loads(texto)["mod"] == NOMBRE_DEL_MOD_DE_SALIDA
    assert texto == json.dumps(json.loads(texto), sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def test_ida_y_vuelta_del_manifiesto() -> None:
    manifiesto = _manifiesto(
        _entrada("textures/a_p.dds", sha256=H_A, tamano=1_000),
        _entrada("textures/b_p.dds", sha256=H_B, tamano=2_000, algoritmo_id="otro_algoritmo"),
    )

    assert leer_manifiesto(serializar_manifiesto(manifiesto)) == manifiesto


def test_la_huella_del_manifiesto_es_de_los_bytes_canonicos_con_su_propio_dominio() -> None:
    manifiesto = _manifiesto(_entrada())
    datos = serializar_manifiesto(manifiesto)

    esperado = hashlib.sha256(b"sky-claw/native-parallax/huella-del-manifiesto/1\n" + datos).hexdigest()
    assert huella_del_manifiesto(manifiesto) == esperado
    assert huella_del_manifiesto(manifiesto) != hashlib.sha256(datos).hexdigest()


def test_serializar_exige_un_manifiesto() -> None:
    with pytest.raises(ManifiestoInvalidoError):
        serializar_manifiesto("no es un manifiesto")  # type: ignore[arg-type]


# --- Lectura: esquema cerrado y falla cerrada -----------------------------------------------------------------------


def _documento_de_referencia() -> dict[str, Any]:
    return json.loads(serializar_manifiesto(_manifiesto(_entrada("textures/a_p.dds"), _entrada("textures/b_p.dds"))))


def _bytes(documento: dict[str, Any]) -> bytes:
    return (json.dumps(documento, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def test_el_documento_de_referencia_se_lee() -> None:
    assert len(leer_manifiesto(_bytes(_documento_de_referencia())).entradas) == 2


def test_el_manifiesto_de_referencia_esta_congelado_byte_a_byte() -> None:
    # El formato en disco es un contrato: cambiarlo exige subir ``ESQUEMA_DEL_MANIFIESTO``.
    datos = serializar_manifiesto(_manifiesto(_entrada("textures/a_p.dds", sha256=H_B, tamano=1234)))

    assert datos.decode("ascii") == (
        "{\n"
        '  "entradas": [\n'
        "    {\n"
        '      "clave_de_cache": "' + clave_de_cache(_procedencia()) + '",\n'
        '      "procedencia": {\n'
        '        "algoritmo_id": "normal_fft_periodic_v1",\n'
        '        "encoder": {\n'
        '          "argumentos": [\n'
        '            "-f",\n'
        '            "BC4_UNORM",\n'
        '            "-m",\n'
        '            "0",\n'
        '            "-y",\n'
        '            "-o"\n'
        "          ],\n"
        '          "exportador_id": "np_e0b_bc4_v1",\n'
        '          "texconv_sha256": "' + H_C + '",\n'
        '          "texconv_version": "may2026"\n'
        "        },\n"
        '        "entorno": {\n'
        '          "numpy": "2.4.6",\n'
        '          "plataforma": "win32"\n'
        "        },\n"
        '        "nivel": "determinista_mismo_entorno",\n'
        '        "origenes": [\n'
        "          {\n"
        '            "rol": "normal",\n'
        '            "sha256": "' + H_A + '"\n'
        "          }\n"
        "        ],\n"
        '        "parametros": {\n'
        '          "borde": "periodico",\n'
        '          "escala": 1.5\n'
        "        }\n"
        "      },\n"
        '      "ruta": "textures/a_p.dds",\n'
        '      "sha256": "' + H_B + '",\n'
        '      "tamano": 1234\n'
        "    }\n"
        "  ],\n"
        '  "esquema": "sky-claw.native-parallax.output-manifest/1",\n'
        '  "mod": "Sky-Claw - Native Parallax Output"\n'
        "}\n"
    )


@pytest.mark.parametrize("datos", [None, "texto", 3, [b"x"]])
def test_leer_exige_bytes(datos: object) -> None:
    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(datos)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "datos",
    [
        b"",
        b"   \n",
        b"\xef\xbb\xbf" + _bytes(_documento_de_referencia()),  # BOM
        b"\xff\xfe\x00",  # no es UTF-8
        b"{",
        b"[]",
        b"null",
        b"3",
        b'"texto"',
    ],
)
def test_un_contenido_que_no_es_un_manifiesto_se_rechaza(datos: bytes) -> None:
    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(datos)


def test_el_tamano_maximo_se_rechaza_antes_de_decodificar() -> None:
    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(b" " * (output.TAMANO_MAXIMO_DEL_MANIFIESTO + 1))


def test_un_manifiesto_no_canonico_se_rechaza() -> None:
    documento = _documento_de_referencia()
    compacto = json.dumps(documento, sort_keys=True, separators=(",", ":")).encode("ascii")
    sin_salto_final = _bytes(documento)[:-1]
    con_basura = _bytes(documento) + b" "

    for datos in (compacto, sin_salto_final, con_basura):
        with pytest.raises(ManifiestoInvalidoError):
            leer_manifiesto(datos)


def test_claves_duplicadas_se_rechazan() -> None:
    texto = _bytes(_documento_de_referencia()).decode("ascii")
    duplicado = texto.replace('  "mod": ', '  "mod": "otro",\n  "mod": ', 1)

    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(duplicado.encode("ascii"))


@pytest.mark.parametrize("constante", ["NaN", "Infinity", "-Infinity"])
def test_las_constantes_no_finitas_se_rechazan(constante: str) -> None:
    texto = _bytes(_documento_de_referencia()).decode("ascii").replace('"escala": 1.5', f'"escala": {constante}')

    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(texto.encode("ascii"))


def _objetos(documento: Any, camino: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Camino a CADA objeto JSON del documento, salvo ``parametros`` (que es libre por diseño)."""
    encontrados: list[tuple[Any, ...]] = []
    if isinstance(documento, dict):
        encontrados.append(camino)
        for clave, valor in documento.items():
            if clave == "parametros":
                continue
            encontrados.extend(_objetos(valor, (*camino, clave)))
    elif isinstance(documento, list):
        for i, valor in enumerate(documento):
            encontrados.extend(_objetos(valor, (*camino, i)))
    return encontrados


def _en(documento: Any, camino: tuple[Any, ...]) -> Any:
    for paso in camino:
        documento = documento[paso]
    return documento


def test_hay_objetos_en_cada_nivel_del_documento() -> None:
    # Guarda del enumerador: si el formato cambia de forma y deja de haber objetos anidados, los dos tests de abajo
    # se vaciarían en silencio.
    caminos = _objetos(_documento_de_referencia())

    assert () in caminos
    assert ("entradas", 0) in caminos
    assert ("entradas", 0, "procedencia") in caminos
    assert ("entradas", 0, "procedencia", "encoder") in caminos
    assert ("entradas", 0, "procedencia", "entorno") in caminos
    assert ("entradas", 0, "procedencia", "origenes", 0) in caminos


def test_una_clave_de_mas_en_cualquier_objeto_se_rechaza() -> None:
    for camino in _objetos(_documento_de_referencia()):
        documento = copy.deepcopy(_documento_de_referencia())
        if camino[-1:] == ("entorno",):
            continue  # el entorno es un mapa abierto de nombre -> versión: una clave más es un dato, no un campo
        _en(documento, camino)["campo_de_mas"] = "x"
        with pytest.raises(ManifiestoInvalidoError):
            leer_manifiesto(_bytes(documento))


def test_una_clave_de_menos_en_cualquier_objeto_se_rechaza() -> None:
    for camino in _objetos(_documento_de_referencia()):
        if camino[-1:] == ("entorno",):
            continue
        for clave in list(_en(_documento_de_referencia(), camino)):
            documento = copy.deepcopy(_documento_de_referencia())
            del _en(documento, camino)[clave]
            with pytest.raises(ManifiestoInvalidoError):
                leer_manifiesto(_bytes(documento))


@pytest.mark.parametrize(
    ("camino", "valor"),
    [
        (("esquema",), "otro/1"),
        (("esquema",), "sky-claw.native-parallax.output-manifest/2"),
        (("esquema",), 1),
        (("mod",), "Otro Mod"),
        (("mod",), None),
        (("entradas",), {}),
        (("entradas",), None),
        (("entradas", 0, "ruta"), "meshes/a_p.dds"),
        (("entradas", 0, "ruta"), 3),
        (("entradas", 0, "sha256"), "A" * 64),
        (("entradas", 0, "sha256"), 3),
        (("entradas", 0, "tamano"), True),
        (("entradas", 0, "tamano"), 0),
        (("entradas", 0, "tamano"), 1.5),
        (("entradas", 0, "tamano"), "10"),
        (("entradas", 0, "clave_de_cache"), H_D),
        (("entradas", 0, "clave_de_cache"), 3),
        (("entradas", 0, "procedencia"), []),
        (("entradas", 0, "procedencia", "algoritmo_id"), "Mal Id"),
        (("entradas", 0, "procedencia", "algoritmo_id"), 3),
        (("entradas", 0, "procedencia", "nivel"), "otro_nivel"),
        (("entradas", 0, "procedencia", "nivel"), 1),
        (("entradas", 0, "procedencia", "origenes"), []),
        (("entradas", 0, "procedencia", "origenes"), {}),
        (("entradas", 0, "procedencia", "origenes", 0, "sha256"), "x"),
        (("entradas", 0, "procedencia", "encoder", "texconv_sha256"), "x"),
        (("entradas", 0, "procedencia", "encoder", "argumentos"), "-f"),
        (("entradas", 0, "procedencia", "encoder", "argumentos"), [1]),
        (("entradas", 0, "procedencia", "entorno"), []),
        (("entradas", 0, "procedencia", "entorno"), {}),
        (("entradas", 0, "procedencia", "entorno"), {"numpy": 3}),
        (("entradas", 0, "procedencia", "parametros"), []),
        (("entradas", 0, "procedencia", "parametros"), {"v": float(2**70)}),
    ],
)
def test_un_valor_invalido_en_cualquier_campo_se_rechaza(camino: tuple[Any, ...], valor: Any) -> None:
    documento = _documento_de_referencia()
    _en(documento, camino[:-1])[camino[-1]] = valor

    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(_bytes(documento))


def test_alterar_un_parametro_sin_actualizar_la_clave_se_detecta() -> None:
    documento = _documento_de_referencia()
    documento["entradas"][0]["procedencia"]["parametros"]["escala"] = 9.5

    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(_bytes(documento))


def test_entradas_fuera_de_orden_o_repetidas_se_rechazan() -> None:
    documento = _documento_de_referencia()
    documento["entradas"].reverse()
    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(_bytes(documento))

    repetido = _documento_de_referencia()
    repetido["entradas"][1] = copy.deepcopy(repetido["entradas"][0])
    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(_bytes(repetido))


def test_un_error_de_lectura_conserva_la_causa_original() -> None:
    with pytest.raises(ManifiestoInvalidoError) as info:
        leer_manifiesto(b"{")

    assert info.value.__cause__ is not None


# ---------------------------------------------------------------------------
# Observaciones y salidas deseadas
# ---------------------------------------------------------------------------


def test_una_observacion_de_archivo_exige_huella() -> None:
    assert Observacion(TipoObservado.ARCHIVO, H_A).sha256 == H_A
    with pytest.raises(PlanInvalidoError):
        Observacion(TipoObservado.ARCHIVO, None)
    with pytest.raises(PlanInvalidoError):
        Observacion(TipoObservado.ARCHIVO, "A" * 64)


@pytest.mark.parametrize("tipo", [TipoObservado.AUSENTE, TipoObservado.OTRO])
def test_una_observacion_que_no_es_archivo_no_lleva_huella(tipo: TipoObservado) -> None:
    assert Observacion(tipo, None).sha256 is None
    with pytest.raises(PlanInvalidoError):
        Observacion(tipo, H_A)


def test_una_observacion_exige_el_enum() -> None:
    with pytest.raises(PlanInvalidoError):
        Observacion("archivo", H_A)  # type: ignore[arg-type]


def test_una_salida_deseada_valida_su_ruta_y_su_clave() -> None:
    assert SalidaDeseada(RUTA, H_A).ruta == RUTA
    with pytest.raises(RutaDeSalidaInvalidaError):
        SalidaDeseada("meshes/a_p.dds", H_A)
    with pytest.raises(PlanInvalidoError):
        SalidaDeseada(RUTA, "no es una huella")


# ---------------------------------------------------------------------------
# Planificador: la tabla, enumerada
# ---------------------------------------------------------------------------

A = AccionDeSalida
M = MotivoDeSalida
T = TipoObservado

#: (deseada, en_manifiesto, clave_igual, observado, hash_coincide) -> (acción, motivo).
#: ``clave_igual`` solo existe si la salida está deseada Y en el manifiesto; ``hash_coincide`` solo si además hay un
#: archivo en disco. Escrita a mano, sin reusar nada del módulo: es la especificación.
TABLA_ESPERADA: dict[
    tuple[bool, bool, bool | None, TipoObservado, bool | None], tuple[AccionDeSalida, MotivoDeSalida]
] = {
    # --- deseada, no figura en el manifiesto: nada es nuestro todavía (PRESERVE_EXISTING) ---
    (True, False, None, T.AUSENTE, None): (A.CONSTRUIR, M.NUEVA),
    (True, False, None, T.ARCHIVO, None): (A.PRESERVAR, M.AJENO_EXISTENTE),
    (True, False, None, T.OTRO, None): (A.PRESERVAR, M.OCUPADO_POR_OTRO_TIPO),
    # --- deseada y figura en el manifiesto ---
    (True, True, True, T.AUSENTE, None): (A.CONSTRUIR, M.FALTA_EN_DISCO),
    (True, True, False, T.AUSENTE, None): (A.CONSTRUIR, M.FALTA_EN_DISCO),
    (True, True, True, T.OTRO, None): (A.PRESERVAR, M.OCUPADO_POR_OTRO_TIPO),
    (True, True, False, T.OTRO, None): (A.PRESERVAR, M.OCUPADO_POR_OTRO_TIPO),
    (True, True, True, T.ARCHIVO, True): (A.OMITIR, M.CACHE_VIGENTE),
    (True, True, False, T.ARCHIVO, True): (A.RECONSTRUIR, M.CLAVE_CAMBIO),
    (True, True, True, T.ARCHIVO, False): (A.PRESERVAR, M.MODIFICADO_POR_TERCEROS),
    (True, True, False, T.ARCHIVO, False): (A.PRESERVAR, M.MODIFICADO_POR_TERCEROS),
    # --- figura en el manifiesto y ya no se desea: huérfana ---
    (False, True, None, T.AUSENTE, None): (A.SOLTAR, M.HUERFANO_AUSENTE),
    (False, True, None, T.OTRO, None): (A.SOLTAR, M.OCUPADO_POR_OTRO_TIPO),
    (False, True, None, T.ARCHIVO, True): (A.RETIRAR, M.HUERFANO_PROPIO),
    (False, True, None, T.ARCHIVO, False): (A.SOLTAR, M.MODIFICADO_POR_TERCEROS),
}


def _estados_posibles() -> list[tuple[bool, bool, bool | None, TipoObservado, bool | None]]:
    """TODAS las combinaciones definidas de las cinco dimensiones; ``(False, False, ...)`` no se planifica."""
    estados = []
    for deseada, en_manifiesto, tipo in itertools.product((True, False), (True, False), TipoObservado):
        if not deseada and not en_manifiesto:
            continue
        claves = (True, False) if deseada and en_manifiesto else (None,)
        hashes = (True, False) if en_manifiesto and tipo is TipoObservado.ARCHIVO else (None,)
        for clave_igual, hash_coincide in itertools.product(claves, hashes):
            estados.append((deseada, en_manifiesto, clave_igual, tipo, hash_coincide))
    return estados


def _escenario(
    estado: tuple[bool, bool, bool | None, TipoObservado, bool | None],
) -> tuple[list[SalidaDeseada], Manifiesto | None, dict[str, Observacion]]:
    """Arma un caso mínimo (una sola ruta) que realiza ``estado``."""
    deseada, en_manifiesto, clave_igual, tipo, hash_coincide = estado
    entrada = _entrada(RUTA, sha256=H_B)
    previo = _manifiesto(entrada) if en_manifiesto else None
    if deseada:
        clave = entrada.clave_de_cache if (en_manifiesto and clave_igual) else H_D
        deseadas = [SalidaDeseada(RUTA, clave)]
    else:
        deseadas = []
    sha_en_disco = H_B if hash_coincide else H_A  # H_B es el del manifiesto
    observacion = Observacion(tipo, sha_en_disco if tipo is TipoObservado.ARCHIVO else None)
    return deseadas, previo, {clave_de_ruta(RUTA): observacion}


def test_la_tabla_cubre_exactamente_los_estados_posibles() -> None:
    assert set(TABLA_ESPERADA) == set(_estados_posibles())
    assert len(TABLA_ESPERADA) == 15


@pytest.mark.parametrize("estado", _estados_posibles(), ids=str)
def test_el_planificador_cumple_la_tabla_para_cada_estado(
    estado: tuple[bool, bool, bool | None, TipoObservado, bool | None],
) -> None:
    deseadas, previo, observado = _escenario(estado)

    plan = planificar(deseadas, previo, observado)

    assert len(plan.acciones) == 1
    accion = plan.acciones[0]
    assert (accion.accion, accion.motivo) == TABLA_ESPERADA[estado]
    assert accion.ruta == RUTA


def test_una_ruta_ni_deseada_ni_en_el_manifiesto_no_genera_accion() -> None:
    observado = {clave_de_ruta(RUTA): Observacion(TipoObservado.ARCHIVO, H_A)}

    plan = planificar([], None, observado)

    assert plan.acciones == ()


@pytest.mark.parametrize("estado", _estados_posibles(), ids=str)
def test_las_acciones_que_tocan_un_archivo_existente_llevan_la_huella_que_las_autoriza(
    estado: tuple[bool, bool, bool | None, TipoObservado, bool | None],
) -> None:
    deseadas, previo, observado = _escenario(estado)

    accion = planificar(deseadas, previo, observado).acciones[0]

    if accion.accion in (A.RECONSTRUIR, A.RETIRAR):
        # El escritor tiene que volver a comprobar que el disco sigue teniendo ESTA huella antes de tocarlo.
        assert accion.sha256_autorizado == H_B
    else:
        assert accion.sha256_autorizado is None


@pytest.mark.parametrize("estado", _estados_posibles(), ids=str)
def test_invariantes_de_seguridad_por_estado(
    estado: tuple[bool, bool, bool | None, TipoObservado, bool | None],
) -> None:
    deseada, en_manifiesto, _clave_igual, tipo, hash_coincide = estado
    deseadas, previo, observado = _escenario(estado)

    accion = planificar(deseadas, previo, observado).acciones[0]

    if accion.accion in (A.RECONSTRUIR, A.RETIRAR):
        # Solo se pisa o se borra lo que es nuestro Y sigue intacto.
        assert en_manifiesto
        assert tipo is TipoObservado.ARCHIVO
        assert hash_coincide is True
    if accion.accion is A.CONSTRUIR:
        # Solo se crea donde no hay nada.
        assert tipo is TipoObservado.AUSENTE
    if tipo is TipoObservado.OTRO:
        # Un directorio, un enlace o un dispositivo en esa ruta nunca se toca.
        assert accion.accion in (A.PRESERVAR, A.SOLTAR)
    if tipo is TipoObservado.ARCHIVO and not en_manifiesto:
        # Un archivo que no figura en el manifiesto es de un tercero.
        assert accion.accion is A.PRESERVAR
    if not deseada:
        assert accion.accion in (A.RETIRAR, A.SOLTAR)


def test_modifica_disco_solo_para_construir_reconstruir_y_retirar() -> None:
    modifican = {
        a for a in AccionDeSalida if AccionPlanificada(RUTA, a, _motivo_para(a), _huella_para(a)).modifica_disco
    }

    assert modifican == {A.CONSTRUIR, A.RECONSTRUIR, A.RETIRAR}


def _motivo_para(accion: AccionDeSalida) -> MotivoDeSalida:
    return next(m for (a, m) in TABLA_ESPERADA.values() if a is accion)


def _huella_para(accion: AccionDeSalida) -> str | None:
    return H_B if accion in (A.RECONSTRUIR, A.RETIRAR) else None


def test_el_enum_de_acciones_y_de_motivos_esta_congelado() -> None:
    assert {a.name for a in AccionDeSalida} == {"CONSTRUIR", "RECONSTRUIR", "OMITIR", "PRESERVAR", "RETIRAR", "SOLTAR"}
    assert {m.name for m in MotivoDeSalida} == {
        "NUEVA",
        "FALTA_EN_DISCO",
        "CACHE_VIGENTE",
        "CLAVE_CAMBIO",
        "AJENO_EXISTENTE",
        "MODIFICADO_POR_TERCEROS",
        "OCUPADO_POR_OTRO_TIPO",
        "HUERFANO_PROPIO",
        "HUERFANO_AUSENTE",
    }
    assert {t.name for t in TipoObservado} == {"AUSENTE", "ARCHIVO", "OTRO"}


@pytest.mark.parametrize(
    ("accion", "motivo", "huella"),
    [
        (A.RECONSTRUIR, M.CLAVE_CAMBIO, None),  # reconstruir sin huella autorizante
        (A.RETIRAR, M.HUERFANO_PROPIO, None),
        (A.CONSTRUIR, M.NUEVA, H_B),  # construir no autoriza ninguna huella: no hay archivo
        (A.OMITIR, M.CACHE_VIGENTE, H_B),
        (A.PRESERVAR, M.AJENO_EXISTENTE, H_B),
        (A.SOLTAR, M.HUERFANO_AUSENTE, H_B),
        (A.RETIRAR, M.HUERFANO_PROPIO, "no es una huella"),
        ("construir", M.NUEVA, None),
        (A.CONSTRUIR, "nueva", None),
    ],
)
def test_una_accion_incoherente_se_rechaza(accion: Any, motivo: Any, huella: Any) -> None:
    with pytest.raises(PlanInvalidoError):
        AccionPlanificada(RUTA, accion, motivo, huella)


def test_una_accion_valida_su_ruta() -> None:
    with pytest.raises(RutaDeSalidaInvalidaError):
        AccionPlanificada("meshes/x_p.dds", A.CONSTRUIR, M.NUEVA, None)


# --- Planificador: varias rutas ------------------------------------------------------------------------------------


def test_el_plan_esta_ordenado_por_la_clave_de_ruta_y_no_depende_del_orden_de_entrada() -> None:
    rutas = [f"textures/t{i}_p.dds" for i in range(8)]
    deseadas = [SalidaDeseada(r, H_A) for r in rutas]
    observado = {clave_de_ruta(r): Observacion(TipoObservado.AUSENTE, None) for r in rutas}
    referencia = planificar(deseadas, None, observado)

    assert [a.ruta for a in referencia.acciones] == sorted(rutas, key=clave_de_ruta)
    for semilla in range(5):
        mezcla = deseadas[:]
        random.Random(semilla).shuffle(mezcla)
        assert planificar(mezcla, None, observado) == referencia


def test_planificar_varias_rutas_es_la_union_de_planificar_cada_una() -> None:
    # Las rutas no interactúan: se comprueba con mezclas aleatorias de estados de rutas distintas.
    estados = _estados_posibles()
    rng = random.Random(2026)
    for _ in range(60):
        elegidos = rng.sample(estados, k=rng.randint(2, 6))
        deseadas: list[SalidaDeseada] = []
        entradas: list[EntradaDeManifiesto] = []
        observado: dict[str, Observacion] = {}
        esperado: list[AccionPlanificada] = []
        for i, estado in enumerate(elegidos):
            ruta = f"textures/r{i}_p.dds"
            deseada, en_manifiesto, clave_igual, tipo, hash_coincide = estado
            entrada = _entrada(ruta, sha256=H_B)
            if en_manifiesto:
                entradas.append(entrada)
            if deseada:
                clave = entrada.clave_de_cache if (en_manifiesto and clave_igual) else H_D
                deseadas.append(SalidaDeseada(ruta, clave))
            sha = H_B if hash_coincide else H_A
            observado[clave_de_ruta(ruta)] = Observacion(tipo, sha if tipo is TipoObservado.ARCHIVO else None)
            accion, motivo = TABLA_ESPERADA[estado]
            esperado.append(
                AccionPlanificada(ruta, accion, motivo, H_B if accion in (A.RECONSTRUIR, A.RETIRAR) else None)
            )

        plan = planificar(deseadas, Manifiesto.ordenado(entradas) if entradas else None, observado)

        assert plan.acciones == tuple(sorted(esperado, key=lambda a: clave_de_ruta(a.ruta)))


def test_dos_salidas_deseadas_que_chocan_ignorando_mayusculas_se_rechazan() -> None:
    deseadas = [SalidaDeseada("textures/a_p.dds", H_A), SalidaDeseada("Textures/A_P.dds", H_A)]
    observado = {clave_de_ruta("textures/a_p.dds"): Observacion(TipoObservado.AUSENTE, None)}

    with pytest.raises(PlanInvalidoError):
        planificar(deseadas, None, observado)


def test_una_salida_repetida_exacta_tambien_se_rechaza() -> None:
    deseadas = [SalidaDeseada(RUTA, H_A), SalidaDeseada(RUTA, H_A)]
    observado = {clave_de_ruta(RUTA): Observacion(TipoObservado.AUSENTE, None)}

    with pytest.raises(PlanInvalidoError):
        planificar(deseadas, None, observado)


def test_la_deseada_y_la_del_manifiesto_con_distinta_capitalizacion_son_la_misma_ruta() -> None:
    previo = _manifiesto(_entrada("textures/Muro_P.dds", sha256=H_B))
    clave = previo.entradas[0].clave_de_cache
    deseadas = [SalidaDeseada("textures/muro_p.dds", clave)]
    observado = {clave_de_ruta("textures/muro_p.dds"): Observacion(TipoObservado.ARCHIVO, H_B)}

    plan = planificar(deseadas, previo, observado)

    assert [(a.accion, a.motivo) for a in plan.acciones] == [(A.OMITIR, M.CACHE_VIGENTE)]


def test_falta_la_observacion_de_una_ruta_relevante() -> None:
    previo = _manifiesto(_entrada("textures/a_p.dds"))
    with pytest.raises(PlanInvalidoError):
        planificar([SalidaDeseada("textures/b_p.dds", H_A)], None, {})
    with pytest.raises(PlanInvalidoError):
        planificar([], previo, {})  # una huérfana tampoco se planifica a ciegas


def test_las_observaciones_de_mas_se_ignoran() -> None:
    observado = {
        clave_de_ruta(RUTA): Observacion(TipoObservado.AUSENTE, None),
        clave_de_ruta("textures/otra_p.dds"): Observacion(TipoObservado.ARCHIVO, H_A),
    }

    plan = planificar([SalidaDeseada(RUTA, H_A)], None, observado)

    assert [a.ruta for a in plan.acciones] == [RUTA]


def test_el_mapa_de_observaciones_se_indexa_por_la_clave_de_ruta() -> None:
    # Una observación guardada con la ruta cruda (sin ``clave_de_ruta``) no cuenta: se falla en vez de adivinar.
    # El mensaje nombra la clave que había que usar, no solo la ruta: es lo que permite diagnosticar un error de mayúsculas.
    with pytest.raises(PlanInvalidoError, match="'textures/a_p.dds'"):
        planificar([SalidaDeseada("Textures/A_p.dds", H_A)], None, {"Textures/A_p.dds": Observacion(T.AUSENTE, None)})


def test_planificar_valida_los_tipos_de_sus_argumentos() -> None:
    observado = {clave_de_ruta(RUTA): Observacion(TipoObservado.AUSENTE, None)}
    with pytest.raises(PlanInvalidoError):
        planificar(["textures/a_p.dds"], None, observado)  # type: ignore[list-item]
    with pytest.raises(PlanInvalidoError):
        planificar([], "manifiesto", observado)  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError):
        planificar([], None, {clave_de_ruta(RUTA): "archivo"})  # type: ignore[dict-item]
    with pytest.raises(PlanInvalidoError):
        planificar([], None, [])  # type: ignore[arg-type]


def test_el_plan_es_inmutable_y_comparable() -> None:
    plan = planificar([SalidaDeseada(RUTA, H_A)], None, {clave_de_ruta(RUTA): Observacion(T.AUSENTE, None)})

    assert isinstance(plan, PlanDeSalida)
    assert isinstance(plan.acciones, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.acciones = ()  # type: ignore[misc]


def test_el_plan_resume_si_hay_cambios_en_el_disco() -> None:
    vacio = planificar([], None, {})
    nuevo = planificar([SalidaDeseada(RUTA, H_A)], None, {clave_de_ruta(RUTA): Observacion(T.AUSENTE, None)})
    previo = _manifiesto(_entrada(RUTA))
    vigente = planificar(
        [SalidaDeseada(RUTA, previo.entradas[0].clave_de_cache)],
        previo,
        {clave_de_ruta(RUTA): Observacion(T.ARCHIVO, H_B)},
    )

    assert (vacio.hay_cambios, nuevo.hay_cambios, vigente.hay_cambios) == (False, True, False)


# ---------------------------------------------------------------------------
# Bordes: lo último que entra y lo primero que no (huecos que mostró la mutación manual)
# ---------------------------------------------------------------------------


def _ruta_de_largo(largo: int) -> str:
    return "textures/" + "x" * (largo - len("textures/") - len("_p.dds")) + "_p.dds"


def test_el_largo_de_la_ruta_se_prueba_en_su_borde() -> None:
    assert validar_ruta_de_salida(_ruta_de_largo(200)) == _ruta_de_largo(200)
    with pytest.raises(RutaDeSalidaInvalidaError):
        validar_ruta_de_salida(_ruta_de_largo(201))


@pytest.mark.parametrize("ruta", ["textures/a~b!c_p.dds", "textures/a b_p.dds"])
def test_los_extremos_del_rango_ascii_imprimible_se_aceptan(ruta: str) -> None:
    # '~' (0x7E) es el último imprimible, '!' (0x21) el primero tras el espacio (0x20).
    assert validar_ruta_de_salida(ruta) == ruta


@pytest.mark.parametrize(
    "ruta", ["textures/a\\b_p.dds", "textures/ap.dds", "textures/abp.dds", "textures/a_p.dd", "textures/a.dds"]
)
def test_mas_rutas_invalidas(ruta: str) -> None:
    # La barra invertida en el medio (sin ella la ruta sería válida) y un sufijo sin el guion bajo.
    with pytest.raises(RutaDeSalidaInvalidaError):
        validar_ruta_de_salida(ruta)


_RESERVADOS = [
    "con",
    "conin$",
    "conout$",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(10)),
    *(f"lpt{i}" for i in range(10)),
]


@pytest.mark.parametrize("nombre", _RESERVADOS)
def test_cada_nombre_reservado_de_windows_se_rechaza_en_cualquier_capitalizacion(nombre: str) -> None:
    for variante in (nombre, nombre.upper(), nombre.capitalize()):
        with pytest.raises(RutaDeSalidaInvalidaError):
            validar_ruta_de_salida(f"textures/{variante}/a_p.dds")
        with pytest.raises(RutaDeSalidaInvalidaError):
            validar_ruta_de_salida(f"textures/{variante}.txt/a_p.dds")


def test_un_nombre_que_solo_empieza_como_uno_reservado_es_valido() -> None:
    assert validar_ruta_de_salida("textures/console/lpt10/com10/conin/conout$x/a_p.dds")


def test_el_largo_de_los_parametros_se_prueba_en_su_borde() -> None:
    assert len(parametros_canonicos({"v": "x" * 65_528})) == 65_536
    with pytest.raises(ProcedenciaInvalidaError):
        parametros_canonicos({"v": "x" * 65_529})


def _anidado(contenedores: int, *, listas: bool) -> dict[str, Any]:
    """Un objeto con EXACTAMENTE ``contenedores`` niveles de lista u objeto contando el de arriba."""
    valor: Any = 1
    for _ in range(contenedores - 1):
        valor = [valor] if listas else {"a": valor}
    return {"a": valor}


@pytest.mark.parametrize("listas", [False, True], ids=["objetos", "listas"])
def test_la_profundidad_de_los_parametros_se_prueba_en_su_borde(listas: bool) -> None:
    assert parametros_canonicos(_anidado(8, listas=listas))
    with pytest.raises(ProcedenciaInvalidaError):
        parametros_canonicos(_anidado(9, listas=listas))


def test_los_enteros_de_64_bits_se_aceptan_en_sus_dos_extremos() -> None:
    assert parametros_canonicos({"a": -(2**63), "b": 2**63 - 1}) == '{"a":-9223372036854775808,"b":9223372036854775807}'


def test_una_clave_de_parametros_no_ascii_se_rechaza() -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        parametros_canonicos({"ñ": 1})


def test_un_texto_con_el_caracter_de_borrado_se_escapa_de_forma_canonica() -> None:
    assert parametros_canonicos({"v": "\x7f"}) == '{"v":"\\u007f"}'


def test_el_cargador_estricto_rechaza_claves_repetidas_en_cualquier_nivel_y_constantes_no_finitas() -> None:
    for texto in ('{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '[{"a":1,"a":1}]'):
        with pytest.raises(ValueError, match="repetida"):
            output._cargar_json_estricto(texto)
    for constante in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="no finita"):
            output._cargar_json_estricto(f'{{"a":{constante}}}')
    assert output._cargar_json_estricto('{"a":{"b":[1,2.5,null,true]}}') == {"a": {"b": [1, 2.5, None, True]}}


def test_el_volcado_canonico_no_acepta_no_finitos_y_escapa_lo_no_imprimible() -> None:
    with pytest.raises(ValueError, match="Out of range"):
        output._volcar_canonico(float("nan"))
    assert output._volcar_canonico("\x7f") == '"\\u007f"'


@pytest.mark.parametrize(
    ("cantidad", "valido"),
    [(1, True), (64, True), (65, False)],
)
def test_la_cantidad_de_origenes_se_prueba_en_su_borde(cantidad: int, valido: bool) -> None:
    origenes = tuple(OrigenDeEntrada(rol=f"o{i:02d}", sha256=H_A) for i in range(cantidad))
    if valido:
        assert len(_procedencia(origenes=origenes).origenes) == cantidad
    else:
        with pytest.raises(ProcedenciaInvalidaError):
            _procedencia(origenes=origenes)


@pytest.mark.parametrize(("cantidad", "valido"), [(1, True), (32, True), (33, False)])
def test_la_cantidad_de_variables_de_entorno_se_prueba_en_su_borde(cantidad: int, valido: bool) -> None:
    entorno = tuple((f"v{i:02d}", "1") for i in range(cantidad))
    if valido:
        assert len(_procedencia(entorno=entorno).entorno) == cantidad
    else:
        with pytest.raises(ProcedenciaInvalidaError):
            _procedencia(entorno=entorno)


@pytest.mark.parametrize("entorno", [[("numpy", "2.4.6")], (["numpy", "2.4.6"],), (("numpy", "2.4.6", "x"),)])
def test_el_entorno_exige_una_tupla_de_pares_en_tupla(entorno: object) -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        _procedencia(entorno=entorno)


@pytest.mark.parametrize(("cantidad", "valido"), [(0, True), (64, True), (65, False)])
def test_la_cantidad_de_argumentos_se_prueba_en_su_borde(cantidad: int, valido: bool) -> None:
    argumentos = ("-x",) * cantidad
    if valido:
        assert len(_encoder(argumentos=argumentos).argumentos) == cantidad
    else:
        with pytest.raises(ProcedenciaInvalidaError):
            _encoder(argumentos=argumentos)


@pytest.mark.parametrize(("largo", "valido"), [(1, True), (256, True), (257, False)])
def test_el_largo_de_un_argumento_se_prueba_en_su_borde(largo: int, valido: bool) -> None:
    if valido:
        assert _encoder(argumentos=("a" * largo,)).argumentos == ("a" * largo,)
    else:
        with pytest.raises(ProcedenciaInvalidaError):
            _encoder(argumentos=("a" * largo,))


@pytest.mark.parametrize(
    ("caracter", "valido"), [(" ", True), ("~", True), ("\x1f", False), ("\x7f", False), ("\t", False)]
)
def test_un_argumento_admite_imprimibles_y_rechaza_controles(caracter: str, valido: bool) -> None:
    argumento = f"a{caracter}b"
    if valido:
        assert _encoder(argumentos=(argumento,)).argumentos == (argumento,)
    else:
        with pytest.raises(ProcedenciaInvalidaError):
            _encoder(argumentos=(argumento,))


def _origen(rol: str) -> OrigenDeEntrada:
    return OrigenDeEntrada(rol=rol, sha256=H_A)


@pytest.mark.parametrize(
    ("construir", "maximo", "caracteres_validos", "caracteres_invalidos"),
    [
        pytest.param(lambda s: _procedencia(algoritmo_id=s), 64, "a_b9", "-A ", id="algoritmo_id"),
        pytest.param(lambda s: _encoder(exportador_id=s), 64, "a_b9", "-A ", id="exportador_id"),
        pytest.param(lambda s: _origen(s), 32, "a_b9", "-A ", id="rol"),
        pytest.param(lambda s: _procedencia(entorno=((s, "1"),)), 32, "a_b9", "-A ", id="nombre_de_entorno"),
    ],
)
def test_los_identificadores_se_prueban_en_su_borde_y_en_su_alfabeto(
    construir: Any, maximo: int, caracteres_validos: str, caracteres_invalidos: str
) -> None:
    assert construir("a")  # un solo carácter
    assert construir("a" + "b" * (maximo - 1))  # el largo máximo
    with pytest.raises(ProcedenciaInvalidaError):
        construir("a" + "b" * maximo)  # un carácter más
    with pytest.raises(ProcedenciaInvalidaError):
        construir("")
    for caracter in caracteres_validos[1:]:
        assert construir(f"a{caracter}")
    for caracter in caracteres_invalidos:
        with pytest.raises(ProcedenciaInvalidaError):
            construir(f"a{caracter}b")
    with pytest.raises(ProcedenciaInvalidaError):
        construir("9a")  # no puede empezar con un dígito
    with pytest.raises(ProcedenciaInvalidaError):
        construir("_a")


def test_la_version_de_texconv_se_prueba_en_su_borde_y_en_su_alfabeto() -> None:
    assert _encoder(texconv_version="a")
    assert _encoder(texconv_version="a" * 32)
    assert _encoder(texconv_version="A.b_c-9")
    for malo in ("", "a" * 33, "a+b", "a b", "a/b"):
        with pytest.raises(ProcedenciaInvalidaError):
            _encoder(texconv_version=malo)


def test_el_valor_de_una_variable_de_entorno_se_prueba_en_su_borde_y_en_su_alfabeto() -> None:
    assert _procedencia(entorno=(("numpy", "1"),))
    assert _procedencia(entorno=(("numpy", "a" * 64),))
    assert _procedencia(entorno=(("numpy", "1.2.3+local_build-x"),))
    for malo in ("", "a" * 65, "1 2", "a/b", "ñ"):
        with pytest.raises(ProcedenciaInvalidaError):
            _procedencia(entorno=(("numpy", malo),))


class _ConTextoDeHuella:
    """No es un texto, pero ``str()`` de él SÍ es una huella válida: el tipo se comprueba, no el texto."""

    def __str__(self) -> str:
        return H_A


class _ConTextoDeRol:
    def __str__(self) -> str:
        return "normal"


def test_un_objeto_que_se_ve_como_huella_o_rol_pero_no_es_texto_se_rechaza() -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        OrigenDeEntrada(rol="normal", sha256=_ConTextoDeHuella())  # type: ignore[arg-type]
    with pytest.raises(ProcedenciaInvalidaError):
        OrigenDeEntrada(rol=_ConTextoDeRol(), sha256=H_A)  # type: ignore[arg-type]


def test_un_json_anidado_hasta_agotar_la_recursion_se_rechaza_con_el_error_del_modulo() -> None:
    profundo = "[" * 200_000
    with pytest.raises(ManifiestoInvalidoError):
        leer_manifiesto(profundo.encode("ascii"))
    with pytest.raises(ProcedenciaInvalidaError):
        _procedencia(parametros=profundo)


def test_clave_de_cache_exige_una_procedencia() -> None:
    with pytest.raises(ProcedenciaInvalidaError):
        clave_de_cache("no es una procedencia")  # type: ignore[arg-type]


@pytest.mark.parametrize(("tamano", "valido"), [(1, True), (2**30, True), (2**30 + 1, False), (0, False)])
def test_el_tamano_de_una_entrada_se_prueba_en_su_borde(tamano: int, valido: bool) -> None:
    if valido:
        assert _entrada(tamano=tamano).tamano == tamano
    else:
        with pytest.raises(ManifiestoInvalidoError):
            _entrada(tamano=tamano)


def test_el_constructor_de_una_entrada_valida_el_tipo_de_la_procedencia() -> None:
    with pytest.raises(ManifiestoInvalidoError):
        EntradaDeManifiesto(ruta=RUTA, sha256=H_B, tamano=10, clave_de_cache=H_D, procedencia="x")  # type: ignore[arg-type]


@pytest.mark.parametrize("clave", ["", "no es una huella", "A" * 64, H_D[:-1]])
def test_una_clave_de_cache_mal_formada_o_equivocada_se_rechaza(clave: str) -> None:
    with pytest.raises(ManifiestoInvalidoError):
        EntradaDeManifiesto(ruta=RUTA, sha256=H_B, tamano=10, clave_de_cache=clave, procedencia=_procedencia())


def test_las_rutas_repetidas_se_detectan_ignorando_mayusculas_en_cualquier_orden_crudo() -> None:
    mayuscula, minuscula = _entrada("Textures/A_P.dds"), _entrada("textures/a_p.dds")

    for entradas in ((mayuscula, minuscula), (minuscula, mayuscula)):
        with pytest.raises(ManifiestoInvalidoError, match="repetidas"):
            Manifiesto(entradas=entradas)
        with pytest.raises(ManifiestoInvalidoError, match="repetidas"):
            Manifiesto.ordenado(entradas)


def test_ordenado_ordena_por_la_clave_plegada_y_no_por_la_ruta_cruda() -> None:
    # Crudo: 'B' < 'a'. Plegado: 'a' < 'b'. El orden canónico es el plegado.
    mayuscula, minuscula = _entrada("textures/B_p.dds"), _entrada("textures/a_p.dds")

    manifiesto = Manifiesto.ordenado([mayuscula, minuscula])

    assert [e.ruta for e in manifiesto.entradas] == ["textures/a_p.dds", "textures/B_p.dds"]
    with pytest.raises(ManifiestoInvalidoError):
        Manifiesto(entradas=(mayuscula, minuscula))


def test_ordenado_exige_entradas() -> None:
    with pytest.raises(ManifiestoInvalidoError):
        Manifiesto.ordenado(["no es una entrada"])  # type: ignore[list-item]


def test_el_caracter_de_borrado_en_los_parametros_se_serializa_escapado_y_ida_y_vuelta() -> None:
    manifiesto = _manifiesto(_entrada(parametros=parametros_canonicos({"v": "\x7f"})))

    datos = serializar_manifiesto(manifiesto)

    assert b"\\u007f" in datos
    assert b"\x7f" not in datos
    assert leer_manifiesto(datos) == manifiesto


def test_leer_acepta_un_bytearray() -> None:
    manifiesto = _manifiesto(_entrada())

    assert leer_manifiesto(bytearray(serializar_manifiesto(manifiesto))) == manifiesto


def test_el_tamano_maximo_del_manifiesto_esta_congelado_y_se_prueba_en_su_borde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert output.TAMANO_MAXIMO_DEL_MANIFIESTO == 128 * 1024 * 1024
    datos = serializar_manifiesto(_manifiesto(_entrada()))

    monkeypatch.setattr(output, "TAMANO_MAXIMO_DEL_MANIFIESTO", len(datos))
    assert leer_manifiesto(datos)  # justo en el tope: entra

    monkeypatch.setattr(output, "TAMANO_MAXIMO_DEL_MANIFIESTO", len(datos) - 1)
    with pytest.raises(ManifiestoInvalidoError, match="supera"):
        leer_manifiesto(datos)  # un byte más: se rechaza ANTES de decodificar


def test_la_decodificacion_es_estricta_y_rechaza_el_bom_en_su_propia_capa() -> None:
    # Las capas de abajo (validación de campos, comparación canónica) también rechazarían estos contenidos, así que
    # se distingue la CAPA por el mensaje: un decodificador que tolera o ignora el BOM los dejaría pasar a ellas.
    valido = serializar_manifiesto(_manifiesto(_entrada()))
    with pytest.raises(ManifiestoInvalidoError, match="utf-8"):
        leer_manifiesto(b"\xff" + valido)
    with pytest.raises(ManifiestoInvalidoError, match="BOM"):
        leer_manifiesto(b"\xef\xbb\xbf" + valido)


def test_el_serializador_aplica_el_mismo_tope_que_el_lector(monkeypatch: pytest.MonkeyPatch) -> None:
    manifiesto = _manifiesto(_entrada())
    largo = len(serializar_manifiesto(manifiesto))

    monkeypatch.setattr(output, "TAMANO_MAXIMO_DEL_MANIFIESTO", largo)
    assert leer_manifiesto(serializar_manifiesto(manifiesto)) == manifiesto  # justo en el tope: se escribe y se lee

    monkeypatch.setattr(output, "TAMANO_MAXIMO_DEL_MANIFIESTO", largo - 1)
    with pytest.raises(ManifiestoInvalidoError, match="supera"):
        serializar_manifiesto(manifiesto)
    with pytest.raises(ManifiestoInvalidoError, match="supera"):
        huella_del_manifiesto(manifiesto)  # la huella pasa por el mismo serializador


def test_todo_manifiesto_que_se_puede_escribir_se_puede_leer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Propiedad, no un caso: para CADA tope entre «no cabe nada» y «cabe todo», o el serializador se niega, o lo que
    # escribe lo lee el lector. Nunca un manifiesto que se escribe y después no se puede cargar.
    manifiesto = _manifiesto(_entrada("textures/a_p.dds"), _entrada("textures/b_p.dds"), _entrada("textures/c_p.dds"))
    largo = len(serializar_manifiesto(manifiesto))
    for tope in range(largo - 40, largo + 40):
        monkeypatch.setattr(output, "TAMANO_MAXIMO_DEL_MANIFIESTO", tope)
        try:
            datos = serializar_manifiesto(manifiesto)
        except ManifiestoInvalidoError:
            assert tope < largo
        else:
            assert tope >= largo
            assert leer_manifiesto(datos) == manifiesto


def test_el_tope_real_alcanza_para_un_lote_grande() -> None:
    # Un lote de 50.000 salidas con procedencia mínima tiene que caber: el costo por entrada se mide, no se supone.
    uno = len(serializar_manifiesto(_manifiesto(_entrada("textures/arch/a_p.dds"))))
    dos = len(serializar_manifiesto(_manifiesto(_entrada("textures/arch/a_p.dds"), _entrada("textures/arch/b_p.dds"))))

    assert (dos - uno) * 50_000 < output.TAMANO_MAXIMO_DEL_MANIFIESTO


def test_los_mensajes_distinguen_esquema_y_mod_equivocados() -> None:
    documento = _documento_de_referencia()
    documento["esquema"] = "otro/1"
    with pytest.raises(ManifiestoInvalidoError, match="esquema desconocido"):
        leer_manifiesto(_bytes(documento))

    documento = _documento_de_referencia()
    documento["mod"] = "Otro Mod"
    with pytest.raises(ManifiestoInvalidoError, match="otro mod"):
        leer_manifiesto(_bytes(documento))


def test_una_observacion_con_un_tipo_que_no_es_el_enum_se_rechaza_aunque_no_lleve_huella() -> None:
    with pytest.raises(PlanInvalidoError, match="TipoObservado"):
        Observacion("archivo", None)  # type: ignore[arg-type]


def _accion(ruta: str) -> AccionPlanificada:
    return AccionPlanificada(ruta, AccionDeSalida.CONSTRUIR, MotivoDeSalida.NUEVA, None)


def test_el_plan_exige_acciones_ordenadas_unicas_y_en_tupla() -> None:
    a, b = _accion("textures/a_p.dds"), _accion("textures/b_p.dds")
    assert PlanDeSalida(acciones=(a, b)).acciones == (a, b)

    with pytest.raises(PlanInvalidoError):
        PlanDeSalida(acciones=(b, a))  # fuera de orden
    with pytest.raises(PlanInvalidoError):
        PlanDeSalida(acciones=(a, a))  # repetida
    with pytest.raises(PlanInvalidoError):
        PlanDeSalida(acciones=[a])  # type: ignore[arg-type]
    with pytest.raises(PlanInvalidoError):
        PlanDeSalida(acciones=("no es una acción",))  # type: ignore[arg-type]


def test_el_plan_ordena_y_detecta_repetidas_por_la_clave_plegada() -> None:
    mayuscula, minuscula = _accion("textures/B_p.dds"), _accion("textures/a_p.dds")
    with pytest.raises(PlanInvalidoError):
        PlanDeSalida(acciones=(mayuscula, minuscula))  # crudo: ordenado; plegado: desordenado
    assert PlanDeSalida(acciones=(minuscula, mayuscula)).acciones == (minuscula, mayuscula)
    with pytest.raises(PlanInvalidoError):
        PlanDeSalida(acciones=(_accion("Textures/A_p.dds"), _accion("textures/a_p.dds")))  # misma ruta plegada


def test_la_ruta_del_plan_conserva_la_capitalizacion_de_la_salida_deseada() -> None:
    ruta = "Textures/Muro_P.dds"
    observado = {clave_de_ruta(ruta): Observacion(TipoObservado.AUSENTE, None)}

    plan = planificar([SalidaDeseada(ruta, H_A)], None, observado)

    assert [a.ruta for a in plan.acciones] == [ruta]
    assert plan.acciones[0].accion is AccionDeSalida.CONSTRUIR


def test_una_huerfana_conserva_la_capitalizacion_del_manifiesto() -> None:
    entrada = _entrada("Textures/Huerfana_P.dds", sha256=H_B)
    observado = {clave_de_ruta(entrada.ruta): Observacion(TipoObservado.ARCHIVO, H_B)}

    plan = planificar([], _manifiesto(entrada), observado)

    assert [(a.ruta, a.accion) for a in plan.acciones] == [("Textures/Huerfana_P.dds", AccionDeSalida.RETIRAR)]


# ---------------------------------------------------------------------------
# Anclas del módulo
# ---------------------------------------------------------------------------

_MODULO = Path(output.__file__)
_PAQUETE = Path(__file__).resolve().parents[1] / "sky_claw"


def test_all_esta_congelado() -> None:
    assert set(output.__all__) == {
        "ESQUEMA_DEL_MANIFIESTO",
        "NOMBRE_DEL_MOD_DE_SALIDA",
        "TAMANO_MAXIMO_DEL_ARCHIVO",
        "TAMANO_MAXIMO_DEL_MANIFIESTO",
        "AccionDeSalida",
        "AccionPlanificada",
        "DescarteDePapelera",
        "EncoderUsado",
        "EntradaDeManifiesto",
        "EstadoTrasCorte",
        "Manifiesto",
        "ManifiestoInvalidoError",
        "MotivoDeSalida",
        "NivelDeReproducibilidad",
        "Observacion",
        "OrigenDeEntrada",
        "PlanDeSalida",
        "PlanInvalidoError",
        "Procedencia",
        "ProcedenciaInvalidaError",
        "RecuperacionDeCorte",
        "RutaDeSalidaInvalidaError",
        "SalidaDeseada",
        "SalidaGestionadaError",
        "TipoObservado",
        "clave_de_cache",
        "clave_de_ruta",
        "cota_del_manifiesto_tras_un_corte",
        "crear_entrada",
        "huella_del_manifiesto",
        "leer_manifiesto",
        "parametros_canonicos",
        "planificar",
        "reconciliar_tras_corte",
        "serializar_manifiesto",
        "validar_ruta_de_salida",
    }


def test_el_modulo_es_puro_sus_unicas_importaciones_son_las_congeladas() -> None:
    arbol = ast.parse(_MODULO.read_text(encoding="utf-8"))
    importados: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.update(alias.name.split(".")[0] for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            importados.add(nodo.module.split(".")[0])

    # Nada de ``os``, ``pathlib``, ``subprocess``, ``tempfile``, ``shutil``, ``time``, ``datetime`` ni ``numpy``: leer
    # el disco, la hora o el entorno hace que la misma entrada dé distinta salida.
    assert importados == {"__future__", "collections", "dataclasses", "enum", "hashlib", "json", "math", "re", "typing"}


def test_el_modulo_no_abre_archivos_ni_llama_a_funciones_de_entrada_salida() -> None:
    arbol = ast.parse(_MODULO.read_text(encoding="utf-8"))
    nombres = {
        nodo.func.id if isinstance(nodo.func, ast.Name) else nodo.func.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name | ast.Attribute)
    }

    assert not nombres & {"open", "read_text", "write_text", "read_bytes", "write_bytes", "unlink", "rename", "replace"}


def test_solo_output_conoce_el_nombre_del_mod_de_salida() -> None:
    """El nombre del mod es la identidad de la propiedad: una segunda definición decidiría la propiedad por su cuenta."""
    quienes = {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if NOMBRE_DEL_MOD_DE_SALIDA in ruta.read_text(encoding="utf-8")
    }

    assert quienes == {"sky_claw/local/native_parallax/output.py"}

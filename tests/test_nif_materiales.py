"""NP-A0j: lector de materiales de un NIF de Skyrim (qué shader, qué flags y qué texturas tiene cada forma).

El plugin de relieve automático decide por forma (``BSGeometry``) a partir de tres cosas que viven en el NIF: el tipo de shader, los
flags del shader y si la forma tiene una propiedad de alfa. El lector las extrae SIN interpretar geometría: se apoya en la tabla de
tamaños de bloque de la cabecera y solo abre las formas, sus propiedades de shader/alfa y el juego de texturas.

Los NIFs de estos tests son sintéticos (``tests/_nif_sintetico.py``, escrito desde la especificación pública NifTools): prueban que el
lector concuerda con la especificación, NO que lea un archivo real del juego. Lo real se contrasta aparte (verificación cruzada con
una implementación ajena y la tabla que devuelve el rig).
"""

from __future__ import annotations

import dataclasses
import struct

import pytest

from sky_claw.local.assets.nif_materiales import (
    TOPE_DE_BLOQUES,
    FormaDeNif,
    LecturaDeNif,
    NifError,
    _Lector,
    leer_materiales,
)
from tests._nif_sintetico import (
    Bloque,
    bloque_alfa,
    bloque_forma_bs,
    bloque_forma_ni,
    bloque_juego_de_texturas,
    bloque_nodo,
    bloque_shader_de_efecto,
    bloque_shader_de_iluminacion,
    construir_nif,
)

_TEXTURAS = (
    "textures\\architecture\\whiterun\\muro01.dds",
    "textures\\architecture\\whiterun\\muro01_n.dds",
    "",
    "textures\\architecture\\whiterun\\muro01_p.dds",
)


def _una_forma_bs(**shader: int) -> bytes:
    """Nodo raíz + ``BSTriShape`` -> ``BSLightingShaderProperty`` -> ``BSShaderTextureSet`` (bloques 0..3)."""
    return construir_nif(
        [
            bloque_nodo(hijos=(1,)),
            bloque_forma_bs(nombre=0, shader=2),
            bloque_shader_de_iluminacion(juego_de_texturas=3, **shader),
            bloque_juego_de_texturas(_TEXTURAS),
        ],
        textos=("muro",),
    )


# --- Lo que se lee de una forma ---------------------------------------------------------------------------------------


def test_leer_materiales_devuelve_el_shader_los_flags_y_las_texturas_de_una_forma_de_skyrim_se() -> None:
    lectura = leer_materiales(_una_forma_bs(tipo_de_shader=0, flags_1=0x80400301, flags_2=0x8021))

    assert lectura.version_bs == 100
    assert lectura.formas == (
        FormaDeNif(
            tipo_de_bloque="BSTriShape",
            nombre="muro",
            propiedad_de_shader="BSLightingShaderProperty",
            tipo_de_shader=0,
            flags_1=0x80400301,
            flags_2=0x8021,
            tiene_propiedad_de_alfa=False,
            texturas=_TEXTURAS,
        ),
    )
    assert lectura.formas_no_soportadas == ()


def test_leer_materiales_lee_una_forma_ni_tri_shape_de_skyrim_le_con_la_misma_forma_de_resultado() -> None:
    nif = construir_nif(
        [
            bloque_nodo(hijos=(1,)),
            bloque_forma_ni(nombre=0, shader=2, alfa=4, materiales=1),
            bloque_shader_de_iluminacion(tipo_de_shader=3, flags_1=1 << 23, flags_2=1 << 5, juego_de_texturas=3),
            bloque_juego_de_texturas(_TEXTURAS),
            bloque_alfa(),
        ],
        textos=("pared",),
        version_bs=83,
    )

    (forma,) = leer_materiales(nif).formas

    assert forma.tipo_de_bloque == "NiTriShape"
    assert forma.nombre == "pared"
    assert forma.tipo_de_shader == 3
    assert forma.flags_1 == 1 << 23
    assert forma.flags_2 == 1 << 5
    assert forma.tiene_propiedad_de_alfa is True
    assert forma.texturas == _TEXTURAS
    assert leer_materiales(nif).version_bs == 83


@pytest.mark.parametrize("tipo", ["NiTriStrips", "BSLODTriShape", "BSSegmentedTriShape"])
def test_leer_materiales_trata_las_variantes_de_ni_tri_shape_como_formas(tipo: str) -> None:
    nif = construir_nif(
        [
            bloque_nodo(hijos=(1,)),
            bloque_forma_ni(tipo=tipo, shader=2),
            bloque_shader_de_iluminacion(juego_de_texturas=3),
            bloque_juego_de_texturas(_TEXTURAS),
        ],
        version_bs=83,
    )

    (forma,) = leer_materiales(nif).formas

    assert forma.tipo_de_bloque == tipo
    assert forma.propiedad_de_shader == "BSLightingShaderProperty"


@pytest.mark.parametrize("tipo", ["BSSubIndexTriShape", "BSDynamicTriShape"])
def test_leer_materiales_trata_las_variantes_de_bs_tri_shape_como_formas(tipo: str) -> None:
    nif = construir_nif(
        [
            bloque_nodo(hijos=(1,)),
            bloque_forma_bs(tipo=tipo, shader=2),
            bloque_shader_de_iluminacion(juego_de_texturas=3),
            bloque_juego_de_texturas(_TEXTURAS),
        ]
    )

    (forma,) = leer_materiales(nif).formas

    assert forma.tipo_de_bloque == tipo
    assert forma.propiedad_de_shader == "BSLightingShaderProperty"


def test_leer_materiales_conserva_las_ranuras_vacias_y_su_posicion() -> None:
    (forma,) = leer_materiales(_una_forma_bs()).formas

    assert forma.texturas is not None
    assert forma.texturas[2] == ""
    assert forma.texturas[3].endswith("muro01_p.dds")


def test_leer_materiales_no_confunde_el_nombre_del_shader_con_el_de_la_forma() -> None:
    nif = construir_nif(
        [
            bloque_forma_bs(nombre=0, shader=1),
            bloque_shader_de_iluminacion(nombre=1, juego_de_texturas=2),
            bloque_juego_de_texturas(_TEXTURAS),
        ],
        textos=("forma", "shader"),
        raices=(0,),
    )

    (forma,) = leer_materiales(nif).formas

    assert forma.nombre == "forma"


def test_leer_materiales_acepta_una_forma_sin_nombre() -> None:
    nif = construir_nif([bloque_forma_bs(nombre=-1)])

    (forma,) = leer_materiales(nif).formas

    assert forma.nombre == ""


def test_leer_materiales_salta_los_datos_de_extra_data_de_la_forma_y_de_la_propiedad() -> None:
    nif = construir_nif(
        [
            bloque_forma_bs(shader=1),
            bloque_shader_de_iluminacion(tipo_de_shader=0, flags_2=1 << 5, juego_de_texturas=2, extra=(7, 8, 9)),
            bloque_juego_de_texturas(_TEXTURAS),
        ]
    )

    (forma,) = leer_materiales(nif).formas

    assert forma.flags_2 == 1 << 5
    assert forma.texturas == _TEXTURAS


# --- Formas sin propiedad de iluminación -------------------------------------------------------------------------------


def test_leer_materiales_marca_una_forma_sin_propiedad_de_shader() -> None:
    (forma,) = leer_materiales(construir_nif([bloque_forma_bs(shader=-1)])).formas

    assert forma.propiedad_de_shader is None
    assert forma.tipo_de_shader is None
    assert forma.flags_1 is None
    assert forma.flags_2 is None
    assert forma.texturas is None


def test_leer_materiales_no_interpreta_un_shader_de_efecto_como_de_iluminacion() -> None:
    nif = construir_nif([bloque_forma_bs(shader=1), bloque_shader_de_efecto()])

    (forma,) = leer_materiales(nif).formas

    assert forma.propiedad_de_shader == "BSEffectShaderProperty"
    assert forma.tipo_de_shader is None
    assert forma.flags_1 is None
    assert forma.texturas is None


def test_leer_materiales_nombra_el_tipo_de_cualquier_otra_propiedad_a_la_que_apunte_la_forma() -> None:
    nif = construir_nif([bloque_forma_bs(shader=1), bloque_nodo()])

    (forma,) = leer_materiales(nif).formas

    assert forma.propiedad_de_shader == "NiNode"
    assert forma.tipo_de_shader is None


def test_leer_materiales_una_forma_con_shader_de_iluminacion_sin_juego_de_texturas_tiene_texturas_none() -> None:
    nif = construir_nif([bloque_forma_bs(shader=1), bloque_shader_de_iluminacion(juego_de_texturas=-1)])

    (forma,) = leer_materiales(nif).formas

    assert forma.tipo_de_shader == 0
    assert forma.texturas is None


def test_leer_materiales_una_propiedad_de_alfa_cuenta_aunque_no_tenga_flags_de_mezcla_ni_de_prueba() -> None:
    nif = construir_nif(
        [
            bloque_forma_bs(shader=1, alfa=3),
            bloque_shader_de_iluminacion(juego_de_texturas=2),
            bloque_juego_de_texturas(_TEXTURAS),
            bloque_alfa(flags=0, umbral=0),
        ]
    )

    (forma,) = leer_materiales(nif).formas

    assert forma.tiene_propiedad_de_alfa is True


def test_leer_materiales_sin_referencia_de_alfa_la_forma_no_tiene_propiedad_de_alfa() -> None:
    (forma,) = leer_materiales(_una_forma_bs()).formas

    assert forma.tiene_propiedad_de_alfa is False


def test_leer_materiales_lee_varias_formas_en_el_orden_de_los_bloques() -> None:
    nif = construir_nif(
        [
            bloque_nodo(hijos=(1, 2)),
            bloque_forma_bs(nombre=0, shader=3),
            bloque_forma_bs(nombre=1, shader=4),
            bloque_shader_de_iluminacion(tipo_de_shader=0, juego_de_texturas=5),
            bloque_shader_de_iluminacion(tipo_de_shader=3, juego_de_texturas=5),
            bloque_juego_de_texturas(_TEXTURAS),
        ],
        textos=("a", "b"),
    )

    formas = leer_materiales(nif).formas

    assert [(f.nombre, f.tipo_de_shader) for f in formas] == [("a", 0), ("b", 3)]


def test_leer_materiales_un_nif_sin_formas_devuelve_una_lectura_vacia() -> None:
    lectura = leer_materiales(construir_nif([bloque_nodo()]))

    assert lectura.formas == ()
    assert lectura.formas_no_soportadas == ()


def test_leer_materiales_declara_las_formas_que_no_sabe_leer_en_vez_de_ignorarlas() -> None:
    nif = construir_nif(
        [
            Bloque("NiParticleSystem", b"\x00" * 16),
            Bloque("NiLines", b"\x00" * 16),
            Bloque("BSFutureTriShape", b"\x00" * 16),
            Bloque("BSFadeNode", b"\x00" * 16),
            Bloque("bhkBoxShape", b"\x00" * 16),
        ]
    )

    assert leer_materiales(nif).formas_no_soportadas == ("BSFutureTriShape", "NiLines", "NiParticleSystem")


def test_la_lectura_y_la_forma_son_inmutables() -> None:
    lectura = leer_materiales(_una_forma_bs())

    with pytest.raises(dataclasses.FrozenInstanceError):
        lectura.version_bs = 1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        lectura.formas[0].tipo_de_shader = 1  # type: ignore[misc]


def test_leer_materiales_es_determinista() -> None:
    nif = _una_forma_bs(tipo_de_shader=0, flags_1=5, flags_2=6)

    assert leer_materiales(nif) == leer_materiales(nif)
    assert isinstance(leer_materiales(nif), LecturaDeNif)


# --- Falla cerrada ------------------------------------------------------------------------------------------------------


def test_leer_materiales_rechaza_un_archivo_que_no_es_un_nif() -> None:
    with pytest.raises(NifError, match="cabecera"):
        leer_materiales(b"DDS |" + b"\x00" * 200)


def test_leer_materiales_rechaza_una_cabecera_que_no_es_la_de_la_version_20_2_0_7() -> None:
    nif = construir_nif([bloque_forma_bs()], cabecera=b"Gamebryo File Format, Version 20.2.0.5\n")

    with pytest.raises(NifError, match="cabecera"):
        leer_materiales(nif)


def test_leer_materiales_rechaza_una_version_numerica_distinta_de_la_de_la_cabecera() -> None:
    with pytest.raises(NifError, match="versión"):
        leer_materiales(construir_nif([bloque_forma_bs()], version=0x14020005))


def test_leer_materiales_rechaza_big_endian() -> None:
    with pytest.raises(NifError, match="endian"):
        leer_materiales(construir_nif([bloque_forma_bs()], little_endian=0))


@pytest.mark.parametrize("version_bs", [34, 130, 155])
def test_leer_materiales_rechaza_una_version_de_bethesda_que_no_es_skyrim(version_bs: int) -> None:
    with pytest.raises(NifError, match="Bethesda"):
        leer_materiales(construir_nif([bloque_forma_bs()], version_bs=version_bs))


@pytest.mark.parametrize("recorte", [10, 45, 80, 120])
def test_leer_materiales_rechaza_un_archivo_truncado_en_la_cabecera(recorte: int) -> None:
    nif = _una_forma_bs()

    with pytest.raises(NifError):
        leer_materiales(nif[:recorte])


def test_leer_materiales_rechaza_un_archivo_truncado_en_los_bloques() -> None:
    nif = _una_forma_bs()

    with pytest.raises(NifError, match="bloques"):
        leer_materiales(nif[:-40])


def test_leer_materiales_rechaza_un_bloque_mas_corto_que_lo_que_el_lector_necesita() -> None:
    corto = Bloque("BSTriShape", b"\x00" * 10)

    with pytest.raises(NifError, match="BSTriShape"):
        leer_materiales(construir_nif([corto]))


def test_leer_materiales_rechaza_una_referencia_a_un_bloque_inexistente() -> None:
    with pytest.raises(NifError, match="referencia"):
        leer_materiales(construir_nif([bloque_forma_bs(shader=9)]))


def test_leer_materiales_rechaza_un_indice_de_texto_fuera_de_la_tabla() -> None:
    with pytest.raises(NifError, match="texto"):
        leer_materiales(construir_nif([bloque_forma_bs(nombre=5)], textos=("solo",)))


def test_leer_materiales_rechaza_un_juego_de_texturas_con_cantidad_absurda() -> None:
    cuerpo = struct.pack("<I", 0xFFFFFFFF)
    nif = construir_nif(
        [
            bloque_forma_bs(shader=1),
            bloque_shader_de_iluminacion(juego_de_texturas=2),
            Bloque("BSShaderTextureSet", cuerpo),
        ]
    )

    with pytest.raises(NifError, match="ranuras de textura"):
        leer_materiales(nif)


def test_leer_materiales_rechaza_una_cantidad_de_bloques_por_encima_del_tope_sin_reservar_memoria() -> None:
    nif = bytearray(construir_nif([bloque_forma_bs()]))
    cabecera = b"Gamebryo File Format, Version 20.2.0.7\n"
    desplazamiento = len(cabecera) + 4 + 1 + 4  # versión, endian, versión de usuario
    nif[desplazamiento : desplazamiento + 4] = struct.pack("<I", TOPE_DE_BLOQUES + 1)

    with pytest.raises(NifError, match="supera el tope"):
        leer_materiales(bytes(nif))


def test_leer_materiales_rechaza_tamanos_de_bloque_que_se_pasan_del_archivo() -> None:
    nif = bytearray(construir_nif([bloque_forma_bs()]))
    posicion = bytes(nif).find(struct.pack("<I", len(bloque_forma_bs().cuerpo)))
    nif[posicion : posicion + 4] = struct.pack("<I", 0x7FFFFFFF)

    with pytest.raises(NifError, match="bloques"):
        leer_materiales(bytes(nif))


def test_leer_materiales_rechaza_un_tipo_de_bloque_fuera_de_la_tabla() -> None:
    nif = bytearray(construir_nif([bloque_forma_bs()]))
    posicion = bytes(nif).rfind(struct.pack("<H", 0))  # el índice de tipo del único bloque
    # busca el índice exacto: queda justo antes de la tabla de tamaños (una entrada de 4 bytes)
    tamano = struct.pack("<I", len(bloque_forma_bs().cuerpo))
    inicio_tamanos = bytes(nif).find(tamano)
    nif[inicio_tamanos - 2 : inicio_tamanos] = struct.pack("<H", 99)
    assert posicion >= 0

    with pytest.raises(NifError, match="tipo"):
        leer_materiales(bytes(nif))


def test_leer_materiales_rechaza_datos_que_no_son_bytes() -> None:
    with pytest.raises(NifError):
        leer_materiales("no son bytes")  # type: ignore[arg-type]


# --- Límites exactos (cada comprobación se prueba de los dos lados) -----------------------------------------------------


def test_leer_materiales_acepta_exactamente_el_tope_de_bloques() -> None:
    nif = construir_nif([Bloque("NiNode", b"")] * TOPE_DE_BLOQUES)

    assert leer_materiales(nif).formas == ()


def test_leer_materiales_acepta_un_juego_de_texturas_con_exactamente_el_tope_de_ranuras_y_rechaza_una_mas() -> None:
    def con(cantidad: int) -> bytes:
        return construir_nif(
            [
                bloque_forma_bs(shader=1),
                bloque_shader_de_iluminacion(juego_de_texturas=2),
                bloque_juego_de_texturas(tuple(f"textures\\a\\{i}.dds" for i in range(cantidad))),
            ]
        )

    (forma,) = leer_materiales(con(16)).formas
    assert forma.texturas is not None
    assert len(forma.texturas) == 16
    with pytest.raises(NifError, match="ranuras de textura"):
        leer_materiales(con(17))


def test_leer_materiales_acepta_un_archivo_sin_pie_porque_el_lector_no_lo_necesita() -> None:
    nif = construir_nif([bloque_forma_bs(nombre=-1)], con_pie=False)

    assert len(leer_materiales(nif).formas) == 1


def test_leer_materiales_salta_los_grupos_de_la_cabecera() -> None:
    nif = construir_nif([bloque_forma_bs(shader=1), bloque_shader_de_iluminacion(tipo_de_shader=3)], grupos=(1, 2, 3))

    (forma,) = leer_materiales(nif).formas

    assert forma.tipo_de_shader == 3


def test_leer_materiales_una_referencia_al_primer_bloque_es_valida_y_una_al_indice_n_no() -> None:
    primero = construir_nif([bloque_shader_de_iluminacion(tipo_de_shader=3), bloque_forma_bs(shader=0)])
    fuera = construir_nif([bloque_forma_bs(shader=1)])

    assert leer_materiales(primero).formas[0].tipo_de_shader == 3
    with pytest.raises(NifError, match="referencia"):
        leer_materiales(fuera)


def test_leer_materiales_un_indice_de_texto_igual_al_tamano_de_la_tabla_esta_fuera() -> None:
    assert leer_materiales(construir_nif([bloque_forma_bs(nombre=0)], textos=("a",))).formas[0].nombre == "a"
    with pytest.raises(NifError, match="texto"):
        leer_materiales(construir_nif([bloque_forma_bs(nombre=1)], textos=("a",)))


def test_leer_materiales_un_indice_de_tipo_igual_a_la_cantidad_de_tipos_esta_fuera_de_la_tabla() -> None:
    nif = bytearray(construir_nif([bloque_forma_bs()]))
    tamano = struct.pack("<I", len(bloque_forma_bs().cuerpo))
    inicio_tamanos = bytes(nif).find(tamano)
    nif[inicio_tamanos - 2 : inicio_tamanos] = struct.pack("<H", 1)  # hay un solo tipo: el índice 1 ya no existe

    with pytest.raises(NifError, match="tipo"):
        leer_materiales(bytes(nif))


def test_leer_materiales_acepta_el_bit_alto_de_physx_en_el_indice_de_tipo() -> None:
    nif = bytearray(construir_nif([bloque_forma_bs()]))
    tamano = struct.pack("<I", len(bloque_forma_bs().cuerpo))
    inicio_tamanos = bytes(nif).find(tamano)
    nif[inicio_tamanos - 2 : inicio_tamanos] = struct.pack("<H", 0x8000)

    assert len(leer_materiales(bytes(nif)).formas) == 1


def test_leer_materiales_una_cabecera_cortada_justo_despues_de_las_tablas_falla_por_datos_insuficientes() -> None:
    nif = construir_nif([bloque_forma_bs()])
    tamano = struct.pack("<I", len(bloque_forma_bs().cuerpo))
    fin_de_tablas = bytes(nif).find(tamano) + 4

    with pytest.raises(NifError, match="datos insuficientes"):
        leer_materiales(nif[:fin_de_tablas])


def test_leer_materiales_la_lista_de_extra_data_de_la_forma_se_salta_entera() -> None:
    nif = construir_nif(
        [bloque_forma_bs(shader=1, extra=(5, 6)), bloque_shader_de_iluminacion(tipo_de_shader=3)],
    )
    nif_ni = construir_nif(
        [bloque_forma_ni(shader=1, extra=(5, 6)), bloque_shader_de_iluminacion(tipo_de_shader=3)], version_bs=83
    )

    assert leer_materiales(nif).formas[0].tipo_de_shader == 3
    assert leer_materiales(nif_ni).formas[0].tipo_de_shader == 3


def test_leer_materiales_el_juego_de_texturas_debe_ser_un_juego_de_texturas() -> None:
    nif = construir_nif(
        [bloque_forma_bs(shader=1), bloque_shader_de_iluminacion(juego_de_texturas=2), bloque_nodo()],
    )

    with pytest.raises(NifError, match="juego de texturas"):
        leer_materiales(nif)


# --- El cursor acotado --------------------------------------------------------------------------------------------------


def test_el_lector_puede_saltar_exactamente_lo_que_queda_y_no_un_byte_mas() -> None:
    lector = _Lector(b"abcdef", 0, 6, "prueba")
    lector.saltar(6)
    assert lector.restante == 0
    with pytest.raises(NifError, match="datos insuficientes"):
        _Lector(b"abcdef", 0, 6, "prueba").saltar(7)


def test_el_lector_no_lee_mas_alla_de_su_zona_aunque_el_buffer_tenga_mas_datos() -> None:
    with pytest.raises(NifError, match="datos insuficientes"):
        _Lector(b"\x01\x00\x00\x00\x02\x00\x00\x00", 0, 4, "prueba").saltar(5)
    with pytest.raises(NifError, match="datos insuficientes"):
        _Lector(b"\x01\x00\x00\x00\x02\x00\x00\x00", 2, 4, "prueba").u32()


def test_el_lector_lee_un_texto_de_exactamente_lo_que_queda_y_rechaza_uno_mas_largo() -> None:
    assert _Lector(b"abc", 0, 3, "prueba").texto(3) == "abc"
    with pytest.raises(NifError, match="texto de longitud inválida"):
        _Lector(b"abc", 0, 3, "prueba").texto(4)


def test_el_lector_rechaza_un_texto_por_encima_del_tope_aunque_haya_datos_de_sobra() -> None:
    datos = b"x" * 5000

    assert len(_Lector(datos, 0, 5000, "prueba").texto(4096)) == 4096
    with pytest.raises(NifError, match="texto de longitud inválida"):
        _Lector(datos, 0, 5000, "prueba").texto(4097)

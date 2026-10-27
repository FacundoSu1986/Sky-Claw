"""NP-A0j: la regla con la que el plugin de relieve automático decide sobre cada forma de una malla.

La regla se transcribe del código fuente público del plugin (``src/XSEPlugin.cpp`` de ``doodlum/skyrim-auto-parallax``, rama ``main``
descargada el 2026-10-27, SHA-256 ``c70f6e21e2cc2c5241b711db358963bda5ee3bb297a9f05fbd52cab2a4a188a3``). Lo que NO está verificado es
que la versión instalada en un rig (v1-0-6-0 en el del operador) se comporte igual que esa rama: la prueba en el juego lo confirma o
lo desmiente con el log del plugin.

Qué se verifica, enumerando y no muestreando:

- el veredicto de TODOS los tipos de shader de ``BSLightingShaderType`` (0–19): solo el 0 entra a la rama que enciende relieve y solo
  el 3 a la que lo apaga;
- cada exclusión por separado y el conjunto completo de exclusiones, congelado por igualdad literal;
- el orden en que el plugin busca el ``_p`` (altura, difuso, normal) y la derivación de la ruta, incluida la sensibilidad a
  mayúsculas del original, que se declara ``ambigua`` en vez de adivinarse.
"""

from __future__ import annotations

import dataclasses

import pytest

from sky_claw.local.assets.auto_parallax_regla import (
    BIT_VERTEX_COLORS,
    EXCLUSIONES_DE_LA_RAMA_QUE_ENCIENDE,
    CandidatoDeRelieve,
    Candidatos,
    Decision,
    EstadoDeLaBusqueda,
    Motivo,
    OrigenDelCandidato,
    ResultadoDeLaBusqueda,
    Veredicto,
    buscar_el_relieve,
    candidatos_de_relieve,
    evaluar_forma,
)
from sky_claw.local.assets.nif_materiales import FormaDeNif

_TEXTURAS = (
    "textures\\a\\muro01.dds",
    "textures\\a\\muro01_n.dds",
    "",
    "",
)

_F1_PROJECTED_UV = 1 << 23
_F1_SKINNED = 1 << 1
_F2_LOD_OBJECTS = 1 << 2
_F2_VERTEX_COLORS = 1 << 5
_F2_VERTEX_LIGHTING = 1 << 10
_F2_TREE_ANIM = 1 << 29


def _forma(
    *,
    tipo_de_shader: int | None = 0,
    flags_1: int = 0,
    flags_2: int = _F2_VERTEX_COLORS,
    alfa: bool = False,
    texturas: tuple[str, ...] | None = _TEXTURAS,
    propiedad: str | None = "BSLightingShaderProperty",
) -> FormaDeNif:
    lighting = propiedad == "BSLightingShaderProperty"
    return FormaDeNif(
        tipo_de_bloque="BSTriShape",
        nombre="forma",
        propiedad_de_shader=propiedad,
        tipo_de_shader=tipo_de_shader if lighting else None,
        flags_1=flags_1 if lighting else None,
        flags_2=flags_2 if lighting else None,
        tiene_propiedad_de_alfa=alfa,
        texturas=texturas if lighting else None,
    )


# --- El veredicto de la forma -----------------------------------------------------------------------------------------


def test_evaluar_forma_default_con_colores_de_vertice_y_sin_exclusiones_es_elegible() -> None:
    assert evaluar_forma(_forma()) == Decision(Veredicto.ELEGIBLE, frozenset())


@pytest.mark.parametrize("tipo", range(20))
def test_evaluar_forma_enumera_los_tipos_de_shader_solo_el_0_enciende_y_solo_el_3_es_de_fabrica(tipo: int) -> None:
    decision = evaluar_forma(_forma(tipo_de_shader=tipo))

    if tipo == 0:
        assert decision.veredicto is Veredicto.ELEGIBLE
    elif tipo == 3:
        assert decision == Decision(Veredicto.PARALLAX_DE_FABRICA, frozenset())
    else:
        assert decision == Decision(Veredicto.NO_ACTUA, frozenset({Motivo.TIPO_DE_SHADER_NO_ADMITIDO}))


def test_evaluar_forma_con_shader_parallax_es_de_fabrica_aunque_tenga_las_exclusiones_de_la_otra_rama() -> None:
    decision = evaluar_forma(_forma(tipo_de_shader=3, flags_1=_F1_PROJECTED_UV | _F1_SKINNED, alfa=True))

    assert decision == Decision(Veredicto.PARALLAX_DE_FABRICA, frozenset())


def test_evaluar_forma_sin_colores_de_vertice_se_excluye_por_ese_motivo() -> None:
    assert evaluar_forma(_forma(flags_2=0)) == Decision(Veredicto.EXCLUIDA, frozenset({Motivo.SIN_VERTEX_COLORS}))


@pytest.mark.parametrize(
    ("flags_1", "flags_2", "motivo"),
    [
        (_F1_PROJECTED_UV, 0, Motivo.PROJECTED_UV),
        (_F1_SKINNED, 0, Motivo.SKINNED),
        (0, _F2_LOD_OBJECTS, Motivo.LOD_OBJECTS),
        (0, _F2_TREE_ANIM, Motivo.TREE_ANIM),
        (0, _F2_VERTEX_LIGHTING, Motivo.VERTEX_LIGHTING),
    ],
)
def test_evaluar_forma_cada_flag_de_exclusion_la_excluye_por_si_solo(
    flags_1: int, flags_2: int, motivo: Motivo
) -> None:
    decision = evaluar_forma(_forma(flags_1=flags_1, flags_2=flags_2 | _F2_VERTEX_COLORS))

    assert decision == Decision(Veredicto.EXCLUIDA, frozenset({motivo}))


def test_evaluar_forma_junta_todos_los_motivos_de_exclusion_y_no_solo_el_primero() -> None:
    decision = evaluar_forma(
        _forma(
            flags_1=_F1_PROJECTED_UV | _F1_SKINNED,
            flags_2=_F2_LOD_OBJECTS | _F2_TREE_ANIM | _F2_VERTEX_LIGHTING,
            alfa=True,
            texturas=None,
        )
    )

    assert decision.veredicto is Veredicto.EXCLUIDA
    assert decision.motivos == frozenset(
        {
            Motivo.PROJECTED_UV,
            Motivo.SKINNED,
            Motivo.LOD_OBJECTS,
            Motivo.TREE_ANIM,
            Motivo.VERTEX_LIGHTING,
            Motivo.SIN_VERTEX_COLORS,
            Motivo.CON_PROPIEDAD_DE_ALFA,
            Motivo.SIN_JUEGO_DE_TEXTURAS,
        }
    )


def test_evaluar_forma_una_propiedad_de_alfa_excluye_aunque_no_tenga_mezcla_ni_prueba() -> None:
    assert evaluar_forma(_forma(alfa=True)) == Decision(Veredicto.EXCLUIDA, frozenset({Motivo.CON_PROPIEDAD_DE_ALFA}))


def test_evaluar_forma_sin_juego_de_texturas_se_excluye() -> None:
    assert evaluar_forma(_forma(texturas=None)) == Decision(
        Veredicto.EXCLUIDA, frozenset({Motivo.SIN_JUEGO_DE_TEXTURAS})
    )


def test_evaluar_forma_un_shader_de_efecto_no_es_tocado_por_el_plugin() -> None:
    decision = evaluar_forma(_forma(propiedad="BSEffectShaderProperty"))

    assert decision == Decision(Veredicto.NO_ACTUA, frozenset({Motivo.SHADER_NO_DE_ILUMINACION}))


def test_evaluar_forma_sin_propiedad_de_shader_no_es_tocada_por_el_plugin() -> None:
    decision = evaluar_forma(_forma(propiedad=None))

    assert decision == Decision(Veredicto.NO_ACTUA, frozenset({Motivo.SIN_PROPIEDAD_DE_SHADER}))


def test_evaluar_forma_una_forma_de_iluminacion_sin_flags_es_un_error_de_quien_la_construyo() -> None:
    sin_flags = dataclasses.replace(_forma(), flags_1=None, flags_2=None)

    with pytest.raises(ValueError, match="flags"):
        evaluar_forma(sin_flags)


def test_evaluar_forma_los_flags_de_otras_ramas_no_excluyen() -> None:
    # Specular (f1 bit 0), Double_Sided (f2 bit 4), HD_LOD_Objects (f2 bit 31) y LOD_Landscape (f2 bit 1) no figuran en la regla.
    decision = evaluar_forma(_forma(flags_1=1, flags_2=_F2_VERTEX_COLORS | (1 << 4) | (1 << 31) | (1 << 1)))

    assert decision.veredicto is Veredicto.ELEGIBLE


def test_las_exclusiones_estan_congeladas_por_igualdad_literal() -> None:
    assert EXCLUSIONES_DE_LA_RAMA_QUE_ENCIENDE == {
        Motivo.PROJECTED_UV: (1, 23),
        Motivo.SKINNED: (1, 1),
        Motivo.LOD_OBJECTS: (2, 2),
        Motivo.TREE_ANIM: (2, 29),
        Motivo.VERTEX_LIGHTING: (2, 10),
    }
    assert BIT_VERTEX_COLORS == (2, 5)


def test_el_vocabulario_de_motivos_y_de_veredictos_esta_cerrado() -> None:
    assert {m.name for m in Motivo} == {
        "PROJECTED_UV",
        "SKINNED",
        "LOD_OBJECTS",
        "TREE_ANIM",
        "VERTEX_LIGHTING",
        "SIN_VERTEX_COLORS",
        "CON_PROPIEDAD_DE_ALFA",
        "SIN_JUEGO_DE_TEXTURAS",
        "TIPO_DE_SHADER_NO_ADMITIDO",
        "SHADER_NO_DE_ILUMINACION",
        "SIN_PROPIEDAD_DE_SHADER",
    }
    assert {v.name for v in Veredicto} == {"ELEGIBLE", "PARALLAX_DE_FABRICA", "NO_ACTUA", "EXCLUIDA"}


def test_la_decision_es_inmutable() -> None:
    decision = evaluar_forma(_forma())

    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.veredicto = Veredicto.EXCLUIDA  # type: ignore[misc]


# --- Qué `_p` busca el plugin -------------------------------------------------------------------------------------------


def test_candidatos_buscan_primero_la_ranura_de_altura_despues_el_difuso_y_al_final_la_normal() -> None:
    resultado = candidatos_de_relieve(
        ("textures\\a\\muro01.dds", "textures\\a\\muro01_n.dds", "", "textures\\a\\alto.dds")
    )

    assert resultado == Candidatos(
        candidatos=(
            CandidatoDeRelieve(OrigenDelCandidato.ALTURA, "textures\\a\\alto.dds"),
            CandidatoDeRelieve(OrigenDelCandidato.DIFUSO, "textures\\a\\muro01_p.dds"),
            CandidatoDeRelieve(OrigenDelCandidato.NORMAL, "textures\\a\\muro01_p.dds"),
        ),
        ambiguo=False,
    )


def test_candidatos_sin_ranura_de_altura_empiezan_por_el_difuso() -> None:
    resultado = candidatos_de_relieve(_TEXTURAS)

    assert [c.origen for c in resultado.candidatos] == [OrigenDelCandidato.DIFUSO, OrigenDelCandidato.NORMAL]


def test_candidatos_un_difuso_con_sufijo_d_pierde_el_sufijo_al_derivar() -> None:
    resultado = candidatos_de_relieve(("textures\\a\\muro01_d.dds", "", "", ""))

    assert resultado.candidatos == (CandidatoDeRelieve(OrigenDelCandidato.DIFUSO, "textures\\a\\muro01_p.dds"),)


def test_candidatos_una_normal_sin_sufijo_n_solo_cambia_la_extension() -> None:
    resultado = candidatos_de_relieve(("", "textures\\a\\muro01_nm.dds", "", ""))

    assert resultado.candidatos == (CandidatoDeRelieve(OrigenDelCandidato.NORMAL, "textures\\a\\muro01_nm_p.dds"),)


def test_candidatos_ignoran_las_ranuras_vacias_y_las_que_no_son_dds() -> None:
    resultado = candidatos_de_relieve(("textures\\a\\muro01.tga", "", "", ""))

    assert resultado == Candidatos(candidatos=(), ambiguo=False)


def test_candidatos_con_exactamente_tres_ranuras_no_tienen_altura_ni_fallan() -> None:
    resultado = candidatos_de_relieve(("textures\\a\\muro01.dds", "textures\\a\\muro01_n.dds", ""))

    assert [c.origen for c in resultado.candidatos] == [OrigenDelCandidato.DIFUSO, OrigenDelCandidato.NORMAL]


@pytest.mark.parametrize("texturas", [(), ("textures\\a\\muro01.dds",)])
def test_candidatos_con_menos_ranuras_que_difuso_y_normal_no_fallan(texturas: tuple[str, ...]) -> None:
    resultado = candidatos_de_relieve(texturas)

    assert len(resultado.candidatos) == len(texturas)


def test_candidatos_una_altura_fuera_de_la_carpeta_textures_es_ambigua() -> None:
    assert candidatos_de_relieve(("", "", "", "alto.dds")).ambiguo is True


def test_candidatos_una_ruta_de_menos_de_seis_caracteres_no_se_razona_y_se_declara_ambigua() -> None:
    assert candidatos_de_relieve(("a.dds", "", "", "")) == Candidatos(
        candidatos=(CandidatoDeRelieve(OrigenDelCandidato.DIFUSO, "", ambigua=True),), ambiguo=True
    )


def test_candidatos_marcan_la_ambiguedad_candidato_por_candidato() -> None:
    resultado = candidatos_de_relieve(("", "textures\\a\\muro01_N.DDS", "", "textures\\a\\alto.dds"))

    assert [(c.origen, c.ambigua) for c in resultado.candidatos] == [
        (OrigenDelCandidato.ALTURA, False),
        (OrigenDelCandidato.NORMAL, True),
    ]
    assert resultado.ambiguo is True


def test_candidatos_una_ruta_de_exactamente_seis_caracteres_si_se_deriva() -> None:
    resultado = candidatos_de_relieve(("ab.dds",))

    # se deriva (no es «demasiado corta»), pero no está bajo textures\\: queda ambigua por eso
    assert resultado.candidatos == (CandidatoDeRelieve(OrigenDelCandidato.DIFUSO, "ab_p.dds", ambigua=True),)


def test_candidatos_con_menos_de_cuatro_ranuras_no_tienen_altura() -> None:
    resultado = candidatos_de_relieve(("textures\\a\\muro01.dds", "textures\\a\\muro01_n.dds"))

    assert [c.origen for c in resultado.candidatos] == [OrigenDelCandidato.DIFUSO, OrigenDelCandidato.NORMAL]


@pytest.mark.parametrize(
    "texturas",
    [
        ("", "textures\\a\\muro01_N.dds", "", ""),  # el original distingue mayúsculas y el motor puede no hacerlo
        ("textures\\a\\muro01_D.dds", "", "", ""),
        ("textures\\a\\muro01.DDS", "", "", ""),
        ("muro01.dds", "", "", ""),  # sin la carpeta textures\\
    ],
)
def test_candidatos_declaran_ambigua_una_ruta_cuyo_resultado_depende_de_las_mayusculas_o_de_la_carpeta(
    texturas: tuple[str, ...],
) -> None:
    assert candidatos_de_relieve(texturas).ambiguo is True


def test_candidatos_una_ruta_en_minusculas_con_barras_invertidas_no_es_ambigua() -> None:
    assert candidatos_de_relieve(_TEXTURAS).ambiguo is False


def test_candidatos_las_mayusculas_que_no_cambian_la_derivacion_no_hacen_ambigua_la_ruta() -> None:
    resultado = candidatos_de_relieve(("Textures\\Architecture\\Muro01.dds", "", "", ""))

    assert resultado == Candidatos(
        candidatos=(CandidatoDeRelieve(OrigenDelCandidato.DIFUSO, "Textures\\Architecture\\Muro01_p.dds"),),
        ambiguo=False,
    )


# --- La búsqueda completa -----------------------------------------------------------------------------------------------


def _existe(*presentes: str, desconocidas: tuple[str, ...] = ()):
    def comprobar(ruta: str) -> bool | None:
        if ruta in desconocidas:
            return None
        return ruta in presentes

    return comprobar


def test_buscar_el_relieve_devuelve_el_primer_candidato_que_existe_y_no_mira_los_siguientes() -> None:
    mirados: list[str] = []

    def existe(ruta: str) -> bool | None:
        mirados.append(ruta)
        return ruta == "textures\\a\\alto.dds"

    resultado = buscar_el_relieve(
        ("textures\\a\\muro01.dds", "textures\\a\\muro01_n.dds", "", "textures\\a\\alto.dds"), existe=existe
    )

    assert resultado == ResultadoDeLaBusqueda(
        ruta="textures\\a\\alto.dds", origen=OrigenDelCandidato.ALTURA, estado=EstadoDeLaBusqueda.ENCUENTRA
    )
    assert mirados == ["textures\\a\\alto.dds"]


def test_buscar_el_relieve_cae_al_difuso_si_la_altura_no_existe() -> None:
    resultado = buscar_el_relieve(
        ("textures\\a\\muro01.dds", "textures\\a\\muro01_n.dds", "", "textures\\a\\alto.dds"),
        existe=_existe("textures\\a\\muro01_p.dds"),
    )

    assert resultado.estado is EstadoDeLaBusqueda.ENCUENTRA
    assert resultado.origen is OrigenDelCandidato.DIFUSO
    assert resultado.ruta == "textures\\a\\muro01_p.dds"


def test_buscar_el_relieve_sin_ningun_candidato_existente_no_encuentra() -> None:
    resultado = buscar_el_relieve(_TEXTURAS, existe=_existe())

    assert resultado == ResultadoDeLaBusqueda(ruta=None, origen=None, estado=EstadoDeLaBusqueda.NO_ENCUENTRA)


def test_buscar_el_relieve_si_un_candidato_anterior_no_se_puede_comprobar_no_afirma_nada_mas_alla() -> None:
    resultado = buscar_el_relieve(
        _TEXTURAS,
        existe=_existe("textures\\a\\muro01_p.dds", desconocidas=("textures\\a\\muro01_p.dds",)),
    )

    assert resultado.estado is EstadoDeLaBusqueda.NO_VERIFICADO
    assert resultado.ruta is None


def test_buscar_el_relieve_una_ruta_ambigua_no_se_afirma() -> None:
    resultado = buscar_el_relieve(("textures\\a\\muro01.DDS", "", "", ""), existe=_existe())

    assert resultado.estado is EstadoDeLaBusqueda.NO_VERIFICADO


def test_buscar_el_relieve_no_llama_a_existe_con_rutas_vacias() -> None:
    mirados: list[str] = []

    def existe(ruta: str) -> bool | None:
        mirados.append(ruta)
        return False

    buscar_el_relieve(("", "", "", ""), existe=existe)

    assert mirados == []


# --- La ambigüedad solo importa si la búsqueda llega hasta ella (P2 de Codex en #819) ---------------------------------------


def test_buscar_el_relieve_una_altura_que_existe_corta_la_busqueda_antes_de_una_normal_ambigua() -> None:
    mirados: list[str] = []

    def existe(ruta: str) -> bool | None:
        mirados.append(ruta)
        return ruta == "textures\\a\\alto.dds"

    resultado = buscar_el_relieve(("", "textures\\a\\muro01_N.DDS", "", "textures\\a\\alto.dds"), existe=existe)

    assert resultado == ResultadoDeLaBusqueda(
        ruta="textures\\a\\alto.dds", origen=OrigenDelCandidato.ALTURA, estado=EstadoDeLaBusqueda.ENCUENTRA
    )
    assert mirados == ["textures\\a\\alto.dds"]


def test_buscar_el_relieve_si_la_altura_no_existe_y_la_siguiente_es_ambigua_no_se_afirma_nada() -> None:
    mirados: list[str] = []

    def existe(ruta: str) -> bool | None:
        mirados.append(ruta)
        return False

    resultado = buscar_el_relieve(("", "textures\\a\\muro01_N.DDS", "", "textures\\a\\alto.dds"), existe=existe)

    assert resultado.estado is EstadoDeLaBusqueda.NO_VERIFICADO
    assert mirados == ["textures\\a\\alto.dds"]  # no se consulta la ruta ambigua: su derivación depende del motor


def test_buscar_el_relieve_un_candidato_firme_anterior_a_uno_ambiguo_que_existe_decide() -> None:
    resultado = buscar_el_relieve(
        ("textures\\a\\muro01.dds", "textures\\a\\muro02_N.DDS", "", ""),
        existe=_existe("textures\\a\\muro01_p.dds"),
    )

    assert resultado.estado is EstadoDeLaBusqueda.ENCUENTRA
    assert resultado.origen is OrigenDelCandidato.DIFUSO

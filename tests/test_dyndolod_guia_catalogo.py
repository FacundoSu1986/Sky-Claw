"""#997 — ancla (a): el catálogo de la guía de la etapa 9 se enumera, no se muestrea.

La propiedad: *ninguna precondición, fase o corte del modo asistido llega al operador sin su texto en criollo y su
arreglo en una línea*. Por eso todo se congela por igualdad literal: agregar un miembro a un enum sin escribirle su
ficha rompe este archivo, y cambiar la severidad o el override de una ficha también.

Decisión del propietario (FASE 1 de #997, 2026-11-23): ❌ solo para fallas seguras o de seguridad y **sin override**;
lo incierto es ⚠️. El único override de la etapa 9 sigue siendo el de etapas previas (D-3), que no vive acá.
"""

from __future__ import annotations

import re

import pytest

from sky_claw.local.tools import dyndolod_guia as guia
from sky_claw.local.tools.dyndolod_guia import (
    BOTONES_POR_HERRAMIENTA,
    CATALOGO,
    EXPLICACION_POR_RAZON_UIA,
    EXPLICACION_POR_RESOLUCION,
    TEXTOS_DE_FASE,
    Estado,
    FaseAsistida,
    IdPrecondicion,
    ResultadoDePrecondicion,
    Severidad,
)
from sky_claw.local.tools.dyndolod_uia_gate import ResolucionProtocoloReadiness
from sky_claw.local.tools.dyndolod_uia_preflight import RazonPreflight

#: Un código técnico en el título es justo lo que #997 saca del cartel principal (``ERROR_UIA``, ``PROCESO_TERMINO``…).
_CODIGO_TECNICO = re.compile(r"\b[A-Z]{2,}_[A-Z_]{2,}\b")


# ---------------------------------------------------------------------------
# Precondiciones
# ---------------------------------------------------------------------------


def test_el_catalogo_de_precondiciones_esta_congelado_por_igualdad_literal() -> None:
    """Severidad máxima y override de cada ficha. Una ficha nueva, o un cambio de política, rompe acá."""
    assert {
        ficha_id.value: (ficha.severidad_maxima, ficha.admite_override) for ficha_id, ficha in CATALOGO.items()
    } == {
        "dyndolod_encontrado": (Severidad.BLOQUEA, False),
        "texgen_encontrado": (Severidad.BLOQUEA, False),
        "herramientas_en_carpeta_permitida": (Severidad.BLOQUEA, False),
        "herramientas_fuera_del_juego": (Severidad.BLOQUEA, False),
        "salida_vieja_desactivada": (Severidad.BLOQUEA, False),
        "etapas_previas": (Severidad.AVISO, False),
        "output_del_preset": (Severidad.AVISO, False),
        "dll_ng_y_masters": (Severidad.AVISO, False),
    }


def test_todo_id_del_enum_tiene_su_ficha_y_ninguna_ficha_sobra() -> None:
    assert set(IdPrecondicion) == set(CATALOGO)
    for ficha_id, ficha in CATALOGO.items():
        assert ficha.id is ficha_id, f"la ficha de {ficha_id} declara otro id"


def test_ninguna_ficha_admite_override() -> None:
    """Decisión del propietario: un ❌ es una falla segura o de seguridad; saltearlo no cambia el desenlace."""
    assert not [ficha.id for ficha in CATALOGO.values() if ficha.admite_override]


@pytest.mark.parametrize("ficha_id", list(IdPrecondicion), ids=lambda f: f.value)
def test_cada_ficha_dice_que_pasa_y_que_hacer_en_una_linea_en_criollo(ficha_id: IdPrecondicion) -> None:
    ficha = CATALOGO[ficha_id]
    for texto in (ficha.titulo, ficha.arreglo):
        assert texto.strip(), f"{ficha_id}: texto vacío"
        assert "\n" not in texto, f"{ficha_id}: el operador lee UNA línea, no un párrafo"
        assert len(texto) <= 160, f"{ficha_id}: demasiado largo para un cartel ({len(texto)} caracteres)"
    assert not _CODIGO_TECNICO.search(ficha.titulo), f"{ficha_id}: el título tiene un código técnico"


@pytest.mark.parametrize("ficha_id", list(IdPrecondicion), ids=lambda f: f.value)
def test_cada_ficha_tiene_su_texto_de_ok_y_su_tema_neutro(ficha_id: IdPrecondicion) -> None:
    """Review Codex en #1007: una fila ✓ decía «No encuentro DynDOLOD». Cada estado necesita SU texto."""
    ficha = CATALOGO[ficha_id]
    for texto in (ficha.titulo_ok, ficha.tema):
        assert texto.strip()
        assert "\n" not in texto
        assert not _CODIGO_TECNICO.search(texto)
    assert ficha.titulo_ok != ficha.titulo


@pytest.mark.parametrize("ficha_id", list(IdPrecondicion), ids=lambda f: f.value)
def test_el_titulo_depende_del_estado_y_nunca_afirma_una_falla_que_no_ocurrio(ficha_id: IdPrecondicion) -> None:
    ficha = CATALOGO[ficha_id]
    ok = guia.resultado(ficha_id, Estado.OK)
    sin_revisar = guia.resultado(ficha_id, Estado.NO_VERIFICABLE, detalle="x")
    falla = guia.resultado(ficha_id, Estado.FALLA, detalle="x")

    assert ok.titulo == ficha.titulo_ok
    assert sin_revisar.titulo == f"No pude revisar {ficha.tema}."
    assert falla.titulo == guia._rellenar(ficha.titulo, {})  # noqa: SLF001
    assert ficha.titulo.split(":")[0] not in ok.titulo
    assert ficha.titulo.split(":")[0] not in sin_revisar.titulo


def test_un_resultado_no_puede_ser_mas_severo_que_su_ficha() -> None:
    with pytest.raises(ValueError, match="severidad"):
        ResultadoDePrecondicion(
            id=IdPrecondicion.ETAPAS_PREVIAS, estado=Estado.FALLA, severidad=Severidad.BLOQUEA, detalle_tecnico="x"
        )


def test_solo_una_falla_bloqueante_bloquea() -> None:
    """``NO_VERIFICABLE`` no bloquea: cada ❌ refleja un corte fail-closed que sigue existiendo aguas abajo."""
    bloqueante = guia.resultado(IdPrecondicion.DYNDOLOD_ENCONTRADO, Estado.FALLA, detalle="sin ruta")
    no_verificable = guia.resultado(IdPrecondicion.DYNDOLOD_ENCONTRADO, Estado.NO_VERIFICABLE, detalle="doble")
    aviso = guia.resultado(IdPrecondicion.DLL_NG_Y_MASTERS, Estado.FALLA, detalle="dll")

    assert bloqueante.bloquea
    assert not no_verificable.bloquea
    assert not aviso.bloquea


# ---------------------------------------------------------------------------
# Fases del modo asistido
# ---------------------------------------------------------------------------


def test_las_fases_del_asistido_estan_congeladas_y_todas_tienen_texto() -> None:
    assert [fase.value for fase in FaseAsistida] == [
        "abriendo_ventana",
        "esperando_tu_aprobacion",
        "verificando_output",
        "podes_apretar_start",
        "herramienta_termino",
        "cortada",
    ]
    assert set(TEXTOS_DE_FASE) == set(FaseAsistida)


@pytest.mark.parametrize("fase", [FaseAsistida.ESPERANDO_TU_APROBACION, FaseAsistida.VERIFICANDO_OUTPUT])
@pytest.mark.parametrize("herramienta", ["TexGen", "DynDOLOD"])
def test_mientras_sky_claw_verifica_el_texto_dice_que_no_toques_el_boton_de_arranque(
    fase: FaseAsistida, herramienta: str
) -> None:
    """#6: el corte del rig fue apretar Start antes del aviso «Output verificado»."""
    texto = guia.CambioDeFase(tool_name=herramienta, fase=fase, esperado="C:/rig/out").texto()
    boton = BOTONES_POR_HERRAMIENTA[herramienta].inicio
    assert f"NO toques «{boton}»" in texto


@pytest.mark.parametrize("herramienta", ["TexGen", "DynDOLOD"])
def test_el_aviso_de_arranque_nombra_el_boton_real_de_cada_herramienta(herramienta: str) -> None:
    texto = guia.CambioDeFase(tool_name=herramienta, fase=FaseAsistida.PODES_APRETAR_START).texto()
    botones = BOTONES_POR_HERRAMIENTA[herramienta]
    assert f"«{botones.inicio}»" in texto
    assert f"«{botones.salida}»" in texto


def test_los_botones_salen_del_contrato_de_ui_medido() -> None:
    """Una sola fuente: los nombres los midió el rig (contrato UIA), no un literal de la guía."""
    assert {tool: (b.previo, b.inicio, b.salida) for tool, b in BOTONES_POR_HERRAMIENTA.items()} == {
        "TexGen": (None, "Start", "Exit TexGen"),
        "DynDOLOD": ("Advanced >>>", "OK", "Save and Exit"),
    }


def test_al_abrir_texgen_avisa_del_cartel_de_stitched() -> None:
    """#8: el modal de stitched tapaba la ventana y el gate reintentaba sin decirle nada al operador."""
    texto = guia.CambioDeFase(tool_name="TexGen", fase=FaseAsistida.ABRIENDO_VENTANA).texto()
    assert "stitched" in texto
    assert "Ignore" in texto


def test_al_pedir_la_aprobacion_muestra_la_ruta_exacta_del_output() -> None:
    texto = guia.CambioDeFase(
        tool_name="TexGen", fase=FaseAsistida.ESPERANDO_TU_APROBACION, esperado=r"G:\rig\ext\DynDOLOD\TexGen"
    ).texto()
    assert r"G:\rig\ext\DynDOLOD\TexGen" in texto


# ---------------------------------------------------------------------------
# Explicaciones de los cortes (criollo primero, técnico en el desplegable)
# ---------------------------------------------------------------------------


def test_todo_desenlace_de_corte_del_protocolo_tiene_explicacion() -> None:
    """``APROBADA`` no es un corte: es la única exclusión, y se declara acá en vez de quedar afuera en silencio."""
    assert set(EXPLICACION_POR_RESOLUCION) == set(ResolucionProtocoloReadiness) - {
        ResolucionProtocoloReadiness.APROBADA
    }


def test_toda_razon_de_corte_del_gate_uia_tiene_explicacion() -> None:
    """``OUTPUT_COINCIDE`` es el MATCH: no corta nunca. Una razón nueva del gate sin texto rompe acá."""
    assert set(EXPLICACION_POR_RAZON_UIA) == set(RazonPreflight) - {RazonPreflight.OUTPUT_COINCIDE}


@pytest.mark.parametrize(
    "explicacion",
    [*EXPLICACION_POR_RESOLUCION.values(), *EXPLICACION_POR_RAZON_UIA.values()],
)
def test_las_explicaciones_no_tienen_codigos_y_entran_en_una_linea(explicacion: guia.Explicacion) -> None:
    for texto in (explicacion.titulo, explicacion.arreglo):
        assert texto.strip()
        assert "\n" not in texto
        assert not _CODIGO_TECNICO.search(texto)


def test_cerrar_la_ventana_antes_del_aviso_se_explica_como_eso() -> None:
    """#6 en el rig: ``PROCESO_TERMINO`` / ``ERROR_UIA`` mientras Sky-Claw verificaba ⇒ se tocó Start o se cerró."""
    for corte in (ResolucionProtocoloReadiness.PROCESO_TERMINO, RazonPreflight.ERROR_UIA):
        explicada = guia.explicar_fallo(
            mensaje_tecnico="protocolo de readiness de DynDOLOD cortó la corrida: proceso_termino",
            fase=FaseAsistida.VERIFICANDO_OUTPUT,
            herramienta="DynDOLOD",
            corte=corte,
        )
        assert "antes de tiempo" in explicada["titulo"]
        assert "«OK»" in explicada["arreglo"], "nombra el botón real de DynDOLOD"
        assert explicada["detalle_tecnico"].startswith("protocolo de readiness"), "el técnico queda en el desplegable"


def test_un_cartel_tapando_la_ventana_se_explica_con_el_boton_ignore() -> None:
    explicada = guia.explicar_fallo(
        mensaje_tecnico="preflight UIA de Output bloqueó a TexGen: UNKNOWN (CONTROL_NO_ENCONTRADO)",
        fase=FaseAsistida.ABRIENDO_VENTANA,
        herramienta="TexGen",
        corte=RazonPreflight.CONTROL_NO_ENCONTRADO,
    )
    assert "Ignore" in explicada["arreglo"]
    assert not _CODIGO_TECNICO.search(explicada["titulo"])


def test_sin_corte_conocido_la_explicacion_es_generica_y_conserva_el_tecnico() -> None:
    explicada = guia.explicar_fallo(mensaje_tecnico="algo raro: XYZ_ABC", fase=None, herramienta=None, corte=None)
    assert explicada["titulo"]
    assert explicada["arreglo"]
    assert explicada["detalle_tecnico"] == "algo raro: XYZ_ABC"

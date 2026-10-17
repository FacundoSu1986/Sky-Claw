"""Contratos de UI por build para la etapa 9 desatendida (#661 P2/P4).

Las anclas congelan por igualdad literal lo que el rig midió (P0-A/P0-B,
``docs/validation/2026-10-02_p0_uia_alpha209/``): agregar un build sin medirlo,
cambiar un selector o un mecanismo de escritura rompe el test a propósito.
"""

from __future__ import annotations

import dataclasses

import pytest

from sky_claw.local.tools.dyndolod_uia_contrato import (
    CONTRATOS_DE_UI,
    MARCAS_DEL_DIALOGO_DE_TAREA,
    AccionDeUI,
    CodigoDesatendido,
    ContextoDeAccion,
    ContratoDeUIHerramienta,
    FirmaDeDialogo,
    MecanismoDeEscritura,
    ModalConocido,
    ModoDeInteraccion,
    PoliticaDeModal,
    modo_de_interaccion_desde,
    resolver_contrato,
)
from sky_claw.local.tools.dyndolod_uia_preflight import SELECTORES_DE_OUTPUT, CriteriosDeControl

SHA_TEXGEN_ALPHA209 = "0939bc8f8cbae2e1b38f17d56fecd1b7a941dade0554e944886bd7f5c4807a62"
SHA_DYNDOLOD_ALPHA209 = "b67625eb7815111ba9ac232c626ff88b07bb64ff30ff5183c5a04b5f045c3bd0"


def test_los_modos_de_interaccion_estan_congelados():
    assert {modo.value for modo in ModoDeInteraccion} == {"asistido", "desatendido"}


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("asistido", ModoDeInteraccion.ASISTIDO),
        ("desatendido", ModoDeInteraccion.DESATENDIDO),
        (ModoDeInteraccion.DESATENDIDO, ModoDeInteraccion.DESATENDIDO),
    ],
)
def test_el_modo_se_parsea_solo_desde_valores_del_contrato(crudo, esperado):
    assert modo_de_interaccion_desde(crudo) is esperado


@pytest.mark.parametrize("crudo", ["unattended", "DESATENDIDO", "", None, True, 1, "auto"])
def test_un_modo_desconocido_falla_cerrado(crudo):
    # Ni alias en inglés ni mayúsculas: un modo que no está en el enum no se
    # adivina, porque adivinar "desatendido" sería mutar la GUI sin pedirlo.
    with pytest.raises(ValueError):
        modo_de_interaccion_desde(crudo)


def test_los_codigos_de_error_son_estables_y_machine_readable():
    assert {codigo.value for codigo in CodigoDesatendido} == {
        "UNATTENDED_CAPABILITY_UNAVAILABLE",
        "UNATTENDED_BUILD_UNSUPPORTED",
        "UNATTENDED_CONFIGURATION_NOT_APPROVED",
        "PRESET_ABSENT",
        "PRESET_MISMATCH",
        "PRESET_RESTORE_FAILED",
        "UNEXPECTED_MODAL",
        "KNOWN_MODAL_NOT_APPROVED",
        "UIA_UNAVAILABLE",
        "UIA_CONTROL_MAP_MISMATCH",
        "OUTPUT_WRITE_UNSUPPORTED",
        "OUTPUT_READBACK_MISMATCH",
        "BEGIN_NOT_ENABLED",
        "BEGIN_OUTCOME_UNKNOWN",
        "BEGIN_NOT_CONFIRMED",
        "PROCESS_DIED_PRE_BEGIN",
        "UNEXPECTED_DIALOG_DURING_GENERATION",
        "TERMINAL_CONTROL_MISMATCH",
        "TERMINAL_OUTCOME_UNKNOWN",
        "UNATTENDED_CONTRACT_VIOLATION",
    }
    # El nombre del miembro ES el valor: un log estructurado y un test pueden
    # usar cualquiera de los dos sin traducción.
    assert all(codigo.name == codigo.value for codigo in CodigoDesatendido)


def test_las_marcas_del_dialogo_de_tarea_son_las_medidas():
    assert MARCAS_DEL_DIALOGO_DE_TAREA == {
        "tipo_de_control": "Window",
        "class_name": "#32770",
        "automation_id_instruccion": "MainInstruction",
        "class_name_boton": "CCPushButton",
    }


def test_el_registro_contiene_exactamente_los_builds_medidos():
    assert set(CONTRATOS_DE_UI) == {
        ("TexGen", SHA_TEXGEN_ALPHA209),
        ("DynDOLOD", SHA_DYNDOLOD_ALPHA209),
    }


def test_contrato_texgen_alpha209_congelado():
    contrato = CONTRATOS_DE_UI[("TexGen", SHA_TEXGEN_ALPHA209)]
    assert contrato == ContratoDeUIHerramienta(
        tool="TexGen",
        exe_sha256=SHA_TEXGEN_ALPHA209,
        version="3.0.0.209",
        modo_de_juego="SSE",
        clase_ventana_principal="TfrmMain",
        selector_output=CriteriosDeControl(tipo_de_control="Edit", class_name="TEdit"),
        mecanismo_output=MecanismoDeEscritura.LEGACY_IACCESSIBLE,
        accion_previa=None,
        accion_inicio=AccionDeUI(
            id="texgen_start",
            selector=CriteriosDeControl(tipo_de_control="Button", class_name="TButton", nombre="Start"),
            contexto=ContextoDeAccion.VENTANA_PRINCIPAL,
        ),
        dialogo_terminal=FirmaDeDialogo(
            instruccion="Exit TexGen, zip and exit, check log or restart?",
            botones=frozenset(
                {
                    ("Exit TexGen", "CommandButton_7"),
                    ("Zip and Exit", "CommandButton_11"),
                    ("Check log", "CommandButton_2"),
                    ("Restart", "CommandButton_4"),
                }
            ),
        ),
        accion_terminal=AccionDeUI(
            id="texgen_exit",
            selector=CriteriosDeControl(
                tipo_de_control="Button",
                class_name="CCPushButton",
                nombre="Exit TexGen",
                automation_id="CommandButton_7",
            ),
            contexto=ContextoDeAccion.DIALOGO,
        ),
        modales_conocidos=(
            ModalConocido(
                id="texgen_stitched_lod_previo",
                firma=FirmaDeDialogo(
                    instruccion=(
                        "Found stitched object LOD textures from earlier TexGen generation installed in game folder."
                    ),
                    botones=frozenset({("Ignore", "CommandButton_5"), ("Exit TexGen", "CommandButton_3")}),
                ),
                accion_continuar=AccionDeUI(
                    id="texgen_modal_ignore",
                    selector=CriteriosDeControl(
                        tipo_de_control="Button",
                        class_name="CCPushButton",
                        nombre="Ignore",
                        automation_id="CommandButton_5",
                    ),
                    contexto=ContextoDeAccion.DIALOGO,
                ),
                politica=PoliticaDeModal.APROBACION_DEL_OPERADOR,
                descripcion=contrato.modales_conocidos[0].descripcion,
            ),
        ),
        preset_relativo=("Edit Scripts", "DynDOLOD", "Presets", "DynDOLOD_SSE_TexGen.ini"),
        seccion_preset="TexGen",
        clave_preset_de_calidad=None,
        evidencia="docs/validation/2026-10-02_p0_uia_alpha209/p0b/final-report-p0b.md",
    )


def test_contrato_dyndolod_alpha209_congelado():
    contrato = CONTRATOS_DE_UI[("DynDOLOD", SHA_DYNDOLOD_ALPHA209)]
    assert contrato == ContratoDeUIHerramienta(
        tool="DynDOLOD",
        exe_sha256=SHA_DYNDOLOD_ALPHA209,
        version="3.0.0.209",
        modo_de_juego="SSE",
        clase_ventana_principal="TfrmMain",
        selector_output=CriteriosDeControl(tipo_de_control="Edit", class_name="TEdit"),
        mecanismo_output=MecanismoDeEscritura.LEGACY_IACCESSIBLE,
        accion_previa=AccionDeUI(
            id="dyndolod_advanced",
            selector=CriteriosDeControl(tipo_de_control="Button", class_name="TButton", nombre="Advanced >>>"),
            contexto=ContextoDeAccion.VENTANA_PRINCIPAL,
        ),
        accion_inicio=AccionDeUI(
            id="dyndolod_begin_ok",
            selector=CriteriosDeControl(tipo_de_control="Button", class_name="TButton", nombre="OK"),
            contexto=ContextoDeAccion.VENTANA_PRINCIPAL,
        ),
        dialogo_terminal=FirmaDeDialogo(
            instruccion="Save DynDOLOD plugins, save plugins and zip output, exit DynDOLOD without saving, check the log?",
            botones=frozenset(
                {
                    ("Save and Exit", "CommandButton_6"),
                    ("Save, Zip and Exit", "CommandButton_11"),
                    ("Exit DynDOLOD", "CommandButton_7"),
                    ("Check log", "CommandButton_2"),
                }
            ),
        ),
        accion_terminal=AccionDeUI(
            id="dyndolod_save_exit",
            selector=CriteriosDeControl(
                tipo_de_control="Button",
                class_name="CCPushButton",
                nombre="Save and Exit",
                automation_id="CommandButton_6",
            ),
            contexto=ContextoDeAccion.DIALOGO,
        ),
        modales_conocidos=(
            ModalConocido(
                id="dyndolod_dll_ng_ausente",
                firma=FirmaDeDialogo(
                    instruccion="DynDOLOD.DLL from DynDOLOD DLL NG and Scripts not found!",
                    botones=frozenset({("Ignore", "CommandButton_5"), ("Exit DynDOLOD", "CommandButton_3")}),
                ),
                accion_continuar=AccionDeUI(
                    id="dyndolod_modal_ignore",
                    selector=CriteriosDeControl(
                        tipo_de_control="Button",
                        class_name="CCPushButton",
                        nombre="Ignore",
                        automation_id="CommandButton_5",
                    ),
                    contexto=ContextoDeAccion.DIALOGO,
                ),
                politica=PoliticaDeModal.APROBACION_DEL_OPERADOR,
                descripcion=contrato.modales_conocidos[0].descripcion,
            ),
        ),
        preset_relativo=("Edit Scripts", "DynDOLOD", "Presets", "DynDOLOD_SSE_Default.ini"),
        seccion_preset="DynDOLOD",
        clave_preset_de_calidad="Preset",
        evidencia="docs/validation/2026-10-02_p0_uia_alpha209/p0b/final-report-p0b.md",
    )


@pytest.mark.parametrize("clave", sorted(CONTRATOS_DE_UI))
def test_el_selector_de_output_del_contrato_es_el_del_gate_read_only(clave):
    # Una sola fuente del selector medido: si el gate read-only y el driver
    # mutante divergieran, el driver escribiría sobre un control que el gate
    # nunca certificó.
    contrato = CONTRATOS_DE_UI[clave]
    assert contrato.selector_output == SELECTORES_DE_OUTPUT[contrato.tool]


@pytest.mark.parametrize("clave", sorted(CONTRATOS_DE_UI))
def test_las_acciones_terminales_nunca_son_variantes_zip_ni_salida_sin_guardar(clave):
    contrato = CONTRATOS_DE_UI[clave]
    nombre = contrato.accion_terminal.selector.nombre or ""
    assert "zip" not in nombre.lower()
    assert nombre != "Exit DynDOLOD"
    # La acción terminal tiene que ser uno de los botones de la firma terminal.
    par = (nombre, contrato.accion_terminal.selector.automation_id)
    assert par in contrato.dialogo_terminal.botones


@pytest.mark.parametrize("clave", sorted(CONTRATOS_DE_UI))
def test_el_boton_de_continuar_de_cada_modal_pertenece_a_su_firma(clave):
    for modal in CONTRATOS_DE_UI[clave].modales_conocidos:
        par = (modal.accion_continuar.selector.nombre, modal.accion_continuar.selector.automation_id)
        assert par in modal.firma.botones
        assert modal.accion_continuar.contexto is ContextoDeAccion.DIALOGO
        assert modal.descripcion.strip()


@pytest.mark.parametrize("clave", sorted(CONTRATOS_DE_UI))
def test_las_firmas_de_modal_y_terminal_no_se_confunden(clave):
    # El modal de arranque de TexGen comparte "Exit TexGen" con el terminal: la
    # instrucción exacta es lo que los separa (lección de la sonda P0-B).
    contrato = CONTRATOS_DE_UI[clave]
    for modal in contrato.modales_conocidos:
        assert modal.firma.instruccion != contrato.dialogo_terminal.instruccion
        assert not contrato.dialogo_terminal.coincide(modal.firma.instruccion, modal.firma.botones)
        assert not modal.firma.coincide(contrato.dialogo_terminal.instruccion, contrato.dialogo_terminal.botones)


def test_los_ids_de_accion_son_unicos_en_todo_el_registro():
    ids = []
    for contrato in CONTRATOS_DE_UI.values():
        ids.append(contrato.accion_inicio.id)
        ids.append(contrato.accion_terminal.id)
        if contrato.accion_previa is not None:
            ids.append(contrato.accion_previa.id)
        ids.extend(modal.accion_continuar.id for modal in contrato.modales_conocidos)
    assert len(ids) == len(set(ids))


def test_la_firma_exige_instruccion_exacta_y_botones_requeridos():
    firma = FirmaDeDialogo(instruccion="Hola", botones=frozenset({("A", "CommandButton_1")}))
    assert firma.coincide("Hola", [("A", "CommandButton_1"), ("Cerrar", "")])
    assert not firma.coincide("Hola ", [("A", "CommandButton_1")])
    assert not firma.coincide("hola", [("A", "CommandButton_1")])
    assert not firma.coincide("Hola", [("A", "CommandButton_9")])
    assert not firma.coincide(None, [("A", "CommandButton_1")])


@pytest.mark.parametrize(
    ("tool", "sha", "modo"),
    [
        ("TexGen", SHA_TEXGEN_ALPHA209, "SSE"),
        ("DynDOLOD", SHA_DYNDOLOD_ALPHA209, "SSE"),
        ("TexGen", SHA_TEXGEN_ALPHA209.upper(), "SSE"),
    ],
)
def test_resolver_contrato_por_tool_sha_y_modo(tool, sha, modo):
    contrato = resolver_contrato(tool, sha, modo)
    assert contrato is not None
    assert contrato.tool == tool


@pytest.mark.parametrize(
    ("tool", "sha", "modo"),
    [
        # Hash desconocido (otro build): sin contrato.
        ("TexGen", "0" * 64, "SSE"),
        # El hash de TexGen bajo la tool DynDOLOD: la clave es el PAR.
        ("DynDOLOD", SHA_TEXGEN_ALPHA209, "SSE"),
        # El modo VR no se midió: el mismo binario con -tes5vr no tiene contrato.
        ("TexGen", SHA_TEXGEN_ALPHA209, "TES5VR"),
        # Tool ajena.
        ("xEdit", SHA_TEXGEN_ALPHA209, "SSE"),
        # Hash malformado.
        ("TexGen", "abc", "SSE"),
    ],
)
def test_resolver_contrato_falla_cerrado(tool, sha, modo):
    assert resolver_contrato(tool, sha, modo) is None


def test_un_contrato_con_sha_malformado_no_se_puede_construir():
    contrato = CONTRATOS_DE_UI[("TexGen", SHA_TEXGEN_ALPHA209)]
    with pytest.raises(ValueError):
        dataclasses.replace(contrato, exe_sha256="XYZ")


def test_un_contrato_con_accion_de_inicio_en_un_dialogo_no_se_puede_construir():
    # El Begin estructural vive en la ventana contractual, NUNCA en un #32770.
    contrato = CONTRATOS_DE_UI[("DynDOLOD", SHA_DYNDOLOD_ALPHA209)]
    inicio_en_dialogo = AccionDeUI(
        id="otro",
        selector=contrato.accion_inicio.selector,
        contexto=ContextoDeAccion.DIALOGO,
    )
    with pytest.raises(ValueError):
        dataclasses.replace(contrato, accion_inicio=inicio_en_dialogo)


def test_un_contrato_con_terminal_fuera_del_dialogo_no_se_puede_construir():
    contrato = CONTRATOS_DE_UI[("TexGen", SHA_TEXGEN_ALPHA209)]
    terminal_en_ventana = AccionDeUI(
        id="otro",
        selector=contrato.accion_terminal.selector,
        contexto=ContextoDeAccion.VENTANA_PRINCIPAL,
    )
    with pytest.raises(ValueError):
        dataclasses.replace(contrato, accion_terminal=terminal_en_ventana)


def test_el_registro_no_se_puede_mutar_en_runtime():
    with pytest.raises(TypeError):
        CONTRATOS_DE_UI[("TexGen", "f" * 64)] = CONTRATOS_DE_UI[("TexGen", SHA_TEXGEN_ALPHA209)]  # type: ignore[index]

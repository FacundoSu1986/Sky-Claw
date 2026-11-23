"""Contratos cabeza-llave de BodySlide — argv, capacidad -q, veredicto estricto.

Tests contra el módulo PURO ``sky_claw/local/tools/bodyslide_headless.py``.

Las cinco reglas del contrato autónomo (todos falsos antes de este PR):
- ``-q`` sólo con capacidad verificada; sin verificación el spawn se rechaza.
- ``-p`` sólo con preset EXPLICITAMENTE verificado, y nunca como ruta XML.
- Sin ``-b``/``-f``/``-regex``/``-conformaddons``/``-preview`` en el argv.
- Veredicto estricto conjunta rc, log, manifiesto y destino.
- Serialización determinista de elecciones explícitas de BuildSelection.
"""

from __future__ import annotations

import pytest

from sky_claw.local.tools.bodyslide_headless import (
    ConflictoDeSalida,
    EvidenciaDeBuild,
    HeadlessContractError,
    ManifiestoArtefactos,
    capability_quiet_permitida,
    construir_argv,
    evaluar_veredicto,
    marcadores_de_corrida,
    resolver_conflictos,
    serializar_build_selection,
    validar_conflictos,
)

EXE = "C:/tools/BodySlide/BodySlide.exe"


def test_argv_con_quiet_verificado_y_preset_verificado() -> None:
    argv = construir_argv(EXE, "CBBE", "C:/salida", preset="Zeroed Sliders", preset_verified=True, quiet_verified=True)
    assert argv == [EXE, "-gbuild", "CBBE", "-t", "C:/salida", "-p", "Zeroed Sliders", "-tri", "-q"]


def test_argv_sin_quiet_no_emite_q() -> None:
    argv = construir_argv(EXE, "CBBE", "C:/salida", quiet_verified=False)
    assert "-q" not in argv


def test_preset_sin_verificacion_se_rechaza() -> None:
    with pytest.raises(HeadlessContractError, match="preset"):
        construir_argv(EXE, "CBBE", "C:/salida", preset="Zeroed Sliders", preset_verified=False)


def test_preset_como_ruta_xml_se_rechaza_aun_verificado() -> None:
    with pytest.raises(HeadlessContractError, match="nombre"):
        construir_argv(
            EXE,
            "CBBE",
            "C:/salida",
            preset="C:/ruta/a/preset.xml",
            preset_verified=True,
        )


def test_preset_como_path_pregunta_se_rechaza() -> None:
    with pytest.raises(HeadlessContractError):
        construir_argv(EXE, "CBBE", "C:/salida", preset="preset.xml?Name", preset_verified=True)


def test_preset_vacio_se_rechaza() -> None:
    with pytest.raises(HeadlessContractError):
        construir_argv(EXE, "CBBE", "C:/salida", preset="  ", preset_verified=True)


@pytest.mark.parametrize("group", ["", "   ", "..", ".", "a/b", "a\\b", "a\x00b"])
def test_group_invalido_se_rechaza(group: str) -> None:
    with pytest.raises(HeadlessContractError):
        construir_argv(EXE, group, "C:/salida")


def test_target_con_nul_se_rechaza() -> None:
    with pytest.raises(HeadlessContractError, match="NUL"):
        construir_argv(EXE, "CBBE", "C:/salida\x00evil")


def test_target_vacio_se_rechaza() -> None:
    with pytest.raises(HeadlessContractError):
        construir_argv(EXE, "CBBE", "  ")


def test_build_morphs_false_omite_tri() -> None:
    argv = construir_argv(EXE, "CBBE", "C:/salida", build_morphs=False, quiet_verified=True)
    assert "-tri" not in argv
    assert "-q" in argv


def test_argv_no_emite_flags_no_autorizados() -> None:
    """Los flags declarados upstream pero no autorizados en el contrato no aparecen."""
    argv = construir_argv(
        EXE,
        "CBBE",
        "C:/salida",
        preset="Zeroed Sliders",
        preset_verified=True,
        quiet_verified=True,
    )
    for flag in ("-b", "-f", "-regex", "-conformaddons", "-preview", "-o"):
        assert flag not in argv, f"{flag} no está autorizado en el contrato headless"


def test_argv_sin_preset_sin_quiet_emite_solo_camino_basico() -> None:
    argv = construir_argv(EXE, "CBBE", "C:/salida")
    assert argv == [EXE, "-gbuild", "CBBE", "-t", "C:/salida", "-tri"]


def test_capability_quiet_permitida_requiere_verified() -> None:
    assert capability_quiet_permitida(True) is True
    assert capability_quiet_permitida(False) is False
    assert capability_quiet_permitida(None) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# BuildSelection — elección explícita y serialización determinista
# --------------------------------------------------------------------------- #


def test_serializar_build_selection_determinista() -> None:
    sel = {"meshes/femalebody.nif": "Outfit B", "meshes/femalebody_2.nif": "Outfit A"}
    t1 = serializar_build_selection(sel)
    t2 = serializar_build_selection(sel)
    assert t1 == t2
    assert t1.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<BuildSelection>" in t1
    # orden de salida: ordenado por path, no por orden de inserción
    assert t1.index("meshes/femalebody.nif") < t1.index("meshes/femalebody_2.nif")


def test_serializar_build_selection_con_zaps_ordenados() -> None:
    texto = serializar_build_selection({}, zap_selecciones={("proj", "zap"): True})
    assert '<ZapChoice project="proj" zap="zap" choice="1"' in texto
    texto2 = serializar_build_selection({}, zap_selecciones={("proj", "zap"): False})
    assert '<ZapChoice project="proj" zap="zap" choice="0"' in texto2


@pytest.mark.parametrize("bad", ["", "  "])
def test_serializar_build_selection_rechaza_path_vacio(bad: str) -> None:
    with pytest.raises(HeadlessContractError):
        serializar_build_selection({bad: "Outfit"})


def test_serializar_build_selection_rechaza_choice_vacio() -> None:
    with pytest.raises(HeadlessContractError):
        serializar_build_selection({"meshes/femalebody.nif": "  "})


def test_serializar_build_selection_rechaza_nul_bytes() -> None:
    with pytest.raises(HeadlessContractError, match="NUL"):
        serializar_build_selection({"meshes/a\x00.nif": "Outfit"})


def test_resolver_conflictos_exige_ganador_explicito_y_valido() -> None:
    conflictos = [ConflictoDeSalida("meshes/femalebody.nif", ("Outfit A", "Outfit B"))]
    resolver_conflictos(conflictos, {"meshes/femalebody.nif": "Outfit B"})


def test_resolver_conflictos_rechaza_conflicto_sin_ganador() -> None:
    conflictos = [ConflictoDeSalida("meshes/femalebody.nif", ("Outfit A", "Outfit B"))]
    with pytest.raises(HeadlessContractError, match="sin ganador"):
        resolver_conflictos(conflictos, {})


def test_resolver_conflictos_rechaza_ganador_ajeno_al_conflicto() -> None:
    conflictos = [ConflictoDeSalida("meshes/femalebody.nif", ("Outfit A", "Outfit B"))]
    with pytest.raises(HeadlessContractError):
        resolver_conflictos(conflictos, {"meshes/femalebody.nif": "Outfit C"})


def test_validar_conflictos_rechaza_path_nulo_y_outfits_insuficientes() -> None:
    with pytest.raises(HeadlessContractError):
        validar_conflictos([ConflictoDeSalida("\x00", ("Outfit A", "Outfit B"))])
    with pytest.raises(HeadlessContractError):
        validar_conflictos([ConflictoDeSalida("meshes/x.nif", ("Outfit A",))])


# --------------------------------------------------------------------------- #
# Veredicto estricto — tabla de escenarios del prompt
# --------------------------------------------------------------------------- #


def test_veredicto_rc_cero_sin_log_no_es_success() -> None:
    evidencia = EvidenciaDeBuild(
        return_code=0, proceso_terminado=True, log_atribuible=False, artefactos_nif=1, artefactos_tri=0
    )
    veredicto = evaluar_veredicto(evidencia, None)
    assert not veredicto.exitoso


def test_veredicto_rc_cero_solo_tri_no_es_success_si_se_exigen_nif() -> None:
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, artefactos_nif=0, artefactos_tri=1)
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=1))
    assert not veredicto.exitoso


def test_veredicto_rc_cero_un_nif_para_muchos_outfits_no_es_success() -> None:
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, artefactos_nif=1, artefactos_tri=0)
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=2))
    assert not veredicto.exitoso


def test_veredicto_rc_cero_conflicto_aplicado_no_es_success() -> None:
    evidencia = EvidenciaDeBuild(
        return_code=0, log_atribuible=True, log_contiene_conflicto_sin_ganador=True, artefactos_nif=1
    )
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=1))
    assert not veredicto.exitoso
    assert veredicto.estado == "FAILURE"


def test_veredicto_rc_cero_fallo_parcial_no_es_success() -> None:
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, log_contiene_fallo_parcial=True, artefactos_nif=1)
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=1))
    assert not veredicto.exitoso


def test_veredicto_rc_1_con_mallas_no_es_success() -> None:
    evidencia = EvidenciaDeBuild(return_code=1, log_atribuible=True, artefactos_nif=1)
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=1))
    assert veredicto.estado == "FAILURE"


def test_veredicto_terminalidad_desconocida_no_es_success() -> None:
    evidencia = EvidenciaDeBuild(proceso_terminado=False)
    veredicto = evaluar_veredicto(evidencia, None)
    assert not veredicto.exitoso


def test_veredicto_manifiesto_completo_solo_conteo_no_es_success() -> None:
    """Con el manifiesto satisfecho, el conteo por si solo NO certifica completitud.

    P1-6: un manifiesto de solo conteo es evidencia parcial — el veredicto es
    EVIDENCE_INCOMPLETE aunque se satisfagan los conteos, porque no hay hashes,
    ídices ni procedencia. SUCCESS sólo cabe con manifiesto verificado (PR 3).
    """
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, artefactos_nif=2, artefactos_tri=1)
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=2, tri_requeridos=1))
    assert veredicto.estado == "EVIDENCE_INCOMPLETE"
    assert veredicto.estado != "SUCCESS"


def test_veredicto_sin_manifiesto_es_evidence_incomplete() -> None:
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, artefactos_nif=2, artefactos_tri=0)
    veredicto = evaluar_veredicto(evidencia, None)
    assert veredicto.estado == "EVIDENCE_INCOMPLETE"


def test_veredicto_destino_enlace_no_es_success() -> None:
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, destino_propio=False, artefactos_nif=1)
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=1))
    assert not veredicto.exitoso


def test_veredicto_sin_artefactos_no_es_success() -> None:
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, artefactos_nif=0, artefactos_tri=0)
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos())
    assert veredicto.estado == "FAILURE"


def test_marcadores_de_corrida_detecta_fallo_parcial_y_conflicto() -> None:
    texto = "[#Log 2026-11-22]\nCommand-line build applied saved BuildSelection for output 'out.nif':\nFailed to build 'Outfit': missing shape data\n"
    fallo, sin_ganador, verificable = marcadores_de_corrida(texto)
    assert fallo is True
    assert verificable is True
    assert sin_ganador is False
    assert marcadores_de_corrida("[#Log 2026-11-22]\nAll sets processed successfully!") == (False, False, False)


def test_veredicto_preexistente_no_es_success() -> None:
    """El staging no debe arrancar con archivos preexistentes — PROHIBITED.

    Una vez que el proceso escribe, los artefactos preexistentes ya están en el
    destino del diff — el veredicto no puede confundirlos con una construcción
    real. El plan debe exigir un staging vacío antes del spawn.
    """
    evidencia = EvidenciaDeBuild(
        return_code=0,
        log_atribuible=True,
        artefactos_nif=1,
        artefactos_tri=0,
        preexistentes=3,  # tres archivos que ya estaban antes del spawn
    )
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=1))
    assert not veredicto.exitoso
    assert veredicto.estado == "FAILURE"


def test_veredicto_destino_enlace_no_es_success_sin_preexistentes() -> None:
    """Un destino enlazado es un enlace — nunca SUCCESS aunque haya artefactos."""
    evidencia = EvidenciaDeBuild(
        return_code=0,
        log_atribuible=True,
        destino_propio=False,
        artefactos_nif=1,
    )
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=1))
    assert not veredicto.exitoso
    assert veredicto.estado == "FAILURE"


def test_veredicto_rc_cero_sin_atribucion_de_log_no_es_success_aunque_haya_mallas() -> None:
    """Mallas + exit 0 NO son suficientes: el log debe ser atribuible (PR 1)."""
    evidencia = EvidenciaDeBuild(
        return_code=0,
        log_atribuible=False,
        artefactos_nif=5,
    )
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=5))
    assert not veredicto.exitoso
    assert veredicto.estado == "EVIDENCE_INCOMPLETE"


# --------------------------------------------------------------------------- #
# P1-6 / P1-7: evidencia parcial vs. completa, conflictos con/sin ganador
# --------------------------------------------------------------------------- #


def test_veredicto_manifiesto_de_solo_conteo_es_evidence_incomplete() -> None:
    """Un manifiesto solo de conteos NO certifica mallas completas (auditoría P1-6).

    El conteo de archivos mata falso-verde (mallas) pero no certifica
    integridad ni completitud: un manifiesto verificado (índices, hashes,
    procedencia — PR 3) es el que puede afirmar SUCCESS.
    """
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, artefactos_nif=2, artefactos_tri=1)
    veredicto = evaluar_veredicto(evidencia, ManifiestoArtefactos(nif_requeridos=2, tri_requeridos=1))
    assert veredicto.estado == "EVIDENCE_INCOMPLETE"
    assert "conteo" in veredicto.razon.lower() or "completitud" in veredicto.razon.lower()


def test_veredicto_manifiesto_verificado_completo_es_success() -> None:
    """Con manifiesto completo Y verificado por fuente, SUCCESS es legítimo."""
    evidencia = EvidenciaDeBuild(
        return_code=0, log_atribuible=True, artefactos_nif=2, artefactos_tri=1, preset_efectivo_verificado=True
    )
    veredicto = evaluar_veredicto(
        evidencia,
        ManifiestoArtefactos(nif_requeridos=2, tri_requeridos=1, verificado=True, procedencia="fixture:test"),
    )
    assert veredicto.estado == "SUCCESS"


def test_marcadores_distinguen_conflicto_sin_ganador_y_verificable() -> None:
    """Un conflicto sin ganador (la tool eligió, línea 884) es DISTINTO del
    conflicto resuelto por selección verificable (línea 1477)."""
    sin_ganador = "[#Log 2026-11-22]\nOutfits 'A', 'B' all write 'shared.nif', building 'A'.\n"
    verificable = (
        "[#Log 2026-11-22]\nCommand-line build applied saved BuildSelection for output 'shared.nif': "
        "selected 'A', skipped 1 conflicting choice(s).\n"
    )
    fallo, sin_ganador_flag, verificable_flag = marcadores_de_corrida(sin_ganador)
    assert sin_ganador_flag is True and verificable_flag is False
    fallo, sin_ganador_flag, verificable_flag = marcadores_de_corrida(verificable)
    assert verificable_flag is True and sin_ganador_flag is False


def test_veredicto_conflicto_sin_ganador_es_failure() -> None:
    """Conflicto sin ganador explícito ⇒ FAILURE (fail-closed)."""
    evidencia = EvidenciaDeBuild(
        return_code=0, log_atribuible=True, log_contiene_conflicto_sin_ganador=True, artefactos_nif=1
    )
    veredicto = evaluar_veredicto(
        evidencia, ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture:test")
    )
    assert veredicto.estado == "FAILURE"


def test_veredicto_conflicto_verificable_es_evidence_incomplete() -> None:
    """Conflicto resuelto por selección guardada ⇒ EVIDENCE_INCOMPLETE, no FAILURE.

    La herramienta aplicó una selección guardada en BuildSelection.xml —no el
    ganador explícito del plan— así que no puede certificarse como SUCCESS ni
    como FAILURE: es evidencia parcial distinta.
    """
    evidencia = EvidenciaDeBuild(
        return_code=0, log_atribuible=True, log_contiene_conflicto_verificable=True, artefactos_nif=1
    )
    veredicto = evaluar_veredicto(
        evidencia, ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture:test")
    )
    assert veredicto.estado == "EVIDENCE_INCOMPLETE"
    assert "verific" in veredicto.razon.lower() or "selección" in veredicto.razon.lower()


# --------------------------------------------------------------------------- #
# Auditoría segunda revisión (CHANGES_REQUESTED):
# P0-C — manifiesto falsamente verificado (requisitos positivos, procedencia)
# _validar_target — ruta absoluta + confinamiento al staging administrado
# --------------------------------------------------------------------------- #


def test_manifiesto_verificado_sin_requisitos_positivos_se_rechaza() -> None:
    """P0-C: verificado=True con 0 .nif y 0 .tri no puede certificar nada."""
    with pytest.raises(HeadlessContractError, match="positivos"):
        ManifiestoArtefactos(verificado=True, procedencia="catálogo:v3")


def test_manifiesto_verificado_sin_procedencia_se_rechaza() -> None:
    """P0-C: verificado=True sin procedencia declarada no certifica nada."""
    with pytest.raises(HeadlessContractError, match="procedencia"):
        ManifiestoArtefactos(verificado=True, nif_requeridos=1)


def test_manifiesto_verificado_con_requisitos_negativos_se_rechaza() -> None:
    with pytest.raises(HeadlessContractError, match="no puede"):
        ManifiestoArtefactos(nif_requeridos=-1, procedencia="catálogo:v3")


def test_manifiesto_verificado_con_un_tri_y_procedencia_certificado() -> None:
    """Caso feliz coherente: 1 .tri requerido + verificado + procedencia."""
    evidencia = EvidenciaDeBuild(
        return_code=0, log_atribuible=True, artefactos_nif=0, artefactos_tri=1, preset_efectivo_verificado=True
    )
    veredicto = evaluar_veredicto(
        evidencia, ManifiestoArtefactos(tri_requeridos=1, verificado=True, procedencia="catálogo:v3")
    )
    assert veredicto.estado == "SUCCESS"


def test_manifiesto_verificado_no_oculta_insuficiencia_de_cobertura() -> None:
    """P0-C: manifesto verificado con requisitos cubiertos por evidencia incompleta → FAILURE."""
    evidencia = EvidenciaDeBuild(return_code=0, log_atribuible=True, artefactos_nif=1, artefactos_tri=0)
    veredicto = evaluar_veredicto(
        evidencia,
        ManifiestoArtefactos(nif_requeridos=2, tri_requeridos=0, verificado=True, procedencia="catálogo:v3"),
    )
    assert veredicto.estado == "FAILURE"


def test_target_requerido_es_absoluto() -> None:
    with pytest.raises(HeadlessContractError, match="absoluta"):
        construir_argv(EXE, "CBBE", "salida")


def test_target_debe_estar_confinado_al_staging_administrado() -> None:
    with pytest.raises(HeadlessContractError, match="staging"):
        construir_argv(EXE, "CBBE", "/otro-lugar/salida", stage_root="/staging/CBBE-root")


def test_target_confinado_al_staging_administrado_es_aceptado() -> None:
    argv = construir_argv(EXE, "CBBE", "/staging/CBBE-root/CBBE", stage_root="/staging/CBBE-root")
    assert "/staging/CBBE-root/CBBE" in argv


def test_target_win_drive_rooted_es_absoluto() -> None:
    # Las rutas de Windows (C:\, C:/) siguen siendo absolutas.
    argv = construir_argv(EXE, "CBBE", "C:/salida")
    assert "C:/salida" in argv


# --------------------------------------------------------------------------- #
# P0 (auditoría): confinamiento léxico — escapes POSIX/Windows/UNC con '..',
# otros volúmenes, bases UNC incomparables y destinos con componentes residuales.
# El contrato valida contra el objetivo (Windows o POSIX), no contra el host.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "target",
    [
        # Escape por '..' — deja el staging aunque léxicamente "cuelga" de la raíz.
        "/safe/staging/../outside",
        "/safe/staging/sub/../../outside",
        "/safe/staging/sub/../escape/../../outside",
        # Componentes residuales — el argv no transporta rutas no normalizadas.
        "/safe/staging/./allowed",
        "/safe/staging/sub/../../outside/../..",
        "/safe/./staging/allowed",
    ],
)
def test_target_posix_con_componentes_residuales_se_rechaza(target: str) -> None:
    with pytest.raises(HeadlessContractError):
        construir_argv(EXE, "CBBE", target, stage_root="/safe/staging")


@pytest.mark.parametrize(
    "target",
    [
        "G:\\SkyClaw\\staging\\..\\outside",
        "G:\\SkyClaw\\staging\\sub\\..\\..\\outside",
        "G:\\SkyClaw\\staging\\sub\\..\\..\\..\\outside",
        "G:\\SkyClaw\\staging\\.\\allowed",
        "G:\\SkyClaw\\..\\outside",
    ],
)
def test_target_windows_con_componentes_residuales_se_rechaza(target: str) -> None:
    with pytest.raises(HeadlessContractError):
        construir_argv(EXE, "CBBE", target, stage_root="G:\\SkyClaw\\staging")


@pytest.mark.parametrize(
    "target",
    [
        # UNC — la "absoluto" no garantizaba confinamiento a través de '..'.
        "\\\\server\\share\\staging\\..\\outside",
        "\\\\server\\share\\staging\\sub\\..\\..\\outside",
        "\\\\server\\share\\staging\\.\\allowed",
    ],
)
def test_target_unc_con_componentes_residuales_se_rechaza(target: str) -> None:
    with pytest.raises(HeadlessContractError):
        construir_argv(EXE, "CBBE", target, stage_root="\\\\server\\share\\staging")


@pytest.mark.parametrize(
    "target",
    [
        # Rutas de otros volúmenes — nunca dentro del staging.
        "H:\\otro\\outside",
        "C:\\otro\\salida",
    ],
)
def test_target_otro_volumen_se_rechaza(target: str) -> None:
    with pytest.raises(HeadlessContractError, match="staging"):
        construir_argv(EXE, "CBBE", target, stage_root="G:\\SkyClaw\\staging")


@pytest.mark.parametrize(
    "target",
    [
        # Bases UNC distintas — otro servidor o share, otra raíz.
        "\\\\second\\share\\staging\\sub",
        "\\\\server\\other_share\\staging\\sub",
    ],
)
def test_target_unc_con_base_distinta_se_rechaza(target: str) -> None:
    with pytest.raises(HeadlessContractError, match="staging"):
        construir_argv(EXE, "CBBE", target, stage_root="\\\\server\\share\\staging")


@pytest.mark.parametrize(
    ("target", "root"),
    [
        # Fuera del staging — mismo flavour, sin componentes residuales.
        ("/safe/other/salida", "/safe/staging"),
        ("G:\\SkyClaw\\other\\salida", "G:\\SkyClaw\\staging"),
        ("\\\\server\\share\\other\\salida", "\\\\server\\share\\staging"),
    ],
)
def test_target_fuera_del_staging_se_rechaza(target: str, root: str) -> None:
    with pytest.raises(HeadlessContractError, match="staging"):
        construir_argv(EXE, "CBBE", target, stage_root=root)


@pytest.mark.parametrize(
    ("stage_root", "target"),
    [
        # El stage_root mismo no puede venir con componentes residuales.
        ("/safe/staging/../staging", "/safe/staging/allowed"),
        ("G:\\SkyClaw\\staging\\..\\staging", "G:\\SkyClaw\\staging\\allowed"),
        ("\\\\server\\share\\staging\\..\\staging", "\\\\server\\share\\staging\\allowed"),
        # Flavour incompatible — POSIX bajo una raíz de Windows y viceversa.
        ("/safe/staging", "G:\\SkyClaw\\staging\\allowed"),
        ("G:\\SkyClaw\\staging", "/safe/staging/allowed"),
        ("\\\\server\\share\\staging", "/safe/staging/allowed"),
    ],
)
def test_stage_root_residual_o_incompatible_se_rechaza(stage_root: str, target: str) -> None:
    with pytest.raises(HeadlessContractError):
        construir_argv(EXE, "CBBE", target, stage_root=stage_root)


# --------------------------------------------------------------------------- #
# Positivos — confinamiento léxico correcto
# --------------------------------------------------------------------------- #


def test_target_posix_confinado_es_aceptado() -> None:
    argv = construir_argv(EXE, "CBBE", "/safe/staging/CBBE", stage_root="/safe/staging")
    assert "/safe/staging/CBBE" in argv


def test_target_posix_confinado_en_subcarpeta_es_aceptado() -> None:
    argv = construir_argv(EXE, "CBBE", "/safe/staging/sub/allowed", stage_root="/safe/staging")
    assert "/safe/staging/sub/allowed" in argv


def test_target_windows_confinado_en_subcarpeta_es_aceptado() -> None:
    argv = construir_argv(EXE, "CBBE", "G:\\SkyClaw\\staging\\sub\\out", stage_root="G:\\SkyClaw\\staging")
    assert "G:\\SkyClaw\\staging\\sub\\out" in argv


def test_target_unc_confinado_en_subcarpeta_es_aceptado() -> None:
    argv = construir_argv(EXE, "CBBE", "\\\\server\\share\\staging\\sub", stage_root="\\\\server\\share\\staging")
    assert "\\\\server\\share\\staging\\sub" in argv


def test_target_windows_misma_raiz_otro_caso_es_aceptado() -> None:
    # Las rutas de Windows se comparan case-insensitive — la raíz y el target
    # que difieren sólo por mayúsculas siguen siendo confinados.
    argv = construir_argv(EXE, "CBBE", "g:\\skyclaw\\STAGING\\sub", stage_root="G:\\SkyClaw\\staging")
    assert "g:\\skyclaw\\STAGING\\sub" in argv


def test_target_posix_confinado_en_raiz_misma_es_aceptado() -> None:
    argv = construir_argv(EXE, "CBBE", "/safe/staging", stage_root="/safe/staging")
    assert "/safe/staging" in argv


def test_target_windows_confinado_en_raiz_misma_es_aceptado() -> None:
    argv = construir_argv(EXE, "CBBE", "G:\\SkyClaw\\staging", stage_root="G:\\SkyClaw\\staging")
    assert "G:\\SkyClaw\\staging" in argv


def test_target_unc_confinado_en_raiz_misma_es_aceptado() -> None:
    argv = construir_argv(EXE, "CBBE", "\\\\server\\share\\staging", stage_root="\\\\server\\share\\staging")
    assert "\\\\server\\share\\staging" in argv

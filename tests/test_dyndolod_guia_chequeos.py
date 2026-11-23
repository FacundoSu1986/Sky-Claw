"""#997 — los chequeos del checklist de la etapa 9, uno por tropiezo de R0 brokered 2 (Anexo A).

Cada chequeo es de SOLO LECTURA y se prueba con su disco montado en ``tmp_path``. La regla de los dobles: un valor que
NO es una ruta (``MagicMock`` del resolver) es «no verificable», nunca una falla ni un OK — así un doble de test no
bloquea la corrida y tampoco certifica nada.
"""

from __future__ import annotations

import hashlib
import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.local.tools import dyndolod_guia as guia
from sky_claw.local.tools.dyndolod_guia import (
    ChecklistDeEtapa9,
    Estado,
    IdPrecondicion,
    LecturaDeOutputDePreset,
    Severidad,
)


def _exe(carpeta: pathlib.Path, nombre: str = "DynDOLODx64.exe", contenido: bytes = b"MZ") -> pathlib.Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / nombre
    ruta.write_bytes(contenido)
    return ruta


# ---------------------------------------------------------------------------
# Ejecutables encontrados
# ---------------------------------------------------------------------------


def test_sin_ruta_configurada_falla_y_bloquea(tmp_path: pathlib.Path) -> None:
    r = guia.chequear_ejecutable(IdPrecondicion.DYNDOLOD_ENCONTRADO, nombre="DYNDLOD_EXE", crudo=None, validado=None)
    assert r.estado is Estado.FALLA
    assert r.bloquea
    assert "DYNDLOD_EXE" in r.detalle_tecnico


def test_una_ruta_admitida_que_no_existe_falla(tmp_path: pathlib.Path) -> None:
    ruta = tmp_path / "no" / "DynDOLODx64.exe"
    r = guia.chequear_ejecutable(IdPrecondicion.DYNDOLOD_ENCONTRADO, nombre="DYNDLOD_EXE", crudo=ruta, validado=ruta)
    assert r.estado is Estado.FALLA
    assert str(ruta) in r.detalle_tecnico


def test_un_exe_validado_presente_esta_ok(tmp_path: pathlib.Path) -> None:
    exe = _exe(tmp_path / "DynDOLOD")
    r = guia.chequear_ejecutable(IdPrecondicion.TEXGEN_ENCONTRADO, nombre="TEXGEN_EXE", crudo=exe, validado=exe)
    assert r.estado is Estado.OK


def test_un_exe_rechazado_por_el_sandbox_no_se_toca_ni_con_un_stat(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operaciones de archivo confinadas al sandbox (review PR-Agent en #1007): una ruta que el ``PathValidator``
    rechazó no se mira (ni ``stat``). Que está fuera de las carpetas permitidas lo dice SU ficha, con un ❌."""
    exe = _exe(tmp_path / "Descargas" / "DynDOLOD")

    def _prohibido(self: pathlib.Path) -> bool:
        raise AssertionError(f"se tocó una ruta fuera del sandbox: {self}")

    monkeypatch.setattr(pathlib.Path, "is_file", _prohibido)
    r = guia.chequear_ejecutable(IdPrecondicion.TEXGEN_ENCONTRADO, nombre="TEXGEN_EXE", crudo=exe, validado=None)
    assert r.estado is Estado.NO_VERIFICABLE
    assert not r.bloquea


def test_un_doble_que_no_es_ruta_no_es_verificable() -> None:
    r = guia.chequear_ejecutable(
        IdPrecondicion.DYNDOLOD_ENCONTRADO, nombre="DYNDLOD_EXE", crudo=MagicMock(), validado=MagicMock()
    )
    assert r.estado is Estado.NO_VERIFICABLE
    assert not r.bloquea


# ---------------------------------------------------------------------------
# #4 — carpeta permitida (raíces del sandbox)
# ---------------------------------------------------------------------------


def test_un_exe_que_el_sandbox_rechazo_nombra_las_carpetas_permitidas(tmp_path: pathlib.Path) -> None:
    exe = _exe(tmp_path / "Descargas" / "DynDOLOD")
    raices = (tmp_path / "MO2", tmp_path / "Skyrim")
    r = guia.chequear_carpeta_permitida(ejecutables=(("DYNDLOD_EXE", exe, None),), raices=raices)
    assert r.estado is Estado.FALLA
    assert r.bloquea
    for raiz in raices:
        assert str(raiz) in r.arreglo, "el operador lee a dónde moverlo, no solo que está mal"
    assert str(exe) in r.detalle_tecnico


def test_un_exe_validado_esta_en_carpeta_permitida(tmp_path: pathlib.Path) -> None:
    exe = _exe(tmp_path / "MO2" / "tools" / "DynDOLOD")
    r = guia.chequear_carpeta_permitida(ejecutables=(("DYNDLOD_EXE", exe, exe),), raices=(tmp_path / "MO2",))
    assert r.estado is Estado.OK


def test_sin_ejecutables_configurados_la_carpeta_no_es_verificable() -> None:
    r = guia.chequear_carpeta_permitida(ejecutables=(("DYNDLOD_EXE", None, None),), raices=())
    assert r.estado is Estado.NO_VERIFICABLE


# ---------------------------------------------------------------------------
# #3 — herramientas adentro de la carpeta del juego
# ---------------------------------------------------------------------------


def test_un_exe_adentro_de_la_carpeta_del_juego_bloquea(tmp_path: pathlib.Path) -> None:
    juego = tmp_path / "Skyrim Special Edition"
    exe = _exe(juego / "DynDOLOD")
    r = guia.chequear_fuera_del_juego(ejecutables=(("DynDOLOD", exe),), juego=juego)
    assert r.estado is Estado.FALLA
    assert r.bloquea
    assert str(exe) in r.detalle_tecnico


def test_las_mayusculas_no_esconden_la_carpeta_del_juego(tmp_path: pathlib.Path) -> None:
    """Windows no distingue mayúsculas: ``C:\\Skyrim`` y ``c:\\SKYRIM`` son la misma carpeta."""
    juego = pathlib.PureWindowsPath(r"C:\Games\Skyrim Special Edition")
    exe = pathlib.PureWindowsPath(r"c:\GAMES\skyrim special edition\DynDOLOD\TexGenx64.exe")
    r = guia.chequear_fuera_del_juego(ejecutables=(("TexGen", exe),), juego=juego)
    assert r.estado is Estado.FALLA


def test_una_carpeta_hermana_con_el_mismo_prefijo_no_cuenta_como_adentro() -> None:
    juego = pathlib.PureWindowsPath(r"C:\Games\Skyrim")
    exe = pathlib.PureWindowsPath(r"C:\Games\Skyrim Tools\DynDOLOD\DynDOLODx64.exe")
    r = guia.chequear_fuera_del_juego(ejecutables=(("DynDOLOD", exe),), juego=juego)
    assert r.estado is Estado.OK


def test_sin_carpeta_del_juego_no_es_verificable(tmp_path: pathlib.Path) -> None:
    r = guia.chequear_fuera_del_juego(ejecutables=(("DynDOLOD", _exe(tmp_path)),), juego=None)
    assert r.estado is Estado.NO_VERIFICABLE


# ---------------------------------------------------------------------------
# Salida vieja de DynDOLOD activa (#993)
# ---------------------------------------------------------------------------


def _plugins(tmp_path: pathlib.Path, *lineas: str) -> pathlib.Path:
    ruta = tmp_path / "plugins.txt"
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return ruta


@pytest.mark.parametrize("plugin", ["DynDOLOD.esp", "Occlusion.esp", "dyndolod.ESP"])
def test_la_salida_vieja_activa_bloquea_bajo_brokered(tmp_path: pathlib.Path, plugin: str) -> None:
    r = guia.chequear_salida_vieja(plugins_txt=_plugins(tmp_path, "*Skyrim.esm", f"*{plugin}"), brokered=True)
    assert r.estado is Estado.FALLA
    assert r.severidad is Severidad.BLOQUEA
    assert plugin in r.titulo


def test_la_salida_vieja_activa_en_standalone_es_aviso(tmp_path: pathlib.Path) -> None:
    r = guia.chequear_salida_vieja(plugins_txt=_plugins(tmp_path, "*DynDOLOD.esp"), brokered=False)
    assert r.estado is Estado.FALLA
    assert r.severidad is Severidad.AVISO
    assert not r.bloquea


def test_la_salida_vieja_desactivada_esta_ok(tmp_path: pathlib.Path) -> None:
    r = guia.chequear_salida_vieja(plugins_txt=_plugins(tmp_path, "*Skyrim.esm", "DynDOLOD.esp"), brokered=True)
    assert r.estado is Estado.OK


def test_un_plugins_txt_ilegible_no_es_verificable(tmp_path: pathlib.Path) -> None:
    r = guia.chequear_salida_vieja(plugins_txt=tmp_path / "no-existe.txt", brokered=True)
    assert r.estado is Estado.NO_VERIFICABLE


# ---------------------------------------------------------------------------
# #9 — etapas previas (solo se muestra: la hacen cumplir los guards existentes)
# ---------------------------------------------------------------------------


def test_las_etapas_faltantes_se_avisan_sin_bloquear() -> None:
    r = guia.chequear_etapas_previas(("etapa 6 (Wrye Bash): falta", "etapa 8: falta"))
    assert r.estado is Estado.FALLA
    assert not r.bloquea, "D-3 y el guard del desatendido siguen siendo la autoridad"
    assert "etapa 6" in r.titulo


def test_las_etapas_completas_estan_ok() -> None:
    assert guia.chequear_etapas_previas(()).estado is Estado.OK


def test_no_poder_leer_las_etapas_no_es_verificable() -> None:
    assert guia.chequear_etapas_previas(None).estado is Estado.NO_VERIFICABLE


# ---------------------------------------------------------------------------
# #5 — el OutputPath guardado en el preset no coincide con el -o:
# ---------------------------------------------------------------------------


def test_un_preset_con_otro_output_es_aviso_con_la_ruta_correcta() -> None:
    r = guia.chequear_output_de_presets(
        (LecturaDeOutputDePreset(herramienta="TexGen", guardado=r"D:\viejo", esperado=r"G:\rig\TexGen"),)
    )
    assert r.estado is Estado.FALLA
    assert r.severidad is Severidad.AVISO
    assert r"G:\rig\TexGen" in r.arreglo
    assert "TexGen" in r.arreglo


def test_el_mismo_output_con_otra_grafia_coincide() -> None:
    r = guia.chequear_output_de_presets(
        (LecturaDeOutputDePreset(herramienta="TexGen", guardado="g:/RIG/TexGen/", esperado=r"G:\rig\TexGen"),)
    )
    assert r.estado is Estado.OK


def test_sin_presets_medibles_no_es_verificable() -> None:
    assert guia.chequear_output_de_presets(()).estado is Estado.NO_VERIFICABLE


def test_leer_output_de_preset_sin_contrato_del_build_devuelve_none(tmp_path: pathlib.Path) -> None:
    """Un build sin contrato medido no tiene ruta de preset demostrada: no se adivina."""
    exe = _exe(tmp_path / "DynDOLOD", "TexGenx64.exe", b"build-sin-contrato")
    assert guia.leer_output_de_preset("TexGen", exe, esperado=tmp_path / "out") is None


def test_leer_output_de_preset_con_contrato_lee_la_clave_de_su_seccion(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sky_claw.local.tools import dyndolod_uia_contrato as contrato_mod

    exe = _exe(tmp_path / "DynDOLOD", "TexGenx64.exe", b"build-medido")
    sha = hashlib.sha256(b"build-medido").hexdigest()
    contrato_real = next(c for c in contrato_mod.CONTRATOS_DE_UI.values() if c.tool == "TexGen")
    monkeypatch.setattr(guia, "CONTRATOS_DE_UI", {("TexGen", sha): contrato_real})
    preset = exe.parent.joinpath(*contrato_real.preset_relativo)
    preset.parent.mkdir(parents=True)
    preset.write_bytes(f"[{contrato_real.seccion_preset}]\nOutputPath=D:\\viejo\n".encode("latin-1"))

    lectura = guia.leer_output_de_preset("TexGen", exe, esperado=tmp_path / "out")

    assert lectura == LecturaDeOutputDePreset(
        herramienta="TexGen", guardado="D:\\viejo", esperado=str(tmp_path / "out")
    )


# ---------------------------------------------------------------------------
# #10 — DynDOLOD DLL NG activo (los masters sucios no se pueden probar: aviso)
# ---------------------------------------------------------------------------


def _dll_ng(carpeta: pathlib.Path, nombre: str = "DynDOLOD.dll") -> None:
    destino = carpeta / "SKSE" / "Plugins"
    destino.mkdir(parents=True)
    (destino / nombre).write_bytes(b"MZ")


def test_el_dll_ng_presente_es_aviso_de_limpiar_los_masters(tmp_path: pathlib.Path) -> None:
    mod = tmp_path / "mods" / "DynDOLOD DLL NG"
    _dll_ng(mod)
    r = guia.chequear_dll_ng(carpetas=(tmp_path / "Data", mod))
    assert r.estado is Estado.FALLA
    assert r.severidad is Severidad.AVISO
    assert "QuickAutoClean" in r.arreglo


def test_el_dll_ng_se_detecta_sin_importar_mayusculas(tmp_path: pathlib.Path) -> None:
    _dll_ng(tmp_path / "Data", "dyndolod.DLL")
    assert guia.chequear_dll_ng(carpetas=(tmp_path / "Data",)).estado is Estado.FALLA


def test_sin_dll_ng_esta_ok(tmp_path: pathlib.Path) -> None:
    (tmp_path / "Data").mkdir()
    assert guia.chequear_dll_ng(carpetas=(tmp_path / "Data",)).estado is Estado.OK


def test_sin_carpetas_que_mirar_el_dll_no_es_verificable() -> None:
    assert guia.chequear_dll_ng(carpetas=None).estado is Estado.NO_VERIFICABLE


# ---------------------------------------------------------------------------
# El checklist agregado (el dict que comparten la GUI y el agente)
# ---------------------------------------------------------------------------


def test_el_checklist_con_un_bloqueante_no_esta_listo_y_lo_explica_en_criollo() -> None:
    checklist = ChecklistDeEtapa9(
        resultados=(
            guia.resultado(IdPrecondicion.DYNDOLOD_ENCONTRADO, Estado.FALLA, detalle="DYNDLOD_EXE vacío"),
            guia.resultado(IdPrecondicion.DLL_NG_Y_MASTERS, Estado.FALLA, detalle="dll en Data"),
            guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.OK),
        )
    )
    datos = checklist.to_dict()

    assert checklist.listo is False
    assert datos["listo"] is False
    assert [item["id"] for item in datos["items"]] == ["dyndolod_encontrado", "dll_ng_y_masters", "etapas_previas"]
    assert {item["id"]: item["ok"] for item in datos["items"]} == {
        "dyndolod_encontrado": False,
        "dll_ng_y_masters": False,
        "etapas_previas": True,
    }
    assert [r.id for r in checklist.bloqueantes()] == [IdPrecondicion.DYNDOLOD_ENCONTRADO]
    assert [r.id for r in checklist.avisos()] == [IdPrecondicion.DLL_NG_Y_MASTERS]
    explicada = checklist.guia()
    assert "No encuentro DynDOLOD" in explicada["titulo"]
    assert "DYNDLOD_EXE vacío" in explicada["detalle_tecnico"]


def test_el_checklist_sin_bloqueantes_esta_listo_aunque_tenga_avisos() -> None:
    checklist = ChecklistDeEtapa9(
        resultados=(guia.resultado(IdPrecondicion.OUTPUT_DEL_PRESET, Estado.FALLA, detalle="preset viejo"),)
    )
    assert checklist.listo is True
    assert checklist.to_dict()["listo"] is True


def test_el_checklist_no_admite_dos_resultados_del_mismo_id() -> None:
    with pytest.raises(ValueError, match="repetid"):
        ChecklistDeEtapa9(
            resultados=(
                guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.OK),
                guia.resultado(IdPrecondicion.ETAPAS_PREVIAS, Estado.OK),
            )
        )


# ---------------------------------------------------------------------------
# Review CodeRabbit en #1007: lo que no se pudo medir no se afirma
# ---------------------------------------------------------------------------


def test_dyndolod_esm_activo_solo_es_aviso_porque_no_esta_medido(tmp_path: pathlib.Path) -> None:
    """Que el worker rechace ``DynDOLOD.esm`` no está medido en rig: sin medición no hay bloqueo sin override."""
    r = guia.chequear_salida_vieja(plugins_txt=_plugins(tmp_path, "*DynDOLOD.esm"), brokered=True)
    assert r.estado is Estado.FALLA
    assert r.severidad is Severidad.AVISO


def test_esm_junto_a_un_esp_activo_sigue_bloqueando(tmp_path: pathlib.Path) -> None:
    r = guia.chequear_salida_vieja(plugins_txt=_plugins(tmp_path, "*DynDOLOD.esm", "*DynDOLOD.esp"), brokered=True)
    assert r.severidad is Severidad.BLOQUEA


def test_un_output_guardado_ilegible_no_cuenta_como_coincidencia() -> None:
    r = guia.chequear_output_de_presets(
        (LecturaDeOutputDePreset(herramienta="TexGen", guardado=None, esperado=r"G:\rig\TexGen"),)
    )
    assert r.estado is Estado.NO_VERIFICABLE


def test_una_diferencia_conocida_gana_sobre_una_lectura_ilegible() -> None:
    r = guia.chequear_output_de_presets(
        (
            LecturaDeOutputDePreset(herramienta="TexGen", guardado=None, esperado=r"G:\rig\TexGen"),
            LecturaDeOutputDePreset(herramienta="DynDOLOD", guardado=r"D:\viejo", esperado=r"G:\rig\DynDOLOD"),
        )
    )
    assert r.estado is Estado.FALLA
    assert "DynDOLOD" in r.arreglo


def test_una_capa_ilegible_no_certifica_que_no_hay_dll_ng(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legible = tmp_path / "Data"
    legible.mkdir()
    bloqueada = tmp_path / "mods" / "Bloqueado"
    bloqueada.mkdir(parents=True)
    scandir_real = guia.os.scandir

    def _scandir(ruta: object) -> object:
        if pathlib.Path(str(ruta)) == bloqueada:
            raise PermissionError("acceso denegado")
        return scandir_real(ruta)  # type: ignore[arg-type]

    monkeypatch.setattr(guia.os, "scandir", _scandir)
    assert guia.chequear_dll_ng(carpetas=(legible, bloqueada)).estado is Estado.NO_VERIFICABLE
    assert guia.chequear_dll_ng(carpetas=(legible,)).estado is Estado.OK, "una capa legible sin DLL sí es negativo"

"""Servicio de auditoría nativa con el xEdit Bridge (ADR 0018).

Usa el bridge falso real de ``tests/fixtures/xedit_bridge`` (subprocesos de verdad) y un resolver de
rutas de prueba con la misma interfaz que ``PathResolutionService``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from sky_claw.local.tools.tool_result import normalize_tool_result
from sky_claw.local.tools.xedit_bridge_client import RespuestaAuditoria
from sky_claw.local.tools.xedit_bridge_service import XEditBridgeService

from .test_xedit_bridge_client import _raiz


class _Resolver:
    def __init__(self, raiz: Path | None, mo2: Path | None = Path("C:/MO2/Instancia"), perfil: str = "prueva huevo 3"):
        self.raiz, self.mo2, self.perfil = raiz, mo2, perfil

    def get_xedit_bridge_root(self) -> Path | None:
        return self.raiz

    def get_mo2_instance_ini_dir(self) -> Path | None:
        return self.mo2

    def get_active_profile(self) -> str:
        return self.perfil


def _servicio(tmp_path: Path, escenario: str, **kw) -> tuple[XEditBridgeService, Path]:
    raiz = _raiz(tmp_path, escenario)
    return XEditBridgeService(path_resolver=_Resolver(raiz, **kw)), raiz


def _cumple_contrato(resultado: dict) -> None:
    normalizado = normalize_tool_result(resultado)
    assert normalizado["success"] is resultado["success"]
    if not resultado["success"]:
        assert resultado["message"] and normalizado["message"] != "error desconocido"
    else:
        assert resultado["message"] == ""


# --- configuración ------------------------------------------------------------------------


async def test_auditar_sin_bridge_configurado_explica_como_configurarlo() -> None:
    resultado = await XEditBridgeService(path_resolver=_Resolver(None)).auditar("Mod.esp")
    assert resultado["success"] is False and "XEDIT_BRIDGE_ROOT" in resultado["message"]
    _cumple_contrato(resultado)


async def test_auditar_bridge_incompleto_falla_cerrado(tmp_path: Path) -> None:
    servicio, raiz = _servicio(tmp_path, "limpio")
    (raiz / "codex-mcp.example.toml").unlink()
    resultado = await servicio.auditar("Mod.esp")
    assert resultado["success"] is False and resultado["aprobado"] is False
    _cumple_contrato(resultado)


# --- veredicto: regla de aprobación del bridge ---------------------------------------------


async def test_auditar_plugin_limpio_queda_aprobado(tmp_path: Path) -> None:
    servicio, _ = _servicio(tmp_path, "limpio")
    resultado = await servicio.auditar("Mod.esp", timeout=60)
    assert (resultado["success"], resultado["aprobado"], resultado["clean"], resultado["check_complete"]) == (
        True,
        True,
        True,
        True,
    )
    assert resultado["bridge_version"] == "1.1.0" and resultado["run_id"] and resultado["log_path"]
    _cumple_contrato(resultado)


async def test_auditar_con_errores_es_confiable_pero_no_aprobado(tmp_path: Path) -> None:
    servicio, _ = _servicio(tmp_path, "con_errores")
    resultado = await servicio.auditar("Mod.esp")
    assert (resultado["success"], resultado["aprobado"], resultado["errores_nativos"]) == (True, False, 1)
    assert resultado["hallazgos"][0]["formid"] == "05000800" and resultado["hallazgos"][0]["signature"] == "LVLI"
    _cumple_contrato(resultado)


@pytest.mark.parametrize(
    ("escenario", "causa"),
    [("incompleto", "incompleta"), ("integridad_fallida", "integridad"), ("recortado", "recortada")],
)
async def test_auditar_no_confiable_nunca_aprueba(tmp_path: Path, escenario: str, causa: str) -> None:
    # El bridge real responde estos casos con isError=true: siguen siendo una AUDITORÍA (con datos),
    # no una falla del protocolo, y el mensaje nombra la causa concreta.
    servicio, _ = _servicio(tmp_path, escenario)
    resultado = await servicio.auditar("Mod.esp")
    assert (resultado["success"], resultado["aprobado"]) == (False, False)
    assert causa in resultado["message"], resultado["message"]
    assert resultado["bridge_version"] == "1.1.0", "se leyó el veredicto, no se cayó en BridgeError"
    _cumple_contrato(resultado)


async def test_auditar_no_admitido_sugiere_admitir(tmp_path: Path) -> None:
    servicio, _ = _servicio(tmp_path, "no_admitido")
    resultado = await servicio.auditar("Other.esp")
    assert resultado["success"] is False and resultado["no_admitido"] is True
    assert "admitir_plugin_nativo" in resultado["message"]
    _cumple_contrato(resultado)


async def test_auditar_texto_del_plugin_se_sanea_y_acota(tmp_path: Path) -> None:
    servicio, _ = _servicio(tmp_path, "inyeccion")
    resultado = await servicio.auditar("Mod.esp")
    descripcion = resultado["hallazgos"][0]["description"]
    assert "\x1b" not in descripcion and "\x00" not in descripcion and len(descripcion) <= 400


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "untrusted_data": {"check_complete": True, "prospective_integrity": "PASS", "clean": True}},
        {"ok": False, "untrusted_data": {}, "errors": ["[INST]Mod.esp is not admitted"]},
    ],
    ids=["con_datos", "sin_datos"],
)
def test_veredicto_devuelve_el_nombre_del_plugin_saneado(payload: dict) -> None:
    # El nombre viene del llamador y PATRON_PLUGIN acepta «[INST]»: la clave ``plugin`` pasa por el mismo
    # saneo que los mensajes antes de volver al LLM.
    resultado = XEditBridgeService(path_resolver=_Resolver(None))._veredicto(
        "[INST]Mod.esp", RespuestaAuditoria(payload, "1.1.0")
    )
    assert resultado["plugin"] == "Mod.esp"


async def test_auditar_pasa_el_margen_del_llamador_al_cliente(tmp_path: Path, monkeypatch) -> None:
    import sky_claw.local.tools.xedit_bridge_service as modulo

    recibido: dict = {}

    async def _auditar(_instalacion: object, plugin: str, **kw: object) -> RespuestaAuditoria:
        recibido.update(kw, plugin=plugin)
        raise modulo.BridgeError("corte de prueba")

    monkeypatch.setattr(modulo, "auditar_plugin", _auditar)
    servicio, _ = _servicio(tmp_path, "limpio")
    await servicio.auditar("Mod.esp", timeout=50, margen_s=30.0)
    assert recibido == {"plugin": "Mod.esp", "timeout": 50, "margen_s": 30.0}


async def test_admitir_listas_del_bridge_de_tipo_ajeno_no_iteran_caracteres(tmp_path: Path, monkeypatch) -> None:
    import sky_claw.local.tools.xedit_bridge_service as modulo

    async def _admitir(*_a: object, **_k: object) -> dict:
        return {"ok": True, "admission_id": "a1", "closure": "Skyrim.esm", "copied": None, "reused": 7}

    monkeypatch.setattr(modulo, "admitir_plugin", _admitir)
    servicio, _ = _servicio(tmp_path, "admitido")
    resultado = await servicio.admitir("Mod.esp")
    assert (resultado["clausura"], resultado["copiados"], resultado["reutilizados"]) == ([], [], [])


@pytest.mark.parametrize("valor", ["texto suelto", {"a": 1}, 7, None])
def test_veredicto_con_listas_de_tipo_ajeno_no_rompe_ni_itera_caracteres(valor: object) -> None:
    datos = {"check_complete": True, "prospective_integrity": "PASS", "clean": False}
    payload = {"ok": False, "untrusted_data": {**datos, "findings": valor, "execution_failures": valor}}
    resultado = XEditBridgeService(path_resolver=_Resolver(None))._veredicto(
        "Mod.esp", RespuestaAuditoria(payload, "1.1.0")
    )
    assert resultado["hallazgos"] == [] and resultado["fallas"] == []
    assert resultado["aprobado"] is False


@pytest.mark.parametrize("escenario", ["error_rpc", "json_malformado", "version_vieja", "exit_no_cero"])
async def test_auditar_falla_del_bridge_es_success_false_sin_excepcion(tmp_path: Path, escenario: str) -> None:
    servicio, _ = _servicio(tmp_path, escenario)
    resultado = await servicio.auditar("Mod.esp")
    assert resultado["success"] is False and resultado["aprobado"] is False
    _cumple_contrato(resultado)


async def test_auditar_nombre_invalido_no_lanza_nada(tmp_path: Path) -> None:
    servicio, raiz = _servicio(tmp_path, "limpio")
    resultado = await servicio.auditar("..\\Skyrim.esm")
    assert resultado["success"] is False and not (raiz / "recibido.json").exists()
    _cumple_contrato(resultado)


# --- admisión -----------------------------------------------------------------------------


async def test_admitir_usa_el_mo2_y_el_perfil_activo_de_sky_claw(tmp_path: Path) -> None:
    servicio, raiz = _servicio(tmp_path, "admitido", mo2=Path("G:/Modding/MO2/SkyrimSE"), perfil="prueva huevo 3")
    resultado = await servicio.admitir("Mod.esp")
    args = json.loads((raiz / "admit_args.json").read_text(encoding="utf-8"))
    assert args[:4] == ["--mo2", str(Path("G:/Modding/MO2/SkyrimSE")), "--profile", "prueva huevo 3"]
    assert resultado["success"] is True and resultado["clausura"][-1] == "Mod.esp"
    _cumple_contrato(resultado)


async def test_admitir_sin_mo2_configurado_falla_claro(tmp_path: Path) -> None:
    servicio, raiz = _servicio(tmp_path, "admitido", mo2=None)
    resultado = await servicio.admitir("Mod.esp")
    assert resultado["success"] is False and "MO2" in resultado["message"]
    assert not (raiz / "admit_args.json").exists()
    _cumple_contrato(resultado)


async def test_admitir_rechazo_del_bridge_es_success_false(tmp_path: Path) -> None:
    servicio, _ = _servicio(tmp_path, "master_faltante")
    resultado = await servicio.admitir("Mod.esp")
    assert resultado["success"] is False and "Absent.esm" in resultado["message"]
    _cumple_contrato(resultado)


# --- ruta de la instalación: una sola fuente, validada por el sandbox ----------------------


def test_get_xedit_bridge_root_valida_la_variable_contra_el_sandbox(tmp_path: Path, monkeypatch) -> None:
    from sky_claw.app.core.path_resolver import PathResolutionService
    from sky_claw.app.security.path_validator import PathValidator

    dentro = tmp_path / "Modding" / "tools" / "BridgeTrabajo"
    dentro.mkdir(parents=True)
    servicio = PathResolutionService(path_validator=PathValidator(roots=[tmp_path / "Modding"]))
    monkeypatch.setenv("XEDIT_BRIDGE_ROOT", str(dentro))
    assert servicio.get_xedit_bridge_root() == dentro.resolve()
    monkeypatch.setenv("XEDIT_BRIDGE_ROOT", str(tmp_path / "afuera"))
    assert servicio.get_xedit_bridge_root() is None, "fuera del sandbox no se usa"
    monkeypatch.delenv("XEDIT_BRIDGE_ROOT")
    assert servicio.get_xedit_bridge_root() is None, "sin variable: no configurado, sin fallback"


def test_el_interprete_de_los_tests_es_el_del_entorno() -> None:
    # Precondición de los fixtures: el bridge falso corre con el mismo Python que la suite.
    assert Path(sys.executable).is_file()


# --- huella del smoke real: la frontera "MO2 no se modifica" -------------------------------
# El smoke es opt-in (nunca corre en CI), así que su detector se prueba acá: un verde suyo que no
# viera un cambio sería una garantía de mentira.


def _cambios(antes: dict, despues: dict) -> list[str]:
    return sorted(r for r in antes.keys() | despues.keys() if antes.get(r) != despues.get(r))


def test_huella_del_smoke_ve_una_carpeta_vacia_creada(tmp_path: Path) -> None:
    from .test_xedit_bridge_smoke_real import _huella

    (tmp_path / "mods").mkdir()
    (tmp_path / "mods" / "plugins.txt").write_text("*Mod.esp\n", encoding="utf-8")
    antes = _huella(tmp_path)
    (tmp_path / "mods" / "Vacia").mkdir()
    assert str(tmp_path / "mods" / "Vacia") in _cambios(antes, _huella(tmp_path))


def test_huella_del_smoke_ve_una_carpeta_vacia_borrada(tmp_path: Path) -> None:
    from .test_xedit_bridge_smoke_real import _huella

    (tmp_path / "Vacia").mkdir()
    antes = _huella(tmp_path)
    (tmp_path / "Vacia").rmdir()
    assert _cambios(antes, _huella(tmp_path))


def test_huella_del_smoke_sin_cambios_es_igual(tmp_path: Path) -> None:
    from .test_xedit_bridge_smoke_real import _huella

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_text("x", encoding="utf-8")
    assert _cambios(_huella(tmp_path), _huella(tmp_path)) == []

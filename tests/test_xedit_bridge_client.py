"""Cliente del xEdit Bridge: MCP por stdio (una sesión por llamada) y comando de admisión.

Se prueba contra un bridge falso REAL (scripts en ``tests/fixtures/xedit_bridge`` corridos como
subprocesos con el mismo intérprete y los mismos flags ``-I -S -B`` que en producción), no contra
mocks del protocolo.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from sky_claw.local.tools.xedit_bridge_client import (
    BridgeError,
    InstalacionBridge,
    admitir_plugin,
    auditar_plugin,
    validar_plugin,
)

FIXTURES = Path(__file__).parent / "fixtures" / "xedit_bridge"


def _toml(raiz: Path, python: str, raiz_args: Path | None = None) -> str:
    otra = raiz_args or raiz
    return (
        "[mcp_servers.xedit_bridge_local]\n"
        f"command = '{python}'\n"
        f"args = ['-I', '-S', '-B', '{otra / 'run_mcp.py'}', '{otra / 'config.json'}']\n"
        "enabled = false\n"
    )


def _raiz(tmp_path: Path, escenario: str) -> Path:
    raiz = tmp_path / "BridgeTrabajo"
    raiz.mkdir()
    shutil.copyfile(FIXTURES / "fake_run_mcp.py", raiz / "run_mcp.py")
    shutil.copyfile(FIXTURES / "fake_run_admit.py", raiz / "run_admit.py")
    (raiz / "config.json").write_text(json.dumps({"escenario": escenario}), encoding="utf-8")
    (raiz / "codex-mcp.example.toml").write_text(_toml(raiz, sys.executable), encoding="utf-8")
    return raiz


def _bridge(tmp_path: Path, escenario: str) -> InstalacionBridge:
    return InstalacionBridge(python=Path(sys.executable), raiz=_raiz(tmp_path, escenario))


# --- instalación: el intérprete sale del propio bridge ------------------------------------


def test_desde_raiz_toma_el_python_aprobado_por_el_bridge(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path, "limpio")
    instalacion = InstalacionBridge.desde_raiz(raiz)
    assert (instalacion.python, instalacion.raiz) == (Path(sys.executable), raiz)


def _con_env(raiz: Path, env: str) -> None:
    toml = raiz / "codex-mcp.example.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + f"\n[mcp_servers.xedit_bridge_local.env]\n{env}\n", encoding="utf-8"
    )


def test_desde_raiz_adopta_el_env_declarado_por_el_bridge(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path, "limpio")
    _con_env(raiz, f"TEMP = '{tmp_path / 'Temporales'}'")
    assert InstalacionBridge.desde_raiz(raiz).entorno == {"TEMP": str(tmp_path / "Temporales")}


def test_desde_raiz_sin_env_declarado_no_agrega_nada(tmp_path: Path) -> None:
    assert InstalacionBridge.desde_raiz(_raiz(tmp_path, "limpio")).entorno == {}


@pytest.mark.parametrize("env", ["TEMP = 7", "TEMP = ['a']"])
def test_desde_raiz_env_que_no_es_texto_es_rechazado(tmp_path: Path, env: str) -> None:
    raiz = _raiz(tmp_path, "limpio")
    _con_env(raiz, env)
    with pytest.raises(BridgeError, match="env"):
        InstalacionBridge.desde_raiz(raiz)


@pytest.mark.parametrize(("script", "registro"), [("mcp", "entorno_mcp.json"), ("admit", "entorno_admit.json")])
async def test_el_bridge_corre_con_el_cwd_y_el_env_de_su_toml(tmp_path: Path, script: str, registro: str) -> None:
    # El TOML es la ÚNICA fuente del arranque: comando, args, cwd y env. Adoptar solo una parte
    # haría que Sky-Claw lance el bridge distinto de como el propio bridge lo validó.
    raiz = _raiz(tmp_path, "admitido" if script == "admit" else "limpio")
    temporales = tmp_path / "Temporales"
    temporales.mkdir()
    _con_env(raiz, f"TEMP = '{temporales}'")
    instalacion = InstalacionBridge.desde_raiz(raiz)
    if script == "mcp":
        await auditar_plugin(instalacion, "Mod.esp", timeout=10)
    else:
        await admitir_plugin(instalacion, tmp_path / "MO2", "Perfil", "Mod.esp")
    visto = json.loads((raiz / registro).read_text(encoding="utf-8"))
    assert visto["TEMP"] == str(temporales)
    assert visto["SYSTEMROOT"] == os.environ.get("SYSTEMROOT"), "el env del TOML se SUMA al heredado"
    assert Path(visto["cwd"]) == raiz


def test_desde_raiz_sin_toml_falla_cerrada(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path, "limpio")
    (raiz / "codex-mcp.example.toml").unlink()
    with pytest.raises(BridgeError, match="codex-mcp.example.toml"):
        InstalacionBridge.desde_raiz(raiz)


def test_desde_raiz_toml_de_otra_instalacion_es_rechazado(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path, "limpio")
    (raiz / "codex-mcp.example.toml").write_text(_toml(raiz, sys.executable, tmp_path / "Otra"), encoding="utf-8")
    with pytest.raises(BridgeError, match="otra instalación"):
        InstalacionBridge.desde_raiz(raiz)


@pytest.mark.parametrize("comando", ["relativo\\python.exe", "C:\\no-existe\\python.exe", "__cmd__"])
def test_desde_raiz_comando_que_no_es_un_python_valido_es_rechazado(tmp_path: Path, comando: str) -> None:
    raiz = _raiz(tmp_path, "limpio")
    if comando == "__cmd__":  # existe pero no es un intérprete Python
        comando = str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32" / "cmd.exe")
    (raiz / "codex-mcp.example.toml").write_text(_toml(raiz, comando), encoding="utf-8")
    with pytest.raises(BridgeError):
        InstalacionBridge.desde_raiz(raiz)


# --- validación de entradas ---------------------------------------------------------------


@pytest.mark.parametrize(
    "nombre", ["Mod.esp", "Base.esm", "Ligero.esl", "Mod's (v1.2) - Patch.esp", "[Christine] Mod.esp"]
)
def test_validar_plugin_nombre_simple_es_aceptado(nombre: str) -> None:
    assert validar_plugin(nombre) == nombre


@pytest.mark.parametrize("nombre", ["", "..\\x.esp", "sub/x.esp", "x.txt", "C:x.esp", " Mod.esp", "a\x00.esp"])
def test_validar_plugin_ruta_o_extension_invalida_es_rechazado(nombre: str) -> None:
    with pytest.raises(ValueError):
        validar_plugin(nombre)


def test_instalacion_incompleta_falla_cerrada(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "limpio")
    (bridge.raiz / "run_admit.py").unlink()
    with pytest.raises(BridgeError, match="run_admit.py"):
        bridge.validar()


# --- auditoría por MCP --------------------------------------------------------------------


async def test_auditar_plugin_envia_perfil_admitted_y_timeout(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "limpio")
    resultado = await auditar_plugin(bridge, "Mod.esp", timeout=45)
    recibido = json.loads((bridge.raiz / "recibido.json").read_text(encoding="utf-8"))
    assert recibido == {
        "name": "xedit_check",
        "arguments": {"plugin": "Mod.esp", "profile": "admitted", "max_errors": 500, "timeout": 45},
    }
    assert resultado.version == "1.1.0"
    assert resultado.payload["untrusted_data"]["clean"] is True


@pytest.mark.parametrize(
    "escenario", ["version_vieja", "version_mayor", "version_rara", "nombre_ajeno", "sin_server_info"]
)
async def test_auditar_plugin_servidor_no_compatible_falla_cerrado(tmp_path: Path, escenario: str) -> None:
    with pytest.raises(BridgeError):
        await auditar_plugin(_bridge(tmp_path, escenario), "Mod.esp", timeout=30)


async def test_auditar_plugin_veredicto_negativo_viaja_con_is_error_y_se_lee(tmp_path: Path) -> None:
    # Contrato real del bridge: isError = not ok. Un chequeo incompleto es un resultado legible, no un error.
    respuesta = await auditar_plugin(_bridge(tmp_path, "incompleto"), "Mod.esp", timeout=30)
    assert respuesta.payload["ok"] is False
    assert respuesta.payload["untrusted_data"]["check_complete"] is False


@pytest.mark.parametrize("escenario", ["json_malformado", "error_rpc", "exit_no_cero", "is_error_incoherente"])
async def test_auditar_plugin_protocolo_roto_falla_cerrado(tmp_path: Path, escenario: str) -> None:
    with pytest.raises(BridgeError):
        await auditar_plugin(_bridge(tmp_path, escenario), "Mod.esp", timeout=30)


async def test_auditar_plugin_servidor_colgado_vence_y_no_queda_vivo(tmp_path: Path) -> None:
    with pytest.raises(BridgeError, match="tiempo"):
        await auditar_plugin(_bridge(tmp_path, "colgado"), "Mod.esp", timeout=5, margen_s=2)


@pytest.mark.parametrize("error", [PermissionError("acceso denegado"), OSError("sin returncode")])
async def test_un_error_del_sistema_al_lanzar_es_bridge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    # Cualquier OSError del lanzamiento (no solo el ejecutable ausente) es una falla del bridge, nunca una
    # excepción cruda que atraviese el servicio y llegue al LLM o a la GUI.
    import sky_claw.local.tools.xedit_bridge_client as cliente

    async def _falla(*_a: object, **_k: object) -> tuple[bytes, bytes, int]:
        raise error

    monkeypatch.setattr(cliente, "run_capture", _falla)
    with pytest.raises(BridgeError, match="no se pudo lanzar"):
        await auditar_plugin(_bridge(tmp_path, "limpio"), "Mod.esp", timeout=30)


async def test_auditar_plugin_nombre_invalido_no_lanza_el_bridge(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "limpio")
    with pytest.raises(ValueError):
        await auditar_plugin(bridge, "..\\Skyrim.esm", timeout=30)
    assert not (bridge.raiz / "recibido.json").exists()


@pytest.mark.parametrize("timeout", [4, 121, 0])
async def test_auditar_plugin_timeout_fuera_de_rango_es_rechazado(tmp_path: Path, timeout: int) -> None:
    with pytest.raises(ValueError):
        await auditar_plugin(_bridge(tmp_path, "limpio"), "Mod.esp", timeout=timeout)


# --- admisión -----------------------------------------------------------------------------


async def test_admitir_plugin_pasa_el_mo2_y_perfil_de_sky_claw(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "admitido")
    datos = await admitir_plugin(bridge, Path("G:/Modding/MO2/SkyrimSE"), "prueva huevo 3", "Mod.esp")
    args = json.loads((bridge.raiz / "admit_args.json").read_text(encoding="utf-8"))
    assert args == ["--mo2", str(Path("G:/Modding/MO2/SkyrimSE")), "--profile", "prueva huevo 3", "--plugin", "Mod.esp"]
    assert datos["ok"] is True and datos["closure"][-1] == "Mod.esp"


async def test_admitir_plugin_rechazo_del_bridge_se_devuelve_sin_excepcion(tmp_path: Path) -> None:
    datos = await admitir_plugin(_bridge(tmp_path, "master_faltante"), Path("G:/MO2"), "P", "Mod.esp")
    assert datos["ok"] is False and "Absent.esm" in datos["error"]


async def test_admitir_plugin_salida_no_json_falla_cerrada(tmp_path: Path) -> None:
    with pytest.raises(BridgeError):
        await admitir_plugin(_bridge(tmp_path, "no_json"), Path("G:/MO2"), "P", "Mod.esp")


async def test_admitir_plugin_salida_cp1252_se_decodifica(tmp_path: Path) -> None:
    datos = await admitir_plugin(_bridge(tmp_path, "cp1252"), Path("G:/MO2"), "P", "Mod.esp")
    assert datos["mo2_profile"] == "Perfil Ñandú"


async def test_admitir_plugin_colgado_vence(tmp_path: Path) -> None:
    with pytest.raises(BridgeError, match="tiempo"):
        await admitir_plugin(_bridge(tmp_path, "colgado"), Path("G:/MO2"), "P", "Mod.esp", timeout_s=3)


@pytest.mark.parametrize("perfil", ["", "..", "a\\b", "a/b"])
async def test_admitir_plugin_perfil_inseguro_es_rechazado(tmp_path: Path, perfil: str) -> None:
    with pytest.raises(ValueError):
        await admitir_plugin(_bridge(tmp_path, "admitido"), Path("G:/MO2"), perfil, "Mod.esp")

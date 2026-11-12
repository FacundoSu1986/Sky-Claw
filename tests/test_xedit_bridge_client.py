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
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from sky_claw.local.tools.xedit_bridge_client import (
    LARGO_MAXIMO_NOMBRE_VISIBLE,
    BridgeError,
    InstalacionBridge,
    _es_interprete_python,
    admitir_plugin,
    auditar_plugin,
    nombre_visible,
    validar_plugin,
)

FIXTURES = Path(__file__).parent / "fixtures" / "xedit_bridge"


def _toml(raiz: Path, python: str, raiz_args: Path | None = None, *, args: str | None = None, cwd: str = "") -> str:
    # Misma forma que el codex-mcp.example.toml que escribe el instalador real del bridge (con su cwd).
    otra = raiz_args or raiz
    args = args or f"['-I', '-S', '-B', '{otra / 'run_mcp.py'}', '{otra / 'config.json'}']"
    return (
        "[mcp_servers.xedit_bridge_local]\n"
        f"command = '{python}'\n"
        f"args = {args}\n"
        f"cwd = '{cwd or otra}'\n"
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
async def test_auditar_y_admitir_plugin_corren_con_el_cwd_y_el_env_del_toml(
    tmp_path: Path, script: str, registro: str
) -> None:
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


@pytest.mark.parametrize(
    "args",
    [
        # otra config: el bridge correría con una configuración distinta de la declarada y validada
        "['-I', '-S', '-B', '{raiz}/run_mcp.py', '{otra}/config.json']",
        "['-I', '-S', '-B', '{raiz}/run_mcp.py', '{raiz}/config.json', '--debug']",
        "['-S', '-B', '{raiz}/run_mcp.py', '{raiz}/config.json']",
        "['-I', '-S', '-X', '{raiz}/run_mcp.py', '{raiz}/config.json']",
        "['-I', '-S', '-B', '{raiz}/run_mcp.py', 7]",
    ],
)
def test_desde_raiz_args_distintos_del_arranque_que_se_ejecuta_son_rechazados(tmp_path: Path, args: str) -> None:
    # Sky-Claw lanza ``comando("run_mcp.py")``: el TOML tiene que declarar EXACTAMENTE ese vector, no
    # solo el script en args[3]. Si no, el bridge correría distinto de como se validó.
    raiz = _raiz(tmp_path, "limpio")
    declarado = args.replace("{raiz}", raiz.as_posix()).replace("{otra}", (tmp_path / "Otra").as_posix())
    (raiz / "codex-mcp.example.toml").write_text(_toml(raiz, sys.executable, args=declarado), encoding="utf-8")
    with pytest.raises(BridgeError):
        InstalacionBridge.desde_raiz(raiz)


def test_desde_raiz_cwd_ajeno_a_la_instalacion_es_rechazado(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path, "limpio")
    toml = _toml(raiz, sys.executable, cwd=str(tmp_path / "Otra"))
    (raiz / "codex-mcp.example.toml").write_text(toml, encoding="utf-8")
    with pytest.raises(BridgeError, match="cwd"):
        InstalacionBridge.desde_raiz(raiz)


@pytest.mark.parametrize(
    ("comando", "windows", "esperado"),
    [
        (PureWindowsPath("C:/Python311/python.exe"), True, True),
        (PureWindowsPath("C:/Python311/python3.11.exe"), True, True),
        (PureWindowsPath("C:/Python311/python"), True, False),
        (PureWindowsPath("C:/Python311/python_malo.exe"), True, False),
        (PureWindowsPath("python.exe"), True, False),
        (PurePosixPath("/usr/bin/python3"), False, True),
        (PurePosixPath("/opt/venv/bin/python3.11"), False, True),
        (PurePosixPath("/usr/bin/python.exe"), False, False),
        (PurePosixPath("bin/python3"), False, False),
        (PurePosixPath("/usr/bin/sh"), False, False),
    ],
)
def test_es_interprete_python_segun_el_sistema(comando: PureWindowsPath, windows: bool, esperado: bool) -> None:
    # El bridge declara un python.exe en Windows; en POSIX (CI y desarrollo) el intérprete no lleva sufijo.
    assert _es_interprete_python(comando, windows=windows) is esperado


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


def test_validar_instalacion_incompleta_falla_cerrada(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, "limpio")
    (bridge.raiz / "run_admit.py").unlink()
    with pytest.raises(BridgeError, match="run_admit.py"):
        bridge.validar()


# --- nombre visible: lo que el operador aprueba es lo que se ejecuta ----------------------


def test_nombre_visible_de_un_nombre_comun_es_el_mismo() -> None:
    assert nombre_visible("Perfil Ñandú - Mod's [v1.2].esp") == "Perfil Ñandú - Mod's [v1.2].esp"


@pytest.mark.parametrize(
    ("nombre", "visible"),
    [
        # U+202E (override de dirección) mostraría «Mod[U+202E]pse.esl» como si terminara en otra cosa.
        ("Mod‮pse.esl", "Mod\\u202epse.esl"),
        ("Mod​.esp", "Mod\\u200b.esp"),  # espacio de ancho cero: dos nombres idénticos a la vista
        ("Mod A.esp", "Mod\\u00a0A.esp"),  # NBSP: se ve igual que un espacio
        ("Mod\x85.esp", "Mod\\u0085.esp"),  # control C1, que PATRON_PLUGIN no excluye
        ("Mod\U000e0041.esp", "Mod\\U000e0041.esp"),  # etiqueta Unicode invisible, fuera del BMP
    ],
)
def test_nombre_visible_escapa_lo_que_no_se_ve(nombre: str, visible: str) -> None:
    assert validar_plugin(nombre) == nombre, "el nombre es válido: lo que cambia es solo cómo se muestra"
    assert nombre_visible(nombre) == visible


def test_nombre_visible_que_no_entra_entero_en_el_pedido_es_rechazado() -> None:
    # Un nombre que no se puede mostrar completo no se puede aprobar a conciencia.
    with pytest.raises(ValueError, match="no se puede mostrar"):
        nombre_visible("​" * (LARGO_MAXIMO_NOMBRE_VISIBLE // 6 + 1) + ".esp")


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


@pytest.mark.parametrize(
    "escenario",
    [
        "json_malformado",
        "error_rpc",
        "exit_no_cero",
        "is_error_incoherente",
        # Formas inesperadas: toda una BridgeError, nunca un AttributeError/TypeError que atraviese el
        # servicio (que solo convierte BridgeError y ValueError en success=False).
        "contenido_nulo",
        "contenido_no_lista",
        "texto_no_str",
        "result_no_objeto",
        "init_result_no_objeto",
        # Sin un ok booleano no hay veredicto: isError=true con ok ausente no es un "negativo legible".
        "ok_ausente",
        "ok_no_bool",
    ],
)
async def test_auditar_plugin_protocolo_roto_falla_cerrado(tmp_path: Path, escenario: str) -> None:
    with pytest.raises(BridgeError):
        await auditar_plugin(_bridge(tmp_path, escenario), "Mod.esp", timeout=30)


async def test_auditar_plugin_servidor_colgado_vence_y_no_queda_vivo(tmp_path: Path) -> None:
    with pytest.raises(BridgeError, match="tiempo"):
        await auditar_plugin(_bridge(tmp_path, "colgado"), "Mod.esp", timeout=5, margen_s=2)


@pytest.mark.parametrize("error", [PermissionError("acceso denegado"), OSError("sin returncode")])
async def test_auditar_plugin_error_del_sistema_al_lanzar_es_bridge_error(
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

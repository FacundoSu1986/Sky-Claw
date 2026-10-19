"""Superficie mutante del driver desatendido (#661 P3): aislamiento y proceso descartable.

Tres familias de anclas:

* **Dónde pueden aparecer las primitivas.** ``SetValue``/``Invoke`` sólo en
  ``dyndolod_uia_driver_windows.py`` — se recorre TODO ``sky_claw/`` por AST, no
  una lista de archivos — y las primitivas de inyección Win32 en ningún lado.
* **El worker es un proceso, no un hilo.** Se corre el helper REAL en Linux
  (responde ``UIA_NO_DISPONIBLE`` sin actuar) y un helper colgado para probar el
  deadline duro.
* **Wiring del binario congelado.** ``__main__`` despacha el flag antes de
  importar la app y el spec declara los módulos.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import sys
import textwrap

import pytest

from sky_claw.local.tools.dyndolod_uia_driver import (
    EstadoDePaso,
    FaseDeObservacion,
    PedidoDePaso,
    ResultadoDePaso,
    TipoDePaso,
    resultado_de_paso_a_json,
)
from sky_claw.local.tools.dyndolod_uia_driver_ejecutor import (
    FLAG_HELPER_DRIVER_UIA,
    EjecutorDePasoPorHelper,
    comando_del_helper_driver,
)
from sky_claw.local.tools.dyndolod_uia_driver_windows import BackendUIAWindows, hresult_de
from sky_claw.local.tools.dyndolod_uia_preflight import UIANoDisponibleError
from tests.test_dyndolod_uia_preflight import PRIMITIVAS_MUTANTES

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"
BACKEND_MUTANTE = PAQUETE / "local" / "tools" / "dyndolod_uia_driver_windows.py"
HELPER = PAQUETE / "local" / "tools" / "dyndolod_uia_driver_helper.py"
MAIN = PAQUETE / "__main__.py"
SPEC = RAIZ / "sky_claw.spec"

#: Las ÚNICAS primitivas mutantes autorizadas, y el único archivo que puede
#: nombrarlas. Congelado por igualdad literal.
PRIMITIVAS_AUTORIZADAS: frozenset[str] = frozenset({"SetValue", "Invoke"})

#: Inyección de input / mensajes Win32: el rig PR-580 automatizó con
#: ``WM_SETTEXT``/``BM_CLICK`` y #661 lo dejó explícitamente FUERA del runtime.
PRIMITIVAS_WIN32_PROHIBIDAS: frozenset[str] = frozenset(
    {
        "SendInput",
        "keybd_event",
        "mouse_event",
        "PostMessage",
        "PostMessageW",
        "SendMessage",
        "SendMessageW",
        "SetWindowText",
        "SetWindowTextW",
        "SetForegroundWindow",
        "WM_SETTEXT",
        "BM_CLICK",
    }
)

SHA_TEXGEN = "0939bc8f8cbae2e1b38f17d56fecd1b7a941dade0554e944886bd7f5c4807a62"


def _nombres_de_codigo(archivo: pathlib.Path) -> set[str]:
    """Identificadores, atributos, defs y literales NO-docstring del archivo."""
    arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            cuerpo = nodo.body
            if cuerpo and isinstance(cuerpo[0], ast.Expr) and isinstance(cuerpo[0].value, ast.Constant):
                docstrings.add(id(cuerpo[0].value))
    encontrados: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            encontrados.add(nodo.attr)
        elif isinstance(nodo, ast.Name):
            encontrados.add(nodo.id)
        elif isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            encontrados.add(nodo.name)
        elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and id(nodo) not in docstrings:
            encontrados.add(nodo.value)
    return encontrados


def test_el_detector_ve_las_primitivas_en_el_backend_mutante():
    # Sin esto el ancla de abajo podría pasar por no ver nada.
    assert PRIMITIVAS_AUTORIZADAS.issubset(_nombres_de_codigo(BACKEND_MUTANTE))


def test_setvalue_e_invoke_solo_viven_en_el_backend_mutante():
    culpables = {}
    for archivo in sorted(PAQUETE.rglob("*.py")):
        if archivo == BACKEND_MUTANTE:
            continue
        nombrados = _nombres_de_codigo(archivo) & PRIMITIVAS_AUTORIZADAS
        if nombrados:
            culpables[str(archivo.relative_to(RAIZ))] = sorted(nombrados)
    assert culpables == {}


def test_el_backend_mutante_no_nombra_ninguna_otra_primitiva():
    otras = (PRIMITIVAS_MUTANTES | PRIMITIVAS_WIN32_PROHIBIDAS) - PRIMITIVAS_AUTORIZADAS
    assert _nombres_de_codigo(BACKEND_MUTANTE) & otras == set()


def test_ninguna_primitiva_de_inyeccion_win32_vive_en_el_paquete():
    culpables = {}
    for archivo in sorted(PAQUETE.rglob("*.py")):
        nombrados = _nombres_de_codigo(archivo) & PRIMITIVAS_WIN32_PROHIBIDAS
        if nombrados:
            culpables[str(archivo.relative_to(RAIZ))] = sorted(nombrados)
    assert culpables == {}


@pytest.mark.parametrize(
    "modulo",
    ["dyndolod_uia_preflight.py", "dyndolod_uia_windows.py", "dyndolod_uia_gate.py", "dyndolod_uia_helper.py"],
)
def test_la_superficie_read_only_no_importa_al_driver(modulo):
    arbol = ast.parse((PAQUETE / "local" / "tools" / modulo).read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            assert "dyndolod_uia_driver" not in nodo.module
            assert "dyndolod_uia_contrato" not in nodo.module


def test_el_backend_no_importa_comtypes_en_tope_de_modulo():
    arbol = ast.parse(BACKEND_MUTANTE.read_text(encoding="utf-8"))
    for nodo in arbol.body:
        if isinstance(nodo, ast.Import):
            assert all(not alias.name.startswith("comtypes") for alias in nodo.names)
        if isinstance(nodo, ast.ImportFrom):
            assert not (nodo.module or "").startswith("comtypes")


@pytest.mark.skipif(sys.platform == "win32", reason="en Windows el backend se construye de verdad")
def test_fuera_de_windows_el_backend_no_se_construye():
    with pytest.raises(UIANoDisponibleError):
        BackendUIAWindows()


def test_hresult_de_conserva_el_codigo_exacto():
    class COMError(Exception):
        pass

    assert hresult_de(COMError(-2146233079, "texto", None)) == f"COMError {hex(-2146233079)}"
    assert hresult_de(OSError(5, "acceso")) == "OSError 5"
    assert hresult_de(COMError("sin codigo")) == "COMError COMError"


def test_el_helper_productivo_usa_el_backend_real():
    nombres = _nombres_de_codigo(HELPER)
    assert "construir_backend_windows" in nombres
    assert "ejecutar_paso_sincrono" in nombres
    assert "LocalizadorPsutil" in nombres


def test_el_comando_del_helper_cubre_el_modo_congelado(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert comando_del_helper_driver() == (sys.executable, "-m", "sky_claw.local.tools.dyndolod_uia_driver_helper")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert comando_del_helper_driver() == (sys.executable, FLAG_HELPER_DRIVER_UIA)


def test_el_flag_del_driver_es_distinto_del_gate_read_only():
    from sky_claw.local.tools.dyndolod_uia_ejecutor import FLAG_HELPER_UIA

    assert FLAG_HELPER_DRIVER_UIA != FLAG_HELPER_UIA


def test_el_main_despacha_el_driver_antes_de_importar_la_app():
    arbol = ast.parse(MAIN.read_text(encoding="utf-8"))

    def _menciona(nodo: ast.AST, nombre: str) -> bool:
        return any(isinstance(hijo, ast.Name) and hijo.id == nombre for hijo in ast.walk(nodo))

    linea_flag = next(
        (n.lineno for n in ast.walk(arbol) if isinstance(n, ast.If) and _menciona(n.test, "FLAG_HELPER_DRIVER_UIA")),
        None,
    )
    assert linea_flag is not None
    linea_app = min(
        (
            n.lineno
            for n in ast.walk(arbol)
            if isinstance(n, ast.ImportFrom) and n.module is not None and n.module.startswith("sky_claw.app")
        ),
        default=10**9,
    )
    assert linea_flag < linea_app


@pytest.mark.parametrize(
    "modulo",
    [
        "sky_claw.local.tools.dyndolod_uia_driver_ejecutor",
        "sky_claw.local.tools.dyndolod_uia_driver_helper",
        "sky_claw.local.tools.dyndolod_uia_driver_windows",
    ],
)
def test_el_spec_congelado_declara_el_worker_del_driver(modulo):
    assert f'"{modulo}"' in SPEC.read_text(encoding="utf-8")


# =============================================================================
# Proceso descartable real
# =============================================================================


def _pedido(**cambios: object) -> PedidoDePaso:
    base: dict[str, object] = {
        "tipo": TipoDePaso.OBSERVAR,
        "tool": "TexGen",
        "exe_sha256": SHA_TEXGEN,
        "modo_de_juego": "SSE",
        "pid": 4242,
        "ejecutable": r"C:\Modding\DynDOLOD\TexGenx64.exe",
        "timeout_segundos": 0.5,
        "intervalo_segundos": 0.1,
        "fase": FaseDeObservacion.ARRANQUE,
        "accion_esperada": "texgen_start",
    }
    base.update(cambios)
    return PedidoDePaso(**base)  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform == "win32", reason="en Windows el helper real observaría de verdad")
async def test_el_helper_real_en_linux_responde_uia_no_disponible_sin_actuar():
    ejecutor = EjecutorDePasoPorHelper(gracia_externa_segundos=60.0)
    resultado = await ejecutor.ejecutar(
        _pedido(tipo=TipoDePaso.INVOCAR, fase=None, accion_esperada=None, accion_id="texgen_start")
    )
    assert resultado.estado is EstadoDePaso.UIA_NO_DISPONIBLE
    assert resultado.invocado is False


def _script(tmp_path: pathlib.Path, cuerpo: str) -> tuple[str, ...]:
    ruta = tmp_path / "helper_falso.py"
    ruta.write_text(textwrap.dedent(cuerpo), encoding="utf-8")
    return (sys.executable, str(ruta))


async def test_un_helper_colgado_se_mata_al_vencer_el_deadline_duro(tmp_path):
    comando = _script(tmp_path, "import time\ntime.sleep(60)\n")
    ejecutor = EjecutorDePasoPorHelper(comando=comando, gracia_externa_segundos=0.5)
    inicio = asyncio.get_running_loop().time()
    resultado = await ejecutor.ejecutar(_pedido(timeout_segundos=0.1, intervalo_segundos=0.1))
    assert resultado.estado is EstadoDePaso.SIN_RESPUESTA
    assert asyncio.get_running_loop().time() - inicio < 20


async def test_un_helper_que_sale_con_error_es_sin_respuesta(tmp_path):
    comando = _script(tmp_path, "import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    resultado = await EjecutorDePasoPorHelper(comando=comando).ejecutar(_pedido())
    assert resultado.estado is EstadoDePaso.SIN_RESPUESTA
    assert "boom" in resultado.detalle


async def test_un_helper_que_devuelve_basura_es_sin_respuesta(tmp_path):
    comando = _script(
        tmp_path,
        """
        import pathlib, sys
        (pathlib.Path(sys.argv[1]) / "resultado_paso.json").write_text("{no json", encoding="utf-8")
        """,
    )
    resultado = await EjecutorDePasoPorHelper(comando=comando).ejecutar(_pedido())
    assert resultado.estado is EstadoDePaso.SIN_RESPUESTA


async def test_una_instantanea_de_otro_pid_es_sin_respuesta(tmp_path):
    ajeno = json.loads(resultado_de_paso_a_json(ResultadoDePaso(estado=EstadoDePaso.OK)))
    ajeno["instantanea"] = {"pid": 1, "ventanas": [], "valor_output": None}
    comando = _script(
        tmp_path,
        f"""
        import pathlib, sys
        (pathlib.Path(sys.argv[1]) / "resultado_paso.json").write_text({json.dumps(json.dumps(ajeno))}, encoding="utf-8")
        """,
    )
    resultado = await EjecutorDePasoPorHelper(comando=comando).ejecutar(_pedido())
    assert resultado.estado is EstadoDePaso.SIN_RESPUESTA


async def test_el_helper_lee_el_pedido_y_el_canal_se_borra(tmp_path):
    registro = tmp_path / "visto.txt"
    comando = _script(
        tmp_path,
        f"""
        import json, pathlib, sys
        directorio = pathlib.Path(sys.argv[1])
        pedido = json.loads((directorio / "paso.json").read_text(encoding="utf-8"))
        pathlib.Path({str(registro)!r}).write_text(str(directorio) + "|" + pedido["tipo"], encoding="utf-8")
        (directorio / "resultado_paso.json").write_text({json.dumps(resultado_de_paso_a_json(ResultadoDePaso(estado=EstadoDePaso.PLAZO_AGOTADO)))}, encoding="utf-8")
        """,
    )
    resultado = await EjecutorDePasoPorHelper(comando=comando).ejecutar(_pedido())
    assert resultado.estado is EstadoDePaso.PLAZO_AGOTADO
    directorio, tipo = registro.read_text(encoding="utf-8").split("|")
    assert tipo == "OBSERVAR"
    assert not pathlib.Path(directorio).exists()

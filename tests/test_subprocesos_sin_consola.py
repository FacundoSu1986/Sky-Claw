"""Ancla: ningún lanzamiento de proceso del producto abre una consola visible.

El ``.exe`` congelado es ``console=False``. En Windows, un proceso sin consola que
lanza un programa de consola (``powershell``, ``icacls``, ``whoami``, ``wslpath``,
LOOT…) sin ``CREATE_NO_WINDOW`` le crea una consola NUEVA: el operador ve una ráfaga
de ventanas negras que parpadean. Al arrancar el bridge de MO2 eran unas veinte
(el broker endurece cuatro rutas con ``restrict_to_owner`` y cada una lanzaba de
dos a cinco procesos), y desde afuera eso parece un virus.

La propiedad del mecanismo: *la consola solo queda oculta si TODOS los
lanzamientos pasan el flag*. Este test enumera por AST cada llamada a
``subprocess.{run,Popen,…}`` y ``asyncio.create_subprocess_{exec,shell}`` bajo
``sky_claw/``. No muestrea: un lanzamiento nuevo sin ``creationflags`` lo rompe.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from sky_claw.sin_consola import CREATE_NO_WINDOW, creationflags_sin_consola

RAIZ_PAQUETE = Path(__file__).resolve().parent.parent / "sky_claw"

_SUBPROCESS = frozenset({"run", "Popen", "check_output", "check_call", "call", "getoutput", "getstatusoutput"})
_ASYNCIO = frozenset({"create_subprocess_exec", "create_subprocess_shell"})


def _alias_de_modulos(arbol: ast.AST) -> dict[str, str]:
    """Nombre local → módulo real, para ``import subprocess as sp`` y similares."""
    alias = {"subprocess": "subprocess", "asyncio": "asyncio"}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for nombre in nodo.names:
                if nombre.name in ("subprocess", "asyncio"):
                    alias[nombre.asname or nombre.name] = nombre.name
    return alias


def _nombre_de_lanzamiento(llamada: ast.Call, alias: dict[str, str]) -> str | None:
    f = llamada.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        modulo = alias.get(f.value.id)
        if modulo == "subprocess" and f.attr in _SUBPROCESS:
            return f"subprocess.{f.attr}"
        if modulo == "asyncio" and f.attr in _ASYNCIO:
            return f"asyncio.{f.attr}"
    if isinstance(f, ast.Name) and f.id in (_SUBPROCESS | _ASYNCIO):
        # Un ``from subprocess import run`` escondería el lanzamiento del censo.
        return f.id
    return None


def _diccionario_expandido(llamada: ast.Call) -> str | None:
    """Nombre del dict que se expande con ``**nombre`` en la llamada, si es un nombre simple."""
    for clave in llamada.keywords:
        if clave.arg is None and isinstance(clave.value, ast.Name):
            return clave.value.id
    return None


def _asigna_creationflags(funcion: ast.AST, diccionario: str) -> bool:
    """¿La función arma ``<diccionario>["creationflags"] = …`` sobre el MISMO dict que expande?"""
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Assign):
            for destino in nodo.targets:
                if (
                    isinstance(destino, ast.Subscript)
                    and isinstance(destino.value, ast.Name)
                    and destino.value.id == diccionario
                    and isinstance(destino.slice, ast.Constant)
                    and destino.slice.value == "creationflags"
                ):
                    return True
    return False


def _censo() -> list[tuple[str, int, str, bool]]:
    """(ruta, línea, lanzamiento, ¿oculta la consola?) para cada lanzamiento del paquete."""
    filas: list[tuple[str, int, str, bool]] = []
    for archivo in sorted(RAIZ_PAQUETE.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        ruta = archivo.relative_to(RAIZ_PAQUETE.parent).as_posix()
        filas.extend((ruta, linea, nombre, oculta) for linea, nombre, oculta in _lanzamientos(arbol))
    return filas


def _lanzamientos(arbol: ast.AST) -> list[tuple[int, str, bool]]:
    """(línea, lanzamiento, ¿oculta la consola?) para cada lanzamiento del árbol."""
    filas: list[tuple[int, str, bool]] = []
    alias = _alias_de_modulos(arbol)
    padres: dict[ast.AST, ast.AST] = {}
    for nodo in ast.walk(arbol):
        for hijo in ast.iter_child_nodes(nodo):
            padres[hijo] = nodo
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        nombre = _nombre_de_lanzamiento(nodo, alias)
        if nombre is None:
            continue
        claves = {k.arg for k in nodo.keywords}
        oculta = "creationflags" in claves
        diccionario = _diccionario_expandido(nodo) if not oculta else None
        if diccionario is not None:
            # ``**kwargs``: vale solo si la función fija el flag en ESE dict.
            # ``kwargs.update(...)`` u otras formas no cuentan: fallan cerrado.
            ancestro: ast.AST | None = padres.get(nodo)
            while ancestro is not None and not isinstance(ancestro, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ancestro = padres.get(ancestro)
            oculta = ancestro is not None and _asigna_creationflags(ancestro, diccionario)
        filas.append((nodo.lineno, nombre, oculta))
    return filas


def test_el_censo_encuentra_los_lanzamientos_conocidos() -> None:
    # Si el detector dejara de ver lanzamientos, el test de abajo pasaría en vacío.
    rutas = {ruta for ruta, *_ in _censo()}
    assert "sky_claw/app/security/file_permissions.py" in rutas
    assert "sky_claw/local/loot/cli.py" in rutas
    assert "sky_claw/local/tools/_process.py" in rutas


_ALIAS_SIN_FLAG = """
import subprocess as sp
sp.run(["x"])
"""
_ALIAS_CON_FLAG = """
import asyncio as aio
async def f():
    await aio.create_subprocess_exec("x", creationflags=1)
"""
_FLAG_EN_OTRO_DICT = """
import subprocess
def f():
    otro = {}
    otro["creationflags"] = 1
    kw = {}
    subprocess.run(["x"], **kw)
"""
_UPDATE_FALLA_CERRADO = """
import subprocess
def f():
    kw = {}
    kw.update(creationflags=1)
    subprocess.run(["x"], **kw)
"""
_FLAG_EN_EL_DICT_EXPANDIDO = """
import subprocess
def f():
    kw = {}
    kw["creationflags"] = 1
    subprocess.run(["x"], **kw)
"""
_IMPORT_DIRECTO = """
from subprocess import run
run(["x"])
"""


@pytest.mark.parametrize(
    ("fuente", "esperado"),
    [
        pytest.param(_ALIAS_SIN_FLAG, [(3, "subprocess.run", False)], id="alias_sin_flag"),
        pytest.param(_ALIAS_CON_FLAG, [(4, "asyncio.create_subprocess_exec", True)], id="alias_con_flag"),
        pytest.param(_FLAG_EN_OTRO_DICT, [(7, "subprocess.run", False)], id="flag_en_otro_dict"),
        pytest.param(_UPDATE_FALLA_CERRADO, [(6, "subprocess.run", False)], id="update_falla_cerrado"),
        pytest.param(_FLAG_EN_EL_DICT_EXPANDIDO, [(6, "subprocess.run", True)], id="flag_en_el_dict_expandido"),
        pytest.param(_IMPORT_DIRECTO, [(3, "run", False)], id="import_directo"),
    ],
)
def test_el_detector_ve_alias_y_exige_el_flag_en_el_dict_expandido(
    fuente: str, esperado: list[tuple[int, str, bool]]
) -> None:
    assert _lanzamientos(ast.parse(fuente)) == esperado


def test_todo_lanzamiento_del_paquete_oculta_la_consola() -> None:
    sin_flag = [f"{ruta}:{linea} {nombre}" for ruta, linea, nombre, oculta in _censo() if not oculta]
    assert sin_flag == [], (
        "Lanzamientos sin creationflags (abren una consola visible desde el .exe console=False). "
        "Pasá creationflags=creationflags_sin_consola():\n" + "\n".join(sin_flag)
    )


def test_creationflags_sin_consola_por_plataforma(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert creationflags_sin_consola() == CREATE_NO_WINDOW == 0x08000000
    assert creationflags_sin_consola(0x00000400) == 0x08000400
    # En POSIX ``creationflags`` distinto de 0 hace fallar a subprocess: siempre 0,
    # aun con un ``extra`` (todas esas constantes son de Windows).
    monkeypatch.setattr(sys, "platform", "linux")
    assert creationflags_sin_consola() == 0
    assert creationflags_sin_consola(0x00000400) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="CREATE_NO_WINDOW es de Windows")
def test_un_proceso_de_consola_lanzado_con_el_flag_no_recibe_consola() -> None:
    # Prueba real: el hijo pregunta si tiene ventana de consola.
    script = "import ctypes,sys; sys.stdout.write(str(int(bool(ctypes.windll.kernel32.GetConsoleWindow()))))"
    salida = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        creationflags=creationflags_sin_consola(),
    ).stdout
    assert salida == "0"

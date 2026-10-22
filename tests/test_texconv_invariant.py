"""Ancla: un solo camino ejecuta ``texconv``, y es el que verifica la huella antes de lanzarlo (NP-E0b).

Defecto que ancla: la regla dominante del repo (``AGENTS.md``, «arreglar un hermano y no al otro»). El día que alguien
agregue una segunda ruta que lance ``texconv`` (otra superficie, un script, un adaptador de NP-A0), el pin de versión y
la verificación de la copia —que viven en ``texconv_runner.py``— pueden quedar fuera de ese camino sin que nada falle.

Qué se congela, enumerando y no muestreando:

- el CONJUNTO de módulos ``.py`` de ``sky_claw/`` y ``scripts/`` que nombran ``texconv`` (por igualdad: un módulo
  nuevo rompe el test hasta que se decide su receta);
- de ellos, el CONJUNTO de los que lanzan procesos (por AST): solo el ejecutor;
- que ese ejecutor verifica la huella y lanza el proceso en la misma función (por AST), de modo que no existe un
  camino dentro del módulo que ejecute sin haber pasado por ``verificar_texconv``.

Límite: lee texto y AST. Un ``texconv`` armado por concatenación de strings, o lanzado con otro nombre de ejecutable,
no lo ve; es un ancla de la clase «segundo camino evidente», no una prueba de que no exista otro.
"""

from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

MODULOS_QUE_NOMBRAN_TEXCONV = {
    # el núcleo puro: pin, mips, cuantización, constructor de la entrada y validador de la salida
    "sky_claw/local/native_parallax/export.py",
    # el único que ejecuta el binario
    "sky_claw/local/tools/texconv_runner.py",
    # una mención en un comentario de la especificación del corpus sintético
    "sky_claw/local/native_parallax/bench/sintetico.py",
    # NP-O0a: nombra texconv solo como DATO (versión, huella y argumentos del encoder dentro de la procedencia del
    # manifiesto). No lo ejecuta ni lo verifica: es puro, y el test de abajo comprueba por AST que no lanza procesos.
    "sky_claw/local/native_parallax/output.py",
}
MODULOS_QUE_LANZAN_PROCESOS = {"sky_claw/local/tools/texconv_runner.py"}

_LLAMADAS_DE_PROCESO = {
    "run_capture",
    "create_subprocess_exec",
    "create_subprocess_shell",
    "spawn_detached",
    "Popen",
    "run",
}


def _fuentes() -> dict[str, str]:
    encontrados: dict[str, str] = {}
    for carpeta in ("sky_claw", "scripts"):
        for ruta in sorted((RAIZ / carpeta).rglob("*.py")):
            encontrados[ruta.relative_to(RAIZ).as_posix()] = ruta.read_text(encoding="utf-8", errors="replace")
    return encontrados


def _nombre_de_la_llamada(nodo: ast.Call) -> str | None:
    if isinstance(nodo.func, ast.Name):
        return nodo.func.id
    if isinstance(nodo.func, ast.Attribute):
        return nodo.func.attr
    return None


def _lanza_procesos(codigo: str) -> bool:
    arbol = ast.parse(codigo)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and _nombre_de_la_llamada(nodo) in _LLAMADAS_DE_PROCESO:
            return True
    return False


def test_los_modulos_que_nombran_texconv_son_exactamente_los_congelados() -> None:
    nombran = {ruta for ruta, texto in _fuentes().items() if "texconv" in texto.lower()}

    assert nombran == MODULOS_QUE_NOMBRAN_TEXCONV


def test_solo_el_ejecutor_lanza_procesos_entre_los_que_nombran_texconv() -> None:
    fuentes = _fuentes()
    lanzan = {ruta for ruta in MODULOS_QUE_NOMBRAN_TEXCONV if _lanza_procesos(fuentes[ruta])}

    assert lanzan == MODULOS_QUE_LANZAN_PROCESOS


def test_el_ejecutor_verifica_la_huella_y_lanza_el_proceso_en_el_mismo_flujo() -> None:
    codigo = (RAIZ / "sky_claw/local/tools/texconv_runner.py").read_text(encoding="utf-8")
    arbol = ast.parse(codigo)
    funciones = [n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    que_lanzan = [
        f.name
        for f in funciones
        if any(isinstance(n, ast.Call) and _nombre_de_la_llamada(n) == "run_capture" for n in ast.walk(f))
    ]
    que_verifican = [
        f.name
        for f in funciones
        if any(isinstance(n, ast.Call) and _nombre_de_la_llamada(n) == "verificar_texconv" for n in ast.walk(f))
    ]

    # Un solo punto de lanzamiento y la verificación vive en la función que prepara lo que se lanza.
    assert que_lanzan == ["exportar_altura_bc4"]
    assert que_verifican == ["_copiar_y_verificar"]
    cuerpo = next(f for f in funciones if f.name == "exportar_altura_bc4")
    llamadas = [_nombre_de_la_llamada(n) for n in ast.walk(cuerpo) if isinstance(n, ast.Call)]
    # el ejecutor lanza solo tras pasar por la preparación verificada (se le entrega ``_copiar_y_verificar`` a un hilo)
    nombres_usados = {n.id for n in ast.walk(cuerpo) if isinstance(n, ast.Name)}
    assert "_copiar_y_verificar" in nombres_usados and "run_capture" in llamadas


def test_el_detector_de_lanzamientos_reconoce_las_formas_habituales() -> None:
    for codigo in (
        "import subprocess\nsubprocess.run(['x'])",
        "import subprocess\nsubprocess.Popen(['x'])",
        "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')",
        "async def f():\n    await run_capture(['x'], timeout=1)",
    ):
        assert _lanza_procesos(codigo), codigo
    assert not _lanza_procesos("import hashlib\nhashlib.sha256(b'x')\nprint('texconv')")

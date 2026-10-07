"""Ancla: todo lo que ``start_full`` publica en ``AppContext`` se limpia al sanear.

Por qué existe. ``operator_chat_id`` era una variable LOCAL de ``start_full`` y la
consumían sólo los closures del HITL. Al publicarla como atributo (para que el
notificador de operador de DynDOLOD —que se instala fuera de ``start_full``— la lea
en cada envío) apareció la clase de defecto que este repo repite: publicarla en un
camino y olvidar el hermano que la limpia. Un atributo que ``start_full`` publica y
``_sanitize_full_references`` no limpia sobrevive a un ``stop()`` o a un
``start_full`` fallido, y el siguiente consumidor le habla a un chat (o a un sender)
de una sesión que ya no existe.

Los tres tests de ARC-03 de ``test_app_context_arc01_arc03.py`` verifican esto
SEMBRANDO a mano cada atributo conocido: atajan al que ya se conocía y no al
siguiente. Este archivo ENUMERA por AST (ver "La regla que más se viola" en
``AGENTS.md``): la propiedad es del mecanismo —*publicar y limpiar son simétricos*—
y no de cada atributo.
"""

from __future__ import annotations

import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP_CONTEXT = RAIZ / "sky_claw" / "app_context.py"

#: Igualdad literal, no subconjunto: es lo que atrapa al atributo nuevo que se publica.
PUBLICADOS_POR_START_FULL = {
    "_full_start_committed",
    "dyndolod_workspace",
    "hitl",
    "install_dir",
    "mo2",
    "mo2_install_dir",
    "mo2_profile",
    "operator_chat_id",
    "polling",
    "router",
    "sandbox_validator",
    "sender",
    "stage9_coordination",
    "sync_engine",
    "tools_installer",
    "vfs_broker",
    "vfs_instance_id",
    "vfs_loot_runner",
}


def _funcion(arbol: ast.AST, nombre: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    candidatas = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == nombre
    ]
    assert len(candidatas) == 1, f"se esperaba una única definición de {nombre}, hay {len(candidatas)}"
    return candidatas[0]


def _atributos_asignados_a_self(funcion: ast.AST) -> set[str]:
    """Nombres ``X`` de toda asignación ``self.X = ...`` (también anotada o aumentada) dentro de ``funcion``.

    ``ast.walk`` entra a los closures anidados: un ``self.X = ...`` dentro de un closure de
    ``start_full`` también es estado que ``start_full`` publica.
    """
    atributos: set[str] = set()
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Assign):
            objetivos = nodo.targets
        elif isinstance(nodo, (ast.AnnAssign, ast.AugAssign)):
            objetivos = [nodo.target]
        else:
            continue
        for objetivo in objetivos:
            if (
                isinstance(objetivo, ast.Attribute)
                and isinstance(objetivo.value, ast.Name)
                and objetivo.value.id == "self"
            ):
                atributos.add(objetivo.attr)
    return atributos


def _publicados_y_sanitizados() -> tuple[set[str], set[str]]:
    arbol = ast.parse(APP_CONTEXT.read_text(encoding="utf-8"))
    publicados = _atributos_asignados_a_self(_funcion(arbol, "_start_full_inner"))
    sanitizados = _atributos_asignados_a_self(_funcion(arbol, "_sanitize_full_references"))
    return publicados, sanitizados


def test_todo_lo_que_start_full_publica_se_limpia_en_sanear_referencias() -> None:
    publicados, sanitizados = _publicados_y_sanitizados()

    sin_limpiar = sorted(publicados - sanitizados)

    assert sin_limpiar == [], (
        f"start_full publica {sin_limpiar} y _sanitize_full_references no lo limpia: "
        "sobrevive a stop() y a un start_full fallido"
    )


def test_la_familia_que_start_full_publica_esta_congelada() -> None:
    publicados, _ = _publicados_y_sanitizados()

    assert publicados == PUBLICADOS_POR_START_FULL, (
        "start_full publica un conjunto distinto de atributos: actualizá PUBLICADOS_POR_START_FULL "
        "y verificá que el atributo nuevo también se limpia en _sanitize_full_references"
    )

"""Ancla del contrato ``SUMMARY`` en toda la familia que lo lee (#549).

Por qué existe: el protocolo ``SUMMARY|total_conflicts=...`` es la única
prueba de que el script de xEdit llegó a ``Finalize``. Durante la revisión de
#548 quedó una segunda superficie —el adapter legacy de la tool del agente—
que podía devolver un resumen vacío ``{}`` como si describiera la corrida,
mientras `ConflictAnalyzer` (la hermana del orquestador/GUI) fallaba
cerrado ante exactamente la misma ausencia. Dos superficies, un contrato: la
regla sólo vale si TODAS las rutas que leen el SUMMARY la cumplen.

Cómo está anclada (enumerar, no muestrear — ver "La regla que más se viola" en
``AGENTS.md``): el censo de funciones que llaman ``parse_summary_line`` se
descubre por AST sobre todo ``sky_claw/`` y se congela por igualdad literal. Un
segundo o tercer llamador —módulo nuevo o función nueva en un módulo ya
listado— rompe el censo y obliga a decidir si valida el conteo de xEdit o
declara la ausencia, en vez de heredar un ``{}`` que se lee como un resumen.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Censo congelado por igualdad literal: módulo -> funciones que leen el SUMMARY.
#: El dueño del parser (``parse_summary_line`` en ``conflict_analyzer``) no
#: está: definir la primitiva no es consumirla, y un ``{}`` ahí es el contrato
#: documentado de "línea ausente", que los consumidores tienen que detectar.
CENSO_LLAMADORES = {
    "sky_claw/app/agent/tools/xedit_readonly_tool.py": ["_ejecutar_conflictos"],
    "sky_claw/local/xedit/conflict_analyzer.py": ["analyze"],
    "sky_claw/local/xedit/grass_analyzer.py": ["_require_consistent_summary"],
}

#: Nombre de la primitiva cuyo resultado debe quedar guardado antes de usarse.
_PRIMITIVA = "parse_summary_line"

#: Frase mínima que debe seguir viva en la documentación canónica de la tool.
#: Sin este ancla, borrar la receta documental es invisible para CI.
_FRASE_CONTRATO_LEGACY = "`summary` es `None`"
_RUTAS_QUE_DOCUMENTAN_EL_CONTRATO = (
    Path("docs/api/tools_ref.md"),
    Path("docs/architecture/security_boundaries.md"),
)


def _archivos_del_paquete() -> list[Path]:
    """Todos los ``.py`` versionados de ``sky_claw/`` (sin exclusiones por tamaño)."""
    return sorted(p for p in PAQUETE.rglob("*.py") if "__pycache__" not in p.parts)


def _nombre_llamada(nodo: ast.Call) -> str:
    """``parse_summary_line`` / ``mod.parse_summary_line`` -> el nombre final."""
    func = nodo.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _funciones_que_llaman(arbol: ast.Module, nombre: str) -> list[str]:
    """Funciones (sync o async) cuyo cuerpo contiene la llamada ``nombre``."""
    encontradas: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for interno in ast.walk(nodo):
            if isinstance(interno, ast.Call) and _nombre_llamada(interno) == nombre:
                encontradas.append(nodo.name)
                break
    return encontradas


def _llamadores_reales() -> dict[str, list[str]]:
    """Descubre el censo por AST sobre todo el paquete de producción."""
    reales: dict[str, list[str]] = {}
    for archivo in _archivos_del_paquete():
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        funciones = _funciones_que_llaman(arbol, _PRIMITIVA)
        if funciones:
            clave = archivo.relative_to(RAIZ).as_posix()
            reales[clave] = sorted(set(funciones))
    return reales


def _asignaciones_de_primitiva(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Nombres locales a los que se asigna el resultado de ``parse_summary_line``."""
    objetivos: set[str] = set()
    for nodo in ast.walk(funcion):
        if not isinstance(nodo, ast.Assign):
            continue
        if isinstance(nodo.value, ast.Call) and _nombre_llamada(nodo.value) == _PRIMITIVA:
            for destino in nodo.targets:
                if isinstance(destino, ast.Name):
                    objetivos.add(destino.id)
    return objetivos


def _tiene_guard_de_pertenencia(funcion: ast.FunctionDef | ast.AsyncFunctionDef, nombres: set[str]) -> bool:
    """``if clave not in resumen: raise ...`` sobre alguna de esas asignaciones."""
    for nodo in ast.walk(funcion):
        if not isinstance(nodo, ast.If):
            continue
        prueba: Any = nodo.test
        if (
            isinstance(prueba, ast.Compare)
            and len(prueba.comparators) == 1
            and isinstance(prueba.ops[0], ast.NotIn)
            and isinstance(prueba.comparators[0], ast.Name)
            and prueba.comparators[0].id in nombres
            and any(isinstance(hijo, ast.Raise) for sentencia in nodo.body for hijo in ast.walk(sentencia))
        ):
            return True
    return False


def _funcion_de_arbol(arbol: ast.Module, nombre: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.AsyncFunctionDef | ast.FunctionDef) and nodo.name == nombre:
            return nodo
    raise AssertionError(f"no existe la función {nombre!r} en el módulo analizado")


def _resumenes_vacios_como_payload(arbol: ast.Module) -> list[int]:
    """Líneas que devuelven ``{"summary": {}}``: la forma exacta del defecto."""
    culpables: list[int] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Dict):
            continue
        for clave, valor in zip(nodo.keys, nodo.values, strict=True):
            if (
                isinstance(clave, ast.Constant)
                and clave.value == "summary"
                and isinstance(valor, ast.Dict)
                and not valor.keys
            ):
                culpables.append(nodo.lineno)
    return culpables


def test_el_censo_de_llamadores_al_summary_esta_congelado() -> None:
    """Nadie lee el protocolo por fuera de la lista; un llamador nuevo rompe acá."""
    # Preparar / Actuar
    reales = _llamadores_reales()

    # Verificar
    assert reales == CENSO_LLAMADORES, (
        "el censo de funciones que llaman `parse_summary_line` cambió: "
        f"descubierto {reales}, congelado {CENSO_LLAMADORES}. Decidi si la "
        "nueva ruta valida el conteo de xEdit o declara la ausencia, y recién "
        "entonces actualizá la lista."
    )


def test_toda_funcion_que_lee_el_summary_valida_su_conteo() -> None:
    """Cada llamador falla cerrado si la clave contadora no está: cero ``{}`` sin dueño."""
    for ruta_relativa, funciones in CENSO_LLAMADORES.items():
        # Preparar
        arbol = ast.parse((RAIZ / ruta_relativa).read_text(encoding="utf-8"), filename=ruta_relativa)

        for nombre in funciones:
            funcion = _funcion_de_arbol(arbol, nombre)
            nombres = _asignaciones_de_primitiva(funcion)

            # Actuar / Verificar
            assert nombres, f"{ruta_relativa}::{nombre} lee el SUMMARY sin asignarlo a un nombre verificable"
            assert _tiene_guard_de_pertenencia(funcion, nombres), (
                f"{ruta_relativa}::{nombre} usa el resultado de `{_PRIMITIVA}` sin "
                "un `if <clave> not in resumen: raise`: un SUMMARY ausente debe "
                "fallar cerrado o declararse como ausencia, nunca pasar por resumen."
            )


def test_ningun_payload_devuelve_un_summary_vacio() -> None:
    """``{"summary": {}}`` está prohibido en la familia: es la ambigüedad de #549."""
    for ruta_relativa in CENSO_LLAMADORES:
        # Preparar
        arbol = ast.parse((RAIZ / ruta_relativa).read_text(encoding="utf-8"), filename=ruta_relativa)

        # Actuar / Verificar
        culpables = _resumenes_vacios_como_payload(arbol)
        assert not culpables, f'{ruta_relativa} devuelve un {{"summary": {{}}}} en las líneas {culpables}'


def test_el_contrato_queda_documentado_en_las_fuentes_canonicas() -> None:
    """La receta de la rama legacy no se borra en silencio: vive en la documentación."""
    for ruta in _RUTAS_QUE_DOCUMENTAN_EL_CONTRATO:
        # Preparar / Actuar
        texto = (RAIZ / ruta).read_text(encoding="utf-8")

        # Verificar
        assert _FRASE_CONTRATO_LEGACY in texto, f"{ruta} dejó de documentar el contrato de `summary` (#549)"

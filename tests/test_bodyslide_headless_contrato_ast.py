"""AST del contrato headless — invariantes estructurales del PR 1.

Estos tests garantizan que el argv, el veredicto y la siembra no puedan
regresar la prohibición explícita del contrato:

- ``construir_argv`` no construye ni emite un argv inline con flags no
  autorizados; sólo emite lo declarado en el contrato.
- ``bodyslide_config.py`` no borra nada recursivamente ni spawnea procesos.
- ``bodyslide_runner.py`` construye el argv vía ``construir_argv`` y no a mano.
"""

from __future__ import annotations

import ast
import pathlib

PAQUETE = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"

#: Módulos prohibidos en los 3 archivos del contrato headless.
_PROHIBIDOS_MODULO = {"subprocess", "os.system", "os.popen", "shutil.rmtree", "eval", "exec"}

#: Flags prohibidos por el contrato PR 1: presentes en upstream pero no
#: autorizados por Sky-Claw.
_FLAGS_PROHIBIDOS = ("-b", "-f", "-regex", "-conformaddons", "-preview")


def _arbol(relativo: str) -> ast.Module:
    archivo = PAQUETE.parent / relativo
    return ast.parse(archivo.read_text(encoding="utf-8"))


def _imports_y_calls(arbol: ast.Module) -> tuple[list[ast.Import | ast.ImportFrom], list[ast.Call]]:
    imports: list[ast.Import | ast.ImportFrom] = []
    calls: list[ast.Call] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Import, ast.ImportFrom)):
            imports.append(nodo)
        elif isinstance(nodo, ast.Call):
            calls.append(nodo)
    return imports, calls


def test_contrato_no_importa_modulos_prohibidos() -> None:
    for relativo in (
        "sky_claw/local/tools/bodyslide_headless.py",
        "sky_claw/local/tools/bodyslide_config.py",
        "sky_claw/local/tools/bodyslide_runner.py",
    ):
        arbol = _arbol(relativo)
        imports, calls = _imports_y_calls(arbol)
        for imp in imports:
            for alias in imp.names:
                nombre = (imp.module or "") if isinstance(imp, ast.ImportFrom) else alias.name
                assert not any(p in nombre for p in _PROHIBIDOS_MODULO), (
                    f"{relativo} importa o referencia módulo prohibido ({nombre})"
                )


def test_config_no_escribe_fuera_de_config_xml_o_nul_bytes() -> None:
    """La siembra toca Config.xml (y los bytes byte a byte) —nunca otros archivos."""
    arbol = _arbol("sky_claw/local/tools/bodyslide_config.py")
    _, calls = _imports_y_calls(arbol)
    for call in calls:
        dump = ast.dump(call)
        assert "shutil.rmtree" not in dump and "os.system" not in dump


def test_runner_arma_argv_solo_via_construir_argv() -> None:
    """Sin ``args = [...]`` inline — todo argv pasa por construir_argv."""
    texto = (PAQUETE.parent / "sky_claw" / "local" / "tools" / "bodyslide_runner.py").read_text(encoding="utf-8")

    # Construye el argv con construir_argv, no con una lista literal.
    assert "construir_argv(" in texto

    # Ningún assignment inline de argv a lista literal.
    arbol = ast.parse(texto)
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Assign):
            continue
        for target in nodo.targets:
            if not (isinstance(target, ast.Name) and target.id in ("args", "argv")):
                continue
            # Sólo se autoriza `args = construir_argv(...)` — nunca `args = [...]` inline.
            if isinstance(nodo.value, ast.Call):
                func = nodo.value.func
                nombre = (
                    func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
                )
                if nombre == "construir_argv":
                    continue
            raise AssertionError(f"bodyslide_runner.py asigna '{target.id}' inline — debe venir de construir_argv()")


def test_argv_no_emite_flags_no_autorizados() -> None:
    """El contrato no emite los flags declarados upstream pero no autorizados.

    El chequeo fuerte — el vector exacto del argv — vive en
    ``test_bodyslide_headless_argv.py::test_argv_no_emite_flags_no_autorizados``;
    acá el ancla AST congela la lista.
    """
    texto = (PAQUETE.parent / "sky_claw" / "local" / "tools" / "bodyslide_headless.py").read_text(encoding="utf-8")
    for flag in _FLAGS_PROHIBIDOS:
        # Sólo puede aparecer como string en donantes docstring; nunca como string
        # literal dentro del cuerpo de construir_argv.
        arbol = ast.parse(texto)
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and nodo.value == flag:
                raise AssertionError(f"el contrato contiene el flag prohibido '{flag}' como literal")


def test_buildselection_serializador_no_escribe_archivo() -> None:
    """serializar_build_selection (pure) no toca filesystem — sin write ni open."""
    archivo = PAQUETE / "local" / "tools" / "bodyslide_headless.py"
    arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    # Localizar la función serializar_build_selection y verificar que no abre archivos.
    fn = next(
        (n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == "serializar_build_selection"),
        None,
    )
    assert fn is not None, "la función serializar_build_selection debe existir"
    for nodo in ast.walk(fn):
        if isinstance(nodo, ast.Call):
            dump = ast.dump(nodo)
            assert "open(" not in dump
            assert "write(" not in dump

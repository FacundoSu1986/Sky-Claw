"""Ancla del conjunto de consumidores del parser XML de tool calls.

Por qué existe: el hardening de `arguments` (PR #458) cambió el contrato del
parser: un payload con `arguments` falsy (`false|0|""|[]|null`) que antes se
normalizaba a `{}` y ejecutaba la tool con argumentos vacíos ahora lanza
`ValueError`. El único consumidor de producción que absorbe ese `ValueError` es
`router.py` (circuito de retry self-healing con presupuesto
`MAX_XML_TOOL_RETRIES`, feedback sanitizado al LLM) — la superficie GUI
(`SupervisorAgent` → `tool_dispatcher`) despacha payloads estructurados y nunca
parsea texto `<tool_call>`, y no existe reprocesamiento de historial que
re-parse los marcadores (el patrón de `security_policy.yaml` es detección, no
parsing).

Estos tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): congelan **qué** módulos de `sky_claw/` importan el parser
canónico (`xml_tool_call_parser`) o su shim legado (`hermes_parser`), y **qué**
símbolos traen — incluyendo la canonicalización de símbolos re-exportados por
namespaces intermedios (p. ej. `from ...router import extract_tool_calls`). Un
consumidor nuevo —módulo nuevo, import adicional o re-export de un símbolo del
parser— rompe el test hasta que se le escriba su receta de manejo de
`ValueError` (test de comportamiento) y se lo agregue acá con esa receta
documentada.
"""

from __future__ import annotations

import ast
from pathlib import Path

PAQUETE = Path(__file__).resolve().parents[1] / "sky_claw"

MODULOS_RAIZ_DEL_PARSER = {"xml_tool_call_parser", "hermes_parser"}

# Símbolos que el parser expone: un import de cualquiera de estos desde un
# namespace que los re-exporta consume el contrato del parser igual que un
# import directo del módulo raíz.
SIMBOLOS_CANONICOS = {"extract_tool_calls", "has_tool_calls", "TOOL_CALL_RE"}

# Consumidores esperados: módulo → símbolos importados del parser, con su
# receta de manejo de ValueError:
# - router.py: try/except ValueError + presupuesto MAX_XML_TOOL_RETRIES +
#   feedback sanitizado al LLM. Anclado en tests/test_xml_tool_call_router.py
#   (test_arguments_falsy_se_reinyecta_como_error_y_recupera y
#   test_arguments_falsy_persistente_agota_presupuesto_de_parse).
# - hermes_parser.py: shim de compatibilidad que re-exporta los símbolos
#   canónicos sin consumirlos (identidad de objetos anclada por
#   test_legacy_parser_module_reexports_canonical_symbols).
CONSUMIDORES_ESPERADOS = {
    "sky_claw/app/agent/router.py": {"extract_tool_calls", "has_tool_calls"},
    "sky_claw/app/agent/hermes_parser.py": {"TOOL_CALL_RE", "extract_tool_calls", "has_tool_calls"},
}


def _ruta_de_modulo(nombre_modulo: str | None, archivo: str) -> str | None:
    """Resuelve el `module` de un ImportFrom a la ruta del archivo en el árbol.

    Soporta absolutos (`sky_claw.app.agent.router`) y relativos (`.agent.router`
    o `..`) resueltos desde el paquete de *archivo*. Devuelve None si no hay
    módulo (imports sin nombre no existen para ImportFrom).
    """
    if not nombre_modulo:
        return None
    partes_paquete = list(Path(archivo).parent.parts)
    if nombre_modulo.startswith("."):
        puntos = len(nombre_modulo) - len(nombre_modulo.lstrip("."))
        base = partes_paquete[: len(partes_paquete) - (puntos - 1)]
        partes = nombre_modulo.lstrip(".").split(".")
        if not partes or partes == [""]:
            return "/".join(base + ["__init__.py"])
        return "/".join(base + partes) + ".py"
    return nombre_modulo.replace(".", "/") + ".py"


def _simbolos_importados(
    arbol: ast.AST,
    namespaces_con_simbolos: set[str] | None = None,
    archivo: str = "",
) -> set[str]:
    """Símbolos del parser que el módulo importa (directo, vía paquete o re-export).

    Cubre: `from ...xml_tool_call_parser import X`, `from ...agent import
    xml_tool_call_parser` (el submódulo queda ligado al paquete),
    `import ...xml_tool_call_parser [as alias]`, y —canonicalización— un símbolo
    canónico importado desde un namespace que lo re-exporta (`from ...router
    import extract_tool_calls`), que consume el contrato aunque no nombre al
    módulo raíz. *namespaces_con_simbolos* es el conjunto de rutas de módulos
    cuyo namespace expone símbolos del parser.
    """
    simbolos: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            modulo = (nodo.module or "").split(".")
            if modulo[-1] in MODULOS_RAIZ_DEL_PARSER:
                simbolos.update(alias.name for alias in nodo.names)
                continue
            for alias in nodo.names:
                if alias.name in MODULOS_RAIZ_DEL_PARSER:
                    simbolos.add(alias.asname or alias.name)
                    continue
                destino = _ruta_de_modulo(nodo.module, archivo)
                if namespaces_con_simbolos and destino in namespaces_con_simbolos and alias.name in SIMBOLOS_CANONICOS:
                    simbolos.add(alias.asname or alias.name)
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                nombre = alias.name.split(".")[-1]
                if nombre in MODULOS_RAIZ_DEL_PARSER:
                    simbolos.add(alias.asname or nombre)
    return simbolos


def _namespaces_con_simbolos(arboles: dict[str, ast.AST]) -> set[str]:
    """Rutas de módulos cuyo namespace expone símbolos del parser.

    El parser raíz y todo módulo que importe de él (re-export incidental o
    deliberado, como `router.py` o el shim). Un paquete que re-exporte en su
    `__init__` aparecerá como consumidor directo y romperá el ancla en su punto
    de extensión antes de poder servir de re-export.
    """
    namespaces = {"sky_claw/app/agent/xml_tool_call_parser.py"}
    for ruta, arbol in arboles.items():
        if _simbolos_importados(arbol):
            namespaces.add(ruta)
    return namespaces


def _consumidores_reales() -> dict[str, set[str]]:
    """Escanea el árbol e indexa cada módulo por los símbolos que importa del parser."""
    arboles: dict[str, ast.AST] = {}
    for archivo in PAQUETE.rglob("*.py"):
        ruta = archivo.relative_to(PAQUETE.parent).as_posix()
        arboles[ruta] = ast.parse(archivo.read_text(encoding="utf-8"))
    namespaces = _namespaces_con_simbolos(arboles)
    consumidores: dict[str, set[str]] = {}
    for ruta, arbol in arboles.items():
        simbolos = _simbolos_importados(arbol, namespaces, ruta)
        if simbolos:
            consumidores[ruta] = simbolos
    return consumidores


def test_detector_detecta_todas_las_formas_de_import() -> None:
    """Las formas válidas de importar el parser deben ser visibles para el ancla.

    Blind spots del review: `from sky_claw.app.agent import xml_tool_call_parser`
    deja `module` en `agent` y el submódulo en `names`; y un símbolo del parser
    re-exportado por un namespace intermedio (`from ...router import
    extract_tool_calls`) no nombra al módulo raíz. Un consumidor escrito así
    quedaría fuera de la congelación sin canonicalización.
    """
    namespaces_con_simbolos = {
        "sky_claw/app/agent/router.py",
        "sky_claw/app/agent/hermes_parser.py",
    }
    muestras = {
        "directo": ("from sky_claw.app.agent.xml_tool_call_parser import extract_tool_calls", frozenset()),
        "paquete": ("from sky_claw.app.agent import xml_tool_call_parser", frozenset()),
        "paquete_con_alias": ("from sky_claw.app.agent import xml_tool_call_parser as p", frozenset()),
        "import_punto": ("import sky_claw.app.agent.xml_tool_call_parser as p", frozenset()),
        "shim": ("from sky_claw.app.agent import hermes_parser", frozenset()),
        "reexport_router": (
            "from sky_claw.app.agent.router import extract_tool_calls",
            frozenset(namespaces_con_simbolos),
        ),
        "reexport_shim": (
            "from sky_claw.app.agent.hermes_parser import has_tool_calls",
            frozenset(namespaces_con_simbolos),
        ),
        "simbolo_ajeno_al_parser": (
            "from sky_claw.app.agent.router import LLMRouter",
            frozenset(namespaces_con_simbolos),
        ),
    }
    for nombre, (codigo, namespaces) in muestras.items():
        simbolos = _simbolos_importados(ast.parse(codigo), namespaces)
        if nombre == "simbolo_ajeno_al_parser":
            assert not simbolos, f"falso positivo: {nombre} ({codigo!r})"
        else:
            assert simbolos, f"forma de import no detectada: {nombre} ({codigo!r})"


def test_consumidores_del_parser_congelados() -> None:
    """Un consumidor nuevo del parser debe traer su receta de manejo de ValueError."""
    assert _consumidores_reales() == CONSUMIDORES_ESPERADOS

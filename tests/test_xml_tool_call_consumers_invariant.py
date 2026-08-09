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
símbolos traen. Un consumidor nuevo —módulo nuevo o un import adicional— rompe
el test hasta que se le escriba su receta de manejo de `ValueError` (test de
comportamiento) y se lo agregue acá con esa receta documentada.
"""

from __future__ import annotations

import ast
from pathlib import Path

PAQUETE = Path(__file__).resolve().parents[1] / "sky_claw"

MODULOS_RAIZ_DEL_PARSER = {"xml_tool_call_parser", "hermes_parser"}

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


def _simbolos_importados(arbol: ast.AST) -> set[str]:
    """Símbolos que el módulo importa del parser (directo, vía paquete o shim).

    Cubre las tres formas válidas: `from ...xml_tool_call_parser import X`,
    `from ...agent import xml_tool_call_parser` (el submódulo queda ligado al
    paquete) e `import ...xml_tool_call_parser [as alias]`.
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
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                nombre = alias.name.split(".")[-1]
                if nombre in MODULOS_RAIZ_DEL_PARSER:
                    simbolos.add(alias.asname or nombre)
    return simbolos


def _consumidores_reales() -> dict[str, set[str]]:
    """Escanea el árbol e indexa cada módulo por los símbolos que importa del parser."""
    consumidores: dict[str, set[str]] = {}
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        simbolos = _simbolos_importados(arbol)
        if simbolos:
            consumidores[archivo.relative_to(PAQUETE.parent).as_posix()] = simbolos
    return consumidores


def test_detector_detecta_todas_las_formas_de_import() -> None:
    """Las formas válidas de importar el parser deben ser visibles para el ancla.

    Blind spot del review: `from sky_claw.app.agent import xml_tool_call_parser`
    deja `module` en `agent` y el submódulo en `names` — un detector que solo
    mira el último componente de `module` no lo ve, y un consumidor escrito así
    quedaría fuera de la congelación.
    """
    muestras = {
        "directo": "from sky_claw.app.agent.xml_tool_call_parser import extract_tool_calls",
        "paquete": "from sky_claw.app.agent import xml_tool_call_parser",
        "paquete_con_alias": "from sky_claw.app.agent import xml_tool_call_parser as p",
        "import_punto": "import sky_claw.app.agent.xml_tool_call_parser as p",
        "shim": "from sky_claw.app.agent import hermes_parser",
    }
    for nombre, codigo in muestras.items():
        simbolos = _simbolos_importados(ast.parse(codigo))
        assert simbolos, f"forma de import no detectada: {nombre} ({codigo!r})"


def test_consumidores_del_parser_congelados() -> None:
    """Un consumidor nuevo del parser debe traer su receta de manejo de ValueError."""
    assert _consumidores_reales() == CONSUMIDORES_ESPERADOS

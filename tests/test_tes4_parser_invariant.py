"""Ancla del invariante de parsing binario TES4.

Por qué existe: en agosto de 2026 se propuso integrar un módulo "Wrye Bash
headless" que traía su propio lector de headers TES4 (`BinaryReader` +
`TES4Header`). Era una reimplementación peor de
`sky_claw/local/validators/plugin_header.py` —decodificaba `latin1` en vez de
`cp1252`, no chequeaba límites al avanzar el offset de subrecords (devolvía
masters basura como `'Skyrim.esm\\x00DATA\\x08'` **sin lanzar excepción**), no
manejaba `XXXX` y descartaba `form_version`— y nada en el repo lo habría hecho
fallar: ningún gate detecta un segundo parser binario.

Este test enumera en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): congela **qué módulos** desempaquetan estructuras binarias con la
firma `TES4`. Un parser nuevo —o un segundo camino de parsing dentro de un
módulo ya listado— rompe acá hasta que se decida explícitamente si delega en
`read_plugin_header` o si es una exención consciente con su motivo escrito.

El detector busca la señal que ningún parser TES4 puede evitar: la firma
literal `TES4` (como `b"TES4"` o `"TES4"`) en un módulo que además desempaqueta
bytes (`struct.unpack*` o `int.from_bytes`). Un módulo que solo *menciona* TES4
en un docstring no cuenta; uno que desempaqueta binario sin tocar TES4 tampoco.
"""

from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

# Módulos que legítimamente parsean el header TES4, con su motivo. El dueño
# canónico es uno solo; cualquier agregado debería ser una decisión, no un
# descuido — de ahí que el motivo viva en el propio dict y no en un comentario
# suelto (mismo patrón que `SERVICIOS_SIN_SALIDA` en `test_output_targets.py`).
PARSERS_TES4_ESPERADOS: dict[str, str] = {
    "sky_claw/local/validators/plugin_header.py": (
        "Dueño canónico del parser TES4. Todo consumidor (missing_masters, "
        "plugin_limits, master_order) delega en read_plugin_header()."
    ),
}

#: Formas de desempaquetar bytes que delatan un parser binario.
_DESEMPAQUETADORES_STRUCT = {"unpack", "unpack_from"}
_METODO_INT = "from_bytes"

#: La firma del record que abre todo plugin de Bethesda.
_FIRMA = "TES4"


def _menciona_firma_tes4(arbol: ast.AST) -> bool:
    """True si aparece la firma `TES4` como literal de código (no en docstring).

    Los docstrings son `ast.Constant` dentro de `ast.Expr` a nivel de módulo,
    clase o función; se excluyen para que documentar el formato no cuente como
    implementarlo.
    """
    docstrings = {
        id(cuerpo[0].value)
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and (cuerpo := nodo.body)
        and isinstance(cuerpo[0], ast.Expr)
        and isinstance(cuerpo[0].value, ast.Constant)
    }
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Constant) or id(nodo) in docstrings:
            continue
        valor = nodo.value
        if isinstance(valor, bytes) and _FIRMA.encode() in valor:
            return True
        if isinstance(valor, str) and _FIRMA in valor:
            return True
    return False


def _aliases_de_struct(arbol: ast.AST) -> set[str]:
    """Nombres con que este módulo puede llamar a `unpack`/`unpack_from`.

    `from struct import unpack as decode` liga el desempaquetador a `decode`, y
    sin resolverlo el ancla se esquiva renombrando el import — que es la forma
    natural de escribirlo, no una evasión rebuscada.
    """
    return {
        alias.asname or alias.name
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.ImportFrom) and nodo.module == "struct"
        for alias in nodo.names
        if alias.name in _DESEMPAQUETADORES_STRUCT
    }


def _desempaqueta_binario(arbol: ast.AST) -> bool:
    """True si el módulo llama a `struct.unpack*` o `<algo>.from_bytes(...)`.

    Se matchea por nombre de atributo/función y no por el módulo importado: lo
    que importa es que haya conversión de bytes a enteros, escrita como se
    escriba (`struct.unpack`, `unpack_from` importado directo o con alias,
    `int.from_bytes`).
    """
    nombres_directos = _DESEMPAQUETADORES_STRUCT | _aliases_de_struct(arbol)
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        func = nodo.func
        if isinstance(func, ast.Attribute) and (func.attr in _DESEMPAQUETADORES_STRUCT or func.attr == _METODO_INT):
            return True
        if isinstance(func, ast.Name) and func.id in nombres_directos:
            return True
    return False


def _es_parser_tes4(fuente: str) -> bool:
    arbol = ast.parse(fuente)
    return _menciona_firma_tes4(arbol) and _desempaqueta_binario(arbol)


def _parsers_tes4_del_paquete() -> set[str]:
    return {
        archivo.relative_to(RAIZ).as_posix()
        for archivo in PAQUETE.rglob("*.py")
        if _es_parser_tes4(archivo.read_text(encoding="utf-8"))
    }


# ---------------------------------------------------------------------------
# Meta-tests del detector: el ancla no sirve si se la esquiva
# ---------------------------------------------------------------------------


def test_detector_reconoce_las_formas_de_desempaquetar() -> None:
    assert _es_parser_tes4("import struct\nstruct.unpack('<4sI', b'TES4')\n")
    assert _es_parser_tes4("import struct\nstruct.unpack_from('<4sI', d, 0)\nS = b'TES4'\n")
    assert _es_parser_tes4("from struct import unpack\nunpack('<4sI', b'TES4')\n")
    # `int.from_bytes` es la forma que usa el parser canónico del repo.
    assert _es_parser_tes4("d = b''\nif d[:4] == b'TES4':\n    int.from_bytes(d[4:8], 'little')\n")
    # La firma como str también cuenta (el snippet auditado decodificaba y comparaba).
    assert _es_parser_tes4("import struct\nt, = struct.unpack('<4s', d)\nif t.decode() == 'TES4':\n    pass\n")
    # Alias del import: la forma que esquivaba el ancla (review Copilot/CodeRabbit PR #434).
    assert _es_parser_tes4(
        "from struct import unpack as decode\nt, _ = decode('<4sI', d)\nif t == b'TES4':\n    pass\n"
    )
    assert _es_parser_tes4("from struct import unpack_from as leer\nt, = leer('<4s', d, 0)\nS = b'TES4'\n")
    # El alias solo vale si viene de `struct`: un homónimo de otro módulo no cuenta.
    assert not _es_parser_tes4("from otra_lib import unpack as decode\ndecode('x')\nS = b'TES4'\n")


def test_detector_ignora_menciones_en_docstrings() -> None:
    """Documentar el formato TES4 no es implementarlo."""
    solo_doc = '"""Lee el record TES4 de un plugin."""\nimport struct\nstruct.unpack("<I", b"")\n'
    assert not _es_parser_tes4(solo_doc)
    # También en docstring de función y de clase.
    en_funcion = 'import struct\ndef f():\n    """Header TES4."""\n    return struct.unpack("<I", b"")\n'
    assert not _es_parser_tes4(en_funcion)
    en_clase = (
        'import struct\nclass C:\n    """Header TES4."""\n    def m(self):\n        return struct.unpack("<I", b"")\n'
    )
    assert not _es_parser_tes4(en_clase)


def test_detector_requiere_las_dos_señales() -> None:
    """Ni desempaquetar binario ajeno ni nombrar TES4 alcanzan por separado."""
    # Binario sin TES4: hay muchos (headers de BSA, hashes, protocolos).
    assert not _es_parser_tes4("import struct\nstruct.unpack('<I', b'')\n")
    # TES4 sin desempaquetar: constantes, mensajes, nombres de archivo.
    assert not _es_parser_tes4("FIRMA = b'TES4'\n")
    assert not _es_parser_tes4("msg = 'no es un plugin TES4 válido'\n")


def test_detector_reconoce_el_parser_canonico() -> None:
    """Prueba de vida: el dueño canónico debe activar el detector."""
    canonico = (PAQUETE / "local" / "validators" / "plugin_header.py").read_text(encoding="utf-8")

    assert _es_parser_tes4(canonico)


def test_detector_reconoce_el_snippet_auditado() -> None:
    """El módulo "Wrye Bash headless" propuesto habría roto el ancla.

    Reproduce la estructura del `TES4Header._parse_header` auditado. Es la
    afirmación que le da sentido al archivo: si esto no se detecta, el ancla no
    ataja el caso que la motivó.
    """
    snippet = (
        "import struct\n"
        "class TES4Header:\n"
        "    def _parse_header(self, f):\n"
        "        raw = f.read(24)\n"
        "        rec_type, size, flags, form_id, vc, version = struct.unpack('<4sIIIIH', raw[:22])\n"
        "        if rec_type.decode('latin1') != 'TES4':\n"
        "            raise ValueError('no es un plugin')\n"
    )

    assert _es_parser_tes4(snippet)


# ---------------------------------------------------------------------------
# El ancla
# ---------------------------------------------------------------------------


def test_los_parsers_tes4_estan_congelados() -> None:
    """Igualdad literal: un parser binario TES4 nuevo rompe acá."""
    assert _parsers_tes4_del_paquete() == set(PARSERS_TES4_ESPERADOS), (
        "Cambió el inventario de módulos que parsean el header TES4. Si es "
        "código nuevo, delegá en "
        "sky_claw.local.validators.plugin_header.read_plugin_header() en vez de "
        "abrir un segundo parser: el duplicado es exactamente el defecto que "
        "este ancla existe para atajar (latin1 vs cp1252, offsets sin bounds "
        "check, XXXX sin manejar). Si de verdad hace falta otro parser, agregalo "
        "a PARSERS_TES4_ESPERADOS con el motivo escrito."
    )


def test_los_consumidores_delegan_en_el_parser_canonico() -> None:
    """Los sensores que necesitan headers importan, no reimplementan."""
    consumidores = [
        "local/validators/missing_masters.py",
        "local/validators/plugin_limits.py",
        "local/validators/master_order.py",
    ]
    for relativo in consumidores:
        fuente = (PAQUETE / relativo).read_text(encoding="utf-8")
        assert "read_plugin_header" in fuente, f"{relativo} no delega en el parser canónico"
        assert relativo not in _parsers_tes4_del_paquete()

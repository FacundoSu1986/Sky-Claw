"""Ancla del invariante de conexiones SQLite.

Por qué existe: `.github/coding_conventions.md` y `CONTRIBUTING.md` sostuvieron
durante meses que las conexiones se manejaban con `threading.local()`. Nunca fue
cierto en este árbol —la DB es `aiosqlite` y la conexión la entrega
`DatabaseLifecycleManager`— y nada lo hacía fallar, así que la regla falsa
sobrevivió a cada lectura.

Estos tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): congelan **cuántas** llamadas a conector hay en **cada** módulo que
conecta por su cuenta. Un conector nuevo —módulo nuevo, o una llamada más dentro
de un módulo ya listado— rompe el test hasta que se decida explícitamente si va
por el lifecycle o si es otra exención consciente.
"""

from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

# Módulos de los que sí se espera que conecten, con la cantidad exacta de
# llamadas. Congelar el conteo y no solo el nombre: agregar un segundo
# `connect()` dentro de un módulo ya listado es el punto de extensión más
# probable, y con un set de nombres pasaría sin romper nada.
#
# `db_lifecycle.py` es el dueño canónico. El resto se divide en dos clases, según
# el triage M-01.1 documentado en el docstring de `DatabaseLifecycleManager`:
# fallbacks pre-M-01 (migrables, conservan el camino directo para
# tests/standalone) y exenciones deliberadas que NO deben migrarse sin una razón
# nueva. La distinción importa: sacar un módulo de acá tiene que ser una decisión,
# no un descuido.
CONEXIONES_ESPERADAS = {
    # Dueño canónico del contrato M-01.
    "sky_claw/app/core/db_lifecycle.py": 3,
    # Fallbacks pre-M-01 con DI opcional `lifecycle=`.
    "sky_claw/app/agent/router.py": 1,
    "sky_claw/app/core/dlq_manager.py": 1,
    "sky_claw/app/db/async_registry.py": 2,
    "sky_claw/app/db/journal.py": 1,
    "sky_claw/app/db/locks.py": 1,
    # Exenciones deliberadas (triage M-01.1 — no migrar sin razón nueva):
    # pool propio acotado sobre un archivo con ACL owner-only.
    "sky_claw/app/security/credential_vault.py": 1,
    # lecturas read-only por operación (en WAL los lectores no bloquean).
    "sky_claw/app/agent/context_manager.py": 1,
    # legacy sin usos en producción.
    "sky_claw/app/db/registry.py": 1,
}

MODULOS_CONECTORES = {"aiosqlite", "sqlite3"}
FUNCION_CONECTORA = "connect"

# Allowlist, no blocklist: enumerar los directorios propios es lo único que no
# depende de cómo cada quien llame a su venv. Una lista de exclusiones deja pasar
# `env/`, `.venv311/`, `venv-sky-claw/` y demás, y ahí adentro hay dependencias
# que usan `threading.local` legítimamente — el test fallaría en la máquina del
# desarrollador por un archivo que ni siquiera es del repo.
DIRECTORIOS_DE_DOCS = ("docs", ".github")


def _nombres_conectores(arbol: ast.AST) -> tuple[set[str], set[str]]:
    """Resuelve los nombres con que este módulo puede llamar al conector.

    Devuelve `(alias_de_modulo, nombres_directos)`. Sin esto, `import aiosqlite
    as db; db.connect(...)` y `from aiosqlite import connect; connect(...)`
    esquivan el detector — y son las dos formas más comunes de escribirlo.
    """
    alias_de_modulo: set[str] = set()
    nombres_directos: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if alias.name in MODULOS_CONECTORES:
                    alias_de_modulo.add(alias.asname or alias.name)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module in MODULOS_CONECTORES:
            for alias in nodo.names:
                if alias.name == FUNCION_CONECTORA:
                    nombres_directos.add(alias.asname or alias.name)
    return alias_de_modulo, nombres_directos


def _es_llamada_a_conector(func: ast.expr, alias_de_modulo: set[str], nombres_directos: set[str]) -> bool:
    """`db.connect(...)` con `db` alias del módulo, o `connect(...)` importado directo."""
    if isinstance(func, ast.Attribute):
        return isinstance(func.value, ast.Name) and func.value.id in alias_de_modulo and func.attr == FUNCION_CONECTORA
    return isinstance(func, ast.Name) and func.id in nombres_directos


def _contar_conexiones_directas(arbol: ast.AST) -> int:
    """Cuenta las *llamadas* a conector, ignorando docstrings y comentarios."""
    alias_de_modulo, nombres_directos = _nombres_conectores(arbol)
    return sum(
        1
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and _es_llamada_a_conector(nodo.func, alias_de_modulo, nombres_directos)
    )


def _conexiones_por_modulo() -> dict[str, int]:
    encontrados: dict[str, int] = {}
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        cantidad = _contar_conexiones_directas(arbol)
        if cantidad:
            encontrados[archivo.relative_to(RAIZ).as_posix()] = cantidad
    return encontrados


def _docs_del_repo() -> list[Path]:
    """Markdown propio del repo: guías canónicas y documentación, sin dependencias."""
    archivos = [ruta for directorio in DIRECTORIOS_DE_DOCS for ruta in (RAIZ / directorio).rglob("*.md")]
    # Los .md de la raíz (AGENTS.md, CLAUDE.md, CONTRIBUTING.md…) son justamente
    # donde vivía la regla falsa, así que entran explícitamente.
    archivos.extend(RAIZ.glob("*.md"))
    archivos.extend(PAQUETE.rglob("AGENTS.md"))
    return archivos


def _contar(fuente: str) -> int:
    return _contar_conexiones_directas(ast.parse(fuente))


def test_detector_reconoce_las_formas_de_importar_el_conector() -> None:
    """El ancla no sirve si se la esquiva renombrando el import."""
    assert _contar("import aiosqlite\naiosqlite.connect('a.db')\n") == 1
    assert _contar("import sqlite3\nsqlite3.connect('a.db')\n") == 1
    # Alias de módulo: la forma que pasaba sin romper nada.
    assert _contar("import aiosqlite as db\ndb.connect('a.db')\n") == 1
    # Import directo de la función, con y sin alias.
    assert _contar("from aiosqlite import connect\nconnect('a.db')\n") == 1
    assert _contar("from sqlite3 import connect as abrir\nabrir('a.db')\n") == 1
    # Dentro de una corrutina, que es como aparece en el código real.
    assert _contar("import aiosqlite\nasync def f():\n    return await aiosqlite.connect('a.db')\n") == 1


def test_detector_ignora_menciones_que_no_son_llamadas() -> None:
    """Docstrings, comentarios y homónimos de otros módulos no cuentan."""
    assert _contar('"""No llamar a aiosqlite.connect() directamente."""\n') == 0
    assert _contar("# aiosqlite.connect(path) queda prohibido\nx = 1\n") == 0
    # `connect` de otro módulo no es el conector.
    assert _contar("import socket\nsocket.connect(('h', 1))\n") == 0
    assert _contar("from otra_lib import connect\nconnect('a.db')\n") == 0
    # Referenciar el atributo sin llamarlo tampoco.
    assert _contar("import aiosqlite\nfn = aiosqlite.connect\n") == 0


def test_detector_cuenta_cada_llamada_no_cada_modulo() -> None:
    """Congelar el nombre del módulo dejaba libre agregar una segunda llamada."""
    fuente = "import aiosqlite\naiosqlite.connect('a.db')\naiosqlite.connect('b.db')\n"
    assert _contar(fuente) == 2


def test_conexiones_directas_por_modulo_estan_congeladas() -> None:
    """Igualdad literal del dict: módulo nuevo *o* llamada nueva rompe acá."""
    assert _conexiones_por_modulo() == CONEXIONES_ESPERADAS, (
        "Cambió el inventario de llamadas directas a aiosqlite.connect()/"
        "sqlite3.connect(). Si es código nuevo, pedile la conexión a "
        "DatabaseLifecycleManager en vez de conectar. Si migraste o agregaste "
        "una llamada en un módulo ya listado, actualizá CONEXIONES_ESPERADAS "
        "con la decisión tomada."
    )


def test_database_agent_no_conecta_por_su_cuenta() -> None:
    """`DatabaseAgent` es el camino recomendado y delega toda conexión al lifecycle."""
    assert "sky_claw/app/core/database.py" not in _conexiones_por_modulo()


def test_los_docs_no_reintroducen_conexiones_por_hilo() -> None:
    """La regla falsa que motivó este archivo no puede volver a entrar.

    Se limita a la documentación a propósito: lo que era falso es la *guía* sobre
    cómo se manejan las conexiones de esta DB. `threading.local` como mecanismo
    no está prohibido en el repo —es legítimo para estado por hilo ajeno a la
    DB— así que prohibirlo en todo `.py` bloquearía cambios sin relación.
    """
    este_archivo = Path(__file__).resolve()
    ofensores = sorted(
        ruta.relative_to(RAIZ).as_posix()
        for ruta in _docs_del_repo()
        if ruta.resolve() != este_archivo and "threading.local" in ruta.read_text(encoding="utf-8", errors="ignore")
    )
    assert not ofensores, (
        f"`threading.local` reaparece como guía de DB en {ofensores}. La conexión "
        "de este repo es aiosqlite vía DatabaseLifecycleManager "
        "(core/db_lifecycle.py); no hay conexiones por hilo."
    )

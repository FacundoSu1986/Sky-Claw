"""Ancla del invariante de conexiones SQLite.

Por qué existe: `.github/coding_conventions.md` y `CONTRIBUTING.md` sostuvieron
durante meses que las conexiones se manejaban con `threading.local()`. Nunca fue
cierto en este árbol —la DB es `aiosqlite` con conexión singleton por path— y
nada lo hacía fallar, así que la regla falsa sobrevivió a cada lectura.

Estos tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): congelan el conjunto de módulos que abren conexiones por su cuenta.
Un módulo nuevo que llame `aiosqlite.connect()` / `sqlite3.connect()` rompe el
test hasta que se decida explícitamente si va por `DatabaseLifecycleManager` o si
es otro fallback legacy consciente.
"""

from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

# Dueño canónico de la conexión: es el único que *debería* conectar.
DUENO_CANONICO = "sky_claw/app/core/db_lifecycle.py"

# Fallbacks pre-M-01 que todavía conectan directo. No es una lista de destino:
# es el estado real congelado. Sacar uno de acá (migrándolo al lifecycle) también
# rompe el test, y eso es correcto — obliga a actualizar el inventario.
FALLBACKS_LEGACY_CONOCIDOS = {
    "sky_claw/app/agent/context_manager.py",
    "sky_claw/app/agent/router.py",
    "sky_claw/app/core/dlq_manager.py",
    "sky_claw/app/db/async_registry.py",
    "sky_claw/app/db/journal.py",
    "sky_claw/app/db/locks.py",
    "sky_claw/app/db/registry.py",
    "sky_claw/app/security/credential_vault.py",
}

CONECTORES = {("aiosqlite", "connect"), ("sqlite3", "connect")}

# Directorios que no son fuente del repo (dependencias, artefactos, VCS).
EXCLUIDOS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__"}


def _es_fuente_del_repo(ruta: Path) -> bool:
    return not EXCLUIDOS.intersection(ruta.relative_to(RAIZ).parts)


def _abre_conexion_directa(arbol: ast.AST) -> bool:
    """True si el módulo *llama* a un conector, ignorando docstrings y comentarios."""
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        func = nodo.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (func.value.id, func.attr) in CONECTORES
        ):
            return True
    return False


def _modulos_que_conectan() -> set[str]:
    encontrados: set[str] = set()
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        if _abre_conexion_directa(arbol):
            encontrados.add(archivo.relative_to(RAIZ).as_posix())
    return encontrados


def test_conjunto_de_modulos_que_abren_conexiones_esta_congelado() -> None:
    """Igualdad literal: un conector nuevo o migrado rompe acá, no en review."""
    esperados = FALLBACKS_LEGACY_CONOCIDOS | {DUENO_CANONICO}
    assert _modulos_que_conectan() == esperados, (
        "Cambió el conjunto de módulos que abren conexiones SQLite directamente. "
        "Si agregaste código nuevo, pedile la conexión a DatabaseLifecycleManager "
        "en vez de conectar. Si migraste un fallback legacy, sacalo de "
        "FALLBACKS_LEGACY_CONOCIDOS."
    )


def test_database_agent_no_conecta_por_su_cuenta() -> None:
    """`DatabaseAgent` es el camino recomendado y delega toda conexión al lifecycle."""
    assert "sky_claw/app/core/database.py" not in _modulos_que_conectan()


def test_ningun_doc_ni_codigo_reintroduce_threading_local_para_la_db() -> None:
    """La regla falsa que motivó este archivo no puede volver a entrar."""
    este_archivo = Path(__file__).resolve()
    ofensores = [
        ruta.relative_to(RAIZ).as_posix()
        for patron in ("*.py", "*.md")
        for ruta in RAIZ.rglob(patron)
        if _es_fuente_del_repo(ruta)
        and ruta.resolve() != este_archivo
        and "threading.local" in ruta.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not ofensores, (
        f"`threading.local` reaparece en {ofensores}. La DB de este repo es "
        "aiosqlite con conexión singleton por path (core/db_lifecycle.py); "
        "no hay conexiones por hilo."
    )

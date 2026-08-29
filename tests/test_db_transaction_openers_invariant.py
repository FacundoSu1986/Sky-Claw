"""Ancla enumerativa de los abridores EXPLÍCITOS de transacciones SQLite.

Por qué existe: el boundary transaccional de este repo es
``DatabaseLifecycleManager.transaction()``, que NO emite ``BEGIN`` propio —
depende del BEGIN DEFERRED implícito de ``sqlite3`` en modo legacy (emitido
antes del primer DML del cuerpo). Los ``BEGIN`` explícitos que existen en el
árbol son decisiones de contrato tomadas una por una: reservan la write
transaction DESDE el inicio porque el caller declara su cuerpo como escritura
(``init_db``, ``_ensure_schema``). Un ``BEGIN`` (o ``BEGIN DEFERRED``) nuevo
cambia el aislamiento prometido por ``transaction()`` —promoción
deferred→write con ventana de SQLITE_BUSY_SNAPSHOT— y hoy NINGÚN caller
establece snapshot de lectura dentro del boundary antes de su primera
escritura; esa propiedad se mantiene en parte porque nada la hacía fallar.

Estos tests enumeran en vez de muestrear (ver "La regla que más se viola" en
`AGENTS.md`): congelan el inventario EXACTO de abridores por módulo y símbolo.
Un ``BEGIN`` nuevo —módulo nuevo, o una forma nueva dentro de un módulo ya
listado— rompe el test hasta que se decida explícitamente si el caller es un
writer que exige BEGIN IMMEDIATE, un boundary read-only deliberado (exención
documentada), o un defecto.

Se detecta por AST: llamadas a ``execute``/``executescript``/``executemany``
 cuyo primer argumento es un literal (o una variable asignada a un literal)
 que abre transacción, case-insensitive. Es sobre-aproximación conservadora:
la evasión deliberada (``getattr``, SQL compuesto en runtime) queda fuera del
alcance de un chequeo estático, mismo criterio que
``tests/test_db_connection_invariant.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

# (módulo, símbolo contenedor, literal) congelados. Igualdad literal del set:
# un abridor nuevo, movido de función o cambiado de forma rompe acá.
ABRIDORES_ESPERADOS: set[tuple[str, str, str]] = {
    # Cuerpos que declaran su boundary como ESCRITURA desde el inicio: reservan
    # la write transaction antes del DDL (el arbiter es SQLite, no Python).
    ("sky_claw/app/core/database.py", "init_db", "BEGIN IMMEDIATE"),
    ("sky_claw/app/core/dlq_manager.py", "_ensure_schema", "BEGIN IMMEDIATE"),
}

METODOS_SQL = {"execute", "executescript", "executemany"}


def _literal_abridor(texto: str) -> str | None:
    """Forma canónica del literal si abre transacción; None si no.

    Case-insensitive a propósito: SQLite acepta ``begin immediate`` y un
    abridor en minúsculas sería exactamente el gemelo escondido que el ancla
    debe detectar. Para scripts (``executescript``) se mira la PRIMERA
    sentencia: un script que empieza con BEGIN abre transacción igual.
    """
    primera_sentencia = texto.strip().split(";", 1)[0]
    normalizado = " ".join(primera_sentencia.upper().split())
    if normalizado == "BEGIN" or normalizado.startswith("BEGIN "):
        return normalizado
    return None


class _ResolvedorDeLiterales:
    """Resuelve ``Nombre`` → literal string por asignación simple (punto fijo).

    Cubre ``SQL = "BEGIN IMMEDIATE"`` + ``conn.execute(SQL)`` — la forma
    natural de esconder un abridor detrás de una constante de módulo. La
    sobre-aproximación (mismo nombre con distinto valor en otra función
    resolve al primero que aparece) marca de más, nunca de menos.
    """

    def __init__(self, arbol: ast.AST) -> None:
        self._por_nombre: dict[str, str] = {}
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Assign)
                and isinstance(nodo.value, ast.Constant)
                and isinstance(nodo.value.value, str)
            ):
                literal = _literal_abridor(nodo.value.value)
                if literal is not None:
                    for objetivo in nodo.targets:
                        if isinstance(objetivo, ast.Name):
                            self._por_nombre[objetivo.id] = literal
            elif (
                isinstance(nodo, ast.AnnAssign)
                and nodo.value is not None
                and isinstance(nodo.value, ast.Constant)
                and isinstance(nodo.value.value, str)
            ):
                literal = _literal_abridor(nodo.value.value)
                if literal is not None and isinstance(nodo.target, ast.Name):
                    self._por_nombre[nodo.target.id] = literal

    def literal_de(self, expresion: ast.expr) -> str | None:
        """Literal de la forma del primer argumento: Constant o Name resuelto."""
        if isinstance(expresion, ast.Constant) and isinstance(expresion.value, str):
            return _literal_abridor(expresion.value)
        if isinstance(expresion, ast.Name):
            return self._por_nombre.get(expresion.id)
        return None


def _simbolo_contenedor(arbol: ast.AST, nodo_objetivo: ast.stmt) -> str:
    """Nombre de la función/método que contiene al statement (aprox. por línea).

     El anchor apunta a descuidos, no a un adversario: resolver el contenedor
     por rango de líneas de las defs es suficiente y evita construir un
    Visitor con pila de padres.
    """
    contenedor = "<modulo>"
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            nodo.lineno <= nodo_objetivo.lineno <= max(getattr(nodo, "end_lineno", nodo.lineno), nodo.lineno)
        ):
            contenedor = nodo.name
    return contenedor


def _abridores_de_modulo(ruta: Path) -> set[tuple[str, str]]:
    """{(símbolo, literal)} de cada abridor explícito en el módulo."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
    return _abridores(arbol)


def _abridores(arbol: ast.AST) -> set[tuple[str, str]]:
    """Abridores explícitos en un AST: {(símbolo contenedor, forma canónica)}.

    Resuelve ``execute``/``executescript``/``executemany`` con literal directo
    o vía variable asignada a un literal abridor.
    """
    resolvedor = _ResolvedorDeLiterales(arbol)
    encontrados: set[tuple[str, str]] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Attribute):
            continue
        if nodo.func.attr not in METODOS_SQL or not nodo.args:
            continue
        literal = resolvedor.literal_de(nodo.args[0])
        if literal is not None:
            stmt = _statement_que_contiene(arbol, nodo.lineno)
            encontrados.add((_simbolo_contenedor(arbol, stmt), literal))
    return encontrados


def _statement_que_contiene(arbol: ast.AST, linea: int) -> ast.stmt:
    """El statement más interno cuyo rango de líneas contiene a `linea`.

    Sólo se usa para ubicar el símbolo contenedor; si el AST cambió de forma,
    devolver el módulo entero sigue siendo conservador.
    """
    mejor: ast.stmt | None = None
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.stmt)
            and nodo.lineno <= linea <= getattr(nodo, "end_lineno", nodo.lineno)
            and (mejor is None or nodo.lineno >= mejor.lineno)
        ):
            mejor = nodo
    if mejor is None:  # pragma: no cover - una Call siempre vive dentro de un stmt
        raise AssertionError(f"Llamada en línea {linea} sin statement contenedor")
    return mejor


def _inventario() -> set[tuple[str, str, str]]:
    inventario: set[tuple[str, str, str]] = set()
    for ruta in PAQUETE.rglob("*.py"):
        abridores = _abridores_de_modulo(ruta)
        for simbolo, literal in abridores:
            inventario.add((ruta.relative_to(RAIZ).as_posix(), simbolo, literal))
    return inventario


# ---------------------------------------------------------------------------
# Detector: el ancla no sirve si se la esquiva
# ---------------------------------------------------------------------------


def _contar_de_fuente(fuente: str) -> set[tuple[str, str]]:
    return _abridores(ast.parse(fuente))


def _abridores(arbol: ast.AST) -> set[tuple[str, str]]:
    resolvedor = _ResolvedorDeLiterales(arbol)
    encontrados: set[tuple[str, str]] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Attribute):
            continue
        if nodo.func.attr not in METODOS_SQL or not nodo.args:
            continue
        literal = resolvedor.literal_de(nodo.args[0])
        if literal is not None:
            stmt = _statement_que_contiene(arbol, nodo.lineno)
            encontrados.add((_simbolo_contenedor(arbol, stmt), literal))
    return encontrados


def test_detector_reconoce_las_formas_de_abrir() -> None:
    """Literal directo, mayúsculas/minúsculas y constante de módulo cuentan."""
    directo = "async def f(conn):\n    await conn.execute('BEGIN IMMEDIATE')\n"
    assert _contar_de_fuente(directo) == {("f", "BEGIN IMMEDIATE")}
    minusculas = "async def f(conn):\n    await conn.execute('begin immediate')\n"
    assert _contar_de_fuente(minusculas) == {("f", "BEGIN IMMEDIATE")}
    via_variable = 'SQL = "BEGIN IMMEDIATE"\nasync def f(conn):\n    await conn.execute(SQL)\n'
    assert _contar_de_fuente(via_variable) == {("f", "BEGIN IMMEDIATE")}
    deferred = "async def f(conn):\n    await conn.execute('BEGIN DEFERRED')\n"
    assert _contar_de_fuente(deferred) == {("f", "BEGIN DEFERRED")}
    executescript = "def f(conn):\n    conn.executescript('BEGIN; SELECT 1; COMMIT;')\n"
    assert _contar_de_fuente(executescript) == {("f", "BEGIN")}


def test_detector_ignora_lo_que_no_abre() -> None:
    """SQL ordinario, strings BEGIN en comentarios y otras llamadas no cuentan."""
    ordinario = "async def f(conn):\n    await conn.execute('SELECT 1')\n"
    assert _contar_de_fuente(ordinario) == set()
    comentario = "# conn.execute('BEGIN IMMEDIATE') prohibido\nx = 1\n"
    assert _contar_de_fuente(comentario) == set()
    otro_metodo = "async def f(conn):\n    await conn.commit()\n"
    assert _contar_de_fuente(otro_metodo) == set()
    no_string = "async def f(conn, sql):\n    await conn.execute(sql)\n"
    assert _contar_de_fuente(no_string) == set()


def test_abridores_explicitos_estan_congelados() -> None:
    """Igualdad literal del inventario: abridor nuevo rompe acá.

    Si agregás un ``BEGIN``/``BEGIN DEFERRED`` explícito, decidí primero: ¿el
    caller es un writer que exige reservar la write transaction (usá
    ``BEGIN IMMEDIATE`` y documentá por qué ``transaction()`` deferred no
    alcanza), o es un boundary read-only (no abrás transacción)? Nunca
    ``BEGIN`` deferred en un writer: deja la ventana de promoción
    deferred→write abierta (SQLITE_BUSY_SNAPSHOT).
    """
    assert _inventario() == ABRIDORES_ESPERADOS, (
        "Cambió el inventario de abridores explícitos de transacciones SQLite. "
        "Un BEGIN nuevo cambia el aislamiento que DatabaseLifecycleManager."
        "transaction() promete; justificar el abridor y actualizar "
        "ABRIDORES_ESPERADOS con la decisión, o quitar el BEGIN."
    )

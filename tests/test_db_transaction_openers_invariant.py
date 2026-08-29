"""Ancla enumerativa de los abridores EXPLÍCITOS de transacciones SQLite.

Alcance del ancla: el inventario de llamadas a ``execute``/``executescript``/
``executemany`` cuyo primer argumento es un literal (o variable asignada a
literal) que abre transacción, case-insensitive, dentro de ``sky_claw/``.

Lo que el ancla SÍ detecta:
- ``BEGIN`` / ``BEGIN DEFERRED`` / ``BEGIN IMMEDIATE`` NUEVO en un módulo
  del paquete.
- Cambio de forma dentro de un módulo ya listado (deferred ↔ immediate).
- Mover un abridor de un símbolo a otro.
- MULTIPLICIDAD: dos ``BEGIN IMMEDIATE`` en el mismo símbolo
  (incluso con la misma forma) elevan el conteo, no colapsan en el
  set. La representación usa ``Counter`` anidado
  ``{(símbolo, forma): n}`` para preservar el ordinal.

Lo que el ancla NO detecta (y la limitación es deliberada):
- El ORDEN de operaciones dentro de un mismo ``transaction()``: este ancla
  enumera ABRIDORES, no el patrón (read-first / write-first) del cuerpo
  del boundary. Un caller que hoy escribe antes de leer puede, sin que
  el ancla se entere, leer antes de escribir y abrir la ventana
  ``SQLITE_BUSY_SNAPSHOT`` que el propio docstring del archivo menciona.
  La defensa contra esa regresión es estructural (auditoría de callers,
  revisión) y la promesa del ancla es sobre las decisiones EXPLÍCITAS,
  no sobre el patrón implícito.
- SQL compuesto en runtime, ``getattr`` para esconder el método, u otras
  evasiones dinámicas: el detector es estático, mismo criterio que
  ``tests/test_db_connection_invariant.py``.

Por qué el inventario se congela: un ``BEGIN`` o ``BEGIN DEFERRED``
nuevo en un writer cambia el aislamiento prometido por
``DatabaseLifecycleManager.transaction()`` (la promoción
deferred→write abre la ventana de ``SQLITE_BUSY_SNAPSHOT``). Cada
abridor es una decisión de contrato tomada una por una: ``init_db`` y
``_ensure_schema`` reservan la write transaction desde el inicio
porque declaran su cuerpo como escritura. Romper el ancla fuerza a
re-explicitar la decisión.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

# ``Counter`` anidado ``{(símbolo, forma): n}``. Multiplicidad
# preservada: dos ``BEGIN IMMEDIATE`` en el mismo símbolo cuentan
# como 2, no colapsan a 1.
ABRIDORES_ESPERADOS: Counter[tuple[str, str, str]] = Counter(
    {
        ("sky_claw/app/core/database.py", "init_db", "BEGIN IMMEDIATE"): 1,
        ("sky_claw/app/core/dlq_manager.py", "_ensure_schema", "BEGIN IMMEDIATE"): 1,
    }
)

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


def _abridores(arbol: ast.AST) -> Counter[tuple[str, str]]:
    """Abridores explícitos en un AST: ``Counter{(símbolo, forma): n}``.

    Multiplicidad preservada: dos ``BEGIN IMMEDIATE`` en el mismo
    símbolo cuentan como 2. Esto es lo que rompe el ancla si se
    agrega un abridor nuevo sin actualizar ``ABRIDORES_ESPERADOS``.
    """
    resolvedor = _ResolvedorDeLiterales(arbol)
    encontrados: Counter[tuple[str, str]] = Counter()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call) or not isinstance(nodo.func, ast.Attribute):
            continue
        if nodo.func.attr not in METODOS_SQL or not nodo.args:
            continue
        literal = resolvedor.literal_de(nodo.args[0])
        if literal is not None:
            stmt = _statement_que_contiene(arbol, nodo.lineno)
            encontrados[(_simbolo_contenedor(arbol, stmt), literal)] += 1
    return encontrados


def _abridores_de_modulo(ruta: Path) -> Counter[tuple[str, str]]:
    """``Counter`` de cada abridor explícito en el módulo."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
    return _abridores(arbol)


def _inventario() -> Counter[tuple[str, str, str]]:
    """Inventario completo del paquete: ``Counter`` con módulo incluido.

    La clave incluye el path del módulo (``relative_to(RAIZ)``) para
    que dos ``init_db`` en módulos distintos NO se confundan.
    Multiplicidad por (módulo, símbolo, forma) preservada.
    """
    inventario: Counter[tuple[str, str, str]] = Counter()
    for ruta in PAQUETE.rglob("*.py"):
        for (simbolo, literal), n in _abridores_de_modulo(ruta).items():
            inventario[(ruta.relative_to(RAIZ).as_posix(), simbolo, literal)] += n
    return inventario


# ---------------------------------------------------------------------------
# Detector: el ancla no sirve si se la esquiva
# ---------------------------------------------------------------------------


def _contar_de_fuente(fuente: str) -> Counter[tuple[str, str]]:
    return _abridores(ast.parse(fuente))


def test_inventario_tiene_una_sola_abridores() -> None:
    """F6: el detector debe estar definido UNA sola vez.

    El bug pre-fix: dos ``def _abridores`` en el mismo módulo. La
    segunda pisa a la primera y los tests siguen pasando
    silenciosamente. El test verifica por AST que existe exactamente
    una función ``_abridores`` en el módulo. Si la duplicación
    vuelve, el test rompe.
    """
    with open(__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    definiciones = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_abridores"]
    assert len(definiciones) == 1, (
        f"_abridores debe estar definida una sola vez, hay {len(definiciones)}: {[d.lineno for d in definiciones]}"
    )


def test_inventario_abridores_detecta_multiplicidad() -> None:
    """F5: el ancla debe detectar un segundo abridor idéntico en el mismo símbolo.

    ``set`` colapsa tuplas idénticas; un segundo ``BEGIN IMMEDIATE`` dentro
    de ``init_db`` pasa invisible al ancla actual. La representación del
    inventario debe preservar la multiplicidad (Counter o tupla con
    ordinal/recuento), y el ancla debe romper cuando un símbolo existente
    gana un abridor adicional con la misma forma.

    Verifica que ``_abridores`` y ``_inventario`` devuelven una
    estructura donde ``BEGIN IMMEDIATE`` aparece DOS veces en
    ``init_db`` cuando se agrega una segunda invocación literal al AST.

    Red pre-fix: con un set, la segunda invocación colapsa con la
    primera y la diferencia no es observable; el ancla pasa
    silenciosamente.
    """
    fuente_con_dos_begin = (
        "async def init_db(conn):\n"
        '    await conn.execute("BEGIN IMMEDIATE")\n'
        '    await conn.execute("BEGIN IMMEDIATE")\n'
    )
    fuente_con_un_begin = 'async def init_db(conn):\n    await conn.execute("BEGIN IMMEDIATE")\n'
    # Lo que el ancla debería distinguir:
    #   - una invocación: el inventario refleja 1 elemento en init_db/BEGIN IMMEDIATE.
    #   - dos invocaciones: el inventario refleja 2 elementos en init_db/BEGIN IMMEDIATE.
    # La forma EXACTA del contenedor (Counter, lista ordenada, tupla con
    # ordinal) es decisión de implementación; el test verifica la
    # PROPIEDAD de que las dos formas NO son iguales y que la forma
    # ``con_dos`` indica más invocaciones que ``con_un``.
    con_un = _abridores(ast.parse(fuente_con_un_begin))
    con_dos = _abridores(ast.parse(fuente_con_dos_begin))
    # Cualquier representación razonable debe poder reportar la diferencia
    # de alguna forma. La representación más simple que cumple la
    # propiedad es ``Counter``: ``Counter({"init_db": {"BEGIN IMMEDIATE": 1}})``
    # vs ``Counter({"init_db": {"BEGIN IMMEDIATE": 2}})``.
    from collections import Counter

    def _a_counter(observado: object) -> Counter:
        # Acepta tanto un set de tuplas como un Counter anidado.
        if isinstance(observado, Counter):
            return observado
        c: Counter = Counter()
        for simbolo, literal in observado:  # type: ignore[union-attr]
            c[(simbolo, literal)] += 1
        return c

    cnt_un = _a_counter(con_un)
    cnt_dos = _a_counter(con_dos)
    assert cnt_dos[("init_db", "BEGIN IMMEDIATE")] == 2, (
        f"el ancla debe contar 2 invocaciones en la fuente con_dos, obtuvo {dict(cnt_dos)}"
    )
    assert cnt_un[("init_db", "BEGIN IMMEDIATE")] == 1, (
        f"el ancla debe contar 1 invocación en la fuente con_un, obtuvo {dict(cnt_un)}"
    )
    assert cnt_dos != cnt_un, "el ancla debe distinguir una invocación de dos (multiplicidad)"


def test_detector_reconoce_las_formas_de_abrir() -> None:
    """Literal directo, mayúsculas/minúsculas y constante de módulo cuentan."""
    directo = "async def f(conn):\n    await conn.execute('BEGIN IMMEDIATE')\n"
    assert _contar_de_fuente(directo) == Counter({("f", "BEGIN IMMEDIATE"): 1})
    minusculas = "async def f(conn):\n    await conn.execute('begin immediate')\n"
    assert _contar_de_fuente(minusculas) == Counter({("f", "BEGIN IMMEDIATE"): 1})
    via_variable = 'SQL = "BEGIN IMMEDIATE"\nasync def f(conn):\n    await conn.execute(SQL)\n'
    assert _contar_de_fuente(via_variable) == Counter({("f", "BEGIN IMMEDIATE"): 1})
    deferred = "async def f(conn):\n    await conn.execute('BEGIN DEFERRED')\n"
    assert _contar_de_fuente(deferred) == Counter({("f", "BEGIN DEFERRED"): 1})
    executescript = "def f(conn):\n    conn.executescript('BEGIN; SELECT 1; COMMIT;')\n"
    assert _contar_de_fuente(executescript) == Counter({("f", "BEGIN"): 1})


def test_detector_ignora_lo_que_no_abre() -> None:
    """SQL ordinario, strings BEGIN en comentarios y otras llamadas no cuentan."""
    ordinario = "async def f(conn):\n    await conn.execute('SELECT 1')\n"
    assert _contar_de_fuente(ordinario) == Counter()
    comentario = "# conn.execute('BEGIN IMMEDIATE') prohibido\nx = 1\n"
    assert _contar_de_fuente(comentario) == Counter()
    otro_metodo = "async def f(conn):\n    await conn.commit()\n"
    assert _contar_de_fuente(otro_metodo) == Counter()
    no_string = "async def f(conn, sql):\n    await conn.execute(sql)\n"
    assert _contar_de_fuente(no_string) == Counter()


def test_abridores_explicitos_estan_congelados() -> None:
    """Congela el inventario EXACTO de abridores BEGIN explícitos.

    Lo que este test PROTEGE: un ``BEGIN``/``BEGIN IMMEDIATE``/
    ``BEGIN DEFERRED`` NUEVO dentro de un módulo del paquete, o una
    forma nueva dentro de un módulo ya listado, rompe hasta decisión
    explícita (aceptar el cambio y actualizar ``ABRIDORES_ESPERADOS``).

    Lo que este test NO cubre: el ORDEN de operaciones dentro de un
    mismo ``transaction()``. Un caller que hoy escribe antes de leer
    podría en el futuro leer antes de escribir dentro del mismo
    boundary, abriendo la ventana ``SQLITE_BUSY_SNAPSHOT`` que el
    propio test menciona — y este ancla no lo detectaría porque el
    ``BEGIN`` explícito (cuando existe) o el ``BEGIN`` implícito del
    driver (cuando no) no cambian de forma. La defensa contra esa
    regresión es estructural del código (auditoría de callers) y de
    revisión, no de este ancla AST.

    Por eso este test describe el inventario de las decisiones
    EXPLÍCITAS, no una promesa sobre el comportamiento de los callers
    implícitos. Si el caller del ``transaction()`` cambia de
    categoría (read-first / write-first), el ancla no rompe: hay que
    revisarlo.
    """
    assert _inventario() == ABRIDORES_ESPERADOS, (
        "Cambió el inventario de abridores explícitos de transacciones SQLite. "
        "Un BEGIN nuevo cambia el aislamiento que DatabaseLifecycleManager."
        "transaction() promete; justificar el abridor y actualizar "
        "ABRIDORES_ESPERADOS con la decisión, o quitar el BEGIN."
    )

"""Ancla de P1/P2: boundary de escritura del paquete Frozen Runtime.

P1 (discovery/observation/provider_signals/models/_vdf/stabilization) es
READ-ONLY sobre disco: sin símbolos de mutación (congelado por AST, misma
técnica que tests/test_db_connection_invariant.py).

P2 introduce el ÚNICO módulo con escritura permitida (``state.py``: estado
persistente + metadata de generations, SIEMPRE dentro del FrozenRuntimeRoot
propio). Su vocabulario de mutación está congelado por igualdad literal: un
mutador nuevo (rmtree, chmod, rename no-atómico, copy*, symlink_to...) rompe
el test a propósito. Los lanzadores de procesos están prohibidos en todo el
paquete. MANAGED_SOURCE_WRITES=NO se mantiene por construcción: ninguna API
de escritura acepta una Managed Source.
"""

from __future__ import annotations

import ast
import pathlib

PAQUETE = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "frozen_runtime"

MUTADORES_FILESYSTEM: frozenset[str] = frozenset(
    {
        "write_text",
        "write_bytes",
        "remove",
        "unlink",
        "rmdir",
        "replace",
        "rename",
        "chmod",
        "mkdir",
        "makedirs",
        "symlink_to",
        "touch",
        "rmtree",
        "move",
        "copy2",
        "copyfile",
        "copytree",
        "write",
        "flush",
        "fsync",
        "mkstemp",
        "fdopen",
        # P3: superficie real de escritura al copiar. `link` (os.link) es el
        # atajo de hardlink que SFR-18 prohibe y que un digest NO detecta; si
        # aparece aca, el Candidate dejaria de ser fisicamente independiente.
        "link",
        "symlink",
    }
)

LANZADORES_PROCESO: frozenset[str] = frozenset(
    {"run", "call", "check_call", "check_output", "Popen", "system", "CreateProcess"}
)

# Vocabulario de mutación congelado por módulo (P2/P3): ÚNICOS módulos con
# escritura permitida, SIEMPRE dentro del FrozenRuntimeRoot propio.
# state.py: temporal en el mismo directorio + os.replace + fsync + cleanup.
# storage.py: creación idempotente del layout (mkdir).
# copying.py: la copia real del Candidate (mkdir + open/xb + fsync/flush).
# candidates.py: el directorio de metadata (mkdir); la escritura del JSON la
# hace state.write_json_atomic, no un segundo serializador.
MODULOS_CON_ESCRITURA_PERMITIDA: dict[str, frozenset[str]] = {
    "state.py": frozenset({"fdopen", "flush", "fsync", "mkstemp", "replace", "unlink"}),
    "storage.py": frozenset({"mkdir"}),
    "copying.py": frozenset({"flush", "fsync", "mkdir"}),
    "candidates.py": frozenset({"mkdir"}),
    "root_lock.py": frozenset({"flush", "fsync", "mkdir", "write"}),
}


def _es_modo_de_escritura(modo: str) -> bool:
    """Un modo de ``open()`` habilita escritura si pide w/a/x o update (``+``).

    Sigue la semantica documentada de ``open``: ``w`` (truncar), ``a`` (anexar),
    ``x`` (crear en exclusiva) y ``+`` (update, lectura+escritura) son las cuatro
    marcas que habilitan escritura; sin ninguna de ellas (``r``, ``rb``, ``rt``)
    el modo es lectura pura. Enumerar las combinaciones a mano dejaba afuera
    variantes validas —``wt``, ``w+``, ``w+b``, ``a+``, ``x+``— por las que un
    modulo NO declarado podia abrir escritura sin que el oracle lo viera
    (CodeRabbit sobre #698). El chequeo es sobre el modo, no sobre el fuente: no
    hay marcado por substring de la llamada.
    """
    return any(marca in modo for marca in ("w", "a", "x", "+"))


#: Módulos autorizados a abrir archivos en modo escritura, con el modo EXACTO
#: que declaran. `xb` es exclusivo de creación: sobreescribir un payload ya
#: existente sería dejar que dos corridas se pisen en silencio.
MODULOS_CON_OPEN_ESCRITURA: dict[str, frozenset[str]] = {
    "copying.py": frozenset({"xb"}),
    # `x` = creacion EXCLUSIVA sin contenido: la reserva de la ruta de metadata del
    # Candidate (P3-U). Es el single-winner hermano de `mkdir(exist_ok=False)`: el
    # placeholder vacio deja al ganador como unico dueno de la ruta, y el escritor
    # atomico despues lo reemplaza sin poder tocar la evidencia de otro.
    "state.py": frozenset({"x"}),
    # P4-S1: root_lock.py abre con `xb` exclusivo para creacion inicial y con `r+b`
    # para adquisicion y lectura/escritura del lockfile persistente bajo root/state/.
    "root_lock.py": frozenset({"r+b", "xb"}),
}


def _modulos_del_paquete() -> tuple[pathlib.Path, ...]:
    return tuple(sorted(PAQUETE.glob("*.py")))


def _es_mutador(nodo: ast.Attribute) -> bool:
    if nodo.attr not in MUTADORES_FILESYSTEM:
        return False
    if nodo.attr == "rename":
        # `.rename` NO existe en `str`/`bytes`: todo `X.rename(...)` es del
        # filesystem (`os.rename`, `Path.rename`). Tratarlo como ambiguo, como
        # se hacia con `replace`, dejaba pasar `ruta.rename(destino)` -- la forma
        # ordinaria cuando `ruta` ya es un `Path` -- y un mutador no-atomico podia
        # entrar mientras el ancla seguia verde (Codex sobre #682).
        return True
    if nodo.attr == "replace":
        # SOLO `replace` es ambiguo (existe `str.replace`); `rename` no tiene
        # equivalente en texto y se trata aparte arriba.
        # y es la forma que un mutador usaría para hacer un swap no-atómico.
        #
        # La regla que las separa sin análisis de tipos: una variable de texto es
        # un `Name` (`entrada.replace("\\", "/")`), mientras que una expresión que
        # produce un Path es una LLAMADA (`pathlib.Path(x).replace(y)`,
        # `candidate_dir(...).rename(...)`). Antes esta función sólo aceptaba
        # `os.replace`, así que `Path.replace` pasaba inadvertido: desde que P3
        # copia archivos de verdad, ese hueco dejó de ser teórico.
        return isinstance(nodo.value, ast.Call) or (isinstance(nodo.value, ast.Name) and nodo.value.id == "os")
    return True


def _mutadores_usados(modulo: pathlib.Path) -> set[str]:
    arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
    return {nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute) and _es_mutador(nodo)}


def test_escritura_solo_en_modulos_permitidos() -> None:
    """Sólo state.py/storage.py mutan, y sólo con su vocabulario congelado."""
    violaciones: list[str] = []
    for modulo in _modulos_del_paquete():
        usados = _mutadores_usados(modulo)
        permitidos = MODULOS_CON_ESCRITURA_PERMITIDA.get(modulo.name, frozenset())
        if usados != permitidos:
            violaciones.append(
                f"{modulo.name}: usados={sorted(usados)} permitidos={sorted(permitidos)} — "
                "decidí si el símbolo pertenece al boundary (escritura dentro del FrozenRuntimeRoot propio) "
                "y congélalo en MODULOS_CON_ESCRITURA_PERMITIDA"
            )
    assert not violaciones, f"boundary de escritura violado: {violaciones}"


def test_sin_lanzadores_de_proceso() -> None:
    """Ningún módulo del paquete puede lanzar procesos (ni Steam ni nada)."""
    violaciones: list[str] = []
    for modulo in _modulos_del_paquete():
        arbol = ast.parse(modulo.read_text(encoding="utf-8"), filename=str(modulo))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Attribute) and nodo.attr in LANZADORES_PROCESO:
                violaciones.append(f"{modulo.name}:{nodo.lineno}: {nodo.attr}")
    assert not violaciones, f"lanzadores de proceso en Frozen Runtime: {violaciones}"


def _modo_de_open(nodo: ast.Call, *, posicional: int) -> ast.expr | None:
    """Modo de un ``open(...)``: el posicional si está, si no ``mode=``.

    ``posicional`` es el índice del modo cuando se pasa sin nombre: ``0`` para
    ``Path.open`` (el receptor es el ``self``) y ``1`` para el ``open`` builtin
    (el ``file`` va primero). ``mode=`` funciona en ambos y es la ÚNICA forma
    cuando la llamada no tiene posicionales.
    """
    if len(nodo.args) > posicional:
        return nodo.args[posicional]
    for kw in nodo.keywords:
        if kw.arg == "mode":
            return kw.value
    return None


def _detectar_open_no_declarado(fuente: str, nombre_modulo: str) -> list[str]:
    permitidos = MODULOS_CON_OPEN_ESCRITURA.get(nombre_modulo, frozenset())
    arbol = ast.parse(fuente, filename=nombre_modulo)
    violaciones: list[str] = []
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        es_open_bare = isinstance(nodo.func, ast.Name) and nodo.func.id == "open"
        es_open_attr = isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "open"
        # El chequeo del modo NO puede exigir posicionales: `ruta.open(mode="w")`
        # no tiene ninguno y era la forma que se escapaba (finding post-merge #682).
        if not (es_open_bare or es_open_attr):
            continue
        if es_open_attr and isinstance(nodo.func.value, ast.Name) and nodo.func.value.id == "os":
            violaciones.append(f"{nombre_modulo}:{nodo.lineno}: os.open() no permitido en Frozen Runtime")
            continue
        modo = _modo_de_open(nodo, posicional=0 if es_open_attr else 1)
        if (
            isinstance(modo, ast.Constant)
            and isinstance(modo.value, str)
            and _es_modo_de_escritura(modo.value)
            and modo.value not in permitidos
        ):
            violaciones.append(
                f"{nombre_modulo}:{nodo.lineno}: open modo '{modo.value}' no declarado en "
                f"MODULOS_CON_OPEN_ESCRITURA (permitidos: {sorted(permitidos) or 'ninguno'})"
            )
    return violaciones


def test_open_en_modo_escritura_solo_en_los_modulos_declarados() -> None:
    """``open()`` en modo escritura requiere declaración explícita por módulo.

    P3 introduce la primera escritura de contenido (``copying.py`` abre el destino
    con ``xb``). Antes el test asumía que NINGÚN módulo podía hacerlo; ahora la
    regla es "solo los que lo declaran, y solo con los modos declarados", que
    sigue cerrando el default y además congela el modo exacto.
    """
    violaciones: list[str] = []
    for modulo in _modulos_del_paquete():
        violaciones.extend(_detectar_open_no_declarado(modulo.read_text(encoding="utf-8"), modulo.name))
    assert not violaciones, f"open() en modo escritura no declarado dentro de Frozen Runtime: {violaciones}"


def test_el_oracle_detecta_path_open_en_modo_escritura() -> None:
    """Detecta Path.open('w') y os.open(...) fuera de los módulos declarados (Codex #682)."""
    codigo_path_open = "def f(ruta):\n    with ruta.open('w') as fh:\n        pass\n"
    codigo_os_open = "def f(ruta):\n    os.open(ruta, 0)\n"
    assert len(_detectar_open_no_declarado(codigo_path_open, "no_declarado.py")) == 1
    assert len(_detectar_open_no_declarado(codigo_os_open, "no_declarado.py")) == 1


def test_el_oracle_detecta_el_modo_de_escritura_pasado_por_keyword() -> None:
    """`ruta.open(mode="w")` también es escritura (finding post-merge de #682).

    La rama exigía `nodo.args` ANTES de inspeccionar el modo, así que una llamada
    SIN posicionales —la forma keyword, que es la idiomática cuando sólo se pasa
    `mode`— nunca entraba al análisis: el boundary quedaba ciego justo para la
    variante que un escritor nuevo escribiría. El modo se busca ahora en
    posicional y en `mode=`, y el default read-only sigue sin marcarse.
    """
    # Escritura por keyword: el hueco del finding.
    assert len(_detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='w')\n", "no_declarado.py")) == 1
    assert len(_detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='xb')\n", "no_declarado.py")) == 1
    assert len(_detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='a')\n", "no_declarado.py")) == 1
    # `open` bare con `file=`/`mode=`: misma familia, sin receptor.
    assert len(_detectar_open_no_declarado("def f(p):\n    open(file=p, mode='w')\n", "no_declarado.py")) == 1
    # Posicional: no se puede perder al arreglar el keyword.
    assert len(_detectar_open_no_declarado("def f(ruta):\n    ruta.open('w')\n", "no_declarado.py")) == 1
    assert len(_detectar_open_no_declarado("def f(ruta):\n    ruta.open('x')\n", "no_declarado.py")) == 1
    # Read-only explícito e implícito: sin falsos positivos.
    assert _detectar_open_no_declarado("def f(ruta):\n    ruta.open('r')\n", "no_declarado.py") == []
    assert _detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='rb')\n", "no_declarado.py") == []
    assert _detectar_open_no_declarado("def f(ruta):\n    ruta.open()\n", "no_declarado.py") == []
    assert (
        _detectar_open_no_declarado("def f(ruta):\n    with ruta.open(mode='r') as fh:\n        fh.read()\n", "no.py")
        == []
    )
    # El modo variable no se adivina (mismo criterio que antes: sólo constantes).
    assert _detectar_open_no_declarado("def f(ruta, m):\n    ruta.open(mode=m)\n", "no_declarado.py") == []


def test_el_oracle_clasifica_todas_las_variantes_de_escritura() -> None:
    """Ninguna variante constante con capacidad de escritura puede escapar (CodeRabbit #698).

    La lista enumerada original omitia combinaciones validas (`wt`, `w+`, `w+b`,
    `a+`, `x+`): un modulo no declarado podia abrir escritura sin que el oracle lo
    viera. La clasificacion sigue ahora la semantica de `open` (marcas w/a/x/+),
    asi que las variantes con marca se detectan y las de lectura pura no.
    """
    # Keyword: variantes que la enumeracion dejaba afuera.
    for modo in ("wt", "w+", "w+b", "wb+", "a+", "at", "x+", "xt", "r+", "r+b", "rb+"):
        codigo = f"def f(ruta):\n    ruta.open(mode={modo!r})\n"
        assert len(_detectar_open_no_declarado(codigo, "no_declarado.py")) == 1, modo
    # Posicional: misma clasificacion.
    for modo in ("w", "x", "a", "wb", "xb"):
        codigo = f"def f(ruta):\n    ruta.open({modo!r})\n"
        assert len(_detectar_open_no_declarado(codigo, "no_declarado.py")) == 1, modo
    # Lectura pura (sin marca de escritura): sin falso positivo.
    for modo in ("r", "rb", "rt", "tr"):
        codigo = f"def f(ruta):\n    ruta.open(mode={modo!r})\n"
        assert _detectar_open_no_declarado(codigo, "no_declarado.py") == [], modo


def test_el_oracle_de_open_conserva_los_modos_declarados_por_modulo() -> None:
    """La declaración por módulo sigue mandando: `copying.py` puede `xb`, no `w`."""
    assert _detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='xb')\n", "copying.py") == []
    assert len(_detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='w')\n", "copying.py")) == 1
    assert _detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='x')\n", "state.py") == []
    assert len(_detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='w')\n", "state.py")) == 1
    # Un módulo sin declaración sigue cerrado por default.
    assert len(_detectar_open_no_declarado("def f(ruta):\n    ruta.open(mode='x')\n", "membership.py")) == 1


def test_el_oracle_distingue_str_replace_de_path_replace() -> None:
    """El anchor no se debilita por endurecer `replace` (§19).

    Antes sólo `os.replace` contaba como mutación, así que `Path.replace` —la
    forma de hacer un swap no-atómico— pasaba inadvertido. Este test congela las
    DOS sides: la de texto se sigue ignorando, la de Path se sigue detectando.
    """
    texto = "def f(entrada: str, ruta):\n    entrada.replace('a', 'b')\n"
    pathlib_ = "def f(ruta):\n    pathlib.Path(ruta).replace(otro)\n"
    os_ = "def f():\n    os.replace(tmp, ruta)\n"

    assert _mutadores_de_fuente(texto) == set()
    assert _mutadores_de_fuente(pathlib_) == {"replace"}
    assert _mutadores_de_fuente(os_) == {"replace"}


def test_el_oracle_detecta_rename_sobre_una_variable_path() -> None:
    """`ruta.rename(...)` es mutación aunque el receptor sea un `Name` (Codex #682).

    `str`/`bytes` no tienen `.rename`, así que sólo `replace` es ambiguo. Antes
    se aplicaba a `rename` la misma regla de receptor que a `replace`, y la forma
    ORDINARIA de renombrar un Path ya construido quedaba invisible.
    """
    assert _mutadores_de_fuente("def f(ruta, destino):\n    ruta.rename(destino)\n") == {"rename"}
    assert _mutadores_de_fuente("def f():\n    os.rename(a, b)\n") == {"rename"}
    assert _mutadores_de_fuente("def f(p):\n    pathlib.Path(p).rename(d)\n") == {"rename"}


def _mutadores_de_fuente(fuente: str) -> set[str]:
    arbol = ast.parse(fuente)
    return {nodo.attr for nodo in ast.walk(arbol) if isinstance(nodo, ast.Attribute) and _es_mutador(nodo)}


def test_el_oracle_detecta_el_atajo_de_hardlink() -> None:
    """`os.link` es mutación prohibida en P3 (SFR-18) y debe romper el anchor.

    Un hardlink daría el mismo `TreeDigest` y la misma `DirectoryMembership`, así
    que el digest no lo detecta: si el motor de copia empezara a "optimizar" con
    `os.link`, el Candidate perdería independencia física sin que ningún otro
    test lo notara.
    """
    assert _mutadores_de_fuente("def f(a, b):\n    os.link(a, b)\n") == {"link"}
    assert _mutadores_de_fuente("def f(a, b):\n    shutil.copytree(a, b)\n") == {"copytree"}

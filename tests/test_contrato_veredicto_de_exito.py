"""Ancla del contrato de veredicto de los runners de herramientas.

Por qué existe: **el exit code de una herramienta de modding no reporta, en
general, el resultado de su trabajo.** No es una hipótesis; es el patrón que el
repo ya encontró tres veces, cada vez en un runner distinto y cada vez después de
que el falso verde saliera a producción:

- **BodySlide** (#433): ``OnRun`` no define ``SetExitCode``, así que termina en 0
  aunque ``BuildListBodies`` devuelva ``ret == 3`` ("the following sets failed") y
  aunque no haya construido nada. El veredicto pasó a exigir artefactos en disco y
  ausencia de fallo parcial en su log.
- **Wrye Bash** (#397): una variante que sí parseara habría hecho un backup de
  settings, salido con 0 y reportado ``success`` sobre un Bashed Patch nunca
  regenerado, que las etapas 7 y 9 del DAG consumen.
- **Pandora**: el motor es *error-tolerant* por diseño — ante un nodo defectuoso
  lo revierte a su estado original y **sigue**, así que sale 0 habiendo descartado
  mods de animación. El SOP (``sky_claw/local/AGENTS.md`` §2.4) ya nombraba la
  consecuencia (T-poses, combat anims rotas) mientras el runner reportaba éxito.

Los tres son el mismo defecto. Y en el momento en que se arregló el tercero,
Pandora era el **único** runner del repo que seguía derivando ``success``
únicamente del exit code: xEdit y DynDOLOD ya cruzaban con ``not errors``,
Synthesis con un patrón de éxito en la salida, BodySlide con artefactos + log.

Por qué un ancla y no un párrafo en `AGENTS.md`: el propio `AGENTS.md` dice que
~94% de sus ítems normativos no tienen gate que los haga fallar, y que una regla
sin comando ni test envejece. "Verificá el veredicto de tu runner" es exactamente
esa clase de regla. Esto **enumera**: detecta por AST toda expresión que decide un
``success`` y falla si alguna se apoya sólo en el exit code. Un runner nuevo lo
rompe hasta que declare por qué SU herramienta sí reporta el resultado ahí.

Que el inventario congelado esté **vacío** es el punto: hoy no hay ninguno, y esa
es la propiedad que se quiere conservar.
"""

from __future__ import annotations

import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"

#: Nombres con que el repo nombra el código de salida de un subproceso. Se compara
#: contra el nombre FINAL de la referencia, no contra el objeto que la contiene:
#: en ``proc.returncode`` lo que nombra el valor es ``returncode``, y exigir que
#: ``proc`` estuviera en este set dejaría pasar la forma más común de escribirlo.
NOMBRES_DE_EXIT_CODE = frozenset(
    {
        "return_code",
        "returncode",
        "exit_code",
        "exitcode",
        "exit_status",
        "rc",
    }
)

#: Runners cuyo veredicto SÍ puede derivarse sólo del exit code, con el motivo
#: verificado contra la fuente de la herramienta. Vacío a propósito: ninguna de
#: las herramientas de este pipeline reporta su resultado ahí. Agregar una entrada
#: es una afirmación sobre el binario, no una excusa para el runner — y sin
#: procedencia real es exactamente el falso verde que este archivo documenta.
VEREDICTO_SOLO_POR_EXIT_CODE: dict[str, str] = {}


def _referencias(expr: ast.expr) -> set[str]:
    """Nombres que la expresión consulta, quedándose con el final de cada cadena.

    ``proc.returncode`` aporta ``returncode`` y no ``proc``; ``self.config.game``
    aporta ``game``. Descender por la cadena de acceso metería el nombre del
    objeto contenedor en el set y volvería inútil la comparación de subconjunto:
    ``{proc, returncode}`` ya no está contenido en los nombres de exit code, así
    que el ofensor más frecuente pasaría sin romper nada.

    Una vez que un ``Attribute``/``Subscript`` nombra el exit code (``.attr`` o
    una clave string), lo que PRODUJO ese objeto —una llamada, una cadena de
    métodos, otro subíndice— no es evidencia independiente y no vale la pena
    bajar a inspeccionarlo: ``subprocess.run(argv).returncode`` es el mismo
    defecto que ``proc.returncode``, sólo que con la llamada de por medio. Sin
    este corte, ``run``/``argv`` se colaban en el set y la comparación de
    subconjunto dejaba de marcarlo (review CodeRabbit, PR #439).
    """
    refs: set[str] = set()

    def visitar(nodo: ast.AST) -> None:
        if isinstance(nodo, ast.Attribute):
            refs.add(nodo.attr)
            if nodo.attr in NOMBRES_DE_EXIT_CODE:
                return
            base: ast.AST = nodo.value
            while isinstance(base, ast.Attribute):
                base = base.value
            # Una cadena de puros accesos (``a.b.c``) ya quedó representada por su
            # atributo final. Si la base es otra cosa —una llamada, un subíndice—
            # sí aporta información y se sigue visitando.
            if not isinstance(base, ast.Name):
                visitar(base)
            return
        if isinstance(nodo, ast.Subscript):
            clave = nodo.slice
            if isinstance(clave, ast.Constant) and isinstance(clave.value, str) and clave.value in NOMBRES_DE_EXIT_CODE:
                refs.add(clave.value)
                return
            for hijo in ast.iter_child_nodes(nodo):
                visitar(hijo)
            return
        if isinstance(nodo, ast.Name):
            refs.add(nodo.id)
            return
        for hijo in ast.iter_child_nodes(nodo):
            visitar(hijo)

    visitar(expr)
    return refs


#: Nodos que delimitan un alcance de variables locales. El módulo entra acá
#: también: una asignación suelta fuera de toda función comparte alcance con
#: otras asignaciones sueltas del mismo archivo.
_NODOS_DE_ALCANCE = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)


def _padres(arbol: ast.AST) -> dict[ast.AST, ast.AST]:
    """Mapa hijo → padre de todo el árbol, para resolver el alcance de un nodo."""
    padres: dict[ast.AST, ast.AST] = {}
    for nodo in ast.walk(arbol):
        for hijo in ast.iter_child_nodes(nodo):
            padres[hijo] = nodo
    return padres


def _alcance_de(nodo: ast.AST, padres: dict[ast.AST, ast.AST]) -> ast.AST:
    """La función (o el módulo) que contiene a *nodo* más de cerca.

    Subir hasta la PRIMERA función ancestro —no hasta el módulo— es lo que hace
    que dos funciones distintas puedan reusar el mismo nombre de variable
    (``rc``) con significados no relacionados sin contaminarse entre sí.
    """
    actual = nodo
    while not isinstance(actual, _NODOS_DE_ALCANCE):
        actual = padres[actual]
    return actual


def _asignacion_simple(nodo: ast.AST) -> tuple[str, ast.expr] | None:
    """``(nombre, valor)`` si *nodo* asigna a un único nombre simple, si no ``None``."""
    if isinstance(nodo, ast.Assign) and len(nodo.targets) == 1 and isinstance(nodo.targets[0], ast.Name):
        return nodo.targets[0].id, nodo.value
    if isinstance(nodo, ast.AnnAssign) and isinstance(nodo.target, ast.Name) and nodo.value is not None:
        return nodo.target.id, nodo.value
    return None


def _nombres_contaminados_por_alcance(arbol: ast.AST, padres: dict[ast.AST, ast.AST]) -> dict[ast.AST, frozenset[str]]:
    """Nombres locales cuyo valor depende ÚNICAMENTE del exit code, agrupados por alcance.

    Cierra el hueco de los alias locales: ``exit_ok = proc.returncode == 0``
    seguido de ``Resultado(success=exit_ok)`` evade un chequeo que sólo mira la
    expresión final, porque ``exit_ok`` por sí solo no es un nombre de exit code
    (review CodeRabbit, PR #439). Punto fijo acotado sobre las asignaciones de
    CADA alcance por separado: una variable queda contaminada si su expresión
    sólo referencia nombres de exit code u otras variables YA contaminadas en
    el MISMO alcance. Por alcance y no por archivo, porque dos funciones
    distintas usando el mismo nombre de variable para cosas no relacionadas no
    deben contaminarse entre sí.
    """
    asignaciones_por_alcance: dict[ast.AST, list[tuple[str, ast.expr]]] = {}
    for nodo in ast.walk(arbol):
        asignacion = _asignacion_simple(nodo)
        if asignacion is None or asignacion[0] == "success":
            continue
        alcance = _alcance_de(nodo, padres)
        asignaciones_por_alcance.setdefault(alcance, []).append(asignacion)

    contaminados_por_alcance: dict[ast.AST, frozenset[str]] = {}
    for alcance, asignaciones in asignaciones_por_alcance.items():
        contaminados: set[str] = set()
        # Una cadena de N alias converge en a lo sumo N rondas; el tope evita un
        # bucle patológico ante un ciclo de alias.
        for _ in range(len(asignaciones) + 1):
            crecio = False
            for nombre, expr in asignaciones:
                if nombre in contaminados:
                    continue
                if _es_solo_exit_code(expr, frozenset(contaminados)):
                    contaminados.add(nombre)
                    crecio = True
            if not crecio:
                break
        contaminados_por_alcance[alcance] = frozenset(contaminados)
    return contaminados_por_alcance


def _expresiones_de_success(arbol: ast.AST) -> list[ast.expr]:
    """Toda expresión que decide un ``success``, en las cuatro formas que usa el repo.

    Las cuatro hacen falta: los runners lo pasan como keyword al construir su
    dataclass (``PandoraResult(success=...)``), los servicios lo asignan a una
    variable local antes de armar el dict, y los dicts de contrato lo llevan como
    clave string. Cubrir sólo una forma dejaría el resto del árbol sin ancla.
    """
    expresiones: list[ast.expr] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.keyword) and nodo.arg == "success":
            expresiones.append(nodo.value)
        elif isinstance(nodo, ast.Assign):
            expresiones.extend(
                nodo.value for objetivo in nodo.targets if isinstance(objetivo, ast.Name) and objetivo.id == "success"
            )
        elif (
            isinstance(nodo, ast.AnnAssign)
            and isinstance(nodo.target, ast.Name)
            and nodo.target.id == "success"
            and nodo.value is not None
        ):
            expresiones.append(nodo.value)
        elif isinstance(nodo, ast.Dict):
            expresiones.extend(
                valor
                for clave, valor in zip(nodo.keys, nodo.values, strict=True)
                if isinstance(clave, ast.Constant) and clave.value == "success"
            )
    return expresiones


def _es_solo_exit_code(expr: ast.expr, contaminados: frozenset[str] = frozenset()) -> bool:
    """¿La expresión decide mirando ÚNICAMENTE el código de salida?

    Un literal (``success=False`` en una rama de error explícita) no consulta
    nada y no es un veredicto sobre el trabajo de la herramienta: no cuenta. Lo
    que se persigue es la expresión que afirma ÉXITO apoyada sólo en el exit
    code — directo o a través de variables locales ya contaminadas en su mismo
    alcance (``contaminados``, ver ``_nombres_contaminados_por_alcance``).
    """
    refs = _referencias(expr)
    return bool(refs) and refs <= (NOMBRES_DE_EXIT_CODE | contaminados)


def _analizar(arbol: ast.AST) -> list[ast.expr]:
    """Expresiones de ``success`` que resultan solo-exit-code, resolviendo alias locales."""
    padres = _padres(arbol)
    contaminados_por_alcance = _nombres_contaminados_por_alcance(arbol, padres)
    culpables: list[ast.expr] = []
    for expr in _expresiones_de_success(arbol):
        contaminados = contaminados_por_alcance.get(_alcance_de(expr, padres), frozenset())
        if _es_solo_exit_code(expr, contaminados):
            culpables.append(expr)
    return culpables


def _ofensores() -> dict[str, list[str]]:
    ofensores: dict[str, list[str]] = {}
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        culpables = [ast.unparse(e) for e in _analizar(arbol)]
        if culpables:
            ofensores[archivo.relative_to(RAIZ).as_posix()] = culpables
    return ofensores


def _detecta(fuente: str) -> list[str]:
    return [ast.unparse(e) for e in _analizar(ast.parse(fuente))]


# --------------------------------------------------------------------------- #
# El detector — se valida antes de confiar en su veredicto
# --------------------------------------------------------------------------- #


def test_detecta_las_cuatro_formas_de_decidir_success() -> None:
    """Un ancla que sólo mira una forma sintáctica se esquiva cambiando de forma."""
    assert _detecta("Resultado(success=return_code == 0)") == ["return_code == 0"]
    assert _detecta("success = return_code == 0") == ["return_code == 0"]
    assert _detecta("success: bool = return_code == 0") == ["return_code == 0"]
    assert _detecta('{"success": return_code == 0}') == ["return_code == 0"]


def test_detecta_el_exit_code_leido_desde_un_objeto() -> None:
    """``proc.returncode == 0`` es el mismo defecto escrito distinto.

    Es la forma más natural de escribirlo, y la que un colector de nombres
    ingenuo deja pasar: agregaría ``proc`` al set y la comparación de subconjunto
    fallaría.
    """
    assert _detecta("success = proc.returncode == 0")
    assert _detecta("success = self._proc.exit_code == 0")


def test_detecta_el_exit_code_detras_de_una_llamada_o_un_alias_local() -> None:
    """Los dos caminos de evasión señalados por revisión (PR #439):

    (a) el atributo llega detrás de una llamada (``subprocess.run(argv)``), no
    de un ``Name`` puro — un colector que sigue bajando a inspeccionar la base
    ve ``run``/``argv`` y la comparación de subconjunto deja de marcarlo;
    (b) el exit code se guarda en una variable local ANTES de asignarla a
    ``success`` — un chequeo que sólo mira la expresión final no ve el origen.
    """
    assert _detecta("Resultado(success=subprocess.run(argv).returncode == 0)") == [
        "subprocess.run(argv).returncode == 0"
    ]
    assert _detecta("exit_ok = proc.returncode == 0\nResultado(success=exit_ok)") == ["exit_ok"]


def test_detecta_el_exit_code_en_un_subindice_de_diccionario() -> None:
    """``d["returncode"]`` es el mismo defecto que ``d.returncode`` vía dict.

    Explícitamente nombrado en la revisión ("subíndices"): un resultado que
    viaja como dict en vez de dataclass usa esta forma para lo mismo.
    """
    assert _detecta('success = resultado["returncode"] == 0')


def test_alias_local_no_contamina_entre_funciones_distintas() -> None:
    """El taint tracking es POR FUNCIÓN: dos funciones que reusan el mismo
    nombre de variable con significados no relacionados no deben cruzarse.

    ``estado`` no es un nombre de exit code (a diferencia de ``rc``, que SÍ
    está en ``NOMBRES_DE_EXIT_CODE`` por convención propia y se marcaría de
    todas formas): sólo se contamina si el taint tracking lo deriva de su
    propia asignación. Si la contaminación fuera global al archivo en vez de
    por función, la función B de abajo —cuyo ``estado`` viene de un string, no
    de un exit code— se marcaría como ofensora sólo porque la función A, en
    otra parte del mismo archivo, sí usa ``estado`` para el exit code. Eso
    sería un falso positivo que ningún runner real podría evitar con un simple
    rename ajeno a su propia función.
    """
    fuente = (
        "def funcion_a():\n"
        "    estado = proc.returncode\n"
        "    return Resultado(success=estado == 0)\n"
        "\n"
        "def funcion_b():\n"
        "    estado = 'listo'\n"
        "    return Resultado(success=(estado == 'listo'))\n"
    )
    culpables = _detecta(fuente)
    assert culpables == ["estado == 0"], culpables


def test_no_marca_un_veredicto_que_cruza_otra_evidencia() -> None:
    """Los hermanos que YA hacen lo correcto no deben romper el ancla."""
    assert _detecta("success = return_code == 0 and not errors") == []
    assert _detecta("success = return_code == 0 and artefactos > 0 and not fallo_parcial") == []
    assert _detecta("success = return_code == 0 and not veredicto.fallas") == []
    assert _detecta("success = bool(patron.search(salida)) and return_code == 0") == []


def test_no_marca_literales_ni_reenvios() -> None:
    """``success=False`` en una rama de error no es un veredicto sobre el trabajo,
    y reenviar el ``success`` de otro resultado tampoco lo decide acá."""
    assert _detecta("Resultado(success=False)") == []
    assert _detecta("Resultado(success=True)") == []
    assert _detecta('{"success": result.success}') == []
    assert _detecta('{"success": res.get("success", False)}') == []


def test_no_marca_un_success_ajeno_al_exit_code() -> None:
    assert _detecta("success = destino.exists()") == []
    assert _detecta('{"success": estado == "ok"}') == []


# --------------------------------------------------------------------------- #
# El inventario congelado
# --------------------------------------------------------------------------- #


def test_ningun_runner_deriva_el_exito_solo_del_exit_code() -> None:
    """Igualdad literal: un runner nuevo que confíe en el exit code rompe acá.

    Para arreglarlo, cruzá el exit code con evidencia independiente —artefacto en
    disco, log de la herramienta, patrón de éxito en la salida— como ya hacen
    xEdit, DynDOLOD, Synthesis, BodySlide y Pandora. Si de verdad tu herramienta
    reporta el resultado en su código de salida, declarala en
    `VEREDICTO_SOLO_POR_EXIT_CODE` con la fuente donde lo verificaste.
    """
    assert _ofensores() == VEREDICTO_SOLO_POR_EXIT_CODE, (
        "Cambió el inventario de veredictos derivados sólo del exit code. Ver el "
        "docstring de este módulo: BodySlide, Wrye Bash y Pandora ya salieron a "
        "producción con este defecto."
    )


def test_toda_excepcion_declara_su_procedencia() -> None:
    """Una excepción sin fuente verificada es el falso verde con otro nombre."""
    vacias = sorted(m for m, motivo in VEREDICTO_SOLO_POR_EXIT_CODE.items() if not motivo.strip())
    assert not vacias, f"Procedencia vacía en {vacias}"


def test_las_excepciones_apuntan_a_modulos_que_existen() -> None:
    """Evita que la tabla envejezca apuntando a archivos renombrados o borrados."""
    inexistentes = sorted(m for m in VEREDICTO_SOLO_POR_EXIT_CODE if not (RAIZ / m).is_file())
    assert not inexistentes, f"VEREDICTO_SOLO_POR_EXIT_CODE apunta a módulos inexistentes: {inexistentes}"

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
    """
    refs: set[str] = set()

    def visitar(nodo: ast.AST) -> None:
        if isinstance(nodo, ast.Attribute):
            refs.add(nodo.attr)
            base: ast.AST = nodo.value
            while isinstance(base, ast.Attribute):
                base = base.value
            # Una cadena de puros accesos (``a.b.c``) ya quedó representada por su
            # atributo final. Si la base es otra cosa —una llamada, un subíndice—
            # sí aporta información y se sigue visitando.
            if not isinstance(base, ast.Name):
                visitar(base)
            return
        if isinstance(nodo, ast.Name):
            refs.add(nodo.id)
            return
        for hijo in ast.iter_child_nodes(nodo):
            visitar(hijo)

    visitar(expr)
    return refs


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


def _es_solo_exit_code(expr: ast.expr) -> bool:
    """¿La expresión decide mirando ÚNICAMENTE el código de salida?

    Un literal (``success=False`` en una rama de error explícita) no consulta
    nada y no es un veredicto sobre el trabajo de la herramienta: no cuenta. Lo
    que se persigue es la expresión que afirma ÉXITO apoyada sólo en el exit code.
    """
    refs = _referencias(expr)
    return bool(refs) and refs <= NOMBRES_DE_EXIT_CODE


def _ofensores() -> dict[str, list[str]]:
    ofensores: dict[str, list[str]] = {}
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        culpables = [ast.unparse(e) for e in _expresiones_de_success(arbol) if _es_solo_exit_code(e)]
        if culpables:
            ofensores[archivo.relative_to(RAIZ).as_posix()] = culpables
    return ofensores


def _detecta(fuente: str) -> list[str]:
    return [ast.unparse(e) for e in _expresiones_de_success(ast.parse(fuente)) if _es_solo_exit_code(e)]


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

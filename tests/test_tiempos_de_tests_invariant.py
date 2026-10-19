"""Ancla: los presupuestos de tiempo de tests que fallaron de forma intermitente en CI.

Tres familias de tests, en tres archivos, dieron fallas que no eran del código
bajo prueba sino de un tope de reloj demasiado ajustado para un runner de
Windows cargado (el CI de Windows tarda ~30 min y comparte CPU):

* ``test_runtime_vault_operator_verifier.py``: el hijo verificador no alcanzaba a
  conectar el pipe en ``timeout_seconds=2.0``.
* ``test_vfs_teardown_deadline.py``: ``asyncio.wait_for(sesion.cancel(),
  timeout=1.0)`` vencía antes de que el teardown terminara de propagar el
  ``VfsTeardownDeadlineError`` que el test espera (``deadline=0.2``).
* ``test_app_context_arc01_arc03.py``: con ``_startup_shutdown_timeout_s=0.01`` el
  cleanup todavía no había arrancado cuando vencía el presupuesto, y
  ``ejecuciones == 1`` daba 0.

Un caso escrito a mano para el hermano que falta no ataja al tercero, así que
cada familia se **enumera** por AST (sin importar los módulos: el verificador
sólo carga en Windows) y se congela por igualdad literal. Agregar un tope corto
nuevo rompe el ancla hasta que se le decide su receta.
"""

from __future__ import annotations

import ast
import pathlib

_TESTS = pathlib.Path(__file__).resolve().parent

# Un tope que sólo existe como red de emergencia (la prueba espera una excepción
# distinta del timeout) no puede agotarse por una pausa del runner.
_MARGEN_MINIMO_S = 5.0
# Umbral por debajo del cual un presupuesto del AppContext es "de reloj".
_PRESUPUESTO_CORTO_S = 0.1


def _arbol(nombre: str) -> ast.Module:
    return ast.parse((_TESTS / nombre).read_text(encoding="utf-8"))


def _constante_numerica(nodo: ast.AST) -> float | None:
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, (int, float)) and not isinstance(nodo.value, bool):
        return float(nodo.value)
    return None


def _funciones(arbol: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _nombre_punteado(nodo: ast.AST) -> str:
    if isinstance(nodo, ast.Name):
        return nodo.id
    if isinstance(nodo, ast.Attribute):
        return f"{_nombre_punteado(nodo.value)}.{nodo.attr}"
    return ""


# ---------------------------------------------------------------------------
# Familia 1: OperatorVerifierBridge(timeout_seconds=...)
# ---------------------------------------------------------------------------

# El presupuesto corto ES el comportamiento bajo prueba: el worker duerme 2 s y el
# test espera `OperatorVerifierTimeoutError` con un tope de 0.5 s.
_VERIFICADOR_EXENTOS = frozenset({"test_w10_timeout_bounded"})


def _topes_del_verificador() -> list[tuple[str, float]]:
    sitios: list[tuple[str, float]] = []
    for funcion in _funciones(_arbol("test_runtime_vault_operator_verifier.py")):
        for nodo in ast.walk(funcion):
            if not (isinstance(nodo, ast.Call) and _nombre_punteado(nodo.func) == "OperatorVerifierBridge"):
                continue
            for palabra in nodo.keywords:
                valor = _constante_numerica(palabra.value) if palabra.arg == "timeout_seconds" else None
                if valor is not None:
                    sitios.append((funcion.name, valor))
    return sitios


def test_el_verificador_no_tiene_topes_de_camino_feliz_por_debajo_del_margen() -> None:
    sitios = _topes_del_verificador()
    assert len(sitios) > 30, "el escáner dejó de ver los OperatorVerifierBridge del archivo"

    cortos = {nombre for nombre, valor in sitios if valor < _MARGEN_MINIMO_S}
    assert cortos == _VERIFICADOR_EXENTOS, (
        f"topes por debajo de {_MARGEN_MINIMO_S}s fuera de la lista de exentos: "
        f"{sorted(cortos - _VERIFICADOR_EXENTOS)}. Si la prueba espera una excepción que no es el "
        "timeout, el tope es una red de emergencia y debe superar el margen; si el tope corto ES lo "
        "que se prueba, agregá el test a _VERIFICADOR_EXENTOS."
    )


# ---------------------------------------------------------------------------
# Familia 2: pytest.raises(VfsTeardownDeadlineError) + wait_for(sesion.cancel(), timeout=T)
# ---------------------------------------------------------------------------

_VFS_SITIOS_ESPERADOS = frozenset(
    {
        "test_teardown_con_bridge_conectado_y_sin_worker_exit_falla_con_deadline",
        "test_worker_exit_tardio_no_sobrescribe_ni_corrompe_estado",
        "test_quarantine_marca_instancia_y_rechaza_nuevos_jobs",
    }
)


def _topes_de_wait_for_de_cancel() -> list[tuple[str, float]]:
    sitios: list[tuple[str, float]] = []
    for funcion in _funciones(_arbol("test_vfs_teardown_deadline.py")):
        for con in (n for n in ast.walk(funcion) if isinstance(n, ast.With)):
            espera_deadline = any(
                isinstance(item.context_expr, ast.Call)
                and _nombre_punteado(item.context_expr.func) == "pytest.raises"
                and item.context_expr.args
                and _nombre_punteado(item.context_expr.args[0]) == "VfsTeardownDeadlineError"
                for item in con.items
            )
            if not espera_deadline:
                continue
            for nodo in ast.walk(con):
                if not (isinstance(nodo, ast.Call) and _nombre_punteado(nodo.func) == "asyncio.wait_for"):
                    continue
                if not (
                    nodo.args
                    and isinstance(nodo.args[0], ast.Call)
                    and _nombre_punteado(nodo.args[0].func).endswith(".cancel")
                ):
                    continue
                for palabra in nodo.keywords:
                    valor = _constante_numerica(palabra.value) if palabra.arg == "timeout" else None
                    if valor is not None:
                        sitios.append((funcion.name, valor))
    return sitios


def test_el_wait_for_que_espera_el_deadline_del_teardown_tiene_margen() -> None:
    sitios = _topes_de_wait_for_de_cancel()

    assert {nombre for nombre, _ in sitios} == _VFS_SITIOS_ESPERADOS, (
        "cambió la familia `pytest.raises(VfsTeardownDeadlineError)` + `wait_for(sesion.cancel(), ...)`: "
        "decidí la receta del tope del sitio nuevo y actualizá _VFS_SITIOS_ESPERADOS"
    )
    cortos = sorted((nombre, valor) for nombre, valor in sitios if valor < _MARGEN_MINIMO_S)
    assert not cortos, (
        f"el `wait_for` es sólo una red de emergencia (la excepción la lanza el deadline de 0.2 s del broker): "
        f"un tope menor a {_MARGEN_MINIMO_S}s se agota con el runner cargado y el test falla con TimeoutError. "
        f"Sitios: {cortos}"
    )


# ---------------------------------------------------------------------------
# Familia 3: AppContext._startup_shutdown_timeout_s de reloj
# ---------------------------------------------------------------------------

# Estos dos presupuestos de 0.01 s son seguros porque el test espera `entro`
# (el cleanup ya arrancó) ANTES de depender de que el presupuesto venza.
_APPCONTEXT_PRESUPUESTO_CORTO = frozenset(
    {
        "test_stop_agota_presupuesto_si_startup_ignora_cancelacion",
        "test_stop_no_cuelga_si_background_task_ignora_cancelacion",
    }
)


def _espera_entro(funcion: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(nodo, ast.Await)
        and isinstance(nodo.value, ast.Call)
        and _nombre_punteado(nodo.value.func) == "asyncio.wait_for"
        and nodo.value.args
        and isinstance(nodo.value.args[0], ast.Call)
        and _nombre_punteado(nodo.value.args[0].func) == "entro.wait"
        for nodo in ast.walk(funcion)
    )


def test_los_presupuestos_de_reloj_del_appcontext_esperan_el_arranque_del_cleanup() -> None:
    cortos: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for funcion in _funciones(_arbol("test_app_context_arc01_arc03.py")):
        for nodo in ast.walk(funcion):
            if (
                isinstance(nodo, ast.Assign)
                and any(_nombre_punteado(t) == "ctx._startup_shutdown_timeout_s" for t in nodo.targets)
                and (valor := _constante_numerica(nodo.value)) is not None
                and valor < _PRESUPUESTO_CORTO_S
            ):
                cortos[funcion.name] = funcion

    assert set(cortos) == _APPCONTEXT_PRESUPUESTO_CORTO, (
        f"presupuestos de reloj < {_PRESUPUESTO_CORTO_S}s fuera de la lista: "
        f"{sorted(set(cortos) ^ _APPCONTEXT_PRESUPUESTO_CORTO)}. Con un presupuesto tan corto el cleanup "
        "puede no haber arrancado cuando vence (el runner de Windows pausa más de 10 ms) y "
        "`ejecuciones == 1` da 0; usá un presupuesto mayor o esperá `entro` antes."
    )
    sin_barrera = sorted(nombre for nombre, funcion in cortos.items() if not _espera_entro(funcion))
    assert not sin_barrera, f"presupuesto de reloj corto sin esperar `entro.wait()`: {sin_barrera}"

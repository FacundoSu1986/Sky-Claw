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

Dos propiedades del escáner, para que no se pueda esquivar sin querer:

* *un tope con nombre sigue siendo un tope*: las constantes numéricas de módulo
  (``TOPE = 0.01``) se resuelven. Una expresión que el escáner no puede resolver
  (``TOPE * 2``, un argumento de helper, ``**kwargs``) **falla cerrado**: se
  reporta como sitio sin resolver en vez de contarse como "no es corto".
* *la familia se enumera en todos los archivos y con cualquier receptor*: el presupuesto
  del ``AppContext`` se escanea en cada ``tests/test_*.py`` que escribe el atributo, se
  llame ``ctx`` o no; ``+=`` y ``setattr(...)`` no se resuelven y fallan cerrado.

**Límite:** el orden barrera→``stop()`` se verifica por posición en el fuente,
no por dominancia de flujo de control (una barrera dentro de un ``if`` que no se
ejecuta pasaría). Los sitios que no resuelve el escáner los frena el fail-closed;
los que el escáner resuelve mal por sombreado de nombres no los ataja nadie.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Sequence

_TESTS = pathlib.Path(__file__).resolve().parent

# Un tope que sólo existe como red de emergencia (la prueba espera una excepción
# distinta del timeout) no puede agotarse por una pausa del runner.
_MARGEN_MINIMO_S = 5.0
# Umbral por debajo del cual un presupuesto del AppContext es "de reloj".
_PRESUPUESTO_CORTO_S = 0.1

_Funcion = ast.FunctionDef | ast.AsyncFunctionDef


def _arbol(nombre: str) -> ast.Module:
    return ast.parse((_TESTS / nombre).read_text(encoding="utf-8"))


def _numero(nodo: ast.AST) -> float | None:
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, (int, float)) and not isinstance(nodo.value, bool):
        return float(nodo.value)
    return None


def _constantes_del_modulo(arbol: ast.Module) -> dict[str, float]:
    """Constantes numéricas de nivel módulo (``TOPE = 0.25`` o ``TOPE: float = 0.25``)."""
    constantes: dict[str, float] = {}
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign) and len(nodo.targets) == 1 and isinstance(nodo.targets[0], ast.Name):
            nombre, valor = nodo.targets[0].id, nodo.value
        elif isinstance(nodo, ast.AnnAssign) and isinstance(nodo.target, ast.Name) and nodo.value is not None:
            nombre, valor = nodo.target.id, nodo.value
        else:
            continue
        if (numero := _numero(valor)) is not None:
            constantes[nombre] = numero
    return constantes


def _valor_numerico(nodo: ast.AST | None, constantes: dict[str, float]) -> float | None:
    """Valor del tope, o ``None`` si el escáner no puede resolverlo (el llamador falla cerrado)."""
    if nodo is None:
        return None
    if (numero := _numero(nodo)) is not None:
        return numero
    if isinstance(nodo, ast.Name):
        return constantes.get(nodo.id)
    return None


def _funciones(arbol: ast.Module) -> list[_Funcion]:
    return [n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _nombre_punteado(nodo: ast.AST) -> str:
    if isinstance(nodo, ast.Name):
        return nodo.id
    if isinstance(nodo, ast.Attribute):
        return f"{_nombre_punteado(nodo.value)}.{nodo.attr}"
    return ""


def _sin_resolver(sitios: Sequence[tuple[object, ...]]) -> list[tuple[object, ...]]:
    """Sitios cuyo último campo (el valor) no se pudo resolver."""
    return [sitio for sitio in sitios if sitio[-1] is None]


# ---------------------------------------------------------------------------
# Familia 1: OperatorVerifierBridge(timeout_seconds=..., *_timeout_seconds=...)
# ---------------------------------------------------------------------------

# El presupuesto corto ES el comportamiento bajo prueba:
# * w10: el worker duerme 2 s y el test espera `OperatorVerifierTimeoutError` con un tope de 0.5 s.
# * s14: `io_timeout_seconds=0.4` < los 0.8 s que duerme el worker, pero el total (5.0 s) alcanza.
# * s15: `operation_timeout_seconds=0.4` es el tope total que el test quiere vencer, y su
#   `io_timeout_seconds=1.0` tiene que ser MAYOR que ése para que venza el total y no el de I/O.
# Residual conocido: s14 es un camino feliz con un margen de 0.4 s por diseño; sin falla observada.
_VERIFICADOR_EXENTOS = frozenset(
    {
        ("test_w10_timeout_bounded", "timeout_seconds"),
        ("test_s14_operation_exceeds_io_timeout_but_within_operation_timeout_succeeds", "io_timeout_seconds"),
        ("test_s15_operation_exceeds_operation_timeout_kills_and_reaps", "io_timeout_seconds"),
        ("test_s15_operation_exceeds_operation_timeout_kills_and_reaps", "operation_timeout_seconds"),
    }
)


def _topes_del_verificador(arbol: ast.Module) -> list[tuple[str, str, float | None]]:
    constantes = _constantes_del_modulo(arbol)
    sitios: list[tuple[str, str, float | None]] = []
    for funcion in _funciones(arbol):
        for nodo in ast.walk(funcion):
            if not (isinstance(nodo, ast.Call) and _nombre_punteado(nodo.func) == "OperatorVerifierBridge"):
                continue
            for palabra in nodo.keywords:
                if palabra.arg is None:  # `**kwargs`: no se puede ver qué topes trae
                    sitios.append((funcion.name, "**", None))
                elif palabra.arg.endswith("timeout_seconds"):
                    sitios.append((funcion.name, palabra.arg, _valor_numerico(palabra.value, constantes)))
    return sitios


def test_el_verificador_no_tiene_topes_de_camino_feliz_por_debajo_del_margen() -> None:
    sitios = _topes_del_verificador(_arbol("test_runtime_vault_operator_verifier.py"))
    assert len(sitios) > 30, "el escáner dejó de ver los OperatorVerifierBridge del archivo"

    sin_resolver = _sin_resolver(sitios)
    assert not sin_resolver, (
        f"topes del verificador que el escáner no puede resolver: {sin_resolver}. Usá un literal o una "
        "constante numérica de módulo, o ampliá el escáner."
    )
    cortos = {(nombre, palabra) for nombre, palabra, valor in sitios if valor is not None and valor < _MARGEN_MINIMO_S}
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


def _topes_de_wait_for_de_cancel(arbol: ast.Module) -> list[tuple[str, float | None]]:
    constantes = _constantes_del_modulo(arbol)
    sitios: list[tuple[str, float | None]] = []
    for funcion in _funciones(arbol):
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
                # El tope puede venir por keyword o como segundo argumento posicional.
                tope = next(
                    (p.value for p in nodo.keywords if p.arg == "timeout"),
                    nodo.args[1] if len(nodo.args) > 1 else None,
                )
                sitios.append((funcion.name, _valor_numerico(tope, constantes)))
    return sitios


def test_el_wait_for_que_espera_el_deadline_del_teardown_tiene_margen() -> None:
    sitios = _topes_de_wait_for_de_cancel(_arbol("test_vfs_teardown_deadline.py"))

    assert {nombre for nombre, _ in sitios} == _VFS_SITIOS_ESPERADOS, (
        "cambió la familia `pytest.raises(VfsTeardownDeadlineError)` + `wait_for(sesion.cancel(), ...)`: "
        "decidí la receta del tope del sitio nuevo y actualizá _VFS_SITIOS_ESPERADOS"
    )
    sin_resolver = _sin_resolver(sitios)
    assert not sin_resolver, (
        f"topes del `wait_for` que el escáner no puede resolver: {sin_resolver}. Usá un literal o una "
        "constante numérica de módulo, o ampliá el escáner."
    )
    cortos = sorted((nombre, valor) for nombre, valor in sitios if valor is not None and valor < _MARGEN_MINIMO_S)
    assert not cortos, (
        f"el `wait_for` es sólo una red de emergencia (la excepción la lanza el deadline de 0.2 s del broker): "
        f"un tope menor a {_MARGEN_MINIMO_S}s se agota con el runner cargado y el test falla con TimeoutError. "
        f"Sitios: {cortos}"
    )


# ---------------------------------------------------------------------------
# Familia 3: AppContext._startup_shutdown_timeout_s de reloj (en todos los archivos de tests)
# ---------------------------------------------------------------------------

# Estos presupuestos cortos son seguros porque el test espera `entro` (la task o el
# startup que se niega a morir ya arrancó) ANTES de llamar a `stop()`, que es lo que
# consume el presupuesto.
_APPCONTEXT_CON_BARRERA = frozenset(
    {
        ("test_app_context_arc01_arc03.py", "test_stop_agota_presupuesto_si_startup_ignora_cancelacion"),
        ("test_app_context_arc01_arc03.py", "test_stop_no_cuelga_si_background_task_ignora_cancelacion"),
        (
            "test_stop_cleanup_finally.py",
            "test_stop_corre_el_cleanup_aunque_una_task_de_fondo_ignore_la_cancelacion",
        ),
        ("test_stop_cleanup_finally.py", "test_stop_no_ejecuta_el_cleanup_dos_veces_tras_el_timeout"),
    }
)
# Sin nada que esperar antes del `stop()`: el trabajo colgado de etapa 9 lo crea y lo
# espera el propio `stop()` y el cleanup es instantáneo. Residual conocido (sin falla
# observada): el presupuesto de 0.05 s sigue siendo de reloj.
_APPCONTEXT_EXENTOS = frozenset({("test_stop_cleanup_finally.py", "test_un_trabajo_de_lods_colgado_no_cuelga_el_stop")})


def _espera_entro(funcion: _Funcion) -> list[tuple[int, int]]:
    """Posiciones de ``await asyncio.wait_for(entro.wait(), ...)`` en el fuente."""
    return [
        (nodo.lineno, nodo.col_offset)
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Await)
        and isinstance(nodo.value, ast.Call)
        and _nombre_punteado(nodo.value.func) == "asyncio.wait_for"
        and nodo.value.args
        and isinstance(nodo.value.args[0], ast.Call)
        and _nombre_punteado(nodo.value.args[0].func) == "entro.wait"
    ]


def _llamadas_a_stop(funcion: _Funcion, receptores: frozenset[str]) -> list[tuple[int, int]]:
    """Posiciones de ``<receptor>.stop(...)`` para cada receptor al que se le escribió el presupuesto."""
    nombres = {f"{receptor}.stop" for receptor in receptores}
    return [
        (nodo.lineno, nodo.col_offset)
        for nodo in ast.walk(funcion)
        if isinstance(nodo, ast.Call) and _nombre_punteado(nodo.func) in nombres
    ]


def _barrera_antes_del_stop(funcion: _Funcion, receptores: frozenset[str]) -> bool:
    """Hay barrera y hay ``stop()``, y la primera barrera precede al primer ``stop()``.

    Falla cerrado: sin un ``<receptor>.stop()`` visible la regla no se puede evaluar.
    """
    barreras, paradas = _espera_entro(funcion), _llamadas_a_stop(funcion, receptores)
    return bool(barreras and paradas and min(barreras) < min(paradas))


_ATRIBUTO_PRESUPUESTO = "_startup_shutdown_timeout_s"


def _escrituras_del_presupuesto(funcion: _Funcion, constantes: dict[str, float]) -> list[tuple[str, float | None]]:
    """``(receptor, valor)`` de cada escritura del presupuesto en la función, con cualquier receptor.

    La regla es sobre el atributo, no sobre cómo se llame la variable. Lo que no es una
    asignación simple (``+=``, ``setattr(...)``) o no tiene valor numérico resoluble da
    ``None``: falla cerrado.
    """
    escrituras: list[tuple[str, float | None]] = []
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Assign):
            objetivos, valor = nodo.targets, _valor_numerico(nodo.value, constantes)
        elif isinstance(nodo, ast.AnnAssign):
            objetivos, valor = [nodo.target], _valor_numerico(nodo.value, constantes)
        elif isinstance(nodo, ast.AugAssign):
            objetivos, valor = [nodo.target], None
        elif (
            isinstance(nodo, ast.Call)
            and _nombre_punteado(nodo.func).endswith("setattr")
            and any(isinstance(a, ast.Constant) and a.value == _ATRIBUTO_PRESUPUESTO for a in nodo.args)
        ):
            escrituras.append((_nombre_punteado(nodo.args[0]) if nodo.args else "", None))
            continue
        else:
            continue
        escrituras.extend(
            (_nombre_punteado(t.value), valor)
            for t in objetivos
            if isinstance(t, ast.Attribute) and t.attr == _ATRIBUTO_PRESUPUESTO
        )
    return escrituras


def _presupuestos_de_reloj() -> tuple[
    dict[tuple[str, str], tuple[_Funcion, frozenset[str]]], list[tuple[str, str, float | None]]
]:
    """Tests que escriben ``_startup_shutdown_timeout_s`` por debajo del umbral, en todos los archivos.

    Devuelve los cortos con su función y los receptores a los que se les escribió, y todas las
    escrituras (``None`` = no resoluble).
    """
    cortos: dict[tuple[str, str], tuple[_Funcion, frozenset[str]]] = {}
    todos: list[tuple[str, str, float | None]] = []
    for ruta in sorted(_TESTS.glob("test_*.py")):
        texto = ruta.read_text(encoding="utf-8")
        if _ATRIBUTO_PRESUPUESTO not in texto:
            continue
        arbol = ast.parse(texto)
        constantes = _constantes_del_modulo(arbol)
        for funcion in _funciones(arbol):
            receptores_cortos: set[str] = set()
            for receptor, valor in _escrituras_del_presupuesto(funcion, constantes):
                todos.append((ruta.name, funcion.name, valor))
                if valor is not None and valor < _PRESUPUESTO_CORTO_S:
                    receptores_cortos.add(receptor)
            if receptores_cortos:
                cortos[(ruta.name, funcion.name)] = (funcion, frozenset(receptores_cortos))
    return cortos, todos


def test_los_presupuestos_de_reloj_del_appcontext_esperan_el_arranque_del_cleanup() -> None:
    cortos, todos = _presupuestos_de_reloj()

    assert len(todos) >= 9, "el escáner dejó de ver las asignaciones de `_startup_shutdown_timeout_s`"
    sin_resolver = _sin_resolver(todos)
    assert not sin_resolver, (
        f"presupuestos del AppContext que el escáner no puede resolver: {sin_resolver}. Usá un literal o "
        "una constante numérica de módulo, o ampliá el escáner."
    )
    esperados = _APPCONTEXT_CON_BARRERA | _APPCONTEXT_EXENTOS
    assert set(cortos) == esperados, (
        f"presupuestos de reloj < {_PRESUPUESTO_CORTO_S}s fuera de la lista: "
        f"{sorted(set(cortos) ^ esperados)}. Con un presupuesto tan corto el cleanup "
        "puede no haber arrancado cuando vence (el runner de Windows pausa más de 10 ms) y "
        "`ejecuciones == 1` da 0; usá un presupuesto mayor o esperá `entro` antes."
    )
    sin_barrera = sorted(
        clave
        for clave, (funcion, receptores) in cortos.items()
        if clave not in _APPCONTEXT_EXENTOS and not _barrera_antes_del_stop(funcion, receptores)
    )
    assert not sin_barrera, f"presupuesto de reloj corto sin esperar `entro.wait()` ANTES del `stop()`: {sin_barrera}"


# ---------------------------------------------------------------------------
# El escáner mismo: casos sintéticos para que los chequeos de arriba no puedan aflojarse en silencio
# ---------------------------------------------------------------------------


def _primera_funcion(fuente: str) -> _Funcion:
    return _funciones(ast.parse(fuente))[0]


def test_valor_numerico_constante_de_modulo_se_resuelve_e_irresoluble_da_none() -> None:
    arbol = ast.parse("TOPE = 0.01\nENTERO: int = 3\nFLAG = True\nCALCULADO = TOPE * 2\n")
    constantes = _constantes_del_modulo(arbol)

    assert constantes == {"TOPE": 0.01, "ENTERO": 3.0}
    assert _valor_numerico(ast.parse("TOPE", mode="eval").body, constantes) == 0.01
    assert _valor_numerico(ast.parse("0.2", mode="eval").body, constantes) == 0.2
    for fuente in ("DESCONOCIDO", "TOPE * 2", "FLAG", "'texto'", "f()"):
        assert _valor_numerico(ast.parse(fuente, mode="eval").body, constantes) is None, fuente
    assert _valor_numerico(None, constantes) is None


def test_barrera_antes_del_stop_exige_orden_y_sin_stop_falla_cerrado() -> None:
    barrera = "    await asyncio.wait_for(entro.wait(), timeout=1.0)\n"
    parada = "    await asyncio.wait_for(ctx.stop(), timeout=5.0)\n"
    ctx = frozenset({"ctx"})

    assert _barrera_antes_del_stop(_primera_funcion(f"async def t():\n{barrera}{parada}"), ctx)
    assert not _barrera_antes_del_stop(_primera_funcion(f"async def t():\n{parada}{barrera}"), ctx)
    assert not _barrera_antes_del_stop(_primera_funcion(f"async def t():\n{parada}"), ctx)
    assert not _barrera_antes_del_stop(_primera_funcion(f"async def t():\n{barrera}"), ctx)
    # El `stop()` que cuenta es el del receptor al que se le escribió el presupuesto, no el de otro.
    assert not _barrera_antes_del_stop(_primera_funcion(f"async def t():\n{barrera}{parada}"), frozenset({"app"}))
    assert _barrera_antes_del_stop(
        _primera_funcion(f"async def t():\n{barrera}{parada.replace('ctx', 'app')}"), frozenset({"app"})
    )


def test_escrituras_del_presupuesto_cualquier_receptor_y_formas_no_simples_fallan_cerrado() -> None:
    fuente = (
        "def t(monkeypatch):\n"
        "    ctx._startup_shutdown_timeout_s = 0.01\n"
        "    self.app._startup_shutdown_timeout_s = TOPE\n"
        "    otro._startup_shutdown_timeout_s: float = 0.2\n"
        "    ctx._startup_shutdown_timeout_s += 1\n"
        "    monkeypatch.setattr(ctx, '_startup_shutdown_timeout_s', 0.01)\n"
        "    ctx._otro_atributo = 0.01\n"
    )

    assert _escrituras_del_presupuesto(_primera_funcion(fuente), {"TOPE": 0.05}) == [
        ("ctx", 0.01),
        ("self.app", 0.05),
        ("otro", 0.2),
        ("ctx", None),
        ("ctx", None),
    ]

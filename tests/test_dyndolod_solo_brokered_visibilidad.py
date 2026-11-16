"""H-13 (B-4): «DynDOLOD solo» bajo brokered no spawnea sin una prueba de visibilidad del load order.

La auditoría de la etapa 9 anotó como hueco que el camino legacy sin handoff (``handoff=None``: ``run_texgen=False``
sin un ``TexGen Output`` preservado y gateado) bajo el backend brokered «spawnea sin prueba de visibilidad del load
order completo», porque el sensor físico U-01 no aplica bajo USVFS y ahí no hay artifact que pase por el gate de seis
pasos. Verificado contra el código, el hueco **ya estaba cerrado a nivel de plugins**: ``BrokeredDynDOLODSpawnStrategy
.spawn`` arma el job con ``expected_plugins`` (el conjunto COMPLETO de plugins activos) en las dos ramas, y el worker
rechaza el dispatch si alguno no es visible en el Data virtual (``tests/test_vfs_worker.py``). Lo que faltaba era
**anclarlo**: nada impedía que una rama nueva de ``spawn`` armara su job sin esa evidencia.

Lo que este módulo NO prueba, y la auditoría tampoco lo cierra: la visibilidad de los archivos que no son plugins
(meshes, texturas) de cada mod. Eso es la hipótesis H-2 (el overlay podría mostrar un árbol viejo o parcial y el
canary acertar igual) y se mide en rig (S-2) antes de diseñar un muestreo. Aplica por igual a las dos rutas, con o
sin handoff.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

import sky_claw.local.mo2.brokered_dyndolod as brokered
from sky_claw.local.mo2.vfs_attestation import read_enabled_plugins
from sky_claw.local.mo2.vfs_contracts import VfsJob
from tests.test_dyndolod_spawn_handoff_binding import (
    _PERFIL,
    _BridgeFalso,
    _escenario,
    _exe_dyndolod,
    _gate_pasa,
    _strategy,
)

PLUGINS_ACTIVOS = ("MiMod.esp", "OtroMod.esp")


class _BridgeQueRegistraElJob(_BridgeFalso):
    """El broker falso de siempre, que además guarda el ``VfsJob`` con el que se abrió cada sesión."""

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[VfsJob] = []

    async def open_session(self, job: VfsJob, **kwargs: Any) -> Any:
        self.jobs.append(job)
        return await super().open_session(job, **kwargs)


def _declarar_plugins_activos(esc: Any) -> None:
    perfil = esc.data_root / "profiles" / _PERFIL
    perfil.mkdir(parents=True, exist_ok=True)
    (perfil / "plugins.txt").write_text("".join(f"*{p}\n" for p in PLUGINS_ACTIVOS), encoding="utf-8")


async def _spawn_dyndolod(tmp_path: pathlib.Path, *, con_handoff: bool) -> tuple[VfsJob, tuple[str, ...]]:
    """El job con el que spawneó la strategy y lo que el ORÁCULO dice que hay que demostrar visible.

    El oráculo es la misma función que define «plugins activos esperados» (``read_enabled_plugins``): masters base +
    los marcados con ``*`` en ``plugins.txt``. Comparar contra ella —y no contra la lista que escribió el test— es
    lo que permite exigir IGUALDAD: ``read_enabled_plugins`` suma masters que el test no declara.
    """
    esc = _escenario(tmp_path)
    _declarar_plugins_activos(esc)
    oraculo = read_enabled_plugins(
        esc.data_root / "profiles" / _PERFIL / "plugins.txt", game_data_dir=esc.physical_data
    )
    bridge = _BridgeQueRegistraElJob()
    approval = await _gate_pasa(esc, bridge, tmp_path) if con_handoff else None
    await _strategy(esc, bridge, tmp_path).spawn(
        executable=_exe_dyndolod(tmp_path),
        args=[],
        tool_name="DynDOLOD",
        cwd=tmp_path,
        timeout=30.0,
        handoff=approval,
    )
    assert len(bridge.jobs) == 1
    return bridge.jobs[0], oraculo


@pytest.mark.parametrize(
    "con_handoff", [False, True], ids=["dyndolod_solo_sin_handoff", "texgen_a_dyndolod_con_handoff"]
)
async def test_todo_spawn_brokered_de_dyndolod_lleva_el_conjunto_completo_de_plugins_activos(
    tmp_path: pathlib.Path, con_handoff: bool
) -> None:
    """Las dos rutas de ``spawn`` entregan al worker EXACTAMENTE lo que tiene que demostrar visible bajo USVFS.

    Igualdad y no subconjunto (review PR-Agent en #953): con ``<=`` un plugin de MÁS en el job pasaba el test y el
    worker rechazaría el dispatch por un plugin que no hay que exigir; uno de MENOS, también, y la prueba sería
    incompleta. Además se conserva la comprobación de que lo declarado en ``plugins.txt`` está adentro: sin ella,
    un oráculo vacío haría trivial la igualdad.
    """
    job, oraculo = await _spawn_dyndolod(tmp_path, con_handoff=con_handoff)

    esperados = job.payload["expected_plugins"]
    assert isinstance(esperados, list)
    assert sorted(esperados) == sorted(oraculo)
    assert {nombre.lower() for nombre in PLUGINS_ACTIVOS} <= {nombre.lower() for nombre in esperados}


async def test_las_dos_rutas_piden_la_misma_evidencia_de_visibilidad(tmp_path: pathlib.Path) -> None:
    """Sin handoff no se pide MENOS que con handoff: el conjunto es el mismo (no depende del artifact gateado)."""
    sin, _ = await _spawn_dyndolod(tmp_path / "sin", con_handoff=False)
    con, _ = await _spawn_dyndolod(tmp_path / "con", con_handoff=True)

    assert sorted(sin.payload["expected_plugins"]) == sorted(con.payload["expected_plugins"])  # type: ignore[arg-type]


def _arbol_del_modulo() -> ast.Module:
    return ast.parse(pathlib.Path(brokered.__file__).read_text(encoding="utf-8"))


def _funcion_spawn(arbol: ast.Module) -> ast.AsyncFunctionDef:
    (clase,) = [n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == "BrokeredDynDOLODSpawnStrategy"]
    (spawn,) = [n for n in clase.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "spawn"]
    return spawn


# Toda forma de CONSTRUIR un VfsJob además del constructor del dataclass: cada classmethod que declara la clase. Se
# detectan por introspección (un constructor nuevo entra solo a la familia) y se congelan por igualdad literal en
# ``test_los_constructores_de_vfsjob_estan_enumerados``: añadir uno rompe ese ancla hasta que alguien decida si el
# detector lo ve.
_CONSTRUCTORES_DE_VFSJOB = frozenset(nombre for nombre, valor in vars(VfsJob).items() if isinstance(valor, classmethod))


def _nombres_locales_de_vfsjob(arbol: ast.AST) -> frozenset[str]:
    """Con qué nombres se refiere el módulo a la clase: ``VfsJob`` y cada alias de ``from ... import VfsJob as X``."""
    nombres = {"VfsJob"}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            nombres.update(alias.asname or alias.name for alias in nodo.names if alias.name == "VfsJob")
    return frozenset(nombres)


def _nombra_la_clase(expr: ast.expr, nombres: frozenset[str]) -> bool:
    """``VfsJob``, un alias importado con ``as``, o ``<módulo, alias de módulo o ruta completa>.VfsJob``."""
    if isinstance(expr, ast.Name):
        return expr.id in nombres
    return isinstance(expr, ast.Attribute) and expr.attr == "VfsJob"


def _es_construccion_de_vfsjob(nodo: ast.AST, nombres: frozenset[str]) -> bool:
    """Cualquier forma de armar un ``VfsJob``: ``VfsJob(...)`` directo o un classmethod constructor.

    El ancla no puede depender de cómo se importe la clase (review PR-Agent en #953 y en #956): ni de la ruta
    (``VfsJob`` / ``vfs.VfsJob`` / ruta completa), ni de un alias de la clase (``import VfsJob as VFS``), ni de que el
    sitio use ``create`` en vez de otra forma de construir. Un detector que ve solo una forma da un falso verde
    cuando el módulo mezcla estilos: cuenta el sitio que reconoce y no el otro.
    """
    if not isinstance(nodo, ast.Call):
        return False
    if _nombra_la_clase(nodo.func, nombres):
        return True
    return (
        isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr in _CONSTRUCTORES_DE_VFSJOB
        and _nombra_la_clase(nodo.func.value, nombres)
    )


class _ConstruccionesPorFuncion(ast.NodeVisitor):
    """Cuenta las construcciones de ``VfsJob`` por la función MÁS INTERNA que las contiene (``Clase.metodo``)."""

    def __init__(self, nombres: frozenset[str]) -> None:
        self._nombres = nombres
        self._pila: list[str] = []
        self.cuenta: dict[str, int] = {}

    def _entrar(self, nodo: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._pila.append(nodo.name)
        self.generic_visit(nodo)
        self._pila.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._entrar(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._entrar(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._entrar(node)

    def visit_Call(self, nodo: ast.Call) -> None:
        if _es_construccion_de_vfsjob(nodo, self._nombres):
            clave = ".".join(self._pila) or "<módulo>"
            self.cuenta[clave] = self.cuenta.get(clave, 0) + 1
        self.generic_visit(nodo)


def test_los_constructores_de_vfsjob_estan_enumerados() -> None:
    """Si ``VfsJob`` gana un constructor, el detector de abajo tiene que decidir si lo ve."""
    assert {"create", "from_dict", "_validated"} == _CONSTRUCTORES_DE_VFSJOB


@pytest.mark.parametrize(
    ("fuente", "esperadas"),
    [
        ("from m import VfsJob\nVfsJob.create(a=1)", 1),
        ("import m as vfs\nvfs.VfsJob.create(a=1)", 1),
        ("sky_claw.local.mo2.vfs_contracts.VfsJob.create(a=1)", 1),
        ("from m import VfsJob as VFS\nVFS.create(a=1)", 1),
        ("from m import VfsJob\nVfsJob(a=1)", 1),
        ("from m import VfsJob as VFS\nVFS(a=1)", 1),
        ("from m import VfsJob\nVfsJob.from_dict({})", 1),
        ("from m import VfsJob as VFS\nVfsJob.create(a=1)\nVFS.create(a=1)", 2),
        ("OtraCosa.create(a=1)", 0),
        ("from m import VfsJob\nVfsJob.otra(a=1)", 0),
        ("VfsJobExtendido.create(a=1)", 0),
        ("from m import Otra as VFS\nVFS.create(a=1)", 0),
    ],
    ids=[
        "import_directo",
        "alias_de_modulo",
        "ruta_completa",
        "alias_de_la_clase",
        "constructor_directo",
        "constructor_directo_con_alias",
        "from_dict",
        "estilos_mezclados",
        "otra_clase",
        "otro_metodo",
        "nombre_parecido",
        "alias_de_otra_clase",
    ],
)
def test_el_detector_de_construcciones_no_depende_del_estilo_de_import(fuente: str, esperadas: int) -> None:
    arbol = ast.parse(fuente)
    nombres = _nombres_locales_de_vfsjob(arbol)

    assert sum(_es_construccion_de_vfsjob(n, nombres) for n in ast.walk(arbol)) == esperadas


def test_el_unico_job_de_spawn_se_arma_fuera_de_las_ramas_y_con_expected_plugins() -> None:
    """Estructura, no muestreo: ``spawn`` arma UN job, incondicionalmente (fuera de toda rama) y con la evidencia.

    Una rama nueva que construyera su propio ``VfsJob`` (por ejemplo, un camino «rápido» para otro modo) rompe esto
    hasta que alguien decida si lleva ``expected_plugins``: es el hermano que el handoff dejaba sin red.
    """
    arbol = _arbol_del_modulo()
    nombres = _nombres_locales_de_vfsjob(arbol)
    spawn = _funcion_spawn(arbol)
    creaciones = [n for n in ast.walk(spawn) if _es_construccion_de_vfsjob(n, nombres)]
    assert len(creaciones) == 1, "cambió la familia de sitios que construyen el VfsJob de spawn: decidir su evidencia"

    # incondicional: la asignación vive en el cuerpo de la función, no dentro de un if/try/with
    asignaciones_de_primer_nivel = [
        n
        for n in spawn.body
        if isinstance(n, ast.Assign)
        and any(isinstance(c, ast.Call) for c in ast.walk(n.value))
        and creaciones[0] in list(ast.walk(n.value))
    ]
    assert len(asignaciones_de_primer_nivel) == 1, "el VfsJob de spawn quedó dentro de una rama"

    payload = next(k.value for k in creaciones[0].keywords if k.arg == "payload")
    assert isinstance(payload, ast.Dict)
    claves = {k.value for k in payload.keys if isinstance(k, ast.Constant)}
    assert claves == {"executable", "argv", "cwd", "expected_plugins"}


def test_toda_construccion_de_vfsjob_del_modulo_esta_enumerada() -> None:
    """El ancla de ``spawn`` mira UN método; esto cierra la familia: todo sitio del módulo que arma un ``VfsJob``.

    Un método nuevo que construya su propio job (otro camino hacia el worker) no pasa por el ancla de ``spawn`` y
    quedaría sin red: rompe esto hasta que alguien decida si necesita ``expected_plugins``. El segundo sitio de hoy es
    la sonda ``health`` del handoff de TexGen (``payload={}``, no lanza DynDOLOD): no es un spawn, y por eso no lleva
    la evidencia de plugins. Que esté enumerado —y no solo justificado en un párrafo— es lo que lo hace verificable.
    """
    arbol = _arbol_del_modulo()
    contador = _ConstruccionesPorFuncion(_nombres_locales_de_vfsjob(arbol))
    contador.visit(arbol)

    assert contador.cuenta == {
        "BrokeredDynDOLODSpawnStrategy.spawn": 1,
        "BrokeredDynDOLODSpawnStrategy._verificar_handoff_mo2": 1,
    }

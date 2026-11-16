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


async def _spawn_dyndolod(tmp_path: pathlib.Path, *, con_handoff: bool) -> VfsJob:
    esc = _escenario(tmp_path)
    _declarar_plugins_activos(esc)
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
    return bridge.jobs[0]


@pytest.mark.parametrize(
    "con_handoff", [False, True], ids=["dyndolod_solo_sin_handoff", "texgen_a_dyndolod_con_handoff"]
)
async def test_todo_spawn_brokered_de_dyndolod_lleva_el_conjunto_completo_de_plugins_activos(
    tmp_path: pathlib.Path, con_handoff: bool
) -> None:
    """Las dos rutas de ``spawn`` entregan al worker lo que tiene que demostrar visible bajo USVFS."""
    job = await _spawn_dyndolod(tmp_path, con_handoff=con_handoff)

    esperados = job.payload["expected_plugins"]
    assert isinstance(esperados, list)
    assert {nombre.lower() for nombre in PLUGINS_ACTIVOS} <= {nombre.lower() for nombre in esperados}


async def test_las_dos_rutas_piden_la_misma_evidencia_de_visibilidad(tmp_path: pathlib.Path) -> None:
    """Sin handoff no se pide MENOS que con handoff: el conjunto es el mismo (no depende del artifact gateado)."""
    sin = await _spawn_dyndolod(tmp_path / "sin", con_handoff=False)
    con = await _spawn_dyndolod(tmp_path / "con", con_handoff=True)

    assert sorted(sin.payload["expected_plugins"]) == sorted(con.payload["expected_plugins"])  # type: ignore[arg-type]


def _funcion_spawn() -> ast.AsyncFunctionDef:
    arbol = ast.parse(pathlib.Path(brokered.__file__).read_text(encoding="utf-8"))
    (clase,) = [n for n in arbol.body if isinstance(n, ast.ClassDef) and n.name == "BrokeredDynDOLODSpawnStrategy"]
    (spawn,) = [n for n in clase.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "spawn"]
    return spawn


def test_el_unico_job_de_spawn_se_arma_fuera_de_las_ramas_y_con_expected_plugins() -> None:
    """Estructura, no muestreo: ``spawn`` arma UN job, incondicionalmente (fuera de toda rama) y con la evidencia.

    Una rama nueva que construyera su propio ``VfsJob`` (por ejemplo, un camino «rápido» para otro modo) rompe esto
    hasta que alguien decida si lleva ``expected_plugins``: es el hermano que el handoff dejaba sin red.
    """
    spawn = _funcion_spawn()
    creaciones = [
        n
        for n in ast.walk(spawn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "VfsJob"
    ]
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

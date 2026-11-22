"""Los mods de salida que el move-aside recrea vacíos (R0 brokered 1, rig real 2026-11-22).

Propiedad del mecanismo: *ninguna mutación de esa carpeta puede sobrevivir a la
lease que la cubre*. Un ``asyncio.to_thread`` cancelado suelta al caller pero no
detiene el hilo: el servicio podría terminar su rollback y soltar las leases de
la etapa 9 mientras un ``rmdir``/``mkdir`` tardío todavía corre, y la corrida
siguiente vería desaparecer su carpeta vacía (review Codex P1, PR #993). Por eso
toda mutación pasa por ``mutar_hasta_terminal``, que retiene al caller hasta que
el hilo es terminal. El ancla AST enumera los sitios: uno nuevo que llame a las
mutaciones por fuera rompe el test.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import threading

import pytest

from sky_claw.local.tools._dir_rollback import mutar_hasta_terminal

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_MUTACIONES = frozenset({"descartar_si_es_carpeta_vacia", "_recrear_mod_de_salida_vacio"})


async def test_una_cancelacion_no_suelta_al_caller_antes_de_que_el_hilo_termine() -> None:
    liberar = threading.Event()
    hecho: list[str] = []

    def _mutacion_bloqueada() -> None:
        liberar.wait(timeout=10)
        hecho.append("mutó")

    caller = asyncio.ensure_future(mutar_hasta_terminal(_mutacion_bloqueada))
    await asyncio.sleep(0.05)
    caller.cancel()
    await asyncio.sleep(0.05)
    caller.cancel()  # una segunda cancelación tampoco lo libera
    await asyncio.sleep(0.05)
    assert not caller.done(), "el caller se liberó con el hilo todavía mutando"

    liberar.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert hecho == ["mutó"]


async def test_sin_cancelacion_devuelve_el_resultado_y_propaga_la_excepcion() -> None:
    assert await mutar_hasta_terminal(lambda x: x * 2, 21) == 42

    def _falla() -> None:
        raise OSError("disco")

    with pytest.raises(OSError, match="disco"):
        await mutar_hasta_terminal(_falla)


def _usos_de_las_mutaciones() -> dict[str, list[str]]:
    """``{módulo: [nombre de la llamada que recibe la mutación, …]}`` por AST."""
    usos: dict[str, list[str]] = {}
    for archivo in sorted((_RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        padres = {hijo: nodo for nodo in ast.walk(arbol) for hijo in ast.iter_child_nodes(nodo)}
        for nodo in ast.walk(arbol):
            if not (isinstance(nodo, ast.Name) and nodo.id in _MUTACIONES):
                continue
            padre = padres.get(nodo)
            llamada = ""
            if isinstance(padre, ast.Call):
                func = padre.func
                llamada = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if padre.func is nodo:
                    llamada = f"<llamada directa a {nodo.id}>"
            usos.setdefault(archivo.relative_to(_RAIZ).as_posix(), []).append(llamada)
    return usos


def test_toda_mutacion_de_la_carpeta_vacia_pasa_por_mutar_hasta_terminal() -> None:
    assert _usos_de_las_mutaciones() == {
        "sky_claw/local/tools/dyndolod_runner.py": ["mutar_hasta_terminal"],
        "sky_claw/local/tools/dyndolod_service.py": ["mutar_hasta_terminal"],
        "sky_claw/local/tools/rollback_reconciler.py": ["mutar_hasta_terminal"],
    }

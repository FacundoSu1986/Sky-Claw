"""Ids de trabajos en segundo plano del agente LLM (#920): no se repiten entre arranques.

Antes salían de ``itertools.count(1)`` por proceso, así que cada arranque volvía a emitir
``admision-1``. La propiedad es del mecanismo: NINGÚN módulo de ``app/agent/tools/`` genera ids con
un contador por proceso, y los dos gestores (admisión y LODs) usan el mismo helper.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools.job_ids import nuevo_id_de_trabajo
from sky_claw.app.agent.tools.lod_tools import GestorDeTrabajosDeLods
from sky_claw.app.agent.tools.xedit_bridge_tools import GestorDeAdmisionesNativas
from sky_claw.app.security.hitl import Decision
from sky_claw.local.tools.xedit_bridge_service import ContextoAdmision

RAIZ_TOOLS = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "app" / "agent" / "tools"
FORMATO = re.compile(r"^admision-\d{8}-\d{6}-[0-9a-f]{6}$")
FORMATO_LODS = re.compile(r"^lods-\d{8}-\d{6}-[0-9a-f]{6}$")


def test_formato_del_id_lleva_prefijo_sello_y_sufijo_aleatorio() -> None:
    assert FORMATO.match(nuevo_id_de_trabajo("admision"))


def test_ids_consecutivos_no_se_repiten() -> None:
    ids = {nuevo_id_de_trabajo("lods") for _ in range(20)}
    assert len(ids) == 20


async def test_dos_gestores_distintos_no_generan_el_mismo_id() -> None:
    """Dos arranques = dos gestores nuevos: antes los dos emitían ``admision-1``."""

    def _servicio() -> MagicMock:
        servicio = MagicMock()
        servicio.admitir = AsyncMock(return_value={"success": True, "message": "", "admission_id": "x"})
        servicio.contexto_de_admision = MagicMock(return_value=ContextoAdmision(pathlib.Path("C:/MO2"), "perfil"))
        return servicio

    hitl = MagicMock()
    hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)
    ids = []
    for _ in range(2):
        gestor = GestorDeAdmisionesNativas()
        ids.append(gestor.iniciar(_servicio(), hitl, "Mod.esp")["trabajo_id"])
        await asyncio.gather(*[t for t in (gestor.actual.tarea,) if t])
    assert ids[0] != ids[1] and all(FORMATO.match(i) for i in ids)


async def test_dos_gestores_de_lods_distintos_no_generan_el_mismo_id() -> None:
    """Hermano de la admisión (#920): ``lods-N`` salía del mismo contador por proceso."""
    ids = []
    for _ in range(2):
        gestor = GestorDeTrabajosDeLods(proveedor_de_servicio=lambda: object())
        # Se aísla el mecanismo del id: ni verificación de etapas ni corrida real.
        gestor._etapas_faltantes = AsyncMock(return_value=())  # type: ignore[method-assign]  # noqa: SLF001
        gestor._correr = AsyncMock(return_value=None)  # type: ignore[method-assign]  # noqa: SLF001
        respuesta = await gestor.iniciar(
            preset="Medium", run_texgen=False, create_snapshot=False, interaction_mode="asistido"
        )
        ids.append(respuesta["trabajo_id"])
        await asyncio.gather(*[t for t in (gestor.actual.tarea,) if t])  # type: ignore[union-attr]
    assert ids[0] != ids[1]
    assert all(FORMATO_LODS.match(i) for i in ids), ids


def _usa_contador_por_proceso(fuente: str) -> bool:
    """¿El módulo construye un ``itertools.count``, con cualquier estilo de import?

    Cubre ``itertools.count(...)``, ``import itertools as it`` + ``it.count(...)``,
    ``from itertools import count`` (con o sin alias) y ``from itertools import *``. Un detector que
    solo mira ``itertools.count`` se evade cambiando la forma del import.
    """
    arbol = ast.parse(fuente)
    alias_del_modulo: set[str] = set()
    alias_de_count: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            alias_del_modulo.update(a.asname or a.name for a in nodo.names if a.name == "itertools")
        elif isinstance(nodo, ast.ImportFrom) and nodo.module == "itertools":
            for a in nodo.names:
                if a.name == "*":
                    alias_de_count.add("count")
                elif a.name == "count":
                    alias_de_count.add(a.asname or "count")
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            f = nodo.func
            if isinstance(f, ast.Attribute) and f.attr == "count" and getattr(f.value, "id", "") in alias_del_modulo:
                return True
            if isinstance(f, ast.Name) and f.id in alias_de_count:
                return True
    return False


@pytest.mark.parametrize(
    ("fuente", "esperado"),
    [
        ("import itertools\n_IDS = itertools.count(1)", True),
        ("import itertools as it\n_IDS = it.count(1)", True),
        ("from itertools import count\n_IDS = count(1)", True),
        ("from itertools import count as contador\n_IDS = contador(1)", True),
        ("from itertools import *\n_IDS = count(1)", True),
        ("import itertools\nlista = list(itertools.chain([1], [2]))", False),
        ("from itertools import chain\nlista = list(chain([1], [2]))", False),
        ("def count(x):\n    return x\nvalor = count(1)", False),
    ],
)
def test_el_detector_de_contadores_no_se_evade_cambiando_el_estilo_de_import(fuente: str, esperado: bool) -> None:
    assert _usa_contador_por_proceso(fuente) is esperado


def test_ningun_modulo_de_tools_usa_contadores_por_proceso_para_ids() -> None:
    """Ancla que ENUMERA la familia: un ``itertools.count`` (en cualquier forma de import) rompe el test."""
    infractores = [
        ruta.name
        for ruta in sorted(RAIZ_TOOLS.glob("*.py"))
        if _usa_contador_por_proceso(ruta.read_text(encoding="utf-8"))
    ]
    assert infractores == []


def test_los_dos_gestores_usan_el_helper_compartido() -> None:
    for nombre in ("xedit_bridge_tools.py", "lod_tools.py"):
        fuente = (RAIZ_TOOLS / nombre).read_text(encoding="utf-8")
        assert "nuevo_id_de_trabajo(" in fuente, nombre

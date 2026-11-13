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

from sky_claw.app.agent.tools.job_ids import nuevo_id_de_trabajo
from sky_claw.app.agent.tools.xedit_bridge_tools import GestorDeAdmisionesNativas
from sky_claw.app.security.hitl import Decision
from sky_claw.local.tools.xedit_bridge_service import ContextoAdmision

RAIZ_TOOLS = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "app" / "agent" / "tools"
FORMATO = re.compile(r"^admision-\d{8}-\d{6}-[0-9a-f]{6}$")


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


def test_ningun_modulo_de_tools_usa_contadores_por_proceso_para_ids() -> None:
    """Ancla que ENUMERA la familia: ``itertools.count`` en cualquier módulo de tools rompe el test."""
    infractores = []
    for ruta in sorted(RAIZ_TOOLS.glob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Attribute)
                and nodo.attr == "count"
                and getattr(nodo.value, "id", "") == "itertools"
            ):
                infractores.append(ruta.name)
    assert infractores == []


def test_los_dos_gestores_usan_el_helper_compartido() -> None:
    for nombre in ("xedit_bridge_tools.py", "lod_tools.py"):
        fuente = (RAIZ_TOOLS / nombre).read_text(encoding="utf-8")
        assert "nuevo_id_de_trabajo(" in fuente, nombre

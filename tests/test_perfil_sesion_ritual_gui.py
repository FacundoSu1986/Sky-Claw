"""Hermano que falta en el PR #460: la superficie GUI del sort de LOOT.

El PR cierra el perfil de sesión en la superficie del agente LLM
(``AsyncToolRegistry`` → los 8 tools de ``system_tools``) y en el constructor del
``SupervisorAgent``. La otra superficie de AGENTS.md —GUI: Ritual →
``SupervisorAgent.dispatch_tool`` → ``tool_dispatcher`` → estrategia— queda afuera
para el ÚNICO recurso que el propio PR nombra como compartido: el runner de LOOT
que muta ``plugins.txt``/``loadorder.txt``.

Camino real, sin mocks del código bajo prueba:

    ritual_runner.dispatch_ritual   -> supervisor.dispatch_tool("execute_loot_sorting", {})
    ExecuteLootSortingStrategy      -> LootExecutionParams(**{})
    app/core/models.py:51           -> profile_name = "Default"   (default de pydantic)
    LootSortingService              -> _ensure_loot_runner("Default")
    BrokeredLootRunner.for_profile  -> runner NUEVO atado a "Default"

El `profile=active_profile` que el PR puso en ``build_vfs_loot_runner``
(``app_context.py``) no lo salva: ``for_profile`` devuelve ``self`` solo cuando el
perfil COINCIDE, así que el rebind a ``"Default"`` descarta el runner de sesión y
construye otro (``brokered_loot.py:74-84``).

El desplazamiento es **coherente**, no parcial: los targets del snapshot salen del
runner ya rebindeado (``_resolve_load_order_for_runner`` → ``mutation_targets``), así
que el rollback cubre los mismos archivos que LOOT reescribe. No queda un archivo
mutado sin red; lo que queda es que la GUI opera entera sobre el perfil equivocado
mientras el agente LLM opera sobre el de la sesión, y que el modal HITL le nombra al
operador el perfil que no es.

Los dos hermanos de la misma carpeta ya tienen el mecanismo correcto:
``ValidatePluginLimitStrategy`` (``default_profile_getter=lambda: supervisor.profile_name``)
y ``GenerateBashedPatchStrategy`` (``profile or self._path_resolver.get_active_profile()``).
``ExecuteLootSortingStrategy`` es el único que cae a un literal.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pydantic
import pytest

from sky_claw.app.gui.controllers.ritual_runner import RITUAL_TOOL_MAP
from sky_claw.app.orchestrator.tool_strategies.execute_loot_sorting import ExecuteLootSortingStrategy
from sky_claw.local.tools.loot_service import LootSortingService

PERFIL_DE_SESION = "Requiem"

#: El payload que manda la GUI, literal: ``dispatch_tool(tool_name, {})``
#: (``sky_claw/app/gui/controllers/ritual_runner.py``). No hay capa intermedia que
#: lo enriquezca: ``dispatch_tool`` delega verbatim y ningún middleware toca el dict.
PAYLOAD_DEL_RITUAL: dict = {}


MO2_ROOT = pathlib.Path.home() / "MO2Portable"


class _RunnerPorPerfil:
    """Doble de ``BrokeredLootRunner``: registra a qué perfil lo rebindean.

    Declara ``mutation_targets`` **derivado de su propio perfil**, como el real
    (``brokered_loot.py:89``). No es decorativo: ``_resolve_load_order_for_runner``
    saca de ahí los targets del snapshot cuando el runner los expone, y solo cae al
    ``_ensure_load_order_resolver()`` (perfil del ``PathResolver``) para runners
    legacy que no los declaran. Un doble sin este método manda al test por la rama
    equivocada y le hace afirmar una divergencia que en producción no existe.
    """

    def __init__(self, perfil: str, registro: list[str]) -> None:
        self._perfil = perfil
        self._registro = registro

    def for_profile(self, profile: str) -> _RunnerPorPerfil:
        self._registro.append(profile)
        return self if profile == self._perfil else _RunnerPorPerfil(profile, self._registro)

    def mutation_targets(self) -> tuple[pathlib.Path, ...]:
        return (MO2_ROOT / "profiles" / self._perfil / "plugins.txt",)

    async def prepare_attestation(self) -> None:
        return None


class _ResolverDeSesion:
    """``PathResolutionService`` con el perfil de sesión ya resuelto por el PR."""

    def get_active_profile(self) -> str:
        return PERFIL_DE_SESION

    def get_mo2_path(self) -> pathlib.Path:
        return MO2_ROOT


def _servicio_y_estrategia() -> tuple[LootSortingService, ExecuteLootSortingStrategy, list[str]]:
    registro: list[str] = []
    service = LootSortingService(
        lock_manager=object(),
        snapshot_manager=object(),
        path_resolver=_ResolverDeSesion(),
        # Igual que producción: `app_context` construye el runner brokered con el
        # perfil ACTIVO — que es justamente lo que este PR arregló ahí.
        loot_runner=_RunnerPorPerfil(PERFIL_DE_SESION, registro),
    )
    strategy = ExecuteLootSortingStrategy(
        service=service,
        default_profile_getter=lambda: PERFIL_DE_SESION,
    )
    return service, strategy, registro


async def test_el_ritual_de_loot_ordena_el_perfil_de_sesion() -> None:
    """El Ritual de LOOT de la GUI muta el load order del perfil de sesión.

    Se afirma sobre a qué perfil se rebindeó el runner —lo que LOOT va a ordenar de
    verdad—, no sobre el texto del wiring.
    """
    _, strategy, registro = _servicio_y_estrategia()

    await strategy.prepare_for_approval(PAYLOAD_DEL_RITUAL)

    assert registro == [PERFIL_DE_SESION], f"el Ritual de LOOT ordenó otro perfil: {registro}"


def test_el_modal_hitl_le_nombra_al_operador_el_perfil_de_sesion() -> None:
    """Lo que el operador aprueba tiene que ser lo que se va a ejecutar.

    ``describe_for_approval`` es el detalle del modal HITL (``middleware.py`` lo pasa
    a ``request_approval``): si dice ``Default`` mientras la sesión es otra, la
    aprobación se dio sobre una operación que no es la que corre.
    """
    _, strategy, _ = _servicio_y_estrategia()

    detalle = strategy.describe_for_approval(PAYLOAD_DEL_RITUAL)

    assert PERFIL_DE_SESION in detalle, f"el modal HITL nombra otro perfil: {detalle!r}"


async def test_el_snapshot_del_rollback_cubre_los_archivos_del_perfil_de_sesion() -> None:
    """Lo que el rollback protege son los archivos del perfil de sesión.

    No hay divergencia entre lo que se ordena y lo que se snapshotea:
    ``_resolve_load_order_for_runner`` saca los targets del runner **ya rebindeado**
    (``mutation_targets``), así que el snapshot sigue al sort por construcción. Lo que
    sí se movía en bloque era el perfil: con el payload vacío del Ritual, LOOT
    ordenaba ``Default`` y el rollback cubría los archivos de ``Default``, mientras la
    sesión era otra. Este test fija el par completo sobre el perfil de sesión.
    """
    service, strategy, _ = _servicio_y_estrategia()

    params = strategy._parse_params(PAYLOAD_DEL_RITUAL)
    runner = service._ensure_loot_runner(params.profile_name)
    targets = await service._resolve_load_order_for_runner(runner)

    assert [ruta.parent.name for ruta in targets.files] == [PERFIL_DE_SESION], (
        f"el rollback cubre archivos de otro perfil: {[str(r) for r in targets.files]}"
    )


# ---------------------------------------------------------------------------
# Ancla enumerativa sobre RITUAL_TOOL_MAP
# ---------------------------------------------------------------------------
#
# Los tres tests de arriba son de comportamiento y cubren el CASO (LOOT). Este
# cubre la CLASE: se enumeran los cinco Rituales de la GUI y se mira qué cruza la
# frontera del dispatch con el payload vacío que manda `dispatch_ritual`.
#
# `RITUAL_TOOL_MAP` ya está congelado por igualdad literal en
# `tests/test_ritual_dispatch.py`, así que un ritual nuevo entra acá solo y rompe
# la igualdad de :data:`RITUALES_QUE_LLEVAN_PERFIL` hasta que alguien decida de
# dónde saca su perfil — y esa decisión lo mete en el assert de comportamiento.
# Mismo instrumento que `tests/test_hitl_client_scoping.py`.

_CAMPOS_DE_PERFIL = {"profile", "profile_name", "source_profile"}

#: Qué Rituales cruzan la frontera del dispatch LLEVANDO un perfil.
#:
#: `False` no es "no le importa el perfil": es que lo resuelven aguas abajo desde la
#: sesión (`supervisor.profile_name` o `PathResolutionService.get_active_profile()`),
#: así que no hay ningún valor que el payload pueda contradecir. `True` significa que
#: el perfil viaja EN la llamada, y entonces tiene que ser el de la sesión — que es
#: exactamente donde `execute_loot_sorting` se caía al default de pydantic.
RITUALES_QUE_LLEVAN_PERFIL: dict[str, bool] = {
    "dyndolod": False,
    "loot": True,
    "pandora": False,
    "wrye_bash": False,
    "xedit": False,
}


def _perfiles_en(args: tuple, kwargs: dict) -> list[str]:
    """Todo valor de perfil que cruzó en una llamada: kwargs con nombre de perfil y
    campos de perfil de cualquier modelo pydantic posicional."""
    hallados: list[str] = []
    for nombre, valor in kwargs.items():
        if nombre in _CAMPOS_DE_PERFIL and isinstance(valor, str):
            hallados.append(valor)
    for valor in (*args, *kwargs.values()):
        if isinstance(valor, pydantic.BaseModel):
            for campo in type(valor).model_fields:
                if campo in _CAMPOS_DE_PERFIL:
                    hallados.append(getattr(valor, campo))
    return hallados


def _supervisor_de_sesion() -> Any:
    """``SupervisorAgent`` construction-free con el perfil de sesión ya resuelto.

    Mismo patrón que ``tests/test_idempotency_wiring.py``: se saltea ``__init__`` y se
    cablean los colaboradores como dobles, para que el dispatcher REAL
    (``build_orchestration_dispatcher``) sea lo único bajo prueba.
    """
    from unittest.mock import AsyncMock, MagicMock

    from sky_claw.app.orchestrator.supervisor import SupervisorAgent

    sup = SupervisorAgent.__new__(SupervisorAgent)
    for atributo in (
        "scraper",
        "tools",
        "interface",
        "_loot_service",
        "_synthesis_service",
        "_xedit_service",
        "_dyndolod_service",
        "_pandora_service",
        "_grass_cache_service",
    ):
        setattr(sup, atributo, MagicMock())
    # Los servicios se invocan con await: AsyncMock en los métodos que el dispatch toca.
    sup._loot_service.sort_load_order = AsyncMock(return_value={"success": True})
    sup._loot_service.prepare_vfs_attestation = MagicMock(return_value=None)
    sup._xedit_service.quick_auto_clean = AsyncMock(return_value={"success": True})
    # DynDOLOD expone `execute`, no un metodo homonimo del ritual.
    sup._dyndolod_service.execute = AsyncMock(return_value={"success": True})
    sup._pandora_service.generate_animations = AsyncMock(return_value={"success": True})
    sup.execute_wrye_bash_pipeline = AsyncMock(return_value={"success": True})
    sup.journal = AsyncMock()
    sup.profile_name = PERFIL_DE_SESION
    return sup


async def _perfiles_que_cruzan(tool: str) -> list[str]:
    """Despacha ``tool`` con el payload vacío del Ritual y devuelve todo perfil que
    haya cruzado la frontera del dispatch."""
    from sky_claw.app.orchestrator.tool_dispatcher import build_orchestration_dispatcher
    from sky_claw.app.orchestrator.tool_strategies.middleware import HitlGateMiddleware

    sup = _supervisor_de_sesion()
    dispatcher = build_orchestration_dispatcher(sup, hitl_gate=HitlGateMiddleware(allow_unattended=True))
    await dispatcher.dispatch(tool, dict(PAYLOAD_DEL_RITUAL))

    hallados: list[str] = []
    for atributo in (
        "_loot_service",
        "_xedit_service",
        "_dyndolod_service",
        "_pandora_service",
        "_grass_cache_service",
        "execute_wrye_bash_pipeline",
    ):
        objetivo = getattr(sup, atributo)
        candidatos = [objetivo, *getattr(objetivo, "_mock_children", {}).values()]
        for candidato in candidatos:
            llamada = getattr(candidato, "call_args", None)
            if llamada is not None:
                hallados.extend(_perfiles_en(llamada.args, llamada.kwargs))
    return hallados


def test_la_clasificacion_de_los_rituales_esta_congelada() -> None:
    """Igualdad literal contra ``RITUAL_TOOL_MAP``: un Ritual nuevo entra solo y rompe
    acá hasta que se decida de dónde saca su perfil."""
    assert set(RITUALES_QUE_LLEVAN_PERFIL) == set(RITUAL_TOOL_MAP)


@pytest.mark.parametrize("ritual", sorted(RITUAL_TOOL_MAP))
async def test_ningun_ritual_cruza_el_dispatch_con_un_perfil_ajeno(ritual: str) -> None:
    """Con el payload vacío de la GUI, ningún Ritual puede llevar un perfil que no sea
    el de la sesión.

    Los que no llevan ninguno (``False`` en la clasificación) lo resuelven aguas abajo
    desde la sesión, así que no hay valor que el payload pueda contradecir; se afirma
    esa ausencia para que empezar a llevarlo sea una decisión explícita y no un
    accidente. ``loot`` es el que lo lleva, y es donde el default de pydantic lo ponía
    en ``"Default"``.
    """
    perfiles = await _perfiles_que_cruzan(RITUAL_TOOL_MAP[ritual])

    assert bool(perfiles) == RITUALES_QUE_LLEVAN_PERFIL[ritual], (
        f"cambió si «{ritual}» lleva perfil en el dispatch: {perfiles}"
    )
    ajenos = [p for p in perfiles if p != PERFIL_DE_SESION]
    assert not ajenos, f"«{ritual}» cruzó el dispatch con un perfil ajeno a la sesión: {ajenos}"

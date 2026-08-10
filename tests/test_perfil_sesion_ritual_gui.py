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

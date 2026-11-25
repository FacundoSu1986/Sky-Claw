"""PR-2b3 (ADR 0019 §3.2): cableado productivo de Synthesis brokered, con el HOLD intacto.

Tres propiedades del mecanismo, cada una anclada acá:

1. *El clon del overwrite y la raíz administrada se excluyen, y los decide UNA función.* En brokered la
   salida va a ``<mods>/Sky-Claw Synthesis Output`` con move-aside (PR-2b2): clonar el overwrite no
   protege nada y la HITL post-run sobre el diff del clon no ve esa raíz. La strategy y el servicio
   preguntan lo mismo a ``dominio_de_visibilidad_del_backend``; si discreparan, un backend correría
   sin ninguna de las dos protecciones.
2. *El backend sólo protege si TODOS los constructores de producción lo reciben.* La composición
   construye el servicio en dos sitios (el base y la factory por corrida); el censo AST de abajo los
   enumera y exige los cuatro kwargs del backend en cada uno.
3. *El HOLD sigue siendo la única llave.* Con ``synthesis_backend="brokered"`` cableado de punta a
   punta, la corrida falla cerrada antes de tocar disco: no hay clon, no hay fallback a standalone.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.orchestrator.tool_strategies.execute_synthesis import ExecuteSynthesisPipelineStrategy
from sky_claw.local.mo2.brokered_synthesis import dominio_de_visibilidad_del_backend
from sky_claw.local.tools.output_targets import SYNTHESIS_MANAGED_MOD_NAME
from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

_RAIZ = pathlib.Path(__file__).resolve().parents[1]

_BACKENDS = ["standalone", " Standalone ", "brokered", "BROKERED", "mo2_broker", "", "otro"]


def _servicio(backend: str, output_path: pathlib.Path | None = None, journal: Any = None) -> SynthesisPipelineService:
    return SynthesisPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=journal if journal is not None else MagicMock(),
        path_resolver=MagicMock(),
        event_bus=MagicMock(),
        output_path=output_path,
        synthesis_backend=backend,
    )


def _strategy(backend: str, **overrides: Any) -> ExecuteSynthesisPipelineStrategy:
    kwargs: dict[str, Any] = {
        "flow_provider": MagicMock(side_effect=AssertionError("brokered no clona el overwrite")),
        "service_factory": MagicMock(),
        "real_journal_provider": MagicMock(),
        "synthesis_backend": backend,
        "raices_mo2_provider": MagicMock(),
        "profile_getter": lambda: "Default",
    }
    kwargs.update(overrides)
    return ExecuteSynthesisPipelineStrategy(**kwargs)


# ---------------------------------------------------------------------------
# 1. Una sola decisión: clon del overwrite XOR raíz administrada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", _BACKENDS)
def test_strategy_y_servicio_deciden_con_la_misma_funcion(backend: str) -> None:
    # Arrange / Act
    sin_clon = _strategy(backend).corre_sin_clon
    servicio = _servicio(backend)

    # Assert: la strategy salta el clon exactamente cuando el servicio promueve la raíz administrada.
    assert sin_clon is servicio._salida_administrada
    assert servicio._data_visibility_domain == dominio_de_visibilidad_del_backend(backend)


def test_el_default_de_la_strategy_es_standalone_con_clon() -> None:
    strategy = ExecuteSynthesisPipelineStrategy(
        flow_provider=MagicMock(), service_factory=MagicMock(), real_journal_provider=MagicMock()
    )

    assert strategy.corre_sin_clon is False


def _perfil(tmp_path: pathlib.Path) -> pathlib.Path:
    perfil = tmp_path / "MO2" / "profiles" / "Default"
    perfil.mkdir(parents=True)
    (perfil / "modlist.txt").write_text("+Otro Mod\n", encoding="utf-8")
    (perfil / "plugins.txt").write_text("*Skyrim.esm\n*DynDOLOD.esp\n", encoding="utf-8")
    return perfil


async def test_brokered_corre_el_servicio_sin_clon_contra_el_journal_real(tmp_path: pathlib.Path) -> None:
    # Arrange
    perfil = _perfil(tmp_path)
    antes = {n: (perfil / n).read_bytes() for n in ("modlist.txt", "plugins.txt")}
    real_journal = MagicMock()
    real_journal.begin_transaction = AsyncMock(return_value=41)
    salida = tmp_path / "MO2" / "mods" / SYNTHESIS_MANAGED_MOD_NAME / "Synthesis.esp"
    vistos: list[tuple[Any, Any]] = []

    def factory(output_path: pathlib.Path | None, journal: Any) -> Any:
        vistos.append((output_path, journal))
        service = MagicMock()

        async def execute_pipeline(**kwargs: Any) -> dict[str, Any]:
            assert kwargs == {"patcher_ids": ["a"]}
            await journal.begin_transaction(description="synthesis_pipeline", agent_id="synthesis-service")
            return {"success": True, "message": "", "output_esps": [str(salida)]}

        service.execute_pipeline = execute_pipeline
        return service

    strategy = _strategy(
        "brokered",
        service_factory=factory,
        real_journal_provider=lambda: real_journal,
        raices_mo2_provider=lambda: SimpleNamespace(data_root=tmp_path / "MO2"),
    )
    emitidos: list[tuple[int | None, Any]] = []

    async def emitir(journal: Any, *, tx_id: int | None, clone: Any) -> None:
        emitidos.append((tx_id, clone))

    strategy._emit_flight_report = emitir  # type: ignore[method-assign]

    # Act
    result = await strategy.execute({"patcher_ids": ["a"], "tool_name": "x"})

    # Assert
    assert result["success"] is True
    assert vistos and vistos[0][0] is None, "sin output_path: el servicio promueve la raíz administrada"
    real_journal.begin_transaction.assert_awaited_once()
    assert emitidos == [(41, None)], "la caja negra se cierra con la TX real y sin traducción de clon"
    activacion = result["activacion"]
    assert activacion["aplicado"] is False
    assert activacion["estado_del_mod"] == "ausente"
    assert activacion["plugins_ausentes"] == ["Synthesis.esp"]
    assert {n: (perfil / n).read_bytes() for n in antes} == antes


async def test_brokered_sin_perfil_legible_informa_el_diff_como_no_disponible(tmp_path: pathlib.Path) -> None:
    service = MagicMock()
    service.execute_pipeline = AsyncMock(return_value={"success": True, "message": "", "output_esps": ["X.esp"]})
    strategy = _strategy(
        "brokered",
        service_factory=lambda _o, _j: service,
        raices_mo2_provider=lambda: SimpleNamespace(data_root=tmp_path / "no-existe"),
    )
    strategy._emit_flight_report = AsyncMock()  # type: ignore[method-assign]

    result = await strategy.execute({})

    assert result["success"] is True, "el diff es informativo: nunca convierte un éxito en fallo"
    assert result["activacion"]["disponible"] is False
    assert "modlist.txt" in result["activacion"]["motivo"]


@pytest.mark.parametrize("perfil", ["..", "a/b", ""])
async def test_brokered_con_perfil_inseguro_no_lee_fuera_del_arbol(perfil: str) -> None:
    service = MagicMock()
    service.execute_pipeline = AsyncMock(return_value={"success": True, "message": "", "output_esps": ["X.esp"]})
    raices = MagicMock()
    strategy = _strategy(
        "brokered", service_factory=lambda _o, _j: service, raices_mo2_provider=raices, profile_getter=lambda: perfil
    )
    strategy._emit_flight_report = AsyncMock()  # type: ignore[method-assign]

    result = await strategy.execute({})

    assert result["activacion"]["disponible"] is False
    raices.assert_not_called()


async def test_brokered_fallido_no_adjunta_diff() -> None:
    service = MagicMock()
    service.execute_pipeline = AsyncMock(return_value={"success": False, "message": "boom"})
    raices = MagicMock()
    strategy = _strategy("brokered", service_factory=lambda _o, _j: service, raices_mo2_provider=raices)
    strategy._emit_flight_report = AsyncMock()  # type: ignore[method-assign]

    result = await strategy.execute({})

    assert "activacion" not in result
    raices.assert_not_called()


async def test_brokered_con_el_hold_real_falla_cerrado_sin_clon(tmp_path: pathlib.Path) -> None:
    """Punta a punta con el servicio REAL: el HOLD corta antes del lock y sin fallback.

    El literal del HOLD solo lo nombra ``test_brokered_synthesis.py`` (barrera enumerada por AST); acá se
    verifica su efecto observable.
    """
    journal = MagicMock()
    journal.begin_transaction = AsyncMock(side_effect=AssertionError("el HOLD corta antes del journal"))
    strategy = _strategy(
        "brokered",
        service_factory=lambda output_path, j: _servicio("brokered", output_path, j),
        real_journal_provider=lambda: journal,
    )
    strategy._emit_flight_report = AsyncMock()  # type: ignore[method-assign]

    result = await strategy.execute({"patcher_ids": ["a"]})

    assert result["success"] is False
    assert "WIRING_SECURITY_HOLD" in result["message"]
    assert "activacion" not in result


# ---------------------------------------------------------------------------
# 2. La composición entrega el MISMO backend a los dos constructores
# ---------------------------------------------------------------------------


def _composicion(**synthesis: Any) -> Any:
    from sky_claw.app.orchestrator.orchestration_composition import build_orchestration_composition

    return build_orchestration_composition(
        scraper=MagicMock(),
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=MagicMock(),
        path_validator=MagicMock(),
        event_bus=MagicMock(),
        profile_name="PerfilSesion",
        hitl_guard=MagicMock(),
        loot_runner=None,
        require_vfs=False,
        patch_advisor_llm=None,
        grass_runtime_deps_provider=MagicMock(),
        plugin_limit_guard=MagicMock(),
        scan_asset_conflicts=MagicMock(),
        scan_asset_conflicts_json=MagicMock(),
        **synthesis,
    )


def test_composicion_por_defecto_sigue_en_standalone_con_clon() -> None:
    composition = _composicion()

    strategy = composition.tool_dispatcher._strategies["execute_synthesis_pipeline"]
    assert composition.synthesis_service._synthesis_backend == "standalone"
    assert strategy.corre_sin_clon is False


def test_composicion_brokered_llega_igual_al_servicio_base_a_la_factory_y_a_la_strategy() -> None:
    # Arrange
    broker = MagicMock()
    preparador = MagicMock()

    # Act
    composition = _composicion(
        synthesis_backend="brokered",
        synthesis_vfs_broker=broker,
        synthesis_instance_id="instancia-1",
        synthesis_preparador_fase1=preparador,
    )
    por_corrida = composition.dispatcher_dependencies.synthesis_service_factory(None, MagicMock())
    strategy = composition.tool_dispatcher._strategies["execute_synthesis_pipeline"]

    # Assert
    for servicio in (composition.synthesis_service, por_corrida):
        assert servicio._synthesis_backend == "brokered"
        assert servicio._vfs_broker is broker
        assert servicio._instance_id == "instancia-1"
        assert servicio._preparador_fase1 is preparador
        assert servicio._salida_administrada is True
    assert por_corrida._output_path is None
    assert strategy.corre_sin_clon is True
    assert composition.dispatcher_dependencies.synthesis_backend == "brokered"


# ---------------------------------------------------------------------------
# 3. Censo AST de los constructores de producción del servicio
# ---------------------------------------------------------------------------

#: Todos los sitios de ``sky_claw/`` que construyen ``SynthesisPipelineService``. Igualdad literal: un
#: constructor nuevo rompe el ancla hasta que se decide si recibe el backend.
_CONSTRUCTORES_DE_PRODUCCION = {
    "sky_claw/app/orchestrator/dispatcher_dependencies.py": 1,
    "sky_claw/app/orchestrator/orchestration_composition.py": 1,
}
_KWARGS_DEL_BACKEND = frozenset({"synthesis_backend", "vfs_broker", "instance_id", "preparador_fase1"})


def _llamadas_al_constructor() -> dict[str, list[ast.Call]]:
    encontradas: dict[str, list[ast.Call]] = {}
    for archivo in sorted((_RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                nombre = nodo.func.id if isinstance(nodo.func, ast.Name) else getattr(nodo.func, "attr", None)
                if nombre == "SynthesisPipelineService":
                    encontradas.setdefault(archivo.relative_to(_RAIZ).as_posix(), []).append(nodo)
    return encontradas


def test_censo_de_constructores_de_produccion() -> None:
    llamadas = _llamadas_al_constructor()

    assert {ruta: len(nodos) for ruta, nodos in llamadas.items()} == _CONSTRUCTORES_DE_PRODUCCION


def test_todo_constructor_de_produccion_recibe_el_backend_completo() -> None:
    faltantes = {
        ruta: sorted(_KWARGS_DEL_BACKEND - {kw.arg for kw in nodo.keywords if kw.arg is not None})
        for ruta, nodos in _llamadas_al_constructor().items()
        for nodo in nodos
    }

    assert all(not f for f in faltantes.values()), faltantes

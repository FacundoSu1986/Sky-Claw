"""``system_tools.generate_bashed_patch`` — la superficie del agente LLM (ADR 0016).

Antes del PR B este handler era el SEGUNDO path de producción del Bashed Patch: delegaba
en ``WryeBashPipelineService.execute_pipeline``, tomaba el lock anidado
(``Bashed Patch, 0.esp`` + ``load-order``), emitía ``ActionManifest``/``FlightReport`` y
abría una TX… para que el runner fallara cerrado siempre (``bash.py`` no expone build).
Ese camino dejó de existir en esta superficie: la etapa 6 es **asistida** y la construye
el operador en la GUI de Wrye Bash lanzado desde MO2.

Lo que este módulo ancla ahora es el contrato NUEVO de la delegación, que es el inverso
del anterior y por eso tiene dientes propios:

1. **Una sola salida**: el handler delega en ``ejecutar_fase_asistida`` (el punto ÚNICO
   de enrutado, compartido con la GUI y el supervisor). Nunca en ``execute_pipeline``.
2. **No toma lock, no snapshotea y no emite caja negra**: ninguna de las dos fases muta,
   así que un lock ajeno —de Wrye Bash o de un sort de LOOT en curso— NO la bloquea, y
   una invocación no deja una TX ``rolled_back`` por ritual. La caja negra existe para lo
   que puede mutar (ADR 0016 §2).
3. **No hay subproceso**: el runner inyectado no se invoca y su ausencia deja de ser un
   error estructural (``runner=None`` es el caso normal).
4. **El contrato JSON de la tool se preserva** (T11): dict serializable, nunca excepción.

La lógica del lock, del rollback y del manifiesto **no se perdió**: sigue viva en
``test_wrye_bash_service.py``, que es donde vive el código que la implementa
(``execute_pipeline``, el camino headless que el PR C de ADR 0016 §7 retira o declara
compat documentada). Duplicarla acá contra un handler que ya no la ejecuta era el falso
verde que este módulo tenía tras el cableado: ``test_handler_bloqueado_si_loot_tiene_el_
load_order`` seguía pasando por un motivo distinto del que su nombre afirma.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.agent.tools.system_tools import generate_bashed_patch
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID
from sky_claw.local.tools.wrye_bash_asistida import BASHED_PATCH_NAME
from sky_claw.local.tools.wrye_bash_runner import WryeBashResult
from sky_claw.local.tools.wrye_bash_service import (
    BASHED_PATCH_RESOURCE_ID,
    WryeBashPipelineService,
)
from sky_claw.local.xedit.patch_orchestrator import DelegateToBashedPatch

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(
        tmp_path / "test_locks.db",
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr  # type: ignore[misc]
    await mgr.close()


@pytest.fixture
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    d = tmp_path / "snapshots"
    d.mkdir()
    mgr = FileSnapshotManager(snapshot_dir=d)
    await mgr.initialize()
    return mgr


def _resultado(success: bool = True, stdout: str = "ok") -> WryeBashResult:
    return WryeBashResult(
        success=success,
        return_code=0 if success else 1,
        stdout=stdout if success else "",
        stderr="" if success else "boom",
        duration_seconds=1.0,
    )


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.generate_bashed_patch = AsyncMock(return_value=_resultado())
    return runner


def _resolver(tmp_path: pathlib.Path, *, perfil: str = "Requiem") -> MagicMock:
    """Resolver con ``plugins.txt`` y Data del juego reales bajo ``tmp_path``."""
    plugins = tmp_path / "profiles" / perfil / "plugins.txt"
    plugins.parent.mkdir(parents=True, exist_ok=True)
    plugins.write_text(f"*{BASHED_PATCH_NAME}\n", encoding="utf-8")
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True, exist_ok=True)
    resolver = MagicMock()
    resolver.get_skyrim_path.return_value = game
    resolver.get_mo2_instance_data_root.return_value = tmp_path
    resolver.get_mo2_mods_path_best_effort.return_value = tmp_path / "mods"
    return resolver


@pytest.fixture()
def sin_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los sensores de preflight tienen su propia suite; acá se mide la delegación."""
    monkeypatch.setattr(WryeBashPipelineService, "_ensure_preflight", lambda self, **kwargs: None)


async def _handler(runner: Any = None, **kwargs: Any) -> dict[str, Any]:
    return json.loads(await generate_bashed_patch(runner, **kwargs))


# ==============================================================================
# 1. Una sola salida: el enrutado único del servicio
# ==============================================================================


async def test_el_handler_delega_en_el_enrutado_unico(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
    sin_preflight: None,
) -> None:
    """La superficie del agente comparte el punto de decisión con GUI y supervisor.

    Se espía ``ejecutar_fase_asistida`` y se afirma que ``execute_pipeline`` —el camino
    headless— no se toca: si alguien volviera a llamarlo, la tool reabriría el ritual
    muerto que el PR B cierra y este test lo vería en la primera llamada.
    """
    espia = AsyncMock(return_value={"success": False, "pendiente": True})
    with (
        patch.object(WryeBashPipelineService, "ejecutar_fase_asistida", espia),
        patch.object(WryeBashPipelineService, "execute_pipeline", AsyncMock()) as headless,
    ):
        out = await _handler(
            _runner(),
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            path_resolver=_resolver(tmp_path),
            profile="Requiem",
        )

    assert out == {"success": False, "pendiente": True}
    espia.assert_awaited_once()
    argumentos = espia.await_args.kwargs
    assert argumentos["profile"] == "Requiem"
    assert argumentos["fase"] == "preparar"
    # M-04 se valida a nivel Supervisor para este path, como antes del PR B.
    assert argumentos["validate_limit"] is False
    headless.assert_not_awaited()


async def test_el_handler_no_invoca_el_runner(tmp_path: pathlib.Path, sin_preflight: None) -> None:
    """Las fases asistidas no spawnnean: el runner inyectado queda sin usar."""
    runner = _runner()

    await _handler(runner, path_resolver=_resolver(tmp_path), profile="Requiem")

    runner.generate_bashed_patch.assert_not_awaited()


async def test_runner_none_ya_no_es_un_error_estructural(tmp_path: pathlib.Path, sin_preflight: None) -> None:
    """Sin build headless el runner no es una precondición: la etapa sigue su curso."""
    out = await _handler(None, path_resolver=_resolver(tmp_path), profile="Requiem")

    assert out["pendiente"] is True
    assert "error" not in out


async def test_sin_resolver_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Sin ``path_resolver`` no se inventa un verde: ``SinRutasResolubles``."""
    out = await _handler(_runner(), profile="Requiem")

    assert out["success"] is False
    assert out["reason"] == "SinRutasResolubles"


# ==============================================================================
# 2. No muta: ni lock, ni snapshot, ni caja negra
# ==============================================================================


async def test_el_handler_no_toma_ningun_lock(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
    sin_preflight: None,
) -> None:
    with patch.object(lock_manager, "acquire_lock", AsyncMock(wraps=lock_manager.acquire_lock)) as espia:
        out = await _handler(
            _runner(),
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
            path_resolver=_resolver(tmp_path),
            profile="Requiem",
        )

    assert out["pendiente"] is True
    espia.assert_not_awaited()


async def test_el_handler_no_emite_caja_negra(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
    sin_preflight: None,
) -> None:
    """Una etapa que sólo lee y describe no abre TX: no hay nada que revertir.

    Era el defecto que el PR A documentó y el PR B cierra: cada invocación del agente
    dejaba una TX ``rolled_back`` por un ritual que nunca iba a mutar.
    """
    journal = MagicMock()
    journal.begin_transaction = AsyncMock(return_value=88)
    journal.commit_transaction = AsyncMock()
    journal.mark_transaction_rolled_back = AsyncMock()
    journal.persist_action_manifest = AsyncMock()
    journal.persist_flight_report = AsyncMock()

    out = await _handler(
        _runner(),
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        journal=journal,
        path_resolver=_resolver(tmp_path),
        profile="Requiem",
    )

    assert out["pendiente"] is True
    journal.begin_transaction.assert_not_awaited()
    journal.persist_action_manifest.assert_not_awaited()
    journal.persist_flight_report.assert_not_awaited()
    journal.mark_transaction_rolled_back.assert_not_awaited()


async def test_un_lock_ajeno_ya_no_bloquea_la_etapa(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    tmp_path: pathlib.Path,
    sin_preflight: None,
) -> None:
    """Reemplaza a los dos viejos «bloqueado si…», que ahora serían falsos verdes.

    Con una corrida de Wrye Bash y un sort de LOOT en curso, la etapa asistida tiene que
    poder PREPARAR igual: no compite por el artefacto porque no lo escribe. Antes esta
    llamada devolvía ``success=False`` por contención de lock — y, tras el cableado,
    devolvía ``success=False`` por otra razón (``SinRutasResolubles``), que es la clase de
    verde/rojo que miente que este repo ya pagó varias veces.
    """
    await lock_manager.acquire_lock(BASHED_PATCH_RESOURCE_ID, "other-runner", ttl=30.0)
    await lock_manager.acquire_lock(LOAD_ORDER_RESOURCE_ID, "loot-service", ttl=30.0)

    out = await _handler(
        _runner(),
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=_resolver(tmp_path),
        profile="Requiem",
    )

    assert out["pendiente"] is True
    assert out["success"] is False
    assert "SinRutasResolubles" not in json.dumps(out)


# ==============================================================================
# 3. Contrato de la tool (T11) y nombre canónico
# ==============================================================================


async def test_el_handler_devuelve_json_serializable_aun_sin_dependencias() -> None:
    """Ninguna combinación de dependencias ausentes puede escapar como excepción."""
    salida = await generate_bashed_patch(None)

    assert isinstance(salida, str)
    assert isinstance(json.loads(salida), dict)


def test_nombre_canonico_del_bashed_patch_sincronizado() -> None:
    """El runner, el servicio (#315) y la estrategia de delegación (ADR 0001) nombran
    el mismo .esp — un drift acá rompería el snapshot/lock de alguno."""
    assert BASHED_PATCH_NAME == DelegateToBashedPatch.BASHED_PATCH_NAME
    assert BASHED_PATCH_NAME == BASHED_PATCH_RESOURCE_ID

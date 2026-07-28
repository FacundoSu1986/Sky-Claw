"""Rollback de la SALIDA ante un resultado fallido — U-04.

**Las dos mitades, y por qué ninguna alcanza sola.** Que un ritual pase su
destino real como ``target_files`` al :class:`SnapshotTransactionLock` es
condición necesaria pero **no suficiente**: el lock solo restaura ante excepción
o ante ``force_rollback``. Un subproceso que sale non-zero (o que agota su
timeout y se traduce a ``success=False``) hace que el ``async with`` salga
*limpio*, así que ``__aexit__`` **descarta** el snapshot y deja el output parcial
en disco — con la agravante de que el caller cree que hubo rollback. Por eso los
tests de acá afirman sobre el **contenido del archivo tras el run fallido**, no
sobre que el lock haya recibido una lista no vacía.

**El ancla enumera la familia, no muestrea un caso** (patrón ``RITUAL_TOOL_MAP``
de ``tests/test_ritual_dispatch.py`` y ``SERVICIOS_CON_SALIDA`` de
``tests/test_output_targets.py``): **todo módulo del paquete** que construya un
``SnapshotTransactionLock`` debe estar clasificado con su mecanismo de rollback, y
los que hoy no tienen uno figuran con el motivo **nombrado**. Un mutador nuevo
rompe el guard de completitud hasta que alguien lo clasifique.

El alcance del barrido es en sí mismo la lección: la primera versión de este
archivo enumeraba ``local/tools/*_service.py`` y por eso no veía a
``run_bodyslide_batch``, que toma el lock desde ``app/agent/tools/system_tools.py``
— un ancla de familia que muestrea el directorio equivocado reproduce el defecto
que existe para atajar.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME, WryeBashResult
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_PAQUETE = pathlib.Path(__file__).resolve().parent.parent / "sky_claw"

#: Módulos que construyen el lock SIN ser un mutador de salida — se excluyen de la
#: familia con su motivo, no por omisión.
_NO_SON_MUTADORES: dict[str, str] = {
    "sky_claw/app/db/locks.py": "define la clase; no la usa sobre un output.",
    "sky_claw/app/orchestrator/preview/chain_preview_service.py": (
        "es el dry-run: usa force_rollback=True, que YA revierte siempre — el "
        "caso que este archivo cubre es el contrario (salida limpia sin revertir)."
    ),
}


# ---------------------------------------------------------------------------
# Ancla de familia — todo mutador que toma el lock declara su rollback
# ---------------------------------------------------------------------------

#: Módulo → mecanismo con el que revierte su SALIDA ante un run fallido.
#:
#: **Se enumera por módulo, no por servicio.** El primer borrador de este ancla
#: barría ``local/tools/*_service.py`` y por eso NO veía a ``run_bodyslide_batch``,
#: que toma el lock desde ``app/agent/tools/system_tools.py`` — la forma exacta del
#: defecto que el ancla existe para atajar. La familia es "quien construye un
#: ``SnapshotTransactionLock``", viva donde viva.
#:
#: ``"snapshot"``    — ``target_files`` reales en el ``SnapshotTransactionLock``.
#: ``"directorio"``  — move-aside O(1) de ``DirectoryRollback``.
#: ``"no-muta"``     — no produce salida propia que revertir (motivo nombrado).
#: ``"pendiente"``   — U-04 sigue ABIERTO acá, con el motivo nombrado. NO es un
#:                     "ya lo miramos": es deuda declarada, y el test de abajo
#:                     exige que el motivo esté escrito.
MECANISMO_DE_ROLLBACK: dict[str, str] = {
    "sky_claw/local/tools/wrye_bash_service.py": "snapshot",
    "sky_claw/local/tools/synthesis_service.py": "snapshot",
    "sky_claw/local/tools/xedit_service.py": "snapshot",
    "sky_claw/local/tools/loot_service.py": "snapshot",
    "sky_claw/local/tools/dyndolod_service.py": "directorio",
    "sky_claw/local/tools/vramr_service.py": "no-muta",
    "sky_claw/local/tools/pandora_service.py": "pendiente",
    "sky_claw/app/agent/tools/system_tools.py": "pendiente",
}

#: Motivo, para los que no tienen rollback de salida. Escribir el racional NO
#: cuenta como cerrarlo (#318/#373): estos siguen contando como U-04 abierto en
#: ``docs/pending_ooda_status.md``.
MOTIVO: dict[str, str] = {
    "sky_claw/local/tools/vramr_service.py": (
        "VRAMr optimiza texturas hacia su propio output y no muta la entrada; el "
        "lock serializa, no snapshotea. Anclado en tests/test_vramr_service.py."
    ),
    "sky_claw/local/tools/pandora_service.py": (
        "Sus destinos (output_targets.pandora_output_candidates) son DIRECTORIOS "
        "compartidos —el Data del juego y el dir del exe—, no archivos: el "
        "SnapshotTransactionLock los saltearía en silencio (solo snapshotea "
        "is_file()) y el move-aside de DirectoryRollback sobre el Data del juego "
        "sería destructivo. Necesita una decisión de diseño, no un cableado."
    ),
    "sky_claw/app/agent/tools/system_tools.py": (
        "run_bodyslide_batch escribe en game_path/<output_path>, un DIRECTORIO "
        "cuyo nombre elige el LLM (BodySlideBatchParams.output_path, default "
        "'meshes'). Mismo bloqueo que Pandora, agravado: el destino del "
        "move-aside no sería una constante del código sino un parámetro del "
        "modelo, así que apuntar a un árbol compartido (Data/meshes) haría "
        "destructivo el propio rollback."
    ),
}


def _modulos_que_toman_el_lock() -> set[str]:
    """Familia a clasificar: TODO módulo que construya el lock transaccional.

    Se detecta por el uso de ``SnapshotTransactionLock(``: tomar el lock es,
    literalmente, declarar "esta operación muta estado compartido". Se barre el
    paquete entero a propósito — acotarlo a ``local/tools`` fue lo que dejó a
    BodySlide fuera de la primera versión de este ancla.
    """
    return {
        ruta.relative_to(_PAQUETE.parent).as_posix()
        for ruta in _PAQUETE.rglob("*.py")
        if "SnapshotTransactionLock(" in ruta.read_text(encoding="utf-8")
    } - set(_NO_SON_MUTADORES)


def test_la_enumeracion_cubre_todos_los_mutadores() -> None:
    """Guard de completitud: un mutador nuevo obliga a declarar CÓMO revierte su
    salida, en vez de heredar en silencio el ``target_files=[]``."""
    assert _modulos_que_toman_el_lock() == set(MECANISMO_DE_ROLLBACK)


def test_todo_mutador_sin_rollback_nombra_su_motivo() -> None:
    """ "Pendiente" sin motivo escrito es indistinguible de un olvido."""
    sin_rollback = {m for m, mecanismo in MECANISMO_DE_ROLLBACK.items() if mecanismo in {"pendiente", "no-muta"}}

    assert sin_rollback == set(MOTIVO)
    assert all(MOTIVO[m].strip() for m in sin_rollback)


# ---------------------------------------------------------------------------
# Wrye Bash — el Bashed Patch vuelve a su estado previo si el run fracasa
# ---------------------------------------------------------------------------


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> AsyncIterator[DistributedLockManager]:
    mgr = DistributedLockManager(
        tmp_path / "locks.db",
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    destino = tmp_path / "snapshots"
    destino.mkdir()
    mgr = FileSnapshotManager(snapshot_dir=destino)
    await mgr.initialize()
    return mgr


def _runner_que_escribe(
    game: pathlib.Path,
    *,
    contenido: str,
    exito: bool,
) -> MagicMock:
    """Runner que MUTA el Bashed Patch en disco y luego reporta ``exito``.

    Reproduce el modo de falla real: Wrye Bash alcanza a escribir un patch
    parcial y recién después sale non-zero (o agota el timeout). Un runner que
    solo devuelve ``success=False`` sin tocar el disco haría pasar el test aun
    sin rollback.
    """
    patch_path = game / "Data" / BASHED_PATCH_NAME

    async def _generar() -> WryeBashResult:
        patch_path.write_text(contenido, encoding="utf-8")
        return WryeBashResult(
            success=exito,
            return_code=0 if exito else 3,
            stdout="",
            stderr="" if exito else "Wrye Bash falló a mitad del rebuild",
            duration_seconds=0.1,
        )

    runner = MagicMock()
    runner.config.game_path = game
    runner.generate_bashed_patch = AsyncMock(side_effect=_generar)
    return runner


def _servicio(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    runner: MagicMock,
) -> WryeBashPipelineService:
    return WryeBashPipelineService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        wrye_bash_runner=runner,
    )


@pytest.fixture
def game(tmp_path: pathlib.Path) -> pathlib.Path:
    raiz = tmp_path / "Skyrim Special Edition"
    (raiz / "Data").mkdir(parents=True)
    return raiz


async def test_run_fallido_restaura_el_bashed_patch_previo(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game: pathlib.Path,
) -> None:
    """La mitad (b): ``success=False`` NO eleva excepción, así que sin forzar el
    rollback el ``async with`` sale limpio, el snapshot se descarta y el patch
    parcial queda en disco — con el caller creyendo que se revirtió."""
    patch_path = game / "Data" / BASHED_PATCH_NAME
    patch_path.write_text("patch previo, valido", encoding="utf-8")
    runner = _runner_que_escribe(game, contenido="patch a medio escribir", exito=False)

    resultado = await _servicio(lock_manager, snapshot_manager, runner).execute_pipeline(profile="Default")

    assert resultado["success"] is False
    assert patch_path.read_text(encoding="utf-8") == "patch previo, valido"


async def test_run_exitoso_conserva_el_patch_nuevo(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game: pathlib.Path,
) -> None:
    """La contracara obligatoria: forzar el rollback ante el fallo no debe
    revertir un run que salió bien (sería un rollback que se come el éxito)."""
    patch_path = game / "Data" / BASHED_PATCH_NAME
    patch_path.write_text("patch previo", encoding="utf-8")
    runner = _runner_que_escribe(game, contenido="patch nuevo y valido", exito=True)

    resultado = await _servicio(lock_manager, snapshot_manager, runner).execute_pipeline(profile="Default")

    assert resultado["success"] is True
    assert patch_path.read_text(encoding="utf-8") == "patch nuevo y valido"


async def test_el_lock_snapshotea_el_destino_real_no_una_lista_vacia(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game: pathlib.Path,
) -> None:
    """La mitad (a): el destino que se snapshotea es el MISMO que declara el
    manifiesto (``output_targets.bashed_patch_target``). Dos fuentes distintas
    para "dónde escribe Wrye Bash" es el episodio #373 esperando a repetirse."""
    patch_path = game / "Data" / BASHED_PATCH_NAME
    patch_path.write_text("previo", encoding="utf-8")
    runner = _runner_que_escribe(game, contenido="nuevo", exito=True)
    servicio = _servicio(lock_manager, snapshot_manager, runner)

    assert servicio._snapshot_targets(runner) == [patch_path]
    assert servicio._bashed_patch_target(runner) == str(patch_path)


async def test_sin_patch_previo_no_inventa_un_rollback(
    lock_manager: DistributedLockManager,
    snapshot_manager: FileSnapshotManager,
    game: pathlib.Path,
) -> None:
    """Primer rebuild (no había patch): no hay estado previo que restaurar, así
    que el parcial queda en disco. El resultado debe seguir siendo un fallo
    honesto — nunca un ``success`` que insinúe que se limpió."""
    patch_path = game / "Data" / BASHED_PATCH_NAME
    runner = _runner_que_escribe(game, contenido="parcial sin previo", exito=False)

    resultado = await _servicio(lock_manager, snapshot_manager, runner).execute_pipeline(profile="Default")

    assert resultado["success"] is False
    assert patch_path.exists()

"""``system_tools.generate_bashed_patch`` — la superficie del agente LLM (ADR 0016).

Antes del PR B este handler era el SEGUNDO path de producción del Bashed Patch: delegaba
en ``WryeBashPipelineService.execute_pipeline``, tomaba el lock anidado
(``Bashed Patch, 0.esp`` + ``load-order``), emitía ``ActionManifest``/``FlightReport`` y
abría una TX… para que el runner fallara cerrado siempre (``bash.py`` no expone build).
Ese camino dejó de existir en esta superficie: la etapa 6 es **asistida** y la construye
el operador en la GUI de Wrye Bash lanzado desde MO2.

Lo que este módulo ancla es el contrato NUEVO de la delegación, que es el inverso del
anterior y por eso tiene dientes propios:

1. **Una sola salida**: el handler delega en ``ejecutar_fase_asistida`` (el punto ÚNICO
   de enrutado, compartido con la GUI y el supervisor). ``execute_pipeline`` **no
   existe más** (PR C de ADR 0016 §7), así que no hay a dónde volver.
2. **No toma lock, no snapshotea y no emite caja negra**: ninguna de las dos fases muta.
   Un lock ajeno —de Wrye Bash o de un sort de LOOT en curso— NO la bloquea, y una
   invocación no deja una TX ``rolled_back`` por ritual.
3. **No hay subproceso NI se le puede pasar uno**: el runner dejó de ser un parámetro del
   handler. ``runner=None`` como "caso normal" era una promesa; que la firma no lo admita
   es la garantía.
4. **El contrato JSON de la tool se preserva** (T11): dict serializable, nunca excepción.

La lógica del lock, del rollback y del manifiesto **se retiró con el código que la
implementaba** (``execute_pipeline`` y sus helpers de caja negra: cero callers de
producción, ver ADR 0016 §7). Acá sólo sobrevive su negación: lo que la tool NO hace.
"""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.agent.tools.system_tools import generate_bashed_patch
from sky_claw.app.db.journal import OperationJournal
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.local.tools.loot_service import LOAD_ORDER_RESOURCE_ID
from sky_claw.local.tools.wrye_bash_asistida import BASHED_PATCH_NAME
from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService
from sky_claw.local.xedit.patch_orchestrator import DelegateToBashedPatch

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> Any:
    mgr = DistributedLockManager(
        tmp_path / "test_locks.db",
        default_ttl=5.0,
        max_retries=2,
        backoff_base=0.05,
        backoff_max=0.2,
    )
    await mgr.initialize()
    yield mgr
    await mgr.close()


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


async def _handler(**kwargs: Any) -> dict[str, Any]:
    return json.loads(await generate_bashed_patch(**kwargs))


# ==============================================================================
# 1. Una sola salida: el enrutado único del servicio
# ==============================================================================


async def test_el_handler_delega_en_el_enrutado_unico(
    tmp_path: pathlib.Path,
    sin_preflight: None,
) -> None:
    """La superficie del agente comparte el punto de decisión con GUI y supervisor.

    Se espía ``ejecutar_fase_asistida`` y se afirma que el camino headless **no existe**:
    después del PR C ya no alcanza con que no se llame, la clase no tiene el método. Es
    el ancla más barata contra una reintroducción por copiar y pegar del ritual muerto.
    """
    espia = AsyncMock(return_value={"success": False, "pendiente": True})
    with patch.object(WryeBashPipelineService, "ejecutar_fase_asistida", espia):
        out = await _handler(path_resolver=_resolver(tmp_path), profile="Requiem")

    assert out == {"success": False, "pendiente": True}
    espia.assert_awaited_once()
    argumentos = espia.await_args.kwargs
    assert argumentos["profile"] == "Requiem"
    assert argumentos["fase"] == "preparar"
    # M-04 se valida a nivel Supervisor para este path, como antes del PR B.
    assert argumentos["validate_limit"] is False
    assert not hasattr(WryeBashPipelineService, "execute_pipeline"), (
        "el camino headless retirado volvió a existir: la tool podría reabrirlo"
    )


async def test_el_handler_no_admite_un_runner(tmp_path: pathlib.Path, sin_preflight: None) -> None:
    """Las fases asistidas no spawnnean: el runner no es un parámetro, es un TypeError.

    Antes del PR C el runner se inyectaba y se afirmaba que no se invocaba. Con el retiro
    la garantía sube de nivel: si alguien vuelve a abrir la puerta al subproceso, esta
    llamada deja de fallar y el test lo dice.
    """
    runner = MagicMock()

    with pytest.raises(TypeError):
        await generate_bashed_patch(runner)  # type: ignore[call-arg]

    out = await _handler(path_resolver=_resolver(tmp_path), profile="Requiem")
    assert out["pendiente"] is True
    runner.generate_bashed_patch.assert_not_called()


async def test_sin_runner_la_etapa_sigue_su_curso(tmp_path: pathlib.Path, sin_preflight: None) -> None:
    """Sin build headless el runner no es una precondición: la etapa sigue su curso."""
    out = await _handler(path_resolver=_resolver(tmp_path), profile="Requiem")

    assert out["pendiente"] is True
    assert "error" not in out


async def test_sin_resolver_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """Sin ``path_resolver`` no se inventa un verde: ``SinRutasResolubles``."""
    out = await _handler(profile="Requiem")

    assert out["success"] is False
    assert out["reason"] == "SinRutasResolubles"


# ==============================================================================
# 2. No muta: ni lock, ni snapshot, ni caja negra
# ==============================================================================


async def test_el_handler_no_toma_ningun_lock(
    tmp_path: pathlib.Path,
    sin_preflight: None,
) -> None:
    """Si alguien reconstruye un lock manager por dentro, este test lo caza.

    El handler ya no recibe el manager, así que espiar la instancia no probaría nada: se
    envenena el método de la CLASE. Una toma de lock —de cualquier instancia— explota.
    """
    with patch.object(
        DistributedLockManager,
        "acquire_lock",
        AsyncMock(side_effect=AssertionError("la etapa asistida no toma locks")),
    ):
        out = await _handler(path_resolver=_resolver(tmp_path), profile="Requiem")

    assert out["pendiente"] is True


async def test_el_handler_no_emite_caja_negra(
    tmp_path: pathlib.Path,
    sin_preflight: None,
) -> None:
    """Una etapa que sólo lee y describe no abre TX: no hay nada que revertir.

    Era el defecto que el PR A documentó y el PR B cierra: cada invocación del agente
    dejaba una TX ``rolled_back`` por un ritual que nunca iba a mutar. El journal ya no
    viaja al handler, así que se envenena la clase: cualquier apertura de TX explota.
    """
    with patch.object(
        OperationJournal,
        "begin_transaction",
        AsyncMock(side_effect=AssertionError("la etapa asistida no abre transacciones")),
    ):
        out = await _handler(path_resolver=_resolver(tmp_path), profile="Requiem")

    assert out["pendiente"] is True
    # Ni siquiera se le puede inyectar un journal: la firma no lo admite.
    parametros = set(inspect.signature(generate_bashed_patch).parameters)
    assert parametros == {"path_resolver", "profile", "fase", "testigo_previo"}, parametros


async def test_un_lock_ajeno_ya_no_bloquea_la_etapa(
    lock_manager: DistributedLockManager,
    tmp_path: pathlib.Path,
    sin_preflight: None,
) -> None:
    """Reemplaza a los dos viejos «bloqueado si…», que ahora serían falsos verdes.

    Con una corrida de Wrye Bash y un sort de LOOT en curso, la etapa asistida tiene que
    poder PREPARAR igual: no compite por el artefacto porque no lo escribe. Antes esta
    llamada devolvía ``success=False`` por contención de lock — y, tras el cableado,
    devolvía ``success=False`` por otra razón (``SinRutasResolubles``), que es la clase de
    verde/rojo que miente que este repo ya pagó varias veces.

    Los ids de lock son literales de OTROS servicios a propósito: el ancla de PR C prohíbe
    ``Bashed Patch, 0.esp`` en producción, no en un test que simula un lock ajeno.
    """
    await lock_manager.acquire_lock(BASHED_PATCH_NAME, "other-runner", ttl=30.0)
    await lock_manager.acquire_lock(LOAD_ORDER_RESOURCE_ID, "loot-service", ttl=30.0)

    out = await _handler(path_resolver=_resolver(tmp_path), profile="Requiem")

    assert out["pendiente"] is True
    assert out["success"] is False
    assert "SinRutasResolubles" not in json.dumps(out)


# ==============================================================================
# 3. Contrato de la tool (T11) y nombre canónico
# ==============================================================================


async def test_el_handler_devuelve_json_serializable_aun_sin_dependencias() -> None:
    """Ninguna combinación de dependencias ausentes puede escapar como excepción."""
    salida = await generate_bashed_patch()

    assert isinstance(salida, str)
    assert isinstance(json.loads(salida), dict)


def test_nombre_canonico_del_bashed_patch_sincronizado() -> None:
    """El runner y la estrategia de delegación (ADR 0001) nombran el mismo .esp.

    La tercera pata de esta igualdad era ``BASHED_PATCH_RESOURCE_ID`` —el id de lock del
    camino headless—, que se retiró con él (ADR 0016 §7): sin tomador, un id de lock
    "canónico" sólo serviría para que el próximo lector creyera que algo lo usa.
    """
    assert BASHED_PATCH_NAME == DelegateToBashedPatch.BASHED_PATCH_NAME

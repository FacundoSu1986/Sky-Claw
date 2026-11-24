"""PR-B — Sky-Claw provee masterlist y prelude de LOOT; ``updateMasterlist = false``.

Propiedad del mecanismo: *LOOT nunca ordena sin los metadatos de la rama de su
versión, y nunca necesita red para ordenar*. El daemon (único proceso con
egress) los descarga por el ``NetworkGateway`` al data root aislado; el worker
se niega a lanzar LOOT si faltan (upstream los ignoraría en silencio,
``game.cpp:945-962``). La regla aplica a las DOS superficies que construyen el
servicio (GUI y agente LLM): el ancla del final las enumera por AST.
"""

from __future__ import annotations

import ast
import os
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.loot.cli import LOOTConfig, LOOTPreconditionError, LOOTRunner
from sky_claw.local.loot.masterlist_provision import (
    LOOT_MASTERLIST_BRANCHES,
    LOOT_MASTERLIST_REPOSITORIES,
    GatewayMasterlistFetcher,
    LootMasterlistError,
    masterlist_branch,
    masterlist_path,
    masterlist_url,
    prelude_path,
    prelude_url,
    provision_masterlist,
    verify_masterlist_provisioned,
)
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.load_order import LoadOrderFileResolver
from sky_claw.local.tools.loot_service import LootSortingService
from tests._loot_masterlist import sembrar_masterlist
from tests._loot_pe import escribir_loot_exe
from tests._loot_witness import TESTIGO_FRESCO

RAIZ = pathlib.Path(__file__).resolve().parents[1]
JUEGO = "Skyrim Special Edition"
VERSION = "0.29.1"
URL_MASTERLIST = "https://raw.githubusercontent.com/loot/skyrimse/v0.29/masterlist.yaml"
URL_PRELUDE = "https://raw.githubusercontent.com/loot/prelude/v0.29/prelude.yaml"
MASTERLIST = b"bash_tags: [Delev]\nplugins:\n  - name: 'Mod A.esp'\n"
PRELUDE = b"common:\n  - &x 1\n"


class _Fetch:
    """Fetcher falso: responde por URL y registra cada pedido."""

    def __init__(self, respuestas: dict[str, bytes | BaseException]) -> None:
        self.respuestas = respuestas
        self.pedidos: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.pedidos.append(url)
        respuesta = self.respuestas[url]
        if isinstance(respuesta, BaseException):
            raise respuesta
        return respuesta


def _ok() -> _Fetch:
    return _Fetch({URL_MASTERLIST: MASTERLIST, URL_PRELUDE: PRELUDE})


def _caido() -> _Fetch:
    error = OSError("sin red")
    return _Fetch({URL_MASTERLIST: error, URL_PRELUDE: error})


# =============================================================================
# Contrato congelado contra upstream (game_settings.h:37, game_settings.cpp:88-114,
# loot_settings.cpp:823-824, game.cpp:109-111/390-393, loot_paths.cpp:74-75)
# =============================================================================


def test_ramas_y_repositorios_congelados_contra_upstream() -> None:
    assert dict(LOOT_MASTERLIST_BRANCHES) == {(0, 29): "v0.29"}
    assert dict(LOOT_MASTERLIST_REPOSITORIES) == {"Skyrim Special Edition": "skyrimse", "Skyrim VR": "skyrimse"}
    assert masterlist_url(JUEGO, "0.29.2") == URL_MASTERLIST
    assert prelude_url("0.29.2") == URL_PRELUDE


def test_rutas_exactas_del_data_root(tmp_path: pathlib.Path) -> None:
    assert masterlist_path(tmp_path, JUEGO) == tmp_path / "games" / JUEGO / "masterlist.yaml"
    assert prelude_path(tmp_path) == tmp_path / "prelude" / "prelude.yaml"


@pytest.mark.parametrize("version", ["0.30.0", "0.28.1", "1.0.0"])
def test_version_sin_rama_verificada_falla_cerrado(version: str) -> None:
    with pytest.raises(LootMasterlistError, match="rama de masterlist verificada"):
        masterlist_branch(version)


@pytest.mark.parametrize("version", ["0.29", "v0.29.1", "0.29.1 ", "00.29.1"])
def test_version_mal_formada_falla_cerrado(version: str) -> None:
    with pytest.raises(LootMasterlistError, match="mal formada"):
        masterlist_branch(version)


def test_juego_sin_repositorio_falla_cerrado(tmp_path: pathlib.Path) -> None:
    with pytest.raises(LootMasterlistError, match="repositorio"):
        masterlist_path(tmp_path, "Oblivion")


# =============================================================================
# Provisión
# =============================================================================


async def test_root_nuevo_descarga_ambos_y_registra_la_fuente(tmp_path: pathlib.Path) -> None:
    fetch = _ok()

    informe = await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=fetch)

    assert sorted(fetch.pedidos) == sorted([URL_MASTERLIST, URL_PRELUDE])
    assert masterlist_path(tmp_path, JUEGO).read_bytes() == MASTERLIST
    assert prelude_path(tmp_path).read_bytes() == PRELUDE
    assert dict(informe.acciones) == {"prelude": "descargado", "masterlist": "descargado"}
    assert informe.avisos == ()
    assert list(tmp_path.rglob("*.tmp")) == []


async def test_copia_vigente_de_la_misma_fuente_no_vuelve_a_descargar(tmp_path: pathlib.Path) -> None:
    await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=_ok())
    segundo = _ok()

    informe = await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=segundo)

    assert segundo.pedidos == []
    assert set(informe.acciones.values()) == {"vigente"}


async def test_copia_vencida_sin_red_se_conserva_con_aviso(tmp_path: pathlib.Path) -> None:
    await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=_ok())
    vieja = 1_000_000_000
    for ruta in (masterlist_path(tmp_path, JUEGO), prelude_path(tmp_path)):
        os.utime(ruta, (vieja, vieja))

    informe = await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=_caido())

    assert set(informe.acciones.values()) == {"copia_anterior"}
    assert len(informe.avisos) == 2
    assert masterlist_path(tmp_path, JUEGO).read_bytes() == MASTERLIST


async def test_sin_copia_y_sin_red_falla_cerrado_sin_escribir(tmp_path: pathlib.Path) -> None:
    with pytest.raises(LootMasterlistError, match="sin metadatos"):
        await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=_caido())
    assert not masterlist_path(tmp_path, JUEGO).exists()


async def test_sin_egress_cableado_y_sin_copia_falla_cerrado(tmp_path: pathlib.Path) -> None:
    with pytest.raises(LootMasterlistError, match="egress"):
        await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=None)


async def test_copia_de_otra_rama_no_se_usa_si_no_se_puede_refrescar(tmp_path: pathlib.Path) -> None:
    # Una copia de v0.26 (de un LOOT anterior) no vale para 0.29: falla cerrado.
    for ruta, url in (
        (masterlist_path(tmp_path, JUEGO), URL_MASTERLIST.replace("v0.29", "v0.26")),
        (prelude_path(tmp_path), URL_PRELUDE.replace("v0.29", "v0.26")),
    ):
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(b"plugins: []\n")
        ruta.with_name(ruta.name + ".skyclaw-source").write_text(url, encoding="utf-8")

    with pytest.raises(LootMasterlistError):
        await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=_caido())


@pytest.mark.parametrize(
    "contenido",
    [b"", b"- una\n- lista\n", b"clave: [sin cerrar\n", b"\xff\xfe"],
    ids=["vacio", "no_mapping", "yaml_invalido", "no_utf8"],
)
async def test_respuesta_invalida_no_pisa_la_copia_previa(tmp_path: pathlib.Path, contenido: bytes) -> None:
    await provision_masterlist(tmp_path, game_folder=JUEGO, loot_version=VERSION, fetch=_ok())
    for ruta in (masterlist_path(tmp_path, JUEGO), prelude_path(tmp_path)):
        os.utime(ruta, (1_000_000_000, 1_000_000_000))

    informe = await provision_masterlist(
        tmp_path,
        game_folder=JUEGO,
        loot_version=VERSION,
        fetch=_Fetch({URL_MASTERLIST: contenido, URL_PRELUDE: contenido}),
    )

    assert set(informe.acciones.values()) == {"copia_anterior"}
    assert masterlist_path(tmp_path, JUEGO).read_bytes() == MASTERLIST
    assert prelude_path(tmp_path).read_bytes() == PRELUDE


def test_verificacion_previa_al_spawn(tmp_path: pathlib.Path) -> None:
    with pytest.raises(LootMasterlistError, match="sin metadatos"):
        verify_masterlist_provisioned(tmp_path, JUEGO)
    sembrar_masterlist(tmp_path)
    verify_masterlist_provisioned(tmp_path, JUEGO)
    prelude_path(tmp_path).write_bytes(b"")
    with pytest.raises(LootMasterlistError, match="vacío"):
        verify_masterlist_provisioned(tmp_path, JUEGO)


# =============================================================================
# Runner: sin masterlist LOOT no se lanza
# =============================================================================


async def test_runner_sin_masterlist_no_lanza_loot(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "loot_data"
    root.mkdir()
    runner = LOOTRunner(
        LOOTConfig(loot_exe=escribir_loot_exe(tmp_path / "LOOT" / "LOOT.exe"), game_path=tmp_path, loot_data_path=root)
    )
    fake_exec = AsyncMock()
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
        pytest.raises(LOOTPreconditionError, match="sin metadatos"),
    ):
        await runner.sort()
    fake_exec.assert_not_awaited()


# =============================================================================
# Servicio: provee antes del spawn, dentro del lock; falla como precondición
# =============================================================================

_PLUGINS = b"*Mod A.esp\n*Mod B.esp\n"


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> DistributedLockManager:
    mgr = DistributedLockManager(tmp_path / "locks.db", default_ttl=5.0, max_retries=2, backoff_base=0.05)
    await mgr.initialize()
    yield mgr  # type: ignore[misc]
    await mgr.close()


@pytest.fixture
async def snapshot_manager(tmp_path: pathlib.Path) -> FileSnapshotManager:
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    mgr = FileSnapshotManager(snapshot_dir=snapshot_dir)
    await mgr.initialize()
    return mgr


def _runner_falso(tmp_path: pathlib.Path) -> Any:
    runner = MagicMock()
    runner.sort = AsyncMock(return_value=LOOTResult(return_code=0, execution_witness=TESTIGO_FRESCO))
    runner.loot_data_path = tmp_path / "loot_data"
    runner.loot_data_path.mkdir()
    runner.loot_exe = escribir_loot_exe(tmp_path / "LOOT" / "LOOT.exe")
    runner.game = "SkyrimSE"
    runner.game_data_dir = None
    return runner


def _servicio(lock_manager: Any, snapshot_manager: Any, runner: Any, tmp_path: pathlib.Path, fetch: Any) -> Any:
    directorio = tmp_path / "load_order"
    directorio.mkdir()
    (directorio / "plugins.txt").write_bytes(_PLUGINS)
    return LootSortingService(
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
        path_resolver=MagicMock(),
        loot_runner=runner,
        load_order_resolver=LoadOrderFileResolver(explicit_dir=directorio),
        preflight=MagicMock(run=AsyncMock(return_value=MagicMock(blocks_mutations=False)), loot_version=None),
        masterlist_fetcher=fetch,
    )


async def test_servicio_provee_el_masterlist_antes_del_sort(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    runner = _runner_falso(tmp_path)
    fetch = _ok()

    res = await _servicio(lock_manager, snapshot_manager, runner, tmp_path, fetch).sort_load_order()

    assert res["success"] is True, res
    runner.sort.assert_awaited_once()
    assert masterlist_path(runner.loot_data_path, JUEGO).read_bytes() == MASTERLIST
    assert res["masterlist"] == {"acciones": {"prelude": "descargado", "masterlist": "descargado"}, "avisos": []}


async def test_servicio_sin_masterlist_es_precondicion_y_no_lanza_loot(
    lock_manager, snapshot_manager, tmp_path
) -> None:  # noqa: ANN001
    runner = _runner_falso(tmp_path)

    res = await _servicio(lock_manager, snapshot_manager, runner, tmp_path, _caido()).sort_load_order()

    assert res["success"] is False
    assert res["failure_reason"] == "precondition_failed"
    assert "Masterlist de LOOT no disponible" in res["outcome_detail"]
    runner.sort.assert_not_awaited()
    assert (tmp_path / "load_order" / "plugins.txt").read_bytes() == _PLUGINS


async def test_servicio_con_loot_sin_rama_verificada_no_lanza(lock_manager, snapshot_manager, tmp_path) -> None:  # noqa: ANN001
    runner = _runner_falso(tmp_path)
    runner.loot_exe = escribir_loot_exe(tmp_path / "LOOT30" / "LOOT.exe", (0, 30, 0))

    res = await _servicio(lock_manager, snapshot_manager, runner, tmp_path, _ok()).sort_load_order()

    assert res["failure_reason"] == "precondition_failed"
    assert "rama de masterlist verificada" in res["outcome_detail"]
    runner.sort.assert_not_awaited()


# =============================================================================
# Fetcher productivo sobre el NetworkGateway
# =============================================================================


async def test_fetcher_del_gateway_devuelve_el_cuerpo_y_rechaza_no_200() -> None:
    from sky_claw.app.security.network_gateway import NetworkGateway

    def _respuesta(status: int, cuerpo: bytes) -> Any:
        resp = MagicMock(status=status)
        resp.content.read = AsyncMock(return_value=cuerpo)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    gateway = NetworkGateway()
    with patch.object(gateway, "request", AsyncMock(return_value=_respuesta(200, MASTERLIST))) as pedido:
        assert await GatewayMasterlistFetcher(gateway)(URL_MASTERLIST) == MASTERLIST
    assert pedido.await_args.args[:2] == ("GET", URL_MASTERLIST)
    with (
        patch.object(gateway, "request", AsyncMock(return_value=_respuesta(404, b""))),
        pytest.raises(LootMasterlistError, match="HTTP 404"),
    ):
        await GatewayMasterlistFetcher(gateway)(URL_MASTERLIST)


# =============================================================================
# Ancla: las DOS superficies le dan egress al servicio (enumera, no muestrea)
# =============================================================================


def _llamadas(nombre: str) -> list[tuple[str, ast.Call]]:
    encontradas: list[tuple[str, ast.Call]] = []
    for ruta in sorted((RAIZ / "sky_claw").rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                func = nodo.func
                llamado = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if llamado == nombre:
                    encontradas.append((ruta.relative_to(RAIZ).as_posix(), nodo))
    return encontradas


def _kwargs(nodo: ast.Call) -> set[str]:
    return {k.arg for k in nodo.keywords if k.arg is not None}


def test_todo_constructor_productivo_del_servicio_cablea_el_fetcher() -> None:
    sitios = _llamadas("LootSortingService")
    assert sorted(ruta for ruta, _ in sitios) == [
        "sky_claw/app/agent/tools/system_tools.py",
        "sky_claw/app/orchestrator/orchestration_composition.py",
    ]
    for ruta, nodo in sitios:
        assert "masterlist_fetcher" in _kwargs(nodo), ruta


def test_la_superficie_del_agente_pasa_el_fetcher_a_run_loot_sort() -> None:
    sitios = [(r, n) for r, n in _llamadas("run_loot_sort") if r == "sky_claw/app/agent/tools/__init__.py"]
    assert len(sitios) == 1
    assert "masterlist_fetcher" in _kwargs(sitios[0][1])


def test_la_superficie_gui_pasa_el_gateway_a_la_composicion() -> None:
    sitios = [(r, n) for r, n in _llamadas("build_orchestration_composition") if r.startswith("sky_claw/")]
    assert [r for r, _ in sitios] == ["sky_claw/app/orchestrator/supervisor.py"]
    assert "gateway" in _kwargs(sitios[0][1])

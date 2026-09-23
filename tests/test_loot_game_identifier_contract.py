"""PR-0: contrato exacto del identificador ``--game`` y del backend de LOOT.

Congela el call graph completo:

    BrokeredLootRunner (``payload["game"]`` = id INTERNO)
    → ``vfs_worker._loot_handler`` (allowlist contra la MISMA frontera)
    → ``LOOTRunner`` (``LOOTConfig.game`` = id interno)
    → ``to_loot_cli_game_id`` (frontera ÚNICA de traducción)
    → ``LOOT.exe --game "<identificador exacto>" --game-path <path> --auto-sort``

Evidencia upstream (``loot/loot`` tag ``0.29.1``, commit ``77f3ba98``):

* ``src/gui/qt/main.cpp``: las opciones declaradas son ``--game``,
  ``--game-path``, ``--loot-data-path`` y ``--auto-sort``.
* ``src/gui/state/game/game_id.cpp`` (``toString(GameId)``): el dialecto de
  ``--game`` son nombres tipo ``"Skyrim Special Edition"`` / ``"Skyrim VR"``.
  ``"SkyrimSE"``/``"SkyrimVR"`` (dialecto legacy ≤0.27) NO matchean ningún
  juego — el match es de string exacto contra el ``folderName``.
* ``src/gui/state/loot_state.cpp`` (``setInitialGame``): un ``--game`` no
  reconocido NO falla cerrado: cae a ``getFirstInstalledGameFolderName()`` y
  ``--auto-sort`` ordena el PRIMER juego instalado. El bug pre-PR-0
  (``--game SkyrimSE`` en producción) era exactamente ese: fallo silencioso
  sobre el juego equivocado en multi-juego.
* ``src/gui/application_mutex.h``: mutex global ``LOOT.Shell.Instance`` — una
  segunda instancia sale ``rc 0`` enfocando la ventana existente (no-op con
  rc 0; el gate de evidencia física de ``LootSortingService`` lo caza, no el
  parser).

Mutaciones ancladas (deben salir ROJO si se reintroduce el defecto):

* M1: devolver el mapping a ``"SkyrimSE"`` → T1/T4 rojos.
* M2: permitir game string arbitrario (pasar el config tal cual al argv) → T2
  rojos (y T3: el worker dejaría de rechazar).
* M3: reintroducir ``--update-masterlist`` en el argv → T5 rojos (más el
  ancla rglob de ``test_contrato_argumentos_cli``).
* M4: bypass del broker VFS con runner directo → el guard F8 de
  ``test_vfs_production_wiring`` (T7) lo caza.
* (FINDING A) detector F8 case-sensitive (``relative_to(install_root / "loot")``)
  → ``test_t6_guard_f8_*casing*`` rojos (cualquier casing del subárbol
  ``loot`` escapaba del guard en un host case-sensitive).

Contratos adicionales (review FINDING B): T8 congela que
``update_masterlist=True`` propaga broker→worker→``LOOTRunner.sort``
(intención) y que ``--update-masterlist`` NUNCA viaja en el argv real
(no-op documentado: el flag no existe en LOOT 0.29.x).
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.loot.cli import (
    DEFAULT_LOOT_INTERNAL_GAME_ID,
    LOOT_CLI_GAME_IDENTIFIERS,
    LOOTConfig,
    LOOTGameIdError,
    LOOTNotFoundError,
    LOOTRunner,
    to_loot_cli_game_id,
)
from sky_claw.local.loot.parser import LOOTOutputParser, LOOTResult
from sky_claw.local.mo2.brokered_loot import (
    BrokeredLootRunner,
    VfsRequiredLootRunner,
    _is_mo2_internal_loot,
    build_vfs_loot_runner,
)
from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob, VfsJobResult
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import _loot_handler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capturando_exec():
    """Spy de ``create_subprocess_exec`` que captura el argv y sale 0 vacío."""
    captured: dict[str, list[str]] = {}

    async def fake_exec(*args: str, **_kwargs: object) -> AsyncMock:
        captured["args"] = list(args)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        proc.kill = MagicMock()
        return proc

    return captured, fake_exec


# ---------------------------------------------------------------------------
# T1 — el id interno produce el identificador CLI exacto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("internal_id", "cli_id"), sorted(LOOT_CLI_GAME_IDENTIFIERS.items()))
def test_t1_id_interno_produce_el_identificador_cli_exacto(internal_id: str, cli_id: str) -> None:
    assert to_loot_cli_game_id(internal_id) == cli_id


def test_t1_familia_de_ids_esta_congelada() -> None:
    """Ancla enumerable: agregar/quitar un juego exige actualizar AQUÍ a
    propósito (misma regla que ``RITUAL_TOOL_MAP`` en test_ritual_dispatch).
    El allowlist del worker se deriva de este mismo dict, así que la
    familia completa queda congelada de un solo lugar."""
    assert LOOT_CLI_GAME_IDENTIFIERS == {
        "SkyrimSE": "Skyrim Special Edition",
        "SkyrimVR": "Skyrim VR",
    }
    assert DEFAULT_LOOT_INTERNAL_GAME_ID == "SkyrimSE"


def test_t1_config_por_defecto_usa_el_id_interno() -> None:
    config = LOOTConfig(loot_exe=pathlib.Path("loot.exe"), game_path=pathlib.Path("juego"))
    assert config.game == DEFAULT_LOOT_INTERNAL_GAME_ID


# ---------------------------------------------------------------------------
# T2 — id desconocido falla cerrado; NO hay fallback silencioso
# ---------------------------------------------------------------------------

# "Fallout4" y "Skyrim" son ids de LOOT legítimos (otras familias de juegos)
# que NO pertenecen al dominio Sky-Claw: la frontera es el allowlist del
# dominio, no un passthrough. Las variantes de casing/whitespace documentan
# que el match es EXACTO (upstream no normaliza).
_IDS_INVALIDOS = (
    "Skyrim",
    "Skyrim SE",
    "SkyrimSE ",
    " skyrimse",
    "SKYRIM SPECIAL EDITION",
    "Fallout4",
    "",
)


@pytest.mark.parametrize("invalido", _IDS_INVALIDOS)
def test_t2_id_desconocido_falla_cerrado(invalido: str) -> None:
    with pytest.raises(LOOTGameIdError):
        to_loot_cli_game_id(invalido)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalido", _IDS_INVALIDOS)
async def test_t2_sort_rechaza_antes_del_subproceso(tmp_path: pathlib.Path, invalido: str) -> None:
    """El rechazo es ANTES de ``create_subprocess_exec``: un id sin
    traducción nunca llega a LOOT (LOOT 0.29.x no fallaría: ordenaría el
    primer juego instalado)."""
    exe = tmp_path / "loot.exe"
    exe.touch()
    juego = tmp_path / "Skyrim"
    juego.mkdir()
    runner = LOOTRunner(LOOTConfig(loot_exe=exe, game_path=juego, game=invalido))
    captured, fake_exec = _capturando_exec()

    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(juego)),
        pytest.raises(LOOTGameIdError),
    ):
        await runner.sort()

    assert "args" not in captured, "el subprocess no debe llegar a lanzarse"


# ---------------------------------------------------------------------------
# T3 — broker y worker NO divergen respecto al mismo mapping
# ---------------------------------------------------------------------------


class _Broker:
    def __init__(self) -> None:
        self.calls: list[tuple[object, dict[str, object]]] = []

    async def submit(self, job, **kwargs):
        self.calls.append((job, kwargs))
        return VfsJobResult.from_dict(
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "job_id": job.job_id,
                "success": True,
                "message": "",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "outputs": [str(path) for path in job.mutation_targets],
                "rollback_state": "not_required",
                "attestation": {
                    "profile": "Default",
                    "profile_fingerprint": job.expected_fingerprint,
                },
                "tool_result": {
                    "sorted_plugins": [],
                    "warnings": [],
                    "errors": [],
                    "missing_patches": [],
                },
            }
        )


def _entorno_broker(tmp_path: pathlib.Path):
    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    data = tmp_path / "Skyrim" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    (profile / "plugins.txt").write_text("*Skyrim.esm\n", encoding="utf-8")
    loot = tmp_path / "LOOT" / "loot.exe"
    loot.parent.mkdir()
    loot.write_bytes(b"loot")
    return mo2, data, loot


@pytest.mark.asyncio
async def test_t3_broker_envia_el_id_interno_de_la_misma_frontera(tmp_path: pathlib.Path) -> None:
    """El payload viaja con el id INTERNO del dominio (nunca el string de
    CLI): si el broker inventara un valor ajeno al mapping, el worker lo
    rechazaría y el sort moriría sin señal de por qué."""
    mo2, data, loot = _entorno_broker(tmp_path)
    broker = _Broker()
    runner = BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2,
        profile="Default",
        game_data_dir=data,
        loot_exe=loot,
        timeout=120,
        mutation_targets=lambda: (),
    )

    await runner.sort(update_masterlist=False)

    job, _ = broker.calls[0]
    assert job.payload["game"] == DEFAULT_LOOT_INTERNAL_GAME_ID
    assert job.payload["game"] in LOOT_CLI_GAME_IDENTIFIERS, (
        "el payload del broker debe usar un id interno de la frontera compartida"
    )


def _entorno_worker(tmp_path: pathlib.Path):
    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    game_data = tmp_path / "Skyrim" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    game_data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    exe = tmp_path / "LOOT" / "LOOT.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"loot")
    return mo2, game_data, exe


def _manifest_loot_sort(
    tmp_path: pathlib.Path,
    mo2: pathlib.Path,
    game_data: pathlib.Path,
    exe: pathlib.Path,
    *,
    game: str,
    update_masterlist: bool = False,
):
    challenge = build_attestation_challenge(
        data_root=mo2,
        profile="Default",
        physical_data_dir=game_data,
    )
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="loot_sort",
        payload={
            "loot_exe": str(exe),
            "game": game,
            "update_masterlist": update_masterlist,
        },
        timeout_seconds=10,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(),
    )
    return VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=challenge,
        mo2_root=mo2,
        virtual_data_dir=game_data,
        descriptor_path=tmp_path / "descriptor.json",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("internal_id", "cli_id"), sorted(LOOT_CLI_GAME_IDENTIFIERS.items()))
async def test_t3_worker_traduce_con_la_misma_frontera(tmp_path: pathlib.Path, internal_id: str, cli_id: str) -> None:
    """End-to-end worker→runner: para CADA id del mapping compartido, el
    argv final lleva el identificador CLI exacto. Si broker y worker
    divergieran de la frontera, este test lo caza en el argv real."""
    mo2, game_data, exe = _entorno_worker(tmp_path)
    manifest = _manifest_loot_sort(tmp_path, mo2, game_data, exe, game=internal_id)
    captured, fake_exec = _capturando_exec()

    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        execution = await _loot_handler(manifest)

    assert execution.success is True
    argv = captured["args"]
    assert argv[argv.index("--game") + 1] == cli_id
    assert "--auto-sort" in argv
    assert "--update-masterlist" not in argv
    assert "--game-path" in argv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "juego_ajeno",
    ["Skyrim", "Skyrim SE", "SKYRIM SPECIAL EDITION", "Fallout4", "SkyrimSE ", 123, None],
)
async def test_t3_worker_rechaza_ids_fuera_de_la_frontera(tmp_path: pathlib.Path, juego_ajeno: object) -> None:
    """Ids que NO están en la frontera compartida mueren en la validación
    del payload (fail-closed), incluso si LOOT 0.29.x los aceptaría
    ("Skyrim", "Fallout4"): el dominio Sky-Claw es SE/VR y la frontera es
    la única lista que puede crecer (con test actualizado a propósito)."""
    mo2, game_data, exe = _entorno_worker(tmp_path)
    manifest = _manifest_loot_sort(tmp_path, mo2, game_data, exe, game=juego_ajeno)  # type: ignore[arg-type]

    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec") as exec_spy,
        pytest.raises(ValueError, match="payload.game"),
    ):
        await _loot_handler(manifest)
    exec_spy.assert_not_called()


@pytest.mark.asyncio
async def test_t3_worker_sin_game_usa_el_default_compartido(tmp_path: pathlib.Path) -> None:
    """Payload sin campo ``game`` → el default es el MISMO id interno que
    envía el broker (no dos defaults independientes que puedan divergir)."""
    mo2, game_data, exe = _entorno_worker(tmp_path)
    challenge = build_attestation_challenge(
        data_root=mo2,
        profile="Default",
        physical_data_dir=game_data,
    )
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="loot_sort",
        payload={"loot_exe": str(exe), "update_masterlist": False},
        timeout_seconds=10,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=(),
    )
    manifest = VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=challenge,
        mo2_root=mo2,
        virtual_data_dir=game_data,
        descriptor_path=tmp_path / "descriptor.json",
    )
    captured, fake_exec = _capturando_exec()

    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        execution = await _loot_handler(manifest)

    assert execution.success is True
    assert (
        captured["args"][captured["args"].index("--game") + 1]
        == LOOT_CLI_GAME_IDENTIFIERS[DEFAULT_LOOT_INTERNAL_GAME_ID]
    )


# ---------------------------------------------------------------------------
# T4 — argv exacto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t4_argv_exacto_desde_id_interno(tmp_path: pathlib.Path) -> None:
    """Vector completo: ``--game <CLI exacto> --game-path <path>
    --auto-sort`` y nada más. Se afirma el vector, no un membership: un flag
    de más o un orden roto rompería la invocación en el parser de LOOT."""
    exe = tmp_path / "loot.exe"
    exe.touch()
    juego = tmp_path / "Skyrim Special Edition"
    juego.mkdir()
    runner = LOOTRunner(LOOTConfig(loot_exe=exe, game_path=juego, game="SkyrimSE"))
    captured, fake_exec = _capturando_exec()

    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(juego)),
    ):
        result = await runner.sort()

    assert result.success is True
    assert captured["args"] == [
        str(exe),
        "--game",
        "Skyrim Special Edition",
        "--game-path",
        str(juego),
        "--auto-sort",
    ]


# ---------------------------------------------------------------------------
# T5 — ``--update-masterlist`` no reaparece
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t5_update_masterlist_true_nunca_apearece_en_el_argv(tmp_path: pathlib.Path) -> None:
    """``update_masterlist=True`` es un no-op documentado (el flag NO existe
    en LOOT 0.29.x y rompería la invocación completa): el vector final es
    idéntico al caso default, con el identificador CLI traducido."""
    exe = tmp_path / "loot.exe"
    exe.touch()
    juego = tmp_path / "Skyrim"
    juego.mkdir()
    runner = LOOTRunner(LOOTConfig(loot_exe=exe, game_path=juego, game=DEFAULT_LOOT_INTERNAL_GAME_ID))
    captured, fake_exec = _capturando_exec()

    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", return_value=str(juego)),
    ):
        await runner.sort(update_masterlist=True)

    assert captured["args"] == [
        str(exe),
        "--game",
        "Skyrim Special Edition",
        "--game-path",
        str(juego),
        "--auto-sort",
    ]
    assert "--update-masterlist" not in captured["args"]


# ---------------------------------------------------------------------------
# T8 — ``update_masterlist=True``: la intención propaga, el flag NO viaja
# (review FINDING B). Dos contratos distintos congelados en el mismo vector.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8_update_masterlist_true_propaga_hasta_el_runner_y_no_viaja_al_argv(
    tmp_path: pathlib.Path,
) -> None:
    """Contrato 1 — **la intención atraviesa el worker**:
    ``BrokeredLootRunner.sort(update_masterlist=True)`` → payload de
    ``VfsJob`` → ``_loot_handler`` → ``LOOTRunner.sort(update_masterlist=True)``.

    Contrato 2 — **el runner la trata como no-op documentado**: el flag
    ``--update-masterlist`` NO existe en LOOT 0.29.x y **nunca** aparece en
    el argv real de ``LOOT.exe``.

    No se modifica comportamiento productivo: este test solo congela lo que
    la cadena ya hace (si alguien "corrige" el no-op enviando el flag, el
    sort completo se rompería en el parser de QCommandLineParser de LOOT).
    """
    # Contrato 1a: el broker lo pone en el payload del VfsJob.
    bbase = tmp_path / "broker"
    mo2b, datab, lootb = _entorno_broker(bbase)
    broker = _Broker()
    broker_runner = BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2b,
        profile="Default",
        game_data_dir=datab,
        loot_exe=lootb,
        timeout=120,
        mutation_targets=lambda: (),
    )
    await broker_runner.sort(update_masterlist=True)
    job, _ = broker.calls[0]
    assert job.payload["update_masterlist"] is True

    # Contrato 1b: el worker lo recibe del payload y lo pasa al runner.
    wbase = tmp_path / "worker"
    mo2, game_data, exe = _entorno_worker(wbase)
    manifest = _manifest_loot_sort(
        wbase,
        mo2,
        game_data,
        exe,
        game=DEFAULT_LOOT_INTERNAL_GAME_ID,
        update_masterlist=True,
    )
    with patch("sky_claw.local.mo2.vfs_worker.LOOTRunner") as runner_cls:
        instance = runner_cls.return_value
        instance.sort = AsyncMock(return_value=LOOTResult())
        await _loot_handler(manifest)
    instance.sort.assert_awaited_once_with(update_masterlist=True)

    # Contrato 2: el argv real de LOOT.exe NUNCA contiene el flag.
    captured, fake_exec = _capturando_exec()
    with (
        patch("sky_claw.local.loot.cli.asyncio.create_subprocess_exec", fake_exec),
        patch("sky_claw.local.loot.cli.translate_path_if_wsl", side_effect=lambda x: str(x)),
    ):
        execution = await _loot_handler(manifest)

    assert execution.success is True
    argv = captured["args"]
    assert "--update-masterlist" not in argv
    assert argv[argv.index("--game") + 1] == LOOT_CLI_GAME_IDENTIFIERS[DEFAULT_LOOT_INTERNAL_GAME_ID]
    assert "--auto-sort" in argv


# ---------------------------------------------------------------------------
# T6 — el backend productivo NUNCA resuelve al loot\lootcli.exe interno de MO2
# ---------------------------------------------------------------------------


def _entorno_mo2(tmp_path: pathlib.Path):
    install = tmp_path / "ModOrganizer2"
    (install / "loot").mkdir(parents=True)
    internal_cli = install / "loot" / "lootcli.exe"
    internal_cli.write_bytes(b"loot-internal")
    internal_gui = install / "loot" / "LOOT.exe"
    internal_gui.write_bytes(b"loot-internal")
    data = tmp_path / "MO2Data"
    (data / "profiles" / "Default").mkdir(parents=True)
    (data / "profiles" / "Default" / "plugins.txt").write_text("*Skyrim.esm\n", encoding="utf-8")
    game = tmp_path / "Skyrim"
    (game / "Data").mkdir(parents=True)
    standalone = tmp_path / "Tools" / "LOOT" / "LOOT.exe"
    standalone.parent.mkdir(parents=True)
    standalone.write_bytes(b"loot-standalone")
    return install, data, game, standalone, internal_cli, internal_gui


@pytest.mark.asyncio
async def test_t6_factory_rechaza_el_lootcli_interno_de_mo2(tmp_path: pathlib.Path) -> None:
    """El factory fail-closed: ``loot\\lootcli.exe`` dentro de la instancia
    MO2 no construye un runner — se devuelve el guard F8, que lanza
    ``LOOTNotFoundError`` al sortear (nunca un subprocess)."""
    install, data, game, _standalone, _cli, _gui = _entorno_mo2(tmp_path)

    runner = build_vfs_loot_runner(
        broker=object(),
        instance_id="portable-main",
        data_root=data,
        install_root=install,
        game_path=game,
        loot_exe=install / "loot" / "lootcli.exe",
        profile="Default",
    )

    assert isinstance(runner, VfsRequiredLootRunner)
    with pytest.raises(LOOTNotFoundError, match="F8 guard"):
        await runner.sort()


def test_t6_factory_rechaza_el_subarbol_loot_interno_de_mo2(tmp_path: pathlib.Path) -> None:
    """No importa que se llame ``LOOT.exe``: dentro del subárbol
    ``<instancia MO2>\\loot\\`` es la copia que MO2 gestiona (implementation
    detail), no el standalone del operador."""
    install, data, game, _standalone, _cli, internal_gui = _entorno_mo2(tmp_path)

    runner = build_vfs_loot_runner(
        broker=object(),
        instance_id="portable-main",
        data_root=data,
        install_root=install,
        game_path=game,
        loot_exe=internal_gui,
        profile="Default",
    )

    assert isinstance(runner, VfsRequiredLootRunner)


def test_t6_construccion_directa_lanza_value_error(tmp_path: pathlib.Path) -> None:
    """El guard vive en el constructor (fuente única): el site que construye
    ``BrokeredLootRunner`` directo (``loot_service._ensure_loot_runner``)
    queda cubierto sin duplicar la regla en el factory."""
    install, data, game, _standalone, internal_cli, _gui = _entorno_mo2(tmp_path)

    with pytest.raises(ValueError, match="F8 guard"):
        BrokeredLootRunner(
            broker=object(),
            instance_id="portable-main",
            data_root=data,
            install_root=install,
            profile="Default",
            game_data_dir=game / "Data",
            loot_exe=internal_cli,
            timeout=120,
            mutation_targets=lambda: (),
        )


def test_t6_standalone_fuera_del_arbol_de_mo2_sigue_valido(tmp_path: pathlib.Path) -> None:
    """Sin falso positivo: el LOOT.exe standalone del operador (fuera del
    árbol MO2) construye el runner brokered normal."""
    install, data, game, standalone, _cli, _gui = _entorno_mo2(tmp_path)

    runner = build_vfs_loot_runner(
        broker=object(),
        instance_id="portable-main",
        data_root=data,
        install_root=install,
        game_path=game,
        loot_exe=standalone,
        profile="Default",
    )

    assert isinstance(runner, BrokeredLootRunner)


def test_t6_standalone_junto_a_la_instalacion_de_mo2_sigue_valido(tmp_path: pathlib.Path) -> None:
    """Sin falso positivo en la frontera: un standalone del operador en la
    RAÍZ de la instalación de MO2 (``<instancia>\\loot.exe``) NO es el
    internal de MO2 — ese vive en el subárbol ``<instancia>\\loot\\``.
    Documenta el límite exacto del guard (mismo layout que el fixture de
    test_vfs_protocol_v2_split_roots, que debe seguir construyendo runner)."""
    install, data, game, _standalone, _cli, _gui = _entorno_mo2(tmp_path)
    junto_a_mo2 = install / "loot.exe"
    junto_a_mo2.write_bytes(b"loot-standalone")

    runner = build_vfs_loot_runner(
        broker=object(),
        instance_id="portable-main",
        data_root=data,
        install_root=install,
        game_path=game,
        loot_exe=junto_a_mo2,
        profile="Default",
    )

    assert isinstance(runner, BrokeredLootRunner)


# ---------------------------------------------------------------------------
# T6c — guard F8: comparación LÓGICA case-insensitive (review FINDING A).
# El target productivo es Windows: ``loot``/``Loot``/``LOOT``/``lOoT`` son el
# MISMO subárbol. La comparación NO puede depender de la semántica
# case-sensitive del filesystem host (la implementación anterior usaba
# ``relative_to(install_root / "loot")``: un casing distinto escapaba del
# guard). Aquí se congelan los cuatro casings canónicos + los casos límite.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subtree", ["loot", "Loot", "LOOT", "lOoT"])
def test_t6_guard_f8_cualquier_casing_del_subarbol_loot_rechaza(tmp_path: pathlib.Path, subtree: str) -> None:
    """Cada casing del componente ``loot`` identifica el MISMO subárbol
    interno de MO2 (semántica Windows), sin importar el host donde corra el
    test (en POSIX ``Loot`` y ``loot`` son directorios distintos: la
    comparación es del contrato lógico, no del disco)."""
    install = tmp_path / "ModOrganizer2"
    (install / subtree).mkdir(parents=True)
    exe = install / subtree / "lootcli.exe"
    exe.write_bytes(b"loot-internal")
    assert _is_mo2_internal_loot(exe.resolve(), install.resolve()) is True


def test_t6_guard_f8_casing_subarbol_rechaza_aunque_el_exe_no_se_llame_lootcli(
    tmp_path: pathlib.Path,
) -> None:
    """El subárbol interno se rechaza por SUBÁRBOL (aunque el exe se llame
    ``LOOT.exe`` o ``tool.exe``), con cualquier casing del componente."""
    install = tmp_path / "ModOrganizer2"
    for subtree, nombre in (("LOOT", "LOOT.exe"), ("Loot", "tool.exe")):
        # exist_ok: en un FS case-insensitive (NTFS) "LOOT" y "Loot" son el
        # MISMO directorio; la segunda iteración no debe fallar (WinError 183).
        (install / subtree).mkdir(parents=True, exist_ok=True)
        exe = install / subtree / nombre
        exe.write_bytes(b"loot-internal")
        assert _is_mo2_internal_loot(exe.resolve(), install.resolve()) is True, (subtree, nombre)


def test_t6_guard_f8_prefijo_con_casing_distinto_en_la_instalacion_rechaza(
    tmp_path: pathlib.Path,
) -> None:
    """Casos donde la raíz misma tiene distinto casing (``mOdOrGanizer2`` vs
    ``ModOrganizer2``): la comparación componente a componente (casefold)
    sigue reconociendo el subárbol interno — ``relative_to`` case-sensitive
    lo escapaba."""
    install = tmp_path / "ModOrganizer2"
    install_casing = tmp_path / "mOdOrGanizer2"
    (install_casing / "loot").mkdir(parents=True)
    exe = install_casing / "loot" / "lootcli.exe"
    exe.write_bytes(b"loot-internal")
    assert _is_mo2_internal_loot(exe.resolve(), install.resolve()) is True


def test_t6_guard_f8_casing_solo_el_componente_loot_no_el_nombre_del_exe(
    tmp_path: pathlib.Path,
) -> None:
    """Contraparte de falsos positivos: un exe en la RAÍZ de la instalación
    (no en el subárbol) con cualquier casing sigue siendo standalone válido,
    y un standalone fuera del árbol MO2 con subdirectorio llamado ``LOOT``
    tampoco es el internal (el subárbol debe estar BAJO install_root)."""
    install = tmp_path / "ModOrganizer2"
    install.mkdir(parents=True)
    for nombre in ("loot.exe", "Loot.exe", "LOOT.exe"):
        exe = install / nombre
        exe.write_bytes(b"loot-standalone")
        assert _is_mo2_internal_loot(exe.resolve(), install.resolve()) is False, nombre
    externo = tmp_path / "Tools" / "LOOT" / "LOOT.exe"
    externo.parent.mkdir(parents=True)
    externo.write_bytes(b"loot-standalone")
    assert _is_mo2_internal_loot(externo.resolve(), install.resolve()) is False


@pytest.mark.parametrize("subtree", ["loot", "Loot", "LOOT", "lOoT"])
def test_t6_guard_f8_casing_via_constructor_rechaza(tmp_path: pathlib.Path, subtree: str) -> None:
    """A través del guard real (constructor de ``BrokeredLootRunner``):
    ``<instancia>\\<cualquier casing de loot>\\lootcli.exe`` lanza
    ``ValueError`` F8 guard — no solo la función pura."""
    install, data, game, _standalone, _cli, _gui = _entorno_mo2(tmp_path)
    (install / subtree).mkdir(parents=True, exist_ok=True)  # "loot" ya existe
    exe = install / subtree / "lootcli.exe"
    exe.write_bytes(b"loot-internal")

    with pytest.raises(ValueError, match="F8 guard"):
        BrokeredLootRunner(
            broker=object(),
            instance_id="portable-main",
            data_root=data,
            install_root=install,
            profile="Default",
            game_data_dir=game / "Data",
            loot_exe=exe,
            timeout=120,
            mutation_targets=lambda: (),
        )


# ---------------------------------------------------------------------------
# Ancla de coherencia del parser: rc 0 + output vacío sigue siendo "success"
# a nivel de PROCESO (el gate de evidencia física vive en LootSortingService,
# no en el parser — ver tests/test_loot_success_contract.py). Congelado aquí
# para que el fix de --game no desbalance accidentalmente el contrato.
# ---------------------------------------------------------------------------


def test_parser_rc0_vacio_es_exito_de_proceso() -> None:
    result: LOOTResult = LOOTOutputParser.parse(stdout="", stderr="", return_code=0)
    assert result.success is True
    assert result.sorted_plugins == []

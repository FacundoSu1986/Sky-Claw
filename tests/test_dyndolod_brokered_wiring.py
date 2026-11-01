"""E3-R3: cableado productivo del backend brokered de TexGen/DynDOLOD.

``build_brokered_dyndolod_spawn_strategy`` existía pero NADIE lo llamaba: la
composición aceptaba ``dyndolod_spawn_strategy`` y ``SupervisorAgent`` no se lo
pasaba, así que en producción DynDOLOD corría siempre standalone. Estos tests
fijan la propiedad del mecanismo —no un recordatorio de proceso—:

* **El backend lo decide UNA configuración explícita** (``dyndolod_backend``),
  nunca la disponibilidad de un broker. ``standalone`` es el default y deja el
  comportamiento histórico byte a byte.
* **Pedir ``brokered`` jamás degrada a standalone.** Si falta cualquier cosa, o
  cualquier excepción corta la construcción, el resultado es una strategy
  *guard* que falla cerrado en el spawn y en el gate de handoff. Un valor de
  configuración desconocido tampoco cae a standalone.
* **GUI y agente LLM comparten UN servicio** (el del composition root del
  supervisor), así que una sola inyección cubre las dos superficies; el único
  otro constructor del servicio (el preview ``dry_run``) nunca spawnea y queda
  anclado por censo y por orden de ejecución, no por un párrafo.
* **El estado del broker y el de la strategy mueren juntos**: la strategy
  retiene una referencia al broker, así que se limpia en los mismos sitios donde
  se limpia el runner LOOT (hermano directo).
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.local.mo2 import brokered_dyndolod as brokered_mod
from sky_claw.local.mo2.brokered_dyndolod import (
    DYNDOLOD_BACKEND_BROKERED,
    DYNDOLOD_BACKEND_STANDALONE,
    BrokeredDynDOLODProcess,
    BrokeredDynDOLODSpawnStrategy,
    VfsRequiredDynDOLODSpawnStrategy,
    resolve_dyndolod_spawn_strategy,
)
from sky_claw.local.mo2.vfs_attestation import VfsAttestationChallenge, VfsAttestationError
from sky_claw.local.mo2.vfs_broker import VfsBrokerError, VfsExecutionBroker, vfs_instance_id
from sky_claw.local.mo2.vfs_contracts import VfsJob
from sky_claw.local.tools.artifact_digest import TreeDigest
from sky_claw.local.tools.dyndolod_runner import (
    DynDOLODConfig,
    DynDOLODExecutionError,
    DynDOLODRunner,
    ReadinessMode,
    StandaloneDynDOLODSpawnStrategy,
    normalize_dyndolod_spawn_strategy,
    resolve_data_visibility_domain,
)
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
from sky_claw.local.tools.output_targets import HerramientaDynDOLOD, derivar_layout_de_dyndolod
from sky_claw.local.tools.texgen_handoff import (
    HandoffDriftError,
    TexGenHandoffApproval,
    TexGenHandoffRequest,
)

PAQUETE = pathlib.Path(brokered_mod.__file__).resolve().parents[2]  # <repo>/sky_claw
REPO = PAQUETE.parent

# ---------------------------------------------------------------------------
# Dobles y escenario (un perfil MO2 real en disco; sin tocar el rig)
# ---------------------------------------------------------------------------


class _SesionFalsa:
    """Sesión mínima: el contrato que ``BrokeredDynDOLODProcess`` consume."""

    def __init__(self) -> None:
        self.pid = 4321
        self.returncode: int | None = None
        self.confirmed_terminal = False
        self.terminality_unknown = False
        self.cancel_calls = 0

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.returncode = -1

    async def result(self) -> Any:
        return MagicMock(stdout="", stderr="")


class _BrokerFalso:
    """Registra lo que el broker real recibiría; implementa el protocolo del seam."""

    def __init__(self) -> None:
        self.sesion = _SesionFalsa()
        self.aperturas: list[tuple[VfsJob, dict[str, Any]]] = []
        self.sumisiones: list[tuple[VfsJob, dict[str, Any]]] = []

    async def open_session(self, job: VfsJob, *, challenge: VfsAttestationChallenge, **kwargs: Any) -> _SesionFalsa:
        self.aperturas.append((job, {"challenge": challenge, **kwargs}))
        return self.sesion

    async def submit(self, job: VfsJob, *, challenge: VfsAttestationChallenge, **kwargs: Any) -> Any:
        self.sumisiones.append((job, {"challenge": challenge, **kwargs}))
        raise AssertionError("el gate no debía llegar al probe runtime en este escenario")


@dataclasses.dataclass(frozen=True)
class _Escenario:
    install_root: pathlib.Path  # dir de ModOrganizer.exe
    data_root: pathlib.Path  # raíz de DATOS de la instancia (profiles/, mods/, overwrite/)
    mods_dir: pathlib.Path
    overwrite: pathlib.Path
    game_data_dir: pathlib.Path  # <juego>/Data: lo que la herramienta ve bajo USVFS
    external_work_root: pathlib.Path
    texgen_exe: pathlib.Path
    dyndolod_exe: pathlib.Path
    cwd: pathlib.Path
    instance_id: str


def _escenario(tmp_path: pathlib.Path) -> _Escenario:
    """Raíces DISTINTAS a propósito: una confusión entre ellas no puede pasar inadvertida."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    install = tmp_path / "MO2Install"
    install.mkdir()
    data = tmp_path / "MO2Instance"
    perfil = data / "profiles" / "Default"
    perfil.mkdir(parents=True)
    (perfil / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (perfil / "plugins.txt").write_text("*MiMod.esp\n", encoding="utf-8")
    mods = data / "mods"
    mods.mkdir()
    overwrite = data / "overwrite"
    overwrite.mkdir()
    game_data = tmp_path / "Skyrim" / "Data"
    game_data.mkdir(parents=True)
    trabajo = tmp_path / "ExternalWork"
    trabajo.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    texgen = tools / "TexGenx64.exe"
    texgen.write_bytes(b"MZ")
    dyndolod = tools / "DynDOLODx64.exe"
    dyndolod.write_bytes(b"MZ")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return _Escenario(
        install_root=install,
        data_root=data,
        mods_dir=mods,
        overwrite=overwrite,
        game_data_dir=game_data,
        external_work_root=trabajo,
        texgen_exe=texgen,
        dyndolod_exe=dyndolod,
        cwd=cwd,
        instance_id=vfs_instance_id(install),
    )


def _kwargs(esc: _Escenario, broker: Any, **cambios: Any) -> dict[str, Any]:
    """Entradas COMPLETAS y coherentes del resolver; ``cambios`` pisa campo a campo."""
    base: dict[str, Any] = {
        "backend": DYNDOLOD_BACKEND_BROKERED,
        "broker": broker,
        "instance_id": esc.instance_id,
        "profile": "Default",
        "data_root": esc.data_root,
        "mods_dir": esc.mods_dir,
        "install_root": esc.install_root,
        "game_data_dir": esc.game_data_dir,
        "external_work_root": esc.external_work_root,
    }
    base.update(cambios)
    return base


def _desafio() -> VfsAttestationChallenge:
    return VfsAttestationChallenge(
        profile="Default",
        source_mod="CanaryMod",
        relative_path=pathlib.PurePosixPath("canary.txt"),
        sha256="a" * 64,
        profile_fingerprint="b" * 64,
    )


@pytest.fixture
def sin_attestation_de_disco(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """La attestation en disco tiene su suite; acá se aísla para medir el WIRING.

    Devuelve la lista de kwargs con los que se pidió el challenge: es la única
    huella observable del ``Data`` FÍSICO que la strategy usa (el broker sólo ve el virtual).
    """
    pedidos: list[dict[str, Any]] = []

    def _construye(**kwargs: Any) -> VfsAttestationChallenge:
        pedidos.append(kwargs)
        return _desafio()

    monkeypatch.setattr(brokered_mod, "build_attestation_challenge", _construye)
    return pedidos


@pytest.fixture
def sin_subprocess_local(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Cualquier subprocess local del daemon es el fallback que NO debe ocurrir."""
    llamadas: list[tuple[Any, ...]] = []

    async def _prohibido(*args: Any, **_kwargs: Any) -> Any:
        llamadas.append(args)
        raise AssertionError(f"fallback a subprocess local prohibido: {args!r}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _prohibido)
    return llamadas


def _estrategia_brokered(esc: _Escenario, broker: Any, **cambios: Any) -> BrokeredDynDOLODSpawnStrategy:
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, broker, **cambios))
    assert isinstance(resultado, BrokeredDynDOLODSpawnStrategy), (
        f"backend 'brokered' completo debía producir la strategy brokered; produjo {resultado!r}"
    )
    return resultado


async def _spawn(
    estrategia: Any,
    esc: _Escenario,
    herramienta: str = "TexGen",
    **extra: Any,
) -> Any:
    exe = esc.texgen_exe if herramienta == "TexGen" else esc.dyndolod_exe
    return await estrategia.spawn(
        executable=exe,
        args=["-sse"],
        tool_name=herramienta,
        cwd=esc.cwd,
        timeout=60.0,
        **extra,
    )


# ---------------------------------------------------------------------------
# 1. standalone explícito: conserva el comportamiento actual
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valor", [DYNDOLOD_BACKEND_STANDALONE, "  Standalone ", "STANDALONE"])
def test_backend_standalone_explicito_no_construye_strategy_ni_toca_el_broker(valor: str) -> None:
    # Arrange: un broker que EXPLOTA ante cualquier uso, y ninguna raíz.
    broker = MagicMock(spec=[])  # sin atributos: tocarlo lanza AttributeError

    # Act
    resultado = resolve_dyndolod_spawn_strategy(
        backend=valor,
        broker=broker,
        instance_id=None,
        profile=None,
        data_root=None,
        mods_dir=None,
        install_root=None,
        game_data_dir=None,
        external_work_root=None,
    )

    # Assert: None = el camino histórico, con el sensor físico U-01 activo.
    assert resultado is None
    assert isinstance(normalize_dyndolod_spawn_strategy(resultado), StandaloneDynDOLODSpawnStrategy)
    assert resolve_data_visibility_domain(resultado) == "physical"


def test_standalone_deja_el_servicio_con_el_dominio_fisico_historico() -> None:
    # Arrange / Act
    servicio = DynDOLODPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        journal=MagicMock(),
        path_resolver=MagicMock(),
        event_bus=MagicMock(),
        spawn_strategy=resolve_dyndolod_spawn_strategy(
            backend=DYNDOLOD_BACKEND_STANDALONE,
            broker=None,
            instance_id=None,
            profile=None,
            data_root=None,
            mods_dir=None,
            install_root=None,
            game_data_dir=None,
            external_work_root=None,
        ),
    )

    # Assert
    assert isinstance(servicio._spawn_strategy, StandaloneDynDOLODSpawnStrategy)
    assert servicio._data_visibility_domain == "physical"


# ---------------------------------------------------------------------------
# 2. brokered configurado y disponible: inyecta la estrategia correcta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valor", [DYNDOLOD_BACKEND_BROKERED, "  Brokered ", "BROKERED"])
def test_backend_brokered_completo_produce_la_strategy_brokered(tmp_path: pathlib.Path, valor: str) -> None:
    # Arrange
    esc = _escenario(tmp_path)

    # Act
    estrategia = _estrategia_brokered(esc, _BrokerFalso(), backend=valor)

    # Assert: virtual_usvfs (el sensor físico NO aplica) y NO es el standalone.
    assert estrategia.data_visibility_domain == "virtual_usvfs"
    assert not isinstance(estrategia, StandaloneDynDOLODSpawnStrategy)


async def test_la_strategy_porta_las_raices_correctas_y_no_las_confunde(
    tmp_path: pathlib.Path, sin_attestation_de_disco: list[dict[str, Any]]
) -> None:
    # Arrange
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    estrategia = _estrategia_brokered(esc, broker)

    # Act: una sesión por herramienta, como en producción.
    await _spawn(estrategia, esc, "TexGen")
    await _spawn(estrategia, esc, "DynDOLOD")

    # Assert 1: las raíces que viajan al broker/manifiesto firmado.
    (job_tg, kw_tg), (job_dd, kw_dd) = broker.aperturas
    for kw in (kw_tg, kw_dd):
        assert kw["data_root"] == esc.data_root.resolve(), "raíz de DATOS de la instancia MO2"
        assert kw["mods_dir"] == esc.mods_dir.resolve()
        assert kw["install_root"] == esc.install_root.resolve(), "instalación (ModOrganizer.exe)"
        assert kw["virtual_data_dir"] == esc.game_data_dir.resolve(), "Data que la herramienta ve"
    # Assert 2: identidad de instancia y perfil en el job.
    assert (job_tg.instance_id, job_tg.profile, job_tg.tool_id) == (esc.instance_id, "Default", "texgen")
    assert (job_dd.instance_id, job_dd.profile, job_dd.tool_id) == (esc.instance_id, "Default", "dyndolod")
    # Assert 2b: el Data FÍSICO de la attestation es <juego>/Data (no la raíz de datos de MO2).
    for pedido in sin_attestation_de_disco:
        assert pedido["physical_data_dir"] == esc.game_data_dir.resolve()
        assert pedido["data_root"] == esc.data_root.resolve()
        assert pedido["mods_dir"] == esc.mods_dir.resolve()
        assert pedido["profile"] == "Default"
    assert len(sin_attestation_de_disco) == 2, "una attestation NUEVA por spawn (nunca se comparte)"
    # Assert 3: ninguna raíz se confunde con otra (el escenario las hace distintas).
    assert kw_tg["virtual_data_dir"] != esc.overwrite.resolve(), "overwrite NO es el Data virtual"
    assert kw_tg["virtual_data_dir"] != kw_tg["data_root"], "game\\Data NO es la raíz de datos de MO2"
    assert kw_tg["data_root"] != kw_tg["install_root"]


async def test_los_output_roots_son_los_mismos_que_deriva_el_runner(
    tmp_path: pathlib.Path, sin_attestation_de_disco: None
) -> None:
    """El ``-o:`` del runner y el ``mutation_targets`` del job salen de UNA derivación."""
    # Arrange
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    estrategia = _estrategia_brokered(esc, broker)
    config = DynDOLODConfig(
        game_path=esc.game_data_dir.parent,
        mo2_path=esc.install_root,
        mo2_mods_path=esc.mods_dir,
        dyndolod_exe=esc.dyndolod_exe,
        external_work_root=esc.external_work_root,
    )
    assert config.output_layout is not None

    # Act
    await _spawn(estrategia, esc, "TexGen")
    await _spawn(estrategia, esc, "DynDOLOD")

    # Assert: el worker exige `-o:` == único mutation_target; si divergieran, la corrida muere ahí.
    (job_tg, _), (job_dd, _) = broker.aperturas
    layout = derivar_layout_de_dyndolod(external_work_root=esc.external_work_root)
    assert job_tg.mutation_targets == (config.output_layout.raiz_de(HerramientaDynDOLOD.TEXGEN).resolve(),)
    assert job_dd.mutation_targets == (config.output_layout.raiz_de(HerramientaDynDOLOD.DYNDOLOD).resolve(),)
    assert job_tg.mutation_targets[0] != layout.family_root.resolve(), "la familia NUNCA es un output"


# ---------------------------------------------------------------------------
# 3. brokered solicitado sin broker: falla cerrado
# ---------------------------------------------------------------------------


async def test_brokered_sin_broker_devuelve_el_guard_que_falla_cerrado(
    tmp_path: pathlib.Path, sin_subprocess_local: list[tuple[Any, ...]]
) -> None:
    # Arrange
    esc = _escenario(tmp_path)

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, None))

    # Assert 1: es el guard — ni None (= standalone) ni la strategy brokered.
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy)
    efectiva = normalize_dyndolod_spawn_strategy(resultado)  # type: ignore[arg-type]
    assert efectiva is resultado, "la identidad del guard se preserva: no se normaliza a standalone"
    assert resolve_data_visibility_domain(efectiva) == "virtual_usvfs"
    # Assert 2: el spawn falla con el error tipado del dominio y SIN subprocess local.
    with pytest.raises(DynDOLODExecutionError, match="broker"):
        await _spawn(resultado, esc, "TexGen")
    assert sin_subprocess_local == []
    # Assert 3: el gate de handoff bloquea (DynDOLOD no se lanzaría).
    veredicto = await resultado.verify_texgen_handoff(
        TexGenHandoffRequest(
            mod_name="TexGen Output",
            staging=esc.cwd,
            data_dir=esc.game_data_dir,
            expected_profile="Default",
        )
    )
    assert veredicto.verified is False
    assert veredicto.pending_action is None, "es un bloqueo duro, no una acción humana pendiente"


# ---------------------------------------------------------------------------
# 4. instancia MO2 incorrecta: no lanza herramientas
# ---------------------------------------------------------------------------


def test_instancia_distinta_a_la_del_install_root_devuelve_el_guard(tmp_path: pathlib.Path) -> None:
    # Arrange: el broker pertenece a OTRA instalación MO2.
    esc = _escenario(tmp_path)
    otra = tmp_path / "OtraInstalacion"
    otra.mkdir()

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, _BrokerFalso(), instance_id=vfs_instance_id(otra)))

    # Assert
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy)
    assert "instancia" in resultado.reason


async def test_un_broker_de_otra_instancia_no_lanza_ninguna_herramienta(
    tmp_path: pathlib.Path, sin_attestation_de_disco: None
) -> None:
    """Contra el broker REAL: el job de otra instancia muere antes de pedirle nada al bridge."""
    # Arrange: broker real de la instancia A, strategy construida (a mano) para la B.
    esc = _escenario(tmp_path)
    broker = VfsExecutionBroker(
        instance_id="mo2-aaaaaaaaaaaaaaaa",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _ruta: None,
    )
    await broker.start()
    try:
        estrategia = brokered_mod.build_brokered_dyndolod_spawn_strategy(
            broker=broker,
            instance_id="mo2-bbbbbbbbbbbbbbbb",
            profile="Default",
            data_root=esc.data_root,
            mods_dir=esc.mods_dir,
            install_root=esc.install_root,
            physical_data_dir=esc.game_data_dir,
            virtual_data_dir=esc.game_data_dir,
            output_roots={"texgen": esc.external_work_root / "TexGen", "dyndolod": esc.external_work_root / "DynDOLOD"},
        )
        assert estrategia is not None

        # Act / Assert
        with pytest.raises(VfsBrokerError, match="otra instancia"):
            await _spawn(estrategia, esc, "TexGen")
    finally:
        await broker.close()


# ---------------------------------------------------------------------------
# 5. perfil ausente o incoherente: no lanza herramientas
# ---------------------------------------------------------------------------


async def test_perfil_ausente_no_abre_ninguna_sesion(tmp_path: pathlib.Path) -> None:
    # Arrange: el resolver no exige el perfil en disco (puede crearse en la sesión);
    # el SPAWN sí: sin perfil no hay attestation y por lo tanto no hay sesión.
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    estrategia = _estrategia_brokered(esc, broker, profile="Fantasma")

    # Act / Assert
    with pytest.raises(VfsAttestationError):
        await _spawn(estrategia, esc, "TexGen")
    assert broker.aperturas == []


async def test_perfil_incoherente_bloquea_el_gate_y_el_spawn(
    tmp_path: pathlib.Path, sin_attestation_de_disco: None
) -> None:
    # Arrange: el backend corre bajo "Otro"; el dueño del artifact y el servicio, bajo "Default".
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    estrategia = _estrategia_brokered(esc, broker, profile="Otro")
    request = TexGenHandoffRequest(
        mod_name="TexGen Output",
        staging=esc.cwd,
        data_dir=esc.game_data_dir,
        expected_profile="Default",
    )
    aprobacion = TexGenHandoffApproval(
        mod_name="TexGen Output",
        artifact_root=esc.mods_dir / "TexGen Output" / "textures",
        artifact=TreeDigest(digest="d" * 64, files=1, bytes=1),
        data_dir=esc.game_data_dir,
        profile="Default",
        profile_fingerprint="b" * 64,
        canary_relative_path=pathlib.PurePosixPath("textures/x.dds"),
        canary_sha256="a" * 64,
    )

    # Act
    veredicto = await estrategia.verify_texgen_handoff(request)

    # Assert 1: el gate no cruza perfiles y no llega al probe runtime.
    assert veredicto.verified is False
    assert "perfiles" in veredicto.reason
    assert broker.sumisiones == []
    # Assert 2: un approval de otro perfil no abre sesión (anti-TOCTOU).
    with pytest.raises(HandoffDriftError, match="perfil"):
        await _spawn(estrategia, esc, "DynDOLOD", handoff=aprobacion)
    assert broker.aperturas == []


# ---------------------------------------------------------------------------
# 6. raíces incompletas: no permite construir la estrategia
# ---------------------------------------------------------------------------

#: Cada entrada del resolver que el backend brokered NECESITA. Se enumera entera:
#: agregar un parámetro sin decidir qué pasa cuando falta rompe el ancla de abajo.
ENTRADAS_OBLIGATORIAS = (
    "instance_id",
    "profile",
    "data_root",
    "mods_dir",
    "install_root",
    "game_data_dir",
    "external_work_root",
)


def test_las_entradas_obligatorias_son_todo_lo_que_el_resolver_acepta_salvo_broker_y_backend() -> None:
    # Arrange / Act
    parametros = set(inspect.signature(resolve_dyndolod_spawn_strategy).parameters)

    # Assert: igualdad literal (enumerar, no muestrear).
    assert parametros == {"backend", "broker", *ENTRADAS_OBLIGATORIAS}


@pytest.mark.parametrize("faltante", ENTRADAS_OBLIGATORIAS)
@pytest.mark.parametrize("vacio", [None, "", "   "])
def test_una_entrada_faltante_o_vacia_devuelve_el_guard_nombrandola(
    tmp_path: pathlib.Path, faltante: str, vacio: object
) -> None:
    # Arrange
    esc = _escenario(tmp_path)

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, _BrokerFalso(), **{faltante: vacio}))

    # Assert: ni None (standalone) ni una strategy parcial; el motivo nombra QUÉ falta.
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy)
    assert faltante in resultado.reason


# ---------------------------------------------------------------------------
# 8. ninguna excepción provoca fallback silencioso de brokered a standalone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "excepcion",
    [ValueError("x"), OSError("x"), RuntimeError("x"), KeyError("x"), AssertionError("x"), TypeError("x")],
    ids=lambda e: type(e).__name__,
)
def test_una_excepcion_al_construir_devuelve_el_guard_y_no_none(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, excepcion: Exception
) -> None:
    # Arrange: el seam existente explota con cualquier tipo de error.
    esc = _escenario(tmp_path)

    def _explota(**_kw: Any) -> None:
        raise excepcion

    monkeypatch.setattr(brokered_mod, "build_brokered_dyndolod_spawn_strategy", _explota)

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, _BrokerFalso()))

    # Assert
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy)
    assert type(excepcion).__name__ in resultado.reason


def test_el_seam_que_devuelve_none_para_brokered_tambien_es_el_guard(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_brokered_...`` devuelve None con TODO en None: ahí NO se puede degradar en silencio."""
    # Arrange
    esc = _escenario(tmp_path)
    monkeypatch.setattr(brokered_mod, "build_brokered_dyndolod_spawn_strategy", lambda **_kw: None)

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, _BrokerFalso()))

    # Assert
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy)


@pytest.mark.parametrize("valor", ["brokerd", "", "   ", "brokered,standalone", 3, None, ["brokered"], True])
def test_un_backend_desconocido_nunca_cae_a_standalone(tmp_path: pathlib.Path, valor: object) -> None:
    # Arrange
    esc = _escenario(tmp_path)

    # Act
    resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, _BrokerFalso(), backend=valor))

    # Assert: un typo en el config no desactiva la protección en silencio.
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy)
    assert "standalone" in resultado.reason and "brokered" in resultado.reason, "el motivo lista los valores válidos"


def test_brokered_jamas_devuelve_none_en_toda_la_grilla_de_entradas(tmp_path: pathlib.Path) -> None:
    """Enumeración: cada subconjunto de entradas ausentes produce strategy o guard, nunca None."""
    # Arrange
    esc = _escenario(tmp_path)
    nombres = ("broker", *ENTRADAS_OBLIGATORIAS)

    # Act / Assert: 2^8 combinaciones.
    for mascara in range(1 << len(nombres)):
        ausentes = {n for i, n in enumerate(nombres) if mascara & (1 << i)}
        cambios = {n: None for n in ausentes if n != "broker"}
        broker = None if "broker" in ausentes else _BrokerFalso()
        resultado = resolve_dyndolod_spawn_strategy(**_kwargs(esc, broker, **cambios))
        assert resultado is not None, f"None (= standalone) con ausentes={sorted(ausentes)}"
        assert isinstance(resultado, BrokeredDynDOLODSpawnStrategy | VfsRequiredDynDOLODSpawnStrategy)
        if ausentes:
            assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy), sorted(ausentes)
        else:
            assert isinstance(resultado, BrokeredDynDOLODSpawnStrategy)


async def test_un_fallo_del_broker_en_el_spawn_no_hace_fallback_a_subprocess(
    tmp_path: pathlib.Path,
    sin_attestation_de_disco: None,
    sin_subprocess_local: list[tuple[Any, ...]],
) -> None:
    # Arrange: el broker acepta construir la strategy y rechaza abrir la sesión.
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    broker.open_session = AsyncMock(side_effect=VfsBrokerError("bridge caído"))  # type: ignore[method-assign]
    estrategia = _estrategia_brokered(esc, broker)

    # Act / Assert
    with pytest.raises(VfsBrokerError, match="bridge caído"):
        await _spawn(estrategia, esc, "DynDOLOD")
    assert sin_subprocess_local == []


# ---------------------------------------------------------------------------
# 9. los gates de handoff TexGen → DynDOLOD siguen conectados
# ---------------------------------------------------------------------------


def _runner(esc: _Escenario, estrategia: Any) -> DynDOLODRunner:
    return DynDOLODRunner(
        DynDOLODConfig(
            game_path=esc.game_data_dir.parent,
            mo2_path=esc.install_root,
            mo2_mods_path=esc.mods_dir,
            dyndolod_exe=esc.dyndolod_exe,
            external_work_root=esc.external_work_root,
        ),
        readiness=ReadinessMode.DISABLED_FOR_TEST,
        spawn_strategy=estrategia,
    )


async def test_el_runner_consulta_el_gate_de_la_strategy_inyectada(tmp_path: pathlib.Path) -> None:
    # Arrange: la strategy REAL de brokered, espiada en su capability de handoff.
    esc = _escenario(tmp_path)
    estrategia = _estrategia_brokered(esc, _BrokerFalso())
    espia = AsyncMock(wraps=estrategia.verify_texgen_handoff)
    estrategia.verify_texgen_handoff = espia  # type: ignore[method-assign]
    runner = _runner(esc, estrategia)
    request = TexGenHandoffRequest(
        mod_name="TexGen Output",
        staging=esc.cwd,
        data_dir=esc.game_data_dir,
        expected_profile="Otro",  # incoherente a propósito: el gate real debe cortar
    )

    # Act
    veredicto = await runner._verificar_handoff_de_texgen(request)

    # Assert: una sola llamada polimórfica, y el corte del gate real llega al runner.
    espia.assert_awaited_once_with(request)
    assert veredicto.verified is False


async def test_el_guard_corta_el_handoff_y_dyndolod_no_se_lanza(tmp_path: pathlib.Path) -> None:
    # Arrange
    esc = _escenario(tmp_path)
    guard = resolve_dyndolod_spawn_strategy(**_kwargs(esc, None))
    assert isinstance(guard, VfsRequiredDynDOLODSpawnStrategy)
    runner = _runner(esc, guard)

    # Act
    veredicto = await runner._verificar_handoff_de_texgen(
        TexGenHandoffRequest(
            mod_name="TexGen Output",
            staging=esc.cwd,
            data_dir=esc.game_data_dir,
            expected_profile="Default",
        )
    )

    # Assert
    assert veredicto.verified is False
    assert veredicto.pending_action is None


def test_la_strategy_brokered_expone_las_tres_capabilities_del_protocolo(tmp_path: pathlib.Path) -> None:
    # Arrange
    esc = _escenario(tmp_path)
    estrategia = _estrategia_brokered(esc, _BrokerFalso())

    # Act / Assert: spawn y verify son corrutinas; el dominio es un valor conocido.
    assert inspect.iscoroutinefunction(estrategia.spawn)
    assert inspect.iscoroutinefunction(estrategia.verify_texgen_handoff)
    assert estrategia.data_visibility_domain == "virtual_usvfs"


async def test_el_spawn_de_texgen_rechaza_un_approval(tmp_path: pathlib.Path, sin_attestation_de_disco: None) -> None:
    """El approval del handoff solo es válido para DynDOLOD (no se re-aprueba a TexGen)."""
    # Arrange
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    estrategia = _estrategia_brokered(esc, broker)
    aprobacion = TexGenHandoffApproval(
        mod_name="TexGen Output",
        artifact_root=esc.mods_dir / "TexGen Output" / "textures",
        artifact=TreeDigest(digest="d" * 64, files=1, bytes=1),
        data_dir=esc.game_data_dir,
        profile="Default",
    )

    # Act / Assert
    with pytest.raises(ValueError, match="DynDOLOD"):
        await _spawn(estrategia, esc, "TexGen", handoff=aprobacion)
    assert broker.aperturas == []


# ---------------------------------------------------------------------------
# 10. cancelación y terminación conservan los contratos del broker
# ---------------------------------------------------------------------------


async def test_el_proceso_brokered_cancela_solo_por_la_sesion_y_nunca_por_el_so(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    sin_attestation_de_disco: None,
) -> None:
    # Arrange: cualquier kill a nivel SO desde el daemon es un fallo del contrato.
    import os

    import psutil

    def _prohibido(*_a: Any, **_k: Any) -> None:
        raise AssertionError("el daemon no mata el PID del tool remoto")

    monkeypatch.setattr(os, "kill", _prohibido)
    monkeypatch.setattr(psutil.Process, "kill", _prohibido)
    monkeypatch.setattr(psutil.Process, "terminate", _prohibido)
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    proceso = await _spawn(_estrategia_brokered(esc, broker), esc, "DynDOLOD")
    assert isinstance(proceso, BrokeredDynDOLODProcess)

    # Act
    proceso.kill()
    proceso.kill()  # idempotente: una sola cancelación
    codigo = await proceso.wait()
    await proceso.terminate()

    # Assert
    assert proceso.backend_managed is True
    assert proceso.assign_job() is None, "el Job Object es del worker/bridge, no del daemon"
    assert codigo == -1
    assert broker.sesion.cancel_calls == 2, "kill() (una vez) + terminate() (una vez), ambos por la sesión"


async def test_la_terminalidad_de_la_sesion_se_propaga_sin_reinterpretarse(
    tmp_path: pathlib.Path, sin_attestation_de_disco: None
) -> None:
    # Arrange
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()
    broker.sesion.confirmed_terminal = True
    broker.sesion.terminality_unknown = True
    proceso = await _spawn(_estrategia_brokered(esc, broker), esc, "TexGen")

    # Act / Assert
    assert proceso.confirmed_terminal is True
    assert proceso.terminality_unknown is True
    assert proceso.pid == 4321


# ---------------------------------------------------------------------------
# 7. GUI y agente LLM terminan en la MISMA strategy y el MISMO servicio
# ---------------------------------------------------------------------------


@pytest.fixture
def mo2_para_supervisor(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Patrón de ``test_supervisor_construction.py``: los SQLite caen en tmp."""
    monkeypatch.chdir(tmp_path)
    mo2 = tmp_path / "MO2"
    (mo2 / "profiles" / "Default").mkdir(parents=True)
    monkeypatch.setenv("MO2_PATH", str(mo2))
    return mo2


def test_el_supervisor_inyecta_la_strategy_en_el_unico_servicio_de_etapa9(
    tmp_path: pathlib.Path, mo2_para_supervisor: pathlib.Path
) -> None:
    # Arrange
    from sky_claw.app.orchestrator.supervisor import SupervisorAgent
    from sky_claw.app.security.path_validator import PathValidator
    from sky_claw.app_context import AppContext

    esc = _escenario(tmp_path / "esc")
    estrategia = _estrategia_brokered(esc, _BrokerFalso())

    # Act
    supervisor = SupervisorAgent(
        profile_name="Default",
        path_validator=PathValidator(roots=[mo2_para_supervisor, tmp_path]),
        dyndolod_spawn_strategy=estrategia,
    )
    ctx = object.__new__(AppContext)
    ctx.servicio_de_etapa9 = None
    ctx.publicar_servicio_de_etapa9(supervisor.dyndolod_service)  # lo que hace el bootloader

    # Assert: superficie GUI (dispatcher) y superficie agente (ctx.servicio_de_etapa9) → UN servicio.
    servicio = supervisor.dyndolod_service
    assert servicio._spawn_strategy is estrategia
    assert servicio._data_visibility_domain == "virtual_usvfs"
    assert supervisor._dispatcher_dependencies.dyndolod_service is servicio
    assert ctx.servicio_de_etapa9 is servicio


def test_sin_strategy_el_supervisor_conserva_el_servicio_standalone(
    tmp_path: pathlib.Path, mo2_para_supervisor: pathlib.Path
) -> None:
    # Arrange / Act
    from sky_claw.app.orchestrator.supervisor import SupervisorAgent
    from sky_claw.app.security.path_validator import PathValidator

    supervisor = SupervisorAgent(
        profile_name="Default",
        path_validator=PathValidator(roots=[mo2_para_supervisor, tmp_path]),
        dyndolod_spawn_strategy=None,
    )

    # Assert: el default no cambia.
    assert isinstance(supervisor.dyndolod_service._spawn_strategy, StandaloneDynDOLODSpawnStrategy)
    assert supervisor.dyndolod_service._data_visibility_domain == "physical"


def _llamadas_a(archivo: str, nombre: str) -> list[ast.Call]:
    arbol = ast.parse((PAQUETE / archivo).read_text(encoding="utf-8"))
    encontradas: list[ast.Call] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            func = nodo.func
            ident = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if ident == nombre:
                encontradas.append(nodo)
    return encontradas


def _kwarg(llamada: ast.Call, nombre: str) -> str | None:
    for kw in llamada.keywords:
        if kw.arg == nombre:
            return ast.unparse(kw.value)
    return None


def test_la_cadena_productiva_de_la_strategy_esta_completa() -> None:
    """AppContext → bootloader → SupervisorAgent → composición → servicio, sin eslabón ausente."""
    cadena = [
        ("app/gui/_bootloader.py", "SupervisorAgent", "dyndolod_spawn_strategy", "ctx.dyndolod_spawn_strategy"),
        (
            "app/orchestrator/supervisor.py",
            "build_orchestration_composition",
            "dyndolod_spawn_strategy",
            "dyndolod_spawn_strategy",
        ),
        (
            "app/orchestrator/orchestration_composition.py",
            "DynDOLODPipelineService",
            "spawn_strategy",
            "dyndolod_spawn_strategy",
        ),
    ]
    for archivo, llamada, kwarg, valor in cadena:
        llamadas = _llamadas_a(archivo, llamada)
        assert len(llamadas) == 1, f"{archivo}: se esperaba UNA llamada a {llamada}, hay {len(llamadas)}"
        assert _kwarg(llamadas[0], kwarg) == valor, f"{archivo}: {llamada} debe pasar {kwarg}={valor!r}"


def test_el_censo_de_constructores_del_servicio_enumera_quien_recibe_la_strategy() -> None:
    """Todo constructor de producción del servicio: ¿pasa ``spawn_strategy``? Igualdad literal."""
    # Arrange / Act
    encontrados: dict[str, bool] = {}
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                func = nodo.func
                ident = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if ident == "DynDOLODPipelineService":
                    clave = archivo.relative_to(REPO).as_posix()
                    pasa = any(kw.arg == "spawn_strategy" for kw in nodo.keywords)
                    encontrados[clave] = encontrados.get(clave, True) and pasa

    # Assert: el composition root la pasa; el preview NO (y eso se prueba en los dos tests de abajo).
    assert encontrados == {
        "sky_claw/app/orchestrator/orchestration_composition.py": True,
        "sky_claw/app/orchestrator/preview/chain_preview_service.py": False,
    }


def test_el_censo_de_constructores_del_supervisor_enumera_quien_recibe_la_strategy() -> None:
    """Todo ``SupervisorAgent(...)`` de producción: ¿pasa la strategy? Y el que no, ¿es un entrypoint de desarrollo?"""
    # Arrange / Act
    encontrados: dict[str, bool] = {}
    fuera_de_main: list[str] = []
    for archivo in PAQUETE.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        dentro_de_main = {
            id(sub)
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.If) and ast.unparse(nodo.test) == "__name__ == '__main__'"
            for sub in ast.walk(nodo)
        }
        for nodo in ast.walk(arbol):
            if (
                isinstance(nodo, ast.Call)
                and getattr(nodo.func, "id", getattr(nodo.func, "attr", None)) == "SupervisorAgent"
            ):
                clave = archivo.relative_to(REPO).as_posix()
                encontrados[clave] = encontrados.get(clave, True) and any(
                    kw.arg == "dyndolod_spawn_strategy" for kw in nodo.keywords
                )
                if id(nodo) not in dentro_de_main:
                    fuera_de_main.append(clave)

    # Assert: el bootloader productivo la pasa; el otro sitio existe SOLO bajo `if __name__ == "__main__"`
    # (entrypoint de desarrollo sin AppContext: no hay broker ni workspace que inyectar).
    assert encontrados == {
        "sky_claw/app/gui/_bootloader.py": True,
        "sky_claw/app/orchestrator/supervisor.py": False,
    }
    assert fuera_de_main == ["sky_claw/app/gui/_bootloader.py"]


def test_el_preview_solo_ejecuta_el_servicio_en_dry_run() -> None:
    """El único constructor SIN strategy es seguro porque solo corre ``dry_run=True``."""
    # Arrange
    archivo = "app/orchestrator/preview/chain_preview_service.py"
    arbol = ast.parse((PAQUETE / archivo).read_text(encoding="utf-8"))
    ejecuciones: list[ast.Call] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute):
            cadena = ast.unparse(nodo.func)
            if cadena == "self._dyndolod_service.execute":
                ejecuciones.append(nodo)

    # Act / Assert
    assert ejecuciones, "el preview debe seguir ejecutando el servicio (si no, este ancla está obsoleta)"
    for llamada in ejecuciones:
        assert _kwarg(llamada, "dry_run") == "True"


def test_execute_retorna_el_dry_run_antes_de_construir_runner_o_preflight() -> None:
    """Orden de ejecución: el preview no puede alcanzar el spawn ni la policy de dominio del preflight."""
    # Arrange
    ruta = PAQUETE / "local/tools/dyndolod_service.py"
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    execute = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute")

    def _linea_de_llamada(nombre: str) -> int:
        lineas = [
            n.lineno for n in ast.walk(execute) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == nombre
        ]
        assert lineas, f"execute() ya no llama a {nombre}"
        return min(lineas)

    retorno_dry_run = next(
        n.lineno for n in ast.walk(execute) if isinstance(n, ast.If) and ast.unparse(n.test) == "dry_run"
    )

    # Act / Assert
    assert retorno_dry_run < _linea_de_llamada("_ensure_preflight")
    assert retorno_dry_run < _linea_de_llamada("_ensure_runner")


# ---------------------------------------------------------------------------
# AppContext: configuración, resolución y CICLO DE VIDA
# ---------------------------------------------------------------------------


def _mo2_de(esc: _Escenario) -> SimpleNamespace:
    return SimpleNamespace(install_root=esc.install_root, data_root=esc.data_root, mods_dir=esc.mods_dir)


def _resolver_de_appcontext(esc: _Escenario, **cambios: Any) -> Any:
    from sky_claw.app_context import AppContext

    entradas: dict[str, Any] = {
        "local_cfg": SimpleNamespace(dyndolod_backend=DYNDOLOD_BACKEND_BROKERED),
        "broker": _BrokerFalso(),
        "instance_id": esc.instance_id,
        "active_profile": "Default",
        "mo2": _mo2_de(esc),
        "game": esc.game_data_dir.parent,
        "workspace": SimpleNamespace(root=esc.external_work_root),
    }
    entradas.update(cambios)
    return AppContext._resolver_estrategia_de_dyndolod(**entradas)


async def test_appcontext_resuelve_la_strategy_con_las_raices_de_su_arranque(
    tmp_path: pathlib.Path, sin_attestation_de_disco: list[dict[str, Any]]
) -> None:
    # Arrange: mo2 (instalación/datos/mods), el JUEGO (no su Data) y el workspace, como en start_full.
    esc = _escenario(tmp_path)
    broker = _BrokerFalso()

    # Act
    resultado = _resolver_de_appcontext(esc, broker=broker)
    assert isinstance(resultado, BrokeredDynDOLODSpawnStrategy)
    await _spawn(resultado, esc, "DynDOLOD")

    # Assert: lo que realmente llega al broker, no sólo el tipo del resultado.
    ((job, kwargs),) = broker.aperturas
    assert kwargs["install_root"] == esc.install_root.resolve()
    assert kwargs["data_root"] == esc.data_root.resolve()
    assert kwargs["mods_dir"] == esc.mods_dir.resolve()
    assert kwargs["virtual_data_dir"] == esc.game_data_dir.resolve(), "<juego>/Data, no <juego>"
    assert sin_attestation_de_disco[0]["physical_data_dir"] == esc.game_data_dir.resolve()
    layout = derivar_layout_de_dyndolod(external_work_root=esc.external_work_root)
    assert job.mutation_targets == (layout.dyndolod_root.resolve(),)
    assert (job.instance_id, job.profile) == (esc.instance_id, "Default")


def test_appcontext_sin_la_clave_de_config_conserva_standalone(tmp_path: pathlib.Path) -> None:
    # Arrange: un config anterior a esta opción (sin el atributo).
    esc = _escenario(tmp_path)

    # Act
    resultado = _resolver_de_appcontext(esc, local_cfg=SimpleNamespace())

    # Assert
    assert resultado is None


@pytest.mark.parametrize(
    "faltante",
    [
        {"broker": None},
        {"game": None},
        {"workspace": None},
        {"mo2": None},
        {"instance_id": None},
    ],
    ids=lambda d: next(iter(d)),
)
def test_appcontext_con_brokered_y_un_insumo_ausente_devuelve_el_guard(tmp_path: pathlib.Path, faltante: dict) -> None:
    # Arrange
    esc = _escenario(tmp_path)

    # Act
    resultado = _resolver_de_appcontext(esc, **faltante)

    # Assert
    assert isinstance(resultado, VfsRequiredDynDOLODSpawnStrategy)


def test_config_trae_standalone_como_default_y_respeta_lo_persistido(tmp_path: pathlib.Path) -> None:
    # Arrange
    from sky_claw.config import Config

    ruta = tmp_path / "config.toml"

    # Act
    por_defecto = Config(ruta).dyndolod_backend
    ruta.write_text('dyndolod_backend = "brokered"\n', encoding="utf-8")
    persistido = Config(ruta).dyndolod_backend

    # Assert
    assert por_defecto == DYNDOLOD_BACKEND_STANDALONE
    assert persistido == DYNDOLOD_BACKEND_BROKERED


def test_la_clave_editada_a_mano_sobrevive_a_persistir_otro_campo(tmp_path: pathlib.Path) -> None:
    """Merge-on-save: la opción nueva no es una clave que ``Config.save()`` pueda perder o pisar."""
    # Arrange: el operador editó el TOML a mano.
    from sky_claw.config import Config

    ruta = tmp_path / "config.toml"
    ruta.write_text(f'dyndolod_backend = "{DYNDOLOD_BACKEND_BROKERED}"\n', encoding="utf-8")
    cfg_largo = Config(ruta)  # objeto de vida larga con el snapshot "brokered"

    # Act 1: otra escritura legítima persiste sin tocar la clave.
    cfg_largo._data["xedit_exe"] = "C:/x/xedit.exe"
    cfg_largo.save()
    tras_guardar = Config(ruta).dyndolod_backend
    # Act 2: el operador cambia el archivo; el objeto viejo persiste OTRO campo.
    ruta.write_text(ruta.read_text(encoding="utf-8").replace(DYNDOLOD_BACKEND_BROKERED, "standalone"), encoding="utf-8")
    cfg_largo._data["pandora_exe"] = "C:/x/pandora.exe"
    cfg_largo.save()

    # Assert
    assert tras_guardar == DYNDOLOD_BACKEND_BROKERED
    assert Config(ruta).dyndolod_backend == DYNDOLOD_BACKEND_STANDALONE, "no pisa la edición posterior del disco"


async def test_cerrar_el_broker_retira_tambien_la_strategy_que_lo_retiene() -> None:
    # Arrange
    from sky_claw.app_context import AppContext

    ctx = object.__new__(AppContext)
    broker = MagicMock()
    broker.close = AsyncMock()
    ctx.vfs_broker = broker
    ctx.vfs_instance_id = "mo2-x"
    ctx.vfs_loot_runner = MagicMock()
    ctx.dyndolod_spawn_strategy = MagicMock()

    # Act
    await ctx._close_vfs_broker(broker)

    # Assert
    assert ctx.vfs_loot_runner is None
    assert ctx.dyndolod_spawn_strategy is None


def _asignaciones_a_none(funcion: str) -> set[str]:
    arbol = ast.parse((PAQUETE / "app_context.py").read_text(encoding="utf-8"))
    nodo = next(
        n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == funcion
    )
    asignadas: set[str] = set()
    for sub in ast.walk(nodo):
        if (
            isinstance(sub, ast.Assign)
            and isinstance(sub.value, ast.Constant)
            and sub.value.value is None
            and len(sub.targets) == 1
            and isinstance(sub.targets[0], ast.Attribute)
        ):
            asignadas.add(sub.targets[0].attr)
    return asignadas


@pytest.mark.parametrize("funcion", ["_sanitize_full_references", "_close_vfs_broker"])
def test_toda_funcion_que_limpia_el_runner_loot_limpia_tambien_la_strategy(funcion: str) -> None:
    """Hermano directo: el runner LOOT y la strategy dependen del MISMO broker."""
    # Arrange / Act
    asignadas = _asignaciones_a_none(funcion)

    # Assert
    assert "vfs_loot_runner" in asignadas, "premisa del ancla: este sitio ya limpiaba el runner LOOT"
    assert "dyndolod_spawn_strategy" in asignadas


def test_start_full_asigna_la_strategy_desde_el_resolver_del_appcontext() -> None:
    # Arrange
    arbol = ast.parse((PAQUETE / "app_context.py").read_text(encoding="utf-8"))
    inner = next(n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_start_full_inner")

    # Act
    asignaciones = [
        n
        for n in ast.walk(inner)
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and ast.unparse(n.targets[0]) == "self.dyndolod_spawn_strategy"
    ]

    # Assert: una sola, y es EXACTAMENTE la llamada al resolver (no un literal, ni un
    # `None and ...`, ni un default silencioso) con cada entrada tomada de su fuente real.
    assert len(asignaciones) == 1
    valor = asignaciones[0].value
    assert isinstance(valor, ast.Call), f"debe ser una llamada directa, es {ast.unparse(valor)!r}"
    assert ast.unparse(valor.func) == "self._resolver_estrategia_de_dyndolod"
    assert {kw.arg: ast.unparse(kw.value) for kw in valor.keywords} == {
        "local_cfg": "local_cfg",
        "broker": "broker",
        "instance_id": "instance_id",
        "active_profile": "active_profile",
        "mo2": "mo2",
        "game": "configured_game",
        "workspace": "self.dyndolod_workspace",
    }
    # ...y DESPUÉS de resolver el workspace (los output roots salen de su root).
    lineas_workspace = [
        n.lineno
        for n in ast.walk(inner)
        if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "self.dyndolod_workspace"
    ]
    assert lineas_workspace and asignaciones[0].lineno > max(lineas_workspace)

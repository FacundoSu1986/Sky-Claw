"""Tests for the BodySlide/Pandora tool consolidation (obs #187).

The legacy AnimationHub-backed pair (``run_pandora``/``run_bodyslide``) and the
runner-backed pair (``run_pandora_behavior``/``run_bodyslide_batch``) were
consolidated: the canonical tool names now delegate to the M-02/M-03 runners
(unified ``_process`` subprocess handling), resolved lazily from ``local_cfg``
at call time so a same-session ``setup_tools`` install is picked up without
mutable-ref plumbing.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools.bodyslide_runner import BodySlideConfig, BodySlideRunner
from sky_claw.local.tools.pandora_runner import PandoraConfig, PandoraRunner


@pytest.fixture
async def proteccion_pandora(
    tmp_path: pathlib.Path,
) -> AsyncIterator[tuple[DistributedLockManager, FileSnapshotManager]]:
    lock_manager = DistributedLockManager(tmp_path / "pandora_locks.db")
    await lock_manager.initialize()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    try:
        yield lock_manager, FileSnapshotManager(snapshot_dir=snapshots)
    finally:
        await lock_manager.close()


def _make_registry(
    *,
    tmp_path: pathlib.Path,
    local_cfg: object | None = None,
    pandora_runner: object | None = None,
    bodyslide_runner: object | None = None,
    path_validator: object | None = None,
    lock_manager: object | None = None,
    snapshot_manager: object | None = None,
) -> AsyncToolRegistry:
    mo2 = MagicMock()
    mo2.root = tmp_path
    return AsyncToolRegistry(
        registry=MagicMock(),
        mo2=mo2,
        sync_engine=MagicMock(),
        loot_exe=None,
        local_cfg=local_cfg,
        pandora_runner=pandora_runner,
        bodyslide_runner=bodyslide_runner,
        path_validator=path_validator,
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
    )


def _runner_result(**overrides: object) -> MagicMock:
    defaults: dict[str, object] = {
        "success": True,
        "return_code": 0,
        "stdout": "ok",
        "stderr": "",
        "duration_seconds": 1.0,
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


# ---------------------------------------------------------------------------
# Tool surface: 4 -> 2
# ---------------------------------------------------------------------------


def test_duplicate_tool_names_removed(tmp_path: pathlib.Path) -> None:
    """Only the canonical names survive; the never-wired duplicates are gone."""
    reg = _make_registry(tmp_path=tmp_path)
    assert "run_pandora" in reg.tools
    assert "run_bodyslide" in reg.tools
    assert "run_pandora_behavior" not in reg.tools
    assert "run_bodyslide_batch" not in reg.tools


def test_run_bodyslide_advertises_group_params(tmp_path: pathlib.Path) -> None:
    """run_bodyslide exposes the BodySlideBatchParams schema (configurable preset
    group) instead of the legacy hardcoded-preset zero-arg contract."""
    reg = _make_registry(tmp_path=tmp_path)
    schema = reg.tools["run_bodyslide"].input_schema
    assert "group" in schema["properties"]
    assert "output_path" in schema["properties"]
    assert "preset" in schema["properties"]
    assert "build_morphs" in schema["properties"]


def test_los_handlers_del_registry_aceptan_todos_los_campos_del_params_model(
    tmp_path: pathlib.Path,
) -> None:
    """execute() despacha ``td.fn(**validated.model_dump())``. Si el lambda
    no declara un campo del schema, toda invocación vía LLM revienta con
    TypeError — el test que llama ``td.fn(...)`` directo no lo ve.
    """
    reg = _make_registry(tmp_path=tmp_path)
    desalineados: list[tuple[str, set[str]]] = []
    for name, td in reg.tools.items():
        if td.params_model is None:
            continue
        parametros = inspect.signature(td.fn).parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parametros.values()):
            continue
        faltantes = set(td.params_model.model_fields) - set(parametros)
        if faltantes:
            desalineados.append((name, faltantes))
    assert desalineados == []


# ---------------------------------------------------------------------------
# Reenvío real de los parámetros aceptados (hermano del test de arriba)
# ---------------------------------------------------------------------------

#: Lambdas de registro que declaran un parámetro del schema y no lo reenvían al
#: servicio, por nombre de tool -> parámetros ignorados. **Vacío a propósito**:
#: agregar una entrada acá es la decisión explícita que el ancla exige antes de
#: aceptar que un campo que el LLM puede mandar se descarte en silencio.
LAMBDAS_QUE_IGNORAN_UN_PARAMETRO: dict[str, set[str]] = {}

#: TODAS las tools que el registro del agente cablea con ``fn=lambda``, congeladas
#: por igualdad literal. Es el censo completo (hoy ninguna registra con una función
#: con nombre), no una muestra: agregar una tool al registro rompe el ancla hasta
#: que alguien confirme que su lambda reenvía lo que declara.
TOOLS_CON_LAMBDA_EN_EL_REGISTRY: set[str] = {
    "analyze_esp_conflicts",
    "check_load_order",
    "close_game",
    "detect_conflicts",
    "download_mod",
    "generate_bashed_patch",
    "install_mod",
    "install_mod_from_archive",
    "launch_game",
    "preview_mod_installer",
    "resolve_fomod",
    "run_bodyslide",
    "run_loot_sort",
    "run_pandora",
    "run_xedit_script",
    "search_mod",
    "search_nexus",
    "setup_tools",
    "toggle_mod",
    "uninstall_mod",
}

_RAIZ_DEL_REPO = pathlib.Path(__file__).resolve().parent.parent
_MODULO_DEL_REGISTRY = _RAIZ_DEL_REPO / "sky_claw" / "app" / "agent" / "tools" / "__init__.py"


def _parametros_aceptados_y_no_reenviados() -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Enumera por AST los lambdas de ``ToolDescriptor`` que ignoran un parámetro.

    Returns
    -------
    tuple
        ``(ignorados, escaneados, sin_lambda)``: por tool, los parámetros
        declarados en su lambda que no se referencian en el cuerpo; el conjunto de
        tools cuyo ``fn`` es un lambda; y las tools que registran ``fn`` con otra
        forma (función con nombre, bound method…), fuera del alcance del escaneo.
    """
    arbol = ast.parse(_MODULO_DEL_REGISTRY.read_text(encoding="utf-8"))
    ignorados: dict[str, set[str]] = {}
    escaneados: set[str] = set()
    sin_lambda: set[str] = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        if not (isinstance(nodo.func, ast.Name) and nodo.func.id == "ToolDescriptor"):
            continue
        claves = {kw.arg: kw.value for kw in nodo.keywords if kw.arg is not None}
        nombre = claves.get("name")
        fn = claves.get("fn")
        if not (isinstance(nombre, ast.Constant) and isinstance(nombre.value, str)):
            continue
        if not isinstance(fn, ast.Lambda):
            sin_lambda.add(nombre.value)
            continue
        escaneados.add(nombre.value)
        declarados = {argumento.arg for argumento in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
        usados = {
            referencia.id
            for referencia in ast.walk(fn.body)
            if isinstance(referencia, ast.Name) and isinstance(referencia.ctx, ast.Load)
        }
        faltantes = declarados - usados
        if faltantes:
            ignorados[nombre.value] = faltantes
    return ignorados, escaneados, sin_lambda


def test_ningun_lambda_de_registro_acepta_un_parametro_que_no_reenvia() -> None:
    """Ancla AST (enumera, no muestrea): el test de arriba prueba que el lambda
    *acepta* los campos del ``params_model``, no que los *reenvíe* — un lambda
    que los declare y los ignore en el cuerpo pasa igual, y el LLM ve el valor
    descartado sin error (el defecto del lambda de 2 args de ``run_bodyslide``,
    que reventaba con TypeError, era la versión ruidosa de esta misma clase).
    Acá se recorren TODOS los registros del módulo, no sólo el hermano conocido:
    el censo de tools con lambda se congela por igualdad, y una tool registrada de
    otra forma falla en vez de quedar fuera del escaneo en silencio.
    """
    ignorados, escaneados, sin_lambda = _parametros_aceptados_y_no_reenviados()
    assert sin_lambda == set(), (
        "estas tools registran su `fn` con algo que no es un lambda, así que este ancla no "
        f"puede probar el reenvío de sus parámetros: {sorted(sin_lambda)}. Extendé "
        "_parametros_aceptados_y_no_reenviados() para analizar esa forma (o registralas con lambda)."
    )
    assert escaneados == TOOLS_CON_LAMBDA_EN_EL_REGISTRY
    assert ignorados == LAMBDAS_QUE_IGNORAN_UN_PARAMETRO


#: Path relativo -> nombre de las tools que ese módulo registra constructor a
#: constructor con ``ToolDescriptor``. Congelado por igualdad literal: es el censo
#: de superficies de registro de TODO ``sky_claw/``, no la del módulo que hoy
#: conocemos. Si aparece un segundo registro (p.ej. un registro propio del camino
#: GUI, como sospechaba la revisión del PR #637), el ancla rompe acá antes de que
#: el hermano quede desincronizado — el patrón dominante del repo (AGENTS.md,
#: "La regla que más se viola").
SUPERFICIES_DE_REGISTRO: dict[str, set[str]] = {
    "sky_claw/app/agent/tools/__init__.py": TOOLS_CON_LAMBDA_EN_EL_REGISTRY,
}


def _censo_de_registros() -> dict[str, set[str]]:
    """Enumera por AST cada ``ToolDescriptor(name=...)`` de ``sky_claw/``.

    Returns
    -------
    dict
        Path relativo a la raíz del repo -> nombres registrados en ese módulo.
    """
    censo: dict[str, set[str]] = {}
    for archivo in sorted((_RAIZ_DEL_REPO / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            es_descriptor = (isinstance(nodo.func, ast.Name) and nodo.func.id == "ToolDescriptor") or (
                isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "ToolDescriptor"
            )
            if not es_descriptor:
                continue
            nombre = next((kw.value for kw in nodo.keywords if kw.arg == "name"), None)
            if not (isinstance(nombre, ast.Constant) and isinstance(nombre.value, str)):
                # Un registro cuyo ``name`` no es un literal no puede contrastarse
                # contra el censo: se declara con el nombre del módulo para que el
                # assert de igualdad lo muestre en vez de ignorarlo.
                censo.setdefault(archivo.relative_to(_RAIZ_DEL_REPO).as_posix(), set()).add("<name no literal>")
                continue
            relativo = archivo.relative_to(_RAIZ_DEL_REPO).as_posix()
            censo.setdefault(relativo, set()).add(nombre.value)
    return censo


def test_toda_tool_se_registra_en_una_sola_superficie() -> None:
    """Las tools que el LLM puede invocar se cablean en un único módulo.

    El fix de BodySlide que motivó esta ancla aterrizó en
    ``AsyncToolRegistry._register_builtins`` y la revisión preguntó por su hermano
    (un registro paralelo del camino GUI/Telegram que conservara el lambda viejo).
    La respuesta es un censo, no una inspección manual de hoy: cualquier segundo
    registro rompe este test y obliga a cablear las dos superficies a la vez.
    """
    assert _censo_de_registros() == SUPERFICIES_DE_REGISTRO


#: Superficies de EJECUCIÓN de BodySlide, por path relativo a la raíz del repo.
#: Congeladas por igualdad literal. El censo de registros de arriba contesta
#: "¿hay un segundo registro?"; éstas contestan el resto de la sospecha del PR #637
#: ("¿el camino GUI conserva su propio runner y sus propios defaults?"): hoy el
#: runner se importa e instancia en un módulo y se ejecuta en otro, y ningún camino
#: paralelo lo hace por su cuenta — el dispatcher de orquestación despacha
#: *estrategias* por nombre y ninguna construye un ``BodySlideRunner``. Un servicio
#: o estrategia que se cablee su propio runner rompe el ancla hasta que se decida
#: explícitamente. El censo de IMPORTACIONES va por el path del módulo
#: (``bodyslide_runner``) y no por el nombre de la clase, así que un alias
#: (``... import BodySlideRunner as R``) tampoco lo esquiva.
MODULOS_QUE_IMPORTAN_BODYSLIDE_RUNNER: set[str] = {"sky_claw/app/agent/tools/__init__.py"}
MODULOS_QUE_INSTANCIAN_BODYSLIDE_RUNNER: set[str] = {"sky_claw/app/agent/tools/__init__.py"}
MODULOS_QUE_EJECUTAN_BODYSLIDE: set[str] = {"sky_claw/app/agent/tools/system_tools.py"}


def _censo_de_ejecucion_de_bodyslide() -> tuple[set[str], set[str], set[str]]:
    """Enumera por AST quién importa, construye y ejecuta el runner de BodySlide.

    Returns
    -------
    tuple
        ``(importan, instancian, ejecutan)``: paths relativos que importan del
        módulo ``bodyslide_runner``, que llaman ``BodySlideRunner(...)`` y que
        llaman ``.run_batch(...)``.
    """
    importan: set[str] = set()
    instancian: set[str] = set()
    ejecutan: set[str] = set()
    for archivo in sorted((_RAIZ_DEL_REPO / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        relativo = archivo.relative_to(_RAIZ_DEL_REPO).as_posix()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, (ast.Import, ast.ImportFrom)):
                modulo = getattr(nodo, "module", None) or ""
                nombres = [alias.name for alias in nodo.names]
                if modulo.endswith("bodyslide_runner") or any(n.endswith("bodyslide_runner") for n in nombres):
                    importan.add(relativo)
                continue
            if not isinstance(nodo, ast.Call):
                continue
            if isinstance(nodo.func, ast.Name) and nodo.func.id == "BodySlideRunner":
                instancian.add(relativo)
            elif isinstance(nodo.func, ast.Attribute) and nodo.func.attr == "run_batch":
                ejecutan.add(relativo)
    return importan, instancian, ejecutan


def test_bodyslide_se_ejecuta_desde_una_sola_superficie() -> None:
    """Ancla AST (enumera, no muestrea): la tool es el único camino a BodySlide.

    El lambda de 2 args que reventaba sólo importa si TODA corrida pasa por la tool
    cableada arriba: si un segundo camino (GUI, servicio, ritual) instanciara su
    runner con sus propios defaults, el ``TypeError`` se habría arreglado en un
    hermano y el otro seguiría sirviendo defaults silenciosos — la clase de defecto
    dominante del repo (AGENTS.md). El runner viaja como dependencia o se resuelve
    desde ``local_cfg``, así que el censo de importaciones, construcciones y
    ejecuciones se congela: agregar una superficie nueva rompe el test —con el
    censo real en el mensaje— en vez de esquivarlo.
    """
    importan, instancian, ejecutan = _censo_de_ejecucion_de_bodyslide()
    assert importan == MODULOS_QUE_IMPORTAN_BODYSLIDE_RUNNER, f"importadores actuales: {sorted(importan)}"
    assert instancian == MODULOS_QUE_INSTANCIAN_BODYSLIDE_RUNNER, f"constructores actuales: {sorted(instancian)}"
    assert ejecutan == MODULOS_QUE_EJECUTAN_BODYSLIDE, f"ejecutores actuales: {sorted(ejecutan)}"


# ---------------------------------------------------------------------------
# Lazy resolution from local_cfg (mirrors the _run_loot_sort pattern)
# ---------------------------------------------------------------------------


def test_resolver_prefers_injected_runner(tmp_path: pathlib.Path) -> None:
    injected = MagicMock(spec=PandoraRunner)
    reg = _make_registry(tmp_path=tmp_path, pandora_runner=injected)
    assert reg._resolve_pandora_runner() is injected


def test_resolver_returns_none_without_config(tmp_path: pathlib.Path) -> None:
    reg = _make_registry(tmp_path=tmp_path, local_cfg=None)
    assert reg._resolve_pandora_runner() is None
    assert reg._resolve_bodyslide_runner() is None


def test_resolver_returns_none_when_exe_missing(tmp_path: pathlib.Path) -> None:
    cfg = SimpleNamespace(pandora_exe=str(tmp_path / "nope.exe"), bodyslide_exe=None)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)
    assert reg._resolve_pandora_runner() is None


def test_resolver_builds_runner_from_local_cfg(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "Pandora.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=str(exe), bodyslide_exe=None)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)

    runner = reg._resolve_pandora_runner()

    assert isinstance(runner, PandoraRunner)
    assert runner.config.pandora_exe == exe
    assert runner.config.game_path == tmp_path  # mo2.root


# ---------------------------------------------------------------------------
# Sandbox validation of config-supplied exe paths (PR #171 review: Codex P1)
# ---------------------------------------------------------------------------


def test_resolver_validates_exe_against_sandbox(tmp_path: pathlib.Path) -> None:
    """The config-derived exe must pass PathValidator before a runner is built."""
    exe = tmp_path / "Pandora.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=str(exe), bodyslide_exe=None)
    validator = MagicMock()
    validator.validate = MagicMock(return_value=exe)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg, path_validator=validator)

    runner = reg._resolve_pandora_runner()

    assert isinstance(runner, PandoraRunner)
    validator.validate.assert_called_once_with(exe)


def test_resolver_rejects_exe_outside_sandbox(tmp_path: pathlib.Path) -> None:
    """A validator rejection (e.g. symlink escape) must block the launch."""
    exe = tmp_path / "evil.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=str(exe), bodyslide_exe=str(exe))
    validator = MagicMock()
    validator.validate = MagicMock(side_effect=ValueError("outside sandbox"))
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg, path_validator=validator)

    assert reg._resolve_pandora_runner() is None
    assert reg._resolve_bodyslide_runner() is None


@pytest.mark.asyncio
async def test_rejected_exe_surfaces_as_not_configured(tmp_path: pathlib.Path) -> None:
    """End-to-end: a sandbox-rejected exe yields the structured error JSON."""
    exe = tmp_path / "evil.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=str(exe), bodyslide_exe=None)
    validator = MagicMock()
    validator.validate = MagicMock(side_effect=ValueError("outside sandbox"))
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg, path_validator=validator)

    result = json.loads(await reg.tools["run_pandora"].fn())

    assert "not configured" in result["error"]


def test_injected_runner_skips_config_validation(tmp_path: pathlib.Path) -> None:
    """Constructor-injected runners are code-controlled DI — trusted as-is."""
    injected = MagicMock(spec=PandoraRunner)
    validator = MagicMock()
    reg = _make_registry(tmp_path=tmp_path, pandora_runner=injected, path_validator=validator)

    assert reg._resolve_pandora_runner() is injected
    validator.validate.assert_not_called()


# ---------------------------------------------------------------------------
# game_path resolution (PR #171 review: Codex P2)
# ---------------------------------------------------------------------------


def test_game_path_prefers_skyrim_path(tmp_path: pathlib.Path) -> None:
    """Portable-MO2 setups: cwd must be the game dir, not mo2.root."""
    skyrim = tmp_path / "skyrim"
    skyrim.mkdir()
    exe = tmp_path / "BodySlide.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=None, bodyslide_exe=str(exe), skyrim_path=str(skyrim))
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)

    runner = reg._resolve_bodyslide_runner()

    assert isinstance(runner, BodySlideRunner)
    assert runner.config.game_path == skyrim


def test_game_path_falls_back_to_mo2_root(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "BodySlide.exe"
    exe.touch()
    cfg = SimpleNamespace(pandora_exe=None, bodyslide_exe=str(exe), skyrim_path=None)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)

    runner = reg._resolve_bodyslide_runner()

    assert runner.config.game_path == tmp_path  # mo2.root


def test_resolver_picks_up_same_session_install(tmp_path: pathlib.Path) -> None:
    """setup_tools persists the exe path into local_cfg; because resolution
    happens at call time, the very next run_bodyslide call must see it."""
    cfg = SimpleNamespace(pandora_exe=None, bodyslide_exe=None)
    reg = _make_registry(tmp_path=tmp_path, local_cfg=cfg)
    assert reg._resolve_bodyslide_runner() is None

    exe = tmp_path / "BodySlide.exe"
    exe.touch()
    cfg.bodyslide_exe = str(exe)  # what setup_tools does post-install

    runner = reg._resolve_bodyslide_runner()
    assert isinstance(runner, BodySlideRunner)
    assert runner.config.bodyslide_exe == exe


# ---------------------------------------------------------------------------
# End-to-end through the tool descriptors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_pandora_tool_uses_injected_runner(
    tmp_path: pathlib.Path,
    proteccion_pandora: tuple[DistributedLockManager, FileSnapshotManager],
) -> None:
    injected = MagicMock(spec=PandoraRunner)
    game = tmp_path / "game"
    game.mkdir(parents=True)
    injected.config = PandoraConfig(
        pandora_exe=tmp_path / "Pandora" / "Pandora.exe",
        game_path=game,
    )

    async def _run_exitoso() -> MagicMock:
        output = game.resolve() / "Pandora_Output"
        output.mkdir()
        (output / "generado.hkx").write_bytes(b"pandora")
        return _runner_result()

    injected.run_pandora = AsyncMock(side_effect=_run_exitoso)
    lock_manager, snapshot_manager = proteccion_pandora
    reg = _make_registry(
        tmp_path=tmp_path,
        pandora_runner=injected,
        lock_manager=lock_manager,
        snapshot_manager=snapshot_manager,
    )

    result = json.loads(await reg.tools["run_pandora"].fn())

    assert result["success"] is True
    injected.run_pandora.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_bodyslide_tool_forwards_group_and_output(tmp_path: pathlib.Path) -> None:
    """U-04: el destino físico ya no es el ``output_path`` que manda el LLM —
    es el subárbol administrado por grupo (``bodyslide_output_target``). El
    parámetro ``output_path`` se conserva en el contrato de la tool (no rompe
    a callers existentes) pero deja de determinar dónde escribe BodySlide."""
    from sky_claw.local.tools.output_targets import bodyslide_output_target

    game = tmp_path / "game"
    injected = MagicMock(spec=BodySlideRunner)
    injected.config = BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=game)
    injected.run_batch = AsyncMock(return_value=_runner_result())
    reg = _make_registry(
        tmp_path=tmp_path,
        bodyslide_runner=injected,
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
    )

    transaction = MagicMock()
    transaction.lease_lost = False
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "sky_claw.app.db.locks.SnapshotTransactionLock",
        return_value=transaction,
    ):
        result = json.loads(await reg.tools["run_bodyslide"].fn(group="3BA", output_path="out"))

    esperado = bodyslide_output_target(game=game, group="3BA")
    assert result["success"] is True
    # ``build_morphs=True`` por defecto también EN EL BORDE de la tool: es donde
    # el default importa, porque es el que usa el agente LLM cuando no lo pide.
    injected.run_batch.assert_awaited_once_with("3BA", str(esperado), preset=None, build_morphs=True)


@pytest.mark.asyncio
async def test_run_bodyslide_execute_despacha_preset_y_morphs(tmp_path: pathlib.Path) -> None:
    """reg.execute valida BodySlideBatchParams y pasa TODOS los campos al runner.

    El lambda de 2 args reventaba con TypeError('preset') en la superficie LLM.
    """
    from sky_claw.local.tools.output_targets import bodyslide_output_target

    game = tmp_path / "game"
    injected = MagicMock(spec=BodySlideRunner)
    injected.config = BodySlideConfig(bodyslide_exe=tmp_path / "BodySlide.exe", game_path=game)
    injected.run_batch = AsyncMock(return_value=_runner_result())
    reg = _make_registry(
        tmp_path=tmp_path,
        bodyslide_runner=injected,
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
    )

    transaction = MagicMock()
    transaction.lease_lost = False
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "sky_claw.app.db.locks.SnapshotTransactionLock",
        return_value=transaction,
    ):
        result = json.loads(
            await reg.execute("run_bodyslide", {"group": "3BA", "preset": "Zeroed Sliders", "build_morphs": False})
        )

    esperado = bodyslide_output_target(game=game, group="3BA")
    assert result["success"] is True
    # Valores NO default a propósito: probar que el campo llega al runner es lo
    # único que distingue "el lambda acepta el parámetro" de "el lambda lo
    # reenvía" — con los defaults, un lambda que los ignore produce el mismo await.
    injected.run_batch.assert_awaited_once_with("3BA", str(esperado), preset="Zeroed Sliders", build_morphs=False)


@pytest.mark.asyncio
async def test_unconfigured_tools_return_structured_error(tmp_path: pathlib.Path) -> None:
    reg = _make_registry(tmp_path=tmp_path, local_cfg=None)

    pandora = json.loads(await reg.tools["run_pandora"].fn())
    bodyslide = json.loads(await reg.tools["run_bodyslide"].fn())

    assert "not configured" in pandora["error"]
    assert "not configured" in bodyslide["error"]


# ---------------------------------------------------------------------------
# output_path sandboxing in the schema (PR #171 review: Codex P1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside",
        "..\\outside",
        "out/../../outside",
        "C:\\evil",
        "C:evil",  # drive-relative
        "\\\\srv\\share",  # UNC
        "/abs/posix",
        "\\abs\\rootless",
    ],
)
def test_bodyslide_output_path_rejects_escapes(bad_path: str) -> None:
    """output_path feeds BodySlide.exe -o; absolute/traversal forms must fail
    central validation (TASK-011: execute() validates via params_model)."""
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    with pytest.raises(ValueError):
        BodySlideBatchParams(output_path=bad_path)


@pytest.mark.parametrize("good_path", ["meshes", "out/meshes", "calientetools\\output"])
def test_bodyslide_output_path_accepts_relative(good_path: str) -> None:
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    params = BodySlideBatchParams(output_path=good_path)
    assert params.output_path == good_path


# ---------------------------------------------------------------------------
# group sandboxing en el schema (U-04): ahora se usa como componente de ruta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_group", [".", ".."])
def test_bodyslide_group_rejects_bare_dot_segments(bad_group: str) -> None:
    """``group`` pasa a ser el componente único de ruta bajo
    ``BodySlide_Output/<group>`` (U-04, ``output_targets.bodyslide_output_target``).
    ``_SAFE_NAME_PATTERN`` permite '.', así que un valor de solo puntos pasaría el
    patrón de caracteres intacto y, resuelto contra el filesystem, ``..`` subiría
    un nivel — el mismo componente especial que ``output_path`` ya rechaza
    explícitamente (PR #171), acá para un campo que antes no tocaba una ruta."""
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    with pytest.raises(ValueError):
        BodySlideBatchParams(group=bad_group)


@pytest.mark.parametrize("bad_group", ["a/b", "a\\b", "../evil", "CBBE/../../evil"])
def test_bodyslide_group_rejects_path_separators(bad_group: str) -> None:
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    with pytest.raises(ValueError):
        BodySlideBatchParams(group=bad_group)


@pytest.mark.parametrize("good_group", ["CBBE", "3BA", "CBBE Body Physics", "Preset-1.0"])
def test_bodyslide_group_accepts_safe_names(good_group: str) -> None:
    from sky_claw.app.agent.tools.schemas import BodySlideBatchParams

    params = BodySlideBatchParams(group=good_group)
    assert params.group == good_group

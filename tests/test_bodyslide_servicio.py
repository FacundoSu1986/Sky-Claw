"""BodySlide PR 2a — servicio compartido, estados tipados y anclas de superficie.

PR 2a mueve la orquestación de ``run_bodyslide_batch`` (lock, validación del
grupo, move-aside, puente fallo→rollback) a ``BodySlidePipelineService`` SIN
cambiar el comportamiento observable: la batería existente de la tool pasa sin
tocarla. Lo nuevo es:

- el vocabulario cerrado de estados (``EstadoBodySlide``), congelado por
  igualdad literal y enlazado por AST con los estados que el runner emite;
- la barrera productiva ``_CONTENCION_BODYSLIDE_VERIFICADA`` en ``False``;
- el censo de superficies: la GUI de PR 4 tiene que pasar por el MISMO servicio
  (regla de los hermanos, AGENTS.md), así que una superficie nueva rompe el test.

Diseño: ``docs/design/specs/2026-11-23-bodyslide-pr2-concepts-brief.md`` §13.3.
"""

from __future__ import annotations

import ast
import json
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.agent.tools.system_tools import run_bodyslide_batch
from sky_claw.app.db.locks import DistributedLockManager
from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from sky_claw.local.tools import bodyslide_service
from sky_claw.local.tools.bodyslide_service import (
    ESTADOS_RESERVADOS,
    BodySlidePipelineService,
    EstadoBodySlide,
    ResultadoDelServicio,
)
from sky_claw.local.tools.output_targets import bodyslide_output_target

_RAIZ_DEL_REPO = pathlib.Path(__file__).resolve().parents[1]
_SERVICIO = _RAIZ_DEL_REPO / "sky_claw" / "local" / "tools" / "bodyslide_service.py"

# ---------------------------------------------------------------------------
# Vocabulario cerrado
# ---------------------------------------------------------------------------

#: Congelado por igualdad literal: agregar o quitar un estado rompe el test
#: hasta que se decide a qué PR pertenece.
ESTADOS_ESPERADOS: frozenset[str] = frozenset(
    {
        # Contrato y runner de PR 1.
        "SUCCESS",
        "FAILURE",
        "EVIDENCE_INCOMPLETE",
        "CAPABILITY_UNKNOWN",
        "CONFIG_PRECONDITION_FAILED",
        "ARGS_INVALID",
        "STAGING_UNDETERMINED",
        "STAGING_UNVERIFIABLE",
        # Precondiciones de la superficie (PR 2a).
        "RUNNER_NOT_CONFIGURED",
        "PROTECTION_MISSING",
        "LOCK_UNAVAILABLE",
        "EXECUTION_ERROR",
        "RESULT_WITHOUT_STATE",
        # Reservados para 2b–2d (brief §6.4): ningún camino los emite en 2a.
        "PRECONDITION_FAILED",
        "CONFIG_IO_HUNG",
        "WRITE_OUTSIDE_STAGING",
        "STAGING_TAMPERED",
        "CONCURRENT_MODIFICATION",
        "TIMEOUT",
        "CANCELLED",
        "HOLD_TERMINALITY_UNKNOWN",
        "HOLD_LEASE_LOST",
        "RESTORE_FAILED",
        "OUTPUT_MOD_NOT_READY",
        "OUTPUT_MOD_NOT_OWNED",
        "CONFLICT_BETWEEN_GROUPS",
        "PUBLISH_ROLLED_BACK",
        "PUBLISH_ROLLBACK_PENDING",
        "PUBLISHED",
    }
)

RESERVADOS_ESPERADOS: frozenset[str] = frozenset(
    {
        "WRITE_OUTSIDE_STAGING",
        "STAGING_TAMPERED",
        "TIMEOUT",
        "CANCELLED",
        "HOLD_LEASE_LOST",
        "OUTPUT_MOD_NOT_READY",
        "OUTPUT_MOD_NOT_OWNED",
        "CONFLICT_BETWEEN_GROUPS",
        "PUBLISH_ROLLED_BACK",
        "PUBLISH_ROLLBACK_PENDING",
        "PUBLISHED",
    }
)


def test_el_vocabulario_de_estados_esta_congelado() -> None:
    # Arrange / Act
    actuales = {estado.value for estado in EstadoBodySlide}
    # Assert
    assert actuales == ESTADOS_ESPERADOS
    assert {estado.name for estado in EstadoBodySlide} == actuales  # nombre == valor, sin alias


def test_los_reservados_estan_congelados_y_pertenecen_al_vocabulario() -> None:
    assert {estado.value for estado in ESTADOS_RESERVADOS} == RESERVADOS_ESPERADOS
    assert set(EstadoBodySlide) >= ESTADOS_RESERVADOS


def _referencias_a_estados(arbol: ast.AST) -> set[str]:
    """``EstadoBodySlide.X`` usados como valor (no definidos) en el módulo."""
    return {
        nodo.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id == "EstadoBodySlide"
    }


def test_el_servicio_no_emite_estados_reservados_en_2a() -> None:
    # Arrange
    arbol = ast.parse(_SERVICIO.read_text(encoding="utf-8"))
    # Act: las referencias dentro de ESTADOS_RESERVADOS son su definición, no una emisión.
    definicion = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(t, ast.Name) and t.id == "ESTADOS_RESERVADOS"
            for t in (nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target])
        )
    )
    usados_en_la_definicion = _referencias_a_estados(definicion)
    # Se excluye el SUBÁRBOL de la definición por identidad de nodo, no por nombre:
    # restar nombres ocultaba un `return EstadoBodySlide.PUBLISHED` fuera de ella
    # (mutación M2 de la verificación de PR 2a: el ancla quedaba verde).
    nodos_de_la_definicion = {id(n) for n in ast.walk(definicion)}
    usados_fuera = {
        nodo.attr
        for nodo in ast.walk(arbol)
        if id(nodo) not in nodos_de_la_definicion
        and isinstance(nodo, ast.Attribute)
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "EstadoBodySlide"
    }
    # Assert
    assert usados_en_la_definicion == RESERVADOS_ESPERADOS
    assert usados_fuera, "el censo no puede ser vacuo: el servicio emite estados"
    assert not (usados_fuera & RESERVADOS_ESPERADOS), sorted(usados_fuera & RESERVADOS_ESPERADOS)


def _estados_emitidos_por_el_contrato_de_pr1() -> set[str]:
    """Literales de estado que emiten runner y contrato (enumerados por AST).

    Tres formas: ``resultado_de_fallo("X", ...)`` y ``VeredictoContrato("X", ...)``
    (primer argumento) y ``estado="X"`` como keyword.
    """
    encontrados: set[str] = set()
    for nombre in ("bodyslide_runner.py", "bodyslide_headless.py"):
        arbol = ast.parse((_RAIZ_DEL_REPO / "sky_claw" / "local" / "tools" / nombre).read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            func = nodo.func
            nombre_func = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if nombre_func in {"resultado_de_fallo", "VeredictoContrato"} and nodo.args:
                primero = nodo.args[0]
                if isinstance(primero, ast.Constant) and isinstance(primero.value, str):
                    encontrados.add(primero.value)
            for kw in nodo.keywords:
                if kw.arg == "estado" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    encontrados.add(kw.value.value)
    return encontrados


def test_todo_estado_que_emite_pr1_pertenece_al_vocabulario() -> None:
    # Arrange / Act
    emitidos = _estados_emitidos_por_el_contrato_de_pr1()
    # Assert: no vacuo, y ninguno fuera del vocabulario cerrado ni entre los reservados.
    assert {"CAPABILITY_UNKNOWN", "ARGS_INVALID", "FAILURE", "EVIDENCE_INCOMPLETE", "SUCCESS"} <= emitidos
    assert emitidos <= ESTADOS_ESPERADOS, sorted(emitidos - ESTADOS_ESPERADOS)
    assert not (emitidos & RESERVADOS_ESPERADOS)


def test_el_resultado_tipado_exige_exito_si_y_solo_si_success() -> None:
    # Éxito coherente.
    ok = ResultadoDelServicio(estado=EstadoBodySlide.SUCCESS, payload={"success": True, "message": ""})
    assert ok.success is True
    assert ok.message == ""
    # Cualquier otro estado con success=True es incoherente: falla cerrado al construir.
    with pytest.raises(ValueError):
        ResultadoDelServicio(estado=EstadoBodySlide.EVIDENCE_INCOMPLETE, payload={"success": True, "message": ""})
    with pytest.raises(ValueError):
        ResultadoDelServicio(estado=EstadoBodySlide.SUCCESS, payload={"success": False, "message": "x"})
    # Un reservado no puede construirse en 2a.
    with pytest.raises(ValueError):
        ResultadoDelServicio(estado=EstadoBodySlide.PUBLISHED, payload={"success": False, "message": "x"})


# ---------------------------------------------------------------------------
# Barrera productiva
# ---------------------------------------------------------------------------


def test_la_barrera_productiva_esta_en_false() -> None:
    assert bodyslide_service._CONTENCION_BODYSLIDE_VERIFICADA is False


def test_nadie_reasigna_la_barrera_productiva() -> None:
    """La barrera sólo se levanta en el PR de activación (brief §13.2 C5)."""
    asignaciones: list[tuple[str, object]] = []
    for archivo in sorted((_RAIZ_DEL_REPO / "sky_claw").rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            objetivos: list[ast.expr] = []
            valor: ast.expr | None = None
            if isinstance(nodo, ast.Assign):
                objetivos, valor = list(nodo.targets), nodo.value
            elif isinstance(nodo, ast.AnnAssign):
                objetivos, valor = [nodo.target], nodo.value
            for objetivo in objetivos:
                nombre = objetivo.id if isinstance(objetivo, ast.Name) else getattr(objetivo, "attr", None)
                if nombre == "_CONTENCION_BODYSLIDE_VERIFICADA":
                    literal = valor.value if isinstance(valor, ast.Constant) else ast.dump(valor) if valor else None
                    asignaciones.append((archivo.relative_to(_RAIZ_DEL_REPO).as_posix(), literal))
    assert asignaciones == [("sky_claw/local/tools/bodyslide_service.py", False)]


# ---------------------------------------------------------------------------
# Censo de superficies
# ---------------------------------------------------------------------------

#: Hoy hay UNA superficie viva (la tool del agente). PR 4 agrega la GUI y tiene
#: que aparecer acá, construyendo el MISMO servicio.
SUPERFICIES_QUE_CONSTRUYEN_EL_SERVICIO: set[str] = {"sky_claw/app/agent/tools/system_tools.py"}
MODULOS_QUE_IMPORTAN_EL_SERVICIO: set[str] = {"sky_claw/app/agent/tools/system_tools.py"}


def _censo_del_servicio() -> tuple[set[str], set[str]]:
    importan: set[str] = set()
    construyen: set[str] = set()
    for archivo in sorted((_RAIZ_DEL_REPO / "sky_claw").rglob("*.py")):
        relativo = archivo.relative_to(_RAIZ_DEL_REPO).as_posix()
        if relativo == "sky_claw/local/tools/bodyslide_service.py":
            continue
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, (ast.Import, ast.ImportFrom)):
                modulo = getattr(nodo, "module", None) or ""
                nombres = [alias.name for alias in nodo.names]
                if modulo.endswith("bodyslide_service") or any(n.endswith("bodyslide_service") for n in nombres):
                    importan.add(relativo)
            elif isinstance(nodo, ast.Call):
                func = nodo.func
                nombre = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if nombre == "BodySlidePipelineService":
                    construyen.add(relativo)
    return importan, construyen


def test_toda_superficie_de_bodyslide_construye_el_mismo_servicio() -> None:
    importan, construyen = _censo_del_servicio()
    assert construyen == SUPERFICIES_QUE_CONSTRUYEN_EL_SERVICIO, f"constructores actuales: {sorted(construyen)}"
    assert importan == MODULOS_QUE_IMPORTAN_EL_SERVICIO, f"importadores actuales: {sorted(importan)}"


#: Primitivas mutantes que sólo el servicio puede usar en el camino de BodySlide.
_PRIMITIVAS_DEL_SERVICIO = {"SnapshotTransactionLock", "DirectoryRollback", "_BodySlideRunFallidoError"}


def _nombres_usados_en_funcion(archivo: pathlib.Path, funcion: str) -> set[str]:
    arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    nodo_funcion = next(
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and nodo.name == funcion
    )
    nombres: set[str] = set()
    for nodo in ast.walk(nodo_funcion):
        if isinstance(nodo, ast.Name):
            nombres.add(nodo.id)
        elif isinstance(nodo, ast.Attribute):
            nombres.add(nodo.attr)
        elif isinstance(nodo, ast.alias):
            nombres.add(nodo.asname or nodo.name)
    return nombres


def test_la_tool_del_agente_solo_delega_en_el_servicio() -> None:
    """La tool no conserva una copia de la orquestación (lock/move-aside): delega."""
    archivo = _RAIZ_DEL_REPO / "sky_claw" / "app" / "agent" / "tools" / "system_tools.py"
    nombres = _nombres_usados_en_funcion(archivo, "run_bodyslide_batch")
    assert "BodySlidePipelineService" in nombres
    assert not (nombres & _PRIMITIVAS_DEL_SERVICIO), sorted(nombres & _PRIMITIVAS_DEL_SERVICIO)
    assert "run_batch" not in nombres


# ---------------------------------------------------------------------------
# Comportamiento del servicio (AAA)
# ---------------------------------------------------------------------------


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path) -> Any:
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
    d = tmp_path / "snapshots"
    d.mkdir()
    return FileSnapshotManager(snapshot_dir=d)


def _runner(tmp_path: pathlib.Path, **resultado: Any) -> MagicMock:
    runner = MagicMock()
    runner.config = SimpleNamespace(game_path=tmp_path / "game")
    base = {"success": True, "return_code": 0, "stdout": "", "stderr": "", "duration_seconds": 1.0, "message": ""}
    base.update(resultado)
    runner.run_batch = AsyncMock(return_value=SimpleNamespace(**base))
    return runner


async def _ejecutar(runner: Any, lock_manager: Any, snapshot_manager: Any, **kwargs: Any) -> ResultadoDelServicio:
    servicio = BodySlidePipelineService(runner=runner, lock_manager=lock_manager, snapshot_manager=snapshot_manager)
    return await servicio.ejecutar(group=kwargs.pop("group", "CBBE"), preset=None, build_morphs=True, **kwargs)


async def test_runner_ausente_es_runner_not_configured() -> None:
    resultado = await _ejecutar(None, MagicMock(), MagicMock())
    assert resultado.estado is EstadoBodySlide.RUNNER_NOT_CONFIGURED
    assert resultado.success is False
    assert set(resultado.payload) == {"error"}  # forma legacy de esta rama, intacta


async def test_sin_gestores_es_protection_missing_y_no_corre(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    resultado = await _ejecutar(runner, None, None)
    assert resultado.estado is EstadoBodySlide.PROTECTION_MISSING
    assert "lock_manager" in resultado.payload["error"]
    runner.run_batch.assert_not_awaited()


async def test_sin_juego_es_staging_undetermined(lock_manager: Any, snapshot_manager: Any) -> None:
    runner = MagicMock()
    runner.config = SimpleNamespace(game_path=None)
    runner.run_batch = AsyncMock()
    resultado = await _ejecutar(runner, lock_manager, snapshot_manager)
    assert resultado.estado is EstadoBodySlide.STAGING_UNDETERMINED
    assert resultado.payload["success"] is False
    runner.run_batch.assert_not_awaited()


async def test_grupo_win32_invalido_es_args_invalid_sin_move_aside(
    tmp_path: pathlib.Path, lock_manager: Any, snapshot_manager: Any
) -> None:
    # Arrange: ya existe la carpeta del grupo "CBBE" con contenido propio.
    runner = _runner(tmp_path)
    previo = bodyslide_output_target(game=tmp_path / "game", group="CBBE")
    assert previo is not None
    previo.mkdir(parents=True)
    (previo / "viejo.nif").write_bytes(b"viejo")
    # Act: "CBBE." resolvería a la misma carpeta en Win32.
    resultado = await _ejecutar(runner, lock_manager, snapshot_manager, group="CBBE.")
    # Assert
    assert resultado.estado is EstadoBodySlide.ARGS_INVALID
    runner.run_batch.assert_not_awaited()
    assert (previo / "viejo.nif").read_bytes() == b"viejo"
    assert sorted(p.name for p in previo.parent.iterdir()) == ["CBBE"]  # sin .rollback-*


async def test_lock_tomado_es_lock_unavailable(
    tmp_path: pathlib.Path, lock_manager: Any, snapshot_manager: Any
) -> None:
    await lock_manager.acquire_lock("bodyslide-meshes", "otro", ttl=30.0)
    runner = _runner(tmp_path)
    resultado = await _ejecutar(runner, lock_manager, snapshot_manager)
    assert resultado.estado is EstadoBodySlide.LOCK_UNAVAILABLE
    runner.run_batch.assert_not_awaited()


async def test_fallo_del_runner_propaga_su_estado_y_restaura_la_salida_previa(
    tmp_path: pathlib.Path, lock_manager: Any, snapshot_manager: Any
) -> None:
    # Arrange
    previo = bodyslide_output_target(game=tmp_path / "game", group="CBBE")
    assert previo is not None
    previo.mkdir(parents=True)
    (previo / "viejo.nif").write_bytes(b"viejo")

    async def corrida_parcial(group: str, output_path: str, **_k: Any) -> SimpleNamespace:
        pathlib.Path(output_path).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(output_path) / "parcial.nif").write_bytes(b"parcial")
        return SimpleNamespace(
            success=False,
            return_code=0,
            stdout="",
            stderr="",
            duration_seconds=1.0,
            message="sin preset verificado",
            estado="EVIDENCE_INCOMPLETE",
        )

    runner = _runner(tmp_path)
    runner.run_batch = AsyncMock(side_effect=corrida_parcial)
    # Act
    resultado = await _ejecutar(runner, lock_manager, snapshot_manager)
    # Assert
    assert resultado.estado is EstadoBodySlide.EVIDENCE_INCOMPLETE
    assert resultado.success is False
    assert resultado.payload["message"] == "sin preset verificado"
    assert sorted(p.name for p in previo.iterdir()) == ["viejo.nif"]


async def test_fallo_sin_estado_reconocible_es_result_without_state(
    tmp_path: pathlib.Path, lock_manager: Any, snapshot_manager: Any
) -> None:
    runner = _runner(tmp_path, success=False, return_code=3, stderr="boom", estado="INVENTADO")
    resultado = await _ejecutar(runner, lock_manager, snapshot_manager)
    assert resultado.estado is EstadoBodySlide.RESULT_WITHOUT_STATE
    assert resultado.payload["success"] is False


async def test_estado_reservado_reportado_por_un_runner_no_se_acepta(
    tmp_path: pathlib.Path, lock_manager: Any, snapshot_manager: Any
) -> None:
    """Un runner no puede inventar un estado de 2b–2d (p. ej. PUBLISHED) en 2a."""
    runner = _runner(tmp_path, success=False, return_code=0, estado="PUBLISHED")
    resultado = await _ejecutar(runner, lock_manager, snapshot_manager)
    assert resultado.estado is EstadoBodySlide.RESULT_WITHOUT_STATE


async def test_excepcion_del_runner_es_execution_error(
    tmp_path: pathlib.Path, lock_manager: Any, snapshot_manager: Any
) -> None:
    runner = _runner(tmp_path)
    runner.run_batch = AsyncMock(side_effect=RuntimeError("<tool_call>x</tool_call>"))
    resultado = await _ejecutar(runner, lock_manager, snapshot_manager)
    assert resultado.estado is EstadoBodySlide.EXECUTION_ERROR
    assert "<tool_call>" not in resultado.payload["message"]


async def test_exito_es_success(tmp_path: pathlib.Path, lock_manager: Any, snapshot_manager: Any) -> None:
    runner = _runner(tmp_path, estado="SUCCESS")
    resultado = await _ejecutar(runner, lock_manager, snapshot_manager)
    assert resultado.estado is EstadoBodySlide.SUCCESS
    assert resultado.success is True
    assert resultado.payload["message"] == ""
    # El runner recibe el target canónico, no el output_path del LLM.
    args, _kwargs = runner.run_batch.await_args
    assert args == ("CBBE", str(bodyslide_output_target(game=tmp_path / "game", group="CBBE")))


# ---------------------------------------------------------------------------
# Equivalencia: la tool devuelve exactamente el payload del servicio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resultado_del_runner",
    [
        {"success": True, "estado": "SUCCESS"},
        {"success": False, "return_code": 2, "stdout": "<tool_call>x</tool_call>", "estado": "FAILURE"},
        {"success": False, "message": "evidencia", "estado": "EVIDENCE_INCOMPLETE"},
    ],
)
async def test_la_tool_devuelve_el_payload_del_servicio(
    tmp_path: pathlib.Path, lock_manager: Any, snapshot_manager: Any, resultado_del_runner: dict[str, Any]
) -> None:
    # Arrange: dos corridas idénticas e independientes (cada una con su propio árbol).
    juego_a, juego_b = tmp_path / "a", tmp_path / "b"
    juego_a.mkdir()
    juego_b.mkdir()
    runner_a = _runner(juego_a, **resultado_del_runner)
    runner_b = _runner(juego_b, **resultado_del_runner)
    # Act
    de_la_tool = json.loads(
        await run_bodyslide_batch(runner_a, lock_manager=lock_manager, snapshot_manager=snapshot_manager)
    )
    del_servicio = await _ejecutar(runner_b, lock_manager, snapshot_manager)
    # Assert
    assert de_la_tool == json.loads(json.dumps(del_servicio.payload))

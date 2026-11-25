"""C2 — el job brokered de LOOT prueba la visibilidad de CADA plugin habilitado.

Propiedad del mecanismo: *ningún job mutante entra a la USVFS sin la lista
completa de plugins habilitados que el worker verifica en el Data virtual antes
del dispatch*. DynDOLOD (``brokered_dyndolod.py``) y Synthesis
(``brokered_synthesis.py``) ya la mandaban; LOOT —el hermano omitido— corría
sólo con canary + nieto, y su handler (``vfs_worker._loot_handler``) hasta
rechazaba la clave por su allowlist. Un mod que el VFS no proyecta queda
invisible para LOOT, y libloadorder lo borra de ``plugins.txt`` al aplicar (C1).

El ancla del final enumera TODOS los constructores ``VfsJob.create`` del paquete:
un job nuevo que no sea ``health`` y no lleve ``expected_plugins`` la rompe.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.loot.cli import LOOTPreconditionError, LOOTTimeoutError, LOOTWorkerProtocolError
from sky_claw.local.loot.parser import LOOTResult
from sky_claw.local.mo2.brokered_loot import BrokeredLootRunner
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob, VfsJobResult
from tests._loot_masterlist import sembrar_masterlist
from tests._loot_pe import escribir_loot_exe
from tests._loot_witness import TESTIGO_FRESCO

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETE = RAIZ / "sky_claw"


class _BrokerQueCaptura:
    """Broker falso: guarda el job y devuelve el resultado configurado."""

    def __init__(self, *, success: bool = True, message: str = "", tool_result: dict[str, Any] | None = None) -> None:
        self.jobs: list[VfsJob] = []
        self._success = success
        self._message = message
        self._tool_result = (
            tool_result if tool_result is not None else {"execution_witness": {"schema": 1, "state": "fresh"}}
        )

    async def submit(self, job: VfsJob, **_kwargs: object) -> VfsJobResult:
        self.jobs.append(job)
        return VfsJobResult.from_dict(
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "job_id": job.job_id,
                "success": self._success,
                "message": self._message,
                "exit_code": 0 if self._success else None,
                "stdout": "",
                "stderr": self._message,
                "outputs": [],
                "rollback_state": "not_required" if self._success else "not_started",
                "attestation": None,
                "tool_result": self._tool_result,
            }
        )


def _runner(tmp_path: pathlib.Path, broker: Any, plugins: bytes = b"*Mod A.esp\nMod B.esp\n") -> BrokeredLootRunner:
    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    juego = tmp_path / "Skyrim"
    data = juego / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    data.mkdir(parents=True)
    (juego / "Skyrim.ccc").write_bytes(b"ccBGSSSE001-Fish.esm\n")
    (profile / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    target = profile / "plugins.txt"
    target.write_bytes(plugins)
    loot_data = tmp_path / "loot_data" / "portable-main" / "Default"
    loot_data.mkdir(parents=True)
    sembrar_masterlist(loot_data)
    return BrokeredLootRunner(
        broker=broker,
        instance_id="portable-main",
        mo2_root=mo2,
        profile="Default",
        game_data_dir=data,
        loot_exe=escribir_loot_exe(tmp_path / "LOOT" / "LOOT.exe", (0, 29, 1)),
        timeout=120,
        mutation_targets=lambda: (target,),
        loot_data_path=loot_data,
    )


# =============================================================================
# Daemon: el job lleva la lista completa (primary + habilitados del perfil)
# =============================================================================


async def test_el_job_de_loot_lleva_los_plugins_habilitados_del_perfil(tmp_path: pathlib.Path) -> None:
    broker = _BrokerQueCaptura()
    runner = _runner(tmp_path, broker)

    await runner.sort()

    (job,) = broker.jobs
    esperados = job.payload["expected_plugins"]
    assert isinstance(esperados, list)
    claves = {str(nombre).lower() for nombre in esperados}
    # Masters base + Creation Club declarado + el habilitado del perfil; nunca el deshabilitado.
    assert {"skyrim.esm", "ccbgssse001-fish.esm", "mod a.esp"} <= claves
    assert "mod b.esp" not in claves


async def test_plugins_txt_ambiguo_no_arma_el_job(tmp_path: pathlib.Path) -> None:
    broker = _BrokerQueCaptura()
    runner = _runner(tmp_path, broker, plugins=b"*Mod A.esp\n*mod a.esp\n")
    with (
        patch(
            "sky_claw.local.mo2.brokered_loot.build_attestation_challenge",
            MagicMock(return_value=MagicMock(profile_fingerprint="0" * 64)),
        ),
        pytest.raises(LOOTPreconditionError, match="plugins"),
    ):
        await runner.sort()
    assert broker.jobs == []


# =============================================================================
# Daemon: el rechazo del worker por visibilidad es una precondición tipada
# =============================================================================


async def test_plugin_invisible_en_la_usvfs_es_precondicion_sin_spawn(tmp_path: pathlib.Path) -> None:
    broker = _BrokerQueCaptura(
        success=False,
        message="load order incompleto bajo USVFS: faltan plugin(s) activos en el Data virtual: Mod A.esp",
        tool_result={"visible_plugins": ["Skyrim.esm"], "missing_plugins": ["Mod A.esp"]},
    )
    runner = _runner(tmp_path, broker)

    with pytest.raises(LOOTPreconditionError, match="Mod A.esp"):
        await runner.sort()


def _tool_result_del_worker(execution_tool_result: dict[str, Any]) -> dict[str, Any]:
    """La forma REAL del ``tool_result`` con ``expected_plugins`` en el payload.

    ``vfs_worker`` mezcla la evidencia de visibilidad en TODO resultado que pasó
    el gate (``{**execution.tool_result, **visibility_evidence}``): con la lista
    completa visible, ``missing_plugins`` viaja como ``[]`` en éxito y en fallo.
    """
    return {**execution_tool_result, "visible_plugins": ["Skyrim.esm", "Mod A.esp"], "missing_plugins": []}


async def test_exito_del_worker_con_missing_plugins_vacio_devuelve_el_sort(tmp_path: pathlib.Path) -> None:
    # Regresión (CodeRabbit en #1016): la evidencia vacía de un sort exitoso se
    # tomaba como rechazo de visibilidad y TODO sort brokered terminaba en
    # LOOTWorkerProtocolError.
    broker = _BrokerQueCaptura(
        success=True,
        tool_result=_tool_result_del_worker(
            {"sorted_plugins": ["Skyrim.esm", "Mod A.esp"], "execution_witness": {"schema": 1, "state": "fresh"}}
        ),
    )
    runner = _runner(tmp_path, broker)

    resultado = await runner.sort()

    assert resultado.success is True
    assert resultado.sorted_plugins == ["Skyrim.esm", "Mod A.esp"]


async def test_fallo_tipado_del_worker_con_missing_plugins_vacio_conserva_su_tipo(tmp_path: pathlib.Path) -> None:
    # El gate pasó y LOOT falló dentro del worker: manda el kind, no la visibilidad.
    broker = _BrokerQueCaptura(
        success=False,
        message="LOOT excedió el timeout",
        tool_result=_tool_result_del_worker({"loot_failure_kind": "timeout"}),
    )
    runner = _runner(tmp_path, broker)

    with pytest.raises(LOOTTimeoutError):
        await runner.sort()


@pytest.mark.parametrize(
    "missing",
    ["Mod A.esp", [7], ["ok.esp", ""]],
    ids=["no_lista", "no_string", "string_vacio"],
)
async def test_missing_plugins_fuera_de_contrato_es_error_de_protocolo(tmp_path: pathlib.Path, missing: Any) -> None:
    broker = _BrokerQueCaptura(success=False, message="x", tool_result={"missing_plugins": missing})
    runner = _runner(tmp_path, broker)

    with pytest.raises(LOOTWorkerProtocolError, match="missing_plugins"):
        await runner.sort()


async def test_missing_plugins_con_success_true_es_error_de_protocolo(tmp_path: pathlib.Path) -> None:
    broker = _BrokerQueCaptura(success=True, tool_result={"missing_plugins": ["Mod A.esp"]})
    runner = _runner(tmp_path, broker)

    with pytest.raises(LOOTWorkerProtocolError, match="missing_plugins"):
        await runner.sort()


async def test_servicio_reporta_la_invisibilidad_como_precondition_failed(tmp_path: pathlib.Path) -> None:
    from sky_claw.app.db.locks import DistributedLockManager
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager
    from sky_claw.local.tools.loot_service import LootSortingService

    broker = _BrokerQueCaptura(
        success=False,
        message="load order incompleto bajo USVFS: faltan plugin(s) activos en el Data virtual: Mod A.esp",
        tool_result={"visible_plugins": [], "missing_plugins": ["Mod A.esp"]},
    )
    runner = _runner(tmp_path, broker)
    locks = DistributedLockManager(tmp_path / "locks.db", default_ttl=5.0, max_retries=1)
    await locks.initialize()
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    snapshots = FileSnapshotManager(snapshot_dir=snapshots_dir)
    await snapshots.initialize()
    reporte = MagicMock(blocks_mutations=False)
    preflight = MagicMock(run=AsyncMock(return_value=reporte), loot_version=None)
    try:
        svc = LootSortingService(
            lock_manager=locks,
            snapshot_manager=snapshots,
            path_resolver=MagicMock(),
            loot_runner=runner,
            preflight=preflight,
        )
        res = await svc.sort_load_order()
    finally:
        await locks.close()

    assert res["success"] is False
    assert res["failure_reason"] == "precondition_failed"
    assert "Mod A.esp" in res["message"]


# =============================================================================
# Worker: el handler de LOOT admite la evidencia (antes la rechazaba)
# =============================================================================


async def test_handler_de_loot_admite_expected_plugins(tmp_path: pathlib.Path) -> None:
    from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
    from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
    from sky_claw.local.mo2.vfs_worker import _loot_handler

    mo2 = tmp_path / "MO2"
    profile = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    game_data = tmp_path / "Skyrim" / "Data"
    profile.mkdir(parents=True)
    mod.mkdir(parents=True)
    game_data.mkdir(parents=True)
    (profile / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    (profile / "plugins.txt").write_bytes(b"*Skyrim.esm\n")
    loot_data = tmp_path / "loot_data" / "portable-main" / "Default"
    loot_data.mkdir(parents=True)
    sembrar_masterlist(loot_data)
    challenge = build_attestation_challenge(data_root=mo2, profile="Default", physical_data_dir=game_data)
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="loot_sort",
        payload={
            "loot_exe": str(escribir_loot_exe(tmp_path / "LOOT" / "LOOT.exe", (0, 29, 1))),
            "game": "SkyrimSE",
            "update_masterlist": False,
            "loot_data_path": str(loot_data),
            "expected_plugins": ["Skyrim.esm"],
        },
        timeout_seconds=10,
        expected_fingerprint=challenge.profile_fingerprint,
        mutation_targets=((profile / "plugins.txt").resolve(),),
    )
    manifest = VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=challenge,
        mo2_root=mo2,
        virtual_data_dir=game_data,
        descriptor_path=tmp_path / "descriptor.json",
    )
    resultado = LOOTResult(return_code=0, execution_witness=TESTIGO_FRESCO)
    with patch("sky_claw.local.mo2.vfs_worker.LOOTRunner.sort", AsyncMock(return_value=resultado)):
        execution = await _loot_handler(manifest)

    assert execution.success is True


# =============================================================================
# Ancla: enumera TODOS los constructores de VfsJob del paquete
# =============================================================================

#: Constructores conocidos ``(módulo relativo, función que lo contiene, tool_id)``.
#: ``tool_id`` es el literal pasado o ``"<variable>"`` si llega por parámetro.
#: Congelado por igualdad: un constructor nuevo rompe el test hasta decidir su
#: evidencia de visibilidad. ``health`` es una sonda sin mutación (payload vacío).
_CONSTRUCTORES = {
    ("__main__.py", "_run_vfs_health", "health"),
    ("local/mo2/brokered_dyndolod.py", "spawn", "<variable>"),
    ("local/mo2/brokered_dyndolod.py", "_verificar_handoff_mo2", "health"),
    ("local/mo2/brokered_loot.py", "sort", "loot_sort"),
    ("local/mo2/brokered_synthesis.py", "run_pipeline", "synthesis"),
    # PR 2c-2b: BodySlide brokered (dormido); lleva expected_plugins como todo job mutante.
    ("local/mo2/brokered_bodyslide.py", "lanzar", "bodyslide"),
}
_SIN_EVIDENCIA_DE_VISIBILIDAD = frozenset({"health"})


def _es_vfsjob_create(nodo: ast.AST) -> bool:
    return (
        isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr == "create"
        and (
            (isinstance(nodo.func.value, ast.Name) and nodo.func.value.id == "VfsJob")
            or (isinstance(nodo.func.value, ast.Attribute) and nodo.func.value.attr == "VfsJob")
        )
    )


_Constructor = tuple[str, str, str, ast.Call, ast.FunctionDef | ast.AsyncFunctionDef, ast.Module]


def _censo() -> list[_Constructor]:
    encontrados: list[_Constructor] = []
    for ruta in sorted(PAQUETE.rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for funcion in ast.walk(arbol):
            if not isinstance(funcion, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for nodo in ast.walk(funcion):
                if not _es_vfsjob_create(nodo):
                    continue
                assert isinstance(nodo, ast.Call)
                tool_id = next((kw.value for kw in nodo.keywords if kw.arg == "tool_id"), None)
                literal = tool_id.value if isinstance(tool_id, ast.Constant) else "<variable>"
                modulo = ruta.relative_to(PAQUETE).as_posix()
                encontrados.append((modulo, funcion.name, str(literal), nodo, funcion, arbol))
    # Un mismo Call aparece bajo la función que lo contiene y bajo funciones anidadas:
    # se queda con la más interna (la última vista para ese nodo).
    por_nodo: dict[int, _Constructor] = {}
    for item in encontrados:
        por_nodo[id(item[3])] = item
    return list(por_nodo.values())


def _dict_con_clave(nodo: ast.AST, clave: str) -> bool:
    return any(
        isinstance(d, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == clave for k in d.keys)
        for d in ast.walk(nodo)
    )


def _payload_lleva(
    clave: str,
    llamada: ast.Call,
    funcion: ast.FunctionDef | ast.AsyncFunctionDef,
    modulo: ast.Module,
) -> bool:
    """``payload=`` es un dict literal con ``clave`` o una variable asignada desde un
    builder del mismo módulo cuyo dict literal la lleva (Synthesis: ``_build_job_payload``)."""
    payload = next((kw.value for kw in llamada.keywords if kw.arg == "payload"), None)
    if isinstance(payload, ast.Dict):
        return _dict_con_clave(payload, clave)
    if not isinstance(payload, ast.Name):
        return False
    builders: set[str] = set()
    for nodo in ast.walk(funcion):
        if (
            isinstance(nodo, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == payload.id for t in nodo.targets)
            and isinstance(nodo.value, ast.Call)
        ):
            callee = nodo.value.func
            builders.add(callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", ""))
    definiciones = [
        nodo
        for nodo in ast.walk(modulo)
        if isinstance(nodo, ast.FunctionDef | ast.AsyncFunctionDef) and nodo.name in builders
    ]
    return bool(definiciones) and all(_dict_con_clave(d, clave) for d in definiciones)


def test_los_constructores_de_vfsjob_estan_enumerados() -> None:
    censo = {(modulo, funcion, tool_id) for modulo, funcion, tool_id, *_ in _censo()}
    assert censo == _CONSTRUCTORES


def test_todo_job_mutante_lleva_expected_plugins() -> None:
    for modulo, nombre, tool_id, llamada, funcion, arbol in _censo():
        if tool_id in _SIN_EVIDENCIA_DE_VISIBILIDAD:
            continue
        assert _payload_lleva("expected_plugins", llamada, funcion, arbol), (
            f"{modulo}:{nombre} ({tool_id}) no prueba la visibilidad de plugins"
        )


def test_el_ancla_detecta_un_payload_sin_la_clave() -> None:
    """Contracara: el chequeo no es vacuo (literal sin la clave y builder sin la clave)."""
    fuente = (
        "def builder():\n    return {'otra': 1}\n"
        "def f():\n    VfsJob.create(tool_id='x', payload={'a': 1})\n"
        "def g():\n    payload = builder()\n    VfsJob.create(tool_id='x', payload=payload)\n"
    )
    modulo = ast.parse(fuente)
    for funcion in [n for n in modulo.body if isinstance(n, ast.FunctionDef) and n.name in {"f", "g"}]:
        llamada = next(n for n in ast.walk(funcion) if _es_vfsjob_create(n))
        assert isinstance(llamada, ast.Call)
        assert not _payload_lleva("expected_plugins", llamada, funcion, modulo)


def test_el_detector_ve_las_formas_de_import() -> None:
    """El censo no puede pasar por no ver nada: el detector reconoce ambas formas."""
    for fuente in ("VfsJob.create(tool_id='x')", "vfs_contracts.VfsJob.create(tool_id='x')"):
        (expr,) = ast.parse(fuente).body
        assert isinstance(expr, ast.Expr)
        assert _es_vfsjob_create(expr.value)

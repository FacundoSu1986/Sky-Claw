"""CONTRATO ESTRUCTURAL DE PRODUCTORES DE EVIDENCIA ORPHAN — familia gestionada
DynDOLOD/TexGen.

El candidato de PR503 concluyó ``PREMATURE_LIVE_PENDING_ABSORPTION =
REFUTED_BY_SERIALIZATION``, pero su ancla sólo congelaba los callers de los
boundaries absorbentes. Eso NO demuestra que no pueda aparecer OTRO productor
de ActionManifest que nombre un artifact gestionado sin pasar por la región
serializada del lease. Este archivo congela el contrato por el lado que faltaba:

    CAN_CREATE_ORPHAN_EVIDENCE_FOR_DYNDOLOD_MANAGED_ARTIFACT
        ⇒ OWNS_DYNDOLOD_PIPELINE_SERIALIZATION_CONTRACT

Cuatro capas, cada una con su mutante que mata:

1. Inventario AST EXACTO (igualdad literal, estilo ``test_ritual_dispatch``)
   de todos los call sites de ``persist_action_manifest`` /
   ``persist_flight_report`` en ``sky_claw/``. Un productor nuevo en
   CUALQUIER módulo rompe el ancla hasta ser allowlisteado explícitamente
   (M-B1 / M-B6 / M-F7). Un comentario o string con el nombre correcto no
   produce nodos de llamada: no engaña al inventario.

2. Exclusividad de la familia gestionada: ningún productor fuera de
   ``dyndolod_service`` referencia los símbolos ni los literales de la
   familia (mods empaquetados TexGen/DynDOLOD, staging administrado bajo
   ``game/Sky-Claw/DynDOLOD``): no puede NOMBRARLA aunque quiera.

3. Unicidad estructural dentro de ``dyndolod_service``: un solo call site de
   manifest (en ``_emit_action_manifest``) y uno de FlightReport, cada uno
   invocado EXACTAMENTE una vez desde ``execute`` — no puede nacer un segundo
   productor "fuera del lease" en el mismo módulo sin romper el ancla (M-B2).

4. Prueba conductual del orden sobre ``execute()`` REAL (mocks sólo en los
   bordes externos: proceso del runner y lock manager):

       acquire(resource_id="dyndolod-pipeline")
           < begin_transaction
           < persist_action_manifest            (único evento de manifest)
           < primera mutación FS (DirectoryRollback.__aenter__)
           ...
           desmontura de los DirectoryRollback
           < release_lock ÚLTIMO

   M-B3/M-F4 (manifest antes del lock), M-B4/M-F5 (resource_id distinto) y
   M-F6 (comentario/string con el lock correcto) mueren acá: la traza graba
   llamadas reales, no texto.

   CERTIFICACIÓN BAJO LEASE (POST_RELEASE_ARTIFACT_PROVENANCE_RACE, PR #503):
   TODA decisión durable sobre el artifact —resume, reemplazo Y preservación
   needs_deployment— ocurre DENTRO del lease, en el orden
   digest < handoff-deploy < release. La exención que antes toleraba el
   handoff de preservación post-release fue ELIMINADA: congelaba como segura
   la ventana de dos carreras confirmadas (absorción de TX viva y firma de
   bytes de la generación siguiente). El contrato que refuta
   PREMATURE_LIVE_PENDING_ABSORPTION queda ahora respaldado por serialización
   REAL de fabricación Y consumo.

L7 (``lease_lost`` veta el restore) ya tiene ancla AST exhaustiva propia en
``test_dir_rollback.py::_llamadas_sin_veto``; este archivo NO la duplica.
L8 es la conjunción de 1+2.
"""

from __future__ import annotations

import ast
import pathlib
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from sky_claw.app.core.event_bus import CoreEventBus
from sky_claw.app.db.journal import OperationJournal
from sky_claw.app.db.locks import DistributedLockManager, LockInfo
from sky_claw.app.db.snapshot_manager import FileSnapshotManager, SnapshotInfo
from sky_claw.local.tools._dir_rollback import DirectoryRollback
from sky_claw.local.tools.dyndolod_runner import DynDOLODConfig, DynDOLODRunner
from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

_RAIZ_SKY_CLAW = pathlib.Path(__file__).resolve().parent.parent / "sky_claw"

# =============================================================================
# 1. Inventario congelado de productores (igualdad literal)
# =============================================================================

#: Los únicos call sites permitidos de ``persist_action_manifest`` /
#: ``persist_flight_report`` en todo ``sky_claw/``, detectados por AST y
#: congelados por igualdad literal. Agregar un productor sin reescribir esta
#: tabla ROMPE el test a propósito: el nuevo productor debe declarar si es
#: dueño de una región serializada antes de poder fabricar evidencia orphan.
#:
#: ``app/db/journal.py`` aparece por sus DELEGADORES (``StagingJournal.*``):
#: no son productores independientes sino el wrapper transaccional que
#: reenvía al journal real; siguen en la tabla para que cualquier delegador
#: NUEVO también tenga que declararse.
_PRODUCTORES_CONGELADOS: dict[str, list[str]] = {
    "app/db/journal.py": [
        "StagingJournal.persist_action_manifest",
        "StagingJournal.persist_flight_report",
    ],
    "app/orchestrator/tool_strategies/execute_synthesis.py": [
        "ExecuteSynthesisPipelineStrategy._emit_flight_report",
    ],
    "local/tools/dyndolod_service.py": [
        "DynDOLODPipelineService._emit_action_manifest",
        "DynDOLODPipelineService._emit_flight_report",
    ],
    "local/tools/loot_service.py": [
        "LootSortingService._emit_action_manifest",
        "LootSortingService._emit_flight_report",
    ],
    "local/tools/pandora_service.py": [
        "PandoraPipelineService._emit_action_manifest",
        "PandoraPipelineService._emit_flight_report",
    ],
    "local/tools/synthesis_service.py": [
        "SynthesisPipelineService._emit_action_manifest",
    ],
    "local/tools/wrye_bash_service.py": [
        "WryeBashPipelineService._emit_action_manifest",
        "WryeBashPipelineService._emit_flight_report",
    ],
    "local/tools/xedit_service.py": [
        "XEditPipelineService._emit_action_manifest",
        "XEditPipelineService._emit_flight_report",
    ],
}

_METODOS_DE_EMISION = ("persist_action_manifest", "persist_flight_report")


def _call_sites_de_emision(ruta: pathlib.Path, arbol: ast.AST) -> dict[str, set[str]]:
    """Call sites de emisión en UN módulo: {qualname envolvente: {método}}."""
    sitios: dict[str, set[str]] = {}
    pila: list[str] = []

    class V(ast.NodeVisitor):
        def visit_ClassDef(self, n: ast.ClassDef) -> None:
            pila.append(n.name)
            self.generic_visit(n)
            pila.pop()

        def _fn(self, n: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            pila.append(n.name)
            self.generic_visit(n)
            pila.pop()

        def visit_FunctionDef(self, n: ast.FunctionDef) -> None:
            self._fn(n)

        def visit_AsyncFunctionDef(self, n: ast.AsyncFunctionDef) -> None:
            self._fn(n)

        def visit_Call(self, n: ast.Call) -> None:
            f = n.func
            nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if nombre in _METODOS_DE_EMISION:
                clave = ruta.relative_to(_RAIZ_SKY_CLAW).as_posix()
                sitios.setdefault(clave, set()).add(".".join(pila))
            self.generic_visit(n)

    V().visit(arbol)
    return sitios


def test_inventario_de_productores_congelado_por_igualdad_literal() -> None:
    """El conjunto EXACTO de call sites de emisión es el allowlist congelado.

    Detecta por AST (no textual) sobre TODO ``sky_claw/``: un productor nuevo,
    aunque viva en un subpaquete que hoy no emite, aparece acá y el assert de
    igualdad falla hasta que se lo declare —y se le exija ownership— en la
    tabla. Comentarios y strings no crean nodos Call: no pueden falsear el
    inventario.
    """
    observado: dict[str, list[str]] = {}
    for py in sorted(_RAIZ_SKY_CLAW.rglob("*.py")):
        arbol = ast.parse(py.read_text(encoding="utf-8"))
        for clave, qualnames in _call_sites_de_emision(py, arbol).items():
            observado.setdefault(clave, []).extend(sorted(qualnames))

    assert {k: sorted(v) for k, v in observado.items()} == _PRODUCTORES_CONGELADOS


# =============================================================================
# 2. Exclusividad de la familia gestionada
# =============================================================================

#: Símbolos que DERIVAN las identidades de la familia gestionada (Sección 10
#: del protocolo): los mods empaquetados bajo el staging MO2 y el staging
#: crudo administrado. Están definidos en ``dyndolod_runner`` /
#: ``output_targets``; un productor que quiera nombrar la familia tiene que
#: pasar por alguno de estos nombres (directo, con alias de import, o vía
#: atributo).
_SIMBOLOS_FAMILIA = {
    "TEXGEN_MOD_NAME",
    "DYNDOLLOD_MOD_NAME",
    "TEXGEN_OUTPUT_NAME",
    "DYNDLOLOD_OUTPUT_NAME",
    "DYNDOLOD_OUTPUT_ROOT",
    "SKY_CLAW_MANAGED_DIR",
    "dyndolod_output_target",
}

#: Literales exactos de los DOS mods empaquetados. El staging crudo usa
#: nombres genéricos (``textures``, ``DynDOLOD_Output``) que otros contextos
#: pueden mencionar legítimamente, así que NO van en esta lista: la capacidad
#: de nombrar el staging pasa igualmente por los símbolos de arriba.
_LITERALES_FAMILIA = {"TexGen Output", "DynDOLOD Output"}

#: Productores que NO son dueños de la serialización del pipeline: no pueden
#: referenciar la familia ni siquiera para construir un path que la nombre.
_PRODUCTORES_NO_DYNDOLOD = tuple(modulo for modulo in _PRODUCTORES_CONGELADOS if "dyndolod_service" not in modulo)


def _referencias_a_familia(arbol: ast.AST) -> list[str]:
    """Referencias (símbolo resuelto con alias, atributo o literal) a la familia."""
    alias_familia = set(_SIMBOLOS_FAMILIA)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and (nodo.module or "").endswith(("dyndolod_runner", "output_targets")):
            for a in nodo.names:
                if a.name in _SIMBOLOS_FAMILIA:
                    alias_familia.add(a.asname or a.name)

    refs: list[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name) and nodo.id in alias_familia:
            refs.append(nodo.id)
        elif isinstance(nodo, ast.Attribute) and nodo.attr in _SIMBOLOS_FAMILIA:
            refs.append("." + nodo.attr)
        elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and nodo.value in _LITERALES_FAMILIA:
            refs.append(repr(nodo.value))
    return refs


@pytest.mark.parametrize("modulo", _PRODUCTORES_NO_DYNDOLOD)
def test_productor_no_dyndolod_no_referencia_la_familia_gestionada(modulo: str) -> None:
    """Ningún productor ajeno al pipeline puede CONSTRUIR una identidad de la
    familia gestionada: ni importando sus símbolos (con o sin alias) ni
    hardcodeando el literal del mod empaquetado. Sin identidad no hay
    ``target_files`` que canonicalice al artifact, y sin ``target_files`` no
    hay evidencia orphan fabricable fuera del lease."""
    arbol = ast.parse((_RAIZ_SKY_CLAW / modulo).read_text(encoding="utf-8"))
    refs = _referencias_a_familia(arbol)
    assert not refs, (
        f"{modulo} referencia la familia gestionada DynDOLOD/TexGen ({refs}) "
        "sin ser dueño de OWNS_DYNDOLOD_PIPELINE_SERIALIZATION_CONTRACT"
    )


# =============================================================================
# 3. Unicidad estructural dentro de dyndolod_service (M-B2)
# =============================================================================


def test_dyndolod_service_tiene_un_solo_productor_y_vive_en_el_camino_del_lease() -> None:
    """Un único call site de manifest en el módulo (``_emit_action_manifest``),
    invocado EXACTAMENTE una vez, léxicamente dentro de ``execute`` —el método
    dueño del ``AsyncExitStack`` del lease. Un segundo productor "fuera del
    lease" en este mismo archivo rompe cualquiera de las tres igualdades."""
    fuente_modulo = _RAIZ_SKY_CLAW / "local/tools/dyndolod_service.py"
    arbol = ast.parse(fuente_modulo.read_text(encoding="utf-8"))

    sitios = _call_sites_de_emision(fuente_modulo, arbol)["local/tools/dyndolod_service.py"]
    assert [s for s in sitios if s.endswith("_emit_action_manifest")] == [
        "DynDOLODPipelineService._emit_action_manifest"
    ], f"call sites de manifest inesperados en dyndolod_service: {sorted(sitios)}"
    assert [s for s in sitios if s.endswith("_emit_flight_report")] == [
        "DynDOLODPipelineService._emit_flight_report"
    ], f"call sites de FlightReport inesperados en dyndolod_service: {sorted(sitios)}"

    # Rango léxico: la INVOCACIÓN debe vivir dentro de un método; se registra
    # el método dueño de cada llamada a los dos emisores.
    invocaciones: list[tuple[str, str, int]] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(nodo):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("_emit_action_manifest", "_emit_flight_report")
                ):
                    invocaciones.append((nodo.name, sub.func.attr, sub.lineno))

    assert sorted((metodo, emit) for metodo, emit, _ in invocaciones) == [
        ("execute", "_emit_action_manifest"),
        ("execute", "_emit_flight_report"),
    ], f"invocaciones de emisión fuera del dueño del lease: {invocaciones}"


# =============================================================================
# 4. Prueba conductual del orden del lease sobre execute() REAL (L1-L6)
# =============================================================================


def _entorno(tmp_path: pathlib.Path) -> tuple[DynDOLODConfig, DynDOLODRunner]:
    game = tmp_path / "game"
    game.mkdir(parents=True, exist_ok=True)
    exe_dir = tmp_path / "DynDOLOD"
    exe_dir.mkdir(parents=True, exist_ok=True)
    (exe_dir / "DynDOLODx64.exe").touch()
    (exe_dir / "TexGenx64.exe").touch()
    config = DynDOLODConfig(
        game_path=game,
        mo2_path=tmp_path / "MO2",
        mo2_mods_path=tmp_path / "MO2" / "mods",
        dyndolod_exe=exe_dir / "DynDOLODx64.exe",
        texgen_exe=exe_dir / "TexGenx64.exe",
    )
    # data_dir tiene default derivado y el preflight falla rápido si no existe.
    assert config.data_dir is not None
    config.data_dir.mkdir(parents=True, exist_ok=True)
    return config, DynDOLODRunner(config)


class _ProcesoFalsoTexgen:
    """Fake de ``DynDOLODRunner._execute_process`` que produce salida real."""

    def __init__(self, config: DynDOLODConfig) -> None:
        self._staging = config.output_root / DynDOLODRunner.TEXGEN_OUTPUT_NAME
        self._logs = config.dyndolod_exe.parent / "Logs"

    async def __call__(self, *args: object, **kwargs: object) -> tuple[str, str, int, float]:
        self._staging.mkdir(parents=True, exist_ok=True)
        (self._staging / "a.dds").write_bytes(b"GEN-OK")
        log_path = self._logs / "TexGen_SSE_log.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "[00:00:01] Using Output Path: C:\\out\\\n[00:10:00] TexGen completed successfully\n",
            encoding="utf-8",
        )
        return "", "", 0, 1.0


async def test_orden_real_del_lease_en_execute(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L1-L6 grabados como TRAZA de llamadas reales sobre execute():

    - L1: acquire(resource_id="dyndolod-pipeline") es el PRIMER evento y el
      resource_id es EXACTO (M-B4/M-F5 mueren acá);
    - L2/L3: begin_transaction < manifest < primera mutación FS
      (M-B3/M-F4 muere si el manifest se adelanta);
    - L4: los handoffs ocurren mientras el stack aún posee el lease;
    - L6: los DirectoryRollback se desmontan ANTES de soltar el lock;
    - L5: release_lock es el ÚLTIMO evento de la región.

    M-F6: la traza graba INVOCACIONES, no texto — un comentario o un string
    "dyndolod-pipeline" sin acquire real deja la traza sin evento de lock y
    todas las igualdades de orden fallan.
    """
    traza: list[str] = []
    resource_ids: list[str] = []

    config, runner = _entorno(tmp_path)
    journal = OperationJournal(tmp_path / "journal.db")
    await journal.open()
    try:
        # --- bordes externos instrumentados (grabar + delegar) -------------
        # Los originales se capturan ANTES del monkeypatch: tras setattr, el
        # atributo de instancia sombra al método ligado y llamarlo por nombre
        # recursaría en el propio wrapper.
        original_begin = journal.begin_transaction

        async def _begin_grabando(*a: object, **k: object) -> int:
            traza.append("begin-tx")
            return await original_begin(*a, **k)  # type: ignore[arg-type]

        original_manifest = journal.persist_action_manifest

        async def _manifest_grabando(*a: object, **k: object) -> int:
            traza.append("manifest")
            return await original_manifest(*a, **k)  # type: ignore[arg-type]

        original_resume = journal.completar_handoff_de_resume

        async def _resume_grabando(*a: object, **k: object) -> bool:
            traza.append("handoff-resume")
            return await original_resume(*a, **k)  # type: ignore[arg-type]

        original_deploy = journal.crear_handoff_de_deployment

        async def _deploy_grabando(*a: object, **k: object) -> int:
            traza.append("handoff-deploy")
            return await original_deploy(*a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(journal, "begin_transaction", _begin_grabando)
        monkeypatch.setattr(journal, "persist_action_manifest", _manifest_grabando)
        monkeypatch.setattr(journal, "completar_handoff_de_resume", _resume_grabando)
        monkeypatch.setattr(journal, "crear_handoff_de_deployment", _deploy_grabando)

        # Par acquire/get_lock_info COHERENTES: assert_owned compara la fila
        # fresca contra el token de acquire — mismo objeto ⇒ match garantizado.
        lock_info_fija = LockInfo(
            resource_id="dyndolod-pipeline",
            agent_id="dyndolod-pipeline-service",
            acquired_at=time.time(),
            expires_at=time.time() + 3600.0,
        )

        async def _acquire_grabando(**k: object) -> LockInfo:
            traza.append("lock-acquire")
            resource_ids.append(str(k.get("resource_id")))
            return lock_info_fija

        async def _release_grabando(*a: object, **k: object) -> bool:
            traza.append("release")
            return True

        lock_mgr = AsyncMock(spec=DistributedLockManager)
        lock_mgr.acquire_lock = AsyncMock(side_effect=_acquire_grabando)
        lock_mgr.get_lock_info = AsyncMock(return_value=lock_info_fija)
        lock_mgr.release_lock = AsyncMock(side_effect=_release_grabando)

        snap_mgr = AsyncMock(spec=FileSnapshotManager)
        snap_mgr.create_snapshot = AsyncMock(
            side_effect=lambda *a, **k: SnapshotInfo(
                snapshot_id="snap",
                original_path="/x",
                snapshot_path="/s",
                checksum="c",
                size_bytes=1,
                created_at=datetime.now().astimezone(),
            )
        )

        # L3/L6: grabar la ENTRADA (primera mutación FS) y la desmontura REAL
        # de cada move-aside.
        dr_enter_original = DirectoryRollback.__aenter__
        dr_exit_original = DirectoryRollback.__aexit__

        async def _dr_enter_grabando(self: DirectoryRollback) -> DirectoryRollback:
            traza.append("dr-enter")
            return await dr_enter_original(self)  # type: ignore[arg-type]

        async def _dr_exit_grabando(self: DirectoryRollback, *exc: object) -> bool | None:
            resultado = await dr_exit_original(self, *exc)  # type: ignore[arg-type]
            traza.append("dr-exit")
            return resultado

        monkeypatch.setattr(DirectoryRollback, "__aenter__", _dr_enter_grabando)
        monkeypatch.setattr(DirectoryRollback, "__aexit__", _dr_exit_grabando)

        # Certificación del artifact: grabar el digest (corre en worker thread;
        # list.append es thread-safe bajo GIL y el orden relativo alcanza).
        import sky_claw.local.tools.dyndolod_service as dyndolod_service_mod

        digest_original_contrato = dyndolod_service_mod.digest_arbol

        def _digest_grabando(ruta: object) -> object:
            resultado = digest_original_contrato(ruta)
            traza.append("digest")
            return resultado

        monkeypatch.setattr(dyndolod_service_mod, "digest_arbol", _digest_grabando)

        bus = AsyncMock(spec=CoreEventBus)
        bus.publish = AsyncMock()

        svc = DynDOLODPipelineService(
            lock_manager=lock_mgr,
            snapshot_manager=snap_mgr,
            journal=journal,  # type: ignore[arg-type]
            path_resolver=MagicMock(),
            event_bus=bus,
            mo2_profile="Perfil-A",
        )
        svc._runner = runner  # type: ignore[attr-defined]
        monkeypatch.setattr(runner, "_execute_process", _ProcesoFalsoTexgen(config))
        monkeypatch.setattr(runner, "run_dyndolod", AsyncMock())

        # --- corrida productiva completa (texgen → packaging → deployment) -
        res = await svc.execute(preset="Medium", run_texgen=True, create_snapshot=True)
        assert res.get("needs_deployment") is True, res

        # --- aserciones de orden ------------------------------------------
        assert traza[0] == "lock-acquire", f"el lock no fue el primer evento: {traza}"
        assert resource_ids == ["dyndolod-pipeline"], resource_ids
        assert traza.count("lock-acquire") == 1, f"adquisiciones múltiples: {traza}"

        # L2/L3: begin < manifest (único) < primera mutación FS.
        assert traza.count("manifest") == 1, f"emisiones de manifest: {traza}"
        i_tx = traza.index("begin-tx")
        i_man = traza.index("manifest")
        i_dr_primera = traza.index("dr-enter")
        assert i_tx > 0 and i_man > i_tx, f"begin→manifest invertido: {traza}"
        assert i_dr_primera > i_man, f"mutación FS antes del manifest: {traza}"

        i_release_final = len(traza) - 1 - traza[::-1].index("release")
        handoffs_post = [e for i, e in enumerate(traza) if e.startswith("handoff-") and i > i_release_final]
        exits = [i for i, e in enumerate(traza) if e == "dr-exit"]

        # L4 (POST_RELEASE_ARTIFACT_PROVENANCE_RACE, PR #503): NINGÚN boundary
        # durable —resume, reemplazo NI preservación needs_deployment— puede
        # colgar después del release. La certificación completa ocurre dentro
        # del lease; la exención anterior para el handoff de preservación fue
        # ELIMINADA: congelaba como segura la ventana de la carrera.
        assert not handoffs_post, f"boundary durable tras soltar el lease: {traza}"

        # Certificación bajo lease: digest < handoff < release.
        if "digest" in traza:
            i_digest = traza.index("digest")
            assert i_digest < traza.index("handoff-deploy") < i_release_final, f"certificación fuera de orden: {traza}"

        # L6: los DirectoryRollback se desmontan ANTES de soltar el lock.
        assert all(e < i_release_final for e in exits), f"rollback desmontado tras el release: {traza}"

        # L5: el lease se libera ÚLTIMO, sin excepciones.
        assert traza[-1] == "release", f"el lease no se liberó al final: {traza}"
    finally:
        await journal.close()

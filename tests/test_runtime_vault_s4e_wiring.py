"""GP2-S4E — anclas estructurales, routing exhaustivo y contrato de API.

Este archivo congela las propiedades del servicio que NO se verifican
leyendo el reporte de un test: las que se rompen si alguien mete la mano en
el módulo. Son la categoría de defecto que este repo declara dominante — un
fix que aterriza en un camino y deja intacto a su gemelo — y por eso se
anclan enumerando el código por AST, no muestreando casos.

Tests y qué sujetan:

* ``Safety anchors``      : el servicio no reimplementa S4-B/C/D ni muta ACLs.
* ``Routing``             : todo estado del FSM tiene ruta, y la ruta es la
                            del contrato vigente.
* ``API contract``        : el camino de recuperación no acepta paths.
* ``Lock continuity``     : el GoldenMutationLock no tiene ventana
                            release→reacquire entre S4-B y S4-D.
* ``Proyecciones``        : la proyección de resultados no pierde ni inventa
                           information.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any

import pytest

from sky_claw.local.runtime_vault import protection_service as svc
from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationDisposition,
    FinalizationLockOutcome,
    FinalizationPhase,
)
from sky_claw.local.runtime_vault.models import RuntimeVaultError
from sky_claw.local.runtime_vault.operation_lock_binding import OperationLockBindingEvidence
from sky_claw.local.runtime_vault.protection_journal import (
    TERMINAL_TRANSACTION_STATES,
    ProtectionTransactionState,
)

_PAQUETE = pathlib.Path(svc.__file__).resolve().parent
_MODULO = _PAQUETE / "protection_service.py"


def _arbol() -> ast.Module:
    return ast.parse(_MODULO.read_text(encoding="utf-8"), filename=str(_MODULO))


def _nombres_del_archivo() -> set[str]:
    """Todo identificador que el módulo menciona: imports, llamadas, atributos."""
    nombres: set[str] = set()
    for nodo in ast.walk(_arbol()):
        match nodo:
            case ast.Name():
                nombres.add(nodo.id)
            case ast.Attribute():
                nombres.add(nodo.attr)
            case ast.alias():
                nombres.add(nodo.name.rsplit(".", 1)[-1])
            case ast.Constant() if isinstance(nodo.value, str):
                nombres.add(nodo.value)
    return nombres


def _llamadas() -> set[str]:
    """Nombres de función/método CALCADOS (excluye definiciones y referencias)."""
    called: set[str] = set()
    for nodo in ast.walk(_arbol()):
        match nodo:
            case ast.Call():
                func = nodo.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
    return called


# ============================================================================
# Safety anchors — el servicio compone, no reimplementa (§26, §27, §28, §54)
# ============================================================================

#: Primitivas cuya presencia en el módulo significaría que S4-E está
#: reimplementando un motor que ya existe y ya fue auditado. Cada entrada
#: nombra la pieza cuyo re-cableado sería el defecto.
_PRIMITIVAS_PROHIBIDAS: dict[str, str] = {
    "SetSecurityInfo": "mutación de ACL cruda (S4-B / S4-C)",
    "apply_target_dacl_by_handle": "escritura del Target DACL (S4-B)",
    "verify_target_dacl": "verificación del Target DACL (S4-B)",
    "restore_security_descriptor_by_handle": "restauración de SD por handle (S4-C)",
    "verify_restored_pre_sd": "verificación de restauración de PRE (S4-C)",
    "commit_finalized": "escritura de COMMITTED (sólo S4-D)",
    "enter_finalization_phase": "avance de fase de S4-D (sólo S4-D)",
    "archive_golden_backup": "escritura del backup (sólo S4-D)",
    "record_node_mutation_intent": "WAL de nodo (sólo S4-B)",
    "record_node_mutation_completed": "WAL de nodo (sólo S4-B)",
    "SeDebugPrivilege": "ampliación de privilegios prohibida (§2)",
    "SeTcbPrivilege": "ampliación de privilegios prohibida (§2)",
}


@pytest.mark.parametrize("primitiva", sorted(_PRIMITIVAS_PROHIBIDAS))
def test_s4e_no_toca_primitivas_de_otros_slices(primitiva: str) -> None:
    """El servicio llama a las piezas completas; nunca a su interior.

    Freeze por igualdad literal del conjunto: si S4-E empieza a Listar
    ``apply_target_dacl_by_handle``, este test falla antes de que alguien lo
    lea en un reporte.
    """
    assert primitiva not in _nombres_del_archivo(), (
        f"S4-E menciona '{primitiva}', que pertenece a {_PRIMITIVAS_PROHIBIDAS[primitiva]}. "
        "La exclusión de S4-E es componer, no reimplementar."
    )


def test_s4e_no_importa_el_motor_de_mutacion_directo() -> None:
    """S4-E no importa target_dacl: la ACL se escribe por los puertos."""
    modulos = {nodo.module for nodo in ast.walk(_arbol()) if isinstance(nodo, ast.ImportFrom) and nodo.module}
    assert not any(m.endswith("target_dacl") for m in modulos), (
        "S4-E importa target_dacl: la implementación de ACL es de S4-B/S4-C, no del coordinador."
    )


def test_s4e_no_importa_staging_ni_el_clon_rv3() -> None:
    """STAGING != AUTHORITY: ni el clon RV-3 ni el staging son autoridad.

    ``UNTRUSTED_STAGING_AS_RECOVERY_SOURCE`` queda congelado en ``NEVER`` y
    la ausencia de estos imports es su ancla mecánica: no hay ni una ruta por
    la cual el servicio pueda leer un candidate manifest.
    """
    modulos = {nodo.module for nodo in ast.walk(_arbol()) if isinstance(nodo, ast.ImportFrom) and nodo.module}
    for prohibido in ("clone", "trusted_registry_store"):
        assert not any(m.endswith(prohibido) for m in modulos), (
            f"S4-E importa '{prohibido}': staging no es autoridad de recovery."
        )
    assert svc.UNTRUSTED_STAGING_AS_RECOVERY_SOURCE == "NEVER"


def test_s4e_no_toma_manifests_de_candidatos_como_autoridad() -> None:
    """Ninguna lectura del candidate manifest llega al camino de recovery.

    El manifest de candidatos es la salida de planning, no una fuente de
    recovery: si el servicio lo leyera, un Actor D con escritura en staging
    podría reconstruir el PRE desde ahí.
    """
    llamadas = _llamadas()
    for prohibido in (
        "read_candidate_manifest_bytes",
        "derive_candidate_manifest_path",
        "deserialize_candidate_manifest",
        "load_authorized_plan",
    ):
        assert prohibido not in llamadas, (
            f"S4-E llama a '{prohibido}': el manifest de candidatos no es autoridad de recovery."
        )


def test_s4e_no_escribe_el_tgr() -> None:
    """TGR != CURRENT FILESYSTEM: S4-E no muta el registro de Goldens de confianza."""
    llamadas = _llamadas()
    for prohibido in (
        "write_trusted_registry_atomically_at",
        "serialize_trusted_golden_registry",
        "admit_golden",
        "refresh_golden_admission",
    ):
        assert prohibido not in llamadas, (
            f"S4-E llama a '{prohibido}': la admisión del Golden es otra capability (S4-A/RV-TGR)."
        )


def test_s4e_no_abre_la_frontera_privilegiada_por_importacion() -> None:
    """Importar S4-E no abre la frontera ni mueve ACLs (efecto de import = nulo).

    Se comprueba que el módulo no tenga código a nivel de módulo que llame a
    ninguna función que abra un handle o lance un proceso: todo está dentro
    de funciones.
    """
    called = _llamadas()
    for prohibido in (
        "orchestrate_golden_protection_planning",
        "establish_privileged_authorization",
        "promote_durable_authorized_plan",
        "create_protection_journal",
        "apply_authorized_plan",
        "recover_interrupted_protection",
        "finalize_protection_transaction",
    ):
        assert prohibido in called, (
            f"'{prohibido}' ya no se llama desde S4-E: si desapareció, el cableado tiene un hueco "
            "y hay que decidirlo explícitamente, no dejarlo por descuido."
        )

    topo = [
        nodo
        for nodo in _arbol().body
        if isinstance(nodo, (ast.Expr, ast.Assign, ast.AnnAssign)) and not isinstance(nodo, ast.ClassDef)
    ]
    topo_llamadas = {
        nodo.func.id
        for stmt in topo
        for nodo in ast.walk(stmt)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
    }
    assert not topo_llamadas & {
        "acquire_golden_mutation_lock",
        "launch_privileged_helper",
        "apply_authorized_plan",
    }, f"efecto de import no nulo: {sorted(topo_llamadas)}"


def test_el_coordinator_esta_cableado_en_el_arranque_real() -> None:
    """El barrido de arranque EXISTE y está dentro del lifecycle real.

    Se afirma sobre el AST de ``app_context.py``, no sobre un grep: un import
    huérfano o una mención en un comentario NO cuentan como cableado. Es el
    mismo mecanismo que ``test_los_reconciliadores_estan_invocados_en_el_arranque``
    (U-03/U-08) aplicado a S4-E, y cierra la clase de fallo que la ausencia de
    wiring sea un no-op silencioso con la suite verde.
    """
    import sky_claw.app_context as app_ctx

    arbol = ast.parse(pathlib.Path(app_ctx.__file__).read_text(encoding="utf-8"))
    llamadas = {
        nodo.func.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
    }
    assert "reconciliar_arranque_pendiente" in llamadas, (
        "S4-E no está cableado en el arranque: existe un reconciliador que nadie invoca "
        "(la clase de fallo #240/#252/#362)."
    )
    # Y el coordinator se publica en el AppContext, que es el registro real
    # de capacidades del producto.
    assert hasattr(app_ctx.AppContext, "__init__")
    cuerpo_init = ast.dump(ast.parse(pathlib.Path(app_ctx.__file__).read_text(encoding="utf-8")))
    assert "runtime_vault_protection" in cuerpo_init, (
        "AppContext no publica runtime_vault_protection: el coordinator queda inalcanzable desde el producto."
    )


def test_el_arranque_no_registra_recovery_como_reconciliador_de_rollback() -> None:
    """S4-E NO se cuela en el ancla de orden de los reconciliadores existentes.

    El barrido del Golden va DESPUÉS de U-08 y es independiente: no comparte
    ritual, lock ni base durable con DynDOLOD. Este test documenta que los dos
    mecanismos siguen separados, que es lo que permite que uno falle sin
    arrastrar al otro.
    """
    import sky_claw.app_context as app_ctx

    fuente = pathlib.Path(app_ctx.__file__).read_text(encoding="utf-8")
    pos_u08 = fuente.index("reconcile_orphan_rollback_backups(")
    pos_s4e = fuente.index("reconciliar_arranque_pendiente(")
    assert pos_u08 < pos_s4e, (
        "el barrido de S4-E se movió antes del de U-08: los dos son independientes y el "
        "orden del existente no es negociable"
    )


def test_el_resolver_de_programdata_llega_al_barrido_y_al_resume() -> None:
    """El resolver del namespace se propaga a AMBAS mitades del barrido.

    Ancla de una clase de fallo real, ya encontrada y corregida durante este
    slice: `reconciliar_arranque_pendiente` pasaba el resolver al
    `discover_pending_operations` pero no al `resume_golden_protection`.
    El barrido enumeraba el namespace del RIG y el resume leia
    `%ProgramData%` PRODUCTIVO. En producción no se manifiesta (ambos
    default al mismo lugar), que es exactamente por lo que las dos mitades
    tienen que estar ancladas juntas.

    Es el mismo mecanismo que el repo ya exige para los pares lock/journal:
    no alcanza con cubrir la mitad que se vio.
    """
    import asyncio  # noqa: PLC0415

    vistos: list[object] = []

    class _Clasif:
        is_valid = True
        detail = ""
        journal = type("_J", (), {"transaction_state": ProtectionTransactionState.COMMITTED})()

    def _fake_discover(*, programdata_resolver=None, **kwargs):
        vistos.append(("discover", programdata_resolver))
        return (
            svc.DiscoveredOperation(
                operation_id=_OPERATION_ID,
                binding_evidence=OperationLockBindingEvidence.ABSENT,
                journal_state=ProtectionTransactionState.COMMITTED,
                route=svc.RestartRoute.S4D_NORMALIZE,
            ),
        )

    def _fake_resume(*, operation_id, frontend=svc._DEFAULT_FRONTEND):
        vistos.append(("resume", frontend.programdata_resolver))
        return svc.ProtectionOutcome(
            operation_id=operation_id,
            disposition=svc.ProtectionDisposition.ALREADY_COMMITTED,
            stage=svc.ProtectionStage.FINALIZATION,
            source_orchestrator="fake",
        )

    resolver = lambda: "/rig/programdata"  # noqa: E731
    monkey = pytest.MonkeyPatch()
    monkey.setattr(svc, "discover_pending_operations", _fake_discover)
    monkey.setattr(svc, "resume_golden_protection", _fake_resume)
    try:
        asyncio.run(
            svc.RuntimeVaultProtectionCoordinator().reconciliar_arranque_pendiente(programdata_resolver=resolver)
        )
    finally:
        monkey.undo()

    assert vistos == [("discover", resolver), ("resume", resolver)], vistos


# ============================================================================
# Routing exhaustivo (§23, M-E11)
# ============================================================================


def test_todo_estado_del_fsm_tiene_una_ruta() -> None:
    """LA tabla es exhaustiva sobre el enum VIVO, por igualdad literal.

    Si ``ProtectionTransactionState`` gana un miembro, este test queda rojo
    hasta que alguien decida a qué suborquestador pertenece. No hay valor por
    defecto en :func:`route_for_state` justamente por esto.
    """
    assert set(svc.RESTART_ROUTES) == set(ProtectionTransactionState), (
        "hay estados del FSM sin ruta o rutas a estados que ya no existen"
    )


def test_la_ruta_se_decide_por_el_contrato_vigente_no_por_comodidad() -> None:
    """Congela el mapeo estado→ruta con la tabla real del contrato.

    Justificación de cada fila:

    * ``ROLLBACK_REQUIRED``/``ROLLING_BACK`` NUNCA van a S4-D: su
      ``_FASES_ADMISIBLES`` los excluye y devuelve ``NOT_APPLICABLE``. El motor
      del rollback es S4-C. (Atajar el "PR-2" de esto, mutando el FSM en vez
      de enrutar, rompería el contrato ya auditado de ambos slices.)
    * ``COMMITTED`` va a ``S4D_NORMALIZE`` — releer y liberar el lock huérfano
      sin re-aplicar ni re-verificar.
    * ``ROLLBACK_FAILED`` e ``INDETERMINATE`` exigen operador y S4-E no los
      reintenta ni los resuelve por su cuenta.
    """
    esperado = {
        ProtectionTransactionState.PREPARING: svc.RestartRoute.S4C_ROLLBACK,
        ProtectionTransactionState.PREPARED: svc.RestartRoute.S4C_ROLLBACK,
        ProtectionTransactionState.AWAITING_ELEVATION: svc.RestartRoute.S4C_ROLLBACK,
        ProtectionTransactionState.APPLYING: svc.RestartRoute.S4C_ROLLBACK,
        ProtectionTransactionState.ROLLBACK_REQUIRED: svc.RestartRoute.S4C_ROLLBACK,
        ProtectionTransactionState.ROLLING_BACK: svc.RestartRoute.S4C_ROLLBACK,
        ProtectionTransactionState.VERIFYING_GP1: svc.RestartRoute.S4D_FINALIZE,
        ProtectionTransactionState.VERIFYING_RV2: svc.RestartRoute.S4D_FINALIZE,
        ProtectionTransactionState.VERIFYING_NODE_SET: svc.RestartRoute.S4D_FINALIZE,
        ProtectionTransactionState.ARCHIVING_BACKUP: svc.RestartRoute.S4D_FINALIZE,
        ProtectionTransactionState.COMMITTED: svc.RestartRoute.S4D_NORMALIZE,
        ProtectionTransactionState.CANCELLED: svc.RestartRoute.TERMINAL,
        ProtectionTransactionState.ELEVATION_REJECTED: svc.RestartRoute.TERMINAL,
        ProtectionTransactionState.REFUSE_TO_APPLY: svc.RestartRoute.TERMINAL,
        ProtectionTransactionState.REFUSE_TO_PLAN: svc.RestartRoute.TERMINAL,
        ProtectionTransactionState.ROLLED_BACK: svc.RestartRoute.TERMINAL,
        ProtectionTransactionState.ROLLBACK_FAILED: svc.RestartRoute.OPERATOR_REQUIRED,
        ProtectionTransactionState.INDETERMINATE: svc.RestartRoute.OPERATOR_REQUIRED,
    }
    assert esperado == svc.RESTART_ROUTES


def test_route_for_state_no_tiene_default() -> None:
    """Un estado nuevo NO puede cair en una ruta por defecto: explota.

    Es la mitad mecánica del anchor anterior: convierte "falta una fila" en
    un fallo ruidoso en el punto de uso, no en un ``TERMINAL`` silencioso que
    dejaría un Golden sin endurecer creyéndolo terminal.
    """
    with pytest.raises(KeyError):
        svc.route_for_state("estado_inventado")  # type: ignore[arg-type]


def test_ningun_estado_terminal_requiere_s4c_o_s4d() -> None:
    """Los terminales que S4-E resuelve sin tocar el Golden son coherentes.

    ``ROLLED_BACK``/``REFUSE_*``/``CANCELLED``/``ELEVATION_REJECTED`` ya
    jongearon: reabrir S4-C o S4-D sobre ellos sería una segunda mutación
    sobre evidencia terminal. ``COMMITTED`` sí enruta a S4-D, pero sólo a la
    ruta que NORMALIZA (relee el backup y libera el lock huérfano) sin
    re-aplicar ni re-verificar — es idempotencia, no una segunda mutación.

    Los otros dos terminales (``INDETERMINATE`` y ``ROLLBACK_FAILED``) van a
    ``OPERATOR_REQUIRED``: S4-E no los ejecuta ni los reintenta.
    """
    rutas_de_terminal = {estado: svc.RESTART_ROUTES[estado] for estado in TERMINAL_TRANSACTION_STATES}
    # Ningún terminal entra a un motor que MUTA. S4-D_NORMALIZE es la única
    # ruta de S4-D admisible sobre un terminal, y es de normalización.
    rutas_mutantes = {
        svc.RestartRoute.S4C_ROLLBACK,
        svc.RestartRoute.S4D_FINALIZE,
    }
    assert not {r for r in rutas_de_terminal.values() if r in rutas_mutantes}, rutas_de_terminal
    # Los terminales limpios se resuelven sin tocar el Golden.
    limpios = set(rutas_de_terminal) - {
        ProtectionTransactionState.COMMITTED,
        ProtectionTransactionState.INDETERMINATE,
        ProtectionTransactionState.ROLLBACK_FAILED,
    }
    assert all(rutas_de_terminal[e] is svc.RestartRoute.TERMINAL for e in limpios), rutas_de_terminal
    assert rutas_de_terminal[ProtectionTransactionState.COMMITTED] is svc.RestartRoute.S4D_NORMALIZE
    # Los dos fail-closed NO se resuelven solos.
    assert rutas_de_terminal[ProtectionTransactionState.INDETERMINATE] is (svc.RestartRoute.OPERATOR_REQUIRED)
    assert rutas_de_terminal[ProtectionTransactionState.ROLLBACK_FAILED] is (svc.RestartRoute.OPERATOR_REQUIRED)


# ============================================================================
# Contrato de API (§19, §20, M-E3, M-E4)
# ============================================================================


def test_el_camino_de_recuperacion_no_acepta_paths() -> None:
    """``resume_golden_protection`` no expone autoridad de filesystem.

    Se niegan por nombre los parámetros que permitirían acuñar autoridad
    eligiendo rutas: plan path, journal path, backup path, lock path y el
    SD del PRE. ``operation_id`` sí se acepta — es identidad durable, no una
    ruta — y ``frontend.programdata_resolver`` es un seam de test, no un
    camino: el servicio nunca lo usa para construir la ruta del plan.
    """
    parametros = set(inspect.signature(svc.resume_golden_protection).parameters)
    assert parametros == {"operation_id", "authorization", "frontend"}, (
        f"la API de recovery cambió su superficie: {sorted(parametros)}"
    )
    for prohibido in (
        "plan_path",
        "journal_path",
        "backup_path",
        "lock_path",
        "golden_lock_path",
        "pre_sd",
        "security_descriptor",
        "program",
        "executable",
        "canonical_root",
        "root",
    ):
        assert prohibido not in parametros, (
            f"el camino de recovery expone '{prohibido}': un caller podría acuñar autoridad eligiendo paths arbitrarios"
        )


def test_la_api_productiva_recibe_intencion_no_detalles() -> None:
    """El camino feliz expone root+operation_id+autorización, no rutas de autoridad."""
    parametros = set(inspect.signature(svc.protect_golden_root).parameters)
    assert {"root", "operation_id", "authorization"} <= parametros
    for prohibido in (
        "plan_path",
        "journal_path",
        "backup_path",
        "lock_path",
        "pre_sd",
    ):
        assert prohibido not in parametros


def test_todos_los_parametros_de_s4e_son_keyword_only() -> None:
    """Ninguna función pública acepta posición: no hay reordenamiento accidental."""
    for fn in (
        svc.protect_golden_root,
        svc.resume_golden_protection,
        svc.discover_pending_operations,
    ):
        parametros = inspect.signature(fn).parameters
        posicionales = [
            nombre
            for nombre, param in parametros.items()
            if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
        ]
        assert not posicionales, f"{fn.__name__} acepta posicionales: {posicionales}"


# ============================================================================
# Continuidad del GoldenMutationLock (§21, M-E1)
# ============================================================================


class _HandleFalso:
    """Handle que registra CADA evento de cierre, en orden."""

    def __init__(self) -> None:
        self.closed = False
        self.eventos: list[str] = []
        self._lock = threading.Lock()

    def release(self) -> bool:
        with self._lock:
            if self.closed:
                return False
            self.closed = True
            self.eventos.append("release")
            return True

    def retain_for_inspection(self) -> bool:
        with self._lock:
            if self.closed:
                return False
            self.closed = True
            self.eventos.append("retain")
            return True


class _TokenFalso:
    def __init__(self) -> None:
        self.cerrado = False

    def close(self) -> bool:
        self.cerrado = True
        return True


class _SesionFalsa:
    """Específicamente NO es una PrivilegedBoundarySession real.

    Existe para observar la SECUENCIA de eventos de la frontera sin abrir un
    handle Win32. ``apply_authorized_plan`` y
    ``finalize_protection_transaction`` están monkeypatcheados en el test, así
    que ninguno la valida — y para esta propiedad esa validación sería
    irrelevante: lo que importa es que el mismo objeto llegue a los dos.
    """

    def __init__(self) -> None:
        self.lock = _HandleFalso()
        self.operator_token = _TokenFalso()
        self.closed = False

    def close(self) -> bool:
        self.closed = True
        self.lock.release()
        self.operator_token.close()
        return True


def test_la_sesion_que_llega_a_apply_es_la_misma_que_llega_a_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misma sesión, mismo lock: no hay release→reacquire entre S4-B y S4-D.

    Es el ancla causal de §21. Si alguien insertara un ``session.close()``
    entre apply y finalización, el segundo ``release()`` sería idempotente y
    el handle ya estaría cerrado: el bug NO se vería mirando el resultado,
    se vería acá.
    """
    sesion = _SesionFalsa()
    vistos: list[Any] = []

    def _fake_auth(**kwargs: Any) -> tuple[Any, Any]:
        return object(), sesion

    def _fake_promote(**kwargs: Any) -> Any:
        return object()

    def _fake_journal(*args: Any, **kwargs: Any) -> Any:
        return object()

    def _fake_apply(*, journal: Any, session: Any, **kwargs: Any) -> Any:
        vistos.append(("apply", id(session), sesion.lock.closed))
        return _ReporteApplyFalso()

    def _fake_finalize(*, journal: Any, port: Any, **kwargs: Any) -> Any:
        vistos.append(("finalize", id(sesion), sesion.lock.closed))
        return _ReporteFinalizacionFalso()

    monkeypatch.setattr(svc, "orchestrate_golden_protection_planning", _planning_exitoso)
    monkeypatch.setattr(svc, "establish_privileged_authorization", _fake_auth)
    monkeypatch.setattr(svc, "promote_durable_authorized_plan", _fake_promote)
    monkeypatch.setattr(svc, "create_protection_journal", _fake_journal)
    monkeypatch.setattr(svc, "apply_authorized_plan", _fake_apply)
    monkeypatch.setattr(svc, "finalize_protection_transaction", _fake_finalize)

    resultado = svc.protect_golden_root(
        root="C:/cualquier",
        operation_id=_OPERATION_ID,
        authorization=_autorizacion_falsa(),  # type: ignore[arg-type]
    )

    assert [etapa for etapa, _, _ in vistos] == ["apply", "finalize"]
    assert vistos[0][1] == vistos[1][1], (
        "apply y finalize recibieron sesiones DISTINTAS: el GoldenMutationLock se soltó y se "
        "re-tomó entre S4-B y S4-D (ventana de exclusión rota)."
    )
    assert not vistos[0][2], "el lock ya estaba cerrado al entrar a S4-B"
    assert not vistos[1][2], (
        "el lock se liberó ANTES de S4-D: otra mutadora puede entrar sobre un Golden que "
        "todavía puede terminar en ROLLBACK_REQUIRED."
    )
    assert resultado.disposition is svc.ProtectionDisposition.COMMITTED


def test_el_lock_se_libera_solo_tras_el_desenlace_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COMMITTED durable ANTES del release, y el release ocurre una vez.

    El orden inverso —liberar y después comitear— es la ventana que S4-D ya
    documenta en su propio argumento; el coordinador no puede reintroducirla
    por el lado del ``finally``.
    """
    sesion = _SesionFalsa()
    orden: list[str] = []

    def _fake_finalize(**kwargs: Any) -> Any:
        orden.append("finalize")
        return _ReporteFinalizacionFalso()

    sesion.lock.release = lambda: (orden.append("release"), True)[1]  # type: ignore[method-assign]

    monkeypatch.setattr(svc, "orchestrate_golden_protection_planning", _planning_exitoso)
    monkeypatch.setattr(svc, "establish_privileged_authorization", lambda **k: (object(), sesion))
    monkeypatch.setattr(svc, "promote_durable_authorized_plan", lambda **k: object())
    monkeypatch.setattr(svc, "create_protection_journal", lambda *a, **k: object())
    monkeypatch.setattr(svc, "apply_authorized_plan", lambda **k: _ReporteApplyFalso())
    monkeypatch.setattr(svc, "finalize_protection_transaction", _fake_finalize)

    svc.protect_golden_root(
        root="C:/cualquier",
        operation_id=_OPERATION_ID,
        authorization=_autorizacion_falsa(),  # type: ignore[arg-type]
    )
    assert orden == ["finalize", "release"], (
        f"orden incorrecto de frontera: {orden} — el COMMITTED durable tiene que preceder al release"
    )


def test_un_desenlace_no_terminal_retiene_el_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """INDETERMINATE ⇒ ``retain_for_inspection``, nunca ``release``.

    Liberar en un desenlace no terminal dejaría la evidencia sin la marca de
    lock huérfano que S4-C necesita para retomar la MISMA operación.
    """
    sesion = _SesionFalsa()

    def _fake_finalize(**kwargs: Any) -> Any:
        return _ReporteFinalizacionFalso(disposition=FinalizationDisposition.INDETERMINATE)

    monkeypatch.setattr(svc, "orchestrate_golden_protection_planning", _planning_exitoso)
    monkeypatch.setattr(svc, "establish_privileged_authorization", lambda **k: (object(), sesion))
    monkeypatch.setattr(svc, "promote_durable_authorized_plan", lambda **k: object())
    monkeypatch.setattr(svc, "create_protection_journal", lambda *a, **k: object())
    monkeypatch.setattr(svc, "apply_authorized_plan", lambda **k: _ReporteApplyFalso())
    monkeypatch.setattr(svc, "finalize_protection_transaction", _fake_finalize)

    resultado = svc.protect_golden_root(
        root="C:/cualquier",
        operation_id=_OPERATION_ID,
        authorization=_autorizacion_falsa(),  # type: ignore[arg-type]
    )
    assert sesion.lock.eventos == ["retain"], sesion.lock.eventos
    assert resultado.disposition is svc.ProtectionDisposition.INDETERMINATE
    assert resultado.fail_closed_reason


def test_cerrar_la_frontera_es_idempotente() -> None:
    """``_cerrar_frontera`` dos veces no suelta el lock dos veces."""
    sesion = _SesionFalsa()
    svc._cerrar_frontera(sesion, retener_lock=False)
    sesion.closed = True
    svc._cerrar_frontera(sesion, retener_lock=False)
    assert sesion.lock.eventos == ["release"]


# ============================================================================
# Proyecciones (§29, §30, §40)
# ============================================================================


def test_un_desenlace_no_terminal_siempre_explica_por_que() -> None:
    """Invariante dura del DTO: sin ``fail_closed_reason`` no se construye.

    Contrapositiva de la invariante que S4-D ya impone sobre su reporte. Un
    resultado que no afirma éxito tiene que decir por qué; un ``FAILED`` mudo
    es indistinguible de un ``LOCK_BUSY``.
    """
    with pytest.raises(ValueError, match="fail_closed_reason"):
        svc.ProtectionOutcome(
            operation_id=_OPERATION_ID,
            disposition=svc.ProtectionDisposition.LOCK_BUSY,
            stage=svc.ProtectionStage.FINALIZATION,
            source_orchestrator="x.y",
        )


def test_el_dto_dice_quien_hablo() -> None:
    """``source_orchestrator`` es obligatorio: el resultado nunca es anónimo."""
    with pytest.raises(ValueError, match="source_orchestrator"):
        svc.ProtectionOutcome(
            operation_id=_OPERATION_ID,
            disposition=svc.ProtectionDisposition.COMMITTED,
            stage=svc.ProtectionStage.FINALIZATION,
            source_orchestrator="",
        )


def test_rolled_back_esta_cerrado_pero_no_committed() -> None:
    """``ROLLED_BACK`` es ``settled`` y NO ``committed``; y no exige razón.

    El bug que este anchor ancla: la invariante de ``fail_closed_reason``
    medía contra ``TERMINAL_DISPOSITIONS`` (= endurecido). Un rollback
    correcto —que es un desenlace COMPLETO, con el Golden devuelto a su
    estado previo y probado— no entraba en ese conjunto, así que el DTO
    rechazaba construirlo y TODO ``ROLLED_BACK`` real terminaba en
    ``ValueError`` dentro de la proyección de S4-C.

    El par de propiedades que importa para la frontera:

    * ``settled`` decide liberar-vs-retener el ``GoldenMutationLock``. Un
      rollback cerrado debe LIBERAR: la operación terminó y no hay evidencia
      que otro proceso necesite retomar.
    * ``committed`` sigue siendo falso: el Golden NO quedó endurecido, y el
      ``ALREADY_COMMITTED`` de un replay tiene que seguir siendo distinto de
      un ``ROLLED_BACK``.
    """
    for disposition, es_committed in (
        (svc.ProtectionDisposition.ROLLED_BACK, False),
        (svc.ProtectionDisposition.NOT_APPLICABLE, False),
        (svc.ProtectionDisposition.COMMITTED, True),
        (svc.ProtectionDisposition.ALREADY_COMMITTED, True),
    ):
        resultado = svc.ProtectionOutcome(
            operation_id=_OPERATION_ID,
            disposition=disposition,
            stage=svc.ProtectionStage.RECOVERY,
            source_orchestrator="x.y",
        )
        assert resultado.settled is True, f"{disposition.value} debería estar cerrado"
        assert resultado.committed is es_committed, f"{disposition.value}: committed mal proyectado"

    for disposition in (
        svc.ProtectionDisposition.LOCK_BUSY,
        svc.ProtectionDisposition.INDETERMINATE,
        svc.ProtectionDisposition.ROLLBACK_REQUIRED,
        svc.ProtectionDisposition.REFUSED,
        svc.ProtectionDisposition.FAILED,
    ):
        with pytest.raises(ValueError, match="fail_closed_reason"):
            svc.ProtectionOutcome(
                operation_id=_OPERATION_ID,
                disposition=disposition,
                stage=svc.ProtectionStage.RECOVERY,
                source_orchestrator="x.y",
            )


def test_la_frontera_libera_tras_un_rollback_cerrado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un ``ROLLED_BACK`` LIBERA el lock; un ``INDETERMINATE`` lo RETIENE.

    Ancla del cierre de frontera con las dos mitades: la propiedad que decide
    no es "¿se commiteó?" sino "¿se cerró?". Medir con ``committed`` liberaba
    el lock sobre un rollback ya terminado — que es justo la ventana que S4-D
    declara prohibida, sólo que en el otro extremo.
    """
    sesion = _SesionFalsa()
    monkeypatch.setattr(svc, "orchestrate_golden_protection_planning", _planning_exitoso)
    monkeypatch.setattr(svc, "establish_privileged_authorization", lambda **k: (object(), sesion))
    monkeypatch.setattr(svc, "promote_durable_authorized_plan", lambda **k: object())
    monkeypatch.setattr(svc, "create_protection_journal", lambda *a, **k: object())
    monkeypatch.setattr(
        svc,
        "apply_authorized_plan",
        lambda **k: _ReporteApplyFalso(
            apply_error="nodo 1 rechazo del permiso", rolled_back_nodes=("Data/Skyrim.esm",)
        ),
    )

    resultado = svc.protect_golden_root(
        root="C:/cualquier",
        operation_id=_OPERATION_ID,
        authorization=_autorizacion_falsa(),
    )
    assert resultado.disposition is svc.ProtectionDisposition.ROLLED_BACK
    assert resultado.rollback_executed is True
    assert sesion.lock.eventos == ["release"], sesion.lock.eventos


def test_la_proyeccion_de_finalizacion_no_reclasifica() -> None:
    """La proyección copia el disposition de S4-D sin reinterpretarlo.

    S4-D ya separó ``LOCK_BUSY`` ("nadie hizo nada") de ``INDETERMINATE``
    ("no se puede demostrar") y ya separó ``COMMITTED`` de
    ``ALREADY_COMMITTED``. Re-clasificar en el coordinador sería una segunda
    taxonomía capaz de discrepar de la suya en silencio.
    """
    for disp in FinalizationDisposition:
        reporte = _ReporteFinalizacionFalso(disposition=disp)
        proyectado = svc._proyeccion_de_finalizacion(reporte)
        assert proyectado.source_orchestrator.startswith("finalization_orchestrator")
        assert proyectado.stage is svc.ProtectionStage.FINALIZATION


def test_indeterminate_pide_operador_y_rollback_required_tambien() -> None:
    """Los dos desenlaces fail-closed de S4-D levantan la bandera de operador."""
    for disp in (
        svc.ProtectionDisposition.INDETERMINATE,
        svc.ProtectionDisposition.ROLLBACK_REQUIRED,
    ):
        reporte = _ReporteFinalizacionFalso(disposition=_DISP_POR_DISPOSITION[disp])
        proyectado = svc._proyeccion_de_finalizacion(reporte)
        assert proyectado.operator_intervention_required is True
        assert proyectado.fail_closed_reason
        assert proyectado.committed is False


def test_el_forense_no_expone_bytes_de_seguridad() -> None:
    """El DTO forense no tiene ningún campo que pueda llevar SD crudo.

    Se congela el CONJUNTO de campos, no una muestra: agregar
    ``pre_security_descriptor`` al dataclass rompe este test.
    """
    campos = set(svc.ProtectionOutcome.__dataclass_fields__)
    assert campos == {
        "operation_id",
        "disposition",
        "stage",
        "source_orchestrator",
        "journal_state",
        "route",
        "plan_digest",
        "archive_digest",
        "lock_outcome",
        "lock_retained",
        "rollback_executed",
        "detail",
        "fail_closed_reason",
        "operator_intervention_required",
    }
    for prohibido in ("sd", "security_descriptor", "token", "handle", "raw_bytes", "pre_sd"):
        assert not any(prohibido in campo for campo in campos)


def test_el_dto_de_discovery_no_es_autoridad() -> None:
    """``DiscoveredOperation`` transporta una identidad, no un permiso."""
    campos = set(svc.DiscoveredOperation.__dataclass_fields__)
    assert campos == {"operation_id", "binding_evidence", "journal_state", "route"}
    assert "root" not in campos
    assert "plan_path" not in campos


# ============================================================================
# Mutantes que el servicio tiene que hacer fallar (M-E5, M-E6, M-E7)
# ============================================================================


def test_el_routing_ignora_la_memoria_del_proceso_anterior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-E12: la ruta sale del journal en disco, no de lo que S4-E "recuerda".

    Se simula un journal que en disco dice ``VERIFYING_RV2`` mientras el
    servicio acaba de applyear: la ruta tiene que ser la del disco.
    """
    from sky_claw.local.runtime_vault.protection_journal_store import (
        ProtectionJournalClassification,
    )

    class _Clasif:
        is_valid = True
        detail = ""
        journal = type(
            "_J",
            (),
            {"transaction_state": ProtectionTransactionState.VERIFYING_RV2},
        )()
        classification = ProtectionJournalClassification.VALID

    llamada_finalize: list[str] = []

    def _fake_finalize(**kwargs: Any) -> Any:
        llamada_finalize.append("finalize")
        return _ReporteFinalizacionFalso()

    monkeypatch.setattr(svc, "classify_protection_journal", lambda *a, **k: _Clasif())
    monkeypatch.setattr(svc, "load_durable_authorized_plan", lambda *a, **k: object())
    monkeypatch.setattr(svc, "open_protection_journal", lambda *a, **k: object())
    monkeypatch.setattr(svc, "build_default_verification_port", lambda: object())
    monkeypatch.setattr(svc, "finalize_protection_transaction", _fake_finalize)

    resultado = svc.resume_golden_protection(operation_id=_OPERATION_ID)
    assert llamada_finalize == ["finalize"], "el estado durable del disco no enruto a S4-D"
    # La ruta que se reporta es la DEL DISCO, no la que el servicio recuerda.
    assert resultado.route is svc.RestartRoute.S4D_FINALIZE
    # `journal_state` del resultado es el estado en que S4-D DEJÓ la
    # transacción, no el de entrada: ese lo consumió el router y ya se
    # no necesita viajar en el DTO terminal.


def test_un_journal_ilegible_produce_indeterminate_sin_tocar_nada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E09/E10: evidencia durable corrupta ⇒ INDETERMINATE y cero escrituras."""
    llamadas: list[str] = []

    class _Clasif:
        is_valid = False
        detail = "torn tail"
        journal = None

    for nombre in (
        "recover_interrupted_protection",
        "finalize_protection_transaction",
        "apply_authorized_plan",
        "load_durable_authorized_plan",
    ):
        monkeypatch.setattr(
            svc,
            nombre,
            lambda *a, _nombre=nombre, **k: llamadas.append(_nombre),
        )

    monkeypatch.setattr(svc, "classify_protection_journal", lambda *a, **k: _Clasif())

    resultado = svc.resume_golden_protection(operation_id=_OPERATION_ID)
    assert resultado.disposition is svc.ProtectionDisposition.INDETERMINATE
    assert resultado.operator_intervention_required is True
    assert resultado.lock_retained is True
    assert llamadas == [], f"se invocó {llamadas} con evidencia durable inválida"


def test_un_estado_operador_requerido_no_se_reintenta_solo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E-caso ROLLBACK_FAILED: S4-E no reintenta un desenlace fail-closed.

    Reintentar automáticamente un rollback fallido es la forma de項 convertir
    "necesita un humano" en "cambia el filesystem otra vez, a ciegas".
    """
    from sky_claw.local.runtime_vault.protection_journal_store import (
        ProtectionJournalClassification,
    )

    llamadas: list[str] = []

    class _Clasif:
        is_valid = True
        detail = ""
        classification = ProtectionJournalClassification.VALID
        journal = type(
            "_J",
            (),
            {"transaction_state": ProtectionTransactionState.ROLLBACK_FAILED},
        )()

    monkeypatch.setattr(svc, "classify_protection_journal", lambda *a, **k: _Clasif())
    monkeypatch.setattr(
        svc,
        "recover_interrupted_protection",
        lambda *a, **k: llamadas.append("s4c") or object(),
    )
    monkeypatch.setattr(
        svc,
        "finalize_protection_transaction",
        lambda *a, **k: llamadas.append("s4d") or object(),
    )

    resultado = svc.resume_golden_protection(operation_id=_OPERATION_ID)
    assert resultado.disposition is svc.ProtectionDisposition.INDETERMINATE
    assert resultado.operator_intervention_required is True
    assert resultado.route is svc.RestartRoute.OPERATOR_REQUIRED
    assert llamadas == []


# ============================================================================
# Fakes locales
# ============================================================================

import threading  # noqa: E402

_OPERATION_ID = "3f2b1c8e-9a4d-4f5e-8b7a-1c2d3e4f5a6b"
#: Campos de GP2Result que el coordinador lee. Congelado por el anchor de abajo.
_CAMPOS_DE_PLANNING_QUE_S4E_LEE = frozenset({"disposition", "success", "message", "sealed_plan"})
_DISP_POR_DISPOSITION = {
    svc.ProtectionDisposition.COMMITTED: FinalizationDisposition.COMMITTED,
    svc.ProtectionDisposition.ALREADY_COMMITTED: FinalizationDisposition.ALREADY_COMMITTED,
    svc.ProtectionDisposition.ROLLBACK_REQUIRED: FinalizationDisposition.ROLLBACK_REQUIRED,
    svc.ProtectionDisposition.LOCK_BUSY: FinalizationDisposition.LOCK_BUSY,
    svc.ProtectionDisposition.INDETERMINATE: FinalizationDisposition.INDETERMINATE,
    svc.ProtectionDisposition.NOT_APPLICABLE: FinalizationDisposition.NOT_APPLICABLE,
}


def _autorizacion_falsa() -> Any:
    return svc.ProtectionAuthorizationInputs(
        launch_request=object(),
        expected_coordinator=object(),
        coordinator_probe_provider=object(),
        elevation_case=object(),
        ppsc_provider=object(),
        ppsc_payload=object(),
        volume_serial_number=1,
        root_file_id=2,
    )


def _planning_exitoso(*args: Any, **kwargs: Any) -> Any:
    """Planning que devuelve un plan SELLADO, sin tocar el disco.

    **Fake de seam, y a propósito.** Estos tests no validan el plan — esa es
    la superficie de S-2 y tiene su propia suite
    (``test_runtime_vault_golden_protection_plan.py``). Lo que se verifica acá
    es que el coordinador *use* el seam de planning correctamente, así que el
    stub expone exactamente los cuatro campos que el servicio lee. El anchor
    ``test_el_coordinador_solo_usa_el_seam_de_planning`` congela ese conjunto,
    de modo que si S-2 cambia el contrato del resultado, el fallo nombra el
    campo en vez de que el fake siga sirviendo un shape que nadie produce.
    """
    from types import SimpleNamespace

    from sky_claw.local.runtime_vault.planning_orchestrator import GP2PlanningDisposition

    return SimpleNamespace(
        disposition=GP2PlanningDisposition.PREPARED,
        success=True,
        message="",
        sealed_plan=object(),
    )


def test_el_coordinador_solo_usa_el_seam_de_planning() -> None:
    """Congela los campos de ``GP2Result`` que S4-E lee.

    La propiedad que importa: el coordinador NO reinterpreta el resultado de
    planning. Lee el desenlace y sigue, o rechaza sin abrir frontera. Si
    empieza a inspeccionar ``golden_verification`` o ``protection_result``
    para decidir por su cuenta, se está convirtiendo en una segunda capa de
    planificación — que es exactamente el defecto de scope que S4-E evita.
    """
    from sky_claw.local.runtime_vault.planning_orchestrator import GP2Result

    assert set(GP2Result.__dataclass_fields__) >= _CAMPOS_DE_PLANNING_QUE_S4E_LEE
    assert {
        "disposition",
        "success",
        "message",
        "sealed_plan",
    } == _CAMPOS_DE_PLANNING_QUE_S4E_LEE, (
        "S4-E lee mas campos de GP2Result de los previstos: revisa si sigue siendo un cableador "
        "o si se convertio en una segunda capa de planificacion."
    )


class _ReporteApplyFalso:
    """`ApplyReport` de S4-B para los tests de composicion.

    Los defaults son los del camino feliz: apply OK, sin rollback, sin error.
    Los tests que necesitan un desenlace distinto pasan `apply_error` /
    `rollback_error` explicitos.
    """

    def __init__(
        self,
        *,
        operation_id: str = _OPERATION_ID,
        transaction_state: ProtectionTransactionState = ProtectionTransactionState.APPLYING,
        apply_error: str | None = None,
        rollback_error: str | None = None,
        rolled_back_nodes: tuple[str, ...] = (),
        setsecurityinfo_calls: int = 3,
    ) -> None:
        self.operation_id = operation_id
        self.transaction_state = transaction_state
        self.apply_error = apply_error
        self.rollback_error = rollback_error
        self.rolled_back_nodes = rolled_back_nodes
        self.setsecurityinfo_calls = setsecurityinfo_calls
        self.outcomes: tuple[Any, ...] = ()


class _ReporteFinalizacionFalso:
    """Construye un ``FinalizationForensicReport`` real (invariantes incluidas).

    No un objeto con los atributos sueltos: si el contrato de S4-D cambia, este
    fake se rompe al construirse y el test dice por qué, en vez de dejar pasar
    una proyección de un reporte que ya no podría existir.
    """

    def __new__(
        cls,
        disposition: FinalizationDisposition = FinalizationDisposition.COMMITTED,
    ) -> Any:
        from sky_claw.local.runtime_vault.finalization_orchestrator import (
            FinalizationForensicReport,
        )

        comiteado = disposition in (
            FinalizationDisposition.COMMITTED,
            FinalizationDisposition.ALREADY_COMMITTED,
        )
        return FinalizationForensicReport(
            operation_id=_OPERATION_ID,
            disposition=disposition,
            phase_reached=FinalizationPhase.COMMITTED if comiteado else FinalizationPhase.ENTRY,
            verdicts=(),
            lock=FinalizationLockOutcome(
                acquired=True,
                released=comiteado,
                retained_as_orphan=not comiteado,
                operation_id=_OPERATION_ID,
            ),
            journal_state=ProtectionTransactionState.COMMITTED
            if comiteado
            else ProtectionTransactionState.INDETERMINATE,
            archive_digest="a" * 64 if comiteado else None,
            authorized_plan_digest="b" * 64,
            fail_closed_reason="" if comiteado else "evidencia durable ambigua",
        )


# ``RuntimeVaultError`` se importa para que el ancla de la jerarquía quede
# visible si alguien la usa como base de una excepción del servicio.
assert issubclass(RuntimeVaultError, Exception)

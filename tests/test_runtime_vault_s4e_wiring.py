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
from types import SimpleNamespace
from typing import Any

import pytest

from sky_claw.local.runtime_vault import protection_service as svc
from sky_claw.local.runtime_vault.authorized_plan_store import DurableWriteOutcome
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
from sky_claw.local.runtime_vault.protection_journal_store import ProtectionJournalClassification

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
        "recover_interrupted_protection_for_continuation",
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
    """GP2-S4E / P4 — el arranque llama al DIAGNOSTICO, y NO al mutante.

    Se afirma sobre el AST de ``app_context.py``, no sobre un grep: un import
    huerfano o una mencion en un comentario NO cuentan como cableado. Es el
    mismo mecanismo que ``test_los_reconciliadores_estan_invocados_en_el_arranque``
    (U-03/U-08) aplicado a S4-E, y cierra la clase de fallo que la ausencia de
    wiring sea un no-op silencioso con la suite verde.

    El anchor afirmaba antes `reconciliar_arranque_pendiente`, que ejecutaba
    S4-C y S4-D — restauracion de SDs, gates, archivado — desde
    `asyncio.to_thread` en el proceso normal. Un thread no cambia el token de
    seguridad, asi que ese cableado afirmaba una garantia de privilegios que no
    existe, y `PACKAGED_HELPER_PROVISIONING_STATUS = UNRESOLVED` impide
    levantarla de verdad desde ahi.

    Ahora el arranque llama `diagnosticar_arranque` (read-only) y NO llama al
    mutante. Las DOS mitades quedan congeladas: si alguien reconecta el metodo
    mutante, este test se pone rojo.
    """
    import sky_claw.app_context as app_ctx

    arbol = ast.parse(pathlib.Path(app_ctx.__file__).read_text(encoding="utf-8"))
    llamadas = {
        nodo.func.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
    }

    assert "diagnosticar_arranque" in llamadas, (
        "S4-E no esta cableado en el arranque: existe un reconciliador que nadie invoca "
        "(la clase de fallo #240/#252/#362)."
    )
    assert "reconciliar_arranque_pendiente" not in llamadas, (
        "el arranque volvio a ejecutar recovery/finalizacion en el proceso normal: "
        "asyncio.to_thread no cambia el token de seguridad, y el helper empaquetado sigue "
        "UNRESOLVED. La reconciliacion mutante pertenece a la frontera privilegiada."
    )
    # Y el coordinator se publica en el AppContext, que es el registro real
    # de capacidades del producto.
    cuerpo_init = ast.dump(arbol)
    assert "runtime_vault_protection" in cuerpo_init, (
        "AppContext no publica runtime_vault_protection: el coordinator queda inalcanzable desde el producto."
    )


def test_el_arranque_publica_el_diagnostico_y_no_lo_descarta() -> None:
    """P4 — los outcomes del arranque quedan OBSERVABLES, no se tiran.

    Un barrido que calcula y descarta el resultado no le sirve a nada: el
    operador no tiene forma de enterarse de que hay una operacion pendiente.
    Este anchor congela que el estado se guarda en el AppContext y que el
    warning estructurado se emite.
    """
    import sky_claw.app_context as app_ctx

    fuente = pathlib.Path(app_ctx.__file__).read_text(encoding="utf-8")

    assert "runtime_vault_startup_attention" in fuente, (
        "el diagnostico de arranque no se guarda en ningun estado consultable"
    )
    assert "STARTUP_CONTINUES_WITH_RUNTIME_VAULT_INTERVENTION_REQUIRED" in fuente, (
        "un arranque que continua sin dejar rastro de la intervencion pendiente es un "
        "best-effort que se confunde con exito silencioso"
    )


def test_el_coordinator_no_puede_mutar_desde_un_thread_normal() -> None:
    """P4 — `diagnosticar_arranque` es sincrono y no muta.

    Es read-only por construccion: no es `async`, asi que no hay
    `asyncio.to_thread` que lo convierta en una mutacion disfrazada de
    consulta. Y devuelve `StartupDiagnosis`, que no lleva ningun handle vivo ni
    ninguna disposicion que afirme exito.
    """
    import inspect

    metodo = svc.RuntimeVaultProtectionCoordinator.diagnosticar_arranque
    assert not inspect.iscoroutinefunction(metodo), (
        "diagnosticar_arranque no debe ser async: un await invite al patron to_thread que este slice acaba de eliminar"
    )
    campos = set(svc.StartupDiagnosis.__dataclass_fields__)
    assert campos == {
        "operation_id",
        "route",
        "journal_state",
        "binding_evidence",
        "requiere_privilegios",
        "motivo",
    }
    assert not any("lock" in campo or "handle" in campo for campo in campos)


def test_el_arranque_no_registra_recovery_como_reconciliador_de_rollback() -> None:
    """S4-E NO se cuela en el ancla de orden de los reconciliadores existentes.

    El barrido del Golden va DESPUÉS de U-08 y es independiente: no comparte
    ritual, lock ni base durable con DynDOLOD. Este test documenta que los dos
    mecanismos siguen separados, que es lo que permite que uno falle sin
    arrastrar al otro.

    P4 — este ancla era FALSO-VERDE. Buscaba ``"reconciliar_arranque_pendiente("``
    con ``str.index``, pero ese método ya no existe en el código productivo y la
    única coincidencia era la línea 2061 de ``app_context.py``: un comentario
    histórico que dice "antes este bloque llamaba a ...". El ancla comparaba
    posiciones de TEXTO, así que un comentario satisfacía la evidencia de
    wiring y la comparación de orden no probaba nada sobre el orden real.

    Ahora la evidencia son nodos ``Call``: se exige que exista una llamada
    productiva al diagnóstico read-only, que venga después de la de U-08, y que
    el mutante no pueda volver ni como llamada ni por texto.
    """
    pos_u08 = _call_sites_en_app_context("reconcile_orphan_rollback_backups")
    pos_s4e = _call_sites_en_app_context("diagnosticar_arranque")

    assert pos_u08, (
        "AppContext ya no llama a reconcile_orphan_rollback_backups: el barrido de U-08 "
        "desapareció y este ancla perdió su segundo término"
    )
    assert pos_s4e, (
        "el arranque de S4-E no llama a diagnosticar_arranque: el cableado se "
        "deshizo, y sin él el barrido queda inalcanzable (la clase de fallo "
        "#240/#252/#362)"
    )
    assert min(pos_u08) < min(pos_s4e), (
        f"el barrido de S4-E se movió antes del de U-08: {pos_s4e} vs {pos_u08}. "
        "Los dos son independientes y el orden del existente no es negociable"
    )

    # La mitad que el ancla viejo NO tenía: el mutante no puede volver.
    assert _call_sites_en_app_context("reconciliar_arranque_pendiente") == [], (
        "volvió una llamada a reconciliar_arranque_pendiente en el arranque: eso ejecuta "
        "S4-C/S4-D desde un thread normal, que no es la frontera privilegiada"
    )


def _censo_de_llamadas_productivas(nombres: frozenset[str]) -> dict[str, list[str]]:
    """Censo de llamadas REALES a *nombres* en todo ``sky_claw/``.

    Recorre cada módulo y sólo cuenta nodos ``ast.Call`` cuyo ``func`` resuelva
    a uno de los nombres, por acceso de atributo (``x.resume_golden_protection()``)
    o por nombre desnudo (``resume_golden_protection()``). Todo lo demás queda
    fuera por construcción: definiciones (``FunctionDef``/``AsyncFunctionDef``),
    exports en ``__all__``, imports, cadenas, comentarios y docstrings no son
    nodos ``Call`` y no pueden sumar nada.

    LIMITACIONES DOCUMENTADAS del análisis por AST —las que un censo por texto no
    tiene y conviene conocer:

    * ``import *`` no se resuelve: si un módulo hace ``from x import *`` y llama
      al nombre sin calificar, el censo lo ve como ``ast.Name`` y lo cuenta sólo
      si el nombre calza literalmente. El repo no usa ``import *`` en
      ``sky_claw/`` y el ancla de estilo lo prohíbe.
    * Un alias rebinding (``f = resume_golden_protection``) seguido de ``f()`` no
      se resuelve: el ``func`` es un ``ast.Name`` con otro identificador. Detectar
      eso requiere análisis de flujo de datos, que no es lo que este ancla
      pretende.
    * ``getattr(mod, "resume_golden_protection")(...)`` es invisible para el censo.
    * Un módulo que no parsea NO se omite en silencio: el censo falla, para que
      una omisión sea visible en vez de disfrazarse de "cero call-sites".

    A la luz de estas cuatro limitaciones la propiedad que el ancla sostiene es
    "ninguna llamada PRODUCTIVA con nombre propio", que es exactamente el
    contrato de P4. No pretende ser un verificador de alcanzabilidad.
    """
    # ``_PAQUETE`` es <repo>/sky_claw/local/runtime_vault: parents[2] es <repo>.
    raiz = _PAQUETE.parents[2]
    censo: dict[str, list[str]] = {nombre: [] for nombre in sorted(nombres)}
    for archivo in sorted((raiz / "sky_claw").rglob("*.py")):
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            pytest.fail(
                f"módulo productivo no parseable, el censo lo omitiría en silencio: {archivo.relative_to(raiz)}: {exc}"
            )
        relativo = str(archivo.relative_to(raiz)).replace("\\", "/")
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            func = nodo.func
            nombre = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")
            if nombre in censo:
                censo[nombre].append(f"{relativo}:{nodo.lineno}")
    return censo


def test_ninguna_ruta_productiva_alcanza_la_reconciliacion_mutante() -> None:
    """P4 — la entrada MUTANTE es inalcanzable desde producción, y se enumera.

    El contrato de P4 es ``PRODUCTIVE_RESUME_CALLS = ∅`` mientras
    ``PACKAGED_HELPER_PROVISIONING_STATUS == "UNRESOLVED"``: sin helper
    provisionado no hay frontera privilegiada real, así que nada en el producto
    puede despachar la reconciliación que muta el Golden y sus SDs.

    Se enumera por AST sobre ``sky_claw/`` entero, no sobre un archivo. El
    ancla de cableado congela UN call-site (``AppContext``); ésa no ve una
    llamada que aparezca en el cableado de tool, en un recovery, o en cualquier
    superficie futura, que es exactamente la clase de fallo que P4 cerró. La
    diferencia entre atajar la clase y mostrar una instancia está en el recorrido
    del censo, no en la forma de la aserción.

    Cuando el helper se provisioned y exista la frontera real, este ancla hay
    que reescribirlo para exigir la sesión privilegiada en vez de su ausencia.
    """
    from sky_claw.local.runtime_vault.privileged_boundary import (
        PACKAGED_HELPER_PROVISIONING_STATUS,
    )

    entradas_mutantes = frozenset({"resume_golden_protection", "reconciliar_arranque_pendiente"})
    censo = _censo_de_llamadas_productivas(entradas_mutantes)

    productive = {nombre: sitios for nombre, sitios in censo.items() if sitios}
    assert not productive, (
        "la reconciliación mutante volvió a ser alcanzable desde producción con la "
        f"frontera privilegiada todavía {PACKAGED_HELPER_PROVISIONING_STATUS}: {productive}"
    )


def _call_sites_en_app_context(nombre: str) -> list[int]:
    """Líneas con llamadas REALES a *nombre* en ``app_context.py``.

    AST, y sólo nodos ``Call``: una mención en un comentario, un docstring o una
    cadena no cuenta como wiring. Es la diferencia entre "el arranque lo
    invoca" y "el arranque lo nombró alguna vez".
    """
    import sky_claw.app_context as app_ctx

    arbol = ast.parse(pathlib.Path(app_ctx.__file__).read_text(encoding="utf-8"))
    return sorted(
        nodo.lineno
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and (
            (isinstance(nodo.func, ast.Attribute) and nodo.func.attr == nombre)
            or (isinstance(nodo.func, ast.Name) and nodo.func.id == nombre)
        )
    )


def test_el_resolver_de_programdata_llega_al_discovery_y_al_diagnostico() -> None:
    """El resolver del namespace tiene que llegar a AMBAS mitades del barrido.

    Ancla de una clase de fallo real, ya encontrada y corregida durante este
    slice: el barrido pasaba el resolver a `discover_pending_operations` pero no
    a la segunda mitad. El barrido enumeraba el namespace del RIG y la segunda
    mitad leia `%ProgramData%` PRODUCTIVO. En produccion no se manifiesta
    —ambos default al mismo lugar—, que es exactamente por lo que las dos
    mitades tienen que estar ancladas juntas.

    Es el mismo mecanismo que el repo ya exige para los pares lock/journal: no
    alcanza con cubrir la mitad que se vio.

    P4 consolido las dos superficies de arranque en UNA
    (`diagnosticar_arranque`), asi que el par congelado ahora es
    discovery -> diagnostico.
    """
    vistos: list[object] = []

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

    def resolver() -> str:  # noqa: ANN202 — el seam espera un callable sin argumentos
        return "/rig/programdata"

    monkey = pytest.MonkeyPatch()
    monkey.setattr(svc, "discover_pending_operations", _fake_discover)
    try:
        diagnostico = svc.RuntimeVaultProtectionCoordinator().diagnosticar_arranque(programdata_resolver=resolver)
    finally:
        monkey.undo()

    assert vistos == [("discover", resolver)], vistos
    # Y el diagnostico se produjo con ESA evidencia: el resolver no se perdio.
    assert len(diagnostico) == 1
    assert diagnostico[0].operation_id == _OPERATION_ID


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


def _handle_que_registra() -> Any:
    """Handle con el contrato de ``GoldenMutationLockHandle`` que S4-E usa.

    Registra CADA evento de cierre en orden, para poder distinguir
    ``retain_for_inspection()`` (cierra el kernel handle, NO escribe RELEASED)
    de ``release()`` (cierra y escribe RELEASED).
    """

    class _Handle:
        def __init__(self, operation_id: str = _OPERATION_ID) -> None:
            self.closed = False
            self.eventos: list[str] = []
            # La invariante constructiva de RecoveryContinuation lee
            # `lock.identity.operation_id`: el handle tiene que端口 la
            # identidad, no solo el comportamiento de cerrar. Un handle sin identidad no
            # puede convertirse en autoridad para ninguna operacion.
            self.identity = SimpleNamespace(operation_id=operation_id)

        def retain_for_inspection(self) -> bool:
            if self.closed:
                return False
            self.closed = True
            self.eventos.append("retain")
            return True

        def release(self) -> bool:
            if self.closed:
                return False
            self.closed = True
            self.eventos.append("release")
            return True

    return _Handle()


def _continuacion(handle: Any) -> Any:
    """``RecoveryContinuation`` REAL, con un ``RecoveryForensicReport`` real.

    Se construye el reporte con su constructor real —invariantes incluidas— en
    vez de un objeto fabricated: si el contrato de S4-C cambia, el test falla al
    construir y dice por qué, en vez de ejercitar un shape que ya nadie produce.
    """
    from sky_claw.local.runtime_vault.recovery_orchestrator import (
        RecoveryContinuation,
        RecoveryDisposition,
        RecoveryForensicReport,
        RecoveryIdentitySource,
        RecoveryLockOutcome,
    )

    reporte = RecoveryForensicReport(
        operation_id=_OPERATION_ID,
        plan_classification=DurableWriteOutcome.DURABLE,
        journal_classification=ProtectionJournalClassification.VALID,
        observed_transaction_state=ProtectionTransactionState.APPLYING,
        node_wal_summary=(),
        physical_identity_source=RecoveryIdentitySource.AUTHORIZED_PLAN,
        lock_outcome=RecoveryLockOutcome.ACQUIRED_RETAINED,
        stale_lock_takeover=True,
        disposition=RecoveryDisposition.POST_VERIFICATION_REQUIRED,
        nodes_restored=(),
        nodes_skipped=(),
        nodes_pending=(),
        physical_restoration_completed=False,
        operator_intervention_required=False,
        indeterminate_reason="",
        detail="apply físico completo; el handoff pertenece a S4-D",
        setsecurityinfo_calls=0,
    )
    return RecoveryContinuation(report=reporte, lock=handle)


def test_s4e_cierra_el_handle_si_s4d_retorna_temprano(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 CAUSAL: un retorno temprano de S4-D NO transfiere el ownership.

    El defecto: S4-E marcaba ``ownership tomado`` con cualquier retorno normal
    de S4-D. Pero ``_reanudar_por_s4d`` puede volver ANTES de
    ``_adquirir_lock_para_finalizacion`` — plan ilegible, journal ausente,
    digest incoherente, estado no admisible, normalización COMMITTED. En esos
    casos el handle queda ABIERTO y el flag hacía que el ``finally`` no lo
    cerrara: HANDLE LEAK silencioso.

    El contrato correcto es causal, no inferencial: la evidencia es
    ``handle.closed``. Si S4-D consumió el handle, el kernel handle está
    cerrado; si retornó antes, sigue abierto y el owner sigue siendo S4-E.
    """
    handle = _handle_que_registra()
    assert not handle.closed, "precondición: el handle transferido está VIVO"

    called: list[str] = []

    def _s4d_retorna_temprano(*args: Any, **kwargs: Any) -> Any:
        # Retorna NORMALMENTE sin tocar el handle: es el caso early-return.
        called.append("s4d")
        return svc.ProtectionOutcome(
            operation_id=_OPERATION_ID,
            disposition=svc.ProtectionDisposition.NOT_APPLICABLE,
            stage=svc.ProtectionStage.FINALIZATION,
            source_orchestrator="finalization_orchestrator.finalize_protection_transaction",
        )

    monkeypatch.setattr(svc, "_reanudar_por_s4d", _s4d_retorna_temprano)

    resultado = svc._proyectar_continuacion(_continuacion(handle), svc._DEFAULT_FRONTEND)

    assert called == ["s4d"]
    # El contrato causal: cerrado, y por RETENCIÓN (no RELEASED).
    assert handle.closed is True, "handle leak: S4-E no cerró el handle que S4-D no consumió"
    assert handle.eventos == ["retain"], (
        f"se esperaba retain_for_inspection() (no RELEASED), no release(): {handle.eventos}. "
        "Escribir RELEASED sobre un Golden que S4-D no verificó abre la ventana que todo "
        "este diseño existe para cerrar."
    )
    assert resultado is not None


def test_s4d_no_toca_el_handle_ya_cerrado_por_s4d(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simétrico: si S4-D liberó el handle, S4-E NO lo vuelve a tocar.

    Es el otro lado del contrato causal: si S4-E hiciera ``retain`` sobre un
    handle que S4-D ya liberó, dejaría evidencia de una retención que nadie
    pidió, y el ``release`` de S4-D ya había escrito RELEASED.
    """
    handle = _handle_que_registra()

    def _s4d_consume_y_libera(*args: Any, **kwargs: Any) -> Any:
        kwargs["continuation_lock"].release()
        return svc.ProtectionOutcome(
            operation_id=_OPERATION_ID,
            disposition=svc.ProtectionDisposition.COMMITTED,
            stage=svc.ProtectionStage.FINALIZATION,
            source_orchestrator="finalization_orchestrator.finalize_protection_transaction",
        )

    monkeypatch.setattr(svc, "_reanudar_por_s4d", _s4d_consume_y_libera)

    resultado = svc._proyectar_continuacion(_continuacion(handle), svc._DEFAULT_FRONTEND)

    assert handle.eventos == ["release"], handle.eventos
    assert resultado.disposition is svc.ProtectionDisposition.COMMITTED


def test_la_retencion_del_fallback_es_idempotente() -> None:
    """``retain_for_inspection`` sobre un handle ya cerrado es no-op.

    S4-E llama a la retencion desde un ``finally``, que puede ejecutarse
    despues de que S4-D ya cerro el handle. Que sea no-op es lo que hace
    segura la combinacion, asi que el comportamiento se congela.
    """
    handle = _handle_que_registra()
    assert handle.retain_for_inspection() is True
    assert handle.retain_for_inspection() is False
    assert handle.retain_for_inspection() is False
    assert handle.eventos == ["retain"], handle.eventos


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
    """Contrato observable del token: cierre contado, para cazar double-close."""

    def __init__(self) -> None:
        self.cerrado = False
        self.cierres = 0

    def close(self) -> bool:
        self.cierres += 1
        self.cerrado = True
        return True


class _SesionFalsa:
    """Específicamente NO es una PrivilegedBoundarySession real.

    Existe para observar la SECUENCIA de eventos de la frontera sin abrir un
    handle Win32. ``apply_authorized_plan`` y
    ``finalize_protection_transaction`` están monkeypatcheados en el test, así
    que ninguno la valida — y para esta propiedad esa validación sería
    irrelevante: lo que importa es que el mismo objeto llegue a los dos.

    Modela el contrato REAL de la sesión tras P5: el lifecycle del lock y del
    token lo decide la sesión, no el llamador. Un fake con un ``close()`` que
    sólo tocaba el lock dejaba pasar la vuelta del anti-pattern que P5 cerró.
    """

    def __init__(self) -> None:
        self.lock = _HandleFalso()
        self.operator_token = _TokenFalso()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> bool:
        """Terminal limpio: RELEASED + token cerrado, exactly once."""
        if self._closed:
            return False
        self._closed = True
        self.lock.release()
        self.operator_token.close()
        return True

    def close_retaining_lock(self) -> bool:
        """Fail-closed: metadata sin RELEASED, token cerrado, exactly once."""
        if self._closed:
            return False
        self._closed = True
        self.lock.retain_for_inspection()
        self.operator_token.close()
        return True


def _parchar_publicacion(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Sustituye la publicación en disco por un recorder.

    Los tests de COMPOSICION no proban la escritura a disco — eso vive en
    `test_runtime_vault_s4e_windows_rig.py` y en el anchor dedicado de
    `publish_candidate_manifest`. Aquí importa que S4-E publica los bytes
    canónicos ANTES de abrir la frontera, y eso sí se registra.
    """
    publicados: list[bytes] = []

    def _fake_publish(operation_id: str, manifest_bytes: bytes, **kwargs: Any) -> None:
        publicados.append(manifest_bytes)

    monkeypatch.setattr(svc, "publish_candidate_manifest", _fake_publish)
    return publicados


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
        return _JournalFalso()

    def _fake_apply(*, journal: Any, session: Any, **kwargs: Any) -> Any:
        vistos.append(("apply", id(session), sesion.lock.closed))
        return _ReporteApplyFalso()

    def _fake_finalize(*, journal: Any, port: Any, **kwargs: Any) -> Any:
        vistos.append(("finalize", id(sesion), sesion.lock.closed))
        return _ReporteFinalizacionFalso()

    monkeypatch.setattr(svc, "orchestrate_golden_protection_planning", _planning_exitoso)
    _parchar_publicacion(monkeypatch)
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
    _parchar_publicacion(monkeypatch)
    monkeypatch.setattr(svc, "establish_privileged_authorization", lambda **k: (object(), sesion))
    monkeypatch.setattr(svc, "promote_durable_authorized_plan", lambda **k: object())
    monkeypatch.setattr(svc, "create_protection_journal", lambda *a, **k: _JournalFalso())
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
    _parchar_publicacion(monkeypatch)
    monkeypatch.setattr(svc, "establish_privileged_authorization", lambda **k: (object(), sesion))
    monkeypatch.setattr(svc, "promote_durable_authorized_plan", lambda **k: object())
    monkeypatch.setattr(svc, "create_protection_journal", lambda *a, **k: _JournalFalso())
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
    """``_cerrar_frontera`` dos veces no cierra dos veces.

    Se usa la API real de la sesión: ``closed`` es una property de lectura, así
    que el patrón viejo del test —asignarle ``True`` a mano— ya no es posible.
    Eso es parte del arreglo, no un obstáculo: el estado de cierre sólo lo
    escribe la sesión, y por eso no puede quedar incoherente con sus recursos.

    El segundo cierre es un no-op porque la sesión ya está cerrada, y el
    ``False`` que devuelve ``_cerrar_frontera`` significa "no quedó retenido"
    — correcto para una sesión que ya no tiene lock que retener.
    """
    sesion = _SesionFalsa()

    assert svc._cerrar_frontera(sesion, retener_lock=False) is False
    assert sesion.lock.eventos == ["release"]

    # Segunda llamada: la sesión ya está cerrada, no se toca nada más.
    assert svc._cerrar_frontera(sesion, retener_lock=False) is False
    assert sesion.lock.eventos == ["release"], "double-close del lock"
    assert sesion.operator_token.cierres == 1, "double-close del token"

    # Y la variante que retiene tampoco reabre nada.
    otra = _SesionFalsa()
    assert svc._cerrar_frontera(otra, retener_lock=True) is True
    assert otra.lock.eventos == ["retain"]
    assert svc._cerrar_frontera(otra, retener_lock=True) is False, (
        "una sesión ya cerrada no retiene nada: no hay lock que retener"
    )
    assert otra.lock.eventos == ["retain"], "double-close en el camino de retención"


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
    _parchar_publicacion(monkeypatch)
    monkeypatch.setattr(svc, "establish_privileged_authorization", lambda **k: (object(), sesion))
    monkeypatch.setattr(svc, "promote_durable_authorized_plan", lambda **k: object())
    monkeypatch.setattr(svc, "create_protection_journal", lambda *a, **k: _JournalFalso())
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
    monkeypatch.setattr(svc, "open_protection_journal", lambda *a, **k: _JournalFalso())
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
        # El router de S4-E distingue ABSENT (pre-plan) de NO-INTERPRETABLE, asi
        # que el fake tiene que exponer classification. Sin ella el test
        # fallaba con AttributeError y no estaba probando el contrato.
        classification = ProtectionJournalClassification.INDETERMINATE
        is_valid = False
        detail = "torn tail"
        journal = None

    for nombre in (
        "recover_interrupted_protection_for_continuation",
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
        "recover_interrupted_protection_for_continuation",
        lambda *a, **k: llamadas.append("s4c") or (object(), None),
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

    class _PlanSellado:
        """Lo mínimo que S4-E lee del plan sellado.

        Antes de P2 el fresh path no tocaba ``sealed_plan``, así que el fake
        era ``object()``. Con la publicación del candidate manifest, S4-E lee
        ``candidate_manifest_bytes`` — y seguir mintiendo acá habría dejado el
        camino fresco entero sin ejercitar.
        """

        def __init__(self) -> None:
            self.candidate_manifest_bytes = b'{"operation_id":"fake","nodos":[]}'
            self.staging_digest = "0" * 64

    return SimpleNamespace(
        disposition=GP2PlanningDisposition.PREPARED,
        success=True,
        message="",
        sealed_plan=_PlanSellado(),
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


class _JournalFalso:
    """Journal con el CONTRATO REAL de `DurableProtectionJournal`.

    S4-E ahora cierra el journal en `finally` (es un handle de Win32 con
    share mode exclusivo), así que el fake tiene que exponer `is_closed` y
    `close()` de verdad. Un `object()` genérico dejaba que el test pasara
    mientras el código de producción fallaba con AttributeError.
    """

    def __init__(self) -> None:
        self._cerrado = False
        self.cierres = 0

    @property
    def is_closed(self) -> bool:
        return self._cerrado

    def close(self) -> None:
        self.cierres += 1
        self._cerrado = True


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

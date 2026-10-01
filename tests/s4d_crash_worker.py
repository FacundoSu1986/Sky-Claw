"""Worker de crash REAL para el RIG GP2-S4D.

Escribe evidencia durable con las primitivas reales del paquete, ejecuta S4-D
con un port que observa el árbol FÍSICO descartable del RIG, y muere con
``os._exit`` en el punto exacto que el controller le pide.

El crash es REAL a propósito. Una excepción Python hace unwind, ejecuta
``finally``, cierra handles y deja el proceso vivo: el "recovery" sería una
simulación. Lo que hay que demostrar es que un proceso NUEVO, sin memoria de
nada, decide bien a partir de bytes.

Cómo se produce cada punto de muerte SIN hooks de test en producción
--------------------------------------------------------------------
El worker NO instrumenta el orquestador. Avanza el journal durable por las
mismas transiciones que usaría S4-D (``enter_finalization_phase``) y muere
después de la escritura. Eso produce exactamente el estado que dejaría un crash
en esa fase, y deja la Guarantee en un sitio solo: el código de producción.

La reanudación es un proceso NUEVO que reabre plan y journal desde disco y no
sabe nada del proceso anterior — tampoco necesita: el journal durable dice
``VERIFYING_GP1`` y eso basta.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import pathlib
import sys
import time
from ctypes import wintypes
from typing import Any

from sky_claw.local.runtime_vault.authorized_plan import (
    AUTHORIZED_PLAN_SCHEMA_VERSION,
    AuthorizedPlan,
    serialize_authorized_plan,
)
from sky_claw.local.runtime_vault.authorized_plan_store import (
    _MINT_PROOF,
    DurableAuthorizedPlan,
    derive_authorized_plan_path,
    load_durable_authorized_plan,
)
from sky_claw.local.runtime_vault.finalization_orchestrator import (
    FinalizationDisposition,
    GateVerdict,
    finalize_protection_transaction,
)
from sky_claw.local.runtime_vault.golden_backup_archive import (
    GoldenBackupDurableWriter,
    archive_golden_backup,
)
from sky_claw.local.runtime_vault.golden_mutation_lock import (
    GoldenLockMetadata,
    GoldenLockPhase,
    derive_golden_lock_key,
    derive_golden_lock_path,
    serialize_golden_lock_metadata,
)
from sky_claw.local.runtime_vault.golden_protection_plan import NodeSecurityBackup
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.node_evidence import probe_node_evidence
from sky_claw.local.runtime_vault.protection_journal import (
    ProtectionTransactionState,
    parse_journal_bytes,
)
from sky_claw.local.runtime_vault.protection_journal_store import (
    derive_protection_journal_path,
    open_protection_journal,
)
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

#: PID que Windows jamás asigna: garantiza "dueño muerto" sin depender de timing
#: ni de haber matado a nadie.
_PID_IMPOSIBLE = 0x7FFFFFFF
_CREATION_TIME_FICTICIA = 140_000_000_000_000_000
_EPOCH_FICTICIO = 1_758_499_100

_PREPARAR = "preparar"
_COMPLETA = "completa"
_SEMILLA = "semilla"
#: Semillas por estado durable al que se debe dejar el journal antes de morir.
_SEMILLAS = {
    "verifying_gp1": ProtectionTransactionState.VERIFYING_GP1,
    "verifying_rv2": ProtectionTransactionState.VERIFYING_RV2,
    "verifying_node_set": ProtectionTransactionState.VERIFYING_NODE_SET,
    "archiving_backup": ProtectionTransactionState.ARCHIVING_BACKUP,
}
#: Estados previos a una semilla, en orden de arista.
_TRAMO_SEMILLA = {
    "verifying_gp1": (),
    "verifying_rv2": (ProtectionTransactionState.VERIFYING_GP1,),
    "verifying_node_set": (
        ProtectionTransactionState.VERIFYING_GP1,
        ProtectionTransactionState.VERIFYING_RV2,
    ),
    "archiving_backup": (
        ProtectionTransactionState.VERIFYING_GP1,
        ProtectionTransactionState.VERIFYING_RV2,
        ProtectionTransactionState.VERIFYING_NODE_SET,
    ),
}

_CODIGO_MUERTE = 60


def _resolver(rig: pathlib.Path) -> Any:
    return lambda: rig / "programdata"


def _log(rig: pathlib.Path, evento: str) -> None:
    """Breadcrumb con PID: el controller lo usa para muerto-vivo y causalidad."""
    with (rig / "breadcrumbs.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()} {evento}\n")
        fh.flush()
        os.fsync(fh.fileno())


def _morir(rig: pathlib.Path, evento: str, codigo: int = _CODIGO_MUERTE) -> None:
    _log(rig, evento)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(codigo)


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ============================================================================
# Estado durable: plan, journal y lock huérfano
# ============================================================================


def _identidad_del_arbol(golden: pathlib.Path) -> tuple[int, int]:
    for evidencia in probe_node_evidence(str(golden)):
        if evidencia.backup.relative_path == ".":
            return (evidencia.backup.volume_serial_number, evidencia.backup.file_id)
    raise SystemExit("el RIG no tiene nodo raíz")


def _tree_digest_real(golden: pathlib.Path) -> TreeDigest:
    """TreeDigest del árbol REAL, no un valor inventado.

    Es lo que hace que el RV-2 final sea una comparación y no un tautología: si
    el controller muta un byte del Golden, este digest deja de coincidir con el
    que quedó congelado en el plan durable.
    """
    return tree_digest_from_files(inventory_tree(golden))


def _plan_real(golden: pathlib.Path, operation_id: str) -> AuthorizedPlan:
    """Plan autoritativo derivado del árbol físico, con los PRE SD reales.

    Los PRE SD salen de ``probe_node_evidence``: son los Security Descriptors
    que el árbol tiene AHORA, que es exactamente lo que un GP3 necesitaría para
    restaurar. Que sean reales, y no constantes de test, es lo que hace que el
    round-trip del backup sea comprobable.
    """
    volumen, file_id = _identidad_del_arbol(golden)
    nodos = tuple(
        NodeSecurityBackup(
            relative_path=evidencia.backup.relative_path,
            node_kind=evidencia.backup.node_kind,
            volume_serial_number=evidencia.backup.volume_serial_number,
            file_id=evidencia.backup.file_id,
            pre_sd_bytes_b64=evidencia.backup.pre_sd_bytes_b64,
            pre_sd_length=evidencia.backup.pre_sd_length,
            pre_sd_sha256=evidencia.backup.pre_sd_sha256,
            owner_sid=evidencia.backup.owner_sid,
            group_sid=evidencia.backup.group_sid,
            dacl_control_flags=evidencia.backup.dacl_control_flags,
            pre_dacl_protected_flag=evidencia.backup.pre_dacl_protected_flag,
            sddl_diagnostic=evidencia.backup.sddl_diagnostic,
        )
        for evidencia in probe_node_evidence(str(golden))
    )
    from sky_claw.local.runtime_vault.authorization_context import OperatorTokenEvidence

    return AuthorizedPlan(
        schema_version=AUTHORIZED_PLAN_SCHEMA_VERSION,
        operation_id=operation_id,
        canonical_root=str(golden).upper(),
        volume_serial_number=volumen,
        root_file_id=file_id,
        tree_digest=_tree_digest_real(golden),
        node_count=len(nodos),
        policy_version="gp2-s4d-rig",
        staging_digest="d" * 64,
        operator_identity=OperatorTokenEvidence(
            operator_sid="S-1-5-18",
            token_type="primary",
            # El enum es CERRADO y valida: no se puede inventar una procedencia
            # "de RIG". Lo honesto es la procedencia real de este proceso, que
            # extrae su token del coordinador de la misma cuenta. Se anota que
            # el SID real es el del usuario del RIG, no SYSTEM.
            acquired_via="same_account_coordinator_extraction",
        ),
        nodes=nodos,
    )


def _plan_durable(rig: pathlib.Path, operation_id: str) -> DurableAuthorizedPlan:
    golden = rig / "golden"
    plan = _plan_real(golden, operation_id)
    ruta = pathlib.Path(str(derive_authorized_plan_path(operation_id, programdata_resolver=_resolver(rig))))
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(serialize_authorized_plan(plan))
    return DurableAuthorizedPlan(plan, ruta, _proof=_MINT_PROOF)


def _sembrar_lock_huerfano(plan: DurableAuthorizedPlan, programdata_resolver: Any) -> None:
    """Lock huérfano de la MISMA operación, con dueño imposible.

    Es el estado que S4-C deja: lock retenido, dueño que ya no existe. S4-D
    tiene que poder reclamarlo por ``operation_id``, y nadie más.
    """
    ruta = pathlib.Path(
        str(
            derive_golden_lock_path(
                plan.volume_serial_number,
                plan.root_file_id,
                programdata_resolver=programdata_resolver,
            )
        )
    )
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(
        serialize_golden_lock_metadata(
            GoldenLockMetadata(
                lock_key=derive_golden_lock_key(plan.volume_serial_number, plan.root_file_id),
                operation_id=plan.operation_id,
                owner_pid=_PID_IMPOSIBLE,
                owner_process_creation_time=_CREATION_TIME_FICTICIA,
                session_id=1,
                created_at=_EPOCH_FICTICIO,
                phase=GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            )
        )
    )


def _aprovisionar_scope_de_backup(plan: DurableAuthorizedPlan, programdata_resolver: Any) -> None:
    """Crea el directorio del backup: el trabajo del NAMESPACE, no del store.

    El store de S4-D falla cerrado si el padre no existe, y con razón: crear ese
    directorio implicaría decidir su DACL desde la finalización, que es
    jurisdicción del namespace de confianza. El RIG hace aquí lo que haría el
    bootstrap —crear la cadena de directorios— para poder ejercitar el resto.

    La DACL canónica de ese directorio NO se prueba acá: exigiría proprietor
    SYSTEM y por lo tanto elevación, que el RIG evita. Queda declarado.
    """
    from sky_claw.local.runtime_vault.golden_backup_archive import derive_golden_backup_path

    destino = pathlib.Path(str(derive_golden_backup_path(plan.plan, programdata_resolver=programdata_resolver)))
    destino.parent.mkdir(parents=True, exist_ok=True)
    _log(rig=programdata_resolver(), evento=f"scope_de_backup_aprovisionado:{destino.parent.name}")


def _dejar_todos_mutated(plan: DurableAuthorizedPlan, programdata_resolver: Any) -> Any:
    """Journal con TODOS los nodos MUTATED durable: la precondición de S4-D.

    S4-B no se reimplementa. El RIG parte del estado durable que S4-C dejaría
    (``POST_VERIFICATION_REQUIRED`` con todos los nodos aplicados), que es lo que
    el ADR declara como punto de entrada de S4-D.

    La CREACIÓN del journal usa bytes canónicos escritos directamente, y a
    partir de ahí el flujo usa el kernel REAL (``_Win32JournalKernel``:
    write-through + ``FlushFileBuffers`` verificado). Es el mismo criterio que
    el RIG de S4-C: la publicación create-once de producción usa
    ``CreateHardLinkW`` sobre un directorio cuyo owner es SYSTEM, y eso exige el
    namespace de confianza y elevación — que este RIG no debe tocar. Lo que S4-D
    necesita Demonstrate es la SECUENCIA de durabilidad y la recuperación, y ésas
    sí corren contra la primitiva real.
    """
    _crear_journal_fixture(plan, programdata_resolver)
    journal = open_protection_journal(plan.operation_id, plan, programdata_resolver=programdata_resolver)
    for node in plan.plan.nodes:
        binding = journal.node_binding(node.relative_path)
        permit = journal.record_node_mutation_intent(binding)
        permit.mark_consumed()
        journal.record_node_mutation_completed(binding)
    _sembrar_lock_huerfano(plan, programdata_resolver)
    return journal


def _crear_journal_fixture(plan: DurableAuthorizedPlan, programdata_resolver: Any) -> None:
    """Header + estado inicial del journal, con bytes canónicos reales."""
    from sky_claw.local.runtime_vault.protection_journal import (
        INITIAL_TRANSACTION_STATE,
        JournalTransactionRecord,
        ProtectionTransactionState,
        build_journal_header_record,
        serialize_journal_record,
    )

    destino = pathlib.Path(
        str(derive_protection_journal_path(plan.operation_id, programdata_resolver=programdata_resolver))
    )
    destino.parent.mkdir(parents=True, exist_ok=True)
    cabecera = build_journal_header_record(
        operation_id=plan.plan.operation_id,
        authorized_plan_digest=plan.plan.plan_digest,
        canonical_root=plan.canonical_root,
        volume_serial_number=plan.volume_serial_number,
        root_file_id=plan.root_file_id,
        created_at="2026-10-01T00:00:00Z",
    )
    inicial = JournalTransactionRecord(sequence=2, state=ProtectionTransactionState(INITIAL_TRANSACTION_STATE))
    destino.write_bytes(serialize_journal_record(cabecera) + serialize_journal_record(inicial))


# ============================================================================
# Writer de backup: create-once REAL sobre el filesystem del RIG
# ============================================================================


class _WriterRIG(GoldenBackupDurableWriter):
    """Create-once real (``O_EXCL``), flush y relectura.

    Reimplementa la SECUENCIA —que es la propiedad que S4-D necesita
    demostrar— y no la primitiva Win32 del namespace de confianza:，写 ésa
    exige proprietor SYSTEM y un namespace real, que el RIG no debe tocar. La
    relectura y el digest los hace el store, no el writer, así que la
    verificación de integridad que S4-D exercise es la de producción.
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        descriptor = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_BINARY)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


# ============================================================================
# Port de verificación sobre el árbol FÍSICO
# ============================================================================


class _PortFisico:
    """Observa el Golden real del RIG.

    RV-2 y NodeSet son REALES: recalculan el TreeDigest y re-observan la
    identidad física de cada nodo sobre el árbol de verdad. GP1 y quiescence se
    reportan como PASS DEGADADO y lo dicen en el ``detail``: endurecer el árbol
    o abrirle handles de escritura exigiría elevación y un Golden real, que este
    RIG prohíbe. Queda declarado en el reporte y en el PR, no enterrado.

    Cada gate deja un breadcrumb con el PID del proceso que lo observ��. Eso es
    lo que permite al controller comprobar que el proceso B de verdad re-observó
    y no consultó el resultado de A.
    """

    def __init__(self, golden: pathlib.Path, rig: pathlib.Path, *, quiescence_real: bool) -> None:
        self.golden = golden
        self.rig = rig
        self.quiescence_real = quiescence_real
        self.llamadas: list[str] = []

    def observar_gp1(self, *, raiz: str, esperado: str) -> GateVerdict:
        self.llamadas.append("gp1")
        _log(self.rig, "gate:gp1")
        detalle = "GP1 no ejercido en el RIG: el árbol descartable no está endurecido (requiere elevación)"
        return GateVerdict(gate="gp1", passed=True, detail=detalle, evidence_digest=_sha(f"gp1:{detalle}"))

    def observar_rv2(self, *, raiz: str, tree_digest_esperado: TreeDigest) -> GateVerdict:
        self.llamadas.append("rv2")
        _log(self.rig, "gate:rv2")
        try:
            observado = _tree_digest_real(self.golden)
        except Exception as exc:  # noqa: BLE001 - el veredicto lo registra y S4-D decide
            detalle = f"inventario falló: {type(exc).__name__}: {exc}"
            return GateVerdict(gate="rv2", passed=False, detail=detalle, evidence_digest=_sha(f"rv2:{detalle}"))
        if observado != tree_digest_esperado:
            detalle = (
                f"TreeDigest difiere del plan durable: digest {observado.digest[:12]} vs "
                f"{tree_digest_esperado.digest[:12]}, files {observado.files} vs "
                f"{tree_digest_esperado.files}, bytes {observado.bytes} vs {tree_digest_esperado.bytes}"
            )
            return GateVerdict(gate="rv2", passed=False, detail=detalle, evidence_digest=_sha(f"rv2:{detalle}"))
        return GateVerdict(
            gate="rv2",
            passed=True,
            detail=f"RV-2 VERIFIED sobre el árbol físico (digest={observado.digest[:12]}…)",
            evidence_digest=_sha(f"rv2:{observado.digest}"),
        )

    def observar_node_set(self, *, raiz: str, nodos_autorizados: tuple[Any, ...]) -> GateVerdict:
        self.llamadas.append("node_set")
        _log(self.rig, "gate:node_set")
        try:
            evidencia = probe_node_evidence(str(self.golden))
        except Exception as exc:  # noqa: BLE001
            detalle = f"probe de nodos falló fail-closed: {type(exc).__name__}: {exc}"
            return GateVerdict(gate="node_set", passed=False, detail=detalle, evidence_digest=_sha(f"ns:{detalle}"))
        observado = {
            (item.backup.relative_path, item.backup.node_kind, item.backup.volume_serial_number, item.backup.file_id)
            for item in evidencia
        }
        esperado = {(n.relative_path, n.node_kind, n.volume_serial_number, n.file_id) for n in nodos_autorizados}
        if observado != esperado:
            faltantes = sorted(item[0] for item in esperado - observado)
            sobrantes = sorted(item[0] for item in observado - esperado)
            detalle = f"NodeSet difiere por identidad física: faltantes={faltantes[:6]} sobrantes={sobrantes[:6]}"
            return GateVerdict(gate="node_set", passed=False, detail=detalle, evidence_digest=_sha(f"ns:{detalle}"))
        return GateVerdict(
            gate="node_set",
            passed=True,
            detail=f"NodeSet idéntico sobre {len(observado)} nodos por identidad física",
            evidence_digest=_sha(f"ns:{len(observado)}"),
        )

    def observar_quiescence(self, *, raiz: str, nodos_autorizados: tuple[Any, ...]) -> GateVerdict:
        self.llamadas.append("quiescence")
        _log(self.rig, "gate:quiescence")
        if not self.quiescence_real:
            detalle = "quiescence no ejercida en el RIG salvo en el escenario con handle bloqueante"
            return GateVerdict(gate="quiescence", passed=True, detail=detalle, evidence_digest=_sha(f"q:{detalle}"))
        # Éste es el escenario W-D07: el probe REAL de quiescence, con un handle
        # abierto que lo bloquea. Es la misma primitiva del ADR, con la misma
        # política de reintentos, y por eso tarda: MAX_PROBE_RETRIES intentos
        # con backoff antes de rendirse.
        from sky_claw.local.runtime_vault.quiescence import (
            DEFAULT_BASE_BACKOFF_SECONDS,
            MAX_PROBE_RETRIES,
            QuiescenceError,
            QuiescenceViolationError,
            probe_tree_quiescence,
        )

        try:
            probe_tree_quiescence(
                pathlib.Path(raiz),
                nodos_autorizados,
                max_attempts=MAX_PROBE_RETRIES,
                base_backoff_seconds=DEFAULT_BASE_BACKOFF_SECONDS,
            )
        except QuiescenceViolationError as exc:
            detalle = f"quiescence real: {len(exc.blocked_paths)} nodo(s) bloqueado(s): {list(exc.blocked_paths)[:4]}"
            return GateVerdict(gate="quiescence", passed=False, detail=detalle, evidence_digest=_sha(f"q:{detalle}"))
        except QuiescenceError as exc:
            detalle = f"quiescence real no concluyente: {type(exc).__name__}: {exc}"
            return GateVerdict(gate="quiescence", passed=False, detail=detalle, evidence_digest=_sha(f"q:{detalle}"))
        detalle = f"quiescence real: sin nodos bloqueantes tras {MAX_PROBE_RETRIES} intentos"
        return GateVerdict(gate="quiescence", passed=True, detail=detalle, evidence_digest=_sha(f"q:{detalle}"))


# ============================================================================
# Handle que bloquea la quiescence (W-D07)
# ============================================================================

_GENERIC_READ = 0x80000000
_SHARE_NONE = 0x0
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80


def _abrir_handle_bloqueante(destino: pathlib.Path) -> int:
    """Abre el Golden con ``dwShareMode=0`` y lo mantiene abierto.

     Con share=0, cualquier segunda apertura —incluida la del probe de
     quiescence— recibe ``ERROR_SHARING_VIOLATION``. Es el caso H1-H6 del ADR
    detallada en §4.1.1, con un proceso real sosteniendo el handle.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.CreateFileW(
        str(destino),
        _GENERIC_READ,
        _SHARE_NONE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == wintypes.HANDLE(-1).value or handle is None:
        raise OSError(f"no se pudo abrir en modo exclusivo: {destino} (winerr={ctypes.get_last_error()})")
    return int(handle)


# ============================================================================
# Fases
# ============================================================================


def _estado_durable(rig: pathlib.Path, operation_id: str) -> Any:
    return parse_journal_bytes(
        derive_protection_journal_path(operation_id, programdata_resolver=_resolver(rig)).read_bytes()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Worker de crash del RIG GP2-S4D")
    parser.add_argument("--rig-root", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument(
        "--phase",
        required=True,
        choices=[_PREPARAR, _COMPLETA, _SEMILLA, "quiescence_bloqueada"],
    )
    parser.add_argument(
        "--destino",
        default="",
        help="Estado durable objetivo cuando --phase=semilla (ver TRAMO_SEMILLA)",
    )
    parser.add_argument("--publicar-backup", action="store_true", help="Publica el backup antes de morir")
    args = parser.parse_args(argv)

    rig = pathlib.Path(args.rig_root)
    operation_id = args.operation_id
    programdata = _resolver(rig)
    golden = rig / "golden"
    _log(rig, f"inicio:{args.phase}")

    # ------------------------------------------------------------------
    # Preparación: plan durable + journal con todos los nodos MUTATED + lock
    # huérfano de la misma operación. Es el estado que S4-C deja.
    # ------------------------------------------------------------------
    if args.phase == _PREPARAR:
        plan = _plan_durable(rig, operation_id)
        _aprovisionar_scope_de_backup(plan, programdata)
        _dejar_todos_mutated(plan, programdata)
        _log(rig, "preparado")
        print(plan.plan.canonical_root)
        return 0

    # ------------------------------------------------------------------
    # Reanudación: proceso NUEVO. Reabre plan y journal desde disco y nada más.
    # ------------------------------------------------------------------
    plan = load_durable_authorized_plan(operation_id, programdata_resolver=programdata)
    journal = open_protection_journal(operation_id, plan, programdata_resolver=programdata)
    leido = _estado_durable(rig, operation_id)
    _log(rig, f"estado_durable:{leido.journal.transaction_state.value if leido.journal else 'indeterminate'}")

    # ------------------------------------------------------------------
    # Semillas: avanzar el journal durable a un estado y QUEDARSE VIVO.
    #
    # Esto reproduce D01/D02/D04/D06/D08 sin instrumentar el orquestador: el
    # estado durable que queda es el mismo que dejaría un crash ahí, y ningún
    # camino de producción tiene un hook de test.
    #
    # El worker NO se suicida: escribe, avisa por breadcrumb y se queda
    # esperando. Lo mata el controller con `taskkill /T /F`, o sea un
    # `TerminateProcess` EXTERNO de un proceso VIVO. Eso es más fuerte que un
    # `os._exit`: mata el árbol entero, no ejecuta ningún `finally`, ningún
    # `atexit` y ningún flush implícito. Es lo que pasa cuando se apaga la
    # máquina.
    # ------------------------------------------------------------------
    if args.phase == _SEMILLA:
        for fase in _TRAMO_SEMILLA[args.destino]:
            journal.enter_finalization_phase(fase)
        journal.enter_finalization_phase(_SEMILLAS[args.destino])
        if args.publicar_backup:
            escritura = archive_golden_backup(plan, programdata_resolver=programdata, writer=_WriterRIG())
            _log(rig, f"backup_publicado:{escritura.durable.archive_digest[:16]}")
        _log(rig, f"semilla:{args.destino}")
        _log(rig, "vivo:esperando_taskkill")
        # Parqueado para siempre: el controller lo mata. Sin esto, el proceso
        # terminaría solo y estaríamos probando un `os._exit`, no un crash.
        while True:
            time.sleep(0.25)
        return 4  # inalcanzable

    # ------------------------------------------------------------------
    # W-D07: un handle abierto que bloquea la quiescence final.
    # ------------------------------------------------------------------
    if args.phase == "quiescence_bloqueada":
        objetivo = golden / "Data" / "Skyrim.esm"
        if not objetivo.exists():
            objetivo = golden / "Skyrim.esm"
        handle = _abrir_handle_bloqueante(objetivo)
        _log(rig, f"handle_bloqueante:{objetivo.name}")
        reporte = finalize_protection_transaction(
            operation_id=operation_id,
            plan=plan,
            journal=journal,
            port=_PortFisico(golden, rig, quiescence_real=True),
            programdata_resolver=programdata,
            archive_writer=_WriterRIG(),
        )
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
        _log(rig, f"desenlace:{reporte.disposition.value}")
        print(f"{reporte.disposition.value}|{reporte.lock.released}|{reporte.archive_digest or '-'}")
        return 0 if reporte.disposition is FinalizationDisposition.COMMITTED else 3

    # ------------------------------------------------------------------
    # W-D01: S4-D completo, de punta a punta.
    # ------------------------------------------------------------------
    reporte = finalize_protection_transaction(
        operation_id=operation_id,
        plan=plan,
        journal=journal,
        port=_PortFisico(golden, rig, quiescence_real=False),
        programdata_resolver=programdata,
        archive_writer=_WriterRIG(),
    )
    _log(rig, f"desenlace:{reporte.disposition.value}")
    _log(rig, f"gates:{','.join(veredicto.gate for veredicto in reporte.verdicts)}")
    print(f"{reporte.disposition.value}|{reporte.lock.released}|{reporte.archive_digest or '-'}")
    return 0 if reporte.disposition is FinalizationDisposition.COMMITTED else 3


if __name__ == "__main__":
    raise SystemExit(main())

"""Worker del RIG S4-E — SÓLO existe para `test_runtime_vault_s4e_windows_rig.py`.

Corre primitivas REALES del Runtime Vault contra un namespace aislado bajo
``%TEMP%`` y muere con ``os._exit`` en el punto durable exacto que el
controller le pide. El controller lo mata con ``taskkill /T /F`` cuando quiere
un crash externo real.

Qué es REAL y qué está DEGRADADO
---------------------------------

REAL: ``GoldenMutationLock`` Win32 (share mode 0, metadata durable),
``DurableAuthorizedPlan``, ``ProtectionJournal`` con flush verificado, S4-B
(``apply_authorized_plan`` sobre ``HandleBoundTargetDaclPort`` →
``SetSecurityInfo``), S4-C (``recover_interrupted_protection``), S4-D
(``finalize_protection_transaction``, archivado del backup, ``COMMITTED``), el
router de S4-E y toda la evidencia durable en disco.

DEGRADADO: GP1/quiescence usan un puerto de observación controlado, y la
frontera de S4-A (elevación + PPSC) no corre. **Ninguno de los dos es
validación nativa** y el RIG no lo afirma. Lo que sí queda probado es la
composición, el orden durable, la continuidad del lock y la recuperación ante
crash, que es el alcance de S4-E.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import uuid
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

CODIGO_OK = 0

#: Punto de muerte dura → código de salida. El controller compara el código.
CRASH_EXIT: dict[str, int] = {
    "pre_journal": 42,
    "mid_apply": 43,
    "all_mutated": 44,
    "post_committed_pre_release": 49,
    "pre_plan": 41,
}


def _abortar_si_raiz_peligrosa(rig_root: pathlib.Path) -> None:
    """Guard duro contra tocar un Golden, Steam, MO2 o ProgramData reales."""
    raiz = str(rig_root.resolve()).lower()
    base = str(pathlib.Path(tempfile.gettempdir()).resolve()).lower()
    if not raiz.startswith(base):
        raise RuntimeError(f"El RIG S4-E debe vivir bajo TEMP; se pasó {rig_root}")
    for prohibido in (
        "skyrim",
        "steam",
        "mod organizer",
        "modorganizer",
        ".mo2",
        "program files",
        "programdata",
    ):
        if prohibido in raiz:
            raise RuntimeError(f"El RIG S4-E rechaza rutas prohibidas: '{prohibido}' en {rig_root}")


def _resolver(rig_root: pathlib.Path) -> Any:
    return lambda: rig_root / "programdata"


class _Miga:
    """NONCE por corrida: los breadcrumbs nunca aceptan eventos históricos."""

    def __init__(self, rig_root: pathlib.Path) -> None:
        self.nonce = uuid.uuid4().hex
        self.ruta = rig_root / "s4e-events.log"

    def marcar(self, evento: str) -> None:
        with self.ruta.open("a", encoding="utf-8") as fh:
            fh.write(f"{self.nonce}|{evento}\n")
            fh.flush()
            os.fsync(fh.fileno())

    def tiene(self, prefijo: str) -> bool:
        if not self.ruta.exists():
            return False
        return any(
            linea.split("|", 1)[1].startswith(prefijo)
            for linea in self.ruta.read_text(encoding="utf-8").splitlines()
            if linea.startswith(f"{self.nonce}|")
        )


def _crash(codigo: int) -> None:
    """Muerte dura de ESTE proceso: no hay finally, ni __aexit__, ni flush."""
    os._exit(codigo)


def _crear_arbol(rig_root: pathlib.Path) -> pathlib.Path:
    base = rig_root / "golden"
    (base / "Data").mkdir(parents=True, exist_ok=True)
    (base / "Data" / "Skyrim.esm").write_bytes(b"esm-descartable-s4e")
    return base


def _plan_durable(rig_root: pathlib.Path, operation_id: str) -> Any:
    """Plan durable REAL, con identidad fisica real del Golden del RIG.

    Reutiliza el builder del RIG de S4-C: es el mismo tipo de objeto que
    produce el slice real, con el mismo serializador y el mismo digest.
    """
    from s4c_crash_worker import _plan_durable as _plan_s4c

    _crear_arbol(rig_root)
    return _plan_s4c(rig_root, operation_id)


def _crear_journal(rig_root: pathlib.Path) -> Any:
    """Journal durable REAL en APPLYING, con bytes canonicos y flush."""
    from s4c_crash_worker import _abrir_journal, _crear_journal_fixture

    durable = _plan_durable(rig_root, _operation_id(rig_root))
    _crear_journal_fixture(rig_root, durable)
    return _abrir_journal(rig_root, durable)


def _operation_id(rig_root: pathlib.Path) -> str:
    return (rig_root / "operation-id.txt").read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------
# Puertos
# --------------------------------------------------------------------------


class _PuertoDelegado:
    """Delegación explícita al puerto productivo.

    Se escribe método por método en vez de usar ``__getattr__`` a propósito:
    si el puerto de producción gana o pierde un método, este wrapper queda
    incompleto y el RIG falla al construirse, en vez de disfrazar el cambio
    de contrato con un ``__getattr__`` que lo absorbe en silencio.
    """

    def __init__(self, interno: Any, miga: _Miga | None = None) -> None:
        self._interno = interno
        self._miga = miga

    def open(self, path: pathlib.Path, node_kind: Any) -> int:
        return self._interno.open(path, node_kind)

    def close(self, handle: int) -> None:
        self._interno.close(handle)

    def read_identity(self, handle: int) -> Any:
        return self._interno.read_identity(handle)

    def read_live_pre_sd_sha256(self, handle: int) -> str:
        return self._interno.read_live_pre_sd_sha256(handle)

    def apply_target_dacl(self, handle: int, node: Any) -> None:
        if self._miga is not None:
            self._miga.marcar(f"pre-sdsi:{node.relative_path}")
        self._interno.apply_target_dacl(handle, node)
        if self._miga is not None:
            self._miga.marcar(f"sdsi:{node.relative_path}")

    def verify_target_dacl(self, handle: int, node: Any) -> Any:
        return self._interno.verify_target_dacl(handle, node)

    def restore_pre_sd(self, handle: int, node: Any) -> None:
        self._interno.restore_pre_sd(handle, node)
        if self._miga is not None:
            self._miga.marcar(f"restore:{node.relative_path}")

    def verify_restored_pre_sd(self, handle: int, node: Any) -> None:
        self._interno.verify_restored_pre_sd(handle, node)

    @property
    def setsecurityinfo_calls(self) -> int:
        return self._interno.setsecurityinfo_calls


class _PuertoConCrash(_PuertoDelegado):
    """S4-B real + muerte dura en el punto durable pedido.

    Sólo hay UN punto de muerte dentro del puerto, y es ``mid_apply``: dispara
    ANTES del primer ``SetSecurityInfo``, con el ``MUTATING`` ya durable. Es
    determinista por construcción.

    El borde "todos los nodos MUTATED" **no** se puede pedir desde acá, y esa
    es una lección medida: al volver de ``apply_target_dacl`` el nodo todavía
    no tiene su registro ``MUTATED`` en el WAL — ése lo escribe el engine
    después, con su flush—, así que matar ahí deja el apply incompleto y el
    router hace rollback. El mismo crash, en una máquina, daba ``committed`` y
    en otra daba ``rolled_back``: dependía de si el flush del nodo alcanzado
    había ocurrido. Es exactamente el punto que el borde real, ya FUERA del
    puerto, sí fija: matar después de que ``apply_authorized_plan`` volvió.
    """

    def __init__(self, interno: Any, modo: str | None, miga: _Miga) -> None:
        super().__init__(interno, miga)
        self._modo = modo

    def apply_target_dacl(self, handle: int, node: Any) -> None:
        self._miga.marcar(f"pre-sdsi:{node.relative_path}")
        if self._modo == "mid_apply":
            # El engine ya registró MUTATING durable al entrar acá: el punto
            # "pre-sdsi" es exactamente el borde de la ventana.
            _crash(CRASH_EXIT["mid_apply"])
        self._interno.apply_target_dacl(handle, node)
        self._miga.marcar(f"sdsi:{node.relative_path}")


def _puerto_mutacion(miga: _Miga, crash_en: str | None) -> Any:
    from sky_claw.local.runtime_vault.mutation_executor import HandleBoundTargetDaclPort

    return _PuertoConCrash(HandleBoundTargetDaclPort(), crash_en, miga)


def _puerto_recovery(miga: _Miga) -> Any:
    from sky_claw.local.runtime_vault.mutation_executor import HandleBoundTargetDaclPort

    return _PuertoDelegado(HandleBoundTargetDaclPort(), miga)


class _EscritorRIG:
    """`GoldenBackupDurableWriter` del RIG: create-once + flush + relectura.

    DEGRADOD y declarado como tal. La politica (create-once, flush, relectura,
    revalidacion) vive en el store y S4-D la aplica igual; lo UNICO que cambia
    es la primitiva de Win32, que aqui publica en el namespace del RIG en vez
    de hacerlo con el SD canonico del namespace productivo.

    Se sustituye porque el SD canonico exige un SID propietario que no resuelve
    sin el bootstrap privilegiado del namespace — el mismo limite que
    ArrangeAttribute tiene para el RIG de S4-D. La durability (que es lo que
    S4-E verifica en el orden backup-antes-de-COMMITTED) SI se ejercita real.
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        if dest.exists():
            raise FileExistsError(f"el backup ya existe (create-once): {dest}")
        tmp = dest.with_name(f".tmp_{uuid.uuid4().hex}.{dest.name}")
        with tmp.open("xb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        # Create-once real: os.link falla si el destino existe.
        os.link(tmp, dest)
        tmp.unlink()
        releido = dest.read_bytes()
        if releido != payload:
            raise OSError(f"relectura del backup no coincide: {dest}")


def _puerto_verificacion(miga: _Miga) -> Any:
    """Port de S4-D. DEGRADED: aprueba los gates, no los ejecuta de verdad.

     Los verdicts respetan el contrato de ``GateVerdict`` (nombre de gate que
     S4-D compara, ``passed`` bool estricto, digest de 64 hex): que el puerto
    activo sea UNICAMENTE eso es parte de lo que el RIG verifica.
    """
    from sky_claw.local.runtime_vault.finalization_orchestrator import GateVerdict

    def _veredicto(gate: str) -> Any:
        miga.marcar(f"gate:{gate}")
        return GateVerdict(gate=gate, passed=True, detail=f"rig S4-E {gate}", evidence_digest="c" * 64)

    class _Puerto:
        def observar_gp1(self, *, raiz: str, esperado: str) -> Any:
            miga.marcar(f"gp1:{esperado}")
            return _veredicto("gp1")

        def observar_rv2(self, *, raiz: str, tree_digest_esperado: Any) -> Any:
            return _veredicto("rv2")

        def observar_node_set(self, *, raiz: str, nodos_autorizados: tuple[Any, ...]) -> Any:
            return _veredicto("node_set")

        def observar_quiescence(self, *, raiz: str, nodos_autorizados: tuple[Any, ...]) -> Any:
            return _veredicto("quiescence")

    return _Puerto()


# --------------------------------------------------------------------------
# Sesion de frontera con lock REAL (sin elevacion: DEGRADED acotado)
# --------------------------------------------------------------------------


def _sesion_con_lock_real(rig_root: pathlib.Path, durable: Any) -> Any:
    from sky_claw.local.runtime_vault.authorization_context import (
        CoordinatorProcessIdentity,
        PrivilegedBoundarySession,
        acquire_operator_primary_token_from_coordinator,
    )

    # El lock abre con OPEN_ALWAYS sobre locks/: el directorio tiene que
    # existir. En el namespace productivo lo crea el bootstrap del namespace
    # privilegiado; en el RIG (no elevado) lo crea el propio worker.
    from sky_claw.local.runtime_vault.golden_mutation_lock import acquire_golden_mutation_lock, derive_golden_lock_path

    lock_path = pathlib.Path(
        str(
            derive_golden_lock_path(
                durable.volume_serial_number,
                durable.root_file_id,
                programdata_resolver=_resolver(rig_root),
            )
        )
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = acquire_golden_mutation_lock(
        durable.volume_serial_number,
        durable.root_file_id,
        durable.operation_id,
        programdata_resolver=_resolver(rig_root),
    )
    # Token: el MISMO camino que S4-C usa (adapter falso, sin tocar el SO).
    # La sesion queda REAL, con su lock REAL, para que close() sea el
    # release real y no un no-op del RIG.
    from s4c_crash_worker import _TokenAdapterFalso

    identidad = CoordinatorProcessIdentity(
        pid=4242,
        creation_time=133_456_789_012_345_678,
        image_path="C:\\Program Files\\Sky-Claw\\sky-claw.exe",
    )
    token = acquire_operator_primary_token_from_coordinator(identidad, adapter=_TokenAdapterFalso())
    return PrivilegedBoundarySession(operator_token=token, lock=lock)


def _volumen(rig_root: pathlib.Path) -> tuple[int, int]:
    """Volume serial + file id REALES del Golden del RIG, por handle."""
    import ctypes  # noqa: PLC0415

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p

    class _INFO(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", ctypes.c_uint32),
            ("ftCreationTime", ctypes.c_uint32 * 2),
            ("ftLastAccessTime", ctypes.c_uint32 * 2),
            ("ftLastWriteTime", ctypes.c_uint32 * 2),
            ("dwVolumeSerialNumber", ctypes.c_uint32),
            ("nFileSizeHigh", ctypes.c_uint32),
            ("nFileSizeLow", ctypes.c_uint32),
            ("nNumberOfLinks", ctypes.c_uint32),
            ("nFileIndexHigh", ctypes.c_uint32),
            ("nFileIndexLow", ctypes.c_uint32),
        ]

    h = k32.CreateFileW(
        ctypes.c_wchar_p(str(rig_root / "golden")),
        0x80000000,
        0x00000007,
        None,
        3,
        0x02000000,
        None,
    )
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _INFO()
    k32.GetFileInformationByHandle(ctypes.c_void_p(h), ctypes.byref(info))
    k32.CloseHandle(ctypes.c_void_p(h))
    return int(info.dwVolumeSerialNumber), (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)


def _volumen_y_file_id(durable: Any) -> tuple[int, int]:
    return int(durable.volume_serial_number), int(durable.root_file_id)


# --------------------------------------------------------------------------
# Fases
# --------------------------------------------------------------------------


def _fase_preparar(rig_root: pathlib.Path) -> None:
    """Plan durable + journal durable en APPLYING, sin tomar lock."""
    from s4c_crash_worker import _crear_journal_fixture

    durable = _plan_durable(rig_root, _operation_id(rig_root))
    _crear_journal_fixture(rig_root, durable)
    (rig_root / "s4e-plan.json").write_text(
        json.dumps(
            {
                "operation_id": durable.operation_id,
                "digest": durable.digest,
                "volume_serial_number": int(durable.volume_serial_number),
                "root_file_id": int(durable.root_file_id),
                "node_count": len(durable.plan.nodes),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _escribir_resultado(rig_root: pathlib.Path, nombre: str, datos: dict[str, Any]) -> None:
    (rig_root / nombre).write_text(json.dumps(datos, indent=2), encoding="utf-8")


def _fase_apply_then_finalize(rig_root: pathlib.Path, crash_en: str | None) -> None:
    """Apply real → COMMITTED durable → release. Con crash inyectable.

    Es el camino feliz de la zona mutadora, con las MISMAS primitivas que
    S4-E compone: la sesión con lock real se pasa a S4-B y a S4-D sin
    soltar el handle en el medio.
    """
    from sky_claw.local.runtime_vault.finalization_orchestrator import (
        finalize_protection_transaction,
    )
    from sky_claw.local.runtime_vault.mutation_executor import apply_authorized_plan

    miga = _Miga(rig_root)
    operation_id = _operation_id(rig_root)
    durable = _plan_durable(rig_root, operation_id)
    from s4c_crash_worker import _crear_journal_fixture

    _crear_journal_fixture(rig_root, durable)
    from sky_claw.local.runtime_vault.protection_journal_store import open_protection_journal

    journal = open_protection_journal(operation_id, durable, programdata_resolver=_resolver(rig_root))
    sesion = _sesion_con_lock_real(rig_root, durable)
    miga.marcar("lock-adquirido")
    # El namespace productivo aprovisiona golden_backups/<scope>/<policy>
    # antes de archivar; en el RIG (no elevado) lo crea el worker. Sin esta
    # fila, S4-D falla cerrado a ROLLBACK_REQUIRED — que también es un caso
    # legítimo, pero aquí lo que queremos probar es el camino COMMIT.
    from sky_claw.local.runtime_vault.golden_backup_archive import derive_golden_backup_dir

    pathlib.Path(
        str(
            derive_golden_backup_dir(
                durable.volume_serial_number,
                durable.root_file_id,
                "golden-policy-v1",
                programdata_resolver=_resolver(rig_root),
            )
        )
    ).mkdir(parents=True, exist_ok=True)

    reporte = apply_authorized_plan(
        plan=durable,
        journal=journal,
        session=sesion,
        port=_puerto_mutacion(miga, crash_en),
    )
    miga.marcar(f"apply-fin:{reporte.ok}:{reporte.transaction_state.value}")

    # E03: TODOS los nodos quedaron MUTATED con su WAL durable y la muerte cae
    # ANTES de S4-D. Que el crash sea acá y no dentro del puerto es lo que hace
    # este borde determinista: volver de `apply_authorized_plan` es la única
    # prueba de que cada nodo cerró su WAL.
    if crash_en == "all_mutated":
        miga.marcar(f"all-mutated:{reporte.setsecurityinfo_calls}")
        _crash(CRASH_EXIT["all_mutated"])

    final = finalize_protection_transaction(
        operation_id=operation_id,
        plan=durable,
        journal=journal,
        port=_puerto_verificacion(miga),
        programdata_resolver=_resolver(rig_root),
        archive_writer=_EscritorRIG(),
        session=sesion,
    )
    miga.marcar(f"finalize-fin:{final.disposition.value}:{final.journal_state.value}")

    if crash_en == "post_committed_pre_release":
        # El COMMITTED ya es durable; el lock todavía no se liberó.
        _crash(CRASH_EXIT["post_committed_pre_release"])

    sesion.close()
    miga.marcar("frontera-cerrada")
    _escribir_resultado(
        rig_root,
        "s4e-result.json",
        {
            "apply_ok": reporte.ok,
            "apply_state": reporte.transaction_state.value,
            "apply_sdsi_calls": reporte.setsecurityinfo_calls,
            "disposition": final.disposition.value,
            "journal_state": final.journal_state.value,
            "archive_digest": final.archive_digest,
            "lock_released": final.lock.released,
            "committed": final.committed,
        },
    )


def _fase_hold_lock(rig_root: pathlib.Path) -> None:
    """Toma el lock REAL y lo retiene, sin avanzar la transacción.

    Sirve para el caso de competencia (E15) y para el lock huérfano: el
    controller lo mata con `taskkill /T /F`, o sea una muerte externa REAL
    con el handle abierto y la metadata sin RELEASED.
    """
    import time

    from s4c_crash_worker import _crear_journal_fixture

    miga = _Miga(rig_root)
    operation_id = _operation_id(rig_root)
    durable = _plan_durable(rig_root, operation_id)
    _crear_journal_fixture(rig_root, durable)
    sesion = _sesion_con_lock_real(rig_root, durable)
    miga.marcar("hold:iniciado")
    limite = time.monotonic() + 120
    while time.monotonic() < limite:
        time.sleep(0.05)
    # Si nadie lo mató, cerramos limpio para no dejar el fixture colgado.
    sesion.close()


class _EscritorBindingRIG:
    """``OperationLockBindingWriter`` del RIG: create-once + flush + relectura.

    DEGRADADO y declarado como tal. El writer de namespace usa el SD canonico
    del vault protegido, que exige un SID propietario que no resuelve sin el
    bootstrap privilegiado — el mismo limite que arrastran los RIG de S4-C y
    S4-D. Lo que NO se cambia es la politica: ``promote_operation_lock_binding``
    sigue siendo la primitive que construye, serializa, valida digest e
    identidad y acuña la autoridad; lo unico inyectado es la escritura.

    Es el mismo puerto que el paquete ya expone para tests
    (``operation_lock_binding.binding_writer``), asi que E16 ejercita el
    protocolo real de promocion y no un atajo.
    """

    def write_create_once(self, dest: pathlib.Path, payload: bytes, object_name: str) -> None:
        if dest.exists():
            raise FileExistsError(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".tmp_{uuid.uuid4().hex}.{dest.name}")
        with open(tmp, "xb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, dest)
        tmp.unlink(missing_ok=True)


def _fase_pre_plan_lock(rig_root: pathlib.Path) -> None:
    """E16 — publica el binding durable, toma el lock REAL y MUERE.

    Reproduce la ventana legitima del protocolo:

        operation_lock_binding durable
          -> GoldenMutationLock adquirido
          -> CRASH
          -> NO hay authorized_plan
          -> NO hay protection_journal

    Todo con primitives PRODUCTIVAS: ``promote_operation_lock_binding`` y
    ``acquire_golden_mutation_lock``. No se escribe ningun JSON a mano, porque
    un fixture fabricado probaria el test y no el protocolo.

    La muerte es ``os._exit``: muerte de PROCESO, sin ``finally``, sin
    ``atexit``, sin flush pendiente. No se presenta como muerte externa; el
    escenario de taskkill externo esta cubierto por E15, que si usa un
    proceso que espera de verdad.
    """
    from sky_claw.local.runtime_vault.golden_mutation_lock import (
        acquire_golden_mutation_lock,
        derive_golden_lock_path,
    )
    from sky_claw.local.runtime_vault.operation_lock_binding import (
        promote_operation_lock_binding,
    )

    miga = _Miga(rig_root)
    operation_id = _operation_id(rig_root)
    serial, file_id = _volumen(rig_root)

    # 1. Binding durable por la primitive real de S4-A.
    promote_operation_lock_binding(
        operation_id=operation_id,
        volume_serial_number=serial,
        root_file_id=file_id,
        programdata_resolver=_resolver(rig_root),
        binding_writer=_EscritorBindingRIG(),
    )
    miga.marcar(f"binding-durable:{operation_id}")

    # 2. Lock REAL tomado con la primitive de produccion.
    pathlib.Path(str(derive_golden_lock_path(serial, file_id, programdata_resolver=_resolver(rig_root)))).parent.mkdir(
        parents=True, exist_ok=True
    )
    lock = acquire_golden_mutation_lock(serial, file_id, operation_id, programdata_resolver=_resolver(rig_root))
    miga.marcar(f"lock-tomado:{lock.identity.operation_id}")

    # 3. Ni plan ni journal: se verifican ausentes ANTES de morir, para que el
    #    test no dependa de que "no se crean" por accidente.
    from sky_claw.local.runtime_vault.authorized_plan_store import (
        classify_durable_authorized_plan,
    )
    from sky_claw.local.runtime_vault.protection_journal_store import (
        classify_protection_journal,
    )

    plan = classify_durable_authorized_plan(operation_id, programdata_resolver=_resolver(rig_root))
    journal = classify_protection_journal(operation_id, programdata_resolver=_resolver(rig_root))
    miga.marcar(f"plan:{plan.value}:journal:{journal.classification.value}")
    _escribir_resultado(
        rig_root,
        "s4e-preplan.json",
        {
            "plan_classification": plan.value,
            "journal_classification": journal.classification.value,
            "owner_pid": lock.identity.owner_pid,
        },
    )

    # 4. Muerte dura con el lock TOMADO y la metadata sin RELEASED.
    _crash(CRASH_EXIT["pre_plan"])


def _fase_resume(rig_root: pathlib.Path) -> None:
    """Proceso B: el router de S4-E reanuda desde evidencia durable."""
    from sky_claw.local.runtime_vault import protection_service as svc

    miga = _Miga(rig_root)
    operation_id = _operation_id(rig_root)
    miga.marcar("resume:begin")

    resultado = svc.resume_golden_protection(
        operation_id=operation_id,
        frontend=svc._Frontend(
            recovery_port=_puerto_recovery(miga),
            verification_port=_puerto_verificacion(miga),
            archive_writer=_EscritorRIG(),
            programdata_resolver=_resolver(rig_root),
        ),
    )
    miga.marcar(f"resume:fin:{resultado.disposition.value}")
    _escribir_resultado(
        rig_root,
        "s4e-result-b.json",
        {
            "operation_id": resultado.operation_id,
            "disposition": resultado.disposition.value,
            "stage": resultado.stage.value,
            "route": resultado.route.value if resultado.route else None,
            "journal_state": resultado.journal_state.value if resultado.journal_state else None,
            "archive_digest": resultado.archive_digest,
            "lock_outcome": resultado.lock_outcome,
            "lock_retained": resultado.lock_retained,
            "rollback_executed": resultado.rollback_executed,
            "committed": resultado.committed,
            "settled": resultado.settled,
            "operator_intervention_required": resultado.operator_intervention_required,
            "fail_closed_reason": resultado.fail_closed_reason,
            "source_orchestrator": resultado.source_orchestrator,
        },
    )


def _fase_discovery(rig_root: pathlib.Path) -> None:
    """El arranque clasifica read-only y NO muta.

    GP2-S4E / P4: con ``PACKAGED_HELPER_PROVISIONING_STATUS = UNRESOLVED`` no
    hay forma honesta de levantar la frontera privilegiada desde el proceso
    normal, asi que el arranque clasifica y reporta. Ejecutar S4-C/S4-D en un
    ``asyncio.to_thread`` seria la misma cosa con otro nombre: un thread no
    cambia el token de seguridad.
    """
    from sky_claw.local.runtime_vault.protection_service import (
        RuntimeVaultProtectionCoordinator,
        discover_pending_operations,
    )

    resolver = _resolver(rig_root)
    pendientes = discover_pending_operations(programdata_resolver=resolver)
    _escribir_resultado(
        rig_root,
        "s4e-discovery.json",
        [
            {
                "operation_id": d.operation_id,
                "binding": d.binding_evidence.value,
                "journal_state": d.journal_state.value if d.journal_state else None,
                "route": d.route.value,
            }
            for d in pendientes
        ],
    )

    coordinador = RuntimeVaultProtectionCoordinator(reconciliar_al_arrancar=True)
    diagnostico = coordinador.diagnosticar_arranque(programdata_resolver=resolver)
    _escribir_resultado(
        rig_root,
        "s4e-boot.json",
        [
            {
                "operation_id": d.operation_id,
                "route": d.route.value,
                "journal_state": d.journal_state.value if d.journal_state else None,
                "binding": d.binding_evidence.value,
                "requiere_privilegios": d.requiere_privilegios,
                "bloquea_operador": d.bloquea_operador,
                "motivo": d.motivo,
            }
            for d in diagnostico
        ],
    )


def _fase_normalizar_lock(rig_root: pathlib.Path) -> None:
    """Deja un lock HUERFANO de esta operacion en disco, sin COMMITTED.

    Reproduce el residuo de un crash entre la adquisicion y el primer asiento
    durable. El controller usa esto para E11 (lock ajeno) y para el camino de
    takeover de S4-C.
    """
    from s4c_crash_worker import _crear_journal_fixture
    from sky_claw.local.runtime_vault.golden_mutation_lock import (
        GoldenLockMetadata,
        GoldenLockPhase,
        derive_golden_lock_key,
        derive_golden_lock_path,
    )

    operation_id = _operation_id(rig_root)
    durable = _plan_durable(rig_root, operation_id)
    _crear_journal_fixture(rig_root, durable)
    sesion = _sesion_con_lock_real(rig_root, durable)
    lock_path = pathlib.Path(
        str(
            derive_golden_lock_path(
                durable.volume_serial_number,
                durable.root_file_id,
                programdata_resolver=_resolver(rig_root),
            )
        )
    )
    # Retener sin liberar: el handle se cierra pero la metadata NO queda
    # RELEASED, que es exactamente el residuo que el recovery debe tomar.
    sesion.lock.retain_for_inspection()
    sesion.closed = True
    _escribir_resultado(
        rig_root,
        "s4e-orphan.json",
        {
            "lock_path": str(lock_path),
            "lock_key": derive_golden_lock_key(durable.volume_serial_number, durable.root_file_id),
            "operation_id": operation_id,
            "phase": GoldenLockPhase.AUTHORIZATION_BOUNDARY.value,
            "metadata_class": GoldenLockMetadata.__name__,
        },
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Worker del RIG S4-E (SÓLO tests)")
    parser.add_argument("--rig-root", required=True)
    parser.add_argument("--fase", required=True)
    parser.add_argument("--crash-en", default=None)
    args = parser.parse_args()

    rig_root = pathlib.Path(args.rig_root)
    _abortar_si_raiz_peligrosa(rig_root)
    rig_root.mkdir(parents=True, exist_ok=True)

    fases = {
        "preparar": lambda: _fase_preparar(rig_root),
        "apply_then_finalize": lambda: _fase_apply_then_finalize(rig_root, args.crash_en),
        "pre_plan_lock": lambda: _fase_pre_plan_lock(rig_root),
        "resume": lambda: _fase_resume(rig_root),
        "hold_lock": lambda: _fase_hold_lock(rig_root),
        "discovery": lambda: _fase_discovery(rig_root),
        "orphan_lock": lambda: _fase_normalizar_lock(rig_root),
    }
    if args.fase not in fases:
        raise SystemExit(f"fase desconocida: {args.fase}")
    fases[args.fase]()
    sys.exit(CODIGO_OK)


if __name__ == "__main__":
    _main()

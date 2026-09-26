"""Store durable del journal transaccional y del WAL por nodo (GP2-S4A, ADR 0010 §19/§20).

Este módulo aporta la durabilidad real al modelo puro de
:mod:`protection_journal`: una capa ctypes propia (``CreateFileW`` con
``CREATE_NEW``/``OPEN_EXISTING`` + ``FILE_FLAG_WRITE_THROUGH``,
``SetFilePointerEx(FILE_END)``, ``WriteFile``, ``FlushFileBuffers`` con BOOL
verificado, ``ReadFile``) abstraída detrás del protocolo
:class:`JournalDurabilityKernel`. El protocolo existe para que los tests causales
corran con kernels falsos deterministas y para que POSIX obtenga un skip tipado en
vez de una semántica inventada.

La propiedad que se defiende aquí es la del WAL (§33/§34): el permiso de mutar un
nodo sólo puede existir DESPUÉS de que ``MUTATING(K)`` esté durable en disco. Si
``FlushFileBuffers`` devuelve ``FALSE`` no hay permiso, no hay callback de
mutación y el estado del nodo NO pasa a ``MUTATING``: se lanza
:class:`DurableJournalFlushError` y el journal queda ``INDETERMINATE`` (§24).

Orden de creación (§53): el journal sólo se crea sobre un plan autoritativo ya
durable y revalidado (``DurableAuthorizedPlan``), nunca antes.
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime
import pathlib
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from sky_claw.local.runtime_vault.authorized_plan import AuthorizedPlan
from sky_claw.local.runtime_vault.authorized_plan_store import (
    DurableAuthorizedPlan,
    derive_authorized_plan_dir,
    load_durable_authorized_plan,
)
from sky_claw.local.runtime_vault.protection_journal import (
    INITIAL_TRANSACTION_STATE,
    PROTECTION_JOURNAL_FILE_NAME,
    PROTECTION_JOURNAL_OBJECT_NAME,
    PROTECTION_JOURNAL_SCHEMA_VERSION,
    S4A_PERMITTED_TRANSITION_TARGETS,
    JournalLoadDisposition,
    JournalNodeRecord,
    JournalRecord,
    JournalTransactionRecord,
    NodeWalState,
    PrematureCommitError,
    ProtectionJournal,
    ProtectionJournalError,
    ProtectionJournalIndeterminateError,
    ProtectionJournalSchemaError,
    ProtectionTransactionState,
    assert_node_transition,
    assert_transaction_transition,
    build_journal_header_record,
    parse_journal_bytes,
    replay_journal_records,
    serialize_journal_record,
)

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class ProtectionJournalStoreError(ProtectionJournalError):
    """Base de excepciones del store durable del journal."""


class ProtectionJournalUnsupportedError(ProtectionJournalStoreError):
    """Plataforma sin las garantías Win32 que este store necesita."""


class ProtectionJournalCreateError(ProtectionJournalStoreError):
    """Fallo de I/O al crear el journal protegido."""


class ProtectionJournalAlreadyExistsError(ProtectionJournalStoreError):
    """El journal de la operación ya existe: nunca se sobreescribe autoridad."""


class ProtectionJournalNotFoundError(ProtectionJournalStoreError):
    """No hay journal durable para la operación (evidencia ausente, no ambigua)."""


class DurableJournalFlushError(ProtectionJournalStoreError):
    """``FlushFileBuffers`` no confirmado: sin permiso de mutación y sin WAL (§24).

    Equivale a ``PERMIT_ISSUED`` sin durabilidad: la intención NO está durable, el
    nodo NO queda ``MUTATING`` y ningún callback de mutación puede ejecutarse.
    """


class ProtectionJournalPlanBindingError(ProtectionJournalStoreError):
    """El journal no liga contra el plan autoritativo o la identidad del nodo."""


class ProtectionJournalAppendError(ProtectionJournalStoreError):
    """Fallo de I/O al anexar un registro al journal."""


# ============================================================================
# Identidad de nodo exigida por el WAL (§38)
# ============================================================================


@dataclass(frozen=True, slots=True)
class NodeMutationBinding:
    """Identidad física completa de un nodo. Nunca un índice desnudo.

    Toda transición del WAL exige estas cuatro piezas juntas: ``relative_path``
    más la identidad física del volumen (``VolumeSerialNumber``, ``FileId``) más
    el hash del PRE SD autorizado. Un índice posicional no identifica nada tras un
    reordenamiento del plan.
    """

    relative_path: str
    volume_serial_number: int
    file_id: int
    pre_sd_sha256: str

    @classmethod
    def from_node(cls, node: Any) -> NodeMutationBinding:
        """Construye el binding desde un nodo del plan autorizado."""
        return cls(
            relative_path=str(node.relative_path),
            volume_serial_number=int(node.volume_serial_number),
            file_id=int(node.file_id),
            pre_sd_sha256=str(node.pre_sd_sha256),
        )


# ============================================================================
# Permiso de mutación (consume-once)
# ============================================================================


_MINT_PROOF = object()


@dataclass(frozen=True, slots=True)
class WalMutationPermit:
    """Permiso de mutación de UN nodo, emitido sólo tras WAL durable + flush.

    Consume-once: :meth:`mark_consumed` lo inutiliza, de modo que un mismo objeto
    no puede autorizar dos mutaciones ni sobrevivir a un error intermedio. La
    prueba de acuñación es privada: nadie fuera de este módulo puede fabricar un
    permiso sin append + ``FlushFileBuffers == TRUE``.
    """

    record: JournalNodeRecord
    operation_id: str
    authorized_plan_digest: str
    journal_path: pathlib.Path
    _proof: Any = field(default=None, repr=False, compare=False)
    _consumed: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _MINT_PROOF:
            raise ProtectionJournalStoreError(
                "WalMutationPermit sólo puede acuñarse tras MUTATING durable + FlushFileBuffers == TRUE"
            )

    @property
    def relative_path(self) -> str:
        return self.record.relative_path

    @property
    def node_binding(self) -> NodeMutationBinding:
        return NodeMutationBinding(
            relative_path=self.record.relative_path,
            volume_serial_number=self.record.volume_serial_number,
            file_id=self.record.file_id,
            pre_sd_sha256=self.record.pre_sd_sha256,
        )

    @property
    def is_consumed(self) -> bool:
        return self._consumed

    def mark_consumed(self) -> None:
        object.__setattr__(self, "_consumed", True)


# ============================================================================
# Clasificación observable
# ============================================================================


class ProtectionJournalClassification(StrEnum):
    """Clasificación por evidencia observable del journal (§52)."""

    ABSENT = "absent"
    VALID = "valid"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ProtectionJournalClassificationResult:
    """Resultado de clasificar. Nunca afirma éxito desde evidencia rota."""

    classification: ProtectionJournalClassification
    journal: ProtectionJournal | None = None
    detail: str = ""

    @property
    def is_valid(self) -> bool:
        return self.classification is ProtectionJournalClassification.VALID and self.journal is not None


# ============================================================================
# Protocolo de durabilidad (kernel inyectable)
# ============================================================================


class JournalDurabilityKernel(Protocol):
    """Contrato mínimo de durabilidad que el store necesita del sistema.

    Los tests usan implementaciones falsas deterministas (incluida la simulación
    de ``FlushFileBuffers == FALSE``); la implementación real vive en
    :class:`_Win32JournalKernel`. Ningún método devuelve éxito sin que el flush
    haya sido confirmado por el kernel.
    """

    def exists(self, path: pathlib.PurePath) -> bool: ...

    def create(self, path: pathlib.PurePath) -> int:
        """Crea ``CREATE_NEW`` con SD canónico + ``FILE_FLAG_WRITE_THROUGH``."""
        ...

    def open_append(self, path: pathlib.PurePath) -> int:
        """Abre ``OPEN_EXISTING`` para anexar al final, write-through."""
        ...

    def append(self, handle: int, payload: bytes) -> None:
        """``SetFilePointerEx(FILE_END)`` + ``WriteFile`` del payload completo."""
        ...

    def flush(self, handle: int) -> bool:
        """``FlushFileBuffers`` con BOOL verificado."""
        ...

    def read_all(self, path: pathlib.PurePath) -> bytes: ...

    def close(self, handle: int) -> None: ...

    def remove(self, path: pathlib.PurePath) -> None: ...


# ============================================================================
# Kernel Win32 real
# ============================================================================

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_FLAG_WRITE_THROUGH = 0x80000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_END = 2
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_INVALID_HANDLE_VALUE = -1

_kernel32: Any = None


class _SecurityAttributes(ctypes.Structure):
    """SECURITY_ATTRIBUTES (mismo ABI que el declarado en trusted_namespace)."""

    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


def _load_kernel32_apis() -> Any:
    """Declara el ABI Win32 usado por el journal (HANDLE/BOOL/GetLastError)."""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # CreateFileW: (LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE) -> HANDLE
    # Se declara aquí con el MISMO ABI que trusted_namespace (mismo objeto
    # ctypes.WinDLL), de modo que re-declarar es idempotente y no divergente.
    _kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    _kernel32.CreateFileW.restype = ctypes.c_void_p

    # WriteFile: (HANDLE, LPCVOID, DWORD, LPDWORD, LPOVERLAPPED) -> BOOL
    _kernel32.WriteFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = ctypes.c_int

    # FlushFileBuffers: (HANDLE) -> BOOL
    _kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    _kernel32.FlushFileBuffers.restype = ctypes.c_int

    # ReadFile: (HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED) -> BOOL
    _kernel32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = ctypes.c_int

    # SetFilePointerEx: (HANDLE, LARGE_INTEGER, PLARGE_INTEGER, DWORD) -> BOOL
    _kernel32.SetFilePointerEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        ctypes.c_uint32,
    ]
    _kernel32.SetFilePointerEx.restype = ctypes.c_int

    # GetFileSizeEx: (HANDLE, PLARGE_INTEGER) -> BOOL
    _kernel32.GetFileSizeEx.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong)]
    _kernel32.GetFileSizeEx.restype = ctypes.c_int

    # CloseHandle: (HANDLE) -> BOOL
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_int
    return _kernel32


def _invalid_handle(handle: int) -> bool:
    return handle in (0, _INVALID_HANDLE_VALUE)


def _safe_close_handle(kernel: Any, handle: int | None) -> None:
    if handle is None or _invalid_handle(handle):
        return
    with contextlib.suppress(OSError):
        kernel.CloseHandle(ctypes.c_void_p(handle))


class _Win32JournalKernel:
    """Kernel real del journal. Sin fallbacks: toda falla es fail-closed."""

    def exists(self, path: pathlib.PurePath) -> bool:
        return pathlib.Path(path).exists()

    def create(self, path: pathlib.PurePath) -> int:
        kernel = _load_kernel32_apis()
        from sky_claw.local.runtime_vault.trusted_namespace import (
            _build_canonical_security_descriptor,
        )

        with _build_canonical_security_descriptor(PROTECTION_JOURNAL_OBJECT_NAME) as sd_ctx:
            sa = _SecurityAttributes()
            sa.nLength = ctypes.sizeof(sa)
            sa.lpSecurityDescriptor = sd_ctx.p_sd
            sa.bInheritHandle = False
            handle = kernel.CreateFileW(
                str(path),
                _GENERIC_WRITE,
                _FILE_SHARE_READ,
                ctypes.byref(sa),
                _CREATE_NEW,
                _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_WRITE_THROUGH | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        raw = int(handle or 0)
        if _invalid_handle(raw):
            err = ctypes.get_last_error()
            if err in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                raise ProtectionJournalAlreadyExistsError(
                    f"El journal de la operación ya existe en '{path}' (código Win32 {err}): nunca se sobreescribe"
                )
            raise ProtectionJournalCreateError(f"CreateFileW falló al crear el journal '{path}': código {err}")
        return raw

    def open_append(self, path: pathlib.PurePath) -> int:
        kernel = _load_kernel32_apis()
        handle = kernel.CreateFileW(
            str(path),
            _GENERIC_WRITE,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_WRITE_THROUGH | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        raw = int(handle or 0)
        if _invalid_handle(raw):
            err = ctypes.get_last_error()
            raise ProtectionJournalCreateError(
                f"CreateFileW falló al abrir el journal para anexar '{path}': código {err}"
            )
        return raw

    def append(self, handle: int, payload: bytes) -> None:
        kernel = _load_kernel32_apis()
        distance = ctypes.c_longlong(0)
        if not kernel.SetFilePointerEx(
            ctypes.c_void_p(handle), ctypes.c_longlong(0), ctypes.byref(distance), _FILE_END
        ):
            err = ctypes.get_last_error()
            raise ProtectionJournalAppendError(f"SetFilePointerEx(FILE_END) falló sobre el journal: código {err}")
        buffer = (ctypes.c_char * len(payload)).from_buffer_copy(payload)
        written = ctypes.c_uint32(0)
        if not kernel.WriteFile(
            ctypes.c_void_p(handle),
            buffer,
            len(payload),
            ctypes.byref(written),
            None,
        ) or written.value != len(payload):
            err = ctypes.get_last_error()
            raise ProtectionJournalAppendError(
                f"WriteFile falló al anexar al journal: código {err}, escritos={written.value} de {len(payload)}"
            )

    def flush(self, handle: int) -> bool:
        kernel = _load_kernel32_apis()
        return bool(kernel.FlushFileBuffers(ctypes.c_void_p(handle)))

    def read_all(self, path: pathlib.PurePath) -> bytes:
        kernel = _load_kernel32_apis()
        handle = kernel.CreateFileW(
            str(path),
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        raw = int(handle or 0)
        if _invalid_handle(raw):
            err = ctypes.get_last_error()
            raise ProtectionJournalCreateError(
                f"CreateFileW falló al reabrir el journal '{path}' para lectura: código {err}"
            )
        try:
            size = ctypes.c_longlong(0)
            if not kernel.GetFileSizeEx(ctypes.c_void_p(raw), ctypes.byref(size)):
                err = ctypes.get_last_error()
                raise ProtectionJournalCreateError(f"GetFileSizeEx falló sobre el journal '{path}': código {err}")
            total = int(size.value)
            if total == 0:
                return b""
            buffer = ctypes.create_string_buffer(total)
            written = ctypes.c_uint32(0)
            if (
                not kernel.ReadFile(
                    ctypes.c_void_p(raw),
                    buffer,
                    total,
                    ctypes.byref(written),
                    None,
                )
                or written.value != total
            ):
                err = ctypes.get_last_error()
                raise ProtectionJournalCreateError(f"ReadFile falló sobre el journal '{path}': código {err}")
            return buffer.raw[: written.value]
        finally:
            _safe_close_handle(kernel, raw)

    def close(self, handle: int) -> None:
        _safe_close_handle(_load_kernel32_apis(), handle)

    def remove(self, path: pathlib.PurePath) -> None:
        pathlib.Path(path).unlink(missing_ok=True)


# ============================================================================
# Derivación de rutas (nunca desde el caller)
# ============================================================================


def _resolve_kernel(kernel: JournalDurabilityKernel | None) -> JournalDurabilityKernel:
    """Resuelve el kernel de durabilidad y con él la exigencia de plataforma.

    Sin kernel inyectado, la durabilidad la aporta este módulo y se exigen las
    garantías Win32 reales (``FILE_FLAG_WRITE_THROUGH`` + ``FlushFileBuffers``
    verificado): en otra plataforma se falla con error tipado en lugar de
    inventar semántica durable. Con kernel inyectado, la durabilidad es
    responsabilidad del caller (tests deterministas, o un backend futuro que
    cumpla el mismo contrato append + flush): el gate de plataforma no aplica
    porque no hay ABI Win32 en juego.
    """
    if kernel is not None:
        return kernel
    if sys.platform != "win32":
        raise ProtectionJournalUnsupportedError(
            "La durabilidad real del journal sólo existe con las garantías Win32 del namespace"
        )
    return _Win32JournalKernel()


def derive_protection_journal_path(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.Path:
    """``...\\\\operations\\\\<operation_id>\\\\protection_journal.json`` (§19)."""
    return derive_authorized_plan_dir(operation_id, programdata_resolver=programdata_resolver) / (
        PROTECTION_JOURNAL_FILE_NAME
    )


# ============================================================================
# Lectura y clasificación (proceso B tras reinicio)
# ============================================================================


def classify_protection_journal(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
    kernel: JournalDurabilityKernel | None = None,
) -> ProtectionJournalClassificationResult:
    """Clasifica el journal por evidencia observable. Nunca lanza por datos de disco."""
    active_kernel = _resolve_kernel(kernel)
    path = derive_protection_journal_path(operation_id, programdata_resolver=programdata_resolver)
    if not active_kernel.exists(path):
        return ProtectionJournalClassificationResult(
            ProtectionJournalClassification.ABSENT, None, f"no existe journal en '{path}'"
        )
    try:
        raw = active_kernel.read_all(path)
    except (OSError, ProtectionJournalStoreError) as exc:
        return ProtectionJournalClassificationResult(
            ProtectionJournalClassification.INDETERMINATE, None, f"no se pudo leer el journal '{path}': {exc}"
        )
    result = parse_journal_bytes(raw)
    if result.disposition is JournalLoadDisposition.VALID and result.journal is not None:
        return ProtectionJournalClassificationResult(
            ProtectionJournalClassification.VALID, result.journal, "journal válido"
        )
    if result.disposition is JournalLoadDisposition.TORN_TAIL:
        return ProtectionJournalClassificationResult(ProtectionJournalClassification.INDETERMINATE, None, result.detail)
    return ProtectionJournalClassificationResult(ProtectionJournalClassification.INDETERMINATE, None, result.detail)


def load_protection_journal(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
    kernel: JournalDurabilityKernel | None = None,
) -> ProtectionJournal:
    """Carga y valida el journal (fail-closed). El staging nunca es autoridad."""
    result = classify_protection_journal(operation_id, programdata_resolver=programdata_resolver, kernel=kernel)
    if result.classification is ProtectionJournalClassification.ABSENT:
        raise ProtectionJournalNotFoundError(f"No existe journal durable para la operación '{operation_id}'")
    if result.classification is ProtectionJournalClassification.INDETERMINATE:
        raise ProtectionJournalIndeterminateError(
            f"Journal de la operación '{operation_id}' con evidencia ambigua: {result.detail}"
        )
    if result.journal is None:  # pragma: no cover - invariante del clasificador
        raise ProtectionJournalIndeterminateError(
            f"Journal de la operación '{operation_id}' sin estado derivado: {result.detail}"
        )
    return result.journal


def _require_journal_matches_plan(journal: ProtectionJournal, plan: DurableAuthorizedPlan) -> None:
    """Liga el journal al plan autoritativo ya durable (§44).

    Comprueba ``operation_id``, ``authorized_plan_digest`` y la identidad física
    del Golden. Un digest distinto (otra operación, otro plan, plan alterado)
    falla cerrado: el journal no puede comandar una transacción que no corresponde
    al plan autorizado.
    """
    if journal.operation_id != plan.operation_id:
        raise ProtectionJournalPlanBindingError(
            f"operation_id del journal ('{journal.operation_id}') no corresponde al plan ('{plan.operation_id}')"
        )
    if journal.authorized_plan_digest != plan.digest:
        raise ProtectionJournalPlanBindingError(
            "authorized_plan_digest del journal no corresponde al plan autoritativo: "
            f"journal={journal.authorized_plan_digest}, plan={plan.digest}"
        )
    if (
        journal.physical_root.volume_serial_number != plan.volume_serial_number
        or journal.physical_root.root_file_id != plan.root_file_id
    ):
        raise ProtectionJournalPlanBindingError(
            "identidad física del Golden en el journal no corresponde al plan autoritativo"
        )
    if journal.physical_root.canonical_root != plan.canonical_root:
        raise ProtectionJournalPlanBindingError("canonical_root del journal no corresponde al plan autoritativo")


# ============================================================================
# Creación del journal
# ============================================================================


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def create_protection_journal(
    durable_plan: DurableAuthorizedPlan,
    *,
    programdata_resolver: Callable[[], object] | None = None,
    kernel: JournalDurabilityKernel | None = None,
    created_at: str | None = None,
) -> DurableProtectionJournal:
    """Crea el journal autoritativo de una operación sobre un plan YA durable (§53).

    Secuencia: crear el archivo protegido ``CREATE_NEW`` -> escribir el header
    (que liga ``authorized_plan_digest`` e identidad física) -> asentar el estado
    inicial ``APPLYING`` -> ``FlushFileBuffers`` -> RE-LEER y validar -> acuñar el
    objeto. Cualquier falla posterior a la creación retira el archivo creado por
    esta llamada: nunca queda autoridad a medias.
    """
    if not isinstance(durable_plan, DurableAuthorizedPlan):
        raise ProtectionJournalPlanBindingError(
            "create_protection_journal exige un DurableAuthorizedPlan (el journal sólo nace tras un plan durable)"
        )
    active_kernel = _resolve_kernel(kernel)
    path = derive_protection_journal_path(durable_plan.operation_id, programdata_resolver=programdata_resolver)

    parent = path.parent
    if not parent.is_dir():
        raise ProtectionJournalCreateError(
            f"El directorio de la operación '{parent}' no existe: el journal sólo se crea tras el plan durable"
        )
    if active_kernel.exists(path):
        raise ProtectionJournalAlreadyExistsError(
            f"El journal de la operación '{durable_plan.operation_id}' ya existe en '{path}'"
        )

    plan = durable_plan.plan
    header = build_journal_header_record(
        operation_id=plan.operation_id,
        authorized_plan_digest=plan.plan_digest,
        canonical_root=plan.canonical_root,
        volume_serial_number=plan.volume_serial_number,
        root_file_id=plan.root_file_id,
        created_at=created_at or _now_iso(),
    )
    initial_state = JournalTransactionRecord(sequence=2, state=ProtectionTransactionState(INITIAL_TRANSACTION_STATE))

    handle: int | None = None
    try:
        handle = active_kernel.create(path)
        active_kernel.append(handle, serialize_journal_record(header))
        active_kernel.append(handle, serialize_journal_record(initial_state))
        if not active_kernel.flush(handle):
            raise DurableJournalFlushError(
                f"FlushFileBuffers no confirmó la creación del journal '{path}': no hay autoridad transaccional"
            )

        # Re-lectura y validación completas ANTES de acuñar el objeto (§27).
        raw = active_kernel.read_all(path)
        parsed = parse_journal_bytes(raw)
        if not parsed.is_valid or parsed.journal is None:
            raise ProtectionJournalCreateError(
                f"El journal creado en '{path}' no superó la revalidación: {parsed.detail}"
            )
        if parsed.journal.authorized_plan_digest != plan.plan_digest:
            raise ProtectionJournalCreateError(f"El journal creado en '{path}' no liga contra el plan autoritativo")
    except BaseException:
        if handle is not None:
            active_kernel.close(handle)
        with contextlib.suppress(OSError):
            active_kernel.remove(path)
        raise

    return DurableProtectionJournal(
        journal=parsed.journal,
        path=path,
        kernel=active_kernel,
        handle=handle,
        plan=plan,
        records=(header, initial_state),
    )


# ============================================================================
# Journal durable: WAL por nodo
# ============================================================================


class DurableProtectionJournal:
    """Handle vivo del journal autoritativo de una operación.

    Sólo existe tras crear o abrir un journal validado y ligado al plan
    autoritativo. Posee el handle de anexado: cada transición se escribe, se
    flushea y sólo entonces se refleja en el estado en memoria.
    """

    __slots__ = ("_durability_failed", "_handle", "_journal", "_kernel", "_path", "_plan", "_records")

    _handle: int | None

    def __init__(
        self,
        *,
        journal: ProtectionJournal,
        path: pathlib.Path,
        kernel: JournalDurabilityKernel,
        handle: int,
        plan: AuthorizedPlan,
        records: tuple[JournalRecord, ...] = (),
    ) -> None:
        self._journal = journal
        self._path = path
        self._kernel = kernel
        self._handle = handle
        self._plan = plan
        self._records = records
        self._durability_failed = False

    # -- propiedades de sólo lectura -------------------------------------

    @property
    def journal(self) -> ProtectionJournal:
        return self._journal

    @property
    def path(self) -> pathlib.Path:
        return self._path

    @property
    def operation_id(self) -> str:
        return self._journal.operation_id

    @property
    def authorized_plan_digest(self) -> str:
        return self._journal.authorized_plan_digest

    @property
    def transaction_state(self) -> ProtectionTransactionState:
        return self._journal.transaction_state

    @property
    def is_closed(self) -> bool:
        return self._handle is None

    # -- binding de nodos -------------------------------------------------

    def node_binding(self, relative_path: str) -> NodeMutationBinding:
        """Binding del nodo tal como lo autorizó el plan (nunca desde el disco)."""
        node = self._plan.node_for(relative_path)
        if node is None:
            raise ProtectionJournalPlanBindingError(
                f"'{relative_path}' no pertenece al plan autorizado de la operación '{self.operation_id}'"
            )
        return NodeMutationBinding.from_node(node)

    def _require_binding(self, binding: NodeMutationBinding) -> NodeMutationBinding:
        if not isinstance(binding, NodeMutationBinding):
            raise ProtectionJournalPlanBindingError("el WAL exige un NodeMutationBinding completo")
        expected = self.node_binding(binding.relative_path)
        if (
            binding.volume_serial_number != expected.volume_serial_number
            or binding.file_id != expected.file_id
            or binding.pre_sd_sha256 != expected.pre_sd_sha256
        ):
            raise ProtectionJournalPlanBindingError(
                f"identidad física del nodo '{binding.relative_path}' no corresponde al plan autorizado: "
                "el WAL nunca se liga por índice desnudo"
            )
        return expected

    # -- WAL --------------------------------------------------------------

    def record_node_mutation_intent(self, binding: NodeMutationBinding) -> WalMutationPermit:
        """Asienta ``MUTATING(K)`` durable y devuelve el permiso de mutación.

        Orden inquebrantable (§33/§34): append -> ``FlushFileBuffers == TRUE`` ->
        permiso. Si el flush falla se lanza :class:`DurableJournalFlushError`, el
        nodo NO queda ``MUTATING``, no existe permiso y ningún callback de
        mutación puede ejecutarse.
        """
        self._require_live_handle()
        expected = self._require_binding(binding)
        current = self._journal.node_state(expected.relative_path)
        assert_node_transition(current, NodeWalState.MUTATING, expected.relative_path)

        record = JournalNodeRecord(
            sequence=self._journal.sequence + 1,
            relative_path=expected.relative_path,
            volume_serial_number=expected.volume_serial_number,
            file_id=expected.file_id,
            pre_sd_sha256=expected.pre_sd_sha256,
            state=NodeWalState.MUTATING,
        )
        self._append_record(record)
        return WalMutationPermit(
            record=record,
            operation_id=self.operation_id,
            authorized_plan_digest=self.authorized_plan_digest,
            journal_path=self._path,
            _proof=_MINT_PROOF,
        )

    def record_node_mutation_completed(self, binding: NodeMutationBinding) -> None:
        """Asienta ``MUTATED(K)`` durable (post-mutación, ya con permiso consumido)."""
        self._require_live_handle()
        expected = self._require_binding(binding)
        current = self._journal.node_state(expected.relative_path)
        assert_node_transition(current, NodeWalState.MUTATED, expected.relative_path)
        record = JournalNodeRecord(
            sequence=self._journal.sequence + 1,
            relative_path=expected.relative_path,
            volume_serial_number=expected.volume_serial_number,
            file_id=expected.file_id,
            pre_sd_sha256=expected.pre_sd_sha256,
            state=NodeWalState.MUTATED,
        )
        self._append_record(record)

    # -- FSM transaccional -------------------------------------------------

    def transition_to(self, target: ProtectionTransactionState) -> None:
        """Transiciona el FSM autoritativo de forma durable.

        S4-A no puede declarar el camino de éxito: ``COMMITTED`` y los estados de
        post-verificación (``VERIFYING_*``, ``ARCHIVING_BACKUP``) se rechazan porque
        sus gates no existen en este slice (§32). Sí se admite la rama de falla
        (``ROLLBACK_REQUIRED`` y siguientes), que S4-A puede alcanzar de verdad.
        """
        self._require_live_handle()
        if not isinstance(target, ProtectionTransactionState):
            raise ProtectionJournalSchemaError("target debe ser un miembro de ProtectionTransactionState")
        if target is ProtectionTransactionState.COMMITTED:
            raise PrematureCommitError(
                "COMMITTED exige GP1 HARDENED, RV-2 VERIFIED, NodeSet idéntico y ARCHIVING_BACKUP "
                "durable: ninguno de esos gates existe en S4-A"
            )
        if target not in S4A_PERMITTED_TRANSITION_TARGETS:
            raise ProtectionJournalSchemaError(
                f"transición '{self.transaction_state.value} -> {target.value}' fuera del alcance durable de S4-A "
                f"(destinos admitidos: {sorted(state.value for state in S4A_PERMITTED_TRANSITION_TARGETS)})"
            )
        assert_transaction_transition(self.transaction_state, target)
        self._append_record(JournalTransactionRecord(sequence=self._journal.sequence + 1, state=target))

    # -- infraestructura ---------------------------------------------------

    def _append_record(self, record: JournalRecord) -> None:
        payload = serialize_journal_record(record)
        handle = self._require_live_handle()
        try:
            self._kernel.append(handle, payload)
            flushed = self._kernel.flush(handle)
        except (ProtectionJournalStoreError, OSError):
            self._mark_durability_failure()
            raise
        if not flushed:
            self._mark_durability_failure()
            raise DurableJournalFlushError(
                f"FlushFileBuffers no confirmó el registro {record.kind.value} del journal "
                f"'{self._path}': sin permiso de mutación y sin estado durable"
            )
        self._records = (*self._records, record)
        self._journal = replay_journal_records(self._records)

    def _mark_durability_failure(self) -> None:
        """Ante un flush no confirmado, la evidencia durable es desconocida (§20 C4b).

        NO se escribe nada más: si ``FlushFileBuffers`` no confirmó el registro
        anterior, no se puede saber si aterrizó, de modo que anexar otro registro
        sólo produciría un journal estructuralmente roto además de ambiguo. El
        objeto queda envenenado (``INDETERMINATE`` en memoria y sin más anexos) y
        la clasificación por evidencia observable decide qué pasó en disco: el
        registro puede estar (``MUTATING`` durable: la mutación PUEDE haber
        ocurrido) o no estar (sin intención durable).
        """
        object.__setattr__(self._journal, "transaction_state", ProtectionTransactionState.INDETERMINATE)
        self._durability_failed = True

    def _require_live_handle(self) -> int:
        if self._durability_failed:
            raise DurableJournalFlushError(
                f"el journal de la operación '{self.operation_id}' sufrió un fallo de durabilidad: "
                "no se puede anexar ni emitir permiso de mutación sobre evidencia ambigua"
            )
        if self._handle is None:
            raise ProtectionJournalStoreError(
                f"el journal de la operación '{self.operation_id}' está cerrado: no se puede anexar"
            )
        return self._handle

    def close(self) -> None:
        if self._handle is None:
            return
        self._kernel.close(self._handle)
        self._handle = None


def open_protection_journal(
    operation_id: str,
    durable_plan: DurableAuthorizedPlan,
    *,
    programdata_resolver: Callable[[], object] | None = None,
    kernel: JournalDurabilityKernel | None = None,
) -> DurableProtectionJournal:
    """Abre el journal existente y lo liga al plan autoritativo (proceso B)."""
    if not isinstance(durable_plan, DurableAuthorizedPlan):
        raise ProtectionJournalPlanBindingError(
            "open_protection_journal exige un DurableAuthorizedPlan acuñado desde disco protegido"
        )
    active_kernel = _resolve_kernel(kernel)
    path = derive_protection_journal_path(operation_id, programdata_resolver=programdata_resolver)
    if not active_kernel.exists(path):
        raise ProtectionJournalNotFoundError(
            f"No existe journal durable para la operación '{operation_id}' en '{path}'"
        )

    raw = active_kernel.read_all(path)
    parsed = parse_journal_bytes(raw)
    if not parsed.is_valid or parsed.journal is None:
        if parsed.disposition is JournalLoadDisposition.TORN_TAIL:
            raise ProtectionJournalIndeterminateError(f"journal '{path}' con escritura cortada: {parsed.detail}")
        raise ProtectionJournalSchemaError(f"journal '{path}' inválido: {parsed.detail}")
    journal = parsed.journal
    _require_journal_matches_plan(journal, durable_plan)

    records = _records_from_bytes(raw)
    handle = active_kernel.open_append(path)
    return DurableProtectionJournal(
        journal=journal,
        path=path,
        kernel=active_kernel,
        handle=handle,
        plan=durable_plan.plan,
        records=records,
    )


def _records_from_bytes(raw: bytes) -> tuple[JournalRecord, ...]:
    """Reconstruye la lista de registros ya validados por :func:`parse_journal_bytes`."""
    from sky_claw.local.runtime_vault.protection_journal import _parse_journal_line

    return tuple(_parse_journal_line(line) for line in raw.split(b"\n")[:-1])


def reload_protection_journal_after_restart(
    operation_id: str,
    *,
    programdata_resolver: Callable[[], object] | None = None,
    kernel: JournalDurabilityKernel | None = None,
    plan_loader: Callable[[str], DurableAuthorizedPlan] | None = None,
) -> tuple[DurableAuthorizedPlan, ProtectionJournal | None, ProtectionJournalClassification]:
    """Recarga tras reinicio: plan protegido + journal, sin staging y sin recovery.

    Demuestra el oráculo C: el proceso B carga la copia protegida del plan, la
    valida criptográficamente, carga el journal, verifica el binding y clasifica
    el estado. NO ejecuta recuperación alguna: la ambigüedad de ``MUTATING(K)``
    se preserva para S4-C.
    """
    loader = plan_loader or load_durable_authorized_plan
    plan = loader(operation_id)
    result = classify_protection_journal(operation_id, programdata_resolver=programdata_resolver, kernel=kernel)
    if result.classification is ProtectionJournalClassification.ABSENT:
        # Fila B de la matriz de crash (§20): el plan es cargable, pero sin
        # journal NO se infiere ningún estado transaccional ni ninguna mutación.
        return plan, None, result.classification
    if result.classification is ProtectionJournalClassification.INDETERMINATE or result.journal is None:
        raise ProtectionJournalIndeterminateError(
            f"journal de la operación '{operation_id}' con evidencia ambigua: {result.detail}"
        )
    _require_journal_matches_plan(result.journal, plan)
    return plan, result.journal, result.classification


__all__ = [
    "DurableJournalFlushError",
    "DurableProtectionJournal",
    "JournalDurabilityKernel",
    "JournalRecord",
    "NodeMutationBinding",
    "PrematureCommitError",
    "PROTECTION_JOURNAL_FILE_NAME",
    "PROTECTION_JOURNAL_OBJECT_NAME",
    "PROTECTION_JOURNAL_SCHEMA_VERSION",
    "ProtectionJournalAlreadyExistsError",
    "ProtectionJournalAppendError",
    "ProtectionJournalClassification",
    "ProtectionJournalClassificationResult",
    "ProtectionJournalCreateError",
    "ProtectionJournalError",
    "ProtectionJournalIndeterminateError",
    "ProtectionJournalNotFoundError",
    "ProtectionJournalPlanBindingError",
    "ProtectionJournalSchemaError",
    "ProtectionJournalStoreError",
    "ProtectionJournalUnsupportedError",
    "ProtectionTransactionState",
    "WalMutationPermit",
    "classify_protection_journal",
    "create_protection_journal",
    "derive_protection_journal_path",
    "load_protection_journal",
    "open_protection_journal",
    "reload_protection_journal_after_restart",
]

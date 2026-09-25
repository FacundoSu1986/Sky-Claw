"""Global TGR serialization lock — exclusión cross-process del RMW del registry (GP2-P2).

Problema que resuelve (ADR 0010 §11.4, línea normativa del replace atómico):

    ATOMIC FILE REPLACE != ATOMIC READ-MODIFY-WRITE

``_write_trusted_registry_atomically_at`` garantiza reemplazo atómico del archivo,
pero dos writers que hacen load → modify → write sobre el mismo
``trusted_goldens.json`` pierden updates (last-writer-wins entre el load y el
replace). Este módulo aporta la SECCIÓN CRÍTICA GLOBAL:

    ACQUIRE → LOAD → MODIFY → WRITE ATOMICALLY → POST-VALIDATE → RELEASE

como una única propiedad de serialización, para que P3
(``REGISTER_OR_REFRESH_TRUSTED_GOLDEN``) pueda componer sin lost update.

Contrato de alcance (INMUTABLE, anclado por tests)::

    TGR_LOCK_PROTECTS_REGISTRY_RMW      = YES
    TGR_LOCK_PROTECTS_GOLDEN_CONTENT    = NO

Este lock es un GLOBAL TGR SERIALIZATION LOCK. Única autoridad: serializar
cambios al Trusted Golden Registry. NO es GoldenMutationLock, NO es un
filesystem-content lock, NO congela el Golden, NO cierra la ventana TOCTOU y NO
es una garantía de estabilidad de contenido para RV-2 (ADR 0010 §11.4: "Si se
adopta un global TGR lock, sólo protege consistencia/serialización del registry;
no congela el Golden ni cierra la ventana TOCTOU").

Primitiva Win32 (consistente con ADR 0010 §19.1 y GoldenMutationLock):

    CreateFileW(
        dwDesiredAccess       = GENERIC_READ | GENERIC_WRITE,
        dwShareMode           = 0    (EXCLUSIVO),
        dwCreationDisposition = OPEN_ALWAYS,
        dwFlagsAndAttributes  = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
    )

- La EXCLUSIÓN pertenece al kernel: ``LOCK OWNERSHIP == HANDLE LIFETIME``. El
  handle se mantiene abierto durante toda la sección crítica y se cierra sólo en
  ``release()`` / ``finally``. Si el proceso muere, el kernel cierra el handle y
  la exclusión desaparece: otro proceso puede adquirir después sin intervención
  manual (crash safety). La mera existencia del archivo ``*.lock`` residual NUNCA
  equivale a LOCK ACTIVE.
- SIN metadata persistente: el archivo queda VACÍO y persistente. Cualquier
  contenido escrito sería una segunda fuente de verdad ("el archivo dice X")
  que §14 prohíbe confundir con la exclusión real. El handle es la ÚNICA
  fuente de exclusión.
- SIN borrado jamás (release normal, crash o power loss): el archivo se crea con
  OPEN_ALWAYS y persiste residual e inerte. Esto elimina estructuralmente la
  carrera de borrado "A closes → B acquires → A deletes": A jamás borra.
- Reparse: apertura con FILE_FLAG_OPEN_REPARSE_POINT y verificación post-open de
  ``ReparseTag == 0`` (§19.1.6): sustitución por junction/symlink aborta
  fail-closed tipado.
- Lock identity: SHA-256 del registry path canónico (absoluto, ancestros
  existentes resueltos, normcase). GLOBAL al registry: dos writers del MISMO
  ``trusted_goldens.json`` colisionan sobre el MISMO lock file aunque usen
  distinta grafía de path; registries distintos jamás comparten lock. No hay
  lock por Golden/root individual (writer root A y writer root B sobre el mismo
  registry comparten este lock). Una colisión criptográfica hipotética produce
  sobre-serialización (seguro), jamás exclusión insuficiente.
- Reentrancia: RECHAZO TIPADO inmediato (mismo hilo intentando re-adquirir).
  No hay reentrancia soportada: una sección crítica RMW anidada sobre el mismo
  registry sería deadlock/lost update consigo misma.
- Espera acotada: ``timeout`` finito configurable (default 30 s; ``0`` = un
  solo intento). Sin busy spin (poll dormido), sin espera infinita, fallo
  determinista tipado (``TrustedRegistryLockBusyError``). Obtener BUSY/TIMEOUT
  NUNCA es permiso para escribir igual.
- Ubicación: ``%ProgramData%\\Sky-Claw\\runtime_vault\\locks\\`` (namespace de
  locks existente, ADR 0010 §11.3): Authenticated Users tiene derechos sólo de
  directorio (0x001200A9, sin herencia a archivos): no puede precrear, abrir ni
  leer ``*.lock`` (UNPRIVILEGED_LOCK_FILE_READ = FORBIDDEN). Cero superficie
  nueva writable.

API difícil de usar mal: la vía productiva (:func:`mutate_trusted_golden_registry`)
NO recibe paths — deriva internamente el registry normativo y su lock (patrón
M-L3 de GoldenMutationLock: el caller no puede inyectar una ruta que desacople
el lock del registry escrito). El callback ``mutate`` sólo recibe el registro
cargado BAJO LOCK y devuelve el registro nuevo: load y write ocurren
estructuralmente dentro de la exclusión. Los seams ``_..._at`` (convención del
repo) permiten tests con paths/locks dir explícitos.

Plataforma: la semántica productiva es Windows-only (Win32 share-mode). En
POSIX se lanza ``TrustedRegistryUnsupportedError`` fail-closed; jamás se ofrece
una falsa exclusión POSIX. El seam ``kernel`` permite probar la máquina de
estados con un kernel fake (mismo patrón que ``GoldenLockKernel``).

GP2 apply NUNCA escribe TGR: este módulo es la primitiva de serialización para
el futuro flujo privilegiado de registro/refresco (P3). No decide admisión,
digests, receipts ni semántica REGISTER_OR_REFRESH.

Sincronicidad: la primitiva es SÍNCRONA y bloqueante por diseño (CreateFileW +
poll dormido; sin wrappers async artificiales). En contextos asyncio (NiceGUI,
agente LLM) invocarla vía ``asyncio.to_thread`` — AGENTS.md: jamás bloquear el
event loop.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import pathlib
import sys
import threading
import time
from collections.abc import Callable
from typing import Final, Protocol

from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenRegistry,
    TrustedRegistryError,
    TrustedRegistryUnsupportedError,
    _write_trusted_registry_atomically_at,
    load_trusted_golden_registry,
)

# ============================================================================
# Contrato de Alcance (anclado por tests; inmutable)
# ============================================================================

#: El lock serializa ciclos enteros LOAD→MODIFY→WRITE sobre el TGR.
TGR_LOCK_PROTECTS_REGISTRY_RMW: Final[bool] = True

#: El lock NO congela el contenido del Golden ni cierra la ventana TOCTOU.
TGR_LOCK_PROTECTS_GOLDEN_CONTENT: Final[bool] = False

# ============================================================================
# Jerarquía de Excepciones
# ============================================================================


class TrustedRegistryLockError(TrustedRegistryError):
    """Base de errores del global TGR serialization lock (fail-closed)."""


class TrustedRegistryLockBusyError(TrustedRegistryLockError):
    """No se obtuvo la exclusión dentro del timeout acotado (BUSY/TIMEOUT).

    NUNCA es permiso para escribir sin exclusión: el caller debe abortar o
    reintentar la operación completa más tarde.
    """

    def __init__(self, *args: object, win32_error: int | None = None, timeout_seconds: float | None = None) -> None:
        super().__init__(*args)
        self.win32_error = win32_error
        self.timeout_seconds = timeout_seconds


class TrustedRegistryLockReentrancyError(TrustedRegistryLockError):
    """Reentrancia en el mismo hilo: rechazo tipado inmediato (no hay retry)."""


class TrustedRegistryLockSecurityError(TrustedRegistryLockError):
    """Path de lock/registry inválido o namespace/objeto no confiable (reparse, sustitución)."""


class TrustedRegistryLockOSError(TrustedRegistryLockError):
    """Fallo Win32 de apertura/cierre del lock file (platform error)."""

    def __init__(self, *args: object, win32_error: int | None = None) -> None:
        super().__init__(*args)
        self.win32_error = win32_error


class TrustedRegistryLockOwnershipError(TrustedRegistryLockError):
    """Uso del handle después de su liberación o cierre fuera de ciclo de vida."""


# ============================================================================
# Constantes Normativas (contrato Win32, anclados por tests)
# ============================================================================

_GENERIC_READ: Final[int] = 0x80000000
_GENERIC_WRITE: Final[int] = 0x40000000

#: dwDesiredAccess contractual: GENERIC_READ | GENERIC_WRITE.
TGR_LOCK_DESIRED_ACCESS: Final[int] = _GENERIC_READ | _GENERIC_WRITE

#: dwShareMode contractual: 0 estrictamente (NUNCA FILE_SHARE_*).
TGR_LOCK_SHARE_MODE: Final[int] = 0

#: dwCreationDisposition contractual: OPEN_ALWAYS (residual file reutilizable, jamás borrado).
TGR_LOCK_CREATION_DISPOSITION: Final[int] = 4

_FILE_ATTRIBUTE_NORMAL: Final[int] = 0x00000080
_FILE_FLAG_OPEN_REPARSE_POINT: Final[int] = 0x00200000

#: dwFlagsAndAttributes contractual.
TGR_LOCK_FLAGS: Final[int] = _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT

#: Prefijo de nombre del lock file: distinto por diseño de ``skyclaw_golden_lock_``
#: (GoldenMutationLock) — los dos locks son primitivas DIFERENTES con identidad
#: y semántica distintas; jamás deben confundirse.
TGR_LOCK_NAME_PREFIX: Final[str] = "skyclaw_tgr_lock_"
TGR_LOCK_FILE_SUFFIX: Final[str] = ".lock"

#: Espera máxima por defecto para la adquisición (acotada, configurable).
DEFAULT_TGR_LOCK_TIMEOUT_SECONDS: Final[float] = 30.0

#: Intervalo de poll dormido entre intentos (sin busy spin).
TGR_LOCK_POLL_INTERVAL_SECONDS: Final[float] = 0.02

_ERROR_SHARING_VIOLATION: Final[int] = 32
_ERROR_LOCK_VIOLATION: Final[int] = 33

TrustedRegistryMutator = Callable[[TrustedGoldenRegistry], TrustedGoldenRegistry]


# ============================================================================
# Kernel del Lock (seam) — nativo Win32 y fake testeable en POSIX
# ============================================================================


class TrustedRegistryLockKernel(Protocol):
    """Primitivas de kernel para el ciclo de vida del lock (único punto Win32).

    Errores tipados, jamás éxito fabricado. Permite probar causalmente la
    adquisición/liberación/timeout/reentrancia con un fake en POSIX mientras la
    clase nativa corre en Windows (mismo patrón que ``GoldenLockKernel``).
    """

    def open_lock_file(self, path: pathlib.PurePath) -> int: ...
    def get_reparse_tag(self, handle: int) -> int: ...
    def close_handle(self, handle: int) -> None: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


if sys.platform == "win32":
    from ctypes import wintypes as _wt

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", _wt.DWORD),
            ("ReparseTag", _wt.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        _wt.LPCWSTR,
        _wt.DWORD,
        _wt.DWORD,
        _wt.LPVOID,
        _wt.DWORD,
        _wt.DWORD,
        _wt.HANDLE,
    ]
    _kernel32.CreateFileW.restype = _wt.HANDLE
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        _wt.HANDLE,
        ctypes.c_int,
        _wt.LPVOID,
        _wt.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = _wt.BOOL
    _kernel32.CloseHandle.argtypes = [_wt.HANDLE]
    _kernel32.CloseHandle.restype = _wt.BOOL

_FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG: Final[int] = 9
_INVALID_HANDLE_VALUES: Final[tuple[int, ...]] = (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise TrustedRegistryUnsupportedError(
            "El global TGR serialization lock (CreateFileW share=0) solo está soportado en Windows"
        )


def _is_invalid_handle(handle: object) -> bool:
    if handle is None or handle == 0:
        return True
    return handle in _INVALID_HANDLE_VALUES


class _Win32TgrLockKernel:
    """Kernel nativo: CreateFileW share=0 + reparse check + cierre exactamente una vez."""

    def open_lock_file(self, path: pathlib.PurePath) -> int:
        handle = _kernel32.CreateFileW(
            str(path),
            TGR_LOCK_DESIRED_ACCESS,
            TGR_LOCK_SHARE_MODE,
            None,
            TGR_LOCK_CREATION_DISPOSITION,
            TGR_LOCK_FLAGS,
            None,
        )
        if _is_invalid_handle(handle):
            err = ctypes.get_last_error()
            if err in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION):
                raise TrustedRegistryLockBusyError(
                    f"Otro writer posee el global TGR lock '{path}' (dwShareMode=0): Win32 {err}",
                    win32_error=err,
                )
            raise TrustedRegistryLockOSError(
                f"CreateFileW falló al abrir el TGR lock '{path}': código {err}", win32_error=err
            )
        return int(handle)

    def get_reparse_tag(self, handle: int) -> int:
        info = _FileAttributeTagInfo()
        if not _kernel32.GetFileInformationByHandleEx(
            handle,
            _FILE_INFO_BY_HANDLE_CLASS_ATTRIBUTE_TAG,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            err = ctypes.get_last_error()
            raise TrustedRegistryLockOSError(
                f"GetFileInformationByHandleEx falló sobre el TGR lock: código {err}", win32_error=err
            )
        return int(info.ReparseTag)

    def close_handle(self, handle: int) -> None:
        if _is_invalid_handle(handle):
            raise TrustedRegistryLockOwnershipError("Cierre de handle de TGR lock inválido")
        if not _kernel32.CloseHandle(handle):
            err = ctypes.get_last_error()
            raise TrustedRegistryLockOSError(f"CloseHandle falló sobre el TGR lock: código {err}", win32_error=err)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# ============================================================================
# Reentrancia (guard intra-hilo; la exclusión cross-* la sigue dando el kernel)
# ============================================================================

_thread_state = threading.local()


def _held_lock_keys() -> set[str]:
    held = getattr(_thread_state, "held_lock_keys", None)
    if held is None:
        held = set()
        _thread_state.held_lock_keys = held
    return held


# ============================================================================
# Identidad del Lock (global al registry path canónico)
# ============================================================================


def canonicalize_trusted_registry_identity(registry_path: pathlib.Path | str | os.PathLike[str]) -> str:
    """Identidad canónica del registry: absoluto + ancestros existentes resueltos + normcase.

    Formas equivalentes del mismo path (``.``, ``..``, separadores repetidos,
    junctions en ancestros existentes, casing en Windows) producen la MISMA
    identidad — y por tanto el MISMO lock. Paths NUL/vacíos/no-pathlike son
    inválidos (fail-closed tipado).
    """
    try:
        raw = os.fspath(registry_path)
    except TypeError as exc:
        raise TrustedRegistryLockSecurityError("registry_path debe ser str o PathLike") from exc
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise TrustedRegistryLockSecurityError("registry_path vacío o con NUL: identidad de lock inválida")

    base = pathlib.Path(raw)
    absolute = pathlib.Path(os.path.abspath(os.fspath(base)))

    # Resolver los ancestros existentes (junctions/symlinks) y recomponer el
    # resto: mismo algoritmo de canonicalización que locking._canonical_lock_key.
    current = absolute
    missing_parts: list[str] = []
    while not current.exists() and current != current.parent:
        missing_parts.append(current.name)
        current = current.parent
    missing_parts.reverse()
    try:
        resolved = current.resolve()
    except OSError:
        resolved = current
    for part in missing_parts:
        resolved = resolved / part

    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(resolved))))


def derive_trusted_registry_lock_key(registry_path: pathlib.Path | str | os.PathLike[str]) -> str:
    """Clave identitaria del lock: ``skyclaw_tgr_lock_<sha256(identidad)>``.

    SHA-256 completo (sin truncar): sin ambigüedad de lock-key. Una colisión
    hipotética entre registries distintos compartiría lock (sobre-serialización,
    siempre seguro); jamás podría dar dos locks para el mismo registry.
    """
    identity = canonicalize_trusted_registry_identity(registry_path)
    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogateescape")).hexdigest()
    return f"{TGR_LOCK_NAME_PREFIX}{digest}"


def _derive_trusted_registry_lock_path_at(
    locks_dir: pathlib.PurePath,
    registry_path: pathlib.Path | str | os.PathLike[str],
) -> pathlib.PurePath:
    """Seam ``_at`` (convención del repo): path del lock bajo un locks dir explícito."""
    if not isinstance(locks_dir, pathlib.PurePath) or not locks_dir.is_absolute():
        raise TrustedRegistryLockSecurityError("locks_dir del seam debe ser un path absoluto")
    key = derive_trusted_registry_lock_key(registry_path)
    return locks_dir / f"{key}{TGR_LOCK_FILE_SUFFIX}"


def derive_trusted_registry_path(*, programdata_resolver: Callable[[], object] | None = None) -> pathlib.Path:
    """Ruta normativa del TGR: ``%ProgramData%\\Sky-Claw\\runtime_vault\\trusted_goldens.json``.

    La raíz se resuelve vía ``SHGetKnownFolderPath(FOLDERID_ProgramData)`` (S3a),
    jamás por env var; ``programdata_resolver`` es seam de verificación que
    aporta la RAÍZ ProgramData (mismo contrato que
    ``derive_golden_lock_path``).
    """
    return pathlib.Path(_resolve_runtime_vault_dir(programdata_resolver=programdata_resolver) / "trusted_goldens.json")


def derive_trusted_registry_lock_path(
    registry_path: pathlib.Path | str | os.PathLike[str],
    *,
    programdata_resolver: Callable[[], object] | None = None,
) -> pathlib.PurePath:
    """Path canónico del lock en ``%ProgramData%\\Sky-Claw\\runtime_vault\\locks\\``.

    El nombre se deriva de la identidad del registry (hash SHA-256); el caller
    no puede inyectar el path del lock. Se reutiliza el namespace de locks
    existente (DACL §11.3: sin herencia a ``*.lock``, precreación no
    privilegiada imposible).
    """
    locks_dir = _resolve_runtime_vault_dir(programdata_resolver=programdata_resolver) / "locks"
    return _derive_trusted_registry_lock_path_at(locks_dir, registry_path)


def _resolve_runtime_vault_dir(*, programdata_resolver: Callable[[], object] | None = None) -> pathlib.PurePath:
    base: pathlib.PurePath
    if programdata_resolver is None:
        _ensure_windows()
        from sky_claw.local.runtime_vault.trusted_namespace import _resolve_programdata_known_folder

        base = pathlib.Path(str(_resolve_programdata_known_folder()))
    else:
        raw = programdata_resolver()
        if isinstance(raw, str):
            if not raw.strip() or "\x00" in raw:
                raise TrustedRegistryLockSecurityError("El resolver de ProgramData devolvió una raíz vacía o con NUL")
            base = pathlib.PureWindowsPath(raw)
        elif isinstance(raw, pathlib.PurePath):
            base = raw
        else:
            raise TrustedRegistryLockSecurityError(
                f"El resolver de ProgramData devolvió un tipo no contratado: {type(raw).__name__}"
            )
    if not base.is_absolute():
        raise TrustedRegistryLockSecurityError(f"La raíz ProgramData resuelta no es absoluta: {base!r}")
    return base / "Sky-Claw" / "runtime_vault"


# ============================================================================
# Validación de Argumentos
# ============================================================================


def _validate_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TrustedRegistryLockError("timeout debe ser un número de segundos finito >= 0")
    value = float(timeout)
    if value < 0.0 or value != value or value in (float("inf"), float("-inf")):
        raise TrustedRegistryLockError(
            f"timeout inválido ({timeout!r}): se exige espera acotada (finita, >= 0). La espera infinita está prohibida."
        )
    return value


# ============================================================================
# Handle del Lock (ownership == handle lifetime; sin metadata persistente)
# ============================================================================

_MINT_PROOF: object = object()


class TrustedRegistryWriteLockHandle:
    """Ownership del handle exclusivo del global TGR lock.

    - Sólo se acuña desde la adquisición (proof privada).
    - ``release()`` cierra el handle EXACTAMENTE una vez (el archivo NO se
      borra ni se escribe jamás: residual vacío e inerte). Cualquier uso
      posterior lanza ``TrustedRegistryLockOwnershipError``.
    - El handle permanece abierto durante toda la sección crítica
      (``LOCK OWNERSHIP == HANDLE LIFETIME``).
    """

    __slots__ = ("_closed", "_handle", "_kernel", "_lock_key", "_lock_path", "_registry_identity")

    def __init__(
        self,
        handle: int,
        *,
        lock_key: str,
        registry_identity: str,
        lock_path: pathlib.PurePath,
        kernel: TrustedRegistryLockKernel,
        _proof: object = None,
    ) -> None:
        if _proof is not _MINT_PROOF:
            raise TrustedRegistryLockOwnershipError(
                "TrustedRegistryWriteLockHandle sólo puede crearse desde la adquisición del TGR lock"
            )
        if _is_invalid_handle(handle):
            raise TrustedRegistryLockOwnershipError("No se puede tomar ownership de un handle de TGR lock inválido")
        self._handle = int(handle)
        self._lock_key = lock_key
        self._registry_identity = registry_identity
        self._lock_path = lock_path
        self._kernel = kernel
        self._closed = False

    @property
    def lock_key(self) -> str:
        return self._lock_key

    @property
    def registry_identity(self) -> str:
        return self._registry_identity

    @property
    def lock_path(self) -> pathlib.PurePath:
        return self._lock_path

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def raw_handle(self) -> int:
        """Handle prestado (borrowing) para Win32 interno: el consumidor NO lo cierra."""
        if self._closed:
            raise TrustedRegistryLockOwnershipError("Uso del TGR lock después de su liberación")
        return self._handle

    def release(self) -> bool:
        """Libera el lock cerrando el handle exactamente una vez. True si esta llamada liberó.

        El archivo residual NO se borra jamás (sin delete-race) y NO se escribe
        (sin metadata: la exclusión vive sólo en el handle). Si ``CloseHandle``
        falla, NUNCA se afirma que el lock quedó libre (fail-closed): el error
        se propaga tipado, el handle queda retirado exactamente una vez y el
        guard de reentrancia se CONSERVA — sólo la muerte del proceso (reaps
        del kernel) podría liberar una exclusión indemostrable.
        """
        if self._closed:
            return False
        self._closed = True
        self._kernel.close_handle(self._handle)
        _held_lock_keys().discard(self._lock_key)
        return True

    def __enter__(self) -> TrustedRegistryWriteLockHandle:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Sale de la sección crítica liberando SIEMPRE el handle exactamente una vez.

        Si el bloque terminó en excepción, un fallo de liberación JAMÁS la
        reemplaza: la excepción original se preserva (se propagará tal cual) y
        el fallo de ``CloseHandle`` queda adjunto como nota sobre ella. Sin
        excepción original, el fallo tipado de liberación se propaga.
        """
        if exc_type is None:
            self.release()
            return
        try:
            self.release()
        except Exception as close_err:  # noqa: BLE001 - en unwind la excepción original MANDA
            if isinstance(exc_val, BaseException):
                exc_val.add_note(f"TGR lock: la liberación falló durante el unwind: {close_err!r}")


# ============================================================================
# Adquisición (espera acotada, poll dormido, fallo determinista)
# ============================================================================


def _acquire_lock_core(
    lock_path: pathlib.PurePath,
    lock_key: str,
    registry_identity: str,
    *,
    timeout: float,
    kernel: TrustedRegistryLockKernel,
) -> TrustedRegistryWriteLockHandle:
    """Núcleo de adquisición: reentrancia tipada, CreateFileW share=0 acotado, reparse check."""
    timeout_s = _validate_timeout(timeout)

    held = _held_lock_keys()
    if lock_key in held:
        raise TrustedRegistryLockReentrancyError(
            f"Reentrancia detectada: este hilo ya posee el global TGR lock '{lock_key}' "
            "(la reentrancia no está soportada; la sección crítica RMW no es anidable)"
        )

    deadline = kernel.monotonic() + timeout_s
    handle: int | None = None
    while True:
        try:
            handle = kernel.open_lock_file(lock_path)
            break
        except TrustedRegistryLockBusyError as busy_exc:
            now = kernel.monotonic()
            if now >= deadline:
                raise TrustedRegistryLockBusyError(
                    f"Global TGR lock '{lock_path}' no adquirido dentro del timeout acotado de {timeout_s}s "
                    "(otro writer lo posee o el timeout expiró): fail-closed, NO se escribe sin exclusión",
                    win32_error=busy_exc.win32_error,
                    timeout_seconds=timeout_s,
                ) from busy_exc
            kernel.sleep(min(TGR_LOCK_POLL_INTERVAL_SECONDS, max(deadline - now, 0.0)))

    success = False
    try:
        if kernel.get_reparse_tag(handle) != 0:
            raise TrustedRegistryLockSecurityError(
                f"El TGR lock '{lock_path}' fue sustituido por un reparse point: fail-closed"
            )
        lock_handle = TrustedRegistryWriteLockHandle(
            handle,
            lock_key=lock_key,
            registry_identity=registry_identity,
            lock_path=lock_path,
            kernel=kernel,
            _proof=_MINT_PROOF,
        )
        held.add(lock_key)
        success = True
        return lock_handle
    finally:
        if not success and handle is not None:
            kernel.close_handle(handle)


def acquire_trusted_registry_write_lock(
    *,
    timeout: float = DEFAULT_TGR_LOCK_TIMEOUT_SECONDS,
    kernel: TrustedRegistryLockKernel | None = None,
    programdata_resolver: Callable[[], object] | None = None,
) -> TrustedRegistryWriteLockHandle:
    """Adquiere el global TGR write lock del registry NORMATIVO (espera acotada).

    El caller NO pasa paths (M-L3): el registry normativo y su lock se derivan
    internamente. La sección crítica correcta es::

        with acquire_trusted_registry_write_lock() as lock:
            registry = load_trusted_golden_registry(...)
            ... modificar ...
            _write_trusted_registry_atomically_at(...)

    Preferí :func:`mutate_trusted_golden_registry`, que estructuralmente impide
    olvidar que el LOAD debe ocurrir BAJO lock. Resultado: ACQUIRED | BUSY/
    TIMEOUT (``TrustedRegistryLockBusyError``) | SECURITY
    (``TrustedRegistryLockSecurityError``) | PLATFORM
    (``TrustedRegistryLockOSError``) | REENTRANCY
    (``TrustedRegistryLockReentrancyError``) | UNSUPPORTED (POSIX,
    ``TrustedRegistryUnsupportedError``). BUSY jamás autoriza a escribir.

    Síncrono y bloqueante por diseño: en asyncio usar ``asyncio.to_thread``
    (AGENTS.md: jamás bloquear el event loop).
    """
    active_kernel: TrustedRegistryLockKernel
    if kernel is None:
        _ensure_windows()
        active_kernel = _Win32TgrLockKernel()
    else:
        active_kernel = kernel
    registry_path = derive_trusted_registry_path(programdata_resolver=programdata_resolver)
    lock_path = derive_trusted_registry_lock_path(registry_path, programdata_resolver=programdata_resolver)
    identity = canonicalize_trusted_registry_identity(registry_path)
    return _acquire_lock_core(
        lock_path,
        derive_trusted_registry_lock_key(registry_path),
        identity,
        timeout=timeout,
        kernel=active_kernel,
    )


def _acquire_trusted_registry_write_lock_at(
    locks_dir: pathlib.PurePath,
    registry_path: pathlib.Path | str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_TGR_LOCK_TIMEOUT_SECONDS,
    kernel: TrustedRegistryLockKernel | None = None,
) -> TrustedRegistryWriteLockHandle:
    """Seam de verificación ``_at``: adquiere el lock de un registry explícito bajo un locks dir explícito.

    Dos writers del MISMO ``registry_path`` derivan SIEMPRE el mismo lock
    (misma identidad, mismo archivo). La rama productiva es
    :func:`acquire_trusted_registry_write_lock`.
    """
    active_kernel: TrustedRegistryLockKernel
    if kernel is None:
        _ensure_windows()
        active_kernel = _Win32TgrLockKernel()
    else:
        active_kernel = kernel
    lock_path = _derive_trusted_registry_lock_path_at(locks_dir, registry_path)
    identity = canonicalize_trusted_registry_identity(registry_path)
    return _acquire_lock_core(
        lock_path,
        derive_trusted_registry_lock_key(registry_path),
        identity,
        timeout=timeout,
        kernel=active_kernel,
    )


# ============================================================================
# Transacción RMW (sección crítica completa: acquire → load → modify → write)
# ============================================================================


def _mutate_trusted_registry_core(
    registry_path: pathlib.Path | str | os.PathLike[str],
    lock_path: pathlib.PurePath,
    lock_key: str,
    registry_identity: str,
    mutate: TrustedRegistryMutator,
    *,
    timeout: float,
    kernel: TrustedRegistryLockKernel,
) -> TrustedGoldenRegistry:
    """Sección crítica RMW COMPLETA dentro de un mismo ownership del lock.

    Orden (anclado por el mutation-anchor test): ACQUIRE → LOAD → MODIFY →
    WRITE atómico (con post-validación del writer) → RELEASE. El LOAD jamás
    ocurre fuera de la exclusión: eso volvería el lost update posible aunque
    el write sea atómico.
    """
    if not callable(mutate):
        raise TrustedRegistryLockError("mutate debe ser un callable (registry_actual) -> registry_nuevo")

    registry = pathlib.Path(os.fspath(registry_path))
    with _acquire_lock_core(
        lock_path,
        lock_key,
        registry_identity,
        timeout=timeout,
        kernel=kernel,
    ):
        current = load_trusted_golden_registry(registry)
        updated = mutate(current)
        if not isinstance(updated, TrustedGoldenRegistry):
            raise TrustedRegistryError(
                "mutate debe devolver un TrustedGoldenRegistry: salida inválida rechazada sin escribir"
            )
        _write_trusted_registry_atomically_at(updated, registry)
        return updated


def mutate_trusted_golden_registry(
    mutate: TrustedRegistryMutator,
    *,
    timeout: float = DEFAULT_TGR_LOCK_TIMEOUT_SECONDS,
    kernel: TrustedRegistryLockKernel | None = None,
    programdata_resolver: Callable[[], object] | None = None,
) -> TrustedGoldenRegistry:
    """Ejecuta un ciclo RMW sobre el TGR NORMATIVO como sección crítica global.

    Uso previsto (P3)::

        def registrar(current):
            ... construir el nuevo TrustedGoldenRegistry a partir de `current` ...
            return nuevo_registry

        mutate_trusted_golden_registry(registrar)

    Garantías estructurales (el caller NO puede desacoplarlas):
    - ACQUIRE del global TGR lock (cross-process, share=0) ANTES del LOAD.
    - LOAD del registry actual BAJO lock.
    - ``mutate(current)`` corre BAJO lock y sólo recibe el registro (sin paths:
      la semántica de admisión/refresco es exclusivamente de P3).
    - WRITE por el writer atómico existente (bytes canónicos, temporal en el
      mismo directorio protegido, reemplazo atómico, post-validación) BAJO lock.
    - RELEASE en ``finally`` (también ante excepciones; la excepción original se
      preserva).

    Falla tipada fail-closed: BUSY/TIMEOUT, SECURITY, OS, REENTRANCY,
    UNSUPPORTED. Un mutate que no devuelve ``TrustedGoldenRegistry`` se
    rechaza sin escribir. El registro previo NUNCA se altera ante fallo.

    Síncrono y bloqueante por diseño: en asyncio usar ``asyncio.to_thread``
    (AGENTS.md: jamás bloquear el event loop).
    """
    active_kernel: TrustedRegistryLockKernel
    if kernel is None:
        _ensure_windows()
        active_kernel = _Win32TgrLockKernel()
    else:
        active_kernel = kernel
    registry_path = derive_trusted_registry_path(programdata_resolver=programdata_resolver)
    return _mutate_trusted_registry_core(
        registry_path,
        derive_trusted_registry_lock_path(registry_path, programdata_resolver=programdata_resolver),
        derive_trusted_registry_lock_key(registry_path),
        canonicalize_trusted_registry_identity(registry_path),
        mutate,
        timeout=timeout,
        kernel=active_kernel,
    )


def _mutate_trusted_registry_under_lock_at(
    locks_dir: pathlib.PurePath,
    registry_path: pathlib.Path | str | os.PathLike[str],
    mutate: TrustedRegistryMutator,
    *,
    timeout: float = DEFAULT_TGR_LOCK_TIMEOUT_SECONDS,
    kernel: TrustedRegistryLockKernel | None = None,
) -> TrustedGoldenRegistry:
    """Seam de verificación ``_at``: ciclo RMW completo sobre un registry/locks dir explícitos.

    Comparte el núcleo con la vía productiva (:func:`mutate_trusted_golden_registry`):
    cualquier regresión que mueva el LOAD fuera del lock rompe este seam y sus
    tests causales.
    """
    active_kernel: TrustedRegistryLockKernel
    if kernel is None:
        _ensure_windows()
        active_kernel = _Win32TgrLockKernel()
    else:
        active_kernel = kernel
    return _mutate_trusted_registry_core(
        registry_path,
        _derive_trusted_registry_lock_path_at(locks_dir, registry_path),
        derive_trusted_registry_lock_key(registry_path),
        canonicalize_trusted_registry_identity(registry_path),
        mutate,
        timeout=timeout,
        kernel=active_kernel,
    )


__all__ = [
    "DEFAULT_TGR_LOCK_TIMEOUT_SECONDS",
    "TGR_LOCK_CREATION_DISPOSITION",
    "TGR_LOCK_DESIRED_ACCESS",
    "TGR_LOCK_FILE_SUFFIX",
    "TGR_LOCK_FLAGS",
    "TGR_LOCK_NAME_PREFIX",
    "TGR_LOCK_POLL_INTERVAL_SECONDS",
    "TGR_LOCK_PROTECTS_GOLDEN_CONTENT",
    "TGR_LOCK_PROTECTS_REGISTRY_RMW",
    "TGR_LOCK_SHARE_MODE",
    "TrustedRegistryLockBusyError",
    "TrustedRegistryLockError",
    "TrustedRegistryLockKernel",
    "TrustedRegistryLockOSError",
    "TrustedRegistryLockOwnershipError",
    "TrustedRegistryLockReentrancyError",
    "TrustedRegistryLockSecurityError",
    "TrustedRegistryMutator",
    "TrustedRegistryWriteLockHandle",
    "acquire_trusted_registry_write_lock",
    "canonicalize_trusted_registry_identity",
    "derive_trusted_registry_lock_key",
    "derive_trusted_registry_lock_path",
    "derive_trusted_registry_path",
    "mutate_trusted_golden_registry",
]

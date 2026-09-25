"""Tests GP2-P2: Global TGR serialization lock (exclusión cross-process del RMW).

Cobertura causal:
- TGRLOCK-01/02: identidad global por registry path canónico (sin lock-key ambiguity).
- TGRLOCK-03: contrato Win32 anclado (share=0, OPEN_ALWAYS, reparse-closed).
- TGRLOCK-04..08: ciclo acquire/release, busy acotado, reentrancia tipada,
  exception release, ownership exactamente una vez, reparse fail-closed.
- TGRLOCK-09: mutation anchor — el LOAD ocurre BAJO lock (falla si se reordena).
- TGRLOCK-10: lost-update oracle REAL cross-process (dos writers, cero pérdida).
- TGRLOCK-11: contención real (el segundo writer jamás entra antes del release).
- TGRLOCK-12: crash release real (kernel handle ownership, archivo residual inerte).
- TGRLOCK-13: identidad errónea de lock (grafías equivalentes colisionan; registries
  distintos jamás).

Contrato anclado: TGR_LOCK_PROTECTS_REGISTRY_RMW = YES; TGR_LOCK_PROTECTS_GOLDEN_CONTENT = NO.
El lock NO congela el contenido del Golden: fuera de alcance por diseño (ADR 0010 §11.4).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

import sky_claw.local.runtime_vault.trusted_registry_lock as tgr_lock_mod
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.trusted_registry import (
    TrustedGoldenEntry,
    TrustedGoldenRegistry,
    TrustedRegistryError,
    TrustedRegistryUnsupportedError,
    load_trusted_golden_registry,
    serialize_trusted_golden_registry,
)
from sky_claw.local.runtime_vault.trusted_registry_lock import (
    DEFAULT_TGR_LOCK_TIMEOUT_SECONDS,
    TGR_LOCK_CREATION_DISPOSITION,
    TGR_LOCK_DESIRED_ACCESS,
    TGR_LOCK_FILE_SUFFIX,
    TGR_LOCK_FLAGS,
    TGR_LOCK_NAME_PREFIX,
    TGR_LOCK_PROTECTS_GOLDEN_CONTENT,
    TGR_LOCK_PROTECTS_REGISTRY_RMW,
    TGR_LOCK_SHARE_MODE,
    TrustedRegistryLockBusyError,
    TrustedRegistryLockError,
    TrustedRegistryLockOSError,
    TrustedRegistryLockOwnershipError,
    TrustedRegistryLockReentrancyError,
    TrustedRegistryLockSecurityError,
    TrustedRegistryWriteLockHandle,
    _acquire_trusted_registry_write_lock_at,
    _derive_trusted_registry_lock_path_at,
    _mutate_trusted_registry_under_lock_at,
    acquire_trusted_registry_write_lock,
    canonicalize_trusted_registry_identity,
    derive_trusted_registry_lock_key,
    derive_trusted_registry_lock_path,
    derive_trusted_registry_path,
    mutate_trusted_golden_registry,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _entry(root: str, tag: str) -> TrustedGoldenEntry:
    """Entry con identidad física ÚNICA por root (el TGR exige unicidad dual).

    Derivaciones planas (sin hashlib): los fixtures son fabricados y de baja
    entropía — jamás material criptográfico real.
    """
    root_bytes = root.encode("utf-8")
    digest = (f"{root}|{tag}".encode().hex() * 3)[:64]
    return TrustedGoldenEntry(
        canonical_root=root,
        volume_serial_number=int.from_bytes(root_bytes[-8:].ljust(8, b"\x00"), "big"),
        root_file_id=int.from_bytes(root_bytes[:16].ljust(16, b"\x00"), "big"),
        tree_digest=TreeDigest(digest=digest, files=1, bytes=2),
        policy_version="gp2-v1",
        registered_by="S-1-5-18",
        registered_at="2026-01-01T00:00:00Z",
    )


def _seed_registry(path: pathlib.Path, entries: tuple[TrustedGoldenEntry, ...] = ()) -> None:
    """Siembra un TGR válido con escritura portable (el load productivo es portable)."""
    path.write_bytes(serialize_trusted_golden_registry(TrustedGoldenRegistry(entries=entries, schema_version="1.0")))


def _rmw_order_oracle(events: list[str]) -> None:
    """Oráculo del mutation anchor: la sección crítica RMW tiene orden EXACTO.

    Cualquier regresión que mueva el LOAD fuera de la exclusión (load → acquire
    → write) o elimine el lock del flujo produce una traza distinta y FALLA.
    """
    expected = ["lock.acquire", "load", "modify", "write", "lock.release"]
    if events != expected:
        raise AssertionError(f"SECCIÓN CRÍTICA RMW REGRESADA: {events!r} != {expected!r}")


def _patch_writer_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sustituye el writer Win32 por uno portable para flujos RMW completos en POSIX."""

    def portable_write(reg: TrustedGoldenRegistry, p: Any) -> None:
        # codeql[py/clear-text-storage-sensitive-information] -- FP: writer fake de
        # tests; los bytes son un registry fabricado (SIDs/digests inventados) en el
        # tmp_path de pytest; cero secretos reales. El writer productivo usa
        # WriteFile nativo (trusted_registry.py), sink fuera de esta regla.
        pathlib.Path(p).write_bytes(serialize_trusted_golden_registry(reg))

    monkeypatch.setattr(tgr_lock_mod, "_write_trusted_registry_atomically_at", portable_write)


class _FakeLockKernel:
    """Simula CreateFileW(share=0) estricto: una sola adquisición por archivo a la vez.

    ``files`` modela la existencia persistente del lock file residual; la
    exclusión vive sólo en ``held_paths`` (handles del kernel), como en Win32.
    """

    def __init__(self) -> None:
        self.open_paths: dict[int, str] = {}
        self.closed: list[int] = []
        self.held_paths: set[str] = set()
        self.reparse_tags: dict[str, int] = {}
        self.files: dict[str, bytes] = {}
        self.sleeps: list[float] = []
        self.clock: float = 0.0
        self.open_attempts = 0
        self.fail_close = False
        self._next_handle = 100

    def open_lock_file(self, path: pathlib.PurePath) -> int:
        key = str(path)
        self.open_attempts += 1
        if key in self.held_paths:
            raise TrustedRegistryLockBusyError(f"sharing violation simulada en {key}", win32_error=32)
        handle = self._next_handle
        self._next_handle += 1
        self.open_paths[handle] = key
        self.held_paths.add(key)
        self.files.setdefault(key, b"")  # OPEN_ALWAYS: el residual nunca se borra
        return handle

    def get_reparse_tag(self, handle: int) -> int:
        return self.reparse_tags.get(self.open_paths[handle], 0)

    def close_handle(self, handle: int) -> None:
        if self.fail_close:
            raise TrustedRegistryLockOSError("CloseHandle simulado falló", win32_error=1)
        self.closed.append(handle)
        self.held_paths.discard(self.open_paths.pop(handle))

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

    def simulate_process_death(self) -> None:
        """El kernel reaps los handles del proceso muerto: la exclusión desaparece.

        El archivo residual persiste (``files`` intacto): existencia != LOCK ACTIVE.
        """
        self.open_paths.clear()
        self.held_paths.clear()


# ============================================================================
# TGRLOCK-01/02: Identidad global del lock y distinción con GoldenMutationLock
# ============================================================================


class TestTgrLockIdentidad:
    def test_mismo_registry_misma_identidad_mismo_lock(self, tmp_path: pathlib.Path) -> None:
        reg = tmp_path / "sub" / "trusted_goldens.json"
        (tmp_path / "sub").mkdir()
        key_a = derive_trusted_registry_lock_key(reg)
        key_b = derive_trusted_registry_lock_key(tmp_path / "sub" / "trusted_goldens.json")
        assert key_a == key_b
        assert key_a.startswith(TGR_LOCK_NAME_PREFIX)
        assert key_a.endswith(TGR_LOCK_FILE_SUFFIX) is False  # la clave no incluye sufijo
        path = _derive_trusted_registry_lock_path_at(tmp_path / "locks", reg)
        assert path.name == f"{key_a}{TGR_LOCK_FILE_SUFFIX}"

    def test_grafias_equivalentes_del_mismo_path_colisionan(self, tmp_path: pathlib.Path) -> None:
        """§29: formas equivalentes de la misma ruta no generan locks distintos."""
        sub = tmp_path / "sub"
        sub.mkdir()
        reg = sub / "trusted_goldens.json"
        variants = [
            reg,
            sub / "." / "trusted_goldens.json",
            sub / "inner" / ".." / "trusted_goldens.json",
            pathlib.Path(os.path.join(str(tmp_path), "sub", "..", "sub", "trusted_goldens.json")),
        ]
        keys = {derive_trusted_registry_lock_key(v) for v in variants}
        identities = {canonicalize_trusted_registry_identity(v) for v in variants}
        assert len(keys) == 1, f"lock-key ambiguity: {keys}"
        assert len(identities) == 1, f"identidades divergentes: {identities}"

    def test_registries_distintos_jamas_comparten_lock(self, tmp_path: pathlib.Path) -> None:
        key_a = derive_trusted_registry_lock_key(tmp_path / "a" / "trusted_goldens.json")
        key_b = derive_trusted_registry_lock_key(tmp_path / "b" / "trusted_goldens.json")
        key_c = derive_trusted_registry_lock_key(tmp_path / "a" / "other.json")
        assert len({key_a, key_b, key_c}) == 3

    def test_identidad_invalida_falla_cerrada(self) -> None:
        with pytest.raises(TrustedRegistryLockSecurityError):
            canonicalize_trusted_registry_identity("")
        with pytest.raises(TrustedRegistryLockSecurityError):
            canonicalize_trusted_registry_identity("con\x00nul")
        with pytest.raises(TrustedRegistryLockSecurityError):
            canonicalize_trusted_registry_identity(123)  # type: ignore[arg-type]

    def test_lock_es_global_al_registry_no_por_golden(self, tmp_path: pathlib.Path) -> None:
        """§8: la identidad deriva del REGISTRY path — no de (volume, root_file_id)."""
        reg = tmp_path / "trusted_goldens.json"
        key = derive_trusted_registry_lock_key(reg)
        # El MISMO registry con Goldens distintos conserva UN solo lock:
        assert derive_trusted_registry_lock_key(reg) == key

    def test_distinguible_de_goldenmutationlock(self, tmp_path: pathlib.Path) -> None:
        """§3/§49: los dos locks son primitivas DIFERENTES, jamás confundibles."""
        from sky_claw.local.runtime_vault.golden_mutation_lock import (
            GOLDEN_LOCK_FILE_SUFFIX,
            GOLDEN_LOCK_NAME_PREFIX,
            derive_golden_lock_key,
        )

        assert TGR_LOCK_NAME_PREFIX != GOLDEN_LOCK_NAME_PREFIX
        assert TGR_LOCK_FILE_SUFFIX == GOLDEN_LOCK_FILE_SUFFIX  # mismo sufijo .lock, distinto prefijo
        golden_key = derive_golden_lock_key(0xA1B2C3D4, 0x1122334455667788)
        tgr_key = derive_trusted_registry_lock_key(tmp_path / "trusted_goldens.json")
        assert not tgr_key.startswith(GOLDEN_LOCK_NAME_PREFIX)
        assert not golden_key.startswith(TGR_LOCK_NAME_PREFIX)
        assert tgr_key != golden_key

    def test_path_del_lock_en_namespace_de_locks(self) -> None:
        """§17: el lock productivo vive en runtime_vault\\locks\\ (no en TEMP ni junto al TGR)."""
        registry_str = "C:\\ProgramData\\Sky-Claw\\runtime_vault\\trusted_goldens.json"
        lock_path = derive_trusted_registry_lock_path(
            registry_str,
            programdata_resolver=lambda: pathlib.PureWindowsPath("C:/ProgramData"),
        )
        key = derive_trusted_registry_lock_key(registry_str)
        expected_suffix = "/".join(["Sky-Claw", "runtime_vault", "locks", key + ".lock"])
        normalized = str(lock_path).replace("\\", "/")
        assert normalized.endswith(expected_suffix), normalized


# ============================================================================
# Contrato de alcance + contrato Win32 anclado (§3, §32, §42)
# ============================================================================


class TestTgrLockContratos:
    def test_contrato_de_alcance_inmutable(self) -> None:
        """TGR_LOCK_PROTECTS_REGISTRY_RMW = YES; TGR_LOCK_PROTECTS_GOLDEN_CONTENT = NO."""
        assert TGR_LOCK_PROTECTS_REGISTRY_RMW is True
        assert TGR_LOCK_PROTECTS_GOLDEN_CONTENT is False

    def test_no_es_primitiva_de_estabilidad_de_contenido(self) -> None:
        """§32: documentación anclada — este lock NO congela el Golden ni cierra TOCTOU."""
        src = pathlib.Path(tgr_lock_mod.__file__).read_text(encoding="utf-8")
        assert "no congela el Golden" in src
        assert "TGR_LOCK_PROTECTS_GOLDEN_CONTENT: Final[bool] = False" in src

    def test_contrato_win32_ancclado(self) -> None:
        """Anclas del contrato CreateFileW (mismo patrón que GOLDEN_LOCK_*)."""
        assert TGR_LOCK_SHARE_MODE == 0
        assert TGR_LOCK_CREATION_DISPOSITION == 4  # OPEN_ALWAYS (residual reutilizable)
        assert TGR_LOCK_DESIRED_ACCESS == 0x80000000 | 0x40000000  # GENERIC_READ | GENERIC_WRITE
        assert TGR_LOCK_FLAGS == 0x00000080 | 0x00200000  # NORMAL | OPEN_REPARSE_POINT
        assert TGR_LOCK_NAME_PREFIX == "skyclaw_tgr_lock_"

    def test_el_lock_nunca_se_borra(self) -> None:
        """§15/§16: sin delete jamás => sin delete-race. Ancla estructural sobre el fuente."""
        src = pathlib.Path(tgr_lock_mod.__file__).read_text(encoding="utf-8")
        for forbidden in ("os.remove", "os.unlink", "shutil.move", "DeleteFileW", "pathlib.Path.unlink", ".unlink("):
            assert forbidden not in src, f"El global TGR lock jamás se borra; encontrado '{forbidden}' en el módulo"

    def test_espera_acotada_por_defecto(self) -> None:
        """§13: default finito configurable; jamás espera infinita."""
        assert 0 < DEFAULT_TGR_LOCK_TIMEOUT_SECONDS < float("inf")

    def test_timeout_invalido_rechazado(self) -> None:
        kernel = _FakeLockKernel()
        for bad in (-1.0, float("nan"), float("inf"), True, "30"):
            with pytest.raises(TrustedRegistryLockError):
                _acquire_trusted_registry_write_lock_at(
                    pathlib.PurePath("/locks"),
                    pathlib.Path("/r.json"),
                    timeout=bad,
                    kernel=kernel,  # type: ignore[arg-type]
                )


# ============================================================================
# TGRLOCK-04..08: ciclo de vida con kernel fake (causal, POSIX-ejecutable)
# ============================================================================


class TestTgrLockCicloDeVida:
    def _acquire(self, kernel: _FakeLockKernel, tmp_path: pathlib.Path, name: str = "r.json", **kw: Any):
        return _acquire_trusted_registry_write_lock_at(
            tmp_path / "locks", tmp_path / name, timeout=kw.pop("timeout", 0.0), kernel=kernel, **kw
        )

    def test_acquire_release_reacquire(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-04 / §26: release normal => re-adquisición sin intervención manual."""
        kernel = _FakeLockKernel()
        lock = self._acquire(kernel, tmp_path)
        assert isinstance(lock, TrustedRegistryWriteLockHandle)
        assert lock.closed is False
        assert lock.release() is True
        assert lock.closed is True
        assert str(_derive_trusted_registry_lock_path_at(tmp_path / "locks", tmp_path / "r.json")) in kernel.files
        lock2 = self._acquire(kernel, tmp_path)
        lock2.release()

    def test_context_manager_libera_al_salir(self, tmp_path: pathlib.Path) -> None:
        kernel = _FakeLockKernel()
        with self._acquire(kernel, tmp_path) as lock:
            assert lock.closed is False
        assert lock.closed is True
        self._acquire(kernel, tmp_path).release()

    def test_busy_timeout_acotado_determinista_sin_busy_spin(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-05 / §13: BUSY/TIMEOUT tipado, espera acotada con polls dormidos."""
        kernel = _FakeLockKernel()
        lock_path = _derive_trusted_registry_lock_path_at(tmp_path / "locks", tmp_path / "r.json")
        # Dueño FORÁNEO (otro proceso/thread) simulado en el kernel:
        kernel.held_paths.add(str(lock_path))
        # timeout = 0 => un solo intento, sin sleeps (fail-fast)
        with pytest.raises(TrustedRegistryLockBusyError):
            self._acquire(kernel, tmp_path, timeout=0.0)
        assert kernel.open_attempts == 1
        assert kernel.sleeps == []
        # timeout acotado => reintentos DORMIDOS hasta el deadline, jamás busy spin
        with pytest.raises(TrustedRegistryLockBusyError) as exc_info:
            self._acquire(kernel, tmp_path, timeout=0.5)
        assert kernel.open_attempts > 1
        assert len(kernel.sleeps) >= 2, "sin sleeps sería busy spin"
        assert sum(kernel.sleeps) <= 0.5 + 1e-9, "la espera debe ser acotada por el timeout"
        assert exc_info.value.win32_error == 32
        # Liberado el dueño foráneo, la adquisición procede:
        kernel.held_paths.clear()
        self._acquire(kernel, tmp_path, timeout=0.0).release()

    def test_owner_release_permite_la_siguiente_adquisicion(self, tmp_path: pathlib.Path) -> None:
        kernel = _FakeLockKernel()
        owner = self._acquire(kernel, tmp_path)
        acquired = threading.Event()

        def contender() -> None:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    lock = self._acquire(kernel, tmp_path, timeout=0.0)
                except TrustedRegistryLockBusyError:
                    time.sleep(0.005)
                    continue
                lock.release()
                acquired.set()
                return

        t = threading.Thread(target=contender, daemon=True)
        t.start()
        time.sleep(0.05)
        owner.release()
        t.join(timeout=5.0)
        assert acquired.is_set(), "el contender debía adquirir tras el release del owner"

    def test_reentrancia_mismo_hilo_rechazo_tipado_inmediato(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-06 / §21: reentrancia = rechazo tipado SIN esperar (no hay retry)."""
        kernel = _FakeLockKernel()
        with self._acquire(kernel, tmp_path):
            attempts = kernel.open_attempts
            with pytest.raises(TrustedRegistryLockReentrancyError):
                self._acquire(kernel, tmp_path, timeout=100.0)
            assert kernel.open_attempts == attempts, "el rechazo debe ser ANTES de tocar el kernel"
            assert kernel.sleeps == []

    def test_otro_hilo_mismo_lock_es_busy_del_kernel(self, tmp_path: pathlib.Path) -> None:
        """La exclusión cross-thread la da el KERNEL (share=0), no el guard de reentrancia."""
        kernel = _FakeLockKernel()
        with self._acquire(kernel, tmp_path), pytest.raises(TrustedRegistryLockBusyError):
            self._acquire_from_thread(kernel, tmp_path)

    def _acquire_from_thread(self, kernel: _FakeLockKernel, tmp_path: pathlib.Path) -> None:
        errors: list[BaseException] = []

        def attempt() -> None:
            try:
                self._acquire(kernel, tmp_path, timeout=0.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=attempt)
        t.start()
        t.join()
        assert len(errors) == 1
        raise errors[0]

    def test_exception_release_original_preservada_y_reacquire(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-07 / §20/§27: excepción => release en finally; original preservada."""
        kernel = _FakeLockKernel()

        class BoomError(RuntimeError):
            pass

        with pytest.raises(BoomError, match="origen"), self._acquire(kernel, tmp_path):
            raise BoomError("origen")
        assert kernel.held_paths == set()
        self._acquire(kernel, tmp_path).release()

    def test_release_exactamente_una_vez_y_use_after_release(self, tmp_path: pathlib.Path) -> None:
        """§19: LOCK OWNERSHIP == HANDLE LIFETIME; cierre exactamente una vez."""
        kernel = _FakeLockKernel()
        lock = self._acquire(kernel, tmp_path)
        assert lock.release() is True
        assert lock.release() is False
        assert kernel.closed == [lock._handle]  # noqa: SLF001 - ancla de exactamente-un-cierre
        with pytest.raises(TrustedRegistryLockOwnershipError):
            _ = lock.raw_handle

    def test_reparse_point_rechazado_y_handle_cerrado(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-08 / §18: sustitución por reparse => fail-closed tipado, sin leak."""
        kernel = _FakeLockKernel()
        lock_path = _derive_trusted_registry_lock_path_at(tmp_path / "locks", tmp_path / "r.json")
        kernel.reparse_tags[str(lock_path)] = 0x40000003
        with pytest.raises(TrustedRegistryLockSecurityError):
            self._acquire(kernel, tmp_path)
        assert kernel.held_paths == set()
        assert len(kernel.closed) == 1

    def test_fallo_de_cierre_no_afirma_liberacion(self, tmp_path: pathlib.Path) -> None:
        """CloseHandle fallido => jamás se afirma liberación (fail-closed): guard retenido."""
        kernel = _FakeLockKernel()
        lock = self._acquire(kernel, tmp_path)
        kernel.fail_close = True
        with pytest.raises(TrustedRegistryLockOSError):
            lock.release()
        assert lock.closed is True
        # La liberación NO se demostró: el mismo hilo no puede re-entrar (guard retenido)
        # y el dueño foráneo que simula el handle sin cerrar tampoco lo cede:
        with pytest.raises(TrustedRegistryLockReentrancyError):
            self._acquire(kernel, tmp_path, timeout=0.0)
        kernel.fail_close = False

    def test_crash_del_dueño_libera_exclusion_el_residual_persiste(self, tmp_path: pathlib.Path) -> None:
        """§14 (modelo): existencia del *.lock residual != LOCK ACTIVE; el kernel reaps al dueño."""
        kernel = _FakeLockKernel()
        results: list[TrustedRegistryWriteLockHandle] = []
        started = threading.Event()

        def owner_thread() -> None:
            results.append(self._acquire(kernel, tmp_path, timeout=1.0))
            started.set()

        t = threading.Thread(target=owner_thread)
        t.start()
        t.join()
        started.wait()
        lock_path = _derive_trusted_registry_lock_path_at(tmp_path / "locks", tmp_path / "r.json")
        assert str(lock_path) in kernel.files  # el residual EXISTE
        assert str(lock_path) in kernel.held_paths  # ... y está adquirido

        kernel.simulate_process_death()  # muerte del dueño: kernel reaps el handle
        assert str(lock_path) in kernel.files  # el residual SIGUE existiendo
        self._acquire(kernel, tmp_path, timeout=0.0).release()  # y el lock es adquirible


# ============================================================================
# Transacción RMW + MUTATION ANCHOR (§9, §23, §39, §40)
# ============================================================================


class TestTgrLockTransaccionRmw:
    def _patched_flow(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kernel: _FakeLockKernel,
        events: list[str],
        lock_path: pathlib.PurePath,
    ) -> None:
        real_load = tgr_lock_mod.load_trusted_golden_registry

        def checked_load(p: Any) -> TrustedGoldenRegistry:
            if str(lock_path) not in kernel.held_paths:
                raise AssertionError("MUTATION ANCHOR: load ejecutado SIN el global TGR lock")
            events.append("load")
            return real_load(p)

        def recording_write(reg: TrustedGoldenRegistry, p: Any) -> None:
            events.append("write")
            pathlib.Path(p).write_bytes(serialize_trusted_golden_registry(reg))

        original_open = kernel.open_lock_file
        original_close = kernel.close_handle

        def open_rec(p: pathlib.PurePath) -> int:
            events.append("lock.acquire")
            return original_open(p)

        def close_rec(h: int) -> None:
            events.append("lock.release")
            return original_close(h)

        monkeypatch.setattr(kernel, "open_lock_file", open_rec)
        monkeypatch.setattr(kernel, "close_handle", close_rec)
        monkeypatch.setattr(tgr_lock_mod, "load_trusted_golden_registry", checked_load)
        monkeypatch.setattr(tgr_lock_mod, "_write_trusted_registry_atomically_at", recording_write)

    def test_mutation_anchor_load_modify_write_bajo_lock(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TGRLOCK-09 / §23: prueba causal que FALLA si se mueve el load fuera de la exclusión."""
        kernel = _FakeLockKernel()
        locks = tmp_path / "locks"
        locks.mkdir()
        reg_path = tmp_path / "trusted_goldens.json"
        _seed_registry(reg_path)
        lock_path = _derive_trusted_registry_lock_path_at(locks, reg_path)
        events: list[str] = []
        self._patched_flow(monkeypatch, kernel, events, lock_path)

        def mutate(current: TrustedGoldenRegistry) -> TrustedGoldenRegistry:
            events.append("modify")
            return TrustedGoldenRegistry(
                entries=tuple(current.entries) + (_entry("C:\\GoldenA", "a"),), schema_version="1.0"
            )

        _mutate_trusted_registry_under_lock_at(locks, reg_path, mutate, timeout=0.0, kernel=kernel)

        _rmw_order_oracle(events)  # ACQUIRE→LOAD→MODIFY→WRITE→RELEASE
        assert events == ["lock.acquire", "load", "modify", "write", "lock.release"]
        final = load_trusted_golden_registry(reg_path)
        assert [e.canonical_root for e in final.entries] == ["C:\\GoldenA"]

    def test_mutation_anchor_detecta_la_regresion_load_fuera_del_lock(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control negativo: el oráculo tiene dientes contra 'load → acquire → write'."""
        kernel = _FakeLockKernel()
        locks = tmp_path / "locks"
        locks.mkdir()
        reg_path = tmp_path / "trusted_goldens.json"
        _seed_registry(reg_path)
        lock_path = _derive_trusted_registry_lock_path_at(locks, reg_path)
        events: list[str] = []
        self._patched_flow(monkeypatch, kernel, events, lock_path)

        # Simula el flujo ROTO: load ANTES de acquire (la regresión de §23).
        broken_trace = ["load", "lock.acquire", "modify", "write", "lock.release"]
        with pytest.raises(AssertionError, match="REGRESADA"):
            _rmw_order_oracle(broken_trace)

        # Y el checked-load del anchor rechaza causalmente un load sin lock sostenido:
        with pytest.raises(AssertionError, match="SIN el global TGR lock"):
            tgr_lock_mod.load_trusted_golden_registry(reg_path)

    def test_mutate_con_salida_invalida_falla_cerrada_sin_escribir(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeLockKernel()
        locks = tmp_path / "locks"
        locks.mkdir()
        reg_path = tmp_path / "trusted_goldens.json"
        _seed_registry(reg_path)
        lock_path = _derive_trusted_registry_lock_path_at(locks, reg_path)
        events: list[str] = []
        self._patched_flow(monkeypatch, kernel, events, lock_path)

        with pytest.raises(TrustedRegistryError, match="mutate debe devolver"):
            _mutate_trusted_registry_under_lock_at(
                locks, reg_path, lambda _c: {"no": "registro"}, timeout=0.0, kernel=kernel
            )  # type: ignore[arg-type, return-value]
        assert "write" not in events
        assert kernel.held_paths == set()

    def test_mutate_con_excepcion_no_escribe_y_libera(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§20/§27 sobre la transacción completa: load+modify bajo lock, cero writes."""
        kernel = _FakeLockKernel()
        locks = tmp_path / "locks"
        locks.mkdir()
        reg_path = tmp_path / "trusted_goldens.json"
        _seed_registry(reg_path, entries=(_entry("C:\\GoldenZ", "z"),))
        lock_path = _derive_trusted_registry_lock_path_at(locks, reg_path)
        events: list[str] = []
        self._patched_flow(monkeypatch, kernel, events, lock_path)

        class BoomError(RuntimeError):
            pass

        with pytest.raises(BoomError, match="origen"):
            _mutate_trusted_registry_under_lock_at(
                locks, reg_path, lambda _c: (_ for _ in ()).throw(BoomError("origen")), timeout=0.0, kernel=kernel
            )
        assert events == ["lock.acquire", "load", "lock.release"]
        assert [e.canonical_root for e in load_trusted_golden_registry(reg_path).entries] == ["C:\\GoldenZ"]
        # Re-adquisición posterior posible sin limpieza manual:
        events.clear()

        def pass_through(current: TrustedGoldenRegistry) -> TrustedGoldenRegistry:
            events.append("modify")
            return current

        out = _mutate_trusted_registry_under_lock_at(locks, reg_path, pass_through, timeout=0.0, kernel=kernel)
        assert [e.canonical_root for e in out.entries] == ["C:\\GoldenZ"]
        _rmw_order_oracle(events)

    def test_reentrancia_en_transaccion_rechazada(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kernel = _FakeLockKernel()
        locks = tmp_path / "locks"
        locks.mkdir()
        reg_path = tmp_path / "trusted_goldens.json"
        _seed_registry(reg_path)
        _patch_writer_portable(monkeypatch)

        def nested(current: TrustedGoldenRegistry) -> TrustedGoldenRegistry:
            with pytest.raises(TrustedRegistryLockReentrancyError):
                _mutate_trusted_registry_under_lock_at(locks, reg_path, lambda c: c, timeout=0.0, kernel=kernel)
            return current

        _mutate_trusted_registry_under_lock_at(locks, reg_path, nested, timeout=0.0, kernel=kernel)

    def test_lost_update_oracle_modelo_con_falso_kernel(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Oráculo de lost-update a nivel de modelo (complementa el real cross-process)."""
        kernel = _FakeLockKernel()
        locks = tmp_path / "locks"
        locks.mkdir()
        reg_path = tmp_path / "trusted_goldens.json"
        _seed_registry(reg_path)
        _patch_writer_portable(monkeypatch)

        def add(current: TrustedGoldenRegistry, root: str, ch: str) -> TrustedGoldenRegistry:
            return TrustedGoldenRegistry(entries=tuple(current.entries) + (_entry(root, ch),), schema_version="1.0")

        out_a = _mutate_trusted_registry_under_lock_at(
            locks, reg_path, lambda c: add(c, "C:\\GoldenA", "a"), timeout=0.0, kernel=kernel
        )
        assert len(out_a.entries) == 1
        out_b = _mutate_trusted_registry_under_lock_at(
            locks, reg_path, lambda c: add(c, "C:\\GoldenB", "b"), timeout=0.0, kernel=kernel
        )
        assert {e.canonical_root for e in out_b.entries} == {"C:\\GoldenA", "C:\\GoldenB"}
        assert {e.canonical_root for e in load_trusted_golden_registry(reg_path).entries} == {
            "C:\\GoldenA",
            "C:\\GoldenB",
        }


# ============================================================================
# API productiva (sin paths) + POSIX fail-closed (§43)
# ============================================================================


class TestTgrLockApiProductiva:
    def _resolver(self, tmp_path: pathlib.Path):
        return lambda: pathlib.Path(tmp_path)

    def test_api_productiva_deriva_registry_y_lock_del_mismo_lugar(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M-L3: el caller NO pasa paths; registry normativo y lock se derivan internamente."""
        kernel = _FakeLockKernel()
        seen: dict[str, Any] = {}

        def fake_load(p: Any) -> TrustedGoldenRegistry:
            seen["registry_path"] = pathlib.Path(p)
            return TrustedGoldenRegistry(entries=(), schema_version="1.0")

        def fake_write(reg: TrustedGoldenRegistry, p: Any) -> None:
            seen["written"] = reg

        monkeypatch.setattr(tgr_lock_mod, "load_trusted_golden_registry", fake_load)
        monkeypatch.setattr(tgr_lock_mod, "_write_trusted_registry_atomically_at", fake_write)

        expected_registry = derive_trusted_registry_path(programdata_resolver=self._resolver(tmp_path))
        expected_lock = derive_trusted_registry_lock_path(
            expected_registry, programdata_resolver=self._resolver(tmp_path)
        )
        assert expected_registry.name == "trusted_goldens.json"
        assert expected_lock.parent.name == "locks"

        out = mutate_trusted_golden_registry(
            lambda _c: TrustedGoldenRegistry(entries=(), schema_version="1.0"),
            timeout=0.0,
            kernel=kernel,
            programdata_resolver=self._resolver(tmp_path),
        )
        assert seen["registry_path"] == expected_registry
        assert out == seen["written"]
        # El handle productivo apunta al lock derivado del MISMO registry:
        lock = acquire_trusted_registry_write_lock(
            timeout=0.0, kernel=kernel, programdata_resolver=self._resolver(tmp_path)
        )
        assert lock.lock_path == expected_lock
        lock.release()

    def test_posix_falla_cerrado_sin_tocar_disco(self, tmp_path: pathlib.Path) -> None:
        """§43: sin semántica POSIX falsa — UnsupportedError fail-closed."""
        with (
            patch("sys.platform", "linux"),
            pytest.raises(TrustedRegistryUnsupportedError, match="solo está soportado en Windows"),
        ):
            acquire_trusted_registry_write_lock()
        with (
            patch("sys.platform", "linux"),
            pytest.raises(TrustedRegistryUnsupportedError, match="solo está soportado en Windows"),
        ):
            mutate_trusted_golden_registry(lambda c: c)
        assert len(list(tmp_path.iterdir())) == 0


# ============================================================================
# TGRLOCK-10..13: Windows REAL (procesos reales, CreateFileW real)
# ============================================================================

# Bootstrap de procesos hijos: instala stubs de paquete con __path__ para
# importar SÓLO runtime_vault (models/trusted_registry/trusted_registry_lock) sin
# pagar la importación completa de la capa app (aiohttp/nicegui/...). El proceso
# hijo de los oráculos sólo necesita la primitiva: arranque ~2 s y cero
# fragilidad de imports ajenos al dominio.
_CHILD_BOOTSTRAP = """\
import pathlib, sys, time, types
REPO = pathlib.Path("__REPO__")
for _name in ("sky_claw", "sky_claw.local", "sky_claw.local.runtime_vault"):
    _stub = types.ModuleType(_name)
    _stub.__path__ = [str(REPO.joinpath(*_name.split(".")))]
    sys.modules[_name] = _stub
from sky_claw.local.runtime_vault.models import TreeDigest
from sky_claw.local.runtime_vault.trusted_registry import TrustedGoldenEntry, TrustedGoldenRegistry
from sky_claw.local.runtime_vault.trusted_registry_lock import (
    _acquire_trusted_registry_write_lock_at,
    _mutate_trusted_registry_under_lock_at,
)
MARKERS = pathlib.Path("__MARKERS__")
REG = pathlib.Path("__REG__")
LOCKS = pathlib.Path("__LOCKS__")
"""

# Entry helper compartido por los hijos (identidad física única por root, sin
# hashlib — fixtures fabricados de baja entropía, nunca material criptográfico).
_CHILD_ENTRY_HELPER = """\
def entry(root, tag):
    root_bytes = root.encode("utf-8")
    digest = (f"{root}|{tag}".encode().hex() * 3)[:64]
    return TrustedGoldenEntry(
        canonical_root=root,
        volume_serial_number=int.from_bytes(root_bytes[-8:].ljust(8, b"\\x00"), "big"),
        root_file_id=int.from_bytes(root_bytes[:16].ljust(16, b"\\x00"), "big"),
        tree_digest=TreeDigest(digest=digest, files=1, bytes=2),
        policy_version="gp2-v1",
        registered_by="S-1-5-18",
        registered_at="2026-01-01T00:00:00Z",
    )
"""

_CHILD_WRITER_A_BODY = """\
def mutate_a(current):
    (MARKERS / "a_in_cs").write_text("1")
    deadline = time.monotonic() + 60
    while not (MARKERS / "b_attempted").exists():
        if time.monotonic() > deadline:
            raise SystemExit(3)
        time.sleep(0.01)
    time.sleep(1.0)
    return TrustedGoldenRegistry(
        entries=tuple(current.entries) + (entry("C:\\\\GoldenA", "a"),),
        schema_version="1.0",
    )

_mutate_trusted_registry_under_lock_at(LOCKS, REG, mutate_a, timeout=60)
"""

_CHILD_WRITER_B_BODY = """\
def mutate_b(current):
    roots = {e.canonical_root.upper() for e in current.entries}
    if "C:\\\\GOLDENA" not in roots:
        raise SystemExit(42)
    (MARKERS / "b_saw_a").write_text("1")
    return TrustedGoldenRegistry(
        entries=tuple(current.entries) + (entry("C:\\\\GoldenB", "b"),),
        schema_version="1.0",
    )

deadline = time.monotonic() + 60
while not (MARKERS / "a_in_cs").exists():
    if time.monotonic() > deadline:
        raise SystemExit(4)
    time.sleep(0.01)
(MARKERS / "b_attempted").write_text("1")
_mutate_trusted_registry_under_lock_at(LOCKS, REG, mutate_b, timeout=60)
"""

_CHILD_HOLDER_BODY = """\
def mutate_hold(current):
    (MARKERS / "a_holding").write_text("1")
    deadline = time.monotonic() + 60
    while not (MARKERS / "a_release").exists():
        if time.monotonic() > deadline:
            raise SystemExit(5)
        time.sleep(0.01)
    return current

_mutate_trusted_registry_under_lock_at(LOCKS, REG, mutate_hold, timeout=60)
"""

_CHILD_CRASHHOLDER_BODY = """\
lock = _acquire_trusted_registry_write_lock_at(LOCKS, REG, timeout=10)
(MARKERS / "child_acquired").write_text("1")
time.sleep(300)
"""


def _render_child_script(
    body: str, repo: pathlib.Path, markers: pathlib.Path, reg: pathlib.Path, locks: pathlib.Path
) -> str:
    """Renderiza la fuente de un script hijo (placeholders literales, sin brace-escaping)."""
    return (
        _CHILD_BOOTSTRAP.replace("__REPO__", str(repo))
        .replace("__MARKERS__", str(markers))
        .replace("__REG__", str(reg))
        .replace("__LOCKS__", str(locks))
        + _CHILD_ENTRY_HELPER
        + body
    )


def _all_child_scripts(
    repo: pathlib.Path, markers: pathlib.Path, reg: pathlib.Path, locks: pathlib.Path
) -> dict[str, str]:
    base = dict(repo=repo, markers=markers, reg=reg, locks=locks)
    return {
        "writer_a.py": _render_child_script(_CHILD_WRITER_A_BODY, **base),
        "writer_b.py": _render_child_script(_CHILD_WRITER_B_BODY, **base),
        "holder.py": _render_child_script(_CHILD_HOLDER_BODY, **base),
        "crashholder.py": _render_child_script(_CHILD_CRASHHOLDER_BODY, **base),
    }


class TestChildScriptSources:
    """Anclas portables de los scripts hijos (corrían ANTES sólo en Windows)."""

    def test_children_compilan(self) -> None:
        """Cada script hijo renderiza a sintaxis Python válida (caza regresiones de quoting)."""
        scripts = _all_child_scripts(
            pathlib.Path("X:/repo"),
            pathlib.Path("X:/tmp/markers"),
            pathlib.Path("X:/tmp/trusted_goldens.json"),
            pathlib.Path("X:/tmp/locks"),
        )
        assert set(scripts) == {"writer_a.py", "writer_b.py", "holder.py", "crashholder.py"}
        for name, src in scripts.items():
            compile(src, name, "exec")
            assert "__REPO__" not in src and "__MARKERS__" not in src

    def test_child_bootstrap_importa_solo_runtime_vault(self) -> None:
        """El stub de paquete del hijo importa runtime_vault sin la capa app (mecanismo real)."""
        child_src = (
            _CHILD_BOOTSTRAP.replace("__REPO__", str(_REPO_ROOT))
            .replace("__MARKERS__", str(_REPO_ROOT))
            .replace("__REG__", str(_REPO_ROOT))
            .replace("__LOCKS__", str(_REPO_ROOT))
            + 'print("BOOTSTRAP_OK", TrustedGoldenRegistry(entries=(), schema_version="1.0").schema_version)\n'
        )
        env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
        res = subprocess.run(
            [sys.executable, "-c", child_src],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(_REPO_ROOT),
        )
        assert res.returncode == 0, res.stderr
        assert "BOOTSTRAP_OK 1.0" in res.stdout


@pytest.mark.timeout(300)
@pytest.mark.skipif(sys.platform != "win32", reason="Primitiva Win32 nativa (CreateFileW share=0)")
class TestTgrLockWindowsReal:
    def _prepare(self, tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        from sky_claw.local.runtime_vault.trusted_registry import _write_trusted_registry_atomically_at

        locks = tmp_path / "locks"
        locks.mkdir()
        markers = tmp_path / "markers"
        markers.mkdir()
        reg_path = tmp_path / "trusted_goldens.json"
        _write_trusted_registry_atomically_at(TrustedGoldenRegistry(entries=(), schema_version="1.0"), reg_path)
        return locks, markers, reg_path

    def _start_child(
        self, tmp_path: pathlib.Path, name: str, markers: pathlib.Path, reg: pathlib.Path, locks: pathlib.Path
    ) -> subprocess.Popen[str]:
        src = _all_child_scripts(_REPO_ROOT, markers, reg, locks)[name]
        script_path = tmp_path / name
        script_path.write_text(src, encoding="utf-8")
        return subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            cwd=str(_REPO_ROOT),
        )

    def _wait_marker(self, markers: pathlib.Path, name: str, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while not (markers / name).exists():
            if time.monotonic() > deadline:
                pytest.fail(f"marker '{name}' no llegó en {timeout}s")
            time.sleep(0.01)

    def test_tgrlock_10_lost_update_oracle_real_cross_process(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-10 / §22/§24: DOS PROCESOS reales sobre el MISMO registry → NO LOST UPDATE.

        Secuencia causal: A adquiere, load R, marca a_in_cs y espera a que B
        INTENTE (b_attempted) mientras aún retiene el lock; B queda bloqueado en
        acquire; A escribe R+A y libera; B adquiere, load R+A (exige ver a A —
        si no, exit 42 = lost update), agrega B, escribe R+A+B. Final contiene A y B.
        """
        locks, markers, reg_path = self._prepare(tmp_path)
        proc_a = self._start_child(tmp_path, "writer_a.py", markers, reg_path, locks)
        self._wait_marker(markers, "a_in_cs")
        proc_b = self._start_child(tmp_path, "writer_b.py", markers, reg_path, locks)
        out_a, err_a = proc_a.communicate(timeout=240)
        out_b, err_b = proc_b.communicate(timeout=240)
        assert proc_a.returncode == 0, f"A falló: {err_a}"
        assert proc_b.returncode == 0, f"B falló (42 = LOST UPDATE): rc={proc_b.returncode}\n{err_b}"
        assert (markers / "b_saw_a").exists()

        final = load_trusted_golden_registry(reg_path)
        assert {e.canonical_root.upper() for e in final.entries} == {"C:\\GOLDENA", "C:\\GOLDENB"}

    def test_tgrlock_11_contencion_real_segundo_writer_no_entra(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-11 / §25: con A reteniendo el lock, B NO entra a la sección crítica."""
        locks, markers, reg_path = self._prepare(tmp_path)
        proc = self._start_child(tmp_path, "holder.py", markers, reg_path, locks)
        try:
            self._wait_marker(markers, "a_holding")
            # B (este proceso) intenta adquirir mientras A sostiene: BUSY acotado.
            with pytest.raises(TrustedRegistryLockBusyError):
                _acquire_trusted_registry_write_lock_at(locks, reg_path, timeout=0.3)
        finally:
            (markers / "a_release").write_text("1")
            _, err = proc.communicate(timeout=240)
        assert proc.returncode == 0, err

        # Tras el release de A, este proceso adquiere sin limpieza manual (§26):
        lock = _acquire_trusted_registry_write_lock_at(locks, reg_path, timeout=10.0)
        lock.release()

    def test_tgrlock_12_crash_release_real(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-12 / §28: el dueño muere => el kernel libera; el residual NO bloquea."""
        locks, markers, reg_path = self._prepare(tmp_path)
        proc = self._start_child(tmp_path, "crashholder.py", markers, reg_path, locks)
        self._wait_marker(markers, "child_acquired")
        proc.kill()
        proc.wait(timeout=30)

        lock_path = _derive_trusted_registry_lock_path_at(locks, reg_path)
        assert pathlib.Path(lock_path).exists(), "el residual debe persistir (jamás se borra)"
        lock = _acquire_trusted_registry_write_lock_at(locks, reg_path, timeout=10.0)
        lock.release()

    def test_tgrlock_12b_residual_tras_release_normal_es_reutilizable(self, tmp_path: pathlib.Path) -> None:
        """§15/§16: release normal deja residual inerte; la re-adquisición no exige borrado."""
        locks, _markers, reg_path = self._prepare(tmp_path)
        lock = _acquire_trusted_registry_write_lock_at(locks, reg_path, timeout=5.0)
        lock.release()
        lock_path = pathlib.Path(_derive_trusted_registry_lock_path_at(locks, reg_path))
        assert lock_path.exists()
        assert lock_path.stat().st_size == 0, "sin metadata persistente: el residual queda VACÍO"
        lock2 = _acquire_trusted_registry_write_lock_at(locks, reg_path, timeout=5.0)
        lock2.release()

    def test_tgrlock_13_identidad_errónea_de_lock(self, tmp_path: pathlib.Path) -> None:
        """TGRLOCK-13 / §8/§29: grafías equivalentes del MISMO registry colisionan en el lock."""
        locks, _markers, _reg = self._prepare(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        reg = sub / "trusted_goldens.json"
        lock = _acquire_trusted_registry_write_lock_at(locks, reg, timeout=5.0)
        alias = sub / "inner" / ".." / "trusted_goldens.json"
        with pytest.raises(TrustedRegistryLockBusyError):
            _acquire_trusted_registry_write_lock_at(locks, alias, timeout=0.0)
        # Un registry DISTINTO no comparte lock (sin lock-key ambiguity):
        other = _acquire_trusted_registry_write_lock_at(locks, sub / "other.json", timeout=0.0)
        other.release()
        lock.release()

    def test_tgrlock_reentrancia_real_kernel(self, tmp_path: pathlib.Path) -> None:
        locks, _markers, reg_path = self._prepare(tmp_path)
        with _acquire_trusted_registry_write_lock_at(locks, reg_path, timeout=5.0):
            with pytest.raises(TrustedRegistryLockReentrancyError):
                _acquire_trusted_registry_write_lock_at(locks, reg_path, timeout=5.0)
            with pytest.raises(TrustedRegistryLockReentrancyError):
                _mutate_trusted_registry_under_lock_at(locks, reg_path, lambda c: c, timeout=5.0)

    def test_tgrlock_exception_release_real(self, tmp_path: pathlib.Path) -> None:
        locks, _markers, reg_path = self._prepare(tmp_path)

        class BoomError(RuntimeError):
            """Excepción de dominio de prueba (N818)."""

            def __init__(self, mensaje: str) -> None:
                super().__init__(mensaje)
                self.mensaje = mensaje

            def __str__(self) -> str:
                return self.mensaje

        def boom(_current: TrustedGoldenRegistry) -> TrustedGoldenRegistry:
            raise BoomError("origen")

        with pytest.raises(BoomError, match="origen"):
            _mutate_trusted_registry_under_lock_at(locks, reg_path, boom, timeout=5.0)
        lock = _acquire_trusted_registry_write_lock_at(locks, reg_path, timeout=5.0)
        lock.release()

    def test_tgrlock_rmw_real_secuencia_completa(self, tmp_path: pathlib.Path) -> None:
        """Sección crítica real completa: acquire→load→modify→atomic write→post-validate→release."""
        locks, _markers, reg_path = self._prepare(tmp_path)

        def add_a(current: TrustedGoldenRegistry) -> TrustedGoldenRegistry:
            return TrustedGoldenRegistry(
                entries=tuple(current.entries) + (_entry("C:\\GoldenA", "a"),), schema_version="1.0"
            )

        out = _mutate_trusted_registry_under_lock_at(locks, reg_path, add_a, timeout=5.0)
        assert {e.canonical_root for e in out.entries} == {"C:\\GoldenA"}
        assert {e.canonical_root for e in load_trusted_golden_registry(reg_path).entries} == {"C:\\GoldenA"}

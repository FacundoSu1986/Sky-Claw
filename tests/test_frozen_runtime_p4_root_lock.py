"""Tests de contrato y adversariales para P4-S1: Frozen Runtime root-keyed cross-process lock.

ADR 0012 §34.3 es la autoridad normativa:
- LOCK_RESOURCE = FrozenRuntimeRoot (recurso único, no por destino).
- LOCK_IDENTITY = canonical root (normcase abspath) + session_id (UUID por adquisición).
- LOCK_SCOPE = exclusión de mutaciones sobre autoridad del runtime.
- LOCK_EXCLUSION_SET = { PROMOTION, ROLLBACK, activation/rebinding,
  startup reconciliation, publish Generation, candidate build que escriba state/ }.
- LOCK_BUSY = fail-closed con timeout acotado; nunca espera infinita.
- LOCK_OWNER = { session_id, pid, process_create_time, acquired_at }.
- LOCK_LIVENESS = pid + process_create_time (nunca sólo PID ni os.kill(pid, 0)).
- LOCK_CRASH = el archivo de lock NO se borra al morir el proceso; persiste como
  evidencia forense de ownership para recuperación/clasificación.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

import psutil
import pytest

from sky_claw.local.frozen_runtime import root_lock as rl
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeLockAdmissionError,
    FrozenRuntimeLockBusyError,
    FrozenRuntimeLockMetadataError,
    FrozenRuntimeLockOrphanedError,
    FrozenRuntimeLockOwnershipError,
)
from sky_claw.local.frozen_runtime.root_lock import (
    LOCK_FILE_NAME,
    LOCK_METADATA_FIELDS,
    LOCK_SCHEMA_VERSION,
    FrozenRuntimeRootLockHandle,
    LockDisposition,
    LockPhase,
    OwnerLiveness,
    _get_thread_lock,
    _read_metadata_bytes,
    acquire_frozen_runtime_root_lock,
    canonical_root_key,
    inspect_frozen_runtime_root_lock,
)

# ============================================================================
# Helpers y fixtures
# ============================================================================


def _crear_root_valido(base: pathlib.Path, nombre: str = "frozen_root") -> pathlib.Path:
    """Crea una estructura básica válida de FrozenRuntimeRoot."""
    root = base / nombre
    (root / "state").mkdir(parents=True, exist_ok=True)
    return root


# ============================================================================
# RL-01 — Mismo root, exclusión intraproceso (threads)
# ============================================================================


def test_rl01_mismo_root_exclusion_intraproceso(tmp_path: pathlib.Path) -> None:
    """RL-01: Dos hilos en el mismo proceso compitiendo por el mismo root."""
    root = _crear_root_valido(tmp_path)
    adquirido_primero = threading.Event()
    segundo_intento_completo = threading.Event()
    error_segundo: list[Exception] = []

    def _hilo_segundo() -> None:
        adquirido_primero.wait(timeout=5.0)
        try:
            # Intento no bloqueante debe fallar inmediatamente con FrozenRuntimeLockBusyError
            acquire_frozen_runtime_root_lock(root, timeout=0.0)
        except Exception as exc:
            error_segundo.append(exc)
        finally:
            segundo_intento_completo.set()

    with acquire_frozen_runtime_root_lock(root, timeout=5.0) as handle:
        handle.assert_owned()
        hilo = threading.Thread(target=_hilo_segundo, daemon=True)
        adquirido_primero.set()
        hilo.start()
        segundo_intento_completo.wait(timeout=5.0)
        hilo.join(timeout=2.0)

    assert len(error_segundo) == 1
    assert isinstance(error_segundo[0], FrozenRuntimeLockBusyError)


# ============================================================================
# RL-02 — Mismo root, exclusión interproceso REAL
# ============================================================================


def test_rl02_mismo_root_exclusion_interproceso_real(tmp_path: pathlib.Path) -> None:
    """RL-02: Dos procesos de SO reales compitiendo por el mismo FrozenRuntimeRoot."""
    root = _crear_root_valido(tmp_path)
    senal_listo = tmp_path / "proceso_a_adquirido.tmp"
    senal_terminar = tmp_path / "proceso_a_puede_terminar.tmp"

    codigo_proceso_a = f"""
import sys, pathlib, time
from sky_claw.local.frozen_runtime.root_lock import acquire_frozen_runtime_root_lock

root = pathlib.Path({str(root)!r})
with acquire_frozen_runtime_root_lock(root, timeout=2.0) as lock:
    lock.assert_owned()
    pathlib.Path({str(senal_listo)!r}).write_text("OK", encoding="utf-8")
    # Esperar hasta que el proceso de test ordene liberar
    limite = time.monotonic() + 10.0
    while not pathlib.Path({str(senal_terminar)!r}).exists() and time.monotonic() < limite:
        time.sleep(0.05)
"""

    subp = subprocess.Popen(
        [sys.executable, "-c", codigo_proceso_a],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Esperar a que el proceso A confirme adquisición del lock
        limite_espera = time.monotonic() + 5.0
        while not senal_listo.exists() and time.monotonic() < limite_espera:
            time.sleep(0.05)
        assert senal_listo.exists(), (
            f"Proceso A no levantó a tiempo: {subp.stderr.read().decode('utf-8', errors='replace')}"
        )

        # Proceso B (este proceso de test) intenta adquirir el mismo root con timeout 0 => BUSY
        with pytest.raises(FrozenRuntimeLockBusyError):
            acquire_frozen_runtime_root_lock(root, timeout=0.0)

        # Proceso B intenta con timeout pequeño acotado => también vence y da BUSY
        inicio = time.monotonic()
        with pytest.raises(FrozenRuntimeLockBusyError):
            acquire_frozen_runtime_root_lock(root, timeout=0.2)
        duracion = time.monotonic() - inicio
        assert 0.15 <= duracion <= 1.0, f"Timeout no fue acotado: duró {duracion}s"

    finally:
        senal_terminar.write_text("GO", encoding="utf-8")
        try:
            subp.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            subp.kill()
            subp.wait(timeout=2.0)

    # Tras terminar proceso A, este proceso puede adquirir el lock
    with acquire_frozen_runtime_root_lock(root, timeout=2.0) as nuevo_handle:
        nuevo_handle.assert_owned()


# ============================================================================
# RL-03 — Roots diferentes no colisionan
# ============================================================================


def test_rl03_roots_diferentes_no_colisionan(tmp_path: pathlib.Path) -> None:
    """RL-03: Locks sobre roots distintos pueden coexistir simultáneamente."""
    root_a = _crear_root_valido(tmp_path, "root_a")
    root_b = _crear_root_valido(tmp_path, "root_b")

    with (
        acquire_frozen_runtime_root_lock(root_a, timeout=1.0) as handle_a,
        acquire_frozen_runtime_root_lock(root_b, timeout=1.0) as handle_b,
    ):
        handle_a.assert_owned()
        handle_b.assert_owned()
        assert handle_a.lock_path != handle_b.lock_path
        assert handle_a.canonical_root != handle_b.canonical_root


# ============================================================================
# RL-04 — Targets distintos, mismo root -> mismo lock
# ============================================================================


def test_rl04_targets_distintos_mismo_root_compiten_por_mismo_lock(tmp_path: pathlib.Path) -> None:
    """RL-04: Operaciones hacia targets distintos dentro del mismo root usan el mismo lock."""
    root = _crear_root_valido(tmp_path)
    # Operación 1 tiene target versions/gen_1, Operación 2 tiene target versions/gen_2
    # La clave y el lock dependen EXCLUSIVAMENTE del FrozenRuntimeRoot
    key1, path1 = canonical_root_key(root)
    key2, path2 = canonical_root_key(root)
    assert key1 == key2
    assert path1 == path2
    assert path1 == root / "state" / LOCK_FILE_NAME


# ============================================================================
# RL-05 — Lockfile no vive en TEMP
# ============================================================================


def test_rl05_lockfile_no_vive_en_temp(tmp_path: pathlib.Path) -> None:
    """RL-05: El lockfile vive bajo root/state, jamás bajo tempfile.gettempdir()."""
    import tempfile

    root = _crear_root_valido(tmp_path)
    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as handle:
        temp_dir_norm = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        lock_path_norm = os.path.normcase(os.path.abspath(os.fspath(handle.lock_path)))
        assert not lock_path_norm.startswith(temp_dir_norm.rstrip(os.sep) + os.sep)
        assert handle.lock_path.parent == root / "state"
        assert handle.lock_path.name == LOCK_FILE_NAME


# ============================================================================
# RL-06 — Session UUID distinta por adquisición
# ============================================================================


def test_rl06_session_uuid_distinta_por_adquisicion(tmp_path: pathlib.Path) -> None:
    """RL-06: Dos adquisiciones sucesivas generan session_id UUID distintos."""
    root = _crear_root_valido(tmp_path)

    handle1 = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    session1 = handle1.session_id
    handle1.release()

    handle2 = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    session2 = handle2.session_id
    handle2.release()

    assert session1 != session2
    # Ambos deben ser UUIDs válidos y canónicos en minúsculas con guiones
    u1 = uuid.UUID(session1)
    u2 = uuid.UUID(session2)
    assert str(u1) == session1
    assert str(u2) == session2


# ============================================================================
# RL-07 — Owner metadata schema
# ============================================================================


def test_rl07_owner_metadata_schema_normativo(tmp_path: pathlib.Path) -> None:
    """RL-07: La metadata persistida en disco cumple exactamente el esquema normativo de 6 campos."""
    root = _crear_root_valido(tmp_path)

    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as handle:
        handle.assert_owned()
        raw = handle.lock_path.read_text(encoding="utf-8")
        payload = json.loads(raw)

        assert isinstance(payload, dict)
        assert set(payload.keys()) == LOCK_METADATA_FIELDS
        assert payload["schema_version"] == LOCK_SCHEMA_VERSION
        assert payload["canonical_root"] == handle.canonical_root
        assert payload["session_id"] == handle.session_id
        assert payload["pid"] == os.getpid()
        assert isinstance(payload["process_create_time"], (int, float))
        assert isinstance(payload["acquired_at"], (int, float))


# ============================================================================
# RL-08 — PID reuse protection
# ============================================================================


def test_rl08_pid_reuse_detectado_como_no_mismo_owner(tmp_path: pathlib.Path) -> None:
    """RL-08: Mismo PID con creation_time distinto se clasifica como DEAD / huérfano."""
    root = _crear_root_valido(tmp_path)
    lock_file = root / "state" / LOCK_FILE_NAME

    # Simular metadata con el PID actual de este proceso, pero un creation_time falso (del pasado)
    metadata_simulada = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "canonical_root": os.path.normcase(os.path.abspath(os.fspath(root))),
        "session_id": str(uuid.uuid4()),
        "pid": os.getpid(),
        "process_create_time": 1000.0,  # creation_time falso
        "acquired_at": time.time(),
        "phase": "HELD",
    }
    lock_file.write_text(json.dumps(metadata_simulada), encoding="utf-8")

    # Inspeccionar sin OS lock activo: debe detectar que el dueño anterior murió (PID reciclado)
    resultado = inspect_frozen_runtime_root_lock(root)
    assert resultado.liveness == OwnerLiveness.DEAD
    assert resultado.disposition == LockDisposition.ORPHANED


# ============================================================================
# RL-09 — Owner liveness UNKNOWN -> FAIL_CLOSED
# ============================================================================


def test_rl09_owner_liveness_indeterminada_fail_closed(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RL-09: Si no se puede verificar liveness del dueño, clasifica INDETERMINATE y no asume DEAD."""
    root = _crear_root_valido(tmp_path)
    lock_file = root / "state" / LOCK_FILE_NAME

    metadata_simulada = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "canonical_root": os.path.normcase(os.path.abspath(os.fspath(root))),
        "session_id": str(uuid.uuid4()),
        "pid": 999999,
        "process_create_time": 123456.0,
        "acquired_at": time.time(),
        "phase": "HELD",
    }
    lock_file.write_text(json.dumps(metadata_simulada), encoding="utf-8")

    # Simular fallo indeterminado en la consulta de liveness (p.ej. PermissionError)
    from sky_claw.local.frozen_runtime import root_lock

    def _liveness_indeterminada(pid: int, create_time: float) -> OwnerLiveness:
        return OwnerLiveness.INDETERMINATE

    monkeypatch.setattr(root_lock, "_check_process_liveness", _liveness_indeterminada)

    resultado = inspect_frozen_runtime_root_lock(root)
    assert resultado.liveness == OwnerLiveness.INDETERMINATE
    assert resultado.disposition == LockDisposition.INDETERMINATE


# ============================================================================
# RL-10 — Release sólo del dueño
# ============================================================================


def test_rl10_release_solo_del_dueno(tmp_path: pathlib.Path) -> None:
    """RL-10: Un handle con session_id ajeno no puede liberar el lock de otra sesión."""
    root = _crear_root_valido(tmp_path)

    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as handle_real:
        handle_real.assert_owned()

        # Handle impostor con session_id falso
        handle_falso = FrozenRuntimeRootLockHandle(
            root=handle_real.root,
            canonical_root=handle_real.canonical_root,
            lock_path=handle_real.lock_path,
            session_id=str(uuid.uuid4()),
            pid=handle_real.pid,
            process_create_time=handle_real.process_create_time,
            acquired_at=handle_real.acquired_at,
        )

        with pytest.raises(FrozenRuntimeLockOwnershipError):
            handle_falso.release()

        # El handle real sigue siendo dueño
        handle_real.assert_owned()


# ============================================================================
# RL-11 — Double release
# ============================================================================


def test_rl11_double_release_idempotente(tmp_path: pathlib.Path) -> None:
    """RL-11: Llamar a release() dos veces sobre el mismo handle es idempotente y seguro."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.release()
    # Segundo release no debe fallar ni causar efectos secundarios
    handle.release()


# ============================================================================
# RL-12 — assert_owned tras release
# ============================================================================


def test_rl12_assert_owned_tras_release_lanza_error(tmp_path: pathlib.Path) -> None:
    """RL-12: assert_owned() después de release() lanza FrozenRuntimeLockOwnershipError."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.release()

    with pytest.raises(FrozenRuntimeLockOwnershipError):
        handle.assert_owned()


# ============================================================================
# RL-13 — Bounded timeout
# ============================================================================


def test_rl13_bounded_timeout_monotonico(tmp_path: pathlib.Path) -> None:
    """RL-13: El timeout es acotado y medido con time.monotonic()."""
    root = _crear_root_valido(tmp_path)

    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as primer_handle:
        primer_handle.assert_owned()

        # Timeout = 0 debe fallar en menos de 50ms
        inicio = time.monotonic()
        with pytest.raises(FrozenRuntimeLockBusyError):
            acquire_frozen_runtime_root_lock(root, timeout=0.0)
        duracion_cero = time.monotonic() - inicio
        assert duracion_cero < 0.1

        # Timeout = 0.15 debe esperar acotadamente
        inicio = time.monotonic()
        with pytest.raises(FrozenRuntimeLockBusyError):
            acquire_frozen_runtime_root_lock(root, timeout=0.15)
        duracion_acotada = time.monotonic() - inicio
        assert 0.10 <= duracion_acotada <= 0.6


# ============================================================================
# RL-14 — Crash process
# ============================================================================


def test_rl14_crash_proceso_libera_os_lock_y_conserva_evidencia(tmp_path: pathlib.Path) -> None:
    """RL-14: Un proceso hijo que crashea sin release libera el lock del SO y deja metadata intacta."""
    root = _crear_root_valido(tmp_path)
    lock_file = root / "state" / LOCK_FILE_NAME
    senal_listo = tmp_path / "crash_listo.tmp"

    codigo_hijo = f"""
import os, sys, pathlib, time
from sky_claw.local.frozen_runtime.root_lock import acquire_frozen_runtime_root_lock

root = pathlib.Path({str(root)!r})
# Adquirir lock y morir bruscamente con os._exit
handle = acquire_frozen_runtime_root_lock(root, timeout=2.0)
handle.assert_owned()
pathlib.Path({str(senal_listo)!r}).write_text(str(handle.session_id), encoding="utf-8")
os._exit(42)  # Muerte brusca sin cleanup ni finally
"""

    subp = subprocess.Popen(
        [sys.executable, "-c", codigo_hijo],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    salida, err = subp.communicate(timeout=5.0)
    assert subp.returncode == 42

    session_id_hijo = senal_listo.read_text(encoding="utf-8").strip()

    # 1. El archivo de metadata persiste intacto en disco (no se borró)
    assert lock_file.exists()
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    assert payload["session_id"] == session_id_hijo

    # 2. El lock del SO fue liberado por el kernel tras la muerte del proceso
    # 3. La inspección clasifica el lock como ORPHANED y al dueño como DEAD
    resultado = inspect_frozen_runtime_root_lock(root)
    assert resultado.is_os_locked is False
    assert resultado.disposition == LockDisposition.ORPHANED
    assert resultado.liveness == OwnerLiveness.DEAD
    assert resultado.metadata is not None
    assert resultado.metadata.session_id == session_id_hijo


# ============================================================================
# RL-15 — Reparse / symlink namespace en state/
# ============================================================================


def test_rl15_state_redirigido_rechaza_adquisicion_fail_closed(tmp_path: pathlib.Path) -> None:
    """RL-15: Si root/state es un symlink/junction/reparse, acquire falla cerrado."""
    root = tmp_path / "root_con_link"
    root.mkdir()
    state_externo = tmp_path / "state_externo"
    state_externo.mkdir()

    state_link = root / "state"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(state_externo), str(state_link))
    else:
        try:
            state_link.symlink_to(state_externo, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("El entorno no permite crear symlinks de directorio")

    with pytest.raises(FrozenRuntimeLockAdmissionError):
        acquire_frozen_runtime_root_lock(root, timeout=0.1)


# ============================================================================
# RL-16 — Lock metadata corrupta
# ============================================================================


@pytest.mark.parametrize(
    "contenido_invalido",
    [
        "esto no es json",
        "{}",
        '{"schema_version": 1}',  # faltan campos
        '{"schema_version": 999, "canonical_root": "x", "session_id": "y", "pid": 1, "process_create_time": 1.0, "acquired_at": 1.0, "phase": "HELD"}',  # schema desconocido
        '{"schema_version": 1, "canonical_root": "x", "session_id": "not-a-uuid", "pid": 1, "process_create_time": 1.0, "acquired_at": 1.0, "phase": "HELD"}',  # UUID inválido
        '{"schema_version": 1, "canonical_root": "x", "session_id": "a93f2a9a-329d-41b7-b4b5-93dfe30351ff", "pid": 1, "process_create_time": 1.0, "acquired_at": 1.0, "phase": "INVALID"}',  # phase inválido
    ],
)
def test_rl16_lock_metadata_corrupta_no_se_trata_como_libre(tmp_path: pathlib.Path, contenido_invalido: str) -> None:
    """RL-16: Metadata corrupta en disco clasifica como CORRUPT_METADATA y no permite robo silencioso."""
    root = _crear_root_valido(tmp_path)
    lock_file = root / "state" / LOCK_FILE_NAME
    lock_file.write_text(contenido_invalido, encoding="utf-8")

    resultado = inspect_frozen_runtime_root_lock(root)
    assert resultado.disposition == LockDisposition.CORRUPT_METADATA

    # acquire normal debe fallar cerrado ante metadata corrupta
    with pytest.raises(FrozenRuntimeLockMetadataError):
        acquire_frozen_runtime_root_lock(root, timeout=0.0)


# ============================================================================
# RL-17 — Canonical root
# ============================================================================


def test_rl17_canonical_root_representaciones_equivalentes(tmp_path: pathlib.Path) -> None:
    """RL-17: Distintas formas sintácticas del mismo root derivan exactamente la misma clave y lockfile."""
    root = _crear_root_valido(tmp_path)
    root_str = str(root)
    root_trailing_slash = root_str + os.sep
    root_rel = os.path.relpath(root_str, start=os.getcwd())

    k1, p1 = canonical_root_key(root)
    k2, p2 = canonical_root_key(root_str)
    k3, p3 = canonical_root_key(root_trailing_slash)
    k4, p4 = canonical_root_key(root_rel)

    assert k1 == k2 == k3 == k4
    assert p1 == p2 == p3 == p4


# ============================================================================
# RL-18 — Ownership metadata substitution
# ============================================================================


def test_rl18_sustitucion_de_metadata_falla_assert_owned_y_release(tmp_path: pathlib.Path) -> None:
    """RL-18: Si la metadata en disco cambia mientras el handle está vivo, assert_owned y release fallan sin borrar evidencia."""
    root = _crear_root_valido(tmp_path)

    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    # Sustituir la metadata en disco por la de otra sesión
    metadata_adulterada = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "canonical_root": handle.canonical_root,
        "session_id": str(uuid.uuid4()),  # Otra sesión
        "pid": os.getpid(),
        "process_create_time": handle.process_create_time,
        "acquired_at": time.time(),
        "phase": "HELD",
    }
    handle.lock_path.write_text(json.dumps(metadata_adulterada), encoding="utf-8")

    # Inmediatamente assert_owned debe fallar cerrado
    with pytest.raises(FrozenRuntimeLockOwnershipError):
        handle.assert_owned()

    # Y release() debe negarse a borrar la evidencia ajena
    with pytest.raises(FrozenRuntimeLockOwnershipError):
        handle.release()

    # La metadata adulterada se preserva en disco para análisis forense
    assert json.loads(handle.lock_path.read_text(encoding="utf-8"))["session_id"] == metadata_adulterada["session_id"]


# ============================================================================
# RL-19 — No human authorization
# ============================================================================


def test_rl19_estructura_del_lock_sin_autorizacion_humana(tmp_path: pathlib.Path) -> None:
    """RL-19: La metadata y handle del lock no contienen campos de aprobación humana."""
    root = _crear_root_valido(tmp_path)

    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as handle:
        # Verificar atributos del handle
        prohibidos = {"approval_scope", "approval_id", "authorization_token", "human_owner"}
        for campo in prohibidos:
            assert not hasattr(handle, campo), f"Handle contiene campo de autorización humana: {campo}"

        raw = handle.lock_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        for campo in prohibidos:
            assert campo not in payload, f"Metadata persistida contiene campo de autorización humana: {campo}"


# ============================================================================
# Guards Estructurales y Adversarios
# ============================================================================


def test_guard_no_import_destination_lock() -> None:
    """Guard: root_lock.py NO debe importar destination_lock (primitiva destination-keyed no reutilizable)."""
    modulo_path = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "frozen_runtime" / "root_lock.py"
    arbol = ast.parse(modulo_path.read_text(encoding="utf-8"), filename=str(modulo_path))

    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            if nodo.module and "destination_lock" in nodo.module:
                pytest.fail("root_lock.py importa destination_lock")
            for alias in nodo.names:
                if alias.name == "destination_lock":
                    pytest.fail("root_lock.py importa símbolo destination_lock")
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if "destination_lock" in alias.name:
                    pytest.fail("root_lock.py importa módulo destination_lock")


def test_guard_no_tempfile_gettempdir_authority() -> None:
    """Guard: root_lock.py NO debe usar tempfile.gettempdir() como autoridad del lock."""
    modulo_path = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "frozen_runtime" / "root_lock.py"
    arbol = ast.parse(modulo_path.read_text(encoding="utf-8"), filename=str(modulo_path))

    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute) and nodo.attr == "gettempdir":
            pytest.fail("root_lock.py utiliza gettempdir()")


def test_guard_derivacion_de_lock_no_depende_de_target() -> None:
    """Guard: La derivación del lock solo acepta FrozenRuntimeRoot y no rutas de target."""
    import inspect

    from sky_claw.local.frozen_runtime import root_lock

    sig = inspect.signature(root_lock.acquire_frozen_runtime_root_lock)
    parametros = list(sig.parameters.keys())
    assert "root" in parametros
    assert "target" not in parametros
    assert "destination" not in parametros


# ============================================================================
# F-01 a F-05 — Tests Quirúrgicos de Revisión Externa (PR #710 Follow-up)
# ============================================================================


def test_f01_inspect_empty_metadata_with_active_os_lock_not_free(tmp_path: pathlib.Path) -> None:
    """F-01 (P1): inspect() con archivo vacío pero lock del SO tomado NO debe devolver FREE."""
    root = _crear_root_valido(tmp_path)
    lock_path = root / "state" / LOCK_FILE_NAME
    # Crear archivo vacío (como ocurre en la ventana entre open() y write_metadata())
    lock_path.write_bytes(b"")

    # Tomar el lock del SO desde otro subproceso y mantenerlo
    senal_listo = tmp_path / "f01_locked.tmp"
    codigo_hijo = f"""
import os, sys, pathlib, time
from sky_claw.local.frozen_runtime.root_lock import _lock_fd

p = pathlib.Path({str(lock_path)!r})
f = open(p, "r+b")
assert _lock_fd(f.fileno()), "No se pudo tomar lock del SO"
pathlib.Path({str(senal_listo)!r}).write_text("OK", encoding="utf-8")
time.sleep(3.0)
f.close()
"""
    subp = subprocess.Popen([sys.executable, "-c", codigo_hijo])
    try:
        for _ in range(50):
            if senal_listo.exists():
                break
            time.sleep(0.05)
        assert senal_listo.exists(), "El subproceso no confirmó lock del SO"

        # Inspeccionar: debe detectar que el OS está bloqueado y la disposición NO es FREE
        res = inspect_frozen_runtime_root_lock(root)
        assert res.is_os_locked is True
        assert res.disposition != LockDisposition.FREE
        assert res.disposition == LockDisposition.ACQUIRING
    finally:
        subp.terminate()
        subp.wait(timeout=2.0)


def test_f02_release_records_released_phase_and_preserves_forensics(tmp_path: pathlib.Path) -> None:
    """F-02 (P1): release() escribe phase=RELEASED en vez de truncate(0) para crash-safety."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    session_id = handle.session_id
    pid = handle.pid

    # Durante HELD:
    payload_held = json.loads(handle.lock_path.read_text(encoding="utf-8"))
    assert payload_held["phase"] == "HELD"
    assert payload_held["session_id"] == session_id

    # Liberar limpiamente
    handle.release()

    # Forense post-release: el archivo NO debe estar vacío a 0 bytes
    assert handle.lock_path.exists()
    assert handle.lock_path.stat().st_size > 0
    payload_released = json.loads(handle.lock_path.read_text(encoding="utf-8"))
    assert payload_released["phase"] == "RELEASED"
    assert payload_released["session_id"] == session_id
    assert payload_released["pid"] == pid
    assert "released_at" in payload_released

    # inspect() sobre lock liberado reporta FREE
    res = inspect_frozen_runtime_root_lock(root)
    assert res.is_os_locked is False
    assert res.disposition == LockDisposition.FREE

    # Un nuevo acquire() adquiere limpiamente sin requerir reclaim_orphaned
    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as nuevo_handle:
        nuevo_handle.assert_owned()
        payload_nuevo = json.loads(nuevo_handle.lock_path.read_text(encoding="utf-8"))
        assert payload_nuevo["phase"] == "HELD"
        assert payload_nuevo["session_id"] != session_id


def test_f03_release_poisoned_metadata_fails_and_preserves_evidence(tmp_path: pathlib.Path) -> None:
    """F-03 (P1): release() con metadata alterada en disco no la trunca ni sobreescribe y lanza error."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)

    # Simular envenenamiento en disco por un atacante o proceso concurrente
    payload_envenenado = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "canonical_root": handle.canonical_root,
        "session_id": str(uuid.uuid4()),
        "pid": 12345,
        "process_create_time": 9999.0,
        "acquired_at": time.time(),
        "phase": "HELD",
    }
    handle.lock_path.write_text(json.dumps(payload_envenenado), encoding="utf-8")

    # release() debe detectar que el disco no le pertenece, lanzar error y NO truncar
    with pytest.raises(FrozenRuntimeLockOwnershipError):
        handle.release()

    # Comprobar que la evidencia envenenada sigue intacta en disco
    payload_post = json.loads(handle.lock_path.read_text(encoding="utf-8"))
    assert payload_post["session_id"] == payload_envenenado["session_id"]
    assert payload_post["pid"] == 12345


def test_f03b_acquire_fails_closed_when_held_by_live_owner(tmp_path: pathlib.Path) -> None:
    """F-03b (P1): acquire() no sobreescribe metadata si el lock previo registra HELD por un dueño EXTERNO vivo.

    F-22 hizo que un registro HELD del PROPIO proceso deje de ser BUSY (es el residuo de un
    release fallido, readquirible por el mismo proceso). El fail-closed que este test protege
    es el del dueño de OTRO proceso: por eso el dueño se materializa con un subproceso real
    que permanece vivo, y no con ``os.getpid()`` como antes.
    """
    root = _crear_root_valido(tmp_path)
    lock_path = root / "state" / LOCK_FILE_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    senal_pid = tmp_path / "dueno_externo.pid"

    codigo_dueno = f"""
import os, pathlib, time
destino = pathlib.Path({str(senal_pid)!r})
tmp = destino.with_suffix(".tmp")
tmp.write_text(str(os.getpid()), encoding="utf-8")
os.replace(tmp, destino)
time.sleep(30.0)
"""
    subp = subprocess.Popen(
        [sys.executable, "-c", codigo_dueno],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # El hijo publica el PID con os.replace (rename atómico): el archivo nunca es
        # visible a medio escribir, así que leerlo tras exists() no puede dar "" .
        limite = time.monotonic() + 15.0
        while not senal_pid.exists() and time.monotonic() < limite:
            time.sleep(0.05)
        assert senal_pid.exists(), f"El dueño externo no arrancó a tiempo (rc={subp.poll()})"

        foreign_pid = int(senal_pid.read_text(encoding="utf-8").strip())
        assert foreign_pid != os.getpid(), "El dueño externo debe ser OTRO proceso"
        assert psutil.Process(foreign_pid).is_running()
        foreign_create_time = psutil.Process(foreign_pid).create_time()

        payload_vivo = {
            "schema_version": LOCK_SCHEMA_VERSION,
            "canonical_root": os.path.normcase(os.path.abspath(os.fspath(root))),
            "session_id": str(uuid.uuid4()),
            "pid": foreign_pid,
            "process_create_time": foreign_create_time,
            "acquired_at": time.time(),
            "phase": "HELD",
        }
        lock_path.write_text(json.dumps(payload_vivo), encoding="utf-8")

        # Dueño externo VIVO ⇒ BUSY, aunque el mutex físico del SO esté libre.
        with pytest.raises(FrozenRuntimeLockBusyError):
            acquire_frozen_runtime_root_lock(root, timeout=0.0)

        # Ni el reclaim explícito puede pisar a un dueño externo vivo (F-22 no relaja esto).
        with pytest.raises(FrozenRuntimeLockBusyError):
            acquire_frozen_runtime_root_lock(root, timeout=0.0, reclaim_orphaned=True)

        # La evidencia del dueño externo sigue intacta en disco.
        payload_post = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload_post["session_id"] == payload_vivo["session_id"]
        assert payload_post["pid"] == foreign_pid
    finally:
        if subp.poll() is None:
            subp.terminate()
        try:
            subp.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            subp.kill()
            subp.communicate(timeout=5.0)


def test_f04_metadata_strict_typing_and_values(tmp_path: pathlib.Path) -> None:
    """F-04 (P2): _read_metadata_bytes rechaza tipos bool como int/float, roots ajenos y floats no finitos."""
    root = _crear_root_valido(tmp_path)
    canonical_str = os.path.normcase(os.path.abspath(os.fspath(root)))

    base = {
        "schema_version": 1,
        "canonical_root": canonical_str,
        "session_id": str(uuid.uuid4()),
        "pid": 123,
        "process_create_time": 100.0,
        "acquired_at": 200.0,
        "phase": "HELD",
    }

    # 1. canonical_root ajeno al root esperado
    payload_wrong_root = dict(base)
    payload_wrong_root["canonical_root"] = "c:/otro/root"
    raw = json.dumps(payload_wrong_root).encode("utf-8")
    with pytest.raises(FrozenRuntimeLockMetadataError, match="canonical_root no coincide"):
        _read_metadata_bytes(raw, expected_canonical_root=canonical_str)

    # 2. canonical_root no es string
    payload_non_str_root = dict(base)
    payload_non_str_root["canonical_root"] = 12345
    with pytest.raises(FrozenRuntimeLockMetadataError, match="canonical_root debe ser un string"):
        _read_metadata_bytes(json.dumps(payload_non_str_root).encode("utf-8"))

    # 3. bool como int/float (True es int en Python)
    for field in ["pid", "schema_version", "process_create_time", "acquired_at"]:
        payload_bool = dict(base)
        payload_bool[field] = True
        with pytest.raises(FrozenRuntimeLockMetadataError, match=f"{field} inválido"):
            _read_metadata_bytes(json.dumps(payload_bool).encode("utf-8"))

    # 4. Floats no finitos (inf, nan, <=0)
    for field in ["process_create_time", "acquired_at"]:
        payload_inf = dict(base)
        payload_inf[field] = float("inf")
        with pytest.raises(FrozenRuntimeLockMetadataError, match=f"{field} inválido"):
            _read_metadata_bytes(json.dumps(payload_inf).encode("utf-8"))

        payload_neg = dict(base)
        payload_neg[field] = -1.0
        with pytest.raises(FrozenRuntimeLockMetadataError, match=f"{field} inválido"):
            _read_metadata_bytes(json.dumps(payload_neg).encode("utf-8"))


def test_f04b_assert_owned_checks_all_metadata_fields(tmp_path: pathlib.Path) -> None:
    """F-04b (P2): assert_owned() valida que create_time, acquired_at y phase sigan coincidiendo."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    # Alterar process_create_time en disco
    payload = json.loads(handle.lock_path.read_text(encoding="utf-8"))
    payload["process_create_time"] = payload["process_create_time"] + 10.0
    handle.lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="process_create_time"):
        handle.assert_owned()

    # Restaurar y alterar acquired_at
    payload["process_create_time"] = handle.process_create_time
    payload["acquired_at"] = payload["acquired_at"] + 10.0
    handle.lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="acquired_at"):
        handle.assert_owned()

    # Restaurar y alterar phase a RELEASED
    payload["acquired_at"] = handle.acquired_at
    payload["phase"] = "RELEASED"
    handle.lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="phase"):
        handle.assert_owned()

    # Restaurar a HELD para permitir liberación limpia
    payload["phase"] = "HELD"
    handle.lock_path.write_text(json.dumps(payload), encoding="utf-8")
    handle.release()


def test_f05_acquire_base_exception_cleans_up_thread_and_os_locks(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-05 (P2): acquire() maneja BaseException (KeyboardInterrupt) liberando thread lock y fd."""
    root = _crear_root_valido(tmp_path)
    canonical_str, _ = canonical_root_key(root)
    thread_lock = _get_thread_lock(canonical_str)

    from sky_claw.local.frozen_runtime import root_lock

    def _simular_keyboard_interrupt(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt("Simulación de Ctrl+C durante adquisición")

    # Interceptar después de tomar el thread lock
    monkeypatch.setattr(root_lock, "_lock_fd", _simular_keyboard_interrupt)

    with pytest.raises(KeyboardInterrupt):
        acquire_frozen_runtime_root_lock(root, timeout=1.0)

    # El thread lock DEBE haber sido liberado estructuralmente
    assert not thread_lock.locked(), "El thread lock quedó retenido tras KeyboardInterrupt!"

    # Una siguiente adquisición debe poder ejecutarse sin deadlock
    monkeypatch.undo()
    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as nuevo_handle:
        nuevo_handle.assert_owned()


# ============================================================================
# F-06 a F-11 — Segunda Ola de Hardening Adversarial (PR #710)
# ============================================================================


def test_f06_inspect_applies_anti_reparse_admission_boundary(tmp_path: pathlib.Path) -> None:
    """F-06 (P1): inspect() aplica el boundary anti-reparse sin crear directorios (REDIRECTED != FREE)."""
    # 1. state/ es un junction o symlink
    root_link = tmp_path / "root_link"
    root_link.mkdir()
    state_externo = tmp_path / "state_ext"
    state_externo.mkdir()
    state_target = root_link / "state"

    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(state_externo), str(state_target))
    else:
        try:
            state_target.symlink_to(state_externo, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("El entorno no permite symlinks de directorio")

    with pytest.raises(FrozenRuntimeLockAdmissionError, match="state/ es un enlace/reparse"):
        inspect_frozen_runtime_root_lock(root_link)

    # 2. lock_path redirigido por symlink de archivo
    root_file_link = _crear_root_valido(tmp_path)
    lock_file = root_file_link / "state" / LOCK_FILE_NAME
    archivo_externo = tmp_path / "externo.lock"
    archivo_externo.write_text("dummy", encoding="utf-8")
    try:
        lock_file.symlink_to(archivo_externo)
        with pytest.raises(FrozenRuntimeLockAdmissionError, match="lockfile es un enlace/reparse"):
            inspect_frozen_runtime_root_lock(root_file_link)
    except (OSError, NotImplementedError):
        pass  # En Windows sin SeCreateSymbolicLinkPrivilege los symlinks de archivo requieren permisos

    # 3. root rechazado por almacenamiento (ej. forma reservada de Windows o steamapps)
    root_steam = tmp_path / "steamapps" / "common" / "Skyrim Special Edition" / "frozen"
    with pytest.raises(FrozenRuntimeLockAdmissionError, match="administrada por Steam"):
        inspect_frozen_runtime_root_lock(root_steam)

    # 4. inspect() sobre un root nuevo NO debe crear el directorio state/
    root_nuevo = tmp_path / "root_nuevo_sin_state"
    root_nuevo.mkdir()
    res = inspect_frozen_runtime_root_lock(root_nuevo)
    assert res.disposition == LockDisposition.FREE
    assert not (root_nuevo / "state").exists(), "inspect() no debe crear directorios mutando el storage"


def test_f07_forked_or_foreign_process_cannot_assert_owned_or_release(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-07 (P1): Un proceso forkeado o ajeno al creador del handle no puede llamar assert_owned() ni release()."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    # 1. Proceso con PID distinto
    monkeypatch.setattr(os, "getpid", lambda: handle.pid + 1000)
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="Frontera de proceso violada"):
        handle.assert_owned()
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="Frontera de proceso violada"):
        handle.release()

    # 2. Proceso con mismo PID pero create_time distinto (PID reciclado tras fork/exec)
    monkeypatch.setattr(os, "getpid", lambda: handle.pid)
    from unittest.mock import MagicMock

    mock_proc = MagicMock()
    mock_proc.create_time.return_value = handle.process_create_time + 10.0
    monkeypatch.setattr(psutil, "Process", lambda pid: mock_proc)
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="process_create_time"):
        handle.assert_owned()
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="process_create_time"):
        handle.release()

    # Restaurar y verificar que el dueño legítimo sí puede liberar
    monkeypatch.undo()
    handle.assert_owned()
    handle.release()


def test_f08_release_crash_boundary_committed_at_released_marker(tmp_path: pathlib.Path) -> None:
    """F-08 (P1): Commit point de release es la escritura durable de phase=RELEASED (Opción A)."""
    root = _crear_root_valido(tmp_path)

    # Escenario A: Crash antes del commit point (phase=HELD en disco al morir el proceso)
    senal_listo_held = tmp_path / "crash_held_ready.tmp"
    codigo_crash_held = f"""
import os, sys, pathlib, time
from sky_claw.local.frozen_runtime.root_lock import acquire_frozen_runtime_root_lock

root = pathlib.Path({str(root)!r})
handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
pathlib.Path({str(senal_listo_held)!r}).write_text(handle.session_id, encoding="utf-8")
time.sleep(10.0)
"""
    p_held = subprocess.Popen([sys.executable, "-c", codigo_crash_held])
    try:
        for _ in range(50):
            if senal_listo_held.exists():
                break
            time.sleep(0.05)
        assert senal_listo_held.exists()
    finally:
        p_held.kill()
        p_held.wait(timeout=2.0)

    # Tras crash con phase=HELD, el kernel liberó OS lock pero metadata indica HELD de dueño DEAD -> ORPHANED
    res_held = inspect_frozen_runtime_root_lock(root)
    assert res_held.is_os_locked is False
    assert res_held.disposition == LockDisposition.ORPHANED
    assert res_held.liveness == OwnerLiveness.DEAD
    assert res_held.metadata is not None
    assert res_held.metadata.phase == LockPhase.HELD

    # Escenario B: Crash DESPUÉS del commit point (phase=RELEASED persistido y fsync'd, pero proceso muere antes de cerrar fd)
    senal_listo_rel = tmp_path / "crash_rel_ready.tmp"
    codigo_crash_rel = f"""
import os, sys, pathlib, time
from sky_claw.local.frozen_runtime.root_lock import (
    acquire_frozen_runtime_root_lock,
    LOCK_SCHEMA_VERSION,
    LockPhase,
)
import json

root = pathlib.Path({str(root)!r})
handle = acquire_frozen_runtime_root_lock(root, timeout=1.0, reclaim_orphaned=True)

# Simular commit point de release: escribir phase=RELEASED y fsync, manteniendo OS lock abierto
payload = {{
    "schema_version": LOCK_SCHEMA_VERSION,
    "canonical_root": handle.canonical_root,
    "session_id": handle.session_id,
    "pid": handle.pid,
    "process_create_time": handle.process_create_time,
    "acquired_at": handle.acquired_at,
    "phase": LockPhase.RELEASED.value,
    "released_at": time.time(),
}}
serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
handle._file_obj.seek(0)
handle._file_obj.write(serialized)
handle._file_obj.truncate(len(serialized))
handle._file_obj.flush()
os.fsync(handle._file_obj.fileno())

pathlib.Path({str(senal_listo_rel)!r}).write_text("COMMITTED", encoding="utf-8")
# Morir abruptamente manteniendo fd abierto hasta el fin
time.sleep(10.0)
"""
    p_rel = subprocess.Popen([sys.executable, "-c", codigo_crash_rel])
    try:
        for _ in range(50):
            if senal_listo_rel.exists():
                break
            time.sleep(0.05)
        assert senal_listo_rel.exists()
    finally:
        p_rel.kill()
        p_rel.wait(timeout=2.0)

    # Tras muerte después del commit point: OS lock liberado y metadata tiene phase=RELEASED -> FREE
    res_rel = inspect_frozen_runtime_root_lock(root)
    assert res_rel.is_os_locked is False
    assert res_rel.disposition == LockDisposition.FREE
    assert res_rel.metadata is not None
    assert res_rel.metadata.phase == LockPhase.RELEASED


def test_f09_release_validates_full_lock_owner_identity_matching_assert_owned(
    tmp_path: pathlib.Path,
) -> None:
    """F-09 (P1): release() valida todos los campos de LOCK_OWNER antes de mutar el lockfile."""
    # 1. acquired_at alterado en disco
    root1 = _crear_root_valido(tmp_path / "case_acquired")
    handle1 = acquire_frozen_runtime_root_lock(root1, timeout=1.0)
    original_meta1 = json.loads(handle1.lock_path.read_text(encoding="utf-8"))
    tampered_acquired = dict(original_meta1)
    tampered_acquired["acquired_at"] = original_meta1["acquired_at"] + 5.0
    handle1.lock_path.write_text(json.dumps(tampered_acquired), encoding="utf-8")
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="acquired_at"):
        handle1.release()
    # Comprobar que no sobreescribió
    assert json.loads(handle1.lock_path.read_text(encoding="utf-8"))["acquired_at"] == tampered_acquired["acquired_at"]

    # 2. canonical_root alterado en disco
    root2 = _crear_root_valido(tmp_path / "case_root")
    handle2 = acquire_frozen_runtime_root_lock(root2, timeout=1.0)
    original_meta2 = json.loads(handle2.lock_path.read_text(encoding="utf-8"))
    tampered_root = dict(original_meta2)
    tampered_root["canonical_root"] = "c:/otro/root/distinto"
    handle2.lock_path.write_text(json.dumps(tampered_root), encoding="utf-8")
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="canonical_root"):
        handle2.release()

    # 3. phase alterado a un valor distinto de HELD
    root3 = _crear_root_valido(tmp_path / "case_phase")
    handle3 = acquire_frozen_runtime_root_lock(root3, timeout=1.0)
    original_meta3 = json.loads(handle3.lock_path.read_text(encoding="utf-8"))
    tampered_phase = dict(original_meta3)
    tampered_phase["phase"] = "RELEASED"
    tampered_phase["released_at"] = time.time()
    handle3.lock_path.write_text(json.dumps(tampered_phase), encoding="utf-8")
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="phase"):
        handle3.release()

    # 4. Descriptor de archivo cerrado
    root4 = _crear_root_valido(tmp_path / "case_file_closed")
    handle4 = acquire_frozen_runtime_root_lock(root4, timeout=1.0)
    original_meta4 = json.loads(handle4.lock_path.read_text(encoding="utf-8"))
    handle4.lock_path.write_text(json.dumps(original_meta4), encoding="utf-8")
    handle4._file_obj.close()
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="descriptor de archivo"):
        handle4.release()

    # 5. Handle ya liberado es idempotente (no error)
    root5 = _crear_root_valido(tmp_path / "case_idempotent")
    handle5 = acquire_frozen_runtime_root_lock(root5, timeout=1.0)
    handle5.release()
    handle5.release()  # No-op


def test_f10_schema_enforces_phase_released_at_consistency(tmp_path: pathlib.Path) -> None:
    """F-10 (P1): Validación estricta de coherencia entre phase, released_at y campos permitidos."""
    root = _crear_root_valido(tmp_path)
    canonical_str = os.path.normcase(os.path.abspath(os.fspath(root)))
    base_held = {
        "schema_version": 1,
        "canonical_root": canonical_str,
        "session_id": str(uuid.uuid4()),
        "pid": 1234,
        "process_create_time": 100.0,
        "acquired_at": 200.0,
        "phase": "HELD",
    }

    # 1. phase=HELD con released_at presente debe fallar
    held_with_rel = dict(base_held)
    held_with_rel["released_at"] = 250.0
    with pytest.raises(FrozenRuntimeLockMetadataError, match="released_at"):
        _read_metadata_bytes(json.dumps(held_with_rel).encode("utf-8"))

    # 2. phase=HELD con campo desconocido debe fallar
    held_with_unknown = dict(base_held)
    held_with_unknown["unknown_campo"] = "valor"
    with pytest.raises(FrozenRuntimeLockMetadataError, match="Campos de metadata inválidos"):
        _read_metadata_bytes(json.dumps(held_with_unknown).encode("utf-8"))

    base_released = {
        "schema_version": 1,
        "canonical_root": canonical_str,
        "session_id": str(uuid.uuid4()),
        "pid": 1234,
        "process_create_time": 100.0,
        "acquired_at": 200.0,
        "phase": "RELEASED",
    }

    # 3. phase=RELEASED sin released_at debe fallar
    with pytest.raises(FrozenRuntimeLockMetadataError, match="released_at"):
        _read_metadata_bytes(json.dumps(base_released).encode("utf-8"))

    # 4. phase=RELEASED con released_at < acquired_at debe fallar
    rel_earlier = dict(base_released)
    rel_earlier["released_at"] = 199.0
    with pytest.raises(FrozenRuntimeLockMetadataError, match="no puede ser anterior a acquired_at"):
        _read_metadata_bytes(json.dumps(rel_earlier).encode("utf-8"))

    # 5. phase=RELEASED con released_at no numérico / bool / inf
    for invalid_val in [True, None, "200.0", float("inf"), float("nan"), -1.0]:
        rel_invalid = dict(base_released)
        rel_invalid["released_at"] = invalid_val
        with pytest.raises(FrozenRuntimeLockMetadataError, match="released_at"):
            _read_metadata_bytes(json.dumps(rel_invalid).encode("utf-8"))

    # 6. phase=RELEASED válido pasa limpiamente
    rel_valid = dict(base_released)
    rel_valid["released_at"] = 205.0
    meta = _read_metadata_bytes(json.dumps(rel_valid).encode("utf-8"))
    assert meta.phase == LockPhase.RELEASED
    assert meta.released_at == 205.0


def test_f11_liveness_zombie_and_tight_float_precision(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-11 (P1): Comprobación universal de zombie y tolerancia estricta 1e-4 para liveness."""
    from unittest.mock import MagicMock

    from sky_claw.local.frozen_runtime.root_lock import OwnerLiveness, _check_process_liveness

    mock_proc = MagicMock()
    mock_proc.is_running.return_value = True
    mock_proc.status.return_value = psutil.STATUS_ZOMBIE
    mock_proc.create_time.return_value = 1000.0
    monkeypatch.setattr(psutil, "Process", lambda pid: mock_proc)

    # 1. Proceso ZOMBIE en cualquier SO reporta DEAD
    liveness_zombie = _check_process_liveness(1234, 1000.0)
    assert liveness_zombie == OwnerLiveness.DEAD

    # 2. Tolerancia estricta: discrepancia de 1ms (0.001) clasifica DEAD (antes 0.05 toleraba hasta 50ms)
    mock_proc.status.return_value = psutil.STATUS_RUNNING
    mock_proc.create_time.return_value = 1000.001
    liveness_diff_1ms = _check_process_liveness(1234, 1000.0)
    assert liveness_diff_1ms == OwnerLiveness.DEAD

    # 3. Discrepancia mínima por representación flotante (1e-5) se clasifica ALIVE
    mock_proc.create_time.return_value = 1000.0 + 1e-5
    liveness_tight = _check_process_liveness(1234, 1000.0)
    assert liveness_tight == OwnerLiveness.ALIVE


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork() sólo está disponible en entornos POSIX")
def test_f12_posix_real_fork_child_cannot_assert_owned_or_release(tmp_path: pathlib.Path) -> None:
    """F-12 (P2): Un proceso hijo real creado vía os.fork() no puede llamar assert_owned() ni release()."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    pid = os.fork()
    if pid == 0:
        # Proceso hijo
        exit_code = 0
        try:
            # 1. El hijo heredó el handle; intentar assert_owned() DEBE fallar con FrozenRuntimeLockOwnershipError
            try:
                handle.assert_owned()
                exit_code = 1  # No falló como se esperaba
            except FrozenRuntimeLockOwnershipError:
                pass
            except Exception:
                exit_code = 2

            # 2. Intentar release() desde el hijo DEBE fallar con FrozenRuntimeLockOwnershipError
            try:
                handle.release()
                exit_code = 3  # No falló como se esperaba
            except FrozenRuntimeLockOwnershipError:
                pass
            except Exception:
                exit_code = 4
        finally:
            os._exit(exit_code)
    else:
        # Proceso padre: esperar al hijo y verificar su código de salida
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status), "El proceso hijo no terminó normalmente"
        exit_code = os.WEXITSTATUS(status)
        assert exit_code == 0, f"El proceso hijo falló la verificación de ownership con código {exit_code}"

        # El padre sigue siendo el dueño legítimo
        handle.assert_owned()
        handle.release()


def test_f13_assert_owned_fails_if_os_lock_is_lost_causally(tmp_path: pathlib.Path) -> None:
    """F-13 (P1): assert_owned() debe fallar si el mutex del SO se pierde causalmente, aun con metadata y fd intactos."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    # Forzar pérdida real del lock del SO sin cerrar el descriptor de archivo ni alterar la metadata en disco
    from sky_claw.local.frozen_runtime.root_lock import _unlock_fd

    _unlock_fd(handle._file_obj.fileno())

    # Metadata en disco, fd abierto, proceso y _ACTIVE_HANDLES siguen intactos,
    # pero el lock del SO fue liberado. assert_owned() DEBE fallar inmediatamente.
    with pytest.raises(FrozenRuntimeLockOwnershipError, match="mutex del sistema operativo"):
        handle.assert_owned()


def test_f14_assert_owned_rejects_indeterminate_probe_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-14 (P1): assert_owned() no acepta errores de I/O o indeterminado en el probe como prueba de posesión (UNKNOWN != OWNED)."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    from sky_claw.local.frozen_runtime import (
        FrozenRuntimeLockIndeterminateError,
        FrozenRuntimeLockOwnershipError,
    )
    from sky_claw.local.frozen_runtime.root_lock import OSLockProbeResult

    # Forzar que la sonda del SO devuelva INDETERMINATE (simulando OSError de I/O, permisos o transient)
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.root_lock._probe_os_lock_state",
        lambda lock_path: OSLockProbeResult.INDETERMINATE,
    )

    # assert_owned DEBE fallar cerrado (rechazando INDETERMINATE como prueba positiva)
    with pytest.raises(FrozenRuntimeLockOwnershipError) as exc_info:
        handle.assert_owned()

    assert "fail-closed" in str(exc_info.value) or "UNKNOWN != OWNED" in str(exc_info.value)
    # También es capturable como FrozenRuntimeLockIndeterminateError
    assert isinstance(exc_info.value, FrozenRuntimeLockIndeterminateError)

    # Restaurar y liberar limpiamente
    monkeypatch.undo()
    handle.assert_owned()
    handle.release()


def test_f14_probe_unexpected_oserror_returns_indeterminate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-14 (P1): Errores inesperados de sistema o I/O al sondear el mutex clasifican INDETERMINATE, nunca HELD."""
    root = _crear_root_valido(tmp_path)
    canonical_str, lock_path = canonical_root_key(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"test")

    import builtins
    import errno

    from sky_claw.local.frozen_runtime.root_lock import (
        OSLockProbeResult,
        _probe_os_lock_state,
    )

    # Caso 1: Error al abrir el archivo (e.g. EIO o EACCES no de lock sino de permisos de FS)
    real_open = builtins.open

    def mock_open(file, *args, **kwargs):
        if str(file) == str(lock_path):
            raise OSError(errno.EIO, "Fallo transitorio de I/O de disco")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", mock_open)
    assert _probe_os_lock_state(lock_path) == OSLockProbeResult.INDETERMINATE
    monkeypatch.undo()

    # Caso 2: Error inesperado al intentar bloquear en _try_lock_probe_fd (e.g. ENOSPC)
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.root_lock._try_lock_probe_fd",
        lambda fd: None,
    )
    assert _probe_os_lock_state(lock_path) == OSLockProbeResult.INDETERMINATE


def test_f14_inspect_classifies_indeterminate_probe_as_indeterminate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-14 (P1): inspect() clasifica como INDETERMINATE cuando la sonda del mutex del SO falla con error no concluyente."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    from sky_claw.local.frozen_runtime.root_lock import (
        LockDisposition,
        OSLockProbeResult,
    )

    # Forzar que la sonda del SO devuelva INDETERMINATE
    monkeypatch.setattr(
        "sky_claw.local.frozen_runtime.root_lock._probe_os_lock_state",
        lambda lock_path: OSLockProbeResult.INDETERMINATE,
    )

    res = inspect_frozen_runtime_root_lock(root)
    assert res.disposition == LockDisposition.INDETERMINATE
    assert res.is_os_locked is False

    monkeypatch.undo()
    handle.release()


def test_f16_release_failure_cleans_up_local_resources_in_owner_process(
    tmp_path: pathlib.Path,
) -> None:
    """F-16 (P1): Si release() falla validación de ownership en el dueño, limpia recursos locales sin deadlocks."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    from sky_claw.local.frozen_runtime import FrozenRuntimeLockOwnershipError
    from sky_claw.local.frozen_runtime.root_lock import _ACTIVE_HANDLES

    # Adulterar la metadata en disco para que falle la validación
    adulterado = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "canonical_root": handle.canonical_root,
        "session_id": str(uuid.uuid4()),
        "pid": os.getpid(),
        "process_create_time": handle.process_create_time,
        "acquired_at": time.time(),
        "phase": "HELD",
    }
    handle.lock_path.write_text(json.dumps(adulterado), encoding="utf-8")

    # release() debe fallar con FrozenRuntimeLockOwnershipError
    with pytest.raises(FrozenRuntimeLockOwnershipError):
        handle.release()

    # Pero los recursos locales del proceso dueño deben haberse limpiado (F-16 fail-safe)
    assert handle._released is True
    assert handle.canonical_root not in _ACTIVE_HANDLES
    assert handle._file_obj is None or handle._file_obj.closed is True
    if handle._thread_lock is not None:
        assert not handle._thread_lock.locked()


def test_f17_inspect_live_owner_without_os_mutex_is_indeterminate(
    tmp_path: pathlib.Path,
) -> None:
    """F-17 (P2): inspect() con owner vivo, phase=HELD pero sin mutex del SO clasifica INDETERMINATE, no ORPHANED."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    from sky_claw.local.frozen_runtime.root_lock import (
        LockDisposition,
        _lock_fd,
        _unlock_fd,
    )

    # Forzar la pérdida del mutex a nivel de descriptor sin alterar metadata
    _unlock_fd(handle._file_obj.fileno())

    # Metadata = HELD, owner = ALIVE (este proceso), pero OS lock = FREE
    res = inspect_frozen_runtime_root_lock(root)
    assert res.disposition == LockDisposition.INDETERMINATE
    assert res.is_os_locked is False
    assert res.liveness == OwnerLiveness.ALIVE

    # Restaurar y liberar
    assert _lock_fd(handle._file_obj.fileno()) is True
    handle.release()


def test_f18_metadata_rewrite_leaves_no_stale_tail(tmp_path: pathlib.Path) -> None:
    """F-18 (P1): Reescritura de metadata con JSON más corto no deja colas viejas ni 'Extra data'."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()

    # release() escribe metadata RELEASED con released_at (JSON más largo)
    handle.release()
    len_released = handle.lock_path.stat().st_size

    # Adquirir nuevamente escribe HELD sin released_at (JSON más corto)
    nuevo_handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    nuevo_handle.assert_owned()
    len_held = nuevo_handle.lock_path.stat().st_size

    assert len_held < len_released, f"Se esperaba JSON de HELD más corto ({len_held} < {len_released})"

    # Leer bytes crudos y comprobar que no hay bytes sobrantes ni errores de Extra data
    raw = nuevo_handle.lock_path.read_bytes()
    assert len(raw) == len_held

    from sky_claw.local.frozen_runtime.root_lock import _read_metadata_bytes

    meta = _read_metadata_bytes(raw, expected_canonical_root=nuevo_handle.canonical_root)
    assert meta.phase == LockPhase.HELD

    nuevo_handle.release()


def test_f19_posix_fork_child_does_not_keep_kernel_flock_if_parent_dies(
    tmp_path: pathlib.Path,
) -> None:
    """F-19 (P1): Proceso hijo tras fork() no retiene el flock en el kernel si el padre muere."""
    if not hasattr(os, "fork"):
        pytest.skip("Test causal exclusivo de POSIX (requiere os.fork)")

    root = _crear_root_valido(tmp_path)
    pipe_r, pipe_w = os.pipe()

    # Proceso A (padre) adquiere lock, forkea hijo, y termina
    parent_pid = os.fork()
    if parent_pid == 0:
        # En el proceso A
        codigo = 1
        try:
            os.close(pipe_r)
            handle = acquire_frozen_runtime_root_lock(root, timeout=2.0)
            handle.assert_owned()

            child_b_pid = os.fork()
            if child_b_pid == 0:
                codigo_hijo = 1
                try:
                    b_pid = os.getpid()
                    os.write(pipe_w, f"{b_pid}\n".encode("ascii"))
                    os.close(pipe_w)
                    time.sleep(30)
                    codigo_hijo = 0
                finally:
                    os._exit(codigo_hijo)

            os.close(pipe_w)
            codigo = 0
        finally:
            with contextlib.suppress(OSError):
                os.close(pipe_w)
            os._exit(codigo)

    # En el test runner
    os.close(pipe_w)
    pipe_file = os.fdopen(pipe_r, "r")
    child_b_pid_str = pipe_file.readline().strip()
    pipe_file.close()
    if not child_b_pid_str:
        _, status = os.waitpid(parent_pid, 0)
        pytest.fail(f"El proceso A terminó antes de comunicar el PID de B: status={status}")
    child_b_pid = int(child_b_pid_str)

    try:
        # Esperar a que el proceso A (dueño del lock) haya muerto
        _, status = os.waitpid(parent_pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0

        # Verificar que hijo B sigue VIVO
        try:
            os.kill(child_b_pid, 0)
            child_b_alive = True
        except OSError:
            child_b_alive = False
        assert child_b_alive, "El hijo B debería seguir vivo para comprobar que no retiene el descriptor"

        # Con el padre muerto y el hijo vivo: un nuevo proceso DEBE poder recuperar el lock (reclaim_orphaned)
        nuevo_handle = acquire_frozen_runtime_root_lock(root, timeout=2.0, reclaim_orphaned=True)
        nuevo_handle.assert_owned()
        nuevo_handle.release()
    finally:
        # Matar al hijo B y hacer waitpid
        with contextlib.suppress(OSError):
            os.kill(child_b_pid, 9)
        with contextlib.suppress(OSError):
            os.waitpid(child_b_pid, 0)


def test_f20_metadata_rewrite_padded_with_whitespace_preserves_crash_evidence(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-20 (P1): Reescritura con padding de espacios preserva evidencia ante crash antes de truncar."""
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()
    canonical_root = handle.canonical_root
    lock_path = handle.lock_path
    handle.release()

    len_released = lock_path.stat().st_size
    assert len_released > 0

    from sky_claw.local.frozen_runtime.root_lock import (
        _read_metadata_bytes,
        _write_metadata_crash_safe,
    )

    # Simular una escritura de HELD (más corto) donde el proceso crashea antes del truncate final
    payload_held = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "canonical_root": canonical_root,
        "session_id": str(uuid.uuid4()),
        "pid": 999999,  # PID no existente (muerto)
        "process_create_time": 1000.0,
        "acquired_at": time.time(),
        "phase": LockPhase.HELD.value,
    }

    # Interceptar truncate para simular crash de proceso inmediatamente tras el primer fsync
    class CrashBeforeTruncateError(Exception):
        pass

    with open(lock_path, "r+b") as f:

        def crashing_truncate(size: int | None = None) -> int:
            raise CrashBeforeTruncateError("Crash simulado antes de truncar espacio residual")

        monkeypatch.setattr(f, "truncate", crashing_truncate)
        with pytest.raises(CrashBeforeTruncateError):
            _write_metadata_crash_safe(f, payload_held)

    # 1. El archivo preserva su tamaño previo (len_released), no quedó truncado a 0
    size_after_crash = lock_path.stat().st_size
    assert size_after_crash == len_released

    # 2. El contenido en disco es JSON sintácticamente válido terminado en espacios ASCII
    raw_after_crash = lock_path.read_bytes()
    assert raw_after_crash.endswith(b" ")
    meta = _read_metadata_bytes(raw_after_crash, expected_canonical_root=canonical_root)
    assert meta.phase == LockPhase.HELD
    assert meta.pid == 999999

    # 3. inspect() NO declara FREE ni CORRUPT_METADATA: preserva la evidencia y declara ORPHANED
    res = inspect_frozen_runtime_root_lock(root)
    assert res.disposition == LockDisposition.ORPHANED
    assert res.metadata is not None
    assert res.metadata.phase == LockPhase.HELD

    # 4. En ejecución normal sin interrupción, _write_metadata_crash_safe trunca los espacios
    with open(lock_path, "r+b") as f:
        _write_metadata_crash_safe(f, payload_held)

    size_normal = lock_path.stat().st_size
    assert size_normal < len_released
    raw_normal = lock_path.read_bytes()
    assert not raw_normal.endswith(b" ")
    meta_normal = _read_metadata_bytes(raw_normal, expected_canonical_root=canonical_root)
    assert meta_normal.phase == LockPhase.HELD


# ============================================================================
# F-22 y F-23 — Cuarta Ola: stale local tras release fallido + deadline de apertura
# ============================================================================


class _CarreraDeAperturaAgotadaError(AssertionError):
    """Sentinel anti-cuelgue: el bucle de apertura atómica no respetó el deadline pactado.

    No es un error del producto: es la guarda que convierte un bucle sin cota (el defecto
    F-23) en un fallo explícito y rápido en vez de un test colgado.
    """


def _fake_open_en_carrera(
    lock_path: pathlib.Path,
    intentos: dict[str, int],
    *,
    limite: int,
    carreras: int | None = None,
) -> Any:
    """Construye un ``open`` que simula la carrera ``x+b -> FileExistsError`` / ``r+b -> FileNotFoundError``.

    - ``carreras=None``: la carrera es PERSISTENTE (nunca se abre el descriptor).
    - ``carreras=N``: tras N llamadas en carrera delega en el ``open`` real, para probar que
      una carrera transitoria se recupera antes del deadline.
    - ``limite``: guarda anti-cuelgue. Si el bucle la supera, el defecto F-23 se manifiesta
      como un fallo explícito en lugar de un test colgado.
    """
    import builtins

    real_open = builtins.open

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if str(file) != str(lock_path):
            return real_open(file, mode, *args, **kwargs)
        intentos["n"] += 1
        if intentos["n"] > limite:
            raise _CarreraDeAperturaAgotadaError(
                f"El bucle de apertura atómica no respetó el deadline ({intentos['n']} intentos sin cota)"
            )
        if carreras is not None and intentos["n"] > carreras:
            return real_open(file, mode, *args, **kwargs)
        if "x" in mode:
            raise FileExistsError(errno.EEXIST, "carrera simulada", str(file))
        raise FileNotFoundError(errno.ENOENT, "carrera simulada", str(file))

    return _open


def test_f22_failed_release_same_process_stale_record_can_be_reacquired(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-22 (P1): un release() fallido deja el registro HELD del PROPIO proceso en disco.

    El proceso dueño limpia sus recursos locales (F-16), pero el registro ``phase=HELD`` con
    su propio ``pid``/``process_create_time`` queda durablemente en disco. Sin F-22 una
    adquisición posterior del MISMO proceso se bloquea a sí misma hasta reiniciarse, porque
    la liveness del dueño es ``ALIVE``.

    El contrato: ``acquire()`` readquiere — sin exigir ``reclaim_orphaned`` — sólo cuando
    puede probar simultáneamente que posee el thread lock per-root, que posee el mutex del
    SO y que no hay handle activo local para ese root. Un dueño VIVO de OTRO proceso sigue
    siendo BUSY (cubierto por F-03b).
    """
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    handle.assert_owned()
    canonical = handle.canonical_root
    lock_path = handle.lock_path
    sesion_previa = handle.session_id

    def _fallo_de_persistencia(*args: Any, **kwargs: Any) -> None:
        raise OSError("fallo simulado de persistencia durante release()")

    # Forzar el fallo en la persistencia del commit point (phase=RELEASED) del release().
    monkeypatch.setattr(rl, "_write_metadata_crash_safe", _fallo_de_persistencia)
    with pytest.raises(OSError):
        handle.release()
    monkeypatch.undo()

    # 1. Recursos locales del dueño limpios: sin handle, sin fd, sin thread lock (F-16 vigente).
    assert handle._released is True
    assert canonical not in rl._ACTIVE_HANDLES
    assert handle._file_obj is None or handle._file_obj.closed is True
    if handle._thread_lock is not None:
        assert not handle._thread_lock.locked()

    # 2. La evidencia en disco sigue siendo el registro HELD del proceso ACTUAL.
    meta_stale = _read_metadata_bytes(lock_path.read_bytes(), expected_canonical_root=canonical)
    assert meta_stale.phase == LockPhase.HELD
    assert meta_stale.pid == os.getpid()
    assert abs(meta_stale.process_create_time - psutil.Process().create_time()) <= 1e-4
    assert meta_stale.session_id == sesion_previa

    # 3. Asimetría deliberada: inspect() NO posee el thread lock per-root, así que no puede
    #    probar que el registro sea stale local. Sigue conservador: INDETERMINATE, nunca FREE.
    inspeccion = inspect_frozen_runtime_root_lock(root)
    assert inspeccion.disposition == LockDisposition.INDETERMINATE
    assert inspeccion.disposition != LockDisposition.FREE
    assert inspeccion.liveness == OwnerLiveness.ALIVE
    assert inspeccion.is_os_locked is False

    # 4. acquire() SÍ puede probarlo ⇒ readquiere sin reclaim_orphaned, con sesión nueva.
    nuevo = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    try:
        nuevo.assert_owned()
        assert nuevo.session_id != sesion_previa
        assert json.loads(lock_path.read_text(encoding="utf-8"))["phase"] == "HELD"
    finally:
        nuevo.release()

    # 5. Sin residuos: un tercer ciclo completo (thread lock y mutex del SO libres de verdad).
    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as tercero:
        tercero.assert_owned()
        assert tercero.session_id != sesion_previa


def test_f22_inspect_keeps_indeterminate_while_acquire_can_reclaim(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-22 (P1): la asimetría inspect()/acquire() sobre el mismo estado es intencional y verificable.

    Congela las DOS mitades: ``inspect()`` declara INDETERMINATE (no puede probar el stale
    local sin el thread lock) mientras ``acquire()`` —que sí lo posee— reclama el residuo.
    Convertir ``INDETERMINATE -> FREE`` en el inspector sólo para simetrizar las APIs sería
    relajar el fail-closed de un observador read-only.
    """
    root = _crear_root_valido(tmp_path)
    handle = acquire_frozen_runtime_root_lock(root, timeout=1.0)
    canonical = handle.canonical_root
    lock_path = handle.lock_path

    # Residuo HELD del propio proceso: se libera a mano el mutex del SO y el fd, dejando la
    # metadata intacta (el estado exacto que deja un release() fallido).
    rl._unlock_fd(handle._file_obj.fileno())
    handle._file_obj.close()
    with rl._THREAD_LOCKS_GUARD:
        rl._ACTIVE_HANDLES.pop(canonical, None)
    handle._thread_lock.release()
    handle._released = True

    meta = _read_metadata_bytes(lock_path.read_bytes(), expected_canonical_root=canonical)
    assert meta.phase == LockPhase.HELD
    assert meta.pid == os.getpid()

    assert inspect_frozen_runtime_root_lock(root).disposition == LockDisposition.INDETERMINATE

    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as nuevo:
        nuevo.assert_owned()


def test_f22_pid_reuse_no_cuenta_como_residuo_del_proceso_actual(tmp_path: pathlib.Path) -> None:
    """F-22 (P1): el residuo local exige PID *y* ``process_create_time``; un PID reutilizado no alcanza.

    Un registro HELD con el PID del proceso actual pero con OTRO ``process_create_time`` lo
    escribió un proceso anterior del SO que ya murió y cuyo PID el SO reasignó. Tratarlo como
    "residuo del propio proceso" sería tomar una identidad ajena por propia: debe clasificar
    como huérfano (dueño muerto) y exigir reconciliación explícita. Es la mitad de la pregunta
    "¿PID reuse sigue protegido por create_time?" que F-22 no puede relajar.
    """
    root = _crear_root_valido(tmp_path)
    lock_path = root / "state" / LOCK_FILE_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    create_time_actual = psutil.Process(os.getpid()).create_time()
    payload_pid_reutilizado = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "canonical_root": os.path.normcase(os.path.abspath(os.fspath(root))),
        "session_id": str(uuid.uuid4()),
        "pid": os.getpid(),
        "process_create_time": create_time_actual + 500.0,
        "acquired_at": time.time(),
        "phase": "HELD",
    }
    lock_path.write_text(json.dumps(payload_pid_reutilizado), encoding="utf-8")

    # Mismo PID, distinto create_time ⇒ dueño MUERTO, no residuo local.
    with pytest.raises(FrozenRuntimeLockOrphanedError):
        acquire_frozen_runtime_root_lock(root, timeout=0.0)

    # Y con reclaim explícito sí se reclama: no queda bloqueado para siempre.
    handle = acquire_frozen_runtime_root_lock(root, timeout=0.0, reclaim_orphaned=True)
    try:
        handle.assert_owned()
        assert handle.session_id != payload_pid_reutilizado["session_id"]
    finally:
        handle.release()


def test_f23a_timeout_cero_carrera_persistente_falla_acotado(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-23-A (P1): con timeout=0 una carrera x+b/r+b persistente no puede reintentar sin fin.

    ``timeout=0`` significa un único intento inmediato: si la apertura encuentra la carrera y
    no obtiene un descriptor estable en el acto, falla acotado con BUSY.
    """
    root = _crear_root_valido(tmp_path)
    _, lock_path = canonical_root_key(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b'{"pre":1}')  # asegura FileExistsError en x+b

    intentos = {"n": 0}
    monkeypatch.setattr(rl, "open", _fake_open_en_carrera(lock_path, intentos, limite=200), raising=False)

    inicio = time.monotonic()
    with pytest.raises(FrozenRuntimeLockBusyError):
        acquire_frozen_runtime_root_lock(root, timeout=0.0)
    assert time.monotonic() - inicio < 2.0, "timeout=0 no puede demorar: falla inmediata"
    # Una sola vuelta de la carrera: x+b y r+b. Sin retry.
    assert intentos["n"] <= 2, f"timeout=0 reintentó la apertura ({intentos['n']} intentos)"


def test_f23b_timeout_positivo_carrera_persistente_termina_en_el_deadline(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-23-B (P1): con timeout>0 una carrera persistente se corta cuando vence el deadline."""
    root = _crear_root_valido(tmp_path)
    _, lock_path = canonical_root_key(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b'{"pre":1}')

    intentos = {"n": 0}
    # El límite alto convierte el bucle sin cota (F-23) en un fallo explícito en vez de un cuelgue.
    monkeypatch.setattr(rl, "open", _fake_open_en_carrera(lock_path, intentos, limite=100_000), raising=False)

    inicio = time.monotonic()
    with pytest.raises(FrozenRuntimeLockBusyError):
        acquire_frozen_runtime_root_lock(root, timeout=0.3)
    transcurrido = time.monotonic() - inicio

    assert transcurrido >= 0.25, f"Devolvió antes del deadline pactado ({transcurrido:.3f}s)"
    assert transcurrido <= 5.0, f"Excedió el deadline pactado ({transcurrido:.3f}s)"
    assert intentos["n"] > 2, "Hubo reintentos acotados, no un fallo en el primer intento"
    # El backoff acotado debe espaciar los reintentos: un busy-spin haría miles de intentos.
    assert intentos["n"] <= 100, f"El reintento no tuvo backoff ({intentos['n']} intentos en {transcurrido:.3f}s)"


def test_f23c_carrera_transitoria_se_recupera_antes_del_deadline(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-23-C (P1): la carrera transitoria sigue siendo recuperable — no se volvió un error permanente."""
    root = _crear_root_valido(tmp_path)
    _, lock_path = canonical_root_key(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    intentos = {"n": 0}
    monkeypatch.setattr(rl, "open", _fake_open_en_carrera(lock_path, intentos, limite=50, carreras=4), raising=False)

    handle = acquire_frozen_runtime_root_lock(root, timeout=2.0)
    llamadas = intentos["n"]
    # Restaurar el open real antes de tocar el handle: assert_owned() sondea el lockfile.
    monkeypatch.undo()

    try:
        handle.assert_owned()
        assert llamadas == 5, f"Se esperaban 4 carreras y una apertura real, hubo {llamadas} llamadas"
    finally:
        handle.release()

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
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import uuid

import pytest

from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeLockAdmissionError,
    FrozenRuntimeLockBusyError,
    FrozenRuntimeLockMetadataError,
    FrozenRuntimeLockOwnershipError,
)
from sky_claw.local.frozen_runtime.root_lock import (
    LOCK_FILE_NAME,
    LOCK_METADATA_FIELDS,
    LOCK_SCHEMA_VERSION,
    FrozenRuntimeRootLockHandle,
    LockDisposition,
    OwnerLiveness,
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
        '{"schema_version": 999, "canonical_root": "x", "session_id": "y", "pid": 1, "process_create_time": 1.0, "acquired_at": 1.0}',  # schema desconocido
        '{"schema_version": 1, "canonical_root": "x", "session_id": "not-a-uuid", "pid": 1, "process_create_time": 1.0, "acquired_at": 1.0}',  # UUID inválido
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


def test_rl18_sustitucion_de_metadata_falla_assert_owned(tmp_path: pathlib.Path) -> None:
    """RL-18: Si la metadata en disco cambia mientras el handle está vivo, assert_owned falla."""
    root = _crear_root_valido(tmp_path)

    with acquire_frozen_runtime_root_lock(root, timeout=1.0) as handle:
        handle.assert_owned()

        # Sustituir la metadata en disco por la de otra sesión
        metadata_adulterada = {
            "schema_version": LOCK_SCHEMA_VERSION,
            "canonical_root": handle.canonical_root,
            "session_id": str(uuid.uuid4()),  # Otra sesión
            "pid": os.getpid(),
            "process_create_time": handle.process_create_time,
            "acquired_at": time.time(),
        }
        handle.lock_path.write_text(json.dumps(metadata_adulterada), encoding="utf-8")

        # Inmediatamente assert_owned debe fallar cerrado
        with pytest.raises(FrozenRuntimeLockOwnershipError):
            handle.assert_owned()


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

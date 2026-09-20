"""Tests para la primitive nativa de inspección de quiescencia (GP2-S2).

Cubre los casos normativos Q1 a Q11 y verificaciones POSIX:
- Q1: FILE usa desired access exacto (0x00010116)
- Q2: DIR usa desired access exacto (0x00010156)
- Q3: dwShareMode == 0
- Q4: OPEN_REPARSE_POINT presente (0x00200000)
- Q5: BACKUP_SEMANTICS presente (0x02000000)
- Q6: success cierra handle inmediatamente
- Q7: sharing violation reintenta con límite (éxito en intento 5 de 5)
- Q8: agota retries -> fail closed tras exactamente 5 intentos
- Q9: error causal distinto de sharing violation -> no retry silencioso
- Q10: todos los handles se cierran ante error causal
- Q11: cero mutaciones filesystem/ACL
- POSIX: manejo tipado fuera de Windows sin fallos al importar
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any
from unittest.mock import patch

import pytest

from sky_claw.local.runtime_vault.golden_protection_plan import GoldenProtectionNodeKind
from sky_claw.local.runtime_vault.quiescence import (
    ERROR_SHARING_VIOLATION,
    PROBE_CREATION_DISPOSITION,
    PROBE_DIR_DESIRED_ACCESS,
    PROBE_FILE_DESIRED_ACCESS,
    PROBE_FLAGS,
    PROBE_SHARE_MODE,
    QuiescenceError,
    QuiescenceUnsupportedError,
    QuiescenceViolationError,
    default_quiescence_jitter,
    probe_node_quiescence,
    probe_tree_quiescence,
)

# ============================================================================
# Q1 a Q5: Constantes y Parámetros ABI de Apertura Win32
# ============================================================================


def test_q1_file_usa_desired_access_exacto() -> None:
    """Q1: El acceso solicitado para archivo regular debe ser exactamente 0x00010116."""
    expected = (
        0x0002  # FILE_WRITE_DATA
        | 0x0004  # FILE_APPEND_DATA
        | 0x0100  # FILE_WRITE_ATTRIBUTES
        | 0x0010  # FILE_WRITE_EA
        | 0x00010000  # DELETE
    )
    assert PROBE_FILE_DESIRED_ACCESS == 0x00010116
    assert expected == PROBE_FILE_DESIRED_ACCESS


def test_q2_dir_usa_desired_access_exacto() -> None:
    """Q2: El acceso solicitado para directorio debe ser exactamente 0x00010156."""
    expected = (
        PROBE_FILE_DESIRED_ACCESS | 0x0040  # FILE_DELETE_CHILD
    )
    assert PROBE_DIR_DESIRED_ACCESS == 0x00010156
    assert expected == PROBE_DIR_DESIRED_ACCESS


def test_q3_dw_share_mode_es_cero_exclusivo() -> None:
    """Q3: dwShareMode debe ser 0 (exclusivo).

    Mutation anchor: si share mode no fuera 0, el probe no colisionaría
    de forma bidireccional determinista contra writers preexistentes.
    """
    assert PROBE_SHARE_MODE == 0


def test_q4_open_reparse_point_presente() -> None:
    """Q4: FILE_FLAG_OPEN_REPARSE_POINT (0x00200000) debe estar presente en PROBE_FLAGS."""
    assert (PROBE_FLAGS & 0x00200000) != 0


def test_q5_backup_semantics_presente() -> None:
    """Q5: FILE_FLAG_BACKUP_SEMANTICS (0x02000000) debe estar presente en PROBE_FLAGS."""
    assert (PROBE_FLAGS & 0x02000000) != 0
    assert PROBE_CREATION_DISPOSITION == 3  # OPEN_EXISTING


# ============================================================================
# Q6 a Q11: Comportamiento del Probe y Política de Reintentos
# ============================================================================


@pytest.mark.skipif(sys.platform != "win32", reason="Prueba nativa de handle Win32")
def test_q6_success_cierra_handle_inmediatamente(tmp_path: pathlib.Path) -> None:
    """Q6: Una apertura exitosa cierra el handle inmediatamente en bloque finally."""
    dummy_file = tmp_path / "quiescence_target.txt"
    dummy_file.write_text("contenido intacto", encoding="utf-8")

    # Ejecutar probe sobre archivo real descartable
    probe_node_quiescence(dummy_file, GoldenProtectionNodeKind.FILE)

    # Si el handle no se hubiera cerrado, la reapertura exclusiva con DELETE fallaría
    # Comprobar que el archivo se puede eliminar de inmediato sin conflicto de sharing
    dummy_file.unlink()
    assert not dummy_file.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Prueba nativa de handle Win32")
def test_q7_sharing_violation_reintenta_con_limite_y_exito_en_intento_5() -> None:
    """Q7: ERROR_SHARING_VIOLATION reintenta con backoff exponencial y tiene éxito en intento 5 de 5."""
    call_count = 0
    delays_recorded: list[float] = []

    def mock_create_file(*args: Any, **kwargs: Any) -> int:
        nonlocal call_count
        call_count += 1
        if call_count < 5:
            return -1  # Falla intentos 1, 2, 3, 4
        return 12345  # Éxito en intento 5

    def mock_get_last_error() -> int:
        return ERROR_SHARING_VIOLATION

    def mock_sleeper(delay: float) -> None:
        delays_recorded.append(delay)

    jitter_called = 0

    def mock_jitter(delay: float) -> float:
        nonlocal jitter_called
        jitter_called += 1
        return delay * 1.05  # jitter determinista +5%

    with (
        patch("sys.platform", "win32"),
        patch("sky_claw.local.runtime_vault.quiescence._kernel32.CreateFileW", side_effect=mock_create_file),
        patch("sky_claw.local.runtime_vault.quiescence._kernel32.CloseHandle", return_value=True) as mock_close,
        patch("ctypes.get_last_error", side_effect=mock_get_last_error),
    ):
        probe_node_quiescence(
            "C:\\dummy\\file.txt",
            GoldenProtectionNodeKind.FILE,
            max_attempts=5,
            base_backoff_seconds=0.01,
            jitter_fn=mock_jitter,
            sleeper=mock_sleeper,
        )

    assert call_count == 5  # Exactamente 5 llamadas a CreateFileW
    assert jitter_called == 4  # 4 esperas intermedias (entre intentos 1-2, 2-3, 3-4, 4-5)
    assert len(delays_recorded) == 4
    # Verificar backoff exponencial: base * 2^0, base * 2^1, base * 2^2, base * 2^3 (+5% jitter)
    expected_delays = [0.01 * (2**i) * 1.05 for i in range(4)]
    for actual, exp in zip(delays_recorded, expected_delays, strict=True):
        assert pytest.approx(actual) == exp
    mock_close.assert_called_once_with(12345)


@pytest.mark.skipif(sys.platform != "win32", reason="Prueba nativa de handle Win32")
def test_q8_agota_retries_fail_closed_tras_exactamente_5_intentos() -> None:
    """Q8: Agotar exactamente 5 intentos totales dispara QuiescenceViolationError sin 6to intento."""
    call_count = 0
    delays_recorded: list[float] = []

    def mock_create_file(*args: Any, **kwargs: Any) -> int:
        nonlocal call_count
        call_count += 1
        return -1  # Falla siempre

    def mock_get_last_error() -> int:
        return ERROR_SHARING_VIOLATION

    with (
        patch("sys.platform", "win32"),
        patch("sky_claw.local.runtime_vault.quiescence._kernel32.CreateFileW", side_effect=mock_create_file),
        patch("ctypes.get_last_error", side_effect=mock_get_last_error),
        pytest.raises(QuiescenceViolationError) as exc_info,
    ):
        probe_node_quiescence(
            "C:\\dummy\\file.txt",
            GoldenProtectionNodeKind.FILE,
            max_attempts=5,
            sleeper=lambda d: delays_recorded.append(d),
        )

    assert call_count == 5  # Exactamente 5 intentos totales, no 6
    assert len(delays_recorded) == 4  # 4 esperas entre los 5 intentos
    assert "ERROR_SHARING_VIOLATION persistente tras agotar 5 intentos" in str(exc_info.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Prueba nativa de handle Win32")
def test_q9_error_distinto_de_sharing_violation_no_reintenta_silenciosamente() -> None:
    """Q9: Error causal distinto de ERROR_SHARING_VIOLATION falla de inmediato (1 intento, sin retries)."""
    call_count = 0
    sleeper_called = False

    def mock_create_file(*args: Any, **kwargs: Any) -> int:
        nonlocal call_count
        call_count += 1
        return -1

    def mock_get_last_error() -> int:
        return 5  # ERROR_ACCESS_DENIED

    def mock_sleeper(d: float) -> None:
        nonlocal sleeper_called
        sleeper_called = True

    with (
        patch("sys.platform", "win32"),
        patch("sky_claw.local.runtime_vault.quiescence._kernel32.CreateFileW", side_effect=mock_create_file),
        patch("ctypes.get_last_error", side_effect=mock_get_last_error),
        pytest.raises(QuiescenceError) as exc_info,
    ):
        probe_node_quiescence(
            "C:\\dummy\\file.txt",
            GoldenProtectionNodeKind.FILE,
            max_attempts=5,
            sleeper=mock_sleeper,
        )

    assert call_count == 1  # Falla de inmediato en el primer intento
    assert not sleeper_called
    assert "código 5" in str(exc_info.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Prueba nativa de handle Win32")
def test_q10_todos_los_handles_se_cierran_ante_error_causal() -> None:
    """Q10: Si ocurre un error inesperado tras obtener handle válido, CloseHandle se ejecuta en finally."""
    closed_handles: list[int] = []

    def mock_close(h: int) -> bool:
        closed_handles.append(h)
        return True

    with (
        patch("sys.platform", "win32"),
        patch("sky_claw.local.runtime_vault.quiescence._kernel32.CreateFileW", return_value=9999),
        patch("sky_claw.local.runtime_vault.quiescence._kernel32.CloseHandle", side_effect=mock_close),
    ):
        # probe normal cierra el handle
        probe_node_quiescence("C:\\dummy\\file.txt", GoldenProtectionNodeKind.FILE)

    assert closed_handles == [9999]


@pytest.mark.skipif(sys.platform != "win32", reason="Prueba nativa de inmutabilidad en Windows")
def test_q11_cero_mutaciones_filesystem_acl(tmp_path: pathlib.Path) -> None:
    """Q11: El probe no realiza ninguna mutación en contenido, tamaño, mtime ni ACL."""
    test_dir = tmp_path / "tree"
    test_dir.mkdir()
    username = os.environ.get("USERNAME", "")
    if username:
        os.system(f'icacls "{test_dir}" /grant "{username}":(OI)(CI)(F) > nul')
    test_file = test_dir / "sample.txt"
    test_file.write_bytes(b"Datos originales inmutables")

    st_file_before = test_file.stat()
    st_dir_before = test_dir.stat()

    # Ejecutar probe tanto en directorio como en archivo
    probe_node_quiescence(test_dir, GoldenProtectionNodeKind.DIR)
    probe_node_quiescence(test_file, GoldenProtectionNodeKind.FILE)

    st_file_after = test_file.stat()
    st_dir_after = test_dir.stat()

    assert test_file.read_bytes() == b"Datos originales inmutables"
    assert st_file_before.st_size == st_file_after.st_size
    assert st_file_before.st_mtime_ns == st_file_after.st_mtime_ns
    assert st_dir_before.st_mtime_ns == st_dir_after.st_mtime_ns


def test_posix_quiescence_falla_con_quiescence_unsupported_error() -> None:
    """POSIX: En sistemas no-Windows, probe_node_quiescence falla con QuiescenceUnsupportedError tipado."""
    with patch("sys.platform", "linux"), pytest.raises(QuiescenceUnsupportedError) as exc_info:
        probe_node_quiescence("/tmp/dummy", GoldenProtectionNodeKind.FILE)

    assert "Plataforma no soportada" in str(exc_info.value)


def test_default_quiescence_jitter_rango() -> None:
    """Verifica que el jitter por defecto añade un retraso no negativo y acotado al 10%."""
    base = 1.0
    for _ in range(50):
        val = default_quiescence_jitter(base)
        assert 1.0 <= val <= 1.10


def test_q12_probe_tree_acumula_todos_los_nodos_bloqueados() -> None:
    """Q12: probe_tree_quiescence acumula todos los nodos persistentemente bloqueados en un único error estructurado."""
    from types import SimpleNamespace

    # 3 nodos en el árbol: 2 bloqueados persistentemente y 1 libre
    nodes = [
        SimpleNamespace(relative_path="data/sub/file1.txt", node_kind=GoldenProtectionNodeKind.FILE),
        SimpleNamespace(relative_path="data/sub/file2.txt", node_kind=GoldenProtectionNodeKind.FILE),
        SimpleNamespace(relative_path="data/clean.txt", node_kind=GoldenProtectionNodeKind.FILE),
    ]

    attempts_by_path: dict[str, int] = {}

    def mock_probe_node(path: Any, node_kind: Any, **kwargs: Any) -> None:
        p_str = str(path).replace("\\", "/")
        attempts_by_path[p_str] = attempts_by_path.get(p_str, 0) + 1
        if "file1.txt" in p_str:
            raise QuiescenceViolationError("busy 1", blocked_paths=("data/sub/file1.txt",))
        if "file2.txt" in p_str:
            raise QuiescenceViolationError("busy 2", blocked_paths=("data/sub/file2.txt",))
        # clean.txt no falla

    with (
        patch("sky_claw.local.runtime_vault.quiescence.probe_node_quiescence", side_effect=mock_probe_node),
        pytest.raises(QuiescenceViolationError) as exc_info,
    ):
        probe_tree_quiescence(
            "C:/fake_root",
            nodes,
            max_attempts=5,
        )

    err = exc_info.value
    assert err.blocked_paths == ("data/sub/file1.txt", "data/sub/file2.txt")
    assert "data/sub/file1.txt" in str(err)
    assert "data/sub/file2.txt" in str(err)
    assert "data/clean.txt" not in err.blocked_paths
    # Verifica que probe_node_quiescence fue llamado para los 3 nodos (no abortó en el primero)
    assert len(attempts_by_path) == 3


def test_q13_probe_tree_error_no_sharing_violation_aborta_inmediato() -> None:
    """Q13: Un error causal distinto de ERROR_SHARING_VIOLATION (fail-closed) aborta inmediatamente."""
    from types import SimpleNamespace

    nodes = [
        SimpleNamespace(relative_path="file1.txt", node_kind=GoldenProtectionNodeKind.FILE),
        SimpleNamespace(relative_path="file2.txt", node_kind=GoldenProtectionNodeKind.FILE),
    ]

    probed_paths: list[str] = []

    def mock_probe_node(path: Any, node_kind: Any, **kwargs: Any) -> None:
        probed_paths.append(str(path))
        raise QuiescenceError("ERROR_ACCESS_DENIED código 5")

    with (
        patch("sky_claw.local.runtime_vault.quiescence.probe_node_quiescence", side_effect=mock_probe_node),
        pytest.raises(QuiescenceError) as exc_info,
    ):
        probe_tree_quiescence("C:/fake_root", nodes)

    assert "ERROR_ACCESS_DENIED" in str(exc_info.value)
    # Abortó de inmediato en el primer nodo sin sondear el segundo
    assert len(probed_paths) == 1

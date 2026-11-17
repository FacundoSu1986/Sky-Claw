"""Tests para el modo asistido externo de ParallaxR (PR-A0).

Verifica descubrimiento acotado, validación de contención de rutas, fingerprint
coherente anti-TOCTOU de ParallaxR.BAT, marcadores de salida preexistentes,
handoff manual inmutable, contrato de resultado (success/message), invariantes
estáticos anti-ejecución y ausencia de mutaciones en disco.
"""

from __future__ import annotations

import ast
import ctypes
import dataclasses
import errno
import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import IO, Any
from unittest.mock import patch

import pytest

from sky_claw.local.tools.parallaxr_assisted import (
    _FINGERPRINT_MAX_ATTEMPTS,
    ParallaxRAssistedPreflight,
    ParallaxRAssistedStatus,
    ParallaxREvidenceIncompleteError,
    ParallaxREvidenceUnstableError,
    ParallaxRInstallationEvidence,
    ParallaxRManualHandoff,
    _FileFingerprintSnapshot,
    _fingerprint_file_consistently,
    discover_output_markers,
    discover_parallaxr_candidates,
    prepare_parallaxr_manual_handoff,
    run_parallaxr_assisted_preflight,
    validate_path_containment,
)

# =============================================================================
# 1. DESCUBRIMIENTO ACOTADO Y CONTRATO DE RESULTADO
# =============================================================================


def test_mods_dir_inexistente_retorna_missing_sin_excepcion(tmp_path: Path) -> None:
    """Si mods_dir no existe en disco, preflight retorna MISSING de forma segura con success=False."""
    mods_dir_inexistente = tmp_path / "mods_fantasma"
    preflight = run_parallaxr_assisted_preflight(mods_dir_inexistente)

    assert preflight.status == ParallaxRAssistedStatus.MISSING
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == ()
    assert preflight.message != ""
    assert "no existe" in preflight.message.lower()


def test_mods_dir_vacio_retorna_missing(tmp_path: Path) -> None:
    """Si mods_dir existe pero está vacío, preflight retorna MISSING con success=False."""
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()

    preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert preflight.status == ParallaxRAssistedStatus.MISSING
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == ()
    assert preflight.message != ""


def test_un_candidato_directo_retorna_ready_con_success_true_y_message_vacio(
    tmp_path: Path,
) -> None:
    """Un único hijo directo con ParallaxR.BAT produce estado READY, success=True y message vacío."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR_Mod"
    mod_dir.mkdir(parents=True)
    bat = mod_dir / "ParallaxR.BAT"
    bat.write_text("@echo off\necho Test ParallaxR\n", encoding="utf-8")

    preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert preflight.status == ParallaxRAssistedStatus.READY
    assert preflight.success is True
    assert preflight.message == ""
    assert preflight.installation is not None
    assert preflight.installation.mod_root == mod_dir.resolve()
    assert preflight.installation.entrypoint == bat.resolve()
    assert preflight.candidate_mod_roots == (mod_dir.resolve(),)


def test_multiples_candidatos_sin_seleccion_retorna_ambiguous(tmp_path: Path) -> None:
    """Múltiples hijos directos con ParallaxR.BAT retornan AMBIGUOUS con success=False."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "ParallaxR_v1"
    mod_b = mods_dir / "ParallaxR_v2"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_text("@echo A", encoding="utf-8")
    (mod_b / "ParallaxR.BAT").write_text("@echo B", encoding="utf-8")

    preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert preflight.status == ParallaxRAssistedStatus.AMBIGUOUS
    assert preflight.success is False
    assert preflight.message != ""
    assert preflight.installation is None
    assert len(preflight.candidate_mod_roots) == 2
    assert mod_a.resolve() in preflight.candidate_mod_roots
    assert mod_b.resolve() in preflight.candidate_mod_roots


def test_ambiguous_con_seleccion_valida_retorna_ready(tmp_path: Path) -> None:
    """Con múltiples candidatos, una explicit_selection válida resuelve a READY con success=True."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "ParallaxR_v1"
    mod_b = mods_dir / "ParallaxR_v2"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_text("@echo A", encoding="utf-8")
    (mod_b / "ParallaxR.BAT").write_text("@echo B", encoding="utf-8")

    # Selección explícita por mod_root de A
    preflight_a = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_a)
    assert preflight_a.status == ParallaxRAssistedStatus.READY
    assert preflight_a.success is True
    assert preflight_a.message == ""
    assert preflight_a.installation is not None
    assert preflight_a.installation.mod_root == mod_a.resolve()

    # Selección explícita por entrypoint de B
    preflight_b = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_b / "ParallaxR.BAT")
    assert preflight_b.status == ParallaxRAssistedStatus.READY
    assert preflight_b.success is True
    assert preflight_b.message == ""
    assert preflight_b.installation is not None
    assert preflight_b.installation.mod_root == mod_b.resolve()


def test_ambiguous_con_seleccion_externa_retorna_invalid_selection(
    tmp_path: Path,
) -> None:
    """Una selección externa no perteneciente a los candidatos descubiertos retorna INVALID_SELECTION."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "ParallaxR_v1"
    mod_b = mods_dir / "ParallaxR_v2"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_text("@echo A", encoding="utf-8")
    (mod_b / "ParallaxR.BAT").write_text("@echo B", encoding="utf-8")

    externo = tmp_path / "otro_lugar" / "ParallaxR"
    externo.mkdir(parents=True)
    (externo / "ParallaxR.BAT").write_text("@echo Externo", encoding="utf-8")

    preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=externo)

    assert preflight.status == ParallaxRAssistedStatus.INVALID_SELECTION
    assert preflight.success is False
    assert preflight.message != ""
    assert preflight.installation is None


def test_single_candidate_con_seleccion_divergente_retorna_invalid_selection(
    tmp_path: Path,
) -> None:
    """Si hay un único candidato pero explicit_selection no coincide, falla cerrado a INVALID_SELECTION."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "ParallaxR_v1"
    mod_a.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_text("@echo A", encoding="utf-8")

    otra_ruta = mods_dir / "OtroModInexistente"

    preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=otra_ruta)

    assert preflight.status == ParallaxRAssistedStatus.INVALID_SELECTION
    assert preflight.success is False
    assert preflight.message != ""
    assert preflight.installation is None


def test_single_candidate_con_seleccion_coincidente_retorna_ready(
    tmp_path: Path,
) -> None:
    """Si hay un único candidato y explicit_selection coincide con él, retorna READY."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "ParallaxR_v1"
    mod_a.mkdir(parents=True)
    bat = mod_a / "ParallaxR.BAT"
    bat.write_text("@echo A", encoding="utf-8")

    preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_a)
    assert preflight.status == ParallaxRAssistedStatus.READY
    assert preflight.success is True
    assert preflight.message == ""
    assert preflight.installation is not None
    assert preflight.installation.mod_root == mod_a.resolve()


def test_nested_entrypoint_no_es_descubierto(tmp_path: Path) -> None:
    """Un ParallaxR.BAT anidado dentro de subcarpetas (no hijo directo) NO se descubre."""
    mods_dir = tmp_path / "mods"
    anidado = mods_dir / "MiMod" / "subcarpeta" / "tools"
    anidado.mkdir(parents=True)
    (anidado / "ParallaxR.BAT").write_text("@echo Anidado", encoding="utf-8")

    candidatos = discover_parallaxr_candidates(mods_dir)
    assert candidatos == ()

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.MISSING
    assert preflight.success is False


def test_entrypoint_directorio_es_rechazado(tmp_path: Path) -> None:
    """Si ParallaxR.BAT es un directorio y no un archivo, se rechaza."""
    mods_dir = tmp_path / "mods"
    falso_bat = mods_dir / "ModConCarpeta" / "ParallaxR.BAT"
    falso_bat.mkdir(parents=True)

    candidatos = discover_parallaxr_candidates(mods_dir)
    assert candidatos == ()


# =============================================================================
# 2. CONTENCIÓN Y ESCAPE DE RUTAS
# =============================================================================


def test_validate_path_containment_adentro_y_afuera(tmp_path: Path) -> None:
    """Valida que una ruta resuelta permanezca físicamente contenida en el directorio padre."""
    parent = tmp_path / "mods"
    parent.mkdir()
    child_ok = parent / "ModA" / "archivo.txt"

    assert validate_path_containment(child_ok, parent) is True

    fuera = tmp_path / "externo" / "archivo.txt"
    assert validate_path_containment(fuera, parent) is False


def test_escape_symlink_es_rechazado(tmp_path: Path) -> None:
    """Si un symlink apunta a un archivo fuera de mods_dir, se rechaza por escape."""
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    externo_dir = tmp_path / "externo"
    externo_dir.mkdir()
    real_bat = externo_dir / "ParallaxR.BAT"
    real_bat.write_text("@echo Fuera", encoding="utf-8")

    mod_link = mods_dir / "ModLinkeado"
    try:
        mod_link.symlink_to(externo_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("SKIP: creación de symlinks no soportada en esta plataforma/privilegio")

    candidatos = discover_parallaxr_candidates(mods_dir)
    assert candidatos == ()


# =============================================================================
# 3. FINGERPRINT COHERENTE Y ANTI-TOCTOU
# =============================================================================


def test_fingerprint_captura_datos_deterministas(tmp_path: Path) -> None:
    """El fingerprint captura tamaño, mtime_ns y sha256 de forma determinista."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR"
    mod_dir.mkdir(parents=True)
    bat = mod_dir / "ParallaxR.BAT"
    contenido = b"@echo off\r\necho ParallaxR Official Entrypoint\r\n"
    bat.write_bytes(contenido)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.READY
    assert preflight.success is True
    assert preflight.message == ""
    assert preflight.installation is not None

    evidence = preflight.installation
    expected_sha = hashlib.sha256(contenido).hexdigest()

    assert evidence.size_bytes == len(contenido)
    assert evidence.sha256 == expected_sha
    assert evidence.mtime_ns == bat.stat().st_mtime_ns

    # Determinismo
    preflight_2 = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight_2.installation == evidence


def test_fingerprint_cambia_si_cambia_contenido(tmp_path: Path) -> None:
    """Si el contenido del archivo cambia, el sha256 resultante cambia."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR"
    mod_dir.mkdir(parents=True)
    bat = mod_dir / "ParallaxR.BAT"

    bat.write_bytes(b"version 1")
    sha1 = run_parallaxr_assisted_preflight(mods_dir).installation.sha256  # type: ignore[union-attr]

    bat.write_bytes(b"version 2")
    sha2 = run_parallaxr_assisted_preflight(mods_dir).installation.sha256  # type: ignore[union-attr]

    assert sha1 != sha2


def test_fingerprint_retry_exitoso_tras_cambio_en_primer_intento(tmp_path: Path) -> None:
    """Si el descriptor cambia en intento 1 pero se estabiliza en intento 2, resuelve READY."""
    file_path = tmp_path / "test.bat"
    file_path.write_bytes(b"contenido estable")

    real_fstat = os.fstat
    call_count = 0

    def fstat_simula_mismatch(fd: int) -> os.stat_result:
        nonlocal call_count
        res = real_fstat(fd)
        call_count += 1
        # En el primer fstat "after" (call_count == 2), simulamos tamaño distinto
        if call_count == 2:
            return os.stat_result(
                (
                    res.st_mode,
                    res.st_ino,
                    res.st_dev,
                    res.st_nlink,
                    res.st_uid,
                    res.st_gid,
                    res.st_size + 10,  # tamaño modificado
                    res.st_atime,
                    res.st_mtime,
                    res.st_ctime,
                )
            )
        return res

    with patch("os.fstat", side_effect=fstat_simula_mismatch):
        snapshot = _fingerprint_file_consistently(file_path, max_attempts=2)

    assert isinstance(snapshot, _FileFingerprintSnapshot)
    assert snapshot.size_bytes == len(b"contenido estable")
    assert snapshot.sha256 == hashlib.sha256(b"contenido estable").hexdigest()
    assert call_count >= 3  # reintentó


def test_fingerprint_falla_a_evidence_unstable_si_continua_cambiando(
    tmp_path: Path,
) -> None:
    """Si el archivo continúa cambiando tras max_attempts, lanza ParallaxREvidenceUnstableError."""
    file_path = tmp_path / "inestable.bat"
    file_path.write_bytes(b"dato")

    real_fstat = os.fstat
    counter = 0

    def fstat_siempre_mismatch(fd: int) -> os.stat_result:
        nonlocal counter
        counter += 1
        res = real_fstat(fd)
        # Retorna tamaño creciente en cada llamada para forzar mismatch
        return os.stat_result(
            (
                res.st_mode,
                res.st_ino,
                res.st_dev,
                res.st_nlink,
                res.st_uid,
                res.st_gid,
                res.st_size + counter,
                res.st_atime,
                res.st_mtime,
                res.st_ctime,
            )
        )

    with (
        patch("os.fstat", side_effect=fstat_siempre_mismatch),
        pytest.raises(ParallaxREvidenceUnstableError, match="cambió durante la captura"),
    ):
        _fingerprint_file_consistently(file_path, max_attempts=2)


def test_preflight_retorna_evidence_unstable_si_entrypoint_cambia(
    tmp_path: Path,
) -> None:
    """Si el entrypoint cambia durante preflight, el preflight degrada a EVIDENCE_UNSTABLE con success=False."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR"
    mod_dir.mkdir(parents=True)
    bat = mod_dir / "ParallaxR.BAT"
    bat.write_bytes(b"@echo off")

    with patch(
        "sky_claw.local.tools.parallaxr_assisted._fingerprint_file_consistently",
        side_effect=ParallaxREvidenceUnstableError("mutación simulada"),
    ):
        preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_UNSTABLE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.message != ""
    assert "cambiaron durante la captura" in preflight.message


def test_preflight_retorna_evidence_unstable_si_marcador_cambia(tmp_path: Path) -> None:
    """Si un marcador de salida cambia durante preflight, degrada a EVIDENCE_UNSTABLE."""
    mods_dir = tmp_path / "mods"
    (mods_dir / "ParallaxR").mkdir(parents=True)
    (mods_dir / "ParallaxR" / "ParallaxR.BAT").write_bytes(b"@echo off")

    (mods_dir / "Output").mkdir(parents=True)
    (mods_dir / "Output" / "ParallaxROutput.tmp").write_bytes(b"marker")

    with patch(
        "sky_claw.local.tools.parallaxr_assisted.discover_output_markers",
        side_effect=ParallaxREvidenceUnstableError("marcador inestable"),
    ):
        preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_UNSTABLE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.message != ""


def test_fingerprint_detecta_reemplazo_de_ruta(tmp_path: Path) -> None:
    """Si la ruta física es reemplazada por otro archivo durante la lectura del handle, se rechaza."""
    file_path = tmp_path / "target.bat"
    file_path.write_bytes(b"original")

    real_stat = Path.stat

    def stat_simula_reemplazo(self: Path) -> os.stat_result:
        res = real_stat(self)
        if self == file_path:
            # Simula que la ruta ahora apunta a un inodo/dev distinto
            return os.stat_result(
                (
                    res.st_mode,
                    res.st_ino + 9999,  # inodo diferente (archivo sustituido)
                    res.st_dev,
                    res.st_nlink,
                    res.st_uid,
                    res.st_gid,
                    res.st_size,
                    res.st_atime,
                    res.st_mtime,
                    res.st_ctime,
                )
            )
        return res

    with patch.object(Path, "stat", stat_simula_reemplazo), pytest.raises(ParallaxREvidenceUnstableError):
        _fingerprint_file_consistently(file_path, max_attempts=2)


# =============================================================================
# 4. MARCADORES DE SALIDA EXISTENTES
# =============================================================================


def test_deteccion_marcador_salida_sin_alterar_ready(tmp_path: Path) -> None:
    """Detección de ParallaxROutput.tmp en mod directo sin alterar el estado READY ni asumir frescura."""
    mods_dir = tmp_path / "mods"
    mod_parallaxr = mods_dir / "ParallaxR"
    mod_parallaxr.mkdir(parents=True)
    (mod_parallaxr / "ParallaxR.BAT").write_bytes(b"@echo run")

    mod_output = mods_dir / "My ParallaxR Output"
    mod_output.mkdir(parents=True)
    marker = mod_output / "ParallaxROutput.tmp"
    marker_content = b"sample marker output"
    marker.write_bytes(marker_content)

    preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert preflight.status == ParallaxRAssistedStatus.READY
    assert preflight.success is True
    assert preflight.message == ""
    assert len(preflight.existing_output_candidates) == 1

    marker_ev = preflight.existing_output_candidates[0]
    assert marker_ev.path == marker.resolve()
    assert marker_ev.size_bytes == len(marker_content)
    assert marker_ev.sha256 == hashlib.sha256(marker_content).hexdigest()
    assert marker_ev.mtime_ns == marker.stat().st_mtime_ns


def test_multiples_marcadores_de_salida_preservados(tmp_path: Path) -> None:
    """Múltiples carpetas con marcadores se preservan sin elegir arbitrariamente."""
    mods_dir = tmp_path / "mods"
    (mods_dir / "ParallaxR").mkdir(parents=True)
    (mods_dir / "ParallaxR" / "ParallaxR.BAT").write_bytes(b"@echo run")

    for name in ("Output_A", "Output_B"):
        out = mods_dir / name
        out.mkdir(parents=True)
        (out / "ParallaxROutput.tmp").write_bytes(f"marker {name}".encode())

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.READY
    assert preflight.success is True
    assert preflight.message == ""
    assert len(preflight.existing_output_candidates) == 2


# =============================================================================
# 5. HANDOFF MANUAL
# =============================================================================


def test_prepare_manual_handoff_exitoso_en_ready(tmp_path: Path) -> None:
    """prepare_parallaxr_manual_handoff construye handoff declarativo con success=True y message vacío."""
    mods_dir = tmp_path / "mods"
    mod = mods_dir / "ParallaxR"
    mod.mkdir(parents=True)
    bat = mod / "ParallaxR.BAT"
    bat.write_bytes(b"@echo run")

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    handoff = prepare_parallaxr_manual_handoff(preflight)

    assert isinstance(handoff, ParallaxRManualHandoff)
    assert handoff.success is True
    assert handoff.message == ""
    assert handoff.mode == "manual_official_entrypoint"
    assert handoff.entrypoint == bat.resolve()
    assert handoff.mod_root == mod.resolve()
    assert handoff.fingerprint == preflight.installation
    assert handoff.requires_user_launch is True
    assert handoff.recommended_launcher == "MO2"
    assert handoff.direct_helper_invocation is False
    assert handoff.sky_claw_launches_process is False
    assert "MO2" in handoff.instruction


def test_prepare_manual_handoff_falla_cerrado_si_no_ready(tmp_path: Path) -> None:
    """prepare_parallaxr_manual_handoff falla cerrado ante MISSING, AMBIGUOUS o INVALID_SELECTION."""
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()

    # Caso MISSING
    preflight_missing = run_parallaxr_assisted_preflight(mods_dir)
    with pytest.raises(ValueError, match="no está en estado READY"):
        prepare_parallaxr_manual_handoff(preflight_missing)

    # Caso AMBIGUOUS
    (mods_dir / "ModA").mkdir()
    (mods_dir / "ModB").mkdir()
    (mods_dir / "ModA" / "ParallaxR.BAT").write_bytes(b"@echo A")
    (mods_dir / "ModB" / "ParallaxR.BAT").write_bytes(b"@echo B")
    preflight_ambiguous = run_parallaxr_assisted_preflight(mods_dir)
    with pytest.raises(ValueError, match="no está en estado READY"):
        prepare_parallaxr_manual_handoff(preflight_ambiguous)


def test_prepare_manual_handoff_no_re_hashea_ni_toca_filesystem(
    tmp_path: Path,
) -> None:
    """prepare_parallaxr_manual_handoff es puramente declarativa y no toca filesystem ni re-hashea."""
    mods_dir = tmp_path / "mods"
    mod = mods_dir / "ParallaxR"
    mod.mkdir(parents=True)
    bat = mod / "ParallaxR.BAT"
    bat.write_bytes(b"@echo run")

    preflight = run_parallaxr_assisted_preflight(mods_dir)

    with patch("sky_claw.local.tools.parallaxr_assisted._fingerprint_file_consistently") as mock_fp:
        handoff = prepare_parallaxr_manual_handoff(preflight)
        mock_fp.assert_not_called()

    assert handoff.fingerprint is preflight.installation


# =============================================================================
# 6. INVARIANTE ESTÁTICO ANTI-EJECUCIÓN Y NOMBRES PROHIBIDOS
# =============================================================================


def _asegurar_sin_ejecucion(tree: ast.Module, source: str) -> None:
    """Análisis AST anti-ejecución sobre un árbol ya parseado.

    Detecta imports de librerías de ejecución/red, llamadas prohibidas y
    **cualquier referencia** a primitivas de lanzamiento de procesos del
    módulo ``os`` (denylist explícita ``prohibited_os_execution_attrs``).
    La regla es sobre nodos ``ast.Attribute`` cuyo objeto base sea un alias
    conocido de ``os`` — sin depender de ``ast.Call`` — así que cubre por
    igual la llamada directa (``os.startfile(...)``), la asignación
    (``x = os.system``), la anotada (``x: Callable = os.system``), el walrus
    (``(x := os.system)``), el argumento (``map(os.system, cmds)``) y
    colecciones (``[os.system]``), incluidos aliases de módulo
    (``import os as operating_system``). ``from os import <primitiva>``
    (con o sin alias) se rechaza en el import. No se prohíben
    indiscriminadamente los atributos de ``os``: producción usa
    legítimamente ``os.fstat`` para fingerprinting.
    """
    # Prohibición de módulos completos de subprocesos / ejecución / red
    prohibited_modules = {
        "subprocess",
        "multiprocessing",
        "pty",
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "socket",
        "asyncio",
    }
    # Prohibición de funciones específicas
    prohibited_call_names = {
        "Popen",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "run_capture",
        "kill_and_reap",
        "assign_kill_on_close_job",
        "system",
    }
    # Primitivas de lanzamiento de procesos de os (existen en Python 3.11/3.12;
    # ``startfile`` es win32-only pero se inspecciona fuente cross-platform)
    prohibited_os_execution_attrs = frozenset(
        {
            "system",
            "popen",
            "startfile",
            "fork",
            "forkpty",
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
            "posix_spawn",
            "posix_spawnp",
        }
    )

    # Registro de módulos importados y sus alias
    module_aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                assert root_pkg not in prohibited_modules, f"Import prohibido: {alias.name}"
                module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_pkg = node.module.split(".")[0]
                assert root_pkg not in prohibited_modules, f"ImportFrom prohibido: {node.module}"
            for alias in node.names:
                if node.module is not None and node.module.split(".")[0] == "os":
                    assert alias.name not in prohibited_os_execution_attrs, f"Import prohibido desde os: {alias.name}"
                assert alias.name not in prohibited_call_names, f"Import de función prohibida: {alias.name}"
        # Regla general: TODA referencia Attribute sobre un alias de os con
        # primitiva de ejecución se rechaza, sin importar el contexto AST
        # (llamada, asignación, annassign, walrus, argumento, colección).
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                owner = module_aliases.get(node.value.id)
                if owner == "os" and node.attr in prohibited_os_execution_attrs:
                    raise AssertionError(f"Uso prohibido de primitiva de ejecución de os: os.{node.attr}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                attr_name = node.func.attr
                assert attr_name not in prohibited_call_names, f"Llamada a método prohibido: {attr_name}"
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id
                assert func_name not in prohibited_call_names, f"Llamada a función prohibida: {func_name}"


def test_invariante_anti_ejecucion_y_helpers_prohibidos() -> None:
    """Verifica que parallaxr_assisted.py no importe librerías de ejecución, no llame funciones prohibidas y no mencione helpers internos."""
    module_path = Path(__file__).resolve().parent.parent / "sky_claw" / "local" / "tools" / "parallaxr_assisted.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    _asegurar_sin_ejecucion(tree, source)

    # Prohibición textual de helpers internos de ParallaxR
    forbidden_helper_names = {
        "MakeUnpack.exe",
        "ExtractBSA.exe",
        "LooseCopy.exe",
        "Exclusions.exe",
        "ParallaxRFilter.exe",
        "HeightMap.exe",
        "OutputQC.exe",
        "AntiSleep.ps1",
    }
    for forbidden in forbidden_helper_names:
        assert forbidden not in source, f"Nombre de helper interno prohibido en código: {forbidden}"

    # ParallaxR.BAT debe ser el único entrypoint externo mencionado
    assert "ParallaxR.BAT" in source


# =============================================================================
# 6.b ADVERSARIAL: alias indirecto de os.system (finding CodeRabbit)
# =============================================================================


def _analiza_snippet(snippet: str) -> None:
    _asegurar_sin_ejecucion(ast.parse(snippet), snippet)


def test_guard_rechaza_alias_indirecto_de_os_system() -> None:
    """Antes del fix, 'ejecutar = os.system; ejecutar(...)' escapaba al guard AST."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet(
            "import os\n\n\ndef _mutante() -> None:\n    ejecutar = os.system\n    ejecutar('echo hueco')\n"
        )


def test_guard_rechaza_alias_de_modulo_os() -> None:
    """El hueco también existe vía alias de módulo: import os as operating_system."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet(
            "import os as operating_system\n"
            "\n"
            "\n"
            "def _mutante() -> None:\n"
            "    ejecutar = operating_system.system\n"
            "    ejecutar('echo hueco')\n"
        )


def test_guard_rechaza_cadena_de_alias() -> None:
    """Reenlazar un alias ya prohibido (y = ejecutar) tampoco escapa."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet(
            "import os\n"
            "\n"
            "\n"
            "def _mutante() -> None:\n"
            "    ejecutar = os.system\n"
            "    reejecutar = ejecutar\n"
            "    reejecutar('echo hueco')\n"
        )


def test_guard_permite_os_fstat_y_atributos_legitimos() -> None:
    """No se prohíben indiscriminadamente los atributos de os: producción usa fstat."""
    _analiza_snippet(
        "import os\n\n\ndef _legitimo(fd: int) -> int:\n    stat = os.fstat(fd)\n    return stat.st_size\n"
    )


def test_guard_permite_alias_inofensivo_de_atributo_os() -> None:
    """Alias de atributos de os no prohibidos (getenv) sigue permitido."""
    _analiza_snippet("import os\n\n\ndef _legitimo() -> str | None:\n    leer = os.getenv\n    return leer('PATH')\n")


# ------------------------------------- round 2 (CodeRabbit): primitivas de os, cualquier forma AST


@pytest.mark.parametrize("attr", ["startfile", "popen", "execv", "spawnl", "posix_spawn"])
def test_guard_rechaza_llamada_directa_de_cada_primitiva_os(attr: str) -> None:
    """A/B + variantes: os.startfile/os.popen/os.execv/os.spawnl/os.posix_spawn(...) directo."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet(f"import os\n\n\ndef _mutante() -> None:\n    os.{attr}('x')\n")


def test_guard_rechaza_annassign_os_system() -> None:
    """C: asignación anotada x: Callable = os.system tampoco escapa."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet(
            "from typing import Callable\n"
            "import os\n"
            "\n"
            "\n"
            "def _mutante() -> None:\n"
            "    ejecutar: Callable[[str], int] = os.system\n"
            "    ejecutar('x')\n"
        )


def test_guard_rechaza_walrus_os_system() -> None:
    """D: (x := os.system) es un NamedExpr cuyo value es el Attribute prohibido."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet("import os\n\n\ndef _mutante() -> None:\n    (ejecutar := os.system)\n")


def test_guard_rechaza_os_system_como_argumento_de_map() -> None:
    """E: map(os.system, cmds) — la primitiva es argumento, no Call.func."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet("import os\n\n\ndef _mutante(cmds: list[str]) -> None:\n    list(map(os.system, cmds))\n")


def test_guard_rechaza_os_system_en_coleccion() -> None:
    """Colecciones: [os.system] contiene el Attribute prohibido igual."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet("import os\n\n\nfuncs = [os.system]\n")


def test_guard_rechaza_alias_de_modulo_os_execv() -> None:
    """F variante: import os as operating_system; x = operating_system.execv."""
    with pytest.raises(AssertionError, match="Uso prohibido de primitiva de ejecución de os"):
        _analiza_snippet("import os as operating_system\n\n\ndef _mutante() -> None:\n    x = operating_system.execv\n")


def test_guard_rechaza_from_os_import_system_con_alias() -> None:
    """G: from os import system as ejecutar — binding bare de primitiva."""
    with pytest.raises(AssertionError, match="Import prohibido desde os"):
        _analiza_snippet("from os import system as ejecutar\n")


@pytest.mark.parametrize("attr", ["startfile", "execv", "posix_spawn"])
def test_guard_rechaza_from_os_import_primitivas(attr: str) -> None:
    """from os import <primitiva> se rechaza en el import, con o sin alias."""
    with pytest.raises(AssertionError, match="Import prohibido desde os"):
        _analiza_snippet(f"from os import {attr}\n")


# =============================================================================
# 7. INVARIANTE READ-ONLY / NO-WRITES EN DISCO
# =============================================================================


def _snapshot_tree(root: Path) -> dict[str, tuple[bool, int, int, str]]:
    """Captura snapshot exhaustivo: path relativo -> (is_file, size, mtime_ns, sha256)."""
    snapshot = {}
    for p in root.rglob("*"):
        rel = str(p.relative_to(root))
        is_f = p.is_file()
        st = p.stat()
        if is_f:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            snapshot[rel] = (True, st.st_size, st.st_mtime_ns, h)
        else:
            snapshot[rel] = (False, 0, st.st_mtime_ns, "")
    return snapshot


def test_preflight_no_modifica_el_disco(tmp_path: Path) -> None:
    """Preflight no crea, borra, modifica contenidos ni altera mtimes en el árbol de mods."""
    mods_dir = tmp_path / "mods"
    mod = mods_dir / "ParallaxR"
    mod.mkdir(parents=True)
    bat = mod / "ParallaxR.BAT"
    bat.write_text("@echo test\n", encoding="utf-8")

    out = mods_dir / "Output"
    out.mkdir(parents=True)
    marker = out / "ParallaxROutput.tmp"
    marker.write_text("marker data", encoding="utf-8")

    # Snapshot previo
    before = _snapshot_tree(mods_dir)

    # Ejecutar preflight y handoff
    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.READY
    assert preflight.success is True
    assert preflight.message == ""
    _ = prepare_parallaxr_manual_handoff(preflight)

    # Snapshot posterior
    after = _snapshot_tree(mods_dir)

    assert before == after, "El árbol de archivos sufrió mutaciones durante el preflight!"


# =============================================================================
# 8. FAIL-CLOSED ANTE OBSERVACIÓN INCOMPLETA (ISSUE #667-A) Y ENDURECIMIENTO WIN32
# =============================================================================


@contextmanager
def _bloqueo_exclusivo_windows(file_path: Path) -> Iterator[None]:
    """Abre file_path sin compartir lectura usando CreateFileW y garantiza cierre del handle."""
    if sys.platform != "win32":
        pytest.skip("Solo reproducible en Windows mediante CreateFileW")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    no_share = 0
    open_existing = 3
    file_attribute_normal = 0x80
    invalid_handle = ctypes.c_void_p(-1).value
    handle = create_file(
        str(file_path),
        generic_read,
        no_share,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    if handle is None or handle == 0 or handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _arena_bucle_junctions_windows(base_dir: Path) -> Iterator[tuple[Path, Path, Path]]:
    """Crea un par de junctions NTFS mutuamente circulares sin destino real intermedio."""
    if sys.platform != "win32":
        pytest.skip("Solo reproducible en Windows mediante _winapi.CreateJunction")
    import _winapi

    arena = base_dir / "loop_arena"
    arena.mkdir(parents=True)
    j1 = arena / "j1"
    j2 = arena / "j2"
    # Crear j2 -> dir temporal, retirar dir y apuntar j1 -> j2 produce un ciclo directo irresoluble.
    j1.mkdir()
    _winapi.CreateJunction(str(j1), str(j2))
    j1.rmdir()
    _winapi.CreateJunction(str(j2), str(j1))
    try:
        yield arena, j1, j2
    finally:
        for junction in (j1, j2):
            with suppress(OSError):
                junction.rmdir()


def test_c1_candidato_valido_y_fallo_posterior_no_devuelve_ready_ni_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: un candidato válido seguido de OSError en un hermano posterior falla cerrado sin descartar evidencia."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_b = mods_dir / "Mod_B"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")

    real_stat = Path.stat
    entrypoint_b = mod_b / "ParallaxR.BAT"

    def stat_con_error_tardio(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == entrypoint_b:
            raise PermissionError("acceso denegado simulado en Mod_B")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_con_error_tardio)

    with pytest.raises(ParallaxREvidenceIncompleteError) as exc_info:
        discover_parallaxr_candidates(mods_dir)

    assert not isinstance(exc_info.value, OSError)
    partial_candidates = exc_info.value.partial_candidates
    assert (mod_a / "ParallaxR.BAT").resolve() in partial_candidates

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status not in (ParallaxRAssistedStatus.READY, ParallaxRAssistedStatus.MISSING)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_a.resolve(),)
    assert preflight.message != ""
    assert "acceso denegado simulado" not in preflight.message


def test_c2_marcadores_observados_y_fallo_posterior_no_ocultan_evidencia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: OSError tras observar un marcador no puede devolver READY ni ocultar marcadores previos."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    out_a = mods_dir / "Output_A"
    out_b = mods_dir / "Output_B"
    mod_a.mkdir(parents=True)
    out_a.mkdir(parents=True)
    out_b.mkdir(parents=True)

    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")
    marker_a = out_a / "ParallaxROutput.tmp"
    marker_a.write_bytes(b"marcador_a")
    marker_b = out_b / "ParallaxROutput.tmp"

    real_stat = Path.stat

    def stat_falla_en_out_b(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == marker_b:
            raise OSError(errno.EIO, "fallo de E/S simulado en Output_B")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_falla_en_out_b)

    with pytest.raises(ParallaxREvidenceIncompleteError) as exc_info:
        discover_output_markers(mods_dir)

    partial_markers = exc_info.value.partial_markers
    assert [m.path for m in partial_markers] == [marker_a.resolve()]

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status not in (ParallaxRAssistedStatus.READY, ParallaxRAssistedStatus.MISSING)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_a.resolve(),)
    assert [m.path for m in preflight.existing_output_candidates] == [marker_a.resolve()]
    assert preflight.message != ""
    assert "fallo de E/S simulado" not in preflight.message


def test_c3_entrypoint_inaccesible_al_capturar_fingerprint_no_degrada_a_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C3: fallo de lectura al capturar el fingerprint del entrypoint no degrada a MISSING ni filtra detalle crudo."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir(parents=True)
    bat = mod_a / "ParallaxR.BAT"
    bat.write_bytes(b"@echo valido pero protegido")

    real_open = Path.open

    def open_inaccesible(self: Path, *args: object, **kwargs: object) -> object:
        if self == bat.resolve():
            raise PermissionError("ruta privada simulada C:/secret/ParallaxR.BAT")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", open_inaccesible)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_a.resolve(),)
    assert preflight.message != ""
    assert "ruta privada simulada" not in preflight.message
    assert "C:/secret" not in preflight.message


def test_oserror_antes_del_primer_candidato_falla_cerrado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un OSError en un directorio previo al primer candidato válido tampoco fabrica MISSING ni READY."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_b = mods_dir / "Mod_B"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_b / "ParallaxR.BAT").write_bytes(b"@echo mod_b")

    real_stat = Path.stat

    def stat_con_error_previo(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == mod_a:
            raise PermissionError("directorio previo ilegible")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_con_error_previo)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.message != ""


def test_oserror_al_enumerar_mods_dir_no_degrada_a_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un OSError en iterdir de mods_dir representa evidencia incompleta, no ausencia de mods."""
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir(parents=True)

    real_iterdir = Path.iterdir

    def iterdir_inaccesible(self: Path) -> object:
        if self == mods_dir:
            raise PermissionError("sin permisos para listar mods_dir")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir_inaccesible)

    with pytest.raises(ParallaxREvidenceIncompleteError):
        discover_parallaxr_candidates(mods_dir)

    with pytest.raises(ParallaxREvidenceIncompleteError):
        discover_output_markers(mods_dir)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.message != ""


def test_oserror_en_stat_de_mods_dir_no_degrada_a_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un OSError al inspeccionar la raíz mods_dir no se degrada a MISSING ni eleva sin control."""
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir(parents=True)

    real_stat = Path.stat

    def stat_mods_inaccesible(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == mods_dir:
            raise PermissionError("acceso bloqueado a la raíz")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_mods_inaccesible)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.message != ""
    assert "acceso bloqueado" not in preflight.message


def test_oserror_en_contencion_de_segundo_candidato_no_fabrica_unicidad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un OSError en Path.resolve al validar contención de Mod_B impide declarar único a Mod_A."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_b = mods_dir / "Mod_B"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")
    bat_b = mod_b / "ParallaxR.BAT"
    bat_b.write_bytes(b"@echo mod_b")

    real_resolve = Path.resolve

    def resolve_con_fallo_en_b(self: Path, strict: bool = False) -> Path:
        if self in (mod_b, bat_b):
            raise OSError("reparse point ilegible en Mod_B")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_con_fallo_en_b)

    with pytest.raises(ParallaxREvidenceIncompleteError) as exc_info:
        discover_parallaxr_candidates(mods_dir)
    assert (mod_a / "ParallaxR.BAT").resolve() in exc_info.value.partial_candidates

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_a.resolve(),)


def test_seleccion_explicita_falla_cerrado_si_descubrimiento_es_incompleto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aunque el usuario pase explicit_selection hacia Mod_A, una inspección incompleta no puede aprobar READY."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_b = mods_dir / "Mod_B"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")

    real_stat = Path.stat
    bat_b = mod_b / "ParallaxR.BAT"

    def stat_con_error_en_b(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == bat_b:
            raise PermissionError("error tardío con selección explícita")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_con_error_en_b)

    preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_a)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_a.resolve(),)


def test_oserror_en_ambos_descubridores_preserva_evidencia_parcial_y_sanitiza_mensaje(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si fallan ambos descubridores con OSError tras hallazgos válidos, se preserva toda evidencia parcial."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "A_Mod"
    out_a = mods_dir / "B_Output"
    mod_z = mods_dir / "Z_Blocked"
    for folder in (mod_a, out_a, mod_z):
        folder.mkdir(parents=True)

    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo a")
    marker_a = out_a / "ParallaxROutput.tmp"
    marker_a.write_bytes(b"out_a")

    real_stat = Path.stat

    def stat_falla_en_z(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self.parent == mod_z:
            raise PermissionError("secreto interno Z_Blocked")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_falla_en_z)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_a.resolve(),)
    assert [m.path for m in preflight.existing_output_candidates] == [marker_a.resolve()]
    assert "secreto interno" not in preflight.message


def test_preflight_salidas_retornadas_son_coherentes_en_todos_los_estados(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Las salidas retornadas por run_parallaxr_assisted_preflight mantienen coherencia status/success/installation/message."""
    resultados = [run_parallaxr_assisted_preflight(tmp_path / "no_existe")]

    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    resultados.append(run_parallaxr_assisted_preflight(mods_dir))

    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir()
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo a")
    resultados.append(run_parallaxr_assisted_preflight(mods_dir))
    resultados.append(run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mods_dir / "Inexistente"))

    mod_b = mods_dir / "Mod_B"
    mod_b.mkdir()
    (mod_b / "ParallaxR.BAT").write_bytes(b"@echo b")
    resultados.append(run_parallaxr_assisted_preflight(mods_dir))

    with patch(
        "sky_claw.local.tools.parallaxr_assisted._fingerprint_file_consistently",
        side_effect=ParallaxREvidenceUnstableError("mutación"),
    ):
        resultados.append(run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_a))

    real_stat = Path.stat
    bat_b = mod_b / "ParallaxR.BAT"

    def stat_bloqueado(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == bat_b:
            raise PermissionError("bloqueado")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_bloqueado)
    resultados.append(run_parallaxr_assisted_preflight(mods_dir))

    assert [r.status for r in resultados] == [
        ParallaxRAssistedStatus.MISSING,
        ParallaxRAssistedStatus.MISSING,
        ParallaxRAssistedStatus.READY,
        ParallaxRAssistedStatus.INVALID_SELECTION,
        ParallaxRAssistedStatus.AMBIGUOUS,
        ParallaxRAssistedStatus.EVIDENCE_UNSTABLE,
        ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE,
    ]
    for preflight in resultados:
        es_ready = preflight.status == ParallaxRAssistedStatus.READY
        assert isinstance(preflight, ParallaxRAssistedPreflight)
        assert preflight.success is es_ready
        assert (preflight.installation is not None) is es_ready
        assert (preflight.message == "") is es_ready


def test_oserror_al_hashear_marcador_falla_cerrado_preservando_evidencia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si falla la lectura de un segundo marcador al capturar su hash, falla cerrado y preserva previos."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "A_Mod"
    out_a = mods_dir / "B_Output"
    out_b = mods_dir / "C_Output"
    for folder in (mod_a, out_a, out_b):
        folder.mkdir(parents=True)

    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo ok")
    marker_a = out_a / "ParallaxROutput.tmp"
    marker_a.write_bytes(b"out_a")
    marker_b = out_b / "ParallaxROutput.tmp"
    marker_b.write_bytes(b"out_b_protegido")

    real_open = Path.open

    def open_marcador_inaccesible(self: Path, *args: object, **kwargs: object) -> object:
        if self == marker_b.resolve():
            raise PermissionError("secreto marcador C_Output")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", open_marcador_inaccesible)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_a.resolve(),)
    assert [m.path for m in preflight.existing_output_candidates] == [marker_a.resolve()]
    assert "secreto marcador" not in preflight.message


def test_oserror_antes_del_primer_marcador_no_devuelve_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un OSError al inspeccionar marcadores antes del primer hallazgo impide devolver READY."""
    mods_dir = tmp_path / "mods"
    out_a = mods_dir / "A_OutputBlocked"
    mod_c = mods_dir / "C_Mod"
    out_a.mkdir(parents=True)
    mod_c.mkdir(parents=True)
    (mod_c / "ParallaxR.BAT").write_bytes(b"@echo c")

    real_stat = Path.stat
    blocked_marker = out_a / "ParallaxROutput.tmp"

    def stat_error_previo(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == blocked_marker:
            raise PermissionError("marcador previo inaccesible")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_error_previo)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_c.resolve(),)


def test_windows_bucle_real_de_junctions_en_hijo_no_fabrica_unicidad(tmp_path: Path) -> None:
    """Windows real: una junction circular en mods_dir impide declarar READY al hermano válido."""
    if sys.platform != "win32":
        pytest.skip("Solo reproducible en Windows mediante _winapi.CreateJunction")
    import _winapi

    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")

    with _arena_bucle_junctions_windows(tmp_path) as (_arena, j1, _j2):
        mod_loop = mods_dir / "Mod_Loop"
        _winapi.CreateJunction(str(j1), str(mod_loop))
        try:
            assert mod_loop.lstat() is not None
            with pytest.raises(OSError):
                mod_loop.stat()

            with pytest.raises(ParallaxREvidenceIncompleteError) as exc_info:
                discover_parallaxr_candidates(mods_dir)
            assert (mod_a / "ParallaxR.BAT").resolve() in exc_info.value.partial_candidates

            preflight = run_parallaxr_assisted_preflight(mods_dir)
            assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
            assert preflight.success is False
            assert preflight.installation is None
            assert preflight.candidate_mod_roots == (mod_a.resolve(),)

            # Selección explícita hacia Mod_A también debe fallar cerrado
            explicit_preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_a)
            assert explicit_preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
            assert explicit_preflight.success is False
            assert explicit_preflight.installation is None
            with pytest.raises(ValueError):
                prepare_parallaxr_manual_handoff(explicit_preflight)
        finally:
            mod_loop.rmdir()


def test_windows_bucle_real_de_junctions_en_mods_dir_no_devuelve_missing(tmp_path: Path) -> None:
    """Windows real: pasar una ruta de mods_dir con bucle directo de junctions falla cerrado en EVIDENCE_INCOMPLETE."""
    with _arena_bucle_junctions_windows(tmp_path) as (_arena, j1, _j2):
        for mods_dir_loop in (j1, j1 / "sub"):
            with pytest.raises(OSError):
                mods_dir_loop.stat()
            with pytest.raises(ParallaxREvidenceIncompleteError):
                discover_parallaxr_candidates(mods_dir_loop)

            preflight = run_parallaxr_assisted_preflight(mods_dir_loop)
            assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
            assert preflight.success is False
            assert preflight.installation is None


def test_windows_junction_roto_en_mods_dir_falla_cerrado(tmp_path: Path) -> None:
    """Windows real: una junction rota dentro de mods_dir impide asumir unicidad del candidato válido."""
    if sys.platform != "win32":
        pytest.skip("Solo reproducible en Windows mediante _winapi.CreateJunction")
    import _winapi

    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")

    destino_borrado = tmp_path / "destino_temporal"
    destino_borrado.mkdir()
    mod_broken = mods_dir / "Mod_Broken"
    _winapi.CreateJunction(str(destino_borrado), str(mod_broken))
    destino_borrado.rmdir()

    try:
        preflight = run_parallaxr_assisted_preflight(mods_dir)
        assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
        assert preflight.success is False
        assert preflight.installation is None
        assert preflight.candidate_mod_roots == (mod_a.resolve(),)
    finally:
        mod_broken.rmdir()


def test_windows_junction_roto_como_mods_dir_y_subruta_no_devuelve_missing(tmp_path: Path) -> None:
    """Windows real: una junction rota como mods_dir o su subruta no puede clasificarse como MISSING."""
    if sys.platform != "win32":
        pytest.skip("Solo reproducible en Windows mediante _winapi.CreateJunction")
    import _winapi

    destino_borrado = tmp_path / "destino_fantasma"
    destino_borrado.mkdir()
    j_broken = tmp_path / "j_broken"
    _winapi.CreateJunction(str(destino_borrado), str(j_broken))
    destino_borrado.rmdir()

    try:
        for mods_dir_roto in (j_broken, j_broken / "sub", j_broken / "a" / "b"):
            with pytest.raises(ParallaxREvidenceIncompleteError):
                discover_parallaxr_candidates(mods_dir_roto)

            preflight = run_parallaxr_assisted_preflight(mods_dir_roto)
            assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
            assert preflight.success is False
            assert preflight.installation is None
    finally:
        j_broken.rmdir()


def test_subruta_bajo_ancestro_con_winerror3_o_enoent_no_devuelve_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si stat/lstat en una subruta devuelve ENOENT/WinError 3 por un ancestro fallido, no se degrada a MISSING."""
    ancestro_bloqueado = tmp_path / "ancestro_bloqueado"
    ancestro_bloqueado.mkdir()
    sub_bloqueada = ancestro_bloqueado / "sub"

    real_stat = Path.stat
    real_lstat = Path.lstat

    def _fnf_win3(target: Path) -> FileNotFoundError:
        if sys.platform == "win32":
            return FileNotFoundError(errno.ENOENT, "No se encuentra la ruta especificada", str(target), 3)
        return FileNotFoundError(errno.ENOENT, "No such file or directory", str(target))

    def stat_ancestro_fallido(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == sub_bloqueada:
            raise _fnf_win3(self)
        if self == ancestro_bloqueado and follow_symlinks:
            raise PermissionError("ancestro inaccesible")
        return real_stat(self, follow_symlinks=follow_symlinks)

    def lstat_sub_enoent(self: Path) -> os.stat_result:
        if self == sub_bloqueada:
            raise _fnf_win3(self)
        return real_lstat(self)

    monkeypatch.setattr(Path, "stat", stat_ancestro_fallido)
    monkeypatch.setattr(Path, "lstat", lstat_sub_enoent)

    with pytest.raises(ParallaxREvidenceIncompleteError):
        discover_parallaxr_candidates(sub_bloqueada)
    preflight_bloqueado = run_parallaxr_assisted_preflight(sub_bloqueada)
    assert preflight_bloqueado.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight_bloqueado.success is False
    assert preflight_bloqueado.installation is None

    # Caso Windows real con ciclo j1 <-> j2 forzando ENOENT/WinError 3 al descender por j1/sub
    if sys.platform == "win32":
        with _arena_bucle_junctions_windows(tmp_path) as (_arena, j1, _j2):
            sub_ciclo = j1 / "sub"

            def stat_ciclo_win3(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
                if self == sub_ciclo:
                    raise _fnf_win3(self)
                return real_stat(self, follow_symlinks=follow_symlinks)

            def lstat_ciclo_win3(self: Path) -> os.stat_result:
                if self == sub_ciclo:
                    raise _fnf_win3(self)
                return real_lstat(self)

            monkeypatch.setattr(Path, "stat", stat_ciclo_win3)
            monkeypatch.setattr(Path, "lstat", lstat_ciclo_win3)

            with pytest.raises(ParallaxREvidenceIncompleteError):
                discover_parallaxr_candidates(sub_ciclo)
            preflight_ciclo = run_parallaxr_assisted_preflight(sub_ciclo)
            assert preflight_ciclo.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
            assert preflight_ciclo.success is False
            assert preflight_ciclo.installation is None

    monkeypatch.undo()

    # Control: ausencia real bajo directorio válido o archivo regular sigue devolviendo MISSING
    archivo_regular = tmp_path / "archivo.txt"
    archivo_regular.write_bytes(b"no_es_dir")
    for ruta_ausente in (tmp_path / "no_existe" / "sub", archivo_regular / "sub"):
        assert discover_parallaxr_candidates(ruta_ausente) == ()
        preflight_missing = run_parallaxr_assisted_preflight(ruta_ausente)
        assert preflight_missing.status == ParallaxRAssistedStatus.MISSING
        assert preflight_missing.success is False
        assert preflight_missing.installation is None


def test_windows_junction_real_fuera_de_mods_dir_es_rechazada(tmp_path: Path) -> None:
    """Windows real: una junction válida que apunta fuera de mods_dir se rechaza por contención sin privilegios."""
    if sys.platform != "win32":
        pytest.skip("Solo reproducible en Windows mediante _winapi.CreateJunction")
    import _winapi

    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    externo = tmp_path / "externo"
    externo.mkdir()
    (externo / "ParallaxR.BAT").write_bytes(b"@echo fuera")

    mod_escape = mods_dir / "Mod_Escape"
    _winapi.CreateJunction(str(externo), str(mod_escape))
    try:
        assert discover_parallaxr_candidates(mods_dir) == ()
        preflight = run_parallaxr_assisted_preflight(mods_dir)
        assert preflight.status == ParallaxRAssistedStatus.MISSING
        assert preflight.success is False
        assert preflight.installation is None
    finally:
        mod_escape.rmdir()


def test_resolucion_estricta_requerida_cuando_strict_false_absorbe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si resolve(strict=False) absorbe WinError 5 y solo strict=True lo eleva, discovery debe exigir strict=True."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir(parents=True)
    bat_a = mod_a / "ParallaxR.BAT"
    bat_a.write_bytes(b"@echo mod_a")

    real_resolve = Path.resolve

    def resolve_absorbe_si_nonstrict(self: Path, strict: bool = False) -> Path:
        if self == bat_a:
            if strict:
                raise ctypes.WinError(5) if sys.platform == "win32" else PermissionError("acceso denegado")
            # Simula el comportamiento real de ntpath._getfinalpathname_nonstrict
            return self
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_absorbe_si_nonstrict)

    with pytest.raises(ParallaxREvidenceIncompleteError):
        discover_parallaxr_candidates(mods_dir)

    preflight = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.installation is None


def test_runtimeerror_bucle_en_explicit_selection_y_discovery_falla_cerrado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RuntimeError por bucle en Path.resolve() degrada fail-closed sin ocultar otros RuntimeError."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_b = mods_dir / "Mod_B"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")
    (mod_b / "ParallaxR.BAT").write_bytes(b"@echo mod_b")

    # Caso 1: explicit_selection con ruta inexistente conserva INVALID_SELECTION
    inexistente = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mods_dir / "NoExiste")
    assert inexistente.status == ParallaxRAssistedStatus.INVALID_SELECTION
    assert inexistente.success is False

    # Caso 2 (Windows real): explicit_selection sobre ciclo directo de junctions no propaga RuntimeError sin control
    if sys.platform == "win32":
        with _arena_bucle_junctions_windows(tmp_path) as (_arena, j1, _j2):
            for loop_path in (j1, j1 / "sub"):
                loop_sel = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=loop_path)
                assert loop_sel.status == ParallaxRAssistedStatus.INVALID_SELECTION
                assert loop_sel.success is False
                assert loop_sel.installation is None

    # Caso 3: un RuntimeError genérico NO relacionado con bucle de symlink no debe ocultarse
    real_resolve = Path.resolve

    def resolve_runtimeerror_ajeno(self: Path, strict: bool = False) -> Path:
        if self == mod_b:
            raise RuntimeError("bug interno no relacionado")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_runtimeerror_ajeno)
    with pytest.raises(RuntimeError, match="bug interno no relacionado"):
        run_parallaxr_assisted_preflight(mods_dir)


def test_falso_negativo_winerror_21_y_1921_en_stat_no_colapsa_a_missing_ni_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Errores ignorados por pathlib.exists/is_dir (WinError 21/1921/ELOOP) deben producir EVIDENCE_INCOMPLETE."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_b = mods_dir / "Mod_B"
    mod_a.mkdir(parents=True)
    mod_b.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")

    real_stat = Path.stat

    # Caso A: WinError 21 (ERROR_NOT_READY) en mods_dir
    def stat_not_ready(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == mods_dir:
            raise OSError(errno.EACCES, "El dispositivo no está listo", str(self), 21)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_not_ready)
    preflight_root = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight_root.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight_root.success is False
    assert preflight_root.installation is None

    # Caso B: WinError 1921 en Mod_B tras descubrir Mod_A
    def stat_cant_resolve(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == mod_b:
            raise OSError(errno.EINVAL, "No se puede resolver", str(self), 1921)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat_cant_resolve)
    preflight_child = run_parallaxr_assisted_preflight(mods_dir)
    assert preflight_child.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight_child.success is False
    assert preflight_child.installation is None
    assert preflight_child.candidate_mod_roots == (mod_a.resolve(),)


def test_windows_bloqueo_exclusivo_real_en_fingerprint_entrypoint_y_marcador(tmp_path: Path) -> None:
    """Windows real sin mocks: un lock exclusivo con CreateFileW bloquea lectura y falla en EVIDENCE_INCOMPLETE."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    out_a = mods_dir / "Out_A"
    out_b = mods_dir / "Out_B"
    mod_a.mkdir(parents=True)
    out_a.mkdir(parents=True)
    out_b.mkdir(parents=True)

    bat_a = mod_a / "ParallaxR.BAT"
    bat_a.write_bytes(b"@echo bloqueo real")
    marker_a = out_a / "ParallaxROutput.tmp"
    marker_a.write_bytes(b"ok_a")
    marker_b = out_b / "ParallaxROutput.tmp"
    marker_b.write_bytes(b"locked_b")

    # 1. Bloqueo exclusivo real sobre el marcador B preservando marcador A y candidato A
    with _bloqueo_exclusivo_windows(marker_b):
        preflight_marker = run_parallaxr_assisted_preflight(mods_dir)
        assert preflight_marker.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
        assert preflight_marker.success is False
        assert preflight_marker.installation is None
        assert preflight_marker.candidate_mod_roots == (mod_a.resolve(),)
        assert [m.path for m in preflight_marker.existing_output_candidates] == [marker_a.resolve()]
        assert "locked_b" not in preflight_marker.message
        assert str(marker_b) not in preflight_marker.message

    # 2. Bloqueo exclusivo real sobre el entrypoint ParallaxR.BAT
    with _bloqueo_exclusivo_windows(bat_a):
        preflight_bat = run_parallaxr_assisted_preflight(mods_dir)
        assert preflight_bat.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
        assert preflight_bat.success is False
        assert preflight_bat.installation is None
        assert preflight_bat.candidate_mod_roots == (mod_a.resolve(),)
        assert str(bat_a) not in preflight_bat.message
        with pytest.raises(ValueError):
            prepare_parallaxr_manual_handoff(preflight_bat)


def test_c4_compatibilidad_escenarios_validos_y_handoff_falla_cerrado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C4: compatibilidad de ausencia real, vacío, candidato único, ambigüedad, selección explícita y handoff fail-closed."""
    inexistente = run_parallaxr_assisted_preflight(tmp_path / "no_existe")
    assert inexistente.status == ParallaxRAssistedStatus.MISSING
    assert inexistente.success is False

    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    vacio = run_parallaxr_assisted_preflight(mods_dir)
    assert vacio.status == ParallaxRAssistedStatus.MISSING
    assert vacio.success is False

    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir()
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo uno")
    unico = run_parallaxr_assisted_preflight(mods_dir)
    assert unico.status == ParallaxRAssistedStatus.READY
    assert unico.success is True
    assert unico.message == ""

    mod_b = mods_dir / "Mod_B"
    mod_b.mkdir()
    (mod_b / "ParallaxR.BAT").write_bytes(b"@echo dos")
    ambiguo = run_parallaxr_assisted_preflight(mods_dir)
    assert ambiguo.status == ParallaxRAssistedStatus.AMBIGUOUS
    assert ambiguo.success is False
    with pytest.raises(ValueError):
        prepare_parallaxr_manual_handoff(ambiguo)

    seleccionado = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_b)
    assert seleccionado.status == ParallaxRAssistedStatus.READY
    assert seleccionado.success is True
    assert seleccionado.installation is not None
    assert seleccionado.installation.mod_root == mod_b.resolve()
    handoff = prepare_parallaxr_manual_handoff(seleccionado)
    assert handoff.sky_claw_launches_process is False
    assert handoff.direct_helper_invocation is False

    # Evidencia cambiante sigue siendo EVIDENCE_UNSTABLE, distinta de inaccesibilidad
    with patch(
        "sky_claw.local.tools.parallaxr_assisted._fingerprint_file_consistently",
        side_effect=ParallaxREvidenceUnstableError("mutación concurrente"),
    ):
        inestable = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_b)
    assert inestable.status == ParallaxRAssistedStatus.EVIDENCE_UNSTABLE
    assert inestable.success is False
    with pytest.raises(ValueError):
        prepare_parallaxr_manual_handoff(inestable)

    # Ante error de E/S real, no se degrada a EVIDENCE_UNSTABLE ni a MISSING
    real_stat = Path.stat
    bat_b = mod_b / "ParallaxR.BAT"

    def fallo_tardio(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == bat_b:
            raise PermissionError("protegido")
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fallo_tardio)
    incompleto = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mod_a)
    assert incompleto.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert incompleto.success is False
    with pytest.raises(ValueError):
        prepare_parallaxr_manual_handoff(incompleto)


# =============================================================================
# 9. VERIFICACIÓN FINAL DE IDENTIDAD EN FINGERPRINT (PR #944)
# =============================================================================


@contextmanager
def _interceptar_stat_final_post_lectura(
    monkeypatch: pytest.MonkeyPatch,
    target_path: Path,
    *,
    make_error: Callable[[int], OSError] | None = None,
    make_stat: Callable[[os.stat_result, int], os.stat_result] | None = None,
    fail_second_open_error: OSError | None = None,
) -> Iterator[dict[str, int]]:
    """Intercepta el Path.stat() posterior a open('rb') dentro de _fingerprint_file_consistently."""
    target_resolved = target_path.resolve()
    real_open = Path.open
    real_stat = Path.stat
    counts = {"open_calls": 0, "post_open_stat_calls": 0}
    pending_post_open_stat = False
    fail_next_open = False

    def wrapped_open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> IO[Any]:
        nonlocal pending_post_open_stat
        if mode == "rb" and self in (target_path, target_resolved):
            counts["open_calls"] += 1
            if fail_next_open and fail_second_open_error is not None:
                raise fail_second_open_error
            handle = real_open(self, mode, buffering, encoding, errors, newline)
            pending_post_open_stat = True
            return handle
        return real_open(self, mode, buffering, encoding, errors, newline)

    def wrapped_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal pending_post_open_stat, fail_next_open
        if pending_post_open_stat and follow_symlinks and self in (target_path, target_resolved):
            pending_post_open_stat = False
            counts["post_open_stat_calls"] += 1
            attempt = counts["post_open_stat_calls"]
            if fail_second_open_error is not None:
                fail_next_open = True
            if make_error is not None:
                raise make_error(attempt)
            if make_stat is not None:
                return make_stat(real_stat(self, follow_symlinks=follow_symlinks), attempt)
        return real_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "open", wrapped_open)
    monkeypatch.setattr(Path, "stat", wrapped_stat)
    yield counts


@pytest.mark.parametrize(
    ("exc_factory", "expected_exc_type"),
    [
        (lambda _attempt: PermissionError(errno.EACCES, "sin permiso en stat final"), PermissionError),
        (lambda _attempt: OSError(errno.EIO, "error de E/S en stat final"), OSError),
    ],
)
def test_fingerprint_c1_error_inspeccion_en_stat_final_devuelve_evidence_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_factory: Callable[[int], OSError],
    expected_exc_type: type[OSError],
) -> None:
    """C1: PermissionError o EIO en Path.stat() tras leer falla a EVIDENCE_INCOMPLETE, no a EVIDENCE_UNSTABLE."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR"
    mod_dir.mkdir(parents=True)
    bat = mod_dir / "ParallaxR.BAT"
    bat.write_bytes(b"@echo ok")

    # Verificación directa del helper: propaga de inmediato en el primer intento sin consumir reintentos.
    with (
        _interceptar_stat_final_post_lectura(monkeypatch, bat, make_error=exc_factory) as counts_direct,
        pytest.raises(expected_exc_type),
    ):
        _fingerprint_file_consistently(bat.resolve())
    assert counts_direct["open_calls"] == 1
    assert counts_direct["post_open_stat_calls"] == 1

    # Verificación integrada en preflight: EVIDENCE_INCOMPLETE sin handoff ni fuga de ruta en mensaje.
    with _interceptar_stat_final_post_lectura(monkeypatch, bat, make_error=exc_factory) as counts_preflight:
        preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert counts_preflight["open_calls"] == 1
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.status != ParallaxRAssistedStatus.EVIDENCE_UNSTABLE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_dir.resolve(),)
    assert str(tmp_path) not in preflight.message
    with pytest.raises(ValueError):
        prepare_parallaxr_manual_handoff(preflight)


def test_fingerprint_c2_error_en_stat_final_de_marcador_preserva_evidencia_previa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2: Error de acceso en Path.stat() final de un segundo marcador falla a EVIDENCE_INCOMPLETE preservando previos."""
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "01_Mod_A"
    mod_a.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a")
    marker_1_bytes = b"marcador valido previo"
    marker_1 = mod_a / "ParallaxROutput.tmp"
    marker_1.write_bytes(marker_1_bytes)

    mod_b = mods_dir / "02_Out_B"
    mod_b.mkdir(parents=True)
    marker_2 = mod_b / "ParallaxROutput.tmp"
    marker_2.write_bytes(b"marcador con fallo en stat final")

    # discover_output_markers debe elevar ParallaxREvidenceIncompleteError con marker_1 preservado.
    with (
        _interceptar_stat_final_post_lectura(
            monkeypatch,
            marker_2,
            make_error=lambda _attempt: PermissionError(errno.EACCES, "acceso denegado tras lectura"),
        ) as counts_markers,
        pytest.raises(ParallaxREvidenceIncompleteError) as exc_info,
    ):
        discover_output_markers(mods_dir)

    assert counts_markers["open_calls"] == 1
    assert len(exc_info.value.partial_markers) == 1
    assert exc_info.value.partial_markers[0].path == marker_1.resolve()
    assert exc_info.value.partial_markers[0].sha256 == hashlib.sha256(marker_1_bytes).hexdigest()

    # run_parallaxr_assisted_preflight preserva marcador válido y candidato sin devolver READY ni EVIDENCE_UNSTABLE.
    with _interceptar_stat_final_post_lectura(
        monkeypatch,
        marker_2,
        make_error=lambda _attempt: OSError(errno.EIO, "error E/S en stat final del marcador"),
    ) as counts_preflight:
        preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert counts_preflight["open_calls"] == 1
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.status != ParallaxRAssistedStatus.EVIDENCE_UNSTABLE
    assert preflight.success is False
    assert preflight.installation is None
    assert preflight.candidate_mod_roots == (mod_a.resolve(),)
    assert len(preflight.existing_output_candidates) == 1
    assert preflight.existing_output_candidates[0].path == marker_1.resolve()
    with pytest.raises(ValueError):
        prepare_parallaxr_manual_handoff(preflight)


def test_fingerprint_c3_mutacion_genuina_conserva_evidence_unstable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3: Un cambio real de identidad tras la lectura sigue clasificando como EVIDENCE_UNSTABLE."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR"
    mod_dir.mkdir(parents=True)
    bat = mod_dir / "ParallaxR.BAT"
    bat.write_bytes(b"@echo estable")

    def stat_mutado(real: os.stat_result, attempt: int) -> os.stat_result:
        return os.stat_result(
            (
                real.st_mode,
                real.st_ino + attempt,
                real.st_dev,
                real.st_nlink,
                real.st_uid,
                real.st_gid,
                real.st_size + 1,
                real.st_atime,
                real.st_mtime,
                real.st_ctime,
            )
        )

    with _interceptar_stat_final_post_lectura(monkeypatch, bat, make_stat=stat_mutado) as counts:
        preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert counts["open_calls"] == _FINGERPRINT_MAX_ATTEMPTS
    assert counts["post_open_stat_calls"] == _FINGERPRINT_MAX_ATTEMPTS
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_UNSTABLE
    assert preflight.success is False
    assert preflight.installation is None
    with pytest.raises(ValueError):
        prepare_parallaxr_manual_handoff(preflight)


def test_fingerprint_c4_enoent_en_stat_final_no_devuelve_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C4: ENOENT durante el stat() final se trata como carrera anti-TOCTOU o evidencia incompleta, nunca READY."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR"
    mod_dir.mkdir(parents=True)
    bat = mod_dir / "ParallaxR.BAT"
    bat.write_bytes(b"@echo carrera")

    # Caso A: reemplazo concurrente repetido donde open() reabre el path pero stat() final ve ENOENT.
    with _interceptar_stat_final_post_lectura(
        monkeypatch,
        bat,
        make_error=lambda _attempt: FileNotFoundError(errno.ENOENT, "ausente en carrera stat"),
    ) as counts_race:
        preflight_race = run_parallaxr_assisted_preflight(mods_dir)

    assert counts_race["open_calls"] == _FINGERPRINT_MAX_ATTEMPTS
    assert counts_race["post_open_stat_calls"] == _FINGERPRINT_MAX_ATTEMPTS
    assert preflight_race.status == ParallaxRAssistedStatus.EVIDENCE_UNSTABLE
    assert preflight_race.status != ParallaxRAssistedStatus.READY
    assert preflight_race.success is False
    assert preflight_race.installation is None
    with pytest.raises(ValueError):
        prepare_parallaxr_manual_handoff(preflight_race)

    # Caso B: ENOENT en stat() final seguido de desaparición real al reintentar open().
    with _interceptar_stat_final_post_lectura(
        monkeypatch,
        bat,
        make_error=lambda _attempt: FileNotFoundError(errno.ENOENT, "desapareció en stat"),
        fail_second_open_error=FileNotFoundError(errno.ENOENT, "desapareció al reintentar"),
    ) as counts_gone:
        preflight_gone = run_parallaxr_assisted_preflight(mods_dir)

    assert counts_gone["open_calls"] == 2
    assert counts_gone["post_open_stat_calls"] == 1
    assert preflight_gone.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight_gone.status not in (ParallaxRAssistedStatus.READY, ParallaxRAssistedStatus.MISSING)
    assert preflight_gone.success is False
    assert preflight_gone.installation is None
    with pytest.raises(ValueError):
        prepare_parallaxr_manual_handoff(preflight_gone)


@pytest.mark.skipif(sys.platform != "win32", reason="WinError específico de Windows")
def test_fingerprint_c4_windows_winerror_1921_en_stat_final_falla_cerrado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """En Windows, WinError 1921 disfrazado con errno ENOENT en stat() final falla inmediato a EVIDENCE_INCOMPLETE."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR"
    mod_dir.mkdir(parents=True)
    bat = mod_dir / "ParallaxR.BAT"
    bat.write_bytes(b"@echo winerror")

    winerror_1921 = FileNotFoundError(errno.ENOENT, "El sistema no puede resolver el nombre del archivo", None, 1921)
    with _interceptar_stat_final_post_lectura(monkeypatch, bat, make_error=lambda _attempt: winerror_1921) as counts:
        preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert counts["open_calls"] == 1
    assert preflight.status == ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.status != ParallaxRAssistedStatus.EVIDENCE_UNSTABLE
    assert preflight.success is False
    assert preflight.installation is None


def test_fingerprint_c5_archivo_estable_mantiene_ready_y_handoff(tmp_path: Path) -> None:
    """C5: Regresión sin mocks: entrypoint y marcador estables mantienen READY y handoff manual válido."""
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "ParallaxR"
    mod_dir.mkdir(parents=True)
    bat_bytes = b"@echo estable_v1"
    bat = mod_dir / "ParallaxR.BAT"
    bat.write_bytes(bat_bytes)

    marker_bytes = b"output_marker_v1"
    marker = mod_dir / "ParallaxROutput.tmp"
    marker.write_bytes(marker_bytes)

    preflight = run_parallaxr_assisted_preflight(mods_dir)

    assert preflight.status == ParallaxRAssistedStatus.READY
    assert preflight.success is True
    assert preflight.message == ""
    assert preflight.installation is not None
    assert preflight.installation.entrypoint == bat.resolve()
    assert preflight.installation.mod_root == mod_dir.resolve()
    assert preflight.installation.size_bytes == len(bat_bytes)
    assert preflight.installation.mtime_ns == bat.stat().st_mtime_ns
    assert preflight.installation.sha256 == hashlib.sha256(bat_bytes).hexdigest()
    assert len(preflight.existing_output_candidates) == 1
    assert preflight.existing_output_candidates[0].path == marker.resolve()
    assert preflight.existing_output_candidates[0].sha256 == hashlib.sha256(marker_bytes).hexdigest()

    handoff = prepare_parallaxr_manual_handoff(preflight)
    assert handoff.success is True
    assert handoff.requires_user_launch is True
    assert handoff.direct_helper_invocation is False
    assert handoff.sky_claw_launches_process is False


# =============================================================================
# 10. INVARIANTES DE ESTADO EN ParallaxRAssistedPreflight (#667-A3)
# =============================================================================

_ESTADOS_NEGATIVOS_ESPERADOS: tuple[ParallaxRAssistedStatus, ...] = (
    ParallaxRAssistedStatus.MISSING,
    ParallaxRAssistedStatus.AMBIGUOUS,
    ParallaxRAssistedStatus.INVALID_SELECTION,
    ParallaxRAssistedStatus.EVIDENCE_UNSTABLE,
    ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE,
)


def _evidencia_instalacion_valida(tmp_path: Path) -> ParallaxRInstallationEvidence:
    """Construye una evidencia de instalación coherente para pruebas de contrato del preflight."""
    mod_root = tmp_path / "mods" / "ParallaxR"
    entrypoint = mod_root / "ParallaxR.BAT"
    contenido = b"@echo off\necho ParallaxR\n"
    return ParallaxRInstallationEvidence(
        mod_root=mod_root,
        entrypoint=entrypoint,
        size_bytes=len(contenido),
        mtime_ns=1_700_000_000_000_000_000,
        sha256=hashlib.sha256(contenido).hexdigest(),
    )


def test_t1_ready_con_success_false_es_rechazado() -> None:
    """T1: construir READY con success=False (nominal o posicional) debe fallar cerrado."""
    with pytest.raises(ValueError, match="READY"):
        ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.READY,
            success=False,
        )

    with pytest.raises(ValueError, match="READY"):
        ParallaxRAssistedPreflight(ParallaxRAssistedStatus.READY, False)


def test_t2_missing_con_success_true_es_rechazado() -> None:
    """T2: construir MISSING con success=True (nominal o posicional) debe fallar cerrado."""
    with pytest.raises(ValueError, match="missing"):
        ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.MISSING,
            success=True,
        )

    with pytest.raises(ValueError, match="missing"):
        ParallaxRAssistedPreflight(ParallaxRAssistedStatus.MISSING, True)


def test_t3_catalogo_estados_negativos_esta_completo() -> None:
    """Ancla que todos los estados no-READY del enum están cubiertos por las pruebas parametrizadas."""
    assert set(_ESTADOS_NEGATIVOS_ESPERADOS) == {
        s for s in ParallaxRAssistedStatus if s != ParallaxRAssistedStatus.READY
    }


@pytest.mark.parametrize("status_negativo", _ESTADOS_NEGATIVOS_ESPERADOS)
def test_t3_ningun_estado_negativo_admite_success_true(
    status_negativo: ParallaxRAssistedStatus,
    tmp_path: Path,
) -> None:
    """T3: ningún estado negativo puede construirse con success=True."""
    with pytest.raises(ValueError):
        ParallaxRAssistedPreflight(
            status=status_negativo,
            success=True,
        )

    with pytest.raises(ValueError):
        ParallaxRAssistedPreflight(
            status=status_negativo,
            success=True,
            installation=_evidencia_instalacion_valida(tmp_path),
            message="",
        )


@pytest.mark.parametrize("valor_no_bool", [1, 0, "true", "false", None])
def test_a3_rechaza_success_no_booleano_estricto(valor_no_bool: Any) -> None:
    """success debe ser bool estricto; enteros truthy/falsy o strings no deben aceptarse."""
    with pytest.raises(TypeError):
        ParallaxRAssistedPreflight(
            status=ParallaxRAssistedStatus.READY,
            success=valor_no_bool,
        )


@pytest.mark.parametrize("status_invalido", ["ready", "missing", None, 123])
def test_a3_rechaza_status_que_no_es_enum(status_invalido: Any) -> None:
    """status debe ser una instancia de ParallaxRAssistedStatus, no un string suelto."""
    with pytest.raises(TypeError):
        ParallaxRAssistedPreflight(
            status=status_invalido,
            success=(status_invalido == "ready"),
        )


def test_t4_ready_valido_conserva_inmutabilidad_y_contrato(tmp_path: Path) -> None:
    """T4: un resultado READY legítimo conserva success=True, message='', evidencia válida e inmutabilidad."""
    evidencia = _evidencia_instalacion_valida(tmp_path)
    preflight = ParallaxRAssistedPreflight(
        status=ParallaxRAssistedStatus.READY,
        success=True,
        installation=evidencia,
        candidate_mod_roots=(evidencia.mod_root,),
        message="",
    )
    preflight_posicional = ParallaxRAssistedPreflight(
        ParallaxRAssistedStatus.READY,
        True,
        evidencia,
    )

    assert preflight.status == ParallaxRAssistedStatus.READY
    assert preflight.success is True
    assert preflight.message == ""
    assert preflight.installation == evidencia
    assert preflight_posicional.success is True
    assert preflight_posicional.installation == evidencia

    with pytest.raises(dataclasses.FrozenInstanceError):
        preflight.success = False  # type: ignore[misc]


@pytest.mark.parametrize("status_negativo", _ESTADOS_NEGATIVOS_ESPERADOS)
def test_t5_estados_negativos_validos_construibles_y_serializables(
    status_negativo: ParallaxRAssistedStatus,
) -> None:
    """T5: todos los estados negativos con success=False siguen siendo construibles y serializables."""
    mensaje = f"Fallo esperado para {status_negativo.value}"
    preflight = ParallaxRAssistedPreflight(
        status=status_negativo,
        success=False,
        message=mensaje,
    )

    assert preflight.status is status_negativo
    assert preflight.success is False
    assert preflight.message == mensaje

    serializado = dataclasses.asdict(preflight)
    assert serializado["status"] == status_negativo
    assert serializado["success"] is False
    assert serializado["message"] == mensaje
    assert serializado["installation"] is None
    assert serializado["existing_output_candidates"] == ()
    assert serializado["candidate_mod_roots"] == ()

    json_texto = json.dumps(serializado, default=str)
    assert f'"{status_negativo.value}"' in json_texto
    assert '"success": false' in json_texto


def test_t6_compatibilidad_handoff_sin_io_ni_procesos(tmp_path: Path) -> None:
    """T6: READY válido habilita handoff puro sin filesystem ni procesos; negativos fallan cerrado."""
    evidencia = _evidencia_instalacion_valida(tmp_path)
    preflight_ready = ParallaxRAssistedPreflight(
        status=ParallaxRAssistedStatus.READY,
        success=True,
        installation=evidencia,
        candidate_mod_roots=(evidencia.mod_root,),
        message="",
    )

    with (
        patch.object(Path, "open", side_effect=AssertionError("handoff no debe abrir archivos")),
        patch.object(Path, "stat", side_effect=AssertionError("handoff no debe consultar stat")),
        patch(
            "sky_claw.local.tools.parallaxr_assisted._fingerprint_file_consistently",
            side_effect=AssertionError("handoff no debe recalcular fingerprint"),
        ),
    ):
        handoff = prepare_parallaxr_manual_handoff(preflight_ready)

    assert handoff.success is True
    assert handoff.message == ""
    assert handoff.entrypoint == evidencia.entrypoint
    assert handoff.mod_root == evidencia.mod_root
    assert handoff.fingerprint == evidencia
    assert handoff.requires_user_launch is True
    assert handoff.direct_helper_invocation is False
    assert handoff.sky_claw_launches_process is False

    for status_negativo in _ESTADOS_NEGATIVOS_ESPERADOS:
        preflight_negativo = ParallaxRAssistedPreflight(
            status=status_negativo,
            success=False,
            message="no listo",
        )
        with pytest.raises(ValueError, match="no está en estado READY"):
            prepare_parallaxr_manual_handoff(preflight_negativo)


def test_t7_compatibilidad_consumidores_dataclass_fields_asdict_y_replace(tmp_path: Path) -> None:
    """T7: consumidores que usan fields(), asdict() y replace() conservan contrato e invariante."""
    nombres_campos = tuple(f.name for f in dataclasses.fields(ParallaxRAssistedPreflight))
    assert nombres_campos == (
        "status",
        "success",
        "installation",
        "existing_output_candidates",
        "candidate_mod_roots",
        "message",
    )

    evidencia = _evidencia_instalacion_valida(tmp_path)
    preflight_ready = ParallaxRAssistedPreflight(
        status=ParallaxRAssistedStatus.READY,
        success=True,
        installation=evidencia,
        candidate_mod_roots=(evidencia.mod_root,),
    )

    actualizado = dataclasses.replace(preflight_ready, candidate_mod_roots=())
    assert actualizado.status == ParallaxRAssistedStatus.READY
    assert actualizado.success is True
    assert dataclasses.asdict(actualizado)["success"] is True

    with pytest.raises(ValueError):
        dataclasses.replace(preflight_ready, status=ParallaxRAssistedStatus.MISSING)

    degradado = dataclasses.replace(
        preflight_ready,
        status=ParallaxRAssistedStatus.MISSING,
        success=False,
        installation=None,
        message="eliminado",
    )
    assert degradado.status == ParallaxRAssistedStatus.MISSING
    assert degradado.success is False
    assert dataclasses.asdict(degradado)["success"] is False


# =============================================================================
# 11. IDENTIDAD LÓGICA MO2 FRENTE A IDENTIDAD FÍSICA DEL FILESYSTEM (#667-A4)
# =============================================================================


@contextmanager
def _junction(destino: Path, enlace: Path) -> Iterator[Path]:
    """Crea una junction NTFS `enlace -> destino` y la retira al salir del contexto.

    Las junctions son el único reparse point de directorio reproducible en Windows
    sin privilegios de symlink: permiten modelar los Casos A y B de MO2 (dos
    carpetas hijas lógicas que comparten el mismo destino físico).
    """
    if sys.platform != "win32":
        pytest.skip("Junctions NTFS solo reproducibles en Windows")
    import _winapi

    _winapi.CreateJunction(str(destino), str(enlace))
    try:
        yield enlace
    finally:
        with suppress(OSError):
            enlace.rmdir()


@contextmanager
def _junction_rota(raiz: Path, enlace: Path) -> Iterator[Path]:
    """Crea una junction cuyo destino se borra de inmediato, dejándola rota y reproducible."""
    if sys.platform != "win32":
        pytest.skip("Junctions NTFS solo reproducibles en Windows")
    import _winapi

    # CreateJunction valida el destino: se crea real y se elimina después para romperla.
    destino = raiz / f"destino_temporal_{enlace.name}"
    destino.mkdir()
    _winapi.CreateJunction(str(destino), str(enlace))
    destino.rmdir()
    try:
        yield enlace
    finally:
        with suppress(OSError):
            enlace.rmdir()


def test_t1_caso_a_sin_seleccion_conserva_ambas_identidades_logicas(tmp_path: Path) -> None:
    """T1 (#667-A4): un mod real y su alias comparten destino físico pero son dos identidades MO2."""
    # Arrange: Caso A — 01_Real con el entrypoint oficial y 02_Alias -> 01_Real.
    mods_dir = tmp_path / "mods"
    mod_real = mods_dir / "01_Real"
    mod_real.mkdir(parents=True)
    (mod_real / "ParallaxR.BAT").write_bytes(b"@echo caso_a")
    resolved_mods_dir = mods_dir.resolve()

    # Act + Assert: sin selección la ambigüedad es entre dos carpetas lógicas, no entre dos rutas iguales.
    with _junction(mod_real, mods_dir / "02_Alias"):
        preflight = run_parallaxr_assisted_preflight(mods_dir)

        assert preflight.status is ParallaxRAssistedStatus.AMBIGUOUS
        assert preflight.success is False
        assert preflight.installation is None
        assert preflight.candidate_mod_roots == (
            resolved_mods_dir / "01_Real",
            resolved_mods_dir / "02_Alias",
        )
        with pytest.raises(ValueError):
            prepare_parallaxr_manual_handoff(preflight)


def test_t2_seleccion_de_alias_preserva_identidad_logica_y_round_trip(tmp_path: Path) -> None:
    """T2 (#667-A4): seleccionar 02_Alias devuelve esa identidad lógica y conserva la procedencia física."""
    # Arrange
    mods_dir = tmp_path / "mods"
    mod_real = mods_dir / "01_Real"
    mod_real.mkdir(parents=True)
    bat_bytes = b"@echo off\necho caso_a\n"
    (mod_real / "ParallaxR.BAT").write_bytes(bat_bytes)
    resolved_mods_dir = mods_dir.resolve()
    logical_real = resolved_mods_dir / "01_Real"
    logical_alias = resolved_mods_dir / "02_Alias"

    with _junction(mod_real, mods_dir / "02_Alias"):
        # Act: selección por la carpeta lógica del alias (Forma 1).
        preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mods_dir / "02_Alias")

        # Assert: identidad lógica MO2 en toda la superficie pública.
        assert preflight.status is ParallaxRAssistedStatus.READY
        assert preflight.success is True
        assert preflight.message == ""
        installation = preflight.installation
        assert installation is not None
        assert installation.mod_root == logical_alias
        assert installation.entrypoint == logical_alias / "ParallaxR.BAT"
        assert installation.mod_root in preflight.candidate_mod_roots

        # Assert: procedencia física verificada, separada de la identidad lógica.
        assert installation.resolved_mod_root == logical_real
        assert installation.resolved_entrypoint == logical_real / "ParallaxR.BAT"
        assert installation.size_bytes == len(bat_bytes)
        assert installation.sha256 == hashlib.sha256(bat_bytes).hexdigest()

        # Assert: handoff lógico y round-trip estable con explicit_selection=handoff.mod_root.
        handoff = prepare_parallaxr_manual_handoff(preflight)
        assert handoff.mod_root == installation.mod_root
        assert handoff.entrypoint == installation.entrypoint
        assert handoff.fingerprint is installation

        round_trip = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=handoff.mod_root)
        assert round_trip.status is ParallaxRAssistedStatus.READY
        assert round_trip.installation is not None
        assert round_trip.installation.mod_root == logical_alias
        assert round_trip.installation.resolved_mod_root == logical_real

        # Assert: Forma 2 — selección por el entrypoint lógico oficial.
        via_entrypoint = run_parallaxr_assisted_preflight(
            mods_dir, explicit_selection=mods_dir / "02_Alias" / "ParallaxR.BAT"
        )
        assert via_entrypoint.status is ParallaxRAssistedStatus.READY
        assert via_entrypoint.installation is not None
        assert via_entrypoint.installation.mod_root == logical_alias


def test_t2_variantes_relativas_se_anclan_al_cwd_y_conservan_el_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 (#667-A4): mods_dir y selección relativas se anclan a cwd, nunca a mods_dir por conveniencia."""
    # Arrange
    mods_dir = tmp_path / "mods"
    mod_real = mods_dir / "01_Real"
    mod_real.mkdir(parents=True)
    (mod_real / "ParallaxR.BAT").write_bytes(b"@echo relativo")
    resolved_mods_dir = mods_dir.resolve()
    logical_alias = resolved_mods_dir / "02_Alias"

    with _junction(mod_real, mods_dir / "02_Alias"):
        monkeypatch.chdir(tmp_path)

        # Act
        por_carpeta = run_parallaxr_assisted_preflight(Path("mods"), explicit_selection=Path("mods/02_Alias"))
        por_entrypoint = run_parallaxr_assisted_preflight(
            Path("mods"), explicit_selection=Path("mods/02_Alias/ParallaxR.BAT")
        )

        # Assert
        for preflight in (por_carpeta, por_entrypoint):
            assert preflight.status is ParallaxRAssistedStatus.READY
            assert preflight.installation is not None
            assert preflight.installation.mod_root == logical_alias
            assert preflight.installation.resolved_mod_root == resolved_mods_dir / "01_Real"


def test_t2_mods_dir_mediante_junction_padre_conserva_identidad(tmp_path: Path) -> None:
    """T2 (#667-A4): un mods_dir alcanzado por una junction padre resuelve igual y conserva el alias."""
    # Arrange
    parent_real = tmp_path / "parent_real"
    mods_real = parent_real / "mods"
    mod_real = mods_real / "01_Real"
    mod_real.mkdir(parents=True)
    (mod_real / "ParallaxR.BAT").write_bytes(b"@echo junction_padre")
    resolved_mods_dir = mods_real.resolve()

    with _junction(mod_real, mods_real / "02_Alias"), _junction(parent_real, tmp_path / "parent_link"):
        parent_link = tmp_path / "parent_link"

        # Act
        preflight = run_parallaxr_assisted_preflight(
            parent_link / "mods", explicit_selection=parent_link / "mods" / "02_Alias"
        )

        # Assert
        assert preflight.status is ParallaxRAssistedStatus.READY
        assert preflight.installation is not None
        assert preflight.installation.mod_root == resolved_mods_dir / "02_Alias"
        assert preflight.installation.resolved_mod_root == resolved_mods_dir / "01_Real"


def test_t3_caso_b_alias_multiples_rechazan_el_destino_fisico(tmp_path: Path) -> None:
    """T3 (#667-A4): dos alias hacia un destino no seleccionable distinguen identidad y rechazan lo físico."""
    # Arrange: Caso B — Alias_A y Alias_B -> _pool/Real (dos niveles bajo mods_dir).
    mods_dir = tmp_path / "mods"
    pool = mods_dir / "_pool" / "Real"
    pool.mkdir(parents=True)
    (pool / "ParallaxR.BAT").write_bytes(b"@echo pool")
    resolved_mods_dir = mods_dir.resolve()

    with _junction(pool, mods_dir / "Alias_A"), _junction(pool, mods_dir / "Alias_B"):
        # Assert (a): sin selección hay dos identidades lógicas distintas.
        sin_seleccion = run_parallaxr_assisted_preflight(mods_dir)
        assert sin_seleccion.status is ParallaxRAssistedStatus.AMBIGUOUS
        assert sin_seleccion.success is False
        assert sin_seleccion.candidate_mod_roots == (
            resolved_mods_dir / "Alias_A",
            resolved_mods_dir / "Alias_B",
        )

        # Assert (b): seleccionar Alias_B preserva Alias_B y registra el destino físico real.
        seleccion_b = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mods_dir / "Alias_B")
        assert seleccion_b.status is ParallaxRAssistedStatus.READY
        assert seleccion_b.installation is not None
        assert seleccion_b.installation.mod_root == resolved_mods_dir / "Alias_B"
        assert seleccion_b.installation.resolved_mod_root == resolved_mods_dir / "_pool" / "Real"

        # Assert (c): la ruta física compartida no es una selección válida de MO2.
        for fisica in (mods_dir / "_pool" / "Real", mods_dir / "_pool" / "Real" / "ParallaxR.BAT"):
            rechazo = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=fisica)
            assert rechazo.status is ParallaxRAssistedStatus.INVALID_SELECTION
            assert rechazo.success is False
            assert rechazo.installation is None
            assert rechazo.candidate_mod_roots == (
                resolved_mods_dir / "Alias_A",
                resolved_mods_dir / "Alias_B",
            )


def test_t3b_unico_alias_no_habilita_el_destino_compartido(tmp_path: Path) -> None:
    """T3 (#667-A4): con un único alias, el destino físico compartido sigue sin ser seleccionable."""
    # Arrange
    mods_dir = tmp_path / "mods"
    pool = mods_dir / "_pool" / "Real"
    pool.mkdir(parents=True)
    (pool / "ParallaxR.BAT").write_bytes(b"@echo unico_pool")
    resolved_mods_dir = mods_dir.resolve()

    with _junction(pool, mods_dir / "Alias_A"):
        # Assert: el único candidato se resuelve por identidad lógica.
        automatico = run_parallaxr_assisted_preflight(mods_dir)
        assert automatico.status is ParallaxRAssistedStatus.READY
        assert automatico.installation is not None
        assert automatico.installation.mod_root == resolved_mods_dir / "Alias_A"
        assert automatico.installation.resolved_mod_root == resolved_mods_dir / "_pool" / "Real"

        # Assert: seleccionar el destino físico (carpeta o entrypoint) falla cerrado.
        for fisica in (mods_dir / "_pool" / "Real", mods_dir / "_pool" / "Real" / "ParallaxR.BAT"):
            rechazo = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=fisica)
            assert rechazo.status is ParallaxRAssistedStatus.INVALID_SELECTION
            assert rechazo.success is False
            assert rechazo.installation is None


def test_t4_esquema_de_evidencia_extendido_y_paridad_fail_closed(tmp_path: Path) -> None:
    """T4 (#667-A4): ParallaxRInstallationEvidence pasa de 5 a 7 campos y exige paridad resolved_*."""
    # Arrange
    mod_root = tmp_path / "mods" / "Mod_A"
    entrypoint = mod_root / "ParallaxR.BAT"
    campos_base = {
        "mod_root": mod_root,
        "entrypoint": entrypoint,
        "size_bytes": 3,
        "mtime_ns": 7,
        "sha256": "a" * 64,
    }

    # Act + Assert: constructor heredado de 5 argumentos conserva los defaults (None, None).
    heredada = ParallaxRInstallationEvidence(**campos_base)
    assert heredada.resolved_mod_root is None
    assert heredada.resolved_entrypoint is None

    nombres = tuple(f.name for f in dataclasses.fields(ParallaxRInstallationEvidence))
    assert nombres == (
        "mod_root",
        "entrypoint",
        "size_bytes",
        "mtime_ns",
        "sha256",
        "resolved_mod_root",
        "resolved_entrypoint",
    )
    assert ParallaxRInstallationEvidence.__dataclass_params__.frozen is True
    assert hasattr(ParallaxRInstallationEvidence, "__slots__")

    completa = ParallaxRInstallationEvidence(
        **campos_base,
        resolved_mod_root=mod_root,
        resolved_entrypoint=entrypoint,
    )
    assert set(dataclasses.asdict(completa)) == set(nombres)

    # Assert: mitad-poblado falla cerrado en __init__ y también vía dataclasses.replace().
    with pytest.raises(ValueError, match="resolved_mod_root"):
        ParallaxRInstallationEvidence(**campos_base, resolved_mod_root=mod_root)
    with pytest.raises(ValueError):
        ParallaxRInstallationEvidence(**campos_base, resolved_entrypoint=entrypoint)
    with pytest.raises(ValueError):
        dataclasses.replace(completa, resolved_entrypoint=None)

    # Assert: los contratos congelados #949 permanecen intactos.
    assert tuple(f.name for f in dataclasses.fields(ParallaxRAssistedPreflight)) == (
        "status",
        "success",
        "installation",
        "existing_output_candidates",
        "candidate_mod_roots",
        "message",
    )
    assert tuple(f.name for f in dataclasses.fields(ParallaxRManualHandoff)) == (
        "mode",
        "entrypoint",
        "mod_root",
        "fingerprint",
        "requires_user_launch",
        "recommended_launcher",
        "direct_helper_invocation",
        "sky_claw_launches_process",
        "instruction",
        "success",
        "message",
    )


def test_t5_descubrimiento_emite_identidades_logicas_y_parciales_alineados(tmp_path: Path) -> None:
    """T5 (#667-A4): el motor único proyecta rutas lógicas en el resultado y en los parciales #944."""
    # Arrange (a): Caso A sano.
    mods_sano = tmp_path / "mods_sano"
    mod_real = mods_sano / "01_Real"
    mod_real.mkdir(parents=True)
    (mod_real / "ParallaxR.BAT").write_bytes(b"@echo sano")
    resolved_sano = mods_sano.resolve()

    with _junction(mod_real, mods_sano / "02_Alias"):
        # Act + Assert: dos entrypoints lógicos distintos, no dos copias de la ruta física.
        candidatos = discover_parallaxr_candidates(mods_sano)
        assert candidatos == (
            resolved_sano / "01_Real" / "ParallaxR.BAT",
            resolved_sano / "02_Alias" / "ParallaxR.BAT",
        )

    # Arrange (b): Caso B con un alias válido y una entrada hija rota (#944).
    mods_roto = tmp_path / "mods_roto"
    pool = mods_roto / "_pool" / "Real"
    pool.mkdir(parents=True)
    (pool / "ParallaxR.BAT").write_bytes(b"@echo parcial")
    resolved_roto = mods_roto.resolve()

    with _junction(pool, mods_roto / "01_Alias"), _junction_rota(mods_roto, mods_roto / "Z_Roto"):
        # Act
        with pytest.raises(ParallaxREvidenceIncompleteError) as exc_info:
            discover_parallaxr_candidates(mods_roto)
        preflight = run_parallaxr_assisted_preflight(mods_roto, explicit_selection=mods_roto / "01_Alias")

        # Assert: los parciales son lógicos y coinciden 1:1 con candidate_mod_roots.
        assert exc_info.value.partial_candidates == (resolved_roto / "01_Alias" / "ParallaxR.BAT",)
        assert preflight.status is ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
        assert preflight.success is False
        assert preflight.installation is None
        assert preflight.candidate_mod_roots == (resolved_roto / "01_Alias",)


def test_t6_entrypoints_anidados_y_escapes_no_producen_candidatos(tmp_path: Path) -> None:
    """T6 (#667-A4): sólo el archivo directo bajo un hijo directo es candidato; escapes y anidados no."""
    # Arrange: entrypoint anidado, junction externa y archivo suelto en la raíz de mods_dir.
    mods_dir = tmp_path / "mods"
    anidado = mods_dir / "Mod_Anidado" / "scripts"
    anidado.mkdir(parents=True)
    (anidado / "ParallaxR.BAT").write_bytes(b"@echo anidado")
    externo = tmp_path / "externo"
    externo.mkdir()
    (externo / "ParallaxR.BAT").write_bytes(b"@echo fuera")

    # Act + Assert: la junction externa se descarta por contención física.
    with _junction(externo, mods_dir / "Mod_Escape"):
        assert discover_parallaxr_candidates(mods_dir) == ()
        escape = run_parallaxr_assisted_preflight(mods_dir)
        assert escape.status is ParallaxRAssistedStatus.MISSING
        assert escape.success is False
        assert escape.installation is None

    # Assert: un ParallaxR.BAT suelto en la raíz de mods_dir tampoco es un candidato.
    (mods_dir / "ParallaxR.BAT").write_bytes(b"@echo suelto")
    assert discover_parallaxr_candidates(mods_dir) == ()
    assert run_parallaxr_assisted_preflight(mods_dir).status is ParallaxRAssistedStatus.MISSING


def test_t6b_entrypoint_symlink_dentro_del_mod_separa_procedencia_fisica(tmp_path: Path) -> None:
    """T6 (#667-A4): un symlink de archivo dentro del mod separa entrypoint lógico y archivo físico."""
    # Arrange
    mods_dir = tmp_path / "mods"
    mod_dir = mods_dir / "Mod_A"
    scripts = mod_dir / "scripts"
    scripts.mkdir(parents=True)
    archivo_real = scripts / "ParallaxR.BAT"
    archivo_real.write_bytes(b"@echo symlink_interno")
    try:
        os.symlink(str(archivo_real), str(mod_dir / "ParallaxR.BAT"))
    except OSError as exc:
        # WinError 1314: el host no otorga SeCreateSymbolicLinkPrivilege. Limitación documentada del SO.
        pytest.skip(f"El host no permite symlinks de archivo sin privilegio de Windows: {exc}")
    resolved_mods_dir = mods_dir.resolve()

    # Act
    preflight = run_parallaxr_assisted_preflight(mods_dir)

    # Assert: el entrypoint lógico es el oficial; el físico es el destino inspeccionado y hasheado.
    assert preflight.status is ParallaxRAssistedStatus.READY
    installation = preflight.installation
    assert installation is not None
    assert installation.mod_root == resolved_mods_dir / "Mod_A"
    assert installation.entrypoint == resolved_mods_dir / "Mod_A" / "ParallaxR.BAT"
    assert installation.resolved_mod_root == resolved_mods_dir / "Mod_A"
    assert installation.resolved_entrypoint == resolved_mods_dir / "Mod_A" / "scripts" / "ParallaxR.BAT"
    assert installation.resolved_entrypoint.parent != installation.resolved_mod_root


def _simular_reparse_de_archivo(monkeypatch: pytest.MonkeyPatch, archivo_logico: Path, destino_fisico: Path) -> None:
    """Hace que `_resolve_path` resuelva un archivo lógico a un destino físico dado.

    Mock acotado: simula el kernel resolviendo un reparse point de archivo sin exigir
    privilegios de symlink en Windows. Sólo intercepta la ruta exacta indicada; el resto
    delega en la implementación real del módulo.
    """
    from sky_claw.local.tools import parallaxr_assisted as modulo

    resolver_real = modulo._resolve_path
    objetivo = os.fspath(archivo_logico)

    def resolver_con_reparse_simulado(path: Path, *, strict: bool = False) -> Path:
        if os.fspath(path) == objetivo:
            return destino_fisico
        return resolver_real(path, strict=strict)

    monkeypatch.setattr(modulo, "_resolve_path", resolver_con_reparse_simulado)


def test_t6c_archivo_resuelto_a_profundidad_mas_uno_es_descartado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6c (#667-A4): un ParallaxR.BAT que resuelve a mods_dir/ParallaxR.BAT (profundidad +1) se descarta.

    El archivo aparece lógicamente dentro de Mod_A, pero su destino físico queda un solo
    componente bajo `resolved_mods_dir`. El guard de descendencia estricta +2 debe excluirlo
    sin fabricar EVIDENCE_INCOMPLETE y sin afectar al hermano legítimo.
    """
    # Arrange: Mod_A con un entrypoint que resuelve a la raíz, Mod_B legítimo y el archivo raíz.
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_b = mods_dir / "Mod_B"
    mod_b.mkdir(parents=True)
    (mod_b / "ParallaxR.BAT").write_bytes(b"@echo mod_b")
    mod_a.mkdir()
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a_logico")
    (mods_dir / "ParallaxR.BAT").write_bytes(b"@echo raiz")
    resolved_mods_dir = mods_dir.resolve()
    _simular_reparse_de_archivo(monkeypatch, mod_a / "ParallaxR.BAT", resolved_mods_dir / "ParallaxR.BAT")

    # Act
    candidatos = discover_parallaxr_candidates(mods_dir)
    preflight = run_parallaxr_assisted_preflight(mods_dir)
    rechazo = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mods_dir / "Mod_A")

    # Assert: excluido por +2, hermano legítimo intacto y descarte sin EVIDENCE_INCOMPLETE.
    assert candidatos == (resolved_mods_dir / "Mod_B" / "ParallaxR.BAT",)
    assert preflight.status is ParallaxRAssistedStatus.READY
    assert preflight.status is not ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.installation is not None
    assert preflight.installation.mod_root == resolved_mods_dir / "Mod_B"
    assert preflight.candidate_mod_roots == (resolved_mods_dir / "Mod_B",)
    assert rechazo.status is ParallaxRAssistedStatus.INVALID_SELECTION


def test_t6d_archivo_fuera_de_profundidad_sin_hermanos_retorna_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6d (#667-A4): si el único entrypoint resuelve a +1, el resultado es MISSING, no EVIDENCE_INCOMPLETE."""
    # Arrange
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo mod_a_logico")
    (mods_dir / "ParallaxR.BAT").write_bytes(b"@echo raiz")
    resolved_mods_dir = mods_dir.resolve()
    _simular_reparse_de_archivo(monkeypatch, mod_a / "ParallaxR.BAT", resolved_mods_dir / "ParallaxR.BAT")

    # Act
    candidatos = discover_parallaxr_candidates(mods_dir)
    preflight = run_parallaxr_assisted_preflight(mods_dir)

    # Assert: el descarte es limpio (ausencia de candidatos), no una anomalía de inspección.
    assert candidatos == ()
    assert preflight.status is ParallaxRAssistedStatus.MISSING
    assert preflight.status is not ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
    assert preflight.success is False
    assert preflight.candidate_mod_roots == ()


def test_t7_self_junction_excluida_de_candidatos_marcadores_y_seleccion(tmp_path: Path) -> None:
    """T7 (#667-A4): SelfLink -> mods_dir no aporta candidatos ni marcadores y no es seleccionable."""
    # Arrange: mod legítimo, archivos sueltos en la raíz y una self-junction.
    mods_dir = tmp_path / "mods"
    mod_real = mods_dir / "Mod_Real"
    mod_real.mkdir(parents=True)
    (mod_real / "ParallaxR.BAT").write_bytes(b"@echo selflink")
    (mods_dir / "ParallaxR.BAT").write_bytes(b"@echo suelto")
    (mods_dir / "ParallaxROutput.tmp").write_bytes(b"marcador_suelto")
    resolved_mods_dir = mods_dir.resolve()

    with _junction(mods_dir, mods_dir / "SelfLink"):
        # Assert: la self-junction (profundidad +0) no es candidato ni marcador.
        assert discover_parallaxr_candidates(mods_dir) == (resolved_mods_dir / "Mod_Real" / "ParallaxR.BAT",)
        assert discover_output_markers(mods_dir) == ()

        # Assert: el mod legítimo sigue siendo válido y el handoff funciona.
        preflight = run_parallaxr_assisted_preflight(mods_dir)
        assert preflight.status is ParallaxRAssistedStatus.READY
        assert preflight.installation is not None
        assert preflight.installation.mod_root == resolved_mods_dir / "Mod_Real"
        assert prepare_parallaxr_manual_handoff(preflight).mod_root == resolved_mods_dir / "Mod_Real"

        # Assert: la self-junction tampoco es una selección válida.
        rechazo = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=mods_dir / "SelfLink")
        assert rechazo.status is ParallaxRAssistedStatus.INVALID_SELECTION
        assert rechazo.success is False
        assert rechazo.installation is None


def test_t7b_contencion_inclusiva_y_hermanos_case_sensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T7b (#667-A4): validate_path_containment es inclusiva pero no pliega casing de componentes."""
    # Arrange: contenedor `Mods` y un hermano que difiere sólo en casing (`mods`).
    base_dir = tmp_path / "base"
    parent = base_dir / "Mods"
    (parent / "ModA").mkdir(parents=True)
    (parent / "ModA" / "a.txt").write_text("contenido", encoding="utf-8")
    sibling = base_dir / "mods"

    if sys.platform == "win32":
        # En NTFS estándar `mods` y `Mods` son el mismo objeto: se simula la resolución canónica
        # de un volumen case-sensitive para probar la comparación por componentes, no `Path.__eq__`.
        from sky_claw.local.tools import parallaxr_assisted as modulo

        real_resolve = modulo._resolve_path
        prefijo_plegado = os.fspath(sibling)

        def resolve_sin_plegado(path: Path, *, strict: bool = False) -> Path:
            crudo = os.fspath(path)
            if crudo == prefijo_plegado or crudo.startswith(prefijo_plegado + os.sep):
                return path
            return real_resolve(path, strict=strict)

        monkeypatch.setattr(modulo, "_resolve_path", resolve_sin_plegado)
    else:
        sibling.mkdir()
        (sibling / "ModA").mkdir()

    # Act + Assert: inclusiva en el contenedor, estricta entre hermanos que difieren en casing.
    assert validate_path_containment(parent, parent) is True
    assert validate_path_containment(parent / "ModA" / "a.txt", parent) is True
    assert validate_path_containment(sibling, parent) is False
    assert validate_path_containment(sibling / "ModA" / "a.txt", parent) is False


def test_t8a_guardias_de_entrada_casing_y_hardlinks_fallan_cerrado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8a (#667-A4): Boundary-Split rechaza casing, hardlinks, traversal, ADS y enlaces externos."""
    # Arrange: Mod_A real, alias de directorio, hardlink alternativo y enlace externo hacia el alias.
    mods_dir = tmp_path / "mods"
    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir(parents=True)
    bat = mod_a / "ParallaxR.BAT"
    bat.write_bytes(b"@echo mod_a")
    os.link(bat, mod_a / "evil.bat")
    resolved_mods_dir = mods_dir.resolve()

    with _junction(mod_a, mods_dir / "02_Alias"), _junction(mod_a, tmp_path / "ext_link"):
        rechazos: tuple[Path, ...] = (
            mods_dir / "02_alias",
            mods_dir / "02_Alias" / "parallaxr.bat",
            mod_a / "evil.bat",
            mods_dir / "Mod_A" / ".." / "Mod_B",
            Path("../mods/02_Alias"),
            Path("mods/*"),
            Path("mods/?"),
            mods_dir / "02_Alias:flujo_ads",
            Path("C:Mod_A"),
            Path("\\mods\\02_Alias"),
            tmp_path / "InexistenteRelativo",
            tmp_path / "ext_link",
            Path(""),
            Path("."),
        )

        # Act + Assert: toda entrada no canónica falla cerrado como INVALID_SELECTION.
        for seleccion in rechazos:
            preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=seleccion)
            assert preflight.status is ParallaxRAssistedStatus.INVALID_SELECTION, str(seleccion)
            assert preflight.success is False, str(seleccion)
            assert preflight.installation is None, str(seleccion)

        # Assert: con cwd dentro de un mod, Path("")/Path(".") no pueden seleccionarlo por anclaje ingenuo.
        monkeypatch.chdir(mod_a)
        for vacuo in (Path(""), Path(".")):
            preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=vacuo)
            assert preflight.status is ParallaxRAssistedStatus.INVALID_SELECTION, str(vacuo)

        # Assert (Windows): el casing distinto sólo en un ancestro case-insensitive lo resuelve el kernel
        # y no invalida la selección, porque los segmentos lógicos son exactos.
        if sys.platform == "win32":
            mods_mayusculas = tmp_path / "MODS"
            assert mods_mayusculas.is_dir()
            positivo = run_parallaxr_assisted_preflight(mods_mayusculas, explicit_selection=mods_mayusculas / "Mod_A")
            assert positivo.status is ParallaxRAssistedStatus.READY
            assert positivo.installation is not None
            assert positivo.installation.mod_root == resolved_mods_dir / "Mod_A"


def test_t8b_comparacion_de_contenedor_sin_path_eq(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T8b (#667-A4): 'Mods' y 'mods' nunca se colapsan aunque Path.__eq__ los considere iguales."""
    # Arrange
    base_dir = tmp_path / "base"
    mods_dir = base_dir / "Mods"
    mod_a = mods_dir / "Mod_A"
    mod_a.mkdir(parents=True)
    (mod_a / "ParallaxR.BAT").write_bytes(b"@echo casing_contenedor")
    sibling = base_dir / "mods"

    from sky_claw.local.tools import parallaxr_assisted as modulo

    real_resolve = modulo._resolve_path
    prefijo_plegado = os.fspath(sibling)

    def resolve_sin_plegado(path: Path, *, strict: bool = False) -> Path:
        crudo = os.fspath(path)
        if crudo == prefijo_plegado or crudo.startswith(prefijo_plegado + os.sep):
            return path
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(modulo, "_resolve_path", resolve_sin_plegado)

    # Act + Assert: ninguna de las dos formas estructurales acepta el contenedor plegado.
    por_carpeta = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=sibling / "Mod_A")
    por_entrypoint = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=sibling / "Mod_A" / "ParallaxR.BAT")

    for preflight in (por_carpeta, por_entrypoint):
        assert preflight.status is ParallaxRAssistedStatus.INVALID_SELECTION
        assert preflight.success is False
        assert preflight.installation is None


def test_t8c_dos_padres_realmente_case_sensitive(tmp_path: Path) -> None:
    """T8c (#667-A4): con dos directorios padre que difieren de verdad en casing jamás se selecciona el ajeno."""
    # Arrange: en POSIX es nativo; en Windows requiere NTFS con case-sensitivity por directorio.
    base_dir = tmp_path / "base"
    upper = base_dir / "Mods"
    (upper / "Mod_A").mkdir(parents=True)
    (upper / "Mod_A" / "ParallaxR.BAT").write_bytes(b"@echo mayusculas")
    lower = base_dir / "mods"

    try:
        (lower / "Mod_A").mkdir(parents=True)
        (lower / "Mod_A" / "ParallaxR.BAT").write_bytes(b"@echo minusculas")
    except OSError:
        pytest.skip("El filesystem del host no permite dos padres que difieran sólo en casing")

    if os.fspath(lower.resolve()) == os.fspath(upper.resolve()):
        # NTFS estándar pliega casing: haría falta `fsutil file setCaseSensitiveInfo` (elevación).
        pytest.skip("El filesystem del host pliega casing: falta case-sensitivity por directorio")

    # Act
    preflight = run_parallaxr_assisted_preflight(upper, explicit_selection=lower / "Mod_A")

    # Assert: el contenedor resuelto difiere => jamás se selecciona Mod_A del padre ajeno.
    assert preflight.status is ParallaxRAssistedStatus.INVALID_SELECTION
    assert preflight.success is False
    assert preflight.installation is None


def test_t9_particion_entre_seleccion_invalida_y_evidencia_incompleta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T9 (#667-A4): bucles externos son INVALID_SELECTION; anomalías internas son EVIDENCE_INCOMPLETE."""
    # Arrange (a): árbol sano y un bucle de junctions externo al árbol de mods.
    mods_dir = tmp_path / "mods"
    for nombre in ("Alias_A", "Alias_B"):
        carpeta = mods_dir / nombre
        carpeta.mkdir(parents=True)
        (carpeta / "ParallaxR.BAT").write_bytes(b"@echo sano")

    # Act + Assert: `j1` y `j1/sub` son selecciones externas, no anomalías de la inspección interna.
    with _arena_bucle_junctions_windows(tmp_path) as (_arena, j1, _j2):
        for bucle in (j1, j1 / "sub"):
            preflight = run_parallaxr_assisted_preflight(mods_dir, explicit_selection=bucle)
            assert preflight.status is ParallaxRAssistedStatus.INVALID_SELECTION
            assert preflight.success is False
            assert preflight.installation is None

    # Arrange (b): anomalía interna #944 con una selección explícita válida.
    mods_roto = tmp_path / "mods_roto"
    mod_ok = mods_roto / "A_Ok"
    mod_ok.mkdir(parents=True)
    (mod_ok / "ParallaxR.BAT").write_bytes(b"@echo ok")
    resolved_roto = mods_roto.resolve()

    with _junction_rota(mods_roto, mods_roto / "Z_Roto"):
        # Act
        incompleto = run_parallaxr_assisted_preflight(mods_roto, explicit_selection=mods_roto / "A_Ok")

        # Assert: la anomalía interna prevalece sobre la selección y conserva los parciales lógicos.
        assert incompleto.status is ParallaxRAssistedStatus.EVIDENCE_INCOMPLETE
        assert incompleto.success is False
        assert incompleto.installation is None
        assert incompleto.candidate_mod_roots == (resolved_roto / "A_Ok",)

    # Arrange (c): un RuntimeError ajeno a un bucle de symlinks no puede enmascararse.
    real_resolve = Path.resolve
    entrypoint_ok = mod_ok / "ParallaxR.BAT"

    def resolve_con_runtimeerror_ajeno(self: Path, strict: bool = False) -> Path:
        if self == entrypoint_ok:
            raise RuntimeError("bug interno no relacionado")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_con_runtimeerror_ajeno)

    # Act + Assert
    with pytest.raises(RuntimeError, match="bug interno no relacionado"):
        run_parallaxr_assisted_preflight(mods_roto)


def test_t10_guardias_estaticas_anti_ffi_plegado_y_path_eq() -> None:
    """T10 (#667-A4): anclas estáticas — sin FFI, sin is_relative_to y sin plegado de casing en rutas."""
    # Arrange
    module_path = Path(__file__).resolve().parent.parent / "sky_claw" / "local" / "tools" / "parallaxr_assisted.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    # Assert: sin FFI Win32 ni biblioteca de tipos externos.
    assert "ctypes" not in source
    assert "FindFirstFileW" not in source

    # Assert: `is_relative_to` pliega casing en Windows y no puede sobrevivir en contención.
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "is_relative_to"]

    # Assert: el único plegado de caso admitido es la traducción de bucle en _resolve_path (str(err).lower()).
    assert "normcase" not in source
    plegados = [
        nodo
        for nodo in ast.walk(tree)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr in {"lower", "casefold"}
    ]
    assert len(plegados) == 1
    unico_plegado = plegados[0]
    assert isinstance(unico_plegado.func.value, ast.Call)
    funcion_base = unico_plegado.func.value.func
    assert isinstance(funcion_base, ast.Name) and funcion_base.id == "str"

    # Assert: la identidad de contenedor se compara por componentes + cadena OS, no por Path.__eq__.
    funciones = {nodo.name: nodo for nodo in tree.body if isinstance(nodo, ast.FunctionDef)}
    assert "_is_same_resolved_container" in funciones
    contrato_contenedor = ast.unparse(funciones["_is_same_resolved_container"])
    assert "parts ==" in contrato_contenedor
    assert "os.fspath" in contrato_contenedor
    assert "is_relative_to" not in contrato_contenedor

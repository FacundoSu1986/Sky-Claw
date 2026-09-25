"""Tests para el modo asistido externo de ParallaxR (PR-A0).

Verifica descubrimiento acotado, validación de contención de rutas, fingerprint
coherente anti-TOCTOU de ParallaxR.BAT, marcadores de salida preexistentes,
handoff manual inmutable, contrato de resultado (success/message), invariantes
estáticos anti-ejecución y ausencia de mutaciones en disco.
"""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from sky_claw.local.tools.parallaxr_assisted import (
    ParallaxRAssistedStatus,
    ParallaxREvidenceUnstableError,
    ParallaxRManualHandoff,
    _FileFingerprintSnapshot,
    _fingerprint_file_consistently,
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

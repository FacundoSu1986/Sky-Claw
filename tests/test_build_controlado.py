"""Pruebas TDD (AAA, negativas y de contrato) para el build controlado PR-LIC-03B (B01–B15).

Cubre:
- B01–B03: locks con hashes, perfiles explícitos base vs native-parallax, sin contaminación dev.
- B04/B13: captura y saneamiento de Analysis TOC real, atribución de árbol fuente limpio vs sucio.
- B05/B08/B09/B12: validación del entorno instalado (runtime + build), PEP 503, detección de
  dependencias build-only filtradas al EXE sin fusionar locks.
- B10: preservación del contrato de build.bat (tests por defecto, aborto ante fallo, SKIP_TESTS).
- B11: protección de rutas peligrosas, directorios preexistentes, symlinks y limpieza de obsoletos.
- B14: veredicto técnico independiente que falla cerrado ante cualquier blocker desconocido y
  preserva bloqueos jurídicos conocidos.
- B15: release.yml byte-idéntico/pausado y ci.yml sin ampliar artefactos publicados.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import build_controlado as bc

RAIZ = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers sintéticos
# ---------------------------------------------------------------------------


def _lock_valido(pares: dict[str, str]) -> dict[str, bc.EntradaLock]:
    return {
        bc._normalizar_pep503(k): bc.EntradaLock(
            nombre=bc._normalizar_pep503(k),
            version=v,
            hashes=(f"sha256:{hashlib.sha256(f'{k}=={v}'.encode()).hexdigest()}",),
        )
        for k, v in pares.items()
    }


def _reporte_base_tecnico_valido() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "gate_passed": False,
        "legal_certification": False,
        "structural_inventory_complete": True,
        "provenance_reconciliation_verified": True,
        "cpython_version_uncertainty": False,
        "analysis_toc_reconciliation": {
            "provided": True,
            "status": "VERIFIED",
            "declared_count": 10,
            "matched_count": 10,
            "missing_from_exe": [],
            "malformed_entries": [],
        },
        "lock_drift": [],
        "unidentified": [],
        "opaque_containers": [],
        "ambiguous_attributions": [],
        "hash_mismatches": [],
        "environment_mismatch": [],
        "spdx_validation_errors": [],
        "packages_summary": {
            "redistributed": ["aiohttp", "pydantic"],
            "installed_not_redistributed": [],
            "build_only": ["pyinstaller", "hatchling", "hatch-vcs", "pyinstaller-hooks-contrib"],
        },
        "blockers": [
            "pending_legal_review=4",
            "license_uncertainties=2",
            "mo2_integration_boundary_pending_legal=8",
        ],
    }


# ---------------------------------------------------------------------------
# 1. Lockfiles, hashes, PEP 503 y compatibilidad (B02, B09, B12)
# ---------------------------------------------------------------------------


def test_parsear_lockfile_con_hashes_acepta_los_tres_locks_reales_del_repo() -> None:
    # Arrange & Act
    lock_base = bc.parsear_lockfile_con_hashes(RAIZ / "requirements.lock")
    lock_np = bc.parsear_lockfile_con_hashes(RAIZ / "requirements-native-parallax.lock")
    lock_build = bc.parsear_lockfile_con_hashes(RAIZ / "requirements-build.lock")

    # Assert
    assert len(lock_base) >= 80
    assert len(lock_np) > len(lock_base)
    assert {"numpy", "pillow"} <= set(lock_np)
    assert "numpy" not in lock_base
    assert "pillow" not in lock_base
    assert set(lock_build) >= bc.HERRAMIENTAS_BUILD_OBLIGATORIAS
    bc.verificar_compatibilidad_locks(lock_base, lock_build)
    bc.verificar_compatibilidad_locks(lock_np, lock_build)


def test_parsear_lockfile_rechaza_sin_hash_flotante_hash_malformado_y_duplicado_pep503(tmp_path: Path) -> None:
    # Arrange 1 — versión flotante
    f_flotante = tmp_path / "flotante.lock"
    f_flotante.write_text("pydantic>=2.0\n", encoding="utf-8")

    # Arrange 2 — pin exacto pero sin hash
    f_sin_hash = tmp_path / "sin_hash.lock"
    f_sin_hash.write_text("pydantic==2.12.5\n", encoding="utf-8")

    # Arrange 3 — hash SHA-256 malformado
    f_hash_malo = tmp_path / "hash_malo.lock"
    f_hash_malo.write_text("pydantic==2.12.5 \\\n    --hash=sha256:noeshex\n", encoding="utf-8")

    # Arrange 4 — duplicado tras normalización PEP 503 (My_Pkg vs my-pkg)
    h = "a" * 64
    f_dup = tmp_path / "dup.lock"
    f_dup.write_text(
        f"My_Pkg==1.0.0 \\\n    --hash=sha256:{h}\nmy-pkg==1.0.0 \\\n    --hash=sha256:{h}\n",
        encoding="utf-8",
    )

    # Act & Assert
    with pytest.raises(bc.BuildControladoError, match="flotante|fijada|=="):
        bc.parsear_lockfile_con_hashes(f_flotante)
    with pytest.raises(bc.BuildControladoError, match="hash"):
        bc.parsear_lockfile_con_hashes(f_sin_hash)
    with pytest.raises(bc.BuildControladoError, match="hash|inválido"):
        bc.parsear_lockfile_con_hashes(f_hash_malo)
    with pytest.raises(bc.BuildControladoError, match="duplicado|PEP 503"):
        bc.parsear_lockfile_con_hashes(f_dup)


def test_verificar_compatibilidad_locks_rechaza_conflicto_de_version_y_faltante_de_build() -> None:
    # Arrange
    lock_rt = _lock_valido({"pefile": "2024.8.26", "pydantic": "2.12.5"})
    lock_build_conflicto = _lock_valido(
        {
            "pyinstaller": "6.22.3",
            "pyinstaller-hooks-contrib": "2026.10",
            "hatchling": "1.33.0",
            "hatch-vcs": "0.5.0",
            "pefile": "2023.2.7",  # conflicto de versión con runtime
        }
    )
    lock_build_incompleto = _lock_valido({"pyinstaller": "6.22.3", "hatchling": "1.33.0"})

    # Act & Assert
    with pytest.raises(bc.BuildControladoError, match="conflicto|versión|pefile"):
        bc.verificar_compatibilidad_locks(lock_rt, lock_build_conflicto)
    with pytest.raises(bc.BuildControladoError, match="obligatoria|build|hatch-vcs"):
        bc.verificar_compatibilidad_locks(lock_rt, lock_build_incompleto)


# ---------------------------------------------------------------------------
# 2. Validación de entorno instalado (B03, B05, B08, B09)
# ---------------------------------------------------------------------------


def test_validar_entorno_build_acepta_base_y_native_parallax_y_rechaza_desviaciones() -> None:
    # Arrange
    lock_rt_base = _lock_valido({"aiohttp": "3.14.4", "pydantic": "2.12.5"})
    lock_rt_np = _lock_valido({"aiohttp": "3.14.4", "pydantic": "2.12.5", "numpy": "2.4.6", "pillow": "13.0.0"})
    lock_build = _lock_valido(
        {
            "pyinstaller": "6.22.3",
            "pyinstaller-hooks-contrib": "2026.10",
            "hatchling": "1.33.0",
            "hatch-vcs": "0.5.0",
        }
    )
    instalados_base_ok = {
        "sky-claw": "0.2.4",
        "pip": "24.0",
        "aiohttp": "3.14.4",
        "pydantic": "2.12.5",
        "pyinstaller": "6.22.3",
        "pyinstaller-hooks-contrib": "2026.10",
        "hatchling": "1.33.0",
        "hatch-vcs": "0.5.0",
    }
    instalados_np_ok = dict(instalados_base_ok, numpy="2.4.6", pillow="13.0.0")

    # Act & Assert positivos
    assert bc.validar_entorno_build(instalados_base_ok, lock_rt_base, lock_build, perfil="base") == []
    assert bc.validar_entorno_build(instalados_np_ok, lock_rt_np, lock_build, perfil="native-parallax") == []

    # Negativo 1 — dependencia dev no autorizada (B01/B05)
    err_dev = bc.validar_entorno_build(dict(instalados_base_ok, mypy="1.20.2"), lock_rt_base, lock_build, perfil="base")
    assert any("mypy" in e for e in err_dev)

    # Negativo 2 — versión incorrecta (B05)
    err_ver = bc.validar_entorno_build(
        dict(instalados_base_ok, aiohttp="3.14.3"), lock_rt_base, lock_build, perfil="base"
    )
    assert any("aiohttp" in e for e in err_ver)

    # Negativo 3 — dependencia transitiva/runtime faltante (B09)
    sin_pydantic = {k: v for k, v in instalados_base_ok.items() if k != "pydantic"}
    err_miss_rt = bc.validar_entorno_build(sin_pydantic, lock_rt_base, lock_build, perfil="base")
    assert any("pydantic" in e for e in err_miss_rt)

    # Negativo 4 — herramienta obligatoria de build faltante (B09)
    sin_hatch = {k: v for k, v in instalados_base_ok.items() if k != "hatch-vcs"}
    err_miss_b = bc.validar_entorno_build(sin_hatch, lock_rt_base, lock_build, perfil="base")
    assert any("hatch-vcs" in e for e in err_miss_b)

    # Negativo 5 — numpy/pillow en perfil base (B03 / ADR 0014)
    lock_rt_envenenado = _lock_valido({"aiohttp": "3.14.4", "pydantic": "2.12.5", "numpy": "2.4.6"})
    err_np = bc.validar_entorno_build(
        dict(instalados_base_ok, numpy="2.4.6"), lock_rt_envenenado, lock_build, perfil="base"
    )
    assert any("numpy" in e for e in err_np)


# ---------------------------------------------------------------------------
# 3. Protección de rutas y rollback (B11)
# ---------------------------------------------------------------------------


def test_validar_directorio_build_seguro_rechaza_rutas_peligrosas_y_preexistentes(tmp_path: Path) -> None:
    # Arrange
    repo_sim = tmp_path / "repo"
    venv_dev = repo_sim / ".venv"
    venv_dev.mkdir(parents=True)
    preexistente = tmp_path / "ya_existe"
    preexistente.mkdir()
    ruta_mo2 = tmp_path / "Mod Organizer 2" / "build"
    ruta_skyrim = tmp_path / "Skyrim Special Edition" / "build"
    ruta_segura = tmp_path / "aislado_nuevo"

    # Act & Assert
    for peligrosa in (repo_sim, venv_dev, preexistente, ruta_mo2, ruta_skyrim):
        with pytest.raises(bc.BuildControladoError, match="peligros|existe|prohibid|desarrollo"):
            bc.validar_directorio_build_seguro(peligrosa, repo_sim)

    # Ruta nueva y segura sí se acepta
    bc.validar_directorio_build_seguro(ruta_segura, repo_sim)


# ---------------------------------------------------------------------------
# 4. B08 + B13 + B14 — Veredicto técnico independiente y procedencia
# ---------------------------------------------------------------------------


def test_veredicto_tecnico_aprueba_con_bloqueos_juridicos_conocidos_y_rechaza_bloqueos_tecnicos() -> None:
    # Arrange
    lock_rt = _lock_valido({"aiohttp": "3.14.4", "pydantic": "2.12.5"})
    lock_build = _lock_valido(
        {
            "pyinstaller": "6.22.3",
            "pyinstaller-hooks-contrib": "2026.10",
            "hatchling": "1.33.0",
            "hatch-vcs": "0.5.0",
            "setuptools": "84.2.1",
        }
    )

    # Act 1 — caso técnicamente válido con bloqueos jurídicos conocidos preservados (B07 + B14)
    v_ok = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=[],
    )
    assert v_ok["technical_build_valid"] is True
    assert v_ok["legal_gate_passed"] is False
    assert v_ok["legal_certification"] is False
    assert len(v_ok["legal_blockers"]) == 3
    assert v_ok["technical_blockers"] == []

    # Act 2 — B08: dependencia build-only (setuptools) termina redistribuida en el EXE
    rep_b08 = _reporte_base_tecnico_valido()
    rep_b08["packages_summary"]["redistributed"] = ["aiohttp", "pydantic", "setuptools"]
    v_b08 = bc.evaluar_veredicto_tecnico(
        rep_b08, lock_rt, lock_build, smoke_passed=True, entorno_errores=[], spdx_internal_errors=[]
    )
    assert v_b08["technical_build_valid"] is False
    assert "setuptools" in v_b08["build_deps_redistributed_in_exe"]

    # Act 3 — B14: blocker desconocido debe fallar cerrado
    rep_unk = _reporte_base_tecnico_valido()
    rep_unk["blockers"].append("unexpected_new_blocker=1")
    v_unk = bc.evaluar_veredicto_tecnico(
        rep_unk, lock_rt, lock_build, smoke_passed=True, entorno_errores=[], spdx_internal_errors=[]
    )
    assert v_unk["technical_build_valid"] is False
    assert "unexpected_new_blocker=1" in v_unk["technical_blockers"]

    # Act 4 — B14/B19: fallo de validación SPDX interna bloquea validez técnica bajo su categoría propia
    v_spdx = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=["error de esquema SPDX"],
    )
    assert v_spdx["technical_build_valid"] is False

    # Act 5 — B13/B14: falta de reconciliación de Analysis TOC bloquea validez técnica
    rep_sin_toc = _reporte_base_tecnico_valido()
    rep_sin_toc["provenance_reconciliation_verified"] = False
    v_sin_toc = bc.evaluar_veredicto_tecnico(
        rep_sin_toc, lock_rt, lock_build, smoke_passed=True, entorno_errores=[], spdx_internal_errors=[]
    )
    assert v_sin_toc["technical_build_valid"] is False


def test_b13_saneamiento_de_rutas_personales_en_analysis_toc_y_estado_sucio() -> None:
    # Arrange — rutas con perfil de usuario personal no deben filtrarse al TOC publicado
    toc_crudo = {
        "schema_version": 1,
        "pure": [["sky_claw", r"C:\Users\PersonaPrivada\repo\sky_claw\__init__.py", "PYMODULE"]],
        "binaries": [["python311.dll", r"C:\Users\PersonaPrivada\AppData\Local\Python\python311.dll", "BINARY"]],
        "datas": [["sky_claw/app/gui/styles.css", r"E:\Skyclaw_Main_Sync\sky_claw\app\gui\styles.css", "DATA"]],
    }

    # Act
    saneado = bc.sanear_analysis_toc(
        toc_crudo,
        repo_root=Path(r"E:\Skyclaw_Main_Sync"),
        build_venv=Path(r"C:\Users\PersonaPrivada\AppData\Local\Python"),
    )

    # Assert
    texto = str(saneado)
    assert "PersonaPrivada" not in texto
    assert "<REPO>" in texto or "<BUILD_VENV>" in texto or "<EXTERNAL>" in texto


# ---------------------------------------------------------------------------
# 5. B10 + B15 — Contratos de build.bat, ci.yml y release.yml
# ---------------------------------------------------------------------------


def test_b10_build_bat_preserva_tests_por_defecto_aborto_y_skip_tests_y_delega_build_controlado() -> None:
    # Arrange
    contenido = (RAIZ / "build.bat").read_text(encoding="utf-8")

    # Assert — tests corren por defecto, abortan en error, respetan SKIP_TESTS, y el build usa build_controlado.py
    assert "SKIP_TESTS" in contenido
    assert "pytest tests/" in contenido
    assert "if errorlevel 1" in contenido
    assert "scripts/build_controlado.py" in contenido or "scripts\\build_controlado.py" in contenido
    assert "uv run --with pyinstaller" not in contenido


def test_b15_ci_usa_locks_con_hashes_sin_ampliar_upload_artifact_y_release_permanece_pausado() -> None:
    # Arrange
    ci = yaml.safe_load((RAIZ / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    release = yaml.safe_load((RAIZ / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))

    build_steps = ci["jobs"]["build"]["steps"]
    texto_build = "\n".join(str(s.get("run", "")) for s in build_steps)
    sec_steps = ci["jobs"]["security"]["steps"]
    texto_sec = "\n".join(str(s.get("run", "")) for s in sec_steps)

    # Assert — B02/B06/B09/B16: build delega instalación aislada/hash-enforced al mismo orquestador
    codigo_orquestador = (RAIZ / "scripts" / "build_controlado.py").read_text(encoding="utf-8")
    assert "--prepare-venv" in texto_build
    assert "requirements.lock" in texto_build
    assert "--require-hashes" in codigo_orquestador
    assert "--no-build-isolation" in codigo_orquestador
    assert "SKYCLAW_EXPORT_ANALYSIS_TOC" in str(build_steps)
    assert "requirements-build.lock" in texto_sec

    # Assert — B15: publicación del EXE suspendida en todos los eventos hasta PR-LIC-04 y release.yml pausado
    uploads = [s for s in build_steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["path"].strip() == "dist/SkyClawApp.exe"
    assert uploads[0].get("if") == "${{ false }}"
    assert release["jobs"]["release"]["if"] == "${{ false }}"


# ---------------------------------------------------------------------------
# 6. R1: B16 — Contrato común local/CI, --verify-report estricto y exit codes pwsh
# ---------------------------------------------------------------------------


def test_b16_verify_report_rechaza_reporte_de_otro_exe_sin_smoke_o_sin_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange — EXE real con un SHA-256 y reporte perteneciente a OTRO EXE
    import json

    exe_real = tmp_path / "SkyClawApp.exe"
    exe_real.write_bytes(b"MZ-ejecutable-real-actual")
    sha_real = hashlib.sha256(exe_real.read_bytes()).hexdigest()

    rep_otro_exe = _reporte_base_tecnico_valido()
    rep_otro_exe["exe"] = {"name": "SkyClawApp.exe", "sha256": "f" * 64, "bytes": 123}
    rep_otro_path = tmp_path / "reconciliation_report_otro.json"
    rep_otro_path.write_text(json.dumps(rep_otro_exe), encoding="utf-8")

    # Act 1 — sin --exe / --validate-venv / --smoke-outcome debe fallar cerrado (no aceptar constantes True/[])
    rc_sin_evidencia = bc.main(["--verify-report", str(rep_otro_path), "--profile", "base"])

    # Act 2 — pasando el EXE real cuyo SHA-256 no coincide con el del reporte debe fallar cerrado
    rc_mismatch = bc.main(
        [
            "--verify-report",
            str(rep_otro_path),
            "--profile",
            "base",
            "--exe",
            str(exe_real),
            "--smoke-outcome",
            "success",
        ]
    )
    assert rc_sin_evidencia != bc.EXIT_OK
    assert rc_mismatch != bc.EXIT_OK

    # Act 3 — caso discriminante aislado para --smoke-outcome: todo válido salvo smoke ausente/fallido
    rep_ok = _reporte_base_tecnico_valido()
    rep_ok["exe"] = {"name": "SkyClawApp.exe", "sha256": sha_real, "bytes": exe_real.stat().st_size}
    rep_ok_path = tmp_path / "reconciliation_report_ok.json"
    rep_ok_path.write_text(json.dumps(rep_ok), encoding="utf-8")
    fake_venv = tmp_path / "fake_venv"
    fake_py = (
        fake_venv / ("Scripts" if bc.os.name == "nt" else "bin") / ("python.exe" if bc.os.name == "nt" else "python")
    )
    fake_py.parent.mkdir(parents=True)
    fake_py.write_bytes(b"")
    id_limpia = {
        "commit": "a" * 40,
        "tree": "b" * 40,
        "source_tree_clean": True,
        "dirty_entries_count": 0,
        "inputs_sha256": {"sky_claw.spec": "1" * 64},
    }
    pre_ok_path = tmp_path / "source_pre_ok.json"
    pre_ok_path.write_text(
        json.dumps({"schema_version": 1, "profile": "base", "run_id": None, "source_identity": id_limpia}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bc, "_obtener_mapa_instalados", lambda *_a, **_k: ("3.11.9", {}))
    monkeypatch.setattr(bc, "validar_entorno_build", lambda *_a, **_k: [])
    monkeypatch.setattr(bc, "capturar_identidad_fuente", lambda *_a, **_k: id_limpia)

    base_args = [
        "--verify-report",
        str(rep_ok_path),
        "--profile",
        "base",
        "--exe",
        str(exe_real),
        "--validate-venv",
        str(fake_venv),
        "--source-pre",
        str(pre_ok_path),
    ]
    assert bc.main([*base_args, "--smoke-outcome", "success"]) == bc.EXIT_OK
    assert bc.main([*base_args, "--smoke-outcome", "failure"]) == bc.EXIT_FAIL_CLOSED
    assert bc.main(base_args) == bc.EXIT_FAIL_CLOSED


def test_b16_ci_comparte_preparacion_venv_y_propaga_lastexitcode_en_pwsh() -> None:
    # Arrange
    ci = yaml.safe_load((RAIZ / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    build_steps = ci["jobs"]["build"]["steps"]
    by_name = {s.get("name", ""): s for s in build_steps}

    install_step = by_name["Install build dependencies (isolated, hash-enforced)"]
    reconcile_step = by_name["Inventory and reconcile frozen executable (PR-LIC-03A/B)"]

    # Assert — CI delega la preparación del venv al mismo orquestador fuera del checkout y verifica $LASTEXITCODE
    install_run = str(install_step.get("run", ""))
    assert "--prepare-venv" in install_run
    assert "--source-pre-out" in install_run
    assert "--run-id" in install_run
    assert "RUNNER_TEMP" in install_run
    assert ".build-venv" not in install_run
    rec_run = str(reconcile_step.get("run", ""))
    assert "$ErrorActionPreference = 'Stop'" in rec_run or '$ErrorActionPreference = "Stop"' in rec_run
    assert "$LASTEXITCODE" in rec_run
    assert "--exe dist/SkyClawApp.exe" in rec_run
    assert "--source-pre" in rec_run
    assert "--run-id" in rec_run
    assert "--smoke-outcome" in rec_run
    assert "--validate-venv" in rec_run


# ---------------------------------------------------------------------------
# 7. R1: B17 — Protección de out-dir, dist-dir, colisiones, symlinks y evidencias ajenas
# ---------------------------------------------------------------------------


def test_b17_out_dir_preexistente_con_evidencia_ajena_falla_cerrado_sin_borrar_nada(tmp_path: Path) -> None:
    # Arrange — directorio de evidencia preexistente de otra corrida con artefactos válidos
    repo_sim = tmp_path / "repo"
    repo_sim.mkdir()
    out_ajeno = tmp_path / "evidencia_previa"
    out_ajeno.mkdir()
    manifiesto_ajeno = out_ajeno / "build_manifest.json"
    sbom_ajeno = out_ajeno / "sbom.spdx.json"
    manifiesto_ajeno.write_text('{"run": "ajeno"}\n', encoding="utf-8")
    sbom_ajeno.write_text('{"spdxVersion": "SPDX-2.3"}\n', encoding="utf-8")
    dist_ok = tmp_path / "dist_ok"

    # Act & Assert — debe fallar cerrado en validación de destinos ANTES de borrar o sobrescribir
    with pytest.raises(bc.BuildControladoError, match="no está vacío"):
        bc.validar_destinos_build(dist_dir=dist_ok, out_dir=out_ajeno, build_dir_opcional=None, repo_root=repo_sim)
    with pytest.raises(bc.BuildControladoError, match="no está vacío"):
        bc.ejecutar_build_controlado(
            repo_root=repo_sim,
            perfil="base",
            dist_dir=dist_ok,
            out_dir=out_ajeno,
        )
    assert manifiesto_ajeno.is_file()
    assert manifiesto_ajeno.read_text(encoding="utf-8") == '{"run": "ajeno"}\n'
    assert sbom_ajeno.is_file()


def test_b17_validar_destinos_rechaza_rutas_peligrosas_colisiones_y_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    repo_sim = tmp_path / "repo"
    repo_sim.mkdir()
    dist_ok = tmp_path / "dist_ok"
    out_ok = tmp_path / "out_ok"
    mismo = tmp_path / "colision"

    # Colisión out_dir == dist_dir
    with pytest.raises(bc.BuildControladoError, match="colis|mismo|dist"):
        bc.validar_destinos_build(dist_dir=mismo, out_dir=mismo, build_dir_opcional=None, repo_root=repo_sim)

    # Ruta peligrosa en out_dir (MO2 / Skyrim / .venv)
    with pytest.raises(bc.BuildControladoError, match="prohibid|peligros"):
        bc.validar_destinos_build(
            dist_dir=dist_ok, out_dir=tmp_path / "Mod Organizer 2" / "ev", build_dir_opcional=None, repo_root=repo_sim
        )

    # Ruta peligrosa en dist_dir
    with pytest.raises(bc.BuildControladoError, match="prohibid|peligros"):
        bc.validar_destinos_build(
            dist_dir=tmp_path / "Skyrim Special Edition" / "dist",
            out_dir=out_ok,
            build_dir_opcional=None,
            repo_root=repo_sim,
        )

    # Simulación de junction/symlink/reparse point en out_dir (desechable, no requiere privilegio Admin)
    out_link = tmp_path / "out_link"
    out_link.mkdir()
    monkeypatch.setattr(bc, "_es_reparse_point_o_symlink", lambda p: p == out_link)
    with pytest.raises(bc.BuildControladoError, match="symlink|junction|reparse"):
        bc.validar_destinos_build(dist_dir=dist_ok, out_dir=out_link, build_dir_opcional=None, repo_root=repo_sim)


# ---------------------------------------------------------------------------
# 8. R1: B18 — Identidad del árbol fuente (pre/post, sucio vs certificable, mutación)
# ---------------------------------------------------------------------------


def test_b18_arbol_sucio_y_mutacion_durante_build_no_obtienen_technical_build_valid() -> None:
    # Arrange
    lock_rt = _lock_valido({"aiohttp": "3.14.4", "pydantic": "2.12.5"})
    lock_build = _lock_valido(
        {
            "pyinstaller": "6.22.3",
            "pyinstaller-hooks-contrib": "2026.10",
            "hatchling": "1.33.0",
            "hatch-vcs": "0.5.0",
        }
    )
    id_limpia = {
        "commit": "a" * 40,
        "tree": "b" * 40,
        "source_tree_clean": True,
        "dirty_entries_count": 0,
        "inputs_sha256": {"sky_claw.spec": "1" * 64, "requirements.lock": "2" * 64, "sky_claw_tree": "3" * 64},
    }
    id_sucia = dict(id_limpia, source_tree_clean=False, dirty_entries_count=2)
    id_mutada = dict(
        id_limpia,
        inputs_sha256={"sky_claw.spec": "1" * 64, "requirements.lock": "9" * 64, "sky_claw_tree": "3" * 64},
    )

    # Act 1 — limpio pre == post => certificable y technical_build_valid=True
    v_limpio = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=[],
        source_pre=id_limpia,
        source_post=id_limpia,
    )
    assert v_limpio["technical_build_valid"] is True
    assert v_limpio["certifiable_controlled_build"] is True
    assert v_limpio["build_mode"] == "controlled_certifiable"

    # Act 2 — árbol sucio (modo default) => technical_build_valid=False y no certificable
    v_sucio = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=[],
        source_pre=id_sucia,
        source_post=id_sucia,
    )
    assert v_sucio["technical_build_valid"] is False
    assert v_sucio["certifiable_controlled_build"] is False
    assert "source_tree_dirty=1" in v_sucio["technical_blockers"]

    # Act 3 — árbol sucio con allow_dirty=True => modo local_dev, NUNCA technical_build_valid=True ni certificable
    v_dev = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=[],
        source_pre=id_sucia,
        source_post=id_sucia,
        allow_dirty=True,
    )
    assert v_dev["technical_build_valid"] is False
    assert v_dev["certifiable_controlled_build"] is False
    assert v_dev["build_mode"] == "local_dev"
    assert v_dev["local_dev_build_passed"] is True

    # Act 4 — mutación de archivo fuente o lock DURANTE el build => bloqueado incluso con allow_dirty=True
    v_mut = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=[],
        source_pre=id_limpia,
        source_post=id_mutada,
        allow_dirty=True,
    )
    assert v_mut["technical_build_valid"] is False
    assert v_mut["certifiable_controlled_build"] is False
    assert v_mut["local_dev_build_passed"] is False
    assert "source_mutated_during_build=1" in v_mut["technical_blockers"]


# ---------------------------------------------------------------------------
# 9. R1: B19 — Separación explícita de validación SPDX interna vs oficial vs no ejecutada
# ---------------------------------------------------------------------------


def test_b19_validacion_spdx_separa_interna_oficial_y_no_ejecutada() -> None:
    # Arrange
    lock_rt = _lock_valido({"aiohttp": "3.14.4", "pydantic": "2.12.5"})
    lock_build = _lock_valido(
        {
            "pyinstaller": "6.22.3",
            "pyinstaller-hooks-contrib": "2026.10",
            "hatchling": "1.33.0",
            "hatch-vcs": "0.5.0",
        }
    )

    # Act 1 — sin schema oficial provisto: interna pasa, oficial figura explícitamente como not_executed
    v_no_of = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=[],
        spdx_official_validation=None,
    )
    assert v_no_of["spdx_internal_validation"]["executed"] is True
    assert v_no_of["spdx_internal_validation"]["passed"] is True
    assert v_no_of["spdx_official_schema_validation"]["status"] == "not_executed"
    assert v_no_of["spdx_official_schema_validation"]["executed"] is False

    # Act 2 — error del validador interno NO se etiqueta como spdx_official_schema_errors
    v_err_int = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=["campo interno faltante"],
        spdx_official_validation=None,
    )
    assert v_err_int["technical_build_valid"] is False
    assert "spdx_internal_validation_errors=1" in v_err_int["technical_blockers"]
    assert not any(b.startswith("spdx_official_schema_errors=") for b in v_err_int["technical_blockers"])

    # Act 3 — error de schema oficial SÍ bloquea como spdx_official_schema_errors
    v_err_of = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_internal_errors=[],
        spdx_official_validation={
            "status": "failed",
            "executed": True,
            "schema_source": "spdx-schema-v2.3.json",
            "errors": ["$.spdxVersion: invalid"],
        },
    )
    assert v_err_of["technical_build_valid"] is False
    assert "spdx_official_schema_errors=1" in v_err_of["technical_blockers"]
    assert v_err_of["spdx_official_schema_validation"]["status"] == "failed"


def test_b18_capturar_identidad_fuente_detecta_mutacion_de_codigo_y_lock_en_repo_temporal(tmp_path: Path) -> None:
    # Arrange — mini repo git limpio desechable
    import subprocess

    repo = tmp_path / "mini_repo"
    (repo / "sky_claw").mkdir(parents=True)
    (repo / "sky_claw" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "sky_claw.spec").write_text("# spec\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "requirements.lock").write_text("a==1.0\n", encoding="utf-8")
    (repo / "requirements-build.lock").write_text("b==1.0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    # Act 1 — captura previa limpia
    pre = bc.capturar_identidad_fuente(
        repo,
        lock_runtime_path=repo / "requirements.lock",
        lock_build_path=repo / "requirements-build.lock",
    )
    assert pre["source_tree_clean"] is True
    assert pre["dirty_entries_count"] == 0

    # Act 2 — mutación de archivo fuente durante compilación
    (repo / "sky_claw" / "mod.py").write_text("X = 2\n", encoding="utf-8")
    post_codigo = bc.capturar_identidad_fuente(
        repo,
        lock_runtime_path=repo / "requirements.lock",
        lock_build_path=repo / "requirements-build.lock",
    )
    assert post_codigo != pre
    assert post_codigo["inputs_sha256"]["sky_claw_tree"] != pre["inputs_sha256"]["sky_claw_tree"]
    assert post_codigo["source_tree_clean"] is False

    # Act 3 — mutación adicional en lockfile
    (repo / "requirements.lock").write_text("a==1.1\n", encoding="utf-8")
    post_lock = bc.capturar_identidad_fuente(
        repo,
        lock_runtime_path=repo / "requirements.lock",
        lock_build_path=repo / "requirements-build.lock",
    )
    assert post_lock["inputs_sha256"]["requirements.lock"] != post_codigo["inputs_sha256"]["requirements.lock"]


def test_b19_validar_spdx_contra_schema_oficial_distingue_passed_failed_y_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange — schema JSON mínimo desechable y dos SBOMs (válido e inválido)
    import json
    import sys
    import types

    schema_path = tmp_path / "spdx-schema-test.json"
    schema_path.write_text(
        json.dumps(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "required": ["spdxVersion", "SPDXID"],
                "properties": {"spdxVersion": {"const": "SPDX-2.3"}, "SPDXID": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )
    sbom_ok = tmp_path / "ok.spdx.json"
    sbom_ok.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "SPDXID": "SPDXRef-DOCUMENT"}), encoding="utf-8")
    sbom_bad = tmp_path / "bad.spdx.json"
    sbom_bad.write_text(json.dumps({"spdxVersion": "SPDX-1.0"}), encoding="utf-8")

    # Act 1 — sin schema provisto => not_executed
    r_none = bc.validar_spdx_contra_schema_oficial(sbom_ok, None)
    assert r_none["status"] == "not_executed"
    assert r_none["executed"] is False

    # Act 2 — schema inexistente => fail-closed
    r_miss = bc.validar_spdx_contra_schema_oficial(sbom_ok, tmp_path / "no_existe.json")
    assert r_miss["status"] == "failed"
    assert r_miss["executed"] is False

    # Act 3 — con validador JSON Schema disponible (real si está instalado, o stub fiel si no)
    if "jsonschema" not in sys.modules:
        try:
            import jsonschema  # noqa: F401
        except ImportError:

            class _FakeErr:
                def __init__(self, path: list[str], message: str) -> None:
                    self.path = path
                    self.message = message

            class _FakeValidator:
                def __init__(self, schema: dict[str, Any]) -> None:
                    self._schema = schema

                def iter_errors(self, instance: dict[str, Any]) -> list[_FakeErr]:
                    errs: list[_FakeErr] = []
                    for req in self._schema.get("required", []):
                        if req not in instance:
                            errs.append(_FakeErr([req], f"'{req}' is a required property"))
                    if instance.get("spdxVersion") != "SPDX-2.3":
                        errs.append(_FakeErr(["spdxVersion"], "'SPDX-2.3' was expected"))
                    return errs

            fake_mod = types.ModuleType("jsonschema")
            fake_validators = types.SimpleNamespace(validator_for=lambda _s: _FakeValidator)
            fake_mod.validators = fake_validators  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, "jsonschema", fake_mod)

    r_ok = bc.validar_spdx_contra_schema_oficial(sbom_ok, schema_path)
    assert r_ok["status"] == "passed"
    assert r_ok["executed"] is True
    assert r_ok["errors"] == []

    r_bad = bc.validar_spdx_contra_schema_oficial(sbom_bad, schema_path)
    assert r_bad["status"] == "failed"
    assert r_bad["executed"] is True
    assert len(r_bad["errors"]) >= 1


# ---------------------------------------------------------------------------
# 10. R2: B18 cierre definitivo en CI — captura pre/post independiente y run_id
# ---------------------------------------------------------------------------


def test_b18_ci_verify_report_exige_source_pre_independiente_y_detecta_mutacion_o_run_id_obsoleto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    import json

    exe_real = tmp_path / "SkyClawApp.exe"
    exe_real.write_bytes(b"MZ-ejecutable-r2")
    sha_real = hashlib.sha256(exe_real.read_bytes()).hexdigest()

    rep_ok = _reporte_base_tecnico_valido()
    rep_ok["exe"] = {"name": "SkyClawApp.exe", "sha256": sha_real, "bytes": exe_real.stat().st_size}
    rep_path = tmp_path / "reconciliation_report.json"
    rep_path.write_text(json.dumps(rep_ok), encoding="utf-8")

    fake_venv = tmp_path / "fake_venv"
    fake_py = (
        fake_venv / ("Scripts" if bc.os.name == "nt" else "bin") / ("python.exe" if bc.os.name == "nt" else "python")
    )
    fake_py.parent.mkdir(parents=True)
    fake_py.write_bytes(b"")

    id_pre = {
        "commit": "a" * 40,
        "tree": "b" * 40,
        "source_tree_clean": True,
        "dirty_entries_count": 0,
        "inputs_sha256": {"sky_claw.spec": "1" * 64, "requirements.lock": "2" * 64, "sky_claw_tree": "3" * 64},
    }
    id_post_mutada = dict(
        id_pre,
        inputs_sha256={"sky_claw.spec": "1" * 64, "requirements.lock": "9" * 64, "sky_claw_tree": "3" * 64},
    )

    pre_ok_path = tmp_path / "source_pre_ok.json"
    pre_ok_path.write_text(
        json.dumps({"schema_version": 1, "profile": "base", "run_id": "run-100-1", "source_identity": id_pre}),
        encoding="utf-8",
    )
    pre_obsoleto_path = tmp_path / "source_pre_obsoleto.json"
    pre_obsoleto_path.write_text(
        json.dumps({"schema_version": 1, "profile": "base", "run_id": "run-099-1", "source_identity": id_pre}),
        encoding="utf-8",
    )
    pre_perfil_mal_path = tmp_path / "source_pre_perfil_mal.json"
    pre_perfil_mal_path.write_text(
        json.dumps(
            {"schema_version": 1, "profile": "native-parallax", "run_id": "run-100-1", "source_identity": id_pre}
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(bc, "_obtener_mapa_instalados", lambda *_a, **_k: ("3.11.9", {}))
    monkeypatch.setattr(bc, "validar_entorno_build", lambda *_a, **_k: [])
    monkeypatch.setattr(bc, "capturar_identidad_fuente", lambda *_a, **_k: id_pre)

    base_args = [
        "--verify-report",
        str(rep_path),
        "--profile",
        "base",
        "--exe",
        str(exe_real),
        "--validate-venv",
        str(fake_venv),
        "--smoke-outcome",
        "success",
        "--run-id",
        "run-100-1",
    ]

    # Act & Assert 1 — sin --source-pre debe fallar cerrado
    assert bc.main(base_args) == bc.EXIT_FAIL_CLOSED

    # Act & Assert 2 — con --source-pre de otro run_id (obsoleto) debe fallar cerrado
    assert bc.main([*base_args, "--source-pre", str(pre_obsoleto_path)]) == bc.EXIT_FAIL_CLOSED

    # Act & Assert 3 — con --source-pre de otro perfil debe fallar cerrado
    assert bc.main([*base_args, "--source-pre", str(pre_perfil_mal_path)]) == bc.EXIT_FAIL_CLOSED

    # Act & Assert 4 — con --source-pre válido e idéntico a post => EXIT_OK
    assert bc.main([*base_args, "--source-pre", str(pre_ok_path)]) == bc.EXIT_OK

    # Act & Assert 5 (discriminante MB16) — si el árbol/lock mutó entre pre y post => EXIT_FAIL_CLOSED
    monkeypatch.setattr(bc, "capturar_identidad_fuente", lambda *_a, **_k: id_post_mutada)
    assert bc.main([*base_args, "--source-pre", str(pre_ok_path)]) == bc.EXIT_FAIL_CLOSED

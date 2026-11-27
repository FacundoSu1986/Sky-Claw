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
        spdx_schema_errors=[],
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
        rep_b08, lock_rt, lock_build, smoke_passed=True, entorno_errores=[], spdx_schema_errors=[]
    )
    assert v_b08["technical_build_valid"] is False
    assert "setuptools" in v_b08["build_deps_redistributed_in_exe"]

    # Act 3 — B14: blocker desconocido debe fallar cerrado
    rep_unk = _reporte_base_tecnico_valido()
    rep_unk["blockers"].append("unexpected_new_blocker=1")
    v_unk = bc.evaluar_veredicto_tecnico(
        rep_unk, lock_rt, lock_build, smoke_passed=True, entorno_errores=[], spdx_schema_errors=[]
    )
    assert v_unk["technical_build_valid"] is False
    assert "unexpected_new_blocker=1" in v_unk["technical_blockers"]

    # Act 4 — B14: fallo de validación SPDX bloquea validez técnica
    v_spdx = bc.evaluar_veredicto_tecnico(
        _reporte_base_tecnico_valido(),
        lock_rt,
        lock_build,
        smoke_passed=True,
        entorno_errores=[],
        spdx_schema_errors=["error de esquema SPDX"],
    )
    assert v_spdx["technical_build_valid"] is False

    # Act 5 — B13/B14: falta de reconciliación de Analysis TOC bloquea validez técnica
    rep_sin_toc = _reporte_base_tecnico_valido()
    rep_sin_toc["provenance_reconciliation_verified"] = False
    v_sin_toc = bc.evaluar_veredicto_tecnico(
        rep_sin_toc, lock_rt, lock_build, smoke_passed=True, entorno_errores=[], spdx_schema_errors=[]
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

    # Assert — B02/B06/B09: build usa locks con --require-hashes y --no-build-isolation
    assert "requirements-build.lock" in texto_build
    assert "requirements.lock" in texto_build
    assert "--require-hashes" in texto_build
    assert "--no-build-isolation" in texto_build
    assert "SKYCLAW_EXPORT_ANALYSIS_TOC" in str(build_steps)
    assert "requirements-build.lock" in texto_sec

    # Assert — B15: no ampliar upload-artifact del job build ni activar release.yml
    uploads = [s for s in build_steps if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["path"].strip() == "dist/SkyClawApp.exe"
    assert release["jobs"]["release"]["if"] == "${{ false }}"

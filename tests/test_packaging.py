"""Packaging invariants for security-critical data files."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_security_policy_yaml_is_forced_into_wheel() -> None:
    """Fail-closed sanitizer must ship its YAML policy in built wheels."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    wheel_config = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    force_include = wheel_config.get("force-include", {})

    assert "sky_claw/app/security/security_policy.yaml" in force_include
    assert force_include["sky_claw/app/security/security_policy.yaml"] == "sky_claw/app/security/security_policy.yaml"


def test_security_policy_yaml_is_in_pyinstaller_datas() -> None:
    """Frozen builds must bundle policy YAML before sanitizer import."""
    spec = Path("sky_claw.spec").read_text(encoding="utf-8")

    assert '("sky_claw/app/security/security_policy.yaml", "sky_claw/app/security")' in spec


def test_xedit_scripts_forzados_en_wheel() -> None:
    """Los .pas bundleados deben viajar en el wheel: el staging los copia a
    'Edit Scripts' en runtime y sin ellos run_script falla con script-not-found."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    wheel_config = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    force_include = wheel_config.get("force-include", {})

    assert force_include.get("sky_claw/local/xedit/scripts") == "sky_claw/local/xedit/scripts"


def test_xedit_scripts_en_pyinstaller_datas() -> None:
    """El exe congelado también necesita los .pas (mismo patrón que la YAML)."""
    spec = Path("sky_claw.spec").read_text(encoding="utf-8")

    assert '("sky_claw/local/xedit/scripts", "sky_claw/local/xedit/scripts")' in spec


def test_bridge_mo2_en_pyinstaller_datas() -> None:
    """El instalador runtime necesita archivos físicos del plugin, no solo PYZ."""
    spec = Path("sky_claw.spec").read_text(encoding="utf-8")

    assert (
        '("sky_claw/local/mo2/plugin_bundle/skyclaw_bridge", "sky_claw/local/mo2/plugin_bundle/skyclaw_bridge")'
    ) in spec
    assert '"sky_claw.local.mo2.vfs_worker"' in spec
    assert '"sky_claw.local.mo2.vfs_broker"' in spec


_FUENTES_SHIM_SYNTHESIS = "sky_claw/local/tools/synthesis_shim_fuentes"


def test_fuentes_del_shim_synthesis_en_las_dos_distribuciones() -> None:
    """PR-3b: la Fase 1 compila el shim desde estas fuentes; sin ellas no hay Synthesis.

    Las dos distribuciones son hermanas (wheel y exe congelado): se verifican juntas para que
    agregar una sin la otra rompa el test.
    """
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})
    spec = Path("sky_claw.spec").read_text(encoding="utf-8")

    assert force_include.get(_FUENTES_SHIM_SYNTHESIS) == _FUENTES_SHIM_SYNTHESIS
    assert f'("{_FUENTES_SHIM_SYNTHESIS}", "{_FUENTES_SHIM_SYNTHESIS}")' in spec


def test_pr_lic_03b_hook_analysis_toc_y_lock_de_build_presentes() -> None:
    """PR-LIC-03B: sky_claw.spec exporta el Analysis TOC real opt-in y requirements-build.lock existe con hashes."""
    spec = Path("sky_claw.spec").read_text(encoding="utf-8")
    build_lock = Path("requirements-build.lock")

    assert "SKYCLAW_EXPORT_ANALYSIS_TOC" in spec
    assert build_lock.is_file()
    contenido = build_lock.read_text(encoding="utf-8")
    assert "pyinstaller==" in contenido
    assert "--hash=sha256:" in contenido

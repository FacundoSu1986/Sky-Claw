"""Regresiones finales del hardening de Asset Conflict Scan (#527)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import sky_claw.app.orchestrator.supervisor as supervisor_module
import sky_claw.local.assets.asset_scanner as asset_scanner_module
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.assets import AssetConflictDetector


def test_scan_preserva_path_lexico_de_symlink_de_archivo_en_sandbox(tmp_path: Path) -> None:
    """Validar un symlink aceptado no debe reemplazar su identidad VFS léxica."""
    sandbox = tmp_path / "sandbox"
    mods = sandbox / "MO2" / "mods"
    textures = mods / "ModA" / "textures"
    textures.mkdir(parents=True)
    real = textures / "real.dds"
    real.write_bytes(b"DDS")
    alias = textures / "alias.dds"
    try:
        alias.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("La plataforma no permite crear symlinks de archivo sin privilegios adicionales")

    detector = AssetConflictDetector(
        mo2_mods_path=mods,
        path_validator=PathValidator(roots=[sandbox]),
    )

    assets = detector.scan_mod_directory("ModA", calculate_checksums=False)

    assert "textures/alias.dds" in assets
    assert "textures/real.dds" in assets
    assert assets["textures/alias.dds"].relative_path == "textures/alias.dds"


def test_scan_propaga_error_de_enumeracion_de_os_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un fallo de scandir/walk no puede convertirse en un mapa parcial exitoso."""
    mods = tmp_path / "mods"
    (mods / "ModA").mkdir(parents=True)
    boom = OSError("fallo de enumeración")

    def walk_roto(
        top: str | Path,
        topdown: bool = True,
        onerror=None,
        followlinks: bool = False,
    ):
        assert topdown is True
        assert followlinks is False
        assert onerror is not None
        onerror(boom)
        return iter(())

    monkeypatch.setattr(asset_scanner_module.os, "walk", walk_roto)
    detector = AssetConflictDetector(mo2_mods_path=mods)

    with pytest.raises(OSError) as ctx:
        detector.scan_mod_directory("ModA", calculate_checksums=False)
    assert ctx.value is boom


def _targets_de(nodo: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    return nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]


def _ref(nodo: ast.expr) -> tuple[str, str] | None:
    if isinstance(nodo, ast.Name):
        return ("name", nodo.id)
    if isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name) and nodo.value.id == "self":
        return ("self", nodo.attr)
    return None


def test_inventario_de_seams_bound_resuelve_aliases_locales() -> None:
    """Un ``alias = self._metodo`` pasado al builder sigue contando como seam bound."""
    source = inspect.getsource(supervisor_module.SupervisorAgent)
    tree = ast.parse(source)
    clase = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    init = next(n for n in clase.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    nombres_metodo = {
        n.name for n in clase.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    builder_calls = [
        n
        for n in ast.walk(init)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "build_orchestration_composition"
    ]
    assert len(builder_calls) == 1
    builder = builder_calls[0]

    bindings: dict[tuple[str, str], str | None] = {}
    asignaciones = sorted(
        (n for n in ast.walk(init) if isinstance(n, (ast.Assign, ast.AnnAssign))),
        key=lambda n: n.lineno,
    )
    for asignacion in asignaciones:
        if asignacion.lineno >= builder.lineno or asignacion.value is None:
            continue
        valor = asignacion.value
        metodo: str | None = None
        if (
            isinstance(valor, ast.Attribute)
            and isinstance(valor.value, ast.Name)
            and valor.value.id == "self"
            and valor.attr in nombres_metodo
        ):
            metodo = valor.attr
        else:
            ref_valor = _ref(valor)
            if ref_valor is not None:
                metodo = bindings.get(ref_valor)
        for target in _targets_de(asignacion):
            ref_target = _ref(target)
            if ref_target is not None:
                bindings[ref_target] = metodo

    seams: dict[str, str] = {}
    for kw in builder.keywords:
        assert kw.arg is not None, "**kwargs impediría inventariar exhaustivamente los seams"
        valor = kw.value
        metodo: str | None = None
        if (
            isinstance(valor, ast.Attribute)
            and isinstance(valor.value, ast.Name)
            and valor.value.id == "self"
            and valor.attr in nombres_metodo
        ):
            metodo = valor.attr
        else:
            ref_valor = _ref(valor)
            if ref_valor is not None:
                metodo = bindings.get(ref_valor)
        if metodo is not None:
            seams[kw.arg] = metodo

    assert seams == {"plugin_limit_guard": "_run_plugin_limit_guard"}

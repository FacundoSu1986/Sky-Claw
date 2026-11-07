"""#879: la tarjeta de Wrye Bash debe leer su ruta de la config, como sus hermanas.

El escáner de la GUI se siembra desde ``config.toml`` (``build_tool_path_cfg_keys``) y
recién DESPUÉS exporta lo resuelto a ``os.environ`` (``_hydrate_tool_env_from_snapshot``).
Una herramienta con ``snapshot_env_var`` pero sin ``config_field`` no tiene por dónde
entrar: con el ejecutable fuera de las rutas auto-detectadas la tarjeta decía «No
instalado» aunque ``WRYE_BASH_PATH`` estuviera definido (smoke de la etapa 6, rig Windows,
2026-11-07).

Anclas por enumeración (no por muestreo): la familia es «toda spec con ``snapshot_env_var``»
y cada miembro está en una de dos listas cerradas, con igualdad literal.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

from sky_claw.app.gui._bootloader import _build_environment_scanner
from sky_claw.config import Config
from sky_claw.local.discovery.registry import EXTERNAL_TOOL_REGISTRY, build_tool_path_cfg_keys
from sky_claw.local.local_config import LocalConfig

#: Specs con ``snapshot_env_var`` que NO tienen ``config_field``, congeladas por NOMBRE.
#: DynDOLOD queda fuera a propósito de #879: su pipeline (TexGen + work root externo,
#: ADR 0011) tiene su propia resolución y cambiar su disponibilidad en la GUI es una
#: decisión de la etapa 9, no un efecto colateral de la etapa 6. Agregar otra tool sin
#: ``config_field`` rompe este test hasta que se decida qué hace.
_SIN_CONFIG_FIELD_POR_DECISION: frozenset[str] = frozenset({"dyndolod"})


def test_toda_spec_con_variable_de_entorno_tiene_config_field_o_excepcion_con_nombre() -> None:
    sin_config = {
        spec.key
        for spec in EXTERNAL_TOOL_REGISTRY.values()
        if spec.snapshot_env_var is not None and spec.config_field is None
    }

    assert sin_config == _SIN_CONFIG_FIELD_POR_DECISION


def test_wrye_bash_declara_su_campo_de_config() -> None:
    assert EXTERNAL_TOOL_REGISTRY["wrye_bash"].config_field == "wrye_bash_exe"
    assert build_tool_path_cfg_keys()["wrye_bash"] == "wrye_bash_exe"


def test_todo_config_field_existe_en_las_dos_clases_de_config() -> None:
    """``Config`` (TOML) y ``LocalConfig`` (JSON legacy) no son intercambiables (AGENTS.md)."""
    campos_locales = {campo.name for campo in dataclasses.fields(LocalConfig)}
    defaults = Config.__dict__["_load_defaults"](SimpleNamespace())

    for spec in EXTERNAL_TOOL_REGISTRY.values():
        if spec.config_field is None:
            continue
        assert spec.config_field in defaults, f"{spec.key}: falta {spec.config_field!r} en Config._load_defaults"
        assert spec.config_field in campos_locales, f"{spec.key}: falta {spec.config_field!r} en LocalConfig"


def test_el_escaner_de_la_gui_se_siembra_con_la_ruta_de_wrye_bash_de_la_config(tmp_path: Path) -> None:
    exe = tmp_path / "Wrye Bash" / "Wrye Bash.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'wrye_bash_exe = "{exe.as_posix()}"\n', encoding="utf-8")

    escaner = _build_environment_scanner(SimpleNamespace(config_path=config_path))

    assert escaner._configured_tool_paths["wrye_bash"] == exe


def test_sin_ruta_configurada_el_escaner_no_siembra_wrye_bash(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")

    escaner = _build_environment_scanner(SimpleNamespace(config_path=config_path))

    assert "wrye_bash" not in escaner._configured_tool_paths

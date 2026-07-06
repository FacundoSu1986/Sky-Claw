"""Tests del hotfix de merge de leveled lists (T-02 de TECHNICAL_REVIEW_TASKS.md).

Los scripts de merge (estático y template generado) copiaban la PRIMERA versión
de cada FormID iterada — el master base, es decir el perdedor por load order —
y descartaban los overrides posteriores (P0, TECHNICAL_REVIEW.md §4.1).

El hotfix convierte ambos en un "forward del ganador": solo se procesa la
versión ganadora por load order (``WinningOverride``). xEdit no es ejecutable
en CI, así que estos tests anclan el CONTENIDO de los scripts: el guard de
``WinningOverride`` debe existir y evaluarse antes de la copia del record.
El smoke real está documentado en el header de ``apply_leveled_list_merge.pas``.
"""

import pathlib

import sky_claw.local.xedit
from sky_claw.local.xedit.runner import ScriptGenerator

RUTA_SCRIPT_ESTATICO = pathlib.Path(sky_claw.local.xedit.__file__).parent / "scripts" / "apply_leveled_list_merge.pas"


def _verificar_guard_de_ganador(script: str) -> None:
    """El guard WinningOverride debe aparecer antes de la copia del record."""
    assert "WinningOverride" in script
    assert "wbCopyElementToRecord" in script
    assert script.index("WinningOverride") < script.index("wbCopyElementToRecord")


class TestScriptEstatico:
    """apply_leveled_list_merge.pas solo copia la versión ganadora."""

    def test_script_estatico_usa_winning_override(self) -> None:
        script = RUTA_SCRIPT_ESTATICO.read_text(encoding="utf-8")
        _verificar_guard_de_ganador(script)

    def test_script_estatico_documenta_smoke_manual(self) -> None:
        """El header debe describir cómo validar el script en un rig real."""
        script = RUTA_SCRIPT_ESTATICO.read_text(encoding="utf-8")
        assert "Smoke manual" in script


class TestTemplateGenerado:
    """El template TEMPLATE_MERGE_LEVELED_LIST solo copia la versión ganadora."""

    def test_template_generado_usa_winning_override(self) -> None:
        script = ScriptGenerator.generate_merge_script(
            output_plugin="SkyClaw_MergedPatch.esp",
            record_types=["LVLI", "LVLN", "LVSP"],
        )
        _verificar_guard_de_ganador(script)

    def test_template_generado_preserva_placeholders(self) -> None:
        """El guard no debe romper el .format() del template (llaves Pascal)."""
        script = ScriptGenerator.generate_merge_script(
            output_plugin="SkyClaw_MergedPatch.esp",
            record_types=["LVLI"],
        )
        assert "SkyClaw_MergedPatch.esp" in script
        # Ningún placeholder sin resolver
        assert "{output_plugin}" not in script
        assert "{record_types}" not in script

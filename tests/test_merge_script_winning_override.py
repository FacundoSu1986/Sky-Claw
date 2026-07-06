"""Tests del forward-del-ganador en los scripts de merge (T-02 + reviews PR #238).

Historia: los scripts de merge (estático y template generado) copiaban la
PRIMERA versión de cada FormID iterada — el master base, el perdedor por load
order — revirtiendo la modlist (P0, TECHNICAL_REVIEW.md §4.1). El hotfix T-02
los convirtió en "forward del ganador", y las reviews del PR #238 endurecieron
dos cosas: el ganador se calcula IGNORANDO el plugin de salida (en un re-run
el output ya cargado sería el ``WinningOverride`` de sus propios records y el
guard saltearía todas las fuentes — Codex), y estos tests anclan el patrón
exacto del guard dentro de ``Process`` en vez de cualquier mención en
comentarios (Copilot). xEdit no corre en CI: se verifica el CONTENIDO de los
scripts; el smoke real está documentado en el header del ``.pas``.
"""

import pathlib
import re

import sky_claw.local.xedit
from sky_claw.local.xedit.runner import ScriptGenerator

RUTA_SCRIPT_ESTATICO = pathlib.Path(sky_claw.local.xedit.__file__).parent / "scripts" / "apply_leveled_list_merge.pas"

#: Patrón exacto del guard: buscar solo "WinningOverride" daría falso positivo
#: con las menciones del header/comentarios (review Copilot PR #238).
_GUARD_GANADOR = "if not Equals(e, WinnerExcludingOutput(e)) then"

#: Skip de los records que viven en el propio output (caso re-run).
_SKIP_OUTPUT = "if SameText(GetFileName(GetFile(e)), outputPluginName) then"


def _cuerpo_process(script: str) -> str:
    """Extrae el cuerpo de la función Process del script Pascal."""
    match = re.search(r"function Process\(e: IInterface\).*?\nend;", script, flags=re.DOTALL)
    assert match is not None, "No se encontró la función Process en el script"
    return match.group(0)


def _verificar_guard_de_ganador(script: str) -> None:
    """El guard debe estar DENTRO de Process y antes de la copia del record.

    La copia puede ser directa (``wbCopyElementToRecord``, template) o vía
    helper (``MergeRecord(e)``, script estático).
    """
    assert "function WinnerExcludingOutput" in script
    cuerpo = _cuerpo_process(script)
    assert _SKIP_OUTPUT in cuerpo
    assert _GUARD_GANADOR in cuerpo

    indices_copia = [cuerpo.find("MergeRecord(e)"), cuerpo.find("wbCopyElementToRecord")]
    primera_copia = min(i for i in indices_copia if i != -1)
    assert cuerpo.index(_GUARD_GANADOR) < primera_copia


class TestScriptEstatico:
    """apply_leveled_list_merge.pas solo copia la versión ganadora real."""

    def test_script_estatico_usa_winner_excluding_output(self) -> None:
        script = RUTA_SCRIPT_ESTATICO.read_text(encoding="utf-8")
        _verificar_guard_de_ganador(script)

    def test_script_estatico_documenta_smoke_manual(self) -> None:
        """El header debe describir cómo validar el script en un rig real."""
        script = RUTA_SCRIPT_ESTATICO.read_text(encoding="utf-8")
        assert "Smoke manual" in script


class TestTemplateGenerado:
    """El template TEMPLATE_MERGE_LEVELED_LIST solo copia la versión ganadora real."""

    def test_template_generado_usa_winner_excluding_output(self) -> None:
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

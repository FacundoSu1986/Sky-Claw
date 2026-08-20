"""Contrato de éxito de SynthesisRunner: success exige output atribuible.

Reproduce el falso verde del pipeline: exit 0 + texto de éxito NO alcanzan si
la corrida no deja evidencia de que generó o modificó ``Synthesis.esp`` — el
único nombre contractual del pipeline (SOP §2.7; el service y el lock lo
hardcodean). En particular:

* un ``*.esp`` ajeno (``OldPatch.esp``) no puede atribuirse a la corrida;
* un ``Synthesis.esp`` preexistente sin cambios no es evidencia de producción
  (en producción el sandbox clona el overwrite, así que el esp viejo SIEMPRE
  está presente: sin esta comparación pre/post, una corrida fantasma lo
  atribuiría y validaría como propio);
* un sondeo pre/post INDETERMINADO (stat/lectura fallida) jamás se atribuye:
  OSError != ausente.
"""

from __future__ import annotations

import os
import pathlib
import shutil
from unittest.mock import patch

from sky_claw.local.tools.synthesis_runner import (
    SynthesisConfig,
    SynthesisRunner,
)

#: Texto que ``parse_output`` considera éxito (patrón "Generated ... patch")
#: sin disparar ningún patrón de error.
STDOUT_EXITO = "Successfully generated patch output"

#: Contenido que hace plausible a un .esp (header TES4 + relleno).
ESP_VALIDO = b"TES4" + b"\x00" * 200


def _runner(tmp_path: pathlib.Path) -> SynthesisRunner:
    """Runner real con exe fake y output_path dedicado."""
    (tmp_path / "Synthesis.exe").touch()
    return SynthesisRunner(
        SynthesisConfig(
            game_path=tmp_path,
            mo2_path=tmp_path,
            output_path=tmp_path / "out",
            synthesis_exe=tmp_path / "Synthesis.exe",
            timeout_seconds=5,
        )
    )


# =============================================================================
# TEST A — éxito textual + exit 0 + sin output
# =============================================================================


async def test_exito_textual_sin_output_no_es_exito(tmp_path: pathlib.Path) -> None:
    """exit 0 + texto de éxito + ningún .esp en el output → NO es éxito."""
    (tmp_path / "out").mkdir()
    runner = _runner(tmp_path)

    async def _phantom(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _phantom):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is False
    assert result.output_esp is None
    # Diagnóstico accionable, no "Unknown error".
    assert any("not produced" in e for e in result.errors)


# =============================================================================
# TEST B — ESP viejo (ajeno) no puede validar la corrida
# =============================================================================


async def test_un_esp_preexistente_ajeno_no_se_atribuye_a_esta_corrida(tmp_path: pathlib.Path) -> None:
    """``OldPatch.esp`` ya existía antes del run: presencia preexistente !=
    evidencia de output de esta corrida (fallback glob ``*.esp`` prohibido)."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "OldPatch.esp").write_bytes(ESP_VALIDO)
    runner = _runner(tmp_path)

    async def _phantom(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _phantom):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is False
    assert result.output_esp is None
    assert result.errors


# =============================================================================
# TEST C — Synthesis.esp preexistente y NO modificado
# =============================================================================


async def test_synthesis_esp_preexistente_sin_cambios_no_es_evidencia(tmp_path: pathlib.Path) -> None:
    """El esp esperado existe antes del run pero la corrida no lo toca: no hay
    producción demostrable → fail-closed (en sandbox el clon SIEMPRE trae el
    esp viejo, así que la mera existencia nunca prueba nada)."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    esp.write_bytes(ESP_VALIDO)
    runner = _runner(tmp_path)

    async def _phantom(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        # El proceso "corre" con éxito textual pero no escribe nada.
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _phantom):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is False
    assert result.output_esp is None
    assert any("not modified" in e for e in result.errors)


# =============================================================================
# TEST D — happy path: la corrida produce/modifica Synthesis.esp
# =============================================================================


async def test_corrida_que_reescribe_synthesis_esp_es_exito(tmp_path: pathlib.Path) -> None:
    """Corrida válida: exit 0 + éxito textual + ``Synthesis.esp`` modificado
    por la corrida → success=True con el esp esperado."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    esp.write_bytes(b"v1")
    runner = _runner(tmp_path)

    async def _escribe(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        esp.write_bytes(ESP_VALIDO + b"v2")
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _escribe):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is True
    assert result.output_esp == esp
    assert result.errors == []


async def test_primera_corrida_crea_synthesis_esp_y_es_exito(tmp_path: pathlib.Path) -> None:
    """Primera corrida: el esp no existía antes y la corrida lo crea → éxito."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    runner = _runner(tmp_path)

    async def _crea(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        esp.write_bytes(ESP_VALIDO)
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _crea):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is True
    assert result.output_esp == esp


# =============================================================================
# TEST F — otro ESP NUEVO pero no Synthesis.esp
# =============================================================================


async def test_un_esp_nuevo_con_otro_nombre_no_sustituye_a_synthesis_esp(tmp_path: pathlib.Path) -> None:
    """``RandomPatch.esp`` creado por la corrida NO es sustituto: el contrato
    exige exactamente ``Synthesis.esp`` (lo hardcodean el service, el lock y
    el SOP §2.7)."""
    (tmp_path / "out").mkdir()
    runner = _runner(tmp_path)
    otro = tmp_path / "out" / "RandomPatch.esp"

    async def _crea_otro(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        otro.write_bytes(ESP_VALIDO)
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _crea_otro):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is False
    assert result.output_esp is None


# =============================================================================
# Guard del conjunct de exit code (exit 0 solo NO alcanza)
# =============================================================================


async def test_exit_code_distinto_de_cero_nunca_es_exito(tmp_path: pathlib.Path) -> None:
    """exit != 0 con éxito textual y esp reescrito → fail (el exit code es un
    conjunct necesario, no suficiente)."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    runner = _runner(tmp_path)

    async def _escribe_pero_falla(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        esp.write_bytes(ESP_VALIDO)
        return STDOUT_EXITO.encode(), b"", 1

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _escribe_pero_falla):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is False
    assert result.output_esp is None


# =============================================================================
# R8/R9 — sondeo INDETERMINADO (OSError != ausente) → fail closed
# =============================================================================


async def test_pre_indeterminado_no_atribuye_jamas(tmp_path: pathlib.Path) -> None:
    """PRE con el esp presente pero no inspeccionable (un directorio ocupa el
    nombre: stat OK, lectura falla) + POST creado → la atribución NO puede
    demostrarse → fail closed (OSError PRE no es equivalente a ausente)."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    esp.mkdir()  # existe pero no es legible como archivo → UNREADABLE
    runner = _runner(tmp_path)

    async def _reemplaza_por_archivo(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        shutil.rmtree(esp)
        esp.write_bytes(ESP_VALIDO)
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _reemplaza_por_archivo):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is False
    assert result.output_esp is None
    assert any("could not be inspected" in e for e in result.errors)


async def test_post_indeterminado_falla_cerrado(tmp_path: pathlib.Path) -> None:
    """PRE legible + POST con el esp no inspeccionable → fail closed: un sondeo
    POST indeterminado nunca es evidencia de producción."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    esp.write_bytes(ESP_VALIDO)
    runner = _runner(tmp_path)

    async def _reemplaza_por_directorio(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        esp.unlink()
        esp.mkdir()  # el "run" deja el nombre ocupado por algo ilegible
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _reemplaza_por_directorio):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is False
    assert result.output_esp is None
    assert any("could not be inspected" in e for e in result.errors)


# =============================================================================
# R10/R11 — identidad de contenido prevalece sobre metadata
# =============================================================================


async def test_mismo_tamano_y_mismo_mtime_con_contenido_distinto_es_modificacion(tmp_path: pathlib.Path) -> None:
    """Un reemplazo que conserva tamaño Y timestamp pero cambia los bytes ES
    modificación: la identidad de contenido (sha256) prevalece sobre metadata."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    esp.write_bytes(b"A" * 1000)
    mtime_original = esp.stat().st_mtime_ns
    runner = _runner(tmp_path)

    async def _reescribe_mismo_tamano_mismo_mtime(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        esp.write_bytes(b"B" * 1000)  # mismo tamaño, contenido distinto
        os.utime(esp, ns=(mtime_original, mtime_original))  # mismo timestamp
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _reescribe_mismo_tamano_mismo_mtime):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is True
    assert result.output_esp == esp
    assert result.errors == []


async def test_mismos_bytes_con_metadata_distinta_no_atribuye(tmp_path: pathlib.Path) -> None:
    """Reescribir exactamente los mismos bytes (metadata nueva, contenido
    idéntico) NO es producción nueva: el contenido es la evidencia, no la
    metadata."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    esp.write_bytes(ESP_VALIDO)
    runner = _runner(tmp_path)

    async def _reescribe_mismos_bytes(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        esp.write_bytes(ESP_VALIDO)  # mtime cambia, contenido idéntico
        return STDOUT_EXITO.encode(), b"", 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _reescribe_mismos_bytes):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is False
    assert result.output_esp is None
    assert any("not modified" in e for e in result.errors)


# =============================================================================
# R14 — éxito textual en stderr (contrato combinado de parse_output)
# =============================================================================


async def test_success_textual_en_stderr_cuenta_como_senal(tmp_path: pathlib.Path) -> None:
    """Contrato histórico de ``parse_output``: analiza stdout+stderr combinados
    (misma fuente para errors/patchers/success). El marcador de completado en
    stderr con stdout vacío SÍ es señal semántica; la atribución decide."""
    (tmp_path / "out").mkdir()
    esp = tmp_path / "out" / "Synthesis.esp"
    runner = _runner(tmp_path)

    async def _escribe_con_success_en_stderr(_args: list[str], **_kwargs: object) -> tuple[bytes, bytes, int]:
        esp.write_bytes(ESP_VALIDO)
        return b"", STDOUT_EXITO.encode(), 0

    with patch("sky_claw.local.tools.synthesis_runner.run_capture", _escribe_con_success_en_stderr):
        result = await runner.run_pipeline(["PatcherA"])

    assert result.success is True
    assert result.output_esp == esp

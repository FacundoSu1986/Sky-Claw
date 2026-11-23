"""Atribución de Log_BS.txt a la ejecución real — auditoría P0-3 / P1-8.

El log acumula hasta cuatro corridas antes de rotar; ``_leer_log`` acota por la
última firma ``[#Log``. Eso NO basta: si la corrida no escribió log (el proceso
terminó antes de ``Log::Initialize`` — o el log se escribió idéntico al previo),
la firma más reciente pertenece a un run ANTERIOR y atribuirla al run actual es
un falso verde.

Regla (no opcional):
- Snapshot (tamaño + mtime_ns) ANTES del spawn.
- Si el log está byte-idéntico al snapshot → la corrida NO escribió log →
  atribuible=False → jamás SUCCESS.
- Si el archivo cambió → reintento acotado (no infinito) y atribución por firma.
- Archivo ausente → atribuible=False.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, patch

import sky_claw.local.tools.bodyslide_runner as mod
from sky_claw.local.tools.bodyslide_headless import ManifiestoArtefactos
from sky_claw.local.tools.bodyslide_runner import (
    BodySlideConfig,
    BodySlideRunner,
    _atribuir_log_a_corrida,
    _snapshot_log_inicial,
)

_CONFIG_VÁLIDA = "<Config>\n  <TargetGame>4</TargetGame>\n</Config>\n"


def _runner(tmp_path: pathlib.Path) -> BodySlideRunner:
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    (exe.parent / "Config.xml").write_text(_CONFIG_VÁLIDA, encoding="utf-8")
    return BodySlideRunner(
        BodySlideConfig(bodyslide_exe=exe, game_path=tmp_path / "game", quiet_verified=True),
    )


def _destino(tmp_path: pathlib.Path) -> pathlib.Path:
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    return destino


def test_snapshot_log_inicial_sin_archivo() -> None:
    exe = pathlib.Path("/no/such/exe/BodySlide.exe")
    assert _snapshot_log_inicial(exe) is None


def test_snapshot_log_inicial_con_archivo(tmp_path: pathlib.Path) -> None:
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    (exe.parent / "Log_BS.txt").write_text("[#Log 2026-11-22]\nhola\n", encoding="utf-8")
    pre = _snapshot_log_inicial(exe)
    assert pre is not None
    assert pre.tamaño > 0 and pre.mtime_ns > 0
    assert pre.ultima_firma == "[#Log 2026-11-22]"
    assert pre.total_firmas == 1
    assert pre.offset_ultima_firma == 0


def test_atribuir_log_con_archivo_inexistente() -> None:
    exe = pathlib.Path("/no/such/exe/BodySlide.exe")
    cola = _atribuir_log_a_corrida(exe, None)
    assert cola.atribuible is False


def test_atribuir_log_sin_cambios_no_reintenta_y_no_atribuye(tmp_path: pathlib.Path) -> None:
    """Log idéntico al snapshot → atribuible=False, sin reintentos innecesarios."""
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    (exe.parent / "Log_BS.txt").write_text("[#Log 2026-11-20]\nAll sets processed successfully!\n", encoding="utf-8")
    pre = _snapshot_log_inicial(exe)
    assert pre is not None

    reads = 0
    real_leer = mod._leer_log

    def _contador(*a, **k):
        nonlocal reads
        reads += 1
        return real_leer(*a, **k)

    with patch.object(mod, "_leer_log", side_effect=_contador):
        cola = _atribuir_log_a_corrida(exe, pre)

    assert cola.atribuible is False
    assert reads == 0, "el log idéntico al snapshot no debe leerse (la corrida no escribió)"


def test_atribuir_log_con_cambios_reintenta_acotada_y_atribuye(tmp_path: pathlib.Path) -> None:
    """Log cambiado → reintentos acotados; devuelve la lectura final."""
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    (exe.parent / "Log_BS.txt").write_text("[#Log 2026-11-20]\nstale\n", encoding="utf-8")
    pre = _snapshot_log_inicial(exe)

    # "La corrida" escribe la firma fresca + un contenido limpio.
    (exe.parent / "Log_BS.txt").write_text(
        "[#Log 2026-11-20]\nstale\n[#Log 2026-11-22]\nAll sets processed successfully!\n",
        encoding="utf-8",
    )

    cola = _atribuir_log_a_corrida(exe, pre)

    assert cola.atribuible is True
    assert "All sets processed successfully!" in cola.texto


def test_atribuir_log_con_cambios_y_sin_firma_no_atribuye(tmp_path: pathlib.Path) -> None:
    """Log cambiado pero sin firma → atribuible=False (nunca SUCCESS)."""
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    (exe.parent / "Log_BS.txt").write_text("[#Log 2026-11-20]\nstale\n", encoding="utf-8")
    pre = _snapshot_log_inicial(exe)

    (exe.parent / "Log_BS.txt").write_text("[#Log 2026-11-20]\nstale\nbasura sin firma\n", encoding="utf-8")

    cola = _atribuir_log_a_corrida(exe, pre)

    assert cola.atribuible is False


async def test_log_antiguo_valido_sin_escritura_es_never_success(tmp_path: pathlib.Path) -> None:
    """Log antiguo válido + corrida sin escribir log + nuevo .nif ⇒ jamás SUCCESS."""
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    # Log ANTIGUO del run anterior — firmado y limpio.
    (exe.parent / "Log_BS.txt").write_text("[#Log 2026-11-20]\nAll sets processed successfully!\n", encoding="utf-8")
    destino = _destino(tmp_path)

    def _efecto(*_a, **_k):
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        return (b"", b"", 0)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", AsyncMock(side_effect=_efecto)):
        result = await runner.run_batch("CBBE", str(destino), manifest=ManifiestoArtefactos(nif_requeridos=1))

    assert result.success is False
    assert result.artifacts_built == 1


async def test_log_de_la_corrida_se_atribuye(tmp_path: pathlib.Path) -> None:
    """Run que escribe log con firma nueva → ATRIBUIBLE (no se atribuye el log viejo).

    P1-E: aserciones independientes — no una OR tautológica ("estado !=
    EVIDENCE_INCOMPLETE o artifacts_built == 1") que pasa siempre.
    """
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    destino = _destino(tmp_path)

    def _efecto(*_a, **_k):
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        (exe.parent / "Log_BS.txt").write_text(
            "[#Log 2026-11-22]\nAll sets processed successfully!\n", encoding="utf-8"
        )
        return (b"", b"", 0)

    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", AsyncMock(side_effect=_efecto)):
        result = await runner.run_batch("CBBE", str(destino), manifest=ManifiestoArtefactos(nif_requeridos=1))

    # Atribución: el log escrito por la corrida se atribuye — el veredicto
    # NO es EVIDENCE_INCOMPLETE "log ausente o no atribuible". La causa del
    # veredicto es la del manifiesto (solo conteo), no la del log.
    assert result.artifacts_built == 1, "el artefacto REALMENTE se cuenta"
    assert result.success is False, "manifiesto sin verificar no puede SUCCESS"
    assert result.estado == "EVIDENCE_INCOMPLETE"
    assert "no atribuible" not in result.message.lower()
    assert "manifiesto" in result.message.lower()


# --------------------------------------------------------------------------- #
# P1-D — corrida nueva con firma/texto idéntico al previo (append + rotación)
# --------------------------------------------------------------------------- #


def test_atribuir_log_con_firma_identica_en_append_atribuye(tmp_path: pathlib.Path) -> None:
    """P1-D: run NUEVO cuyo log es idéntico al previo (append de la misma [#Log]) → atribuible.

    El log previo termina con [#Log 2026-11-22] (misma fecha textual del run
    nuevo). El append agrega una firma nueva — el archivo CRECE (la firma del
    run nuevo queda DENTRO de la región nueva, offset > tamaño del snapshot) —
    y esa firma nueva NO es la firma vieja, aunque el texto sea idéntico.
    """
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    viejo = "[#Log 2026-11-22]\nold-marker-OLD\n"
    (exe.parent / "Log_BS.txt").write_text(viejo, encoding="utf-8")
    pre = _snapshot_log_inicial(exe)
    assert pre is not None

    # El run NUEVO appenda su firma + su propio log — append real, el archivo crece.
    (exe.parent / "Log_BS.txt").write_text(viejo + "[#Log 2026-11-22]\nfresh-marker-NEW\n", encoding="utf-8")

    cola = _atribuir_log_a_corrida(exe, pre)

    assert cola.atribuible is True, "la firma nueva (append después del snapshot) debe ser atribuible"
    assert "fresh-marker-NEW" in cola.texto
    assert "old-marker-OLD" not in cola.texto, "el contenido viejo no puede atribuirse a esta corrida"


def test_atribuir_log_con_rotacion_atribuye_sin_contenido_viejo(tmp_path: pathlib.Path) -> None:
    """P1-D: rotación del archivo (>= 4 firmas) que conserva las últimas dos → atribuible.

    Log::Initialize copia desde la 2ª firma hasta EOF y agrega la firma nueva.
    Nunca se atribuyen las líneas de runs anteriores (B/C/D) — sólo lo que
    sigue a la firma nueva.
    """
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    viejo = (
        "[#Log 2026-11-19]\nano-marker\n"
        "[#Log 2026-11-20]\ndos-marker\n"
        "[#Log 2026-11-21]\ntres-marker\n"
        "[#Log 2026-11-22]\ncuatro-marker\n"
    )
    (exe.parent / "Log_BS.txt").write_text(viejo, encoding="utf-8")
    pre = _snapshot_log_inicial(exe)
    assert pre is not None

    # Rotado: conserva desde la 2ª firma (dos/tres/cuatro) + firma nueva (misma fecha textual).
    rotado = (
        "[#Log 2026-11-20]\ndos-marker\n"
        "[#Log 2026-11-21]\ntres-marker\n"
        "[#Log 2026-11-22]\ncuatro-marker\n"
        "[#Log 2026-11-22]\nfresh-marker-NEW\n"
    )
    (exe.parent / "Log_BS.txt").write_text(rotado, encoding="utf-8")

    cola = _atribuir_log_a_corrida(exe, pre)

    assert cola.atribuible is True, "la firma nueva tras la rotación debe ser atribuible"
    assert "fresh-marker-NEW" in cola.texto
    for viejo in ("dos-marker", "tres-marker", "cuatro-marker", "ano-marker"):
        assert viejo not in cola.texto, f"contenido viejo '{viejo}' no puede atribuirse a esta corrida"


def test_atribuir_log_sin_firma_nueva_ni_cambio_de_contenido_no_atribuye(tmp_path: pathlib.Path) -> None:
    """Guard: archivo cambiado pero el contenido/las firmas no nuevas → no atribuye (P0-3)."""
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True)
    viejo = "[#Log 2026-11-22]\nold-marker-OLD\n"
    (exe.parent / "Log_BS.txt").write_text(viejo, encoding="utf-8")
    pre = _snapshot_log_inicial(exe)
    assert pre is not None

    # Archivo REESCRITO con el mismo contenido (no hay escritura): relato adversarial.
    (exe.parent / "Log_BS.txt").write_text(viejo, encoding="utf-8")

    cola = _atribuir_log_a_corrida(exe, pre)

    assert cola.atribuible is False

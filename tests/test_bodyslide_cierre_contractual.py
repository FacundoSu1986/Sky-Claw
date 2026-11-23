"""Cierre contractual de PR #1003: preset determinista, terminalidad, identidad de grupo y log manipulado.

Los tests de las tres primeras secciones se escribieron ANTES del fix y se
verificaron ROJOS sobre ``2951a09a`` (evidencia en el cuerpo del PR):

- **Preset determinista.** Sin ``-p``, ``CommandLineBuild`` construye con el
  ``SelectedPreset`` que la GUI dejó guardado en ``BodySlide.xml`` (upstream
  v6.2.0 ``BodySlideAppBuild.cpp``: ``activePreset =
  BodySlideConfig["SelectedPreset"]`` en el batch; el override existe sólo con
  ``cmdPreset``). El preset efectivo es entonces desconocido para el plan: el
  veredicto compartido no puede declarar SUCCESS. La invocación sin ``-p``
  sigue siendo válida, pero termina en ``EVIDENCE_INCOMPLETE``.
- **Terminalidad.** El runner exige el Job Object ``KILL_ON_JOB_CLOSE`` de
  ``run_capture`` (``require_job=True``), igual que sus hermanos
  ``synthesis_runner`` y ``xedit_bridge_client``: si Windows no lo asigna, la
  corrida falla cerrada. NO reemplaza al broker USVFS de PR 2 ni elimina la
  micro-ventana anterior a la asignación del PID.
- **Identidad de grupo.** En NTFS ``CBBE`` y ``cbbe`` son la MISMA carpeta: un
  grupo cuyo nombre sólo difiere en mayúsculas de una carpeta existente del
  staging no puede tratarse como un destino independiente. El chequeo corre en
  los DOS caminos que llegan al staging: el runner (público) y la tool del
  agente ANTES de ``DirectoryRollback``, que aparta la carpeta con el nombre
  pedido y borra la grafía original.

La última sección es adversarial: un ``Log_BS.txt`` viejo o manipulado que
simula éxito nunca produce SUCCESS sin salida verificable de la corrida actual.
"""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sky_claw.local.tools._process as proceso_mod
import sky_claw.local.tools.bodyslide_runner as mod
from sky_claw.local.tools.bodyslide_headless import (
    EvidenciaDeBuild,
    HeadlessContractError,
    ManifiestoArtefactos,
    evaluar_veredicto,
    exigir_identidad_de_grupo_unica,
)
from sky_claw.local.tools.bodyslide_runner import (
    BodySlideConfig,
    BodySlideExecutionError,
    BodySlideRunner,
)

_CONFIG_VÁLIDA = "<Config>\n  <TargetGame>4</TargetGame>\n</Config>\n"
_MANIFIESTO = ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture:cierre")
_LOG_DE_EXITO = "[#Log 2026-11-23]\nAll command-line build sets processed successfully!\n"


def _runner(tmp_path: pathlib.Path) -> BodySlideRunner:
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    (exe.parent / "Config.xml").write_text(_CONFIG_VÁLIDA, encoding="utf-8")
    return BodySlideRunner(BodySlideConfig(bodyslide_exe=exe, game_path=tmp_path / "game", quiet_verified=True))


def _raiz(tmp_path: pathlib.Path) -> pathlib.Path:
    return (tmp_path / "game").resolve() / "BodySlide_Output"


def _log(runner: BodySlideRunner) -> pathlib.Path:
    return runner.config.bodyslide_exe.parent / "Log_BS.txt"


def _build_real(destino: pathlib.Path, log: pathlib.Path) -> AsyncMock:
    """Simula una corrida legítima: firma ``[#Log`` NUEVA (append) + un ``.nif`` nuevo."""

    def _efecto(*_a, **_k):
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        with log.open("ab") as handle:
            handle.write(_LOG_DE_EXITO.encode("utf-8"))
        return (b"", b"", 0)

    return AsyncMock(side_effect=_efecto)


async def _correr(runner: BodySlideRunner, grupo: str, destino: pathlib.Path, proceso, **opciones):
    with patch.object(mod, "run_capture", proceso):
        return await runner.run_batch(grupo, str(destino), **opciones)


# --------------------------------------------------------------------------- #
# P1 — Preset determinista (contrato de veredicto compartido)
# --------------------------------------------------------------------------- #


def _evidencia_completa(**cambios: object) -> EvidenciaDeBuild:
    base: dict[str, object] = {"return_code": 0, "log_atribuible": True, "artefactos_nif": 1}
    base.update(cambios)
    return EvidenciaDeBuild(**base)  # type: ignore[arg-type]


def test_veredicto_sin_evidencia_del_preset_efectivo_no_es_success() -> None:
    """Evidencia completa + manifiesto verificado, pero el preset efectivo es desconocido."""
    veredicto = evaluar_veredicto(_evidencia_completa(), _MANIFIESTO)
    assert veredicto.estado == "EVIDENCE_INCOMPLETE"
    assert "preset" in veredicto.razon.lower()


def test_veredicto_con_preset_efectivo_verificado_es_success() -> None:
    veredicto = evaluar_veredicto(_evidencia_completa(preset_efectivo_verificado=True), _MANIFIESTO)
    assert veredicto.estado == "SUCCESS"


@pytest.mark.parametrize(
    ("cambios", "estado"),
    [
        ({"return_code": 1}, "FAILURE"),
        ({"artefactos_nif": 0}, "FAILURE"),
        ({"log_contiene_fallo_parcial": True}, "FAILURE"),
        ({"proceso_terminado": False}, "EVIDENCE_INCOMPLETE"),
    ],
)
def test_preset_desconocido_no_enmascara_un_fallo(cambios: dict[str, object], estado: str) -> None:
    """La regla del preset sólo puede bajar un SUCCESS: un FAILURE sigue siendo FAILURE."""
    assert evaluar_veredicto(_evidencia_completa(**cambios), _MANIFIESTO).estado == estado


async def test_runner_sin_preset_spawnea_sin_p_y_devuelve_evidence_incomplete(tmp_path: pathlib.Path) -> None:
    """Compatibilidad de invocación: sin preset se lanza igual (sin ``-p``), pero nunca SUCCESS."""
    runner = _runner(tmp_path)
    destino = _raiz(tmp_path) / "CBBE"
    proceso = _build_real(destino, _log(runner))
    result = await _correr(runner, "CBBE", destino, proceso, manifest=_MANIFIESTO)
    assert proceso.await_count == 1
    assert "-p" not in proceso.await_args.args[0]
    assert result.success is False
    assert result.estado == "EVIDENCE_INCOMPLETE"
    assert "preset" in result.message.lower()


async def test_runner_con_preset_no_verificado_no_spawnea(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    destino = _raiz(tmp_path) / "CBBE"
    proceso = _build_real(destino, _log(runner))
    result = await _correr(runner, "CBBE", destino, proceso, preset="Zeroed", manifest=_MANIFIESTO)
    assert proceso.await_count == 0
    assert result.estado == "ARGS_INVALID"


async def test_runner_con_preset_verificado_es_success(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    destino = _raiz(tmp_path) / "CBBE"
    proceso = _build_real(destino, _log(runner))
    result = await _correr(
        runner, "CBBE", destino, proceso, preset="Zeroed", preset_verified=True, manifest=_MANIFIESTO
    )
    argv = proceso.await_args.args[0]
    assert argv[argv.index("-p") + 1] == "Zeroed"
    assert result.estado == "SUCCESS", result.message
    assert result.success is True


# --------------------------------------------------------------------------- #
# P1 — Terminalidad: Job Object obligatorio
# --------------------------------------------------------------------------- #


async def test_runner_exige_job_object_a_run_capture(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    destino = _raiz(tmp_path) / "CBBE"
    proceso = _build_real(destino, _log(runner))
    await _correr(runner, "CBBE", destino, proceso, preset="Zeroed", preset_verified=True, manifest=_MANIFIESTO)
    assert proceso.await_args.kwargs.get("require_job") is True


async def test_fallo_de_asignacion_del_job_falla_cerrado_y_restaura_config(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_capture`` REAL con un hijo real: si Windows no asigna el Job, la corrida no sigue.

    El hijo es el intérprete de Python (rechaza ``-gbuild`` y sale con error): lo
    que importa es que el runner no llegue a esperar su resultado ni a contar
    artefactos. Sin Job no hay terminalidad del árbol que afirmar.
    """
    runner = _runner(tmp_path)
    # El cwd del hijo es game_path: tiene que existir para que el spawn ocurra
    # de verdad y el fallo observado sea el del Job, no el del directorio.
    (tmp_path / "game").mkdir()
    config = runner.config.bodyslide_exe.parent / "Config.xml"
    original = config.read_bytes()
    monkeypatch.setattr(proceso_mod.sys, "platform", "win32")
    monkeypatch.setattr(proceso_mod, "assign_kill_on_close_job", lambda _pid: None)

    async def _run_capture_real_con_python(args, **kwargs):
        return await proceso_mod.run_capture([sys.executable, *args[1:]], **kwargs)

    destino = _raiz(tmp_path) / "CBBE"
    with pytest.raises(BodySlideExecutionError):
        await _correr(
            runner,
            "CBBE",
            destino,
            _run_capture_real_con_python,
            preset="Zeroed",
            preset_verified=True,
            manifest=_MANIFIESTO,
        )
    assert config.read_bytes() == original, "un fallo de Job no puede dejar la config sembrada"


# --------------------------------------------------------------------------- #
# P1 — Identidad de grupo: equivalencia por mayúsculas (NTFS)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("grupo", "existentes"),
    [
        ("cbbe", ["CBBE"]),
        ("Cbbe", ["3BA", "CBBE"]),
        ("CBBE", ["cbbe"]),
        ("bhunp 3bbb advanced", ["BHUNP 3BBB Advanced"]),
    ],
)
def test_identidad_equivalente_por_mayusculas_se_rechaza(grupo: str, existentes: list[str]) -> None:
    with pytest.raises(HeadlessContractError, match="mayúsculas"):
        exigir_identidad_de_grupo_unica(grupo, existentes)


@pytest.mark.parametrize(
    ("grupo", "existentes"),
    [
        ("CBBE", []),
        ("CBBE", ["CBBE"]),
        ("CBBE", ["3BA", "CBBE Body", "CBBE.rollback-1"]),
        ("3BA", ["CBBE"]),
    ],
)
def test_identidad_unica_o_misma_grafia_se_acepta(grupo: str, existentes: list[str]) -> None:
    exigir_identidad_de_grupo_unica(grupo, existentes)


async def test_runner_rechaza_grupo_equivalente_por_mayusculas_sin_spawn(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    existente = _raiz(tmp_path) / "CBBE"
    existente.mkdir(parents=True)
    (existente / "previo.nif").write_bytes(b"\x00")
    config = runner.config.bodyslide_exe.parent / "Config.xml"
    original = config.read_bytes()
    destino = _raiz(tmp_path) / "cbbe"
    proceso = _build_real(destino, _log(runner))
    result = await _correr(runner, "cbbe", destino, proceso, preset="Zeroed", preset_verified=True)
    assert proceso.await_count == 0
    assert result.estado == "ARGS_INVALID"
    assert "mayúsculas" in result.message
    assert config.read_bytes() == original


@pytest.fixture
async def lock_manager(tmp_path: pathlib.Path):
    from sky_claw.app.db.locks import DistributedLockManager

    mgr = DistributedLockManager(tmp_path / "locks.db", default_ttl=5.0, max_retries=2, backoff_base=0.05)
    await mgr.initialize()
    yield mgr
    await mgr.close()


@pytest.fixture
def snapshot_manager(tmp_path: pathlib.Path):
    from sky_claw.app.db.snapshot_manager import FileSnapshotManager

    d = tmp_path / "snapshots"
    d.mkdir()
    return FileSnapshotManager(snapshot_dir=d)


async def test_tool_del_agente_rechaza_grupo_equivalente_antes_del_move_aside(
    tmp_path: pathlib.Path, lock_manager, snapshot_manager
) -> None:
    """Camino de producción: el chequeo debe correr ANTES de ``DirectoryRollback``.

    Si corriera después, en NTFS el move-aside de ``.../cbbe`` ya habría
    renombrado la carpeta ``CBBE`` a ``cbbe.rollback-N`` y la grafía original
    —la única evidencia de la colisión— ya no estaría.
    """
    from sky_claw.app.agent.tools.system_tools import run_bodyslide_batch

    existente = _raiz(tmp_path) / "CBBE"
    existente.mkdir(parents=True)
    (existente / "previo.nif").write_bytes(b"\x00")
    runner = MagicMock()
    runner.config = SimpleNamespace(game_path=tmp_path / "game")
    runner.run_batch = AsyncMock()

    out = json.loads(
        await run_bodyslide_batch(runner, group="cbbe", lock_manager=lock_manager, snapshot_manager=snapshot_manager)
    )

    assert out["success"] is False
    assert "mayúsculas" in out["message"]
    runner.run_batch.assert_not_awaited()
    assert sorted(p.name for p in _raiz(tmp_path).iterdir()) == ["CBBE"], "no hubo move-aside"
    assert (existente / "previo.nif").read_bytes() == b"\x00"


# --------------------------------------------------------------------------- #
# Adversarial — Log_BS.txt viejo o manipulado que simula éxito
# --------------------------------------------------------------------------- #

_OPCIONES_COMPLETAS = {"preset": "Zeroed", "preset_verified": True, "manifest": _MANIFIESTO}


@pytest.mark.parametrize("con_artefacto_nuevo", [False, True])
async def test_log_viejo_de_exito_intacto_nunca_es_success(tmp_path: pathlib.Path, con_artefacto_nuevo: bool) -> None:
    """El log de una corrida ANTERIOR dice "processed successfully" y la corrida actual no lo toca.

    Con artefacto nuevo: aun con salida real de esta corrida, un log no
    atribuible no certifica nada (mutación: quitar el gate de atribución de
    ``evaluar_veredicto`` vuelve SUCCESS este caso).
    """
    runner = _runner(tmp_path)
    _log(runner).write_text(_LOG_DE_EXITO, encoding="utf-8")
    destino = _raiz(tmp_path) / "CBBE"

    def _efecto(*_a, **_k):
        if con_artefacto_nuevo:
            destino.mkdir(parents=True, exist_ok=True)
            (destino / "cuerpo.nif").write_bytes(b"\x00")
        return (b"", b"", 0)

    result = await _correr(runner, "CBBE", destino, AsyncMock(side_effect=_efecto), **_OPCIONES_COMPLETAS)
    assert result.success is False
    assert result.estado != "SUCCESS"


async def test_log_falsificado_con_firma_nueva_sin_salida_de_la_corrida_no_es_success(tmp_path: pathlib.Path) -> None:
    """Alguien reescribe el log con firmas NUEVAS y "éxito", pero la corrida no produjo mallas."""
    runner = _runner(tmp_path)
    _log(runner).write_text("[#Log 2026-11-20]\nFailed to build 'X': y\n", encoding="utf-8")
    destino = _raiz(tmp_path) / "CBBE"

    def _efecto(*_a, **_k):
        _log(runner).write_text(_LOG_DE_EXITO + _LOG_DE_EXITO, encoding="utf-8")
        return (b"", b"", 0)

    result = await _correr(runner, "CBBE", destino, AsyncMock(side_effect=_efecto), **_OPCIONES_COMPLETAS)
    assert result.success is False
    assert result.estado == "FAILURE"


async def test_log_falsificado_con_mallas_preexistentes_no_es_success(tmp_path: pathlib.Path) -> None:
    """Firma nueva y "éxito" en el log, pero las mallas ya estaban: no son salida de esta corrida."""
    runner = _runner(tmp_path)
    destino = _raiz(tmp_path) / "CBBE"
    destino.mkdir(parents=True)
    (destino / "cuerpo.nif").write_bytes(b"\x00")

    def _efecto(*_a, **_k):
        with _log(runner).open("ab") as handle:
            handle.write(_LOG_DE_EXITO.encode("utf-8"))
        return (b"", b"", 0)

    result = await _correr(runner, "CBBE", destino, AsyncMock(side_effect=_efecto), **_OPCIONES_COMPLETAS)
    assert result.success is False
    assert result.estado == "FAILURE"


async def test_log_manipulado_in_place_sin_firma_nueva_no_es_success(tmp_path: pathlib.Path) -> None:
    """Misma firma, mismo offset, mismo conteo: se borra "Failed to build" in situ durante la corrida."""
    runner = _runner(tmp_path)
    viejo = "[#Log 2026-11-23]\nFailed to build 'X': y\n"
    _log(runner).write_text(viejo, encoding="utf-8")
    destino = _raiz(tmp_path) / "CBBE"

    def _efecto(*_a, **_k):
        _log(runner).write_text(viejo.replace("Failed to build", "Built ok here!!"), encoding="utf-8")
        destino.mkdir(parents=True, exist_ok=True)
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        return (b"", b"", 0)

    result = await _correr(runner, "CBBE", destino, AsyncMock(side_effect=_efecto), **_OPCIONES_COMPLETAS)
    assert result.success is False
    assert result.estado == "EVIDENCE_INCOMPLETE"


# --------------------------------------------------------------------------- #
# Review Codex #1003 — la restauración es terminal y atómica
# --------------------------------------------------------------------------- #


def _proceso_sin_write_bytes(destino: pathlib.Path, log: pathlib.Path) -> AsyncMock:
    """Corrida legítima que no usa ``Path.write_bytes`` (los tests de abajo lo intervienen)."""

    def _efecto(*_a, **_k):
        destino.mkdir(parents=True, exist_ok=True)
        with (destino / "cuerpo.nif").open("wb") as handle:
            handle.write(b"\x00")
        with log.open("ab") as handle:
            handle.write(_LOG_DE_EXITO.encode("utf-8"))
        return (b"", b"", 0)

    return AsyncMock(side_effect=_efecto)


async def test_cancelar_durante_la_restauracion_espera_a_que_termine(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelar la corrida mientras restaura NO suelta al caller antes de que el hilo termine.

    Si soltara, ``run_bodyslide_batch`` liberaría el lock con la restauración todavía
    en vuelo y esa escritura tardía pisaría la config del dueño siguiente (mismo
    defecto que el P1 de #993, resuelto con ``mutar_hasta_terminal``).
    """
    import asyncio
    import threading

    runner = _runner(tmp_path)
    en_curso = threading.Event()
    liberar = threading.Event()
    terminada = threading.Event()
    original = mod._snapshot_y_restaurar_archivos

    def _snapshot_con_restauracion_lenta(exe):
        snapshot, restaurar = original(exe)

        def _lenta():
            en_curso.set()
            liberar.wait(10.0)  # guarda contra cuelgues, no sincronización
            resultado = restaurar()
            terminada.set()
            return resultado

        return snapshot, _lenta

    monkeypatch.setattr(mod, "_snapshot_y_restaurar_archivos", _snapshot_con_restauracion_lenta)
    destino = _raiz(tmp_path) / "CBBE"
    with patch.object(mod, "run_capture", _proceso_sin_write_bytes(destino, _log(runner))):
        tarea = asyncio.create_task(
            runner.run_batch("CBBE", str(destino), preset="Zeroed", preset_verified=True, manifest=_MANIFIESTO)
        )
        assert await asyncio.to_thread(en_curso.wait, 10.0)
        tarea.cancel()
        hechas, _ = await asyncio.wait({tarea}, timeout=0.3)
        assert not hechas, "la corrida salió con la restauración todavía en vuelo"
        liberar.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    assert terminada.is_set()
    assert (runner.config.bodyslide_exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG_VÁLIDA


async def test_restauracion_con_escritura_parcial_no_trunca_la_config(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un fallo a mitad de la escritura (disco lleno) no puede dejar ``Config.xml`` truncado."""
    runner = _runner(tmp_path)
    config = runner.config.bodyslide_exe.parent / "Config.xml"
    original = mod._snapshot_y_restaurar_archivos

    def _escritura_parcial(self: pathlib.Path, data: bytes) -> int:
        with self.open("wb") as handle:
            handle.write(data[:7])
        raise OSError(28, "No space left on device")

    def _snapshot_con_disco_lleno(exe):
        snapshot, restaurar = original(exe)

        def _restaurar_con_disco_lleno():
            with patch.object(pathlib.Path, "write_bytes", _escritura_parcial):
                return restaurar()

        return snapshot, _restaurar_con_disco_lleno

    monkeypatch.setattr(mod, "_snapshot_y_restaurar_archivos", _snapshot_con_disco_lleno)
    destino = _raiz(tmp_path) / "CBBE"
    with pytest.raises(BodySlideExecutionError):
        await _correr(
            runner,
            "CBBE",
            destino,
            _proceso_sin_write_bytes(destino, _log(runner)),
            preset="Zeroed",
            preset_verified=True,
            manifest=_MANIFIESTO,
        )
    import xml.etree.ElementTree as ET

    ET.fromstring(config.read_bytes())  # XML completo: original o sembrado, nunca truncado
    assert not list(config.parent.glob("*.tmp")), "no quedan temporales de la restauración"

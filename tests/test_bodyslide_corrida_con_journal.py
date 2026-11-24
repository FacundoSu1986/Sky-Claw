"""BodySlide PR 2c-1 — la corrida usa el journal de 2b, con plazo de siembra y cuarentena (C3).

Contrato aprobado por el propietario (2026-11-24):

- una config enlazada se rechaza antes de mutar (retira el soporte P0-2 de PR 1);
- ``LANZADO`` se graba ANTES del spawn; si la corrida se cancela con el proceso lanzado,
  la terminalidad no está demostrada: el journal queda en ``LANZADO`` y la config NO se
  restaura (HOLD). Retorno normal, timeout o error de ``run_capture`` (que mata, cosecha
  y cierra el Job) registran ``TERMINAL`` y restauran;
- la siembra tiene plazo: al vencer, se escribe el marcador de cuarentena ANTES de
  responder ``CONFIG_IO_HUNG``; ninguna corrida nueva arranca con el marcador; si el hilo
  termina en este proceso, restaura y retira el marcador; si no, lo libera el operador.

Producción sigue apagada por ``quiet_verified`` (``CAPABILITY_UNKNOWN``) y por la barrera.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.local.tools import bodyslide_journal as bj
from sky_claw.local.tools.bodyslide_config import calcular_config_sembrada, sembrar_config_de_bodyslide
from sky_claw.local.tools.bodyslide_headless import ManifiestoArtefactos
from sky_claw.local.tools.bodyslide_journal import AlmacenDeJournal, identidad_de_instancia
from sky_claw.local.tools.bodyslide_runner import (
    BodySlideConfig,
    BodySlideRunner,
    BodySlideTimeoutError,
)

_CONFIG = "<Config>\n  <TargetGame>4</TargetGame>\n  <ProjectPath>/mis/proyectos</ProjectPath>\n</Config>\n"
_APP = "<BodySlideConfig><SelectedPreset>Viejo</SelectedPreset></BodySlideConfig>\n"
_MANIFIESTO = ManifiestoArtefactos(nif_requeridos=1, verificado=True, procedencia="fixture")


def _almacen(tmp_path: pathlib.Path) -> AlmacenDeJournal:
    return AlmacenDeJournal(tmp_path / "state" / "bodyslide_journal", endurecer=lambda _p: None)


def _runner(tmp_path: pathlib.Path, *, plazo: float = 30.0) -> BodySlideRunner:
    exe = tmp_path / "BodySlide" / "BodySlide.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    (exe.parent / "Config.xml").write_text(_CONFIG, encoding="utf-8")
    (exe.parent / "BodySlide.xml").write_text(_APP, encoding="utf-8")
    return BodySlideRunner(
        BodySlideConfig(
            bodyslide_exe=exe,
            game_path=tmp_path / "game",
            quiet_verified=True,
            expected_target_game=4,
            almacen_de_journal=_almacen(tmp_path),
            plazo_de_siembra_segundos=plazo,
        )
    )


def _destino(tmp_path: pathlib.Path) -> pathlib.Path:
    destino = tmp_path / "game" / "BodySlide_Output" / "CBBE"
    destino.mkdir(parents=True)
    return destino


def _ruta_del_journal(runner: BodySlideRunner) -> pathlib.Path:
    almacen = runner.config.almacen_de_journal
    assert almacen is not None
    return almacen.ruta_de(identidad_de_instancia(runner.config.bodyslide_exe.parent))


def _estado_en_disco(ruta: pathlib.Path) -> str:
    estado: str = json.loads(ruta.read_text(encoding="utf-8"))["estado"]
    return estado


async def _correr(runner: BodySlideRunner, destino: pathlib.Path):  # type: ignore[no-untyped-def]
    return await runner.run_batch("CBBE", str(destino), manifest=_MANIFIESTO, preset="Zeroed", preset_verified=True)


def _efecto_que_construye(destino: pathlib.Path, exe: pathlib.Path, observado: dict[str, object]):  # type: ignore[no-untyped-def]
    def _efecto(*_a: object, **_k: object) -> tuple[bytes, bytes, int]:
        ruta = observado["journal"]
        assert isinstance(ruta, pathlib.Path)
        observado["estado_al_spawn"] = _estado_en_disco(ruta)
        observado["config_al_spawn"] = (exe.parent / "Config.xml").read_bytes()
        (destino / "cuerpo.nif").write_bytes(b"\x00")
        (exe.parent / "BodySlide.xml").write_text(
            "<BodySlideConfig><SelectedPreset>Nuevo</SelectedPreset></BodySlideConfig>\n", encoding="utf-8"
        )
        (exe.parent / "Log_BS.txt").write_text("[#Log]\nAll sets processed successfully!\n", encoding="utf-8")
        return (b"", b"", 0)

    return _efecto


# ---------------------------------------------------------------------------
# Siembra: el cálculo puro es exactamente lo que se escribe
# ---------------------------------------------------------------------------


def test_calcular_la_siembra_da_los_mismos_bytes_que_sembrar(tmp_path: pathlib.Path) -> None:
    # Arrange: dos carpetas idénticas
    uno, dos = tmp_path / "uno", tmp_path / "dos"
    for carpeta in (uno, dos):
        carpeta.mkdir()
        (carpeta / "Config.xml").write_text(_CONFIG, encoding="utf-8")
    kwargs = {"game_path": tmp_path / "game", "output_root": tmp_path / "salida", "expected_target_game": 4}
    # Act
    calculado = calcular_config_sembrada(exe_dir=uno, **kwargs)
    sembrar_config_de_bodyslide(exe_dir=dos, **kwargs)
    # Assert: el cálculo no escribe y coincide byte a byte con la siembra real.
    assert (uno / "Config.xml").read_text(encoding="utf-8") == _CONFIG
    assert calculado == (dos / "Config.xml").read_bytes()


# ---------------------------------------------------------------------------
# Corrida normal: journal antes de mutar, LANZADO antes del spawn, cierre limpio
# ---------------------------------------------------------------------------


async def test_la_corrida_registra_lanzado_antes_del_spawn_y_cierra_el_journal(tmp_path: pathlib.Path) -> None:
    # Arrange
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)
    observado: dict[str, object] = {"journal": _ruta_del_journal(runner)}
    # Act
    with patch(
        "sky_claw.local.tools.bodyslide_runner.run_capture",
        AsyncMock(side_effect=_efecto_que_construye(destino, exe, observado)),
    ):
        resultado = await _correr(runner, destino)
    # Assert
    assert observado["estado_al_spawn"] == "LANZADO"
    assert observado["config_al_spawn"] != original, "al spawn la config ya estaba sembrada"
    assert resultado.success is True
    assert (exe.parent / "Config.xml").read_bytes() == original
    assert (exe.parent / "BodySlide.xml").read_text(encoding="utf-8") == _APP
    assert set(resultado.config_restored) == {"Config.xml", "BodySlide.xml"}
    assert not _ruta_del_journal(runner).exists()


async def test_cancelar_con_el_proceso_lanzado_deja_hold_sin_restaurar(tmp_path: pathlib.Path) -> None:
    # Arrange: la corrida se cancela mientras run_capture espera al proceso.
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)

    async def _cancelado(*_a: object, **_k: object) -> tuple[bytes, bytes, int]:
        raise asyncio.CancelledError

    # Act
    with (
        patch("sky_claw.local.tools.bodyslide_runner.run_capture", AsyncMock(side_effect=_cancelado)),
        pytest.raises(asyncio.CancelledError),
    ):
        await _correr(runner, destino)
    # Assert: sin terminalidad demostrada no se toca la config; el journal la retiene.
    ruta = _ruta_del_journal(runner)
    assert _estado_en_disco(ruta) == "LANZADO"
    assert (exe.parent / "Config.xml").read_bytes() != original


async def test_timeout_registra_terminal_y_restaura(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    original = (exe.parent / "Config.xml").read_bytes()
    destino = _destino(tmp_path)
    with (
        patch("sky_claw.local.tools.bodyslide_runner.run_capture", AsyncMock(side_effect=TimeoutError)),
        pytest.raises(BodySlideTimeoutError),
    ):
        await _correr(runner, destino)
    assert (exe.parent / "Config.xml").read_bytes() == original
    assert not _ruta_del_journal(runner).exists()


async def test_la_restauracion_fallida_es_restore_failed_y_conserva_el_journal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    destino = _destino(tmp_path)
    observado: dict[str, object] = {"journal": _ruta_del_journal(runner)}
    real = bj._reemplazar_atomico

    def _disco_lleno(destino_archivo: pathlib.Path, blob: bytes) -> None:
        if destino_archivo.name == "Config.xml" and observado.get("estado_al_spawn"):
            raise OSError(28, "No space left on device")
        real(destino_archivo, blob)

    monkeypatch.setattr(bj, "_reemplazar_atomico", _disco_lleno)
    with patch(
        "sky_claw.local.tools.bodyslide_runner.run_capture",
        AsyncMock(side_effect=_efecto_que_construye(destino, exe, observado)),
    ):
        resultado = await _correr(runner, destino)
    assert resultado.success is False
    assert resultado.estado == "RESTORE_FAILED"
    assert _estado_en_disco(_ruta_del_journal(runner)) == "RESTAURACION_FALLIDA"


# ---------------------------------------------------------------------------
# Precondiciones que fallan cerrado ANTES de mutar
# ---------------------------------------------------------------------------


async def test_config_enlazada_se_rechaza_antes_de_mutar(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    afuera = tmp_path / "afuera.xml"
    afuera.write_text(_APP, encoding="utf-8")
    (exe.parent / "BodySlide.xml").unlink()
    try:
        (exe.parent / "BodySlide.xml").symlink_to(afuera)
    except OSError:
        pytest.skip("sin privilegio para crear enlaces simbólicos")
    run_capture = AsyncMock()
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        resultado = await _correr(runner, _destino(tmp_path))
    assert resultado.estado == "CONFIG_PRECONDITION_FAILED"
    assert run_capture.await_count == 0
    assert (exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG
    assert not _ruta_del_journal(runner).exists()


async def test_journal_pendiente_impide_una_corrida_nueva(tmp_path: pathlib.Path) -> None:
    # Arrange: una corrida anterior quedó sin recuperar.
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    almacen = runner.config.almacen_de_journal
    assert almacen is not None
    bj.iniciar_transaccion(
        almacen=almacen,
        identidad=bj.IdentidadDeEjecucion(maquina="m", instancia=identidad_de_instancia(exe.parent)),
        exe_dir=exe.parent,
        bytes_sembrados=b"<Config/>",
    )
    run_capture = AsyncMock()
    # Act
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        resultado = await _correr(runner, _destino(tmp_path))
    # Assert
    assert resultado.estado == "PRECONDITION_FAILED"
    assert run_capture.await_count == 0
    assert (exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG


async def test_sin_identidad_de_maquina_no_se_muta(tmp_path: pathlib.Path) -> None:
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    run_capture = AsyncMock()
    with (
        patch("sky_claw.local.tools.bodyslide_runner.identidad_actual", return_value=None),
        patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture),
    ):
        resultado = await _correr(runner, _destino(tmp_path))
    assert resultado.estado == "CONFIG_PRECONDITION_FAILED"
    assert run_capture.await_count == 0
    assert (exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG


# ---------------------------------------------------------------------------
# C3: plazo de siembra, cuarentena persistente y liberación
# ---------------------------------------------------------------------------


def _esperar(condicion, segundos: float = 15.0) -> bool:  # type: ignore[no-untyped-def]
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.02)
    return False


async def test_siembra_colgada_responde_config_io_hung_y_pone_en_cuarentena(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: la siembra se bloquea de verdad (un evento que no se setea hasta el final).
    runner = _runner(tmp_path, plazo=0.2)
    exe = runner.config.bodyslide_exe
    almacen = runner.config.almacen_de_journal
    assert almacen is not None
    liberar = threading.Event()
    sembrar_real = bj.TransaccionDeConfig.sembrar

    def _sembrar_colgado(self: bj.TransaccionDeConfig) -> None:
        liberar.wait(30)
        sembrar_real(self)

    monkeypatch.setattr(bj.TransaccionDeConfig, "sembrar", _sembrar_colgado)
    run_capture = AsyncMock()
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        # Act 1: el plazo vence.
        primera = await _correr(runner, _destino(tmp_path))
        # Assert 1: responde sin lanzar y la cuarentena ya está en disco.
        assert primera.estado == "CONFIG_IO_HUNG"
        assert primera.success is False
        assert almacen.marcador_de_cuarentena.exists()
        # Act 2: una corrida nueva con el marcador presente.
        segunda = await runner.run_batch(
            "CBBE",
            str(tmp_path / "game" / "BodySlide_Output" / "CBBE"),
            manifest=_MANIFIESTO,
            preset="Zeroed",
            preset_verified=True,
        )
        # Assert 2: no arranca.
        assert segunda.estado == "PRECONDITION_FAILED"
        assert run_capture.await_count == 0
        # Act 3: el hilo termina en este proceso.
        liberar.set()
        # Assert 3: restaura, cierra el journal y retira la cuarentena él mismo.
        assert _esperar(lambda: not almacen.marcador_de_cuarentena.exists())
    assert (exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG
    assert not _ruta_del_journal(runner).exists()


def test_liberar_la_cuarentena_exige_evidencia(tmp_path: pathlib.Path) -> None:
    almacen = _almacen(tmp_path)
    almacen.asegurar_directorio()
    almacen.marcador_de_cuarentena.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        bj.liberar_cuarentena(almacen, evidencia="   ")
    assert almacen.marcador_de_cuarentena.exists()
    assert bj.liberar_cuarentena(almacen, evidencia="el operador verificó que BodySlide no corre") is True
    assert not almacen.marcador_de_cuarentena.exists()
    assert bj.liberar_cuarentena(almacen, evidencia="de nuevo") is False


def test_el_marcador_de_cuarentena_es_durable_y_legible(tmp_path: pathlib.Path) -> None:
    almacen = _almacen(tmp_path)
    bj.poner_en_cuarentena(almacen, run_id="a" * 32, motivo="plazo de siembra vencido")
    datos = json.loads(almacen.marcador_de_cuarentena.read_text(encoding="utf-8"))
    assert datos["run_id"] == "a" * 32
    assert datos["motivo"] == "plazo de siembra vencido"
    assert datos["recurso"] == "bodyslide-meshes"


# ---------------------------------------------------------------------------
# Arranque: el consumidor bodyslide-config corre después del barrido y es best-effort
# ---------------------------------------------------------------------------


def test_el_arranque_corre_el_consumidor_despues_del_barrido_y_sin_bloquear() -> None:
    import ast

    fuente = (pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "app_context.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    llamadas = {
        nodo.func.id: nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Name)
        and nodo.func.id in {"reconcile_orphan_rollback_backups", "reconcile_bodyslide_config_journals"}
    }
    assert set(llamadas) == {"reconcile_orphan_rollback_backups", "reconcile_bodyslide_config_journals"}
    assert llamadas["reconcile_bodyslide_config_journals"].lineno > llamadas["reconcile_orphan_rollback_backups"].lineno
    # Best-effort: la llamada vive dentro de un try con un handler que atrapa Exception,
    # y NO dentro del try del barrido de move-aside (un fallo de uno no salta al otro).
    consumidor = llamadas["reconcile_bodyslide_config_journals"]
    barrido = llamadas["reconcile_orphan_rollback_backups"]
    tries = [
        nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.Try) and any(n is consumidor for n in ast.walk(nodo))
    ]
    propio = min(tries, key=lambda t: t.end_lineno - t.lineno)
    assert not any(n is barrido for n in ast.walk(propio))
    assert any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in propio.handlers)


async def test_la_cuarentena_del_recurso_bloquea_aunque_no_haya_journal_propio(tmp_path: pathlib.Path) -> None:
    """La cuarentena es del RECURSO bodyslide-meshes, no de una instalación.

    Si otra instalación dejó su siembra colgada, acá no hay journal pendiente: sólo el
    marcador impide arrancar mientras aquel hilo puede seguir escribiendo.
    """
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    almacen = runner.config.almacen_de_journal
    assert almacen is not None
    bj.poner_en_cuarentena(almacen, run_id="b" * 32, motivo="siembra colgada en otra instalación")
    run_capture = AsyncMock()
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        resultado = await _correr(runner, _destino(tmp_path))
    assert resultado.estado == "PRECONDITION_FAILED"
    assert run_capture.await_count == 0
    assert (exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG
    assert not _ruta_del_journal(runner).exists()


# ---------------------------------------------------------------------------
# Review Codex #1021 — cancelaciones en los bordes y plazo independiente del executor
# ---------------------------------------------------------------------------


def _tx_sembrable(tmp_path: pathlib.Path) -> tuple[bj.TransaccionDeConfig, pathlib.Path]:
    exe_dir = tmp_path / "BodySlideTx"
    exe_dir.mkdir()
    (exe_dir / "Config.xml").write_text(_CONFIG, encoding="utf-8")
    tx = bj.iniciar_transaccion(
        almacen=_almacen(tmp_path),
        identidad=bj.IdentidadDeEjecucion(maquina="m", instancia=identidad_de_instancia(exe_dir)),
        exe_dir=exe_dir,
        bytes_sembrados=b"<Config><TargetGame>4</TargetGame><Sembrado/></Config>",
    )
    return tx, exe_dir


async def test_el_plazo_de_siembra_no_depende_del_executor_compartido(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con el executor por defecto saturado, el plazo vence igual y la cuarentena se escribe."""
    import concurrent.futures

    tx, _ = _tx_sembrable(tmp_path)
    liberar_siembra = threading.Event()
    sembrar_real = bj.TransaccionDeConfig.sembrar

    def _colgado(self: bj.TransaccionDeConfig) -> None:
        liberar_siembra.wait(30)
        sembrar_real(self)

    monkeypatch.setattr(bj.TransaccionDeConfig, "sembrar", _colgado)
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    ocupar = threading.Event()
    try:
        loop.run_in_executor(None, ocupar.wait, 30)  # el único worker queda tomado
        with pytest.raises(bj.SiembraColgadaError):
            await asyncio.wait_for(bj.SiembraConPlazo(tx, plazo=0.05).ejecutar(), timeout=5)
        assert tx.almacen.marcador_de_cuarentena.exists()
    finally:
        ocupar.set()
        liberar_siembra.set()
        executor.shutdown(wait=True)


async def test_cancelar_durante_el_abandono_retiene_hasta_dejar_la_cuarentena(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una cancelación mientras se escribe la cuarentena no suelta al caller con ``abandonada=False``."""
    tx, _ = _tx_sembrable(tmp_path)
    liberar_siembra = threading.Event()
    entro = threading.Event()
    liberar_marcador = threading.Event()
    sembrar_real = bj.TransaccionDeConfig.sembrar
    poner_real = bj.poner_en_cuarentena

    def _colgado(self: bj.TransaccionDeConfig) -> None:
        liberar_siembra.wait(30)
        sembrar_real(self)

    def _marcador_lento(almacen: AlmacenDeJournal, **kwargs: str) -> None:
        entro.set()
        liberar_marcador.wait(30)
        poner_real(almacen, **kwargs)

    monkeypatch.setattr(bj.TransaccionDeConfig, "sembrar", _colgado)
    monkeypatch.setattr(bj, "poner_en_cuarentena", _marcador_lento)
    siembra = bj.SiembraConPlazo(tx, plazo=0.05)
    tarea = asyncio.create_task(siembra.ejecutar())
    try:
        assert await asyncio.to_thread(entro.wait, 10)
        tarea.cancel()
        hechas, _ = await asyncio.wait({tarea}, timeout=0.3)
        assert not hechas, "el caller salió con el abandono todavía en vuelo"
        liberar_marcador.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        assert siembra.abandonada is True
        assert tx.almacen.marcador_de_cuarentena.exists()
    finally:
        liberar_marcador.set()
        liberar_siembra.set()


async def test_cancelar_durante_lanzado_registra_terminal_y_restaura(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la cancelación llega mientras se graba LANZADO, el spawn nunca se pidió: se restaura."""
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    entro = threading.Event()
    liberar = threading.Event()
    lanzado_real = bj.TransaccionDeConfig.marcar_lanzado

    def _lanzado_lento(self: bj.TransaccionDeConfig) -> None:
        entro.set()
        liberar.wait(30)
        lanzado_real(self)

    monkeypatch.setattr(bj.TransaccionDeConfig, "marcar_lanzado", _lanzado_lento)
    run_capture = AsyncMock()
    with patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture):
        tarea = asyncio.create_task(_correr(runner, _destino(tmp_path)))
        assert await asyncio.to_thread(entro.wait, 10)
        tarea.cancel()
        liberar.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    assert run_capture.await_count == 0
    assert (exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG
    assert not _ruta_del_journal(runner).exists()


# ---------------------------------------------------------------------------
# Review CodeRabbit #1021 (segunda ronda)
# ---------------------------------------------------------------------------


def test_cr3_un_guardado_entre_clasificar_y_escribir_no_se_pisa(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cada archivo se re-clasifica justo antes de escribirlo: un guardado intermedio frena todo."""
    exe_dir = tmp_path / "BodySlideCR3"
    exe_dir.mkdir()
    (exe_dir / "Config.xml").write_text(_CONFIG, encoding="utf-8")
    (exe_dir / "BodySlide.xml").write_text(_APP, encoding="utf-8")
    tx = bj.iniciar_transaccion(
        almacen=_almacen(tmp_path),
        identidad=bj.IdentidadDeEjecucion(maquina="m", instancia=identidad_de_instancia(exe_dir)),
        exe_dir=exe_dir,
        bytes_sembrados=b"<Config><TargetGame>4</TargetGame><Sembrado/></Config>",
    )
    tx.sembrar()
    tx.marcar_lanzado()
    (exe_dir / "BodySlide.xml").write_text("<BodySlideConfig>corrida</BodySlideConfig>", encoding="utf-8")
    tx.marcar_terminal()
    guardado = "<BodySlideConfig>guardado-del-usuario</BodySlideConfig>"
    real = bj._reemplazar_atomico

    def _usuario_guarda_en_el_medio(destino: pathlib.Path, blob: bytes) -> None:
        if destino.name == "Config.xml":
            (exe_dir / "BodySlide.xml").write_text(guardado, encoding="utf-8")
        real(destino, blob)

    monkeypatch.setattr(bj, "_reemplazar_atomico", _usuario_guarda_en_el_medio)
    informe = tx.restaurar()
    assert informe.veredicto is bj.Veredicto.CONCURRENT_MODIFICATION
    assert (exe_dir / "BodySlide.xml").read_text(encoding="utf-8") == guardado
    assert tx.ruta.exists()


async def test_cr4_la_limpieza_automatica_no_borra_la_cuarentena_de_otra_corrida(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tx, _ = _tx_sembrable(tmp_path)
    liberar = threading.Event()
    sembrar_real = bj.TransaccionDeConfig.sembrar

    def _colgado(self: bj.TransaccionDeConfig) -> None:
        liberar.wait(30)
        sembrar_real(self)

    monkeypatch.setattr(bj.TransaccionDeConfig, "sembrar", _colgado)
    with pytest.raises(bj.SiembraColgadaError):
        await bj.SiembraConPlazo(tx, plazo=0.05).ejecutar()
    # El operador libera la cuarentena de A y otra corrida B publica la suya.
    assert bj.liberar_cuarentena(tx.almacen, evidencia="prueba")
    bj.poner_en_cuarentena(tx.almacen, run_id="b" * 32, motivo="corrida B colgada")
    liberar.set()
    assert await asyncio.to_thread(_esperar, lambda: not tx.ruta.exists())
    marcador = json.loads(tx.almacen.marcador_de_cuarentena.read_text(encoding="utf-8"))
    assert marcador["run_id"] == "b" * 32, "la limpieza de A borró la cuarentena de B"


async def test_cr8_config_cambiado_entre_calcular_y_snapshot_no_siembra_contenido_viejo(
    tmp_path: pathlib.Path,
) -> None:
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    real = calcular_config_sembrada
    llamadas = [0]

    def _calculo_con_edicion_concurrente(**kwargs: object) -> bytes:
        bytes_calculados = real(**kwargs)  # type: ignore[arg-type]
        llamadas[0] += 1
        if llamadas[0] == 1:
            # El usuario guarda Config.xml justo después del cálculo y antes del snapshot.
            (exe.parent / "Config.xml").write_text(_CONFIG.replace("/mis/proyectos", "/otros"), encoding="utf-8")
        return bytes_calculados

    run_capture = AsyncMock()
    with (
        patch("sky_claw.local.tools.bodyslide_runner.calcular_config_sembrada", _calculo_con_edicion_concurrente),
        patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture),
    ):
        resultado = await _correr(runner, _destino(tmp_path))
    assert resultado.estado == "CONCURRENT_MODIFICATION"
    assert run_capture.await_count == 0
    assert "/otros" in (exe.parent / "Config.xml").read_text(encoding="utf-8")
    assert not _ruta_del_journal(runner).exists()


async def test_cr9_cancelar_mientras_se_abre_el_journal_no_lo_deja_huerfano(
    tmp_path: pathlib.Path,
) -> None:
    runner = _runner(tmp_path)
    exe = runner.config.bodyslide_exe
    entro = threading.Event()
    liberar = threading.Event()

    def _apertura_lenta(**kwargs: object) -> bj.TransaccionDeConfig:
        entro.set()
        liberar.wait(30)
        return bj.iniciar_transaccion(**kwargs)  # type: ignore[arg-type]

    run_capture = AsyncMock()
    with (
        patch("sky_claw.local.tools.bodyslide_runner.iniciar_transaccion", _apertura_lenta),
        patch("sky_claw.local.tools.bodyslide_runner.run_capture", run_capture),
    ):
        tarea = asyncio.create_task(_correr(runner, _destino(tmp_path)))
        assert await asyncio.to_thread(entro.wait, 10)
        tarea.cancel()
        liberar.set()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    assert run_capture.await_count == 0
    assert (exe.parent / "Config.xml").read_text(encoding="utf-8") == _CONFIG
    assert not _ruta_del_journal(runner).exists(), "el PREPARADO de una apertura cancelada bloquea la instancia"


def test_cr12_un_archivo_inclasificable_no_impide_registrar_terminal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TERMINAL se registra igual; el archivo inclasificable queda fuera de la restauración."""
    exe_dir = tmp_path / "BodySlideCR12"
    exe_dir.mkdir()
    (exe_dir / "Config.xml").write_text(_CONFIG, encoding="utf-8")
    tx = bj.iniciar_transaccion(
        almacen=_almacen(tmp_path),
        identidad=bj.IdentidadDeEjecucion(maquina="m", instancia=identidad_de_instancia(exe_dir)),
        exe_dir=exe_dir,
        bytes_sembrados=b"<Config><TargetGame>4</TargetGame><Sembrado/></Config>",
    )
    tx.sembrar()
    tx.marcar_lanzado()
    real = bj._huella_actual

    def _buildselection_es_un_enlace(destino: pathlib.Path) -> str:
        if destino.name == "BuildSelection.xml":
            raise bj.ModificacionConcurrenteError("'BuildSelection.xml' es un enlace")
        return real(destino)

    monkeypatch.setattr(bj, "_huella_actual", _buildselection_es_un_enlace)
    tx.marcar_terminal()
    assert _estado_en_disco(tx.ruta) == "TERMINAL"
    assert tx.restaurar().veredicto is bj.Veredicto.CONCURRENT_MODIFICATION
    assert _estado_en_disco(tx.ruta) == "TERMINAL", "la evidencia de terminalidad se conserva"
    # El operador retira el enlace: la siguiente restauración ya puede cerrar.
    monkeypatch.setattr(bj, "_huella_actual", real)
    assert tx.restaurar().veredicto is bj.Veredicto.RESTAURADO
    assert (exe_dir / "Config.xml").read_text(encoding="utf-8") == _CONFIG


def test_cr12_un_archivo_ilegible_tampoco_impide_registrar_terminal(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermano de cr12: un ``OSError`` al leer un archivo no deja el journal en LANZADO para siempre."""
    exe_dir = tmp_path / "BodySlideCR12b"
    exe_dir.mkdir()
    (exe_dir / "Config.xml").write_text(_CONFIG, encoding="utf-8")
    tx = bj.iniciar_transaccion(
        almacen=_almacen(tmp_path),
        identidad=bj.IdentidadDeEjecucion(maquina="m", instancia=identidad_de_instancia(exe_dir)),
        exe_dir=exe_dir,
        bytes_sembrados=b"<Config><TargetGame>4</TargetGame><Sembrado/></Config>",
    )
    tx.sembrar()
    tx.marcar_lanzado()
    real = bj._huella_actual

    def _buildselection_ilegible(destino: pathlib.Path) -> str:
        if destino.name == "BuildSelection.xml":
            raise PermissionError(13, "Access is denied")
        return real(destino)

    monkeypatch.setattr(bj, "_huella_actual", _buildselection_ilegible)
    tx.marcar_terminal()
    assert _estado_en_disco(tx.ruta) == "TERMINAL"
    assert tx.restaurar().veredicto is bj.Veredicto.RESTORE_FAILED
    assert _estado_en_disco(tx.ruta) == "RESTAURACION_FALLIDA", "la terminalidad probada se conserva"
    monkeypatch.setattr(bj, "_huella_actual", real)
    assert tx.restaurar().veredicto is bj.Veredicto.RESTAURADO
    assert (exe_dir / "Config.xml").read_text(encoding="utf-8") == _CONFIG

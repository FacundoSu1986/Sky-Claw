"""PR 2c-2b: superficie de operador de BodySlide (``--mode bodyslide-recovery``).

El propietario eligió (2026-11-25) que las dos salidas manuales de BodySlide tengan una
superficie: ``liberar_cuarentena`` (2c-1) y ``registrar_terminalidad_por_operador``
(2c-2b, opción (a)). Sin ella, un ``LANZADO`` en HOLD o una cuarentena del recurso solo
se liberaban editando archivos de estado a mano.

Propiedad: *el operador ve lo mismo que el reconciliador y solo cambia algo con evidencia
escrita*. ``status`` no escribe nada; las dos acciones que escriben exigen evidencia y
fallan cerradas, sin tocar el estado, ante cualquier prueba que falte.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import subprocess
import sys

import psutil
import pytest

from sky_claw.local.tools import bodyslide_journal as bj
from sky_claw.local.tools.bodyslide_journal import (
    AlmacenDeJournal,
    EstadoDelJournal,
    IdentidadDeEjecucion,
    IdentidadDeProceso,
    identidad_de_instancia,
    iniciar_transaccion,
)
from sky_claw.local.tools.bodyslide_operador import (
    ACCIONES_DEL_OPERADOR,
    AccionDelOperador,
    AccionDelOperadorRechazadaError,
    ejecutar_accion_del_operador,
)

ORIGINAL = b"<Config><TargetGame>4</TargetGame></Config>"
SEMBRADO = b"<Config><TargetGame>4</TargetGame><GameDataPath>C:/juego/Data</GameDataPath></Config>"
MAQUINA = "maquina-de-prueba"
EVIDENCIA = "MO2 y Sky-Claw cerrados; no queda ningún BodySlide x64.exe vivo"
RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _sin_endurecer(_ruta: pathlib.Path) -> None:
    """Sin ``icacls``: el endurecimiento real se ancla en ``test_bodyslide_journal.py``."""


@pytest.fixture
def exe_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    directorio = tmp_path / "BodySlide"
    directorio.mkdir()
    (directorio / "Config.xml").write_bytes(ORIGINAL)
    return directorio


@pytest.fixture
def almacen(tmp_path: pathlib.Path) -> AlmacenDeJournal:
    return AlmacenDeJournal(tmp_path / "state" / "bodyslide_journal", endurecer=_sin_endurecer)


@pytest.fixture(scope="module")
def duenio_muerto() -> IdentidadDeProceso:
    proceso = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])  # noqa: S603 - argv fijo
    creado = psutil.Process(proceso.pid).create_time()
    proceso.wait(timeout=30)
    return IdentidadDeProceso(pid=proceso.pid, creado=creado)


@pytest.fixture
def maquina_fija(monkeypatch: pytest.MonkeyPatch) -> None:
    """La identidad de la máquina real no se lee en tests: el registro de Windows no es del test."""
    monkeypatch.setattr(bj, "identidad_de_maquina", lambda: MAQUINA)


def _lanzado(almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio: IdentidadDeProceso) -> bj.TransaccionDeConfig:
    tx = iniciar_transaccion(
        almacen=almacen,
        identidad=IdentidadDeEjecucion(maquina=MAQUINA, instancia=identidad_de_instancia(exe_dir)),
        exe_dir=exe_dir,
        bytes_sembrados=SEMBRADO,
        duenio=duenio,
    )
    tx.sembrar()
    tx.marcar_lanzado()
    return tx


# ---------------------------------------------------------------------------
# Anclas
# ---------------------------------------------------------------------------


def test_las_acciones_estan_congeladas() -> None:
    assert ACCIONES_DEL_OPERADOR == ("status", "release-quarantine", "confirm-terminal")
    assert tuple(a.value for a in AccionDelOperador) == ACCIONES_DEL_OPERADOR


def test_el_modulo_no_importa_el_contexto_de_la_app() -> None:
    """Corre con Sky-Claw cerrado: arrancar el daemon tomaría el recurso que el operador está liberando."""
    fuente = (RAIZ / "sky_claw" / "local" / "tools" / "bodyslide_operador.py").read_text(encoding="utf-8")
    importados = {
        nodo.module
        for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.ImportFrom) and nodo.module is not None
    }
    assert not any("app_context" in modulo for modulo in importados)


# ---------------------------------------------------------------------------
# status: solo lectura
# ---------------------------------------------------------------------------


def test_status_sin_nada_pendiente(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    informe = ejecutar_accion_del_operador(AccionDelOperador.STATUS, almacen=almacen, exe_dir=exe_dir, evidencia=None)

    assert informe.cuarentena_activa is False
    assert informe.journals == ()
    assert informe.cambio is False


def test_status_muestra_journals_y_cuarentena_sin_escribir(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    # Arrange: un LANZADO en HOLD, un journal corrupto de otra instancia y la cuarentena del recurso.
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    corrupto = almacen.ruta_de("f" * 32)
    corrupto.write_bytes(b"basura")
    bj.poner_en_cuarentena(almacen, run_id=tx.run_id, motivo="siembra colgada")
    antes = {ruta.name: ruta.read_bytes() for ruta in almacen.directorio.iterdir()}

    # Act
    informe = ejecutar_accion_del_operador(AccionDelOperador.STATUS, almacen=almacen, exe_dir=exe_dir, evidencia=None)

    # Assert
    assert informe.cuarentena_activa is True
    por_archivo = {visto.archivo: visto for visto in informe.journals}
    assert por_archivo[tx.ruta.name].estado == "LANZADO"
    assert por_archivo[tx.ruta.name].run_id == tx.run_id
    assert por_archivo[tx.ruta.name].de_esta_instalacion is True
    assert por_archivo[corrupto.name].estado == "CORRUPTO"
    assert por_archivo[corrupto.name].de_esta_instalacion is False
    assert {ruta.name: ruta.read_bytes() for ruta in almacen.directorio.iterdir()} == antes
    assert informe.cambio is False


def test_status_sin_carpeta_configurada_igual_lista(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)

    informe = ejecutar_accion_del_operador(AccionDelOperador.STATUS, almacen=almacen, exe_dir=None, evidencia=None)

    assert [visto.archivo for visto in informe.journals] == [tx.ruta.name]
    assert informe.journals[0].de_esta_instalacion is False


# ---------------------------------------------------------------------------
# release-quarantine
# ---------------------------------------------------------------------------


def test_release_quarantine_con_evidencia_libera_y_registra_critical(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    bj.poner_en_cuarentena(almacen, run_id="a" * 32, motivo="siembra colgada")

    with caplog.at_level(logging.CRITICAL, logger=bj.__name__):
        informe = ejecutar_accion_del_operador(
            AccionDelOperador.RELEASE_QUARANTINE, almacen=almacen, exe_dir=exe_dir, evidencia=EVIDENCIA
        )

    assert informe.cambio is True
    assert informe.cuarentena_activa is False
    assert not bj.cuarentena_activa(almacen)
    assert any(r.levelno == logging.CRITICAL and EVIDENCIA in r.getMessage() for r in caplog.records)


def test_release_quarantine_sin_cuarentena_no_cambia_nada(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    informe = ejecutar_accion_del_operador(
        AccionDelOperador.RELEASE_QUARANTINE, almacen=almacen, exe_dir=exe_dir, evidencia=EVIDENCIA
    )

    assert informe.cambio is False


@pytest.mark.parametrize(
    "accion", [AccionDelOperador.RELEASE_QUARANTINE, AccionDelOperador.CONFIRM_TERMINAL], ids=lambda a: a.value
)
@pytest.mark.parametrize("evidencia", [None, "", "   "])
def test_las_acciones_que_escriben_exigen_evidencia(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    duenio_muerto: IdentidadDeProceso,
    maquina_fija: None,
    accion: AccionDelOperador,
    evidencia: str | None,
) -> None:
    # Arrange: hay algo que liberar en los dos casos.
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    bj.poner_en_cuarentena(almacen, run_id=tx.run_id, motivo="siembra colgada")
    antes = {ruta.name: ruta.read_bytes() for ruta in almacen.directorio.iterdir()}

    # Act / Assert
    with pytest.raises(AccionDelOperadorRechazadaError, match="evidencia"):
        ejecutar_accion_del_operador(accion, almacen=almacen, exe_dir=exe_dir, evidencia=evidencia)
    assert {ruta.name: ruta.read_bytes() for ruta in almacen.directorio.iterdir()} == antes


# ---------------------------------------------------------------------------
# confirm-terminal
# ---------------------------------------------------------------------------


def test_confirm_terminal_lleva_el_lanzado_de_esta_instalacion_a_terminal(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso, maquina_fija: None
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)

    informe = ejecutar_accion_del_operador(
        AccionDelOperador.CONFIRM_TERMINAL, almacen=almacen, exe_dir=exe_dir, evidencia=EVIDENCIA
    )

    assert informe.cambio is True
    assert almacen.leer(tx.ruta).estado is EstadoDelJournal.TERMINAL
    assert "próximo arranque" in informe.detalle


def test_confirm_terminal_sin_carpeta_configurada_se_rechaza(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    antes = tx.ruta.read_bytes()

    with pytest.raises(AccionDelOperadorRechazadaError, match="bodyslide_exe"):
        ejecutar_accion_del_operador(
            AccionDelOperador.CONFIRM_TERMINAL, almacen=almacen, exe_dir=None, evidencia=EVIDENCIA
        )

    assert tx.ruta.read_bytes() == antes


def test_confirm_terminal_sin_journal_de_esta_instalacion_se_rechaza(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, maquina_fija: None
) -> None:
    with pytest.raises(AccionDelOperadorRechazadaError, match="no hay journal"):
        ejecutar_accion_del_operador(
            AccionDelOperador.CONFIRM_TERMINAL, almacen=almacen, exe_dir=exe_dir, evidencia=EVIDENCIA
        )


def test_confirm_terminal_sin_identidad_de_maquina_se_rechaza(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    duenio_muerto: IdentidadDeProceso,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con el perfil aislado no se lee ``MachineGuid``: el operador no puede saltear esa prueba."""
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    monkeypatch.setattr(bj, "identidad_de_maquina", lambda: None)
    antes = tx.ruta.read_bytes()

    with pytest.raises(AccionDelOperadorRechazadaError, match="identidad"):
        ejecutar_accion_del_operador(
            AccionDelOperador.CONFIRM_TERMINAL, almacen=almacen, exe_dir=exe_dir, evidencia=EVIDENCIA
        )

    assert tx.ruta.read_bytes() == antes


def test_confirm_terminal_traduce_el_rechazo_del_journal(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, maquina_fija: None
) -> None:
    """Dueño vivo (este proceso): el rechazo del journal llega al operador como motivo, sin traceback."""
    tx = iniciar_transaccion(
        almacen=almacen,
        identidad=IdentidadDeEjecucion(maquina=MAQUINA, instancia=identidad_de_instancia(exe_dir)),
        exe_dir=exe_dir,
        bytes_sembrados=SEMBRADO,
    )
    tx.sembrar()
    tx.marcar_lanzado()
    antes = tx.ruta.read_bytes()

    with pytest.raises(AccionDelOperadorRechazadaError, match="vivo"):
        ejecutar_accion_del_operador(
            AccionDelOperador.CONFIRM_TERMINAL, almacen=almacen, exe_dir=exe_dir, evidencia=EVIDENCIA
        )

    assert tx.ruta.read_bytes() == antes


def test_confirm_terminal_con_journal_corrupto_se_rechaza(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, maquina_fija: None
) -> None:
    almacen.asegurar_directorio()
    ruta = almacen.ruta_de(identidad_de_instancia(exe_dir))
    ruta.write_bytes(b"basura")

    with pytest.raises(AccionDelOperadorRechazadaError, match="corrupto|válido"):
        ejecutar_accion_del_operador(
            AccionDelOperador.CONFIRM_TERMINAL, almacen=almacen, exe_dir=exe_dir, evidencia=EVIDENCIA
        )

    assert ruta.read_bytes() == b"basura"


# ---------------------------------------------------------------------------
# La CLI: --mode bodyslide-recovery
# ---------------------------------------------------------------------------


def test_el_parser_expone_exactamente_las_acciones_del_modulo(tmp_path: pathlib.Path) -> None:
    from sky_claw.__main__ import _build_parser, _parse_args

    accion = next(a for a in _build_parser()._actions if "--bodyslide-recovery-action" in a.option_strings)
    assert tuple(accion.choices) == ACCIONES_DEL_OPERADOR

    args = _parse_args(
        [
            "--mode",
            "bodyslide-recovery",
            "--bodyslide-recovery-action",
            "confirm-terminal",
            "--bodyslide-evidence",
            EVIDENCIA,
            "--bodyslide-exe",
            str(tmp_path / "BodySlide" / "BodySlide x64.exe"),
        ]
    )
    assert args.bodyslide_recovery_action == "confirm-terminal"
    assert args.bodyslide_evidence == EVIDENCIA
    assert args.bodyslide_exe == tmp_path / "BodySlide" / "BodySlide x64.exe"


def test_una_accion_desconocida_la_rechaza_el_parser() -> None:
    from sky_claw.__main__ import _parse_args

    with pytest.raises(SystemExit) as exc_info:
        _parse_args(["--mode", "bodyslide-recovery", "--bodyslide-recovery-action", "borrar-todo"])
    assert exc_info.value.code == 2


async def test_el_modo_no_arranca_el_contexto_de_la_app(tmp_path: pathlib.Path) -> None:
    from unittest.mock import patch

    from sky_claw.__main__ import _main

    with (
        patch("sky_claw.__main__._run_bodyslide_recovery") as modo,
        patch("sky_claw.__main__.AppContext") as app_context,
    ):
        await _main(["--mode", "bodyslide-recovery"])

    modo.assert_called_once()
    app_context.assert_not_called()


async def test_un_rechazo_sale_con_codigo_1_y_el_motivo(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sky_claw.__main__ import _main

    monkeypatch.setattr(bj, "AlmacenDeJournal", lambda: almacen)
    bj.poner_en_cuarentena(almacen, run_id="a" * 32, motivo="siembra colgada")

    with caplog.at_level(logging.ERROR, logger="sky_claw"), pytest.raises(SystemExit) as exc_info:
        await _main(["--mode", "bodyslide-recovery", "--bodyslide-recovery-action", "release-quarantine"])

    assert exc_info.value.code == 1
    assert any("evidencia" in r.getMessage() for r in caplog.records)
    assert bj.cuarentena_activa(almacen), "sin evidencia la cuarentena sigue puesta"


async def test_confirm_terminal_de_punta_a_punta_por_la_cli(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    duenio_muerto: IdentidadDeProceso,
    maquina_fija: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sky_claw.__main__ import _main

    monkeypatch.setattr(bj, "AlmacenDeJournal", lambda: almacen)
    tx = _lanzado(almacen, exe_dir, duenio_muerto)

    with caplog.at_level(logging.INFO, logger="sky_claw"):
        await _main(
            [
                "--mode",
                "bodyslide-recovery",
                "--bodyslide-recovery-action",
                "confirm-terminal",
                "--bodyslide-evidence",
                EVIDENCIA,
                "--bodyslide-exe",
                str(exe_dir / "BodySlide x64.exe"),
            ]
        )

    assert almacen.leer(tx.ruta).estado is EstadoDelJournal.TERMINAL
    texto = "\n".join(r.getMessage() for r in caplog.records)
    assert "LANZADO → TERMINAL" in texto
    assert "estado=TERMINAL" in texto

"""PR 2c-2b (opción (a) del brief §13.6): el operador registra la terminalidad de un ``LANZADO``.

Cuando el teardown del broker falla, el journal queda en ``LANZADO`` y ningún camino
automático puede probar que el proceso terminó. La salida es del OPERADOR: con evidencia
escrita, ``registrar_terminalidad_por_operador`` lleva ``LANZADO → TERMINAL``. No restaura:
la restauración la hace el reconciliador en el próximo arranque, con sus propias pruebas
(bytes esperados, identidad física, dueño muerto).

Propiedad: *la evidencia del operador sustituye a la terminalidad, nunca a las demás
pruebas*. Por eso la operación exige journal íntegro, misma máquina e instancia, carpeta
física idéntica, dueño que no esté vivo y estado exactamente ``LANZADO``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import subprocess
import sys
from typing import Any

import psutil
import pytest

from sky_claw.local.frozen_runtime.root_lock import OwnerLiveness
from sky_claw.local.tools import bodyslide_journal as bj
from sky_claw.local.tools.bodyslide_journal import (
    AlmacenDeJournal,
    EstadoDelJournal,
    IdentidadDeEjecucion,
    IdentidadDeProceso,
    JournalCorruptoError,
    TerminalidadNoRegistrableError,
    Veredicto,
    identidad_de_instancia,
    iniciar_transaccion,
    recuperar_journal,
    registrar_terminalidad_por_operador,
)

ORIGINAL = b"<Config><TargetGame>4</TargetGame></Config>"
SEMBRADO = b"<Config><TargetGame>4</TargetGame><GameDataPath>C:/juego/Data</GameDataPath></Config>"
APP_ORIGINAL = b"<BodySlideConfig><SelectedPreset>Viejo</SelectedPreset></BodySlideConfig>"
APP_DE_LA_CORRIDA = b"<BodySlideConfig><SelectedPreset>Nuevo</SelectedPreset></BodySlideConfig>"
MAQUINA = "maquina-de-prueba"
EVIDENCIA = "MO2 cerrado; el Administrador de tareas no muestra BodySlide x64.exe ni OutfitStudio"


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _sin_endurecer(_ruta: pathlib.Path) -> None:
    """Sin ``icacls``: el endurecimiento real se ancla en ``test_bodyslide_journal.py``."""


@pytest.fixture
def exe_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    directorio = tmp_path / "BodySlide"
    directorio.mkdir()
    (directorio / "Config.xml").write_bytes(ORIGINAL)
    (directorio / "BodySlide.xml").write_bytes(APP_ORIGINAL)
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


def _identidad(exe_dir: pathlib.Path, maquina: str = MAQUINA) -> IdentidadDeEjecucion:
    return IdentidadDeEjecucion(maquina=maquina, instancia=identidad_de_instancia(exe_dir))


def _lanzado(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio: IdentidadDeProceso | None
) -> bj.TransaccionDeConfig:
    """Una corrida que sembró, pidió el spawn y quedó sin terminalidad (el HOLD que se libera)."""
    tx = iniciar_transaccion(
        almacen=almacen, identidad=_identidad(exe_dir), exe_dir=exe_dir, bytes_sembrados=SEMBRADO, duenio=duenio
    )
    tx.sembrar()
    tx.marcar_lanzado()
    # La corrida escribió BodySlide.xml antes de que se perdiera el rastro del proceso.
    (exe_dir / "BodySlide.xml").write_bytes(APP_DE_LA_CORRIDA)
    return tx


def _registrar(
    tx: bj.TransaccionDeConfig,
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    *,
    evidencia: str = EVIDENCIA,
    identidad: IdentidadDeEjecucion | None = None,
    vivacidad: Any = None,
) -> bj.DocumentoDeJournal:
    argumentos: dict[str, Any] = {}
    if vivacidad is not None:
        argumentos["vivacidad"] = vivacidad
    return registrar_terminalidad_por_operador(
        tx.ruta,
        almacen=almacen,
        identidad_actual=identidad if identidad is not None else _identidad(exe_dir),
        exe_dir=exe_dir,
        evidencia=evidencia,
        **argumentos,
    )


def _en_disco(ruta: pathlib.Path) -> dict[str, Any]:
    datos: dict[str, Any] = json.loads(ruta.read_text(encoding="utf-8"))
    return datos


# ---------------------------------------------------------------------------
# Camino feliz: LANZADO → TERMINAL, y el reconciliador restaura después
# ---------------------------------------------------------------------------


def test_con_evidencia_registra_terminal_con_lo_observado_y_no_restaura(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    # Arrange
    tx = _lanzado(almacen, exe_dir, duenio_muerto)

    # Act
    documento = _registrar(tx, almacen, exe_dir)

    # Assert: TERMINAL durable, con los hashes que dejó la corrida, y la config sin tocar.
    assert documento.estado is EstadoDelJournal.TERMINAL
    en_disco = _en_disco(tx.ruta)
    assert en_disco["estado"] == "TERMINAL"
    por_nombre = {archivo["nombre"]: archivo for archivo in en_disco["archivos"]}
    assert por_nombre["BodySlide.xml"]["esperados_sha256"] == [_sha(APP_DE_LA_CORRIDA)]
    assert (exe_dir / "Config.xml").read_bytes() == SEMBRADO
    assert (exe_dir / "BodySlide.xml").read_bytes() == APP_DE_LA_CORRIDA


def test_despues_del_registro_el_reconciliador_restaura(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    # Arrange: antes del registro, la recuperación es HOLD.
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    antes = recuperar_journal(tx.ruta, almacen=almacen, identidad_actual=_identidad(exe_dir), exe_dir=exe_dir)
    assert antes.veredicto is Veredicto.HOLD_TERMINALITY_UNKNOWN

    # Act
    _registrar(tx, almacen, exe_dir)
    despues = recuperar_journal(tx.ruta, almacen=almacen, identidad_actual=_identidad(exe_dir), exe_dir=exe_dir)

    # Assert
    assert despues.veredicto is Veredicto.RESTAURADO
    assert (exe_dir / "Config.xml").read_bytes() == ORIGINAL
    assert (exe_dir / "BodySlide.xml").read_bytes() == APP_ORIGINAL
    assert not tx.ruta.exists()


def test_la_evidencia_queda_en_el_log_como_critical(
    almacen: AlmacenDeJournal,
    exe_dir: pathlib.Path,
    duenio_muerto: IdentidadDeProceso,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)

    with caplog.at_level(logging.CRITICAL, logger=bj.__name__):
        _registrar(tx, almacen, exe_dir)

    criticos = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticos) == 1
    assert EVIDENCIA in criticos[0].getMessage()
    assert tx.run_id in criticos[0].getMessage()


# ---------------------------------------------------------------------------
# Rechazos: cada prueba que falta deja el journal intacto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evidencia", ["", "   ", "\n\t"])
def test_sin_evidencia_no_registra(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso, evidencia: str
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    antes = tx.ruta.read_bytes()

    with pytest.raises(ValueError, match="evidencia"):
        _registrar(tx, almacen, exe_dir, evidencia=evidencia)

    assert tx.ruta.read_bytes() == antes


def test_un_estado_distinto_de_lanzado_no_se_registra(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    """SEMBRADO no necesita al operador: nunca se pidió el spawn y el reconciliador lo resuelve solo."""
    tx = iniciar_transaccion(
        almacen=almacen, identidad=_identidad(exe_dir), exe_dir=exe_dir, bytes_sembrados=SEMBRADO, duenio=duenio_muerto
    )
    tx.sembrar()
    antes = tx.ruta.read_bytes()

    with pytest.raises(TerminalidadNoRegistrableError, match="LANZADO"):
        _registrar(tx, almacen, exe_dir)

    assert tx.ruta.read_bytes() == antes


def test_dos_registros_seguidos_el_segundo_se_rechaza(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    _registrar(tx, almacen, exe_dir)
    antes = tx.ruta.read_bytes()

    with pytest.raises(TerminalidadNoRegistrableError):
        _registrar(tx, almacen, exe_dir)

    assert tx.ruta.read_bytes() == antes


def test_con_el_duenio_vivo_no_se_registra(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    """El dueño vivo puede seguir siendo la corrida: el operador cierra Sky-Claw antes (como en vfs-quarantine)."""
    tx = _lanzado(almacen, exe_dir, duenio=None)  # dueño: este proceso, vivo
    antes = tx.ruta.read_bytes()

    with pytest.raises(TerminalidadNoRegistrableError, match="vivo"):
        _registrar(tx, almacen, exe_dir)

    assert tx.ruta.read_bytes() == antes


def test_con_vivacidad_indeterminada_la_evidencia_del_operador_alcanza(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    """Indeterminada es justo lo que la evidencia del operador resuelve; solo un dueño VIVO frena."""
    tx = _lanzado(almacen, exe_dir, duenio_muerto)

    documento = _registrar(tx, almacen, exe_dir, vivacidad=lambda _pid, _creado: OwnerLiveness.INDETERMINATE)

    assert documento.estado is EstadoDelJournal.TERMINAL


@pytest.mark.parametrize(
    "identidad",
    [
        pytest.param("otra-maquina", id="otra-maquina"),
        pytest.param("", id="maquina-desconocida"),
    ],
)
def test_otra_maquina_o_desconocida_no_registra(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso, identidad: str
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    antes = tx.ruta.read_bytes()

    with pytest.raises(TerminalidadNoRegistrableError, match="máquina"):
        _registrar(tx, almacen, exe_dir, identidad=_identidad(exe_dir, maquina=identidad))

    assert tx.ruta.read_bytes() == antes


def test_sin_identidad_no_registra(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    antes = tx.ruta.read_bytes()

    with pytest.raises(TerminalidadNoRegistrableError, match="máquina"):
        registrar_terminalidad_por_operador(
            tx.ruta, almacen=almacen, identidad_actual=None, exe_dir=exe_dir, evidencia=EVIDENCIA
        )

    assert tx.ruta.read_bytes() == antes


def test_carpeta_reemplazada_no_registra(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    instancia = _en_disco(tx.ruta)["instancia"]
    exe_dir.rename(exe_dir.with_name("BodySlide.viejo"))
    exe_dir.mkdir()
    (exe_dir / "Config.xml").write_bytes(SEMBRADO)
    antes = tx.ruta.read_bytes()

    with pytest.raises(TerminalidadNoRegistrableError):
        _registrar(tx, almacen, exe_dir, identidad=IdentidadDeEjecucion(maquina=MAQUINA, instancia=instancia))

    assert tx.ruta.read_bytes() == antes


def test_journal_corrupto_no_se_toca(
    almacen: AlmacenDeJournal, exe_dir: pathlib.Path, duenio_muerto: IdentidadDeProceso
) -> None:
    tx = _lanzado(almacen, exe_dir, duenio_muerto)
    tx.ruta.write_bytes(b"{ esto no es un journal")

    with pytest.raises(JournalCorruptoError):
        _registrar(tx, almacen, exe_dir)

    assert tx.ruta.read_bytes() == b"{ esto no es un journal"


def test_sin_journal_lo_dice(almacen: AlmacenDeJournal, exe_dir: pathlib.Path) -> None:
    ruta = almacen.ruta_de(identidad_de_instancia(exe_dir))

    with pytest.raises(FileNotFoundError):
        registrar_terminalidad_por_operador(
            ruta, almacen=almacen, identidad_actual=_identidad(exe_dir), exe_dir=exe_dir, evidencia=EVIDENCIA
        )

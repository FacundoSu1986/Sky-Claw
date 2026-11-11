"""H-4 (auditoría de la etapa 9): la cuarentena del broker VFS tiene superficie de operador.

``VfsExecutionBroker.release_quarantine`` solo sirve con un broker vivo que ya es dueño del lock, y con el lock
«envenenado» ni siquiera se puede arrancar uno: el operador salía borrando dotfiles a mano. ``vfs_quarantine`` lee el
estado sin escribir nada y libera con evidencia, SIN broker vivo.

Estos tests fabrican cada estado con el **broker real** (no con archivos escritos a mano) y cruzan lo que dice el
módulo contra lo que el broker hace al arrancar: así el módulo no puede divergir del mecanismo que describe.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any

import pytest

from sky_claw.local.mo2 import vfs_quarantine
from sky_claw.local.mo2.vfs_broker import VfsExecutionBroker, VfsInstanceQuarantinedError
from sky_claw.local.mo2.vfs_quarantine import (
    CuarentenaNoLiberableError,
    OrigenDeCuarentena,
    leer_estado_de_cuarentena,
    liberar_cuarentena,
    listar_instancias_con_cuarentena,
    rutas_de_estado,
)

_INSTANCIA = "inst-q"


def _pid_muerto() -> int:
    """PID de un proceso que ya terminó (el propio test lo lanzó y lo esperó)."""
    proceso = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603 -- intérprete del propio test
    proceso.wait()
    return proceso.pid


def _broker(state_dir: pathlib.Path, instancia: str = _INSTANCIA) -> VfsExecutionBroker:
    return VfsExecutionBroker(instance_id=instancia, state_dir=state_dir)


async def _con_marcador(state_dir: pathlib.Path) -> None:
    """Cuarentena persistida por un broker real que se cerró limpio (marcador, sin lock)."""
    broker = _broker(state_dir)
    await broker.start()
    broker._quarantine_instance("job-77", "terminalidad desconocida demostrada")
    await broker.close()


async def _con_marcador_ilegible(state_dir: pathlib.Path) -> None:
    await _con_marcador(state_dir)
    rutas_de_estado(state_dir, _INSTANCIA).marcador.write_text("{no es json", encoding="utf-8")


async def _con_lock_envenenado(
    state_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, dueno_vivo: bool = False
) -> None:
    """Falla la persistencia del marcador: el broker real envenena el lock y lo retiene al cerrar."""
    broker = _broker(state_dir)
    await broker.start()
    real_replace = os.replace

    def _replace_que_falla(src: Any, dst: Any) -> None:
        if "quarantine" in pathlib.Path(dst).name:
            raise OSError("fallo simulado de E/S al persistir la cuarentena")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _replace_que_falla)
    broker._quarantine_instance("job-88", "falla de persistencia")
    monkeypatch.setattr(os, "replace", real_replace)
    await broker.close()
    lock = rutas_de_estado(state_dir, _INSTANCIA).lock
    assert json.loads(lock.read_text(encoding="utf-8")).get("quarantined") is True, "precondición: lock envenenado"
    if not dueno_vivo:
        crudo = json.loads(lock.read_text(encoding="utf-8"))
        crudo["pid"] = _pid_muerto()
        lock.write_text(json.dumps(crudo), encoding="utf-8")


def _con_lock_huerfano(state_dir: pathlib.Path) -> None:
    """Crash del broker: queda el lock de un dueño muerto y ningún marcador."""
    state_dir.mkdir(parents=True, exist_ok=True)
    rutas_de_estado(state_dir, _INSTANCIA).lock.write_text(
        json.dumps({"pid": _pid_muerto(), "session_id": "sesion-que-murio", "create_time": None}), encoding="utf-8"
    )


async def _comportamiento_del_broker(state_dir: pathlib.Path) -> str:
    """Qué hace el broker REAL al arrancar sobre ese estado (sobre una copia: arrancar muta el disco)."""
    copia = state_dir.parent / f"{state_dir.name}_copia"
    shutil.copytree(state_dir, copia)
    broker = _broker(copia)
    try:
        await broker.start()
    except VfsInstanceQuarantinedError:
        return "se_niega_a_arrancar"
    try:
        return "cuarentena" if broker.quarantine_reason is not None else "limpio"
    finally:
        await broker.close()


# --------------------------------------------------------------------------------------------------------------------
# Lectura: enumera las formas persistidas de la cuarentena y las cruza con el broker real
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("escenario", "origen_esperado", "broker_esperado"),
    [
        ("limpio", None, "limpio"),
        ("marcador", OrigenDeCuarentena.MARCADOR, "cuarentena"),
        ("marcador_ilegible", OrigenDeCuarentena.MARCADOR_ILEGIBLE, "cuarentena"),
        ("lock_envenenado", OrigenDeCuarentena.LOCK_ENVENENADO, "se_niega_a_arrancar"),
        ("lock_huerfano", OrigenDeCuarentena.LOCK_HUERFANO, "cuarentena"),
    ],
)
async def test_el_estado_leido_coincide_con_lo_que_hace_el_broker_real(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    escenario: str,
    origen_esperado: OrigenDeCuarentena | None,
    broker_esperado: str,
) -> None:
    # Arrange
    state_dir = tmp_path / "estado"
    if escenario == "marcador":
        await _con_marcador(state_dir)
    elif escenario == "marcador_ilegible":
        await _con_marcador_ilegible(state_dir)
    elif escenario == "lock_envenenado":
        await _con_lock_envenenado(state_dir, monkeypatch)
    elif escenario == "lock_huerfano":
        _con_lock_huerfano(state_dir)
    else:
        state_dir.mkdir()

    # Act
    estado = leer_estado_de_cuarentena(state_dir, _INSTANCIA)

    # Assert: el módulo y el broker real opinan lo mismo sobre el mismo disco
    assert estado.origen == origen_esperado
    assert estado.en_cuarentena is (origen_esperado is not None)
    assert await _comportamiento_del_broker(state_dir) == broker_esperado
    assert (estado.en_cuarentena) == (broker_esperado != "limpio")


async def test_leer_el_estado_no_escribe_nada(tmp_path: pathlib.Path) -> None:
    state_dir = tmp_path / "estado"
    await _con_marcador(state_dir)
    antes = {p.name: p.read_bytes() for p in state_dir.rglob("*") if p.is_file()}

    leer_estado_de_cuarentena(state_dir, _INSTANCIA)

    despues = {p.name: p.read_bytes() for p in state_dir.rglob("*") if p.is_file()}
    assert antes == despues


async def test_el_marcador_informa_razon_job_y_momento(tmp_path: pathlib.Path) -> None:
    state_dir = tmp_path / "estado"
    await _con_marcador(state_dir)

    estado = leer_estado_de_cuarentena(state_dir, _INSTANCIA)

    assert estado.razon == "terminalidad desconocida demostrada"
    assert estado.job_id == "job-77"
    assert estado.desde is not None and estado.desde > 0
    assert estado.dueno_vivo is False


async def test_un_broker_vivo_dueno_del_lock_se_reporta_como_dueno_vivo(tmp_path: pathlib.Path) -> None:
    state_dir = tmp_path / "estado"
    broker = _broker(state_dir)
    await broker.start()
    try:
        broker._quarantine_instance("job-vivo", "cuarentena con el broker todavía vivo")

        estado = leer_estado_de_cuarentena(state_dir, _INSTANCIA)

        assert estado.en_cuarentena is True
        assert estado.dueno_vivo is True
    finally:
        await broker.close()


def test_las_rutas_de_estado_son_las_del_broker(tmp_path: pathlib.Path) -> None:
    broker = _broker(tmp_path)

    rutas = rutas_de_estado(tmp_path, _INSTANCIA)

    assert rutas.marcador == broker._quarantine_path
    assert rutas.lock == broker._instance_lock_path


def test_listar_encuentra_solo_las_instancias_en_cuarentena(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "vfs_bridge"
    for nombre, con_marcador in (("mo2-aaaa", True), ("mo2-bbbb", False)):
        directorio = base / nombre
        directorio.mkdir(parents=True)
        if con_marcador:
            rutas_de_estado(directorio, nombre).marcador.write_text(json.dumps({"reason": "x"}), encoding="utf-8")
    (base / "archivo_suelto.txt").write_text("no es una instancia", encoding="utf-8")

    encontradas = listar_instancias_con_cuarentena(base)

    assert [e.instance_id for e in encontradas] == ["mo2-aaaa"]


def test_listar_sin_directorio_base_devuelve_vacio(tmp_path: pathlib.Path) -> None:
    assert listar_instancias_con_cuarentena(tmp_path / "no_existe") == []


# --------------------------------------------------------------------------------------------------------------------
# Liberación
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("evidencia", ["", "   ", "\n\t"])
async def test_liberar_sin_evidencia_falla_y_no_borra_nada(tmp_path: pathlib.Path, evidencia: str) -> None:
    state_dir = tmp_path / "estado"
    await _con_marcador(state_dir)

    with pytest.raises(CuarentenaNoLiberableError, match="evidencia"):
        liberar_cuarentena(state_dir, _INSTANCIA, evidencia=evidencia)

    assert rutas_de_estado(state_dir, _INSTANCIA).marcador.exists()


@pytest.mark.parametrize("escenario", ["marcador", "marcador_ilegible", "lock_envenenado", "lock_huerfano"])
async def test_liberar_deja_al_broker_real_arrancar_sin_cuarentena(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, escenario: str
) -> None:
    """El caso del lock huérfano es la regresión central: borrar SOLO el marcador haría que el próximo arranque
    reclame el lock huérfano y vuelva a poner la instancia en cuarentena."""
    # Arrange
    state_dir = tmp_path / "estado"
    if escenario == "marcador":
        await _con_marcador(state_dir)
    elif escenario == "marcador_ilegible":
        await _con_marcador_ilegible(state_dir)
    elif escenario == "lock_envenenado":
        await _con_lock_envenenado(state_dir, monkeypatch)
    else:
        _con_lock_huerfano(state_dir)

    # Act
    previo = liberar_cuarentena(state_dir, _INSTANCIA, evidencia="verificado en el Administrador de tareas")

    # Assert
    assert previo.en_cuarentena is True, "devuelve el estado que había antes de liberar"
    assert leer_estado_de_cuarentena(state_dir, _INSTANCIA).en_cuarentena is False
    assert await _comportamiento_del_broker(state_dir) == "limpio"


async def test_liberar_con_marcador_y_lock_huerfano_borra_ambos(tmp_path: pathlib.Path) -> None:
    state_dir = tmp_path / "estado"
    await _con_marcador(state_dir)
    rutas = rutas_de_estado(state_dir, _INSTANCIA)
    rutas.lock.write_text(
        json.dumps({"pid": _pid_muerto(), "session_id": "murio", "create_time": None}), encoding="utf-8"
    )

    liberar_cuarentena(state_dir, _INSTANCIA, evidencia="el broker murió y no queda ningún proceso")

    assert not rutas.marcador.exists()
    assert not rutas.lock.exists()
    assert await _comportamiento_del_broker(state_dir) == "limpio"


async def test_liberar_se_niega_si_un_broker_vivo_es_dueno_del_lock(tmp_path: pathlib.Path) -> None:
    state_dir = tmp_path / "estado"
    broker = _broker(state_dir)
    await broker.start()
    try:
        broker._quarantine_instance("job-vivo", "cuarentena con el broker todavía vivo")

        with pytest.raises(CuarentenaNoLiberableError, match="Sky-Claw"):
            liberar_cuarentena(state_dir, _INSTANCIA, evidencia="intento con la app abierta")

        rutas = rutas_de_estado(state_dir, _INSTANCIA)
        assert rutas.marcador.exists() and rutas.lock.exists()
        assert broker.quarantine_reason is not None
    finally:
        await broker.close()


async def test_liberar_se_niega_si_el_dueno_del_lock_envenenado_sigue_vivo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "estado"
    await _con_lock_envenenado(state_dir, monkeypatch, dueno_vivo=True)

    with pytest.raises(CuarentenaNoLiberableError, match=str(os.getpid())):
        liberar_cuarentena(state_dir, _INSTANCIA, evidencia="intento con el dueño vivo")

    assert rutas_de_estado(state_dir, _INSTANCIA).lock.exists()


def test_liberar_sin_cuarentena_es_un_no_op(tmp_path: pathlib.Path) -> None:
    tmp_path.mkdir(exist_ok=True)

    estado = liberar_cuarentena(tmp_path, _INSTANCIA, evidencia="no había nada")

    assert estado.en_cuarentena is False


async def test_si_no_puede_borrar_el_marcador_falla_y_la_instancia_sigue_en_cuarentena(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "estado"
    await _con_marcador(state_dir)
    unlink_real = pathlib.Path.unlink

    def _unlink_que_falla(self: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        if self.name.endswith(".quarantine"):
            raise PermissionError("acceso denegado simulado")
        unlink_real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", _unlink_que_falla)

    with pytest.raises(CuarentenaNoLiberableError, match="no se pudo"):
        liberar_cuarentena(state_dir, _INSTANCIA, evidencia="evidencia válida pero el disco falla")

    monkeypatch.undo()
    assert leer_estado_de_cuarentena(state_dir, _INSTANCIA).en_cuarentena is True


def test_no_borra_un_lock_que_cambio_de_dueno_entre_la_lectura_y_el_borrado(tmp_path: pathlib.Path) -> None:
    """Carrera con un arranque concurrente: si el lock ya no es el que se juzgó huérfano, no se toca."""
    lock = tmp_path / ".inst.lock"
    lock.write_bytes(b'{"pid": 1, "session_id": "nuevo-broker-vivo"}')

    borro = vfs_quarantine._borrar_si_no_cambio(lock, visto=b'{"pid": 2, "session_id": "el-viejo-huerfano"}')

    assert borro is False
    assert lock.exists()


async def test_liberar_registra_critical_con_la_evidencia_y_el_estado_previo(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    state_dir = tmp_path / "estado"
    await _con_marcador(state_dir)

    with caplog.at_level(logging.CRITICAL, logger=vfs_quarantine.__name__):
        liberar_cuarentena(state_dir, _INSTANCIA, evidencia="verificado: no queda ningún LODGen.exe")

    # ``caplog`` captura desde el inicio del test: el armado (broker real) también registró CRITICAL.
    registros = [r for r in caplog.records if r.name == vfs_quarantine.__name__ and r.levelno == logging.CRITICAL]
    assert registros, "liberar una cuarentena es un evento CRITICAL auditable"
    texto = registros[0].getMessage()
    assert "verificado: no queda ningún LODGen.exe" in texto
    assert "job-77" in texto

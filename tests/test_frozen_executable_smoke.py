"""Contrato del smoke de arranque del ejecutable congelado en CI."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import scripts.smoke_frozen_executable as smoke
from scripts.smoke_frozen_executable import (
    DEFAULT_TIMEOUT_SECONDS,
    FrozenExecutableSmokeError,
    _construir_entorno_aislado,
    _decodificar_salida,
    _run_smoke_command,
    main,
    smoke_frozen_executable,
)


def _rutas_de_salida(tmp_path: Path) -> tuple[Path, Path, Path]:
    salida = tmp_path / "stdout.bin"
    error = tmp_path / "stderr.bin"
    crash_log = tmp_path / ".sky_claw" / "logs" / "crash.log"
    return salida, error, crash_log


def test_entorno_temporal_descarta_config_y_secretos_conocidos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USERPROFILE", "C:/usuario-real")
    monkeypatch.setenv("OPENAI_API_KEY", "secreto-de-prueba")
    monkeypatch.setenv("GITHUB_TOKEN", "token-de-prueba")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "clave-de-prueba")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "json-de-credenciales")
    monkeypatch.setenv("SSH_AUTH_SOCK", "socket-del-agente")
    monkeypatch.setenv("SKY_CLAW_MO2_ROOT", "C:/config-real")
    monkeypatch.setenv("PYTHONPATH", "C:/modulos-externos")
    perfil = tmp_path / "perfil"
    temporal = tmp_path / "temp"

    entorno = _construir_entorno_aislado(perfil, temporal)

    assert Path(entorno["USERPROFILE"]) == perfil
    assert Path(entorno["HOME"]) == perfil
    assert Path(entorno["APPDATA"]).is_dir()
    assert Path(entorno["LOCALAPPDATA"]).is_dir()
    assert Path(entorno["TEMP"]) == temporal
    assert Path(entorno["TMP"]) == temporal
    assert {
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "DOCKER_AUTH_CONFIG",
        "SSH_AUTH_SOCK",
        "SKY_CLAW_MO2_ROOT",
        "PYTHONPATH",
    }.isdisjoint(entorno)


def test_smoke_arranca_un_ejecutable_real_con_home_aislado() -> None:
    # El intérprete sirve como ejecutable real de prueba: --help termina en cero
    # en Windows y POSIX, sin requerir un binario Sky-Claw dentro de pytest.
    smoke_frozen_executable(Path(sys.executable))


def test_codigo_de_salida_no_cero_incluye_salida_y_crash_log(tmp_path: Path) -> None:
    _, _, crash_log = _rutas_de_salida(tmp_path)
    codigo = (
        "import pathlib,sys;"
        "p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);"
        "p.write_text('fallo de arranque empaquetado',encoding='utf-8');"
        "print('salida normal');"
        "print('ImportError: modulo ausente',file=sys.stderr);"
        "sys.exit(17)"
    )

    with pytest.raises(FrozenExecutableSmokeError) as error:
        _run_smoke_command(
            [sys.executable, "-c", codigo, str(crash_log)],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_s=DEFAULT_TIMEOUT_SECONDS,
            stdout_path=tmp_path / "stdout.bin",
            stderr_path=tmp_path / "stderr.bin",
            crash_log=crash_log,
        )

    mensaje = str(error.value)
    assert "código 17" in mensaje
    assert "salida normal" in mensaje
    assert "ImportError: modulo ausente" in mensaje
    assert "fallo de arranque empaquetado" in mensaje


def test_salida_diagnostica_se_lee_con_limite(tmp_path: Path) -> None:
    salida, error, crash_log = _rutas_de_salida(tmp_path)
    codigo = "import sys;sys.stdout.write('x'*20000);sys.stderr.write('y'*20000);sys.exit(17)"

    with pytest.raises(FrozenExecutableSmokeError) as fallo:
        _run_smoke_command(
            [sys.executable, "-c", codigo],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_s=DEFAULT_TIMEOUT_SECONDS,
            stdout_path=salida,
            stderr_path=error,
            crash_log=crash_log,
        )

    assert len(str(fallo.value)) < 9_000
    assert "código 17" in str(fallo.value)


def test_timeout_mata_el_arbol_y_devuelve_diagnostico_acotado(tmp_path: Path) -> None:
    marcador = tmp_path / "descendiente-sobrevivio.txt"
    pid_file = tmp_path / "descendiente.pid"
    hijo = "import pathlib,sys,time;time.sleep(2.5);pathlib.Path(sys.argv[1]).write_text('sobrevivio',encoding='utf-8')"
    padre = (
        "import pathlib,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]],"
        "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));"
        "pathlib.Path(sys.argv[3]).write_text(str(child.pid),encoding='ascii');"
        "print('stdout parcial',flush=True);"
        "print('stderr parcial',file=sys.stderr,flush=True);"
        "time.sleep(30)"
    )
    salida, error, crash_log = _rutas_de_salida(tmp_path)
    comando = [sys.executable, "-c", padre, hijo, str(marcador), str(pid_file)]
    inicio = time.monotonic()
    with pytest.raises(FrozenExecutableSmokeError) as timeout:
        _run_smoke_command(
            comando,
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout_s=1.5,
            stdout_path=salida,
            stderr_path=error,
            crash_log=crash_log,
        )

    assert timeout.value.cleanup_warning is None
    assert "superó el timeout" in str(timeout.value)
    assert pid_file.is_file(), "el proceso padre no llegó a crear el descendiente"
    assert int(pid_file.read_text(encoding="ascii")) > 0
    assert "stdout parcial" in salida.read_bytes().decode("utf-8")
    assert "stderr parcial" in error.read_bytes().decode("utf-8")

    limite = time.monotonic() + 2.0
    while not marcador.exists() and time.monotonic() < limite:
        time.sleep(0.05)
    assert not marcador.exists(), "un proceso descendiente sobrevivió al timeout del smoke"
    assert time.monotonic() - inicio < 15, "el timeout o la limpieza del árbol no respetaron su límite"


def test_timeout_invalido_se_rechaza_antes_de_lanzar(monkeypatch: pytest.MonkeyPatch) -> None:
    lanzado = False

    def no_debe_lanzar(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal lanzado
        lanzado = True
        raise AssertionError("no se debe invocar el proceso con un timeout inválido")

    monkeypatch.setattr(smoke.subprocess, "Popen", no_debe_lanzar)

    with pytest.raises(FrozenExecutableSmokeError, match="timeout debe ser finito y mayor que cero"):
        smoke_frozen_executable(Path(sys.executable), timeout_s=float("nan"))

    assert lanzado is False


def test_salida_de_consola_windows_decodifica_utf8_y_cp1252() -> None:
    assert _decodificar_salida("café".encode()) == "café"
    assert _decodificar_salida(b"caf\xe9") == "café"


def test_main_retorna_cero_y_muestra_aprobacion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    llamada: dict[str, object] = {}

    def aprobado(ejecutable: Path, *, timeout_s: float) -> None:
        llamada["ejecutable"] = ejecutable
        llamada["timeout_s"] = timeout_s

    monkeypatch.setattr(smoke, "smoke_frozen_executable", aprobado)
    ejecutable = tmp_path / "SkyClawApp.exe"

    assert main([str(ejecutable)]) == 0
    assert llamada == {"ejecutable": ejecutable, "timeout_s": DEFAULT_TIMEOUT_SECONDS}
    assert "aprobado" in capsys.readouterr().out


def test_main_retorna_uno_y_muestra_fallo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def fallido(*args: object, **kwargs: object) -> None:
        raise FrozenExecutableSmokeError("causa de prueba")

    monkeypatch.setattr(smoke, "smoke_frozen_executable", fallido)

    assert main([str(tmp_path / "SkyClawApp.exe")]) == 1
    assert "causa de prueba" in capsys.readouterr().err

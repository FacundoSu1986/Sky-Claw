"""Contrato del smoke de arranque del ejecutable congelado en CI."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.smoke_frozen_executable import (
    DEFAULT_TIMEOUT_SECONDS,
    FrozenExecutableSmokeError,
    smoke_frozen_executable,
)


def _ejecutable_falso(tmp_path: Path) -> Path:
    ejecutable = tmp_path / "SkyClawApp.exe"
    ejecutable.write_bytes(b"ejecutable de prueba")
    return ejecutable


def test_falla_cerrado_si_el_ejecutable_no_existe(tmp_path: Path) -> None:
    with pytest.raises(FrozenExecutableSmokeError, match="no existe"):
        smoke_frozen_executable(tmp_path / "ausente.exe")


def test_ejecuta_ayuda_con_home_temporal_y_sin_secretos_del_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ejecutable = _ejecutable_falso(tmp_path)
    monkeypatch.setenv("USERPROFILE", "C:/usuario-real")
    monkeypatch.setenv("OPENAI_API_KEY", "secreto-de-prueba")
    monkeypatch.setenv("GITHUB_TOKEN", "token-de-prueba")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "clave-de-prueba")
    monkeypatch.setenv("SKY_CLAW_MO2_ROOT", "C:/config-real")
    monkeypatch.setenv("PYTHONPATH", "C:/modulos-externos")
    llamada: dict[str, object] = {}

    def ejecutar(comando: list[str], **opciones: object) -> subprocess.CompletedProcess[str]:
        llamada["comando"] = comando
        llamada["opciones"] = opciones
        entorno = opciones["env"]
        assert isinstance(entorno, dict)
        assert Path(str(entorno["USERPROFILE"])).is_dir()
        assert Path(str(entorno["APPDATA"])).is_dir()
        assert Path(str(entorno["LOCALAPPDATA"])).is_dir()
        assert Path(str(entorno["TEMP"])).is_dir()
        assert Path(str(entorno["TMP"])).is_dir()
        assert "OPENAI_API_KEY" not in entorno
        assert "GITHUB_TOKEN" not in entorno
        assert "AWS_ACCESS_KEY_ID" not in entorno
        assert "SKY_CLAW_MO2_ROOT" not in entorno
        assert "PYTHONPATH" not in entorno
        return subprocess.CompletedProcess(comando, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", ejecutar)
    smoke_frozen_executable(ejecutable)

    assert llamada["comando"] == [str(ejecutable.resolve()), "--help"]
    opciones = llamada["opciones"]
    assert isinstance(opciones, dict)
    assert opciones["timeout"] == DEFAULT_TIMEOUT_SECONDS
    assert opciones["capture_output"] is True
    assert opciones["check"] is False
    assert opciones["shell"] is False
    assert not Path(str(opciones["env"]["USERPROFILE"])).exists()


def test_codigo_de_salida_no_cero_incluye_evidencia_del_fallo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ejecutable = _ejecutable_falso(tmp_path)

    def fallar(comando: list[str], **opciones: object) -> subprocess.CompletedProcess[str]:
        entorno = opciones["env"]
        assert isinstance(entorno, dict)
        log = Path(str(entorno["USERPROFILE"])) / ".sky_claw" / "logs" / "crash.log"
        log.parent.mkdir(parents=True)
        log.write_text("fallo de arranque empaquetado", encoding="utf-8")
        return subprocess.CompletedProcess(comando, 17, stdout="", stderr="ImportError: modulo ausente")

    monkeypatch.setattr(subprocess, "run", fallar)

    with pytest.raises(FrozenExecutableSmokeError) as error:
        smoke_frozen_executable(ejecutable)

    mensaje = str(error.value)
    assert "código 17" in mensaje
    assert "ImportError: modulo ausente" in mensaje
    assert "fallo de arranque empaquetado" in mensaje


def test_timeout_es_fail_closed_y_diagnosticable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ejecutable = _ejecutable_falso(tmp_path)

    def colgar(comando: list[str], **opciones: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            comando, opciones["timeout"], output=b"stdout parcial", stderr=b"stderr parcial"
        )

    monkeypatch.setattr(subprocess, "run", colgar)

    with pytest.raises(FrozenExecutableSmokeError, match="superó el timeout") as error:
        smoke_frozen_executable(ejecutable, timeout_s=0.25)

    assert "stdout parcial" in str(error.value)
    assert "stderr parcial" in str(error.value)


def test_timeout_invalido_se_rechaza_antes_de_lanzar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ejecutable = _ejecutable_falso(tmp_path)
    lanzado = False

    def no_debe_lanzar(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal lanzado
        lanzado = True
        raise AssertionError("no se debe invocar el proceso con un timeout inválido")

    monkeypatch.setattr(subprocess, "run", no_debe_lanzar)

    with pytest.raises(FrozenExecutableSmokeError, match="timeout debe ser finito y mayor que cero"):
        smoke_frozen_executable(ejecutable, timeout_s=float("nan"))

    assert lanzado is False

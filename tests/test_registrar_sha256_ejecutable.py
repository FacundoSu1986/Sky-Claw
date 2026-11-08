"""Registro y verificación del SHA-256 del ejecutable congelado que producen los workflows.

La evidencia sobre el binario final solo vale si se sabe CUÁL fue. Este script es la única
implementación que usan los tres workflows que construyen ``SkyClawApp.exe`` (``ci.yml``,
``release.yml`` y el laboratorio de Windows); el ancla que los enumera vive en
``tests/test_release_smoke_wiring.py``.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from scripts import registrar_sha256_ejecutable as reg

_VARIABLES_DE_CI = (
    "GITHUB_SHA",
    "GITHUB_WORKFLOW",
    "GITHUB_JOB",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_STEP_SUMMARY",
)


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test hereda el entorno real de GitHub Actions (el CI corre estos mismos tests)."""
    for nombre in _VARIABLES_DE_CI:
        monkeypatch.delenv(nombre, raising=False)


def _exe(tmp_path: pathlib.Path, contenido: bytes = b"MZ-ejecutable-falso") -> pathlib.Path:
    ruta = tmp_path / "SkyClawApp.exe"
    ruta.write_bytes(contenido)
    return ruta


# ---------------------------------------------------------------------------
# registrar
# ---------------------------------------------------------------------------


def test_registrar_escribe_el_sha256_y_el_tamano_correctos(tmp_path: pathlib.Path) -> None:
    # Arrange
    exe = _exe(tmp_path)
    salida = tmp_path / "resultados_sha256.json"

    # Act
    codigo = reg.main(["registrar", str(exe), "--json", str(salida)])

    # Assert
    datos = json.loads(salida.read_text(encoding="utf-8"))
    assert codigo == reg.EXIT_OK
    assert datos["archivo"] == "SkyClawApp.exe"
    assert datos["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert datos["bytes"] == exe.stat().st_size


def test_registrar_imprime_el_hash_en_una_linea_para_el_log(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    exe = _exe(tmp_path)

    # Act
    reg.main(["registrar", str(exe)])

    # Assert: el log del job es lo primero que se mira; el hash va completo y en minúsculas.
    esperado = hashlib.sha256(exe.read_bytes()).hexdigest()
    assert f"sha256={esperado}" in capsys.readouterr().out


def test_registrar_hashea_por_bloques_un_archivo_mayor_que_un_bloque(tmp_path: pathlib.Path) -> None:
    # Arrange: el ejecutable real pesa decenas de MB; no se puede leer entero en memoria sin necesidad.
    contenido = bytes(range(256)) * (3 * reg.TAMANO_DE_BLOQUE // 256 + 7)
    exe = _exe(tmp_path, contenido)
    salida = tmp_path / "r.json"

    # Act
    reg.main(["registrar", str(exe), "--json", str(salida)])

    # Assert
    assert len(contenido) > reg.TAMANO_DE_BLOQUE
    assert json.loads(salida.read_text(encoding="utf-8"))["sha256"] == hashlib.sha256(contenido).hexdigest()


def test_registrar_incluye_el_contexto_del_run_que_lo_construyo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_WORKFLOW", "VFS supervision sandbox (Windows)")
    monkeypatch.setenv("GITHUB_JOB", "sandbox")
    monkeypatch.setenv("GITHUB_RUN_ID", "42512107684")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    salida = tmp_path / "r.json"

    # Act
    reg.main(["registrar", str(_exe(tmp_path)), "--json", str(salida)])

    # Assert
    datos = json.loads(salida.read_text(encoding="utf-8"))
    assert datos["commit"] == "a" * 40
    assert datos["workflow"] == "VFS supervision sandbox (Windows)"
    assert datos["job"] == "sandbox"
    assert datos["run_id"] == "42512107684"
    assert datos["run_attempt"] == "1"


def test_registrar_fuera_de_ci_deja_el_contexto_en_none_y_no_falla(tmp_path: pathlib.Path) -> None:
    # Arrange
    salida = tmp_path / "r.json"

    # Act
    codigo = reg.main(["registrar", str(_exe(tmp_path)), "--json", str(salida)])

    # Assert: correrlo en una máquina de desarrollo no debe inventar un commit ni romperse.
    datos = json.loads(salida.read_text(encoding="utf-8"))
    assert codigo == reg.EXIT_OK
    assert datos["commit"] is None
    assert datos["run_id"] is None


def test_registrar_agrega_al_resumen_del_job_sin_pisar_lo_anterior(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: otros pasos ya escribieron en el resumen del job.
    resumen = tmp_path / "resumen.md"
    resumen.write_text("## Pasos previos\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(resumen))
    exe = _exe(tmp_path)

    # Act
    reg.main(["registrar", str(exe)])

    # Assert
    texto = resumen.read_text(encoding="utf-8")
    assert texto.startswith("## Pasos previos\n")
    assert hashlib.sha256(exe.read_bytes()).hexdigest() in texto
    assert "SkyClawApp.exe" in texto


def test_registrar_con_un_archivo_inexistente_falla_y_no_escribe_el_json(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    salida = tmp_path / "r.json"

    # Act
    codigo = reg.main(["registrar", str(tmp_path / "no_existe.exe"), "--json", str(salida)])

    # Assert: un registro de un ejecutable que no está sería evidencia falsa.
    assert codigo == reg.EXIT_ERROR
    assert not salida.exists()
    assert "no_existe.exe" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# verificar
# ---------------------------------------------------------------------------


def test_verificar_acepta_el_mismo_ejecutable(tmp_path: pathlib.Path) -> None:
    # Arrange
    exe = _exe(tmp_path)
    registro = tmp_path / "r.json"
    reg.main(["registrar", str(exe), "--json", str(registro)])

    # Act
    codigo = reg.main(["verificar", str(registro), str(exe)])

    # Assert
    assert codigo == reg.EXIT_OK


def test_verificar_rechaza_un_ejecutable_modificado_despues_de_registrarlo(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange: el hash solo es evidencia si el ejecutable PROBADO es el que se hasheó.
    exe = _exe(tmp_path)
    registro = tmp_path / "r.json"
    reg.main(["registrar", str(exe), "--json", str(registro)])
    exe.write_bytes(exe.read_bytes() + b"\x00")

    # Act
    codigo = reg.main(["verificar", str(registro), str(exe)])

    # Assert
    assert codigo == reg.EXIT_DISTINTO
    assert "distinto" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "contenido_del_registro",
    [
        pytest.param("no es json", id="json_ilegible"),
        pytest.param("[]", id="no_es_un_objeto"),
        pytest.param('{"archivo": "SkyClawApp.exe"}', id="sin_sha256"),
        pytest.param('{"sha256": 7}', id="sha256_no_es_texto"),
        pytest.param('{"sha256": "abc"}', id="sha256_con_formato_invalido"),
        pytest.param('{"sha256": "' + "A" * 64 + '"}', id="sha256_en_mayusculas"),
    ],
)
def test_verificar_con_un_registro_invalido_es_error_y_no_aprueba(
    tmp_path: pathlib.Path, contenido_del_registro: str
) -> None:
    # Arrange
    exe = _exe(tmp_path)
    registro = tmp_path / "r.json"
    registro.write_text(contenido_del_registro, encoding="utf-8")

    # Act
    codigo = reg.main(["verificar", str(registro), str(exe)])

    # Assert: ante cualquier duda, NO se aprueba (fail-closed).
    assert codigo == reg.EXIT_ERROR


def test_verificar_con_el_registro_o_el_ejecutable_ausentes_es_error(tmp_path: pathlib.Path) -> None:
    # Arrange
    exe = _exe(tmp_path)
    registro = tmp_path / "r.json"
    reg.main(["registrar", str(exe), "--json", str(registro)])

    # Act / Assert
    assert reg.main(["verificar", str(tmp_path / "falta.json"), str(exe)]) == reg.EXIT_ERROR
    assert reg.main(["verificar", str(registro), str(tmp_path / "falta.exe")]) == reg.EXIT_ERROR

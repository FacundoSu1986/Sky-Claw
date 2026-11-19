"""Anclas del harness de P7-A5-COMPAT: un resultado inválido NO es un éxito.

Contexto
--------
Las sondas de ``docs/validation/2026-11-17_p7_a5_compat_mo2/probe/`` publicaban
``{"ok": true}`` aunque la medición hubiera quedado a medias: ``leer_json``
devolvía un dict de error en vez de fallar, ``run_probe`` devolvía el error como
si fuera un resultado, ``matrix_win32.agredir`` emitía ``SIN_SALIDA(rc=…)`` como
valor ordinario y los ``proc.wait()`` no miraban ``returncode``.

Estas anclas fijan la propiedad contraria: **un bloqueo esperado es un resultado
válido, un proceso muerto o un JSON ausente no lo son**. Por eso hay controles
positivos: si todo fallara cerrado, ``test_exigir_bloqueo_acepta_…`` y los dos
controles de éxito se rompen.

Se cargan las sondas por ruta porque viven fuera del paquete (mismo criterio que
``tests/test_rig_cli_prereqs_wm_close.py``).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PROBE = RAIZ / "docs" / "validation" / "2026-11-17_p7_a5_compat_mo2" / "probe"

if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))


def _cargar(nombre: str, archivo: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(nombre, PROBE / archivo)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo


@pytest.fixture(scope="module")
def sonda() -> types.ModuleType:
    return _cargar("probe_qsettings_bajo_test", "probe_qsettings.py")


@pytest.fixture(scope="module")
def matriz() -> types.ModuleType:
    return _cargar("matrix_win32_bajo_test", "matrix_win32.py")


class _ProcFalso:
    """Proceso inerte con la superficie que usan las sondas."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.stdout, self.stderr

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        return None


# ---------------------------------------------------------------------------
# A — leer_json: parcial NO es resultado
# ---------------------------------------------------------------------------


def test_leer_json_ausente_falla(sonda: types.ModuleType, tmp_path: pathlib.Path) -> None:
    with pytest.raises(sonda.HarnessError, match="no se produjo el JSON"):
        sonda.leer_json(tmp_path / "no-existe.json", "cliente A")


def test_leer_json_malformado_falla(sonda: types.ModuleType, tmp_path: pathlib.Path) -> None:
    roto = tmp_path / "roto.json"
    roto.write_text("{ esto no es json", encoding="utf-8")
    with pytest.raises(sonda.HarnessError, match="JSON ilegible"):
        sonda.leer_json(roto, "cliente A")


def test_leer_json_no_objeto_falla(sonda: types.ModuleType, tmp_path: pathlib.Path) -> None:
    lista = tmp_path / "lista.json"
    lista.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(sonda.HarnessError, match="no es un objeto"):
        sonda.leer_json(lista, "cliente A")


def test_leer_json_valido_devuelve_el_objeto(sonda: types.ModuleType, tmp_path: pathlib.Path) -> None:
    """Control positivo: el camino bueno sigue funcionando."""
    bueno = tmp_path / "ok.json"
    bueno.write_text(json.dumps({"status_tras_leer": "AccessError"}), encoding="utf-8")
    assert sonda.leer_json(bueno, "cliente A") == {"status_tras_leer": "AccessError"}


# ---------------------------------------------------------------------------
# B — códigos de salida
# ---------------------------------------------------------------------------


def test_exigir_codigo_rechaza_rc_distinto_de_cero(sonda: types.ModuleType) -> None:
    with pytest.raises(sonda.HarnessError, match="rc=3"):
        sonda.exigir_codigo(_ProcFalso(returncode=3), "cliente A", "murió")


def test_exigir_codigo_acepta_cero(sonda: types.ModuleType) -> None:
    sonda.exigir_codigo(_ProcFalso(returncode=0), "cliente A")


# ---------------------------------------------------------------------------
# C — bloqueos esperados son resultados VÁLIDOS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("codigo", ["32", "5", "13"])
def test_exigir_bloqueo_acepta_un_bloqueo_medido(matriz: types.ModuleType, codigo: str) -> None:
    assert matriz.exigir_bloqueo(f"BLOQUEADO:{codigo}", "matriz/escribir") == codigo


@pytest.mark.parametrize("salida", ["", "   ", "SIN_SALIDA(rc=1,err='boom')", "BLOQUEADO:", "EXITO"])
def test_exigir_bloqueo_rechaza_salidas_que_no_son_bloqueo(matriz: types.ModuleType, salida: str) -> None:
    with pytest.raises(matriz.HarnessError):
        matriz.exigir_bloqueo(salida, "matriz/escribir")


def test_exigir_marca_acepta_el_exito_esperado(matriz: types.ModuleType) -> None:
    matriz.exigir_marca("EXITO", "control", "EXITO")


def test_exigir_marca_rechaza_otra_cosa(matriz: types.ModuleType) -> None:
    with pytest.raises(matriz.HarnessError):
        matriz.exigir_marca("BLOQUEADO:32", "control", "EXITO")


# ---------------------------------------------------------------------------
# D — run_probe: un error de ejecución no es un resultado
# ---------------------------------------------------------------------------


def test_run_probe_sin_salida_json_falla(sonda: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sonda.subprocess, "run", lambda *a, **k: _ProcFalso(returncode=0, stdout="nada"))
    with pytest.raises(sonda.HarnessError, match="sin salida JSON"):
        sonda.run_probe("acquirer.py", "x")


def test_run_probe_con_rc_distinto_falla_aunque_imprima_json(
    sonda: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El código manda: un JSON en stdout no absuelve un proceso que falló."""
    monkeypatch.setattr(
        sonda.subprocess, "run", lambda *a, **k: _ProcFalso(returncode=2, stdout='{"estado": "X"}', stderr="boom")
    )
    with pytest.raises(sonda.HarnessError, match="rc=2"):
        sonda.run_probe("acquirer.py", "x")


def test_run_probe_devuelve_el_json_valido(sonda: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sonda.subprocess, "run", lambda *a, **k: _ProcFalso(returncode=0, stdout='ruido\n{"autoriza": true}')
    )
    assert sonda.run_probe("acquirer.py", "x") == {"autoriza": True}


# ---------------------------------------------------------------------------
# E — exigir_exito: cliente muerto, sin JSON o con JSON roto
# ---------------------------------------------------------------------------


def _handle(tmp_path: pathlib.Path, proc: _ProcFalso, contenido: str | None) -> dict[str, object]:
    salida = tmp_path / "cliente.json"
    if contenido is not None:
        salida.write_text(contenido, encoding="utf-8")
    return {"proc": proc, "salida": salida}


def test_exigir_exito_rechaza_cliente_muerto(sonda: types.ModuleType, tmp_path: pathlib.Path) -> None:
    h = _handle(tmp_path, _ProcFalso(returncode=1, stderr="traceback"), json.dumps({"a": 1}))
    with pytest.raises(sonda.HarnessError, match="rc=1"):
        sonda.exigir_exito(h, "A")


def test_exigir_exito_rechaza_cliente_sin_json(sonda: types.ModuleType, tmp_path: pathlib.Path) -> None:
    h = _handle(tmp_path, _ProcFalso(returncode=0), None)
    with pytest.raises(sonda.HarnessError, match="no se produjo el JSON"):
        sonda.exigir_exito(h, "A")


def test_exigir_exito_devuelve_el_json_del_cliente(sonda: types.ModuleType, tmp_path: pathlib.Path) -> None:
    """Control positivo: AccessError como VALOR es un resultado aceptado."""
    h = _handle(tmp_path, _ProcFalso(returncode=0), json.dumps({"status_tras_leer": "AccessError"}))
    assert sonda.exigir_exito(h, "A") == {"status_tras_leer": "AccessError"}


# ---------------------------------------------------------------------------
# F — medir() aborta: no se publica evidencia parcial
# ---------------------------------------------------------------------------


class _TenedorFalso:
    """Tenedor que dice haber tomado y liberado la exclusión."""

    def __init__(self, etiqueta: str) -> None:
        self.etiqueta = etiqueta

    def esperar_ready(self, limite_s: float = 60.0) -> dict[str, object]:
        return {"estado": "EXCLUSION_ESTABLISHED", "autoriza": True, "motivo": "falso"}

    def liberar(self, limite_s: float = 60.0) -> None:
        return None


def _preparar_instance(sonda: types.ModuleType, tmp_path: pathlib.Path) -> pathlib.Path:
    instancia = tmp_path / "instance"
    instancia.mkdir(parents=True, exist_ok=True)
    ini = instancia / "modorganizer.ini"
    ini.write_text("[General]\ngameName=Skyrim Special Edition\n", encoding="utf-8")
    return ini


def test_medir_aborta_si_el_cliente_muere(
    sonda: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ini = _preparar_instance(sonda, tmp_path)
    monkeypatch.setattr(sonda, "RUN", tmp_path)
    monkeypatch.setattr(sonda, "INSTANCIA", ini.parent)
    monkeypatch.setattr(sonda, "INI", ini)
    monkeypatch.setattr(sonda, "autorar_ini", lambda: None)
    monkeypatch.setattr(sonda, "Tenedor", _TenedorFalso)
    monkeypatch.setattr(
        sonda,
        "hijo",
        lambda *a, **k: {"proc": _ProcFalso(returncode=1, stderr="boom"), "salida": tmp_path / "no.json"},
    )
    with pytest.raises(sonda.HarnessError, match="rc=1"):
        sonda.medir()


def test_medir_aborta_si_el_holder_no_libera(
    sonda: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un tenedor que no confirma la liberación no puede dar por buena la corrida."""
    ini = _preparar_instance(sonda, tmp_path)
    monkeypatch.setattr(sonda, "RUN", tmp_path)
    monkeypatch.setattr(sonda, "INSTANCIA", ini.parent)
    monkeypatch.setattr(sonda, "INI", ini)
    monkeypatch.setattr(sonda, "autorar_ini", lambda: None)

    class _TenedorQueNoLibera(_TenedorFalso):
        def liberar(self, limite_s: float = 60.0) -> None:
            raise sonda.HarnessError("tenedor A: no liberó el handle")

    monkeypatch.setattr(sonda, "Tenedor", _TenedorQueNoLibera)

    def _hijo_con_json(*a: object, **k: object) -> dict[str, object]:
        salida = tmp_path / "cliente.json"
        salida.write_text(json.dumps({"status_tras_leer": "AccessError", "n_claves": 0}), encoding="utf-8")
        return {"proc": _ProcFalso(returncode=0), "salida": salida}

    monkeypatch.setattr(sonda, "hijo", _hijo_con_json)
    with pytest.raises(sonda.HarnessError, match="no liberó"):
        sonda.medir()


def test_main_no_publica_ok_si_medir_falla(
    sonda: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sonda.sys, "argv", ["probe_qsettings.py", sys.executable, str(tmp_path)])
    monkeypatch.setattr(sonda, "medir", lambda: (_ for _ in ()).throw(sonda.HarnessError("medición inválida")))
    with pytest.raises(sonda.HarnessError):
        sonda.main()
    assert "ok" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# G — matriz: SIN_SALIDA ya no es un resultado
# ---------------------------------------------------------------------------


def test_agredir_rechaza_salida_vacia(
    matriz: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(matriz, "RUN", tmp_path)
    monkeypatch.setattr(matriz.subprocess, "run", lambda *a, **k: _ProcFalso(returncode=0, stdout=""))
    with pytest.raises(matriz.HarnessError, match="sin resultado válido"):
        matriz.agredir("escribir", tmp_path / "ini")


def test_agredir_rechaza_rc_distinto(
    matriz: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(matriz, "RUN", tmp_path)
    monkeypatch.setattr(matriz.subprocess, "run", lambda *a, **k: _ProcFalso(returncode=2, stdout="EXITO"))
    with pytest.raises(matriz.HarnessError, match="rc=2"):
        matriz.agredir("escribir", tmp_path / "ini")


def test_agredir_devuelve_el_bloqueo_medido(
    matriz: types.ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control positivo: el bloqueo esperado atraviesa la validación."""
    monkeypatch.setattr(matriz, "RUN", tmp_path)
    monkeypatch.setattr(matriz.subprocess, "run", lambda *a, **k: _ProcFalso(returncode=0, stdout="BLOQUEADO:32"))
    assert matriz.agredir("escribir", tmp_path / "ini") == "BLOQUEADO:32"


def test_matrix_main_no_publica_ok_si_medir_falla(
    matriz: types.ModuleType,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(matriz.sys, "argv", ["matrix_win32.py", str(tmp_path)])
    monkeypatch.setattr(matriz, "medir", lambda: (_ for _ in ()).throw(matriz.HarnessError("medición inválida")))
    with pytest.raises(matriz.HarnessError):
        matriz.main()
    assert "ok" not in capsys.readouterr().out

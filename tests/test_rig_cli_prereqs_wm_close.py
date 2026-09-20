"""Contrato de cierre ``WM_CLOSE`` del rig de prerrequisitos CLI (#593, PR #597).

El rig vive fuera del paquete (``docs/validation/...``): se carga por ruta, como
la sonda de ``test_dyndolod_uia_preflight.py``. Hallazgo de review (CodeRabbit):
``cerrar_ventana_del_pid`` devolvía la cantidad de ventanas ENCONTRADAS y
descartaba el retorno de ``PostMessageW``, así que un envío fallido igual
producía ``cerrado=True``. Acá se ancla la propiedad: el valor devuelto es la
cantidad de envíos que devolvieron distinto de cero.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
RIG = RAIZ / "docs" / "validation" / "2026-09-19_pr593_cli_prereqs" / "rig_cli_prereqs.py"

#: Claves que el rig pisa al importarse (código top-level); monkeypatch las restaura.
_CLAVES_DEL_ENTORNO = (
    "SKYRIM_PATH",
    "MO2_PATH",
    "MO2_MODS_PATH",
    "DYNDLOD_EXE",
    "TEXGEN_EXE",
    "DYNDLOD_INI_DIR",
)


def _cargar_rig(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Carga el rig con ``sys.argv`` y entorno mínimos (no ejecuta ``main``)."""
    monkeypatch.setattr(sys, "argv", ["rig_cli_prereqs.py", str(RAIZ), str(tmp_path / "sesion")])
    for clave in _CLAVES_DEL_ENTORNO:
        monkeypatch.setenv(clave, os.environ.get(clave, ""))
    spec = importlib.util.spec_from_file_location("rig_cli_prereqs", RIG)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    if sys.path and sys.path[0] == str(RAIZ):
        sys.path.pop(0)
    return modulo


def _user32_falso(resultados: dict[int, int]) -> tuple[SimpleNamespace, list[tuple[int, int, int, int]]]:
    llamadas: list[tuple[int, int, int, int]] = []

    def post_message(hwnd: int, mensaje: int, wparam: int, lparam: int) -> int:
        llamadas.append((hwnd, mensaje, wparam, lparam))
        return resultados[hwnd]

    return SimpleNamespace(PostMessageW=post_message), llamadas


def test_cierre_cuenta_solo_los_envios_exitosos(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dos ventanas enumeradas, un envío exitoso y uno fallido: devuelve 1."""
    modulo = _cargar_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(modulo, "_ventanas_del_pid", lambda _pid: [101, 102])
    user32, llamadas = _user32_falso({101: 1, 102: 0})
    monkeypatch.setattr(modulo, "ctypes", SimpleNamespace(windll=SimpleNamespace(user32=user32)))

    assert modulo.cerrar_ventana_del_pid(1234) == 1
    assert llamadas == [(101, modulo._WM_CLOSE, 0, 0), (102, modulo._WM_CLOSE, 0, 0)]


def test_cierre_devuelve_cero_cuando_todos_los_envios_fallan(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PostMessageW`` => 0 en todas las ventanas: el conteo no es la enumeración."""
    modulo = _cargar_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(modulo, "_ventanas_del_pid", lambda _pid: [101, 102])
    user32, _ = _user32_falso({101: 0, 102: 0})
    monkeypatch.setattr(modulo, "ctypes", SimpleNamespace(windll=SimpleNamespace(user32=user32)))

    assert modulo.cerrar_ventana_del_pid(1234) == 0

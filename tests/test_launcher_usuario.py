"""Superficie de usuario del launcher Windows (``SkyClawApp.bat``).

Dos defectos de la MISMA familia quedaron sin arreglar cuando se corrigió su
hermano en ``build.bat`` (CHANGELOG 0.2.1: "apuntaba a ``venv\\`` en vez del
``.venv\\`` real del repo"):

1. El launcher seguía buscando ``venv\\Scripts\\python.exe``: en un clon nuevo
   no lo encontraba (el repo usa ``.venv``, creado por ``build.bat``/``uv``),
   caía al Python del sistema y moría con ``ModuleNotFoundError``.
2. El aviso de puerto miraba 8888, que no lo bindea nadie: la GUI corre en 8080
   (NiceGUI) y 8765 (API ``/api/chat``), así que el conflicto real
   (``WinError 10048`` al reabrir) pasaba sin aviso.

3. Los mensajes ``echo`` llevaban un ``!`` suelto (``echo [!] ALERTA``): con
   ``setlocal enabledelayedexpansion`` (que estos ``.bat`` necesitan para
   ``!PY_CMD!``) el parser toma el ``!`` como inicio de variable y lo descarta,
   así que el aviso salía como ``[] ALERTA``.

Los anclajes enumeran, no muestrean:

- la familia de ``.bat`` de raíz queda congelada por igualdad contra el filesystem:
  si aparece un hermano nuevo, CI obliga a clasificarlo explícitamente;
- el venv se verifica sobre TODOS los ``.bat`` de arranque del repo, con el
  mismo criterio que ``tests/test_pyinstaller.py::test_build_bat_uses_dot_venv``;
- los puertos del aviso se comparan por igualdad contra los que bindean
  ``gui_mode.py`` y ``_bootloader.py``, así que cambiar un puerto en cualquiera
  de los dos lados rompe el test hasta que el launcher siga a la GUI;
- los ``echo`` se recorren todos: cada ``!`` tiene que ser una expansión
  pareada (``!VAR!``) o el escape documentado (``^^!``).
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent

#: Familia explícitamente aceptada de lanzadores .bat en la raíz del repo.
BATS_DE_ARRANQUE = ("SkyClawApp.bat", "build.bat")


def _leer_bat(nombre: str) -> str:
    return (REPO_ROOT / nombre).read_text(encoding="utf-8")


def test_la_familia_de_bats_de_raiz_esta_congelada() -> None:
    """Un .bat nuevo exige clasificar explícitamente si pertenece a esta familia."""
    encontrados = {ruta.name for ruta in REPO_ROOT.glob("*.bat")}
    assert encontrados == set(BATS_DE_ARRANQUE), (
        f"Familia .bat cambió: esperados={sorted(BATS_DE_ARRANQUE)}, encontrados={sorted(encontrados)}"
    )


@pytest.mark.parametrize("nombre", BATS_DE_ARRANQUE)
def test_los_bat_de_arranque_apuntan_al_dot_venv_del_repo(nombre: str) -> None:
    """El entorno real del repo es ``.venv`` (uv/build.bat), nunca ``venv\\``."""
    contenido = _leer_bat(nombre)

    assert ".venv" in contenido, f"{nombre} no referencia el .venv del repo"
    assert not re.search(r"(?<!\.)\bvenv\\", contenido), f"{nombre} referencia un 'venv\\' pelado en vez de '.venv\\'"


def _puertos_bindeados_por_la_gui() -> set[int]:
    """Puertos que la GUI levanta, derivados de las líneas que los bindean."""
    gui_mode = (REPO_ROOT / "sky_claw" / "app" / "modes" / "gui_mode.py").read_text(encoding="utf-8")
    bootloader = (REPO_ROOT / "sky_claw" / "app" / "gui" / "_bootloader.py").read_text(encoding="utf-8")

    puertos: set[int] = set()

    nicegui = re.search(r"run_nicegui\(\s*args\s*,\s*port=(\d+)", gui_mode)
    assert nicegui, "gui_mode.py no llama a run_nicegui(args, port=N)"
    puertos.add(int(nicegui.group(1)))

    api = re.findall(r'TCPSite\(\s*runner\s*,\s*"[^"]+"\s*,\s*(\d+)\s*\)', bootloader)
    assert api, "_bootloader.py no bindea el API con TCPSite(runner, host, puerto)"
    puertos.update(int(p) for p in api)

    return puertos


def test_el_launcher_avisa_por_los_puertos_que_la_gui_bindea() -> None:
    """El aviso de puerto del launcher cubre exactamente los puertos de la GUI."""
    contenido = _leer_bat("SkyClawApp.bat")

    declarados = re.search(r'set\s+"GUI_PORTS=([\d ]+)"', contenido)
    assert declarados, "SkyClawApp.bat no declara la lista GUI_PORTS"
    avisados = {int(p) for p in declarados.group(1).split()}

    assert re.search(r"netstat[^\n]*findstr", contenido), (
        "SkyClawApp.bat dejó de sondear los puertos con netstat/findstr"
    )

    esperados = _puertos_bindeados_por_la_gui()
    assert avisados == esperados, f"SkyClawApp.bat avisa por {sorted(avisados)} pero la GUI bindea {sorted(esperados)}"


def _echos_con_bang_suelto(contenido: str) -> list[str]:
    """Líneas ``echo`` cuyo texto deja un ``!`` fuera de una expansión pareada.

    Con delayed expansion activo, ``!`` inicia (o cierra) una referencia a
    variable; el ``!`` que no forma par se descarta al imprimir. Las
    expansiones legítimas (``!PY_CMD!``, ``!EXE_PATH!``) y el escape
    documentado (``^^!``) no cuentan como sueltos.
    """
    sueltos: list[str] = []
    for numero, linea in enumerate(contenido.splitlines(), start=1):
        match = re.match(r"\s*echo\s+(?P<texto>.+)$", linea, re.IGNORECASE)
        if not match:
            continue
        resto = re.sub(r"![\w]+!", "", match.group("texto")).replace("^^!", "")
        if "!" in resto:
            sueltos.append(f"{numero}: {linea.strip()}")
    return sueltos


@pytest.mark.parametrize("nombre", BATS_DE_ARRANQUE)
def test_los_mensajes_echo_no_pierden_un_bang_por_delayed_expansion(nombre: str) -> None:
    sueltos = _echos_con_bang_suelto(_leer_bat(nombre))
    assert not sueltos, (
        f"{nombre}: hay '!' sueltos en mensajes echo (delayed expansion los descarta al imprimir): {sueltos}"
    )

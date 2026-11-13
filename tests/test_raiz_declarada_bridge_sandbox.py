"""``XEDIT_BRIDGE_ROOT`` declarada por el operador suma al sandbox (#917).

Antes el bridge solo se aceptaba si ``install_dir`` de ``config.toml`` lo cubría, y ``--install-dir``
no tenía efecto (``config.toml`` lo pisa). La raíz declarada se SUMA a las demás, sin reemplazarlas ni
cambiar ``install_dir`` (que decide dónde se instalan las herramientas descargadas).

La propiedad es del mecanismo: la composición vive en ``_construir_raices_sandbox``, que es la que
consumen TODOS los llamadores (``AppContext`` y el modo VFS de ``__main__``); un test los enumera por AST.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.app_context import _construir_raices_sandbox

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]


def _raices(tmp_path: pathlib.Path, install_dir: pathlib.Path | None) -> list[pathlib.Path]:
    mo2 = tmp_path / "MO2"
    mo2.mkdir(exist_ok=True)
    return _construir_raices_sandbox(mo2_root=mo2, install_dir=install_dir, skyrim_path=None)


def test_bridge_fuera_de_install_dir_se_suma_sin_reemplazar_nada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = tmp_path / "Modding"
    bridge = tmp_path / "otra_unidad" / "BridgeTrabajo"
    bridge.mkdir(parents=True)
    monkeypatch.setenv("XEDIT_BRIDGE_ROOT", str(bridge))

    raices = _raices(tmp_path, install)

    assert bridge.resolve() in [r.resolve() for r in raices]
    assert install in raices  # install_dir sigue: ni reemplazado ni modificado
    PathValidator(roots=raices).validate(bridge / "Admitidos" / "Mod.esp")  # no lanza


@pytest.mark.parametrize("valor", ["", "   ", "relativa/BridgeTrabajo", "NO_EXISTE"])
def test_valores_no_validos_no_agregan_nada(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    sin_variable = _raices(tmp_path, None)
    if valor == "NO_EXISTE":
        valor = str(tmp_path / "no_existe")
    monkeypatch.setenv("XEDIT_BRIDGE_ROOT", valor)

    assert _raices(tmp_path, None) == sin_variable


def test_un_archivo_no_cuenta_como_raiz(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sin_variable = _raices(tmp_path, None)
    archivo = tmp_path / "bridge.txt"
    archivo.write_text("x", encoding="utf-8")
    monkeypatch.setenv("XEDIT_BRIDGE_ROOT", str(archivo))

    assert _raices(tmp_path, None) == sin_variable


def test_la_raiz_declarada_no_habilita_rutas_hermanas(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = tmp_path / "otra_unidad" / "BridgeTrabajo"
    bridge.mkdir(parents=True)
    monkeypatch.setenv("XEDIT_BRIDGE_ROOT", str(bridge))
    validador = PathValidator(roots=_raices(tmp_path, None))

    with pytest.raises(Exception):  # noqa: B017 -- el tipo exacto es del validador; lo que importa es que rechaza
        validador.validate(bridge.parent / "secretos.txt")
    with pytest.raises(Exception):  # noqa: B017
        validador.validate(bridge / ".." / "secretos.txt")


def test_todos_los_llamadores_de_la_composicion_son_los_conocidos() -> None:
    """Congela la familia: un llamador nuevo (o uno que arme raíces por su cuenta) rompe el ancla."""
    llamadores = set()
    for ruta in sorted((RAIZ_REPO / "sky_claw").rglob("*.py")):
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                f = nodo.func
                nombre = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if nombre == "_construir_raices_sandbox":
                    llamadores.add(ruta.relative_to(RAIZ_REPO).as_posix())
    assert llamadores == {"sky_claw/app_context.py", "sky_claw/__main__.py"}

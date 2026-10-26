"""NP-A0h: las raíces de la instancia MO2 (instalación, datos, mods) se resuelven en UN lugar.

El agente LLM las recibe del ``MO2Controller`` que arma ``AppContext`` y la GUI (dispatcher) las pide al ``PathResolutionService``: si
cada una derivara ``data_root`` y ``mods_dir`` por su cuenta, el diagnóstico del perfil de parallax contestaría sobre árboles
distintos (la clase «dos superficies, un recurso» de AGENTS.md). Qué se verifica: la derivación sobre una instancia real en disco, que
falla cerrado sin instalación, y por AST que las dos superficies la piden a la misma función y que ``AppContext`` ya no la repite.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import patch

import pytest

from sky_claw.app.core.path_resolver import PathResolutionService, RaicesDeInstanciaMO2
from sky_claw.app.security.path_validator import PathValidator

_RAIZ_DEL_REPO = pathlib.Path(__file__).resolve().parent.parent


def _servicio(raiz: pathlib.Path) -> PathResolutionService:
    return PathResolutionService(path_validator=PathValidator(roots=[raiz]), mo2_install_dir=raiz)


def test_una_instancia_portable_tiene_instalacion_datos_y_mods_en_la_misma_raiz(tmp_path: pathlib.Path) -> None:
    (tmp_path / "mods").mkdir()
    with patch.dict("os.environ", {}, clear=True):
        raices = _servicio(tmp_path).raices_de_instancia_mo2()

    assert raices == RaicesDeInstanciaMO2(
        install_root=tmp_path.resolve(), data_root=tmp_path.resolve(), mods_dir=(tmp_path / "mods").resolve()
    )


def test_el_respaldo_de_instalacion_se_usa_solo_si_el_resolver_no_conoce_ninguna(tmp_path: pathlib.Path) -> None:
    servicio = PathResolutionService(path_validator=PathValidator(roots=[tmp_path]))
    with patch.dict("os.environ", {}, clear=True):
        raices = servicio.raices_de_instancia_mo2(install_de_respaldo=tmp_path)

    assert raices.install_root == tmp_path
    assert raices.data_root == tmp_path
    assert raices.mods_dir == tmp_path / "mods"


def test_sin_ninguna_instalacion_falla_cerrado_con_un_motivo(tmp_path: pathlib.Path) -> None:
    servicio = PathResolutionService(path_validator=PathValidator(roots=[tmp_path]))
    with patch.dict("os.environ", {}, clear=True), pytest.raises(RuntimeError, match="MO2"):
        servicio.raices_de_instancia_mo2()


def _llamadores_de(nombre: str) -> set[str]:
    """Módulos que USAN el método *nombre*: lo llaman o lo pasan como callable (la GUI se lo entrega al dispatcher sin llamarlo)."""
    encontrados: set[str] = set()
    for archivo in (_RAIZ_DEL_REPO / "sky_claw").rglob("*.py"):
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.Attribute) and nodo.attr == nombre:
                encontrados.add(archivo.relative_to(_RAIZ_DEL_REPO).as_posix())
    return encontrados


def test_las_dos_superficies_piden_las_raices_a_la_misma_funcion() -> None:
    """Congelado por igualdad: una superficie nueva que las derive aparte rompe esto hasta que se declare acá."""
    assert _llamadores_de("raices_de_instancia_mo2") == {
        "sky_claw/app_context.py",
        "sky_claw/app/orchestrator/orchestration_composition.py",
    }


def test_app_context_no_repite_la_derivacion_de_las_raices() -> None:
    """La derivación vive en el resolver: ``AppContext`` no vuelve a combinar sus tres getters a mano."""
    llamadas = _llamadores_de("get_mo2_instance_data_root_estricto") | _llamadores_de("get_mo2_mods_path_para_destino")
    assert "sky_claw/app_context.py" not in llamadas

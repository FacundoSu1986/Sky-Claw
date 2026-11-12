"""Smoke REAL: Sky-Claw → ``XEditBridgeService`` → xEdit Bridge instalado → SSEEdit (ADR 0018).

Opt-in y NUNCA en CI: corre solo con ``SKYCLAW_SMOKE_XEDIT_BRIDGE=1`` y ``XEDIT_BRIDGE_ROOT`` apuntando a
la carpeta ``BridgeTrabajo`` de una instalación real. Usa el ``PathResolutionService`` real (sandbox =
``SKYCLAW_SMOKE_SANDBOX``, por defecto ``G:\\Modding``) con la instancia MO2 que descubre y el perfil de
``MO2_PROFILE``. Nada de mocks: es la única prueba de que el adaptador funciona contra el bridge real, y un
verde de los tests con el bridge falso no la reemplaza.

Los veredictos esperados son los que registró la validación del bridge 1.1.0 sobre los 39 plugins del
perfil ``prueva huevo 3`` (mismos veredictos y mismos bytes que la corrida 1.0.1). Además exige que la
instancia MO2 —raíz de datos y carpeta del INI— quede idéntica (tamaño y mtime de cada archivo).

Escribe solo en el laboratorio del bridge (``Laboratorio/Admitidos`` y sus evidencias).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sky_claw.app.core.path_resolver import PathResolutionService
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.tools.xedit_bridge_service import XEditBridgeService

pytestmark = pytest.mark.skipif(
    os.environ.get("SKYCLAW_SMOKE_XEDIT_BRIDGE") != "1" or not os.environ.get("XEDIT_BRIDGE_ROOT"),
    reason="smoke real opt-in: SKYCLAW_SMOKE_XEDIT_BRIDGE=1 y XEDIT_BRIDGE_ROOT de una instalación real",
)

#: Validación 1.1.0 del bridge, perfil «prueva huevo 3» (evidence/real-admission-110/audits.json).
ESPERADOS: dict[str, dict[str, object]] = {
    "RetreteVIP.esl": {"success": True, "aprobado": True, "clean": True, "check_complete": True, "errores_nativos": 0},
    "HDD_Spike.esp": {"success": True, "aprobado": False, "clean": False, "check_complete": True, "errores_nativos": 3},
    # Diagnóstico estructural del motor: el chequeo no es confiable y nunca puede aprobar.
    "ImperialArmoredMammoth.esp": {"success": False, "aprobado": False, "clean": False, "check_complete": False},
}
HALLAZGOS_ESPERADOS = {"RetreteVIP.esl": 0, "HDD_Spike.esp": 3}

#: El conftest aísla LOCALAPPDATA por test (fixture autouse). Este smoke necesita el host REAL —la
#: instancia global de MO2 vive ahí—, así que se captura al importar, antes de que corra el fixture.
_LOCALAPPDATA_REAL = os.environ.get("LOCALAPPDATA", "")


def _huella(*raices: Path) -> dict[str, tuple[bool, int, int]]:
    """``ruta -> (es_carpeta, tamaño, mtime_ns)`` de cada carpeta y archivo, sin seguir enlaces ni leer contenido.

    Las carpetas cuentan: una carpeta vacía creada (o borrada) dentro de la instancia no cambia ningún
    archivo, pero sí aparece como entrada nueva y mueve el mtime de su carpeta madre. ``os.walk`` no
    entra en enlaces a carpetas y ``lstat`` no los sigue.
    """
    huella: dict[str, tuple[bool, int, int]] = {}
    for raiz in raices:
        info = os.lstat(raiz)
        huella[str(raiz)] = (True, 0, info.st_mtime_ns)
        for carpeta, subcarpetas, archivos in os.walk(raiz):
            for nombre in (*subcarpetas, *archivos):
                ruta = os.path.join(carpeta, nombre)
                info = os.lstat(ruta)
                es_carpeta = nombre in subcarpetas
                huella[ruta] = (es_carpeta, 0 if es_carpeta else info.st_size, info.st_mtime_ns)
    return huella


@pytest.fixture(scope="module")
def resolver() -> PathResolutionService:
    sandbox = Path(os.environ.get("SKYCLAW_SMOKE_SANDBOX", "G:/Modding"))
    return PathResolutionService(path_validator=PathValidator(roots=[sandbox]))


@pytest.mark.timeout(1200)
@pytest.mark.parametrize("plugin", sorted(ESPERADOS))
async def test_admitir_y_auditar_un_plugin_real_coincide_con_el_bridge(
    resolver: PathResolutionService, plugin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", _LOCALAPPDATA_REAL)
    ini_dir, datos = resolver.get_mo2_instance_ini_dir(), resolver.get_mo2_instance_data_root()
    assert ini_dir is not None and datos is not None, "Sky-Claw no resolvió la instancia MO2 de esta máquina"
    antes = _huella(datos, ini_dir)
    servicio = XEditBridgeService(path_resolver=resolver)

    admision = await servicio.admitir(plugin)
    assert admision["success"] is True, admision["message"]
    assert plugin in admision["clausura"]

    resultado = await servicio.auditar(plugin, timeout=120)
    obtenido = {campo: resultado.get(campo) for campo in ESPERADOS[plugin]}
    assert obtenido == ESPERADOS[plugin], resultado
    if plugin in HALLAZGOS_ESPERADOS:
        assert len(resultado["hallazgos"]) == HALLAZGOS_ESPERADOS[plugin], resultado
    assert resultado["bridge_version"].startswith("1."), resultado

    despues = _huella(datos, ini_dir)
    cambiados = sorted(ruta for ruta in antes.keys() | despues.keys() if antes.get(ruta) != despues.get(ruta))
    assert not cambiados, f"la instancia MO2 cambió (debía quedar intacta): {cambiados[:10]}"


@pytest.mark.timeout(600)
async def test_auditar_un_plugin_no_admitido_lo_dice_y_sugiere_admitir(resolver: PathResolutionService) -> None:
    # Solo lectura: el bridge real responde isError=true con untrusted_data vacío y el motivo en ``errors``.
    resultado = await XEditBridgeService(path_resolver=resolver).auditar("NoExisteSkyClaw.esp", timeout=30)
    assert (resultado["success"], resultado["aprobado"], resultado["no_admitido"]) == (False, False, True), resultado
    assert "admitir_plugin_nativo" in resultado["message"]

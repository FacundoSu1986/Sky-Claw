"""P-1 (auditoría de la etapa 9): presupuesto de disco ANTES de lanzar TexGen/DynDOLOD.

El defecto: el espacio libre sólo se medía al empaquetar (``_package_output_as_mod``),
o sea DESPUÉS de 30+ minutos de generación. Con el disco justo, la corrida moría al
final y el trabajo se perdía. Y ``-t:`` cae a ``%TEMP%`` (normalmente ``C:``), donde
TexGen/DynDOLOD escriben temporales de varios GB.

El sensor mide los volúmenes que la corrida va a escribir —work root, temporales y
``mods/``— y suma lo requerido cuando dos roles comparten volumen (el staging y el
mod empaquetado coexisten durante el packaging, así que no se pueden contar por separado).
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable

import pytest

from sky_claw.local.validators.dyndolod_disk_budget import (
    DYNDOLOD_MIN_FREE_BYTES_POR_ROL,
    DiskBudgetChecker,
    DiskVolume,
    disk_budget_preflight_check,
)
from sky_claw.local.validators.preflight import PreflightService, PreflightStatus

_GIB = 1024**3
_RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _libre(valores: dict[pathlib.Path, int]) -> Callable[[pathlib.Path], int]:
    def _fn(ruta: pathlib.Path) -> int:
        return valores[ruta]

    return _fn


def test_volumen_con_espacio_suficiente_es_verde(tmp_path: pathlib.Path) -> None:
    scan = DiskBudgetChecker(
        volumes=[DiskVolume("work_root", tmp_path, 10 * _GIB)],
        free_bytes=lambda _p: 50 * _GIB,
    ).check()
    check = disk_budget_preflight_check(scan)
    assert check.name == "disk_budget"
    assert check.status is PreflightStatus.GREEN
    assert not scan.issues


def test_volumen_sin_espacio_es_rojo_y_nombra_el_rol(tmp_path: pathlib.Path) -> None:
    scan = DiskBudgetChecker(
        volumes=[DiskVolume("temp", tmp_path, 10 * _GIB)],
        free_bytes=lambda _p: 2 * _GIB,
    ).check()
    check = disk_budget_preflight_check(scan)
    assert check.status is PreflightStatus.RED
    assert any("temp" in d for d in check.details)
    assert scan.issues[0].kind == "insufficient"


def test_dos_roles_en_el_mismo_volumen_suman_lo_requerido(tmp_path: pathlib.Path) -> None:
    """El staging y el mod empaquetado coexisten: 15 GiB libres no alcanzan para 10 + 10."""
    otra = tmp_path / "mods"
    otra.mkdir()
    scan = DiskBudgetChecker(
        volumes=[
            DiskVolume("work_root", tmp_path, 10 * _GIB),
            DiskVolume("mods", otra, 10 * _GIB),
        ],
        free_bytes=lambda _p: 15 * _GIB,
    ).check()
    assert disk_budget_preflight_check(scan).status is PreflightStatus.RED
    assert len(scan.issues) == 1, "un volumen compartido se reporta UNA vez, no una por rol"
    assert "work_root" in scan.issues[0].label and "mods" in scan.issues[0].label


def test_ruta_inexistente_se_mide_en_su_ancestro_existente(tmp_path: pathlib.Path) -> None:
    """El work root puede no existir en la primera corrida: se mide el ancestro que sí existe."""
    futura = tmp_path / "no" / "existe" / "todavia"
    medidas: list[pathlib.Path] = []

    def _fn(ruta: pathlib.Path) -> int:
        medidas.append(ruta)
        return 50 * _GIB

    DiskBudgetChecker(volumes=[DiskVolume("work_root", futura, _GIB)], free_bytes=_fn).check()
    assert medidas == [tmp_path]


def test_no_poder_medir_es_amarillo_no_verde(tmp_path: pathlib.Path) -> None:
    """Honestidad: si la medición falla no se afirma un verde, pero tampoco se bloquea sin evidencia."""

    def _roto(_p: pathlib.Path) -> int:
        raise OSError("volumen desmontado")

    scan = DiskBudgetChecker(volumes=[DiskVolume("temp", tmp_path, _GIB)], free_bytes=_roto).check()
    check = disk_budget_preflight_check(scan)
    assert check.status is PreflightStatus.YELLOW
    assert scan.issues[0].kind == "unmeasurable"


def test_sin_volumenes_no_finge_una_medicion() -> None:
    check = disk_budget_preflight_check(DiskBudgetChecker(volumes=[], free_bytes=lambda _p: 0).check())
    assert check.status is PreflightStatus.GREEN
    assert "sin volúmenes" in check.summary.lower()


def test_el_umbral_por_rol_es_positivo_y_cubre_los_tres_roles() -> None:
    assert set(DYNDOLOD_MIN_FREE_BYTES_POR_ROL) == {"work_root", "temp", "mods"}
    assert all(v > 0 for v in DYNDOLOD_MIN_FREE_BYTES_POR_ROL.values())


async def test_el_rojo_de_disco_bloquea_el_preflight(tmp_path: pathlib.Path) -> None:
    scan_fn = DiskBudgetChecker(
        volumes=[DiskVolume("temp", tmp_path, 10 * _GIB)],
        free_bytes=lambda _p: _GIB,
    ).check
    servicio = PreflightService(disk_check=scan_fn, omit_unconfigured=True)
    reporte = await servicio.run()
    assert reporte.blocks_mutations is True
    assert [c.name for c in reporte.checks] == ["disk_budget"]


async def test_sin_sensor_el_preflight_lo_omite() -> None:
    reporte = await PreflightService(omit_unconfigured=True).run()
    assert "disk_budget" not in [c.name for c in reporte.checks]


def test_el_servicio_dyndolod_cablea_el_sensor_de_disco_con_los_tres_roles() -> None:
    """Ancla por AST: ``_ensure_preflight`` pasa ``disk_check`` y los tres roles están declarados.

    Es el hermano que suele quedar sin cablear: un sensor que existe pero que ningún
    constructor productivo recibe no protege nada.
    """
    fuente = (_RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_service.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    pasa_disk_check = False
    roles: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and getattr(nodo.func, "id", None) == "PreflightService":
            pasa_disk_check = any(kw.arg == "disk_check" for kw in nodo.keywords)
        es_volumen = isinstance(nodo, ast.Call) and getattr(nodo.func, "id", None) == "DiskVolume"
        if es_volumen and nodo.args and isinstance(nodo.args[0], ast.Constant):
            roles.add(str(nodo.args[0].value))
    assert pasa_disk_check, "DynDOLODPipelineService no cablea disk_check al PreflightService"
    assert roles == {"work_root", "temp", "mods"}


@pytest.mark.parametrize("otro", ["loot_service", "xedit_service", "synthesis_service", "pandora_service"])
def test_los_otros_servicios_no_declaran_el_sensor_de_la_etapa_9(otro: str) -> None:
    """El umbral es de TexGen/DynDOLOD (GBs de LODs): no se le aplica a servicios que no lo generan."""
    fuente = (_RAIZ / "sky_claw" / "local" / "tools" / f"{otro}.py").read_text(encoding="utf-8")
    assert "dyndolod_disk_budget" not in fuente

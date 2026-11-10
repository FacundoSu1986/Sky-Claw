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
from sky_claw.local.validators.dyndolod_disk_budget import _disk_usage_free as _medicion_real
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
            pasa_disk_check = pasa_disk_check or any(kw.arg == "disk_check" for kw in nodo.keywords)
        es_volumen = isinstance(nodo, ast.Call) and getattr(nodo.func, "id", None) == "DiskVolume"
        if es_volumen and nodo.args and isinstance(nodo.args[0], ast.Constant):
            roles.add(str(nodo.args[0].value))
    assert pasa_disk_check, "DynDOLODPipelineService no cablea disk_check al PreflightService"
    assert roles == {"work_root", "temp", "mods"}


@pytest.mark.parametrize(
    "otro", ["loot_service", "xedit_service", "synthesis_service", "pandora_service", "wrye_bash_service"]
)
def test_los_otros_servicios_no_declaran_el_sensor_de_la_etapa_9(otro: str) -> None:
    """El umbral es de TexGen/DynDOLOD (GBs de LODs): no se le aplica a servicios que no lo generan."""
    fuente = (_RAIZ / "sky_claw" / "local" / "tools" / f"{otro}.py").read_text(encoding="utf-8")
    assert "dyndolod_disk_budget" not in fuente


def test_el_volumen_de_temporales_es_el_que_usa_la_config() -> None:
    """El rol ``temp`` mide ``tempfile.gettempdir()`` porque la config de la corrida cae ahí.

    Si el servicio empezara a pasar ``temp_dir`` a ``DynDOLODConfig`` el sensor mediría el volumen
    equivocado y daría un verde sobre un disco que no es el de los temporales. El test rompe en
    ese momento y obliga a derivar el rol de la misma fuente.
    """
    fuente = (_RAIZ / "sky_claw" / "local" / "tools" / "dyndolod_service.py").read_text(encoding="utf-8")
    llamadas = [
        n
        for n in ast.walk(ast.parse(fuente))
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "DynDOLODConfig"
    ]
    assert llamadas, "el servicio dejó de construir DynDOLODConfig: revisar de dónde sale `-t:`"
    assert all(kw.arg != "temp_dir" for c in llamadas for kw in c.keywords), (
        "el servicio ahora cablea temp_dir: el rol `temp` del sensor de disco debe seguirlo"
    )


def test_la_medicion_real_funciona_aunque_la_suite_la_enmascare(tmp_path: pathlib.Path) -> None:
    """El fixture autouse de ``conftest.py`` fija ``_disk_usage_free`` para que la suite no dependa del host.

    Este test usa la referencia ORIGINAL (importada antes de que el fixture la reemplace) para probar que la
    medición por defecto sí habla con ``shutil.disk_usage`` y que el sensor la consume de punta a punta:
    el enmascaramiento global no deja la ruta real sin ejercitar.
    """
    assert _medicion_real(tmp_path) > 0
    scan = DiskBudgetChecker(volumes=[DiskVolume("temp", tmp_path, 1)], free_bytes=_medicion_real).check()
    assert not scan.issues
    assert scan.measured and "temp" in scan.measured[0]


def test_identidad_de_volumen_desconocida_no_da_verde(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Si no se puede saber qué roles comparten volumen, no se afirma un verde.

    Sin identidad, tratar cada ruta como un volumen distinto da verde con 15 GiB libres para dos roles que
    necesitan 10 + 10 si en realidad son el mismo disco. Debe quedar amarillo ("no se pudo medir"); y si
    algún volumen SÍ está corto, sigue siendo rojo.
    """
    monkeypatch.setattr("sky_claw.local.validators.dyndolod_disk_budget._identidad_de_volumen", lambda _p: None)
    otra = tmp_path / "mods"
    otra.mkdir()
    volumenes = [DiskVolume("work_root", tmp_path, 10 * _GIB), DiskVolume("mods", otra, 10 * _GIB)]

    amarillo = DiskBudgetChecker(volumes=volumenes, free_bytes=lambda _p: 15 * _GIB).check()
    check = disk_budget_preflight_check(amarillo)
    assert check.status is PreflightStatus.YELLOW
    assert all(i.kind == "unmeasurable" for i in amarillo.issues)

    rojo = DiskBudgetChecker(volumes=volumenes, free_bytes=lambda _p: 5 * _GIB).check()
    assert disk_budget_preflight_check(rojo).status is PreflightStatus.RED


def _roles_del_preflight_real(tmp_path: pathlib.Path, *, mods_resoluble: bool) -> set[str]:
    """Construye ``DynDOLODPipelineService`` de verdad y devuelve los roles que su sensor de disco mide."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
    from tests.test_vfs_visibility_wiring import _instancia, _resolver

    skyrim, mo2 = _instancia(tmp_path)
    resolver = _resolver(skyrim=skyrim, mo2=mo2)
    resolver.get_mo2_mods_path_best_effort = MagicMock(return_value=(mo2 / "mods") if mods_resoluble else None)
    servicio = DynDOLODPipelineService(
        lock_manager=MagicMock(),
        snapshot_manager=MagicMock(),
        path_resolver=resolver,
        journal=MagicMock(),
        event_bus=MagicMock(),
        workspace=SimpleNamespace(root=tmp_path / "work_root_externo"),
    )
    preflight = servicio._ensure_preflight()
    assert preflight is not None
    checker = preflight._disk_check.__self__  # type: ignore[union-attr]
    return {v.label for v in checker._volumes}


def test_el_servicio_real_mide_los_tres_roles_cuando_todo_es_resoluble(tmp_path: pathlib.Path) -> None:
    """Complementa al ancla AST: lo que ella no ve es qué roles quedan REALMENTE en el sensor en runtime."""
    assert _roles_del_preflight_real(tmp_path, mods_resoluble=True) == {"work_root", "temp", "mods"}


def test_sin_mods_resoluble_el_sensor_omite_ese_rol_y_no_inventa_una_ruta(tmp_path: pathlib.Path) -> None:
    """Un ``mods_dir`` no resoluble omite el rol ``mods`` (no se mide lo que no se sabe); work_root y temp siguen."""
    assert _roles_del_preflight_real(tmp_path, mods_resoluble=False) == {"work_root", "temp"}

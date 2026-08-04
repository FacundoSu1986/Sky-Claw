"""Tests del sensor de orden de masters (hueco V-7 de la auditoría del snippet headless).

Un master presente y habilitado igual produce CTD si carga **después** del
plugin que lo declara. `MissingMastersChecker` responde "¿está?"; este sensor
responde "¿está antes?", que es el eje que faltaba: ningún validador del repo lo
cubría, y el módulo "Wrye Bash headless" auditado devolvía `SUCCESS` sobre un
load order con el master invertido.

El detalle que hace correcto al sensor —y que una comparación ingenua de
índices erra— es que el motor de Skyrim **hoistea el bloque de masters**: todo
plugin con flag ESM (o extensión `.esm`/`.esl`) carga antes que cualquier `.esp`
plano, sin importar su posición en `plugins.txt`. Comparar índices crudos
generaría falsos positivos masivos en cualquier setup real.

Las fixtures construyen plugins sintéticos con el layout real de Skyrim SE:
record TES4 (header de 24 bytes) + subrecords HEDR/MAST/DATA.
"""

from __future__ import annotations

import pathlib
import struct

import pytest

from sky_claw.local.validators.master_order import (
    MasterOrderChecker,
    order_preflight_check,
)
from sky_claw.local.validators.preflight import PreflightStatus

#: Flags del record TES4 (mismos valores que `plugin_header`).
_FLAG_MASTER = 0x00000001
_FLAG_LIGHT = 0x00000200


def _tes4_plugin(
    path: pathlib.Path,
    masters: list[str],
    *,
    flags: int = 0,
) -> pathlib.Path:
    """Escribe un plugin sintético con header TES4 válido, masters y flags."""
    subrecords = b""
    hedr = struct.pack("<fiI", 1.7, 0, 0x800)
    subrecords += b"HEDR" + struct.pack("<H", len(hedr)) + hedr
    for master in masters:
        data = master.encode("cp1252") + b"\x00"
        subrecords += b"MAST" + struct.pack("<H", len(data)) + data
        subrecords += b"DATA" + struct.pack("<H", 8) + struct.pack("<Q", 0)
    header = b"TES4" + struct.pack("<IIIIHH", len(subrecords), flags, 0, 0, 44, 0)
    path.write_bytes(header + subrecords)
    return path


# ---------------------------------------------------------------------------
# El caso que motivó el sensor: master después de su dependiente
# ---------------------------------------------------------------------------


def test_orden_correcto_no_reporta_nada(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Base.esp", [])
    _tes4_plugin(data / "Parche.esp", ["Base.esp"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Base.esp", "Parche.esp"])

    assert issues == []


def test_master_despues_del_dependiente_es_critical(tmp_path: pathlib.Path) -> None:
    """El caso exacto que el snippet auditado reportaba como SUCCESS."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Base.esp", [])
    _tes4_plugin(data / "Parche.esp", ["Base.esp"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Parche.esp", "Base.esp"])

    assert len(issues) == 1
    assert issues[0].kind == "master_after_dependent"
    assert issues[0].severity == "critical"
    assert issues[0].plugin == "Parche.esp"
    assert issues[0].master == "Base.esp"
    assert issues[0].plugin_index < issues[0].master_index
    assert "antes" in issues[0].remediation.lower()  # remediación accionable


def test_reporta_cada_master_invertido_por_separado(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "A.esp", [])
    _tes4_plugin(data / "B.esp", [])
    _tes4_plugin(data / "Parche.esp", ["A.esp", "B.esp"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Parche.esp", "A.esp", "B.esp"])

    assert {i.master for i in issues} == {"A.esp", "B.esp"}


# ---------------------------------------------------------------------------
# Hoisting del bloque de masters: donde una comparación ingenua se rompe
# ---------------------------------------------------------------------------


def test_esm_listado_despues_no_es_problema(tmp_path: pathlib.Path) -> None:
    """El motor carga TODOS los masters primero: un .esm más abajo en
    plugins.txt igual carga antes que cualquier .esp. Comparar índices crudos
    daría un falso positivo acá."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Base.esm", [], flags=_FLAG_MASTER)
    _tes4_plugin(data / "Parche.esp", ["Base.esm"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Parche.esp", "Base.esm"])

    assert issues == []


def test_esp_con_flag_esm_tambien_se_hoistea(tmp_path: pathlib.Path) -> None:
    """El flag manda sobre la extensión: un .esp con flag ESM entra al bloque
    de masters igual que un .esm."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "BaseFlagged.esp", [], flags=_FLAG_MASTER)
    _tes4_plugin(data / "Parche.esp", ["BaseFlagged.esp"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Parche.esp", "BaseFlagged.esp"])

    assert issues == []


def test_esl_se_hoistea_aunque_no_tenga_flag_esm(tmp_path: pathlib.Path) -> None:
    """Los .esl cargan en el bloque de masters (pool FE) por extensión."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Ligero.esl", [], flags=_FLAG_LIGHT)
    _tes4_plugin(data / "Parche.esp", ["Ligero.esl"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Parche.esp", "Ligero.esl"])

    assert issues == []


def test_dentro_del_bloque_de_masters_el_orden_sigue_importando(tmp_path: pathlib.Path) -> None:
    """El hoisting no exime al bloque: entre dos .esm el orden relativo manda."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Skyrim.esm", [], flags=_FLAG_MASTER)
    _tes4_plugin(data / "Dawnguard.esm", ["Skyrim.esm"], flags=_FLAG_MASTER)

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Dawnguard.esm", "Skyrim.esm"])

    assert len(issues) == 1
    assert issues[0].plugin == "Dawnguard.esm"
    assert issues[0].master == "Skyrim.esm"


def test_un_esp_nunca_puede_ser_master_de_un_esm_sin_issue(tmp_path: pathlib.Path) -> None:
    """Un .esm que declara master a un .esp plano: el .esp carga DESPUÉS del
    bloque de masters, así que siempre es una inversión."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Suelto.esp", [])
    _tes4_plugin(data / "Raro.esm", ["Suelto.esp"], flags=_FLAG_MASTER)

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Suelto.esp", "Raro.esm"])

    assert len(issues) == 1
    assert issues[0].plugin == "Raro.esm"
    assert issues[0].master == "Suelto.esp"


# ---------------------------------------------------------------------------
# Separación de responsabilidades con MissingMastersChecker
# ---------------------------------------------------------------------------


def test_master_ausente_del_load_order_no_se_reporta_aca(tmp_path: pathlib.Path) -> None:
    """ "No está" es de `missing_masters`; este sensor solo juzga posición.
    Duplicar el issue haría que el semáforo cuente dos veces el mismo problema."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Parche.esp", ["NoInstalado.esm"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Parche.esp"])

    assert issues == []


def test_plugin_ilegible_no_se_reporta_aca(tmp_path: pathlib.Path) -> None:
    """`missing_masters` ya emite el issue `unreadable`."""
    data = tmp_path / "Data"
    data.mkdir()
    (data / "Corrupto.esp").write_bytes(b"garbage")

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Corrupto.esp"])

    assert issues == []


def test_plugin_del_load_order_ausente_en_disco_no_rompe(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Base.esp", [])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Fantasma.esp", "Base.esp"])

    assert issues == []


# ---------------------------------------------------------------------------
# Robustez del matching (los bugs V-3 y V-2 del snippet auditado)
# ---------------------------------------------------------------------------


def test_matching_case_insensitive_como_windows(tmp_path: pathlib.Path) -> None:
    """El snippet auditado comparaba con `in set(...)` sensible a mayúsculas y
    producía falsos 'master faltante'."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Base.esp", [])
    _tes4_plugin(data / "Parche.esp", ["BASE.ESP"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["parche.esp", "Base.esp"])

    assert len(issues) == 1
    assert issues[0].master == "BASE.ESP"  # se conserva el nombre tal como lo declara el header


def test_busca_en_multiples_directorios(tmp_path: pathlib.Path) -> None:
    """Fuera del VFS de MO2 los plugins están repartidos entre mods."""
    data = tmp_path / "Data"
    mod_a = tmp_path / "mods" / "ModA"
    data.mkdir()
    mod_a.mkdir(parents=True)
    _tes4_plugin(data / "Base.esp", [])
    _tes4_plugin(mod_a / "ModA.esp", ["Base.esp"])

    issues = MasterOrderChecker(plugin_dirs=[data, mod_a]).check(["ModA.esp", "Base.esp"])

    assert len(issues) == 1
    assert issues[0].plugin == "ModA.esp"


def test_nombres_repetidos_en_el_load_order_usan_la_primera_posicion(tmp_path: pathlib.Path) -> None:
    """Un plugins.txt malformado con repetidos no debe inventar inversiones."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Base.esp", [])
    _tes4_plugin(data / "Parche.esp", ["Base.esp"])

    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Base.esp", "Parche.esp", "Base.esp"])

    assert issues == []


def test_load_order_vacio_no_rompe(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "Data"
    data.mkdir()

    assert MasterOrderChecker(plugin_dirs=[data]).check([]) == []


def test_directorio_inaccesible_no_tumba_preflight(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un fallo de IO al listar directorios degrada a best-effort."""
    data = tmp_path / "Data"
    data.mkdir()
    original_iterdir = pathlib.Path.iterdir

    def fail_for_data(self: pathlib.Path):
        if self == data:
            raise OSError("permiso denegado")
        return original_iterdir(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", fail_for_data)
    caplog.set_level("DEBUG", logger="sky_claw.local.validators.master_order")

    assert MasterOrderChecker(plugin_dirs=[data]).check(["Parche.esp"]) == []
    assert "permiso denegado" in caplog.text


# ---------------------------------------------------------------------------
# Puente al semáforo del preflight
# ---------------------------------------------------------------------------


def test_check_verde_sin_issues() -> None:
    check = order_preflight_check([])

    assert check.name == "master_order"
    assert check.status is PreflightStatus.GREEN


def test_check_rojo_con_inversion(tmp_path: pathlib.Path) -> None:
    """Una inversión es CTD garantizado: bloquea, no advierte."""
    data = tmp_path / "Data"
    data.mkdir()
    _tes4_plugin(data / "Base.esp", [])
    _tes4_plugin(data / "Parche.esp", ["Base.esp"])
    issues = MasterOrderChecker(plugin_dirs=[data]).check(["Parche.esp", "Base.esp"])

    check = order_preflight_check(issues)

    assert check.status is PreflightStatus.RED
    assert any("Base.esp" in d for d in check.details)


# ---------------------------------------------------------------------------
# Ancla del cableado: qué servicios montan el sensor, y cuál NO (y por qué)
# ---------------------------------------------------------------------------

_TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1] / "sky_claw" / "local" / "tools"

#: Servicios que consumen un load order YA estabilizado: una inversión de
#: masters invalida su salida, así que el sensor bloquea antes de trabajar.
SERVICIOS_CON_ORDEN: frozenset[str] = frozenset(
    {
        "wrye_bash_service",  # stage 6 — merge de Leveled Lists
        "synthesis_service",  # stage 7 — mutators sobre el load order
        "dyndolod_service",  # stage 9 — 30+ min de LODs
    }
)

#: Exclusiones deliberadas, con el motivo en el propio dict (no en un comentario
#: suelto): sacar o agregar un servicio acá tiene que ser una decisión.
SERVICIOS_SIN_ORDEN: dict[str, str] = {
    "loot_service": (
        "LOOT es la REMEDIACIÓN de un orden inválido: bloquearlo por orden "
        "inválido dejaría al usuario sin forma de arreglarlo (deadlock). La "
        "remediación de OrderIssue dice literalmente 'corré LOOT'."
    ),
}


def _servicios_con_sensores_de_modlist() -> set[str]:
    """Servicios que cablean la familia de sensores de modlist al preflight.

    Se detecta por el uso de ``masters_check=``: pasar ese sensor es, en los
    hechos, declarar "acá leo el load order del perfil". Todo servicio de esa
    familia tiene que estar clasificado como con-orden o sin-orden.
    """
    return {
        ruta.stem for ruta in _TOOLS_DIR.glob("*_service.py") if "masters_check=" in ruta.read_text(encoding="utf-8")
    }


def test_la_enumeracion_cubre_toda_la_familia() -> None:
    """Un servicio nuevo que lea el modlist rompe acá hasta clasificarlo."""
    assert _servicios_con_sensores_de_modlist() == SERVICIOS_CON_ORDEN | set(SERVICIOS_SIN_ORDEN), (
        "Cambió el conjunto de servicios que cablean sensores de modlist al "
        "preflight. Clasificá el nuevo en SERVICIOS_CON_ORDEN (consume un load "
        "order estabilizado) o en SERVICIOS_SIN_ORDEN con el motivo escrito."
    )


@pytest.mark.parametrize("servicio", sorted(SERVICIOS_CON_ORDEN))
def test_los_consumidores_montan_el_sensor_de_orden(servicio: str) -> None:
    """El fix tiene que aterrizar en TODOS los hermanos, no en uno."""
    fuente = (_TOOLS_DIR / f"{servicio}.py").read_text(encoding="utf-8")

    assert "order_check=" in fuente, f"{servicio} no pasa order_check a PreflightService"
    assert "build_master_order_sensor" in fuente, f"{servicio} no construye el sensor de orden"


@pytest.mark.parametrize("servicio", sorted(SERVICIOS_SIN_ORDEN))
def test_los_excluidos_siguen_sin_el_sensor(servicio: str) -> None:
    """La exclusión de LOOT es deliberada: si alguien la revierte, rompe acá."""
    fuente = (_TOOLS_DIR / f"{servicio}.py").read_text(encoding="utf-8")

    assert "order_check=" not in fuente, (
        f"{servicio} montó el sensor de orden, pero está excluido a propósito: {SERVICIOS_SIN_ORDEN[servicio]}"
    )

"""Tests for :class:`EnvironmentScanner` honouring manually-configured paths.

Follow-up #2 from PR #209: on setups where the user pinned ``skyrim_path`` /
``loot_exe`` / ``xedit_exe`` in the config, the scanner must

1. prefer the configured ``skyrim_path`` over auto-detection, and
2. NOT early-return before seeding tools when Skyrim isn't auto-detected — it
   must seed ``snap.tools[key]`` from the configured executables instead of
   sending them all to ``snap.missing`` (which made installed tools show up as
   "No instalado" in the Rituales).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from sky_claw.local.discovery import scanner as scanner_module
from sky_claw.local.discovery.scanner import (
    _DYNDOLOD_NAMES,
    _LOOT_NAMES,
    _PANDORA_NAMES,
    _WRYE_BASH_NAMES,
    _XEDIT_NAMES,
    EnvironmentScanner,
    HealthStatus,
    _read_pe_product_version,
)


def _touch_exe(directory: Path, name: str) -> Path:
    """Create a stub executable; only its existence/size are probed by the scan."""
    exe = directory / name
    exe.write_bytes(b"MZ")
    return exe


#: Herramientas que resuelve el resolvedor genérico (`_resolve_tool_path`) en
#: `_scan_inner`. skse y community_shaders usan detección dedicada y NO van acá.
#: Ancla por enumeración explícita (regla del repo: enumerar, no muestrear) con #: enforcement en test_generic_tools_ancladas_al_resolvedor: ese test pregunta al #: `_scan_inner` real qué claves pasan por `_resolve_tool_path`, así que agregar una #: tool genérica al scanner sin cablearla acá rompe el módulo.
#: Tuplas: (key del scanner, nombres del ejecutable, technical_name del MissingTool).
_GENERIC_TOOLS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("loot", _LOOT_NAMES, "LOOT"),
    ("xedit", _XEDIT_NAMES, "SSEEdit"),
    ("pandora", _PANDORA_NAMES, "Pandora Behaviour Engine+"),
    ("wrye_bash", _WRYE_BASH_NAMES, "Wrye Bash"),
    ("dyndolod", _DYNDOLOD_NAMES, "DynDOLOD64"),
)


@pytest.fixture(autouse=True)
def _aislar_autodeteccion_del_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla el módulo del host real (Skyrim, LOOT y MO2 instalados).

    El escaneo completo barre registry, Steam, rutas comunes y PATH; en una
    máquina con un setup de modding real la autodetección encuentra Skyrim y
    herramientas, y los tests que asumen un host vacío fallan de forma
    dependiente del entorno (ver ``test_missing_configured_tool_path_falls_back_to_missing``).

    Skyrim se neutraliza por FUENTES externas (_reg_read, _parse_steam_libraries,
    SKYRIM_COMMON_PATHS), no por método: la precedencia del ``skyrim_path``
    configurado es parte del contrato bajo test y debe seguir verificándose
    contra el ``_find_skyrim`` real — mockear el método entero escondería un
    fallo en esa precedencia. MO2 y tools no son objeto de este módulo: sus
    detectores se neutralizan por método.
    """
    # Skyrim: solo fuentes externas — el método real sigue ejecutándose.
    monkeypatch.setattr(scanner_module, "_reg_read", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner_module, "_parse_steam_libraries", lambda: [])
    monkeypatch.setattr(scanner_module, "SKYRIM_COMMON_PATHS", ())

    async def _find_mo2(self):
        return None

    monkeypatch.setattr(EnvironmentScanner, "_find_mo2", _find_mo2)
    monkeypatch.setattr(EnvironmentScanner, "_find_tool", lambda self, exe_names, search_roots: None)


async def test_configured_tool_path_seeds_tools_without_skyrim(tmp_path: Path) -> None:
    # La fixture autouse aísla la autodetección del host: sin config, cada tool
    # caería en ``missing``. Un loot_exe configurado debe seedear snap.tools["loot"].
    loot_exe = _touch_exe(tmp_path, "loot.exe")
    scanner = EnvironmentScanner(tool_paths={"loot": str(loot_exe)})

    snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.tools["loot"].exe_path == loot_exe
    assert not any(m.technical_name.lower() == "loot" for m in snap.missing)


async def test_configured_skyrim_path_is_respected(tmp_path: Path) -> None:
    skyrim_dir = tmp_path / "Skyrim Special Edition"
    skyrim_dir.mkdir()
    _touch_exe(skyrim_dir, "SkyrimSE.exe")
    scanner = EnvironmentScanner(skyrim_path=str(skyrim_dir))

    snap = await scanner.scan()

    assert snap.skyrim is not None
    assert snap.skyrim.path == skyrim_dir


async def test_multiple_configured_tool_paths_seeded(tmp_path: Path) -> None:
    loot_exe = _touch_exe(tmp_path, "loot.exe")
    xedit_exe = _touch_exe(tmp_path, "SSEEdit.exe")
    scanner = EnvironmentScanner(tool_paths={"loot": str(loot_exe), "xedit": str(xedit_exe)})

    snap = await scanner.scan()

    assert snap.has_tool("loot")
    assert snap.has_tool("xedit")
    assert snap.tools["xedit"].exe_path == xedit_exe


@pytest.mark.parametrize(("key", "names", "technical"), _GENERIC_TOOLS)
async def test_missing_configured_tool_path_falls_back_to_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    names: tuple[str, ...],
    technical: str,
) -> None:
    # Un path configurado que no existe no debe crashear ni reportarse como
    # instalado — el tool cae a la autodetección (aíslada por la fixture, que
    # simula un host vacío) → missing.
    find_tool = Mock(return_value=None)
    monkeypatch.setattr(EnvironmentScanner, "_find_tool", find_tool)
    scanner = EnvironmentScanner(tool_paths={key: str(tmp_path / "nope" / names[0])})

    snap = await scanner.scan()

    assert not snap.has_tool(key)
    assert any(m.technical_name == technical for m in snap.missing)
    # Ancla el mecanismo: el path configurado inválido NO bloquea el fallback —
    # _resolve_tool_path realmente alcanzó a _find_tool para esta tool.
    # `_find_tool` es síncrono en producción (scanner.py) y se invoca por
    # posición; la aserción tolera kwargs para no depender del estilo de la
    # llamada futura.
    assert any(
        (call.args and call.args[0] == names) or call.kwargs.get("exe_names") == names
        for call in find_tool.call_args_list
    )


@pytest.mark.parametrize(("key", "names", "_technical"), _GENERIC_TOOLS)
async def test_configured_tool_path_preferred_over_autodeteccion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    names: tuple[str, ...],
    _technical: str,
) -> None:
    # Hermano del anterior: un path configurado VÁLIDO gana aunque la
    # autodetección (señuelo válido aquí) encuentre otra copia del tool.
    loot_exe = _touch_exe(tmp_path, names[0])
    decoy_dir = tmp_path / "autodetectado"
    decoy_dir.mkdir()
    decoy = _touch_exe(decoy_dir, names[0])
    monkeypatch.setattr(
        EnvironmentScanner,
        "_find_tool",
        lambda self, exe_names, search_roots: decoy if exe_names == names else None,
    )
    scanner = EnvironmentScanner(tool_paths={key: str(loot_exe)})

    snap = await scanner.scan()

    assert snap.has_tool(key)
    assert snap.tools[key].exe_path == loot_exe


async def test_generic_tools_ancladas_al_resolvedor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ancla de enforcement: `_GENERIC_TOOLS` solo alimenta el parametrize, así que
    # este test la ata al registro REAL preguntándole a `_scan_inner` qué claves
    # pasan por `_resolve_tool_path` (skse y community_shaders usan detección
    # dedicada y NO pasan por acá). Si se agrega una tool genérica al scanner sin
    # cablearla a `_GENERIC_TOOLS`, este test rompe. `tool_defs` es un literal local
    # (sin registro exportado, a diferencia de `RITUAL_TOOL_MAP`), así que la
    # introspección es por ejecución y no por import.
    claves: list[str] = []
    resolver_real = EnvironmentScanner._resolve_tool_path

    def espiar(self, key, exe_names, search_roots):
        claves.append(key)
        return resolver_real(self, key, exe_names, search_roots)

    monkeypatch.setattr(EnvironmentScanner, "_resolve_tool_path", espiar)
    scanner = EnvironmentScanner(tool_paths={"loot": str(tmp_path / "nope" / "loot.exe")})

    await scanner.scan()

    assert set(claves) == {key for key, _, _ in _GENERIC_TOOLS}


def test_read_pe_product_version_devuelve_none_con_pe_corrupto(tmp_path: Path) -> None:
    # Ancla del contrato del helper (limpieza post-#275): el stub "MZ" de 2 bytes
    # dispara pefile.PEFormatError si pefile está instalada (hereda de Exception
    # a secas, no de OSError/ValueError), o bien el helper retorna None antes
    # por ImportError si pefile no está disponible en el entorno. En ambos casos
    # el helper debe devolver None — señal de "usar heurística de tamaño" —
    # sin propagar jamás la excepción.
    exe = _touch_exe(tmp_path, "SkyrimSE.exe")

    assert _read_pe_product_version(exe) is None


async def test_bare_scanner_without_skyrim_stays_critical(tmp_path: Path, monkeypatch) -> None:
    # Regression guard: with no config and no Skyrim, behaviour is unchanged —
    # the scan still reports the game as not found.
    async def mock_find_skyrim(*args, **kwargs):
        return None

    monkeypatch.setattr(EnvironmentScanner, "_find_skyrim", mock_find_skyrim)
    scanner = EnvironmentScanner()

    snap = await scanner.scan()

    assert snap.skyrim is None


async def test_herramientas_optativas_ausentes_no_impiden_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """READY significa que están las herramientas críticas, no todas las optativas."""
    skyrim_dir = tmp_path / "Skyrim"
    skyrim_dir.mkdir()
    skyrim_exe = _touch_exe(skyrim_dir, "SkyrimSE.exe")
    tool_paths = {key: str(_touch_exe(tmp_path, names[0])) for key, names, _technical in _GENERIC_TOOLS}
    monkeypatch.setattr(scanner_module, "find_skse_installation", lambda *_a, **_k: skyrim_exe)
    scanner = EnvironmentScanner(skyrim_path=skyrim_dir, tool_paths=tool_paths)

    snap = await scanner.scan()

    assert snap.missing
    assert all(not missing.is_critical for missing in snap.missing)
    assert snap.health_status is HealthStatus.READY

"""Tests de ``ToolsInstaller.ensure_community_shaders`` — autoinstalador de Community Shaders.

Cubre la instalación como mods de MO2 de (orden de dependencia):
- Address Library for SKSE Plugins (Nexus 32444, selector por cascada — la
  página migró al file MAIN "all game versions"),
- SSE Engine Fixes (Nexus 17230, file MAIN explícito — el primary genérico
  puede elegir el preloader de 24 KB),
- Community Shaders core (GitHub community-shaders/skyrim-community-shaders,
  asset ``CommunityShaders-*.zip`` — NUNCA ``CommunityShaders_AIO-*``).

Contrato congelado acá (Fase 0 del Blueprint):
- Gates fail-closed ANTES del primer HITL y del primer byte de egress:
  edición, downloader, versión (SE 1.5.97 exacta / AE >= 1.6.1170), SKSE
  presente, ENB ausente, preloader de Engine Fixes presente, y validación
  del ``mods_dir`` contra el sandbox (gate 7).
- Tres invariantes de ``clean_on_fail``: nunca en denegación HITL, nunca si el
  dir preexistía, nunca en ``CancelledError``.
- R2: verificación de integridad (digest sha256 de la API y pin de
  ``_PINNED_SHA256`` — el pin gana sobre el digest). El techo de bytes durante
  el streaming de ``_download_asset`` (helper compartido) vive en
  ``tests/test_tools_installer.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import pathlib
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

import sky_claw.local.tools_installer as ti_mod
from sky_claw.app.scraper.nexus_downloader import FileInfo
from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.app.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.app.security.path_validator import PathValidator, PathViolationError
from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.tools_installer import ToolInstallError, ToolsInstaller

# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def validator(tmp_path: pathlib.Path) -> PathValidator:
    return PathValidator(roots=[tmp_path])


@pytest.fixture
def gateway() -> NetworkGateway:
    return NetworkGateway(EgressPolicy(block_private_ips=False))


@pytest.fixture
def hitl_guard() -> HITLGuard:
    return HITLGuard(notify_fn=None, timeout=5)


@pytest.fixture
def installer(hitl_guard: HITLGuard, gateway: NetworkGateway, validator: PathValidator) -> ToolsInstaller:
    # Lock mockeado (mismo criterio que test_tools_installer_ngio.py): el
    # mecanismo real cross-process lo valida el test de contención.
    lock_manager = MagicMock()
    lock_manager.acquire_lock = AsyncMock(
        return_value=MagicMock(resource_id="tools-install:test", agent_id="tools-installer")
    )
    lock_manager.release_lock = AsyncMock(return_value=True)
    lock_manager.renew_lock = AsyncMock(return_value=True)
    return ToolsInstaller(
        hitl=hitl_guard,
        gateway=gateway,
        path_validator=validator,
        lock_manager=lock_manager,
        install_ttl=60.0,
    )


@pytest.fixture
def mods_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "mods"
    d.mkdir()
    return d


def _zip_bytes(entries: dict[str, str]) -> bytes:
    """Zip real en memoria con las entradas dadas (path → contenido)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


# Payloads reales por componente (layout Data/ = raíz del mod MO2).
_ZIP_ADDRLIB_SE = _zip_bytes({"SKSE/Plugins/version-1-5-97-0.bin": "fake-bin"})
_ZIP_ADDRLIB_AE = _zip_bytes({"SKSE/Plugins/version-1-6-1170-0.bin": "fake-bin"})
_ZIP_EF = _zip_bytes(
    {
        "SKSE/Plugins/EngineFixes.dll": "fake-dll",
        "SKSE/Plugins/EngineFixes.toml": "toml",
    }
)
_ZIP_EF_FOMOD = _zip_bytes(
    {
        "EngineFixes FOMOD Installer/AE/SKSE/Plugins/EngineFixes.dll": "fake-dll-ae",
        "EngineFixes FOMOD Installer/SE/SKSE/Plugins/EngineFixes.dll": "fake-dll-se",
        "EngineFixes FOMOD Installer/Required/SKSE/Plugins/EngineFixes.toml": "toml",
        "EngineFixes FOMOD Installer/fomod/ModuleConfig.xml": "<config/>",
    }
)
_ZIP_CS = _zip_bytes(
    {
        "SKSE/Plugins/CommunityShaders.dll": "fake-dll",
        "Shaders/Effect.hlsl": "shader",
    }
)
# Variante del bug real del upstream (1.8.0 #2588): zip sin los feature files.
_ZIP_CS_SIN_SHADERS = _zip_bytes({"SKSE/Plugins/CommunityShaders.dll": "fake-dll"})


# ---------------------------------------------------------------------------
# Listados de Nexus (files.json) y assets de GitHub — congelan el naming
# ---------------------------------------------------------------------------

# Address Library 2024: MAIN con SE/AE separados (naming del fixture NGIO).
_ADDRLIB_FILES_2024: list[dict[str, Any]] = [
    {
        "file_id": 493038,
        "name": "All in one (Special Edition)",
        "file_name": "All.in.one.Special.Edition.zip",
        "category_name": "MAIN",
    },
    {
        "file_id": 493039,
        "name": "All in one (Anniversary Edition)",
        "file_name": "All.in.one.Anniversary.Edition.zip",
        "category_name": "MAIN",
    },
]

# Address Library 2026 (v11): MAIN = un solo file universal (file_id REAL
# verificado contra files.json vivo en la Fase 1 del blueprint); los
# específicos por edición pasaron a OPTIONAL.
_ADDRLIB_FILES_2026: list[dict[str, Any]] = [
    {
        "file_id": 720756,
        "name": "Address Library - All in one (all game versions)",
        "file_name": "All in one (all game versions)-32444-11-1770897704.zip",
        "category_name": "MAIN",
        "is_primary": True,
        "version": "11",
    },
    {
        "file_id": 470707,
        "name": "All in one (1.6.X)",
        "file_name": "All in one Address Library (Anniversary Edition)-32444-11-1707902394.zip",
        "category_name": "OPTIONAL",
    },
]

# SSE Engine Fixes 7.0.20: MAIN con DOS archivos (file_ids REALES verificados
# contra files.json vivo) — Main File y SKSE64 Preloader. NINGUNO es primary.
_EF_FILES_DUAL: list[dict[str, Any]] = [
    {
        "file_id": 725753,
        "name": "Engine Fixes - Main File",
        "file_name": "Engine Fixes - Main File-17230-7-0-20-1772078239.7z",
        "category_name": "MAIN",
        "is_primary": False,
        "version": "7.0.20",
    },
    {
        "file_id": 725261,
        "name": "Engine Fixes - SKSE64 Preloader",
        "file_name": "Engine Fixes - SKSE64 Preloader-17230-7-1771936758.7z",
        "category_name": "MAIN",
        "is_primary": False,
        "version": "7",
    },
]

_FILES_POR_NEXUS: dict[int, list[dict[str, Any]]] = {
    32444: _ADDRLIB_FILES_2026,
    17230: _EF_FILES_DUAL,
}

# Assets reales de la release v1.8.2 (verificados contra la API de GitHub):
# el core ``CommunityShaders-<UTC>.zip`` + 13 zips de features opcionales.
_CS_ASSET_CORE = "CommunityShaders-2026-08-06T11-32Z.zip"
_CS_ASSETS_REALES = [
    _CS_ASSET_CORE,
    "Effects11-2026-08-06T11-32Z.zip",
    "Exponential.Height.Fog-2026-08-06T11-32Z.zip",
    "Hair.Specular-2026-08-06T11-32Z.zip",
    "HDR.Display-2026-08-06T11-32Z.zip",
    "Linear.Lighting-2026-08-06T11-32Z.zip",
    "Screen.Space.GI-2026-08-06T11-32Z.zip",
    "Skin-2026-08-06T11-32Z.zip",
    "Skylighting-2026-08-06T11-32Z.zip",
    "Terrain.Blending-2026-08-06T11-32Z.zip",
    "Terrain.Helper-2026-08-06T11-32Z.zip",
    "Terrain.Variation-2026-08-06T11-32Z.zip",
    "Upscaling-2026-08-06T11-32Z.zip",
    "Wetness.Effects-2026-08-06T11-32Z.zip",
]
# Trampa de nombres verificada en CMakeLists del upstream: el AIO se llama
# ``CommunityShaders_AIO-<UTC>.zip`` — el ancla del core es el GUION.
_CS_ASSET_AIO = "CommunityShaders_AIO-2026-08-06T11-32Z.zip"


def _cs_asset_entry(asset_name: str, size: int, digest: str | None = None) -> dict[str, Any]:
    entry = {
        "name": asset_name,
        "size": size,
        "url": (
            "https://api.github.com/repos/community-shaders/skyrim-community-shaders/"
            f"releases/assets/{abs(hash(asset_name)) & 0xFFFF}"
        ),
        "browser_download_url": (
            f"https://github.com/community-shaders/skyrim-community-shaders/releases/download/v1.8.2/{asset_name}"
        ),
    }
    if digest is not None:
        entry["digest"] = f"sha256:{digest}"
    return entry


def _cs_release_json(
    asset_name: str = _CS_ASSET_CORE,
    size: int = 0,
    digest: str | None = None,
    tag: str = "v1.8.2",
) -> dict[str, Any]:
    """Release de CS con los 14 assets reales; el elegido es *asset_name*."""
    otros = [n for n in _CS_ASSETS_REALES if n != asset_name]
    return {
        "tag_name": tag,
        "assets": [
            _cs_asset_entry(asset_name, size, digest),
            *(_cs_asset_entry(n, size) for n in sorted(otros)),
        ],
    }


def _mock_gateway_github_cs(
    installer: ToolsInstaller,
    zip_payload: bytes,
    *,
    asset_name: str = _CS_ASSET_CORE,
    digest: str | None = None,
) -> None:
    """Encadena las dos respuestas del gateway: metadata del release y stream binario.

    Por defecto el digest es el sha256 REAL del payload: el happy path ejercita
    la verificación de integridad; pasar un digest distinto lo vuelve rojo.
    """
    if digest is None:
        digest = hashlib.sha256(zip_payload).hexdigest()
    release_json = _cs_release_json(asset_name=asset_name, size=len(zip_payload), digest=digest)

    mock_api_resp = MagicMock()
    mock_api_resp.status = 200
    mock_api_resp.json = AsyncMock(return_value=release_json)
    mock_api_resp.release = MagicMock()

    async def _iter_chunks(size: int):
        yield zip_payload

    mock_dl_resp = MagicMock()
    mock_dl_resp.status = 200
    mock_dl_resp.raise_for_status = MagicMock()
    mock_dl_resp.release = MagicMock()
    mock_dl_resp.content = MagicMock()
    mock_dl_resp.content.iter_chunked = _iter_chunks

    installer._gateway.request = AsyncMock(side_effect=[mock_api_resp, mock_dl_resp])  # type: ignore[method-assign]


def _fake_downloader(staging: pathlib.Path, archives: dict[int, tuple[str, bytes]]) -> MagicMock:
    """NexusDownloader falso: sirve zips reales y listados por mod (32444 / 17230)."""
    staging.mkdir(parents=True, exist_ok=True)
    dl = MagicMock()

    async def _get_file_info(nexus_id: int, file_id: int | None, session: Any) -> FileInfo:
        file_name, data = archives[nexus_id]
        return FileInfo(
            nexus_id=nexus_id,
            file_id=file_id if file_id is not None else 90_000 + nexus_id,
            file_name=file_name,
            size_bytes=len(data),
            md5="",
        )

    async def _download(file_info: FileInfo, session: Any) -> pathlib.Path:
        dest = staging / file_info.file_name
        dest.write_bytes(archives[file_info.nexus_id][1])
        return dest

    dl.list_files = AsyncMock(side_effect=lambda nexus_id, session: _FILES_POR_NEXUS.get(nexus_id, []))
    dl.get_file_info = AsyncMock(side_effect=_get_file_info)
    dl.download = AsyncMock(side_effect=_download)
    return dl


def _downloader_completo(tmp_path: pathlib.Path, *, addrlib_zip: bytes = _ZIP_ADDRLIB_SE) -> MagicMock:
    return _fake_downloader(
        tmp_path / "staging",
        {
            32444: ("Address.Library.All.in.one.all.game.versions.zip", addrlib_zip),
            17230: ("Engine.Fixes.Main.File.zip", _ZIP_EF_FOMOD),
        },
    )


def _aprobar_todo(installer: ToolsInstaller) -> AsyncMock:
    mock = AsyncMock(return_value=Decision.APPROVED)
    installer._hitl.request_approval = mock  # type: ignore[method-assign]
    return mock


def _hitl_espiado(installer: ToolsInstaller, *, denegar_call: int | None = None) -> AsyncMock:
    """Reemplaza el guard por un spy que puede denegar una llamada por orden."""
    call_count = 0

    async def _decidir(**_kwargs: Any) -> Decision:
        nonlocal call_count
        call_count += 1
        if denegar_call is not None and call_count == denegar_call:
            return Decision.DENIED
        return Decision.APPROVED

    mock = AsyncMock(side_effect=_decidir)
    installer._hitl.request_approval = mock  # type: ignore[method-assign]
    return mock


def _sentinel(mod_dir: pathlib.Path, patron: str) -> bool:
    return any((mod_dir / "SKSE" / "Plugins").glob(patron))


def _juego_valido(game_dir: pathlib.Path) -> None:
    """Host que satisface los gates 4-6: SKSE instalado, sin ENB, preloader presente."""
    game_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "skse64_loader.exe").write_text("fake", encoding="utf-8")
    (game_dir / "d3dx9_42.dll").write_text("fake-preloader", encoding="utf-8")


def _mods_sin_payload(mods_dir: pathlib.Path) -> None:
    """Mods instalados con sentinel + estructura válida → el flujo debe ser idempotente.

    CS incluye ``Shaders/``: con T4, un CS con DLL pero sin Shaders/ (bug real
    1.8.0) NO cuenta como already_existed — se re-instala por diseño. EF incluye
    ``EngineFixes.toml``: con ``required_files``, un EF con el DLL pero sin su
    config NO cuenta como already_existed (el fresh install es fail-closed ante
    eso; la idempotencia no puede ser más laxa).
    """
    for nombre, payload in (
        (ti_mod.ADDRESS_LIBRARY_MOD_NAME, "version-1-5-97-0.bin"),
        (ti_mod.SSE_ENGINE_FIXES_MOD_NAME, "EngineFixes.dll"),
        (ti_mod.COMMUNITY_SHADERS_MOD_NAME, "CommunityShaders.dll"),
    ):
        plugins = mods_dir / nombre / "SKSE" / "Plugins"
        plugins.mkdir(parents=True)
        (plugins / payload).write_text("fake", encoding="utf-8")
    (mods_dir / ti_mod.SSE_ENGINE_FIXES_MOD_NAME / "SKSE" / "Plugins" / "EngineFixes.toml").write_text(
        "toml", encoding="utf-8"
    )
    (mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME / "Shaders").mkdir()


# ---------------------------------------------------------------------------
# Funciones puras: parseo de versión y detectores de host
# ---------------------------------------------------------------------------


class TestParseGameVersion:
    @pytest.mark.parametrize(
        ("raw", "esperado"),
        [
            ("1.5.97", (1, 5, 97)),
            ("1.6.1170", (1, 6, 1170)),
            ("1.6.1170 ", (1, 6, 1170)),
            ("1.6.1130.0", (1, 6, 1130, 0)),
        ],
    )
    def test_versiones_validas(self, raw: str, esperado: tuple[int, ...]) -> None:
        assert ti_mod._parse_game_version(raw) == esperado

    @pytest.mark.parametrize("raw", ["", "abc", "1.6.x", "1..2"])
    def test_versiones_invalidas_devuelven_none(self, raw: str) -> None:
        assert ti_mod._parse_game_version(raw) is None


class TestDetectoresDeHost:
    def test_detectar_skse(self, tmp_path: pathlib.Path) -> None:
        assert ti_mod._detectar_skse(tmp_path) is False
        (tmp_path / "skse64_loader.exe").write_text("x", encoding="utf-8")
        assert ti_mod._detectar_skse(tmp_path) is True

    @pytest.mark.parametrize("firma", ["enbseries.ini", "enbseries"])
    def test_detectar_enb(self, tmp_path: pathlib.Path, firma: str) -> None:
        assert ti_mod._detectar_enb(tmp_path) is False
        p = tmp_path / firma
        if firma.endswith(".ini"):
            p.write_text("x", encoding="utf-8")
        else:
            p.mkdir()
        assert ti_mod._detectar_enb(tmp_path) is True

    def test_detectar_preloader(self, tmp_path: pathlib.Path) -> None:
        assert ti_mod._detectar_preloader(tmp_path) is False
        (tmp_path / "d3dx9_42.dll").write_text("x", encoding="utf-8")
        assert ti_mod._detectar_preloader(tmp_path) is True


class TestTransformFomodEngineFixes:
    @pytest.mark.parametrize(
        ("edition", "esperado"),
        [(SkyrimEdition.AE, "fake-dll-ae"), (SkyrimEdition.SE, "fake-dll-se")],
    )
    def test_selecciona_la_rama_por_edicion_y_mergea_required(
        self, tmp_path: pathlib.Path, edition: SkyrimEdition, esperado: str
    ) -> None:
        """El FOMOD real de EF (verificado en el archive de Nexus 7.0.20) se resuelve
        a un layout MO2 válido: SKSE/Plugins/EngineFixes.dll de la rama + config de Required."""
        mod_dir = tmp_path / "mod"
        for rel, contenido in (
            ("AE/SKSE/Plugins/EngineFixes.dll", "fake-dll-ae"),
            ("SE/SKSE/Plugins/EngineFixes.dll", "fake-dll-se"),
            ("Required/SKSE/Plugins/EngineFixes.toml", "toml"),
            ("Required/SKSE/Plugins/EngineFixes_SNCT.ini", "ini"),
            ("fomod/ModuleConfig.xml", "<config/>"),
        ):
            p = mod_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(contenido, encoding="utf-8")

        ti_mod._transform_fomod_engine_fixes(mod_dir, edition)

        assert (mod_dir / "SKSE" / "Plugins" / "EngineFixes.dll").read_text(encoding="utf-8") == esperado
        assert (mod_dir / "SKSE" / "Plugins" / "EngineFixes.toml").read_text(encoding="utf-8") == "toml"
        assert (mod_dir / "SKSE" / "Plugins" / "EngineFixes_SNCT.ini").read_text(encoding="utf-8") == "ini"
        # El FOMOD ya no contamina el mod.
        assert not (mod_dir / "AE").exists()
        assert not (mod_dir / "SE").exists()
        assert not (mod_dir / "Required").exists()
        assert not (mod_dir / "fomod").exists()

    def test_rama_ausente_aborta_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """FOMOD sin la rama de la edición (upstream cambió el layout) → abort, no adivinar."""
        mod_dir = tmp_path / "mod"
        (mod_dir / "AE" / "SKSE" / "Plugins").mkdir(parents=True)
        (mod_dir / "AE" / "SKSE" / "Plugins" / "EngineFixes.dll").write_text("x", encoding="utf-8")

        with pytest.raises(ToolInstallError, match="EngineFixes.dll"):
            ti_mod._transform_fomod_engine_fixes(mod_dir, SkyrimEdition.SE)

    def test_required_ausente_aborta_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """FOMOD sin Required/SKSE (config del plugin) → abort: un mod 'instalado'
        sin EngineFixes.toml arrancaría con defaults incorrectos — nunca adivinar."""
        mod_dir = tmp_path / "mod"
        (mod_dir / "AE" / "SKSE" / "Plugins").mkdir(parents=True)
        (mod_dir / "AE" / "SKSE" / "Plugins" / "EngineFixes.dll").write_text("x", encoding="utf-8")

        with pytest.raises(ToolInstallError, match="Required"):
            ti_mod._transform_fomod_engine_fixes(mod_dir, SkyrimEdition.AE)


# ---------------------------------------------------------------------------
# _select_address_library_file — cascada (2024 / 2026 / hostil)
# ---------------------------------------------------------------------------


class TestCascadaAddressLibrary:
    def test_cascada_2026_universal_para_se_y_ae(self) -> None:
        """El MAIN actual ('all game versions') sirve para ambas ediciones."""
        assert ti_mod._select_address_library_file(_ADDRLIB_FILES_2026, SkyrimEdition.SE) == 720756
        assert ti_mod._select_address_library_file(_ADDRLIB_FILES_2026, SkyrimEdition.AE) == 720756

    def test_cascada_2024_por_edicion(self) -> None:
        """Naming histórico: 'special'/'anniversary' siguen ganando por edición."""
        assert ti_mod._select_address_library_file(_ADDRLIB_FILES_2024, SkyrimEdition.SE) == 493038
        assert ti_mod._select_address_library_file(_ADDRLIB_FILES_2024, SkyrimEdition.AE) == 493039

    def test_cascada_especifico_gana_sobre_universal(self) -> None:
        """Si conviven específico y universal en MAIN, el específico gana."""
        files = [
            {
                "file_id": 720756,
                "name": "Address Library - All in one (all game versions)",
                "file_name": "u.zip",
                "category_name": "MAIN",
            },
            {
                "file_id": 493039,
                "name": "All in one (Anniversary Edition)",
                "file_name": "a.zip",
                "category_name": "MAIN",
            },
        ]
        assert ti_mod._select_address_library_file(files, SkyrimEdition.AE) == 493039

    def test_cascada_ignora_files_optional(self) -> None:
        """El '1.6.X' en Optional NO gana: la cascada filtra por MAIN primero."""
        assert ti_mod._select_address_library_file(_ADDRLIB_FILES_2026, SkyrimEdition.AE) == 720756

    def test_cascada_tolera_file_id_como_lista(self) -> None:
        """Quirk de la API: file_id como lista [id, algo] → se toma el segundo."""
        files = [
            {
                "file_id": [720756, 2295],
                "name": "Address Library - All in one (all game versions)",
                "file_name": "u.zip",
                "category_name": "MAIN",
            },
        ]
        assert ti_mod._select_address_library_file(files, SkyrimEdition.SE) == 2295

    def test_cascada_sin_match_aborta_con_disponibles(self) -> None:
        """Nada matchea (ni específico ni familia ni universal) → abort con la lista."""
        files = [{"file_id": 1, "name": "Solo viejos", "file_name": "v.zip", "category_name": "MAIN"}]
        with pytest.raises(ToolInstallError) as exc_info:
            ti_mod._select_address_library_file(files, SkyrimEdition.AE)
        assert "Solo viejos" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _select_engine_fixes_file — el primary genérico puede ser el preloader
# ---------------------------------------------------------------------------


class TestSeleccionEngineFixesFile:
    def test_elige_main_file_no_preloader(self) -> None:
        """MAIN con Main File + Preloader → el plugin SKSE (7.6 MB), nunca el preloader."""
        assert ti_mod._select_engine_fixes_file(_EF_FILES_DUAL) == 725753

    def test_solo_preloader_aborta(self) -> None:
        """Si solo queda el preloader, abort con la lista (instalarlo en mods/ es inerte)."""
        files = [
            {
                "file_id": 725261,
                "name": "Engine Fixes - SKSE64 Preloader",
                "file_name": "p.zip",
                "category_name": "MAIN",
            },
        ]
        with pytest.raises(ToolInstallError) as exc_info:
            ti_mod._select_engine_fixes_file(files)
        assert "Preloader" in str(exc_info.value)

    def test_ambiguedad_aborta(self) -> None:
        """Dos candidatos MAIN sin preloader y ambos 'main file' → nunca elegir al azar."""
        files = [
            {"file_id": 1, "name": "Engine Fixes - Main File", "file_name": "a.zip", "category_name": "MAIN"},
            {"file_id": 2, "name": "Engine Fixes - Main File", "file_name": "b.zip", "category_name": "MAIN"},
        ]
        with pytest.raises(ToolInstallError):
            ti_mod._select_engine_fixes_file(files)


# ---------------------------------------------------------------------------
# _select_community_shaders_asset — ancla en el GUION (trampa _AIO)
# ---------------------------------------------------------------------------


class TestSeleccionCommunityShadersAsset:
    def test_elige_el_core_entre_assets_reales(self) -> None:
        """Assets reales de v1.8.2: el core gana; features y AIO quedan afuera."""
        assets = [_cs_asset_entry(n, 1) for n in _CS_ASSETS_REALES] + [
            _cs_asset_entry(_CS_ASSET_AIO, 1),
            _cs_asset_entry("Source code (zip)", 1),
        ]
        assert ti_mod._select_community_shaders_asset(assets)["name"] == _CS_ASSET_CORE

    def test_aio_y_source_code_nunca_son_core(self) -> None:
        """Solo AIO + Source code disponibles → no hay core → abort."""
        assets = [_cs_asset_entry(_CS_ASSET_AIO, 1), _cs_asset_entry("Source code (zip)", 1)]
        with pytest.raises(ToolInstallError):
            ti_mod._select_community_shaders_asset(assets)

    def test_dos_cores_aborta_por_ambiguedad(self) -> None:
        """Dos 'CommunityShaders-*.zip' en la misma release → abort, nunca elegir al azar."""
        assets = [
            _cs_asset_entry("CommunityShaders-2026-08-06T11-32Z.zip", 1),
            _cs_asset_entry("CommunityShaders-2026-08-06T12-00Z.zip", 1),
        ]
        with pytest.raises(ToolInstallError):
            ti_mod._select_community_shaders_asset(assets)

    def test_sin_match_lista_disponibles(self) -> None:
        """Nada con el prefijo del core → abort con la lista de disponibles."""
        assets = [_cs_asset_entry("Effects11-2026-08-06T11-32Z.zip", 1)]
        with pytest.raises(ToolInstallError) as exc_info:
            ti_mod._select_community_shaders_asset(assets)
        assert "Effects11-2026-08-06T11-32Z.zip" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ensure_community_shaders — gates fail-closed (sin red, sin HITL)
# ---------------------------------------------------------------------------


class TestEnsureCommunityShadersGates:
    @pytest.mark.parametrize("edition", [SkyrimEdition.LE, SkyrimEdition.UNKNOWN])
    async def test_edicion_le_o_unknown_rechazada_sin_red(
        self,
        installer: ToolsInstaller,
        mods_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        edition: SkyrimEdition,
    ) -> None:
        """LE/UNKNOWN abortan antes de HITL, gateway y downloader."""
        hitl = _hitl_espiado(installer)
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        _juego_valido(tmp_path / "game")

        with pytest.raises(ToolInstallError):
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                downloader,
                edition=edition,
                game_version="1.6.1170",
                game_dir=tmp_path / "game",
            )

        assert hitl.await_count == 0
        assert installer._gateway.request.await_count == 0
        assert downloader.download.await_count == 0

    async def test_sin_downloader_falla_con_mensaje_claro(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Address Library y Engine Fixes vienen de Nexus: sin downloader, abort explícito."""
        hitl = _hitl_espiado(installer)
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        session = MagicMock(spec=aiohttp.ClientSession)
        _juego_valido(tmp_path / "game")

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                None,
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=tmp_path / "game",
            )

        assert "NexusDownloader" in str(exc_info.value)
        assert hitl.await_count == 0

    @pytest.mark.parametrize(
        ("edition", "version_ok"),
        [(SkyrimEdition.SE, "1.5.97.0"), (SkyrimEdition.AE, "1.6.1170.0")],
    )
    async def test_version_con_cuarto_componente_normaliza(
        self,
        installer: ToolsInstaller,
        mods_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        edition: SkyrimEdition,
        version_ok: str,
    ) -> None:
        """ProductVersion de Windows (4 componentes: 1.5.97.0) se normaliza a
        major.minor.build: el gate NO aborta en SE reales (hallazgo review PR #442)."""
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS)
        _aprobar_todo(installer)
        downloader = _downloader_completo(
            tmp_path, addrlib_zip=_ZIP_ADDRLIB_AE if edition is SkyrimEdition.AE else _ZIP_ADDRLIB_SE
        )
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=edition,
            game_version=version_ok,
            game_dir=game_dir,
        )

        assert len(resultados) == 3
        assert resultados[0].already_existed is False

    async def test_ae_1640_aborta_fail_closed(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """1.6.640 NO está soportada (texto oficial de CS): abort antes de HITL."""
        hitl = _hitl_espiado(installer)
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        _juego_valido(tmp_path / "game")

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                downloader,
                edition=SkyrimEdition.AE,
                game_version="1.6.640",
                game_dir=tmp_path / "game",
            )

        assert "1.6.1170" in str(exc_info.value)
        assert hitl.await_count == 0
        assert downloader.download.await_count == 0

    async def test_se_version_equivocada_aborta(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """SE solo soporta 1.5.97 exacta (verificada en el PE del juego)."""
        hitl = _hitl_espiado(installer)
        session = MagicMock(spec=aiohttp.ClientSession)
        _juego_valido(tmp_path / "game")

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                _downloader_completo(tmp_path),
                edition=SkyrimEdition.SE,
                game_version="1.5.80",
                game_dir=tmp_path / "game",
            )

        assert "1.5.97" in str(exc_info.value)
        assert hitl.await_count == 0

    @pytest.mark.parametrize("version", ["", "no-es-version"])
    async def test_version_vacia_o_ilegible_aborta(
        self,
        installer: ToolsInstaller,
        mods_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        version: str,
    ) -> None:
        """Fail-closed: sin versión legible no se puede garantizar el gate AE."""
        hitl = _hitl_espiado(installer)
        session = MagicMock(spec=aiohttp.ClientSession)
        _juego_valido(tmp_path / "game")

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                _downloader_completo(tmp_path),
                edition=SkyrimEdition.AE,
                game_version=version,
                game_dir=tmp_path / "game",
            )

        assert "versión" in str(exc_info.value).lower()
        assert hitl.await_count == 0

    async def test_sin_skse_en_game_dir_aborta(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """CS es un plugin SKSE: sin el loader en el juego, abort accionable."""
        hitl = _hitl_espiado(installer)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        game_dir.mkdir()

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                _downloader_completo(tmp_path),
                edition=SkyrimEdition.AE,
                game_version="1.6.1170",
                game_dir=game_dir,
            )

        assert "SKSE" in str(exc_info.value)
        assert hitl.await_count == 0

    async def test_enb_presente_aborta_antes_de_descargar(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """ENB detectado (enbseries.ini): CS se auto-deshabilita; instalar sería un no-op."""
        hitl = _hitl_espiado(installer)
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        (game_dir / "skse64_loader.exe").write_text("fake", encoding="utf-8")
        (game_dir / "enbseries.ini").write_text("fake", encoding="utf-8")

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                downloader,
                edition=SkyrimEdition.AE,
                game_version="1.6.1170",
                game_dir=game_dir,
            )

        assert "ENB" in str(exc_info.value)
        assert hitl.await_count == 0
        assert downloader.download.await_count == 0
        assert installer._gateway.request.await_count == 0

    async def test_preloader_ausente_aborta_con_instruccion(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Sin el preloader de Engine Fixes en la raíz del juego, CS falla su check en runtime."""
        hitl = _hitl_espiado(installer)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        game_dir.mkdir()
        (game_dir / "skse64_loader.exe").write_text("fake", encoding="utf-8")

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                _downloader_completo(tmp_path),
                edition=SkyrimEdition.AE,
                game_version="1.6.1170",
                game_dir=game_dir,
            )

        assert "Preloader" in str(exc_info.value)
        assert hitl.await_count == 0

    async def test_mods_dir_fuera_del_sandbox_aborta(
        self,
        installer: ToolsInstaller,
        tmp_path: pathlib.Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Gate 7: un mods_dir fuera de los roots del PathValidator aborta antes
        de HITL, egress y downloads (completa la enumeración de los 7 gates)."""
        hitl = _hitl_espiado(installer)
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        _juego_valido(tmp_path / "game")
        mods_afuera = tmp_path_factory.mktemp("otro_sandbox") / "mods"
        mods_afuera.mkdir()

        with pytest.raises(PathViolationError):
            await installer.ensure_community_shaders(
                mods_afuera,
                session,
                downloader,
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=tmp_path / "game",
            )

        assert hitl.await_count == 0
        assert installer._gateway.request.await_count == 0
        assert downloader.download.await_count == 0


# ---------------------------------------------------------------------------
# ensure_community_shaders — instalación (happy path, idempotencia, abortos)
# ---------------------------------------------------------------------------


class TestEnsureCommunityShadersInstalacion:
    async def test_instala_los_tres_componentes_en_se(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Happy path SE: 3 mods con sentinel + Shaders/; 3 HITL category=download en orden."""
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS)
        hitl = _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=SkyrimEdition.SE,
            game_version="1.5.97",
            game_dir=game_dir,
        )

        assert [r.mod_name for r in resultados] == [
            ti_mod.ADDRESS_LIBRARY_MOD_NAME,
            ti_mod.SSE_ENGINE_FIXES_MOD_NAME,
            ti_mod.COMMUNITY_SHADERS_MOD_NAME,
        ]
        assert all(r.already_existed is False for r in resultados)
        assert _sentinel(mods_dir / ti_mod.ADDRESS_LIBRARY_MOD_NAME, "*.bin")
        assert _sentinel(mods_dir / ti_mod.SSE_ENGINE_FIXES_MOD_NAME, "EngineFixes.dll")
        assert _sentinel(mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME, "CommunityShaders.dll")
        assert (mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME / "Shaders").is_dir()
        # Orden de aprobación = orden de dependencia; todas category="download".
        assert hitl.await_count == 3
        assert all(c.kwargs["category"] == "download" for c in hitl.await_args_list)
        ids = [c.kwargs["request_id"] for c in hitl.await_args_list]
        assert ids[0].startswith("nexus-mod-install-")
        assert ids[1].startswith("nexus-mod-install-")
        assert ids[2].startswith("github-mod-install-")
        assert len(set(ids)) == 3
        # Selectores: cascada universal 2026 y file MAIN explícito de Engine Fixes.
        assert downloader.get_file_info.await_args_list[0].args[0:2] == (32444, 720756)
        assert downloader.get_file_info.await_args_list[1].args[0:2] == (17230, 725753)
        assert resultados[2].version == "v1.8.2"

    async def test_instala_los_tres_componentes_en_ae(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Happy path AE: sentinel version-1-6-*.bin; el gate de versión pasa con 1.6.1170."""
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS)
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path, addrlib_zip=_ZIP_ADDRLIB_AE)
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=SkyrimEdition.AE,
            game_version="1.6.1170",
            game_dir=game_dir,
        )

        assert len(resultados) == 3
        assert _sentinel(mods_dir / ti_mod.ADDRESS_LIBRARY_MOD_NAME, "version-1-6-*.bin")
        assert _sentinel(mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME, "CommunityShaders.dll")

    async def test_idempotente_con_sentinels_no_pide_hitl_ni_red(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """3 mods con sentinel: already_existed=True, cero HITL, cero egress, cero download."""
        _mods_sin_payload(mods_dir)
        hitl = _aprobar_todo(installer)
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        resultados = await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=SkyrimEdition.SE,
            game_version="1.5.97",
            game_dir=game_dir,
        )

        assert all(r.already_existed is True for r in resultados)
        assert all(r.version == "existing" for r in resultados)
        assert hitl.await_count == 0
        assert installer._gateway.request.await_count == 0
        assert downloader.get_file_info.await_count == 0
        assert downloader.download.await_count == 0

    async def test_cs_roto_sin_shaders_no_es_already_existed(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Mod con el DLL pero sin Shaders/ (bug real CS 1.8.0 #2588) NO cuenta como
        instalado: required_rel se re-verifica en el camino already_existed y el
        componente se re-instala (el agente jamás reporta un mod roto como listo)."""
        # Árbol propio: Address Library + EF idempotentes, CS con DLL pero SIN Shaders/.
        for nombre, payload in (
            (ti_mod.ADDRESS_LIBRARY_MOD_NAME, "version-1-5-97-0.bin"),
            (ti_mod.SSE_ENGINE_FIXES_MOD_NAME, "EngineFixes.dll"),
        ):
            plugins = mods_dir / nombre / "SKSE" / "Plugins"
            plugins.mkdir(parents=True)
            (plugins / payload).write_text("fake", encoding="utf-8")
        # EF idempotente exige su config (required_files): sin el toml NO sería
        # already_existed y este test mediría otra cosa.
        (mods_dir / ti_mod.SSE_ENGINE_FIXES_MOD_NAME / "SKSE" / "Plugins" / "EngineFixes.toml").write_text(
            "toml", encoding="utf-8"
        )
        cs_dir = mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME
        plugins = cs_dir / "SKSE" / "Plugins"
        plugins.mkdir(parents=True)
        (plugins / "CommunityShaders.dll").write_text("x", encoding="utf-8")
        assert not (cs_dir / "Shaders").exists()

        _mock_gateway_github_cs(installer, _ZIP_CS)
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        resultados = await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=SkyrimEdition.SE,
            game_version="1.5.97",
            game_dir=game_dir,
        )

        assert resultados[0].already_existed is True  # Address Library
        assert resultados[1].already_existed is True  # SSE Engine Fixes
        assert resultados[2].already_existed is False  # CS roto → se re-instaló
        assert (mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME / "Shaders").is_dir()

    async def test_engine_fixes_sin_toml_no_es_already_existed(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """EF con el DLL pero SIN ``EngineFixes.toml`` NO cuenta como instalado.

        La instalación nueva es fail-closed ante un FOMOD sin ``Required/SKSE``
        (dejaría el plugin arrancando con defaults incorrectos), pero el camino
        idempotente aceptaba ese mismo mod roto con solo el sentinel del DLL:
        el fresh install era más estricto que el ``already_existed``
        (inconsistencia cazada en la revisión de #442). ``required_files`` cierra
        la brecha en AMBOS caminos: el mod se re-instala (reparándolo) en vez de
        reportarse listo.
        """
        # Árbol propio: Address Library + CS idempotentes; EF con DLL pero SIN toml.
        for nombre, payload in (
            (ti_mod.ADDRESS_LIBRARY_MOD_NAME, "version-1-5-97-0.bin"),
            (ti_mod.SSE_ENGINE_FIXES_MOD_NAME, "EngineFixes.dll"),
        ):
            plugins = mods_dir / nombre / "SKSE" / "Plugins"
            plugins.mkdir(parents=True)
            (plugins / payload).write_text("fake", encoding="utf-8")
        ef_plugins = mods_dir / ti_mod.SSE_ENGINE_FIXES_MOD_NAME / "SKSE" / "Plugins"
        assert not (ef_plugins / "EngineFixes.toml").exists()
        cs_dir = mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME
        plugins = cs_dir / "SKSE" / "Plugins"
        plugins.mkdir(parents=True)
        (plugins / "CommunityShaders.dll").write_text("x", encoding="utf-8")
        (cs_dir / "Shaders").mkdir()

        _mock_gateway_github_cs(installer, _ZIP_CS)
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        resultados = await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=SkyrimEdition.SE,
            game_version="1.5.97",
            game_dir=game_dir,
        )

        assert resultados[0].already_existed is True  # Address Library
        assert resultados[1].already_existed is False  # EF roto → se re-instaló
        assert resultados[2].already_existed is True  # CS
        # La reparación trajo la config: el FOMOD resuelto incluye EngineFixes.toml.
        assert (ef_plugins / "EngineFixes.toml").is_file()

    async def test_denegacion_a_mitad_aborta_el_resto_y_conserva_lo_instalado(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Operador deniega SSE Engine Fixes: Address Library queda instalada, CS nunca se descarga."""
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS)
        hitl = _hitl_espiado(installer, denegar_call=2)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError):
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                downloader,
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

        # Solo se pidieron 2 aprobaciones (addrlib aprobada, EF denegada).
        assert hitl.await_count == 2
        # Address Library quedó instalada (patrón NGIO: lo ya instalado queda).
        assert _sentinel(mods_dir / ti_mod.ADDRESS_LIBRARY_MOD_NAME, "*.bin")
        # El core jamás se descargó: un solo download de Nexus y cero gateway.
        assert downloader.download.await_count == 1
        assert installer._gateway.request.await_count == 0
        assert not (mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME).exists()

    async def test_payload_incompleto_sin_shaders_aborta_y_limpia(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Zip del core sin Shaders/ (bug real 1.8.0 #2588): abort + mod_dir limpio; lo previo queda."""
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS_SIN_SHADERS)
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                downloader,
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

        assert "Shaders" in str(exc_info.value)
        # clean_on_fail: el mod_dir del core quedó limpio (era nuevo).
        assert not (mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME).exists()
        # Los dos componentes previos quedaron instalados.
        assert _sentinel(mods_dir / ti_mod.ADDRESS_LIBRARY_MOD_NAME, "*.bin")
        assert _sentinel(mods_dir / ti_mod.SSE_ENGINE_FIXES_MOD_NAME, "EngineFixes.dll")

    async def test_digest_sha256_mismatch_aborta_y_borra(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Digest de la API ≠ sha256 real: abort, sin zips residuales, mod_dir limpio."""
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS, digest="0" * 64)
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                downloader,
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

        assert "SHA-256" in str(exc_info.value)
        assert list(mods_dir.rglob("*.zip")) == []
        assert not (mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME).exists()

    async def test_pin_sha256_gana_sobre_digest(
        self,
        installer: ToolsInstaller,
        mods_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pin explícito de _PINNED_SHA256 correcto + digest malo de la API → instala igual."""
        monkeypatch.setitem(ti_mod._PINNED_SHA256, _CS_ASSET_CORE, hashlib.sha256(_ZIP_CS).hexdigest())
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS, digest="0" * 64)
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=SkyrimEdition.SE,
            game_version="1.5.97",
            game_dir=game_dir,
        )

        assert _sentinel(mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME, "CommunityShaders.dll")
        assert resultados[2].already_existed is False

    async def test_meta_ini_con_comment_correcto(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """El meta.ini de CS habla de Community Shaders; ninguno miente sobre el precache de grass."""
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS)
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=SkyrimEdition.SE,
            game_version="1.5.97",
            game_dir=game_dir,
        )

        meta_cs = (mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME / "meta.ini").read_text(encoding="utf-8")
        assert "Community Shaders" in meta_cs
        assert "precache" not in meta_cs.lower()
        meta_ef = (mods_dir / ti_mod.SSE_ENGINE_FIXES_MOD_NAME / "meta.ini").read_text(encoding="utf-8")
        assert "precache de grass" not in meta_ef.lower()


# ---------------------------------------------------------------------------
# clean_on_fail — las tres invariantes
# ---------------------------------------------------------------------------


class TestCleanOnFailInvariantes:
    def test_promocion_posix_no_reemplaza_destino_vacio_aparecido_en_carrera(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La promoción no-replace conserva ambos árboles aun con semántica POSIX."""
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "propio.txt").write_text("sky-claw", encoding="utf-8")
        destino = tmp_path / "mod"
        destino.mkdir()

        exists_real = pathlib.Path.exists
        rename_real = pathlib.Path.rename

        def _exists_con_carrera(path: pathlib.Path) -> bool:
            # Simula que el precheck vio ausente el destino aunque otro proceso
            # ya lo creó vacío antes de la operación de rename.
            if path == destino:
                return False
            return exists_real(path)

        def _rename_con_semantica_posix(path: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
            if path == payload and target == destino and destino.is_dir() and not any(destino.iterdir()):
                destino.rmdir()
            return rename_real(path, target)

        monkeypatch.setattr(pathlib.Path, "exists", _exists_con_carrera)
        monkeypatch.setattr(pathlib.Path, "rename", _rename_con_semantica_posix)
        monkeypatch.setattr(ti_mod, "_ES_WINDOWS", False, raising=False)

        with pytest.raises(ToolInstallError, match="apareció"):
            ti_mod._promover_payload_verificado(payload, destino, "Mod externo")

        assert destino.is_dir()
        assert payload.is_dir()
        assert (payload / "propio.txt").read_text(encoding="utf-8") == "sky-claw"

    @pytest.mark.parametrize("origen", ["nexus", "github"])
    async def test_fallo_de_cleanup_no_enmascara_la_excepcion_original_en_ningun_hermano(
        self,
        installer: ToolsInstaller,
        mods_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        origen: str,
    ) -> None:
        """Un lock transitorio al limpiar no reemplaza el diagnóstico de instalación."""
        extract_real = installer._extract

        def _extract_que_falla(archive: pathlib.Path, dest: pathlib.Path) -> None:
            es_github = archive.name == _CS_ASSET_CORE
            if (origen == "github" and es_github) or (origen == "nexus" and not es_github):
                raise ToolInstallError(f"fallo original {origen}")
            extract_real(archive, dest)

        cleanup_real = ti_mod.rmtree_link_aware
        destino_del_fallo = mods_dir / (
            ti_mod.ADDRESS_LIBRARY_MOD_NAME if origen == "nexus" else ti_mod.COMMUNITY_SHADERS_MOD_NAME
        )

        def _cleanup_lockeado(path: pathlib.Path, **kwargs: object) -> None:
            if path == destino_del_fallo or path.name.startswith("payload-"):
                raise PermissionError(32, "archivo en uso")
            cleanup_real(path, **kwargs)

        installer._extract = _extract_que_falla  # type: ignore[method-assign]
        monkeypatch.setattr(ti_mod, "rmtree_link_aware", _cleanup_lockeado)
        _aprobar_todo(installer)
        if origen == "github":
            _mock_gateway_github_cs(installer, _ZIP_CS)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        with pytest.raises(ToolInstallError, match=f"fallo original {origen}"):
            await installer.ensure_community_shaders(
                mods_dir,
                MagicMock(spec=aiohttp.ClientSession),
                _downloader_completo(tmp_path),
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

    async def test_nexus_no_borra_archivos_creados_por_un_escritor_externo_durante_la_operacion(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """El cleanup sólo toca staging propio aunque el destino aparezca tras el precheck."""
        destino = mods_dir / ti_mod.ADDRESS_LIBRARY_MOD_NAME
        nota = destino / "creado-por-mo2.txt"

        def _extract_con_carrera(_archive: pathlib.Path, _dest: pathlib.Path) -> None:
            destino.mkdir(parents=True, exist_ok=True)
            nota.write_text("externo", encoding="utf-8")
            raise ToolInstallError("fallo simulado después de la carrera")

        installer._extract = _extract_con_carrera  # type: ignore[method-assign]
        _aprobar_todo(installer)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        with pytest.raises(ToolInstallError, match="fallo simulado"):
            await installer.ensure_community_shaders(
                mods_dir,
                MagicMock(spec=aiohttp.ClientSession),
                _downloader_completo(tmp_path),
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

        assert nota.read_text(encoding="utf-8") == "externo"

    async def test_github_no_borra_archivos_creados_por_un_escritor_externo_durante_la_operacion(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """El hermano GitHub conserva el destino ajeno y descarta sólo su payload temporal."""
        destino = mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME
        nota = destino / "creado-por-mo2.txt"
        extract_real = installer._extract

        def _extract_con_carrera(archive: pathlib.Path, dest: pathlib.Path) -> None:
            if archive.name == _CS_ASSET_CORE:
                destino.mkdir(parents=True, exist_ok=True)
                nota.write_text("externo", encoding="utf-8")
                raise ToolInstallError("fallo simulado en GitHub")
            extract_real(archive, dest)

        installer._extract = _extract_con_carrera  # type: ignore[method-assign]
        _mock_gateway_github_cs(installer, _ZIP_CS)
        _aprobar_todo(installer)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        with pytest.raises(ToolInstallError, match="fallo simulado en GitHub"):
            await installer.ensure_community_shaders(
                mods_dir,
                MagicMock(spec=aiohttp.ClientSession),
                _downloader_completo(tmp_path),
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

        assert nota.read_text(encoding="utf-8") == "externo"

    async def test_denegacion_hitl_no_borra_mod_dir_preexistente(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Denegación: no se escribió nada; un dir ajeno con contenido debe sobrevivir."""
        dir_ajeno = mods_dir / ti_mod.ADDRESS_LIBRARY_MOD_NAME
        dir_ajeno.mkdir()
        notas = dir_ajeno / "notas.txt"
        notas.write_text("contenido del usuario", encoding="utf-8")

        _hitl_espiado(installer, denegar_call=1)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        with pytest.raises(ToolInstallError):
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                _downloader_completo(tmp_path),
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

        assert notas.read_text(encoding="utf-8") == "contenido del usuario"

    async def test_fallo_post_descarga_no_borra_dir_preexistente(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Fallo de extracción sobre un dir que YA tenía contenido → el contenido sobrevive."""
        dir_existente = mods_dir / ti_mod.ADDRESS_LIBRARY_MOD_NAME
        dir_existente.mkdir()
        notas = dir_existente / "notas.txt"
        notas.write_text("usuario", encoding="utf-8")

        extract_real = installer._extract

        def _extract_parcial(archive: pathlib.Path, dest: pathlib.Path) -> None:
            if dest == dir_existente:
                raise ToolInstallError("fallo simulado de extracción")
            extract_real(archive, dest)

        installer._extract = _extract_parcial  # type: ignore[method-assign]
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        with pytest.raises(ToolInstallError, match="fallo simulado"):
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                downloader,
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

        assert notas.read_text(encoding="utf-8") == "usuario"
        assert not _sentinel(dir_existente, "*.bin")

    async def test_cancelled_error_no_limpia_el_parcial(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """CancelledError pasa de largo el except Exception: el parcial sobrevive (nunca compite con un hilo)."""

        def _extract_que_cancela(archive: pathlib.Path, dest: pathlib.Path) -> None:
            raise asyncio.CancelledError

        installer._extract = _extract_que_cancela  # type: ignore[method-assign]
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)
        game_dir = tmp_path / "game"
        _juego_valido(game_dir)

        with pytest.raises(asyncio.CancelledError):
            await installer.ensure_community_shaders(
                mods_dir,
                session,
                downloader,
                edition=SkyrimEdition.SE,
                game_version="1.5.97",
                game_dir=game_dir,
            )

        # El destino MO2 nunca se publicó. El parcial propio queda en staging:
        # la cancelación no dispara el cleanup de ``except Exception``.
        addrlib_dir = mods_dir / ti_mod.ADDRESS_LIBRARY_MOD_NAME
        assert not addrlib_dir.exists()
        parciales = list((tmp_path / ".skyclaw-dl" / ti_mod.ADDRESS_LIBRARY_MOD_NAME).glob("payload-*"))
        assert len(parciales) == 1

    async def test_unlink_del_archive_lockeado_no_rompe_la_instalacion(
        self,
        installer: ToolsInstaller,
        mods_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """WinError 32 transitorio (handle del subproceso 7z/antivirus en Windows) al
        borrar el archive de staging NO puede tumbar una instalación ya completa.

        Cazado en el smoke E2E premium: el download real de EF (7.6 MB) y la
        extracción con 7z.exe del sistema terminaron bien, pero el unlink del
        archive falló con PermissionError y el except de clean_on_fail borró un
        mod VÁLIDO (el unlink corre antes del verify, así que la invariante del
        sentinel no lo protegía). El staging se reescribe en la próxima corrida:
        el cleanup del archive es basura tolerable, no un gate.
        """
        real_unlink = pathlib.Path.unlink

        def _unlink_lockeado_en_staging(self: pathlib.Path, *args: object, **kwargs: object) -> None:
            if ".skyclaw-dl" in str(self) and self.suffix.lower() in {".zip", ".7z"}:
                raise PermissionError(32, "archivo en uso por otro proceso")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "unlink", _unlink_lockeado_en_staging)

        game_dir = tmp_path / "game"
        _juego_valido(game_dir)
        _mock_gateway_github_cs(installer, _ZIP_CS)
        _aprobar_todo(installer)
        downloader = _downloader_completo(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_community_shaders(
            mods_dir,
            session,
            downloader,
            edition=SkyrimEdition.SE,
            game_version="1.5.97",
            game_dir=game_dir,
        )

        # El flujo completó y el mod con payload válido quedó instalado.
        assert all(r.already_existed is False for r in resultados)
        assert _sentinel(mods_dir / ti_mod.SSE_ENGINE_FIXES_MOD_NAME, "EngineFixes.dll")
        assert _sentinel(mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME, "CommunityShaders.dll")
        assert (mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME / "Shaders").is_dir()
        # El zip stale (unlink fallido) quedó en el staging, NUNCA dentro del mod:
        # ni visible en MO2, ni bloquea el flatten, ni envenena preexistia.
        # Desde F4 el staging se keyea por ``mod_dir.name`` (la identidad del
        # lock de instalación), no por el request_slug.
        assert list((mods_dir / ti_mod.COMMUNITY_SHADERS_MOD_NAME).rglob("*.zip")) == []
        assert list((mods_dir / ti_mod.SSE_ENGINE_FIXES_MOD_NAME).rglob("*.zip")) == []
        stale = tmp_path / ".skyclaw-dl" / ti_mod.COMMUNITY_SHADERS_MOD_NAME / _CS_ASSET_CORE
        assert stale.exists(), "el zip stale debe quedar en el staging, fuera del mod"


# ---------------------------------------------------------------------------
# setup_tools — rama "community_shaders" (espejo de la rama "ngio")
# ---------------------------------------------------------------------------


class TestSetupToolsCommunityShaders:
    async def test_setup_tools_rama_community_shaders_persiste_mods_y_detecta_version(
        self, gateway: NetworkGateway, tmp_path: pathlib.Path
    ) -> None:
        """setup_tools('community_shaders') detecta edición+versión, delega y persiste los mods."""
        from sky_claw.app.agent.tools.external_tools import setup_tools
        from sky_claw.local.local_config import LocalConfig
        from sky_claw.local.tools_installer import ModInstallResult

        mo2_root = tmp_path / "MO2"
        (mo2_root / "mods").mkdir(parents=True)
        skyrim_dir = tmp_path / "Skyrim"
        skyrim_dir.mkdir()
        (skyrim_dir / "SkyrimSE.exe").write_text("no-es-un-pe-real", encoding="utf-8")

        cfg = LocalConfig(mo2_root=str(mo2_root), skyrim_path=str(skyrim_dir))
        config_path = tmp_path / "sky_claw_config.json"

        mods_resultado = [
            ModInstallResult(
                mod_name=ti_mod.ADDRESS_LIBRARY_MOD_NAME,
                mod_dir=mo2_root / "mods" / ti_mod.ADDRESS_LIBRARY_MOD_NAME,
                version="v11",
                already_existed=False,
            ),
            ModInstallResult(
                mod_name=ti_mod.SSE_ENGINE_FIXES_MOD_NAME,
                mod_dir=mo2_root / "mods" / ti_mod.SSE_ENGINE_FIXES_MOD_NAME,
                version="7.0.20",
                already_existed=False,
            ),
            ModInstallResult(
                mod_name=ti_mod.COMMUNITY_SHADERS_MOD_NAME,
                mod_dir=mo2_root / "mods" / ti_mod.COMMUNITY_SHADERS_MOD_NAME,
                version="v1.8.2",
                already_existed=False,
            ),
        ]
        mock_installer = MagicMock()
        mock_installer.ensure_community_shaders = AsyncMock(return_value=mods_resultado)

        with (
            patch("sky_claw.local.discovery.scanner.detect_skyrim_edition", return_value=SkyrimEdition.SE),
            patch("sky_claw.local.discovery.scanner.read_skyrim_version", return_value="1.5.97"),
        ):
            salida = await setup_tools(
                mock_installer,
                tmp_path / "tools",
                cfg,
                config_path,
                MagicMock(),
                tools=["community_shaders"],
                gateway=gateway,
                session=MagicMock(spec=aiohttp.ClientSession),
            )

        resultado = json.loads(salida)["community_shaders"]
        assert resultado["status"] == "installed"
        assert resultado["edition"] == SkyrimEdition.SE.value
        assert resultado["game_version"] == "1.5.97"
        assert resultado["mods"] == [
            ti_mod.ADDRESS_LIBRARY_MOD_NAME,
            ti_mod.SSE_ENGINE_FIXES_MOD_NAME,
            ti_mod.COMMUNITY_SHADERS_MOD_NAME,
        ]
        # ensure_community_shaders recibió mods/, edición, versión y game_dir del host.
        llamada = mock_installer.ensure_community_shaders.await_args
        assert llamada.args[0] == mo2_root / "mods"
        assert llamada.kwargs["edition"] is SkyrimEdition.SE
        assert llamada.kwargs["game_version"] == "1.5.97"
        assert llamada.kwargs["game_dir"] == skyrim_dir
        # Persistencia: en el dataclass y en el JSON guardado.
        assert cfg.community_shaders_mods == [
            ti_mod.ADDRESS_LIBRARY_MOD_NAME,
            ti_mod.SSE_ENGINE_FIXES_MOD_NAME,
            ti_mod.COMMUNITY_SHADERS_MOD_NAME,
        ]
        persistido = json.loads(config_path.read_text(encoding="utf-8"))
        assert persistido["community_shaders_mods"] == [
            ti_mod.ADDRESS_LIBRARY_MOD_NAME,
            ti_mod.SSE_ENGINE_FIXES_MOD_NAME,
            ti_mod.COMMUNITY_SHADERS_MOD_NAME,
        ]

    async def test_setup_tools_persiste_con_el_config_real_que_inyecta_app_context(
        self, gateway: NetworkGateway, tmp_path: pathlib.Path
    ) -> None:
        """El objeto que llega en producción es `sky_claw.config.Config`, no `LocalConfig`.

        `AppContext` construye `Config(config_path)` (TOML, estado en `_data`) y
        lo inyecta en el registry del agente. El test hermano de arriba usa
        `LocalConfig` —el formato legacy— y por eso pasaba en verde mientras el
        camino real ni escribía el campo donde se guarda ni sabía persistirlo.
        """
        import tomllib

        from sky_claw.app.agent.tools.external_tools import setup_tools
        from sky_claw.config import Config
        from sky_claw.local.tools_installer import ModInstallResult

        mo2_root = tmp_path / "MO2"
        (mo2_root / "mods").mkdir(parents=True)
        skyrim_dir = tmp_path / "Skyrim"
        skyrim_dir.mkdir()
        (skyrim_dir / "SkyrimSE.exe").write_text("no-es-un-pe-real", encoding="utf-8")

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            f'llm_provider = "anthropic"\nmo2_root = "{mo2_root.as_posix()}"\nskyrim_path = "{skyrim_dir.as_posix()}"\n',
            encoding="utf-8",
        )
        cfg = Config(config_path)

        mods_resultado = [
            ModInstallResult(
                mod_name=ti_mod.COMMUNITY_SHADERS_MOD_NAME,
                mod_dir=mo2_root / "mods" / ti_mod.COMMUNITY_SHADERS_MOD_NAME,
                version="v1.8.2",
                already_existed=False,
            ),
        ]
        mock_installer = MagicMock()
        mock_installer.ensure_community_shaders = AsyncMock(return_value=mods_resultado)

        with (
            patch("sky_claw.local.discovery.scanner.detect_skyrim_edition", return_value=SkyrimEdition.SE),
            patch("sky_claw.local.discovery.scanner.read_skyrim_version", return_value="1.5.97"),
        ):
            salida = await setup_tools(
                mock_installer,
                tmp_path / "tools",
                cfg,
                config_path,
                MagicMock(),
                tools=["community_shaders"],
                gateway=gateway,
                session=MagicMock(spec=aiohttp.ClientSession),
            )

        # No hubo excepción y el resultado llegó entero (antes el guardado
        # levantaba TypeError y se perdía todo el JSON de salida).
        assert json.loads(salida)["community_shaders"]["mods"] == [ti_mod.COMMUNITY_SHADERS_MOD_NAME]
        persistido = tomllib.loads(config_path.read_text(encoding="utf-8"))
        assert persistido["community_shaders_mods"] == [ti_mod.COMMUNITY_SHADERS_MOD_NAME]
        # Y el TOML sigue siendo TOML, con lo que ya tenía.
        assert persistido["mo2_root"] == mo2_root.as_posix()
        assert persistido["llm_provider"] == "anthropic"

    async def test_setup_tools_community_shaders_sin_mo2_root_devuelve_error(
        self, gateway: NetworkGateway, tmp_path: pathlib.Path
    ) -> None:
        """Sin mo2_root la rama devuelve el contrato canónico con mensaje accionable."""
        from sky_claw.app.agent.tools.external_tools import setup_tools
        from sky_claw.local.local_config import LocalConfig
        from sky_claw.local.tools.tool_result import normalize_tool_result

        cfg = LocalConfig(mo2_root=None, skyrim_path=None)
        mock_installer = MagicMock()
        mock_installer.ensure_community_shaders = AsyncMock()

        salida = await setup_tools(
            mock_installer,
            tmp_path / "tools",
            cfg,
            tmp_path / "cfg.json",
            None,
            tools=["community_shaders"],
            gateway=gateway,
            session=MagicMock(spec=aiohttp.ClientSession),
        )

        entrada = json.loads(salida)["community_shaders"]
        assert entrada["success"] is False
        assert entrada["message"]
        assert set(entrada) == {"success", "message"}
        normalizado = normalize_tool_result(entrada)
        assert normalizado["success"] is False
        assert normalizado["message"]
        assert normalizado["message"] != "error desconocido"
        assert mock_installer.ensure_community_shaders.await_count == 0

    def test_community_shaders_no_aparece_en_defaults(self) -> None:
        """Diferencia deliberada con ngio: CS es opt-in (sus prerrequisitos no los satisface Sky-Claw solo)."""
        from sky_claw.app.agent.tools.schemas import SetupToolsParams

        assert "community_shaders" not in SetupToolsParams().tools
        assert "ngio" in SetupToolsParams().tools


# ---------------------------------------------------------------------------
# scanner — caso especial de detección por sentinel del mod bajo mo2_root/mods
# ---------------------------------------------------------------------------


class TestScannerCommunityShaders:
    async def test_scanner_detecta_cs_por_sentinel_del_mod(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con el sentinel físico presente, has_tool('community_shaders') es True.

        El VFS de MO2 no es visible para el scanner: la detección es por el
        archivo real bajo mo2_root/mods — mismo criterio que `_existing_mod_result`.
        """
        from sky_claw.local.discovery.scanner import EnvironmentScanner

        mo2_root = tmp_path / "MO2"
        sentinel_dir = mo2_root / "mods" / ti_mod.COMMUNITY_SHADERS_MOD_NAME / "SKSE" / "Plugins"
        sentinel_dir.mkdir(parents=True)
        (sentinel_dir / "CommunityShaders.dll").write_bytes(b"MZ")
        # CS válido exige también Shaders/ (mismo criterio que el instalador).
        (mo2_root / "mods" / ti_mod.COMMUNITY_SHADERS_MOD_NAME / "Shaders").mkdir()

        async def mock_find_skyrim(*_a: object, **_k: object) -> None:
            return None

        async def mock_find_mo2(*_a: object, **_k: object) -> pathlib.Path:
            return mo2_root

        monkeypatch.setattr(EnvironmentScanner, "_find_skyrim", mock_find_skyrim)
        monkeypatch.setattr(EnvironmentScanner, "_find_mo2", mock_find_mo2)

        scanner = EnvironmentScanner(tool_paths={"loot": str(tmp_path / "loot.exe")})
        snap = await scanner.scan()

        assert snap.has_tool("community_shaders")

    async def test_scanner_sin_cs_lo_marca_missing(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin el sentinel, la tarjeta queda en 'missing' con el botón Instalar."""
        from sky_claw.local.discovery.scanner import EnvironmentScanner

        mo2_root = tmp_path / "MO2"
        (mo2_root / "mods").mkdir(parents=True)

        async def mock_find_skyrim(*_a: object, **_k: object) -> None:
            return None

        async def mock_find_mo2(*_a: object, **_k: object) -> pathlib.Path:
            return mo2_root

        monkeypatch.setattr(EnvironmentScanner, "_find_skyrim", mock_find_skyrim)
        monkeypatch.setattr(EnvironmentScanner, "_find_mo2", mock_find_mo2)

        scanner = EnvironmentScanner(tool_paths={"loot": str(tmp_path / "loot.exe")})
        snap = await scanner.scan()

        assert not snap.has_tool("community_shaders")
        assert any(m.technical_name == "community_shaders" for m in snap.missing)

    async def test_scanner_cs_roto_sin_shaders_no_es_instalado(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DLL presente pero sin Shaders/ (bug real CS 1.8.0): la GUI NO muestra
        'Instalado' — conserva la acción de reparación (mismo criterio que el
        instalador, hallazgo de review del PR #442)."""
        from sky_claw.local.discovery.scanner import EnvironmentScanner

        mo2_root = tmp_path / "MO2"
        plugins = mo2_root / "mods" / ti_mod.COMMUNITY_SHADERS_MOD_NAME / "SKSE" / "Plugins"
        plugins.mkdir(parents=True)
        (plugins / "CommunityShaders.dll").write_bytes(b"MZ")
        assert not (plugins.parent.parent / "Shaders").exists()

        async def mock_find_skyrim(*_a: object, **_k: object) -> None:
            return None

        async def mock_find_mo2(*_a: object, **_k: object) -> pathlib.Path:
            return mo2_root

        monkeypatch.setattr(EnvironmentScanner, "_find_skyrim", mock_find_skyrim)
        monkeypatch.setattr(EnvironmentScanner, "_find_mo2", mock_find_mo2)

        scanner = EnvironmentScanner(tool_paths={"loot": str(tmp_path / "loot.exe")})
        snap = await scanner.scan()

        assert not snap.has_tool("community_shaders")
        assert any(m.technical_name == "community_shaders" for m in snap.missing)

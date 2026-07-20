"""Tests de ``ToolsInstaller.ensure_ngio`` — instalación de dependencias NGIO (PR-6 grass cache).

Cubre la instalación como mods de MO2 de:
- No Grass In Objects NG (GitHub, DwemerEngineer/No-Grass-In-Objects-NG),
- Address Library for SKSE Plugins (Nexus 32444, file según edición SE/AE),
- Grass Cache Helper NG (Nexus 101095, obligatorio SOLO en AE — SOP §2.8).

Convenciones verificadas: HITL obligatorio con category="download", extracción
anti path-traversal, idempotencia por sentinel ``SKSE/Plugins/*.dll|*.bin``,
pin opcional de SHA-256 por nombre de asset, y contrato de ``setup_tools``
(entradas ``status``/``error`` que ``normalize_tool_result`` entiende).
"""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from sky_claw.antigravity.scraper.nexus_downloader import FileInfo
from sky_claw.antigravity.security.hitl import Decision, HITLGuard
from sky_claw.antigravity.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError
from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.tools_installer import (
    ADDRESS_LIBRARY_MOD_NAME,
    GRASS_CACHE_HELPER_MOD_NAME,
    NGIO_MOD_NAME,
    ToolInstallError,
    ToolsInstaller,
    _select_address_library_file,
    _select_ngio_asset,
)

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
    return ToolsInstaller(hitl=hitl_guard, gateway=gateway, path_validator=validator)


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


def _zip_bytes_malicioso() -> bytes:
    """Zip con una entrada de path traversal (``../evil.dll``)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(zipfile.ZipInfo("../evil.dll"), "payload-malicioso")
    return buf.getvalue()


# Nombre real del build SE/NG (los assets de NGIO empiezan con "NGIO-"; ver
# github.com/DwemerEngineer/No-Grass-In-Objects-NG/releases). Se usa .zip (no
# .7z) para que la extracción de los tests funcione sin py7zr.
_NGIO_ASSET = "NGIO-NG_1.0.13.zip"
# Assets reales de una release de NGIO (nombres verificados contra la release
# viva 1.0.13): AE (1.6.x), NG (1.5.97/SE) y VR (siempre excluido).
_NGIO_ASSET_AE = "NGIO-AE-1.0.13-1.6.640.zip"
_NGIO_ASSET_VR = "NGIO-NG-1.0.13-VR.zip"

_ZIP_NGIO = _zip_bytes({"SKSE/Plugins/GrassControl.dll": "fake-dll", "SKSE/Plugins/GrassControl.ini": "cfg"})
_ZIP_ADDRLIB_SE = _zip_bytes({"SKSE/Plugins/version-1-5-97-0.bin": "fake-bin"})
_ZIP_ADDRLIB_AE = _zip_bytes({"SKSE/Plugins/version-1-6-1170-0.bin": "fake-bin"})
_ZIP_GCH = _zip_bytes({"SKSE/Plugins/GrassCacheHelperNG.dll": "fake-dll"})

# Files de Nexus 32444 tal como los sirve files.json (SE y AE separados).
_ADDRLIB_FILES: list[dict[str, Any]] = [
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
    {
        "file_id": 111,
        "name": "All in one (Special Edition)",
        "file_name": "vieja.zip",
        "category_name": "OLD_VERSION",
    },
]


def _ngio_asset_entry(asset_name: str, size: int) -> dict[str, Any]:
    return {
        "name": asset_name,
        "size": size,
        "url": f"https://api.github.com/repos/DwemerEngineer/No-Grass-In-Objects-NG/releases/assets/{hash(asset_name) & 0xFFFF}",
        "browser_download_url": (
            f"https://github.com/DwemerEngineer/No-Grass-In-Objects-NG/releases/download/1.0.13/{asset_name}"
        ),
    }


def _ngio_release_json(tag: str = "1.0.13", asset_name: str = _NGIO_ASSET, size: int = 0) -> dict[str, Any]:
    """Release de NGIO con los TRES builds reales (AE / NG-SE / VR).

    ``asset_name``/``size`` describen el build que efectivamente se descarga
    (el que la selección por edición elige); los otros dos existen para que la
    selección se ejercite de verdad. VR debe quedar siempre afuera.
    """
    otros = {_NGIO_ASSET, _NGIO_ASSET_AE, _NGIO_ASSET_VR} - {asset_name}
    return {
        "tag_name": tag,
        "assets": [_ngio_asset_entry(asset_name, size), *(_ngio_asset_entry(n, size) for n in sorted(otros))],
    }


def _mock_gateway_github(installer: ToolsInstaller, zip_payload: bytes, asset_name: str = _NGIO_ASSET) -> None:
    """Encadena las dos respuestas del gateway: metadata del release y stream binario."""
    release_json = _ngio_release_json(asset_name=asset_name, size=len(zip_payload))

    mock_api_resp = AsyncMock()
    mock_api_resp.status = 200
    mock_api_resp.json = AsyncMock(return_value=release_json)
    mock_api_resp.release = MagicMock()

    async def _iter_chunks(size: int):
        yield zip_payload

    mock_dl_resp = AsyncMock()
    mock_dl_resp.status = 200
    mock_dl_resp.raise_for_status = MagicMock()
    mock_dl_resp.release = MagicMock()
    mock_dl_resp.content = MagicMock()
    mock_dl_resp.content.iter_chunked = _iter_chunks

    installer._gateway.request = AsyncMock(side_effect=[mock_api_resp, mock_dl_resp])  # type: ignore[method-assign]


def _fake_downloader(staging: pathlib.Path, archives: dict[int, tuple[str, bytes]]) -> MagicMock:
    """NexusDownloader falso: sirve zips reales desde ``archives`` (nexus_id → (file_name, bytes))."""
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

    dl.list_files = AsyncMock(return_value=list(_ADDRLIB_FILES))
    dl.get_file_info = AsyncMock(side_effect=_get_file_info)
    dl.download = AsyncMock(side_effect=_download)
    return dl


def _downloader_se(tmp_path: pathlib.Path) -> MagicMock:
    return _fake_downloader(
        tmp_path / "staging",
        {32444: ("All.in.one.Special.Edition.zip", _ZIP_ADDRLIB_SE)},
    )


def _downloader_ae(tmp_path: pathlib.Path) -> MagicMock:
    return _fake_downloader(
        tmp_path / "staging",
        {
            32444: ("All.in.one.Anniversary.Edition.zip", _ZIP_ADDRLIB_AE),
            101095: ("GrassCacheHelperNG.zip", _ZIP_GCH),
        },
    )


def _aprobar_todo(installer: ToolsInstaller) -> AsyncMock:
    mock = AsyncMock(return_value=Decision.APPROVED)
    installer._hitl.request_approval = mock  # type: ignore[method-assign]
    return mock


def _sentinel(mod_dir: pathlib.Path, patron: str) -> bool:
    return any((mod_dir / "SKSE" / "Plugins").glob(patron))


# ---------------------------------------------------------------------------
# _select_address_library_file (función pura)
# ---------------------------------------------------------------------------


class TestSeleccionAddressLibrary:
    @pytest.mark.parametrize(
        ("edition", "esperado"),
        [(SkyrimEdition.SE, 493038), (SkyrimEdition.AE, 493039)],
    )
    def test_seleccion_address_library_por_edicion(self, edition: SkyrimEdition, esperado: int) -> None:
        """Elige el file_id MAIN cuyo nombre matchea 'Special'/'Anniversary' según la edición."""
        assert _select_address_library_file(_ADDRLIB_FILES, edition) == esperado

    def test_seleccion_tolera_file_id_como_lista(self) -> None:
        """Nexus a veces sirve file_id como lista [id, algo]: se toma el segundo elemento (quirk de get_file_info)."""
        files = [
            {
                "file_id": [493038, 2295],
                "name": "All in one (Special Edition)",
                "file_name": "x.zip",
                "category_name": "MAIN",
            },
        ]
        assert _select_address_library_file(files, SkyrimEdition.SE) == 2295

    def test_seleccion_address_library_sin_match_lanza_error(self) -> None:
        """Sin file que matchee la edición, ToolInstallError lista los nombres disponibles."""
        files = [
            {"file_id": 1, "name": "Otra cosa", "file_name": "otra.zip", "category_name": "MAIN"},
        ]
        with pytest.raises(ToolInstallError) as exc_info:
            _select_address_library_file(files, SkyrimEdition.AE)
        assert "Otra cosa" in str(exc_info.value)


class TestSeleccionNgioAsset:
    """Selección del build de NGIO-NG (GitHub) por edición — los assets reales
    son NGIO-AE-* (1.6.x), NGIO-NG_* (1.5.97/SE) y NGIO-NG-*-VR (excluido).
    Con ``keyword`` genérico el instalador bajaría el build equivocado (o
    ninguno): esta selección es la que arregla el bug del #293."""

    _ASSETS = [
        {"name": "NGIO-AE-1.0.13-1.6.640.zip"},
        {"name": "NGIO-NG_1.0.13.1.7z"},
        {"name": "NGIO-NG-1.0.13-VR.zip"},
    ]

    def test_ae_elige_el_build_ae(self) -> None:
        """En Anniversary Edition se elige el asset NGIO-AE (1.6.x), no el NG ni el VR."""
        assert _select_ngio_asset(self._ASSETS, SkyrimEdition.AE)["name"] == "NGIO-AE-1.0.13-1.6.640.zip"

    def test_se_elige_el_build_ng_no_ae(self) -> None:
        """En Special Edition se elige el NGIO-NG (1.5.97), nunca el marcado AE."""
        assert _select_ngio_asset(self._ASSETS, SkyrimEdition.SE)["name"] == "NGIO-NG_1.0.13.1.7z"

    def test_vr_siempre_excluido(self) -> None:
        """VR jamás se elige: con solo AE+VR disponibles, SE no cae al VR — falla explícito."""
        solo_ae_y_vr = [{"name": "NGIO-AE-1.0.13.zip"}, {"name": "NGIO-NG-1.0.13-VR.zip"}]
        with pytest.raises(ToolInstallError):
            _select_ngio_asset(solo_ae_y_vr, SkyrimEdition.SE)

    def test_sin_match_lista_disponibles(self) -> None:
        """Si ningún asset es de NGIO, ToolInstallError lista lo disponible (diagnóstico, no keyword ciego)."""
        with pytest.raises(ToolInstallError) as exc_info:
            _select_ngio_asset([{"name": "otracosa.zip"}], SkyrimEdition.AE)
        assert "otracosa.zip" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ensure_ngio — validaciones previas (sin red)
# ---------------------------------------------------------------------------


class TestEnsureNgioValidaciones:
    @pytest.mark.parametrize("edition", [SkyrimEdition.LE, SkyrimEdition.UNKNOWN])
    async def test_edicion_le_o_unknown_rechazada_sin_red(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path, edition: SkyrimEdition
    ) -> None:
        """LE y UNKNOWN abortan antes de tocar la red: ni gateway ni downloader se invocan."""
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError):
            await installer.ensure_ngio(mods_dir, session, downloader, edition=edition)

        assert installer._gateway.request.await_count == 0
        assert downloader.get_file_info.await_count == 0
        assert downloader.download.await_count == 0

    async def test_sin_downloader_falla_con_mensaje_claro(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path
    ) -> None:
        """Address Library viene de Nexus: sin NexusDownloader, ToolInstallError explícito."""
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_ngio(mods_dir, session, None, edition=SkyrimEdition.SE)

        assert "NexusDownloader" in str(exc_info.value)
        assert installer._gateway.request.await_count == 0

    async def test_hitl_denegado_no_descarga_nada(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """DENY del operador en el primer componente: nada se descarga ni se extrae."""
        _mock_gateway_github(installer, _ZIP_NGIO)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.DENIED)  # type: ignore[method-assign]
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError):
            await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        # La aprobación fue pedida con la categoría que la GUI parquea en el modal.
        assert installer._hitl.request_approval.call_args.kwargs["category"] == "download"
        # Solo se pidió la metadata del release (1 llamada), nunca el binario.
        assert installer._gateway.request.await_count == 1
        assert downloader.download.await_count == 0
        assert list(mods_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# ensure_ngio — instalación por edición
# ---------------------------------------------------------------------------


class TestEnsureNgioInstalacion:
    async def test_instala_ngio_y_address_library_en_se(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Happy path SE: 2 mods creados con meta.ini y payload SKSE; el helper AE NO se pide."""
        _mock_gateway_github(installer, _ZIP_NGIO)
        _aprobar_todo(installer)
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        assert [r.mod_name for r in resultados] == [NGIO_MOD_NAME, ADDRESS_LIBRARY_MOD_NAME]
        assert all(r.already_existed is False for r in resultados)
        for r in resultados:
            assert (r.mod_dir / "meta.ini").is_file()
        assert _sentinel(mods_dir / NGIO_MOD_NAME, "*.dll")
        assert _sentinel(mods_dir / ADDRESS_LIBRARY_MOD_NAME, "*.bin")
        # El file de Address Library se eligió por edición (SE) vía list_files.
        downloader.list_files.assert_awaited_once()
        assert downloader.get_file_info.await_args_list[0].args[0:2] == (32444, 493038)
        # Grass Cache Helper NG (101095) jamás se consulta en SE.
        assert all(call.args[0] != 101095 for call in downloader.get_file_info.await_args_list)
        assert not (mods_dir / GRASS_CACHE_HELPER_MOD_NAME).exists()

    async def test_se_baja_el_build_ng_no_el_ae_ni_vr(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Con los 3 builds en la release, SE aprueba/baja el NGIO-NG (no el AE ni el VR)."""
        _mock_gateway_github(installer, _ZIP_NGIO)
        hitl = _aprobar_todo(installer)
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        # La primera aprobación HITL es la de NGIO; su detalle nombra el asset elegido.
        detalle_ngio = hitl.await_args_list[0].kwargs["detail"]
        assert _NGIO_ASSET in detalle_ngio
        assert "-AE-" not in detalle_ngio and "-VR" not in detalle_ngio

    async def test_ae_baja_el_build_ae(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """En AE se aprueba/baja el build NGIO-AE, no el NG-SE."""
        _mock_gateway_github(installer, _ZIP_NGIO)
        hitl = _aprobar_todo(installer)
        downloader = _downloader_ae(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.AE)

        detalle_ngio = hitl.await_args_list[0].kwargs["detail"]
        assert _NGIO_ASSET_AE in detalle_ngio

    async def test_ae_exige_grass_cache_helper(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """En AE se instala además Grass Cache Helper NG (Nexus 101095) con file primary."""
        _mock_gateway_github(installer, _ZIP_NGIO)
        _aprobar_todo(installer)
        downloader = _downloader_ae(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.AE)

        assert [r.mod_name for r in resultados] == [
            NGIO_MOD_NAME,
            ADDRESS_LIBRARY_MOD_NAME,
            GRASS_CACHE_HELPER_MOD_NAME,
        ]
        assert _sentinel(mods_dir / GRASS_CACHE_HELPER_MOD_NAME, "*.dll")
        # El helper se pide sin selección por edición (file primary de Nexus).
        llamadas_gch = [c for c in downloader.get_file_info.await_args_list if c.args[0] == 101095]
        assert len(llamadas_gch) == 1
        assert llamadas_gch[0].args[1] is None
        # Y el Address Library elegido es el de AE.
        assert downloader.get_file_info.await_args_list[0].args[0:2] == (32444, 493039)

    async def test_idempotente_si_mods_ya_instalados(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Mods preexistentes con sentinel: already_existed=True, sin HITL ni red."""
        for nombre, payload in (
            (NGIO_MOD_NAME, "GrassControl.dll"),
            (ADDRESS_LIBRARY_MOD_NAME, "version-1-5-97-0.bin"),
        ):
            plugins = mods_dir / nombre / "SKSE" / "Plugins"
            plugins.mkdir(parents=True)
            (plugins / payload).write_text("fake", encoding="utf-8")

        hitl_mock = _aprobar_todo(installer)
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        assert all(r.already_existed is True for r in resultados)
        assert all(r.version == "existing" for r in resultados)
        assert hitl_mock.await_count == 0
        assert installer._gateway.request.await_count == 0
        assert downloader.download.await_count == 0


# ---------------------------------------------------------------------------
# Pin de SHA-256
# ---------------------------------------------------------------------------


class TestPinSha256:
    async def test_pin_sha256_mismatch_aborta_y_borra(
        self,
        installer: ToolsInstaller,
        mods_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pin incorrecto para el asset de GitHub: ToolInstallError, archive borrado, mod sin payload."""
        from sky_claw.local import tools_installer as ti_mod

        monkeypatch.setitem(ti_mod._PINNED_SHA256, _NGIO_ASSET, "0" * 64)
        _mock_gateway_github(installer, _ZIP_NGIO)
        _aprobar_todo(installer)
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        assert "SHA-256" in str(exc_info.value)
        assert list(mods_dir.rglob("*.zip")) == []
        assert not _sentinel(mods_dir / NGIO_MOD_NAME, "*.dll")

    async def test_pin_sha256_correcto_instala(
        self,
        installer: ToolsInstaller,
        mods_dir: pathlib.Path,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pin == sha256 real del zip: la instalación procede normalmente."""
        from sky_claw.local import tools_installer as ti_mod

        monkeypatch.setitem(ti_mod._PINNED_SHA256, _NGIO_ASSET, hashlib.sha256(_ZIP_NGIO).hexdigest())
        _mock_gateway_github(installer, _ZIP_NGIO)
        _aprobar_todo(installer)
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        resultados = await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        assert _sentinel(mods_dir / NGIO_MOD_NAME, "*.dll")
        assert resultados[0].version == "1.0.13"


# ---------------------------------------------------------------------------
# Extracción: traversal, aplanado y layout inesperado
# ---------------------------------------------------------------------------


class TestExtraccionMods:
    async def test_zip_malicioso_rechazado_en_extraccion(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Un zip de Nexus con entrada '../evil.dll' dispara PathViolationError y nada escapa del sandbox."""
        _mock_gateway_github(installer, _ZIP_NGIO)
        _aprobar_todo(installer)
        downloader = _fake_downloader(
            tmp_path / "staging",
            {32444: ("All.in.one.Special.Edition.zip", _zip_bytes_malicioso())},
        )
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(PathViolationError):
            await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        assert not (tmp_path / "evil.dll").exists()
        assert not (mods_dir / "evil.dll").exists()

    async def test_aplana_zip_con_carpeta_raiz(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Archive que envuelve el payload en 'NGIO-1.0.13/': el contenido queda con SKSE/ en la raíz del mod."""
        zip_envuelto = _zip_bytes({"NGIO-1.0.13/SKSE/Plugins/GrassControl.dll": "fake-dll"})
        _mock_gateway_github(installer, zip_envuelto)
        _aprobar_todo(installer)
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        mod_dir = mods_dir / NGIO_MOD_NAME
        assert _sentinel(mod_dir, "*.dll")
        assert not (mod_dir / "NGIO-1.0.13").exists()

    async def test_sentinel_ausente_tras_extraccion_falla(
        self, installer: ToolsInstaller, mods_dir: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        """Zip sin SKSE/Plugins: ToolInstallError (layout inesperado), análogo al 'exe not found'."""
        zip_sin_payload = _zip_bytes({"readme.txt": "sin plugins"})
        _mock_gateway_github(installer, zip_sin_payload)
        _aprobar_todo(installer)
        downloader = _downloader_se(tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError) as exc_info:
            await installer.ensure_ngio(mods_dir, session, downloader, edition=SkyrimEdition.SE)

        assert "SKSE/Plugins" in str(exc_info.value)


# ---------------------------------------------------------------------------
# setup_tools — rama "ngio"
# ---------------------------------------------------------------------------


class TestSetupToolsNgio:
    async def test_setup_tools_rama_ngio_persiste_mods_en_config(
        self, gateway: NetworkGateway, tmp_path: pathlib.Path
    ) -> None:
        """setup_tools('ngio') detecta la edición desde skyrim_path, delega en ensure_ngio y persiste ngio_mods."""
        from sky_claw.antigravity.agent.tools.external_tools import setup_tools
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
                mod_name=NGIO_MOD_NAME,
                mod_dir=mo2_root / "mods" / NGIO_MOD_NAME,
                version="1.0.13",
                already_existed=False,
            ),
            ModInstallResult(
                mod_name=ADDRESS_LIBRARY_MOD_NAME,
                mod_dir=mo2_root / "mods" / ADDRESS_LIBRARY_MOD_NAME,
                version="All.in.one.Special.Edition.zip",
                already_existed=False,
            ),
        ]
        mock_installer = MagicMock()
        mock_installer.ensure_ngio = AsyncMock(return_value=mods_resultado)

        with patch(
            "sky_claw.local.discovery.scanner.detect_skyrim_edition",
            return_value=SkyrimEdition.SE,
        ):
            salida = await setup_tools(
                mock_installer,
                tmp_path / "tools",
                cfg,
                config_path,
                MagicMock(),
                tools=["ngio"],
                gateway=gateway,
                session=MagicMock(spec=aiohttp.ClientSession),
            )

        resultado = json.loads(salida)["ngio"]
        assert resultado["status"] == "installed"
        assert resultado["edition"] == SkyrimEdition.SE.value
        assert resultado["mods"] == [NGIO_MOD_NAME, ADDRESS_LIBRARY_MOD_NAME]
        # ensure_ngio recibió el mods/ del MO2 configurado y la edición detectada.
        llamada = mock_installer.ensure_ngio.await_args
        assert llamada.args[0] == mo2_root / "mods"
        assert llamada.kwargs["edition"] is SkyrimEdition.SE
        # Persistencia: en el dataclass y en el JSON guardado.
        assert cfg.ngio_mods == [NGIO_MOD_NAME, ADDRESS_LIBRARY_MOD_NAME]
        persistido = json.loads(config_path.read_text(encoding="utf-8"))
        assert persistido["ngio_mods"] == [NGIO_MOD_NAME, ADDRESS_LIBRARY_MOD_NAME]

    async def test_setup_tools_ngio_sin_mo2_root_devuelve_error(
        self, gateway: NetworkGateway, tmp_path: pathlib.Path
    ) -> None:
        """Sin mo2_root la rama devuelve {'error': ...} y normalize_tool_result nunca cae en 'error desconocido'."""
        from sky_claw.antigravity.agent.tools.external_tools import setup_tools
        from sky_claw.local.local_config import LocalConfig
        from sky_claw.local.tools.tool_result import normalize_tool_result

        cfg = LocalConfig(mo2_root=None, skyrim_path=None)
        mock_installer = MagicMock()
        mock_installer.ensure_ngio = AsyncMock()

        salida = await setup_tools(
            mock_installer,
            tmp_path / "tools",
            cfg,
            tmp_path / "cfg.json",
            None,
            tools=["ngio"],
            gateway=gateway,
            session=MagicMock(spec=aiohttp.ClientSession),
        )

        entrada = json.loads(salida)["ngio"]
        assert "error" in entrada
        normalizado = normalize_tool_result(entrada)
        assert normalizado["success"] is False
        assert normalizado["message"]
        assert normalizado["message"] != "error desconocido"
        assert mock_installer.ensure_ngio.await_count == 0

    async def test_setup_tools_ngio_aparece_en_defaults(self) -> None:
        """El default de SetupToolsParams incluye 'ngio' (registro LLM-facing consistente)."""
        from sky_claw.antigravity.agent.tools.schemas import SetupToolsParams

        assert "ngio" in SetupToolsParams().tools


# ---------------------------------------------------------------------------
# NexusDownloader.list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    async def test_list_files_devuelve_files_crudos(self, tmp_path: pathlib.Path) -> None:
        """list_files pega a files.json vía gateway.request (F5b) y devuelve la lista cruda."""
        from sky_claw.antigravity.scraper.nexus_downloader import NexusDownloader

        resp = AsyncMock()
        resp.status = 200
        resp.raise_for_status = MagicMock()
        resp.release = MagicMock()
        resp.json = AsyncMock(return_value={"files": list(_ADDRLIB_FILES)})

        gateway = MagicMock()
        gateway.request = AsyncMock(return_value=resp)
        downloader = NexusDownloader(api_key="fake", gateway=gateway, staging_dir=tmp_path / "staging")

        session = MagicMock(spec=aiohttp.ClientSession)

        files = await downloader.list_files(32444, session)

        assert files == _ADDRLIB_FILES
        gateway.request.assert_awaited_once()
        # request(method, url, session, ...) → args[1] es la URL.
        url_pedida = gateway.request.await_args.args[1]
        assert "/mods/32444/files.json" in url_pedida

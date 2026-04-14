"""Tests for sky_claw.scraper.nexus_downloader and the download_mod tool."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from sky_claw.agent.tools import AsyncToolRegistry, DownloadModParams
from sky_claw.db.async_registry import AsyncModRegistry
from sky_claw.mo2.vfs import MO2Controller
from sky_claw.orchestrator.sync_engine import SyncEngine
from sky_claw.scraper.masterlist import MasterlistClient
from sky_claw.scraper.nexus_downloader import (
    DownloadError,
    DownloadProgress,
    FileInfo,
    HashValidationError,
    MD5ValidationError,  # Alias de compatibilidad hacia atrás
    NexusDownloader,
    _cleanup,
    validate_sha256_format,
)
from sky_claw.security.hitl import Decision, HITLGuard
from sky_claw.security.network_gateway import (
    EgressPolicy,
    EgressViolation,
    NetworkGateway,
)
from sky_claw.security.path_validator import PathValidator
from tenacity import wait_none

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _make_gateway() -> NetworkGateway:
    return NetworkGateway(EgressPolicy(block_private_ips=False))


def _make_downloader(
    tmp_path: pathlib.Path,
    gateway: NetworkGateway | None = None,
    chunk_size: int = 1024,
    timeout: int = 60,
) -> NexusDownloader:
    return NexusDownloader(
        api_key="test-api-key",
        gateway=gateway or _make_gateway(),
        staging_dir=tmp_path / "staging",
        chunk_size=chunk_size,
        timeout=timeout,
        file_info_retry_wait=wait_none(),
        download_retry_wait=wait_none(),
    )


def _make_file_info(
    *,
    nexus_id: int = 100,
    file_id: int = 200,
    file_name: str = "TestMod-100-v1.zip",
    size_bytes: int = 1024,
    md5: str = "",
    sha256: str = "",
    download_url: str = "https://premium-files.nexusmods.com/file/100/200",
) -> FileInfo:
    return FileInfo(
        nexus_id=nexus_id,
        file_id=file_id,
        file_name=file_name,
        size_bytes=size_bytes,
        md5=md5,
        sha256=sha256,
        download_url=download_url,
    )


def _md5_of(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _sha256_of(data: bytes) -> str:
    """Helper para calcular SHA256 de bytes."""
    return hashlib.sha256(data).hexdigest()


def _make_aiohttp_response(
    status: int = 200,
    json_data: Any = None,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock aiohttp response suitable for use as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), status=status
        )
    resp.json = AsyncMock(return_value=json_data)

    # Simulate iter_chunked as an async generator.
    async def _iter_chunked(size: int):
        for i in range(0, len(content), size):
            yield content[i : i + size]

    resp.content = MagicMock()
    resp.content.iter_chunked = _iter_chunked

    # Make response work as an async context manager.
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session(*responses: MagicMock) -> AsyncMock:
    """Return a mock ClientSession whose .get() returns responses in order."""
    session = AsyncMock(spec=aiohttp.ClientSession)
    session.get = MagicMock(side_effect=iter(responses))
    return session


@pytest.fixture()
async def adb(tmp_path: pathlib.Path) -> AsyncModRegistry:
    registry = AsyncModRegistry(db_path=tmp_path / "test_nexus.db")
    await registry.open()
    yield registry  # type: ignore[misc]
    await registry.close()


@pytest.fixture()
def mo2(tmp_path: pathlib.Path) -> MO2Controller:
    profile_dir = tmp_path / "profiles" / "Default"
    profile_dir.mkdir(parents=True)
    (profile_dir / "modlist.txt").write_text("+Mod-1001-v1\n", encoding="utf-8")
    validator = PathValidator(roots=[tmp_path])
    return MO2Controller(tmp_path, path_validator=validator)


@pytest.fixture()
async def sync_engine(
    mo2: MO2Controller, adb: AsyncModRegistry
) -> AsyncGenerator[SyncEngine, None]:
    gw = _make_gateway()
    masterlist = MasterlistClient(gateway=gw, api_key="fake")
    engine = SyncEngine(
        mo2=mo2, masterlist=masterlist, registry=adb, fetch_retry_wait=wait_none()
    )
    yield engine
    tasks = list(engine._download_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture()
def downloader(tmp_path: pathlib.Path) -> NexusDownloader:
    return _make_downloader(tmp_path)


@pytest.fixture()
def hitl_guard() -> HITLGuard:
    return HITLGuard(notify_fn=None, timeout=5)


@pytest.fixture()
def tool_registry(
    adb: AsyncModRegistry,
    mo2: MO2Controller,
    sync_engine: SyncEngine,
    downloader: NexusDownloader,
    hitl_guard: HITLGuard,
) -> AsyncToolRegistry:
    return AsyncToolRegistry(
        registry=adb,
        mo2=mo2,
        sync_engine=sync_engine,
        hitl=hitl_guard,
        downloader=downloader,
    )


# ---------------------------------------------------------------------------
# FileInfo
# ---------------------------------------------------------------------------


class TestFileInfo:
    def test_fields_are_stored(self) -> None:
        fi = _make_file_info(
            nexus_id=1, file_id=2, file_name="a.zip", size_bytes=512, md5="abc"
        )
        assert fi.nexus_id == 1
        assert fi.file_id == 2
        assert fi.file_name == "a.zip"
        assert fi.size_bytes == 512
        assert fi.md5 == "abc"

    def test_sha256_field_default_empty(self) -> None:
        """Verifica que sha256 tiene valor por defecto vacío."""
        fi = _make_file_info()
        assert fi.sha256 == ""

    def test_sha256_field_stored(self) -> None:
        """Verifica que sha256 se almacena correctamente."""
        sha256_hash = "a" * 64  # 64 chars hex simulado
        fi = _make_file_info(sha256=sha256_hash)
        assert fi.sha256 == sha256_hash

    def test_frozen(self) -> None:
        fi = _make_file_info()
        with pytest.raises((AttributeError, TypeError)):
            fi.nexus_id = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# validate_sha256_format
# ---------------------------------------------------------------------------


class TestValidateSha256Format:
    """Tests para la función validate_sha256_format."""

    def test_valid_sha256_format(self) -> None:
        """Hash SHA256 válido de 64 caracteres hexadecimales."""
        valid_hash = "a" * 64
        assert validate_sha256_format(valid_hash) is True

    def test_valid_sha256_with_numbers(self) -> None:
        """Hash SHA256 válido con números y letras."""
        valid_hash = "0123456789abcdef" * 4
        assert validate_sha256_format(valid_hash) is True

    def test_valid_sha256_uppercase(self) -> None:
        """Hash SHA256 válido con mayúsculas."""
        valid_hash = "ABCDEF" + "0" * 58
        assert validate_sha256_format(valid_hash) is True

    def test_invalid_too_short(self) -> None:
        """Hash muy corto (menos de 64 chars)."""
        assert validate_sha256_format("a" * 63) is False

    def test_invalid_too_long(self) -> None:
        """Hash muy largo (más de 64 chars)."""
        assert validate_sha256_format("a" * 65) is False

    def test_invalid_empty(self) -> None:
        """Hash vacío."""
        assert validate_sha256_format("") is False

    def test_invalid_non_hex_chars(self) -> None:
        """Hash con caracteres no hexadecimales."""
        invalid_hash = "g" * 64  # 'g' no es hex válido
        assert validate_sha256_format(invalid_hash) is False

    def test_invalid_with_spaces(self) -> None:
        """Hash con espacios."""
        invalid_hash = "a" * 32 + " " + "a" * 32
        assert validate_sha256_format(invalid_hash) is False


# ---------------------------------------------------------------------------
# DownloadProgress
# ---------------------------------------------------------------------------


class TestDownloadProgress:
    def test_percent_zero_when_total_is_zero(self) -> None:
        p = DownloadProgress(file_name="f.zip", total_bytes=0)
        assert p.percent == 0.0

    def test_percent_half(self) -> None:
        p = DownloadProgress(file_name="f.zip", total_bytes=200, downloaded_bytes=100)
        assert p.percent == 50.0

    def test_percent_full(self) -> None:
        p = DownloadProgress(file_name="f.zip", total_bytes=100, downloaded_bytes=100)
        assert p.percent == 100.0

    def test_percent_capped_at_100(self) -> None:
        p = DownloadProgress(file_name="f.zip", total_bytes=100, downloaded_bytes=150)
        assert p.percent == 100.0

    def test_downloaded_bytes_default(self) -> None:
        p = DownloadProgress(file_name="f.zip", total_bytes=500)
        assert p.downloaded_bytes == 0


# ---------------------------------------------------------------------------
# NexusDownloader — construction & properties
# ---------------------------------------------------------------------------


class TestNexusDownloaderProperties:
    def test_staging_dir_property(self, tmp_path: pathlib.Path) -> None:
        d = _make_downloader(tmp_path)
        assert d.staging_dir == tmp_path / "staging"

    def test_timeout_property(self, tmp_path: pathlib.Path) -> None:
        d = NexusDownloader(
            api_key="k",
            gateway=_make_gateway(),
            staging_dir=tmp_path,
            timeout=120,
        )
        assert d.timeout == 120

    def test_custom_chunk_size(self, tmp_path: pathlib.Path) -> None:
        d = _make_downloader(tmp_path, chunk_size=512 * 1024)
        assert d._chunk_size == 512 * 1024


# ---------------------------------------------------------------------------
# NexusDownloader.get_file_info
# ---------------------------------------------------------------------------


class TestGetFileInfo:
    @pytest.mark.asyncio
    async def test_success(self, tmp_path: pathlib.Path) -> None:
        meta_resp = _make_aiohttp_response(
            json_data={
                "file_name": "mod.zip",
                "size_in_bytes": 2048,
                "md5": "deadbeef",
            }
        )
        link_resp = _make_aiohttp_response(
            json_data=[{"URI": "https://premium-files.nexusmods.com/f/mod.zip"}]
        )
        session = _make_session(meta_resp, link_resp)
        d = _make_downloader(tmp_path)
        info = await d.get_file_info(42, 7, session)

        assert info.nexus_id == 42
        assert info.file_id == 7
        assert info.file_name == "mod.zip"
        assert info.size_bytes == 2048
        assert info.md5 == "deadbeef"
        assert "premium-files.nexusmods.com" in info.download_url

    @pytest.mark.asyncio
    async def test_size_falls_back_to_kb_field(self, tmp_path: pathlib.Path) -> None:
        meta_resp = _make_aiohttp_response(
            json_data={"file_name": "mod.zip", "size": 5, "md5": ""}
        )
        link_resp = _make_aiohttp_response(
            json_data=[{"URI": "https://premium-files.nexusmods.com/f/mod.zip"}]
        )
        session = _make_session(meta_resp, link_resp)
        d = _make_downloader(tmp_path)
        info = await d.get_file_info(1, 1, session)
        assert info.size_bytes == 5 * 1024

    @pytest.mark.asyncio
    async def test_401_raises_download_error(self, tmp_path: pathlib.Path) -> None:
        resp = _make_aiohttp_response(status=401)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        with pytest.raises(DownloadError, match="Invalid or missing"):
            await d.get_file_info(1, 1, session)

    @pytest.mark.asyncio
    async def test_403_raises_download_error(self, tmp_path: pathlib.Path) -> None:
        resp = _make_aiohttp_response(status=403)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        with pytest.raises(DownloadError, match="premium"):
            await d.get_file_info(1, 1, session)

    @pytest.mark.asyncio
    async def test_404_raises_download_error(self, tmp_path: pathlib.Path) -> None:
        resp = _make_aiohttp_response(status=404)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        with pytest.raises(DownloadError, match="not found"):
            await d.get_file_info(1, 1, session)

    @pytest.mark.asyncio
    async def test_gateway_authorize_called(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway()
        meta_resp = _make_aiohttp_response(
            json_data={"file_name": "f.zip", "size_in_bytes": 0, "md5": ""}
        )
        link_resp = _make_aiohttp_response(
            json_data=[{"URI": "https://premium-files.nexusmods.com/f/f.zip"}]
        )
        session = _make_session(meta_resp, link_resp)
        d = NexusDownloader(api_key="k", gateway=gw, staging_dir=tmp_path / "s")
        with patch.object(gw, "authorize", wraps=gw.authorize) as mock_auth:
            await d.get_file_info(1, 2, session)
        # Two GETs: metadata + download_link
        assert mock_auth.call_count == 2


# ---------------------------------------------------------------------------
# NexusDownloader._get_download_url
# ---------------------------------------------------------------------------


class TestGetDownloadUrl:
    @pytest.mark.asyncio
    async def test_returns_first_uri(self, tmp_path: pathlib.Path) -> None:
        resp = _make_aiohttp_response(
            json_data=[
                {"URI": "https://premium-files.nexusmods.com/primary"},
                {"URI": "https://cf-files.nexusmods.com/fallback"},
            ]
        )
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        url = await d._get_download_url(1, 1, session)
        assert url == "https://premium-files.nexusmods.com/primary"

    @pytest.mark.asyncio
    async def test_empty_links_raises(self, tmp_path: pathlib.Path) -> None:
        resp = _make_aiohttp_response(json_data=[])
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        with pytest.raises(DownloadError, match="No CDN"):
            await d._get_download_url(1, 1, session)

    @pytest.mark.asyncio
    async def test_401_raises(self, tmp_path: pathlib.Path) -> None:
        resp = _make_aiohttp_response(status=401)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        with pytest.raises(DownloadError, match="Premium API key"):
            await d._get_download_url(1, 1, session)

    @pytest.mark.asyncio
    async def test_403_raises(self, tmp_path: pathlib.Path) -> None:
        resp = _make_aiohttp_response(status=403)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        with pytest.raises(DownloadError, match="Premium API key"):
            await d._get_download_url(1, 1, session)

    @pytest.mark.asyncio
    async def test_404_raises(self, tmp_path: pathlib.Path) -> None:
        resp = _make_aiohttp_response(status=404)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        with pytest.raises(DownloadError, match="not found"):
            await d._get_download_url(1, 1, session)


# ---------------------------------------------------------------------------
# NexusDownloader.download
# ---------------------------------------------------------------------------


class TestDownload:
    @pytest.mark.asyncio
    async def test_successful_download_no_md5(self, tmp_path: pathlib.Path) -> None:
        content = b"hello world data"
        resp = _make_aiohttp_response(content=content)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="test.zip",
            size_bytes=len(content),
            md5="",
            download_url="https://premium-files.nexusmods.com/test.zip",
        )
        dest = await d.download(fi, session)
        assert dest.exists()
        assert dest.read_bytes() == content

    @pytest.mark.asyncio
    async def test_successful_download_md5_valid(self, tmp_path: pathlib.Path) -> None:
        content = b"skyrim mod data bytes"
        md5 = _md5_of(content)
        resp = _make_aiohttp_response(content=content)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="mod.zip",
            size_bytes=len(content),
            md5=md5,
            download_url="https://premium-files.nexusmods.com/mod.zip",
        )
        dest = await d.download(fi, session)
        assert dest.exists()
        assert dest.read_bytes() == content

    @pytest.mark.asyncio
    async def test_md5_mismatch_raises_and_cleans_up(
        self, tmp_path: pathlib.Path
    ) -> None:
        content = b"real data"
        # Provide 5 responses for 5 retry attempts (MD5ValidationError triggers retries)
        responses = [_make_aiohttp_response(content=content) for _ in range(5)]
        session = _make_session(*responses)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="bad.zip",
            size_bytes=len(content),
            md5="0000000000000000000000000000000000",
            download_url="https://premium-files.nexusmods.com/bad.zip",
        )
        with pytest.raises(MD5ValidationError, match="MD5 mismatch"):
            await d.download(fi, session)
        # File must be cleaned up
        assert not (d.staging_dir / "bad.zip").exists()

    @pytest.mark.asyncio
    async def test_network_error_raises_download_error_and_cleans_up(
        self, tmp_path: pathlib.Path
    ) -> None:
        resp = MagicMock()
        resp.status = 200
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        resp.content = MagicMock()

        async def _bad_iter(size: int):
            yield b"partial"
            raise aiohttp.ClientError("connection reset")

        resp.content.iter_chunked = _bad_iter
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="net_fail.zip",
            download_url="https://premium-files.nexusmods.com/net_fail.zip",
        )
        with pytest.raises(DownloadError):
            await d.download(fi, session)
        assert not (d.staging_dir / "net_fail.zip").exists()

    @pytest.mark.asyncio
    async def test_progress_callback_called(self, tmp_path: pathlib.Path) -> None:
        content = b"a" * 3000
        resp = _make_aiohttp_response(content=content)
        session = _make_session(resp)
        # Use a chunk size of 1000 so callback is called 3 times.
        d = _make_downloader(tmp_path, chunk_size=1000)
        fi = _make_file_info(
            file_name="prog.zip",
            size_bytes=3000,
            download_url="https://premium-files.nexusmods.com/prog.zip",
        )
        calls: list[float] = []

        async def cb(p: DownloadProgress) -> None:
            calls.append(p.percent)

        await d.download(fi, session, progress_cb=cb)
        assert len(calls) == 3
        assert calls[-1] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_content_length_used_when_size_zero(
        self, tmp_path: pathlib.Path
    ) -> None:
        content = b"data"
        resp = _make_aiohttp_response(
            content=content,
            headers={"Content-Length": str(len(content))},
        )
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="cl.zip",
            size_bytes=0,
            download_url="https://premium-files.nexusmods.com/cl.zip",
        )
        progress_calls: list[DownloadProgress] = []

        async def cb(p: DownloadProgress) -> None:
            progress_calls.append(
                DownloadProgress(p.file_name, p.total_bytes, p.downloaded_bytes)
            )

        await d.download(fi, session, progress_cb=cb)
        assert progress_calls[0].total_bytes == len(content)

    @pytest.mark.asyncio
    async def test_successful_download_sha256_valid(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Verifica que la validación SHA256 funciona correctamente."""
        content = b"skyrim mod data with sha256"
        sha256 = _sha256_of(content)
        resp = _make_aiohttp_response(content=content)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="mod_sha256.zip",
            size_bytes=len(content),
            md5="",  # Sin MD5, solo SHA256
            sha256=sha256,
            download_url="https://premium-files.nexusmods.com/mod_sha256.zip",
        )
        dest = await d.download(fi, session)
        assert dest.exists()
        assert dest.read_bytes() == content

    @pytest.mark.asyncio
    async def test_sha256_mismatch_raises_and_cleans_up(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Verifica que SHA256 mismatch limpia el archivo y lanza HashValidationError."""
        content = b"real data for sha256"
        # Provide 5 responses for 5 retry attempts
        responses = [_make_aiohttp_response(content=content) for _ in range(5)]
        session = _make_session(*responses)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="bad_sha256.zip",
            size_bytes=len(content),
            md5="",
            sha256="0" * 64,  # SHA256 incorrecto
            download_url="https://premium-files.nexusmods.com/bad_sha256.zip",
        )
        with pytest.raises(HashValidationError, match="SHA256 mismatch"):
            await d.download(fi, session)
        # File must be cleaned up
        assert not (d.staging_dir / "bad_sha256.zip").exists()

    @pytest.mark.asyncio
    async def test_hash_validation_error_is_alias_for_md5(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Verifica que HashValidationError es compatible con MD5ValidationError (alias hacia atrás)."""
        content = b"test alias"
        responses = [_make_aiohttp_response(content=content) for _ in range(5)]
        session = _make_session(*responses)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="alias_test.zip",
            size_bytes=len(content),
            md5="wrongmd5hash00000000000000000",
            download_url="https://premium-files.nexusmods.com/alias_test.zip",
        )
        # Ambos deben funcionar: HashValidationError y MD5ValidationError
        with pytest.raises(HashValidationError):
            await d.download(fi, session)
        assert not (d.staging_dir / "alias_test.zip").exists()

    @pytest.mark.asyncio
    async def test_dual_hash_validation_md5_and_sha256(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Verifica cálculo dual de hashes MD5 y SHA256."""
        content = b"dual hash test content"
        md5 = _md5_of(content)
        sha256 = _sha256_of(content)
        resp = _make_aiohttp_response(content=content)
        session = _make_session(resp)
        d = _make_downloader(tmp_path)
        fi = _make_file_info(
            file_name="dual_hash.zip",
            size_bytes=len(content),
            md5=md5,
            sha256=sha256,
            download_url="https://premium-files.nexusmods.com/dual_hash.zip",
        )
        dest = await d.download(fi, session)
        assert dest.exists()
        assert dest.read_bytes() == content

    @pytest.mark.asyncio
    async def test_staging_dir_created_if_missing(self, tmp_path: pathlib.Path) -> None:
        staging = tmp_path / "deep" / "nested" / "staging"
        d = NexusDownloader(
            api_key="k",
            gateway=_make_gateway(),
            staging_dir=staging,
        )
        content = b"x"
        resp = _make_aiohttp_response(content=content)
        session = _make_session(resp)
        fi = _make_file_info(
            file_name="deep.zip",
            download_url="https://premium-files.nexusmods.com/deep.zip",
        )
        dest = await d.download(fi, session)
        assert dest.parent == staging
        assert dest.exists()

    @pytest.mark.asyncio
    async def test_gateway_authorize_called_before_download(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _make_gateway()
        content = b"bytes"
        resp = _make_aiohttp_response(content=content)
        session = _make_session(resp)
        d = NexusDownloader(api_key="k", gateway=gw, staging_dir=tmp_path / "s")
        fi = _make_file_info(
            file_name="gw.zip",
            download_url="https://premium-files.nexusmods.com/gw.zip",
        )
        with patch.object(gw, "authorize", wraps=gw.authorize) as mock_auth:
            await d.download(fi, session)
        mock_auth.assert_called_once_with("GET", fi.download_url)

    @pytest.mark.asyncio
    async def test_egress_violation_propagates(self, tmp_path: pathlib.Path) -> None:
        """A CDN URL not in the allow-list must raise EgressViolation."""
        content = b"data"
        resp = _make_aiohttp_response(content=content)
        session = _make_session(resp)
        # Strict policy that blocks ALL hosts.
        strict_gw = NetworkGateway(
            EgressPolicy(allowed_hosts=frozenset(), block_private_ips=False)
        )
        d = NexusDownloader(api_key="k", gateway=strict_gw, staging_dir=tmp_path / "s")
        fi = _make_file_info(
            file_name="blocked.zip",
            download_url="https://premium-files.nexusmods.com/blocked.zip",
        )
        with pytest.raises(EgressViolation):
            await d.download(fi, session)


# ---------------------------------------------------------------------------
# _cleanup helper
# ---------------------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_deletes_existing_file(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "del.zip"
        f.write_bytes(b"x")
        await _cleanup(f)
        assert not f.exists()

    @pytest.mark.asyncio
    async def test_noop_on_missing_file(self, tmp_path: pathlib.Path) -> None:
        await _cleanup(tmp_path / "nonexistent.zip")  # must not raise


# ---------------------------------------------------------------------------
# SyncEngine.enqueue_download
# ---------------------------------------------------------------------------


class TestEnqueueDownload:
    @pytest.mark.asyncio
    async def test_enqueue_runs_coroutine(self, sync_engine: SyncEngine) -> None:
        result: list[str] = []

        async def _coro() -> None:
            result.append("done")

        task = sync_engine.enqueue_download(_coro())
        assert isinstance(task, asyncio.Task)
        await task
        assert result == ["done"]

    @pytest.mark.asyncio
    async def test_multiple_downloads_enqueued(self, sync_engine: SyncEngine) -> None:
        results: list[int] = []

        async def _coro(n: int) -> None:
            results.append(n)

        tasks = [sync_engine.enqueue_download(_coro(i)) for i in range(3)]
        await asyncio.gather(*tasks)
        assert sorted(results) == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_done_callback_removes_task(self, sync_engine: SyncEngine) -> None:
        async def _noop() -> None:
            pass

        task = sync_engine.enqueue_download(_noop())
        await task
        # After completion the task should be removed from _download_tasks.
        assert task not in sync_engine._download_tasks


# ---------------------------------------------------------------------------
# DownloadModParams (Pydantic)
# ---------------------------------------------------------------------------


class TestDownloadModParams:
    def test_valid(self) -> None:
        p = DownloadModParams(nexus_id=1, file_id=2)
        assert p.nexus_id == 1
        assert p.file_id == 2

    def test_nexus_id_must_be_positive(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            DownloadModParams(nexus_id=0, file_id=1)

    def test_file_id_must_be_positive(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            DownloadModParams(nexus_id=1, file_id=0)


# ---------------------------------------------------------------------------
# download_mod tool — schema registration
# ---------------------------------------------------------------------------


class TestDownloadModSchema:
    def test_tool_registered(self, tool_registry: AsyncToolRegistry) -> None:
        assert "download_mod" in tool_registry.tools

    def test_schema_has_required_fields(self, tool_registry: AsyncToolRegistry) -> None:
        schema = tool_registry.tools["download_mod"].input_schema
        assert "nexus_id" in schema["properties"]
        assert "file_id" in schema["properties"]
        assert schema["required"] == ["nexus_id"]

    def test_total_tools_count(self, tool_registry: AsyncToolRegistry) -> None:
        # search_mod, check_load_order, detect_conflicts, run_loot_sort,
        # install_mod, run_xedit_analysis, download_mod,
        # preview_mod_installer, install_mod_from_archive, setup_tools,
        # analyze_esp_conflicts = 11 + newly added tools
        assert len(tool_registry.tools) == 21


# ---------------------------------------------------------------------------
# download_mod tool — missing configuration
# ---------------------------------------------------------------------------


class TestDownloadModMissingConfig:
    @pytest.mark.asyncio
    async def test_no_downloader_returns_error(
        self, adb: AsyncModRegistry, mo2: MO2Controller, sync_engine: SyncEngine
    ) -> None:
        registry = AsyncToolRegistry(
            registry=adb,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=HITLGuard(),
            downloader=None,
        )
        result = json.loads(
            await registry.execute("download_mod", {"nexus_id": 1, "file_id": 2})
        )
        assert "error" in result
        assert "downloader" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_no_hitl_returns_error(
        self,
        adb: AsyncModRegistry,
        mo2: MO2Controller,
        sync_engine: SyncEngine,
        downloader: NexusDownloader,
    ) -> None:
        registry = AsyncToolRegistry(
            registry=adb,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=None,
            downloader=downloader,
        )
        result = json.loads(
            await registry.execute("download_mod", {"nexus_id": 1, "file_id": 2})
        )
        assert "error" in result
        assert "hitl" in result["error"].lower()


# ---------------------------------------------------------------------------
# download_mod tool — get_file_info failure
# ---------------------------------------------------------------------------


class TestDownloadModMetadataFailure:
    @pytest.mark.asyncio
    async def test_metadata_error_returns_error_json(
        self, tool_registry: AsyncToolRegistry
    ) -> None:
        with patch(
            "sky_claw.agent.tools.nexus_tools.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            with patch.object(
                tool_registry._downloader,  # type: ignore[union-attr]
                "get_file_info",
                side_effect=DownloadError("API unreachable"),
            ):
                result = json.loads(
                    await tool_registry.execute(
                        "download_mod", {"nexus_id": 99, "file_id": 1}
                    )
                )

        assert "error" in result
        assert result["nexus_id"] == 99


# ---------------------------------------------------------------------------
# download_mod tool — HITL denied / timeout
# ---------------------------------------------------------------------------


class TestDownloadModHITLDenied:
    @pytest.mark.asyncio
    async def test_denied_returns_denied_status(
        self,
        adb: AsyncModRegistry,
        mo2: MO2Controller,
        sync_engine: SyncEngine,
        downloader: NexusDownloader,
        tmp_path: pathlib.Path,
    ) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)

        registry = AsyncToolRegistry(
            registry=adb,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=guard,
            downloader=downloader,
        )

        fi = _make_file_info(nexus_id=10, file_id=20, file_name="denied.zip")

        with patch(
            "sky_claw.agent.tools.nexus_tools.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            with (
                patch.object(downloader, "get_file_info", return_value=fi),
                patch.object(
                    guard,
                    "request_approval",
                    return_value=Decision.DENIED,
                ),
            ):
                result = json.loads(
                    await registry.execute(
                        "download_mod", {"nexus_id": 10, "file_id": 20}
                    )
                )

        assert result["status"] == "denied"
        assert result["decision"] == "denied"
        assert result["file_name"] == "denied.zip"

    @pytest.mark.asyncio
    async def test_timeout_returns_denied_status(
        self,
        adb: AsyncModRegistry,
        mo2: MO2Controller,
        sync_engine: SyncEngine,
        downloader: NexusDownloader,
    ) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)

        registry = AsyncToolRegistry(
            registry=adb,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=guard,
            downloader=downloader,
        )
        fi = _make_file_info(nexus_id=11, file_id=21, file_name="timeout.zip")

        with patch(
            "sky_claw.agent.tools.nexus_tools.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            with (
                patch.object(downloader, "get_file_info", return_value=fi),
                patch.object(
                    guard,
                    "request_approval",
                    return_value=Decision.TIMEOUT,
                ),
            ):
                result = json.loads(
                    await registry.execute(
                        "download_mod", {"nexus_id": 11, "file_id": 21}
                    )
                )

        assert result["status"] == "denied"
        assert result["decision"] == "timeout"


# ---------------------------------------------------------------------------
# download_mod tool — HITL approved → enqueue
# ---------------------------------------------------------------------------


class TestDownloadModApproved:
    @pytest.mark.asyncio
    async def test_approved_enqueues_download(
        self,
        adb: AsyncModRegistry,
        mo2: MO2Controller,
        sync_engine: SyncEngine,
        downloader: NexusDownloader,
    ) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)
        registry = AsyncToolRegistry(
            registry=adb,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=guard,
            downloader=downloader,
        )
        fi = _make_file_info(
            nexus_id=55,
            file_id=77,
            file_name="approved.zip",
            size_bytes=4096,
        )

        enqueued: list[Any] = []

        def _fake_enqueue(coro: Any, context: str = "") -> asyncio.Task:
            task = asyncio.create_task(coro)
            enqueued.append(task)
            return task

        with patch(
            "sky_claw.agent.tools.nexus_tools.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            with patch.object(downloader, "get_file_info", return_value=fi):
                with patch.object(
                    guard, "request_approval", return_value=Decision.APPROVED
                ):
                    with patch.object(
                        sync_engine, "enqueue_download", side_effect=_fake_enqueue
                    ):
                        result = json.loads(
                            await registry.execute(
                                "download_mod", {"nexus_id": 55, "file_id": 77}
                            )
                        )

        assert result["status"] == "enqueued"
        assert result["nexus_id"] == 55
        assert result["file_id"] == 77
        assert result["file_name"] == "approved.zip"
        assert result["size_bytes"] == 4096
        assert len(enqueued) == 1

    @pytest.mark.asyncio
    async def test_approved_payload_includes_staging_dir(
        self,
        adb: AsyncModRegistry,
        mo2: MO2Controller,
        sync_engine: SyncEngine,
        downloader: NexusDownloader,
    ) -> None:
        guard = HITLGuard(notify_fn=None, timeout=5)
        registry = AsyncToolRegistry(
            registry=adb,
            mo2=mo2,
            sync_engine=sync_engine,
            hitl=guard,
            downloader=downloader,
        )
        fi = _make_file_info(nexus_id=1, file_id=1, file_name="f.zip")

        def _discard_enqueue(coro: Any, context: str = "") -> MagicMock:
            coro.close()
            return MagicMock()

        with patch(
            "sky_claw.agent.tools.nexus_tools.aiohttp.ClientSession"
        ) as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            with patch.object(downloader, "get_file_info", return_value=fi):
                with patch.object(
                    guard, "request_approval", return_value=Decision.APPROVED
                ):
                    with patch.object(
                        sync_engine, "enqueue_download", side_effect=_discard_enqueue
                    ):
                        result = json.loads(
                            await registry.execute(
                                "download_mod", {"nexus_id": 1, "file_id": 1}
                            )
                        )

        assert "staging_dir" in result
        assert str(downloader.staging_dir) == result["staging_dir"]


# ---------------------------------------------------------------------------
# Config — CDN hosts in allow-list
# ---------------------------------------------------------------------------


class TestConfigCDNHosts:
    def test_premium_files_in_allowed_hosts(self) -> None:
        from sky_claw.config import ALLOWED_HOSTS

        assert "premium-files.nexusmods.com" in ALLOWED_HOSTS

    def test_cf_files_in_allowed_hosts(self) -> None:
        from sky_claw.config import ALLOWED_HOSTS

        assert "cf-files.nexusmods.com" in ALLOWED_HOSTS

    def test_premium_files_method_get_only(self) -> None:
        from sky_claw.config import ALLOWED_METHODS

        methods = ALLOWED_METHODS["premium-files.nexusmods.com"]
        assert methods == frozenset({"GET"})

    def test_cf_files_method_get_only(self) -> None:
        from sky_claw.config import ALLOWED_METHODS

        methods = ALLOWED_METHODS["cf-files.nexusmods.com"]
        assert methods == frozenset({"GET"})

    def test_download_chunk_size_constant(self) -> None:
        from sky_claw.config import NEXUS_DOWNLOAD_CHUNK_SIZE

        assert NEXUS_DOWNLOAD_CHUNK_SIZE == 1024 * 1024

    def test_download_timeout_constant(self) -> None:
        from sky_claw.config import NEXUS_DOWNLOAD_TIMEOUT_SECONDS

        assert NEXUS_DOWNLOAD_TIMEOUT_SECONDS == 600


# ---------------------------------------------------------------------------
# NetworkGateway — CDN host authorization
# ---------------------------------------------------------------------------


class TestNetworkGatewayCDN:
    @pytest.mark.asyncio
    async def test_premium_files_get_authorized(self) -> None:
        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        await gw.authorize("GET", "https://premium-files.nexusmods.com/file.zip")

    @pytest.mark.asyncio
    async def test_cf_files_get_authorized(self) -> None:
        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        await gw.authorize("GET", "https://cf-files.nexusmods.com/file.zip")

    @pytest.mark.asyncio
    async def test_premium_files_post_rejected(self) -> None:
        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        with pytest.raises(EgressViolation):
            await gw.authorize("POST", "https://premium-files.nexusmods.com/file.zip")

    @pytest.mark.asyncio
    async def test_cf_files_post_rejected(self) -> None:
        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        with pytest.raises(EgressViolation):
            await gw.authorize("POST", "https://cf-files.nexusmods.com/file.zip")

"""Tests for ToolsInstaller, local_config, and setup_tools tool."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import pathlib
import tempfile
import threading
import zipfile
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
import pytest

from sky_claw.app.security.hitl import Decision, HITLGuard
from sky_claw.app.security.network_gateway import EgressPolicy, NetworkGateway
from sky_claw.app.security.path_validator import PathValidator, PathViolationError
from sky_claw.local import tools_installer
from sky_claw.local.discovery import scanner
from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.discovery.scanner import PANDORA_EXE_NAMES
from sky_claw.local.tools_installer import (
    _SEVENZIP_TIMEOUT_SECONDS,
    InstallVerification,
    ReleaseAsset,
    ToolInstallError,
    ToolsInstaller,
    _extract_7z_safe,
    _extract_zip_safe,
    _parse_7z_listing,
    find_exe_in_dir,
    scan_common_paths,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def validator(tmp_path: pathlib.Path) -> PathValidator:
    """Espeja las raíces que ``AppContext.start_full`` le da al validator real.

    La raíz de staging (``gettempdir()/sky_claw``) no es decorativa: los instaladores
    que descargan a un temporal la necesitan, y omitirla acá hacía que el fixture
    aceptara código que en producción muere con ``PathViolationError``.
    """
    return PathValidator(roots=[tmp_path, pathlib.Path(tempfile.gettempdir()) / "sky_claw"])


@pytest.fixture
def gateway() -> NetworkGateway:
    return NetworkGateway(EgressPolicy(block_private_ips=False))


@pytest.fixture
def hitl_guard() -> HITLGuard:
    return HITLGuard(notify_fn=None, timeout=5)


@pytest.fixture
def installer(hitl_guard: HITLGuard, gateway: NetworkGateway, validator: PathValidator) -> ToolsInstaller:
    # Lock mockeado: el mecanismo real cross-process lo valida el test de
    # contención (test_tools_installer_lock_contencion.py). Acá basta con que
    # los ensure_* adquieran y liberen; un DistributedLockManager real exigiría
    # initialize() que no corre en fixtures síncronas.
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


def _github_release_json(
    tag: str = "0.22.4",
    asset_name: str = "loot_0.22.4-win64.zip",
    size: int = 50_000_000,
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": asset_name,
                "size": size,
                "url": "https://api.github.com/repos/loot/loot/releases/assets/1001",
                "browser_download_url": f"https://github.com/loot/loot/releases/download/{tag}/{asset_name}",
            },
            {
                "name": "loot_0.22.4-linux.tar.gz",
                "size": 40_000_000,
                "url": "https://api.github.com/repos/loot/loot/releases/assets/1002",
                "browser_download_url": f"https://github.com/loot/loot/releases/download/{tag}/loot_0.22.4-linux.tar.gz",
            },
        ],
    }


def _xedit_release_json(
    tag: str = "4.1.5",
    asset_name: str = "xEdit_4.1.5.7z",
    size: int = 30_000_000,
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": asset_name,
                "size": size,
                "url": "https://api.github.com/repos/TES5Edit/TES5Edit/releases/assets/2001",
                "browser_download_url": f"https://github.com/TES5Edit/TES5Edit/releases/download/{tag}/{asset_name}",
            },
        ],
    }


def _mockear_descarga_de_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    *,
    asset_name: str,
    version: str,
) -> None:
    """Corta red y HITL: deja el ``ensure_*`` corriendo hasta la extracción."""
    asset = ReleaseAsset(
        name=asset_name,
        size=1024,
        download_url="https://api.github.com/x",
        browser_download_url="https://github.com/x",
    )
    monkeypatch.setattr(
        "sky_claw.local.tools_installer.ToolsInstaller._find_github_asset",
        AsyncMock(return_value=(asset, version)),
    )
    monkeypatch.setattr(
        "sky_claw.local.tools_installer.HITLGuard.request_approval",
        AsyncMock(return_value=Decision.APPROVED),
    )
    monkeypatch.setattr(
        "sky_claw.local.tools_installer.ToolsInstaller._download_asset",
        AsyncMock(return_value=tmp_path / asset_name),
    )


def _mockear_py7zr(monkeypatch: pytest.MonkeyPatch, *, miembros: list[str]) -> None:
    """py7zr abre bien y lista *miembros* (para ejercitar su propia validación)."""
    szf = MagicMock()
    szf.getnames = MagicMock(return_value=miembros)
    szf.list = MagicMock(return_value=[MagicMock(filename=nombre, is_symlink=False) for nombre in miembros])
    szf.__enter__ = MagicMock(return_value=szf)
    szf.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("py7zr.SevenZipFile", MagicMock(return_value=szf))


def _mockear_py7zr_roto(monkeypatch: pytest.MonkeyPatch) -> None:
    """py7zr revienta al ABRIR — nunca llega a enumerar miembros (caso BCJ2)."""

    def _reventar(*args: Any, **kwargs: Any) -> None:
        raise ValueError("BCJ2 filter is not supported")

    monkeypatch.setattr("py7zr.SevenZipFile", _reventar)


def _mockear_7z_del_sistema(monkeypatch: pytest.MonkeyPatch, *, miembros: list[str]) -> MagicMock:
    """Simula el binario ``7z``: ``l -slt`` devuelve *miembros*, ``x`` no hace nada."""
    listado = "7-Zip 21.07\n--\nPath = dummy.7z\nType = 7z\n\n----------\n"
    listado += "".join(f"Path = {m}\nSize = 1\n\n" for m in miembros)

    def _run(cmd: list[str], **kwargs: Any) -> MagicMock:
        resultado = MagicMock()
        resultado.stdout = listado.encode("utf-8") if cmd[1] == "l" else b""
        resultado.stderr = b""
        return resultado

    mock_run = MagicMock(side_effect=_run)
    monkeypatch.setattr("subprocess.run", mock_run)
    return mock_run


# ---------------------------------------------------------------------------
# find_exe_in_dir / scan_common_paths
# ---------------------------------------------------------------------------


class TestFindExeInDir:
    def test_finds_exe_in_flat_dir(self, tmp_path: pathlib.Path) -> None:
        exe = tmp_path / "loot.exe"
        exe.write_text("fake", encoding="utf-8")
        assert find_exe_in_dir(tmp_path, "loot.exe") == exe

    def test_finds_exe_in_subdir(self, tmp_path: pathlib.Path) -> None:
        subdir = tmp_path / "LOOT" / "bin"
        subdir.mkdir(parents=True)
        exe = subdir / "loot.exe"
        exe.write_text("fake", encoding="utf-8")
        assert find_exe_in_dir(tmp_path, "loot.exe") == exe

    def test_returns_none_when_not_found(self, tmp_path: pathlib.Path) -> None:
        assert find_exe_in_dir(tmp_path, "loot.exe") is None

    def test_returns_none_for_nonexistent_dir(self) -> None:
        assert find_exe_in_dir(pathlib.Path("/nonexistent"), "loot.exe") is None


class TestScanCommonPaths:
    def test_finds_in_common_path(self, tmp_path: pathlib.Path) -> None:
        loot_dir = tmp_path / "LOOT"
        loot_dir.mkdir()
        (loot_dir / "loot.exe").write_text("fake", encoding="utf-8")
        result = scan_common_paths((loot_dir,), "loot.exe")
        assert result is not None
        assert result.name == "loot.exe"

    def test_returns_none_when_no_match(self) -> None:
        result = scan_common_paths(
            (pathlib.Path("/nonexistent1"), pathlib.Path("/nonexistent2")),
            "loot.exe",
        )
        assert result is None


# ---------------------------------------------------------------------------
# ToolsInstaller.ensure_loot
# ---------------------------------------------------------------------------


class TestEnsureLoot:
    @pytest.mark.asyncio
    async def test_approval_uses_download_category(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """La aprobación de descarga se etiqueta con category='download' para que el puente
        HITL de la GUI pueda retenerla en el modal (Follow-up C) en vez de caer a Telegram."""
        from unittest.mock import AsyncMock

        from sky_claw.app.security.hitl import Decision
        from sky_claw.local.tools_installer import ReleaseAsset

        install_dir = tmp_path / "install"
        install_dir.mkdir()

        asset = ReleaseAsset(
            name="loot_0.22.4-win64.zip",
            size=1,
            download_url="https://api.github.com/x",
            browser_download_url="https://github.com/loot/loot/releases/download/0.22.4/loot.zip",
        )
        installer._find_github_asset = AsyncMock(return_value=(asset, "0.22.4"))  # type: ignore[method-assign]
        installer._hitl.request_approval = AsyncMock(return_value=Decision.DENIED)  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)
        with pytest.raises(ToolInstallError):
            await installer.ensure_loot(install_dir, session)

        assert installer._hitl.request_approval.call_args.kwargs["category"] == "download"

    @pytest.mark.asyncio
    async def test_returns_existing_without_download(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando loot.exe ya existe, lo devuelve inmediatamente sin descargar."""
        exe = tmp_path / "loot.exe"
        exe.write_text("fake", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_loot(tmp_path, session)

        assert result.already_existed is True
        assert result.exe_path == exe
        assert result.tool_name == "LOOT"

    @pytest.mark.asyncio
    async def test_downloads_and_extracts_when_missing(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando loot.exe está ausente, descarga de GitHub y extrae el archivo."""
        import zipfile

        # Prepare a fake zip containing loot.exe.
        zip_path = tmp_path / "_fake_asset.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("LOOT/loot.exe", "fake-binary")

        zip_bytes = zip_path.read_bytes()
        zip_path.unlink()

        install_dir = tmp_path / "install"
        install_dir.mkdir()

        release_json = _github_release_json(size=len(zip_bytes))

        # Mock aiohttp responses.
        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)
        mock_api_resp.__aenter__ = AsyncMock(return_value=mock_api_resp)
        mock_api_resp.__aexit__ = AsyncMock(return_value=False)

        # Simulate streaming download.
        async def _iter_chunks(size: int) -> AsyncIterator[bytes]:
            yield zip_bytes

        mock_dl_resp = MagicMock()
        mock_dl_resp.status = 200
        mock_dl_resp.raise_for_status = MagicMock()
        mock_dl_resp.content = MagicMock()
        mock_dl_resp.content.iter_chunked = _iter_chunks

        session = MagicMock(spec=aiohttp.ClientSession)

        # Both _find_github_asset and _download_asset now go through gateway.request.
        # First call: GitHub releases API → returns JSON metadata (mock_api_resp).
        # Second call: asset download URL → returns binary stream (mock_dl_resp).
        installer._gateway.request = AsyncMock(side_effect=[mock_api_resp, mock_dl_resp])

        # Auto-approve HITL.
        async def _auto_approve() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-loot-0.22.4", approved=True)

        asyncio.create_task(_auto_approve())

        result = await installer.ensure_loot(install_dir, session)

        assert result.already_existed is False
        assert result.tool_name == "LOOT"
        assert result.version == "0.22.4"
        assert result.exe_path.name == "loot.exe"
        assert result.exe_path.exists()
        assert installer._gateway.request.await_count == 2
        # Second call must be the asset's API URL (redirect-reauth enforced via gateway).
        assert (
            installer._gateway.request.call_args_list[1].args[1]
            == "https://api.github.com/repos/loot/loot/releases/assets/1001"
        )

    @pytest.mark.asyncio
    async def test_hitl_denial_raises(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando el operador lo deniega, lanza ToolInstallError."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        release_json = _github_release_json()
        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._gateway.request = AsyncMock(return_value=mock_api_resp)

        async def _auto_deny() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-loot-0.22.4", approved=False)

        asyncio.create_task(_auto_deny())

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_loot(install_dir, session)

    @pytest.mark.asyncio
    async def test_github_api_failure_raises(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando la API de GitHub falla, lanza ToolInstallError."""
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        mock_resp = MagicMock()
        mock_resp.status = 403

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._gateway.request = AsyncMock(return_value=mock_resp)

        with pytest.raises(ToolInstallError, match="403"):
            await installer.ensure_loot(install_dir, session)


# ---------------------------------------------------------------------------
# ToolsInstaller.ensure_xedit
# ---------------------------------------------------------------------------


class TestEnsureXedit:
    @pytest.mark.asyncio
    async def test_returns_existing_without_download(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        exe = tmp_path / "SSEEdit.exe"
        exe.write_text("fake", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_xedit(tmp_path, session)

        assert result.already_existed is True
        assert result.exe_path == exe
        assert result.tool_name == "SSEEdit"

    @pytest.mark.asyncio
    async def test_downloads_and_extracts_when_missing(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        import zipfile

        zip_path = tmp_path / "_fake_asset.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("SSEEdit/SSEEdit.exe", "fake-binary")

        zip_bytes = zip_path.read_bytes()
        zip_path.unlink()

        install_dir = tmp_path / "install"
        install_dir.mkdir()

        # Use a .zip asset name to match.
        release_json = _xedit_release_json(asset_name="xEdit_4.1.5.zip", size=len(zip_bytes))

        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)
        mock_api_resp.__aenter__ = AsyncMock(return_value=mock_api_resp)
        mock_api_resp.__aexit__ = AsyncMock(return_value=False)

        async def _iter_chunks(size: int) -> AsyncIterator[bytes]:
            yield zip_bytes

        mock_dl_resp = MagicMock()
        mock_dl_resp.status = 200
        mock_dl_resp.raise_for_status = MagicMock()
        mock_dl_resp.content = MagicMock()
        mock_dl_resp.content.iter_chunked = _iter_chunks

        session = MagicMock(spec=aiohttp.ClientSession)

        # Both _find_github_asset and _download_asset go through gateway.request.
        installer._gateway.request = AsyncMock(side_effect=[mock_api_resp, mock_dl_resp])

        async def _auto_approve() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-xedit-4.1.5", approved=True)

        asyncio.create_task(_auto_approve())

        result = await installer.ensure_xedit(install_dir, session)

        assert result.already_existed is False
        assert result.tool_name == "SSEEdit"
        assert result.version == "4.1.5"
        assert result.exe_path.name == "SSEEdit.exe"
        assert installer._gateway.request.await_count == 2
        assert (
            installer._gateway.request.call_args_list[1].args[1]
            == "https://api.github.com/repos/TES5Edit/TES5Edit/releases/assets/2001"
        )

    @pytest.mark.asyncio
    async def test_hitl_denial_raises(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        install_dir = tmp_path / "install"
        install_dir.mkdir()

        release_json = _xedit_release_json(asset_name="xEdit_4.1.5.zip")
        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.json = AsyncMock(return_value=release_json)

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._gateway.request = AsyncMock(return_value=mock_api_resp)

        async def _auto_deny() -> None:
            await asyncio.sleep(0.01)
            await installer._hitl.respond("install-xedit-4.1.5", approved=False)

        asyncio.create_task(_auto_deny())

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_xedit(install_dir, session)

    @pytest.mark.asyncio
    async def test_renombra_xedit_a_sseedit(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El archive trae el nombre genérico xEdit.exe → queda como SSEEdit.exe.

        Sin el renombrado, SSEEdit arranca con el popup de selección de juego y
        bloquea la GUI esperando un click que nadie da.
        """
        install_dir = tmp_path / "xedit"
        install_dir.mkdir()
        _mockear_descarga_de_tool(monkeypatch, tmp_path, asset_name="xEdit_4.1.5.7z", version="4.1.5")

        # _extract simulado: la extracción real deja xEdit.exe, no SSEEdit.exe.
        def _extract_falso(self_obj: ToolsInstaller, archive: pathlib.Path, directory: pathlib.Path) -> None:
            (directory / "xEdit.exe").write_text("binario-xedit", encoding="utf-8")

        monkeypatch.setattr("sky_claw.local.tools_installer.ToolsInstaller._extract", _extract_falso)

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_xedit(install_dir, session)

        assert result.tool_name == "SSEEdit"
        assert result.exe_path == install_dir / "SSEEdit.exe"
        assert (install_dir / "SSEEdit.exe").exists()
        assert not (install_dir / "xEdit.exe").exists()

    @pytest.mark.asyncio
    async def test_no_pisa_un_sseedit_existente(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el archive trae AMBOS exes, el SSEEdit.exe real sobrevive intacto.

        El renombrado incondicional destruía el binario bueno (o reventaba con
        FileExistsError en Windows, donde os.rename no pisa el destino).
        """
        install_dir = tmp_path / "xedit"
        install_dir.mkdir()
        _mockear_descarga_de_tool(monkeypatch, tmp_path, asset_name="xEdit_4.1.5.7z", version="4.1.5")

        def _extract_falso(self_obj: ToolsInstaller, archive: pathlib.Path, directory: pathlib.Path) -> None:
            (directory / "SSEEdit.exe").write_text("binario-sseedit", encoding="utf-8")
            (directory / "xEdit.exe").write_text("binario-xedit", encoding="utf-8")

        monkeypatch.setattr("sky_claw.local.tools_installer.ToolsInstaller._extract", _extract_falso)

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_xedit(install_dir, session)

        assert result.exe_path == install_dir / "SSEEdit.exe"
        assert (install_dir / "SSEEdit.exe").read_text(encoding="utf-8") == "binario-sseedit"
        assert (install_dir / "xEdit.exe").exists(), "xEdit.exe no debe desaparecer si ya había un SSEEdit.exe"

    @pytest.mark.asyncio
    async def test_no_pisa_si_el_sseedit_aparece_en_la_carrera(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TOCTOU: el destino aparece ENTRE el chequeo y el renombrado.

        Es el motivo de usar ``rename()`` y no ``replace()``: os.replace pisa el
        destino, que es exactamente lo que hay que evitar. En Windows os.rename
        levanta FileExistsError y el SSEEdit.exe bueno sobrevive.
        """
        install_dir = tmp_path / "xedit"
        install_dir.mkdir()
        _mockear_descarga_de_tool(monkeypatch, tmp_path, asset_name="xEdit_4.1.5.7z", version="4.1.5")

        def _extract_falso(self_obj: ToolsInstaller, archive: pathlib.Path, directory: pathlib.Path) -> None:
            (directory / "xEdit.exe").write_text("binario-xedit", encoding="utf-8")

        monkeypatch.setattr("sky_claw.local.tools_installer.ToolsInstaller._extract", _extract_falso)

        # El rename se comporta como en Windows: el destino ya existe al momento
        # del syscall, aunque el `exists()` previo lo haya visto libre.
        rename_real = pathlib.Path.rename

        def _rename_que_choca(self_path: pathlib.Path, target: Any) -> pathlib.Path:
            if pathlib.Path(target).name == "SSEEdit.exe":
                pathlib.Path(target).write_text("binario-sseedit", encoding="utf-8")
                raise FileExistsError(17, "File exists", str(target))
            return rename_real(self_path, target)

        monkeypatch.setattr(pathlib.Path, "rename", _rename_que_choca)

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_xedit(install_dir, session)

        assert result.exe_path == install_dir / "SSEEdit.exe"
        assert (install_dir / "SSEEdit.exe").read_text(encoding="utf-8") == "binario-sseedit"


class TestEnsurePandora:
    def test_los_nombres_de_pandora_son_una_sola_fuente_de_verdad(self) -> None:
        """Ancla que ENUMERA: el instalador y el scanner comparten el mismo tuple.

        Igualdad literal a propósito. Agregar una variante de nombre al scanner sin
        que el instalador la reconozca (o al revés) rompe acá: son dos superficies
        del mismo recurso y el defecto histórico es arreglar una sola.
        """
        assert PANDORA_EXE_NAMES == ("Pandora Behaviour Engine+.exe", "Pandora Engine.exe", "Pandora.exe")
        assert tools_installer.PANDORA_EXE_NAMES is scanner.PANDORA_EXE_NAMES
        # El alias interno es lo que consume la detección DENTRO del scanner: si
        # alguien lo redefine con su propia tupla, la detección diverge del
        # instalador y las dos aserciones de arriba siguen en verde.
        assert scanner._PANDORA_NAMES is scanner.PANDORA_EXE_NAMES

    @pytest.mark.asyncio
    async def test_prefiere_el_ejecutable_del_release_actual(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Conviviendo el legacy y el actual, devuelve el actual.

        Tras un upgrade manual in-place quedan los dos. Devolver `Pandora.exe`
        exporta el binario viejo por `PANDORA_EXE` y los rituales siguen
        lanzando ése pese a estar el nuevo instalado.
        """
        install_dir = tmp_path / "pandora"
        install_dir.mkdir()
        (install_dir / "Pandora.exe").write_text("viejo", encoding="utf-8")
        (install_dir / "Pandora Behaviour Engine+.exe").write_text("actual", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_pandora(install_dir, session)

        assert result.exe_path == install_dir / "Pandora Behaviour Engine+.exe"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exe_name", PANDORA_EXE_NAMES)
    async def test_detecta_cualquier_variante_ya_instalada(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, exe_name: str
    ) -> None:
        """Pandora ya instalado bajo CUALQUIER nombre conocido → ni red ni HITL.

        Buscando un solo nombre, una instalación previa como ``Pandora.exe`` se
        reportaba como faltante y disparaba re-descarga + aprobación redundante.
        """
        install_dir = tmp_path / "pandora"
        install_dir.mkdir()
        (install_dir / exe_name).write_text("fake", encoding="utf-8")

        installer._gateway.request = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("no debe salir a la red si Pandora ya está instalado")
        )
        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_pandora(install_dir, session)

        assert result.already_existed is True
        assert result.exe_path == install_dir / exe_name
        assert result.tool_name == "Pandora"

    @pytest.mark.asyncio
    async def test_usa_el_nombre_real_del_ejecutable(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tras extraer, encuentra el ``Pandora Behaviour Engine+.exe`` del release actual."""
        install_dir = tmp_path / "pandora"
        install_dir.mkdir()
        _mockear_descarga_de_tool(monkeypatch, tmp_path, asset_name="Pandora.zip", version="1.0.0")

        def _extract_falso(self_obj: ToolsInstaller, archive: pathlib.Path, directory: pathlib.Path) -> None:
            (directory / "Pandora Behaviour Engine+.exe").write_text("fake", encoding="utf-8")

        monkeypatch.setattr("sky_claw.local.tools_installer.ToolsInstaller._extract", _extract_falso)

        session = MagicMock(spec=aiohttp.ClientSession)
        result = await installer.ensure_pandora(install_dir, session)

        assert result.tool_name == "Pandora"
        assert result.exe_path == install_dir / "Pandora Behaviour Engine+.exe"
        assert result.already_existed is False


class TestExtraccion7zConFallback:
    """Fallback al ``7z`` del sistema cuando py7zr no soporta el archive (BCJ2)."""

    def test_cae_al_7z_del_sistema_cuando_py7zr_falla(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Un fallo de FORMATO de py7zr sí degrada al binario del sistema."""
        _mockear_py7zr_roto(monkeypatch)
        mock_run = _mockear_7z_del_sistema(monkeypatch, miembros=["SSEEdit.exe", "Edit Scripts/algo.pas"])

        archive = tmp_path / "dummy.7z"
        dest = tmp_path / "out"
        _extract_7z_safe(archive, dest)

        assert [c.args[0][:2] for c in mock_run.call_args_list] == [["7z", "l"], ["7z", "x"]]

    def test_invoca_7z_con_los_argumentos_exactos(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Igualdad sobre la llamada COMPLETA: flags, destino, check, capture y timeout."""
        _mockear_py7zr_roto(monkeypatch)
        mock_run = _mockear_7z_del_sistema(monkeypatch, miembros=["SSEEdit.exe"])

        archive = tmp_path / "dummy.7z"
        dest = tmp_path / "out"
        _extract_7z_safe(archive, dest)

        assert mock_run.call_args_list == [
            call(
                ["7z", "l", "-slt", "-y", str(archive)],
                check=True,
                capture_output=True,
                timeout=_SEVENZIP_TIMEOUT_SECONDS,
            ),
            call(
                ["7z", "x", f"-o{dest}", "-y", str(archive)],
                check=True,
                capture_output=True,
                timeout=_SEVENZIP_TIMEOUT_SECONDS,
            ),
        ]

    def test_no_cae_al_7z_si_la_falla_fue_de_seguridad(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Zip-slip detectado por py7zr → se propaga SIN tocar el 7z del sistema.

        Degradar a otro extractor tras una falla de seguridad es exactamente el
        bypass del sandbox: el archive malicioso terminaría extraído por un binario
        que no aplica ``_is_safe_path``.
        """
        _mockear_py7zr(monkeypatch, miembros=["../evil.txt"])
        mock_run = MagicMock()
        monkeypatch.setattr("subprocess.run", mock_run)

        with pytest.raises(PathViolationError):
            _extract_7z_safe(tmp_path / "malicioso.7z", tmp_path / "out")

        mock_run.assert_not_called()

    def test_rechaza_symlink_informado_por_py7zr_antes_de_extraer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Un miembro symlink perforaría el sandbox aunque su nombre fuese relativo."""
        szf = MagicMock()
        szf.list.return_value = [MagicMock(filename="enlace-seguro", is_symlink=True)]
        szf.__enter__.return_value = szf
        monkeypatch.setattr("py7zr.SevenZipFile", MagicMock(return_value=szf))
        mock_run = MagicMock()
        monkeypatch.setattr("subprocess.run", mock_run)

        with pytest.raises(PathViolationError, match="enlace simbólico"):
            _extract_7z_safe(tmp_path / "malicioso.7z", tmp_path / "out")

        szf.extractall.assert_not_called()
        mock_run.assert_not_called()

    def test_rechaza_el_archive_si_el_listado_de_7z_tiene_traversal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """py7zr falla ANTES de enumerar → el listado de 7z se valida igual.

        Este es el hueco real: con el header corrupto py7zr revienta al abrir, así
        que sus nombres nunca pasaron por ``_is_safe_path``. El fallback no puede
        confiar en la protección nativa de ``7z`` — valida el listado él mismo y
        aborta antes de escribir un solo byte.
        """
        _mockear_py7zr_roto(monkeypatch)
        mock_run = _mockear_7z_del_sistema(monkeypatch, miembros=["ok.txt", "../../evil.txt"])

        with pytest.raises(PathViolationError, match="listado de 7z"):
            _extract_7z_safe(tmp_path / "malicioso.7z", tmp_path / "out")

        assert [c.args[0][:2] for c in mock_run.call_args_list] == [["7z", "l"]], "no debe llegar a `7z x`"

    def test_rechaza_rutas_absolutas_en_el_listado(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Hermano del anterior: la ruta absoluta también escapa del destino."""
        _mockear_py7zr_roto(monkeypatch)
        mock_run = _mockear_7z_del_sistema(monkeypatch, miembros=[r"C:\Windows\System32\evil.dll"])

        with pytest.raises(PathViolationError):
            _extract_7z_safe(tmp_path / "malicioso.7z", tmp_path / "out")

        assert [c.args[0][:2] for c in mock_run.call_args_list] == [["7z", "l"]]

    def test_el_timeout_de_7z_se_traduce_a_toolinstallerror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Un 7z colgado no puede quedarse esperando para siempre."""
        import subprocess

        _mockear_py7zr_roto(monkeypatch)
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=subprocess.TimeoutExpired(cmd=["7z"], timeout=_SEVENZIP_TIMEOUT_SECONDS)),
        )

        with pytest.raises(ToolInstallError, match="timeout"):
            _extract_7z_safe(tmp_path / "dummy.7z", tmp_path / "out")

    def test_sin_7z_en_el_path_falla_con_mensaje_accionable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """py7zr no puede y no hay binario: el operador necesita saber qué instalar."""
        _mockear_py7zr_roto(monkeypatch)
        monkeypatch.setattr("subprocess.run", MagicMock(side_effect=FileNotFoundError("7z")))

        with pytest.raises(ToolInstallError, match="PATH"):
            _extract_7z_safe(tmp_path / "dummy.7z", tmp_path / "out")

    def test_falla_cerrado_si_el_listado_no_se_puede_parsear(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Listado sin el separador esperado → NO se extrae (fail-closed).

        Otra versión de 7z, otro locale, u otro formato de salida dejan la lista de
        miembros vacía. El loop de validación itera cero veces y `7z x` correría sin
        haber validado un solo nombre — el mismo bypass del sandbox, pero silencioso.
        No poder validar no es lo mismo que estar validado.
        """
        _mockear_py7zr_roto(monkeypatch)

        def _run(cmd: list[str], **kwargs: Any) -> MagicMock:
            resultado = MagicMock()
            resultado.stdout = b"7-Zip 21.07\nSalida inesperada sin separador\n"
            resultado.stderr = b""
            return resultado

        mock_run = MagicMock(side_effect=_run)
        monkeypatch.setattr("subprocess.run", mock_run)

        with pytest.raises(ToolInstallError, match="listado de 7z"):
            _extract_7z_safe(tmp_path / "dummy.7z", tmp_path / "out")

        assert [c.args[0][:2] for c in mock_run.call_args_list] == [["7z", "l"]], "no debe llegar a `7z x`"

    def test_falla_cerrado_si_el_listado_viene_vacio(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Separador presente pero sin ningún miembro: tampoco se extrae."""
        _mockear_py7zr_roto(monkeypatch)
        mock_run = _mockear_7z_del_sistema(monkeypatch, miembros=[])

        with pytest.raises(ToolInstallError, match="listado de 7z"):
            _extract_7z_safe(tmp_path / "dummy.7z", tmp_path / "out")

        assert [c.args[0][:2] for c in mock_run.call_args_list] == [["7z", "l"]]

    @pytest.mark.parametrize("clave_de_link", tools_installer._7Z_CLAVES_DE_LINK)
    def test_rechaza_los_enlaces_del_archive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, clave_de_link: str
    ) -> None:
        """Un enlace con Path seguro pero destino afuera → no se extrae.

        El destino del enlace no viaja en ``Path``, así que validar los nombres no
        dice nada sobre a dónde apunta: `7z x` lo materializa y después escribe a
        través de él, fuera de dest, con todos los nombres "seguros".

        Parametrizado SOBRE el tuple: agregar una clave de enlace nueva sin
        cubrirla no queda fuera de la cobertura por descuido.
        """
        _mockear_py7zr_roto(monkeypatch)

        listado = (
            "7-Zip 21.07\n--\nPath = dummy.7z\n\n----------\n"
            "Path = inocente.txt\nSize = 10\n\n"
            f"Path = enlace\nSize = 0\n{clave_de_link}../../../etc\n\n"
        )

        def _run(cmd: list[str], **kwargs: Any) -> MagicMock:
            resultado = MagicMock()
            resultado.stdout = listado.encode("utf-8") if cmd[1] == "l" else b""
            resultado.stderr = b""
            return resultado

        mock_run = MagicMock(side_effect=_run)
        monkeypatch.setattr("subprocess.run", mock_run)

        with pytest.raises(PathViolationError, match="enlace"):
            _extract_7z_safe(tmp_path / "malicioso.7z", tmp_path / "out")

        assert [c.args[0][:2] for c in mock_run.call_args_list] == [["7z", "l"]], "no debe llegar a `7z x`"

    def test_el_fallo_de_7z_se_traduce_a_toolinstallerror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """Código de salida != 0 (archive corrupto, disco lleno): el operador ve el stderr."""
        import subprocess

        _mockear_py7zr_roto(monkeypatch)
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(2, ["7z"], stderr=b"archive corrupto")),
        )

        with pytest.raises(ToolInstallError, match="archive corrupto"):
            _extract_7z_safe(tmp_path / "dummy.7z", tmp_path / "out")

    def test_el_fallo_de_7z_sin_stderr_no_revienta_al_formatear(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """``stderr`` es None cuando 7z muere sin escribir nada: no debe romper el mensaje."""
        import subprocess

        _mockear_py7zr_roto(monkeypatch)
        monkeypatch.setattr(
            "subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(2, ["7z"], stderr=None)),
        )

        with pytest.raises(ToolInstallError, match="listar"):
            _extract_7z_safe(tmp_path / "dummy.7z", tmp_path / "out")

    def test_rechaza_la_continuacion_disfrazada_de_clave_valor(self) -> None:
        """PoC del reviewer: la continuación del nombre PARECE un par clave-valor.

        El atacante elige el nombre del miembro, así que puede hacer que la línea
        que sigue al salto tenga forma ``Algo = valor`` y pase cualquier chequeo
        de forma. Lo que no puede evitar es llevar el traversal: sin ``..`` no
        escapa de dest y el ataque no sirve. Por eso se valida el VALOR de toda
        clave que no sea Path.
        """
        salida = "7-Zip\n--\nPath = dummy.7z\n\n----------\nPath = ok.txt\nSomeKey = ../../evil.txt\nSize = 12\n\n"
        with pytest.raises(ToolInstallError, match="traversal"):
            _parse_7z_listing(salida)

    def test_rechaza_la_continuacion_con_ruta_absoluta(self) -> None:
        """Hermano del anterior: el payload es absoluto en vez de relativo."""
        salida = "7-Zip\n--\nPath = dummy.7z\n\n----------\nPath = ok.txt\nSomeKey = C:\\Windows\\evil.dll\nSize = 1\n"
        with pytest.raises(ToolInstallError, match="traversal"):
            _parse_7z_listing(salida)

    def test_no_rechaza_valores_legitimos_con_puntos(self) -> None:
        """No debe haber falso positivo: versiones, CRCs y métodos pasan.

        Si esto rompiera, el fallback abortaría instalaciones válidas de xEdit —
        el escenario que este PR viene justo a arreglar.
        """
        salida = (
            "7-Zip\n--\nPath = dummy.7z\n\n----------\n"
            "Path = SSEEdit.exe\nSize = 1234\nCRC = A1B2C3D4\n"
            "Method = BCJ2 LZMA:20\nModified = 2024-01-02 03:04:05\nAttributes = A_ -rw-r--r--\n\n"
        )
        assert _parse_7z_listing(salida) == ["SSEEdit.exe"]

    def test_rechaza_una_linea_que_no_entiende(self) -> None:
        """Continuación de un nombre con salto de línea embebido → listado rechazado.

        El parser vería ``Path = ok.txt`` y validaría eso, mientras `7z x` escribe
        el nombre completo con el traversal. La línea suelta es la evidencia.
        """
        salida = "7-Zip\n--\nPath = dummy.7z\n\n----------\nPath = ok.txt\n../../evil\nSize = 1\n"
        with pytest.raises(ToolInstallError, match="Línea inesperada"):
            _parse_7z_listing(salida)

    def test_parsea_solo_los_miembros_del_listado(self) -> None:
        """El ``Path`` del encabezado es el archive mismo, no un miembro: se ignora."""
        salida = (
            "7-Zip 21.07\n"
            "Listing archive: dummy.7z\n"
            "\n"
            "--\n"
            "Path = dummy.7z\n"
            "Type = 7z\n"
            "\n"
            "----------\n"
            "Path = SSEEdit.exe\n"
            "Size = 1234\n"
            "\n"
            "Path = Edit Scripts/algo.pas\n"
            "Size = 10\n"
        )
        assert _parse_7z_listing(salida) == ["SSEEdit.exe", "Edit Scripts/algo.pas"]


# ---------------------------------------------------------------------------
# local_config
# ---------------------------------------------------------------------------


class TestLocalConfig:
    def test_load_defaults_when_file_missing(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.local_config import load

        cfg = load(tmp_path / "nonexistent.json")
        assert cfg.first_run is True
        assert cfg.loot_exe is None

    def test_save_and_load_roundtrip(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.local_config import LocalConfig, load, save

        cfg = LocalConfig(
            loot_exe="C:/LOOT/loot.exe",
            xedit_exe="C:/SSEEdit/SSEEdit.exe",
            first_run=False,
        )
        path = tmp_path / "config.json"
        save(cfg, path)

        loaded = load(path)
        assert loaded.loot_exe == "C:/LOOT/loot.exe"
        assert loaded.xedit_exe == "C:/SSEEdit/SSEEdit.exe"
        assert loaded.first_run is False

    def test_load_handles_corrupt_json(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.local.local_config import load

        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        cfg = load(path)
        assert cfg.first_run is True


# ---------------------------------------------------------------------------
# setup_tools tool (via AsyncToolRegistry)
# ---------------------------------------------------------------------------


class TestSetupToolsTool:
    @pytest.mark.asyncio
    async def test_setup_tools_installs_both(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.app.agent.tools import AsyncToolRegistry
        from sky_claw.app.db.async_registry import AsyncModRegistry
        from sky_claw.app.orchestrator.sync_engine import SyncEngine
        from sky_claw.app.scraper.masterlist import MasterlistClient
        from sky_claw.local.mo2.vfs import MO2Controller

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

        db = AsyncModRegistry(db_path=tmp_path / "test.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        from sky_claw.app.db.locks import DistributedLockManager

        lock_mgr = DistributedLockManager(
            tmp_path / "test_locks.db",
            default_ttl=60.0,
            max_retries=1,
            backoff_base=0.01,
            backoff_max=0.01,
        )
        # El try abre ANTES del initialize: si abre la conexion y despues falla,
        # el finally tiene que cerrar igual el manager y la DB.
        try:
            await lock_mgr.initialize()
            guard = HITLGuard(notify_fn=None, timeout=5)
            ti = ToolsInstaller(hitl=guard, gateway=gw, path_validator=validator, lock_manager=lock_mgr)

            install_dir = tmp_path / "tools"
            install_dir.mkdir()

            # Se crean los ejecutables de antemano para que ensure_loot/ensure_xedit los encuentren.
            loot_dir = install_dir / "LOOT"
            loot_dir.mkdir()
            (loot_dir / "loot.exe").write_text("fake", encoding="utf-8")

            xedit_dir = install_dir / "SSEEdit"
            xedit_dir.mkdir()
            (xedit_dir / "SSEEdit.exe").write_text("fake", encoding="utf-8")

            registry = AsyncToolRegistry(
                registry=db,
                mo2=mo2,
                sync_engine=sync_engine,
                hitl=guard,
                tools_installer=ti,
                install_dir=install_dir,
                gateway=gw,  # TASK-013 P1: Zero-Trust requires gateway
            )

            result_str = await registry.execute("setup_tools", {"tools": ["loot", "xedit"]})
            result = json.loads(result_str)

            assert result["loot"]["status"] == "already_installed"
            assert result["xedit"]["status"] == "already_installed"
            assert "loot.exe" in result["loot"]["exe_path"]
            assert "SSEEdit.exe" in result["xedit"]["exe_path"]
        finally:
            await lock_mgr.close()
            await db.close()

    @pytest.mark.asyncio
    async def test_setup_tools_no_installer_returns_error(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.app.agent.tools import AsyncToolRegistry
        from sky_claw.app.db.async_registry import AsyncModRegistry
        from sky_claw.app.orchestrator.sync_engine import SyncEngine
        from sky_claw.app.scraper.masterlist import MasterlistClient
        from sky_claw.local.mo2.vfs import MO2Controller

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

        db = AsyncModRegistry(db_path=tmp_path / "test.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        registry = AsyncToolRegistry(
            registry=db,
            mo2=mo2,
            sync_engine=sync_engine,
            # tools_installer=None (default)
        )

        result_str = await registry.execute("setup_tools", {})
        result = json.loads(result_str)
        assert result["success"] is False
        assert "not configured" in result["message"]
        assert set(result) == {"success", "message"}

        await db.close()

    @pytest.mark.asyncio
    async def test_setup_tools_unknown_tool(self, tmp_path: pathlib.Path) -> None:
        from sky_claw.app.agent.tools import AsyncToolRegistry
        from sky_claw.app.db.async_registry import AsyncModRegistry
        from sky_claw.app.orchestrator.sync_engine import SyncEngine
        from sky_claw.app.scraper.masterlist import MasterlistClient
        from sky_claw.local.mo2.vfs import MO2Controller

        gw = NetworkGateway(EgressPolicy(block_private_ips=False))
        validator = PathValidator(roots=[tmp_path])
        mo2 = MO2Controller(tmp_path, path_validator=validator)
        (tmp_path / "profiles" / "Default").mkdir(parents=True)
        (tmp_path / "profiles" / "Default" / "modlist.txt").write_text("", encoding="utf-8")

        db = AsyncModRegistry(db_path=tmp_path / "test.db")
        await db.open()

        masterlist = MasterlistClient(gateway=gw, api_key="fake")
        sync_engine = SyncEngine(mo2=mo2, masterlist=masterlist, registry=db)

        from sky_claw.app.db.locks import DistributedLockManager

        lock_mgr = DistributedLockManager(
            tmp_path / "test_locks2.db",
            default_ttl=60.0,
            max_retries=1,
            backoff_base=0.01,
            backoff_max=0.01,
        )
        # El try abre ANTES del initialize y cubre tambien el cierre de la DB:
        # un fallo de initialize() —o de registry.execute()— dejaba abiertos el
        # manager y la base.
        try:
            await lock_mgr.initialize()
            guard = HITLGuard(notify_fn=None, timeout=5)
            ti = ToolsInstaller(hitl=guard, gateway=gw, path_validator=validator, lock_manager=lock_mgr)

            registry = AsyncToolRegistry(
                registry=db,
                mo2=mo2,
                sync_engine=sync_engine,
                hitl=guard,
                tools_installer=ti,
                install_dir=tmp_path,
                gateway=gw,  # TASK-013 P1: Zero-Trust requires gateway
            )

            result_str = await registry.execute("setup_tools", {"tools": ["unknown_tool"]})

            result = json.loads(result_str)
            assert "unknown_tool" in result
            assert result["unknown_tool"]["success"] is False
            assert set(result["unknown_tool"]) == {"success", "message"}
        finally:
            await lock_mgr.close()
            await db.close()


# ---------------------------------------------------------------------------
# AppContext wiring — tools_installer present
# ---------------------------------------------------------------------------


class TestAppContextToolsInstaller:
    @pytest.mark.asyncio
    async def test_tools_installer_wired(self, tmp_path: pathlib.Path) -> None:
        import argparse

        from sky_claw.__main__ import AppContext  # type: ignore[attr-defined]

        args = argparse.Namespace(
            db_path=tmp_path / "test.db",
            mo2_root=tmp_path,
            loot_exe=pathlib.Path("loot.exe"),
            xedit_exe=None,
            operator_chat_id=None,
            staging_dir=tmp_path / "staging",
            provider=None,
            install_dir=tmp_path / "tools",
        )

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "test-key",
                "NEXUS_API_KEY": "",
                "TELEGRAM_BOT_TOKEN": "",
            },
        ):
            ctx = AppContext(args)
            await ctx.start()

        try:
            assert ctx.tools_installer is not None
        finally:
            await ctx.stop()


# ---------------------------------------------------------------------------
# _extract_zip_safe — zip-slip protection (A-03)
# ---------------------------------------------------------------------------


class TestExtractZipSafe:
    """Tests for zip-slip protection in _extract_zip_safe."""

    def test_normal_extraction_succeeds(self, tmp_path: pathlib.Path) -> None:
        """Un archivo zip limpio se extrae correctamente."""
        zip_path = tmp_path / "archive.zip"
        dest = tmp_path / "dest"
        dest.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readme.txt", "hello world")

        _extract_zip_safe(zip_path, dest)

        assert (dest / "readme.txt").exists()
        assert (dest / "readme.txt").read_text(encoding="utf-8") == "hello world"

    def test_dotdot_path_raises(self, tmp_path: pathlib.Path) -> None:
        """Zip con '../evil.txt' lanza PathViolationError."""
        zip_path = tmp_path / "malicious.zip"
        dest = tmp_path / "dest"
        dest.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil.txt", "evil content")

        with pytest.raises(PathViolationError):
            _extract_zip_safe(zip_path, dest)

        # No file should have escaped outside dest
        assert not (tmp_path / "evil.txt").exists()

    def test_absolute_path_raises(self, tmp_path: pathlib.Path) -> None:
        """Zip con ruta absoluta '/etc/passwd' lanza PathViolationError."""
        zip_path = tmp_path / "absolute.zip"
        dest = tmp_path / "dest"
        dest.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("/etc/passwd", "root:x:0:0")

        with pytest.raises(PathViolationError):
            _extract_zip_safe(zip_path, dest)

        # Verify NO file was extracted inside dest
        assert not any(dest.iterdir()), "No files should be extracted from malicious zip"

    def test_nested_normal_path_succeeds(self, tmp_path: pathlib.Path) -> None:
        """Un zip con 'subdir/file.txt' se extrae correctamente."""
        zip_path = tmp_path / "nested.zip"
        dest = tmp_path / "dest"
        dest.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("subdir/file.txt", "nested content")

        _extract_zip_safe(zip_path, dest)

        assert (dest / "subdir" / "file.txt").exists()
        assert (dest / "subdir" / "file.txt").read_text(encoding="utf-8") == "nested content"


# ---------------------------------------------------------------------------
# ToolsInstaller.ensure_skse
# ---------------------------------------------------------------------------


class TestEnsureSkse:
    @pytest.mark.asyncio
    async def test_retorna_existente_sin_descarga(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando SKSE ya está instalado, no descarga y devuelve already_existed=True."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        (install_dir / "skse64_1_6_1170.dll").write_text("fake", encoding="utf-8")
        (install_dir / "skse64_loader.exe").write_text("fake", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)

        from sky_claw.local.discovery.environment import SkyrimEdition

        result = await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert result.already_existed is True
        assert result.tool_name == "SKSE"
        assert result.exe_path == install_dir / "skse64_loader.exe"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "edition",
        [
            e
            for e in __import__("sky_claw.local.discovery.environment", fromlist=["SkyrimEdition"]).SkyrimEdition
            if e != __import__("sky_claw.local.discovery.environment", fromlist=["SkyrimEdition"]).SkyrimEdition.UNKNOWN
        ],
    )
    async def test_ediciones_soportadas(self, installer: ToolsInstaller, tmp_path: pathlib.Path, edition: Any) -> None:
        """Verifica que las ediciones conocidas pasen la validación inicial sin error de MS Store."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        session = MagicMock(spec=aiohttp.ClientSession)

        # Mock HITL to abort early but after edition check
        installer._hitl.request_approval = AsyncMock(return_value=Decision.DENIED)  # type: ignore[method-assign]

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_skse(install_dir, session, edition=edition)

    @pytest.mark.asyncio
    async def test_falla_con_edicion_ms_store(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Si la edición es UNKNOWN (MS Store), lanza error antes de la red sin necesidad de monkeypatch."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        session = MagicMock(spec=aiohttp.ClientSession)

        from sky_claw.local.discovery.environment import SkyrimEdition

        with pytest.raises(ToolInstallError, match="no es compatible"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.UNKNOWN)

    @pytest.mark.asyncio
    async def test_hitl_denial_raises(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Cuando el operador lo deniega, lanza ToolInstallError y conserva los DLLs existentes."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        session = MagicMock(spec=aiohttp.ClientSession)

        old_dll = install_dir / "skse64_1_5_97.dll"
        old_dll.write_text("viejo", encoding="utf-8")

        installer._hitl.request_approval = AsyncMock(return_value=Decision.DENIED)  # type: ignore[method-assign]

        from sky_claw.local.discovery.environment import SkyrimEdition

        with pytest.raises(ToolInstallError, match="denied"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert old_dll.exists(), "Debe conservar los DLLs existentes si el operador deniega la instalación."

    @pytest.mark.asyncio
    async def test_no_borra_el_dll_viejo_si_la_copia_falla(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Tercer hermano de la familia: la copia es el último paso antes de limpiar.

        Si `_copy_skse_files` falla (disco lleno, permisos a mitad de camino), el
        cleanup nunca corrió — el DLL de la edición vieja sigue ahí y el juego sigue
        arrancando con el SKSE que tenía, aunque la instalación nueva no haya cuajado.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()

        old_dll = install_dir / "skse64_1_5_97.dll"
        old_dll.write_text("viejo", encoding="utf-8")
        import stat

        old_dll.chmod(stat.S_IREAD)

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "extracted" / "root")  # type: ignore[method-assign]

        async def mock_copy_fail(*args, **kwargs):
            raise ToolInstallError("Simulated copy failure")

        installer._copy_skse_files = mock_copy_fail  # type: ignore[method-assign]

        from sky_claw.local.discovery.environment import SkyrimEdition

        with pytest.raises(ToolInstallError, match="Simulated copy failure"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert old_dll.exists(), "Una copia fallida no puede dejar el juego sin ningún SKSE"

    @pytest.mark.asyncio
    async def test_limpia_dlls_huerfanos_tras_una_copia_exitosa(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Camino feliz de la familia: recién con la copia OK se limpian los DLL huérfanos.

        Cubre además que un DLL de solo lectura no bloquea la limpieza.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()

        old_dll = install_dir / "skse64_1_5_97.dll"
        old_dll.write_text("viejo", encoding="utf-8")
        import stat

        old_dll.chmod(stat.S_IREAD)

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "extracted" / "root")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        from sky_claw.local.discovery.environment import SkyrimEdition

        await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert not old_dll.exists(), "Tras una copia exitosa, los DLL de la edición vieja deben limpiarse"

    @pytest.mark.asyncio
    async def test_no_borra_el_dll_viejo_si_la_extraccion_falla(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Un .7z corrupto no puede dejar al juego sin la versión vieja NI la nueva.

        Es el punto de no retorno del flujo: hasta que el payload no está extraído y
        validado en staging, el directorio del juego no se toca. Sin rollback, borrar
        antes de validar es pérdida de datos irreversible.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()

        old_dll = install_dir / "skse64_1_5_97.dll"
        old_dll.write_text("viejo", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]

        def extraccion_corrupta(*args: object, **kwargs: object) -> None:
            raise ToolInstallError("archivo 7z corrupto")

        installer._extract = MagicMock(side_effect=extraccion_corrupta)  # type: ignore[method-assign]

        from sky_claw.local.discovery.environment import SkyrimEdition

        with pytest.raises(ToolInstallError, match="corrupto"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert old_dll.exists(), "Una extracción fallida no puede dejar el juego sin ningún SKSE"

    @pytest.mark.asyncio
    async def test_no_borra_el_dll_viejo_si_el_payload_es_invalido(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Hermano del anterior: extrae bien pero la estructura no valida."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()

        old_dll = install_dir / "skse64_1_5_97.dll"
        old_dll.write_text("viejo", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]

        def root_invalido(*args: object, **kwargs: object) -> pathlib.Path:
            raise ToolInstallError("No se encontró skse64_loader.exe válido")

        installer._find_skse_root = MagicMock(side_effect=root_invalido)  # type: ignore[method-assign]

        from sky_claw.local.discovery.environment import SkyrimEdition

        with pytest.raises(ToolInstallError, match="loader"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert old_dll.exists(), "Un payload inválido no puede dejar el juego sin ningún SKSE"

    @pytest.mark.asyncio
    async def test_permiso_denegado_si_juego_abierto(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si un DLL está bloqueado (juego abierto), levanta un ToolInstallError accionable."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        old_dll = install_dir / "skse64_1_5_97.dll"
        old_dll.write_text("viejo", encoding="utf-8")

        session = MagicMock(spec=aiohttp.ClientSession)
        # Sin esto la aprobación sale DENIED y el test verificaba el mensaje equivocado
        # (moría en el HITL, no en la limpieza).
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "extracted" / "root")  # type: ignore[method-assign]
        # La copia corre antes que la limpieza; se mockea para llegar al escenario
        # bajo prueba (limpieza bloqueada) sin depender de un payload real en disco.
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        # Acotado al DLL del juego: parchear `pathlib.Path.unlink` global también
        # rompería el cleanup del TemporaryDirectory del staging.
        original_unlink = pathlib.Path.unlink

        def unlink_mock(self: pathlib.Path, *args: object, **kwargs: object) -> None:
            if self.name == "skse64_1_5_97.dll":
                raise PermissionError("Access denied")
            original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "unlink", unlink_mock)

        from sky_claw.local.discovery.environment import SkyrimEdition

        with pytest.raises(ToolInstallError, match="Permiso denegado al limpiar"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

    @pytest.mark.asyncio
    async def test_descarga_y_extrae_correctamente(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Flujo feliz: descarga, extrae excluyendo MAC OSX, y copia Data y binarios."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]

        # Se asigna sobre la INSTANCIA, así que no recibe self: firmarla con `self_obj`
        # la dejaba pidiendo un argumento que nadie pasa.
        def mock_extract(archive: pathlib.Path, dest: pathlib.Path) -> None:
            dest.mkdir(parents=True, exist_ok=True)
            skse_dir = dest / "skse64_2_02_06"
            skse_dir.mkdir()
            (skse_dir / "skse64_loader.exe").write_text("loader", encoding="utf-8")
            (skse_dir / "skse64_1_6_1170.dll").write_text("dll", encoding="utf-8")
            # Simulamos el Data
            data_dir = skse_dir / "Data"
            data_dir.mkdir()
            (data_dir / "scripts").mkdir()
            (data_dir / "scripts" / "test.pex").write_text("pex", encoding="utf-8")

        installer._extract = mock_extract  # type: ignore[method-assign]

        from sky_claw.local.discovery.environment import SkyrimEdition

        res = await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert res.tool_name == "SKSE"
        assert res.exe_path == install_dir / "skse64_loader.exe"
        assert res.already_existed is False
        assert (install_dir / "skse64_1_6_1170.dll").exists()
        assert (install_dir / "Data" / "scripts" / "test.pex").exists()

    @pytest.mark.asyncio
    async def test_descarga_falla_hash(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La validación de hash rechaza payloads modificados."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        session = MagicMock(spec=aiohttp.ClientSession)

        # Patch config with bad hash. SKSE_CONFIG es constante de MÓDULO, no atributo
        # de clase: `installer.__class__.SKSE_CONFIG` levanta AttributeError y el test
        # moría en el setup sin llegar nunca a validar un hash.
        monkeypatch.setitem(tools_installer.SKSE_CONFIG["AE"], "sha256", "DEADBEEF")
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]

        # Simular descarga de "basura". `request` se AWAITEA: con MagicMock devuelve un
        # Mock no-awaitable y el test explota antes de llegar a comparar el hash.
        installer._gateway = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(return_value=None)
        mock_resp.release = MagicMock(return_value=None)
        mock_resp.content.iter_chunked.return_value = _async_iter([b"fake data"])
        installer._gateway.request = AsyncMock(return_value=mock_resp)

        from sky_claw.local.discovery.environment import SkyrimEdition

        with pytest.raises(ToolInstallError, match="Validación de hash fallida"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

    @pytest.mark.asyncio
    async def test_encuentra_loader_ambiguo(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Si hay múltiples loaders, lanza error."""
        extract_path = tmp_path / "extracted"
        extract_path.mkdir(parents=True)
        (extract_path / "dir1").mkdir()
        (extract_path / "dir2").mkdir()

        cfg = {"loader": "skse64_loader.exe", "dll": "skse64_1_6_1170.dll"}

        (extract_path / "dir1" / "skse64_loader.exe").write_text("a")
        (extract_path / "dir2" / "skse64_loader.exe").write_text("b")

        with pytest.raises(ToolInstallError, match="ambigua"):
            installer._find_skse_root(extract_path, cfg)

    @pytest.mark.asyncio
    async def test_falta_dll_en_payload(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """Si está el loader pero no el dll, lanza error."""
        extract_path = tmp_path / "extracted"
        skse_root = extract_path / "skse"
        skse_root.mkdir(parents=True)

        cfg = {"loader": "skse64_loader.exe", "dll": "skse64_1_6_1170.dll"}
        (skse_root / "skse64_loader.exe").write_text("a")
        # falta dll

        with pytest.raises(ToolInstallError, match="incompleto"):
            installer._find_skse_root(extract_path, cfg)

    def test_toda_edicion_del_enum_resuelve_o_falla_cerrado(self, installer: ToolsInstaller) -> None:
        """Ninguna edición puede caer silenciosamente al payload de otra.

        Se recorre el enum COMPLETO en vez de muestrear AE/SE/LE. La propiedad es
        binaria: cada miembro o resuelve a una clave que existe en SKSE_CONFIG, o
        levanta. Agregar `VR` al enum sin su payload cae en la segunda rama —que es
        el desenlace correcto— en vez de instalarle el `skse64_1_6_1170.dll` de AE a
        un usuario de VR, que es lo que hacía el `mapping.get(edition, "AE")` viejo.
        """
        for edition in SkyrimEdition:
            if edition is SkyrimEdition.UNKNOWN:
                with pytest.raises(ToolInstallError, match="UNKNOWN"):
                    installer._edition_to_config_key(edition)
                continue
            try:
                key = installer._edition_to_config_key(edition)
            except ToolInstallError as exc:
                assert "no tiene payload" in str(exc)
                continue
            assert key in tools_installer.SKSE_CONFIG, f"{edition} mapea a una clave inexistente"

    def test_cada_payload_declara_dll_y_loader_coherentes(self) -> None:
        """El triplete URL/DLL/loader de cada edición tiene que ser internamente consistente."""
        for key, cfg in tools_installer.SKSE_CONFIG.items():
            assert cfg["url"].startswith("https://skse.silverlock.org/"), key
            assert cfg["dll"].endswith(".dll"), key
            assert cfg["loader"].endswith("_loader.exe"), key
            # LE usa la familia `skse_`; SE/AE la familia `skse64_`.
            familia = "skse64_" if key in {"AE", "SE"} else "skse_"
            assert cfg["dll"].startswith(familia), f"{key}: dll de otra familia"
            assert cfg["loader"].startswith(familia), f"{key}: loader de otra familia"

    def test_el_host_de_skse_esta_habilitado_en_el_egress(self) -> None:
        """Sin la entrada en ALLOWED_HOSTS la descarga muere DESPUÉS de la aprobación HITL.

        La pertenencia se escribe como inclusión de conjuntos y no como
        ``"skse.silverlock.org" in ALLOWED_HOSTS``: CodeQL lee ese ``in`` como el patrón
        de saneamiento de URLs por substring (``py/incomplete-url-substring-sanitization``)
        y lo reporta, aunque acá el operando derecho sea un ``frozenset`` de hosts y la
        comparación sea exacta. La aserción es la misma; solo cambia la forma de decirla.
        """
        from sky_claw.config import ALLOWED_HOSTS, ALLOWED_METHODS

        assert {"skse.silverlock.org"} <= ALLOWED_HOSTS
        assert ALLOWED_METHODS["skse.silverlock.org"] == frozenset(["GET"])

    @pytest.mark.asyncio
    async def test_version_reporta_el_build_completo(self, installer: ToolsInstaller, tmp_path: pathlib.Path) -> None:
        """La versión sale del stem del archivo, no del último segmento entre guiones bajos."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "root")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        res = await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert res.version == "skse64_2_02_06"

    @pytest.mark.asyncio
    async def test_edicion_se_deriva_del_pe_no_de_los_dll_presentes(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """En una instalación SE limpia hay que bajar el SKSE de SE, no el de AE.

        Es el caso que el autodetect viejo erraba siempre: sin DLL de SKSE en disco
        —el único escenario en que la instalación corre— defaulteaba a AE.

        La versión se stubea junto con la edición: el `SkyrimSE.exe` de texto de este
        fixture no es un PE, así que `read_skyrim_version` devuelve "" y el gate de
        runtime corta antes de elegir payload. Sin el stub, el test pasaba por la rama
        fail-open que este PR cierra, no por la propiedad que dice medir.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        (install_dir / "SkyrimSE.exe").write_text("pe", encoding="utf-8")

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.SE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.5.97")

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "root")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        res = await installer.ensure_skse(install_dir, session)

        assert res.version == "skse64_2_00_20", "SE debe recibir el payload de SE"
        assert res.exe_path == install_dir / "skse64_loader.exe"

    @pytest.mark.asyncio
    async def test_bloquea_si_el_build_exacto_no_coincide(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dos AE pueden compartir edición y tener DLL de SKSE incompatibles entre sí.

        `SKSE_CONFIG["AE"]` solo targetea 1.6.1170; un downgrade a un 1.6.x más viejo
        (1.6.640 es un pin común para compatibilidad de mods) sigue clasificando como
        AE por edición, pero el DLL de 1.6.1170 no carga sobre ese runtime.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        (install_dir / "SkyrimSE.exe").write_text("pe", encoding="utf-8")

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.640")

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]

        with pytest.raises(ToolInstallError, match="1.6.640") as exc_info:
            await installer.ensure_skse(install_dir, session)

        assert "1.6.1170" in str(exc_info.value), "El mensaje debe nombrar ambas versiones para ser accionable"

    @pytest.mark.asyncio
    async def test_tolera_un_cuarto_segmento_de_build_en_la_version_detectada(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`1.6.1170.0` (con build) sigue siendo compatible con el DLL targeteado a `1.6.1170`."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        (install_dir / "SkyrimSE.exe").write_text("pe", encoding="utf-8")

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.1170.0")

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "root")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        res = await installer.ensure_skse(install_dir, session)

        assert res.version == "skse64_2_02_06"

    @pytest.mark.asyncio
    async def test_edicion_explicita_no_dispara_el_gate_de_version(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Un override explícito de edición no exige un .exe real en disco.

        Cubre el resto de la suite: casi todos los tests de esta clase pasan
        `edition=` sin crear `SkyrimSE.exe`, y siguen debiendo funcionar.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "root")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        res = await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        assert res.version == "skse64_2_02_06"

    # ── TOCTOU: el runtime puede cambiar entre el gate y la copia ────────────
    #
    # El gate pre-HITL prueba compatibilidad al ARRANCAR la operación. Entre ese
    # punto y la primera escritura al directorio del juego pasan tres cosas que
    # duran: la espera del HITL (el repo tiene precedente de timeouts de horas),
    # la descarga y la extracción. Steam puede actualizar Skyrim en esa ventana.
    #
    # Sin revalidar, el contrato demostrado es "el runtime ERA compatible cuando
    # empecé", no "lo SIGUE siendo cuando escribo" — y lo que decide si el DLL
    # carga es lo segundo.

    def _instalador_hasta_la_copia(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> tuple[AsyncMock, AsyncMock]:
        """Mockea el tramo HITL→download→extract y devuelve los espías de mutación.

        Devuelve `(copy, cleanup)`: las dos únicas operaciones que tocan el
        directorio del juego. Todo lo anterior (sandbox, archive, extracción) vive
        en staging temporal, así que mockearlo no oculta ninguna mutación.
        """
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "root")  # type: ignore[method-assign]
        copy = AsyncMock(return_value=None)
        cleanup = AsyncMock(return_value=None)
        installer._copy_skse_files = copy  # type: ignore[method-assign]
        installer._cleanup_orphaned_skse_dlls = cleanup  # type: ignore[method-assign]
        return copy, cleanup

    def _runtime_que_cambia_durante_la_descarga(
        self,
        installer: ToolsInstaller,
        monkeypatch: pytest.MonkeyPatch,
        *,
        inicial: str,
        despues: str,
    ) -> None:
        """Steam actualiza Skyrim MIENTRAS Sky-Claw descarga el payload.

        El cambio se dispara dentro de `_download_skse_archive` a propósito, en vez
        de contar llamadas a `read_skyrim_version`: así lo que el test mide es
        DÓNDE está el gate y no cuántas veces lee. Un gate colocado antes de la
        descarga leería todavía `inicial`, aprobaría, y la copia ocurriría — que es
        exactamente el defecto que este contrato cierra y el motivo por el que
        revalidar "en algún lugar después del primero" no alcanza.
        """
        version = {"actual": inicial}
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: version["actual"])

        async def _descargar_y_actualizar_el_juego(*_args: object, **_kwargs: object) -> None:
            version["actual"] = despues

        installer._download_skse_archive = AsyncMock(  # type: ignore[method-assign]
            side_effect=_descargar_y_actualizar_el_juego
        )

    @pytest.mark.asyncio
    async def test_runtime_que_cambia_entre_el_gate_y_la_copia_aborta(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Steam actualiza Skyrim mientras esperábamos el HITL: no se escribe nada.

        El payload que se bajó es el de 1.6.1170 y el juego ahora corre 1.6.640;
        copiarlo deja un DLL que no carga. La ventana es real: entre el gate y la
        copia hay una espera de operador más una descarga.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        copy, cleanup = self._instalador_hasta_la_copia(installer, tmp_path)
        self._runtime_que_cambia_durante_la_descarga(installer, monkeypatch, inicial="1.6.1170", despues="1.6.640")
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="1.6.640") as exc_info:
            await installer.ensure_skse(install_dir, session)

        assert "1.6.1170" in str(exc_info.value), "el mensaje nombra la versión que el payload targetea"
        copy.assert_not_awaited(), "la copia es la primera mutación del juego: no puede ocurrir"
        cleanup.assert_not_awaited(), "el cleanup borra DLLs del juego: tampoco"
        assert set(install_dir.iterdir()) == antes

    @pytest.mark.asyncio
    async def test_runtime_que_se_vuelve_ilegible_antes_de_la_copia_aborta(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si la segunda lectura no prueba nada, vale lo mismo que un mismatch.

        Un PE que dejó de parsear a mitad de la operación —actualización en curso,
        archivo a medio escribir— no autoriza a escribir: la propiedad es PODER
        PROBAR la compatibilidad, y acá dejó de poderse.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        copy, cleanup = self._instalador_hasta_la_copia(installer, tmp_path)
        self._runtime_que_cambia_durante_la_descarga(installer, monkeypatch, inicial="1.6.1170", despues="")
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="dejó de poder verificarse"):
            await installer.ensure_skse(install_dir, session)

        copy.assert_not_awaited()
        cleanup.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes

    @pytest.mark.asyncio
    async def test_ejecutable_que_desaparece_antes_de_la_copia_aborta(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El juego se desinstaló o se movió durante la operación: no escribir ahí.

        Distinto de "nunca hubo ejecutable" (staging con edición explícita, que
        sigue permitido): acá SÍ había uno cuando arrancamos y ya no está, así que
        el directorio dejó de ser una instalación de Skyrim que podamos probar.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        exe = install_dir / "SkyrimSE.exe"
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.1170")

        copy, cleanup = self._instalador_hasta_la_copia(installer, tmp_path)

        # El .exe se borra DESPUÉS del gate inicial: la extracción es el último
        # paso antes de la copia, así que ahí se simula el proceso externo.
        def _extraer_y_desaparecer(*_args: object, **_kwargs: object) -> None:
            exe.unlink()

        installer._extract = MagicMock(side_effect=_extraer_y_desaparecer)  # type: ignore[method-assign]

        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="ya no encuentro|desapareció"):
            await installer.ensure_skse(install_dir, session)

        copy.assert_not_awaited()
        cleanup.assert_not_awaited()
        # El ancla resta el `.exe` porque el propio test lo borra para simular al
        # proceso externo: omitirla por esa complicación dejaría este test sin la
        # comprobación que sus hermanos sí tienen, que es cómo se cuela un hermano
        # sin cablear.
        assert set(install_dir.iterdir()) == antes - {exe}, "cero mutaciones más allá del exe que borró el test"

    @pytest.mark.asyncio
    async def test_lectura_de_version_que_explota_se_traduce_a_error_de_dominio(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una excepción del parser no puede escaparse cruda del gate.

        `_read_pe_product_version` captura `OSError`/`ValueError`/`PEFormatError`, y
        con eso alcanza para el archivo inexistente, el PE corrupto y el truncado
        —los tres devuelven `""`—. Pero `pefile` levanta un `Exception` PELADO en al
        menos un caso de acceso (ruta que resulta ser un directorio), y ese no cae en
        ninguna de las tres ramas. `_leer_version_del_ejecutable` mira `is_file()`
        antes de leer, así que hace falta que la ruta cambie entre ambas cosas — una
        ventana chica, pero es exactamente la clase de carrera que este gate existe
        para cubrir, y el gate corre justo cuando el juego se está actualizando.

        El contrato del método es `ToolInstallError`. Que escape un `Exception` crudo
        conserva el fail-closed (nada se copia) pero le da al operador el texto
        interno del parser en vez de un mensaje accionable, y rompe el contrato de
        errores tipados del que depende la GUI.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        def _parser_que_explota(_exe: pathlib.Path) -> str:
            raise Exception("Unable to access file: [Errno 21] Is a directory")

        monkeypatch.setattr(tools_installer, "read_skyrim_version", _parser_que_explota)

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_un_bug_del_parser_no_se_disfraza_de_runtime_incompatible(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La contracara: un defecto NUESTRO tiene que propagarse, no traducirse.

        Traducir todo a "versión ilegible" haría que un `TypeError` por cambio de
        firma o un `AttributeError` por una versión nueva de `pefile` le lleguen al
        operador como "tu Skyrim no se puede verificar" — culpando al juego del
        jugador por un bug de Sky-Claw, con el traceback real enterrado en un
        warning. El operador no podría distinguir un problema suyo de uno nuestro.

        La distinción es por tipo EXACTO: `pefile` levanta `Exception` pelado, y
        cualquier subtipo es código nuestro fallando.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        def _bug_nuestro(_exe: pathlib.Path) -> str:
            raise TypeError("read_skyrim_version() got an unexpected keyword argument")

        monkeypatch.setattr(tools_installer, "read_skyrim_version", _bug_nuestro)

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(TypeError, match="unexpected keyword argument"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        # Que propague no alcanza: tiene que propagar SIN haber cruzado ninguna
        # frontera. Una regresión que pidiera aprobación, bajara el payload y recién
        # ahí explotara seguiría propagando el TypeError y pasaría este test.
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_el_parser_que_explota_tambien_se_traduce_en_autodeteccion(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El camino gemelo: sin `edition`, el mismo PE roto da el mismo error tipado.

        La traducción vivió primero solo en `_leer_version_del_ejecutable`, que usa la
        rama de edición explícita; `_detect_skyrim_edition_from_exe` leía el mismo
        parser sin traducir. Con `edition=None` la excepción cruda escapaba de
        `ensure_skse`: fallaba cerrado, pero rompía el contrato de errores tipados en
        exactamente la mitad de los casos — y los dos tests del parser pasaban
        `edition=AE`, así que ninguno lo tocaba.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        def _parser_que_explota(_exe: pathlib.Path) -> str:
            raise Exception("Unable to access file: [Errno 21] Is a directory")

        monkeypatch.setattr(tools_installer, "read_skyrim_version", _parser_que_explota)
        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", _parser_que_explota)

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_un_bug_nuestro_tampoco_se_disfraza_en_autodeteccion(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Y su contracara en el gemelo: el subtipo propaga por los DOS caminos."""
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        def _bug_nuestro(_exe: pathlib.Path) -> str:
            raise AttributeError("module 'pefile' has no attribute 'PE'")

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", _bug_nuestro)

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(AttributeError, match="no attribute 'PE'"):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_runtime_estable_llega_a_la_copia(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Camino feliz: si el runtime no cambió, la revalidación no estorba."""
        install_dir = self._skyrim_limpio(tmp_path)

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.1170")

        copy, cleanup = self._instalador_hasta_la_copia(installer, tmp_path)
        session = MagicMock(spec=aiohttp.ClientSession)

        res = await installer.ensure_skse(install_dir, session)

        assert res.version == "skse64_2_02_06"
        copy.assert_awaited_once()
        cleanup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_la_revalidacion_no_reemplaza_al_gate_pre_hitl(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Los dos gates son distintos y ambos tienen que seguir existiendo.

        Un runtime incompatible desde el arranque tiene que cortar ANTES de pedir
        aprobación y de gastar egress — no llegar hasta la revalidación. Sin este
        ancla, mover el gate inicial al lugar del segundo pasaría en verde y le
        costaría al operador una aprobación y una descarga inútiles.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.640")

        hitl, egress = self._espias_de_frontera(installer)
        descarga = AsyncMock(return_value=None)
        installer._download_skse_archive = descarga  # type: ignore[method-assign]
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="1.6.640"):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        descarga.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    def test_todo_test_de_aborto_ancla_sus_fronteras(self) -> None:
        """Ancla enumerativa: un test de aborto nuevo no puede entrar sin sus fronteras.

        Esta comprobación existe porque el modo manual falló TRES veces en este mismo
        PR: cada vez que se sumó un test de aborto a una de las dos familias, quedó
        sin alguna de las aserciones que sus hermanos ya tenían, y lo encontró un
        revisor automático en vez de la suite. Auditar a mano no escala; enumerar sí.

        La regla, por familia de helper:

        * ``_espias_de_frontera``      -> HITL y egress no invocados
        * ``_instalador_hasta_la_copia`` -> copia y cleanup no invocados
        * ambas                        -> el directorio del juego queda igual

        Un test se considera "de aborto" si espera una excepción (`pytest.raises`).
        El camino feliz no entra: verifica que la copia SÍ ocurre, no su ausencia.

        Se enumeran los tests `async` y los sincrónicos: los dos helpers son
        sincrónicos, así que un test sync que maneje su propio bucle de eventos
        también puede provocar un aborto y debe anclar sus fronteras igual.
        Esta misma función queda excluida por nombre dinámico: su fuente contiene
        las marcas exigidas como literales, así que se cumpliría a sí misma y
        —peor— mantendría `revisados` no vacío aunque desaparecieran todos los
        tests de aborto reales, desactivando la comprobación de liveness.
        """
        import ast  # noqa: PLC0415 — solo lo usa esta ancla
        import inspect  # noqa: PLC0415 — solo para excluirse a sí misma sin hardcodear el nombre

        fuente = pathlib.Path(__file__).read_text(encoding="utf-8")
        arbol = ast.parse(fuente)

        clase = next(
            nodo for nodo in ast.walk(arbol) if isinstance(nodo, ast.ClassDef) and nodo.name == "TestEnsureSkse"
        )

        requisitos_por_helper = {
            "_espias_de_frontera": ("hitl.assert_not_awaited()", "egress.assert_not_awaited()"),
            "_instalador_hasta_la_copia": ("copy.assert_not_awaited()", "cleanup.assert_not_awaited()"),
        }

        incumplen: dict[str, list[str]] = {}
        revisados: list[str] = []

        marco = inspect.currentframe()
        assert marco is not None
        esta_ancla = marco.f_code.co_name

        for metodo in clase.body:
            if not isinstance(metodo, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if not metodo.name.startswith("test_") or metodo.name == esta_ancla:
                continue
            cuerpo = ast.get_source_segment(fuente, metodo) or ""
            helpers = [h for h in requisitos_por_helper if h in cuerpo]
            if not helpers or "pytest.raises" not in cuerpo:
                continue

            revisados.append(metodo.name)
            faltan = [marca for h in helpers for marca in requisitos_por_helper[h] if marca not in cuerpo]
            if "iterdir()) == antes" not in cuerpo:
                faltan.append("comparación del directorio contra su estado inicial")
            if faltan:
                incumplen[metodo.name] = faltan

        assert revisados, "el ancla dejó de encontrar tests de aborto: revisá los nombres de los helpers"
        assert not incumplen, "tests de aborto sin anclar todas sus fronteras: " + "; ".join(
            f"{nombre} (falta {', '.join(marcas)})" for nombre, marcas in incumplen.items()
        )

    def test_skse_dll_game_version_reconstruye_el_build_del_nombre(self) -> None:
        assert tools_installer._skse_dll_game_version("skse64_1_6_1170.dll") == "1.6.1170"
        assert tools_installer._skse_dll_game_version("skse64_1_5_97.dll") == "1.5.97"
        assert tools_installer._skse_dll_game_version("skse_1_9_32.dll") == "1.9.32"

    def test_game_version_matches_tolera_build_extra_no_mismatch_real(self) -> None:
        matches = tools_installer._game_version_matches
        assert matches("1.6.1170", "1.6.1170") is True
        assert matches("1.6.1170.0", "1.6.1170") is True, "un 4to segmento de build no es un mismatch"
        assert matches("1.6.640", "1.6.1170") is False
        assert matches("1.5.97", "1.6.1170") is False

    @pytest.mark.asyncio
    async def test_sin_ejecutable_del_juego_no_adivina_la_edicion(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Sin exe no hay edición: cortar es preferible a instalar el payload equivocado."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()

        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="No encontré el ejecutable"):
            await installer.ensure_skse(install_dir, session)

    # ── Payload mal configurado: sin `assert` ────────────────────────────────

    @pytest.mark.asyncio
    async def test_payload_sin_dll_falla_con_el_error_del_contrato(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un campo faltante en SKSE_CONFIG corta con ToolInstallError, no con AssertionError.

        Los `assert` que validaban esto se compilan fuera con `python -O`: ahí el
        payload incompleto llegaba hasta `install_dir / None` (TypeError críptico)
        DESPUÉS de haber consumido la aprobación del operador.
        """
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        monkeypatch.setitem(tools_installer.SKSE_CONFIG["AE"], "dll", None)

        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="falta el campo obligatorio 'dll'"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

    def test_campo_obligatorio_no_depende_de_assert(self) -> None:
        """Ancla directa del helper: mismo comportamiento corra o no con `-O`."""
        with pytest.raises(ToolInstallError, match="falta el campo obligatorio 'url'"):
            tools_installer._skse_cfg_field({"url": None}, "url")

        assert tools_installer._skse_cfg_field({"url": "https://x/y.7z"}, "url") == "https://x/y.7z"

    # ── Destinos de copia: symlinks y verificación post-copia ────────────────

    def _payload(self, tmp_path: pathlib.Path, *, con_dll: bool = True) -> pathlib.Path:
        """Payload extraído mínimo: loader + DLL de runtime en la raíz."""
        skse_root = tmp_path / "payload"
        skse_root.mkdir(parents=True, exist_ok=True)
        (skse_root / "skse64_loader.exe").write_bytes(b"MZ-loader")
        if con_dll:
            (skse_root / "skse64_1_6_1170.dll").write_bytes(b"MZ-dll")
        return skse_root

    @pytest.mark.asyncio
    async def test_rechaza_destino_symlink_que_apunta_afuera(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """`shutil.copy2` sigue el link: escribiría fuera del directorio validado."""
        game_dir = tmp_path / "skyrim"
        game_dir.mkdir()
        afuera = tmp_path / "afuera.dll"
        afuera.write_bytes(b"victima")
        try:
            (game_dir / "skse64_1_6_1170.dll").symlink_to(afuera)
        except (OSError, NotImplementedError):
            pytest.skip("crear symlinks requiere privilegios no disponibles en este entorno")

        with pytest.raises(ToolInstallError, match="enlace"):
            await installer._copy_skse_files(self._payload(tmp_path), game_dir, tools_installer.SKSE_CONFIG["AE"])

        assert afuera.read_bytes() == b"victima", "el archivo apuntado no debió tocarse"

    @pytest.mark.asyncio
    async def test_rechaza_destino_symlink_aunque_apunte_adentro(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Un link a otro archivo DENTRO del juego igual sobrescribe algo que no es el destino."""
        game_dir = tmp_path / "skyrim"
        game_dir.mkdir()
        vecino = game_dir / "otro.dll"
        vecino.write_bytes(b"vecino")
        try:
            (game_dir / "skse64_1_6_1170.dll").symlink_to(vecino)
        except (OSError, NotImplementedError):
            pytest.skip("crear symlinks requiere privilegios no disponibles en este entorno")

        with pytest.raises(ToolInstallError, match="enlace"):
            await installer._copy_skse_files(self._payload(tmp_path), game_dir, tools_installer.SKSE_CONFIG["AE"])

        assert vecino.read_bytes() == b"vecino"

    @pytest.mark.asyncio
    async def test_rechaza_data_cuyo_ancestro_es_un_symlink(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Validar la ruta sin resolver deja pasar `Data/` linkeado afuera del juego."""
        game_dir = tmp_path / "skyrim"
        game_dir.mkdir()
        afuera = tmp_path / "data_afuera"
        afuera.mkdir()
        try:
            (game_dir / "Data").symlink_to(afuera, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("crear symlinks requiere privilegios no disponibles en este entorno")

        skse_root = self._payload(tmp_path)
        scripts = skse_root / "Data" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "Actor.pex").write_bytes(b"pex")

        with pytest.raises(ToolInstallError, match="enlace"):
            await installer._copy_skse_files(skse_root, game_dir, tools_installer.SKSE_CONFIG["AE"])

        assert not (afuera / "Scripts").exists(), "no debió escribir fuera del directorio del juego"

    @pytest.mark.asyncio
    async def test_copia_normal_deja_loader_y_dll_en_destino(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """El camino feliz sigue funcionando con las validaciones nuevas."""
        game_dir = tmp_path / "skyrim"
        game_dir.mkdir()
        skse_root = self._payload(tmp_path)
        scripts = skse_root / "Data" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "Actor.pex").write_bytes(b"pex")

        await installer._copy_skse_files(skse_root, game_dir, tools_installer.SKSE_CONFIG["AE"])

        assert (game_dir / "skse64_loader.exe").read_bytes() == b"MZ-loader"
        assert (game_dir / "skse64_1_6_1170.dll").read_bytes() == b"MZ-dll"
        assert (game_dir / "Data" / "Scripts" / "Actor.pex").read_bytes() == b"pex"

    @pytest.mark.asyncio
    async def test_falla_si_el_dll_de_runtime_no_quedo_en_destino(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Contar archivos copiados no alcanza: sin el DLL del build, SKSE no carga.

        Un payload con el loader y un `Data/` poblado deja `copied_count > 0` y
        reportaba instalación exitosa aunque el runtime DLL nunca llegara al juego.
        """
        game_dir = tmp_path / "skyrim"
        game_dir.mkdir()
        skse_root = self._payload(tmp_path, con_dll=False)
        scripts = skse_root / "Data" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "Actor.pex").write_bytes(b"pex")

        with pytest.raises(ToolInstallError, match="skse64_1_6_1170.dll"):
            await installer._copy_skse_files(skse_root, game_dir, tools_installer.SKSE_CONFIG["AE"])

    # ── Descarga acotada en bytes ────────────────────────────────────────────

    def _mock_stream(self, installer: ToolsInstaller, chunks: list[bytes], content_length: str | None = None) -> None:
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)
        resp.release = MagicMock(return_value=None)
        resp.headers = {} if content_length is None else {"Content-Length": content_length}
        resp.content.iter_chunked.return_value = _async_iter(chunks)
        installer._gateway = MagicMock()
        installer._gateway.request = AsyncMock(return_value=resp)

    @pytest.mark.asyncio
    async def test_aborta_si_el_stream_excede_el_techo_de_bytes(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El timeout acota el TIEMPO, no los BYTES: un origen comprometido llenaría el disco."""
        monkeypatch.setattr(tools_installer, "_SKSE_MAX_ARCHIVE_BYTES", 1024)
        dest = tmp_path / "skse.7z"
        session = MagicMock(spec=aiohttp.ClientSession)
        self._mock_stream(installer, [b"x" * 600, b"y" * 600, b"z" * 600])

        with pytest.raises(ToolInstallError, match="excede el tamaño máximo"):
            await installer._download_skse_archive(session, tools_installer.SKSE_CONFIG["AE"], dest)

        assert not dest.exists(), "el parcial tiene que borrarse"

    @pytest.mark.asyncio
    async def test_rechaza_content_length_desmedido_antes_de_leer_el_cuerpo(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el origen ya declara un tamaño imposible, no hace falta bajar nada."""
        monkeypatch.setattr(tools_installer, "_SKSE_MAX_ARCHIVE_BYTES", 1024)
        dest = tmp_path / "skse.7z"
        session = MagicMock(spec=aiohttp.ClientSession)
        self._mock_stream(installer, [b"x" * 10], content_length=str(50 * 1024))

        with pytest.raises(ToolInstallError, match="excede el tamaño máximo"):
            await installer._download_skse_archive(session, tools_installer.SKSE_CONFIG["AE"], dest)

        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_descarga_dentro_del_techo_pasa(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El techo no puede romper la descarga legítima."""
        monkeypatch.setitem(tools_installer.SKSE_CONFIG["AE"], "sha256", None)
        dest = tmp_path / "skse.7z"
        session = MagicMock(spec=aiohttp.ClientSession)
        self._mock_stream(installer, [b"payload"], content_length="7")

        await installer._download_skse_archive(session, tools_installer.SKSE_CONFIG["AE"], dest)

        assert dest.read_bytes() == b"payload"

    # ── Gate de runtime: probar la compatibilidad ANTES de HITL / egress / copia ──
    #
    # SKSE está pinneado al BUILD exacto del ejecutable. El gate de mismatch ya
    # existía, pero solo corría con `detected_version` no vacía: la rama de versión
    # ILEGIBLE (`read_skyrim_version() == ""`) lo saltaba entera y seguía hasta pedir
    # la aprobación, descargar y escribir DLLs. Y en esa misma rama la edición no la
    # decide el PE sino una heurística de TAMAÑO de archivo
    # (`scanner._detect_skyrim_version`: >60 MB ⇒ AE), así que lo que se instalaba era
    # el payload de un build ADIVINADO sobre un runtime desconocido.

    #: Sitio oficial al que TODO error de compatibilidad tiene que mandar al operador:
    #: sin él el mensaje dice "no se puede" y no dice adónde ir.
    _URL_OFICIAL = "https://skse.silverlock.org/"

    def _skyrim_limpio(self, tmp_path: pathlib.Path) -> pathlib.Path:
        """Raíz de juego con ejecutable y sin SKSE: es donde corre el gate."""
        install_dir = tmp_path / "skyrim"
        install_dir.mkdir()
        (install_dir / "SkyrimSE.exe").write_bytes(b"MZ")
        return install_dir

    def _espias_de_frontera(self, installer: ToolsInstaller) -> tuple[AsyncMock, AsyncMock]:
        """Espías sobre las dos fronteras que el gate tiene que anteceder.

        El HITL devuelve APPROVED y el gateway explota: así, si el gate deja pasar,
        el test falla por la frontera cruzada y no por un mock que dijo que no.
        """
        hitl = AsyncMock(return_value=Decision.APPROVED)
        egress = AsyncMock(side_effect=AssertionError("no puede haber egress sin probar compatibilidad"))
        installer._hitl.request_approval = hitl  # type: ignore[method-assign]
        installer._gateway = MagicMock()
        installer._gateway.request = egress
        return hitl, egress

    @pytest.mark.asyncio
    async def test_version_ilegible_falla_cerrado_antes_del_hitl_y_del_egress(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin versión exacta no hay prueba de compatibilidad: no se aprueba ni se baja nada.

        `read_skyrim_version` devuelve `""` en tres casos reales: sin `pefile`
        instalada, con un PE que no parsea, y con un PE válido sin recurso de
        versión. Degradar ahí "a edición sola" era fail-OPEN: la edición de esa
        misma rama sale de una heurística de tamaño.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="No pude leer la versión exacta") as exc_info:
            await installer.ensure_skse(install_dir, session)

        # La pertenencia se afirma contando la URL oficial completa y no como
        # `"skse.silverlock.org" in <mensaje>`: CodeQL lee ese `in` como el patrón de
        # saneamiento de URLs por substring (`py/incomplete-url-substring-sanitization`)
        # y lo reporta high, aunque acá el operando sea el TEXTO DE UN ERROR y no un
        # guard de seguridad. Mismo precedente y mismo motivo que
        # `test_el_host_de_skse_esta_habilitado_en_el_egress`. La aserción queda más
        # fuerte, no más débil: exige la URL entera y exactamente una vez.
        assert str(exc_info.value).count(self._URL_OFICIAL) == 1, "el mensaje tiene que ser accionable"
        hitl.assert_not_awaited(), "no se le pide aprobación al operador para algo que no se puede probar"
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_build_incompatible_falla_cerrado_antes_del_hitl_y_del_egress(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El gate de mismatch ya existía; lo que se ancla acá es su ORDEN.

        `test_bloquea_si_el_build_exacto_no_coincide` afirma que corta, no que corta
        ANTES de consumir la aprobación y el egress. Sin este ancla, mover el gate
        debajo del HITL no rompe nada.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.640")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="1.6.640"):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes

    @pytest.mark.asyncio
    async def test_edicion_explicita_no_exime_del_gate_si_hay_runtime_ilegible(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quién nombra la EDICIÓN no decide si hace falta probar la VERSIÓN.

        El gate se ató primero a `edition is None`, así que un caller que resolviera la
        edición por su cuenta —config, snapshot, un consumidor futuro— se saltaba la
        prueba de compatibilidad entera y llegaba a HITL, egress y escritura de DLLs
        con el payload elegido solo por edición: el mismo fail-open que este PR cierra,
        vivo en el camino gemelo.

        La condición correcta es sobre el RUNTIME: si hay un ejecutable de Skyrim en
        disco y no se le puede leer la versión, no se instala nada.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="No pude leer la versión exacta"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes

    @pytest.mark.asyncio
    async def test_edicion_explicita_con_runtime_incompatible_tambien_corta(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Y si la versión SÍ se lee y no matchea, el gate de mismatch tampoco se saltea.

        Las tres aserciones van juntas por la misma razón en toda esta familia: HITL y
        egress cubren las dos fronteras externas, y el snapshot del directorio cubre la
        tercera. Sin la última, una regresión que escribiera en el juego ANTES de lanzar
        el error pasaría en verde — que es precisamente el desenlace que el gate existe
        para impedir.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.640")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="1.6.640"):
            await installer.ensure_skse(install_dir, session, edition=SkyrimEdition.AE)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_ya_instalado_con_version_ilegible_sigue_siendo_idempotente(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El corte por versión ilegible aplica a INSTALAR, no a reportar lo ya instalado.

        El early-return de idempotencia no descarga ni escribe nada, así que negárselo
        a una máquina cuyo PE no se puede leer rompería una instalación que funciona
        sin ganar ninguna garantía. Misma política que el hermano de detección
        (`scanner.find_skse_installation`), que ante versión vacía degrada en vez de
        reportar faltante.

        El gate de MISMATCH conserva su precedencia sobre la idempotencia y eso se
        cubre aparte: ahí la incompatibilidad está demostrada.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_6_1170.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        res = await installer.ensure_skse(install_dir, session)

        assert res.already_existed is True
        assert res.exe_path == install_dir / "skse64_loader.exe"
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes

    @pytest.mark.asyncio
    async def test_ya_instalado_con_build_incompatible_sigue_cortando(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La contracara: con mismatch DEMOSTRADO, tener archivos en disco no absuelve.

        El DLL presente es el del build equivocado —no carga sobre ese runtime— y el
        operador tiene que enterarse acá, no al arrancar el juego. Sin este test, mover
        la idempotencia por encima de ambos gates pasaría en verde.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_6_1170.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.640")

        # Mismo hermano que `test_edicion_explicita_con_runtime_incompatible_tambien_corta`,
        # pero por autodetección: sus tres aserciones de frontera van también acá, o el
        # gemelo por parámetro queda cableado y este no. Al usar el helper, además, el
        # ancla enumerativa deja de saltearlo por helper-less y lo empieza a exigir.
        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="1.6.640"):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_runtime_exacto_soportado_sigue_instalando(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El camino feliz no se rompe: con versión legible y compatible, instala."""
        install_dir = self._skyrim_limpio(tmp_path)

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.1170")

        session = MagicMock(spec=aiohttp.ClientSession)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]
        installer._download_skse_archive = AsyncMock(return_value=None)  # type: ignore[method-assign]
        installer._extract = MagicMock(return_value=None)  # type: ignore[method-assign]
        installer._find_skse_root = MagicMock(return_value=tmp_path / "root")  # type: ignore[method-assign]
        installer._copy_skse_files = AsyncMock(return_value=None)  # type: ignore[method-assign]

        res = await installer.ensure_skse(install_dir, session)

        assert res.version == "skse64_2_02_06"
        assert res.already_existed is False

    @pytest.mark.asyncio
    async def test_runtime_de_gog_no_cae_al_payload_de_steam(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GOG NO es plataforma soportada por el autoinstalador, y se resuelve por el gate.

        No hay `SKSE_CONFIG["GOG"]` ni rama de instalación GOG: el AE de GOG
        (1.6.1179) clasifica como AE por edición pero no matchea el 1.6.1170 del
        payload de Steam, así que el gate lo corta antes del HITL y del egress y lo
        manda al sitio oficial. Este test congela ese desenlace; NO convierte a GOG
        en soportado.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.1179")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="1.6.1179") as exc_info:
            await installer.ensure_skse(install_dir, session)

        assert str(exc_info.value).count(self._URL_OFICIAL) == 1  # ver nota de CodeQL arriba
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes
        assert "GOG" not in tools_installer.SKSE_CONFIG, "el recorte de producto sigue vigente"

    # ── Steam loader: la expectativa sale del PAYLOAD, no de la edición ──────

    @pytest.mark.asyncio
    async def test_build_sin_steam_loader_instala_sin_aviso_enganoso(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Un archive que no trae el steam loader deja una instalación válida y silenciosa.

        `SKSE_CONFIG` declara `skse64_steam_loader.dll` para SE y AE, y el post-check
        avisaba por su ausencia en el DESTINO. Contra un build que no lo trae, ese
        aviso sale en toda instalación buena y le dice al operador que le falta un
        archivo que su build nunca tuvo.
        """
        game_dir = tmp_path / "skyrim"
        game_dir.mkdir()
        skse_root = self._payload(tmp_path)  # loader + DLL de runtime, sin steam loader

        with caplog.at_level("WARNING"):
            await installer._copy_skse_files(skse_root, game_dir, tools_installer.SKSE_CONFIG["AE"])

        assert (game_dir / "skse64_loader.exe").is_file()
        assert (game_dir / "skse64_1_6_1170.dll").is_file()
        assert "steam_loader" not in caplog.text

    @pytest.mark.asyncio
    async def test_payload_con_steam_loader_que_no_aterriza_sigue_avisando(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Compatibilidad con los builds que SÍ lo traen: ahí la ausencia es real.

        Es la contracara del test de arriba y lo que impide que el fix se convierta en
        "nunca avisar": si el archive traía el componente y no llegó al destino, la
        copia quedó incompleta y el aviso es información, no ruido.
        """
        game_dir = tmp_path / "skyrim"
        game_dir.mkdir()
        skse_root = self._payload(tmp_path)
        (skse_root / "skse64_steam_loader.dll").write_bytes(b"MZ-steam")

        # La copia real lo llevaría; se lo saca del destino para simular la copia
        # incompleta sin tocar el bucle de copia.
        async def _copiar_sin_steam_loader(origen: pathlib.Path, destino: pathlib.Path) -> None:
            if origen.name != "skse64_steam_loader.dll":
                destino.write_bytes(origen.read_bytes())

        with (
            patch.object(tools_installer, "_esperar_hilo_ininterrumpible", new=AsyncMock()),
            caplog.at_level("WARNING"),
        ):
            # `_esperar_hilo_ininterrumpible` mockeado no copia nada: se replica la
            # copia a mano para que el post-check mire un destino realista.
            for item in skse_root.iterdir():
                await _copiar_sin_steam_loader(item, game_dir / item.name)
            await installer._copy_skse_files(skse_root, game_dir, tools_installer.SKSE_CONFIG["AE"])

        assert "skse64_steam_loader.dll" in caplog.text

    # ── Presencia vs. compatibilidad verificada (deuda heredada de #490) ─────
    #
    # El gate de #490 exige PODER PROBAR la compatibilidad para MUTAR, y eso sigue
    # intacto. Lo que estaba mal era el otro lado: `ya_instalado` se calculaba con el
    # DLL de la edición ADIVINADA, así que una instalación SE buena en una máquina con
    # PE ilegible (donde la edición sale de una heurística de TAMAÑO) se reportaba como
    # ausente y el operador recibía "instalá a mano" sobre un SKSE que ya estaba.
    #
    # La corrección NO es "loader + cualquier DLL ⇒ éxito": eso convierte el falso
    # negativo en el falso positivo opuesto (runtime desconocido + DLL stale ⇒ SUCCESS).
    # Se distingue PRESENCIA de COMPATIBILIDAD PROBADA y se dice cuál de las dos se tiene.

    @pytest.mark.asyncio
    async def test_runtime_legible_y_dll_del_build_exacto_se_reporta_verificado(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caso A: la versión se leyó, matchea, y el DLL de ese build está en disco.

        Es el ÚNICO desenlace que puede llamarse verificado: hay una prueba, no una
        conjetura. El resto de la familia existe para que este estado no se reparta
        gratis a los casos en que la prueba no se pudo hacer.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_6_1170.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.1170")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        res = await installer.ensure_skse(install_dir, session)

        assert res.verification is InstallVerification.VERIFIED
        assert res.already_existed is True
        assert res.exe_path == install_dir / "skse64_loader.exe"
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes

    @pytest.mark.asyncio
    async def test_skse_presente_con_runtime_ilegible_no_se_reporta_ausente(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caso B — el falso negativo heredado de #490, en su forma mínima.

        En disco hay una instalación SE correcta (`skse64_1_5_97.dll`). El PE no se
        puede leer, así que la edición sale de la heurística de tamaño y dice AE. El
        contrato viejo buscaba el DLL **AE**, no lo encontraba, y concluía "no hay
        SKSE": `ToolInstallError` mandando a instalar a mano algo que ya estaba.

        Lo que corresponde no es afirmar compatibilidad —el runtime sigue siendo
        desconocido— sino reportar lo único que se sabe: está presente, no se pudo
        verificar. Y con las mismas cero consecuencias que el early-return normal.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_5_97.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        # La heurística clasifica AE aunque lo instalado sea SE: es exactamente la
        # rama donde la edición NO sale del PE (`scanner._detect_skyrim_version`).
        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        res = await installer.ensure_skse(install_dir, session)

        assert res.verification is InstallVerification.PRESENT_BUT_UNVERIFIED
        assert res.exe_path == install_dir / "skse64_loader.exe"
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones: ni copia ni limpieza de DLL"

    @pytest.mark.asyncio
    async def test_runtime_ilegible_nunca_se_reporta_verificado_aunque_el_dll_sea_el_esperado(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caso C del contrato: el DLL con el nombre correcto tampoco PRUEBA nada.

        Acá el DLL en disco es justo el que este payload instalaría, así que la
        tentación es marcarlo verificado. Pero lo que verifica no es el nombre del
        archivo: es haber leído la versión del ejecutable y haberla comparado. Sin
        esa lectura, `skse64_1_6_1170.dll` sobre un runtime desconocido puede ser un
        leftover de un upgrade anterior — presencia, no compatibilidad.

        Este es el ancla que impide "arreglar" el falso negativo devolviendo VERIFIED
        siempre que haya archivos.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_6_1170.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        res = await installer.ensure_skse(install_dir, session)

        assert res.verification is InstallVerification.PRESENT_BUT_UNVERIFIED
        assert res.verification is not InstallVerification.VERIFIED
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes

    @pytest.mark.asyncio
    async def test_mismatch_demostrado_no_se_degrada_a_presente_sin_verificar(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La contracara del caso B: con la versión LEÍDA, "no pude probar" no aplica.

        Variante nueva que el estado PRESENT_BUT_UNVERIFIED hace alcanzable: el DLL
        en disco (SE) no es el que este payload instalaría, así que el early-return
        exacto no dispara y la detección de PRESENCIA sí encuentra algo. Si esa
        detección se consultara sin mirar antes si la versión se pudo leer, un
        mismatch DEMOSTRADO (1.6.640 contra un payload 1.6.1170) saldría reportado
        como "presente, no verificable" en vez de cortar.

        El gate de mismatch de #490 conserva su precedencia: la incertidumbre es un
        estado para lo que no se pudo probar, nunca un lugar donde esconder lo que sí
        se probó y salió mal.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_5_97.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "1.6.640")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match="1.6.640"):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_loader_sin_dll_de_runtime_sigue_fallando_cerrado(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un upgrade a medias (loader sí, DLL no) NO es una instalación presente.

        Es el borde que separa "hay SKSE que no puedo verificar" de "no hay SKSE":
        sin DLL de runtime el juego no carga nada, así que degradar esto a
        PRESENT_BUT_UNVERIFIED reportaría como instalación algo que no arranca. El
        fail-closed de #490 tiene que seguir cortando acá.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match=self._URL_OFICIAL):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_dll_de_runtime_sin_loader_sigue_fallando_cerrado(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El gemelo del anterior: sin loader tampoco hay nada que el juego cargue."""
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_1_5_97.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match=self._URL_OFICIAL):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_solo_steam_loader_dll_no_cuenta_como_runtime_presente(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`skse64_steam_loader.dll` matchea `skse*.dll` pero no codifica versión de juego.

        Es el falso positivo más barato de todos: un glob ingenuo lo cuenta como DLL
        de runtime y cualquier directorio con el steam loader pasa a "SKSE presente".
        La detección compartida ya lo excluye (`_SKSE_NON_RUNTIME_DLLS`) y este test
        congela que `ensure_skse` herede esa exclusión en vez de re-implementar el glob.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_steam_loader.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        with pytest.raises(ToolInstallError, match=self._URL_OFICIAL):
            await installer.ensure_skse(install_dir, session)

        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "cero mutaciones del directorio del juego"

    @pytest.mark.asyncio
    async def test_varios_dll_historicos_con_runtime_ilegible_no_eligen_ganador(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SE y AE conviviendo en disco: leftover de upgrade, no prueba de nada.

        Con dos DLL de builds distintos, como mucho uno puede corresponder al runtime
        —y sin poder leerlo no se sabe cuál—. El desenlace correcto sigue siendo el
        mismo estado de incertidumbre, sin elegir ganador y sin borrar el perdedor:
        el cleanup de DLL huérfanos es parte del camino de instalación, y este camino
        no instala nada.
        """
        install_dir = self._skyrim_limpio(tmp_path)
        (install_dir / "skse64_loader.exe").write_bytes(b"MZ")
        (install_dir / "skse64_1_5_97.dll").write_bytes(b"MZ")
        (install_dir / "skse64_1_6_1170.dll").write_bytes(b"MZ")
        antes = set(install_dir.iterdir())

        monkeypatch.setattr(tools_installer, "detect_skyrim_edition", lambda _exe: SkyrimEdition.AE)
        monkeypatch.setattr(tools_installer, "read_skyrim_version", lambda _exe: "")

        hitl, egress = self._espias_de_frontera(installer)
        session = MagicMock(spec=aiohttp.ClientSession)

        res = await installer.ensure_skse(install_dir, session)

        assert res.verification is InstallVerification.PRESENT_BUT_UNVERIFIED
        hitl.assert_not_awaited()
        egress.assert_not_awaited()
        assert set(install_dir.iterdir()) == antes, "ni se instala ni se limpia el DLL 'sobrante'"

    def test_la_verificacion_por_defecto_no_le_cambia_el_contrato_a_los_otros_tools(self) -> None:
        """El campo nuevo es opcional y su default no toca a LOOT/xEdit/Pandora/BodySlide.

        Ninguno de esos está pinneado al build de un runtime: no tienen nada que
        probar, así que su resultado no arrastra incertidumbre. El default los deja
        exactamente donde estaban y evita que este contrato se vuelva un campo que
        todos los `ensure_*` tienen que aprender a llenar.
        """
        res = tools_installer.InstallResult(
            tool_name="LOOT",
            exe_path=pathlib.Path("C:/Modding/LOOT/loot.exe"),
            version="0.22.4",
            already_existed=True,
        )

        assert res.verification is InstallVerification.VERIFIED


async def _async_iter(items: list[bytes]):
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Blueprint Fase 2 — helpers compartidos del autoinstalador de Community
# Shaders (camino compartido con loot/xedit/pandora/NGIO): digest de la API de
# GitHub, techo de bytes durante el streaming, verificación compuesta
# (required_rel) y comment del meta.ini.
# ---------------------------------------------------------------------------


class TestHelpersCompartidosBlueprint:
    def test_parse_github_digest_normaliza_y_rechaza(self) -> None:
        """'sha256:<hex>' y el hex pelado se normalizan; malformado/ausente → '' (TOFU)."""
        hex64 = "5c9bbb49f71402fa8fd5936df6eefa40a8f0bcb4c623ebceeb478a1e14447c11"
        assert tools_installer._parse_github_digest(f"sha256:{hex64}") == hex64
        assert tools_installer._parse_github_digest(hex64) == hex64
        assert tools_installer._parse_github_digest("sha256:zz" + "0" * 62) == ""
        assert tools_installer._parse_github_digest("sha256:abc") == ""
        assert tools_installer._parse_github_digest("") == ""
        assert tools_installer._parse_github_digest(None) == ""
        assert tools_installer._parse_github_digest(123) == ""

    async def test_download_asset_digest_mismatch_aborta_y_borra(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """El digest de la API (vía expected_sha256) es vinculante: mismatch → abort + borrado."""
        payload = b"contenido-de-prueba"
        asset = ReleaseAsset(
            name="x.zip",
            size=len(payload),
            download_url="https://api.github.com/x",
            browser_download_url="https://github.com/x",
        )

        async def _iter_chunks(size: int):
            yield payload

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.release = MagicMock()
        resp.content = MagicMock()
        resp.content.iter_chunked = _iter_chunks
        installer._gateway.request = AsyncMock(return_value=resp)  # type: ignore[method-assign]

        dest_dir = tmp_path / "dest"
        with pytest.raises(ToolInstallError, match="SHA-256"):
            await installer._download_asset(
                MagicMock(spec=aiohttp.ClientSession),
                asset,
                dest_dir,
                expected_sha256="0" * 64,
            )
        assert not (dest_dir / asset.name).exists()

    async def test_download_asset_digest_correcto_instala(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Digest correcto: la descarga procede y el archivo queda en disco."""
        payload = b"contenido-de-prueba"
        asset = ReleaseAsset(
            name="x.zip",
            size=len(payload),
            download_url="https://api.github.com/x",
            browser_download_url="https://github.com/x",
        )

        async def _iter_chunks(size: int):
            yield payload

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.release = MagicMock()
        resp.content = MagicMock()
        resp.content.iter_chunked = _iter_chunks
        installer._gateway.request = AsyncMock(return_value=resp)  # type: ignore[method-assign]

        dest_dir = tmp_path / "dest"
        dest = await installer._download_asset(
            MagicMock(spec=aiohttp.ClientSession),
            asset,
            dest_dir,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        assert dest.read_bytes() == payload

    async def test_techo_de_bytes_aborta_durante_streaming_y_borra(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Un cuerpo mayor al size declarado aborta a mitad de la descarga y borra el parcial."""
        asset = ReleaseAsset(
            name="gordo.zip",
            size=10,
            download_url="https://api.github.com/x",
            browser_download_url="https://github.com/x",
        )

        async def _iter_chunks(size: int):
            yield b"x" * 100

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.release = MagicMock()
        resp.content = MagicMock()
        resp.content.iter_chunked = _iter_chunks
        installer._gateway.request = AsyncMock(return_value=resp)  # type: ignore[method-assign]

        dest_dir = tmp_path / "dest"
        with pytest.raises(ToolInstallError, match="excede"):
            await installer._download_asset(MagicMock(spec=aiohttp.ClientSession), asset, dest_dir)
        assert not (dest_dir / asset.name).exists()

    async def test_techo_absoluto_de_respaldo_cuando_size_es_cero(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin size publicado por la API (size=0) rige el techo absoluto de respaldo:
        sin él, el streaming escribe sin límite hasta el timeout total de 600 s
        (el camino SKSE ya tenía techo absoluto; este helper compartido, no)."""
        monkeypatch.setattr(tools_installer, "_GITHUB_ASSET_MAX_BYTES", 100)
        asset = ReleaseAsset(
            name="sin-size.zip",
            size=0,
            download_url="https://api.github.com/x",
            browser_download_url="https://github.com/x",
        )

        async def _iter_chunks(size: int):
            yield b"x" * 200

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.release = MagicMock()
        resp.content = MagicMock()
        resp.content.iter_chunked = _iter_chunks
        installer._gateway.request = AsyncMock(return_value=resp)  # type: ignore[method-assign]

        dest_dir = tmp_path / "dest"
        with pytest.raises(ToolInstallError, match="techo absoluto"):
            await installer._download_asset(MagicMock(spec=aiohttp.ClientSession), asset, dest_dir)
        assert not (dest_dir / asset.name).exists()

    async def test_staging_se_valida_contra_el_sandbox_antes_de_crearse(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """El staging (``.skyclaw-dl``) se valida contra el PathValidator ANTES del
        mkdir: con ``mods_dir`` == raíz del sandbox, el staging cae en el padre
        (fuera de los roots) y la descarga no arranca. El assert de "dentro de los
        roots" del comentario dejaba la garantía en la palabra, no en el código.

        Se ancla la SECUENCIA de validación porque el camino viejo también lanzaba
        ``PathViolationError`` — pero tarde: dentro de ``_download_asset``, sobre el
        destino y DESPUÉS de crear el directorio no autorizado (el rmdir del finally
        borraba la evidencia)."""
        hitl = AsyncMock(return_value=Decision.APPROVED)
        installer._hitl.request_approval = hitl  # type: ignore[method-assign]
        installer._gateway.request = AsyncMock()  # type: ignore[method-assign]
        installer._find_github_asset = AsyncMock(  # type: ignore[method-assign]
            return_value=(
                ReleaseAsset(
                    name="mod.zip",
                    size=10,
                    download_url="https://api.github.com/x",
                    browser_download_url="https://github.com/x",
                ),
                "1.0.0",
            )
        )
        validados: list[pathlib.Path] = []
        validar_real = installer._validator.validate

        def _registrar(path: str | pathlib.Path, *, strict_symlink: bool = True) -> pathlib.Path:
            validados.append(pathlib.Path(path))
            return validar_real(path, strict_symlink=strict_symlink)

        installer._validator.validate = _registrar  # type: ignore[method-assign]

        with pytest.raises(PathViolationError):
            await installer._ensure_github_mod(
                tmp_path,
                MagicMock(spec=aiohttp.ClientSession),
                mod_name="ModFicticio",
                releases_url="https://api.github.com/repos/x/x/releases",
                sentinel_glob="*.dll",
                request_slug="mod-ficticio",
                source_hint="GitHub x/x releases",
            )

        # El staging COMO TAL es lo único validado: si la validación viniera de
        # adentro de la descarga, la lista tendría el destino (<staging>/mod.zip).
        # Desde F4 el staging vive en .skyclaw-dl/<mod_dir.name> (la identidad
        # del lock), no en la raíz compartida de antes ni bajo el slug.
        assert validados == [tmp_path.parent / ".skyclaw-dl" / "ModFicticio"]
        assert not (tmp_path.parent / ".skyclaw-dl" / "ModFicticio").exists()
        assert installer._gateway.request.await_count == 0

    async def test_post_extract_corre_en_un_hilo_no_en_el_event_loop(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """F5: ``post_extract`` hace I/O pesado — el transform del FOMOD de Engine
        Fixes corre ``shutil.copytree``/``copy2`` y ``rmtree_link_aware``. Llamado
        inline en ``_ensure_nexus_mod`` congelaba el event loop (en NiceGUI se ve
        como un hang de la UI). Debe despacharse por ``_esperar_hilo_ininterrumpible``,
        igual que la extracción: no cambia el orden ni el contrato funcional.
        """
        hilo_del_loop = threading.get_ident()
        hilo_post_extract: list[int] = []

        def _post_extract(mod_dir: pathlib.Path) -> None:
            hilo_post_extract.append(threading.get_ident())
            # Arma el payload que el zip no trae: el foco del test es el hilo,
            # la verificación del sentinel tiene que pasar igual.
            plugins = mod_dir / "SKSE" / "Plugins"
            plugins.mkdir(parents=True, exist_ok=True)
            (plugins / "Tool.dll").write_text("x", encoding="utf-8")

        mods_dir = tmp_path / "mods"
        mods_dir.mkdir()
        # Zip mínimo extraíble; el payload real lo aporta post_extract.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("placeholder.txt", "x")
        archive = tmp_path / "staging" / "tool.zip"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(buf.getvalue())

        file_info = MagicMock(file_id=1, file_name="tool.zip", size_bytes=100)
        downloader = MagicMock()
        downloader.get_file_info = AsyncMock(return_value=file_info)
        downloader.download = AsyncMock(return_value=archive)
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]

        resultado = await installer._ensure_nexus_mod(
            mods_dir,
            MagicMock(spec=aiohttp.ClientSession),
            downloader,
            mod_name="ModFicticio",
            nexus_id=99999,
            sentinel_glob="Tool.dll",
            request_slug="post-extract-thread",
            post_extract=_post_extract,
        )

        assert resultado.already_existed is False
        assert (mods_dir / "ModFicticio" / "SKSE" / "Plugins" / "Tool.dll").is_file()
        # El transform corrió en un worker del pool, NUNCA en el hilo del loop.
        assert hilo_post_extract and hilo_post_extract[0] != hilo_del_loop

    async def test_dos_instalaciones_github_concurrentes_no_se_pisan_el_staging(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """Carrera F4: dos ``_ensure_github_mod`` concurrentes (mods DISTINTOS → locks
        per-mod_dir distintos) compartían UN único staging ``.skyclaw-dl``. El mod que
        terminaba hacía ``rmdir()`` mientras el otro seguía descargando (entre el mkdir
        y el ``open`` de su archivo, en el ``await`` al gateway) y le borraba el directorio
        en uso, haciéndolo fallar con ``FileNotFoundError``. El staging por mod
        (``.skyclaw-dl/<mod_dir.name>``) hace que cada instalación limpie SOLO lo propio.

        El interleaving es determinista: A bloquea su descarga (señalando que ya hizo el
        mkdir de staging pero aún no escribió el archivo), B corre COMPLETA — su ``finally``
        ``rmdir()`` es el que, con staging compartido, elimina el directorio de A — y solo
        entonces se libera A. Antes del fix, A muere al abrir su destino.

        El staging se keyea por ``mod_dir.name`` —la MISMA identidad que el lock de
        instalación— y no por ``request_slug``: dos mods que reutilizaran un slug quedan
        igualmente aislados (ver el ``request_slug`` repetido abajo). Review de Codex.
        """
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir()

        # Payload mínimo válido: sentinel ``*.dll`` bajo SKSE/Plugins para ambos mods.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKSE/Plugins/tool.dll", "x")
        payload = buf.getvalue()

        en_descarga_a = asyncio.Event()
        liberar_a = asyncio.Event()

        asset_a = ReleaseAsset(
            name="a.zip",
            size=len(payload),
            download_url="https://api.github.com/repos/x/a/releases/assets/1",
            browser_download_url="https://github.com/x/a/a.zip",
        )
        asset_b = ReleaseAsset(
            name="b.zip",
            size=len(payload),
            download_url="https://api.github.com/repos/x/b/releases/assets/2",
            browser_download_url="https://github.com/x/b/b.zip",
        )

        async def _find_asset(session: Any, releases_url: str, *, keyword: Any = None, select: Any = None) -> Any:
            return (asset_a if "/a/" in releases_url else asset_b, "1.0.0")

        installer._find_github_asset = AsyncMock(side_effect=_find_asset)  # type: ignore[method-assign]
        installer._hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)  # type: ignore[method-assign]

        def _stream_response() -> MagicMock:
            async def _chunks(_n: int) -> AsyncIterator[bytes]:
                yield payload

            resp = MagicMock()
            resp.status = 200
            resp.raise_for_status = MagicMock()
            resp.release = MagicMock()
            resp.content = MagicMock()
            resp.content.iter_chunked = _chunks
            return resp

        async def _gateway_request(method: str, url: str, session: Any, **kwargs: Any) -> MagicMock:
            if "/a/" in url:
                en_descarga_a.set()  # A ya hizo mkdir de staging, aún no escribió a.zip
                await liberar_a.wait()
            return _stream_response()

        installer._gateway.request = AsyncMock(side_effect=_gateway_request)  # type: ignore[method-assign]

        comun = dict(sentinel_glob="*.dll", source_hint="GitHub x/x releases")
        # Los DOS usan el MISMO request_slug a propósito: ancla la regresión que
        # señaló la review de Codex — si el staging se keyeara por slug, dos mods
        # distintos con un slug repetido tendrían locks distintos sobre el MISMO
        # staging y la carrera reaparecería. El staging keyeado por mod_dir.name
        # lo hace estructuralmente imposible aunque el slug se repita.
        task_a = asyncio.create_task(
            installer._ensure_github_mod(
                mods_dir,
                MagicMock(spec=aiohttp.ClientSession),
                mod_name="ModA",
                releases_url="https://api.github.com/repos/x/a/releases",
                request_slug="slug-compartido",
                **comun,
            )
        )
        await en_descarga_a.wait()  # A está entre el mkdir y el open, esperando el gateway

        # B corre completa; su ``finally`` rmdir() es el que pisaría el staging de A.
        resultado_b = await installer._ensure_github_mod(
            mods_dir,
            MagicMock(spec=aiohttp.ClientSession),
            mod_name="ModB",
            releases_url="https://api.github.com/repos/x/b/releases",
            request_slug="slug-compartido",
            **comun,
        )

        liberar_a.set()
        resultado_a = await task_a

        assert resultado_b.already_existed is False
        assert resultado_a.already_existed is False
        assert (mods_dir / "ModA" / "SKSE" / "Plugins" / "tool.dll").is_file()
        assert (mods_dir / "ModB" / "SKSE" / "Plugins" / "tool.dll").is_file()

    def test_verify_mod_payload_requiere_dirs_relativos(
        self, installer: ToolsInstaller, tmp_path: pathlib.Path
    ) -> None:
        """required_rel es vinculante: sin el dir, abort con su nombre; con él, pasa."""
        mod_dir = tmp_path / "mod"
        (mod_dir / "SKSE" / "Plugins").mkdir(parents=True)
        (mod_dir / "SKSE" / "Plugins" / "CommunityShaders.dll").write_text("x", encoding="utf-8")

        with pytest.raises(ToolInstallError, match="Shaders"):
            installer._verify_mod_payload(mod_dir, "Mod", "CommunityShaders.dll", required_rel=("Shaders",))

        (mod_dir / "Shaders").mkdir()
        installer._verify_mod_payload(mod_dir, "Mod", "CommunityShaders.dll", required_rel=("Shaders",))

    def test_write_mod_meta_ini_comment_deja_de_mentir(self, tmp_path: pathlib.Path) -> None:
        """Default genérico sin 'precache'; comment explícito lo reemplaza."""
        mod_dir = tmp_path / "mod"
        mod_dir.mkdir()

        tools_installer._write_mod_meta_ini(mod_dir, "Mod", "1.0")
        default = (mod_dir / "meta.ini").read_text(encoding="utf-8")
        assert "Instalado por Sky-Claw." in default
        assert "precache" not in default.lower()

        tools_installer._write_mod_meta_ini(
            mod_dir, "Mod", "1.0", comment="Instalado por Sky-Claw (Community Shaders)."
        )
        custom = (mod_dir / "meta.ini").read_text(encoding="utf-8")
        assert "Community Shaders" in custom

"""Guardia de regresión para el canal de releases de Community Shaders.

El endpoint ``GET /repos/{owner}/{repo}/releases/latest`` de GitHub devuelve la
última release publicada que no sea draft ni prerelease. Sky-Claw usa ese
endpoint deliberadamente para Community Shaders, de modo que una RC/beta
publicada por upstream no se convierta automáticamente en candidata de
instalación.

El test es hermético: ejercita el flujo de ``ensure_community_shaders`` con
colaboradores simulados y congela el endpoint que ese flujo entrega al
instalador de mods de GitHub, sin acceder a la red ni duplicar localmente el
algoritmo de selección de releases de GitHub.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

import sky_claw.local.tools_installer as tools_installer
from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.tools_installer import ToolsInstaller


@pytest.mark.asyncio
async def test_flujo_community_shaders_usa_canal_estable_de_github(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El flujo de Community Shaders debe entregar ``releases/latest`` al instalador."""
    installer = ToolsInstaller(
        hitl=MagicMock(),
        gateway=MagicMock(),
        path_validator=MagicMock(),
        lock_manager=MagicMock(),
    )
    asegurar_nexus = AsyncMock(return_value=MagicMock())
    asegurar_github = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(installer, "_ensure_nexus_mod", asegurar_nexus)
    monkeypatch.setattr(installer, "_ensure_github_mod", asegurar_github)
    monkeypatch.setattr(tools_installer, "_detectar_skse", lambda _game_dir: True)
    monkeypatch.setattr(tools_installer, "_detectar_enb", lambda _game_dir: False)
    monkeypatch.setattr(tools_installer, "_detectar_preloader", lambda _game_dir: True)

    await installer.ensure_community_shaders(
        tmp_path / "mods",
        MagicMock(),
        downloader=MagicMock(),
        edition=SkyrimEdition.AE,
        game_version="1.6.1170",
        game_dir=tmp_path / "game",
    )

    asegurar_github.assert_awaited_once()
    assert asegurar_github.await_args.kwargs["releases_url"] == (
        "https://api.github.com/repos/community-shaders/"
        "skyrim-community-shaders/releases/latest"
    )

"""Contrato canónico de las tools FOMOD del agente (preview / install / resolve).

Ancla la regla de AGENTS.md: todo resultado de tool emite ``success: bool`` +
``message: str`` (vacío en éxito) además de sus campos estructurados. Las tres
tools FOMOD usaban claves legacy (``error``) y no estaban cableadas a un
``FomodInstaller`` en producción — este archivo fija ambos contratos.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import zipfile
from typing import Any

import pytest

from sky_claw.app.agent.tools import AsyncToolRegistry
from sky_claw.app.agent.tools.system_tools import (
    install_mod_from_archive,
    preview_mod_installer,
    resolve_fomod,
)
from sky_claw.app.security.hitl import Decision
from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.fomod.installer import FomodInstaller, FomodPreview, InstallResult

# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class _FakeFomodInstaller:
    """Doble del FomodInstaller: preview/install controlados por el test."""

    def __init__(
        self,
        *,
        preview: FomodPreview | None = None,
        install_result: InstallResult | None = None,
        fomod_xml: str | None = "<config><moduleName>TestMod</moduleName></config>",
        raise_on_preview: BaseException | None = None,
        raise_on_install: BaseException | None = None,
    ) -> None:
        self._preview = preview
        self._install_result = install_result
        self._fomod_xml = fomod_xml
        self._raise_on_preview = raise_on_preview
        self._raise_on_install = raise_on_install

    async def preview(self, archive_path: pathlib.Path) -> FomodPreview:
        if self._raise_on_preview is not None:
            raise self._raise_on_preview
        if self._preview is not None:
            return self._preview
        return FomodPreview(mod_name=archive_path.stem, has_fomod=False)

    async def install(
        self,
        archive_path: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
        selections: dict[str, list[str]] | None = None,
    ) -> InstallResult:
        if self._raise_on_install is not None:
            raise self._raise_on_install
        if self._install_result is not None:
            return self._install_result
        return InstallResult(mod_name=archive_path.stem, installed=True)

    def read_fomod_xml(self, archive_path: pathlib.Path) -> str | None:
        return self._fomod_xml


class _FakeHITL:
    def __init__(self, decision: Decision = Decision.APPROVED) -> None:
        self._decision = decision
        self.requested: list[str] = []
        self.requested_kwargs: list[dict[str, Any]] = []

    async def request_approval(self, request_id: str, reason: str, **kwargs: Any) -> Decision:
        self.requested.append(request_id)
        self.requested_kwargs.append(kwargs)
        return self._decision


class _FakeMO2:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.added: list[str] = []
        self.added_profiles: list[str] = []
        self._fail_modlist = False

    def fail_modlist(self) -> None:
        self._fail_modlist = True

    async def add_mod_to_modlist(self, mod_name: str, profile: str = "Default") -> None:
        if self._fail_modlist:
            raise OSError("modlist.txt read-only")
        self.added.append(mod_name)
        self.added_profiles.append(profile)


@pytest.fixture()
def fake_installer() -> _FakeFomodInstaller:
    return _FakeFomodInstaller()


@pytest.fixture()
def fake_mo2(tmp_path: pathlib.Path) -> _FakeMO2:
    return _FakeMO2(tmp_path)


def _cargar(payload: str) -> dict[str, Any]:
    return json.loads(payload)


# ---------------------------------------------------------------------------
# preview_mod_installer
# ---------------------------------------------------------------------------


class TestPreviewModInstallerContrato:
    async def test_sin_instalador_emite_contrato_canonico(self) -> None:
        payload = _cargar(await preview_mod_installer(None, "C:/mods/TestMod.zip"))

        assert payload["success"] is False
        assert isinstance(payload["message"], str)
        assert payload["message"] != ""
        # Regla 7: sin claves legacy como sustituto de success/message.
        assert "error" not in payload

    async def test_con_fomod_devuelve_opciones_y_mensaje_vacio(self) -> None:
        preview = FomodPreview(
            mod_name="TestMod",
            has_fomod=True,
            steps=[
                {
                    "name": "Options",
                    "groups": [{"name": "Textures", "type": "SelectExactlyOne", "options": ["1K", "2K"]}],
                }
            ],
        )
        installer = _FakeFomodInstaller(preview=preview)
        payload = _cargar(await preview_mod_installer(installer, "C:/mods/TestMod.zip"))

        assert payload["success"] is True
        assert payload["message"] == ""
        assert payload["has_fomod"] is True
        assert payload["mod_name"] == "TestMod"
        assert payload["steps"][0]["name"] == "Options"

    async def test_sin_fomod_devuelve_success_con_has_fomod_false(self, fake_installer: _FakeFomodInstaller) -> None:
        payload = _cargar(await preview_mod_installer(fake_installer, "C:/mods/Flat.zip"))

        assert payload["success"] is True
        assert payload["has_fomod"] is False

    async def test_error_de_frontera_devuelve_success_false(self) -> None:
        installer = _FakeFomodInstaller(raise_on_preview=RuntimeError("boom"))
        payload = _cargar(await preview_mod_installer(installer, "C:/mods/TestMod.zip"))

        assert payload["success"] is False
        assert payload["message"] != ""
        assert "error" not in payload

    async def test_propaga_cancelacion_async(self) -> None:
        installer = _FakeFomodInstaller(raise_on_preview=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await preview_mod_installer(installer, "C:/mods/TestMod.zip")


# ---------------------------------------------------------------------------
# install_mod_from_archive
# ---------------------------------------------------------------------------


class TestInstallModFromArchiveContrato:
    async def test_sin_hitl_bloquea_con_contrato_canonico(
        self, fake_installer: _FakeFomodInstaller, fake_mo2: _FakeMO2
    ) -> None:
        payload = _cargar(
            await install_mod_from_archive(fake_mo2, fake_installer, None, "C:/mods/TestMod.zip", profile="Default")
        )

        assert payload["success"] is False
        assert "HITL" in payload["message"]
        assert "error" not in payload

    async def test_sin_instalador_fomod_bloquea(self, fake_mo2: _FakeMO2) -> None:
        hitl = _FakeHITL()
        payload = _cargar(
            await install_mod_from_archive(fake_mo2, None, hitl, "C:/mods/TestMod.zip", profile="Default")
        )

        assert payload["success"] is False
        assert "FOMOD installer" in payload["message"]

    async def test_rechazo_del_operador_es_denied(
        self, fake_installer: _FakeFomodInstaller, fake_mo2: _FakeMO2
    ) -> None:
        hitl = _FakeHITL(decision=Decision.DENIED)
        payload = _cargar(
            await install_mod_from_archive(fake_mo2, fake_installer, hitl, "C:/mods/TestMod.zip", profile="Default")
        )

        assert payload["success"] is False
        assert payload["status"] == "denied"
        assert fake_mo2.added == []
        assert hitl.requested_kwargs[0]["category"] == "tool_execution"

    async def test_instalacion_ok_agrega_al_modlist(self, fake_mo2: _FakeMO2) -> None:
        hitl = _FakeHITL()
        installer = _FakeFomodInstaller(
            install_result=InstallResult(mod_name="TestMod", files_copied=["plugin.esp"], installed=True)
        )
        payload = _cargar(
            await install_mod_from_archive(fake_mo2, installer, hitl, "C:/mods/TestMod.zip", profile="Default")
        )

        assert payload["success"] is True
        assert payload["message"] == ""
        assert payload["files_copied"] == ["plugin.esp"]
        assert fake_mo2.added == ["TestMod"]

    async def test_fallo_de_instalacion_conserva_errores_estructurados(self, fake_mo2: _FakeMO2) -> None:
        hitl = _FakeHITL()
        installer = _FakeFomodInstaller(
            install_result=InstallResult(mod_name="TestMod", errors=["no se pudo extraer"], installed=False)
        )
        payload = _cargar(
            await install_mod_from_archive(fake_mo2, installer, hitl, "C:/mods/TestMod.zip", profile="Default")
        )

        assert payload["success"] is False
        assert payload["errors"] == ["no se pudo extraer"]
        assert fake_mo2.added == []

    async def test_instalacion_incompleta_sin_errores_tiene_mensaje(self, fake_mo2: _FakeMO2) -> None:
        """FOMOD con decisiones pendientes y sin archivos requeridos: el installer
        devuelve installed=False sin errores; el mensaje no puede quedar vacío."""
        hitl = _FakeHITL()
        installer = _FakeFomodInstaller(
            install_result=InstallResult(
                mod_name="TestMod",
                pending_decisions=["Options/Textures: must select exactly one"],
                installed=False,
            )
        )
        payload = _cargar(
            await install_mod_from_archive(fake_mo2, installer, hitl, "C:/mods/TestMod.zip", profile="Default")
        )

        assert payload["success"] is False
        assert payload["message"] != ""
        assert payload["pending_decisions"] == ["Options/Textures: must select exactly one"]

    async def test_instalacion_registra_en_perfil_configurado(self, fake_mo2: _FakeMO2) -> None:
        installer = _FakeFomodInstaller(
            install_result=InstallResult(mod_name="TestMod", files_copied=["plugin.esp"], installed=True)
        )

        payload = _cargar(
            await install_mod_from_archive(
                fake_mo2,
                installer,
                _FakeHITL(),
                "C:/mods/TestMod.zip",
                profile="Requiem",
            )
        )

        assert payload["success"] is True
        assert fake_mo2.added_profiles == ["Requiem"]

    async def test_fallo_de_modlist_es_fallo_parcial(self, fake_mo2: _FakeMO2) -> None:
        hitl = _FakeHITL()
        fake_mo2.fail_modlist()
        installer = _FakeFomodInstaller(
            install_result=InstallResult(mod_name="TestMod", files_copied=["plugin.esp"], installed=True)
        )
        payload = _cargar(
            await install_mod_from_archive(fake_mo2, installer, hitl, "C:/mods/TestMod.zip", profile="Default")
        )

        # Los archivos quedaron en mods/ pero el mod no se activa en MO2: fallo
        # parcial honesto que conserva los campos estructurados.
        assert payload["success"] is False
        assert "modlist" in payload["message"]
        assert payload["files_copied"] == ["plugin.esp"]

    async def test_instalacion_ok_con_lock_manager_cableado(
        self,
        fake_mo2: _FakeMO2,
        tmp_path: pathlib.Path,
    ) -> None:
        """Con lock_manager cableado, la instalación participa del lock
        cross-process de la familia T-31 y el flujo completo funciona."""
        from sky_claw.app.db.locks import DistributedLockManager

        lock_manager = DistributedLockManager(db_path=tmp_path / "locks.db")
        await lock_manager.initialize()
        try:
            hitl = _FakeHITL()
            installer = _FakeFomodInstaller(
                install_result=InstallResult(mod_name="TestMod", files_copied=["plugin.esp"], installed=True)
            )
            payload = _cargar(
                await install_mod_from_archive(
                    fake_mo2,
                    installer,
                    hitl,
                    "C:/mods/TestMod.zip",
                    profile="Default",
                    lock_manager=lock_manager,
                )
            )

            assert payload["success"] is True
            assert payload["files_copied"] == ["plugin.esp"]
            assert fake_mo2.added == ["TestMod"]
        finally:
            await lock_manager.close()

    async def test_propaga_cancelacion_async(self, fake_mo2: _FakeMO2) -> None:
        class _CancelHITL(_FakeHITL):
            async def request_approval(self, request_id: str, reason: str, **kwargs: Any) -> Decision:
                raise asyncio.CancelledError()

        installer = _FakeFomodInstaller()

        with pytest.raises(asyncio.CancelledError):
            await install_mod_from_archive(fake_mo2, installer, _CancelHITL(), "C:/mods/TestMod.zip", profile="Default")


# ---------------------------------------------------------------------------
# resolve_fomod
# ---------------------------------------------------------------------------


class TestResolveFomodContrato:
    async def test_sin_instalador_emite_contrato_canonico(self) -> None:
        payload = _cargar(await resolve_fomod(None, "C:/mods/TestMod.zip"))

        assert payload["success"] is False
        assert isinstance(payload["message"], str)
        assert "error" not in payload

    async def test_resuelve_archivos_a_instalar(self, fake_installer: _FakeFomodInstaller) -> None:
        fomod_xml = """\
        <config>
            <moduleName>TestMod</moduleName>
            <requiredInstallFiles>
                <file source="core/main.esp" destination="main.esp" />
            </requiredInstallFiles>
        </config>
        """
        installer = _FakeFomodInstaller(fomod_xml=fomod_xml)
        payload = _cargar(await resolve_fomod(installer, "C:/mods/TestMod.zip"))

        assert payload["success"] is True
        assert payload["message"] == ""
        assert "core/main.esp" in payload["files_to_install"]

    async def test_archive_sin_fomod_devuelve_failure(self) -> None:
        installer = _FakeFomodInstaller(fomod_xml=None)
        payload = _cargar(await resolve_fomod(installer, "C:/mods/Flat.zip"))

        assert payload["success"] is False
        assert "ModuleConfig" in payload["message"]

    async def test_xml_invalido_devuelve_failure(self) -> None:
        installer = _FakeFomodInstaller(fomod_xml="<config><unclosed>")
        payload = _cargar(await resolve_fomod(installer, "C:/mods/Roto.zip"))

        assert payload["success"] is False
        assert payload["message"] != ""

    async def test_rechaza_archive_fuera_del_sandbox(self, tmp_path: pathlib.Path) -> None:
        permitido = tmp_path / "permitido"
        permitido.mkdir()
        archive = tmp_path / "fuera.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("fomod/ModuleConfig.xml", "<config><moduleName>Fuera</moduleName></config>")
        installer = FomodInstaller(PathValidator(roots=[permitido]))

        payload = _cargar(await resolve_fomod(installer, str(archive)))

        assert payload["success"] is False
        assert payload["message"] != ""


# ---------------------------------------------------------------------------
# Wiring del registry: con FomodInstaller cableado las tools funcionan
# ---------------------------------------------------------------------------


class TestRegistryFomodCableado:
    def test_ancla_enumera_las_tres_tools_fomod(self, fake_mo2: _FakeMO2) -> None:
        """Ancla de superficie: las tres tools FOMOD deben estar registradas —
        si un camino nuevo agrega una herramienta FOMOD sin cablearla, este
        test se rompe (regla del repo: anclas que enumeran, no que muestrean)."""
        registry = AsyncToolRegistry(
            registry=None,
            mo2=fake_mo2,
            sync_engine=None,
            fomod_installer=_FakeFomodInstaller(),
        )

        assert {"preview_mod_installer", "install_mod_from_archive", "resolve_fomod"} <= set(registry.tools)

    def test_ancla_api_publica_de_los_helpers_de_lock(self) -> None:
        """El agente consume la API pública de la familia de instalación (T-31):
        si un rename dentro de tools_installer rompe el alias, este test lo
        atrapa en import time, no en runtime de instalación."""
        import sky_claw.local.tools_installer as tools_installer

        assert tools_installer.bajo_lock_de_instalacion is tools_installer._bajo_lock_de_instalacion
        assert tools_installer.install_lock_resource_id is tools_installer._install_lock_resource_id

    async def test_ejecuta_preview_con_instalador_cableado(self, fake_mo2: _FakeMO2) -> None:
        preview = FomodPreview(
            mod_name="TestMod",
            has_fomod=True,
            steps=[{"name": "Options", "groups": []}],
        )
        registry = AsyncToolRegistry(
            registry=None,
            mo2=fake_mo2,
            sync_engine=None,
            fomod_installer=_FakeFomodInstaller(preview=preview),
        )
        # El schema valida que el path viva dentro de los directorios sandbox.
        archive = pathlib.Path.home() / "Modding" / "TestMod.zip"

        payload = _cargar(await registry.execute("preview_mod_installer", {"archive_path": str(archive)}))

        assert payload["success"] is True
        assert payload["has_fomod"] is True
        assert payload["steps"][0]["name"] == "Options"

    async def test_ejecuta_install_y_resolve_via_registry(self, fake_mo2: _FakeMO2) -> None:
        """Las tres tools pasan por el registry con sus dependencias inyectadas:
        el wiring del lambda (mo2/hitl/installer) no puede quedar como no-op."""
        fomod_xml = """\
        <config>
            <moduleName>TestMod</moduleName>
            <requiredInstallFiles>
                <file source="core/main.esp" destination="main.esp" />
            </requiredInstallFiles>
        </config>
        """
        installer = _FakeFomodInstaller(
            install_result=InstallResult(mod_name="TestMod", files_copied=["main.esp"], installed=True),
            fomod_xml=fomod_xml,
        )
        registry = AsyncToolRegistry(
            registry=None,
            mo2=fake_mo2,
            sync_engine=None,
            hitl=_FakeHITL(),
            fomod_installer=installer,
            mo2_profile="Requiem",
        )
        archive = pathlib.Path.home() / "Modding" / "TestMod.zip"

        install_payload = _cargar(
            await registry.execute("install_mod_from_archive", {"archive_path": str(archive), "selections": {}})
        )
        assert install_payload["success"] is True
        assert install_payload["files_copied"] == ["main.esp"]
        assert fake_mo2.added == ["TestMod"]
        assert fake_mo2.added_profiles == ["Requiem"]

        resolve_payload = _cargar(await registry.execute("resolve_fomod", {"archive_path": str(archive)}))
        assert resolve_payload["success"] is True
        assert "core/main.esp" in resolve_payload["files_to_install"]

    async def test_sin_instalador_el_registry_responde_not_configured(self, fake_mo2: _FakeMO2) -> None:
        registry = AsyncToolRegistry(registry=None, mo2=fake_mo2, sync_engine=None)
        archive = pathlib.Path.home() / "Modding" / "TestMod.zip"

        payload = _cargar(await registry.execute("preview_mod_installer", {"archive_path": str(archive)}))

        assert payload["success"] is False
        assert "FOMOD installer" in payload["message"]

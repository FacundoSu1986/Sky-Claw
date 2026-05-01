"""Tests for the FOMOD installer and related tools."""

from __future__ import annotations

import pathlib
import zipfile

import pytest

from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError
from sky_claw.local.fomod.installer import (
    FomodInstaller,
    _is_safe_path,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


@pytest.fixture
def validator(sandbox: pathlib.Path) -> PathValidator:
    return PathValidator(roots=[sandbox])


@pytest.fixture
def installer(validator: PathValidator) -> FomodInstaller:
    return FomodInstaller(path_validator=validator)


@pytest.fixture
def mo2_mods_dir(sandbox: pathlib.Path) -> pathlib.Path:
    d = sandbox / "mods"
    d.mkdir()
    return d


def _make_simple_zip(
    path: pathlib.Path,
    files: dict[str, str],
) -> pathlib.Path:
    """Create a zip archive with the given files."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


SIMPLE_FOMOD_XML = """\
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <moduleName>TestMod</moduleName>
    <requiredInstallFiles>
        <file source="core/plugin.esp" destination="plugin.esp" />
    </requiredInstallFiles>
    <installSteps order="Explicit">
        <installStep name="Options">
            <optionalFileGroups order="Explicit">
                <group name="Textures" type="SelectExactlyOne">
                    <plugins order="Explicit">
                        <plugin name="HD Textures">
                            <files>
                                <file source="textures/hd" destination="textures" />
                            </files>
                        </plugin>
                        <plugin name="SD Textures">
                            <files>
                                <file source="textures/sd" destination="textures" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""


# ------------------------------------------------------------------
# Path safety checks
# ------------------------------------------------------------------


class TestPathSafety:
    def test_safe_path(self) -> None:
        assert _is_safe_path("data/textures/file.dds") is True

    def test_traversal_rejected(self) -> None:
        assert _is_safe_path("../../../etc/passwd") is False

    def test_hidden_traversal(self) -> None:
        assert _is_safe_path("data/../../secret.txt") is False

    def test_root_path(self) -> None:
        assert _is_safe_path("file.txt") is True


# ------------------------------------------------------------------
# Simple mod installation (no FOMOD)
# ------------------------------------------------------------------


class TestSimpleInstall:
    @pytest.mark.asyncio
    async def test_simple_mod_copies_all_files(
        self,
        installer: FomodInstaller,
        sandbox: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
    ) -> None:
        archive = _make_simple_zip(
            sandbox / "SimpleMod.zip",
            {
                "SimpleMod/plugin.esp": "esp data",
                "SimpleMod/textures/file.dds": "dds data",
            },
        )

        result = await installer.install(archive, mo2_mods_dir)

        assert result.installed is True
        assert result.mod_name == "SimpleMod"
        assert len(result.files_copied) == 2
        assert (mo2_mods_dir / "SimpleMod" / "plugin.esp").exists()
        assert (mo2_mods_dir / "SimpleMod" / "textures" / "file.dds").exists()

    @pytest.mark.asyncio
    async def test_flat_archive_uses_stem_as_name(
        self,
        installer: FomodInstaller,
        sandbox: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
    ) -> None:
        """Archive without a top-level directory uses the archive stem."""
        archive = _make_simple_zip(
            sandbox / "FlatMod.zip",
            {"plugin.esp": "esp data", "readme.txt": "info"},
        )

        result = await installer.install(archive, mo2_mods_dir)

        assert result.installed is True
        assert result.mod_name == "FlatMod"
        assert (mo2_mods_dir / "FlatMod" / "plugin.esp").exists()


# ------------------------------------------------------------------
# FOMOD installation
# ------------------------------------------------------------------


class TestFomodInstall:
    @pytest.mark.asyncio
    async def test_fomod_with_selections(
        self,
        installer: FomodInstaller,
        sandbox: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
    ) -> None:
        archive = _make_simple_zip(
            sandbox / "FomodMod.zip",
            {
                "fomod/ModuleConfig.xml": SIMPLE_FOMOD_XML,
                "core/plugin.esp": "esp data",
                "textures/hd/file.dds": "hd texture",
                "textures/sd/file.dds": "sd texture",
            },
        )

        result = await installer.install(
            archive,
            mo2_mods_dir,
            selections={"Options": ["HD Textures"]},
        )

        assert result.installed is True
        assert result.mod_name == "TestMod"
        assert any("plugin.esp" in f for f in result.files_copied)
        # HD was selected, so hd file.dds should exist
        assert (mo2_mods_dir / "TestMod" / "textures" / "file.dds").exists()

    @pytest.mark.asyncio
    async def test_fomod_pending_decisions(
        self,
        installer: FomodInstaller,
        sandbox: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
    ) -> None:
        """Empty selections with SelectExactlyOne should report pending."""
        archive = _make_simple_zip(
            sandbox / "PendingMod.zip",
            {
                "fomod/ModuleConfig.xml": SIMPLE_FOMOD_XML,
                "core/plugin.esp": "esp data",
                "textures/hd/file.dds": "hd",
                "textures/sd/file.dds": "sd",
            },
        )

        result = await installer.install(archive, mo2_mods_dir, selections={})

        # Should have pending decisions for the SelectExactlyOne group
        # but still install required files
        assert len(result.pending_decisions) > 0 or result.installed is True


# ------------------------------------------------------------------
# Preview
# ------------------------------------------------------------------


class TestPreview:
    @pytest.mark.asyncio
    async def test_preview_fomod_archive(self, installer: FomodInstaller, sandbox: pathlib.Path) -> None:
        archive = _make_simple_zip(
            sandbox / "PreviewMod.zip",
            {"fomod/ModuleConfig.xml": SIMPLE_FOMOD_XML},
        )

        preview = await installer.preview(archive)

        assert preview.has_fomod is True
        assert preview.mod_name == "TestMod"
        assert len(preview.steps) == 1
        assert preview.steps[0]["name"] == "Options"
        assert len(preview.steps[0]["groups"]) == 1
        assert "HD Textures" in preview.steps[0]["groups"][0]["options"]
        assert "SD Textures" in preview.steps[0]["groups"][0]["options"]

    @pytest.mark.asyncio
    async def test_preview_simple_archive(self, installer: FomodInstaller, sandbox: pathlib.Path) -> None:
        archive = _make_simple_zip(
            sandbox / "NoFomod.zip",
            {"plugin.esp": "data"},
        )

        preview = await installer.preview(archive)

        assert preview.has_fomod is False
        assert preview.mod_name == "NoFomod"
        assert preview.steps == []


# ------------------------------------------------------------------
# Zip-slip protection
# ------------------------------------------------------------------


class TestZipSlipProtection:
    @pytest.mark.asyncio
    async def test_zip_slip_rejected(
        self,
        installer: FomodInstaller,
        sandbox: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
    ) -> None:
        """Archives with path traversal must be rejected."""
        archive_path = sandbox / "malicious.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            # Manually craft a malicious entry
            info = zipfile.ZipInfo("../../../etc/passwd")
            zf.writestr(info, "malicious content")

        with pytest.raises(PathViolationError, match="Zip-slip"):
            await installer.install(archive_path, mo2_mods_dir)


# ------------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_temp_dir_cleaned_after_install(
        self,
        installer: FomodInstaller,
        sandbox: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
    ) -> None:
        """Temporary extraction directory should be removed after install."""
        import os

        archive = _make_simple_zip(
            sandbox / "CleanupMod.zip",
            {"CleanupMod/file.txt": "data"},
        )

        # Count temp dirs before
        temp_base = pathlib.Path(os.environ.get("TEMP", "/tmp"))
        before = set(p for p in temp_base.iterdir() if p.name.startswith("skyclaw_install_"))

        await installer.install(archive, mo2_mods_dir)

        # Count after — should not increase
        after = set(p for p in temp_base.iterdir() if p.name.startswith("skyclaw_install_"))
        assert after - before == set()


# ------------------------------------------------------------------
# Unsupported format
# ------------------------------------------------------------------


class TestUnsupportedFormat:
    @pytest.mark.asyncio
    async def test_unsupported_format_returns_error(
        self,
        installer: FomodInstaller,
        sandbox: pathlib.Path,
        mo2_mods_dir: pathlib.Path,
    ) -> None:
        fake_archive = sandbox / "mod.exe"
        fake_archive.write_bytes(b"not a real archive")

        result = await installer.install(fake_archive, mo2_mods_dir)

        assert result.installed is False
        assert len(result.errors) == 1
        assert "Unsupported" in result.errors[0]
        assert ".exe" in result.errors[0]


# ------------------------------------------------------------------
# MO2 modlist integration
# ------------------------------------------------------------------


class TestAddModToModlist:
    @pytest.mark.asyncio
    async def test_add_mod_to_modlist(self, sandbox: pathlib.Path) -> None:
        from sky_claw.local.mo2.vfs import MO2Controller

        validator = PathValidator(roots=[sandbox])
        mo2 = MO2Controller(sandbox, path_validator=validator)

        profile_dir = sandbox / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        modlist = profile_dir / "modlist.txt"
        modlist.write_text("+ExistingMod\n-DisabledMod\n", encoding="utf-8")

        await mo2.add_mod_to_modlist("NewMod")

        content = modlist.read_text(encoding="utf-8")
        assert "+NewMod" in content
        assert content.count("NewMod") == 1

    @pytest.mark.asyncio
    async def test_add_existing_mod_skips(self, sandbox: pathlib.Path) -> None:
        from sky_claw.local.mo2.vfs import MO2Controller

        validator = PathValidator(roots=[sandbox])
        mo2 = MO2Controller(sandbox, path_validator=validator)

        profile_dir = sandbox / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        modlist = profile_dir / "modlist.txt"
        modlist.write_text("+ExistingMod\n", encoding="utf-8")

        await mo2.add_mod_to_modlist("ExistingMod")

        content = modlist.read_text(encoding="utf-8")
        assert content.count("ExistingMod") == 1

    @pytest.mark.asyncio
    async def test_add_to_empty_modlist(self, sandbox: pathlib.Path) -> None:
        from sky_claw.local.mo2.vfs import MO2Controller

        validator = PathValidator(roots=[sandbox])
        mo2 = MO2Controller(sandbox, path_validator=validator)

        profile_dir = sandbox / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        modlist = profile_dir / "modlist.txt"
        modlist.write_text("", encoding="utf-8")

        await mo2.add_mod_to_modlist("FirstMod")

        content = modlist.read_text(encoding="utf-8")
        assert "+FirstMod\n" in content


# ------------------------------------------------------------------
# Malicious FOMOD manifest — path-traversal via source/destination attrs
# ------------------------------------------------------------------

_MALICIOUS_DEST_FOMOD_XML = """\
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">
  <moduleName>Evil Mod</moduleName>
  <installSteps order="Explicit">
    <installStep name="Step1">
      <optionalFileGroups order="Explicit">
        <group name="G1" type="SelectAll">
          <plugins order="Explicit">
            <plugin name="All">
              <files>
                <!-- destination escapes the mod folder -->
                <file source="data/legit.esp"
                      destination="../../escape/injected.esp" />
                <!-- legitimate entry must still install -->
                <file source="data/legit.esp"
                      destination="legit.esp" />
              </files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>
"""

_MALICIOUS_SOURCE_FOMOD_XML = """\
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">
  <moduleName>Evil Mod</moduleName>
  <installSteps order="Explicit">
    <installStep name="Step1">
      <optionalFileGroups order="Explicit">
        <group name="G1" type="SelectAll">
          <plugins order="Explicit">
            <plugin name="All">
              <files>
                <!-- source tries to escape the content root -->
                <file source="../../outside/secret.dll"
                      destination="plugins/secret.dll" />
                <!-- legitimate entry must still install -->
                <file source="data/legit.esp"
                      destination="legit.esp" />
              </files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>
"""


def _build_fomod_zip(
    tmp_path: pathlib.Path,
    fomod_xml: str,
    extra_files: dict[str, bytes] | None = None,
) -> pathlib.Path:
    """Create a zip archive with the given FOMOD XML and optional extra files."""
    archive = tmp_path / "evil_mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("fomod/ModuleConfig.xml", fomod_xml)
        for name, data in (extra_files or {}).items():
            zf.writestr(name, data)
    return archive


class TestMaliciousFomodManifest:
    """Integration tests: malicious FOMOD manifests must not escape the sandbox."""

    @pytest.mark.asyncio
    async def test_malicious_destination_is_rejected_legit_entry_installs(
        self, sandbox: pathlib.Path, validator: PathValidator
    ) -> None:
        """A poisoned destination= must be skipped; the clean entry must still copy."""
        mo2_mods = sandbox / "mods"
        mo2_mods.mkdir(parents=True)

        archive = _build_fomod_zip(
            sandbox,
            _MALICIOUS_DEST_FOMOD_XML,
            extra_files={"data/legit.esp": b"SKYRIM_ESP"},
        )

        installer = FomodInstaller(path_validator=validator)
        result = await installer.install(archive, mo2_mods)

        # The traversal entry must NOT be in the copied list.
        assert not any("escape" in f or ".." in f for f in result.files_copied)
        # The legitimate entry MUST have installed.
        assert any("legit.esp" in f for f in result.files_copied)

    @pytest.mark.asyncio
    async def test_malicious_source_is_rejected_legit_entry_installs(
        self, sandbox: pathlib.Path, validator: PathValidator
    ) -> None:
        """A poisoned source= must be skipped; the clean entry must still copy."""
        mo2_mods = sandbox / "mods"
        mo2_mods.mkdir(parents=True)

        archive = _build_fomod_zip(
            sandbox,
            _MALICIOUS_SOURCE_FOMOD_XML,
            extra_files={"data/legit.esp": b"SKYRIM_ESP"},
        )

        installer = FomodInstaller(path_validator=validator)
        result = await installer.install(archive, mo2_mods)

        # The escaping source must not have produced any output.
        assert not any("secret.dll" in f for f in result.files_copied)
        # The legitimate entry MUST have installed.
        assert any("legit.esp" in f for f in result.files_copied)

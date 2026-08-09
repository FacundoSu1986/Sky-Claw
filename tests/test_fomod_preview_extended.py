"""Preview extendido del FomodInstaller (T-5).

Cierra la limitación 5 de la auditoría FOMOD: el preview solo leía zips y
reportaba ``has_fomod=False`` para 7z/rar (falso negativo). Además enriquece
las opciones con descripción, imagen, tipo y dependencias para que el agente
pueda decidir con información real.
"""

from __future__ import annotations

import pathlib
import zipfile

import py7zr
import pytest
import rarfile

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.fomod.installer import FomodInstaller, _is_fomod_xml_name

PREVIEW_XML = """\
<config>
    <moduleName>PreviewMod</moduleName>
    <installSteps order="Explicit">
        <installStep name="Options">
            <optionalFileGroups order="Explicit">
                <group name="Textures" type="SelectExactlyOne">
                    <plugins order="Explicit">
                        <plugin name="HD Textures">
                            <description>Alta resolución</description>
                            <image path="images/hd.png" />
                            <typeDescriptor><type name="Recommended" /></typeDescriptor>
                            <files>
                                <file source="textures/hd" destination="textures" />
                            </files>
                        </plugin>
                        <plugin name="Parche USSEP">
                            <description>Parche para USSEP</description>
                            <typeDescriptor>
                                <dependencyType defaultType="Recommended" operator="And">
                                    <dependencies>
                                        <fileDependency file="USSEP.esp" state="Active" />
                                    </dependencies>
                                    <patterns>
                                        <pattern>
                                            <type name="NotUsable" />
                                            <dependencies>
                                                <fileDependency file="OldMod.esp" state="Active" />
                                            </dependencies>
                                        </pattern>
                                    </patterns>
                                </dependencyType>
                                <type name="Optional" />
                            </typeDescriptor>
                            <files>
                                <file source="patches/ussep.esp" destination="ussep.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""


@pytest.fixture()
def installer(tmp_path: pathlib.Path) -> FomodInstaller:
    return FomodInstaller(path_validator=PathValidator(roots=[tmp_path]))


# ---------------------------------------------------------------------------
# Detección del nombre del ModuleConfig
# ---------------------------------------------------------------------------


class TestIsFomodXmlName:
    def test_reconoce_path_canonico(self) -> None:
        assert _is_fomod_xml_name("fomod/ModuleConfig.xml") is True

    def test_case_insensitive_y_backslashes(self) -> None:
        assert _is_fomod_xml_name("FOMOD\\moduleconfig.xml") is True

    def test_rechaza_otros_xml(self) -> None:
        assert _is_fomod_xml_name("data/plugin.xml") is False

    def test_rechaza_traversal(self) -> None:
        assert _is_fomod_xml_name("../../fomod/ModuleConfig.xml") is False

    def test_rechaza_path_absoluto_posix(self) -> None:
        assert _is_fomod_xml_name("/fomod/ModuleConfig.xml") is False

    def test_rechaza_path_absoluto_windows(self) -> None:
        assert _is_fomod_xml_name("C:/fomod/ModuleConfig.xml") is False


# ---------------------------------------------------------------------------
# Preview de .7z (lectura selectiva real con py7zr)
# ---------------------------------------------------------------------------


class TestPreview7z:
    async def test_preview_7z_detecta_fomod(self, installer: FomodInstaller, tmp_path: pathlib.Path) -> None:
        archive = tmp_path / "Mod.7z"
        with py7zr.SevenZipFile(archive, "w") as szf:
            # py7zr >= 1.0: writestr(data, arcname) — orden invertido.
            szf.writestr(PREVIEW_XML.encode("utf-8"), "fomod/ModuleConfig.xml")

        preview = await installer.preview(archive)

        assert preview.has_fomod is True
        assert preview.mod_name == "PreviewMod"

    async def test_preview_7z_sin_fomod(self, installer: FomodInstaller, tmp_path: pathlib.Path) -> None:
        archive = tmp_path / "Flat.7z"
        with py7zr.SevenZipFile(archive, "w") as szf:
            szf.writestr(b"esp", "plugin.esp")

        preview = await installer.preview(archive)

        assert preview.has_fomod is False


# ---------------------------------------------------------------------------
# Preview de .rar (con doble de rarfile: no se puede crear un .rar real)
# ---------------------------------------------------------------------------


class _FakeRarInfo:
    def __init__(self, filename: str) -> None:
        self.filename = filename


class _FakeRarFile:
    def __init__(self, path: str, mode: str) -> None:
        self._entries = {
            "fomod/ModuleConfig.xml": PREVIEW_XML.encode("utf-8"),
            "plugin.esp": b"esp",
        }

    @classmethod
    def con_entradas(cls, entries: dict[str, bytes]) -> type[_FakeRarFile]:
        """Devuelve una subclase con las entradas dadas (para el caso sin FOMOD)."""

        class _ConEntradas(cls):  # type: ignore[misc, valid-type]
            def __init__(self, path: str, mode: str) -> None:
                self._entries = entries

        return _ConEntradas

    def __enter__(self) -> _FakeRarFile:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def infolist(self) -> list[_FakeRarInfo]:
        return [_FakeRarInfo(name) for name in self._entries]

    def read(self, name: str) -> bytes:
        return self._entries[name]


class TestPreviewRar:
    async def test_preview_rar_detecta_fomod(
        self,
        installer: FomodInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(rarfile, "RarFile", _FakeRarFile)
        archive = tmp_path / "Mod.rar"
        archive.write_bytes(b"fake")

        preview = await installer.preview(archive)

        assert preview.has_fomod is True
        assert preview.mod_name == "PreviewMod"

    async def test_preview_rar_sin_fomod(
        self,
        installer: FomodInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sin_fomod = _FakeRarFile.con_entradas({"plugin.esp": b"esp"})
        monkeypatch.setattr(rarfile, "RarFile", sin_fomod)
        archive = tmp_path / "Flat.rar"
        archive.write_bytes(b"fake")

        preview = await installer.preview(archive)

        assert preview.has_fomod is False

    async def test_preview_rar_sin_binario_unrar_degrada_a_sin_fomod(
        self,
        installer: FomodInstaller,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sin el binario ``unrar``, rarfile lanza RarCannotExec: el preview
        debe degradar a has_fomod=False, no reventar la tool."""

        class _RarSinUnrar:
            def __init__(self, path: str, mode: str) -> None:
                raise rarfile.RarCannotExec("unrar not found")

        monkeypatch.setattr(rarfile, "RarFile", _RarSinUnrar)
        archive = tmp_path / "Mod.rar"
        archive.write_bytes(b"fake")

        preview = await installer.preview(archive)

        assert preview.has_fomod is False


# ---------------------------------------------------------------------------
# Detalle de plugins en el preview
# ---------------------------------------------------------------------------


class TestPreviewDetallePlugins:
    async def test_preview_incluye_descripcion_tipo_y_dependencia(
        self, installer: FomodInstaller, tmp_path: pathlib.Path
    ) -> None:
        archive = tmp_path / "Mod.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("fomod/ModuleConfig.xml", PREVIEW_XML)

        preview = await installer.preview(archive)

        group = preview.steps[0]["groups"][0]
        details = group["plugin_details"]
        assert len(details) == 2

        hd = details[0]
        assert hd["name"] == "HD Textures"
        assert hd["description"] == "Alta resolución"
        assert hd["image"] == "images/hd.png"
        assert hd["type"] == "Recommended"
        assert hd["dependency"] is None

        ussep = details[1]
        assert ussep["type"] == "Optional"
        assert ussep["dependency"]["operator"] == "And"
        assert ussep["dependency"]["file_dependencies"] == [{"file": "USSEP.esp", "state": "Active"}]
        # El detalle expone la semántica que usa resolve_fomod: default_type y
        # los patterns (NotUsable/CouldBeUsable) con sus condiciones.
        assert ussep["default_type"] == "Recommended"
        assert len(ussep["dependency_patterns"]) == 1
        assert ussep["dependency_patterns"][0]["type"] == "NotUsable"
        assert ussep["dependency_patterns"][0]["conditions"]["file_dependencies"] == [
            {"file": "OldMod.esp", "state": "Active"}
        ]

        # Compat: la lista de nombres se conserva para consumidores previos.
        assert group["options"] == ["HD Textures", "Parche USSEP"]

    async def test_preview_sin_fomod_no_incluye_detalles(
        self, installer: FomodInstaller, tmp_path: pathlib.Path
    ) -> None:
        archive = tmp_path / "Flat.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("plugin.esp", "esp")

        preview = await installer.preview(archive)

        assert preview.has_fomod is False
        assert preview.steps == []

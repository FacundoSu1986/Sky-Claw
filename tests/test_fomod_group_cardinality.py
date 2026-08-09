"""Validación estricta de cardinalidad de grupos FOMOD.

Riesgo #4 de la auditoría FOMOD: una selección inválida (dos opciones
mutuamente excluyentes en ``SelectExactlyOne``) se instalaba silenciosamente.
Este archivo fija el fail-closed: selección inválida → nada de ese grupo se
instala y queda una decisión pendiente. ``Required`` define un grupo de
selección única (el obligatorio manda).
"""

from __future__ import annotations

import pathlib
import zipfile

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.fomod.installer import FomodInstaller
from sky_claw.local.fomod.parser import parse_fomod_string
from sky_claw.local.fomod.resolver import FomodResolver

FOMOD_GRUPOS = """\
<config>
    <moduleName>ModGrupos</moduleName>
    <installSteps order="Explicit">
        <installStep name="Opciones">
            <optionalFileGroups order="Explicit">
                <group name="Exacto" type="SelectExactlyOne">
                    <plugins order="Explicit">
                        <plugin name="A">
                            <files><file source="a/x.esp" destination="x.esp" /></files>
                        </plugin>
                        <plugin name="B">
                            <files><file source="b/x.esp" destination="x.esp" /></files>
                        </plugin>
                    </plugins>
                </group>
                <group name="MaxUno" type="SelectAtMostOne">
                    <plugins order="Explicit">
                        <plugin name="C">
                            <files><file source="c/c.esp" destination="c.esp" /></files>
                        </plugin>
                        <plugin name="D">
                            <files><file source="d/d.esp" destination="d.esp" /></files>
                        </plugin>
                    </plugins>
                </group>
                <group name="MinUno" type="SelectAtLeastOne">
                    <plugins order="Explicit">
                        <plugin name="E">
                            <files><file source="e/e.esp" destination="e.esp" /></files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""

FOMOD_REQUIRED_EN_EXACTO = """\
<config>
    <moduleName>ModRequeridoExacto</moduleName>
    <installSteps order="Explicit">
        <installStep name="Core">
            <optionalFileGroups order="Explicit">
                <group name="Motor" type="SelectExactlyOne">
                    <plugins order="Explicit">
                        <plugin name="Core">
                            <typeDescriptor><type name="Required" /></typeDescriptor>
                            <files><file source="core/engine.esp" destination="engine.esp" /></files>
                        </plugin>
                        <plugin name="Lite">
                            <typeDescriptor><type name="Optional" /></typeDescriptor>
                            <files><file source="lite/engine.esp" destination="engine.esp" /></files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""


def _config(fomod_xml: str):
    return parse_fomod_string(fomod_xml)


class TestCardinalidadDeGrupos:
    def test_exactamente_uno_con_una_seleccion_ok(self) -> None:
        resolver = FomodResolver(_config(FOMOD_GRUPOS))

        result = resolver.resolve({"Opciones": ["A"]})

        sources = [f.source for f in result.files]
        assert "a/x.esp" in sources
        assert "b/x.esp" not in sources

    def test_exactamente_uno_con_dos_selecciones_rechaza_ambas(self) -> None:
        """Selección inválida: dos mutuamente excluyentes → fail-closed, ninguna."""
        resolver = FomodResolver(_config(FOMOD_GRUPOS))

        result = resolver.resolve({"Opciones": ["A", "B"]})

        sources = [f.source for f in result.files]
        assert "a/x.esp" not in sources
        assert "b/x.esp" not in sources
        assert any("Exacto" in d for d in result.pending_decisions)

    def test_at_most_one_con_dos_selecciones_rechaza(self) -> None:
        resolver = FomodResolver(_config(FOMOD_GRUPOS))

        result = resolver.resolve({"Opciones": ["C", "D"]})

        sources = [f.source for f in result.files]
        assert "c/c.esp" not in sources
        assert "d/d.esp" not in sources
        assert any("MaxUno" in d for d in result.pending_decisions)

    def test_at_most_one_con_cero_ok(self) -> None:
        resolver = FomodResolver(_config(FOMOD_GRUPOS))

        result = resolver.resolve({"Opciones": ["A"]})

        sources = [f.source for f in result.files]
        assert "c/c.esp" not in sources
        assert "d/d.esp" not in sources
        assert not any("MaxUno" in d for d in result.pending_decisions)

    def test_at_least_one_sin_seleccion_pendiente(self) -> None:
        resolver = FomodResolver(_config(FOMOD_GRUPOS))

        result = resolver.resolve({"Opciones": ["A"]})

        assert any("MinUno" in d for d in result.pending_decisions)

    def test_required_en_grupo_exacto_define_el_grupo(self) -> None:
        """Required en SelectExactlyOne: el obligatorio gana y la selección del
        usuario en ese grupo (incompatible por cardinalidad) se descarta."""
        resolver = FomodResolver(_config(FOMOD_REQUIRED_EN_EXACTO))

        result = resolver.resolve({"Core": ["Lite"]})

        sources = [f.source for f in result.files]
        assert "core/engine.esp" in sources
        assert "lite/engine.esp" not in sources
        assert result.pending_decisions == []


class TestInstallerCardinalidad:
    async def test_install_rechaza_seleccion_invalida_sin_copiar(self, tmp_path: pathlib.Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        mo2_mods_dir = sandbox / "mods"
        mo2_mods_dir.mkdir()

        archive = sandbox / "ModGrupos.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("fomod/ModuleConfig.xml", FOMOD_GRUPOS)
            zf.writestr("a/x.esp", "a")
            zf.writestr("b/x.esp", "b")

        installer = FomodInstaller(path_validator=PathValidator(roots=[sandbox]))
        # Dos opciones mutuamente excluyentes: la instalación no copia nada.
        result = await installer.install(archive, mo2_mods_dir, selections={"Opciones": ["A", "B"]})

        assert result.installed is False
        assert len(result.pending_decisions) >= 1
        assert result.files_copied == []
        assert not (mo2_mods_dir / "ModGrupos").exists()

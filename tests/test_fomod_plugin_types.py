"""typeDescriptor / dependencyType de FOMOD en parser y resolver.

La auditoría FOMOD marcó que ``typeDescriptor`` se parseaba pero nunca influía
en la resolución. Este archivo fija la semántica: ``Required`` se fuerza,
``Recommended`` se preselecciona, y ``dependencyType`` condiciona la
elegibilidad del plugin (no se instala un parche cuyo requisito no se cumple).
"""

from __future__ import annotations

import pathlib
import zipfile

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.fomod.installer import FomodInstaller
from sky_claw.local.fomod.models import FomodConfig, PluginType
from sky_claw.local.fomod.parser import parse_fomod_string
from sky_claw.local.fomod.plugin_state import MO2PluginStateProvider
from sky_claw.local.fomod.resolver import FomodResolver
from tests.conftest import crear_arbol_mo2

FOMOD_REQUIRED_RECOMMENDED = """\
<config>
    <moduleName>ModTipos</moduleName>
    <installSteps order="Explicit">
        <installStep name="Componentes">
            <optionalFileGroups order="Explicit">
                <group name="Base" type="SelectAny">
                    <plugins order="Explicit">
                        <plugin name="Core">
                            <typeDescriptor><type name="Required" /></typeDescriptor>
                            <files>
                                <file source="core/plugin.esp" destination="plugin.esp" />
                            </files>
                        </plugin>
                        <plugin name="Lite">
                            <typeDescriptor><type name="Optional" /></typeDescriptor>
                            <files>
                                <file source="lite/plugin.esp" destination="plugin.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
                <group name="Extras" type="SelectAny">
                    <plugins order="Explicit">
                        <plugin name="Recomendado">
                            <typeDescriptor><type name="Recommended" /></typeDescriptor>
                            <files>
                                <file source="rec/extra.esp" destination="extra.esp" />
                            </files>
                        </plugin>
                        <plugin name="Extra2">
                            <typeDescriptor><type name="Optional" /></typeDescriptor>
                            <files>
                                <file source="extra2/extra2.esp" destination="extra2.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""

FOMOD_DEPENDENCY_TYPE = """\
<config>
    <moduleName>ModDependiente</moduleName>
    <installSteps order="Explicit">
        <installStep name="Parches">
            <optionalFileGroups order="Explicit">
                <group name="Compat" type="SelectAny">
                    <plugins order="Explicit">
                        <plugin name="Parche USSEP">
                            <typeDescriptor>
                                <dependencyType operator="And">
                                    <dependencies>
                                        <fileDependency file="USSEP.esp" state="Active" />
                                    </dependencies>
                                </dependencyType>
                                <type name="Optional" />
                            </typeDescriptor>
                            <files>
                                <file source="patches/ussep.esp" destination="ussep.esp" />
                            </files>
                        </plugin>
                        <plugin name="Parche LOTD">
                            <typeDescriptor>
                                <dependencyType operator="And">
                                    <dependencies>
                                        <fileDependency file="LotD.esp" state="Active" />
                                    </dependencies>
                                </dependencyType>
                                <type name="Optional" />
                            </typeDescriptor>
                            <files>
                                <file source="patches/lotd.esp" destination="lotd.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""

FOMOD_REQUIRED_CON_DEPENDENCIA = """\
<config>
    <moduleName>ModRequeridoCondicionado</moduleName>
    <installSteps order="Explicit">
        <installStep name="Core">
            <optionalFileGroups order="Explicit">
                <group name="Motor" type="SelectExactlyOne">
                    <plugins order="Explicit">
                        <plugin name="MotorSE">
                            <typeDescriptor>
                                <dependencyType operator="And">
                                    <dependencies>
                                        <fileDependency file="AddressLibrary.esp" state="Active" />
                                    </dependencies>
                                </dependencyType>
                                <type name="Required" />
                            </typeDescriptor>
                            <files>
                                <file source="se/engine.esp" destination="engine.esp" />
                            </files>
                        </plugin>
                        <plugin name="MotorFallback">
                            <typeDescriptor><type name="Optional" /></typeDescriptor>
                            <files>
                                <file source="fallback/engine.esp" destination="engine.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""


def _configs(fomod_xml: str) -> FomodConfig:
    return parse_fomod_string(fomod_xml)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParserPluginTypes:
    def test_type_descriptor_required(self) -> None:
        config = _configs(FOMOD_REQUIRED_RECOMMENDED)
        plugin_core = config.install_steps[0].groups[0].plugins[0]

        assert plugin_core.type_descriptor == PluginType.REQUIRED

    def test_type_descriptor_recommended(self) -> None:
        config = _configs(FOMOD_REQUIRED_RECOMMENDED)
        plugin_rec = config.install_steps[0].groups[1].plugins[0]

        assert plugin_rec.type_descriptor == PluginType.RECOMMENDED

    def test_type_descriptor_optional_por_default(self) -> None:
        config = _configs(FOMOD_REQUIRED_RECOMMENDED)
        plugin_lite = config.install_steps[0].groups[0].plugins[1]

        assert plugin_lite.type_descriptor == PluginType.OPTIONAL

    def test_type_descriptor_invalido_cae_a_optional(self) -> None:
        xml = """\
        <config>
            <installSteps order="Explicit">
                <installStep name="S">
                    <optionalFileGroups order="Explicit">
                        <group name="G" type="SelectAny">
                            <plugins order="Explicit">
                                <plugin name="P">
                                    <typeDescriptor><type name="Weird" /></typeDescriptor>
                                </plugin>
                            </plugins>
                        </group>
                    </optionalFileGroups>
                </installStep>
            </installSteps>
        </config>
        """
        config = _configs(xml)

        assert config.install_steps[0].groups[0].plugins[0].type_descriptor == PluginType.OPTIONAL

    def test_dependency_type_se_parsea(self) -> None:
        config = _configs(FOMOD_DEPENDENCY_TYPE)
        plugin_ussep = config.install_steps[0].groups[0].plugins[0]

        assert plugin_ussep.dependency is not None
        assert len(plugin_ussep.dependency.file_deps) == 1
        assert plugin_ussep.dependency.file_deps[0].file == "USSEP.esp"


# ---------------------------------------------------------------------------
# Resolver: Required / Recommended / dependencyType
# ---------------------------------------------------------------------------


class TestResolverPluginTypes:
    def test_required_se_fuerza_sin_seleccion(self) -> None:
        config = _configs(FOMOD_REQUIRED_RECOMMENDED)
        resolver = FomodResolver(config)

        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "core/plugin.esp" in sources
        # Recommended preselecciona en el grupo sin selección explícita.
        assert "rec/extra.esp" in sources
        # Sin selección y sin pending: ambos grupos se resolvieron.
        assert result.pending_decisions == []

    def test_recommended_no_pisa_seleccion_explicita(self) -> None:
        config = _configs(FOMOD_REQUIRED_RECOMMENDED)
        resolver = FomodResolver(config)

        # Selección explícita en ambos grupos (Lite en Base, Extra2 en Extras).
        result = resolver.resolve({"Componentes": ["Lite", "Extra2"]})

        sources = [f.source for f in result.files]
        # Required siempre está, aunque el usuario eligió Lite.
        assert "core/plugin.esp" in sources
        assert "lite/plugin.esp" in sources
        assert "extra2/extra2.esp" in sources
        # La selección explícita de Extras gana sobre Recommended.
        assert "rec/extra.esp" not in sources

    def test_plugin_con_dependency_type_satisfecho_se_puede_elegir(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+USSEP\n",
            mods={"USSEP": {"USSEP.esp": "esp"}},
        )
        config = _configs(FOMOD_DEPENDENCY_TYPE)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        result = resolver.resolve({"Parches": ["Parche USSEP"]})

        sources = [f.source for f in result.files]
        assert "patches/ussep.esp" in sources

    def test_plugin_con_dependency_type_insatisfecho_se_excluye(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+USSEP\n",
            mods={"USSEP": {"USSEP.esp": "esp"}},
        )
        config = _configs(FOMOD_DEPENDENCY_TYPE)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        # El usuario pide el parche de LOTD, pero LotD.esp no está instalado.
        result = resolver.resolve({"Parches": ["Parche LOTD"]})

        sources = [f.source for f in result.files]
        assert "patches/lotd.esp" not in sources

    def test_required_con_dependencia_insatisfecha_no_se_fuerza(self, tmp_path: pathlib.Path) -> None:
        """Un Required cuyo dependencyType no se cumple no puede instalarse:
        sin AddressLibrary, forzar el plugin rompería el load order."""
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+OtroMod\n",
            mods={"OtroMod": {"OtroMod.esp": "esp"}},
        )
        config = _configs(FOMOD_REQUIRED_CON_DEPENDENCIA)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "se/engine.esp" not in sources
        # El grupo queda sin resolución: decisión pendiente.
        assert len(result.pending_decisions) >= 1

    def test_required_con_dependencia_satisfecha_se_fuerza(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+AddressLibrary\n",
            mods={"AddressLibrary": {"AddressLibrary.esp": "esp"}},
        )
        config = _configs(FOMOD_REQUIRED_CON_DEPENDENCIA)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "se/engine.esp" in sources


# ---------------------------------------------------------------------------
# Integración: FomodInstaller aplica los tipos durante la instalación
# ---------------------------------------------------------------------------


class TestInstallerPluginTypes:
    async def test_install_aplica_required_y_recommended(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+Base\n",
            mods={"Base": {"base.esp": "esp"}},
        )
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        mo2_mods_dir = sandbox / "mods"
        mo2_mods_dir.mkdir()

        archive = sandbox / "ModTipos.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("fomod/ModuleConfig.xml", FOMOD_REQUIRED_RECOMMENDED)
            zf.writestr("core/plugin.esp", "core")
            zf.writestr("lite/plugin.esp", "lite")
            zf.writestr("rec/extra.esp", "rec")

        installer = FomodInstaller(
            path_validator=PathValidator(roots=[sandbox]),
            file_state_provider=MO2PluginStateProvider(mo2_root),
        )
        result = await installer.install(archive, mo2_mods_dir, selections={})

        assert result.installed is True
        copied = [f for f in result.files_copied]
        assert any(f.endswith("plugin.esp") for f in copied)
        assert any(f.endswith("extra.esp") for f in copied)


# ---------------------------------------------------------------------------
# dependencyType: defaultType y patterns (estándar FOMOD completo)
# ---------------------------------------------------------------------------

FOMOD_DEFAULT_TYPE = """\
<config>
    <moduleName>ModDefaultType</moduleName>
    <installSteps order="Explicit">
        <installStep name="Compat">
            <optionalFileGroups order="Explicit">
                <group name="Parches" type="SelectAny">
                    <plugins order="Explicit">
                        <plugin name="Parche Ligero">
                            <typeDescriptor>
                                <dependencyType defaultType="Recommended" operator="And">
                                    <dependencies>
                                        <fileDependency file="USSEP.esp" state="Active" />
                                    </dependencies>
                                </dependencyType>
                                <type name="Optional" />
                            </typeDescriptor>
                            <files>
                                <file source="light/light.esp" destination="light.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""

FOMOD_PATTERNS = """\
<config>
    <moduleName>ModPatterns</moduleName>
    <installSteps order="Explicit">
        <installStep name="Base">
            <optionalFileGroups order="Explicit">
                <group name="Modo" type="SelectExactlyOne">
                    <plugins order="Explicit">
                        <plugin name="Avanzado">
                            <conditionFlags>
                                <flag name="modo">avanzado</flag>
                            </conditionFlags>
                            <files>
                                <file source="av/plugin.esp" destination="plugin.esp" />
                            </files>
                        </plugin>
                        <plugin name="Simple">
                            <files>
                                <file source="simple/plugin.esp" destination="plugin.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
        <installStep name="Compat">
            <optionalFileGroups order="Explicit">
                <group name="Parches" type="SelectAny">
                    <plugins order="Explicit">
                        <plugin name="ParcheAvanzado">
                            <typeDescriptor>
                                <dependencyType operator="And">
                                    <patterns>
                                        <pattern>
                                            <type name="NotUsable" />
                                            <dependencies>
                                                <fileDependency file="OldMod.esp" state="Active" />
                                            </dependencies>
                                        </pattern>
                                        <pattern>
                                            <type name="CouldBeUsable" />
                                            <dependencies>
                                                <flagDependency flag="modo" value="avanzado" />
                                            </dependencies>
                                        </pattern>
                                    </patterns>
                                </dependencyType>
                                <type name="Optional" />
                            </typeDescriptor>
                            <files>
                                <file source="compat/av.esp" destination="av.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
</config>
"""


class TestParserDependencyTypeExtendido:
    def test_parsea_default_type(self) -> None:
        config = _configs(FOMOD_DEFAULT_TYPE)
        plugin = config.install_steps[0].groups[0].plugins[0]

        assert plugin.dependency_default_type == PluginType.RECOMMENDED
        assert plugin.type_descriptor == PluginType.OPTIONAL

    def test_parsea_patterns_con_sus_tipos(self) -> None:
        config = _configs(FOMOD_PATTERNS)
        plugin = config.install_steps[1].groups[0].plugins[0]

        assert len(plugin.dependency_patterns) == 2
        assert plugin.dependency_patterns[0].type == PluginType.NOT_USABLE
        assert plugin.dependency_patterns[0].conditions.file_deps[0].file == "OldMod.esp"
        assert plugin.dependency_patterns[1].type == PluginType.COULD_BE_USABLE
        assert plugin.dependency_patterns[1].conditions.flag_deps[0].flag == "modo"


class TestResolverDependencyTypeExtendido:
    def test_type_estatico_not_usable_deja_el_plugin_no_elegible(self) -> None:
        """Un <type name="NotUsable"/> estático (sin dependencyType) es una
        opción deshabilitada por el autor: no puede instalarse."""
        xml = """\
        <config>
            <installSteps order="Explicit">
                <installStep name="Compat">
                    <optionalFileGroups order="Explicit">
                        <group name="Parches" type="SelectAny">
                            <plugins order="Explicit">
                                <plugin name="Roto">
                                    <typeDescriptor><type name="NotUsable" /></typeDescriptor>
                                    <files><file source="roto/roto.esp" destination="roto.esp" /></files>
                                </plugin>
                            </plugins>
                        </group>
                    </optionalFileGroups>
                </installStep>
            </installSteps>
        </config>
        """
        resolver = FomodResolver(_configs(xml))

        result = resolver.resolve({"Compat": ["Roto"]})

        sources = [f.source for f in result.files]
        assert "roto/roto.esp" not in sources

    def test_type_estatico_could_be_usable_se_trata_como_optional(self) -> None:
        """Fuera de un pattern, CouldBeUsable se comporta como Optional:
        elegible pero nunca preseleccionado."""
        xml = """\
        <config>
            <installSteps order="Explicit">
                <installStep name="Compat">
                    <optionalFileGroups order="Explicit">
                        <group name="Parches" type="SelectAny">
                            <plugins order="Explicit">
                                <plugin name="Opcional">
                                    <typeDescriptor><type name="CouldBeUsable" /></typeDescriptor>
                                    <files><file source="opt/opt.esp" destination="opt.esp" /></files>
                                </plugin>
                            </plugins>
                        </group>
                    </optionalFileGroups>
                </installStep>
            </installSteps>
        </config>
        """
        resolver = FomodResolver(_configs(xml))

        sin_seleccion = resolver.resolve({})
        assert [f.source for f in sin_seleccion.files] == []

        con_seleccion = resolver.resolve({"Compat": ["Opcional"]})
        assert [f.source for f in con_seleccion.files] == ["opt/opt.esp"]

    def test_default_type_se_aplica_cuando_dependencias_no_se_cumplen(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+OtroMod\n",
            mods={"OtroMod": {"OtroMod.esp": "esp"}},
        )
        config = _configs(FOMOD_DEFAULT_TYPE)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        # USSEP ausente → deps no cumplidas → tipo efectivo = defaultType
        # (Recommended) → el plugin se preselecciona.
        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "light/light.esp" in sources

    def test_default_type_ignorado_cuando_dependencias_se_cumplen(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+USSEP\n",
            mods={"USSEP": {"USSEP.esp": "esp"}},
        )
        config = _configs(FOMOD_DEFAULT_TYPE)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        # USSEP presente → deps cumplidas → tipo = type normal (Optional):
        # el plugin NO se preselecciona.
        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "light/light.esp" not in sources

    def test_pattern_not_usable_bloquea_el_plugin(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+OldMod\n",
            mods={"OldMod": {"OldMod.esp": "esp"}},
        )
        config = _configs(FOMOD_PATTERNS)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        result = resolver.resolve({"Base": ["Avanzado"], "Compat": ["ParcheAvanzado"]})

        sources = [f.source for f in result.files]
        assert "compat/av.esp" not in sources

    def test_pattern_could_be_usable_habilita_el_plugin(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+OtroMod\n",
            mods={"OtroMod": {"OtroMod.esp": "esp"}},
        )
        config = _configs(FOMOD_PATTERNS)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        # Sin OldMod, con el flag "modo=avanzado" seteado por la selección.
        result = resolver.resolve({"Base": ["Avanzado"], "Compat": ["ParcheAvanzado"]})

        sources = [f.source for f in result.files]
        assert "compat/av.esp" in sources

    def test_pattern_sin_match_deja_el_plugin_no_usable(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+OtroMod\n",
            mods={"OtroMod": {"OtroMod.esp": "esp"}},
        )
        config = _configs(FOMOD_PATTERNS)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        # Sin OldMod y sin el flag: ningún pattern matchea → no elegible.
        result = resolver.resolve({"Base": ["Simple"], "Compat": ["ParcheAvanzado"]})

        sources = [f.source for f in result.files]
        assert "compat/av.esp" not in sources

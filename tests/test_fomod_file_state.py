"""Proveedor de estado de archivos (fileDependency) del resolver FOMOD.

Caso central de la auditoría FOMOD: un parche condicionado a ``USSEP.esp``
debe instalarse SOLO cuando el plugin está presente. Sin proveedor de estado,
el resolver trataba las fileDependency como siempre verdaderas ("optimistic")
— este archivo fija el comportamiento fail-closed y la evaluación real.
"""

from __future__ import annotations

import pathlib
import zipfile

from sky_claw.app.security.path_validator import PathValidator
from sky_claw.local.fomod.installer import FomodInstaller
from sky_claw.local.fomod.models import FileState
from sky_claw.local.fomod.parser import parse_fomod_string
from sky_claw.local.fomod.plugin_state import MO2PluginStateProvider
from sky_claw.local.fomod.resolver import FomodResolver
from tests.conftest import crear_arbol_mo2

FOMOD_PATCH_USSEP = """\
<config>
    <moduleName>ModConParche</moduleName>
    <requiredInstallFiles>
        <file source="core/main.esp" destination="main.esp" />
    </requiredInstallFiles>
    <conditionalFileInstalls>
        <patterns>
            <pattern>
                <dependencies>
                    <fileDependency file="USSEP.esp" state="Active" />
                </dependencies>
                <files>
                    <file source="patches/ussep_patch.esp" destination="ussep_patch.esp" />
                </files>
            </pattern>
        </patterns>
    </conditionalFileInstalls>
</config>
"""

FOMOD_PATCH_MISSING = """\
<config>
    <moduleName>ModAntiDuplicado</moduleName>
    <requiredInstallFiles>
        <file source="core/main.esp" destination="main.esp" />
    </requiredInstallFiles>
    <conditionalFileInstalls>
        <patterns>
            <pattern>
                <dependencies>
                    <fileDependency file="OldMod.esp" state="Missing" />
                </dependencies>
                <files>
                    <file source="patches/fix.esp" destination="fix.esp" />
                </files>
            </pattern>
        </patterns>
    </conditionalFileInstalls>
</config>
"""

FOMOD_FLAG_ONLY = """\
<config>
    <moduleName>ModPorFlags</moduleName>
    <installSteps order="Explicit">
        <installStep name="Version">
            <optionalFileGroups order="Explicit">
                <group name="Rama" type="SelectExactlyOne">
                    <plugins order="Explicit">
                        <plugin name="Standard">
                            <conditionFlags>
                                <!-- Valor como texto: forma canónica FOMOD. -->
                                <flag name="rama">standard</flag>
                            </conditionFlags>
                            <files>
                                <file source="std/plugin.esp" destination="plugin.esp" />
                            </files>
                        </plugin>
                    </plugins>
                </group>
            </optionalFileGroups>
        </installStep>
    </installSteps>
    <conditionalFileInstalls>
        <patterns>
            <pattern>
                <dependencies>
                    <flagDependency flag="rama" value="standard" />
                </dependencies>
                <files>
                    <file source="extras/readme.txt" destination="readme.txt" />
                </files>
            </pattern>
        </patterns>
    </conditionalFileInstalls>
</config>
"""


# ---------------------------------------------------------------------------
# MO2PluginStateProvider
# ---------------------------------------------------------------------------


class TestMO2PluginStateProvider:
    def test_archivo_en_mod_habilitado_es_active(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+ModA\n",
            mods={"ModA": {"ModA.esp": "esp"}},
        )
        provider = MO2PluginStateProvider(mo2_root)

        assert provider.file_state("ModA.esp") == FileState.ACTIVE

    def test_archivo_en_mod_deshabilitado_es_inactive(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="-ModA\n",
            mods={"ModA": {"ModA.esp": "esp"}},
        )
        provider = MO2PluginStateProvider(mo2_root)

        assert provider.file_state("ModA.esp") == FileState.INACTIVE

    def test_archivo_ausente_es_missing(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+ModA\n",
            mods={"ModA": {"ModA.esp": "esp"}},
        )
        provider = MO2PluginStateProvider(mo2_root)

        assert provider.file_state("USSEP.esp") == FileState.MISSING

    def test_comparacion_case_insensitive(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+ModA\n",
            mods={"ModA": {"ModA.esp": "esp"}},
        )
        provider = MO2PluginStateProvider(mo2_root)

        assert provider.file_state("moda.ESP") == FileState.ACTIVE

    def test_path_con_prefijo_data(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+ModA\n",
            mods={"ModA": {"ModA.esp": "esp"}},
        )
        provider = MO2PluginStateProvider(mo2_root)

        assert provider.file_state("Data/ModA.esp") == FileState.ACTIVE

    def test_separadores_y_espacios_en_modlist(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+Mod Con Espacios\n==\n-ModB\n",
            mods={
                "Mod Con Espacios": {"ModA.esp": "esp"},
                "ModB": {"ModB.esp": "esp"},
            },
        )
        provider = MO2PluginStateProvider(mo2_root)

        assert provider.file_state("ModA.esp") == FileState.ACTIVE
        assert provider.file_state("ModB.esp") == FileState.INACTIVE

    def test_invalida_el_indice_cuando_cambia_el_modlist(self, tmp_path: pathlib.Path) -> None:
        """Activar/desactivar un mod durante la sesión no puede quedar cacheado."""
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="-ModA\n",
            mods={"ModA": {"ModA.esp": "esp"}},
        )
        provider = MO2PluginStateProvider(mo2_root)
        assert provider.file_state("ModA.esp") == FileState.INACTIVE

        (mo2_root / "profiles" / "Default" / "modlist.txt").write_text("+ModA\n", encoding="utf-8")

        assert provider.file_state("ModA.esp") == FileState.ACTIVE

    def test_invalida_el_indice_cuando_aparece_un_mod_nuevo(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+ModA\n",
            mods={"ModA": {"ModA.esp": "esp"}},
        )
        provider = MO2PluginStateProvider(mo2_root)
        assert provider.file_state("Nuevo.esp") == FileState.MISSING

        nuevo = mo2_root / "mods" / "ModNuevo"
        nuevo.mkdir()
        (nuevo / "Nuevo.esp").write_text("esp", encoding="utf-8")
        (mo2_root / "profiles" / "Default" / "modlist.txt").write_text("+ModA\n+ModNuevo\n", encoding="utf-8")

        assert provider.file_state("Nuevo.esp") == FileState.ACTIVE

    def test_modlist_no_utf8_degrada_a_missing(self, tmp_path: pathlib.Path) -> None:
        """Un modlist.txt corrupto no puede tumbar la resolución: estado vacío."""
        mo2_root = tmp_path / "mo2"
        profile_dir = mo2_root / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "modlist.txt").write_bytes(b"\xff\xfe+ModA\xff")
        mod_dir = mo2_root / "mods" / "ModA"
        mod_dir.mkdir(parents=True)
        (mod_dir / "ModA.esp").write_text("esp", encoding="utf-8")
        provider = MO2PluginStateProvider(mo2_root)

        assert provider.file_state("ModA.esp") == FileState.MISSING


# ---------------------------------------------------------------------------
# Resolver: fileDependency evaluadas contra el proveedor (fail-closed)
# ---------------------------------------------------------------------------


class TestResolverFileDependency:
    def test_parche_condicionado_a_plugin_presente_se_instala(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+USSEP\n",
            mods={"USSEP": {"USSEP.esp": "esp"}},
        )
        config = parse_fomod_string(FOMOD_PATCH_USSEP)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "core/main.esp" in sources
        assert "patches/ussep_patch.esp" in sources

    def test_parche_condicionado_a_plugin_ausente_no_se_instala(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+OtroMod\n",
            mods={"OtroMod": {"OtroMod.esp": "esp"}},
        )
        config = parse_fomod_string(FOMOD_PATCH_USSEP)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "core/main.esp" in sources
        assert "patches/ussep_patch.esp" not in sources

    def test_sin_provider_fail_closed_no_instala_parche(self) -> None:
        """Sin proveedor de estado no hay evidencia de que la condición se
        cumpla: la fileDependency se evalúa falsa (nunca instalar a ciegas)."""
        config = parse_fomod_string(FOMOD_PATCH_USSEP)
        resolver = FomodResolver(config)

        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "patches/ussep_patch.esp" not in sources

    def test_condicion_de_flag_sin_provider_sigue_funcionando(self) -> None:
        """Las flagDependency no dependen del filesystem: sin provider siguen
        evaluándose contra los flags internos del instalador."""
        config = parse_fomod_string(FOMOD_FLAG_ONLY)
        resolver = FomodResolver(config)

        result = resolver.resolve({"Version": ["Standard"]})

        sources = [f.source for f in result.files]
        assert "extras/readme.txt" in sources

    def test_estado_missing_se_cumple_cuando_archivo_no_existe(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+ModActual\n",
            mods={"ModActual": {"main.esp": "esp"}},
        )
        config = parse_fomod_string(FOMOD_PATCH_MISSING)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "patches/fix.esp" in sources

    def test_estado_missing_no_se_cumple_cuando_archivo_existe(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+OldMod\n",
            mods={"OldMod": {"OldMod.esp": "esp"}},
        )
        config = parse_fomod_string(FOMOD_PATCH_MISSING)
        resolver = FomodResolver(config, file_state=MO2PluginStateProvider(mo2_root))

        result = resolver.resolve({})

        sources = [f.source for f in result.files]
        assert "patches/fix.esp" not in sources


# ---------------------------------------------------------------------------
# Integración: FomodInstaller usa el proveedor durante la instalación
# ---------------------------------------------------------------------------


def _zip_con_fomod(sandbox: pathlib.Path, fomod_xml: str) -> pathlib.Path:
    archive = sandbox / "ModConParche.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("fomod/ModuleConfig.xml", fomod_xml)
        zf.writestr("core/main.esp", "esp")
        zf.writestr("patches/ussep_patch.esp", "patch")
    return archive


class TestInstallerFileDependency:
    async def test_install_fomod_respeta_estado_de_plugins(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+USSEP\n",
            mods={"USSEP": {"USSEP.esp": "esp"}},
        )
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        mo2_mods_dir = sandbox / "mods"
        mo2_mods_dir.mkdir()
        archive = _zip_con_fomod(sandbox, FOMOD_PATCH_USSEP)

        installer = FomodInstaller(
            path_validator=PathValidator(roots=[sandbox]),
            file_state_provider=MO2PluginStateProvider(mo2_root),
        )
        result = await installer.install(archive, mo2_mods_dir)

        assert result.installed is True
        copied = [f for f in result.files_copied]
        assert any(f.endswith("main.esp") for f in copied)
        assert any(f.endswith("ussep_patch.esp") for f in copied)

    async def test_install_fomod_omite_parche_sin_plugin(self, tmp_path: pathlib.Path) -> None:
        mo2_root = crear_arbol_mo2(
            tmp_path,
            modlist="+OtroMod\n",
            mods={"OtroMod": {"OtroMod.esp": "esp"}},
        )
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        mo2_mods_dir = sandbox / "mods"
        mo2_mods_dir.mkdir()
        archive = _zip_con_fomod(sandbox, FOMOD_PATCH_USSEP)

        installer = FomodInstaller(
            path_validator=PathValidator(roots=[sandbox]),
            file_state_provider=MO2PluginStateProvider(mo2_root),
        )
        result = await installer.install(archive, mo2_mods_dir)

        assert result.installed is True
        copied = [f for f in result.files_copied]
        assert any(f.endswith("main.esp") for f in copied)
        assert not any(f.endswith("ussep_patch.esp") for f in copied)

"""Tests for sky_claw.fomod (parser, models, resolver)."""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from sky_claw.fomod.models import (
    FomodConfig,
    GroupType,
)
from sky_claw.core.errors import FomodParserSecurityError
from sky_claw.fomod.parser import parse_fomod, parse_fomod_string, FomodParseError
from sky_claw.fomod.resolver import FomodResolver

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "fomod"


# ------------------------------------------------------------------
# Parser — simple fixture
# ------------------------------------------------------------------


class TestParserSimple:
    @pytest.fixture()
    def config(self) -> FomodConfig:
        return parse_fomod(FIXTURES / "simple.xml")

    def test_module_name(self, config: FomodConfig) -> None:
        assert config.module_name == "Simple Test Mod"

    def test_required_files(self, config: FomodConfig) -> None:
        assert len(config.required_files) == 1
        assert config.required_files[0].source == "core/main.esp"

    def test_install_steps(self, config: FomodConfig) -> None:
        assert len(config.install_steps) == 1
        step = config.install_steps[0]
        assert step.name == "Main Options"

    def test_groups(self, config: FomodConfig) -> None:
        step = config.install_steps[0]
        assert len(step.groups) == 1
        group = step.groups[0]
        assert group.name == "Textures"
        assert group.group_type == GroupType.SELECT_EXACTLY_ONE

    def test_plugins(self, config: FomodConfig) -> None:
        group = config.install_steps[0].groups[0]
        assert len(group.plugins) == 3
        names = [p.name for p in group.plugins]
        assert names == ["1K Textures", "2K Textures", "4K Textures"]

    def test_plugin_files(self, config: FomodConfig) -> None:
        plugin = config.install_steps[0].groups[0].plugins[0]
        assert len(plugin.files) == 1
        assert plugin.files[0].source == "textures/1k"
        assert plugin.files[0].destination == "textures"

    def test_path_normalization(self, config: FomodConfig) -> None:
        """Backslashes in source paths should be normalized to forward slashes."""
        for fi in config.required_files:
            assert "\\" not in fi.source


# ------------------------------------------------------------------
# Parser — conditional fixture
# ------------------------------------------------------------------


class TestParserConditional:
    @pytest.fixture()
    def config(self) -> FomodConfig:
        return parse_fomod(FIXTURES / "conditional.xml")

    def test_module_name(self, config: FomodConfig) -> None:
        assert config.module_name == "Conditional Mod"

    def test_two_steps(self, config: FomodConfig) -> None:
        assert len(config.install_steps) == 2

    def test_visibility_conditions(self, config: FomodConfig) -> None:
        patches_step = config.install_steps[1]
        assert patches_step.visibility is not None
        assert len(patches_step.visibility.flag_deps) == 1
        assert patches_step.visibility.flag_deps[0].flag == "version"
        assert patches_step.visibility.flag_deps[0].value == "standard"

    def test_condition_flags_on_plugins(self, config: FomodConfig) -> None:
        step = config.install_steps[0]
        standard = step.groups[0].plugins[0]
        assert len(standard.condition_flags) == 1
        assert standard.condition_flags[0].name == "version"
        assert standard.condition_flags[0].value == "standard"

    def test_conditional_installs(self, config: FomodConfig) -> None:
        assert len(config.conditional_installs) == 2
        pattern = config.conditional_installs[0]
        assert len(pattern.conditions.flag_deps) == 1
        assert pattern.files[0].source == "extras/standard_readme.txt"


# ------------------------------------------------------------------
# Parser — complex fixture
# ------------------------------------------------------------------


class TestParserComplex:
    @pytest.fixture()
    def config(self) -> FomodConfig:
        return parse_fomod(FIXTURES / "complex.xml")

    def test_three_steps(self, config: FomodConfig) -> None:
        assert len(config.install_steps) == 3

    def test_required_files_with_folder(self, config: FomodConfig) -> None:
        assert len(config.required_files) == 2
        sources = {f.source for f in config.required_files}
        assert "core/base.esm" in sources
        assert "core/meshes" in sources

    def test_select_all_group(self, config: FomodConfig) -> None:
        sse_patches = config.install_steps[2]
        assert sse_patches.groups[0].group_type == GroupType.SELECT_ALL

    def test_image_path(self, config: FomodConfig) -> None:
        tex_step = config.install_steps[1]
        plugin_1k = tex_step.groups[0].plugins[0]
        assert plugin_1k.image == "images/1k_preview.png"

    def test_priority_values(self, config: FomodConfig) -> None:
        tex_step = config.install_steps[1]
        plugin_2k = tex_step.groups[0].plugins[1]
        assert plugin_2k.files[0].priority == 10


# ------------------------------------------------------------------
# Parser — error handling
# ------------------------------------------------------------------


class TestParserErrors:
    def test_malformed_xml_raises(self, tmp_path: pathlib.Path) -> None:
        bad_xml = tmp_path / "bad.xml"
        bad_xml.write_text("<config><unclosed>", encoding="utf-8")
        with pytest.raises(FomodParseError):
            parse_fomod(bad_xml)

    def test_empty_config(self) -> None:
        config = parse_fomod_string("<config/>")
        assert config.module_name == ""
        assert config.install_steps == []
        assert config.required_files == []

    def test_parse_string(self) -> None:
        xml = textwrap.dedent("""\
            <config>
                <moduleName>From String</moduleName>
            </config>
        """)
        config = parse_fomod_string(xml)
        assert config.module_name == "From String"

    def test_entity_expansion_blocked(self) -> None:
        """defusedxml should reject XML with entity declarations (billion-laughs)."""
        bomb = textwrap.dedent("""\
            <?xml version="1.0"?>
            <!DOCTYPE bomb [
              <!ENTITY a "aaaaaaaaaaaaaaaaaa">
            ]>
            <config>
                <moduleName>&a;</moduleName>
            </config>
        """)
        with pytest.raises(FomodParserSecurityError):
            parse_fomod_string(bomb)

    def test_entity_expansion_blocked_file(self, tmp_path: pathlib.Path) -> None:
        """defusedxml should reject XML files with entity declarations."""
        bomb_file = tmp_path / "bomb.xml"
        bomb_file.write_text(
            '<?xml version="1.0"?>'
            "<!DOCTYPE bomb ["
            '  <!ENTITY a "aaaa">'
            "]>"
            "<config><moduleName>&a;</moduleName></config>",
            encoding="utf-8",
        )
        with pytest.raises(FomodParserSecurityError):
            parse_fomod(bomb_file)


# ------------------------------------------------------------------
# Resolver — simple
# ------------------------------------------------------------------


class TestResolverSimple:
    @pytest.fixture()
    def config(self) -> FomodConfig:
        return parse_fomod(FIXTURES / "simple.xml")

    def test_resolve_with_selection(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve({"Main Options": ["2K Textures"]})

        sources = [f.source for f in result.files]
        assert "core/main.esp" in sources  # required
        assert "textures/2k" in sources  # selected
        assert "textures/1k" not in sources
        assert "textures/4k" not in sources

    def test_resolve_no_selection_gives_pending(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve({})

        assert len(result.pending_decisions) == 1
        assert "exactly one" in result.pending_decisions[0]

    def test_required_files_always_included(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve({})
        sources = [f.source for f in result.files]
        assert "core/main.esp" in sources


# ------------------------------------------------------------------
# Resolver — conditional
# ------------------------------------------------------------------


class TestResolverConditional:
    @pytest.fixture()
    def config(self) -> FomodConfig:
        return parse_fomod(FIXTURES / "conditional.xml")

    def test_standard_version_shows_patches(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve(
            {
                "Choose Version": ["Standard"],
                "Patches": ["USSEP Patch"],
            }
        )

        sources = [f.source for f in result.files]
        assert "standard/plugin.esp" in sources
        assert "patches/ussep.esp" in sources
        # Conditional install for standard
        assert "extras/standard_readme.txt" in sources
        assert "extras/lite_readme.txt" not in sources

    def test_lite_version_hides_patches(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve(
            {
                "Choose Version": ["Lite"],
            }
        )

        sources = [f.source for f in result.files]
        assert "lite/plugin.esp" in sources
        # Patches step should be invisible (version != standard)
        assert "patches/ussep.esp" not in sources
        # Conditional install for lite
        assert "extras/lite_readme.txt" in sources

    def test_flags_tracked(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve({"Choose Version": ["Standard"]})
        assert result.flags.get("version") == "standard"


# ------------------------------------------------------------------
# Resolver — complex
# ------------------------------------------------------------------


class TestResolverComplex:
    @pytest.fixture()
    def config(self) -> FomodConfig:
        return parse_fomod(FIXTURES / "complex.xml")

    def test_sse_full_selection(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve(
            {
                "Game Version": ["SSE"],
                "Texture Resolution": ["2K"],
                "SSE Patches": ["ENB Compatibility"],
            }
        )

        sources = [f.source for f in result.files]
        # Required
        assert "core/base.esm" in sources
        assert "core/meshes" in sources
        # Game version
        assert "sse/plugin.esp" in sources
        # Textures
        assert "textures/2k" in sources
        # SelectAll group → DLC Patch always included
        assert "patches/dlc_patch.esp" in sources
        # Optional patch selected
        assert "patches/enb" in sources
        # AE conditional should NOT be included
        assert "ae/ae_patch.esp" not in sources

    def test_ae_skips_sse_patches(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve(
            {
                "Game Version": ["AE"],
                "Texture Resolution": ["1K"],
            }
        )

        sources = [f.source for f in result.files]
        assert "ae/plugin.esp" in sources
        assert "textures/1k" in sources
        # SSE Patches step invisible (game != sse)
        assert "patches/dlc_patch.esp" not in sources
        # AE conditional SHOULD be included
        assert "ae/ae_patch.esp" in sources

    def test_files_sorted_by_priority(self, config: FomodConfig) -> None:
        resolver = FomodResolver(config)
        result = resolver.resolve(
            {
                "Game Version": ["SSE"],
                "Texture Resolution": ["2K"],
            }
        )

        priorities = [f.priority for f in result.files]
        assert priorities == sorted(priorities)

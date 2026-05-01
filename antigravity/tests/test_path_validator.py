"""Comprehensive tests for sky_claw.antigravity.security.path_validator.

Covers:
- PathValidator construction: valid roots, empty roots raises ValueError.
- validate: path inside sandbox → returned as resolved Path; path outside all
  roots → PathViolationError; path with '..' component → PathViolationError (pre-resolve
  guard); deeply nested path still inside root → allowed.
- Symlinks: symlink pointing inside sandbox with strict_symlink=True → allowed;
  symlink pointing outside sandbox with strict_symlink=True → PathViolationError;
  symlink pointing outside sandbox with strict_symlink=False → allowed (resolved
  normally if target is inside root, else PathViolationError on final check).
- Multiple sandbox roots: file in any root is accepted.
- sandboxed_io decorator: valid positional path → function executes; invalid
  path → PathViolationError; custom arg_name kwarg path → function executes.

All tests use the `tmp_path` pytest fixture for real filesystem operations.
No mocks are used where real filesystem calls are appropriate.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from sky_claw.antigravity.security.path_validator import (
    PathValidator,
    PathViolationError,
    assert_safe_component,
    safe_join,
    sandboxed_io,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_validator(*roots: pathlib.Path) -> PathValidator:
    return PathValidator(roots=list(roots))


# ---------------------------------------------------------------------------
# TestPathValidatorConstruction
# ---------------------------------------------------------------------------


class TestPathValidatorConstruction:
    def test_single_root_stored(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        assert tmp_path.resolve() in v.roots

    def test_multiple_roots_stored(self, tmp_path: pathlib.Path) -> None:
        r1 = tmp_path / "a"
        r2 = tmp_path / "b"
        r1.mkdir()
        r2.mkdir()
        v = _make_validator(r1, r2)
        assert r1.resolve() in v.roots
        assert r2.resolve() in v.roots

    def test_empty_roots_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="At least one"):
            PathValidator(roots=[])

    def test_roots_resolved_to_absolute(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        for root in v.roots:
            assert root.is_absolute()

    def test_non_existent_root_is_accepted_at_construction(self, tmp_path: pathlib.Path) -> None:
        # The validator does not require roots to exist at construction time;
        # it only resolves them.  (resolve() on a non-existent path still works
        # on Python 3.6+, returning a cleaned absolute path.)
        phantom = tmp_path / "does_not_exist_yet"
        v = _make_validator(phantom)
        assert phantom.resolve() in v.roots


# ---------------------------------------------------------------------------
# TestValidateInsideSandbox
# ---------------------------------------------------------------------------


class TestValidateInsideSandbox:
    def test_file_directly_in_root_allowed(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "readme.txt"
        f.touch()
        v = _make_validator(tmp_path)
        result = v.validate(f)
        assert result == f.resolve()

    def test_subdirectory_file_allowed(self, tmp_path: pathlib.Path) -> None:
        sub = tmp_path / "mods" / "skse" / "plugins"
        sub.mkdir(parents=True)
        target = sub / "plugin.dll"
        target.touch()
        v = _make_validator(tmp_path)
        result = v.validate(target)
        assert result == target.resolve()

    def test_returns_resolved_path_object(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "file.bin"
        f.touch()
        v = _make_validator(tmp_path)
        result = v.validate(f)
        assert isinstance(result, pathlib.Path)
        assert result.is_absolute()

    def test_string_path_accepted(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "data.json"
        f.touch()
        v = _make_validator(tmp_path)
        result = v.validate(str(f))
        assert result == f.resolve()

    def test_root_itself_allowed(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        result = v.validate(tmp_path)
        assert result == tmp_path.resolve()

    def test_deep_nesting_allowed(self, tmp_path: pathlib.Path) -> None:
        deep = tmp_path
        for i in range(10):
            deep = deep / f"level_{i}"
        deep.mkdir(parents=True)
        leaf = deep / "leaf.esp"
        leaf.touch()
        v = _make_validator(tmp_path)
        assert v.validate(leaf) == leaf.resolve()


# ---------------------------------------------------------------------------
# TestValidateOutsideSandbox
# ---------------------------------------------------------------------------


class TestValidateOutsideSandbox:
    def test_absolute_path_outside_root_raises(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        # /tmp or C:\Windows always exists but is outside tmp_path.
        outside: pathlib.Path
        if sys.platform == "win32":
            outside = pathlib.Path("C:/Windows/System32/cmd.exe")
        else:
            outside = pathlib.Path("/etc/passwd")
        with pytest.raises(PathViolationError, match="outside all sandbox roots"):
            v.validate(outside)

    def test_sibling_directory_outside_root_raises(self, tmp_path: pathlib.Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        v = _make_validator(sandbox)
        sneaky = sibling / "evil.esp"
        sneaky.touch()
        with pytest.raises(PathViolationError, match="outside all sandbox roots"):
            v.validate(sneaky)

    def test_parent_of_root_is_outside(self, tmp_path: pathlib.Path) -> None:
        sandbox = tmp_path / "child"
        sandbox.mkdir()
        v = _make_validator(sandbox)
        # tmp_path itself is the parent of the sandbox — not allowed.
        f = tmp_path / "outside.txt"
        f.touch()
        with pytest.raises(PathViolationError):
            v.validate(f)

    def test_violation_message_contains_path(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        outside: pathlib.Path
        if sys.platform == "win32":
            outside = pathlib.Path("C:/Windows/notepad.exe")
        else:
            outside = pathlib.Path("/usr/bin/env")
        with pytest.raises(PathViolationError) as exc_info:
            v.validate(outside)
        assert "outside all sandbox roots" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TestValidatePathTraversal
# ---------------------------------------------------------------------------


class TestValidatePathTraversal:
    def test_double_dot_component_raises(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        # The validator catches '..' in path parts before resolving.
        traversal = tmp_path / "sub" / ".." / ".." / "etc" / "passwd"
        with pytest.raises(PathViolationError, match="traversal"):
            v.validate(traversal)

    def test_double_dot_at_start_raises(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        traversal = pathlib.Path("..") / "secret.txt"
        with pytest.raises(PathViolationError, match="traversal"):
            v.validate(traversal)

    def test_double_dot_in_middle_raises(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        traversal = tmp_path / "mods" / ".." / ".." / "Windows" / "system32"
        with pytest.raises(PathViolationError, match="traversal"):
            v.validate(traversal)

    @pytest.mark.skipif(sys.platform == "win32", reason="'...' is not a valid directory name on Windows")
    def test_triple_dot_not_traversal(self, tmp_path: pathlib.Path) -> None:
        # "..." is a valid filename component on POSIX, not a traversal marker.
        d = tmp_path / "..."
        d.mkdir()
        v = _make_validator(tmp_path)
        result = v.validate(d)
        assert result == d.resolve()

    def test_plain_dotfile_allowed(self, tmp_path: pathlib.Path) -> None:
        # ".hidden" contains a dot but not "..".
        hidden = tmp_path / ".hidden_config"
        hidden.touch()
        v = _make_validator(tmp_path)
        result = v.validate(hidden)
        assert result == hidden.resolve()


# ---------------------------------------------------------------------------
# TestValidateSymlinks
# ---------------------------------------------------------------------------


def _is_symlink_creatable() -> bool:
    """Return True if the current process can create symlinks."""
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "src.txt"
            src.touch()
            dst = pathlib.Path(td) / "link.txt"
            dst.symlink_to(src)
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.mark.skipif(
    sys.platform == "win32" and not _is_symlink_creatable(),
    reason="Symlink creation requires elevated privileges on Windows",
)
class TestValidateSymlinks:
    def test_symlink_pointing_inside_sandbox_strict_allowed(self, tmp_path: pathlib.Path) -> None:
        target = tmp_path / "real_file.txt"
        target.touch()
        link = tmp_path / "link_to_real.txt"
        link.symlink_to(target)

        v = _make_validator(tmp_path)
        result = v.validate(link, strict_symlink=True)
        assert result == link.resolve()

    def test_symlink_pointing_outside_sandbox_strict_raises(self, tmp_path: pathlib.Path) -> None:
        # Create a second tmp dir to act as the "outside".
        outside_dir = tmp_path.parent / f"_outside_{tmp_path.name}"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret")

        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        link = sandbox / "evil_link.txt"
        link.symlink_to(outside_file)

        v = _make_validator(sandbox)
        with pytest.raises(PathViolationError, match="Symlink strictly escapes sandbox"):
            v.validate(link, strict_symlink=True)

        # Clean up manually.
        outside_file.unlink(missing_ok=True)
        outside_dir.rmdir()

    def test_symlink_outside_sandbox_non_strict_allowed_if_resolves_inside(self, tmp_path: pathlib.Path) -> None:
        # With strict_symlink=False, the symlink check is skipped; only the
        # final resolved path is checked against the sandbox.
        real = tmp_path / "real.txt"
        real.touch()
        link = tmp_path / "link.txt"
        link.symlink_to(real)

        v = _make_validator(tmp_path)
        # Even with strict_symlink=False the resolved target is inside sandbox.
        result = v.validate(link, strict_symlink=False)
        assert result == link.resolve()

    def test_symlink_outside_sandbox_non_strict_blocked_if_resolves_outside(self, tmp_path: pathlib.Path) -> None:
        # With strict_symlink=False the symlink check is skipped, but the
        # final resolve() check still catches an escape.
        outside_dir = tmp_path.parent / f"_outside2_{tmp_path.name}"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "data.txt"
        outside_file.write_text("data")

        sandbox = tmp_path / "sandbox2"
        sandbox.mkdir()
        link = sandbox / "link2.txt"
        link.symlink_to(outside_file)

        v = _make_validator(sandbox)
        # strict_symlink=False: the pre-resolve symlink check is skipped, but
        # resolve() will give the outside path, which fails the root check.
        with pytest.raises(PathViolationError, match="outside all sandbox roots"):
            v.validate(link, strict_symlink=False)

        # Clean up.
        outside_file.unlink(missing_ok=True)
        outside_dir.rmdir()

    def test_broken_symlink_strict_raises_path_violation(self, tmp_path: pathlib.Path) -> None:
        link = tmp_path / "broken_link.txt"
        link.symlink_to(tmp_path / "nonexistent_target.txt")

        v = _make_validator(tmp_path)
        with pytest.raises(PathViolationError):
            v.validate(link, strict_symlink=True)


# ---------------------------------------------------------------------------
# TestMultipleSandboxRoots
# ---------------------------------------------------------------------------


class TestMultipleSandboxRoots:
    def test_file_in_first_root_allowed(self, tmp_path: pathlib.Path) -> None:
        r1 = tmp_path / "root1"
        r2 = tmp_path / "root2"
        r1.mkdir()
        r2.mkdir()
        f = r1 / "file.txt"
        f.touch()
        v = _make_validator(r1, r2)
        assert v.validate(f) == f.resolve()

    def test_file_in_second_root_allowed(self, tmp_path: pathlib.Path) -> None:
        r1 = tmp_path / "root1"
        r2 = tmp_path / "root2"
        r1.mkdir()
        r2.mkdir()
        f = r2 / "data.bin"
        f.touch()
        v = _make_validator(r1, r2)
        assert v.validate(f) == f.resolve()

    def test_file_outside_all_roots_raises(self, tmp_path: pathlib.Path) -> None:
        r1 = tmp_path / "root1"
        r2 = tmp_path / "root2"
        outside = tmp_path / "outside"
        r1.mkdir()
        r2.mkdir()
        outside.mkdir()
        f = outside / "forbidden.txt"
        f.touch()
        v = _make_validator(r1, r2)
        with pytest.raises(PathViolationError, match="outside all sandbox roots"):
            v.validate(f)

    def test_three_roots_file_in_third(self, tmp_path: pathlib.Path) -> None:
        roots = [tmp_path / f"r{i}" for i in range(3)]
        for r in roots:
            r.mkdir()
        f = roots[2] / "deep" / "nested.esp"
        f.parent.mkdir(parents=True)
        f.touch()
        v = PathValidator(roots=roots)
        assert v.validate(f) == f.resolve()

    def test_roots_property_returns_tuple_of_resolved_paths(self, tmp_path: pathlib.Path) -> None:
        r1 = tmp_path / "a"
        r2 = tmp_path / "b"
        r1.mkdir()
        r2.mkdir()
        v = _make_validator(r1, r2)
        assert isinstance(v.roots, tuple)
        assert all(isinstance(r, pathlib.Path) for r in v.roots)
        assert r1.resolve() in v.roots
        assert r2.resolve() in v.roots


# ---------------------------------------------------------------------------
# TestSandboxedIODecorator
# ---------------------------------------------------------------------------


class TestSandboxedIODecorator:
    def test_valid_positional_path_executes_function(self, tmp_path: pathlib.Path) -> None:
        @sandboxed_io(roots=[tmp_path])
        def read_file(path: pathlib.Path) -> str:
            return "content"

        valid = tmp_path / "valid.txt"
        valid.touch()
        assert read_file(valid) == "content"

    def test_invalid_positional_path_raises_violation(self, tmp_path: pathlib.Path) -> None:
        @sandboxed_io(roots=[tmp_path])
        def read_file(path: pathlib.Path) -> str:
            return "content"

        if sys.platform == "win32":
            outside = pathlib.Path("C:/Windows/System32/ntdll.dll")
        else:
            outside = pathlib.Path("/etc/shadow")
        with pytest.raises(PathViolationError):
            read_file(outside)

    def test_custom_arg_name_kwarg(self, tmp_path: pathlib.Path) -> None:
        @sandboxed_io(roots=[tmp_path], arg_name="target")
        def write_file(target: pathlib.Path) -> str:
            return "written"

        valid = tmp_path / "dest.dat"
        valid.touch()
        assert write_file(target=valid) == "written"

    def test_custom_arg_name_invalid_path_raises(self, tmp_path: pathlib.Path) -> None:
        @sandboxed_io(roots=[tmp_path], arg_name="target")
        def write_file(target: pathlib.Path) -> str:
            return "written"

        if sys.platform == "win32":
            outside = pathlib.Path("C:/Windows/win.ini")
        else:
            outside = pathlib.Path("/etc/crontab")
        with pytest.raises(PathViolationError):
            write_file(target=outside)

    def test_return_value_preserved(self, tmp_path: pathlib.Path) -> None:
        @sandboxed_io(roots=[tmp_path])
        def identity(path: pathlib.Path) -> pathlib.Path:
            return path

        f = tmp_path / "echo.txt"
        f.touch()
        assert identity(f) == f

    def test_function_name_preserved_via_wraps(self, tmp_path: pathlib.Path) -> None:
        @sandboxed_io(roots=[tmp_path])
        def my_special_function(path: pathlib.Path) -> None:
            pass

        assert my_special_function.__name__ == "my_special_function"

    def test_traversal_in_decorated_call_raises(self, tmp_path: pathlib.Path) -> None:
        @sandboxed_io(roots=[tmp_path])
        def process(path: pathlib.Path) -> None:
            pass

        traversal = tmp_path / ".." / "etc" / "passwd"
        with pytest.raises(PathViolationError, match="traversal"):
            process(traversal)

    def test_multiple_calls_only_checks_path_arg(self, tmp_path: pathlib.Path) -> None:
        """The decorator must not inspect non-path arguments."""
        call_log: list[tuple] = []

        @sandboxed_io(roots=[tmp_path])
        def fn_with_extra(path: pathlib.Path, mode: str = "r") -> str:
            call_log.append((path, mode))
            return mode

        valid = tmp_path / "f.txt"
        valid.touch()
        result = fn_with_extra(valid, mode="rb")
        assert result == "rb"
        assert len(call_log) == 1


# ---------------------------------------------------------------------------
# TestSafeJoin
# ---------------------------------------------------------------------------


class TestSafeJoin:
    """safe_join must sandbox the resolved result inside root."""

    def test_normal_relative_path(self, tmp_path: pathlib.Path) -> None:
        result = safe_join(tmp_path, "mods/Requiem")
        assert result == (tmp_path / "mods" / "Requiem").resolve()

    def test_nested_path_accepted(self, tmp_path: pathlib.Path) -> None:
        result = safe_join(tmp_path, "a/b/c/d.esp")
        assert result.is_relative_to(tmp_path.resolve())

    def test_double_dot_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(PathViolationError, match="escapes root"):
            safe_join(tmp_path, "../etc/passwd")

    def test_deep_double_dot_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(PathViolationError, match="escapes root"):
            safe_join(tmp_path, "a/b/../../../../../../etc/passwd")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX absolute path test")
    def test_absolute_posix_path_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(PathViolationError, match="escapes root"):
            safe_join(tmp_path, "/etc/shadow")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows absolute path test")
    def test_absolute_windows_path_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(PathViolationError, match="escapes root"):
            safe_join(tmp_path, "C:\\Windows\\System32\\cmd.exe")

    def test_string_root_accepted(self, tmp_path: pathlib.Path) -> None:
        result = safe_join(str(tmp_path), "sub/file.txt")
        assert result.is_relative_to(tmp_path.resolve())

    def test_path_untrusted_accepted(self, tmp_path: pathlib.Path) -> None:
        result = safe_join(tmp_path, pathlib.Path("sub/file.txt"))
        assert result.is_relative_to(tmp_path.resolve())

    def test_empty_string_yields_root(self, tmp_path: pathlib.Path) -> None:
        result = safe_join(tmp_path, "")
        assert result == tmp_path.resolve()

    def test_dot_segment_yields_root(self, tmp_path: pathlib.Path) -> None:
        result = safe_join(tmp_path, ".")
        assert result == tmp_path.resolve()

    def test_mixed_traversal_and_valid_rejected(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(PathViolationError):
            safe_join(tmp_path, "valid/../../../etc")


# ---------------------------------------------------------------------------
# TestAssertSafeComponent
# ---------------------------------------------------------------------------


class TestAssertSafeComponent:
    """assert_safe_component must reject hostile path components."""

    def test_normal_mod_name_accepted(self) -> None:
        assert assert_safe_component("Requiem") == "Requiem"

    def test_name_with_dots_and_hyphens_accepted(self) -> None:
        assert assert_safe_component("SkyUI_5.2-SE") == "SkyUI_5.2-SE"

    def test_unicode_name_accepted(self) -> None:
        assert assert_safe_component("MódNàme") == "MódNàme"

    def test_returns_original_unchanged(self) -> None:
        name = "Requiem"
        assert assert_safe_component(name) is name

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="must not be empty"):
            assert_safe_component("")

    def test_none_rejected(self) -> None:
        with pytest.raises(PathViolationError):
            assert_safe_component(None)  # type: ignore[arg-type]

    def test_non_string_rejected(self) -> None:
        with pytest.raises(PathViolationError):
            assert_safe_component(123)  # type: ignore[arg-type]

    def test_single_dot_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="traversal"):
            assert_safe_component(".")

    def test_double_dot_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="traversal"):
            assert_safe_component("..")

    def test_forward_slash_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="separators"):
            assert_safe_component("mods/evil")

    def test_backslash_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="separators"):
            assert_safe_component("mods\\evil")

    def test_newline_injection_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="control character"):
            assert_safe_component("legit\nevil_entry")

    def test_carriage_return_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="control character"):
            assert_safe_component("mod\rname")

    def test_tab_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="control character"):
            assert_safe_component("mod\tname")

    def test_bell_char_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="control character"):
            assert_safe_component("mod\x07name")

    def test_nul_byte_rejected(self) -> None:
        with pytest.raises(PathViolationError, match="NUL"):
            assert_safe_component("mod\x00name")

    def test_field_name_appears_in_error(self) -> None:
        with pytest.raises(PathViolationError, match="mod_name"):
            assert_safe_component("..", field="mod_name")

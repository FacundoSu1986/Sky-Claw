"""Comprehensive tests for sky_claw.security.path_validator.

Covers:
- PathValidator construction: valid roots, empty roots raises ValueError.
- validate: path inside sandbox → returned as resolved Path; path outside all
  roots → PathViolation; path with '..' component → PathViolation (pre-resolve
  guard); deeply nested path still inside root → allowed.
- Symlinks: symlink pointing inside sandbox with strict_symlink=True → allowed;
  symlink pointing outside sandbox with strict_symlink=True → PathViolation;
  symlink pointing outside sandbox with strict_symlink=False → allowed (resolved
  normally if target is inside root, else PathViolation on final check).
- Multiple sandbox roots: file in any root is accepted.
- sandboxed_io decorator: valid positional path → function executes; invalid
  path → PathViolation; custom arg_name kwarg path → function executes.

All tests use the `tmp_path` pytest fixture for real filesystem operations.
No mocks are used where real filesystem calls are appropriate.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from sky_claw.security.path_validator import (
    PathValidator,
    PathViolation,
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

    def test_non_existent_root_is_accepted_at_construction(
        self, tmp_path: pathlib.Path
    ) -> None:
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
        with pytest.raises(PathViolation, match="outside all sandbox roots"):
            v.validate(outside)

    def test_sibling_directory_outside_root_raises(
        self, tmp_path: pathlib.Path
    ) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        v = _make_validator(sandbox)
        sneaky = sibling / "evil.esp"
        sneaky.touch()
        with pytest.raises(PathViolation, match="outside all sandbox roots"):
            v.validate(sneaky)

    def test_parent_of_root_is_outside(self, tmp_path: pathlib.Path) -> None:
        sandbox = tmp_path / "child"
        sandbox.mkdir()
        v = _make_validator(sandbox)
        # tmp_path itself is the parent of the sandbox — not allowed.
        f = tmp_path / "outside.txt"
        f.touch()
        with pytest.raises(PathViolation):
            v.validate(f)

    def test_violation_message_contains_path(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        outside: pathlib.Path
        if sys.platform == "win32":
            outside = pathlib.Path("C:/Windows/notepad.exe")
        else:
            outside = pathlib.Path("/usr/bin/env")
        with pytest.raises(PathViolation) as exc_info:
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
        with pytest.raises(PathViolation, match="traversal"):
            v.validate(traversal)

    def test_double_dot_at_start_raises(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        traversal = pathlib.Path("..") / "secret.txt"
        with pytest.raises(PathViolation, match="traversal"):
            v.validate(traversal)

    def test_double_dot_in_middle_raises(self, tmp_path: pathlib.Path) -> None:
        v = _make_validator(tmp_path)
        traversal = tmp_path / "mods" / ".." / ".." / "Windows" / "system32"
        with pytest.raises(PathViolation, match="traversal"):
            v.validate(traversal)

    @pytest.mark.skipif(
        sys.platform == "win32", reason="'...' is not a valid directory name on Windows"
    )
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
    def test_symlink_pointing_inside_sandbox_strict_allowed(
        self, tmp_path: pathlib.Path
    ) -> None:
        target = tmp_path / "real_file.txt"
        target.touch()
        link = tmp_path / "link_to_real.txt"
        link.symlink_to(target)

        v = _make_validator(tmp_path)
        result = v.validate(link, strict_symlink=True)
        assert result == link.resolve()

    def test_symlink_pointing_outside_sandbox_strict_raises(
        self, tmp_path: pathlib.Path
    ) -> None:
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
        with pytest.raises(PathViolation, match="Symlink strictly escapes sandbox"):
            v.validate(link, strict_symlink=True)

        # Clean up manually.
        outside_file.unlink(missing_ok=True)
        outside_dir.rmdir()

    def test_symlink_outside_sandbox_non_strict_allowed_if_resolves_inside(
        self, tmp_path: pathlib.Path
    ) -> None:
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

    def test_symlink_outside_sandbox_non_strict_blocked_if_resolves_outside(
        self, tmp_path: pathlib.Path
    ) -> None:
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
        with pytest.raises(PathViolation, match="outside all sandbox roots"):
            v.validate(link, strict_symlink=False)

        # Clean up.
        outside_file.unlink(missing_ok=True)
        outside_dir.rmdir()

    def test_broken_symlink_strict_raises_path_violation(
        self, tmp_path: pathlib.Path
    ) -> None:
        link = tmp_path / "broken_link.txt"
        link.symlink_to(tmp_path / "nonexistent_target.txt")

        v = _make_validator(tmp_path)
        with pytest.raises(PathViolation):
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
        with pytest.raises(PathViolation, match="outside all sandbox roots"):
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

    def test_roots_property_returns_tuple_of_resolved_paths(
        self, tmp_path: pathlib.Path
    ) -> None:
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
    def test_valid_positional_path_executes_function(
        self, tmp_path: pathlib.Path
    ) -> None:
        @sandboxed_io(roots=[tmp_path])
        def read_file(path: pathlib.Path) -> str:
            return "content"

        valid = tmp_path / "valid.txt"
        valid.touch()
        assert read_file(valid) == "content"

    def test_invalid_positional_path_raises_violation(
        self, tmp_path: pathlib.Path
    ) -> None:
        @sandboxed_io(roots=[tmp_path])
        def read_file(path: pathlib.Path) -> str:
            return "content"

        if sys.platform == "win32":
            outside = pathlib.Path("C:/Windows/System32/ntdll.dll")
        else:
            outside = pathlib.Path("/etc/shadow")
        with pytest.raises(PathViolation):
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
        with pytest.raises(PathViolation):
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
        with pytest.raises(PathViolation, match="traversal"):
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

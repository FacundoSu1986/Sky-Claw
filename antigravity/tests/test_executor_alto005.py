"""Tests for sky_claw.agent.executor (ALTO-005 fix).

Shift-Left Validation: ensures PathValidator is injected in __init__ and
reused across execute() calls, eliminating per-invocation instantiation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.agent.executor import ManagedToolExecutor
from sky_claw.antigravity.security.path_validator import PathValidator, PathViolationError


class TestManagedToolExecutorPathValidator:
    """ALTO-005: PathValidator lifecycle and reuse."""

    def test_injected_validator_is_reused(self) -> None:
        """Injected PathValidator must be stored and reused."""
        validator = MagicMock(spec=PathValidator)
        executor = ManagedToolExecutor(path_validator=validator)
        assert executor._validator is validator

    def test_lazy_initializes_validator_in_init(self, tmp_path) -> None:
        """If no validator is injected, one is created in __init__."""
        modding_root = tmp_path / "Modding"
        modding_root.mkdir()
        with patch(
            "sky_claw.antigravity.agent.executor.SystemPaths.modding_root",
            return_value=modding_root,
        ):
            executor = ManagedToolExecutor()
        assert executor._validator is not None
        assert isinstance(executor._validator, PathValidator)

    def test_does_not_recreate_validator_per_execute(self, tmp_path) -> None:
        """Validator instance must survive across execute() calls."""
        modding_root = tmp_path / "Modding"
        modding_root.mkdir()
        with patch(
            "sky_claw.antigravity.agent.executor.SystemPaths.modding_root",
            return_value=modding_root,
        ):
            executor = ManagedToolExecutor()
        first = executor._validator
        assert first is not None
        assert executor._validator is first

    @pytest.mark.asyncio
    async def test_execute_returns_negative_one_when_validator_unavailable(self) -> None:
        """If validator init failed, execute must fail fast."""
        executor = ManagedToolExecutor(path_validator=None)
        executor._validator = None
        result = await executor.execute("bin", ["arg"])
        assert result == -1

    @pytest.mark.asyncio
    async def test_execute_validates_paths_with_injected_validator(self) -> None:
        """execute() must use the injected validator for path checks."""
        validator = MagicMock(spec=PathValidator)
        validator.validate = MagicMock(return_value=MagicMock())
        executor = ManagedToolExecutor(path_validator=validator)

        mock_proc = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0

        wsl_arg = "/mnt/c/Modding/test"
        translated = "C:\\Modding\\test"
        with (
            patch(
                "sky_claw.antigravity.agent.executor.ModdingToolsAgent.translate_path_wsl_to_win",
                new=AsyncMock(return_value=translated),
            ),
            patch(
                "sky_claw.antigravity.agent.executor.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
        ):
            result = await executor.execute("bin", [wsl_arg])

        assert result == 0
        validator.validate.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_aborts_on_validation_failure(self) -> None:
        """PathViolationError during WSL path validation must abort before subprocess."""
        validator = MagicMock(spec=PathValidator)
        validator.validate.side_effect = PathViolationError("outside sandbox")
        executor = ManagedToolExecutor(path_validator=validator)

        wsl_arg = "/mnt/c/Modding/test"
        translated = "C:\\Modding\\test"
        with (
            patch(
                "sky_claw.antigravity.agent.executor.ModdingToolsAgent.translate_path_wsl_to_win",
                new=AsyncMock(return_value=translated),
            ),
            patch(
                "sky_claw.antigravity.agent.executor.asyncio.create_subprocess_exec",
            ) as mock_subproc,
        ):
            result = await executor.execute("bin", [wsl_arg])

        assert result == -1
        validator.validate.assert_called_once()
        mock_subproc.assert_not_called()

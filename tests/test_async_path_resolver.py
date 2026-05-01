"""Tests para AsyncPathResolver — primitiva de resolución de rutas no bloqueante.

Cubre:
- Fast-Path: hit de caché en O(1).
- Slow-Path: delegación a asyncio.to_thread y memoización.
- Cirugía de errores: solo OSError y RuntimeError se traducen a AsyncPathResolutionError.
- Concurrencia: múltiples corutinas sobre la misma ruta sólo realizan una llamada a I/O.
- Invariantes: logger inyectable, no hay variables globales, __slots__.
- Cache+strict: clave compuesta (raw_path, strict) evita colisiones entre strict=True/False.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.core.async_path_resolver import AsyncPathResolutionError, AsyncPathResolver

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def resolver() -> AsyncPathResolver:
    """AsyncPathResolver listo para usar con logger silencioso."""
    null_logger = logging.getLogger("tests.async_path_resolver")
    if not null_logger.handlers:
        null_logger.addHandler(logging.NullHandler())
    return AsyncPathResolver(logger=null_logger)


@pytest.fixture
def existing_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Directorio real que puede ser resuelto con strict=True."""
    target = tmp_path / "subdir"
    target.mkdir()
    return target


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


class TestInstantiation:
    """Verifica que la clase cumple sus invariantes de construcción."""

    def test_default_logger_is_assigned(self) -> None:
        r = AsyncPathResolver()
        assert r._logger.name == "SkyClaw.AsyncPathResolver"

    def test_custom_logger_is_injected(self) -> None:
        custom = logging.getLogger("custom.test")
        r = AsyncPathResolver(logger=custom)
        assert r._logger is custom

    def test_cache_starts_empty(self) -> None:
        r = AsyncPathResolver()
        assert r._cache == {}

    def test_uses_slots(self) -> None:
        assert hasattr(AsyncPathResolver, "__slots__")
        assert "dict" not in str(AsyncPathResolver.__dict__.get("__slots__", ""))


# ---------------------------------------------------------------------------
# Fast-Path
# ---------------------------------------------------------------------------


class TestFastPath:
    """Verifica que el caché evita llamadas redundantes a I/O."""

    async def test_second_call_returns_cached(
        self,
        resolver: AsyncPathResolver,
        existing_path: pathlib.Path,
    ) -> None:
        raw = str(existing_path)
        first = await resolver.resolve_safe(raw)

        with patch.object(
            AsyncPathResolver,
            "_resolve_blocking",
            side_effect=AssertionError("No debería llamarse en Fast-Path"),
        ):
            second = await resolver.resolve_safe(raw)

        assert first == second

    async def test_cache_is_populated_after_first_call(
        self,
        resolver: AsyncPathResolver,
        existing_path: pathlib.Path,
    ) -> None:
        raw = str(existing_path)
        await resolver.resolve_safe(raw)
        assert (raw, True) in resolver._cache

    async def test_different_raw_paths_cached_independently(
        self,
        resolver: AsyncPathResolver,
        tmp_path: pathlib.Path,
    ) -> None:
        path_a = tmp_path / "a"
        path_a.mkdir()
        path_b = tmp_path / "b"
        path_b.mkdir()

        await resolver.resolve_safe(str(path_a))
        await resolver.resolve_safe(str(path_b))

        assert (str(path_a), True) in resolver._cache
        assert (str(path_b), True) in resolver._cache


# ---------------------------------------------------------------------------
# Slow-Path
# ---------------------------------------------------------------------------


class TestSlowPath:
    """Verifica la delegación a asyncio.to_thread en el Slow-Path."""

    async def test_resolves_existing_path_strict(
        self,
        resolver: AsyncPathResolver,
        existing_path: pathlib.Path,
    ) -> None:
        result = await resolver.resolve_safe(str(existing_path), strict=True)
        assert result == existing_path.resolve()

    async def test_resolves_nonexistent_path_non_strict(
        self,
        resolver: AsyncPathResolver,
        tmp_path: pathlib.Path,
    ) -> None:
        ghost = tmp_path / "does_not_exist"
        result = await resolver.resolve_safe(str(ghost), strict=False)
        assert result == ghost.resolve(strict=False)

    async def test_to_thread_is_called_for_uncached_path(
        self,
        resolver: AsyncPathResolver,
        existing_path: pathlib.Path,
    ) -> None:
        raw = str(existing_path)
        expected = existing_path.resolve()

        with patch(
            "sky_claw.antigravity.core.async_path_resolver.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_thread:
            result = await resolver.resolve_safe(raw, strict=True)

        mock_thread.assert_awaited_once_with(
            AsyncPathResolver._resolve_blocking,
            raw,
            True,
        )
        assert result == expected


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Verifica la cirugía de errores: solo OSError y RuntimeError."""

    async def test_os_error_raises_domain_exception(
        self,
        resolver: AsyncPathResolver,
    ) -> None:
        raw = "/non/existent/strictly"
        with pytest.raises(AsyncPathResolutionError) as exc_info:
            await resolver.resolve_safe(raw, strict=True)

        assert "non/existent/strictly" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, OSError)

    async def test_runtime_error_is_translated(
        self,
        resolver: AsyncPathResolver,
        existing_path: pathlib.Path,
    ) -> None:
        raw = str(existing_path)

        with (
            patch(
                "sky_claw.antigravity.core.async_path_resolver.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=RuntimeError("simulated runtime failure"),
            ),
            pytest.raises(AsyncPathResolutionError) as exc_info,
        ):
            await resolver.resolve_safe(raw)

        assert isinstance(exc_info.value.__cause__, RuntimeError)

    async def test_unexpected_exception_propagates_unmodified(
        self,
        resolver: AsyncPathResolver,
        existing_path: pathlib.Path,
    ) -> None:
        raw = str(existing_path)

        with (
            patch(
                "sky_claw.antigravity.core.async_path_resolver.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=ValueError("bug upstream inesperado"),
            ),
            pytest.raises(ValueError, match="bug upstream inesperado"),
        ):
            await resolver.resolve_safe(raw)

    async def test_error_is_logged_with_exc_info(
        self,
        existing_path: pathlib.Path,
    ) -> None:
        mock_logger = MagicMock(spec=logging.Logger)
        r = AsyncPathResolver(logger=mock_logger)
        raw = str(existing_path)

        with (
            patch(
                "sky_claw.antigravity.core.async_path_resolver.asyncio.to_thread",
                new_callable=AsyncMock,
                side_effect=OSError("disco lleno"),
            ),
            pytest.raises(AsyncPathResolutionError),
        ):
            await r.resolve_safe(raw)

        mock_logger.error.assert_called_once()
        _, kwargs = mock_logger.error.call_args
        assert kwargs.get("exc_info") is True

    async def test_failed_path_is_not_cached(
        self,
        resolver: AsyncPathResolver,
    ) -> None:
        raw = "/absolutely/nonexistent/path/xyz"
        with pytest.raises(AsyncPathResolutionError):
            await resolver.resolve_safe(raw, strict=True)

        assert (raw, True) not in resolver._cache


# ---------------------------------------------------------------------------
# Concurrencia
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Verifica seguridad bajo corutinas concurrentes."""

    async def test_concurrent_same_path_resolves_once(
        self,
        resolver: AsyncPathResolver,
        existing_path: pathlib.Path,
    ) -> None:
        raw = str(existing_path)
        call_count = 0
        original = AsyncPathResolver._resolve_blocking

        def counting_resolve(raw: str, strict: bool) -> pathlib.Path:
            nonlocal call_count
            call_count += 1
            return original(raw, strict)

        with patch.object(AsyncPathResolver, "_resolve_blocking", side_effect=counting_resolve):
            results = await asyncio.gather(*[resolver.resolve_safe(raw) for _ in range(10)])

        assert all(r == existing_path.resolve() for r in results)
        assert call_count == 1

    async def test_concurrent_different_paths_all_succeed(
        self,
        resolver: AsyncPathResolver,
        tmp_path: pathlib.Path,
    ) -> None:
        paths = []
        for i in range(5):
            p = tmp_path / f"dir_{i}"
            p.mkdir()
            paths.append(p)

        results = await asyncio.gather(*[resolver.resolve_safe(str(p)) for p in paths])
        assert len(results) == 5
        assert all(r.exists() for r in results)


# ---------------------------------------------------------------------------
# Cache + strict interaction
# ---------------------------------------------------------------------------


class TestCacheStrictInteraction:
    """Verifica que la clave (raw_path, strict) aísla resoluciones strict=True/False."""

    async def test_nonexistent_strict_false_does_not_poison_strict_true(
        self,
        resolver: AsyncPathResolver,
        tmp_path: pathlib.Path,
    ) -> None:
        ghost = tmp_path / "ghost_dir"
        raw = str(ghost)

        # strict=False → cachea un Path resuelto sin verificar existencia.
        result_false = await resolver.resolve_safe(raw, strict=False)
        assert result_false == ghost.resolve(strict=False)

        # strict=True → debe fallar porque el path no existe, no servir desde caché.
        with pytest.raises(AsyncPathResolutionError) as exc_info:
            await resolver.resolve_safe(raw, strict=True)

        assert isinstance(exc_info.value.__cause__, OSError)

    async def test_strict_true_and_false_cached_independently(
        self,
        resolver: AsyncPathResolver,
        existing_path: pathlib.Path,
    ) -> None:
        raw = str(existing_path)

        await resolver.resolve_safe(raw, strict=True)
        await resolver.resolve_safe(raw, strict=False)

        assert (raw, True) in resolver._cache
        assert (raw, False) in resolver._cache

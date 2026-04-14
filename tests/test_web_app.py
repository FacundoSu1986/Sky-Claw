"""Tests for sky_claw.web.app.WebApp — security-focused.

Covers:
- /api/auto-detect 500 must NOT leak exception details
- /api/chat 500 must NOT leak exception details
- /api/setup loopback-only middleware (127.0.0.1 allowed, 192.168.1.1 → 403)
- /api/chat auth_manager: missing Bearer → 401, invalid token → 401, valid → 200
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from sky_claw.security.auth_token_manager import AuthTokenManager
from sky_claw.web.app import WebApp

if TYPE_CHECKING:
    import pathlib

    from aiohttp.test_utils import TestClient

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_web_app(
    router=None,
    session=None,
    config_path: pathlib.Path | None = None,
    auth_manager=None,
) -> WebApp:
    """Return a WebApp with sensible defaults for tests."""
    if session is None:
        session = MagicMock()
    return WebApp(
        router=router,
        session=session,
        config_path=config_path,
        auth_manager=auth_manager,
    )


def _make_mock_router(response: str = "ok") -> MagicMock:
    router = MagicMock()
    router.chat = AsyncMock(return_value=response)
    return router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_router() -> MagicMock:
    return _make_mock_router()


@pytest.fixture
def mock_session() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_local_config(tmp_path):
    """Patch load/save so no real filesystem config is touched."""
    cfg = MagicMock()
    cfg.first_run = False
    cfg.mo2_root = ""
    cfg.install_dir = ""
    cfg.loot_exe = ""
    cfg.xedit_exe = ""
    cfg.pandora_exe = ""
    cfg.bodyslide_exe = ""

    with (
        patch("sky_claw.web.app.load_local_config", return_value=cfg),
        patch("sky_claw.web.app.save_local_config"),
    ):
        yield cfg


# ---------------------------------------------------------------------------
# Helper: build an aiohttp TestClient pointing at a WebApp
# ---------------------------------------------------------------------------


async def _client(web_app: WebApp, aiohttp_client) -> TestClient:
    app = web_app.create_app()
    return await aiohttp_client(app)


# ===========================================================================
# 1.  /api/auto-detect — 500 must NOT expose exception message
# ===========================================================================


class TestAutoDetect500:
    """Confirm that a raised exception inside AutoDetector is NOT forwarded
    to the HTTP client — only a generic error message is returned."""

    @pytest.mark.asyncio
    async def test_500_does_not_leak_exception_detail(
        self, aiohttp_client, mock_router, mock_session, mock_local_config
    ):
        secret_detail = "super-secret internal path C:\\passwords\\db.key"
        web_app = _make_web_app(router=mock_router, session=mock_session)

        with patch(
            "sky_claw.web.app.AutoDetector.detect_all",
            new_callable=AsyncMock,
            side_effect=RuntimeError(secret_detail),
        ):
            client = await _client(web_app, aiohttp_client)
            resp = await client.get("/api/auto-detect")

        assert resp.status == 500
        body = await resp.json()

        # Must contain *some* error key
        assert "error" in body

        # Must NOT echo the internal exception message
        error_text = body["error"]
        assert secret_detail not in error_text, (
            f"Exception detail leaked in response: {error_text!r}"
        )

    @pytest.mark.asyncio
    async def test_500_returns_generic_message(
        self, aiohttp_client, mock_router, mock_session, mock_local_config
    ):
        """The generic message should be a non-empty, human-readable string."""
        web_app = _make_web_app(router=mock_router, session=mock_session)

        with patch(
            "sky_claw.web.app.AutoDetector.detect_all",
            new_callable=AsyncMock,
            side_effect=Exception("anything"),
        ):
            client = await _client(web_app, aiohttp_client)
            resp = await client.get("/api/auto-detect")

        body = await resp.json()
        assert isinstance(body.get("error"), str)
        assert len(body["error"]) > 0

    @pytest.mark.asyncio
    async def test_200_on_success(
        self, aiohttp_client, mock_router, mock_session, mock_local_config
    ):
        """Sanity check: when detect_all succeeds, 200 is returned."""
        web_app = _make_web_app(router=mock_router, session=mock_session)

        with patch(
            "sky_claw.web.app.AutoDetector.detect_all",
            new_callable=AsyncMock,
            return_value={"mo2_root": "C:\\MO2"},
        ):
            client = await _client(web_app, aiohttp_client)
            resp = await client.get("/api/auto-detect")

        assert resp.status == 200
        body = await resp.json()
        assert body.get("mo2_root") == "C:\\MO2"


# ===========================================================================
# 2.  /api/chat — 500 must NOT expose exception message
# ===========================================================================


class TestChat500:
    """Confirm that a router exception is NOT forwarded verbatim to the client."""

    @pytest.mark.asyncio
    async def test_500_does_not_leak_exception_detail(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        secret_detail = "db password=hunter2 at host internal.corp"
        router = MagicMock()
        router.chat = AsyncMock(side_effect=RuntimeError(secret_detail))

        web_app = _make_web_app(router=router, session=mock_session)
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "hello"})

        assert resp.status == 500
        body = await resp.json()
        assert "error" in body

        error_text = body["error"]
        assert secret_detail not in error_text, (
            f"Exception detail leaked in /api/chat response: {error_text!r}"
        )

    @pytest.mark.asyncio
    async def test_500_returns_generic_message(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        router = MagicMock()
        router.chat = AsyncMock(side_effect=Exception("boom"))

        web_app = _make_web_app(router=router, session=mock_session)
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "ping"})

        body = await resp.json()
        assert isinstance(body.get("error"), str)
        assert len(body["error"]) > 0

    @pytest.mark.asyncio
    async def test_500_body_is_json(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        """Even on error the response must be valid JSON."""
        router = MagicMock()
        router.chat = AsyncMock(side_effect=Exception("crash"))

        web_app = _make_web_app(router=router, session=mock_session)
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "test"})
        assert resp.content_type == "application/json"


# ===========================================================================
# 3.  /api/setup — loopback-only middleware
# ===========================================================================


class TestSetupLoopbackMiddleware:
    """The setup endpoints must only be reachable from loopback addresses."""

    # --- GET /api/setup ---

    @pytest.mark.asyncio
    async def test_get_setup_allowed_from_loopback(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        web_app = _make_web_app(session=mock_session)
        client = await _client(web_app, aiohttp_client)

        # aiohttp TestClient connects via loopback by default
        resp = await client.get("/api/setup")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_get_setup_blocked_from_remote_ip(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        """A non-loopback peer address must receive HTTP 403."""
        web_app = _make_web_app(session=mock_session)
        web_app.create_app()

        # Override request.remote for this request by patching the middleware
        # check by spoofing the peer via a custom header approach. Since
        # aiohttp's TestServer always presents loopback, we test the middleware
        # logic directly by injecting a fake Request.
        #
        # Strategy: subclass the app's _setup_auth_middleware and intercept
        # the remote attribute, OR call the middleware directly.
        #
        # We call the middleware directly with a mock request object.
        remote_request = MagicMock(spec=web.Request)
        remote_request.path = "/api/setup"
        remote_request.remote = "192.168.1.1"

        response = await web_app._setup_auth_middleware(
            remote_request,
            handler=AsyncMock(),  # should never be reached
        )
        assert response.status == 403

    @pytest.mark.asyncio
    async def test_get_setup_blocked_from_arbitrary_remote(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        """Any non-loopback IP is blocked, regardless of subnet."""
        web_app = _make_web_app(session=mock_session)

        for remote_ip in ("10.0.0.1", "172.16.0.5", "8.8.8.8", "203.0.113.1"):
            remote_request = MagicMock(spec=web.Request)
            remote_request.path = "/api/setup"
            remote_request.remote = remote_ip

            response = await web_app._setup_auth_middleware(
                remote_request,
                handler=AsyncMock(),
            )
            assert response.status == 403, (
                f"Expected 403 for remote {remote_ip}, got {response.status}"
            )

    @pytest.mark.asyncio
    async def test_setup_allowed_from_127_0_0_1(self, mock_session, mock_local_config):
        """127.0.0.1 must pass the loopback check."""
        web_app = _make_web_app(session=mock_session)

        handler_called = AsyncMock(return_value=web.Response(status=200))
        local_request = MagicMock(spec=web.Request)
        local_request.path = "/api/setup"
        local_request.remote = "127.0.0.1"

        response = await web_app._setup_auth_middleware(local_request, handler_called)

        handler_called.assert_called_once_with(local_request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_setup_allowed_from_ipv6_loopback(
        self, mock_session, mock_local_config
    ):
        """::1 (IPv6 loopback) must also be permitted."""
        web_app = _make_web_app(session=mock_session)

        handler_called = AsyncMock(return_value=web.Response(status=200))
        local_request = MagicMock(spec=web.Request)
        local_request.path = "/api/setup"
        local_request.remote = "::1"

        response = await web_app._setup_auth_middleware(local_request, handler_called)

        handler_called.assert_called_once_with(local_request)
        assert response.status == 200

    # --- POST /api/setup ---

    @pytest.mark.asyncio
    async def test_post_setup_blocked_from_remote_ip(
        self, mock_session, mock_local_config
    ):
        web_app = _make_web_app(session=mock_session)

        remote_request = MagicMock(spec=web.Request)
        remote_request.path = "/api/setup"
        remote_request.remote = "192.168.1.100"

        response = await web_app._setup_auth_middleware(
            remote_request, handler=AsyncMock()
        )
        assert response.status == 403

    @pytest.mark.asyncio
    async def test_non_setup_path_not_affected_by_loopback_check(
        self, mock_session, mock_local_config
    ):
        """Loopback restriction must NOT apply to other endpoints."""
        web_app = _make_web_app(router=_make_mock_router(), session=mock_session)

        handler_called = AsyncMock(return_value=web.Response(status=200))
        remote_request = MagicMock(spec=web.Request)
        remote_request.path = "/api/chat"
        remote_request.remote = "192.168.1.1"
        # No auth_manager — chat should pass the middleware without restriction
        remote_request.headers = {}

        await web_app._setup_auth_middleware(remote_request, handler_called)

        # Handler must have been called (not blocked)
        handler_called.assert_called_once()


# ===========================================================================
# 4.  /api/chat — Bearer token authentication
# ===========================================================================


class TestChatBearerAuth:
    """When auth_manager is configured, /api/chat is token-gated."""

    def _make_auth_manager(self, valid: bool = True) -> MagicMock:
        mgr = MagicMock(spec=AuthTokenManager)
        mgr.validate = MagicMock(return_value=valid)
        return mgr

    # --- Missing Authorization header ---

    @pytest.mark.asyncio
    async def test_missing_auth_header_returns_401(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(
            router=_make_mock_router(),
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "hello"})

        assert resp.status == 401

    # --- Wrong scheme (not Bearer) ---

    @pytest.mark.asyncio
    async def test_non_bearer_scheme_returns_401(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(
            router=_make_mock_router(),
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        resp = await client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status == 401

    # --- Invalid / wrong token ---

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        auth_mgr = self._make_auth_manager(valid=False)
        web_app = _make_web_app(
            router=_make_mock_router(),
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        resp = await client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status == 401

    # --- Valid token ---

    @pytest.mark.asyncio
    async def test_valid_token_returns_200(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        auth_mgr = self._make_auth_manager(valid=True)
        router = _make_mock_router("authenticated response")
        web_app = _make_web_app(
            router=router,
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        resp = await client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer valid-token-abc"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body.get("response") == "authenticated response"

    # --- No auth_manager: token is not required ---

    @pytest.mark.asyncio
    async def test_no_auth_manager_does_not_require_token(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        """If auth_manager is None, /api/chat must work without a token."""
        router = _make_mock_router("open response")
        web_app = _make_web_app(router=router, session=mock_session, auth_manager=None)
        client = await _client(web_app, aiohttp_client)

        resp = await client.post("/api/chat", json={"message": "hello"})
        assert resp.status == 200
        body = await resp.json()
        assert body.get("response") == "open response"

    # --- validate() is called with the token from the header ---

    @pytest.mark.asyncio
    async def test_validate_called_with_token_value(
        self, aiohttp_client, mock_session, mock_local_config
    ):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(
            router=_make_mock_router(),
            session=mock_session,
            auth_manager=auth_mgr,
        )
        client = await _client(web_app, aiohttp_client)

        token_value = "my-secret-token-xyz"
        await client.post(
            "/api/chat",
            json={"message": "test"},
            headers={"Authorization": f"Bearer {token_value}"},
        )

        auth_mgr.validate.assert_called_once_with(token_value)

    # --- Direct middleware test: 401 path ---

    @pytest.mark.asyncio
    async def test_middleware_401_without_bearer(self, mock_session):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(session=mock_session, auth_manager=auth_mgr)

        request = MagicMock(spec=web.Request)
        request.path = "/api/chat"
        request.remote = "127.0.0.1"
        request.headers = {"Authorization": ""}

        response = await web_app._setup_auth_middleware(
            request, handler=AsyncMock(return_value=web.Response(status=200))
        )
        assert response.status == 401

    # --- Direct middleware test: valid token passes through ---

    @pytest.mark.asyncio
    async def test_middleware_passes_valid_token(self, mock_session):
        auth_mgr = self._make_auth_manager(valid=True)
        web_app = _make_web_app(session=mock_session, auth_manager=auth_mgr)

        handler = AsyncMock(return_value=web.Response(status=200))
        request = MagicMock(spec=web.Request)
        request.path = "/api/chat"
        request.remote = "127.0.0.1"
        request.headers = {"Authorization": "Bearer goodtoken"}

        response = await web_app._setup_auth_middleware(request, handler)

        handler.assert_called_once()
        assert response.status == 200

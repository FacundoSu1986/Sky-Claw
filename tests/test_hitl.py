"""Comprehensive tests for sky_claw.security.hitl.HITLGuard.

Covers:
- requires_approval: pattern matching for out-of-scope hosts (exact, wildcard,
  in-scope host, non-URL, URL with path/port, case insensitivity).
- request_approval: auto-generates a unique request_id when none supplied;
  duplicate request_id returns DENIED immediately; reason/url/detail forwarded
  to notify_fn; pending dict cleared after each outcome.
- respond: APPROVED/DENIED set the correct Decision; unknown request_id returns
  False; event is set so the waiter unblocks.
- Timeout path: event never set within timeout → Decision.TIMEOUT; pending dict
  cleared afterwards.
- notify_fn failure → fail-closed Decision.TIMEOUT; pending dict cleared.
- Concurrency: two simultaneous requests can be resolved independently.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from sky_claw.security.hitl import Decision, HITLGuard, HITLRequest


# ---------------------------------------------------------------------------
# Helpers / factories
# ---------------------------------------------------------------------------


def _guard(
    notify_fn=None,
    timeout: int = 5,
    out_of_scope_hosts: frozenset[str] | None = None,
) -> HITLGuard:
    """Build a HITLGuard with deterministic test defaults."""
    if out_of_scope_hosts is None:
        # Use the real defaults from config so detection tests are realistic.
        return HITLGuard(notify_fn=notify_fn, timeout=timeout)
    return HITLGuard(
        notify_fn=notify_fn,
        timeout=timeout,
        out_of_scope_hosts=out_of_scope_hosts,
    )


def _fast_guard(notify_fn=None) -> HITLGuard:
    """Guard with timeout=0 so asyncio.wait_for expires immediately."""
    return HITLGuard(notify_fn=notify_fn, timeout=0)


async def _inject_pending(guard: HITLGuard, req_id: str, reason: str = "test") -> HITLRequest:
    """Directly insert a fake HITLRequest into the guard's pending dict."""
    fake = HITLRequest(request_id=req_id, reason=reason)
    async with guard._lock:
        guard._pending[req_id] = fake
    return fake


# ---------------------------------------------------------------------------
# TestRequiresApproval
# ---------------------------------------------------------------------------


class TestRequiresApproval:
    # --- out-of-scope hosts from real config defaults ---

    def test_github_is_out_of_scope(self) -> None:
        assert _guard().requires_approval("https://github.com/user/repo") is True

    def test_discord_is_out_of_scope(self) -> None:
        assert _guard().requires_approval("https://discord.com/channels/xyz") is True

    def test_dropbox_is_out_of_scope(self) -> None:
        assert _guard().requires_approval("https://dropbox.com/s/abc") is True

    def test_mega_is_out_of_scope(self) -> None:
        assert _guard().requires_approval("https://mega.nz/file/abc") is True

    def test_patreon_is_out_of_scope(self) -> None:
        assert _guard().requires_approval("https://patreon.com/creator") is True

    # --- in-scope hosts ---

    def test_nexusmods_in_scope(self) -> None:
        assert _guard().requires_approval("https://www.nexusmods.com/skyrimspecialedition") is False

    def test_nexus_api_in_scope(self) -> None:
        assert _guard().requires_approval("https://api.nexusmods.com/v1/mods/123") is False

    def test_unknown_host_not_flagged(self) -> None:
        assert _guard().requires_approval("https://example.com/page") is False

    # --- URL parsing edge cases ---

    def test_url_with_port_uses_hostname(self) -> None:
        assert _guard().requires_approval("https://github.com:443/repo") is True

    def test_url_with_path_and_query(self) -> None:
        assert _guard().requires_approval("https://discord.com/invite/abc?ref=1") is True

    def test_case_insensitive_hostname_matching(self) -> None:
        # urlparse lowercases hostname; requires_approval also lowercases.
        assert _guard().requires_approval("HTTPS://GITHUB.COM/user/repo") is True

    def test_empty_url_returns_false(self) -> None:
        assert _guard().requires_approval("") is False

    def test_non_url_string_returns_false(self) -> None:
        # urlparse cannot extract a hostname → returns "".
        assert _guard().requires_approval("not-a-url-at-all") is False

    def test_custom_out_of_scope_hosts_used(self) -> None:
        custom = frozenset(["evil.example.com"])
        guard = HITLGuard(out_of_scope_hosts=custom, timeout=1)
        assert guard.requires_approval("https://evil.example.com/payload") is True
        assert guard.requires_approval("https://github.com/repo") is False  # not in custom


# ---------------------------------------------------------------------------
# TestRequestApproval – unique request_id generation
# ---------------------------------------------------------------------------


class TestRequestApprovalIdGeneration:
    @pytest.mark.asyncio
    async def test_auto_generated_id_is_valid_uuid4(self) -> None:
        captured: list[HITLRequest] = []

        async def spy(req: HITLRequest) -> None:
            captured.append(req)
            req.decision = Decision.APPROVED
            req._event.set()

        guard = HITLGuard(notify_fn=spy, timeout=5)
        await guard.request_approval(reason="id_generation_test")

        assert len(captured) == 1
        req_id = captured[0].request_id
        parsed = uuid.UUID(req_id, version=4)
        assert str(parsed) == req_id

    @pytest.mark.asyncio
    async def test_two_calls_produce_distinct_ids(self) -> None:
        ids: list[str] = []

        async def spy(req: HITLRequest) -> None:
            ids.append(req.request_id)
            req.decision = Decision.APPROVED
            req._event.set()

        guard = HITLGuard(notify_fn=spy, timeout=5)
        await guard.request_approval(reason="first")
        await guard.request_approval(reason="second")

        assert len(ids) == 2
        assert ids[0] != ids[1]

    @pytest.mark.asyncio
    async def test_caller_supplied_request_id_is_used(self) -> None:
        captured: list[HITLRequest] = []
        fixed_id = "custom-id-abc123"

        async def spy(req: HITLRequest) -> None:
            captured.append(req)
            req.decision = Decision.APPROVED
            req._event.set()

        guard = HITLGuard(notify_fn=spy, timeout=5)
        await guard.request_approval(reason="custom", request_id=fixed_id)

        assert captured[0].request_id == fixed_id


# ---------------------------------------------------------------------------
# TestRequestApproval – duplicate request_id
# ---------------------------------------------------------------------------


class TestRequestApprovalDuplicateId:
    @pytest.mark.asyncio
    async def test_duplicate_pending_id_returns_denied_immediately(self) -> None:
        fixed_id = str(uuid.uuid4())
        guard = HITLGuard(timeout=5)

        # Manually insert the id as already-pending.
        await _inject_pending(guard, fixed_id, reason="original")

        # Now request_approval with the same id — should return DENIED without
        # calling notify_fn or waiting for the event.
        mock_notify = AsyncMock()
        guard._notify = mock_notify

        decision = await guard.request_approval(reason="duplicate", request_id=fixed_id)
        assert decision == Decision.DENIED
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_id_accepted_after_first_completes(self) -> None:
        """After the first request with a given id completes, the same id can be reused."""
        fixed_id = str(uuid.uuid4())
        guard = HITLGuard(timeout=5)

        async def immediate_approve(req: HITLRequest) -> None:
            req.decision = Decision.APPROVED
            req._event.set()

        guard._notify = immediate_approve

        first = await guard.request_approval(reason="first run", request_id=fixed_id)
        assert first == Decision.APPROVED
        # Pending dict should now be clear.
        assert fixed_id not in guard._pending

        # Second call with the same id should succeed (not be rejected as duplicate).
        second = await guard.request_approval(reason="second run", request_id=fixed_id)
        assert second == Decision.APPROVED


# ---------------------------------------------------------------------------
# TestRequestApproval – approved / denied decisions
# ---------------------------------------------------------------------------


class TestRequestApprovalDecisions:
    @pytest.mark.asyncio
    async def test_approved_decision_propagated(self) -> None:
        guard = HITLGuard(timeout=5)

        async def approve(req: HITLRequest) -> None:
            await asyncio.sleep(0)
            await guard.respond(req.request_id, approved=True)

        guard._notify = approve
        decision = await guard.request_approval(reason="approve test")
        assert decision == Decision.APPROVED

    @pytest.mark.asyncio
    async def test_denied_decision_propagated(self) -> None:
        guard = HITLGuard(timeout=5)

        async def deny(req: HITLRequest) -> None:
            await asyncio.sleep(0)
            await guard.respond(req.request_id, approved=False)

        guard._notify = deny
        decision = await guard.request_approval(reason="deny test")
        assert decision == Decision.DENIED

    @pytest.mark.asyncio
    async def test_url_and_detail_forwarded_to_notify(self) -> None:
        received: list[HITLRequest] = []

        async def capture(req: HITLRequest) -> None:
            received.append(req)
            req.decision = Decision.APPROVED
            req._event.set()

        guard = HITLGuard(notify_fn=capture, timeout=5)
        await guard.request_approval(
            reason="github asset",
            url="https://github.com/user/repo",
            detail="binary release v1.2.3",
        )

        assert received[0].url == "https://github.com/user/repo"
        assert received[0].detail == "binary release v1.2.3"
        assert received[0].reason == "github asset"

    @pytest.mark.asyncio
    async def test_pending_cleared_after_approval(self) -> None:
        guard = HITLGuard(timeout=5)
        req_id_box: list[str] = []

        async def capture_and_approve(req: HITLRequest) -> None:
            req_id_box.append(req.request_id)
            await guard.respond(req.request_id, approved=True)

        guard._notify = capture_and_approve
        await guard.request_approval(reason="cleanup_approved")
        assert req_id_box[0] not in guard._pending

    @pytest.mark.asyncio
    async def test_pending_cleared_after_denial(self) -> None:
        guard = HITLGuard(timeout=5)
        req_id_box: list[str] = []

        async def capture_and_deny(req: HITLRequest) -> None:
            req_id_box.append(req.request_id)
            await guard.respond(req.request_id, approved=False)

        guard._notify = capture_and_deny
        await guard.request_approval(reason="cleanup_denied")
        assert req_id_box[0] not in guard._pending


# ---------------------------------------------------------------------------
# TestRespond
# ---------------------------------------------------------------------------


class TestRespond:
    @pytest.mark.asyncio
    async def test_respond_approved_sets_approved_decision(self) -> None:
        guard = HITLGuard(timeout=5)
        req_id = str(uuid.uuid4())
        fake = await _inject_pending(guard, req_id)

        result = await guard.respond(req_id, approved=True)
        assert result is True
        assert fake.decision == Decision.APPROVED

    @pytest.mark.asyncio
    async def test_respond_denied_sets_denied_decision(self) -> None:
        guard = HITLGuard(timeout=5)
        req_id = str(uuid.uuid4())
        fake = await _inject_pending(guard, req_id)

        result = await guard.respond(req_id, approved=False)
        assert result is True
        assert fake.decision == Decision.DENIED

    @pytest.mark.asyncio
    async def test_respond_unknown_id_returns_false(self) -> None:
        guard = HITLGuard()
        result = await guard.respond("nonexistent-id-xyz-987", approved=True)
        assert result is False

    @pytest.mark.asyncio
    async def test_respond_unknown_id_does_not_mutate_pending(self) -> None:
        guard = HITLGuard()
        existing_id = str(uuid.uuid4())
        await _inject_pending(guard, existing_id)
        before = dict(guard._pending)
        await guard.respond("no-such-id", approved=True)
        assert guard._pending == before

    @pytest.mark.asyncio
    async def test_respond_sets_event_for_waiter(self) -> None:
        guard = HITLGuard(timeout=5)
        req_id = str(uuid.uuid4())
        fake = await _inject_pending(guard, req_id)

        assert not fake._event.is_set()
        await guard.respond(req_id, approved=True)
        assert fake._event.is_set()


# ---------------------------------------------------------------------------
# TestTimeout
# ---------------------------------------------------------------------------


class TestTimeout:
    @pytest.mark.asyncio
    async def test_no_response_within_timeout_returns_timeout(self) -> None:
        guard = HITLGuard(notify_fn=None, timeout=0)
        decision = await guard.request_approval(reason="timeout_test")
        assert decision == Decision.TIMEOUT

    @pytest.mark.asyncio
    async def test_timeout_clears_pending_entry(self) -> None:
        captured_id: list[str] = []

        async def stall(req: HITLRequest) -> None:
            captured_id.append(req.request_id)
            # Do NOT set the event; let timeout fire.

        guard = HITLGuard(notify_fn=stall, timeout=0)
        await guard.request_approval(reason="stall_test")
        assert captured_id[0] not in guard._pending

    @pytest.mark.asyncio
    async def test_timeout_decision_on_hitl_request(self) -> None:
        """The HITLRequest.decision field must be TIMEOUT after expiry."""
        last_req: list[HITLRequest] = []

        async def capture(req: HITLRequest) -> None:
            last_req.append(req)
            # Do not resolve; let timeout fire.

        guard = HITLGuard(notify_fn=capture, timeout=0)
        decision = await guard.request_approval(reason="decision_field_test")
        assert decision == Decision.TIMEOUT
        if last_req:
            assert last_req[0].decision == Decision.TIMEOUT


# ---------------------------------------------------------------------------
# TestNotifyFnFailure
# ---------------------------------------------------------------------------


class TestNotifyFnFailure:
    @pytest.mark.asyncio
    async def test_notify_raises_returns_timeout(self) -> None:
        """notify_fn raising an exception -> fallback to TIMEOUT."""

        async def broken(req: HITLRequest) -> None:
            raise RuntimeError("Telegram unreachable")

        guard = HITLGuard(notify_fn=broken, timeout=5)
        decision = await guard.request_approval(reason="notify_fail")
        assert decision == Decision.TIMEOUT

    @pytest.mark.asyncio
    async def test_notify_raises_clears_pending(self) -> None:
        captured_id: list[str] = []

        async def broken(req: HITLRequest) -> None:
            captured_id.append(req.request_id)
            raise ConnectionRefusedError("no Telegram")

        guard = HITLGuard(notify_fn=broken, timeout=5)
        decision = await guard.request_approval(reason="notify_fail_cleanup")
        assert decision == Decision.TIMEOUT
        assert captured_id[0] not in guard._pending

    @pytest.mark.asyncio
    async def test_notify_mock_called_once_then_timeout(self) -> None:
        mock_notify = AsyncMock(side_effect=OSError("network down"))
        guard = HITLGuard(notify_fn=mock_notify, timeout=5)
        decision = await guard.request_approval(reason="mock_fail")
        assert decision == Decision.TIMEOUT
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_notify_fn_times_out_normally(self) -> None:
        """With no notify_fn the guard should wait then time out."""
        guard = HITLGuard(notify_fn=None, timeout=0)
        decision = await guard.request_approval(reason="no_notify_fn")
        assert decision == Decision.TIMEOUT

    @pytest.mark.asyncio
    async def test_notify_value_error_is_fail_closed(self) -> None:
        async def raise_value_error(req: HITLRequest) -> None:
            raise ValueError("bad config")

        guard = HITLGuard(notify_fn=raise_value_error, timeout=5)
        decision = await guard.request_approval(reason="value_error")
        assert decision == Decision.TIMEOUT


# ---------------------------------------------------------------------------
# TestConcurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_two_concurrent_requests_resolved_independently(self) -> None:
        guard = HITLGuard(timeout=5)
        captured: list[HITLRequest] = []

        async def capture(req: HITLRequest) -> None:
            captured.append(req)

        guard._notify = capture

        # Fire both concurrently.
        t1 = asyncio.create_task(guard.request_approval(reason="alpha"))
        t2 = asyncio.create_task(guard.request_approval(reason="beta"))

        # Let both tasks register their pending entries.
        await asyncio.sleep(0.05)
        assert len(captured) == 2

        # Resolve in opposite order: deny first, approve second.
        await guard.respond(captured[0].request_id, approved=False)
        await guard.respond(captured[1].request_id, approved=True)

        d1 = await t1
        d2 = await t2

        assert d1 == Decision.DENIED
        assert d2 == Decision.APPROVED

    @pytest.mark.asyncio
    async def test_resolving_one_does_not_affect_other(self) -> None:
        guard = HITLGuard(timeout=5)
        ids: list[str] = []

        async def capture(req: HITLRequest) -> None:
            ids.append(req.request_id)

        guard._notify = capture

        t1 = asyncio.create_task(guard.request_approval(reason="req_one"))
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(guard.request_approval(reason="req_two"))
        await asyncio.sleep(0.05)

        # Resolve only the first task.
        await guard.respond(ids[0], approved=True)
        d1 = await t1
        assert d1 == Decision.APPROVED

        # Second task must still be pending.
        assert not t2.done()
        assert ids[1] in guard._pending

        # Now clean up the second task.
        await guard.respond(ids[1], approved=False)
        d2 = await t2
        assert d2 == Decision.DENIED

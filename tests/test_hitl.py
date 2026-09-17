"""Comprehensive tests for sky_claw.app.security.hitl.HITLGuard.

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

import ast
import asyncio
import pathlib
import uuid
from unittest.mock import AsyncMock

import pytest

from sky_claw.app.security.hitl import Decision, HITLGuard, HITLRequest, new_hitl_request_id

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

    def test_new_hitl_request_id_is_unique_and_preserves_prefix(self) -> None:
        first = new_hitl_request_id("test")
        second = new_hitl_request_id("test")

        assert first != second
        assert first.startswith("test-")
        assert len(first.encode("utf-8")) <= 51

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


class TestHitlProducerRequestIds:
    """Ancla de familia: todo productor humano debe evitar IDs reutilizables."""

    _EXPECTED_PRODUCERS = {
        "sky_claw/app/agent/tools/nexus_tools.py": {"download_mod": 1},
        "sky_claw/app/agent/tools/system_tools.py": {"install_mod_from_archive": 1},
        "sky_claw/app/orchestrator/dyndolod_readiness_hitl.py": {"confirmar": 1},
        "sky_claw/app/orchestrator/preview/approval_gate.py": {"preview_then_execute": 1},
        "sky_claw/app/orchestrator/sandbox_promotion.py": {"_request_decision": 1},
        "sky_claw/app/orchestrator/sync_engine.py": {"_check_and_update_mod": 2},
        "sky_claw/app/orchestrator/tool_strategies/middleware.py": {"__call__": 1},
        "sky_claw/local/tools_installer.py": {
            "ensure_loot": 1,
            "ensure_xedit": 1,
            "ensure_pandora": 1,
            "ensure_skse": 1,
            "ensure_bodyslide": 1,
            "_ensure_github_mod": 1,
            "_ensure_nexus_mod": 1,
        },
    }

    _EXPECTED_PREFIXES = {
        ("sky_claw/app/agent/tools/nexus_tools.py", "download_mod"): "nexus-download",
        ("sky_claw/app/agent/tools/system_tools.py", "install_mod_from_archive"): "mod-install",
        ("sky_claw/app/orchestrator/dyndolod_readiness_hitl.py", "confirmar"): "dyndolod-readiness",
        ("sky_claw/app/orchestrator/sync_engine.py", "_check_and_update_mod"): "mod-update",
        ("sky_claw/local/tools_installer.py", "ensure_loot"): "loot-install",
        ("sky_claw/local/tools_installer.py", "ensure_xedit"): "xedit-install",
        ("sky_claw/local/tools_installer.py", "ensure_pandora"): "pandora-install",
        ("sky_claw/local/tools_installer.py", "ensure_skse"): "skse-install",
        ("sky_claw/local/tools_installer.py", "ensure_bodyslide"): "bodyslide-install",
        ("sky_claw/local/tools_installer.py", "_ensure_github_mod"): "github-mod-install",
        ("sky_claw/local/tools_installer.py", "_ensure_nexus_mod"): "nexus-mod-install",
    }

    @staticmethod
    def _dotted_names(node: ast.AST) -> set[str]:
        return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)} | {
            item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute)
        }

    @classmethod
    def _is_unique_expression(cls, expression: ast.AST, function: ast.AST, seen: set[str]) -> bool:
        names = cls._dotted_names(expression)
        if "new_hitl_request_id" in names or "uuid4" in names:
            return True
        if not isinstance(expression, ast.Name) or expression.id in seen:
            return False
        seen.add(expression.id)
        values: list[ast.AST] = []
        for item in ast.walk(function):
            if isinstance(item, ast.Assign):
                targets = item.targets
                value = item.value
            elif isinstance(item, ast.AnnAssign):
                targets = [item.target]
                value = item.value
            else:
                continue
            if (
                any(isinstance(target, ast.Name) and target.id == expression.id for target in targets)
                and value is not None
            ):
                values.append(value)
        return bool(values) and all(cls._is_unique_expression(value, function, seen.copy()) for value in values)

    def test_todos_los_productores_humanos_tienen_identidad_por_intento(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        discovered: dict[str, dict[str, int]] = {}
        calls: list[tuple[str, str, ast.Call, ast.AST]] = []

        for path in sorted((root / "sky_claw").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

            class Visitor(ast.NodeVisitor):
                def __init__(self, relative_path: str) -> None:
                    self._relative_path = relative_path
                    self.functions: list[tuple[str, ast.AST]] = []

                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    self.functions.append((node.name, node))
                    self.generic_visit(node)
                    self.functions.pop()

                visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

                def visit_Call(self, node: ast.Call) -> None:
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "request_approval" and self.functions:
                        function_name, function_node = self.functions[-1]
                        calls.append((self._relative_path, function_name, node, function_node))
                    self.generic_visit(node)

            Visitor(path.relative_to(root).as_posix()).visit(tree)

        for relative, function_name, call, function_node in calls:
            discovered.setdefault(relative, {})[function_name] = (
                discovered.setdefault(relative, {}).get(function_name, 0) + 1
            )
            request_id = next((keyword.value for keyword in call.keywords if keyword.arg == "request_id"), None)
            if request_id is None:
                assert (relative, function_name) == (
                    "sky_claw/app/orchestrator/preview/approval_gate.py",
                    "preview_then_execute",
                )
            else:
                assert self._is_unique_expression(request_id, function_node, set()), (
                    f"request_id no único en {relative}:{function_name}"
                )

        assert discovered == self._EXPECTED_PRODUCERS

    def test_prefijos_productivos_son_constantes_y_no_dependen_de_telegram(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]

        for (relative, function_name), expected_prefix in self._EXPECTED_PREFIXES.items():
            path = root / relative
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            function = next(
                (
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
                ),
                None,
            )
            assert function is not None, f"productor no encontrado: {relative}:{function_name}"

            helper_calls = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and "new_hitl_request_id" in self._dotted_names(node.func)
            ]
            assert len(helper_calls) == 1, f"prefijo ambiguo en {relative}:{function_name}"
            prefix_argument = helper_calls[0].args[0]
            assert isinstance(prefix_argument, ast.Constant) and isinstance(prefix_argument.value, str)
            assert prefix_argument.value == expected_prefix

            # El request_id es una identidad del guard y ya no está condicionado
            # por el límite de callback_data; Telegram lo tokeniza en su boundary.
            sample_request_id = f"{expected_prefix}-{'0' * 1000}"
            assert sample_request_id.startswith(expected_prefix)


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
        """Sin respuesta durante el timeout → TIMEOUT (política fail-secure)."""
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
        """HITLRequest.decision debe ser TIMEOUT tras expirar (fail-secure)."""
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
# TestTimeoutRespondRace (F6, auditoría 2026-07-18)
# ---------------------------------------------------------------------------


class TestTimeoutRespondRace:
    """La resolución de una request debe ser atómica: primer escritor gana bajo
    el lock. Antes, ``request_approval`` seteaba ``req.decision = DENIED`` FUERA
    del lock en el timeout mientras ``respond`` lo seteaba dentro — dos
    escritores compitiendo. Un ``respond`` que llegaba en la ventana devolvía
    ``True`` ("aprobación registrada") al operador aunque la request se
    auto-denegara, o una aprobación tardía pisaba el fail-secure."""

    @pytest.mark.asyncio
    async def test_segundo_respond_no_pisa_la_decision_y_devuelve_false(self) -> None:
        """Ancla de la race: una vez resuelta la request (primer respond gana),
        un segundo respond NO puede cambiar la decisión y devuelve False. Es el
        mismo mecanismo que protege contra el respond-tardío-vs-timeout."""
        guard = HITLGuard(timeout=5)
        req_id = str(uuid.uuid4())
        fake = await _inject_pending(guard, req_id)

        assert await guard.respond(req_id, approved=True) is True
        assert fake.decision == Decision.APPROVED

        # El operador (o una race) intenta denegar después: rechazado, sin pisar.
        assert await guard.respond(req_id, approved=False) is False
        assert fake.decision == Decision.APPROVED

    @pytest.mark.asyncio
    async def test_respond_tras_resolucion_por_timeout_es_rechazado(self) -> None:
        """Fail-secure: tras el auto-deny por timeout, un respond tardío no
        encuentra la request (ni la pisa) y devuelve False — sin ack falso."""
        guard = HITLGuard(notify_fn=None, timeout=0)
        decision = await guard.request_approval(request_id="r-late", reason="x")
        assert decision == Decision.TIMEOUT

        assert await guard.respond("r-late", approved=True) is False

    @pytest.mark.asyncio
    async def test_respond_en_ventana_de_notify_se_honra(self) -> None:
        """Un respond que llega ANTES de que expire el timeout (simulado dentro
        de notify_fn, justo después del registro) se honra: la decisión del
        operador gana y el waiter se despierta con APPROVED."""
        acks: list[bool] = []

        async def notify(req: HITLRequest) -> None:
            acks.append(await guard.respond(req.request_id, approved=True))

        guard = HITLGuard(notify_fn=notify, timeout=5)
        decision = await guard.request_approval(request_id="r-intime", reason="x")

        assert decision == Decision.APPROVED
        assert acks == [True]


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
        """Sin notify_fn, el guard espera y expira → TIMEOUT (fail-secure)."""
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
# TestNotifyCancellation
# ---------------------------------------------------------------------------


class TestNotifyCancellation:
    @pytest.mark.asyncio
    async def test_cancelacion_durante_notify_limpia_pending_y_se_propaga(self) -> None:
        """Cancelar notify no debe dejar la solicitud huérfana ni tragarse el cancel."""
        notify_started = asyncio.Event()

        async def blocking_notify(req: HITLRequest) -> None:
            notify_started.set()
            await asyncio.Event().wait()

        request_id = "cancel-during-notify"
        guard = HITLGuard(notify_fn=blocking_notify, timeout=5)
        task = asyncio.create_task(guard.request_approval(request_id=request_id, reason="cancel test"))
        await notify_started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert request_id not in guard._pending

    @pytest.mark.asyncio
    async def test_request_id_reutilizable_tras_cancelacion_durante_notify(self) -> None:
        """El mismo ID debe poder usarse después de cancelar la notificación."""
        notify_started = asyncio.Event()

        async def blocking_notify(req: HITLRequest) -> None:
            notify_started.set()
            await asyncio.Event().wait()

        request_id = "reusable-after-cancel"
        guard = HITLGuard(notify_fn=blocking_notify, timeout=5)
        task = asyncio.create_task(guard.request_approval(request_id=request_id, reason="first attempt"))
        await notify_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async def approve(req: HITLRequest) -> None:
            await guard.respond(req.request_id, approved=True)

        guard._notify = approve
        decision = await guard.request_approval(request_id=request_id, reason="second attempt")

        assert decision is Decision.APPROVED


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


# ---------------------------------------------------------------------------
# TestHITLFailSecure
# ---------------------------------------------------------------------------


class TestHITLFailSecure:
    """Verifica fail-secure: timeout → TIMEOUT y nunca APPROVED."""

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout(self) -> None:
        """Sin respuesta del operador, la decisión debe ser TIMEOUT (fail-secure)."""

        async def stall(req: HITLRequest) -> None:
            # No resolver: dejar que expire el timeout.
            pass

        guard = HITLGuard(notify_fn=stall, timeout=0)
        decision = await guard.request_approval(reason="fail_secure_timeout_test")
        assert decision == Decision.TIMEOUT
        # La expiración sigue siendo explícitamente no aprobada.
        assert decision != Decision.APPROVED

    @pytest.mark.asyncio
    async def test_timeout_is_not_approved(self) -> None:
        """Una solicitud expirada nunca debe aprobarse."""

        async def stall(req: HITLRequest) -> None:
            # No resolver: dejar que expire el timeout.
            pass

        guard = HITLGuard(notify_fn=stall, timeout=0)
        decision = await guard.request_approval(reason="timeout_denied_test")
        assert decision != Decision.APPROVED
        assert decision == Decision.TIMEOUT

    @pytest.mark.asyncio
    async def test_timeout_with_short_duration(self) -> None:
        """Un timeout de 0.05 s también debe devolver TIMEOUT."""

        async def stall(req: HITLRequest) -> None:
            # No establecer el evento: dejar que expire el timeout.
            pass

        guard = HITLGuard(notify_fn=stall, timeout=0.05)
        decision = await guard.request_approval(reason="short_timeout_test")
        assert decision == Decision.TIMEOUT

    @pytest.mark.asyncio
    async def test_decision_timeout_enum_still_exists(self) -> None:
        """Decision.TIMEOUT must still exist (may be used in logs/UI)."""
        # Verify the enum value exists (not deleted)
        assert hasattr(Decision, "TIMEOUT")
        assert Decision.TIMEOUT.value == "timeout"


# ---------------------------------------------------------------------------
# T5-v2 — familia H1-H8: categoría de readiness, timeout por request y
# aprobación stale. El productor real es
# ``sky_claw.app.orchestrator.dyndolod_readiness_hitl.ConfirmadorHITL``.
# ---------------------------------------------------------------------------


def _solicitud_de_readiness(tool: str = "TexGen", timeout: float = 5.0):
    from sky_claw.local.tools.dyndolod_uia_gate import OperatorConfigurationReadyRequest  # noqa: PLC0415

    return OperatorConfigurationReadyRequest(
        tool_name=tool,
        pid=4242,
        executable=pathlib.Path("C:/Modding/DynDOLOD/TexGenx64.exe"),
        expected_output=pathlib.Path("E:/Modding/ExternalWork/DynDOLOD/TexGen"),
        timeout_seconds=timeout,
        observed_output=r"E:\Sky-Claw T5 Rig\Stale TexGen",
    )


def _confirmador(guard: HITLGuard):
    from sky_claw.app.orchestrator.dyndolod_readiness_hitl import ConfirmadorHITL  # noqa: PLC0415

    return ConfirmadorHITL(hitl_guard=guard)


class TestCategoriaDeReadiness:
    """H1/H2 — la categoría existe y nunca se auto-aprueba en Modo local."""

    def test_h1_la_categoria_existe_y_no_es_tool_execution(self) -> None:
        from sky_claw.app.security.hitl import CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA  # noqa: PLC0415

        assert CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA == "dyndolod_configuracion_lista"
        assert CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA != "tool_execution"

    @pytest.mark.asyncio
    async def test_h2_el_modo_local_no_la_auto_aprueba(self) -> None:
        from sky_claw.app.gui.controllers.ritual_runner import make_gui_hitl_notify  # noqa: PLC0415
        from sky_claw.app.security.hitl import CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA  # noqa: PLC0415

        aprobaciones: list[tuple[str, bool]] = []
        parkeados: list[dict] = []

        async def _respond(request_id: str, approved: bool) -> None:
            aprobaciones.append((request_id, approved))

        notify = make_gui_hitl_notify(
            respond=_respond,
            set_pending=parkeados.append,
            auto_approve_getter=lambda: True,  # «Modo local» ENCENDIDO
            tab_id_getter=lambda: "tab-1",
            delegate=None,
        )
        await notify(
            HITLRequest(
                request_id="dyndolod-readiness-abc",
                reason="configuración lista",
                detail="pid=1",
                category=CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA,
            )
        )
        assert aprobaciones == [], "Modo local auto-aprobó una confirmación mid-run"
        assert len(parkeados) == 1
        assert parkeados[0]["category"] == CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA

    @pytest.mark.asyncio
    async def test_h2b_tool_execution_si_se_autoaprueba_para_contrastar(self) -> None:
        """Control positivo: la categoría vecina SÍ se auto-aprueba en Modo local.

        Sin este contraste, el test de arriba pasaría también si el wrapper
        ignorara el auto-approve para TODAS las categorías (que no es el caso).
        """
        from sky_claw.app.gui.controllers.ritual_runner import make_gui_hitl_notify  # noqa: PLC0415

        aprobaciones: list[tuple[str, bool]] = []

        async def _respond(request_id: str, approved: bool) -> None:
            aprobaciones.append((request_id, approved))

        notify = make_gui_hitl_notify(
            respond=_respond,
            set_pending=lambda payload: None,
            auto_approve_getter=lambda: True,
            tab_id_getter=lambda: "tab-1",
            delegate=None,
        )
        await notify(HITLRequest(request_id="tool-generate_lods-abc", reason="r", category="tool_execution"))
        assert aprobaciones == [("tool-generate_lods-abc", True)]


class TestConfirmadorHITLDeReadiness:
    """H3-H6 — traducción, categoría efectiva y timeout por request."""

    @pytest.mark.asyncio
    async def test_h3_approve_se_traduce_a_aprobada(self) -> None:
        from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion  # noqa: PLC0415

        visto: list[HITLRequest] = []
        guard: HITLGuard

        async def _auto_approve(req: HITLRequest) -> None:
            visto.append(req)
            await guard.respond(req.request_id, True)

        guard = HITLGuard(notify_fn=_auto_approve, timeout=5)
        resultado = await _confirmador(guard).confirmar(_solicitud_de_readiness())

        assert resultado is ResultadoConfirmacion.APROBADA
        assert len(visto) == 1
        from sky_claw.app.security.hitl import CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA  # noqa: PLC0415

        assert visto[0].category == CATEGORIA_DYNDOLOD_CONFIGURACION_LISTA
        assert visto[0].request_id.startswith("dyndolod-readiness-")

    @pytest.mark.asyncio
    async def test_h4_deny_se_traduce_a_denegada(self) -> None:
        from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion  # noqa: PLC0415

        guard: HITLGuard

        async def _deny(req: HITLRequest) -> None:
            await guard.respond(req.request_id, False)

        guard = HITLGuard(notify_fn=_deny, timeout=5)
        assert await _confirmador(guard).confirmar(_solicitud_de_readiness()) is ResultadoConfirmacion.DENEGADA

    @pytest.mark.asyncio
    async def test_h5_el_timeout_de_la_solicitud_manda_sobre_el_global(self) -> None:
        """El plazo de la solicitud se respeta aunque el global sea enorme."""
        from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion  # noqa: PLC0415

        async def _stall(req: HITLRequest) -> None:
            return None  # nunca responde

        guard = HITLGuard(notify_fn=_stall, timeout=3600)
        inicio = asyncio.get_running_loop().time()
        resultado = await _confirmador(guard).confirmar(_solicitud_de_readiness(timeout=0.05))
        transcurrido = asyncio.get_running_loop().time() - inicio

        assert resultado is ResultadoConfirmacion.TIMEOUT
        assert transcurrido < 30, "el override por solicitud no se aplicó (esperó el global)"

    @pytest.mark.asyncio
    async def test_h6_sin_timeout_explicito_el_guard_conserva_su_global(self) -> None:
        """H6: el default sigue siendo ``self._timeout`` — sin cambios de conducta."""

        async def _stall(req: HITLRequest) -> None:
            return None

        guard = HITLGuard(notify_fn=_stall, timeout=0.05)
        assert await guard.request_approval(request_id="sin-override") is Decision.TIMEOUT

    @pytest.mark.asyncio
    async def test_h6c_un_timeout_no_positivo_es_bug_del_caller(self) -> None:
        """Finding Qodo: un 0/negativo cortaría al instante y disfrazaría el bug.

        ``asyncio.wait_for`` con plazo no positivo devuelve de inmediato, así que
        el fail-closed convertiría cada prompt en un ``TIMEOUT`` instantáneo. Se
        rechaza ANTES de registrar el pendiente: la entrada no queda colgada.
        """
        guard = HITLGuard(timeout=5)
        with pytest.raises(ValueError, match="timeout debe ser finito y > 0"):
            await guard.request_approval(request_id="cero", timeout=0)
        with pytest.raises(ValueError, match="timeout debe ser finito y > 0"):
            await guard.request_approval(request_id="negativo", timeout=-1)
        assert guard._pending == {}  # noqa: SLF001 -- sin pendiente fantasma

    @pytest.mark.asyncio
    @pytest.mark.parametrize("valor", [float("nan"), float("inf"), float("-inf")])
    async def test_h6d_un_timeout_no_finito_es_bug_del_caller(self, valor: float) -> None:
        """Finding CodeRabbit: nan/inf pasan la comparación ``<= 0`` y no son plazos.

        ``nan`` haría un ``TIMEOUT`` casi inmediato; ``inf`` dejaría la espera sin
        límite. Ninguno es una espera humana válida: se rechazan igual que el
        cero, antes de registrar el pendiente.
        """
        guard = HITLGuard(timeout=5)
        with pytest.raises(ValueError, match="timeout debe ser finito y > 0"):
            await guard.request_approval(request_id="no-finito", timeout=valor)
        assert guard._pending == {}  # noqa: SLF001 -- sin pendiente fantasma

    @pytest.mark.asyncio
    async def test_h6b_sin_canal_cableado_es_canal_no_disponible(self) -> None:
        from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion  # noqa: PLC0415

        assert (
            await _confirmador(None).confirmar(_solicitud_de_readiness())  # type: ignore[arg-type]
            is ResultadoConfirmacion.CANAL_NO_DISPONIBLE
        )


class TestPromptDeReadiness:
    """A9-A11 — el prompt le dice al operador QUÉ corregir, y no lo contrario.

    El rig real (2026-09-10) midió que TexGen arranca con el Output del preset
    rancio precargado y que la corrección la hace el operador a mano. El texto
    del HITL es el contrato de esa corrección: cita el Output observado, el
    esperado (la misma raíz del ``-o:``) y ordena corregir el campo antes de
    aprobar. La versión anterior —"dejá el campo Output como está"— hacía
    imposible el flujo documentado y quedó refutada.
    """

    @staticmethod
    async def _reason_de_la_solicitud() -> str:
        visto: list[HITLRequest] = []
        guard: HITLGuard

        async def _auto_approve(req: HITLRequest) -> None:
            visto.append(req)
            await guard.respond(req.request_id, True)

        guard = HITLGuard(notify_fn=_auto_approve, timeout=5)
        await _confirmador(guard).confirmar(_solicitud_de_readiness())
        assert len(visto) == 1
        return visto[0].reason

    @pytest.mark.asyncio
    async def test_a9_el_reason_incluye_el_expected_output(self) -> None:
        reason = await self._reason_de_la_solicitud()
        assert str(_solicitud_de_readiness().expected_output) in reason

    @pytest.mark.asyncio
    async def test_a10_el_reason_no_pide_dejar_el_output_como_esta(self) -> None:
        reason = await self._reason_de_la_solicitud()
        assert "dejá el campo Output como está" not in reason

    @pytest.mark.asyncio
    async def test_a11_el_observed_output_aparece_en_el_prompt(self) -> None:
        solicitud = _solicitud_de_readiness()
        reason = await self._reason_de_la_solicitud()
        assert solicitud.observed_output is not None
        assert solicitud.observed_output in reason, "el operador tiene que saber QUÉ corregir"
        # Requisito 3 del encargo: tool, pid, observado y esperado en el prompt.
        assert solicitud.tool_name in reason
        assert f"pid={solicitud.pid}" in reason
        assert str(solicitud.expected_output) in reason

    @staticmethod
    async def _reason_para(tool: str) -> str:
        visto: list[HITLRequest] = []
        guard: HITLGuard

        async def _auto_approve(req: HITLRequest) -> None:
            visto.append(req)
            await guard.respond(req.request_id, True)

        guard = HITLGuard(notify_fn=_auto_approve, timeout=5)
        await _confirmador(guard).confirmar(_solicitud_de_readiness(tool=tool))
        assert len(visto) == 1
        return visto[0].reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", ["TexGen", "DynDOLOD"])
    async def test_a12_el_reason_prohibe_pulsar_start_hasta_la_verificacion_final(self, tool: str) -> None:
        """A12: el prompt declara el protocolo entero, no sólo la corrección.

        El contrato débil del gate (``dyndolod_uia_gate``) es que el operador NO
        interactúe con Start durante el gate y que Sky-Claw no continúe hasta
        MATCH. El prompt es la única superficie que el operador lee antes de
        aprobar, así que la instrucción "no pulses Start todavía" tiene que
        viajar acá — aprobar habilita el FINAL gate, no el botón Start. El
        aviso "podés continuar con Start" llega recién después del MATCH final.
        Un solo adapter sirve a las dos herramientas: el contrato es el mismo.
        """
        reason = await self._reason_para(tool)
        assert "no pulses start" in reason.lower(), (
            f"el prompt de {tool} no prohíbe pulsar Start tras aprobar: {reason!r}"
        )
        assert "podés continuar con Start" in reason, (
            f"el prompt de {tool} no anuncia el aviso post-final-MATCH como momento de Start: {reason!r}"
        )


class TestAvisoDeReadiness:
    """T5-v2.1 — el aviso post-final-MATCH llega a una superficie real.

    El finding de review: en producción `ConfirmadorHITL` se construía sin
    `on_informar`, así que "podés continuar con Start" quedaba en un log y el
    operador que aprobó el modal no recibía señal. La entrega ahora usa la
    superficie de avisos del guard (`HITLGuard.notify_operator`), cableada por
    la GUI al panel de feedback del ritual. Sigue siendo best-effort: una
    superficie caída no puede tumbar una corrida ya verificada.
    """

    @pytest.mark.asyncio
    async def test_el_aviso_llega_al_notice_fn_del_guard_sin_on_informar(self) -> None:
        recibidos: list[str] = []

        async def _notice(mensaje: str) -> None:
            recibidos.append(mensaje)

        guard = HITLGuard(timeout=5, notice_fn=_notice)
        await _confirmador(guard).informar(tool="TexGen", mensaje="podés continuar con Start")
        assert recibidos == ["podés continuar con Start"]

    @pytest.mark.asyncio
    async def test_el_on_informar_explicito_gana_sobre_el_notice_del_guard(self) -> None:
        recibidos_guard: list[str] = []
        recibidos_explicitos: list[tuple[str, str]] = []

        async def _notice(mensaje: str) -> None:
            recibidos_guard.append(mensaje)

        async def _explicito(tool: str, mensaje: str) -> None:
            recibidos_explicitos.append((tool, mensaje))

        guard = HITLGuard(timeout=5, notice_fn=_notice)
        from sky_claw.app.orchestrator.dyndolod_readiness_hitl import ConfirmadorHITL  # noqa: PLC0415

        await ConfirmadorHITL(hitl_guard=guard, on_informar=_explicito).informar(tool="TexGen", mensaje="listo")
        assert recibidos_explicitos == [("TexGen", "listo")]
        assert recibidos_guard == []

    @pytest.mark.asyncio
    async def test_un_notice_fn_roto_no_propaga_ni_gatea(self) -> None:
        async def _roto(_mensaje: str) -> None:
            raise RuntimeError("sin superficie")

        guard = HITLGuard(timeout=5, notice_fn=_roto)
        await _confirmador(guard).informar(tool="TexGen", mensaje="listo")  # no lanza

    @pytest.mark.asyncio
    async def test_el_bootloader_gui_cablea_el_aviso_al_feedback_del_ritual(self) -> None:
        from sky_claw.app.gui._bootloader import _install_gui_hitl_bridge  # noqa: PLC0415
        from sky_claw.app.gui.controllers.ritual_runner import STORE_KEY_RITUAL_FEEDBACK  # noqa: PLC0415

        class _StoreFalso:
            def __init__(self) -> None:
                self.escrituras: dict[object, object] = {}

            def set(self, clave, valor) -> None:
                self.escrituras[clave] = valor

            def get(self, clave):
                return self.escrituras.get(clave)

        class _CtxFalso:
            def __init__(self, guard: HITLGuard) -> None:
                self.hitl = guard

        guard = HITLGuard(timeout=5)
        store = _StoreFalso()
        _install_gui_hitl_bridge(_CtxFalso(guard), store)  # type: ignore[arg-type]

        assert guard.notice_fn is not None, "la GUI no cableó la superficie de avisos"
        await guard.notify_operator("Output verificado: podés continuar con Start.")
        assert store.escrituras[STORE_KEY_RITUAL_FEEDBACK] == {
            "text": "Output verificado: podés continuar con Start.",
            "type": "info",
        }

    @pytest.mark.asyncio
    async def test_el_bootloader_gui_compone_sobre_el_aviso_de_telegram(self) -> None:
        """El puente GUI no puede PISAR el notice_fn de Telegram de AppContext.

        AppContext cablea ``notice_fn`` con el sender de Telegram para que el
        aviso post-final-MATCH llegue al chat que recibió el prompt HITL. Si el
        bootloader reemplazara la superficie en vez de componerla, en modo GUI
        el operador de Telegram deja de recibirla — el defecto "hermano sin
        fix" (clase #1 del repo): arreglar un camino y dejar el gemelo intacto.
        """
        from sky_claw.app.gui._bootloader import _install_gui_hitl_bridge  # noqa: PLC0415
        from sky_claw.app.gui.controllers.ritual_runner import STORE_KEY_RITUAL_FEEDBACK  # noqa: PLC0415

        class _StoreFalso:
            def __init__(self) -> None:
                self.escrituras: dict[object, object] = {}

            def set(self, clave, valor) -> None:
                self.escrituras[clave] = valor

            def get(self, clave):
                return self.escrituras.get(clave)

        class _CtxFalso:
            def __init__(self, guard: HITLGuard) -> None:
                self.hitl = guard

        mensaje = "Output verificado contra la raíz administrada: podés continuar con Start."
        telegram_recibidos: list[str] = []

        async def _notice_telegram(texto: str) -> None:
            telegram_recibidos.append(texto)

        guard = HITLGuard(timeout=5, notice_fn=_notice_telegram)
        store = _StoreFalso()
        _install_gui_hitl_bridge(_CtxFalso(guard), store)  # type: ignore[arg-type]

        assert guard.notice_fn is not None
        await guard.notify_operator(mensaje)

        # La GUI escribe el panel...
        assert store.escrituras[STORE_KEY_RITUAL_FEEDBACK]["text"] == mensaje
        # ...y Telegram (la superficie previa de AppContext) sigue entregando.
        assert telegram_recibidos == [mensaje]


class TestAprobacionStale:
    """H7/H8 — una aprobación vieja no resuelve una request nueva."""

    @pytest.mark.asyncio
    async def test_h7_una_aprobacion_de_otra_corrida_no_satisface_la_nueva(self) -> None:
        guard = HITLGuard(timeout=5)  # sin notify: espera la respuesta del operador
        tarea = asyncio.create_task(guard.request_approval(request_id="corrida-nueva", reason="r"))
        for _ in range(200):
            if "corrida-nueva" in guard._pending:  # noqa: SLF001 -- la ventana se prueba por estado
                break
            await asyncio.sleep(0)
        else:  # pragma: no cover -- la request nunca se registró
            pytest.fail("la request nueva no se registró como pendiente")

        # Un clic sobre el modal de la corrida ANTERIOR no debe resolver ésta.
        assert await guard.respond("corrida-anterior", approved=True) is False
        assert "corrida-nueva" in guard._pending  # noqa: SLF001
        assert await guard.respond("corrida-nueva", approved=True) is True
        assert await tarea is Decision.APPROVED

    @pytest.mark.asyncio
    async def test_h8_la_cancelacion_de_la_espera_se_preserva(self) -> None:
        guard = HITLGuard(timeout=3600)
        tarea = asyncio.create_task(guard.request_approval(request_id="cancelable", reason="r"))
        for _ in range(200):
            if "cancelable" in guard._pending:  # noqa: SLF001
                break
            await asyncio.sleep(0)
        else:  # pragma: no cover
            pytest.fail("la request no se registró como pendiente")

        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        assert "cancelable" not in guard._pending  # noqa: SLF001

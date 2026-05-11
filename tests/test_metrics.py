"""Tests for the Prometheus metrics module and HTTP /metrics endpoint."""

from __future__ import annotations

from prometheus_client import CollectorRegistry
from prometheus_client.exposition import generate_latest


class TestMetricsModule:
    def test_registry_is_isolated(self) -> None:
        from sky_claw.antigravity.core.metrics import get_registry

        reg = get_registry()
        assert isinstance(reg, CollectorRegistry)

    def test_counters_and_helpers_exist(self) -> None:
        from sky_claw.antigravity.core import metrics as m

        assert hasattr(m, "SYNC_ATTEMPTS_TOTAL")
        assert hasattr(m, "SYNC_DURATION_SECONDS")
        assert hasattr(m, "QUEUE_DEPTH")
        assert hasattr(m, "CIRCUIT_BREAKER_STATE")
        assert callable(m.record_sync_success)
        assert callable(m.record_sync_failure)
        assert callable(m.record_queue_depth)
        assert callable(m.record_circuit_state)

    def test_record_sync_success_increments(self) -> None:
        from sky_claw.antigravity.core import metrics as m

        m.record_sync_success(count=1)
        body = generate_latest(m.get_registry()).decode()
        assert 'sky_claw_sync_attempts_total{status="success"}' in body

    def test_record_circuit_state_maps_string_to_int(self) -> None:
        from sky_claw.antigravity.core import metrics as m

        m.record_circuit_state("masterlist", "open")
        body = generate_latest(m.get_registry()).decode()
        assert 'sky_claw_circuit_breaker_state{breaker_name="masterlist"} 2.0' in body

    def test_record_queue_depth_sets_value(self) -> None:
        from sky_claw.antigravity.core import metrics as m

        m.record_queue_depth(42)
        body = generate_latest(m.get_registry()).decode()
        assert "sky_claw_queue_depth 42.0" in body


def _build_app(validator):
    from sky_claw.antigravity.core.metrics_server import build_metrics_app

    return build_metrics_app(validator=validator)


class TestMetricsServer:
    def test_resolve_bind_port_uses_explicit_port(self, monkeypatch) -> None:
        from sky_claw.antigravity.core.metrics_server import _resolve_bind_port

        monkeypatch.setenv("SKYCLAW_METRICS_PORT", "not-an-int")
        assert _resolve_bind_port(9200) == 9200

    def test_resolve_bind_port_invalid_env_falls_back_default(self, monkeypatch, caplog) -> None:
        from sky_claw.antigravity.core.metrics_server import _DEFAULT_PORT, _resolve_bind_port

        caplog.set_level("WARNING")
        monkeypatch.setenv("SKYCLAW_METRICS_PORT", "invalid")
        assert _resolve_bind_port(None) == _DEFAULT_PORT
        assert any("metrics_port_invalid_fallback" in rec.message for rec in caplog.records)

    async def test_returns_401_without_token(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app(lambda t: t == "good"))
        resp = await client.get("/metrics")
        assert resp.status == 401

    async def test_returns_401_with_wrong_token(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app(lambda t: t == "good"))
        resp = await client.get("/metrics", headers={"X-Auth-Token": "bad"})
        assert resp.status == 401

    async def test_returns_200_and_prometheus_content_type(self, aiohttp_client) -> None:
        client = await aiohttp_client(_build_app(lambda t: t == "good"))
        resp = await client.get("/metrics", headers={"X-Auth-Token": "good"})
        assert resp.status == 200
        ctype = resp.headers["Content-Type"]
        assert ctype.startswith("text/plain")
        body = await resp.text()
        assert "sky_claw_sync_attempts_total" in body
        assert "sky_claw_queue_depth" in body

    async def test_increments_visible_in_response(self, aiohttp_client) -> None:
        from sky_claw.antigravity.core import metrics as m

        client = await aiohttp_client(_build_app(lambda t: t == "good"))
        m.record_sync_success(3)
        resp = await client.get("/metrics", headers={"X-Auth-Token": "good"})
        body = await resp.text()
        assert 'sky_claw_sync_attempts_total{status="success"}' in body


class TestSyncEngineInstrumentation:
    def test_sync_engine_imports_metrics_helpers(self) -> None:
        """Static smoke: if symbol names change, the instrumentation breaks."""
        from sky_claw.antigravity.orchestrator import sync_engine

        with open(sync_engine.__file__, encoding="utf-8") as fh:
            src = fh.read()
        assert "record_sync_success" in src
        assert "record_sync_failure" in src
        assert "record_queue_depth" in src
        assert "record_circuit_state" in src
        assert "SYNC_DURATION_SECONDS" in src


class TestAppContextWiring:
    def test_app_context_starts_and_stops_metrics_server(self) -> None:
        """Static smoke: AppContext references the metrics server lifecycle."""
        from sky_claw import app_context

        with open(app_context.__file__, encoding="utf-8") as fh:
            src = fh.read()
        assert "start_metrics_server" in src
        assert "stop_metrics_server" in src
        assert "push_async_callback" in src

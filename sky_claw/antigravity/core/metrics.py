"""Prometheus metrics for sky_claw observability.

Module-level singletons over a private CollectorRegistry. Helpers wrap label
construction to keep call-sites short and type-safe. The private registry
avoids leaking metric definitions into the global REGISTRY (eases tests and
prevents collisions if a future component also uses prometheus_client).
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

_REGISTRY: Final[CollectorRegistry] = CollectorRegistry()

SYNC_ATTEMPTS_TOTAL: Final[Counter] = Counter(
    "sky_claw_sync_attempts_total",
    "Total sync attempts by status",
    ["status"],
    registry=_REGISTRY,
)

SYNC_DURATION_SECONDS: Final[Histogram] = Histogram(
    "sky_claw_sync_duration_seconds",
    "Duration of a single batch sync cycle",
    buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0),
    registry=_REGISTRY,
)

QUEUE_DEPTH: Final[Gauge] = Gauge(
    "sky_claw_queue_depth",
    "Current depth of the sync producer-consumer queue",
    registry=_REGISTRY,
)

CIRCUIT_BREAKER_STATE: Final[Gauge] = Gauge(
    "sky_claw_circuit_breaker_state",
    "Circuit breaker state per breaker_name (0=closed, 1=half-open, 2=open)",
    ["breaker_name"],
    registry=_REGISTRY,
)

_STATE_INT_MAP: Final[dict[str, int]] = {
    "closed": 0,
    "half-open": 1,
    "open": 2,
}


def get_registry() -> CollectorRegistry:
    return _REGISTRY


def record_sync_success(count: int = 1) -> None:
    SYNC_ATTEMPTS_TOTAL.labels(status="success").inc(count)


def record_sync_failure(count: int = 1) -> None:
    SYNC_ATTEMPTS_TOTAL.labels(status="failed").inc(count)


def record_queue_depth(value: int) -> None:
    QUEUE_DEPTH.set(value)


def record_circuit_state(breaker_name: str, state: str) -> None:
    CIRCUIT_BREAKER_STATE.labels(breaker_name=breaker_name).set(_STATE_INT_MAP.get(state, 0))

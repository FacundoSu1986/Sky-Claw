"""Anclas estructurales del harness de acceptance de Grass Cache."""

from tests.bdd.support.grass_cache_harness import (
    SUPERFICIE_DISPATCHER_SUPERVISOR,
    superficie_dispatcher_supervisor,
)


def test_harness_enumera_toda_la_superficie_del_supervisor() -> None:
    """Una dependencia nueva del dispatcher exige cablearla explícitamente."""
    assert superficie_dispatcher_supervisor() == SUPERFICIE_DISPATCHER_SUPERVISOR

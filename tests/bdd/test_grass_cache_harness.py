"""Anclas estructurales del harness de acceptance de Grass Cache."""

from tests.bdd.support.grass_cache_harness import (
    SUPERFICIE_DISPATCHER_DEPENDENCIES,
    superficie_dispatcher_dependencies,
)


def test_harness_enumera_toda_la_superficie_de_dependencias() -> None:
    """Una dependencia nueva del dispatcher exige cablearla explícitamente."""
    assert superficie_dispatcher_dependencies() == SUPERFICIE_DISPATCHER_DEPENDENCIES

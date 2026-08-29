"""Anclas estructurales del harness de acceptance de Grass Cache."""

from tests.bdd.support.grass_cache_harness import (
    SUPERFICIE_DISPATCHER_DEPENDENCIES,
    campos_dispatcher_dependencies,
    superficie_dispatcher_dependencies,
)


def test_harness_enumera_toda_la_superficie_de_dependencias() -> None:
    """Una dependencia nueva del dispatcher exige cablearla explícitamente.

    La doble igualdad (campos del dataclass + uso por AST) cierra el
    "hermano sin cablear": agregar un campo no usado al dataclass, o usar
    un atributo ``dependencies.*`` no declarado, rompen este test antes
    de que la regresión llegue a producción.
    """
    assert campos_dispatcher_dependencies() == SUPERFICIE_DISPATCHER_DEPENDENCIES
    assert superficie_dispatcher_dependencies() == SUPERFICIE_DISPATCHER_DEPENDENCIES

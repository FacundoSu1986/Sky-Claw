"""Sensor de orden de masters para el preflight.

`missing_masters` responde *"¿el master está instalado y habilitado?"*. Este
sensor responde la otra mitad: *"¿carga **antes** que el plugin que lo
declara?"*. Un master presente pero posterior a su dependiente es CTD al
arrancar, y hasta acá ningún validador del repo cubría ese eje — el hueco lo
expuso la auditoría del módulo "Wrye Bash headless" propuesto, que devolvía
``SUCCESS`` sobre un load order con el master invertido.

Reusa el parser TES4 canónico
(:func:`~sky_claw.local.validators.plugin_header.read_plugin_header`): no abre
un segundo camino de parsing binario, que es precisamente el defecto que la
auditoría marcó en el snippet.

**El orden efectivo no es el de ``plugins.txt``.** El motor hoistea el bloque de
masters: todo plugin con flag ESM —o extensión ``.esm``/``.esl``— carga antes
que cualquier ``.esp`` plano, sin importar su posición en el archivo. Comparar
los índices crudos de ``plugins.txt`` produciría falsos positivos en cualquier
setup real (un ``.esm`` listado al final es normal y correcto). Por eso
:func:`effective_load_order` reordena primero y la comparación se hace sobre
*esa* secuencia.

Separación de responsabilidades deliberada: un master ausente del load order o
un header ilegible **no** se reportan acá — ya son issues de ``missing_masters``
(``missing``/``disabled``/``unreadable``), y duplicarlos haría que el semáforo
cuente dos veces el mismo problema. Este sensor solo juzga posición.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sky_claw.local.validators.plugin_header import (
    PluginHeader,
    PluginHeaderError,
    index_plugin_files,
    read_plugin_header,
)
from sky_claw.local.validators.preflight import PreflightCheck, PreflightStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MasterOrderChecker",
    "OrderIssue",
    "OrderIssueKind",
    "Severity",
    "effective_load_order",
    "order_preflight_check",
]

Severity = Literal["critical", "warning"]
OrderIssueKind = Literal["master_after_dependent"]

#: Extensiones que el motor carga en el bloque de masters aunque el record no
#: traiga el flag ESM. Los ``.esl`` van al pool ligero (FE), que también carga
#: antes que los ``.esp`` planos.
_MASTER_BLOCK_SUFFIXES: frozenset[str] = frozenset({".esm", ".esl"})


@dataclass(frozen=True, slots=True)
class OrderIssue:
    """Un master que carga después del plugin que lo declara.

    Attributes:
        plugin: Plugin dependiente.
        master: Master declarado que carga tarde.
        kind: Siempre ``master_after_dependent`` (el sensor tiene un solo modo
            de fallo; el campo existe para que el consumidor no tenga que
            discriminar por tipo al componer con los otros sensores).
        severity: Siempre ``critical`` — el motor no arranca.
        plugin_index: Posición del dependiente en el orden **efectivo**.
        master_index: Posición del master en el orden **efectivo**.
        remediation: Qué hacer, en términos del usuario.
    """

    plugin: str
    master: str
    kind: OrderIssueKind
    severity: Severity
    plugin_index: int
    master_index: int
    remediation: str


def effective_load_order(
    enabled_plugins: Sequence[str],
    *,
    is_master: dict[str, bool],
) -> list[str]:
    """Reordena el load order como lo hace el motor: masters primero.

    Skyrim carga el bloque de masters completo antes que los ``.esp`` planos,
    conservando el orden relativo dentro de cada bloque. Un plugin ausente de
    ``is_master`` (no está en disco o su header no se pudo leer) se trata como
    no-master: es el caso conservador, porque asumirlo master lo movería hacia
    adelante y podría **ocultar** una inversión real.

    Args:
        enabled_plugins: Load order habilitado, en orden de ``plugins.txt``.
        is_master: Nombre casefold → si carga en el bloque de masters.
    """
    bloque_masters: list[str] = []
    resto: list[str] = []
    for nombre in enabled_plugins:
        destino = bloque_masters if is_master.get(nombre.casefold(), False) else resto
        destino.append(nombre)
    return bloque_masters + resto


class MasterOrderChecker:
    """Detecta masters que cargan después de su dependiente.

    Args:
        plugin_dirs: Directorios donde viven los plugins (``Data`` del juego
            y/o carpetas de mods de MO2 — fuera del VFS los plugins están
            repartidos entre mods, por eso se aceptan varios, en orden de
            precedencia). No se recorre recursivo.
    """

    def __init__(self, *, plugin_dirs: Sequence[pathlib.Path]) -> None:
        self._plugin_dirs = tuple(plugin_dirs)

    def check(self, enabled_plugins: Sequence[str]) -> list[OrderIssue]:
        """Devuelve las inversiones de orden del load order, en orden estable."""
        available = index_plugin_files(self._plugin_dirs, log=logger)
        headers = self._read_headers(enabled_plugins, available)

        is_master = {
            key: header.is_master or pathlib.Path(key).suffix in _MASTER_BLOCK_SUFFIXES
            for key, header in headers.items()
        }
        efectivo = effective_load_order(enabled_plugins, is_master=is_master)

        # Primera aparición gana: un plugins.txt malformado con nombres
        # repetidos no debe inventar inversiones contra su propia copia.
        posicion: dict[str, int] = {}
        for indice, nombre in enumerate(efectivo):
            posicion.setdefault(nombre.casefold(), indice)

        issues: list[OrderIssue] = []
        for nombre in efectivo:
            key = nombre.casefold()
            header = headers.get(key)
            if header is None:
                continue  # ausente o ilegible: lo reporta `missing_masters`.
            indice_plugin = posicion[key]
            for master in header.masters:
                indice_master = posicion.get(master.casefold())
                if indice_master is None:
                    continue  # no está en el load order: `missing_masters` lo cubre.
                if indice_master < indice_plugin:
                    continue
                issues.append(
                    OrderIssue(
                        plugin=nombre,
                        master=master,
                        kind="master_after_dependent",
                        severity="critical",
                        plugin_index=indice_plugin,
                        master_index=indice_master,
                        remediation=(
                            f"'{nombre}' declara master a '{master}', que carga después: "
                            "movelo antes de su dependiente (corré LOOT para reordenar "
                            "el load order)."
                        ),
                    )
                )

        if issues:
            logger.warning(
                "Master order: %d inversión(es) en el load order: %s",
                len(issues),
                [f"{i.plugin}←{i.master}" for i in issues],
            )
        return issues

    def _read_headers(
        self,
        enabled_plugins: Sequence[str],
        available: dict[str, pathlib.Path],
    ) -> dict[str, PluginHeader]:
        """Lee el header de cada plugin habilitado que exista y sea legible."""
        headers: dict[str, PluginHeader] = {}
        for nombre in enabled_plugins:
            key = nombre.casefold()
            if key in headers:
                continue
            path = available.get(key)
            if path is None:
                continue
            try:
                headers[key] = read_plugin_header(path)
            except PluginHeaderError as exc:
                # `missing_masters` ya emite el issue `unreadable`; acá solo se
                # deja constancia y el plugin queda fuera del análisis.
                logger.debug("Header ilegible en %s: %s", nombre, exc)
        return headers


def order_preflight_check(issues: Sequence[OrderIssue]) -> PreflightCheck:
    """Compone los issues en un :class:`PreflightCheck` para el semáforo.

    Siempre rojo cuando hay issues: una inversión de masters no es una
    advertencia, el motor no arranca.
    """
    if not issues:
        return PreflightCheck(
            name="master_order",
            status=PreflightStatus.GREEN,
            summary="Todos los masters cargan antes que sus dependientes.",
        )
    return PreflightCheck(
        name="master_order",
        status=PreflightStatus.RED,
        summary=f"{len(issues)} master(s) cargan después de su dependiente.",
        details=tuple(f"{i.plugin} → {i.master} [{i.kind}]: {i.remediation}" for i in issues),
    )

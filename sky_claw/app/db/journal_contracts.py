"""Contrato compartido del journal entre servicios (lectura ↔ escritura).

El guard Stage 5→8 de ``GrassCacheService`` decide si LOOT (Stage 5) terminó un
sort exitoso leyendo la metadata de la última operación journalizada de LOOT.
Ese acuerdo LOOT↔grass estaba codificado como strings sueltos en ambos lados
(el que escribe el ``FlightReport`` y el que lo lee), un contrato frágil: si un
lado renombra una clave, el otro rompe en silencio (análisis hostil §2.2).

Este módulo es la ÚNICA fuente de verdad de esas claves/valores. Tanto el lado
de escritura (``FlightReport`` en ``orchestrator/preview/manifest.py``,
persistido por ``LootSortingService``) como el de lectura (el guard) deben
referenciar estas constantes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sky_claw.app.db.journal import TransactionStatus

#: Discriminador del ``FlightReport`` dentro de ``metadata`` (lo distingue del
#: ActionManifest, que viaja como metadata de otra operación en la misma TX).
FLIGHT_REPORT_KIND = "flight_report"

#: Valor de ``metadata["transaction_status"]`` que prueba un sort commiteado.
#: Se toma del enum canónico del journal para no desincronizar el string.
TX_STATUS_COMMITTED = TransactionStatus.COMMITTED.value

#: Discriminador de la operación que registra un teardown incompleto (U-09).
#: El ritual de grass puede terminar con el PRODUCTO bien (el cache generado y
#: preservado en ``overwrite/Grass``) pero el CLEANUP a medias (el clon de perfil
#: o el mod de config sin borrar). Como ``transactions`` solo admite
#: ``pending``/``committed``/``rolled_back``, ese estado mixto se expresa con una
#: operación FALLIDA dentro de una TX COMMITEADA: marcar la TX entera como
#: revertida mentiría sobre un cache que sí existe.
TEARDOWN_INCOMPLETE_KIND = "teardown_incomplete"


def is_flight_report_committed(metadata: Mapping[str, Any] | None) -> bool:
    """True si *metadata* es la de un ``FlightReport`` de una TX commiteada.

    Es el marcador CONFIABLE de que LOOT completó un sort exitoso: el
    ``FlightReport`` solo se emite en el path de éxito y carga el estado real de
    la transacción. Una operación COMPLETED pelada NO alcanza — el
    ActionManifest pre-sort también queda COMPLETED aunque el sort falle.
    """
    if not isinstance(metadata, Mapping):
        return False
    return metadata.get("kind") == FLIGHT_REPORT_KIND and metadata.get("transaction_status") == TX_STATUS_COMMITTED


def is_teardown_incomplete(metadata: Mapping[str, Any] | None) -> bool:
    """True si *metadata* marca un teardown que dejó residuos en disco (U-09).

    Identifica la operación que el ritual de grass registra cuando el cache se
    generó bien pero el clon/mod no se pudieron borrar. Leerlo permite distinguir
    "TX commiteada y FS limpio" de "TX commiteada con cleanup pendiente" sin
    inspeccionar los paths uno por uno.
    """
    if not isinstance(metadata, Mapping):
        return False
    return metadata.get("kind") == TEARDOWN_INCOMPLETE_KIND


__all__ = [
    "FLIGHT_REPORT_KIND",
    "TEARDOWN_INCOMPLETE_KIND",
    "TX_STATUS_COMMITTED",
    "is_flight_report_committed",
    "is_teardown_incomplete",
]

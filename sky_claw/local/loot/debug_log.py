"""Hallazgos del ``LOOTDebugLog.txt`` de UNA corrida de LOOT (A4, U-06 b).

Qué se lee y por qué con este alcance (evidencia: loot/loot ``0.29.1``):

* Formato de línea de spdlog: ``[%T.%f] [%l]: %v`` (``src/gui/state/logging.cpp:125,141``).
* El log vive en ``<loot-data-path>/LOOTDebugLog.txt`` y LOOT lo recrea al
  inicializar su runtime (``loot_state.cpp:105-106``); antes de cada corrida
  Sky-Claw planta un sentinel (:mod:`.execution_witness`). Un archivo que todavía
  es el sentinel no tiene nada de LOOT; uno anterior al inicio de la corrida
  (``desde``) es de otra corrida. En los dos casos no hay hallazgos.

**Firmas fatales** (:data:`LOOT_LOG_FATAL_SIGNATURES`, conjunto CERRADO y citado):
mensajes ``[error]``/``[critical]`` cuyos caminos upstream terminan en un modal
(``handleError``/``handleException``) o agregan un mensaje de error que cancela
el auto-sort o el auto-cierre (``main_window.cpp:2830-2846,2878-2880``). Con
salida 0 y testigo fresco son una CONTRADICCIÓN, y el clasificador falla cerrado.

**Todo el resto** de ``[error]``/``[critical]`` es diagnóstico, no veredicto.
Gatear sobre cualquier error daría falsos rojos: ``Failed to find Xbox gaming
root paths`` (``loot_state.cpp:306``) aparece en máquinas sanas y los errores
de ``closeEvent`` (``main_window.cpp:1443-1494``) ocurren DESPUÉS de aplicar el
orden. Agregar una firma es una decisión de contrato con su cita (ancla:
``tests/test_loot_debug_log.py``).

Las líneas devueltas ya están saneadas (``sanitize_for_prompt``) y acotadas:
llegan al LLM y a la GUI.
"""

from __future__ import annotations

import os
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from sky_claw.app.security.sanitize import sanitize_for_prompt
from sky_claw.local.loot.execution_witness import LOOT_DEBUG_LOG_FILENAME, LOOT_WITNESS_SENTINEL_PREFIX

#: Prefijo del mensaje → cita en loot/loot 0.29.1 (``src/gui/...``).
LOOT_LOG_FATAL_SIGNATURES: Final[Mapping[str, str]] = MappingProxyType(
    {
        # Sort fallido: excepción del SortPluginsQuery → modal (main_window.cpp:1585).
        "Failed to sort plugins": "game.cpp:199,217,876,888",
        # handleException / handleQueryException → QMessageBox::critical (main_window.cpp:1526-1531).
        "Caught an exception": "main_window.cpp:1536,1551",
        # appendMessage(MessageType::error) → cancela el auto-sort.
        "An error occurred while parsing the metadata list(s)": "game.cpp:967",
        "An error occurred while parsing the userlist": "game.cpp:991",
        "Failed to load current load order": "game.cpp:1188",
        # Fallo de un query en background (tarea del sort o de la carga del juego).
        "Exception while executing query": "tasks.cpp:42,67",
        # Sin juego seleccionado no hay sort: "...could not be selected" / "...could not be set".
        "Initial game could not be": "loot_state.cpp:386,407",
        "Game-specific settings could not be initialised": "loot_state.cpp:162",
    }
)

#: Niveles de spdlog que cuentan como error.
_NIVELES_DE_ERROR: Final[frozenset[str]] = frozenset({"error", "critical"})
#: ``[%T.%f] [%l]: %v``.
_LINEA: Final[re.Pattern[str]] = re.compile(r"^\[[^\]]*\] \[(?P<nivel>[a-z]+)\]: (?P<mensaje>.*)$")
#: Tope de líneas devueltas por categoría (se conservan las ÚLTIMAS: las más cercanas al fallo).
MAX_LINEAS_DEL_LOG: Final[int] = 20
#: Largo máximo de cada línea devuelta.
_MAX_LARGO_DE_LINEA: Final[int] = 300
#: Sólo se lee la cola del log: un log de debug puede pesar varios MB.
_MAX_BYTES_LEIDOS: Final[int] = 8 * 1024 * 1024
#: Tolerancia de reloj entre ``time.time()`` y el mtime del filesystem.
_TOLERANCIA_DE_MTIME_SEGUNDOS: Final[float] = 0.05


@dataclass(frozen=True, slots=True)
class LootLogFindings:
    """Errores del log de una corrida, ya saneados.

    Attributes:
        errores: líneas ``[error]``/``[critical]`` (incluye las fatales), las últimas
            :data:`MAX_LINEAS_DEL_LOG`.
        fatales: subconjunto con firma de :data:`LOOT_LOG_FATAL_SIGNATURES`.
        truncado: hubo más líneas (o más bytes) de los que se devuelven.
    """

    errores: tuple[str, ...]
    fatales: tuple[str, ...]
    truncado: bool

    def to_payload(self) -> dict[str, list[str] | bool]:
        """Forma JSON para la respuesta del servicio."""
        return {"errores": list(self.errores), "fatales": list(self.fatales), "truncado": self.truncado}


def _es_fatal(mensaje: str) -> bool:
    return any(mensaje.startswith(firma) for firma in LOOT_LOG_FATAL_SIGNATURES)


def read_loot_log_findings(
    loot_data_path: pathlib.Path,
    *,
    desde: float | None = None,
    max_lineas: int = MAX_LINEAS_DEL_LOG,
) -> LootLogFindings | None:
    """Lee los errores del log de la corrida que empezó en ``desde`` (epoch).

    Returns:
        ``None`` si no hay log de LOOT atribuible: ausente, ilegible, todavía el
        sentinel de Sky-Claw o anterior a ``desde``. Si no, los hallazgos (con
        tuplas vacías si la corrida no registró errores).
    """
    ruta = loot_data_path / LOOT_DEBUG_LOG_FILENAME
    try:
        with ruta.open("rb") as handle:
            estado = os.fstat(handle.fileno())
            if desde is not None and estado.st_mtime < desde - _TOLERANCIA_DE_MTIME_SEGUNDOS:
                return None
            if handle.read(len(LOOT_WITNESS_SENTINEL_PREFIX)) == LOOT_WITNESS_SENTINEL_PREFIX:
                return None
            inicio = max(0, estado.st_size - _MAX_BYTES_LEIDOS)
            handle.seek(inicio)
            datos = handle.read(_MAX_BYTES_LEIDOS)
    except OSError:
        return None
    lineas = datos.decode("utf-8", errors="replace").splitlines()
    if inicio > 0 and lineas:
        lineas = lineas[1:]  # la primera quedó cortada al leer sólo la cola
    errores: list[str] = []
    fatales: list[str] = []
    for linea in lineas:
        coincidencia = _LINEA.match(linea)
        if coincidencia is None or coincidencia["nivel"] not in _NIVELES_DE_ERROR:
            continue
        mensaje = coincidencia["mensaje"]
        citable = sanitize_for_prompt(f"[{coincidencia['nivel']}]: {mensaje}", max_length=_MAX_LARGO_DE_LINEA)
        errores.append(citable)
        if _es_fatal(mensaje):
            fatales.append(citable)
    truncado = inicio > 0 or len(errores) > max_lineas or len(fatales) > max_lineas
    return LootLogFindings(
        errores=tuple(errores[-max_lineas:]),
        fatales=tuple(fatales[-max_lineas:]),
        truncado=truncado,
    )

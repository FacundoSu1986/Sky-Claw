"""Testigo de ejecución de LOOT (PR-2): ¿ESTA invocación pasó el mutex y llegó al runtime?

Problema: ``LOOT.exe`` que sale con código 0 NO prueba que haya ordenado. Una
segunda instancia sale 0 sin hacer nada cuando otra ya posee el mutex
``LOOT.Shell.Instance``. Este módulo fabrica la evidencia que separa esa
no-ejecución del NO_CHANGE legítimo, sin adivinar por mtime.

Evidencia upstream — ``loot/loot`` tag ``0.29.1``, commit
``77f3ba98966819fd6d92d97dcb2dbc4c1b9fb9b9`` (verificada línea por línea; NO se
asume que 0.29.2+ sea idéntico):

* ``src/gui/application_mutex.h:44,60,63-67``: ``ApplicationMutexGuard`` crea el
  mutex con nombre ``L"LOOT.Shell.Instance"`` (``CreateMutex(nullptr, FALSE,
  ...)``) e ``isApplicationMutexLocked()`` sólo prueba su existencia con
  ``OpenMutex``. Fuera de Windows, ``isApplicationMutexLocked()`` es ``false``.
* ``src/gui/qt/main.cpp:90-95``: lo PRIMERO que hace ``main`` es consultar el
  mutex; si existe, ``FindWindow``/``SetForegroundWindow`` y ``return 0``. Eso
  ocurre ANTES de ``ApplicationMutexGuard`` (l.98), ``QApplication`` (l.112),
  ``QCommandLineParser`` (l.114-126), ``LootState`` (l.135),
  ``logRuntimeEnvironment`` (l.137) y ``state.init`` (l.139). La segunda
  instancia no parsea ``--loot-data-path``, no inicializa logging y no toca
  NINGÚN archivo del data root.
* ``src/gui/state/loot_state.cpp:94-114`` (constructor de ``LootState``):
  ``createLootDataPath()``, luego ``fs::remove(paths_.getLogPath())`` y
  ``setLogPath(paths_.getLogPath())``. Toda instancia que supera el mutex BORRA
  el log y crea uno NUEVO; nunca anexa al anterior.
* ``src/gui/state/loot_paths.cpp:70-72``: ``getLogPath()`` es
  ``<loot-data-path>/LOOTDebugLog.txt`` (con el ``--loot-data-path`` aislado de
  PR-1, el log es propiedad exclusiva de Sky-Claw).
* ``src/gui/state/logging.cpp:140-159``: ``setLogPath`` instala un
  ``basic_file_sink`` de spdlog (crea el archivo) con ``flush_on(trace)``.
* ``src/gui/qt/main.cpp:59-82,137``: ``logRuntimeEnvironment()`` escribe líneas
  ``info`` inmediatamente después del constructor. Un proceso sólo sale 0 por
  ``app.exec()`` (l.218), así que toda corrida que salió 0 habiendo superado el
  mutex dejó el log NUEVO y NO vacío.

Contrato (contenido controlado, no mtime):

1. :func:`arm_execution_witness` — justo ANTES de ``create_subprocess_exec``,
   reemplaza atómicamente ``LOOTDebugLog.txt`` por un sentinel con un nonce
   aleatorio de 128 bits.
2. :meth:`ArmedExecutionWitness.observe` — justo DESPUÉS de ``communicate()``,
   relee el comienzo del archivo:

   * ``FRESH``: existe, no está vacío y no empieza con el sentinel → un proceso
     LOOT con ESTE data root ejecutó el constructor de ``LootState`` durante la
     ventana de observación. Es el único estado positivo.
   * ``SENTINEL_INTACT``: bytes idénticos al sentinel → ningún runtime de LOOT
     se inicializó con este data root. Es compatible con la segunda instancia
     por mutex, pero NO la prueba: sólo prueba ausencia de runtime.
   * ``SENTINEL_ALTERED`` / ``EMPTY`` / ``ABSENT``: formas que el ciclo de vida
     upstream no produce (anexar sin recrear, recrear vacío, borrar sin
     recrear) → anomalía, nunca positivo.
   * ``UNREADABLE``: sin evidencia.

Atribución — qué prueba y qué NO: ``FRESH`` prueba que ALGÚN proceso LOOT
lanzado con este ``--loot-data-path`` alcanzó el runtime entre ``arm`` y
``observe``. Que ese proceso sea el NUESTRO descansa en la exclusividad del
data root (PR-1: propiedad de Sky-Claw por instancia+perfil, nunca el default
del GUI) y en que los lanzamientos se serializan bajo el lock
``LOAD_ORDER_RESOURCE_ID`` — la MISMA precondición que ya sostiene el
snapshot/rollback del servicio. ``FRESH`` tampoco prueba que el sort haya
terminado bien: eso lo deciden el código de salida, el parser y el estado del
load order observado (:mod:`sky_claw.local.loot.outcome`).

Lo que este módulo deliberadamente NO hace: parsear mensajes humanos del log.
``"Sorting operation complete."`` se loguea INCONDICIONALMENTE al final de
``SortPluginsQuery::executeLogic`` (``src/gui/query/types/sort_plugins_query.h``),
también cuando ``Game::sortPlugins()`` devolvió la lista vacía de un fallo de
sort (``src/gui/state/game/game.cpp:869-892``): es un marcador de finalización
de la operación, no de éxito. El testigo es de ciclo de vida, no de contenido.
"""

from __future__ import annotations

import contextlib
import enum
import os
import pathlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: Nombre exacto del log bajo ``--loot-data-path`` (``loot_paths.cpp:70-72``).
LOOT_DEBUG_LOG_FILENAME: Final[str] = "LOOTDebugLog.txt"

#: Versión del schema serializado que cruza la frontera worker → daemon.
LOOT_EXECUTION_WITNESS_SCHEMA: Final[int] = 1

#: Claves EXACTAS del payload serializado (schema cerrado: campo de más o de
#: menos ⇒ payload inválido, nunca un testigo "parcialmente" aceptado).
_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset({"schema", "state"})

#: Sentinel autoexplicativo (ASCII puro): si un operador abre el archivo tras
#: una corrida que no llegó al runtime, el texto le dice qué pasó.
_SENTINEL_TEMPLATE: Final[str] = (
    "# Sky-Claw LOOT execution witness {nonce}\n"
    "# LOOT borra y recrea este archivo al inicializar su runtime\n"
    "# (loot/loot 0.29.1 src/gui/state/loot_state.cpp:105-106). Si este texto\n"
    "# sigue intacto tras una corrida, esa corrida NO alcanzo el runtime de LOOT.\n"
)


class LootExecutionWitnessState(enum.StrEnum):
    """Estado observado del log tras la corrida. Sólo ``FRESH`` es positivo."""

    FRESH = "fresh"
    SENTINEL_INTACT = "sentinel_intact"
    SENTINEL_ALTERED = "sentinel_altered"
    EMPTY = "empty"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


class LootExecutionWitnessError(RuntimeError):
    """No se pudo armar el testigo: LOOT NO debe lanzarse (fail-closed).

    Caso esperable en Windows: un LOOT huérfano con este data root mantiene
    ``LOOTDebugLog.txt`` abierto sin ``FILE_SHARE_DELETE`` (spdlog abre con
    ``_SH_DENYNO``) y el ``os.replace`` del sentinel falla. Lanzar igual sería
    correr una ejecución que no se podría atribuir.
    """


class LootExecutionWitnessPayloadError(ValueError):
    """El payload serializado del testigo no cumple el schema cerrado v1."""


@dataclass(frozen=True, slots=True)
class LootExecutionWitness:
    """Evidencia estructurada y serializable del ciclo de vida del log."""

    state: LootExecutionWitnessState

    @property
    def fresh(self) -> bool:
        """``True`` sólo si LOOT recreó el log (única señal de atribución)."""
        return self.state is LootExecutionWitnessState.FRESH

    def to_payload(self) -> dict[str, str | int]:
        """Serialización JSON explícita para la frontera worker → daemon."""
        return {"schema": LOOT_EXECUTION_WITNESS_SCHEMA, "state": self.state.value}

    @classmethod
    def from_payload(cls, raw: object) -> LootExecutionWitness:
        """Valida el payload con schema CERRADO; nunca acepta un dict arbitrario.

        Raises:
            LootExecutionWitnessPayloadError: tipo, claves, schema o estado
                inválidos.
        """
        if not isinstance(raw, Mapping):
            raise LootExecutionWitnessPayloadError("execution_witness debe ser un objeto JSON")
        if any(not isinstance(key, str) for key in raw):
            raise LootExecutionWitnessPayloadError("execution_witness sólo admite claves string")
        keys = set(raw)
        if keys != _PAYLOAD_KEYS:
            raise LootExecutionWitnessPayloadError(
                f"execution_witness debe tener exactamente {sorted(_PAYLOAD_KEYS)}; recibió {sorted(keys)}"
            )
        schema = raw["schema"]
        if type(schema) is not int or schema != LOOT_EXECUTION_WITNESS_SCHEMA:
            raise LootExecutionWitnessPayloadError(f"execution_witness.schema no soportado: {schema!r}")
        state = raw["state"]
        if not isinstance(state, str):
            raise LootExecutionWitnessPayloadError("execution_witness.state debe ser string")
        try:
            parsed = LootExecutionWitnessState(state)
        except ValueError:
            raise LootExecutionWitnessPayloadError(f"execution_witness.state desconocido: {state!r}") from None
        return cls(state=parsed)


@dataclass(frozen=True, slots=True)
class ArmedExecutionWitness:
    """Sentinel plantado para UNA invocación; ``observe()`` lo relee tras salir."""

    log_path: pathlib.Path
    sentinel: bytes

    def observe(self) -> LootExecutionWitness:
        """Clasifica el log tras la salida del proceso (ver docstring del módulo).

        Lee sólo ``len(sentinel) + 1`` bytes: alcanza para distinguir sentinel
        intacto, sentinel con bytes anexados y archivo recreado, sin cargar un
        log de varios MB.
        """
        try:
            with self.log_path.open("rb") as handle:
                head = handle.read(len(self.sentinel) + 1)
        except FileNotFoundError:
            return LootExecutionWitness(state=LootExecutionWitnessState.ABSENT)
        except OSError:
            return LootExecutionWitness(state=LootExecutionWitnessState.UNREADABLE)
        if head == self.sentinel:
            return LootExecutionWitness(state=LootExecutionWitnessState.SENTINEL_INTACT)
        if head.startswith(self.sentinel):
            return LootExecutionWitness(state=LootExecutionWitnessState.SENTINEL_ALTERED)
        if not head:
            return LootExecutionWitness(state=LootExecutionWitnessState.EMPTY)
        return LootExecutionWitness(state=LootExecutionWitnessState.FRESH)


def arm_execution_witness(loot_data_path: pathlib.Path) -> ArmedExecutionWitness:
    """Planta el sentinel de ESTA invocación en ``<loot_data_path>/LOOTDebugLog.txt``.

    Reemplazo atómico (temporal exclusivo + ``os.replace`` en el mismo
    directorio): el archivo queda con el sentinel exacto o no se toca. El log
    previo se descarta — LOOT lo borraría igual al inicializar
    (``loot_state.cpp:105``); sólo se pierde antes en el caso sin runtime, donde
    ese log (de la corrida ANTERIOR) no explica nada de la actual.

    Raises:
        LootExecutionWitnessError: el sentinel no se pudo plantar; el caller
            NO debe lanzar LOOT.
    """
    log_path = loot_data_path / LOOT_DEBUG_LOG_FILENAME
    nonce = secrets.token_hex(16)
    sentinel = _SENTINEL_TEMPLATE.format(nonce=nonce).encode("ascii")
    temporal = log_path.with_name(f"{log_path.name}.skyclaw-{nonce}.tmp")
    try:
        with temporal.open("xb") as handle:
            handle.write(sentinel)
        os.replace(temporal, log_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporal.unlink()
        raise LootExecutionWitnessError(
            f"No se pudo armar el testigo de ejecución de LOOT en {log_path}: {exc}. "
            "LOOT no se lanzó: sin testigo, una salida 0 no sería atribuible (¿otro "
            "LOOT vivo con este data root mantiene el log abierto?)."
        ) from exc
    return ArmedExecutionWitness(log_path=log_path, sentinel=sentinel)

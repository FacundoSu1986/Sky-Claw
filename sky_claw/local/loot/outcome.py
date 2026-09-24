"""Contrato de resultado verificable del sort de LOOT (PR-2): CHANGED / NO_CHANGE / FAIL.

Dos niveles de "éxito" que NO se mezclan:

* **Éxito de proceso/parser** — :attr:`sky_claw.local.loot.parser.LOOTResult.success`:
  ``return_code == 0`` y sin errores parseados. Necesario, nunca suficiente: una
  segunda instancia bloqueada por el mutex ``LOOT.Shell.Instance`` también sale 0
  (``loot/loot`` 0.29.1 ``src/gui/qt/main.cpp:90-95``).
* **Veredicto verificado** — :func:`classify_loot_sort`, que además exige
  atribución (testigo de ejecución fresco, :mod:`.execution_witness`) y estado
  del load order observable antes y después.

Por qué "ningún archivo cambió" NO es fallo (el bug que PR-2 corrige): en 0.29.1
el apply sólo ocurre si el orden cambió. ``handlePluginsSorted``
(``src/gui/qt/main_window.cpp:1596-1617``) llama ``enterSortingState()`` — que
hace visible ``actionApplySort`` (l.1040-1051) — SÓLO cuando
``hasLoadOrderChanged`` (l.124-137) es verdadero; ``handlePluginsAutoSorted``
(l.2870-2884) dispara ``actionApplySort`` sólo si es visible. El apply
(``ApplySortQuery`` → ``Game::setLoadOrder``, ``src/gui/state/game/game.cpp:788-791``)
es el ÚNICO lugar de ese flujo que hace backup y ``SetLoadOrder``. En NO_CHANGE
``plugins.txt``/``loadorder.txt`` no se reescriben y no hay backup, y el proceso
sale 0 por el quit normal.

Estado semántico del load order (comparación antes/después): se replica la
lectura de libloadorder 18.8.1 (backend de libloot 0.29.4, que es el que usa LOOT
0.29.1; SkyrimSE es ``LoadOrderMethod::Asterisk``, ``src/game_settings.rs:193-197``):

* ``src/load_order/mutable.rs:328-332``: decodifica Windows-1252 SIN manejo de
  BOM y separa con ``str::lines()`` (``\\n`` o ``\\r\\n``). Por eso se compara en
  bytes (Windows-1252 es biyectivo) y un BOM NO se ignora: libloadorder lo leería
  como parte del primer nombre — no es demostrablemente irrelevante.
* ``src/load_order/asterisk_based.rs:314-322`` y ``mutable.rs:335-341``: se
  descartan las líneas vacías y las que empiezan con ``#``; un ``*`` inicial
  marca el plugin como activo. La activación SE PRESERVA: quitarla escondería un
  cambio de activación.
* Sin ``strip`` ni case-folding (libloadorder no hace ``trim``): una diferencia
  sólo de mayúsculas o espacios cuenta como cambio. Es conservador y seguro:
  CHANGED y NO_CHANGE son ambos éxito; la etiqueta nunca decide éxito vs fallo.

Matriz canónica (el orden de evaluación es parte del contrato):

====  ==================================================  ===========================
Caso  Condición                                           Veredicto
====  ==================================================  ===========================
C     proceso/parser falló (rc != 0 o errores)            FAIL / PROCESS_ERROR
E     estado antes o después inobservable                 FAIL / STATE_UNOBSERVABLE
D     rc 0, testigo NO fresco, sin cambio semántico       FAIL / EXECUTION_NOT_ATTRIBUTABLE
F     rc 0, testigo NO fresco, CON cambio semántico       FAIL / EXECUTION_NOT_ATTRIBUTABLE
A     testigo fresco, observable, semántica distinta      CHANGED (éxito)
B     testigo fresco, observable, semántica igual         NO_CHANGE (éxito)
====  ==================================================  ===========================

TIMEOUT y PRECONDITION_FAILED no pasan por el clasificador: se deciden por
excepción antes de que exista un resultado de proceso (ver ``loot_service``).
``LOOTResult.sorted_plugins`` NO participa: con LOOT GUI real llega vacío.
"""

from __future__ import annotations

import enum
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeAlias

from sky_claw.local.loot.execution_witness import LootExecutionWitness, LootExecutionWitnessState


class LootSortOutcome(enum.StrEnum):
    """Veredicto enumerable del sort. ``success`` legacy = ``outcome is not FAIL``."""

    CHANGED = "changed"
    NO_CHANGE = "no_change"
    FAIL = "fail"


class LootSortFailureReason(enum.StrEnum):
    """Razones tipadas de FAIL. Sólo las que el sistema produce hoy."""

    PRECONDITION_FAILED = "precondition_failed"
    PROCESS_ERROR = "process_error"
    TIMEOUT = "timeout"
    EXECUTION_NOT_ATTRIBUTABLE = "execution_not_attributable"
    STATE_UNOBSERVABLE = "state_unobservable"


@dataclass(frozen=True, slots=True)
class LootSortVerdict:
    """Veredicto final: outcome + razón tipada (sólo en FAIL) + diagnóstico humano."""

    outcome: LootSortOutcome
    failure_reason: LootSortFailureReason | None
    detail: str

    def __post_init__(self) -> None:
        # Invariante del contrato: FAIL ⇔ hay razón. Un FAIL sin razón o un
        # éxito con razón serían respuestas ambiguas para el caller.
        if (self.outcome is LootSortOutcome.FAIL) != (self.failure_reason is not None):
            raise ValueError("LootSortVerdict: failure_reason es obligatorio en FAIL y prohibido en éxito")

    @property
    def success(self) -> bool:
        """Compatibilidad externa: CHANGED y NO_CHANGE son éxito; FAIL no."""
        return self.outcome is not LootSortOutcome.FAIL

    @classmethod
    def fail(cls, reason: LootSortFailureReason, detail: str) -> LootSortVerdict:
        return cls(outcome=LootSortOutcome.FAIL, failure_reason=reason, detail=detail)


#: Una entrada del load order: nombre en bytes crudos (Windows-1252) + activo.
LoadOrderEntry: TypeAlias = tuple[bytes, bool]
#: Estado de un archivo: ``None`` = ausente; tupla = entradas en orden.
LoadOrderFileState: TypeAlias = tuple[LoadOrderEntry, ...] | None
#: Estado de todos los targets, ordenado por ruta (igualdad determinista).
LoadOrderSemanticState: TypeAlias = tuple[tuple[str, LoadOrderFileState], ...]


class LoadOrderUnobservableError(OSError):
    """Un target existe (o podría existir) pero no se pudo leer: sin evidencia."""

    def __init__(self, path: pathlib.Path, cause: OSError) -> None:
        super().__init__(f"No se pudo leer {path}: {cause}")
        self.path = path


def parse_load_order_bytes(data: bytes) -> tuple[LoadOrderEntry, ...]:
    """Entradas semánticas de un ``plugins.txt``/``loadorder.txt`` (ver docstring).

    Ignora sólo lo que libloadorder ignora: fin de línea ``\\r\\n`` vs ``\\n``,
    líneas vacías y comentarios ``#``. Conserva el ``*`` como activación.
    """
    entries: list[LoadOrderEntry] = []
    for raw in data.split(b"\n"):
        line = raw[:-1] if raw.endswith(b"\r") else raw
        if not line or line.startswith(b"#"):
            continue
        if line.startswith(b"*"):
            entries.append((line[1:], True))
        else:
            entries.append((line, False))
    return tuple(entries)


def read_load_order_semantics(paths: Iterable[pathlib.Path]) -> LoadOrderSemanticState:
    """Lee el estado semántico de TODOS los targets.

    Raises:
        LoadOrderUnobservableError: algún target no se pudo leer por una causa
            distinta de "no existe" (permisos, IO, es un directorio…). Nunca se
            deduce "no cambió" de una lectura fallida.
    """
    state: list[tuple[str, LoadOrderFileState]] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            state.append((str(path), None))
            continue
        except OSError as exc:
            raise LoadOrderUnobservableError(path, exc) from exc
        state.append((str(path), parse_load_order_bytes(data)))
    return tuple(sorted(state, key=lambda item: item[0]))


def classify_loot_sort(
    *,
    process_success: bool,
    process_detail: str,
    witness: LootExecutionWitness | None,
    before: LoadOrderSemanticState | None,
    after: LoadOrderSemanticState | None,
) -> LootSortVerdict:
    """Aplica la matriz canónica A-F (ver docstring del módulo). Función pura.

    Args:
        process_success: ``LOOTResult.success`` (rc 0 y sin errores parseados).
        process_detail: diagnóstico del proceso para el caso C.
        witness: testigo transportado desde ``LOOTRunner``; ``None`` = el runner
            no produjo testigo (sin data root aislado o resultado sin schema).
        before / after: estado semántico observado bajo el lock; ``None`` =
            inobservable.
    """
    if not process_success:
        return LootSortVerdict.fail(LootSortFailureReason.PROCESS_ERROR, process_detail)
    if before is None or after is None:
        momento = "antes de correr LOOT" if before is None else "tras la corrida"
        return LootSortVerdict.fail(
            LootSortFailureReason.STATE_UNOBSERVABLE,
            f"No se pudo inspeccionar el estado del load order {momento}: sin evidencia "
            "verificable no se puede confirmar qué hizo LOOT.",
        )
    cambio = before != after
    if witness is None or not witness.fresh:
        estado = "sin testigo" if witness is None else f"testigo {witness.state.value}"
        if cambio:
            detalle = (
                f"LOOT salió con código 0 pero la ejecución no es atribuible ({estado}) y el load "
                "order CAMBIÓ durante la ventana: mutación no atribuible a esta corrida (¿otro "
                "escritor externo?). No se reporta como resultado de LOOT."
            )
        else:
            detalle = (
                f"LOOT salió con código 0 pero la ejecución no es atribuible ({estado}): ningún "
                "runtime de LOOT se inicializó con este data root. Diagnóstico posible (no "
                "probado): otra instancia de LOOT ya abierta posee el mutex global "
                "LOOT.Shell.Instance y esta corrida salió sin ordenar."
            )
            if witness is not None and witness.state is not LootExecutionWitnessState.SENTINEL_INTACT:
                detalle += " El log de LOOT quedó en un estado que su ciclo de vida no produce (anomalía)."
        return LootSortVerdict.fail(LootSortFailureReason.EXECUTION_NOT_ATTRIBUTABLE, detalle)
    if cambio:
        return LootSortVerdict(
            outcome=LootSortOutcome.CHANGED,
            failure_reason=None,
            detail="LOOT se ejecutó (testigo fresco) y aplicó un load order distinto.",
        )
    return LootSortVerdict(
        outcome=LootSortOutcome.NO_CHANGE,
        failure_reason=None,
        detail=(
            "LOOT se ejecutó (testigo fresco), salió con código 0 y el load order observado quedó "
            "semánticamente idéntico: ya estaba ordenado, sin cambios que aplicar."
        ),
    )

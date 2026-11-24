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
  BOM y separa con ``str::lines()``. El decoder (``encoding_rs::WINDOWS_1252``,
  ``decode_without_bom_handling_and_without_replacement``) es TOTAL e INYECTIVO:
  los 256 bytes mapean a code points distintos (los 5 "sin uso" van a controles
  C1, comentario de ``mutable.rs:326-327``) y ``\\n``/``\\r``/``#``/``*`` son ASCII
  que sólo se mapean a sí mismos. Por eso comparar bytes ⇔ comparar el texto que
  lee libloadorder, también con nombres UTF-8 o emoji (mojibake determinista,
  nunca error): se compara en bytes y un BOM NO se ignora — libloadorder lo
  leería como parte del primer nombre.
* ``str::lines()`` es ``split_inclusive('\\n')`` + ``LinesMap`` (Rust 1.82,
  ``library/core/src/str/mod.rs``; libloadorder 18.8.1 exige
  ``rust-version = "1.82"``): quita ``\\n`` y, SÓLO si lo había, un ``\\r``. Una
  última línea sin ``\\n`` conserva su ``\\r`` final.
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
G     testigo fresco, plugins HABILITADOS distintos       FAIL / ACTIVE_PLUGINS_CHANGED
H     testigo fresco, firma fatal en el log de la corrida FAIL / PROCESS_ERROR
A     testigo fresco, observable, semántica distinta      CHANGED (éxito)
B     testigo fresco, observable, semántica igual         NO_CHANGE (éxito)
====  ==================================================  ===========================

Caso G (C1 — "LOOT reordena; nunca activa ni desactiva"): ``--auto-sort`` aplica
un ORDEN; el conjunto de plugins habilitados del perfil no es suyo. Pero
libloadorder 18.8.1 descarta al cargar los plugins de ``plugins.txt`` que no ve
instalados (``src/load_order/asterisk_based.rs``, test
``load_should_ignore_plugins_in_active_plugins_file_that_are_not_installed``) y
el ``save`` posterior los borra: con visibilidad USVFS parcial, un sort "exitoso"
desactiva mods en silencio. La activación se compara con la semántica canónica de
MO2 (:func:`read_active_plugins_state` → ``vfs_attestation.read_enabled_plugins``),
que excluye los plugins de carga temprana — libloadorder los omite al escribir
(``save_should_omit_early_loading_plugins_from_active_plugins_file``), así que
quitar su línea no es un cambio de activación. La observabilidad de la
activación sigue la regla E: observada a medias ⇒ ``STATE_UNOBSERVABLE``.

Caso H (A4, U-06 b): con salida 0 y testigo fresco, una firma fatal en el
``LOOTDebugLog.txt`` de ESTA corrida (:mod:`.debug_log`) es una contradicción —
esos caminos upstream terminan en un modal o cancelan el auto-sort— y falla
cerrado. Los demás ``[error]`` son diagnóstico, no veredicto.

TIMEOUT, PRECONDITION_FAILED y PROTOCOL_ERROR no pasan por el clasificador:
se deciden por excepción antes de que exista un resultado de proceso confiable
(ver ``loot_service``). PROTOCOL_ERROR es el resultado del worker USVFS que
viola el contrato cerrado de transporte (``LOOTWorkerProtocolError``): ningún
campo suyo se usa como evidencia. ``LOOTResult.sorted_plugins`` NO participa:
con LOOT GUI real llega vacío.
"""

from __future__ import annotations

import enum
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, TypeAlias

from sky_claw.app.security.sanitize import sanitize_for_prompt
from sky_claw.local.loot.execution_witness import LootExecutionWitness, LootExecutionWitnessState
from sky_claw.local.mo2.vfs_attestation import VfsAttestationError, read_enabled_plugins


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
    PROTOCOL_ERROR = "protocol_error"
    #: C1: el sort cambió el conjunto de plugins habilitados (sólo debe cambiar el orden).
    ACTIVE_PLUGINS_CHANGED = "active_plugins_changed"


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
#: Plugins habilitados por cada ``plugins.txt`` observado (claves case-insensitive
#: canónicas de MO2), ordenado por ruta. Ver caso G del docstring del módulo.
ActivePluginsState: TypeAlias = tuple[tuple[str, frozenset[str]], ...]

#: Nombre del archivo de activación del dialecto asterisk (SSE/AE).
_ARCHIVO_DE_ACTIVACION: Final[str] = "plugins.txt"
#: Tope de nombres citados por dirección en el diagnóstico del caso G.
_MAX_NOMBRES_EN_DETALLE: Final[int] = 20
#: Largo máximo de cada nombre citado (los nombres vienen de un archivo del perfil).
_MAX_LARGO_DE_NOMBRE: Final[int] = 256


class LoadOrderUnobservableError(OSError):
    """Un target existe (o podría existir) pero no se pudo leer: sin evidencia."""

    def __init__(self, path: pathlib.Path, cause: Exception) -> None:
        super().__init__(f"No se pudo leer {path}: {cause}")
        self.path = path


def parse_load_order_bytes(data: bytes) -> tuple[LoadOrderEntry, ...]:
    """Entradas semánticas de un ``plugins.txt``/``loadorder.txt`` (ver docstring).

    Ignora sólo lo que libloadorder ignora: fin de línea ``\\r\\n`` vs ``\\n``,
    líneas vacías y comentarios ``#``. Conserva el ``*`` como activación y el
    ``\\r`` final de una última línea sin ``\\n`` (semántica exacta de
    ``str::lines()``).
    """
    entries: list[LoadOrderEntry] = []
    piezas = data.split(b"\n")
    for indice, raw in enumerate(piezas):
        terminada = indice < len(piezas) - 1
        line = raw[:-1] if terminada and raw.endswith(b"\r") else raw
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


def read_active_plugins_state(
    paths: Iterable[pathlib.Path],
    *,
    game_data_dir: pathlib.Path | None,
) -> ActivePluginsState:
    """Plugins habilitados de cada ``plugins.txt`` entre ``paths`` (caso G).

    Reusa la lectura canónica de MO2 de la atestación VFS
    (``vfs_attestation.read_enabled_plugins``): identidad case-insensitive,
    líneas sin estrella inertes y los primary plugins (masters base + Creation
    Club declarado en ``Skyrim.ccc`` del game root) siempre presentes. Los demás
    targets (``loadorder.txt``) no llevan activación y se ignoran. Un archivo
    ausente es "sin plugins habilitados no primarios", igual que para MO2.

    Args:
        paths: targets del sort (snapshot/rollback).
        game_data_dir: ``Data`` del juego; ``None`` = sólo masters base
            (conservador: un CC listado con estrella cuenta como estado).

    Raises:
        LoadOrderUnobservableError: un ``plugins.txt`` existe pero no se pudo
            leer o es ambiguo (nombre repetido, entrada inválida). Nunca se
            deduce "no cambió" de una lectura fallida.
    """
    state: list[tuple[str, frozenset[str]]] = []
    for path in paths:
        if path.name.lower() != _ARCHIVO_DE_ACTIVACION:
            continue
        try:
            habilitados = read_enabled_plugins(path, game_data_dir=game_data_dir)
        except VfsAttestationError as exc:
            raise LoadOrderUnobservableError(path, exc) from exc
        state.append((str(path), frozenset(habilitados)))
    return tuple(sorted(state, key=lambda item: item[0]))


def _nombres_citables(nombres: Iterable[str]) -> str:
    """Nombres de plugin saneados y acotados para el diagnóstico (llegan al LLM y a la GUI)."""
    ordenados = sorted(nombres)
    citados = [sanitize_for_prompt(nombre, max_length=_MAX_LARGO_DE_NOMBRE) for nombre in ordenados]
    texto = ", ".join(citados[:_MAX_NOMBRES_EN_DETALLE])
    if len(citados) > _MAX_NOMBRES_EN_DETALLE:
        texto += f" (+{len(citados) - _MAX_NOMBRES_EN_DETALLE} más)"
    return texto


def describe_active_plugins_change(before: ActivePluginsState, after: ActivePluginsState) -> str:
    """Diagnóstico del caso G: qué plugins dejaron de estar (o pasaron a estar) habilitados."""
    antes = dict(before)
    despues = dict(after)
    perdidos: set[str] = set()
    agregados: set[str] = set()
    for ruta in antes.keys() | despues.keys():
        previo = antes.get(ruta, frozenset())
        posterior = despues.get(ruta, frozenset())
        perdidos |= previo - posterior
        agregados |= posterior - previo
    partes = [
        "LOOT se ejecutó (testigo fresco) pero cambió el conjunto de plugins HABILITADOS: un sort "
        "sólo reordena. Causa típica: LOOT no vio esos plugins en el Data (visibilidad USVFS "
        "parcial o mod no instalado) y libloadorder los descartó al reescribir plugins.txt."
    ]
    if perdidos:
        partes.append(f"Dejaron de estar habilitados: {_nombres_citables(perdidos)}.")
    if agregados:
        partes.append(f"Pasaron a estar habilitados: {_nombres_citables(agregados)}.")
    partes.append("El load order se restaura desde el snapshot.")
    return " ".join(partes)


def classify_loot_sort(
    *,
    process_success: bool,
    process_detail: str,
    witness: LootExecutionWitness | None,
    before: LoadOrderSemanticState | None,
    after: LoadOrderSemanticState | None,
    active_before: ActivePluginsState | None = None,
    active_after: ActivePluginsState | None = None,
    log_fatal: tuple[str, ...] = (),
) -> LootSortVerdict:
    """Aplica la matriz canónica (ver docstring del módulo). Función pura.

    Args:
        process_success: ``LOOTResult.success`` (rc 0 y sin errores parseados).
        process_detail: diagnóstico del proceso para el caso C.
        witness: testigo transportado desde ``LOOTRunner``; ``None`` = el runner
            no produjo testigo (sin data root aislado o resultado sin schema).
        before / after: estado semántico observado bajo el lock; ``None`` =
            inobservable.
        active_before / active_after: plugins habilitados (caso G). Los dos
            ``None`` = activación no evaluada (sólo callers sin estado; el
            servicio SIEMPRE los pasa — ancla en
            ``tests/test_loot_integridad_de_activos.py``). Uno solo ``None`` =
            activación inobservable ⇒ ``STATE_UNOBSERVABLE``.
        log_fatal: líneas con firma fatal del log de ESTA corrida (caso H),
            ya saneadas por :func:`sky_claw.local.loot.debug_log.read_loot_log_findings`.
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
    if (active_before is None) != (active_after is None):
        momento = "antes de correr LOOT" if active_before is None else "tras la corrida"
        return LootSortVerdict.fail(
            LootSortFailureReason.STATE_UNOBSERVABLE,
            f"No se pudo inspeccionar qué plugins estaban habilitados {momento}: sin esa "
            "evidencia no se puede confirmar que el sort sólo reordenó.",
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
    if active_before is not None and active_after is not None and active_before != active_after:
        return LootSortVerdict.fail(
            LootSortFailureReason.ACTIVE_PLUGINS_CHANGED,
            describe_active_plugins_change(active_before, active_after),
        )
    if log_fatal:
        return LootSortVerdict.fail(
            LootSortFailureReason.PROCESS_ERROR,
            "LOOT salió con código 0 y testigo fresco, pero el log de esta corrida registra un "
            "error fatal (esos caminos terminan en un diálogo o cancelan el auto-sort: el "
            "resultado no es confiable): " + " | ".join(log_fatal[:3]),
        )
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

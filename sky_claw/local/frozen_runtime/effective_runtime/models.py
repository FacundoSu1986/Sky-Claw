"""Modelos del oráculo de Effective Runtime — P5-A (ADR 0012 §13/§29.4, Q12).

Vocabulario ÚNICO del oráculo, alineado con lo que el consumidor de P4 ya
espera: ``journal.reconciliar_journal(observador_effective=...)`` recibe una
``ObservacionEffective`` (``generation_id``/``clone_id``/``effective_path``).
Este paquete es el **productor** que faltaba; no define un segundo vocabulario.

Tres decisiones de modelado que no son cosméticas:

1. **``EvidenceSource`` no es el censo de superficies de binding.** El ADR §29.4
   enumera 6 superficies *configuradas/derivadas* + 3 *consumidores con cache en
   memoria* porque eso es lo que un **binder** debe mover como una unidad. El
   oráculo resuelve un problema distinto —**observar**— y su familia es la de
   *evidencias legibles*. Confundirlas produciría un ancla que cuenta lo que no
   debe (y un binding que cree cubrir algo por tener el número).
2. **No hay fuente de evidencia para la Managed Source.** El árbol de Steam no
   es una ruta efectiva posible (SFR-05/SFR-19): se usa sólo como *referencia
   negativa* para rechazar un match, nunca como origen de la ruta. Un miembro
   con "STEAM" en el nombre reabriría esa puerta.
3. **``authorizes_activation`` es una propiedad del TIPO, no del caso.** Que
   ``UNKNOWN`` nunca autorice no se logra con un ``if`` en el llamador —el
   defecto que este repo cerró cuatro veces— sino haciendo que la única
   expresión que devuelve ``True`` exija ``MATCH`` **y** ``COMPATIBLE``.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Callable
from enum import StrEnum

from sky_claw.local.runtime_vault.models import RuntimeIdentity


class EffectiveRuntimeOracleError(Exception):
    """Error de programación del oráculo (nunca un desenlace de observación).

    Los desenlaces de observación —no poder leer, no haber evidencia, haber
    evidencia contradictoria— NO se lanzan: se representan con
    ``ObservationState``/``EffectiveRuntimeVerdict``. Lanzar por ellos obligaría
    a cada llamador a traducir excepciones a ``UNKNOWN``, y el que se olvide
    convierte un fallo cerrado en un crash (o peor, en un ``except`` genérico).
    """


class EvidenceSource(StrEnum):
    """Familia de evidencias que el oráculo puede LEER (read-only).

    Cerrada a propósito y anclada por igualdad literal en los tests: agregar un
    miembro sin cablearlo a ``assess_effective_runtime`` rompe el ancla.
    """

    #: ``local_cfg.skyrim_path`` — TOML de Sky-Claw. Es lo que Sky-Claw cree,
    #: NO lo que MO2 usa. Clase 1 del censo §29.4.
    CONFIG_SKYRIM_PATH = "config_skyrim_path"
    #: ``SKYRIM_PATH`` en el entorno del proceso. Derivada en el arranque por
    #: ``_bootloader._hydrate_tool_env_from_snapshot`` (``setdefault``).
    ENV_SKYRIM_PATH = "env_skyrim_path"
    #: ``[Settings] gamePath`` de ``modorganizer.ini``, serializado como
    #: ``@ByteArray`` de Qt. Es la superficie que MO2 resuelve al arrancar.
    MO2_GAME_PATH = "mo2_game_path"
    #: ``[customExecutables] N\\binary``: ejecutables REGISTRADOS en la
    #: instancia. Registrar no es seleccionar (auditoría B3/Oracle): un
    #: lanzador del juego registrado fuera de la raíz efectiva deja la
    #: selección sin prueba (``EXECUTABLE_SELECTION_UNPROVEN``), pero jamás
    #: se usa como fuente de ruta efectiva — inventar la selección sería
    #: aceptar una ruta declarada como prueba. Lo que MO2 lanza de verdad lo
    #: prueba únicamente un proceso vivo (``LIVE_GAME_PROCESS``).
    MO2_EXECUTABLE = "mo2_executable"
    #: Imagen de un ``SkyrimSE.exe`` VIVO: la única evidencia de qué se está
    #: ejecutando *ahora*. Gana sobre cualquier valor persistido.
    LIVE_GAME_PROCESS = "live_game_process"
    #: Imagen de un ``ModOrganizer.exe`` VIVO: no prueba la ruta del juego, pero
    #: su hora de arranque decide si el ``gamePath`` del ini sigue vigente en
    #: memoria (``started_at < mtime(ini)`` ⇒ el ini pudo cambiar después).
    LIVE_MO2_PROCESS = "live_mo2_process"


class ObservationState(StrEnum):
    """Desenlace de leer UNA superficie. ``ABSENT`` no es ``UNREADABLE``.

    La distinción es la misma que ``state.py`` hace para ``active.json``: "no
    hay valor" y "hay un valor que no pude interpretar" producen veredictos
    distintos. Colapsarlas convierte un ``@ByteArray`` truncado en "no
    configurado" y el oráculo declararía ``UNKNOWN`` por la razón equivocada.
    """

    OBSERVED = "observed"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


class RuntimeCompatibility(StrEnum):
    """Gate ``RUNTIME_COMPATIBILITY`` (F6). ``UNKNOWN != COMPATIBLE``."""

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class EffectiveRuntimeVerdict(StrEnum):
    """Veredicto del oráculo. Sólo ``MATCH`` puede habilitar una activación."""

    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class ReasonCode(StrEnum):
    """Motivo tipado de cada faceta del veredicto (nunca un string libre).

    ``reasons`` es una TUPLA: un assessment puede acumular varias razones
    (``PERSISTED_DIVERGES_FROM_LIVE`` + ``EFFECTIVE_IS_NOT_DESIRED_CLONE``) y
    perder la primera al colapsarlas a un solo motivo es exactamente cómo se
    documenta un defecto como si fuera un éxito.
    """

    #: El camino feliz completo: ruta observada == Desired Clone, identidad
    #: leída, compatibilidad COMPATIBLE.
    EFFECTIVE_MATCHES_DESIRED_CLONE = "effective_matches_desired_clone"
    #: La ruta efectiva ES la Managed Source (SFR-05/SFR-19).
    EFFECTIVE_IS_MANAGED_SOURCE = "effective_is_managed_source"
    #: La ruta efectiva cae dentro de una Generation (SFR-19: nunca se ejecuta
    #: desde una Generation, sólo desde un Clone).
    EFFECTIVE_IS_GENERATION = "effective_is_generation"
    #: La ruta efectiva es un Clone, pero no el Desired.
    EFFECTIVE_IS_NOT_DESIRED_CLONE = "effective_is_not_desired_clone"
    #: Algún componente de la ruta es symlink/junction/reparse (§29.4, SFR-18).
    REPARSE_NOT_ALLOWED = "reparse_not_allowed"
    #: La ruta efectiva no existe en disco: no hay runtime que ejecutar.
    EFFECTIVE_PATH_MISSING = "effective_path_missing"
    #: No se pudo leer la identidad del runtime (PE ilegible/ambiguo).
    IDENTITY_UNREADABLE = "identity_unreadable"
    #: Loader y DLL de SKSE presentes pero de otro build ⇒ INCOMPATIBLE (F6).
    SKSE_INCOMPATIBLE = "skse_incompatible"
    #: Compatibilidad no demostrable: no autoriza (``UNKNOWN != COMPATIBLE``).
    COMPATIBILITY_UNKNOWN = "compatibility_unknown"
    #: Ninguna superficie aportó una ruta.
    NO_EFFECTIVE_EVIDENCE = "no_effective_evidence"
    #: No hay autoridad durable del Desired Clone (falta el store de
    #: ``RuntimeCloneRecord``: hallazgo de FASE D, no defecto del oráculo).
    NO_DESIRED_CLONE = "no_desired_clone"
    #: El valor persistido difiere de la ruta del proceso vivo.
    PERSISTED_DIVERGES_FROM_LIVE = "persisted_diverges_from_live"
    #: Una superficie de Sky-Claw (``skyrim_path`` persistido o ``SKYRIM_PATH``
    #: del entorno) apunta a otro árbol que el runtime efectivo de MO2. Es el
    #: split-brain de §29.4 en su forma medible: las tools de Sky-Claw (LOOT,
    #: xEdit, DynDOLOD, grass) escribirían en el runtime saliente mientras el
    #: juego corre desde el otro. Detectado en el rig real de desarrollo.
    CONFIG_DIVERGES_FROM_EFFECTIVE = "config_diverges_from_effective"
    #: MO2 vivo arrancó ANTES de la última escritura del ini: puede retener en
    #: memoria un ``gamePath`` distinto del persistido.
    MO2_IN_MEMORY_MAY_DIVERGE = "mo2_in_memory_may_diverge"
    #: Dos procesos de juego vivos desde raíces distintas: no hay UNA ruta.
    AMBIGUOUS_LIVE_GAME = "ambiguous_live_game"
    #: Hay un proceso de juego vivo: es la evidencia más fuerte disponible.
    LIVE_GAME_PRESENT = "live_game_present"
    #: La tabla de procesos no se pudo leer: no se puede probar ausencia.
    PROCESS_TABLE_UNREADABLE = "process_table_unreadable"
    #: Tabla de procesos PARCIALMENTE ilegible con un juego observado: hay una
    #: raíz probada, pero un pid denegado podría ser otro juego desde otra raíz,
    #: y no se puede descartar (auditoría adversarial B1). El MATCH exige poder
    #: afirmar que NO hay otra raíz; con ``indeterminate=True`` no se puede.
    OTHER_ROOT_NOT_RULED_OUT = "other_root_not_ruled_out"
    #: Hay registrado un lanzador del juego (``SkyrimSE.exe``/``skse64_loader.exe``/
    #: ``skse64.exe``) fuera de la raíz efectiva y ninguna evidencia prueba cuál
    #: se seleccionará (auditoría adversarial B3). No es MISMATCH —no se sabe qué
    #: ejecutará MO2— pero el MATCH presupone una selección sin prueba. Las
    #: herramientas externas (LOOT, xEdit) no disparan este motivo.
    EXECUTABLE_SELECTION_UNPROVEN = "executable_selection_unproven"
    #: La inspección de reparse points falló (``OSError``): no se pudo determinar
    #: si la ruta es symlink/junction (auditoría adversarial B4). Colapsar este
    #: caso en "no hay reparse" fabrica una inspección exitosa sobre una duda.
    REPARSE_INSPECTION_FAILED = "reparse_inspection_failed"


@dataclasses.dataclass(frozen=True, slots=True)
class CompatibilityResult:
    """Resultado tipado del gate ``RUNTIME_COMPATIBILITY`` (nunca un bool)."""

    state: RuntimeCompatibility
    reason: str
    game_version: str | None = None
    skse_loader: str | None = None
    skse_dll: str | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            # Un COMPATIBLE sin motivo es una afirmación sin evidencia.
            raise EffectiveRuntimeOracleError("CompatibilityResult exige un motivo no vacío")


@dataclasses.dataclass(frozen=True, slots=True)
class SurfaceObservation:
    """Lo que UNA superficie declaró, tal cual, más su forma normalizada.

    ``normalized`` NO sigue reparse points: normalizar resolviendo symlinks
    borraría justamente lo que el escenario 6 necesita detectar (el mismo motivo
    por el que ``path_resolver`` conserva los accessors crudos).
    """

    source: EvidenceSource
    state: ObservationState
    raw: str | None = None
    normalized: str | None = None
    label: str = ""
    detail: str = ""


class LiveProcessKind(StrEnum):
    """Qué clase de proceso se observó vivo."""

    MO2 = "mo2"
    GAME = "game"
    SKSE_LOADER = "skse_loader"


@dataclasses.dataclass(frozen=True, slots=True)
class LiveProcessEvidence:
    """Proceso vivo observado: la ruta retenida en memoria, por la vía del SO."""

    kind: LiveProcessKind
    pid: int
    image_path: str
    started_at_ns: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class LiveProcessSnapshot:
    """Foto de la tabla de procesos. ``indeterminate`` ≠ "no hay procesos".

    ``indeterminate=True`` significa que la tabla no se pudo leer completa. Un
    ``processes=()`` con ``indeterminate=True`` NO es "no hay juego corriendo":
    es "no sé". Tratarlo como ausencia haría que un ``AccessDenied`` habilitara
    una activación sobre un runtime en uso.
    """

    processes: tuple[LiveProcessEvidence, ...] = ()
    indeterminate: bool = False
    detail: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class DesiredRuntime:
    """La EXPECTATIVA contra la que se compara (nunca la evidencia).

    ``clone_root`` es ``None`` mientras no exista el store de
    ``RuntimeCloneRecord``. Con ``clone_root=None`` el oráculo no puede probar
    igualdad y devuelve ``UNKNOWN``: fabricar el Desired desde una ruta que pasa
    el llamador sería exactamente la "cadena declarada por el llamador" que el
    brief prohíbe como prueba.
    """

    clone_id: str | None = None
    clone_root: pathlib.Path | None = None
    generation_id: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class EffectiveRuntimeAssessment:
    """Veredicto completo del oráculo, con la evidencia que lo sostiene."""

    verdict: EffectiveRuntimeVerdict
    reasons: tuple[ReasonCode, ...]
    effective_path: str | None
    effective_source: EvidenceSource | None
    desired_clone_id: str | None
    desired_clone_root: str | None
    observations: tuple[SurfaceObservation, ...]
    runtime_identity: RuntimeIdentity | None
    compatibility: RuntimeCompatibility
    compatibility_reason: str
    observed_at_ns: int

    def __post_init__(self) -> None:
        if not self.reasons:
            raise EffectiveRuntimeOracleError("un assessment sin motivos no es auditable")

    @property
    def authorizes_activation(self) -> bool:
        """Única expresión del paquete que puede devolver ``True``.

        Exige las dos condiciones del brief juntas: ``MATCH`` (la ruta
        observada ES el Desired Clone) y ``COMPATIBLE`` (el build sirve). Con
        cualquiera de las dos en ``UNKNOWN`` no hay activación.
        """
        return self.verdict is EffectiveRuntimeVerdict.MATCH and self.compatibility is RuntimeCompatibility.COMPATIBLE


@dataclasses.dataclass(frozen=True, slots=True)
class RuntimeProbes:
    """Los tres observadores externos, inyectables para poder probarlos.

    Se inyectan por la misma razón que ``PathValidator`` en ``MO2Controller``:
    un test que necesitara un PE real con recurso de versión para ejercitar el
    oráculo mediría el filesystem, no la lógica. Los DEFAULT están cableados en
    ``oracle.default_runtime_probes`` y un test verifica que degradan a
    ``UNKNOWN`` en vez de fabricar identidad.
    """

    identity: Callable[[pathlib.Path], RuntimeIdentity]
    compatibility: Callable[[pathlib.Path], CompatibilityResult]
    processes: Callable[[], LiveProcessSnapshot]

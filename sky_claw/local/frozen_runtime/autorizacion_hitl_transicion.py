"""Cableado de autorización HITL de la transición Golden Master (P7-A5 PR 3).

Tracker: #867 (histórico: #672)
ADR 0012 §35.2, §36.3, §36.6, §37.2, §37.4, §39.3, §39.4, §46, §47
ADR 0015 §12, §13

Por qué existe este módulo
--------------------------
Hasta P7-A5 PR 2 las piezas de la autorización existían **implementadas y
probadas, pero desconectadas entre sí y sin un consumidor productivo**:

```text
approval.proponer_aprobacion           FASE A — propuesta       sin llamador productivo
owner_consent.presentar/responder      FASE B/B' — HITL         HITL_PRODUCTIVE = NOT_WIRED
owner_consent.emitir_aprobacion_...    FASE C — ISSUE           cerrada por interruptor
runtime_clone_binder.ejecutar_...      CONSUME → BIND → ...     sin emisor que lo alimente
```

Este módulo es el **consumidor autorizado**: arma el ``ApprovalScope`` con
evidencia que sale de verificadores ``read-only`` (no de strings del llamador),
la presenta al propietario, recoge su decisión, la emite por la superficie
productiva, la consume a través del binder y verifica el resultado durable.

Lo que este módulo NO hace
--------------------------
- **No abre la emisión.** ``approval.EMISION_PRODUCTIVA_HABILITADA`` sigue en
  ``False``: el cableado llama a ``emitir_aprobacion_autorizada`` justamente para
  que la puerta cerrada sea la que gobierna. Los tests que emiten lo hacen con un
  ``monkeypatch`` explícito del interruptor, así que abrirlo es un cambio de una
  línea con el consumidor ya probado.
- **No define un segundo interruptor, ni un segundo ledger, ni una segunda
  superficie de consentimiento.** Hay censos por AST en ``tests/`` que lo
  congelan (``ConsentimientoAprobacion`` sólo lo construye ``owner_consent``;
  ``consumir_aprobacion`` sólo lo llaman ``approval`` y el binder).
- **No entrega mensajes ni habla con Telegram.** La presentación se entrega por
  un puerto inyectado (``entregar``) y la identidad la acuña
  ``verificar_identidad_telegram`` a partir del update CRUDO del transporte. El
  adaptador de transporte es el gap declarado del PR siguiente
  (``TRANSPORT_ADAPTER = NOT_WIRED``); inventarlo acá sin consumidor sería una
  segunda superficie de identidad.
- **No ejecuta la transición real ni toca instalaciones reales** en este PR:
  ``PRIMERA_TRANSICION_REAL = NOT_EXECUTED``.

Qué se deriva y de dónde (G1)
-----------------------------
Ningún campo del scope lo aporta el llamador. Cada uno sale de una verificación
existente:

| campo del scope | fuente |
|---|---|
| ``operation`` | ``PROMOTION`` (única operación que el binder ejecuta) |
| ``source_activation`` | ``desired_runtime_from_storage`` + INI/config observados (§36.3) |
| ``generation_id`` / ``clone_id`` | registro del Clone + preflight |
| ``clone_evidence`` | digest del árbol verificado de la Generation + registro del Clone |
| ``runtime_setup_id`` / ``runtime_setup_evidence`` | ``verificar_runtime_setup`` sobre el Clone |
| ``compatibility_evidence_id`` | compatibilidad COMPATIBLE + loader/DLL del build exacto |
| ``candidate_id`` | surrogate content-bound (ver ``NOTA candidate_id``) |
| ``approval_provenance`` | resumen canónico + digest de instancia MO2, INI, política, A1 y destino |

``NOTA candidate_id``: el esquema exige ``candidate_id`` en ``PROMOTION``
(§38.5 campo 10) y el camino Stock→Generation **no crea un Candidate de P3**
(``CANDIDATE_OBJECT = NOT_CREATED``). Se usa un surrogate determinista
``cand_<32 hex>`` derivado del contenido verificado. No se declara un Candidate:
se declara un identificador content-bound, y el residual queda escrito.

Discrepancias resueltas (ver ``docs/audits/2026-11-11_p7_a5_pr3_hitl_wiring_scope_audit.md``)
---------------------------------------------------------------------------------------------
1. **``SourceActivation`` no puede representar un runtime saliente no gestionado.**
   El contrato §36.3 exige «los cuatro en null» o «la terna completa»; un runtime
   manual no es una Generation de Sky-Claw y no puede producir la terna. El
   cableado usa el bootstrap íntegramente vacío —que significa «Sky-Claw no tenía
   activación registrada», verdadero— y **declara el runtime saliente en
   ``approval_provenance``** (digest de la ruta observada), que el digest del
   scope compromete. Además rechaza el bootstrap vacío cuando el runtime saliente
   está dentro de una superficie gestionada: ahí el vacío sería una mentira.
2. **El scope no liga la instancia de MO2.** No se agrega un campo (esquema
   cerrado, digest congelado). La instancia entra en ``approval_provenance`` y el
   cableado la re-verifica antes de emitir y antes de ejecutar. Residual
   declarado: un llamador **directo** del binder que no pase por acá no la
   verifica; hoy es inalcanzable en producción porque la emisión está cerrada y
   este módulo es el único emisor.
3. **Un fallo con el journal ``PENDING`` no se reanuda.** El burn-first gasta la
   aprobación antes de crear el ``PENDING``; si el fallo ocurre después, la
   aprobación es terminal y el journal exige al dueño. Este módulo **no**
   inventa una reanudación: la clasifica y la rechaza explícitamente
   (``ReanudacionRequiereDuenoError``). Lo que sí reanuda es la ventana
   anterior: aprobación consumida y journal sin ``PENDING``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, MutableMapping
from enum import StrEnum

from sky_claw.local.frozen_runtime import binder_recovery as recovery
from sky_claw.local.frozen_runtime import journal as journal_module
from sky_claw.local.frozen_runtime import storage
from sky_claw.local.frozen_runtime.approval import (
    ApprovalOperation,
    ApprovalTombstone,
    ConsentimientoAprobacion,
    IssuanceWitness,
    PropuestaAprobacion,
    TerminalState,
    derivar_autoridad_aprobacion,
    proponer_aprobacion,
    verificar_consumo_aprobacion,
)
from sky_claw.local.frozen_runtime.binder_preflight import (
    BinderPreflightVerdict,
    BinderTransitionPolicy,
    evaluar_binder_preflight,
)
from sky_claw.local.frozen_runtime.clones import (
    RuntimeCloneLifecycle,
    cargar_runtime_clone_record,
    verificar_runtime_clone,
)
from sky_claw.local.frozen_runtime.effective_runtime.mo2_ini import leer_game_path_de_mo2
from sky_claw.local.frozen_runtime.effective_runtime.models import (
    Mo2InstanceIdentityState,
    ObservationState,
    RuntimeProbes,
)
from sky_claw.local.frozen_runtime.effective_runtime.oracle import (
    default_runtime_probes,
    desired_runtime_from_storage,
)
from sky_claw.local.frozen_runtime.errors import (
    FrozenRuntimeError,
    JournalCorruptError,
    JournalIdentityError,
    JournalSchemaError,
    JournalStoreNoInicializadoError,
)
from sky_claw.local.frozen_runtime.generations import verificar_generation
from sky_claw.local.frozen_runtime.journal import (
    AprobacionConsumida,
    JournalDisposition,
    SourceActivation,
    cargar_journal_transicion,
    derivar_historial_finalizados,
    digest_canonico,
)
from sky_claw.local.frozen_runtime.owner_consent import (
    DecisionConsentimiento,
    IdentidadPropietario,
    PresentacionConsentimiento,
    RegistroConsentimientos,
    _exigir_identidad_verificada,
    emitir_aprobacion_autorizada,
)
from sky_claw.local.frozen_runtime.provisioning import verificar_runtime_setup
from sky_claw.local.frozen_runtime.root_lock import canonical_root_key, participant_lock
from sky_claw.local.frozen_runtime.runtime_clone_binder import (
    BinderResult,
    _propuesta_desde_witness,
    ejecutar_transicion_binder,
)
from sky_claw.local.frozen_runtime.state import write_json_atomic
from sky_claw.local.frozen_runtime.storage_models import GenerationVerificationState
from sky_claw.local.runtime_vault.inventory import inventory_tree
from sky_claw.local.runtime_vault.verification import tree_digest_from_files

logger = logging.getLogger(__name__)

#: Declaraciones de alcance. Se escriben para que el estado real del cableado no
#: se infiera de la ausencia de código.
EMISION_PRODUCTIVA_ABIERTA_POR_ESTE_MODULO = False
TRANSPORT_ADAPTER = "NOT_WIRED"
PRIMERA_TRANSICION_REAL = "NOT_EXECUTED"
CANDIDATE_OBJECT = "NOT_CREATED"
#: La aprobación se puede reanudar SÓLO en la ventana «consumida sin PENDING».
REANUDACION_POST_PENDING = "REQUIERE_DUENO"

#: Prefijo de la procedencia. Es parte del ``approval_scope_digest``: cambiarlo
#: invalida toda aprobación anterior, que es exactamente lo que se quiere.
_PREFIJO_PROCEDENCIA = "golden-master:v1"
#: El nombre del archivo de journal lo define ``journal``; acá no se duplica.
_SIN_TRANSICION = frozenset({JournalDisposition.ABSENT, JournalDisposition.NONE})


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


class AutorizacionTransicionError(FrozenRuntimeError):
    """Base de los rechazos del cableado. Todos son fail-closed."""


class EvidenciaTransicionError(AutorizacionTransicionError):
    """La evidencia no se pudo derivar, o cambió respecto de lo aprobado."""


class TransicionEnCursoError(AutorizacionTransicionError):
    """Hay una transición pendiente, reanudable o una aprobación emitida vigente."""


class TransicionYaActivaError(AutorizacionTransicionError):
    """El destino ya es el runtime efectivo: no hay nada que autorizar."""


class ReanudacionNoDisponibleError(AutorizacionTransicionError):
    """No hay ninguna transición consumida que reanudar para ese ``transition_id``."""


class ReanudacionRequiereDuenoError(AutorizacionTransicionError):
    """La transición dejó un ``PENDING``: el burn-first gastó la aprobación y decide el dueño."""


class SesionFueraDeOrdenError(AutorizacionTransicionError):
    """Se intentó un paso sin cumplir el anterior (o con otro objeto de sesión)."""


class AbandonoTransicionInvalidoError(AutorizacionTransicionError):
    """El abandono solicitado es inválido o viola una precondición durable (fail-closed)."""


class AbandonoCorruptoError(AutorizacionTransicionError):
    """El registro de abandono persistido es ilegible, incompleto o su digest no coincide (fail-closed)."""


#: Versión canónica del esquema de registros de abandono durable (§Codex discussion_r4543251726)
SCHEMA_VERSION_ABANDONO = 1


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class RegistroAbandonoTransicion:
    """Registro inmutable y verificable del abandono explícito de una transición consumida.

    Liga durablemente el storage root, la transición abandonada, la aprobación consumida,
    la identidad del propietario que la autorizó, el motivo y el timestamp, sellado por un digest canónico.
    """

    schema_version: int
    root_id: str
    transition_id: str
    approval_id: str
    approval_seq: int
    scope_digest: str
    propietario_sujeto: str
    propietario_superficie: str
    propietario_sesion: str
    propietario_mecanismo: str
    motivo: str
    abandonado_en_ns: int
    record_digest: str


class OrigenTransicion(StrEnum):
    """Qué runtime reemplaza la transición, según la evidencia durable de Sky-Claw."""

    #: No había Desired registrado: bootstrap íntegramente vacío (§36.8).
    SIN_ACTIVACION_DE_SKY_CLAW = "sin_activacion_de_sky_claw"
    #: Había un Desired registrado: la terna completa del origen es obligatoria.
    ACTIVACION_DE_SKY_CLAW = "activacion_de_sky_claw"


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenciaTransicion:
    """Todo lo que hace falta para proponer y luego re-verificar una transición.

    Es inmutable y **recomputable**: el cableado la vuelve a derivar antes de
    emitir y antes de ejecutar. Si dos derivaciones difieren, la aprobación del
    dueño ya no describe el mundo y no se usa.
    """

    transition_policy: BinderTransitionPolicy
    storage_root: pathlib.Path
    mo2_instance_dir: pathlib.Path
    mo2_ini_path: pathlib.Path
    mo2_ini_sha256: str
    mo2_instance_identity: Mo2InstanceIdentityState
    config_path: pathlib.Path | None
    config_skyrim_path: str | None
    generation_id: str
    generation_tree_digest: str
    clone_tree_digest: str
    clone_id: str
    clone_root: pathlib.Path
    clone_lifecycle: RuntimeCloneLifecycle
    clone_evidence: str
    runtime_setup_id: str
    runtime_setup_evidence: str
    compatibility_evidence_id: str
    candidate_id: str
    source_activation: SourceActivation
    origen: OrigenTransicion
    observed_mo2_game_path: str
    outgoing_manual_runtime_root: pathlib.Path | None
    stock_reference_digest: str | None
    approval_provenance: str

    def campos_derivados(self) -> dict[str, object]:
        """Los campos del scope que salen de la evidencia (sin ``approval_id``/``seq``).

        Es la lista que se compara contra un ``ApprovalScope`` ya emitido: una
        lista paralela se desactualiza sin ruido, así que se deriva del dataclass
        y se contrasta contra el scope campo por campo en ``_exigir_evidencia_vigente``.
        """
        return {
            "operation": ApprovalOperation.PROMOTION.value,
            "source_activation": dataclasses.asdict(self.source_activation),
            "source_activation_digest": digest_canonico(self.source_activation),
            "generation_id": self.generation_id,
            "clone_id": self.clone_id,
            "clone_evidence": self.clone_evidence,
            "runtime_setup_id": self.runtime_setup_id,
            "runtime_setup_evidence": self.runtime_setup_evidence,
            "compatibility_evidence_id": self.compatibility_evidence_id,
            "candidate_id": self.candidate_id,
            "approval_provenance": self.approval_provenance,
        }


class EstadoDurable(StrEnum):
    """Clasificación del estado durable del root para decidir si se autoriza algo."""

    #: Sin transición pendiente, sin aprobación consumida sin finalizar.
    LIMPIO = "limpio"
    #: Aprobación CONSUMIDA por una transición que no llegó a publicar su PENDING.
    REANUDABLE = "reanudable"
    #: Journal PENDING y/o registro de recuperación: hay superficies que restaurar.
    PENDIENTE_RECUPERABLE = "pendiente_recuperable"
    #: Incoherencia (journal ilegible, registro sin PENDING, evidencia ilegible).
    REQUIERE_DUENO = "requiere_dueno"


@dataclasses.dataclass(frozen=True, slots=True)
class DiagnosticoDurable:
    """Lo que el root dice de sí mismo, sin mutar nada."""

    estado: EstadoDurable
    journal_disposition: JournalDisposition | None
    transicion_en_curso: str | None
    transicion_reanudable: str | None
    registros_recuperacion: tuple[str, ...]
    motivo: str


@dataclasses.dataclass(frozen=True, slots=True)
class SolicitudTransicion:
    """Solicitud ABIERTA + la evidencia exacta que se presentó."""

    evidencia: EvidenciaTransicion
    propuesta: PropuestaAprobacion
    presentacion: PresentacionConsentimiento
    transicion_id: str


class EscenarioTransicion(StrEnum):
    """Desenlace de una ejecución/reanudación."""

    EJECUTADA = "ejecutada"
    REANUDADA = "reanudada"
    YA_FINALIZADA = "ya_finalizada"


@dataclasses.dataclass(frozen=True, slots=True)
class ResultadoTransicion:
    """Resultado con la evidencia que lo hace auditable."""

    escenario: EscenarioTransicion
    transicion_id: str
    aprobacion_id: str | None
    aprobacion_seq: int | None
    approval_scope_digest: str | None
    binder: BinderResult | None
    journal_disposition: JournalDisposition | None
    finalized_record: journal_module.FinalizedTransitionRecord | None
    verificado_en_ns: int


class _FaseSesion(StrEnum):
    NUEVA = "nueva"
    PREPARADA = "preparada"
    EMITIDA = "emitida"
    EJECUTADA = "ejecutada"
    RESUELTA_SIN_EMISION = "resuelta_sin_emision"


#: Puerto de entrega de la presentación al propietario. Lo implementa el
#: adaptador de transporte (PR siguiente). Sin él no hay presentación y, por lo
#: tanto, no hay autorización.
EntregarPresentacion = Callable[[PresentacionConsentimiento], None]


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------


def _clave(ruta: str | pathlib.Path | None) -> str | None:
    """Misma normalización que el binder usa para comparar rutas (hermano, no copia)."""
    if ruta is None:
        return None
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(ruta))))


def _dentro_de(ruta: pathlib.Path, raiz: pathlib.Path) -> bool:
    """``True`` si ``ruta`` es ``raiz`` o está contenida (comparación normalizada)."""
    objetivo = _clave(ruta)
    contenedor = _clave(raiz)
    if objetivo is None or contenedor is None:
        return False
    return objetivo == contenedor or objetivo.startswith(contenedor + os.sep)


#: Caracteres que se conservan legibles en la procedencia. El dueño tiene que poder
#: LEER qué instancia y qué runtime aprueba: una procedencia que sólo se puede
#: verificar y no leer convierte la aprobación en un acto de fe. Se escapan los
#: separadores (``:`` y ``;``) y todo lo que pueda partir la línea.
_SEGURO_EN_PROCEDENCIA = "/\\ ._-~"


def _segmento(valor: str) -> str:
    """Segmento de una sola línea, legible y sin separadores ambiguos."""
    return urllib.parse.quote(valor, safe=_SEGURO_EN_PROCEDENCIA)


def _leer_skyrim_path_persistido(config_path: pathlib.Path | None) -> str | None:
    """Valor REAL de ``skyrim_path`` en el config, leído por el mismo lector que el binder.

    Se importa el lector del binder en vez de reimplementarlo: el POST VERIFY del
    binder compara contra ese valor, y dos lectores distintos podrían divergir en
    silencio sobre qué dice el archivo persistido.
    """
    if config_path is None:
        return None
    from sky_claw.local.frozen_runtime.runtime_clone_binder import _leer_skyrim_path_persistido as lector

    try:
        return lector(config_path)
    except (FrozenRuntimeError, OSError, ValueError) as exc:
        raise EvidenciaTransicionError(
            f"la configuración persistida ({config_path}) no existe o no es legible: {exc}"
        ) from exc


def _registros_de_clones(storage_root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Raíces de TODOS los Clones registrados (para detectar un origen «oculto»)."""
    directorio = storage_root / "state" / "clones"
    if not directorio.is_dir():
        return ()
    raices: list[pathlib.Path] = []
    for ruta in sorted(directorio.glob("*.json")):
        try:
            registro = cargar_runtime_clone_record(storage_root, ruta.stem)
        except FrozenRuntimeError as exc:
            # Fail-closed: esta lista sostiene la afirmación «el runtime saliente no es una
            # superficie administrada». Un registro ilegible no prueba nada, y descartarlo en
            # silencio convertiría una prueba faltante en una prueba superada.
            raise EvidenciaTransicionError(
                f"el registro de Clone {ruta.name} existe pero no se puede leer ({exc}): sin ese registro "
                "no se puede afirmar que el runtime observado no pertenezca a un Clone administrado por "
                "Sky-Claw (fail-closed)"
            ) from exc
        if registro is not None:
            raices.append(pathlib.Path(registro.root_path))
    return tuple(raices)


def _exigir_origen_declarado(
    *,
    storage_root: pathlib.Path,
    observed_game_path: str | None,
    stock_source_root: pathlib.Path | None,
) -> None:
    """El bootstrap vacío no puede esconder un runtime gestionado por Sky-Claw.

    Si el INI apunta a una superficie que Sky-Claw administra (storage, una
    Generation, un Clone registrado o la copia Stock), entonces «no había
    activación registrada» es falso: falta el Desired, o alguien borró
    ``active.json``. En los dos casos la decisión es del dueño, no del cableado.
    """
    if observed_game_path is None:
        return
    observado = pathlib.Path(observed_game_path)
    superficies: list[tuple[str, pathlib.Path]] = [
        ("storage", storage_root),
        ("generations", storage_root / "versions"),
        *((f"clone:{raiz.name}", raiz) for raiz in _registros_de_clones(storage_root)),
    ]
    if stock_source_root is not None:
        superficies.append(("stock", stock_source_root))
    for etiqueta, raiz in superficies:
        if _dentro_de(observado, raiz):
            raise EvidenciaTransicionError(
                f"el runtime saliente observado ({observado}) está dentro de la superficie gestionada "
                f"{etiqueta} ({raiz}) y no hay Desired Runtime registrado: el bootstrap «sin activación de "
                "Sky-Claw» sería una declaración falsa. No se autoriza una transición sobre un estado que "
                "el dueño tiene que reconciliar primero (fail-closed)"
            )


def _activacion_de_origen(
    *,
    storage_root: pathlib.Path,
    mo2_instance_dir: pathlib.Path,
    observed_game_path: str | None,
    config_skyrim_path: str | None,
    stock_source_root: pathlib.Path | None,
) -> SourceActivation:
    """Construye el ``source_activation`` (§36.3) desde la observación, no desde el llamador."""
    desired = desired_runtime_from_storage(storage_root)
    if desired.clone_id is None and desired.generation_id is None:
        _exigir_origen_declarado(
            storage_root=storage_root,
            observed_game_path=observed_game_path,
            stock_source_root=stock_source_root,
        )
        # Bootstrap íntegramente vacío: Sky-Claw no tenía activación registrada.
        return SourceActivation(
            generation_id=None,
            clone_id=None,
            runtime_setup_id=None,
            effective_runtime_evidence=None,
        )

    if desired.clone_id is None or desired.generation_id is None or desired.clone_root is None:
        raise EvidenciaTransicionError(
            "el Desired Runtime persistido está incompleto (falta clone_id, generation_id o raíz): "
            "no se puede declarar el origen de la transición"
        )
    registro = cargar_runtime_clone_record(storage_root, desired.clone_id)
    if registro is None:
        raise EvidenciaTransicionError(
            f"el Desired Runtime declara el clone {desired.clone_id!r} y no hay registro de ese Clone: "
            "el origen de la transición no es verificable (fail-closed)"
        )
    if registro.lifecycle is not RuntimeCloneLifecycle.PROVISIONED or not registro.verified_runtime_setup_id:
        raise EvidenciaTransicionError(
            f"el Clone de origen {desired.clone_id!r} no está PROVISIONED con setup verificado "
            f"({registro.lifecycle.value}): no se declara un origen que no se puede verificar"
        )
    # §36.3: la terna completa con su evidencia. La evidencia liga QUÉ superficies
    # se reemplazan: Desired, ruta del Clone, gamePath observado y config persistido.
    evidencia_superficies = digest_canonico(
        {
            "desired_clone_id": desired.clone_id,
            "desired_generation_id": desired.generation_id,
            "desired_clone_root": _clave(desired.clone_root),
            "observed_game_path": _clave(observed_game_path),
            "config_skyrim_path": _clave(config_skyrim_path),
            "mo2_instance_dir": _clave(mo2_instance_dir),
        }
    )
    return SourceActivation(
        generation_id=desired.generation_id,
        clone_id=desired.clone_id,
        runtime_setup_id=registro.verified_runtime_setup_id,
        effective_runtime_evidence=evidencia_superficies,
    )


def _derivar_candidate_id(*, generation_id: str, clone_id: str, clone_evidence: str, runtime_setup_id: str) -> str:
    """Surrogate content-bound del ``candidate_id`` que ``PROMOTION`` exige.

    ``CANDIDATE_OBJECT = NOT_CREATED``: el camino Stock→Generation no crea un
    Candidate de P3, así que el campo obligatorio se cubre con un identificador
    derivado del contenido ya verificado. Mismo contenido ⇒ mismo id; cualquier
    byte distinto ⇒ otro id. No se presenta como Candidate: se presenta como lo
    que es.
    """
    semilla = digest_canonico(
        {
            "schema_version": 1,
            "kind": "golden-master-promotion-candidate-surrogate",
            "generation_id": generation_id,
            "clone_id": clone_id,
            "clone_evidence": clone_evidence,
            "runtime_setup_id": runtime_setup_id,
        }
    )
    return f"cand_{semilla[:32]}"


def _procedencia(*, payload: Mapping[str, object], resumen: str) -> str:
    """Procedencia de UNA línea: hechos legibles por el dueño + digest que los compromete."""
    semilla = digest_canonico({"schema_version": 1, "kind": "golden-master-provenance", **payload})
    return f"{_PREFIJO_PROCEDENCIA}:{resumen}:bind={semilla[:32]}"


# ---------------------------------------------------------------------------
# Derivation de evidencia (read-only)
# ---------------------------------------------------------------------------


def derivar_evidencia_transicion(
    *,
    storage_root: pathlib.Path,
    mo2_instance_dir: pathlib.Path,
    clone_id: str,
    transition_policy: BinderTransitionPolicy = BinderTransitionPolicy.STRICT_PROVIDER_IDLE,
    managed_source_root: pathlib.Path | None = None,
    outgoing_manual_runtime_root: pathlib.Path | None = None,
    stock_source_root: pathlib.Path | None = None,
    approved_stock_reference_digest: str | None = None,
    approved_stock_reference_digest_file: pathlib.Path | None = None,
    config_path: pathlib.Path | None = None,
    environ: Mapping[str, str] | None = None,
    probes: RuntimeProbes | None = None,
) -> EvidenciaTransicion:
    """Deriva el contenido del ``ApprovalScope`` desde verificadores read-only.

    Falla cerrado si cualquier eslabón no se puede verificar: preflight no
    ``READY``, política distinta de la pedida, Clone no ``PROVISIONED``,
    Generation no ``VALID``, setup observado distinto del registrado, o ausencia
    del ancla A1 bajo ``STOCK_INDEPENDENT_TRANSITION``.

    No escribe nada y no toma el root lock: es la FASE A' del protocolo, la
    lectura que alimenta la propuesta que después se presenta.
    """
    storage_path = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    mo2_dir = pathlib.Path(os.path.abspath(os.fspath(mo2_instance_dir)))
    ini_path = mo2_dir / "modorganizer.ini"
    observadores = probes if probes is not None else default_runtime_probes()
    if config_path is None:
        raise EvidenciaTransicionError(
            "se exige config_path para verificar la configuración persistida antes de presentar o emitir "
            "una aprobación (fail-closed contra el requisito estricto del binder; Codex discussion_r4543251718)"
        )
    if not pathlib.Path(config_path).is_file():
        raise EvidenciaTransicionError(
            f"la configuración persistida ({config_path}) no existe o no es legible (fail-closed)"
        )
    cfg_skyrim = _leer_skyrim_path_persistido(config_path)
    if not cfg_skyrim:
        raise EvidenciaTransicionError(
            f"el archivo de configuración persistido ({config_path}) no define skyrim_path: "
            "el binder exige una configuración coherente para la transición (fail-closed)"
        )

    preflight = evaluar_binder_preflight(
        storage_root=storage_path,
        mo2_instance_dir=mo2_dir,
        clone_id=clone_id,
        managed_source_root=managed_source_root,
        config_skyrim_path=cfg_skyrim,
        exigir_config_skyrim_path=True,
        environ=environ,
        probes=observadores,
        transition_policy=transition_policy,
        outgoing_manual_runtime_root=outgoing_manual_runtime_root,
        stock_source_root=stock_source_root,
        approved_stock_reference_digest=approved_stock_reference_digest,
        approved_stock_reference_digest_file=approved_stock_reference_digest_file,
    )
    if preflight.verdict is not BinderPreflightVerdict.READY or not preflight.is_ready:
        raise EvidenciaTransicionError(
            "el preflight de integración no autorizó la operación, así que no hay nada que presentar al "
            f"propietario: {[motivo.value for motivo in preflight.reasons]}"
        )
    if preflight.transition_policy is not transition_policy:
        raise EvidenciaTransicionError(
            f"el preflight evaluó con la política {preflight.transition_policy.value} y se pidió "
            f"{transition_policy.value}: no se autoriza una transición con una política que no es la declarada"
        )
    if preflight.mo2_instance_identity_state is not Mo2InstanceIdentityState.ACTIVE_GLOBAL:
        raise EvidenciaTransicionError(
            "la identidad de la instancia de MO2 no es la instancia global activa "
            f"({preflight.mo2_instance_identity_state}): no se autoriza mutar un INI que no es el de la "
            "instancia verificada"
        )
    # El ancla A1 sólo es una entrada de AUTORIZACIÓN bajo la política que la exige.
    # El preflight puede informar el digest embebido en la Generation también bajo
    # STRICT; incluirlo en la procedencia sugeriría que el dueño aprobó un ancla A1
    # que esa política no verificó.
    stock_reference_digest = (
        preflight.stock_reference_digest
        if transition_policy is BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION
        else None
    )
    if (
        transition_policy is BinderTransitionPolicy.STOCK_INDEPENDENT_TRANSITION
        and not preflight.stock_reference_digest
    ):
        raise EvidenciaTransicionError(
            "STOCK_INDEPENDENT_TRANSITION exige el digest de procedencia Stock A1 aprobado por el "
            "propietario y el preflight no lo verificó: no se deriva evidencia sin ancla A1"
        )

    registro = cargar_runtime_clone_record(storage_path, clone_id)
    if registro is None:
        raise EvidenciaTransicionError(f"no hay registro del Clone {clone_id!r} en el storage")
    if registro.lifecycle is not RuntimeCloneLifecycle.PROVISIONED:
        raise EvidenciaTransicionError(
            f"el Clone {clone_id!r} está en {registro.lifecycle.value}, no PROVISIONED: no es destino de una "
            "transición autorizable"
        )
    if not registro.verified_runtime_setup_id:
        raise EvidenciaTransicionError(
            f"el Clone {clone_id!r} no tiene setup verificado: el propietario no tendría qué aprobar"
        )

    clone_verificacion = verificar_runtime_clone(storage_path, clone_id, managed_source_root=managed_source_root)
    if clone_verificacion.state is not GenerationVerificationState.VALID:
        raise EvidenciaTransicionError(
            f"la verificación física del Clone {clone_id!r} no es VALID "
            f"({clone_verificacion.state.value}): {clone_verificacion.message}"
        )
    generation_verificacion = verificar_generation(storage_path, registro.source_generation_id)
    if (
        generation_verificacion.state is not GenerationVerificationState.VALID
        or generation_verificacion.recorded is None
    ):
        raise EvidenciaTransicionError(
            f"la Generation {registro.source_generation_id!r} del Clone no es VALID "
            f"({generation_verificacion.state.value}): {generation_verificacion.message}"
        )
    metadata = generation_verificacion.recorded

    clone_root = pathlib.Path(registro.root_path)
    try:
        setup = verificar_runtime_setup(clone_root)
    except FrozenRuntimeError as exc:
        raise EvidenciaTransicionError(f"el setup del Clone {clone_id!r} no se pudo verificar: {exc}") from exc
    if setup.setup_id != registro.verified_runtime_setup_id:
        raise EvidenciaTransicionError(
            f"el setup observado del Clone ({setup.setup_id}) no es el que el registro declara verificado "
            f"({registro.verified_runtime_setup_id}): el runtime cambió después de provisionarse y el "
            "propietario aprobaría un setup que no es el que se va a activar"
        )
    if registro.intended_runtime_setup_id != registro.verified_runtime_setup_id:
        raise EvidenciaTransicionError(
            f"el registro del Clone declara setup intencionado {registro.intended_runtime_setup_id!r} y "
            f"verificado {registro.verified_runtime_setup_id!r}: estado incoherente, no se autoriza"
        )

    # El árbol del Clone se verifica CONTRA LA GENERATION, byte por byte.
    #
    # ``verificar_runtime_clone`` comprueba independencia física, identidad de
    # runtime y archivos CRÍTICOS: un archivo agregado o cambiado fuera de esa
    # lista no lo invalida (hallazgo F-03 de la auditoría de este PR). El
    # propietario aprueba un contenido, no una lista de tres archivos, así que la
    # evidencia liga el ``TreeDigest`` observado y lo exige igual al registrado.
    # Costo declarado: una pasada completa de hash sobre el Clone por derivación
    # (tres en el camino completo: preparar, emitir, ejecutar).
    try:
        observado_arbol = tree_digest_from_files(inventory_tree(clone_root))
    except FrozenRuntimeError as exc:
        raise EvidenciaTransicionError(f"no se pudo inventariar el árbol del Clone {clone_id!r}: {exc}") from exc
    if (
        observado_arbol.digest != metadata.tree_digest.digest
        or observado_arbol.files != metadata.tree_digest.files
        or observado_arbol.bytes != metadata.tree_digest.bytes
    ):
        raise EvidenciaTransicionError(
            "el árbol del Clone no es el de su Generation "
            f"(observado digest={observado_arbol.digest} files={observado_arbol.files} bytes={observado_arbol.bytes}; "
            f"Generation digest={metadata.tree_digest.digest} files={metadata.tree_digest.files} "
            f"bytes={metadata.tree_digest.bytes}): el propietario aprobaría un contenido que ya cambió. "
            "fail-closed"
        )

    ini_evidencia = leer_game_path_de_mo2(ini_path)
    if ini_evidencia.state is not ObservationState.OBSERVED or ini_evidencia.path is None:
        raise EvidenciaTransicionError(
            f"el gamePath de {ini_path} no es observable ({ini_evidencia.state.value}): no se autoriza una "
            "mutación sobre un INI que no se puede describir"
        )
    try:
        ini_sha256 = digest_canonico({"ini_bytes_sha256": _sha256_archivo(ini_path)})
    except OSError as exc:
        raise EvidenciaTransicionError(f"no se pudo leer {ini_path}: {exc}") from exc

    source_activation = _activacion_de_origen(
        storage_root=storage_path,
        mo2_instance_dir=mo2_dir,
        observed_game_path=ini_evidencia.path,
        config_skyrim_path=cfg_skyrim,
        stock_source_root=stock_source_root,
    )
    origen = (
        OrigenTransicion.SIN_ACTIVACION_DE_SKY_CLAW
        if source_activation.generation_id is None
        else OrigenTransicion.ACTIVACION_DE_SKY_CLAW
    )

    criticos = {
        "runtime_identity": {
            "game_key": setup.runtime_identity.game_key,
            "game_version": setup.runtime_identity.game_version,
        },
        "files": [
            {"rel_path": archivo.rel_path, "size": archivo.size, "digest": archivo.digest}
            for archivo in setup.critical_files
        ],
    }
    runtime_setup_evidence = digest_canonico({"schema_version": 1, "setup_id": setup.setup_id, **criticos})
    compatibility_evidence_id = digest_canonico(
        {
            "schema_version": 1,
            "state": "COMPATIBLE",
            "setup_id": setup.setup_id,
            "runtime_identity": criticos["runtime_identity"],
            "loader_y_dll": [
                {"rel_path": archivo.rel_path, "digest": archivo.digest}
                for archivo in setup.critical_files
                if archivo.rel_path != "SkyrimSE.exe"
            ],
        }
    )
    clone_evidence = digest_canonico(
        {
            "schema_version": 1,
            "clone_id": registro.clone_id,
            "generation_id": registro.source_generation_id,
            "lifecycle": registro.lifecycle.value,
            "admitted_role": registro.admitted_role,
            "verified_runtime_setup_id": registro.verified_runtime_setup_id,
            "clone_root": _clave(clone_root),
            "generation_tree_digest": metadata.tree_digest.digest,
            "observed_clone_tree_digest": observado_arbol.digest,
            "generation_files": metadata.tree_digest.files,
            "generation_bytes": metadata.tree_digest.bytes,
            "generation_critical": [
                {"rel_path": archivo.rel_path, "size": archivo.size, "digest": archivo.digest}
                for archivo in metadata.critical_files
            ],
        }
    )
    candidate_id = _derivar_candidate_id(
        generation_id=registro.source_generation_id,
        clone_id=registro.clone_id,
        clone_evidence=clone_evidence,
        runtime_setup_id=registro.verified_runtime_setup_id,
    )

    resumen = ":".join(
        (
            f"inst={_segmento(_clave(mo2_dir) or '')}",
            f"ini={ini_sha256[:16]}",
            f"policy={transition_policy.value}",
            f"stock={'(none)' if stock_reference_digest is None else stock_reference_digest[:16]}",
            f"from={_segmento(_clave(ini_evidencia.path) or '(none)')}",
            f"to={_segmento(registro.clone_id)}@{_segmento(registro.source_generation_id)}",
            f"tree={observado_arbol.digest[:16]}",
            f"setup={_segmento(registro.verified_runtime_setup_id)}",
        )
    )
    provenance = _procedencia(
        payload={
            "storage_root": _clave(storage_path),
            "mo2_instance_dir": _clave(mo2_dir),
            "mo2_ini_sha256": ini_sha256,
            "observed_game_path": _clave(ini_evidencia.path),
            "config_path": _clave(config_path),
            "config_skyrim_path": _clave(cfg_skyrim),
            "transition_policy": transition_policy.value,
            "stock_reference_digest": stock_reference_digest,
            "outgoing_manual_runtime_root": _clave(outgoing_manual_runtime_root),
            "clone_id": registro.clone_id,
            "clone_root": _clave(clone_root),
            "generation_id": registro.source_generation_id,
            "generation_tree_digest": metadata.tree_digest.digest,
            "observed_clone_tree_digest": observado_arbol.digest,
            "runtime_setup_id": registro.verified_runtime_setup_id,
            "clone_evidence": clone_evidence,
            "runtime_setup_evidence": runtime_setup_evidence,
            "compatibility_evidence_id": compatibility_evidence_id,
            "candidate_id": candidate_id,
            "source_activation": dataclasses.asdict(source_activation),
        },
        resumen=resumen,
    )

    return EvidenciaTransicion(
        transition_policy=transition_policy,
        storage_root=storage_path,
        mo2_instance_dir=mo2_dir,
        mo2_ini_path=ini_path,
        mo2_ini_sha256=ini_sha256,
        mo2_instance_identity=preflight.mo2_instance_identity_state,
        config_path=pathlib.Path(config_path) if config_path is not None else None,
        config_skyrim_path=cfg_skyrim,
        generation_id=registro.source_generation_id,
        generation_tree_digest=metadata.tree_digest.digest,
        clone_tree_digest=observado_arbol.digest,
        clone_id=registro.clone_id,
        clone_root=clone_root,
        clone_lifecycle=registro.lifecycle,
        clone_evidence=clone_evidence,
        runtime_setup_id=registro.verified_runtime_setup_id,
        runtime_setup_evidence=runtime_setup_evidence,
        compatibility_evidence_id=compatibility_evidence_id,
        candidate_id=candidate_id,
        source_activation=source_activation,
        origen=origen,
        observed_mo2_game_path=ini_evidencia.path,
        outgoing_manual_runtime_root=(
            pathlib.Path(outgoing_manual_runtime_root) if outgoing_manual_runtime_root is not None else None
        ),
        stock_reference_digest=stock_reference_digest,
        approval_provenance=provenance,
    )


def _sha256_archivo(ruta: pathlib.Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Diagnóstico durable y resolución por abandono
# ---------------------------------------------------------------------------


def _calcular_digest_abandono(payload: Mapping[str, object]) -> str:
    """Calcula el digest canónico del registro de abandono sobre todos sus campos salvo 'record_digest'."""
    data = {k: v for k, v in payload.items() if k != "record_digest"}
    return digest_canonico(data)


def cargar_registro_abandono(
    ruta: pathlib.Path,
    *,
    expected_root_id: str | None = None,
) -> RegistroAbandonoTransicion:
    """Lee y valida un registro durable de abandono. Falla cerrado ante cualquier anomalía."""
    p = pathlib.Path(ruta)
    if not p.is_file():
        raise AbandonoTransicionInvalidoError(f"el archivo de registro de abandono no existe: {p}")
    try:
        raw = p.read_bytes()
        contenido = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise AbandonoCorruptoError(f"el registro de abandono en {p} no es JSON válido: {exc}") from exc

    if not isinstance(contenido, dict):
        raise AbandonoCorruptoError(f"el registro de abandono en {p} debe ser un objeto JSON")

    version = contenido.get("schema_version")
    if version != SCHEMA_VERSION_ABANDONO:
        raise AbandonoCorruptoError(
            f"versión de schema no soportada en {p}: {version!r} (se esperaba {SCHEMA_VERSION_ABANDONO})"
        )

    campos = (
        "root_id",
        "transition_id",
        "approval_id",
        "approval_seq",
        "scope_digest",
        "propietario_sujeto",
        "propietario_superficie",
        "propietario_sesion",
        "propietario_mecanismo",
        "motivo",
        "abandonado_en_ns",
        "record_digest",
    )
    faltantes = [c for c in campos if c not in contenido]
    if faltantes:
        raise AbandonoCorruptoError(f"el registro de abandono en {p} carece de campos obligatorios: {faltantes}")

    root_id = str(contenido["root_id"])
    if expected_root_id is not None and root_id != expected_root_id:
        raise AbandonoTransicionInvalidoError(
            f"el root_id del registro ({root_id}) no coincide con el esperado ({expected_root_id})"
        )

    digest_declarado = str(contenido["record_digest"])
    digest_calculado = _calcular_digest_abandono(contenido)
    if digest_declarado != digest_calculado:
        raise AbandonoCorruptoError(
            f"el digest del registro de abandono en {p} está corrupto (declarado={digest_declarado}, calculado={digest_calculado})"
        )

    return RegistroAbandonoTransicion(
        schema_version=int(version),
        root_id=root_id,
        transition_id=str(contenido["transition_id"]),
        approval_id=str(contenido["approval_id"]),
        approval_seq=int(contenido["approval_seq"]),
        scope_digest=str(contenido["scope_digest"]),
        propietario_sujeto=str(contenido["propietario_sujeto"]),
        propietario_superficie=str(contenido["propietario_superficie"]),
        propietario_sesion=str(contenido["propietario_sesion"]),
        propietario_mecanismo=str(contenido["propietario_mecanismo"]),
        motivo=str(contenido["motivo"]),
        abandonado_en_ns=int(contenido["abandonado_en_ns"]),
        record_digest=digest_declarado,
    )


def _cargar_abandonos(storage_root: pathlib.Path) -> dict[str, RegistroAbandonoTransicion]:
    """Carga todos los registros de abandono del storage. Falla cerrado si alguno está corrupto."""
    dir_abandonos = storage.abandoned_transitions_state_dir(storage_root)
    if not dir_abandonos.is_dir():
        return {}
    esperado_root = canonical_root_key(storage_root)[0]
    registros: dict[str, RegistroAbandonoTransicion] = {}
    for ruta in sorted(dir_abandonos.glob("*.json")):
        reg = cargar_registro_abandono(ruta, expected_root_id=esperado_root)
        registros[reg.transition_id] = reg
    return registros


def abandonar_transicion_consumida(
    storage_root: pathlib.Path,
    *,
    transition_id: str,
    identidad: IdentidadPropietario,
    motivo: str,
    timeout: float = 30.0,
    clock_ns: Callable[[], int] = time.time_ns,
) -> RegistroAbandonoTransicion:
    """Registra el abandono autorizado de una transición consumida que no llegó a finalizar.

    Invariantes obligatorios (§Codex discussion_r4543251726):
    - Requiere identidad verificada del propietario (reutiliza el mecanismo HITL existente).
    - Exige que transition_id y motivo sean cadenas no vacías.
    - Se ejecuta bajo el participant_lock del storage_root (exclusión mutua durable).
    - Idempotente y resistente a replay: si ya fue abandonada con idénticos parámetros, retorna el registro existente.
    - Rechaza si el journal tiene PENDING.
    - Rechaza si existen registros de recuperación no resueltos.
    - Rechaza si la transición ya está FINALIZED en el historial.
    - Rechaza si no existe una aprobación CONSUMED para este transition_id.
    - La aprobación y el tombstone CONSUMED permanecen INTACTOS (no se borran ni se modifican).
    - Persiste un registro JSON atómico e inmutable en state/abandoned_transitions/<transition_id>.json.
    """
    if not isinstance(transition_id, str) or not transition_id.strip():
        raise AbandonoTransicionInvalidoError("transition_id debe ser una cadena no vacía")
    t_id = transition_id.strip()

    if not isinstance(motivo, str) or not motivo.strip():
        raise AbandonoTransicionInvalidoError("se exige un motivo no vacío para autorizar el abandono")
    motivo_limpio = motivo.strip()

    _exigir_identidad_verificada(identidad)

    storage_path = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    if not storage_path.is_dir():
        raise AbandonoTransicionInvalidoError(f"storage_root no existe o no es directorio: {storage_path}")

    with participant_lock(storage_path, timeout=timeout):
        root_id = canonical_root_key(storage_path)[0]
        ruta_registro = storage.abandoned_transition_record_path(storage_path, t_id)

        # Idempotencia: si ya existe el registro de abandono, validarlo y devolverlo
        if ruta_registro.is_file():
            registro_existente = cargar_registro_abandono(ruta_registro, expected_root_id=root_id)
            if registro_existente.transition_id == t_id:
                return registro_existente

        # 1. Verificar journal: rechazar si hay PENDING
        try:
            snapshot_journal = cargar_journal_transicion(storage_path)
            if snapshot_journal.disposition not in _SIN_TRANSICION:
                raise AbandonoTransicionInvalidoError(
                    f"no se puede abandonar: el journal declara un PENDING ({snapshot_journal.disposition.value}) "
                    f"para la transición {snapshot_journal.journal.transition_id if snapshot_journal.journal else t_id!r}. "
                    "El dueño debe resolver primero la transición pendiente"
                )
        except (JournalCorruptError, JournalSchemaError, JournalIdentityError, JournalStoreNoInicializadoError) as exc:
            raise AbandonoTransicionInvalidoError(f"el journal no es legible ({exc}): fail-closed") from exc

        # 2. Verificar recovery: rechazar si existen registros de recuperación
        try:
            registros_rec = list(recovery.listar_registros(storage_path))
            if registros_rec:
                raise AbandonoTransicionInvalidoError(
                    f"no se puede abandonar: existen {len(registros_rec)} registro(s) de recuperación no resueltos. "
                    "El dueño debe recuperar las superficies antes de autorizar el abandono"
                )
        except (recovery.RecuperacionError, OSError) as exc:
            raise AbandonoTransicionInvalidoError(
                f"error al verificar registros de recuperación ({exc}): fail-closed"
            ) from exc

        # 3. Verificar historial: rechazar si ya está FINALIZED
        try:
            historial = derivar_historial_finalizados(storage_path)
            if any(r.transition_id == t_id for r in historial.registros):
                raise AbandonoTransicionInvalidoError(
                    f"la transición {t_id!r} ya está FINALIZED: no se puede abandonar una transición finalizada con éxito"
                )
        except (FrozenRuntimeError, OSError) as exc:
            raise AbandonoTransicionInvalidoError(
                f"error al consultar el historial de finalizados ({exc}): fail-closed"
            ) from exc

        # 4. Verificar tombstone: debe existir tombstone CONSUMED para este transition_id
        try:
            consumidos = _tombstones_consumidos(storage_path)
        except (FrozenRuntimeError, OSError) as exc:
            raise AbandonoTransicionInvalidoError(
                f"error al consultar la autoridad de aprobación ({exc}): fail-closed"
            ) from exc

        tombstone = next((t for t in consumidos if t.consumed_by_transition_id == t_id), None)
        if tombstone is None:
            raise AbandonoTransicionInvalidoError(
                f"no se encontró ninguna aprobación consumida por la transición {t_id!r} para abandonar"
            )

        # 5. Asegurar directorio de abandonos y persistir registro inmutable atómico
        storage.asegurar_directorio_abandonos(storage_path)
        ahora_ns = int(clock_ns())

        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION_ABANDONO,
            "root_id": root_id,
            "transition_id": t_id,
            "approval_id": tombstone.approval_id,
            "approval_seq": int(tombstone.approval_seq),
            "scope_digest": tombstone.approval_scope_digest,
            "propietario_sujeto": identidad.sujeto,
            "propietario_superficie": identidad.superficie.value,
            "propietario_sesion": identidad.sesion,
            "propietario_mecanismo": identidad.mecanismo,
            "motivo": motivo_limpio,
            "abandonado_en_ns": ahora_ns,
        }
        digest = _calcular_digest_abandono(payload)
        payload["record_digest"] = digest

        write_json_atomic(ruta_registro, payload)

        return cargar_registro_abandono(ruta_registro, expected_root_id=root_id)


def _tombstones_consumidos(root: pathlib.Path) -> tuple[ApprovalTombstone, ...]:
    autoridad = derivar_autoridad_aprobacion(root)
    return tuple(t for t in autoridad.tombstones if t.terminal_state is TerminalState.CONSUMED)


def diagnosticar_estado_durable(storage_root: pathlib.Path) -> DiagnosticoDurable:
    """Clasifica el estado durable SIN mutar nada (una transición a la vez).

    Es la puerta G6: mientras haya una transición en curso, reanudable o un
    ``PENDING`` con superficies que restaurar, no se autoriza otra. Sin ella, el
    burn-first podía gastar una aprobación nueva sobre un root que el binder
    rechazaría después —el dueño aprobaba dos veces la misma operación.
    """
    storages = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
    try:
        snapshot = cargar_journal_transicion(storages)
        disposition: JournalDisposition | None = snapshot.disposition
        journal = snapshot.journal
    except (JournalCorruptError, JournalSchemaError, JournalIdentityError, JournalStoreNoInicializadoError) as exc:
        return DiagnosticoDurable(
            estado=EstadoDurable.REQUIERE_DUENO,
            journal_disposition=None,
            transicion_en_curso=None,
            transicion_reanudable=None,
            registros_recuperacion=(),
            motivo=f"el journal transicional no es legible ({exc}): fail-closed hasta que lo resuelva el dueño",
        )
    try:
        registros = tuple(registro.transition_id for registro in recovery.listar_registros(storages))
    except (recovery.RecuperacionError, OSError) as exc:
        return DiagnosticoDurable(
            estado=EstadoDurable.REQUIERE_DUENO,
            journal_disposition=disposition,
            transicion_en_curso=None if journal is None else journal.transition_id,
            transicion_reanudable=None,
            registros_recuperacion=(),
            motivo=f"los registros de recuperación no son legibles ({exc}): fail-closed",
        )

    try:
        historial = derivar_historial_finalizados(storages)
        finalizados = {registro.transition_id for registro in historial.registros}
        consumidos = _tombstones_consumidos(storages)
    except (FrozenRuntimeError, OSError) as exc:
        return DiagnosticoDurable(
            estado=EstadoDurable.REQUIERE_DUENO,
            journal_disposition=disposition,
            transicion_en_curso=None if journal is None else journal.transition_id,
            transicion_reanudable=None,
            registros_recuperacion=registros,
            motivo=f"la autoridad durable no es legible ({exc}): fail-closed",
        )

    en_curso = None if journal is None else journal.transition_id
    if disposition is None:
        return DiagnosticoDurable(
            estado=EstadoDurable.REQUIERE_DUENO,
            journal_disposition=None,
            transicion_en_curso=en_curso,
            transicion_reanudable=None,
            registros_recuperacion=registros,
            motivo="el journal transicional no declara disposición: fail-closed hasta que lo resuelva el dueño",
        )
    if disposition not in _SIN_TRANSICION:
        estado = EstadoDurable.PENDIENTE_RECUPERABLE if registros else EstadoDurable.REQUIERE_DUENO
        motivo = (
            f"hay un PENDING ({disposition.value}) para la transición {en_curso}: primero se recupera y se "
            "reconcilia, después se vuelve a autorizar"
            if registros
            else f"hay un PENDING ({disposition.value}) sin registro de recuperación: no hay snapshot durable "
            "desde donde restaurar, decide el dueño"
        )
        return DiagnosticoDurable(estado, disposition, en_curso, None, registros, motivo)

    if registros:
        return DiagnosticoDurable(
            estado=EstadoDurable.PENDIENTE_RECUPERABLE,
            journal_disposition=disposition,
            transicion_en_curso=None,
            transicion_reanudable=None,
            registros_recuperacion=registros,
            motivo=(
                f"hay {len(registros)} registro(s) de recuperación sin PENDING: una transición interrumpida "
                "antes del journal. Se recuperan las superficies y se reconcilia el journal"
            ),
        )

    try:
        abandonos = _cargar_abandonos(storages)
    except (AbandonoCorruptoError, AbandonoTransicionInvalidoError, FrozenRuntimeError, OSError) as exc:
        return DiagnosticoDurable(
            estado=EstadoDurable.REQUIERE_DUENO,
            journal_disposition=disposition,
            transicion_en_curso=en_curso,
            transicion_reanudable=None,
            registros_recuperacion=registros,
            motivo=f"los registros de abandono no son legibles o están corruptos ({exc}): fail-closed",
        )

    reanudables: list[ApprovalTombstone] = []
    for t in consumidos:
        t_id = t.consumed_by_transition_id
        if t_id in finalizados:
            continue
        if t_id in abandonos:
            abandono = abandonos[t_id]
            if (
                abandono.approval_id == t.approval_id
                and abandono.approval_seq == int(t.approval_seq)
                and abandono.scope_digest == t.approval_scope_digest
            ):
                continue
        reanudables.append(t)

    if reanudables:
        ultimo = max(reanudables, key=lambda t: int(t.approval_seq))
        return DiagnosticoDurable(
            estado=EstadoDurable.REANUDABLE,
            journal_disposition=disposition,
            transicion_en_curso=None,
            transicion_reanudable=ultimo.consumed_by_transition_id,
            registros_recuperacion=(),
            motivo=(
                f"la aprobación {ultimo.approval_seq} fue consumida por la transición "
                f"{ultimo.consumed_by_transition_id} y esa transición no está FINALIZED: se reanuda con la "
                "MISMA aprobación (no se emite otra)"
            ),
        )

    return DiagnosticoDurable(
        estado=EstadoDurable.LIMPIO,
        journal_disposition=disposition,
        transicion_en_curso=None,
        transicion_reanudable=None,
        registros_recuperacion=(),
        motivo="sin transición pendiente ni aprobación consumida sin finalizar",
    )


def _exigir_sin_transicion_en_curso(storage_root: pathlib.Path) -> DiagnosticoDurable:
    diagnostico = diagnosticar_estado_durable(storage_root)
    if diagnostico.estado is EstadoDurable.LIMPIO:
        return diagnostico
    if diagnostico.estado is EstadoDurable.REANUDABLE:
        raise TransicionEnCursoError(
            f"{diagnostico.motivo}. Reanudá con transition_id={diagnostico.transicion_reanudable!r}: emitir "
            "una aprobación nueva autorizaría dos veces la misma transición"
        )
    raise TransicionEnCursoError(diagnostico.motivo)


# ---------------------------------------------------------------------------
# Sesión de autorización
# ---------------------------------------------------------------------------


class SesionTransicionGoldenMaster:
    """Coordina la cadena autorizada de UNA transición Golden Master.

    La sesión no es durable y no guarda autoridad: guarda **estado de
    conversación** (qué evidencia se presentó, qué consentimiento se emitió) para
    que no se pueda ejecutar algo que no se autorizó. La autoridad sigue en
    ``approval`` (witness + tombstone + ledger), en ``journal`` y en el
    ``binder_recovery``. Cerrar el proceso pierde la sesión y no pierde evidencia.
    """

    def __init__(
        self,
        *,
        storage_root: pathlib.Path,
        mo2_instance_dir: pathlib.Path,
        clone_id: str,
        transition_policy: BinderTransitionPolicy = BinderTransitionPolicy.STRICT_PROVIDER_IDLE,
        managed_source_root: pathlib.Path | None = None,
        outgoing_manual_runtime_root: pathlib.Path | None = None,
        stock_source_root: pathlib.Path | None = None,
        approved_stock_reference_digest: str | None = None,
        approved_stock_reference_digest_file: pathlib.Path | None = None,
        config_path: pathlib.Path | None = None,
        environ: MutableMapping[str, str] | None = None,
        probes: RuntimeProbes | None = None,
        registro: RegistroConsentimientos | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not isinstance(transition_policy, BinderTransitionPolicy):
            raise EvidenciaTransicionError(
                f"transition_policy debe ser BinderTransitionPolicy, no {type(transition_policy).__name__}: "
                "una cadena suelta no selecciona la política de admisión del proveedor"
            )
        self._storage_root = pathlib.Path(os.path.abspath(os.fspath(storage_root)))
        self._mo2_instance_dir = pathlib.Path(os.path.abspath(os.fspath(mo2_instance_dir)))
        self._clone_id = clone_id
        self._transition_policy = transition_policy
        self._managed_source_root = managed_source_root
        self._outgoing_manual_runtime_root = outgoing_manual_runtime_root
        self._stock_source_root = stock_source_root
        self._approved_stock_reference_digest = approved_stock_reference_digest
        self._approved_stock_reference_digest_file = approved_stock_reference_digest_file
        self._config_path = config_path
        self._environ = environ
        self._probes = probes
        self._registro = registro if registro is not None else RegistroConsentimientos()
        self._clock_ns = clock_ns
        self._fase = _FaseSesion.NUEVA
        self._solicitud: SolicitudTransicion | None = None
        self._consentimiento: ConsentimientoAprobacion | None = None
        self._transicion_id: str | None = None

    # -- lectura -----------------------------------------------------------

    @property
    def registro(self) -> RegistroConsentimientos:
        return self._registro

    @property
    def fase(self) -> _FaseSesion:
        return self._fase

    def diagnosticar(self) -> DiagnosticoDurable:
        return diagnosticar_estado_durable(self._storage_root)

    def derivar_evidencia(self) -> EvidenciaTransicion:
        return derivar_evidencia_transicion(
            storage_root=self._storage_root,
            mo2_instance_dir=self._mo2_instance_dir,
            clone_id=self._clone_id,
            transition_policy=self._transition_policy,
            managed_source_root=self._managed_source_root,
            outgoing_manual_runtime_root=self._outgoing_manual_runtime_root,
            stock_source_root=self._stock_source_root,
            approved_stock_reference_digest=self._approved_stock_reference_digest,
            approved_stock_reference_digest_file=self._approved_stock_reference_digest_file,
            config_path=self._config_path,
            environ=self._environ,
            probes=self._probes,
        )

    # -- FASE A/B: propuesta + presentación --------------------------------

    def preparar(
        self,
        *,
        identidad: IdentidadPropietario,
        entregar: EntregarPresentacion,
        transition_id: str | None = None,
    ) -> SolicitudTransicion:
        """Deriva la evidencia, propone y PRESENTA. No emite y no toma el root lock."""
        if self._fase is not _FaseSesion.NUEVA:
            raise SesionFueraDeOrdenError(
                f"la sesión ya está en fase {self._fase.value}: una sesión prepara una sola transición"
            )
        if not callable(entregar):
            raise SesionFueraDeOrdenError(
                "preparar exige un puerto de entrega: una presentación que no se entrega no es una solicitud"
            )
        _exigir_sin_transicion_en_curso(self._storage_root)
        evidencia = self.derivar_evidencia()
        self._exigir_destino_no_activo(evidencia)
        propuesta = proponer_aprobacion(
            self._storage_root,
            operation=ApprovalOperation.PROMOTION,
            source_activation=evidencia.source_activation,
            generation_id=evidencia.generation_id,
            clone_id=evidencia.clone_id,
            clone_evidence=evidencia.clone_evidence,
            runtime_setup_id=evidencia.runtime_setup_id,
            runtime_setup_evidence=evidencia.runtime_setup_evidence,
            compatibility_evidence_id=evidencia.compatibility_evidence_id,
            candidate_id=evidencia.candidate_id,
            approval_provenance=evidencia.approval_provenance,
        )
        presentacion = self._registro.presentar(self._storage_root, propuesta, identidad=identidad)
        try:
            entregar(presentacion)
        except BaseException:
            # Sin entrega no hay solicitud: se cancela para que nadie la responda
            # desde un canal que el dueño nunca vio.
            self._registro.cancelar(presentacion.solicitud_id, motivo="fallo del puerto de entrega")
            raise
        self._solicitud = SolicitudTransicion(
            evidencia=evidencia,
            propuesta=propuesta,
            presentacion=presentacion,
            transicion_id=transition_id or str(uuid.uuid4()),
        )
        self._fase = _FaseSesion.PREPARADA
        logger.info(
            "golden-master: solicitud %s presentada (seq %s) para el clone %s",
            presentacion.solicitud_id,
            propuesta.scope.approval_seq,
            evidencia.clone_id,
        )
        return self._solicitud

    def _exigir_destino_no_activo(self, evidencia: EvidenciaTransicion) -> None:
        """Si el destino YA es el runtime efectivo, no hay nada que autorizar."""
        desired = desired_runtime_from_storage(self._storage_root)
        if desired.clone_id != evidencia.clone_id or desired.clone_root is None:
            return
        if _clave(desired.clone_root) != _clave(evidencia.clone_root):
            return
        ini = leer_game_path_de_mo2(evidencia.mo2_ini_path)
        if (
            ini.state is ObservationState.OBSERVED
            and ini.path is not None
            and _clave(ini.path) == _clave(evidencia.clone_root)
        ):
            raise TransicionYaActivaError(
                f"el Clone {evidencia.clone_id!r} ya es el runtime efectivo (Desired + gamePath): una nueva "
                "aprobación no autorizaría ninguna transición"
            )

    # -- FASE B'/C: decisión + emisión ------------------------------------

    def decidir_y_emitir(
        self,
        *,
        solicitud: SolicitudTransicion | None,
        decision: DecisionConsentimiento,
        identidad: IdentidadPropietario,
    ) -> ConsentimientoAprobacion | None:
        """Registra la decisión y, si es ``APROBAR``, EMITE por la superficie productiva.

        Orden deliberado:

        1. ``responder`` (lo único que produce consentimiento) — el registro
           valida superficie, sesión y sujeto, y acuña el binding durable;
        2. re-derivar la evidencia y compararla con la presentada — el dueño
           aprobó un estado del mundo, no una intención;
        3. ``emitir_aprobacion_autorizada`` — la puerta sigue cerrada por
           ``EMISION_PRODUCTIVA_HABILITADA = False`` y este módulo no la abre;
        4. verificar el witness y el binding de la solicitud.

        Una decisión negativa devuelve ``None`` y no escribe autoridad.
        """
        if self._fase is not _FaseSesion.PREPARADA or self._solicitud is None:
            raise SesionFueraDeOrdenError(f"no hay una solicitud presentada que decidir (fase {self._fase.value})")
        if solicitud is not self._solicitud:
            raise SesionFueraDeOrdenError(
                "la solicitud no es la que esta sesión presentó: no se decide sobre una solicitud ajena"
            )
        consentimiento = self._registro.responder(
            solicitud.presentacion.solicitud_id,
            decision=decision,
            identidad=identidad,
        )
        if consentimiento is None:
            self._fase = _FaseSesion.RESUELTA_SIN_EMISION
            return None

        actual = self.derivar_evidencia()
        self._exigir_evidencia_vigente(solicitud, actual)
        emitir_aprobacion_autorizada(self._storage_root, solicitud.propuesta, consentimiento)
        self._verificar_emision(solicitud)
        self._consentimiento = consentimiento
        self._fase = _FaseSesion.EMITIDA
        return consentimiento

    def _exigir_evidencia_vigente(self, solicitud: SolicitudTransicion, actual: EvidenciaTransicion) -> None:
        """La evidencia re-derivada tiene que ser la que se presentó, campo por campo."""
        esperados = solicitud.evidencia.campos_derivados()
        observados = actual.campos_derivados()
        if esperados == observados:
            return
        distintas = sorted(campo for campo in esperados if esperados[campo] != observados.get(campo))
        raise EvidenciaTransicionError(
            "la evidencia cambió entre la presentación y la ejecución "
            f"(campos distintos: {distintas}). El propietario aprobó un estado del mundo concreto; se "
            "descarta la aprobación y se debe presentar de nuevo"
        )

    def _verificar_emision(self, solicitud: SolicitudTransicion) -> None:
        autoridad = derivar_autoridad_aprobacion(self._storage_root)
        seq = solicitud.propuesta.scope.approval_seq
        witness = next((w for w in autoridad.witnesses if w.approval_seq == seq), None)
        if witness is None:
            raise AutorizacionTransicionError(
                f"la emisión no dejó witness para la seq {seq}: no hay autoridad durable que consumir"
            )
        if witness.approval_scope_digest != solicitud.propuesta.scope.approval_scope_digest:
            raise AutorizacionTransicionError(
                "el witness emitido no liga el digest del scope presentado: la autoridad no coincide con lo "
                "que el propietario aprobó"
            )
        binding = witness.solicitud_binding
        if binding is None or binding.get("solicitud_id") != solicitud.presentacion.solicitud_id:
            raise AutorizacionTransicionError(
                "el witness no está atado a la solicitud que el propietario vio: REQUEST_PRESENTED != "
                "REQUEST_PERSISTED (§47.1)"
            )

    # -- CONSUMO + BINDER + VERIFICACIÓN ---------------------------------

    def ejecutar(
        self,
        *,
        solicitud: SolicitudTransicion | None,
        consentimiento: ConsentimientoAprobacion | None,
        transition_id: str | None = None,
    ) -> ResultadoTransicion:
        """Consume la aprobación a través del binder y verifica el resultado durable."""
        if self._fase is not _FaseSesion.EMITIDA or self._solicitud is None or self._consentimiento is None:
            raise SesionFueraDeOrdenError(
                f"no hay una aprobación emitida que ejecutar (fase {self._fase.value}): primero se decide"
            )
        if solicitud is not self._solicitud or consentimiento is not self._consentimiento:
            raise SesionFueraDeOrdenError("la solicitud o el consentimiento no son los que esta sesión emitió")
        if transition_id is not None and transition_id != self._solicitud.transicion_id:
            raise SesionFueraDeOrdenError(
                f"el transition_id {transition_id!r} no es el de esta sesión "
                f"({self._solicitud.transicion_id!r}): el consumo es single-use por transición"
            )
        _exigir_sin_transicion_en_curso(self._storage_root)
        actual = self.derivar_evidencia()
        self._exigir_evidencia_vigente(self._solicitud, actual)

        resultado = ejecutar_transicion_binder(
            storage_root=self._storage_root,
            mo2_instance_dir=self._mo2_instance_dir,
            target_clone_id=self._clone_id,
            propuesta=self._solicitud.propuesta,
            consentimiento=consentimiento,
            managed_source_root=self._managed_source_root,
            config_path=self._config_path,
            environ=self._environ,
            probes=self._probes,
            transition_id=self._solicitud.transicion_id,
            transition_policy=self._transition_policy,
            outgoing_manual_runtime_root=self._outgoing_manual_runtime_root,
            stock_source_root=self._stock_source_root,
            approved_stock_reference_digest=self._approved_stock_reference_digest,
            approved_stock_reference_digest_file=self._approved_stock_reference_digest_file,
        )
        self._transicion_id = self._solicitud.transicion_id
        self._fase = _FaseSesion.EJECUTADA
        return self._resultado_verificado(
            escenario=EscenarioTransicion.EJECUTADA,
            transicion_id=self._solicitud.transicion_id,
            binder=resultado,
            aprobacion_id=self._solicitud.propuesta.scope.approval_id,
            aprobacion_seq=self._solicitud.propuesta.scope.approval_seq,
            scope_digest=self._solicitud.propuesta.scope.approval_scope_digest,
        )

    def _resultado_verificado(
        self,
        *,
        escenario: EscenarioTransicion,
        transicion_id: str,
        binder: BinderResult | None,
        aprobacion_id: str | None,
        aprobacion_seq: int | None,
        scope_digest: str | None,
    ) -> ResultadoTransicion:
        """Verifica en la autoridad durable lo que el binder acaba de declarar."""
        if binder is not None and binder.transition_id == "idempotent-noop":
            escenario = EscenarioTransicion.YA_FINALIZADA
        historial = derivar_historial_finalizados(self._storage_root)
        registro = next((r for r in historial.registros if r.transition_id == transicion_id), None)
        if registro is None and binder is not None and binder.success and binder.transition_id != "idempotent-noop":
            raise AutorizacionTransicionError(
                f"el binder declaró éxito de la transición {transicion_id} y el historial no tiene su "
                "registro FINALIZED: no se declara una transición exitosa sin evidencia durable"
            )
        autoridad = derivar_autoridad_aprobacion(self._storage_root)
        if aprobacion_seq is not None:
            terminal = next(
                (t for t in autoridad.tombstones if t.approval_seq == aprobacion_seq),
                None,
            )
            if terminal is None or terminal.terminal_state is not TerminalState.CONSUMED:
                raise AutorizacionTransicionError(
                    f"la aprobación {aprobacion_seq} no quedó CONSUMED: la transición declaró éxito sin "
                    "haber quemado la autorización"
                )
            if terminal.consumed_by_transition_id != transicion_id:
                raise AutorizacionTransicionError(
                    f"la aprobación fue consumida por {terminal.consumed_by_transition_id!r}, no por "
                    f"{transicion_id!r} (single-use §35.2)"
                )
        snapshot = cargar_journal_transicion(self._storage_root)
        return ResultadoTransicion(
            escenario=escenario,
            transicion_id=transicion_id,
            aprobacion_id=aprobacion_id,
            aprobacion_seq=aprobacion_seq,
            approval_scope_digest=scope_digest,
            binder=binder,
            journal_disposition=snapshot.disposition,
            finalized_record=registro,
            verificado_en_ns=int(self._clock_ns()),
        )

    # -- REANUDACIÓN ------------------------------------------------------

    def reanudar(self, *, transition_id: str) -> ResultadoTransicion:
        """Reanuda con la MISMA aprobación consumida, o falla cerrado.

        Sólo hay un escenario reanudable: la aprobación se consumió y la
        transición no llegó a publicar su ``PENDING`` (el journal está en
        ``ABSENT``/``NONE`` y no hay registro de recuperación). Ahí el
        ``transition_id`` es el mismo y ``consumir_aprobacion`` es idempotente
        para él.

        Con un ``PENDING`` publicado el burn-first ya gastó la aprobación: las
        superficies se restauran desde el snapshot durable, el journal queda
        ``PENDING`` y **la decisión es del dueño**. Inventar una reanudación ahí
        sería re-emitir una aprobación que el propietario ya gastó.
        """
        diagnostico = diagnosticar_estado_durable(self._storage_root)
        historial = derivar_historial_finalizados(self._storage_root)
        finalizado = next((r for r in historial.registros if r.transition_id == transition_id), None)
        if finalizado is not None:
            return self._resultado_verificado(
                escenario=EscenarioTransicion.YA_FINALIZADA,
                transicion_id=transition_id,
                binder=None,
                aprobacion_id=finalizado.consumed_approval_id,
                aprobacion_seq=finalizado.consumed_approval_seq,
                scope_digest=finalizado.approval_scope_digest,
            )

        if diagnostico.estado is EstadoDurable.PENDIENTE_RECUPERABLE:
            # SÓLO diagnóstico. ``recuperar_transicion_interrumpida`` reescribe superficies y
            # retira registros: es una operación con efectos y no puede viajar escondida dentro
            # de un método que después lanza una excepción (el operador no sabría qué cambió).
            # La recuperación es un paso EXPLÍCITO del dueño; acá no se escribe ni un byte.
            raise ReanudacionRequiereDuenoError(
                "la transición dejó un PENDING publicado: el burn-first gastó la aprobación, no se "
                "re-emite y esta reanudación es de SÓLO DIAGNÓSTICO (no restaura nada). La "
                "recuperación de superficies es un paso explícito del dueño: "
                "binder_recovery.recuperar_transicion_interrumpida(root, transition_id=...) restaura "
                "desde el snapshot durable, journal.reconciliar_journal(root) clasifica el journal, y "
                "después se autoriza una transición NUEVA"
            )
        if diagnostico.estado is EstadoDurable.REQUIERE_DUENO:
            raise ReanudacionRequiereDuenoError(diagnostico.motivo)
        if diagnostico.estado is not EstadoDurable.REANUDABLE or diagnostico.transicion_reanudable != transition_id:
            raise ReanudacionNoDisponibleError(
                f"no hay ninguna aprobación consumida por la transición {transition_id!r} que reanudar "
                f"(estado={diagnostico.estado.value}, reanudable={diagnostico.transicion_reanudable!r})"
            )

        aprobacion, witness = self._aprobacion_consumida_durable(transition_id)
        # Re-derivar y comparar contra lo APROBADO, igual que hace ``ejecutar``: una
        # interrupción no puede degradar el vínculo con la instancia de MO2 ni con el
        # estado del mundo que el propietario aprobó (F-08).
        self._exigir_evidencia_aprobada(witness=witness, actual=self.derivar_evidencia())
        resultado = ejecutar_transicion_binder(
            storage_root=self._storage_root,
            mo2_instance_dir=self._mo2_instance_dir,
            target_clone_id=self._clone_id,
            aprobacion_consumida=aprobacion,
            managed_source_root=self._managed_source_root,
            config_path=self._config_path,
            environ=self._environ,
            probes=self._probes,
            transition_id=transition_id,
            transition_policy=self._transition_policy,
            outgoing_manual_runtime_root=self._outgoing_manual_runtime_root,
            stock_source_root=self._stock_source_root,
            approved_stock_reference_digest=self._approved_stock_reference_digest,
            approved_stock_reference_digest_file=self._approved_stock_reference_digest_file,
        )
        return self._resultado_verificado(
            escenario=EscenarioTransicion.REANUDADA,
            transicion_id=transition_id,
            binder=resultado,
            aprobacion_id=aprobacion.approval_id,
            aprobacion_seq=aprobacion.approval_seq,
            scope_digest=aprobacion.scope_digest,
        )

    def _exigir_evidencia_aprobada(self, *, witness: IssuanceWitness, actual: EvidenciaTransicion) -> None:
        """La reanudación corre sobre el estado que el propietario aprobó, no sobre el actual.

        ``approval_provenance`` liga instancia de MO2, SHA-256 del INI, ``gamePath``
        observado, config persistido, política y ancla A1; comparar campo por campo
        contra la proyección del witness es lo que impide que una aprobación gastada
        para la instancia A se aplique al INI de la instancia B (F-08).
        """
        aprobados = witness.scope_payload
        observados = actual.campos_derivados()
        distintas = sorted(campo for campo, valor in observados.items() if aprobados.get(campo) != valor)
        if distintas:
            raise EvidenciaTransicionError(
                "la evidencia re-derivada no es la que el propietario aprobó (campos distintos: "
                f"{distintas}): la aprobación liga una instancia de MO2 y un estado del mundo concretos y "
                "no se reanuda sobre otro estado. Se descarta la reanudación; hay que presentar de nuevo"
            )

    def _aprobacion_consumida_durable(self, transition_id: str) -> tuple[AprobacionConsumida, IssuanceWitness]:
        """Reconstruye la evidencia de consumo DESDE la autoridad durable.

        No se guarda en memoria ni se acepta del llamador: sale del witness
        (proyección del scope emitido) y del tombstone (consumo terminal), y se
        cruza con ``verificar_consumo_aprobacion`` antes de usarse.
        """
        autoridad = derivar_autoridad_aprobacion(self._storage_root)
        terminales = [
            t
            for t in autoridad.tombstones
            if t.terminal_state is TerminalState.CONSUMED and t.consumed_by_transition_id == transition_id
        ]
        if len(terminales) != 1:
            raise ReanudacionNoDisponibleError(
                f"se esperaba exactamente un tombstone CONSUMED para la transición {transition_id!r} y hay "
                f"{len(terminales)}: la evidencia durable no permite reanudar"
            )
        terminal = terminales[0]
        witness: IssuanceWitness | None = next(
            (w for w in autoridad.witnesses if w.approval_seq == terminal.approval_seq),
            None,
        )
        if witness is None:
            raise ReanudacionNoDisponibleError(
                f"el tombstone de la seq {terminal.approval_seq} no tiene witness de emisión: no se reanuda"
            )
        clone_id = str(witness.scope_payload["clone_id"])
        if clone_id != self._clone_id:
            raise ReanudacionNoDisponibleError(
                f"la aprobación consumida es para el clone {clone_id!r} y esta sesión apunta a "
                f"{self._clone_id!r}: no se reanuda una transición ajena"
            )
        registro = cargar_runtime_clone_record(self._storage_root, clone_id)
        if registro is None:
            raise ReanudacionNoDisponibleError(
                f"no hay registro del Clone {clone_id!r}: no se puede reconstruir el destino efectivo aprobado"
            )
        propuesta = _propuesta_desde_witness(
            witness,
            transition_id=transition_id,
            expected_effective_path=registro.root_path,
            created_at_ns=int(self._clock_ns()),
        )
        try:
            aprobacion = verificar_consumo_aprobacion(
                self._storage_root,
                approval_id=terminal.approval_id,
                approval_seq=terminal.approval_seq,
                approval_scope_digest=terminal.approval_scope_digest,
                transition_id=transition_id,
                transicion=propuesta,
            )
        except FrozenRuntimeError as exc:
            raise ReanudacionNoDisponibleError(
                f"la evidencia de consumo de la transición {transition_id!r} no liga con el witness: {exc}"
            ) from exc
        # El witness vuelve con la aprobación: es la proyección APROBADA contra la que se
        # re-verifica el estado actual antes de reanudar (F-08).
        return aprobacion, witness

    def abandonar(
        self,
        *,
        transition_id: str,
        identidad: IdentidadPropietario,
        motivo: str,
        timeout: float = 30.0,
    ) -> RegistroAbandonoTransicion:
        """Registra el abandono autorizado de una transición consumida sobre el storage de la sesión."""
        return abandonar_transicion_consumida(
            storage_root=self._storage_root,
            transition_id=transition_id,
            identidad=identidad,
            motivo=motivo,
            timeout=timeout,
            clock_ns=self._clock_ns,
        )


__all__ = [
    "CANDIDATE_OBJECT",
    "EMISION_PRODUCTIVA_ABIERTA_POR_ESTE_MODULO",
    "PRIMERA_TRANSICION_REAL",
    "REANUDACION_POST_PENDING",
    "SCHEMA_VERSION_ABANDONO",
    "TRANSPORT_ADAPTER",
    "AbandonoCorruptoError",
    "AbandonoTransicionInvalidoError",
    "AutorizacionTransicionError",
    "DiagnosticoDurable",
    "EscenarioTransicion",
    "EstadoDurable",
    "EvidenciaTransicion",
    "EvidenciaTransicionError",
    "OrigenTransicion",
    "RegistroAbandonoTransicion",
    "ReanudacionNoDisponibleError",
    "ReanudacionRequiereDuenoError",
    "ResultadoTransicion",
    "SesionFueraDeOrdenError",
    "SesionTransicionGoldenMaster",
    "SolicitudTransicion",
    "TransicionEnCursoError",
    "TransicionYaActivaError",
    "abandonar_transicion_consumida",
    "cargar_registro_abandono",
    "derivar_evidencia_transicion",
    "diagnosticar_estado_durable",
]

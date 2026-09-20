"""Contrato puro del Pre-LOD Material Pipeline (P1a).

Este módulo define el dominio y sus invariantes SIN runtime: no ejecuta
herramientas externas, no toca el filesystem, MO2, VFS, ni importa
infraestructura. Es importable desde cualquier capa.

Contexto (leer antes de modificar):
- Evidencia P0: ``docs/design/research/2026-09-19-pre-lod-material-p0-evidence.md``
  (prevalece sobre el plan v3 en B7/B10, contratos por versión, B8/B9 y first cut).
- Diseño: ``docs/design/plans/2026-08-19-pre-lod-material-pipeline-v3.md``.

Decisiones congeladas acá:
- Stage 9 sigue siendo ``TexGen -> DynDOLOD``. Los materiales son una
  *capability* semántica (``pre_lod_materials``) que converge ANTES de Stage 9;
  no hay etapa nueva ni renumeración.
- First cut = PGPatcher only (P0 §18). VRAMr/ParallaxR/BENDr quedan declarados
  como BLOCKED con sus blockers (B3 / B6-L); no se implementan en P1a.
- Política conservadora B10 (P0 §8.5): un rerun de Stage 5/6/7 deja el output
  de ``PGPATCHER`` STALE y arrastra a ``POST_PG_RECONCILE``. El estado de VRAMr
  depende de la política de orden PG↔VRAMr y NO se congela acá.
- PGPatcher ↔ VRAMr: este contrato es NEUTRAL, sin arista de orden. B9/P0
  soporta POLICY_A (VRAMr -> PGPatcher) y POLICY_B (PGPatcher -> VRAMr); la
  relación la resuelve el planner (P1b), no P1a.
- Contratos PGPatcher por versión conocida, sin rango abierto ni herencia
  SemVer (P0 §9 / B8).

Fuera de alcance: ejecución, scheduling, máquina de estados persistente,
transacciones MO2, VFS, output ownership real y detección de versión (PE).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

# =============================================================================
# CAPABILITY SEMÁNTICA
# =============================================================================

#: Identificador de la capability pre-LOD. No renombra ni renumera Stage 9:
#: la convergencia de materiales ocurre entre Stage 7/8 y Stage 9 (TexGen -> DynDOLOD).
PRE_LOD_MATERIALS_CAPABILITY: Final = "pre_lod_materials"

#: Etapas cuyo rerun deja STALE el output de PGPATCHER (política conservadora P0 §8.5).
UPSTREAM_MUTATION_STAGES: Final[frozenset[int]] = frozenset({5, 6, 7})


# =============================================================================
# ERRORES DE CONTRATO
# =============================================================================


class MaterialContractError(ValueError):
    """Violación de un invariante del contrato material."""


class MaterialPlanError(MaterialContractError):
    """Plan de materiales inválido: pasos desconocidos, duplicados, dependencias u orden."""


# =============================================================================
# VOCABULARIO
# =============================================================================


class MaterialStepId(StrEnum):
    """IDs estables de los nodos materiales (Stage 9 NO es uno de ellos)."""

    PARALLAXR = "parallaxr"
    BENDR = "bendr"
    PGPATCHER = "pgpatcher"
    VRAMR = "vramr"
    POST_PG_RECONCILE = "post_pg_reconcile"


class MaterialReadiness(StrEnum):
    """Readiness de IMPLEMENTACIÓN (no estado de código ni de corrida).

    FIRST_CUT: autorizado para el primer corte. Nada está implementado todavía:
    P1a es solo contrato.
    DEFERRED: fuera del primer corte, sin blocker duro declarado.
    BLOCKED: fuera del primer corte con blocker abierto que exige evidencia o permiso.
    """

    FIRST_CUT = "first_cut"
    DEFERRED = "deferred"
    BLOCKED = "blocked"


class MaterialBlocker(StrEnum):
    """Blockers de evidencia abiertos que impiden implementar un nodo."""

    B3 = "B3"  # VRAMr: entry point headless soportado — REAL_RIG_REQUIRED (P0 §19)
    B6_L = "B6-L"  # R-suite: permiso de invocación directa de helpers — OPEN (P0 §7)


class MaterialCapability(StrEnum):
    """Capacidades que un nodo requiere o produce (metadata descriptiva).

    ``PLUGIN_OUTPUT`` es condicional: PGPatcher puede no generar plugins si no
    modificó records (P0 §8.1); el reconcile es entonces un no-op.
    """

    VFS_LAUNCH = "vfs_launch"  # correr bajo la vista virtual de MO2 (P5: GO arquitectónico / HOLD operacional)
    MESH_OUTPUT = "mesh_output"
    TEXTURE_OUTPUT = "texture_output"
    PLUGIN_OUTPUT = "plugin_output"
    PLUGIN_SORT = "plugin_sort"


class MaterialNodeState(StrEnum):
    """Estados conceptuales de un nodo. SOLO enum: no hay máquina de estados acá."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    STALE = "stale"
    QUARANTINED = "quarantined"


# =============================================================================
# GRAFO DECLARATIVO (datos, no engine)
# =============================================================================


@dataclass(frozen=True, slots=True)
class MaterialNodeSpec:
    """Especificación declarativa de un nodo material.

    ``ordered_after``: arista blanda — si la dependencia está en el plan, debe
    preceder a este nodo; su ausencia NO invalida el plan.
    ``requires_present``: arista dura — la dependencia debe estar en el plan.
    """

    step_id: MaterialStepId
    readiness: MaterialReadiness
    optional: bool
    ordered_after: tuple[MaterialStepId, ...] = ()
    requires_present: tuple[MaterialStepId, ...] = ()
    requires_capabilities: frozenset[MaterialCapability] = frozenset()
    produces_capabilities: frozenset[MaterialCapability] = frozenset()
    invalidated_by_stages: frozenset[int] = frozenset()
    blockers: frozenset[MaterialBlocker] = frozenset()

    def __post_init__(self) -> None:
        """Valida coherencia interna del nodo (falla cerrado en import)."""
        if self.step_id in self.ordered_after or self.step_id in self.requires_present:
            raise MaterialContractError(f"{self.step_id.value} no puede depender de sí mismo")
        if self.readiness is MaterialReadiness.BLOCKED and not self.blockers:
            raise MaterialContractError(f"{self.step_id.value} está BLOCKED sin blocker declarado")
        if self.readiness is not MaterialReadiness.BLOCKED and self.blockers:
            raise MaterialContractError(f"{self.step_id.value} declara blockers sin estar BLOCKED")
        fuera_de_rango = {stage for stage in self.invalidated_by_stages if not 1 <= stage <= 9}
        if fuera_de_rango:
            raise MaterialContractError(
                f"{self.step_id.value} declara etapas fuera del pipeline 1..9: {sorted(fuera_de_rango)}"
            )


def _build_pipeline() -> Mapping[MaterialStepId, MaterialNodeSpec]:
    """Construye el grafo declarativo y verifica sus referencias (pura)."""
    specs = (
        MaterialNodeSpec(
            step_id=MaterialStepId.PARALLAXR,
            readiness=MaterialReadiness.BLOCKED,
            optional=True,
            blockers=frozenset({MaterialBlocker.B6_L}),
            produces_capabilities=frozenset({MaterialCapability.TEXTURE_OUTPUT}),
        ),
        MaterialNodeSpec(
            step_id=MaterialStepId.BENDR,
            readiness=MaterialReadiness.BLOCKED,
            optional=True,
            ordered_after=(MaterialStepId.PARALLAXR,),
            blockers=frozenset({MaterialBlocker.B6_L}),
            # La evidencia de BENDr es de normal maps/texturas procesadas, no de
            # meshes: declarar MESH_OUTPUT haría que ManagedOutput los espere.
            produces_capabilities=frozenset({MaterialCapability.TEXTURE_OUTPUT}),
        ),
        MaterialNodeSpec(
            step_id=MaterialStepId.PGPATCHER,
            readiness=MaterialReadiness.FIRST_CUT,
            optional=False,
            ordered_after=(MaterialStepId.PARALLAXR, MaterialStepId.BENDR),
            requires_capabilities=frozenset({MaterialCapability.VFS_LAUNCH}),
            produces_capabilities=frozenset(
                {
                    MaterialCapability.MESH_OUTPUT,
                    MaterialCapability.TEXTURE_OUTPUT,
                    MaterialCapability.PLUGIN_OUTPUT,
                }
            ),
            invalidated_by_stages=UPSTREAM_MUTATION_STAGES,
        ),
        MaterialNodeSpec(
            step_id=MaterialStepId.VRAMR,
            readiness=MaterialReadiness.BLOCKED,
            optional=True,
            # Sin arista PG↔VRAMr: B9/P0 soporta POLICY_A y POLICY_B; un
            # ordered_after fijo convertiría una de las dos en inválida.
            blockers=frozenset({MaterialBlocker.B3, MaterialBlocker.B6_L}),
            produces_capabilities=frozenset({MaterialCapability.TEXTURE_OUTPUT}),
        ),
        MaterialNodeSpec(
            step_id=MaterialStepId.POST_PG_RECONCILE,
            readiness=MaterialReadiness.FIRST_CUT,
            optional=False,
            ordered_after=(MaterialStepId.PGPATCHER, MaterialStepId.VRAMR),
            requires_present=(MaterialStepId.PGPATCHER,),
            requires_capabilities=frozenset({MaterialCapability.PLUGIN_OUTPUT}),
            produces_capabilities=frozenset({MaterialCapability.PLUGIN_SORT}),
        ),
    )
    pipeline = MappingProxyType({spec.step_id: spec for spec in specs})
    _validate_graph(pipeline)
    return pipeline


def _validate_graph(pipeline: Mapping[MaterialStepId, MaterialNodeSpec]) -> None:
    """Congela la consistencia del grafo: referencias existentes y orden compatible."""
    index = {step: position for position, step in enumerate(MaterialStepId)}
    for spec in pipeline.values():
        for dependency in spec.ordered_after + spec.requires_present:
            if dependency not in pipeline:
                raise MaterialContractError(f"{spec.step_id.value} referencia un paso inexistente: {dependency}")
            if index[dependency] >= index[spec.step_id]:
                raise MaterialContractError(
                    f"orden canónico inconsistente: {dependency.value} debe preceder a {spec.step_id.value}"
                )


#: Grafo material declarativo (inmutable). El orden canónico es el de declaración.
MATERIAL_PIPELINE: Final[Mapping[MaterialStepId, MaterialNodeSpec]] = _build_pipeline()

#: Orden canónico de los nodos (declaración del enum; las aristas lo respetan).
MATERIAL_STEP_ORDER: Final[tuple[MaterialStepId, ...]] = tuple(MaterialStepId)


# =============================================================================
# OPERACIONES PURAS
# =============================================================================


def resolve_material_order(steps: Iterable[MaterialStepId]) -> tuple[MaterialStepId, ...]:
    """Devuelve el orden canónico del subconjunto de pasos, validándolo.

    Valida pasos representables y aristas duras (``requires_present``). Las
    aristas blandas (``ordered_after``) se cumplen por construcción del orden
    canónico. El orden canónico NO expresa la política PG↔VRAMr (ausente por
    diseño); el planner de P1b decide POLICY_A o POLICY_B. Levanta
    :class:`MaterialPlanError` si el plan es inválido.
    """
    plan = frozenset(steps)
    desconocidos = sorted(str(step) for step in plan if step not in MATERIAL_PIPELINE)
    if desconocidos:
        raise MaterialPlanError(f"pasos materiales desconocidos: {', '.join(desconocidos)}")
    for spec in MATERIAL_PIPELINE.values():
        if spec.step_id not in plan:
            continue
        faltantes = [dependency.value for dependency in spec.requires_present if dependency not in plan]
        if faltantes:
            raise MaterialPlanError(f"{spec.step_id.value} requiere en el plan: {', '.join(faltantes)}")
    return tuple(step for step in MATERIAL_STEP_ORDER if step in plan)


def validate_material_order(steps: Sequence[MaterialStepId]) -> None:
    """Valida una secuencia explícita de pasos, orden incluido.

    Levanta :class:`MaterialPlanError` ante pasos desconocidos, duplicados,
    dependencias duras ausentes o una dependencia ubicada después del nodo.
    """
    orden = tuple(steps)
    desconocidos = sorted(str(step) for step in orden if step not in MATERIAL_PIPELINE)
    if desconocidos:
        raise MaterialPlanError(f"pasos materiales desconocidos: {', '.join(desconocidos)}")
    if len(set(orden)) != len(orden):
        raise MaterialPlanError("el plan material tiene pasos duplicados")
    posicion = {step: indice for indice, step in enumerate(orden)}
    for spec in MATERIAL_PIPELINE.values():
        if spec.step_id not in posicion:
            continue
        for dependency in spec.requires_present:
            if dependency not in posicion:
                raise MaterialPlanError(f"{spec.step_id.value} requiere en el plan: {dependency.value}")
        for dependency in spec.ordered_after + spec.requires_present:
            if dependency in posicion and posicion[dependency] >= posicion[spec.step_id]:
                raise MaterialPlanError(f"{dependency.value} debe preceder a {spec.step_id.value}")


def invalidated_material_steps(rerun_stages: Iterable[int]) -> frozenset[MaterialStepId]:
    """Clausura de invalidación por rerun de etapas upstream (política B10, P0 §8.5).

    Un rerun de Stage 5/6/7 deja el output de PGPatcher STALE; la invalidación
    se propaga hacia adelante por las aristas declaradas: ``POST_PG_RECONCILE``
    (requiere PGPatcher) también queda STALE. ``VRAMR`` NO se invalida en el
    contrato: su staleness depende de la política de orden PG↔VRAMr (B9) y la
    resolverá el planner. La recuperación es
    ``PGPatcher -> POST_PG_RECONCILE -> convergencia`` (usar ``resolve_material_order``).
    """
    stages = frozenset(rerun_stages)
    invalidated = {spec.step_id for spec in MATERIAL_PIPELINE.values() if stages & spec.invalidated_by_stages}
    changed = True
    while changed:
        changed = False
        for spec in MATERIAL_PIPELINE.values():
            if spec.step_id in invalidated:
                continue
            dependencias = set(spec.ordered_after) | set(spec.requires_present)
            if dependencias & invalidated:
                invalidated.add(spec.step_id)
                changed = True
    return frozenset(invalidated)


# =============================================================================
# CONTRATOS CONOCIDOS DE PGPATCHER (B8: sin rango abierto)
# =============================================================================

#: Clave de herramienta de PGPatcher en el dominio material.
PGPATCHER_TOOL_KEY: Final = "pgpatcher"


class PgPatcherCapability(StrEnum):
    """Capacidades de PGPatcher por contrato conocido (P0 §9)."""

    AUTOSTART = "autostart"  # desde 0.5.0
    VFS_CHECK_IGNORE = "vfs_check_ignore"  # --ignore-mo2vfscheck, desde 1.1.0
    CONSOLE = "console"  # --console, desde 0.9.9
    EXCLUDE_FACEGENS = "exclude_facegens"  # desde 1.2.0
    ESM_MODE_CLI = "esm_mode_cli"  # --esm-all / --no-esm, desde 1.3.0
    UPDATE_OUTPUT = "update_output"  # --autostart-update + cache, desde 2.0.0
    PBR_JSON_SCHEMA_V2 = "pbr_json_schema_v2"  # BREAKING de campos PBR, desde 2.0.0


@dataclass(frozen=True, slots=True)
class ToolVersionContract:
    """Contrato de VERSIÓN conocido de una herramienta (no es un fingerprint de artefacto).

    ``version`` es clave EXACTA de reconocimiento: no hay comparación SemVer.
    Una versión ausente del registro no hereda compatibilidad de otra; las
    fases posteriores deben fallar cerrado ante ella (``VERSION_UNSUPPORTED``).

    No incluye ``exe_sha256``: la identidad binaria del artefacto la produce la
    detección de versión/artefacto (P3/P9). P0 solo tiene fingerprint de artefacto
    verificado para el paquete 1.2.0 (P0 §9) y ese dato NO vive en esta estructura.
    """

    tool_key: str
    version: str
    capabilities: frozenset[PgPatcherCapability]


_BASE_PGPATCHER_CAPABILITIES: Final[frozenset[PgPatcherCapability]] = frozenset(
    {
        PgPatcherCapability.AUTOSTART,
        PgPatcherCapability.VFS_CHECK_IGNORE,
        PgPatcherCapability.CONSOLE,
        PgPatcherCapability.EXCLUDE_FACEGENS,
    }
)


def _pgpatcher_version_contract(version: str, *extras: PgPatcherCapability) -> ToolVersionContract:
    """Construye el contrato de versión conocido con sus capacidades."""
    return ToolVersionContract(PGPATCHER_TOOL_KEY, version, frozenset({*_BASE_PGPATCHER_CAPABILITIES, *extras}))


#: Versiones de PGPatcher con contrato verificado en P0 (no es un rango abierto).
PGPATCHER_KNOWN_CONTRACTS: Final[Mapping[str, ToolVersionContract]] = MappingProxyType(
    {
        "1.2.0": _pgpatcher_version_contract("1.2.0"),
        "1.3.0": _pgpatcher_version_contract("1.3.0", PgPatcherCapability.ESM_MODE_CLI),
        "2.0.0": _pgpatcher_version_contract(
            "2.0.0",
            PgPatcherCapability.ESM_MODE_CLI,
            PgPatcherCapability.UPDATE_OUTPUT,
            PgPatcherCapability.PBR_JSON_SCHEMA_V2,
        ),
        "2.1.0": _pgpatcher_version_contract(
            "2.1.0",
            PgPatcherCapability.ESM_MODE_CLI,
            PgPatcherCapability.UPDATE_OUTPUT,
            PgPatcherCapability.PBR_JSON_SCHEMA_V2,
        ),
        "2.1.1": _pgpatcher_version_contract(
            "2.1.1",
            PgPatcherCapability.ESM_MODE_CLI,
            PgPatcherCapability.UPDATE_OUTPUT,
            PgPatcherCapability.PBR_JSON_SCHEMA_V2,
        ),
    }
)


def resolve_pgpatcher_contract(version: str) -> ToolVersionContract | None:
    """Devuelve el contrato conocido de PGPatcher o ``None``.

    Sin herencia por SemVer: ``2.2.0`` o ``3.0.0`` no son compatibles con
    ``2.1.1`` por comparación de versiones. Un ``None`` obliga a las fases
    posteriores a fallar cerrado hasta registrar y verificar el contrato nuevo.
    """
    return PGPATCHER_KNOWN_CONTRACTS.get(version)

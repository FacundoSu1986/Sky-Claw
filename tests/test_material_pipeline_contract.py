"""Anclas del contrato puro del Pre-LOD Material Pipeline (P1a).

Tests puros: sin subprocess, sin filesystem, sin MO2/VFS, sin red.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from itertools import combinations
from types import MappingProxyType

import pytest

from sky_claw.local.tools.material_contract import (
    MATERIAL_PIPELINE,
    MATERIAL_STEP_ORDER,
    PGPATCHER_KNOWN_CONTRACTS,
    PGPATCHER_TOOL_KEY,
    PRE_LOD_MATERIALS_CAPABILITY,
    MaterialBlocker,
    MaterialCapability,
    MaterialContractError,
    MaterialNodeSpec,
    MaterialNodeState,
    MaterialPlanError,
    MaterialReadiness,
    MaterialStepId,
    PgPatcherCapability,
    ToolVersionContract,
    _validate_graph,
    invalidated_material_steps,
    resolve_material_order,
    resolve_pgpatcher_contract,
    validate_material_order,
)


def test_stage_9_no_es_un_material_step() -> None:
    """Stage 9 (TexGen -> DynDOLOD) no existe en el vocabulario material."""
    assert {step.value for step in MaterialStepId} == {
        "parallaxr",
        "bendr",
        "pgpatcher",
        "vramr",
        "post_pg_reconcile",
    }
    assert PRE_LOD_MATERIALS_CAPABILITY == "pre_lod_materials"


def test_orden_canonico_congelado() -> None:
    assert tuple(step.value for step in MATERIAL_STEP_ORDER) == (
        "parallaxr",
        "bendr",
        "pgpatcher",
        "vramr",
        "post_pg_reconcile",
    )


def test_estados_conceptuales_congelados() -> None:
    assert {state.value for state in MaterialNodeState} == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "skipped",
        "stale",
        "quarantined",
    }


def test_readiness_del_first_cut() -> None:
    assert MATERIAL_PIPELINE[MaterialStepId.PGPATCHER].readiness is MaterialReadiness.FIRST_CUT
    assert MATERIAL_PIPELINE[MaterialStepId.POST_PG_RECONCILE].readiness is MaterialReadiness.FIRST_CUT
    for step in (MaterialStepId.PARALLAXR, MaterialStepId.BENDR, MaterialStepId.VRAMR):
        assert MATERIAL_PIPELINE[step].readiness is MaterialReadiness.BLOCKED


def test_los_nodos_bloqueados_declaran_sus_blockers() -> None:
    assert MATERIAL_PIPELINE[MaterialStepId.VRAMR].blockers == frozenset({MaterialBlocker.B3, MaterialBlocker.B6_L})
    assert MATERIAL_PIPELINE[MaterialStepId.PARALLAXR].blockers == frozenset({MaterialBlocker.B6_L})
    assert MATERIAL_PIPELINE[MaterialStepId.BENDR].blockers == frozenset({MaterialBlocker.B6_L})
    assert MATERIAL_PIPELINE[MaterialStepId.PGPATCHER].blockers == frozenset()
    assert MATERIAL_PIPELINE[MaterialStepId.POST_PG_RECONCILE].blockers == frozenset()


def test_ningun_paso_material_es_obligatorio() -> None:
    """Decisión del 27/10 (``np-pilas-y-rutas-de-malla.md`` §1.3): PGPatcher es opcional, no requisito; la herramienta funciona sin él.

    El reconcile solo existe cuando PGPatcher está en el plan (``requires_present``), así que tampoco puede ser obligatorio. Se enumera el
    grafo entero por igualdad literal: un paso nuevo obliga a decidir si es obligatorio. ``readiness`` (qué se implementa primero) y
    ``optional`` (si un plan puede omitirlo) son ejes distintos: PGPatcher sigue siendo ``FIRST_CUT``.
    """
    assert {paso: spec.optional for paso, spec in MATERIAL_PIPELINE.items()} == {
        MaterialStepId.PARALLAXR: True,
        MaterialStepId.BENDR: True,
        MaterialStepId.PGPATCHER: True,
        MaterialStepId.VRAMR: True,
        MaterialStepId.POST_PG_RECONCILE: True,
    }


def _subconjuntos_de_pasos() -> Iterator[frozenset[MaterialStepId]]:
    for tamano in range(len(MaterialStepId) + 1):
        for combinacion in combinations(MaterialStepId, tamano):
            yield frozenset(combinacion)


@pytest.mark.parametrize(
    "plan", list(_subconjuntos_de_pasos()), ids=lambda p: "+".join(sorted(x.value for x in p)) or "vacio"
)
def test_un_plan_es_valido_si_y_solo_si_trae_las_dependencias_duras_de_cada_paso(
    plan: frozenset[MaterialStepId],
) -> None:
    """Los 2^5 planes posibles, uno por uno: la validez la decide ``requires_present`` y nada más (ni ``optional``, ni ``readiness``)."""
    dependencias_cumplidas = all(set(MATERIAL_PIPELINE[paso].requires_present) <= plan for paso in plan)

    if dependencias_cumplidas:
        assert resolve_material_order(plan) == tuple(paso for paso in MATERIAL_STEP_ORDER if paso in plan)
        validate_material_order(resolve_material_order(plan))
    else:
        with pytest.raises(MaterialPlanError):
            resolve_material_order(plan)


def test_la_herramienta_funciona_sin_pgpatcher_en_todos_los_planes_posibles() -> None:
    """La propiedad que importa del lado del producto: omitir PGPatcher (y con él su reconcile) nunca invalida un plan."""
    sin_pgpatcher = [
        plan
        for plan in _subconjuntos_de_pasos()
        if MaterialStepId.PGPATCHER not in plan and MaterialStepId.POST_PG_RECONCILE not in plan
    ]
    assert len(sin_pgpatcher) == 2 ** (len(MaterialStepId) - 2)  # los 8 planes de la R-suite: ninguno puede fallar
    for plan in sin_pgpatcher:
        assert resolve_material_order(plan) == tuple(paso for paso in MATERIAL_STEP_ORDER if paso in plan)


def test_un_rerun_upstream_sin_pgpatcher_en_el_plan_no_invalida_nada_del_plan() -> None:
    """``invalidated_material_steps`` es del grafo entero, no del plan: el planner la intersecta con sus pasos."""
    plan = frozenset({MaterialStepId.VRAMR, MaterialStepId.BENDR})

    assert invalidated_material_steps({5, 6, 7}) & plan == frozenset()


def test_el_grafo_real_no_tiene_un_paso_obligatorio_que_exija_uno_opcional() -> None:
    for spec in MATERIAL_PIPELINE.values():
        for dependencia in spec.requires_present:
            assert spec.optional or not MATERIAL_PIPELINE[dependencia].optional, (spec.step_id, dependencia)


def test_un_paso_obligatorio_que_exige_la_presencia_de_uno_opcional_se_rechaza_al_validar_el_grafo() -> None:
    """La contradicción de #834/np-pilas: el reconcile era obligatorio y exigía un PGPatcher que la decisión del 27/10 volvió opcional."""
    opcional = MaterialNodeSpec(MaterialStepId.PGPATCHER, MaterialReadiness.FIRST_CUT, optional=True)
    obligatorio = MaterialNodeSpec(
        MaterialStepId.POST_PG_RECONCILE,
        MaterialReadiness.FIRST_CUT,
        optional=False,
        requires_present=(MaterialStepId.PGPATCHER,),
    )

    with pytest.raises(MaterialContractError, match="obligatorio.*opcional"):
        _validate_graph(MappingProxyType({spec.step_id: spec for spec in (opcional, obligatorio)}))


def test_pgpatcher_requiere_vfs_y_produce_plugins() -> None:
    spec = MATERIAL_PIPELINE[MaterialStepId.PGPATCHER]
    assert MaterialCapability.VFS_LAUNCH in spec.requires_capabilities
    assert MaterialCapability.PLUGIN_OUTPUT in spec.produces_capabilities


def test_el_reconcile_consume_plugins_de_pgpatcher() -> None:
    spec = MATERIAL_PIPELINE[MaterialStepId.POST_PG_RECONCILE]
    assert spec.requires_present == (MaterialStepId.PGPATCHER,)
    assert MaterialCapability.PLUGIN_OUTPUT in spec.requires_capabilities
    assert MaterialCapability.PLUGIN_SORT in spec.produces_capabilities


def test_bendr_solo_produce_texturas() -> None:
    """La evidencia de BENDr es normal maps/texturas procesadas, no meshes."""
    spec = MATERIAL_PIPELINE[MaterialStepId.BENDR]
    assert MaterialCapability.TEXTURE_OUTPUT in spec.produces_capabilities
    assert MaterialCapability.MESH_OUTPUT not in spec.produces_capabilities


def test_vramr_no_declara_orden_fijo_con_pgpatcher() -> None:
    """B9/P0: P1a es neutral; POLICY_A y POLICY_B las resuelve el planner (P1b)."""
    spec = MATERIAL_PIPELINE[MaterialStepId.VRAMR]
    assert spec.ordered_after == ()
    assert spec.requires_present == ()


def test_dependencias_por_nodo_son_exactas() -> None:
    """Igualdad exhaustiva de aristas por nodo: blanda (``ordered_after``) y dura (``requires_present``)."""
    assert {step: spec.ordered_after for step, spec in MATERIAL_PIPELINE.items()} == {
        MaterialStepId.PARALLAXR: (),
        MaterialStepId.BENDR: (MaterialStepId.PARALLAXR,),
        MaterialStepId.PGPATCHER: (MaterialStepId.PARALLAXR, MaterialStepId.BENDR),
        MaterialStepId.VRAMR: (),
        MaterialStepId.POST_PG_RECONCILE: (MaterialStepId.PGPATCHER, MaterialStepId.VRAMR),
    }
    assert {step: spec.requires_present for step, spec in MATERIAL_PIPELINE.items()} == {
        MaterialStepId.PARALLAXR: (),
        MaterialStepId.BENDR: (),
        MaterialStepId.PGPATCHER: (),
        MaterialStepId.VRAMR: (),
        MaterialStepId.POST_PG_RECONCILE: (MaterialStepId.PGPATCHER,),
    }


def test_capabilities_por_nodo_son_exactas() -> None:
    """Igualdad exhaustiva por nodo: una capability extra o faltante rompe el contrato."""
    assert {step: spec.requires_capabilities for step, spec in MATERIAL_PIPELINE.items()} == {
        MaterialStepId.PARALLAXR: frozenset(),
        MaterialStepId.BENDR: frozenset(),
        MaterialStepId.PGPATCHER: frozenset({MaterialCapability.VFS_LAUNCH}),
        MaterialStepId.VRAMR: frozenset(),
        MaterialStepId.POST_PG_RECONCILE: frozenset({MaterialCapability.PLUGIN_OUTPUT}),
    }
    assert {step: spec.produces_capabilities for step, spec in MATERIAL_PIPELINE.items()} == {
        MaterialStepId.PARALLAXR: frozenset({MaterialCapability.TEXTURE_OUTPUT}),
        MaterialStepId.BENDR: frozenset({MaterialCapability.TEXTURE_OUTPUT}),
        MaterialStepId.PGPATCHER: frozenset(
            {
                MaterialCapability.MESH_OUTPUT,
                MaterialCapability.TEXTURE_OUTPUT,
                MaterialCapability.PLUGIN_OUTPUT,
            }
        ),
        MaterialStepId.VRAMR: frozenset({MaterialCapability.TEXTURE_OUTPUT}),
        MaterialStepId.POST_PG_RECONCILE: frozenset({MaterialCapability.PLUGIN_SORT}),
    }


def test_solo_pgpatcher_declara_etapas_invalidadoras() -> None:
    assert MATERIAL_PIPELINE[MaterialStepId.PGPATCHER].invalidated_by_stages == frozenset({5, 6, 7})
    for step in (
        MaterialStepId.PARALLAXR,
        MaterialStepId.BENDR,
        MaterialStepId.VRAMR,
        MaterialStepId.POST_PG_RECONCILE,
    ):
        assert MATERIAL_PIPELINE[step].invalidated_by_stages == frozenset()


@pytest.mark.parametrize(
    ("plan", "esperado"),
    [
        ({MaterialStepId.PGPATCHER}, (MaterialStepId.PGPATCHER,)),
        (
            {MaterialStepId.PARALLAXR, MaterialStepId.PGPATCHER},
            (MaterialStepId.PARALLAXR, MaterialStepId.PGPATCHER),
        ),
        (
            {MaterialStepId.PARALLAXR, MaterialStepId.BENDR, MaterialStepId.PGPATCHER},
            (MaterialStepId.PARALLAXR, MaterialStepId.BENDR, MaterialStepId.PGPATCHER),
        ),
        (
            {MaterialStepId.PGPATCHER, MaterialStepId.VRAMR},
            (MaterialStepId.PGPATCHER, MaterialStepId.VRAMR),
        ),
        (
            {MaterialStepId.PGPATCHER, MaterialStepId.POST_PG_RECONCILE},
            (MaterialStepId.PGPATCHER, MaterialStepId.POST_PG_RECONCILE),
        ),
        (
            {
                MaterialStepId.PARALLAXR,
                MaterialStepId.BENDR,
                MaterialStepId.PGPATCHER,
                MaterialStepId.VRAMR,
                MaterialStepId.POST_PG_RECONCILE,
            },
            MATERIAL_STEP_ORDER,
        ),
    ],
)
def test_caminos_parciales_validos(plan: frozenset[MaterialStepId], esperado: tuple[MaterialStepId, ...]) -> None:
    assert resolve_material_order(plan) == esperado


def test_reconcile_requiere_pgpatcher_presente() -> None:
    with pytest.raises(MaterialPlanError):
        resolve_material_order({MaterialStepId.POST_PG_RECONCILE})


def test_pasos_no_representables_fallan_cerrado() -> None:
    """Stage 9 no puede colarse como paso material ni siquiera por string crudo."""
    with pytest.raises(MaterialPlanError):
        resolve_material_order({"stage9"})  # type: ignore[arg-type]
    with pytest.raises(MaterialPlanError):
        validate_material_order(("stage9",))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "orden_invalido",
    [
        (MaterialStepId.POST_PG_RECONCILE, MaterialStepId.PGPATCHER),
        (MaterialStepId.BENDR, MaterialStepId.PARALLAXR),
        (MaterialStepId.PGPATCHER, MaterialStepId.PGPATCHER),
        (MaterialStepId.PGPATCHER, MaterialStepId.PARALLAXR),
        (MaterialStepId.PGPATCHER, MaterialStepId.BENDR),
        (MaterialStepId.PGPATCHER, MaterialStepId.POST_PG_RECONCILE, MaterialStepId.VRAMR),
        (MaterialStepId.POST_PG_RECONCILE,),
    ],
)
def test_orden_invalido_es_detectado(orden_invalido: tuple[MaterialStepId, ...]) -> None:
    with pytest.raises(MaterialPlanError):
        validate_material_order(orden_invalido)


def test_ordenes_validos_son_aceptados() -> None:
    validate_material_order(MATERIAL_STEP_ORDER)
    validate_material_order((MaterialStepId.PARALLAXR, MaterialStepId.PGPATCHER))
    validate_material_order((MaterialStepId.PGPATCHER, MaterialStepId.VRAMR, MaterialStepId.POST_PG_RECONCILE))


def test_bendr_no_requiere_presencia_de_parallaxr() -> None:
    """``ordered_after`` es blanda: BENDr solo exige precedencia si ParallaxR está en el plan."""
    validate_material_order((MaterialStepId.BENDR,))


@pytest.mark.parametrize(
    "orden",
    [
        (MaterialStepId.VRAMR, MaterialStepId.PGPATCHER, MaterialStepId.POST_PG_RECONCILE),
        (MaterialStepId.PGPATCHER, MaterialStepId.VRAMR, MaterialStepId.POST_PG_RECONCILE),
    ],
)
def test_ambas_politicas_de_orden_vramr_pgpatcher_son_validas(orden: tuple[MaterialStepId, ...]) -> None:
    """B9/P0: POLICY_A (VRAMr -> PGPatcher) y POLICY_B (PGPatcher -> VRAMr)."""
    validate_material_order(orden)


@pytest.mark.parametrize("stage", [5, 6, 7])
def test_rerun_de_etapas_upstream_invalida_materiales(stage: int) -> None:
    """Política conservadora B10: 5/6/7 -> PGPatcher STALE y arrastre al reconcile.

    VRAMr NO se invalida en el contrato: su staleness depende de la política de
    orden PG↔VRAMr (B9), que P1a no codifica.
    """
    invalidados = invalidated_material_steps({stage})
    assert invalidados == frozenset({MaterialStepId.PGPATCHER, MaterialStepId.POST_PG_RECONCILE})
    assert MaterialStepId.VRAMR not in invalidados


@pytest.mark.parametrize("stage", [1, 2, 3, 4, 8, 9])
def test_etapas_fuera_de_la_politica_no_invalidan(stage: int) -> None:
    """El alcance 5-7 es el congelado en P0 §8.5; no se extrapola a otras etapas."""
    assert invalidated_material_steps({stage}) == frozenset()


def test_tras_la_invalidacion_pgpatcher_precede_al_reconcile() -> None:
    invalidados = invalidated_material_steps({5})
    reorden = resolve_material_order(invalidados)
    assert reorden.index(MaterialStepId.PGPATCHER) < reorden.index(MaterialStepId.POST_PG_RECONCILE)


def test_contratos_pgpatcher_conocidos() -> None:
    assert set(PGPATCHER_KNOWN_CONTRACTS) == {"1.2.0", "1.3.0", "2.0.0", "2.1.0", "2.1.1"}
    for version in PGPATCHER_KNOWN_CONTRACTS:
        assert PGPATCHER_KNOWN_CONTRACTS[version].tool_key == PGPATCHER_TOOL_KEY


@pytest.mark.parametrize("version", ["1.2.0", "1.3.0", "2.0.0", "2.1.0", "2.1.1"])
def test_version_conocida_resuelve_contrato(version: str) -> None:
    contrato = resolve_pgpatcher_contract(version)
    assert contrato is not None
    assert contrato.version == version
    assert PgPatcherCapability.AUTOSTART in contrato.capabilities


@pytest.mark.parametrize("version", sorted(PGPATCHER_KNOWN_CONTRACTS))
def test_resolver_devuelve_exactamente_el_contrato_registrado(version: str) -> None:
    """Igualdad estructural: el resolver no transforma ni agrega capabilities."""
    assert resolve_pgpatcher_contract(version) == PGPATCHER_KNOWN_CONTRACTS[version]


def test_capacidades_llegan_por_version_de_contrato() -> None:
    v1_2 = resolve_pgpatcher_contract("1.2.0")
    v1_3 = resolve_pgpatcher_contract("1.3.0")
    v2_0 = resolve_pgpatcher_contract("2.0.0")
    assert v1_2 is not None and v1_3 is not None and v2_0 is not None
    assert PgPatcherCapability.ESM_MODE_CLI not in v1_2.capabilities
    assert PgPatcherCapability.ESM_MODE_CLI in v1_3.capabilities
    assert PgPatcherCapability.UPDATE_OUTPUT not in v1_3.capabilities
    assert PgPatcherCapability.UPDATE_OUTPUT in v2_0.capabilities
    assert PgPatcherCapability.PBR_JSON_SCHEMA_V2 in v2_0.capabilities


def test_capabilities_por_contrato_pgpatcher_son_exactas() -> None:
    """Igualdad exhaustiva de capabilities por versión conocida (incluye 2.1.0 y 2.1.1)."""
    base = {
        PgPatcherCapability.AUTOSTART,
        PgPatcherCapability.VFS_CHECK_IGNORE,
        PgPatcherCapability.CONSOLE,
        PgPatcherCapability.EXCLUDE_FACEGENS,
    }
    con_esm = {*base, PgPatcherCapability.ESM_MODE_CLI}
    con_update = {*con_esm, PgPatcherCapability.UPDATE_OUTPUT, PgPatcherCapability.PBR_JSON_SCHEMA_V2}
    assert {version: contrato.capabilities for version, contrato in PGPATCHER_KNOWN_CONTRACTS.items()} == {
        "1.2.0": frozenset(base),
        "1.3.0": frozenset(con_esm),
        "2.0.0": frozenset(con_update),
        "2.1.0": frozenset(con_update),
        "2.1.1": frozenset(con_update),
    }


def test_el_contrato_de_version_no_es_un_fingerprint_de_artefacto() -> None:
    """P0 §9: el contrato de versión no carga identidad binaria (sin ``exe_sha256``)."""
    contrato = resolve_pgpatcher_contract("1.2.0")
    assert contrato is not None
    assert isinstance(contrato, ToolVersionContract)
    assert not hasattr(contrato, "exe_sha256")


@pytest.mark.parametrize("version", ["2.1.2", "1.2.1", "2.2.0", "3.0.0", "2.1.1.1", "4.0.0", ""])
def test_version_no_registrada_no_hereda_compatibilidad(version: str) -> None:
    """Sin herencia SemVer: un contrato de versión ausente es fail-closed en fases posteriores."""
    assert resolve_pgpatcher_contract(version) is None


def test_material_node_spec_es_inmutable() -> None:
    """``frozen=True`` es parte del contrato: un nodo no puede mutarse in-place."""
    spec = MATERIAL_PIPELINE[MaterialStepId.VRAMR]
    with pytest.raises(FrozenInstanceError):
        spec.readiness = MaterialReadiness.FIRST_CUT  # type: ignore[misc]


def test_tool_version_contract_es_inmutable() -> None:
    """El contrato de versión no puede mutarse in-place."""
    contrato = PGPATCHER_KNOWN_CONTRACTS["1.2.0"]
    with pytest.raises(FrozenInstanceError):
        contrato.version = "9.9.9"  # type: ignore[misc]


def test_material_pipeline_no_permite_escritura() -> None:
    """El mapping público es de solo lectura: no admite reasignar entradas."""
    with pytest.raises(TypeError):
        MATERIAL_PIPELINE[MaterialStepId.VRAMR] = MATERIAL_PIPELINE[MaterialStepId.PGPATCHER]  # type: ignore[index]


def test_pgpatcher_known_contracts_no_permite_escritura() -> None:
    """El registro de contratos conocidos es de solo lectura."""
    with pytest.raises(TypeError):
        PGPATCHER_KNOWN_CONTRACTS["9.9.9"] = PGPATCHER_KNOWN_CONTRACTS["1.2.0"]  # type: ignore[index]

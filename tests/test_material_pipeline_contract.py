"""Anclas del contrato puro del Pre-LOD Material Pipeline (P1a).

Tests puros: sin subprocess, sin filesystem, sin MO2/VFS, sin red.
"""

from __future__ import annotations

import pytest

from sky_claw.local.tools.material_contract import (
    MATERIAL_PIPELINE,
    MATERIAL_STEP_ORDER,
    PGPATCHER_KNOWN_CONTRACTS,
    PGPATCHER_TOOL_KEY,
    PRE_LOD_MATERIALS_CAPABILITY,
    MaterialBlocker,
    MaterialCapability,
    MaterialNodeState,
    MaterialPlanError,
    MaterialReadiness,
    MaterialStepId,
    PgPatcherCapability,
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


def test_la_r_suite_es_opcional_y_pgpatcher_no() -> None:
    assert MATERIAL_PIPELINE[MaterialStepId.PARALLAXR].optional is True
    assert MATERIAL_PIPELINE[MaterialStepId.BENDR].optional is True
    assert MATERIAL_PIPELINE[MaterialStepId.VRAMR].optional is True
    assert MATERIAL_PIPELINE[MaterialStepId.PGPATCHER].optional is False
    assert MATERIAL_PIPELINE[MaterialStepId.POST_PG_RECONCILE].optional is False


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
    ],
)
def test_orden_invalido_es_detectado(orden_invalido: tuple[MaterialStepId, ...]) -> None:
    with pytest.raises(MaterialPlanError):
        validate_material_order(orden_invalido)


def test_ordenes_validos_son_aceptados() -> None:
    validate_material_order(MATERIAL_STEP_ORDER)
    validate_material_order((MaterialStepId.PARALLAXR, MaterialStepId.PGPATCHER))
    validate_material_order((MaterialStepId.PGPATCHER, MaterialStepId.VRAMR, MaterialStepId.POST_PG_RECONCILE))


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


@pytest.mark.parametrize("version", ["2.2.0", "3.0.0", "2.1.1.1", "4.0.0", ""])
def test_version_no_registrada_no_hereda_compatibilidad(version: str) -> None:
    """Sin herencia SemVer: un fingerprint ausente es fail-closed en fases posteriores."""
    assert resolve_pgpatcher_contract(version) is None

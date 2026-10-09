# Architecture Decision Records

> **Estado:** decisiones aceptadas; los ADR en revisión declaran `Propuesta` en su
> encabezado. Cada ADR conserva su contexto histórico.
>
> **Audiencia:** desarrolladores, operadores y agentes.
>
> **Fuentes canónicas:** ADR 0001–0013 en este directorio.
>
> **Última verificación:** 2026-10-08; ADR 0012 (Frozen Runtime) **enmendado en P0.4 y
> con el diseño de P4 congelado (P4-D0)**.
> **Revisión base** = `origin/main` `5039997a` (cierre de P3 en #682 y hardening
> post-merge #698): es la base sobre la que se enmendó y **no** contiene §§29–35.
> **Revisiones de enmienda** (rondas P0.4 sobre esa base): `f46853b5` (ronda 1, §29),
> `d930e5b2` (ronda 2, §30), `d8879662` (ronda 3, §31), la ronda 4 (§32), la ronda 4.1
> (§33) y **P4-D0** (§34, sobre `af8e726c`) con su **ronda adversarial 1** (§35). Ronda 1:
> la Generation pasa a ser referencia no ejecutada y el Runtime Clone el Effective
> Runtime. Ronda 2: el rollback tiene dos autoridades (Generation = versión,
> `RuntimeSetupManifest` = setup operativo), SFR-16 exige el par Generation+Clone,
> SFR-19 se acota a las superficies controladas por Sky-Claw, y la aprobación se liga
> a un `ApprovalScope` exacto. Ronda 3: nomenclatura única del schema (sin alias
> `active.*`), ejecutable ≠ activable, diseño cerrado ≠ implementación cerrada,
> `ApprovalScope` operation-aware y `RUNTIME_SETUP_ARTIFACT_AVAILABILITY = OPEN`.
> Ronda 4 (revisión externa de GitHub): exclusión física de la Managed Source,
> rollback con intención durable previa al binding, aprobación ligada a **contenido**,
> ciclo de vida de provisionamiento, target histórico de rollback y
> `RUNTIME_CLONE_RECORD_INTEGRITY = OPEN`. Ronda 4.1: la transición se finaliza **sólo
> después** del POST-verify (`FINALIZED ⇒ POST passed`) y la vía rápida del Clone
> retenido exige Generation de origen `VALID`. **P4-D0 (§34):** congela los contratos de
> transición y autoridad de P4 (lock cross-process root-keyed, transición durable,
> reconciliación de arranque, `ApprovalScope` con evidencia de contenido, reverify
> TOCTOU, integridad de metadata/manifiesto/registro, expectativas críticas
> obligatorias, inyección de enlaces post-activación y gate P4↔P5 `UNKNOWN !=
> COMPATIBLE`), con matriz de crashes C0–C7 y revisión adversarial de 12 preguntas.
> `P4_DESIGN_FROZEN = YES`, `P4_IMPLEMENTED = NO`, `P5_IMPLEMENTED = NO`. **P4-D0
> ronda adversarial 1 (§35):** la revisión del Tech Lead sobre `81fb83a2` encontró 6
> defectos contractuales (aprobación sin ligadura de *source*, C3 con ordering
> imposible, inyección de enlaces derivada a P5, censo incompleto de blockers P4,
> `FINALIZED` sin evidencia durable del POST, wording P4↔P5 ambiguo), todos
> `CONFIRMED` y corregidos: `ApprovalScope` liga `source_activation` y es single-use;
> C3 persiste `Desired=target` antes del POST; el escaneo de independencia física
> enumera el namespace **actual** (archivos **y** directorios); el censo cubre **17/17**
> requisitos P4; `FINALIZED` exige evidencia durable + CAS por `transition_id`. Sigue en
> estado Propuesta.

- [0001 — Leveled lists](0001-leveled-lists.md)
- [0002 — Caja negra de vuelo](0002-norte-caja-negra.md)
- [0003 — Daemons fail-fast](0003-daemons-fail-fast-sin-restart.md)
- [0004 — Governance singleton](0004-governance-singleton-sync-defendible.md)
- [0005 — Promoción síncrona HITL](0005-sandbox-promocion-sincrona-hitl.md)
- [0006 — Guardrail en dispatcher](0006-retiro-stategraph-guardrail-en-dispatcher.md)
- [0007 — MO2 broker USVFS](0007-mo2-broker-usvfs.md)
- [0008 — El KnowledgeCase](0008-knowledge-case.md)
- [0009 — RV-GP1: Golden Protection Status](0009-runtime-vault-golden-protection-status.md)
- [0010 — RV-GP2: Protect Golden / Golden Protection Apply](0010-runtime-vault-golden-protection-apply.md)
- [0011 — DynDOLOD PR-2: external_work_root y binding de propiedad](0011-dyndolod-external-work-root.md)
- [0012 — Frozen Runtime: promoción aislada de versiones](0012-frozen-runtime.md)
- [0013 — Clean-room del generador nativo de parallax](0013-clean-room-native-parallax.md)

Un ADR explica una decisión. Para saber cuánto está implementado, contrastarlo
con código, tests y la sección de alcance del propio ADR.

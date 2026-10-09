# Architecture Decision Records

> **Estado:** decisiones aceptadas; los ADR en revisión declaran `Propuesta` en su
> encabezado. Cada ADR conserva su contexto histórico.
>
> **Audiencia:** desarrolladores, operadores y agentes.
>
> **Fuentes canónicas:** ADR 0001–0013 en este directorio.
>
> **Última verificación:** 2026-10-09; ADR 0012 (Frozen Runtime) **enmendado en P0.4 y
> con el diseño de P4 congelado (P4-D0, cuatro rondas adversariales)**.
> **Revisión base** = `origin/main` `5039997a` (cierre de P3 en #682 y hardening
> post-merge #698): es la base sobre la que se enmendó y **no** contiene §§29–35.
> **Revisiones de enmienda** (rondas P0.4 sobre esa base): `f46853b5` (ronda 1, §29),
> `d930e5b2` (ronda 2, §30), `d8879662` (ronda 3, §31), la ronda 4 (§32), la ronda 4.1
> (§33) y **P4-D0** (§34, sobre `af8e726c`) con su **ronda adversarial 1** (§35, sobre
> `81fb83a2`), su **ronda adversarial 2** (§36, sobre `589805ee`), su **ronda
> adversarial 3** (§37, sobre `1215429b`) y su **ronda adversarial 4** (§38, sobre
> `6f257e7b`). Ronda 1:
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
> requisitos P4; `FINALIZED` exige evidencia durable + CAS por `transition_id`. **P4-D0
> ronda adversarial 2 (§36):** la revisión externa sobre `589805ee` encontró **6
> residuos nuevos** más un defecto de contabilidad del censo, todos `CONFIRMED` y
> cerrados **corrigiendo in-place** las secciones normativas (§7, §11, §12, §19, §30.6,
> §31.4, §32, §34, §35): `previous_activation_target` se publica **sólo** en el
> FINALIZE (autoridad única = registro de transición finalizado, `active.json` v2 deja
> de llevarlo); el **source baseline** debe ser el Effective **observado**
> (`Desired == Effective == RuntimeCloneRecord`) y su divergencia sin transición es
> `INCONSISTENT_BASELINE → FAIL_CLOSED`; el consumo de aprobación tiene **autoridad
> durable** fuera del journal (`approval_id` + `approval_seq` monótono), con
> `APPROVAL_REPLAY_AFTER_JOURNAL_ROTATION = IMPOSSIBLE_BY_CONTRACT`; el journal define
> CAS de `CREATE`/`UPDATE`/`FINALIZE`/`START_NEXT` con `journal_revision` anti-ABA y
> **un solo** `PENDING` por root; `CANCELLED` es resultado de operación (no lifecycle
> del Clone) y `PROVISIONING_FAILURE` tiene una única semántica (`INVALID`, retry =
> Clone nuevo), contrastada con `ensure_skse`. **P4-D0 ronda adversarial 3 (§37):** la
> revisión externa sobre `1215429b` encontró **5 blockers nuevos** en la maquinaria de
> aprobación y journal que la ronda 2 había agregado, más 2 findings nuevos, todos
> `CONFIRMED` y cerrados **corrigiendo in-place** las secciones normativas (§7, §11,
> §12, §19, §29.8, §30.6, §31.4, §34.4, §34.6, §35.2, §35.5, §35.6, §35.8.5, §35.10,
> §35.13 y §36): el `ApprovalScope` liga ahora `approval_seq`,
> `approval_scope_digest` y `approval_provenance` (el consumo compara el scope
> presentado contra el registrado, no `approval_id == approval_id`); el ledger es
> **una sola autoridad** `state/approval.json` con **un solo `approval_revision` bajo
> CAS** para todo escritor; el consumo y la creación del journal se ordenan con un
> protocolo **burn-first** (no se reclama atomicidad entre archivos:
> `CROSS_FILE_ATOMICITY = NOT_CLAIMED`) con matriz de crash explícita; la aprobación
> tiene ciclo de vida `ISSUED | CONSUMED | REVOKED` (cancelar tras aprobar **revoca**,
> no deja token reutilizable); el historial `FINALIZED` gana almacén propio inmutable
> (`state/transitions/<transition_id>.json`) con orden **lógico** (`finalization_seq`,
> no mtime); y la retención se corrige a **`K ≥ 3`**. **P4-D0 ronda adversarial 4
> (§38):** la revisión externa sobre `6f257e7b` encontró **4 residuos contractuales** más
> **2 findings preventivos** (uno material), todos `CONFIRMED` y cerrados corrigiendo
> in-place las secciones normativas (§6, §29.6, §29.8, §34.4, §35.2, §35.6, §36.5, §36.6,
> §36.10, §36.14, §36.15, §37.2–§37.7): la aprobación terminal deja **evidencia durable
> independiente** (`state/approvals/<approval_seq>-<approval_id>.json`, inmutable,
> no-clobber, escrita **antes** del ledger) de modo que la restauración de
> `state/approval.json` **no puede resucitar** una aprobación `CONSUMED`-sin-transición ni
> una `REVOKED` —el cross-check contra el historial FINALIZADO se degrada a chequeo de
> coherencia porque no las cubría—; `last_finalization_seq` deja de ser una variable sin
> autoridad y pasa a **derivarse** del store validado (`max(finalization_seq) + 1` bajo el
> root lock), con `START_NEXT` comparando contra el registro más reciente derivado y no
> contra un campo del journal; `FINALIZED` deja de figurar como estado del journal en
> §34.4/§36.14/§36.15; la lista normativa de `ApprovalScope` se unifica en **14 campos**
> sin alias (§35.2 usaba `target_*`); y la **frontera de retención** queda definida por
> posición (un predecesor ausente en la frontera es válido; un hueco interior es
> `FAIL_CLOSED`), sin archivos ni campos nuevos. Sigue en estado Propuesta.

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

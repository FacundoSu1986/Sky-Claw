# ADR 0012 — Frozen Runtime: promoción aislada de versiones

**Fecha:** 2026-10-03
**Estado:** Propuesta (P0: diseño y censo; prohibida la implementación de código de
producción en este ADR). No cambia contratos existentes; sólo decide arquitectura.
**Enmienda P0.1 (2026-10-03):** endurecimiento de la promoción — `SourceSnapshotEvidence`
(anti-self-verification, SFR-15), separación Desired Active Generation / Effective
Runtime (anti split-brain, SFR-16) y ciclo de vida de Generation con estado
`DRIFTED` (SFR-17). Alcance: design/contract only; no habilita implementación
productiva ni decide la primitiva concreta de binding (P5).
**Enmienda P0.3 (2026-10-04):** rename de branding — el nombre de feature pasa a ser
**Frozen Runtime** (sin "Steam"); el término genérico de arquitectura pasa a ser
**Managed Source** (proveedor inicial: Steam). "Steam" se conserva sólo como
referencia técnica al proveedor. IDs `SFR-01..17` intactos; prefijo reinterpretado
como `SFR = Sky-Claw Frozen Runtime`.
**Enmienda P0.2 (2026-10-04):** cierre de la revisión adversarial de P0 — se agrega
`SFR-18` (independencia física de Generation respecto de la Managed Source) y se
explicita que el rollback no depende de la Managed Source.
**Enmienda P2.1 (2026-10-04):** hardening del storage — admisión fail-closed de
cada componente persistente (P2-B1: un junction en `state/` no puede redirigir
escrituras fuera del root); `steamapps/common` exacto rechazado (P2-M1); SFR-18
pasa a ser propiedad **on-demand** de la Generation (`st_nlink==1`, sin requerir
la Managed Source): `VALID` exige integridad física fresca (P2-B2). Veredictos:
violación física conocida ⇒ `INVALID`; identidad física inobservable ⇒
`INDETERMINATE`; nunca `VALID`.
**Enmienda P0.4 (2026-10-07):** reconciliación posterior al cierre de P3 (PR #682)
y al hardening post-merge (#698, `origin/main` `5039997a`). **Separa los dos roles
que el ADR original confundía**: la **Generation** pasa a ser la *referencia*
—lógicamente inmutable y **nunca ejecutada** (SFR-19)— y lo que MO2/SKSE/Sky-Claw
ejecutan es un **Runtime Clone** derivado de ella (SFR-20). Se agrega SFR-21 (sin
pasos manuales repetidos por arranque) y el slice **P3b**. **La autoridad de
rollback es la Generation —autoridad de versión— más el `RuntimeSetupManifest`
—autoridad de setup operativo—**, no un Clone retenido (§12). Alcance: design/contract
only; no cambia lo ya mergeado en P1–P3, no habilita implementación productiva y
**no** decide el mecanismo de P3b (depende de evidencia de rig). Registro de la
adjudicación: §29.
**Enmienda P0.4, ronda 2 (2026-10-07):** adjudicación de la revisión del Tech Lead
sobre `f46853b5`. Los cinco findings se confirman y se corrigen **en el cuerpo del
ADR**: el rollback reconstruye el **runtime operativo** (`RuntimeSetupManifest`,
SFR-22); SFR-16 exige el **par** Generation+Clone (`active.json` v2); se elimina la
terminología residual que ejecutaba la Generation; SFR-19 se **acota** a las
superficies controladas por Sky-Claw con admisión por identidad lógica y se separa
**violación de política ≠ `DRIFTED`**; y se fija **preparación ≠ activación** con
`ApprovalScope` re-verificado antes del binding (SFR-23). Registro: §30. Sin cambios
de código de producción.
**Enmienda P0.4, ronda 3 (2026-10-07):** cierre de los **residuos contractuales** de
`d930e5b2`. (R3-F1) el schema tiene **una sola** nomenclatura —se eliminan los alias
`active.generation_id` / `active.clone_id`, que no existen en v2—; (R3-F2) se separa
**ejecutable** de **activable**: `FRESH_CLONE_READY_FOR_ACTIVATION = NO` reemplaza a
la afirmación absoluta "no es jugable"; (R3-F3) **diseño cerrado ≠ implementación
cerrada** (`P4_APPROVAL_SCOPE` / `P4_REVERIFY`: `DESIGN = CLOSED`,
`IMPLEMENTATION = OPEN`); (R3-F4) el `ApprovalScope` pasa a ser **operation-aware**
(`candidate_id` REQUIRED en `PROMOTION`, NOT_APPLICABLE en `ROLLBACK`). Se registra
además el blocker **`RUNTIME_SETUP_ARTIFACT_AVAILABILITY = OPEN`** (R3-B1): el
manifest declara procedencia, no disponibilidad futura. Registro: §31. Sin cambios
de código de producción.
**Contexto de origen:** `origin/main` `0103ee4f6de15207032d25c254ede5cf2c01bff9`
(merge de RV-GP2/S4D, PR #666).
**Relación con GP2-S4E:** el workstream archivado (rama
`feat/runtime-vault-s4e-production-wiring`, PR #669, issue #671) queda **pausado y
diferido**; este ADR **no** lo reanuda, **no** lo modifica y **no** depende de él.
Se lo referencia únicamente como contexto histórico del enfoque anterior.
**Invariante de alcance (histórico, P0):** el único lugar de trabajo era
`E:\Skyclaw_Steam_Frozen_Runtime` en la rama `feat/steam-frozen-runtime`; `main` sólo
se leía. **Superado por P0.4:** la reconciliación se hizo en un worktree propio bajo
`<repo>/.worktrees/` con rama nueva (convención vigente de `AGENTS.md`), y el
resultado se propone por PR en estado Draft. La regla que sobrevive es la de fondo:
`main` sólo se lee.
**Deuda de naming (no funcional, registrada en P0.3):** la rama conserva el nombre
histórico `feat/steam-frozen-runtime` y el worktree conserva su path físico: el
rename de la rama remota **cierra el PR #673** (comprobado empíricamente el
2026-10-04 vía `POST /branches/{branch}/rename`, que no actualiza el head ref del PR
y lo dejó `CLOSED`; se revirtió y se reabrió). La rama/worktree se renombrarán sólo
si en el futuro un mecanismo que preserve el PR esté probado. Un path local no es
branding público.

---

## 1. Contexto

Sky-Claw administra mods de Skyrim SE/AE vía Mod Organizer 2. El repositorio ya
contiene un **Runtime Vault** maduro (RV-1, RV-2, RV-3 merged) y una capa de
**Golden Protection** (GP1 merged, GP2 con implementación parcial sobre `main`).

Una actualización de Skyrim lanzada por Steam sobre la instalación que el usuario
realmente juega es un evento disruptivo: rompe binarios pinneados a la versión
exacta del ejecutable (SKSE y sus plugins, Address Library, Community Shaders,
DynDOLOD DLL NG) y puede introducir cambios incompatibles con la lista de mods.

El enfoque previo (auditoría del rig
[`docs/audits/2026-08-22_runtime_vault_mo2_stock_launch_audit.md`](../audits/2026-08-22_runtime_vault_mo2_stock_launch_audit.md))
intentaba **oponerse a Steam** marcando el `appmanifest_489830.acf` como ReadOnly
para impedir la actualización. Esa evidencia dejó la causalidad **no probada**
(`UPDATE_GUARD_CAUSALITY = NOT_PROVEN`) y el resultado observado fue
`Disk write failure` mientras el manifest seguía ReadOnly. La misma auditoría sí
probó dos hechos positivos y reutilizables:

- `MO2_EXTERNAL_RUNTIME` = `PROVEN`: MO2 ejecutó `SkyrimSE.exe` físicamente desde
  un Stock externo, fuera de `steamapps/common`.
- `STEAM_PARTICIPATION_EXTERNAL_RUNTIME` = `PROVEN` para esa corrida: Steam
  reconoció el AppID `489830`, reportó `App Running` y cargó su overlay con el
  ejecutable fuera del directorio de Steam.

Además, la issue **#494** (abierta, no implementada) ya propuso separar
`Steam-managed install` (fuente mutable) de un `Sky-Claw detached pinned runtime`
(copia operacional versionada), y ADR 0010 §11.4 lo cita como relación conceptual,
aclarando que esa separación **separa lifecycle, no firma provenance**.

Este ADR decide cómo materializar esa separación de lifecycle de forma simple,
reproducible y verificable.

## 2. Problema

> Steam debe poder descargar e instalar normalmente una nueva versión de Skyrim en
> su instalación administrada, mientras el Skyrim que el usuario realmente juega
> con MO2/SKSE permanece congelado e independiente hasta que el usuario autorice
> explícitamente adoptar la nueva versión.

El requisito correcto **no** es engañar a Steam para que crea que actualizó. El
requisito es:

```text
Steam realmente actualiza SU copia.
El Frozen Runtime permanece intacto.
```

Esto elimina la necesidad de manipular `appmanifest`, flags de estado de Steam, la
base de datos de Steam, ACLs de la instalación de Steam o internals del cliente.
No se implementan trucos de ese tipo (SFR-11).

## 3. Enfoque anterior y distinción con GP2-S4E archivado

| Eje | GP2-S4E (archivado) | Frozen Runtime (este ADR) |
|---|---|---|
| Mecanismo | ACL mutation transaccional del Golden (`SetSecurityInfo`, WAL, helper elevado) | Aislamiento físico: la copia jugable vive fuera del árbol de Steam |
| Relación con Steam | Bloquear/negar la escritura de Steam sobre el Golden | Steam escribe libremente sobre su propia instalación |
| Complejidad | FSM privilegiada, journal durable, WAL, lock de kernel, store autoritativo, IPC autenticado | Copia → verificar → puntero de generación activa |
| Dependencias | GP1/GP2/S4-A..E | RV-1 (reuso), RV-2/RV-3 (reuso parcial), nada de GP2 |
| Threat model | Admin filtrado, handles de control preexistentes, power-loss | Actualización de Steam, promoción equivocada, candidate corrupto |

El threat model de GP2-S4E **no se hereda**. Este ADR protege contra las fallas de
§14 (F1–F10), no contra un administrador malicioso, atacante de kernel, tampering
con `SeDebugPrivilege`, crash durante `SetSecurityInfo` ni rollback transaccional
de ACL.

## 4. Objetivos (Goals)

Nota canónica (P0.3): `SFR` = **Sky-Claw Frozen Runtime**. El prefijo se conserva
para no migrar IDs ya documentados (`SFR-01..23`) en docs, tests e issues.

1. `SFR-01` Steam puede actualizar su propia instalación.
2. `SFR-02` Steam nunca administra el Frozen Runtime.
3. `SFR-03` MO2/SKSE/juego activo utilizan el **Runtime Clone** de la Generation
   activa (no la Generation misma; ver SFR-19/20).
4. `SFR-04` Una actualización de Steam no modifica el Frozen Runtime activo.
5. `SFR-05` La Managed Source nunca se convierte automáticamente en runtime activo.
6. `SFR-06` Toda nueva versión entra primero como Candidate.
7. `SFR-07` Candidate debe verificarse antes de poder promocionarse.
8. `SFR-08` Promotion requiere autorización explícita del usuario.
9. `SFR-09` Una promoción fallida nunca destruye la versión activa anterior.
10. `SFR-10` La versión anterior permanece disponible para rollback.
11. `SFR-11` No editar ni falsificar estado interno de Steam.
12. `SFR-12` No depender de ACL mutation para proteger el runtime activo.
13. `SFR-13` No utilizar GP2-S4E como dependencia necesaria.
14. `SFR-14` La solución debe ser comprensible y mantenible por un proyecto pequeño.
15. `SFR-15` Un Candidate jamás puede establecer la evidencia esperada contra la
    que él mismo se aprueba (anti-self-verification; ver §9.1 y §10).
16. `SFR-16` La promoción no está completa hasta que la coherencia sea
    **probadamente** cierta, y esa coherencia tiene **dos identidades**, no una:
    `Desired Generation == G` **y** `Desired Clone == C`, con
    `C.source_generation_id == G`, `Effective Runtime == C.root_path`, y `C`
    habiendo pasado el gate de activación. Un cambio exitoso de
    `state/active.json` por sí solo NO es promoción exitosa (§11).
17. `SFR-17` La identidad de una Generation es **lógicamente inmutable** aunque el
    filesystem siga siendo escribible. Sky-Claw no muta una Generation promocionada
    in-place; una Generation cuyo árbol ya no coincide con su identidad registrada
    está `DRIFTED` y no es un target válido de rollback/reactivación sin
    re-verificación previa (§12).
18. `SFR-18` Una Generation **no debe compartir objetos de filesystem mutables**
    con su Managed Source: sin symlink/junction/reparse point que conecten ambos
    árboles y sin hardlinks hacia archivos de la Managed Source. La independencia
    física se verifica en la creación (por inodo, RV-3) y la admisión de rutas la
    preserva (§17).
19. `SFR-19` La Generation es **referencia**: **nunca se ejecuta**, nunca recibe
    instalaciones de herramientas ni es el Effective Runtime. La prohibición es
    **fail-closed en toda superficie de launch, instalación de herramientas y
    mutación del game dir que Sky-Claw controle**: ninguna acepta una Generation
    como destino, y el rechazo es por **identidad lógica registrada**
    (`RuntimeCloneRecord.admitted_role`), no por prefijo de ruta — dónde vivan
    físicamente los Clones sigue abierto (Q18). Fuera de esas superficies (p. ej.
    el propietario ejecutando `versions/G/SkyrimSE.exe` a mano) Sky-Claw **no
    puede impedirlo**: eso es una **violación de política**, no una derivación.
    Ejecutar y derivar son hechos distintos: una Generation ejecutada que no se
    modificó sigue `VALID`; una Generation modificada está `DRIFTED` (SFR-17). **No
    se afirma** que toda ejecución produzca `DRIFTED` (§30.5).
20. `SFR-20` Lo que MO2/SKSE/Sky-Claw ejecutan es un **Runtime Clone**: copia
    operativa creada desde una Generation `VALID` con RV-2 → RV-3, físicamente
    independiente de la Generation y de la Managed Source, con **linaje
    registrado** (`clone_id`, `source_generation_id`) y un **RuntimeSetup**
    aplicado (`runtime_setup_id`). Su deriva es **esperada** (juego, SKSE, ENB,
    Creation Club): se **reporta**, y sólo bloquea si un archivo **crítico** cambió
    o si aparece un objeto de filesystem compartido (§29.3).
21. `SFR-21` (requisito de producto) Tras la captura inicial, ni arrancar el juego
    ni que Steam actualice su propia copia exigen una **acción manual repetida**
    del usuario (sin solo-lectura sobre el manifest, sin restaurar nada, sin
    desconectar red, sin bloqueos). Criterio de aceptación de P7, **no demostrado
    todavía** (§29.7).
22. `SFR-22` **`RuntimeSetupManifest`** es la autoridad del **setup operativo**:
    qué necesita una versión de Skyrim (una Generation) para convertirse en
    *nuestro* runtime jugable — SKSE (loader + DLLs del build exacto), archivos de
    root (ENB y compañía), componentes requeridos — con **procedencia y
    operaciones reproducibles**. No duplica necesariamente el payload: declara
    versiones, hashes y operaciones. Un Runtime Clone recién creado desde una
    Generation **no se considera por defecto un runtime operativo listo para
    activación**: debe aplicarse y verificarse el `RuntimeSetup` requerido y
    superar los gates definidos (§31.2). El registro del Clone distingue el setup
    **intencionado** del **verificado** mediante un ciclo de vida explícito
    (`PROVISIONED` es requisito de activación; R4-F9, §19c). Esto **no** afirma que
    el Clone no pueda ejecutar el juego —afirma que no está listo para ser *nuestro*
    Effective Runtime—. La **disponibilidad futura** de los artefactos que el
    manifest declara es un blocker separado y abierto (§31.5).
23. `SFR-23` **Preparación ≠ activación.** Publicar una Generation, instanciar un
    Runtime Clone y provisionar su RuntimeSetup son operaciones **no activas** y
    **no requieren aprobación**, siempre que no toquen el Effective Runtime. La
    aprobación explícita (SFR-08) se pide **después** del gate y queda ligada a un
    conjunto exacto de artefactos (`ApprovalScope`, §11) —identidad **y contenido**
    (R4-F5)—, que se **re-verifica** justo antes de mutar el Effective Runtime.
    Cancelar deja el runtime activo
    intacto; los artefactos preparados quedan **inactivos** y su limpieza es GC
    explícito (§23), no parte de la promoción.

## 5. No-objetivos (Non-goals)

- **No hay auto-promoción.** Aunque el Candidate sea válido, no se activa sin
  aprobación explícita del usuario (SFR-08). El agente/IA puede detectar, preparar,
  validar y recomendar; no autoriza por sí mismo. **Preparar no es activar**
  (SFR-23): publicar la Generation, instanciar el Clone y provisionar su
  RuntimeSetup no requieren aprobación mientras no toquen el Effective Runtime;
  lo que la requiere —y se re-verifica— es el **binding**. El agente no puede
  auto-activar por la puerta de atrás de "preparar".
- **No hay garbage collection.** No se borra la generación anterior durante la
  promoción (SFR-10; §23). La retención/limpieza futura será otro slice con
  confirmación explícita.
- **No hay ACL hardening** del Frozen Runtime. "Frozen" significa **versión
  congelada**, no filesystem read-only (SFR-17: la inmutabilidad de la Generation
  es **lógica**, no impuesta por ACL). Se prefiere aislamiento
  arquitectónico a bloqueo.
- **No hay helper privilegiado, UAC, `WRITE_DAC`, WAL ni journal de protección**
  (§21). Si en el futuro se propone reutilizar algo de esa lista, debe justificarse
  por qué el problema no se resuelve con `copia a nueva generación → verificar →
  actualizar puntero`.
- **No se decide la primitiva de binding** de la Effective Runtime (re-apuntar
  `gamePath`/`SKYRIM_PATH` vs alias estable vs otra) en P0. Se fija la invariante
  (SFR-16) y la semántica de falla (§11); la primitiva se decide en P5. Tampoco se
  decide **dónde viven físicamente los Clones** (Q18), así que la identidad del
  target operativo es **lógica y registrada** (`RuntimeCloneRecord`), nunca un
  prefijo de ruta (SFR-19).
- **No se clasifica Creation Club por intuición.** Qué parte de Creation Club ya
  está en el snapshot (y por tanto pertenece a la Generation) y qué parte es estado
  operativo añadido después (y por tanto pertenece al RuntimeSetupManifest) requiere
  evidencia del rig. Queda **sin adjudicar** en este PR (§30.2; Q22).
- **No hay resolución automática de compatibilidad de mods.** Sólo se diseña el
  gate `RUNTIME_COMPATIBILITY ∈ {UNKNOWN, COMPATIBLE, INCOMPATIBLE}` con
  `UNKNOWN != COMPATIBLE` por defecto (§20/§13).
- **No hay GUI ni promoción real** en P0. Sólo censo, arquitectura, ADR, roadmap,
  análisis de riesgo/reuso y plan de tests.
- **No se modifica** `appmanifest_*.acf`, `Steam`, `ModOrganizer.ini`, ni
  configuración del usuario durante P0.

## 6. Terminología canónica

| Término | Definición |
|---|---|
| **Managed Source** | Instalación mutable del juego administrada por una plataforma externa. **Proveedor inicial: Steam** (path típico `steamapps/common/...`). Fuente válida para crear Candidate. No es Golden. `Managed Source != Frozen Runtime` y `Managed Source != Golden Master`. |
| **SourceSnapshotEvidence** | Observación sellada de una Managed Source **estabilizada**: `RuntimeIdentity` + `TreeDigest` + critical file evidence + `provider_metadata` opcional (para Steam: `appid`, `buildid`, `library_path`). No es Golden ni autoridad GP2; es la evidencia de la fuente concreta que se pretendía copiar (§9.1). |
| **Frozen Runtime** | Feature de Sky-Claw: árbol de directorios independiente, fuera del árbol de juego administrado por la Managed Source. Contiene las **Generations** (referencias), los **RuntimeSetupManifest** (setup operativo) y los **Runtime Clones** (copias operativas) que MO2/SKSE/juego ejecutan. |
| **Candidate** | Copia candidata derivada de la Managed Source, aún no promocionada. Debe verificarse **contra `SourceSnapshotEvidence`** antes de promocionar. Nunca genera la evidencia contra la que se aprueba (SFR-15). |
| **Generation** | Snapshot inmutable y versionado del árbol completo del juego dentro del Frozen Runtime (`versions/<generation-id>/`). Lógicamente inmutable (SFR-17). Es la **referencia**: no se ejecuta (SFR-19) y es la **autoridad de versión** —qué versión de Skyrim es— para el rollback (§12). Por sí sola **no** describe el setup operativo: eso es el `RuntimeSetupManifest` (SFR-22). |
| **RuntimeSetupManifest** | Autoridad del **setup operativo** de una versión: qué necesita una Generation para ser *nuestro* runtime jugable (SKSE, archivos de root, componentes requeridos), con procedencia, hashes y **operaciones reproducibles**. No necesariamente duplica el payload (SFR-22). Vive fuera del árbol de la Generation. |
| **Runtime Clone** | Copia operativa derivada de una Generation `VALID` (`clones/<clone-id>/`), creada con RV-2 → RV-3 y con un RuntimeSetup aplicado. **Es el Effective Runtime**: lo que MO2/SKSE/Sky-Claw ejecutan (SFR-20). **Mutable**: su deriva es esperada y se reporta. No es una Generation. Su **linaje** (`source_generation_id`) y su setup (`runtime_setup_id`) quedan registrados, no inferidos. |
| **RuntimeCloneRecord** | Identidad **lógica registrada** de un Runtime Clone: `clone_id`, `source_generation_id`, `lifecycle` (`CREATED`/`PROVISIONING`/`PROVISIONED`/`INVALID`), `intended_runtime_setup_id`, `verified_runtime_setup_id`, `root_path`, `admitted_role = "runtime_clone"`. Es la autoridad para decidir si un target operativo es admisible (SFR-19/§30.5); **no** un prefijo de ruta, porque la ubicación física de los Clones sigue abierta (Q18) —pero la admisión exige además **exclusión física** de la Managed Source (R4-F6). Su propia **integridad** es un blocker abierto (`RUNTIME_CLONE_RECORD_INTEGRITY`, R4-F7). |
| **Desired Generation / Desired Clone** | Estado persistente de Sky-Claw (`state/active.json` v2). Los nombres de campo son **literales y únicos**: `desired_generation_id` y `desired_clone_id`. No existen alias (`active.generation_id`, `active.clone_id`, `desired_active_generation` quedan **fuera** del schema v2). Sólo el par identifica sin ambigüedad el Effective Runtime pretendido, porque una Generation admite varios Clones (§31.1). |
| **Effective Runtime** | La ruta que MO2/SKSE ejecutan **realmente** (game path efectivo): desde P0.4, `RuntimeCloneRecord.root_path` de un Runtime Clone. Puede divergir del Desired; esa divergencia es un defecto de promoción, no un éxito (SFR-16). |
| **ApprovalScope** | Conjunto exacto de artefactos al que queda ligada la aprobación del propietario: `operation` (`PROMOTION` o `ROLLBACK`), `generation_id`, `clone_id`, **evidencia de contenido** del Clone (`clone_evidence`), `runtime_setup_id`, **evidencia de contenido** del manifest (`runtime_setup_evidence`), `compatibility_evidence_id` y —sólo en `PROMOTION`— `candidate_id`. Antes de mutar el Effective Runtime se **re-verifica** ese conjunto, no "la intención de actualizar" (SFR-23; §11; §31.4). Ligar sólo IDs es insuficiente: el Clone es mutable (R4-F5). |
| **DRIFTED** | Estado de una Generation cuyo árbol ya no coincide con su identidad registrada (`runtime_identity`/`tree_digest`). `DRIFTED != VALID` y `DRIFTED` **no es fuente de clonación** (SFR-17; F10). La detección es **on-demand**: re-verificación de identidad antes de clonar o reparar; no se promete monitoreo continuo. Desde P0.4 la deriva por **uso normal** no debería ocurrir (la Generation no se ejecuta, SFR-19); si ocurre, es una mutación externa o una violación de política, y se trata igual: fail-closed. **Ejecutar una Generation y modificar una Generation son hechos distintos** (§30.5). |
| **Promotion** | Acto explícito y autorizado de hacer que el par Desired Generation/Desired Clone y el Effective Runtime pasen a ser, **probadamente coherentes**, un Candidate verificado materializado en un Runtime Clone provisionado; sin sobreescribir el runtime activo anterior (SFR-09/16/23). |
| **Rollback** | Hacer que el par Desired Generation/Desired Clone y el Effective Runtime pasen a ser, probadamente coherentes, una **Generation anterior retenida y re-verificada**, **re-materializada** en un Runtime Clone nuevo y **provisionada** con su RuntimeSetupManifest. La **autoridad de versión es la Generation** y la **autoridad de setup es el RuntimeSetupManifest**; un Clone retenido es una vía rápida opcional, nunca la única autoridad (§12). No reconstruye archivos "a mano" ni depende de la Managed Source (SFR-10/22). |

`Golden Master` sigue existiendo como concepto de Runtime Vault (RV-2); **no** es
sinónimo de Frozen Runtime.

## 7. Arquitectura

```text
┌──────────────────────────────────────┐
│ MANAGED SOURCE  (provider: Steam)    │
│ <library>/steamapps/common/          │
│   Skyrim Special Edition/            │
│ Steam actualiza libremente (SFR-01)  │
└───────────────┬──────────────────────┘
                │  snapshot / copy  (candidate creation)
                ▼
┌──────────────────────────────────────┐
│ CANDIDATE  (aislado, no activo)      │
│ <FrozenRuntimeRoot>/candidates/<id>/ │
│ verificación contra SourceSnapshot-  │
│ Evidence (SFR-06/07/15)              │
└───────────────┬──────────────────────┘
                │  PREPARACIÓN (no activa; sin aprobación, SFR-23)
                │  NO copia sobre la activa (SFR-09)
                ▼
┌──────────────────────────────────────┐
│ FROZEN RUNTIME                       │
│                                      │
│ GENERATION  versions/<gen>/          │
│   referencia inmutable (SFR-17)      │
│   Steam NO la administra (SFR-02)    │
│   NO se ejecuta (SFR-19)             │
│   autoridad de VERSIÓN               │
│             │ RV-2 verify            │
│             │ RV-3 create clone      │
│             ▼                        │
│ RUNTIME CLONE  clones/<clone-id>/    │
│   copia operativa (SFR-20)           │
│   source_generation = <gen>          │
│   runtime_setup     = <setup>        │
│ MO2/SKSE/juego ejecutan ACÁ (SFR-03) │
└──────────────────────────────────────┘

RuntimeSetupManifest S    (autoridad del SETUP operativo; SFR-22)
    └─ provisiona SKSE / archivos de root / componentes sobre el Clone

Desired Generation = G   │  Effective Runtime = C.root_path
Desired Clone      = C   │  linaje C.source_generation_id = G
                         │  coherencia completa ⇔ SFR-16
```

Flujo de promoción (contrato de producto, no UI):

```text
Managed Source (estabilizada; provider: Steam)
    ↓   capture SourceSnapshotEvidence PRE      (§9.1)
    ↓   crear Candidate desde la Managed Source
    ↓   verificar Candidate CONTRA SourceSnapshotEvidence (SFR-15)
    ↓   reobserve Managed Source POST
    ↓   POST == PRE  (si no: Candidate = INVALID / SOURCE_CHANGED)
  READY
    ↓   publicar Generation G   (versions/G; sin tocar el runtime activo; SFR-09)
    ↓   instanciar Runtime Clone C desde G   (RV-2 → RV-3; lifecycle CREATED)
    ↓   provisionar RuntimeSetupManifest S sobre C   (SFR-22; → PROVISIONING → PROVISIONED)
    ↓   gate de activación de C   (§29.3)
  PREPARADO  (nada activo cambió; SFR-23)
    ↓   coherencia del SOURCE BASELINE   (§36.3; D0-R2.2)
    │      Desired source == Effective observado == RuntimeCloneRecord registrado
    │      si divergen ⇒ FAIL_CLOSED: sin aprobación, sin PENDING, sin promoción
    ↓   USER APPROVES   (SFR-08; operation = PROMOTION;
    │                     scope = {source_activation, source_activation_digest,
    │                              generation_id, clone_id, clone_evidence,
    │                              runtime_setup_id, runtime_setup_evidence,
    │                              compatibility_evidence_id, candidate_id,
    │                              approval_id})
    ↓   re-verificar EXACTAMENTE la evidencia aprobada   (SFR-23; §11; R4-F5; §36.3)
    ↓   CAS CREATE: persist durable PENDING PROMOTION   (R4-F3; §36.6; ANTES de mutar)
    │      + consumo atómico de la aprobación (single-use durable, §36.5)
    ↓   bind Effective Runtime → C   (primitiva P5)
    ↓   verify effective_path == C.root_path  y  linaje C → G
    ↓   CAS UPDATE: persistir active.desired_* = (G, C)   (§36.6)
    ↓   POST verify de coherencia   (SFR-16)
    │      si FALLA ⇒ la transición sigue PENDING (recuperable); NO se finaliza
    ↓   CAS FINALIZE: finalizar la transición pendiente   (R4-F3; SÓLO tras POST OK)
    │      y en la MISMA operación lógica publicar
    │      previous_activation_target = source_activation   (§35.8.5; §36.2)
  SUCCESS
```

## 8. Fronteras de confianza (Trust boundaries)

| Superficie | Confianza | Regla |
|---|---|---|
| **Managed Source (provider: Steam)** | MUTABLE / *untrusted as active runtime* | Fuente válida para Candidate. Nunca `reference_only`. Nunca Golden. El proveedor escribe libremente en ella (SFR-01). |
| **SourceSnapshotEvidence** | Observación de la fuente, **no autoridad** | Evidencia sellada de la Managed Source estabilizada que se pretendía copiar. No es Golden, no tiene autoridad GP2 (SFR-15; §9.1). |
| **Candidate** | Derivada, no confiable hasta verificar | Se inventaría/verifica **contra `SourceSnapshotEvidence`**, nunca contra su propia medición (SFR-15). Estados `building → ready → invalid` (SFR-06/07). |
| **Frozen Runtime (Generation)** | Confiable tras verificación; **referencia** | La versión está congelada y **nadie la ejecuta** (SFR-19): es la referencia contra la que se clona y la **autoridad de versión** para el rollback. Sky-Claw **no muta una Generation promocionada in-place**; si su árbol ya no coincide con su identidad registrada, está `DRIFTED` (SFR-17) y no se clona desde ella. No comparte objetos de filesystem mutables con la Managed Source (SFR-18). `VALID` exige **integridad física fresca** (sin reparse, `st_nlink==1`): un hardlink insertado después de publicar no cambia el digest pero nunca es `VALID` (P2-B2). Por sí sola **no** define el runtime jugable: eso lo completa el `RuntimeSetupManifest` (SFR-22). |
| **RuntimeSetupManifest** | Autoridad del setup operativo; **no es payload** | Declara qué necesita una Generation para ser nuestro runtime jugable, con procedencia y operaciones reproducibles (SFR-22). No duplica el árbol. Su **integridad** es un requisito abierto (Q23): si un manifest mentiroso puede hacer que un Clone pase el gate, el gate hereda la debilidad de la metadata (§30.7). |
| **Runtime Clone** | Derivado de una Generation `VALID`; **mutable** | Creado con RV-2 → RV-3, con linaje, **ciclo de vida** y setup **registrados** (`RuntimeCloneRecord`), físicamente independiente de la Generation y de la Managed Source (SFR-18). Su deriva por uso normal es **esperada** y se reporta (SFR-20). Lo bloquean: archivo **crítico** alterado, objeto de filesystem compartido, **contención en la Managed Source** (R4-F6), `lifecycle != PROVISIONED` (R4-F9), o fallo de identidad de runtime (§29.3). Un Clone **recién creado y sin setup provisionado no está listo para activación** (`FRESH_CLONE_READY_FOR_ACTIVATION = NO`); eso no afirma que no pueda ejecutar el juego (§31.2). |
| **Desired Generation / Desired Clone (`state/active.json` v2)** | Intención persistente de Sky-Claw | **Dos** identificadores (v1 sólo tenía uno y era ambiguo: §30.3). Escritura atómica (temp + `os.replace`). Por sí solo **no** declara promoción exitosa (SFR-16). La migración v1 → v2 es de **P4** y fail-closed (§19). |
| **Effective Runtime** (game path que MO2/SKSE ejecutan) | Autoridad de hecho | La promoción sólo es exitosa cuando el Effective Runtime está probadamente apuntando al **Runtime Clone** del par Desired, con linaje verificado hacia la Generation. Sin oráculo de observación (P5) no se declara `SUCCESS`. |

## 9. Identidad de versión (Version identity)

Se separa **DISPLAY VERSION** de **RUNTIME IDENTITY**. El mínimo suficiente, todo
reutilizando contratos existentes:

```text
display_version : "1.6.1170"                       # derivado de game_version (major.minor.patch)
runtime_identity: RuntimeIdentity(game_key="skyrimse", game_version="1.6.1170.0")
tree_digest     : TreeDigest(digest=sha256, files=N, bytes=M)      # inventory_tree + tree_digest_from_files
critical[exe]   : CriticalFileExpectation(rel_path="SkyrimSE.exe", expected_digest=sha256, expected_size=size)
```

- `game_version` sale del recurso PE `ProductVersion` del ejecutable, vía
  `sky_claw.local.runtime_vault.runtime_observation.observe_runtime_identity_from_root`
  (observación fresca, fail-closed ante enlace/reparse, anti-ambigüedad de
  ejecutables).
- `tree_digest` sale de RV-1 (`inventory_tree` + `tree_digest_from_files`):
  `(relpath, size, sha256)` por archivo, agregado e independiente del root físico.
- El `critical[exe]` ata la versión al **contenido** del ejecutable, no sólo a su
  string.
- Evidencia **auxiliar, no identidad**: `provider_metadata` (para el proveedor
  Steam: `provider="steam"`, `appid="489830"`, `buildid`, `library_path`) y estado
  de compatibilidad SKSE (§13). El `buildid` no define por sí solo una Generation.
  No se usa la ruta dentro de Steam ni la presencia del manifest como autoridad
  (coincide con ADR 0010 §11.4).
- `generation-id` propuesto: `"<display_version>__<tree_digest[:12]>"` (legible y
  libre de colisiones por contenido). Decisión final de esquema en P2.

### 9.1 SourceSnapshotEvidence (evidencia de fuente, no autoridad)

El Candidate **no puede** generar la evidencia esperada contra la que él mismo se
aprueba (SFR-15). La evidencia esperada proviene de una observación sellada de la
Managed Source **estabilizada** (§18), capturada **antes** de crear el Candidate:

```text
SourceSnapshotEvidence = {
    provider:          "steam"                                # proveedor de la Managed Source observada
    runtime_identity:  RuntimeIdentity(game_key, game_version)   # observe_runtime_identity_from_root
    tree_digest:       TreeDigest(digest, files, bytes)          # inventory_tree + tree_digest_from_files
    critical_evidence: [CriticalFileEvidence("SkyrimSE.exe", sha256, size), ...]
    provider_metadata: optional { appid, buildid, library_path } # advisory, no identidad
}
```

Reglas:

- `SourceSnapshotEvidence` **no se llama Golden** y **no tiene autoridad GP2**. Es
  simplemente evidencia de la fuente concreta que se pretendía copiar. El modelo
  concreto de campos se define en P1/P2; acá se fija la forma.
- Se captura **PRE** (antes de copiar) y se re-observa **POST** (después de
  verificar el Candidate). Si POST != PRE, el Candidate es `INVALID / SOURCE_CHANGED`
  aunque internamente sea consistente (§10).
- El inventario del Candidate se compara **contra** `SourceSnapshotEvidence`
  (identity + digest + critical). La auto-medición del Candidate nunca es la
  expectativa.

## 10. Ciclo de vida del Candidate

Estados: `none → building → ready → promoted` o `building → invalid`. El estado
`invalid` lleva motivo: `INCOMPLETE`, `CORRUPT` o `SOURCE_CHANGED`.

Flujo requerido (SFR-15):

```text
stabilize Managed Source                       (§18)
→ capture SourceSnapshotEvidence PRE           (§9.1)
→ create Candidate from Managed Source          (staging + publish atómico no-clobber, §15)
→ inventory Candidate                           (RV-1, fail-closed)
→ Candidate TreeDigest MUST equal SourceSnapshotEvidence.tree_digest
→ Candidate RuntimeIdentity MUST equal SourceSnapshotEvidence.runtime_identity
→ Candidate critical evidence MUST equal SourceSnapshotEvidence critical evidence
→ directory membership PRE/Candidate/POST MUST match   (P3 BLOCKER, §26-14:
   el TreeDigest no sella directorios vacíos; sin esta comparación un directorio
   vacío removido/omitido pasa desapercibido)
→ reobserve Managed Source POST                 (§9.1)
→ POST source evidence MUST equal PRE source evidence
→ only then Candidate = READY
```

1. La evidencia esperada es SIEMPRE `SourceSnapshotEvidence`, nunca el resultado de
   inventariar el Candidate (SFR-15).
2. Cualquier discrepancia Candidate-vs-evidencia ⇒ `invalid` (`INCOMPLETE`/
   `CORRUPT`; F2/F3).
3. Si la Managed Source cambió entre PRE y POST ⇒ `invalid / SOURCE_CHANGED`, **aunque
   el Candidate sea internamente consistente** (F8). No se promueve lo que ya no
   representa a la fuente observada.
4. `READY` no cambia nada de la activa; **no hay auto-promoción** (SFR-05/SFR-08).

Un Candidate incompleto, corrupto o con fuente cambiada **nunca** puede convertirse
en el runtime activo (F2/F3/F8).

## 11. Ciclo de vida de Promotion

Promotion es **completa** sólo cuando el par Desired Generation/Desired Clone y el
Effective Runtime están **probadamente coherentes** (SFR-16). Un `state/active.json`
reescrito con éxito, con MO2/SKSE ejecutando todavía otra ruta, es un **estado
ambiguo, no un éxito**.

Promotion se parte en dos fases con contratos distintos (SFR-23):

```text
FASE 1 — PREPARACIÓN (no activa; no requiere aprobación)
  publicar Generation G        # versions/G (sin tocar el runtime activo; SFR-09)
  verify G                     # identidad + tree_digest + críticos + integridad física (fresco)
  instantiate C                # Runtime Clone desde G (RV-2 → RV-3); lifecycle CREATED; SFR-20
  provision C                  # RuntimeSetupManifest S (SFR-22): SKSE, root files, componentes
                               # → PROVISIONING → PROVISIONED (R4-F9)
  gate C                       # activación: §29.3  →  PREPARADO

FASE 2 — ACTIVACIÓN (muta el Effective Runtime; requiere aprobación)
  source baseline coherente    # Desired source == Effective == RuntimeCloneRecord (§36.3)
                               # divergencia ⇒ FAIL_CLOSED, sin aprobación ni PENDING
  user approves                # SFR-08; operation = PROMOTION;
                               # scope = {source_activation, source_activation_digest,
                               #          generation_id, clone_id, clone_evidence,
                               #          runtime_setup_id, runtime_setup_evidence,
                               #          compatibility_evidence_id, candidate_id,
                               #          approval_id}
  reverify approved evidence   # re-verificar EXACTAMENTE lo aprobado (SFR-23; R4-F5)
  CAS CREATE PENDING PROMOTION # intención durable ANTES de mutar (R4-F3; §36.6)
                               # + consumo atómico de la aprobación (§36.5)
  bind Effective Runtime → C   # primitiva P5 (re-apuntar gamePath/SKYRIM_PATH, o alias)
  verify effective_path == C.root_path  y  linaje C → G
  CAS UPDATE active.desired_generation_id = G  y  active.desired_clone_id = C   # §36.6
  POST verify de coherencia    # SFR-16
                               # si FALLA ⇒ la transición sigue PENDING; NO se finaliza
  CAS FINALIZE PENDING PROMOTION  # cerrar la transición durable SÓLO tras POST OK (R4-F3)
                               # y en la MISMA operación lógica publicar
                               # previous_activation_target = source_activation (§35.8.5)
  SUCCESS
```

Reglas:

1. **Re-verificación fresca de `G`** antes de clonar y de tocar cualquier binding
   (`verificar_generation` + `observe_runtime_identity_from_root` + `verify_tree`
   contra su `tree_digest` registrado + archivos críticos). Una `G` `DRIFTED` no
   se clona (SFR-17; F10). El Clone `C` pasa además el gate de activación de
   §29.3 antes de ser el Effective Runtime.
2. **Sin copia sobre el runtime activo** (SFR-09): `versions/G` es una Generation
   nueva; la anterior no se borra (SFR-10) y su Clone retenido queda intacto.
3. **Preparación sin aprobación; activación con aprobación** (SFR-23). Preparar
   artefactos que no se activan no pide permiso; lo que lo pide es el **binding**.
   Si el propietario cancela **antes** de que exista una transición durable, el
   runtime activo queda **idéntico** y los artefactos preparados permanecen
   **inactivos** (su limpieza es GC explícito, §23). Cancelar **después** de
   persistir `PENDING_*` es un caso distinto (D0-R2.6; §36.7): `CANCELLED` es un
   **resultado de operación**, no un estado del ciclo de vida del Clone, y no borra
   la transición, no reclama rollback y no declara estado limpio — recovery
   gobierna la transición que ya está en disco.
4. **La aprobación se liga a un conjunto exacto de artefactos**, no a la intención
   de actualizar (`ApprovalScope`, ver el contrato completo abajo): el conjunto
   incluye la **operación** aprobada y los artefactos que se van a activar. Antes
   de mutar el Effective Runtime se **re-verifica ese conjunto** (digest/identidad
   frescos y correspondencia con lo aprobado). Si algo cambió entre la aprobación
   y el binding, **no se activa**: se vuelve a pedir aprobación. Esto **cierra el
   diseño** de `P4_APPROVAL_SCOPE` y `P4_REVERIFY_AFTER_APPROVAL`;
   **su implementación sigue abierta** (§30.7, R3-F3): acá se decide el contrato,
   **no** se implementa el mecanismo de producción.

   **Contrato de `ApprovalScope` (operation-aware; R3-F4).** Una Promoción se apoya
   en un Candidate; un Rollback canónico **no** —parte de una Generation retenida y
   re-verificada (§12)—, así que exigir `candidate_id` en ambos obligaría a
   preservar eternamente un Candidate histórico sólo para poder volver atrás. La
   aprobación se liga a los artefactos **reales** que se van a activar:

   ```text
   ApprovalScope:
       operation = PROMOTION | ROLLBACK
       source_activation         # el runtime SALIENTE que se reemplaza (§35.2; §36.3)
       source_activation_digest  # SHA-256(canonical_json(source_activation))
       generation_id
       clone_id                  # identidad (NECESARIA, no suficiente)
       clone_evidence            # huella/evidencia del ESTADO aprobado del Clone
       runtime_setup_id
       runtime_setup_evidence    # digest del manifest aprobado
       compatibility_evidence_id
       candidate_id              # sólo PROMOTION
       approval_id               # identidad durable de la aprobación (§36.5)

   PROMOTION: candidate_id REQUIRED
   ROLLBACK:  candidate_id NOT_APPLICABLE   # ni string vacío, ni Candidate falso
   ```

   **El scope liga el source, no sólo el target (D0-R1; §35.2).** Sin
   `source_activation` una aprobación obtenida en otro contexto pasaría con el target
   intacto. La igualdad `aprobado == actual` se comprueba **bajo el lock**, antes de
   persistir `PENDING_*`, y el source debe ser **coherente con el Effective
   observado** (§36.3), no sólo con el estado Desired registrado.

   **Los identificadores solos NO alcanzan (R4-F5).** Un Runtime Clone es
   **mutable** por definición y el gate tolera deriva **no crítica** (§29.3): si el
   contenido del Clone o del manifest cambia después de aprobar mientras sus IDs
   siguen iguales, re-verificar sólo los IDs aprobaría un payload **distinto** del
   que el propietario vio (TOCTOU). Por eso el scope liga **evidencia de contenido**
   (`clone_evidence`, `runtime_setup_evidence`) además de identidades, con la
   invariante:

   ```text
   approval-time evidence == pre-bind freshly recomputed/revalidated evidence
   ```

   Un `ApprovalScope` que sólo compare `clone_id`/`runtime_setup_id` es
   **insuficiente** y no debe considerarse cerrado. La representación concreta de
   `clone_evidence`/`runtime_setup_evidence` (fingerprint de contenido, referencia a
   evidencia sellada, o equivalente) se elige en P4; acá se congela el **principio**
   (R4-F5).
5. **Falla parcial de binding ⇒ no hay éxito ambiguo ⇒ el runtime anterior es
   recuperable.** La promoción devuelve `SUCCESS` sólo con el par
   desired/effective **demostrado**; cualquier otro desenlace es un estado
   explícito distinto (`PENDING`/`FAILED`) con su ruta de recuperación
   (re-verificar la Generation anterior, volver a bindear su Clone si hizo falta).
   Nunca se reporta "promocionado" sobre un desired/effective divergente (SFR-16).
6. **Honestidad de la ventana**: bind-effective y persist-desired son dos pasos y
   entre ellos existe una ventana real (effective ya es `C`, desired todavía el
   anterior). No se maquilla: la mitigación es que el estado persistente
   **registre la operación en curso** (o se ejecute y verifique en el orden que P5
   elija), y que el desenlace sea `SUCCESS` sólo tras el POST-verify de coherencia.
   Si P5 elige persistir `desired` **antes** de bindear, el diseño debe incluir
   **rollback causal** explícito (revertir `desired` si el bind falla) — se
   documentará en P5, no se supone.
   **P4 es normativo respecto de este bloque**: la intención durable (transición
   pendiente) se persiste ANTES de mutar el Effective Runtime y el arranque
   reconcilia el estado intermedio (§26-15b); el orden ilustrativo de arriba se
   ajustará al implementar P4, no antes (P2 no finge resolverlo).
7. **POST verify, y sólo entonces FINALIZE** (R4.1-F1). Se observa de nuevo el
   Effective Runtime y se confirma `root_path == C.root_path` y la coherencia con el
   par desired. La transición durable se **finaliza después** de que el POST-verify
   pase; si falla, la transición **sigue `PENDING`** (recuperable) y **no** se
   finaliza. La recuperación posterior de una transición que quedó `PENDING` sigue
   F5/F9; no se finaliza antes del POST-verify. Invariante:

   ```text
   FINALIZED ⇒ POST verification already passed
   ```

   Cerrar la transición antes del POST-verify convierte una falla de verificación en
   un estado que ya no es recuperable por el arranque —exactamente el split-brain
   que la transición durable existe para evitar—. Este invariante aplica por igual a
   **promoción y rollback** (§12).
8. **Fail-closed sin observación**: hasta que P5 entregue la primitiva de
   observación del Effective Runtime (pregunta abierta Q12), la promoción **no
   puede declarar `SUCCESS`**. P4 no debe sustituir el paso de verificación de
   coherencia por un supuesto: sin observación, el desenlace es `FAILED`/
   `PENDING`, nunca éxito asumido (SFR-16).
9. **Coherencia del source baseline (D0-R2.2; §36.3).** Antes de **presentar** una
   aprobación de activación debe demostrarse que el runtime saliente es uno solo:
   `Desired source == Effective observado == RuntimeCloneRecord registrado`,
   incluido el setup correspondiente. La evidencia de la observación viaja en
   `source_activation` y queda ligada a la aprobación. Si Desired y Effective
   divergen —y **no** hay transición `PENDING` que lo explique— el estado es
   `INCONSISTENT_BASELINE`: `FAIL_CLOSED`, sin aprobación nueva, sin `PENDING`
   nuevo, sin promoción ni rollback, `REQUIRE_OWNER`/recovery (§36.4). Nunca se
   convierte el Effective en Desired ni el Desired en Effective por conveniencia.
10. **`previous_activation_target` se publica con el FINALIZE, no antes
    (D0-R2.1; §36.2).** El target histórico saliente se escribe **sólo** cuando el
    POST pasó, con el mismo `transition_id`, y como parte de la misma operación
    lógica que finaliza la transición. Antes de eso, el registro `PENDING`
    **retiene** `source_activation` para poder reconstruirlo; sobrescribir el
    histórico previo antes de una transición exitosa está **prohibido**. La
    autoridad de ese dato es el **registro de transición finalizado** (§36.8).

## 12. Rollback

```text
desired generation = G_prev        (Generation, referencia)
desired clone      = C_prev        (Runtime Clone derivado de G_prev y provisionado)
effective          = C_prev.root_path          (probado, SFR-16)
```

**El rollback tiene dos autoridades, no una.**

- **Autoridad de VERSIÓN = la Generation.** Un Runtime Clone es mutable por
  definición (juego, SKSE, DLLs, ENB, Creation Club, residuos de herramientas,
  borrados accidentales): aceptarlo como *única* autoridad de recuperación haría
  que la posibilidad de volver atrás dependiera de la copia que más se movió. La
  Generation no se ejecuta (SFR-19), así que es la referencia estable desde la que
  se puede **re-materializar** el runtime.
- **Autoridad de SETUP = el `RuntimeSetupManifest`** (SFR-22). Un Clone recién
  clonado desde una Generation **no se considera por defecto** un runtime listo
  para activación: le falta el setup operativo (SKSE, los archivos de root y el
  resto), que `ensure_skse` y compañía instalan **dentro del game dir** —es decir,
  dentro del Clone, por diseño—. Sin la segunda autoridad, "rollback"
  reconstruiría el snapshot base sin el setup que lo vuelve *nuestro* runtime
  (§30.2). El manifest declara versiones, hashes, procedencia y operaciones
  reproducibles; no necesariamente duplica el payload. **Que el árbol pueda
  ejecutar el juego y que esté listo para activación son hechos distintos**
  (§31.2); y que los artefactos declarados sigan siendo recuperables es un blocker
  aparte (§31.5).

```text
Generation G_prev              # QUÉ versión de Skyrim es
    │
    ▼  RV-2 → RV-3
Runtime Clone limpio           # snapshot base; sin setup, NO listo para activación
    │
    │ RuntimeSetupManifest S    # QUÉ necesita esa versión para ser nuestro runtime
    ▼  provisión reproducible (SKSE, root files, componentes)
gate de activación (§29.3) + gate de compatibilidad
    │
    ▼
Runtime Clone listo para activación   # candidato a Effective Runtime
```

Secuencia exacta (vía de integridad, la canónica):

1. **Seleccionar el target exacto de rollback**, nunca adivinado (R4-F10):
   `G_prev` (Generation retenida), su `S_prev` (`RuntimeSetupManifest`) y —sólo si
   se elige la vía rápida— el `clone_id` anterior. El setup **no se infiere** de la
   Generation: el `runtime_setup_id` es un dato por Clone (§19c) y una Generation
   admite varios setups, así que la identidad operativa previa debe ser
   **durable** (§19a).
2. **Re-verificar `G_prev`** (identidad fresca + `tree_digest` registrado +
   archivos críticos + integridad física, P2-B2). Si `G_prev` está `DRIFTED` o
   `INVALID`, **no es fuente de clonación**: falla cerrado o se elige otra
   Generation retenida verificada (SFR-17; F10).
3. **Instanciar un Runtime Clone nuevo** `C_prev` desde `G_prev` (RV-2 → RV-3).
4. **Provisionar `C_prev` con `S_prev`** (SFR-22): aplicar las operaciones
   reproducibles del manifest —SKSE del build exacto, archivos de root,
   componentes— y verificar sus hashes/identidades declaradas.
5. Aplicar el **gate de compatibilidad** y el **gate de activación** de §29.3 a
   `C_prev` (identidad, críticos, independencia, exclusión de la Managed Source,
   setup verificado, quiescencia).
6. **Verificar el source baseline y presentar el `ApprovalScope` exacto** de la
   operación (§31.4) con su **evidencia de contenido**: `operation = ROLLBACK`,
   `source_activation` + `source_activation_digest` (el runtime saliente, §36.3),
   `generation_id`, `clone_id`, `clone_evidence`, `runtime_setup_id`,
   `runtime_setup_evidence`, `compatibility_evidence_id`, `approval_id`;
   `candidate_id NOT_APPLICABLE` — un rollback canónico **no** debe preservar un
   Candidate histórico para poder volver atrás. Si el source baseline no es
   coherente (Desired ≠ Effective sin transición que lo explique), no se presenta
   scope: `FAIL_CLOSED` (§36.4).
7. **Aprobación del propietario** (SFR-08). Si cancela, el runtime activo queda
   **idéntico** y los artefactos preparados quedan inactivos (SFR-23).
8. **Re-verificar EXACTAMENTE la evidencia aprobada** justo antes de mutar: si algo
   cambió entre la aprobación y el binding, **no se activa** (SFR-23; R4-F5).
9. **Persistir la intención durable `PENDING ROLLBACK`** —antes de mutar el
   Effective Runtime— con la transición completa y el **target operativo previo**
   `(generation_id, clone_id, runtime_setup_id)` saliente (R4-F3/R4-F10).
10. **Bind Effective Runtime → `C_prev`** (primitiva P5, misma que promotion).
11. Verificar `effective_path == C_prev.root_path` y linaje
    `C_prev.source_generation_id == G_prev`.
12. **Persistir** el par `desired_generation_id = G_prev` **y**
    `desired_clone_id = C_prev` (atómico, `active.json` v2; nombres literales,
    §31.1) — CAS UPDATE sobre `transition_id` (§36.6). **La transición sigue
    `PENDING`**: no se cierra todavía, y `previous_activation_target` **no** se
    escribe en este paso (D0-R2.1; §36.2).
13. **POST verify** de coherencia desired/effective (SFR-16). Si **pasa**,
    **finalizar** la transición pendiente —CAS FINALIZE— y, en la **misma**
    operación lógica, publicar `previous_activation_target = source_activation`
    (el `G_saliente/C_saliente/S_saliente` que el registro `PENDING` retuvo) y
    devolver `SUCCESS`. Si **falla**, la transición **sigue `PENDING`**
    (recuperable) — **no** se finaliza y el target histórico **no** se publica.

**El ordenamiento es normativo (R4-F3; R4.1-F1).** El binding (paso 10) **nunca**
ocurre antes de persistir la intención durable (paso 9): un proceso que muera entre
ambos dejaría `active.json` describiendo el runtime viejo sin transición pendiente,
es decir el split-brain desired/effective que §11 exige registrar antes de mutar. Y
la transición **nunca se finaliza antes del POST-verify** (paso 13): la invariante
`FINALIZED ⇒ POST verification already passed` vale igual para promoción y rollback.
Y `previous_activation_target` **nunca se escribe antes del POST exitoso** (D0-R2.1;
§36.2): el histórico previo sigue siendo autoridad hasta que la transición nueva se
complete, y se publica como parte del mismo acto lógico que finaliza la transición.
Promoción y rollback usan **el mismo modelo de transición durable** —no hay una
ruta "rápida" que saltee la intención previa **ni** el POST-verify—; el formato
concreto del registro de transición es de P4 (`P4_DURABLE_TRANSITION`, §29.8), pero
el **ordering** queda congelado acá.

**Vía rápida opcional (no autoridad).** Reactivar un Runtime Clone anterior
**retenido** `C_prev_old` —con el setup del usuario (SKSE, ENB) intacto— es más
barato que re-clonar, y es legítimo **sólo** si se cumplen **las tres** condiciones,
sin excepción: (a) pasa el gate de §29.3 con la evidencia crítica **registrada en la
metadata de su Generation de origen** (no con su propia medición); (b) su Generation
de origen sigue `VALID`; y (c) el setup **observado** del Clone coincide con el
manifest declarado.

Las tres son **condiciones de autorización, no de diagnóstico** (R4.1-F2):

```text
(a) NO se cumple  → retained Clone activation = REJECTED
(b) NO se cumple  → retained Clone activation = REJECTED
(c) NO se cumple  → retained Clone activation = REJECTED
```

- **Si el setup difiere (c)**, la activación falla cerrado (R4-F4): la diferencia se
  **reporta** como diagnóstico, pero un Clone retenido con setup incompatible o
  incompleto **no se activa** hasta ser reparado/re-provisionado y **re-verificado**.
- **Si la Generation de origen no está `VALID` (b)**, la activación del Clone
  retenido queda **RECHAZADA** —no "posible con advertencia"—: la vía rápida existe
  *porque* la reparación por re-clonado sigue disponible, y sin una Generation
  `VALID` esa ruta no existe. Que el Clone exista, que SKSE parezca intacto o que el
  setup se vea correcto **no** autoriza: son indicios de diagnóstico.

"Activar igual y avisar" está prohibido en los tres casos: contradiría SFR-22, SFR-17
y el gate de §29.3. **Un Clone retenido no reemplaza a la Generation como autoridad
recuperable**, y no se admite cuando esa autoridad no está `VALID`.

No se reconstruyen archivos destruidos "a mano". El rollback **no depende de la
Managed Source**: se materializa desde una Generation retenida y re-verificada,
nunca desde la fuente. La re-verificación incluye la **integridad física fresca**
de la Generation (SFR-18 on-demand, P2-B2), así que no hace falta que la Managed
Source exista para verificar: si `G_prev` no está retenida, está `DRIFTED` o viola
su integridad física, el rollback falla cerrado (por eso MVP **no** borra
generaciones y **re-verifica** antes de materializar).

**Clasificación de Creation Club: sin adjudicar.** Qué parte de Creation Club ya
forma parte del snapshot (y pertenece a la Generation) y qué parte es estado
operativo añadido después (y pertenece al `RuntimeSetupManifest`) **no se decide
por intuición en este PR**. Requiere evidencia del rig (el audit registró payload
de Creation Club escrito por el juego: Q8/Q22). Mientras no esté clasificado, el
manifest debe **declarar** qué asume, no absorberlo en silencio.

**Trade-off de almacenamiento (declarado, no resuelto).** La vía canónica cuesta
≈1 árbol completo por rollback (§29.9), y el manifest debe permitir re-provisionar
sin guardar todo el payload duplicado. La retención/GC de Clones y Generations
sigue fuera del MVP (§23). Prioridad explícita: **integridad > velocidad**.

## 13. Integración MO2 / SKSE

Preguntas a resolver y hallazgos del censo:

- **¿Qué setting determina el game path?**
  - Sky-Claw resuelve `SKYRIM_PATH` desde entorno
    (`sky_claw/app/core/path_resolver.py::get_skyrim_path`), derivado de
    `local_cfg.skyrim_path`.
  - MO2 guarda su `gamePath` (y `selected_profile`) en su `ModOrganizer.ini` como
    `@ByteArray` de Qt; el repo **explícitamente no los decodifica** hoy
    (`path_resolver.py` §notas). Editar `gamePath` es un ítem abierto (P5).
  - La auditoría probó que MO2 puede ejecutar `SkyrimSE.exe` desde un Stock
    externo, así que repuntar el game path de MO2 es viable operativamente.
- **¿MO2 permite usar la copia aislada directamente?** Evidencia empírica
  (`MO2_EXTERNAL_RUNTIME = PROVEN`) dice que sí para una instalación externa. El
  mecanismo de binding (repuntar el path vs alias estable) queda como Open
  Question.
- **¿Qué ejecutable inicia el usuario?** MO2 → `skse64_loader.exe` (o
  `SkyrimSE.exe`/`SkyrimSELauncher.exe`) desde el root del **Runtime Clone**
  (nunca desde una Generation: SFR-19).
- **¿SKSE resuelve DLLs desde ese root?** SKSE carga por ruta fija (loader + DLL
  de runtime del build exacto en la raíz del juego). `scanner.find_skse_installation`
  ya valida loader + `skse*.dll` compatibles con la versión exacta
  (`skse_dll_game_version`, `skyrim_version_matches`). Esto alimenta el gate
  `RUNTIME_COMPATIBILITY`.
- **¿Qué configuración cambiaría al promover?** El puntero del Frozen Runtime
  (Desired Generation + Desired Clone) + las superficies del game path que
  determinan la Effective Runtime, una sola vez por promoción; todas deben quedar
  **probadamente coherentes** (SFR-16). El censo de P0.4 (§29.4) encontró **seis**
  superficies, no dos: `skyrim_path` persistido, `SKYRIM_PATH` en el entorno,
  raíces del sandbox de `PathValidator`, snapshot del `EnvironmentScanner` (con su
  paridad con `AutoDetector`), el `gamePath` de MO2 y los ejecutables de MO2 con
  ruta absoluta / `moshortcut://SKSE`. La primitiva de binding de P5 debe moverlas
  **como una unidad** o demostrar que derivan entre sí; mover una sola viola SFR-16
  (patrón "dos superficies, un recurso" de `AGENTS.md`). **P0 no modifica** nada de
  esto; sólo lo documenta (F6: si es incompatible, no hay migración forzada).
- **¿Quién ESCRIBE en el árbol del juego?** Pregunta distinta de la anterior y
  relevante para SFR-19/20: no alcanza con resolver bien el game path, hay que
  saber quién muta lo que ese path apunta. El censo de P0.4 (§29.4) refuta la
  afirmación de que `ensure_skse` es el único mutador: `grass_cache_runner` escribe
  `PrecacheGrass.txt` en el root del juego, y `output_targets.py` enumera varios
  destinos administrados bajo el game dir (`Pandora_Output`, `BodySlide_Output`,
  `Data/Bashed Patch, 0.ESP` de Wrye Bash, y el `<game>/Sky-Claw/DynDOLOD` legacy
  de sólo-recovery). **Todos deben apuntar al Runtime Clone, nunca a la
  Generation**, y el conjunto debe congelarse con un ancla enumerativa en P5.

Gate futuro:

```text
RUNTIME_COMPATIBILITY: UNKNOWN | COMPATIBLE | INCOMPATIBLE
UNKNOWN != COMPATIBLE     # por defecto
```

## 14. Matriz de fallas (F1–F10)

| # | Escenario | Esperado | Mecanismo |
|---|---|---|---|
| **F1** | Steam actualiza mientras el usuario juega el Frozen Runtime | Frozen unaffected | Aislamiento físico: árboles disjuntos (SFR-02/04). |
| **F2** | La copia del Candidate falla a mitad | active unaffected; candidate INVALID/INCOMPLETE | Staging temporal + publicación atómica no-clobber; el Candidate no se publica hasta verificar. |
| **F3** | El Candidate valida mal | no promotion | Verificación contra `SourceSnapshotEvidence` (SFR-15); identidad completa. |
| **F4** | El usuario no autoriza | active unchanged indefinitely | No hay auto-promoción (SFR-08). |
| **F5** | La promoción falla | previous active remains usable | Puntero atómico + reversión a la generación anterior; nunca se borra la activa. |
| **F6** | Nueva versión incompatible con SKSE/mods | no forced migration | Gate `RUNTIME_COMPATIBILITY`; `UNKNOWN != COMPATIBLE`. |
| **F7** | El usuario decide volver atrás | runtime anterior utilizable de nuevo | Rollback = re-materializar un Runtime Clone desde la Generation retenida **+ provisionar su RuntimeSetupManifest** + activar (§12; SFR-10/22). No es un simple "repuntar": un Clone sin setup provisionado **no está listo para activación**, aunque el árbol pueda ejecutar el juego (§31.2). |
| **F8** | La Managed Source cambia entre PRE y POST de la captura | candidate `INVALID / SOURCE_CHANGED`, aunque sea internamente consistente | Re-observación POST vs `SourceSnapshotEvidence` PRE (SFR-15; §10). |
| **F9** | Split-brain: `desired` y `effective` divergen tras una promoción | **no hay éxito ambiguo**; estado explícito (`PENDING`/`FAILED`) + recuperación a `A` | SFR-16: `SUCCESS` sólo con coherencia probada; POST verify; rollback causal si P5 elige persist-desired primero (§11). |
| **F10** | Generation `DRIFTED` al momento de rollback/reactivación | fail-closed; no se reactiva en silencio | Re-verificación obligatoria de identidad antes de rollback/reactivación (SFR-17; §12). |

## 15. Reuso de RV-1 / RV-2 / RV-3

Verificado leyendo el código de `sky_claw/local/runtime_vault/` sobre
`origin/main@0103ee4f`:

| Capa | Ubicación | Contrato real | Decisión |
|---|---|---|---|
| **RV-1** | `inventory.py`, `verification.py`, `models.py` | Inventario completo sellado (falla ante mutación concurrente y enlaces), `TreeDigest` independiente del root, `verify_tree`, `verify_runtime_identity`. Sin dependencias de GP2. | **REUSE tal cual.** |
| **RV-1b** | `runtime_observation.py` | Observación fresca de identidad de runtime desde un root (fail-closed, anti-ambigüedad). Sin dependencias de GP2. | **REUSE tal cual.** |
| **RV-2** | `golden.py` | `verify_golden_master` exige **evidencia independiente** (`expected_tree`, `expected_runtime`, `critical_expectations`) y devuelve un `GoldenMasterDescriptor` con `role="reference_only"`; `HASHING_A_FOLDER_DOES_NOT_MAKE_IT_A_GOLDEN_MASTER`. | **ADAPT** para Managed Source → Candidate (no hay expectativa independiente; sería auto-confianza). **ADAPT fino** para Generation → Clone: la expectativa independiente **existe** (la metadata registrada de la Generation, nacida de `SourceSnapshotEvidence`), pero hay que (a) convertir `GenerationMetadata.critical_files` (`FileIdentity`: `rel_path`/`size`/`digest`) a `CriticalFileExpectation` (`rel_path`/`expected_digest`/`expected_size`), y (b) precederlo con `verificar_generation`, porque `verify_golden_master` **no** verifica la integridad física (`st_nlink==1`) que SFR-18/P2-B2 exigen. |
| **RV-3** | `clone.py` | `create_runtime_clone(golden_source: GoldenMasterVerificationResult, ...)` exige fuente **VERIFIED** con `descriptor.role == "reference_only"`; `RuntimeCloneResult` exige `source_golden` no nulo y `descriptor.runtime_identity == source_golden.runtime_identity`. Copia real, staging hermano, publish atómico no-clobber, independencia física, **re-validación del origen PRE y POST**. Sin dependencias de GP2. | **REUSE** para Generation → Clone: es el uso para el que fue diseñado, y la re-validación PRE/POST del origen ya convierte "Generation derivada" en fallo fail-closed por sí sola (verificado). **No reusar** su `destination_lock` como lock de P4: su lockfile vive en `tempfile.gettempdir()` y su clave es la ruta destino, así que dos operaciones sobre el mismo root con destinos distintos **no** se excluyen (`P4_CROSS_PROCESS_LOCK`, §29.8). |
| **P3 (`copying.py`)** | `frozen_runtime/copying.py` | Copia **cross-volume** por enumeración sellada de PRE, sin hardlink, con re-admisión del destino. | **NO reusar** para Generation → Clone: su contrato asume una fuente *no confiable* (se enumeró PRE) y no ofrece staging hermano + publish atómico; para Generation → Clone el motor correcto es RV-3. |

`RV-3` responde las preguntas de §9 del pedido: crea copia física real, preserva
archivos, maneja directorios vacíos (`_capture_directory_structure`), rechaza
links/reparse (fail-closed), verifica independencia física por inodo, y **no**
requiere GP2. Su limitación era la **autoridad de fuente** (Golden verificado), no la
copia — y esa limitación **desaparece** en el paso Generation → Clone, donde la
Generation es exactamente un "Golden verificado" con rol `reference_only`.

## 16. Dependencias GP2 excluidas explícitamente

Este proyecto **no** importa ni requiere:

```text
golden_mutation_lock / GoldenMutationLock
protection_journal / WAL / FlushFileBuffers
authorized_plan / authorized_plan_store / operator_token / privileged_boundary
mutation_executor / target_dacl / golden_admission*
trusted_registry / trusted_namespace
inspect_golden_protection (GP1) como gate
GP1, GP2, S4-A, S4-B, S4-C, S4-D, S4-E
```

`Golden Admission` (#624) y el `Steam-managed mirror` de #494 se citan sólo como
contexto conceptual; no se importa su provenance.

## 17. Consideraciones de seguridad

Threat model nuevo y reducido. Se protege contra: actualización de Steam,
actualización accidental, promoción equivocada, Candidate incompleto/corrupto,
cambio de versión incompatible, operador eligiendo la versión equivocada, y fallo
durante creación/promoción.

Controles:

1. **Aislamiento físico primero.** La protección no es un lock: es que Steam no
   administra el Frozen Runtime (§7, SFR-02/04).
2. **Admisión de rutas e independencia física.** El `FrozenRuntimeRoot` y las rutas
   de Generation se validan: no deben estar dentro de `steamapps/common/<juego>`; se
   resuelve la ruta física (ancestros incluidos) y se rechazan symlink/junction/
   reparse (reusar `sky_claw.app.security.links` y las guardas de RV-3: igualdad
   léxica/física, anidamiento, traversal). La independencia física entre Generation
   y Managed Source se verifica por inodo al crear (RV-3), la copia no crea
   hardlinks (`shutil.copy2`), y los links hacia/desde la Managed Source se
   rechazan (SFR-18). **Cada componente persistente** (`versions/`, `candidates/`,
   `state/`, `state/generations/`) se admite fail-closed ANTES de crear o
   escribir (P2-B1): un junction/symlink existente ⇒ rechazo, sin seguir el
   target ni tocar el árbol externo (test con sentinel). El chequeo de
   `steamapps/common` incluye la ruta misma, no sólo sus ancestros (P2-M1).
   Las ESCRITURAS re-admiten el namespace justo antes de mutar (un link
   inyectado DESPUÉS del init tampoco redirige `active.json` ni la metadata);
   los paths relativos se absolutizan con `abspath` (no `resolve`) para que el
   chequeo de enlaces vea la ruta original, no el target (cierre de revisión
   adversarial P2.1).
3. **Sin ACL mutation** (SFR-12): no `WRITE_DAC`, no helper privilegiado, no UAC.
4. **Puntero atómico:** `temp + os.replace` en el mismo directorio (patrón del
   repo); un fallo a mitad no puede truncar el estado.
5. **No auto-promoción** (SFR-08): la IA puede recomendar, no autorizar.
6. **No editar Steam** (SFR-11): el appmanifest/buildid es evidencia **advisory**,
   nunca autoridad ni objeto de escritura.
7. **Sin secretos:** el estado persistente no contiene credenciales.

Riesgos y mitigaciones:

| Riesgo | Mitigación |
|---|---|
| Copiar una Managed Source en plena escritura | Gate de estabilización (§18) + `inventory_tree` falla cerrado ante mutación concurrente. |
| Capturar una actualización parcial como Candidate válido | Estabilización + verificación de identidad completa (conteo de archivos + `tree_digest`). |
| MO2/SKSE apuntando a una Generation borrada | MVP no borra generaciones (SFR-10); el puntero siempre referencia una Generation retenida. |
| Destination dentro de Steam (romper SFR-02) | Admisión de rutas falla cerrada. |
| Hardlink/symlink/junction entre Managed Source y una Generation (mutación indirecta vía Steam) | SFR-18: rechazo de links + verificación de independencia por inodo en la creación. |
| Espacio en disco durante la copia | Pendiente declarado (P2); la copia necesita ~espacio de un juego completo. |

## 18. Estabilización de la Managed Source ("¿el proveedor terminó?")

**No se asume** `appmanifest cambió = actualización terminada`. Estado actual del
censo: no existe en el repo un parser del contenido de `appmanifest_489830.acf`
(sólo se chequea su **existencia** en `scanner._detect_store`), ni detección de
`steamapps/downloading`, ni de archivos `.part`.

Regla de naming y de diseño: **observaciones específicas del proveedor** (las
señales de Steam) sobre un **contrato de estabilidad independiente del proveedor**
(dos inventories sellados idénticos). P1 demuestra el algoritmo inicialmente con
Steam y sus señales (`appmanifest`, `downloading/<appid>`, archivos parciales,
inventory estable); el contrato no nombra a Steam.

Estrategia propuesta (gate explícito de **P1**, no demostrada en P0):

```text
STABLE(ManagedSource) ⇔
  (a) appmanifest_489830.acf en estado idle (no "Update Queued/Required/App Running"),
      leído como evidencia advisory; Y
  (b) ausencia de artefactos en-progreso (p. ej. steamapps/downloading/<appid>,
      archivos .part) ; Y
  (c) dos inventories sellados completos de la Managed Source, separados por una ventana
      silenciosa, producen el MISMO TreeDigest (y sin cambios de membresía).
```

`inventory_tree` es el sensor natural: su sello de estabilidad por archivo y de
árbol ya falla cerrado ante mutación concurrente, así que un árbol en escritura no
produce un digest válido. Si P1 no puede demostrar (a)–(c) en el rig real, la
creación de Candidate se **bloquea**, no se adivina.

**Residuo declarado:** una actualización parcial que **ya terminó** y dejó el árbol
estable antes de la ventana silenciosa no es distinguible por inventario (el sello
sólo detecta mutación **durante** el inventario). Las señales del proveedor
(a)/(b) y el rig de P1 deben cerrar o **declarar** ese residuo; nunca se lo asume
cerrado por omisión.

**Estado de implementación (P1, PR #673):** el contrato multi-señal quedó
implementado en `sky_claw/local/frozen_runtime/` (discovery → provider pre-check →
inventory PRE → ventana silenciosa configurable → provider post-check → inventory
POST → comparación), con veredictos tipados `STABLE/UNSTABLE/INDETERMINATE` y
`INDETERMINATE != STABLE`. Cobertura de tests S01–S11 + caso adversarial
"mutación que retorna a la misma versión/buildid superficial" en
`tests/test_frozen_runtime_p1.py`. El probe READ-ONLY sobre la máquina de
desarrollo dio `NOT_FOUND` (sin Skyrim de Steam local): la demostración con
señales reales de Steam queda en P7. **Q-04 se cierra sólo en el alcance
demostrado** (contrato de ventana observada); la detección absoluta de
actualizaciones de Steam permanece NO demostrada y declarada como gate de P7.

## 19. Modelo de datos (decidido e implementado en P2)

Dos piezas mínimas, ambas versionadas (``schema_version`` v1):

**a) Estado de intención** — `state/active.json` (SFR-16: registra **intención**;
NO registra Effective Runtime y jamás afirma por sí solo promoción exitosa).

**v1 — implementado en P2** (una sola identidad: el Desired Active Generation):

```json
{
  "schema_version": 1,
  "desired_active_generation": "1.6.1170__a1b2c3d4e5f6",
  "updated_at_ns": 1789000000000000000
}
```

**v2 — propuesto en P0.4** (el par Generation + Clone; §30.3): `generation_id` no
alcanza, porque una Generation admite **varios** Clones (`G1 → C1, C2, C3`) y
`desired_active_generation = G1` no dice cuál debe ejecutar MO2. El par sí:

```json
{
  "schema_version": 2,
  "desired_generation_id": "1.6.1170__a1b2c3d4e5f6",
  "desired_clone_id": "1.6.1170__a1b2c3d4e5f6--c3",
  "updated_at_ns": 1789000000000000000
}
```

`previous_activation_target` **no** vive en `active.json` v2 (D0-R2.7; §36.8): su
autoridad es el **registro de transición finalizado** y tenerlo en dos archivos
crearía dos autoridades que pueden divergir. La corrección del schema v2 se registra
en §36.8.

- `desired_generation_id: null` **y** `desired_clone_id: null` = arranque limpio
  (sin runtime activo). Un par **parcialmente** nulo (uno sí, otro no) es estado
  **corrupto**, no "limpio": fail-closed.
- **Identidad operativa previa (R4-F10; corregido en D0-R2.1/D0-R2.7).** El par
  `desired_*` describe el runtime **actual**; al promocionar se sobreescribe. Como el
  `runtime_setup_id` es un dato **por Clone** (§19c) y una Generation admite varios
  setups, la identidad del runtime anterior no es derivable de `G_prev`: sin
  registrarla, el rollback tendría que **adivinar** qué setup correspondía al runtime
  anterior. Por eso el sistema conserva `previous_activation_target` (el target
  **saliente**). Regla dura:
  ```text
  rollback target must never be guessed
  previous operational setup identity must be durable
  ```
  **Dónde y cuándo se escribe (D0-R2.1).** `previous_activation_target` se publica
  **sólo** cuando el POST pasó, con el mismo `transition_id`, como parte de la
  **misma operación lógica** que finaliza la transición (§35.8.5; §36.2). Antes de
  eso el histórico previo sigue siendo autoridad y el registro `PENDING` **retiene**
  `source_activation` para reconstruirlo. Escribirlo al persistir el par desired
  —como decía el texto anterior a esta corrección— publicaba un histórico que la
  transición todavía no había ganado.
  **Quién es la autoridad (D0-R2.7; §36.8).** El registro de transición finalizado,
  **no** `active.json`. Se elige **A (un único target previo, acotado y derivado)**
  sobre B (ledger de activaciones de profundidad arbitraria) porque reutiliza la
  maquinaria de transición durable que R4-F3 ya exige y respeta el presupuesto de
  complejidad del MVP (§21); y se evita duplicar el dato en dos archivos porque dos
  copias pueden divergir. El `clone_id` previo queda disponible para la vía rápida
  opcional (§12), pero **no** convierte al Clone anterior en autoridad. Si P4
  concluye que una Generation puede mapear a **varios** manifiestos canónicos, el
  setup previo se **registra**, nunca se infiere. El formato concreto se implementa
  en P4.
- Reader fail-closed: **ausente** = limpio; **presente pero corrupto**
  (JSON malformado/truncado, UTF-8 inválido, schema desconocido, campo ausente
  o con tipo incorrecto, id con traversal) LANZA — nunca se interpreta como
  ausente.
- Escritura atómica: `mkstemp` en el MISMO directorio → `fsync` del archivo →
  `os.replace` → cleanup del temporal ante fallo (patrón del repo en
  `local_config.py`). El temporal nunca vive en `%TEMP%` global.
- **Migración v1 → v2 = P4, fail-closed.** Este PR **no la implementa**: sólo
  congela el contrato. Un archivo v1 no se reinterpreta como v2 ni se "adivina" el
  Clone a partir de la Generation; mientras P4 no implemente la migración, un
  `active.json` v1 sigue leyéndose con las reglas de v1.
- **POWER_LOSS_GUARANTEE = NOT_CLAIMED**: reemplazo atómico del namespace bajo
  semántica normal de proceso/filesystem local; no hay WAL ni GP2.

**b) Metadata por Generation** — `state/generations/<generation-id>.json`
(fuera del árbol de la Generation: el árbol queda byte-idéntico al snapshot y
el digest no cambia al publicar):

```json
{
  "schema_version": 1,
  "generation_id": "1.6.1170__a1b2c3d4e5f6",
  "display_version": "1.6.1170",
  "runtime_identity": { "game_key": "skyrimse", "game_version": "1.6.1170.0" },
  "tree_digest": { "digest": "<sha256 completo de 64 hex>", "files": 0, "bytes": 0 },
  "critical_files": [ { "rel_path": "SkyrimSE.exe", "size": 0, "digest": "…" } ],
  "provider": "steam",
  "provider_appid": "489830",
  "provider_buildid": "…",
  "created_at_ns": 0
}
```

- El id legible usa un prefijo de 12 hex, pero la metadata conserva el digest
  **completo** (el buildid del proveedor es auxiliar y jamás define identidad).
- La metadata NO auto-autoriza (SFR-15 conceptual): `VALID` sólo sale de
  comparar un inventario sellado fresco contra el digest registrado.
- Colisión: mismo id con identidad completa distinta ⇒ fail-closed
  (`GenerationCollisionError`); nunca se sobreescribe en silencio.
- No se persisten flags mutables de estado (VALID/DRIFTED se derivan on-demand
  de evidencia fresca; sin flags que envejezcan).

**c) Metadata por Runtime Clone** — `state/clones/<clone-id>.json` (P0.4;
propuesto). Es el `RuntimeCloneRecord`: la identidad **lógica** del target
operativo, con su linaje y su setup. Registra **linaje**, no inferencia: el
`source_generation_id` es un dato declarado, no algo que se deduzca de la ruta.

```json
{
  "schema_version": 1,
  "clone_id": "1.6.1170__a1b2c3d4e5f6--c3",
  "source_generation_id": "1.6.1170__a1b2c3d4e5f6",
  "lifecycle": "PROVISIONED",
  "intended_runtime_setup_id": "skyrimse-1.6.1170--s3",
  "verified_runtime_setup_id": "skyrimse-1.6.1170--s3",
  "root_path": "<FrozenRuntimeRoot>/clones/<clone-id>",
  "admitted_role": "runtime_clone",
  "created_at_ns": 0
}
```

- `admitted_role` es la autoridad del gate (SFR-19/§30.5): un target operativo sólo
  es admisible si `admitted_role == "runtime_clone"`, `target != Generation` y el
  linaje es válido. **No** se usa un prefijo de ruta: dónde viven físicamente los
  Clones sigue abierto (Q18). Además, `admitted_role` es autoridad de **admisión**,
  así que su integridad es un requisito explícito (R4-F7): ver abajo
  `RUNTIME_CLONE_RECORD_INTEGRITY`.
- **Ciclo de vida del provisionamiento (R4-F9).** El flujo promocional **instancia**
  el Clone y **después** provisiona su setup (SFR-22, §11). Un corte entre ambos
  pasos no puede dejar un registro que **afirme** un setup aplicado cuando no lo
  está, ni un Clone sin rastrear. Por eso el registro distingue el setup
  **intencionado** del **verificado** mediante un estado explícito:
  ```text
  RuntimeCloneLifecycle = CREATED | PROVISIONING | PROVISIONED | INVALID
  ```
  - `intended_runtime_setup_id` = el setup que **debe** aplicarse.
  - `verified_runtime_setup_id` = el setup efectivamente aplicado y verificado;
    `null` hasta que el gate confirme hashes/identidades.
  - **Fail-closed:** la activación exige `lifecycle == PROVISIONED` y
    `verified_runtime_setup_id != null`. La mera presencia de un
    `intended_runtime_setup_id` **no** implica setup aplicado. Un Clone en
    `CREATED`/`PROVISIONING` tras un crash es **descubrible y clasificable** al
    arranque (reconciliación de `P4_DURABLE_TRANSITION`), y **no** es activable.
  El formato exacto de la FSM se implementa en P4; acá se congela la semántica.
- `root_path` es informativo y **se re-verifica** contra la realidad en el gate; un
  `root_path` que no coincide con el árbol observado no admite el target. La
  re-verificación incluye **exclusión física de la Managed Source** (R4-F6): la
  identidad lógica es **necesaria pero no suficiente**.
- **`RUNTIME_CLONE_RECORD_INTEGRITY = OPEN`** (R4-F7). Este registro es autoridad
  de admisión, pero —a diferencia de la metadata de Generation y del
  `RuntimeSetupManifest`— no tenía blocker de integridad propio. Un JSON
  sustituido o editado válidamente podría etiquetar un árbol arbitrario como
  `runtime_clone` apuntando a una Generation válida; el gate verifica identidad de
  runtime, críticos, setup e independencia, pero **no** la derivación completa
  desde esa Generation, así que el linaje falso podría aceptarse. Requisitos
  conceptuales (no se decide HMAC/firma acá): identidad no clobber, schema
  validado, consistencia `clone_id` ↔ nombre de archivo/clave, integridad de
  `source_generation_id`/`runtime_setup_id`/`root_path`, y `admitted_role` no
  forjable en silencio. Threat model: corrupción accidental, bugs y sustitución no
  autorizada por el propio sistema — **no** administrador malicioso (§17).

**d) RuntimeSetupManifest** — `state/runtime_setups/<runtime-setup-id>.json`
(P0.4; propuesto). Declara el setup operativo reproducible de una versión
(SFR-22). No duplica el payload: declara componentes, versiones, hashes y
operaciones.

```json
{
  "schema_version": 1,
  "runtime_setup_id": "skyrimse-1.6.1170--s3",
  "generation_id": "1.6.1170__a1b2c3d4e5f6",
  "components": [
    { "name": "skse64", "version": "2.2.6", "artifact_digest": "…", "install_op": "…" }
  ],
  "root_files": [ { "rel_path": "enbseries.ini", "digest": "…" } ],
  "assumptions": [ "creation_club: clasificación pendiente (Q22)" ],
  "created_at_ns": 0
}
```

- `assumptions` es **obligatorio declarar** lo que no está clasificado (p. ej.
  Creation Club, Q22): el manifest no absorbe en silencio lo que no se decidió.
- Su **integridad** (¿un manifest mentiroso puede hacer pasar un Clone por el gate?)
  es un requisito abierto de P4 (Q23).

No hay base de datos; JSON canónico mínimo basta.

## 20. Layout de filesystem (decidido e implementado en P2)

```text
<FrozenRuntimeRoot>/                 # configurable; NUNCA dentro de steamapps/common
├── versions/
│   ├── 1.6.1170__<digest12>/        # Generation: referencia, NO se ejecuta (SFR-17/19)
│   └── 1.7.xxxx__<digest12>/
├── clones/                          # Runtime Clones: copias operativas (P0.4, SFR-20)
│   └── <clone-id>/                  # Effective Runtime; mutable por uso normal
├── candidates/                      # namespace reservado (P3 crea Candidates)
└── state/
    ├── active.json                  # Desired Generation + Desired Clone (v2; SFR-16)
    ├── .active.json.<rand>.tmp      # temporal de escritura atómica (mismo dir)
    ├── generations/
    │   └── <generation-id>.json     # metadata inmutable por Generation
    ├── clones/                      # (P0.4) RuntimeCloneRecord: linaje + setup
    │   └── <clone-id>.json
    └── runtime_setups/              # (P0.4) RuntimeSetupManifest (SFR-22)
        └── <runtime-setup-id>.json
```

`clones/` y `state/runtime_setups/` son namespaces **nuevos** (P0.4): el layout de
P2 sólo tenía `versions/`, `candidates/` y `state/` (verificado en `storage.py`:
`VERSIONS_DIR_NAME`/`CANDIDATES_DIR_NAME`/`STATE_DIR_NAME`). El esquema exacto de
`active.json` v2, de la metadata del Clone y del manifest de setup se **contrata**
acá (§19) y se **implementa** en P4; `active.json` v1 debe seguir leyéndose
fail-closed. La admisión de rutas de §17 aplica también a `clones/` y a
`state/runtime_setups/` (fuera de `steamapps/common`, sin enlaces, sin solapar la
Managed Source ni la Generation).

**Ruta por defecto (resuelve Q5):** `~/.sky_claw/frozen-runtime`, **per-user**
(operación de usuario normal, sin helper privilegiado; MO2 es user-level;
ownership/lifecycle simple; sin servicio global). Es hermano de
`Config.DEFAULT_CONFIG_DIR` (`~/.sky_claw`) y de `Config.runtime_state_dir()`
por el mismo motivo que ellos: no depende de desde dónde se lanzó el proceso.
Configurable: las generaciones pueden pesar decenas de GB y el root puede
apuntarse a otra unidad; la copia cross-volume la maneja P3, y los renames
atómicos ocurren dentro del volumen del root (primitive `same_volume`).
Restricción dura: fuera del árbol administrado por Steam (admisión de rutas
rechaza solapamiento con la Managed Source y contención en `steamapps/common`).

## 21. Invariantes de complejidad (presupuesto MVP)

Esta primera versión **no** necesita: WAL de ACL, `GoldenMutationLock`, helper
privilegiado, `WRITE_DAC`, S4-C/D/E, ni protocolo de transacción ante power-loss.
Se reemplazan por `copia a nueva generación → verificar → actualizar puntero`.

```text
USES_ACL_MUTATION=NO
USES_PRIVILEGED_HELPER=NO
USES_GP2=NO
AUTO_PROMOTION=NO
DELETES_PREVIOUS_GENERATION=NO
```

## 22. Slices de implementación (roadmap)

| Slice | Contenido | Gate de salida |
|---|---|---|
| **P0** | Arquitectura / ADR / censo / roadmap (este documento) | ADR mergeado; veredicto P0. |
| **P1** | Managed Source discovery (provider: Steam) + Runtime Identity + **estabilización** + captura de `SourceSnapshotEvidence` | `STABLE(ManagedSource)` demostrado o bloqueo fail-closed. **Implementado en PR #673** (`sky_claw/local/frozen_runtime/`; Q-04 cerrado en el alcance demostrado de la ventana observada). |
| **P2** | Frozen Runtime storage + modelo de Generation + admisión de rutas | Crear/listar generations; registro atómico; rechazo de destino dentro de Steam; identidad registrada por Generation (base de `DRIFTED`). **Implementado en PR #673** (`storage.py`/`state.py`/`generations.py`/`independence.py`/`generation_id.py`; Q5/Q6/Q10 resueltas; SFR-18 ejecutable con hardlink/junction/reparse; drift on-demand). **Hardening P2.1**: admisión fail-closed de cada componente del layout (junction/symlink ⇒ rechazo, árbol externo intacto), integridad física on-demand de la Generation (`st_nlink==1`, sin Managed Source), `steamapps/common` exacto rechazado. |
| **P3** | Candidate creation + verification | Candidate `ready`/`invalid` contra `SourceSnapshotEvidence`; F2/F3/F8 cubiertos (SFR-15). **Implementado en PR #682** (`candidates.py`/`copying.py`/`membership.py`; blockers P3_DIRECTORY_MEMBERSHIP y P3_CANDIDATE_SOURCE_EVIDENCE cerrados) + **hardening post-merge en #698**. |
| **P3b** (P0.4) | Captura con update pendiente / adopción de un runtime existente | Que la versión **actual** pueda sellarse aunque Steam ya la haya marcado para actualizar, sin romper SFR-11 ni SFR-15. **Requerido antes de que la fuente vieja desaparezca, pero su implementación está diferida**: la semántica de `StateFlags` no está verificada (§29.5). Gate `P3B_UPDATE_PENDING_CAPTURE`. |
| **P4** | Explicit Promotion + Rollback | Sin copia sobre el runtime activo; F4/F5/F7/F9/F10 cubiertos; coherencia del par desired/effective probada (SFR-16) y re-verificación anti-DRIFTED (SFR-17). Con P0.4: publica la Generation, **instancia el Runtime Clone** (RV-2 → RV-3), **provisiona su `RuntimeSetupManifest`** (SFR-22), aplica el gate de activación (§29.3), implementa `ApprovalScope` operation-aware + re-verificación pre-binding (SFR-23; **diseño cerrado, implementación abierta**) y hace rollback **re-materializando el runtime** desde Generation + RuntimeSetup (§12). Incluye la **migración fail-closed `active.json` v1 → v2** (§19). **No puede declarar rollback operativo** hasta adjudicar `RUNTIME_SETUP_ARTIFACT_AVAILABILITY` (§31.5). |
| **P5** | MO2/SKSE integration (binding del game path + gate de compatibilidad) | F6; el juego arranca desde el **Runtime Clone** (nunca desde una Generation: SFR-19). Con P0.4: el binding mueve las **seis** superficies de §29.4 hacia el Runtime Clone, más los escritores del game dir, y aporta el oráculo de Effective Runtime (SFR-16). |
| **P6** | Update detection / user-facing status | Contrato de producto (§7). Con P0.4: un aviso al propietario cuando el buildid de Steam difiera del de la referencia, y estado informativo del manifest (solo-lectura / update pendiente), sin modificarlo (SFR-11). |
| **P7** | Windows real rig | Evidencia de F1/F7 en rig. Con P0.4: SFR-21 (arranques repetidos sin pasos manuales), la semántica real de `StateFlags` (§29.5) y la instanciación del Clone en Windows real (antivirus, handles sobre `os.replace` de directorios). |
| **P8** | Final integration / documentación | Cierre y sincronización de docs. |

División sujeta al código vivo de P1; se evita superar ~8 slices.

## 23. Retención

Garbage collection **fuera del MVP**. No borrar la Frozen Runtime anterior durante
la promoción. La limpieza futura será otro slice con confirmación explícita
(SFR-10).

## 24. Plan de verificación

P0 es **docs-only**:

```powershell
git diff --check
git status
git diff origin/main...HEAD --stat
```

No se ejecuta la suite completa porque no cambia código. Si por accidente apareciera
un cambio en `.py`, detenerse y justificarlo.

Tests planificados por slice (convención del repo: español, AAA, anclas
enumerativas):

| Slice | Tests |
|---|---|
| P1 | Estabilización: manifest idle vs update-in-progress; ausencia de `.part`; dos inventories idénticos ⇒ STABLE; mutación concurrente ⇒ fail-closed; captura de `SourceSnapshotEvidence` sellada. |
| P2 | Admisión de rutas (destino dentro de Steam ⇒ rechazo; symlink/junction ⇒ rechazo); puntero atómico; registro enumerado (igualdad literal); detección de Generation `DRIFTED` contra su identidad registrada. **Implementado**: L01–L10+c (layout/admisión, incluye `steamapps/common` exacto), SR01–SR08 (namespace de storage con junction/symlink y sentinel: el árbol externo jamás se toca), ST01–ST10 (estado), G01–G08 (generation-id), DR01–DR11 (drift), PI01–PI09 (SFR-18 contra la fuente con hardlink/junction reales), PI10–PI15 (integridad física on-demand: hardlink post-publicación con mismo digest ⇒ `INVALID`, sin Managed Source) + ancla AST del boundary de escritura. |
| P3 | Candidate completo y fuente estable ⇒ ready; corrupción/truncamiento ⇒ invalid; fuente cambiada PRE/POST ⇒ `invalid / SOURCE_CHANGED` aunque el Candidate sea consistente (SFR-15); fallo a mitad ⇒ activa intacta (F2/F3/F8). Caso negativo anti-self-verification: Candidate **internamente consistente pero distinto** del `SourceSnapshotEvidence` ⇒ rechazado (el test prueba el camino de comparación, no sólo el resultado). |
| P4 | Promoción no borra el runtime previo; sin coherencia del par desired/effective no hay `SUCCESS` (F9); fallo de promoción revierte y el runtime anterior queda usable (F5); sin aprobación no se **activa**, pero **preparar sin aprobación no falla** (SFR-23); una aprobación cuyo `ApprovalScope` cambió entre aprobar y bindear ⇒ no activa; rollback **re-materializa y provisiona** (F7); rollback sobre Generation `DRIFTED` falla cerrado (F10); migración `active.json` v1 → v2 fail-closed. |
| P5 | SKSE compatible/incompatible/unknown; game path re-apuntado y observado; MO2 arranca desde el **Runtime Clone**; un target cuyo `RuntimeCloneRecord` no declare `admitted_role == "runtime_clone"` (o cuya Generation no sea válida) es rechazado; coherencia del par desired/effective verificable desde el rig. |
| P7 | Rig: Steam actualiza (F1) sin tocar Frozen; rollback real (F7); **reproducibilidad del rollback con los artefactos declarados** por el manifest (R3-B1). |

## 25. Definition of Done (del proyecto, no de P0)

1. Steam puede actualizar su instalación sin tocar el Frozen Runtime (F1).
2. Toda versión nueva entra como Candidate y se verifica contra
   `SourceSnapshotEvidence` antes de promocionar (SFR-06/07/15).
3. Promoción sólo con aprobación explícita (SFR-08), sin destruir el runtime
   anterior (SFR-09) y con coherencia del par Desired Generation/Desired Clone vs
   Effective Runtime **probada** antes de declarar éxito (SFR-16). La aprobación
   queda ligada a un `ApprovalScope` exacto y se re-verifica antes de mutar el
   Effective Runtime (SFR-23).
4. **Rollback = re-materializar el runtime operativo**, no "repuntar": re-clonar
   desde la Generation destino re-verificada (SFR-10/17) **y provisionar su
   `RuntimeSetupManifest`** (SFR-22), de modo que el resultado sea jugable.
5. **MO2/SKSE nunca ejecutan una Generation en ningún flujo administrado por
   Sky-Claw** (SFR-19); ejecutan el **Runtime Clone** del par Desired activo
   (SFR-03/20). Fuera de los flujos de Sky-Claw, ejecutar una Generation a mano es
   una violación de política que Sky-Claw no puede impedir — y que **no** se
   confunde con `DRIFTED` (§30.5).
6. Sin ACL mutation, sin helper privilegiado, sin GP2 (SFR-12/13).
7. `USES_ACL_MUTATION/FROZEN... = NO` (verificación de complejidad).
8. Documentación sincronizada y evidencia de rig registrada.
9. Ninguna Generation `DRIFTED` se reactiva ni se usa como target de rollback sin
   re-verificación exitosa (SFR-17).
10. Ninguna Generation comparte objetos de filesystem mutables con la Managed
    Source (SFR-18).
11. Ningún target operativo se admite por prefijo de ruta: la admisión es por
    `RuntimeCloneRecord` (`admitted_role == "runtime_clone"`, linaje válido,
    `root_path` re-verificado) — §30.5.
12. Un Clone recién creado **no** se declara listo para activación sin su
    RuntimeSetup aplicado y verificado (SFR-22; §31.2). Eso no afirma que el árbol
    no pueda ejecutar el juego.
13. **El rollback operativo no se declara reproducible** mientras
    `RUNTIME_SETUP_ARTIFACT_AVAILABILITY` siga `OPEN`: hay que demostrar qué
    artefactos quedan retenidos o recuperables por Generation retenida (§31.5).

## 26. Preguntas abiertas (Open questions)

Sin resolver en P0; varias quedan como **gates explícitos** de P1/P2/P3/P5. No se
inventan soluciones.

1. **Estabilización de la Managed Source**: ¿(a)–(c) de §18 alcanzan para demostrar
   `STABLE(ManagedSource)` en el rig real (proveedor Steam)? Gate P1. **Estado:
   implementado como contrato de ventana acotada (P1, tests S01–S11); la
   demostración con Steam real queda para P7.**
2. **Binding de la Effective Runtime a MO2/SKSE**: ¿repuntar el `gamePath` de MO2
   (Qt `@ByteArray`) y `SKYRIM_PATH`, o un alias estable (`active` → generation) que
   MO2/SKSE/USVFS resuelvan? La auditoría probó un Stock externo, no el mecanismo de
   conmutación. Cualquiera sea la primitiva, debe permitir **demostrar** coherencia
   desired/effective (SFR-16). Gate P5.
3. **Decodificación/edición del `gamePath` de MO2**: el repo hoy no decodifica
   `@ByteArray`; ¿lo hace Sky-Claw o es una acción manual documentada? Gate P5.
4. **Adaptación de RV-3**: ¿adaptador de autoridad de fuente, o rutina de copia
   mínima sobre primitivas RV-1? Gate P3.
5. **Ruta por defecto del `FrozenRuntimeRoot`**: **RESUELTA en P2** —
   `~/.sky_claw/frozen-runtime`, per-user, configurable (§20; convención
   `~/.sky_claw` del repo; sin servicio global).
6. **Esquema de `generation-id`**: **RESUELTA en P2** —
   `<display_version>__<digest12>` con charset `[a-z0-9._-]`, validación
   fail-closed de traversal/reservados, digest completo retenido en metadata,
   colisiones fail-closed (§10/§19).
7. **Espacio en disco**: la copia requiere ~el tamaño completo del juego por
   generación. **Parcial en P2**: `same_volume` verifica el boundary de rename
   atómico; la Managed Source PUEDE estar en otro volumen (copia cross-volume
   en P3). El dimensionamiento operativo (cuánto espacio, cuántas generaciones)
   sigue abierto para P3/P4.
8. **Completitud de la captura**: ¿el conjunto de archivos de la Managed Source
   (incluyendo Creation Club/BSAs) alcanza para un runtime jugable sin Steam? La
   auditoría observó payload de Creation Club escrito por el juego; verificar en P7.
9. **Compatibilidad SKSE**: mapear `find_skse_installation` + `skse_dll_game_version`
   a `RUNTIME_COMPATIBILITY`; definir el alcance de "COMPATIBLE" (sólo SKSE core o
   también Address Library / DLL plugins). Gate P5.
10. **Per-user vs per-machine**: **RESUELTA en P2** — per-user (usuario normal,
    sin helper privilegiado; MO2 es user-level; ownership/lifecycle simple). El
    default `~/.sky_claw/frozen-runtime` es per-user y configurable; no hay
    servicio global.
11. **Downgrade/promoción hacia atrás**: ¿promover una versión menor que la activa
    es un caso soportado o se fuerza a usar rollback? Gate P4.
12. **Observación del Effective Runtime**: ¿cómo demuestra Sky-Claw qué ruta
    ejecutan realmente MO2/SKSE (lectura del game path efectivo, attestation de
    lanzamiento, canary)? Sin esa observación no hay `SUCCESS` de promoción
    (SFR-16). Gate P5.
13. **Orden causal bind/persist**: P5 decide si persiste `desired` antes o después
    de bindear `effective`; si persiste antes, debe documentar el **rollback causal**
    y sus tests (§11, regla 4). Gate P5.
14. **P3 BLOCKER — directory membership evidence**: `TreeDigest` sella archivos,
    pero no la membresía de directorios (incluidos vacíos); dos árboles con los
    mismos archivos y directorios distintos comparten digest. P3 **no puede
    declarar un Candidate `READY` sólo con `TreeDigest`**: debe sellar/comparar
    directorio-membership PRE/Candidate/POST (reutilizar `_capture_directory_structure`
    de RV-3 o extraer una primitive reusable). No se implementa en P2 por diseño.
15. **P4 BLOCKERS registrados (no implementados en P2)**: (a) **serialización
    cross-process**: todos los mutadores de Frozen Runtime deben participar de un
    único contrato de serialización (test enumerativo por introspección, patrón
    `AGENTS.md`); (b) **transición durable**: la intención debe persistirse ANTES
    de mutar el Effective Runtime (sin WAL nuevo: el estado v1 no finge
    resolverlo); (c) **approval scope**: `P4_APPROVAL_SCOPE_DESIGN = CLOSED`
    (contrato decidido en §11: `ApprovalScope` operation-aware, ligado a los
    artefactos reales que se activan, **no** a "quiero actualizar"),
    **`P4_APPROVAL_SCOPE_IMPLEMENTATION = OPEN`** (el mecanismo de producción no
    existe); (d) **reverify-after-approval**: `P4_REVERIFY_DESIGN = CLOSED` (la
    regla está fijada: `approval → re-verificar EXACTAMENTE el ApprovalScope
    (Generation fresca + Clone + RuntimeSetup + compatibilidad) → mutación`),
    **`P4_REVERIFY_IMPLEMENTATION = OPEN`**; (e) **P4 no puede promover sin el gate
    de compatibilidad disponible** (`UNKNOWN != COMPATIBLE`, implementación P5).
    **Contract decided ≠ production mechanism implemented** (§30.7, R3-F3).
16. **P5 — Effective Runtime oracle**: cómo se demuestra qué ruta ejecutan
    MO2/SKSE (Q12) y el binding de las superficies (Q2/Q13). Gate P5.
17. **(P0.4) Mecanismo de P3b**: ¿captura con update pendiente, adopción de un
    runtime existente, o ambas? ¿Cuál es el criterio de bits de `StateFlags`, y
    con qué evidencia de rig se fija? Gate `P3B_UPDATE_PENDING_CAPTURE` (§29.5).
    **Sin respuesta hoy: `STATEFLAGS_6/518_SEMANTICS = UNVERIFIED`.**
18. **(P0.4) Ubicación del Runtime Clone**: ¿bajo el `FrozenRuntimeRoot` o en la
    unidad de modding (MO2/USVFS, rutas largas, `same_volume`)? Gate P4. **No
    bloquea el modelo**: por eso la admisión del target operativo es por identidad
    **lógica** registrada (`RuntimeCloneRecord`), no por prefijo de ruta (SFR-19).
19. **(P0.4) Catálogo de archivos críticos del Clone**: hoy
    `CRITICAL_EXE_BY_GAME` sólo tiene `SkyrimSE.exe` (`models.py`); el audit del
    rig verifica además `SkyrimSELauncher.exe` y `bink2w64.dll`. Incluye el
    **re-baseline** cuando el propietario modifica un crítico a propósito. Gate P4.
20. **(P0.4) Retención/GC de Clones y Generations**: política de disco y
    confirmación explícita (§23; §29.9). Gate posterior a P4.
21. **(P0.4) Integridad de la metadata de la Generation**: es la raíz de confianza
    de `VALID` y de la autoridad de rollback, y hoy **no tiene protección de
    integridad** (§29.10-19). ¿Se acepta como límite declarado, o P4 le agrega un
    digest propio / firma? Decisión de P4.
22. **(P0.4) Clasificación de Creation Club**: qué parte ya está en el snapshot
    (y pertenece a la Generation) y qué parte es estado operativo añadido después
    (y pertenece al `RuntimeSetupManifest`). **Sin adjudicar**: requiere evidencia
    del rig (Q8). No se decide por intuición en P0.4 (§12; §30.2).
23. **(P0.4) Integridad del `RuntimeSetupManifest`**: es la autoridad del setup
    operativo (SFR-22) y **no** tiene hoy protección de integridad — misma clase de
    problema que Q21. Si un manifest mentiroso puede hacer pasar un Clone por el
    gate de activación, el gate hereda esa debilidad. Decisión de P4.
24. **(P0.4) Disponibilidad de los artefactos del `RuntimeSetupManifest`**
    (R3-B1): el manifest declara `version`, `hash`, `source/provenance` y
    `operation`, pero **nada garantiza que el payload siga siendo recuperable**. Si
    el origen es una URL externa que desaparece, quedan `Generation VALID` +
    `RuntimeSetupManifest VALID` + `artifact unavailable` ⇒ **rollback operativo no
    reproducible**. Estrategias candidatas (sin elegir): **(A)** external
    reacquisition only, **(B)** retained local payload, **(C)** content-addressed
    local artifact cache, **(D)** hybrid. Preferencia declarada:
    `rollback reproducibility should not silently depend on third-party artifact
    availability` ⇒ **A sola es incompatible con la promesa de producto salvo
    declaración explícita**. Blocker abierto (§31.5).
    ```text
    RUNTIME_SETUP_ARTIFACT_AVAILABILITY =
        OPEN / MUST_BE_ADJUDICATED_BEFORE_OPERATIONAL_ROLLBACK_IS_CLAIMED
    ```

## 27. Revisión adversarial (auto-cuestionamiento)

| Pregunta | Respuesta |
|---|---|
| ¿Steam todavía puede tocar el Frozen Runtime? | No por diseño (árboles disjuntos). Verificación de admisión de rutas lo garantiza (P2). |
| ¿Alguna ruta está dentro de `steamapps/common`? | El layout lo prohíbe y la admisión lo rechaza fail-closed. |
| ¿Promotion puede destruir la activa antes de tener reemplazo válido? | No: re-verificación previa + publicación de nueva Generation + puntero atómico; nunca copia sobre la activa (SFR-09). |
| ¿Rollback depende de reconstrucción "a mano"? | No: re-materializa un Runtime Clone desde una Generation retenida **y lo provisiona con su `RuntimeSetupManifest`** (SFR-10/22; §12). Tampoco depende de la Managed Source. |
| ¿Candidate puede convertirse en activo sin aprobación? | No (SFR-08). |
| ¿El Candidate puede generar la evidencia esperada contra la que él mismo se aprueba? | No (SFR-15): la expectativa es `SourceSnapshotEvidence` (PRE/POST) de la Managed Source; la auto-medición del Candidate nunca es autoridad. |
| ¿Un `state/active.json` reescrito basta para declarar promoción exitosa? | No (SFR-16): se exige coherencia desired/effective probada; un split-brain es `PENDING`/`FAILED`, jamás `SUCCESS`. |
| ¿Una Generation `DRIFTED` puede reactivarse en silencio? | No (SFR-17): re-verificación de identidad obligatoria antes de rollback/reactivación; si no verifica, falla cerrado. |
| ¿Una Generation puede compartir hardlinks/links con la Managed Source y ser mutada indirectamente por Steam? | No (SFR-18): links rechazados + independencia física verificada por inodo en la creación; la copia no crea hardlinks. |
| ¿Duplicamos RV-1/RV-2/RV-3? | RV-1 se reusa tal cual; RV-2 y RV-3 se reusan parcialmente por su contrato de autoridad de fuente (no por duplicación). |
| ¿Arrastramos GP2 por costumbre? | No: §16 excluye toda dependencia GP2. |
| ¿MO2/SKSE pueden quedar apuntando a una generación borrada? | No: MVP no borra generaciones. |
| ¿Una actualización parcial de Steam puede ser capturada como candidate válido? | Gate de estabilización (§18) + verificación de identidad completa; si no se demuestra, se bloquea. |
| ¿Cómo sabemos que Steam terminó de actualizar? | §18; gate explícito P1, no demostrado en P0. |
| (P0.4) ¿Ejecutar el juego puede hacer derivar la referencia que necesito para el rollback? | No: la Generation no se ejecuta (SFR-19) y lo que se juega es el Runtime Clone (§7). La contradicción original entre SFR-03 y SFR-17 desaparece. |
| (P0.4) ¿Una Generation `DRIFTED` puede producir un Clone? | No: `verificar_generation` la marca `DRIFTED` y, aun con un descriptor viejo, la re-validación PRE-copia de `create_runtime_clone` compara el digest fresco contra `golden_desc.tree_digest` y falla (verificado en código). |
| (P0.4) ¿Un Runtime Clone retenido puede convertirse en la única autoridad de rollback? | No: la autoridad es la Generation (§12). El Clone retenido es vía rápida opcional, y sólo si su Generation de origen sigue `VALID`. |
| (P0.4) ¿El usuario debe repetir una operación manual (solo-lectura del manifest) al iniciar el juego? | No (SFR-21): Sky-Claw no toca el manifest (SFR-11). Es un **criterio de aceptación de P7**, no una propiedad demostrada todavía. |
| (P0.4) ¿Un Clone recién clonado ya es activable como Effective Runtime? | **No**: le falta el setup operativo (SKSE y compañía), que se instala **dentro del game dir** —o sea, dentro del Clone—. Por eso el rollback **provisiona** desde el `RuntimeSetupManifest` (SFR-22; §31.2). Eso **no** afirma que el árbol no pueda ejecutar el juego: `FRESH_CLONE_READY_FOR_ACTIVATION = NO`, no "no arranca". |
| (P0.4) ¿El `ApprovalScope` exige un Candidate también en rollback? | **No**: es operation-aware. `candidate_id` es REQUIRED en `PROMOTION` y NOT_APPLICABLE en `ROLLBACK`, donde alcanzan Generation + Clone + RuntimeSetup + evidencia de compatibilidad (§31.4). |
| (P0.4) ¿El diseño puede prometer rollback operativo si los artefactos del setup dependen de una URL externa? | **No sin declararlo**: `RUNTIME_SETUP_ARTIFACT_AVAILABILITY = OPEN`. El manifest declara procedencia, no disponibilidad futura; la preferencia es no depender en silencio de terceros (§31.5). |
| (P0.4) ¿`desired_active_generation` alcanza para saber qué ejecuta MO2? | **No**: una Generation admite varios Clones (`G1 → C1, C2, C3`). El estado v2 registra el **par** Generation+Clone (SFR-16; §30.3). |
| (P0.4) ¿Ejecutar una Generation a mano la vuelve `DRIFTED`? | **No necesariamente**: ejecutar y modificar son hechos distintos. Ejecutarla fuera de los flujos de Sky-Claw es una **violación de política** (que Sky-Claw no puede impedir); `DRIFTED` requiere que el árbol haya cambiado (SFR-17/19; §30.5). |
| (P0.4) ¿Preparar artefactos sin aprobación es una puerta trasera a la auto-promoción? | **No**: preparar no muta el Effective Runtime. La aprobación se pide en el **binding** y se re-verifica el `ApprovalScope` (SFR-23; §30.6). |
| (P0.4) ¿La aprobación del propietario puede aplicarse a algo distinto de lo que aprobó? | **No**: antes de mutar se re-verifica exactamente el conjunto aprobado; si cambió entre la aprobación y el binding, **no se activa** y se vuelve a pedir aprobación (§11 regla 4). |

## 28. Referencias

- `sky_claw/local/runtime_vault/inventory.py`, `verification.py`, `models.py` (RV-1).
- `sky_claw/local/runtime_vault/golden.py` (RV-2).
- `sky_claw/local/runtime_vault/clone.py` (RV-3).
- `sky_claw/local/runtime_vault/runtime_observation.py` (observación fresca).
- `sky_claw/local/discovery/scanner.py` (detección Skyrim, PE ProductVersion, SKSE).
- `sky_claw/config.py` (`SKYRIM_SE_APPID`, `STEAM_DEFAULT_PATHS`, `runtime_state_dir`).
- `sky_claw/app/core/path_resolver.py` (`SKYRIM_PATH`, notas sobre `gamePath` de MO2).
- ADR 0007 (MO2 broker USVFS), ADR 0009/0010 (Runtime Vault GP1/GP2, contexto).
- `docs/audits/2026-08-22_runtime_vault_mo2_stock_launch_audit.md` (evidencia del rig).
- Issue #494 (Steam-managed mirror / detached pinned runtime), #624 (Golden Admission).
- GP2-S4E archivado: PR #669, issue #671 (contexto histórico; no dependencia).
- Enmienda P0.4 (§29): tracker #672; `sky_claw/local/runtime_vault/`
  (`golden.py`, `clone.py`, `locking.py`, `models.py`); `sky_claw/local/frozen_runtime/`
  (`provider_signals.py`, `generations.py`, `storage.py`, `storage_models.py`,
  `candidates.py`, `copying.py`, `independence.py`); `sky_claw/local/tools_installer.py`;
  `sky_claw/local/tools/output_targets.py`; `sky_claw/local/tools/grass_cache_runner.py`;
  `sky_claw/app_context.py`; `sky_claw/local/auto_detect.py`;
  `sky_claw/app/gui/_bootloader.py`; `sky_claw/local/discovery/scanner.py`;
  `sky_claw/app/core/path_resolver.py`; `sky_claw/local/mo2/vfs.py`; ADR 0007.
- Enmienda P0.4, ronda 2 (§30): revisión del Tech Lead sobre `f46853b5` (PR #701).
  Fundamenta F1 en `tools_installer.py::_copy_skse_files` /
  `_cleanup_orphaned_skse_dlls` (el setup se instala dentro del game dir ⇒ dentro del
  Clone). No agrega evidencia de código nueva.

---

## 29. Enmienda P0.4 — adjudicación: Generation como referencia, Runtime Clone como Effective Runtime (2026-10-07)

**Estado:** Propuesta, igual que el resto del ADR (design/contract only; sin código de
producción). **Verificada sobre `origin/main` `5039997a5d5b4290a8957ab8b7d02ddaa0f1f224`**
(2026-10-07), el merge de #698 — no sobre `97dcc7ab`, que es la base de la propuesta
previa. No reabre P1–P3: lo mergeado sigue vigente tal cual.

**Origen:** la propuesta previa (rama `claude/zen-davinci-3yuv0u`, commit
`8b58e6a3fa1358fbb2f7f564e9f124cea5576537`) introdujo la separación
referencia/copia. Esta enmienda la **reconcilia** en el cuerpo del ADR en vez de
anexarle punteros, y adjudica cada decisión contra el código vivo. Se trata la
propuesta previa como **design input, no autoridad**: varias de sus afirmaciones no
sobrevivieron a la verificación (§29.9).

**En una línea:** la versión que Steam actualiza, la referencia sellada y la copia que
se juega dejan de ser la misma carpeta; la referencia **no se ejecuta**, y por eso el
rollback deja de depender de una copia que derivó por uso normal.

### 29.1 Hipótesis y veredicto

```text
GENERATION_IS_REFERENCE          = YES   (SFR-19)
GENERATION_EXECUTED              = NO    (SFR-19)
RUNTIME_CLONE_IS_PLAYABLE        = YES   (SFR-20)
RUNTIME_CLONE_IS_EFFECTIVE       = YES   (SFR-20)
GENERATION_IS_VERSION_AUTHORITY  = YES   (§12)
RUNTIME_SETUP_AUTHORITY          = RuntimeSetupManifest   (SFR-22)
FRESH_CLONE_READY_FOR_ACTIVATION = NO    (SFR-22; §31.2 — puede ejecutar, no está
                                         listo para ser nuestro Effective Runtime)
RUNTIME_SETUP_ARTIFACT_AVAILABILITY = OPEN   (R3-B1; §31.5)
RUNTIME_CLONE_RECORD_INTEGRITY   = OPEN   (R4-F7; §32)
RETAINED_CLONE_FAST_ROLLBACK     = OPTIONAL / NOT_AUTHORITY   (§12)
CLONE_INSIDE_MANAGED_SOURCE      = NOT_ADMITTED   (R4-F6; §29.3)
RETAINED_CLONE_SETUP_MISMATCH    = FAIL_CLOSED   (R4-F4; §12)
APPROVAL_BINDS_TO                = ApprovalScope (identidad **y contenido**),
                                   no a "quiero actualizar"   (SFR-23; R4-F5)
ROLLBACK_DURABLE_INTENT          = BEFORE_BIND   (R4-F3; §12)
ROLLBACK_TARGET                  = DURABLE / NEVER_GUESSED   (R4-F10; §19a)
PREPARATION_REQUIRES_APPROVAL    = NO    (SFR-23)
AUTO_PROMOTION                   = NO
```

> **Corrección de la ronda 2 (§30).** Este bloque decía
> `GENERATION_IS_ROLLBACK_AUTHORITY = YES`, lo que resultó incompleto: la Generation
> es la autoridad de **versión**, pero **no** alcanza para reconstruir el runtime
> operativo (SKSE, root files). La autoridad de setup es el `RuntimeSetupManifest`
> (SFR-22). El veredicto de esta sección se lee con esa corrección.

La hipótesis ("la Generation no debería ejecutarse porque ejecutar escribe el árbol")
**queda confirmada**, pero no por el argumento de estilo con que la propuesta la
presentaba, sino por una contradicción interna que el ADR original no podía resolver:
SFR-03 y §7 hacían que MO2/SKSE ejecutaran la Generation, mientras SFR-17/§12/F10
vedaban reactivar una Generation `DRIFTED`. Bajo ese diseño el uso normal **podía
escribir** en la referencia (el juego escribe logs, SKSE, ENB, Creation Club) y
cualquier escritura la habría derivado: el rollback quedaba bloqueado justo por el
uso normal. **Ejecutar no deriva *necesariamente*** —una ejecución sin modificación
no vuelve `DRIFTED` a la Generation (§30.5: ejecución = violación de política,
modificación = `DRIFTED`)—, pero un diseño que *deja* que las escrituras aterricen
sobre la referencia no puede prometer que no la deriven. Separar referencia de copia
operativa elimina la contradicción en lugar de administrarla con una política de
"deriva benigna" (que sería un catálogo abierto y frágil).

**Lo que NO cambia:** P1–P3 (código y contratos), SFR-01..18 salvo la lectura de
SFR-03/16/17, la exclusión de GP2 (§16), la ausencia de ACL mutation y de
auto-promoción, y la prohibición de tocar el estado interno de Steam (SFR-11).

### 29.2 Evidencia verificada (código, no intención)

| # | Afirmación | Dónde se verifica | Resultado |
|---|---|---|---|
| E1 | Ejecutar **puede** escribir el árbol ejecutado (no: "ejecutar siempre lo modifica") | audit del rig §7/§9 (`GOLDEN_EXECUTION_CAN_CAUSE_WRITES = PROVEN`) + §29.4 (escritores del game dir) | **CONFIRMADO** |
| E2 | `verificar_generation` no tolera deriva | `frozen_runtime/generations.py::verificar_generation` compara `TreeDigest` completo + críticos + identidad fresca + integridad física | **CONFIRMADO** |
| E3 | `create_runtime_clone` exige una fuente `VERIFIED` con rol `reference_only` | `runtime_vault/clone.py:210-228` | **CONFIRMADO** |
| E4 | `verify_golden_master` **produce** ese descriptor | `runtime_vault/golden.py:257-262` (`role="reference_only"`) | **CONFIRMADO** |
| E5 | Clonar desde una Generation derivada falla por sí solo | `clone.py:289-294`: `src_digest != golden_desc.tree_digest` ⇒ `RuntimeCloneError`; y `clone.py:391-396` re-verifica el origen POST | **CONFIRMADO** |
| E6 | El motor de RV-3 da copia real, staging hermano, publish no-clobber e independencia física | `clone.py:300-358` + `verify_physical_independence` (`st_dev`/`st_ino`) | **CONFIRMADO** |
| E7 | `destination_lock` no sirve como lock de P4 | `locking.py:70` (clave = ruta destino) y `:85` (`tempfile.gettempdir()`) | **CONFIRMADO** |
| E8 | `verify_golden_master` **no** verifica integridad física (`st_nlink==1`) | `golden.py` no la consulta; vive en `independence.py::verify_generation_physical_integrity` | **CONFIRMADO** |
| E9 | `frozen_runtime` es 100 % síncrono, sin cancelación ni progreso | `candidates.py::crear_candidate` es `def` y usa `time.sleep` | **CONFIRMADO** |
| E10 | No existe namespace `clones/` | `storage.py:39-41`: sólo `versions/`, `candidates/`, `state/` | **CONFIRMADO** |
| E11 | El catálogo de críticos tiene una sola entrada | `frozen_runtime/models.py:22`: `CRITICAL_EXE_BY_GAME = {"skyrimse": "SkyrimSE.exe"}` | **CONFIRMADO** |
| E12 | `StateFlags != "4"` ⇒ `ACTIVE`; `6 & 4 == 4` y `518 & 4 == 4` | `provider_signals.py:37,66` + aritmética + `tests/test_frozen_runtime_p1.py` (`MANIFEST_UPDATE_ACTIVE`, test S06 ⇒ `UNSTABLE`) | **CONFIRMADO (observación)**; la semántica de los bits es **UNVERIFIED** |
| E13 | El repo trata las señales del proveedor como **advisory** | docstring de `provider_signals.py`: "Heurísticas documentadas (advisory; no es un contrato de Valve)" | **CONFIRMADO** |
| E14 | La quiescencia de RV no es reutilizable | `runtime_vault/quiescence.py`: primitiva de ADR 0010/GP2, "reservada para el helper privilegiado"; importa `golden_protection_plan` ⇒ violaría §16 | **CONFIRMADO** |

### 29.3 Gate de activación del Runtime Clone (promoción y rollback; todo fail-closed)

0. **Admisión por identidad lógica + exclusión física** (§30.5; R4-F6): el target
   operativo debe tener un `RuntimeCloneRecord` con `admitted_role == "runtime_clone"`,
   linaje válido (`source_generation_id` verificable y `VALID`), `root_path`
   coincidente con el árbol observado, y `lifecycle == PROVISIONED`. **No** se admite
   por prefijo de ruta: la ubicación física del Clone sigue abierta (Q18), así que el
   gate no puede **exigir** un prefijo fijo. Pero la identidad lógica es **necesaria
   y no suficiente**: también se exige **no contención y no solapamiento físico con
   la Managed Source** — el Clone no puede vivir dentro de `steamapps/common/<juego>`
   ni ser ancestro/descendiente suyo. Un directorio copiado bajo `steamapps/common`
   tiene inodos propios y pasaría las demás comprobaciones, pero Steam lo
   administraría, sobrescribiría o borraría igual (SFR-02/04; §20). La regla es
   **exclusión de namespaces peligrosos**, no prefijo fijo.
1. **Identidad de runtime fresca** (`observe_runtime_identity_from_root`) igual a la
   registrada en la metadata de la Generation de origen.
2. **Archivos críticos** iguales a la evidencia crítica registrada en la metadata de
   la Generation (hoy sólo `SkyrimSE.exe`; catálogo en Q19). Las expectativas se
   **pasan explícitamente** desde la metadata: nunca quedan en el default `()` de
   RV-2/RV-3, que desactiva el chequeo en silencio (§29.10-20).
3. **Independencia física** respecto de la Generation **y** de la Managed Source
   (`verify_physical_independence` de RV-3 y/o `verify_generation_independence`),
   sin objetos de filesystem compartidos. Nota: `verify_physical_independence`
   compara **inodos** (detecta hardlinks); la **contención/solapamiento** de rutas
   contra la Managed Source es un chequeo **distinto** y adicional (paso 0), que hoy
   no existe.
4. **RuntimeSetup aplicado y verificado** (SFR-22; R4-F9): el `RuntimeCloneRecord`
   debe estar en `lifecycle == PROVISIONED` con `verified_runtime_setup_id != null`
   (§19c), y sus componentes/hashes declarados coinciden con lo observado. Un Clone
   **sin setup provisionado** —o cuyo provisionamiento quedó incompleto por un
   corte— **no está listo para activación** y no se activa (§31.2). La mera presencia
   de un `intended_runtime_setup_id` **no** implica setup aplicado. Esto es un juicio
   sobre la **activación**, no sobre la capacidad del árbol de ejecutar el juego.
5. **Quiescencia**: ningún proceso ejecuta desde el Clone entrante ni el saliente, y
   MO2 está cerrado durante el binding (`P5_QUIESCENCE`, §29.8).

**La activación ocurre sólo tras la aprobación** (SFR-23): el gate se corre en la
**preparación** (que no muta nada activo), y antes de mutar el Effective Runtime se
**re-verifica exactamente el `ApprovalScope`** aprobado (§11 regla 4; §30.6).

**No se exige igualdad de `TreeDigest` con la Generation**: el Clone **debe** derivar
(el juego escribe logs, SKSE, ENB, Creation Club). La deriva se **reporta**
(archivos agregados / cambiados / quitados, agrupados para diagnóstico). Un archivo
**crítico** alterado **bloquea**, y el propietario puede re-baselinar con aprobación
explícita (Q19).

**Por qué el gate no usa `verify_golden_master` sobre el Clone:** ese contrato exige
`expected == observed` del árbol completo, que un Clone jugado nunca cumple por
diseño. Usarlo para *admitir* el Clone sería el error simétrico al de admitir la
Managed Source.

### 29.4 Superficies del game path y unidad de binding (3 clases)

**La unidad de binding de P5 no son "6 superficies" sino tres clases** (R4-F2). El
censo original contaba sólo las superficies que **determinan** la Effective Runtime
al resolverla; omitía a los consumidores que **cachean** el game path en memoria
dentro de un proceso vivo. Un binder que repunte las 6 y no invalide los caches
deja escrituras aterrizando en el runtime saliente.

**Clase 1 — configuradas/persistidas (2):**

| # | Superficie | Clase | Dónde |
|---|---|---|---|
| 1 | `skyrim_path` persistido (zero-config) | persistida | `local/auto_detect.py::AutoDetector._find_skyrim_inner`, `app_context.py::start_full` (`escribir_campo(local_cfg, "skyrim_path", …)`) |
| 5 | `gamePath` de MO2 | persistida | `ModOrganizer.ini` como `@ByteArray` de Qt; `path_resolver.py` documenta explícitamente que **no** lo decodifica |

**Clase 2 — derivadas (4):**

| # | Superficie | Clase | Dónde |
|---|---|---|---|
| 2 | `SKYRIM_PATH` en el entorno | derivada | `app/gui/_bootloader.py::_hydrate_tool_env_from_snapshot` (`os.environ.setdefault`, un valor del operador gana); `path_resolver.py::get_skyrim_path` lo lee |
| 3 | Raíces del sandbox de `PathValidator` | derivada | `app_context.py::_construir_raices_sandbox` |
| 4 | Snapshot del `EnvironmentScanner` y su paridad con `AutoDetector` | derivada | `local/discovery/scanner.py` (la ruta configurada gana si contiene el ejecutable) + `tests/test_paridad_deteccion_skyrim.py` |
| 6 | Ejecutables de MO2 con ruta absoluta y `moshortcut://SKSE` | derivada | `local/mo2/vfs.py::MO2Controller.launch_game` (`moshortcut://SKSE`) |

**Clase 3 — consumidores con game path cacheado en memoria (verificado, R4-F2):**

| Consumidor | Qué cachea | Dónde | Nota |
|---|---|---|---|
| `GrassCacheService._ensure_runtime_deps` | `deps.game_path` → `self._game_path` | `local/tools/grass_cache_service.py:277-299` | **Sticky**: un early-return (`if self._profile_manager is not None: return`) hace que, una vez poblado, **nunca** se re-resuelva. Si el grass corrió antes de una promoción in-process, sigue escribiendo `PrecacheGrass.txt` en el runtime saliente (`grass_cache_runner.py:56,212,249,700`). |
| `VfsHealthValidator` | `game_path` capturado en el constructor | `local/validators/vfs_health.py:94` | Instancia con path fijo: stale si sobrevive a una promoción. |
| `XEditRunner` | `game_path` capturado en el constructor | `local/xedit/runner.py:717` | Ídem. |

El proveedor `GrassRuntimeDepsProvider` **sí** re-resuelve (`get_skyrim_path()` por
llamada) y memoiza sólo el `MO2Controller`; la obsolescencia está en el **cache del
servicio**, no en el proveedor.

```text
CONFIGURED_GAME_PATH_SURFACES = 6    (2 persistidas + 4 derivadas)
CACHED_RUNTIME_CONSUMERS      = 3    (1 sticky + 2 capturadas en construcción)
P5_BINDING_UNIT               = configuradas/persistidas + derivadas + cacheadas en memoria
P5_BINDING_SCOPE_VERIFIED     = NO   (el censo está verificado; el binding no existe)
```

**No se congela un `COUNT` total como "7"**: la clase 3 es una **familia** que debe
descubrirse por enumeración (introspección/AST), no contarse a mano. El ancla de P5
debe detectar consumidores **nuevos**, y un consumidor nuevo debe romper el ancla
hasta ser cableado — el patrón que `AGENTS.md` exige (familia enumerada, no
muestreada).

**Escritores en el árbol del juego — la propuesta previa los subestimó.** Afirmaba
que "el único verificado hoy es `ensure_skse`". Es **falso**; hay al menos:

| Escritor | Qué escribe | Dónde |
|---|---|---|
| `tools_installer.ensure_skse` | SKSE (loader + DLLs) en el game dir | `tools_installer.py::_copy_skse_files`, `_cleanup_orphaned_skse_dlls` |
| `grass_cache_runner` | `PrecacheGrass.txt` **en el root del juego** (`config.game_path / "PrecacheGrass.txt"`, `write_text`/`unlink`) | `local/tools/grass_cache_runner.py:56,212,249,700` |
| Wrye Bash | `Data/Bashed Patch, 0.ESP` (corre con `cwd=game_path` y sin ruta de salida en el comando) | `local/tools/output_targets.py::bashed_patch_target` |
| Pandora | `<game>/Pandora_Output` | `output_targets.py::pandora_output_target` |
| BodySlide | `<game>/BodySlide_Output/<grupo>` | `output_targets.py::bodyslide_output_root` |
| DynDOLOD legacy | `<game>/Sky-Claw/DynDOLOD` — **sólo recovery**; ningún productor nuevo lo usa | `output_targets.py::dyndolod_legacy_recovery_target` |

La familia de destinos ya tiene un ancla enumerativa
(`tests/test_output_targets.py::test_la_enumeracion_cubre_toda_la_familia`); **P5 debe
extenderla** a "toda superficie que resuelva o escriba el game path", con igualdad
literal, y **P4/P5 deben garantizar que todos apunten al Clone** — nunca a la
Generation (SFR-19).

> **Nota de alcance:** ADR 0011 §1.1 todavía describe `<game>/Sky-Claw/DynDOLOD` como
> la salida **actual** y cita el símbolo `output_targets.dyndolod_output_target`, que
> ya no existe (renombrado a `dyndolod_legacy_recovery_target`). El código y los tests
> ya lo tratan como legacy de sólo-recovery; el texto del ADR 0011 quedó atrás.
> `DOCUMENTATION_DRIFT = POSSIBLE`, `OUT_OF_SCOPE = YES` para este PR: se registra
> aquí y se corrige aparte.

### 29.5 Slice P3b — captura con update pendiente (diseño listo; implementación NO)

**Observación (verificada).** `evaluate_provider_observation` compara `StateFlags`
contra el literal `"4"`. Con `"6"` y con `"518"` el resultado es `ACTIVE` (ambos
tienen el bit `4` encendido), así que la estabilidad da `UNSTABLE`/`INDETERMINATE` y
la versión **actual** no puede sellarse una vez que Steam la marcó para actualizar.
`tests/test_frozen_runtime_p1.py` congela ese comportamiento (S06).

**Conclusión semántica (NO verificada).** Que `6` signifique "update pendiente y
árbol quieto" y `518` "update pendiente" **no está demostrado**. El propio módulo
declara sus heurísticas como advisory y no como contrato de Valve. Por lo tanto:

```text
STATEFLAGS_6_SEMANTICS   = UNVERIFIED
STATEFLAGS_518_SEMANTICS = UNVERIFIED
```

**Regla de diseño (adoptada):** separar **observación** de **conclusión semántica**.
El árbol y las observaciones físicas tienen más autoridad que una interpretación no
documentada de un bitfield. **Prohibido** hardcodear `6`/`518` como "capturable".

**La pregunta correcta** no es "¿qué número de `StateFlags` corresponde a update
pendiente?" sino "¿podemos demostrar que el árbol está quieto aunque Steam tenga una
actualización anunciada?". El contrato de §18 ya tiene los instrumentos:
`PRE_TREE == CANDIDATE_TREE == POST_TREE`, `PRE_RUNTIME == POST_RUNTIME`, ausencia de
`steamapps/downloading/<appid>` y de staging temporal, y `inventory_tree` sellado que
falla cerrado ante mutación concurrente. P3b es, entonces, una **captura autorizada y
fuertemente verificada** cuando las señales de actividad **no** están presentes pero
`StateFlags` no es `idle` — con confirmación explícita del operador y `provider_state`
marcado en la metadata. Alternativa registrada (no excluyente): **adoptar** un runtime
existente verificado contra evidencia independiente.

**Invariantes que P3b no puede romper:** `MANAGED_SOURCE_WRITES = NO` (SFR-11);
`INDETERMINATE != STABLE` por defecto (el override es explícito, auditado y por
captura, nunca un flag global); sin `PRE == Candidate == POST` no hay `READY`
(SFR-15); `Steam` nunca se modifica ni se engaña.

**Orden operativo (crítico para el producto):** sellar **antes** de que Steam
actualice. Después, la referencia de la versión vieja pierde su fuente en Steam.

```text
P3B_REQUIRED_BEFORE_P4 = DEFERRED_PENDING_RIG
P3B_DESIGN_READY       = YES
P3B_IMPLEMENTATION     = NO
```

**Lectura de `DEFERRED_PENDING_RIG`:** la *necesidad* está demostrada (con
`StateFlags != 4` la captura se bloquea hoy, y tras la actualización de Steam la
versión vieja es irrecuperable desde la fuente); lo que **no** se puede fijar sin rig
es (i) qué significan realmente `6`/`518` y (ii) si una captura autorizada en ese
estado es segura. Por eso el **diseño** queda congelado acá y la **decisión del gate**
se difiere a evidencia de rig. Consecuencia para el roadmap: P4 puede **diseñarse** en
paralelo, pero **no** puede darse por implementable ni validarse end-to-end en el rig
real hasta que P3b exista, porque en la máquina del propietario la fuente estará
marcada para actualizar.

### 29.6 Layout (enmienda de §20)

Ver §20: se agregan `clones/<clone-id>/`, `state/clones/<clone-id>.json` y
`state/runtime_setups/<runtime-setup-id>.json`. El **contrato** del esquema v2 de
`active.json`, de la metadata del Clone y del manifest de setup se congela acá
(§19); su **implementación** y la migración v1 → v2 son de P4. Invariante: el
Desired apunta a un **par** (Generation, Clone) y el linaje es un dato registrado,
no inferido; v1 sigue leyéndose fail-closed.

### 29.7 Regla de arranque y SFR-21

- **R-ARRANQUE (hipótesis operativa; validar en P7):** el juego se arranca por MO2
  (loader de SKSE o `SkyrimSE.exe`) desde el Runtime Clone, nunca por el Play de Steam
  ni por `SkyrimSELauncher.exe`. Evidencia parcial: el audit §3 registró
  `steam://run/489830//` en la corrida con launcher; la corrida con `SkyrimSE.exe`
  directo arrancó desde el árbol externo. La causalidad del prompt de Steam **no está
  probada** y el audit no cubre el loader de SKSE.
- **Sky-Claw no toca el manifest** (SFR-11): no fija ni limpia solo-lectura. Sí puede
  **observarlo** (lectura) e informarlo en P6.
- **SFR-21** se formaliza como criterio de aceptación de P7: arrancar N veces por MO2
  con la Managed Source en actualización pendiente y luego ya actualizada, **sin
  ninguna acción manual** sobre Steam entre arranques. **No está demostrado.**

### 29.8 Requisitos registrados (el diseño se hace en cada slice)

| Blocker | Slice | Requisito |
|---|---|---|
| `P4_CROSS_PROCESS_LOCK` (ampliado) | P4 | Una clave **por `FrozenRuntimeRoot`**, no por destino; base del lockfile derivada del root (p. ej. dentro de `state/`), no de `tempfile.gettempdir()`. `destination_lock` de RV-3 **no sirve** (E7). Los instaladores toman un lock por `game_dir`, cuya clave **cambia** al repuntar la ruta: la promoción debe tomar ambas o usar una clave lógica. Participantes congelados por introspección/AST. |
| `P4_LONG_RUNNING_CANCELLATION` (nuevo) | P4 | `frozen_runtime` es 100 % síncrono (E9), sin cancelación ni progreso. `crear_candidate` hace varios recorridos SHA-256 completos + copia con `fsync`; instanciar un Clone con RV-2/RV-3 suma otros. Requisito: fachada async con executor dedicado, token de cancelación cooperativo, progreso hacia el event loop, single-flight por root, y cancelación que termina en un **resultado de operación** explícito (`SUCCESS`/`CANCELLED`/`FAILED`), nunca en un `BUILDING` huérfano. `CANCELLED` **no** es un estado del ciclo de vida del Clone (D0-R2.6; §36.7): la FSM del Clone sigue siendo `CREATED \| PROVISIONING \| PROVISIONED \| INVALID`. **No se refactoriza a asyncio en P0.4.** |
| `P4_DURABLE_TRANSITION` (ampliado) | P4 | La intención durable cubre la secuencia completa: publicar Generation, instanciar Clone y binding, **y también el rollback** (R4-F3): promoción y rollback usan el **mismo modelo**. El **ordering es normativo**: (i) la transición `PENDING` se persiste **antes** de mutar el Effective Runtime; y (ii) se **finaliza SÓLO después** de que el POST-verify pase (R4.1-F1) — invariante `FINALIZED ⇒ POST verification already passed`; si el POST falla, la transición **sigue `PENDING`** (recuperable). Formato del registro = P4; ordering = congelado. |
| `P4_APPROVAL_SCOPE` (ampliado) | P4 | **DESIGN = CLOSED / IMPLEMENTATION = OPEN.** El contrato está decidido (§11: `ApprovalScope` operation-aware —`operation`, `source_activation`, `source_activation_digest`, `generation_id`, `clone_id`, `clone_evidence`, `runtime_setup_id`, `runtime_setup_evidence`, `compatibility_evidence_id`, `candidate_id`, `approval_id`— con `candidate_id` REQUIRED sólo en `PROMOTION` y NOT_APPLICABLE en `ROLLBACK`); el **mecanismo** no existe. **El scope liga el SOURCE que se reemplaza, no sólo el target** (D0-R1; §35.2): sin él, una aprobación obtenida en otro contexto pasaría con el target intacto. **Y liga evidencia de CONTENIDO, no sólo IDs** (R4-F5): un Clone es mutable, así que la comparación pre-bind debe detectar cambios de payload con IDs estables. Falta implementar: representación de `clone_evidence`/`runtime_setup_evidence`, la **autoridad durable de consumo** (`approval_id`/nonce, §36.5), expiración y la re-verificación previa al binding. "El propietario" debe definirse: la capa del agente LLM es lock-only y el HITL de la GUI documenta que una solicitud sin pestaña lanzadora queda sin dueño — el lock **no** es autorización humana. |
| `P4_RUNTIME_SETUP_PROVISIONING` (nuevo) | P4 | Implementar SFR-22: registrar el `RuntimeSetupManifest` por versión, provisionar el Clone de forma reproducible (SKSE del build exacto, root files, componentes) y verificar sus hashes declarados. Sin esto el rollback no reconstruye un runtime **listo para activación** (§31.2). Incluye declarar `assumptions` para lo no clasificado (Creation Club, Q22). |
| `P4_RUNTIME_SETUP_ARTIFACT_AVAILABILITY` (nuevo) | P4 / P6 / P7 | R3-B1 (§31.5): adjudicar qué estrategia (A/B/C/D) garantiza que los artefactos declarados por el manifest sigan siendo recuperables, y **demostrar por Generation retenida** qué queda retenido o reproducible. Sin esta adjudicación **no se puede prometer rollback operativo**: el manifest declara procedencia, no disponibilidad futura. `RUNTIME_SETUP_ARTIFACT_AVAILABILITY = OPEN`. |
| `P4_CLONE_ACTIVATION_GATE` (nuevo) | P4 | Implementar §29.3, incluyendo el catálogo de críticos (Q19) y el **reporte** de deriva (no sólo el veredicto). Además: (a) las expectativas críticas deben **pasarse explícitamente** desde la metadata —nunca quedar en el default `()` de RV-2/RV-3, que desactiva el chequeo en silencio (§29.10-20)—; (b) el gate debe rechazar reparse points que **escapen** del Clone, no sólo comparar inodos contra el origen (§29.10-21); (c) **exclusión física de la Managed Source** (R4-F6): la identidad lógica es necesaria y no suficiente, hace falta no-contención/no-solapamiento con `steamapps/common`; (d) exigir `lifecycle == PROVISIONED` (R4-F9), no la mera presencia de un `intended_runtime_setup_id`. |
| `P4_CLONE_PROVISIONING_LIFECYCLE` (nuevo) | P4 | R4-F9 (§19c): implementar la FSM `CREATED \| PROVISIONING \| PROVISIONED \| INVALID` y la distinción `intended_runtime_setup_id` / `verified_runtime_setup_id`. Un corte entre publicar el Clone (RV-3) y provisionar el setup debe ser **descubrible y clasificable** al arranque, nunca leído como setup aplicado. Fail-closed: `PROVISIONED` es requisito de activación. **Fallo de provisionamiento con semántica única** (D0-R2.6; §36.7): `PROVISIONING_FAILURE ⇒ lifecycle = INVALID`, sin activación y con reintento sobre un **Clone nuevo** — no se demostró idempotencia completa del provisionamiento actual (§36.7, contraste con `ensure_skse`). |
| `P4_ROLLBACK_TARGET_HISTORY` (nuevo) | P4 | R4-F10 (§19a): conservar `previous_activation_target` (el target operativo **saliente**) de forma **transaccional**, publicado **sólo** al finalizar una transición exitosa (POST passed ∧ mismo `transition_id`), para que el rollback no **adivine** qué setup correspondía al runtime anterior. Preferencia declarada: **A** (un único target previo) sobre B (ledger de profundidad arbitraria), reutilizando la maquinaria de `P4_DURABLE_TRANSITION`. **Autoridad única (D0-R2.7; §36.8):** el registro de transición finalizado — **no** `active.json`, que deja de llevar el campo para no crear dos autoridades divergentes. |
| `P4_RUNTIME_CLONE_RECORD_INTEGRITY` (nuevo) | P4 | R4-F7: el `RuntimeCloneRecord` es autoridad de admisión (`admitted_role`) y hoy no tiene blocker de integridad propio. Requisitos: identidad no-clobber, schema validado, consistencia `clone_id` ↔ clave, integridad de `source_generation_id`/`runtime_setup_id`/`root_path`, y `admitted_role` no forjable en silencio. **No** se decide HMAC/firma acá. `RUNTIME_CLONE_RECORD_INTEGRITY = OPEN`. |
| `P5_EFFECTIVE_RUNTIME_ORACLE` (ampliado) | P5 | El bridge MO2 no expone hoy ninguna información de ruta (la operación `health` sólo emite `bridge_health`). Una extensión de **sólo lectura** es la candidata; la API de MO2 no está verificada. Sin oráculo, la promoción queda `PENDING` (SFR-16). El plugin no gana operaciones mutantes (ADR 0007). |
| `P5_PATH_SURFACES_UNIT` (nuevo) | P5 | Binder transaccional de las **tres clases** de §29.4 —configuradas/persistidas, derivadas y **cacheadas en memoria**— **más** los escritores del game dir, con rollback, con MO2 cerrado, y ancla enumerativa de igualdad literal. Repuntar sólo las superficies resueltas deja los caches stale (R4-F2). |
| `P5_QUIESCENCE` (nuevo) | P5 | Antes del binding: ningún proceso ejecuta desde el Clone saliente ni entrante, y MO2 está cerrado. El repo sólo trackea los PID que él lanzó; hace falta un sensor a nivel sistema (precedente: `psutil.process_iter`) y el lock de instancia MO2 del broker (ADR 0007). **No reutilizar** `runtime_vault/quiescence.py` (E14). |
| `P6_STEAM_STATUS_NOTICE` (nuevo) | P6 | Un aviso cuando el buildid de Steam difiera del de la referencia; estado informativo del manifest, de sólo lectura. |

### 29.9 Adjudicación de la propuesta previa (`8b58e6a3`)

| # | Decisión de la propuesta | Adjudicación | Evidencia / consecuencia |
|---|---|---|---|
| **D1** | Generation nunca ejecutada | **ACCEPT** | E1/E2 + la contradicción SFR-03↔SFR-17. Debe ser propiedad del mecanismo: gate de activación (§29.3) + ancla enumerativa en P5, no un recordatorio de proceso. |
| **D2** | Runtime Clone como Effective Runtime | **ACCEPT** | Consecuencia directa de D1; E6 la hace materializable. |
| **D3** | Reutilizar RV-2/RV-3 (Generation → Clone) | **ACCEPT_WITH_CHANGES** | E3/E4/E5/E6: los contratos encajan. Cambios: adaptador `FileIdentity` → `CriticalFileExpectation` (E8: `verify_golden_master` no cubre integridad física ⇒ preceder con `verificar_generation`) y E7: `destination_lock` **no** es el lock de P4. |
| **D4** | Rollback **entre Runtime Clones** | **ACCEPT_WITH_CHANGES** | La propuesta dejaba la autoridad en el Clone (mutable). Se adopta `ROLLBACK_AUTHORITY = GENERATION`: el Clone retenido es vía rápida opcional, y sólo con su Generation de origen `VALID` (§12). |
| **D5** | Generation como autoridad de recuperación | **ACCEPT_WITH_CHANGES** (ronda 2) | E5 + `create_runtime_clone` reconstruye sin la Managed Source; el MVP no borra generaciones (SFR-10). **Corrección de §30.2 (F1):** la Generation es la autoridad de **versión**, no del **setup operativo**; reconstruir el runtime jugable exige además provisionar el `RuntimeSetupManifest` (SFR-22). |
| **D6** | P3b antes de P4 | **DEFER** | `P3B_REQUIRED_BEFORE_P4 = DEFERRED_PENDING_RIG` (§29.5). Diseño listo; implementación no autorizada sin evidencia de rig. |
| **D7** | `StateFlags 6` = update pendiente | **UNVERIFIED** | E12/E13: observación confirmada (`ACTIVE`), semántica no. No hardcodear. |
| **D8** | `StateFlags 518` = update pendiente | **UNVERIFIED** | Ídem D7 (`518 & 4 == 4`). |
| **D9** | Seis superficies del game path | **ACCEPT_WITH_CHANGES** | Las 6 existen, pero **no** son la unidad de binding: §29.4 se reformuló en 3 clases (configuradas/persistidas, derivadas y **cacheadas en memoria**, R4-F2). Se **rechaza** la afirmación acompañante de que `ensure_skse` es el único mutador del game dir: hay al menos 5 más, una de ellas (`grass_cache_runner`) con evidencia directa en código. |
| **D10** | SFR-21 sin pasos manuales repetidos | **ACCEPT_WITH_CHANGES** | Se acepta como **requisito de producto** (SFR-21) y criterio de P7; **no** se declara demostrado (§29.7). |
| **D11** | Catálogo de archivos críticos | **ACCEPT_WITH_CHANGES** | E11: hoy sólo `SkyrimSE.exe`. Extender + re-baseline con aprobación explícita (Q19, `P4_CLONE_ACTIVATION_GATE`). |
| **D12** | Quiescencia antes del binding | **ACCEPT** | E14: no hay primitiva reutilizable; el sensor es nuevo y es de P5 (`P5_QUIESCENCE`). |
| **D13** | Cancelación/progreso para operaciones largas | **ACCEPT** | E9: 100 % síncrono. Asignado a P4 (`P4_LONG_RUNNING_CANCELLATION`); **no** se refactoriza acá. |

### 29.10 Revisión adversarial propia

Cada pregunta cierra en un estado explícito. Los `UNKNOWN` no se ocultan.

| # | Pregunta | Estado | Fundamento |
|---|---|---|---|
| 1 | ¿Puede Steam modificar el Runtime Clone? | **ANSWERED** con condición | El layout pone al Clone bajo `<FrozenRuntimeRoot>/clones/` (fuera de `steamapps/common`), pero **la identidad lógica sola no lo garantiza** (R4-F6): un Clone registrado dentro de `steamapps/common` tendría inodos propios y pasaría la independencia por inodo. Por eso el gate exige **exclusión física** de la Managed Source (§29.3 paso 0); sin ese chequeo, la respuesta sería "sí". La admisión de `clones/` es requisito de P4. |
| 2 | ¿Puede jugar Skyrim modificar la Generation? | **ANSWERED** (diseño) / enforcement en P4–P5 | Sólo si algo la ejecuta **y** la modifica. SFR-19 la prohíbe y el gate de activación sólo admite un target con `RuntimeCloneRecord` (`admitted_role == "runtime_clone"`). Las superficies controladas por Sky-Claw la **rechazan**; ejecutarla a mano es una violación de política que Sky-Claw no puede impedir, y la mutación resultante se detecta (`DRIFTED`). **Ejecutar ≠ modificar** (§30.5). |
| 3 | ¿Puede SKSE instalarse accidentalmente en la Generation? | **ANSWERED** | `ensure_skse` escribe en el game dir resuelto: si P5 apunta al Clone, no. Si alguien lo corre contra la Generation, `ensure_skse` **muta** el árbol ⇒ queda `DRIFTED` y deja de ser fuente de clonación (E5). Ejecutar sin escribir no la vuelve `DRIFTED`. Detectado, no prevenido. |
| 4 | ¿Una actualización de Steam cambia el runtime jugable? | **ANSWERED** | Aislamiento físico (§7, SFR-02/04). |
| 5 | ¿Un Candidate puede autoautorizarse? | **ANSWERED** | SFR-15: la evidencia esperada es `SourceSnapshotEvidence` PRE/POST, nunca la auto-medición del Candidate. |
| 6 | ¿Una Generation `DRIFTED` puede generar un Clone? | **ANSWERED** | E2 + E5: doble barrera (verificación previa y re-validación PRE-copia). |
| 7 | ¿Un Clone corrupto puede convertirse en autoridad de rollback? | **ANSWERED** con riesgo residual | Con `ROLLBACK_AUTHORITY = GENERATION`, no es la autoridad. Residual: un Clone retenido corrompido **más allá de los críticos** (una DLL, un mod) puede pasar el gate de §29.3, que sólo mira identidad, críticos e independencia. **DEFERRED_WITH_BLOCKER** (`P4_CLONE_ACTIVATION_GATE`: definir la fuerza del gate). |
| 8 | ¿Se puede reconstruir una versión anterior desde su Generation? | **ANSWERED** | D5/E5: re-clonado desde la Generation `VALID`, sin la Managed Source. |
| 9 | ¿Puede `active.json` declarar éxito cuando MO2 ejecuta otra ruta? | **ANSWERED** (fail-closed) | `active.json` sólo registra Desired; sin oráculo de Effective Runtime (P5) la promoción **no** puede declarar `SUCCESS` (§11 regla 6). |
| 10 | ¿Qué ocurre si P4 muere entre crear el Clone y cambiar el binding? | **DEFERRED_WITH_BLOCKER** | `P4_DURABLE_TRANSITION`: persistir la intención **antes** de mutar el Effective Runtime + reconciliación al arranque. |
| 11 | ¿Qué ocurre si MO2 está abierto? | **DEFERRED_WITH_BLOCKER** | `P5_QUIESCENCE`: MO2 puede reescribir su ini al salir; el binding exige MO2 cerrado. |
| 12 | ¿Qué ocurre si Steam está actualizando **realmente** durante la captura? | **ANSWERED** | `UNSTABLE`: actividad observable (`downloading/<appid>`, staging, `BytesToDownload > BytesDownloaded`) y `inventory_tree` falla cerrado ante mutación concurrente. No hay Candidate. |
| 13 | ¿Qué ocurre si Steam **sólo anuncia** una actualización? | **UNKNOWN** | Hoy se bloquea la captura y no hay forma de distinguir "anunciada" de "en curso" con la evidencia actual. Es exactamente el problema de P3b (`DEFERRED_WITH_BLOCKER`, §29.5). |
| 14 | ¿Qué conocimiento real tenemos sobre `StateFlags`? | **UNVERIFIED** | Sólo: bit `4` encendido en `4`/`6`/`518`, y `"4"` ⇒ idle **por heurística del repo**. Nada sobre semántica de Valve (E13). |
| 15 | ¿El diseño obliga al usuario a repetir pasos manuales? | **ANSWERED** (requisito) / no demostrado | SFR-11 ya prohíbe tocar el manifest, así que el diseño no lo exige; SFR-21 se valida en P7. |
| 16 | ¿Hay objetos de filesystem compartidos (hardlinks/junctions)? | **ANSWERED** (con límite) | `verify_generation_independence` (Generation↔Managed Source), `verify_physical_independence` (Clone↔Generation), `copying.py` copia contenido (nunca `os.link`) y verifica `st_nlink == 1`. **Límite (R4-F6):** todos comparan **inodos**; **ninguno** chequea contención/solapamiento de rutas contra la Managed Source, que es un chequeo distinto y ahora explícito (§29.3 paso 0). |
| 17 | ¿Cuál es la autoridad final de rollback? | **ANSWERED** | **La Generation** (§12). Un Clone retenido no la reemplaza. |
| 18 | ¿Un Clone anterior con DLLs incompatibles o malware accidental puede pasar un gate débil? | **DEFERRED_WITH_BLOCKER** | Sí, si el gate se limita a identidad + críticos + independencia. Mitigación adoptada: la autoridad es la Generation; el gate de compatibilidad (`RUNTIME_COMPATIBILITY`) aplica a la Generation. La **fuerza** del gate de activación se define en P4. |
| 19 | ¿La metadata de la Generation —que ahora es la autoridad de rollback— está protegida contra manipulación? | **UNKNOWN** / **DEFERRED_WITH_BLOCKER** | **Hallazgo nuevo.** `state/generations/<id>.json` es la raíz de confianza (`VALID` = inventario fresco == digest **registrado**) y **no tiene protección de integridad**: no hay firma, HMAC ni digest del propio archivo (verificado: `frozen_runtime/` no contiene ninguna primitiva de ese tipo). P0.4 **eleva la apuesta** al convertir la Generation en autoridad de rollback: quien edite ese JSON y el árbol en consecuencia obtiene `VALID`. Fuera del threat model declarado (§17), pero debe declararse como límite y decidirse en P4. |
| 20 | ¿El gate puede degradarse en silencio por un default? | **ANSWERED** (footgun registrado) | **Hallazgo nuevo.** `critical_expectations` es `()` por defecto **en RV-2 y RV-3** (`golden.py`, `clone.py`) y `all(...)` sobre vacío es `True`: un caller que omita las expectativas de la metadata obtiene `VERIFIED` **sin verificar ningún crítico**. La reutilización de RV-2/RV-3 es tan fuerte como el adaptador que le pasa la metadata; ese adaptador debe ser parte del contrato congelado, no un parámetro opcional. Requisito para `P4_CLONE_ACTIVATION_GATE`. |
| 21 | ¿Un enlace creado **dentro del Clone después de activarlo** puede redirigir escrituras hacia la Generation? | **DEFERRED_WITH_BLOCKER** | `verify_physical_independence` compara los archivos del inventario **del origen**; un directorio nuevo en el Clone apuntando a la Generation no lo activa. SFR-18 se verifica en la creación y en la activación, no de forma continua. Si ocurriera, la Generation derivaría y `verificar_generation` la marcaría `DRIFTED` (detectado, pero ya perdida la referencia). Requisito: el gate de activación debe recorrer el árbol del Clone rechazando reparse points que **escapen** del Clone, no sólo comparar inodos contra el origen. Fuera del threat model declarado (§17); se registra como hardening. |

### 29.11 Presupuesto de complejidad y trade-offs

```text
USES_ACL_MUTATION=NO
USES_PRIVILEGED_HELPER=NO
USES_GP2=NO
AUTO_PROMOTION=NO
DELETES_PREVIOUS_GENERATION=NO
STEAM_MANIFEST_MUTATION=NO
STEAM_UPDATE_BLOCKING=NO
ACL_UPDATE_GUARD=NO
GENERATION_IS_EXECUTED=NO          # nuevo (SFR-19)
CLONE_ENGINE=RV-2+RV-3             # nuevo: sin reimplementar la copia
RUNTIME_SETUP_AUTHORITY=RuntimeSetupManifest   # ronda 2 (SFR-22)
PREPARATION_REQUIRES_APPROVAL=NO               # ronda 2 (SFR-23)
ROLLBACK_DURABLE_INTENT=BEFORE_BIND            # ronda 4 (R4-F3)
APPROVAL_BINDS_TO_CONTENT=YES                  # ronda 4 (R4-F5)
CLONE_INSIDE_MANAGED_SOURCE=NOT_ADMITTED       # ronda 4 (R4-F6)
RUNTIME_CLONE_RECORD_INTEGRITY=OPEN            # ronda 4 (R4-F7)
CLONE_ACTIVATION_REQUIRES_PROVISIONED=YES      # ronda 4 (R4-F9)
ROLLBACK_TARGET=NEVER_GUESSED                  # ronda 4 (R4-F10)
FINALIZE_ONLY_AFTER_POST_VERIFY=YES            # ronda 4.1 (R4.1-F1)
RETAINED_CLONE_REQUIRES_VALID_GENERATION=YES   # ronda 4.1 (R4.1-F2)
```

- **Disco:** ≈2 árboles completos por versión retenida (referencia + copia operativa)
  más la Managed Source. El `RuntimeSetupManifest` **no** duplica el payload
  (declara versiones, hashes y operaciones), así que no suma un tercer árbol. La
  retención/GC queda fuera del MVP (§23; Q20). Prioridad declarada:
  **integridad > velocidad**.
- **Más estados que coordinar** (Desired/Effective ahora sobre el Clone): lo mitigan
  `P4_DURABLE_TRANSITION` y el oráculo de P5.
- **Menos política frágil:** desaparece la necesidad de definir qué deriva es
  "benigna" sobre el `TreeDigest`; la deriva vive en el Clone, donde es esperada.
- **Tiempo de adopción:** la vía canónica suma la creación del Candidate, la
  instanciación del Clone y (en rollback) otro clonado completo. La optimización
  (reusar inventarios sellados sin debilitar la frescura) queda para P4 con números
  del rig.

### 29.12 Límites y no-verificado

**Verificado en esta sesión sobre `5039997a`:** E1–E14 de §29.2, por **lectura de
símbolos y contratos** más **sondas puntuales ejecutadas** (imports reales,
`inspect.signature` de `verify_golden_master`/`create_runtime_clone`, aritmética de
`StateFlags` y evaluación de `all()` sobre un iterable vacío) y la lectura de los tests
citados. **No** se ejecutó ningún prototipo sobre un árbol de juego real ni sobre
NTFS: no hay smoke de clonado en esta sesión. No se agregó ni modificó código de
producción (`PRODUCT_CODE_CHANGED = NO`).

**No verificado (smokes de rig pendientes, P7):** NTFS, junctions, antivirus y `st_ino`
reales; Steam real (semántica de `StateFlags` más allá de la aritmética de bits, y si
el prompt de Steam aparece al arrancar el loader de SKSE desde el Clone con la
actualización pendiente); la API de MO2 para el oráculo y la reescritura de su ini al
salir; tiempos y consumo de disco reales.

---

## 30. Enmienda P0.4 — ronda 2: adjudicación de la revisión del Tech Lead (2026-10-07)

**Estado:** Propuesta, igual que el resto del ADR. Docs-only. **No** implementa P4 ni
P5, **no** reabre P1–P3 y **no** marca el PR como listo para merge.

**Origen:** revisión del Tech Lead sobre `f46853b5` (la ronda 1 de §29). Los **cinco**
findings se adjudican como **CONFIRMED**. Dos (`F1`, `F2`) son **blockers de diseño**
y uno (`F4`) es blocker de diseño sobre SFR-19. Esta ronda **corrige el cuerpo del
ADR** —no le anexa punteros— y deja el registro acá.

### 30.1 Adjudicación

| Finding | Adjudicación | Corrección aplicada |
|---|---|---|
| **F1** — El rollback no reconstruye el runtime **operativo** | **CONFIRMED / BLOCKER** | Se introduce `RuntimeSetupManifest` como **segunda autoridad** (`RUNTIME_SETUP_AUTHORITY`), SFR-22. §12 y F7 reescritos: el rollback **provisiona**, no sólo re-clona. |
| **F2** — SFR-16 con identidad ambigua | **CONFIRMED / BLOCKER** | `active.json` **v2** con `desired_generation_id` **y** `desired_clone_id`; `RuntimeCloneRecord` con `source_generation_id`; SFR-16 reformulada con cuatro condiciones; migración v1 → v2 = P4, fail-closed (§19). |
| **F3** — Terminología residual que ejecuta la Generation | **CONFIRMED** | Corregidos §7, §10, §13, §14-F7, §24-P5, §25-DoD y §27. El DoD ahora dice inequívocamente que MO2/SKSE **nunca** ejecutan una Generation en flujos administrados por Sky-Claw. |
| **F4** — SFR-19 formulada demasiado fuerte | **CONFIRMED / BLOCKER DE DISEÑO** | SFR-19 acotada a las superficies **controladas por Sky-Claw**, con admisión por **identidad lógica** (`RuntimeCloneRecord`), y separación explícita **violación de política ≠ `DRIFTED`**. |
| **F5** — Orden de aprobación inconsistente | **CONFIRMED** | SFR-23: **preparación ≠ activación**; `ApprovalScope` exacto y **re-verificación antes del binding**; cancelar deja el runtime activo intacto. |

**Sin cambios en esta ronda:** P1–P3 (código y contratos), §16 (exclusión de GP2),
SFR-11 (no tocar Steam), `AUTO_PROMOTION = NO`, el censo de superficies de §29.4 y la
adjudicación D1–D13 de §29.9 salvo donde este §30 la corrige explícitamente.

### 30.2 F1 — El rollback reconstruye el **runtime operativo**

La ronda 1 dejó: `Generation G0 → crear Clone nuevo → rollback completo`. Eso
reconstruye el **snapshot base**, no necesariamente el **Skyrim jugable**.

El código lo confirma: `ensure_skse()` instala dentro del `install_dir` del juego
(`tools_installer.py::_copy_skse_files`, `_cleanup_orphaned_skse_dlls`). Por diseño
P0.4 esa escritura ocurre **sobre el Clone**, no sobre la Generation — que es
justamente lo que la separación busca. Consecuencia directa:

```text
Generation G0
    ↓ clone
Runtime Clone limpio
```

puede **no** tener SKSE, archivos de root tipo ENB, ni otros componentes operativos
requeridos. Un "rollback" que termina ahí devuelve un runtime **sin el setup que lo
vuelve nuestro Effective Runtime**: no está listo para activación, aunque el árbol
pueda ejecutar el juego (§31.2 — no se afirma que no arranque).

**Decisión: dos autoridades, no una.**

| Pregunta | Autoridad | Artefacto |
|---|---|---|
| ¿Qué versión de Skyrim es? | **Versión** | `Generation` (`versions/<generation-id>/`) |
| ¿Qué necesita esa versión para ser *nuestro* runtime jugable? | **Setup operativo** | `RuntimeSetupManifest` (`state/runtime_setups/<id>.json`) |
| ¿Cuál es la materialización mutable? | — (derivada de ambas) | `Runtime Clone` (`clones/<clone-id>/`) |

```text
Generation
   │  versión base exacta
   ▼
fresh Runtime Clone
   │  RuntimeSetupManifest
   ▼
provision SKSE / componentes operativos
   │
   ▼
compatibility + activation gate
   │
   ▼
Playable Runtime Clone
```

El manifest **no** guarda necesariamente todos esos archivos duplicados: su contrato
son **versiones, hashes, procedencia y operaciones reproducibles**. Eso conserva la
separación `Generation = qué versión` / `RuntimeSetupManifest = qué necesita esa
versión` / `RuntimeClone = materialización mutable`, que es más sólida que hacer del
Clone mutable la autoridad de rollback.

**Creation Club: sin adjudicar.** Qué ya forma parte del snapshot (y permanece en la
Generation) y qué es estado operativo añadido después (y pertenece al manifest) **no
se decide por intuición en este PR**. Queda como Q22, con `assumptions` obligatorias
en el manifest mientras no esté clasificado.

### 30.3 F2 — SFR-16 necesita **dos** identidades

El estado v1 sólo conoce `desired_active_generation`. Pero P0.4 permite:

```text
G1
 ├── Clone C1
 ├── Clone C2
 └── Clone C3
```

Entonces `desired_generation = G1` **no** identifica cuál debe ejecutar MO2. Y no se
impone `1 Generation = 1 Clone`, porque eso rompería precisamente la capacidad de
recrear un Clone fresco (el rollback canónico crea uno nuevo cada vez, §12).

El contrato v2 (`active.json`, §19):

```text
desired_generation_id = G1
desired_clone_id      = C3
```

y la metadata del Clone (`RuntimeCloneRecord`, §19):

```text
clone_id             = C3
source_generation_id = G1
runtime_setup_id     = S3
root_path            = <ruta física del Clone>
admitted_role        = "runtime_clone"
```

SFR-16 pasa de la ambigüedad `desired == effective` a algo comprobable con
nombres **literales del schema** (ronda 3, R3-F1: sin alias):

```text
active.desired_generation_id == C3.source_generation_id
active.desired_clone_id      == C3.clone_id

effective_path               == C3.root_path
C3.admitted_role             == "runtime_clone"
C3 pasa el gate de activación (§29.3)
```

Sólo entonces `SFR16_COHERENT = YES`. No existe `active.generation_id` ni
`active.clone_id`: el schema v2 tiene **una sola** nomenclatura (§31.1).

**Migración `active.json` v1 → v2 = P4, fail-closed.** Este PR **no la implementa**:
congela el contrato. Un v1 no se reinterpreta como v2 ni se "adivina" el Clone a
partir de la Generation.

### 30.4 F3 — Terminología residual: la Generation no se ejecuta

Contradicciones heredadas del modelo anterior, corregidas en el cuerpo del ADR:

| Lugar | Antes | Ahora |
|---|---|---|
| §7 (diagrama) | un solo bloque "FROZEN RUNTIME" con MO2/SKSE ejecutando dentro, sin distinguir Generation de Clone | Generation marcada `NO se ejecuta (SFR-19)`; el Clone dice `MO2/SKSE/juego ejecutan ACÁ`; el `RuntimeSetupManifest` aparece como autoridad separada |
| §10 | "nunca puede convertirse en la Generation activa" | "nunca puede convertirse en el **runtime activo**" |
| §13 | "desde el root del Frozen Runtime" | "desde el root del **Runtime Clone** (nunca desde una Generation)" |
| §14 (F7) | `activate previous generation` / "Rollback = repuntar" | "re-materializar un Runtime Clone desde la Generation retenida **+ provisionar su RuntimeSetupManifest** + activar" |
| §24 (P5) | "MO2 arranca desde Generation activa" | "MO2 arranca desde el **Runtime Clone**" |
| §25 (DoD 4–5) | "Rollback = repuntar" / "MO2/SKSE arrancan desde el Frozen Runtime" | re-materializar **y provisionar** / "MO2/SKSE **nunca** ejecutan una Generation en ningún flujo administrado por Sky-Claw" |
| §27 | "Rollback … repunta a una Generation retenida" | "re-materializa … **y lo provisiona**" |

Flujo de referencia (el que el DoD hace obligatorio):

```text
select Generation
→ materialize/revalidate Runtime Clone
→ provision operational setup
→ activation gate
→ explicit approval (ApprovalScope)
→ bind Effective Runtime to Clone
```

### 30.5 F4 — SFR-19 acotada: violación de política ≠ deriva

La formulación de la ronda 1 decía, en efecto:

```text
Generation ejecutada por error → deriva → DRIFTED
```

Eso **no** es necesariamente cierto: puede ejecutarse y no producir ninguna
modificación observable. La relación correcta es:

```text
Generation ejecutada  = POLICY VIOLATION
Generation modificada = DRIFTED
```

Son hechos distintos. Y tampoco se puede afirmar que Sky-Claw impedirá que el
propietario ejecute a mano `versions/G1/SkyrimSE.exe` fuera del sistema. Por tanto
SFR-19 queda **acotada**:

```text
Todas las superficies de launch, instalación de herramientas y mutación del game dir
CONTROLADAS POR SKY-CLAW deben rechazar una Generation como destino.
```

Y el rechazo **no** se liga a un prefijo de ruta (`path under <root>/clones/`),
porque Q18 todavía no decidió dónde vivirán físicamente los Clones. Se define una
**identidad lógica registrada**:

```text
RuntimeCloneRecord:
    clone_id
    source_generation_id
    runtime_setup_id
    root_path
    admitted_role = runtime_clone
```

Un target operativo sólo es admisible cuando:

```text
target == registered RuntimeClone root
AND target != Generation
AND linaje válido (source_generation_id registrada y VALID)
AND lifecycle == PROVISIONED
AND NO contención/solapamiento físico con la Managed Source   # R4-F6
AND gate de activación válido
```

Eso permite que mañana el Clone viva en otro SSD sin romper el modelo. Pero la
identidad lógica es **necesaria y no suficiente** (R4-F6): "no exigir un prefijo
fijo" **no** equivale a "cualquier ubicación sirve". Un Clone registrado dentro de
`steamapps/common/<juego>` puede tener inodos independientes y aun así ser
administrado, sobrescrito o borrado por Steam (SFR-02/04). La regla correcta es
**exclusión de namespaces peligrosos**, no prefijo fijo: se rechaza contención en
la Managed Source y conflicto ancestro/descendiente, sin volver a
`must be under <root>/clones/` (Q18 sigue decidiendo la ubicación).

### 30.6 F5 — Preparación ≠ activación; `ApprovalScope`

No hay razón para pedir permiso por **preparar artefactos no activos**. Se permite sin
aprobación:

```text
Candidate READY
   ↓
publish Generation
   ↓
create Runtime Clone
   ↓
provision Runtime Setup
   ↓
compatibility / activation gate
```

todo sin tocar el runtime activo. Después se muestra al propietario **exactamente**
qué se activará:

```text
Generation      G2
Clone           C5
RuntimeSetup    S3
compatibility = COMPATIBLE
```

y ahí:

```text
USER APPROVES
```

La aprobación queda ligada a ese conjunto, no a "quiero actualizar". El scope es
**operation-aware** (R3-F4): `candidate_id` sólo tiene sentido en una promoción
basada en Candidate; un rollback canónico parte de una Generation retenida y
re-verificada (§12) y **no** debe exigir un Candidate histórico.

```text
ApprovalScope:
    operation                     # PROMOTION | ROLLBACK
    source_activation             # runtime SALIENTE (ronda 2, D0-R2.2; §36.3)
    source_activation_digest
    generation_id
    clone_id
    clone_evidence                # contenido del Clone aprobado (ronda 4, R4-F5)
    runtime_setup_id
    runtime_setup_evidence        # digest del manifest aprobado (ronda 4, R4-F5)
    compatibility_evidence_id
    candidate_id                  # REQUIRED en PROMOTION; NOT_APPLICABLE en ROLLBACK
    approval_id                   # identidad durable single-use (ronda 2, D0-R2.4; §36.5)
```

Antes de presentar el scope se exige además **coherencia del source baseline**
(`Desired source == Effective observado == RuntimeCloneRecord registrado`): si
divergen, no hay aprobación nueva (§36.3/§36.4).

Y antes de mutar el Effective Runtime:

```text
reverify exact approved artifacts
+ approval-time evidence == pre-bind freshly recomputed evidence   (R4-F5)
```

Eso **cierra el diseño** de `P4_APPROVAL_SCOPE` y `P4_REVERIFY_AFTER_APPROVAL`;
**la implementación sigue abierta** (`OPEN`) — ver §30.7 (R3-F3). Una cosa es el
contrato decidido y otra el mecanismo de producción: esta ronda fija el primero y
**no** implementa el segundo. Si el propietario cancela:

```text
active runtime = unchanged
```

Los artefactos preparados permanecen **inactivos**; su limpieza pertenece al GC
explícito (§23), no a la promoción.

### 30.7 Blockers vigentes (sin cambios en esta ronda)

Esta ronda **no** cierra los blockers previos; los deja explícitos:

```text
GENERATION_METADATA_INTEGRITY    = OPEN / adjudicate before P4   (§26-21; §29.10-19)
MANDATORY_CRITICAL_EXPECTATIONS  = OPEN / must fail closed       (§26-19; §29.10-20)
POST_ACTIVATION_LINK_INJECTION   = OPEN / activation hardening   (§29.10-21)
RUNTIME_SETUP_MANIFEST_INTEGRITY = OPEN / adjudicate before P4   (Q23, nuevo en esta ronda)
CREATION_CLUB_CLASSIFICATION     = DEFERRED_PENDING_EVIDENCE     (Q22, nuevo en esta ronda)
P3B                              = DEFERRED_PENDING_RIG          (§29.5)
P4_READY_TO_IMPLEMENT            = NO
```

`RUNTIME_SETUP_MANIFEST_INTEGRITY` es la contraparte de `GENERATION_METADATA_INTEGRITY`:
si el manifest pasa a ser la autoridad del setup operativo (SFR-22) y **no** tiene
protección de integridad, el gate de activación hereda esa debilidad.

### 30.8 Estado de los findings

```text
F1_OPERATIONAL_ROLLBACK        = CLOSED
F2_SFR16_IDENTITY              = CLOSED
F3_STALE_GENERATION_EXECUTION  = CLOSED
F4_SFR19_ENFORCEMENT_MODEL     = CLOSED
F5_APPROVAL_ORDER              = CLOSED
```

> **Precisión (ronda 3, R3-F3).** Estos `CLOSED` son de **finding**, no de
> **implementación**: significan que el contrato corrigió lo que el finding
> señalaba. En particular, `F5_APPROVAL_ORDER = CLOSED` **no** implica que
> `P4_APPROVAL_SCOPE` esté implementado. Ver §31.2 para la separación
> `DESIGN = CLOSED` / `IMPLEMENTATION = OPEN`.

**Arquitectura que esta ronda busca congelar:**

```text
Steam Managed Source
        │
        ▼
    Candidate
        │
        ▼
   Generation G
 immutable reference
        │
        ├──────── RuntimeSetupManifest S
        │
        ▼
 Runtime Clone C
 source_generation_id = G
 runtime_setup_id     = S
        │
        ▼
 activation gate
        │
        ▼
 explicit approval (ApprovalScope)
        │
        ▼
 durable transition
        │
        ▼
 MO2 / SKSE / Skyrim
```

Coherencia activa (**nombres literales del schema**; ronda 3, R3-F1):

```text
active.desired_generation_id == C.source_generation_id
active.desired_clone_id      == C.clone_id
effective_path               == C.root_path
C.admitted_role              == "runtime_clone"
C.lifecycle                  == PROVISIONED
C passes activation gate
approved artifact set        == reverified artifact set
approval-time evidence       == pre-bind freshly recomputed evidence   (R4-F5)
transition FINALIZED         ⇒ POST verification already passed          (R4.1-F1)
```

> Nota de la ronda 3 (R3-F1): la versión anterior de este bloque escribía
> `active.generation_id` / `active.clone_id`, que **no existen** en el schema v2
> (§19). Los únicos nombres válidos son `desired_generation_id` y
> `desired_clone_id`; ver §31.1.

**Alcance de la ronda 2:** docs-only. `PRODUCT_CODE_CHANGED = NO`. No se
implementaron SFR-22 ni SFR-23; no se escribió el adaptador de metadata; no se tocó
`active.json`, ni el estado, ni MO2. No se agregó evidencia de código nueva a §29.2:
esta ronda es adjudicación y corrección de contrato sobre la evidencia ya verificada
en la ronda 1, más el razonamiento de `ensure_skse` → `install_dir` (ya citado en
§29.4) que fundamenta F1.

---

## 31. Enmienda P0.4 — ronda 3: residuos contractuales y disponibilidad de artefactos (2026-10-07)

**Estado:** Propuesta. Docs-only. **No** implementa P4 ni P5, **no** reabre P1–P3 y
**no** reabre los findings ya cerrados en la ronda 2 (se corrigen sólo los residuos
derivados que esta ronda identifica).

### 31.1 R3-F1 — Nomenclatura del schema: sin alias

`§19` define el schema v2 con `desired_generation_id` y `desired_clone_id`. La ronda 2
introdujo en §30.3 y §30.8 las formas `active.generation_id` / `active.clone_id`, que
**no existen** en el contrato: un alias silencioso que un implementador podía copiar
tal cual.

**Contrato canónico (nombres literales y únicos):**

```text
active.desired_generation_id == C.source_generation_id
active.desired_clone_id      == C.clone_id
effective_path               == C.root_path
C.admitted_role              == "runtime_clone"
C passes activation gate
approved artifact set        == freshly reverified artifact set
```

**Residuos adjudicados:**

| Ocurrencia | Dónde estaba | Adjudicación |
|---|---|---|
| `active.generation_id` | §30.8 (coherencia activa) | **ELIMINADO** — no existe en el schema |
| `active.clone_id` | §30.8 (coherencia activa) | **ELIMINADO** — no existe en el schema |
| `DesiredGeneration` / `DesiredClone` (sin sufijo) | §30.3 | **NORMALIZADO** a `active.desired_generation_id` / `active.desired_clone_id` |
| `desired == effective == B` | §30.3 (descripción del cambio) | **PERMITIDO sólo como referencia histórica** a la formulación v1, explícitamente marcada como ambigua |
| `desired_active_generation` (v1) | §19 | **VIGENTE sólo para v1**; fuera del schema v2 |

```text
R3_F1_SCHEMA_NAMES_UNIQUE = YES
```

### 31.2 R3-F2 — Ejecutable ≠ activable

La ronda 2 afirmaba `FRESH_CLONE_IS_JUGABLE = NO` y "un Clone recién creado no es
jugable por sí solo". Es **demasiado absoluto**: un Clone base podría ejecutar Skyrim
vanilla aunque todavía **no** esté listo para convertirse en *nuestro* Effective
Runtime aprobado. Sin evidencia específica no corresponde afirmar "no arranca" ni "no
puede ejecutar Skyrim".

Se separan dos hechos distintos:

```text
can execute                                   # ¿el árbol puede lanzar el juego?
ready for activation as Sky-Claw Effective Runtime   # ¿puede ser NUESTRO runtime?
```

**Contrato preferido (reemplaza la formulación absoluta):**

```text
FRESH_CLONE_READY_FOR_ACTIVATION = NO
```

hasta que pasen: `RuntimeSetup` requerido aplicado y verificado, gate de
compatibilidad, gate de activación, verificación de linaje y evidencia crítica.

SFR-22 pasa a decir, en lugar de "no es jugable por sí solo":

> Un Runtime Clone recién creado desde una Generation **no se considera por defecto
> un runtime operativo listo para activación**. Debe aplicarse y verificarse el
> `RuntimeSetup` requerido y superar los gates definidos. Esto **no** afirma que el
> Clone no pueda ejecutar el juego: afirma que no está **listo para activación**
> hasta que el setup esté provisionado y verificado.

```text
R3_F2_EXECUTABLE_VS_ACTIVATABLE_SEPARATED = YES
```

### 31.3 R3-F3 — Diseño cerrado ≠ implementación cerrada

La ronda 2 decía que SFR-23 "cierra simultáneamente `P4_APPROVAL_SCOPE` y
`P4_REVERIFY_AFTER_APPROVAL`". Un agente posterior podía leer eso como "ya está
implementado" y saltear el trabajo. Se separa explícitamente:

```text
P4_APPROVAL_SCOPE_DESIGN         = CLOSED
P4_APPROVAL_SCOPE_IMPLEMENTATION = OPEN

P4_REVERIFY_DESIGN               = CLOSED
P4_REVERIFY_IMPLEMENTATION       = OPEN
```

**Contract decided ≠ production mechanism implemented.** Ningún blocker de código se
marca `CLOSED` por haber decidido su contrato. Los `CLOSED` de §30.8 son de
**finding**, no de implementación (§30.8, nota).

```text
R3_F3_DESIGN_IMPLEMENTATION_STATUS_SEPARATED = YES
```

### 31.4 R3-F4 — `ApprovalScope` operation-aware

El scope de la ronda 2 exigía `candidate_id` siempre. Eso es correcto para una
Promotion basada en Candidate, pero **no** para un rollback canónico desde una
Generation retenida: obligaría a preservar eternamente un Candidate histórico sólo
para poder volver atrás.

```text
ApprovalOperation = PROMOTION | ROLLBACK

ApprovalScope:
    operation
    generation_id
    clone_id                  # identidad (NECESARIA, no suficiente)
    clone_evidence            # evidencia de CONTENIDO del Clone aprobado (R4-F5)
    runtime_setup_id
    runtime_setup_evidence    # digest del manifest aprobado (R4-F5)
    compatibility_evidence_id
    candidate_id

PROMOTION: candidate_id REQUIRED
ROLLBACK:  candidate_id NOT_APPLICABLE   # ni string vacío, ni Candidate falso
```

La aprobación queda ligada al conjunto **real** de artefactos que se van a activar.
Para rollback, `Generation + Runtime Clone + RuntimeSetup + compatibility evidence`
son suficientes, y el contrato lo sostiene: la coherencia activa de §31.1 no menciona
`candidate_id`, y el rollback canónico (§12) no pasa por un Candidate.

**Corrección de la ronda 4 (R4-F5).** "Suficientes" era cierto sólo como
**identidades**: un Runtime Clone es **mutable** y el gate tolera deriva no crítica,
así que re-verificar `clone_id`/`runtime_setup_id` no detecta un cambio de contenido
con IDs estables. El scope incorpora `clone_evidence` y `runtime_setup_evidence`
(evidencia de contenido) con la invariante
`approval-time evidence == pre-bind freshly recomputed evidence`.

**Rollback canónico explícito:**

```text
select exact historical target (G_prev, S_prev, optional prior clone_id)
verify G_prev authority/evidence
materialize fresh Clone C_prev  (or fast path: retained Clone only if fully admissible)
provision/verify RuntimeSetup S_prev
activation gate (incl. exclusión de Managed Source)
source baseline coherence (Desired == Effective == RuntimeCloneRecord)   # §36.3
present exact ROLLBACK scope + source activation + content evidence + approval_id
user approves
reverify exact approved evidence
CAS CREATE durable PENDING ROLLBACK   # antes de mutar (R4-F3; §36.6) + consumo de aprobación
bind Effective Runtime → C_prev
observe/verify effective + linaje
CAS UPDATE desired pair   # transición aún PENDING; previous_activation_target NO se toca
POST verify coherence
    if FAIL → transition stays PENDING (recoverable); DO NOT finalize
CAS FINALIZE transition   # sólo tras POST OK (R4.1-F1); publica previous_activation_target
SUCCESS
```

```text
R3_F4_APPROVAL_SCOPE_OPERATION_AWARE = YES
R4_F5_APPROVAL_CONTENT_BINDING       = CLOSED
```

### 31.5 R3-B1 — Disponibilidad de los artefactos del RuntimeSetup (blocker nuevo)

`SFR-22` introduce el `RuntimeSetupManifest`, que declara `version`, `hash`,
`source/provenance` y `operation`. **Eso no garantiza que el payload siga disponible
en el futuro.** Ejemplo:

```text
SKSE build X
hash H
source = external URL
```

Si la URL desaparece:

```text
Generation VALID
RuntimeSetupManifest VALID
artifact unavailable
→ rollback operativo no reproducible
```

```text
RUNTIME_SETUP_ARTIFACT_AVAILABILITY =
    OPEN / MUST_BE_ADJUDICATED_BEFORE_OPERATIONAL_ROLLBACK_IS_CLAIMED
```

**Estrategias futuras posibles (no se elige ninguna en esta ronda):**

| Opción | Descripción | Costo / límite |
|---|---|---|
| **A** | External reacquisition only | Barato en disco; el rollback **depende** de terceros (URL, cuenta, disponibilidad) |
| **B** | Retained local payload | Reproducción garantizada; ≈payload completo por versión retenida |
| **C** | Content-addressed local artifact cache | Deduplica entre setups; suma una estructura nueva y su GC |
| **D** | Hybrid | Retiene lo irrecuperable y re-adquiere lo estable; más complejo de razonar |

**Preferencia arquitectónica declarada (sin elegir implementación):**

```text
rollback reproducibility should not silently depend
on third-party artifact availability
```

Es decir: la opción **A** sola es incompatible con la promesa de producto salvo que se
**declare** explícitamente. Si el proyecto quiere poder afirmar

> "puedo volver a una versión anterior cuando el propietario quiera"

entonces **P4/P6/P7 deberán demostrar qué artefactos necesarios quedan retenidos o
reproducibles** para cada Generation retenida. No se implementa cache en esta ronda.

### 31.6 Blockers vigentes

```text
GENERATION_METADATA_INTEGRITY    = OPEN / adjudicate before P4 implementation
RUNTIME_SETUP_MANIFEST_INTEGRITY = OPEN / adjudicate before P4 implementation
RUNTIME_CLONE_RECORD_INTEGRITY   = OPEN / adjudicate before P4 implementation  (R4-F7, ronda 4)
RUNTIME_SETUP_ARTIFACT_AVAILABILITY = OPEN   (R3-B1)
MANDATORY_CRITICAL_EXPECTATIONS  = OPEN / fail-closed contract required
POST_ACTIVATION_LINK_INJECTION   = OPEN / activation hardening
CREATION_CLUB_CLASSIFICATION     = DEFERRED_PENDING_EVIDENCE
P3B                              = DEFERRED_PENDING_RIG
P4_READY_TO_IMPLEMENT            = NO
```

### 31.7 Revisión adversarial local

| # | Pregunta | Estado | Fundamento |
|---|---|---|---|
| 1 | ¿Un Clone vanilla que arranca pero carece de SKSE puede confundirse con un Clone activable? | **ANSWERED** | No: `FRESH_CLONE_READY_FOR_ACTIVATION = NO` hasta que el setup esté **provisionado y verificado** y pasen los gates (§31.2). Poder ejecutar y estar listo para activación son hechos distintos. |
| 2 | ¿Hay algún alias entre `desired_generation_id` y otro nombre? | **ANSWERED** | No: `R3_F1_SCHEMA_NAMES_UNIQUE = YES`; `active.generation_id` / `active.clone_id` eliminados, `desired_active_generation` vive sólo en v1 (§31.1). |
| 3 | ¿Un agente futuro puede leer "`P4_APPROVAL_SCOPE` closed" y saltarse la implementación? | **ANSWERED** | No: `DESIGN = CLOSED` / `IMPLEMENTATION = OPEN` separados explícitamente, y §30.8 aclara que sus `CLOSED` son de *finding* (§31.3). |
| 4 | ¿El rollback exige un Candidate que quizá ya no exista? | **ANSWERED** | No: `candidate_id NOT_APPLICABLE` en `ROLLBACK`; el scope se liga a Generation + Clone + RuntimeSetup + evidencia de compatibilidad (§31.4). |
| 5 | ¿El manifest puede describir un artefacto imposible de recuperar? | **OPEN_WITH_BLOCKER** | Sí: `RUNTIME_SETUP_ARTIFACT_AVAILABILITY = OPEN` (§31.5). El manifest declara procedencia y hash, no garantiza disponibilidad futura. |
| 6 | ¿El diseño promete rollback operativo aunque dependa de una URL externa? | **OPEN_WITH_BLOCKER** | No lo promete sin declararlo: la preferencia es no depender en silencio de terceros, pero la promesa **requiere** adjudicar A/B/C/D y demostrar retención en P4/P6/P7 (§31.5). |
| 7 | ¿Se mantiene clara la diferencia entre versión, setup, disponibilidad de artefactos y Effective Runtime? | **ANSWERED** | Sí: **versión** = Generation (§12); **setup** = `RuntimeSetupManifest` (SFR-22); **disponibilidad** = R3-B1 (§31.5); **Effective Runtime** = `C.root_path` con linaje verificado (§31.1). |

### 31.8 Estado de los residuos

```text
R3_F1_SCHEMA_NAMES              = CLOSED
R3_F2_FRESH_CLONE_SEMANTICS     = CLOSED
R3_F3_DESIGN_IMPL_STATUS        = CLOSED
R3_F4_OPERATION_AWARE_APPROVAL  = CLOSED
R3_B1_ARTIFACT_AVAILABILITY     = OPEN
```

```text
P0_4_CORE_ARCHITECTURE = SOUND   (con R3_B1 abierto y declarado)
P4_READY_TO_DESIGN     = YES
P4_READY_TO_IMPLEMENT  = NO
```

**Alcance de la ronda 3:** docs-only. `PRODUCT_CODE_CHANGED = NO`. No se implementó
ningún schema, ni `active.json`, ni `RuntimeCloneRecord`, ni `RuntimeSetupManifest`,
ni cache de artefactos. No se tocó MO2.

---

## 32. Enmienda P0.4 — ronda 4: adjudicación de la revisión externa (2026-10-08)

Provocada por los reviews reales de GitHub al sacar #701 de Draft. Cada finding se
verificó **contra el HEAD `d8879662`** antes de adjudicarlo; ninguno se aceptó por
autoridad de quien lo reportó. **10 threads abiertos** (1 CodeRabbit, 9 Codex) + 1
inconsistencia de estado del PR.

### 32.1 Adjudicación

| Finding | Fuente | Sev. | Adjudicación | Resolución |
|---|---|---|---|---|
| **R4-F1** README: base ≠ revision P0.4 | CodeRabbit | Minor | **CONFIRMED** | `5039997a` es ancestro de las tres rondas y **no** contiene §§29–31 (verificado: 0 ocurrencias de `## 29.`). El README distingue **revisión base** de **revisiones de enmienda** (`f46853b5`, `d930e5b2`, `d8879662`). |
| **R4-F2** El censo omite consumidores con cache | Codex | P1 | **CONFIRMED** | `GrassCacheService._ensure_runtime_deps` cachea `game_path` con early-return **permanente** (`grass_cache_service.py:277-299`); hay además 2 consumidores que capturan el path en construcción. §29.4 reformulado a **3 clases**; unidad de binding actualizada. |
| **R4-F3** El rollback debe persistir intención antes del binding | Codex | P1 | **CONFIRMED** | La secuencia §12 bindeaba (paso 6) antes de persistir (paso 8), contradiciendo §11 regla 6/§26-15b. Reordenado: `PENDING ROLLBACK` **antes** de mutar; promoción y rollback comparten el modelo durable. |
| **R4-F4** La vía rápida no puede aceptar setup mismatch | Codex | P1 | **CONFIRMED** | El texto decía "o la diferencia se declara en el reporte" ⇒ activar igual y avisar. Ahora **fail-closed**: mismatch ⇒ no se activa hasta reparar/re-provisionar y re-verificar. |
| **R4-F5** `ApprovalScope` debe ligarse al contenido | Codex | P1 | **CONFIRMED** | El scope tenía sólo IDs; el Clone es mutable y el gate tolera deriva no crítica ⇒ TOCTOU. Se agregan `clone_evidence`/`runtime_setup_evidence` e invariante `approval-time == pre-bind fresh`. |
| **R4-F6** El Clone no puede vivir dentro de Managed Source | Codex | P1 | **CONFIRMED** | `verify_physical_independence` sólo compara inodos (hardlinks), no contención; un copiado bajo `steamapps/common` pasaría. Se exige **no-contención/no-solapamiento** sin volver a prefijo fijo. |
| **R4-F7** `RuntimeCloneRecord` es autoridad y necesita integridad | Codex | P1 | **CONFIRMED** | Blocker nuevo `RUNTIME_CLONE_RECORD_INTEGRITY = OPEN` (§19c, §31.6). |
| **R4-F8** Claim residual "execution always drifts" | Codex | P2 | **CONFIRMED** | §29.1 decía "Ejecutar **es** derivar" y E1 "Ejecutar escribe el árbol". Reescritos: ejecución **puede** escribir; no deriva necesariamente (§30.5). |
| **R4-F9** Clone no provisionado necesita ciclo de vida explícito | Codex | P1 | **CONFIRMED** | `RuntimeCloneRecord` exigía `runtime_setup_id` sin distinguir intención de verificación. Se agrega FSM `CREATED/PROVISIONING/PROVISIONED/INVALID` + `intended_`/`verified_runtime_setup_id`. |
| **R4-F10** El rollback necesita el target operativo histórico exacto | Codex | P1 | **CONFIRMED** | `active.json` v2 sólo guardaba el par actual. Se agrega `previous_activation_target` (durable, transaccional); `rollback target must never be guessed`. |
| **R4-F11** El body del PR todavía dice DRAFT | estado del PR | — | **CONFIRMED** | Encabezado actualizado a `READY FOR REVIEW` (no merge-ready). No se escribe `MERGE READY`. |

```text
R4_F1_README_REVISION_MARKER        = CLOSED
R4_F2_CACHED_RUNTIME_CONSUMERS      = CONFIRMED / CLOSED
R4_F3_ROLLBACK_DURABLE_INTENT       = CLOSED
R4_F4_RETAINED_CLONE_SETUP_GATE     = CLOSED
R4_F5_APPROVAL_CONTENT_BINDING      = CLOSED
R4_F6_MANAGED_SOURCE_EXCLUSION      = CLOSED
R4_F7_CLONE_RECORD_INTEGRITY        = CONFIRMED / blocker registrado
R4_F8_EXECUTION_VS_DRIFT            = CLOSED
R4_F9_CLONE_PROVISIONING_LIFECYCLE  = CLOSED
R4_F10_ROLLBACK_TARGET_HISTORY      = CONFIRMED / CLOSED
R4_F11_PR_STATUS_TEXT               = CLOSED
```

> **Supersesión parcial (ronda 4.1, §33).** Los flujos canónicos ilustrados en §32.3
> y §32.4 de esta sección finalizaban la transición **antes** del POST-verify; la
> ronda 4.1 lo corrigió (`FINALIZED ⇒ POST verification already passed`). Los
> veredictos de la tabla de arriba siguen vigentes; sólo cambió el **ordering** de los
> flujos. Ver §33.2.

### 32.2 La distinción crítica (autoridades separadas)

```text
Generation                  = version authority
RuntimeSetupManifest        = setup authority
RuntimeSetup artifact store = availability authority/problem   (R3-B1, OPEN)
RuntimeCloneRecord          = clone identity + lineage + lifecycle metadata
Runtime Clone               = mutable operational materialization
Approval evidence           = exact state/content approved          (R4-F5)
Effective Runtime           = actual path MO2/SKSE are using
Durable transition          = crash-safe intent/result state       (R4-F3)
```

Ninguna se mezcla: la Generation no es el setup, el setup no es la disponibilidad
del artefacto, el registro no es la materialización, y la identidad del Clone no es
la evidencia de su contenido.

### 32.3 Flujo canónico de Promotion (referencia; no implementado)

```text
Managed Source
→ Candidate READY
→ publish Generation G
→ verify G
→ create Runtime Clone C
→ persist/track Clone lifecycle (CREATED → PROVISIONING)
→ provision RuntimeSetup S
→ verify setup (→ PROVISIONED)
→ compatibility gate
→ activation gate
→ source baseline coherence (Desired == Effective == RuntimeCloneRecord)   # §36.3
→ PRESENT exact ApprovalScope + source activation + content evidence + approval_id
→ USER APPROVES
→ freshly reverify exact approved evidence
→ CAS CREATE durable PENDING PROMOTION + consumo de aprobación             # §36.5/§36.6
→ bind Effective Runtime → C
→ observe Effective Runtime
→ CAS UPDATE desired Generation+Clone   # previous_activation_target NO se toca
→ POST verify coherence
→     if FAIL → transition stays PENDING (recoverable); DO NOT finalize
→ CAS FINALIZE transition        # sólo tras POST OK (R4.1-F1)
→     publica previous_activation_target = source_activation              # §35.8.5
→ SUCCESS
```

### 32.4 Flujo canónico de Rollback (referencia; no implementado)

```text
select exact historical rollback target
    Generation G_prev
    RuntimeSetup S_prev
    optional prior Clone for fast path
verify target authority/evidence
choose:
    canonical → new Clone from G_prev
    fast path → retained Clone only if fully admissible
provision/verify S_prev
compatibility gate
activation gate
source baseline coherence (Desired == Effective == RuntimeCloneRecord)   # §36.3
present exact ROLLBACK ApprovalScope + source activation + content evidence + approval_id
USER APPROVES
reverify exact approved evidence
CAS CREATE durable PENDING ROLLBACK + consumo de aprobación   # §36.5/§36.6
bind Effective Runtime
observe/verify Effective Runtime
CAS UPDATE desired Generation+Clone   # aún PENDING; previous_activation_target NO se toca
POST verify coherence
    if FAIL → transition stays PENDING (recoverable); DO NOT finalize
CAS FINALIZE transition   # sólo tras POST OK (R4.1-F1); publica previous_activation_target
SUCCESS
```

Un Clone retenido cuya **Generation de origen no esté `VALID`** queda **RECHAZADO**
para activación (R4.1-F2), igual que uno con setup que no coincide (R4-F4): en ambos
casos **NO ACTIVATION**, y los indicios favorables (Clone existente, SKSE intacto,
setup aparentemente correcto) sirven para **diagnóstico**, no para autorización.

### 32.5 Unidad de binding del game path

```text
P5_BINDING_UNIT =
    configuradas/persistidas
  + derivadas
  + consumidores con cache en memoria

CONFIGURED_GAME_PATH_SURFACES = 6   (2 persistidas + 4 derivadas)
CACHED_RUNTIME_CONSUMERS      = 3   (1 sticky + 2 capturadas en construcción)
TOTAL_BINDING_CLASSES         = 3
```

No se congela `COUNT = 7`: la clase 3 es una **familia** a descubrir por enumeración,
y un consumidor nuevo debe romper el ancla hasta ser cableado (§29.4).

### 32.6 Blockers vigentes (acumulado tras la ronda 4)

```text
GENERATION_METADATA_INTEGRITY      = OPEN / adjudicate before P4 implementation
RUNTIME_SETUP_MANIFEST_INTEGRITY   = OPEN / adjudicate before P4 implementation
RUNTIME_CLONE_RECORD_INTEGRITY     = OPEN / adjudicate before P4 implementation  (R4-F7)
RUNTIME_SETUP_ARTIFACT_AVAILABILITY = OPEN / adjudicate before operational rollback is claimed
MANDATORY_CRITICAL_EXPECTATIONS    = OPEN / fail-closed contract required
POST_ACTIVATION_LINK_INJECTION     = OPEN / activation hardening
CREATION_CLUB_CLASSIFICATION       = DEFERRED_PENDING_EVIDENCE
P3B                                = DEFERRED_PENDING_RIG
P4_READY_TO_IMPLEMENT              = NO
```

Ningún blocker se cerró para poder mergear.

### 32.7 Revisión adversarial local

| # | Pregunta | Estado | Fundamento |
|---|---|---|---|
| 1 | ¿La verificación de independencia física cubre contención en Managed Source? | **ANSWERED** | No: `verify_physical_independence` compara `(st_dev, st_ino)` por archivo (hardlinks). La contención es un chequeo **distinto** y ahora explícito (§29.3 paso 0; R4-F6). |
| 2 | ¿El rollback puede activar un Clone con setup distinto al manifest? | **ANSWERED** | No: fail-closed (R4-F4). |
| 3 | ¿Puede aprobarse un payload distinto del que se mostró? | **ANSWERED** | No si el scope liga evidencia de contenido (R4-F5); sí si sólo compara IDs, que es lo que se corrigió. |
| 4 | ¿Se puede saber qué setup estaba activo antes de promocionar? | **ANSWERED** | Sí, con `previous_activation_target` durable (R4-F10), publicado al FINALIZE y con el registro de transición finalizado como autoridad única (D0-R2.1/D0-R2.7; §36.2/§36.8). Sin él, el rollback adivinaba. |
| 5 | ¿Un Clone cortado a mitad de provisionamiento se activa? | **ANSWERED** | No: `lifecycle == PROVISIONED` requerido (R4-F9). |
| 6 | ¿Queda alguna afirmación de que ejecutar *necesariamente* deriva? | **ANSWERED** | No: §29.1 y E1 reescritos; ejecución = violación de política, modificación = `DRIFTED` (§30.5). |
| 7 | ¿El `RuntimeCloneRecord` puede forjarse en silencio? | **OPEN_WITH_BLOCKER** | `RUNTIME_CLONE_RECORD_INTEGRITY = OPEN` (R4-F7). Fuera del threat model de administrador malicioso; se adjudica en P4. |
| 8 | ¿El `COUNT` de superficies del game path es estable? | **ANSWERED** | No se congela un total: la clase 3 es una familia enumerable (R4-F2). |

### 32.8 Estado

```text
P0_4_CORE_ARCHITECTURE = SOUND   (con R3_B1 y R4-F7 abiertos y declarados)
P4_READY_TO_DESIGN     = YES
P4_READY_TO_IMPLEMENT  = NO
```

**Alcance de la ronda 4:** docs-only. `PRODUCT_CODE_CHANGED = NO`. No se implementó
FSM de ciclo de vida, ni `previous_activation_target`, ni evidencia de contenido, ni
exclusión física de rutas, ni reconciliación al arranque. No se tocó MO2 ni P4/P5.

**Qodo Regression & Test Oracle — `CANCELLED`.** Investigado: **no** es una falla del
producto. La conclusión autoritativa del check-run es `cancelled` (no `fail`). El log
muestra que el modelo primario devolvió YAML inparseable, el fallback free colgó, y el
job murió por su propio `timeout-minutes: 15`. Clasificación:
`QODO_REGRESSION_CANCELLED = ACTION_REQUIRED` (re-ejecutar), **no** `BENIGN_SUPERSEDED`
(no hubo run más nuevo que lo reemplace) y **no** un defecto del ADR. El re-run del
HEAD `16619d3d` terminó `pass` en 1m6s, confirmando el flake transitorio.

---

## 33. Enmienda P0.4 — ronda 4.1: dos contratos de secuencia y autorización (2026-10-08)

Sobre `16619d3d`, CodeRabbit publicó **2 findings nuevos** en comentarios *outside
diff* (no como threads inline). Por eso `REVIEW_THREADS_REMAINING = 0` **no** era
suficiente como gate: los findings fuera de diff viven en el cuerpo de la review, no
en `reviewThreads`. Ambos verificados contra el texto y **CONFIRMED**.

### 33.1 Adjudicación

| Finding | Fuente | Sev. | Adjudicación | Resolución |
|---|---|---|---|---|
| **R4.1-F1** La transición se finaliza **antes** del POST-verify | CodeRabbit | Major | **CONFIRMED** | Promoción (§7, §11) y rollback (§12, §31.4, §32.3/§32.4) cerraban la transición **antes** del POST-verify. Reordenados los **6** flujos: `PENDING → bind → persist/observar → POST VERIFY → (si pasa) FINALIZE → SUCCESS`. Invariante nuevo: `FINALIZED ⇒ POST verification already passed`. Si el POST falla, la transición **sigue `PENDING`** (recuperable). |
| **R4.1-F2** La vía rápida admite activación sin Generation `VALID` | CodeRabbit | Major | **CONFIRMED** | La vía rápida decía "si (b) no se cumple, la activación es posible pero debe declararlo". Contradice el gate de §29.3 y SFR-17. Ahora (a), (b) y (c) son **condiciones de autorización, no de diagnóstico**: cualquiera que falte ⇒ `retained Clone activation = REJECTED`. |

```text
R4_1_F1_FINALIZE_AFTER_POST_VERIFY   = CLOSED
R4_1_F2_FAST_PATH_REQUIRES_VALID     = CLOSED
```

### 33.2 Por qué R4.1-F1 importa

Cerrar la transición antes del POST-verify convierte una falla de verificación en un
estado **no recuperable por el arranque**: el registro dice "terminado" cuando el
desenlace real todavía no se confirmó. Es exactamente el split-brain que la transición
durable existe para evitar, reintroducido por el orden de dos pasos.

```text
PENDING → bind → persist/observe result → POST VERIFY
                                              │
                              ┌───────────────┴───────────────┐
                          PASS│                               │FAIL
                              ▼                               ▼
                    FINALIZE → SUCCESS            queda PENDING (recuperable)
```

La §29.8 ya declaraba "se finaliza tras el POST-verify" para `P4_DURABLE_TRANSITION`;
los flujos ilustrativos lo contradecían. Ahora coinciden, y el invariante queda
explícito en §11 regla 7 y en la coherencia de §31.1.

### 33.3 Por qué R4.1-F2 importa

La vía rápida existe **porque** la reparación por re-clonado sigue disponible —y esa
ruta requiere una Generation de origen `VALID`. Sin ella, activar el Clone retenido
deja al usuario sin fuente de recuperación si el Clone resulta defectuoso.

```text
source Generation != VALID  →  retained Clone activation = REJECTED
```

Los indicios favorables (el Clone existe, SKSE parece intacto, el setup se ve
correcto) sirven para **diagnóstico**, nunca para autorización. Es la misma clase de
error que R4-F4: tolerar una condición de autorización como si fuera una advertencia.

### 33.4 Estado

```text
R4_1_F1_FINALIZE_AFTER_POST_VERIFY = CLOSED
R4_1_F2_FAST_PATH_REQUIRES_VALID   = CLOSED

P0_4_CORE_ARCHITECTURE = SOUND   (con R3_B1 y R4-F7 abiertos y declarados)
P4_READY_TO_DESIGN     = YES
P4_READY_TO_IMPLEMENT  = NO
PR_READY_TO_MERGE      = NO
```

**Alcance de la ronda 4.1:** docs-only. `PRODUCT_CODE_CHANGED = NO`. Sólo se
corrigieron los dos contratos de secuencia/autorización; no se implementó nada de
P4/P5, no se tocó MO2, y no se reabrió ningún finding ya cerrado.

---

## 34. P4-D0 — congelamiento de contratos de transición y autoridad (2026-10-08)

> **Supersesión parcial (ronda adversarial 1, §35).** La revisión del Tech Lead sobre
> `81fb83a2` encontró **6 defectos contractuales** en esta sección. Los veredictos y el
> censo de abajo siguen siendo el registro de la primera pasada, pero quedan
> **corregidos** por §35 en estos puntos concretos:
>
> - **§34.6** (`ApprovalScope`) — le faltaba el **source activation** y no adjudicaba
>   single-use/expiración/identidad del propietario. ⇒ §35.2 (D0-R1).
> - **§34.5 / §34.15 C3** — el orden de C3 era **imposible**: con `Desired=source` y
>   `Effective=target` el POST de coherencia no puede pasar. ⇒ §35.3 (D0-R2).
> - **§34.13** (`POST_ACTIVATION_LINK_INJECTION`) — derivar a P5 un enlace fuera del
>   inventario **no alcanza**. ⇒ §35.4 (D0-R3).
> - **§34.17** — omitía **5 requisitos P4** registrados en §29.8. ⇒ §35.5 (D0-R4).
> - **§34.4** — `FINALIZED` dependía del orden procedural, sin evidencia durable del
>   POST ni regla anti-clobber. ⇒ §35.6 (D0-R5).
> - **§34.14** — "P4 no espera a P5" era ambiguo. ⇒ §35.7 (D0-R6).
>
> **§34.18 declaraba `P4_DESIGN_FROZEN = YES` y `P4_READY_TO_IMPLEMENT = YES`: ambos
> quedan `NO` hasta que §35 cierre los 6 findings.** El estado vigente es el de §35.12.
>
> **Segunda supersesión parcial (ronda adversarial 2, §36).** La revisión externa sobre
> `589805ee` encontró **6 residuos nuevos** (más un séptimo de consistencia de schema y
> un defecto aritmético del censo). Quedan corregidos por §36 en estos puntos de esta
> sección:
>
> - **§34.4** — `source_activation` no probaba el Effective Runtime saliente. ⇒ §36.3.
> - **§34.5** — la tabla de reconciliación no cubría `NONE + Desired != Effective` ni
>   distinguía journal corrupto de ausente. ⇒ §36.4.
> - **§34.6** — el `ApprovalScope` seguía sin `source_activation` ni autoridad durable
>   de consumo. ⇒ §36.3/§36.5.
> - **§34.16 q11** — el fundamento citaba el target histórico sin su punto de
>   publicación ni su autoridad. ⇒ §36.2/§36.8.
>
> **§34.18 ya no es el estado vigente: el estado es el de §36.15.**

Esta sección **cierra el diseño contractual** de P4. **No implementa P4.** Su única
promesa es que, si los contratos de acá se respetan, una implementación de P4 puede
ser *fail-closed por construcción*. `P4_IMPLEMENTED = NO`, `P5_IMPLEMENTED = NO`.

**Baseline congelado.** `origin/main = af8e726c7195b35c38cd10cdfdef8d3fbfb6803c`
(merge de #701), que es el main post-P3 (#698, `5039997a`) más la tanda de
endurecimiento de MO2/VFS. `HEAD == origin/main`, `WORKTREE_CLEAN = YES`.
Rama `design/frozen-runtime-p4-contracts` (nombre **plano**; en esta máquina las
ramas anidadas pueden perder la ref — ver `MEMORY.md`).

### 34.1 Adjudicación del tracker #672

El cuerpo de #672 arrastra texto previo a P0.4. Se contrastó **claim por claim**
contra el ADR vigente (§§29–33) y contra el código actual de `main`.

| TRACKER CLAIM | CURRENT ADR | CURRENT CODE | VERDICT | ACTION |
|---|---|---|---|---|
| "Frozen Runtime = runtime real del usuario" | §7/§28: la Effective Runtime es la que consume MO2/SKSE; la Generation **no se ejecuta** | `frozen_runtime/` no contiene ningún ejecutor; `runtime_vault/clone.py` produce materialización | **CURRENT (con matiz)** | Conservar. El matiz ("quién la consume") es de P5 |
| "Sky-Claw observa + valida + crea candidate + promueve sólo con autorización" | §7, SFR-06/08 | `candidates.py` existe; promoción **no** | **CURRENT** | Conservar |
| "P3 = CLOSED, PR #682 MERGED" | §29–31 no contradicen | `copying.py`, `candidates.py` presentes en `main` | **CURRENT** | Conservar |
| "P4 = READY_TO_START, no iniciado" | §33.4 `P4_READY_TO_DESIGN = YES` | `runtime_setup`/`RuntimeCloneRecord` **ausentes** del código | **CURRENT** | Refinar a `P4_READY_TO_DESIGN = YES`, `P4_READY_TO_IMPLEMENT = NO` |
| `P4_APPROVAL_SCOPE` = "aprobación ligada al Candidate **exacto**" | §31.4: operation-aware, ligada a **contenido**, no sólo IDs | no implementado | **SUPERSEDED** | Reemplazar por el contrato de §34.5 (D0-4) |
| `P5 blocker` = "binding de **dos** superficies" | §29.4/§32.5: **3 clases** (6 configuradas/derivadas + 3 cacheadas) | 3 consumidores con cache verificados | **SUPERSEDED** | Reemplazar por "3 clases"; no congelar COUNT |
| "SFR-16: promoción no completa hasta Desired ≡ Effective probados" | §33.2 `FINALIZED ⇒ POST verification already passed` | — | **CURRENT (reforzado)** | Conservar; el orden de §34.4 lo hace ejecutable |
| "SFR-15: el Candidate jamás establece su propia evidencia" | §29, P3-Z/AA | `candidates.py::exigir_listo_para_persistencia` | **CURRENT** | Conservar; P4 lo extiende al `ApprovalScope` |
| `P5_EFFECTIVE_RUNTIME_ORACLE = OPEN` | §32.6 sigue abierto | no existe oráculo | **STILL_OPEN** | P5; P4 responde `UNKNOWN → NO ACTIVATION` |
| `CREATION_CLUB_CLASSIFICATION` sin resolver | §32.6 `DEFERRED_PENDING_EVIDENCE` | — | **DEFERRED_PENDING_EVIDENCE** | No decidir por intuición (§34.11) |
| `P3_DIRECTORY_MEMBERSHIP` bloqueaba el TreeDigest | §29: cerrado | `membership.py` completo | **STALE (cerrado)** | Archivar |
| "No se inicia P3 sin cerrar sus blockers" | §29 cerrado, §31.3 | — | **STALE** | Reformular como "no se inicia **P4** sin cerrar los D0-*" |
| "Binding de dos superficies" (roadmap P5) | §29.4 | — | **SUPERSEDED** | Ídem fila P5 |

**Nota de proceso.** No se actualiza #672 en esta ronda: la instrucción es no tocar el
tracker hasta que el diseño esté cerrado **y** el Tech Lead lo valide. Se propone el
update preciso en §34.13.

### 34.2 Censo de código (evidencia enumerativa, no por nombre)

Antes de diseñar se enumeró qué existe **hoy** en `main`. Resultado (todas son
ausencias o presencias **verificadas por grep**, no inferidas):

```text
EXISTE:
  storage.py            layout, contención (normcase), admisión fail-closed de root
  state.py              active.json schema v1; escritura atómica; reserva 'x'
  generations.py        metadata fuera del árbol; verificar_generation; colisiones
  independence.py       SFR-18 (inodo, st_nlink==1, reparse); contención/overlap
  membership.py         TreeDigest + membership de directorios; canonicalización
  copying.py            copiado independiente; lote validado antes de mutar
  candidates.py         Candidate READY; evidencia PRE/POST; SFR-15
  observation.py        CRITICAL_EXE_BY_GAME = {"skyrimse": "SkyrimSE.exe"}
  discovery.py          Managed Source (FOUND/AMBIGUOUS/INVALID)
  provider_signals.py   ProviderObservationState; StateFlags != "4" ⇒ ACTIVE
  runtime_vault/locking.py   destination_lock (destino-keyed, %TEMP%)
  runtime_vault/clone.py     create_runtime_clone(*, critical_expectations=())
  runtime_vault/golden.py    verify_critical_files(for exp in expectations)

NO EXISTE (verificado):
  RuntimeSetupManifest / runtime_setup_*    (0 hits en sky_claw/, sólo el ADR)
  RuntimeCloneRecord / lifecycle FSM        (0 hits en sky_claw/, sólo el ADR)
  admitted_role                             (0 hits)
  gate de compatibilidad P4↔P5              (0 hits)
  provisionador de SKSE                     (sólo skse_catalog.py: catálogo puro)
  lock cross-process del Frozen Runtime
  journal / transición durable
  reconciliación de arranque
```

`SKSE_CATALOG_IS_A_PROVISIONER = NO`: `skse_catalog.py` declara explícitamente "No
descarga ni verifica artifacts… Sin I/O ni side effects DE ESTE MÓDULO", y **2 de 5**
releases tienen `artifact_name = None` (fuente NEXUS sin URL estática verificable).
Esto es evidencia dura para D0-7/D0-9: "recordar la URL del artefacto" **no está
disponible** ni siquiera como promesa para todos los casos.

```text
CRITICAL_EXE_SOURCE_OF_TRUTH = {"skyrimse": "SkyrimSE.exe"}   (observation.py)
CRITICAL_EXPECTATIONS_DEFAULT = ()                            (clone.py, golden.py)
all(()) == True                                               (footgun confirmado)
```

### 34.3 D0-1 — `P4_CROSS_PROCESS_LOCK` (diseño)

**Evidencia.** El único lock real es `runtime_vault/locking.py::destination_lock`:
clave = SHA-256 de `normcase(abspath(destino))` resuelto; archivo en
`Path(tempfile.gettempdir())/".skyclaw_vault_locks"/…`; `threading.Lock` +
`os.open(O_RDWR|O_CREAT)` + `msvcrt.locking(LK_NBLCK,1)` / `fcntl.flock(EX|NB)`;
`RuntimeCloneError` fail-closed; libera en `finally`. **No tiene** identidad de
dueño, lease, heartbeat, liveness de dueño muerto, ni recuperación más allá de que el
SO libere el byte-range. `DistributedLockManager` (`app/db/locks.py`, SQLite/aiosqlite)
sí tiene todo eso, pero es **async** y otra capa.

**Decisión.**

```text
LOCK_RESOURCE      = FrozenRuntimeRoot   (uno solo, no destino-keyed)
LOCK_IDENTITY      = root canónico (normcase abspath) + session_id (UUID por adquisición)
LOCK_SCOPE         = toda mutación de autoridad del runtime
LOCK_EXCLUSION_SET = { PROMOTION, ROLLBACK, activation/rebinding,
                       startup reconciliation, publish Generation,
                       candidate build que escriba state/ }
LOCK_BUSY          = fail-closed con timeout acotado; NO espera indefinida
LOCK_OWNER         = { session_id, pid, create_time (reloj del SO), acquired_at }
LOCK_STALE         = dueño NO vivo ⇒ permitido reclamar (con evidencia)
LOCK_LIVENESS      = pid + create_time  (NUNCA os.kill(pid,0); reuso de PID)
LOCK_CRASH         = el lock NO se borra al morir el proceso; se marca huérfano y
                     el arranque decide (§34.4)
LOCK_TTL           = requerido si hay renew; renew_interval < TTL / 2
LOCK_MULTI_RESOURCE= NO (un root, un lock; sin orden de adquisición ⇒ sin deadlock)
```

**Por qué root-keyed y no destino-keyed.** La promoción y el rollback mutan **la
misma** autoridad (`active.json` + Effective Runtime) aunque el destino sea distinto.
Un lock por destino permitiría dos transiciones concurrentes sobre estados que se
pisan. El recurso lógico es **el runtime**, no la carpeta.

**Principio que sí se reutiliza (código, no).** De `vfs_broker.py` se adopta el
**patrón**, no la implementación: (a) token de adquisición que obliga a que release y
renew prueben propiedad; (b) `assert_owned()` inmediatamente antes de mutar;
(c) liveness por `pid + create_time`; (d) retención fail-closed del lock envenenado en
vez de borrarlo. `CROSS_PROCESS_LOCK_PRIMITIVE_REUSE = NO` (principio sí, primitiva no).

### 34.4 D0-2 — `P4_DURABLE_TRANSITION` (diseño)

**Evidencia.** No existe journal. `active.json` v1 sólo guarda
`{schema_version, desired_active_generation, updated_at_ns}` y su docstring dice
explícitamente "este archivo NO registra Effective Runtime". `candidates.py` tiene el
precedente exacto de "no puedo persistir un estado autoritativo sin evidencia
completa" (`exigir_listo_para_persistencia`).

**Estado durable mínimo (persistido ANTES de mutar Effective Runtime):**

```text
operation             PROMOTION | ROLLBACK
transition_id         UUID v4 (una por transición; idempotencia)
journal_revision      entero monótono por root (CAS anti-ABA; §36.6)
schema_version        entero (v1)
state                 NONE | PENDING_PROMOTION | PENDING_ROLLBACK | FINALIZED
source_activation     qué estaba activo:
                      { generation_id, clone_id, runtime_setup_id,
                        effective_runtime_evidence }      (§36.3; D0-R2.2)
source_activation_digest  SHA-256(canonical_json(source_activation))
target_generation_id
target_clone_id
target_runtime_setup_id
approval_scope_digest hash del scope aprobado (§34.6)
consumed_approval_id  la aprobación que esta transición consumió (§36.5)
evidence_digests      { clone_evidence, runtime_setup_evidence, compatibility_evidence }
expected_effective_path   la ruta que el bind va a producir
created_at_ns         entero >= 0
LOCK_SESSION_ID       quién tiene el lock al crear la transición
                      (propiedad de proceso, NO autorización humana; §35.2)
```

Al finalizar, el registro se completa con la evidencia durable del POST
(`post_verify_evidence_digest`, `post_verified_at_ns`, `finalized_from_transition_id`,
§35.6) y pasa a ser **inmutable** (§36.8/§36.9).

**Reglas.**

```text
PERSIST_BEFORE_BIND        = YES   (invariante duro)
FINISHED_BEFORE_POST       = PROHIBIDO   (§33.2)
FINALIZED ⇒ POST passed    = YES   (unidireccional)
TRANSITION_ID_INMUTABLE    = YES
WRITE_ATOMIC               = write_json_atomic (mkstemp+fsync+os.replace)
STATE_FILE                 = state/transition.json   (NUEVO; sujeto al oráculo AST)
UNKNOWN_STATE              = fail-closed (no "NONE por defecto")
CORRUPT_STATE              = LANZA (nunca se degrada a "limpio"; §36.4 N2)
AT_MOST_ONE_PENDING_PER_ROOT = YES  (PENDING exists ⇒ no second transition; §36.6)
CAS_ON_EVERY_MUTATION      = YES   (transition_id + journal_revision; §36.6)
PREVIOUS_TARGET_WRITE_POINT = FINALIZE (nunca antes del POST PASS; §35.8.5/§36.2)
PREVIOUS_TARGET_AUTHORITY   = registro de transición finalizado (no active.json; §36.8)
```

**Restricción del oráculo de escritura.** `tests/test_frozen_runtime_p1_readonly.py`
congela `MODULOS_CON_ESCRITURA_PERMITIDA` y prohíbe `os.open()`. Toda escritura nueva
de P4 debe (a) vivir en un módulo declarado y (b) usar `open(path, "x")` para reserva
exclusiva, nunca `os.open`. Esto **no se implementa ahora**, sólo se registra como
restricción vinculante.

**No se inventan nombres mejores que los del repo**: se reutiliza `schema_version`,
`desired_active_generation`, `updated_at_ns` y el vocabulario `PENDING_*` que ya
aparece en §11/§32.3/§32.4.

### 34.5 D0-3 — `P4_STARTUP_RECONCILIATION` (diseño)

**Evidencia doctrinal (reutilizable como principio).** `docs/operations/recovery.md`:
"Detener productores, preservar evidencia y recuperar desde la última frontera
durable conocida. No borrar archivos de control para forzar un estado limpio."
ADR 0007: "La ausencia de evidencia terminal no equivale a terminalidad"; liberación
de cuarentena automática **sólo** con evidencia tardía, o manual con
`release_quarantine(evidence=...)`.

**Entradas de la reconciliación** (leídas al arrancar, sin asumir nada):

```text
observed_desired_state    active.json (o ausente)
observed_effective_state  lo que realmente consume el runtime (oráculo de P5)
pending_transition         transition.json (o ausente/corrupto)
target_evidence            los digests referenciados por la transición
```

**Resultado determinista:**

| Situación | Resultado | Automático | Aprobación de dueño |
|---|---|---|---|
| **N0** — no hay transición (`NONE`), `Desired == Effective`, coherente | `COMPLETE` | — | — |
| **N1** — no hay transición (`NONE`), `Desired != Effective` | `INCONSISTENT_BASELINE` → `FAIL_CLOSED` (§36.4) | no | sí |
| **N2** — `transition.json` corrupto o ilegible | `FAIL_CLOSED` — **nunca** se lee como `NONE` (§36.4) | no | sí |
| `PENDING_*`, Effective **coincide** con `expected_effective_path`, POST no corrido | re-correr POST → `COMPLETE`/`FINALIZE` | sí | no |
| `PENDING_*`, Effective **no** coincide, y la evidencia objetivo se re-verifica OK | `RECOVER` (re-bind idempotente) | sí | no |
| `PENDING_*`, evidencia objetivo **cambió** o no re-verifica | `REQUIRE_OWNER` | no | sí |
| `state` desconocido / schema futuro | `FAIL_CLOSED` | no | sí |
| Effective **indeterminado** (no se puede observar) | `FAIL_CLOSED` | no | sí |

```text
INCOMPLETE_TRANSITION_IS_AUTOMATICALLY_REVERTED = NO
NO_PENDING_BASELINE_SPLIT_IS_A_FAIL_CLOSED_STATE = YES   (N1; §36.4)
CORRUPT_JOURNAL_IS_NEVER_READ_AS_ABSENT          = YES   (N2; §36.4)
```

La fila **N1** es la que faltaba: el estado "no hay transición y aun así Desired y
Effective divergen" puede venir de un cambio externo, corrupción, un fallo histórico,
intervención manual, software ajeno o pérdida del journal. **No** se fabrica un
`PENDING` retroactivo y **no** se elige automáticamente ni Desired ni Effective: la
reconciliación de arranque debe detectarlo **aunque `transition.json` no exista**.

Un `PENDING` **no** se revierte solo: "revertir" asume que el bind no ocurrió o que
deshacerlo es seguro, y ninguna de las dos cosas se sabe. La reconciliación **observa
primero** y decide después. Si el Effective ya coincide con el objetivo, la transición
está más cerca de completarse que de revertirse.

### 34.6 D0-4 — `P4_APPROVAL_SCOPE` (diseño)

Operation-aware y ligado a **contenido**, no a IDs (R4-F5, §31.4).

```text
ApprovalScope {
  operation            PROMOTION | ROLLBACK
  source_activation    { generation_id, clone_id, runtime_setup_id,
                         effective_runtime_evidence }      (§36.3; D0-R2.2)
  source_activation_digest
  generation_id
  clone_id
  runtime_setup_id
  candidate_id         (PROMOTION: id;  ROLLBACK: NOT_APPLICABLE)
  compatibility_evidence_id
  clone_evidence       { identidad de contenido del Clone, no su ruta }
  runtime_setup_evidence { identidad de contenido del setup aplicado }
  approval_id          identidad durable single-use (§36.5; D0-R2.4)
}
```

Sin `source_activation` una aprobación obtenida en otro contexto pasaría con el
target intacto (§35.2); sin `approval_id` no hay forma de probar el consumo después
de rotar el journal (§36.5).

Heredado de `candidates.py`/`membership.py`: la evidencia es un **digest sellado**
capturado de una sola observación coherente (`observar_arbol_sellado`), no una lista
de rutas. `APPROVE_BY_IDS_ONLY = PROHIBIDO`. `clone_evidence` y
`runtime_setup_evidence` **son** los digests de contenido; no existen como campos
sueltos "por si acaso".

### 34.7 D0-5 — `P4_REVERIFY_AFTER_APPROVAL` (diseño)

```text
GATE = approval-time evidence == freshly recomputed pre-bind evidence
RE_MEASURED    = { clone content digest, runtime_setup applied-state digest,
                   identity de Generation, identidad física del Clone }
COMPARED       = digests exactos (no "parecido", no "misma ruta")
METADATA_RE_READ = sí (nunca se confía en el objeto cacheado del momento de aprobar)
ANY_APPROVED_MATERIAL_CHANGE = NO BIND
```

Modelo de implementación: `observar_arbol_sellado` ya resuelve el problema de "una
sola observación, o ninguna" (membership PRE + identidad PRE + inventario + identidad
POST + membership POST, y `TreeObservationCoherenceError` si algo derivó). El reverify
reusa esa forma: no es un segundo mecanismo, es el mismo aplicado al momento correcto.

**Ventana residual declarada:** `observar_arbol_sellado` documenta que **no** promete
una garantía TOCTOU fuerte (no hay handle-grade). El lock (§34.3) reduce la carrera
entre procesos; no la elimina a nivel kernel.
`HANDLE_GRADE_PRE_BIND = NO` (declarado, no oculto).

### 34.8 D0-6 — `GENERATION_METADATA_INTEGRITY` (diseño)

**Evidencia.** La metadata vive **fuera** del árbol, en
`state/generations/<generation-id>.json`. `_metadata_de_payload` es estricto; el id se
deriva del nombre del directorio (`display_version = id.split("__",1)[0]`); hay
chequeo `metadata.generation_id != entrada.name ⇒ INVALID`; `descubrir_generations`
clasifica. **No hay** sello independiente, ni registro de dueño, ni contraste
`created_at_ns` vs ctime del directorio.

**Decisión — detección de corrupción accidental, no defensa anti-administrador:**

```text
THREAT_MODEL = integridad accidental / corrupción / inconsistencia
               (NO administrador malicioso) ⇒ sin HMAC ni firma criptográfica
SELLAR = { schema_version, generation_id, display_version, tree_digest,
           critical_files, digest_global }
DETECTA = clobber (id de metadata ≠ nombre de dir)   → INVALID
          raíz inconsistente (root declarado ≠ root real) → INVALID
          digest mismatch (inventario fresco ≠ registrado) → DRIFTED/INVALID
          lineage mismatch (source_* incoherente)     → INVALID
          metadata ausente/corrupta                   → INDETERMINATE (nunca VALID)
UNKNOWN != VALID  (ya vigente)
```

Costo conocido y aceptado: hoy `verificar_generation` camina el árbol **dos veces**
por verificación on-demand; la optimización de un solo recorrido queda diferida a
P3/P4 (documentado en el propio módulo). No se cierra acá.

### 34.9 D0-7 — `RUNTIME_SETUP_MANIFEST_INTEGRITY` (diseño)

Tres problemas **separados** que el texto viejo mezclaba:

```text
MANIFEST_INTEGRITY      el manifiesto es válido y no fue alterado
ARTIFACT_AVAILABILITY   el payload todavía se puede obtener       (§34.11, D0-9)
ARTIFACT_INTEGRITY      el payload es el que el manifiesto declara
```

```text
MUST_SEAL = { schema_version, artifact_identities, artifact_hashes,
              source/provenance, operations, target_locations,
              ordering/dependencies }
VALID_MANIFEST ⇒ PAYLOAD_AVAILABLE = FALSO   (no se sigue)
```

El manifiesto **no** promete disponibilidad. Un manifiesto válido con payload
desaparecido es un estado real y debe reportarse como tal, no como "listo".

### 34.10 D0-8 — `RUNTIME_CLONE_RECORD_INTEGRITY` (diseño)

El registro (`state/clones/<clone-id>.json`) es **autoridad** sobre identidad, linaje
y ciclo de vida. Hoy **no existe** (0 hits).

```text
CAMPOS = { schema_version, clone_id, source_generation_id, root_path,
           lifecycle (CREATED|PROVISIONING|PROVISIONED|INVALID),
           intended_runtime_setup_id, verified_runtime_setup_id, admitted_role }

FAIL_CLOSED si:
  registro ausente                     → INVALID (no "crear por defecto")
  schema inválido / campos faltantes   → INVALID
  root_path ≠ raíz real del Clone      → INVALID
  clone_id ≠ identidad del directorio  → INVALID
  source_generation_id no VALID        → RECHAZA activación (§33.3)
  setup mismatch (intended ≠ verified) → RECHAZA activación
  transición de lifecycle inválida     → INVALID (FSM explícita)
  sobreescritura silenciosa / clobber  → INVALID (misma clase que generations)
```

`admitted_role` existe para distinguir *qué* rol se le admitió al Clone (p. ej.
candidato a activación vs. materialización), y **no** para autorizar por sí mismo.

### 34.11 D0-9 — `RUNTIME_SETUP_ARTIFACT_AVAILABILITY` (decisión)

Opciones analizadas, **ninguna implementada**:

| Opción | Qué promete | Costo | Veredicto |
|---|---|---|---|
| A — readquisición externa | "recordamos la fuente" | barato | Insuficiente: 2/5 releases SKSE tienen `artifact_name=None` |
| B — payload retenido | "guardamos el artefacto" | disco | Único que sostiene "rollback garantizado" |
| C — cache content-addressed | "guardamos por hash" | disco + índice | Mejor que B para dedupe; mismo costo base |
| D — híbrido | depende del artefacto | complejo | Preferido a largo plazo |

```text
MVP_PROMISE = "manifest válido + artefacto disponible en el momento de la promoción"
ROLLBACK_GUARANTEED_SEMANTICS = NO se promete con A
RUNTIME_SETUP_ARTIFACT_AVAILABILITY = KEEP_OPEN
BLOCKS = { prometer "rollback garantizado" cuando el payload no está retenido }
```

**Lo que bloquea exactamente:** no hay evidencia todavía de cuánto payload hay que
retener por caso, ni presupuesto de disco aceptado. Mientras eso no se decida, P4
puede implementarse fail-closed: si el artefacto no está disponible al momento de
preparar la activación, **no se activa**. Eso **no** bloquea P4; bloquea el *claim*
"rollback garantizado".

### 34.12 D0-10 — `MANDATORY_CRITICAL_EXPECTATIONS` (diseño)

**Evidencia dura.** `golden.py::verify_critical_files` itera `for exp in
expectations:` ⇒ lista vacía ⇒ evidencia vacía; `todos_verified = tree and runtime and
all(...)` ⇒ `all(()) == True` ⇒ VERIFIED **sin evidencia crítica**. `clone.py` y
`golden.py` usan `critical_expectations=()` por defecto. `observation.py` tiene **una
sola** fuente de verdad crítica: `CRITICAL_EXE_BY_GAME = {"skyrimse": "SkyrimSE.exe"}`.

```text
missing != empty-valid      (contrato, no accidente de serialización)
EMPTY_EXPECTATIONS ⇒ RECHAZO  (no ⇒ VERIFIED)
```

**Quién produce las expectativas.** La Generation (contenido-bound, ya sellado por
P2/P3) es la fuente: los archivos críticos declarados por la Generation son los que el
Clone debe preservar. **NO** el Candidate sobre sí mismo (SFR-15).

```text
EXPECTATIONS_PRODUCER   = Generation verificada
EXPECTATIONS_SCHEMA     = mismo digest canónico que critical_expectations_digest
EXTENSIBLE              = sí (skyrimse hoy: SkyrimSE.exe; el catálogo puede crecer)
VACÍO_SIGNIFICA         = "no sé"  ⇒ fail-closed
```

`critical_expectations.py` hoy **digiere felizmente** `[]`; por eso la barrera debe
ser un **contrato de P4**, no un efecto de la serialización.

### 34.13 D0-11 — `POST_ACTIVATION_LINK_INJECTION` (diseño)

**Evidencia.** `clone.py::verify_physical_independence` itera sólo `for f in files`
(el inventario sellado) ⇒ un hardlink/junction/symlink/reparse insertado **después**
de admitir el Clone, o fuera de esa lista, queda fuera de alcance. La cobertura de
enlaces más fuerte que existe (`independence.py`) está aplicada a la **Generation**, no
al Clone.

```text
RESPONSABLE = PRIMARIO: P4 (gate de activación) · SECUNDARIO: P5 (oráculo de Effective)
CUÁNDO      = PRE-approval (evidencia) + PRE-bind (reverify §34.7)
              + POST-bind (verify de coherencia) + ARRANQUE (§34.5)
ESCANEO_INFINITO = PROHIBIDO
```

No es un escaneo continuo: son **puntos de control acotados**, y cualquier reparse
**nuevo** invalida. **Corregido en §35.4 (D0-R3):** derivar a P5 un enlace fuera del
inventario **no alcanza** —el escaneo debe enumerar el **namespace actual** del Clone
(archivos **y** directorios), porque un junction creado dentro del Clone tras aprobar
no es una superficie del game path ni responsabilidad del binder. La ventana residual
entre el último checkpoint y la ejecución se **declara**, no se esconde.

### 34.14 D0-12 — `P4_P5_COMPATIBILITY_GATE` (diseño)

```text
COMPATIBILITY ∈ { COMPATIBLE, INCOMPATIBLE, UNKNOWN }
UNKNOWN != COMPATIBLE     (invariante)
```

| Condición | Quién evalúa | Resultado si no se puede evaluar |
|---|---|---|
| Generation `VALID` | P4 | INCOMPATIBLE / INDETERMINATE → NO ACTIVATION |
| Clone `lifecycle == PROVISIONED` | P4 | NO ACTIVATION |
| setup `verified == intended` | P4 | NO ACTIVATION |
| independencia física del Clone | P4 | NO ACTIVATION |
| archivos críticos presentes | P4 | NO ACTIVATION |
| SKSE/game-version match | **P5** | UNKNOWN → **NO ACTIVATION** |
| Effective Runtime real (oráculo) | **P5** | UNKNOWN → **NO ACTIVATION** |

```text
P4_CANNOT_ACTIVATE_BECAUSE_P5_DOES_NOT_KNOW = CERRADO por diseño
DELEGATED_TO_P5 = { skse/game match, effective runtime oracle }
P5_IMPLEMENTED = NO
```

P4 **no** espera a P5 para **poder implementarse y testearse**; pero **corregido en
§35.7 (D0-R6):** mientras P5 devuelva `UNKNOWN`, P4 **no puede completar la activación
ni finalizar con SUCCESS**. Es la diferencia entre "no sé ⇒ no" y "no sé ⇒ sí".

### 34.15 Matriz de crashes (obligatoria) — C0..C7

Durable = `transition.json` (+ `active.json`). Effective = lo que consume el runtime.

| Punto | Durable | Effective posible | Acción al arrancar | Recuperación automática | Dueño |
|---|---|---|---|---|---|
| **C0** antes de persistir PENDING | `NONE` | el original | `COMPLETE` | — | no |
| **C1** tras PENDING, antes de bind | `PENDING` | el original | re-verificar objetivo → `RECOVER` (re-bind idempotente) o `REQUIRE_OWNER` si cambió evidencia | sí, si la evidencia re-verifica | solo si cambió |
| **C2** durante el bind | `PENDING` | **indeterminado** (puede estar a medias) | **primero observar** el Effective; nunca asumir | `RECOVER` si observa objetivo+evidencia OK; si no, `FAIL_CLOSED` | si no se puede observar |
| **C3** tras bind, antes de actualizar desired | `PENDING` | el objetivo | **CORREGIDO en §35.3**: persistir `Desired=target` **antes** del POST (con `Desired=source` el POST no puede pasar); luego POST → `FINALIZE` | sí | no |
| **C4** tras desired, antes de POST | `PENDING` | el objetivo | re-correr POST | sí | no |
| **C5** POST falla | `PENDING` (sigue) | el objetivo, **no verificado** | **NO FINALIZA**; `REQUIRE_OWNER` o `RECOVER` | no (fail-closed) | sí |
| **C6** POST pasa, antes de FINALIZE | `PENDING` | el objetivo, verificado | re-correr POST (idempotente) → `FINALIZE` | sí | no |
| **C7** tras FINALIZE | `FINALIZED` | el objetivo | `COMPLETE` | — | no |

```text
FINALIZED_BEFORE_POST = PROHIBIDO en todos los puntos (§33.2)
C2_ASSUMPTION = NINGUNA  ("rollback si hace falta" está prohibido como frase y como diseño)
```

### 34.16 Revisión adversarial local (12 preguntas)

| # | Pregunta | Estado | Fundamento |
|---|---|---|---|
| 1 | ¿Dos procesos pueden iniciar Promoción/Rollback a la vez? | **CLOSED_BY_DESIGN** | Un lock root-keyed (§34.3), exclusión para ambos |
| 2 | ¿Un crash tras el bind deja estado determinísticamente recuperable? | **CLOSED_BY_DESIGN** | C3/C4/C6 → POST idempotente; C2 → observar antes de decidir |
| 3 | ¿El arranque distingue transición incompleta de corrupción? | **CLOSED_BY_DESIGN** | `PENDING` ≠ `FAIL_CLOSED`; corrupto/desconocido ⇒ `FAIL_CLOSED` |
| 4 | ¿La aprobación identifica contenido mutable exacto? | **CLOSED_BY_DESIGN** | `clone_evidence`/`runtime_setup_evidence` como digests sellados (§34.6) |
| 5 | ¿Un cambio tras aprobar puede escapar del reverify? | **CLOSED_BY_DESIGN** | `ANY MATERIAL CHANGE ⇒ NO BIND`; ventana TOCTOU declarada |
| 6 | ¿Metadata adulterada accidentalmente puede volverse autoridad? | **CLOSED_BY_DESIGN** | §34.8/§34.10 fail-closed; `UNKNOWN != VALID` |
| 7 | ¿Un manifiesto válido sin payload puede prometer rollback? | **CLOSED_BY_DESIGN (frase) / DEFERRED_FAIL_CLOSED (garantía)** | §34.9; la garantía queda KEEP_OPEN, P4 no activa sin payload |
| 8 | ¿`critical_expectations` vacío puede pasar? | **CLOSED_BY_DESIGN** | `missing != empty-valid`; vacío ⇒ RECHAZO |
| 9 | ¿Un hardlink/junction insertado tras admitir el Clone escapa? | **DEFERRED_FAIL_CLOSED** | §34.13: puntos de control acotados; ventana residual declarada |
| 10 | ¿P4 puede activar si P5 devuelve `UNKNOWN`? | **CLOSED_BY_DESIGN** | `UNKNOWN != COMPATIBLE` (§34.14) |
| 11 | ¿Se puede reconstruir el target histórico exacto sin adivinar? | **CLOSED_BY_DESIGN** | `previous_activation_target` durable (R4-F10), publicado **sólo** al FINALIZE con el registro de transición finalizado como autoridad única (§36.2/§36.8) + `target_*` en la transición |
| 12 | ¿Existe una ruta donde el Clone mutable reemplace a Generation+Setup como autoridad? | **CLOSED_BY_DESIGN** | §32.2 autoridades separadas; §34.6 evidencia de contenido; §33.3 Generation no VALID ⇒ rechazo |

### 34.17 Resultado del diseño

| BLOCKER | EVIDENCE | DECISION | DESIGN STATUS | IMPLEMENTATION STATUS | TARGET PHASE |
|---|---|---|---|---|---|
| `P4_CROSS_PROCESS_LOCK` | `runtime_vault/locking.py` (destino-keyed, `%TEMP%`, sin dueño) | lock root-keyed, token+lease, fail-closed | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `P4_DURABLE_TRANSITION` | `state.py` (sin Effective); `candidates.py` precedente | `transition.json` v1, persist-before-bind | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `P4_STARTUP_RECONCILIATION` | `recovery.md`, ADR 0007 | `COMPLETE/RECOVER/REQUIRE_OWNER/FAIL_CLOSED` | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `P4_APPROVAL_SCOPE` | `critical_expectations.py`, `membership.py` | operation-aware + evidencia de contenido | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `P4_REVERIFY_AFTER_APPROVAL` | `observar_arbol_sellado` | aprobación == pre-bind fresco | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `GENERATION_METADATA_INTEGRITY` | `generations.py` (sin sello) | threat model accidental; sin HMAC | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `RUNTIME_SETUP_MANIFEST_INTEGRITY` | 0 hits `runtime_setup` | 3 problemas separados; qué sella | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `RUNTIME_CLONE_RECORD_INTEGRITY` | 0 hits `RuntimeCloneRecord` | FSM + fail-closed (6 reglas) | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `MANDATORY_CRITICAL_EXPECTATIONS` | `golden.py:120`, `clone.py:200`, `all(())==True` | `missing != empty-valid`; productor = Generation | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `POST_ACTIVATION_LINK_INJECTION` | `clone.py` itera sólo `files` | puntos acotados P4(+P5), sin escaneo infinito | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4/P5 |
| `P4_P5_COMPATIBILITY_GATE` | 0 hits gate | `UNKNOWN != COMPATIBLE`; delegación explícita | **DESIGN_CLOSED** | IMPLEMENTATION_OPEN | P4 |
| `RUNTIME_SETUP_ARTIFACT_AVAILABILITY` | `skse_catalog.py` (2/5 `artifact_name=None`) | opciones A–D; MVP promise acotada | **KEEP_OPEN** | IMPLEMENTATION_OPEN | P4/P5 |
| `CREATION_CLUB_CLASSIFICATION` | — | no decidir por intuición | **DEFERRED_PENDING_EVIDENCE** | IMPLEMENTATION_OPEN | P5 |
| `P3B` (`StateFlags 6`/`518`) | `provider_signals.py:37,66` | no hardcodear `6`/`518` | **DEFERRED_PENDING_RIG** | IMPLEMENTATION_OPEN | P3b |

### 34.18 Estado

```text
TRACKER_672_AUDITED          = YES
P4_READY_TO_DESIGN           = YES
P4_DESIGN_FROZEN             = NO   (SUPERSEDED dos veces: ronda 1 (§35) y ronda 2 (§36); estado vigente en §36.15)
P4_READY_TO_IMPLEMENT        = NO   (SUPERSEDED: ver §36.15)
P4_IMPLEMENTED               = NO
P5_IMPLEMENTED               = NO

CROSS_PROCESS_LOCK_PRIMITIVE_REUSE = NO   (principio sí, primitiva no)
ROLLBACK_VETO_REUSE                = NO   (principio sí, acoplamiento in-memory no)
ATOMIC_WRITE_REUSE                 = YES  (write_json_atomic + 'x')

CREATION_CLUB_CLASSIFICATION = DEFERRED_PENDING_EVIDENCE
P3B                          = DEFERRED_PENDING_RIG
STATEFLAGS_6_518_SEMANTICS   = UNVERIFIED
```

**Criterio de `P4_READY_TO_IMPLEMENT = YES`.** *(SUPERSEDED por §35.5 y §35.13: este
criterio enumeraba sólo 10 blockers y omitía 5 requisitos P4 registrados en §29.8. El
criterio vigente exige **todos** los requisitos P4 conocidos, `DESIGN_CLOSED` o
demostrablemente `DEFERRED_FAIL_CLOSED`.)* Los diez blockers que enumeraba están
`DESIGN_CLOSED`: `P4_CROSS_PROCESS_LOCK`, `P4_DURABLE_TRANSITION`,
`P4_APPROVAL_SCOPE`, `P4_REVERIFY_AFTER_APPROVAL`, `P4_P5_COMPATIBILITY_GATE`,
`GENERATION_METADATA_INTEGRITY`, `RUNTIME_SETUP_MANIFEST_INTEGRITY`,
`RUNTIME_CLONE_RECORD_INTEGRITY`, `MANDATORY_CRITICAL_EXPECTATIONS`,
`POST_ACTIVATION_LINK_INJECTION`. Los dos que quedan abiertos **no** impiden una
implementación fail-closed:

- `RUNTIME_SETUP_ARTIFACT_AVAILABILITY`: P4 no activa si el payload no está
  disponible; sólo bloquea el *claim* "rollback garantizado".
- `P3B` / `CREATION_CLUB_CLASSIFICATION`: P4 responde `UNKNOWN → NO ACTIVATION`.

> `P4_READY_TO_IMPLEMENT = YES` significa **"el diseño no bloquea"**, no "P4 existe".
> `P4_IMPLEMENTED = NO` sigue siendo el estado real.

**Alcance de esta ronda:** docs-only. `PRODUCT_CODE_CHANGED = NO`. No se implementó
transición, ni lock cross-process, ni `active.json` real, ni binding de MO2, ni setup
de SKSE, ni cache de artefactos, ni rollback, ni promoción. No se tocó P5. No se
mergea el Draft PR.

---

## 35. P4-D0 — ronda adversarial 1: correcciones contractuales (2026-10-08)

Revisión adversarial del Tech Lead sobre `81fb83a2`. **6 findings, todos `CONFIRMED`**,
verificados contra el texto y contra el código antes de adjudicarlos. Esta sección
**corrige** §34; no implementa nada.

> **Supersesión parcial (ronda adversarial 2, §36).** La revisión externa sobre
> `589805ee` encontró **6 residuos nuevos** en los contratos que esta sección dejó
> abiertos. Esta sección sigue siendo el registro de la ronda 1, pero queda
> **corregida in-place** por §36 en: `previous_activation_target` (§35.8.5 — se le
> agrega la autoridad única), cancelación y provisioning (§35.8.1/§35.8.2/§35.8.4 —
> semántica determinista) y el censo (§35.5 — desglose aritmético). **§35.13 ya no es
> el estado vigente: el estado es el de §36.15.**

### 35.1 Adjudicación

| Finding | Sev. | Adjudicación | Resolución |
|---|---|---|---|
| **D0-R1** Aprobación liga el target pero no el **source** que reemplaza | P1 | **CONFIRMED** | §35.2: el `ApprovalScope` liga `source_activation`; la igualdad `aprobado == actual` se comprueba **bajo el lock** antes de persistir `PENDING_*`. Single-use, expiración e identidad del propietario adjudicadas. |
| **D0-R2** C3 tiene un **ordering imposible** | P1 | **CONFIRMED** | Con `Desired=source` / `Effective=target` el POST de coherencia no puede pasar. §35.3 corrige C3 (persistir `Desired=target` **antes** del POST) y rehace la reconciliación de arranque. |
| **D0-R3** La inyección de enlaces post-activación **sigue abierta** | P1 | **CONFIRMED** | Derivar a P5 un enlace fuera del inventario no alcanza. §35.4 exige **escaneo fresco del namespace actual** (archivos **y** directorios). |
| **D0-R4** La readiness **omite blockers P4** | P1 | **CONFIRMED** | §34.17 adjudicaba 12 de **17** requisitos P4 de §29.8. §35.5 hace el censo completo y rediseña los 5 faltantes (§35.8). |
| **D0-R5** `FINALIZED` no conserva **prueba de finalización** | P2 | **CONFIRMED** | §35.6 exige evidencia durable del POST y regla anti-clobber (CAS por `transition_id`). |
| **D0-R6** Wording P4↔P5 demasiado fuerte | P2 | **CONFIRMED** | §35.7 separa "P4 se puede implementar" de "la activación puede completarse". |

```text
D0_R1_APPROVAL_SOURCE_BINDING        = CONFIRMED / CLOSED (§35.2)
D0_R2_CRASH_C3_ORDERING              = CONFIRMED / CLOSED (§35.3)
D0_R3_FRESH_CLONE_NAMESPACE_INTEGRITY= CONFIRMED / CLOSED (§35.4)
D0_R4_ALL_P4_BLOCKERS_CENSUSED       = CONFIRMED / CLOSED (§35.5, §35.8)
D0_R5_FINALIZATION_EVIDENCE_AND_CAS  = CONFIRMED / CLOSED (§35.6)
D0_R6_P4_P5_WORDING                  = CONFIRMED / CLOSED (§35.7)
```

### 35.2 D0-R1 — `ApprovalScope` liga el source activation (replay / TOCTOU)

**El defecto.** §34.6 ligaba con precisión el **target** (`generation_id`, `clone_id`,
`clone_evidence`, `runtime_setup_id`, `runtime_setup_evidence`,
`compatibility_evidence_id`, `candidate_id`) pero **no** el runtime **saliente**. Un
flujo concurrente que cambiara el source entre la aprobación y el bind dejaría pasar
una aprobación obtenida en otro contexto, con el target intacto.

**Representación (auditada contra el repo, no copiada del enunciado).** Hoy
`active.json` **v1** sólo tiene `{schema_version, desired_active_generation,
updated_at_ns}` y su docstring declara que **no** registra Effective Runtime; ni
`clone_id` ni `runtime_setup_id` viven ahí, y `previous_activation_target` tiene
**0 ocurrencias** en código. El source es entonces una **tupla derivada**, y su
identidad se sella como **digest de contenido** —el patrón del repo
(`generation-id`, `TreeDigest`, `critical_expectations_digest`), no un contador ni un
timestamp:

```text
source_activation = {
    generation_id,          # desired_active_generation (active.json) — existe hoy
    clone_id,               # target activo (RuntimeCloneRecord) — diseño v2
    runtime_setup_id,       # verified_runtime_setup_id del Clone activo — diseño v2
}
source_activation_digest = SHA-256(canonical_json(source_activation))
```

**Por qué digest y no `updated_at_ns`.** Un reloj de pared no es una revisión: dos
estados distintos pueden compartir timestamp (resolución) y una reescritura con
contenido idéntico lo cambia (falso positivo). La identidad content-bound es
**estrictamente más fuerte** para este threat model.

**Contrato.**

```text
ApprovalScope {
  operation                 PROMOTION | ROLLBACK
  source_activation         { generation_id, clone_id, runtime_setup_id }
  source_activation_digest
  target_generation_id
  target_clone_id
  target_runtime_setup_id
  candidate_id              (PROMOTION: id; ROLLBACK: NOT_APPLICABLE)
  compatibility_evidence_id
  clone_evidence            (digest de contenido, no ruta)
  runtime_setup_evidence    (digest de contenido)
}
```

```text
CHECK = (approved source_activation_digest == current source_activation_digest)
WHEN  = UNDER_THE_P4_CROSS_PROCESS_LOCK, inmediatamente ANTES de persistir
        PENDING_PROMOTION | PENDING_ROLLBACK
SI CAMBIA:
    APPROVAL INVALIDATED · NO PENDING · NO BIND · NEW APPROVAL REQUIRED
```

**Single-use.**

```text
APPROVAL_SINGLE_USE = YES
CONSUMO      = crear una transición con estado PENDING_* registra consumed_by_transition_id
REUSO        = una aprobación NO puede crear una segunda transición (otro transition_id)
CONTINUACIÓN = reanudar la MISMA transición (recovery C1/C3/C4/C6) está AUTORIZADO
               (es continuación, no replay: mismo transition_id, mismo source, mismo target)
```

La transición liga la aprobación a `{transition_id, approval_scope_digest,
source_activation, target_evidence}`.

**Expiración.**

```text
TIME_BASED_EXPIRATION_REQUIRED = NO
INVALIDACIÓN = cualquier cambio de source_activation O de target evidence
JUSTIFICACIÓN = el riesgo real es "el mundo cambió bajo la aprobación", y eso lo atrapa
                exactamente la ligadura source+target. Un TTL por reloj o bien expira
                aprobaciones todavía válidas (fuerza re-aprobación sin ganancia de
                seguridad) o bien es demasiado largo para importar.
ADITIVO = un TTL puede agregarse después sin debilitar este contrato.
```

**Identidad del propietario — no fingir una seguridad que el producto no tiene.**

```text
APPROVAL_AUTHORITY   = la superficie HITL del producto (quién dijo que sí)
APPROVAL_PROVENANCE  = referencia opaca que viaja en el scope (superficie + id de solicitud si existe)
LOCK_OWNERSHIP       = el session_id del lock P4 (proceso, NO identidad humana)
lock_session_id == human_authorization_identity  ⇒ NO (no demostrado)
OWNER_IDENTITY_BINDING = DEFERRED
```

`§29.8` ya registra que la capa del agente LLM es lock-only y que el HITL de la GUI
documenta una solicitud sin pestaña lanzadora **sin dueño**. P4 **no** equipara
propiedad del lock con autorización humana: la protección anti-replay es
**single-use + ligadura de source**, no una identidad que el producto todavía no
emite. `P4_APPROVAL_SCOPE_DESIGN = CLOSED` **sólo** con single-use y expiración
adjudicadas, que es lo que esta sección hace.

### 35.3 D0-R2 — C3 corregido + reconciliación de arranque

**El defecto.** §34.15 decía: `PENDING / bind hecho / desired anterior → re-run POST →
FINALIZE`. Si `Desired=source` y `Effective=target`, el POST de coherencia
(SFR-16) **no puede pasar**. El ordering era imposible.

**C3 corregido.**

```text
PENDING · Desired=source · Effective=target
  → assert same transition_id (CAS, §35.6)
  → re-verificar evidencia fresca de source Y target
  → persistir Desired = target              (prerequisito de SFR-16)
  → POST verify Desired == Effective
  → PASS: FINALIZE atómico con previous_activation_target = source_activation
          (tomado del registro de transición) · SUCCESS
  → FAIL: sigue PENDING · NO FINALIZE
```

`previous_activation_target` se escribe **con** el FINALIZE (§35.8.5); hasta entonces
`source_activation` vive en el registro `PENDING`, que es lo que permite reconstruirlo.

**Reconciliación de arranque (§34.5 corregido).** No alcanza con "`Effective ==
expected_effective_path` → POST". Hay que observar **por separado** `transition state`,
`Desired`, `Effective`, `source activation` y `target evidence`, y decidir en qué fase
real quedó:

| Desired | Effective | Fase real | Acción | Automático | Dueño | ¿Puede finalizar? |
|---|---|---|---|---|---|---|
| — (no hay transición) | coherente | limpio (**N0**) | `COMPLETE` | — | no | — |
| — (no hay transición) | **Desired != Effective** | **`INCONSISTENT_BASELINE` (N1)** | `FAIL_CLOSED` · `REQUIRE_OWNER`/recovery; **no** se fabrica `PENDING`, **no** se elige Desired ni Effective (§36.4) | no | sí | no |
| source | source | bind no aplicado (C0/C1) | re-bind si: aprobación válida + source matchea + target evidence fresca + lock re-adquirido | sí si todo matchea | sólo si no matchea | sí, tras bind + POST |
| source | target | bind aplicado, desired atrás (C3) | completar hacia adelante: persistir Desired, POST, finalize | sí | no | sí si POST pasa |
| target | target | desired y effective alineados (C4/C6) | re-correr POST (idempotente) | sí | no | sí si POST pasa y mismo `transition_id` |
| target | source | desired avanzado sin bind | **NO finalizar**; `RECOVER` (re-aplicar bind) o `REQUIRE_OWNER` | no si la evidencia no matchea | sí | no |
| cualquiera | `UNKNOWN` | no observable | `FAIL_CLOSED` | no | sí | no |
| `PENDING` con `transition.json` corrupto | — | indeterminado (**N2**) | `FAIL_CLOSED` — **nunca** se lee como `NONE` | no | sí | no |

```text
INCOMPLETE_TRANSITION_IS_AUTOMATICALLY_REVERTED = NO
NO_INVENTAR_EXITO = YES   (ningún camino asume target sin observarlo)
```

### 35.4 D0-R3 — integridad física del Clone: escaneo fresco del namespace

**El defecto.** §34.13 decía que un enlace **fuera del inventario sellado** era
responsabilidad del bind de P5. Falso: un junction creado *dentro* del Clone después de
aprobar (p. ej. `Data\SomeMod → junction`) no es una superficie del game path ni
responsabilidad conceptual del binder.

**Contrato nuevo.**

```text
FRESH_NAMESPACE_SCAN = YES
SCOPE   = FILES + DIRECTORIES del namespace ACTUAL del Clone
          (NO `for file in old_inventory`)
DETECTA = symlink · junction · reparse point · hardlink prohibido ·
          namespace escape · solapamiento con Managed Source
NO_PROMETE_VIGILANCIA_CONTINUA = YES
```

**Primitivas existentes (reutilizar, no reinventar):**

| Qué | Primitiva | Estado |
|---|---|---|
| Archivos | `independence._archivos_fisicos` — rechaza `is_symlink()` y reparse (`st_file_attributes & 0x400`), exige `st_dev>0` y `st_ino>0` | existe (hoy aplicada a la **Generation**) |
| Directorios | `membership.capturar_membership_directorios` — "un symlink/junction/reparse dentro del scope **no se sigue ni se ignora**"; `scandir` + re-validación por identidad | existe |
| Contención | `independence.exigir_contencion_fisica` / `storage._contenida` | existe |
| Solapamiento | `independence.verify_generation_independence` (ambas direcciones) | existe, scoped a Generation |

El escaneo del Clone **combina** las dos primeras (archivos **y** directorios) y les
suma contención y solapamiento. Ninguna pieza es nueva; la novedad es aplicarlas al
**Clone** y al **namespace actual**.

**Puntos de control (adjudicados, no todos redundantes):**

```text
PRE-approval = OBLIGATORIO   la evidencia que el usuario aprueba debe reflejar el namespace actual
PRE-bind     = OBLIGATORIO   carga la garantía: es la ventana aprobación→bind
POST-bind    = OBLIGATORIO   el árbol bindeado es el que se ejecuta
startup      = OBLIGATORIO   reconciliación (§35.3)
REDUNDANTE   = NINGUNO       PRE-approval y PRE-bind difieren por la ventana aprobación→bind
```

```text
P5_BINDING_CLASSES != CLONE_FILESYSTEM_INTEGRITY
```

Son **responsabilidades distintas**: el binder de P5 repunta superficies del game path
(§29.4); la integridad física del Clone es del gate de P4. Que P5 repunte bien **no**
dice nada sobre si hay un junction dentro del árbol.

### 35.5 D0-R4 — censo completo de requisitos P4

**El defecto.** §34.17 adjudicaba 12 requisitos. §29.8 registra **17**. Faltaban
`P4_LONG_RUNNING_CANCELLATION`, `P4_RUNTIME_SETUP_PROVISIONING`,
`P4_CLONE_ACTIVATION_GATE`, `P4_CLONE_PROVISIONING_LIFECYCLE` y
`P4_ROLLBACK_TARGET_HISTORY`. "No estaba en la lista de 10" **no** es justificación.

**Censo reproducible.**

```bash
grep -oE "P4_[A-Z0-9_]+" docs/adr/0012-frozen-runtime.md | sort -u
```

Sobre el ADR **completo tras §35** devuelve **36 símbolos**: **13 nombres de
requisito con prefijo `P4_`** más **23 indicadores de estado / etiquetas de
adjudicación** (`P4_READY_TO_DESIGN`, `P4_READY_TO_IMPLEMENT`, `P4_DESIGN_FROZEN`,
`P4_IMPLEMENTED`, `P4_DESIGN_BLOCKERS`, `P4_REQUIREMENTS_DISCOVERED`,
`P4_REQUIREMENTS_DESIGN_CLOSED`, `P4_REQUIREMENTS_DEFERRED_FAIL_CLOSED`,
`P4_REQUIREMENTS_OPEN`, `P4_APPROVAL_SCOPE_DESIGN`,
`P4_APPROVAL_SCOPE_IMPLEMENTATION`, `P4_REVERIFY`, `P4_REVERIFY_DESIGN`,
`P4_REVERIFY_IMPLEMENTATION`, `P4_CORE_MAY_BE_IMPLEMENTED_BEFORE_P5`,
`P4_CANNOT_ACTIVATE_BECAUSE_P5_DOES_NOT_KNOW`, `P4_BLOCKERS_CENSUSED`,
`P4_P5_WORDING`, y los `P4_*_DESIGN` de los cinco requisitos diseñados en §35.8).
El conteo **no** se toma como lista de requisitos: la lista de **requisitos** se
normaliza contra §29.8 + §32.6 + §34 y es la de la tabla de abajo (**17**, de los
cuales **13** llevan prefijo `P4_` y **4** no: `GENERATION_METADATA_INTEGRITY`,
`RUNTIME_SETUP_MANIFEST_INTEGRITY`, `MANDATORY_CRITICAL_EXPECTATIONS` y
`POST_ACTIVATION_LINK_INJECTION`). El desglose `13 + 23 = 36` **cierra**; el texto
anterior a esta corrección decía "17 requisitos más 19 indicadores", que sumaba 36
por coincidencia pero describía mal ambos sumandos (D0-R2.8). El conteo está
**scopeado a la revisión**: agregar símbolos `P4_` nuevos lo mueve, así que el
número vigente se recomputa con el comando de arriba.

| # | P4 REGISTERED REQUIREMENT | §29 STATUS | §34 DECISIÓN | ¿BLOQUEA IMPLEMENTACIÓN? | DESIGN STATUS |
|---|---|---|---|---|---|
| 1 | `P4_CROSS_PROCESS_LOCK` | OPEN (ampliado) | §34.3 | no | **DESIGN_CLOSED** |
| 2 | `P4_LONG_RUNNING_CANCELLATION` | OPEN (nuevo) | **§35.8.1** | no | **DESIGN_CLOSED** |
| 3 | `P4_DURABLE_TRANSITION` | OPEN (ampliado) | §34.4 + §35.6 | no | **DESIGN_CLOSED** |
| 4 | `P4_APPROVAL_SCOPE` | DESIGN CLOSED / IMPL OPEN | §34.6 + **§35.2** | no | **DESIGN_CLOSED** |
| 5 | `P4_RUNTIME_SETUP_PROVISIONING` | OPEN (nuevo) | **§35.8.2** | no | **DESIGN_CLOSED** |
| 6 | `P4_RUNTIME_SETUP_ARTIFACT_AVAILABILITY` | OPEN | §34.11 | no (P4 no activa sin payload) | **DEFERRED_FAIL_CLOSED** |
| 7 | `P4_CLONE_ACTIVATION_GATE` | OPEN (nuevo) | **§35.8.3** | no | **DESIGN_CLOSED** |
| 8 | `P4_CLONE_PROVISIONING_LIFECYCLE` | OPEN (nuevo) | **§35.8.4** | no | **DESIGN_CLOSED** |
| 9 | `P4_ROLLBACK_TARGET_HISTORY` | OPEN (nuevo) | **§35.8.5** | no | **DESIGN_CLOSED** |
| 10 | `P4_RUNTIME_CLONE_RECORD_INTEGRITY` | OPEN | §34.10 | no | **DESIGN_CLOSED** |
| 11 | `P4_STARTUP_RECONCILIATION` | (§34) | §34.5 + **§35.3** | no | **DESIGN_CLOSED** |
| 12 | `P4_REVERIFY_AFTER_APPROVAL` | (§29.8) | §34.7 | no | **DESIGN_CLOSED** |
| 13 | `P4_P5_COMPATIBILITY_GATE` | (§34) | §34.14 + **§35.7** | no | **DESIGN_CLOSED** |
| 14 | `GENERATION_METADATA_INTEGRITY` | §32.6 OPEN | §34.8 | no | **DESIGN_CLOSED** |
| 15 | `RUNTIME_SETUP_MANIFEST_INTEGRITY` | §32.6 OPEN | §34.9 | no | **DESIGN_CLOSED** |
| 16 | `MANDATORY_CRITICAL_EXPECTATIONS` | §32.6 OPEN | §34.12 | no | **DESIGN_CLOSED** |
| 17 | `POST_ACTIVATION_LINK_INJECTION` | §32.6 OPEN | §34.13 + **§35.4** | no | **DESIGN_CLOSED** |

```text
P4_REQUIREMENTS_DISCOVERED          = 17
P4_REQUIREMENTS_DESIGN_CLOSED       = 16
P4_REQUIREMENTS_DEFERRED_FAIL_CLOSED= 1   (RUNTIME_SETUP_ARTIFACT_AVAILABILITY)
P4_REQUIREMENTS_OPEN                = 0
```

**Fuera de alcance P4 (P5/P6, no cuentan para readiness de P4):**
`P5_EFFECTIVE_RUNTIME_ORACLE`, `P5_PATH_SURFACES_UNIT`, `P5_QUIESCENCE`,
`P6_STEAM_STATUS_NOTICE`. Se registran como `OUT_OF_SCOPE_P5`.

**Regla de readiness (corregida).**

```text
P4_READY_TO_IMPLEMENT = YES  sólo si TODOS los requisitos P4 conocidos están
                             DESIGN_CLOSED, o demostrablemente DEFERRED_FAIL_CLOSED
                             Y no requeridos para un P4 core seguro.
```

`RUNTIME_SETUP_ARTIFACT_AVAILABILITY` cumple: sin payload P4 **no activa**; lo que
queda sin prometer es la *garantía de rollback*, no el core. `CREATION_CLUB_CLASSIFICATION`
(`DEFERRED_PENDING_EVIDENCE`) y `P3B` (`DEFERRED_PENDING_RIG`) tampoco bloquean: P4
responde `UNKNOWN → NO ACTIVATION`.

### 35.6 D0-R5 — evidencia durable de finalización + no-clobber (CAS)

**El defecto.** `state = FINALIZED` no llevaba prueba: la garantía
`FINALIZED ⇒ POST passed` dependía sólo del **orden procedural**.

**Evidencia durable mínima del POST exitoso:**

```text
post_verify_evidence_digest     digest del resultado de la verificación de coherencia
post_verified_at_ns             entero >= 0
finalized_from_transition_id    la transición que se finalizó
```

```text
INVARIANTE = FINALIZED no puede existir sin evidencia durable que ligue, al MISMO:
             { transition_id, source, target, desired state, effective observation }
```

**No-clobber / CAS.**

```text
expected_transition_id == current_transition_id   ANTES de toda mutación del journal
SI NO COINCIDE ⇒ FAIL_CLOSED
```

Una transición vieja **no puede** finalizar una más nueva. El CAS se exige en cada
mutación de `transition.json` (crear PENDING, actualizar, finalizar). No se implementa
acá; el **contrato** queda congelado.

### 35.7 D0-R6 — P4↔P5: separar implementación de activación

**El defecto.** "P4 no espera a P5 para funcionar" era ambiguo: confundía *poder
desarrollar y testear P4* con *poder completar la activación*.

```text
P4_CORE_MAY_BE_IMPLEMENTED_BEFORE_P5 = YES   (desarrollo y test del core P4)
ACTIVATION_COMPLETION_REQUIRES_P5    = YES   (compatibilidad + evidencia de Effective Runtime)

MIENTRAS P5 = UNKNOWN, P4 PUEDE:
    preparar · validar lo que P4 posee · persistir artefactos NO activos
MIENTRAS P5 = UNKNOWN, P4 NO PUEDE:
    completar la activación · finalizar con SUCCESS
UNKNOWN != COMPATIBLE   (se mantiene)
```

Es la diferencia entre "no sé ⇒ no activo" y "no sé ⇒ activo igual".

### 35.8 Requisitos P4 faltantes — diseño

#### 35.8.1 `P4_LONG_RUNNING_CANCELLATION`

`frozen_runtime` es 100 % síncrono (E9). Requisito: fachada async con executor
dedicado, token de cancelación cooperativo, progreso hacia el event loop, single-flight
por root.

```text
CANCELACIÓN_COOPERATIVA = YES (token)
RESULTADO_DE_OPERACIÓN_NUNCA_AMBIGUO = YES → SUCCESS | CANCELLED | FAILED,
    jamás un BUILDING huérfano   (corregido en D0-R2.6: `CANCELLED` NO es un estado
    del ciclo de vida del Clone; ver §36.7)
CLONE_LIFECYCLE_NO_ADMITE_CANCELLED = YES  (CREATED | PROVISIONING | PROVISIONED | INVALID)
LOCK_SE_CONSERVA_MIENTRAS_HAYA_MUTADOR = YES
CANCELACIÓN_DURANTE_TRANSICIÓN != ROLLBACK EXITOSO = YES
    (cancelar NO simula un rollback exitoso; no toca el Effective Runtime a medias)
PENDING_DURABLE_GOBIERNA_RECOVERY = YES
    (si la transición ya se persistió, la cancelación la deja PENDING para §35.3;
     NO la borra para "quedar limpio")
REFACTOR_ASYNCIO_REQUERIDO = NO
IMPLEMENTATION = slice dentro de P4 (no bloquea el core)
```

#### 35.8.2 `P4_RUNTIME_SETUP_PROVISIONING`

```text
FSM = CREATED → PROVISIONING → PROVISIONED (| INVALID)
SOLO PROVISIONED ENTRA AL ACTIVATION GATE
PROVISIONING_FAILURE ⇒ lifecycle = INVALID · NO ACTIVATION   (semántica ÚNICA;
    corregido en D0-R2.6: el "INVALID o estado recuperable NO activo" dejaba el
    contrato abierto — ver §36.7 y el contraste con `ensure_skse`)
PROVISIONING_RETRY = Clone NUEVO (no se reintenta in-place sobre el mismo Clone)
SETUP_INTENT != SETUP_VERIFIED   (intended_runtime_setup_id != verified_runtime_setup_id)
NO_CLASIFICADO (Creation Club, Q22) = declarar `assumptions`; sin clasificar ⇒ no activa
```

No basta decir "existirá un manifest": el provisionamiento es una operación con
resultado tipado, y su fallo es un estado explícito, nunca "setup aplicado".

#### 35.8.3 `P4_CLONE_ACTIVATION_GATE` — composición congelada

```text
P4-OWNED:
  Generation VALID
  RuntimeCloneRecord válido
  lifecycle == PROVISIONED
  setup intended == verified
  critical expectations NON-EMPTY y en PASS
  independencia física fresca / escaneo de namespace (§35.4)
  exclusión de Managed Source
  approval scope válido (incl. source activation, §35.2)
  source activation sin cambios
  reverify fresco (§34.7)

P5-OWNED:
  compatibility == COMPATIBLE
  quiescencia
  oráculo de Effective Runtime

SI UN CHECK P5-OWNED NO EXISTE ⇒ UNKNOWN ⇒ NO ACTIVATION
```

#### 35.8.4 `P4_CLONE_PROVISIONING_LIFECYCLE`

```text
FSM = CREATED | PROVISIONING | PROVISIONED | INVALID
UN CORTE entre publicar el Clone y provisionar el setup debe ser DESCUBRIBLE y
CLASIFICABLE al arranque, NUNCA leído como setup aplicado.
PROVISIONED es requisito de activación (fail-closed).
UN CLONE EN PROVISIONING NO ES ACTIVABLE aunque su loader parezca presente
    (la detección de "ya instalado" no prueba el set completo; §36.7)
PROVISIONING_FAILURE ⇒ INVALID; RETRY = fresh Clone   (§36.7; D0-R2.6)
```

#### 35.8.5 `P4_ROLLBACK_TARGET_HISTORY`

```text
previous_activation_target = source_activation
SE ESCRIBE SÓLO CUANDO: POST passed ∧ mismo transition_id ∧ finalización exitosa
SE ESCRIBE ATÓMICAMENTE con FINALIZED (misma operación lógica)
ANTES DE ESO: el registro PENDING retiene source_activation para reconstruirlo
NO SOBRESCRIBIR el target histórico antes de una transición exitosa
CRASH ANTES ⇒ PENDING retiene source_activation (reconstrucción, no adivinanza)
AUTORIDAD = el registro de transición finalizado, inmutable y retenido (D0-R2.7; §36.8)
NO_DUPLICAR_EN_ACTIVE_JSON = YES   (dos copias pueden divergir; §36.8)
```

### 35.9 Matriz de crashes C0..C7 (revalidada tras R2/R5)

| Punto | transition durable | Desired | Effective posible | source activation | target evidence | Próxima acción segura | Automático | Dueño | ¿Finalizar? |
|---|---|---|---|---|---|---|---|---|---|
| **C0** antes de PENDING | `NONE` | source | source | actual | — | `COMPLETE` | — | no | — |
| **C1** tras PENDING, antes de bind | `PENDING` | source | source | en el registro | en el registro | re-bind **sólo si**: single-use no consumida por otra + source matchea + target evidence fresca + lock re-adquirido; si no, `REQUIRE_OWNER` | sí si todo matchea | sólo si no matchea | tras bind + POST |
| **C2** durante el bind | `PENDING` | source | **UNKNOWN / parcial** | en el registro | en el registro | **observar primero**; nunca suponer target. `RECOVER` si observa target + evidencia OK; si no, `FAIL_CLOSED` | sólo con observación concluyente | sí si no se puede observar | sólo tras observar + POST |
| **C3** tras bind, antes de desired | `PENDING` | source | target | en el registro | en el registro | persistir `Desired=target` → POST → finalize (§35.3) | sí | no | sí si POST pasa |
| **C4** tras desired, antes de POST | `PENDING` | target | target | en el registro | en el registro | re-correr POST | sí | no | sí si POST pasa |
| **C5** POST falla | `PENDING` (sigue) | target | target **no verificado** | en el registro | en el registro | **NO FINALIZA**. La aprobación original queda **consumida** por esa transición: no autoriza un bind nuevo; el dueño decide reparar (dentro del mismo `transition_id`) o abortar | no (fail-closed) | sí | no |
| **C6** POST pasa, antes de FINALIZE | `PENDING` | target | target verificado | en el registro | en el registro | repetir POST y finalizar **sólo si**: mismo `transition_id` ∧ mismo source/target ∧ mismo Desired ∧ mismo Effective ∧ evidencia fresca en PASS | sí | no | sí |
| **C7** tras FINALIZE | `FINALIZED` (+ evidencia POST) | target | target | `previous_activation_target` escrito | — | `COMPLETE` | — | no | — |

```text
FINALIZED_BEFORE_POST = PROHIBIDO en todos los puntos (§33.2)
C2_ASSUMPTION = NINGUNA
C5_APPROVAL_AFTER_FAILURE = CONSUMIDA por esa transición (no reutilizable para otra)
```

> **Revalidada y ampliada en la ronda 2 (§36.9).** Esa matriz cubre estados
> **dentro** de una transición; la ronda 2 agrega las columnas de **estado de
> consumo de la aprobación** y de **source baseline**, y los tres estados **fuera**
> de transición (`N0` limpio, `N1` `NONE + Desired != Effective`, `N2` journal
> corrupto).

### 35.10 Matriz de replay de aprobación

| Caso | Veredicto |
|---|---|
| source sin cambios + target sin cambios | **VALID** (continúa la misma transición) |
| source **cambió** + target sin cambios | **INVALIDATE** → `NEW APPROVAL REQUIRED` |
| source sin cambios + target **cambió** | **INVALIDATE** → `NEW APPROVAL REQUIRED` |
| source **cambió** + target **cambió** | **INVALIDATE** → `NEW APPROVAL REQUIRED` |
| aprobación ya consumida (otro `transition_id`) | **FAIL_CLOSED** (single-use) |
| aprobación stale/desconocida | **FAIL_CLOSED** |
| `transition_id` mismatch (CAS) | **FAIL_CLOSED** |

```text
SILENT_REUSE = PROHIBIDO
```

> **Matriz ampliada en la ronda 2 (§36.11).** Esta tabla sigue vigente, pero la
> ronda 2 le agrega los casos de **source baseline incoherente**, **aprobación
> consumida por una transición anterior tras rotar el journal** y **`approval_seq`
> ya consumido**, que son los que el `transition_id` solo no cubría (§36.5).

### 35.11 Matriz de independencia física del Clone

| Caso | Veredicto |
|---|---|
| symlink nuevo (archivo) | **BLOQUEA activación** |
| symlink nuevo (directorio) | **BLOQUEA activación** |
| junction nuevo (directorio) | **BLOQUEA activación** |
| reparse nuevo (directorio) | **BLOQUEA activación** |
| hardlink nuevo hacia Managed Source | **BLOQUEA activación** |
| archivo normal nuevo | **diagnóstico** (deriva reportada, no bloquea) |
| directorio normal nuevo | **diagnóstico** (deriva reportada, no bloquea) |
| directorio desaparecido | **diagnóstico**; si era crítico, **BLOQUEA** |
| archivo desaparecido | **diagnóstico**; si era crítico, **BLOQUEA** |
| cualquier caso anterior detectado **tras** aprobar | **INVALIDA la aprobación** (evidencia de contenido cambió) |

```text
NO_ASUMIR_QUE_SOLO_LOS_ARCHIVOS_PREVIAMENTE_INVENTARIADOS_IMPORTAN = YES
```

### 35.12 Revisión adversarial local — 18 preguntas

| # | Pregunta | Estado | Fundamento |
|---|---|---|---|
| 1 | ¿Una aprobación puede aplicarse si cambió el runtime de origen? | **CLOSED_BY_DESIGN** | §35.2: igualdad de `source_activation_digest` bajo lock; ampliado en §36.3 (el source debe ser el **Effective observado**, no sólo el Desired registrado) |
| 2 | ¿Una aprobación puede usarse dos veces? | **CLOSED_BY_DESIGN** | Single-use + `consumed_by_transition_id`; la ronda 2 agrega la **autoridad durable de consumo** que sobrevive a la rotación del journal (§36.5) |
| 3 | ¿Un `transition_id` viejo puede finalizar una transición nueva? | **CLOSED_BY_DESIGN** | CAS por `transition_id` (§35.6) |
| 4 | ¿`FINALIZED` tiene evidencia durable del POST? | **CLOSED_BY_DESIGN** | `post_verify_evidence_digest` + `post_verified_at_ns` + `finalized_from_transition_id` |
| 5 | ¿C3 puede completar sin violar `Desired==Effective`? | **CLOSED_BY_DESIGN** | §35.3: `Desired=target` **antes** del POST |
| 6 | ¿Startup distingue `Desired=source` / `Effective=target`? | **CLOSED_BY_DESIGN** | Matriz de §35.3 (5 combinaciones + corrupto) |
| 7 | ¿Un junction nuevo no presente en el inventario anterior escapa? | **CLOSED_BY_DESIGN** | §35.4: se enumera el namespace **actual**, no el inventario viejo |
| 8 | ¿Se escanean directorios actuales, no sólo archivos viejos? | **CLOSED_BY_DESIGN** | `_archivos_fisicos` + `capturar_membership_directorios` |
| 9 | ¿P5 binding quedó separado de integridad física del Clone? | **CLOSED_BY_DESIGN** | §35.4: `P5_BINDING_CLASSES != CLONE_FILESYSTEM_INTEGRITY` |
| 10 | ¿Todos los P4 blockers registrados fueron adjudicados? | **CLOSED_BY_DESIGN** | §35.5: 17/17, `OPEN = 0` |
| 11 | ¿Cancelación deja estados durables recuperables? | **CLOSED_BY_DESIGN** | §35.8.1: `PENDING` gobierna recovery; nunca `BUILDING` huérfano |
| 12 | ¿Un Clone no `PROVISIONED` puede activarse? | **CLOSED_BY_DESIGN** | §35.8.3/§35.8.4: `lifecycle == PROVISIONED` obligatorio |
| 13 | ¿Critical expectations vacías pueden pasar? | **CLOSED_BY_DESIGN** | `missing != empty-valid` (§34.12) |
| 14 | ¿Un source Generation no `VALID` permite retained fast path? | **CLOSED_BY_DESIGN** | §33.3 (R4.1-F2) |
| 15 | ¿P4 puede terminar activación con P5 `UNKNOWN`? | **CLOSED_BY_DESIGN** | §35.7: `UNKNOWN != COMPATIBLE` |
| 16 | ¿`previous_activation_target` puede sobrescribirse antes del POST? | **CLOSED_BY_DESIGN** | §35.8.5: sólo con POST passed ∧ mismo `transition_id` ∧ finalize |
| 17 | ¿Recovery reutiliza aprobación sólo cuando source+target+transition siguen idénticos? | **CLOSED_BY_DESIGN** | §35.10 + CAS |
| 18 | ¿Hay algún camino donde el Clone mutable se vuelva autoridad? | **CLOSED_BY_DESIGN** | §32.2 + §34.6/§35.2 (evidencia de contenido) + §33.3 |

```text
OPEN_BLOCKER = 0
```

### 35.13 Estado

```text
D0_R1_APPROVAL_SOURCE_BINDING         = CLOSED
D0_R2_CRASH_C3_ORDERING               = CLOSED
D0_R3_FRESH_CLONE_NAMESPACE_INTEGRITY = CLOSED
D0_R4_ALL_P4_BLOCKERS_CENSUSED        = CLOSED
D0_R5_FINALIZATION_EVIDENCE_AND_CAS   = CLOSED
D0_R6_P4_P5_WORDING                   = CLOSED

APPROVAL_BINDS_SOURCE_ACTIVATION = YES
APPROVAL_SINGLE_USE              = YES
APPROVAL_EXPIRATION_POLICY       = NO_TIME_BASED / CONTENT_BOUND_INVALIDATION
APPROVAL_OWNER_IDENTITY          = DEFERRED (no se equipara lock con identidad humana)
TRANSITION_ID_NO_CLOBBER         = YES (CAS)
FINALIZED_HAS_POST_EVIDENCE      = YES

CRASH_C3_DESIRED_BEFORE_POST     = YES
STARTUP_DESIRED_EFFECTIVE_MATRIX = YES (5 combinaciones + corrupto)
FRESH_NAMESPACE_SCAN_FILES       = YES
FRESH_NAMESPACE_SCAN_DIRECTORIES = YES
P5_BINDING_SEPARATE_FROM_CLONE_INTEGRITY = YES

P4_REQUIREMENTS_DISCOVERED           = 17
P4_REQUIREMENTS_DESIGN_CLOSED        = 16
P4_REQUIREMENTS_DEFERRED_FAIL_CLOSED = 1
P4_REQUIREMENTS_OPEN                 = 0

P4_LONG_RUNNING_CANCELLATION_DESIGN    = DESIGN_CLOSED
P4_RUNTIME_SETUP_PROVISIONING_DESIGN   = DESIGN_CLOSED
P4_CLONE_ACTIVATION_GATE_DESIGN        = DESIGN_CLOSED
P4_CLONE_PROVISIONING_LIFECYCLE_DESIGN = DESIGN_CLOSED
P4_ROLLBACK_TARGET_HISTORY_DESIGN      = DESIGN_CLOSED

RUNTIME_SETUP_ARTIFACT_AVAILABILITY = DEFERRED_FAIL_CLOSED
CREATION_CLUB_CLASSIFICATION        = DEFERRED_PENDING_EVIDENCE
P3B                                 = DEFERRED_PENDING_RIG

OPEN_P4_DESIGN_BLOCKERS = 0

P4_READY_TO_DESIGN      = YES
P4_DESIGN_FROZEN        = NO   (SUPERSEDED: la ronda adversarial 2 abrió 6 residuos;
                                estado vigente en §36.15)
P4_READY_TO_IMPLEMENT   = NO   (SUPERSEDED: ver §36.15)
P4_IMPLEMENTED          = NO
P5_IMPLEMENTED          = NO
PR_READY_TO_MERGE       = NO
```

`P4_READY_TO_IMPLEMENT = YES` significa **"el diseño no bloquea"**, no "P4 existe".
`P4_IMPLEMENTED = NO` sigue siendo el estado real.

**Alcance de esta ronda:** docs-only. `PRODUCT_CODE_CHANGED = NO`. No se implementó
transición, ni lock cross-process, ni CAS, ni escaneo de namespace, ni provisioning, ni
cancelación, ni gate de activación, ni `active.json` real, ni binding de MO2, ni setup
de SKSE, ni cache de artefactos, ni rollback, ni promoción. No se tocó P5. `MERGE = NO`.

---

## 36. P4-D0 — ronda adversarial 2: cierre de residuos contractuales (2026-10-09)

Revisión adversarial externa sobre `589805ee130eb3e780f1b7c581959e0792a6c3f2`.
**6 residuos nuevos, todos `CONFIRMED`**, más **1 finding de consistencia** que la
ronda 1 dejó abierto (`previous_activation_target` vs `active.json`) y **1 defecto
aritmético** descubierto al rehacer el censo. Cada uno se verificó contra el texto y
contra el código **antes** de adjudicarlo; ninguno se aceptó por autoridad de quien lo
reportó.

Esta sección **corrige in-place** §7, §11, §12, §19, §29.8, §30.6, §31.4, §32, §34 y
§35. No agrega banners de supersesión como sustituto de la corrección: las secciones
normativas viejas **dicen ahora lo mismo** que este contrato.

`docs-only`. `PRODUCT_CODE_CHANGED = NO`. `P4_IMPLEMENTED = NO`, `P5_IMPLEMENTED = NO`.

### 36.1 Adjudicación

| Finding | Sev. | Adjudicación | Resolución |
|---|---|---|---|
| **D0-R2.1** `previous_activation_target` tiene ordering contradictorio | P1 | **CONFIRMED** | §36.2: se congela **un solo** punto de escritura (FINALIZE, tras POST PASS) y se corrigen **in-place** §7, §11, §12, §19a, §30.6, §31.4, §32.3, §32.4. |
| **D0-R2.2** `source_activation` no prueba el Effective Runtime saliente | P1 | **CONFIRMED** | §36.3: `NEW_TRANSITION_REQUIRES_COHERENT_SOURCE_BASELINE = YES`; el source se demuestra contra el **Effective observado**, no contra el Desired registrado. Divergencia ⇒ `FAIL_CLOSED`. |
| **D0-R2.3** falta `NO PENDING + Desired != Effective` | P1 | **CONFIRMED** | §36.4: estado `N1 = INCONSISTENT_BASELINE` explícito, detectable **aunque `transition.json` no exista**; prohibido fabricar `PENDING` o elegir un lado. |
| **D0-R2.4** `APPROVAL_SINGLE_USE` necesita autoridad durable | P1 | **CONFIRMED** | §36.5: autoridad de consumo **fuera del journal** (contador monótono + `approval_id`), acotada y no dependiente de la profundidad del histórico. `APPROVAL_REPLAY_AFTER_JOURNAL_REPLACEMENT = IMPOSSIBLE_BY_CONTRACT`. |
| **D0-R2.5** CAS incompleto para creación/reemplazo del journal | P2 | **CONFIRMED** | §36.6: se distinguen `CREATE`, `UPDATE`, `FINALIZE` y `START_NEXT` con su precondición, más `journal_revision` monótona (anti-ABA) y `AT MOST ONE PENDING PER ROOT`. |
| **D0-R2.6** cancelación y provisioning con estados ambiguos | P2 | **CONFIRMED** | §36.7: `CANCELLED` es **resultado de operación**, nunca lifecycle del Clone; `PROVISIONING_FAILURE` pasa a tener **una** semántica (`INVALID`, reintento = Clone nuevo), contrastada con `ensure_skse`. |
| **D0-R2.7** `previous_activation_target`: `active.json` vs transición | P2 | **CONFIRMED** (relacionado con R2.1) | §36.8: **autoridad única** = registro de transición finalizado; se corrige el schema v2 de `active.json` para no duplicar el dato. |
| **D0-R2.8** desglose aritmético del censo de §35.5 | P3 | **CONFIRMED** (**finding nuevo**, descubierto en esta ronda) | §36.13: el desglose real es `13 + 23 = 36`, no `17 + 19`; corregido in-place en §35.5. No bloquea diseño. |

```text
D0_R2_1_PREVIOUS_TARGET_ORDERING        = CONFIRMED / CLOSED (§36.2)
D0_R2_2_EFFECTIVE_SOURCE_BASELINE       = CONFIRMED / CLOSED (§36.3)
D0_R2_3_NO_PENDING_SPLIT_BRAIN          = CONFIRMED / CLOSED (§36.4)
D0_R2_4_DURABLE_APPROVAL_SINGLE_USE     = CONFIRMED / CLOSED (§36.5)
D0_R2_5_JOURNAL_CREATE_REPLACE_CAS      = CONFIRMED / CLOSED (§36.6)
D0_R2_6_CANCELLATION_PROVISIONING_FSM   = CONFIRMED / CLOSED (§36.7)
D0_R2_7_PREVIOUS_TARGET_AUTHORITY       = CONFIRMED / CLOSED (§36.8)
D0_R2_8_CENSUS_ARITHMETIC               = CONFIRMED / CLOSED (§36.13)
```

### 36.2 D0-R2.1 — `previous_activation_target` se publica en el FINALIZE

**El defecto.** §35.8.5 ya decía lo correcto —se escribe **sólo** con POST passed, con
el mismo `transition_id`, atómicamente con `FINALIZED`— pero el resto del ADR seguía
describiendo el orden viejo: persistir el par desired **y** `previous_activation_target`
**antes** del POST. Residuos verificados en §7, §11, §12 paso 12, §19a, §31.4, §32.3 y
§32.4. Con esa secuencia, un POST que falla dejaba publicado como "histórico previo" un
runtime que la transición todavía no había ganado.

**Semántica normativa única (la de este bloque, y la única que el ADR describe ahora):**

```text
PENDING ya contiene source_activation
    ↓
bind target
    ↓
persist Desired = target            (CAS UPDATE)
    ↓
POST verify Desired == Effective
    ↓
POST PASS
    ↓
FINALIZE transition                 (CAS FINALIZE)
AND atomically publish:
    previous_activation_target = source_activation
    ↓
SUCCESS
```

```text
PREVIOUS_TARGET_WRITE_POINT                    = FINALIZE
PREVIOUS_TARGET_MUTATED_BEFORE_POST_PASS       = PROHIBIDO
PREVIOUS_TARGET_REQUIRES_SAME_TRANSITION_ID    = YES
PREVIOUS_TARGET_WRITTEN_ATOMICALLY_WITH_FINALIZE = YES
PREVIOUS_TARGET_SURVIVES_CRASH_BEFORE_FINALIZE = NO  (correcto: la transición no se completó)
BEFORE_FINALIZE = el histórico previo sigue siendo autoridad; el registro PENDING
                  retiene source_activation para reconstruirlo (no se adivina)
```

**Por qué no alcanza un banner.** Los flujos de §7/§11/§12/§31.4/§32.3/§32.4 son la
especificación que un implementador lee primero; dejar el texto viejo y una nota al pie
produce exactamente el defecto que este ADR documenta como dominante. Se corrigieron
**in-place**, y el orden viejo ya no aparece en ninguna sección normativa.

### 36.3 D0-R2.2 — el source baseline debe ser el Effective observado

**El defecto.** `source_activation = { generation_id, clone_id, runtime_setup_id }`
(§35.2) se derivaba del estado **Desired/registrado**. Eso no demuestra qué runtime se
está ejecutando. Caso adversarial:

```text
Desired   = G1 / C1 / S1
pero por corrupción o mutación externa:
Effective = G3 / C3 / S3
y NO hay transición PENDING que lo explique
```

Con la ligadura vieja, una aprobación se crearía con `source_activation = G1/C1/S1`
mientras el runtime que realmente se reemplaza es `G3/C3/S3` — y
`previous_activation_target = source_activation` habría guardado el **histórico
equivocado**. Es el mismo tipo de error que D0-R2.1 y D0-R2.3: dar por probado lo que
sólo estaba registrado.

**Contrato.**

```text
NEW_TRANSITION_REQUIRES_COHERENT_SOURCE_BASELINE = YES
```

Antes de **presentar** (y por lo tanto antes de aceptar) una `ApprovalScope` nueva:

```text
Desired source  ==  Effective source observado  ==  RuntimeCloneRecord source
```

donde "source" es la terna completa **incluido el setup**:

```text
source_activation = {
    generation_id,            # Desired registrado (active.json)
    clone_id,                 # RuntimeCloneRecord del target activo
    runtime_setup_id,         # verified_runtime_setup_id del Clone activo
    effective_runtime_evidence,   # NUEVO: observación del Effective Runtime
                                  # (primitiva de P5) que sostiene la igualdad
}
source_activation_digest = SHA-256(canonical_json(source_activation))
```

`effective_runtime_evidence` es la pieza que convierte una afirmación registrada en una
afirmación **observada**: sin ella el source es una intención, no un hecho. La evidencia
queda **ligada a la aprobación** (viaja en el scope y en su digest), de modo que la
re-verificación pre-bind compara observación contra observación, no identidad contra
identidad.

**Si Desired y Effective divergen:**

```text
NO NEW APPROVAL
NO NEW PENDING
NO PROMOTION
NO ROLLBACK
→ FAIL_CLOSED
→ RECOVERY / REQUIRE_OWNER          (§36.4)
```

```text
SILENT_DESIRED_TO_EFFECTIVE_CONVERSION = PROHIBIDO
SILENT_EFFECTIVE_TO_DESIRED_CONVERSION = PROHIBIDO
```

Nunca se convierte uno en el otro "para que cierre".

**Dependencia declarada, no oculta.** La observación del Effective Runtime es de P5
(`P5_EFFECTIVE_RUNTIME_ORACLE`). Mientras P5 devuelva `UNKNOWN`, la triple igualdad
**no puede demostrarse** y por lo tanto **no se crea una transición nueva**: es la misma
dirección que `UNKNOWN != COMPATIBLE` y que §35.7. Esto **no** bloquea implementar y
testear el core de P4 —el gate recibe la observación como entrada y se ejercita con un
doble de test— pero sí implica que la activación no se completa sin P5. Es una
**restricción más fuerte** que §35.7 y se declara como tal:

```text
P4_CORE_MAY_BE_IMPLEMENTED_BEFORE_P5 = YES        (sin cambios)
ACTIVATION_COMPLETION_REQUIRES_P5    = YES        (sin cambios)
NEW_TRANSITION_REQUIRES_P5_OBSERVATION = YES      (agregado por D0-R2.2)
```

### 36.4 D0-R2.3 — `NO PENDING + Desired != Effective` es un estado propio

**El defecto.** §35.3 cubría "no hay transición y el estado es coherente" y varios
estados con `PENDING`, pero **no** el caso:

```text
NO transition
+
Desired != Effective
```

Ese estado es alcanzable sin ninguna transición en curso: cambio externo, corrupción,
fallo histórico, intervención manual, software ajeno o pérdida del journal. Al no
nombrarlo, la reconciliación podía leerlo como "no hay nada que hacer".

**Contrato.**

```text
transition = NONE
Desired != Effective
    ⇒ INCONSISTENT_BASELINE            (estado N1)
    ⇒ FAIL_CLOSED
    ⇒ NO NEW TRANSITION
    ⇒ NO NEW APPROVAL
    ⇒ REQUIRE_OWNER / RECOVERY
```

Prohibiciones explícitas:

```text
FABRICAR_UN_PENDING_RETROACTIVO      = PROHIBIDO
ELEGIR_AUTOMATICAMENTE_DESIRED       = PROHIBIDO
ELEGIR_AUTOMATICAMENTE_EFFECTIVE     = PROHIBIDO
```

`Startup reconciliation` debe **detectar** este estado **aunque `transition.json` no
exista**: la detección no puede depender de que haya un journal que leer. Los tres
estados fuera de transición quedan nombrados:

```text
N0: journal NONE  · Desired == Effective  → clean (COMPLETE)
N1: journal NONE  · Desired != Effective  → INCONSISTENT_BASELINE → FAIL_CLOSED
N2: journal corrupto (con o sin transición previa) → FAIL_CLOSED
```

```text
CORRUPT_JOURNAL_IS_NEVER_READ_AS_ABSENT = YES
```

Un journal corrupto **no** es un journal ausente: degradarlo a `NONE` convertiría N2 en
N0 y borraría la única evidencia de que algo estaba en curso.

### 36.5 D0-R2.4 — autoridad durable del consumo de aprobación

**El defecto.** §35 declara `APPROVAL_SINGLE_USE = YES` y `consumed_by_transition_id`,
lo cual funciona **mientras la transición T1 sigue representada**. Pero el journal es un
archivo que se reemplaza: tras `FINALIZED(T1) → PENDING(T2)`, `transition.json` ya no
menciona T1. Sin una autoridad que sobreviva a esa rotación, "esta aprobación ya se
consumió" deja de ser demostrable y el replay vuelve a ser posible.

**La pregunta a resolver:** ¿quién es la autoridad durable de
`approval already consumed`?

| Opción | Qué ofrece | Límite |
|---|---|---|
| **A** — la autoridad HITL emite un token/nonce durable single-use | la identidad nace con la autorización | el producto **hoy no emite** ese nonce: la solicitud HITL es efímera (§35.2: `OWNER_IDENTITY_BINDING = DEFERRED`) |
| **B** — Sky-Claw persiste los IDs/nonces consumidos | demostrable y local | un **ledger** de profundidad arbitraria crece sin cota |
| **C** — el `approval_id` sólo existe dentro de un registro de transición inmutable, retenido por historia acotada | reutiliza la maquinaria del journal | una ventana acotada puede **agotarse**: más allá de K rotaciones el replay deja de estar bloqueado |
| **D** — equivalente, acotada | — | — |

**Decisión: D — combinación acotada y demostrablemente equivalente.**

```text
APPROVAL_AUTHORITY    = la superficie HITL del producto (quién dijo que sí)
APPROVAL_IDENTITY     = approval_id (UUID v4) + approval_seq (entero monótono por root)
APPROVAL_PROVENANCE   = referencia opaca (superficie + id de solicitud) en el scope
PROCESS_LOCK_OWNER    = session_id del lock P4 (proceso, NO identidad humana)

lock_session_id == human_authorization_identity  ⇒ NO (no demostrado; sigue DEFERRED)
```

`approval_id`/`approval_seq` **no** son una identidad humana: son un **token de
consumo**. Las cuatro cosas —authority, identity/nonce, provenance, lock owner— quedan
separadas para no fingir una garantía que el producto todavía no emite.

**Autoridad de consumo: fuera del journal.**

```text
CONSUMPTION_AUTHORITY = state/approval_consumption.json   (durable, escritura atómica)
{
  schema_version,
  last_issued_approval_seq,     # emisión (la escribe la superficie de aprobación)
  last_consumed_approval_seq,   # consumo (lo escribe el CREATE PENDING)
  consumed_ring[]               # K entradas, SÓLO auditoría — NO es el mecanismo de bloqueo
}
```

```text
CONSUME requires: approval_seq == last_issued_approval_seq
                ∧ approval_seq >  last_consumed_approval_seq
                ∧ approval_id    == el de la aprobación presentada
ON CONSUME:       last_consumed_approval_seq = approval_seq
ATOMICIDAD:       el consumo se escribe en la MISMA operación lógica que el CREATE de
                  PENDING_* (§36.6): no hay ventana "consumida pero sin transición" ni
                  "transición sin aprobación consumida"
EMISIÓN vs CONSUMO: `last_issued_approval_seq` lo escribe SÓLO la autoridad de
                  aprobación; la maquinaria de transición NUNCA lo escribe. Si el mismo
                  componente pudiera emitir y consumir, la aprobación sería una
                  auto-autorización y la separación de §35.2 quedaría vacía.
LEDGER AUSENTE    ⇒ no hay aprobaciones emitidas ⇒ ninguna aprobación es válida
LEDGER CORRUPTO   ⇒ FAIL_CLOSED (nunca "sin consumo previo")
```

```text
SELF_ISSUED_APPROVAL_IS_AUTHORIZATION = NO
APPROVAL_ISSUER_IS_NOT_THE_TRANSITION_MACHINERY = YES
```

**Por qué el contador monótono y por qué C sola no alcanza.** El bloqueo lo da
`approval_seq > last_consumed_approval_seq`: es **O(1)**, no depende de la profundidad
del histórico y **no se agota**. El anillo `consumed_ring[]` es auditoría, no
enforcement: si se recorta, no se debilita ninguna garantía. La opción C sola dependía
de que la ventana retenida alcanzara; el contador vuelve esa dependencia innecesaria
para el bloqueo.

```text
APPROVAL_REPLAY_AFTER_JOURNAL_REPLACEMENT = IMPOSSIBLE_BY_CONTRACT
APPROVAL_DURABLE_IDENTITY                 = approval_id (UUID v4) + approval_seq monótono
APPROVAL_CONSUMPTION_STATE_IS_BOUNDED     = YES (2 enteros + anillo de auditoría)
APPROVAL_LEDGER_DEPTH_UNBOUNDED           = NO
```

**Dependencia declarada.** Que la superficie HITL **emita** `approval_id`/`approval_seq`
es un requisito de implementación de P4 sobre la superficie de aprobación, y hoy no
existe. El **contrato** queda cerrado acá; el mecanismo, abierto como el resto de P4.

### 36.6 D0-R2.5 — CAS de creación, actualización, finalización y rotación

**El defecto.** `expected_transition_id == current_transition_id` (§35.6) alcanza para
**actualizar** T1, pero no define `NONE → PENDING T1` ni `FINALIZED T1 → PENDING T2`, y
no dice qué pasa si ya existe un `PENDING`.

**Estados del journal:**

```text
ABSENT | PENDING_PROMOTION | PENDING_ROLLBACK | FINALIZED | CORRUPT
```

**Precondiciones por operación:**

```text
CREATE (ABSENT|NONE → PENDING T):
    expected journal ∈ { ABSENT, NONE, FINALIZED }
    ∧ NO existe PENDING activo
    ∧ consumo de aprobación OK (§36.5), en la MISMA operación lógica
    ∧ T.transition_id no visto antes

UPDATE (PENDING T → PENDING T):
    expected (transition_id == T, journal_revision == R)

FINALIZE (PENDING T → FINALIZED T):
    expected (transition_id == T, journal_revision == R)
    ∧ post_verify_evidence presente y en PASS (§35.6)
    ∧ publica previous_activation_target en la misma operación (§36.2)

START_NEXT (FINALIZED Tprev → PENDING Tnext):
    expected journal == FINALIZED
    ∧ finalized_from_transition_id == Tprev
    ∧ Tnext.transition_id != Tprev
    ∧ el registro FINALIZED(Tprev) queda RETENIDO e inmutable (§36.9)
```

**Reglas duras:**

```text
AT_MOST_ONE_ACTIVE_PENDING_TRANSITION_PER_FrozenRuntimeRoot = YES
PENDING_EXISTS ⇒ NO_SECOND_TRANSITION                        = YES
CAS_FAILURE    ⇒ FAIL_CLOSED (nunca "seguir igual")
CORRUPT_JOURNAL ⇒ FAIL_CLOSED (nunca ABSENT)                 = YES   (§36.4 N2)
```

**Anti-ABA / anti-replay.** `transition_id` es UUID v4 (no se reutiliza) y cada mutación
incrementa `journal_revision`, un entero monótono por root. El CAS compara **ambos**: una
transición vieja no puede finalizar una más nueva ni "volver" a un estado anterior con el
mismo `transition_id`, porque la revisión ya avanzó.

```text
JOURNAL_CREATE_CAS          = journal ∈ {ABSENT, FINALIZED} ∧ no PENDING ∧ consumo atómico
JOURNAL_UPDATE_CAS          = (transition_id, journal_revision)
JOURNAL_FINALIZE_CAS        = (transition_id, journal_revision) ∧ POST evidence PASS
JOURNAL_NEXT_TRANSITION_CAS = FINALIZED ∧ finalized_from_transition_id == Tprev
ABA_PROTECTION              = transition_id (UUIDv4) + journal_revision monótona
```

### 36.7 D0-R2.6 — cancelación y provisioning con semántica determinista

**El defecto (a).** §35.8.1 decía `INVALID | CANCELLED` como estado final, pero la FSM
de `RuntimeCloneRecord` (§19c) es `CREATED | PROVISIONING | PROVISIONED | INVALID`:
`CANCELLED` **no existe** ahí. Mezclar el resultado de una operación con el ciclo de vida
del Clone deja abierta la pregunta de si un Clone "cancelado" sigue siendo activable.

**Decisión (a).**

```text
CANCELLED_IS_CLONE_LIFECYCLE   = NO
CANCELLED_IS_OPERATION_OUTCOME = YES

Clone lifecycle (sin cambios, §19c):
    CREATED | PROVISIONING | PROVISIONED | INVALID
Operation result:
    SUCCESS | CANCELLED | FAILED | ...
```

No se agregan estados a la FSM sin justificación: una cancelación no cambia **qué es** el
Clone, cambia **qué pasó con la operación** que lo estaba preparando.

```text
CANCEL BEFORE PENDING  → el runtime activo queda idéntico; artefactos preparados inactivos
CANCEL AFTER  PENDING  → MUST NOT erase PENDING
                         MUST NOT claim rollback
                         MUST NOT claim clean state
                         recovery gobierna la transición que ya está en disco
```

Detalle por fase en la matriz de §36.12.

**El defecto (b).** §35.8.2 decía
`PROVISIONING_FAILURE = INVALID o estado recuperable NO activo`. Ese **"o"** dejaba el
contrato abierto: dos desenlaces posibles para el mismo hecho.

**Contraste con el código (evidencia, no intuición).** `ensure_skse`
(`sky_claw/local/tools_installer.py`) es el provisionador real que existe hoy:

1. resuelve el release por el **runtime exacto** del ejecutable (fail-closed antes de
   cualquier escritura);
2. **idempotencia**: `find_skse_installation(...)` — "loader del juego + algún DLL de
   runtime que corresponda al runtime real";
3. descarga a **staging** temporal, extrae y **re-valida el PE pegado a la copia**;
4. `_copy_skse_files` escribe loader/DLL/`Data` en el game dir — **primera mutación**;
5. `_cleanup_orphaned_skse_dlls` corre **después** de que la copia terminó sin excepción
   (el propio comentario del código lo declara el punto de no retorno).

De ahí salen las respuestas, medidas y no supuestas:

```text
1. ¿Puede PROVISIONING reintentarse sobre el mismo Clone?
   NO con garantía: la detección de idempotencia prueba "hay un loader y un DLL del
   runtime", NO que el set completo del payload esté aplicado.
2. ¿El fallo parcial deja archivos mutados?
   SÍ: `_copy_skse_files` muta el game dir y puede fallar a mitad (disco lleno,
   permisos). La limpieza de DLL huérfanos NO corre en ese caso.
3. ¿Puede conocerse qué componentes llegaron a instalarse?
   HOY NO: no hay registro por componente del set aplicado.
4. ¿Es más seguro invalidar el Clone y recrearlo?
   SÍ, mientras (3) siga siendo NO.
5. ¿Existe un retry idempotente demostrable?
   NO demostrado. Un Clone parcialmente provisionado con loader + DLL presentes sería
   reportado `already_existed` por la detección de (2) y quedaría leído como setup
   aplicado — exactamente el fail-open que el gate debe evitar.
```

**Decisión (b) — una sola semántica:**

```text
PROVISIONING_FAILURE ⇒ RuntimeCloneRecord.lifecycle = INVALID
NO ACTIVATION
RETRY = fresh Clone            (no se reintenta in-place sobre el mismo Clone)
UN_CLONE_EN_PROVISIONING_NO_ES_ACTIVABLE_AUNQUE_EL_LOADER_PAREZCA_PRESENTE = YES
```

Si en el futuro se demuestra idempotencia completa (registro por componente + set
verificable), la política puede relajarse; hoy **no** está demostrada y el diseño no la
supone.

### 36.8 D0-R2.7 — una sola autoridad para `previous_activation_target`

El ADR histórico mostraba `previous_activation_target` **dentro** de `active.json` v2
(§19) y §35.8.5 lo publicaba al FINALIZE. Dos lugares para el mismo dato son dos
autoridades, y dos autoridades pueden divergir.

**Decisión: la autoridad es el registro de transición finalizado.** `active.json` v2
**deja de llevar el campo**.

```text
PREVIOUS_TARGET_AUTHORITY      = registro de transición FINALIZED (inmutable, retenido)
PREVIOUS_TARGET_IN_ACTIVE_JSON = NO   (schema v2 corregido in-place en §19)
PREVIOUS_TARGET                = source_activation del FINALIZED más reciente;
                                 NONE si no hay ninguno (hecho, no incógnita)
```

**Por qué esta dirección y no la otra.** Mantener el campo en `active.json` exigía
declararlo *proyección* del registro de transición y verificar la coincidencia al
arrancar — una autoridad más un chequeo de divergencia, para un dato que ya vive en la
maquinaria de transición que §36.5 y §36.6 **necesitan de todos modos**. Con autoridad
única no hay nada que pueda divergir.

```text
DOS_AUTORIDADES_INDEPENDIENTES_PARA_PREVIOUS_TARGET = PROHIBIDO
ACTIVE_JSON_V2_CARRIES_PREVIOUS_TARGET              = NO (corregido)
DERIVACION_ES_DETERMINISTA                          = YES (no se adivina)
```

Consecuencia práctica: el rollback lee el target histórico del registro finalizado, no de
`active.json`. Sin registros finalizados, el target previo es `NONE`.

### 36.9 Matriz de crashes revalidada — C0..C7 más N0/N1/N2

Cada fila declara las diez dimensiones que un implementador necesita para decidir sin
adivinar. `consumo` es el estado de la aprobación según §36.5; `baseline` es la triple
igualdad de §36.3.

| Punto | journal | consumo | Desired | Effective | source baseline | target evidence | próxima acción | auto | dueño | ¿finaliza? |
|---|---|---|---|---|---|---|---|---|---|---|
| **N0** sin transición, coherente | `NONE` | no consumida | == Effective | == Desired | coherente | — | `COMPLETE` | — | no | — |
| **N1** sin transición, divergente | `NONE` | no consumida | **!= Effective** | **!= Desired** | **INCOHERENTE** | — | `INCONSISTENT_BASELINE` → `FAIL_CLOSED` · `REQUIRE_OWNER`/recovery | no | sí | no |
| **N2** journal corrupto | `CORRUPT` | indeterminado | indeterminado | indeterminado | indeterminado | indeterminado | `FAIL_CLOSED` (**nunca** `NONE`) | no | sí | no |
| **C0** antes de PENDING | `NONE` | no consumida | source | source | coherente | — | `COMPLETE` | — | no | — |
| **C1** tras PENDING, antes de bind | `PENDING` | consumida por T | source | source | re-verificar | en el registro | re-bind **sólo si**: consumo consistente ∧ source matchea ∧ target evidence fresca ∧ lock re-adquirido; si no, `REQUIRE_OWNER` | sí si todo matchea | sólo si no matchea | tras bind + POST |
| **C2** durante el bind | `PENDING` | consumida por T | source | **UNKNOWN / parcial** | indeterminado | en el registro | **observar primero**; nunca suponer target. `RECOVER` si observa target + evidencia OK; si no, `FAIL_CLOSED` | sólo con observación concluyente | sí si no se puede observar | sólo tras observar + POST |
| **C3** tras bind, antes de desired | `PENDING` | consumida por T | source | target | coherente | en el registro | persistir `Desired=target` → POST → finalize (§35.3) | sí | no | sí si POST pasa |
| **C4** tras desired, antes de POST | `PENDING` | consumida por T | target | target | coherente | en el registro | re-correr POST | sí | no | sí si POST pasa |
| **C5** POST falla | `PENDING` (sigue) | **consumida por esa transición** | target | target **no verificado** | coherente | en el registro | **NO FINALIZA**. La aprobación no autoriza un bind nuevo; el dueño repara dentro del mismo `transition_id` o aborta | no (fail-closed) | sí | no |
| **C6** POST pasa, antes de FINALIZE | `PENDING` | consumida por T | target | target verificado | coherente | en el registro | repetir POST y finalizar **sólo si**: mismo `transition_id` ∧ mismo source/target ∧ mismo Desired ∧ mismo Effective ∧ evidencia fresca en PASS | sí | no | sí |
| **C7** tras FINALIZE | `FINALIZED` (+ evidencia POST) | consumida por T | target | target | coherente | — | `COMPLETE`; `previous_activation_target` publicado | — | no | — |

```text
FINALIZED_BEFORE_POST              = PROHIBIDO en todos los puntos (§33.2)
C2_ASSUMPTION                      = NINGUNA
C5_APPROVAL_AFTER_FAILURE          = CONSUMIDA por esa transición (no reutilizable para otra)
PREVIOUS_TARGET_WRITTEN_AT         = C7 (FINALIZE), nunca antes
NO_TRANSITION_MATRIX               = N0 / N1 / N2 (§36.4)
```

### 36.10 Ciclo de vida del journal

```text
ABSENT → PENDING_PROMOTION | PENDING_ROLLBACK → FINALIZED → (START_NEXT) → PENDING_*
CORRUPT es un estado de DETECCIÓN, no una fase del ciclo
```

| Pregunta | Respuesta |
|---|---|
| ¿se conserva `FINALIZED`? | **Sí.** Pasa a ser un registro **inmutable**; no se reescribe. |
| ¿por cuánto tiempo? | Historia **acotada** de `K` registros finalizados, con `K ≥ 2` (el vigente y su predecesor). `K ≥ 2` es lo que exigen el CAS de `START_NEXT` (§36.6) y la derivación de `previous_activation_target` (§36.8). |
| ¿cómo empieza la siguiente transición? | `START_NEXT` con CAS sobre `FINALIZED ∧ finalized_from_transition_id == Tprev` (§36.6). |
| ¿qué evidencia conserva? | `post_verify_evidence_digest`, `post_verified_at_ns`, `finalized_from_transition_id` (§35.6), más `source_activation`, `target_*`, `approval_scope_digest` y `consumed_approval_id`. |
| ¿cómo se evita replay? | Tres capas: `approval_seq` monótono (consumo, §36.5), `transition_id` UUIDv4 + `journal_revision` monótona (journal, §36.6) y CAS de `START_NEXT`. |
| ¿cómo se vincula el consumo de aprobación? | El registro guarda `consumed_approval_id`; la **autoridad** es `state/approval_consumption.json` (§36.5), que sobrevive a la rotación. |
| ¿cómo se vincula `previous_activation_target`? | Es el `source_activation` del registro `FINALIZED` **más reciente** (§36.8); no se duplica en `active.json`. |
| ¿cómo se recupera tras crash? | §36.9: observar primero, decidir después; `PENDING` gobierna recovery; nunca revertir automáticamente ni asumir el target. |

```text
JOURNAL_RETENTION_DEPTH_FOR_BLOCKING = 0     (el bloqueo de replay NO depende de K)
JOURNAL_RETENTION_DEPTH_FOR_DERIVATION = 2   (K ≥ 2)
FINALIZED_RECORDS_ARE_IMMUTABLE = YES
```

### 36.11 Matriz de aprobación — ronda 2

Extiende §35.10 con los casos que el `transition_id` solo no cubría.

| Caso | Veredicto |
|---|---|
| source `Desired == Effective` y todo lo demás matchea | **ALLOW** |
| source `Desired != Effective` sin transición que lo explique | **FAIL_CLOSED** (N1, §36.4) |
| source `Desired != Effective` con transición `PENDING` que lo explica | **RECOVERY** gobierna (§36.9) |
| aprobación **sin consumir** (`approval_seq == last_issued` ∧ `> last_consumed`) | **ALLOW** |
| aprobación consumida **por la misma transición** | **CONTINUE_SAME_TRANSITION** (es continuación, no replay) |
| aprobación consumida **por una transición anterior** (journal ya rotado) | **FAIL_CLOSED** (`approval_seq <= last_consumed`; §36.5) |
| `approval_id` desconocido | **FAIL_CLOSED** |
| `approval_id` no coincide con el del scope presentado | **FAIL_CLOSED** |
| provenance de aprobación ausente | **FAIL_CLOSED** (es requisito del scope) |
| target sin cambios | **CONTINUE_SAME_TRANSITION** |
| target cambió | **REQUIRE_NEW_APPROVAL** |
| source cambió | **REQUIRE_NEW_APPROVAL** |
| journal cambió (CAS de revisión) | **FAIL_CLOSED** |
| predecessor de transición no coincide (`START_NEXT`) | **FAIL_CLOSED** |
| ledger de consumo ausente | **FAIL_CLOSED** (no hay aprobaciones emitidas) |
| ledger de consumo corrupto | **FAIL_CLOSED** (nunca "sin consumo previo") |

```text
SILENT_REUSE = PROHIBIDO
APROBACION_NO_PUEDE_SALTAR_EL_BASELINE = YES
```

### 36.12 Matriz de provisioning y cancelación

Columnas: fase de la cancelación · lifecycle del Clone · resultado de operación · estado
del journal · ¿cleanup permitido? · ruta de recovery · ¿dueño?

| Caso | Clone lifecycle | Resultado | journal | ¿cleanup? | recovery | ¿dueño? |
|---|---|---|---|---|---|---|
| cancel **antes** de crear el Clone | — (no existe) | `CANCELLED` | `NONE` | sí (nada que limpiar) | ninguna | no |
| cancel **después de `CREATED`** | `CREATED` | `CANCELLED` | `NONE` | sí (artefacto inactivo, GC §23) | ninguna | no |
| cancel **durante `PROVISIONING`** | `INVALID` (§36.7) | `CANCELLED` | `NONE` | sí, con el Clone invalidado | Clone nuevo si se reintenta | no |
| **provisioning falla parcialmente** | `INVALID` | `FAILED` | `NONE` | sí, con el Clone invalidado | Clone nuevo (no retry in-place) | no |
| cancel **tras `PROVISIONED`, antes de aprobar** | `PROVISIONED` | `CANCELLED` | `NONE` | sí (inactivo, GC §23) | ninguna | no |
| cancel **tras aprobar, antes de `PENDING`** | `PROVISIONED` | `CANCELLED` | `NONE` | aprobación **no consumida**; artefactos inactivos | ninguna | no |
| cancel **tras `PENDING`, antes del bind** | sin cambio | `CANCELLED` | **`PENDING` (se conserva)** | **NO** borrar el journal | recovery de §36.9 (C1) | no |
| cancel **durante el bind** | sin cambio | `CANCELLED` | **`PENDING` (se conserva)** | **NO** | recovery (C2): observar primero | sí si no se puede observar |
| cancel **tras el bind, antes del POST** | sin cambio | `CANCELLED` | **`PENDING` (se conserva)** | **NO** | recovery (C3/C4) | no |

```text
CANCEL_AFTER_PENDING_ERASES_TRANSITION = PROHIBIDO
CANCEL_AFTER_PENDING_CLAIMS_ROLLBACK   = PROHIBIDO
CANCEL_AFTER_PENDING_CLAIMS_CLEAN      = PROHIBIDO
CANCELLED_CHANGES_CLONE_LIFECYCLE      = NO
PARTIAL_PROVISIONED_CLONE_IS_ACTIVABLE = NO
```

### 36.13 Censo de readiness — recomputado, no recordado

```bash
grep -oE "P4_[A-Z0-9_]+" docs/adr/0012-frozen-runtime.md | sort -u
```

Sobre el ADR tras §36 devuelve **36 símbolos**, el mismo total que tras §35: esta ronda
**no introdujo nombres `P4_` nuevos**. El desglose es **13 nombres de requisito con
prefijo `P4_`** más **23 indicadores de estado / etiquetas de adjudicación** — §35.5
corregido in-place (`13 + 23 = 36`, no `17 + 19`; ver D0-R2.8 abajo).

La lista de **requisitos** no se toma del grep: se normaliza contra §29.8 + §32.6 + §34 y
sigue siendo la de **17** (13 con prefijo `P4_` + 4 sin prefijo). **La ronda 2 no agregó
requisitos P4**: agregó decisiones dentro de requisitos ya registrados.

| # | P4 REGISTERED REQUIREMENT | §29 STATUS | DECISIÓN | ¿BLOQUEA IMPLEMENTACIÓN? | DESIGN STATUS |
|---|---|---|---|---|---|
| 1 | `P4_CROSS_PROCESS_LOCK` | OPEN (ampliado) | §34.3 | no | **DESIGN_CLOSED** |
| 2 | `P4_LONG_RUNNING_CANCELLATION` | OPEN (nuevo) | §35.8.1 + **§36.7** | no | **DESIGN_CLOSED** |
| 3 | `P4_DURABLE_TRANSITION` | OPEN (ampliado) | §34.4 + §35.6 + **§36.6** | no | **DESIGN_CLOSED** |
| 4 | `P4_APPROVAL_SCOPE` | DESIGN CLOSED / IMPL OPEN | §34.6 + §35.2 + **§36.3/§36.5** | no | **DESIGN_CLOSED** |
| 5 | `P4_RUNTIME_SETUP_PROVISIONING` | OPEN (nuevo) | §35.8.2 + **§36.7** | no | **DESIGN_CLOSED** |
| 6 | `P4_RUNTIME_SETUP_ARTIFACT_AVAILABILITY` | OPEN | §34.11 | no (P4 no activa sin payload) | **DEFERRED_FAIL_CLOSED** |
| 7 | `P4_CLONE_ACTIVATION_GATE` | OPEN (nuevo) | §35.8.3 | no | **DESIGN_CLOSED** |
| 8 | `P4_CLONE_PROVISIONING_LIFECYCLE` | OPEN (nuevo) | §35.8.4 + **§36.7** | no | **DESIGN_CLOSED** |
| 9 | `P4_ROLLBACK_TARGET_HISTORY` | OPEN (nuevo) | §35.8.5 + **§36.2/§36.8** | no | **DESIGN_CLOSED** |
| 10 | `P4_RUNTIME_CLONE_RECORD_INTEGRITY` | OPEN | §34.10 | no | **DESIGN_CLOSED** |
| 11 | `P4_STARTUP_RECONCILIATION` | (§34) | §34.5 + §35.3 + **§36.4** | no | **DESIGN_CLOSED** |
| 12 | `P4_REVERIFY_AFTER_APPROVAL` | (§29.8) | §34.7 | no | **DESIGN_CLOSED** |
| 13 | `P4_P5_COMPATIBILITY_GATE` | (§34) | §34.14 + §35.7 + **§36.3** | no | **DESIGN_CLOSED** |
| 14 | `GENERATION_METADATA_INTEGRITY` | §32.6 OPEN | §34.8 | no | **DESIGN_CLOSED** |
| 15 | `RUNTIME_SETUP_MANIFEST_INTEGRITY` | §32.6 OPEN | §34.9 | no | **DESIGN_CLOSED** |
| 16 | `MANDATORY_CRITICAL_EXPECTATIONS` | §32.6 OPEN | §34.12 | no | **DESIGN_CLOSED** |
| 17 | `POST_ACTIVATION_LINK_INJECTION` | §32.6 OPEN | §34.13 + §35.4 | no | **DESIGN_CLOSED** |

```text
P4_REQUIREMENTS_DISCOVERED           = 17
P4_REQUIREMENTS_DESIGN_CLOSED        = 16
P4_REQUIREMENTS_DEFERRED_FAIL_CLOSED = 1   (RUNTIME_SETUP_ARTIFACT_AVAILABILITY)
P4_REQUIREMENTS_OPEN                 = 0
```

**D0-R2.8 (finding nuevo, P3, cerrado).** §35.5 afirmaba que los 36 símbolos se
descomponían en "17 requisitos más 19 indicadores". El comando documentado no devuelve 17
nombres de requisito con prefijo `P4_` —devuelve **13**— y los indicadores son **23**, no
19. La suma (36) era correcta por coincidencia; el desglose describía mal ambos sumandos.
Corregido in-place en §35.5. No es un blocker de diseño: es un defecto de contabilidad del
censo, del mismo tipo que la ronda 1 ya había corregido una vez.

**Fuera de alcance P4 (P5/P6, no cuentan para readiness de P4):**
`P5_EFFECTIVE_RUNTIME_ORACLE`, `P5_PATH_SURFACES_UNIT`, `P5_QUIESCENCE`,
`P6_STEAM_STATUS_NOTICE` — `OUT_OF_SCOPE_P5`.

**Regla de readiness.**

```text
P4_READY_TO_IMPLEMENT = YES  sólo si TODOS los requisitos P4 conocidos están
                             DESIGN_CLOSED, o demostrablemente DEFERRED_FAIL_CLOSED
                             Y no requeridos para un P4 core seguro.
```

`RUNTIME_SETUP_ARTIFACT_AVAILABILITY` cumple: sin payload P4 **no activa**; lo que queda
sin prometer es la *garantía de rollback*, no el core. `CREATION_CLUB_CLASSIFICATION`
(`DEFERRED_PENDING_EVIDENCE`) y `P3B` (`DEFERRED_PENDING_RIG`) tampoco bloquean: P4
responde `UNKNOWN → NO ACTIVATION`.

### 36.14 Revisión adversarial local — 20 preguntas

| # | Pregunta | Estado | Fundamento |
|---|---|---|---|
| 1 | ¿Puede `previous_activation_target` cambiar antes de POST PASS? | **CLOSED_BY_DESIGN** | §36.2: punto de escritura único = FINALIZE |
| 2 | ¿Hay alguna sección vieja que todavía diga lo contrario? | **CLOSED_BY_DESIGN** | Corrección in-place de §7, §11, §12, §19, §30.6, §31.4, §32.3/§32.4; grep de verificación en §36.16 |
| 3 | ¿Una nueva transición puede comenzar con Desired != Effective? | **CLOSED_BY_DESIGN** | §36.3/§36.4: no; `INCONSISTENT_BASELINE → FAIL_CLOSED` |
| 4 | ¿`source_activation` representa el Effective real observado? | **CLOSED_BY_DESIGN** | §36.3: `effective_runtime_evidence` + triple igualdad |
| 5 | ¿El runtime que se registra como previous target es exactamente el que estaba activo? | **CLOSED_BY_DESIGN** | §36.2 + §36.3: el source se demuestra contra el Effective observado antes de aprobar |
| 6 | ¿Una aprobación vieja puede reutilizarse después de rotar el journal? | **CLOSED_BY_DESIGN** | §36.5: `approval_seq` monótono fuera del journal |
| 7 | ¿Existe una autoridad durable del consumo de aprobación? | **CLOSED_BY_DESIGN** | §36.5: `state/approval_consumption.json`, acotado |
| 8 | ¿Puede crearse T2 mientras T1 sigue PENDING? | **CLOSED_BY_DESIGN** | §36.6: `AT MOST ONE PENDING PER ROOT` |
| 9 | ¿CAS define `NONE → PENDING`? | **CLOSED_BY_DESIGN** | §36.6: `CREATE` (∈ {ABSENT, NONE, FINALIZED}) |
| 10 | ¿CAS define `FINALIZED(T1) → PENDING(T2)`? | **CLOSED_BY_DESIGN** | §36.6: `START_NEXT` con `finalized_from_transition_id == Tprev` |
| 11 | ¿Un journal corrupto puede interpretarse como `NONE`? | **CLOSED_BY_DESIGN** | §36.4 N2: nunca; `FAIL_CLOSED` |
| 12 | ¿`CANCELLED` es lifecycle del Clone accidentalmente? | **CLOSED_BY_DESIGN** | §36.7: es resultado de operación; FSM sin cambios |
| 13 | ¿Provisioning failure tiene una sola semántica? | **CLOSED_BY_DESIGN** | §36.7: `INVALID`; retry = Clone nuevo |
| 14 | ¿Un Clone parcialmente provisionado puede activarse? | **CLOSED_BY_DESIGN** | §36.7 + §35.8.3: `lifecycle == PROVISIONED` obligatorio |
| 15 | ¿Cancel after PENDING puede borrar la transición? | **CLOSED_BY_DESIGN** | §36.12: no borra, no reclama rollback, no declara limpio |
| 16 | ¿Startup con `NONE + Desired != Effective` bloquea? | **CLOSED_BY_DESIGN** | §36.4 N1: sí, sin depender de `transition.json` |
| 17 | ¿Hay dos autoridades distintas para `previous_activation_target`? | **CLOSED_BY_DESIGN** | §36.8: autoridad única (registro finalizado); `active.json` corregido |
| 18 | ¿Approval authority y lock owner siguen separados? | **CLOSED_BY_DESIGN** | §36.5: emisor ≠ consumidor; `lock_session_id != human identity` |
| 19 | ¿P5 `UNKNOWN` sigue impidiendo completar la activación? | **CLOSED_BY_DESIGN** | §35.7 + §36.3: además impide **crear** una transición nueva |
| 20 | ¿El Clone mutable sigue sin poder convertirse en recovery authority? | **CLOSED_BY_DESIGN** | §32.2 + §33.3: `source Generation != VALID ⇒ RECHAZADO` |

```text
OPEN_BLOCKER = 0
```

### 36.15 Estado

```text
D0_R2_1_PREVIOUS_TARGET_ORDERING       = CLOSED
D0_R2_2_EFFECTIVE_SOURCE_BASELINE      = CLOSED
D0_R2_3_NO_PENDING_SPLIT_BRAIN         = CLOSED
D0_R2_4_DURABLE_APPROVAL_SINGLE_USE    = CLOSED
D0_R2_5_JOURNAL_CREATE_REPLACE_CAS     = CLOSED
D0_R2_6_CANCELLATION_PROVISIONING_FSM  = CLOSED
D0_R2_7_PREVIOUS_TARGET_AUTHORITY      = CLOSED
D0_R2_8_CENSUS_ARITHMETIC              = CLOSED

PREVIOUS_TARGET_WRITE_POINT             = FINALIZE (post POST PASS)
PREVIOUS_TARGET_AUTHORITY               = registro de transición FINALIZED
NEW_TRANSITION_REQUIRES_COHERENT_SOURCE = YES
SOURCE_DESIRED_EFFECTIVE_MATCH_REQUIRED = YES
NO_PENDING_DESIRED_EFFECTIVE_MISMATCH   = INCONSISTENT_BASELINE → FAIL_CLOSED
APPROVAL_DURABLE_IDENTITY               = approval_id (UUIDv4) + approval_seq monótono
APPROVAL_REPLAY_AFTER_JOURNAL_ROTATION  = IMPOSSIBLE_BY_CONTRACT
MAX_PENDING_TRANSITIONS_PER_ROOT        = 1
JOURNAL_CREATE_CAS                      = ABSENT|NONE|FINALIZED ∧ no PENDING ∧ consumo atómico
JOURNAL_UPDATE_CAS                      = (transition_id, journal_revision)
JOURNAL_NEXT_TRANSITION_CAS             = FINALIZED ∧ finalized_from_transition_id == Tprev

CANCELLED_IS_CLONE_LIFECYCLE   = NO
CANCELLED_IS_OPERATION_OUTCOME = YES
PROVISIONING_FAILURE_LIFECYCLE = INVALID
PROVISIONING_RETRY_POLICY      = fresh Clone

CRASH_MATRIX_REVALIDATED         = YES (C0..C7 + N0/N1/N2)
NO_TRANSITION_MATRIX             = YES (N0/N1/N2)
APPROVAL_MATRIX_REVALIDATED      = YES
PROVISIONING_CANCELLATION_MATRIX = YES

P4_REQUIREMENTS_DISCOVERED           = 17
P4_REQUIREMENTS_DESIGN_CLOSED        = 16
P4_REQUIREMENTS_DEFERRED_FAIL_CLOSED = 1
P4_REQUIREMENTS_OPEN                 = 0

OPEN_P4_DESIGN_BLOCKERS = 0
NEW_FINDINGS            = 1   (D0-R2.8, P3, cerrado in-place en §35.5)

P4_READY_TO_DESIGN     = YES
P4_DESIGN_FROZEN       = YES
P4_READY_TO_IMPLEMENT  = YES
P4_IMPLEMENTED         = NO
P5_IMPLEMENTED         = NO
PR_SAFE_TO_MERGE       = NO
MERGE                  = NO
```

`P4_READY_TO_IMPLEMENT = YES` significa **"el diseño no bloquea"**, no "P4 existe".
`P4_IMPLEMENTED = NO` sigue siendo el estado real. Dos dependencias quedan **declaradas y
abiertas como implementación**, no como diseño: el **oráculo de Effective Runtime** (P5)
y la **emisión de `approval_id`/`approval_seq`** por la superficie HITL.

### 36.16 Verificación de esta ronda

Comandos de control usados para cerrar los residuos (reproducibles sobre el HEAD de esta
ronda):

```bash
# el orden viejo no debe aparecer en ninguna sección normativa
rg -n "previous_activation_target.*POST|POST.*previous_activation_target" \
   docs/adr/0012-frozen-runtime.md

# CANCELLED y PROVISIONING_FAILURE con una sola semántica
rg -n "CANCELLED|PROVISIONING_FAILURE" docs/adr/0012-frozen-runtime.md

# estados vigentes, sin YES adelantados
rg -n "P4_READY_TO_IMPLEMENT|P4_DESIGN_FROZEN" docs/adr/0012-frozen-runtime.md

# censo
grep -oE "P4_[A-Z0-9_]+" docs/adr/0012-frozen-runtime.md | sort -u | wc -l
```

Las coincidencias del primer comando son, todas, las formulaciones **corregidas**
("nunca antes del POST", "sólo tras POST OK") o los registros históricos de §32/§35 que
ya describían el orden correcto. No queda ninguna sección normativa que escriba el target
antes del POST.

**Alcance de esta ronda:** docs-only. `PRODUCT_CODE_CHANGED = NO`. No se implementó
transición, ni CAS, ni journal, ni autoridad de consumo de aprobación, ni escaneo de
namespace, ni provisioning, ni cancelación, ni gate de activación, ni `active.json` real,
ni binding de MO2, ni setup de SKSE, ni cache de artefactos, ni rollback, ni promoción. No
se tocó P5. `MERGE = NO`.

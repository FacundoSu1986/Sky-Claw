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
    versiones, hashes y operaciones. Un Clone recién creado desde una Generation
    **no es jugable por sí solo**; el rollback re-materializa el runtime desde
    `Generation (versión) + RuntimeSetupManifest (setup)` (§12, §30.2).
23. `SFR-23` **Preparación ≠ activación.** Publicar una Generation, instanciar un
    Runtime Clone y provisionar su RuntimeSetup son operaciones **no activas** y
    **no requieren aprobación**, siempre que no toquen el Effective Runtime. La
    aprobación explícita (SFR-08) se pide **después** del gate y queda ligada a un
    conjunto exacto de artefactos (`ApprovalScope`, §11), que se **re-verifica**
    justo antes de mutar el Effective Runtime. Cancelar deja el runtime activo
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
| **RuntimeCloneRecord** | Identidad **lógica registrada** de un Runtime Clone: `clone_id`, `source_generation_id`, `runtime_setup_id`, `root_path`, `admitted_role = "runtime_clone"`. Es la autoridad para decidir si un target operativo es admisible (SFR-19/§30.5); **no** un prefijo de ruta, porque la ubicación física de los Clones sigue abierta (Q18). |
| **Desired Generation / Desired Clone** | Estado persistente de Sky-Claw (`state/active.json` v2): **dos** identificadores, `desired_generation_id` y `desired_clone_id`. Sólo el par identifica sin ambigüedad el Effective Runtime pretendido, porque una Generation admite varios Clones (§30.3). |
| **Effective Runtime** | La ruta que MO2/SKSE ejecutan **realmente** (game path efectivo): desde P0.4, `RuntimeCloneRecord.root_path` de un Runtime Clone. Puede divergir del Desired; esa divergencia es un defecto de promoción, no un éxito (SFR-16). |
| **ApprovalScope** | Conjunto exacto de artefactos al que queda ligada la aprobación del propietario: `candidate_id`, `generation_id`, `clone_id`, `runtime_setup_id`, `compatibility_evidence_id`. Antes de mutar el Effective Runtime se **re-verifica** ese conjunto, no "la intención de actualizar" (SFR-23; §30.6). |
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
    ↓   instanciar Runtime Clone C desde G   (RV-2 → RV-3)
    ↓   provisionar RuntimeSetupManifest S sobre C   (SFR-22)
    ↓   gate de activación de C   (§29.3)
  PREPARADO  (nada activo cambió; SFR-23)
    ↓   USER APPROVES   (SFR-08; ApprovalScope = {G, C, S, evidencia de compatibilidad})
    ↓   re-verificar EXACTAMENTE el conjunto aprobado   (SFR-23; §30.6)
    ↓   bind Effective Runtime → C   (primitiva P5)
    ↓   verify effective path == C.root_path  y  linaje C → G
    ↓   persist Desired Generation = G  y  Desired Clone = C
    ↓   POST verify de coherencia   (SFR-16)
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
| **Runtime Clone** | Derivado de una Generation `VALID`; **mutable** | Creado con RV-2 → RV-3, con linaje y setup **registrados** (`RuntimeCloneRecord`), físicamente independiente de la Generation y de la Managed Source (SFR-18). Su deriva por uso normal es **esperada** y se reporta (SFR-20). Lo bloquean: archivo **crítico** alterado, objeto de filesystem compartido, o fallo de identidad de runtime (§29.3). Un Clone **recién creado y sin provisionar no es jugable**. |
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
  instantiate C                # Runtime Clone desde G (RV-2 → RV-3); SFR-20
  provision C                  # RuntimeSetupManifest S (SFR-22): SKSE, root files, componentes
  gate C                       # activación: §29.3  →  PREPARADO

FASE 2 — ACTIVACIÓN (muta el Effective Runtime; requiere aprobación)
  user approves                # SFR-08; ApprovalScope = {G, C, S, evidencia}
  reverify ApprovalScope       # re-verificar EXACTAMENTE lo aprobado (SFR-23)
  bind Effective Runtime → C   # primitiva P5 (re-apuntar gamePath/SKYRIM_PATH, o alias)
  verify effective path == C.root_path  y  linaje C → G
  persist Desired Generation = G  y  Desired Clone = C   # active.json v2, temp + os.replace
  POST verify de coherencia    # SFR-16
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
   Si el propietario cancela, el runtime activo queda **idéntico** y los artefactos
   preparados permanecen **inactivos** (su limpieza es GC explícito, §23).
4. **La aprobación se liga a un conjunto exacto de artefactos**, no a la intención
   de actualizar (`ApprovalScope`): `candidate_id`, `generation_id`, `clone_id`,
   `runtime_setup_id`, `compatibility_evidence_id`. Antes de mutar el Effective
   Runtime se **re-verifica ese conjunto** (digest/identidad frescos y
   correspondencia con lo aprobado). Si algo cambió entre la aprobación y el
   binding, **no se activa**: se vuelve a pedir aprobación. Esto cierra
   simultáneamente `P4_APPROVAL_SCOPE` y `P4_REVERIFY_AFTER_APPROVAL` (§30.6).
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
7. **POST verify**: observar de nuevo el Effective Runtime y confirmar
   `root_path == C.root_path` y coherencia con el par desired. Si no, se revierte
   (F5/F9).
8. **Fail-closed sin observación**: hasta que P5 entregue la primitiva de
   observación del Effective Runtime (pregunta abierta Q12), la promoción **no
   puede declarar `SUCCESS`**. P4 no debe sustituir el paso de verificación de
   coherencia por un supuesto: sin observación, el desenlace es `FAILED`/
   `PENDING`, nunca éxito asumido (SFR-16).

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
  clonado desde una Generation **no es jugable**: le falta SKSE, los archivos de
  root y el resto del setup operativo, que `ensure_skse` y compañía instalan
  **dentro del game dir** —es decir, dentro del Clone, por diseño—. Sin la segunda
  autoridad, "rollback" reconstruiría el snapshot base pero **no el Skyrim
  jugable** (§30.2). El manifest declara versiones, hashes, procedencia y
  operaciones reproducibles; no necesariamente duplica el payload.

```text
Generation G_prev              # QUÉ versión de Skyrim es
    │
    ▼  RV-2 → RV-3
Runtime Clone limpio           # snapshot base, NO jugable todavía
    │
    │ RuntimeSetupManifest S    # QUÉ necesita esa versión para ser nuestro runtime
    ▼  provisión reproducible (SKSE, root files, componentes)
gate de activación (§29.3) + gate de compatibilidad
    │
    ▼
Runtime Clone jugable          # Effective Runtime
```

Secuencia exacta (vía de integridad, la canónica):

1. Operador elige una Generation retenida `G_prev`.
2. **Re-verificar `G_prev`** (identidad fresca + `tree_digest` registrado +
   archivos críticos + integridad física, P2-B2). Si `G_prev` está `DRIFTED` o
   `INVALID`, **no es fuente de clonación**: falla cerrado o se elige otra
   Generation retenida verificada (SFR-17; F10).
3. **Instanciar un Runtime Clone nuevo** `C_prev` desde `G_prev` (RV-2 → RV-3).
4. **Provisionar `C_prev` con su `RuntimeSetupManifest`** (SFR-22): aplicar las
   operaciones reproducibles del manifest —SKSE del build exacto, archivos de
   root, componentes— y verificar sus hashes/identidades declaradas.
5. Aplicar el gate de activación de §29.3 a `C_prev` (identidad, críticos,
   independencia, quiescencia).
6. Bind Effective Runtime → `C_prev` (primitiva P5, misma que promotion).
7. Verificar `effective path == C_prev.root_path` y linaje
   `C_prev.source_generation_id == G_prev`.
8. Persistir Desired Generation = `G_prev` **y** Desired Clone = `C_prev`
   (atómico, `active.json` v2).
9. POST verify de coherencia desired/effective (SFR-16).

**Vía rápida opcional (no autoridad).** Reactivar un Runtime Clone anterior
**retenido** `C_prev_old` —con el setup del usuario (SKSE, ENB) intacto— es más
barato que re-clonar, y es legítimo **sólo** si: (a) pasa el gate de §29.3 con la
evidencia crítica **registrada en la metadata de su Generation de origen** (no con
su propia medición); (b) su Generation de origen sigue `VALID`, de modo que la
reparación por re-clonado siga disponible; y (c) su `runtime_setup_id` declarado
corresponde al manifest de esa Generation —o la diferencia se **declara** en el
reporte—. Si (b) no se cumple, la activación es posible pero debe **declarar** que
la ruta de reparación no está disponible: nunca se reporta como equivalente a la
vía canónica. **Un Clone retenido no reemplaza a la Generation como autoridad
recuperable.**

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
| **F7** | El usuario decide volver atrás | runtime anterior jugable de nuevo | Rollback = re-materializar un Runtime Clone desde la Generation retenida **+ provisionar su RuntimeSetupManifest** + activar (§12; SFR-10/22). No es un simple "repuntar": un Clone recién clonado no es jugable sin setup. |
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

- `desired_generation_id: null` **y** `desired_clone_id: null` = arranque limpio
  (sin runtime activo). Un par **parcialmente** nulo (uno sí, otro no) es estado
  **corrupto**, no "limpio": fail-closed.
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
  "runtime_setup_id": "skyrimse-1.6.1170--s3",
  "root_path": "<FrozenRuntimeRoot>/clones/<clone-id>",
  "admitted_role": "runtime_clone",
  "created_at_ns": 0
}
```

- `admitted_role` es la autoridad del gate (SFR-19/§30.5): un target operativo sólo
  es admisible si `admitted_role == "runtime_clone"`, `target != Generation` y el
  linaje es válido. **No** se usa un prefijo de ruta: dónde viven físicamente los
  Clones sigue abierto (Q18).
- `root_path` es informativo y **se re-verifica** contra la realidad en el gate; un
  `root_path` que no coincide con el árbol observado no admite el target.

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
| **P4** | Explicit Promotion + Rollback | Sin copia sobre el runtime activo; F4/F5/F7/F9/F10 cubiertos; coherencia del par desired/effective probada (SFR-16) y re-verificación anti-DRIFTED (SFR-17). Con P0.4: publica la Generation, **instancia el Runtime Clone** (RV-2 → RV-3), **provisiona su `RuntimeSetupManifest`** (SFR-22), aplica el gate de activación (§29.3), implementa `ApprovalScope` + re-verificación pre-binding (SFR-23) y hace rollback **re-materializando el runtime operativo** desde Generation + RuntimeSetup (§12). Incluye la **migración fail-closed `active.json` v1 → v2** (§19). |
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
| P7 | Rig: Steam actualiza (F1) sin tocar Frozen; rollback real (F7). |

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
12. Un Clone recién creado **no** se declara jugable sin su RuntimeSetup aplicado y
    verificado (SFR-22; §30.2).

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
    resolverlo); (c) **approval scope**: la aprobación se liga al conjunto exacto
    `ApprovalScope = {candidate_id, generation_id, clone_id, runtime_setup_id,
    compatibility_evidence_id}` (digest/evidencia/operación/single-use/expiry),
    **no** a "quiero actualizar" (§11 regla 4; §30.6); (d)
    **reverify-after-approval**: `approval → re-verificar EXACTAMENTE el
    ApprovalScope (Generation fresca + Clone + RuntimeSetup + compatibilidad) →
    mutación`; (e) **P4 no puede promover sin el gate de compatibilidad
    disponible** (`UNKNOWN != COMPATIBLE`, implementación P5).
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
| (P0.4) ¿Un Clone recién clonado ya es jugable? | **No**: le falta SKSE y el resto del setup operativo, que se instala **dentro del game dir** —o sea, dentro del Clone—. Por eso el rollback **provisiona** desde el `RuntimeSetupManifest` (SFR-22; §30.2). |
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
FRESH_CLONE_IS_JUGABLE           = NO    (§12; §30.2)
RETAINED_CLONE_FAST_ROLLBACK     = OPTIONAL / NOT_AUTHORITY   (§12)
APPROVAL_BINDS_TO                = ApprovalScope, no a "quiero actualizar"   (SFR-23)
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
vedaban reactivar una Generation `DRIFTED`. Ejecutar **es** derivar, así que el
rollback quedaba bloqueado justo por el uso normal. Separar referencia de copia
operativa elimina la contradicción en lugar de administrarla con una política de
"deriva benigna" (que sería un catálogo abierto y frágil).

**Lo que NO cambia:** P1–P3 (código y contratos), SFR-01..18 salvo la lectura de
SFR-03/16/17, la exclusión de GP2 (§16), la ausencia de ACL mutation y de
auto-promoción, y la prohibición de tocar el estado interno de Steam (SFR-11).

### 29.2 Evidencia verificada (código, no intención)

| # | Afirmación | Dónde se verifica | Resultado |
|---|---|---|---|
| E1 | Ejecutar escribe el árbol ejecutado | audit del rig §7/§9 (`GOLDEN_EXECUTION_CAN_CAUSE_WRITES = PROVEN`) + §29.4 (escritores del game dir) | **CONFIRMADO** |
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

0. **Admisión por identidad lógica** (§30.5): el target operativo debe tener un
   `RuntimeCloneRecord` con `admitted_role == "runtime_clone"`, linaje válido
   (`source_generation_id` verificable y `VALID`) y `root_path` coincidente con el
   árbol observado. **No** se admite por prefijo de ruta: la ubicación física del
   Clone sigue abierta (Q18), así que el gate no puede depender de dónde vive.
1. **Identidad de runtime fresca** (`observe_runtime_identity_from_root`) igual a la
   registrada en la metadata de la Generation de origen.
2. **Archivos críticos** iguales a la evidencia crítica registrada en la metadata de
   la Generation (hoy sólo `SkyrimSE.exe`; catálogo en Q19). Las expectativas se
   **pasan explícitamente** desde la metadata: nunca quedan en el default `()` de
   RV-2/RV-3, que desactiva el chequeo en silencio (§29.10-20).
3. **Independencia física** respecto de la Generation **y** de la Managed Source
   (`verify_physical_independence` de RV-3 y/o `verify_generation_independence`),
   sin objetos de filesystem compartidos.
4. **RuntimeSetup aplicado y verificado** (SFR-22): el Clone declara un
   `runtime_setup_id` y sus componentes/hashes declarados coinciden con lo
   observado. Un Clone **sin setup provisionado no es un runtime jugable** y no se
   activa (§30.2).
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

### 29.4 Superficies del game path (6) y escritores del game dir (>1)

**Superficies que determinan la Effective Runtime — censo verificado (6):**

| # | Superficie | Dónde |
|---|---|---|
| 1 | `skyrim_path` persistido (zero-config) | `local/auto_detect.py::AutoDetector._find_skyrim_inner`, `app_context.py::start_full` (`escribir_campo(local_cfg, "skyrim_path", …)`) |
| 2 | `SKYRIM_PATH` en el entorno | `app/gui/_bootloader.py::_hydrate_tool_env_from_snapshot` (`os.environ.setdefault`, un valor del operador gana); `path_resolver.py::get_skyrim_path` lo lee |
| 3 | Raíces del sandbox de `PathValidator` | `app_context.py::_construir_raices_sandbox` |
| 4 | Snapshot del `EnvironmentScanner` y su paridad con `AutoDetector` | `local/discovery/scanner.py` (la ruta configurada gana si contiene el ejecutable) + `tests/test_paridad_deteccion_skyrim.py` |
| 5 | `gamePath` de MO2 | `ModOrganizer.ini` como `@ByteArray` de Qt; `path_resolver.py` documenta explícitamente que **no** lo decodifica |
| 6 | Ejecutables de MO2 con ruta absoluta y `moshortcut://SKSE` | `local/mo2/vfs.py::MO2Controller.launch_game` (`moshortcut://SKSE`) |

```text
GAME_PATH_SURFACES_COUNT   = 6
P5_BINDING_SCOPE_VERIFIED  = NO   (el censo está verificado; el binding no existe)
```

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
| `P4_LONG_RUNNING_CANCELLATION` (nuevo) | P4 | `frozen_runtime` es 100 % síncrono (E9), sin cancelación ni progreso. `crear_candidate` hace varios recorridos SHA-256 completos + copia con `fsync`; instanciar un Clone con RV-2/RV-3 suma otros. Requisito: fachada async con executor dedicado, token de cancelación cooperativo, progreso hacia el event loop, single-flight por root, y cancelación que termina en un estado explícito (`INVALID`/`CANCELLED`), nunca en un `BUILDING` huérfano. **No se refactoriza a asyncio en P0.4.** |
| `P4_DURABLE_TRANSITION` (ampliado) | P4 | La intención durable cubre la secuencia completa: publicar Generation, instanciar Clone y binding. |
| `P4_APPROVAL_SCOPE` (ampliado) | P4 | La aprobación se liga al `ApprovalScope` exacto —`candidate_id`, `generation_id`, `clone_id`, `runtime_setup_id`, `compatibility_evidence_id`— y se re-verifica antes de mutar el Effective Runtime (SFR-23). "El propietario" debe definirse: la capa del agente LLM es lock-only y el HITL de la GUI documenta que una solicitud sin pestaña lanzadora queda sin dueño. |
| `P4_RUNTIME_SETUP_PROVISIONING` (nuevo) | P4 | Implementar SFR-22: registrar el `RuntimeSetupManifest` por versión, provisionar el Clone de forma reproducible (SKSE del build exacto, root files, componentes) y verificar sus hashes declarados. Sin esto el rollback no reconstruye un runtime **jugable** (§30.2). Incluye declarar `assumptions` para lo no clasificado (Creation Club, Q22). |
| `P4_CLONE_ACTIVATION_GATE` (nuevo) | P4 | Implementar §29.3, incluyendo el catálogo de críticos (Q19) y el **reporte** de deriva (no sólo el veredicto). Además: (a) las expectativas críticas deben **pasarse explícitamente** desde la metadata —nunca quedar en el default `()` de RV-2/RV-3, que desactiva el chequeo en silencio (§29.10-20)—; (b) el gate debe rechazar reparse points que **escapen** del Clone, no sólo comparar inodos contra el origen (§29.10-21). |
| `P5_EFFECTIVE_RUNTIME_ORACLE` (ampliado) | P5 | El bridge MO2 no expone hoy ninguna información de ruta (la operación `health` sólo emite `bridge_health`). Una extensión de **sólo lectura** es la candidata; la API de MO2 no está verificada. Sin oráculo, la promoción queda `PENDING` (SFR-16). El plugin no gana operaciones mutantes (ADR 0007). |
| `P5_PATH_SURFACES_UNIT` (nuevo) | P5 | Binder transaccional de las 6 superficies de §29.4 **más** los escritores del game dir, con rollback, con MO2 cerrado, y ancla de igualdad literal del inventario. |
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
| **D9** | Seis superficies del game path | **ACCEPT_WITH_CHANGES** | Las 6 existen (§29.4). Se **rechaza** la afirmación acompañante de que `ensure_skse` es el único mutador del game dir: hay al menos 5 más, una de ellas (`grass_cache_runner`) con evidencia directa en código. |
| **D10** | SFR-21 sin pasos manuales repetidos | **ACCEPT_WITH_CHANGES** | Se acepta como **requisito de producto** (SFR-21) y criterio de P7; **no** se declara demostrado (§29.7). |
| **D11** | Catálogo de archivos críticos | **ACCEPT_WITH_CHANGES** | E11: hoy sólo `SkyrimSE.exe`. Extender + re-baseline con aprobación explícita (Q19, `P4_CLONE_ACTIVATION_GATE`). |
| **D12** | Quiescencia antes del binding | **ACCEPT** | E14: no hay primitiva reutilizable; el sensor es nuevo y es de P5 (`P5_QUIESCENCE`). |
| **D13** | Cancelación/progreso para operaciones largas | **ACCEPT** | E9: 100 % síncrono. Asignado a P4 (`P4_LONG_RUNNING_CANCELLATION`); **no** se refactoriza acá. |

### 29.10 Revisión adversarial propia

Cada pregunta cierra en un estado explícito. Los `UNKNOWN` no se ocultan.

| # | Pregunta | Estado | Fundamento |
|---|---|---|---|
| 1 | ¿Puede Steam modificar el Runtime Clone? | **ANSWERED** | El Clone vive fuera de `steamapps/common`; la admisión de rutas y la independencia física (E6) lo impiden. La admisión de `clones/` es requisito de P4. |
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
| 16 | ¿Hay objetos de filesystem compartidos (hardlinks/junctions)? | **ANSWERED** | `verify_generation_independence` (Generation↔Managed Source), `verify_physical_independence` (Clone↔Generation), `copying.py` copia contenido (nunca `os.link`) y verifica `st_nlink == 1`. |
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
requeridos. Un "rollback" que termina ahí devuelve una carpeta que no arranca.

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

SFR-16 pasa de la ambigüedad `desired == effective == B` a algo comprobable:

```text
DesiredGeneration == G1
DesiredClone      == C3

Clone(C3).source_generation_id == G1

EffectiveRuntime  == C3.root_path
C3 pasa el gate de activación (§29.3)
```

Sólo entonces `SFR16_COHERENT = YES`.

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
AND gate de activación válido
```

Eso permite que mañana el Clone viva en otro SSD sin romper el modelo.

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

La aprobación queda ligada a ese conjunto, no a "quiero actualizar":

```text
ApprovalScope:
    candidate_id
    generation_id
    clone_id
    runtime_setup_id
    compatibility_evidence_id
```

Y antes de mutar el Effective Runtime:

```text
reverify exact approved artifacts
```

Eso cierra simultáneamente `P4_APPROVAL_SCOPE` y `P4_REVERIFY_AFTER_APPROVAL`. Si el
propietario cancela:

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
 source_generation = G
 runtime_setup     = S
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

Coherencia activa:

```text
active.generation_id == C.source_generation_id
active.clone_id      == C.clone_id
effective_path       == C.root_path
C passes activation gate
approved artifact set == reverified artifact set
```

**Alcance de la ronda 2:** docs-only. `PRODUCT_CODE_CHANGED = NO`. No se
implementaron SFR-22 ni SFR-23; no se escribió el adaptador de metadata; no se tocó
`active.json`, ni el estado, ni MO2. No se agregó evidencia de código nueva a §29.2:
esta ronda es adjudicación y corrección de contrato sobre la evidencia ya verificada
en la ronda 1, más el razonamiento de `ensure_skse` → `install_dir` (ya citado en
§29.4) que fundamenta F1.

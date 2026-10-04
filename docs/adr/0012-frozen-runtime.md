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
**Contexto de origen:** `origin/main` `0103ee4f6de15207032d25c254ede5cf2c01bff9`
(merge de RV-GP2/S4D, PR #666).
**Relación con GP2-S4E:** el workstream archivado (rama
`feat/runtime-vault-s4e-production-wiring`, PR #669, issue #671) queda **pausado y
diferido**; este ADR **no** lo reanuda, **no** lo modifica y **no** depende de él.
Se lo referencia únicamente como contexto histórico del enfoque anterior.
**Invariante de alcance:** el único lugar de trabajo es
`E:\Skyclaw_Steam_Frozen_Runtime` en la rama `feat/steam-frozen-runtime`. `main` sólo
se lee.
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
para no migrar IDs ya documentados (`SFR-01..18`) en docs, tests e issues.

1. `SFR-01` Steam puede actualizar su propia instalación.
2. `SFR-02` Steam nunca administra el Frozen Runtime.
3. `SFR-03` MO2/SKSE/juego activo utilizan el Frozen Runtime.
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
16. `SFR-16` La promoción no está completa hasta que **Desired Active Generation** y
    **Effective Runtime** están probadamente coherentes. Un cambio exitoso de
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

## 5. No-objetivos (Non-goals)

- **No hay auto-promoción.** Aunque el Candidate sea válido, no se activa sin
  aprobación explícita del usuario (SFR-08). El agente/IA puede detectar, preparar,
  validar y recomendar; no autoriza por sí mismo.
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
- **No se decide la primitiva de binding** de la Effective Runtime (repuntar
  `gamePath`/`SKYRIM_PATH` vs alias estable vs otra) en P0. Se fija la invariante
  (SFR-16) y la semántica de falla (§11); la primitiva se decide en P5.
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
| **Frozen Runtime** | Feature de Sky-Claw: árbol de directorios independiente, fuera del árbol de juego administrado por la Managed Source. Contiene la versión que MO2/SKSE/juego usan. |
| **Candidate** | Copia candidata derivada de la Managed Source, aún no promocionada. Debe verificarse **contra `SourceSnapshotEvidence`** antes de promocionar. Nunca genera la evidencia contra la que se aprueba (SFR-15). |
| **Generation** | Snapshot inmutable y versionado del árbol completo del juego dentro del Frozen Runtime (`versions/<generation-id>/`). Lógicamente inmutable (SFR-17). |
| **Desired Active Generation** | Estado persistente de Sky-Claw (`state/active.json`): qué Generation pretende ser la activa. |
| **Effective Runtime** | La ruta que MO2/SKSE ejecutan **realmente** (game path efectivo). Puede divergir del Desired; esa divergencia es un defecto de promoción, no un éxito (SFR-16). |
| **DRIFTED** | Estado de una Generation cuyo árbol ya no coincide con su identidad registrada (`runtime_identity`/`tree_digest`). `DRIFTED != READY` y `DRIFTED != target de rollback` sin re-verificación (§12). La detección es **on-demand**: re-verificación de identidad antes de rollback/reactivación; no se promete monitoreo continuo (la Generation activa es escribible y puede derivar durante el uso normal). |
| **Promotion** | Acto explícito y autorizado de hacer que Desired Active Generation y Effective Runtime pasen a ser, **probadamente coherentes**, un Candidate verificado; sin sobreescribir la generación activa anterior (SFR-09/16). |
| **Rollback** | Hacer que Desired Active Generation y Effective Runtime pasen a ser, probadamente coherentes, una Generation anterior retenida y re-verificada. No reconstruye archivos (SFR-10). |

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
                │  PROMOCIÓN (SFR-08: aprobación explícita)
                │  NO copia sobre la activa (SFR-09)
                ▼
┌──────────────────────────────────────┐
│ FROZEN RUNTIME  (versión congelada)  │
│ <FrozenRuntimeRoot>/versions/<gen>/  │
│ Steam NO la administra (SFR-02)      │
│ MO2/SKSE/juego la usan (SFR-03)      │
└──────────────────────────────────────┘
                ▲
                │ Desired Active Generation: state/active.json   (intención persistente)
                │ Effective Runtime: game path que MO2/SKSE ejecutan
                │ promoción/rollback completos ⇔ desired == effective (SFR-16)
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
    ↓   USER APPROVES   (SFR-08)
    ↓   bind Effective Runtime → B
    ↓   verificar effective path/identity == B
    ↓   persist Desired Active Generation = B
    ↓   POST verify desired/effective coherentes   (SFR-16)
  SUCCESS
```

## 8. Fronteras de confianza (Trust boundaries)

| Superficie | Confianza | Regla |
|---|---|---|
| **Managed Source (provider: Steam)** | MUTABLE / *untrusted as active runtime* | Fuente válida para Candidate. Nunca `reference_only`. Nunca Golden. El proveedor escribe libremente en ella (SFR-01). |
| **SourceSnapshotEvidence** | Observación de la fuente, **no autoridad** | Evidencia sellada de la Managed Source estabilizada que se pretendía copiar. No es Golden, no tiene autoridad GP2 (SFR-15; §9.1). |
| **Candidate** | Derivada, no confiable hasta verificar | Se inventaría/verifica **contra `SourceSnapshotEvidence`**, nunca contra su propia medición (SFR-15). Estados `building → ready → invalid` (SFR-06/07). |
| **Frozen Runtime (Generation)** | Confiable tras verificación; **writable** | La versión está congelada; el árbol puede seguir siendo escribible por MO2/SKSE/runtime. Sky-Claw **no muta una Generation promocionada in-place**; si su árbol ya no coincide con su identidad registrada, la Generation está `DRIFTED` (SFR-17). No comparte objetos de filesystem mutables con la Managed Source (SFR-18). |
| **Desired Active Generation (`state/active.json`)** | Intención persistente de Sky-Claw | Escritura atómica (temp + `os.replace`). Por sí sola **no** declara promoción exitosa (SFR-16). |
| **Effective Runtime** (game path que MO2/SKSE ejecutan) | Autoridad de hecho | La promoción sólo es exitosa cuando Effective Runtime está probadamente apuntando a la misma Generation que Desired (SFR-16). |

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
en la Generation activa (F2/F3/F8).

## 11. Ciclo de vida de Promotion

Promotion es **completa** sólo cuando Desired Active Generation y Effective Runtime
están **probadamente coherentes** (SFR-16). Un `state/active.json` reescrito con
éxito, con MO2/SKSE ejecutando todavía otra Generation, es un **estado ambiguo, no
un éxito**.

Modelo (la primitiva concreta de binding se decide en P5; acá se fija la
invariante):

```text
previous = A
prepare B                    # publish C → versions/B (sin tocar A; SFR-09)
verify B                     # identidad + tree_digest + críticos (fresco)
user approves                # SFR-08
bind Effective Runtime → B   # primitiva P5 (repuntar gamePath/SKYRIM_PATH, o alias)
verify effective path/identity == B
persist Desired Active Generation = B   # state/active.json, temp + os.replace
POST verify desired/effective coherentes  # SFR-16
SUCCESS
```

Reglas:

1. **Re-verificación fresca de `B`** antes de tocar cualquier binding
   (`observe_runtime_identity_from_root` + `verify_tree` contra su `tree_digest`
   registrado + archivos críticos). Un `B` `DRIFTED` no se promueve (SFR-17).
2. **Sin copia sobre la activa** (SFR-09): `versions/B` es una Generation nueva; `A`
   no se borra (SFR-10).
3. **Falla parcial de binding ⇒ no hay éxito ambiguo ⇒ el runtime anterior es
   recuperable.** La promoción devuelve `SUCCESS` sólo con desired == effective ==
   `B` **demostrado**; cualquier otro desenlace es un estado explícito distinto
   (`PENDING`/`FAILED`) con su ruta de recuperación (re-verificar `A`, volver a
   bindear `A` si hizo falta). Nunca se reporta "promocionado" sobre un
   desired/effective divergente (SFR-16).
4. **Honestidad de la ventana**: bind-effective y persist-desired son dos pasos y
   entre ellos existe una ventana real (effective ya es `B`, desired todavía `A`).
   No se maquilla: la mitigación es que el estado persistente **registre la
   operación en curso** (o se ejecute y verifique en el orden que P5 elija), y que
   el desenlace sea `SUCCESS` sólo tras el POST-verify de coherencia. Si P5 elige
   persistir `desired` **antes** de bindear, el diseño debe incluir **rollback
   causal** explícito (revertir `desired` a `A` si el bind falla) — se documentará
   en P5, no se supone.
5. **POST verify**: observar de nuevo la Effective Runtime y confirmar identidad
   == `B` y coherencia con `desired`. Si no, se revierte a `A` (F5/F9).
6. **Fail-closed sin observación**: hasta que P5 entregue la primitiva de
   observación del Effective Runtime (pregunta abierta Q12), la promoción **no
   puede declarar `SUCCESS`**. P4 no debe sustituir el paso de verificación de
   coherencia por un supuesto: sin observación, el desenlace es `FAILED`/
   `PENDING`, nunca éxito asumido (SFR-16).

## 12. Rollback

```text
desired = effective = previous_generation   (probado, SFR-16)
```

Secuencia exacta:

1. Operador elige una Generation retenida `G_prev`.
2. **Re-verificar `G_prev`** (identidad fresca + `tree_digest` registrado +
   archivos críticos). Si `G_prev` está `DRIFTED`, **no es un target de rollback
   válido**: falla cerrado o se elige otra Generation retenida verificada
   (SFR-17; F10).
3. Bind Effective Runtime → `G_prev` (primitiva P5, misma que promotion).
4. Verificar effective path/identity == `G_prev`.
5. Persistir Desired Active Generation = `G_prev` (atómico).
6. POST verify de coherencia desired/effective (SFR-16).

No se reconstruyen archivos destruidos. El rollback **no depende de la Managed
Source**: se repunta a una Generation retenida y re-verificada, nunca se reconstruye
desde la fuente. Si `G_prev` no está retenida o está `DRIFTED`, el rollback falla
cerrado (por eso MVP **no** borra generaciones y **re-verifica** antes de
reactivar).

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
  `SkyrimSE.exe`/`SkyrimSELauncher.exe`) desde el root del Frozen Runtime.
- **¿SKSE resuelve DLLs desde ese root?** SKSE carga por ruta fija (loader + DLL
  de runtime del build exacto en la raíz del juego). `scanner.find_skse_installation`
  ya valida loader + `skse*.dll` compatibles con la versión exacta
  (`skse_dll_game_version`, `skyrim_version_matches`). Esto alimenta el gate
  `RUNTIME_COMPATIBILITY`.
- **¿Qué configuración cambiaría al promover?** Sólo el puntero del Frozen Runtime
  (Desired Active Generation) + el game path de MO2/SKYRIM_PATH (Effective Runtime),
  una sola vez por promoción; ambos deben quedar **probadamente coherentes**
  (SFR-16). La primitiva de binding de P5 debe cubrir **ambas superficies** (el
  `gamePath` de MO2 y `SKYRIM_PATH`) o demostrar que una deriva de la otra;
  actualizar una sola viola SFR-16 (patrón "dos superficies, un recurso" de
  `AGENTS.md`). **P0 no modifica** nada de esto; sólo lo documenta (F6: si es
  incompatible, no hay migración forzada).

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
| **F7** | El usuario decide volver atrás | activate previous generation | Rollback = repuntar (SFR-10). |
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
| **RV-2** | `golden.py` | `verify_golden_master` exige **evidencia independiente** (`expected_tree`, `expected_runtime`); `HASHING_A_FOLDER_DOES_NOT_MAKE_IT_A_GOLDEN_MASTER`. | **ADAPT/PARTIAL.** No usar `verify_golden_master` para admitir la Managed Source (no hay expectativa independiente; sería auto-confianza). Reusar sus bloques: `verify_critical_files`, `verify_runtime_identity`, `inventory_tree`, `tree_digest_from_files`. |
| **RV-3** | `clone.py` | `create_runtime_clone(golden_source: GoldenMasterVerificationResult, ...)` exige fuente **VERIFIED** con `descriptor.role == "reference_only"`; `RuntimeCloneResult` exige `source_golden` no nulo y `descriptor.runtime_identity == source_golden.runtime_identity`. Copia real, staging hermano, publish atómico no-clobber, independencia física. Sin dependencias de GP2. | **ADAPT.** El *motor de copia+verificación+publicación* es reutilizable, pero el contrato de autoridad de fuente no acepta una Managed Source. Opciones a decidir en P3: **(a)** adaptador fino que produzca un descriptor equivalente para el snapshot de la Managed Source; **(b)** rutina de copia mínima que reuse las primitivas RV-1 y las garantías de staging/publicación de RV-3. No se decide el mecanismo en P0. |

`RV-3` responde las preguntas de §9 del pedido: crea copia física real, preserva
archivos, maneja directorios vacíos (`_capture_directory_structure`), rechaza
links/reparse (fail-closed), verifica independencia física por inodo, y **no**
requiere GP2. Su limitación es la **autoridad de fuente** (Golden verificado), no la
copia.

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
   rechazan (SFR-18).
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

**a) Estado de intención** — `state/active.json` (SFR-16: registra sólo el
**Desired Active Generation**; NO registra Effective Runtime y jamás afirma por
sí solo promoción exitosa):

```json
{
  "schema_version": 1,
  "desired_active_generation": "1.6.1170__a1b2c3d4e5f6",
  "updated_at_ns": 1789000000000000000
}
```

- `desired_active_generation: null` = arranque limpio (sin Generation activa).
- Reader fail-closed: **ausente** = limpio; **presente pero corrupto**
  (JSON malformado/truncado, UTF-8 inválido, schema desconocido, campo ausente
  o con tipo incorrecto, generation-id con traversal) LANZA — nunca se
  interpreta como ausente.
- Escritura atómica: `mkstemp` en el MISMO directorio → `fsync` del archivo →
  `os.replace` → cleanup del temporal ante fallo (patrón del repo en
  `local_config.py`). El temporal nunca vive en `%TEMP%` global.
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

No hay base de datos; JSON canónico mínimo basta.

## 20. Layout de filesystem (decidido e implementado en P2)

```text
<FrozenRuntimeRoot>/                 # configurable; NUNCA dentro de steamapps/common
├── versions/
│   ├── 1.6.1170__<digest12>/        # Generation completa, versión congelada (SFR-17)
│   └── 1.7.xxxx__<digest12>/
├── candidates/                      # namespace reservado (P3 crea Candidates)
└── state/
    ├── active.json                  # Desired Active Generation (SFR-16)
    ├── .active.json.<rand>.tmp      # temporal de escritura atómica (mismo dir)
    └── generations/
        └── <generation-id>.json     # metadata inmutable por Generation
```

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
| **P2** | Frozen Runtime storage + modelo de Generation + admisión de rutas | Crear/listar generations; registro atómico; rechazo de destino dentro de Steam; identidad registrada por Generation (base de `DRIFTED`). **Implementado en PR #673** (`storage.py`/`state.py`/`generations.py`/`independence.py`/`generation_id.py`; Q5/Q6/Q10 resueltas; SFR-18 ejecutable con hardlink/junction/reparse; drift on-demand). |
| **P3** | Candidate creation + verification | Candidate `ready`/`invalid` contra `SourceSnapshotEvidence`; F2/F3/F8 cubiertos (SFR-15). |
| **P4** | Explicit Promotion + Rollback | Sin copia sobre activa; F4/F5/F7/F9/F10 cubiertos; coherencia desired/effective probada (SFR-16) y re-verificación anti-DRIFTED (SFR-17). |
| **P5** | MO2/SKSE integration (binding del game path + gate de compatibilidad) | F6; juego arranca desde Frozen Runtime. |
| **P6** | Update detection / user-facing status | Contrato de producto (§7). |
| **P7** | Windows real rig | Evidencia de F1/F7 en rig. |
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
| P2 | Admisión de rutas (destino dentro de Steam ⇒ rechazo; symlink/junction ⇒ rechazo); puntero atómico; registro enumerado (igualdad literal); detección de Generation `DRIFTED` contra su identidad registrada. **Implementado**: L01–L10 (layout/admisión), ST01–ST10 (estado), G01–G08 (generation-id), DR01–DR11 (drift), PI01–PI09 (SFR-18 con hardlink/junction reales) + ancla AST del boundary de escritura. |
| P3 | Candidate completo y fuente estable ⇒ ready; corrupción/truncamiento ⇒ invalid; fuente cambiada PRE/POST ⇒ `invalid / SOURCE_CHANGED` aunque el Candidate sea consistente (SFR-15); fallo a mitad ⇒ activa intacta (F2/F3/F8). Caso negativo anti-self-verification: Candidate **internamente consistente pero distinto** del `SourceSnapshotEvidence` ⇒ rechazado (el test prueba el camino de comparación, no sólo el resultado). |
| P4 | Promoción no borra previa; sin coherencia desired/effective no hay `SUCCESS` (F9); fallo de promoción revierte y `A` queda usable (F5); sin aprobación no promueve (F4); rollback repunta (F7); rollback sobre Generation `DRIFTED` falla cerrado (F10). |
| P5 | SKSE compatible/incompatible/unknown; game path repuntado y observado; MO2 arranca desde Generation activa; coherencia desired/effective verificable desde el rig. |
| P7 | Rig: Steam actualiza (F1) sin tocar Frozen; rollback real (F7). |

## 25. Definition of Done (del proyecto, no de P0)

1. Steam puede actualizar su instalación sin tocar el Frozen Runtime (F1).
2. Toda versión nueva entra como Candidate y se verifica contra
   `SourceSnapshotEvidence` antes de promocionar (SFR-06/07/15).
3. Promoción sólo con aprobación explícita (SFR-08), sin destruir la anterior
   (SFR-09) y con coherencia desired/effective **probada** antes de declarar
   éxito (SFR-16).
4. Rollback = repuntar, previa re-verificación de la Generation destino (SFR-10/17).
5. MO2/SKSE arrancan desde el Frozen Runtime (SFR-03).
6. Sin ACL mutation, sin helper privilegiado, sin GP2 (SFR-12/13).
7. `USES_ACL_MUTATION/FROZEN... = NO` (verificación de complejidad).
8. Documentación sincronizada y evidencia de rig registrada.
9. Ninguna Generation `DRIFTED` se reactiva ni se usa como target de rollback sin
   re-verificación exitosa (SFR-17).
10. Ninguna Generation comparte objetos de filesystem mutables con la Managed
    Source (SFR-18).

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

## 27. Revisión adversarial (auto-cuestionamiento)

| Pregunta | Respuesta |
|---|---|
| ¿Steam todavía puede tocar el Frozen Runtime? | No por diseño (árboles disjuntos). Verificación de admisión de rutas lo garantiza (P2). |
| ¿Alguna ruta está dentro de `steamapps/common`? | El layout lo prohíbe y la admisión lo rechaza fail-closed. |
| ¿Promotion puede destruir la activa antes de tener reemplazo válido? | No: re-verificación previa + publicación de nueva Generation + puntero atómico; nunca copia sobre la activa (SFR-09). |
| ¿Rollback depende de reconstrucción? | No: repunta a una Generation retenida (SFR-10). |
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

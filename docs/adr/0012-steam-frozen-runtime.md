# ADR 0012 — Steam Frozen Runtime: promoción de versiones aislada

**Fecha:** 2026-10-03
**Estado:** Propuesta (P0: diseño y censo; prohibida la implementación de código de
producción en este ADR). No cambia contratos existentes; sólo decide arquitectura.
**Contexto de origen:** `origin/main` `0103ee4f6de15207032d25c254ede5cf2c01bff9`
(merge de RV-GP2/S4D, PR #666).
**Relación con GP2-S4E:** el workstream archivado (rama
`feat/runtime-vault-s4e-production-wiring`, PR #669, issue #671) queda **pausado y
diferido**; este ADR **no** lo reanuda, **no** lo modifica y **no** depende de él.
Se lo referencia únicamente como contexto histórico del enfoque anterior.
**Invariante de alcance:** el único lugar de trabajo es
`E:\Skyclaw_Steam_Frozen_Runtime` en la rama `feat/steam-frozen-runtime`. `main` sólo
se lee.

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

| Eje | GP2-S4E (archivado) | Steam Frozen Runtime (este ADR) |
|---|---|---|
| Mecanismo | ACL mutation transaccional del Golden (`SetSecurityInfo`, WAL, helper elevado) | Aislamiento físico: la copia jugable vive fuera del árbol de Steam |
| Relación con Steam | Bloquear/negar la escritura de Steam sobre el Golden | Steam escribe libremente sobre su propia instalación |
| Complejidad | FSM privilegiada, journal durable, WAL, lock de kernel, store autoritativo, IPC autenticado | Copia → verificar → puntero de generación activa |
| Dependencias | GP1/GP2/S4-A..E | RV-1 (reuso), RV-2/RV-3 (reuso parcial), nada de GP2 |
| Threat model | Admin filtrado, handles de control preexistentes, power-loss | Actualización de Steam, promoción equivocada, candidate corrupto |

El threat model de GP2-S4E **no se hereda**. Este ADR protege contra las fallas de
§14 (F1–F7), no contra un administrador malicioso, atacante de kernel, tampering
con `SeDebugPrivilege`, crash durante `SetSecurityInfo` ni rollback transaccional
de ACL.

## 4. Objetivos (Goals)

1. `SFR-01` Steam puede actualizar su propia instalación.
2. `SFR-02` Steam nunca administra el Frozen Runtime.
3. `SFR-03` MO2/SKSE/juego activo utilizan el Frozen Runtime.
4. `SFR-04` Una actualización de Steam no modifica el Frozen Runtime activo.
5. `SFR-05` Steam Source nunca se convierte automáticamente en runtime activo.
6. `SFR-06` Toda nueva versión entra primero como Candidate.
7. `SFR-07` Candidate debe verificarse antes de poder promocionarse.
8. `SFR-08` Promotion requiere autorización explícita del usuario.
9. `SFR-09` Una promoción fallida nunca destruye la versión activa anterior.
10. `SFR-10` La versión anterior permanece disponible para rollback.
11. `SFR-11` No editar ni falsificar estado interno de Steam.
12. `SFR-12` No depender de ACL mutation para proteger el runtime activo.
13. `SFR-13` No utilizar GP2-S4E como dependencia necesaria.
14. `SFR-14` La solución debe ser comprensible y mantenible por un proyecto pequeño.

## 5. No-objetivos (Non-goals)

- **No hay auto-promoción.** Aunque el Candidate sea válido, no se activa sin
  aprobación explícita del usuario (SFR-08). El agente/IA puede detectar, preparar,
  validar y recomendar; no autoriza por sí mismo.
- **No hay garbage collection.** No se borra la generación anterior durante la
  promoción (SFR-10; §23). La retención/limpieza futura será otro slice con
  confirmación explícita.
- **No hay ACL hardening** del Frozen Runtime. "Frozen" significa **versión
  congelada**, no filesystem read-only (§15). Se prefiere aislamiento
  arquitectónico a bloqueo.
- **No hay helper privilegiado, UAC, `WRITE_DAC`, WAL ni journal de protección**
  (§21). Si en el futuro se propone reutilizar algo de esa lista, debe justificarse
  por qué el problema no se resuelve con `copia a nueva generación → verificar →
  actualizar puntero`.
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
| **Steam Source** | Instalación de Skyrim administrada por Steam (`steamapps/common/...`). Mutable. Fuente válida para crear Candidate. No es Golden. |
| **Frozen Runtime** | Árbol de directorios independiente, fuera del árbol de juego administrado por Steam. Contiene la versión que MO2/SKSE/juego usan. |
| **Candidate** | Copia candidata derivada de Steam Source, aún no promocionada. Debe verificarse antes de promocionar. |
| **Generation** | Snapshot inmutable y versionado del árbol completo del juego dentro del Frozen Runtime (`versions/<generation-id>/`). |
| **Active Generation** | Generation referenciada por el puntero activo (`state/active.json`); la que MO2/SKSE/juego deben usar. |
| **Promotion** | Acto explícito y autorizado de cambiar Active Generation a un Candidate verificado, sin sobreescribir la generación activa anterior. |
| **Rollback** | Cambiar Active Generation a una Generation anterior retenida. No reconstruye archivos. |

`Golden Master` sigue existiendo como concepto de Runtime Vault (RV-2); **no** es
sinónimo de Frozen Runtime.

## 7. Arquitectura

```text
┌──────────────────────────────────────┐
│ STEAM SOURCE  (mutable)              │
│ <library>/steamapps/common/          │
│   Skyrim Special Edition/            │
│ Steam actualiza libremente (SFR-01)  │
└───────────────┬──────────────────────┘
                │  snapshot / copy  (candidate creation)
                ▼
┌──────────────────────────────────────┐
│ CANDIDATE  (aislado, no activo)      │
│ <FrozenRuntimeRoot>/candidates/<id>/ │
│ identidad + verificación (SFR-06/07) │
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
                │ puntero: state/active.json  (única parte mutable)
                │ rollback = repuntar a <gen-anterior> (SFR-10)
```

Flujo de promoción (contrato de producto, no UI):

```text
Steam Source
    ↓   crear Candidate
    ↓   verificar (RV-1/RV-3)
  READY
    ↓   USER APPROVES   (SFR-08)
  activar Candidate = actualizar puntero + repuntar MO2/SKYRIM_PATH
```

## 8. Fronteras de confianza (Trust boundaries)

| Superficie | Confianza | Regla |
|---|---|---|
| **Steam Source** | MUTABLE / *untrusted as active runtime* | Fuente válida para Candidate. Nunca `reference_only`. Nunca Golden. Steam escribe libremente (SFR-01). |
| **Candidate** | Derivada, no confiable hasta verificar | Debe inventariarse/verificarse (RV-1) antes de promocionar. Estado `building → ready → invalid` (SFR-06/07). |
| **Frozen Runtime (Generation)** | Confiable tras verificación; **writable** | La versión está congelada; el árbol puede seguir siendo escribible por MO2/SKSE/runtime si hace falta. La identidad de versión se valida antes de activar. |
| **Active pointer (`state/active.json`)** | Autoridad de activación | Única pieza mutable del estado. Escritura atómica (temp + `os.replace`). |

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
- Evidencia **auxiliar, no identidad**: `buildid` del appmanifest de Steam, ruta
  de la library, estado de compatibilidad SKSE (§13). No se usa la ruta dentro de
  Steam ni la presencia del manifest como autoridad (coincide con ADR 0010 §11.4).
- `generation-id` propuesto: `"<display_version>__<tree_digest[:12]>"` (legible y
  libre de colisiones por contenido). Decisión final de esquema en P2.

## 10. Ciclo de vida del Candidate

Estados: `none → building → ready → promoted` o `building → invalid`.

1. **Detección**: se observa Steam Source (identidad fresca + estabilización, §13).
2. **Creación**: copia a `candidates/<candidate-id>/` (staging + publicación
   atómica no-clobber; ver reuso de RV-3 en §15).
3. **Verificación**: inventario completo + `tree_digest` == esperado + archivos
   críticos + independencia física. Cualquier discrepancia ⇒ `invalid` (F2/F3).
4. **READY**: el Candidate queda disponible para promoción; la activa no cambia.
5. **No promover automáticamente** (SFR-05/SFR-08).

Un Candidate incompleto o corrupto **nunca** puede convertirse en Active
Generation (F2/F3).

## 11. Ciclo de vida de Promotion

Secuencia exacta:

1. Operador solicita promover el Candidate `C` (aprobación explícita; SFR-08).
2. **Re-verificación fresca de C**: `observe_runtime_identity_from_root(C)` +
   `verify_tree(C, expected=C.tree_digest)` + archivos críticos. Confirmar que el
   árbol de `C` está estable y completo.
3. Publicar `C` como `versions/<gen>` si aún no está publicado (rename atómico
   dentro del mismo volumen; no hay copia sobre la activa).
4. **Actualizar el puntero** `state/active.json` (temp + `os.replace`), agregando
   la nueva Generation sin eliminar la anterior (SFR-09/SFR-10).
5. (P5) Repuntar MO2/SKYRIM_PATH a `versions/<gen>` (§13).
6. Post-check: la identidad observada de la Active Generation coincide con la
   esperada; si no, se revierte el puntero a la anterior (F5).

La activación **no copia encima** de la versión activa: se prefieren generaciones
versionadas + puntero chico.

## 12. Rollback

```text
active = previous_generation
```

Secuencia exacta:

1. Operador elige una Generation retenida `G_prev`.
2. Re-verificar `G_prev` (identidad + `tree_digest`).
3. Actualizar el puntero a `G_prev` (atómico).
4. (P5) Repuntar MO2/SKYRIM_PATH.
5. Post-check de identidad.

No se reconstruyen archivos destruidos. Si `G_prev` no está retenida, el rollback
falla cerrado (por eso MVP **no** borra generaciones).

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
  + el game path de MO2/SKYRIM_PATH, una sola vez por promoción. **P0 no modifica**
  nada de esto; sólo lo documenta (F6: si es incompatible, no hay migración
  forzada).

Gate futuro:

```text
RUNTIME_COMPATIBILITY: UNKNOWN | COMPATIBLE | INCOMPATIBLE
UNKNOWN != COMPATIBLE     # por defecto
```

## 14. Matriz de fallas (F1–F7)

| # | Escenario | Esperado | Mecanismo |
|---|---|---|---|
| **F1** | Steam actualiza mientras el usuario juega el Frozen Runtime | Frozen unaffected | Aislamiento físico: árboles disjuntos (SFR-02/04). |
| **F2** | La copia del Candidate falla a mitad | active unaffected; candidate INVALID/INCOMPLETE | Staging temporal + publicación atómica no-clobber; el Candidate no se publica hasta verificar. |
| **F3** | El Candidate valida mal | no promotion | Gate de verificación RV-1/RV-3 (identidad completa). |
| **F4** | El usuario no autoriza | active unchanged indefinitely | No hay auto-promoción (SFR-08). |
| **F5** | La promoción falla | previous active remains usable | Puntero atómico + reversión a la generación anterior; nunca se borra la activa. |
| **F6** | Nueva versión incompatible con SKSE/mods | no forced migration | Gate `RUNTIME_COMPATIBILITY`; `UNKNOWN != COMPATIBLE`. |
| **F7** | El usuario decide volver atrás | activate previous generation | Rollback = repuntar (SFR-10). |

## 15. Reuso de RV-1 / RV-2 / RV-3

Verificado leyendo el código de `sky_claw/local/runtime_vault/` sobre
`origin/main@0103ee4f`:

| Capa | Ubicación | Contrato real | Decisión |
|---|---|---|---|
| **RV-1** | `inventory.py`, `verification.py`, `models.py` | Inventario completo sellado (falla ante mutación concurrente y enlaces), `TreeDigest` independiente del root, `verify_tree`, `verify_runtime_identity`. Sin dependencias de GP2. | **REUSE tal cual.** |
| **RV-1b** | `runtime_observation.py` | Observación fresca de identidad de runtime desde un root (fail-closed, anti-ambigüedad). Sin dependencias de GP2. | **REUSE tal cual.** |
| **RV-2** | `golden.py` | `verify_golden_master` exige **evidencia independiente** (`expected_tree`, `expected_runtime`); `HASHING_A_FOLDER_DOES_NOT_MAKE_IT_A_GOLDEN_MASTER`. | **ADAPT/PARTIAL.** No usar `verify_golden_master` para admitir Steam Source (no hay expectativa independiente; sería auto-confianza). Reusar sus bloques: `verify_critical_files`, `verify_runtime_identity`, `inventory_tree`, `tree_digest_from_files`. |
| **RV-3** | `clone.py` | `create_runtime_clone(golden_source: GoldenMasterVerificationResult, ...)` exige fuente **VERIFIED** con `descriptor.role == "reference_only"`; `RuntimeCloneResult` exige `source_golden` no nulo y `descriptor.runtime_identity == source_golden.runtime_identity`. Copia real, staging hermano, publish atómico no-clobber, independencia física. Sin dependencias de GP2. | **ADAPT.** El *motor de copia+verificación+publicación* es reutilizable, pero el contrato de autoridad de fuente no acepta un Steam Source. Opciones a decidir en P3: **(a)** adaptador fino que produzca un descriptor equivalente para el snapshot de Steam; **(b)** rutina de copia mínima que reuse las primitivas RV-1 y las garantías de staging/publicación de RV-3. No se decide el mecanismo en P0. |

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
2. **Admisión de rutas.** El `FrozenRuntimeRoot` y las rutas de Generation se
   validan: no deben estar dentro de `steamapps/common/<juego>`; se resuelve la
   ruta física (ancestros incluidos) y se rechazan symlink/junction/reparse
   (reusar `sky_claw.app.security.links` y las guardas de RV-3: igualdad
   léxica/física, anidamiento, traversal).
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
| Copiar un Steam Source en plena escritura | Gate de estabilización (§18) + `inventory_tree` falla cerrado ante mutación concurrente. |
| Capturar una actualización parcial como Candidate válido | Estabilización + verificación de identidad completa (conteo de archivos + `tree_digest`). |
| MO2/SKSE apuntando a una Generation borrada | MVP no borra generaciones (SFR-10); el puntero siempre referencia una Generation retenida. |
| Destination dentro de Steam (romper SFR-02) | Admisión de rutas falla cerrada. |
| Espacio en disco durante la copia | Pendiente declarado (P2); la copia necesita ~espacio de un juego completo. |

## 18. Estabilización de Steam ("¿Steam terminó?")

**No se asume** `appmanifest cambió = actualización terminada`. Estado actual del
censo: no existe en el repo un parser del contenido de `appmanifest_489830.acf`
(sólo se chequea su **existencia** en `scanner._detect_store`), ni detección de
`steamapps/downloading`, ni de archivos `.part`.

Estrategia propuesta (gate explícito de **P1**, no demostrada en P0):

```text
STABLE(SteamSource) ⇔
  (a) appmanifest_489830.acf en estado idle (no "Update Queued/Required/App Running"),
      leído como evidencia advisory; Y
  (b) ausencia de artefactos en-progreso (p. ej. steamapps/downloading/<appid>,
      archivos .part) ; Y
  (c) dos inventories sellados completos de Steam Source, separados por una ventana
      silenciosa, producen el MISMO TreeDigest (y sin cambios de membresía).
```

`inventory_tree` es el sensor natural: su sello de estabilidad por archivo y de
árbol ya falla cerrado ante mutación concurrente, así que un árbol en escritura no
produce un digest válido. Si P1 no puede demostrar (a)–(c) en el rig real, la
creación de Candidate se **bloquea**, no se adivina.

## 19. Modelo de datos

Estado persistente mínimo (JSON, ~1 archivo):

```json
{
  "schema_version": 1,
  "active_generation": "1.6.1170__a1b2c3d4e5f6",
  "generations": [
    {
      "id": "1.6.1170__a1b2c3d4e5f6",
      "display_version": "1.6.1170",
      "runtime_identity": { "game_key": "skyrimse", "game_version": "1.6.1170.0" },
      "tree_digest": { "digest": "…", "files": 0, "bytes": 0 },
      "critical": [ { "rel_path": "SkyrimSE.exe", "expected_digest": "…", "expected_size": 0 } ],
      "source": { "kind": "steam", "appid": "489830", "buildid": "…", "path": "…" },
      "created_at": "2026-10-03T00:00:00Z"
    }
  ],
  "steam_source": {
    "path": "…",
    "appid": "489830",
    "observed_version": "1.7.xxxx",
    "observed_buildid": "…"
  },
  "candidate": {
    "id": null,
    "state": "none",
    "runtime_identity": null,
    "tree_digest": null
  },
  "update_available": false,
  "promotion_required": false
}
```

- Persistir con `temp + os.replace` (patrón del repo).
- No meter esto en `Config`/`config.toml`: el puntero vive junto al
  `FrozenRuntimeRoot` para que el `os.replace` sea atómico en el mismo volumen y
  para no mezclar estado de runtime grande con config de usuario.
- No crear base de datos. Un JSON canónico basta.

## 20. Layout de filesystem (propuesta)

```text
<FrozenRuntimeRoot>/                 # configurable; NUNCA dentro de steamapps/common
├── versions/
│   ├── 1.6.1170__<digest12>/        # Generation completa, versión congelada
│   └── 1.7.xxxx__<digest12>/
├── candidates/
│   └── <candidate-id>/              # copy-to-verify; publicado como versión al promover
└── state/
    └── active.json                  # puntero + registro
    └── active.json.<rand>.tmp       # temporal de escritura atómica
```

Ruta por defecto: **a decidir** (Open Question). Candidato: un root hermano en la
misma unidad que la library de Steam (para que la publicación sea barata), o
`Config.modding_root()/FrozenRuntime`. Restricción dura: fuera del árbol
administrado por Steam.

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
| **P1** | Steam Source discovery + Runtime Identity + **estabilización** | `STABLE(SteamSource)` demostrado o bloqueo fail-closed. |
| **P2** | Frozen Runtime storage + modelo de Generation + admisión de rutas | Crear/listar generations; regular atómico; rechazo de destino dentro de Steam. |
| **P3** | Candidate creation + verification | Candidate `ready`/`invalid`; F2/F3 cubiertos. |
| **P4** | Explicit Promotion + Rollback | Sin copia sobre activa; F4/F5/F7 cubiertos. |
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
```

No se ejecuta la suite completa porque no cambia código. Si por accidente apareciera
un cambio en `.py`, detenerse y justificarlo.

Tests planificados por slice (convención del repo: español, AAA, anclas
enumerativas):

| Slice | Tests |
|---|---|
| P1 | Estabilización: manifest idle vs update-in-progress; ausencia de `.part`; dos inventories idénticos ⇒ STABLE; mutación concurrente ⇒ fail-closed. |
| P2 | Admisión de rutas (destino dentro de Steam ⇒ rechazo; symlink/junction ⇒ rechazo); puntero atómico; registro enumerado (igualdad literal). |
| P3 | Candidate completo ⇒ ready; corrupción/truncamiento ⇒ invalid; fallo a mitad ⇒ activa intacta (F2/F3). |
| P4 | Promoción no borra previa; fallo de promoción revierte puntero (F5); sin aprobación no promueve (F4); rollback repunta (F7). |
| P5 | SKSE compatible/incompatible/unknown; game path repuntado; MO2 arranca desde Generation activa. |
| P7 | Rig: Steam actualiza (F1) sin tocar Frozen; rollback real (F7). |

## 25. Definition of Done (del proyecto, no de P0)

1. Steam puede actualizar su instalación sin tocar el Frozen Runtime (F1).
2. Toda versión nueva entra como Candidate y se verifica antes de promocionar
   (SFR-06/07).
3. Promoción sólo con aprobación explícita (SFR-08) y sin destruir la anterior
   (SFR-09).
4. Rollback = repuntar (SFR-10).
5. MO2/SKSE arrancan desde el Frozen Runtime (SFR-03).
6. Sin ACL mutation, sin helper privilegiado, sin GP2 (SFR-12/13).
7. `USES_ACL_MUTATION/FROZEN... = NO` (verificación de complejidad).
8. Documentación sincronizada y evidencia de rig registrada.

## 26. Preguntas abiertas (Open questions)

Sin resolver en P0; varias quedan como **gates explícitos** de P1/P2/P3/P5. No se
inventan soluciones.

1. **Estabilización de Steam**: ¿(a)–(c) de §18 alcanzan para demostrar
   `STABLE(SteamSource)` en el rig real? Gate P1.
2. **Binding de la Active Generation a MO2/SKSE**: ¿repuntar el `gamePath` de MO2
   (Qt `@ByteArray`) y `SKYRIM_PATH`, o un alias estable (`active` → generation) que
   MO2/SKSE/USVFS resuelvan? La auditoría probó un Stock externo, no el mecanismo de
   conmutación. Gate P5.
3. **Decodificación/edición del `gamePath` de MO2**: el repo hoy no decodifica
   `@ByteArray`; ¿lo hace Sky-Claw o es una acción manual documentada? Gate P5.
4. **Adaptación de RV-3**: ¿adaptador de autoridad de fuente, o rutina de copia
   mínima sobre primitivas RV-1? Gate P3.
5. **Ruta por defecto del `FrozenRuntimeRoot`**: ¿misma unidad que la library de
   Steam, `Config.modding_root()`, o elección del usuario? Gate P2.
6. **Esquema de `generation-id`**: ¿`<display_version>__<digest12>` u otro? Gate P2.
7. **Espacio en disco**: la copia requiere ~el tamaño completo del juego por
   generación; ¿se admite multivolumen? Gate P2.
8. **Completitud de la captura**: ¿el conjunto de archivos de Steam Source
   (incluyendo Creation Club/BSAs) alcanza para un runtime jugable sin Steam? La
   auditoría observó payload de Creation Club escrito por el juego; verificar en P7.
9. **Compatibilidad SKSE**: mapear `find_skse_installation` + `skse_dll_game_version`
   a `RUNTIME_COMPATIBILITY`; definir el alcance de "COMPATIBLE" (sólo SKSE core o
   también Address Library / DLL plugins). Gate P5.
10. **Per-user vs per-machine**: ¿el Frozen Runtime es por usuario o compartido?
    Gate P2.
11. **Downgrade/promoción hacia atrás**: ¿promover una versión menor que la activa
    es un caso soportado o se fuerza a usar rollback? Gate P4.

## 27. Revisión adversarial (auto-cuestionamiento)

| Pregunta | Respuesta |
|---|---|
| ¿Steam todavía puede tocar el Frozen Runtime? | No por diseño (árboles disjuntos). Verificación de admisión de rutas lo garantiza (P2). |
| ¿Alguna ruta está dentro de `steamapps/common`? | El layout lo prohíbe y la admisión lo rechaza fail-closed. |
| ¿Promotion puede destruir la activa antes de tener reemplazo válido? | No: re-verificación previa + publicación de nueva Generation + puntero atómico; nunca copia sobre la activa (SFR-09). |
| ¿Rollback depende de reconstrucción? | No: repunta a una Generation retenida (SFR-10). |
| ¿Candidate puede convertirse en activo sin aprobación? | No (SFR-08). |
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

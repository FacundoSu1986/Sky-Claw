# ADR 0010 — RV-GP2: Protect Golden / Golden Protection Apply

**Fecha:** 2026-08-27
**Estado:** Propuesta (design-only; prohibida la implementación de código de producción en este PR).
**Contexto de origen:** tarea RV-GP2 sobre `origin/main` `2ca4115b11740f0974c463a3d70db07aa068ca9c` (post-merge de PR #508, PR #509, PR #511 y PR #512).
**Alcance:** Diseño arquitectónico exclusivo de la capability mutadora transaccional de protección del Golden Master (`protect_golden_master`), su helper de elevación con privilegio mínimo, verificación estricta de quiescencia (probe handle exclusivo) y mutación atada a handle (`SetSecurityInfo`), autoridad transaccional privilegiada continua durante todo el ciclo de vida del FSM, simetría rigurosa entre las dos superficies del repositorio (GUI `tool_dispatcher` y agente LLM `AsyncToolRegistry`), lock de mutación cross-process basado en kernel (`GoldenMutationLock`), store de operaciones independiente del perfil de usuario (`%ProgramData%`) con separación estricta staging vs autorización privilegiada, su journal transaccional con Write-Ahead Logging (WAL y `FlushFileBuffers`), su modelo de recuperación ante caídas (crash recovery) y su compatibilidad forward con GP3.
**Reglas de exclusión:** Sin código de producción; sin ejecución de UAC; sin mutación de ACLs reales; sin acceso a Golden físico ni Runtime físico; sin interacción con Skyrim, MO2 ni Steam; aislamiento estricto respecto del issue #506.

---

## 1. Contexto

El Runtime Vault define hoy cuatro hitos normativos:

| Capa | Estado | Qué afirma | Dónde vive |
|---|---|---|---|
| RV-1 | Merged | Identidad de runtime y de árbol: `(relpath, size, sha256)` por archivo + `TreeDigest(digest, files, bytes)` agregado | `sky_claw/local/runtime_vault/{models,verification,inventory}.py` |
| RV-2 | Merged | Elevación de un candidato a Golden Master `VERIFIED` con evidencia independiente (`GoldenMasterDescriptor`, `reference_only`) | `sky_claw/local/runtime_vault/golden.py` |
| RV-3 | Merged | Creación y verificación de clon operativo independiente (`create_runtime_clone`, `PhysicalIndependenceResult`) | `sky_claw/local/runtime_vault/clone.py` |
| RV-GP1 | Merged (PR #508) | Inspección read-only del estado de protección filesystem del Golden Master (`inspect_golden_protection`) | `sky_claw/local/runtime_vault/protection.py` (ADR 0009) |

RV-GP1 permite observar y certificar si un Golden Master está en estado `HARDENED`, `WRITE_PROTECTED`, `UNPROTECTED`, `UNSUPPORTED` o `UNKNOWN` frente al token efectivo actual no elevado, mediante `AccessCheck` nativo sobre todo el subárbol y la superficie del parent inmediato.

Sin embargo, si un Golden Master recién verificado se encuentra en estado `UNPROTECTED`, el sistema carece hoy de un mecanismo seguro, determinista y transaccional para aplicar el blindaje ACL sin intervención manual propensa a errores.

La auditoría de lanzamiento (`docs/audits/2026-08-22_runtime_vault_mo2_stock_launch_audit.md`) demostró que procesos normales no elevados (Skyrim, MO2, plugins, herramientas del pipeline) realizan escrituras colaterales sobre carpetas de juego si el sistema de archivos lo permite.

**RV-GP2 ("Protect Golden")** es la capability mutadora encargada de transformar un Golden Master en estado `UNPROTECTED` (o `WRITE_PROTECTED` sub-óptimo) al estado objetivo verificado **`HARDENED`**, garantizando:
1. **Transaccionalidad y recuperabilidad:** captura previa exhaustiva de Security Descriptors (SD) por nodo antes de cualquier mutación, con binding criptográfico entre bytes y hash que el helper revalida antes de promover el plan;
2. **Autorización Privilegiada, Plan-Specific y Registro de Confianza:** validación de pertenencia al Registro de Golden Masters Confiables (`%ProgramData%\Sky-Claw\runtime_vault\trusted_goldens.json`, DACL protegida contra escritura no elevada) más confirmación privilegiada de intención ligada a la identidad del plan, separación estricta entre staging no confiable (`UNTRUSTED_STAGING`) y store autoritativo protegido (`AUTHORIZED_OPERATIONS` en `%ProgramData%`), soportando elevación Over-The-Shoulder (OTS); `UAC != autorización del plan`, `ARBITRARY_ROOT_FROM_STAGING_ACCEPTED = NO`;
3. **Garantía Rigurosa de Quiescencia y Mutación Atada a Handle (Handle-Bound Mutation) con doble apertura normativa:** un **probe handle transitorio** con `dwDesiredAccess` normativo en §4.1 (write-data mask) y `dwShareMode = 0` detecta writers preexistentes y mappings escribibles vía `ERROR_SHARING_VIOLATION`; tras la verificación positiva se abre un **handle de mutación** distinto con `dwDesiredAccess = READ_CONTROL | WRITE_DAC` y `dwShareMode = FILE_SHARE_READ` sobre el cual se ejecutan lectura PRE, `SetSecurityInfo` y lectura POST (binding por `(VolumeSerialNumber, FileId)` revalidado en cada paso);
4. **Autoridad Transaccional Privilegiada Continua (FSM Writer):** el helper elevado es el **único escritor del FSM autoritativo y del journal de operaciones**, reteniendo el `GoldenMutationLock` durante las fases de mutación, post-verificación (orquestando la ejecución de GP1/RV2/NodeSet bajo token no elevado vía IPC autenticado, **read-only**), archivado de respaldo y commit final; el verificador no elevado nunca escribe `FSM`, `COMMITTED` ni `ARCHIVING_BACKUP`;
5. **Simetría Rigurosa de Superficies Hermanas (AGENTS.md):** exposición uniforme y cableado simétrico en la GUI (`SupervisorAgent` -> `tool_dispatcher`) y en el agente LLM (`AsyncToolRegistry` -> `LLMRouter`), con ancla de test que enumera por introspección/igualdad literal ambos cableados y previene asimetrías de elevación;
6. **Serialización Cross-Process por Kernel Handle (`GoldenMutationLock`):** exclusión mutua respaldada por el kernel de Windows mediante `CreateFileW` con `dwDesiredAccess = GENERIC_READ | GENERIC_WRITE` (o equivalente explícito) y `dwShareMode = 0` sobre un archivo en `locks/` (DACL protegida), con defensa contra reuso de PID mediante `GetProcessTimes`;
7. **Cero mutación no planificada:** mitigación de propagación silenciosa de herencia (`SetSecurityInfo` con `PROTECTED_DACL_SECURITY_INFORMATION` en apply, `UNPROTECTED_DACL_SECURITY_INFORMATION` condicional en rollback cuando el SD PRE no tenía `SE_DACL_PROTECTED`);
8. **Verificación post-mutación de triple eje:** post-verificación de acceso GP1 (`state == HARDENED`), post-verificación de integridad de contenido RV-2 (`TreeDigest` intacto y `VERIFIED`), y verificación de igualdad literal exhaustiva del conjunto de nodos (`MANIFEST_NODE_SET == FRESH_POST_CANONICAL_NODE_SET`); el PR body declara este **triple gate**;
9. **Rollback transaccional fail-closed con WAL durable:** restauración compensatoria ante cualquier fallo o inconsistencia intermedia con protocolo Write-Ahead Logging (WAL) y persistencia física obligatoria mediante `FlushFileBuffers(journal_handle)` antes de `SetSecurityInfo`.

---

## 2. Decisión

1. **Target de Estado:** El único estado final de éxito admisible para GP2 es **`HARDENED`** con `success == True` y `assurance_scope == "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"`. Un resultado final que termine en `WRITE_PROTECTED`, `UNPROTECTED`, `UNKNOWN` o `UNSUPPORTED` constituye un fallo de la operación y dispara rollback obligatorio.
2. **Mutación por Nodo Bottom-Up (Deepest Descendants First):** GP2 muta individualmente cada archivo y directorio del subárbol comenzando por las hojas más profundas y finalizando en la raíz del Golden (`root`). Cada nodo se configura explícitamente con `PROTECTED_DACL_SECURITY_INFORMATION`, desvinculando la herencia antes de que sus ancestros sean modificados, eliminando cualquier riesgo de propagación no planificada hacia descendientes abiertos.
3. **Garantía de Quiescencia y Mutación Estrictamente Atada a Handle (Handle-Bound Mutation) — DOS handles normativos por nodo:**
   - **Probe handle transitorio (detección de writers/mappings):** antes de abrir el handle de mutación, el helper abre cada nodo con
     `dwDesiredAccess = FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | DELETE` para archivos, o `FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | FILE_DELETE_CHILD | FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | DELETE` para directorios; `dwShareMode = 0`; `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`; `OPEN_EXISTING`. El probe se cierra inmediatamente tras la verificación.
   - **Handle de mutación (binding por handle):** tras la verificación positiva, el helper abre un segundo handle con `dwDesiredAccess = READ_CONTROL | WRITE_DAC`; `dwShareMode = FILE_SHARE_READ` (sin `FILE_SHARE_WRITE`, sin `FILE_SHARE_DELETE`); mismas flags. Sobre este handle se ejecutan: validación de `(VolumeSerialNumber, FileId)` y `reparse_tag`, lectura de SD PRE, WAL durable con `FlushFileBuffers(journal_handle)`, `SetSecurityInfo` y re-lectura de SD POST.
   - **Razón del doble handle:** la compartición de Win32 se evalúa contra el `dwShareMode` de los handles ya abiertos; un writer que abrió con `FILE_SHARE_READ | FILE_SHARE_WRITE` (patrón habitual de MO2, engines, antivirus, indexadores) no produce `ERROR_SHARING_VIOLATION` si el nuevo handle sólo pide `READ_CONTROL | WRITE_DAC`. Sólo un probe que solicite acceso de datos de escritura con `dwShareMode = 0` colisiona de forma determinista con cualquier writer o `CreateFileMapping` escribible preexistente.
   - **Limitación declarada:** NTFS no revoca los derechos concedidos en handles ya abiertos cuando se cambia la DACL; un writer cooperativo que abrió con `FILE_SHARE_READ | FILE_SHARE_WRITE` antes de la mutación conservará su handle y podrá seguir escribiendo. La garantía v1 es: (a) **detección** de que un writer existe al momento del probe; (b) **negación fail-closed** de la transición a `COMMITTED` mientras exista un writer o mapping escribible, vía rerun del probe inmediatamente antes de `ARCHIVING_BACKUP`; (c) post-detección, el rollback no restaura contenido, sólo ACLs, por lo que cualquier drift de contenido es responsabilidad del RV-2 (`TreeDigest`) que aborta la transición. **No se afirma exclusividad absoluta de writers preexistentes que no comparten delete y comparten write**; la matriz exacta figura en §4.1.
   - **Detección de TOCTOU/rename/delete:** el binding por `(VolumeSerialNumber, FileId)` revalidado en cada paso del handle de mutación, más `FILE_FLAG_OPEN_REPARSE_POINT` y verificación de `reparse_tag == 0`, neutraliza ventanas de renombre o reemplazo de archivo entre probe y `SetSecurityInfo`.
4. **Parent Policy Normativa — Refuse to Apply:** GP2 v1 **NO modifica el directorio parent** contenedor del Golden. Si la inspección previa detecta que el parent inmediato presenta vectores de borrado/renombrado del root (`FILE_DELETE_CHILD`, cadena delete+create, `WRITE_DAC` o `WRITE_OWNER` escalable), GP2 se rehúsa terminantemente a aplicar (`PARENT_UNSAFE -> REFUSE_TO_APPLY`). Prohibido warning + continue; prohibido override HITL en v1.
5. **Política de Hardlinks Internos — Refuse to Apply:** Si el inventario detecta dos o más rutas relativas dentro del subárbol que comparten la misma identidad física `(VolumeSerialNumber, FileId)`, GP2 v1 se rehúsa a aplicar (`REFUSE_TO_APPLY` con `DuplicateFileIdError`) antes de la primera mutación física, evitando generar registros de respaldo PRE conflictivos para un único objeto de seguridad mutable.
6. **Respaldo Binario Exacto (Raw Self-Relative SD):** Antes de la primera mutación física, se genera un manifiesto sellado que almacena para cada nodo los bytes binarios exactos del `SECURITY_DESCRIPTOR` autoritativo en formato self-relative (`OWNER | GROUP | DACL` y bits de control `SE_DACL_PROTECTED`), con longitud validada mediante `GetSecurityDescriptorLength`. La cadena SDDL se preserva únicamente para diagnóstico/auditoría.
7. **Exclusión de SACL en v1:** GP2 v1 no lee, no muta y no promete restauración de SACLs (`SACL_CAPTURED=NO`, `SACL_MUTATED=NO`, `SACL_RESTORE_GUARANTEE=NO`). La garantía de restauración v1 cubre de forma exacta `OWNER`, `GROUP`, `DACL` y flags de control de herencia de DACL. No se solicita ni habilita `SeSecurityPrivilege`.
8. **Política de Owner — Preserve Owner by Default con Owner Rights ACE:** GP2 v1 **preserva el propietario original del objeto** (`PRESERVE_OWNER_BY_DEFAULT`). Para neutralizar el `WRITE_DAC` y `READ_CONTROL` implícito que Windows otorga por defecto al dueño del objeto, la Target DACL inyecta una ACE explícita del Well-Known SID **Owner Rights (`S-1-3-4`)** con máscara restrictiva de solo lectura.
9. **Target DACL Estricta y Explícita:** Se define una DACL allowlist universal sin ACEs deny innecesarias: Owner Rights (`S-1-3-4`) con `FILE_GENERIC_READ` (archivos) / `FILE_GENERIC_READ | FILE_TRAVERSE` (directorios); Principales estándar (`S-1-5-11` Authenticated Users / usuario actual) con lectura/ejecución; `S-1-5-18` LocalSystem y `S-1-5-32-544` Administrators con `FILE_ALL_ACCESS`.
10. **Store de Operaciones Profile-Independent con Autorización Privilegiada:** El almacén de operaciones y manifiestos se divide en dos capas bajo el directorio del sistema `%ProgramData%\Sky-Claw\runtime_vault`:
    - `staging\<op_id>\candidate_manifest.json`: espacio de staging donde el coordinador no elevado escribe el plan candidato.
    - `operations\<op_id>\authorized_plan.json`: store protegido donde **únicamente el helper elevado** puede escribir tras validar físicamente el plan contra el registro de Golden Masters confiables y obtener la confirmación privilegiada de intención del operador.
11. **Helper Privilegiado Confinado (Anti-Confused Deputy):** El helper elevado recibe exclusivamente `--operation-id <UUID>` y `--staging-digest <SHA-256>`. El helper no acepta rutas libres ni scripts arbitrarios. El helper revalida de manera autónoma el lock de mutación activo, la pertenencia de la ruta al registro de Golden Masters confiables, el confinamiento estricto bajo el root, ausencia total de reparse points, `FileId` y digest del SD PRE antes de mutar cada nodo.
12. **Autoridad Transaccional Privilegiada Continua (FSM Writer):** El helper elevado permanece activo como la **única autoridad que escribe el FSM autoritativo**, manteniendo el `GoldenMutationLock` durante las fases de mutación, post-verificación, archivado de respaldo y commit:
    - Tras finalizar la mutación, el helper orquesta la post-verificación (GP1, RV2 y NodeSet) ejecutando un proceso verificador no elevado bajo el token del usuario y recibiendo el resultado mediante IPC autenticado.
    - Si la verificación es exitosa, el helper archiva el respaldo en `golden_backups` y realiza la transición a `COMMITTED`.
    - Si falla, el helper ejecuta el rollback Top-Down compensatorio y transiciona a `ROLLED_BACK`.
13. **Simetría en las Dos Superficies del Repositorio (AGENTS.md):** La capability `protect_golden_master` se registra de forma estrictamente simétrica tanto en `SupervisorAgent` (`tool_dispatcher` en GUI) como en `AsyncToolRegistry` (`LLMRouter` para Telegram y chat), garantizando que ambas superficies compartan el mismo lock cross-process, el mismo preflight y el mismo requerimiento de elevación UAC.
14. **Lock Cross-Process OS-Enforced por Golden (`GoldenMutationLock`):** Toda mutación (APPLY, ROLLBACK, RECOVERY, futuro GP3) adquiere un lock transaccional exclusivo respaldado por un handle Win32 con sharing exclusivo en `%ProgramData%\Sky-Claw\runtime_vault\locks\<lock_key>.lock`. La carpeta `locks` tiene permisos de solo lectura para usuarios no elevados. Si el proceso muere, el kernel libera el lock automáticamente. Se valida PID y `ProcessCreationTime` para neutralizar el reuso de PIDs.
15. **Journal Transaccional Propio con WAL y FlushFileBuffers:** La máquina de estados persiste sus transiciones y el estado de cada nodo en `sky_claw/local/runtime_vault/protection_journal.py` (en `AUTHORIZED_OPERATIONS`), completamente desacoplado de `sky_claw/app/db/journal.py`. Antes de mutar cualquier nodo $K$, se registra durablemente la intención `MUTATING(K)` y se invoca `FlushFileBuffers` para cerrar la ventana de pérdida por caída de energía.
16. **Triple Post-Verificación Independiente:**
    - GP1 `inspect_golden_protection(golden_path)` exigiendo `state == HARDENED` y `success == True`.
    - RV-2 `verify_golden_master(...)` exigiendo que el `TreeDigest` y los archivos críticos permanezcan `VERIFIED` e idénticos al baseline.
    - Verificación de Exhaustividad de Nodos: `MANIFEST_NODE_SET == FRESH_POST_CANONICAL_NODE_SET` (igualdad literal de rutas, tipo y `FileId`).
17. **Archivado de Respaldo por Identidad Estable en Store Protegido:** En la fase `ARCHIVING_BACKUP` (antes de `COMMITTED`), el helper archiva el manifiesto en el almacén protegido del sistema: `%ProgramData%\Sky-Claw\runtime_vault\golden_backups\<vol_serial>_<root_file_id>\<policy_version>\<op_id>_manifest.json`, con DACL de solo lectura para usuarios, impidiendo que un Actor D modifique el baseline de restauración.
18. **Rollback Top-Down vs GP3:** Si ocurre un fallo en cualquier etapa, el helper ejecuta un rollback transaccional restaurando los SDs originales en orden inverso (**Top-Down: Root primero, descendientes después**). Este rollback es un mecanismo de recuperación interna ante fallos y es conceptual y contractualmente distinto de **GP3** (capability futura de desprotección a demanda del operador).

---

## 3. Non-goals

- **No implementa GP3 (Unprotect / Restore a demanda):** GP3 es una operación deliberada solicitada por el usuario para revertir un Golden protegido a su estado editable; GP2 solo implementa el rollback automático ante fallos de la propia protección.
- **No modifica el parent contenedor del Golden:** La creación de contenedores dedicados de Runtime Vault con ACLs heredables pre-configuradas queda diferida para versiones futuras.
- **No implementa Execution Guard:** El bloqueo de ejecución de ejecutables marcados `reference_only` corresponde a la capa de lanzamiento/MO2 (RV-5), no al filesystem ACL.
- **No altera los contratos de RV-1, RV-2 ni RV-3:** `GoldenMasterDescriptor`, `GoldenMasterVerificationResult` y `RuntimeCloneResult` permanecen intactos y desacoplados del estado de protección.
- **No promete protección contra Administradores o SYSTEM comprometidos:** Conforme al threat model de §5, un proceso con privilegios administrativos plenos puede saltarse las ACLs de NTFS; la garantía es estricta frente al token no elevado (`assurance_scope == CURRENT_EFFECTIVE_UNELEVATED_TOKEN`).
- **No implementa soporte para plataformas no-Windows ni filesystems no-NTFS:** Ambientes `UNSUPPORTED` en GP1 son rechazados inmediatamente por GP2 sin intentar mutaciones.
- **No reutiliza ni modifica `sky_claw/app/db/journal.py`:** Aislamiento total de las estructuras de persistencia de DynDOLOD / #506.

---

## 4. Invariantes Heredados de GP1 (ADR 0009)

GP2 se construye sobre las garantías formales establecidas en ADR 0009 y verificadas en `protection.py`:

1. **Criterio de Medición sobre Token Real:** La efectividad de una ACL solo se valida mediante `AccessCheck` nativo sobre el token efectivo no elevado, nunca mediante inferencias de texto SDDL ni utilitarios CLI (`icacls`).
2. **Definición Rigurosa de HARDENED:** `HARDENED` exige conjunción estricta de:
   - `WRITE_PROTECTED` sobre TODO el subárbol y la superficie del parent;
   - Token evaluado no elevado (`TokenElevation == False`);
   - Propietario no atribuible al token o neutralizado mediante ACE Owner Rights (`S-1-3-4`) sin `WRITE_DAC` concedido;
   - Ausencia de privilegios mutation-enabling (`SeRestorePrivilege`, `SeTakeOwnershipPrivilege`).
3. **Fail-Closed Ante Reparse Points:** Cualquier enlace simbólico, junction o reparse point detectado mediante la primitiva canónica `sky_claw.app.security.links` invalida la operación inmediatamente (`UNKNOWN` en GP1, `REFUSE_TO_APPLY` en GP2).
4. **Ortogonalidad entre Integridad y Protección:** `TreeDigest` identifica contenido (`(digest, files, bytes)`); la protección describe accesibilidad (`GoldenProtectionResult`). Ninguno reemplaza al otro.
5. **Contrato de Resultados del Repo:** `success: bool` es `True` únicamente cuando la operación concluye en el estado objetivo deseado; `message: str` permanece estrictamente vacío `""` en caso de éxito.

---

## 4.1 Primitiva Única de Quiescencia y Matriz de Share-Semantics

La detección de quiescencia se materializa mediante **una sola primitiva normativa** (probe handle transitorio), usada idénticamente en §2.3, §5.2, §12.2, §25 (GP2-T23/T24/T27) y §26 (M-Q1/M-Q2). Su contrato es:

```text
QUIESCENCE_PROBE = CreateFileW(
    node_path,
    PROBE_DESIRED_ACCESS,        // ver bloque normativo abajo
    /* dwShareMode */ 0,         // CERO: ninguna clase de acceso se ofrece
    NULL,
    OPEN_EXISTING,
    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
    NULL
)
```

`PROBE_DESIRED_ACCESS` (igual para todos los nodos del subárbol en v1, parametrizable por `node_kind`):

```text
// Archivos regulares (node_kind == "file"):
PROBE_DESIRED_ACCESS =
      FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_ATTRIBUTES
    | FILE_WRITE_EA
    | DELETE

// Directorios (node_kind == "dir"):
PROBE_DESIRED_ACCESS =
      FILE_ADD_FILE
    | FILE_ADD_SUBDIRECTORY
    | FILE_DELETE_CHILD
    | FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_ATTRIBUTES
    | FILE_WRITE_EA
    | DELETE
```

Tras la verificación, **el probe se cierra** y se abre un **handle de mutación** independiente con `READ_CONTROL | WRITE_DAC` y `dwShareMode = FILE_SHARE_READ` (sin `FILE_SHARE_WRITE`, sin `FILE_SHARE_DELETE`). El binding por `(VolumeSerialNumber, FileId)` revalidado en cada paso del handle de mutación neutraliza la ventana TOCTOU entre probe y `SetSecurityInfo`.

### 4.1.1 Matriz de Colisión de Share-Modes (probe contra writers preexistentes)

El resultado del probe se rige por la intersección entre los `dwDesiredAccess` solicitados por el writer y los que el probe solicita, cruzada con el `dwShareMode` que el writer ofreció al abrir. La tabla siguiente es **normativa**: el oráculo de `GP2-T23` enumera sus cuatro filas de share-mode del writer, fija el `dwDesiredAccess` exacto del writer (mínimo `GENERIC_WRITE`) y exige el desenlace contractual por fila; no se admite parametrización parcial.

| Writer `dwDesiredAccess` | Writer `dwShareMode` | Probe `dwShareMode = 0` solicita acceso de datos write | Desenlace del probe | Estado GP2 |
|---|---|---|---|---|
| `GENERIC_WRITE` (incluye `FILE_WRITE_DATA`) | `0` (exclusivo) | Sí | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| `GENERIC_WRITE` | `FILE_SHARE_READ` | Sí | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| `GENERIC_WRITE` | `FILE_SHARE_READ \| FILE_SHARE_WRITE` | Sí | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| `GENERIC_WRITE` | `FILE_SHARE_READ \| FILE_SHARE_WRITE \| FILE_SHARE_DELETE` | Sí | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| `GENERIC_READ` (sólo lectura, sin `FILE_WRITE_DATA`) | cualquiera | El probe sí solicita write; depende del share del writer | Si el writer sólo abrió read y no concedió write, el probe tiene éxito y sigue a `SetSecurityInfo` | depende de la verificación posterior |
| Mapping `PAGE_READWRITE` abierto vía `CreateFileMapping` con `FILE_SHARE_READ \| FILE_SHARE_WRITE` | (de la mapping) | Sí | `ERROR_SHARING_VIOLATION` (mapping escribible colisiona con `FILE_WRITE_DATA`) | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |

**Limitación declarada (no inventada):** NTFS no revoca los derechos concedidos a un handle ya abierto cuando se cambia la DACL; un writer que abrió con `FILE_SHARE_READ | FILE_SHARE_WRITE` antes de la mutación conservará su handle. El probe sólo **detecta** la presencia del writer; el rerun del probe inmediatamente antes de `ARCHIVING_BACKUP` (ver §2.16) impide la transición a `COMMITTED` mientras el writer siga vivo. El RV-2 post-verificación detecta drift de contenido. El ADR no afirma exclusividad absoluta posterior a `COMMITTED`; esa garantía corresponde a un módulo de enforcement distinto (no GP2 v1).

### 4.1.2 Mapping escribible (`CreateFileMapping` con `PAGE_READWRITE`)

La creación de un file mapping con `PAGE_READWRITE` mantiene una referencia de datos compartidos en el filesystem que cuenta como writer desde la perspectiva del sharing. El probe, al solicitar `FILE_WRITE_DATA` con `dwShareMode = 0`, colisiona con el mapping si su `dwShareMode` no concedió `FILE_SHARE_WRITE`. **Caso de fallo conocido:** un mapping abierto con `FILE_SHARE_READ | FILE_SHARE_WRITE` deja al writer acceso a la página compartida aunque el probe del helper retenga `ERROR_SHARING_VIOLATION` por la *referencia mapping* misma; el rerun del probe antes de `ARCHIVING_BACKUP` detecta la persistencia. `GP2-T24` cubre este caso con un fixture que abre `CreateFileMapping(..., PAGE_READWRITE)` y verifica que el probe falla y que la verificación posterior aborta antes de `COMMITTED`.

---

## 5. Threat Model

### 5.1 Actores y Alcance

| Actor | Contexto / Token | Cobertura GP2 |
|---|---|---|
| **Actor A** | Proceso normal de usuario (Skyrim, MO2, plugins DLL, scripts de modding, Sky-Claw sin elevación). Token interactivo estándar. | **Objetivo Primario:** Bloqueo total de escritura, append, delete, rename, cambio de atributos, cambio de DACL y cambio de owner. |
| **Actor B** | Usuario miembro de Administrators ejecutando con **token filtrado** (UAC activo). SIDs admin marcados deny-only, privilegios elevados deshabilitados. | **Objetivo Primario:** Idem Actor A. Los SIDs deny-only no conceden permisos de mutación. |
| **Actor C** | Proceso elevado tras consentimiento UAC legítimo. | **Vector Autorizado:** Utilizado exclusivamente por el helper GP2 para aplicar la configuración. Fuera de la garantía de bloqueo post-aplicación. |
| **Actor D** | Malware no privilegiado o proceso hostil en espacio de usuario. | **Objetivo de Seguridad:** Intento de invocar el helper GP2 como *Confused Deputy* para alterar archivos ajenos o corromper el Golden. Mitigado por Registro de Golden Masters Confiables, Confirmación Privilegiada de Intención, store privilegiado de autorización (`AUTHORIZED_OPERATIONS`), garantía de quiescencia y mutación atada a handle (§11, §12). |
| **Actor E** | SYSTEM / Administrador malicioso persistente. | **Fuera de Alcance:** Todo usuario con privilegios de kernel o token SYSTEM puede saltarse ACLs NTFS. Se declara explícitamente. |

### 5.2 Vectores Específicos de Ataque y Mitigaciones en GP2

```text
Amenaza: Confused Deputy en Helper UAC (Actor D forja plan en staging para directorio arbitrario)
Mitigación: Trusted Golden Registry (ARBITRARY_ROOT_FROM_STAGING_ACCEPTED = NO) + Confirmación Privilegiada de Intención Plan-Specific en el Helper + Store autoritativo protegido (%ProgramData%\...\operations) donde solo el Helper elevado puede crear el binding autorizado.

Amenaza: Escrituras concurrentes de writers preexistentes tras cambiar la DACL
Mitigación: Doble apertura normativa (ver §2.3 y §12.2). Probe handle transitorio con `dwDesiredAccess = FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | DELETE` (archivos) o equivalente para directorios, `dwShareMode = 0`. Si existe un writer abierto, un `CreateFileMapping` con `PAGE_READWRITE`, o cualquier handle con derecho de write/delete que no haya concedido ese share -> `CreateFileW` falla con `ERROR_SHARING_VIOLATION` -> `REFUSE_TO_APPLY` (0 nodos mutados) o `ROLLBACK_REQUIRED` (>0 nodos mutados). Handle de mutación con `READ_CONTROL | WRITE_DAC` y `dwShareMode = FILE_SHARE_READ`, sin `FILE_SHARE_WRITE`, sin `FILE_SHARE_DELETE`. **No se omite `FILE_SHARE_WRITE` del handle de mutación** porque la detección ocurre en el probe. La matriz de share-semantics figura en §4.1.

Amenaza: Sustitución de archivo / TOCTOU entre validación y mutación
Mitigación: Handle-Bound Mutation (CreateFileW -> GetSecurityInfo(handle) -> SetSecurityInfo(mismo handle) -> GetSecurityInfo(mismo handle)), impidiendo desvíos por rename/delete durante la operación.

Amenaza: Asimetría entre Superficies (GUI vs Agente LLM)
Mitigación: Exposición simétrica de protect_golden_master en tool_dispatcher (GUI) y AsyncToolRegistry (LLM Router) con ancla de test AST GP2-T27.

Amenaza: Mutación no planificada por herencia
Mitigación: Bottom-Up Apply + PROTECTED_DACL_SECURITY_INFORMATION por nodo.

Amenaza: Borrado/Renombrado vía Parent Unsafe
Mitigación: Refuse to Apply si parent permite DELETE_CHILD o WRITE_DAC.

Amenaza: Dueño reescribe DACL implícitamente
Mitigación: Inyección explícita de Owner Rights S-1-3-4 con solo lectura.

Amenaza: Intercalación concurrente de transacciones sobre el mismo Golden
Mitigación: GoldenMutationLock respaldado por handle exclusivo del kernel en directorio protegido serializa todo apply, rollback, recovery y GP3 por (VolumeSerialNumber, root_file_id).

Amenaza: Ventana de Crash entre Mutación y Registro en Journal
Mitigación: Write-Ahead Logging con FlushFileBuffers obligatorio (MUTATING persistido en disco antes de SetSecurityInfo) + Re-escaneo de diferencias en recuperación.

Amenaza: Omisión de descendiente pre-endurecido en el plan
Mitigación: Verificación obligatoria de igualdad literal entre MANIFEST_NODE_SET y FRESH_POST_CANONICAL_NODE_SET antes de COMMITTED.
```

---

## 6. Protection Target

El estado objetivo que GP2 debe producir de forma determinista y verificable es:

```text
TargetState = HARDENED
AssuranceScope = CURRENT_EFFECTIVE_UNELEVATED_TOKEN
ScanScope = FULL_SUBTREE_AND_PARENT
```

Para alcanzar este estado, GP2 debe garantizar que:
1. **Todo archivo regular** dentro del Golden tenga denegados de forma efectiva: `WRITE_DATA`, `APPEND_DATA`, `DELETE`, `WRITE_ATTRIBUTES`, `WRITE_EA`, `WRITE_DAC` y `WRITE_OWNER`.
2. **Todo directorio** dentro del Golden tenga denegados de forma efectiva: `ADD_FILE`, `ADD_SUBDIRECTORY`, `DELETE`, `DELETE_CHILD`, `WRITE_ATTRIBUTES`, `WRITE_EA`, `WRITE_DAC` y `WRITE_OWNER`.
3. **El root del Golden** no sea susceptible de borrado o renombrado a través de su parent contenedor.
4. **Todo nodo** conserve lectura efectiva (`READ_DATA` / `LIST_DIRECTORY`, `TRAVERSE`, `READ_ATTRIBUTES`, `READ_EA`, `READ_CONTROL`, `SYNCHRONIZE`) para el usuario actual y los grupos estándar del sistema.
5. **Ningún nodo** dependa de herencia no controlada del parent (`SE_DACL_PROTECTED` activo en todos los nodos).

---

## 7. Exact Target DACL (Definición Normativa)

Para evitar ambigüedades en la implementación, se fija la estructura y contenido exacto de la DACL destino v1:

### 7.1 Atributos de la DACL
- **Control Bits:** `SE_DACL_PROTECTED` (`0x1000`) activado en cada nodo (vía flag `PROTECTED_DACL_SECURITY_INFORMATION` en `SetSecurityInfo`).
- **Herencia en ACEs:** Sin flags de herencia (`0x00`, sin `OBJECT_INHERIT_ACE` ni `CONTAINER_INHERIT_ACE`), ya que GP2 calcula y aplica la DACL de forma explícita a cada nodo del subárbol.
- **Tipo de ACE:** `ACCESS_ALLOWED_ACE_TYPE` (`0x00`). No se utilizan ACEs de denegación (`ACCESS_DENIED_ACE_TYPE`), basando la seguridad en el principio de allowlist canónica de NTFS (todo derecho no concedido explícitamente queda denegado).

### 7.2 Entradas de Control de Acceso (ACEs) por Nodo

#### Para Archivos Regulares (`node_kind == "file"`):

| Orden | Principal (SID) | Tipo | Flags | Máscara de Acceso (Bits) | Derechos Nominales |
|---|---|---|---|---|---|
| 1 | **Owner Rights** (`S-1-3-4`) | Allow | `0x00` | `0x00120089` | `FILE_GENERIC_READ` (`FILE_READ_DATA`, `FILE_READ_ATTRIBUTES`, `FILE_READ_EA`, `READ_CONTROL`, `SYNCHRONIZE`) |
| 2 | **Authenticated Users** (`S-1-5-11`) | Allow | `0x00` | `0x001200A9` | `FILE_GENERIC_READ | FILE_GENERIC_EXECUTE` (agrega `FILE_EXECUTE` para binarios/DLLs del juego) |
| 3 | **LocalSystem** (`S-1-5-18`) | Allow | `0x00` | `0x001F01FF` | `FILE_ALL_ACCESS` |
| 4 | **Builtin Administrators** (`S-1-5-32-544`) | Allow | `0x00` | `0x001F01FF` | `FILE_ALL_ACCESS` |

#### Para Directorios (`node_kind == "dir"`):

| Orden | Principal (SID) | Tipo | Flags | Máscara de Acceso (Bits) | Derechos Nominales |
|---|---|---|---|---|---|
| 1 | **Owner Rights** (`S-1-3-4`) | Allow | `0x00` | `0x001200A9` | `FILE_GENERIC_READ | FILE_TRAVERSE` (`FILE_LIST_DIRECTORY`, `FILE_READ_ATTRIBUTES`, `FILE_READ_EA`, `FILE_TRAVERSE`, `READ_CONTROL`, `SYNCHRONIZE`) |
| 2 | **Authenticated Users** (`S-1-5-11`) | Allow | `0x00` | `0x001200A9` | `FILE_GENERIC_READ | FILE_TRAVERSE` |
| 3 | **LocalSystem** (`S-1-5-18`) | Allow | `0x00` | `0x001F01FF` | `FILE_ALL_ACCESS` |
| 4 | **Builtin Administrators** (`S-1-5-32-544`) | Allow | `0x00` | `0x001F01FF` | `FILE_ALL_ACCESS` |

Nota sobre Token Filtrado: La inclusión de `S-1-5-32-544` (Administrators) con `FILE_ALL_ACCESS` no debilita la protección frente al Actor B (Admin con token filtrado), porque durante la ejecución normal el SID `S-1-5-32-544` posee el atributo `SE_GROUP_USE_FOR_DENY_ONLY` en el token filtrado, lo que impide que el kernel utilice esta ACE para conceder acceso.

---

## 8. Owner Policy (Normativa)

### 8.1 Decisión: `PRESERVE_OWNER_BY_DEFAULT`
GP2 v1 **conserva el propietario (`owner_sid`) preexistente** de cada archivo y directorio.

### 8.2 Justificación Técnica y Demostración
En el modelo de seguridad de Windows ([MS-ADTS] §6.1.3.4, [MS-DTYP] §2.4.6):
1. Por defecto, el propietario de un objeto recibe implícitamente los derechos `READ_CONTROL` y `WRITE_DAC`, incluso si la DACL no contiene ninguna ACE para su SID.
2. Sin embargo, cuando una ACE para el SID especial **Owner Rights (`S-1-3-4`)** está presente en la DACL, el gestor de seguridad de Windows **suprime la concesión implícita de `WRITE_DAC` y `READ_CONTROL`** y evalúa exclusivamente los derechos estipulados en dicha ACE.
3. Dado que la Target DACL definida en §7 asigna a `S-1-3-4` únicamente `FILE_GENERIC_READ` (sin `WRITE_DAC` ni `WRITE_OWNER`), el propietario pierde la capacidad de reescribir la DACL desde su token no elevado.
4. El clasificador GP1 (`protection.py`, líneas 275-280 y 350-357) valida expresamente que si `owner_rights_ace_present == True` y `WRITE_DAC` no es concedido por `AccessCheck`, la relación de ownership no degrada el estado a `UNPROTECTED` y permite la promoción a `HARDENED`.

### 8.3 Excepciones y Restricciones
- Si durante la preparación del plan se detecta un nodo cuyo `owner_sid` es corrupto, no resoluble a SID string o no presente en el SD, la fase de planificación falla con `PlanCreationError` (desenlace `REFUSE_TO_APPLY`).
- GP2 v1 no intenta transferir la propiedad a `TrustedInstaller` ni a `SYSTEM`, eliminando la necesidad de adquirir o retener `SeTakeOwnershipPrivilege` o `SeRestorePrivilege` durante la operación normal de protección.

---

## 9. SD Backup / Restore ABI

Para garantizar una restauración fiel ante fallos (rollback) o futura desprotección (GP3), se define el ABI de respaldo binario por nodo:

### 9.1 Estructura del Registro de Respaldo (`NodeSecurityBackup`)

```python
@dataclass(frozen=True, slots=True)
class NodeSecurityBackup:
    relative_path: str                  # Path relativo canónico posix ("." para el root)
    node_kind: str                      # "dir" | "file"
    volume_serial_number: int           # Número de serie del volumen NTFS (estabilidad de anclaje)
    file_id: int                        # 64-bit o 128-bit File ID único en el volumen
    pre_sd_bytes_b64: str               # Raw self-relative SECURITY_DESCRIPTOR serializado en Base64
    pre_sd_length: int                  # Longitud en bytes según GetSecurityDescriptorLength
    pre_sd_sha256: str                  # SHA-256 hexadecimal del buffer pre_sd_bytes
    owner_sid: str                      # String SID del propietario (S-1-...)
    group_sid: str                      # String SID del grupo primario (S-1-...)
    dacl_control_flags: int             # Control word (SE_DACL_PROTECTED, SE_DACL_PRESENT, etc.)
    sddl_diagnostic: str                # SDDL string representativo (SOLO DIAGNÓSTICO)
```

### 9.2 Reglas Normativas del Respaldo
1. **Captura Autoritativa:** La fuente primaria y autoritativa para el restore son **exclusivamente los bytes binarios `pre_sd_bytes_b64`** decodificados y validados contra `pre_sd_sha256`. `sddl_diagnostic` es solo para logs de auditoría y no se utiliza como oráculo de restauración.
2. **Binding Criptográfico pre_sd_bytes_b64 ↔ pre_sd_sha256 (normativo, sin excepción):** El helper, antes de promover el plan, ejecuta y exige éxito de las **dos** igualdades:
   ```text
   sha256(base64_decode(pre_sd_bytes_b64)) == pre_sd_sha256
   sha256(sd_leido_del_handle_via_GetSecurityInfo) == pre_sd_sha256
   ```
   La primera igualdad rechaza planes adversarios en los que `pre_sd_sha256` coincide con el SD real pero los `pre_sd_bytes_b64` son maliciosos (un Actor D podría entonces forzar al rollback a aplicar SD adversario). La segunda rechaza drift de identidad entre el momento de captura y la ejecución. Si cualquiera falla -> `REFUSE_TO_PLAN` antes de mutar nada.
3. **Uso de los bytes en rollback:** el helper, durante el rollback, **decodifica `pre_sd_bytes_b64` y aplica `SetSecurityInfo` sobre esos bytes exactamente**; nunca vuelve a leer el descriptor desde el filesystem para "verificar" contra el backup, porque esa verificación cruzada es la que habilita el ataque de sustitución.
4. **Obtención Segura de Longitud:** La longitud del buffer se valida mediante la API Win32 `GetSecurityDescriptorLength(sd_ptr)`.
5. **Formato Self-Relative:** Todo descriptor capturado se normaliza a self-relative (`MakeSelfRelativeSD` si fuera necesario o `GetSecurityInfo` que devuelve buffers contiguos).
6. **SACL Excluida y Alcance Exacto de Restauración:**
   - `SACL_CAPTURED = NO`
   - `SACL_MUTATED = NO`
   - `SACL_RESTORE_GUARANTEE = NO`
   - La garantía v1 se enuncia estrictamente como: *Restauración exacta del estado PRE de OWNER, GROUP, DACL y flags de control de herencia de DACL para cada nodo intervenido*.
   - Si un nodo posee SACL preexistente en el filesystem, GP2 no la modifica (`SACL_SECURITY_INFORMATION` nunca se pasa en los flags de `SetSecurityInfo`), por lo que la SACL permanece inalterada en el objeto durante la mutación y durante el eventual rollback.
7. **Ataque mitigado por este binding:** un Actor D no puede presentar un plan con `pre_sd_sha256` legítimo y `pre_sd_bytes_b64` adversario: la primera igualdad falla y el helper aborta antes de la primera mutación. El oráculo `GP2-T26` (test paramétrico) y el mutante `M-B1` (ver §26) anclan este contrato.

---

## 10. Inventory, Sealing & Hardlink Policy (Pre-Mutation Manifest)

Antes de cualquier mutación en el filesystem, GP2 construye y sella el **Plan de Protección Candidato** (`GoldenProtectionPlan`):

### 10.1 Detección y Política de Hardlinks Internos
En NTFS, un mismo archivo físico (identificado unívocamente por su tupla `(VolumeSerialNumber, FileId)`) puede poseer múltiples nombres de enlace (hardlinks). Si el inventario incluye dos rutas que apuntan al mismo `FileId`:
- Mutar la primera ruta modificaría inmediatamente el SD de la segunda, haciendo que su comparación PRE falle y corrompa la evidencia transaccional.
- **Política GP2 v1:** Si se detecta cualquier `(VolumeSerialNumber, FileId)` duplicado dentro del subárbol del Golden, la fase de planificación aborta de inmediato con excepción `DuplicateFileIdError` y estado **`REFUSE_TO_APPLY`** antes de realizar ninguna mutación física.

### 10.2 Fases del Sellado
1. **PRE-INVENTORY:** Recorrido exhaustivo del árbol con detección estricta de reparse points (`link_kind_and_identity_or_raise`).
2. **SECURITY CAPTURE & HARDLINK CHECK:** Lectura de `NodeSecurityBackup` para cada nodo; verificación de unicidad de `(VolumeSerialNumber, FileId)`.
3. **IDENTITY ANCHOR:** Vinculación de cada nodo con su `(VolumeSerialNumber, FileId)` obtenido mediante `GetFileInformationByHandleEx`.
4. **TARGET DACL SYNTHESIS:** Construcción de la Target DACL canónica y de la estructura semántica esperada (`expected_post_owner`, `expected_post_group`, `expected_post_dacl_protected`, `expected_post_dacl_aces`).
5. **POST-INVENTORY STRUCTURAL PASS:** Re-verificación estructural completa (sin drift de archivos, mtime ni identidad). Si el árbol muta durante la captura -> `InventoryError` -> aborto inmediato con estado `REFUSE_TO_APPLY` sin mutar nada.
6. **CANONICAL MANIFEST WRITING:** Serialización determinista del manifiesto en JSON canónico (claves ordenadas, sin espacios redundantes, UTF-8) en `UNTRUSTED_STAGING`.
7. **MANIFEST DIGEST COMPUTATION:** Cálculo del `staging_digest = sha256(candidate_manifest_bytes)`.

---

## 11. Trust, Authentication Model & ProgramData ACL Contract

### 11.0 Axioma de Autoridad Única

```text
PLAN_AUTHORIZATION_AUTHORITY = Elevated Helper Process ONLY
UAC_IS_PLAN_AUTHORIZATION     = NO
ARBITRARY_ROOT_FROM_STAGING_ACCEPTED = NO
```

El consentimiento UAC (`runas`) **únicamente eleva el helper**; **no autoriza el plan concreto**. La autorización de un plan GP2 v1 exige, sin excepción, los tres componentes normativos siguientes, todos verificados por el helper elevado antes de crear `authorized_plan.json`:

1. **Trusted Golden Registry (TGR):** `%ProgramData%\Sky-Claw\runtime_vault\trusted_goldens.json` (ruta normativa, hardcodeada en el helper). El helper compara `canonical_root`, `VolumeSerialNumber` y `root_file_id` del plan contra una entrada registrada.
2. **Privileged Plan-Specific Confirmation (PPSC):** diálogo privilegiado en el helper que muestra al operador —bajo UAC, con la sesión elevada— los valores exactos: `operation_id`, `canonical_root`, `VolumeSerialNumber`, `root_file_id`, `TreeDigest`, `node_count`, `policy_version`. La confirmación queda registrada en el journal de `AUTHORIZED_OPERATIONS`. No se introduce un segundo canal de intención (token, OTS handshake, etc.) en v1.
3. **Protected Authorized Binding (PAB):** `authorized_plan.json` se crea **únicamente** en `%ProgramData%\Sky-Claw\runtime_vault\operations\<op_id>\authorized_plan.json`, directorio del que un proceso no elevado carece de `WRITE_DATA`/`WRITE_DAC`/`DELETE`. El binding es inmodificable para el Actor D.

El helper debe **rechazar** la promoción del plan si cualquiera de los tres componentes falta o falla. **Queda prohibida** la frase o variante "diálogo privilegiado o token de intención ligado a la operación" u otras alternativas ambiguas: existe una sola autoridad normativa.

### 11.1 El Problema de la Autorización y Defensa contra Confused Deputy
Un hash SHA-256 garantiza **integridad de corrupción**, pero no **autorización**. Para impedir que un proceso no privilegiado (Actor D) manipule al helper para proteger un directorio arbitrario o un plan forjado:
1. **Registro de Golden Masters Confiables (`Trusted Golden Registry`):** El helper verifica que el `canonical_root`, `VolumeSerialNumber` y `root_file_id` coincidan con una entrada registrada en `%ProgramData%\Sky-Claw\runtime_vault\trusted_goldens.json`. `ARBITRARY_ROOT_FROM_STAGING_ACCEPTED = NO`. El registro reside en `%ProgramData%` con DACL de escritura exclusiva para `Administrators`/`SYSTEM`; un proceso no elevado no puede añadir, reemplazar, borrar ni modificar entradas.
2. **Confirmación Privilegiada de Intención (Plan-Specific Intent):** El helper muestra al operador una confirmación privilegiada que incluye `operation_id`, `canonical_root`, `VolumeSerialNumber`, `root_file_id`, `TreeDigest`, `node_count` y `policy_version`. Solo tras la confirmación explícita se genera el binding autorizado.

### 11.2 Definición Formal del Modelo de Autorización
```text
AUTHORIZATION_AUTHORITY = Elevated Helper Process (ejecutándose bajo token elevado SYSTEM o Administrators)
WHO_CAN_CREATE_BINDING = Elevated Helper ÚNICAMENTE (tras validar TGR + PPSC contra el plan)
WHO_CAN_MODIFY_BINDING = NADIE (DACL NTFS deniega WRITE_DATA, WRITE_DAC y DELETE a Authenticated Users)
WHO_CAN_WRITE_TGR      = Administrators / SYSTEM ONLY (DACL protegida)
WHO_CAN_READ_TGR       = Authenticated Users (READ ONLY; sin WRITE, sin DELETE, sin WRITE_DAC, sin WRITE_OWNER, sin REPARSE)
HOW_HELPER_PROVES_AUTHORIZATION = El Helper escribe y lee el authoritative binding en %ProgramData%\Sky-Claw\runtime_vault\operations\<op_id>\authorized_plan.json, en un directorio donde el usuario no elevado carece de permisos de escritura.
```

### 11.3 Contrato de DACLs en el Namespace `%ProgramData%`

**Bootstrap del namespace:** el subárbol `%ProgramData%\Sky-Claw\runtime_vault\` (incluidos `staging\`, `operations\`, `locks\`, `golden_backups\` y el archivo `trusted_goldens.json`) es **provisionado por un componente privilegiado definido por el diseño** (instalador ejecutado por `Administrators`/`SYSTEM`, o un servicio de Sky-Claw que arranca con `SeImpersonatePrivilege`/`SeAssignPrimaryTokenPrivilege` y se auto-eleva al primer uso). El ADR no asume que el namespace ya existe. La rama `staging\<op_id>\` se crea al vuelo por el coordinador no elevado con permisos de escritura limitados al propio usuario; las demás ramas las crea exclusivamente el componente privilegiado.

Se definen las zonas bajo `%ProgramData%\Sky-Claw\runtime_vault`:

| Namespace / Directorio | Propietario / Creador | DACL para `Administrators` y `SYSTEM` | DACL para `Authenticated Users` (`S-1-5-11`) | Propósito |
|---|---|---|---|---|
| `staging/<op_id>/` (`UNTRUSTED_STAGING`) | Coordinador (usuario interactivo) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_EXECUTE` (con `CREATOR OWNER` pleno sobre sus carpetas) | Almacenamiento temporal del candidate manifest generado por el proceso no elevado. |
| `operations/<op_id>/` (`AUTHORIZED_OPERATIONS`) | Helper Elevado | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ` (`0x001200A9`) — **SIN DERECHO DE ESCRITURA NI BORRADO** | Almacén autoritativo del plan sellado `authorized_plan.json` y el journal de ejecución FSM. |
| `golden_backups/<vol_root>/` (`AUTHORIZED_BACKUPS`) | Helper Elevado | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ` (`0x001200A9`) — **SIN DERECHO DE ESCRITURA NI BORRADO** | Respaldo durable de seguridad PRE para rollback forense y futura capability GP3. |
| `locks/` (`LOCKS_STORE`) | Helper Elevado | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ` (`0x001200A9`) — **SIN DERECHO DE ESCRITURA, CREACIÓN, BORRADO, WRITE_DAC NI WRITE_OWNER** | Contenedor protegido para archivos de lock del kernel (GoldenMutationLock). |
| `trusted_goldens.json` (`TRUSTED_GOLDEN_REGISTRY`) | Componente privilegiado (instalador) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ` (`0x00120089`) — **READ ONLY: SIN CREATE, SIN WRITE, SIN DELETE, SIN WRITE_DAC, SIN WRITE_OWNER** | Trust root del helper elevado. Contiene entradas `{canonical_root, VolumeSerialNumber, root_file_id, policy_version, registered_by, registered_at}`. Un proceso no elevado no puede añadir, reemplazar, borrar, ni siquiera reparse-sustituir el archivo. |

---

## 12. Privileged Helper Boundary, Quiescence & Handle-Bound Mutation

El helper elevado (`sky-claw-vault-helper.exe` o worker elevado) ejecuta la mutación física de seguridad sobre el subárbol.

### 12.1 Confinamiento Estricto del Helper
- **Sin argumentos arbitrarios:** Solo recibe `--operation-id <UUID>` y `--staging-digest <SHA256>`.
- **Sin scripts dinámicos:** La lógica de mutación está rígidamente compilada/empaquetada.
- **Sin seguimiento de reparse points:** Si cualquier nodo presenta reparse tag != 0 -> aborto inmediato.
- **Sin cruce de límites de volumen:** Todas las operaciones pertenecen al mismo `VolumeSerialNumber`.

### 12.2 Algoritmo de Ejecución con Quiescencia y Handle-Bound Mutation (dos handles normativos por nodo)

La primitiva única de quiescencia y la matriz de share-semantics se definen en §4.1. El algoritmo siguiente las aplica de forma estricta; los `dwDesiredAccess` y `dwShareMode` exactos son **normativos** y no pueden variarse por nodo ni por tipo de excepción.

```text
Fase de Autorización en el Helper (Elevado):
    1. Leer candidate_manifest.json desde UNTRUSTED_STAGING.
    2. Verificar sha256(candidate_manifest_bytes) == staging_digest.
    3. Validar pertenencia de la ruta contra TGR (canonical_root + VolumeSerialNumber + root_file_id) en %ProgramData%\Sky-Claw\runtime_vault\trusted_goldens.json. Falla -> REFUSE_TO_PLAN (no se crea authorized_plan.json).
    4. Confirmación Privilegiada de Intención (PPSC) en la sesión UAC: el diálogo elevado debe exhibir exactamente operation_id, canonical_root, VolumeSerialNumber, root_file_id, TreeDigest, node_count, policy_version, y obtener consentimiento explícito del operador. Sin confirmación -> ELEVATION_REJECTED / REFUSE_TO_PLAN. Se registra durablemente en el journal que PPSC pasó.
    5. Adquirir GoldenMutationLock exclusivo (CreateFileW sobre %ProgramData%\Sky-Claw\runtime_vault\locks\skyclaw_golden_lock_<vol_serial>_<root_file_id>.lock con dwDesiredAccess = GENERIC_READ | GENERIC_WRITE, dwShareMode = 0; ver §19.1).
    6. Crear authorized_plan.json en AUTHORIZED_OPERATIONS (único productor). El binding incluye operation_id, canonical_root, VolumeSerialNumber, root_file_id, TreeDigest, node_count, policy_version, staging_digest y el timestamp PPSC.

Fase de Mutación por Nodo K en el Plan (en orden Bottom-Up):
    1. Probe Handle de Quiescencia (transitorio):
       h_probe = CreateFileW(
           node_path,
           PROBE_DESIRED_ACCESS,           // normativo en §4.1
           /* dwShareMode */ 0,            // CERO
           NULL,
           OPEN_EXISTING,
           FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
           NULL
       )
       Si h_probe == INVALID_HANDLE_VALUE y GetLastError() == ERROR_SHARING_VIOLATION:
           Si 0 nodos mutados -> FAIL_CLOSED -> REFUSE_TO_APPLY (QuiescenceViolationError)
           Si >0 nodos mutados -> FAIL_CLOSED -> Iniciar secuencia de ROLLBACK (ROLLBACK_REQUIRED).
       Si h_probe es inválido por otro motivo -> FAIL_CLOSED -> REFUSE_TO_APPLY / ROLLBACK_REQUIRED según corresponda.
       CloseHandle(h_probe).

    2. Apertura del Handle de Mutación (binding por handle) y Validación de Identidad:
       h = CreateFileW(
           node_path,
           READ_CONTROL | WRITE_DAC,
           FILE_SHARE_READ,                // sin FILE_SHARE_WRITE, sin FILE_SHARE_DELETE
           NULL,
           OPEN_EXISTING,
           FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
           NULL
       )
       Si h == INVALID_HANDLE_VALUE y GetLastError() == ERROR_ACCESS_DENIED:
           Habilitar SeRestorePrivilege en el scope del bloque (justo antes de esta apertura, no después), reintentar una vez; si persiste ACCESS_DENIED -> ROLLBACK_REQUIRED (no se asume que WRITE_DAC está concedido por la DACL existente).
       info = GetFileInformationByHandleEx(h, FileIdInfo)
       tag  = GetFileInformationByHandleEx(h, FileAttributeTagInfo).ReparseTag
       Si info.VolumeSerialNumber != expected.vol_serial o info.FileId != expected.file_id o tag != 0:
           CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK.

    3. Leer SD PRE sobre el Handle y Validar Binding pre_sd_bytes_b64 == pre_sd_sha256:
       sd_pre = GetSecurityInfo(h, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, ...)
       Si sha256(sd_pre) != expected.pre_sd_sha256:
           CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK.
       Si sha256(base64_decode(expected.pre_sd_bytes_b64)) != expected.pre_sd_sha256:
           CloseHandle(h) -> FAIL_CLOSED -> REFUSE_TO_APLAN (manifest adversario detectado ANTES de mutar nada).
       Capture pre_sd_protected_flag = GetSecurityDescriptorControl(sd_pre) & SE_DACL_PROTECTED para usarlo en rollback (ver §13).

    4. Write-Ahead Log (WAL) con FlushFileBuffers Obligatorio:
       Registrar durablemente en el journal de AUTHORIZED_OPERATIONS: MUTATING(K, pre_sd_sha256, pre_sd_protected_flag).
       Invocar FlushFileBuffers(journal_handle) para garantizar persistencia física previa a la mutación.

    5. Aplicar Target DACL sobre el MISMO Handle:
       err = SetSecurityInfo(
           h,
           SE_FILE_OBJECT,
           DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
           NULL,
           NULL,
           target_dacl_ptr,
           NULL
       )
       Si err != 0 -> CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK.

    6. Re-leer y Validar Semántica POST sobre el MISMO Handle:
       sd_post = GetSecurityInfo(h, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, ...)
       Verificación Semántica Post-Mutación:
         - Control flags contiene SE_DACL_PROTECTED (0x1000);
         - Owner SID y Group SID permanecen invariantes;
         - DACL contiene exactamente las 4 ACEs de allowlist requeridas (§7) en orden canónico.
       Si no cumple -> CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK.

    7. Confirmación en Journal y Cierre de Handle:
       Registrar durablemente en journal: MUTATED(K).
       Invocar FlushFileBuffers(journal_handle).
       CloseHandle(h).

Rerun del probe antes de ARCHIVING_BACKUP: una vez aplicadas todas las mutaciones y antes de transicionar a ARCHIVING_BACKUP, el helper reejecuta el Paso 1 sobre cada nodo. Si el probe falla para cualquier nodo -> transición a ROLLBACK_REQUIRED (no se archiva ni se promueve a COMMITTED). Esto cubre el caso de un writer que abrió durante la fase de mutación.

Rollback por nodo (Top-Down, ver §17): el helper usa el `pre_sd_bytes_b64` validado en el paso 3 (no vuelve a leer del filesystem) y aplica `SetSecurityInfo` con el flag:
   - Si pre_sd_protected_flag == SE_DACL_PROTECTED: DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
   - Si no: DACL_SECURITY_INFORMATION | UNPROTECTED_DACL_SECURITY_INFORMATION
   restaurando exactamente OWNER, GROUP, DACL y flag de herencia PRE. El binding por (VolumeSerialNumber, FileId) se revalida en cada nodo antes de la restauración; si difiere -> INDETERMINATE.
```

---

## 13. UAC, Human-in-the-Loop (HITL) & Superficies Hermanas

1. **Consentimiento del SO Exclusivo:** La elevación del helper se solicita **únicamente** a través del mecanismo nativo de UAC de Windows (`ShellExecuteExW` con verbo `"runas"`). **UAC no es autorización del plan:** el plan se autoriza por TGR + PPSC + PAB (ver §11.0). Si el usuario cancela el UAC, el helper no se eleva y `authorized_plan.json` no se crea.
2. **Alineación con el Ciclo de Vida HITL:** En concordancia con el guardrail fail-closed del repositorio (`sky_claw/app/security/hitl.py`), toda solicitud HITL previa a la elevación se valida de forma no reentrable y con resolución terminal única. La PPSC del helper elevado es un diálogo adicional y distinto: HITL pre-elevación sobre la *operación*; PPSC post-elevación sobre el *plan concreto* (valores exactos del §11.0).
3. **Simetría en las Dos Superficies (AGENTS.md):**
   - **Superficie GUI:** `SupervisorAgent` -> `tool_dispatcher["protect_golden_master"]` (con UAC / HITL bridge integrado).
   - **Superficie Agente LLM:** `AsyncToolRegistry` -> `LLMRouter["protect_golden_master"]` (con UAC / HITL bridge integrado).
   - Ambas superficies consumen el mismo servicio de backend (`sky_claw/local/runtime_vault/protection_service.py`), garantizando que ninguna superficie pueda ejecutar la mutación eludiendo el preflight, el lock cross-process o la elevación. El ancla AST `GP2-T27` enumera por introspección/igualdad literal ambas superficies y el set de herramientas mutadoras con su requerimiento de elevación.
4. **Soporte Pleno de Elevación Over-The-Shoulder (OTS):** Al residir el staging, el store autorizado y el TGR en `%ProgramData%`, el helper elevado puede leer el plan candidate, el TGR y escribir el plan autoritativo independientemente de si la cuenta administrativa ingresada en el diálogo UAC coincide con el usuario interactivo o es un administrador secundario de la máquina.
5. **Prohibición Absoluta de Credenciales Propias:** Sky-Claw **nunca** solicitará, almacenará, parseará ni enviará contraseñas ni credenciales de usuario.

---

## 14. Parent Strategy (Normativa)

### 14.1 Política v1: `PARENT_UNSAFE -> REFUSE_TO_APPLY`
GP2 v1 **NO modifica bajo ninguna circunstancia el directorio parent** contenedor del Golden Master.

### 14.2 Chequeo Previo Obligatorio
Antes de generar el plan de mutación, se ejecuta `inspect_golden_protection(golden_path)`. Si el parent inmediato presenta cualquiera de los siguientes vectores:
- `parent_observation.granted_rights` contiene `DELETE_CHILD`;
- Cadena de renombrado viable sobre el root (`DELETE_CHILD` o `DELETE` en root ∧ `ADD_FILE`/`ADD_SUBDIRECTORY` en parent);
- `parent_observation.granted_rights` contiene `CHANGE_PERMISSIONS` (`WRITE_DAC`);
- `parent_observation.granted_rights` contiene `CHANGE_OWNER` (`WRITE_OWNER`) sin Owner Rights ACE restrictiva;

**Desenlace Obligatorio:**
`GP2Result(success=False, state=REFUSE_TO_APPLY, message="El directorio contenedor (parent) no es seguro...")`

### 14.3 Prohibición de Warning + Continue y Override
Queda terminantemente prohibido degradar esta condición a una advertencia permisiva o permitir un flag `--force` / checkbox "Continuar de todos modos". Si el parent es inseguro, GP1 post-verificación jamás podría emitir `HARDENED`, por lo que la operación fallaría inevitablemente.

---

## 15. Reparse & Hardlink Policy (Normativa Unificada)

- **Tolerancia Cero a Reparse Points:** La presencia de cualquier reparse tag (`st_reparse_tag != 0`), symlink, junction, volume mount point o placeholder en el root, en cualquier nodo descendiente o en el parent inmediato produce de forma canónica y exclusiva el estado **`REFUSE_TO_APPLY`** con `success=False` y excepción `InventoryLinkError`.
- **Tolerancia Cero a Hardlinks Internos:** La presencia de dos rutas que compartan el mismo `(VolumeSerialNumber, FileId)` produce el estado **`REFUSE_TO_APPLY`** con `success=False` y excepción `DuplicateFileIdError`.
- **Primitiva Única:** La detección utiliza exclusivamente `sky_claw.app.security.links.link_kind_and_identity_or_raise`. Prohibido reimplementar inspecciones ad-hoc.
- **Race Condition Gate:** Si un archivo o directorio es reemplazado por un enlace entre la fase de planificación y la ejecución del helper, el chequeo de `FileId` y `reparse_tag` sobre el handle abierto del helper detecta la colisión antes de mutar y aborta la transacción con `REFUSE_TO_APPLY` / rollback.

---

## 16. Apply Order (Normativa Decidida)

### 16.1 Decisión: Deepest Descendants First (Bottom-Up / Post-Order), Root Last
```text
APPLY_ORDER = BOTTOM_UP_POST_ORDER
```

### 16.2 Justificación Técnica
1. **Contención de Herencia:** Al aplicar `PROTECTED_DACL_SECURITY_INFORMATION` primero sobre los archivos y subdirectorios de mayor profundidad, cada nodo queda explícitamente blindado y protegido contra herencia antes de que sus ancestros directos sean modificados.
2. **Prevención de Propagación Accidental:** Si se modificara el root en primer lugar con flags heredables o si el sistema de archivos intentara propagar cambios a los hijos, los descendientes aún no procesados podrían recibir estados de seguridad no planificados. El orden bottom-up garantiza que cuando se muta el root, el 100% de los descendientes ya son nodos protegidos autónomos.
3. **Determinismo:** El ordenamiento se calcula por profundidad decreciente de path (`len(parts)` desc) y secundariamente por orden lexicográfico inverso del relpath posix.

---

## 17. Rollback Order (Normativa Decidida)

### 17.1 Decisión: Top-Down / Pre-Order (Root First, Descendants Afterwards)
```text
ROLLBACK_ORDER = TOP_DOWN_PRE_ORDER
```

### 17.2 Justificación Técnica
1. **Restauración de Contexto de Ancestros:** Si un nodo hijo tenía originalmente herencia habilitada (`dacl_inheritance_protected == False`), su DACL dependía de la DACL de su directorio contenedor.
2. **Secuencia Correcta de Re-Herencia:** Al restaurar primero el root y los directorios superiores a su estado PRE exacto, cuando un nodo hijo vuelve a recibir su descriptor original (desactivando `SE_DACL_PROTECTED` si correspondía), el parent ya posee la configuración legítima sobre la cual propagar o resolver permisos.
3. **Determinismo y Conjunto Afectado:** El rollback procesa todo nodo registrado como `MUTATING` o `MUTATED` en el journal (y cualquier nodo cuyo SD actual difiera del PRE tras re-escaneo de recuperación), ordenados por profundidad creciente (`len(parts)` asc) y secundariamente por orden lexicográfico posix.

### 17.3 Restauración Exacta del Flag de Herencia PRE (normativa)

El flag `SE_DACL_PROTECTED` del descriptor PRE de cada nodo se captura durante la fase de mutación y se persiste en el journal asociado a `MUTATING(K)` (ver §12.2 paso 4). El rollback selecciona el flag de `SetSecurityInfo` que se pasa a Windows según ese valor capturado:

```text
if captured_pre_dacl_protected_flag & SE_DACL_PROTECTED:
    SetSecurityInfo flags include PROTECTED_DACL_SECURITY_INFORMATION
else:
    SetSecurityInfo flags include UNPROTECTED_DACL_SECURITY_INFORMATION
```

Pasar siempre `PROTECTED_DACL_SECURITY_INFORMATION` en el rollback dejaría `SE_DACL_PROTECTED` activado en nodos que originalmente heredaban de su parent, alterando la semántica de propagación DACL posterior y violando la promesa de "restauración exacta" del §2.6. Pasar siempre `UNPROTECTED_DACL_SECURITY_INFORMATION` produciría el mismo defecto en sentido inverso. La decisión es por nodo, leída del journal, y es la única vía válida en v1.

`GP2-T12` extiende su oráculo para incluir un caso explícito: nodo con DACL PRE heredada (no protegida) -> APPLY -> fallo simulado -> rollback -> `SE_DACL_PROTECTED` del nodo es exactamente el del PRE (False). El mutante `M-R1` cubre la omisión de `UNPROTECTED_DACL_SECURITY_INFORMATION`.

---

## 18. Privilege Strategy

El helper privilegiado gestiona sus privilegios de token con máxima restricción temporal:

```text
TAKE_OWNERSHIP_REQUIRED_WHEN = NEVER_IN_V1 (Preserve Owner by default)
SE_RESTORE_REQUIRED_WHEN = ONLY_IF_WRITE_DAC_DENIED_BY_EXISTING_RESTRICTIVE_DACL
SE_SECURITY_REQUIRED = NO (SACL no modificada)
PRIVILEGE_ENABLE_SCOPE = EXACT_WIN32_CALL_BOUNDARY
PRIVILEGE_RESTORE_POLICY = ALWAYS_RESTORE_PREVIOUS_STATE_IN_FINALLY
```

### 18.1 Manejo de `AdjustTokenPrivileges`
- Toda activación de privilegios (ej. `SeRestorePrivilege`) se habilita **antes de la apertura del handle de mutación** del nodo y se deshabilita inmediatamente después en un bloque de guarda `finally`. El motivo es normativo: si la DACL preexistente deniega `WRITE_DAC` a `Administrators` y el helper corre bajo una cuenta OTS secundaria que no es owner del objeto, la apertura con `READ_CONTROL | WRITE_DAC` falla con `ACCESS_DENIED` antes de llegar a `SetSecurityInfo`; habilitar `SeRestorePrivilege` después de esa apertura es tarde. El scope del privilegio cubre, por tanto, **toda la secuencia por nodo**: apertura del handle, lectura de SD PRE, `SetSecurityInfo` y re-lectura de SD POST.
- La función de habilitación valida explícitamente `GetLastError() != ERROR_NOT_ALL_ASSIGNED`. Si el privilegio no fue asignado, la operación falla cerrado.
- Se preserva y restaura la estructura `TOKEN_PRIVILEGES.PreviousState`.
- Si tras habilitar `SeRestorePrivilege` la apertura sigue devolviendo `ACCESS_DENIED`, se transiciona a `ROLLBACK_REQUIRED` sin intentar `SeTakeOwnershipPrivilege` (no se transfiere ownership en v1; el contrato es fail-closed).

---

## 19. Transaction FSM, Journal Ownership & GoldenMutationLock

Para evitar cualquier interferencia con el trabajo paralelo del Issue #506 en `sky_claw/app/db/journal.py`, GP2 implementa un almacén transaccional y lock exclusivo en `sky_claw/local/runtime_vault/protection_journal.py` (en `AUTHORIZED_OPERATIONS`).

### 19.1 Lock Cross-Process por Golden (`GoldenMutationLock`)
1. **Primitiva OS-Enforced (contrato Win32 inequívoco):** El lock se adquiere mediante `CreateFileW` abriendo el archivo `%ProgramData%\Sky-Claw\runtime_vault\locks\skyclaw_golden_lock_<vol_serial>_<root_file_id>.lock` con:
   ```text
   dwDesiredAccess = GENERIC_READ | GENERIC_WRITE
   dwShareMode     = 0    // EXCLUSIVO: sin FILE_SHARE_READ, sin FILE_SHARE_WRITE, sin FILE_SHARE_DELETE
   dwCreationDisposition = OPEN_ALWAYS (crear si no existe, abrir si existe)
   dwFlagsAndAttributes  = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_DELETE_PROTECT (rechaza reparse/replacement)
   ```
   El handle se mantiene abierto durante **toda** la autoridad transaccional. `dwShareMode = 0` es contractual y no se omite: la consecuencia es que el archivo de lock sólo puede ser reabierto por otro mutador después de que el kernel cierre el handle (muerte de proceso, kill, `CloseHandle`). No se admite `dwDesiredAccess` implícito.
2. **Clave de Identidad:**
   `lock_key = f"skyclaw_golden_lock_{volume_serial}_{root_file_id}"`
3. **Participantes Obligatorios:** GP2 APPLY, GP2 ROLLBACK, GP2 RECOVERY y futuro GP3 UNPROTECT. Todo mutador que abre un archivo bajo el Golden o escribe en `AUTHORIZED_OPERATIONS` debe **adquirir el lock con el contrato exacto del punto 1** antes del primer byte mutado. Queda prohibido un segundo FSM writer.
4. **Comportamiento ante Muerte de Proceso:** El kernel de Windows cierra automáticamente todos los handles abiertos cuando un proceso finaliza (sea por crash, kill o salida normal), liberando la exclusión mutua de forma síncrona.
5. **Defensa contra Reuso de PID y Recuperación de Lock Huérfano:**
   - Dentro del archivo de lock se escribe la metadata del dueño: `{"lock_key": "...", "owner_pid": 1234, "owner_process_creation_time": 133456789012345678, "session_id": 1, "operation_id": "...", "phase": "APPLYING", "created_at": 1756245000}`.
   - Si un proceso intenta adquirir el lock y `CreateFileW` tiene éxito (el handle fue liberado por el SO), pero existe metadata de una operación previa no concluida:
     - El recuperador consulta `GetProcessTimes` sobre `owner_pid`. Si el proceso existe y su `CreationTime` coincide con `owner_process_creation_time`, el proceso sigue vivo y NO se puede robar el lock (`GoldenLockError`).
     - Si el proceso murió o el `CreationTime` difiere (PID reuse detectado), se confirma que el lock quedó huérfano por crash y se transiciona de forma segura al procedimiento de Crash Recovery (§20).
     - Prohibido robar el lock basándose en timeout de heartbeat si el proceso propietario sigue vivo.
6. **Prohibición de reparse-substitution en el lock file:** cualquier `ReparseTag != 0` al reabrir el lock aborta la operación con `GoldenLockError` (defensa contra Actor D que reemplaza el lock por un junction).

### 19.2 FSM Completa y Autoridad del Helper

```text
WHO_WRITES_AUTHORITATIVE_FSM = Elevated Helper Process ONLY
WHO_WRITES_FSM              = Elevated Helper Process ONLY
WHO_WRITES_AUTHORIZED_OPERATIONS = Elevated Helper Process ONLY
WHO_WRITES_COMMITTED        = Elevated Helper Process ONLY
WHO_WRITES_ARCHIVING_BACKUP = Elevated Helper Process ONLY
POSTVERIFY_EXECUTOR         = Unelevated Verifier Process (read-only, ejecutado por el Helper vía IPC autenticado)
BACKUP_ARCHIVER             = Elevated Helper Process
LOCK_OWNER_DURING_POSTVERIFY = Elevated Helper Process (mantiene el GoldenMutationLock continuamente)
VERIFIER_CAN_WRITE_FSM      = NO
VERIFIER_CAN_WRITE_AUTHORIZED_OPERATIONS = NO
```

```text
PREPARING -> PREPARED -> AWAITING_ELEVATION -> APPLYING -> VERIFYING_GP1 -> VERIFYING_RV2 -> VERIFYING_NODE_SET -> ARCHIVING_BACKUP -> COMMITTED
   |            |               |                 |               |               |                    |                   |
   v            v               v                 v               v               v                    v                   v
REFUSE_TO_APPLY CANCELLED ELEVATION_REJECTED ROLLBACK_REQ   ROLLBACK_REQ    ROLLBACK_REQ         ROLLBACK_REQ        RETRY_ARCHIVE
                                                 |               |               |                    |
                                                 +---------------+---------------+--------------------+
                                                                 |
                                                                 v
                                                           ROLLING_BACK
                                                            /        \
                                                           v          v
                                                      ROLLED_BACK  ROLLBACK_FAILED / INDETERMINATE
```

---

## 20. Crash Recovery Matrix

Matriz exhaustiva de recuperación ante caídas del sistema o interrupciones de proceso en cada frontera:

| Punto de Crash | Evidencia Durable en Disco | Estado Recuperado | Acción Segura al Reiniciar | ¿Auto-Recovery? | ¿Requiere HITL? |
|---|---|---|---|---|---|
| **C1: Durante PREPARING** | Manifiesto ausente o incompleto en staging | `None` / Huérfano | Limpieza de temporales. Liberar lock. Estado no modificado. | Sí | No |
| **C2: Manifiesto sellado (PREPARED)** | Manifiesto íntegro en staging, 0 nodos mutados | `PREPARED` | Operación cancelable sin efectos en FS. Liberar lock. | Sí | No |
| **C3: UAC aceptado, antes del Nodo 1** | Journal `APPLYING` en authorized store, lista de nodos mutados vacía | `APPLYING` (0 nodes) | Marcar como cancelado; ningún nodo fue alterado. Liberar lock. | Sí | No |
| **C4: Durante mutación de Nodo K (tras MUTATING(K), antes de MUTATED(K))** | Journal con `MUTATING(K)` y `FlushFileBuffers` persistido | `ROLLBACK_REQUIRED` | Helper re-escanea nodos: incluye nodos 1..K (cualquier nodo cuyo SD actual difiera del PRE) y restaura Top-Down. | No (exige UAC) | Sí (UAC para rollback) |
| **C5: Helper finalizado, antes de Post-Verificación** | Journal `APPLYING` completo (todos los nodos `MUTATED`) | `APPLYING` | Helper retoma en `VERIFYING_GP1`. | No (exige UAC) | Sí (UAC para verificar/commitear) |
| **C6: Durante verificación GP1/RV2/NodeSet** | Journal `VERIFYING_*`, todos los nodos mutados | `VERIFYING_*` | Re-ejecutar verificaciones read-only. Si pasan -> `ARCHIVING_BACKUP`; si fallan -> `ROLLBACK_REQUIRED`. | No (exige UAC) | Sí (UAC) |
| **C7: Durante ARCHIVING_BACKUP (antes de COMMITTED)** | Verificaciones OK en journal, archivo en `golden_backups` parcial | `ARCHIVING_BACKUP` | Re-copiar manifiesto a `golden_backups` de forma segura y transicionar a `COMMITTED`. Liberar lock. | No (exige UAC) | Sí (UAC) |
| **C8: Durante ejecución de Rollback** | Journal `ROLLING_BACK`, lista parcial de restaurados | `ROLLING_BACK` | Helper retoma rollback para los nodos restantes pendientes de restauración. | No (exige UAC) | Sí (UAC) |
| **C9: Sustitución de nodo / Inconsistencia FileId en Rollback** | Discrepancia entre `FileId` actual y backup | `INDETERMINATE` | Detención inmediata. Emisión de informe forense. No aplicar SD a archivo desconocido. Retener lock para inspección. | No | Sí (Alerta Crítica) |
| **C10: Lock huérfano por caída de proceso (Stale Lock)** | Archivo de lock libre en kernel pero con metadata incompleta | `STALE_LOCK` | Revalidar PID y ProcessCreationTime; si proceso no existe, iniciar recuperación de FSM según C1-C9. | Sí | Según estado C1-C9 |

---

## 21. GP1 Post-Verification (Requisito Normativo)

Inmediatamente después de que el helper completa la aplicación de cambios:
1. El helper orquesta la ejecución de `inspect_golden_protection(golden_path)` bajo el token no elevado del operador.
2. Se exige de forma estricta:
   - `result.success is True`
   - `result.state is GoldenProtectionState.HARDENED`
   - `result.evidence.pre_post_structural_match is True`
   - `result.assurance_scope == "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"`
3. Si el resultado es `WRITE_PROTECTED`, `UNPROTECTED`, `UNKNOWN` o `UNSUPPORTED`, el helper transiciona el journal a `ROLLBACK_REQUIRED`.

---

## 22. RV-2 Post-Verification (Requisito Normativo)

Inmediatamente tras validar GP1:
1. El helper orquesta la ejecución de `verify_golden_master(golden_path, expected_tree=baseline_tree, expected_runtime=baseline_runtime, critical_expectations=...)` bajo el token no elevado del operador.
2. Se exige de forma estricta:
   - `result.success is True`
   - `result.state is VerificationState.VERIFIED`
   - `result.descriptor.tree_digest == baseline_tree` (garantía de que ningún byte de contenido ni tamaño fue alterado).
3. Si el hash o estructura del árbol cambió, el helper transiciona a `ROLLBACK_REQUIRED` y registra una alerta crítica de integridad.

---

## 23. Node Set Exhaustiveness & Backup Identity Architecture

### 23.1 Verificación de Igualdad Literal del Conjunto de Nodos
Para garantizar que ningún nodo descendiente haya sido omitido del plan (incluso si dicho nodo ya se encontraba fortuitamente en estado `HARDENED` y no degradó la evaluación de GP1):
1. Se extrae el conjunto canónico del post-escaneo de GP1:
   `FRESH_POST_CANONICAL_NODE_SET = {(n.relative_path, n.node_kind, n.volume_serial_number, n.file_id) for n in gp1_post_evidence.nodes}`
2. Se extrae el conjunto del manifiesto planificado:
   `MANIFEST_NODE_SET = {(n.relative_path, n.node_kind, n.volume_serial_number, n.file_id) for n in manifest.nodes}`
3. Se exige **igualdad de conjuntos estricta**:
   `MANIFEST_NODE_SET == FRESH_POST_CANONICAL_NODE_SET`
   Si existe cualquier diferencia (nodo de más o de menos) -> fallo con `NodeSetDiscrepancyError` y transición inmediata a `ROLLBACK_REQUIRED`.

### 23.2 Identidad Estable del Archivo de Respaldo en Store Protegido (GP3 Forward Compatibility)
El respaldo no se guarda con una clave basada únicamente en el digest de contenido, sino en la identidad estable del sistema de archivos en el store protegido del sistema:
1. **Ruta de Respaldo Durable:**
   `%ProgramData%\Sky-Claw\runtime_vault\golden_backups\<vol_serial>_<root_file_id>\<policy_version>\<op_id>_manifest.json`
2. **Metadata del Respaldo:** El archivo contiene la ruta canónica original, `(VolumeSerialNumber, root_file_id)`, `TreeDigest`, versión de schema de protección y tabla completa `NodeSecurityBackup`.
3. **Inmutabilidad y No-Sobreescritura:** La DACL de `golden_backups` restringe la escritura exclusivamente a `Administrators` y `SYSTEM` (solo lectura para `Authenticated Users`), impidiendo que un Actor D modifique los descriptores PRE. Si existen dos Golden Masters con idéntico `TreeDigest` pero ubicados en distintas carpetas/volúmenes, cada uno posee su clave independiente en `golden_backups`.
4. **Autenticación en GP3:** La capability GP3 autentica el respaldo comprobando que reside dentro del almacén protegido por el SO (`%ProgramData%\Sky-Claw\runtime_vault\golden_backups\`), revalida la coincidencia física de `(VolumeSerialNumber, root_file_id)` y ejecuta la desprotección bajo el mismo modelo de helper elevado.

---

## 24. Taxonomía de Errores

| Error de Dominio | Causa / Condición | Estado FSM / Desenlace |
|---|---|---|
| `ProtectionPrecheckError` | Path inválido, no absoluto, no directorio, o plataforma no-Windows | `REFUSE_TO_APPLY` (sin mutación, sin UAC) |
| `UnsafeParentError` | Parent con `DELETE_CHILD`, `WRITE_DAC` o rename viable | `REFUSE_TO_APPLY` (sin mutación, sin UAC) |
| `InventoryLinkError` / `PlanCreationError` | Error al leer SDs originales, reparse point detectado, o drift PRE/POST | `REFUSE_TO_APPLY` (sin mutación, sin UAC) |
| `DuplicateFileIdError` | Múltiples rutas en el subárbol comparten el mismo `FileId` (hardlink interno) | `REFUSE_TO_APPLY` (sin mutación, sin UAC) |
| `QuiescenceViolationError` | Handle de escritura o mapping preexistente en un nodo (`ERROR_SHARING_VIOLATION`) | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| `GoldenLockError` | No se pudo adquirir el `GoldenMutationLock` por conflicto concurrente activo | `REFUSE_TO_APPLY` / Aborto |
| `ElevationRejectedError` | Usuario canceló o denegó el prompt de UAC | `ELEVATION_REJECTED` (sin mutación) |
| `HelperExecutionError` | Helper falló durante la mutación de nodos | Transición a `ROLLBACK_REQUIRED` |
| `PostVerificationError` | GP1 no emitió `HARDENED` o RV2 no emitió `VERIFIED` | Transición a `ROLLBACK_REQUIRED` |
| `NodeSetDiscrepancyError` | Discrepancia entre `MANIFEST_NODE_SET` y `FRESH_POST_CANONICAL_NODE_SET` | Transición a `ROLLBACK_REQUIRED` |
| `RollbackFailureError` | Helper falló durante la restauración de SDs PRE | Transición a `ROLLBACK_FAILED` / `INDETERMINATE` |

---

## 25. Estrategia de Tests Futuros (Plan para Implementación)

Los tests de GP2 se implementarán en módulos dedicados bajo `tests/`:
- `tests/test_runtime_vault_protection_apply_plan.py` (Unit tests de planificación y sellado).
- `tests/test_runtime_vault_protection_apply_fsm.py` (Unit tests de la máquina de estados, locks y crash recovery).
- `tests/test_runtime_vault_protection_apply_integration.py` (Tests de integración en Windows TEMP únicamente, sin tocar Golden real).

### Configuración Canónica de Fixture para Tests de Integración en Windows TEMP
Para garantizar que los tests de integración en `%TEMP%` sean rigurosos, reproducibles y limpien su estado sin dejar residuos:
1. **Contenedor Seguro por Construcción (sin contradicción con §14.1):** El fixture de test `temp_golden_harness` estructura el árbol bajo un contenedor seguro `fixture_parent/golden_root` cuyo DACL **no concede `WRITE_DAC` al harness de test** durante la fase de mutación. La separación de roles es normativa:
   - **Construcción y teardown del contenedor:** un helper de fixture (subproceso con token elevado *de test*, o un mock de la API Win32) crea `fixture_parent` con DACL que excluye `WRITE_DAC`/`DELETE_CHILD` para Authenticated Users y concede al harness únicamente `READ_CONTROL | FILE_TRAVERSE` durante la fase del test.
   - **Eliminación posterior:** el helper de fixture elevado (o un mock equivalente) captura el SD original, aplica una DACL de teardown que concede `FILE_ALL_ACCESS` al harness, ejecuta `shutil.rmtree` y restaura el SD original capturado. El teardown nunca depende de que el harness conserve `WRITE_DAC` durante el test.
2. **Prueba Obligatoria de Parent Inseguro:** Se incluye un test específico que evalúa un parent estándar de usuario con `WRITE_DAC` activo para verificar que GP2 emite `UnsafeParentError`, retorna `REFUSE_TO_APPLY` y realiza cero mutaciones sobre el disco. Este test no requiere helper de fixture: la DACL del parent es la que un usuario normal recibe en `%TEMP%` por defecto.
3. **GP2-T00 específico (ancla):** aserta que `fixture_parent` durante la ejecución del test **no concede `WRITE_DAC` al proceso de pytest** (consultando `AccessCheck` o `GetEffectiveRightsFromAcl`); falla si concede. Esto evita que el oráculo "pass for the wrong reason" detectado por Qodo en este PR.

### Casos de Test Obligatorios:
- **GP2-T00:** El fixture de test verifica explícitamente que `fixture_parent` no concede `WRITE_DAC` al proceso de pytest durante el test; teardown limpio sin residuos, ejecutado por helper elevado o mock de fixture.
- **GP2-T01:** Planificación exitosa genera manifiesto canónico con SDs self-relative y FileIds.
- **GP2-T02:** Reparse point en cualquier nodo del árbol aborta la planificación con excepción `InventoryLinkError` y estado `REFUSE_TO_APPLY`.
- **GP2-T03:** Parent con `FILE_DELETE_CHILD` aborta la planificación (`UnsafeParentError`, estado `REFUSE_TO_APPLY`).
- **GP2-T04:** Parent con `WRITE_DAC` aborta la planificación (`UnsafeParentError`, estado `REFUSE_TO_APPLY`).
- **GP2-T05:** Parent seguro permite generar plan con orden Bottom-Up estricto.
- **GP2-T06:** Target DACL contiene exactamente la Owner Rights ACE `S-1-3-4` con `FILE_GENERIC_READ`.
- **GP2-T07:** Target DACL no contiene ACEs de denegación ni flags de herencia.
- **GP2-T08:** Helper revalida `FileId` y `pre_sd_sha256` sobre el handle abierto antes de mutar cada nodo.
- **GP2-T09:** Helper detecta reemplazo de archivo por symlink en tiempo de ejecución y aborta.
- **GP2-T10:** Mutación exitosa de árbol temporal culmina en GP1 `HARDENED` y RV-2 `VERIFIED`.
- **GP2-T11:** Fallo simulado en el nodo intermedio K dispara rollback Top-Down transaccional.
- **GP2-T12:** Rollback restaura exactamente los campos `OWNER`, `GROUP`, `DACL` y flags de herencia de DACL originales; incluye explícitamente un fixture con `PRE_DACL_PROTECTED = False` y verifica que tras APPLY + fallo + rollback, el nodo vuelve a tener `SE_DACL_PROTECTED = False`. Fixture con SACL presente valida que la SACL se mantiene inalterada.
- **GP2-T13:** Fallo en post-verificación GP1 dispara rollback transaccional.
- **GP2-T14:** Fallo en post-verificación RV-2 (digest alterado) dispara rollback transaccional.
- **GP2-T15:** Simulación de crash en cada estado del FSM (incluyendo crash entre mutación y registro con WAL respaldado por `FlushFileBuffers`, rechazo de UAC resultando en `ELEVATION_REJECTED`, y crash en `ARCHIVING_BACKUP`) recupera la acción segura correcta. Ancla AST/comportamiento: verifica que la escritura del journal usa `FlushFileBuffers` y que la simulación de crash entre `write()` y `flush()` resulta en `INDETERMINATE` (no en `COMMITTED` silencioso).
- **GP2-T16:** AST Anchor: `protection_journal.py` no importa `sky_claw.app.db.journal`.
- **GP2-T17:** AST Anchor: El módulo no utiliza strings de cuentas localizadas ("Todos", "Administradores"), solo SIDs.
- **GP2-T18:** Presencia de hardlinks internos (mismo `(VolumeSerialNumber, FileId)` en 2 rutas) aborta con `DuplicateFileIdError` y estado `REFUSE_TO_APPLY`.
- **GP2-T19:** Omisión de un nodo pre-endurecido en el manifiesto es detectada por la verificación de igualdad `MANIFEST_NODE_SET == FRESH_POST_CANONICAL_NODE_SET` y aborta la transacción.
- **GP2-T20:** `GoldenMutationLock` serializa dos llamadas concurrentes sobre el mismo Golden rechazando la segunda con `GoldenLockError`. Aserta además que el contrato de apertura del lock usa `dwDesiredAccess = GENERIC_READ | GENERIC_WRITE` y `dwShareMode = 0`.
- **GP2-T21:** La mutación se ejecuta sobre el mismo handle (`SetSecurityInfo(handle)`), fallando si el archivo se intenta reemplazar bajo el handle abierto.
- **GP2-T22:** El store de operaciones y backup en `%ProgramData%` permite resolver el plan de forma transparente y autenticar el respaldo en escenarios de elevación Over-The-Shoulder (OTS).
- **GP2-T23:** Oráculo paramétrico de la matriz de share-semantics del §4.1.1. Para cada `dwShareMode ∈ {0, FILE_SHARE_READ, FILE_SHARE_READ|FILE_SHARE_WRITE, FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE}`, abre un writer con `dwDesiredAccess = GENERIC_WRITE`; el probe del helper debe fallar con `ERROR_SHARING_VIOLATION` y el desenlace contractual de la fila correspondiente. Sin parametrización: el test enumera literalmente las cuatro filas. No se admite mock que sustituya `CreateFileW`; el probe debe ejercitar el camino real (con la dependencia real de `pywin32`/`ctypes`).
- **GP2-T24:** Mapping de memoria escribible preexistente (`CreateFileMapping` con `PAGE_READWRITE`) produce `ERROR_SHARING_VIOLATION` en el probe; rerun del probe antes de `ARCHIVING_BACKUP` detecta la persistencia y aborta antes de `COMMITTED`.
- **GP2-T25:** Intento de escritura en `AUTHORIZED_OPERATIONS` o `golden_backups` por parte de un proceso no elevado es bloqueado por la DACL del kernel (`ACCESS_DENIED`). Cubre además `trusted_goldens.json` y `locks/`.
- **GP2-T26 (ancla de binding pre_sd_bytes_b64):** test paramétrico que produce dos variantes del mismo plan —`(a)` plan correcto y `(b)` plan con `pre_sd_sha256` legítimo y `pre_sd_bytes_b64` adulterado— y verifica que el helper rechaza `(b)` con `PreSdBytesHashMismatchError` *antes* de la primera mutación, y acepta `(a)`. Complementa al mutante `M-B1`.
- **GP2-T27:** AST Anchor de Simetría de Superficies: enumera por introspección/igualdad literal el set de herramientas mutadoras registradas tanto en `SupervisorAgent` (`tool_dispatcher`) como en `AsyncToolRegistry` (`LLMRouter`); detecta asimetrías de cableado y de `HITLGuard`/UAC. El ancla es por **igualdad literal del set** (no por muestreo), siguiendo el patrón de `RITUAL_TOOL_MAP` del repositorio.
- **GP2-T28 (TGR unprivileged mutation):** un proceso no elevado intenta añadir, modificar o borrar una entrada de `trusted_goldens.json` o sustituirlo por un junction -> `ACCESS_DENIED` por DACL, `REFUSE_TO_PLAN`.
- **GP2-T29 (PPSC sin UAC):** el helper no crea `authorized_plan.json` si el operador cancela o rechaza la PPSC; el desenlace es `REFUSE_TO_PLAN` y la rama `staging` queda intacta.
- **GP2-T30 (plan-specific confirmation parameters):** aserta que el diálogo PPSC muestra literalmente `operation_id`, `canonical_root`, `VolumeSerialNumber`, `root_file_id`, `TreeDigest`, `node_count`, `policy_version` y exige consentimiento explícito antes de crear `authorized_plan.json`.
- **GP2-T31 (UAC != plan authorization):** un UAC aceptado sin PPSC posterior no produce `authorized_plan.json`; el desenlace es `REFUSE_TO_PLAN` con traza en el journal.
- **GP2-T32 (privileged privilege scope):** DACL preexistente que deniega `WRITE_DAC` a Administrators + elevación OTS secundaria: el helper habilita `SeRestorePrivilege` antes de la apertura del handle de mutación; si la apertura sigue devolviendo `ACCESS_DENIED`, transición a `ROLLBACK_REQUIRED` (no se activa `SeTakeOwnershipPrivilege`).
- **GP2-T33 (verifier cannot write FSM):** el verificador no elevado intenta escribir en `AUTHORIZED_OPERATIONS` o transicionar a `COMMITTED`/`ARCHIVING_BACKUP` -> `ACCESS_DENIED` por DACL y desenlace `REFUSE_TO_PLAN`/`INDETERMINATE`.

---

## 26. Matriz de Mutantes Causales

| ID | Mutante Introducido | Detección / Test Esperado |
|---|---|---|
| **M01** | Omitir backup de SD en un nodo | Fallo en validación de completitud del manifiesto |
| **M02** | Mutar filesystem antes de sellar el manifiesto | Fallo en FSM (estado no es `PREPARED`) |
| **M03** | Aceptar drift de identidad (`FileId` distinto) | Revalidación del helper aborta con `IdentityMismatchError` |
| **M04** | Aceptar drift de descriptor PRE (`pre_sd_sha256` distinto) | Revalidación del helper aborta con `DescriptorDriftError` |
| **M05** | Parent inseguro emite advertencia y continúa | Fallo en test de parent policy (`UnsafeParentError` no lanzado) |
| **M06** | Omitir un archivo descendiente que ya estaba HARDENED | `NodeSetDiscrepancyError` detecta discrepancia en `MANIFEST_NODE_SET` |
| **M07** | Orden de aplicación Top-Down en lugar de Bottom-Up | Test de orden de mutación detecta inversión de secuencia |
| **M08** | Permitir propagación de herencia no observada (`SE_DACL_PROTECTED` ausente) | Post-validation semántica falla por flags de control incompletos |
| **M09** | Declarar éxito antes de ejecutar GP1 | Test de contrato GP2 exige `post_gp1_result` verificado |
| **M10** | Declarar éxito cuando GP1 devuelve `UNKNOWN` | Contrato exige `state == HARDENED`; `UNKNOWN` dispara rollback |
| **M11** | Declarar éxito cuando GP1 devuelve `WRITE_PROTECTED` | Contrato exige `state == HARDENED`; `WRITE_PROTECTED` dispara rollback |
| **M12** | Omitir verificación RV-2 post-mutación | Test de contrato GP2 exige `post_rv2_result` verificado |
| **M13** | Perder registro de nodos mutados en crash | Protocolo WAL (`MUTATING`), `FlushFileBuffers` y re-escaneo restauran todos los nodos tocados |
| **M14** | Reset recursivo de ACLs desde el root en rollback | Test de rollback valida que cada nodo recibe su SD individual |
| **M15** | Seguir reparse point durante inventario o aplicación | Primitiva canónica lanza `InventoryLinkError` / Helper aborta con `REFUSE_TO_APPLY` |
| **M16** | Helper acepta manifiesto no sellado | Helper exige coincidencia con `--staging-digest` |
| **M17** | Helper acepta manifiesto con hash discrepante | Helper aborta con `ManifestDigestMismatchError` |
| **M18** | Helper acepta ruta arbitraria por CLI | CLI de helper solo acepta `--operation-id` y `--staging-digest` |
| **M19** | Helper acepta root fuera de la configuración del Vault | Helper confina root a rutas registradas en Trusted Registry |
| **M20** | Helper ejecuta comando shell arbitrario | Helper solo implementa el bucle cerrado de mutación de SD |
| **M21** | Habilitar privilegios permanentemente en el token | Guarda `finally` desactiva privilegios inmediatamente |
| **M22** | Ignorar `ERROR_NOT_ALL_ASSIGNED` en `AdjustTokenPrivileges` | Verificación explícita de `GetLastError()` lanza error |
| **M23** | Omitir restauración de `PreviousState` de privilegios | Bloque de limpieza restaura estado previo |
| **M24** | HMAC derivado de manifiesto atacable considerado autenticación | Modelo de confianza declara formalmente los límites de SHA-256 |
| **M25** | Continuar tras fallo de `SetSecurityInfo` en un nodo | Helper detiene bucle y transiciona a rollback |
| **M26** | Restaurar SD sobre un nodo que cambió de `FileId` | Helper aborta rollback en ese nodo y declara `INDETERMINATE` |
| **M27** | Permitir override HITL ante parent inseguro | Contrato v1 no expone parámetro de bypass |
| **M28** | Transición a COMMITTED antes de archivar backup permanente | FSM exige `ARCHIVING_BACKUP` completado antes de `COMMITTED` |
| **M29** | Aceptar hardlink interno (duplicate FileId) en el plan | `DuplicateFileIdError` aborta en planificación con `REFUSE_TO_APPLY` |
| **M30** | Mutar por path (`SetNamedSecurityInfoW`) en lugar de handle abierto | Test de reemplazo TOCTOU detecta desvío de objeto |
| **M31** | Omitir `GoldenMutationLock` permitiendo ejecución concurrente | Test de colisión de locks detecta falta de serialización |
| **M32** | Habilitar `FILE_SHARE_WRITE` accidentalmente en el handle del nodo | Test `GP2-T23` detecta violación de quiescencia |
| **M33** | Ignorar `ERROR_SHARING_VIOLATION` como advertencia blanda | Test `GP2-T23` exige `REFUSE_TO_APPLY` o `ROLLBACK_REQUIRED` |
| **M34** | Atacante genera plan en staging para directorio no registrado | Helper rechaza el plan antes de `AUTHORIZED_OPERATIONS` |
| **M35** | Proceso no elevado intenta escribir `COMMITTED` en el journal | DACL del kernel deniega la escritura (`ACCESS_DENIED`) |
| **M36** | `protect_golden_master` registrado en GUI pero omitido en el registry del agente LLM | Test AST `GP2-T27` detecta asimetría entre superficies |
| **M-Q1** | Probe de quiescencia implementado con un solo handle (`READ_CONTROL | WRITE_DAC`, `FILE_SHARE_READ`) en lugar del doble handle normativo de §4.1/§12.2 | Test `GP2-T23` con `dwShareMode ∈ {FILE_SHARE_READ, FILE_SHARE_READ|FILE_SHARE_WRITE}` detecta que el writer pasa el probe indebidamente |
| **M-Q2** | Writer con `FILE_SHARE_READ | FILE_SHARE_WRITE` queda activo y el helper muta la DACL sin abortar | Test `GP2-T23` exige `ERROR_SHARING_VIOLATION` y `REFUSE_TO_APPLY` para esa fila de la matriz |
| **M-A1** | Helper acepta UAC sin confirmación PPSC posterior, o acepta un UAC como sustituto de PPSC | Test `GP2-T31` exige `REFUSE_TO_PLAN` cuando PPSC no se completa; `GP2-T30` exige que el diálogo PPSC exhiba los siete campos |
| **M-A2** | Proceso no elevado modifica `trusted_goldens.json` (añade entrada, reemplaza archivo, junction) | Test `GP2-T28` exige `ACCESS_DENIED` y `REFUSE_TO_PLAN` |
| **M-B1** | `pre_sd_sha256` correcto y `pre_sd_bytes_b64` adulterado; helper lo acepta y rollback aplica bytes adversarios | Test `GP2-T26` exige `PreSdBytesHashMismatchError` y `REFUSE_TO_PLAN` antes de la primera mutación |
| **M-R1** | Rollback omite `UNPROTECTED_DACL_SECURITY_INFORMATION` cuando el SD PRE no tenía `SE_DACL_PROTECTED`, o viceversa | Test `GP2-T12` (caso con `PRE_DACL_PROTECTED = False`) exige que tras rollback el flag vuelva a `False` |
| **M-F1** | Verifier no elevado escribe `FSM`, `COMMITTED` o `ARCHIVING_BACKUP` en `AUTHORIZED_OPERATIONS` | Test `GP2-T33` exige `ACCESS_DENIED` por DACL del kernel |
| **M-P1** | Helper habilita `SeRestorePrivilege` *después* de la apertura del handle, perdiendo la oportunidad frente a un DACL restrictivo | Test `GP2-T32` exige habilitar antes de la apertura y `ROLLBACK_REQUIRED` si `ACCESS_DENIED` persiste |
| **M-W1** | Falta el rerun del probe antes de `ARCHIVING_BACKUP`; un writer que abrió durante la mutación sobrevive a `COMMITTED` | Test que detecta que `COMMITTED` se alcanza con un writer activo |

---

## 27. Performance

- **Complejidad Temporal:** $O(N)$ donde $N$ es el número total de nodos en el subárbol.
- **Rendimiento Esperado:** En el rig de referencia de Skyrim SE (~16.000 archivos), la captura y cálculo del plan insume < 1,5 segundos; la aplicación de `SetSecurityInfo` sobre handles abiertos por el helper insume < 2,5 segundos en discos SSD NVMe.
- **I/O Bloqueante Controlado:** Todas las operaciones de I/O en el proceso principal se ejecutan sincrónicamente en funciones puras y se envuelven en `asyncio.to_thread` en la capa de orquestación, siguiendo el patrón del repositorio.

---

## 28. Limitaciones de Seguridad Declaradas

1. **Compromiso Administrativo / SYSTEM:** Un administrador malicioso con elevación UAC o un proceso ejecutándose como `NT AUTHORITY\SYSTEM` puede en cualquier momento otorgarse `SeTakeOwnershipPrivilege` o `SeRestorePrivilege`, reescribir las ACLs y mutar el Golden Master. La garantía de `HARDENED` aplica frente al **token interactivo normal no elevado**.
2. **No Atomicidad a Nivel de Sistema de Archivos:** NTFS no provee transacciones multi-archivo (tras la obsolescencia de TxF). La recuperabilidad de GP2 es de nivel aplicativo mediante journal y rollback compensatorio con protocolo WAL. Un fallo catastrófico de energía durante una mutación individual es recuperado mediante el análisis del journal en el siguiente arranque.
3. **Restricción de Directorio Contenedor (Parent):** Si el Golden Master reside en una carpeta donde el usuario tiene permisos amplios de borrado de hijos (`FILE_DELETE_CHILD`), la protección no puede aplicarse hasta que el Golden sea reubicado en una ruta segura.

---

## 29. Referencias Primarias Microsoft

- [MS-DTYP] §2.4.6: Security Descriptor Description and Structure — *learn.microsoft.com/openspecs/windows_protocols/ms-dtyp/7d4dac05-9cef-4563-a058-f108abec36d4*
- [MS-ADTS] §6.1.3.4: Blocking Implicit Owner Rights — *learn.microsoft.com/openspecs/windows_protocols/ms-adts/fb7c101d-ec8b-4fbf-bca8-7d7c2d747d0c*
- `SetSecurityInfo` function — *learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo*
- `GetSecurityInfo` function — *learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo*
- `FlushFileBuffers` function — *learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers*
- `GetSecurityDescriptorLength` function — *learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-getsecuritydescriptorlength*
- `GetSecurityDescriptorControl` function — *learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-getsecuritydescriptorcontrol*
- `AdjustTokenPrivileges` function — *learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-adjusttokenprivileges*
- `GetFileInformationByHandleEx` function — *learn.microsoft.com/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex*
- `GetProcessTimes` function — *learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes*
- Well-known SIDs / Owner Rights SID (`S-1-3-4`) — *learn.microsoft.com/windows/win32/secauthz/well-known-sids*
- Order of ACEs in a DACL — *learn.microsoft.com/windows/win32/secauthz/order-of-aces-in-a-dacl*
- File Access Rights Constants — *learn.microsoft.com/windows/win32/fileio/file-access-rights-constants*

---

## 30. Preguntas No Bloqueantes (Futuras Iteraciones)

1. **GP2.1 Dedicated Container Provisioner:** ¿Diseñar una utilidad que cree automáticamente una carpeta contenedor dedicada con permisos restringidos para albergar Golden Masters cuando el parent original sea `UNSAFE`? (Feature futura independiente).
2. **Compresión de Manifiesto en Storage:** ¿Comprimir con gzip los buffers Base64 de `NodeSecurityBackup` en instalaciones con más de 100.000 archivos para reducir el uso de disco del journal? (Optimización no funcional).
3. **Telemetría Estructurada de Tiempos de Helper:** ¿Medir tiempos de ejecución por lote de nodos en el helper para ajustar métricas de progreso en la UI de Sky-Claw? (Mejora de UX).

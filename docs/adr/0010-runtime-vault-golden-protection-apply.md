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
3. **Garantía Rigurosa de Quiescencia y Mutación Atada a Handle (Handle-Bound Mutation) con doble apertura normativa:** un **probe handle transitorio** con `dwDesiredAccess` normativo en §4.1 (write-data mask) y `dwShareMode = 0` detecta writers preexistentes y mappings escribibles vía `ERROR_SHARING_VIOLATION`; tras la verificación positiva se abre un **handle de mutación** distinto con `dwDesiredAccess = READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES` (contrato único normativo, idéntico en §2.3/§4.1/§12.2; `WRITE_OWNER` es imprescindible porque el rollback restaura OWNER/GROUP vía `SetSecurityInfo`; `FILE_READ_ATTRIBUTES` es requerido por `GetFileInformationByHandleEx` en la revalidación de identidad/hardlink y es clase de metadatos que no participa en la compartición, ver §12.2) y `dwShareMode = FILE_SHARE_READ` sobre el cual se ejecutan lectura PRE, `SetSecurityInfo` y lectura POST (binding por `(VolumeSerialNumber, FileId)` revalidado en cada paso);
4. **Autoridad Transaccional Privilegiada Continua (FSM Writer):** el helper elevado es el **único escritor del FSM autoritativo y del journal de operaciones**, reteniendo el `GoldenMutationLock` durante las fases de mutación, post-verificación (orquestando la ejecución de GP1/RV2/NodeSet bajo token no elevado vía IPC autenticado, **read-only**), archivado de respaldo y commit final; el verificador no elevado nunca escribe `FSM`, `COMMITTED` ni `ARCHIVING_BACKUP`;
5. **Simetría Rigurosa de Superficies Hermanas (AGENTS.md):** exposición uniforme y cableado simétrico en la GUI (`SupervisorAgent` -> `tool_dispatcher`) y en el agente LLM (`AsyncToolRegistry` -> `LLMRouter`), con ancla de test que enumera por introspección/igualdad literal ambos cableados y previene asimetrías de elevación;
6. **Serialización Cross-Process por Kernel Handle (`GoldenMutationLock`):** exclusión mutua respaldada por el kernel de Windows mediante `CreateFileW` con `dwDesiredAccess = GENERIC_READ | GENERIC_WRITE` (o equivalente explícito) y `dwShareMode = 0` sobre un archivo en `locks/` (DACL protegida), con defensa contra reuso de PID mediante `GetProcessTimes`;
7. **Cero mutación no planificada:** mitigación de propagación silenciosa de herencia (`SetSecurityInfo` con `PROTECTED_DACL_SECURITY_INFORMATION` en apply, `UNPROTECTED_DACL_SECURITY_INFORMATION` condicional en rollback cuando el SD PRE no tenía `SE_DACL_PROTECTED`);
8. **Verificación post-mutación de triple eje:** post-verificación de acceso GP1 (`state == HARDENED`), post-verificación de integridad de contenido RV-2 (`TreeDigest` intacto y `VERIFIED`), y verificación de igualdad literal exhaustiva del conjunto de nodos (`MANIFEST_NODE_SET == FRESH_POST_CANONICAL_NODE_SET`); el PR body declara este **triple gate**;
9. **Rollback transaccional fail-closed con WAL durable:** restauración compensatoria ante cualquier fallo o inconsistencia intermedia con protocolo Write-Ahead Logging (WAL) y persistencia física obligatoria mediante `FlushFileBuffers(journal_handle)` antes de `SetSecurityInfo`.

---

## 2. Decisión

1. **Target de Estado:** El único estado final de éxito admisible para GP2 es **`HARDENED`** con `success == True` y `assurance_scope == "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"`. Un resultado final que termine en `WRITE_PROTECTED`, `UNPROTECTED`, `UNKNOWN` o `UNSUPPORTED` constituye un fallo de la operación y dispara rollback obligatorio.
   - **Distinción normativa de `success`:** el `success` de este punto se refiere al **resultado transaccional GP2** (`GP2Result`), no al `GoldenProtectionResult` del inspector GP1. Según el contrato vigente de `protection.py`, `GoldenProtectionResult.success == (state != UNKNOWN)`: una **clasificación concluyente** que también es `True` para `UNSUPPORTED`, `UNPROTECTED` y `WRITE_PROTECTED`. Por tanto, el gate GP2 **no** puede depender de `GoldenProtectionResult.success`; exige **independientemente** `state == HARDENED` (ver §21). Queda prohibido usar `GoldenProtectionResult.success` como sinónimo de endurecimiento.
2. **Mutación por Nodo Bottom-Up (Deepest Descendants First):** GP2 muta individualmente cada archivo y directorio del subárbol comenzando por las hojas más profundas y finalizando en la raíz del Golden (`root`). Cada nodo se configura explícitamente con `PROTECTED_DACL_SECURITY_INFORMATION`, desvinculando la herencia antes de que sus ancestros sean modificados, eliminando cualquier riesgo de propagación no planificada hacia descendientes abiertos.
3. **Garantía de Quiescencia y Mutación Estrictamente Atada a Handle (Handle-Bound Mutation) — DOS handles normativos por nodo:**
   - **Probe handle transitorio (detección de writers/mappings):** antes de abrir el handle de mutación, el helper abre cada nodo con
     `dwDesiredAccess = FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | DELETE` para archivos, o `FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | FILE_DELETE_CHILD | FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | DELETE` para directorios; `dwShareMode = 0`; `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`; `OPEN_EXISTING`. El probe se cierra inmediatamente tras la verificación.
   - **Handle de mutación (binding por handle):** tras la verificación positiva, el helper abre un segundo handle con `dwDesiredAccess = READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES` (**contrato único normativo**, idéntico en §1.3/§4.1/§12.2; `WRITE_OWNER` es imprescindible porque el rollback restaura OWNER/GROUP vía `SetSecurityInfo`; `FILE_READ_ATTRIBUTES` es requerido por `GetFileInformationByHandleEx` y es clase de metadatos que no participa en la compartición, ver §12.2); `dwShareMode = FILE_SHARE_READ` (sin `FILE_SHARE_WRITE`, sin `FILE_SHARE_DELETE`); mismas flags. Sobre este handle se ejecutan: validación de `(VolumeSerialNumber, FileId)` y `reparse_tag`, lectura de SD PRE, WAL durable con `FlushFileBuffers(journal_handle)`, `SetSecurityInfo` y re-lectura de SD POST.
   - **Razón del doble handle:** la compartición de Win32 se evalúa contra el `dwShareMode` de los handles ya abiertos; un writer que abrió con `FILE_SHARE_READ | FILE_SHARE_WRITE` (patrón habitual de MO2, engines, antivirus, indexadores) no produce `ERROR_SHARING_VIOLATION` frente al handle de mutación porque `READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES` **no son clases de datos** (read/write/delete) y por tanto no disparan la comprobación de compartición de `CreateFileW`. Sólo un probe que solicite acceso de datos de escritura con `dwShareMode = 0` colisiona de forma determinista con cualquier writer o `CreateFileMapping` escribible preexistente.
   - **Limitación declarada:** NTFS no revoca los derechos concedidos en handles ya abiertos cuando se cambia la DACL, y el probe es una **instantánea**: detecta todo handle de datos preexistente en el momento del probe (el probe usa `dwShareMode = 0`, que por la semántica bidireccional de `CreateFileW` colisiona con todo handle con acceso de datos, sin importar el share mode que éste haya concedido — ver HANDLE SHARE MATRIX §4.1.1). La garantía v1 es: (a) **detección** de todo handle de datos preexistente en el probe y en el rerun pre-`ARCHIVING_BACKUP`; (b) **negación fail-closed** de la transición a `COMMITTED` si el probe o el rerun detectan writer; (c) **detección de drift de contenido** vía RV-2 (`TreeDigest`) si un writer o mapping abre después del rerun y escribe. **No se afirma exclusividad absoluta post-`COMMITTED`**: un proceso con derechos puede reabrir y escribir; la garantía completa requiere un módulo de enforcement distinto (no GP2 v1).
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
    - Tras finalizar la mutación, el helper orquesta la post-verificación (GP1, RV2 y NodeSet) ejecutando un proceso verificador no elevado bajo el token del usuario y recibiendo el resultado mediante el canal IPC autenticado normativo de **§12.3** (pipe anónimo heredado por el hijo lanzado por el helper + verificación de peer PID/creation-time/imagen + nonce por operación).
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
5. **Contrato de `success` del Inspector GP1 vs gate GP2:** `GoldenProtectionResult.success` se define en `protection.py` como `state is not GoldenProtectionState.UNKNOWN`: representa una **clasificación concluyente**, y es `True` también para `UNSUPPORTED`, `UNPROTECTED` y `WRITE_PROTECTED`. **No significa** que el Golden haya alcanzado `HARDENED`. El gate de éxito GP2 exige por separado e incondicionalmente `result.state == HARDENED` (además de `result.success is True`, que por sí solo no es suficiente). El `success` del resultado transaccional `GP2Result` (operación concluida en el estado objetivo) es un contrato distinto del `success` del inspector GP1 y no debe confundirse con él. `message: str` permanece estrictamente vacío `""` en caso de éxito de cualquiera de los dos contratos.

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

Tras la verificación, **el probe se cierra** y se abre un **handle de mutación** independiente con `READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES` (contrato único normativo, idéntico en §1.3, §2.3 y §12.2; `WRITE_OWNER` para que el rollback restaure OWNER/GROUP con `SetSecurityInfo`; `FILE_READ_ATTRIBUTES` requerido por `GetFileInformationByHandleEx`, clase de metadatos que no participa en la compartición) y `dwShareMode = FILE_SHARE_READ` (sin `FILE_SHARE_WRITE`, sin `FILE_SHARE_DELETE`). El binding por `(VolumeSerialNumber, FileId)` revalidado en cada paso del handle de mutación neutraliza la ventana TOCTOU entre probe y `SetSecurityInfo`.

### 4.1.1 HANDLE SHARE MATRIX (probe contra handles preexistentes)

**Semántica bidireccional de compartición (Win32, `CreateFileW`):** la compatibilidad de una nueva apertura se evalúa **en ambas direcciones** contra cada handle preexistente del archivo:
1. El handle preexistente debe haber **concedido** (en su `dwShareMode`) todas las clases de acceso que la nueva apertura solicita; si no -> `ERROR_SHARING_VIOLATION`.
2. La nueva apertura debe **conceder** (en su propio `dwShareMode`) las clases de acceso que el handle preexistente obtuvo; si la nueva apertura usa `dwShareMode = 0`, no concede nada -> `ERROR_SHARING_VIOLATION` contra **todo** handle preexistente que tenga acceso de datos (`FILE_READ_DATA`, `FILE_WRITE_DATA`, `FILE_APPEND_DATA`, `FILE_WRITE_ATTRIBUTES`, `FILE_WRITE_EA`, `FILE_ADD_FILE`, `FILE_ADD_SUBDIRECTORY`, `FILE_DELETE_CHILD` o `DELETE`).

   **Condición de disparo (normativa, `IoCheckShareAccess` — ver §29):** la comprobación de compartición de ambas direcciones se ejecuta **sólo si la nueva apertura solicita alguna clase de datos** — `FILE_READ_DATA`/`FILE_EXECUTE` (read), `FILE_WRITE_DATA`/`FILE_APPEND_DATA` (write) o `DELETE`. Las clases de **metadatos y de control** —`READ_CONTROL`, `WRITE_DAC`, `WRITE_OWNER`, `SYNCHRONIZE`, `FILE_READ_ATTRIBUTES`, `FILE_READ_EA` sin datos— **no participan en la compartición**: una apertura que sólo pide estas clases **retorna éxito sin evaluar la dirección 2** y no puede producir `ERROR_SHARING_VIOLATION`, ni siquiera contra un handle abierto con `dwShareMode = 0`. Por eso el **handle de mutación** (`READ_CONTROL | WRITE_DAC | WRITE_OWNER`, sin ninguna clase de datos) **no** detecta writers por sí mismo —la detección es responsabilidad exclusiva del probe (§4.1)— y tampoco falla con `ERROR_SHARING_VIOLATION` ante un writer preexistente (esto es, además, lo que permite reescribir la DACL de un archivo abierto por otro proceso).

El probe normativo (§4.1) usa `dwShareMode = 0`, por lo que **colisiona con todo handle preexistente que tenga acceso de datos**, con independencia del `dwShareMode` que ese handle haya concedido. La tabla siguiente es **normativa**: seis filas handle-level, cada una con desenlace contractual único. **Es imposible que una misma fila tenga simultáneamente `ERROR_SHARING_VIOLATION` y "open exitoso".** El caso de mappings escribibles NO es una fila de esta matriz: se trata por separado en §4.1.2 (`WRITABLE MAPPING CASE`).

| ID | Handle preexistente `dwDesiredAccess` | Handle preexistente `dwShareMode` | ¿Probe (share=0) colisiona? | Desenlace del probe | Estado GP2 |
|---|---|---|---|---|---|
| H1 | `GENERIC_WRITE` (incluye `FILE_WRITE_DATA`) | `0` (exclusivo) | SÍ (regla 2: el probe no concede nada; regla 1: el handle no concedió nada) | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| H2 | `GENERIC_WRITE` | `FILE_SHARE_READ` | SÍ (regla 2) | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| H3 | `GENERIC_WRITE` | `FILE_SHARE_READ \| FILE_SHARE_WRITE` | SÍ (regla 2) | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| H4 | `GENERIC_WRITE` | `FILE_SHARE_READ \| FILE_SHARE_WRITE \| FILE_SHARE_DELETE` | SÍ (regla 2: el probe con share=0 no comparte el acceso write/delete del handle) | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| H5 | `GENERIC_READ` (sólo lectura) | `FILE_SHARE_READ` | SÍ (regla 2: el probe no comparte el read del handle) | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| H6 | `GENERIC_READ` (sólo lectura) | `FILE_SHARE_READ \| FILE_SHARE_WRITE \| FILE_SHARE_DELETE` | SÍ (regla 2: el probe no comparte el read del handle) | `ERROR_SHARING_VIOLATION` | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |

**Único caso de "open exitoso":** ausencia total de handles preexistentes con acceso de datos (sólo handles de metadatos, o ninguno). En ese caso el probe tiene éxito y el flujo continúa a `SetSecurityInfo`.

**Limitación declarada (crítica, no inventada):** el probe es una **instantánea**: detecta todo handle de datos existente al momento del probe, pero NTFS no revoca los derechos concedidos a un handle ya abierto cuando se cambia la DACL, y nada impide que un writer abra el archivo **después** del probe (o después del rerun pre-`ARCHIVING_BACKUP` y antes de `COMMITTED`) y conserve su handle con `GENERIC_WRITE`. La garantía v1 es:
1. **Detección** de todo handle de datos preexistente en el momento del probe (filas H1–H6) y en el rerun pre-`ARCHIVING_BACKUP`.
2. **Negación fail-closed** de la transición a `COMMITTED` si el probe o el rerun detectan writer.
3. **Detección de drift de contenido** vía RV-2 (`TreeDigest`) cuando un writer abrió después del rerun y escribió.
4. **Cero afirmación de exclusividad absoluta** post-`COMMITTED`: un proceso con derechos puede reabrir y escribir. La garantía completa post-`COMMITTED` requiere un módulo de enforcement distinto (no GP2 v1).

`GP2-T23` se reespecifica: el oráculo enumera **literalmente las seis filas H1–H6**, todas con desenlace `ERROR_SHARING_VIOLATION`, más un caso control "sin handles de datos" cuyo desenlace es "open exitoso" y continúa a `SetSecurityInfo`. No se admite parametrización parcial ni omisión de filas.

### 4.1.2 WRITABLE MAPPING CASE (`CreateFileMapping` con `PAGE_READWRITE`)

Este caso es **independiente de la HANDLE SHARE MATRIX** de §4.1.1 y se trata aparte porque un file mapping es una referencia al archivo con semántica propia.

**Semántica de construcción (Win32, verificada contra documentación primaria):** `CreateFileMappingW` con `PAGE_READWRITE` exige que el handle subyacente posea **derechos de acceso** `GENERIC_READ | GENERIC_WRITE`; **no** exige un `dwShareMode` determinado del handle (el share mode sólo condiciona aperturas posteriores por otros handles, no la creación del mapping). Consecuencia normativa:

- **Todo mapping `PAGE_READWRITE` (o superior) representa acceso de datos write sobre el archivo.** El probe del helper (`dwShareMode = 0`) colisiona con él por la regla bidireccional de §4.1.1 (el probe no comparte write) -> `ERROR_SHARING_VIOLATION` -> `REFUSE_TO_APPLY` (0 nodos) / `ROLLBACK_REQUIRED` (>0 nodos). No existe un mapping escribible "no detectado" por el probe.
- Un mapping `PAGE_READONLY` no es una amenaza de mutación (no concede write) y no bloquea el flujo.

**Limitación residual:** como con cualquier handle, un mapping creado **después** del probe (o del rerun pre-`ARCHIVING_BACKUP`) y antes de `COMMITTED` no es detectado; RV-2 (`TreeDigest`) detecta el drift de contenido que produzca. `GP2-T24` cubre ambos casos (ver §25).

### 4.1.3 Política de reintentos acotados del probe (colisión de lectores benignos)

El probe (`dwShareMode = 0`) colisiona con **todo** handle de datos preexistente, incluidos los **lectores benignos** de solo lectura (filas H5/H6: indexador de búsqueda de Windows, antivirus, el shell generando thumbnails/preview). Estos lectores **no** son una amenaza de quiescencia —no pueden mutar el archivo— pero el probe no puede distinguirlos de un writer, porque la colisión ocurre en la dirección 1 (§4.1.1: el lector no concedió `FILE_SHARE_WRITE`). En un árbol de ~16.000 nodos (§27) sobre un escritorio Windows vivo, la probabilidad de que **algún** nodo tenga un handle de lectura transitorio en el instante del probe es alta; un aborto inmediato a `REFUSE_TO_APPLY` haría `protect_golden_master` intermitentemente inviable.

Por eso el desenlace de `ERROR_SHARING_VIOLATION` en el probe **no es aborto inmediato**, sino una **política de reintentos acotados con backoff** (normativa; refina el "primer `ERROR_SHARING_VIOLATION` -> `REFUSE_TO_APPLY`" de versiones previas):

```text
PROBE_RETRY_POLICY (por nodo):
  intentos = 0
  mientras intentos < MAX_PROBE_RETRIES:            // MAX_PROBE_RETRIES configurable (default 5)
      h_probe = QUIESCENCE_PROBE(node)              // §4.1
      si h_probe válido: CloseHandle; nodo quiescente; continuar el flujo normal.
      si GetLastError() == ERROR_SHARING_VIOLATION:
          registrar telemetría de "nodo bloqueante" (relpath, intento, timestamp);
          esperar backoff exponencial (base configurable, p. ej. 50ms * 2^intentos, con jitter);
          intentos += 1; continuar el bucle.
      cualquier otro error: aplicar §12.2 (ACCESS_DENIED / catch-all fail-closed).
  // Reintentos agotados y el nodo sigue colisionando:
  si 0 nodos mutados -> REFUSE_TO_APPLY (QuiescenceViolationError, con la lista de nodos bloqueantes en el reporte)
  si >0 nodos mutados -> ROLLBACK_REQUIRED
```

- **No debilita la seguridad:** el desenlace terminal tras agotar reintentos sigue siendo **fail-closed** (`REFUSE_TO_APPLY`/`ROLLBACK_REQUIRED`); nunca se muta un nodo cuyo probe final no fue limpio. El backoff sólo da tiempo a que un lector transitorio (thumbnail, escaneo AV) cierre su handle.
- **Guía de operación:** el reporte de fallo enumera los nodos bloqueantes y el proceso propietario del handle cuando `RmGetList`/`NtQuerySystemInformation` lo permita (best-effort, sólo diagnóstico), para que el operador cierre la app o excluya el Golden del indexador/AV y reintente.
- **Aplica también al rerun pre-`ARCHIVING_BACKUP`** (§12.2) con el mismo `MAX_PROBE_RETRIES`.

Oráculo: `GP2-T45` verifica que (a) una colisión de lector que se libera dentro de la ventana de reintentos permite completar el probe y proseguir; (b) una colisión persistente agota `MAX_PROBE_RETRIES` y termina en `REFUSE_TO_APPLY` (0 nodos) / `ROLLBACK_REQUIRED` (>0) con la lista de nodos bloqueantes; (c) el número de intentos y el backoff son acotados (no bucle infinito). El mutante `M-Q3` (aborto inmediato sin reintentos, o reintentos no acotados) es detectado por `GP2-T45`.

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
Mitigación: Doble apertura normativa (ver §2.3 y §12.2). Probe handle transitorio con `dwDesiredAccess = FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA | DELETE` (archivos) o equivalente para directorios, `dwShareMode = 0`. Por la semántica **bidireccional** de compartición de `CreateFileW` (§4.1.1), el probe con `dwShareMode = 0` no comparte nada, por lo que colisiona con **todo** handle preexistente que posea acceso de datos —un writer abierto, un `CreateFileMapping` con `PAGE_READWRITE`, o cualquier handle con derecho de read/write/delete de datos— **con independencia del `dwShareMode` que ese handle haya concedido** (incluso full-share `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`, filas H4/H6): `CreateFileW` falla con `ERROR_SHARING_VIOLATION` -> `REFUSE_TO_APPLY` (0 nodos mutados) o `ROLLBACK_REQUIRED` (>0 nodos mutados). Handle de mutación con `READ_CONTROL | WRITE_DAC` (`WRITE_OWNER` añadido para el rollback, ver §12.2) y `dwShareMode = FILE_SHARE_READ`, sin `FILE_SHARE_WRITE`, sin `FILE_SHARE_DELETE`. El handle de mutación no dispara la comprobación de compartición (no solicita clases de datos read/write/delete), por eso NO detecta writers por sí mismo: la detección ocurre exclusivamente en el probe. La matriz de share-semantics figura en §4.1.

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
- GP2 v1 no intenta transferir la propiedad a `TrustedInstaller` ni a `SYSTEM`: **`SeTakeOwnershipPrivilege` nunca se adquiere ni se habilita en v1**.
- **`SeRestorePrivilege` sí puede ser necesario** para atravesar DACLs restrictivas preexistentes (apertura del probe y del handle de mutación sobre un Golden cuyo DACL deniega los derechos solicitados), conforme a la política única de §18. La afirmación "la operación normal no requiere privilegios" queda acotada a: la operación **no requiere** `SeTakeOwnershipPrivilege` ni `SeSecurityPrivilege`; puede requerir `SeRestorePrivilege` sólo en el caso documentado de DACL preexistente restrictiva (ver §18.2).

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
3. **Uso de los bytes en rollback (no releer del filesystem para "verificar" el backup):** el helper, durante el rollback, **decodifica `pre_sd_bytes_b64` y aplica `SetSecurityInfo` sobre esos bytes exactamente**; nunca vuelve a leer el descriptor desde el filesystem para "verificar" contra el backup, porque esa verificación cruzada es la que habilita el ataque de sustitución. Esta regla aplica a la mutación normal; la recuperación ante crash (§20 C4) sí compara bytes del SD actual contra el backup PRE, pero **únicamente como detección de drift de mutación tras revalidar antes `(VolumeSerialNumber, FileId)`** — y nunca como verificación del backup.
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

### 10.1 Detección y Política de Hardlinks (internos y externos)
En NTFS, un mismo archivo físico (identificado unívocamente por su tupla `(VolumeSerialNumber, FileId)`) puede poseer múltiples nombres de enlace (hardlinks). Dos clases de riesgo:
- **Hardlinks internos (dos rutas dentro del subárbol con el mismo `FileId`):** mutar la primera ruta modificaría inmediatamente el SD de la segunda, haciendo que su comparación PRE falle y corrompa la evidencia transaccional.
- **Hardlinks que escapan del subárbol (un archivo dentro del Golden con un segundo enlace fuera del root):** el chequeo interno de `FileId` duplicado no lo detecta (el manifest sólo ve una ruta), pero `SetSecurityInfo` a través del nombre interno muta el **mismo objeto de seguridad** que el enlace externo, convirtiendo al helper elevado en un bypass de confinamiento del root (un Actor D podría forzar la modificación de SD de archivos fuera del Golden).

**Política GP2 v1:** si se detecta cualquier `(VolumeSerialNumber, FileId)` duplicado dentro del subárbol del Golden, o cualquier archivo regular cuyo `FileStandardInfo.NumberOfLinks > 1` (indica un hardlink externo), la fase de planificación aborta de inmediato con excepción `DuplicateFileIdError` y estado **`REFUSE_TO_APPLY`** antes de realizar ninguna mutación física.

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

1. **Trusted Golden Registry (TGR):** `%ProgramData%\Sky-Claw\runtime_vault\trusted_goldens.json` (ruta normativa, hardcodeada en el helper). El helper compara `canonical_root`, `VolumeSerialNumber`, `root_file_id` **y `tree_digest`** del plan contra una entrada registrada. Cada entrada del TGR incluye el `TreeDigest` de la **última verificación RV-2 aprobada** de ese Golden (`{canonical_root, VolumeSerialNumber, root_file_id, tree_digest, policy_version, registered_by, registered_at}`); el registro se actualiza (sólo por componente privilegiado/helper elevado) cuando una nueva verificación RV-2 aprueba un digest, mediante el flujo de refresco normativo de **§11.4** (operación privilegiada independiente de GP2 apply). Esto impide que Actor D modifique un Golden registrado pero `UNPROTECTED` antes del staging y consiga que el helper commute el contenido alterado: si `plan.TreeDigest != tgr_entry.tree_digest` -> `REFUSE_TO_PLAN`.
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
| `staging/` (padre, sin `<op_id>`) | Componente privilegiado (instalador) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_LIST_DIRECTORY \| FILE_TRAVERSE \| FILE_ADD_SUBDIRECTORY` — **crear su propia operación `<op_id>` SÍ; NO concede `FILE_ADD_FILE` sobre staging/ mismo, NO concede `FILE_WRITE_DATA` sobre staging/ mismo, y NO concede ningún derecho sobre `operations/`, `locks/`, `golden_backups/` ni `trusted_goldens.json`** | Contenedor de operaciones de staging. Un usuario no elevado puede crear `staging\<op_id>\` (y limpiarlo tras la operación, ver nota de teardown) pero no puede crear archivos sueltos en el padre ni alcanzar ningún store autoritativo a través de esta DACL. |
| `staging/<op_id>/` (`UNTRUSTED_STAGING`) | Coordinador (usuario interactivo) | `FILE_ALL_ACCESS` (`0x001F01FF`) | **Escritura acotada al `CREATOR OWNER` de la operación, NO a todo `Authenticated Users`:** la ACE de `<op_id>` concede `FILE_GENERIC_READ \| FILE_GENERIC_WRITE \| FILE_EXECUTE` **sólo al `CREATOR OWNER`** (el coordinador que creó el directorio); `Authenticated Users` como grupo obtiene a lo sumo `FILE_GENERIC_READ` sobre operaciones ajenas. Así ningún otro usuario no elevado puede sobrescribir ni plantar el `candidate_manifest.json` de una operación ajena (evita DoS de digest y la sustitución previa al hasheo). | Almacenamiento temporal del candidate manifest generado por el proceso no elevado. El coordinador es `CREATOR OWNER` de su `<op_id>` y puede escribir/borrar dentro de su propia operación; otro usuario no elevado no. **Nota de teardown (semántica única, sin borrado cruzado):** la limpieza de `staging\<op_id>\` la realiza su creador mediante `DELETE`/`FILE_DELETE_CHILD` concedidos **por la ACE `CREATOR OWNER` heredable acotada al propio subárbol `<op_id>` del creador** — NO mediante `FILE_DELETE_CHILD` sobre el padre `staging/` (el padre `staging/` **no** concede `FILE_DELETE_CHILD` a `Authenticated Users`). Por tanto un usuario no elevado **NO** puede borrar ni el `<op_id>` ni el `candidate_manifest.json` de otro usuario: no hay borrado cruzado ni el DoS local asociado. Esto tampoco afecta a `operations/`, `locks/`, `golden_backups/` ni `trusted_goldens.json`, cuyos directorios padre deniegan `FILE_DELETE_CHILD` a Authenticated Users. |
| `operations/<op_id>/` (`AUTHORIZED_OPERATIONS`) | Helper Elevado | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ` (`0x001200A9`) — **SIN DERECHO DE ESCRITURA NI BORRADO** | Almacén autoritativo del plan sellado `authorized_plan.json` y el journal de ejecución FSM. |
| `golden_backups/<vol_root>/` (`AUTHORIZED_BACKUPS`) | Helper Elevado | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ` (`0x001200A9`) — **SIN DERECHO DE ESCRITURA NI BORRADO** | Respaldo durable de seguridad PRE para rollback forense y futura capability GP3. |
| `locks/` (`LOCKS_STORE`) | Helper Elevado | `FILE_ALL_ACCESS` (`0x001F01FF`) | **SIN NINGÚN DERECHO DE LECTURA NI ESCRITURA sobre los archivos de lock**: `FILE_LIST_DIRECTORY \| FILE_TRAVERSE` sobre el directorio, sin `FILE_READ_DATA` sobre los archivos (un handle de lectura retenido por un proceso no elevado causaría `ERROR_SHARING_VIOLATION` permanente al helper, que abre con `dwShareMode = 0` — DoS) | Contenedor protegido para archivos de lock del kernel (GoldenMutationLock). |
| `trusted_goldens.json` (`TRUSTED_GOLDEN_REGISTRY`) | Componente privilegiado (instalador) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ` (`0x00120089`) — **READ ONLY: SIN CREATE, SIN WRITE, SIN DELETE, SIN WRITE_DAC, SIN WRITE_OWNER** | Trust root del helper elevado. Contiene entradas `{canonical_root, VolumeSerialNumber, root_file_id, tree_digest, policy_version, registered_by, registered_at}` (el `tree_digest` es el de la última RV-2 aprobada). Un proceso no elevado no puede añadir, reemplazar, borrar, ni siquiera reparse-sustituir el archivo. |
| `runtime_vault/` (raíz del namespace) | Componente privilegiado (instalador) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ \| FILE_TRAVERSE` (`0x001200A9`) — **SIN `FILE_ADD_FILE`, SIN `FILE_ADD_SUBDIRECTORY`, SIN `FILE_DELETE_CHILD`, SIN `DELETE`, SIN `WRITE_DAC`, SIN `WRITE_OWNER`** | Directorio raíz del namespace; un proceso no elevado no puede añadir ni borrar entradas que cambien la composición del árbol (la ruta `Sky-Claw\runtime_vault\` y sus ramas hijas no se pueden renombrar, mover ni eliminar por usuarios no elevados). |
| `Sky-Claw/` (padre del namespace, bajo `%ProgramData%`) | Componente privilegiado (instalador) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_TRAVERSE` únicamente — **SIN `FILE_LIST_DIRECTORY`, SIN `FILE_ADD_SUBDIRECTORY`, SIN `FILE_ADD_FILE`, SIN `FILE_DELETE_CHILD`, SIN `DELETE`, SIN `WRITE_DAC`, SIN `WRITE_OWNER`** | Directorio padre del trust root: protege todo el namespace contra borrado/recreación a través del parent (un `FILE_DELETE_CHILD` sobre `Sky-Claw\` permitiría a Actor D borrar y recrear `runtime_vault\` completo, incluido un TGR forjado). El componente privilegiado crea `Sky-Claw\` con esta DACL propia (no heredada de `%ProgramData%`). |
| `operations/` (intermedio, sin `<op_id>`) | Componente privilegiado (instalador) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ \| FILE_TRAVERSE` — **SIN `FILE_ADD_FILE`, SIN `FILE_ADD_SUBDIRECTORY`, SIN `FILE_DELETE_CHILD`, SIN `DELETE`, SIN `WRITE_DAC`, SIN `WRITE_OWNER`** | Sólo el helper elevado puede crear `operations\<op_id>\`. Un proceso no elevado que intente borrar `operations\X\authorized_plan.json` a través del parent `operations\` recibe `ACCESS_DENIED` por la DACL del directorio padre. |
| `golden_backups/` (intermedio, sin `<vol_root>`) | Componente privilegiado (instalador) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ \| FILE_TRAVERSE` — **SIN `FILE_ADD_FILE`, SIN `FILE_ADD_SUBDIRECTORY`, SIN `FILE_DELETE_CHILD`, SIN `DELETE`, SIN `WRITE_DAC`, SIN `WRITE_OWNER`** | Sólo el helper elevado puede crear `golden_backups\<vol_serial>_<root_file_id>\`. |
| `locks/` (intermedio) | Componente privilegiado (instalador) | `FILE_ALL_ACCESS` (`0x001F01FF`) | `FILE_GENERIC_READ \| FILE_TRAVERSE` — **SIN `FILE_ADD_FILE`, SIN `FILE_ADD_SUBDIRECTORY`, SIN `FILE_DELETE_CHILD`, SIN `DELETE`, SIN `WRITE_DAC`, SIN `WRITE_OWNER`** | Sólo el helper elevado puede crear `locks\<lock_key>.lock`. |

### 11.4 Refresco del TGR (flujo normativo de actualización del `tree_digest`)

El binding `plan.TreeDigest == tgr_entry.tree_digest` (§11.0 punto 1) plantea un problema de ciclo de vida: cuando un Golden cambia **legítimamente** (nuevo build, mod verificado) su `TreeDigest` avanza, pero la verificación RV-2 que produce el digest nuevo corre bajo **token no elevado** y el TGR sólo es escribible por `Administrators`/`SYSTEM`. Sin un flujo de refresco, la entrada del TGR quedaría obsoleta y **toda** operación GP2 legítima posterior sería rechazada con `REFUSE_TO_PLAN`. El refresco se define como una operación privilegiada **independiente de GP2 apply**:

1. **Separación de autoridad:** GP2 apply **nunca** escribe el TGR — sólo lo lee y hace `REFUSE_TO_PLAN` ante mismatch. El registro/refresco es una operación distinta (`op_kind = REGISTER_OR_REFRESH_TRUSTED_GOLDEN`), con su propio consentimiento privilegiado. Esto rompe el deadlock (un Golden actualizado nunca podría protegerse) sin diluir la defensa Confused Deputy.
2. **Staging del pedido (no elevado):** el coordinador stagea en `UNTRUSTED_STAGING` un pedido de refresco con `canonical_root`, `(VolumeSerialNumber, root_file_id)`, el `tree_digest` nuevo y la evidencia RV-2 (`GoldenMasterVerificationResult`, `reference_only`). Estos bytes son UNTRUSTED (Actor D puede forjarlos); no se confía en ellos por sí mismos.
3. **Revalidación y confirmación (elevado):** el helper elevado, tras UAC, **re-ejecuta RV-2 bajo el token del operador** sobre el `canonical_root` físico (no confía en el digest staged) y exige `state == VERIFIED`. Luego exhibe una **confirmación privilegiada** —equivalente a la PPSC— con `canonical_root`, `(VolumeSerialNumber, root_file_id)`, `tree_digest_anterior`, `tree_digest_nuevo`, `policy_version` y resumen de evidencia RV-2, y requiere consentimiento explícito del operador (el humano autoriza que el contenido nuevo es legítimo). Sólo entonces el helper escribe/actualiza la entrada del TGR de forma **atómica** (temporal + `os.replace` en el mismo directorio, DACL `Administrators`/`SYSTEM` únicamente), fijando `registered_by` y `registered_at`.
4. **Primera registración:** una alta inicial (sin entrada previa) usa exactamente el mismo flujo; `ARBITRARY_ROOT_FROM_STAGING_ACCEPTED = NO` se mantiene porque el `canonical_root` lo confirma el operador bajo sesión elevada.
5. **Anti-Confused-Deputy:** un proceso no elevado no puede refrescar el TGR (DACL de escritura exclusiva de `Administrators`/`SYSTEM`) ni saltarse la re-ejecución RV-2 ni la confirmación privilegiada. Un digest staged forjado es descartado por la revalidación RV-2 del paso 3.

Oráculo: `GP2-T44` cubre (a) que GP2 apply nunca escribe el TGR; (b) que el refresco exige re-ejecución RV-2 `VERIFIED` + confirmación privilegiada antes de mutar la entrada; (c) que un pedido de refresco de un proceso no elevado sobre `trusted_goldens.json` recibe `ACCESS_DENIED` por DACL. El mutante `M-T2` (refresco que confía en el digest staged sin re-ejecutar RV-2) es detectado por `GP2-T44`.

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
    3. Validar pertenencia de la ruta contra TGR en %ProgramData%\Sky-Claw\runtime_vault\trusted_goldens.json, comparando **los cuatro** campos normativos de §11.0: canonical_root + VolumeSerialNumber + root_file_id **y** plan.TreeDigest == tgr_entry.tree_digest. Cualquier mismatch -> REFUSE_TO_PLAN (no se crea authorized_plan.json). La comparación de tree_digest es la defensa automática contra Actor D que altera el contenido de un Golden registrado en estado UNPROTECTED (§11.0/§11.4); NO puede omitirse aquí ni delegarse sólo a la PPSC (que la muestra al operador pero no la exige).
    4. Confirmación Privilegiada de Intención (PPSC) en la sesión UAC: el diálogo elevado debe exhibir exactamente operation_id, canonical_root, VolumeSerialNumber, root_file_id, TreeDigest, node_count, policy_version, y obtener consentimiento explícito del operador. Sin confirmación -> ELEVATION_REJECTED / REFUSE_TO_PLAN. Se registra durablemente en el journal que PPSC pasó.
    5. **Validación exhaustiva de precondiciones criptográficas y estructurales del plan** (se ejecuta **una vez** sobre el `candidate_manifest.json` completo, antes de adquirir el `GoldenMutationLock` y antes de cualquier mutación). Si cualquier nodo del plan no satisface los invariantes de abajo -> `REFUSE_TO_PLAN` (sin `GoldenMutationLock` adquirido, sin nodos mutados, sin `authorized_plan.json` creado):
        - Para cada nodo K: `len(base64_decode(node.pre_sd_bytes_b64)) >= SECURITY_DESCRIPTOR_MIN_LENGTH` y `IsValidSecurityDescriptor(base64_decode(node.pre_sd_bytes_b64)) == True` (los bytes del manifest son controlados por Actor D: un hash auto-consistente no garantiza un descriptor estructuralmente válido; parsear con accessors nativos sin esta validación es UB/fallo de seguridad).
        - Para cada nodo K: `sha256(base64_decode(node.pre_sd_bytes_b64)) == node.pre_sd_sha256` (rechaza `pre_sd_bytes_b64` adversarios) y `node.pre_sd_length == GetSecurityDescriptorLength(base64_decode(node.pre_sd_bytes_b64))`.
        - **Para cada nodo K, contra el descriptor VIVO (cierra la ventana de manifest auto-consistente forjado):** abrir `node_path` con `READ_CONTROL | FILE_READ_ATTRIBUTES` (handle de lectura transitorio, `dwShareMode = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`, **`dwFlagsAndAttributes = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`** — `FILE_FLAG_BACKUP_SEMANTICS` es obligatorio para abrir **directorios** con `CreateFileW`, y el plan siempre contiene directorios; `FILE_FLAG_OPEN_REPARSE_POINT` evita seguir un reparse en esta validación fail-closed). Si esta apertura falla con `ERROR_ACCESS_DENIED` (la DACL preexistente de un Golden endurecido/personalizado puede no conceder `READ_CONTROL` a Administrators) -> `REFUSE_TO_PLAN` **incondicional** (fail-closed, cero nodos mutados): NO se extiende aquí la excepción de privilegios de §18.2 —que cubre sólo el probe y el handle de mutación— porque leer el SD bajo denegación exigiría `SeBackupPrivilege`, prohibido por §18.2 (`SE_BACKUP_REQUIRED_WHEN = NEVER`); un Golden cuyo descriptor vivo ni siquiera puede leerse antes de mutar se rehúsa. Con handle válido: verificar `GetFileInformationByHandleEx(FileAttributeTagInfo).ReparseTag == 0` (si != 0 -> `REFUSE_TO_PLAN`), leer `GetSecurityInfo(handle, OWNER | GROUP | DACL_SECURITY_INFORMATION)` y exigir `sha256(sd_vivo) == node.pre_sd_sha256`. Si difiere -> `REFUSE_TO_PLAN` (el descriptor vivo no coincide con el plan; **antes** de mutar nada). Cerrar el handle.
        - Unicidad de `(VolumeSerialNumber, FileId)` en el conjunto de nodos, **y ausencia de hardlinks que escapen del subárbol**: para cada archivo regular, `FileStandardInfo.NumberOfLinks == 1` (un segundo hardlink fuera del Golden haría que `SetSecurityInfo` a través del nombre interno mutara también el archivo externo — bypass de confinamiento del root).
        - Consistencia de la jerarquía: cada relpath es resoluble bajo `canonical_root` y no escapa de él.
       Esta validación se realiza antes de la mutación para que un plan adversario sea rechazado con cero mutaciones; los chequeos revalidación per-nodo en el bucle de mutación (§12.2 paso 3) son **revalidation**, no primera validación.
    6. Adquirir GoldenMutationLock exclusivo (CreateFileW sobre %ProgramData%\Sky-Claw\runtime_vault\locks\skyclaw_golden_lock_<vol_serial>_<root_file_id>.lock con dwDesiredAccess = GENERIC_READ | GENERIC_WRITE, dwShareMode = 0; ver §19.1).
    7. Crear authorized_plan.json en AUTHORIZED_OPERATIONS (único productor). El binding incluye operation_id, canonical_root, VolumeSerialNumber, root_file_id, TreeDigest, node_count, policy_version, staging_digest y el timestamp PPSC.

Fase de Mutación por Nodo K en el Plan (en orden Bottom-Up):
    1. Probe Handle de Quiescencia (transitorio):
       // Política de privilegios (única, ver §18.2): primer intento SIN privilegios (least privilege).
       // Si el DACL preexistente deniega las clases solicitadas -> ERROR_ACCESS_DENIED -> se habilita
       // SOLO SeRestorePrivilege (el necesario para aperturas write/delete con FILE_FLAG_BACKUP_SEMANTICS;
       // SeBackupPrivilege NO se habilita: el probe no solicita READ_DATA) -> retry UNA vez -> si persiste
       // ACCESS_DENIED -> bifurcar por nodos mutados (0 -> REFUSE_TO_APPLY; >0 -> ROLLBACK_REQUIRED).
       h_probe = CreateFileW(
           node_path,
           PROBE_DESIRED_ACCESS,           // normativo en §4.1
           /* dwShareMode */ 0,            // CERO
           NULL,
           OPEN_EXISTING,
           FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
           NULL
       )
       Si h_probe == INVALID_HANDLE_VALUE:
           err = GetLastError();
           Si err == ERROR_SHARING_VIOLATION:
               // Aplicar PROBE_RETRY_POLICY (§4.1.3): reintentos acotados con backoff para tolerar
               // lectores benignos transitorios (indexador/AV/shell) antes de decidir. Sólo tras
               // agotar MAX_PROBE_RETRIES sin quiescencia se bifurca:
               Si 0 nodos mutados -> FAIL_CLOSED -> REFUSE_TO_APPLY (QuiescenceViolationError, con lista de nodos bloqueantes)
               Si >0 nodos mutados -> FAIL_CLOSED -> Iniciar secuencia de ROLLBACK (ROLLBACK_REQUIRED).
           Si err == ERROR_ACCESS_DENIED:
               // El DACL del nodo niega las clases solicitadas por el probe a Administrators/helper.
               // Esto NO es quiescencia: indica que el helper no tiene los derechos para
               // ni siquiera detectar writers. Aplicar §18.2: habilitar SeRestorePrivilege y
               // reintentar una vez (SeBackupPrivilege NO aplica: el probe no pide READ_DATA).
               Habilitar SeRestorePrivilege (validar ERROR_NOT_ALL_ASSIGNED);
               h_probe = CreateFileW(node_path, PROBE_DESIRED_ACCESS, 0, NULL, OPEN_EXISTING,
                                     FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
               Deshabilitar SeRestorePrivilege en finally (restaurar PreviousState);
               Si h_probe == INVALID_HANDLE_VALUE:
                   Si 0 nodos mutados -> FAIL_CLOSED -> REFUSE_TO_APPLY (ProbeAccessDeniedError)
                   Si >0 nodos mutados -> FAIL_CLOSED -> Iniciar secuencia de ROLLBACK (ROLLBACK_REQUIRED).
           Cualquier otro err -> FAIL_CLOSED -> Si 0 nodos mutados: REFUSE_TO_APPLY; Si >0 nodos mutados: ROLLBACK_REQUIRED.
       CloseHandle(h_probe).

    2. Apertura del Handle de Mutación (binding por handle) y Validación de Identidad:
       h = CreateFileW(
           node_path,
           READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES,   // WRITE_OWNER: el rollback restaura OWNER/GROUP PRE (ver §12.2 rollback). FILE_READ_ATTRIBUTES: requerido por GetFileInformationByHandleEx (FileIdInfo/FileAttributeTagInfo/FileStandardInfo) usado en la revalidación de identidad/hardlink; es clase de metadatos y NO participa en la compartición (§4.1.1), así que no altera la matriz de share.
           FILE_SHARE_READ,                // sin FILE_SHARE_WRITE, sin FILE_SHARE_DELETE
           NULL,
           OPEN_EXISTING,
           FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
           NULL
       )
        Si h == INVALID_HANDLE_VALUE y GetLastError() == ERROR_ACCESS_DENIED:
            Habilitar SeRestorePrivilege en el scope del bloque (justo antes de esta apertura, no después), reintentar una vez; si persiste ACCESS_DENIED:
                Si 0 nodos mutados -> FAIL_CLOSED -> REFUSE_TO_APPLY (ProbeAccessDeniedError)
                Si >0 nodos mutados -> FAIL_CLOSED -> Iniciar secuencia de ROLLBACK (ROLLBACK_REQUIRED).
            (No se asume que WRITE_DAC está concedido por la DACL existente; no se usa SeTakeOwnershipPrivilege en v1.)
       Si h == INVALID_HANDLE_VALUE por cualquier OTRO GetLastError() (catch-all fail-closed, simétrico al paso 1 del probe):
            // ERROR_SHARING_VIOLATION NO puede originarse en esta apertura: el handle de mutación
            // solicita sólo READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES (todas
            // clases de metadatos/control), ninguna clase de datos
            // read/write/delete, y `IoCheckShareAccess` sólo evalúa compartición cuando la
            // apertura pide FILE_READ_DATA/FILE_EXECUTE, FILE_WRITE_DATA/FILE_APPEND_DATA o DELETE
            // (ver §4.1.1 y §29). Aun así, todo GetLastError() distinto se bifurca de forma determinista:
            Si 0 nodos mutados -> FAIL_CLOSED -> REFUSE_TO_APPLY
            Si >0 nodos mutados -> FAIL_CLOSED -> Iniciar secuencia de ROLLBACK (ROLLBACK_REQUIRED).
       // A partir de aquí h es un handle válido:
       info  = GetFileInformationByHandleEx(h, FileIdInfo)
       tag   = GetFileInformationByHandleEx(h, FileAttributeTagInfo).ReparseTag
       links = GetFileInformationByHandleEx(h, FileStandardInfo).NumberOfLinks   // revalidación anti-hardlink VIVA
       Si info.VolumeSerialNumber != expected.vol_serial o info.FileId != expected.file_id o tag != 0:
           CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK.
       Si node_kind == "file" y links != 1:
           // TOCTOU de hardlink externo: un Actor D creó un segundo enlace hacia este objeto
           // ENTRE el sellado (§10.1, que sólo mira el manifiesto) y esta apertura. `FileId`, SD y
           // reparse_tag no cambian al añadir un hardlink, así que sólo `NumberOfLinks` lo detecta.
           // Aplicar la Target DACL mutaría también el enlace externo (bypass de confinamiento del root).
           CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK (DuplicateFileIdError, hardlink externo detectado en runtime).

    3. Leer SD PRE sobre el Handle y **Revalidar** Binding pre_sd_bytes_b64 == pre_sd_sha256:
       sd_pre = GetSecurityInfo(h, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, ...)
       Si sha256(sd_pre) != expected.pre_sd_sha256:   // drift entre captura y ejecución
           CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK (no `REFUSE_TO_PLAN` aquí: ya estamos en el bucle de mutación; este chequeo es la revalidación per-nodo; la primera validación contra el descriptor vivo ocurrió en §12.2 paso 5).
       Si sha256(base64_decode(expected.pre_sd_bytes_b64)) != expected.pre_sd_sha256:
           CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK (defensa en profundidad; la primera validación ocurrió en la fase de autorización §12.2 paso 5).
       Capture pre_sd_protected_flag = GetSecurityDescriptorControl(sd_pre) & SE_DACL_PROTECTED para usarlo en rollback (ver §13).

    4. Write-Ahead Log (WAL) con FlushFileBuffers Obligatorio:
       Registrar durablemente en el journal de AUTHORIZED_OPERATIONS: MUTATING(K, pre_sd_sha256, pre_sd_protected_flag).
       Invocar FlushFileBuffers(journal_handle) para garantizar persistencia física previa a la mutación.

    5. Aplicar Target DACL sobre el MISMO Handle:
       // Revalidación anti-hardlink INMEDIATA (cierra la ventana TOCTOU entre el paso 2 y el paso 5:
       // lectura de SD PRE, validación de hashes y WAL+FlushFileBuffers ocurren en medio; un Actor D
       // podría añadir un hardlink externo en ese intervalo). Se relee sobre el MISMO handle:
       links_now = GetFileInformationByHandleEx(h, FileStandardInfo).NumberOfLinks
       Si node_kind == "file" y links_now != 1:
           CloseHandle(h) -> FAIL_CLOSED -> Iniciar ROLLBACK (DuplicateFileIdError, hardlink externo en ventana pre-mutación).
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

Rerun del probe + RV-2 final antes de ARCHIVING_BACKUP: una vez aplicadas todas las mutaciones y antes de transicionar a ARCHIVING_BACKUP, el helper (a) reejecuta el Paso 1 (probe) sobre cada nodo y (b) ejecuta una **segunda verificación RV-2 (`TreeDigest`) final**. Si el probe falla para cualquier nodo, o si el `TreeDigest` final difiere del baseline -> transición a ROLLBACK_REQUIRED (no se archiva ni se promueve a COMMITTED).

**Por qué la RV-2 final es imprescindible (cierra la contradicción de orden del FSM):** la primera pasada RV-2 (`VERIFYING_RV2`, §19.2) ocurre **antes** de este rerun. Un writer que abrió y escribió un byte **después** del rerun del probe dejaría un drift de contenido que la primera RV-2 ya no puede medir; sin esta segunda RV-2 posterior al rerun, `GP2-T24` Parte 2 y `M-W1` (que afirman que RV-2 detecta ese drift y aborta antes de `COMMITTED`) no se sostendrían contra el FSM. Con la RV-2 final, la cadena queda: `VERIFYING_NODE_SET -> [probe rerun] -> [RV-2 final] -> ARCHIVING_BACKUP -> COMMITTED`. La ventana residual entre la RV-2 final y `COMMITTED` NO se afirma cerrada (§4.1.1, limitación: sin exclusividad absoluta post-`COMMITTED`); lo que ahora sí es consistente es que cualquier drift **anterior** a `ARCHIVING_BACKUP` es detectado. Esto cubre el caso de un writer que abrió durante la fase de mutación o inmediatamente tras el rerun.

Rollback por nodo (Top-Down, ver §17): el helper usa el `pre_sd_bytes_b64` validado en el paso 3 (no vuelve a leer del filesystem) y reconstruye los componentes del `SECURITY_DESCRIPTOR` self-relative antes de invocar `SetSecurityInfo` (que **no** acepta un buffer SD completo, sino punteros separados a owner/group/dacl):

```text
decoded_sd = Base64Decode(pre_sd_bytes_b64)
// validar: sha256(decoded_sd) == pre_sd_sha256 y GetSecurityDescriptorLength(decoded_sd) == pre_sd_length
GetSecurityDescriptorOwner(decoded_sd, &pre_owner, NULL)
GetSecurityDescriptorGroup(decoded_sd, &pre_group, NULL)
GetSecurityDescriptorDacl(decoded_sd, &dacl_present, &pre_dacl, &dacl_defaulted)
GetSecurityDescriptorControl(decoded_sd, &control_bits, NULL)
pre_dacl_protected = (control_bits & SE_DACL_PROTECTED) != 0

err = SetSecurityInfo(
    handle,
    SE_FILE_OBJECT,
    OWNER_SECURITY_INFORMATION
      | GROUP_SECURITY_INFORMATION
      | DACL_SECURITY_INFORMATION
      | protection_flag,                 // PROTECTED_DACL_ o UNPROTECTED_DACL_ según pre_dacl_protected
    pre_owner,
    pre_group,
    pre_dacl,
    NULL                                 // SACL: NO se toca (SACL_SECURITY_INFORMATION nunca se pasa)
)
```

donde:

```text
protection_flag =
    PROTECTED_DACL_SECURITY_INFORMATION   si pre_dacl_protected == True (el PRE estaba protegido)
    UNPROTECTED_DACL_SECURITY_INFORMATION si pre_dacl_protected == False (el PRE heredaba)
```

Tras la restauración, el helper **re-lee** con `GetSecurityInfo(handle, OWNER | GROUP | DACL_SECURITY_INFORMATION)` y verifica exactamente: `owner == pre_owner`, `group == pre_group`, `dacl == pre_dacl` (comparación semántica de ACEs y orden canónico), y el bit `SE_DACL_PROTECTED` del descriptor resultante es exactamente `pre_dacl_protected`. Si cualquiera difiere -> `RollbackFailureError` -> `ROLLBACK_FAILED` / `INDETERMINATE`. El binding por `(VolumeSerialNumber, FileId)` se revalida en cada nodo antes de la restauración; si difiere -> `INDETERMINATE`.
```

### 12.3 Canal IPC de Post-Verificación (normativo, anti-suplantación del verificador)

La decisión de `COMMITTED` depende del resultado (`HARDENED`/`VERIFIED`/NodeSet OK) que el verificador **no elevado** devuelve al helper elevado. Como esa decisión es crítica, el canal IPC y la autenticación del peer se especifican de forma **normativa** para impedir que un Actor D local suplante al verificador y responda un falso `HARDENED`/`VERIFIED` (lo que provocaría `COMMITTED` sobre un Golden no endurecido o alterado):

1. **El helper LANZA al verificador como proceso hijo directo** bajo el **token efectivo NO elevado del operador** (obtenido según §13.4), con el path de imagen **fijo** del binario verificador empaquetado (mismo binario firmado que el helper), nunca una ruta recibida por parámetro. **API de lanzamiento según el privilegio del helper (normativo):** un helper **UAC-elevado interactivo** (Administrators, integridad alta) posee `SeImpersonatePrivilege` pero **no** `SeAssignPrimaryTokenPrivilege`, por lo que **debe** usar **`CreateProcessWithTokenW`** (que sólo exige `SeImpersonatePrivilege`); `CreateProcessAsUserW` fallaría con `ERROR_PRIVILEGE_NOT_HELD`. Un helper que corra como **SYSTEM/servicio** (con `SeAssignPrimaryTokenPrivilege`, ver §11.3) usa `CreateProcessAsUserW`. **Queda prohibido `CreateProcessW` sin token**: correría el verificador bajo el token ELEVADO del helper e invalidaría `assurance_scope == CURRENT_EFFECTIVE_UNELEVATED_TOKEN`. No existe un servicio verificador de larga vida al que "conectarse".
2. **Canal, acoplado a la API de lanzamiento:**
   - **Con `CreateProcessAsUserW` (helper SYSTEM/servicio):** **pipe anónimo con handles heredados** (`CreatePipe` + `bInheritHandle = TRUE`, pasados al hijo vía `STARTUPINFO`/handle inheritance). Un pipe **anónimo no tiene nombre**, por lo que **ningún** proceso externo (Actor D) puede abrirlo: sólo el hijo lanzado por el helper posee el extremo heredado.
   - **Con `CreateProcessWithTokenW` (helper UAC-elevado):** esta API **no propaga la herencia de handles**, así que el pipe anónimo heredado no está disponible; se usa un **named pipe con nombre aleatorio por operación (CSPRNG) y DACL restrictiva** que admite exclusivamente a `SYSTEM`/`Administrators` y al SID del operador (el del token del hijo), y el helper valida al cliente con `GetNamedPipeClientProcessId` + la verificación de peer de abajo. Un nombre adivinable o una DACL permisiva quedan **prohibidos**.
   En ambos casos el resultado sólo se acepta por ese canal privado; no hay endpoint público que Actor D pueda usar.
3. **Autenticación del peer (defensa en profundidad):** el helper conserva el `PID` y el `ProcessCreationTime` devueltos por `CreateProcess`, y antes de aceptar el resultado verifica que el extremo del pipe corresponde a ese hijo (imagen firmada esperada, `PID` + `ProcessCreationTime` estables — misma defensa PID-reuse que §19.1.5). Además el helper genera un **nonce por operación** (CSPRNG), lo pasa al hijo al lanzarlo y exige que el resultado lo devuelva; un payload sin el nonce vigente se descarta (anti-replay).
4. **Fail-closed ante ausencia/ambigüedad:** si el hijo muere, el pipe cierra (EOF), el nonce no coincide, el `PID`/`ProcessCreationTime` no casan, o el resultado es ilegible -> el helper trata la post-verificación como **fallida** -> `ROLLBACK_REQUIRED`. **Nunca** se transiciona a `COMMITTED` con un resultado ausente, tardío o no autenticado.
5. **Read-only:** el verificador sólo lee (GP1/RV-2/NodeSet) y devuelve un veredicto; no escribe el FSM, `AUTHORIZED_OPERATIONS`, `COMMITTED` ni `ARCHIVING_BACKUP` (§19.2). El helper retiene el `GoldenMutationLock` durante toda la post-verificación.

Oráculo: `GP2-T46` verifica que (a) el helper sólo acepta el resultado por el **canal privado** del hijo que lanzó —pipe anónimo heredado con `CreateProcessAsUserW`, o named pipe de nombre aleatorio con DACL restrictiva con `CreateProcessWithTokenW` (punto 2)— con imagen esperada, `PID`+`ProcessCreationTime` y nonce vigente; (b) un proceso ajeno que intente entregar `HARDENED`/`VERIFIED` por cualquier otro canal (o con nonce inválido/reutilizado) es ignorado y **no** produce `COMMITTED`; (c) muerte del verificador o resultado ausente -> `ROLLBACK_REQUIRED`, nunca `COMMITTED`. El mutante `M-I1` (aceptar el veredicto sin autenticar el peer, o por un named pipe con DACL permisiva/nombre adivinable) es detectado por `GP2-T46`.

---

## 13. UAC, Human-in-the-Loop (HITL) & Superficies Hermanas

1. **Consentimiento del SO Exclusivo:** La elevación del helper se solicita **únicamente** a través del mecanismo nativo de UAC de Windows (`ShellExecuteExW` con verbo `"runas"`). **UAC no es autorización del plan:** el plan se autoriza por TGR + PPSC + PAB (ver §11.0). Si el usuario cancela el UAC, el helper no se eleva y `authorized_plan.json` no se crea.
2. **Alineación con el Ciclo de Vida HITL:** En concordancia con el guardrail fail-closed del repositorio (`sky_claw/app/security/hitl.py`), toda solicitud HITL previa a la elevación se valida de forma no reentrable y con resolución terminal única. La PPSC del helper elevado es un diálogo adicional y distinto: HITL pre-elevación sobre la *operación*; PPSC post-elevación sobre el *plan concreto* (valores exactos del §11.0).
3. **Simetría en las Dos Superficies (AGENTS.md):**
   - **Superficie GUI:** `SupervisorAgent` -> `tool_dispatcher["protect_golden_master"]` (con UAC / HITL bridge integrado).
   - **Superficie Agente LLM:** `AsyncToolRegistry` -> `LLMRouter["protect_golden_master"]` (con UAC / HITL bridge integrado).
   - Ambas superficies consumen el mismo servicio de backend (`sky_claw/local/runtime_vault/protection_service.py`), garantizando que ninguna superficie pueda ejecutar la mutación eludiendo el preflight, el lock cross-process o la elevación. El ancla AST `GP2-T27` enumera por introspección/igualdad literal ambas superficies y el set de herramientas mutadoras con su requerimiento de elevación.
4. **Soporte Pleno de Elevación Over-The-Shoulder (OTS):** Al residir el staging, el store autorizado y el TGR en `%ProgramData%`, el helper elevado puede leer el plan candidate, el TGR y escribir el plan autoritativo independientemente de si la cuenta administrativa ingresada en el diálogo UAC coincide con el usuario interactivo o es un administrador secundario de la máquina.
   - **Obtención del token del operador original (normativo, cierra el hueco OTS):** la post-verificación (§12.3/§21/§22) y la re-ejecución RV-2 del refresco del TGR (§11.4) deben correr bajo el **token efectivo no elevado del operador interactivo original** (para que `state == HARDENED` valga bajo `CURRENT_EFFECTIVE_UNELEVATED_TOKEN`), que en OTS **no** es la cuenta del diálogo UAC. Un proceso elevado sólo por UAC **no** posee ese token automáticamente. El diseño lo obtiene por una de dos vías, en este orden de preferencia:
     1. **El helper elevado EXTRAE el token del coordinador (preferida, sin `SYSTEM`; dirección de integridad correcta):** el helper se eleva por UAC (`ShellExecuteExW` verbo `runas`, §13.1) a integridad **alta**; el coordinador corre a integridad **media**. La política de integridad de Windows permite que un sujeto de integridad **mayor** abra un objeto de integridad **menor** (no al revés), por lo que el **helper (alto)** abre el proceso **coordinador (medio)** —cuyo `PID` + `ProcessCreationTime` conoce por el contexto de la operación y **verifica** (imagen firmada esperada, misma defensa PID-reuse que §19.1.5)— con `PROCESS_QUERY_INFORMATION | PROCESS_DUP_HANDLE`, hace `OpenProcessToken` + `DuplicateTokenEx` para obtener el token efectivo del operador, y con él lanza el verificador según la API normativa de §12.3 (`CreateProcessWithTokenW` porque este helper es UAC-elevado y sólo tiene `SeImpersonatePrivilege`; `CreateProcessAsUserW` sólo si el helper corre como SYSTEM/servicio). **No** se intenta la dirección inversa (el coordinador medio NO puede abrir el helper alto con `PROCESS_DUP_HANDLE`: lo bloquea el filtro de integridad; y `ShellExecuteExW` no propaga `bInheritHandles`). No requiere `SYSTEM` ni `SeTcbPrivilege`.
     2. **`WTSQueryUserToken` por el componente privilegiado:** si la operación la conduce el servicio privilegiado de Sky-Claw (que arranca como `SYSTEM`/con `SeTcbPrivilege`, ver §11.3), éste obtiene el token del operador con `WTSQueryUserToken(interactive_session_id)`.
   - **Fail-closed si el token no está disponible:** si en un escenario OTS no se puede obtener el token efectivo del operador original por ninguna de las dos vías, la operación **no** procede a post-verificación bajo un token ajeno; se rehúsa **antes de mutar** (`REFUSE_TO_PLAN`) — la garantía `HARDENED` bajo `CURRENT_EFFECTIVE_UNELEVATED_TOKEN` nunca se evalúa con un token que no representa al operador. `GP2-T22` (OTS) se extiende para cubrir este desenlace.
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
- `parent_observation.granted_rights` contiene `CHANGE_OWNER` (`WRITE_OWNER`): **siempre** `REFUSE_TO_APPLY` en v1. GP1 sólo expone `owner_rights_ace_present` (bool), no la máscara de la Owner Rights ACE del parent; sin la máscara, una ACE permisiva permitiría tomar posesión, obtener `WRITE_DAC` implícito, auto-concederse `FILE_DELETE_CHILD` y reemplazar el Golden root. Con evidencia insuficiente el desenlace es fail-closed (el clasificador GP1 devuelve `UNKNOWN` para este caso); GP2 no continúa ni siquiera con `owner_rights_ace_present == True`.

**Desenlace Obligatorio:**
`GP2Result(success=False, state=REFUSE_TO_APPLY, message="El directorio contenedor (parent) no es seguro...")`

### 14.3 Prohibición de Warning + Continue y Override
Queda terminantemente prohibido degradar esta condición a una advertencia permisiva o permitir un flag `--force` / checkbox "Continuar de todos modos". Si el parent es inseguro, GP1 post-verificación jamás podría emitir `HARDENED`, por lo que la operación fallaría inevitablemente.

---

## 15. Reparse & Hardlink Policy (Normativa Unificada)

- **Tolerancia Cero a Reparse Points:** La presencia de cualquier reparse tag (`st_reparse_tag != 0`), symlink, junction, volume mount point o placeholder en el root, en cualquier nodo descendiente o en el parent inmediato produce de forma canónica y exclusiva el estado **`REFUSE_TO_APPLY`** con `success=False` y excepción `InventoryLinkError`.
- **Tolerancia Cero a Hardlinks:** La presencia de dos rutas que compartan el mismo `(VolumeSerialNumber, FileId)` **dentro** del subárbol, o de cualquier archivo regular con `NumberOfLinks > 1` (hardlink que **escapa** del subárbol), produce el estado **`REFUSE_TO_APPLY`** con `success=False` y excepción `DuplicateFileIdError`.
- **Primitiva Única:** La detección utiliza exclusivamente `sky_claw.app.security.links.link_kind_and_identity_or_raise`. Prohibido reimplementar inspecciones ad-hoc.
- **Race Condition Gate:** Si un archivo o directorio es reemplazado por un enlace entre la fase de planificación y la ejecución del helper, el chequeo de `FileId` y `reparse_tag` sobre el handle abierto del helper detecta la colisión antes de mutar y aborta la transacción con `REFUSE_TO_APPLY` / rollback.
- **Hardlink externo TOCTOU (revalidación viva):** añadir un segundo hardlink hacia un nodo del subárbol **no** cambia su `FileId`, su SD ni su `reparse_tag`, por lo que los chequeos anteriores no lo detectan. Por eso el bucle de mutación (§12.2 paso 2) revalida `FileStandardInfo.NumberOfLinks == 1` sobre el **handle vivo** de cada archivo regular, inmediatamente antes de `SetSecurityInfo`: si un Actor D creó un enlace externo entre el sellado (§10.1, que sólo inspecciona el manifiesto) y la mutación, `NumberOfLinks > 1` -> `REFUSE_TO_APPLY` (0 nodos) / `ROLLBACK_REQUIRED` (>0), sin mutar la ACL del objeto compartido (impide el bypass de confinamiento del root).

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

El flag `SE_DACL_PROTECTED` del descriptor PRE de cada nodo se captura durante la fase de mutación y se persiste en el journal asociado a `MUTATING(K)` (ver §12.2 paso 4). El rollback selecciona el flag de `SetSecurityInfo` que se pasa a Windows según ese valor capturado **dentro del bloque normativo completo de §12.2 (rollback)**, que reconstruye `pre_owner`, `pre_group` y `pre_dacl` desde `pre_sd_bytes_b64` y los pasa como parámetros separados:

```text
if captured_pre_dacl_protected_flag & SE_DACL_PROTECTED:
    SetSecurityInfo flags include PROTECTED_DACL_SECURITY_INFORMATION
else:
    SetSecurityInfo flags include UNPROTECTED_DACL_SECURITY_INFORMATION
```

Pasar siempre `PROTECTED_DACL_SECURITY_INFORMATION` en el rollback dejaría `SE_DACL_PROTECTED` activado en nodos que originalmente heredaban de su parent, alterando la semántica de propagación DACL posterior y violando la promesa de "restauración exacta" del §2.6. Pasar siempre `UNPROTECTED_DACL_SECURITY_INFORMATION` produciría el mismo defecto en sentido inverso. La decisión es por nodo, leída del journal, y es la única vía válida en v1. **El flag DACL no es suficiente para restaurar OWNER/GROUP**: la llamada completa (OWNER | GROUP | DACL | protection_flag con `pre_owner`/`pre_group`/`pre_dacl` extraídos del SD) es normativa y está definida en §12.2.

`GP2-T12` extiende su oráculo para incluir un caso explícito: nodo con DACL PRE heredada (no protegida) -> APPLY -> fallo simulado -> rollback -> `SE_DACL_PROTECTED` del nodo es exactamente el del PRE (False). El mutante `M-R1` cubre la omisión de `UNPROTECTED_DACL_SECURITY_INFORMATION`.

---

## 18. Privilege Strategy

El helper privilegiado gestiona sus privilegios de token con máxima restricción temporal:

```text
TAKE_OWNERSHIP_REQUIRED_WHEN = NEVER_IN_V1 (Preserve Owner by default; SeTakeOwnershipPrivilege nunca se habilita)
SE_RESTORE_REQUIRED_WHEN = ONLY_IF_ACCESS_DENIED_BY_EXISTING_RESTRICTIVE_DACL (probe y handle de mutación)
SE_BACKUP_REQUIRED_WHEN  = NEVER (ninguna apertura del helper solicita READ_DATA con backup semantics)
SE_SECURITY_REQUIRED = NO (SACL no modificada)
PRIVILEGE_ENABLE_SCOPE = EXACT_WIN32_CALL_BOUNDARY
PRIVILEGE_RESTORE_POLICY = ALWAYS_RESTORE_PREVIOUS_STATE_IN_FINALLY
```

### 18.1 Manejo de `AdjustTokenPrivileges`
- Toda activación de privilegios (`SeRestorePrivilege`) se habilita **justo antes** de la llamada Win32 que lo requiere (apertura del probe o del handle de mutación) y se deshabilita inmediatamente después en un bloque de guarda `finally`, restaurando `TOKEN_PRIVILEGES.PreviousState`.
- La función de habilitación valida explícitamente `GetLastError() != ERROR_NOT_ALL_ASSIGNED`. Si el privilegio no fue asignado, la operación falla cerrado.
- Si tras habilitar `SeRestorePrivilege` la apertura sigue devolviendo `ACCESS_DENIED`, se bifurca por nodos mutados (`0 -> REFUSE_TO_APPLY`; `>0 -> ROLLBACK_REQUIRED`) sin intentar `SeTakeOwnershipPrivilege` (no se transfiere ownership en v1; el contrato es fail-closed).

### 18.2 Estrategia Normativa Única de Privilegios (least privilege con retry documentado)

La **única** política de privilegios del helper para las aperturas de nodo es:

```text
1. Intentar CreateFileW SIN privilegios habilitados (least privilege).
2. Si el resultado es ERROR_ACCESS_DENIED:
   a. Habilitar SeRestorePrivilege (validando ERROR_NOT_ALL_ASSIGNED; si no está asignado -> fail-closed).
   b. Reintentar la MISMA CreateFileW una única vez.
   c. Deshabilitar SeRestorePrivilege en finally (restaurar PreviousState).
3. Si el retry persiste en ERROR_ACCESS_DENIED:
   - mutated_nodes == 0 -> REFUSE_TO_APPLY (ProbeAccessDeniedError)
   - mutated_nodes > 0  -> ROLLBACK_REQUIRED
4. Cualquier otro GetLastError() tras el retry -> misma bifurcación 0/>0.
```

Justificación técnica (documentación primaria Microsoft, `CreateFileW` / File Security and Access Rights): con `FILE_FLAG_BACKUP_SEMANTICS`, el sistema omite las comprobaciones de la DACL para las clases solicitadas **sólo si el token tiene habilitado `SeRestorePrivilege`** (semántica write/delete: `FILE_WRITE_DATA`, `FILE_APPEND_DATA`, `FILE_WRITE_ATTRIBUTES`, `FILE_WRITE_EA`, `DELETE`, y también `WRITE_DAC`/`WRITE_OWNER`). `SeBackupPrivilege` cubre exclusivamente la semántica de **lectura** (`FILE_READ_DATA`), que el probe y el handle de mutación **no solicitan**; por tanto **no se habilita nunca** (least privilege). `FILE_FLAG_OPEN_REPARSE_POINT` y la verificación posterior de `ReparseTag == 0` se mantienen en ambos handles.

**`WRITE_OWNER` y la elección de `SeRestorePrivilege` (no `SeTakeOwnershipPrivilege`) — evidencia primaria:** la constante de privilegio **`SE_RESTORE_NAME` (`SeRestorePrivilege`)** está documentada como "grants all *write* access control to any file, regardless of the ACL", y la lista explícita de derechos que concede incluye **`WRITE_DAC` y `WRITE_OWNER`** (además de `FILE_ADD_FILE`, `FILE_ADD_SUBDIRECTORY`, `DELETE`, etc.). Por tanto, abrir el handle de mutación con `READ_CONTROL | WRITE_DAC | WRITE_OWNER` bajo `FILE_FLAG_BACKUP_SEMANTICS` **sí** queda cubierto por el retry con `SeRestorePrivilege` cuando la DACL preexistente lo deniega (incluido el caso típico de un árbol cuyo `owner` es el usuario y cuya DACL no incluye ACE de `Administrators`: el helper elevado no es el dueño ni tiene ACE, obtiene `ACCESS_DENIED`, habilita `SeRestorePrivilege` y reabre con éxito). La elección de `SeRestorePrivilege` es además la **única** correcta para el rollback: restaurar el `OWNER` al SID **original** (que puede no ser el del helper) es una operación de *restore* que exige `SeRestorePrivilege`; `SeTakeOwnershipPrivilege` sólo permite fijar el owner **a uno mismo** (SID del token), por lo que sería insuficiente para restaurar un owner ajeno — y está prohibido en v1 (§8.3/§18). No hay, por tanto, contradicción: el contrato de apertura (`WRITE_OWNER`) y la política de privilegios (`SeRestorePrivilege`) son coherentes y suficientes. La semántica exacta se ancla además con `GP2-T37` (rollback restaura OWNER/GROUP) sobre un fixture cuyo owner es un SID distinto del helper.

El handle de mutación (paso 2 del §12.2), además del retry de apertura, mantiene `SeRestorePrivilege` habilitado durante toda la secuencia por nodo (apertura, lectura de SD PRE, `SetSecurityInfo`, re-lectura de SD POST) cuando la DACL preexistente así lo exige; el probe (paso 1) usa únicamente el retry puntual y cierra el handle antes de restaurar el estado del token.

---

## 19. Transaction FSM, Journal Ownership & GoldenMutationLock

Para evitar cualquier interferencia con el trabajo paralelo del Issue #506 en `sky_claw/app/db/journal.py`, GP2 implementa un almacén transaccional y lock exclusivo en `sky_claw/local/runtime_vault/protection_journal.py` (en `AUTHORIZED_OPERATIONS`).

### 19.1 Lock Cross-Process por Golden (`GoldenMutationLock`)
1. **Primitiva OS-Enforced (contrato Win32 inequívoco):** El lock se adquiere mediante `CreateFileW` abriendo el archivo `%ProgramData%\Sky-Claw\runtime_vault\locks\skyclaw_golden_lock_<vol_serial>_<root_file_id>.lock` con:
   ```text
   dwDesiredAccess         = GENERIC_READ | GENERIC_WRITE
   dwShareMode             = 0    // EXCLUSIVO: sin FILE_SHARE_READ, sin FILE_SHARE_WRITE, sin FILE_SHARE_DELETE
   dwCreationDisposition   = OPEN_ALWAYS (crear si no existe, abrir si existe)
   dwFlagsAndAttributes    = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT
   ```
   El handle se mantiene abierto durante **toda** la autoridad transaccional. `dwShareMode = 0` es contractual y no se omite: la consecuencia es que el archivo de lock sólo puede ser reabierto por otro mutador después de que el kernel cierre el handle (muerte de proceso, kill, `CloseHandle`). No se admite `dwDesiredAccess` implícito. La defensa contra sustitución por reparse/junction se implementa con `FILE_FLAG_OPEN_REPARSE_POINT` más la verificación post-open de `GetFileInformationByHandleEx(..., FileAttributeTagInfo).ReparseTag == 0` descrita en §19.1.6 (no se usa `FILE_FLAG_DELETE_PROTECT`, que no existe en la API Win32).
2. **Clave de Identidad:**
   `lock_key = f"skyclaw_golden_lock_{volume_serial}_{root_file_id}"`
3. **Participantes Obligatorios:** GP2 APPLY, GP2 ROLLBACK, GP2 RECOVERY y futuro GP3 UNPROTECT. Todo mutador que abre un archivo bajo el Golden o escribe en `AUTHORIZED_OPERATIONS` debe **adquirir el lock con el contrato exacto del punto 1** antes del primer byte mutado. Queda prohibido un segundo FSM writer.
4. **Comportamiento ante Muerte de Proceso:** El kernel de Windows cierra automáticamente todos los handles abiertos cuando un proceso finaliza (sea por crash, kill o salida normal), liberando la exclusión mutua de forma síncrona.
5. **Defensa contra Reuso de PID y Recuperación de Lock Huérfano:**
   - Dentro del archivo de lock se escribe la metadata del dueño: `{"lock_key": "...", "owner_pid": 1234, "owner_process_creation_time": 133456789012345678, "session_id": 1, "operation_id": "...", "phase": "APPLYING", "created_at": 1756245000}`.
   - **Ciclo de vida del lock file (normativo, cierra el caso "lock residual"):**
     - En toda **terminación normal** (`COMMITTED`, `ROLLED_BACK`, `CANCELLED`, `REFUSE_TO_APPLY`), el propietario escribe `phase=RELEASED` en la metadata, invoca `FlushFileBuffers` y luego `CloseHandle`. **El archivo de lock NO se borra.** El contrato de apertura mantiene `dwShareMode = 0` sin `FILE_SHARE_DELETE` (§19.1.1), por lo que un `DeleteFileW` sobre el handle todavía abierto fallaría de forma determinista con `ERROR_SHARING_VIOLATION`, y posponerlo hasta después de `CloseHandle` abriría una carrera con otro mutador que adquiriese el lock entre el cierre y el borrado. En su lugar el archivo persiste (`dwCreationDisposition = OPEN_ALWAYS`, §19.1.1) con `phase=RELEASED`: la próxima adquisición reabre el mismo archivo, detecta `phase=RELEASED` y sobreescribe la metadata como residual (no es un lock activo). No se requiere borrado privilegiado ni intervención manual.
     - En terminación por **crash**, el kernel cierra el handle y el archivo conserva metadata con `phase != RELEASED`.
   - Si un proceso intenta adquirir el lock y `CreateFileW` tiene éxito (el handle fue liberado por el SO), pero existe metadata de una operación previa:
     - Si `phase == RELEASED` -> metadata **residual** de una operación terminada; el adquirente la sobreescribe y continúa (no es un lock activo).
     - Si `phase != RELEASED` (operación previa no concluida):
       - El recuperador consulta `GetProcessTimes` sobre `owner_pid`. Si el proceso existe y su `CreationTime` coincide con `owner_process_creation_time`, el proceso sigue vivo y NO se puede robar el lock (`GoldenLockError`).
       - Si el proceso murió o el `CreationTime` difiere (PID reuse detectado), se confirma que el lock quedó huérfano por crash y se transiciona de forma segura al procedimiento de Crash Recovery (§20).
     - Prohibido robar el lock basándose en timeout de heartbeat si el proceso propietario sigue vivo.
   - **Oráculo:** tras una operación normal completada (COMMITTED/ROLLED_BACK) y la liberación del lock, la siguiente adquisición sobre el mismo Golden debe tener éxito **sin intervención manual** (ni borrado privilegiado ni reinicio); GP2-T20 cubre esta secuencia de re-adquisición.
6. **Prohibición de reparse-substitution en el lock file:** cualquier `ReparseTag != 0` al reabrir el lock aborta la operación con `GoldenLockError` (defensa contra Actor D que reemplaza el lock por un junction).

### 19.2 FSM Completa y Autoridad del Helper

```text
WHO_WRITES_AUTHORITATIVE_FSM = Elevated Helper Process ONLY
WHO_WRITES_EARLY_FSM        = Unelevated Coordinator ONLY (estados PREPARING/PREPARED/AWAITING_ELEVATION + CANCELLED/ELEVATION_REJECTED, persistidos en UNTRUSTED_STAGING, no autoritativos, sin GoldenMutationLock; ver nota "Dos fases del FSM")
WHO_WRITES_AUTHORIZED_OPERATIONS = Elevated Helper Process ONLY
WHO_WRITES_COMMITTED        = Elevated Helper Process ONLY
WHO_WRITES_ARCHIVING_BACKUP = Elevated Helper Process ONLY
POSTVERIFY_EXECUTOR         = Unelevated Verifier Process (read-only, hijo lanzado por el Helper; resultado vía canal IPC autenticado normativo §12.3)
BACKUP_ARCHIVER             = Elevated Helper Process
LOCK_OWNER_DURING_POSTVERIFY = Elevated Helper Process (mantiene el GoldenMutationLock continuamente)
VERIFIER_CAN_WRITE_FSM      = NO
VERIFIER_CAN_WRITE_AUTHORIZED_OPERATIONS = NO
```

```text
PREPARING -> PREPARED -> AWAITING_ELEVATION -> APPLYING -> VERIFYING_GP1 -> VERIFYING_RV2 -> VERIFYING_NODE_SET -> ARCHIVING_BACKUP -> COMMITTED
   |            |               |                 |               |               |                    |                   |
   v            v               v                 v               v               v                    v                   v
REFUSE_TO_APPLY CANCELLED REFUSE_TO_PLAN / ELEVATION_REJECTED ROLLBACK_REQ   ROLLBACK_REQ    ROLLBACK_REQ         ROLLBACK_REQ        ROLLBACK_REQ
                                                  |               |               |                    |
                                                  +---------------+---------------+--------------------+
                                                                  |               |
                                                                  v               v
                                                             ROLLING_BACK   INDETERMINATE (evidencia ambigua)
                                                              /        \
                                                             v          v
                                                        ROLLED_BACK  ROLLBACK_FAILED / INDETERMINATE
```

**Transiciones a `INDETERMINATE` (normativas):**
- `APPLYING -> INDETERMINATE`: crash pre-flush del journal WAL (fila C4b de §20) o cualquier evidencia transaccional ambigua durante la mutación (re-escaneo de recuperación sin registro durable ni drift decidible).
- `VERIFYING_GP1 | VERIFYING_RV2 | VERIFYING_NODE_SET -> INDETERMINATE`: evidencia de verificación ambigua o inconsistente que impide decidir entre `ROLLBACK_REQUIRED` y éxito.
- `ROLLING_BACK -> INDETERMINATE`: fallo de rollback con estado irrecuperable (fila C9 de §20, inconsistencia de `FileId`).
- `APPLYING | VERIFYING_* -> ROLLBACK_REQUIRED -> ROLLING_BACK -> ROLLED_BACK | ROLLBACK_FAILED`: camino de fallo determinista sin ambigüedad.
El **conjunto completo de transiciones** del FSM (incluidas las de `INDETERMINATE`) se ancla por igualdad literal en `GP2-T15` (patrón `RITUAL_TOOL_MAP` del repositorio): una transición no declarada rompe el test.

**Nota sobre `REFUSE_TO_PLAN`:** `REFUSE_TO_PLAN` es un **estado terminal no-mutador** de la fase de autorización del helper, alcanzable desde `AWAITING_ELEVATION` (y sólo desde ahí) cuando falla cualquiera de los tres componentes normativos de §11.0 (TGR, PPSC) o la validación criptográfica de precondiciones de §12.2 paso 5 (binding `pre_sd_bytes_b64`/`pre_sd_sha256`, unicidad de `(VolumeSerialNumber, FileId)`, consistencia de jerarquía). Se alcanza con **cero nodos mutados**, sin `GoldenMutationLock` adquirido y sin `authorized_plan.json` creado; el lock se libera (o nunca se adquirió) y la rama `staging` queda intacta. Figura en la taxonomía de errores §24 y sus oráculos son GP2-T26/T29/T31/T33/T35. `REFUSE_TO_APPLY` (coordinador/preflight) y `REFUSE_TO_PLAN` (helper/autorización) son estados distintos y no intercambiables.

**Dos fases del FSM (coordinador vs helper):**
- **FSM temprano (PREPARING -> PREPARED -> AWAITING_ELEVATION, y `CANCELLED`/`ELEVATION_REJECTED`):** transcurre **antes** de que exista el helper elevado; el coordinador no elevado lo persiste en `UNTRUSTED_STAGING` (único lugar que puede escribir). `ELEVATION_REJECTED` se registra en staging por el coordinador cuando el UAC se cancela (o por el helper si arrancó y falló la elevación). Estas escrituras no son autoritativas (el staging es UNTRUSTED) y no requieren el `GoldenMutationLock`.
- **FSM autoritativo lock-gated (APPLYING -> ... -> COMMITTED, y ROLLBACK_* / INDETERMINATE):** comienza cuando el helper elevado adquiere el `GoldenMutationLock` y escribe el journal en `AUTHORIZED_OPERATIONS`; a partir de ese punto aplica el contrato `WHO_WRITES_* = Elevated Helper Process ONLY` de §19.2. La matriz de crash recovery §20 (C1/C2) referencia la evidencia del staging para la fase temprana y el journal autoritativo para la fase helper.
- **`REFUSE_TO_PLAN` (terminal de autorización del helper, NO lock-gated):** lo escribe el **helper elevado** (no un proceso no elevado) durante su fase de autorización (§12.2 pasos 3-5), que ocurre **antes** de adquirir el `GoldenMutationLock` y de crear `authorized_plan.json`. El `GoldenMutationLock` gobierna únicamente la **mutación del Golden** (desde `APPLYING`), no las decisiones de autorización; por eso `REFUSE_TO_PLAN` se alcanza con el lock **no adquirido** y cero nodos mutados. Escritor: helper elevado; store: registro de autorización del helper en `AUTHORIZED_OPERATIONS` (el helper es elevado y puede escribirlo) o retorno directo si `operations\<op_id>\` aún no existe; **nunca** en `staging` por un no-elevado. No forma parte del subconjunto lock-gated. `WHO_WRITES_AUTHORITATIVE_FSM` se respeta porque el escritor sigue siendo el helper elevado. `GP2-T15`/`GP2-T31` anclan que `AWAITING_ELEVATION -> REFUSE_TO_PLAN` **no adquiere el `GoldenMutationLock` ni crea `authorized_plan.json`**.

**Nota sobre la transición `VERIFYING_NODE_SET -> ARCHIVING_BACKUP`:** esta transición **no es directa**; incorpora el **rerun del probe + una RV-2 (`TreeDigest`) final** (§12.2, "Rerun del probe + RV-2 final") ejecutados por el helper elevado. Cualquier `ERROR_SHARING_VIOLATION` en el rerun (tras `MAX_PROBE_RETRIES`) o drift de `TreeDigest` en la RV-2 final -> `ROLLBACK_REQUIRED` antes de archivar. Esto hace consistentes `GP2-T24` Parte 2 y `M-W1` (que exigen detectar drift de un writer post-rerun antes de `COMMITTED`). No se añaden estados nuevos al FSM: son verificaciones dentro de la arista declarada.

**Nota sobre `ARCHIVING_BACKUP`:** la fase `ARCHIVING_BACKUP` puede fallar transitoriamente (espacio en disco, antivirus). La política de reintentos es **acotada a nivel de fase** (reintentos dentro de `ARCHIVING_BACKUP` hasta un máximo configurable `MAX_ARCHIVE_RETRIES`); si los reintentos se agotan, la transición es a `ROLLBACK_REQUIRED` (no a un estado huérfano `RETRY_ARCHIVE`). El lock se mantiene durante los reintentos. `GP2-T15` cubre el caso de archivo exitoso y de archivo fallido tras agotar reintentos.

---

## 20. Crash Recovery Matrix

Matriz exhaustiva de recuperación ante caídas del sistema o interrupciones de proceso en cada frontera:

| Punto de Crash | Evidencia Durable en Disco | Estado Recuperado | Acción Segura al Reiniciar | ¿Auto-Recovery? | ¿Requiere HITL? |
|---|---|---|---|---|---|
| **C1: Durante PREPARING** | Manifiesto ausente o incompleto en staging | `None` / Huérfano | Fase temprana del coordinador no elevado (sin `GoldenMutationLock` adquirido; ver "Dos fases del FSM" §19.2). Limpieza de temporales en staging. Estado no modificado. | Sí | No |
| **C2: Manifiesto sellado (PREPARED)** | Manifiesto íntegro en staging, 0 nodos mutados | `PREPARED` | Fase temprana del coordinador no elevado (sin `GoldenMutationLock` adquirido). Operación cancelable sin efectos en FS; limpieza de staging. | Sí | No |
| **C3: UAC aceptado, antes del Nodo 1** | Journal `APPLYING` en authorized store, lista de nodos mutados vacía | `APPLYING` (0 nodes) | **Transición declarada del FSM (§19.2), sin estados nuevos:** el helper elevado (nueva invocación UAC) retoma en la transición declarada `APPLYING -> ROLLBACK_REQUIRED` con **0 nodos a restaurar** -> `ROLLING_BACK` (no-op) -> `ROLLED_BACK` (terminal declarado), y libera/limpia el lock. **NO** se emite `CANCELLED` desde `APPLYING` (esa transición no existe en el FSM; `CANCELLED` sólo es alcanzable desde la fase temprana). Ninguna mutación ocurrió, por lo que el rollback es vacío, pero recorre únicamente transiciones declaradas y respeta `WHO_WRITES_AUTHORITATIVE_FSM` (sólo el helper elevado escribe el journal autoritativo). | No (exige UAC) | Sí (UAC) |
| **C4: Durante mutación de Nodo K (tras MUTATING(K), antes de MUTATED(K))** | Journal con `MUTATING(K)` y `FlushFileBuffers` persistido | `ROLLBACK_REQUIRED` | Helper re-escanea nodos del plan (nodos 1..K); **primero** revalida `(VolumeSerialNumber, FileId)` esperado vs actual (binding de identidad, vía `GetFileInformationByHandleEx`) y **sólo después** compara bytes del SD actual contra el backup PRE para decidir si restaurar. Si `FileId` difiere -> `INDETERMINATE` y no se aplica SD. La comparación de bytes SD contra backup es una **detección de drift de mutación** (no una verificación del backup); §9.2.3 prohíbe usar esa comparación como sanity check del backup durante la mutación normal, pero la autoriza como paso legítimo de recuperación (las dos operaciones son distintas). | No (exige UAC) | Sí (UAC para rollback) |
| **C4b: Crash entre `write()` y `FlushFileBuffers` del journal WAL (antes de `MUTATING(K)` durable)** | `write()` ejecutado pero `FlushFileBuffers` no completado: la intención `MUTATING(K)` puede no estar en disco | `INDETERMINATE` | El journal no es evidencia durable del estado transaccional del nodo K. El recuperador re-escanea el plan con revalidación de identidad `(VolumeSerialNumber, FileId)` por nodo: (a) nodos con `MUTATED(K)` durable -> se tratan como mutados; (b) nodos con `MUTATING(K)` durable -> `ROLLBACK_REQUIRED`; (c) nodos sin registro durable -> comparar SD actual contra backup PRE **sólo como detección de drift** tras revalidar identidad; si la evidencia es ambigua -> `INDETERMINATE` con informe forense y lock retenido para inspección. GP2-T15 ancla este desenlace (crash simulado entre `write()` y `flush()` -> `INDETERMINATE`, nunca `COMMITTED` silencioso). | No (exige UAC) | Sí (Alerta Crítica) |
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
   - `result.success is True` — condición **necesaria pero no suficiente**: `GoldenProtectionResult.success == (state != UNKNOWN)` (ver §4, invariante 5), por lo que no aporta por sí sola la garantía de endurecimiento.
   - `result.state is GoldenProtectionState.HARDENED` — **gate independiente y decisivo**. El estado `HARDENED` es el único admisible; `UNSUPPORTED`, `UNPROTECTED`, `WRITE_PROTECTED` y `UNKNOWN` tienen `success == True` salvo `UNKNOWN`, y ninguno constituye éxito GP2.
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

> **Fuente del conjunto fresco (no es la evidencia de GP1).** El contrato vigente de GP1 (`NodeProtectionObservation` en `protection.py`) expone por nodo **sólo** `(relative_path, node_kind, owner_sid, granted_rights, owner_rights_ace_present, dacl_inheritance_protected)` — **no** expone `VolumeSerialNumber` ni `FileId`. Por tanto el conjunto fresco **no** se construye desde `gp1_post_evidence.nodes`: GP2 realiza su **propio re-inventario canónico post-mutación** (misma primitiva de `§10`/`§15`: `link_kind_and_identity_or_raise` + `GetFileInformationByHandleEx(FileIdInfo)`), que sí obtiene `(VolumeSerialNumber, FileId)` por nodo. Esto mantiene el gate fuerte **sin modificar GP1** (ADR 0009 permanece intacto, conforme a la regla design-only). La verificación de acceso `HARDENED` de GP1 (§21) y esta exhaustividad de nodos son ejes independientes del triple gate.

1. Se extrae el conjunto canónico del **re-inventario fresco de GP2** (post-mutación, bajo el token no elevado):
   `FRESH_POST_CANONICAL_NODE_SET = {(n.relative_path, n.node_kind, n.volume_serial_number, n.file_id) for n in gp2_fresh_post_scan.nodes}`
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
| `PlanAuthorizationError` | TGR no registra la ruta, PPSC rechazada/cancelada, o binding criptográfico del plan falla en la fase de autorización | `REFUSE_TO_PLAN` (terminal no-mutador, desde `AWAITING_ELEVATION`, 0 nodos mutados; ver §19.2 y §12.2 pasos 3-5) |
| `QuiescenceViolationError` | Handle de escritura o mapping preexistente en un nodo (`ERROR_SHARING_VIOLATION`) | `REFUSE_TO_APPLY` / `ROLLBACK_REQUIRED` |
| `ProbeAccessDeniedError` | El DACL preexistente deniega las clases del probe o del handle de mutación incluso tras el retry con `SeRestorePrivilege` (`ERROR_ACCESS_DENIED` persistente) | `0 nodos mutados -> REFUSE_TO_APPLY`; `>0 nodos mutados -> ROLLBACK_REQUIRED` (bifurcación determinista por conteo de nodos; ver §12.2 pasos 1-2 y §18.2) |
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
- **GP2-T15:** Simulación de crash en cada estado del FSM (incluyendo crash entre mutación y registro con WAL respaldado por `FlushFileBuffers`, rechazo de UAC resultando en `ELEVATION_REJECTED`, fallo TGR/PPSC/binding resultando en `REFUSE_TO_PLAN`, y crash en `ARCHIVING_BACKUP`) recupera la acción segura correcta. Ancla AST/comportamiento: verifica que la escritura del journal usa `FlushFileBuffers` y que la simulación de crash entre `write()` y `flush()` resulta en `INDETERMINATE` (no en `COMMITTED` silencioso) — desenlace anclado contra la fila C4b de §20. Ancla además el **conjunto de estados y transiciones del FSM** por igualdad literal (incluye `REFUSE_TO_PLAN` como terminal no-mutador y las transiciones a `INDETERMINATE` desde `APPLYING`, `VERIFYING_*` y `ROLLING_BACK` del diagrama §19.2), de modo que un estado o transición no declarada en el diagrama rompa el test.
- **GP2-T16:** AST Anchor: `protection_journal.py` no importa `sky_claw.app.db.journal`.
- **GP2-T17:** AST Anchor: El módulo no utiliza strings de cuentas localizadas ("Todos", "Administradores"), solo SIDs.
- **GP2-T18:** Presencia de hardlinks internos (mismo `(VolumeSerialNumber, FileId)` en 2 rutas) aborta con `DuplicateFileIdError` y estado `REFUSE_TO_APPLY`.
- **GP2-T19:** Omisión de un nodo pre-endurecido en el manifiesto es detectada por la verificación de igualdad `MANIFEST_NODE_SET == FRESH_POST_CANONICAL_NODE_SET` y aborta la transacción.
- **GP2-T20:** `GoldenMutationLock` serializa dos llamadas concurrentes sobre el mismo Golden rechazando la segunda con `GoldenLockError`. Aserta además que el contrato de apertura del lock usa `dwDesiredAccess = GENERIC_READ | GENERIC_WRITE`, `dwShareMode = 0` y `FILE_FLAG_OPEN_REPARSE_POINT`, y que tras abrir el lock se valida `ReparseTag == 0` y la pareja `(VolumeSerialNumber, FileId)` del lock file; si el lock fue sustituido por un junction, la segunda adquisición falla con `GoldenLockError`. **Re-adquisición post-terminal (oráculo de lock residual, §19.1.5):** completar una operación normal (COMMITTED/ROLLED_BACK), liberar el lock y adquirirlo de nuevo sobre el mismo Golden debe ser exitoso sin intervención manual; verifica que la metadata residual con `phase=RELEASED` (o el borrado del lock file) no dispara `GoldenLockError`.
- **GP2-T21:** La mutación se ejecuta sobre el mismo handle (`SetSecurityInfo(handle)`), fallando si el archivo se intenta reemplazar bajo el handle abierto.
- **GP2-T22:** El store de operaciones y backup en `%ProgramData%` permite resolver el plan de forma transparente y autenticar el respaldo en escenarios de elevación Over-The-Shoulder (OTS).
- **GP2-T23:** Oráculo de la HANDLE SHARE MATRIX del §4.1.1. El test enumera **literalmente las seis filas H1–H6** con su desenlace contractual por fila (sin parametrización parcial y sin mocks que sustituyan `CreateFileW`); **todas** las filas producen `ERROR_SHARING_VIOLATION` **a nivel de `CreateFileW`** (semántica bidireccional: el probe con `dwShareMode = 0` no comparte el acceso de datos del handle preexistente). El desenlace de FSM `REFUSE_TO_APPLY` (0 nodos mutados) se evalúa **tras agotar `MAX_PROBE_RETRIES`** conforme a la política de reintentos de §4.1.3 (ver `GP2-T45`): los fixtures de T23 **mantienen el handle colisionante abierto durante todo el test** (colisión persistente), de modo que los reintentos no lo limpian y el terminal es `REFUSE_TO_APPLY`. La aserción por fila es el resultado `ERROR_SHARING_VIOLATION` de cada intento:
  - H1 (`GENERIC_WRITE`, `dwShareMode = 0`) -> `ERROR_SHARING_VIOLATION`.
  - H2 (`GENERIC_WRITE`, `FILE_SHARE_READ`) -> `ERROR_SHARING_VIOLATION`.
  - H3 (`GENERIC_WRITE`, `FILE_SHARE_READ | FILE_SHARE_WRITE`) -> `ERROR_SHARING_VIOLATION`.
  - H4 (`GENERIC_WRITE`, `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`) -> `ERROR_SHARING_VIOLATION` (el probe no comparte write/delete).
  - H5 (`GENERIC_READ` con share incompleto, `FILE_SHARE_READ`) -> `ERROR_SHARING_VIOLATION`.
  - H6 (`GENERIC_READ` con share completo) -> `ERROR_SHARING_VIOLATION` (el probe no comparte read).
  - **Caso control "sin handles de datos":** sin handles preexistentes con acceso de datos, el probe tiene éxito y el flujo continúa a `SetSecurityInfo`.
  El desenlace esperado por fila es único y no admite ambigüedad entre `ERROR_SHARING_VIOLATION` y open exitoso.
- **GP2-T24:** WRITABLE MAPPING CASE (§4.1.2), dos partes con desenlace contractual por parte:
  - **Parte 1 (fail-closed):** handle abierto con `GENERIC_READ | GENERIC_WRITE` + `CreateFileMapping(PAGE_READWRITE)` sobre ese handle (construcción válida) -> el probe del helper (share=0) colisiona con el mapping por la regla bidireccional -> `ERROR_SHARING_VIOLATION` -> `REFUSE_TO_APPLY` (0 nodos) / `ROLLBACK_REQUIRED` (>0 nodos). Sin mock de `CreateFileW`; si el `CreateFileMapping` falla en el setup, el test falla explícitamente (setup error).
  - **Parte 2 (ventana post-rerun):** un writer abre un handle de datos **después** del rerun del probe pre-`ARCHIVING_BACKUP` y escribe un byte en un nodo mutado; el test aserta que la **RV-2 final posterior al rerun** (§12.2, "Rerun del probe + RV-2 final") detecta el drift de `TreeDigest` y transiciona a `ROLLBACK_REQUIRED` **antes de `COMMITTED`** — la omisión de esa RV-2 final (o ejecutar RV-2 sólo antes del rerun) hace fallar el test.
- **GP2-T25:** Intento de escritura en `AUTHORIZED_OPERATIONS` o `golden_backups` por parte de un proceso no elevado es bloqueado por la DACL del kernel (`ACCESS_DENIED`). Cubre además `trusted_goldens.json` y `locks/`.
- **GP2-T26 (ancla de binding pre_sd_bytes_b64):** test paramétrico que produce dos variantes del mismo plan —`(a)` plan correcto y `(b)` plan con `pre_sd_sha256` legítimo y `pre_sd_bytes_b64` adulterado— y verifica que el helper rechaza `(b)` con `PreSdBytesHashMismatchError` *antes* de la primera mutación, y acepta `(a)`. Complementa al mutante `M-B1`.
- **GP2-T27:** AST Anchor de Simetría de Superficies: enumera por introspección/igualdad literal el set de herramientas mutadoras registradas tanto en `SupervisorAgent` (`tool_dispatcher`) como en `AsyncToolRegistry` (`LLMRouter`); detecta asimetrías de cableado y de `HITLGuard`/UAC. El ancla es por **igualdad literal del set** (no por muestreo), siguiendo el patrón de `RITUAL_TOOL_MAP` del repositorio.
- **GP2-T28 (TGR unprivileged mutation):** un proceso no elevado intenta añadir, modificar o borrar una entrada de `trusted_goldens.json` o sustituirlo por un junction -> `ACCESS_DENIED` por DACL, `REFUSE_TO_PLAN`.
- **GP2-T29 (PPSC sin UAC):** el helper no crea `authorized_plan.json` si el operador cancela o rechaza la PPSC; el desenlace es `REFUSE_TO_PLAN` y la rama `staging` queda intacta.
- **GP2-T30 (plan-specific confirmation parameters):** aserta que el diálogo PPSC muestra literalmente `operation_id`, `canonical_root`, `VolumeSerialNumber`, `root_file_id`, `TreeDigest`, `node_count`, `policy_version` y exige consentimiento explícito antes de crear `authorized_plan.json`.
- **GP2-T31 (UAC != plan authorization):** un UAC aceptado sin PPSC posterior no produce `authorized_plan.json`; el desenlace es `REFUSE_TO_PLAN` con traza en el journal.
- **GP2-T32 (privileged privilege scope):** DACL preexistente que deniega `WRITE_DAC`/write-data a Administrators + elevación OTS secundaria: el helper aplica la estrategia §18.2 — primer intento del **probe** sin privilegios -> `ERROR_ACCESS_DENIED` -> habilita `SeRestorePrivilege` -> retry único -> éxito; y el mismo patrón para el handle de mutación. Si el retry persiste en `ACCESS_DENIED`, el desenlace es `REFUSE_TO_APPLY` con 0 nodos mutados (o `ROLLBACK_REQUIRED` con >0), nunca `SeTakeOwnershipPrivilege`. Aserta que `SeBackupPrivilege` nunca se habilita.
- **GP2-T33 (verifier cannot write FSM):** el verificador no elevado intenta escribir en `AUTHORIZED_OPERATIONS` o transicionar a `COMMITTED`/`ARCHIVING_BACKUP` -> `ACCESS_DENIED` por DACL y desenlace `REFUSE_TO_PLAN`/`INDETERMINATE`.
- **GP2-T34 (intermediate directory DACL):** un proceso no elevado intenta borrar `trusted_goldens.json` o `authorized_plan.json` a través del directorio padre (no por acceso directo al archivo) -> `ACCESS_DENIED` por la DACL del directorio intermedio. Complementa a `M-D1`. Cubre además la DACL de `staging/`: un usuario no elevado puede crear `staging\<op_id>\` y escribir en su propia operación (como `CREATOR OWNER`), pero **un SEGUNDO usuario no elevado NO puede escribir ni sustituir el `candidate_manifest.json` de la operación ajena** (la escritura está acotada al `CREATOR OWNER`, no a `Authenticated Users`; ver §11.3), ni crear archivos sueltos en `staging/`, ni obtener `FILE_DELETE_CHILD` sobre `operations/`, `locks/`, `golden_backups/`.
- **GP2-T35 (binding validated before lock acquired):** un plan con `pre_sd_bytes_b64` adulterado en un nodo intermedio (K>1) es rechazado con `REFUSE_TO_PLAN` y el `GoldenMutationLock` nunca es adquirido; cero nodos mutados en disco. Complementa a `M-B2`.
- **GP2-T36 (probe ACCESS_DENIED bifurcation):** con DACL restrictiva que deniega write-data a Administrators, el probe falla `ERROR_ACCESS_DENIED`; tras el retry con `SeRestorePrivilege` persiste -> `REFUSE_TO_APPLY` (0 nodos mutados). El mismo fixture sobre un nodo K>1 (nodos previos mutados) -> `ROLLBACK_REQUIRED`. Aserta que `SeBackupPrivilege` no se habilita y que el `GoldenMutationLock` se libera en ambos desenlaces. Complementa a `M-P1`/`ProbeAccessDeniedError`.
- **GP2-T37 (rollback restaura OWNER/GROUP/DACL por componentes):** tras APPLY + fallo simulado en K, el rollback decodifica `pre_sd_bytes_b64`, extrae `pre_owner`/`pre_group`/`pre_dacl`/`SE_DACL_PROTECTED` y llama `SetSecurityInfo(handle, OWNER | GROUP | DACL | protection_flag, pre_owner, pre_group, pre_dacl, NULL)`; la re-lectura POST verifica exactamente `owner`, `group`, `dacl` (semántica y orden) y el bit de protección. Un fixture con `PRE_DACL_PROTECTED = False` verifica que el flag vuelve a `False`. Complementa a `M-R1`. Aserta además que el handle de mutación se abre con `READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES` (restaurar OWNER/GROUP exige `WRITE_OWNER`; `FILE_READ_ATTRIBUTES` es requerido por `GetFileInformationByHandleEx`) y **ejecuta efectivamente** las tres revalidaciones sobre el handle (`FileIdInfo`, `FileAttributeTagInfo`, `FileStandardInfo`), fallando el test si cualquiera devuelve `ERROR_ACCESS_DENIED` (evita el "pasa por la razón equivocada" cuando las llamadas nunca corren).
- **GP2-T38 (hardlink externo rechazado):** archivo dentro del Golden con `NumberOfLinks > 1` (enlace externo creado por el fixture) -> `DuplicateFileIdError` -> `REFUSE_TO_APPLY` en planificación, cero mutaciones. Complementa a la extensión de `M29`/`M-D1`.
- **GP2-T39 (TGR liga TreeDigest):** plan cuyo `TreeDigest` difiere del `tree_digest` registrado en `trusted_goldens.json` -> `REFUSE_TO_PLAN` (aunque `canonical_root`/`VolumeSerialNumber`/`root_file_id` coincidan). Complementa a `M-A2`/`M34`.
- **GP2-T40 (lock file sin lectura para no elevados):** un proceso no elevado intenta abrir un lock file en `locks/` con `GENERIC_READ` -> `ACCESS_DENIED` (sin `FILE_READ_DATA`); retener un handle de lectura para DoS es imposible. Complementa a `M35`.
- **GP2-T41 (descriptor vivo validado antes del lock):** manifest con `pre_sd_bytes_b64`/`pre_sd_sha256` auto-consistentes pero que NO coinciden con el descriptor vivo de un nodo K>1 -> `REFUSE_TO_PLAN` en la fase de autorización (§12.2 paso 5, validación de descriptores vivos) con cero mutaciones; el `GoldenMutationLock` nunca se adquiere. Complementa a `M-B2`.
- **GP2-T42 (parent WRITE_OWNER fail-closed):** parent con `CHANGE_OWNER` efectivo (con o sin Owner Rights ACE) -> `UnsafeParentError` -> `REFUSE_TO_APPLY`, cero mutaciones; GP2 nunca continúa basándose sólo en `owner_rights_ace_present`. Complementa a `M05`.
- **GP2-T43 (descriptor estructuralmente inválido rechazado):** manifest con `pre_sd_bytes_b64` auto-consistente pero estructuralmente inválido (longitud menor a `SECURITY_DESCRIPTOR_MIN_LENGTH` o `IsValidSecurityDescriptor == False`) -> `REFUSE_TO_PLAN` en la fase de autorización, sin parsear con accessors nativos.
- **GP2-T44 (refresco del TGR, §11.4):** (a) una operación GP2 apply nunca escribe `trusted_goldens.json` (ancla AST/comportamiento: apply sólo lee el TGR); (b) el flujo `REGISTER_OR_REFRESH_TRUSTED_GOLDEN` exige re-ejecución RV-2 con `state == VERIFIED` bajo el token del operador **más** confirmación privilegiada explícita antes de escribir la entrada, y actualiza `tree_digest` de forma atómica; (c) un proceso no elevado que intente escribir/refrescar `trusted_goldens.json` recibe `ACCESS_DENIED` por DACL. Un Golden actualizado legítimamente (nuevo `TreeDigest`) vuelve a ser protegible por GP2 sólo tras el refresco. Complementa a `M-T2`.
- **GP2-T45 (probe con reintentos acotados, §4.1.3):** (a) un lector benigno (`GENERIC_READ`, filas H5/H6) que cierra su handle dentro de la ventana de reintentos permite completar el probe y proseguir; (b) una colisión persistente agota `MAX_PROBE_RETRIES` y termina en `REFUSE_TO_APPLY` (0 nodos) / `ROLLBACK_REQUIRED` (>0) con la lista de nodos bloqueantes en el reporte; (c) el número de intentos y el backoff están acotados (sin bucle infinito) y el desenlace terminal sigue siendo fail-closed. Complementa a `M-Q3`.
- **GP2-T46 (IPC de post-verificación autenticado, §12.3):** (a) el helper sólo acepta el veredicto por el **canal privado** del hijo verificador que lanzó (pipe anónimo heredado con `CreateProcessAsUserW`, o named pipe aleatorio con DACL restrictiva con `CreateProcessWithTokenW`, según su privilegio), con imagen esperada, `PID`+`ProcessCreationTime` y nonce por operación vigente; incluye el caso de un helper UAC-elevado (sin `SeAssignPrimaryTokenPrivilege`) que **debe** usar `CreateProcessWithTokenW`; (b) un proceso ajeno que entregue `HARDENED`/`VERIFIED` por otro canal, o con nonce inválido/reutilizado, es ignorado y no produce `COMMITTED`; (c) muerte del verificador o resultado ausente/tardío -> `ROLLBACK_REQUIRED`, nunca `COMMITTED`. Complementa a `M-I1`.
- **GP2-T47 (hardlink externo TOCTOU post-sellado, §12.2 paso 2 / §15):** un fixture crea un hardlink externo hacia un archivo del subárbol **después** del sellado y **antes** de `SetSecurityInfo`; el test exige que la revalidación viva de `NumberOfLinks == 1` sobre el handle de mutación lo detecte y termine en `REFUSE_TO_APPLY` (0 nodos) / `ROLLBACK_REQUIRED` (>0) **sin** modificar la ACL del archivo externo. Se distingue de `GP2-T38` (que crea el enlace antes de planificar). Complementa a `M-H2`.

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
| **M-P1** | Helper habilita `SeRestorePrivilege` *después* de la apertura (perdiendo la oportunidad frente a un DACL restrictivo), o habilita `SeBackupPrivilege` innecesariamente | Test `GP2-T32`/`GP2-T36` exigen el patrón §18.2: retry con `SeRestorePrivilege` para probe y handle de mutación, `SeBackupPrivilege` nunca habilitado, y `REFUSE_TO_APPLY`/`ROLLBACK_REQUIRED` según la bifurcación 0/>0 |
| **M-W1** | Falta el rerun del probe antes de `ARCHIVING_BACKUP`; un writer que abrió durante la mutación sobrevive a `COMMITTED` | Test que abre un writer con `FILE_SHARE_READ | FILE_SHARE_WRITE` (no presente en el probe inicial porque aún no existía; abre durante la mutación). El rerun del probe (share=0) colisiona con su handle de datos por la regla bidireccional -> `ERROR_SHARING_VIOLATION` -> como ya hay nodos mutados, el desenlace contractual del FSM es **`ROLLBACK_REQUIRED` -> `ROLLING_BACK`** (nunca `REFUSE_TO_PLAN`, que sólo existe en la fase de planificación/autorización). Un writer que escribe un byte **tras el rerun** es detectado por la **RV-2 final posterior al rerun** (§12.2): drift de `TreeDigest` -> `ROLLBACK_REQUIRED` antes de `COMMITTED`. La única ventana no cerrada (declarada, no un defecto) es tras la RV-2 final y antes de `COMMITTED` (§4.1.1, sin exclusividad absoluta post-`COMMITTED`). |
| **M-B2** | Validación del binding `pre_sd_bytes_b64`/`pre_sd_sha256` se omite o sólo ocurre en el bucle per-nodo en vez de en la fase de autorización previa; un plan con binding adversario en nodo K>1 produce nodos 1..K-1 mutados sin restauración | Test que presenta un plan con binding adulterado sólo en un nodo intermedio y aserta que el desenlace es `REFUSE_TO_PLAN` (sin mutación), no un rollback parcial |
| **M-D1** | DACL de un directorio intermedio (`runtime_vault/`, `operations/`, `golden_backups/`, `locks/`) permite a un proceso no elevado añadir, borrar o sustituir entradas (rompe la garantía sobre la hoja) | Test que intenta borrar `trusted_goldens.json` o `authorized_plan.json` a través del parent directory y espera `ACCESS_DENIED` |
| **M-H1** | Inventario acepta un archivo con hardlink externo (`NumberOfLinks > 1`) o un `FileId` duplicado externo; el helper muta el SD de un archivo fuera del Golden | Test `GP2-T38` exige `DuplicateFileIdError` y `REFUSE_TO_APPLY` en planificación |
| **M-T1** | TGR omite `tree_digest` o el helper no lo compara; Actor D modifica un Golden registrado y el plan con digest alterado es aceptado | Test `GP2-T39` exige `REFUSE_TO_PLAN` cuando `plan.TreeDigest != tgr_entry.tree_digest` |
| **M-L1** | DACL de `locks/` concede lectura a Authenticated Users; Actor D retiene un handle de lectura y bloquea al helper con sharing violation permanente | Test `GP2-T40` exige `ACCESS_DENIED` al abrir el lock file con `GENERIC_READ` |
| **M-V1** | Validación del binding sólo contra bytes del manifest (sin comprobar el descriptor vivo); manifest auto-consistente forjado en nodo K>1 muta nodos previos | Test `GP2-T41` exige `REFUSE_TO_PLAN` en la fase de autorización, cero mutaciones |
| **M-O1** | Helper continúa con parent `WRITE_OWNER` efectivo cuando `owner_rights_ace_present == True` (máscara desconocida) | Test `GP2-T42` exige `REFUSE_TO_APPLY` siempre con `CHANGE_OWNER` efectivo |
| **M-S1** | Helper parsea `pre_sd_bytes_b64` con accessors nativos sin `IsValidSecurityDescriptor`/longitud mínima | Test `GP2-T43` exige `REFUSE_TO_PLAN` sin parsear descriptores inválidos |
| **M-T2** | Refresco del TGR confía en el `tree_digest` staged (Actor D) sin re-ejecutar RV-2 bajo el token del operador, o GP2 apply escribe el TGR por su cuenta | Test `GP2-T44` exige re-ejecución RV-2 `VERIFIED` + confirmación privilegiada antes de mutar la entrada, y que apply nunca escriba `trusted_goldens.json` |
| **M-Q3** | El probe aborta a `REFUSE_TO_APPLY` en la primera colisión `ERROR_SHARING_VIOLATION` (sin reintentos) por un lector benigno, o reintenta de forma no acotada | Test `GP2-T45` exige reintentos acotados con backoff y desenlace fail-closed sólo tras agotar `MAX_PROBE_RETRIES` |
| **M-I1** | El helper acepta el veredicto de post-verificación sin autenticar al peer (canal nombrado con DACL permisiva/nombre adivinable, sin verificar imagen/PID/creation-time/nonce), o lanza el verificador con `CreateProcessW` bajo su propio token elevado, permitiendo a Actor D inyectar un falso `HARDENED`/`VERIFIED` o medir con el token equivocado | Test `GP2-T46` exige el canal privado acoplado a la API de lanzamiento (§12.3) + verificación de peer + nonce, y `ROLLBACK_REQUIRED` ante resultado no autenticado o ausente |
| **M-H2** | El bucle de mutación no revalida `NumberOfLinks` sobre el handle vivo; un hardlink externo creado tras el sellado hace que `SetSecurityInfo` mute también la ACL del archivo externo (bypass de confinamiento) | Test `GP2-T47` crea el enlace externo post-sellado y exige `REFUSE_TO_APPLY`/`ROLLBACK_REQUIRED` sin tocar el archivo externo |

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
- `IoCheckShareAccess` routine (share-access sólo se evalúa para clases de datos read/write/delete; `READ_CONTROL`/`WRITE_DAC`/`WRITE_OWNER` no participan) — *learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/nf-ntifs-iocheckshareaccess*
- `CreateFileW` function (dwShareMode, sharing model) — *learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew*
- Privilege Constants — `SE_RESTORE_NAME` grants all write access control incl. `WRITE_DAC`/`WRITE_OWNER`; `SE_TAKE_OWNERSHIP_NAME` sets owner to self only — *learn.microsoft.com/windows/win32/secauthz/privilege-constants*
- `CreatePipe` (anonymous pipe) / `CreateProcessAsUserW` (handle inheritance; requires `SeAssignPrimaryTokenPrivilege`) — *learn.microsoft.com/windows/win32/api/namedpipeapi/nf-namedpipeapi-createpipe* · *learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw*
- `CreateProcessWithTokenW` (launch under the operator token from a UAC-elevated helper; requires only `SeImpersonatePrivilege`; does not propagate handle inheritance) — *learn.microsoft.com/windows/win32/api/winbase/nf-winbase-createprocesswithtokenw*
- `WTSQueryUserToken` (obtain the interactive operator's token; requires `SeTcbPrivilege`/SYSTEM) — *learn.microsoft.com/windows/win32/api/wtsapi32/nf-wtsapi32-wtsqueryusertoken*
- `OpenProcessToken` + `DuplicateTokenEx` (elevated helper extracts the coordinator's token; higher-integrity subject may open a lower-integrity process) / Mandatory Integrity Control — *learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocesstoken* · *learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-duplicatetokenex* · *learn.microsoft.com/windows/win32/secauthz/mandatory-integrity-control*
- `CreateFileW` `FILE_FLAG_BACKUP_SEMANTICS` (required to open a directory handle) — *learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew* (Remarks: Directories)

---

## 30. Preguntas No Bloqueantes (Futuras Iteraciones)

1. **GP2.1 Dedicated Container Provisioner:** ¿Diseñar una utilidad que cree automáticamente una carpeta contenedor dedicada con permisos restringidos para albergar Golden Masters cuando el parent original sea `UNSAFE`? (Feature futura independiente).
2. **Compresión de Manifiesto en Storage:** ¿Comprimir con gzip los buffers Base64 de `NodeSecurityBackup` en instalaciones con más de 100.000 archivos para reducir el uso de disco del journal? (Optimización no funcional).
3. **Telemetría Estructurada de Tiempos de Helper:** ¿Medir tiempos de ejecución por lote de nodos en el helper para ajustar métricas de progreso en la UI de Sky-Claw? (Mejora de UX).

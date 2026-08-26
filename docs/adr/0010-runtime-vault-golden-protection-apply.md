# ADR 0010 — RV-GP2: Protect Golden / Golden Protection Apply

**Fecha:** 2026-08-26
**Estado:** Propuesta (design-only; prohibida la implementación de código de producción en este PR).
**Contexto de origen:** tarea RV-GP2 sobre `origin/main` `faa1317db3ca61d103343f3689be5f0859c2a56c` (post-merge de PR #508, RV-GP1).
**Alcance:** Diseño arquitectónico exclusivo de la capability mutadora transaccional de protección del Golden Master (`protect_golden_master`), su helper de elevación con privilegio mínimo, su journal/store transaccional aislado, su modelo de recuperación ante caídas (crash recovery) y su compatibilidad forward con GP3.
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

Sin embargo, si un Golden Master recién verificado se encuentra en estado `UNPROTECTED`, el sistema carece hoy de un mecanismo seguro, determinista y atómico para aplicar el blindaje ACL sin intervención manual propensa a errores.

La auditoría de lanzamiento (`docs/audits/2026-08-22_runtime_vault_mo2_stock_launch_audit.md`) demostró que procesos normales no elevados (Skyrim, MO2, plugins, herramientas del pipeline) realizan escrituras colaterales sobre carpetas de juego si el sistema de archivos lo permite.

**RV-GP2 ("Protect Golden")** es la capability mutadora encargada de transformar un Golden Master en estado `UNPROTECTED` (o `WRITE_PROTECTED` sub-óptimo) al estado objetivo verificado **`HARDENED`**, garantizando:
1. **Transaccionalidad y recuperabilidad:** captura previa exhaustiva de Security Descriptors (SD) por nodo antes de cualquier mutación;
2. **Elevación acotada con Privilegio Mínimo:** un helper privilegiado estrictamente confinado a un plan sellado con verificación cruzada anti-*Confused Deputy*;
3. **Cero mutación no planificada:** mitigación de propagación silenciosa de herencia (`SetNamedSecurityInfoW` / `PROTECTED_DACL_SECURITY_INFORMATION`);
4. **Verificación post-mutación de doble eje:** post-verificación de acceso GP1 (`state == HARDENED`) y post-verificación de integridad de contenido RV-2 (`TreeDigest` intacto y `VERIFIED`);
5. **Rollback automático y fail-closed:** restauración atómica ante cualquier fallo o inconsistencia intermedia.

---

## 2. Decisión

1. **Target de Estado:** El único estado final de éxito admisible para GP2 es **`HARDENED`** con `success == True` y `assurance_scope == "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"`. Un resultado final que termine en `WRITE_PROTECTED`, `UNPROTECTED`, `UNKNOWN` o `UNSUPPORTED` constituye un fallo de la operación y dispara rollback obligatorio.
2. **Mutación por Nodo Bottom-Up (Deepest Descendants First):** GP2 muta individualmente cada archivo y directorio del subárbol comenzando por las hojas más profundas y finalizando en la raíz del Golden (`root`). Cada nodo se configura explícitamente con `PROTECTED_DACL_SECURITY_INFORMATION`, desvinculando la herencia antes de que sus ancestros sean modificados, eliminando cualquier riesgo de propagación no planificada hacia descendientes abiertos.
3. **Parent Policy Normativa — Refuse to Apply:** GP2 v1 **NO modifica el directorio parent** contenedor del Golden. Si la inspección previa detecta que el parent inmediato presenta vectores de borrado/renombrado del root (`FILE_DELETE_CHILD`, cadena delete+create, `WRITE_DAC` o `WRITE_OWNER` escalable), GP2 se rehúsa terminantemente a aplicar (`PARENT_UNSAFE -> REFUSE_TO_APPLY`). Prohibido warning + continue; prohibido override HITL en v1.
4. **Respaldo Binario Exacto (Raw Self-Relative SD):** Antes de la primera mutación física, se genera un manifiesto sellado que almacena para cada nodo los bytes binarios exactos del `SECURITY_DESCRIPTOR` autoritativo en formato self-relative (`OWNER | GROUP | DACL` y bits de control `SE_DACL_PROTECTED`), con longitud validada mediante `GetSecurityDescriptorLength`. La cadena SDDL se preserva únicamente para diagnóstico/auditoría.
5. **Exclusión de SACL en v1:** GP2 v1 no lee, no muta y no promete restauración de SACLs (`SACL_CAPTURED=NO`, `SACL_MUTATED=NO`, `SACL_RESTORE_GUARANTEE=NO`). La garantía de restauración v1 cubre de forma exacta `OWNER`, `GROUP`, `DACL` y flags de control de herencia de DACL. No se solicita ni habilita `SeSecurityPrivilege`.
6. **Política de Owner — Preserve Owner by Default con Owner Rights ACE:** GP2 v1 **preserva el propietario original del objeto** (`PRESERVE_OWNER_BY_DEFAULT`). Para neutralizar el `WRITE_DAC` y `READ_CONTROL` implícito que Windows otorga por defecto al dueño del objeto, la Target DACL inyecta una ACE explícita del Well-Known SID **Owner Rights (`S-1-3-4`)** con máscara restrictiva de solo lectura.
7. **Target DACL Estricta y Explícita:** Se define una DACL allowlist universal sin ACEs deny innecesarias: Owner Rights (`S-1-3-4`) con `FILE_GENERIC_READ` (archivos) / `FILE_GENERIC_READ | FILE_TRAVERSE` (directorios); Principales estándar (`S-1-5-11` Authenticated Users / usuario actual) con lectura/ejecución; `S-1-5-18` LocalSystem y `S-1-5-32-544` Administrators con `FILE_ALL_ACCESS`.
8. **Helper Privilegiado Confinado (Anti-Confused Deputy):** La mutación física y el eventual rollback son ejecutados por un proceso helper elevado vía UAC (`ShellExecuteExW` con verbo `runas`). El helper no acepta rutas libres ni scripts arbitrarios; recibe exclusivamente `--operation-id <UUID>` y `--manifest-digest <SHA-256>`. El helper revalida de manera autónoma la identidad del volumen, confinamiento estricto bajo el root, ausencia total de reparse points, identidad de nodo `(VolumeSerialNumber, FileId)` y coincidencia del digest del SD PRE antes de mutar cada nodo.
9. **Journal Transaccional Propio Aislado:** La máquina de estados de GP2 persiste sus transiciones y el estado de cada nodo en un almacén propio (`sky_claw/local/runtime_vault/protection_journal.py`), completamente desacoplado de `sky_claw/app/db/journal.py` (aislamiento estricto del issue #506).
10. **Doble Post-Verificación Independiente:** Tras finalizar el helper, el proceso principal (no elevado) ejecuta de forma obligatoria:
    - GP1 `inspect_golden_protection(golden_path)` exigiendo `state == HARDENED` y `success == True`.
    - RV-2 `verify_golden_master(...)` exigiendo que el `TreeDigest` y los archivos críticos permanezcan `VERIFIED` e idénticos al baseline.
11. **Rollback Top-Down vs GP3:** Si ocurre un fallo en cualquier etapa, el helper ejecuta un rollback transaccional restaurando los SDs originales en orden inverso (**Top-Down: Root primero, descendientes después**). Este rollback es un mecanismo de recuperación interna ante fallos y es conceptual y contractualmente distinto de **GP3** (capability futura de desprotección a demanda del operador).

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

## 5. Threat Model

### 5.1 Actores y Alcance

| Actor | Contexto / Token | Cobertura GP2 |
|---|---|---|
| **Actor A** | Proceso normal de usuario (Skyrim, MO2, plugins DLL, scripts de modding, Sky-Claw sin elevación). Token interactivo estándar. | **Objetivo Primario:** Bloqueo total de escritura, append, delete, rename, cambio de atributos, cambio de DACL y cambio de owner. |
| **Actor B** | Usuario miembro de Administrators ejecutando con **token filtrado** (UAC activo). SIDs admin marcados deny-only, privilegios elevados deshabilitados. | **Objetivo Primario:** Idem Actor A. Los SIDs deny-only no conceden permisos de mutación. |
| **Actor C** | Proceso elevado tras consentimiento UAC legítimo. | **Vector Autorizado:** Utilizado exclusivamente por el helper GP2 para aplicar la configuración. Fuera de la garantía de bloqueo post-aplicación. |
| **Actor D** | Malware no privilegiado o proceso hostil en espacio de usuario. | **Objetivo de Seguridad:** Intento de invocar el helper GP2 como *Confused Deputy* para alterar archivos ajenos o corromper el Golden. Mitigado por el confinamiento estricto del helper (§12). |
| **Actor E** | SYSTEM / Administrador malicioso persistente. | **Fuera de Alcance:** Todo usuario con privilegios de kernel o token SYSTEM puede saltarse ACLs NTFS. Se declara explícitamente. |

### 5.2 Vectores Específicos de Ataque y Mitigaciones en GP2

```text
Amenaza: Confused Deputy en Helper UAC
Mitigación: Plan sellado + digest SHA-256 + Revalidación estricta de FileId y Pre-SD

Amenaza: Mutación no planificada por herencia
Mitigación: Bottom-Up Apply + PROTECTED_DACL_SECURITY_INFORMATION por nodo

Amenaza: Borrado/Renombrado vía Parent Unsafe
Mitigación: Refuse to Apply si parent permite DELETE_CHILD o WRITE_DAC

Amenaza: Dueño reescribe DACL implícitamente
Mitigación: Inyección explícita de Owner Rights S-1-3-4 con solo lectura

Amenaza: Reparse Point Swap / TOCTOU
Mitigación: Revalidación canónica link_kind_and_identity_or_raise antes de cada mutación
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
- **Control Bits:** `SE_DACL_PROTECTED` (`0x1000`) activado en cada nodo (vía flag `PROTECTED_DACL_SECURITY_INFORMATION` en `SetNamedSecurityInfoW`).
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
- Si durante la preparación del plan se detecta un nodo cuyo `owner_sid` es corrupto, no resoluble a SID string o no presente en el SD, la fase de planificación falla con `PLAN_CREATION_FAILED`.
- GP2 v1 no intenta transferir la propiedad a `TrustedInstaller` ni a `SYSTEM`, eliminando la necesidad de adquirir o retener `SeTakeOwnershipPrivilege` o `SeRestorePrivilege` durante la operación normal de protección.

---

## 9. SD Backup / Restore ABI

Para garantizar una restauración byte-exacta ante fallos (rollback) o futura desprotección (GP3), se define el ABI de respaldo binario por nodo:

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
1. **Captura Autoritativa:** La fuente primaria y autoritativa para el restore son **exclusivamente los bytes binarios `pre_sd_bytes`**. `sddl_diagnostic` es solo para logs de auditoría y no se utiliza como oráculo de restauración.
2. **Obtención Segura de Longitud:** La longitud del buffer se valida mediante la API Win32 `GetSecurityDescriptorLength(sd_ptr)`.
3. **Formato Self-Relative:** Todo descriptor capturado se normaliza a self-relative (`MakeSelfRelativeSD` si fuera necesario o `GetNamedSecurityInfoW` que devuelve buffers contiguos).
4. **SACL Excluida:**
   - `SACL_CAPTURED = NO`
   - `SACL_MUTATED = NO`
   - `SACL_RESTORE_GUARANTEE = NO`
   - La garantía v1 se enuncia estrictamente como: *Restauración exacta del estado PRE de OWNER, GROUP, DACL y flags de control de herencia de DACL para cada nodo intervenido*.

---

## 10. Inventory & Sealing (Pre-Mutation Manifest)

Antes de cualquier mutación en el filesystem, GP2 construye y sella el **Plan de Protección** (`GoldenProtectionPlan`):

### 10.1 Fases del Sellado
1. **PRE-INVENTORY:** Recorrido exhaustivo del árbol con detección estricta de reparse points (`link_kind_and_identity_or_raise`).
2. **SECURITY CAPTURE:** Lectura de `NodeSecurityBackup` para cada nodo del subárbol.
3. **IDENTITY ANCHOR:** Vinculación de cada nodo con su `(VolumeSerialNumber, FileId)` obtenido mediante `GetFileInformationByHandle`.
4. **TARGET DACL SYNTHESIS:** Construcción de la Target DACL y pre-cálculo del `planned_post_sd_sha256`.
5. **POST-INVENTORY STRUCTURAL PASS:** Re-verificación estructural completa (sin drift de archivos, mtime ni identidad). Si el árbol muta durante la captura -> `InventoryError` -> aborto inmediato sin mutar nada.
6. **CANONICAL MANIFEST WRITING:** Serialización determinista del manifiesto en JSON canónico (claves ordenadas, sin espacios redundantes, UTF-8).
7. **MANIFEST DIGEST COMPUTATION:** Cálculo del `manifest_digest = sha256(manifest_bytes)`.

El manifiesto queda inmutable en disco bajo el almacén de operaciones del Runtime Vault antes de invocar la elevación UAC.

---

## 11. Trust & Authentication Model

### 11.1 Separación de Conceptos: Integridad vs Autenticidad
- **Integridad de Corrupción (`CORRUPTION_INTEGRITY`):** El hash SHA-256 del manifiesto detecta alteraciones accidentales o corrupción en disco.
- **Autenticidad y Anti-Tamper (`AUTHENTICITY`):** Un hash SHA-256 no impide que un atacante con permisos de escritura reemplace el archivo de manifiesto y recalcule el hash.

### 11.2 Root of Trust en GP2 v1
En GP2 v1, la raíz de confianza contra reemplazo de manifiesto descansa en:
1. **Ubicación en Store Protegido por SO:** El manifiesto se escribe en `%LOCALAPPDATA%\Sky-Claw\runtime_vault\operations\<op_id>\manifest.json`. Este directorio hereda las ACLs del perfil del usuario (restringido al usuario actual y Administrators).
2. **Paso Explícito de Parámetros en UAC:** El proceso coordinador pasa a la línea de comando del helper UAC:
   `--operation-id <UUID> --manifest-digest <SHA256>`
   El prompt UAC de Windows presenta al usuario el ejecutable y el consentimiento de elevación.
3. **Revalidación Autónoma del Helper (Defense in Depth):** Aunque un atacante local pudiera alterar el manifiesto en disco, el helper elevado revalida de manera estricta que todas las rutas residan dentro del volumen y directorio raíz del Golden verificado, y que todos los `FileId` y `pre_sd_sha256` coincidan con el estado físico real del disco antes de tocar una sola ACL.

---

## 12. Privileged Helper Boundary & Confused Deputy Mitigation

El helper elevado (`sky-claw-vault-helper.exe` o invocación controlada de worker elevado) constituye la frontera de mayor privilegio del sistema.

### 12.1 Restricciones Inviolables del Helper
- **NO acepta rutas arbitrarias:** El helper no recibe `--path <ruta>` para mutar. Solo recibe `--operation-id` y `--manifest-digest`.
- **NO acepta scripts ni comandos dinámicos:** La lógica de mutación está rígidamente compilada/empaquetada.
- **NO sigue reparse points:** Si cualquier nodo es o se convierte en junction/symlink -> aborto inmediato.
- **NO cruza límites de volumen:** Todas las operaciones deben pertenecer al mismo `VolumeSerialNumber`.

### 12.2 Algoritmo de Ejecución del Helper por Nodo

```text
Para cada nodo en el plan (en orden de aplicación):
    1. Abrir handle seguro (FILE_FLAG_BACKUP_SEMANTICS, sin seguir enlaces).
    2. Verificar VolumeSerialNumber y FileId contra el plan -> si difiere: FAIL_CLOSED.
    3. Leer SD actual y verificar sha256(current_sd) == expected_pre_sd_sha256 -> si difiere: FAIL_CLOSED.
    4. Aplicar Target DACL con SetNamedSecurityInfoW(..., PROTECTED_DACL_SECURITY_INFORMATION).
    5. Re-leer SD inmediatamente tras mutación y verificar sha256(new_sd) == planned_post_sd_sha256.
    6. Registrar nodo como MUTATED en el journal de operación.
    Si cualquier paso falla:
        Interrumpir ciclo inmediatamente -> Iniciar secuencia de ROLLBACK.
```

---

## 13. UAC & Human-in-the-Loop (HITL)

1. **Consentimiento del SO Exclusivo:** La autorización privilegiada se solicita **únicamente** a través del mecanismo nativo de UAC de Windows (`ShellExecuteExW` con verbo `"runas"`).
2. **Prohibición Absoluta de Credenciales Propias:** Sky-Claw **nunca** solicitará, almacenará, parseará ni enviará contraseñas ni credenciales de usuario.
3. **HITL Coordinador Previo:** Antes de invocar UAC, la interfaz de Sky-Claw (GUI o CLI) presentará al operador la solicitud formal de confirmación con el resumen estructurado:
   - Ruta física del Golden Master;
   - Número total de archivos y carpetas a proteger;
   - Estado previo observado (ej. `UNPROTECTED`);
   - Advertencia explícita de que se requerirá confirmación en la ventana de control de cuentas de usuario (UAC).

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

## 15. Reparse Policy

- **Tolerancia Cero:** La presencia de cualquier reparse tag (`st_reparse_tag != 0`), symlink, junction, volume mount point o placeholder en el root, en cualquier nodo descendiente o en el parent inmediato produce la denegación inmediata de la operación (`REFUSE_TO_APPLY`).
- **Primitiva Única:** La detección utiliza exclusivamente `sky_claw.app.security.links.link_kind_and_identity_or_raise`. Prohibido reimplementar inspecciones ad-hoc.
- **Race Condition Gate:** Si un archivo o directorio es reemplazado por un enlace entre la fase de planificación y la ejecución del helper, el chequeo de `FileId` y `reparse_tag` del helper detecta la colisión antes de mutar y aborta la transacción.

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
3. **Determinismo:** El rollback procesa únicamente los nodos que fueron efectivamente mutados (registrados en el journal), ordenados por profundidad creciente (`len(parts)` asc) y secundariamente por orden lexicográfico posix.

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
- Toda activación de privilegios (ej. `SeRestorePrivilege`) se realiza inmediatamente antes de la llamada a `SetNamedSecurityInfoW` y se deshabilita inmediatamente después en un bloque de guarda `finally`.
- La función de habilitación valida explícitamente `GetLastError() != ERROR_NOT_ALL_ASSIGNED`. Si el privilegio no fue asignado, la operación falla cerrado.
- Se preserva y restaura la estructura `TOKEN_PRIVILEGES.PreviousState`.

---

## 19. Transaction FSM & Journal Ownership

Para evitar cualquier interferencia con el trabajo paralelo del Issue #506 en `sky_claw/app/db/journal.py`, GP2 implementa un almacén transaccional exclusivo en `sky_claw/local/runtime_vault/protection_journal.py`.

```text
PREPARING -> PREPARED -> AWAITING_ELEVATION -> APPLYING -> VERIFYING_GP1 -> VERIFYING_RV2 -> COMMITTED
   |            |              |                 |               |               |
   v            v              v                 v               v               v
FAILED      CANCELLED      REJECTED         ROLLBACK_REQ    ROLLBACK_REQ    ROLLBACK_REQ
                                                 |               |               |
                                                 +---------------+---------------+
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
| **C1: Durante PREPARING** | Manifiesto ausente o incompleto | `None` / Huérfano | Limpieza de temporales. Estado no modificado. | Sí | No |
| **C2: Manifiesto sellado (PREPARED)** | Manifiesto íntegro, journal `PREPARED`, 0 nodos mutados | `PREPARED` | Operación cancelable sin efectos en FS. | Sí | No |
| **C3: UAC aceptado, antes del Nodo 1** | Journal `APPLYING`, lista `mutated_nodes` vacía | `APPLYING` (0 nodes) | Marcar como cancelado; ningún nodo fue alterado. | Sí | No |
| **C4: Durante mutación de Nodo K (1..N)** | Journal `APPLYING`, nodos 1..K-1 marcados `MUTATED` | `ROLLBACK_REQUIRED` | Invocar helper para rollback Top-Down de nodos 1..K-1. | No (exige UAC) | Sí (UAC para rollback) |
| **C5: Helper finalizado, antes de GP1** | Journal `APPLYING` completo (N nodos mutados) | `APPLYING` | Proceso principal retoma en `VERIFYING_GP1`. | Sí | No |
| **C6: Durante verificación GP1/RV2** | Journal `VERIFYING_*`, N nodos mutados | `VERIFYING_*` | Re-ejecutar verificaciones read-only. Si pasan -> `COMMITTED`; si fallan -> `ROLLBACK_REQUIRED`. | Sí | No |
| **C7: Tras verificación exitosa, antes de persistir COMMITTED** | Verificaciones en memoria, journal aún `VERIFYING_RV2` | `VERIFYING_RV2` | Re-verificar read-only y transicionar a `COMMITTED`. | Sí | No |
| **C8: Durante ejecución de Rollback** | Journal `ROLLING_BACK`, lista parcial de restaurados | `ROLLING_BACK` | Helper retoma rollback para los nodos restantes pendientes de restauración. | No (exige UAC) | Sí (UAC) |
| **C9: Sustitución de nodo / Inconsistencia FileId en Rollback** | Discrepancia entre `FileId` actual y backup | `INDETERMINATE` | Detención inmediata. Emisión de informe forense. No aplicar SD a archivo desconocido. | No | Sí (Alerta Crítica) |

---

## 21. GP1 Post-Verification (Requisito Normativo)

Inmediatamente después de que el helper completa la aplicación de cambios:
1. El proceso coordinador ejecuta `inspect_golden_protection(golden_path)`.
2. Se exige de forma estricta:
   - `result.success is True`
   - `result.state is GoldenProtectionState.HARDENED`
   - `result.evidence.pre_post_structural_match is True`
   - `result.assurance_scope == "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"`
3. Si el resultado es `WRITE_PROTECTED`, `UNPROTECTED`, `UNKNOWN` o `UNSUPPORTED`, la operación se considera fallida y se transiciona a `ROLLBACK_REQUIRED`.

---

## 22. RV-2 Post-Verification (Requisito Normativo)

Inmediatamente tras validar GP1:
1. El proceso coordinador ejecuta `verify_golden_master(golden_path, expected_tree=baseline_tree, expected_runtime=baseline_runtime, critical_expectations=...)`.
2. Se exige de forma estricta:
   - `result.success is True`
   - `result.state is VerificationState.VERIFIED`
   - `result.descriptor.tree_digest == baseline_tree` (garantía de que ningún byte de contenido ni tamaño fue alterado).
3. Si el hash o estructura del árbol cambió, se transiciona a `ROLLBACK_REQUIRED` y se registra una alerta crítica de integridad.

---

## 23. GP3 Compatibility (Forward Compatibility)

GP2 se diseña para garantizar plena compatibilidad con la futura capability GP3 (Unprotect / Restore a demanda):
1. **Persistencia Permanente del Backup:** Tras alcanzar el estado `COMMITTED`, el manifiesto de respaldo (`manifest.json`) se archiva de forma durable en el almacén de seguridad del Golden (`%LOCALAPPDATA%\Sky-Claw\runtime_vault\golden_backups\<golden_digest>\protection_manifest.json`).
2. **Formato Unificado:** GP3 utilizará exactamente la misma estructura `NodeSecurityBackup` y el mismo algoritmo de aplicación Top-Down para restaurar el estado original cuando el usuario lo solicite.
3. **No-Degradación:** GP3 exigirá que el Golden se encuentre en estado `VERIFIED` antes y después de desproteger.

---

## 24. Taxonomía de Errores

| Error de Dominio | Causa / Condición | Consecuencia |
|---|---|---|
| `ProtectionPrecheckError` | Path inválido, no absoluto, no directorio, o plataforma no-Windows | Aborto inmediato sin iniciar transacción |
| `UnsafeParentError` | Parent con `DELETE_CHILD`, `WRITE_DAC` o rename viable | `REFUSE_TO_APPLY` (sin mutación, sin UAC) |
| `PlanCreationError` | Error al leer SDs originales, reparse point detectado, o drift PRE/POST | `FAILED_PREPARATION` (sin mutación) |
| `ElevationRejectedError` | Usuario canceló o denegó el prompt de UAC | `ELEVATION_REJECTED` (sin mutación) |
| `HelperExecutionError` | Helper falló durante la mutación de nodos | Transición a `ROLLBACK_REQUIRED` |
| `PostVerificationError` | GP1 no emitió `HARDENED` o RV2 no emitió `VERIFIED` | Transición a `ROLLBACK_REQUIRED` |
| `RollbackFailureError` | Helper falló durante la restauración de SDs PRE | Transición a `ROLLBACK_FAILED` / `INDETERMINATE` |

---

## 25. Estrategia de Tests Futuros (Plan para Implementación)

Los tests de GP2 se implementarán en módulos dedicados bajo `tests/`:
- `tests/test_runtime_vault_protection_apply_plan.py` (Unit tests de planificación y sellado).
- `tests/test_runtime_vault_protection_apply_fsm.py` (Unit tests de la máquina de estados y crash recovery).
- `tests/test_runtime_vault_protection_apply_integration.py` (Tests de integración en Windows TEMP únicamente, sin tocar Golden real).

### Casos de Test Obligatorios:
- **GP2-T01:** Planificación exitosa genera manifiesto canónico con SDs self-relative y FileIds.
- **GP2-T02:** Reparse point en cualquier nodo del árbol aborta la planificación (`REFUSE_TO_APPLY`).
- **GP2-T03:** Parent con `FILE_DELETE_CHILD` aborta la planificación (`UnsafeParentError`).
- **GP2-T04:** Parent con `WRITE_DAC` aborta la planificación (`UnsafeParentError`).
- **GP2-T05:** Parent seguro permite generar plan con orden Bottom-Up estricto.
- **GP2-T06:** Target DACL contiene exactamente la Owner Rights ACE `S-1-3-4` con `FILE_GENERIC_READ`.
- **GP2-T07:** Target DACL no contiene ACEs de denegación ni flags de herencia.
- **GP2-T08:** Helper revalida `FileId` y `pre_sd_sha256` antes de mutar cada nodo.
- **GP2-T09:** Helper detecta reemplazo de archivo por symlink en tiempo de ejecución y aborta.
- **GP2-T10:** Mutación exitosa de árbol temporal culmina en GP1 `HARDENED` y RV-2 `VERIFIED`.
- **GP2-T11:** Fallo simulado en el nodo intermedio K dispara rollback Top-Down automático.
- **GP2-T12:** Rollback restaura exactamente los SDs originales byte a byte.
- **GP2-T13:** Fallo en post-verificación GP1 dispara rollback automático.
- **GP2-T14:** Fallo en post-verificación RV-2 (digest alterado) dispara rollback automático.
- **GP2-T15:** Simulación de crash en cada estado del FSM recupera la acción segura correcta.
- **GP2-T16:** AST Anchor: `protection_journal.py` no importa `sky_claw.app.db.journal`.
- **GP2-T17:** AST Anchor: El módulo no utiliza strings de cuentas localizadas ("Todos", "Administradores"), solo SIDs.

---

## 26. Matriz de Mutantes Causales

| ID | Mutante Introducido | Detección / Test Esperado |
|---|---|---|
| **M01** | Omitir backup de SD en un nodo | Fallo en validación de completitud del manifiesto |
| **M02** | Mutar filesystem antes de sellar el manifiesto | Fallo en FSM (estado no es `PREPARED`) |
| **M03** | Aceptar drift de identidad (`FileId` distinto) | Revalidación del helper aborta con `IdentityMismatchError` |
| **M04** | Aceptar drift de descriptor PRE (`pre_sd_sha256` distinto) | Revalidación del helper aborta con `DescriptorDriftError` |
| **M05** | Parent inseguro emite advertencia y continúa | Fallo en test de parent policy (`UnsafeParentError` no lanzado) |
| **M06** | Omitir un archivo descendiente en el plan | GP1 post-scan exhaustivo detecta nodo no protegido -> `UNPROTECTED` -> Rollback |
| **M07** | Orden de aplicación Top-Down en lugar de Bottom-Up | Test de orden de mutación detecta inversión de secuencia |
| **M08** | Permitir propagación de herencia no observada (`SE_DACL_PROTECTED` ausente) | Post-validation de nodo falla por digest post no coincidente |
| **M09** | Declarar éxito antes de ejecutar GP1 | Test de contrato GP2 exige `post_gp1_result` verificado |
| **M10** | Declarar éxito cuando GP1 devuelve `UNKNOWN` | Contrato exige `state == HARDENED`; `UNKNOWN` dispara rollback |
| **M11** | Declarar éxito cuando GP1 devuelve `WRITE_PROTECTED` | Contrato exige `state == HARDENED`; `WRITE_PROTECTED` dispara rollback |
| **M12** | Omitir verificación RV-2 post-mutación | Test de contrato GP2 exige `post_rv2_result` verificado |
| **M13** | Perder registro de nodos mutados en crash | Journal persistido antes de retornar cada mutación |
| **M14** | Reset recursivo de ACLs desde el root en rollback | Test de rollback valida que cada nodo recibe su SD individual |
| **M15** | Seguir reparse point durante inventario o aplicación | Primitiva canónica lanza `InventoryLinkError` / Helper aborta |
| **M16** | Helper acepta manifiesto no sellado | Helper exige coincidencia con `--manifest-digest` |
| **M17** | Helper acepta manifiesto con hash discrepante | Helper aborta con `ManifestDigestMismatchError` |
| **M18** | Helper acepta ruta arbitraria por CLI | CLI de helper solo acepta `--operation-id` y `--manifest-digest` |
| **M19** | Helper acepta root fuera de la configuración del Vault | Helper confina root a rutas registradas |
| **M20** | Helper ejecuta comando shell arbitrario | Helper solo implementa el bucle cerrado de mutación de SD |
| **M21** | Habilitar privilegios permanentemente en el token | Guarda `finally` desactiva privilegios inmediatamente |
| **M22** | Ignorar `ERROR_NOT_ALL_ASSIGNED` en `AdjustTokenPrivileges` | Verificación explícita de `GetLastError()` lanza error |
| **M23** | Omitir restauración de `PreviousState` de privilegios | Bloque de limpieza restaura estado previo |
| **M24** | HMAC derivado de manifiesto atacable considerado autenticación | Modelo de confianza declara formalmente los límites de SHA-256 |
| **M25** | Continuar tras fallo de `SetNamedSecurityInfoW` en un nodo | Helper detiene bucle y transiciona a rollback |
| **M26** | Restaurar SD sobre un nodo que cambió de `FileId` | Helper aborta rollback en ese nodo y declara `INDETERMINATE` |
| **M27** | Permitir override HITL ante parent inseguro | Contrato v1 no expone parámetro de bypass |

---

## 27. Performance

- **Complejidad Temporal:** $O(N)$ donde $N$ es el número total de nodos en el subárbol.
- **Rendimiento Esperado:** En el rig de referencia de Skyrim SE (~16.000 archivos), la captura y cálculo del plan insume < 1,5 segundos; la aplicación de `SetNamedSecurityInfoW` por el helper insume < 3,0 segundos en discos SSD NVMe.
- **I/O Bloqueante Controlado:** Todas las operaciones de I/O en el proceso principal se ejecutan sincrónicamente en funciones puras y se envuelven en `asyncio.to_thread` en la capa de orquestación, siguiendo el patrón del repositorio.

---

## 28. Limitaciones de Seguridad Declaradas

1. **Compromiso Administrativo / SYSTEM:** Un administrador malicioso con elevación UAC o un proceso ejecutándose como `NT AUTHORITY\SYSTEM` puede en cualquier momento otorgarse `SeTakeOwnershipPrivilege` o `SeRestorePrivilege`, reescribir las ACLs y mutar el Golden Master. La garantía de `HARDENED` aplica frente al **token interactivo normal no elevado**.
2. **No Atomicidad a Nivel de Sistema de Archivos:** NTFS no provee transacciones multi-archivo (tras la obsolescencia de TxF). La atomicidad de GP2 es de nivel aplicativo mediante journal y rollback compensatorio. Un fallo catastrófico de energía durante una mutación individual puede requerir el análisis del journal en el siguiente arranque.
3. **Restricción de Directorio Contenedor (Parent):** Si el Golden Master reside en una carpeta donde el usuario tiene permisos amplios de borrado de hijos (`FILE_DELETE_CHILD`), la protección no puede aplicarse hasta que el Golden sea reubicado en una ruta segura.

---

## 29. Referencias Primarias Microsoft

- [MS-DTYP] §2.4.6: Security Descriptor Description and Structure — *learn.microsoft.com/openspecs/windows_protocols/ms-dtyp/7d4dac05-9cef-4563-a058-f108abec36d4*
- [MS-ADTS] §6.1.3.4: Blocking Implicit Owner Rights — *learn.microsoft.com/openspecs/windows_protocols/ms-adts/fb7c101d-ec8b-4fbf-bca8-7d7c2d747d0c*
- `SetNamedSecurityInfoW` function — *learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow*
- `GetSecurityDescriptorLength` function — *learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-getsecuritydescriptorlength*
- `GetSecurityDescriptorControl` function — *learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-getsecuritydescriptorcontrol*
- `AdjustTokenPrivileges` function — *learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-adjusttokenprivileges*
- Well-known SIDs / Owner Rights SID (`S-1-3-4`) — *learn.microsoft.com/windows/win32/secauthz/well-known-sids*
- Order of ACEs in a DACL — *learn.microsoft.com/windows/win32/secauthz/order-of-aces-in-a-dacl*
- File Access Rights Constants — *learn.microsoft.com/windows/win32/fileio/file-access-rights-constants*

---

## 30. Preguntas No Bloqueantes (Futuras Iteraciones)

1. **GP2.1 Dedicated Container Provisioner:** ¿Diseñar una utilidad que cree automáticamente una carpeta contenedor dedicada con permisos restringidos para albergar Golden Masters cuando el parent original sea `UNSAFE`? (Feature futura independiente).
2. **Compresión de Manifiesto en Storage:** ¿Comprimir con gzip los buffers Base64 de `NodeSecurityBackup` en instalaciones con más de 100.000 archivos para reducir el uso de disco del journal? (Optimización no funcional).
3. **Telemetría Estructurada de Tiempos de Helper:** ¿Medir tiempos de ejecución por lote de nodos en el helper para ajustar métricas de progreso en la UI de Sky-Claw? (Mejora de UX).

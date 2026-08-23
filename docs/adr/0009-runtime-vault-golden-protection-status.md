# ADR 0009 — RV-GP1: Estado de protección filesystem del Golden Master

**Fecha:** 2026-08-23
**Estado:** Propuesta (design-only; prohibida la implementación en este PR)
**Contexto de origen:** tarea RV-GP1 sobre `origin/main` `da017348fb545626c8b6f853ef9c16738882a9a1`
(merge de PR #502, RV-3). Trabajo exclusivamente read-only: sin modificar Golden, Runtime,
ACLs, Steam ni MO2; sin pruebas mutadoras sobre el filesystem real; sin elevación.

---

## 1. Contexto

El Runtime Vault ya define tres capas verificadas:

| Capa | Qué afirma | Dónde vive |
|---|---|---|
| RV-1 | Identidad de runtime y de árbol: `(relpath, size, sha256)` por archivo + `(digest, files, bytes)` agregado | `sky_claw/local/runtime_vault/{models,verification,inventory}.py` |
| RV-2 | Elevación de un candidato a Golden Master VERIFIED con evidencia independiente | `sky_claw/local/runtime_vault/golden.py` |
| RV-3 | Clon operativo independiente del Golden (`create_runtime_clone`) | `sky_claw/local/runtime_vault/clone.py` |

Lo que ninguna capa afirma es una pregunta distinta: **¿quién puede mutar físicamente el
Golden?** `GoldenMasterDescriptor.role == "reference_only"` es hoy una convención de datos;
no existe mecanismo que observe ni garantice nada a nivel filesystem.

La amenaza no es hipotética en este rig: la auditoría de lanzamiento
[`docs/audits/2026-08-22_runtime_vault_mo2_stock_launch_audit.md`](../audits/2026-08-22_runtime_vault_mo2_stock_launch_audit.md)
preservó evidencia de **escrituras físicas observables dentro del directorio Stock durante
lanzamientos MO2/Skyrim**, más divergencias SHA-256 post-lanzamiento de causalidad no
demostrada. El modelo de amenaza dominante es, por lo tanto, *mutación accidental o colateral
desde procesos normales*, no un atacante administrativo deliberado.

Evidencia empírica previa del Golden protegido (registrada por el operador; esta tarea NO
repite esas pruebas mutadoras): contenido y tree digest VERIFIED; escritura/creación/borrado/
renombrado DENIED para el token normal; lectura/hash/copia ALLOWED.

Necesidad: una capability de **inspección read-only** —futura función
`inspect_golden_protection(path)`— que clasifique el estado de protección sin escribir nada.
Este ADR define su contrato antes de permitir cualquier implementación.

### Nomenclatura

Existe un roadmap previo RV-1..RV-5 donde **RV-4 está reservado para "Runtime registry /
drift" y RV-5 para "MO2 Runtime integration"**. Esta capability NO reutiliza esos números.
Se registra el prefijo nuevo:

- **RV-GP1** — Golden Protection Status (este ADR).
- **RV-GP2** — Protect Golden (futuro, MUTATING).
- **RV-GP3** — Restore / Unprotect Golden (futuro, MUTATING).

No existe archivo de roadmap en el repo que registrar (verificado por búsqueda); la
nomenclatura queda anclada acá, dentro del ADR, evitando la colisión con RV-4.

## 2. Decisión

1. **Read-only primero.** La inspección NUNCA crea/escribe/borra/renombra nada dentro del
   Golden ni ejecuta probes activos (`ACTIVE_WRITE_PROBE_REQUIRED=NO`, justificado en §6).
2. **Fail closed.** Evidencia ausente o ambigua produce UNKNOWN; nunca se promueve a un
   estado protegido por omisión (`UNKNOWN != WRITE_PROTECTED != HARDENED`, mismo principio
   que `UNKNOWN != VERIFIED` en RV-1).
3. **Backend Windows v1 = AccessCheck nativo + introspección de token**, vía `ctypes` sobre
   advapi32/kernel32. Sin dependencia Python nueva, sin subprocess, sin parseo de salida
   localizada (análisis completo en §7).
4. **Cinco estados** con definiciones operativas y evidencia objetiva exigente (§5):
   `UNSUPPORTED`, `UNPROTECTED`, `WRITE_PROTECTED`, `HARDENED`, `UNKNOWN`.
5. **HARDENED elegido = OPTION A** (§9): endurecido frente al token interactivo actual no
   elevado, demostrable al 100% read-only, con threat model explícito y límites honestos.
6. **Ortogonalidad total** respecto de RV-1/RV-2/RV-3 (§11): la protección no autoriza ni
   bloquea clonación, ni degrada verificación, ni depende de la versión del juego.
7. **Semántica success/state desacoplada** (§10): `success=True` con `UNPROTECTED` es válido
   (la inspección tuvo éxito y descubrió falta de protección).

## 3. Non-goals

- No implementa GP2 (proteger) ni GP3 (restaurar); sólo registra sus restricciones (§15).
- No implementa el Execution Guard (`reference_only` no seleccionable como target
  operativo): feature futura independiente, fuera de GP1 y de RV-3 (§15).
- No modifica `GoldenMasterDescriptor`, `GoldenMasterVerificationResult`,
  `RuntimeCloneResult` ni ningún contrato existente de Runtime Vault.
- No promete resistencia contra administrador/SYSTEM completamente comprometidos (§14).
- No exige NTFS como requisito de Runtime Vault: un filesystem sin backend declarable
  produce `UNSUPPORTED` y **no bloquea** verification/clone (§8).
- No agrega dependencias nuevas (ni pywin32 ni herramientas CLI).

## 4. Threat model

La regla del repo prohíbe frases ambiguas como "el usuario no puede escribir". Todo juicio
de acceso se enuncia contra un **token de seguridad concreto**: el evaluado por el inspector
es siempre el *current effective token* del proceso Sky-Claw llamador, tal como existe en el
momento de la inspección. Actores:

| # | Actor | Descripción | Cobertura GP1 |
|---|---|---|---|
| A | Proceso normal bajo token interactivo no elevado | El caso dominante: MO2, Skyrim, herramientas de pipeline, el propio Sky-Claw sin elevación | **Objetivo principal**: su capacidad efectiva es exactamente lo que GP1 mide |
| B | Miembro de Administrators con token filtrado (Admin Approval Mode) | Al loguearse, Windows crea dos tokens; el estándar/filtrado remueve privilegios administrativos y marca los SIDs admin como deny-only [^uac]. Un proceso hijo hereda el token del padre [^uac] | Cubierto por construcción: GP1 evalúa el token REAL del proceso; los SIDs deny-only no conceden acceso [^accesscheck] |
| C | El mismo usuario tras aprobación UAC | Token completo (elevado) con privilegios administrativos restaurados | **Explícitamente fuera de la garantía**. Que B pueda elevarse a C no convierte un estado protegido en mentira: el estado describe el token evaluado |
| D | Usuario estándar sin credenciales administrativas | Sólo posee token estándar; la elevación requiere credenciales de admin (prompt de credenciales) [^uac] | Cubierto igual que A (mismo mecanismo de medición) |
| E | SYSTEM / servicio privilegiado | Fuera del alcance de cualquier DACL razonable | **Fuera de alcance como atacante**: no se afirma protección contra él |
| F | Otro proceso ya privilegiado | Ej.: instalador elevado, antivirus | Fuera de alcance (idem E) |

Qué pretende resolver Golden Protection (GP1+GP2+GP3): **blindar el Golden frente a la
ejecución accidental o colateral de procesos normales no elevados** (actores A/B/D),
preservando lectura/hash/copia para las operaciones legítimas del vault. NTFS DACL **no
puede** proteger contra un administrador/SYSTEM completamente comprometido (C/E/F): dueño
del objeto o portador de privilegios de bypass puede reescribir la protección [^ontt] [^ifspriv].
Este límite se declara, no se oculta.

### Tabla de escenarios y falsos verdes

DELETE/RENAME se analizan considerando semántica del directorio padre: borrar requiere
`DELETE` sobre el objeto **o** `FILE_DELETE_CHILD` sobre el directorio que lo contiene
(incluye read-only files) [^filear]; renombrar exige además derecho de creación en el padre
destino (`FILE_ADD_FILE`/`FILE_ADD_SUBDIRECTORY`) [^filear]. La fuente primaria confirma que
GetEffectiveRightsFromAcl **no ve** estos derechos provistos por el padre [^gerfa], razón
más para usar AccessCheck sobre la superficie completa {root, parent}.

| Escenario | Evidencia observada | Estado | Confianza | Riesgo falso verde |
|---|---|---|---|---|
| Usuario estándar, sólo lectura efectiva en root+parent | AccessCheck: sin derechos de mutación; sin privilegios bypass | WRITE_PROTECTED | Alta | Bajo: medido sobre token real, no inferido de ACEs |
| Admin filtrado (B), DACL restrictiva | SIDs Administrators deny-only no conceden; mutación denegada; owner no atribuible | WRITE_PROTECTED o HARDENED (según owner/elevación) | Alta | Bajo: deny-only es comportamiento del sistema, no parsing |
| Admin elevado (C) ejecutando Sky-Claw | Token Full; típicamente derechos amplios o privilegio bypass presente | UNPROTECTED | Alta | Ninguno: nunca HARDENED con token elevado (regla dura) |
| Owner = SID del usuario actual | Owner atribuible al token → camino implícito READ_CONTROL+WRITE_DAC del dueño [^msadts] [^ontt] | No HARDENED (WRITE_PROTECTED si la mutación de contenido está cerrada) | Alta | Ninguno: el check de owner es comparación de SIDs del token |
| Owner = Administrators/SYSTEM/TrustedInstaller | Owner no atribuible al token evaluado | Compatible con HARDENED | Alta | Bajo: condición negativa portable, sin allowlist de nombres |
| ACE allow heredada amplia (p. ej. Users:Modify) | AccessCheck concede WRITE_CONTENT | UNPROTECTED | Alta | Bajo: la heredad ya está resuelta dentro del SD devuelto por el sistema |
| Deny explícita al usuario + Allow amplia a Everyone | Deny precede: corta evaluación para ese derecho [^accesscheck] [^aceorder] | Según resto de derechos: WRITE_PROTECTED/HARDENED posible | Alta | Bajo: AccessCheck aplica precedencia canónica por nosotros |
| Descriptor ilegible/malformado | ERROR_ACCESS_DENIED / ERROR_INVALID_SECURITY_DESCR | UNKNOWN (success=False) | n/a | Cero por diseño: jamás promovido |
| FAT/exFAT | Volumen sin `FILE_PERSISTENT_ACLS` [^gvi] | UNSUPPORTED | Alta | Cero: capability inexistente declarada |
| Plataforma no-Windows | `sys.platform != "win32"` | UNSUPPORTED | Alta | Cero |
| Directorio con herencia deshabilitada (protected DACL) | SD propio del root sin ACEs heredadas | Se evalúa el SD efectivo del root; la superficie profunda queda limitación declarada (§14) | Media para el subtree | Medio: mitigado por digest RV-2 como backstop de detección |
| DELETE permitido vía padre pese a file-DACL restrictiva | Parent con FILE_DELETE_CHILD efectivo | UNPROTECTED | Alta | Bajo: el padre ES parte de la superficie evaluada |
| DACL NULL | AccessCheck concede todo lo pedido [^accesscheck] [^acllists] | UNPROTECTED | Alta | Bajo |
| DACL vacía (sin ACEs) | Se niega todo [^acllists]; sin derechos ni siquiera de lectura | WRITE_PROTECTED (lectura registrada como evidencia, no como gate) | Alta | Documentado en §5 |

Hardlinks: crear un hardlink externo hacia un archivo del Golden no muta contenido; abrirlo
para escritura o borrar cualquiera de sus nombres pasa por el mismo chequeo del archivo/directorio
(DACL por archivo, no por nombre). No constituye bypass de los estados definidos.

## 5. Estados (definiciones normativas)

Los cinco estados son exhaustivos y excluyentes. Todos los juicios de "derecho efectivo"
significan: resultado de AccessCheck con el token evaluado, sobre la superficie inspeccionada
{root del Golden, parent inmediato del root}, con generic-mapping de archivos aplicado
(§7). Derechos relevantes agrupados:

- **Mutación de contenido**: `FILE_WRITE_DATA`+`FILE_APPEND_DATA` sobre archivos ≡
  `FILE_ADD_FILE`+`FILE_ADD_SUBDIRECTORY` sobre directorios [^filear]; `DELETE`;
  `FILE_DELETE_CHILD` (sólo tiene sentido en directorios) [^filear]. RENAME es derivado:
  `DELETE` (o `DELETE_CHILD` en el padre fuente) + derecho de creación en el padre destino.
- **Mutación de metadata**: `FILE_WRITE_ATTRIBUTES`, `FILE_WRITE_EA`. Cambiar timestamps o
  EAs no altera el digest de RV-2 pero sí es mutación accidental real de un activo
  reference-only; se incluye para no prometer de más.
- **Reescritura de la propia protección**: `WRITE_DAC`, `WRITE_OWNER`.
- **Bypass por privilegio**: presencia en el token de `SeBackupPrivilege`,
  `SeRestorePrivilege` o `SeTakeOwnershipPrivilege` (§6).

```
SYNCHRONIZE, TRAVERSE, READ_CONTROL, lecturas: contexto operacional, no deciden estado
(excepto READ_CONTROL implícito necesario para leer el SD: si falta → UNKNOWN).
```

### UNSUPPORTED

Sky-Claw reconoce que **no posee un mecanismo probado** para evaluar la protección en esa
plataforma/filesystem. Es una limitación conocida de capability, decidida en código, no un
resultado observacional. Casos v1 (§8): plataforma no-Windows; volumen sin ACLs persistentes
(FAT/exFAT); filesystem no admitido en la matriz de soporte; ruta UNC/red; unidad no local.
Regla: **UNSUPPORTED != UNKNOWN**. No bloquea golden verification, clone ni nada del vault.

### UNKNOWN

La plataforma/backend **podría** observar el estado, pero evidencia necesaria no pudo
obtenerse o quedó ambigua. Casos: security descriptor ilegible o malformado
(`ERROR_ACCESS_DENIED` / `ERROR_INVALID_SECURITY_DESCR` al leerlo [^accesscheck]); consulta
de token fallida; fallo de la llamada AccessCheck; filesystem no determinable tras pasar el
gate de plataforma; root o parent que resultan symlink/junction/reparse point (la identidad
del objeto inspeccionado difiere del path: fail closed, misma política que
`InventoryLinkError` en RV-1); inconsistencia de metadata entre lstat y apertura (señal
TOCTOU). Regla dura: **UNKNOWN jamás se promueve a estado protegido**.

### UNPROTECTED

Existe evidencia suficiente de que el token evaluado conserva alguna **capacidad relevante
de mutación** sobre el Golden. Es UNPROTECTED si cualquiera de estas condiciones se cumple
(en root o parent, la más permisiva gana):

1. Algún derecho de mutación de contenido efectivamente concedido (write/append/add/delete/
   delete-child/rename-viable);
2. algún derecho de mutación de metadata concedido (`WRITE_ATTRIBUTES`/`WRITE_EA`);
3. `WRITE_DAC` o `WRITE_OWNER` efectivos (el token puede reescribir su propio acceso: la
   restricción presente no es sostenible) [^msadts];
4. presencia en el token de alguno de los tres privilegios de bypass (un privilegio
   presente-aunque-deshabilitado cuenta: el propio proceso puede habilitarlo sin elevación
   mediante AdjustTokenPrivileges; el sistema exige "hold AND enable", y el hold ya está)
   [^ifspriv];
5. parent con `FILE_DELETE_CHILD` efectivo (borrado del propio root o de hijos por encima
   del DACL individual).

### WRITE_PROTECTED

Contrato mínimo: el token normal/no elevado evaluado **no posee derechos efectivos** para
ninguna capacidad relevante de mutación de las listadas arriba (contenido, metadata,
reescritura de protección, bypass por privilegio, delete-vía-parent), sobre toda la
superficie evaluada. Lecturas (`READ_DATA`/`LIST_DIRECTORY`/`EXECUTE`/`TRAVERSE`) quedan
**registradas como campos de evidencia**, pero su presencia o ausencia NO decide el estado:
protección y legibilidad son capacidades ortogonales (decisión documentada; alternativa
rechazada: exigir `read_allowed=True`, que mezclaría dos ejes y clasificaría un directorio
totalmente inaccesible como no-protegido).

Regla anti-falso-verde central: **"una ACE parece negar WRITE" no es evidencia**. Sólo el
resultado de AccessCheck —que incorpora orden canónico de ACEs [^aceorder], precedencia de
deny [^accesscheck], SIDs deny-only del token filtrado [^uac] [^sidattrs], expansión de
generic rights y mapping archivo/directorio— demuestra capacidad efectiva. Inferencias
textuales del DACL están prohibidas como base de clasificación.

### HARDENED

Definido como refinamiento estricto de WRITE_PROTECTED (elección OPTION A, análisis completo
en §9). HARDENED ⇔ WRITE_PROTECTED **y además**:

1. el token evaluado no está elevado (`TokenElevation != TokenElevated`; equivalente a
   `TokenElevationType ∈ {Default, Limited}` [^teletype]); y
2. `owner_sid` del root **no es atribuible** al token evaluado: no es el user SID del token
   ni ningún grupo del token que no esté marcado deny-only (identidades por SID; los
   well-known SIDs relevantes se toman de la fuente primaria [^wellknown]). Esto cierra el camino implícito
   dueño→`READ_CONTROL`+`WRITE_DAC` [^msadts] [^ontt] y el camino explícito
   takeown→setowner→reescribir DACL [^ontt], porque los tres privilegios que alimentan ese
   camino ya fueron excluidos por la conjunción WRITE_PROTECTED.

HARDENED significa exactamente: *contra este token, sin elevación, no existe vía directa
demostrable para mutar contenido ni para retirar la protección*. NO significa "imposible de
desproteger": si el usuario pertenece a Administrators y puede aprobar UAC, un proceso
elegido por él puede elevarse y revertir todo. El nombre se conserva (`HARDENED`) con este
threat model documentado; la alternativa `HARDENED_AGAINST_CURRENT_UNELEVATED_TOKEN` fue
evaluada y rechazada por verbosa para UI sin añadir precisión — el calificador vive en este
ADR y en el campo `message`/docs, no en el identificador del enum.

## 6. Modelo de evidencia (y FASE active-probes)

**¿Debe el inspector demostrar la protección intentando crear/escribir/borrar/renombrar?**
NO. `ACTIVE_WRITE_PROBE_REQUIRED=NO`.

Justificación:

1. **El daño del probe recae sobre el activo que se quiere proteger.** Un probe fallido
   igual intenta la operación: sobre un Golden mal protegido eso es exactamente la mutación
   accidental que la capability existe para evitar. El sensor preexistente
   `WritePermissionsChecker` (`sky_claw/local/validators/write_permissions.py`) usa write-probe
   empírico con pleno derecho porque sus targets son **rutas mutables del Ritual**
   (overwrite/mods/Data/perfiles), donde un `.skyclaw_probe_*.tmp` es inocuo. Esa asimetría
   —probe OK para outputs, prohibido para el Golden— queda registrada como decisión, no como
   contradicción.
2. **El kernel ya respondió la pregunta**: AccessCheck ejecuta el mismo algoritmo que el
   object manager usa al abrir [^accesscheck] [^howdacl]. Una escritura real sólo agregaría
   información sobre carreras temporales, no sobre el contrato.
3. **El repo ya documentó que `os.access(W_OK)` miente en Windows** (ignora ACLs): otro motivo
   para no aceptar atajos stdlib como "evidencia".

Si Windows no permitiera demostrar alguna propiedad sin escritura real, esa propiedad se
clasifica UNKNOWN/no demostrable con el backend v1 — no se agrega la escritura.

### Superficie y fuentes de evidencia

| Pieza | Fuente (todas read-only) | Notas |
|---|---|---|
| Tipo/identidad del nodo y detección de reparse points | primitiva canónica `sky_claw.app.security.links` (lstat + `st_reparse_tag`) — la MISMA que usa `inventory_tree`; prohibido reimplementar (ancla `tests/test_links.py`) | Root o parent como enlace → UNKNOWN |
| Filesystem del volumen | `CreateFileW` (0 access + `FILE_FLAG_BACKUP_SEMANTICS`) + `GetVolumeInformationByHandleW`: nombre FS + flag `FILE_PERSISTENT_ACLS` [^gvi] | Por handle: correcto ante mount points |
| Security descriptor (owner + DACL) | `GetNamedSecurityInfoW(SE_FILE_OBJECT, OWNER\|DACL)` [^gerfa-ej] | Requiere READ_CONTROL; denegado → UNKNOWN |
| Token: user, grupos y atributos | `GetTokenInformation(TokenUser/TokenGroups)`; grupos deny-only distinguibles por atributos [^sidattrs] | Base del check de owner-atributable |
| Token: elevación | `GetTokenInformation(TokenElevation)` (+ `TokenElevationType` diagnóstico) [^teletype] | |
| Token: privilegios presentes | `GetTokenInformation(TokenPrivileges)`; LUID resuelto con `LookupPrivilegeValue` (los LUID varían por boot [^priv]) | Presencia, no sólo estado enabled |
| Acceso efectivo | `AccessCheck` con token de impersonación duplicado (`DuplicateTokenEx`), desired `MAXIMUM_ALLOWED` con `MapGenericMask`, `GENERIC_MAPPING` de archivos [^accesscheck] | Devuelve máscara concedida real |

**Evidencia insuficiente (prohibida como base de estado)**: strings de nombres de cuenta;
salida parseada de icacls/cacls; atributo ReadOnly del filesystem; `os.access`; bits POSIX
heredados; `GetEffectiveRightsFromAcl` (deprecated y con puntos ciegos documentados: ignora
derechos implícitos del owner, ignora privilegios, ignora grupos de sesión de logon, falla
con deny heredadas y no ve delete provisto por el padre [^gerfa]); presencia de una ACE
deny leída a mano.

## 7. Análisis del backend Windows

| Opción | Correctitud | Localización | Subprocess | Compat | Testabilidad | Privilegios | Mantenimiento | Token arbitrario |
|---|---|---|---|---|---|---|---|---|
| A. Parseo manual de DACL/ACEs | Baja: hay que reimplementar orden canónico [^aceorder], deny-precedence [^howdacl], deny-only [^sidattrs], generic mapping, owner-implícito [^msadts] | n/a | No | Buena | Media | Hay que emular bypass | Pésimo: cada regla nueva del kernel es deuda | Sí, pero a mano |
| B. **AccessCheck API (advapi32)** | **Alta: el mismo motor del sistema** [^accesscheck] | Ninguna | No | XP+ | Alta (SD sintéticos en temp + clasificador puro) | No aplica bypass solo → complementar con introspección de token (§6) | Bajo: ~5 APIs estables | Sólo tokens que el proceso posea/duplique |
| C. Authz API | Alta (misma semántica; es el reemplazo recomendado de GetEffectiveRightsFromAcl [^gerfa]) | Ninguna | No | Vista+ | Muy alta: contextos sintéticos desde SID puro [^gerfa] | Controlado por callback | Medio-alto: resource manager + structs extra | **Sí**: contextos desde SIDs sin tener el token |
| D. icacls/CLI externos | Baja como evidencia: salida textual localizada; el repo YA carga workarounds de renderizado SID/ICACLS en `file_permissions.py` | **Alta** (idioma del SO) | Sí | Variable | Frágil (golden-outputs) | n/a | Alto | No |

Decisión RV-GP1: **backend B** (AccessCheck + GetTokenInformation + GetNamedSecurityInfoW +
GetVolumeInformationByHandleW, vía ctypes). Motivos: única opción que satisface el contrato
completo con complejidad mínima; cero dependencias nuevas (precedente ctypes del repo:
`skyclaw_bridge/runtime.py`); cero subprocess; immune a idioma del SO (los tests GP-T13/T14/T15
no dependen de strings). La opción C se reserva como vehículo futuro para simulación de
contextos arbitrarios (p. ej., "¿qué vería un usuario estándar?" desde un proceso admin) —
GP1 v1 no la necesita porque el contrato evalúa el token actual. La opción D queda rechazada
explícitamente (test GP-T21 ancla la ausencia).

Nota de implementación (para el PR futuro): el módulo debe entrar a la lista strict de mypy
como hizo `sky_claw.app.security.links`, porque `sky_claw.local.runtime_vault.*` hereda
estándares estrictos y `app/security/*` está exento de mypy/BLE001.

## 8. Capability del filesystem

1. **¿Cómo saber qué filesystem contiene el path?** Handle propio del path +
   `GetVolumeInformationByHandleW` → nombre ("NTFS", "ReFS", "exFAT", …) y flags, entre
   ellos `FILE_PERSISTENT_ACLS` = "el volumen preserva y hace cumplir ACLs (NTFS sí, FAT
   no)" [^gvi].
2. **Matriz v1**: se proclama soporte sólo donde el contrato puede demostrarse y testearse:

| Entorno | Estado v1 | Razón |
|---|---|---|
| NTFS local (letra de unidad) | SOPORTADO | Modelo ACL completo; único validable en CI/rig hoy |
| ReFS | UNSUPPORTED (candidato GP1.1) | Estructuralmente mantiene ACLs (flag persistente y soporte documentado de la API [^gvi]), pero v1 no admite capabilities sin validación de rig — regla anti-optimismo |
| exFAT/FAT | UNSUPPORTED estructural | Sin ACLs persistentes [^gvi]: no hay nada que evaluar |
| FS desconocido/tercero con flag de ACLs | UNSUPPORTED | Fail closed: nombre fuera de matriz |
| UNC / red / SMB | UNSUPPORTED | El chequeo de acceso es del servidor; la evaluación local no representa al consumidor real |
| Linux/macOS | UNSUPPORTED | Backend v1 es Windows-only; el vault sigue funcionando sin esta capability |

3. **NTFS no es requisito del Runtime Vault**: `UNSUPPORTED_BLOCKS_RUNTIME_VAULT=NO`;
   verification/clone no consultan esta capability jamás (§11).

## 9. Decisión semántica de HARDENED

**OPTION A — "Endurecido frente al token actual no elevado"** (ELEGIDA).
HARDENED ⇔ WRITE_PROTECTED ∧ token-no-elevado ∧ owner-no-attribuible (§5).
Demostrable 100% read-only; portable (SIDs, sin nombres); no promete nada contra UAC/admin/
SYSTEM; útil para UI futura (un verde honesto); testeable de punta a punta con DACLs
sintéticas en temp dirs + clasificador puro.

**OPTION B — HARDENED_DEFINITION=DEFERRED.** Exponer sólo 4 estados hasta GP1.1. Descartada:
la definición A ES demostrable con el backend elegido; aplazarla regalaría la categoría más
útil para el objetivo declarado (protección frente a procesos normales) sin ganar honestidad.

**OPTION C — "Imposible de desproteger sin credenciales administrativas".** Incluiría
pertenencia potencial a Administrators + capacidad UAC como criterio de no-HARDENED.
Descartada: **confunde capability de elevación con permisos del token actual**; obligaría a
responder "¿puede este usuario aprobar UAC?" (consultas LSA/account policy fuera de alcance,
frágiles y aún así falseables), y seguiría sin cubrir SYSTEM/F. Prometería resistencia que
NTFS no da.

Elección: **OPTION A**, con el nombre público `HARDENED` y el threat model de §4/§5 como
contrato normativo. Si una implementación futura no pudiera obtener ALGUNA de las evidencias
de la conjunción (owner, elevación, privilegios, acceso efectivo), el resultado es UNKNOWN —
nunca HARDENED parcial.

## 10. Taxonomía de errores y semántica success/state

Aplicando el principio del repo (`success: bool` + `message` canónico vacío en éxito):

| Clase | Ejemplos concretos | Mecanismo |
|---|---|---|
| PROGRAMMER INPUT ERROR | path vacío/relativo; path inexistente; path que no es directorio; llamada con `Path` de archivo | Excepción de dominio (`RuntimeVaultError`-family propuesta: `GoldenProtectionInputError`). No produce result |
| OBSERVATION FAILURE | SD ilegible; AccessCheck falla; token query falla; root/parent reparse point; TOCTOU señal | `state=UNKNOWN`, `success=False`, `message` explicativo no vacío |
| MEASURED SECURITY STATE | sin derechos de mutación → WRITE_PROTECTED; con alguno → UNPROTECTED; conjunción completa → HARDENED | `success=True`, `message=""` |
| KNOWN UNSUPPORTED CAPABILITY | no-Windows; exFAT; UNC; FS fuera de matriz | `state=UNSUPPORTED`, `success=True` (la inspección tuvo éxito: descubrió correctamente que no hay backend), `message=""`, evidence con platform/filesystem observados |

Invarianzas estructurales (a anclar en `__post_init__`, patrón `PhysicalIndependenceResult` /
`TreeVerificationResult`):

- `success=True ⇔ state ∈ {UNSUPPORTED, UNPROTECTED, WRITE_PROTECTED, HARDENED}`;
- `success=False ⇒ state=UNKNOWN ∧ message≠""`; `success=True ⇒ message==""`;
- `state=UNKNOWN ⇒ ¬success` (jamás un UNKNOWN "informativo exitoso");
- nunca `success == protected`: son conceptos distintos (§11 muestra `VERIFIED + UNPROTECTED`).

## 11. Independencia del Runtime Vault (no-acoplamiento RV-3)

Verificado contra el código vigente (`clone.py::create_runtime_clone(golden_source:
GoldenMasterVerificationResult, destination, ...)`):

- `create_runtime_clone` **NO necesita** consultar protection state. Su cadena de
  autorización permanece: `GoldenMasterVerificationResult.state == VERIFIED` → clone →
  `RuntimeCloneResult`, **independientemente** de que la protección sea UNSUPPORTED,
  UNPROTECTED, WRITE_PROTECTED, HARDENED o UNKNOWN.
- Se rechazan explícitamente: `if protection != HARDENED: reject clone`; campos de
  protección en `GoldenMasterDescriptor` / `GoldenMasterVerificationResult` /
  `RuntimeCloneResult`; cualquier import de protection desde clone/models.
- Integridad ⊥ protección (FASE 12): TreeDigest identifica CONTENIDO; la protección es una
  capability de ACCESO. `VERIFIED + UNPROTECTED` es un Golden válido (íntegro y vulnerable)
  igual que `VERIFIED + HARDENED`. El VerificationState del Golden **no se degrada por ACL**;
  simétricamente, un digest FAILED no cambia el protection state. La relación operativa es
  de backstop: RV-2 detecta mutaciones que la protección dejó pasar (post-hoc), la protección
  las previene (pre-hoc); ninguno sustituye al otro.
- Si durante la implementación apareciera una necesidad real de acoplar ambos mundos, el
  trabajo se detiene con veredicto `STOP_RV4_ARCHITECTURAL_COUPLING` y justificación
  arquitectónica extraordinaria en un ADR nuevo.

Anclas de test que congelan esta ortogonalidad: GP-T19 (comportamiento) y GP-T34 (AST:
`runtime_vault.clone` no importa el módulo de protección — patrón enumerativo del repo).

## 12. API/modelo propuesto (sin implementar)

```python
class GoldenProtectionState(StrEnum):
    UNSUPPORTED = "unsupported"
    UNPROTECTED = "unprotected"
    WRITE_PROTECTED = "write_protected"
    HARDENED = "hardened"
    UNKNOWN = "unknown"


class GoldenProtectionRight(StrEnum):
    """Derechos efectivos relevantes, ya resueltos por AccessCheck."""
    READ_DATA           # FILE_READ_DATA / FILE_LIST_DIRECTORY
    EXECUTE             # FILE_EXECUTE / FILE_TRAVERSE
    WRITE_CONTENT       # WRITE_DATA+APPEND ≡ ADD_FILE+ADD_SUBDIR según nodo
    DELETE              # DELETE del nodo
    DELETE_CHILD        # FILE_DELETE_CHILD del directorio evaluado (parent/root)
    WRITE_METADATA      # FILE_WRITE_ATTRIBUTES + FILE_WRITE_EA
    CHANGE_PERMISSIONS  # WRITE_DAC
    CHANGE_OWNER        # WRITE_OWNER


@dataclass(frozen=True, slots=True)
class GoldenProtectionEvidence:
    """Evidencia cruda observada; cada campo None = no observable (posible causa UNKNOWN)."""
    platform: str                                  # p.ej. "windows"; siempre observable
    filesystem: str | None                         # nombre del volumen según GetVolumeInformationByHandleW
    filesystem_persistent_acls: bool | None        # flag FILE_PERSISTENT_ACLS
    owner_sid: str | None                          # S-1-… del root (ConvertSidToStringSid)
    current_user_sid: str | None                   # user SID del token evaluado
    current_token_elevated: bool | None            # TokenElevation
    bypass_privileges_present: frozenset[str] | None  # {"SeTakeOwnershipPrivilege",...} presentes
    granted_rights_root: frozenset[GoldenProtectionRight] | None
    granted_rights_parent: frozenset[GoldenProtectionRight] | None


@dataclass(frozen=True, slots=True)
class GoldenProtectionResult:
    state: GoldenProtectionState
    message: str = ""
    evidence: GoldenProtectionEvidence            # siempre presente; campos None-ables
    scope: str = "root_and_parent"                # constante v1; documenta el límite del subtree

    @property
    def success(self) -> bool: ...                # True SOLO en los 4 estados definitivos (§10)
```

Justificación campo por campo (los `*_allowed` booleanos propuestos en la tarea se
RECHAZAN como 12 campos planos: el frozenset es la misma información sin duplicación, y
cada booleano sería derivable con `right in evidence.granted_rights_*`):

| Campo | ¿Necesario? | ¿Evidence o derivado? | ¿None posible? | Invariante |
|---|---|---|---|---|
| state/message/success | contrato del repo | derivado de la clasificación | message="" salvo UNKNOWN | §10 |
| evidence.platform | gate de UNSUPPORTED | evidence | no | `"windows"` ⇒ backend disponible |
| filesystem, filesystem_persistent_acls | gate de matriz §8 | evidence | sí (si no llegó a observarse) | ambos None juntos o juntos informados |
| owner_sid | conjunción HARDENED + diagnóstico | evidence | sí (ilegible ⇒ UNKNOWN) | string SIDL, jamás nombre de cuenta |
| current_user_sid | check owner-attribuable + tests T14–T16 | evidence | sí | idem |
| current_token_elevated | conjunción HARDENED | evidence | sí | coherente con HARDENED: `HARDENED ⇒ current_token_elevated is False` |
| bypass_privileges_present | anti-bypass (UNPROTECTED/WRITE_PROTECTED) | evidence | sí | HARDENED/WRITE_PROTECTED ⇒ conjunto vacío |
| granted_rights_root / _parent | clasificación + diff pre/post GP2/GP3 | derivado de AccessCheck, conservado crudo | sí | `WRITE_PROTECTED/HARDENED ⇒ ∩ mutación = ∅` sobre ambos sets |
| scope | honestidad del límite subtree (§14) | constante v1 | no | literal `"root_and_parent"` en v1 |

Firmas propuestas (sólo diseño): `inspect_golden_protection(path: pathlib.Path) ->
GoldenProtectionResult` (sync, blocking I/O corto; el caller async envuelve con to_thread,
patrón del repo) y excepción `GoldenProtectionInputError(RuntimeVaultError)`.

## 13. Plan de tests (para la implementación futura; NO se implementan acá)

Convención del repo: tests en español, AAA, anclas enumerativas. Integraciones de effective
access usan directorios temporales con DACLs manipuladas vía `SetNamedSecurityInfoW` (mutar
temp dirs es legítimo); variaciones de token/elevación se testean a nivel clasificador puro
(evidencia fabricada), porque el proceso de test no controla su propio token.

| ID | Escenario | Tipo |
|---|---|---|
| GP-T01 | plataforma no soportada → UNSUPPORTED (portabilidad: corre en POSIX CI también) | portabilidad |
| GP-T02 | filesystem no admitido (stub de evidence con fs="exFAT") → UNSUPPORTED | unit/clasificador |
| GP-T03 | fallo de observación del SD (stub) → UNKNOWN, success=False, message≠"" | unit/clasificador |
| GP-T04 | token con WRITE efectivo → UNPROTECTED | integración temp-dir |
| GP-T05 | token con CREATE (ADD_FILE) efectivo → UNPROTECTED | integración temp-dir |
| GP-T06 | token con DELETE efectivo → UNPROTECTED | integración temp-dir |
| GP-T07 | token con RENAME viable (delete-child en parent + add en destino) → UNPROTECTED | integración temp-dir |
| GP-T08 | lectura/listado permitidos + todos los derechos de mutación denegados → WRITE_PROTECTED | integración temp-dir |
| GP-T09 | WRITE_DAC disponible → NO HARDENED (UNPROTECTED por capacidad de reescritura) | integración temp-dir |
| GP-T10 | WRITE_OWNER disponible → NO HARDENED | integración temp-dir |
| GP-T11 | owner=user + capacidad de reescribir ACL → NO HARDENED | integración temp-dir |
| GP-T12 | owner privilegiado + sin mutación/DAC/OWNER/privilegios para el token + token no elevado → HARDENED | integración temp-dir |
| GP-T13 | idioma de Windows localizado no afecta la clasificación (sin strings de cuenta en el código: AST) | windows/ancla |
| GP-T14 | SID de Microsoft Account (S-1-12-…) tratado sin asumir username | unit |
| GP-T15 | SID de cuenta local (S-1-21-…-RID) tratado genéricamente | unit |
| GP-T16 | token de usuario estándar manejado (clasificador con evidencia sintética) | unit |
| GP-T17 | token admin FILTRADO distinguido de token ELEVADO: deny-only groups + sin privilegios admin vs Full | unit/clasificador |
| GP-T18 | UNKNOWN jamás se vuelve protegido: paramétrico sobre todas las causas de UNKNOWN | false-green |
| GP-T19 | el estado de protección no cambia la autorización de Runtime Clone (VERIFIED+UNPROTECTED clona igual que VERIFIED+HARDENED) | ortogonalidad |
| GP-T20 | el inspector no ejecuta ningún syscall de escritura/creación/borrado/renombrado (monkeypatch de os/open/subprocess + digest del árbol idéntico pre/post) | false-green |
| GP-T21 | sin dependencia de subprocess/icacls si el backend es API (AST anchor: módulo sin `subprocess`, sin literales "icacls"/"takeown") | ancla |
| GP-T22 | el inspector no recomputa el tree digest (AST: protection no importa verification/inventory) | rendimiento/frontera |
| GP-T23 | symlink/junction en root o parent → UNKNOWN (fail closed), usando la primitiva canónica de links | fail-closed |
| GP-T24 | path inexistente → excepción input-error; path-file → excepción input-error; nunca result verde | taxonomía |
| GP-T25 | invarianzas success/state paramétricas sobre los 5 estados (§10) | contracto |
| GP-T26 | deny explícita precede allow amplia → derecho denegado pese al allow (DACL sintética) | adversarial/false-green |
| GP-T27 | privilegio bypass presente (aunque disabled) en evidencia → UNPROTECTED, nunca WRITE_PROTECTED | adversarial/false-green |
| GP-T28 | evidencia de token elevado → nunca HARDENED | adversarial |
| GP-T29 | parent con FILE_DELETE_CHILD efectivo aunque root restrictivo → UNPROTECTED | adversarial/false-green |
| GP-T30 | DACL NULL → UNPROTECTED; DACL vacía → WRITE_PROTECTED con lecturas en None/false | adversarial |
| GP-T31 | idempotencia: dos inspecciones consecutivas producen resultados iguales y el árbol no cambia | estabilidad |
| GP-T32 | owner SID no resoluble a string → UNKNOWN | observación |
| GP-T33 | UNC path → UNSUPPORTED | matriz |
| GP-T34 | AST: `runtime_vault.clone` y `runtime_vault.models` no referencian el módulo de protección (ortogonalidad enumerada, estilo test_ritual_dispatch) | ortogonalidad/ancla |

Resumen: 34 tests propuestos; false-green explícitos: T18, T20, T26, T27, T29, T30;
Windows-específicos: T04–T12, T13, T17, T23, T29, T30, T33 (skipif no-win32 donde corresponda);
portabilidad: T01, T02, T03, T24, T25, T34 corren en cualquier plataforma.

## 14. Limitaciones de seguridad (declaradas)

1. **No protege contra C/E/F**: un proceso elevado por consentimiento del usuario, SYSTEM u
   otro servicio privilegiado puede reescribir owner/DACL y saltarse toda la protección
   [^ontt] [^ifspriv]. HARDENED es un statement sobre el token evaluado, no sobre el mundo.
2. **Scope v1 = {root, parent}**: captura todos los vectores de un paso contra el propio
   root (incluido delete/rename del root vía parent). Los descendientes se asumen amparados
   por herencia del root; ACEs explícitas no heredadas más profundas NO son auditadas en v1.
   Mitigaciones: campo `scope` explícito en el resultado (nada sobrepromete), y RV-2 tree
   digest como detector post-hoc completo. Un deep-scan opcional de security descriptors por
   subárbol queda como candidato GP1.1 (read-only, costoso, cacheable por descriptor único).
3. **TOCTOU best-effort**: entre la lectura del SD y cualquier uso posterior, un actor con
   derecho de cambio podría alterarlo; la política de enlaces y la revalidación reducen la
   ventana sin eliminarla (mismo límite declarado que `inventory_tree`).
4. **UAC puede revertir HARDENED**: la pertenencia potencial a Administrators + aprobación
   UAC no es observable de forma confiable y NO participa del contrato (OPTION C rechazada).
5. Los atributos ReadOnly del filesystem NO cuentan como protección (cualquier herramienta
   los limpia; no son un control de acceso).

## 15. Futuro GP2/GP3 y Execution Guard (sólo restricciones)

**GP2 — Protect Golden** (MUTATING, futuro): HITL obligatorio; autorización EXCLUSIVAMENTE
vía UAC/elevación de Windows (consentimiento del SO). **Nunca** pedir, escribir ni guardar
passwords/credenciales en Sky-Claw (coherente con la política lock-only de la capa agente).
Debe: capturar ANTES el descriptor de seguridad original (rollback serializado, p. ej. SDDL),
aplicar protección uniformemente heredable al subtree (cerrando el hueco de profundidad de
§14.2), re-inspeccionar con GP1 esperando HARDENED, re-verificar digest RV-2 intacto, y
fail-closed con journal si la verificación post-cambio no cuadra (patrones existentes del
repo). El schema de evidencia de GP1 (granted_rights sets comparables) está diseñado para el
diff pre/post.

**GP3 — Restore/Unprotect** (MUTATING, futuro): HITL + UAC; restaura el descriptor original
capturado por GP2 y re-verifica digest VERIFIED. Verified recovery, sin atajos.

**Execution Guard** (feature futura, fuera de GP1 y de RV-3): `reference_only` → no
seleccionable como execution target. Es una decisión de la capa de selección/lanzamiento de
runtimes (territorio RV-5/MO2), NO una propiedad ACL. `GoldenMasterDescriptor.role` ya lleva
el dato; el enforcement no se diseña acá.

GP1 no impide ninguno de los tres: no introduce estados que exijan acoplamiento, no consume
la nomenclatura RV-4, y su backend read-only es subconjunto del que GP2/GP3 necesitarán.

## 16. Preguntas abiertas

1. Actualizar el índice de `docs/adr/README.md` (lista + línea "Última verificación") quedó
   FUERA del write-set autorizado de esta tarea; hacerlo en merge review o follow-up.
2. Validar ReFS en rig real para promoverlo a matriz de soporte en GP1.1.
3. ¿Deep-scan de descriptores por subtree en GP1.1, o suficiente el backstop RV-2?
4. Redacción final del mensaje de UI para HARDENED (debe transmitir "frente al uso normal",
   no "intocable").
5. Nombre definitivo de la excepción de input (`GoldenProtectionInputError` propuesto).

## Referencias (fuentes primarias Microsoft)

[^uac]: How User Account Control works — learn.microsoft.com/windows/security/identity-protection/user-account-control/how-user-account-control-works (dos tokens al logon; el filtrado remueve privilegios y SIDs admin; herencia hijo-padre; consent vs credential prompt).
[^accesscheck]: AccessCheck function — learn.microsoft.com/windows/win32/api/securitybaseapi/nf-securitybaseapi-accesscheck (token de impersonación + TOKEN_QUERY; MapGenericMask; MAXIMUM_ALLOWED; DACL NULL concede; ERROR_INVALID_SECURITY_DESCR sin owner/group).
[^howdacl]: How AccessCheck Works — learn.microsoft.com/windows/win32/secauthz/how-dacls-control-access-to-an-object (secuencia de ACEs; deny corta; denegación implícita final; SIDs de grupo no-enabled ignorados).
[^aceorder]: Order of ACEs in a DACL — learn.microsoft.com/windows/win32/secauthz/order-of-aces-in-a-dacl (orden preferido: explícitas antes que heredadas; deny antes que allow).
[^wellknown]: Well-known SIDs — learn.microsoft.com/windows/win32/secauthz/well-known-sids (S-1-5-32-544 Administrators; S-1-5-32-545 Users; S-1-5-18 LocalSystem; S-1-5-11 Authenticated Users; S-1-5-5-X-Y sesión).
[^filear]: File Access Rights Constants — learn.microsoft.com/windows/win32/fileio/file-access-rights-constants (FILE_DELETE_CHILD incluye read-only; FILE_WRITE_DATA≡ADD_FILE y APPEND≡ADD_SUBDIR en directorios; BYPASS_TRAVERSE_CHECKING).
[^gerfa]: GetEffectiveRightsFromAclA — learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-geteffectiverightsfromacla (deprecated; ignora owner-implícito, privilegios y grupos de sesión; falla con deny heredada; no ve Delete/ReadAttributes provistos por el padre; ejemplo oficial migra a Authz).
[^gvi]: GetVolumeInformationByHandleW — learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getvolumeinformationbyhandlew (FILE_PERSISTENT_ACLS: NTFS sí, FAT no; nombre del FS; tabla de tecnologías: ReFS soportado).
[^teletype]: TOKEN_ELEVATION_TYPE — learn.microsoft.com/windows/win32/api/winnt/ne-winnt-token_elevation_type (Default/Full/Limited).
[^priv]: Privileges — learn.microsoft.com/windows/win32/secauthz/privileges (enumeración vía GetTokenInformation con estado enabled; LUID por boot → LookupPrivilegeValue; PrivilegeCheck).
[^ifspriv]: Managing Privileges in a File System (Windows drivers) — learn.microsoft.com/windows-hardware/drivers/ifs/privileges (SeBackupPrivilege lee y SeRestorePrivilege escribe/cambia owner-protection saltándose chequeos; hold AND enabled).
[^evt4661]: Event 4661 (auditoría) — learn.microsoft.com/previous-versions/windows/it-pro/windows-10/security/threat-protection/auditing/event-4661 (máscaras auto-concedidas: SeRestorePrivilege ⇒ WRITE_DAC|WRITE_OWNER|GENERIC_WRITE|ADD_FILE|ADD_SUBDIR|DELETE; SeBackupPrivilege ⇒ READ_CONTROL|GENERIC_READ|TRAVERSE).
[^msadts]: [MS-ADTS] 6.1.3.4 Blocking Implicit Owner Rights — learn.microsoft.com/openspecs/windows_protocols/ms-adts/fb7c101d-ec8b-4fbf-bca8-7d7c2d747d0c ("The Owner of a security descriptor is implicitly granted READ_CONTROL and WRITE_DAC rights by default").
[^ontt]: The Old New Thing, 2024-10-30 — devblogs.microsoft.com/oldnewthing/20241030-00 (SeTakeOwnershipPrivilege gobierna SetNamedSecurityInfo(OWNER_...); ownership ⇒ READ_CONTROL+WRITE_DAC automáticos; cadena takeown→setowner→rewrite-DACL→write).
[^acllists]: Access Control Lists — learn.microsoft.com/windows/win32/secauthz/access-control-lists (DACL NULL concede a everyone; DACL vacía niega todo).
[^sidattrs]: SID Attributes in an Access Token — learn.microsoft.com/windows/win32/secauthz/sid-attributes-in-an-access-token (SE_GROUP_USE_FOR_DENY_ONLY del token filtrado).

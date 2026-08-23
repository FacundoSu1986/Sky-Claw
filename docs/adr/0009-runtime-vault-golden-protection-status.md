# ADR 0009 — RV-GP1: Estado de protección filesystem del Golden Master

**Fecha:** 2026-08-23
**Estado:** Propuesta (design-only; prohibida la implementación en este PR). Revisión 2:
ronda de corrección sobre los review threads del PR #504 (cobertura de subtree, composición
de derechos, privilegios, backend AccessCheck, drives remotos, scope estructurado de
HARDENED, anclas anti-mutación nativa, ortogonalidad completa, inventario GP2 por nodo).
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
3. **La protección del Golden es una propiedad del ÁRBOL completo**, no del root (§5).
   Un estado protegido (`WRITE_PROTECTED`/`HARDENED`) sólo puede emitirse tras inspección
   EXHAUSTIVA de la superficie `PROTECTED_SCAN_SCOPE` = root + todo directorio descendiente
   + todo archivo descendiente + el parent inmediato del root (por la semántica
   delete/rename del propio root). Sin sampling, sin "archivos representativos", sin
   inferencia "root ⇒ descendientes". Escaneo parcial JAMÁS emite estado protegido.
4. **Backend Windows v1 = AccessCheck nativo + introspección de token**, vía `ctypes` sobre
   advapi32/kernel32, solicitando `OWNER|GROUP|DACL_SECURITY_INFORMATION` (el SD debe tener
   owner y group para AccessCheck [^accesscheck]). Gate previo de localidad con
   `GetDriveTypeW` (§7-8). Sin dependencia Python nueva, sin subprocess, sin parseo de
   salida localizada (análisis completo en §7).
5. **Cinco estados** con definiciones operativas y evidencia exigente (§5):
   `UNSUPPORTED`, `UNPROTECTED`, `WRITE_PROTECTED`, `HARDENED`, `UNKNOWN`. Agregación
   determinista sobre el árbol: capability gate → UNKNOWN ante cualquier fallo/drift →
   UNPROTECTED ante cualquier nodo/superficie compuesta mutable → HARDENED si además se
   cumplen todas sus conjunciones → si no, WRITE_PROTECTED.
6. **HARDENED elegido = OPTION A** (§9): endurecido frente al token interactivo actual no
   elevado sobre TODO el subtree, demostrable al 100% read-only. El calificador viaja en
   metadata estructurada (`assurance_scope`), NUNCA en `message` (§12).
   `HARDENED != ADMIN_PROOF != SYSTEM_PROOF`.
7. **Ortogonalidad total** respecto de RV-1/RV-2/RV-3 (§11): la protección no autoriza ni
   bloquea clonación, ni degrada verificación, ni depende de la versión del juego.
8. **Semántica success/state desacoplada** (§10): `success=True` con `UNPROTECTED` es válido
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
| Owner = SID del usuario actual, sin ACE del Owner Rights SID | Dueño atribuible ⇒ WRITE_DAC efectivo implícito (medido por AccessCheck o asumido fail-closed) [^msadts] [^ontt] [^ownerrights] | UNPROTECTED | Alta | Ninguno: la regla normativa "CHANGE_PERMISSIONS efectivo ⇒ UNPROTECTED" se aplica a CUALQUIER vía, incluida la del dueño |
| Owner = usuario actual + ACE del Owner Rights SID (S-1-3-4) restringiendo al dueño [^ownerrights] | AccessCheck mide el WRITE_DAC que la ACE Owner Rights concede/niega | Según medición: WRITE_PROTECTED/HARDENED posible; ambigüedad → UNKNOWN | Alta | Bajo: el caso es explícito y medido, nunca inferido |
| Owner = Administrators/SYSTEM/TrustedInstaller | Owner no atribuible al token evaluado | Compatible con HARDENED | Alta | Bajo: condición negativa portable, sin allowlist de nombres |
| ACE allow heredada amplia (p. ej. Users:Modify) en root o en CUALQUIER descendiente | AccessCheck concede WRITE_CONTENT en ese nodo | UNPROTECTED (agregación ANY_MUTABLE_NODE sobre el árbol) | Alta | Bajo: herencia ya resuelta dentro de cada SD; cobertura exhaustiva elimina el hueco de descendientes |
| Deny explícita al usuario + Allow amplia a Everyone | Deny precede: corta evaluación para ese derecho [^accesscheck] [^aceorder] | Según resto de derechos: WRITE_PROTECTED/HARDENED posible | Alta | Bajo: AccessCheck aplica precedencia canónica por nosotros |
| Descriptor ilegible/malformado | ERROR_ACCESS_DENIED / ERROR_INVALID_SECURITY_DESCR | UNKNOWN (success=False) | n/a | Cero por diseño: jamás promovido |
| FAT/exFAT | Volumen sin `FILE_PERSISTENT_ACLS` [^gvi] | UNSUPPORTED | Alta | Cero: capability inexistente declarada |
| Plataforma no-Windows | `sys.platform != "win32"` | UNSUPPORTED | Alta | Cero |
| Directorio descendiente con herencia deshabilitada (protected DACL) | Se evalúa el SD propio de CADA nodo (cobertura exhaustiva §5) | Clasificado por su propio descriptor; sin efecto ciego de herencia | Alta | Bajo: el hueco root-only fue eliminado en la revisión 2 |
| DELETE permitido vía padre pese a file-DACL restrictiva | Parent con FILE_DELETE_CHILD efectivo | UNPROTECTED | Alta | Bajo: el padre ES parte de la superficie evaluada |
| DACL NULL | AccessCheck concede todo lo pedido [^accesscheck] [^acllists] | UNPROTECTED | Alta | Bajo |
| DACL vacía (sin ACEs) | Descriptor OBSERVADO con DACL presente-vacía ⇒ niega todo salvo los derechos implícitos del dueño [^acllists]; clasificación sólo tras completar TODAS las checks de mutación incluida la regla del dueño (§5) | WRITE_PROTECTED posible; dueño atribuible ⇒ regla OWNER SELF-REWRITE manda (UNPROTECTED); descriptor ilegible ⇒ UNKNOWN, nunca protegido | Alta | Corregido en revisión 3 (thread CodeRabbit u6B) |

Hardlinks: crear un hardlink externo hacia un archivo del Golden no muta contenido; abrirlo
para escritura o borrar cualquiera de sus nombres pasa por el mismo chequeo del archivo/directorio
(DACL por archivo, no por nombre). No constituye bypass de los estados definidos.

## 5. Estados (definiciones normativas)

**La protección del Golden es una propiedad del ÁRBOL.** Conceptualmente:

```text
GoldenProtection(tree) = aggregate(
    estado de seguridad de CADA nodo del subtree,
    superficie de delete/rename del root vía su parent,
    token evaluado,
    privilegios relevantes del token
)
```

Superficie obligatoria para estados definitivos (`PROTECTED_SCAN_SCOPE`): el root, TODO
directorio descendiente, TODO archivo descendiente, y el parent inmediato del root (sólo
por los vectores delete/rename/replace del propio root: `FILE_DELETE_CHILD` se evalúa sobre
el directorio contenedor inmediato [^filear], así que ancestros más altos no aportan
vectores de un paso contra el root). Sin sampling ni inferencia por herencia: un
descendiente con herencia deshabilitada, ACE explícita escribible u owner distinto es un
caso de primera clase (agregación `ANY_MUTABLE_NODE`). Costo O(nodos) declarado; el rig de
referencia (~16k archivos) es factible y cacheable por descriptor único.

Observación sellada best-effort (mismo principio RV-1, sin prometer atomicidad real):

1. **PRE**: recorrido estructural completo del subtree (identidad de cada nodo vía la
   primitiva canónica de links);
2. **EVIDENCE**: lectura del security descriptor + AccessCheck de cada nodo;
3. **POST**: re-recorrido estructural — cualquier diferencia (archivo agregado o borrado,
   nodo reemplazado, drift de directorios) o cualquier evidencia ilegible ⇒ `UNKNOWN`.
   Nunca un escaneo parcial produce WRITE_PROTECTED ni HARDENED.

Todos los juicios de "derecho efectivo" significan: resultado de AccessCheck con el token
evaluado sobre el security descriptor del nodo, con generic-mapping de archivos/directorios
aplicado (§7).

### Capacidades compuestas (composición operacional, no bits sueltos)

La clasificación NO usa unión "el más permisivo gana" sobre derechos aislados; compone
capacidades con semántica Windows:

| Capacidad | Composición | Consecuencia |
|---|---|---|
| DIRECT CONTENT/METADATA MUTATION (por nodo) | `WRITE_DATA`/`APPEND` sobre archivo; `WRITE_ATTRIBUTES`/`WRITE_EA` sobre nodo | Nodo mutable |
| CREATE WITHIN GOLDEN | `FILE_ADD_FILE` o `FILE_ADD_SUBDIRECTORY` sobre un directorio DEL SUBTREE golden | Nodo mutable (altera membresía del árbol) |
| DELETE OBJECT | `DELETE` sobre el nodo ∨ `FILE_DELETE_CHILD` sobre su directorio contenedor inmediato [^filear] | Nodo mutable |
| REPLACE / RENAME (cadena) | capacidad DELETE OBJECT sobre la fuente ∧ derecho de creación de entrada (`ADD_FILE`/`ADD_SUBDIR`) en el directorio destino | Nodo mutable |
| OWNER SELF-REWRITE | `CHANGE_PERMISSIONS` (`WRITE_DAC`) efectivo por CUALQUIER vía —explícita, heredada o implícita del dueño— salvo ACE del Owner Rights SID que la restrinja [^ownerrights] | Nodo mutable |
| OWNER CHANGE PATH | `WRITE_OWNER` efectivo ∨ privilegio `SeTakeOwnershipPrivilege` presente | Nodo mutable |
| PRIVILEGED WRITE BYPASS | privilegio `SeRestorePrivilege` presente en el token [^ifspriv] [^evt4661] | Todo el árbol mutable |
| PROTECCIÓN DEL ROOT vía parent | parent con `FILE_DELETE_CHILD` (borrar root), o cadena rename del root (delete-child/delete en parent ∧ add en parent) | Árbol comprometido como unidad |
| ESCALATION POR PARENT | `WRITE_DAC`/`WRITE_OWNER` del parent cuando el root depende de herencia del parent (flags de herencia activos): reescribir el parent reescribe lo heredado por el root | Árbol mutable |

Un derecho de creación en el parent FUERA del Golden (crear hermanos del Golden) NO muta el
Golden por sí solo y no dispara UNPROTECTED sin una cadena delete/replace hacia dentro
(GP-T55). Los derechos del parent sólo participan por: delete/rename del root, y escalation
de herencia según la tabla.

Clasificación de privilegios (fuente primaria de máscaras auto-concedidas [^evt4661],
semántica FS [^ifspriv]):

- **Mutation-enabling** (presencia ⇒ UNPROTECTED): `SeRestorePrivilege` (concede
  `GENERIC_WRITE`, `DELETE`, `FILE_ADD_FILE`, `FILE_ADD_SUBDIRECTORY`, `WRITE_DAC`,
  `WRITE_OWNER` saltándose chequeos); `SeTakeOwnershipPrivilege` (permite convertirse en
  dueño → camino dueño→`WRITE_DAC`→reescribir DACL→mutar [^ontt]).
- **Diagnósticos solamentemente** (NO disparan UNPROTECTED; se registran como evidencia):
  `SeBackupPrivilege` (bypass de LECTURA: `READ_CONTROL`, `GENERIC_READ`, `TRAVERSE` — sin
  cadena mutadora por sí solo [^evt4661]); `SeSecurityPrivilege` (acceso a SACL/auditoría:
  ni contenido ni DACL). Ningún otro privilegio entra al contrato sin investigación con
  fuente primaria; presencia de otros queda fuera del modelo v1.

```text
SYNCHRONIZE, TRAVERSE, READ_CONTROL, lecturas: contexto operacional, no deciden estado
(excepto READ_CONTROL necesario para leer el SD: si falta → UNKNOWN).
```

### UNSUPPORTED

Sky-Claw reconoce que **no posee un mecanismo probado** para evaluar la protección en esa
plataforma/filesystem. Es una limitación conocida de capability, decidida en código, no un
resultado observacional. Se decide ANTES de enumerar el árbol (capability gate primero).
Casos v1 (§8): plataforma no-Windows; tipo de unidad no local-fija (`DRIVE_REMOTE` —incluye
toda letra mapeada a share SMB—, `DRIVE_UNKNOWN`, `DRIVE_NO_ROOT_DIR`, etc. [^drivetype]);
ruta UNC; volumen sin ACLs persistentes (FAT/exFAT) [^gvi]; filesystem fuera de la matriz
admitida. Regla: **UNSUPPORTED != UNKNOWN**. No bloquea golden verification, clone ni nada
del vault.

### UNKNOWN

La plataforma/backend **podría** observar el estado, pero evidencia necesaria no pudo
obtenerse o quedó ambigua. Casos sobre CUALQUIER nodo de la superficie (root, descendientes,
parent): security descriptor ilegible o malformado (`ERROR_ACCESS_DENIED` /
`ERROR_INVALID_SECURITY_DESCR` al leerlo [^accesscheck]); consulta de token fallida; fallo
de la llamada AccessCheck; filesystem/drive no determinable tras pasar el gate de plataforma;
nodo que resulta symlink/junction/reparse point no aceptado (la identidad del objeto
inspeccionado difiere del path: fail closed, misma política que `InventoryLinkError` en
RV-1); inconsistencia de metadata entre lstat y apertura; nodo que no puede enumerarse;
drift del árbol entre PRE y POST (entrada agregada, desaparecida o reemplazada); dueño
atribuible con CHANGE_PERMISSIONS medido-denegado sin ACE Owner Rights que lo explique
(§5, owner self-rewrite). Regla dura: **UNKNOWN jamás se promueve a estado protegido**, y
`ANY_UNKNOWN → estado global UNKNOWN`.

### UNPROTECTED

Existe evidencia suficiente de que el token evaluado conserva alguna **capacidad relevante
de mutación** sobre ALGÚN punto del árbol o sobre su superficie compuesta. Agregación
`ANY_MUTABLE_NODE` (más las capacidades compuestas del parent/root de §5): basta UN nodo
mutable, UNA cadena replace viable, UN privilegio mutation-enabling presente, o UN vector
delete/rename/escalation del root vía parent. La evaluación es por nodo según la tabla de
capacidades compuestas; nunca por unión ciega de bits aislados (GP-T55: crear hermanos en
el parent no muta el Golden).

### WRITE_PROTECTED

Contrato mínimo: tras inspección EXHAUSTIVA de `PROTECTED_SCAN_SCOPE` (§5), el token normal/
no elevado evaluado **no posee derechos efectivos** para ninguna capacidad relevante de
mutación en NINGÚN nodo del subtree ni en la superficie compuesta del parent (contenido,
metadata, reescritura de protección, cadena replace, delete-vía-parent, escalation por
herencia), y ningún privilegio mutation-enabling está presente en el token. Lecturas
(`READ_DATA`/`LIST_DIRECTORY`/`EXECUTE`/`TRAVERSE`) quedan registradas como campos de
evidencia, pero su presencia o ausencia NO decide el estado: protección y legibilidad son
capacidades ortogonales (decisión documentada; alternativa rechazada: exigir
`read_allowed=True`, que mezclaría dos ejes y clasificaría un directorio totalmente
inaccesible como no-protegido).

Regla anti-falso-verde central: **"una ACE parece negar WRITE" no es evidencia**. Sólo el
resultado de AccessCheck —que incorpora orden canónico de ACEs [^aceorder], precedencia de
deny [^accesscheck], SIDs deny-only del token filtrado [^uac] [^sidattrs], expansión de
generic rights, semántica Owner Rights [^ownerrights] y mapping archivo/directorio— demuestra
capacidad efectiva. Inferencias textuales del DACL están prohibidas como base de
clasificación.

### HARDENED

Refinamiento estricto de WRITE_PROTECTED (elección OPTION A, análisis completo en §9).
HARDENED ⇔ WRITE_PROTECTED sobre TODO el subtree (§5) **y además**:

1. el token evaluado no está elevado. Gate ÚNICO: `GetTokenInformation(TokenElevation)`
   con `TokenIsElevated == FALSE` [^teletype]. `TokenElevationType` queda como evidencia
   DIAGNÓSTICA exclusivamente y JAMÁS decide el gate: `Default` sólo significa "sin token
   vinculado", condición que ocurre tanto para usuarios estándar como para administradores
   con UAC deshabilitada ejecutándose con privilegios plenos — no es equivalente a
   no-elevado; y
2. `owner_sid` de CADA nodo relevante no es atribuible al token evaluado —no es el user SID
   del token ni ningún grupo del token que no esté marcado deny-only (identidades por SID;
   well-known SIDs por fuente primaria [^wellknown])— o, si algún nodo es propiedad del
   usuario, una ACE del Owner Rights SID (S-1-3-4) restringe explícitamente los derechos
   implícitos del dueño y AccessCheck mide sin `WRITE_DAC` [^ownerrights]. Esto cierra el
   camino implícito dueño→`READ_CONTROL`+`WRITE_DAC` [^msadts] [^ontt] y el camino explícito
   takeown→setowner→reescribir DACL, porque el privilegio que lo alimenta ya fue excluido
   por la conjunción WRITE_PROTECTED.

HARDENED significa exactamente: *contra este token, sin elevación, no existe vía directa
demostrable para mutar ningún punto del Golden ni para retirar la protección*. NO significa
"imposible de desproteger": si el usuario pertenece a Administrators y puede aprobar UAC, un
proceso elegido por él puede elevarse y revertir todo.

**Transporte estructurado del calificador** (no textual): el resultado lleva
`assurance_scope = CURRENT_EFFECTIVE_UNELEVATED_TOKEN` como campo estructurado (§12); el
`message` queda vacío en éxito por contrato. Queda fijado normativamente:
`HARDENED != ADMIN_PROOF`, `HARDENED != SYSTEM_PROOF`, `HARDENED` no afirma resistencia a
elevación UAC posterior ni a compromiso administrativo. El nombre público se conserva
(`HARDENED`) con este scope estructurado; renombrarlo a
`HARDENED_AGAINST_CURRENT_UNELEVATED_TOKEN` fue evaluado y rechazado: verboso para UI sin
añadir precisión que el campo estructurado ya transporta.

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
| Tipo/identidad de CADA nodo de la superficie y detección de reparse points | primitiva canónica `sky_claw.app.security.links` (lstat + `st_reparse_tag`) — la MISMA que usa `inventory_tree`; prohibido reimplementar (ancla `tests/test_links.py`) | Cualquier nodo (root, descendiente o parent) como enlace → UNKNOWN |
| Enumeración estructural PRE/POST del subtree | recorrido read-only equivalente al inventario RV-1 (sin leer contenido); dos pasadas selladas | Drift entre pasadas → UNKNOWN (GP-T39/T40/T41) |
| Localidad del volumen (gate previo) | `GetDriveTypeW` sobre la raíz de la unidad: sólo `DRIVE_FIXED` admitido v1; `DRIVE_REMOTE`, `DRIVE_UNKNOWN`, `DRIVE_NO_ROOT_DIR`, etc. → UNSUPPORTED [^drivetype] | Atrapa letras mapeadas a SMB aunque el FS reporte NTFS |
| Filesystem del volumen | `CreateFileW` (0 access + `FILE_FLAG_BACKUP_SEMANTICS`) + `GetVolumeInformationByHandleW`: nombre FS + flag `FILE_PERSISTENT_ACLS` [^gvi] | Por handle: correcto ante mount points |
| Security descriptor (owner + group + DACL) | `GetNamedSecurityInfoW(SE_FILE_OBJECT, OWNER\|GROUP\|DACL_SECURITY_INFORMATION)` [^gerfa-ej] [^accesscheck] | AccessCheck exige owner y group en el SD (`ERROR_INVALID_SECURITY_DESCR` si faltan); sin SACL (evita requerir SeSecurityPrivilege); READ_CONTROL denegado → UNKNOWN |
| Token: user, grupos y atributos | `GetTokenInformation(TokenUser/TokenGroups)`; grupos deny-only distinguibles por atributos [^sidattrs] | Base del check de owner-attribuable |
| Token: elevación | `GetTokenInformation(TokenElevation)` (+ `TokenElevationType` diagnóstico) [^teletype] | |
| Token: privilegios presentes | `GetTokenInformation(TokenPrivileges)`; LUID resuelto con `LookupPrivilegeValue` (los LUID varían por boot [^priv]) | Presencia, no sólo estado enabled; separados en mutation-enabling vs diagnósticos (§5) |
| ACE Owner Rights (S-1-3-4) presente | hecho estructural del DACL, usado EXCLUSIVAMENTE para interpretar el caso dueño-atribuible-con-WD-denegado (desambiguación, nunca para inferir mutación) | Caso normativo en §5 HARDENED conjunct 2 |
| Acceso efectivo | `AccessCheck` con token de impersonación duplicado (`DuplicateTokenEx`), desired `MAXIMUM_ALLOWED` con `MapGenericMask`, `GENERIC_MAPPING` de archivos [^accesscheck] | Devuelve máscara concedida real; chequeo dirigido adicional de `WRITE_DAC` para el camino implícito del dueño (GP-T47) |

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

### 7.b Allowlist de APIs Win32 / prohibición de mutadores

Contrato NORMATIVO para la futura implementación GP1 y ancla de code-review. El backend GP1
sólo puede invocar APIs de esta lista; cualquier otra API Win32 con potencial mutador queda
FORBIDDEN_IN_GP1.

| API | READ_ONLY | Por qué la necesita GP1 | Riesgo de mutación si se abusa |
|---|---|---|---|
| `GetNamedSecurityInfoW` | Sí | owner/group/DACL de cada nodo | Ninguno intrínseco (variante Get) |
| `AccessCheck` | Sí | acceso efectivo del token | Ninguno |
| `GetTokenInformation` | Sí | user/grupos/elevación/privilegios | Ninguno |
| `OpenProcessToken` / `OpenThreadToken` | Sí | obtener el handle del token propio (proceso; thread impersonante como vía preferente), acceso restringido a `TOKEN_QUERY` | Ninguno con `TOKEN_QUERY` |
| `CloseHandle` | Sí | liberar TODO handle obtenido (abiertos y duplicados) | Ninguno (su ausencia sería leak de handles) |
| `DuplicateTokenEx` | Sí* | duplicar token propio a impersonación para AccessCheck | *No altera privilegios ni grupos (copia); prohibido usarlo como base de AdjustTokenPrivileges en GP1 |
| `GetVolumeInformationByHandleW` | Sí | nombre FS + FILE_PERSISTENT_ACLS | Ninguno |
| `GetDriveTypeW` | Sí | gate de localidad (DRIVE_FIXED) | Ninguno |
| `CreateFileW` | Sólo con parameterización fija: desired access `0` o `FILE_READ_ATTRIBUTES`, flags `FILE_FLAG_BACKUP_SEMANTICS` | handle para metadatos de volumen | CUALQUIER otro disposition/access/flags está FORBIDDEN (sería apertura mutadora) — anclado por test |
| `ConvertSidToStringSidW` | Sí | render S-1-… sin nombres localizados | Ninguno |
| `LookupPrivilegeValueW` / `LookupPrivilegeNameW` | Sí | resolver LUID↔constante de privilegio [^priv] | Ninguno |
| `LocalFree` / gestión de memoria | Sí | liberar buffers devueltos | Ninguno |

FORBIDDEN_IN_GP1 (lista no exhaustiva; la regla general es "toda API que no esté en la
allowlist"): `SetNamedSecurityInfoW`, `SetFileSecurityW`, `SetSecurityInfo`, `SetFileAttributesW`,
`DeleteFileW`, `RemoveDirectoryW`, `MoveFileW*`, `CreateHardLinkW`, `CreateSymbolicLinkW`,
`SetFileInformationByHandle`, `AdjustTokenPrivileges`, `InitiateSystemShutdown*`, y
`CreateFileW` fuera de la parameterización permitida. `AdjustTokenPrivileges` es doblemente
prohibido: además de mutar el propio token, sería la vía para habilitar un privilegio
mutation-enabling presente-aunque-deshabilitado durante la inspección, invalidando la
medición.

Anclas de test asociadas: GP-T20 (allowlist AST + monkeypatch Python-level), GP-T49
(invocación de API nativa mutadora ⇒ fallo), GP-T50 (SD/metadata del árbol idénticos antes/
después; TreeDigest ≠ integridad de ACL).

## 8. Capability del filesystem

Orden de gates (todos ANTES de enumerar el árbol): plataforma → localidad de la unidad →
filesystem → ACLs persistentes.

1. **¿Cómo saber qué unidad/volumen contiene el path?** `GetDriveTypeW` sobre la raíz:
   sólo `DRIVE_FIXED` es candidato v1. `DRIVE_REMOTE` (toda letra mapeada a una share SMB,
   aunque el servidor reporte NTFS y el SD llegue legible) → UNSUPPORTED: el chequeo de
   acceso lo ejecuta el servidor contra SU representación del token, así que el modelo de
   acceso local no representa al consumidor real [^drivetype]. `DRIVE_UNKNOWN`,
   `DRIVE_NO_ROOT_DIR`, `DRIVE_CDROM`, `DRIVE_RAMDISK`, `DRIVE_REMOVABLE` → UNSUPPORTED v1
   (fail closed). Rutas `\\?\`/subst que resuelven a volumen fijo local heredan la decisión
   del volumen real vía los APIs por-handle.
2. **¿Qué filesystem?** Handle propio del path + `GetVolumeInformationByHandleW` → nombre
   ("NTFS", "ReFS", "exFAT", …) y flags, entre ellos `FILE_PERSISTENT_ACLS` = "el volumen
   preserva y hace cumplir ACLs (NTFS sí, FAT no)" [^gvi].
3. **Matriz v1**: se proclama soporte sólo donde el contrato puede demostrarse y testearse:

| Entorno | Estado v1 | Razón |
|---|---|---|
| Windows + unidad `DRIVE_FIXED` + NTFS + `FILE_PERSISTENT_ACLS` | SOPORTADO | Modelo ACL completo, acceso local al objeto; único combinación validable en CI/rig hoy (`LOCAL_WINDOWS_NTFS_ONLY`) |
| Letra mapeada a share SMB (aunque reporte NTFS) | UNSUPPORTED | `DRIVE_REMOTE` [^drivetype]; autoridad de acceso remota (GP-T35) |
| UNC / red / SMB directo | UNSUPPORTED | Ídem anterior |
| exFAT/FAT | UNSUPPORTED estructural | Sin ACLs persistentes [^gvi]: no hay nada que evaluar |
| ReFS | UNSUPPORTED (candidato GP1.1) | Mantiene ACLs (flag persistente y soporte documentado de la API [^gvi]), PERO el flag por sí solo NO proclama soporte: exige investigación formal + validación en rig antes de entrar a la matriz — regla anti-optimismo |
| FS desconocido/tercero con flag de ACLs | UNSUPPORTED | Fail closed: nombre fuera de matriz |
| Unidad no fija / tipo indeterminable | UNSUPPORTED | Gate de localidad [^drivetype] |
| Linux/macOS | UNSUPPORTED | Backend v1 es Windows-only; el vault sigue funcionando sin esta capability |

4. **NTFS no es requisito del Runtime Vault**: `UNSUPPORTED_BLOCKS_RUNTIME_VAULT=NO`;
   verification/clone no consultan esta capability jamás (§11).

## 9. Decisión semántica de HARDENED

**OPTION A — "Endurecido frente al token actual no elevado"** (ELEGIDA).
HARDENED ⇔ la definición completa de §5 HARDENED: WRITE_PROTECTED sobre TODO el subtree ∧
gate único de no-elevación (`TokenElevation`) ∧ sin vías efectivas de reescritura
DACL/ownership ∧ relación de ownership sin camino de auto-reescritura —incluida la
excepción Owner Rights S-1-3-4— ∧ ningún privilegio mutation-enabling presente.
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

**Revalidación tras la ronda de corrección (#504)**: con (a) cobertura exhaustiva del
subtree, (b) capacidades compuestas en vez de bits sueltos, (c) separación de privilegios
mutation-enabling vs diagnósticos, (d) semántica Owner Rights documentada y (e) transporte
estructurado del scope, la definición A sigue siendo 100% demostrable read-only y sin
sobreafirmaciones. Resultado: `HARDENED_DEFINITION_STATUS=RETAINED_WITH_STRUCTURED_SCOPE`.
La definición vigente es la de §5 HARDENED; su alcance viaja en `assurance_scope`
(§12), jamás en texto humano.

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

Ejemplos normativos (el safety qualifier vive en metadata estructurada, NUNCA en message):

```text
success=True  state=UNPROTECTED      message=""   # válido: inspección exitosa que descubre vulnerabilidad
success=True  state=WRITE_PROTECTED  message=""   # válido; scope en assurance_scope/scan_scope
success=True  state=HARDENED         message=""   # válido; NO implica admin-proof (§5)
success=True  state=UNSUPPORTED      message=""   # válido: capability probe exitoso y concluyente
success=False state=UNKNOWN          message≠""   # único estado con success=False
```

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

Anclas de test que congelan esta ortogonalidad: GP-T19 y GP-T51/T52/T53 (comportamental:
ningún estado de protección altera VERIFIED ni la elegibilidad de clone), GP-T34 (AST:
`runtime_vault.clone`/`models`) y GP-T54 (AST/import-graph sobre TODO el core
`inventory`/`verification`/`golden`/`clone`/`models`, con la regla de dirección §13 —
patrón enumerativo del repo).

## 12. API/modelo propuesto (sin implementar)

```python
class GoldenProtectionState(StrEnum):
    UNSUPPORTED = "unsupported"
    UNPROTECTED = "unprotected"
    WRITE_PROTECTED = "write_protected"
    HARDENED = "hardened"
    UNKNOWN = "unknown"


class GoldenProtectionRight(StrEnum):
    """Derechos efectivos relevantes, ya resueltos por AccessCheck por nodo."""
    READ_DATA = "read_data"             # FILE_READ_DATA / FILE_LIST_DIRECTORY
    EXECUTE = "execute"                 # FILE_EXECUTE / FILE_TRAVERSE
    WRITE_CONTENT = "write_content"     # WRITE_DATA+APPEND ≡ ADD_FILE+ADD_SUBDIR según nodo
    DELETE = "delete"                   # DELETE del nodo
    DELETE_CHILD = "delete_child"       # FILE_DELETE_CHILD del directorio evaluado
    WRITE_METADATA = "write_metadata"   # FILE_WRITE_ATTRIBUTES + FILE_WRITE_EA
    CHANGE_PERMISSIONS = "change_permissions"  # WRITE_DAC (incluye la vía implícita del dueño)
    CHANGE_OWNER = "change_owner"       # WRITE_OWNER


@dataclass(frozen=True, slots=True)
class NodeProtectionObservation:
    """Evidencia cruda de UN nodo; campo None = no observable (posible causa UNKNOWN)."""
    relative_path: str
    node_kind: str                                        # "dir" | "file"
    owner_sid: str | None                                 # S-1-… (ConvertSidToStringSid)
    granted_rights: frozenset[GoldenProtectionRight] | None
    owner_rights_ace_present: bool | None                 # ACE S-1-3-4 en el DACL (diagnóstico §5)


@dataclass(frozen=True, slots=True)
class GoldenProtectionEvidence:
    platform: str                                         # p.ej. "windows"; siempre observable
    drive_type: int | None                                # GetDriveTypeW (gate de localidad §8)
    filesystem: str | None                                # GetVolumeInformationByHandleW
    filesystem_persistent_acls: bool | None               # flag FILE_PERSISTENT_ACLS
    current_user_sid: str | None                          # user SID del token evaluado
    current_token_elevated: bool | None                   # TokenElevation
    mutation_privileges_present: frozenset[str] | None    # {"SeRestorePrivilege","SeTakeOwnershipPrivilege"}
    diagnostic_privileges_present: frozenset[str] | None  # {"SeBackupPrivilege","SeSecurityPrivilege"} (§5)
    parent_observation: NodeProtectionObservation | None  # parent del root (delete/rename del root)
    nodes: tuple[NodeProtectionObservation, ...] | None   # TODO el subtree, orden determinista
    pre_post_structural_match: bool | None                # sello best-effort PRE==POST (§5)


@dataclass(frozen=True, slots=True)
class GoldenProtectionResult:
    state: GoldenProtectionState
    evidence: GoldenProtectionEvidence                    # siempre presente; campos None-ables
    message: str = ""                                     # "" en success (contrato repo)
    scan_scope: str = "FULL_SUBTREE_AND_PARENT"           # constante v1 (nombre definitivo abierto)
    assurance_scope: str = "CURRENT_EFFECTIVE_UNELEVATED_TOKEN"  # constante v1; transporta el
    # calificador de HARDENED ESTRUCTURADAMENTE: nunca en message, nunca inferido por el lector.

    @property
    def success(self) -> bool: ...                        # True SOLO en los 4 estados definitivos (§10)
```

Justificación campo por campo (los `*_allowed` booleanos propuestos en la tarea se
RECHAZAN como campos planos duplicados: los derechos viajan como frozenset por nodo y cada
booleano es derivable con `right in observation.granted_rights`):

| Campo | ¿Necesario? | ¿Evidence o derivado? | ¿None posible? | Invariante |
|---|---|---|---|---|
| state/message/success | contrato del repo | derivado de la clasificación agregada | message="" salvo UNKNOWN | §10 |
| evidence.platform | gate de UNSUPPORTED | evidence | no | `"windows"` ⇒ backend disponible |
| drive_type | gate de localidad §8 (mapped SMB ≠ NTFS local) | evidence | sí (si no llegó a observarse) | estado protegido ⇒ `DRIVE_FIXED` |
| filesystem, filesystem_persistent_acls | gate de matriz §8 | evidence | sí | ambos None juntos o juntos informados |
| nodes[].owner_sid | conjunción HARDENED + diagnóstico | evidence | sí (ilegible ⇒ UNKNOWN) | string SIDL, jamás nombre de cuenta |
| nodes[].granted_rights | clasificación compuesta §5 + diff pre/post GP2/GP3 | derivado de AccessCheck, conservado crudo | sí | `WRITE_PROTECTED/HARDENED ⇒ ninguna capacidad compuesta mutable en ningún nodo` |
| nodes[].owner_rights_ace_present | desambiguación del caso dueño-con-WD-denegado (§5) | evidence estructural | sí | jamás usado para inferir mutación |
| current_user_sid | check owner-attribuable + tests T14–T16 | evidence | sí | idem SID string |
| current_token_elevated | conjunción HARDENED | evidence | sí | `HARDENED ⇒ current_token_elevated is False` |
| mutation_privileges_present | anti-bypass (SeRestore/SeTakeOwnership) | evidence | sí | `WRITE_PROTECTED/HARDENED ⇒ conjunto vacío` |
| diagnostic_privileges_present | contexto operacional (lectura backup, SACL) sin efecto en estado | evidence | sí | NO participa de la clasificación (GP-T44) |
| parent_observation | vectores delete/rename del root + escalation por herencia | evidence | sí | cubierto por capacidades compuestas §5 |
| nodes / pre_post_structural_match | propiedad del árbol + sello fail-closed | evidence | sí | estados protegidos ⇒ `pre_post_structural_match is True` ∧ cobertura completa |
| scan_scope / assurance_scope | transporte estructurado del calificador (Finding HARDENED-scope) | constantes v1 | no | nombres definitivos decidibles al implementar; semántica normativa fijada en §5 |

Firmas propuestas (sólo diseño): `inspect_golden_protection(path: pathlib.Path) ->
GoldenProtectionResult` (sync; O(nodos) declarado, I/O bloqueante corto — el caller async
envuelve con to_thread, patrón del repo) y excepción
`GoldenProtectionInputError(RuntimeVaultError)`.

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
| GP-T20 | el inspector no usa APIs de mutación a nivel Python (monkeypatch de `os.*`, `pathlib`, `open`, builtins) NI APIs Win32 nativas fuera de la allowlist §7.b (ancla enumerativa sobre la lista FORBIDDEN_IN_GP1) | false-green |
| GP-T21 | sin dependencia de subprocess/icacls si el backend es API (AST anchor: módulo sin `subprocess`, sin literales "icacls"/"takeown") | ancla |
| GP-T22 | el inspector no recomputa el tree digest (AST: protection no importa verification/inventory) | rendimiento/frontera |
| GP-T23 | symlink/junction en CUALQUIER nodo de la superficie (root, descendiente o parent) → UNKNOWN (fail closed), usando la primitiva canónica de links | fail-closed |
| GP-T24 | path inexistente → excepción input-error; path-file → excepción input-error; nunca result verde | taxonomía |
| GP-T25 | invarianzas success/state paramétricas sobre los 5 estados (§10) | contracto |
| GP-T26 | deny explícita precede allow amplia → derecho denegado pese al allow (DACL sintética) | adversarial/false-green |
| GP-T27 | privilegio mutation-enabling presente aunque disabled (`SeRestorePrivilege`/`SeTakeOwnershipPrivilege`) en evidencia → UNPROTECTED, nunca WRITE_PROTECTED | adversarial/false-green |
| GP-T28 | evidencia de token elevado → nunca HARDENED | adversarial |
| GP-T29 | parent con FILE_DELETE_CHILD efectivo aunque root restrictivo → UNPROTECTED | adversarial/false-green |
| GP-T30 | DACL NULL → UNPROTECTED; DACL vacía OBSERVADA + todas las checks de mutación completas incluida la regla del dueño → WRITE_PROTECTED; descriptor ilegible (sin READ_CONTROL ni ownership) → UNKNOWN, jamás protegido | adversarial |
| GP-T31 | idempotencia: dos inspecciones consecutivas producen resultados iguales y el árbol no cambia | estabilidad |
| GP-T32 | owner SID no resoluble a string → UNKNOWN | observación |
| GP-T33 | UNC path → UNSUPPORTED | matriz |
| GP-T34 | AST: `runtime_vault.clone` y `runtime_vault.models` no referencian el módulo de protección (ortogonalidad enumerada, estilo test_ritual_dispatch) | ortogonalidad/ancla |
| GP-T35 | unidad mapeada remota (SMB montado como letra, p.ej. Z:\) → UNSUPPORTED aunque el FS reporte NTFS (`DRIVE_REMOTE`) | matriz/windows |
| GP-T36 | descendiente con ACE explícita escribible (herencia intacta en el resto) → UNPROTECTED | subtree/false-green |
| GP-T37 | descendiente con herencia deshabilitada + acceso efectivo escribible → UNPROTECTED | subtree/false-green |
| GP-T38 | security descriptor de un descendiente ilegible → UNKNOWN global | subtree/fail-closed |
| GP-T39 | descendiente aparece durante la inspección (drift PRE/POST) → UNKNOWN | subtree/fail-closed |
| GP-T40 | descendiente desaparece durante la inspección (drift PRE/POST) → UNKNOWN | subtree/fail-closed |
| GP-T41 | descendiente reemplazado por nodo de identidad distinta durante la inspección → UNKNOWN | subtree/fail-closed |
| GP-T42 | todos los descendientes protegidos (+root+parent OK) → elegible WRITE_PROTECTED | subtree |
| GP-T43 | todos los descendientes protegidos + conjunciones HARDENED → HARDENED | subtree |
| GP-T44 | `SeBackupPrivilege` solo NO produce UNPROTECTED (queda como evidencia diagnóstica) | privilegios/false-green |
| GP-T45 | `SeRestorePrivilege` presente bloquea estado protegido (capacidad de mutación) | privilegios |
| GP-T46 | `SeTakeOwnershipPrivilege` presente bloquea estado protegido según contrato final | privilegios |
| GP-T47 | owner implícito: directorio propiedad del usuario actual con DACL que no le concede WD ⇒ CHANGE_PERMISSIONS efectivo por vía del dueño → UNPROTECTED (y con ACE Owner Rights restrictiva, se respeta la medición) | owner/false-green |
| GP-T48 | el descriptor construido para AccessCheck incluye OWNER+GROUP+DACL (sin SACL); SD sin group → UNKNOWN, no crash | backend |
| GP-T49 | invocación de cualquier API nativa mutadora (FORBIDDEN_IN_GP1 §7.b) durante la inspección ⇒ fallo de test | anti-mutación |
| GP-T50 | security descriptors y metadata relevante del árbol IDÉNTICOS antes/después de la inspección (TreeDigest ≠ integridad de ACL: se comparan ambos ejes) | anti-mutación |
| GP-T51 | VERIFIED + protection=UNPROTECTED no altera el resultado RV-2 (sigue VERIFIED) | ortogonalidad/comportamental |
| GP-T52 | VERIFIED + protection=UNKNOWN no bloquea RV-3 clone | ortogonalidad/comportamental |
| GP-T53 | VERIFIED + protection=UNSUPPORTED no bloquea RV-3 clone | ortogonalidad/comportamental |
| GP-T54 | AST/import-graph: NINGUNO de `inventory.py`, `verification.py`, `golden.py`, `clone.py`, `models.py` importa el módulo de protección (extiende GP-T34 a todo el core RV-1/RV-2/RV-3) | ortogonalidad/ancla |
| GP-T55 | parent permite sólo crear hermanos (ADD_FILE/ADD_SUBDIR), sin delete-child ni cadena replace hacia el Golden → NO se clasifica UNPROTECTED por ese hecho | composición/false-green |

Regla de dependencias que GP-T54 congela (dirección única): el inspector de protección PUEDE
reutilizar helpers read-only del core Runtime Vault; el core
(`inventory.py`/`verification.py`/`golden.py`/`clone.py`/`models.py`) NO DEBE importar ni
exigir estado de protección para sus contratos existentes de autorización/integridad.
Complementos comportamentales: T51/T52/T53.

Resumen: **55 tests** propuestos; false-green explícitos: T18, T20, T26, T27, T29, T30,
T36, T37, T38–T41 (drift→UNKNOWN), T44, T47, T49, T50, T55; Windows-específicos: T04–T12,
T13, T17, T23, T29, T30, T33, T35, T36–T46, T47–T50 (skipif no-win32 donde corresponda);
portabilidad: T01, T02, T03, T24, T25, T34, T51–T54 corren en cualquier plataforma.

## 14. Limitaciones de seguridad (declaradas)

1. **No protege contra C/E/F**: un proceso elevado por consentimiento del usuario, SYSTEM u
   otro servicio privilegiado puede reescribir owner/DACL y saltarse toda la protección
   [^ontt] [^ifspriv]. HARDENED es un statement sobre el token evaluado, no sobre el mundo.
2. **Costo O(nodos) del escaneo exhaustivo**: la cobertura total del subtree es el precio
   de no prometer "root protegido ⇒ Golden protegido" (falso verde eliminado por diseño).
   Para ~16k nodos es factible; cacheable por hash de descriptor único si hiciera falta.
   La observación es best-effort sellada (PRE/evidencia/POST): NO afirma snapshot atómico;
   cualquier drift detectado degrada a UNKNOWN (§5).
3. **TOCTOU residual**: entre el sello POST y cualquier uso posterior del resultado, un actor
   con derecho de cambio podría alterarlo; la revalidación reduce la ventana sin eliminarla
   (mismo límite declarado que `inventory_tree`).
4. **UAC puede revertir HARDENED**: la pertenencia potencial a Administrators + aprobación
   UAC no es observable de forma confiable y NO participa del contrato (OPTION C rechazada).
5. Los atributos ReadOnly del filesystem NO cuentan como protección (cualquier herramienta
   los limpia; no son un control de acceso).

## 15. Futuro GP2/GP3 y Execution Guard (sólo restricciones)

**GP2 — Protect Golden** (MUTATING, futuro): HITL obligatorio; autorización EXCLUSIVAMENTE
vía UAC/elevación de Windows (consentimiento del SO). **Nunca** pedir, escribir ni guardar
passwords/credenciales en Sky-Claw. Restricciones de seguridad registradas para su diseño
futuro (NO se diseña acá):

1. **Inventario pre-mutación COMPLETO por nodo**: antes de cualquier mutación, capturar el
   security descriptor de TODOS los nodos que GP2 pueda modificar (no sólo el root), cada
   registro ligado como mínimo a: relative path, identidad de filesystem, tipo de nodo,
   descriptor original y estado de herencia.
2. **Inventario sellado**: el inventario queda cerrado antes de la primera mutación; si un
   nodo cambia después de la captura → fail closed.
3. Representación del descriptor original (self-relative SD serializado vs representación
   canónica segura): decisión abierta, deliberadamente no tomada acá.
4. Aplicar protección uniformemente heredable al subtree; re-inspeccionar con GP1 esperando
   HARDENED; re-verificar digest RV-2 intacto; journal de mutación; fail-closed si la
   verificación post-cambio no cuadra.

**GP3 — Restore/Unprotect** (MUTATING, futuro): HITL + UAC; restaura el estado POR NODO del
inventario capturado por GP2 (jamás "aplicar ACL del root recursivamente" ni inferir desde
el root), re-inspecciona con GP1 y re-verifica digest VERIFIED. Verified recovery, sin atajos.

**Execution Guard** (feature futura, fuera de GP1 y de RV-3): `reference_only` → no
seleccionable como execution target. Es una decisión de la capa de selección/lanzamiento de
runtimes (territorio RV-5/MO2), NO una propiedad ACL. `GoldenMasterDescriptor.role` ya lleva
el dato; el enforcement no se diseña acá.

GP1 no impide ninguno de los tres: no introduce estados que exijan acoplamiento, no consume
la nomenclatura RV-4, y su backend read-only es subconjunto del que GP2/GP3 necesitarán.

## 16. Preguntas abiertas

1. ~~Actualizar el índice de `docs/adr/README.md`~~ → resuelto en la ronda de corrección
   (índice actualizado a 0001–0009 en el mismo PR).
2. Validar ReFS en rig real para promoverlo a matriz de soporte en GP1.1 (el flag
   FILE_PERSISTENT_ACLS por sí solo NO proclama soporte, §8).
3. ¿Cache por hash de descriptor único para acelerar re-inspecciones del subtree completo?
   (optimización, no contrato).
4. Redacción final del mensaje de UI para HARDENED (debe transmitir "frente al uso normal",
   no "intocable"); el dato estructurado ya no depende del texto.
5. Nombre definitivo de la excepción de input (`GoldenProtectionInputError` propuesto) y de
   los enums `scan_scope`/`assurance_scope` (semántica ya normativa en §5/§10).

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
[^ownerrights]: WELL_KNOWN_SID_TYPE — learn.microsoft.com/windows/win32/api/winnt/ne-winnt-well_known_sid_type (`WinCreatorOwnerRightsSid = 71`, el Owner Rights SID S-1-3-4); semántica documentada en la tabla de well-known SIDs de Windows: cuando un ACE con este SID aplica al objeto, el sistema ignora los permisos implícitos READ_CONTROL y WRITE_DAC del dueño.
[^drivetype]: GetDriveTypeW — learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-getdrivetypew (DRIVE_FIXED=3; DRIVE_REMOTE=4 unidad remota/de red; DRIVE_UNKNOWN=0; DRIVE_NO_ROOT_DIR=1).

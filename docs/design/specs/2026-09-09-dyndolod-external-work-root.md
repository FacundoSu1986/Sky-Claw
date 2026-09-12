# DynDOLOD PR-2: contrato de trabajo externo

> **Estado:** decisión arquitectónica cerrada por [ADR 0011](../../adr/0011-dyndolod-external-work-root.md)
> (Aceptada). **P0 IMPLEMENTADO** (preferencia, binding v1, admisión, A–H,
> single-winner cross-process, coordinación durable y transición restart-only:
> `sky_claw/local/tools/dyndolod_workspace.py`,
> `sky_claw/app/security/known_folders.py`); **P2.1 (derivación + per-tool
> `-o:`) IMPLEMENTADO COMO CANDIDATO** (rama `feat/dyndolod-pr2-external-staging`,
> PR-2 `DRAFT`): los subroots de §7 ya llegan al `-o:`. **P2.2 (servicio,
> transacción y packaging) IMPLEMENTADO COMO CANDIDATO** en la misma rama: el
> `WorkspaceResuelto` productivo llega al servicio con su fence, el born-empty
> cubre el root completo por herramienta y el packaging es disjunto. **P2.3
> (recovery/migración) sigue NO IMPLEMENTADO**: el barrido de arranque de los
> backups `<family>/<Tool>.rollback-*` es su deuda explícita. El gate de
> lanzamiento inicial quedó cerrado por T5-v2
> (2026-09-10 — [`docs/validation/2026-09-10_t5v2_dyndolod_stage9.md`](../../validation/2026-09-10_t5v2_dyndolod_stage9.md):
> T5-V2 LAUNCH GATE: PASS; T5-V2 FULL CHECKLIST: PARTIAL 7/10) **sobre el builder
> viejo**; P2.1 lo **REABRE** al mutar los subroots de salida usados por `-o:`.
> **Baseline:** `origin/main` `5e5e9448db0d4015b3bf0dc4c1df10fdc49e226c`,
> verificado el 2026-09-09 mediante fetch y lectura de código.
> **Alcance:** lifecycle, identidad, propiedad, admisión y fronteras de PR-2.
> **Ejecución:** [plan y procedimiento de aceptación](../plans/2026-09-09-dyndolod-pr2-resolution.md).

## 1. Decisión

`external_work_root` es una **preferencia persistente del usuario que apunta a un
directorio de estado de trabajo recuperable, exclusivo de una instancia gestionada**.
Su contenido no es configuración, instalación ni caché descartable. Propiedades
normativas (ADR 0011 §2.1):

```text
persistente entre cierres de Sky-Claw
persistente entre reinicios de Windows
externo al juego
externo a MO2
externo a directorios de instalación de herramientas
no cache descartable
no TEMP
elegible en otro volumen local
exclusivo por instancia
```

**Ausencia de la preferencia ⇒ DynDOLOD administrado NO CONFIGURADO.** El resto de
Sky-Claw arranca. Fallbacks prohibidos: `game`, `game parent`, MO2, `install_dir`,
`%TEMP%`, `Documents`, `C:\Sky-Claw`. No hay variable de entorno nueva en esta
entrega.

### Alternativas evaluadas

Ver [ADR 0011 §3](../../adr/0011-dyndolod-external-work-root.md): base global con
subcarpetas hash, derivación de `install_dir`/juego/`%TEMP%`, hot-reload, variable
de entorno y promesa de concurrencia — todas rechazadas, con el motivo.

## 2. Identidad y binding

**La tupla de paths NO es identidad durable** (ADR 0011 §2.2). Dos conceptos con
significado congelado:

- **`resource_binding`** — identifica los **recursos de la instancia lógica**
  observada: `game_path`, `mo2_instance_data_root`, `mo2_mods_path`,
  canonicalizados con las primitivas existentes (`resolve(strict=False)` no
  estricto como `_canonicalizar` de `path_resolver.py`; comparación contra el
  sandbox con `PathValidator`).
- **`binding_id`** — UUID generado **una única vez** al inicializar un work
  root; identifica el **ownership concreto** de ese `external_work_root`.
  Nunca por hash de paths; nunca hostname/usuario/SID.

**Regla de identidad:** dos configuraciones que resuelven el mismo
`resource_binding` son la misma instancia lógica, y una misma instancia lógica
**no puede tener dos `external_work_root` activos simultáneamente**. Cambiar de
root exige una transición/rebind explícita; PR-2 NO implementa rebind automático
(casos G y H de la máquina de estados). La detección/rechazo de "mismo
`resource_binding` en dos roots" es instalacional: la provee el estado durable
de coordinación de etapa 9 (prerrequisito P0.2 del plan), no el JSON local del
binding, que sólo describe su propio root.

El perfil MO2 **no**
crea otro work root: los mods empaquetados se comparten entre perfiles de una
instancia; cambiar perfil invalida el contexto del handoff según su contrato
vigente y no reutiliza su evidencia.

## 3. Metadata de propiedad

```text
<external_work_root>/.sky-claw-binding.json
```

El archivo está **fuera** de `DynDOLOD/TexGen` y `DynDOLOD/DynDOLOD` (subárboles
que serán targets de move-aside/born-empty). No colisiona con convenciones del
repo: Sky-Claw ya usa estado con punto (`.skyclaw_backups`); la diferencia
deliberada es que este binding vive dentro del root elegido por el usuario para
que la propiedad viaje con el directorio.

**Schema v1 congelado:**

```json
{
  "schema_version": 1,
  "binding_id": "<uuid>",
  "resource_binding": {
    "game_path": "<canonical>",
    "mo2_instance_data_root": "<canonical>",
    "mo2_mods_path": "<canonical>"
  }
}
```

Los tres campos de evidencia viven **sólo** como objeto anidado
`resource_binding`; no existen representaciones planas contractuales.
`config_path` **NO pertenece al binding**: puede aparecer como procedencia en
logs/diagnóstico fuera del archivo, nunca como metadata contractual. Política
de campos extra, explícita: cualquier campo fuera de este schema (raíz o dentro
de `resource_binding`) es schema desconocido → caso E, RECHAZAR fail-closed;
extender el schema exige `schema_version` nueva y su ADR. Sin timestamps.

El writer **no se implementó en el PR documental**; P0 lo implementó después
(`dyndolod_workspace.publicar_binding` / `_crear_binding_exclusivo` /
`reescribir_binding_propio`), respetando lo que la spec declaraba:

- creación inicial **single-winner no-reemplazante**: exactamente un proceso
  publica el binding; el perdedor NO reemplaza el archivo del ganador — lo
  relee, valida el binding publicado y continúa sólo si el `resource_binding`
  publicado es compatible (si no, fail-closed). `os.replace()` por sí solo NO
  provee esta propiedad: exige una primitiva adecuada (creación exclusiva,
  lock cross-process alrededor del check + publish, u otro mecanismo
  equivalente con la propiedad demostrable). **Implementado con `os.open` +
  `O_CREAT | O_EXCL`**; T-PR2-22 se valida con dos procesos reales
  (`test_single_winner_entre_dos_procesos_reales`), no con coroutines;
- escritura **atómica con `os.replace`** — sólo para actualizaciones donde
  reemplazar es contractualmente válido (temporal + `os.replace` en el mismo
  directorio, como `Config.save()` y los serializadores de `local_config.py`);
- **nunca reescribir silenciosamente un binding ajeno**;
- **metadata corrupta o schema desconocido = fail-closed** (caso E, incluye
  campos fuera del schema v1).

## 4. Máquina de estados del root (normativa)

| Caso | Estado del root | Veredicto |
|---|---|---|
| A | Root ausente | Permitido inicializar |
| B | Root existente y vacío, sin binding | Permitido inicializar |
| C | Root con binding válido para los mismos recursos | Permitido utilizar |
| D | Root no vacío, sin binding | **RECHAZAR** — no adoptar contenido automáticamente |
| E | Metadata corrupta / schema desconocido (incluye cualquier campo fuera del schema v1) | **RECHAZAR fail-closed** |
| F | Binding perteneciente a otros recursos | **RECHAZAR** |
| G | Binding propio cuyo `resource_binding` ya no coincide con la resolución actual | **RECHAZAR** — requiere transición/rebind explícito; PR-2 NO implementa rebind automático |
| H | Root con binding válido para los mismos recursos, pero ese `resource_binding` ya tiene otro `external_work_root` activo | **RECHAZAR** — requiere transición/rebind explícito, igual que G. Detección instalacional vía el estado durable de coordinación (P0.2 del plan), no vía el JSON local |

C y F se resuelven por comparación exacta del `resource_binding` contra la
resolución canonicalizada del arranque; H consulta además el estado de
coordinación. No hay tercera vía ni degradación.

## 5. Cambio de preferencia

Cambiar `external_work_root` **no hace hot-reload del pipeline**:

```text
persistir nueva preferencia → aplicar en próximo arranque de Sky-Claw
```

Evita mutar en caliente `AppContext`, las raíces de `PathValidator`, el runner
cacheado, el rollback reconciler y las transacciones activas. No se diseña hot
reload para PR-2. Persistencia con `escribir_campo` y los wrappers bloqueantes de
`local_config.py`, con el merge-on-save de `Config` — nunca JSON sobre el TOML ni
asignaciones muertas.

## 6. Validación de path

Propiedades de admisión (ADR 0011 §2.7):

- absoluta; filesystem local; no root de volumen; no UNC/network en PR-2;
- puede vivir en otro volumen local (si admite rename y locks);
- **no solapa en NINGUNA dirección** con: game; Data; SteamApps; MO2 install; MO2
  instance data root; profiles; mods; overwrite; DynDOLOD/TexGen install dirs;
  TEMP (`tempfile.gettempdir()`); y el **conjunto cerrado de Windows Known
  Folders prohibidos**, v1: `Documents`, `Desktop`, `Downloads` — resueltos por
  identificador con la API de Known Folders de Windows (ruta efectiva vigente,
  incluidas redirecciones a otros volúmenes), nunca derivados de
  `%USERPROFILE%` ni por substrings. Justificación y cierre del conjunto en
  [ADR 0011 §2.7](../../adr/0011-dyndolod-external-work-root.md);
- **no amplía `PathValidator`** autorizando toda una unidad o ancestro amplio:
  sólo el root admitido + destinos derivados (familia y metadatos);
- sin búsqueda ingenua de substrings (`"Steam"`, `"OneDrive"`) para establecer
  identidad; sin detección universal de proveedores cloud;
- symlinks/junctions/reparse points — propiedad:

```text
ninguna mutación administrada puede resolver fuera del external_work_root
admitido mediante un componente redirigido.
```

La implementación reutiliza las primitivas existentes
(`sky_claw/app/security/links.py` — `is_link`/`link_kind`/`rmtree_link_aware` — y
`PathValidator.validate` con `strict_symlink`) antes de inventar un subsistema.
**P2.2 cierra la ventana de un reparse introducido DESPUÉS del boot** con
`links.exigir_contencion_fisica`: recorre la cadena de componentes de
`external_work_root` al destino con `lstat` (sin seguir enlaces), exige que cada
componente existente sea un directorio real y revalida su identidad antes de
devolver; se cablea antes del move-aside, antes de crear el root vacío, antes de
cada spawn y antes de cada packaging. La comparación de rutas resueltas no
alcanza: con un ancestro redirigido, candidato y tool root resuelven al mismo
árbol externo y la relación lógica sigue siendo verdadera.
El mecanismo de Known Folders **no existía** en el árbol: P0 lo construyó en
`sky_claw/app/security/known_folders.py` con el conjunto congelado de la ADR §2.7
y su test parametrizado (cada Known Folder contractual, incluida una ruta
redirigida a otro volumen, → `external_work_root` rechazado).

## 7. Layout

```text
external_work_root/
├── .sky-claw-binding.json
└── DynDOLOD/
    ├── TexGen/
    │   └── textures/...
    └── DynDOLOD/
        └── ...
```

```text
DynDOLOD/             = family namespace, NO empaquetable
DynDOLOD/TexGen/      = root exclusivo de TexGen, recibe -o:
DynDOLOD/DynDOLOD/    = root exclusivo de DynDOLOD, recibe -o:
```

Los nombres no contradicen `output_targets.py` (la familia ya es
`DYNDOLOD_OUTPUT_ROOT = "DynDOLOD"`); los strings exactos de los subroots se
congelan como constantes en PR-2.

## 8. Root legacy

```text
<game>/Sky-Claw/DynDOLOD
```

**Distinción conceptual de targets** (los nombres de implementación se deciden
en PR-2): los subroots del `external_work_root` (§7) son los
`ACTIVE_TARGET`; el destino histórico del root legacy,
`<game>/Sky-Claw/DynDOLOD/textures` — ruta completa siempre nombrada así, nunca
`<root>/textures` (ambiguo entre los dos mundos) —, es
`LEGACY_RECOVERY_ONLY_TARGET`.

PR-2 **NO lo migra, NO lo borra, NO lo mueve, NO lo adopta automáticamente como
staging nuevo, y NO crea nuevos `DirectoryRollback` sobre él**. Ninguna corrida
nueva usa, mueve ni adopta el árbol legacy. Nota operativa: puede contener GB de
generaciones anteriores; su limpieza es otro cambio, si alguna vez se decide
(inventario explícito, nunca barrido por sufijo parecido).

El rol recovery-only es la ÚNICA superficie de escritura admitida sobre ese
árbol, y es restaurativa, no productiva. **"Inequívoco" deja de ser un adjetivo
suelto: es el predicado cerrado que `rollback_reconciler` YA aplica a un destino
move-aside** (`reconcile_orphan_rollback_backups` → `_listar_backups_move_aside`
+ `_reconciliar_move_aside`, bajo `_bajo_el_lock_del_ritual`), no un mecanismo
nuevo. Un backup bajo el root legacy es candidato de restauración ÚNICAMENTE si
se cumplen TODAS estas condiciones; si alguna falla, el recovery no lo toca:

1. **Target exacto.** Corresponde al único `LEGACY_RECOVERY_ONLY_TARGET`,
   `<game>/Sky-Claw/DynDOLOD/textures`
   (`dyndolod_output_target(game) / DynDOLODRunner.TEXGEN_OUTPUT_NAME`, con
   `TEXGEN_OUTPUT_NAME == "textures"`). Nunca el padre
   `<game>/Sky-Claw/DynDOLOD` ni el placeholder `<root>/textures`.
2. **Sibling exacto.** Es un hijo DIRECTO del parent del target
   (`<game>/Sky-Claw/DynDOLOD/`) cuyo basename, tras quitar el sufijo, es
   literalmente `textures` (igualdad exacta con el nombre del destino declarado;
   no glob, no prefijo). Un sibling con otro basename no le pertenece.
3. **Sufijo válido.** El nombre termina en `.rollback-<nonce>` con `<nonce>` de
   **≥12 dígitos decimales**
   (`_SUFIJO_MOVE_ASIDE = re.compile(r"\.rollback-\d{12,}$")`). El productor
   `DirectoryRollback` emite `time.time_ns()` (19 dígitos); el piso de 12 es el
   umbral de admisión del reconciliador, no un `time_ns` exacto —por eso este
   contrato congela `≥12 dígitos`, no "el nonce de `time_ns`".
4. **Directorio físico real, no enlace.** La entrada candidata es un directorio
   real; un enlace (symlink/junction/reparse point) con nombre de backup se
   IGNORA y nunca se restaura siguiéndolo (`is_link` → skip con log), y lo que no
   es directorio no entra a la lista. Un backup legítimo lo produce un `rename`
   O(1), que nunca deja un enlace.
5. **Lock del productor.** La reconciliación adquiere el lock del ritual
   productor, `dyndolod-pipeline`, con el agente del reconciliador y TTL corto.
   Un lock **vivo** (no expirado, aun en otra instancia de Sky-Claw) o un
   `acquire_lock` fallido → **SKIP sin mutar** (se reporta como omitido): ritual
   en curso ⇒ el backup es legítimo, no huérfano.
6. **Decisión por presencia del target** (marcador durable en disco, medido con
   `path_present`, que cuenta un enlace roto como presente):
   - **Target AUSENTE** → restaurar el backup al target exacto con un `rename`
     O(1). Si el `rename` lanza `OSError`, preservar el backup y avisar (nunca se
     pierde la única copia).
   - **Target PRESENTE** → **NO sobrescribir, NO borrar; preservar backup +
     target** y exigir intervención manual (estado ambiguo: el filesystem no
     prueba si la salida presente está completa).

| Entrada | Veredicto |
|---|---|
| Sibling con basename distinto (aunque el sufijo sea válido) | IGNORE — no es su target |
| Sufijo `.rollback-*` inválido (corto, no numérico, no anclado al final) | IGNORE |
| Nombre de backup pero no es un directorio real | IGNORE (no entra a la lista) / fail-safe |
| Enlace symlink/junction/reparse | IGNORE — nunca restaurar siguiendo el enlace |
| Lock del productor vivo | SKIP sin mutar |
| Backup válido + target presente | PRESERVE BOTH — nunca reemplazar |
| Backup válido + target ausente bajo lock seguro | RESTORE |
| Sin backup válido | NO-OP |

Fuera de este predicado, ninguna corrida nueva usa, mueve, migra, adopta ni crea
`DirectoryRollback` sobre el legacy, ni lo pasa como `-o:`. Los productores
activos de la familia nueva declaran sólo sus `ACTIVE_TARGET`; el legacy entra a
la declaración del reconciliador únicamente como `LEGACY_RECOVERY_ONLY_TARGET`,
en la superficie de recovery de arranque (el plan P2.3 y `T-PR2-23` congelan la
familia completa A–H).

## 9. Concurrencia

```text
múltiples instancias configuradas con roots diferentes:
SÍ

ejecución simultánea de DynDOLOD/TexGen entre instancias:
NO GARANTIZADA POR PR-2
```

Motivo: exe, INI, logs, Data, mods y otros recursos físicos pueden seguir
compartidos. No se inventa locking global adicional en este PR documental. La
creación inicial del binding debe ser **single-winner no-reemplazante** en la
implementación (§3): `os.replace()` por sí solo no la provee. Una misma
instancia lógica (mismo `resource_binding`) no puede tener dos
`external_work_root` activos: el caso H de la máquina de estados lo rechaza vía
el estado durable de coordinación (P0.2 del plan).
El dominio de coordinación de etapa 9 (serialización por usuario, estado durable
independiente de cwd) era prerrequisito P0 del plan y **P0 lo entregó**:
`Stage9Coordination` sobre `SystemPaths.runtime_state_dir()`, con el ritual
`dyndolod-pipeline` y los leases existentes, más `RegistroDeRootActivo` para la
unicidad de root activo por `resource_binding`. Lo que este documento sigue **sin**
prometer es ejecución simultánea entre instancias (§9, arriba): los recursos
físicos compartidos no cambiaron.

## 10. Born-empty

La metadata de binding nunca vive dentro del root movible de una herramienta.
Para cada tool futura:

```text
tool_root previo
→ move-aside
→ tool_root nuevo/vacío
→ assert 0 entries
→ spawn
→ validate
→ package
→ commit / rollback
```

No se retira freshness todavía (PR-3, posterior al rig de ownership).

## 11. Transacción, recuperación y espacio

```text
validar propietario y rutas → adquirir coordinación + lock transaccional
→ registrar intención → mover directorios anteriores a backups adyacentes
→ crear destinos nuevos vacíos → lanzar y validar → empaquetar
→ commit de los destinos de la transacción o rollback → liberar locks
```

`DirectoryRollback.__aenter__` aparta el directorio; **no crea el nuevo**. La
creación explícita y la aserción de vacío ocurren antes del spawn, bajo lock. Con
`run_texgen=False` no se aparta, borra ni regenera su raíz cruda. El commit
conserva el alcance transaccional actual (no descartar el backup TexGen si un
fallo de DynDOLOD debe restaurarlo); el corte F1 mantiene solo el mod TexGen; la
TX queda PENDING; `mutation_coverage_complete` sigue en `False`. El reconciliador
declara los dos roots crudos y los dos mods por igualdad exacta con los
productores reales. Bajo PR-2 esos productores apuntan a los `ACTIVE_TARGET`
del `external_work_root` (§7); el target legacy (§8) sólo entra a esa
declaración como `LEGACY_RECOVERY_ONLY_TARGET`, en la superficie de recovery de
arranque y nunca como destino de una corrida nueva.

**Retención:** la generación actual y los backups necesarios hasta
commit/rollback; sin histórico ilimitado ni purga por antigüedad; ENOSPC produce
fallo y recuperación, nunca limpieza automática del backup. **Capacidad:**
presupuesto por volumen físico del pico restante (staging + copia a mods +
reserva); renombrar no libera ni duplica bytes; si es desconocido, informarlo y
no presentar el preflight como garantía. **Volúmenes:** cada backup hermano de su
destino (rename same-volume); el packaging entre unidades copia y no promete
rename atómico.

## 12. Contratos que no cambian

- **#552:** `INSTALL != DATA != MODS_DIR != PROFILE`.
- **#567:** `texgen_success → attributable_output → packaging_success →
  visibility_success → recién entonces DynDOLOD spawn`.
- `needs_deployment`, `texgen_packaging_attempted`, `dyndolod_result is None`,
  resume/historical con `run_texgen=False`.
- `mutation_coverage_complete` en `False`: logs, INI/presets del ejecutable y
  scratch siguen fuera de la cobertura de mutación.

## 13. Gate T5 y alcance honesto

**Antecedente histórico:** al redactarse inicialmente esta documentación, seguían haciendo
falta dos corridas reales separadas (TexGen y DynDOLOD) con root con espacio,
`Using Output Path:` exacto, archivos físicos en el root esperado y presets stale
ejercitados sin desvío.

**Estado formal del gate de lanzamiento: CERRADO (2026-09-10).** Esas dos corridas se
ejecutaron y el gate de lanzamiento quedó satisfecho (T5-V2 LAUNCH GATE: PASS;
T5-V2 FULL CHECKLIST: PARTIAL 7/10) — informe commiteado:
[`docs/validation/2026-09-10_t5v2_dyndolod_stage9.md`](../../validation/2026-09-10_t5v2_dyndolod_stage9.md).
Los criterios 8–10 del checklist completo quedan pendientes para el rig de
ownership posterior a PR-2.

**Reapertura ante PR-2:** P0 es el único prerrequisito para COMENZAR PR-2. Al
cambiar los subroots administrados de salida usados por `-o:`, PR-2 **reabre** el
gate de lanzamiento. PR-2 no puede mergearse sin repetir las dos corridas de rig
reales (TexGen + DynDOLOD) sobre el candidato PR-2 bajo las condiciones
canónicas del gate.

La [documentación oficial de TexGen](https://dyndolod.info/Help/TexGen) recomienda
salida dedicada externa (consultada 2026-09-09) y describe la línea de comandos,
pero **no demuestra** cómo se comporta la versión del binario del rig.

La corrección manual del campo Output es admisible como procedimiento asistido
documentado, con preset rancio presente al iniciar y evidencia antes/después; no
se registra como PASS hasta ejecutar y revisar ambas corridas. PR-2 preserva
el contrato #552, los cuatro prerrequisitos #567, flags, F1, resume, taxonomía
del log y freshness. No implementa UIA, limpieza de firmas, soporte ZIP ni
soluciona activación MO2 por copiar archivos a mods. Cada cierre exige su
evidencia propia en el [plan](../plans/2026-09-09-dyndolod-pr2-resolution.md).

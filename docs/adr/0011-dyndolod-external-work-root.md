# ADR 0011 — DynDOLOD PR-2: `external_work_root` y binding de propiedad

**Fecha:** 2026-09-09
**Estado:** Aceptada (propuesta originalmente en #570; formalizada tras el cierre
del gate de lanzamiento T5-v2 por el informe 2026-09-10, ver §2.12). P0 es el único
prerrequisito restante para comenzar PR-2; la implementación de PR-2 reabrirá el
gate de lanzamiento al mutar los subroots administrados de `-o:` y exigirá repetir
las dos corridas reales antes de su merge.
**Contexto de origen:** `origin/main` `5e5e9448db0d4015b3bf0dc4c1df10fdc49e226c`
(post-merge #569), verificado por `fetch` + lectura de código el 2026-09-09.
**Alcance:** cerrar la decisión arquitectónica de lifecycle, identidad, propiedad y
fronteras de la raíz de trabajo externo (`external_work_root`) que PR-2 usará como
`-o:` de TexGen y DynDOLOD; la máquina de estados del root; el cambio de preferencia;
la admisión de rutas; el layout; el comportamiento frente al root legacy; los límites
de concurrencia; y los contratos que no cambian (#552, #567).
**Reglas de exclusión:** sin código de producción; sin cambiar `-o:`/`-d:`/`-t:`/`-m:`/`-p:`;
sin writer de metadata; sin UUIDs reales de instancia; sin migración; sin tocar #528;
sin retirar freshness; sin ejecutar el rig.

---

## 1. Contexto

### 1.1 Dónde estamos

La etapa 9 opera hoy sobre una raíz administrada compartida:
`<game>/Sky-Claw/DynDOLOD` (`output_targets.dyndolod_output_target`). TexGen escribe
`<game>/Sky-Claw/DynDOLOD/textures` (`DynDOLODRunner.TEXGEN_OUTPUT_NAME`), DynDOLOD crea
`DynDOLOD_Output` (`DynDOLODRunner.DYNDLOD_OUTPUT_NAME`) y puede caer en la raíz
(interpretación B de `-o:`). Sobre esa física real aterrizaron tres cierres fail-closed
(filas del inventario OODA): **A** — la raíz compartida no es unidad empaquetable;
**B** — el staging de TexGen entra al move-aside antes de lanzar (born-empty);
**C** — visibilidad byte a byte del staging en el `-d:<Data>` antes de spawn.

La clase de defecto A se elimina con **subroots exclusivos por herramienta** —un
`-o:` distinto por binario sobre una raíz de trabajo externa—, que es el alcance de
PR-2. Ese cambio toca `-o:`, así que cae bajo el gate de aceptación de
`sky_claw/local/AGENTS.md` §2.9 (dos corridas reales separadas, una por binario).
El gate de lanzamiento inicial previo a PR-2 quedó cerrado por T5-v2 el 2026-09-10
(ver §2.12); la implementación posterior de PR-2 reabrirá el gate al modificar los
subroots de `-o:`.

### 1.2 Qué faltaba

El lifecycle de esa raíz externa **no estaba decidido**: dónde vive, quién es el
dueño, cómo se vincula a una instancia, qué ocurre ante colisión, corrupción o
cambio de preferencia. Sin esa decisión, PR-2 no tiene contrato de implementación
y cualquier cableado de P0 sería arbitrariedad reversible en el peor sentido.
Este ADR cierra ese hueco; el gate de rig era un blocker distinto —cerrado por
T5-v2 el 2026-09-10 (§2.12)— y el blocker de implementación pasa a ser P0.

### 1.3 Riesgos de decidir mal

- Un fallback silencioso del root a `game`, su padre, MO2, `install_dir`, `%TEMP%`,
  `Documents` o `C:\Sky-Claw` recrea la clase de defecto que PR-2 viene a eliminar.
- Adoptar automáticamente un directorio existente no vacío absorbe datos ajenos
  (el root legacy puede contener GB de generaciones anteriores).
- Derivar la identidad de la tupla de paths es inestable: reubicar la instancia
  rompe el vínculo, y hostname/usuario/SID contaminan la identidad con datos de
  máquina.
- Dos instancias aceptando el mismo root sin evidencia de propiedad es corrupción
  de staging silenciosa.

---

## 2. Decisión

### 2.1 `external_work_root` es una preferencia persistente del usuario

Apunta a un **directorio de estado de trabajo recuperable, exclusivo de una
instancia gestionada**. Propiedades normativas:

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
Sky-Claw arranca y funciona normalmente. **No hay fallback silencioso**, ni a
`game`, ni a `game parent`, ni a MO2, ni a `install_dir`, ni a `%TEMP%`, ni a
`Documents`, ni a `C:\Sky-Claw`. En esta entrega **no se agrega variable de
entorno**: una segunda fuente de selección reabre el split-brain que la capa de
resolución ya cerró para MODS/PROFILE (#552/#555). La inyección explícita de
`output_root` en `DynDOLODConfig` se conserva para pruebas y rig; no es un
parámetro libre del payload del LLM (`_extra_args_admisibles` ya lo impide).

### 2.2 Identidad: `binding_id` ≠ `resource_binding`

**La tupla de paths NO es identidad durable.** Se separan dos conceptos:

- **`binding_id`:** UUID generado **una única vez** al inicializar un work root.
  No se deriva por hash de paths. No usa hostname/usuario/SID como identidad.
- **`resource_binding`:** **evidencia** de qué recursos estaban asociados al
  binding: `game_path`, `mo2_instance_data_root`, `mo2_mods_path`, todos
  canonicalizados con las primitivas reales existentes
  (`pathlib.Path.resolve(strict=False)` como `_canonicalizar` en
  `app/core/path_resolver.py`, y contención vía `PathValidator`).

`resource_binding` identifica los **recursos de la instancia lógica observada**;
`binding_id` identifica el **ownership concreto** de ese `external_work_root`
(para proveniencia y recuperación). Dos configuraciones que resuelven el mismo
`resource_binding` son la **misma instancia lógica** (ver §2.6): una misma
instancia lógica no puede tener dos `external_work_root` activos simultáneamente —
cambiar de root exige una transición/rebind explícita (§2.6; casos G y H de §2.4).

`config_path` **NO pertenece al binding**: no es contractual, no entra al schema
y un writer que la serialice produce un archivo inválido (fail-closed por el
caso E). Puede mencionarse en logs o diagnóstico fuera del binding, nunca como
metadata del archivo. El perfil MO2 **no** crea otro work root: los mods
empaquetados se comparten entre perfiles de una instancia; cambiar perfil
invalida el contexto del handoff según su contrato vigente y no justifica
reutilizar su evidencia.

### 2.3 Metadata de propiedad

```text
<external_work_root>/.sky-claw-binding.json
```

El archivo vive **fuera** de `DynDOLOD/TexGen` y `DynDOLOD/DynDOLOD`, porque esos
subárboles serán targets de move-aside/born-empty. No entra en conflicto con
convenciones existentes del repo: el estado de staging de Sky-Claw ya usa nombres
con punto (`.skyclaw_backups`) y la diferencia deliberada —el binding vive dentro
del root elegido por el usuario, no en el staging derivado del cwd— es lo que hace
que la propiedad viaje con el directorio.

**Schema v1 congelado** (sin timestamps; sin metadata que no sea contractual):

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

No existen representaciones contractuales alternativas: los tres campos de
evidencia viven **sólo** como objeto anidado `resource_binding`; un binding con
ellos en la raíz no es un binding válido. Política de campos extra, declarada
explícitamente: cualquier campo no presente en este schema (raíz o dentro de
`resource_binding`) es schema desconocido y sigue el veredicto del caso E
(RECHAZAR fail-closed). Extender el schema exige versión nueva
(`schema_version`) y su ADR, no un campo suelto.

La implementación futura declara y verifica:

- **creación inicial single-winner no-reemplazante.** `os.replace()` por sí
  solo NO provee single-winner: puede sustituir un binding publicado
  concurrentemente por otro proceso. La creación exige una primitiva con la
  propiedad demostrable de que, si P1 y P2 inicializan el mismo root vacío,
  exactamente uno publica el binding, **el perdedor NO reemplaza el archivo del
  ganador**, y el perdedor relee/valida el binding publicado y continúa sólo si
  el `resource_binding` publicado es compatible (si no, fail-closed). Candidatas:
  creación exclusiva (no-reemplazante); lock cross-process alrededor del
  check + publish (el repo ya tiene maquinaria de locks distribuidos); u otro
  mecanismo equivalente con la misma propiedad. La API concreta no se congela
  acá — el árbol aún no tiene una adecuada — y T-PR2-22 la valida con **dos
  procesos reales o un mecanismo equivalente cross-process, no sólo coroutines**;
- **escritura atómica con `os.replace`** — reservada a actualizaciones donde
  reemplazar es contractualmente válido (reescritura del binding propio por su
  dueño), con el patrón temporal + `os.replace` en el mismo directorio que usan
  `Config.save()` y los serializadores de `local_config.py`;
- **nunca reescribir silenciosamente un binding ajeno**;
- **metadata corrupta o schema desconocido = fail-closed** (caso E, incluye
  campos fuera del schema v1). No se implementa el writer en este PR.

### 2.4 Máquina de estados del root (normativa)

| Caso | Estado del root | Veredicto |
|---|---|---|
| A | Root ausente | Permitido inicializar |
| B | Root existente y vacío, sin binding | Permitido inicializar |
| C | Root con binding válido para los mismos recursos | Permitido utilizar |
| D | Root no vacío, sin binding | **RECHAZAR.** No adoptar contenido automáticamente |
| E | Metadata corrupta / schema desconocido (incluye cualquier campo fuera del schema v1) | **RECHAZAR fail-closed** |
| F | Binding perteneciente a otros recursos | **RECHAZAR** |
| G | Binding propio cuyo `resource_binding` ya no coincide con la resolución actual | **RECHAZAR** y requerir transición/rebind explícito. PR-2 NO implementa rebind automático |
| H | Root con binding válido para los mismos recursos, pero ese `resource_binding` ya tiene otro `external_work_root` activo (detectado por el estado de coordinación de la instalación) | **RECHAZAR** — requiere transición/rebind explícito, igual que G. PR-2 NO implementa rebind automático |

"Casos C, F y H comparten mecanismo": C y F se resuelven por la comparación
exacta del `resource_binding` registrado contra la resolución canonicalizada del
arranque; H añade, para esa misma comparación, la consulta al estado durable de
coordinación (§2.6) — no hay tercera vía ni degradación en ninguno.

### 2.5 Cambio de preferencia

Cambiar `external_work_root` **no hace hot-reload del pipeline**. Contrato:

```text
persistir nueva preferencia → aplicar en próximo arranque de Sky-Claw
```

Eso evita mutar en caliente `AppContext`, las raíces de `PathValidator`, el runner
cacheado, el rollback reconciler y transacciones activas. No se diseña hot reload
para PR-2. Persistencia con `escribir_campo` + `guardar_config`/`persistir_campo`
(`local_config.py`) y el merge-on-save de `Config.save()` — nunca `load`/`save`
crudos ni asignaciones muertas (contrato de `AGENTS.md` raíz).

### 2.6 Concurrencia

```text
múltiples instancias configuradas con roots diferentes: SÍ

ejecución simultánea de DynDOLOD/TexGen entre instancias:
NO GARANTIZADA POR PR-2
```

Motivo: exe, INI, logs, Data, mods y otros recursos físicos pueden seguir
compartidos aunque los work roots sean distintos. No se inventa locking global
adicional en este PR documental. Sí se **exige** que la creación inicial del
binding sea **single-winner no-reemplazante** en la implementación futura (§2.3).

**Identidad y unicidad del root activo.** El `resource_binding` identifica la
instancia lógica; el `binding_id` identifica el ownership concreto de un root.
Dos configuraciones con el mismo `resource_binding` son la misma instancia
lógica y **NO pueden tener dos `external_work_root` activos simultáneamente**:
cambiar de root es una transición/rebind explícita (caso G), y PR-2 no la
implementa automáticamente. En consecuencia, dos configuraciones con el mismo
`resource_binding` **NO** pueden inicializar dos roots independientes y
considerar ambos activos: cuando el segundo root presenta el mismo
`resource_binding`, la máquina de estados lo rechaza (caso H). La detección
global de "este `resource_binding` ya tiene otro root activo" **no puede
demostrarse con el JSON local del binding solos** — el binding describe su
propio root, no el estado de la instalación. Lo provee el estado durable de
coordinación de etapa 9 (el mismo dominio que serializa la instancia por
usuario con ubicación independiente de cwd, prerrequisito P0 del plan):
mientras ese estado no exista, el caso H se declara como requisito P0 y su
ausencia debe tratarse fail-closed, no ignorarse. No se inventa un catálogo
global de bindings fuera de ese dominio.

### 2.7 Admisión de rutas

Como propiedad (no como algoritmo congelado):

- absoluta; filesystem local; no root de volumen; no UNC/network en PR-2;
- puede vivir en otro volumen local (si admite rename y locks — precondición del
  move-aside por `DirectoryRollback`);
- **no solapa en NINGUNA dirección** con: game; Data; SteamApps; MO2 install; MO2
  instance data root; profiles; mods; overwrite; DynDOLOD/TexGen install dirs;
  TEMP (`tempfile.gettempdir()`); y el **conjunto cerrado de Windows Known
  Folders prohibidos**, v1: `Documents`, `Desktop`, `Downloads`. La resolución
  es por **identificador de Known Folder con la API de Windows** (p. ej.
  `SHGetKnownFolderPath`), obteniendo la ruta efectiva vigente al admitir —
  lo que cubre redirecciones a otros volúmenes y a carpetas de sincronización;
  **nunca** derivada de `%USERPROFILE%` (no refleja redirecciones) ni por
  coincidencia de substrings. Justificación por carpeta: `Documents` es fallback
  prohibido por nombre en §2.1, el SOP ya midió su redirección OneDrive
  (`sky_claw/local/AGENTS.md` §2.9 punto 2) y los placeholders de sincronización
  rompen la precondición rename/lock del move-aside; `Desktop` comparte ese
  riesgo de sincronización y añade ciclo de vida visible al usuario (limpieza y
  borrado manual rutinarios sobre una carpeta que el usuario percibe suya);
  `Downloads` está gestionada por agentes externos al usuario (navegador,
  políticas de limpieza de almacenamiento) que borran o mueven contenido como
  parte de su ciclo de vida normal. El conjunto es cerrado para v1; agregar una
  carpeta exige enmienda de este ADR con su propia justificación de riesgo, no
  intuición. No existe hoy una API de Known Folders dedicada en el árbol:
  construirla con ese conjunto congelado es requisito de P0, no una primitiva
  existente que se esté fingiendo tener;
- no se amplía `PathValidator` autorizando toda una unidad o ancestro amplio: se
  autoriza **sólo el root admitido + destinos derivados** (la familia y los
  metadatos);
- sin búsqueda ingenua de substrings (`"Steam"`, `"OneDrive"`) para establecer
  identidad; sin detección universal de proveedores cloud;
- propiedad de symlinks/junctions/reparse points: **ninguna mutación administrada
  puede resolver fuera del `external_work_root` admitido mediante un componente
  redirigido.** La implementación reutiliza las primitivas existentes
  (`sky_claw/app/security/links.py`: `is_link`/`link_kind`/`rmtree_link_aware`,
  y `PathValidator.validate` con `strict_symlink`) antes de inventar un subsistema.

### 2.8 Layout

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

Los nombres no contradicen convenciones vigentes en `output_targets.py`: la familia
ya se llama `DYNDOLOD_OUTPUT_ROOT = "DynDOLOD"` y la regla *namespace compartido ≠
namespace empaquetable* es la vigente. Los nombres exactos de los subroots por
herramienta se congelarán como constantes de `output_targets.py` en PR-2; lo
normativo acá es la estructura, no el string.

### 2.9 Root legacy

```text
<game>/Sky-Claw/DynDOLOD
```

PR-2 **NO lo migra, NO lo borra, NO lo mueve, NO lo adopta automáticamente como
staging nuevo, y NO crea nuevos `DirectoryRollback` sobre él**. El directorio
viejo queda intacto. Nota operativa: puede contener GB de generaciones
anteriores; su limpieza es una decisión aparte (inventario explícito, nunca
barrido por sufijo parecido).

**Distinción conceptual de targets** (los nombres de implementación se deciden
en PR-2):

- `ACTIVE_TARGET`: los targets que los productores nuevos crean y mutan en cada
  corrida — los subroots del `external_work_root` (§2.8) — y los únicos que
  entran a la familia de move-aside/reconciliación como destinos mutables;
- `LEGACY_RECOVERY_ONLY_TARGET`: `<game>/Sky-Claw/DynDOLOD/textures` — el
  destino histórico dentro del root legacy, ruta completa siempre nombrada así,
  nunca como `<root>/textures` (el placeholder es ambiguo entre ambos mundos).

El rol de ese target legacy es **sólo recovery**: restaurativo, no productivo.
**"Inequívoco" deja de ser un adjetivo suelto: es el predicado cerrado que
`rollback_reconciler` YA aplica a un destino move-aside**
(`reconcile_orphan_rollback_backups` → `_listar_backups_move_aside`
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

### 2.10 Born-empty

La metadata de binding **nunca** vive dentro del root movible de una herramienta.
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

No se retira freshness todavía: pertenece a PR-3, posterior al rig de ownership.

### 2.11 Contratos que no cambian

- **#552:** `INSTALL != DATA != MODS_DIR != PROFILE`.
- **#567:** `texgen_success → attributable_output → packaging_success →
  visibility_success → recién entonces DynDOLOD spawn`.
- `needs_deployment`, `texgen_packaging_attempted`, `dyndolod_result is None`,
  resume/historical con `run_texgen=False`.

### 2.12 Gate T5

**Antecedente histórico:** al redactarse originalmente este ADR en #570, el gate
de rig previo seguía abierto y exigía dos corridas reales separadas (TexGen y
DynDOLOD), cada una con: root con espacio, `Using Output Path:` exacto, archivos
físicos en el root esperado y presets stale ejercitados sin desvío.

**Estado formal del gate de lanzamiento: CERRADO (2026-09-10).** El gate de
lanzamiento T5-v2 quedó satisfecho por la corrida documentada en
[`docs/validation/2026-09-10_t5v2_dyndolod_stage9.md`](../validation/2026-09-10_t5v2_dyndolod_stage9.md)
(T5-V2 LAUNCH GATE: PASS en ambos binarios: root con espacio, eco exacto, archivos
físicos en el root, presets stale ejercitados con corrección asistida, cero desvío
al decoy y restauración verificada; criterios 1–7 demostrados). El checklist T5-v2
completo permanece **PARCIAL (7/10)**: los criterios 8–10 corresponden al rig de
ownership posterior a PR-2.

**Lifecycle y reapertura ante PR-2:** P0 es el único prerrequisito para COMENZAR
la implementación de PR-2. Al modificar los subroots administrados de salida
usados por `-o:`, PR-2 **reabre** el gate de lanzamiento. PR-2 NO puede
mergearse hasta repetir las dos corridas de rig reales (TexGen + DynDOLOD) sobre
el candidato de PR-2 bajo las condiciones canónicas del gate (dos herramientas,
root con espacios, `Using Output Path:` exacto, outputs físicos, stale preset
ejercitado y cero desvío).

---

## 3. Alternativas evaluadas

| Alternativa | Veredicto | Motivo |
|---|---|---|
| Raíz elegida por el usuario, exclusiva por instancia | **Elegida** | Volumen y capacidad explícitos; derivación única en `output_targets`; sin inventar identificadores de carpetas |
| Base global con subcarpetas hash por juego/instancia | Rechazada para PR-2 | Añade reglas de identidad, migración y catálogo que PR-2 no necesita |
| Derivar de `install_dir`, juego, padre del juego o `%TEMP%` | Rechazada | Confunde instalación con trabajo; conserva el defecto del juego; permite purga de la única copia recuperable |
| Tupla de paths como identidad durable | Rechazada | Inestable ante reubicación; contamina con hostname/usuario/SID; confunde evidencia con identidad |
| `binding_id` UUID + `resource_binding` como evidencia | **Elegida** | Identidad estable ante reubicación del binding; la evidencia se compara, no se deriva |
| Fallback silencioso cuando la preferencia falta | Rechazada | Recrea la clase de defecto; "no configurado" es el estado honesto |
| Adoptar automáticamente un root no vacío sin binding | Rechazada | Absorción silenciosa de datos ajenos, incluido el root legacy con GB |
| Hot-reload de la preferencia | Rechazada | Muta en caliente AppContext, PathValidator, runner cache, reconciler y TX activas; el arranque es el boundary natural |
| Variable de entorno para el root | Rechazada | Segunda fuente de selección; root que cambia entre arranques sin reflejarse en la preferencia |
| Prometer ejecución simultánea entre instancias | Rechazada | No demostrado; recursos físicos compartidos (exe, INI, logs, Data, mods) |

---

## 4. Consecuencias

**Positivas:**

1. PR-2 tiene contrato de implementación: P0 (preferencia, binding, admisión,
   coordinación) queda especificado en el plan, con el estado del root cerrado
   caso por caso.
2. La identidad por `binding_id` hace que la propiedad del root sobreviva a
   reubicaciones de config y no dependa de datos de máquina.
3. El fail-closed ante metadata corrupta y root no vacío extiende al work root la
   doctrina de no-adopción-silenciosa que el repo ya aplica a backups y staging.
4. El root legacy queda protegido por inacción explícita, no por omisión.

**Costos y obligaciones:**

1. La preferencia nueva debe entrar a `Config._load_defaults()` (vacía) en P0,
   con anclas del contrato de persistencia (`tests/test_local_config_persistencia.py`).
2. La máquina de estados A–H exige tests que la **enumeren**, no que la muestreen
   (patrón de `AGENTS.md` raíz): el plan lleva los IDs T-PR2-21/22/23. La
   detección del caso H añade la obligación de que el estado durable de
   coordinación (P0.2) registre el root activo por `resource_binding`.
3. La admisión de rutas necesita un mecanismo de Known Folders que **no existe
   hoy** en el árbol: se declara como construcción de P0 con el conjunto cerrado
   de la §2.7 (`Documents`, `Desktop`, `Downloads`) y su test parametrizado —
   cada Known Folder contractual, incluida una ruta redirigida a otro volumen,
   hace rechazar el `external_work_root` —, no como primitiva vigente.
4. `sky_claw/local/AGENTS.md` §2.9 punto 4 sigue describiendo el root compartido
   vigente: es correcto hasta que PR-2 cambie el `-o:`, y se enmienda en ese mismo
   PR (regla del roadmap: enmendar SOP y código juntos).

**Qué verifica este ADR:** es una decisión documental; sus gates de ejecución son
los tests que el plan asigna a P0/PR-2 (estado del root A–H, single-winner entre
procesos, frontera legacy, admisión, persistencia). Conforme a `AGENTS.md` raíz,
ninguna regla de este ADR se apoya en
"confiar": cada una tiene su receta de verificación futura declarada en el plan.

## 5. Non-goals

Implementar el writer de binding; crear UUIDs reales de instancia; migrar o limpiar
el root legacy; retirar freshness (PR-3); soporte UNC/network; detección universal
de proveedores cloud; locking global cross-instancia; catálogo global de bindings
entre instalaciones (la unicidad de root por instancia es instalacional vía el
estado de coordinación de P0.2); rebind automático (casos G y H quedan en
transición explícita); tocar #528; levantar el gate de rig.

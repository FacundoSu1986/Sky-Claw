# ADR 0011 — DynDOLOD PR-2: `external_work_root` y binding de propiedad

**Fecha:** 2026-09-09
**Estado:** Propuesta (docs-only; este PR no implementa código productivo ni levanta el
gate de rig de `sky_claw/local/AGENTS.md` §2.9). La aceptación formal ocurre con el
merge de este PR documental; la implementación sigue bloqueada por ese gate.
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
`<root>/textures` (`DynDOLODRunner.TEXGEN_OUTPUT_NAME`), DynDOLOD crea
`DynDOLOD_Output` (`DynDOLODRunner.DYNDLOD_OUTPUT_NAME`) y puede caer en la raíz
(interpretación B de `-o:`). Sobre esa física real aterrizaron tres cierres fail-closed
(filas del inventario OODA): **A** — la raíz compartida no es unidad empaquetable;
**B** — el staging de TexGen entra al move-aside antes de lanzar (born-empty);
**C** — visibilidad byte a byte del staging en el `-d:<Data>` antes de spawn.

La clase de defecto A se elimina con **subroots exclusivos por herramienta** —un
`-o:` distinto por binario sobre una raíz de trabajo externa—, que es el alcance de
PR-2. Ese cambio toca `-o:`, así que cae bajo el gate de aceptación de
`sky_claw/local/AGENTS.md` §2.9 (dos corridas reales separadas, una por binario).
Ese gate sigue abierto y este ADR no lo levanta.

### 1.2 Qué faltaba

El lifecycle de esa raíz externa **no estaba decidido**: dónde vive, quién es el
dueño, cómo se vincula a una instancia, qué ocurre ante colisión, corrupción o
cambio de preferencia. Sin esa decisión, PR-2 no tiene contrato de implementación
y cualquier cableado de P0 sería arbitrariedad reversible en el peor sentido.
Este ADR cierra ese hueco; el gate de rig es un blocker distinto y permanece.

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

`config_path` puede registrarse como **procedencia** si resulta útil, pero NO es
parte de la identidad de la instancia. El perfil MO2 **no** crea otro work root:
los mods empaquetados se comparten entre perfiles de una instancia; cambiar perfil
invalida el contexto del handoff según su contrato vigente y no justifica reutilizar
su evidencia.

La admisión compara `resource_binding` contra la resolución actual; `binding_id`
identifica el binding para proveniencia y recuperación. Dos configuraciones que
resuelven los mismos recursos comparten la misma instancia lógica (ver §2.6).

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

Schema mínimo (sin timestamps; sin metadata que no sea contractual):

```json
{
  "schema_version": 1,
  "binding_id": "<uuid>",
  "game_path": "<canonical>",
  "mo2_instance_data_root": "<canonical>",
  "mo2_mods_path": "<canonical>"
}
```

La implementación futura declara y verifica: **escritura atómica** (mismo patrón
temporal + `os.replace` en el mismo directorio que usan `Config.save()` y los
serializadores de `local_config.py`); **creación inicial single-winner** (dos
inicializaciones concurrentes del mismo root no pueden producir dos bindings);
**nunca reescribir silenciosamente un binding ajeno**; **metadata corrupta o con
schema desconocido = fail-closed**. No se implementa el writer en este PR.

### 2.4 Máquina de estados del root (normativa)

| Caso | Estado del root | Veredicto |
|---|---|---|
| A | Root ausente | Permitido inicializar |
| B | Root existente y vacío, sin binding | Permitido inicializar |
| C | Root con binding válido para los mismos recursos | Permitido utilizar |
| D | Root no vacío, sin binding | **RECHAZAR.** No adoptar contenido automáticamente |
| E | Metadata corrupta / schema desconocido | **RECHAZAR fail-closed** |
| F | Binding perteneciente a otros recursos | **RECHAZAR** |
| G | Binding propio cuyo `resource_binding` ya no coincide con la resolución actual | **RECHAZAR** y requerir transición/rebind explícito. PR-2 NO implementa rebind automático |

"Casos C y F comparten mecanismo": la única diferencia entre admitir y rechazar es
la comparación exacta del `resource_binding` registrado contra la resolución
canonicalizada del arranque — no hay tercera vía ni degradación.

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
binding sea single-winner de forma atómica en la implementación futura. Dos
configuraciones sobre los mismos recursos y el mismo root son la misma instancia
lógica (caso C); su serialización en el tiempo es requisito del dominio de
coordinación de etapa 9 que el plan de PR-2 lleva como P0, no una promesa de
este ADR.

### 2.7 Admisión de rutas

Como propiedad (no como algoritmo congelado):

- absoluta; filesystem local; no root de volumen; no UNC/network en PR-2;
- puede vivir en otro volumen local (si admite rename y locks — precondición del
  move-aside por `DirectoryRollback`);
- **no solapa en NINGUNA dirección** con: game; Data; SteamApps; MO2 install; MO2
  instance data root; profiles; mods; overwrite; DynDOLOD/TexGen install dirs;
  TEMP; Windows Known Folders prohibidos según los mecanismos existentes (identidades
  resueltas + `tempfile.gettempdir()` + perfil de usuario del entorno). No existe hoy
  una API de Known Folders dedicada en el árbol: construirla es requisito de P0, no
  una primitiva existente que se esté fingiendo tener;
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

PR-2 **NO lo migra, NO lo borra, NO lo mueve, NO lo adopta automáticamente**. El
directorio viejo queda intacto. Nota operativa: puede contener GB de generaciones
anteriores; su limpieza es una decisión aparte (inventario explícito, nunca barrido
por sufijo parecido). El reconciliador mantiene el target legacy `<root>/textures`
durante la transición: un backup anterior puede ser la única copia recuperable tras
un crash de la versión previa.

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

**Este ADR NO levanta el gate de rig.** Después de aprobar esta documentación
siguen haciendo falta dos corridas reales separadas (TexGen y DynDOLOD), cada una
con: root con espacio, `Using Output Path:` exacto, archivos físicos en el root
esperado, preset stale presente deliberadamente y sin desvío al preset stale. La
documentación oficial de DynDOLOD (dyndolod.info) describe la línea de comandos;
**no demuestra** el comportamiento real del binario del rig.

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
2. La máquina de estados A–G exige tests que la **enumeren**, no que la muestreen
   (patrón de `AGENTS.md` raíz): el plan lleva los IDs T-PR2-21/22.
3. La admission de rutas necesita un mecanismo de Known Folders que **no existe
   hoy** en el árbol: se declara como construcción de P0 con su test, no como
   primitiva vigente.
4. `sky_claw/local/AGENTS.md` §2.9 punto 4 sigue describiendo el root compartido
   vigente: es correcto hasta que PR-2 cambie el `-o:`, y se enmienda en ese mismo
   PR (regla del roadmap: enmendar SOP y código juntos).

**Qué verifica este ADR:** es una decisión documental; sus gates de ejecución son
los tests que el plan asigna a P0/PR-2 (estado del root, single-winner, admisión,
persistencia). Conforme a `AGENTS.md` raíz, ninguna regla de este ADR se apoya en
"confiar": cada una tiene su receta de verificación futura declarada en el plan.

## 5. Non-goals

Implementar el writer de binding; crear UUIDs reales de instancia; migrar o limpiar
el root legacy; retirar freshness (PR-3); soporte UNC/network; detección universal
de proveedores cloud; locking global cross-instancia; rebind automático (caso G
queda en transición explícita); tocar #528; levantar el gate de rig.

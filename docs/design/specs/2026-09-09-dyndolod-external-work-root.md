# DynDOLOD PR-2: contrato de trabajo externo

> **Estado:** decisión arquitectónica cerrada por [ADR 0011](../../adr/0011-dyndolod-external-work-root.md)
> (Propuesta hasta el merge de ese PR). No implementada; el gate de rig de
> `sky_claw/local/AGENTS.md` §2.9 sigue abierto y este documento no lo levanta.
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

**La tupla de paths NO es identidad durable** (ADR 0011 §2.2). Se separan:

- **`binding_id`** — UUID generado **una única vez** al inicializar un work root.
  Nunca por hash de paths; nunca hostname/usuario/SID.
- **`resource_binding`** — evidencia de los recursos asociados al binding:
  `game_path`, `mo2_instance_data_root`, `mo2_mods_path`, canonicalizados con las
  primitivas existentes (`resolve(strict=False)` no estricto como
  `_canonicalizar` de `path_resolver.py`; comparación contra el sandbox con
  `PathValidator`).

`config_path` se documenta como procedencia, **no** identidad. El perfil MO2 **no**
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

Schema mínimo:

```json
{
  "schema_version": 1,
  "binding_id": "<uuid>",
  "game_path": "<canonical>",
  "mo2_instance_data_root": "<canonical>",
  "mo2_mods_path": "<canonical>"
}
```

Sin metadata extra; sin timestamps. El writer **no se implementa acá**. La spec
declara para la implementación futura:

- escritura **atómica** (temporal + `os.replace` en el mismo directorio, como
  `Config.save()` y los serializadores de `local_config.py`);
- creación inicial **single-winner** (dos inicializaciones concurrentes del mismo
  root no pueden producir dos bindings);
- **nunca reescribir silenciosamente un binding ajeno**;
- **metadata corrupta o schema desconocido = fail-closed**.

## 4. Máquina de estados del root (normativa)

| Caso | Estado del root | Veredicto |
|---|---|---|
| A | Root ausente | Permitido inicializar |
| B | Root existente y vacío, sin binding | Permitido inicializar |
| C | Root con binding válido para los mismos recursos | Permitido utilizar |
| D | Root no vacío, sin binding | **RECHAZAR** — no adoptar contenido automáticamente |
| E | Metadata corrupta / schema desconocido | **RECHAZAR fail-closed** |
| F | Binding perteneciente a otros recursos | **RECHAZAR** |
| G | Binding propio cuyo `resource_binding` ya no coincide con la resolución actual | **RECHAZAR** — requiere transición/rebind explícito; PR-2 NO implementa rebind automático |

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
  TEMP; Windows Known Folders prohibidos según los mecanismos existentes;
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
El mecanismo de Known Folders **no existe hoy** en el árbol: su construcción es
requisito de P0 con su test, no una primitiva que se finja tener.

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

PR-2 **NO lo migra, NO lo borra, NO lo mueve, NO lo adopta automáticamente**.
Queda intacto. Nota operativa: puede contener GB de generaciones anteriores; su
limpieza es otro cambio, si alguna vez se decide (inventario explícito, nunca
barrido por sufijo parecido). El reconciliador conserva el target legacy
`<root>/textures` durante la transición.

## 9. Concurrencia

```text
múltiples instancias configuradas con roots diferentes:
SÍ

ejecución simultánea de DynDOLOD/TexGen entre instancias:
NO GARANTIZADA POR PR-2
```

Motivo: exe, INI, logs, Data, mods y otros recursos físicos pueden seguir
compartidos. No se inventa locking global adicional en este PR documental. La
creación inicial del binding debe ser single-winner atómica en la implementación.
El dominio de coordinación de etapa 9 (serialización por usuario, estado durable
independiente de cwd) sigue siendo prerrequisito P0 del plan — este documento no
lo promete resuelto.

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
productores reales.

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

**Este contrato NO levanta el gate de rig.** Después de aprobar esta documentación
siguen haciendo falta **dos corridas reales separadas** (TexGen y DynDOLOD), cada
una con: root con espacio, `Using Output Path:` exacto, archivos físicos en el
root esperado, preset stale presente deliberadamente y sin desvío al preset stale.
La [documentación oficial de TexGen](https://dyndolod.info/Help/TexGen) recomienda
salida dedicada externa (consultada 2026-09-09) y describe la línea de comandos,
pero **no demuestra** cómo se comporta la versión del binario del rig.

La corrección manual del campo Output es admisible como procedimiento asistido
documentado, con preset rancio presente al iniciar y evidencia antes/después; no
se registra como PASS hasta ejecutar y revisar ambas corridas. PR-2 preserva
#552, los cuatro prerrequisitos #567, flags, F1, resume, taxonomía del log y
freshness. No implementa UIA, limpieza de firmas, soporte ZIP ni soluciona
activación MO2 por copiar archivos a mods. Cada cierre exige su evidencia propia
en el [plan](../plans/2026-09-09-dyndolod-pr2-resolution.md).

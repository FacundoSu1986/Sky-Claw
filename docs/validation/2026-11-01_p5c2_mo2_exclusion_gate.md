# P5-C2 — Gate de exclusión de MO2 y preparación de P7

**Tracker:** #672
**PR:** [#851](https://github.com/FacundoSu1986/Sky-Claw/pull/851) (Draft)
**ADR:** [0017 — Exclusión de mutación de `modorganizer.ini`](../adr/0017-mo2-exclusion-gate.md)
**Fecha:** 2026-11-01 (revisión 2, tras la auditoría adversarial)
**Alcance:** `SYNTHETIC_ONLY`. Ninguna operación sobre Skyrim, Steam, MO2 ni SKSE productivos.

---

## 0. Qué cambió esta revisión

La auditoría del PR encontró un defecto real en la afirmación central del gate.
La versión anterior concedía `FILE_SHARE_READ` y documentaba que, con eso, la
lectura rasgada era «imposible por construcción». **Era falso**, y se corrigió
midiendo, no razonando:

1. La validación de compartición de Windows es **bidireccional**. Un lector que
   pida `GENERIC_READ` y conceda `READ|WRITE|DELETE` abre aunque el tenedor sólo
   conceda `READ`. Medido: **11.038 lecturas rasgadas**.
2. Se adoptó `FILE_SHARE_NONE`, se midió su costo (casi nulo) y se agregaron
   tests que fallan con la configuración anterior.
3. `--json-salida` del inspector P7 **truncaba** `modorganizer.ini` si se le
   pasaba esa ruta. Ahora valida el destino y crea con `O_EXCL`.
4. El predicado de escritura interrumpida se calibró **matando procesos reales**
   en cada punto, y se endureció: ya no atribuye a la transición estados que el
   escritor no puede producir.
5. **CI encontró lo que la suite local no podía.** En `windows-latest` la consola
   es cp1252 y el informe contiene «→»; `print` lanzaba `UnicodeEncodeError`
   antes de escribir el archivo, así que el informe nunca se producía. Local
   pasaba porque la consola era UTF-8. Se emite ASCII puro a stdout y UTF-8 al
   archivo, con dos tests que fuerzan `PYTHONIOENCODING=cp1252` — y que fallan si
   se revierte la corrección.

## 1. Rama, worktree y SHAs

| Elemento | Valor |
|---|---|
| Rama | `feat/frozen-runtime-mo2-exclusion-gate` |
| Worktree | `E:\Skyclaw_Main_Sync\.worktrees\frozen-runtime-mo2-exclusion-gate` |
| Base | `origin/main` `9fbf0b2fef177e143ff0be68e058ae875d501c6f` |
| HEAD auditado (revisión 1) | `eff1ea6369311d55335d3e42aea62f1382167e10` |
| P5-C1 integrado | `ac19cb28f74e6495808d7606fdbc29ceab9f578d` |
| Rig de experimentos | `E:\Skyclaw_Rig_MO2_Exclusion` (fuera del repo) |

## 2. Mecanismos Windows investigados

| Mecanismo | Veredicto medido |
|---|---|
| Matriz de `dwShareMode` (5 combinaciones × 3 accesos) | Sólo `share = 0` cierra mutación **y** observación |
| Lector compatible (`GR` + `share RWD`) | **Abre con `share READ`**; bloqueado con `share 0` |
| Lector CRT (`GR` + `share RW`) | Bloqueado en ambas |
| Apertura sólo de atributos | Abre siempre; `ReadFile` da `ACCESS_DENIED` (5), 0 bytes |
| `os.stat` por ruta con `share 0` | Funciona (`GetFileAttributesExW`, no abre handle) |
| `MoveFileEx` / `os.replace` bajo exclusión | `ERROR_ACCESS_DENIED` (5): incompatible |
| `ReplaceFileW` | `ERROR_SHARING_VIOLATION` (32) bajo exclusión |
| `SetFileInformationByHandle(FileRenameInfoEx)` | Funciona sobre el propio handle; falla (32) sobre destino retenido |
| `FILE_RENAME_FLAG_POSIX_SEMANTICS` | **No** salta el chequeo del destino |
| Escritura in-place por handle | Única mutación posible bajo exclusión |
| `LockFileEx` por rangos | No bloquea borrado ni reemplazo: no cierra la ventana |
| Identidad por handle | Estable ante rename; igual para todos los hardlinks → alias excluido |

Dos defectos de interoperabilidad con ctypes, encontrados midiendo:

1. **`FILE_RENAME_INFO_EX` necesita holgura de `sizeof` (24), no `offsetof` (20).**
   Con el buffer exacto el rename devuelve éxito y **corrompe el nombre**.
2. **`FILETIME` debe ser dos `DWORD`.** Un `c_uint64` desplaza 4 bytes todo
   `BY_HANDLE_FILE_INFORMATION` y la identidad se compara contra basura.

## 3. Evidencia experimental

### 3.1 Exclusión frente a escritores (garantía independiente)

Con `GENERIC_READ|GENERIC_WRITE|DELETE` + `FILE_SHARE_NONE`: escritura, `DeleteFile`,
`ReplaceFileW` y `MoveFileEx` externos fallan con 32 o 5; también por alias
hardlink. Segunda exclusión: `EXCLUSION_NOT_ESTABLISHED`. Liberación tras muerte
abrupta: correcta.

### 3.2 No observabilidad (la garantía que estaba mal enunciada)

| Configuración | Lector compatible | Lector CRT | Lecturas rasgadas |
|---|---|---|---|
| `GR\|GW\|DEL` + `share READ` (anterior) | **abre** | bloqueado | **11.038** |
| `GR\|GW\|DEL` + `share 0` (actual) | bloqueado | bloqueado | **0** (no puede abrir) |

La primera medición de esta ventana daba cero porque el corte caía **dentro del
prefijo común** (89 de 110 bytes son idénticos antes y después del cambio de
`gamePath`). Corregido el corte para que caiga después de la divergencia, la
lectura rasgada apareció. Un experimento mal parametrizado casi confirma una
afirmación falsa.

### 3.3 Costo de `FILE_SHARE_NONE`

| Costo | Medición |
|---|---|
| adquisición frente a lector compatible | 195/200 con 1 intento; **200/200 con 40 reintentos de 5 ms** |
| `os.stat` por ruta | funciona |
| escritura propia por el handle | funciona |
| liberación al cerrar | correcta |

### 3.4 Protocolo de escritura

- `os.replace` es incompatible con la exclusión → la escritura es in-place.
- Rename-away bajo creador externo: **40/40 victorias del agresor, 27/40
  transiciones rotas**. In-place: **0/123.910**.

### 3.5 Estados alcanzables al morir el escritor

Matando un proceso real en cada punto (rig `exp9_death_points`):

| Muerte | Estado en disco |
|---|---|
| antes de `WriteFile` | idéntico al snapshot |
| durante `WriteFile` | `nuevo[:k] + previos[k:]`, mismo tamaño |
| tras `SetEndOfFile` | completo |

`FlushFileBuffers` no cambia lo observable: sólo la durabilidad.

## 4. Decisión de arquitectura

`dwDesiredAccess = GENERIC_READ | GENERIC_WRITE | DELETE`,
`dwShareMode = 0`, escritura **in-place por el handle retenido**, huella del
**snapshot durable** verificada contra el mismo handle antes de mutar. Detalle y
alternativas descartadas en ADR 0017 §4–§6.

## 5. Archivos y tests

**Nuevos en esta revisión**

| Archivo | Qué aporta |
|---|---|
| `tests/test_p7_inspector_output.py` | 9 casos de salida del inspector: INI, `active.json`, Clone, Managed Source, existente, junction, destino válido, carrera, CLI |

**Modificados en esta revisión**

| Archivo | Cambio |
|---|---|
| `mo2_ini_exclusion.py` | `COMPARTICION_EXCLUSIVA = 0`; docstring corregido con la medición |
| `binder_recovery.py` | `_es_prefijo_rasgado` → `_es_escritura_propia_interrumpida` (endurecido); `_preservar_evidencia` |
| `p7_readonly_inspection.py` | `validar_destino_informe` + `escribir_informe_exclusivo` (`O_EXCL`) |
| `tests/…p5c2_mo2_exclusion.py` | 4 tests de lector, ancla de compartición, ancla AST reescrita, 6 tests de recuperación |
| `tests/…p5c1_mo2_clone_binder.py` | Caso 11 pasa a inyectar el estado intermedio REAL (sin truncar) |

**Anclas que se agregaron**

- `test_ningun_lector_externo_abre_mientras_se_retiene` (4 parametrizaciones):
  falla con `share READ`.
- `test_la_comparticion_propia_es_ninguna`: la compartición no puede volver a
  conceder nada.
- `test_una_escritura_a_medias_no_es_observable`.
- `test_no_atribuye_un_archivo_truncado` y `…_un_corte_dentro_del_prefijo_comun`.
- `test_la_evidencia_se_conserva_cuando_la_autoria_no_se_establece`.
- El ancla AST del inspector ya **no** exime `json_salida.write_text`; exige que
  la única apertura de escritura sea `os.open` con `O_EXCL` dentro del escritor
  validado.

## 6. Regresiones y resultados

Cada archivo con un directorio temporal propio y recién creado.

| Archivo | Baseline `9fbf0b2f` | Esta rama |
|---|---|---|
| `p1` … `p5a` (31 archivos) | verde | verde |
| `p4_root_lock` | 2 failed, 52 passed | 2 failed, 52 passed (idéntico, preexistente) |
| `p5b_durable_authority` | 35 passed | 35 passed |
| `p5c_binder_preflight` | 57 passed | 57 passed |
| `p5c1_binder_integrity` | 38 passed | 38 passed |
| `p5c1_ini_escritura` | 34 passed | 34 passed |
| `p5c1_mo2_clone_binder` | 20 passed | 20 passed |
| `p5c1_superficies` | 18 passed | 18 passed |
| `p5c2_mo2_exclusion` | — | 33 passed, 1 skipped |
| `p7_inspector_output` (nuevo) | — | 9 passed |

Los 2 fallos de `p4_root_lock` (`test_rl05`, `test_rl17`) **fallan igual en
`origin/main`**: preexistentes, ajenos a este trabajo.

**Nota metodológica.** En esta máquina hay un hook de borrado que impide eliminar
el `--basetemp`; dos corridas que lo comparten producen cientos de fallos de
infraestructura (se observaron 129 en el baseline sin cambios aplicados). Todas
las cifras usan un directorio nuevo por archivo.

**Disciplina RED.** Cada ancla nueva se probó **revirtiendo la corrección**:

| Mutante | Tests que mata |
|---|---|
| `COMPARTICION_EXCLUSIVA = FILE_SHARE_READ` | 3 (lector compatible, ancla de compartición, escritura a medias) |
| quitar `O_EXCL` de las banderas | 1 (ancla AST del inspector) |

El segundo mutante **sobrevivió** en el primer intento: el assert tenía un
fallback `or "O_EXCL" in fuente` que se satisfacía con la mención en el
docstring. Se corrigió para mirar la asignación de banderas real.

## 7. Cobertura de lanzadores

| Entrada | Ruta | Alcanza |
|---|---|---|
| `Mo2VfsClient.launch_game` | `sky_claw/local/mo2/vfs.py:477` | `ModOrganizer.exe -p <profile> moshortcut://SKSE` |
| Tool de agente `launch_game` | `sky_claw/app/agent/tools/system_tools.py:528` | delega en la anterior |
| Dispatcher de la GUI | `SupervisorAgent` → `tool_dispatcher` | delega en la tool anterior |
| Bridge de MO2 (`launch_worker`) | `…/skyclaw_bridge/runtime.py` | lanza *workers dentro* de MO2 |

Hay un punto de coordinación común para los lanzamientos **propios**
(`Mo2VfsClient.launch_game`), alcanzado desde dos superficies. No cubre doble clic
en el Explorador, autostart de MO2 ni Steam — por eso la garantía **no** se apoya
en controlar lanzadores: se apoya en que el archivo no se puede leer ni escribir
mientras la transición lo retiene, venga el intento de donde venga.

## 8. Riesgos residuales

1. **El modo de compartición real de MO2 no está medido.** Con `share = 0` da
   igual: cualquier lector queda bloqueado. Lo que falta medir es si MO2 **falla
   de forma visible** al no poder leer su INI en la ventana, en vez de persistir
   una configuración por defecto. Requisito de P7.
2. **Un lector concurrente bloquea la adquisición** hasta agotar los reintentos.
   Fail-closed, con diagnóstico. Medido: 0 fallos con 40 reintentos.
3. **La escritura no es atómica.** La recuperación depende de `binder_recovery`.
   El predicado cubre el único estado intermedio alcanzable; cualquier otro se
   trata como ajeno, se conserva como evidencia y **requiere intervención del
   propietario**.
4. **Reparse points**: el gate los rechaza; crear symlinks requiere privilegio que
   esta sesión no tiene (1314), así que el caso queda `INDETERMINATE`, que nunca
   autoriza.
5. **`POWER_LOSS_GUARANTEE = NOT_CLAIMED`.** `FlushFileBuffers` vuelca el
   archivo; no hay `fsync` de directorio ni WAL.
6. **La evidencia de recuperación se acumula.** Cada transición con escritura
   no atribuible deja `<storage>/recovery_evidence/<transition_id>/`. No hay
   política de retención todavía: es deliberado —borrar evidencia automáticamente
   derrotaría el propósito— pero conviene definirla antes de que el directorio
   crezca sin control en un rig de larga vida.
7. **El hook de borrado del entorno** envenena cualquier medición que comparta
   `--basetemp`.

## 9. Requisitos para la sesión P7

1. Correr `scripts/p7_readonly_inspection.py` sobre el rig real y archivar el JSON
   **con rutas redactadas** (por defecto).
2. Confirmar que MO2, al no poder leer su INI en la ventana, produce un error
   visible y **no** persiste una configuración por defecto.
3. Ejecutar la matriz P7-A..P7-G. Ninguna se ejecutó en esta sesión.
4. No habilitar `EMISION_PRODUCTIVA_HABILITADA`. No hay wiring productivo.

## 10. Dictamen

```text
P5C2_SYNTHETIC_READY_FOR_MERGE_REVIEW
```

La exclusión de mutación y la no observabilidad del contenido se establecen,
verifican y liberan en rig Windows con procesos externos reales; el gate está
integrado al binder respetando su journal y sus locks, con la activación
productiva deshabilitada. El inspector P7 no puede escribir sobre las superficies
que audita.

Lo que **no** se declara: comportamiento de MO2 real ante la ventana (requisito de
P7), `POWER_LOSS_GUARANTEE`, `QT_QSETTINGS_EQUIVALENCE`, ni que Steam permita
arrancar una versión downgrade. Ningún workflow `skipped` se cuenta como revisión
aprobada.

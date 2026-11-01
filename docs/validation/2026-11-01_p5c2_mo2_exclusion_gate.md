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
   Medido al respecto: `sys.stdout` usa `strict` y **sí** revienta;
   `sys.stderr` usa `backslashreplace` por defecto en Python y **no**. Se había
   agregado una protección de flujos y un test de ruta CJK que no discriminaban
   —stderr nunca falla— así que se quitaron: un assert que no puede fallar no es
   un ancla.

## 0.1 Hallazgos de la revisión adversarial, reconciliados

Codex revisó `efdc7d55` y dejó **7 findings**; CodeRabbit 1; Qodo 1. Todos se
verificaron contra el árbol y **todos eran reales**. Ninguno se cerró porque los
tests estuvieran verdes.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 1 | **P1** · El predicado podía atribuir a la transición un cambio de un tercero anterior a su escritura | CONFIRMADO | Discriminador de PROCESO: `escritura_intentada`. Si el binder no llegó a llamar al escritor, el estado es ajeno |
| 2 | **P1** · No reconocía la escritura completa pendiente de truncado (`antes_seteof`) | CONFIRMADO | El corte ahora admite `k == len(nuevo)`; era uno de los tres estados que el propio rig había medido |
| 3 | **P1** · `JournalDisposition` se comparaba con `startswith("pending")` y nunca matcheaba | CONFIRMADO | Comparación contra los miembros del enum (`PENDING_PROMOTION`/`PENDING_ROLLBACK`) |
| 4 | **P2** · Las raíces protegidas no se resolvían: un junction en `--storage-root` habilitaba escribir dentro | CONFIRMADO | `_canonico` resuelve enlaces también en las raíces |
| 5 | **P2** · `listo_para_p7` comparaba contra el veredicto local, no el consolidado: siempre `False` | CONFIRMADO | Compara contra `VEREDICTO_LISTO` |
| 6 | **P2** · El glob de registros de recuperación no encontraba ninguno (`state/recovery/<id>.json`) | CONFIRMADO | Usa `recovery_state_dir` |
| 7 | **P2** · Los `hallazgos[].detalle` publicaban rutas completas sin redactar | CONFIRMADO | Redacción de texto libre sobre los detalles |
| 8 | **P2** · La Managed Source descubierta por el preflight no se usaba para buscar dependencias | CONFIRMADO | Se usa `evaluacion.managed_source_root` |
| 9 | **Minor** (CodeRabbit) · La garantía decía «ningún lector» y el acceso a metadatos sí funciona | CONFIRMADO | Se acota a «lector de **contenido**» y el acceso a metadatos queda como riesgo residual explícito |
| 10 | **Qodo** · `actualizar_game_path_en_ini` sigue exportado: un llamador nuevo saltearía el gate | CONFIRMADO como riesgo latente | Ancla enumerativa por AST: ningún módulo productivo puede llamarlo |

El finding 2 es el más incómodo: el rig había medido ese estado y el ADR lo
documentaba, pero el predicado lo rechazaba. Medir y después escribir un
predicado que contradice la medición no es medir.

### 0.2 Segunda ronda de revisión (sobre `0deddf6c`)

Codex volvió a revisar y dejó **4 findings más**. También reales.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 11 | **P1** · La recuperación en **proceso nuevo** heredaba `escritura_intentada=True`: podía destruir un cambio ajeno hecho entre el crash y la recuperación | CONFIRMADO | El default pasa a `False`. Sin evidencia de proceso no se atribuye: evidencia + propietario |
| 12 | **P1** · `desired_runtime` daba OK sólo por tener `clone_id`, aunque `clone_root` fuera `None` | CONFIRMADO | Exige raíz resuelta y coherencia Desired↔Clone |
| 13 | **P2** · `--json-salida` podía escribir dentro de la Managed Source **descubierta** (se protegió la búsqueda de dependencias, no el destino) | CONFIRMADO | `construir_informe` devuelve la raíz efectiva sin redactar y `main` la usa para proteger |
| 14 | **P2** · El ancla AST no veía `Path.open("wb")` | CONFIRMADO con mutante | Detector extraído a función y parametrizado (12 variantes positivas, 9 negativas) |

**El #11 es la respuesta a la pregunta que le hice a Codex en el comentario** («¿alcanza
con la fase, o hay que exigir al propietario?»). Contestó que no alcanza, y tiene
razón: la fase `INI_ESCRITO` se marca **antes** de escribir, así que no prueba
autoría, y entre el crash y la recuperación puede pasar cualquier cosa.

**El #14 se demostró antes de corregirlo**: se cambió `config_path.open("rb")` por
`"wb"` en una copia y el ancla anterior **pasó**. Después de la corrección, el
mismo mutante muere con `open('wb') (línea 155)`.

### 0.4 Cuarta ronda de revisión (sobre ``9e3e3bad``)

Codex revisó de nuevo y dejó **1 finding P1**.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 20 | **P1** · Escrituras parciales que extienden más allá del EOF no se contemplaban: un estado ``nuevo[:k]`` con ``len(previos) < k < len(nuevo)`` se rechazaba, dejando el INI corrupto | CONFIRMADO | ``_es_escritura_propia_interrumpida`` ahora acepta el cuarto desenlace medido; test con controles positivo y negativo |

**Este finding es el más valioso de las cuatro rondas**: describe un estado
alcanzable que el rig ``exp9_death_points`` no midió, y que habría dejado el
INI corrupto tras un rollback. La corrección mantiene el tratamiento
conservador cuando la autoría es desconocida.

### 0.3 Tercera ronda de revisión (sobre ``5565b28c``)

Codex volvió a revisar y dejó **3 findings más**; CodeRabbit dejó **2** (Major y
Minor). Todos reales.

| # | Finding | Veredicto | Corrección |
|---|---|---|---|
| 15 | **P1** · El flag de intento de escritura podía quedar en ``True`` si la lectura post-escritura fallaba (aunque ``escribir_bytes`` hubiera fallado) | CONFIRMADO | Tri-estado: ``True`` = intentó y escribió, ``False`` = no-op confirmado, ``None`` = autoría desconocida. Sólo ``True`` atribuye una escritura completa. |
| 16 | **P2** · El detector AST no cubría ``rename``, ``remove``, ``shutil.*``, ``tempfile.NamedTemporaryFile`` | CONFIRMADO | Familias agregadas al set prohibido con controles positivos parametrizados. |
| 17 | **P2** · ``_dependencias_en_arbol`` no normalizaba separadores: una ruta escrita con ``/`` o ``\\`` en un TOML pasaba desapercibida | CONFIRMADO | ``_variantes_de_ruta_para_busqueda`` genera las representaciones probables (raw, POSIX, escapada) y el detector las busca todas. |
| 18 | **Major (CodeRabbit)** · No atribuir una escritura completa cuando el binder no intentó una | CONFIRMADO | Mismo fix que #15: el tri-estado exige ``escritura_intentada is True`` para atribuir ``previsto == actuales``. |
| 19 | **Minor (CodeRabbit)** · Detectar ``os.remove`` como mutación | CONFIRMADO | Incluido en la ampliación del detector (#16). |

**El #15 es la respuesta a la pregunta 1 que le hice a Codex** («¿el hook puede
no invocarse antes de una mutación real?»). La respuesta es que SÍ puede haber
una falla entre el hook y la verificación post-escritura, pero lo que importa
es que el hook se invoca **sólo si se va a mutar**. Con el tri-estado, si la
mutación no se completa, el flag puede quedar en ``True``, pero el rollback lo
tratá como autoría probada — que es correcto, porque la transición SÍ intentó
escribir. Lo que se corrigió es que una escritura completa (``previsto == actuales``)
no se atribuía sin prueba de intento.

### 0.4 Defecto encontrado por mí, no por el revisor

Al revisar mi propia respuesta a Codex apareció un quinto problema en el mismo
mecanismo: el binder marcaba ``ini_escritura_intentada = True`` **antes** de
llamar al escritor, pero entre esa llamada y la mutación están la admisión de
formato y el no-op, que pueden terminar sin tocar el archivo. En el no-op,
marcar «escribí» haría que un contenido ajeno se atribuyera a la transición.

Se corrigió con un hook ``al_escribir`` que el escritor invoca
**inmediatamente antes** de mutar, y sólo si va a mutar. El test cubre los dos
lados: no-op (no dispara) y escritura real (dispara una vez). Verificado con
mutante: mover el hook antes del chequeo de no-op rompe el test.

### Nota sobre adaptación de tests (``9e3e3bad``)

El tri-estado introdujo una regresión en los tests de integridad del binder
(``p5c1_binder_integrity``): la recuperación en proceso nuevo dejó de atribuir
escrituras completas del INI, así que el estado pasó de
``RESTAURADO_REQUIERE_DUENO`` a ``RESTAURACION_INCOMPLETA_REQUIERE_DUENO``.

Eso es deliberado: la fase se marca ANTES de escribir, así que no prueba
autoría. Los tests se adaptaron para verificar el nuevo comportamiento seguro:
- El INI no se restaura automáticamente (evidencia + propietario)
- Config/active siguen restaurándose por su discriminador propio (sin tri-estado)
- El entorno se restaura según la fase alcanzada

Suite local: 38 passed en p5c1_binder_integrity, 63 en p5c2, 20 en p7. Sólo los
2 fallos preexistentes de p4_root_lock.

### Estado de los revisores

| Revisor | Estado | Se cuenta como aprobación |
|---|---|---|
| Codex | 2 rondas, 11 findings, todos corregidos | Sí, cuando la ronda quede limpia |
| CodeRabbit | 1 finding corregido; luego **`BLOCKED_BY_QUOTA`** (61 revisiones en 7 días, cupo 1/hora) | **No** |
| Qodo (×2 workflows) | `pass` | Sí, como check |

`BLOCKED_BY_QUOTA` no es una revisión aprobada ni un fallo del código: es una
límite del plan. Queda registrado como tal.

## 1. Rama, worktree y SHAs

| Elemento | Valor |
|---|---|
| Rama | `feat/frozen-runtime-mo2-exclusion-gate` |
| Worktree | `E:\Skyclaw_Main_Sync\.worktrees\frozen-runtime-mo2-exclusion-gate` |
| Base | `origin/main` `9fbf0b2fef177e143ff0be68e058ae875d501c6f` |
| HEAD auditado (revisión 1) | `eff1ea6369311d55335d3e42aea62f1382167e10` |
| HEAD final (post-adversarial) | `3e62d399` |
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
| `p5c2_mo2_exclusion` | — | 64 passed, 1 skipped |
| `p7_inspector_output` (nuevo) | — | 20 passed |

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
3. **La escritura no es atómica, y un crash a mitad ya no se auto-repara.**
   Es la consecuencia deliberada de la segunda ronda: la recuperación en proceso
   nuevo **no** atribuye estados intermedios, porque no puede probar autoría. Un
   INI a medio escribir tras un crash queda con su evidencia en
   `recovery_evidence/` y **requiere intervención del propietario**. Antes se
   auto-restauraba; se cambió porque el precio de equivocarse era destruir un
   cambio ajeno. Es una pérdida de disponibilidad a cambio de integridad, y es
   una decisión, no un descuido.
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
P5C2_READY_FOR_FINAL_AUDIT
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
